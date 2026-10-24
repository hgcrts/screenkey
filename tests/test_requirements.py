"""依赖清单的准确性回归测试。

``requirements.txt`` 是给人看的，但它里面的**注释**是断言——写着"X 是 Y 的依赖"
就可能让后人去装一个根本不需要的包（参考项目就是这么多了个 ``betterproto``）。
所以这里把几条可验证的断言变成测试：

1. ``blivedm`` 的 ``requires_dist`` 里**没有** ``betterproto``，源码里也不 import 它；
2. ``requirements.txt`` 因此刻意不列 ``betterproto``；
3. ``blivedm`` 确实精确锁定 ``brotli==1.0.9``（README 里"要编译"那段的前提）；
4. ``requirements.txt`` 不把任何可选依赖写成必装。

全部离线，只读元数据与文件。
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"


@pytest.fixture(scope="module")
def requirements_text() -> str:
    return REQUIREMENTS.read_text(encoding="utf-8")


def _active_lines(text: str) -> list[str]:
    """去掉注释与空行后的真正依赖行。"""
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            out.append(stripped)
    return out


class TestBetterprotoIsNotNeeded:
    """``betterproto`` 曾被误标为 "blivedm 的依赖"。"""

    def test_requirements_does_not_list_betterproto(self, requirements_text):
        """清单里不能出现 betterproto（注释里解释原因是可以的）。"""
        active = _active_lines(requirements_text)
        offenders = [line for line in active if "betterproto" in line.lower()]
        assert not offenders, (
            f"requirements.txt 又列上了 betterproto：{offenders}。"
            "实测 blivedm 0.1.1 既不在 requires_dist 里声明它，也不 import 它。"
        )

    def test_blivedm_does_not_declare_betterproto(self):
        """实测元数据：blivedm 的依赖只有 aiohttp 与 brotli。"""
        try:
            dist = md.distribution("blivedm")
        except md.PackageNotFoundError:  # pragma: no cover
            pytest.skip("本环境没装 blivedm")

        requires = dist.requires or []
        joined = " ".join(requires).lower()
        assert "betterproto" not in joined, f"blivedm 竟然声明了 betterproto：{requires}"
        # 并且确实有这两条，否则说明这个包的元数据形态变了、本测试的前提失效
        assert any("aiohttp" in r.lower() for r in requires), requires
        assert any("brotli" in r.lower() for r in requires), requires

    def test_blivedm_source_does_not_import_betterproto(self):
        """真正的判据是源码：blivedm 的模块里不出现 `import betterproto`。"""
        try:
            import blivedm
        except ImportError:  # pragma: no cover
            pytest.skip("本环境没装 blivedm")

        pkg_dir = Path(blivedm.__file__).parent
        hits = []
        for path in sorted(pkg_dir.rglob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                token = line.strip()
                if token.startswith(("import ", "from ")) and "betterproto" in token:
                    hits.append(f"{path.name}:{lineno}: {token}")
        assert not hits, "blivedm 竟然 import 了 betterproto：\n" + "\n".join(hits)

    def test_nothing_installed_requires_betterproto(self):
        """反向验证：扫一遍所有已安装包，看谁把它列为依赖。"""
        importers = []
        for dist in md.distributions():
            name = dist.metadata["Name"] or "?"
            for req in dist.requires or []:
                if "betterproto" in req.lower():
                    importers.append(f"{name} -> {req}")
        assert not importers, (
            "以下包要求 betterproto，说明它确实是某条依赖链需要的，"
            "本项目的判断需要更新：\n" + "\n".join(importers)
        )


class TestBrotliPinIsReal:
    """README 里"Python 3.12 装 blivedm 要编译 brotli"的前提。"""

    def test_blivedm_pins_brotli_109(self):
        try:
            dist = md.distribution("blivedm")
        except md.PackageNotFoundError:  # pragma: no cover
            pytest.skip("本环境没装 blivedm")

        brotli_reqs = [r for r in (dist.requires or []) if "brotli" in r.lower()]
        assert brotli_reqs, "blivedm 不再依赖 brotli 了，README 那段需要重写"
        # 必须是精确锁定（==），这样"不能升级到 1.2.0"的结论才成立
        assert any("==" in r for r in brotli_reqs), (
            f"blivedm 对 brotli 不再是精确锁定：{brotli_reqs}；"
            "如果改成 >= 就可以直接升到 1.2.0，绕开 br 的做法可以重新评估"
        )
        assert any("1.0.9" in r for r in brotli_reqs), brotli_reqs

    def test_readme_mentions_the_compile_requirement(self):
        """README 必须提醒这一点，否则用户会在安装时被非预期编译卡住。"""
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        assert "brotli" in readme.lower(), "README 没提 brotli 的安装问题"
        # 必须明确说不要升到 1.2.0（否则与 blivedm 的锁定冲突）
        assert "1.2.0" in readme, "README 没说明「不要升到 1.2.0」"

    def test_requirements_mentions_the_compile_requirement(self, requirements_text):
        assert "1.0.9" in requirements_text, "requirements.txt 没提 brotli 1.0.9 的编译成本"


class TestOptionalDepsStayOptional:
    """可选依赖不能被写成必装。"""

    def test_playwright_and_pyautogui_are_commented_out(self, requirements_text):
        active = _active_lines(requirements_text)
        for name in ("playwright", "pyautogui"):
            offenders = [line for line in active if name in line.lower()]
            assert not offenders, f"{name} 被误设为必装依赖：{offenders}"

    def test_every_active_line_is_a_requirement_like_spec(self, requirements_text):
        """活动行必须长得像依赖声明（名字+可选版本约束），防手滑写成散文。"""
        pattern = re.compile(r"^[A-Za-z0-9_.\-]+\s*([<>=!~]=?.*)?$")
        for line in _active_lines(requirements_text):
            assert pattern.match(line), f"不像依赖声明：{line!r}"

    def test_python_version_assumption_documented(self, requirements_text):
        """README 建议"卡住就换 3.11"，这条建议依赖本机就是 3.12。"""
        assert sys.version_info[:2] == (3, 12), (
            "本测试前提是 Python 3.12；换版本后 README 里 brotli 那段需重新评估"
        )
