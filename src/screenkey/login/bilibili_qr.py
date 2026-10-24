# -*- coding: utf-8 -*-
"""B 站扫码登录。

参考项目用 ``segno`` 生成二维码图片再让用户拿手机扫，但**扫码之后的轮询
状态机写得比较粗**（只区分成功/失败，没有区分"已扫码待确认"）。
这里把 B 站官方的四种状态都区分出来，界面就能给出准确提示。

协议（已对线上接口实测通过）::

    1. GET https://passport.bilibili.com/x/passport-login/web/qrcode/generate
       → {"code":0,"data":{"url":"...","qrcode_key":"..."}}

    2. 把 data.url 渲染成二维码给用户扫

    3. GET https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=...
       → {"code":0,"data":{"code":<内层状态>,"message":"...","url":"..."}}
         内层 code: 86101 未扫码 / 86090 已扫码待确认 / 86038 已过期 / 0 成功

    4. 成功时响应头 ``Set-Cookie`` 里带 SESSDATA 等，这就是抓弹幕要用的凭据。

为什么不用 ``qrcode`` 库
------------------------
``qrcode`` 依赖 Pillow（几十 MB）。本项目只需要"把一段文本变成二维码像素"，
:mod:`screenkey.login.qr_image` 里用 ``segno``（纯 Python，可导出 PNG 字节）
并在缺依赖时回落到一个自绘的占位图，保证界面永远能起来。
"""

from __future__ import annotations

import http.cookies
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from ..utils.logging_setup import get_logger

logger = get_logger("login.bilibili")

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}

#: 登录后需要的 Cookie 名（SESSDATA 是抓弹幕的关键）
WANTED_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")


class QRState(str, Enum):
    """扫码登录的状态。"""

    WAITING = "waiting"          # 等待扫码（86101）
    SCANNED = "scanned"          # 已扫码，等待手机确认（86090）
    EXPIRED = "expired"          # 二维码过期（86038）
    SUCCESS = "success"          # 登录成功（0）
    ERROR = "error"              # 其它错误

    @property
    def label(self) -> str:
        """给界面显示的中文说明。"""
        return {
            QRState.WAITING: "请用 B 站手机客户端扫描二维码",
            QRState.SCANNED: "已扫码，请在手机上确认登录",
            QRState.EXPIRED: "二维码已过期，请点击刷新",
            QRState.SUCCESS: "登录成功",
            QRState.ERROR: "登录失败",
        }[self]


#: B 站内层状态码 → 我们的状态
#:
#: 官方只文档化了这 4 个码。未列出的码 ``poll_once`` 会报 ERROR，但**后台轮询
#: 不会因此终止**：``start_polling`` 会容忍连续 ``MAX_CONSECUTIVE_POLL_ERRORS``
#: 次失败，给协议漂移（B 站加了新中间态）留出容错空间。早期实现是"一次
#: ERROR 就退出轮询"，用户扫完码点确认时程序早已放弃，只看到"登录失败"。
_INNER_CODE_MAP = {
    0: QRState.SUCCESS,
    86101: QRState.WAITING,
    86090: QRState.SCANNED,
    86038: QRState.EXPIRED,
}

#: 后台轮询允许的连续网络错误次数。单次 ``poll_once`` 遇到瞬时网络抖动会
#: 返回 ERROR，但后台轮询不能因为一次抖动就永久终止（否则用户扫完码点确认
#: 时程序早已放弃，SESSDATA 永远拿不到）。
MAX_CONSECUTIVE_POLL_ERRORS = 5


@dataclass
class QRCodeSession:
    """一次二维码登录会话。"""

    url: str = ""
    qrcode_key: str = ""
    created_at: float = field(default_factory=time.time)
    #: 生成出来的二维码 PNG 字节
    png: bytes = b""

    @property
    def age(self) -> float:
        """已经过去多少秒。"""
        return time.time() - self.created_at

    @property
    def expired(self) -> bool:
        """本地判断是否已过期（B 站二维码约 180 秒有效）。"""
        return self.age > 170

    def to_dict(self) -> Dict[str, Any]:
        """序列化（不含 PNG）。"""
        return {"url": self.url, "qrcode_key": self.qrcode_key}


@dataclass
class LoginResult:
    """登录结果。"""

    state: QRState = QRState.WAITING
    sessdata: str = ""
    cookies: Dict[str, str] = field(default_factory=dict)
    message: str = ""
    dede_user_id: str = ""
    nickname: str = ""

    @property
    def ok(self) -> bool:
        """是否登录成功。"""
        return self.state is QRState.SUCCESS and bool(self.sessdata)

    @property
    def cookie_string(self) -> str:
        """拼成 ``Cookie:`` 请求头用的字符串。"""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


def _http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15.0) -> Any:
    """发一个 GET，返回响应对象（调用方负责读 body / 响应头）。"""
    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)
    request = urllib.request.Request(url, headers=merged)
    return urllib.request.urlopen(request, timeout=timeout)


