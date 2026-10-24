"""抖音登录辅助。

**为什么没有扫码登录**
--------------------
参考项目尝试过抖音扫码：``https://www.douyin.com/passport/web/get_qrcode/``
返回 ``error_code: 4031`` 并下发一个反爬 JS 挑战页，暖场会话最多只能拿到
``__ac_nonce``，拿不到 ``ttwid``，二维码永远画不出来。

V2 实测复核：抖音 Web 端所有 passport 接口现在都要求 ``ttwid`` + ``msToken``
一类由 JS 现场生成的设备指纹，纯 HTTP 客户端无法自行生成；即便伪造出来，
``webcast`` 弹幕连接还会二次校验 signature。**与其做一个点了必然失败的按钮，
不如老实告诉用户怎么做。**

因此本模块只提供"从浏览器导出 Cookie"的引导与解析，不做假的扫码：

1. 在 Chrome/Edge 里打开 ``https://live.douyin.com/<房间号>``；
2. F12 → Application → Cookies → ``https://www.douyin.com``；
3. 至少复制 ``ttwid``，建议连 ``sessionid`` / ``msToken`` / ``__ac_nonce`` 一起复制；
4. 粘进登录框（支持 ``document.cookie`` 格式、``name=value; name=value`` 格式、
   以及 DevTools 的 ``name<TAB>value`` 表格粘贴）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..utils.logging_setup import get_logger

logger = get_logger(__name__)

__all__ = [
    "DOUYIN_GUIDE_STEPS",
    "DOUYIN_GUIDE_TEXT",
    "IMPORTANT_COOKIE_NAMES",
    "CookieSanitizeResult",
    "DouyinLoginInfo",
    "detect_cookie_source",
    "parse_cookie_string",
    "sanitize_cookie_header",
    "validate_cookie_string",
    "build_cookie_header",
    "check_cookie_text",
]

#: 拿到这些 cookie 才算"有设备指纹"。
IMPORTANT_COOKIE_NAMES = ("ttwid", "sessionid", "msToken", "__ac_nonce", "odin_tt", "passport_csrf_token")

DOUYIN_GUIDE_STEPS = [
    "用 Chrome / Edge 打开 https://live.douyin.com/ 并进入任意直播间",
    "按 F12 打开开发者工具，切到 Application（应用）→ Cookies → https://www.douyin.com",
    "找到 ttwid 并复制它的值；若列表里有 sessionid / msToken / __ac_nonce 也一并复制",
    "回到本程序，把这些内容整体粘进下方输入框（支持 name=value; 连写、DevTools 表格、Cookie 请求头整行）",
    "点「保存 Cookie」，再回到主界面填房间号并连接",
]

DOUYIN_GUIDE_TEXT = "\n".join(f"{i}. {s}" for i, s in enumerate(DOUYIN_GUIDE_STEPS, 1))

# name=value 之间的分隔：分号 / 换行 / 制表符（DevTools 表格粘贴）/ 逗号。
_SPLIT_RE = re.compile(r"[;\n\r\t,]+")
_PAIR_RE = re.compile(r"^\s*([A-Za-z0-9_\-\.]+)\s*=\s*(.*?)\s*$")
_BARE_RE = re.compile(r"^\s*([A-Za-z0-9_\-\.]+)\s*$")
#: 用户可能整行粘进来的请求头前缀（``Cookie: ...`` / ``Set-Cookie: ...``）
_HEADER_PREFIX_RE = re.compile(r"^\s*(?:set-)?cookie\s*:\s*", re.IGNORECASE)
#: 一个合法的 cookie 名
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")


def _unquote(value: str) -> str:
    """去掉 DevTools 表格里值两侧可能带的引号。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_cookie_string(text: str) -> Dict[str, str]:
    """把用户粘贴的任意 cookie 文本解析成 dict。

    容忍：

    * ``a=1; b=2``
    * 多行 ``a=1`` / ``b=2``
    * 整行请求头 ``Cookie: a=1; b=2``（以前整行被丢弃）
    * DevTools 表格粘贴 ``name<TAB>value``（以前 TAB 被当成分隔符，
      ``ttwid\\t1%7Cabc`` 会解析成 ``{"ttwid": ""}``——名字留下了、值没了，
      而界面引导恰恰教用户从 DevTools 复制）
    * 裸名字（无 ``=``）：视为需要用户补值，忽略但记录
    * 值里带 ``=``（只在第一个 ``=`` 处切分）
    """

    result: Dict[str, str] = {}
    if not text:
        return result

    # 逐行处理是必须的：DevTools 的表格粘贴是"一行一个 cookie"，
    # 若先把 TAB 当分隔符整体切分，名字和值会被拆成两个独立 token。
    for line in text.splitlines():
        line = _HEADER_PREFIX_RE.sub("", line).strip()
        if not line:
            continue

        if "\t" in line:
            cells = [_unquote(cell.strip()) for cell in line.split("\t")]
            name = cells[0]
            # 只有"首列是纯名字"才按表格处理；``a=1<TAB>b=2`` 这种仍走普通路径
            if _NAME_RE.match(name) and "=" not in name:
                value = cells[1] if len(cells) > 1 else ""
                result[name] = value
                continue

        for raw in _SPLIT_RE.split(line):
            part = raw.strip()
            if not part:
                continue
            m = _PAIR_RE.match(part)
            if m:
                name, value = m.group(1), m.group(2)
                if name:
                    result[name] = _unquote(value)
                continue
            m = _BARE_RE.match(part)
            if m:
                # 只有名字没有值：多半是从表格里粘丢了，留空占位
                result.setdefault(m.group(1), "")
    return result


