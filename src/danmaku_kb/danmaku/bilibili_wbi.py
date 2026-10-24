# -*- coding: utf-8 -*-
"""B 站 WBI 签名（``getDanmuInfo`` 现在必须带签名，否则一律 ``-352``）。

## 为什么需要这个文件

blivedm 0.1.1 的 :meth:`BLiveClient._init_host_server` 直接请求
``/xlive/web-room/v1/index/getDanmuInfo?id=<room>&type=0``，**不带任何签名**。
2026-10 实测该接口对未签名请求一律返回::

    {"code": -352, "message": "-352", "ttl": 1}

而 ``init_room()`` 只要 ``_init_host_server()`` 返回 False 就抛
``InitError('init_room() failed')``，整个连接直接失败——换房间号、
加 ``buvid3`` cookie、改 User-Agent 都没有用。

同一时刻用浏览器的 ``wbi`` 方式签名（``w_rid`` + ``wts``）请求，
接口返回 ``code=0`` 并给出 ``host_list`` / ``token``。所以这不是 IP 风控，
是**接口签名要求**。签名算法与 B 站 Web 端一致：

1. ``GET https://api.bilibili.com/x/web-interface/nav`` 取 ``wbi_img``
   （未登录返回 ``code=-101``，但 ``data.wbi_img`` 仍然有效）；
2. 从 ``img_url`` / ``sub_url`` 取文件名（去扩展名）作为 ``img_key`` / ``sub_key``；
3. 两者拼接后按固定 64 位置换表重排，取前 32 位得到 ``mixin_key``；
4. 业务参数 + ``wts`` 按 key 排序、剔除值里的 ``!'()*`` 后 urlencode，
   再拼上 ``mixin_key`` 做 MD5 得到 ``w_rid``。

纯函数（:func:`mixin_key` / :func:`sign_params`）不依赖网络，便于离线测试。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any, Dict, Mapping, Optional, Tuple

from ..utils.logging_setup import get_logger

logger = get_logger("danmaku.bilibili_wbi")

#: 置换表：B 站 Web 端 ``mixinKeyEncTab``，顺序错了签名就对不上。
MIXIN_KEY_ENC_TAB: Tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

#: 请求必须伪装成浏览器，否则 B 站回 HTTP 412 + 反爬 HTML。
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"

#: wbi 密钥轮换周期按天算，缓存一小时足够，避免每次连接都多一次请求。
_KEY_CACHE_TTL = 3600.0
_key_cache: Dict[str, Any] = {"mixin_key": None, "fetched_at": 0.0}


def mixin_key(img_key: str, sub_key: str) -> str:
    """按置换表把 ``img_key`` + ``sub_key`` 压成 32 位 ``mixin_key``。"""
    raw = img_key + sub_key
    # 保障：密钥异常短时按可用长度截断，避免 IndexError 把连接流程带崩。
    limit = min(32, len(raw))
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB[:limit])


def _filter_value(value: Any) -> str:
    """剔除 ``!'()*``——这几个字符会让服务端算出的 ``w_rid`` 与本端不一致。"""
    return "".join(ch for ch in str(value) if ch not in "!'()*")


def sign_params(
    params: Mapping[str, Any],
    key: str,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """给业务参数补上 ``wts`` / ``w_rid``。返回值可直接交给 aiohttp。

    返回的**业务值已剔除** ``!'()*``：签名与真实请求必须用同一份值，
    否则服务端重算的 ``w_rid`` 与本端不一致，照样 ``-352``。
    """
    signed: Dict[str, Any] = {k: _filter_value(v) for k, v in params.items()}
    signed["wts"] = int(now if now is not None else time.time())
    items = sorted(signed.items())
    query = urllib.parse.urlencode(items)
    signed["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
    return signed


def _key_from_url(url: str) -> str:
    """``.../7cd084941338484aae1ad9425b84077c.png`` → 文件名（去扩展名）。"""
    return str(url).rsplit("/", 1)[-1].split(".")[0]


async def fetch_mixin_key(session: Any, *, force: bool = False) -> Optional[str]:
    """取当前 ``mixin_key``；失败返回 None（调用方决定降级策略）。"""
    cached = _key_cache.get("mixin_key")
    if not force and cached and (time.time() - _key_cache["fetched_at"]) < _KEY_CACHE_TTL:
        return str(cached)
    try:
        async with session.get(
            NAV_URL,
            headers={"User-Agent": _BROWSER_UA, "Referer": "https://www.bilibili.com/"},
        ) as resp:
            if resp.status != 200:
                logger.warning("取 wbi 密钥失败：HTTP %s", resp.status)
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:  # pragma: no cover - 网络异常分支
        logger.warning("取 wbi 密钥请求失败: %s", exc)
        return None

    wbi = (data or {}).get("data", {}).get("wbi_img") or {}
    img_key = _key_from_url(wbi.get("img_url", ""))
    sub_key = _key_from_url(wbi.get("sub_url", ""))
    if not img_key or not sub_key:
        logger.warning("wbi 密钥格式异常: %r", wbi)
        return None
    key = mixin_key(img_key, sub_key)
    _key_cache["mixin_key"] = key
    _key_cache["fetched_at"] = time.time()
    return key


async def fetch_danmu_info(
    session: Any,
    room_id: Any,
    *,
    timeout: float = 8.0,
) -> Optional[Dict[str, Any]]:
    """带 WBI 签名取弹幕服务器配置（``host_list`` + ``token``）。

    :returns: 接口 ``data`` 字段；任何失败（网络/签名/风控）都返回 None，
        由调用方决定是否退回默认服务器列表，绝不让异常穿透到连接流程。
    """
    try:
        import aiohttp  # 局部导入：纯函数与离线测试不需要它
    except Exception:  # pragma: no cover - 依赖环境
        return None
    key = await fetch_mixin_key(session)
    if not key:
        return None
    signed = sign_params({"id": room_id, "type": 0}, key)
    try:
        async with session.get(
            DANMU_INFO_URL,
            params=signed,
            headers={
                "User-Agent": _BROWSER_UA,
                "Referer": "https://live.bilibili.com/%s" % room_id,
            },
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                logger.warning("getDanmuInfo 签名请求 HTTP %s", resp.status)
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:  # pragma: no cover - 网络异常分支
        logger.warning("getDanmuInfo 签名请求失败: %s", exc)
        return None
    if (data or {}).get("code") != 0:
        logger.warning("getDanmuInfo 签名请求返回 %s", (data or {}).get("code"))
        return None
    return data.get("data") or {}


def reset_cache() -> None:
    """清空密钥缓存（测试与「换网络后重连」用）。"""
    _key_cache["mixin_key"] = None
    _key_cache["fetched_at"] = 0.0
