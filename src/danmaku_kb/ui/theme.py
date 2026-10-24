"""界面样式与配色。

单文件集中管理，避免像参考项目那样把颜色散落在各处、改一处漏一处。
"""

from __future__ import annotations

from typing import Dict, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QComboBox, QLabel, QStackedLayout, QWidget

__all__ = [
    "COLORS",
    "RADIUS",
    "STYLESHEET",
    "EmptyState",
    "TYPE_STYLES",
    "danmaku_html",
    "escape_html",
    "rich_paragraph",
    "style_combo",
]

#: 圆角只有三档，按「元素身份」分配，别再随手写新数字：
#:
#: - ``container`` 6px —— 面（QGroupBox 卡片、二维码面）
#: - ``control``   4px —— 控件（按钮、输入框、下拉、表格、页签、下拉弹层）
#: - ``indicator`` 3px —— 小指示器（14px 的勾选框方块）
#:
#: ``scroll`` 与 control 同值，但保留独立键：滚动条滑块宽 8-10px，取 4px 恰好是
#: "药丸"；将来想单独调滑轨时不必动控件圆角。
RADIUS: Dict[str, int] = {"container": 6, "control": 4, "indicator": 3, "scroll": 4}


COLORS: Dict[str, str] = {
    # 基础三层灰：bg 最底、bg_alt 次底、panel 为卡片面
    "bg": "#1e1f26",
    "bg_alt": "#262832",
    "panel": "#2b2d38",
    "border": "#3a3d4a",
    "text": "#e6e6ea",
    "text_dim": "#9a9db0",
    "text_disabled": "#6e7284",
    # 品牌色：accent 只用于描边 / 焦点 / 滚动条；实心填充一律用 accent_strong，
    # 否则白字压 #4c8dff 只有 3.20:1，达不到 WCAG AA 的 4.5:1
    "accent": "#4c8dff",
    "accent_hover": "#5f9bff",
    "accent_strong": "#2a63d6",
    "accent_strong_hover": "#3369d9",
    "ok": "#3ecf8e",
    # warn 与 action 原本亮度相近、色相差 3°，同屏无法区分"警告"与"可触发"
    "warn": "#e08a3c",
    "err": "#f2545b",
    "danger_strong": "#c93a41",
    "action": "#ffb84d",
    "nick": "#7fd1ff",
    "sys": "#9a9db0",
    # 选中底：亮 accent 上压近白文字只有 2.57:1，改为同族深蓝（配白字 8.4:1）
    "selection": "#2f4d80",
    # 普通按钮要比所在的 panel 亮一档才算"抬升"，否则静止时凹陷、hover 时融进容器
    "btn": "#33364a",
    "btn_hover": "#3a3e54",
    "btn_border": "#464a5c",
    # 二维码底色必须是纯白：扫码依赖黑白模块的高对比，换成 panel 灰就扫不出来。
    # 唯一允许的"纯白面"写进 token，免得它作为硬编码散落在 login_dialog 里。
    "qr_bg": "#ffffff",
    # ---- 状态色（此前散落在 STYLESHEET 里写死，改名要全文搜十六进制）----
    "btn_disabled_border": "#34374a",
    "primary_pressed": "#22479c",
    "primary_disabled": "#2f4266",
    "danger_hover": "#d1434a",
    "danger_pressed": "#b93a41",
    "danger_disabled": "#4a2b30",
    # 压色底上的禁用文字：不能用 text_disabled（#6e7284）——它是给 bg_alt 配的，
    # 压在 #2f4266 上只剩 2.2:1，连"这是禁用态"都读不出来。禁用态 WCAG 豁免，
    # 但要让用户看清按钮上写的是什么，所以这里取一档更亮的灰。
    "disabled_on_tint": "#8b93a8",
}

