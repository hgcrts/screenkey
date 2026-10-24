"""筛选规则编辑器。

参考项目的筛选是 6 个平行列表（关键词 / 前缀 / 正则 / 屏蔽用户 / 长度上下限），
它们**只能全部叠加**——无法表达"要么 A 要么 B"，也无法让不同规则做不同的事
（比如"含广告词就丢弃"和"以 ! 开头就改写内容"）。

这里改成**有序规则列表**，第一条命中的规则生效，规则内部条件之间是"与"。
界面上就是一个可增删的规则表 + 条件子表。
"""

from __future__ import annotations

from typing import List, Optional
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..filters.rules import (
    CompareOp,
    Condition,
    FieldMatch,
    FilterAction,
    FilterEngine,
    FilterRule,
)
from ..utils.logging_setup import get_logger
from .theme import EmptyState, rich_paragraph, style_combo

logger = get_logger(__name__)

__all__ = ["FilterEditorDialog"]

_ACTION_LABELS = {
    "ALLOW": "放行（可触发按键）",
    "DROP": "丢弃（不显示、不触发）",
    "DISPLAY_ONLY": "只显示（不触发按键）",
    "REWRITE": "改写内容（再按改写后的匹配）",
}
_FIELD_LABELS = {
    "CONTENT": "弹幕内容",
    "NICKNAME": "用户昵称",
    "USER_ID": "用户ID",
    "PLATFORM": "平台",
    "MSG_TYPE": "消息类型",
}
_OP_LABELS = {
    "EQUALS": "等于",
    "CONTAINS": "包含",
    "NOT_CONTAINS": "不包含",
    "STARTWITH": "开头是",
    "ENDSWITH": "结尾是",
    "REGEX": "正则匹配",
    "NOT_REGEX": "正则不匹配",
    "IN": "属于（逗号分隔）",
    "NOT_IN": "不属于",
    "LENGTH_GT": "长度大于",
    "LENGTH_LT": "长度小于",
    "LENGTH_EQ": "长度等于",
}


def _copy_condition(cond: Condition) -> Condition:
    """条件深拷贝（``conditions`` 里是可变 dataclass）。"""
    return Condition(
        field=cond.field,
        op=cond.op,
        value=cond.value,
        case_sensitive=cond.case_sensitive,
        negate=cond.negate,
    )


def _copy_rule(rule: FilterRule) -> FilterRule:
    """规则深拷贝。

    对话框里所有编辑都是**原地改字段**（``rule.name = ...``）。若只做
    ``list(engine.rules)``，元素仍与正在运行的引擎共享，点「取消」也会把
    改动写进引擎和 ``ConfigManager`` —— 用户以为撤销了，其实已经生效。
    """
    return FilterRule(
        name=rule.name,
        action=rule.action,
        conditions=[_copy_condition(c) for c in rule.conditions],
        msg_types=list(rule.msg_types),
        platforms=list(rule.platforms),
        strip_prefix=rule.strip_prefix,
        rewrite_to=rule.rewrite_to,
        enabled=rule.enabled,
        description=rule.description,
        match_any=rule.match_any,
    )


def _enum_combo(enum_cls, labels: dict, current=None) -> QComboBox:
    box = style_combo(QComboBox(), 10)
    for member in enum_cls:
        box.addItem(labels.get(member.name, member.name), member)
    if current is not None:
        for i in range(box.count()):
            if box.itemData(i) == current:
                box.setCurrentIndex(i)
                break
    return box


def _pin_row_height(table: QTableWidget) -> None:
    """把行高钉死在 30px。

    单元格里放的是 27px 高的下拉框 / 输入框 / 复选框，而默认行高按字体度量算
    只有 18-20px。行高不固定时 Qt 取控件 sizeHint 的折中值：下拉框只露半截、
    复选框被裁掉上下沿、「区分大小写」这类表头折成两行。规则表和条件表都是
    本应用最核心的编辑界面，必须逐行可读。
    """

    header = table.verticalHeader()
    header.setVisible(False)
    header.setDefaultSectionSize(30)
    header.setSectionResizeMode(QHeaderView.Fixed)
    table.setWordWrap(False)


