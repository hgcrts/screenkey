# -*- coding: utf-8 -*-
"""弹幕键盘引擎：把「抓取 → 筛选 → 匹配 → 按键」串成一条流水线。

这是本项目的主干。界面只跟引擎打交道，引擎只跟下面这些部件打交道：

    DanmakuFetcher   ── 产出 DanmakuMessage
          ↓
    FilterEngine     ── 决定这条弹幕是否放行 / 是否参与按键 / 是否改写
          ↓
    matcher          ── 在映射表里找出命中的 KeyMapping / MouseMapping
          ↓
    KeyboardController / MouseController  ── 真正发出按键

线程模型
--------
抓取器在自己的后台线程里跑，回调也在那个线程被调用。引擎**不**做线程切换，
它只在抓取线程里执行筛选与匹配，然后把结果通过 ``on_event`` 抛给界面；
界面自己负责切回主线程（PyQt 里用 signal，天然跨线程安全）。

按键执行也发生在抓取线程 —— 这是刻意的：``SendInput`` / ``keybd_event``
是线程无关的 Win32 调用，不需要主线程。这样即使界面卡住，按键也不会延迟。

引擎自身用一把可重入锁保护可变状态，``stop()`` 可从任意线程调用。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .config.models import AppConfig
from .danmaku.base import DanmakuFetcher
from .danmaku.message import DanmakuMessage, make_system_message
from .filters.rules import FilterEngine, FilterResult
from .focus_gate import FocusGate
from .input.keyboard import KeyboardController
from .input.mappings import (
    BaseMapping,
    InputType,
    KeyMapping,
    MouseMapping,
    default_key_mappings,
    default_mouse_mappings,
)
from .input.keycodes import (
    injection_environment_problem,
    restricted_host_markers,
    target_blocks_injection,
)
from .input.matcher import CooldownTracker, MatchResult, match_content
from .input.mouse import MouseController
from .input.window import FocusFilter, get_foreground_window
from .utils.logging_setup import get_logger

logger = get_logger("engine")


# ----------------------------------------------------------------------
# 事件
# ----------------------------------------------------------------------


@dataclass
class EngineEvent:
    """引擎抛给界面的一件事。

    :param kind: ``"message"`` / ``"action"`` / ``"status"`` / ``"error"`` /
        ``"warning"`` / ``"stats"``
    :param message: 相关弹幕（``message`` / ``action`` 事件有）
    :param actionable: 该弹幕是否允许触发按键
    :param matches: 命中的映射
    :param text: 人类可读的说明（``status`` / ``error`` 用）
    :param extra: 附加数据
    """

    kind: str
    message: Optional[DanmakuMessage] = None
    actionable: bool = False
    matches: List[MatchResult] = field(default_factory=list)
    text: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def rule(self) -> str:
        """命中的筛选规则名。"""
        return str(self.extra.get("rule", ""))


@dataclass
class EngineStats:
    """运行统计。"""

    started_at: float = 0.0
    total_messages: int = 0
    displayed_messages: int = 0
    dropped_messages: int = 0
    total_actions: int = 0
    key_actions: int = 0
    mouse_actions: int = 0
    suppressed_actions: int = 0
    #: 因为"前台窗口不是指定窗口"而没注入的次数。
    #: **不计入** ``suppressed_actions``：那是"本该发但被冷却/去重挡了"，
    #: 而这个是用户主动配置的预期行为，混在一起会让抑制率看起来虚高。
    focus_suppressed: int = 0
    errors: int = 0
    key_counts: Dict[str, int] = field(default_factory=dict)
    user_counts: Dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        """清零（保留 ``started_at``）。"""
        self.total_messages = 0
        self.displayed_messages = 0
        self.dropped_messages = 0
        self.total_actions = 0
        self.key_actions = 0
        self.mouse_actions = 0
        self.suppressed_actions = 0
        self.focus_suppressed = 0
        self.errors = 0
        self.key_counts.clear()
        self.user_counts.clear()

    @property
    def elapsed(self) -> float:
        """运行秒数。"""
        if not self.started_at:
            return 0.0
        return max(0.0, time.time() - self.started_at)

    def to_dict(self) -> Dict[str, Any]:
        """给界面用的快照。"""
        elapsed = self.elapsed
        return {
            "elapsed": elapsed,
            "total_messages": self.total_messages,
            "displayed_messages": self.displayed_messages,
            "dropped_messages": self.dropped_messages,
            "total_actions": self.total_actions,
            "key_actions": self.key_actions,
            "mouse_actions": self.mouse_actions,
            "suppressed_actions": self.suppressed_actions,
            "focus_suppressed": self.focus_suppressed,
            "errors": self.errors,
            "per_minute": (self.total_messages / elapsed * 60.0) if elapsed > 0 else 0.0,
            "top_keys": sorted(self.key_counts.items(), key=lambda kv: -kv[1])[:5],
            "top_users": sorted(self.user_counts.items(), key=lambda kv: -kv[1])[:5],
        }


# ----------------------------------------------------------------------
# 引擎
# ----------------------------------------------------------------------


class DanmakuKeyboardEngine:
    """弹幕 → 键盘 的主引擎。

    :param config: 应用配置（连接/显示/输入/窗口）
    :param key_mappings: 键盘映射；``None`` 用默认表
    :param mouse_mappings: 鼠标映射；``None`` 用默认表
    :param filters: 筛选引擎；``None`` 用默认规则
    :param fetcher: 抓取器；``None`` 表示还没连（``connect`` 时创建）
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        key_mappings: Optional[Sequence[KeyMapping]] = None,
        mouse_mappings: Optional[Sequence[MouseMapping]] = None,
        filters: Optional[FilterEngine] = None,
        fetcher: Optional[DanmakuFetcher] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.config = config or AppConfig()

        self.key_mappings: List[KeyMapping] = (
            list(key_mappings) if key_mappings is not None else default_key_mappings()
        )
        self.mouse_mappings: List[MouseMapping] = (
            list(mouse_mappings) if mouse_mappings is not None else default_mouse_mappings()
        )
        self.filters = filters or FilterEngine.default()

        self.keyboard = KeyboardController(game_mode=self.config.input.game_mode)
        self.mouse = MouseController()
        self.cooldowns = CooldownTracker()
        self.stats = EngineStats()
        #: 前台窗口判定（不缓存，每次按当前配置重算）
        self.focus = FocusGate(self.config)

        self.fetcher: Optional[DanmakuFetcher] = fetcher
        self._recent: Dict[str, float] = {}

        #: 事件回调 ``(EngineEvent) -> None``；在抓取线程被调用
        self.on_event: Optional[Callable[[EngineEvent], None]] = None
        #: 是否允许真实按键（测试时置 False 可只跑逻辑）
        self.dry_run = False

        #: 注入环境（受限令牌）是否已经提醒过——每次运行只提醒一次
        self._env_warned = False
        #: 上一次检查过完整性级别的前台进程 pid（切换窗口时才重新检查）
        self._checked_fg_pid = 0

        self._bound_fetcher: Optional[DanmakuFetcher] = None

    # ------------------------------------------------------------------
    # 抓取器绑定
    # ------------------------------------------------------------------

    def bind_fetcher(self, fetcher: DanmakuFetcher) -> None:
        """把抓取器的回调接到引擎上。"""
        with self._lock:
            if self._bound_fetcher is not None:
                self._bound_fetcher.clear_callbacks()
            self.fetcher = fetcher
            self._bound_fetcher = fetcher
            fetcher.on_message(self._handle_message)
            fetcher.on_status(self._handle_status)
            fetcher.on_error(self._handle_error)

    def _ensure_fetcher(self, platform: str, **kwargs: Any) -> DanmakuFetcher:
        """按平台创建并绑定抓取器（若已有同平台抓取器则复用）。"""
        from .danmaku import create_fetcher

        if self._bound_fetcher is not None and self._bound_fetcher.platform == platform:
            return self._bound_fetcher
        fetcher = create_fetcher(platform, **kwargs)
        self.bind_fetcher(fetcher)
        return fetcher

    # ------------------------------------------------------------------
    # 连接控制
    # ------------------------------------------------------------------

    def start(
        self,
        platform: Optional[str] = None,
        room: Optional[str] = None,
        **kwargs: Any,
    ) -> bool:
        """连接直播间并开始把弹幕映射成按键。

        :param platform: ``"bilibili"`` / ``"douyin"`` / ``"simulator"``；
            默认用配置里的
        :param room: 直播间地址或房间号；默认用配置里的
        :param kwargs: 传给抓取器（``sessdata`` / ``cookie`` / ``interval`` 等）
        :return: 是否成功启动（真实连接结果通过 ``status`` 事件异步到达）
        """
        with self._lock:
            conn = self.config.connection
            platform = (platform or conn.platform.value or "bilibili").strip().lower()
            room = (room if room is not None else conn.room) or ""

            if platform == "bilibili":
                kwargs.setdefault("sessdata", conn.sessdata)
            elif platform == "douyin":
                kwargs.setdefault("cookie", conn.cookie)
                kwargs.setdefault("mode", conn.douyin_mode.value)

            if not room and platform != "simulator":
                self._emit_error(f"还没有填写{platform}直播间地址或房间号")
                return False

            # 抓取器自己的 ``start()`` 只认 room + kwargs；连接相关配置
            # （重连次数、退避、超时、心跳）以前**从来没有**被传下去，
            # 用户在选项里怎么改都不生效。这里统一注入，抓取器用
            # ``apply_connection_config`` 消费（不认识就忽略）。
            connection_options = {
                "auto_reconnect": conn.auto_reconnect,
                "reconnect_delay": conn.reconnect_delay,
                "max_reconnect_attempts": conn.max_reconnect_attempts,
                "connection_timeout": conn.connection_timeout,
                "heartbeat_interval": conn.heartbeat_interval,
            }
            for key, value in connection_options.items():
                kwargs.setdefault(key, value)

            fetcher = self._ensure_fetcher(platform, **kwargs)
            # _ensure_fetcher 已经把抓取器绑到 self.fetcher 上；这里保留局部
            # 变量只是为了在下面 start() 前做一次显式非空断言。
            if fetcher is None:  # pragma: no cover - create_fetcher 不返回 None
                self._emit_error("创建弹幕抓取器失败")
                return False
            self.stats.started_at = time.time()
            self.stats.reset()
            self.cooldowns.reset()
            self._recent.clear()
            # ``stop()`` 会把两个控制器置为"停止"状态以拦截迟到的按键动作，
            # 这里必须解封，否则停止后重新连接就再也发不出按键了。
            self.keyboard.start()
            self.mouse.start()

        self._emit_status(f"正在连接 {room or platform} …", connected=False)
        logger.info("开始连接: platform=%s room=%s", platform, room)
        # 先自检运行环境：受限令牌下后面每一条按键都会被系统静默丢弃，
        # 用户需要的是一句明确的解释，而不是"日志里有、对方收不到"。
        self._warn_injection_environment()

        ok = self.fetcher.start(room, **kwargs) if self.fetcher else False
        if not ok:
            self._emit_error("启动弹幕接收线程失败")
        return bool(ok)

    def stop(self, timeout: float = 3.0) -> None:
        """断开连接并释放所有按下的键。

        ``fetcher.stop()`` 是**非阻塞**的（只置 ``_stopping`` 标志），所以要
        再 ``join`` 一次等抓取线程真正退出：线程可能正卡在
        :meth:`_execute_match` 的 ``time.sleep`` 或按键执行里，不等它退出就
        返回，它醒来后仍会把键按下去 —— 进程随即结束，系统里就留下一批
        物理按住的键。挑取线程退不出来的极端情况也不能把调用方挂死，
        因此 join 有超时上限。
        """
        with self._lock:
            fetcher = self._bound_fetcher
        if fetcher is not None:
            try:
                fetcher.stop(timeout=timeout)
            except Exception as exc:  # pragma: no cover - 防御
                logger.error("停止抓取器失败: %s", exc)
            # 先掐断按键输出，再等线程退出：顺序反了的话线程醒来还能 press。
            self.keyboard.stop()
            self.mouse.stop()
            try:
                fetcher.join(timeout=max(0.1, float(timeout)))
            except Exception as exc:  # pragma: no cover - 防御
                logger.error("等待抓取线程退出失败: %s", exc)
        self.release_all()
        self._emit_status("已断开", connected=False)

    @property
    def running(self) -> bool:
        """是否正在运行。"""
        fetcher = self._bound_fetcher
        return bool(fetcher and fetcher.running)

    @property
    def connected(self) -> bool:
        """是否真的连上了直播间。"""
        fetcher = self._bound_fetcher
        return bool(fetcher and fetcher.connected)

    def release_all(self) -> None:
        """释放所有按下的键与鼠标键（防止"卡键"）。"""
        try:
            self.keyboard.release_all()
        except Exception as exc:  # pragma: no cover - 防御
            logger.error("释放键盘失败: %s", exc)
        try:
            self.mouse.release_all()
        except Exception as exc:  # pragma: no cover - 防御
            logger.error("释放鼠标失败: %s", exc)

    # ------------------------------------------------------------------
    # 映射表
    # ------------------------------------------------------------------

    def set_mappings(
        self,
        key_mappings: Optional[Sequence[KeyMapping]] = None,
        mouse_mappings: Optional[Sequence[MouseMapping]] = None,
    ) -> None:
        """整体替换映射表（界面编辑后调用）。"""
        with self._lock:
            if key_mappings is not None:
                self.key_mappings = list(key_mappings)
            if mouse_mappings is not None:
                self.mouse_mappings = list(mouse_mappings)

    @property
    def mappings(self) -> List[BaseMapping]:
        """键盘 + 鼠标映射的合并视图。"""
        return [*self.key_mappings, *self.mouse_mappings]

    def set_input_enabled(self, enabled: bool) -> None:
        """开关按键输出。"""
        with self._lock:
            self.config.input.enabled = bool(enabled)

    def set_game_mode(self, enabled: bool) -> None:
        """切换游戏模式（走扫描码，兼容 DirectInput 游戏）。"""
        with self._lock:
            self.config.input.game_mode = bool(enabled)
            self.keyboard.set_game_mode(bool(enabled))
        self._emit_status(f"已{'开启' if enabled else '关闭'}游戏模式", connected=self.connected)

    # ------------------------------------------------------------------
    # 弹幕处理
    # ------------------------------------------------------------------

    def process(self, message: DanmakuMessage) -> Optional[MatchResult]:
        """同步处理一条弹幕（供测试与"手动发一条"用）。"""
        return self._handle_message(message)

    def _handle_message(self, message: DanmakuMessage) -> Optional[MatchResult]:
        """抓取线程里处理一条弹幕：筛选 → 匹配 → 冷却 → 执行。"""
        result: Optional[MatchResult] = None
        with self._lock:
            self.stats.total_messages += 1
            if message.user_id:
                self.stats.user_counts[message.user_id] = (
                    self.stats.user_counts.get(message.user_id, 0) + 1
                )

        # 1) 筛选
        filtered: FilterResult = self.filters.evaluate(message)
        if filtered.dropped:
            with self._lock:
                self.stats.dropped_messages += 1
            self._emit(
                EngineEvent(
                    kind="message",
                    message=message,
                    actionable=False,
                    text="已按规则丢弃",
                    extra={"rule": filtered.rule, "dropped": True},
                )
            )
            return None

        out_message = filtered.message or message
        actionable = filtered.actionable

        # 2) 匹配
        matches: List[MatchResult] = []
        if actionable:
            with self._lock:
                mappings: List[BaseMapping] = [*self.key_mappings, *self.mouse_mappings]
                max_results = max(0, int(self.config.input.max_actions_per_message)) or 0
            if max_results:
                matches = match_content(out_message.content, mappings, max_results=max_results)

        with self._lock:
            self.stats.displayed_messages += 1

        # 3) 逐个执行
        executed: List[MatchResult] = []
        for match in matches:
            if self._execute_match(out_message, match):
                executed.append(match)

        result = executed[0] if executed else (matches[0] if matches else None)

        self._emit(
            EngineEvent(
                kind="action" if executed else "message",
                message=out_message,
                actionable=actionable,
                matches=executed or matches,
                text=filtered.rule,
                extra={
                    "rule": filtered.rule,
                    "dropped": False,
                    "suppressed": bool(matches) and not executed,
                },
            )
        )
        return result

    def _focus_filter(self) -> FocusFilter:
        """按当前配置构造焦点过滤器（见 ``focus_gate`` 模块说明）。"""
        return self.focus.filter()

    def focus_allows_injection(self) -> bool:
        """当前是否允许注入按键（给界面的状态显示用）。"""
        return self.focus.allows_injection(self.dry_run)

    def focus_status_text(self) -> str:
        """给界面的焦点过滤状态文案。"""
        return self.focus.status_text(self.dry_run)

    def _focus_blocked(self, message: DanmakuMessage) -> bool:
        """前台窗口不是指定窗口时返回 ``True``，并计入统计。

        "该不该拦"的判定本身在 :class:`~danmaku_kb.focus_gate.FocusGate` 里，
        这里只负责拦下之后的副作用（统计与日志）。
        """
        if not self.focus.blocks(self.dry_run):
            return False
        with self._lock:
            self.stats.focus_suppressed += 1
        logger.debug("前台窗口不是指定窗口，跳过注入：%s", message.content)
        return True

    def _execute_match(self, message: DanmakuMessage, match: MatchResult) -> bool:
        """执行一条命中的映射；返回是否真的发出了按键。"""
        mapping = match.mapping
        cfg = self.config.input

        # 焦点过滤放在去重/冷却**之前**：窗口不对时既不该注入，也不该白白
        # 吃掉这个映射的冷却窗口——否则用户切回游戏后还要等冷却，体感是
        # "切回来了还是没反应"。
        if self._focus_blocked(message):
            return False

        with self._lock:
            if not cfg.enabled:
                self.stats.suppressed_actions += 1
                return False

            # 同一条弹幕去重（平台重发 / 断线重连会重复推送）
            if cfg.dedupe_window > 0:
                fp = message.fingerprint
                now = time.monotonic()
                last = self._recent.get(fp)
                if last is not None and (now - last) < cfg.dedupe_window:
                    self.stats.suppressed_actions += 1
                    logger.debug("去重抑制: %s", message.content)
                    return False
                self._recent[fp] = now
                if len(self._recent) > 2048:
                    cutoff = now - max(cfg.dedupe_window, 1.0) * 4
                    self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}

            # 冷却：全局 + 按用户
            key = f"{mapping.input_type.value}:{mapping.key_id()}"
            if not self.cooldowns.ready(key, mapping.cooldown):
                self.stats.suppressed_actions += 1
                logger.debug("冷却抑制: %s", mapping.trigger)
                return False

            if cfg.per_user_cooldown > 0 and message.user_id:
                if not self.cooldowns.ready_for_user(message.user_id, cfg.per_user_cooldown):
                    self.stats.suppressed_actions += 1
                    return False

            if cfg.global_cooldown > 0:
                if not self.cooldowns.ready("__global__", cfg.global_cooldown):
                    self.stats.suppressed_actions += 1
                    return False

        # 延迟（在锁外睡，避免阻塞其它弹幕）
        delay = float(mapping.delay or 0.0)
        if delay > 0:
            time.sleep(min(delay, 5.0))
            # 睡之前判过一次焦点，但这一觉最长 5 秒，用户完全可能中途切走
            # 窗口。"真正注入的这一刻谁在前台"才算数，所以再判一次。
            if self._focus_blocked(message):
                return False

        ok = True
        if not self.dry_run:
            # 真正要注入之前再看一眼前台窗口的权限：目标比本程序高时
            # Windows 会静默丢按键，用户只会觉得"没反应"。
            self._check_foreground_integrity()
            try:
                if mapping.input_type is InputType.MOUSE:
                    ok = self.mouse.execute(mapping, is_long_press=match.is_long_press)  # type: ignore[arg-type]
                else:
                    ok = self.keyboard.execute(mapping, is_long_press=match.is_long_press)  # type: ignore[arg-type]
            except Exception as exc:
                logger.error("执行映射失败: %s", exc)
                self._emit_error(f"执行映射失败: {exc}")
                ok = False

        now = time.monotonic()
        with self._lock:
            self.cooldowns.mark(key, now)
            if cfg.per_user_cooldown > 0 and message.user_id:
                self.cooldowns.mark_user(message.user_id, now)
            if cfg.global_cooldown > 0:
                self.cooldowns.mark("__global__", now)

            self.stats.total_actions += 1
            if mapping.input_type is InputType.MOUSE:
                self.stats.mouse_actions += 1
                label = getattr(mapping, "button", None)
                label = label.value if hasattr(label, "value") else "mouse"
            else:
                self.stats.key_actions += 1
                label = getattr(mapping, "key", "") or mapping.trigger
            self.stats.key_counts[str(label)] = self.stats.key_counts.get(str(label), 0) + 1

        logger.info(
            "弹幕「%s」(%s) → %s %s%s",
            message.content,
            message.nickname or "?",
            match.trigger,
            "长按" if match.is_long_press else "点按",
            "" if ok else " [发送失败]",
        )
        return ok

    # ------------------------------------------------------------------
    # 抓取器回调
    # ------------------------------------------------------------------

    def _handle_status(self, connected: bool, text: str = "") -> None:
        """抓取器状态变化。"""
        if text:
            self._emit_status(text, connected=connected)
        elif connected:
            self._emit_status("已连接", connected=True)
        else:
            self._emit_status("已断开", connected=False)

    def _handle_error(self, text: str) -> None:
        """抓取器报错。"""
        self._emit_error(text)

    # ------------------------------------------------------------------
    # 事件派发
    # ------------------------------------------------------------------

    def _emit(self, event: EngineEvent) -> None:
        """调用事件回调（异常绝不传播，否则会打断弹幕流）。"""
        callback = self.on_event
        if callback is None:
            return
        try:
            callback(event)
        except Exception as exc:  # pragma: no cover - 防御
            logger.error("事件回调异常: %s", exc)

    def _emit_status(self, text: str, connected: bool) -> None:
        """抛状态事件。"""
        self._emit(EngineEvent(kind="status", text=text, extra={"connected": connected}))

    def _emit_error(self, text: str) -> None:
        """抛错误事件。"""
        with self._lock:
            self.stats.errors += 1
        logger.error("%s", text)
        self._emit(EngineEvent(kind="error", text=text))

    def _emit_warning(self, text: str) -> None:
        """抛警告事件（不计入错误统计）。

        "按键发出去了但目标收不到"这类问题**不是**代码出错，却会让用户
        以为程序坏了；单独走一路警告，界面能把它显示在日志和状态栏上。
        """
        logger.warning("%s", text)
        self._emit(EngineEvent(kind="warning", text=text))

    # ------------------------------------------------------------------
    # 注入环境自检
    # ------------------------------------------------------------------

    def _warn_injection_environment(self) -> None:
        """启动时自检一次：本进程自身的令牌是否让注入必然失效。

        ``SendInput`` 被 UIPI 拦下时"返回成功但什么也没发生"，用户看到的
        是记事本和游戏都不响应、日志里却没有任何失败标记。这里主动把
        环境问题说出来。

        判据有两条，都只报**能确证**的情况（误报比不报更伤信任）：

        1. 本进程完整性级别确实偏低（令牌读得到、而且低）；
        2. 环境里有已知的受限宿主标记（沙箱/自动化宿主）——这类宿主里
           令牌级别往往看起来正常，只有标记能识别。
        """
        if self._env_warned or self.dry_run:
            return
        self._env_warned = True
        try:
            problem = injection_environment_problem()
        except Exception as exc:  # pragma: no cover - 自检本身不该影响启动
            logger.debug("注入环境自检失败：%s", exc)
            problem = ""
        if problem:
            self._emit_warning(problem)
            return
        try:
            markers = restricted_host_markers()
        except Exception as exc:  # pragma: no cover - 自检本身不该影响启动
            logger.debug("受限宿主标记检测失败：%s", exc)
            markers = ""
        if markers:
            self._emit_warning(
                f"检测到受限宿主标记（{markers}）。这类宿主里 Windows 往往会**静默丢弃**"
                "注入的按键：SendInput 依旧返回成功，目标窗口却收不到。"
                "建议关掉当前宿主，改从「Windows 资源管理器」里双击「启动.bat」运行。"
            )

    def _check_foreground_integrity(self) -> None:
        """前台窗口权限比本程序高时提醒（每个窗口只提醒一次）。

        没有提权的程序向以管理员身份运行的游戏注入按键会被 Windows
        静默丢弃——这是"游戏收不到按键"最常见的原因。
        """
        if self.dry_run:
            return
        try:
            info = get_foreground_window()
        except Exception:  # pragma: no cover - 取前台窗口失败不影响注入
            return
        pid = int(getattr(info, "pid", 0) or 0)
        if not pid or pid == self._checked_fg_pid:
            return
        self._checked_fg_pid = pid
        try:
            problem = target_blocks_injection(pid)
        except Exception:  # pragma: no cover
            return
        if problem:
            self._emit_warning(problem)

    def emit_system(self, text: str) -> None:
        """往弹幕流里插一条系统提示（界面显示用）。"""
        platform = self.config.connection.platform.value
        self._emit(
            EngineEvent(
                kind="message",
                message=make_system_message(text, platform=platform),
                actionable=False,
                extra={"system": True},
            )
        )

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def stats_snapshot(self) -> Dict[str, Any]:
        """统计快照。"""
        with self._lock:
            return self.stats.to_dict()


__all__ = ["DanmakuKeyboardEngine", "EngineEvent", "EngineStats"]
