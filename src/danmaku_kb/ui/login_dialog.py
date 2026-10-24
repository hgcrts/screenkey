"""登录对话框：B 站扫码 + 抖音 Cookie 引导。

两个平台分开成两个 tab，因为它们的可用性天差地别：

* **B 站**：官方扫码接口真能用（``passport.bilibili.com``），所以给完整流程
  ——出二维码、显示剩余时间、实时状态、成功后自动填 SESSDATA。
* **抖音**：纯 HTTP 扫码在 2023 年后已不可行（passport 接口要 JS 生成的 ttwid），
  所以这里**不放假按钮**，只给分步引导 + 粘贴框 + 校验。参考项目在这里做了个
  点了必然报 4031 的扫码按钮，用户只会以为程序坏了。
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..login.bilibili_qr import BilibiliQRLogin, QRState
from ..login.douyin import DOUYIN_GUIDE_TEXT, check_cookie_text, sanitize_cookie_header
from ..login.qr_image import SEGNO_AVAILABLE, make_qr_png
from ..utils.logging_setup import get_logger
from .theme import COLORS, rich_paragraph

logger = get_logger(__name__)

#: GUI 线程里停止轮询时给的最大 join 时间。``BilibiliQRLogin.poll_once`` 是同步
#: HTTP（超时 15 秒），用默认的 2 秒会让「取消/关闭对话框」时界面明显卡顿，
#: 而且本来也等不到它结束；轮询线程是守护线程，交给它自己退即可。
_GUI_STOP_TIMEOUT = 0.2

__all__ = ["LoginDialog"]


class LoginDialog(QDialog):
    """凭证获取对话框。

    成功时用 :meth:`credentials` 取回 ``(platform, value)``：

    * ``("bilibili", "<SESSDATA>")``
    * ``("douyin", "ttwid=...; sessionid=...")``
    """

    #: 轮询线程 → 主线程的状态投递。pyqtSignal.emit 是线程安全的，
    #: 而 QTimer.singleShot 从非 GUI 线程调用并不可靠，故用信号。
    biliUpdate = pyqtSignal(object)

    def __init__(
        self,
        platform: str = "bilibili",
        sessdata: str = "",
        cookie: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("获取登录凭证 - 弹幕键盘 V2")
        self.resize(560, 640)
        # 固定初始尺寸之外补一个下限：小屏上拖到过窄会让二维码标签和长说明挤成一列字
        self.setMinimumSize(520, 540)

        self._platform = platform
        self._sessdata = sessdata
        self._cookie = cookie
        self._login: Optional[BilibiliQRLogin] = None
        # 每次「重新生成二维码」都把代数 +1。旧轮询线程可能要等它那次 15 秒
        # HTTP 超时才会发现该停了，期间它的回调仍然会打到本对话框上 —— 没有
        # 这个代数标记，过期的结果会把新二维码的状态反复覆盖回去。
        self._bili_generation = 0
        # 终态（登录成功/二维码失效）后置 True，让 1 秒倒计时不再覆盖它。
        self._status_locked = False
        self._accepted_platform = ""
        self._accepted_value = ""

        self._build_ui()
        # 注意方法名是 ``_apply_bili_status``（下面第 237 行定义的那个）。
        # 曾经这里写成 ``_apply_bili_update``——一个**不存在**的方法，
        # 而 PyQt5 的 ``pyqtSignal.connect()`` 在连接时就解析槽函数，
        # 于是 ``LoginDialog(...)`` 构造直接抛 AttributeError。
        # main_window 的「获取凭证」按钮没有 try/except，点一下整个进程 abort。
        self.biliUpdate.connect(self._apply_bili_status)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_countdown)

        if platform == "douyin":
            self.tabs.setCurrentIndex(1)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_bilibili_tab(), "B站扫码登录")
        self.tabs.addTab(self._build_douyin_tab(), "抖音 Cookie")
        layout.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("使用此凭证")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self._on_reject)
        layout.addWidget(buttons)

    def _build_bilibili_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        tip = QLabel("用手机 B 站客户端扫描下方二维码，然后在手机上确认登录。")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.qr_label = QLabel("点下方按钮生成二维码")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setMinimumHeight(260)
        # 二维码面必须纯白（扫码靠黑白对比），所以它是全应用唯一的"亮面"；
        # 但 QSS 的全局 color 是浅灰，白底上浅灰字等于隐形 ——
        # 占位文案"点下方按钮生成二维码"曾经就是这么消失的。字色取最深的 bg token。
        self.qr_label.setStyleSheet(
            f"background:{COLORS['qr_bg']}; color:{COLORS['bg']};"
            f"border:1px solid {COLORS['border']}; border-radius:6px;"
        )
        layout.addWidget(self.qr_label)

        self.bili_status = QLabel("未开始")
        self.bili_status.setAlignment(Qt.AlignCenter)
        self.bili_status.setObjectName("hint")
        layout.addWidget(self.bili_status)

        row = QHBoxLayout()
        self.qr_btn = QPushButton("生成二维码")
        self.qr_btn.setObjectName("primary")
        self.qr_btn.clicked.connect(self._start_bili_login)
        row.addWidget(self.qr_btn)

        self.bili_cancel_btn = QPushButton("停止轮询")
        self.bili_cancel_btn.setEnabled(False)
        # 用 lambda 显式传参：``clicked`` 信号会把 ``checked`` 作为第一个位置
        # 参数传给槽函数，直接连 ``self._stop_bili_login`` 会让 ``keep_text``
        # 收到 ``False`` —— 恰好是想要的值，纯属巧合。一旦按钮改成 checkable
        # 就会变成 True，状态栏文案再也不刷新。
        self.bili_cancel_btn.clicked.connect(lambda: self._stop_bili_login())
        row.addWidget(self.bili_cancel_btn)
        row.addStretch(1)
        layout.addLayout(row)

        sess_row = QHBoxLayout()
        sess_row.addWidget(QLabel("SESSDATA："))
        self.sessdata_edit = QLineEdit(self._sessdata)
        self.sessdata_edit.setPlaceholderText("扫码成功后自动填入；也可手动粘贴")
        sess_row.addWidget(self.sessdata_edit, 1)
        layout.addLayout(sess_row)

        if not SEGNO_AVAILABLE:
            warn = QLabel("⚠ 未安装 segno，二维码显示为占位图。安装命令：pip install segno")
            warn.setObjectName("status_warn")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        layout.addStretch(1)
        return page

    def _build_douyin_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        why = QLabel(
            rich_paragraph(
                "<b>为什么抖音没有扫码按钮？</b><br>"
                "抖音 Web 端登录接口现在要求浏览器 JS 现场生成的 <code>ttwid</code> 设备指纹，"
                "纯 HTTP 客户端拿不到；强行调用只会返回反爬验证页。"
                "所以这里直接给你最快的办法——从浏览器复制 Cookie。",
            )
        )
        why.setWordWrap(True)
        layout.addWidget(why)

        guide = QGroupBox("操作步骤")
        gl = QVBoxLayout(guide)
        # 5 步说明：QSS 没有 line-height，多行只能靠富文本段落（见 theme.rich_paragraph）
        steps = QLabel(rich_paragraph(DOUYIN_GUIDE_TEXT, 1.7))
        steps.setWordWrap(True)
        steps.setTextInteractionFlags(Qt.TextSelectableByMouse)
        gl.addWidget(steps)
        layout.addWidget(guide)

        layout.addWidget(QLabel("粘贴 Cookie（ttwid 必须有）："))
        self.cookie_edit = QPlainTextEdit(self._cookie)
        self.cookie_edit.setPlaceholderText("ttwid=xxxxxxxx; sessionid=yyyyyyyy; msToken=zzzz")
        self.cookie_edit.setMinimumHeight(90)
        layout.addWidget(self.cookie_edit)

        row = QHBoxLayout()
        check_btn = QPushButton("校验")
        check_btn.clicked.connect(self._check_douyin_cookie)
        row.addWidget(check_btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.douyin_status = QLabel("未填写")
        self.douyin_status.setObjectName("hint")
        self.douyin_status.setWordWrap(True)
        layout.addWidget(self.douyin_status)

        layout.addStretch(1)
        return page

    # ------------------------------------------------------------- B 站流程
    def _start_bili_login(self) -> None:
        # 先停掉上一次的轮询：以前直接覆盖 self._login，重复点「生成二维码」
        # 会让两条轮询线程同时往这个对话框写状态。
        self._stop_bili_login(keep_text=True)
        self._bili_generation += 1
        generation = self._bili_generation
        self._status_locked = False
        self.qr_btn.setEnabled(False)
        self.bili_status.setText("正在申请二维码…")
        try:
            self._login = BilibiliQRLogin(timeout=15.0)
            session = self._login.generate()
        except Exception as exc:
            self.bili_status.setText(f"申请二维码失败：{exc}")
            self.bili_status.setObjectName("status_err")
            self.bili_status.style().unpolish(self.bili_status)
            self.bili_status.style().polish(self.bili_status)
            self.qr_btn.setEnabled(True)
            return

        if session is None or not session.url:
            self.bili_status.setText("申请二维码失败：接口未返回二维码内容")
            self.bili_status.setObjectName("status_err")
            self.bili_status.style().unpolish(self.bili_status)
            self.bili_status.style().polish(self.bili_status)
            self.qr_btn.setEnabled(True)
            return

        png = make_qr_png(session.url, scale=6, border=4)
        pix = QPixmap()
        if pix.loadFromData(png, "PNG"):
            self.qr_label.setPixmap(pix.scaled(240, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.qr_label.setText("二维码渲染失败")

        self.bili_cancel_btn.setEnabled(True)
        self._tick.start()
        # 用默认参数把代数绑进回调：过期线程的结果会被 _on_bili_update 丢弃。
        started = self._login.start_polling(
            on_update=lambda result, gen=generation: self._on_bili_update(result, gen),
            interval=2.0,
            max_seconds=180.0,
        )
        if not started:
            self.bili_status.setText("轮询启动失败")
            self.bili_cancel_btn.setEnabled(False)
            self.qr_btn.setEnabled(True)
            return
        self.bili_status.setText("请用 B 站手机客户端扫码…")

    def _on_bili_update(self, result, generation: Optional[int] = None) -> None:
        """轮询回调（在轮询线程里）。只发信号，不碰控件。"""

        if generation is not None and generation != self._bili_generation:
            # 上一次「生成二维码」留下的线程，结果已过期，直接丢。
            return
        self.biliUpdate.emit(result)

    def _apply_bili_status(self, result) -> None:
        state = result.state
        if self._status_locked and not (result.ok and result.sessdata):
            # 已经进入终态：迟到的中间态结果不能把"登录成功"/"二维码已失效"
            # 顶掉（轮询线程在停止前还可能再投递一两条）。
            return
        label = state.label if hasattr(state, "label") else str(state)
        self.bili_status.setText(f"{label}　{result.message or ''}".strip())
        if result.ok and result.sessdata:
            self.sessdata_edit.setText(result.sessdata)
            self.bili_status.setText(f"登录成功！已获取 SESSDATA（{len(result.sessdata)} 字符）")
            self.bili_status.setObjectName("status_ok")
            self.bili_status.style().unpolish(self.bili_status)
            self.bili_status.style().polish(self.bili_status)
            # 锁住文本：否则 1 秒后的倒计时会把"登录成功！"顶掉。
            self._status_locked = True
            self._stop_bili_login(keep_text=True)
        elif state == QRState.EXPIRED:
            self._status_locked = True
            self._stop_bili_login(keep_text=True)
            self.qr_label.setText("二维码已失效，请重新生成")
        elif state == QRState.ERROR:
            self._status_locked = True
            self.bili_status.setObjectName("status_err")
            self.bili_status.style().unpolish(self.bili_status)
            self.bili_status.style().polish(self.bili_status)
            self._stop_bili_login(keep_text=True)

    def _stop_bili_login(self, keep_text: bool = False) -> None:
        self._tick.stop()
        if self._login is not None:
            # 这里是 GUI 线程：默认 2 秒的 join 会让界面卡住（poll_once 的
            # HTTP 超时是 15 秒，本来也等不到它结束）。给个很短的宽限就够了，
            # 剩下的交给守护线程自己退。
            #
            # 外面包一层 try：这个方法会在 closeEvent 里被调用，而 Qt 事件处理
            # 里的未捕获异常会让 PyQt5 直接 abort()（0xC0000409），连 Python
            # 的 traceback 都看不到。
            try:
                self._login.stop_polling(timeout=_GUI_STOP_TIMEOUT)
            except Exception as exc:
                logger.warning("停止二维码轮询失败：%s", exc)
            self._login = None
        self.bili_cancel_btn.setEnabled(False)
        self.qr_btn.setEnabled(True)
        if not keep_text:
            self._status_locked = False
            self.bili_status.setText("已停止轮询")

    def _update_countdown(self) -> None:
        if self._status_locked:
            return
        if self._login is None or self._login.session is None:
            return
        left = max(0, 170 - int(self._login.session.age))
        if left > 0:
            self.bili_status.setText(f"等待扫码…（二维码剩余 {left} 秒）")

    # ----------------------------------------------------------- 抖音流程
    def _check_douyin_cookie(self) -> None:
        ok, message = check_cookie_text(self.cookie_edit.toPlainText())
        self.douyin_status.setText(("✓ " if ok else "✗ ") + message)
        self.douyin_status.setObjectName("status_ok" if ok else "status_err")
        self.douyin_status.style().unpolish(self.douyin_status)
        self.douyin_status.style().polish(self.douyin_status)

    # --------------------------------------------------------------- 提交
    def _on_accept(self) -> None:
        index = self.tabs.currentIndex()
        if index == 0:
            value = self.sessdata_edit.text().strip()
            if not value:
                self.bili_status.setText("SESSDATA 为空。请先扫码，或手动粘贴 SESSDATA。")
                self.bili_status.setObjectName("status_err")
                self.bili_status.style().unpolish(self.bili_status)
                self.bili_status.style().polish(self.bili_status)
                return
            self._accepted_platform = "bilibili"
            self._accepted_value = value
        else:
            text = self.cookie_edit.toPlainText().strip()
            ok, message = check_cookie_text(text)
            if not ok:
                self.douyin_status.setText("✗ " + message)
                self.douyin_status.setObjectName("status_err")
                self.douyin_status.style().unpolish(self.douyin_status)
                self.douyin_status.style().polish(self.douyin_status)
                return
            # 通过校验后顺手清掉 HTTP 头放不下的字符（全角标点、零宽字符等），
            # 免得存进配置的还是一份"握手时才炸"的串。
            sanitized = sanitize_cookie_header(text)
            if sanitized.value:
                text = sanitized.value
            if sanitized.notes:
                self.douyin_status.setText("✓ " + message + "；" + "；".join(sanitized.notes))
            self._accepted_platform = "douyin"
            self._accepted_value = text
        self._stop_bili_login(keep_text=True)
        self.accept()

    def _on_reject(self) -> None:
        self._stop_bili_login(keep_text=True)
        self.reject()

    def credentials(self) -> tuple:
        """返回 ``(platform, value)``；取消时为空字符串对。"""

        return self._accepted_platform, self._accepted_value

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_bili_login(keep_text=True)
        super().closeEvent(event)
