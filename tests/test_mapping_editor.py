"""映射编辑器（``ui/mapping_editor.py``）的离屏单元测试。

两组回归对象
------------
1. **「上移 / 下移」会毁掉一条映射**：旧实现是
   ``_row_to_mapping(row)`` → ``removeRow(row)`` → ``insertRow(target)`` →
   ``_fill_row(target, mapping)``，而 ``_fill_row`` 在 ``insertRow`` **之后**
   读 ``cellWidget``，拿到的全是 ``None``（``insertRow`` 建出的是空行），
   于是一个字段都没写回去 —— 点一次上移就丢一条映射并留下一个空行，
   用户再点保存必然被"触发词不能为空"拦下。这里锁住"换顺序不丢字段"。

2. **「匹配方式」列不存在**：``_MATCH_MODE_LABELS`` 定义了五种模式的显示名，
   但表格里从来没有对应控件，用户在界面上改不了 ``match_mode``，
   只能手改 ``settings.json``。这里锁住第 2 列的存在、显示与回读。

全部离屏（``QT_QPA_PLATFORM=offscreen``），并且**必须** monkeypatch 掉
``QMessageBox.warning``：离屏下它是模态的，会直接把测试挂住。
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
    """离屏 QApplication；没有 PyQt5 就跳过整组。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测界面")

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

    from screenkey.ui.qt_env import PYQT5_AVAILABLE, configure_qt_environment

    assert PYQT5_AVAILABLE, "PyQt5 已装但探测为不可用，qt_env 的探测逻辑有问题"
    configure_qt_environment()

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class _WarningRecorder:
    """``QMessageBox`` 的替身：只记账，不弹窗。"""

    def __init__(self) -> None:
        self.calls = []

    def warning(self, parent, title, text):  # noqa: D401 - 与 Qt 签名对齐
        self.calls.append((title, text))
        return 0


@pytest.fixture
def no_modals(monkeypatch):
    """把 ``mapping_editor`` 里的 ``QMessageBox`` 换成记账替身。"""
    import screenkey.ui.mapping_editor as me

    recorder = _WarningRecorder()
    monkeypatch.setattr(me, "QMessageBox", recorder)
    return recorder


def _key_editor(qt):
    from screenkey.ui.mapping_editor import MappingEditor

    return MappingEditor(is_mouse=False)


def _mouse_editor(qt):
    from screenkey.ui.mapping_editor import MappingEditor

    return MappingEditor(is_mouse=True)


def _fields(m):
    """一条映射的全部可比字段（刻意含 ``match_mode``：旧实现正是丢了它）。"""
    return (
        getattr(m, "trigger", None),
        getattr(m, "key", None),
        getattr(m, "button", None),
        getattr(m, "action", None),
        getattr(m, "cooldown", None),
        getattr(m, "description", None),
        getattr(m, "match_mode", None),
        getattr(m, "enabled", None),
    )


def _collected(editor):
    out = editor.collect()
    assert out is not None, "collect() 不该返回 None"
    return [_fields(m) for m in out]


def _three_distinct():
    """三条**每个字段都不同**的键盘映射，任何字段丢失都会被看出来。"""
    from screenkey.input.mappings import KeyAction, KeyMapping, MatchMode

    return [
        KeyMapping(
            trigger="w", key="up", action=KeyAction.PRESS_AND_RELEASE,
            cooldown=0.5, description="第一条", match_mode=MatchMode.EXACT,
        ),
        KeyMapping(
            trigger="q", key="q", action=KeyAction.LONG_PRESS,
            cooldown=2.5, description="第二条", match_mode=MatchMode.CONTAINS,
        ),
        KeyMapping(
            trigger="e", key="e", action=KeyAction.PRESS,
            cooldown=1.25, description="第三条", match_mode=MatchMode.REGEX,
        ),
    ]


class TestLoad:
    def test_row_count_matches_input(self, qt):
        editor = _key_editor(qt)
        editor.load(_three_distinct())
        assert editor.table.rowCount() == 3
        assert editor.table.columnCount() == 8

    def test_empty_load_clears(self, qt):
        editor = _key_editor(qt)
        editor.load(_three_distinct())
        editor.load([])
        assert editor.table.rowCount() == 0


class TestMoveSelectedKeepsEveryField:
    """核心回归：上移 / 下移只换顺序，任何字段都不许丢。"""

    def test_move_down_swaps_without_losing_fields(self, qt, no_modals):
        before = _three_distinct()
        editor = _key_editor(qt)
        editor.load(before)
        original = _collected(editor)

        editor.table.selectRow(1)          # 第 1 行（第 2 条）
        editor.move_selected(1)            # 下移一位

        after = _collected(editor)
        assert editor.table.rowCount() == 3, "上移/下移不许改变行数"
        assert after == [original[0], original[2], original[1]]
        # 再明确比一遍关键字段，避免"顺序对了但字段没了"这类巧合
        assert after[1] == _fields(before[2])
        assert after[2] == _fields(before[1])
        assert no_modals.calls == []

    def test_move_up_swaps_without_losing_fields(self, qt, no_modals):
        before = _three_distinct()
        editor = _key_editor(qt)
        editor.load(before)
        original = _collected(editor)

        editor.table.selectRow(2)          # 末行上移
        editor.move_selected(-1)

        after = _collected(editor)
        assert after == [original[0], original[2], original[1]]
        assert after[1] == _fields(before[2])
        assert no_modals.calls == []

    def test_no_empty_row_is_left_behind(self, qt, no_modals):
        """旧实现会在表尾留一个全空行；这里逐列检查没有任何 ``None``。"""
        editor = _key_editor(qt)
        editor.load(_three_distinct())
        editor.table.selectRow(1)
        editor.move_selected(1)

        for row in range(editor.table.rowCount()):
            for col in range(editor.table.columnCount()):
                assert editor.table.cellWidget(row, col) is not None, (
                    f"第 {row + 1} 行第 {col} 列是空的 —— 移动时丢了控件"
                )

    def test_mouse_editor_also_moves(self, qt, no_modals):
        from screenkey.input.mappings import (
            MatchMode,
            MouseAction,
            MouseButton,
            MouseMapping,
        )

        editor = _mouse_editor(qt)
        editor.load([
            MouseMapping(trigger="左键", button=MouseButton.LEFT, action=MouseAction.CLICK),
            MouseMapping(
                trigger="上滚", button=MouseButton.MIDDLE, action=MouseAction.SCROLL_UP,
                cooldown=1.5, description="滚轮", match_mode=MatchMode.SENSING,
            ),
        ])
        original = _collected(editor)
        editor.table.selectRow(0)
        editor.move_selected(1)
        assert _collected(editor) == [original[1], original[0]]
        assert no_modals.calls == []


