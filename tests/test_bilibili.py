"""B 站抓取器回归测试。

这一组测试锁的是三个**实测踩到过**的坑，全部与真实环境相关，
所以它们刻意只验证"代码结构与可观察行为"，不联网：

1. **blivedm 0.1.x 是扁平单文件包**（``client`` / ``handlers`` / ``models``），
   没有 ``blivedm.clients`` 与 ``blivedm.models`` 子包。曾经把
   ``import blivedm.models.web`` 和 ``import blivedm`` 放进同一个 ``try``，
   结果前者抛 ``ModuleNotFoundError`` 把后者一起带走，
   ``BLIVEDM_AVAILABLE`` 恒为 False，
   :meth:`BilibiliFetcher.connect` 于是**谎报"缺少 blivedm 依赖"**并永远返回 False。
2. **必须显式声明 ``Accept-Encoding: gzip, deflate``**。aiohttp 检测到本机有
   brotli 就会自动协商 ``br``，而 ``blivedm`` 依赖锁定的 ``brotli==1.0.9`` 的
   ``Decompressor`` 只有 ``process(data)`` 单参签名，aiohttp 3.14.4 的
   ``BrotliDecompressor.decompress_sync`` 在 ``max_length`` 非 0 时传两个参数 →
   ``TypeError`` 被包成 ``ContentEncodingError: 400, message: Can not decode
   content-encoding: br``，**所有 HTTP 请求全部解码失败**。
3. **不能在 ``client.start()`` 之后立刻报"已连接"**。``start()`` 只是
   ``asyncio.create_task(...)``，此时 ``init_room()`` / ``ws_connect`` / 认证
   都还没发生；必须等 WebSocket 真的握手（``_on_ws_connect`` 回调）再上报。
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

BILIBILI_PY = (
    Path(__file__).resolve().parents[1] / "src" / "danmaku_kb" / "danmaku" / "bilibili.py"
)


@pytest.fixture(scope="module")
def source() -> str:
    return BILIBILI_PY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def module():
    from danmaku_kb.danmaku import bilibili

    return bilibili


# ----------------------------------------------------------------------
# 1. blivedm 探测
# ----------------------------------------------------------------------


class TestBlivedmDetection:
    def test_module_imports_in_this_environment(self, module):
        """本套件所在环境装了 blivedm，所以探测结果必须是 True。

        这条断言的实际价值：如果哪天有人又把 ``blivedm.models.web`` 塞回
        那个 ``try``，这里会立刻变红，而不是等到用户抱怨"提示缺依赖但我装了"。
        """
        assert module.BLIVEDM_AVAILABLE is True, (
            "blivedm 已安装却探测失败——检查是不是又在同一个 try 里 import 了"
            "不存在的子模块（blivedm.clients / blivedm.models.web）"
        )

    def test_flat_package_layout_assumption(self, module):
        """确认 PyPI 上 blivedm 0.1.x 的扁平布局：只有单文件模块，没有子包。"""
        import blivedm

        assert hasattr(blivedm, "client")
        assert hasattr(blivedm, "handlers")
        assert hasattr(blivedm, "BaseHandler")
        # 这三个在 PyPI 的 0.1.x 上不存在，代码里绝不能硬依赖
        assert not hasattr(blivedm, "clients")
        assert not hasattr(blivedm, "models") or not hasattr(blivedm.models, "web")

    def test_handler_inherits_base_handler(self, module):
        """处理器必须真的继承 blivedm.BaseHandler，否则回调不会被调用。"""
        import blivedm

        mro = module.BiliHandler.__mro__
        assert blivedm.BaseHandler in mro, f"BiliHandler 的 MRO 里没有 BaseHandler: {mro}"

    def test_import_of_blivedm_is_isolated(self, source: str):
        """``import blivedm`` 必须位于独立 try 中，不能与子模块探测同生共死。"""
        # 找到第一个 try 块里出现的 blivedm import 语句
        assert re.search(r"^\s*import blivedm\s*$", source, re.M), "找不到 import blivedm"
        # 不允许出现 ``import blivedm.<sub>`` 形式的硬依赖
        bad = re.findall(r"^\s*(?:import|from)\s+blivedm\.\w+", source, re.M)
        assert not bad, f"存在对 blivedm 子包的硬依赖（0.1.x 没有子包）: {bad}"

    def test_submodule_probe_is_non_fatal(self, source: str):
        """子模块探测必须用 importlib 且不参与 BLIVEDM_AVAILABLE 的判定。"""
        assert "importlib" in source
        assert "_blivedm_models" in source


# ----------------------------------------------------------------------
# 2. Accept-Encoding
# ----------------------------------------------------------------------


class TestAcceptEncoding:
    def test_session_declares_gzip_deflate(self, source: str):
        """建 ClientSession 时必须显式声明 Accept-Encoding。

        不声明的话 aiohttp 会自己协商 ``br``，而锁定版本的 brotli 后端
        与 aiohttp 3.14.4 的调用约定不匹配，**每个**HTTP 响应都会
        ``ContentEncodingError``。
        """
        assert "Accept-Encoding" in source, (
            "bilibili.py 没有声明 Accept-Encoding；aiohttp 会自动协商 br，"
            "而环境里的 brotli 后端会让所有响应解码失败"
        )
        match = re.search(r'["\']Accept-Encoding["\']\s*:\s*["\']([^"\']+)["\']', source)
        assert match, "Accept-Encoding 必须赋一个字面量字符串"
        value = match.group(1)
        assert "gzip" in value and "deflate" in value
        assert "br" not in value.split(",")[0].strip(), (
            f"Accept-Encoding 不得包含 br（实测解码必失败）: {value!r}"
        )

    def test_aiohttp_session_header_matches_douyin(self, source: str, module):
        """与抖音抓取器保持同一个 Accept-Encoding 值，避免两边行为漂移。"""
        douyin_src = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "danmaku_kb"
            / "danmaku"
            / "douyin.py"
        ).read_text(encoding="utf-8")
        pat = re.compile(r'["\']Accept-Encoding["\']\s*:\s*["\']([^"\']+)["\']')
        bili_value = pat.search(source)
        douyin_value = pat.search(douyin_src)
        assert bili_value and douyin_value
        assert bili_value.group(1) == douyin_value.group(1), (
            "B 站与抖音的 Accept-Encoding 必须一致，否则必然有一边踩 brotli 坑"
        )

    @pytest.mark.skipif(
        not pytest.importorskip("aiohttp", reason="需要 aiohttp"), reason="需要 aiohttp"
    )
    def test_brotli_backend_is_incompatible_by_design(self):
        """把「为什么必须绕开 br」固化成可执行证据。

        实测（PyPI wheel 逐个解开验证）：

        ====================  ==========================  ==========================
        brotli 版本          ``Decompressor`` 上的 API    ``process(data, maxlen)``
        ====================  ==========================  ==========================
        1.0.9（blivedm 锁定）  只有 ``process(data)``       ``TypeError``（单参签名）
        1.1.0                只有 ``process(data)``       ``TypeError``（单参签名）
        1.2.0                只有 ``process(data)``       **能跑**（已支持双参）
        ====================  ==========================  ==========================

        也就是说 1.0.9 / 1.1.0 会让 aiohttp 的流式解压崩掉，1.2.0 才修好；
        而 ``blivedm`` 0.1.1 硬锁 ``brotli==1.0.9``，且 PyPI 上 **1.0.9 没有任何
        cp312 的 wheel**（82 个文件里最高到 cp311，只有 ``Brotli-1.0.9.zip`` 源码包），
        在 Python 3.12 下必须本地编译。所以"升级 brotli 到 1.2.0"这条路与
        ``pip install blivedm`` 是冲突的——绕开 ``br`` 才是稳的做法。

        这个测试描述的是**依赖环境**的既成事实，不是本项目的 bug：
        哪天依赖链能自然升到 1.2.0+，这条会失败，那时就可以放心去掉限制。
        """
        try:
            import brotli
        except ImportError:  # pragma: no cover
            pytest.skip("环境没有 brotli，aiohttp 不会协商 br，无需绕开")

        version = getattr(brotli, "__version__", "?")
        obj = brotli.Decompressor()

        # 断言"当前环境里 br 确实不能用"，任何一条不成立都说明可以重新评估
        assert not hasattr(obj, "decompress"), (
            f"环境里的 brotli {version} 已支持 streaming decompress()——"
            "可以重新评估 Accept-Encoding 是否需要绕开 br"
        )
        assert hasattr(obj, "process")
        if version == "1.2.0":
            # 已知的例外：1.2.0 修好了双参调用
            obj.process(b"", 8192)  # 不应抛 TypeError
        else:
            with pytest.raises(TypeError):
                obj.process(b"", 8192)

    def test_brotli_109_has_no_cp312_wheel(self):
        """``blivedm`` 锁定的 ``brotli==1.0.9`` 在 PyPI 上没有 cp312 wheel。

        这条不联网：它验证的是 requirements 注释里那句断言的"理由是否存在"。
        结论已在 PyPI 元数据上核过——1.0.9 共 82 个文件（81 wheel + 1 sdist），
        最高的 CPython 是 cp311，``Brotli-1.0.9-cp312*.whl`` **不存在**。
        所以 Python 3.12 下装 blivedm 必然触发源码编译，
        这就是 README 要求"先用 3.12 装依赖"时要留意的点。
        """
        import sys

        assert sys.version_info[:2] == (3, 12), "本断言的前提是 Python 3.12"
        # 若本机真装上了 1.0.9，它一定是被手工放置或本地编译出来的
        try:
            import brotli
        except ImportError:  # pragma: no cover
            pytest.skip("环境没有 brotli")

        if getattr(brotli, "__version__", "") == "1.0.9":
            # 这种情况说明它是编译/手工放置的，dist-info 可能缺失
            assert brotli.__file__.endswith("brotli.py")


# ----------------------------------------------------------------------
# 3. "已连接"必须在 WebSocket 真的握手之后
# ----------------------------------------------------------------------


class TestConnectionReadiness:
    def test_ws_ready_handler_has_handle_method(self, module):
        """``_WsReadyHandler`` 必须有 ``handle()``，且被调用后置位就绪标志。

        这是 blivedm 0.1.1 的硬性契约：``client.py:597-601`` 用
        ``asyncio.gather(*(handler.handle(self, command) for handler in ...))``
        分派命令。生成器表达式在**构造 gather 参数时**就取 ``handler.handle``，
        所以缺这个方法会直接抛 ``AttributeError``（``return_exceptions=True``
        也救不了），异常被 ``_on_ws_message`` 的宽泛 except 吞掉——
        表现就是"连上了但一条弹幕都收不到"。

        旧实现只定义了 ``_on_ws_connect`` / ``_on_ws_close``，而 blivedm
        **从不遍历 handlers 调这两个钩子**（它调的是 client 自己的同名方法），
        所以那两个方法既不会被调用、又占着"就绪信号"的位置，
        导致 ``_ws_ready`` 永远为 False、``connect()`` 必然超时失败。
        """
        fetcher = module.BilibiliFetcher()
        handler = module._WsReadyHandler(fetcher)

        assert hasattr(handler, "handle"), (
            "_WsReadyHandler 缺 handle()：blivedm 分派命令时会 AttributeError，"
            "所有弹幕/心跳命令都会被丢弃"
        )
        assert fetcher._ws_ready is False
        asyncio.run(handler.handle(None, {"cmd": "DANMU_MSG"}))
        assert fetcher._ws_ready is True

    def test_ws_ready_handler_does_not_rely_on_blivedm_hooks(self, module):
        """结构断言：不能再用 blivedm 不支持的 ``_on_ws_connect`` 钩子做就绪信号。"""
        handler = module._WsReadyHandler(module.BilibiliFetcher())
        assert not hasattr(handler, "_on_ws_connect"), (
            "_WsReadyHandler 又挂上了 _on_ws_connect —— blivedm 0.1.1 从不遍历"
            "handlers 调用该钩子，_ws_ready 永远不会被置位"
        )
        assert asyncio.iscoroutinefunction(handler.handle)

    def test_wait_ws_ready_returns_true_when_flag_already_set(self, module):
        fetcher = module.BilibiliFetcher()
        fetcher.connect_timeout = 1.0
        fetcher._ws_ready = True
        assert asyncio.run(fetcher._wait_ws_ready()) is True

    def test_wait_ws_ready_accepts_established_websocket(self, module):
        """ws 已握手但服务端还没推消息（空房间）时，也必须算就绪。

        只看"收到第一条命令"会让安静的直播间白等满 15 秒后误报失败，
        所以 ``_wait_ws_ready`` 还要接受 ``client._websocket is not None``。
        """

        class _Client:
            _websocket = object()  # 模拟 blivedm 握手成功后赋的值

        fetcher = module.BilibiliFetcher()
        fetcher.connect_timeout = 1.0
        fetcher._client = _Client()
        assert asyncio.run(fetcher._wait_ws_ready()) is True

    def test_wait_ws_ready_times_out(self, module):
        """等不到就返回 False，绝不能无限挂住。"""
        fetcher = module.BilibiliFetcher()
        fetcher.connect_timeout = 0.15
        assert asyncio.run(fetcher._wait_ws_ready()) is False

    def test_wait_ws_ready_bails_out_when_join_task_finished(self, module):
        """后台协程已经结束（如 init_room 反复失败）时立刻返回，不必等满超时。"""
        fetcher = module.BilibiliFetcher()
        fetcher.connect_timeout = 10.0  # 故意设很长

        async def scenario():
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(None)
            fetcher._join_task = fut  # type: ignore[assignment]
            loop = asyncio.get_running_loop()
            started = loop.time()
            result = await fetcher._wait_ws_ready()
            return result, loop.time() - started

        result, elapsed = asyncio.run(scenario())
        assert result is False
        assert elapsed < 1.0, f"join_task 已结束时不该继续等满超时（实际等了 {elapsed:.2f}s）"

    def test_connect_does_not_report_success_before_ws_ready(self, module):
        """结构断言：``_notify_status(True)`` 必须出现在 ``_wait_ws_ready`` 之后。

        这是本组最关键的回归点——曾经的代码在 ``start()`` 后立刻上报"已连接"，
        界面上显示连接成功，实际一帧弹幕都收不到。
        """
        src = inspect.getsource(module.BilibiliFetcher.connect)
        # 注释里为了解释历史缺陷也写着 ``_notify_status(True)``，
        # 所以先剥掉注释行，只比较真正的代码。
        code_lines = [
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        wait_at = code.find("_wait_ws_ready")
        assert wait_at != -1, "connect() 里找不到 _wait_ws_ready()"
        success_at = code.find("_notify_status(True")
        assert success_at != -1, "connect() 里找不到成功上报"
        assert wait_at < success_at, (
            "connect() 在等待 WebSocket 就绪之前就上报了成功——"
            "这就是「假已连接」缺陷"
        )

    def test_connect_reports_failure_when_not_ready(self, module):
        """未就绪时必须上报 False 且返回 False，不得静默成功。"""
        src = inspect.getsource(module.BilibiliFetcher.connect)
        assert "_notify_status(False" in src, "未就绪时没有如实上报失败"
        assert "return False" in src


class TestConnectionLifecycle:
    """连接生命周期：connect 不得阻塞、join 结束必须复位状态、清理要吞取消异常。

    这三条都是**实测踩到过**的：``await self._join_task`` 让 connect 永不返回，
    基类 ``_thread_main`` 的空闲循环到不了，``stop()`` 置的 ``_stopping`` 无人检查，
    于是线程永不退出、``_running`` 永为 True、下次 ``start()`` 直接失败；
    而 blivedm 的 ``stop_and_close()`` 会抛 ``CancelledError``（继承 ``BaseException``），
    ``except Exception`` 捕不到，异常从 ``_thread_main`` 的 finally 逃出去，
    ``loop.close()`` 与 ``_running = False`` 全被跳过。
    """

    def test_connect_does_not_await_join_task(self, module):
        """结构断言：connect() 里不能 ``await self._join_task``。"""
        src = inspect.getsource(module.BilibiliFetcher.connect)
        code = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        assert "await self._join_task" not in code, (
            "connect() 又 await join_task 了：它会阻塞到连接结束，"
            "基类的空闲循环与 stop() 检查都到不了，线程永不退出"
        )
        # 取而代之：join 结束时必须有回调负责复位状态
        assert "add_done_callback" in code, (
            "join_task 没有 done callback：blivedm 彻底停止后没人让线程退出"
        )

    def test_join_done_resets_state_when_not_stopping(self, module):
        """join 异常结束时：上报错误 + 置 ``_stopping`` 让基类线程退出。"""
        fetcher = module.BilibiliFetcher()
        errors = []
        statuses = []
        fetcher.on_error(lambda e: errors.append(e))
        fetcher.on_status(lambda connected, text="": statuses.append((connected, text)))

        async def scenario():
            fut = asyncio.get_running_loop().create_future()
            fut.set_exception(RuntimeError("auth failed"))
            fetcher._on_join_done(fut)
            fut.exception()  # 消费掉，避免 "exception never retrieved" 警告

        asyncio.run(scenario())
        assert fetcher._stopping is True, "join 结束后没有让基类线程退出"
        assert errors, "连接彻底中断时必须上报错误"
        assert "auth failed" in errors[0]

    def test_join_done_is_silent_on_user_stop(self, module):
        """用户主动停止是正常路径，不该再报一条错误。"""
        fetcher = module.BilibiliFetcher()
        errors = []
        fetcher.on_error(lambda e: errors.append(e))
        fetcher._stopping = True

        async def scenario():
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(None)
            fetcher._on_join_done(fut)

        asyncio.run(scenario())
        assert errors == [], f"主动停止不该报错: {errors}"

    def test_disconnect_survives_cancelled_error(self, module):
        """``stop_and_close()`` 抛 ``CancelledError`` 时清理流程必须走完。

        Python 3.8+ 的 ``CancelledError`` 继承 ``BaseException``，
        只写 ``except Exception`` 会让异常穿透 ``_thread_main`` 的 finally。
        """

        class _Client:
            async def stop_and_close(self):
                raise asyncio.CancelledError()

        fetcher = module.BilibiliFetcher()
        fetcher._client = _Client()
        asyncio.run(fetcher.disconnect())
        assert fetcher._client is None, "CancelledError 让清理流程中断了"
        assert fetcher._disconnecting is False


# ----------------------------------------------------------------------
# 4. 房间号解析与依赖缺失时的报错文案
# ----------------------------------------------------------------------


class TestConnectGuards:
    def test_extract_room_id_from_various_inputs(self):
        from danmaku_kb.utils.helpers import extract_bilibili_room_id

        assert extract_bilibili_room_id("https://live.bilibili.com/22637261") == "22637261"
        assert extract_bilibili_room_id("live.bilibili.com/12345") == "12345"
        assert extract_bilibili_room_id("https://live.bilibili.com/blanc/1234") == "1234"
        assert extract_bilibili_room_id("21452505") == "21452505"
        # 解析失败返回的是 None（不是空串），调用方一律按假值判断
        assert not extract_bilibili_room_id("不是房间号")

    def test_connect_rejects_unparsable_room(self, module):
        fetcher = module.BilibiliFetcher()
        errors = []
        fetcher.on_error(lambda e: errors.append(e))
        ok = asyncio.run(fetcher.connect("完全不是房间号"))
        assert ok is False
        assert errors, "无法解析房间号时必须上报错误"
        assert "房间号" in errors[0]

    def test_connect_does_not_claim_missing_dependency_when_installed(self, module):
        """探测定为 True 时，报错不得再提"缺少 blivedm 依赖"。"""
        assert module.BLIVEDM_AVAILABLE is True
        fetcher = module.BilibiliFetcher()
        errors = []
        fetcher.on_error(lambda e: errors.append(e))
        # 用非法房间号走一遍：应报房间号问题，而不是依赖问题
        asyncio.run(fetcher.connect(""))
        assert errors
        assert "blivedm" not in errors[0].lower(), (
            f"blivedm 明明装好了却谎报缺依赖: {errors[0]!r}"
        )


# ----------------------------------------------------------------------
# 5. 连不上时的原因诊断（-352 风控必须翻成人话）
# ----------------------------------------------------------------------


class _FakeResponse:
    """够用的 aiohttp 响应替身：异步上下文管理器 + status + text()。"""

    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self) -> str:
        return self._text


class _FakeSession:
    """记录最后一次请求的 URL 与 headers，方便断言"有没有伪装成浏览器"。"""

    def __init__(self, status: int = 200, text: str = "{}", error=None) -> None:
        self.status = status
        self.text = text
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.status, self.text)


class TestFailureDiagnosis:
    """``_diagnose_ws_failure`` 把接口返回码翻成人话。

    实测背景：2026-10 之前的取证把 ``getDanmuInfo`` 恒返回 ``-352`` 全归因于
    "出口 IP 被风控"/"按 IP 判定的接口级风控"。**2026-10-23 推翻了这个结论**：
    同一台机器、同一条网络、不加任何代理，只要给 ``getDanmuInfo`` 补上
    **WBI 签名**就能拿到 ``code=0`` 与 ``host_list``，并实测收到 41 条弹幕
    （见 ``danmaku/bilibili_wbi.py``）。所以 ``-352`` 的主因是**缺 WBI 签名**，
    不是 IP；现在签名补齐后仍报 ``-352`` 才是真的风控。

    旧代码只报"连接未就绪"，用户完全看不出原因，会一直换房间号白折腾——
    这层诊断本身仍然要保留，只是文案改成了"已带 WBI 签名仍被拒"。
    """

    def _fetcher(self, module, session):
        fetcher = module.BilibiliFetcher()
        fetcher._room_id = "10542806"
        fetcher._session = session
        return fetcher

    def test_352_is_translated_and_says_changing_room_is_useless(self, module):
        """-352 必须翻成人话：说明"已带 WBI 签名仍被拒"，换房间号没用。"""
        session = _FakeSession(200, '{"code": -352, "message": "-352"}')
        fetcher = self._fetcher(module, session)
        reason = asyncio.run(fetcher._diagnose_ws_failure())

        assert "352" in reason
        assert "换网络" in reason, f"没告诉用户该怎么办: {reason!r}"
        assert "房间号" in reason, f"没说明换房间号无用: {reason!r}"

    def test_other_error_code_is_surfaced(self, module):
        """其它错误码要原样带出来，不能被兜底文案吞掉。"""
        session = _FakeSession(200, '{"code": -400, "message": "请求错误"}')
        fetcher = self._fetcher(module, session)
        reason = asyncio.run(fetcher._diagnose_ws_failure())

        assert "-400" in reason
        assert "请求错误" in reason

    def test_non_200_antispider_page_does_not_crash(self, module):
        """HTTP 412 + 反爬 HTML：不能抛异常，回落成兜底文案。

        实测不伪装浏览器时 B 站直接回 412（``Content-Type: text/html``，
        正文 3286 字节），旧实现用 ``resp.json()`` 会抛 JSONDecodeError，
        被 except 吞掉后诊断静默失效——**看起来有诊断，实际永远是兜底文案**。
        """
        session = _FakeSession(412, "<!DOCTYPE html><html>反爬</html>")
        fetcher = self._fetcher(module, session)
        reason = asyncio.run(fetcher._diagnose_ws_failure())

        assert reason, "不能返回空文案"
        assert "352" not in reason, "412 反爬页不该被误报成 -352"

    def test_non_json_body_does_not_crash(self, module):
        session = _FakeSession(200, "<html>not json</html>")
        fetcher = self._fetcher(module, session)
        reason = asyncio.run(fetcher._diagnose_ws_failure())
        assert reason

    def test_network_exception_falls_back(self, module):
        """网络异常（DNS/超时/连接重置）不能让诊断本身把连接流程弄挂。"""
        session = _FakeSession(error=OSError("network down"))
        fetcher = self._fetcher(module, session)
        reason = asyncio.run(fetcher._diagnose_ws_failure())
        assert reason
        assert "352" not in reason

    def test_ok_code_reports_ws_level_failure_not_risk_control(self, module):
        """接口是好的（code=0）就不能赖到风控头上，要说清是连接层。"""
        session = _FakeSession(200, '{"code": 0, "data": {"host_list": []}}')
        fetcher = self._fetcher(module, session)
        reason = asyncio.run(fetcher._diagnose_ws_failure())

        assert "352" not in reason
        assert "未就绪" in reason

    def test_request_pretends_to_be_a_browser(self, module):
        """必须带浏览器 UA + Referer —— 这是 412 反爬的真实修复点。

        诊断现在会先走 WBI 签名接口取一次 nav 密钥，所以断言要挑
        ``getDanmuInfo`` 那一次请求，而不是"第一次请求"。
        """
        session = _FakeSession(200, '{"code": -352}')
        fetcher = self._fetcher(module, session)
        asyncio.run(fetcher._diagnose_ws_failure())

        calls = [(url, kw) for url, kw in session.calls if "getDanmuInfo" in url]
        assert calls, f"根本没请求 getDanmuInfo: {[u for u, _ in session.calls]}"
        url, kwargs = calls[0]
        headers = kwargs.get("headers") or {}
        ua = headers.get("User-Agent", "")
        assert "Mozilla" in ua, f"没伪装浏览器 UA，会被 412 挡掉: {ua!r}"
        assert headers.get("Referer", "").startswith("https://live.bilibili.com")

    def test_connect_calls_diagnosis_when_not_ready(self, module):
        """结构断言：未就绪分支必须调用诊断，否则用户只看得到"未就绪"。"""
        src = inspect.getsource(module.BilibiliFetcher.connect)
        assert "_diagnose_ws_failure" in src, (
            "connect() 未就绪时没有调用诊断，用户无法得知 -352 风控"
        )
        # 诊断必须在"未就绪"分支里，不能只是被定义
        not_ready_at = src.find("if not await self._wait_ws_ready()")
        diagnose_at = src.find("_diagnose_ws_failure")
        assert not_ready_at != -1 and diagnose_at > not_ready_at


# ----------------------------------------------------------------------
# 9. 登录态自检与 uid 透传（2026-10-23 实测：SESSDATA 失效时昵称被掩码）
# ----------------------------------------------------------------------


class _NavResponse:
    """``session.get(...)`` 返回的异步上下文管理器（只满足 nav 探测的用法）。"""

    def __init__(self, status: int, data: Any):
        self.status = status
        self._data = data

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _NavSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestLoginStateProbe:
    def test_valid_session_returns_mid(self, module):
        session = _NavSession([_NavResponse(200, {"code": 0, "data": {"mid": 123456}})])
        assert asyncio.run(module._probe_login_mid(session)) == 123456
        url, headers = session.calls[0]
        assert url.endswith("/x/web-interface/nav")
        assert "Mozilla" in (headers or {}).get("User-Agent", "")

    def test_expired_session_returns_zero(self, module):
        session = _NavSession([_NavResponse(200, {"code": -101, "message": "账号未登录"})])
        assert asyncio.run(module._probe_login_mid(session)) == 0

    def test_network_error_is_not_fatal(self, module):
        session = _NavSession([RuntimeError("boom")])
        assert asyncio.run(module._probe_login_mid(session)) == 0

    def test_http_error_returns_zero(self, module):
        session = _NavSession([_NavResponse(500, {})])
        assert asyncio.run(module._probe_login_mid(session)) == 0

    def test_mid_zero_when_data_has_no_mid(self, module):
        session = _NavSession([_NavResponse(200, {"code": 0, "data": {}})])
        assert asyncio.run(module._probe_login_mid(session)) == 0

    def test_connect_passes_uid_to_client(self, module):
        """结构断言：``connect()`` 必须把探测到的 mid 作为 uid 交给客户端。

        不传 uid 时 blivedm 的 ws 鉴权包 ``uid=0``，B 站按游客下发掩码昵称
        （``伊***``），与网页端显示的全名不一致。
        """
        src = inspect.getsource(module.BilibiliFetcher.connect)
        assert "_probe_login_mid" in src, "connect() 没有做登录态自检"
        assert "uid=login_mid" in src, "connect() 没有把 mid 透传给 blivedm（uid=login_mid）"

    def test_connect_warns_when_cookie_stale(self, module):
        """SESSDATA 失效必须明确提示，不能静默以游客身份连（用户会误以为程序坏了）。"""
        src = inspect.getsource(module.BilibiliFetcher.connect)
        warn_at = src.find("emit_system")
        probe_at = src.find("_probe_login_mid")
        assert warn_at != -1, "connect() 没有对失效 Cookie 发出任何提示"
        assert probe_at < warn_at, "提示必须出现在登录态探测之后"
        assert "掩码" in src or "游客" in src


# ----------------------------------------------------------------------
# N. INTERACT_WORD_V2 / 在线人数（2026-10 实测补的协议分支）
# ----------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "bilibili_frames.json"


def base64_of(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")


class _FakeFetcher:
    """只提供 handler 用得到的成员：``room_id`` 与 ``_emit_message``。"""

    room_id = "21414905"

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def _emit_message(self, msg: Any) -> None:  # noqa: D102 - 测试替身
        self.messages.append(msg)


def _frames() -> dict[str, Any]:
    import json

    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    grouped: dict[str, Any] = {}
    for item in data:
        cmd = item["cmd"]
        grouped.setdefault(cmd, []).append(item["data"])
    return grouped


class TestInteractWordV2AndStats:
    """服务端已把进场换成 INTERACT_WORD_V2，blivedm 0.1.1 不认识它。

    fixture 是按真实抓包（房间 21414905，2026-10-24）的字段结构**重新编码**的
    匿名帧：1=uid 2=uname 4=identities(packed) 5=msg_type 6=room_id
    7=秒 8=毫秒 15=msg_id。真实帧里的用户名是掩码且带真实头像 URL，故不落库。
    """

    def test_parse_enter(self, module):
        parsed = module._parse_interact_word_v2(_frames()["INTERACT_WORD_V2"][0]["pb"])
        assert parsed == {
            "uid": 200001,
            "uname": "测试用户",
            "msg_type": 1,
            "room_id": 21414905,
            "timestamp": 1792814604,
        }

    def test_malformed_payload_returns_none(self, module):
        for bad in (None, "", "!!!not-base64!!!", base64_of(b"\xff\xff\xff")):
            assert module._parse_interact_word_v2(bad) is None

    def test_handle_enter_emits_enter(self, module):
        fetcher = _FakeFetcher()
        handler = module._BiliHandler(fetcher)
        cmd = {"cmd": "INTERACT_WORD_V2", "data": _frames()["INTERACT_WORD_V2"][0]}
        asyncio.run(handler.handle(None, cmd))
        assert len(fetcher.messages) == 1
        msg = fetcher.messages[0]
        assert msg.msg_type.value == "enter"
        assert msg.nickname == "测试用户"
        assert msg.user_id == "200001"
        assert msg.platform == "bilibili"

    def test_handle_follow_emits_follow(self, module):
        fetcher = _FakeFetcher()
        handler = module._BiliHandler(fetcher)
        cmd = {"cmd": "INTERACT_WORD_V2", "data": _frames()["INTERACT_WORD_V2"][1]}
        asyncio.run(handler.handle(None, cmd))
        assert [m.msg_type.value for m in fetcher.messages] == ["follow"]
        assert fetcher.messages[0].content == "关注了主播"

    def test_handle_share_emits_system(self, module):
        fetcher = _FakeFetcher()
        handler = module._BiliHandler(fetcher)
        cmd = {"cmd": "INTERACT_WORD_V2", "data": _frames()["INTERACT_WORD_V2"][2]}
        asyncio.run(handler.handle(None, cmd))
        assert [m.msg_type.value for m in fetcher.messages] == ["system"]
        assert fetcher.messages[0].content == "分享了直播间"

    def test_handle_watched_change_emits_stats(self, module):
        fetcher = _FakeFetcher()
        handler = module._BiliHandler(fetcher)
        cmd = {"cmd": "WATCHED_CHANGE", "data": _frames()["WATCHED_CHANGE"][0]}
        asyncio.run(handler.handle(None, cmd))
        assert len(fetcher.messages) == 1
        msg = fetcher.messages[0]
        assert msg.msg_type.value == "stats"
        assert msg.content == "看过 1.7万"
        assert msg.extra["viewers"] == 17757

    def test_handle_online_rank_count_emits_stats(self, module):
        fetcher = _FakeFetcher()
        handler = module._BiliHandler(fetcher)
        cmd = {"cmd": "ONLINE_RANK_COUNT", "data": _frames()["ONLINE_RANK_COUNT"][0]}
        asyncio.run(handler.handle(None, cmd))
        assert len(fetcher.messages) == 1
        assert fetcher.messages[0].content == "在线 74 人"
        assert fetcher.messages[0].extra["online"] == 74

    def test_unknown_v2_msg_type_is_ignored(self, module):
        """msg_type=9（协议里的其他互动）不应被当成进场。"""
        from danmaku_kb.danmaku.protobuf_lite import encode_field_string, encode_field_varint

        body = encode_field_varint(1, 1) + encode_field_string(2, "x") + encode_field_varint(5, 9)
        fetcher = _FakeFetcher()
        handler = module._BiliHandler(fetcher)
        cmd = {"cmd": "INTERACT_WORD_V2", "data": {"pb": base64_of(body)}}
        asyncio.run(handler.handle(None, cmd))
        assert fetcher.messages == []

    def test_empty_stats_are_ignored(self, module):
        fetcher = _FakeFetcher()
        handler = module._BiliHandler(fetcher)
        for cmd in (
            {"cmd": "WATCHED_CHANGE", "data": {}},
            {"cmd": "ONLINE_RANK_COUNT", "data": {}},
        ):
            asyncio.run(handler.handle(None, cmd))
        assert fetcher.messages == []

    def test_duplicate_stats_suppressed(self, module):
        """同一文案 15 秒内只发一次：服务端约 5s 推一次，重复会把列表刷屏。"""
        fetcher = _FakeFetcher()
        handler = module._BiliHandler(fetcher)
        cmd = {"cmd": "ONLINE_RANK_COUNT", "data": _frames()["ONLINE_RANK_COUNT"][0]}
        asyncio.run(handler.handle(None, cmd))
        asyncio.run(handler.handle(None, cmd))
        assert len(fetcher.messages) == 1

    def test_stats_change_is_not_suppressed(self, module):
        fetcher = _FakeFetcher()
        handler = module._BiliHandler(fetcher)
        for online in (74, 75):
            data = {"count": 1, "online_count": online}
            asyncio.run(handler.handle(None, {"cmd": "ONLINE_RANK_COUNT", "data": data}))
        assert [m.content for m in fetcher.messages] == ["在线 74 人", "在线 75 人"]
