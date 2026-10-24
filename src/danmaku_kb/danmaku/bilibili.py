# -*- coding: utf-8 -*-
"""B 站弹幕抓取（基于 blivedm）。

blivedm 封装了 B 站的弹幕长连接协议（含握手、心跳、认证），
本模块只负责把它的回调消息翻译成统一的
:class:`~danmaku_kb.danmaku.message.DanmakuMessage`。

参考项目在这个文件里踩过两个坑，这里都避开了：

1. 调用了 blivedm **不存在**的 ``set_handler`` —— 新版是 ``add_handler``。
   本模块用 ``_register_handler`` 做能力探测，两个都试。
2. ``stop()`` 里设了 ``_stopping`` 标志，而 ``disconnect()`` 开头
   ``if self._stopping: return``，导致断开永远不可达。
   本模块把"停止"与"正在清理"拆成两个标志（见基类）。

字段名以 blivedm 0.1.1 为准：``DanmakuMessage.uname/.uid/.msg``、
``GiftMessage.uname/.uid/.gift_name/.num``、``GuardBuyMessage.username/.uid/.guard_level``、
``SuperChatMessage.uname/.uid/.price/.message``。
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
from datetime import datetime
from typing import Any, Dict, Optional

from ..utils.helpers import extract_bilibili_room_id
from ..utils.logging_setup import get_logger
from .base import DanmakuFetcher
from .bilibili_wbi import fetch_danmu_info as _fetch_signed_danmu_info
from .message import DanmakuMessage, MessageType
from .protobuf_lite import ProtobufError, get_int, get_str, parse_message

logger = get_logger("danmaku.bilibili")

#: 诊断请求必须伪装成浏览器，否则 B 站直接回 HTTP 412 + 反爬 HTML（实测）。
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ----------------------------------------------------------------------
# 可选依赖探测
# ----------------------------------------------------------------------

try:  # pragma: no cover - 依赖环境
    import aiohttp

    AIOHTTP_AVAILABLE = True
except Exception:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:  # pragma: no cover - 依赖环境
    import blivedm

    BLIVEDM_AVAILABLE = True
except Exception:  # pragma: no cover
    blivedm = None  # type: ignore[assignment]
    BLIVEDM_AVAILABLE = False

#: blivedm 的模型模块。**包结构随版本变过**：PyPI 上的 0.1.x 是扁平单文件
#: （``blivedm/models.py``），而仓库 master 上是包（``blivedm/models/web.py``）。
#: 这里只探测、不强制——早期版本把它写成 ``import blivedm.models.web`` 放在
#: 同一个 try 里，于是 0.1.1 环境下**整段 import 失败**，``BLIVEDM_AVAILABLE``
#: 被置成 False，B 站弹幕直接不可用（还谎报"缺少 blivedm 依赖"）。
_blivedm_models: Any = None
if BLIVEDM_AVAILABLE:  # pragma: no cover - 依赖环境
    for _mod_name in ("blivedm.models.web", "blivedm.models"):
        try:
            _blivedm_models = importlib.import_module(_mod_name)
            break
        except Exception:
            continue


def _first_attr(obj: Any, *names: str, default: Any = "") -> Any:
    """从对象上按顺序取第一个存在的属性（非 None、非空）。

    B 站消息字段名在不同 blivedm 版本间有出入（``uname`` vs ``username``），
    这个辅助函数让代码对新旧版本都成立。
    """
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return default


#: blivedm 自带的默认弹幕服务器列表。``getDanmuInfo`` 签名接口偶发失败时
#: 退回这里，至少还有机会连上（旧版 blivedm 本来就想这么降级，
#: 但它在 ``init_room()`` 里紧接着抛 ``InitError``，降级其实从未生效）。
#: 用 importlib 探测而不是 ``from blivedm.client import ...``：包的布局在
#: 不同版本变过，硬编码子模块导入会把依赖探测变成"装了也报缺依赖"。
_DEFAULT_SERVERS: Any = []
if BLIVEDM_AVAILABLE:  # pragma: no cover - 依赖环境
    try:
        _DEFAULT_SERVERS = list(
            getattr(importlib.import_module("blivedm.client"),
                    "DEFAULT_DANMAKU_SERVER_LIST", []) or []
        )
    except Exception:
        _DEFAULT_SERVERS = []


if BLIVEDM_AVAILABLE:  # pragma: no cover - 依赖环境
    _BaseClient = getattr(blivedm, "BLiveClient", object)

    class _SignedHostServerClient(_BaseClient):  # type: ignore[misc, valid-type]
        """修正 blivedm 0.1.1 与现行 B 站接口的三处不兼容。

        1. ``_init_room_id_and_owner()`` 请求的 ``getInfoByRoom`` 现在一律回
           ``-352``（实测未签名/签名都一样），于是房间号拿不到；
        2. ``_init_host_server()`` 请求的 ``getDanmuInfo`` 不带 WBI 签名也一律
           ``-352``，而 ``init_room()`` 见它失败就抛 ``InitError``；
        3. ``init_room()`` 把"房间信息失败"当致命错误，其实弹幕鉴权只需要
           房间号本身——``room_init`` 接口（``?id=``）没有风控。

        这里用 ``room_init`` 取房间号、用 WBI 签名取服务器列表，并把
        ``init_room()`` 改成"房间信息失败不算死"的宽松语义。
        """

        async def _init_room_id_and_owner(self) -> bool:
            try:
                async with self._session.get(
                    "https://api.live.bilibili.com/room/v1/Room/room_init",
                    headers={"User-Agent": _BROWSER_UA},
                    params={"id": self._tmp_room_id},
                    ssl=self._ssl,
                ) as res:
                    if res.status != 200:
                        logger.warning("B 站 room_init HTTP %s", res.status)
                        return False
                    data = await res.json(content_type=None)
            except Exception as exc:  # pragma: no cover - 网络异常分支
                logger.warning("B 站 room_init 请求失败: %s", exc)
                return False
            if (data or {}).get("code") != 0:
                logger.warning("B 站 room_init 返回 %s", (data or {}).get("code"))
                return False
            info = (data or {}).get("data") or {}
            self._room_id = info.get("room_id") or self._tmp_room_id
            self._room_short_id = info.get("short_id") or 0
            self._room_owner_uid = info.get("uid") or 0
            return True

        async def _init_host_server(self) -> bool:
            try:
                data = await _fetch_signed_danmu_info(self._session, self._room_id)
            except Exception as exc:  # pragma: no cover - 防御性兜底
                logger.warning("B 站签名获取弹幕服务器失败: %s", exc)
                data = None
            if data and self._parse_danmaku_server_conf(data):
                return True
            if _DEFAULT_SERVERS:
                logger.warning("B 站签名接口不可用，退回默认弹幕服务器列表")
                self._host_server_list = list(_DEFAULT_SERVERS)
                self._host_server_token = None
                return True
            return False

        async def init_room(self) -> bool:
            if not await self._init_room_id_and_owner():
                # 房间信息失败不致命：ws 鉴权只需要房间号，直接用用户给的号
                self._room_id = self._room_short_id = self._tmp_room_id
                self._room_owner_uid = 0
            return await self._init_host_server()

else:  # pragma: no cover - 依赖环境
    _SignedHostServerClient = None  # type: ignore[assignment]


class _WsReadyHandler:
    """观察者 handler：收到第一条（已认证的）命令时置位 ``fetcher._ws_ready``。

    .. warning::

       这里**不能**依赖 ``_on_ws_connect`` / ``_on_ws_close`` 这两个钩子。
       blivedm 0.1.1 的 ``client.py:396`` 调用的是 **BLiveClient 自己**的
       ``_on_ws_connect()``（只发认证包 + 起心跳定时器），它**从不遍历**
       ``self._handlers``；``handlers.py`` 里也没有这两个钩子。旧实现把
       就绪信号挂在这两个方法上，于是 ``_ws_ready`` 永远是 ``False``，
       ``connect()`` 每次都在 15s 超时后返回 False —— B 站弹幕因此
       100% 连不上，还会误报"本地网络屏蔽了 2243/443 端口"。

    另外，blivedm 在 ``client.py:597-601`` 对每个 handler 调
    ``handler.handle(self, command)``：**handler 必须有 ``handle()``**，
    否则 ``AttributeError`` 会在 ``asyncio.gather(...)`` 的参数求值阶段抛出，
    ``return_exceptions=True`` 也救不了，最终被 ``_on_ws_message`` 的宽泛
    ``except`` 吞掉 —— 表现为"连上了但一条弹幕都收不到"。

    所以这里既补上真正的 ``handle()``，``_wait_ws_ready`` 也改为直接看
    ``client._websocket``（握手成功即置位），两条证据互为补充。
    """

    def __init__(self, fetcher: "BilibiliFetcher") -> None:
        self.fetcher = fetcher

    async def handle(self, client: Any, command: Any) -> None:
        """blivedm 的 handler 入口。第一条命令说明认证已通过、链路可用。"""
        self.fetcher._ws_ready = True


def _parse_interact_word_v2(pb_b64: Any) -> Optional[Dict[str, Any]]:
    """解析 ``INTERACT_WORD_V2`` 的 protobuf 载荷（进场 / 关注 / 分享）。

    字段号来自 2026-10-24 对真实 WS 帧的实测取证（房间 21414905）：:

        1  uid        varint（游客/隐身时服务端直接省略）
        2  uname      string
        4  identities packed repeated varint（粉丝勋章身份位）
        5  msg_type   varint（1=进入 2=关注 3=分享）
        6  room_id    varint
        7  timestamp  秒
        8  timestamp  毫秒
        15 msg_id     uint64

    为什么要自己解：blivedm 0.1.1 的 ``handlers.py`` 根本不认
    ``INTERACT_WORD_V2``（V1 才在 ``IGNORED_CMDS`` 里），服务端把 V1 换成 V2
    之后进场/关注就整批丢了 —— 日志里只剩一行 ``unknown cmd=INTERACT_WORD_V2``。

    :returns: 解析结果 dict；载荷不是合法 protobuf 时返回 ``None``（不抛异常，
        平台改协议时只会少一类消息，不会断掉整条链路）。
    """
    if not pb_b64:
        return None
    try:
        raw = base64.b64decode(str(pb_b64), validate=False)
    except (ValueError, TypeError):
        return None
    if not raw:
        return None
    try:
        fields = parse_message(raw)
    except ProtobufError:
        return None
    return {
        "uid": get_int(fields, 1),
        "uname": get_str(fields, 2),
        "msg_type": get_int(fields, 5),
        "room_id": get_int(fields, 6),
        # 毫秒字段（8）精度更高，秒字段（7）兜底
        "timestamp": (get_int(fields, 8) // 1000) or get_int(fields, 7),
    }


class _BiliHandler:  # pragma: no cover - 需要真实连接
    """blivedm 回调处理器。

    继承 ``blivedm.BaseHandler`` 以拿到默认空实现（这样只需覆写关心的事件）。
    """

    def __init__(self, fetcher: "BilibiliFetcher") -> None:
        self.fetcher = fetcher
        # 在线人数/WATCHED_CHANGE 大约 5 秒推一次，值不变时重复发会把弹幕列表刷屏
        self._last_stats_content = ""
        self._last_stats_at = 0.0

    # --- 弹幕 ---
    async def _on_danmaku(self, client: Any, message: Any) -> None:
        try:
            content = getattr(message, "msg", None) or getattr(message, "content", "") or ""
            nickname = _first_attr(message, "uname", "username", "author", default="未知用户")
            uid = str(_first_attr(message, "uid", "user_id", default=""))
            self.fetcher._emit_message(
                DanmakuMessage(
                    content=content,
                    nickname=str(nickname),
                    user_id=uid,
                    platform="bilibili",
                    room_id=self.fetcher.room_id,
                    msg_type=MessageType.DANMAKU,
                    medal_name=str(getattr(message, "medal_name", "") or ""),
                    medal_level=int(getattr(message, "medal_level", 0) or 0),
                    level=int(getattr(message, "user_level", 0) or 0),
                )
            )
        except Exception as exc:
            logger.error("处理弹幕消息失败: %s", exc)

    # --- 礼物 ---
    async def _on_gift(self, client: Any, message: Any) -> None:
        try:
            gift_name = getattr(message, "gift_name", None)
            if not gift_name:
                gift = getattr(message, "gift", None)
                if isinstance(gift, dict):
                    gift_name = gift.get("name")
            gift_name = gift_name or "礼物"
            num = int(getattr(message, "num", 1) or 1)
            self.fetcher._emit_message(
                DanmakuMessage(
                    content=f"送出 {gift_name} x{num}",
                    nickname=str(_first_attr(message, "uname", "username", default="未知用户")),
                    user_id=str(_first_attr(message, "uid", "user_id", default="")),
                    platform="bilibili",
                    room_id=self.fetcher.room_id,
                    msg_type=MessageType.GIFT,
                    extra={"gift_name": gift_name, "num": num},
                )
            )
        except Exception as exc:
            logger.error("处理礼物消息失败: %s", exc)

    # --- 上舰 ---
    async def _on_buy_guard(self, client: Any, message: Any) -> None:
        try:
            level = int(getattr(message, "guard_level", 0) or 0)
            names = {1: "总督", 2: "提督", 3: "舰长"}
            label = names.get(level, f"{level}级舰长")
            self.fetcher._emit_message(
                DanmakuMessage(
                    content=f"开通了{label}",
                    nickname=str(_first_attr(message, "username", "uname", default="未知用户")),
                    user_id=str(_first_attr(message, "uid", "user_id", default="")),
                    platform="bilibili",
                    room_id=self.fetcher.room_id,
                    msg_type=MessageType.GUARD,
                    extra={"guard_level": level},
                )
            )
        except Exception as exc:
            logger.error("处理上舰消息失败: %s", exc)

    # --- 醒目留言 ---
    async def _on_super_chat(self, client: Any, message: Any) -> None:
        try:
            price = getattr(message, "price", 0)
            text = getattr(message, "message", "") or ""
            self.fetcher._emit_message(
                DanmakuMessage(
                    content=f"¥{price} {text}".strip(),
                    nickname=str(_first_attr(message, "uname", "username", default="未知用户")),
                    user_id=str(_first_attr(message, "uid", "user_id", default="")),
                    platform="bilibili",
                    room_id=self.fetcher.room_id,
                    msg_type=MessageType.SUPER_CHAT,
                    extra={"price": price},
                )
            )
        except Exception as exc:
            logger.error("处理醒目留言失败: %s", exc)

    # --- 进场 / 点赞 ---
    #
    # .. note::
    #
    #    这两个事件 blivedm 0.1.1 **不会**分派给我们：``handlers.py:15-39`` 把
    #    ``INTERACT_WORD`` 放进 ``IGNORED_CMDS``（``_CMD_CALLBACK_DICT`` 里置 None），
    #    ``LIKE`` 压根不在字典里。旧实现只写了 ``_on_interact_word`` / ``_on_like``
    #    两个回调，指望 BaseHandler 调它们——那是**死代码**，进场/点赞永远收不到。
    #    现在改由 :meth:`handle` 自己从原始 ``cmd`` 里挑出来（见下），
    #    所以这两个方法改成接收 ``data`` dict。
    async def _on_interact_word(self, client: Any, data: Any) -> None:
        try:
            payload = data if isinstance(data, dict) else {}
            # msg_type: 1=进入直播间, 2=关注, 3=分享
            msg_type = int(payload.get("msg_type", 0) or 0)
            texts = {1: "进入了直播间", 2: "关注了主播", 3: "分享了直播间"}
            text = texts.get(msg_type)
            if not text:
                return
            kinds = {
                1: MessageType.ENTER,
                2: MessageType.FOLLOW,
                3: MessageType.SYSTEM,
            }
            self.fetcher._emit_message(
                DanmakuMessage(
                    content=text,
                    nickname=str(payload.get("uname") or "未知用户"),
                    user_id=str(payload.get("uid") or ""),
                    platform="bilibili",
                    room_id=self.fetcher.room_id,
                    msg_type=kinds[msg_type],
                )
            )
        except Exception as exc:
            logger.error("处理进场消息失败: %s", exc)

    async def _on_like(self, client: Any, data: Any) -> None:
        try:
            payload = data if isinstance(data, dict) else {}
            self.fetcher._emit_message(
                DanmakuMessage(
                    content="点赞了直播间",
                    nickname=str(payload.get("uname") or "未知用户"),
                    user_id=str(payload.get("uid") or ""),
                    platform="bilibili",
                    room_id=self.fetcher.room_id,
                    msg_type=MessageType.LIKE,
                )
            )
        except Exception as exc:
            logger.error("处理点赞消息失败: %s", exc)

    # --- 进场（V2 协议）/ 在线人数 ---
    #
    # 2026-10 实测：服务端已把进场事件从 ``INTERACT_WORD`` 换成
    # ``INTERACT_WORD_V2``（protobuf 载荷放在 ``data.pb``，base64）。
    # blivedm 0.1.1 不认识它，所以必须在 :meth:`handle` 里自己接。
    async def _on_interact_word_v2(self, client: Any, data: Any) -> None:
        try:
            payload = data if isinstance(data, dict) else {}
            parsed = _parse_interact_word_v2(payload.get("pb"))
            if not parsed:
                return
            texts = {1: "进入了直播间", 2: "关注了主播", 3: "分享了直播间"}
            kinds = {1: MessageType.ENTER, 2: MessageType.FOLLOW, 3: MessageType.SYSTEM}
            msg_type = int(parsed.get("msg_type") or 0)
            text = texts.get(msg_type)
            if not text:
                return
            ts = parsed.get("timestamp") or 0
            self.fetcher._emit_message(
                DanmakuMessage(
                    content=text,
                    nickname=str(parsed.get("uname") or "未知用户"),
                    user_id=str(parsed.get("uid") or ""),
                    platform="bilibili",
                    room_id=self.fetcher.room_id,
                    msg_type=kinds[msg_type],
                    timestamp=ts or datetime.now(),
                    extra={"protocol": "INTERACT_WORD_V2"},
                )
            )
        except Exception as exc:
            logger.error("处理进场消息(V2)失败: %s", exc)

    def _stats_duplicate(self, content: str, window: float = 15.0) -> bool:
        """同一统计文案在 ``window`` 秒内只发一次（``WATCHED_CHANGE`` 约 5s 一推）。"""
        import time

        now = time.monotonic()
        if content == self._last_stats_content and now - self._last_stats_at < window:
            return True
        self._last_stats_content = content
        self._last_stats_at = now
        return False

    async def _on_watched_change(self, client: Any, data: Any) -> None:
        """``WATCHED_CHANGE``：累计观看人次（``num`` + 已格式化文案）。"""
        try:
            payload = data if isinstance(data, dict) else {}
            num = int(payload.get("num") or 0)
            text = str(payload.get("text_small") or "")
            if not num and not text:
                return
            content = f"看过 {text or num}"
            # 累计观看只在跨量级时才有信息量，窗口给大一点
            if self._stats_duplicate(content, window=60.0):
                return
            self.fetcher._emit_message(
                DanmakuMessage(
                    content=content,
                    platform="bilibili",
                    room_id=self.fetcher.room_id,
                    msg_type=MessageType.STATS,
                    extra={"viewers": num, "viewers_text": text},
                )
            )
        except Exception as exc:
            logger.error("处理观看人数失败: %s", exc)

    async def _on_online_rank_count(self, client: Any, data: Any) -> None:
        """``ONLINE_RANK_COUNT``：当前在线人数（``online_count`` 字段）。"""
        try:
            payload = data if isinstance(data, dict) else {}
            online = int(payload.get("online_count") or 0)
            if not online:
                online = int(payload.get("count") or 0)
            if not online:
                return
            content = f"在线 {online} 人"
            # 在线人数几秒一跳，30 秒内同值算重复
            if self._stats_duplicate(content, window=30.0):
                return
            self.fetcher._emit_message(
                DanmakuMessage(
                    content=content,
                    platform="bilibili",
                    room_id=self.fetcher.room_id,
                    msg_type=MessageType.STATS,
                    extra={"online": online},
                )
            )
        except Exception as exc:
            logger.error("处理在线人数失败: %s", exc)

    async def handle(self, client: Any, command: Any) -> None:
        """blivedm 的 handler 入口：先摘出被 blivedm 忽略的命令，再走默认分派。

        ``client.py:597-601`` 用 ``asyncio.gather(*(h.handle(self, command) ...))``
        分派，**每个 handler 都必须有 ``handle()``**；缺了会在 gather 的参数
        求值阶段抛 ``AttributeError``，被 ``_on_ws_message`` 的宽泛 except 吞掉，
        结果是"连上了但一条弹幕都收不到"。
        """
        cmd = ""
        data: Any = {}
        if isinstance(command, dict):
            cmd = str(command.get("cmd") or "")
            data = command.get("data") or {}
        base_cmd = cmd.split(":", 1)[0]
        try:
            if base_cmd == "INTERACT_WORD":
                await self._on_interact_word(client, data)
                return
            if base_cmd.startswith("LIKE_INFO"):
                await self._on_like(client, data)
                return
            if base_cmd == "INTERACT_WORD_V2":
                await self._on_interact_word_v2(client, data)
                return
            if base_cmd == "WATCHED_CHANGE":
                await self._on_watched_change(client, data)
                return
            if base_cmd == "ONLINE_RANK_COUNT":
                await self._on_online_rank_count(client, data)
                return
        except Exception as exc:  # pragma: no cover - 防御
            logger.error("处理扩展命令 %s 失败: %s", base_cmd, exc)
            return

        parent = getattr(super(), "handle", None)
        if parent is None:  # pragma: no cover - blivedm 不可用时无默认分派
            return
        await parent(client, command)


# 若 blivedm 可用，让处理器真正继承它的基类（拿到默认空实现）
if BLIVEDM_AVAILABLE:  # pragma: no cover - 依赖环境
    try:
        _BaseHandler = blivedm.BaseHandler  # type: ignore[attr-defined]

        class BiliHandler(_BiliHandler, _BaseHandler):  # type: ignore[misc]
            """继承 blivedm.BaseHandler 的处理器。"""

            def __init__(self, fetcher: "BilibiliFetcher") -> None:
                _BaseHandler.__init__(self)
                _BiliHandler.__init__(self, fetcher)

    except Exception:  # pragma: no cover
        BiliHandler = _BiliHandler  # type: ignore[misc,assignment]
else:  # pragma: no cover
    BiliHandler = _BiliHandler  # type: ignore[misc,assignment]


class BilibiliFetcher(DanmakuFetcher):
    """B 站弹幕抓取器。"""

    supports_realtime = True
    platform = "bilibili"

    def __init__(self, sessdata: str = "") -> None:
        super().__init__()
        self.sessdata = sessdata
        self._client: Any = None
        self._session: Any = None
        self._handler: Any = None
        self._join_task: Optional[asyncio.Task] = None
        #: WebSocket 真正握手成功时由 ``_WsReadyHandler`` 置位。
        self._ws_ready = False
        #: 连上后等待 WebSocket 就绪的秒数。
        self.connect_timeout = 15.0

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    async def connect(self, room: str, sessdata: str = "", **kwargs: Any) -> bool:
        """连接 B 站直播间。

        :param room: 直播间链接或房间号
        :param sessdata: 登录 Cookie 的 SESSDATA；不发言的话可以留空
        """
        if not BLIVEDM_AVAILABLE:
            self._notify_error("缺少 blivedm 依赖，请运行: pip install blivedm")
            return False
        if not AIOHTTP_AVAILABLE:
            self._notify_error("缺少 aiohttp 依赖，请运行: pip install aiohttp")
            return False

        room_id = extract_bilibili_room_id(str(room))
        if not room_id:
            self._notify_error(f"无法从 {room!r} 解析出 B 站房间号，请输入直播间链接或纯数字房间号")
            return False

        self._room_id = room_id
        if sessdata:
            self.sessdata = sessdata

        try:
            # 必须显式声明 Accept-Encoding：aiohttp 检测到本机有 brotli 可用时会自动
            # 带上 ``br``，但环境里的 brotli 后端未必与 aiohttp 的调用约定匹配
            # （实测 ``_brotli.Decompressor.process()`` 只接受 1 个参数，而
            # aiohttp 3.14.4 传 2 个），于是整个响应解码失败：
            # ``ClientPayloadError: 400, message: Can not decode content-encoding: br``。
            # 抖音抓取器早就为此显式声明过头，B 站这条路径当时漏了。
            self._session = aiohttp.ClientSession(
                headers={"Accept-Encoding": "gzip, deflate"},
                # 只限制建连阶段：长连接的 ws 会一直挂着，设 ``total`` 会在
                # 若干秒后把好好的弹幕连接掐断（这里不能图省事写 total=10）。
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
            )

            # 把 SESSDATA 塞进 cookie jar，blivedm 建立长连接时会带上
            if self.sessdata:
                self._session.cookie_jar.update_cookies(
                    {"SESSDATA": self.sessdata},
                    response_url=_yarl_url("https://live.bilibili.com"),
                )
                logger.info("已设置 SESSDATA（长度 %d）", len(self.sessdata))
            else:
                logger.info("未提供 SESSDATA，以游客身份连接（只能收弹幕，无法发言）")

            # 登录态自检（原因见 ``_probe_login_mid``）：SESSDATA 过期时
            # 服务端按游客下发掩码昵称，用户会误以为程序坏了。
            login_mid = 0
            if self.sessdata:
                login_mid = await _probe_login_mid(self._session)
                if login_mid:
                    logger.info("B 站登录态有效（mid=%s），WS 鉴权带上 uid", login_mid)
                else:
                    self.emit_system(
                        "B 站 Cookie（SESSDATA）已失效或无效，当前以游客身份接收弹幕："
                        "发送者昵称会显示成「伊***」这类掩码。请重新扫码登录后再连。"
                    )

            # blivedm 0.1.x 是 BLiveClient；更早版本是单例式的 LiveClient
            # 优先用本地子类：它会用带 WBI 签名的 getDanmuInfo 取服务器列表。
            client_cls = _SignedHostServerClient or getattr(blivedm, "BLiveClient", None)
            if client_cls is None:
                self._notify_error("blivedm 版本不受支持（找不到 BLiveClient）")
                return False

            try:
                self._client = client_cls(int(room_id), uid=login_mid, session=self._session)
            except TypeError:
                # 旧版本/测试替身不接受 uid 关键字：退回只传房间号
                self._client = client_cls(int(room_id))

            self._handler = BiliHandler(self)
            if not self._register_handler(self._client, self._handler):
                self._notify_error("无法向 blivedm 客户端注册处理器")
                return False

            # 再挂一个只负责"WebSocket 真的连上了"的观察者。
            # blivedm 在 ws 握手成功后回调每个 handler 的 _on_ws_connect()，
            # 这是唯一可靠的"已就绪"信号（init_room 可能因 -352 风控降级，
            # 但那只是换了服务器列表，不代表连接失败）。
            self._register_handler(self._client, _WsReadyHandler(self))

            self._client.start()

            # 后台协程：join() 会一直阻塞到连接结束（blivedm 内部自带重连循环，
            # 所以正常掉线不会让它返回；只有 stop / 认证彻底失败 / SSL 错误才会）。
            self._join_task = asyncio.ensure_future(self._client.join())
            # 它结束时必须让基类知道，否则 `_thread_main` 的空闲循环会一直
            # 空转，界面永远停在"运行中"，且 `_running` 为 True 导致无法重连。
            self._join_task.add_done_callback(self._on_join_done)

            # **不能**在 start() 之后立刻报"已连接"：blivedm 的 start() 只是把一个
            # 后台协程排进事件循环，真正的 ``init_room()`` → ``ws_connect`` → 认证
            # 都还没发生。实测在 -352 风控下 init_room 会失败并降级到默认服务器列表，
            # 若此时就 ``_notify_status(True)``，界面会显示"已连接"而实际一帧都收不到。
            # 这里等 WebSocket 真的连上再上报。
            if not await self._wait_ws_ready():
                # 等不到就如实报错，不假装成功。但只说"未就绪"对用户毫无帮助：
                # 先问一次**已签名**接口（拿到服务器列表 → 问题在 WS 握手），
                # 再问一次**未签名**接口只为拿错误码并翻成人话。
                # 注意：历史上把 -352 全归因于"出口 IP 风控"是错的——真正的根因
                # 是 getDanmuInfo 缺 WBI 签名（见 bilibili_wbi.py），签名补齐后
                # 正常网络已经能连；现在仍报 -352 才真的是风控。
                reason = await self._diagnose_ws_failure()
                self._notify_status(False, reason)
                self._notify_error(reason)
                logger.warning("B 站 WebSocket 在 %.1fs 内未就绪：%s",
                               self.connect_timeout, reason)
                return False

            self._notify_status(True, "已连接 B 站直播间")
            logger.info("已连接 B 站直播间 %s", room_id)
            # **立刻返回**。基类的 `_thread_main` 是
            # ``run_until_complete(connect(...))`` 然后再进入
            # ``while not self._stopping`` 的空闲循环 —— 若在这里
            # ``await self._join_task``，connect 永不返回，空闲循环到不了，
            # ``stop()`` 置的 ``_stopping`` 也无人检查，于是线程永不退出、
            # ``_running`` 永为 True、下次 start() 直接失败。
            # 连接中断由 `_on_join_done` 回调负责上报并让线程退出。
            return True

        except Exception as exc:
            logger.exception("连接 B 站失败: %s", exc)
            self._notify_error(f"连接失败: {exc}")
            return False

    def _on_join_done(self, task: "asyncio.Future[Any]") -> None:
        """``client.join()`` 结束时的回调：非正常停止则上报错误并让线程退出。

        没有这个回调时，blivedm 因认证失败 / SSL 错误彻底停止后，
        ``_thread_main`` 的空闲循环仍然 ``while not self._stopping`` 空转，
        界面永远显示"运行中"，而 ``_running`` 为 True 又让下次 ``start()``
        直接返回 False（用户只能先点"停止"再点"连接"）。
        """
        self._ws_ready = False
        if self._stopping or self._disconnecting:
            # 用户主动停止 / 正在清理，属于正常路径
            return
        exc: Optional[BaseException] = None
        if not task.cancelled():
            exc = task.exception()
        if exc is not None:
            logger.error("B 站连接中断: %s", exc)
            self._notify_error(f"B 站连接中断: {exc}")
        else:
            logger.warning("B 站连接已结束（服务端断开且未能重连）")
            self._notify_error("B 站连接已结束（服务端断开且未能自动重连）")
        # 置 _stopping 让基类的空闲循环退出，_running 才会被复位、允许重连。
        self._stopping = True

    async def _diagnose_ws_failure(self) -> str:
        """WebSocket 没就绪时，把原因翻成人话。

        先走**签名**接口：它成功说明服务器列表拿到了，问题在 WebSocket
        握手（本地网络 / 端口 / 认证），不再乱报"接口风控"。

        再走一次**未签名**接口只为拿错误码：B 站对无签名请求回 ``-352``，
        这时如实说明"签名后仍被拦 / 取密钥失败"，而不是误导用户去换房间号。
        换网络出口仍然是最后手段，但不再是第一嫌疑。

        必须带浏览器 ``User-Agent`` 与 ``Referer``：只用 aiohttp 默认头请求时
        B 站直接返回 **HTTP 412 + 反爬 HTML**（实测 ``Content-Type: text/html``），
        连 JSON 都拿不到，于是诊断会静默落到兜底文案。

        这一段只是**诊断**，不影响连接结果：任何异常都吞掉并返回兜底文案，
        绝不让"诊断"本身把连接流程弄挂。
        """
        fallback = "B 站连接未就绪（可能是网络问题或接口风控）"
        try:
            signed = await _fetch_signed_danmu_info(self._session, self._room_id)
        except Exception:  # pragma: no cover - 防御性兜底
            signed = None
        if signed and signed.get("host_list"):
            return "B 站弹幕服务器连接未就绪（服务器列表已取到，可能是本地网络屏蔽了 443 端口）"

        try:
            url = ("https://api.live.bilibili.com/xlive/web-room/v1/index/"
                   "getDanmuInfo?id=%s&type=0" % self._room_id)
            headers = {
                "User-Agent": _BROWSER_UA,
                "Referer": "https://live.bilibili.com/%s" % self._room_id,
                "Accept": "application/json, text/plain, */*",
            }
            async with self._session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                status = resp.status
                if status != 200:
                    logger.info("B 站诊断：HTTP %s（非 200，多半是反爬页）", status)
                    return fallback
                text = await resp.text()
        except Exception as exc:  # pragma: no cover - 网络异常分支
            logger.debug("B 站诊断请求失败: %s", exc)
            return fallback

        try:
            data = json.loads(text)
        except Exception:
            logger.info("B 站诊断：响应不是 JSON（前 60 字 %r）", text[:60])
            return fallback

        if not isinstance(data, dict):
            return fallback

        code = data.get("code")
        if code == -352:
            return (
                "B 站接口返回 -352（风控拦截）：请求已带 WBI 签名仍被拒，"
                "换房间号没有用，请换网络出口（家宽 / 手机热点）后重试"
            )
        if code not in (0, None):
            return "B 站接口返回 %s（%s），无法取得弹幕服务器地址" % (
                code, data.get("message") or data.get("msg") or "无说明")
        # 接口是好的，那就是 WebSocket 层面没握上手
        return "B 站弹幕服务器连接未就绪（接口正常，可能是本地网络屏蔽了 2243/443 端口）"

    async def _wait_ws_ready(self) -> bool:
        """等待 WebSocket 真正就绪。

        两条证据，任一成立即可：

        * ``fetcher._ws_ready`` —— :class:`_WsReadyHandler` 收到第一条命令后置位
          （命令能到说明认证已通过）；
        * ``client._websocket is not None`` —— blivedm 在 ``ws_connect`` 成功后
          立刻赋值的属性（``client.py:395``），比第一条命令更早，用于覆盖
          "连上了但服务端还没推消息"的空房间场景。

        用轮询而不是 Event，是为了不把 asyncio 原语当作跨版本/跨事件的共享状态；
        间隔 50ms，代价可以忽略，而且 ``connect_timeout`` 一到就返回 False。
        """
        deadline = self.connect_timeout
        step = 0.05
        waited = 0.0
        while waited < deadline:
            if self._ws_ready or self._websocket_connected():
                return True
            if self._join_task is not None and self._join_task.done():
                # 后台协程已经结束（例如 init_room 反复失败），再等也没意义
                return False
            await asyncio.sleep(step)
            waited += step
        return self._ws_ready or self._websocket_connected()

    def _websocket_connected(self) -> bool:
        """blivedm 的 ws 是否已握手成功（读它的私有属性，做能力探测）。"""
        client = self._client
        if client is None:
            return False
        try:
            return getattr(client, "_websocket", None) is not None
        except Exception:  # pragma: no cover - 防御
            return False

    @staticmethod
    def _register_handler(client: Any, handler: Any) -> bool:
        """把处理器注册到 blivedm 客户端。

        新版 blivedm 用 ``add_handler``，旧版用 ``set_handler``。
        参考项目硬编码调用 ``set_handler``，在 0.1.1 上直接 AttributeError。
        """
        for name in ("add_handler", "set_handler"):
            method = getattr(client, name, None)
            if callable(method):
                try:
                    method(handler)
                    logger.debug("已用 %s 注册处理器", name)
                    return True
                except Exception as exc:
                    logger.error("%s 注册失败: %s", name, exc)
        logger.error("客户端既无 add_handler 也无 set_handler")
        return False

    # ------------------------------------------------------------------
    # 断开
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        """清理连接。

        用 ``_disconnecting`` 防重入；**不**检查 ``_stopping``，
        否则 stop() 之后这个函数会直接返回，socket 永不关闭。
        """
        if self._disconnecting:
            return
        self._disconnecting = True
        try:
            if self._join_task is not None and not self._join_task.done():
                self._join_task.cancel()
                try:
                    await self._join_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._join_task = None

            client = self._client
            self._client = None
            if client is not None:
                stop_and_close = getattr(client, "stop_and_close", None)
                if callable(stop_and_close):
                    try:
                        await stop_and_close()
                    # blivedm 的 ``stop_and_close()`` 内部是
                    # ``stop()`` + ``await join()``，而 ``join()`` 是
                    # ``await asyncio.shield(self._network_future)``：
                    # 刚 cancel 过的 future 会让 shield 立刻抛 CancelledError。
                    # Python 3.8+ 的 CancelledError 继承 BaseException，
                    # 光写 ``except Exception`` 捕不到 —— 异常会从
                    # ``_thread_main`` 的 finally 逃出去，导致 ``loop.close()``
                    # 和 ``_running = False`` 都不执行（线程/会话泄漏、
                    # 之后再也无法重连）。所以必须显式列出来。
                    except (asyncio.CancelledError, Exception) as exc:
                        logger.debug("stop_and_close 出错: %s", exc)
                else:
                    stop = getattr(client, "stop", None)
                    if callable(stop):
                        try:
                            stop()
                        except (asyncio.CancelledError, Exception) as exc:
                            logger.debug("stop 出错: %s", exc)

            session = self._session
            self._session = None
            if session is not None and not session.closed:
                try:
                    await session.close()
                except Exception as exc:
                    logger.debug("关闭 session 出错: %s", exc)

            self._handler = None
            logger.info("B 站连接已清理")
        finally:
            self._disconnecting = False

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    # ``_emit_message`` 直接用基类实现（带 ``message is None`` 与 ``_stopping``
    # 双重检查）。这里原来覆写了一份，既重复又漏掉了 None 检查。


async def _probe_login_mid(session: Any) -> int:
    """用会话里的 SESSDATA 问一次 ``nav``：登录中返回 mid，否则返回 0。

    为什么要单独探测：B 站对**未登录**的弹幕客户端下发的 ``DANMU_MSG``
    里 ``uname`` 是服务端掩码（``伊***``）、``uid`` 是 ``0``，payload 里
    也没有反解需要的字段（``info[0][15]`` 的 extra 只有 ``user_hash``）。
    见 2026-10-23 实测：同一房间网页端显示全名，抓取器拿到的却是掩码。
    所以连接前必须先确认登录态，并把 ``mid`` 作为 ``uid`` 交给 blivedm
    的 WS 鉴权包；同时把"cookie 失效"明确告诉用户——他们几乎都以为
    自己"已经扫码登录过了"。
    """
    try:
        async with session.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"User-Agent": _BROWSER_UA, "Referer": "https://live.bilibili.com/"},
        ) as res:
            if res.status != 200:
                logger.info("B 站登录态探测 HTTP %s（按游客处理）", res.status)
                return 0
            data = await res.json(content_type=None)
    except Exception as exc:  # pragma: no cover - 网络异常分支
        logger.info("B 站登录态探测失败（按游客处理）: %s", exc)
        return 0
    code = (data or {}).get("code")
    if code != 0:
        logger.info("B 站登录态探测 code=%s（按游客处理）", code)
        return 0
    try:
        return int(((data or {}).get("data") or {}).get("mid") or 0)
    except (TypeError, ValueError):
        return 0


def _yarl_url(url: str) -> Any:
    """构造 aiohttp 需要的 URL 对象。

    aiohttp 3.x 的 ``cookie_jar.update_cookies`` 第二参数要求 ``yarl.URL``；
    某些版本也接受字符串。这里做一次兼容尝试。
    """
    try:  # pragma: no cover - 依赖环境
        from yarl import URL

        return URL(url)
    except Exception:  # pragma: no cover
        return url


__all__ = ["AIOHTTP_AVAILABLE", "BLIVEDM_AVAILABLE", "BilibiliFetcher"]