class TestMoveSelectedBoundaries:
    def test_first_row_up_is_noop(self, qt, no_modals):
        editor = _key_editor(qt)
        editor.load(_three_distinct())
        before = _collected(editor)

        editor.table.selectRow(0)
        editor.move_selected(-1)

        assert _collected(editor) == before
        assert editor.table.rowCount() == 3
        assert no_modals.calls == []

    def test_last_row_down_is_noop(self, qt, no_modals):
        editor = _key_editor(qt)
        editor.load(_three_distinct())
        before = _collected(editor)

        editor.table.selectRow(2)
        editor.move_selected(1)

        assert _collected(editor) == before
        assert no_modals.calls == []

    def test_no_selection_is_noop(self, qt, no_modals):
        editor = _key_editor(qt)
        editor.load(_three_distinct())
        before = _collected(editor)

        editor.table.clearSelection()
        editor.move_selected(1)
        editor.move_selected(-1)

        assert _collected(editor) == before
        assert no_modals.calls == []

    def test_multi_selection_is_noop(self, qt, no_modals):
        from PyQt5.QtWidgets import QAbstractItemView

        editor = _key_editor(qt)
        editor.load(_three_distinct())
        before = _collected(editor)

        editor.table.setSelectionMode(QAbstractItemView.MultiSelection)
        editor.table.selectRow(0)
        editor.table.selectRow(2)
        assert len({i.row() for i in editor.table.selectedIndexes()}) == 2

        editor.move_selected(1)
        assert _collected(editor) == before
        assert no_modals.calls == []


class TestDuplicateAndRemove:
    def test_duplicate_keeps_every_field(self, qt, no_modals):
        editor = _key_editor(qt)
        editor.load(_three_distinct())
        original = _collected(editor)

        editor.table.selectRow(1)
        editor.duplicate_selected()

        assert editor.table.rowCount() == 4
        after = _collected(editor)
        assert after == [original[0], original[1], original[2], original[1]]

    def test_remove_keeps_remaining_fields(self, qt, no_modals):
        editor = _key_editor(qt)
        editor.load(_three_distinct())
        original = _collected(editor)

        editor.table.selectRow(1)
        editor.remove_selected()

        assert editor.table.rowCount() == 2
        assert _collected(editor) == [original[0], original[2]]

    def test_remove_without_selection_is_noop(self, qt, no_modals):
        editor = _key_editor(qt)
        editor.load(_three_distinct())
        before = _collected(editor)

        editor.table.clearSelection()
        editor.remove_selected()

        assert _collected(editor) == before


class TestMatchModeColumn:
    """「匹配方式」必须真的在界面上、并且能读回来。"""

    def test_headers_are_the_eight_columns(self, qt):
        key_editor = _key_editor(qt)
        key_headers = [
            key_editor.table.horizontalHeaderItem(c).text()
            for c in range(key_editor.table.columnCount())
        ]
        assert key_headers == [
            "启用", "触发词", "匹配方式", "按键", "动作", "长按(秒)", "冷却(秒)", "说明",
        ]

        mouse_editor = _mouse_editor(qt)
        mouse_headers = [
            mouse_editor.table.horizontalHeaderItem(c).text()
            for c in range(mouse_editor.table.columnCount())
        ]
        assert mouse_headers == [
            "启用", "触发词", "匹配方式", "鼠标键", "动作", "长按(秒)", "冷却(秒)", "说明",
        ]

    def test_regex_is_shown_and_read_back(self, qt, no_modals):
        from screenkey.input.mappings import KeyMapping, MatchMode

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(
                trigger="^w+$", key="up", match_mode=MatchMode.REGEX,
                cooldown=0.75, description="正则行",
            )
        ])

        combo = editor.table.cellWidget(0, 2)
        assert combo is not None, "第 2 列必须是「匹配方式」下拉框"
        assert combo.currentText() == "正则"
        assert combo.currentData() is MatchMode.REGEX

        collected = editor.collect()
        assert collected is not None
        assert collected[0].match_mode is MatchMode.REGEX
        assert no_modals.calls == []

    def test_every_label_is_selectable(self, qt, no_modals):
        """五种模式都要出现在下拉框里（``_MATCH_MODE_LABELS`` 不再无人引用）。"""
        from screenkey.input.mappings import MatchMode

        editor = _key_editor(qt)
        editor.add_row()
        combo = editor.table.cellWidget(0, 2)
        labels = [combo.itemText(i) for i in range(combo.count())]
        assert labels == ["精确匹配", "包含", "开头是", "正则", "感应（重复字符）"]
        assert [combo.itemData(i) for i in range(combo.count())] == list(MatchMode)

    def test_mouse_editor_has_match_mode_too(self, qt, no_modals):
        from screenkey.input.mappings import MatchMode, MouseMapping

        editor = _mouse_editor(qt)
        editor.load([MouseMapping(trigger="上滚", match_mode=MatchMode.STARTWITH)])

        combo = editor.table.cellWidget(0, 2)
        assert combo.currentText() == "开头是"
        assert editor.collect()[0].match_mode is MatchMode.STARTWITH

    def test_missing_match_mode_falls_back_to_exact(self, qt, no_modals):
        """旧配置 / 非法值都要回落到 EXACT，而不是让这一行读不出来。"""

        class _Legacy:
            trigger = "w"
            key = "up"
            cooldown = 0.5
            description = "旧数据"
            enabled = True
            # 刻意**没有** match_mode 属性

        editor = _key_editor(qt)
        editor.load([_Legacy()])
        combo = editor.table.cellWidget(0, 2)
        assert combo.currentText() == "精确匹配"
        assert editor.collect()[0].match_mode.name == "EXACT"

    def test_illegal_match_mode_falls_back_to_exact(self, qt, no_modals):
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load([KeyMapping(trigger="w", key="up", match_mode="bogus")])
        combo = editor.table.cellWidget(0, 2)
        assert combo.currentText() == "精确匹配"
        assert editor.collect()[0].match_mode.name == "EXACT"

    def test_changing_combo_is_reflected_in_collect(self, qt, no_modals):
        from screenkey.input.mappings import KeyMapping, MatchMode

        editor = _key_editor(qt)
        editor.load([KeyMapping(trigger="w", key="up", match_mode=MatchMode.EXACT)])
        combo = editor.table.cellWidget(0, 2)
        combo.setCurrentIndex(3)  # 正则
        assert editor.collect()[0].match_mode is MatchMode.REGEX