class FilterEditorDialog(QDialog):
    """规则列表编辑器。"""

    def __init__(self, engine: Optional[FilterEngine] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑筛选规则 - 弹幕键盘 V2")
        self.resize(920, 640)
        # 下限按"三列都能用"反推：规则表 250 + 条件表 440 + 间距
        self.setMinimumSize(760, 520)

        # **必须深拷贝**。``list(engine.rules)`` 只复制了列表、元素仍是引擎里
        # 的活对象，而 ``_save_detail()``/``_on_rule_enabled()`` 是原地改字段，
        # 于是点「取消」也会把改动留在正在运行的引擎和配置里。
        source = engine.rules if engine else FilterEngine.default().rules
        self._rules: List[FilterRule] = [_copy_rule(rule) for rule in source]
        self._conditions: List[List[Condition]] = [list(r.conditions) for r in self._rules]
        #: 上一次选中的规则行。切换行前要先把旧行的详情与条件收集回来，
        #: 否则用户在输入框里刚敲的内容会被 ``_reload_*`` 静默覆盖。
        self._current_row = -1
        #: 装载详情控件时为 True，用来挡住控件信号把中间态写回模型。
        self._loading = False

        self._build_ui()
        self._reload_rules()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        # 三行说明改一行（原 4 行）后仍要紧凑：走 rich_paragraph 才有行距，
        # QSS 不支持 line-height。
        tip = QLabel(
            rich_paragraph(
                "规则从上到下依次判断，第一条命中的决定这条弹幕的命运，都不命中则默认放行。"
                "一条规则内多个条件怎么组合由「条件之间」决定。"
            )
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        splitter = QSplitter(Qt.Horizontal)

        # ---- 左：规则表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        rules_head = QLabel("规则（自上而下优先）")
        rules_head.setObjectName("section")
        ll.addWidget(rules_head)

        self.rule_table = QTableWidget(0, 3)
        self.rule_table.setHorizontalHeaderLabels(["启用", "规则名", "动作"])
        self.rule_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rule_table.setSelectionMode(QAbstractItemView.SingleSelection)
        # 与主窗口映射表、映射编辑器保持一致：三张表要么都有斑马纹，要么都没有
        self.rule_table.setAlternatingRowColors(True)
        _pin_row_height(self.rule_table)
        self.rule_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.rule_table.itemSelectionChanged.connect(self._on_rule_selected)
        # 空态提示叠在表上（EmptyState），不占布局高度：否则显隐会让整列跳 23px
        self.empty_hint = EmptyState(
            self.rule_table, "还没有规则。点「新建」加一条；没有规则时所有弹幕一律放行。"
        )
        ll.addWidget(self.empty_hint, 1)

        rbar = QHBoxLayout()
        rbar.setSpacing(8)
        for text, slot in (
            ("新建", self._add_rule),
            ("删除", self._remove_rule),
            ("上移", lambda: self._move_rule(-1)),
            ("下移", lambda: self._move_rule(1)),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            rbar.addWidget(btn)
        rbar.addStretch(1)
        ll.addLayout(rbar)
        splitter.addWidget(left)

        # ---- 右：规则详情 + 条件表
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        self.detail_box = QGroupBox("规则设置")
        self.detail_form = QFormLayout(self.detail_box)
        # 与 main_window 的 _form 同一套度量：行距 8 / 列距 12 / 标签右对齐
        self.detail_form.setVerticalSpacing(8)
        self.detail_form.setHorizontalSpacing(12)
        self.detail_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._save_detail)
        self.detail_form.addRow("规则名：", self.name_edit)

        self.action_combo = _enum_combo(FilterAction, _ACTION_LABELS)
        # 注意：currentIndexChanged 会带一个 int 参数，而 _save_detail(row=None)
        # 的第一个参数是"目标行号"。必须用 lambda 吞掉这个 int，否则详情会被
        # 写到"行号=下拉框索引"的规则上（改 A 规则结果改坏了 B 规则）。
        self.action_combo.currentIndexChanged.connect(lambda *_: self._save_detail())
        self.detail_form.addRow("命中后：", self.action_combo)

        self.strip_edit = QLineEdit()
        self.strip_edit.setPlaceholderText("例如 ! ——把内容开头的 ! 去掉再匹配")
        self.strip_edit.editingFinished.connect(self._save_detail)
        self.detail_form.addRow("剥离前缀：", self.strip_edit)

        self.rewrite_edit = QLineEdit()
        self.rewrite_edit.setPlaceholderText("动作选「改写内容」时生效，替换成这里的文本")
        self.rewrite_edit.editingFinished.connect(self._save_detail)
        self.detail_form.addRow("改写为：", self.rewrite_edit)

        self.match_mode_combo = style_combo(QComboBox(), 12)
        self.match_mode_combo.addItem("全部满足（与）", False)
        self.match_mode_combo.addItem("任一满足（或）", True)
        self.match_mode_combo.setToolTip(
            "条件之间的关系。\n"
            "「与」：所有条件都成立才算命中这条规则。\n"
            "「或」：任一条件成立即命中——长度限制这类"
            "「太短 或 太长都该丢」的规则必须选「或」，"
            "否则两个条件不可能同时成立，规则永不生效。"
        )
        # 同样要吞掉 Qt 传进来的 int，否则会被当成"目标行号"
        self.match_mode_combo.currentIndexChanged.connect(lambda *_: self._save_detail())
        self.match_mode_combo.currentIndexChanged.connect(lambda *_: self._sync_cond_hint())
        self.detail_form.addRow("条件之间：", self.match_mode_combo)
        rl.addWidget(self.detail_box)

        # 这行旧文案把「与」写死了：用户把「条件之间」改成「或」之后，
        # 标题还在说"全部满足"，等于界面自相矛盾。改成跟着下拉框走。
        self.cond_hint = QLabel()
        self.cond_hint.setObjectName("section")
        self._sync_cond_hint()
        rl.addWidget(self.cond_hint)
        self.cond_table = QTableWidget(0, 5)
        self.cond_table.setHorizontalHeaderLabels(["字段", "判断", "值", "区分大小写", "取反"])
        _pin_row_height(self.cond_table)
        self.cond_table.setAlternatingRowColors(True)
        self.cond_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        rl.addWidget(self.cond_table, 1)

        cbar = QHBoxLayout()
        cbar.setSpacing(8)
        add_c = QPushButton("加条件")
        add_c.clicked.connect(self._add_condition)
        cbar.addWidget(add_c)
        del_c = QPushButton("删条件")
        del_c.clicked.connect(self._remove_condition)
        cbar.addWidget(del_c)
        cbar.addStretch(1)
        rl.addLayout(cbar)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------ 规则表
    def _reload_rules(self) -> None:
        self.rule_table.blockSignals(True)
        self.rule_table.setRowCount(0)
        for rule in self._rules:
            row = self.rule_table.rowCount()
            self.rule_table.insertRow(row)

            chk = QCheckBox()
            chk.setChecked(bool(rule.enabled))
            chk.stateChanged.connect(lambda _s, r=row: self._on_rule_enabled(r))
            wrap = QWidget()
            wl = QHBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setAlignment(Qt.AlignCenter)
            wl.addWidget(chk)
            self.rule_table.setCellWidget(row, 0, wrap)

            self.rule_table.setItem(row, 1, QTableWidgetItem(rule.name or "（未命名）"))
            self.rule_table.setItem(
                row, 2, QTableWidgetItem(_ACTION_LABELS.get(rule.action.name, rule.action.name))
            )
        self.rule_table.blockSignals(False)
        if self._rules:
            self.rule_table.selectRow(0)
        self._sync_empty_hint()

    def _sync_empty_hint(self) -> None:
        """按规则条数切换空态提示。空表 + 一排按钮是最容易被当成"坏了"的样子。"""

        self.empty_hint.refresh()

    def _sync_cond_hint(self) -> None:
        """条件表标题跟随「条件之间」与条数，避免文案与当前语义打架。"""

        label = self.match_mode_combo.currentText() if hasattr(self, "match_mode_combo") else ""
        logic = "任一满足即命中本规则" if "或" in label else "全部满足才命中本规则"
        count = self.cond_table.rowCount() if hasattr(self, "cond_table") else 0
        if count == 0:
            # 没有条件 = 这条规则无条件命中，必须说清楚，否则"什么都没配却全拦了"
            self.cond_hint.setText("条件：无（本规则无条件命中）")
        else:
            self.cond_hint.setText(f"条件（{logic}，共 {count} 条）")

    def _on_rule_enabled(self, row: int) -> None:
        if 0 <= row < len(self._rules):
            wrap = self.rule_table.cellWidget(row, 0)
            chk = wrap.findChild(QCheckBox) if isinstance(wrap, QWidget) else None
            if chk:
                self._rules[row].enabled = chk.isChecked()

    def _selected_rule(self) -> int:
        rows = {i.row() for i in self.rule_table.selectedIndexes()}
        return next(iter(rows)) if rows else -1

    def _flush_current_row(self) -> None:
        """把当前行正在编辑的详情和条件收回到内存模型。

        规则表的选择变化、加/删/移规则、加/删条件都会重建控件，而重建前若
        不先收集，用户刚输入的内容会被 ``_reload_*`` 直接覆盖（静默丢数据）。
        所有会触发重建的入口都必须先调这个方法。
        """
        row = self._current_row
        if row < 0 or row >= len(self._rules):
            return
        self._save_detail(row)
        if 0 <= row < len(self._conditions):
            self._conditions[row] = self._collect_conditions(row)

    def _on_rule_selected(self) -> None:
        row = self._selected_rule()
        # 切换行前先把旧行收回来（含未提交的规则名/前缀/改写文本与条件表）
        if row != self._current_row:
            self._flush_current_row()
        if row < 0 or row >= len(self._rules):
            self._current_row = -1
            self.detail_box.setEnabled(False)
            self.cond_table.setRowCount(0)
            return
        self._current_row = row
        self.detail_box.setEnabled(True)
        rule = self._rules[row]
        self._loading = True
        self.name_edit.setText(rule.name)
        for i in range(self.action_combo.count()):
            if self.action_combo.itemData(i) == rule.action:
                self.action_combo.setCurrentIndex(i)
                break
        self.strip_edit.setText(rule.strip_prefix)
        self.rewrite_edit.setText(rule.rewrite_to)
        for i in range(self.match_mode_combo.count()):
            if bool(self.match_mode_combo.itemData(i)) == bool(rule.match_any):
                self.match_mode_combo.setCurrentIndex(i)
                break
        self._loading = False
        self._update_cond_hint()
        self._reload_conditions(row)

    def _save_detail(self, row: Optional[int] = None) -> None:
        if getattr(self, "_loading", False):
            return
        if row is None:
            row = self._selected_rule()
        if row < 0 or row >= len(self._rules):
            return
        rule = self._rules[row]
        rule.name = self.name_edit.text().strip()
        rule.action = self.action_combo.currentData() or FilterAction.ALLOW
        rule.strip_prefix = self.strip_edit.text()
        rule.rewrite_to = self.rewrite_edit.text()
        rule.match_any = bool(self.match_mode_combo.currentData())
        self._update_cond_hint()
        item = self.rule_table.item(row, 1)
        if item is not None:
            item.setText(rule.name or "（未命名）")
        item = self.rule_table.item(row, 2)
        if item is not None:
            item.setText(_ACTION_LABELS.get(rule.action.name, rule.action.name))

    def _update_cond_hint(self) -> None:
        """条件表标题跟随「与/或」选择，避免用户误判语义。"""
        if not hasattr(self, "cond_hint"):
            return
        any_mode = bool(self.match_mode_combo.currentData())
        self.cond_hint.setText(
            "条件（任一满足即命中本规则）" if any_mode else "条件（全部满足才命中本规则）"
        )

    def _add_rule(self) -> None:
        self._flush_current_row()
        self._rules.append(FilterRule(name="新规则", action=FilterAction.ALLOW))
        self._conditions.append([])
        self._current_row = -1
        self._reload_rules()
        self.rule_table.selectRow(len(self._rules) - 1)

    def _remove_rule(self) -> None:
        row = self._selected_rule()
        if row < 0:
            return
        self._flush_current_row()
        del self._rules[row]
        del self._conditions[row]
        self._current_row = -1
        self._reload_rules()

    def _move_rule(self, delta: int) -> None:
        row = self._selected_rule()
        target = row + delta
        if row < 0 or target < 0 or target >= len(self._rules):
            return
        self._flush_current_row()
        self._rules[row], self._rules[target] = self._rules[target], self._rules[row]
        self._conditions[row], self._conditions[target] = self._conditions[target], self._conditions[row]
        self._current_row = -1
        self._reload_rules()
        self.rule_table.selectRow(target)

    # ------------------------------------------------------------ 条件表
    def _reload_conditions(self, rule_row: int) -> None:
        conds = self._conditions[rule_row] if 0 <= rule_row < len(self._conditions) else []
        self.cond_table.blockSignals(True)
        self.cond_table.setRowCount(0)
        for cond in conds:
            row = self.cond_table.rowCount()
            self.cond_table.insertRow(row)
            self.cond_table.setCellWidget(row, 0, _enum_combo(FieldMatch, _FIELD_LABELS, cond.field))
            self.cond_table.setCellWidget(row, 1, _enum_combo(CompareOp, _OP_LABELS, cond.op))
            self.cond_table.setCellWidget(row, 2, QLineEdit(str(cond.value)))
            cs = QCheckBox()
            cs.setChecked(bool(cond.case_sensitive))
            self.cond_table.setCellWidget(row, 3, cs)
            neg = QCheckBox()
            neg.setChecked(bool(cond.negate))
            self.cond_table.setCellWidget(row, 4, neg)
        self.cond_table.blockSignals(False)
        if hasattr(self, "cond_hint"):
            self._sync_cond_hint()

    def _collect_conditions(self, rule_row: int) -> List[Condition]:
        out: List[Condition] = []
        for row in range(self.cond_table.rowCount()):
            field_w = self.cond_table.cellWidget(row, 0)
            op_w = self.cond_table.cellWidget(row, 1)
            val_w = self.cond_table.cellWidget(row, 2)
            cs_w = self.cond_table.cellWidget(row, 3)
            neg_w = self.cond_table.cellWidget(row, 4)
            out.append(
                Condition(
                    field=field_w.currentData() if isinstance(field_w, QComboBox) else FieldMatch.CONTENT,
                    op=op_w.currentData() if isinstance(op_w, QComboBox) else CompareOp.CONTAINS,
                    value=val_w.text() if isinstance(val_w, QLineEdit) else "",
                    case_sensitive=bool(cs_w.isChecked()) if isinstance(cs_w, QCheckBox) else False,
                    negate=bool(neg_w.isChecked()) if isinstance(neg_w, QCheckBox) else False,
                )
            )
        return out

    def _add_condition(self) -> None:
        row = self._selected_rule()
        if row < 0:
            return
        # 先收回条件表里已有的输入，否则新增一行会把它整个覆盖
        self._flush_current_row()
        self._conditions[row].append(Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value=""))
        self._reload_conditions(row)

    def _remove_condition(self) -> None:
        row = self._selected_rule()
        if row < 0:
            return
        sel = sorted({i.row() for i in self.cond_table.selectedIndexes()}, reverse=True)
        if not sel:
            return
        # 同上：删除前先把没删的那些行的输入收回
        self._flush_current_row()
        for r in sel:
            if 0 <= r < len(self._conditions[row]):
                del self._conditions[row][r]
        self._reload_conditions(row)

    # --------------------------------------------------------------- 提交
    def _on_accept(self) -> None:
        # 先把当前行编辑中的详情与条件写回，再校验
        self._flush_current_row()

        # 校验正则，写错立刻告诉用户，而不是运行时静默失效
        import re

        for i, (rule, conds) in enumerate(zip(self._rules, self._conditions)):
            for cond in conds:
                if cond.op in (CompareOp.REGEX, CompareOp.NOT_REGEX):
                    try:
                        re.compile(str(cond.value))
                    except re.error as exc:
                        QMessageBox.warning(
                            self, "正则写错了",
                            f"第 {i + 1} 条规则「{rule.name}」的正则无法编译：\n{cond.value}\n\n{exc}",
                        )
                        self.rule_table.selectRow(i)
                        return
            rule.conditions = [_copy_condition(c) for c in conds]
        self.accept()

    def engine(self) -> FilterEngine:
        """构造编辑后的引擎（每条规则都是独立副本）。"""

        return FilterEngine(rules=[_copy_rule(r) for r in self._rules])
