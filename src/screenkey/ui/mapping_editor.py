"""映射编辑器对话框。

对照参考项目
------------
参考项目的映射编辑散在主窗口里：键盘映射用表格、鼠标映射用另一套控件，
字段校验各写一遍，且**鼠标映射从未被持久化**（保存时漏掉了它）。

这里把两者统一到一个对话框，用 ``QTabWidget`` 分页，共用同一套
"读取控件 → 构造 dataclass → 校验" 流程。

表格列（键盘与鼠标共用同一列序，仅第 3 列的表头文字不同）
--------------------------------------------------------
``0=启用 / 1=触发词 / 2=匹配方式 / 3=按键|鼠标键 / 4=动作 / 5=冷却(秒) / 6=说明``

"匹配方式"列是本文件的**唯一**写入 ``match_mode`` 的地方：早先它只存在于
数据层与提示文字里，界面上根本没有对应控件，用户只能手改 ``settings.json``。
"""

from __future__ import annotations

import copy
import dataclasses
from typing import List, Optional, Sequence, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..input.keycodes import is_valid_key_name
from .key_selector import KeyComboBox
from ..input.mappings import (
    KeyAction,
    KeyMapping,
    MatchMode,
    MouseAction,
    MouseButton,
    MouseMapping,
)
from ..utils.logging_setup import get_logger
from .theme import EmptyState, rich_paragraph, style_combo

logger = get_logger(__name__)

__all__ = ["MappingEditorDialog", "MappingEditor", "action_label"]

#: 表格列索引（唯一事实来源；``_append_mapping`` 与 ``_row_to_mapping`` 必须一致）。
_COL_ENABLED = 0
_COL_TRIGGER = 1
_COL_MATCH_MODE = 2
_COL_TARGET = 3
_COL_ACTION = 4
_COL_HOLD = 5
_COL_COOLDOWN = 6
_COL_DESCRIPTION = 7
_COLUMN_COUNT = 8

#: 「长按」按住时长的可选范围（秒）与精度。
#:
#: 下限 0.05 秒已经短到和普通点按没区别，上限 5 秒足够按住类操作；
#: 再长就成了「卡键」，真需要的话应该用「按下 / 松开」两个动作拆开写。
HOLD_MIN = 0.05
HOLD_MAX = 5.0
HOLD_STEP = 0.05
HOLD_DECIMALS = 2
HOLD_DEFAULT = 0.5


def _combo(items: Sequence[Tuple[str, object]], current: object = None) -> QComboBox:
    """用 ``(显示名, 值)`` 列表填一个下拉框。"""

    box = style_combo(QComboBox(), 10)
    for label, value in items:
        box.addItem(label, value)
    if current is not None:
        for i in range(box.count()):
            if box.itemData(i) == current:
                box.setCurrentIndex(i)
                break
    return box


def _enum_items(enum_cls, labels: Optional[dict] = None) -> List[Tuple[str, object]]:
    labels = labels or {}
    return [(labels.get(m.name, m.name), m) for m in enum_cls]


def _coerce_hold(value: object) -> float:
    """把「长按时长」夹到可选范围（缺失 / 非数字 / NaN 回落到默认 0.5 秒）。"""

    try:
        duration = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return HOLD_DEFAULT
    if not (duration == duration):  # NaN 自己不等于自己
        return HOLD_DEFAULT
    return max(HOLD_MIN, min(HOLD_MAX, duration))


_KEY_ACTION_LABELS = {
    "PRESS": "按下（不松开）",
    "RELEASE": "松开",
    "PRESS_AND_RELEASE": "按下并松开（点按）",
    "LONG_PRESS": "长按",
}
_MOUSE_ACTION_LABELS = {
    "CLICK": "单击",
    "DOUBLE_CLICK": "双击",
    "PRESS": "按下（不松开）",
    "RELEASE": "松开",
    "LONG_PRESS": "长按",
    "MOVE": "移动",
    "SCROLL_UP": "向上滚动",
    "SCROLL_DOWN": "向下滚动",
}
_MOUSE_BUTTON_LABELS = {"LEFT": "左键", "RIGHT": "右键", "MIDDLE": "中键"}
_MATCH_MODE_LABELS = {
    "EXACT": "精确匹配",
    "CONTAINS": "包含",
    "STARTWITH": "开头是",
    "REGEX": "正则",
    "SENSING": "感应（重复字符）",
}