def parse_cookies(headers: Any) -> Dict[str, str]:
    """从响应头里解析出 Set-Cookie。

    ``urllib`` 的 ``msg.get_all('Set-Cookie')`` 返回的是原始行列表，
    直接用 ``http.cookies.SimpleCookie`` 逐条解析。
    """
    cookies: Dict[str, str] = {}
    raw_list: List[str] = []
    try:
        if hasattr(headers, "get_all"):
            raw_list = headers.get_all("Set-Cookie") or []
        elif headers:
            value = headers.get("Set-Cookie")
            if value:
                raw_list = [value]
    except Exception as exc:  # pragma: no cover - 防御
        logger.debug("读取 Set-Cookie 失败: %s", exc)

    for raw in raw_list:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            continue
        for key, morsel in jar.items():
            cookies[key] = morsel.value
    return cookies


class BilibiliQRLogin:
    """B 站扫码登录客户端。

    :param timeout: 单次 HTTP 超时
    """

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._session: Optional[QRCodeSession] = None
        self._result = LoginResult()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 生成二维码
    # ------------------------------------------------------------------

    def generate(self) -> Optional[QRCodeSession]:
        """申请一个二维码；失败返回 ``None``。

        生成出来的 PNG 会存进 ``session.png``，界面直接拿去显示。

        申请前先停掉可能还在跑的旧轮询线程：用户点「刷新二维码」时旧线程
        还在轮询上一张码，不停掉就会出现两个线程同时改 ``_result``/``_session``
        的竞态，界面上的状态会来回跳。
        """
        self.stop_polling()
        try:
            response = _http_get(QR_GENERATE_URL, timeout=self.timeout)
            with response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            logger.error("申请二维码失败 HTTP %s: %s", exc.code, exc.reason)
            self._result = LoginResult(state=QRState.ERROR, message=f"网络错误 HTTP {exc.code}")
            return None
        except Exception as exc:
            logger.error("申请二维码失败: %s", exc)
            self._result = LoginResult(state=QRState.ERROR, message=f"网络错误：{exc}")
            return None

        try:
            payload = json.loads(body)
        except ValueError:
            logger.error("二维码接口返回的不是 JSON: %s", body[:200])
            self._result = LoginResult(state=QRState.ERROR, message="接口返回格式异常")
            return None

        if payload.get("code") != 0:
            message = payload.get("message") or "申请二维码失败"
            logger.error("申请二维码被拒: %s", message)
            self._result = LoginResult(state=QRState.ERROR, message=str(message))
            return None

        data = payload.get("data") or {}
        session = QRCodeSession(
            url=str(data.get("url") or ""),
            qrcode_key=str(data.get("qrcode_key") or ""),
        )
        if not session.url or not session.qrcode_key:
            logger.error("二维码响应缺少 url/qrcode_key: %s", data)
            self._result = LoginResult(state=QRState.ERROR, message="接口返回缺少必要字段")
            return None

        session.png = self._render_qr(session.url)
        self._session = session
        self._result = LoginResult(state=QRState.WAITING, message=QRState.WAITING.label)
        logger.info("已申请 B 站登录二维码")
        return session

    @staticmethod
    def _render_qr(text: str) -> bytes:
        """把 URL 渲染成 PNG 字节。"""
        from .qr_image import make_qr_png

        return make_qr_png(text)

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------

    def poll_once(self) -> LoginResult:
        """轮询一次扫码状态。"""
        session = self._session
        if session is None:
            return LoginResult(state=QRState.ERROR, message="还没有申请二维码")

        if session.expired:
            self._result = LoginResult(state=QRState.EXPIRED, message=QRState.EXPIRED.label)
            return self._result

        url = QR_POLL_URL + "?" + urllib.parse.urlencode({"qrcode_key": session.qrcode_key})
        try:
            response = _http_get(url, timeout=self.timeout)
            with response:
                body = response.read().decode("utf-8", "replace")
                cookies = parse_cookies(response.headers)
        except urllib.error.HTTPError as exc:
            self._result = LoginResult(state=QRState.ERROR, message=f"网络错误 HTTP {exc.code}")
            return self._result
        except Exception as exc:
            self._result = LoginResult(state=QRState.ERROR, message=f"网络错误：{exc}")
            return self._result

        try:
            payload = json.loads(body)
        except ValueError:
            self._result = LoginResult(state=QRState.ERROR, message="接口返回格式异常")
            return self._result

        if payload.get("code") != 0:
            message = payload.get("message") or "轮询失败"
            self._result = LoginResult(state=QRState.ERROR, message=str(message))
            return self._result

        data = payload.get("data") or {}
        inner_code = data.get("code")
        try:
            inner_code = int(inner_code)
        except (TypeError, ValueError):
            inner_code = -1

        state = _INNER_CODE_MAP.get(inner_code, QRState.ERROR)
        message = str(data.get("message") or state.label)

        if state is QRState.SUCCESS:
            sessdata = cookies.get("SESSDATA", "")
            if not sessdata:
                # 极少数情况 cookie 在 data.url 的查询串里
                sessdata = _sessdata_from_url(data.get("url") or "")
            wanted = {k: v for k, v in cookies.items() if k in WANTED_COOKIES}
            if not wanted and sessdata:
                wanted["SESSDATA"] = sessdata
            result = LoginResult(
                state=QRState.SUCCESS,
                sessdata=sessdata,
                cookies=wanted or cookies,
                message="登录成功",
                dede_user_id=cookies.get("DedeUserID", ""),
            )
            logger.info("B 站登录成功，拿到 %d 个 cookie", len(result.cookies))
        else:
            result = LoginResult(state=state, message=message)

        self._result = result
        return result

    @property
    def result(self) -> LoginResult:
        """最近一次结果。"""
        return self._result

    @property
    def session(self) -> Optional[QRCodeSession]:
        """当前二维码会话。"""
        return self._session

    # ------------------------------------------------------------------
    # 后台轮询
    # ------------------------------------------------------------------

    def start_polling(
        self,
        on_update: Optional[Callable[[LoginResult], None]] = None,
        interval: float = 2.0,
        max_seconds: float = 180.0,
    ) -> bool:
        """在后台线程里轮询，直到成功/过期/超时。

        :param on_update: 每次状态变化时回调（在后台线程里被调用）
        :param interval: 轮询间隔
        :param max_seconds: 最长等待时间
        """
        if self._session is None:
            return False
        if self._thread is not None and self._thread.is_alive():
            return False

        self._stop_event.clear()

        def _run() -> None:
            deadline = time.time() + max_seconds
            last_state: Optional[QRState] = None
            consecutive_errors = 0
            while not self._stop_event.is_set() and time.time() < deadline:
                result = self.poll_once()

                if result.state is QRState.ERROR:
                    # 单次网络抖动不该让整个登录流程报废：用户可能正在手机上
                    # 扫码确认，这里一退出就永远收不到成功回调了。只有连续
                    # MAX_CONSECUTIVE_POLL_ERRORS 次失败才认定真的连不上。
                    consecutive_errors += 1
                    if consecutive_errors < MAX_CONSECUTIVE_POLL_ERRORS:
                        logger.warning(
                            "B 站登录轮询第 %d 次失败（将重试）：%s",
                            consecutive_errors, result.message,
                        )
                        self._stop_event.wait(interval)
                        continue
                    result = LoginResult(
                        state=QRState.ERROR,
                        message=f"连续 {consecutive_errors} 次网络失败，已停止等待：{result.message}",
                    )
                else:
                    consecutive_errors = 0

                if result.state is not last_state:
                    last_state = result.state
                    if on_update is not None:
                        try:
                            on_update(result)
                        except Exception as exc:  # pragma: no cover - 防御
                            logger.error("登录状态回调异常（已隔离）: %s", exc)
                if result.state in (QRState.SUCCESS, QRState.EXPIRED, QRState.ERROR):
                    self._result = result
                    return
                self._stop_event.wait(interval)
            if not self._stop_event.is_set() and last_state not in (QRState.SUCCESS,):
                timeout_result = LoginResult(state=QRState.EXPIRED, message="等待扫码超时，请刷新二维码")
                self._result = timeout_result
                if on_update is not None:
                    try:
                        on_update(timeout_result)
                    except Exception:  # pragma: no cover
                        pass

        self._thread = threading.Thread(target=_run, name="bili-qr-login", daemon=True)
        self._thread.start()
        return True

    def stop_polling(self, timeout: float = 2.0) -> None:
        """停止后台轮询。

        ``join`` 超时后线程可能还活着（它可能正卡在一次 15 秒的 HTTP 请求里）。
        这时**不能**把 ``_thread`` 置空：否则下一次 ``start_polling`` 会另起
        一个线程，旧线程醒来后继续写 ``_result``，两个线程互相覆盖状态。
        保留引用让 ``start_polling`` 的 ``is_alive()`` 检查拦住重复启动。
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("B 站登录轮询线程未在 %.1fs 内退出（仍在等待网络请求）", timeout)
                return
        self._thread = None


def _sessdata_from_url(url: str) -> str:
    """从成功回调的 url 里抠 SESSDATA（兜底路径）。"""
    if not url:
        return ""
    try:
        query = urllib.parse.urlparse(url).query
        params = urllib.parse.parse_qs(query)
        values = params.get("SESSDATA") or []
        return values[0] if values else ""
    except Exception:
        return ""


def login_sync(timeout: float = 15.0) -> Optional[QRCodeSession]:
    """同步申请一个二维码（不轮询）。"""
    return BilibiliQRLogin(timeout=timeout).generate()


__all__ = [
    "BilibiliQRLogin",
    "LoginResult",
    "QRCodeSession",
    "QRState",
    "QR_GENERATE_URL",
    "QR_POLL_URL",
    "WANTED_COOKIES",
    "login_sync",
    "parse_cookies",
]