class TestWarningBehaviour:
    """``collect()`` 照旧弹框，``move_selected`` 的预读必须安静。"""

    def test_collect_warns_on_empty_trigger(self, qt, no_modals):
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(trigger="w", key="up"),
            KeyMapping(trigger="", key="q"),   # 非法：触发词为空
        ])

        assert editor.collect() is None
        assert len(no_modals.calls) == 1
        assert no_modals.calls[0][0] == "触发词为空"

    def test_move_selected_never_warns(self, qt, no_modals):
        """别的行非法时上移/下移只是空操作，**一个**警告框都不许弹。"""
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(trigger="w", key="up"),
            KeyMapping(trigger="q", key="q"),
            KeyMapping(trigger="", key="e"),   # 非法行
        ])
        before = [editor.table.cellWidget(r, 1).text() for r in range(3)]

        editor.table.selectRow(0)
        editor.move_selected(1)
        editor.table.selectRow(1)
        editor.move_selected(-1)

        assert no_modals.calls == [], f"上移/下移弹了警告框：{no_modals.calls}"
        assert [editor.table.cellWidget(r, 1).text() for r in range(3)] == before

    def test_collect_warns_on_unknown_key(self, qt, no_modals):
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load([KeyMapping(trigger="w", key="definitely_not_a_key")])

        assert editor.collect() is None
        assert len(no_modals.calls) == 1
        assert no_modals.calls[0][0] == "未知按键"


# ======================================================================
# 「打开即保存」不许清空表格里没有控件的字段
# ======================================================================
#
# 表格有 8 列，而 ``BaseMapping`` 还有一批字段**界面上根本没有控件**：
# ``delay`` / ``enable_sensing`` / ``short_press_count`` / ``long_press_count`` /
# ``case_sensitive`` / ``use_scancode``，
# 鼠标另有 ``move_x`` / ``move_y`` / ``scroll_amount``。
#
# 旧 ``_row_to_mapping`` 是「用控件值 new 一个新对象」，只传当时那 7 列，
# 于是这些字段**静默回落成 dataclass 默认值**：用户打开「编辑映射…」，
# 什么都不改直接点「保存」，就把 21/25 条键盘映射的 ``enable_sensing``
# 从 ``True`` 清成 ``False``（鼠标 4/9 条同样）——「666666 → 长按 6」
# 这个主打功能整体失效，且没有任何提示。
#
# 下面这组测试锁死修复：``_row_to_mapping`` 必须以**原来那一行的对象**为底
# （``dataclasses.replace``），只覆盖真有控件的那几列，其余字段原样带过。
#
# 注意：``long_press_duration`` 后来有了「长按(秒)」列（可编辑），
# 所以它不再属于"隐藏字段"；对应测试在 :class:`TestHoldDurationColumn`。

#: 表格里没有控件、必须靠原始对象带过的**全部**字段（键鼠共用一张表；
#: 不适用的字段两侧都是 ``None``，比对结果自然相等）。
_HIDDEN_FIELDS = (
    "delay",
    "enable_sensing",
    "short_press_count",
    "long_press_count",
    "case_sensitive",
    "use_scancode",
    "move_x",
    "move_y",
    "scroll_amount",
)

#: 键盘隐藏字段（任务书点名的 6 个）。
_HIDDEN_KEY_FIELDS = _HIDDEN_FIELDS[:6]

#: 鼠标隐藏字段（任务书点名的 4 个）。
_HIDDEN_MOUSE_FIELDS = ("move_x", "move_y", "scroll_amount", "enable_sensing")


def _hidden(m, names=_HIDDEN_FIELDS):
    """一条映射的隐藏字段取值元组。"""
    return tuple(getattr(m, name, None) for name in names)


def _hidden_list(mappings, names=_HIDDEN_FIELDS):
    """逐条取隐藏字段 —— 刻意**不用**集合/计数比对：

    只比 ``sum(enable_sensing)`` 这类计数会让"第 3 条和第 7 条的值对调"
    这种错误蒙混过关，而"逐条相等"能抓住。
    """
    return [_hidden(m, names) for m in mappings]