STYLESHEET = f"""
QWidget {{
    background: {COLORS['bg']};
    color: {COLORS['text']};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}}
/* QWidget 的 background 会命中所有 QWidget 子类，于是在 #2b2d38 的卡片面上，
   QLabel / QCheckBox 各自铺出一块 #1e1f26 的"补丁"（实测两张卡片内 8 处、
   合计 8.3 万像素）。这里把"承载文字的控件"显式置透明，接缝即消失。
   必须写在 QWidget 规则之后：类型选择器的特异性高于 QWidget。 */
QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
QGroupBox {{
    background: {COLORS['panel']};
    border: 1px solid {COLORS['border']};
    border-radius: {RADIUS['container']}px;
    margin-top: 16px;
    padding-top: 8px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    /* 标题字形原本紧贴上边框（实测间隙 0-1px），靠下内距把它压下来 */
    padding: 0 4px 2px 4px;
    color: {COLORS['text']};
    font-weight: 700;
}}
QPushButton {{
    background: {COLORS['btn']};
    border: 1px solid {COLORS['btn_border']};
    border-radius: {RADIUS['control']}px;
    padding: 4px 16px;
    /* 输入类控件是 4+4+2+21=31px，按钮原来 30px，同排差 1px 会看出来 */
    min-height: 21px;
}}
QPushButton:hover {{ background: {COLORS['btn_hover']}; border-color: {COLORS['accent']}; }}
QPushButton:pressed {{ background: {COLORS['bg']}; }}
QPushButton:disabled {{
    background: {COLORS['bg_alt']};
    color: {COLORS['text_disabled']};
    border-color: {COLORS['btn_disabled_border']};
}}
QPushButton:checked {{
    background: {COLORS['accent_strong']};
    border-color: {COLORS['accent_strong']};
    color: #ffffff;
    font-weight: bold;
}}
QPushButton:checked:hover {{
    background: {COLORS['accent_strong_hover']};
    border-color: {COLORS['accent_strong_hover']};
}}
QPushButton#primary {{
    background: {COLORS['accent_strong']};
    border-color: {COLORS['accent_strong']};
    color: #ffffff;
    font-weight: bold;
}}
QPushButton#primary:hover {{
    background: {COLORS['accent_strong_hover']};
    border-color: {COLORS['accent_strong_hover']};
}}
QPushButton#primary:pressed {{
    background: {COLORS['primary_pressed']};
    border-color: {COLORS['primary_pressed']};
}}
QPushButton#primary:disabled {{
    background: {COLORS['primary_disabled']};
    border-color: {COLORS['primary_disabled']};
    color: {COLORS['disabled_on_tint']};
}}
QPushButton#danger {{
    background: {COLORS['danger_strong']};
    border-color: {COLORS['danger_strong']};
    color: #ffffff;
    font-weight: bold;
}}
QPushButton#danger:hover {{
    background: {COLORS['danger_hover']};
    border-color: {COLORS['danger_hover']};
}}
QPushButton#danger:pressed {{
    background: {COLORS['danger_pressed']};
    border-color: {COLORS['danger_pressed']};
}}
QPushButton#danger:disabled {{
    background: {COLORS['danger_disabled']};
    border-color: {COLORS['danger_disabled']};
    color: {COLORS['disabled_on_tint']};
}}
/* 键盘焦点必须与鼠标 hover 可区分：hover 是品牌色描边，focus 换成亮描边。
   带 ID 的按钮特异性更高，必须单独补，否则 #primary / #danger 永远拿不到焦点样式。 */
QPushButton:focus {{ border-color: {COLORS['text']}; }}
QPushButton#primary:focus, QPushButton#danger:focus {{ border-color: {COLORS['text']}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {COLORS['bg_alt']};
    border: 1px solid {COLORS['border']};
    border-radius: {RADIUS['control']}px;
    padding: 4px 6px;
    selection-background-color: {COLORS['selection']};
    selection-color: #ffffff;
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {COLORS['accent']};
}}
/* 输入类控件的禁用态：QLineEdit / QComboBox / QSpinBox 等基础规则里的
   color/background 会压过调色板，不显式写的话"禁用"在像素上等于"启用"
   （实测 filter_editor 的 detail_box 整块禁用后差异 0 像素）。
   底色刻意**不用** bg：输入框一旦画成页面底色，在卡片里就是一块"洞"，
   和页面漏底分不开（焦点窗口组默认整组禁用，实测 42754 像素的假洞）。
   也不用面板色"沉进卡片"以外的更暗色 —— 禁用面取 panel：比启用态浅、
   与卡片同面，读作"这一格现在不起作用"，但绝不会被误认成漏底。 */
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    background: {COLORS['panel']};
    color: {COLORS['text_disabled']};
    border-color: {COLORS['border']};
}}
QComboBox:disabled {{ padding-right: 24px; }}
QComboBox::drop-down:disabled {{ border: none; width: 20px; }}
QTableWidget:disabled, QTableView:disabled, QListWidget:disabled {{ color: {COLORS['text_disabled']}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {COLORS['text_disabled']}; }}
QGroupBox::title:disabled {{ color: {COLORS['text_disabled']}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox {{ padding-right: 24px; }}
/* 下拉 / 数字框的"箭头"实际渲染成实心小方块（8x5），不是三角。
   曾经以为 CSS 的 border 三角在 QSS 里同样成立 —— 实测不成立：Qt5 的 QSS
   不实现 border 三角，透明边不绘制、实体边铺满整个宽度，三角形退化为矩形。
   要做真三角得上 QProxyStyle + QPainterPath 重画 PE_IndicatorArrowDown；
   在引入那层之前，这里就按"指示块"来写：给它 1px 圆角，读起来是箭头槽而非脏点。
   无论如何，它比系统原生的浅色小方块（唯一未样式化时的样子）要协调得多。 */
QComboBox::down-arrow {{
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {COLORS['text_dim']};
    border-radius: 1px;
    margin-right: 8px;
}}
QSpinBox, QDoubleSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    background: {COLORS['panel']};
    border-left: 1px solid {COLORS['border']};
    border-top-right-radius: {RADIUS['control']}px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    background: {COLORS['panel']};
    border-left: 1px solid {COLORS['border']};
    border-top: 1px solid {COLORS['border']};
    border-bottom-right-radius: {RADIUS['control']}px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    width: 0;
    height: 0;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid {COLORS['text_dim']};
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    width: 0;
    height: 0;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid {COLORS['text_dim']};
}}
QComboBox QAbstractItemView {{
    background: {COLORS['panel']};
    border: 1px solid {COLORS['border']};
    selection-background-color: {COLORS['selection']};
    selection-color: #ffffff;
}}
QTableWidget, QTableView, QListWidget {{
    background: {COLORS['bg_alt']};
    border: 1px solid {COLORS['border']};
    border-radius: {RADIUS['control']}px;
    gridline-color: {COLORS['border']};
    selection-background-color: {COLORS['selection']};
    selection-color: #ffffff;
    alternate-background-color: {COLORS['panel']};
}}
QHeaderView::section {{
    background: {COLORS['panel']};
    color: {COLORS['text_dim']};
    border: none;
    border-right: 1px solid {COLORS['border']};
    border-bottom: 1px solid {COLORS['border']};
    padding: 4px 8px;
    font-weight: bold;
}}
QTabWidget::pane {{ border: 1px solid {COLORS['border']}; border-radius: {RADIUS['control']}px; top: -1px; }}
QTabBar::tab {{
    background: {COLORS['bg_alt']};
    border: 1px solid {COLORS['border']};
    border-bottom: none;
    border-top-left-radius: {RADIUS['control']}px;
    border-top-right-radius: {RADIUS['control']}px;
    padding: 8px 16px;
    margin-right: 2px;
    color: {COLORS['text_dim']};
}}
QTabBar::tab:selected {{ background: {COLORS['panel']}; color: {COLORS['text']}; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}
QCheckBox::indicator:unchecked {{
    background: {COLORS['bg_alt']}; border: 1px solid {COLORS['border']}; border-radius: {RADIUS['indicator']}px;
}}
QCheckBox::indicator:checked {{
    background: {COLORS['accent']}; border: 1px solid {COLORS['accent']}; border-radius: {RADIUS['indicator']}px;
}}
QScrollBar:vertical {{ background: {COLORS['bg']}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {COLORS['border']}; border-radius: {RADIUS['scroll']}px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {COLORS['accent']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
/* 滑块上下那两段"空槽"（add-page / sub-page）必须显式给底色。
   不定义的话 Qt 回落到 style 默认画刷，在深色主题上画出一串纯白网点——
   实测选项页竖滚动条 10x689 里有 127px 的滑轨全是 #ffffff 与 #1e1f26 交替。 */
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: {COLORS['bg_alt']};
}}
QScrollBar:horizontal {{ background: {COLORS['bg']}; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {COLORS['border']}; border-radius: {RADIUS['scroll']}px; min-width: 24px; }}
QLabel#hint {{ color: {COLORS['text_dim']}; font-size: 12px; }}
/* 段落标题：对话框里放在表格/表单上方的一行导语。它比正文重要、但没有分组框
   那么重，所以只给"正文色 + 加粗"——刻意不做成 QGroupBox，避免为一行字套边框。 */
QLabel#section {{ color: {COLORS['text']}; font-weight: 700; }}
/* 日志用等宽字体：时间戳与层级前缀靠列对齐才扫得动。
   字号/字族只在这里声明 —— 全局 QWidget 规则会覆盖 setFont()。 */
QPlainTextEdit#log {{
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 12px;
}}
QLabel#status_ok {{ color: {COLORS['ok']}; }}
QLabel#status_err {{ color: {COLORS['err']}; }}
QLabel#status_warn {{ color: {COLORS['warn']}; }}
QStatusBar {{ background: {COLORS['panel']}; color: {COLORS['text_dim']}; }}
QToolTip {{
    background: {COLORS['panel']}; color: {COLORS['text']};
    border: 1px solid {COLORS['border']}; padding: 4px;
}}
QSplitter::handle {{ background: {COLORS['border']}; width: 2px; }}
/* 分割条与表格网格线同色（#3a3d4a），静止时几乎找不到。加 hover 让"可拖"可发现。 */
QSplitter::handle:hover {{ background: {COLORS['accent']}; }}
"""

