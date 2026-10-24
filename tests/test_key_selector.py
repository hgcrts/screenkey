"""按键选择器（``input/keycodes.py`` 的按键目录 + ``ui/key_selector.py``）测试。

三组对象
--------
1. **按键目录**：``all_key_names()`` / ``key_label()`` / ``key_display()`` 是
   界面下拉的数据源。历史坑：``VK_CODES`` 里同时有 ``space`` 和 ``" "`` 两个
   别名，后者在下拉里显示成一个**空白项**，看起来像坏数据（而且真被选中过）。
   这里锁住"目录里不出现纯空白键名"，同时锁住手输 ``" "`` 仍然有效。

2. **搜索规则**：用户要求"填写的时候下面的可选也会自动同步与填写内容相似的"。
   排序规则（完全相等 → 前缀 → 键名包含 → 中文说明包含）放在纯函数
   ``search_key_names()`` 里，这里逐条钉住；中文名与去空白匹配是重点
   （输入「空格」要筛出 ``space``，输入「左Shift」要筛出 ``lshift``）。

3. **控件读写**：``KeyComboBox.current_key()`` 必须永远返回**键名**而不是
   显示文字；手填内容原样返回（不 ``strip``，否则空格键映射保存不了）。

全部离屏，不联网、不发真实按键。
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


def _keycodes():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from screenkey.input import keycodes

    return keycodes


# ----------------------------------------------------------------------
# 1. 按键目录
# ----------------------------------------------------------------------
class TestKeyCatalog:
    def test_names_are_sorted_and_not_empty(self):
        names = _keycodes().all_key_names()
        assert names, "按键目录不能为空"
        assert names == sorted(names)

    def test_no_blank_names(self):
        """``" "`` 是 ``space`` 的别名，留在下拉里会显示成空白项。"""

        names = _keycodes().all_key_names()
        assert all(name.strip() for name in names)
        assert " " not in names
        assert "space" in names

    def test_covers_every_kind_of_key(self):
        names = set(_keycodes().all_key_names())
        for expected in ("a", "z", "0", "9", "f1", "f12", "numpad0", "space", "lshift", "up"):
            assert expected in names, f"按键目录缺少 {expected}"

    def test_every_catalog_name_is_usable(self):
        """目录里列出来的键必须真能解析，否则用户选了也保存不了。"""

        keycodes = _keycodes()
        broken = [name for name in keycodes.all_key_names() if keycodes.resolve_key(name) is None]
        assert broken == []

    def test_labels(self):
        keycodes = _keycodes()
        assert keycodes.key_label("space") == "空格"
        assert keycodes.key_label(" ") == "空格"
        assert keycodes.key_label("lshift") == "左 Shift"
        assert keycodes.key_label("a") == "字母 A"
        assert keycodes.key_label("7") == "数字 7"
        assert keycodes.key_label("f1") == "F1 功能键"
        assert keycodes.key_label("numpad3") == "小键盘 3"
        assert keycodes.key_label("up") == "方向键 ↑"

    def test_unknown_label_is_empty(self):
        assert _keycodes().key_label("不存在的键") == ""
        assert _keycodes().key_label("") == ""

    def test_display_combines_name_and_label(self):
        keycodes = _keycodes()
        assert keycodes.key_display("space") == "space　空格"
        assert keycodes.key_display("a") == "a　字母 A"
        # 没有中文说明就只显示键名，不留一个空的「　」后缀
        assert keycodes.key_display("不存在的键") == "不存在的键"

    def test_normalize_for_search(self):
        keycodes = _keycodes()
        assert keycodes.normalize_key_text(" 左 Shift ") == "左shift"
        assert keycodes.normalize_key_text("SPACE") == "space"
        assert keycodes.normalize_key_text(None) == ""


# ----------------------------------------------------------------------
# 2. 搜索规则
# ----------------------------------------------------------------------
class TestSearchKeyNames:
    def test_empty_query_returns_everything(self):
        keycodes = _keycodes()
        assert keycodes.search_key_names("") == keycodes.all_key_names()
        assert keycodes.search_key_names("   ") == keycodes.all_key_names()

    def test_limit(self):
        keycodes = _keycodes()
        assert len(keycodes.search_key_names("", limit=5)) == 5
        assert len(keycodes.search_key_names("s", limit=3)) == 3

    def test_exact_match_comes_first(self):
        keycodes = _keycodes()
        result = keycodes.search_key_names("space")
        assert result[0] == "space"
        assert "backspace" in result  # 名字里也含 space，应排在后面

    def test_prefix_before_contains(self):
        result = _keycodes().search_key_names("shift")
        prefix = [name for name in result if name.startswith("shift")]
        contains = [name for name in result if "shift" in name and not name.startswith("shift")]
        assert result[: len(prefix)] == prefix
        assert contains, "应当也搜到 lshift / rshift 这类包含匹配"
        assert len(result) == len(prefix) + len(contains)

    def test_chinese_label_search(self):
        keycodes = _keycodes()
        assert keycodes.search_key_names("空")[0] == "space"
        assert "space" in keycodes.search_key_names("空格")

    def test_label_search_ignores_spaces(self):
        """「左Shift」没有空格，也要对得上「左 Shift」。"""

        assert "lshift" in _keycodes().search_key_names("左Shift")

    def test_case_insensitive(self):
        assert _keycodes().search_key_names("SPACE")[0] == "space"

    def test_no_match_is_empty(self):
        assert _keycodes().search_key_names("这个键不存在") == []

    def test_every_name_searchable_by_itself(self):
        keycodes = _keycodes()
        for name in keycodes.all_key_names():
            assert name in keycodes.search_key_names(name), f"{name} 搜不到自己"


# ----------------------------------------------------------------------
# 3. 控件读写
# ----------------------------------------------------------------------
class TestKeyComboBoxBasics:
    def test_lists_every_key_with_display_text(self, qt):
        from screenkey.input.keycodes import all_key_names, key_display
        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        names = all_key_names()
        assert box.count() == len(names)
        assert box.itemData(0) in names
        assert box.itemText(0) == key_display(box.itemData(0))
        assert "　" in box.itemText(0)

    def test_no_blank_item(self, qt):
        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        assert box.findData(" ") == -1
        assert all(box.itemText(i).strip() for i in range(box.count()))

    def test_set_and_read_canonical_key(self, qt):
        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        for name in ("space", "w", "up", "f1", "numpad5"):
            box.set_key(name)
            assert box.current_key() == name
            # 界面上显示的是「键名　中文说明」，不是裸键名
            assert box.currentText().startswith(name)

    def test_space_alias_survives(self, qt):
        """可用性回归：手输单个空格不能被 ``strip`` 吃掉。"""

        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        box.set_key(" ")
        assert box.current_key() == " "
        box.setEditText(" ")
        assert box.current_key() == " "

    def test_empty_key(self, qt):
        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        box.set_key("")
        assert box.current_key() == ""
        assert box.currentIndex() == -1

    def test_typed_chinese_label_maps_to_key_name(self, qt):
        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        box.setEditText("空格")
        assert box.current_key() == "space"
        box.setEditText("左Shift")
        assert box.current_key() == "lshift"

    def test_typed_display_text_maps_to_key_name(self, qt):
        from screenkey.ui.key_selector import KeyComboBox
        from screenkey.input.keycodes import key_display

        box = KeyComboBox()
        box.setEditText(key_display("enter"))
        assert box.current_key() == "enter"

    def test_ambiguous_label_prefix_is_not_guessed(self, qt):
        """「左」能对上左 Alt / 左 Ctrl / 左 Shift / 左 Win，不许瞎猜。"""

        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        box.setEditText("左")
        assert box.current_key() == "左"
        assert box.resolvable() is False

    def test_unknown_typing_is_preserved(self, qt):
        """手填是允许的：控件不擅自纠正，合法性由保存时校验。"""

        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        box.setEditText("随便写")
        assert box.current_key() == "随便写"
        assert box.resolvable() is False

    def test_resolvable_matches_save_time_validation(self, qt):
        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        box.set_key("f7")
        assert box.resolvable() is True
        box.setEditText("空格")  # 会被换算成 space，所以仍然可用
        assert box.resolvable() is True

    def test_spaced_typing_is_collapsed_to_canonical_name(self, qt):
        """搜索不区分空白，所以照着搜索结果手打的内容也必须能存下来。"""

        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        for typed, expected in (
            ("page down", "pagedown"),
            ("page_down", "page_down"),
            ("Page Up", "pageup"),
            ("pageup", "pageup"),
        ):
            box.setEditText(typed)
            assert box.current_key() == expected, typed
            assert box.resolvable() is True, typed

    def test_spaced_typing_is_accepted_though_popup_matches_literally(self, qt):
        """多词写法能保存，但弹窗候选是 Qt 的字面包含匹配——这是刻意的取舍。

        不想为了一个多词写法去改写用户输入，也不想自定义补全器：
        候选请敲 `page` / `pagedown` / `page_down`，而 `page down` 这种写法
        照样能存下来（`current_key()` 会按空白不敏感换算）。
        """

        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        box.setEditText("page down")
        assert box.current_key() == "pagedown"
        assert box.resolvable() is True

    def test_typing_filters_candidates_like_a_search_box(self, qt):
        """真实敲键路径下，候选确实随输入收窄（QCompleter 的 MatchContains 生效）。

        注意不能用 ``setEditText()`` 来断言：程序化改文本不会触发 Qt 内部的补全前缀
        更新，候选会一直是全量；必须用 ``QTest.keyClicks()`` 走真实按键事件。
        """

        from PyQt5.QtTest import QTest

        from screenkey.input.keycodes import all_key_names
        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        box.show()
        box.lineEdit().setFocus()

        QTest.keyClicks(box.lineEdit(), "pagedown")
        completer = box.completer()
        found = set()
        for row in range(completer.completionCount()):
            completer.setCurrentRow(row)
            found.add(completer.currentCompletion())
        assert found == {
            "page_down\u3000下翻页 PageDown",
            "pagedown\u3000下翻页 PageDown",
        }

        box.setEditText("")
        QTest.keyClicks(box.lineEdit(), "shi")
        narrowed = box.completer().completionCount()
        assert 0 < narrowed < len(all_key_names())
        box.close()

    def test_completer_filters_by_content(self, qt):
        """用户要求的是"输入时下拉自动同步相似内容"，靠 MatchContains 完成。"""

        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QCompleter

        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        completer = box.completer()
        assert isinstance(completer, QCompleter)
        assert completer.filterMode() == Qt.MatchContains
        assert completer.caseSensitivity() == Qt.CaseInsensitive
        assert completer.completionMode() == QCompleter.PopupCompletion
        # 候选来自同一个数据模型，下标与下拉项一一对应
        assert completer.model() is box.model()

    def test_matches_for_uses_shared_rules(self, qt):
        from screenkey.input.keycodes import search_key_names
        from screenkey.ui.key_selector import KeyComboBox

        box = KeyComboBox()
        for query in ("space", "空", "左Shift", ""):
            assert box.matches_for(query) == search_key_names(query)


# ----------------------------------------------------------------------
# 4. 与映射编辑器联动
# ----------------------------------------------------------------------
class _WarningRecorder:
    def __init__(self) -> None:
        self.calls = []

    def warning(self, parent, title, text):  # noqa: D401 - 与 Qt 签名对齐
        self.calls.append((title, text))
        return 0


@pytest.fixture
def no_modals(monkeypatch):
    import screenkey.ui.mapping_editor as me

    recorder = _WarningRecorder()
    monkeypatch.setattr(me, "QMessageBox", recorder)
    return recorder


def _key_editor():
    from screenkey.ui.mapping_editor import MappingEditor

    return MappingEditor(is_mouse=False)


class TestMappingEditorIntegration:
    def test_key_column_uses_the_selector(self, qt):
        from screenkey.input.mappings import KeyMapping
        from screenkey.ui import mapping_editor as me
        from screenkey.ui.key_selector import KeyComboBox

        editor = _key_editor()
        editor.load([KeyMapping(trigger="w", key="up")])
        widget = editor.table.cellWidget(0, me._COL_TARGET)
        assert isinstance(widget, KeyComboBox)

    def test_loaded_key_roundtrips(self, qt, no_modals):
        from screenkey.input.mappings import KeyMapping

        editor = _key_editor()
        editor.load(
            [
                KeyMapping(trigger="1", key="space"),
                KeyMapping(trigger="2", key="numpad5"),
                KeyMapping(trigger="3", key="lshift"),
            ]
        )
        assert [m.key for m in editor.collect()] == ["space", "numpad5", "lshift"]
        assert no_modals.calls == []

    def test_picking_from_dropdown_writes_key_name(self, qt, no_modals):
        from screenkey.input.mappings import KeyMapping
        from screenkey.ui import mapping_editor as me

        editor = _key_editor()
        editor.load([KeyMapping(trigger="w", key="up")])
        widget = editor.table.cellWidget(0, me._COL_TARGET)
        widget.set_key("f9")
        assert editor.collect()[0].key == "f9"
        assert no_modals.calls == []

    def test_typing_chinese_label_writes_key_name(self, qt, no_modals):
        from screenkey.input.mappings import KeyMapping
        from screenkey.ui import mapping_editor as me

        editor = _key_editor()
        editor.load([KeyMapping(trigger="w", key="up")])
        widget = editor.table.cellWidget(0, me._COL_TARGET)
        widget.setEditText("回车")
        assert editor.collect()[0].key == "enter"
        assert no_modals.calls == []

    def test_space_key_mapping_still_savable(self, qt, no_modals):
        """历史坑：``strip`` 把单个空格吃掉后，空格键映射永远保存不了。"""

        from screenkey.input.mappings import KeyMapping
        from screenkey.ui import mapping_editor as me

        editor = _key_editor()
        editor.load([KeyMapping(trigger="空格", key="space")])
        widget = editor.table.cellWidget(0, me._COL_TARGET)
        widget.set_key(" ")
        assert editor.collect()[0].key == " "
        assert no_modals.calls == []

    def test_unknown_typed_key_is_rejected_with_a_message(self, qt, no_modals):
        from screenkey.input.mappings import KeyMapping
        from screenkey.ui import mapping_editor as me

        editor = _key_editor()
        editor.load([KeyMapping(trigger="w", key="up")])
        editor.table.cellWidget(0, me._COL_TARGET).setEditText("随便写")
        assert editor.collect() is None
        assert [title for title, _ in no_modals.calls] == ["未知按键"]

    def test_mouse_editor_is_unaffected(self, qt, no_modals):
        from screenkey.input.mappings import MouseMapping
        from screenkey.ui.mapping_editor import MappingEditor
        from screenkey.ui.key_selector import KeyComboBox

        editor = MappingEditor(is_mouse=True)
        editor.load([MouseMapping(trigger="左键")])
        assert editor.table.cellWidget(0, 3) is not None
        assert not isinstance(editor.table.cellWidget(0, 3), KeyComboBox)
        assert editor.collect()[0].button.value == "left"