def _nondefault_key_mappings():
    """三条**隐藏字段全部非默认**的键盘映射。

    ``default_key_mappings()`` 里除 ``enable_sensing`` 外基本都是默认值，
    光靠它测不出 ``short_press_count`` / ``use_scancode`` 之类的丢失，
    所以这里每条都给不同的非默认值。
    """
    from screenkey.input.mappings import KeyAction, KeyMapping, MatchMode

    return [
        KeyMapping(
            trigger="w", key="up", action=KeyAction.PRESS_AND_RELEASE,
            cooldown=0.5, description="第一条", match_mode=MatchMode.EXACT,
            delay=0.25, enable_sensing=True, short_press_count=2,
            long_press_count=9, long_press_duration=1.75,
            case_sensitive=True, use_scancode=True,
        ),
        KeyMapping(
            trigger="q", key="q", action=KeyAction.LONG_PRESS,
            cooldown=2.5, description="第二条", match_mode=MatchMode.CONTAINS,
            delay=1.5, enable_sensing=False, short_press_count=3,
            long_press_count=12, long_press_duration=0.125,
            case_sensitive=False, use_scancode=False,
        ),
        KeyMapping(
            trigger="e", key="e", action=KeyAction.PRESS,
            cooldown=1.25, description="第三条", match_mode=MatchMode.REGEX,
            delay=0.75, enable_sensing=True, short_press_count=5,
            long_press_count=20, long_press_duration=3.5,
            case_sensitive=True, use_scancode=True,
        ),
    ]


def _nondefault_mouse_mappings():
    """两条隐藏字段全部非默认的鼠标映射。"""
    from screenkey.input.mappings import (
        MatchMode,
        MouseAction,
        MouseButton,
        MouseMapping,
    )

    return [
        MouseMapping(
            trigger="左键", button=MouseButton.LEFT, action=MouseAction.CLICK,
            cooldown=0.5, description="鼠标一", match_mode=MatchMode.EXACT,
            delay=0.3, enable_sensing=True, short_press_count=4,
            long_press_count=11, long_press_duration=2.25,
            case_sensitive=True, move_x=-12, move_y=34, scroll_amount=7,
        ),
        MouseMapping(
            trigger="上滚", button=MouseButton.MIDDLE, action=MouseAction.SCROLL_UP,
            cooldown=1.5, description="鼠标二", match_mode=MatchMode.SENSING,
            delay=0.9, enable_sensing=False, short_press_count=6,
            long_press_count=15, long_press_duration=0.75,
            case_sensitive=False, move_x=56, move_y=-78, scroll_amount=9,
        ),
    ]


class TestOpenAndSaveKeepsHiddenFields:
    """核心回归：「打开编辑器 → 什么都不改 → 保存」不许改掉任何隐藏字段。"""

    def test_default_key_mappings_survive_roundtrip(self, qt, no_modals):
        """用**默认映射表**跑一遍真实的用户路径（任务书点名的 7 个字段）。"""
        from screenkey.input.mappings import default_key_mappings

        before = default_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)

        after = editor.collect()
        assert after is not None, "collect() 不该返回 None"
        assert len(after) == len(before)

        # 逐条比：不是比总条数，也不是比 enable_sensing 的计数
        assert _hidden_list(after, _HIDDEN_KEY_FIELDS) == _hidden_list(
            before, _HIDDEN_KEY_FIELDS
        ), "打开即保存把键盘隐藏字段改掉了"
        assert no_modals.calls == []

        # 感应功能（主打）必须整体保住，而不是"从 21 条清成 0 条"
        assert sum(1 for m in after if m.enable_sensing) == 21

    def test_default_mouse_mappings_survive_roundtrip(self, qt, no_modals):
        from screenkey.input.mappings import default_mouse_mappings

        before = default_mouse_mappings()
        editor = _mouse_editor(qt)
        editor.load(before)

        after = editor.collect()
        assert after is not None
        assert len(after) == len(before)
        assert _hidden_list(after, _HIDDEN_MOUSE_FIELDS) == _hidden_list(
            before, _HIDDEN_MOUSE_FIELDS
        ), "打开即保存把鼠标隐藏字段改掉了"
        assert no_modals.calls == []

    def test_nondefault_key_hidden_fields_survive_roundtrip(self, qt, no_modals):
        """每条隐藏字段都取**非默认值**：任何一项回落默认值都会被抓到。"""
        before = _nondefault_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)

        after = editor.collect()
        assert after is not None
        for i, (b, a) in enumerate(zip(before, after)):
            assert _hidden(a) == _hidden(b), f"第 {i + 1} 条隐藏字段被改掉了"

    def test_nondefault_mouse_hidden_fields_survive_roundtrip(self, qt, no_modals):
        before = _nondefault_mouse_mappings()
        editor = _mouse_editor(qt)
        editor.load(before)

        after = editor.collect()
        assert after is not None
        for i, (b, a) in enumerate(zip(before, after)):
            assert _hidden(a) == _hidden(b), f"第 {i + 1} 条隐藏字段被改掉了"

    def test_returned_objects_are_the_same_dataclass_type(self, qt, no_modals):
        """``dataclasses.replace`` 必须保持类型，不许换成别的类。"""
        from screenkey.input.mappings import KeyMapping, MouseMapping

        key_editor = _key_editor(qt)
        key_editor.load(_nondefault_key_mappings())
        keys = key_editor.collect()
        assert keys is not None
        for m in keys:
            assert type(m) is KeyMapping, f"键盘行返回了 {type(m).__name__}"

        mouse_editor = _mouse_editor(qt)
        mouse_editor.load(_nondefault_mouse_mappings())
        mouse = mouse_editor.collect()
        assert mouse is not None
        for m in mouse:
            assert type(m) is MouseMapping, f"鼠标行返回了 {type(m).__name__}"

    def test_visible_fields_still_roundtrip(self, qt, no_modals):
        """修过头变成"什么都不生效"同样不行：7 列必须照旧读得出来。"""
        before = _nondefault_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)

        after = editor.collect()
        assert after is not None
        for b, a in zip(before, after):
            assert a.trigger == b.trigger
            assert a.key == b.key
            assert a.action is b.action
            assert a.cooldown == b.cooldown
            assert a.description == b.description
            assert a.match_mode is b.match_mode
            assert a.enabled is b.enabled