_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}


def escape_html(text: str) -> str:
    """转义 HTML。弹幕内容完全来自外部，**必须**转义后再进富文本控件。"""

    return "".join(_ESCAPES.get(ch, ch) for ch in str(text or ""))


def rich_paragraph(text: str, line_height: float = 1.6) -> str:
    """把多行说明文字包成"有行距的段落"。

    QSS 管不了段内行距（``line-height`` 是排版属性，Qt 样式表没有实现），
    所以多行说明只能靠富文本。实测 Qt 富文本对 ``line-height:160%`` 生效
    （同文本 64px → 120px），这是当前唯一能让长句中行距松下来的办法。

    注意：``text`` 必须是**可信内容**（允许 <b>/<br>），外部文本请先
    :func:`escape_html`，否则至少是排版错乱，最坏是注入。
    """

    body = str(text or "").replace("\n", "<br>").replace("\r", "")
    return f'<p style="line-height:{int(round(line_height * 100))}%">{body}</p>'


def style_combo(box: QComboBox, min_chars: int = 14) -> QComboBox:
    """给下拉框套上统一的宽度策略，返回原对象方便链式使用。

    默认的 ``AdjustToContents`` 会让宽度由**最长选项**决定：按键下拉里
    「左 Control　左 Ctrl（Left Ctrl）」这类条目会把整列撑到 300px 以上，
    而表格里同一种下拉又必须能压到一格宽。改成按"最少可见字符数"给下限，
    多出来的宽度交给布局分配 —— 同一个控件在表单里能拉满、在表格里能收窄。
    """

    box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(int(min_chars))
    return box


