"""按键选择框：可下拉选全部按键，也能手填，边打边筛。

为什么单独做一个控件
--------------------
映射编辑器原来的"目标"列是一个**可编辑的 QComboBox**：它确实列了所有键名，
但只有一串英文键名（``space``、``lshift``、``numpad5``…），既没有中文说明，
输入时下拉也不会跟着筛。用户的原话是"填写本地按键的方式太原始还是手动填写"
——长得像输入框，就得靠背键名。

这里把三件事一次做掉：

1. **下拉列出全部可选按键**，每项带中文说明（``space　空格（Space）``）；
2. **同一个框里仍能手填**任意键名，包括下拉里没有的写法（校验照旧在
   :mod:`danmaku_kb.ui.mapping_editor` 里做）；
3. **输入即筛选**：把 ``QCompleter`` 挂在同一个数据模型上，``MatchContains``
   让"名称"和"中文说明"都能搜（输入 ``空格`` 能筛出 ``space``），
   弹窗出现在输入框下方，视觉效果与"下拉自动同步"一致。

搜索规则本身放在 :func:`danmaku_kb.input.keycodes.search_key_names` 里（纯函数、
不依赖 Qt），这样筛选顺序能被离线测试逐条钉住，而不用去驱动真实 UI。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QComboBox, QCompleter, QWidget

from .theme import style_combo
from ..input.keycodes import (
    VK_CODES,
    all_key_names,
    is_valid_key_name,
    key_display,
    key_label,
    normalize_key_text,
    search_key_names,
)

#: 下拉一次最多显示多少行
_MAX_VISIBLE_ITEMS = 16


class KeyComboBox(QComboBox):
    """列出全部按键的可编辑下拉框，输入即筛选。

    对外只暴露三个方法（``current_key`` / ``set_key`` / ``matches_for``），
    读写语义与映射编辑器原来的 ``QComboBox`` 用法保持一致：

    - 从下拉或候选里选中 → 返回**键名**（``space``），不是显示文字；
    - 手填内容 → 原样返回，**不做 strip**：``" "``（单个空格）本身就是
      ``space`` 的合法键名，剥掉空白会让"空格键"映射永远保存不了；
    - 手填的内容如果正好等于某项的显示文字或某个按键的中文说明，也换算回键名。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        # 用户输入不应被自动追加成新条目：候选只是提示，键名合法性由保存时校验
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMaxVisibleItems(_MAX_VISIBLE_ITEMS)
        # 宽度交给统一策略：按键显示文字最长能到「左 Control　左 Ctrl（Left Ctrl）」，
        # 但这一列在表格里只有一格宽，所以按"最少可见字符数"给下限、其余交给布局。
        style_combo(self, 18)
        self.setToolTip(
            "可下拉选择，也可以直接输入；"
            "输入时会自动筛选相似的按键（支持中文名，如「空格」「左Shift」）"
        )

        self._names: List[str] = all_key_names()
        for name in self._names:
            # itemData 存真正的键名，itemText 存给人看的「键名　中文说明」
            self.addItem(key_display(name), name)

        # 「中文说明 ↦ 正名」：只在别名表里出现的写法（" "）也收进来，
        # 但同一条说明优先指向下拉里列出的正名 —— 手输「空格」应得 "space"，不是 " "。
        # 键用归一化文字（去空白、小写），这样「左Shift」也能对上「左 Shift」。
        canonical = set(self._names)
        self._label_to_name: Dict[str, str] = {}
        for name in VK_CODES:
            label = key_label(name)
            if not label:
                continue
            key = normalize_key_text(label)
            existing = self._label_to_name.get(key)
            if existing in canonical and name not in canonical:
                continue
            self._label_to_name[key] = name
        self._setup_completer()
        self.lineEdit().setPlaceholderText("输入或选择按键，如 space / 空格 / f1")
        self.setCurrentIndex(-1)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _setup_completer(self) -> None:
        """让输入框下的候选弹窗随输入内容筛选。"""

        completer = QCompleter(self.model(), self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        # MatchContains：不要求从开头匹配，"shift" 也能筛出 "lshift"
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setMaxVisibleItems(_MAX_VISIBLE_ITEMS)
        self.setCompleter(completer)
        self._completer = completer

    def _name_from_display(self, text: str) -> str:
        """把显示文字或中文说明换算回键名；换算不出来原样返回。"""

        if not text:
            return text
        for index in range(self.count()):
            if self.itemText(index) == text:
                data = self.itemData(index)
                if isinstance(data, str):
                    return data
        exact = self._label_to_name.get(normalize_key_text(text))
        if exact is not None:
            return exact
        # 中文说明常常只记住前半截（「回车」≟「回车 Enter」）。只有当候选的
        # **说明**唯一时才换算，避免"左"同时对上好几个键；enter/return 这类
        # 说明相同的别名算一个，取排在前面的那个。
        needle = normalize_key_text(text)
        if needle:
            hits = [
                name
                for name in self._names
                if normalize_key_text(key_label(name)).startswith(needle)
            ]
            if len({normalize_key_text(key_label(name)) for name in hits}) == 1:
                return hits[0]
        # 手填的键名只要合法就换成规范写法：搜索时空白不参与比较，
        # 所以用户照搜索结果手打 `page down` 也该存成 `pagedown`。
        collapsed = normalize_key_text(text)
        if collapsed and is_valid_key_name(collapsed):
            return collapsed
        return text

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def current_key(self) -> str:
        """当前按键名；手填内容原样返回（保留 ``" "`` 这种合法键名）。"""

        text = self.currentText()
        index = self.currentIndex()
        if 0 <= index < self.count() and text == self.itemText(index):
            # 只有「输入框内容仍是这一项」时才信 currentIndex：用户选中一项后
            # 接着改文字，索引不会跟着变（Qt 的行为），这时必须读文字，
            # 否则刚敲进去的内容会被静默丢掉。
            data = self.itemData(index)
            if isinstance(data, str) and data:
                return data
        return self._name_from_display(text)

    def set_key(self, name: object) -> None:
        """按按键名选中对应条目；找不到就把原文填进输入框（不报错）。"""

        text = "" if name is None else str(name)
        index = self.findData(text) if text else -1
        if index >= 0:
            self.setCurrentIndex(index)
            return
        self.setCurrentIndex(-1)
        self.setEditText(text)

    def matches_for(self, query: str, limit: int = 0) -> List[str]:
        """返回与 ``query`` 相似的按键名（与输入时的筛选同一套规则）。"""

        return search_key_names(query, limit=limit)

    def resolvable(self) -> bool:
        """当前内容是不是一个能用的键名（与保存时的校验口径一致）。"""

        return is_valid_key_name(self.current_key())