class TestMoveSelectedKeepsHiddenFields:
    """回归点：上一个修复只保住了 7 列，隐藏字段仍会被上移/下移清掉。"""

    def test_move_down_keeps_hidden_fields(self, qt, no_modals):
        before = _nondefault_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)
        expect = _hidden_list(before)

        editor.table.selectRow(1)
        editor.move_selected(1)

        after = editor.collect()
        assert after is not None
        assert editor.table.rowCount() == 3
        # 逐条比（顺序已交换）
        assert _hidden_list(after) == [expect[0], expect[2], expect[1]]
        assert no_modals.calls == []

    def test_move_up_keeps_hidden_fields(self, qt, no_modals):
        before = _nondefault_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)
        expect = _hidden_list(before)

        editor.table.selectRow(2)
        editor.move_selected(-1)

        after = editor.collect()
        assert after is not None
        assert _hidden_list(after) == [expect[0], expect[2], expect[1]]
        assert no_modals.calls == []

    def test_move_keeps_hidden_fields_after_several_moves(self, qt, no_modals):
        """连续移动：隐藏字段必须始终跟着**它自己那条**走，不许串行。"""
        before = _nondefault_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)
        expect = {m.trigger: _hidden(m) for m in before}

        editor.table.selectRow(0)
        editor.move_selected(1)
        editor.table.selectRow(2)
        editor.move_selected(-1)
        editor.table.selectRow(0)
        editor.move_selected(1)

        after = editor.collect()
        assert after is not None
        assert {m.trigger: _hidden(m) for m in after} == expect
        assert no_modals.calls == []

    def test_mouse_move_keeps_hidden_fields(self, qt, no_modals):
        before = _nondefault_mouse_mappings()
        editor = _mouse_editor(qt)
        editor.load(before)
        expect = _hidden_list(before)

        editor.table.selectRow(0)
        editor.move_selected(1)

        after = editor.collect()
        assert after is not None
        assert _hidden_list(after) == [expect[1], expect[0]]
        assert no_modals.calls == []


class TestDuplicateKeepsHiddenFields:
    def test_duplicate_copy_keeps_hidden_fields(self, qt, no_modals):
        before = _nondefault_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)
        expect = _hidden_list(before)

        editor.table.selectRow(1)
        editor.duplicate_selected()

        assert editor.table.rowCount() == 4
        after = editor.collect()
        assert after is not None
        assert _hidden_list(after) == [expect[0], expect[1], expect[2], expect[1]]
        # 副本真的等于原条
        assert _hidden(after[3]) == _hidden(before[1])
        assert after[3].trigger == before[1].trigger
        assert no_modals.calls == []

    def test_duplicate_copy_is_independent(self, qt, no_modals):
        """改了副本的可见列，不许串到原条（deepcopy 的意义）。"""
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import MatchMode

        before = _nondefault_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)
        editor.table.selectRow(1)
        editor.duplicate_selected()

        combo = editor.table.cellWidget(3, me._COL_MATCH_MODE)   # 副本行的「匹配方式」
        combo.setCurrentIndex(combo.findData(MatchMode.STARTWITH))
        editor.table.cellWidget(3, me._COL_COOLDOWN).setValue(99.0)

        after = editor.collect()
        assert after is not None
        assert after[3].match_mode is MatchMode.STARTWITH
        assert after[3].cooldown == 99.0
        # 原条纹丝不动
        assert after[1].match_mode is before[1].match_mode
        assert after[1].cooldown == before[1].cooldown
        assert _hidden(after[3]) == _hidden(before[1])
        assert no_modals.calls == []

    def test_mouse_duplicate_keeps_hidden_fields(self, qt, no_modals):
        before = _nondefault_mouse_mappings()
        editor = _mouse_editor(qt)
        editor.load(before)
        expect = _hidden_list(before)

        editor.table.selectRow(0)
        editor.duplicate_selected()

        after = editor.collect()
        assert after is not None
        assert _hidden_list(after) == [expect[0], expect[1], expect[0]]
        assert no_modals.calls == []


class TestEditingVisibleColumnsDoesNotDisturbHidden:
    """改可见列：可见列**确实**变了，隐藏字段**一点不动**。"""

    def test_change_match_mode_and_cooldown_and_description(self, qt, no_modals):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import MatchMode

        before = _nondefault_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)
        expect = _hidden_list(before)

        combo = editor.table.cellWidget(1, me._COL_MATCH_MODE)   # 第 2 行的匹配方式
        combo.setCurrentIndex(combo.findData(MatchMode.STARTWITH))
        editor.table.cellWidget(1, me._COL_COOLDOWN).setValue(4.5)        # 冷却
        editor.table.cellWidget(1, me._COL_DESCRIPTION).setText("改过的说明")  # 说明

        after = editor.collect()
        assert after is not None
        # 可见列真的生效了
        assert after[1].match_mode is MatchMode.STARTWITH
        assert after[1].cooldown == 4.5
        assert after[1].description == "改过的说明"
        # 其它行不受影响
        assert after[0].match_mode is before[0].match_mode
        assert after[0].cooldown == before[0].cooldown
        assert after[0].description == before[0].description
        # 隐藏字段逐条不变
        assert _hidden_list(after) == expect
        assert no_modals.calls == []

    def test_toggle_enabled_and_trigger_keeps_hidden(self, qt, no_modals):
        from PyQt5.QtWidgets import QCheckBox

        before = _nondefault_key_mappings()
        editor = _key_editor(qt)
        editor.load(before)
        expect = _hidden_list(before)

        wrap = editor.table.cellWidget(0, 0)
        chk = wrap.findChild(QCheckBox)
        chk.setChecked(not before[0].enabled)
        editor.table.cellWidget(0, 1).setText("w,上,前")

        after = editor.collect()
        assert after is not None
        assert after[0].enabled is (not before[0].enabled)
        assert after[0].trigger == "w,上,前"
        assert _hidden_list(after) == expect
        assert no_modals.calls == []

    def test_mouse_change_action_keeps_hidden(self, qt, no_modals):
        from screenkey.input.mappings import MouseAction

        before = _nondefault_mouse_mappings()
        editor = _mouse_editor(qt)
        editor.load(before)
        expect = _hidden_list(before)

        combo = editor.table.cellWidget(0, 4)   # 动作
        combo.setCurrentIndex(combo.findData(MouseAction.MOVE))

        after = editor.collect()
        assert after is not None
        assert after[0].action is MouseAction.MOVE
        assert after[0].button is before[0].button
        assert _hidden_list(after) == expect
        assert no_modals.calls == []


