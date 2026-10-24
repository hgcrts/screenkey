"""筛选规则编辑器（``ui/filter_editor.py``）的离屏回归测试。

这里锁住四类**静默丢数据**的缺陷（都是实测复现过的）：

1. **点「取消」也会改配置** —— 旧实现 ``self._rules = list(engine.rules)``
   只复制列表，元素仍是引擎里的活对象；``_save_detail()`` 原地改字段，
   于是「取消」等于「保存」。必须深拷贝。
2. **切换规则行会丢正在输入的内容** —— ``_on_rule_selected`` 直接
   ``_reload_conditions``，没先收集旧行。
3. **加/删条件、加/删/移规则同样丢条件表输入**。
4. **``currentIndexChanged`` 传进来的 int 被当成行号** —— 改 A 规则的下拉框
   会把详情写到第 N 条规则上。

全部离屏（``QT_QPA_PLATFORM=offscreen``）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"


@pytest.fixture(scope="module")
def qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测界面")

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

    from danmaku_kb.ui.qt_env import PYQT5_AVAILABLE, configure_qt_environment

    assert PYQT5_AVAILABLE, "PyQt5 已装但探测为不可用，qt_env 的探测逻辑有问题"
    configure_qt_environment()

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def no_modals(monkeypatch):
    """``QMessageBox.warning`` 在离屏下是模态的，会把测试挂死。"""
    import danmaku_kb.ui.filter_editor as fe

    calls = []

    class _Recorder:
        @staticmethod
        def warning(parent, title, text):
            calls.append((title, text))
            return 0

    monkeypatch.setattr(fe, "QMessageBox", _Recorder)
    return calls


def _engine_with(*rules):
    from danmaku_kb.filters.rules import FilterEngine

    return FilterEngine(list(rules))


class TestCancelDoesNotMutate:
    def test_cancel_leaves_engine_untouched(self, qt):
        """在对话框里改名后点取消，引擎里的规则名不能变。"""
        from danmaku_kb.filters.rules import FilterAction, FilterRule
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(FilterRule(name="原名", action=FilterAction.ALLOW))
        dialog = FilterEditorDialog(engine)
        dialog.name_edit.setText("改过的名字")
        dialog._save_detail()

        assert engine.rules[0].name == "原名", "编辑过程中不得原地修改引擎的规则"
        dialog.reject()
        assert engine.rules[0].name == "原名"

    def test_cancel_leaves_conditions_untouched(self, qt):
        from danmaku_kb.filters.rules import (
            CompareOp,
            Condition,
            FieldMatch,
            FilterAction,
            FilterRule,
        )
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        rule = FilterRule(
            name="r",
            action=FilterAction.DROP,
            conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value="原值")],
        )
        engine = _engine_with(rule)
        dialog = FilterEditorDialog(engine)
        # 改条件表里的值
        widget = dialog.cond_table.cellWidget(0, 2)
        widget.setText("新值")
        dialog._flush_current_row()

        # 引擎里的活对象不能被碰
        assert engine.rules[0].conditions[0].value == "原值"
        # 对话框内的编辑结果收在 _conditions（_rules[row].conditions 要等 accept 才同步）
        assert dialog._conditions[0][0].value == "新值"
        # 点取消后，accept 路径没有走，引擎仍然干净
        dialog.reject()
        assert engine.rules[0].conditions[0].value == "原值"

    def test_engine_returns_independent_copy(self, qt):
        from danmaku_kb.filters.rules import FilterAction, FilterRule
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(FilterRule(name="a", action=FilterAction.ALLOW))
        dialog = FilterEditorDialog(engine)
        dialog.name_edit.setText("b")
        dialog._save_detail()
        built = dialog.engine()
        built.rules[0].name = "c"
        assert dialog._rules[0].name == "b", "engine() 不能把内部状态暴露出去"


class TestRowSwitchKeepsInput:
    def test_conditions_survive_row_switch(self, qt):
        """在第 0 行输入条件，切到第 1 行再切回来，输入还在。"""
        from danmaku_kb.filters.rules import (
            CompareOp,
            Condition,
            FieldMatch,
            FilterAction,
            FilterRule,
        )
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(
            FilterRule(
                name="一",
                action=FilterAction.DROP,
                conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value="")],
            ),
            FilterRule(name="二", action=FilterAction.ALLOW),
        )
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        dialog.cond_table.cellWidget(0, 2).setText("用户刚敲的")

        dialog.rule_table.selectRow(1)
        dialog.rule_table.selectRow(0)

        assert dialog.cond_table.cellWidget(0, 2).text() == "用户刚敲的"

    def test_rule_name_survives_row_switch(self, qt):
        from danmaku_kb.filters.rules import FilterAction, FilterRule
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(
            FilterRule(name="一", action=FilterAction.ALLOW),
            FilterRule(name="二", action=FilterAction.ALLOW),
        )
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        dialog.name_edit.setText("改过的")

        dialog.rule_table.selectRow(1)
        dialog.rule_table.selectRow(0)
        assert dialog.name_edit.text() == "改过的"

    def test_switch_does_not_write_into_wrong_rule(self, qt):
        from danmaku_kb.filters.rules import FilterAction, FilterRule
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(
            FilterRule(name="一", action=FilterAction.ALLOW),
            FilterRule(name="二", action=FilterAction.ALLOW),
        )
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        dialog.name_edit.setText("只改第一行")
        dialog.rule_table.selectRow(1)
        assert dialog._rules[1].name == "二", "切行不能把上一行的内容写进新行"


class TestStructuralEditsKeepInput:
    def test_add_condition_keeps_existing_input(self, qt):
        from danmaku_kb.filters.rules import (
            CompareOp,
            Condition,
            FieldMatch,
            FilterAction,
            FilterRule,
        )
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(
            FilterRule(
                name="r",
                action=FilterAction.DROP,
                conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value="")],
            )
        )
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        dialog.cond_table.cellWidget(0, 2).setText("第一个条件")
        dialog._add_condition()

        assert dialog.cond_table.rowCount() == 2
        assert dialog.cond_table.cellWidget(0, 2).text() == "第一个条件"

    def test_remove_condition_keeps_other_input(self, qt):
        from danmaku_kb.filters.rules import (
            CompareOp,
            Condition,
            FieldMatch,
            FilterAction,
            FilterRule,
        )
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(
            FilterRule(
                name="r",
                action=FilterAction.DROP,
                conditions=[
                    Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value=""),
                    Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value=""),
                ],
            )
        )
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        dialog.cond_table.cellWidget(0, 2).setText("要保留的")
        # 选中第二行并删掉它
        dialog.cond_table.selectRow(1)
        dialog._remove_condition()

        assert dialog.cond_table.rowCount() == 1
        assert dialog.cond_table.cellWidget(0, 2).text() == "要保留的"

    def test_add_rule_keeps_current_input(self, qt):
        from danmaku_kb.filters.rules import FilterAction, FilterRule
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(FilterRule(name="一", action=FilterAction.ALLOW))
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        dialog.name_edit.setText("改过的")
        dialog._add_rule()

        assert dialog._rules[0].name == "改过的"
        assert len(dialog._rules) == 2

    def test_move_rule_keeps_input(self, qt):
        from danmaku_kb.filters.rules import FilterAction, FilterRule
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(
            FilterRule(name="一", action=FilterAction.ALLOW),
            FilterRule(name="二", action=FilterAction.ALLOW),
        )
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        dialog.name_edit.setText("改过的一")
        dialog._move_rule(1)

        assert [r.name for r in dialog._rules] == ["二", "改过的一"]


class TestComboSignalsDoNotCorruptRows:
    def test_action_combo_change_only_touches_current_row(self, qt):
        """``currentIndexChanged`` 带一个 int；旧代码把它当成行号。"""
        from danmaku_kb.filters.rules import FilterAction, FilterRule
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(
            FilterRule(name="一", action=FilterAction.ALLOW),
            FilterRule(name="二", action=FilterAction.ALLOW),
        )
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        # 改成 DROP（下拉框索引 1，旧代码会把它当成"第 1 行"）
        for i in range(dialog.action_combo.count()):
            if dialog.action_combo.itemData(i) is FilterAction.DROP:
                dialog.action_combo.setCurrentIndex(i)
                break

        assert dialog._rules[0].action is FilterAction.DROP
        assert dialog._rules[1].action is FilterAction.ALLOW

    def test_match_mode_change_only_touches_current_row(self, qt):
        from danmaku_kb.filters.rules import FilterAction, FilterRule
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(
            FilterRule(name="一", action=FilterAction.ALLOW),
            FilterRule(name="二", action=FilterAction.ALLOW),
        )
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        dialog.match_mode_combo.setCurrentIndex(1)  # 「或」

        assert dialog._rules[0].match_any is True
        assert dialog._rules[1].match_any is False


class TestAcceptValidation:
    def test_bad_regex_blocks_accept(self, qt, no_modals):
        from danmaku_kb.filters.rules import (
            CompareOp,
            Condition,
            FieldMatch,
            FilterAction,
            FilterRule,
        )
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        engine = _engine_with(
            FilterRule(
                name="正则",
                action=FilterAction.DROP,
                conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.REGEX, value="(")],
            )
        )
        dialog = FilterEditorDialog(engine)
        dialog.rule_table.selectRow(0)
        dialog._on_accept()

        assert no_modals, "非法正则应弹提示"
        assert dialog.result() == 0, "校验失败时不能 accept"
        assert len(dialog._rules) == 1

    def test_valid_rules_accept(self, qt, no_modals):
        from danmaku_kb.filters.rules import FilterAction, FilterRule
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        dialog = FilterEditorDialog(_engine_with(FilterRule(name="ok", action=FilterAction.ALLOW)))
        dialog._on_accept()
        assert not no_modals
        assert dialog.result() == 1


class TestDefaults:
    def test_no_engine_uses_default_rules(self, qt):
        from danmaku_kb.filters.rules import FilterEngine
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        dialog = FilterEditorDialog(None)
        assert len(dialog._rules) == len(FilterEngine.default().rules)

    def test_default_rules_not_mutated_by_editing(self, qt):
        """没传 engine 时也不能改到 ``FilterEngine.default()`` 的共享实例。"""
        from danmaku_kb.filters.rules import FilterEngine
        from danmaku_kb.ui.filter_editor import FilterEditorDialog

        before = [r.name for r in FilterEngine.default().rules]
        dialog = FilterEditorDialog(None)
        dialog.name_edit.setText("污染")
        dialog._save_detail()
        after = [r.name for r in FilterEngine.default().rules]
        assert before == after