def detect_cookie_source(text: str) -> str:
    """粗略判断用户粘的是哪种格式（只用于给界面的提示文案）。"""
    if not text:
        return ""
    if re.search(r"(?:set-)?cookie\s*:", text, re.IGNORECASE):
        return "请求头"
    if "\t" in text:
        return "DevTools 表格"
    return "cookie 文本"


def build_cookie_header(cookies: Dict[str, str]) -> str:
    """dict → ``Cookie`` 请求头。空值项被跳过（避免 ``ttwid=;`` 这种无效头）。"""

    parts = [f"{k}={v}" for k, v in cookies.items() if k and v]
    return "; ".join(parts)


#: HTTP 头值里允许的字符：TAB 与可见 ASCII。
#: websockets 的 ``Headers.__setitem__`` 用的正是这条规则
#: （``re.compile(r"[\x09\x20-\x7e\x80-\xff]*").fullmatch``）：值里出现别的字符就
#: 抛 ``InvalidHeaderValue``，而它的 ``__str__`` 是
#: ``f"invalid {name} header: {value}"`` —— 界面于是显示
#: 「invalid Cookie header: ttwid=1%7C…」，整串 cookie 糊在报错里，
#: 用户完全看不出是自己粘的内容有问题。
_HTTP_HEADER_BAD_CHARS = re.compile(r"[^\t\x20-\x7e]")

#: 复制时被"省略/截断"的痕迹。浏览器 DevTools、某些输入框在显示超长值时会把
#: 中间换成这些字符；用户连它一起复制下来，cookie 值就永久缺了一段。
_TRUNCATION_MARKS = ("\u2026", "\u2025", "\u22ef")

#: 全角标点 → 半角。手打或从截图抄 cookie 时最容易把 ``;`` ``=`` 打成全角。
_FULLWIDTH_MAP = str.maketrans(
    {"；": ";", "，": ";", "、": ";", "＝": "=", "：": "=", "　": " "}
)


@dataclass
class CookieSanitizeResult:
    """``sanitize_cookie_header`` 的结果。"""

    value: str = ""
    notes: List[str] = field(default_factory=list)
    #: 是否发现了「…」这类"内容被省略"的痕迹（cookie 已经不完整）
    truncated: bool = False

    @property
    def changed(self) -> bool:
        """是否动过内容（调用方据此决定要不要写回配置/提示用户）。"""

        return bool(self.notes)


