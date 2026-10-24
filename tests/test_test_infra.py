"""测试基础设施自身的回归测试。

为什么需要这一层：本套件跑在受限沙箱里，而 pytest 的**内置** ``tmp_path``
用的是 ``mkdir(mode=0o700)``——本环境唯一不可访问的权限组合（详见
``conftest.py`` 里 ``tmp_path`` 的 docstring）。``conftest.py`` 因此用自管夹具
顶掉了内置实现。如果哪天有人把它删掉（或者给 fixture 传了 ``name=`` 导致
覆盖失效），受影响的**不是**这些测试而是其他所有用 ``tmp_path`` 的测试，
报错还长得像权限问题、完全看不出根因。

这里就把"覆盖真的生效"这件事锁住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestTmpPathOverride:
    def test_tmp_path_is_usable(self, tmp_path):
        """临时目录必须真的能写、能列、能删。"""

        assert tmp_path.is_dir()

        probe = tmp_path / "sub" / "probe.txt"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == "ok"
        assert list(tmp_path.iterdir())

        probe.unlink()
        probe.parent.rmdir()

    def test_tmp_path_is_self_managed_not_builtin(self, tmp_path):
        """确认拿到的是**自管**目录，而不是 pytest 内置的那个。

        两个判别特征一起用：位置在项目内的 ``.test_tmp`` 下（内置的在系统
        临时目录的 ``pytest-of-<user>`` 下），且不是 0700。
        """

        resolved = tmp_path.resolve()
        assert resolved.is_relative_to(PROJECT_ROOT.resolve()), (
            f"tmp_path={resolved} 不在项目内，说明自管夹具没生效"
        )
        assert ".test_tmp" in resolved.parts, (
            f"tmp_path={resolved} 不在 .test_tmp 下，说明跑的是内置实现"
        )

        # Windows 上 st_mode 的权限位无意义（一律 0o777），只在 POSIX 上校验
        import sys

        if sys.platform != "win32":
            assert (tmp_path.stat().st_mode & 0o777) != 0o700

    def test_tmp_path_is_isolated_between_tests(self, tmp_path):
        """每个测试拿到不同目录，不能互相污染。"""

        marker = tmp_path / "marker.txt"
        assert not marker.exists(), "临时目录被上一个测试污染了"
        marker.write_text("x", encoding="utf-8")

    def test_tmp_path_cleanup_does_not_raise(self, tmp_path):
        """即使删不掉也不能让测试失败（沙箱里有些目录删不掉）。"""

        # 只验证夹具本身不抛异常；真正的清理发生在 teardown
        assert tmp_path.exists()


class TestTmpPathFactory:
    def test_factory_makes_usable_dirs(self, tmp_path_factory):
        import sys

        base = tmp_path_factory.mktemp("factory")
        assert base.is_dir()
        assert ".test_tmp" in base.resolve().parts or sys.platform != "win32"

        probe = base / "a.txt"
        probe.write_text("ok", encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == "ok"

    def test_factory_getbasetemp(self, tmp_path_factory):
        base = tmp_path_factory.getbasetemp()
        assert base.is_dir()
        assert list(base.iterdir()) is not None  # 必须可枚举，否则清理会崩


class TestNorecursedirsCoversSandboxLeftovers:
    """裸跑 ``pytest`` 时不能去采集那些删不掉的残留目录。"""

    @pytest.mark.parametrize(
        "pattern",
        ["pytest-cache-files-*", "_probe*"],
    )
    def test_pattern_registered(self, pytestconfig, pattern):
        assert pattern in pytestconfig.getini("norecursedirs")

    def test_defaults_preserved(self, pytestconfig):
        """追加自定义模式不能把 pytest 的默认模式挤掉。"""

        patterns = pytestconfig.getini("norecursedirs")
        for default in ("*.egg", ".*", "build", "dist", "node_modules", "venv"):
            assert default in patterns, f"默认模式 {default} 丢了"


class TestOfflineGuarantee:
    """整套测试必须离线：不允许联网、不允许真的发按键。"""

    def test_engine_fixture_is_dry_run(self, engine):
        assert engine.dry_run is True

    def test_simulator_fixture_does_not_autostart(self, simulator):
        assert simulator.running is False
