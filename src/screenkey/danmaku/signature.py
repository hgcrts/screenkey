"""抖音 WS ``signature`` 参数计算（Node + 混淆 JS 桥）。

为什么需要这个模块
------------------
抖音的弹幕 WebSocket 在 URL 上要求一个 ``signature`` 参数。它**不是** MD5，
也不是 ``X-Bogus`` 请求头，而是::

    signature = get_sign(md5(13 个字段的逗号串))

其中 ``get_sign`` 由抖音自己的 ``webmssdk.js``（已混淆）现场计算，纯 Python
无法复现。缺这个参数的后果很明确：**连接建立后约 155ms 被服务端以 1006
断开，一帧数据都收不到**。

本模块的取舍
------------
1. **绝不假装能算**。没有 Node 或没有 JS 文件时，:func:`signature_available`
   返回 False，抓取器会如实把失败原因报给界面，而不是伪装成"已连接"。
2. **常驻 Node 进程**。``sign.js`` 有 480KB+，每次冷启动都要重新解析混淆代码
   （实测 300ms+）。所以这里起一个长驻子进程，用「一行 md5 进 / 一行签名出」
   的协议复用已加载的 VM 上下文，单次计算降到毫秒级。
3. **失败即重启**。Node 进程崩了就丢掉重建，连续失败一定次数后放弃并把
   ``signature_provider`` 置空，避免每次弹幕都卡 60 秒超时。
4. **JS 文件是抖音的知识产权**。它随包携带只是为了研究学习，不要商用分发。
   :func:`find_sign_js` 也支持用环境变量指向你自己的副本。
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..utils.logging_setup import get_logger

logger = get_logger("danmaku.signature")

#: 参与 MD5 的字段，**顺序是协议的一部分，不能改**
SIGN_MD5_FIELDS: Sequence[str] = (
    "live_id",
    "aid",
    "version_code",
    "webcast_sdk_version",
    "room_id",
    "sub_room_id",
    "sub_channel_id",
    "did_rule",
    "user_unique_id",
    "device_platform",
    "device_type",
    "ac",
    "identity",
)

#: 环境变量：指向你自己的 ``sign.js``
ENV_SIGN_JS = "SCREENKEY_SIGN_JS"
#: 改名前的环境变量名，老脚本仍在用
ENV_SIGN_JS_LEGACY = "DANMAKU_KB_SIGN_JS"
#: 环境变量：指定 node 可执行文件
ENV_NODE = "SCREENKEY_NODE"
#: 改名前的环境变量名
ENV_NODE_LEGACY = "DANMAKU_KB_NODE"

#: 单次计算超时（秒）
SIGN_TIMEOUT = 20.0
#: 连续失败多少次后彻底放弃
MAX_CONSECUTIVE_FAILURES = 3

#: 包内自带的 JS 位置
_PACKAGE_DATA = Path(__file__).resolve().parent.parent / "data"
_BUNDLED_SIGN_JS = _PACKAGE_DATA / "sign.js"


# ----------------------------------------------------------------------
# JS 驱动（常驻进程协议：stdin 一行 md5 → stdout 一行签名）
# ----------------------------------------------------------------------

_DRIVER_JS = r"""
const fs = require("fs");
const vm = require("vm");
const readline = require("readline");

const signPath = process.argv[1];
const ua = process.argv[2] || "Mozilla/5.0 (Windows NT 10.0; Win64; x64)";