def action_label(mapping: object) -> str:
    """一条映射的「动作」中文显示名（长按带上按住时长）。

    主窗口的只读总表与编辑器的下拉共用同一份文案。以前总表直接显示
    ``m.action.value``（``press_and_release`` / ``click`` 这种英文枚举值），
    用户看到的就是"映射界面的动作栏怎么是英文"。

    长按额外带出时长（``长按 1.25s``）：这个值在编辑器里可调，
    总览里只写「长按」的话用户看不出自己设的时长存没存下来。
    """

    action = getattr(mapping, "action", None)
    if action is None:
        return ""
    # 鼠标映射有 button 字段、键盘映射有 key 字段：用它区分该查哪张表。
    labels = _MOUSE_ACTION_LABELS if hasattr(mapping, "button") else _KEY_ACTION_LABELS
    name = str(getattr(action, "name", ""))
    text = labels.get(name) or str(getattr(action, "value", action))
    if name == "LONG_PRESS":
        try:
            duration = float(getattr(mapping, "long_press_duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        text = f"{text} {duration:.2f}s"
    return text


class MappingEditor(QWidget):
    """单张映射表 + 字段编辑区。"""

    def __init__(self, is_mouse: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.is_mouse = is_mouse
        #: 与表格行**一一对应**的原始映射对象（行号 == 索引）。
        #:
        #: 表格有 8 列，而 ``BaseMapping`` 还有 ``delay`` /
        #: ``enable_sensing`` / ``short_press_count`` / ``long_press_count`` /
        #: ``case_sensitive`` / ``use_scancode``
        #: （鼠标另有 ``move_x`` / ``move_y`` / ``scroll_amount``）**没有任何控件**。
        #: 只保留在内存里，:meth:`_row_to_mapping` 用 ``dataclasses.replace``
        #: 在它之上覆盖那 8 列，其余字段原样带过 —— 否则「打开即保存」
        #: 会把这些字段静默清成 dataclass 默认值（感应/长按整体失效）。
        self._originals: List[object] = []
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # 这段 6 行的说明是全对话框的"怎么用"，行距放开才读得下去（QSS 无 line-height）
        hint = QLabel(rich_paragraph(
            "触发词：弹幕内容。多个触发词用英文逗号分隔，例如 <b>w,上,前</b>。"
            "「匹配方式」决定怎么比对：精确匹配要求整条弹幕等于触发词，"
            "包含 / 开头是 / 正则 为常见的文本匹配，"
            "感应（重复字符）模式下写重复的字符，例如 <b>6</b> 表示弹幕里连续出现 6 就触发。"
            "动作选「长按」后，右边会出现「长按(秒)」列，填按住多久"
            "（感应模式判成长按时同样使用）。", 1.6))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, _COLUMN_COUNT, self)
        if self.is_mouse:
            self.table.setHorizontalHeaderLabels(
                ["启用", "触发词", "匹配方式", "鼠标键", "动作", "长按(秒)", "冷却(秒)", "说明"]
            )
        else:
            self.table.setHorizontalHeaderLabels(
                ["启用", "触发词", "匹配方式", "按键", "动作", "长按(秒)", "冷却(秒)", "说明"]
            )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        # 单元格里塞的是 27px 高的输入控件，而默认行高按字体度量算只有 18-20px；
        # 行高不固定时 Qt 按 sizeHint 折中 → 下拉框只露半截、数字框的上下箭头被裁、
        # 「space 空格（Space）」这种长键名被切。这是全应用最核心的编辑界面，
        # 钉死 30px（= 控件 27px + 上下各留 1.5px 呼吸），同时与 4px 栅格兼容。
        vh = self.table.verticalHeader()
        vh.setVisible(False)
        vh.setDefaultSectionSize(30)
        vh.setSectionResizeMode(QHeaderView.Fixed)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_COL_TRIGGER, QHeaderView.Stretch)
        header.setSectionResizeMode(_COL_DESCRIPTION, QHeaderView.Stretch)
        for col in (
            _COL_ENABLED,
            _COL_MATCH_MODE,
            _COL_TARGET,
            _COL_ACTION,
            _COL_HOLD,
            _COL_COOLDOWN,
        ):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        # 空态叠在表上：不占布局高度，切换时不推挤表与工具条
        self.empty_hint = EmptyState(
            self.table, "还没有映射规则。点「新建」加一条，或用「导入」载入现有配置。"
        )
        layout.addWidget(self.empty_hint, 1)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        add_btn = QPushButton("新建")
        add_btn.clicked.connect(self.add_row)
        bar.addWidget(add_btn)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self.remove_selected)
        bar.addWidget(del_btn)
        dup_btn = QPushButton("复制")
        dup_btn.clicked.connect(self.duplicate_selected)
        bar.addWidget(dup_btn)
        up_btn = QPushButton("上移")
        up_btn.clicked.connect(lambda: self.move_selected(-1))
        bar.addWidget(up_btn)
        down_btn = QPushButton("下移")
        down_btn.clicked.connect(lambda: self.move_selected(1))
        bar.addWidget(down_btn)
        bar.addStretch(1)
        layout.addLayout(bar)

    # --------------------------------------------------------------- 表格
    def _sync_empty_hint(self) -> None:
        """按行数切换空态提示。空表比任何提示都更需要一句"接下来做什么"。"""

        self.empty_hint.refresh()

    def load(self, mappings: Sequence[object]) -> None:
        self.table.setRowCount(0)
        self._originals = []
        for m in mappings:
            self._append_mapping(m)
        self._refresh_hold_column()
        self._sync_empty_hint()

    def _append_mapping(self, m: object) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        # 先登记原始对象再建控件：这样 ``len(self._originals) == rowCount()``
        # 这一不变量在 ``_append_mapping`` 的**任何**返回路径上都成立，
        # 行号与索引永远不会错位。
        self._originals.append(m)

        chk = QCheckBox()
        chk.setChecked(bool(getattr(m, "enabled", True)))
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setAlignment(Qt.AlignCenter)
        wl.addWidget(chk)
        self.table.setCellWidget(row, _COL_ENABLED, wrap)

        trigger = QLineEdit(str(getattr(m, "trigger", "")))
        trigger.setToolTip("可用英文逗号分隔多个触发词")
        self.table.setCellWidget(row, _COL_TRIGGER, trigger)

        mode_box = _combo(
            _enum_items(MatchMode, _MATCH_MODE_LABELS),
            MatchMode.coerce(getattr(m, "match_mode", None)),
        )
        mode_box.setToolTip("这一条映射怎么和弹幕内容比对")
        self.table.setCellWidget(row, _COL_MATCH_MODE, mode_box)

        if self.is_mouse:
            box = _combo(
                _enum_items(MouseButton, _MOUSE_BUTTON_LABELS),
                getattr(m, "button", MouseButton.LEFT),
            )
        else:
            # 按键选择器：下拉列出全部按键（带中文说明），输入时自动筛选，
            # 也能直接手填下拉里没有的写法。VK_CODES 里同时有 "space" 和 " "
            # 两个别名，后者在下拉里会显示成一个空白项，所以控件只列 "space"；
            # 手输 " " 依然有效（current_key() 不 strip）。
            box = KeyComboBox()
            box.set_key(getattr(m, "key", ""))
        self.table.setCellWidget(row, _COL_TARGET, box)

        if self.is_mouse:
            action_box = _combo(
                _enum_items(MouseAction, _MOUSE_ACTION_LABELS),
                getattr(m, "action", MouseAction.CLICK),
            )
        else:
            action_box = _combo(
                _enum_items(KeyAction, _KEY_ACTION_LABELS),
                getattr(m, "action", KeyAction.PRESS_AND_RELEASE),
            )
        self.table.setCellWidget(row, _COL_ACTION, action_box)
        # 换了动作就重算「长按(秒)」列：只在真的用得上时出现（见 _refresh_hold_column）
        action_box.currentIndexChanged.connect(self._refresh_hold_column)

        # 「长按」按住多久：动作选「长按」时按这个时长按住再松开；
        # 感应模式（重复字符）被判成长按时也用它 —— 所以值不随动作被清掉。
        # 可见性由 _refresh_hold_column 统一管：不是「长按」的行把它藏起来。
        hold = QDoubleSpinBox()
        hold.setRange(HOLD_MIN, HOLD_MAX)
        hold.setSingleStep(HOLD_STEP)
        hold.setDecimals(HOLD_DECIMALS)
        hold.setSuffix(" 秒")
        hold.setValue(_coerce_hold(getattr(m, "long_press_duration", None)))
        hold.setToolTip(
            f"「长按」动作按住多久（{HOLD_MIN:g}–{HOLD_MAX:g} 秒）。\n"
            "感应模式下重复次数达到「长按次数」时也按这个时长按住。\n"
            "其它动作（按下 / 松开 / 按下并松开）不使用这个值。"
        )
        self.table.setCellWidget(row, _COL_HOLD, hold)

        cd = QDoubleSpinBox()
        cd.setRange(0.0, 600.0)
        cd.setSingleStep(0.1)
        cd.setDecimals(2)
        cd.setValue(float(getattr(m, "cooldown", 0.5) or 0.0))
        self.table.setCellWidget(row, _COL_COOLDOWN, cd)

        desc = QLineEdit(str(getattr(m, "description", "")))
        self.table.setCellWidget(row, _COL_DESCRIPTION, desc)
        return row

    def _row_is_long_press(self, row: int) -> bool:
        """这一行的「动作」是不是「长按」。"""

        action_widget = self.table.cellWidget(row, _COL_ACTION)
        if not isinstance(action_widget, QComboBox):
            return False
        expected = MouseAction.LONG_PRESS if self.is_mouse else KeyAction.LONG_PRESS
        return action_widget.currentData() is expected

    def _refresh_hold_column(self, *_args: object) -> None:
        """让「长按(秒)」列只在真的用得上时出现。

        用户原话：「前面选了长按后面再出来，现在有点突兀而且会让人误会」——
        一列常驻会让选「按下 / 松开 / 按下并松开」的人以为它对自己也生效。
        所以分两层收：

        * **单行**：动作不是「长按」时隐藏该行的输入框，但**值仍然保留**
          （切回「长按」不用重填，也不丢用户在 JSON 里手写好的时长）；
        * **整列**：全表没有任何一行是「长按」时整列一起收起。

        挂 ``currentIndexChanged`` 时会带上索引参数，所以收 ``*_args``。
        """

        any_long_press = False
        for row in range(self.table.rowCount()):
            long_press = self._row_is_long_press(row)
            any_long_press = any_long_press or long_press
            hold = self.table.cellWidget(row, _COL_HOLD)
            if isinstance(hold, QDoubleSpinBox):
                hold.setVisible(long_press)
        self.table.setColumnHidden(_COL_HOLD, not any_long_press)

    def add_row(self) -> None:
        if self.is_mouse:
            m: object = MouseMapping(trigger="", button=MouseButton.LEFT, action=MouseAction.CLICK)
        else:
            m = KeyMapping(trigger="", key="", action=KeyAction.PRESS_AND_RELEASE)
        row = self._append_mapping(m)
        self.table.selectRow(row)
        widget = self.table.cellWidget(row, _COL_TRIGGER)
        if widget:
            widget.setFocus()
        self._refresh_hold_column()
        self._sync_empty_hint()

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            # 倒序删：从大到小删不会让待删的小索引发生位移，
            # 因此可以边删表边删 ``_originals`` 的同一下标。
            self.table.removeRow(row)
            if 0 <= row < len(self._originals):
                del self._originals[row]
        self._refresh_hold_column()
        self._sync_empty_hint()

    def duplicate_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return
        skipped: List[int] = []
        for row in rows:
            # 升序追加：新行落在表尾，不会让后面待复制的行号发生位移。
            # deepcopy 一份再追加，副本与源行**不共享**任何可变状态；
            # ``_row_to_mapping`` 已经保住了 7 列之外的隐藏字段，
            # deepcopy 只是再加一道"改了副本不会串到原件"的保险。
            mapping = self._row_to_mapping(row)
            if mapping is None:
                # 静默跳过会让用户以为按钮坏了（那一行往往是触发词/按键填错）
                skipped.append(row + 1)
                continue
            self._append_mapping(copy.deepcopy(mapping))
        self._refresh_hold_column()
        self._sync_empty_hint()
        if skipped:
            QMessageBox.warning(
                self,
                "部分行没有复制",
                "以下行的内容不完整，无法复制：\n"
                + "、".join(f"第 {n} 行" for n in skipped)
                + "\n\n请先补全触发词与按键。",
            )

    def move_selected(self, delta: int) -> None:
        """把选中的那一行整体上移 / 下移 ``delta`` 行。

        为什么不能"删一行再插一行"：``insertRow`` 建出的是**全新的空行**，
        所有单元格的 ``cellWidget`` 都是 ``None``。早先的实现
        ``removeRow(row)`` → ``insertRow(target)`` → ``_fill_row(target, mapping)``
        里，``_fill_row`` 只回填了「启用」和「触发词」两列，而且是在
        ``insertRow`` 之后去读 ``cellWidget`` —— 拿到的全是 ``None``，
        于是一个字段都没写回去：点一次「上移」就毁掉一条映射，还在表里留下一个空行，
        用户再点保存必然被"触发词不能为空"拦下。

        现在改成**数据驱动**：先把整张表读成映射列表，在列表里交换两项，
        再用 :meth:`load` 重建整张表。顺序变了，任何字段都不可能丢。
        """

        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if len(rows) != 1:
            return
        row = rows[0]
        target = row + delta
        if target < 0 or target >= self.table.rowCount():
            return
        # quiet=True：别的行非法时只返回 None，不弹警告框 ——
        # 否则"点一下上移"会按行数弹出一串模态对话框。
        mappings = [
            self._row_to_mapping(r, quiet=True) for r in range(self.table.rowCount())
        ]
        if any(m is None for m in mappings):
            return
        mappings[row], mappings[target] = mappings[target], mappings[row]
        self.load(mappings)
        self.table.selectRow(target)

    # ----------------------------------------------------------- 读写映射
    def _original_for(self, row: int) -> Optional[object]:
        """取第 ``row`` 行对应的原始映射对象；越界或缺失时返回 ``None``。

        绝不抛异常：``_row_to_mapping`` 在 ``_originals`` 不可信时
        （行号越界、列表被外部清空）必须能安全回落到"新建对象"的老行为。
        """

        try:
            if 0 <= row < len(self._originals):
                return self._originals[row]
        except TypeError:
            # row 不是整数之类的怪值：当作越界
            return None
        return None

    def _row_to_mapping(self, row: int, quiet: bool = False) -> Optional[object]:
        """从一行控件读出映射对象；字段非法时返回 ``None`` 并提示。

        实现要点：**以第 ``row`` 行的原始对象为底**，用
        ``dataclasses.replace`` 只覆盖表格里真有控件的 8 列
        （触发词 / 匹配方式 / 启用 / 动作 / 长按时长 / 冷却 / 说明 / 按键|鼠标键），
        其余字段（``delay``、``enable_sensing``、``short_press_count``、
        ``long_press_count``、``case_sensitive``、``use_scancode``，
        鼠标另有 ``move_x`` / ``move_y`` / ``scroll_amount``）
        **原样带过**。

        早先这里是 ``KeyMapping(...)`` / ``MouseMapping(...)`` 直接新建，
        那 7 列之外的字段一律静默回落到 dataclass 默认值：用户打开编辑器
        **什么都不改**直接点保存，就会把 21/25 条键盘映射的 ``enable_sensing``
        从 ``True`` 清成 ``False``（鼠标 4/9 条同样），"666666 → 长按 6"
        这个主打功能整体失效且没有任何提示。

        ``quiet=True`` 时只返回 ``None``、**不弹任何警告框**：这是给
        :meth:`move_selected` 的预读用的——上移/下移是纯顺序操作，
        不该因为**别的**行填得不合法就弹出一串模态警告。
        :meth:`collect` 保持原行为（非法即弹框并返回 ``None``）。
        """

        def warn(title: str, text: str) -> None:
            if not quiet:
                QMessageBox.warning(self, title, text)

        trigger_widget = self.table.cellWidget(row, _COL_TRIGGER)
        trigger = trigger_widget.text().strip() if isinstance(trigger_widget, QLineEdit) else ""
        if not trigger:
            warn("触发词为空", f"第 {row + 1} 行的触发词不能为空。")
            return None

        enabled_wrap = self.table.cellWidget(row, _COL_ENABLED)
        enabled = True
        if isinstance(enabled_wrap, QWidget):
            chk = enabled_wrap.findChild(QCheckBox)
            if chk:
                enabled = chk.isChecked()

        desc_widget = self.table.cellWidget(row, _COL_DESCRIPTION)
        description = desc_widget.text().strip() if isinstance(desc_widget, QLineEdit) else ""

        cd_widget = self.table.cellWidget(row, _COL_COOLDOWN)
        cooldown = float(cd_widget.value()) if isinstance(cd_widget, QDoubleSpinBox) else 0.5

        mode_widget = self.table.cellWidget(row, _COL_MATCH_MODE)
        # 旧配置里可能没有 match_mode，或存着非法值 —— coerce 会回落到 EXACT
        match_mode = MatchMode.coerce(
            mode_widget.currentData() if isinstance(mode_widget, QComboBox) else None
        )

        action_widget = self.table.cellWidget(row, _COL_ACTION)
        action = action_widget.currentData() if isinstance(action_widget, QComboBox) else None

        original = self._original_for(row)

        hold_widget = self.table.cellWidget(row, _COL_HOLD)
        if isinstance(hold_widget, QDoubleSpinBox):
            hold = _coerce_hold(hold_widget.value())
        else:
            # 控件缺失（被外部删掉、将来重构漏建）时用**原值**，不要用默认 0.5：
            # 那会让"打开对话框直接保存"把用户设过的时长悄悄改回默认值。
            hold = _coerce_hold(getattr(original, "long_press_duration", None))

        # 表格里真有控件的那 8 列 —— 只覆盖这些，别的一律保留原值。
        visible = {
            "trigger": trigger,
            "enabled": enabled,
            "cooldown": cooldown,
            "description": description,
            "match_mode": match_mode,
            "long_press_duration": hold,
        }

        if self.is_mouse:
            button_widget = self.table.cellWidget(row, _COL_TARGET)
            button = (
                button_widget.currentData() if isinstance(button_widget, QComboBox) else MouseButton.LEFT
            )
            replaced = self._replace_original(
                original,
                MouseMapping,
                button=button or MouseButton.LEFT,
                action=action or MouseAction.CLICK,
                **visible,
            )
            if replaced is not None:
                return replaced
            return MouseMapping(
                trigger=trigger,
                button=button or MouseButton.LEFT,
                action=action or MouseAction.CLICK,
                enabled=enabled,
                cooldown=cooldown,
                description=description,
                match_mode=match_mode,
                long_press_duration=hold,
            )

        key_widget = self.table.cellWidget(row, _COL_TARGET)
        if isinstance(key_widget, KeyComboBox):
            # 按键选择器知道"显示文字 ↦ 键名"的换算，直接问它要键名
            raw_key = key_widget.current_key()
        elif isinstance(key_widget, QComboBox):
            raw_key = key_widget.currentText()
        else:
            raw_key = ""
        # 不能简单 strip()：VK_CODES 里 " "（单个空格）是空格键的合法别名，
        # strip 后变成 ""，于是空格键映射永远保存不了、还报"没有选择按键"。
        # 只把纯空白输入原样保留，其余情况仍去掉首尾空白。
        key = raw_key.strip() or raw_key
        if not key:
            warn("按键为空", f"第 {row + 1} 行没有选择按键。")
            return None
        if not is_valid_key_name(key):
            warn(
                "未知按键",
                f"第 {row + 1} 行的按键「{key}」无法识别。\n"
                f"可直接从下拉里选，或输入 a-z、0-9、f1-f12、space、enter、"
                f"up/down/left/right 等键名。",
            )
            return None
        replaced = self._replace_original(
            original,
            KeyMapping,
            key=key.lower(),
            action=action or KeyAction.PRESS_AND_RELEASE,
            **visible,
        )
        if replaced is not None:
            return replaced
        return KeyMapping(
            trigger=trigger,
            key=key.lower(),
            action=action or KeyAction.PRESS_AND_RELEASE,
            enabled=enabled,
            cooldown=cooldown,
            description=description,
            match_mode=match_mode,
            long_press_duration=hold,
        )

    @staticmethod
    def _replace_original(
        original: Optional[object], expected_cls: type, **overrides: object
    ) -> Optional[object]:
        """在 ``original`` 之上覆盖 ``overrides``；不适用时返回 ``None``。

        三道门槛，任何一道不过就交回调用方走"新建对象"的老路径：

        - ``original`` 缺失（越界、``_originals`` 被清空）；
        - ``original`` 不是 ``expected_cls`` 的实例（键鼠表格串了对象、
          或载入的是 :class:`_Legacy` 这类鸭子类型替身）——
          此时 ``dataclasses.replace`` 会因为字段名不存在抛 ``TypeError``
          （``KeyMapping`` 有 ``key`` 没有 ``button``，``MouseMapping`` 反之），
          必须捕获而不是让异常冒到界面上；
        - ``original`` 不是 dataclass 实例（同上，``replace`` 抛 ``TypeError``）。

        ``dataclasses.replace`` 会**新建**一个同类型实例，因此返回值与
        ``original`` 不共享可变状态，调用方可以安全地把它塞回
        ``_originals``（例如 :meth:`move_selected` 走 ``load`` 重建整表）。
        """

        if original is None or not isinstance(original, expected_cls):
            return None
        try:
            return dataclasses.replace(original, **overrides)
        except (TypeError, ValueError):
            return None

    def collect(self) -> Optional[List[object]]:
        """收集全部行；任一行非法则返回 ``None``。"""

        out: List[object] = []
        for row in range(self.table.rowCount()):
            mapping = self._row_to_mapping(row)
            if mapping is None:
                self.table.selectRow(row)
                return None
            out.append(mapping)
        return out