class EmptyState(QWidget):
    """空态提示：**叠在**目标控件上，而不是插进布局里。

    提示行如果和表格同处一个布局，显隐就会改变布局高度 —— 实测筛选编辑器
    从"有规则"切到"无规则"时，规则表 top 由 25 变 48、高度由 478 变 455，
    整列内容会跳 23px。这里用 ``QStackedLayout.StackAll`` 把提示浮在表上方：
    表格几何始终不变，空表时提示正好落在空白区域中央。

    ``target`` 需要有 ``rowCount()``（QTableWidget / QListWidget 都满足）。
    """

    def __init__(self, target: QWidget, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._target = target
        self._label = QLabel(text, self)
        self._label.setObjectName("hint")
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setWordWrap(True)
        # 提示不该拦住鼠标：它盖在表格上，点它应当照常落到空表（然后可以调表头排序等）
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 必须显式透明：QLabel 默认会取窗口底色，浮在表上就是一块暗方块
        self._label.setStyleSheet("background: transparent;")

        stack = QStackedLayout(self)
        stack.setStackingMode(QStackedLayout.StackAll)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.addWidget(target)
        stack.addWidget(self._label)
        self.refresh()

    def refresh(self) -> None:
        """按目标控件的行数切换提示。改变数据后调用。"""

        counter = getattr(self._target, "rowCount", None)
        count = int(counter()) if callable(counter) else 0
        self._label.setVisible(count == 0)


#: 非普通弹幕在列表里的类型标签与强调色。
#:
#: 旧版程序按消息类型给整行上色（弹幕绿、礼物红、进场青……），一屏里
#: 三四种颜色同时闪，读起来很吵。这里只给"不是聊天内容"的消息加一个小
#: 标签并把正文染色，普通弹幕保持默认文字色。
TYPE_STYLES = {
    "gift": ("礼物", "#ff6b6b"),
    "guard": ("上舰", "#ff69b4"),
    "super_chat": ("SC", "#ffd700"),
    "enter": ("进场", "#4cc9f0"),
    "follow": ("关注", "#c77dff"),
    "like": ("点赞", "#f4c95d"),
    "stats": ("在线", "#9a9db0"),
    "system": ("系统", "#9a9db0"),
}


def danmaku_html(
    text: str,
    nickname: str = "",
    timestamp: str = "",
    platform: str = "",
    actionable: bool = False,
    matched: str = "",
    kind: str = "",
) -> str:
    """生成一行弹幕的富文本 HTML。

    :param kind: 消息类型（``MessageType`` 的值）。普通弹幕传空串即可；
        礼物/进场/点赞等会带一个彩色标签。
    """

    parts = []
    if timestamp:
        parts.append(f'<span style="color:{COLORS["sys"]}">[{escape_html(timestamp)}]</span>')
    if platform:
        parts.append(f'<span style="color:{COLORS["sys"]}">[{escape_html(platform)}]</span>')
    label, label_color = TYPE_STYLES.get(str(kind or "").strip().lower(), ("", ""))
    if label:
        parts.append(f'<span style="color:{label_color}">〔{escape_html(label)}〕</span>')
    if nickname:
        parts.append(f'<span style="color:{COLORS["nick"]}">{escape_html(nickname)}：</span>')
    if actionable:
        color = COLORS["action"]
    elif label_color:
        color = label_color
    else:
        color = COLORS["text"]
    parts.append(f'<span style="color:{color}">{escape_html(text)}</span>')
    if actionable and matched:
        parts.append(f'<span style="color:{COLORS["ok"]}"> → {escape_html(matched)}</span>')
    return "".join(parts)