class TestOriginalsAlignment:
    """``_originals`` 必须与表格行**始终**一一对应（行号 == 索引）。"""

    def _assert_aligned(self, editor):
        assert len(editor._originals) == editor.table.rowCount(), (
            f"_originals 有 {len(editor._originals)} 项，"
            f"表格有 {editor.table.rowCount()} 行 —— 已经错位"
        )

    def test_load_then_aligned(self, qt):
        editor = _key_editor(qt)
        editor.load(_nondefault_key_mappings())
        self._assert_aligned(editor)

    def test_add_row_then_aligned(self, qt):
        editor = _key_editor(qt)
        editor.load(_nondefault_key_mappings())
        editor.add_row()
        self._assert_aligned(editor)

    def test_duplicate_then_aligned(self, qt, no_modals):
        editor = _key_editor(qt)
        editor.load(_nondefault_key_mappings())
        editor.table.selectRow(1)
        editor.duplicate_selected()
        self._assert_aligned(editor)

    def test_remove_then_aligned(self, qt, no_modals):
        editor = _key_editor(qt)
        editor.load(_nondefault_key_mappings())
        editor.table.selectRow(1)
        editor.remove_selected()
        self._assert_aligned(editor)
        # 删的是中间那条：剩下的对象必须是第 1、3 条
        assert [m.trigger for m in editor._originals] == ["w", "e"]

    def test_load_again_resets_alignment(self, qt):
        editor = _key_editor(qt)
        editor.load(_nondefault_key_mappings())
        editor.load(_nondefault_mouse_mappings()[:1])
        self._assert_aligned(editor)

    def test_add_row_registers_new_object(self, qt):
        """``add_row`` 造的新映射也要进 ``_originals``，否则它后面那些行会错位。"""
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load(_nondefault_key_mappings())
        editor.add_row()
        assert isinstance(editor._originals[-1], KeyMapping)
        assert len(editor._originals) == 4

    def test_row_to_mapping_out_of_range_falls_back_without_raising(self, qt, no_modals):
        """越界必须安全回落成"新建对象"，**不许抛异常**。"""
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load([KeyMapping(trigger="w", key="up")])

        editor._originals = []                     # 模拟列表被清空
        got = editor._row_to_mapping(0)
        assert isinstance(got, KeyMapping), "回落路径必须仍能造出对象"
        assert got.trigger == "w" and got.key == "up"

        assert editor._row_to_mapping(99) is None  # 越界且触发词读不到 → None
        assert editor._row_to_mapping(-1) is None

    def test_originals_cleared_on_empty_load(self, qt):
        editor = _key_editor(qt)
        editor.load(_nondefault_key_mappings())
        editor.load([])
        assert editor._originals == []
        assert editor.table.rowCount() == 0

    def test_mouse_and_key_originals_do_not_cross(self, qt, no_modals):
        """类型不匹配的原始对象不许让 ``replace`` 抛 ``TypeError``。

        ``KeyMapping`` 有 ``key`` 没有 ``button``，``MouseMapping`` 反之；
        把鼠标对象塞进键盘表时 ``dataclasses.replace(original, key=...)``
        会抛 ``TypeError``。必须被 :meth:`_replace_original` 挡住并回落到
        "新建对象"：键盘表里 ``MouseMapping`` 没有 ``key`` 属性 → 按键列是空的
        → ``collect()`` 按既有规则弹「按键为空」返回 ``None``，
        **而不是**把 ``TypeError`` 冒到界面上。
        """
        from screenkey.input.mappings import KeyMapping, MouseMapping

        editor = _key_editor(qt)
        editor.load(_nondefault_mouse_mappings())   # 类型不匹配的原始对象

        # 直接验回落：类型不符时必须返回 None 交给调用方走新建路径
        assert editor._replace_original(
            editor._originals[0], KeyMapping, trigger="x", key="up"
        ) is None
        assert editor._replace_original(
            KeyMapping(trigger="w", key="up"), MouseMapping, trigger="x", button=None
        ) is None

        # 整表读取：安全返回 None（按键为空），不抛异常
        assert editor.collect() is None
        assert len(no_modals.calls) == 1
        assert no_modals.calls[0][0] == "按键为空"

    def test_replace_original_handles_non_dataclass(self, qt):
        """鸭子类型替身（非 dataclass）也要安全回落，不许抛 ``TypeError``。"""

        class _Legacy:
            trigger = "w"

        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        assert editor._replace_original(
            _Legacy(), KeyMapping, trigger="w", key="up"
        ) is None
        assert editor._replace_original(None, KeyMapping, trigger="w", key="up") is None

    def test_legacy_object_row_still_collects(self, qt, no_modals):
        """载入 ``_Legacy`` 替身时走回落路径，可见列照旧读得出来。"""

        class _Legacy:
            trigger = "w"
            key = "up"
            cooldown = 0.5
            description = "旧数据"
            enabled = True

        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load([_Legacy()])
        got = editor.collect()
        assert got is not None
        assert isinstance(got[0], KeyMapping)
        assert (got[0].trigger, got[0].key, got[0].description) == ("w", "up", "旧数据")
        assert no_modals.calls == []