class MappingEditorDialog(QDialog):
    """键盘 + 鼠标映射的统一编辑对话框。"""

    def __init__(
        self,
        key_mappings: Sequence[KeyMapping],
        mouse_mappings: Sequence[MouseMapping],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑映射 - 幕键")
        self.resize(860, 600)
        self.setMinimumSize(700, 480)
        self._result_keys: Optional[List[KeyMapping]] = None
        self._result_mouse: Optional[List[MouseMapping]] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.tabs = QTabWidget()
        self.key_editor = MappingEditor(is_mouse=False)
        self.key_editor.load(key_mappings)
        self.tabs.addTab(self.key_editor, f"键盘映射（{len(key_mappings)} 条）")

        self.mouse_editor = MappingEditor(is_mouse=True)
        self.mouse_editor.load(mouse_mappings)
        self.tabs.addTab(self.mouse_editor, f"鼠标映射（{len(mouse_mappings)} 条）")

        layout.addWidget(self.tabs, 1)

        note = QLabel(
            "提示：同一弹幕命中多条规则时，<b>触发词更长的优先</b>；"
            "长度相同时精确匹配优先于包含匹配。"
            "每行的「匹配方式」都能单独设置；感应模式需要弹幕里连续重复字符达到设定次数。"
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        keys = self.key_editor.collect()
        if keys is None:
            self.tabs.setCurrentWidget(self.key_editor)
            return
        mouse = self.mouse_editor.collect()
        if mouse is None:
            self.tabs.setCurrentWidget(self.mouse_editor)
            return
        self._result_keys = [m for m in keys if isinstance(m, KeyMapping)]
        self._result_mouse = [m for m in mouse if isinstance(m, MouseMapping)]
        self.accept()

    def result_mappings(self) -> Tuple[List[KeyMapping], List[MouseMapping]]:
        """返回 ``(键盘映射, 鼠标映射)``；未保存时返回空列表。"""

        return self._result_keys or [], self._result_mouse or []