def sanitize_cookie_header(text: str) -> CookieSanitizeResult:
    """把用户粘来的 Cookie 修成"能放进 HTTP 头"的形式，并说明修了什么。

    真实故障（2026-10-22，用户报「抖音链接报错」）：

    用户从浏览器复制了一份 **1024 字符**的 cookie，但中间第 512 个字符被
    换成了 ``…`` —— 复制源把超长值省略了。之后 ``parse_cookie_string``、
    配置保存、``check_cookie_text`` 全都没意见（ttwid 还在，界面照样显示
    "有效"），一直走到 ``websockets.connect()`` 才炸::

        invalid Cookie header: ttwid=1%7C5SYJ...…...%22

    随后重试 5 次，每次都是同一句 —— 因为它不是网络问题，是**粘进来的
    字符串本身带了非法字符**，重试一万次也不会好。

    这里做四件事：

    1. 全角标点归一（否则会被当成非法字符删掉，把两个 cookie 粘成一个）；
    2. 识别并去掉「…」截断痕迹，同时置 ``truncated`` 让上层提醒用户重粘；
    3. 删掉其它 HTTP 头不允许的字符（中文、零宽字符、控制字符……）；
    4. 空白与 ``;`` 分隔符归一。

    :param text: 用户粘贴的 cookie 文本（``a=1; b=2`` / 请求头整行 / 表格）
    """

    result = CookieSanitizeResult(value="", notes=[], truncated=False)
    if not text:
        return result

    cleaned = str(text)

    translated = cleaned.translate(_FULLWIDTH_MAP)
    if translated != cleaned:
        cleaned = translated
        result.notes.append("已把全角标点（；，＝ 等）换成半角。")

    for mark in _TRUNCATION_MARKS:
        if mark in cleaned:
            result.truncated = True
            cleaned = cleaned.replace(mark, "")
    if result.truncated:
        result.notes.append(
            "Cookie 里出现了「…」省略号：复制时中间那段被省略了，只保留了首尾。"
            "已把它去掉，但 cookie 值已经不完整 —— 若连接仍失败，请重新完整复制一次"
            "（F12 → Network → 任一请求 → Request Headers → 复制整行 Cookie）。"
        )

    bad = _HTTP_HEADER_BAD_CHARS.findall(cleaned)
    if bad:
        cleaned = _HTTP_HEADER_BAD_CHARS.sub("", cleaned)
        result.notes.append(
            f"已去掉 {len(bad)} 个不能出现在 HTTP 头里的字符（中文/零宽/控制字符等）。"
        )

    cleaned = " ".join(cleaned.split())
    cleaned = re.sub(r"\s*;\s*", "; ", cleaned).strip().strip(";").strip()
    result.value = cleaned
    return result


@dataclass
class DouyinLoginInfo:
    """抖音 Cookie 解析结果。"""

    cookies: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    source: str = ""

    @property
    def has_ttwid(self) -> bool:
        return bool(self.cookies.get("ttwid"))

    @property
    def has_session(self) -> bool:
        return bool(self.cookies.get("sessionid"))

    @property
    def ok(self) -> bool:
        """能连弹幕的最低要求：有 ttwid（设备指纹），否则页面必返验证页。"""

        return self.has_ttwid

    @property
    def level(self) -> str:
        if self.has_ttwid and self.has_session:
            return "已登录（可看全部弹幕）"
        if self.has_ttwid:
            return "仅有设备指纹（能连，部分弹幕可能不推送）"
        return "缺少 ttwid（大概率被反爬验证页拦住）"

    def to_dict(self) -> Dict[str, object]:
        return {
            "cookies": dict(self.cookies),
            "missing": list(self.missing),
            "level": self.level,
            "ok": self.ok,
        }


def validate_cookie_string(text: str) -> DouyinLoginInfo:
    """解析并评估一段 cookie 文本。"""

    cookies = parse_cookie_string(text)
    missing = [n for n in IMPORTANT_COOKIE_NAMES if not cookies.get(n)]
    return DouyinLoginInfo(cookies=cookies, missing=missing, source=detect_cookie_source(text))


def check_cookie_text(text: str) -> tuple:
    """给界面用的轻量校验：返回 ``(ok, 中文提示)``。"""

    info = validate_cookie_string(text)
    if not info.cookies:
        return False, "没有解析到任何 Cookie，请检查粘贴内容"
    # 内容被省略（「…」）时，cookie 是残缺的：ttwid 可能还在，所以后面的
    # 检查会一路通过，直到握手才炸出 "invalid Cookie header"。必须在这里拦住。
    sanitized = sanitize_cookie_header(text)
    if sanitized.truncated:
        return False, (
            "Cookie 里出现了「…」省略号 —— 复制时中间那段被省略了，值已经不完整。"
            "请重新完整复制一次（F12 → Network → 任一请求 → Request Headers → 整行 Cookie）。"
        )
    if not info.has_ttwid:
        extra = ""
        if "ttwid" in info.cookies and not info.cookies["ttwid"]:
            extra = "（ttwid 是空的，多半只复制了名字）"
        return False, f"缺少 ttwid，抖音会返回反爬验证页{extra}"
    # 告诉用户"我按哪种格式理解了你粘的内容"——格式识别错时用户能立刻发现
    source = f"（识别为{info.source}）" if info.source else ""
    if info.has_session:
        return True, f"Cookie 有效：{info.level}，共 {len(info.cookies)} 项{source}"
    return True, f"Cookie 可用但未登录：{info.level}，共 {len(info.cookies)} 项{source}"


def extract_ttwid(cookie_or_header: str) -> Optional[str]:
    """从 cookie 文本或 ``Cookie`` 头里单独取出 ttwid。"""

    cookies = parse_cookie_string(cookie_or_header)
    value = cookies.get("ttwid")
    return value or None