# ======================================================================
# 「长按(秒)」列：动作选「长按」时到底按住多久
# ======================================================================
class TestHoldDurationColumn:
    """用户诉求：长按要能设置按多久（以前只有 dataclass 默认 0.5 秒，界面上改不了）。"""

    def test_default_hold_is_half_second(self, qt):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load([KeyMapping(trigger="w", key="up")])

        hold = editor.table.cellWidget(0, me._COL_HOLD)
        assert isinstance(hold, me.QDoubleSpinBox)
        assert hold.value() == pytest.approx(me.HOLD_DEFAULT)
        assert (me.HOLD_MIN, me.HOLD_MAX) == (0.05, 5.0)

    def test_hold_shown_from_config(self, qt):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS,
                       long_press_duration=1.75),
        ])

        hold = editor.table.cellWidget(0, me._COL_HOLD)
        assert hold.value() == pytest.approx(1.75)

    @staticmethod
    def _select_action(combo, action):
        """把「动作」下拉切到指定成员（``itemData`` 存的是枚举本身）。"""

        for i in range(combo.count()):
            if combo.itemData(i) == action:
                combo.setCurrentIndex(i)
                return
        raise AssertionError(f"动作下拉里没有 {action!r}")

    def test_hold_column_is_hidden_when_no_row_is_long_press(self, qt):
        """用户反馈：一列常驻「长按(秒)」很突兀，会让人以为别的动作也用它。"""

        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load([KeyMapping(trigger="w", key="up")])  # 动作默认是「按下并松开」

        assert editor.table.isColumnHidden(me._COL_HOLD) is True
        assert editor.table.cellWidget(0, me._COL_HOLD).isHidden() is True

    def test_hold_column_appears_after_choosing_long_press(self, qt):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        editor = _key_editor(qt)
        editor.load([KeyMapping(trigger="w", key="up")])
        combo = editor.table.cellWidget(0, me._COL_ACTION)
        hold = editor.table.cellWidget(0, me._COL_HOLD)

        self._select_action(combo, KeyAction.LONG_PRESS)
        assert editor.table.isColumnHidden(me._COL_HOLD) is False
        assert hold.isHidden() is False

        # 再切回别的动作：列与输入框一起收起
        self._select_action(combo, KeyAction.PRESS_AND_RELEASE)
        assert editor.table.isColumnHidden(me._COL_HOLD) is True
        assert hold.isHidden() is True

    def test_hold_box_hidden_only_for_non_long_press_rows(self, qt):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS),
            KeyMapping(trigger="w", key="up"),
        ])

        # 有一行是长按 → 整列出现；非长按那行仍然收着
        assert editor.table.isColumnHidden(me._COL_HOLD) is False
        assert editor.table.cellWidget(0, me._COL_HOLD).isHidden() is False
        assert editor.table.cellWidget(1, me._COL_HOLD).isHidden() is True

    def test_hold_value_survives_switching_action_away_and_back(self, qt, no_modals):
        """藏起来只是看不见，值必须留着 —— 否则切一下动作就把配置改了。"""

        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS,
                       long_press_duration=1.25),
        ])
        combo = editor.table.cellWidget(0, me._COL_ACTION)
        hold = editor.table.cellWidget(0, me._COL_HOLD)
        hold.setValue(3.5)

        self._select_action(combo, KeyAction.PRESS_AND_RELEASE)
        assert hold.isHidden() is True

        got = editor.collect()
        assert got is not None
        assert got[0].action is KeyAction.PRESS_AND_RELEASE
        assert got[0].long_press_duration == pytest.approx(3.5)
        assert no_modals.calls == []

        # 切回长按：值还在，不用重填
        self._select_action(combo, KeyAction.LONG_PRESS)
        assert hold.isHidden() is False
        assert hold.value() == pytest.approx(3.5)

    def test_removing_last_long_press_row_hides_the_column_again(self, qt):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        editor = _key_editor(qt)
        editor.load([KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS)])
        assert editor.table.isColumnHidden(me._COL_HOLD) is False

        editor.table.selectRow(0)
        editor.remove_selected()

        assert editor.table.rowCount() == 0
        assert editor.table.isColumnHidden(me._COL_HOLD) is True

    def test_mouse_hold_column_also_hidden_for_click(self, qt):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import MouseAction, MouseButton, MouseMapping

        editor = _mouse_editor(qt)
        editor.load([MouseMapping(trigger="点", button=MouseButton.LEFT)])
        assert editor.table.isColumnHidden(me._COL_HOLD) is True

        combo = editor.table.cellWidget(0, me._COL_ACTION)
        self._select_action(combo, MouseAction.LONG_PRESS)
        assert editor.table.isColumnHidden(me._COL_HOLD) is False
        assert editor.table.cellWidget(0, me._COL_HOLD).isHidden() is False

    def test_hold_is_clamped_on_load(self, qt):
        """配置里写了越界值（手改 JSON / 旧版本）时夹进可选范围，不许原样显示。"""

        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(trigger="w", key="up", long_press_duration=9.0),
            KeyMapping(trigger="e", key="e", long_press_duration=0.0),
        ])

        assert editor.table.cellWidget(0, me._COL_HOLD).value() == pytest.approx(me.HOLD_MAX)
        assert editor.table.cellWidget(1, me._COL_HOLD).value() == pytest.approx(me.HOLD_MIN)

    def test_editing_hold_is_collected(self, qt, no_modals):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS,
                       long_press_duration=0.5),
        ])

        editor.table.cellWidget(0, me._COL_HOLD).setValue(2.25)
        got = editor.collect()
        assert got is not None
        assert got[0].action is KeyAction.LONG_PRESS
        assert got[0].long_press_duration == pytest.approx(2.25)
        assert no_modals.calls == []

    def test_mouse_hold_column_works(self, qt, no_modals):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import MouseAction, MouseButton, MouseMapping

        editor = _mouse_editor(qt)
        editor.load([
            MouseMapping(trigger="按住", button=MouseButton.LEFT,
                         action=MouseAction.LONG_PRESS, long_press_duration=0.5),
        ])

        hold = editor.table.cellWidget(0, me._COL_HOLD)
        assert hold.value() == pytest.approx(0.5)
        hold.setValue(1.5)

        got = editor.collect()
        assert got is not None
        assert got[0].long_press_duration == pytest.approx(1.5)

    def test_hold_is_kept_for_non_long_press_actions(self, qt, no_modals):
        """动作不是「长按」时这个值也照旧存着：改回长按不用重新填。"""

        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(trigger="w", key="up", action=KeyAction.PRESS,
                       long_press_duration=3.5),
        ])

        got = editor.collect()
        assert got is not None
        assert got[0].action is KeyAction.PRESS
        assert got[0].long_press_duration == pytest.approx(3.5)
        assert editor.table.cellWidget(0, me._COL_HOLD).value() == pytest.approx(3.5)

    def test_hold_survives_json_roundtrip(self):
        """存盘走 ``to_dict`` / ``from_dict``，长按时长必须真的进得了 settings.json。"""

        from screenkey.input.mappings import KeyAction, KeyMapping

        original = KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS,
                              long_press_duration=1.25)
        payload = original.to_dict()
        assert payload["long_press_duration"] == pytest.approx(1.25)
        restored = KeyMapping.from_dict(payload)
        assert restored.long_press_duration == pytest.approx(1.25)
        assert restored.action is KeyAction.LONG_PRESS

    def test_coerce_hold_helper(self):
        import screenkey.ui.mapping_editor as me

        assert me._coerce_hold(None) == pytest.approx(me.HOLD_DEFAULT)
        assert me._coerce_hold("abc") == pytest.approx(me.HOLD_DEFAULT)
        assert me._coerce_hold(float("nan")) == pytest.approx(me.HOLD_DEFAULT)
        assert me._coerce_hold(-3) == pytest.approx(me.HOLD_MIN)
        assert me._coerce_hold(99) == pytest.approx(me.HOLD_MAX)
        assert me._coerce_hold("1.5") == pytest.approx(1.5)

    def test_missing_hold_widget_keeps_original_value(self, qt, no_modals):
        """控件缺失时回落到**原值**，不是默认 0.5——否则等于静默改配置。"""
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        editor = _key_editor(qt)
        editor.load([
            KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS,
                       long_press_duration=1.75),
        ])
        editor.table.removeCellWidget(0, me._COL_HOLD)

        got = editor.collect()
        assert got is not None
        assert got[0].long_press_duration == pytest.approx(1.75)

    def test_dialog_returns_edited_hold(self, qt, no_modals):
        """整条链路：编辑器对话框「确定」后拿到的是界面上设的时长。"""

        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        dialog = me.MappingEditorDialog(
            [KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS)],
            [],
        )
        dialog.key_editor.table.cellWidget(0, me._COL_HOLD).setValue(0.85)
        dialog._on_accept()

        keys, mouse = dialog.result_mappings()
        assert mouse == []
        assert len(keys) == 1
        assert keys[0].long_press_duration == pytest.approx(0.85)