function makeContext() {
  const ctx = {
    console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
    window: {},
    document: {},
    navigator: { userAgent: ua, platform: "Win32", language: "zh-CN" },
    location: { href: "https://live.douyin.com/", protocol: "https:" },
    self: {},
    screen: { width: 1920, height: 1080 },
    setTimeout, clearTimeout, setInterval, clearInterval,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.top = ctx;
  ctx.parent = ctx;
  vm.createContext(ctx);
  return ctx;
}

let ctx;
try {
  ctx = makeContext();
  const code = fs.readFileSync(signPath, "utf8");
  vm.runInContext(code, ctx, { timeout: 30000, filename: signPath });
} catch (err) {
  process.stderr.write("LOAD_FAILED " + String(err && err.message) + "\n");
  process.exit(2);
}

if (typeof ctx.get_sign !== "function") {
  process.stderr.write("NO_GET_SIGN\n");
  process.exit(3);
}

// 预热：跑一次丢弃结果，把混淆代码里的惰性初始化提前触发
try { ctx.get_sign("00000000000000000000000000000000"); } catch (e) {}

process.stdout.write("READY\n");

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", (line) => {
  const md5 = line.trim();
  if (!md5) { return; }
  if (md5 === "__PING__") { process.stdout.write("PONG\n"); return; }
  if (md5 === "__EXIT__") { process.exit(0); }
  try {
    const out = ctx.get_sign(md5);
    process.stdout.write(String(out) + "\n");
  } catch (err) {
    process.stdout.write("ERR:" + String(err && err.message) + "\n");
  }
});
"""


class SignatureUnavailable(RuntimeError):
    """无法计算签名（缺 node / 缺 JS / 进程起不来）。"""


def find_node() -> Optional[str]:
    """找到 node 可执行文件；找不到返回 None。"""
    override = (
        os.environ.get(ENV_NODE, "") or os.environ.get(ENV_NODE_LEGACY, "")
    ).strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(override)
        if found:
            return found
        logger.warning("%s 指向的 node 不存在: %s", ENV_NODE, override)
    return shutil.which("node") or shutil.which("node.exe")


def find_sign_js() -> Optional[Path]:
    """找到 ``sign.js``。

    查找顺序：环境变量 → 包内 ``data/sign.js`` → 包目录下任意 ``sign.js``。
    """
    override = (
        os.environ.get(ENV_SIGN_JS, "") or os.environ.get(ENV_SIGN_JS_LEGACY, "")
    ).strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        logger.warning("%s 指向的文件不存在: %s", ENV_SIGN_JS, override)

    if _BUNDLED_SIGN_JS.is_file():
        return _BUNDLED_SIGN_JS

    # 兜底：包目录旁边放着的副本
    for pattern in ("sign.js", "data/sign.js"):
        for candidate in _PACKAGE_DATA.parent.glob(pattern):
            if candidate.is_file():
                return candidate
    return None


def build_md5_param(params: Dict[str, str]) -> str:
    """按协议顺序把 13 个字段拼成待 MD5 的字符串。

    缺失字段按空串参与拼接 —— 这是协议要求，不是容错。
    """
    return ",".join(f"{name}={params.get(name, '')}" for name in SIGN_MD5_FIELDS)


def md5_hex(text: str) -> str:
    """计算 MD5 十六进制串。"""
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def signature_available() -> bool:
    """当前环境能否计算签名（不实际启动进程，快）。"""
    return find_node() is not None and find_sign_js() is not None


def availability_report() -> Dict[str, object]:
    """给界面用的一句话诊断。"""
    node = find_node()
    sign_js = find_sign_js()
    reasons: List[str] = []
    if node is None:
        reasons.append("未找到 node（请安装 Node.js，或设置 %s）" % ENV_NODE)
    if sign_js is None:
        reasons.append("未找到 sign.js（请设置 %s）" % ENV_SIGN_JS)
    return {
        "available": not reasons,
        "node": node or "",
        "sign_js": str(sign_js) if sign_js else "",
        "reason": "；".join(reasons),
    }


class SignatureEngine:
    """常驻 Node 进程，把 ``md5`` 换成抖音的 ``signature``。

    线程安全：内部锁串行化 stdin/stdout 往返，抓取线程和界面线程都能调。
    """

    def __init__(
        self,
        sign_js: Optional[Path] = None,
        node: Optional[str] = None,
        user_agent: str = "",
        timeout: float = SIGN_TIMEOUT,
    ) -> None:
        self.sign_js = Path(sign_js) if sign_js else find_sign_js()
        self.node = node or find_node()
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        self.timeout = timeout

        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._failures = 0
        self._disabled = False
        self._calls = 0
        self._cache: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def disabled(self) -> bool:
        """连续失败太多次后被熔断。"""
        return self._disabled

    @property
    def calls(self) -> int:
        """成功计算次数。"""
        return self._calls

    def available(self) -> bool:
        """能否工作（尚未熔断 + 依赖齐全）。"""
        if self._disabled:
            return False
        return bool(self.node) and bool(self.sign_js and self.sign_js.is_file())

    def _spawn(self) -> subprocess.Popen:
        """启动常驻 Node 进程并等到 ``READY``。

        .. note::
           这里有两个曾经会让**整个抓取线程永久挂死**的坑：

            1. ``proc.stdout.readline()`` 是裸阻塞的。驱动脚本在写 ``READY``
               之前会先跑一次预热 ``ctx.get_sign(...)``，而 ``vm.runInContext``
               的 ``timeout: 30000`` **不覆盖**这次调用 —— sign.js 一旦卡住，
               ``readline()`` 就永远不返回，``stop()`` 也杀不掉它。
               改用 :func:`_readline_with_timeout`，超时就 kill 重建。
            2. ``stderr=subprocess.PIPE`` 启动成功后**从不读取**，node 往
               stderr 写满 64KB 管道缓冲就会阻塞在写调用上，表现同样是"卡住"。
               改成重定向到临时文件：既不会阻塞子进程，失败时也能读回最后
               几行错误给用户看。
        """
        if self.node is None:
            raise SignatureUnavailable("未找到 node 可执行文件")
        if self.sign_js is None or not self.sign_js.is_file():
            raise SignatureUnavailable("未找到 sign.js")

        try:
            stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - 临时目录不可写
            stderr_file = None

        try:
            proc = subprocess.Popen(
                [self.node, "-e", _DRIVER_JS, str(self.sign_js), self.user_agent],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file if stderr_file is not None else subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # 不让子进程继承我们的控制台信号组，否则 Ctrl+C 会杀掉它两次
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            if stderr_file is not None:
                stderr_file.close()
            raise

        # 把临时文件挂到进程对象上，方便失败时读回 stderr（close() 里一并关掉）
        proc._danmaku_stderr = stderr_file  # type: ignore[attr-defined]

        line = _readline_with_timeout(proc, self.timeout)
        if line is not None and line.strip() == "READY":
            logger.info("抖音签名进程已就绪（node=%s, sign.js=%s）", self.node, self.sign_js)
            return proc

        detail = ""
        if line is None:
            detail = f"（等待 {self.timeout}s 未收到 READY，进程已终止）"
        if stderr_file is not None:
            try:
                stderr_file.seek(0)
                detail += stderr_file.read(600)
            except Exception:  # pragma: no cover
                pass
        try:
            proc.kill()
        except Exception:  # pragma: no cover
            pass
        _close_proc_stderr(proc)
        raise SignatureUnavailable(
            f"sign.js 加载失败（node 输出 {(line or '').strip()!r}）：{detail.strip()}"
        )

    def start(self) -> bool:
        """显式预热。返回是否成功。

        显式预热会把熔断复位：熔断只该拦住"自动重试"，不该拦住用户
        修好环境后的手动重来（否则用户装完 Node 必须重启整个程序）。
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
            self._disabled = False
            self._failures = 0
            if not self.available():
                return False
            try:
                self._proc = self._spawn()
                return True
            except SignatureUnavailable as exc:
                logger.warning("签名进程启动失败: %s", exc)
                self._proc = None
                return False

    def close(self) -> None:
        """关掉常驻进程。可重复调用。

        顺手把 ``_disabled`` 熔断也复位：熔断的本意是"这次会话别再试了"，
        但 ``close()`` 是用户/生命周期显式要求释放资源，下次重新 ``start()``
        时应当给一次干净的机会（例如用户刚把 Node 装好）。
        """
        with self._lock:
            proc, self._proc = self._proc, None
            self._disabled = False
            self._failures = 0
            if proc is None:
                return
            try:
                if proc.stdin:
                    proc.stdin.write("__EXIT__\n")
                    proc.stdin.flush()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:  # pragma: no cover
                    pass
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # pragma: no cover
                    pass
            _close_proc_stderr(proc)

    def __enter__(self) -> "SignatureEngine":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 计算
    # ------------------------------------------------------------------

    def get_sign(self, md5_value: str) -> str:
        """把 MD5 十六进制串换成 ``signature``；失败返回空串。

        **不抛异常** —— 调用点在构建 URL 的关键路径上，签名拿不到时应该
        退化成"不带 signature 去连"，让服务端的真实错误暴露出来。
        """
        if not md5_value or self._disabled:
            return ""

        cached = self._cache.get(md5_value)
        if cached is not None:
            return cached

        with self._lock:
            if self._disabled:
                return ""
            cached = self._cache.get(md5_value)
            if cached is not None:
                return cached

            for attempt in (1, 2):
                try:
                    result = self._round_trip(md5_value)
                except SignatureUnavailable as exc:
                    logger.warning("签名计算失败（第 %d 次）: %s", attempt, exc)
                    # 只置 None 不收拾进程的话，每失败一次就漏一个 node 子进程。
                    proc, self._proc = self._proc, None
                    _discard_proc(proc)
                    continue
                if result:
                    self._failures = 0
                    self._calls += 1
                    # 签名带随机数、每次都不同，但同一 md5 短时间内可复用，
                    # 缓存能省掉重连时的重复计算。只留最近 64 条。
                    if len(self._cache) > 64:
                        self._cache.clear()
                    self._cache[md5_value] = result
                    return result
                break

            self._failures += 1
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._disabled = True
                logger.error(
                    "签名计算连续失败 %d 次，已停用（后续将不带 signature 连接，"
                    "抖音大概率会立即断开）",
                    self._failures,
                )
            return ""

    def _round_trip(self, md5_value: str) -> str:
        """一次 stdin/stdout 往返。调用方持锁。"""
        if self._proc is None or self._proc.poll() is not None:
            self._proc = self._spawn()

        proc = self._proc
        if proc.stdin is None or proc.stdout is None:  # pragma: no cover
            raise SignatureUnavailable("签名进程管道不可用")

        try:
            proc.stdin.write(md5_value + "\n")
            proc.stdin.flush()
        except Exception as exc:
            raise SignatureUnavailable(f"写入签名进程失败: {exc}") from exc

        line = _readline_with_timeout(proc, self.timeout)
        if line is None:
            raise SignatureUnavailable(f"等待签名超时（{self.timeout}s）")

        text = line.strip()
        if not text:
            raise SignatureUnavailable("签名进程返回空行")
        if text.startswith("ERR:"):
            raise SignatureUnavailable(f"get_sign 抛错: {text[4:]}")
        return text

    # ------------------------------------------------------------------
    # 便捷入口
    # ------------------------------------------------------------------

    def signature_for(self, params: Dict[str, str]) -> str:
        """从 WS 参数表直接算出 ``signature``。"""
        return self.get_sign(md5_hex(build_md5_param(params)))


