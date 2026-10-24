"""主窗口。

对照参考项目（``main_window.py`` 37KB 单文件）的改进
--------------------------------------------------
* **拆分**：连接 / 映射 / 筛选 / 统计四个页签各自独立控件类，
  主窗口只做编排，不再是一个巨型类。
* **凭证持久化**：参考项目把 SESSDATA 存在配置里但鼠标映射忘了存；
  这里统一走 :class:`ConfigManager`，关闭时保存、启动时恢复，含窗口几何。
* **状态如实呈现**：连接成功/失败、B 站 -352 风控、抖音 signature 缺失，
  都在状态栏和日志页明确写出，而不是显示"已连接"却收不到弹幕。
* **热键**：``ctrl+shift+p`` 暂停/恢复输入（可在配置里改）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from PyQt5.QtCore import QPoint, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QColor, QDesktopServices
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QDoubleSpinBox,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config.manager import ConfigManager
from ..config.models import AppConfig, DisplayConfig, DouyinMode, Platform
from ..danmaku.message import DanmakuMessage
from ..engine import DanmakuKeyboardEngine
from ..filters.rules import FilterEngine
from ..input.hotkey import PYNPUT_AVAILABLE, GlobalHotkey
from ..input.keycodes import injection_environment_problem
from ..input.mappings import KeyMapping, MouseAction, MouseMapping
from ..input.window import (
    FIELD_LABELS,
    FOCUS_FIELDS,
    FOCUS_MATCH_MODES,
    MODE_LABELS,
    WINDOWS_API,
    list_windows,
)
from ..utils.logging_setup import get_logger
from .danmaku_window import DanmakuWindow
from .filter_editor import FilterEditorDialog
from .login_dialog import LoginDialog
from .mapping_editor import MappingEditorDialog, action_label
from .theme import COLORS, style_combo

from .worker import EngineBridge

logger = get_logger(__name__)


def credential_summary(connection: Any, platform: Platform) -> str:
    """生成「两个平台各自存了什么凭证」的状态行。

    两个平台的凭证分别存在 ``connection.sessdata``（B 站）和
    ``connection.cookie``（抖音），界面上却只有一个输入框，而且它只显示
    **当前平台**的那份。于是在 B 站平台下打开程序时，抖音 Cookie 明明
    还在 settings.json 里，框里却是空的 SESSDATA —— 看起来就像"cookie 丢了"。
    这一行把两边的状态都摊开写清楚，避免误判。

    :param connection: ``AppConfig.connection``（或任何带 sessdata/cookie 的对象）
    :param platform: 当前选中的平台，用于补一句"另一份在哪看"
    """
    from ..login.douyin import parse_cookie_string, sanitize_cookie_header

    cookie_text = getattr(connection, "cookie", "") or ""
    sessdata = getattr(connection, "sessdata", "") or ""
    cookies = parse_cookie_string(cookie_text) if cookie_text else {}

    if cookies:
        quality = "含 ttwid" if cookies.get("ttwid") else "缺少 ttwid，可能被反爬拦截"
        douyin_text = f"抖音 Cookie 已保存（{len(cookies)} 项，{quality}）"
    else:
        douyin_text = "抖音 Cookie 未保存"
    bili_text = "B 站 SESSDATA 已保存" if sessdata else "B 站 SESSDATA 未保存"

    text = "凭证状态：" + "　｜　".join([bili_text, douyin_text])

    # Cookie 里混进了 HTTP 头放不下的字符（最常见是复制时被换成「…」）。
    # 这种串 ttwid 还在，解析/保存/校验全都"通过"，一直要到握手才炸成
    # "invalid Cookie header: <整串>" —— 用户只会以为"连接不上直播间"。
    # 所以这里必须提前把问题摆在界面上。
    problem = ""
    if cookie_text:
        sanitized = sanitize_cookie_header(cookie_text)
        if sanitized.truncated:
            problem = (
                "⚠ Cookie 中间有「…」省略号：复制时那段被省略了，值不完整。"
                "请重新完整复制（F12 → Network → 请求头里的整行 Cookie）。"
            )
        elif sanitized.changed:
            problem = "⚠ Cookie 里有非法字符，已在连接时自动清理：" + "；".join(
                sanitized.notes
            )
    if problem:
        text += "\n" + problem

    current_missing = (platform == Platform.BILIBILI and not sessdata) or (
        platform == Platform.DOUYIN and not cookies
    )
    other_saved = (platform == Platform.BILIBILI and bool(cookies)) or (
        platform == Platform.DOUYIN and bool(sessdata)
    )
    if current_missing and other_saved:
        other = "抖音" if platform == Platform.BILIBILI else "B 站"
        text += (
            f"\n注意：上面框里只显示「{platform.label}」的凭证；"
            f"{other}的凭证已保存，切换平台即可看到。"
        )
    return text

__all__ = ["MainWindow"]


class MainWindow(QMainWindow):
    """弹幕键盘主窗口。"""

    #: 全局热键被按下。**必须走信号而不是直接调控件**：``GlobalHotkey`` 的回调
    #: 在 pynput 的监听线程里同步执行，而暂停按钮的 ``toggle()`` 会连到
    #: :meth:`_on_pause_input`，那里会改 ``pause_input``/``enable_input`` 两个控件
    #: 并写日志框 —— 在非 GUI 线程碰控件轻则
    #: ``QObject: Cannot create children for a parent that is in a different thread``，
    #: 重则随机崩溃。信号跨线程默认是 QueuedConnection，会自动排到 GUI 线程执行。
    hotkeyTriggered = pyqtSignal()

    def __init__(self, config: Optional[AppConfig] = None, config_manager: Optional[ConfigManager] = None) -> None:
        super().__init__()

        self.manager = config_manager or ConfigManager()
        if config is None:
            self.manager.load()
            config = self.manager.config
        self.config = config

        # 筛选规则从配置里恢复（过去这里硬编码 ``FilterEngine.default()``，
        # 而 ConfigManager 又不保存筛选规则，于是用户在界面上编辑的规则
        # 重启一次就没了 —— 日志里那句"如需永久保存请写入配置文件"就是当时的
        # 免责声明）。现在两者都补齐了。
        if self.manager.filters:
            filters = FilterEngine(list(self.manager.filters))
        else:
            filters = FilterEngine.default()

        self.engine = DanmakuKeyboardEngine(
            config=self.config,
            key_mappings=list(self.manager.key_mappings),
            mouse_mappings=list(self.manager.mouse_mappings),
            filters=filters,
        )
        self.bridge = EngineBridge(self.engine, self)

        self.danmaku_window: Optional[DanmakuWindow] = None
        self.hotkey: Optional[GlobalHotkey] = None
        # 选项页每拖一次滑块都会发 valueChanged，直接写盘等于每帧一次
        # mkstemp+fsync+os.replace。用 800ms 防抖攒起来一次写完；
        # closeEvent 里的 _persist() 仍会兜底保证最后一次一定落盘。
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self._persist)

        self._build_ui()
        self._wire_signals()
        self._restore_geometry()
        self._start_hotkey()
        self._apply_initial_pause_state()

        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(1000)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start()

        self.log("弹幕键盘 V2 已启动。")
        if not PYNPUT_AVAILABLE:
            self.log("提示：未安装 pynput，全局热键不可用（pip install pynput）。")
        # 一进来就说清楚"这个环境里注入会不会被系统丢掉"，别等用户去猜。
        self._check_injection_environment()

    # ================================================================= UI
    def _build_ui(self) -> None:
        self.setWindowTitle("弹幕键盘 V2")
        self.setMinimumSize(760, 560)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 4)
        outer.setSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("主功能页签")
        self.tabs.addTab(self._build_connect_tab(), "连接")
        self.tabs.addTab(self._build_mapping_tab(), "映射")
        self.tabs.addTab(self._build_options_tab(), "选项")
        self.tabs.addTab(self._build_log_tab(), "日志")
        outer.addWidget(self.tabs, 1)

        # 底部主控条
        control = QHBoxLayout()
        control.setSpacing(8)
        self.connect_btn = QPushButton("开始接收")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setMinimumWidth(130)
        # 可访问名：屏幕阅读器/自动化按名字找控件，而按钮上写的是动作、不是对象
        self.connect_btn.setAccessibleName("开始接收弹幕")
        control.addWidget(self.connect_btn)

        self.stop_btn = QPushButton("停止接收")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setAccessibleName("停止接收弹幕")
        self.stop_btn.setEnabled(False)
        control.addWidget(self.stop_btn)

        control.addSpacing(12)
        self.pause_input = QPushButton("暂停按键输出")
        self.pause_input.setCheckable(True)
        self.pause_input.setAccessibleName("暂停按键输出")
        self.pause_input.setToolTip(f"暂停/恢复按键输出（全局热键 {self.config.input.toggle_hotkey}）")
        control.addWidget(self.pause_input)

        self.show_danmaku_btn = QPushButton("显示弹幕窗")
        self.show_danmaku_btn.setAccessibleName("打开弹幕窗")
        control.addWidget(self.show_danmaku_btn)

        control.addStretch(1)

        self.conn_label = QLabel("未连接")
        self.conn_label.setObjectName("status_warn")
        control.addWidget(self.conn_label)
        outer.addLayout(control)

        bar = QStatusBar()
        self.setStatusBar(bar)
        self.status_label = QLabel("就绪")
        bar.addWidget(self.status_label)
        self.stats_label = QLabel("")
        bar.addPermanentWidget(self.stats_label)

    @staticmethod
    def _form(box: QWidget) -> QFormLayout:
        """统一的表单排版。

        过去只有连接页显式设了行距，其余 4 个 ``QFormLayout`` 走系统默认（≈6），
        于是同一应用里表单行距两套值、页签一切换就能感到松紧跳变；标签默认左对齐，
        冒号不成列。这里一次定死：行距 8 / 列距 12 / 标签右对齐 / 字段自适应拉伸。
        """

        form = QFormLayout(box)
        form.setVerticalSpacing(8)
        form.setHorizontalSpacing(12)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return form

    # ------------------------------------------------------------ 连接页
    def _build_connect_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(16)

        box = QGroupBox("直播间")
        form = self._form(box)

        self.platform_combo = style_combo(QComboBox(), 12)
        for p in (Platform.BILIBILI, Platform.DOUYIN):
            self.platform_combo.addItem(p.label, p)
        idx = self.platform_combo.findData(self.config.connection.platform)
        if idx >= 0:
            self.platform_combo.setCurrentIndex(idx)
        form.addRow("平台：", self.platform_combo)

        room_row = QHBoxLayout()
        self.room_edit = QLineEdit(self.config.connection.room)
        self.room_edit.setAccessibleName("直播间房间号或网址")
        self.room_edit.setPlaceholderText(self.config.connection.platform.default_room_hint)
        room_row.addWidget(self.room_edit, 1)
        self.room_hint = QLabel("")
        self.room_hint.setObjectName("hint")
        room_row.addWidget(self.room_hint)
        form.addRow("房间号 / 网址：", room_row)

        cred_row = QHBoxLayout()
        self.cred_edit = QLineEdit()
        self.cred_edit.setAccessibleName("登录凭证（Cookie 或 SESSDATA）")
        self.cred_edit.setEchoMode(QLineEdit.Password)
        cred_row.addWidget(self.cred_edit, 1)
        self.login_btn = QPushButton("获取凭证…")
        cred_row.addWidget(self.login_btn)
        self.show_cred = QCheckBox("显示")
        self.show_cred.toggled.connect(
            lambda on: self.cred_edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        cred_row.addWidget(self.show_cred)
        form.addRow("凭证：", cred_row)

        self.cred_hint = QLabel("")
        self.cred_hint.setObjectName("hint")
        self.cred_hint.setWordWrap(True)
        form.addRow("", self.cred_hint)

        # 两个平台的凭证分别存在 config 的两处，界面上却只有一个输入框，
        # 且它只显示**当前平台**的凭证 —— 另一个平台存过的凭证在切换过去
        # 之前完全看不见，用户会以为"cookie 没了"。这一行把两边都列出来。
        self.cred_saved = QLabel("")
        self.cred_saved.setObjectName("hint")
        self.cred_saved.setWordWrap(True)
        form.addRow("", self.cred_saved)

        # 配置文件到底在读哪一份。用户目录不可写时程序会回落到程序旁边的
        # .danmaku_kb（见 config.manager.writable_config_dir），两份配置长得很像、
        # 内容却可能完全不同 —— 不写出来，用户只能看到"我填的没了"。
        cfg_row = QHBoxLayout()
        self.config_path_label = QLabel("")
        self.config_path_label.setObjectName("hint")
        self.config_path_label.setWordWrap(True)
        self.config_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cfg_row.addWidget(self.config_path_label, 1)
        self.open_cfg_btn = QPushButton("打开目录")
        self.open_cfg_btn.setToolTip("用资源管理器打开当前使用的配置目录")
        cfg_row.addWidget(self.open_cfg_btn)
        form.addRow("配置文件：", cfg_row)

        layout.addWidget(box)

        # 模拟模式
        sim_box = QGroupBox("测试（不发真实弹幕，用内置样例）")
        sl = QVBoxLayout(sim_box)
        self.sim_check = QCheckBox("启用模拟弹幕（不连接任何平台，适合先验证映射是否正确）")
        sl.addWidget(self.sim_check)
        sim_row = QHBoxLayout()
        sim_row.addWidget(QLabel("发送间隔（秒）："))
        self.sim_interval = QDoubleSpinBox()
        self.sim_interval.setRange(0.1, 10.0)
        self.sim_interval.setSingleStep(0.1)
        self.sim_interval.setValue(0.8)
        sim_row.addWidget(self.sim_interval)
        self.sim_once_btn = QPushButton("发一条")
        sim_row.addWidget(self.sim_once_btn)
        self.sim_seq_btn = QPushButton("发一组")
        sim_row.addWidget(self.sim_seq_btn)
        sim_row.addStretch(1)
        sl.addLayout(sim_row)
        layout.addWidget(sim_box)

        # 平台可用性说明原来是 4 行富文本压在首屏底部：新用户第一眼先读到
        # "可能连不上"，注意力被从「开始接收弹幕」上拽走。压成一行结论，
        # 细节留在 tooltip —— 想深究的人 hover 就有，不想看的人不必先读四行。
        note = QLabel(
            "平台提示：B 站报 -352 是风控（需补全 Cookie）；抖音需要 ttwid，"
            "少数房间还要 signature。连不上时程序会如实显示服务端回应。"
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        note.setToolTip(
            "<b>关于平台可用性（如实说明）</b><br>"
            "• <b>B站</b>：基于 blivedm，功能完整。若报 <code>-352</code>，是 B 站对弹幕服务器"
            "信息接口新增的风控，需在浏览器登录后补全 Cookie，或稍后重试。<br>"
            "• <b>抖音</b>：走真实 protobuf WebSocket。若连接后收不到弹幕，通常是缺少 "
            "<code>ttwid</code> 或服务端要求 <code>signature</code>；程序会把服务端的真实回应"
            "显示出来，不会假装连上。"
        )
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------ 映射页
    def _build_mapping_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        row = QHBoxLayout()
        edit_btn = QPushButton("编辑映射…")
        edit_btn.setObjectName("primary")
        edit_btn.clicked.connect(self._edit_mappings)
        row.addWidget(edit_btn)
        reset_btn = QPushButton("恢复默认映射")
        reset_btn.clicked.connect(self._reset_mappings)
        row.addWidget(reset_btn)
        row.addStretch(1)
        self.mapping_count = QLabel("")
        self.mapping_count.setObjectName("hint")
        row.addWidget(self.mapping_count)
        layout.addLayout(row)

        self.mapping_table = QTableWidget(0, 6)
        self.mapping_table.setHorizontalHeaderLabels(["类型", "触发词", "输出", "动作", "冷却", "说明"])
        self.mapping_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.mapping_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.mapping_table.verticalHeader().setVisible(False)
        self.mapping_table.setAlternatingRowColors(True)
        header = self.mapping_table.horizontalHeader()
        # 列宽按内容分工：「触发词」与「说明」是长短不一的自由文本，占剩余宽度；
        # 「动作」是固定词表（"按下并松开（点按）"实测 117px），必须按内容给宽，
        # 否则每行都被省略号截成"按下并松开（…"。
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Interactive)
        self.mapping_table.setColumnWidth(5, 220)
        layout.addWidget(self.mapping_table, 1)

        test_row = QHBoxLayout()
        test_row.addWidget(QLabel("试一下："))
        self.test_edit = QLineEdit()
        self.test_edit.setPlaceholderText("输入一条弹幕内容，例如 666666 或 w")
        test_row.addWidget(self.test_edit, 1)
        test_btn = QPushButton("测试匹配")
        test_btn.clicked.connect(self._test_match)
        test_row.addWidget(test_btn)
        layout.addLayout(test_row)

        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setObjectName("hint")
        layout.addWidget(self.test_result)

        self._refresh_mapping_table()
        return page

    # ------------------------------------------------------------ 选项页
    def _build_options_tab(self) -> QWidget:
        # 选项页 4 个组叠起来约 680px，而窗口最小高度 560 时可用区只有约
        # 460px（扣掉标签栏/底栏/状态栏）。没有滚动容器时 QVBoxLayout 只能把
        # 各组压到 minimumSizeHint 以下 —— 控件互相挤压、文字被裁，最后两组
        # 直接滑出可视区且滚不到。滚动手感由 theme.py 的 QScrollBar 提供。
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(16)  # 组间(16) > 组内行距(8)，分组边界才立得住

        keys = QGroupBox("按键输出")
        kf = self._form(keys)
        self.enable_input = QCheckBox("启用按键输出（关掉后只显示弹幕，不模拟按键）")
        self.enable_input.setChecked(bool(self.config.input.enabled))
        kf.addRow(self.enable_input)

        self.game_mode = QCheckBox("游戏模式（用扫描码发送，兼容 DirectInput 游戏）")
        self.game_mode.setChecked(bool(self.config.input.game_mode))
        self.game_mode.setToolTip("普通模式用 SendInput；游戏模式改用扫描码 + keybd_event，很多游戏只认后者")
        kf.addRow(self.game_mode)

        self.max_actions = QSpinBox()
        self.max_actions.setRange(1, 10)
        self.max_actions.setValue(int(self.config.input.max_actions_per_message))
        kf.addRow("单条弹幕最多触发：", self.max_actions)

        self.global_cd = QDoubleSpinBox()
        self.global_cd.setRange(0.0, 60.0)
        self.global_cd.setSingleStep(0.05)
        self.global_cd.setDecimals(2)
        self.global_cd.setValue(float(self.config.input.global_cooldown))
        kf.addRow("全局动作间隔（秒）：", self.global_cd)

        self.user_cd = QDoubleSpinBox()
        self.user_cd.setRange(0.0, 60.0)
        self.user_cd.setSingleStep(0.1)
        self.user_cd.setDecimals(2)
        self.user_cd.setValue(float(self.config.input.per_user_cooldown))
        kf.addRow("同一用户冷却（秒）：", self.user_cd)

        self.dedupe = QDoubleSpinBox()
        self.dedupe.setRange(0.0, 60.0)
        self.dedupe.setSingleStep(0.1)
        self.dedupe.setDecimals(2)
        self.dedupe.setValue(float(self.config.input.dedupe_window))
        kf.addRow("相同内容去重窗口（秒）：", self.dedupe)

        self.hotkey_edit = QLineEdit(self.config.input.toggle_hotkey)
        kf.addRow("暂停/恢复热键：", self.hotkey_edit)
        layout.addWidget(keys)

        layout.addWidget(self._build_focus_group())

        disp = QGroupBox("弹幕显示")
        df = self._form(disp)
        self.max_lines = QSpinBox()
        self.max_lines.setRange(50, 20000)
        self.max_lines.setValue(int(self.config.display.max_lines))
        df.addRow("最多显示行数：", self.max_lines)

        self.font_size = QSpinBox()
        self.font_size.setRange(8, 32)
        self.font_size.setValue(int(self.config.display.font_size))
        # 与全文一致：这个数字就是像素值（别处再没有第二套单位）
        self.font_size.setToolTip(
            "弹幕窗字号，单位像素。界面正文约 13px，弹幕窗常用 14-16px。\n"
            "旧版这个数字按「磅」渲染（12 磅≈16px），想保持原来的观感请填 16。"
        )
        df.addRow("字号（像素）：", self.font_size)

        self.show_ts = QCheckBox("显示时间")
        self.show_ts.setChecked(bool(self.config.display.show_timestamp))
        df.addRow(self.show_ts)
        self.show_nick = QCheckBox("显示昵称")
        self.show_nick.setChecked(bool(self.config.display.show_nickname))
        df.addRow(self.show_nick)
        self.show_plat = QCheckBox("显示平台")
        self.show_plat.setChecked(bool(self.config.display.show_platform))
        df.addRow(self.show_plat)
        layout.addWidget(disp)

        # 「筛选规则」原来是一个只装着一颗按钮的 QGroupBox：边框比内容还醒目，
        # 分组框的层级被一个"入口级"操作占用了。降级为页脚操作行 —— 说明缩成提示，
        # 按钮靠右，和上面的分组之间靠组间距拉开。
        footer = QHBoxLayout()
        footer.setSpacing(8)
        filt_hint = QLabel("筛选规则决定哪些弹幕能触发按键（默认忽略「进入直播间」这类事件）。")
        filt_hint.setObjectName("hint")
        filt_hint.setWordWrap(True)
        footer.addWidget(filt_hint, 1)
        edit_f = QPushButton("编辑筛选规则…")
        edit_f.setAccessibleName("编辑筛选规则")
        edit_f.clicked.connect(self._edit_filters)
        footer.addWidget(edit_f, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addLayout(footer)

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        return scroll

    def _build_focus_group(self) -> QGroupBox:
        """「焦点窗口」设置组。

        这个功能一句话：**只有指定窗口是前台时才真的发按键**。做直播/挂机
        的人最怕的是弹幕按键打进了浏览器、聊天窗、甚至自己的编辑器里，所以
        默认关、并且开着的时候界面上有一行实时状态告诉你"现在会不会注入"，
        而不是等到发现游戏没反应/别处乱打字才知道。
        """
        focus = QGroupBox("焦点窗口（只在指定窗口为前台时才注入按键）")
        ff = self._form(focus)
        cfg = self.config.input.focus_filter

        self.focus_enable = QCheckBox("启用：非指定窗口在前台时不发送任何按键")
        self.focus_enable.setChecked(bool(cfg.enabled))
        self.focus_enable.setToolTip(
            "关掉＝往当前前台窗口发（旧版行为）。开启后，只有当下面填的目标窗口\n"
            "是前台窗口时才会注入键鼠；其它窗口一律不发。"
        )
        ff.addRow(self.focus_enable)

        self.focus_field = style_combo(QComboBox(), 14)
        for name in FOCUS_FIELDS:
            self.focus_field.addItem(FIELD_LABELS.get(name, name), name)
        self._select_combo_data(self.focus_field, cfg.field)
        self.focus_field.setToolTip(
            "比对窗口的哪个属性。游戏标题常带等级/房间名，会变；\n"
            "进程名（如 GenshinImpact.exe）最稳定，推荐先用它。"
        )
        ff.addRow("比对目标：", self.focus_field)

        self.focus_mode = style_combo(QComboBox(), 12)
        for name in FOCUS_MATCH_MODES:
            self.focus_mode.addItem(MODE_LABELS.get(name, name), name)
        self._select_combo_data(self.focus_mode, cfg.match_mode)
        ff.addRow("匹配方式：", self.focus_mode)

        self.focus_pattern = QLineEdit(cfg.pattern)
        self.focus_pattern.setPlaceholderText("例如：GenshinImpact.exe 或 原神")
        ff.addRow("目标内容：", self.focus_pattern)

        self.focus_case = QCheckBox("区分大小写")
        self.focus_case.setChecked(bool(cfg.case_sensitive))
        ff.addRow(self.focus_case)

        pick = QPushButton("从当前打开的窗口选择…")
        pick.clicked.connect(self._pick_focus_window)
        pick.setEnabled(bool(WINDOWS_API))
        if not WINDOWS_API:
            pick.setToolTip("当前系统读不到窗口列表（仅 Windows 支持）。")
        refresh = QPushButton("刷新状态")
        refresh.clicked.connect(self._refresh_focus_status)
        row = QHBoxLayout()
        row.addWidget(pick)
        row.addWidget(refresh)
        row.addStretch(1)
        ff.addRow(row)

        self.focus_status = QLabel("")
        self.focus_status.setWordWrap(True)
        ff.addRow(self.focus_status)
        return focus

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: object) -> None:
        """按 ``itemData`` 选中；找不到就留在第 0 项。"""
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _focus_detail_widgets(self) -> List[QWidget]:
        """焦点过滤的明细控件（关掉总开关时整体置灰）。"""
        return [
            self.focus_field,
            self.focus_mode,
            self.focus_pattern,
            self.focus_case,
        ]

    def _refresh_focus_status(self) -> None:
        """刷新那一行实时状态。

        状态文案由引擎给（它才知道 ``dry_run`` 与当前配置），界面不自己判断
        "到底会不会注入"——两处各写一套判断迟早会不一致。
        """
        if not hasattr(self, "focus_status"):
            return
        enabled = self.focus_enable.isChecked()
        for widget in self._focus_detail_widgets():
            widget.setEnabled(enabled)
        if not enabled:
            self.focus_status.setText("未启用：按键会注入到当前任意窗口。")
            return
        self.focus_status.setText(self.engine.focus_status_text())

    def _on_focus_changed(self) -> None:
        """焦点过滤设置变更 → 写配置 + 防抖落盘 + 刷状态。"""
        cfg = self.config.input.focus_filter
        cfg.enabled = self.focus_enable.isChecked()
        cfg.field = self.focus_field.currentData() or "title"
        cfg.match_mode = self.focus_mode.currentData() or "contains"
        # 用户从别处复制窗口标题时很容易带上首尾空格，这里就去掉：留着会
        # 变成"看着一模一样但就是匹配不上"，极难自查。
        cfg.pattern = self.focus_pattern.text().strip()
        cfg.case_sensitive = self.focus_case.isChecked()
        self._refresh_focus_status()
        self._schedule_save()

    def _pick_focus_window(self) -> None:
        """列出当前可见窗口，选一个把它的字段值填进目标内容。"""
        try:
            infos = list_windows()
        except Exception as exc:  # pragma: no cover - 平台相关
            logger.warning("枚举窗口失败：%s", exc)
            QMessageBox.warning(self, "读取窗口列表失败", f"没能读取窗口列表：{exc}")
            return
        if not infos:
            QMessageBox.information(
                self,
                "没有可选的窗口",
                "没读到任何可见窗口。请先把目标窗口打开（不要最小化），再点这里。",
            )
            return
        field = self.focus_field.currentData() or "title"
        labels = []
        seen = set()
        for info in infos:
            value = info.field_value(field)
            if not value or value in seen:
                continue
            seen.add(value)
            labels.append(value)
        if not labels:
            QMessageBox.information(
                self,
                "没有可选的窗口",
                "这些窗口里没有可用的%s。换成「进程名」再试一次。" % FIELD_LABELS.get(field, field),
            )
            return
        chosen, ok = QInputDialog.getItem(
            self, "选择焦点窗口", f"只会注入到选中的这个{ FIELD_LABELS.get(field, field) }：", labels, 0, False
        )
        if ok and chosen:
            self.focus_pattern.setText(chosen)
            self._on_focus_changed()
            self.log(f"焦点窗口目标已设为：{chosen}")

    # ------------------------------------------------------------ 日志页
    def _build_log_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(lambda: self.log_view.clear())
        toolbar.addWidget(clear_btn)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        # 统计从"一行 9 项、全角竖线分隔"改成固定 3 行 × 3 项：
        # 760px 窗口下原来那行会自己折行，折点取决于当前数值长度，读起来是碎的。
        # 现在换行位置由代码决定，行内用全角空格（中文排版里比竖线更安静）。
        self.stat_view = QLabel("尚无数据")
        self.stat_view.setObjectName("hint")
        self.stat_view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.stat_view.setWordWrap(False)
        layout.addWidget(self.stat_view)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        # 字体走 QSS（QPlainTextEdit#log）：应用级 QSS 的 QWidget{font-size:13px}
        # 会在 polish 时覆盖 setFont()，所以原来那句 QFont("Consolas", 9) 是死代码 ——
        # 日志一直用界面正文字体/字号在渲染。
        self.log_view.setObjectName("log")
        self.log_view.setMaximumBlockCount(3000)
        layout.addWidget(self.log_view, 1)
        return page

    # ============================================================ 信号接线
    def _wire_signals(self) -> None:
        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)
        self.room_edit.editingFinished.connect(self._save_room)
        self.login_btn.clicked.connect(self._open_login)
        self.open_cfg_btn.clicked.connect(self._open_config_dir)
        self.connect_btn.clicked.connect(self._on_connect)
        self.stop_btn.clicked.connect(self._on_stop)
        self.pause_input.toggled.connect(self._on_pause_input)
        self.show_danmaku_btn.clicked.connect(self._show_danmaku_window)
        self.sim_check.toggled.connect(self._on_sim_toggled)
        self.sim_once_btn.clicked.connect(self._sim_once)
        self.sim_seq_btn.clicked.connect(self._sim_sequence)
        self.game_mode.toggled.connect(self._on_game_mode)
        self.enable_input.toggled.connect(self._on_enable_input)
        self.max_actions.valueChanged.connect(self._on_options_changed)
        self.global_cd.valueChanged.connect(self._on_options_changed)
        self.user_cd.valueChanged.connect(self._on_options_changed)
        self.dedupe.valueChanged.connect(self._on_options_changed)
        self.max_lines.valueChanged.connect(self._on_options_changed)
        self.font_size.valueChanged.connect(self._on_options_changed)
        self.show_ts.toggled.connect(self._on_options_changed)
        self.show_nick.toggled.connect(self._on_options_changed)
        self.show_plat.toggled.connect(self._on_options_changed)
        self.hotkey_edit.editingFinished.connect(self._restart_hotkey)

        # 焦点窗口过滤：改任何一项都立即生效（引擎每次判定都读当前配置）
        self.focus_enable.toggled.connect(self._on_focus_changed)
        self.focus_field.currentIndexChanged.connect(self._on_focus_changed)
        self.focus_mode.currentIndexChanged.connect(self._on_focus_changed)
        self.focus_case.toggled.connect(self._on_focus_changed)
        self.focus_pattern.editingFinished.connect(self._on_focus_changed)
        self._refresh_focus_status()

        # 热键监听线程 → GUI 线程的桥（跨线程自动排队，见 hotkeyTriggered 注释）
        self.hotkeyTriggered.connect(self._on_hotkey_triggered)

        self.bridge.messageReceived.connect(self._on_message)
        self.bridge.actionTriggered.connect(self._on_action)
        self.bridge.statusChanged.connect(self._on_status)
        self.bridge.errorOccurred.connect(self._on_error)
        self.bridge.warningOccurred.connect(self._on_warning)
        self.bridge.statsUpdated.connect(self._on_stats_updated)

        self._on_platform_changed()

    # ============================================================ 连接控制
    def _current_platform(self) -> Platform:
        data = self.platform_combo.currentData()
        return data if isinstance(data, Platform) else Platform.BILIBILI

    def _on_platform_changed(self) -> None:
        platform = self._current_platform()
        self.room_edit.setPlaceholderText(platform.default_room_hint)
        if platform == Platform.BILIBILI:
            self.cred_hint.setText("B 站：点「获取凭证」扫码，得到 SESSDATA。不填也能连，但拿不到用户信息（UID 会是 0）。")
            self.cred_edit.setPlaceholderText("SESSDATA（可留空）")
        else:
            self.cred_hint.setText(
                "抖音：粘贴浏览器里的 Cookie，<b>至少要有 ttwid</b>，否则会拿到反爬验证页。"
                "点「获取凭证」有分步引导。"
            )
            self.cred_edit.setPlaceholderText("ttwid=...; sessionid=...（建议填写）")
        self._load_credential()

    def _load_credential(self) -> None:
        conn = self.config.connection
        if self._current_platform() == Platform.BILIBILI:
            self.cred_edit.setText(conn.sessdata)
        else:
            self.cred_edit.setText(conn.cookie)
        self._refresh_cred_summary()

    def _refresh_cred_summary(self) -> None:
        """刷新「两个平台各自存了什么凭证」那一行（逻辑见 credential_summary）。"""
        self.cred_saved.setText(
            credential_summary(self.config.connection, self._current_platform())
        )
        self._refresh_config_path()

    def _config_path(self):
        """当前实际读写的配置文件路径（没有就返回 None）。"""
        return getattr(self.manager, "settings_path", None)

    def _refresh_config_path(self) -> None:
        """把「配置文件：<绝对路径>」写出来。

        ``writable_config_dir()`` 在用户目录不可写时会回落到程序旁边的
        ``.danmaku_kb``，两份 settings.json 互不相干。用户看到"填的 cookie
        没了"，九成是因为程序读的是另一份 —— 所以路径必须摆在界面上。
        """
        path = self._config_path()
        if path is None:
            self.config_path_label.setText("未知")
            return
        exists = path.exists()
        text = f"{path}" + ("" if exists else "（尚未保存过）")
        try:
            from ..config.manager import default_config_dir

            if Path(path).parent != default_config_dir():
                # 回落到程序旁边了：这不是"设置丢了"，是不同的文件，
                # 必须让用户一眼看见，否则他会以为程序把配置清空了。
                text += "（用户目录不可写，已回落到程序目录）"
        except Exception:  # pragma: no cover - 防御性
            pass
        self.config_path_label.setText(text)

    def _open_config_dir(self) -> None:
        """用资源管理器打开配置目录，方便用户自己核对 settings.json。"""
        path = self._config_path()
        if path is None:
            return
        folder = path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log(f"配置目录打不开：{exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self.log(f"系统没有打开这个目录，请手动打开：{folder}")

    def _select_platform(self, platform: Platform) -> None:
        """把平台下拉框切到指定平台（会触发 _on_platform_changed）。"""
        for index in range(self.platform_combo.count()):
            if self.platform_combo.itemData(index) == platform:
                self.platform_combo.setCurrentIndex(index)
                return

    def _save_room(self) -> None:
        self.config.connection.room = self.room_edit.text().strip()

    def _open_login(self) -> None:
        platform = self._current_platform().value
        # 用 with 语义式地保证释放：LoginDialog 持有 QPixmap（二维码图片）和
        # 一个轮询线程引用，反复打开而不 deleteLater() 会一直累积。
        dialog = LoginDialog(
            platform=platform,
            sessdata=self.config.connection.sessdata,
            cookie=self.config.connection.cookie,
            parent=self,
        )
        try:
            if dialog.exec_() != LoginDialog.Accepted:
                return
            got_platform, value = dialog.credentials()
        finally:
            dialog.deleteLater()
        if not value:
            return
        if got_platform == "bilibili":
            self.config.connection.sessdata = value
            self.log("已保存 B 站 SESSDATA。")
        elif got_platform == "douyin":
            value, cookie_note = self._clean_douyin_cookie(value)
            self.config.connection.cookie = value
            self.log(
                "已保存抖音 Cookie（注意：上面的问题已写进日志，建议重新完整复制）。"
                if cookie_note
                else "已保存抖音 Cookie。"
            )
        else:
            return

        target = (
            Platform.BILIBILI if got_platform == "bilibili" else Platform.DOUYIN
        )
        if target != self._current_platform():
            # 从「获取凭证」保存的是另一个平台的凭证：顺手把主界面切过去，
            # 否则当前平台的输入框里看不到刚存的内容，用户会以为没保存上。
            self._select_platform(target)
            self.log(f"已切换到{target.label}平台，上面的凭证框就是刚保存的内容。")
        self.cred_edit.setText(value)
        self._refresh_cred_summary()
        self._persist()

    def _clean_douyin_cookie(self, credential: str) -> tuple:
        """连接前把抖音 Cookie 修成"能放进 HTTP 头"的形式。

        返回 ``(清洗后的 cookie, 提示文案)``。为什么要走这一步：

        用户从浏览器复制了一份长 cookie，但复制源把中间省略成了「…」。
        ``parse_cookie_string`` 不关心非法字符、``check_cookie_text`` 只看
        ttwid 在不在，于是**一路通过**，直到 ``websockets.connect()`` 才抛
        ``invalid Cookie header: <整串>``，并且重试 5 次都是同一句。

        这里清理只能让它不再语法报错（握手时才不会本地就失败），但被省略掉
        的那段内容是补不回来的 —— 所以提示文案必须让用户知道要重新粘一次。
        """
        from ..login.douyin import sanitize_cookie_header

        result = sanitize_cookie_header(credential)
        if not result.notes:
            return credential, ""
        note = "；".join(result.notes)
        self.log(f"抖音 Cookie 已自动清理：{note}")
        # 只有当前平台就是抖音时才回写输入框，否则会把抖音凭证写进
        # B 站的 SESSDATA 框里（从「获取凭证」保存另一个平台的凭证时会发生）。
        if result.value != credential and self._current_platform() == Platform.DOUYIN:
            self.cred_edit.setText(result.value)
        return result.value, note

    def _on_connect(self) -> None:
        platform = self._current_platform()
        room = self.room_edit.text().strip()
        self.config.connection.platform = platform
        self.config.connection.room = room

        if self.sim_check.isChecked():
            platform = Platform.BILIBILI  # 占位，下面走 simulator
            kwargs = {"interval": float(self.sim_interval.value())}
            ok = self.engine.start(platform="simulator", room="simulator", **kwargs)
            if ok:
                self.log("已启动模拟弹幕（不会连接真实平台）。")
        else:
            credential = self.cred_edit.text().strip()
            kwargs = {}
            if platform == Platform.BILIBILI:
                self.config.connection.sessdata = credential
                if credential:
                    kwargs["sessdata"] = credential
            else:
                credential, cookie_note = self._clean_douyin_cookie(credential)
                self.config.connection.cookie = credential
                self.config.connection.douyin_mode = DouyinMode.WEBSOCKET
                kwargs["mode"] = "websocket"
                if credential:
                    kwargs["cookie"] = credential
                if cookie_note:
                    self._refresh_cred_summary()
            if not room:
                QMessageBox.warning(
                    self, "还没填房间号",
                    f"请填写 {platform.label} 的直播间地址或房间号。\n示例：{platform.default_room_hint}",
                )
                return
            ok = self.engine.start(platform=platform.value, room=room, **kwargs)

        if not ok:
            self.log("启动失败，详见上方错误。")
            return

        # 注意：``engine.start()`` 返回 True 只表示**抓取线程已起来**，
        # 真实连接结果稍后通过 status 事件异步到达。这里以前直接
        # ``_set_connected_ui(True)``，于是抖音连不上时界面先闪一下"已连接"
        # 再被状态回调改回"未连接"，看起来像连上了又掉线。改成"连接中…"，
        # 由 ``_on_status`` 负责最终状态。
        self._set_connecting_ui()
        self._persist()
        self._show_danmaku_window()

    def _on_stop(self) -> None:
        self.engine.stop()
        self._set_connected_ui(False)
        self.log("已停止接收弹幕。")

    def _set_connecting_ui(self) -> None:
        """进入"连接中"状态：按钮切过去，但标签不谎报已连接。"""
        self.connect_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.conn_label.setText("连接中…")
        self.conn_label.setObjectName("status_warn")
        self.conn_label.style().unpolish(self.conn_label)
        self.conn_label.style().polish(self.conn_label)

    def _set_connected_ui(self, connected: bool) -> None:
        self.connect_btn.setEnabled(not connected)
        self.stop_btn.setEnabled(connected)
        self.conn_label.setText("已连接" if connected else "未连接")
        self.conn_label.setObjectName("status_ok" if connected else "status_warn")
        self.conn_label.style().unpolish(self.conn_label)
        self.conn_label.style().polish(self.conn_label)

    def _apply_initial_pause_state(self) -> None:
        """把持久化的"是否启用按键输出"同步到界面与引擎。

        配置里的 ``input.enabled=False`` 表示"上次退出时按了暂停"。以前这个
        值只赋给一个再没人读的成员（``self._input_enabled``），``enable_input``
        复选框和引擎状态都停留在默认值 —— 用户上次退出时若暂停了输出，
        重启后按键会突然开始动。
        """
        enabled = bool(self.config.input.enabled)
        self.engine.set_input_enabled(enabled)
        # 复选框与暂停按钮都反映同一个状态，避免两处显示打架
        if hasattr(self, "enable_input"):
            blocked = self.enable_input.blockSignals(True)
            self.enable_input.setChecked(enabled)
            self.enable_input.blockSignals(blocked)
        if hasattr(self, "pause_input"):
            blocked = self.pause_input.blockSignals(True)
            self.pause_input.setChecked(not enabled)
            self.pause_input.setText("暂停按键输出" if enabled else "恢复按键输出")
            self.pause_input.blockSignals(blocked)

    def _on_pause_input(self, paused: bool) -> None:
        self.pause_input.setText("恢复按键输出" if paused else "暂停按键输出")
        self.engine.set_input_enabled(not paused)
        # 同步"启用按键输出"复选框，否则界面两处状态不一致
        if hasattr(self, "enable_input"):
            blocked = self.enable_input.blockSignals(True)
            self.enable_input.setChecked(not paused)
            self.enable_input.blockSignals(blocked)
        self.log("按键输出已暂停。" if paused else "按键输出已恢复。")

    # ============================================================ 事件处理
    def _on_message(self, message: DanmakuMessage, actionable: bool, matched: str) -> None:
        if self.danmaku_window is not None and self.danmaku_window.isVisible():
            self.danmaku_window.append(message, actionable=actionable, matched=matched)

    def _on_action(self, message: DanmakuMessage, matches: list) -> None:
        if message is None:
            return
        desc = "、".join(
            getattr(getattr(m, "mapping", None), "description", "") or getattr(getattr(m, "mapping", None), "trigger", "")
            for m in matches
        )
        self.log(f"[按键] {message.nickname}: {message.content} → {desc or '（已触发）'}")

    def _on_status(self, connected: bool, text: str) -> None:
        if text:
            self.log(f"[状态] {text}")
        if connected:
            self.conn_label.setText("已连接")
            self.conn_label.setObjectName("status_ok")
        else:
            self.conn_label.setText("未连接")
            self.conn_label.setObjectName("status_warn")
        self.conn_label.style().unpolish(self.conn_label)
        self.conn_label.style().polish(self.conn_label)
        # 按钮状态跟着真实连接状态走：以前只有点"停止"才会恢复按钮，
        # 而连接失败时按钮永远停在"连接中"（connect 禁用、stop 可用）。
        self.connect_btn.setEnabled(not connected)
        self.stop_btn.setEnabled(connected)
        if self.danmaku_window is not None and text:
            self.danmaku_window.append_system(text)

    def _on_error(self, text: str) -> None:
        self.log(f"[错误] {text}")
        self.status_label.setText(text[:120])
        if self.danmaku_window is not None:
            self.danmaku_window.append_system(f"错误：{text}")

    def _on_warning(self, text: str) -> None:
        """警告：不弹窗、不算错误，但要显眼。

        "注入被系统静默丢弃"这类问题在旧版本里完全无声——日志显示按键
        发出成功，用户在记事本/游戏里什么都看不到。
        """
        self.log(f"[警告] {text}")
        self.status_label.setText(f"⚠ {text}"[:160])
        if self.danmaku_window is not None:
            self.danmaku_window.append_system(f"警告：{text}")

    def _check_injection_environment(self) -> None:
        """界面启动时自检一次注入环境（受限令牌 / 沙箱）。"""
        try:
            problem = injection_environment_problem()
        except Exception as exc:  # pragma: no cover - 自检不该影响启动
            logger.debug("注入环境自检失败：%s", exc)
            return
        if problem:
            self._on_warning(problem)

    # ============================================================ 弹幕窗口
    def _show_danmaku_window(self) -> None:
        if self.danmaku_window is None:
            self.danmaku_window = DanmakuWindow(self.config.display)
            self.danmaku_window.set_opacity(self.config.display.opacity)
            self.danmaku_window.configChanged.connect(self._on_display_config_changed)
            self.danmaku_window.closed.connect(self._on_danmaku_window_closed)
        self.danmaku_window.show()
        self.danmaku_window.raise_()
        self.danmaku_window.activateWindow()

    def _on_danmaku_window_closed(self) -> None:
        """用户关掉了弹幕窗口：丢弃引用。

        否则这个隐藏窗口会一直收到 ``append()``：``_flush`` 照样把每一条
        ``insertHtml`` 进一个看不见的文档里（纯烧 CPU），``_history``/``_pending``
        也无上限地涨。丢掉引用后下次点「弹幕窗口」会用当前配置重新建一个。
        """

        win = self.danmaku_window
        self.danmaku_window = None
        if win is not None:
            try:
                win.deleteLater()
            except Exception:  # pragma: no cover - 关闭路径不该再抛
                pass

    def _on_display_config_changed(self, display: object) -> None:
        """弹幕窗口里的显示设置改了：写回主配置并落盘。

        以前这里只回写 ``only_triggered`` / ``auto_scroll`` 两项，弹幕窗口
        「设置」里改的字号、行数、类型开关一旦触发选项页刷新就被旧值覆盖，
        现在整份 ``DisplayConfig`` 同步过去，并把选项页控件也刷成新值。
        """
        if isinstance(display, DisplayConfig):
            self.config.display = display
            self._sync_display_widgets(display)
        self._schedule_save()

    def _sync_display_widgets(self, display: DisplayConfig) -> None:
        """把 DisplayConfig 的值刷进选项页控件（阻断信号，避免回环保存）。"""
        for widget, value in (
            (getattr(self, "max_lines", None), int(display.max_lines)),
            (getattr(self, "font_size", None), int(display.font_size)),
        ):
            if widget is None:
                continue
            blocked = widget.blockSignals(True)
            try:
                widget.setValue(value)
            finally:
                widget.blockSignals(blocked)
        for box_name, value in (
            ("show_ts", bool(display.show_timestamp)),
            ("show_nick", bool(display.show_nickname)),
            ("show_plat", bool(display.show_platform)),
        ):
            box = getattr(self, box_name, None)
            if box is None:
                continue
            blocked = box.blockSignals(True)
            try:
                box.setChecked(value)
            finally:
                box.blockSignals(blocked)

    def _schedule_save(self) -> None:
        """防抖保存：选项页每次拖动都会发 valueChanged，不能每次都写盘。"""
        timer = getattr(self, "_save_timer", None)
        if timer is not None:
            timer.start()

    # ============================================================ 模拟
    def _on_sim_toggled(self, on: bool) -> None:
        self.connect_btn.setText("开始模拟弹幕" if on else "开始接收弹幕")
        self.room_edit.setEnabled(not on)
        self.login_btn.setEnabled(not on)

    def _sim_fetcher(self):
        fetcher = self.engine.fetcher
        if fetcher is None or getattr(fetcher, "platform", "") != "simulator":
            self.log("请先点「开始接收弹幕」进入模拟模式。")
            return None
        return fetcher

    def _sim_once(self) -> None:
        fetcher = self._sim_fetcher()
        if fetcher is None:
            return
        fetcher.emit_once("测试弹幕")

    def _sim_sequence(self) -> None:
        fetcher = self._sim_fetcher()
        if fetcher is None:
            return
        seq = ["666666", "w", "s", "上", "下", "左", "右", "左键", "右键", "上滚", "1", "2"]
        # 以前这里在 GUI 线程里同步跑完 emit_sequence：每条弹幕都会走
        # 筛选→匹配→按键，而按键执行里有 time.sleep（冷却/延迟最多 5 秒），
        # 整个序列期间窗口完全无响应（"未响应"白屏）。
        # 改成用 0 延时的 singleShot 逐条投递，让事件循环在两条之间有机会重绘。
        self._sim_queue = list(seq)
        self._sim_sent = 0
        self._pump_sim_queue()

    def _pump_sim_queue(self) -> None:
        queue = getattr(self, "_sim_queue", None)
        if not queue:
            self.log(f"已发送 {getattr(self, '_sim_sent', 0)} 条测试弹幕。")
            self._sim_queue = []
            return
        fetcher = self._sim_fetcher()
        if fetcher is None:
            self._sim_queue = []
            return
        content = queue.pop(0)
        try:
            fetcher.emit_once(content)
        except Exception as exc:
            logger.error("发送模拟弹幕失败：%s", exc)
        self._sim_sent = getattr(self, "_sim_sent", 0) + 1
        QTimer.singleShot(0, self._pump_sim_queue)

    # ============================================================ 映射
    def _refresh_mapping_table(self) -> None:
        keys: List[KeyMapping] = list(self.manager.key_mappings)
        mouse: List[MouseMapping] = list(self.manager.mouse_mappings)
        rows = []
        for m in keys:
            # 「动作」用中文显示（与编辑器下拉同一份文案），长按带上按住时长；
            # 以前这里直接写 m.action.value，表格里就是 press_and_release 这种英文。
            action = action_label(m)
            rows.append(("键盘", m.trigger, m.key, action, f"{m.cooldown:.2f}s", m.description, m.enabled))
        for m in mouse:
            out = m.button.value
            # 用枚举成员比较，不要拿 action.value 跟大写字符串比：
            # MouseAction 的值是小写的（"move"/"scroll_up"），
            # 曾经写成 == "MOVE" 与 .startswith("SCROLL")，
            # 两个分支永远不成立，表格里就一直显示原始英文枚举值。
            if m.action is MouseAction.MOVE:
                out = f"({m.move_x},{m.move_y})"
            elif m.action in (MouseAction.SCROLL_UP, MouseAction.SCROLL_DOWN):
                out = f"滚轮 x{m.scroll_amount}"
            action = action_label(m)
            rows.append(("鼠标", m.trigger, out, action, f"{m.cooldown:.2f}s", m.description, m.enabled))

        self.mapping_table.setRowCount(0)
        for kind, trigger, out, action, cd, desc, enabled in rows:
            r = self.mapping_table.rowCount()
            self.mapping_table.insertRow(r)
            cells = [kind, trigger, out, action, cd, desc]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if not enabled:
                    # 禁用项用主题里的"禁用文字"色，不再借 Qt.gray（系统调色板，深浅不可控）
                    item.setForeground(QColor(COLORS["text_disabled"]))
                self.mapping_table.setItem(r, c, item)
        self.mapping_count.setText(f"键盘 {len(keys)} 条 / 鼠标 {len(mouse)} 条")
        self.engine.set_mappings(list(keys), list(mouse))

    def _edit_mappings(self) -> None:
        dialog = MappingEditorDialog(
            self.manager.key_mappings, self.manager.mouse_mappings, parent=self
        )
        if dialog.exec_() != MappingEditorDialog.Accepted:
            return
        keys, mouse = dialog.result_mappings()
        self.manager.key_mappings = keys
        self.manager.mouse_mappings = mouse
        self._refresh_mapping_table()
        # ConfigManager.save() 把 OSError 吞掉后返回 False（不抛异常），所以
        # 不看返回值就会在磁盘写失败时照样打印"映射已保存"，用户重启才发现改动没了。
        if self.manager.save():
            self.log(f"映射已保存：键盘 {len(keys)} 条，鼠标 {len(mouse)} 条。")
        else:
            QMessageBox.warning(
                self, "保存失败", "映射未能写入配置文件，请检查配置目录是否可写。"
            )

    def _reset_mappings(self) -> None:
        if QMessageBox.question(
            self, "恢复默认映射", "将丢弃当前所有自定义映射，确定吗？"
        ) != QMessageBox.Yes:
            return
        from ..input.mappings import default_key_mappings, default_mouse_mappings

        self.manager.key_mappings = default_key_mappings()
        self.manager.mouse_mappings = default_mouse_mappings()
        self._refresh_mapping_table()
        if self.manager.save():
            self.log("已恢复默认映射。")
        else:
            QMessageBox.warning(
                self, "保存失败", "默认映射未能写入配置文件，请检查配置目录是否可写。"
            )

    def _test_match(self) -> None:
        text = self.test_edit.text().strip()
        if not text:
            self.test_result.setText("请先输入一条弹幕内容。")
            return
        from ..input.matcher import match_content

        found = match_content(text, self.engine.mappings, max_results=5)
        if not found:
            self.test_result.setText(f"「{text}」没有命中任何映射。")
            return
        parts = []
        for m in found:
            mp = m.mapping
            if isinstance(mp, KeyMapping):
                out = f"按键 {mp.key}（{mp.action.value}）"
            else:
                out = f"鼠标 {mp.button.value} {mp.action.value}"
            tag = "长按" if m.is_long_press else "点按"
            parts.append(f"「{mp.trigger}」→ {out}　[{m.kind}/{tag}]")
        self.test_result.setText("命中：" + "；".join(parts))

    # ============================================================ 筛选
    def _edit_filters(self) -> None:
        dialog = FilterEditorDialog(self.engine.filters, parent=self)
        if dialog.exec_() != FilterEditorDialog.Accepted:
            return
        self.engine.filters = dialog.engine()
        # 落盘：过去这里只改内存，用户辛苦配的规则重启即丢。
        self.manager.filters = list(self.engine.filters.rules)
        if self.manager.save():
            self.log("筛选规则已更新并保存（共 %d 条）。" % len(self.manager.filters))
        else:
            self.log("筛选规则已更新（保存失败，仅本次运行有效）。")

    # ============================================================ 选项
    def _on_options_changed(self) -> None:
        cfg = self.config
        cfg.input.max_actions_per_message = int(self.max_actions.value())
        cfg.input.global_cooldown = float(self.global_cd.value())
        cfg.input.per_user_cooldown = float(self.user_cd.value())
        cfg.input.dedupe_window = float(self.dedupe.value())
        cfg.display.max_lines = int(self.max_lines.value())
        cfg.display.font_size = int(self.font_size.value())
        cfg.display.show_timestamp = self.show_ts.isChecked()
        cfg.display.show_nickname = self.show_nick.isChecked()
        cfg.display.show_platform = self.show_plat.isChecked()
        cfg.input.toggle_hotkey = self.hotkey_edit.text().strip() or "ctrl+shift+p"
        if self.danmaku_window is not None:
            self.danmaku_window.apply_config(cfg.display)
        # 以前这里只写内存，选项页的全部改动要等 closeEvent 的 _persist() 才
        # 落盘 —— 程序被强杀或崩溃就全丢了。改成防抖保存。
        self._schedule_save()

    def _on_game_mode(self, on: bool) -> None:
        self.config.input.game_mode = on
        self.engine.set_game_mode(on)
        self.log("已切换到游戏模式（扫描码发送）。" if on else "已切换到普通模式（SendInput）。")
        self._schedule_save()

    def _on_enable_input(self, on: bool) -> None:
        self.config.input.enabled = on
        self.engine.set_input_enabled(on)
        self.log("按键输出已启用。" if on else "按键输出已禁用（只显示弹幕）。")
        self._schedule_save()

    # ============================================================ 热键
    def _start_hotkey(self) -> None:
        if not PYNPUT_AVAILABLE:
            return
        combo = self.config.input.toggle_hotkey
        try:
            # 回调必须收下 combo 参数：GlobalHotkey 以 ``self._on_trigger(combo)``
            # 形式调用，0 参 lambda 会抛 TypeError 并被回调隔离吞掉 —— 表现为
            # "热键注册成功但按了没反应"，极难排查。
            #
            # 回调里只发信号，绝不在监听线程里碰控件（见 ``hotkeyTriggered``）。
            self.hotkey = GlobalHotkey(on_trigger=lambda _combo: self.hotkeyTriggered.emit())
            self.hotkey.register(combo)
            self.hotkey.start()
        except Exception as exc:
            self.log(f"全局热键注册失败：{exc}")

    def _on_hotkey_triggered(self) -> None:
        """全局热键槽，保证在 GUI 线程执行。"""
        self.pause_input.toggle()

    def _restart_hotkey(self) -> None:
        self._on_options_changed()
        if self.hotkey is not None:
            try:
                self.hotkey.stop()
            except Exception:
                pass
            self.hotkey = None
        self._start_hotkey()
        if self.hotkey is not None:
            self.log(f"热键已更新为 {self.config.input.toggle_hotkey}。")

    # ============================================================ 统计 / 日志
    def _refresh_stats(self) -> None:
        # 走 EngineBridge 而不是直接读 self.engine.stats：抓取线程在锁内改这些
        # 计数器，GUI 线程无锁读会拿到撕裂的中间值（而且 statsUpdated 信号
        # 以前全项目 0 处 connect，等于死代码）。
        self.bridge.refresh_stats()
        # 焦点过滤开着时顺带刷那一行"现在会不会注入"：用户切窗口后要能立刻
        # 看出来生效了没有。关着的时候不碰，省掉每秒一次的 Win32 调用。
        if getattr(self, "focus_enable", None) is not None and self.focus_enable.isChecked():
            self._refresh_focus_status()

    def _on_stats_updated(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return
        elapsed = max(1.0, float(snapshot.get("elapsed", 0.0) or 0.0))
        total = int(snapshot.get("total_messages", 0) or 0)
        per_min = total / elapsed * 60.0
        self.stats_label.setText(
            f"弹幕 {total}　按键 {int(snapshot.get('total_actions', 0) or 0)}"
            f"　丢弃 {int(snapshot.get('dropped_messages', 0) or 0)}"
        )

        def _n(key: str) -> int:
            return int(snapshot.get(key, 0) or 0)

        # 固定 3 行 × 3 项：换行由这里决定，不再随窗口宽度和数值位数乱折；
        # 行内用全角空格分组，替换掉原来那串全角竖线（9 根竖线自己就成了噪声）。
        self.stat_view.setText(
            f"运行 {int(elapsed)} 秒　弹幕 {total}（{per_min:.1f}/分）　显示 {_n('displayed_messages')}\n"
            f"丢弃 {_n('dropped_messages')}　按键 {_n('key_actions')}　鼠标 {_n('mouse_actions')}\n"
            f"被抑制 {_n('suppressed_actions')}　焦点拦截 {_n('focus_suppressed')}　错误 {_n('errors')}"
        )

    def log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {text}")
        logger.info("%s", text)

    # ============================================================ 持久化
    def _restore_geometry(self) -> None:
        geo = self.config.window
        width = max(640, geo.width)
        height = max(480, geo.height)
        self.resize(width, height)
        # 只夹 width/height 是不够的：显示器拔掉、分辨率变化、或者手改
        # settings.json 之后，窗口可能整个落在屏幕外——界面上表现为"启动后
        # 什么也没有"，用户只能去删配置文件。这里用实际屏幕可用区域兜底，
        # 保证窗口至少有一部分（标题栏）落在某块屏幕上。
        self.move(self._clamp_to_screens(geo.x, geo.y, width, height))
        self.room_edit.setText(self.config.connection.room)
        self._load_credential()

    @staticmethod
    def _clamp_to_screens(x: int, y: int, width: int, height: int) -> QPoint:
        """把窗口左上角夹到"至少有一部分可见"的位置。

        多屏时优先保留原坐标（只要它落在任意一块屏幕的可用区域内）；
        否则夹到主屏可用区域里，并留出标题栏高度，避免窗口跑到任务栏底下。
        """
        try:
            from PyQt5.QtWidgets import QApplication

            app = QApplication.instance()
            screens = app.screens() if app is not None else []
        except Exception:  # pragma: no cover - 无 Qt 环境
            screens = []

        if not screens:
            return QPoint(x, y)

        # 已经落在某块屏幕里（哪怕只露一角）就尊重原坐标
        for screen in screens:
            area = screen.availableGeometry()
            if (
                area.left() - width + 120 <= x <= area.right() - 120
                and area.top() - 10 <= y <= area.bottom() - 60
            ):
                return QPoint(x, y)

        area = screens[0].availableGeometry()
        max_x = area.right() - 120
        max_y = area.bottom() - 60
        return QPoint(
            min(max(x, area.left()), max(area.left(), max_x)),
            min(max(y, area.top()), max(area.top(), max_y)),
        )

    def _persist(self) -> None:
        self.config.window.x = self.x()
        self.config.window.y = self.y()
        self.config.window.width = self.width()
        self.config.window.height = self.height()
        self.manager.key_mappings = list(self.engine.key_mappings)
        self.manager.mouse_mappings = list(self.engine.mouse_mappings)
        self.manager.filters = list(self.engine.filters.rules)
        try:
            if not self.manager.save():
                self.log("保存配置失败：配置文件未能写入，请检查目录权限。")
        except OSError as exc:
            self.log(f"保存配置失败：{exc}")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # 退出顺序很关键。engine.stop() 会置停止标志、拒绝新的按键、再 join
        # 抓取线程：线程可能卡在 _execute_match 的 sleep 或按键执行里，不等它
        # 退出就返回的话，它醒来后仍会把键按下去 —— 进程随即结束，系统里就
        # 留下一批物理按住的键。这里再兜底 stop/release 一次。
        try:
            self.engine.stop()
        except Exception:
            pass
        try:
            self.engine.keyboard.stop()
            self.engine.mouse.stop()
            self.engine.release_all()
        except Exception:
            pass
        # 统计定时器必须显式停掉：它是 QTimer(self) 的子对象，但 `close()` 只隐藏窗口、
        # 不销毁对象，所以不主动 stop 的话它会继续每秒回调 `_refresh_stats`，
        # 在一个已经关掉的窗口上刷文本（而且此时引擎已停，数据永远不变）。
        timer = getattr(self, "_stats_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        # 防抖保存定时器同理：不显式停掉它可能在窗口销毁后再触发一次 _persist。
        save_timer = getattr(self, "_save_timer", None)
        if save_timer is not None:
            try:
                save_timer.stop()
            except Exception:
                pass
        if self.hotkey is not None:
            try:
                self.hotkey.stop()
            except Exception:
                pass
        if self.danmaku_window is not None:
            self.danmaku_window.close()
        self.bridge.detach()
        self._persist()
        self.log("已退出。")
        super().closeEvent(event)