# ======================================================================
# 「动作」的中文显示名：主窗口总表与编辑器共用一份文案
# ======================================================================
class TestActionLabel:
    """用户反馈：不进编辑器时，映射页「动作」列显示的是英文。

    以前总表直接写 ``m.action.value``（``press_and_release`` / ``click``），
    现在统一走 :func:`screenkey.ui.mapping_editor.action_label`。
    """

    def test_keyboard_actions(self, qt):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import KeyAction, KeyMapping

        assert me.action_label(KeyMapping(trigger="w", key="up")) == "按下并松开（点按）"
        assert me.action_label(
            KeyMapping(trigger="w", key="up", action=KeyAction.PRESS)
        ) == "按下（不松开）"
        assert me.action_label(
            KeyMapping(trigger="w", key="up", action=KeyAction.RELEASE)
        ) == "松开"
        assert me.action_label(
            KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS,
                       long_press_duration=1.25)
        ) == "长按 1.25s"

    def test_mouse_actions(self, qt):
        import screenkey.ui.mapping_editor as me
        from screenkey.input.mappings import MouseAction, MouseMapping

        assert me.action_label(MouseMapping(trigger="左键")) == "单击"
        assert me.action_label(
            MouseMapping(trigger="双击", action=MouseAction.DOUBLE_CLICK)
        ) == "双击"
        assert me.action_label(
            MouseMapping(trigger="上滚", action=MouseAction.SCROLL_UP)
        ) == "向上滚动"
        assert me.action_label(
            MouseMapping(trigger="按住", action=MouseAction.LONG_PRESS,
                         long_press_duration=0.8)
        ) == "长按 0.80s"

    def test_missing_action_returns_empty(self, qt):
        import screenkey.ui.mapping_editor as me

        assert me.action_label(object()) == ""

    def test_unlabelled_enum_falls_back_to_value(self, qt):
        """以后新增动作忘了写中文说明时，至少显示 value，而不是空白。"""

        import screenkey.ui.mapping_editor as me

        class _FakeAction:
            name = "SOMETHING_NEW"
            value = "something_new"

        class _FakeMapping:
            action = _FakeAction()
            key = "a"

        assert me.action_label(_FakeMapping()) == "something_new"