def _readline_with_timeout(proc: subprocess.Popen, timeout: float) -> Optional[str]:
    """读一行，带超时；超时返回 None。

    ``subprocess`` 的管道没有超时读，所以放到线程里等，
    超时就让调用方把进程整个丢掉重建 —— 卡住的进程救不回来。
    """
    holder: List[Optional[str]] = [None]

    def _read() -> None:
        try:
            assert proc.stdout is not None
            holder[0] = proc.stdout.readline()
        except Exception:  # pragma: no cover
            holder[0] = None

    worker = threading.Thread(target=_read, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        try:
            proc.kill()
        except Exception:  # pragma: no cover
            pass
        return None
    return holder[0]


def _close_proc_stderr(proc: subprocess.Popen) -> None:
    """关掉挂在进程对象上的 stderr 临时文件（没有就静默跳过）。"""
    handle = getattr(proc, "_danmaku_stderr", None)
    if handle is None:
        return
    try:
        handle.close()
    except Exception:  # pragma: no cover
        pass
    try:
        del proc._danmaku_stderr  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass


def _discard_proc(proc: Optional[subprocess.Popen]) -> None:
    """把一条已经不可用的 node 进程彻底收拾掉。

    以前失败分支只做 ``self._proc = None``，进程本身既没 kill 也没 wait，
    连续失败 3 次就会留下 3 个僵尸 node（Windows 上是真实的孤儿进程）。
    """
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:  # pragma: no cover
        pass
    try:
        proc.wait(timeout=1)
    except Exception:  # pragma: no cover
        pass
    for stream in (proc.stdin, proc.stdout):
        try:
            if stream is not None:
                stream.close()
        except Exception:  # pragma: no cover
            pass
    _close_proc_stderr(proc)


# ----------------------------------------------------------------------
# 模块级默认引擎（进程内共享，避免每个抓取器都起一个 node）
# ----------------------------------------------------------------------

_default_engine: Optional[SignatureEngine] = None
_default_lock = threading.Lock()


def default_engine() -> SignatureEngine:
    """取得进程内共享的签名引擎（惰性创建，不自动启动）。"""
    global _default_engine
    with _default_lock:
        if _default_engine is None:
            _default_engine = SignatureEngine()
            atexit.register(_default_engine.close)
        return _default_engine


__all__ = [
    "ENV_NODE",
    "ENV_NODE_LEGACY",
    "ENV_SIGN_JS",
    "ENV_SIGN_JS_LEGACY",
    "MAX_CONSECUTIVE_FAILURES",
    "SIGN_MD5_FIELDS",
    "SIGN_TIMEOUT",
    "SignatureEngine",
    "SignatureUnavailable",
    "availability_report",
    "build_md5_param",
    "default_engine",
    "find_node",
    "find_sign_js",
    "md5_hex",
    "signature_available",
]
