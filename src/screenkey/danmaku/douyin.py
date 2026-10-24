# -*- coding: utf-8 -*-
"""抖音直播弹幕抓取器（真实 WebSocket + protobuf 实现）。

参考项目里的 ``core/danmaku/douyin.py`` **从来没有真正收到过抖音弹幕**：
它的 ``listen()`` 是一个 ``while running: await asyncio.sleep(0.1)`` 空转循环，
``_connect_direct()`` 只是 GET 一下 ``live.douyin.com/<房间号>`` 看返回 200，
然后就 ``_notify_connected()`` 让界面显示"已连接"，同时用
``supports_realtime_danmaku = False`` 在代码里承认了这件事。

本模块把它做成真的：走抖音自己的 IM 推送协议。

协议概览
--------
1. ``GET https://live.douyin.com/<web_rid>`` 拿 HTML，从里面抠出
   真实 ``room_id``、``user_unique_id``、初始 ``cursor`` 与 ``internal_ext``。
2. 需要 ``ttwid`` cookie。没有的话不报错：先 GET 一次 ``https://live.douyin.com/``
   暖场，服务端会在 ``Set-Cookie`` 里下发 ``ttwid``，合并进来再请求直播间页。
   （实测不带 ttwid 直接请求直播间页会被判定为爬虫并返回验证页，页面里搜不到
   ``roomId``；带 cookie 手动填也可以。）
3. 连 ``wss://<host>/webcast/im/push/v2/?...``，参数里带上 room_id /
   user_unique_id / device_id / cursor / internal_ext 等。
4. 服务端推来的每一帧都是 ``PushFrame``：
   ``payloadEncoding="gzip"``、``payloadType="msg"``、``payload`` 是 gzip 后的
   ``Response``。解压后 ``Response.messagesList`` 里是一堆 ``Message``，
   每个 ``Message`` 的 ``method`` 形如 ``WebcastChatMessage``，
   ``payload`` 是具体消息体。
5. 收到消息后要回 ack（把 ``internal_ext`` 塞进 ``PushFrame.payloadType="ack"``），
   否则服务端会认为客户端跟不上而断开。
6. 定期发心跳 ``b':\\x02hb'``（就是 ``PushFrame.payloadType="hb"``）。

为什么手写 protobuf 解析
------------------------
抖音的字段号会随改版漂移。用 ``protobuf`` 编译器意味着字段对不上就直接抛异常；
:mod:`screenkey.danmaku.protobuf_lite` 手写解析对未知字段号**直接跳过**，
并且本模块对关键字段采用"多候选字段号 + 逐个试"的策略，
所以平台小改版不会让整个功能失效。

关于 ``signature``（请读这段）
-----------------------------
新版抖音在 WS URL 上要求一个 ``signature`` 参数，它由 ``webmssdk.js`` 里
被混淆的 JS 现场计算，纯 Python 无法复现。因此本抓取器的策略是：

- 默认**不带 signature** 连接，并把真实结果如实报给界面（成功就是成功，
  失败就报出服务端返回的原始原因）；
- 允许通过 ``signature=`` 传入你自己抓到的签名（一次可用一段时间）；
- 允许通过 ``signature_provider=`` 传入一个回调，由外部（比如本地 Node
  跑 JS）实时算签名。

**不会**在没有签名的情况下假装连上。这是与参考项目最本质的区别。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from ..login.douyin import sanitize_cookie_header
from ..utils.helpers import clean_text, extract_douyin_room_id
from ..utils.logging_setup import get_logger, get_raw_danmaku_logger
from .base import DanmakuFetcher
from .message import DanmakuMessage, MessageType
from .protobuf_lite import (
    ProtobufError,
    encode_field_string,
    parse_message,
)
from .signature import (
    SIGN_MD5_FIELDS,
    SignatureEngine,
    availability_report,
    default_engine,
    signature_available,
)

logger = get_logger("danmaku.douyin")
raw_logger = get_raw_danmaku_logger()

try:  # pragma: no cover - 取决于环境
    import websockets

    WEBSOCKETS_AVAILABLE = True
except Exception:  # pragma: no cover
    websockets = None  # type: ignore[assignment]
    WEBSOCKETS_AVAILABLE = False

try:  # pragma: no cover
    import aiohttp

    AIOHTTP_AVAILABLE = True
except Exception:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False


# ----------------------------------------------------------------------
# 协议常量
# ----------------------------------------------------------------------

#: 抖音 IM 推送的 WebSocket 主机（服务端会下发可用的 push_server，这里是兜底）
#:
#: 实测可用的是 ``webcast100-ws-web-lq``；``webcast5-ws-web-lf`` 也能握手，
#: 但用它时更容易被立即断开，所以默认用前者。
DEFAULT_WS_HOST = "wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/"

#: 浏览器 UA（用于抓 HTML；必须是像样的桌面 Chrome，否则拿不到 INIT_PROPS）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 心跳帧：PushFrame.payloadType = "hb"
HEARTBEAT_FRAME = encode_field_string(7, "hb")

#: 服务端要求 ack 时最多重试几次解析
MAX_PARSE_ERRORS = 20

#: 收包循环单轮 ``recv()`` 的最长等待（秒）。
#:
#: 这个值**不承担心跳节奏**，只决定两件事：多快能发现"该发心跳了"、多快能
#: 响应 ``stop()``。心跳节奏由 ``_heartbeat_due()`` 按单调时钟单独判断，所以
#: 这里可以取得很短。
RECV_POLL_INTERVAL = 1.0

#: Set-Cookie 的属性名，不是 cookie 本身 —— 解析 Cookie 串时必须跳过，
#: 否则 ``Domain`` / ``Path`` / ``expires`` 会被当成 cookie 一起发回去。
_COOKIE_ATTRIBUTES = frozenset(
    {
        "domain",
        "path",
        "expires",
        "max-age",
        "samesite",
        "secure",
        "httponly",
        "version",
        "comment",
    }
)

# ----------------------------------------------------------------------
# 协议层（字段号表、帧解析、HTML 提取）在 douyin_protocol.py
# ----------------------------------------------------------------------
from .douyin_protocol import (  # noqa: F401, I001
    CHAT_CONTENT,
    CHAT_USER,
    CONTROL_STATUS,
    GIFT_COUNT,
    GIFT_GIFT,
    GIFT_STRUCT_NAME,
    GIFT_USER,
    HEADER_INTERNAL_EXT,
    IGNORED_METHODS,
    LIKE_COUNT,
    LIKE_TOTAL,
    LIKE_USER,
    MEMBER_USER,
    METHOD_MAP,
    MSG_METHOD,
    MSG_PAYLOAD,
    SEQ_TOTAL,
    SOCIAL_ACTION,
    SOCIAL_USER,
    USER_ID,
    USER_LEVEL,
    USER_NICKNAME,
    _pick,
    _pick_int,
    _pick_message,
    _pick_str,
    build_ack,
    decode_payload,
    decode_payload_ex,
    extract_room_info,
    parse_push_frame,
    parse_response,
)
#: 对外再导出：测试与外部脚本一直按 ``danmaku.douyin.<名字>`` 取这些符号
#: （``tests/test_douyin_protocol.py`` 就从这里 import，别当未用清理掉）
from .douyin_protocol import (
    HEADER_COMPRESS_TYPE,
    MSG_MSG_ID,
    parse_headers_list,
)

# ----------------------------------------------------------------------
# 抓取器
# ----------------------------------------------------------------------


class CookieHeaderError(RuntimeError):
    """Cookie 里有 HTTP 头放不下的字符，连接必然失败 —— 不值得重试。

    真实故障（2026-10-22）：用户粘的 cookie 中间被换成了 ``…``，
    ``websockets`` 在构造握手时抛 ``InvalidHeaderValue``，``str()`` 是
    ``invalid Cookie header: ttwid=1%7C…``。旧代码把它当普通网络异常重试
    5 次，用户看到的是同一句话刷 5 遍，完全不知道要重新复制 cookie。
    """


#: ``websockets`` 校验请求头时可能抛的异常名（各版本类名不同，按名字判）
_HEADER_ERROR_NAMES = frozenset({"InvalidHeader", "InvalidHeaderValue", "InvalidHeaderFormat"})


def _looks_like_header_error(exc: BaseException) -> bool:
    """异常是不是"请求头里有非法字符"（而不是网络/风控问题）。"""

    if exc.__class__.__name__ in _HEADER_ERROR_NAMES:
        return True
    text = str(exc).lower()
    return "header" in text and "invalid" in text


def _header_error_text(exc: BaseException) -> str:
    """把 ``invalid Cookie header: ttwid=1%7C…`` 换成人话（长值要截断）。"""

    raw = " ".join(str(exc).split())
    if len(raw) > 200:
        raw = raw[:200] + "…（原始报错里还有一长串 cookie，已省略）"
    return (
        "Cookie 里有 HTTP 头放不下的字符，握手在本地就被拒绝了 —— 不是网络问题、"
        "也不是房间号问题，重试不会好。请到抖音 Cookie 输入框里重新完整粘贴一次"
        "（不要复制带「…」的省略文本）。原始报错：" + raw
    )


class DouyinFetcher(DanmakuFetcher):
    """抖音直播弹幕抓取器。

    :param cookie: 浏览器里复制出来的 Cookie 字符串（可选，但强烈建议）
    :param mode: ``"websocket"``（真实协议）或 ``"browser"``（同样走协议，
        只是明确表示"这是给浏览器登录态用的"；不再假装开浏览器）
    :param signature: 自己抓到的 ``signature`` 参数（可选）
    :param signature_provider: ``() -> str`` 回调，实时算签名（可选）
    :param proxy: 代理地址（可选）
    """

    supports_realtime = True
    platform = "douyin"

    def __init__(
        self,
        cookie: str = "",
        mode: str = "websocket",
        signature: str = "",
        signature_provider: Optional[Callable[[], str]] = None,
        proxy: str = "",
        device_id: str = "",
    ) -> None:
        super().__init__()
        self.cookie = cookie or ""
        self.mode = (mode or "websocket").strip().lower()
        self.signature = signature or ""
        self.signature_provider = signature_provider
        self.proxy = proxy or ""
        self.device_id = device_id or self._make_device_id()

        self.web_rid = ""
        self._douyin_room_id = ""
        self.user_unique_id = ""
        self.cursor = ""
        self.internal_ext = ""
        self.push_server = ""

        self._session: Optional[Any] = None
        self._ws: Optional[Any] = None
        self._heartbeat_interval = 10.0
        self._last_heartbeat = 0.0
        self._parse_errors = 0
        self._got_any_message = False
        self._pending_ack: Optional[bytes] = None
        self._signature_engine: Optional[SignatureEngine] = None
        self._last_signature_error = ""
        self._signature_reported = False
        #: 最近一次连接时对 cookie 做了哪些修复（给界面提示用，空表示没问题）
        self._cookie_notes: List[str] = []

    @property
    def room_id(self) -> str:
        """抖音真实 ``room_id``（数字串）。

        基类的 ``room_id`` 是只读 property，所以这里必须覆盖而不是赋值。
        """
        return self._douyin_room_id

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _make_device_id() -> str:
        """生成一个像样的 device_id（19 位数字）。"""
        return "".join(random.choice("0123456789") for _ in range(19))

    def _cookie_header(self) -> str:
        """``self.cookie`` 清洗后的版本（可直接放进 ``Cookie`` 头）。

        ``self.cookie`` 会在 ``_absorb_cookies()`` 里被重新拼装，所以这里每次
        都重新清洗，而不是在 ``__init__`` 里算一次缓存起来。

        不做清洗的后果（实测）：用户在浏览器里复制了一份 1024 字符的 cookie，
        中间第 512 个字符是 ``…``。``aiohttp`` 与 ``websockets`` 都按
        HTTP 头规则校验值，直接抛 ``invalid Cookie header: <整串>``，
        整串 cookie（含 ttwid）被糊在错误提示里，用户只看到"链接不上"。
        """

        return sanitize_cookie_header(self.cookie or "").value

    def _headers(self, referer: str = "https://live.douyin.com/") -> Dict[str, str]:
        """构造浏览器风格请求头。"""
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            # 必须显式声明。aiohttp 在检测到 brotli 可用时会自动带上 ``br``，
            # 但很多环境里装的是 ``brotli`` 的 shim（不是 brotlicffi），
            # 解码时会抛 ``ClientPayloadError: 400, message: Can not decode
            # content-encoding: br`` —— 整个页面直接取不回来。
            # 只声明 gzip/deflate 就能稳定拿到 HTML。
            "Accept-Encoding": "gzip, deflate",
            "Referer": referer,
            "Upgrade-Insecure-Requests": "1",
        }
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _ensure_ttwid(self) -> str:
        """从 Cookie 里取 ttwid；没有就返回空串（不阻塞连接尝试）。

        取值后**校验格式**：早期版本这里只做 ``re.search(r"ttwid=([^;\\s]+)")``，
        一旦 cookie 串被污染成 ``ttwid=Set-Cookie: ttwid=1%7C...`` 就会匹配到
        字面量 ``Set-Cookie:`` 并误判为"已有 ttwid"，于是跳过暖场、带着畸形
        Cookie 去握手，服务端直接以 ``HTTP 200`` 拒绝升级（InvalidStatus）。
        """
        for match in re.finditer(r"(?:^|;\s*)ttwid=([^;]+)", self._cookie_header()):
            value = match.group(1).strip()
            # 真实 ttwid 形如 "1%7C<base64url>%7C<ts>%7C<md5>"，不含空格/冒号
            if value and ":" not in value and " " not in value:
                return value
        return ""

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    async def connect(self, room: str, **kwargs: Any) -> bool:
        """连接抖音直播间。

        返回值语义与基类契约一致：**``True`` 表示已经连上**，基类随后进入
        保活循环（``while not self._stopping: sleep(0.1)``）。所以这里绝不能
        在重试耗尽后仍然 ``return True`` —— 那会让基类以为连接正常、线程永不
        退出、``_running`` 永远为 ``True``，用户下次点"连接"只会收到
        "启动弹幕接收线程失败"，必须先点"停止"才能恢复。
        """
        if not WEBSOCKETS_AVAILABLE:
            self._notify_error("缺少 websockets 依赖，无法接收抖音弹幕：pip install websockets")
            return False
        if not AIOHTTP_AVAILABLE:
            self._notify_error("缺少 aiohttp 依赖，无法访问抖音页面：pip install aiohttp")
            return False

        # 基类 start() 已经复位过 _stopping；这里再写一次会**吞掉**用户在
        # 启动间隙按下的"停止"（stop() 置 True 之后被这里覆盖成 False，
        # 停止请求永久丢失，连接会一直保持到下次 stop()）。
        if self._stopping:
            return False

        # Cookie 必须在连接前修一遍，并把修了什么告诉界面。
        # 带「…」这类字符时握手必然失败（websockets 直接抛 InvalidHeaderValue），
        # 而重试一万次也不会变好 —— 字符串本身是坏的。旧代码不修也不说，
        # 用户只看到 "invalid Cookie header: ttwid=1%7C…" 刷屏。
        sanitized = sanitize_cookie_header(self.cookie or "")
        self._cookie_notes = list(sanitized.notes)
        if sanitized.value != (self.cookie or ""):
            self.cookie = sanitized.value
        if self._cookie_notes:
            for note in self._cookie_notes:
                logger.warning("Cookie 自动修复: %s", note)
            self._notify_status(
                False, "抖音 Cookie 有问题，已自动修复：" + "；".join(self._cookie_notes)
            )

        web_rid = extract_douyin_room_id(room) or ""
        if not web_rid:
            # 短链（v.douyin.com/xxxx）里没有房间号，必须跟一次跳转才知道落在哪个直播间。
            # 以前这里退化成 str(room)，于是 url 被拼成
            # https://live.douyin.com/https://www.douyin.com/root/live/123
            # ——页面直接 404，用户只看到「访问抖音直播间失败: HTTP 404」。
            web_rid = await self._resolve_room_link(room)
        if not web_rid:
            self._notify_error(
                "无法从输入里识别抖音直播间号。请填 https://live.douyin.com/<房间号>、"
                "https://www.douyin.com/root/live/<房间号> 这种完整链接，或直接填房间号。"
            )
            return False

        self.web_rid = web_rid
        self._got_any_message = False
        self._parse_errors = 0
        self._signature_reported = False

        try:
            info = await self._fetch_room_info(web_rid)
        except Exception as exc:
            self._notify_error(f"访问抖音直播间失败: {exc}")
            return False

        self._douyin_room_id = info.get("room_id") or ""
        self.user_unique_id = info.get("user_unique_id") or ""
        self.cursor = info.get("cursor") or ""
        self.internal_ext = info.get("internal_ext") or ""

        if not self._douyin_room_id:
            self._notify_error(
                "没能从页面里解析出 room_id —— 通常是抖音返回了反爬验证页。"
                "请填入浏览器里复制出来的完整 Cookie（需要包含 ttwid）后重试。"
            )
            return False

        if not self.user_unique_id:
            self.user_unique_id = self.device_id

        # 基类的 room_id property 读的就是 _room_id
        self._room_id = str(self._douyin_room_id)
        logger.info(
            "抖音房间信息: web_rid=%s room_id=%s user_unique_id=%s cursor=%s",
            self.web_rid,
            self._douyin_room_id,
            self.user_unique_id,
            (self.cursor or "")[:40],
        )

        return await self._run_connection_loop()

    async def _resolve_room_link(self, room: str) -> str:
        """把 ``v.douyin.com/xxxx`` 这类短链跟到最终直播间页，再提取房间号。

        解析失败不抛异常：返回空串，由调用方给出可读提示。
        只在 ``extract_douyin_room_id`` 已经失败时才走这里，正常链接不会多一次请求。
        """
        text = str(room or "").strip()
        if not text.lower().startswith(("http://", "https://")):
            return ""
        if "douyin.com" not in text.lower():
            return ""
        if not AIOHTTP_AVAILABLE:
            return ""
        timeout = aiohttp.ClientTimeout(total=min(15.0, self._connection_timeout()))
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, headers=self._headers()
            ) as session:
                async with session.get(text, allow_redirects=True) as response:
                    await response.read()
                    final_url = str(response.url)
                    self._absorb_cookies(response)
        except Exception as exc:  # noqa: BLE001 - 短链解析失败不该中断连接流程
            logger.debug("解析抖音短链失败: %s", exc)
            return ""
        web_rid = extract_douyin_room_id(final_url) or ""
        if web_rid:
            logger.info("抖音短链已解析: %s -> %s", text, final_url)
        return web_rid

    async def _fetch_room_info(self, web_rid: str) -> Dict[str, Any]:
        """GET 直播间页面并提取连接信息。

        **必须先访问一次首页拿 ``ttwid``**：实测直接请求直播间页会被判定为
        爬虫并返回验证页（页面里搜不到 roomId）。带上 ttwid 后同样的请求就能
        稳定拿到含 roomId 的正常页面。
        """
        timeout = aiohttp.ClientTimeout(total=self._connection_timeout())
        # proxy 必须同时作用于 HTTP 抓取：用户配代理的动机通常正是"本机 IP
        # 被抖音风控"，而拿 room_id 这一步恰恰是最容易被反爬拦的。以前只有
        # websockets 走了代理，抓页面仍是直连，等于白配。
        session_kwargs: Dict[str, Any] = {"timeout": timeout, "headers": self._headers()}
        if self.proxy:
            session_kwargs["proxy"] = self.proxy
        try:
            self._session = aiohttp.ClientSession(**session_kwargs)
        except TypeError:
            # aiohttp < 3.9 不支持会话级 proxy
            session_kwargs.pop("proxy", None)
            self._session = aiohttp.ClientSession(**session_kwargs)

        # 第一步：暖场，拿 ttwid（已有就跳过，省一次请求）
        if not self._ensure_ttwid():
            try:
                async with self._session.get(
                    "https://live.douyin.com/", allow_redirects=True
                ) as home:
                    await home.read()
                    logger.debug("抖音首页 HTTP %s", home.status)
                    self._absorb_cookies(home)
            except Exception as exc:
                logger.debug("访问抖音首页失败（继续尝试直播间页）: %s", exc)
            if self._ensure_ttwid():
                logger.info("已从首页取得 ttwid")
            else:
                logger.warning("未能取得 ttwid，直播间页可能返回验证页")

        # 第二步：请求直播间页
        url = f"https://live.douyin.com/{web_rid}"
        # Cookie 变了要同步到 session 头，否则 ttwid 不会被带上
        self._session.headers.update(self._headers(referer="https://live.douyin.com/"))
        async with self._session.get(url, allow_redirects=True) as response:
            html = await response.text(errors="replace")
            logger.debug("直播间页面 HTTP %s，长度 %d", response.status, len(html))
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            self._absorb_cookies(response)

        info = extract_room_info(html)
        if not info.get("room_id"):
            if "验证" in html or "verify" in html.lower() or "__ac_nonce" in html:
                logger.warning("抖音返回了反爬验证页，未能拿到 room_id")
        return info

    def _absorb_cookies(self, response: Any) -> None:
        """把响应里新下发的 cookie 合并进 ``self.cookie``。

        两个真实坑：
        1. ``key not in self.cookie`` 是**子串匹配**，"ttwid" 恰好出现在别的
           cookie 名里就会漏掉 —— 必须解析成字典按键比较。
        2. aiohttp 的 ``response.cookies`` 迭代出来的是 ``Morsel``，而
           ``str(morsel)`` 是**整段 ``Set-Cookie`` 文本**（``ttwid=Set-Cookie:
           ttwid=1%7C...; Domain=...``）。早期版本直接 f-string 拼接，于是
           Cookie 头被污染成畸形串，服务端拒绝升级。必须取 ``morsel.value``。
        """
        try:
            jar = getattr(response, "cookies", None) or {}
            existing = self._cookie_map()
            added: List[str] = []
            for key, value in jar.items():
                if not key:
                    continue
                # Morsel -> 只取 value；普通 str 直接用
                value = getattr(value, "value", value)
                if value is None:
                    continue
                value = str(value).strip()
                # 防御：万一还是被污染成 Set-Cookie 文本，丢弃而不是写进去
                if not value or value.startswith("Set-Cookie") or ";" in value:
                    logger.debug("丢弃可疑 cookie 值: %s=%s", key, value[:40])
                    continue
                if existing.get(key) == value:
                    continue
                existing[key] = value
                added.append(key)
            if added:
                self.cookie = "; ".join(f"{k}={v}" for k, v in existing.items())
                logger.debug("合并了 %d 个 cookie: %s", len(added), added)
        except Exception as exc:  # pragma: no cover - 防御
            logger.debug("合并 cookie 失败: %s", exc)

    def _cookie_map(self) -> Dict[str, str]:
        """把 ``self.cookie`` 解析成字典（容忍空格与重复）。"""
        result: Dict[str, str] = {}
        for part in (self.cookie or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            # 跳过 Set-Cookie 的属性名（Domain/Path/expires/...），它们不是 cookie
            if key.lower() in _COOKIE_ATTRIBUTES:
                continue
            result[key] = value
        return result

    async def _run_connection_loop(self) -> bool:
        """连接 + 断线重连主循环。

        返回值直接交给基类判断"是否算连上"：

        - ``True``：至少成功连上过一次（或用户主动停止），基类进入保活循环；
        - ``False``：重试耗尽仍未连上，基类据此打印"连接未成功建立"并退出线程。

        以前这个方法是 ``-> None`` 且 ``connect()`` 在它之后无条件
        ``return True``，于是重试耗尽后基类仍以为连上了，线程在保活循环里
        永久空转、``_running`` 一直是 ``True``，用户下次点"连接"只会得到
        "启动弹幕接收线程失败"，必须先点"停止"才能恢复。
        """
        attempt = 0
        max_attempts = max(1, int(self.config_max_attempts()))
        connected_once = False
        while not self._stopping:
            try:
                await self._connect_once()
                # _connect_once 正常返回说明连接被正常关闭（通常是我们自己
                # 在 _stopping 后关的），不当作失败。
                connected_once = True
                attempt = 0
            except asyncio.CancelledError:
                raise
            except CookieHeaderError as exc:
                # Cookie 字符串本身有问题：重试一万次也是同一句报错，直接报给用户。
                logger.error("抖音 Cookie 不能用于握手: %s", exc)
                self._notify_error(str(exc))
                return False
            except Exception as exc:
                # 用户刚按了"停止"：这次断开是我们自己关的，不是掉线。
                # 旧行为会先闪一句"连接中断，正在重试(1/5)"再停，
                # 用户看到的却是"连不上、一直重试"，很难分清是自己停的。
                if self._stopping:
                    return connected_once
                attempt += 1
                text = str(exc) or exc.__class__.__name__
                logger.warning("抖音连接中断(%d/%d): %s", attempt, max_attempts, text)
                self._notify_status(False, f"连接中断，正在重试({attempt}/{max_attempts})：{text}")
                if attempt >= max_attempts:
                    self._notify_error(f"抖音连接失败，已重试 {attempt} 次：{text}")
                    return connected_once
                await asyncio.sleep(self._reconnect_delay(attempt))
        # 走到这里说明是 _stopping 让循环退出：用户主动停止，算正常结束。
        return True

    def config_max_attempts(self) -> int:
        """重连次数上限。

        以前硬编码 5，``ConnectionConfig.max_reconnect_attempts`` 改了也
        不生效。现在优先用注入的连接配置；``auto_reconnect=False`` 时直接
        返回 1（只连一次，不重试）。
        """
        if not bool(self._connection_option("auto_reconnect", True)):
            return 1
        raw = self._connection_option("max_reconnect_attempts", 5)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 5

    def _reconnect_delay(self, attempt: int) -> float:
        """第 ``attempt`` 次重连前的等待秒数。"""
        raw = self._connection_option("reconnect_delay", 2.0)
        try:
            base = max(0.0, float(raw))
        except (TypeError, ValueError):
            base = 2.0
        if base <= 0.0:
            return 0.0
        return min(base * attempt, 10.0)

    def _connection_timeout(self) -> float:
        """建立连接的超时秒数。"""
        raw = self._connection_option("connection_timeout", 15.0)
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            return 15.0

    async def _connect_once(self) -> None:
        """建立一次 WebSocket 连接并收包，直到断开。"""
        # 心跳间隔可配置（默认沿用服务端下发值）
        configured_heartbeat = self._connection_option("heartbeat_interval", 0)
        try:
            if configured_heartbeat and float(configured_heartbeat) > 0:
                self._heartbeat_interval = max(1.0, float(configured_heartbeat))
        except (TypeError, ValueError):
            pass

        url = self._build_ws_url()
        logger.info("连接抖音 WebSocket: %s", url.split("?")[0] + "?…")

        # 签名算不出来时，**在连接前**就把真实原因送到界面。
        # 以前 ``_last_signature_error`` 只写不读，用户只看到 1006 断开和
        # 一条"连接中断"的猜测文案，根本不知道是缺 Node / 缺 sign.js。
        if self._last_signature_error and not self._signature_reported:
            self._signature_reported = True
            self._notify_error(self._last_signature_error)

        connect_kwargs: Dict[str, Any] = {
            "max_size": 8 * 1024 * 1024,
            "open_timeout": self._connection_timeout(),
            "ping_interval": None,  # 用自己的协议级心跳
        }
        if self.proxy:
            connect_kwargs["proxy"] = self.proxy

        # websockets 14+ 把 extra_headers 改名为 additional_headers
        headers = self._headers()
        try:
            ws_ctx = websockets.connect(url, additional_headers=headers, **connect_kwargs)
        except TypeError:
            ws_ctx = websockets.connect(url, extra_headers=headers, **connect_kwargs)

        try:
            async with ws_ctx as ws:
                await self._pump(ws)
        except CookieHeaderError:
            raise
        except Exception as exc:
            # websockets 在构造握手时校验头值，非法字符抛 InvalidHeaderValue，
            # 文案是 "invalid Cookie header: <整串>"。这既不是网络问题也不会
            # 自愈，必须换成用户能照做的说明，并让外层停止重试。
            if _looks_like_header_error(exc):
                raise CookieHeaderError(_header_error_text(exc)) from exc
            raise

    async def _pump(self, ws: Any) -> None:
        """把一条已建立的连接收包到断开为止。"""

        self._ws = ws
        try:
            logger.info("抖音 WebSocket 已建立，等待弹幕推送…")
            self._notify_status(True, "已连接抖音直播间（真实 WebSocket）")
            self._last_heartbeat = time.monotonic()

            # 连接建立后先发一次心跳，让服务端开始推流
            try:
                await ws.send(HEARTBEAT_FRAME)
            except Exception:  # pragma: no cover
                pass

            await self._receive_loop(ws)
        finally:
            # 必须放在 finally：收包抛异常时也要清掉引用，否则重连期间
            # self._ws 指向已关闭的连接，disconnect() 会对死对象 close()，
            # 把真实状态掩盖成"正常关闭"。
            self._ws = None

    def _default_internal_ext(self) -> str:
        """构造 ``internal_ext``。

        直播间页面里**没有**这个串（实测搜 ``wss_push_room_id`` / ``wrds_v``
        均 0 命中），它由客户端 SDK 自己拼。协议里除 ``wss_push_room_id`` 与
        ``wss_push_did`` 外，其余时间戳/版本号都是客户端自报值，服务端不校验
        新鲜度 —— 用固定模板即可，**但绝不能留空**：空 internal_ext 实测会被
        服务端在约 21 秒后以 1006 断开。
        """
        return (
            "internal_src:dim"
            f"|wss_push_room_id:{self.room_id}"
            f"|wss_push_did:{self.device_id}"
            "|first_req_ms:1721106114541"
            "|fetch_time:1721106114633"
            "|seq:1"
            "|wss_info:0-1721106114633-0-0"
            "|wrds_v:7392094459690748497"
        )

    def _build_ws_url(self) -> str:
        """拼出 WS URL。

        参数表刻意与浏览器实际发出的那一份**逐字段对齐**（多传/少传都可能被
        服务端拒绝），所以不要随手加"看起来有用"的参数。
        """
        host = self.push_server or DEFAULT_WS_HOST
        if host.startswith("http://"):
            host = "ws://" + host[len("http://"):]
        elif host.startswith("https://"):
            host = "wss://" + host[len("https://"):]
        if not host.startswith(("ws://", "wss://")):
            host = "wss://" + host
        # push_server 可能自带 query（``.../push/v2/?x=1``）。直接拼
        # ``endswith("/webcast/im/push/v2/")`` 会判失败，拼出
        # ``.../push/v2/?x=1/webcast/im/push/v2/`` 这种畸形 URL。
        host_path, _, host_query = host.partition("?")
        if not host_path.endswith("/webcast/im/push/v2/"):
            host_path = host_path.rstrip("/") + "/webcast/im/push/v2/"
        host = host_path + (("?" + host_query) if host_query else "")

        params: Dict[str, str] = {
            "app_name": "douyin_web",
            "version_code": "180800",
            "webcast_sdk_version": "1.0.14-beta.0",
            "update_version_code": "1.0.14-beta.0",
            "compress": "gzip",
            "device_platform": "web",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Mozilla",
            "browser_version": (
                "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "browser_online": "true",
            "tz_name": "Asia/Shanghai",
            "cursor": self.cursor or "d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1",
            "internal_ext": self.internal_ext or self._default_internal_ext(),
            "host": "https://live.douyin.com",
            "aid": "6383",
            "live_id": "1",
            "did_rule": "3",
            "endpoint": "live_pc",
            "support_wrds": "1",
            "user_unique_id": self.user_unique_id,
            "im_path": "/webcast/im/fetch/",
            "identity": "audience",
            "need_persist_msg_count": "15",
            "insert_task_id": "",
            "live_reason": "",
            "room_id": self.room_id,
            "heartbeatDuration": "0",
            # 下面 4 个不传值也必须**出现在 URL 里**：它们是签名 MD5 的 13 个
            # 字段之一，服务端会用同一套规则复算，缺字段会导致签名对不上。
            "sub_room_id": "",
            "sub_channel_id": "",
            "device_type": "",
            "ac": "",
        }
        # 补上 signature 算法要求、但上面没列全的字段（缺失必须按空串参与 MD5，
        # 所以这里显式给默认值，避免 params.get 与协议顺序对不上）
        for name in SIGN_MD5_FIELDS:
            params.setdefault(name, "")

        # 签名必须用**这份 params 自身**算出来：get_sign(md5(13 字段逗号串))。
        # 用外部传入的静态 signature 时直接采用（用户自己抓的、或短期复用）。
        signature = self.signature
        if not signature and self.signature_provider is not None:
            try:
                # provider 可能自己持有一份 params（调用的是**它那份**而不是这里
                # 这份）。不回填的话会拿空/陈旧的 room_id 算签名，服务端直接
                # 1006，而日志里只有"连接中断"。所以先喂 update() 再签。
                updater = getattr(self.signature_provider, "update", None)
                if callable(updater):
                    updater(params)
                produced = self.signature_provider()
                if produced:
                    signature = produced
            except Exception as exc:
                logger.warning("signature_provider 调用失败: %s", exc)
        if not signature:
            # 没有外部签名时，尝试本地 Node 桥自动计算。
            # 这一步失败不会让整个连接失败 —— 会如实退化成"不带签名去连"，
            # 由服务端决定是否断开（实测 155ms 内 1006）。
            signature = self._local_signature(params)
        if signature:
            params["signature"] = signature
            self._last_signature_error = ""
        else:
            self._last_signature_error = (
                "未能计算 signature："
                + (availability_report().get("reason") or "本地签名引擎不可用")
            )

        return host + "?" + urlencode(params, quote_via=quote)

    def _local_signature(self, params: Dict[str, str]) -> str:
        """用本地 Node 桥算 ``signature``；不可用时返回空串。

        结果按 ``md5`` 缓存，重连时不必重算。
        """
        engine = self._signature_engine
        if engine is None:
            if not signature_available():
                return ""
            engine = default_engine()
            self._signature_engine = engine
        if not engine.available():
            return ""
        try:
            return engine.signature_for(params)
        except Exception as exc:  # pragma: no cover - 引擎内部已兜底
            logger.warning("本地签名计算失败: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # 收包
    # ------------------------------------------------------------------

    async def _receive_loop(self, ws: Any) -> None:
        """收包循环。

        心跳**不能**挂在 ``recv()`` 超时上：热门房间服务端持续推帧，``recv()``
        永远不会超时，于是整场一次心跳都不发（``_last_heartbeat`` 只写不读是
        死变量）。这里把 ``recv()`` 的超时压成固定的 :data:`RECV_POLL_INTERVAL`，
        每轮按 ``time.monotonic()`` 判断心跳是否到期 —— 既能按时保活，也让
        ``stop()`` 最多等一个短周期就能打断 ``recv()``（原来要等满心跳间隔，
        上限 60 秒，用户点"停止"后 3 秒内重连必然失败）。
        """
        while not self._stopping:
            # 每轮都按单调时钟检查心跳是否到期 —— 热门房间 recv() 一直有数据，
            # 只在超时分支发心跳会导致整场不发 hb。
            if self._heartbeat_due():
                await self._send_heartbeat(ws)
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=RECV_POLL_INTERVAL)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping:
                    return
                raise RuntimeError(self._explain_close(exc)) from exc

            if not isinstance(frame, (bytes, bytearray)):
                # 文本帧（一般是错误信息）
                text = str(frame)[:500]
                logger.debug("收到文本帧: %s", text)
                if not self._got_any_message and ("error" in text.lower() or "sign" in text.lower()):
                    raise RuntimeError(f"服务端拒绝连接: {text}")
                continue

            self._handle_frame_sync(bytes(frame))
            await self._ack(ws)

    def _heartbeat_due(self) -> bool:
        """心跳是否到期（按单调时钟，与收包频率无关）。"""
        interval = max(1.0, float(self._heartbeat_interval or 10.0))
        return (time.monotonic() - self._last_heartbeat) >= interval


    def _handle_frame_sync(self, data: bytes) -> None:
        """处理一个 PushFrame（同步部分：解析 + 分派）。

        ack 需要 ``await``，所以真正的发送在 :meth:`_ack` 里；
        这里把待 ack 的信息记下来，由调用方在事件循环里发出去。
        """
        self._pending_ack = None
        frame = parse_push_frame(data)
        if frame is None:
            self._parse_errors += 1
            if self._parse_errors >= MAX_PARSE_ERRORS:
                logger.warning("PushFrame 连续解析失败 %d 次，放弃当前连接", self._parse_errors)
                raise RuntimeError("PushFrame 连续解析失败")
            return
        self._parse_errors = 0

        frame_type = (frame.get("type") or "").lower()
        if frame_type in ("ack", "hb"):
            return
        if frame_type and frame_type != "msg":
            logger.debug("忽略未知 PushFrame 类型: %s", frame_type)
            return

        headers = frame.get("headers") or {}
        payload, decompressed_ok = decode_payload_ex(
            frame.get("payload") or b"",
            frame.get("encoding") or "",
            headers,
        )
        if not decompressed_ok:
            # 声明是 gzip 却解不开：这是最常见的"连上了但永远 0 条弹幕"的原因，
            # 必须计入连续失败计数，否则连接会一直挂着不重连。
            self._parse_errors += 1
            if self._parse_errors >= MAX_PARSE_ERRORS:
                logger.warning(
                    "连续 %d 帧解压失败，放弃当前连接", self._parse_errors
                )
                raise RuntimeError("连续解压失败")
            return
        if not payload:
            return

        response = parse_response(payload)

        if response.get("cursor"):
            self.cursor = response["cursor"]
        if response.get("internal_ext"):
            self.internal_ext = response["internal_ext"]
        if response.get("push_server"):
            self.push_server = response["push_server"]

        heartbeat = response.get("heartbeat") or 0
        if heartbeat:
            # 服务端可能给毫秒也可能给秒，>1000 视作毫秒
            seconds = heartbeat / 1000.0 if heartbeat > 1000 else float(heartbeat)
            self._heartbeat_interval = max(3.0, min(60.0, seconds))

        # 记下待回的 ack（真正的 await ws.send 在 _ack 里）。
        # internal_ext 优先取 Response 的，其次取 headersList 的
        # ``im-internal_ext`` —— 实测两种都出现过，只认一个就会漏 ack。
        ack_ext = response.get("internal_ext") or headers.get(HEADER_INTERNAL_EXT) or ""
        if response.get("need_ack") or ack_ext:
            # ack_ext 为空时 build_ack 会编出**没有 payload 的 ack 帧**，
            # 服务端不认。退回我们自己的 internal_ext（绝不空）。
            ack_ext = ack_ext or self.internal_ext or self._default_internal_ext()
            self._pending_ack = build_ack(ack_ext, frame.get("log_id") or 0)

        for raw_message in response.get("messages") or []:
            try:
                self._handle_message_envelope(raw_message)
            except ProtobufError as exc:
                logger.debug("消息体解析失败，已跳过: %s", exc)
            except Exception as exc:  # pragma: no cover - 防御
                logger.error("处理抖音消息异常: %s", exc)

    async def _ack(self, ws: Any) -> None:
        """把待回的 ack 发出去。

        **必须 await**：``ws.send`` 是协程，忘了 await 就会静默地不回 ack，
        服务端随即断开连接。

        发送失败时**不清空** ``_pending_ack``，让下一帧还能重试：ack 丢失正是
        "约 21 秒后被 1006 断开"的直接原因，以前只记 DEBUG，用户只看到"连接
        中断"却拿不到真实原因。
        """
        pending = getattr(self, "_pending_ack", None)
        if not pending:
            return
        try:
            await ws.send(pending)
            self._pending_ack = None
            logger.debug("已回复 ack")
        except Exception as exc:  # pragma: no cover - 防御
            logger.warning("发送 ack 失败（将在下一帧重试）: %s", exc)

    def _explain_close(self, exc: BaseException) -> str:
        """把断线异常翻译成能指导用户的中文说明。"""
        text = str(exc)
        hint = ""
        if "1006" in text or "no close frame" in text:
            hint = (
                " —— 服务端未握手就断开，通常是 signature 缺失/失效或 room_id 已过期。"
                "请在浏览器打开该直播间复制完整 Cookie（含 ttwid、msToken）填入；"
                "若仍失败，说明当前抖音版本强制校验 signature，需要外部 JS 计算（可用 signature_provider）。"
            )
        return f"{exc}{hint}"

    async def _send_heartbeat(self, ws: Any) -> None:
        """发协议级心跳。"""
        try:
            await ws.send(HEARTBEAT_FRAME)
            self._last_heartbeat = time.monotonic()
            logger.debug("已发送抖音心跳")
        except Exception as exc:
            logger.debug("发送心跳失败: %s", exc)

    # ------------------------------------------------------------------
    # 消息分发
    # ------------------------------------------------------------------

    def _handle_message_envelope(self, data: bytes) -> None:
        """解析 ``Message`` 信封并分派到具体处理函数。"""
        fields = parse_message(data)
        method = _pick_str(fields, MSG_METHOD, "")
        if not method:
            return
        if method in IGNORED_METHODS:
            return

        msg_type = METHOD_MAP.get(method)
        if msg_type is None:
            logger.debug("未处理的抖音消息类型: %s", method)
            return

        body = _pick(fields, MSG_PAYLOAD, b"")
        if not isinstance(body, bytes) or not body:
            return
        payload = parse_message(body)

        if raw_logger.isEnabledFor(10):  # DEBUG
            raw_logger.debug("[抖音] %s fields=%s", method, sorted(payload.keys()))

        message: Optional[DanmakuMessage] = None
        if msg_type is MessageType.DANMAKU:
            message = self._build_chat(method, payload)
        elif msg_type is MessageType.GIFT:
            message = self._build_gift(payload)
        elif msg_type is MessageType.ENTER:
            message = self._build_member(payload)
        elif msg_type is MessageType.LIKE:
            message = self._build_like(payload)
        elif msg_type is MessageType.FOLLOW:
            message = self._build_social(payload)
        elif msg_type is MessageType.STATS:
            message = self._build_stats(payload)
        elif msg_type is MessageType.SYSTEM:
            message = self._build_control(payload)

        if message is not None:
            self._got_any_message = True
            self._emit_message(message)

    def _user_of(self, payload: Dict[int, List[Any]], numbers: Tuple[int, ...]) -> Tuple[str, str, int]:
        """从消息里取 (user_id, nickname, level)。"""
        user = _pick_message(payload, numbers)
        if not user:
            return ("", "", 0)
        user_id = str(_pick_int(user, USER_ID, 0) or _pick_str(user, USER_ID, ""))
        nickname = clean_text(_pick_str(user, USER_NICKNAME, ""), max_length=64)
        level = _pick_int(user, USER_LEVEL, 0)
        return (user_id, nickname, level)

    def _base(self, msg_type: MessageType) -> Dict[str, Any]:
        """构造消息时的公共参数。"""
        return {
            "platform": "douyin",
            "room_id": self.room_id,
            "msg_type": msg_type,
        }

    def _build_chat(self, method: str, payload: Dict[int, List[Any]]) -> Optional[DanmakuMessage]:
        """弹幕正文。"""
        content = clean_text(_pick_str(payload, CHAT_CONTENT, ""), max_length=500)
        if not content:
            return None
        user_id, nickname, level = self._user_of(payload, CHAT_USER)
        return DanmakuMessage(
            content=content,
            nickname=nickname or "未知用户",
            user_id=user_id,
            level=level,
            **self._base(MessageType.DANMAKU),
        )

    def _build_gift(self, payload: Dict[int, List[Any]]) -> Optional[DanmakuMessage]:
        """礼物。"""
        gift = _pick_message(payload, GIFT_GIFT)
        gift_name = ""
        if gift:
            gift_name = _pick_str(gift, GIFT_STRUCT_NAME, "")
        if not gift_name:
            gift_name = "礼物"
        count = _pick_int(payload, GIFT_COUNT, 1) or 1
        user_id, nickname, level = self._user_of(payload, GIFT_USER)
        return DanmakuMessage(
            content=f"送出 {gift_name} x{count}",
            nickname=nickname or "未知用户",
            user_id=user_id,
            level=level,
            **self._base(MessageType.GIFT),
        )

    def _build_member(self, payload: Dict[int, List[Any]]) -> Optional[DanmakuMessage]:
        """进入直播间。"""
        user_id, nickname, level = self._user_of(payload, MEMBER_USER)
        return DanmakuMessage(
            content="进入了直播间",
            nickname=nickname or "未知用户",
            user_id=user_id,
            level=level,
            **self._base(MessageType.ENTER),
        )

    def _build_like(self, payload: Dict[int, List[Any]]) -> Optional[DanmakuMessage]:
        """点赞。"""
        count = _pick_int(payload, LIKE_COUNT, 0)
        total = _pick_int(payload, LIKE_TOTAL, 0)
        user_id, nickname, level = self._user_of(payload, LIKE_USER)
        text = f"点赞 x{count}" if count else "点赞"
        if total:
            text += f"（累计 {total}）"
        return DanmakuMessage(
            content=text,
            nickname=nickname or "未知用户",
            user_id=user_id,
            level=level,
            **self._base(MessageType.LIKE),
        )

    def _build_social(self, payload: Dict[int, List[Any]]) -> Optional[DanmakuMessage]:
        """关注 / 分享。"""
        action = _pick_int(payload, SOCIAL_ACTION, 0)
        user_id, nickname, level = self._user_of(payload, SOCIAL_USER)
        text = "关注了主播" if action == 1 else "分享了直播间"
        return DanmakuMessage(
            content=text,
            nickname=nickname or "未知用户",
            user_id=user_id,
            level=level,
            **self._base(MessageType.FOLLOW),
        )

    def _build_stats(self, payload: Dict[int, List[Any]]) -> Optional[DanmakuMessage]:
        """在线人数等统计。"""
        total = _pick_int(payload, SEQ_TOTAL, 0)
        if not total:
            return None
        return DanmakuMessage(
            content=f"当前在线 {total} 人",
            nickname="",
            user_id="",
            **self._base(MessageType.STATS),
        )

    def _build_control(self, payload: Dict[int, List[Any]]) -> Optional[DanmakuMessage]:
        """直播状态变化。"""
        status = _pick_int(payload, CONTROL_STATUS, 0)
        text = {1: "直播已结束", 2: "直播已暂停", 3: "直播已恢复"}.get(status, "直播状态变化")
        return DanmakuMessage(content=text, nickname="", user_id="", **self._base(MessageType.SYSTEM))

    # ------------------------------------------------------------------
    # 断开
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        """关闭连接（用 ``_disconnecting`` 防重入）。"""
        if self._disconnecting:
            return
        self._disconnecting = True
        try:
            ws = self._ws
            self._ws = None
            if ws is not None:
                try:
                    await ws.close()
                except Exception as exc:  # pragma: no cover - 防御
                    logger.debug("关闭 WS 失败: %s", exc)
            session = self._session
            self._session = None
            if session is not None:
                try:
                    await session.close()
                except Exception as exc:  # pragma: no cover - 防御
                    logger.debug("关闭 aiohttp 会话失败: %s", exc)
            logger.info("抖音连接已关闭")
        finally:
            self._disconnecting = False


__all__ = [
    "DEFAULT_WS_HOST",
    "DouyinFetcher",
    "HEARTBEAT_FRAME",
    "HEADER_COMPRESS_TYPE",
    "MSG_MSG_ID",
    "build_ack",
    "decode_payload",
    "extract_room_info",
    "parse_headers_list",
    "parse_push_frame",
    "parse_response",
]
