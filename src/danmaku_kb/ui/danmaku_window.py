"""弹幕显示窗口。

设计要点（对照参考项目）
------------------------
* **有上限**：参考项目的弹幕框无限增长（``_trim_danmaku()`` 是空实现），
  挂一小时就卡死。这里每次 append 都按 ``max_lines`` 裁剪。
* **转义**：弹幕文本全部经 :func:`theme.escape_html`，参考项目直接把外部文本
  塞进 HTML，一条 ``<img src=x onerror=...>`` 就能让 Qt 富文本渲染器出问题。
* **批量刷新**：弹幕可能每秒几十条，逐条 ``appendHtml`` 会卡 UI。
  这里进队列、由 100ms 定时器批量 flush。
* **暂停/清空/保存**：运营场景常用，参考项目没有。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..config.models import DANMAKU_TYPE_ORDER, DisplayConfig
from ..danmaku.message import DanmakuMessage
from ..utils.logging_setup import get_logger
from .theme import COLORS, TYPE_STYLES, danmaku_html, escape_html

logger = get_logger(__name__)

__all__ = ["DanmakuWindow", "DisplaySettingsDialog"]

_DANMAKU_MIN_PX = 10

#: 设置对话框里每种消息类型的中文名。键跟 ``DANMAKU_TYPE_ORDER`` 走。
_TYPE_LABELS = {
    "danmaku": "普通弹幕",
    "gift": "礼物",
    "guard": "上舰",
    "super_chat": "醒目留言（SC）",
    "enter": "进场",
    "follow": "关注",
    "like": "点赞",
    "stats": "在线人数",
    "system": "连接/系统提示",
}


class DisplaySettingsDialog(QDialog):
    """弹幕窗口自己的「设置」对话框。

    旧版程序把"筛选"做成一个对话框：8 种消息类型各一个勾选框 + 时间/昵称/
    滚动/字号。这里参照它，但把选择器收进一个对话框、把值直接写回
    :class:`DisplayConfig`，这样选项页与弹幕窗口用的是同一份配置，
    不存在两套设置互相覆盖的问题。
    """

    def __init__(self, config: DisplayConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("弹幕显示设置")
        self.setMinimumWidth(360)
        self._type_boxes: Dict[str, QCheckBox] = {}
        self._build_ui(config)

    # ------------------------------------------------------------------ UI
    def _build_ui(self, config: DisplayConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        type_group = QGroupBox("显示哪些消息")
        grid = QGridLayout(type_group)
        grid.setVerticalSpacing(6)
        for row, key in enumerate(DANMAKU_TYPE_ORDER):
            label, color = TYPE_STYLES.get(key, ("", ""))
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{color or COLORS['text_dim']};")
            dot.setFixedWidth(14)
            box = QCheckBox(_TYPE_LABELS.get(key, key))
            box.setChecked(bool(config.show_types.get(key, True)))
            self._type_boxes[key] = box
            grid.addWidget(dot, row, 0)
            grid.addWidget(box, row, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(type_group)

        show_group = QGroupBox("显示与窗口")
        show_layout = QVBoxLayout(show_group)
        self.show_timestamp = QCheckBox("时间戳")
        self.show_timestamp.setChecked(bool(config.show_timestamp))
        self.show_nickname = QCheckBox("发送者昵称")
        self.show_nickname.setChecked(bool(config.show_nickname))
        self.show_platform = QCheckBox("平台标记（抖音/B站）")
        self.show_platform.setChecked(bool(config.show_platform))
        self.auto_scroll = QCheckBox("自动滚动到最新")
        self.auto_scroll.setChecked(bool(config.auto_scroll))
        self.always_on_top = QCheckBox("窗口置顶（悬浮在其它窗口之上）")
        self.always_on_top.setChecked(bool(config.always_on_top))
        for box in (self.show_timestamp, self.show_nickname, self.show_platform,
                    self.auto_scroll, self.always_on_top):
            show_layout.addWidget(box)
        layout.addWidget(show_group)

        size_group = QGroupBox("排版")
        size_layout = QGridLayout(size_group)
        self.font_size = QSpinBox()
        self.font_size.setRange(10, 48)
        self.font_size.setSuffix(" px")
        self.font_size.setValue(max(10, min(48, int(config.font_size))))
        self.max_lines = QSpinBox()
        self.max_lines.setRange(50, 10000)
        self.max_lines.setSingleStep(50)
        self.max_lines.setValue(max(50, min(10000, int(config.max_lines))))
        size_layout.addWidget(QLabel("字号"), 0, 0)
        size_layout.addWidget(self.font_size, 0, 1)
        size_layout.addWidget(QLabel("最多保留行数"), 1, 0)
        size_layout.addWidget(self.max_lines, 1, 1)
        layout.addWidget(size_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("应用")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # --------------------------------------------------------------- 取值
    def values(self) -> Dict[str, object]:
        """把界面状态收成 ``DisplayConfig`` 的字段字典。"""
        return {
            "show_types": {key: box.isChecked() for key, box in self._type_boxes.items()},
            "show_timestamp": self.show_timestamp.isChecked(),
            "show_nickname": self.show_nickname.isChecked(),
            "show_platform": self.show_platform.isChecked(),
            "auto_scroll": self.auto_scroll.isChecked(),
            "always_on_top": self.always_on_top.isChecked(),
            "font_size": int(self.font_size.value()),
            "max_lines": int(self.max_lines.value()),
        }


class DanmakuWindow(QWidget):
    """独立的弹幕列表窗口。

    用法::

        win = DanmakuWindow(config.display)
        win.append(message, actionable=True, matched="w")
        win.show()
    """

    closed = pyqtSignal()
    #: 「只看触发按键的」/「自动滚动」被改动（携带更新后的 DisplayConfig）。
    #: 这两个复选框以前只存在于控件里，表面上"能用"，但只要用户改一下字号或
    #: 行数触发 ``apply_config``，勾选状态就被配置里的旧值静默回滚，重启也不保留。
    configChanged = pyqtSignal(object)

    #: 待渲染队列上限。暂停期间 ``_flush`` 不消费队列，而队列里放的是已经渲染
    #: 好的 HTML 字符串 —— 不设上限的话暂停一小时就是单向内存增长，恢复瞬间
    #: 还会一次性堆进 ``insertHtml`` 把界面卡住。
    PENDING_LIMIT = 5000

    def __init__(self, config: Optional[DisplayConfig] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config or DisplayConfig()
        self._pending: Deque[str] = deque(maxlen=self.PENDING_LIMIT)
        self._history: List[DanmakuMessage] = []
        self._paused = False
        self._count = 0
        #: 被「显示哪些消息」设置挡掉的条数（只统计，不进历史/导出）
        self._type_filtered = 0

        self.setWindowTitle("弹幕列表 - 弹幕键盘 V2")
        self.resize(560, 620)
        self._build_ui()

        # 窗还没 show 时改 flags 最干净：不会闪一下，也不用恢复几何。
        self._apply_always_on_top(bool(self.config.always_on_top), initial=True)

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(100)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("暂停后新弹幕只入库不显示，恢复时补齐")
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        bar.addWidget(self.pause_btn)

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.clear)
        bar.addWidget(clear_btn)

        save_btn = QPushButton("导出")
        save_btn.setToolTip("把当前显示的弹幕存成文本文件")
        save_btn.clicked.connect(self._save)
        bar.addWidget(save_btn)

        # 工具条按功能分两组：左边是"操作"（暂停/清空/导出），右边是"筛选"
        # （只看触发/自动滚动）。以前 6 件挤在一起、中间只有一段弹性空白，
        # 用户得逐个读完标签才知道哪个是按钮、哪个是开关。
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background:{COLORS['border']}; border:none;")
        bar.addWidget(sep)

        self.only_triggered = QCheckBox("只看触发按键的")
        self.only_triggered.setChecked(bool(self.config.only_triggered))
        self.only_triggered.toggled.connect(self._on_only_triggered_toggled)
        bar.addWidget(self.only_triggered)

        self.auto_scroll = QCheckBox("自动滚动")
        self.auto_scroll.setChecked(bool(self.config.auto_scroll))
        self.auto_scroll.toggled.connect(self._on_auto_scroll_toggled)
        bar.addWidget(self.auto_scroll)

        # 置顶是窗口级开关（``Qt.WindowStaysOnTopHint``），初值在 __init__ 里
        # 统一应用；这里先 setChecked 再 connect，避免构造期多发一次 toggled。
        self.pin_btn = QPushButton("置顶")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setChecked(bool(self.config.always_on_top))
        self.pin_btn.setToolTip("让弹幕窗口始终显示在其它窗口之上（再点一次取消）")
        self.pin_btn.toggled.connect(self._on_pin_toggled)
        bar.addWidget(self.pin_btn)

        settings_btn = QPushButton("设置")
        settings_btn.setToolTip("选择显示哪些消息类型、时间/昵称/平台、字号与行数")
        settings_btn.clicked.connect(self.open_settings)
        bar.addWidget(settings_btn)
        bar.addStretch(1)

        layout.addLayout(bar)

        self.view = QTextEdit(self)
        self.view.setReadOnly(True)
        self._apply_view_font()
        self.view.setLineWrapMode(QTextEdit.WidgetWidth)
        self.view.document().setMaximumBlockCount(max(50, int(self.config.max_lines)))
        layout.addWidget(self.view, 1)

        self.status = QLabel("等待弹幕…")
        self.status.setObjectName("hint")
        layout.addWidget(self.status)

    def _apply_view_font(self) -> None:
        """应用弹幕字号。**必须走控件级 QSS**，``setFont()`` 在这里是无效的。

        应用级样式表里有 ``QWidget { font-size: 13px }``，它在控件 polish 时
        覆盖 ``setFont()``：实测窗口 ``show()`` 前 ``font().pixelSize()`` 是配置值，
        ``show()`` 之后一律变回 13 —— 也就是说「字号」这项设置在过去**从来没
        生效过**，弹幕窗永远是 13px（改滑块只在下一次 repolish 前短暂生效，
        所以是"有时候管用"的错觉）。控件级样式表优先级高于应用级，且 repolish
        后依然保留，因此把字号写在这里。
        """

        size = max(_DANMAKU_MIN_PX, int(getattr(self.config, "font_size", 13) or 13))
        self.view.setStyleSheet(f"font-size: {size}px;")

    # --------------------------------------------------------------- 写入
    def append(self, message: DanmakuMessage, actionable: bool = False, matched: str = "") -> None:
        """入队一条弹幕。真正的 DOM 写入在定时器里批量做。"""

        self._count += 1
        raw_type = getattr(message, "msg_type", "")
        kind = str(getattr(raw_type, "value", raw_type) or "")
        # 类型开关最先判：被关掉的类型不进历史也不进队列，否则「导出」
        # 会把用户明确不想看的内容一并导出去。
        if not self.config.type_enabled(kind):
            self._type_filtered += 1
            self._update_status()
            return

        self._history.append(message)
        if len(self._history) > 5000:
            del self._history[:1000]

        if self.only_triggered.isChecked() and not actionable:
            self._update_status()
            return

        stamp = ""
        if self.config.show_timestamp:
            # ``message.timestamp`` 是 ``datetime``（见 ``DanmakuMessage.__post_init__``），
            # 不是 unix 时间戳。这里曾经写成 ``datetime.fromtimestamp(...)``，
            # 对 datetime 会抛 TypeError；而 PyQt5 遇到槽里的未捕获异常会直接
            # qFatal 终结整个进程（0xC0000409），所以是"收到第一条弹幕就闪退"。
            stamp = message.timestamp.strftime("%H:%M:%S")
        platform = ""
        if self.config.show_platform:
            platform = {"douyin": "抖音", "bilibili": "B站", "simulator": "模拟"}.get(
                message.platform, message.platform or ""
            )
        nickname = message.nickname if self.config.show_nickname else ""
        html = danmaku_html(
            text=message.content,
            nickname=nickname,
            timestamp=stamp,
            platform=platform,
            actionable=actionable,
            matched=matched,
            kind=kind,
        )
        self._pending.append(html)
        self._update_status()

    def append_system(self, text: str) -> None:
        """插入一条系统提示（连接状态等）。"""

        stamp = datetime.now().strftime("%H:%M:%S")
        self._pending.append(
            f'<span style="color:{COLORS["sys"]}">[{stamp}] —— {escape_html(text)} ——</span>'
        )

    def _flush(self) -> None:
        if self._paused or not self._pending:
            self._update_status()
            return

        # 单次最多写 200 行，避免一次卡住 UI
        chunk = []
        for _ in range(min(len(self._pending), 200)):
            chunk.append(self._pending.popleft())
        if not chunk:
            return

        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self.view.moveCursor(QTextCursor.End)
        self.view.insertHtml("<br>".join(chunk) + "<br>")
        if self.auto_scroll.isChecked() and at_bottom:
            sb.setValue(sb.maximum())
        self._update_status()

    def _update_status(self) -> None:
        n = self._count
        shown = self.view.document().blockCount()
        extra = f"（暂停中，积压 {len(self._pending)} 条）" if self._paused else ""
        if self._type_filtered:
            extra += f"（按设置过滤 {self._type_filtered} 条）"
        self.status.setText(f"共收到 {n} 条弹幕，当前显示 {shown} 行{extra}")

    # --------------------------------------------------------------- 交互
    def open_settings(self) -> None:
        """打开「设置」对话框，应用后立刻生效并写回配置。"""
        dialog = DisplaySettingsDialog(self.config, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        self.apply_settings(dialog.values())

    def apply_settings(self, values: Dict[str, object]) -> None:
        """把 :meth:`DisplaySettingsDialog.values` 的结果应用到本窗口。"""
        show_types = values.get("show_types")
        if isinstance(show_types, dict):
            # 只覆盖界面里有的键，保证旧配置里多出来的未知类型不被抹掉
            for key, flag in show_types.items():
                self.config.show_types[str(key)] = bool(flag)
        self.config.show_timestamp = bool(values.get("show_timestamp", self.config.show_timestamp))
        self.config.show_nickname = bool(values.get("show_nickname", self.config.show_nickname))
        self.config.show_platform = bool(values.get("show_platform", self.config.show_platform))
        self.config.auto_scroll = bool(values.get("auto_scroll", self.config.auto_scroll))
        if "font_size" in values:
            self.config.font_size = max(_DANMAKU_MIN_PX, min(48, int(values["font_size"])))  # type: ignore[arg-type]
        if "max_lines" in values:
            self.config.max_lines = max(50, min(10000, int(values["max_lines"])))  # type: ignore[arg-type]
        if "always_on_top" in values:
            self.config.always_on_top = bool(values["always_on_top"])
            blocked = self.pin_btn.blockSignals(True)
            self.pin_btn.setChecked(self.config.always_on_top)
            self.pin_btn.blockSignals(blocked)
            self._apply_always_on_top(self.config.always_on_top)

        # 工具栏那个「自动滚动」勾选框是同一项设置的另一处入口，必须同步，
        # 否则两处显示的值会长期不一致。
        blocked = self.auto_scroll.blockSignals(True)
        self.auto_scroll.setChecked(bool(self.config.auto_scroll))
        self.auto_scroll.blockSignals(blocked)

        self._apply_view_font()
        self.view.document().setMaximumBlockCount(max(50, int(self.config.max_lines)))
        self.configChanged.emit(self.config)

    def _on_only_triggered_toggled(self, on: bool) -> None:
        self.config.only_triggered = bool(on)
        self.configChanged.emit(self.config)

    def _on_auto_scroll_toggled(self, on: bool) -> None:
        self.config.auto_scroll = bool(on)
        self.configChanged.emit(self.config)

    def _on_pin_toggled(self, on: bool) -> None:
        self.config.always_on_top = bool(on)
        self._apply_always_on_top(bool(on))
        self.configChanged.emit(self.config)

    def _apply_always_on_top(self, on: bool, initial: bool = False) -> None:
        """切换 ``Qt.WindowStaysOnTopHint``。

        注意两个 Qt 的坑（都在真机上验证过）：

        * ``setWindowFlags`` 在 Windows 上会**隐藏窗口并可能重置几何**，
          所以已显示的窗口改完 flags 必须 ``show()`` 回来，并先存一份
          ``geometry()`` 恢复，否则窗口会跑到左上角、尺寸也会变；
        * 只在 flags 真变化时才动，避免每次 ``apply_config`` 都把窗口
          闪一下。
        """
        flags = self.windowFlags()
        if bool(flags & Qt.WindowStaysOnTopHint) == bool(on):
            return
        if on:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint

        if initial:
            self.setWindowFlags(flags)
            return

        geometry = self.geometry()
        visible = self.isVisible()
        self.setWindowFlags(flags)
        self.setGeometry(geometry)
        if visible:
            self.show()

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        self.pause_btn.setText("恢复" if paused else "暂停")
        self._update_status()

    def clear(self) -> None:
        self._pending.clear()
        self.view.clear()
        self._history.clear()
        self._count = 0
        self._type_filtered = 0
        self._update_status()

    def _save(self) -> None:
        if not self._history:
            self.status.setText("没有可导出的弹幕")
            return
        default = f"弹幕记录_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "导出弹幕", default, "文本文件 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for msg in self._history:
                    ts = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                    fh.write(f"[{ts}] [{msg.platform}] {msg.nickname}: {msg.content}\n")
            self.status.setText(f"已导出 {len(self._history)} 条到 {path}")
        except OSError as exc:
            self.status.setText(f"导出失败：{exc}")

    # --------------------------------------------------------------- 生命周期
    def apply_config(self, config: DisplayConfig) -> None:
        self.config = config
        self._apply_view_font()
        self.view.document().setMaximumBlockCount(max(50, int(config.max_lines)))
        # 必须先阻断信号：``setChecked`` 会触发 ``toggled``，不挡的话
        # ``_on_*_toggled`` 会立刻把刚读进来的配置又写回去（此时 self.config
        # 已经是新对象，等于把用户的勾选状态用"新配置"覆盖，看似无害，但会在
        # 以后有人把这两个槽改成写盘时变成一次多余的保存）。
        for box, value in (
            (self.only_triggered, bool(config.only_triggered)),
            (self.auto_scroll, bool(config.auto_scroll)),
            (self.pin_btn, bool(config.always_on_top)),
        ):
            blocked = box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(blocked)

        self._apply_always_on_top(bool(config.always_on_top))

    def set_opacity(self, value: float) -> None:
        self.setWindowOpacity(min(1.0, max(0.2, float(value))))

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """重新显示时恢复刷新定时器。

        ``closeEvent`` 会停掉 100ms 的 ``_flush_timer``，而主窗口复用同一个
        ``DanmakuWindow`` 实例（只调 ``show()``），不重启定时器的话第二次
        打开弹幕窗口后 ``_pending`` 永远不被消费 —— 窗口一直是空白的。
        """
        super().showEvent(event)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._flush_timer.stop()
        self.closed.emit()
        super().closeEvent(event)
