#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main.py - 程序入口（SPEC 3.1 / 7.3，B2-M5+M6）

启动即做四件事：
  1) QApplication + DPI（Win32 物理像素为准，Qt 用逻辑像素，主线程负责换算）；
  2) 起 CaptureWorker（读消息）与 AnalyzerWorker（推理），队列 maxsize=1；
  3) 状态灯 + 分析面板 + 三条回复选项三个窗口，按 SPEC 4.3 的空白带策略定位；
  4) 托盘菜单：状态、立即重探、暂停/继续、退出。

红线：Worker 不碰 UI；窗口几何只由主线程维护；一键填入只做"置前 + 点输入框 + 一次 Ctrl+V"，
      **绝不发送回车**（SPEC 2.2(d)）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor
from PySide6.QtNetwork import QLocalServer, QLocalSocket
# 第 80 轮末：`QMessageBox` 不再用（第二个实例原来那个模态框会卡住进程，已改成
# 通知已有实例 + 直接退出）→ 从 import 里去掉。
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import config as app_config
from config import Config, load_config, save_config
from core.analyzer import AnalysisResult, Analyzer
from core.message_reader import Message, MessageReader
from core.worker import (AnalyzerWorker, CaptureWorker, FollowReader, LatestRequestQueue,
                         ScrollMotionTracker)
from ui.overlay_dot import OverlayDot
from ui.overlay_panel import OverlayPanel, ReplyOptionsBar, plan_panel_placement
from ui.settings_dialog import SettingsDialog
from ui.wheel_sink import MouseWheelSink
from ui.context_viewer import ContextButton, ContextViewer
from ui.toast import ToastWindow
from ui.first_run_hint import FirstRunHint
from utils import coordinate as coord
from utils import win32_api as w32
from typing import Optional, Sequence

logger = logging.getLogger("main")


class _FillWorker(QThread):
    """在后台线程里执行"置前 QQ → 粘贴 → 回读校验"。

    为什么要挪到线程（用户第 36 轮要求）：填入流程里有多次 `time.sleep` 和跨进程 UIA 校验，
    跑在主线程会把 Qt 事件循环冻住 ~0.3-0.6s，表现为"点完选项卡一下才出文本"。
    放到线程后主线程全程不阻塞，选项条动画也能顺畅播完。

    注意：这里只做**只读校验 + 一次 Ctrl+V 粘贴**（与主线程版完全相同的动作），
    不做任何新的输入模拟；失败会如实回报，不会假装成功。
    """

    done = Signal(bool, str)

    def __init__(self, fn, parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            ok, note = self._fn()
        except Exception as exc:                      # 让线程里的异常也变成"如实回报"
            ok, note = False, f"{type(exc).__name__}: {exc}"
        self.done.emit(bool(ok), str(note))


def pick_hovered_message(cursor: Sequence[int], messages: Sequence[Message],
                         padding: int = 3, only_other_party: bool = True,
                         max_gap: int = 60,
                         row_bounds: Optional[Sequence[int]] = None) -> Optional[Message]:
    """选出鼠标指向的那条消息（纯函数，便于单测）。

    相邻消息挨得很近时，"矩形包含 + 大容差"会互相抢（上一条抢不到），
    所以这里改成**最近气泡**判定：
      1) 在鼠标所在列（水平方向 ±padding 内）找候选；
      2) 垂直距离（在气泡内=0）最小者胜出；
      3) 距离超过 max_gap 或鼠标落在"自己发的消息"上 → 不算命中。

    `row_bounds`（点击路径传消息列表矩形）：把命中范围横向放宽到**整行自己那一侧**
    （左边消息算到列表左缘、右边消息算到列表右缘）。第 79 轮修 bug：用户"极少数情况下
    点气泡没反应、要点一两下"——部分是他点在**头像/气泡边缘**，而旧判定只认气泡本体那一小段 x。
    中间那条空隙仍然不算任何消息（不会误命中另一侧）。
    """
    x, y = int(cursor[0]), int(cursor[1])
    if only_other_party and any(not m.is_other_party and not m.is_image
                                and coord.point_in_rect((x, y), m.bbox, padding)
                                for m in messages):
        return None                       # 鼠标正压在自己发的消息上 → 不切换
    best: Optional[Message] = None
    best_score = None
    for message in messages:
        if message.is_image:
            continue
        if only_other_party and not message.is_other_party:
            continue
        bbox = message.bbox
        left, right = bbox[0] - padding, bbox[2] + padding
        if row_bounds is not None:
            if message.is_other_party:
                left = min(left, int(row_bounds[0]) + 4)
            else:
                right = max(right, int(row_bounds[2]) - 4)
        if not (left <= x <= right):
            continue
        if y < bbox[1]:
            dy = bbox[1] - y
        elif y > bbox[3]:
            dy = y - bbox[3]
        else:
            dy = 0
        if dy > max_gap:
            continue
        score = (dy, abs(x - (bbox[0] + bbox[2]) // 2))
        if best_score is None or score < best_score:
            best, best_score = message, score
    return best


def setup_logging(debug: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    path = Path(app_config.LOG_DIR) / "chatassistant.log"
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-18s %(message)s",
                            "%H:%M:%S")
    logger_root = logging.getLogger()
    logger_root.setLevel(logging.DEBUG if debug else logging.INFO)
    logger_root.handlers.clear()
    # 第 63 轮加固：原来是普通 FileHandler、永不轮转 —— 实测跑几天就到 15 MB 还在涨。
    # 现在按 5 MB 轮转、留 3 份历史（chatassistant.log.1/.2/.3），
    # 长期运行不会把工作区撑大，也不需要用户手动清。
    fh = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    logger_root.addHandler(fh)
    # 打包成 --windowed 的 exe 时 sys.stdout / sys.stderr 都是 None，
    # 这时只留文件日志（再加 StreamHandler 只会让 logging 内部报错没人看见）。
    if sys.stdout is not None or sys.stderr is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger_root.addHandler(sh)


def load_app_icon() -> QIcon:
    """应用图标：优先用 assets/icon.ico（tools/make_icon.py 生成），找不到就退回画一个圆。

    查找顺序 = exe 同级 assets/（打包后）→ 工作区 assets/（开发期）。
    """
    candidates = [Path(app_config.APP_DIR) / "assets" / "icon.ico",
                  Path(app_config.WORKSPACE) / "assets" / "icon.ico",
                  Path(app_config.APP_DIR) / "assets" / "icon.png",
                  Path(app_config.WORKSPACE) / "assets" / "icon.png"]
    for path in candidates:
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(QColor("#FF9ECB"))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(4, 4, 24, 24)
    painter.end()
    return QIcon(pixmap)


class ChatAssistantApp:
    IDLE_POLL_MS = 700           # 空闲/鼠标不在聊天区：整树读取间隔（原来 200ms = 每秒 5 次整树
    #   读，每次在 QQ 进程里跑 ~80ms → 明显加重 QQ 负担，切换会话时表现为卡顿）
    HOVER_POLL_MS = 50
    NEAR_POLL_MS = 30            # 鼠标在 QQ 窗口范围内：30ms 轮询（≈33Hz，悬停切换才够灵敏）
    GEOMETRY_POLL_MS = 200
    # 点击命中判定（第 79 轮续，见 _on_click_miss）：
    CLICK_FRESH_S = 0.5          # 消息坐标比这更新 → "没命中"可以信 → 立刻收起
    PENDING_CLICK_VISIBLE_S = 0.45   # 面板开着时"挂起重判"的窗口（要短：用户要的是点了就收）
    PENDING_CLICK_HIDDEN_S = 1.2     # 面板没开着时的窗口（保证"点气泡能开出来"）

    def __init__(self, app: QApplication, cfg: Config) -> None:
        self.app = app
        self.cfg = cfg
        self.scale = 1.0
        self.reader = MessageReader(cfg)
        self.analyzer = Analyzer(cfg)
        self.queue = LatestRequestQueue()

        self.dot = OverlayDot(cfg, on_click=self.open_settings)
        self.panel = OverlayPanel(cfg)
        self.options = ReplyOptionsBar(cfg)
        self.options.chosen.connect(self.on_option_chosen)
        self.options.refresh.connect(self.on_options_refresh)
        self.toast = ToastWindow(cfg)
        # 第 79 轮：首次启动的一行使用提示（状态灯下方，点一下淡出关闭；只出现一次）
        self.hint = FirstRunHint(cfg)
        self.hint.clicked.connect(self._dismiss_first_run_hint)
        self._hint_done = bool(getattr(cfg.ui, "first_run_hint_shown", False))

        self.messages: List[Message] = []
        self.snapshot: Dict[str, Any] = {}
        self.window_rect: Optional[Sequence[int]] = None
        self.list_box: Optional[Sequence[int]] = None
        self.target: Optional[Message] = None
        self.results: Dict[str, AnalysisResult] = {}
        self.pending_key: Optional[str] = None
        self.paused = False
        self._analyzing = False
        self._last_attention: Optional[str] = None
        self._owner_hwnd: Optional[int] = None
        self._hook_handle = 0
        self._hook_proc = None
        self._cursor_hook = 0
        self._cursor_proc = None
        self._last_cursor_ts = 0.0
        self._hover_pending = False
        self._status_streak = 0          # 连续"读不到"的次数（防抖：不要一次失败就灰灯/收面板）
        self._read_window_rect: Optional[Sequence[int]] = None
        self._last_context_count = 0
        self._target_missing_streak = 0
        self._hidden_by_foreground = False
        self._lbtn_down = False          # 点击触发模式：左键上一帧是否按下（上升沿才算一次点击）
        # 第 79 轮：同一次物理点击只处理一次（轮询 + Raw Input 双通道去重，见 _poll_click）
        self._last_click_handled = 0.0
        self._smooth_prev: Dict[str, QRect] = {}    # 面板/选项条位置缓动状态（滚动跟随用）
        self._locked_panel_x = None                 # 已定位面板的横向位置（目标不变就不动它）
        self._locked_panel_target = None
        # 展开/收起动画（0=完全收起，1=完全展开）
        self._predicted_dy = 0               # 滚轮预测的累计位移（物理像素，会自然衰减）
        self._last_wheel_ts = 0.0            # 最近一次滚轮事件时间（用于避让 UIA 校准）
        self._wheel_notches = 0              # 自上次 UIA 校准以来滚了几格（用于自动标定）
        self._wheel_step_runtime = int(getattr(cfg.ui, "wheel_step_px", 187) or 187)
        self._panel_anim = 1.0
        self._panel_anim_target = 1.0
        self._options_anim = 0.0            # 选项条自己的渐显进度（与面板同段动画一起用）
        self._options_anim_target = 1.0     # 1=展开显示，0=收起（选完回复后置 0）
        # 第 79 轮：用户已经点过"这条消息"的某个选项（可能是流式生成途中点的）→
        # 后续 partial 上屏时**不许再把选项条弹回来**（否则看着像没点中）。
        self._options_dismissed_key: Optional[str] = None
        # 上下文历史面板（第 66 轮）：**默认展开**，位置在 QQ 窗口右侧，
        # 展开/收起用与分析面板同一套动画（宽度 + 透明度，30ms×6 帧）。
        self._ctx_anim = 0.0
        # 第 79 轮（用户口径）：**启动时不再默认打开** —— 收起状态起步，
        # 点状态灯左边的小图标才展开（展开后 `_ctx_user_open=True`，切应用回来仍是开的）。
        self._ctx_anim_target = 0.0
        self._ctx_user_open = False         # 用户是否希望它是开着的（切到别的应用时只是暂时收）
        # 第 68 轮：会话窗口是否"可用"（有消息列表、能读到消息、窗口在屏幕上）。
        # 不可用时把贴在 QQ 上的浮窗全部隐藏 —— 否则会飘到"会话列表窗口"或别的应用上面。
        self._chat_available = False
        self._trace_follow = bool(os.environ.get("QQCA_TRACE_FOLLOW"))
        self._trace_path = str(app_config.LOG_DIR / "follow_trace.csv")
        self._cursor_events = 0
        self._last_event_report = 0.0
        self._place_pending = False
        self.settings_dialog = None

        self.capture = CaptureWorker(cfg, self.reader)
        self.capture.messagesUpdated.connect(self.on_messages)
        self.analyzer_worker = AnalyzerWorker(cfg, self.analyzer, self.queue)
        self.follow_reader = FollowReader(self.reader, cfg,
                                          on_need_rebuild=self.capture.request_now)
        self.follow_reader.updated.connect(self.on_follow_rect)
        # 实时滚动跟随（只读屏幕像素做帧间互相关；不碰任何输入 API，不会卡鼠标）
        self.motion_tracker = ScrollMotionTracker(self.reader, cfg)
        self.motion_tracker.motion.connect(self.on_scroll_motion)
        # 【已停用·事故记录】全局鼠标钩子（WH_MOUSE_LL）会让**系统级鼠标输入**经我们线程回调，
        # 一旦该线程被拖住或注入输入未配对释放，整台机器的鼠标会卡住（2026-09-23 实测事故：
        # 用户鼠标失灵，需从任务管理器结束本进程才恢复）。因此**不再安装任何全局钩子**
        # （第 72 轮已把那段 WheelWatcher 死代码从 core/worker.py 里删掉），
        # 滚轮预测跟随功能整体下线；滚动跟随仍由后台 FollowReader + 缓动负责。
        # 改用 Raw Input INPUTSINK（安全的滚轮事件来源：只是事件副本，不在输入投递链路上）
        self.wheel_sink = MouseWheelSink()
        self.wheel_sink.wheeled.connect(self.on_wheel_event)
        self.wheel_sink.clicked.connect(self.on_raw_click)
        # 第 81 轮末（用户："退出程序后关机，有时会弹'xxx 不能为 read'的报错"）：
        # 关机/注销时 Windows 会给每个顶层窗口发 WM_QUERYENDSESSION → 我们在那个原生窗口里
        # 立刻把"正在生成的那次"取消掉（llama.cpp 的取消 1 个 token 内生效、线程安全），
        # 免得系统收尾时有线程正卡在 CUDA 里 → 那种小概率的访问冲突弹窗。
        self.wheel_sink.on_session_end = self._on_session_end
        # 状态灯左侧的"上下文历史"入口（小图标）与查看面板
        self.context_button = ContextButton(cfg, on_click=self.toggle_context_viewer)
        self.context_viewer = ContextViewer(cfg)
        self._raw_click_active = bool(getattr(self.wheel_sink, "registered", False))
        # 抢占式取消：本地后端开（新目标立刻顶掉正在生成的），API 后端关（避免重复计费）
        # 抢占取消：默认关闭（用户要求"优先分析最新消息、不要打断它"）；
        # API 后端本来就关（避免重复计费）。需要时可在 config 里打开。
        self.queue.preempt = bool(cfg.analyzer.preempt_enabled
                                  and cfg.analyzer.backend != "api")
        self.analyzer_worker.analysisStarted.connect(self.on_analysis_started)
        self.analyzer_worker.analysisPartial.connect(self.on_analysis_partial)
        self.analyzer_worker.analysisReady.connect(self.on_analysis_ready)
        self.analyzer_worker.analysisCancelled.connect(self.on_analysis_cancelled)
        self.analyzer_worker.modelLoaded.connect(self.on_model_loaded)

        self.mouse_timer = QTimer()
        self.mouse_timer.setInterval(self.IDLE_POLL_MS)
        self.mouse_timer.timeout.connect(self.on_mouse_tick)
        self.geometry_timer = QTimer()
        self.geometry_timer.setInterval(cfg.ui.follow_poll_ms or self.GEOMETRY_POLL_MS)
        self.geometry_timer.timeout.connect(self.on_geometry_tick)
        # 高频跟随：直接刷新已缓存元素的坐标（30ms 级），比整棵重读快两个数量级
        self.fast_timer = QTimer()
        self.fast_timer.setInterval(30)
        self.fast_timer.timeout.connect(self.on_fast_tick)

        self._build_tray()

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        # 第 71 轮：**启动时不直接显示**任何贴在 QQ 上的浮窗 —— 交给
        # `_set_chat_available(True)`（读到消息列表后）统一显示。
        # 否则没有会话窗口时，状态灯/上下文图标会以默认坐标飘在屏幕上（用户反馈过）。
        self.toast.show_toast("ChatAssistant 已启动", "正在加载模型…",
                              near=self._dot_physical_rect() if self.window_rect else None,
                              duration_ms=6000)
        # 第 74 轮：首次用 GPU 加载模型要编译/缓存显卡内核（30-120s 很正常），
        # 这期间状态灯还没变绿；给日志留进度，免得被当成"卡死"（用户反馈第一次卡、第二次就好）。
        self._model_ready = False
        self._load_started_ts = time.time()
        self._load_log_ts = 0.0
        self.mouse_timer.start()
        self.geometry_timer.start()
        self.fast_timer.start()
        self.capture.start()
        self.follow_reader.start()
        self.motion_tracker.start()
        self.analyzer_worker.start()
        logger.info("ChatAssistant 已启动（模型：%s）", app_config.resolve_model_path(self.cfg))
        # config.json 会覆盖代码默认值（SPEC 6.1 踩过 5 次）：启动时把"与代码默认值不同"的键列出来，
        # 这样"改了默认值却忘了同步 JSON"（或反过来）一眼可见。
        overrides = app_config.config_diff_vs_defaults()
        if overrides:
            logger.info("配置自检：config.json 有 %d 处与代码默认值不同 → %s",
                        len(overrides), "；".join(overrides))

    def _on_session_end(self, hard: bool = False) -> None:
        """系统要关机/注销（WM_QUERYENDSESSION）时，由 Raw Input 原生窗口直接调进来。

        第 81 轮末（用户："退出程序后关机，有时会弹'xxx 不能为 read'的报错"）：
        这种报错是**原生层的访问冲突**弹窗，典型成因是进程正在收尾时还有线程卡在
        llama.cpp/CUDA 里（系统只给几秒钟，Qt 事件队列可能都来不及跑）。
        所以这里只做两件**最快、最关键**的事：取消正在跑的生成 + 让读取线程先歇着；
        界面收尾不做（马上要断电了，做了也没人看，反而多一次和 CUDA 抢锁的机会）。
        `hard=True`（WM_ENDSESSION，会话真的要结束）时，调用方会紧接着 `os._exit(0)`。
        """
        try:
            self.queue.cancel_active()          # 线程安全：llama.cpp 1 个 token 内停下
        except Exception as exc:
            logger.debug("关机时取消生成失败：%s", exc)
        try:
            self.capture.set_paused(True)       # 别再去读 QQ 的 UIA 了
        except Exception as exc:
            logger.debug("关机时暂停读取失败：%s", exc)
        logger.info("关机收尾：已取消在跑的生成、暂停读取（hard=%s）", hard)

    def shutdown(self) -> None:
        """退出：**先让界面立刻消失**，再做后台收尾，每一步都打点计时。

        第 79 轮（用户："退出软件的速度似乎有点慢"）：旧实现按
        "卸钩子 → 停定时器 → 等 CaptureWorker(最多 2s) → 等 AnalyzerWorker(最多 4s)
         → 释放模型 → 隐藏窗口 → 存配置" 的顺序 —— 界面（状态灯/面板/图标）一直留在屏幕上
        等前面几步跑完，用户看到的就是"点了退出还赖着不走"。
        现在：隐藏浮窗放在最前面（观感即时），等待时间也收紧，并把每一步耗时写进日志，
        下次再慢能直接从日志看出来是哪一步。
        """
        t_start = time.perf_counter()
        logger.info("正在退出……")

        # ① 界面先消失（用户看到的就是这一下）
        for widget in (self.dot, self.panel, self.options, self.context_button,
                       self.context_viewer, self.hint, self.toast):
            try:
                widget.hide()
            except Exception:
                pass

        # ② 停掉所有定时器与钩子（都是毫秒级）
        for timer in (self.mouse_timer, self.geometry_timer, self.fast_timer,
                      self.dot._fast_timer if hasattr(self.dot, "_fast_timer") else None):
            try:
                if timer is not None:
                    timer.stop()
            except Exception:
                pass
        if self._hook_handle:
            w32.uninstall_hook(self._hook_handle)
            self._hook_handle, self._hook_proc = 0, None
        if self._cursor_hook:
            w32.uninstall_hook(self._cursor_hook)
            self._cursor_hook, self._cursor_proc = 0, None
        logger.info("退出计时：界面隐藏 + 钩子/定时器 %.0fms",
                    (time.perf_counter() - t_start) * 1000)

        # ③ 停后台线程：先发停止信号（会顺带取消在跑的生成），再各自给一个**短**超时
        t = time.perf_counter()
        # ③a 第 79 轮：先把"正在生成的那条"取消掉 —— 否则线程要等这一段生成跑完
        #     （最长 1.2s 才被 wait 放弃），用户感觉就是"点了退出还在转"。
        try:
            if self.queue.cancel_active():
                logger.info("退出：已取消在跑的生成")
        except Exception as exc:
            logger.debug("取消生成异常：%s", exc)
        self.capture.stop()
        self.analyzer_worker.stop()
        try:
            self.queue.wake()          # 立刻叫醒"等任务"的循环，不用干等这次 0.2s 超时
        except Exception as exc:
            logger.debug("唤醒队列异常：%s", exc)
        capture_ok = self.capture.wait(600)
        logger.info("退出计时：CaptureWorker 停止 %.0fms（%s）",
                    (time.perf_counter() - t) * 1000, "正常" if capture_ok else "超时未停")

        t = time.perf_counter()
        analyzer_ok = self.analyzer_worker.wait(1200)
        logger.info("退出计时：AnalyzerWorker 停止 %.0fms（%s）",
                    (time.perf_counter() - t) * 1000,
                    "正常" if analyzer_ok else "超时（可能还在生成，交给进程退出回收）")

        # ④ UIA 监听器：COM 注销有时要几百毫秒，单独计时
        t = time.perf_counter()
        try:
            self.reader.close()
        except Exception as exc:
            logger.debug("reader.close 异常：%s", exc)
        logger.info("退出计时：UIA 释放 %.0fms", (time.perf_counter() - t) * 1000)

        # ④b 第 79 轮：之前**漏停**了这两条线程 —— Raw Input 消息循环线程（ui/wheel_sink）
        #     和填入线程（_FillWorker）。它们不死，Qt 退出时会一直等/最终强杀，进程看起来
        #     "点了退出还赖着几秒"（实测：收尾 447ms，进程消失却要 4.3s）。
        t = time.perf_counter()
        try:
            self.wheel_sink.stop()
        except Exception as exc:
            logger.debug("停 Raw Input 线程异常：%s", exc)
        worker = getattr(self, "_fill_worker", None)
        try:
            if worker is not None and worker.isRunning():
                worker.wait(400)
        except Exception as exc:
            logger.debug("等填入线程异常：%s", exc)
        logger.info("退出计时：Raw Input / 填入线程 %.0fms", (time.perf_counter() - t) * 1000)

        # ⑤ 存配置（必须做，且很快）
        t = time.perf_counter()
        save_config(self.cfg)
        logger.info("退出计时：保存配置 %.0fms", (time.perf_counter() - t) * 1000)

        # ⑥ 不释放模型（第 79 轮）：`analyzer.close()` = llama_free + 销毁 CUDA 上下文，
        #    实测 **254ms**，而我们紧接着就 os._exit 结束进程 —— 显存/内存由系统在进程消失时
        #    回收，这一步纯属白等。需要"优雅释放"的场合（跑测试、复用进程）自己调用 close()。
        logger.info("已退出（总计 %.0fms；模型不显式释放，交给系统回收）",
                    (time.perf_counter() - t_start) * 1000)

    # ---------------- 托盘 ----------------
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(load_app_icon(), self.app)
        self.tray.setToolTip("ChatAssistant")
        menu = QMenu()
        self.status_action = QAction("状态：启动中…", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()
        self.pause_action = QAction("暂停分析", menu)
        self.pause_action.triggered.connect(lambda *_: self.toggle_pause())
        menu.addAction(self.pause_action)
        reprobe = QAction("立即重探 QQ 窗口", menu)
        reprobe.triggered.connect(lambda *_: self.force_reprobe())
        menu.addAction(reprobe)
        # 第 79 轮：一键导出 UIA 诊断包（QQ 再更新、读不到/读错时，让对方发这个文件即可）
        diagnose_action = QAction("导出 UIA 诊断包", menu)
        diagnose_action.triggered.connect(lambda *_: self.export_diagnose())
        menu.addAction(diagnose_action)
        settings_action = QAction("设置…", menu)
        # 注意：QAction.triggered 会带一个 bool 参数；直接连无参槽函数会抛 TypeError
        # （表现为"托盘里点设置没反应"）→ 统一用 lambda 吸收参数
        settings_action.triggered.connect(lambda *_: self.open_settings())
        menu.addAction(settings_action)
        menu.addSeparator()
        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(lambda *_: self.app.quit())
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.show()

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.capture.set_paused(self.paused)
        self.pause_action.setText("继续分析" if self.paused else "暂停分析")
        if self.paused:
            self.dot.set_status("paused")
            self.panel.hide()
            self.options.hide()

    def force_reprobe(self) -> None:
        logger.info("手动重探 QQ 窗口")
        self.reader.uia.close()
        self.reader.status = "init"
        self.queue.clear()

    def export_diagnose(self) -> None:
        """一键导出 UIA 诊断包（托盘菜单）。失败也要给用户一个明确提示，不许静默。"""
        from core import diagnose

        try:
            path = diagnose.write(self.reader.uia, self.cfg)
            logger.info("已导出 UIA 诊断包：%s", path)
            self.toast.show_toast("已导出诊断包", str(path),
                                  near=self._dot_physical_rect() if self.window_rect else None,
                                  duration_ms=4000)
        except Exception as exc:
            logger.warning("导出诊断包失败：%s: %s", type(exc).__name__, exc)
            self.toast.show_toast("导出诊断包失败", f"{type(exc).__name__}: {exc}"[:60],
                                  near=self._dot_physical_rect() if self.window_rect else None)

    # ---------------- 坐标换算（Win32 物理 → Qt 逻辑） ----------------
    def _logical_rect(self, rect: Sequence[int]) -> QRect:
        scale = self.scale or 1.0
        left = int(round(rect[0] / scale))
        top = int(round(rect[1] / scale))
        width = int(round((rect[2] - rect[0]) / scale))
        height = int(round((rect[3] - rect[1]) / scale))
        return QRect(left, top, max(1, width), max(1, height))

    # ---------------- 绑定到 QQ 窗口：owner + 移动事件钩子 ----------------
    def _bind_overlays_to_window(self, hwnd: int, pid: int) -> None:
        """把三个浮窗设成 QQ 窗口的 owned window，并监听它的移动事件。

        - owned window 的语义 = **永远在 QQ 之上、但不会盖住其他应用**（别的应用激活即排到前面）；
          QQ 最小化时浮窗跟着隐藏，符合"只在 QQ 上层"的要求；
        - WinEvent 钩子让"拖动 QQ 时浮窗零延迟跟随"（比 200ms 轮询丝滑）。
        """
        self._owner_hwnd = hwnd
        if self.cfg.ui.use_owner_window:
            for widget in (self.dot, self.panel, self.options):
                try:
                    widget.set_owner(hwnd)
                except Exception as exc:
                    logger.debug("绑定 owner 失败：%s", exc)
            logger.info("浮窗已绑定到 QQ 窗口 hwnd=%s pid=%s（owner 模式）", hwnd, pid)
        else:
            # 默认方案：不用 owner（反复改 owner 实测会让 QQ 窗口漂移），
            # 改为"QQ 在前台时把浮窗设为 topmost"，见 _sync_topmost()
            logger.info("浮窗跟随 QQ 窗口 hwnd=%s pid=%s（topmost 同步模式）", hwnd, pid)
        if self.cfg.ui.use_move_hook and pid:
            if self._hook_handle:
                w32.uninstall_hook(self._hook_handle)
                self._hook_handle, self._hook_proc = 0, None
            self._hook_handle, self._hook_proc = w32.install_location_hook(
                pid, self._on_window_move)
            logger.info("窗口移动钩子：%s", "已安装" if self._hook_handle else "安装失败（回退轮询）")

    def _on_window_move(self, event: int, hwnd: int) -> None:
        """WinEvent 回调（主线程消息循环里执行）：合并成一次重排，避免抖动。"""
        if hwnd != self._owner_hwnd or self._place_pending:
            return
        self._place_pending = True
        QTimer.singleShot(0, self._place_after_move)

    def _on_cursor_event(self, event: int, hwnd: int, id_object: int) -> None:
        """鼠标移动事件（OBJID_CURSOR）：让"悬停切换消息"零延迟、拖动时也跟手。"""
        if id_object != w32.OBJID_CURSOR or self.paused:
            return
        self._cursor_events += 1
        now = time.time()
        if now - self._last_event_report > 5.0:      # 诊断：每 5 秒留一行，便于排查"不切换"
            logger.debug("光标事件：%d 次/5s", self._cursor_events)
            self._cursor_events = 0
            self._last_event_report = now
        if now - self._last_cursor_ts < 0.03:      # 30ms 节流，避免事件洪水拖慢 UI
            return
        self._last_cursor_ts = now
        if not self._hover_pending:
            self._hover_pending = True
            QTimer.singleShot(0, self._process_hover)

    def _process_hover(self) -> None:
        self._hover_pending = False
        self.on_mouse_tick()

    def _place_after_move(self) -> None:
        self._place_pending = False
        window = self.snapshot.get("window") or {}
        if window.get("hwnd"):
            self.window_rect = w32.get_window_rect(int(window["hwnd"]))
        self._place_windows()

    def _physical_size(self, widget) -> tuple[int, int]:
        scale = self.scale or 1.0
        return (int(widget.width() * scale), int(widget.height() * scale))

    def _dot_physical_rect(self) -> List[int]:
        """状态灯的物理矩形：首选 QQ 窗口**上沿外侧**（不与标题栏按钮重叠）；
        上方没空间时退到标题栏内、并把窗口按钮区域让开。"""
        margin = self.cfg.ui.dot_margin_px
        size = self.cfg.ui.dot_size_px
        screen = w32.get_virtual_screen_rect()
        # 用户口径（第 48 轮）：放到**聊天界面右上角的空白处**（消息列表右上角，
        # 一般在第一条可见消息上方），比贴标题栏好看。可用 ui.dot_position 切回旧行为。
        if getattr(self.cfg.ui, "dot_position", "") == "chat_top_right":
            list_box = self._translated_list_box()
            if list_box:
                x = int(list_box[2]) - margin - size
                y = int(list_box[1]) + margin
                x = max(screen[0], min(x, screen[2] - size))
                y = max(screen[1], min(y, screen[3] - size))
                return [x, y, x + size, y + size]
        x = self.window_rect[2] - margin - size
        y = self.window_rect[1] - size - 6
        if y < screen[1]:
            y = self.window_rect[1] + margin
            x = self.window_rect[2] - margin - size - self.cfg.ui.dot_avoid_buttons_px
        x = max(screen[0], min(x, screen[2] - size))
        y = max(screen[1], min(y, screen[3] - size))
        return [x, y, x + size, y + size]

    # ---------------- 读取回调（主线程） ----------------
    def on_messages(self, messages: List[Message], snapshot: Dict[str, Any]) -> None:
        self.messages, self.snapshot = messages, snapshot
        # 第 79 轮续（用户："有时候按空白区域面板不会收回，要再点一两下"）：
        # 记下"这份坐标是什么时候读到的" —— 点击没命中时用它判断"能不能信这一判"。
        self._messages_ts = time.time()
        # 第 79 轮：有"等新坐标重判"的点击 → 用这份新数据补判一次（见 _toggle_target）
        try:
            self._retry_pending_click()
        except Exception as exc:
            logger.debug("补判点击异常：%s", exc)
        # 上下文历史面板开着时实时刷新（用户反馈：不重开就看不到新记录）。
        # 限流 0.6s，避免每 120-300ms 一次读取都去重排富文本。
        if self.context_viewer.isVisible():
            now = time.time()
            if now - getattr(self, "_ctx_viewer_refresh_ts", 0.0) > 0.6:
                self._ctx_viewer_refresh_ts = now
                title, body = self._context_snapshot_text()
                self.context_viewer.set_history(title, body, keep_scroll=True)
        # 换会话检测：用**窗口标题**（QQ 标题栏就是当前会话名），而不是"消息全变了"——
        # 实测滚动 3 格也会让可见消息全变，用消息集合判断会误判成换会话（反而拖慢滚动）。
        # 换会话时 QQ 正在重建无障碍树，我们再插进去读会明显加重卡顿 → 安静 1.5s 让它先建完。
        title = str(((snapshot.get("window") or {}).get("title")) or "")
        clean_title = "".join(ch for ch in title if not ch.isdigit() and ch not in "（）()")
        last_title = getattr(self, "_last_window_title", None)
        self._last_window_title = clean_title
        if clean_title and last_title and clean_title != last_title:
            # 第 79 轮（用户："刚切换会话时点击聊天气泡的前几下还是经常没反应"）：
            # 安静期里旧会话的矩形还留在 self.messages 里 → 点新会话的气泡必然判成"没命中"，
            # 而这段时间又读不到新坐标 → 前几下点击全落空。现在两处一起改：
            #   ① 丢掉上一个会话的缓存矩形（宁可"暂时没有候选"，也不要拿旧坐标硬判）；
            #   ② 安静期从 1.5s 收到 1.0s，并且**用户一在聊天里点击就提前结束它**（见 _toggle_target）。
            self.messages = []
            self._switch_quiet_until = time.time() + 1.0
            self.capture.set_paused(True)
            QTimer.singleShot(1000, lambda: self.capture.set_paused(False))
            logger.info("检测到切换会话 → 暂停读取 1s，避免和 QQ 重建无障碍树抢资源")
        window = snapshot.get("window") or {}
        # 【第 68d 轮 · 状态灯/浮窗快速拖动时"闪现"的根因】
        # Chromium 在**快速拖动窗口时不更新无障碍矩形**（UIA 坐标还停在拖动前的位置），
        # 但 `window["rect"]` 是 Win32 实时值。两者一起写进 (list_box, _read_window_rect) 这对
        # 坐标基准后，`dx = 当前窗口x − 记录时窗口x` 就变成 ≈0，于是浮窗按**过期的绝对坐标**
        # 又摆回拖动前的位置 —— 下一次读取再摆回来，肉眼就是"闪"。
        # 规则：窗口正在移动期间**冻结"UIA 那一份"**（messages 的 bbox / list_box /
        # _read_window_rect 保持同一批），只让窗口位移做刚性平移；停手 ~0.25s 后再接纳新数据。
        moving = time.time() < getattr(self, "_window_moving_until", 0.0)
        if window.get("rect"):
            self.window_rect = window["rect"]
            if messages and not moving:
                self._read_window_rect = list(window["rect"])   # 记录"这批 bbox 对应的窗口位置"
            self.scale = w32.get_window_scale(int(window["hwnd"])) or 1.0
            if int(window["hwnd"]) != self._owner_hwnd:
                self._bind_overlays_to_window(int(window["hwnd"]), int(window.get("pid") or 0))
        if not moving:
            self.list_box = (snapshot.get("uia") or {}).get("list_box")
        # 目标消息滚出可视区 → 收起面板（否则面板会停在过期位置"乱跑"）
        # 注意：必须**连续两次**都找不到才收，避免某次读取不完整导致面板闪
        if messages and self.target is not None:
            visible_keys = {m.msg_key for m in messages}
            if self.target.msg_key not in visible_keys:
                self._target_missing_streak += 1
                if self._target_missing_streak >= 2:
                    logger.info("目标消息已滚出可视区，收起面板：%s",
                                self._log_text(self.target.text, 16))
                    self.target = None
                    self._target_missing_streak = 0
                    self._hide_overlays(reason="target 滚出可视区")
            else:
                self._target_missing_streak = 0
        status = snapshot.get("status", "init")
        # 防抖：偶尔一次读不到不要立刻灰灯/收面板（否则会闪）
        if status == "ready" and messages:
            self._status_streak = 0
        else:
            self._status_streak += 1
        degraded = self._status_streak >= 4
        if not degraded:
            self.dot.set_status("analyzing" if self._analyzing else
                                ("ready" if status == "ready" else self.dot.status))
        # 第 68 轮：会话窗口可用性（用户口径："我只希望在会话窗口上显示"）。
        # 判定 = 读到了消息 + 状态 ready + 拿到了「消息列表」矩形（有它才知道往哪贴）。
        # 不可用（没开会话 / 只剩会话列表 / 最小化 / UIA 读不到）→ 浮窗全隐；
        # 用防抖（连续 4 次不合格）避免偶发一次读不到就整屏闪。
        if degraded or self.paused:
            self._set_chat_available(False)
        elif status == "ready" and messages and self.list_box:
            self._set_chat_available(True)
        self.status_action.setText(
            f"状态：{status}｜消息 {len(messages)} 条｜读取 {snapshot.get('last_read_ms', 0):.0f}ms")
        if degraded or self.paused:
            self.dot.set_status(status if status != "ready" else "paused")
            self._hide_overlays(reason=f"status={status} streak={self._status_streak}")
            self.target = None
            return
        # 点击触发模式（默认）：**不**因为鼠标停在某条消息上就建目标/开始分析，
        # 目标只由 _poll_click（点一下）决定；悬停模式才走这里自动跟随。
        if self.cfg.ui.trigger_mode == "hover":
            self._update_target(auto=True)

    def _set_chat_available(self, available: bool) -> None:
        """会话窗口可用/不可用 → 统一显示或隐藏"贴在 QQ 上"的浮窗（第 68 轮）。

        用户口径："我只希望在会话窗口上显示"。QQ 没开会话（只剩会话列表）、窗口最小化、
        或 UIA 读不到「消息列表」时，状态灯 / 上下文小图标 / 上下文面板一起隐藏 ——
        否则它们会飘在会话列表窗口（或别的应用）上面，看起来就是"状态灯错位 / 识别到列表窗口"。
        """
        if self._chat_available == available:
            return
        self._chat_available = available
        logger.info("会话窗口%s → %s浮窗", "可用" if available else "不可用",
                    "显示" if available else "隐藏")
        if available:
            if not self._hidden_by_foreground:
                self.dot.show()
                self.context_button.show()
                if self._ctx_user_open:
                    self._ctx_anim_target = 1.0
            self._place_windows()
            return
        self.dot.hide()
        self.context_button.hide()
        self.panel.hide()
        self.options.hide()
        self._panel_anim = 0.0
        self._panel_anim_target = 0.0
        self._ctx_anim = 0.0
        self._ctx_anim_target = 0.0
        self.context_viewer.hide()

    def _hide_overlays(self, reason: str) -> None:
        """收起浮窗。开了动画时先播"向消息一侧收起"的动画，播完再真正隐藏。"""
        if self.panel.isVisible():
            logger.info("收起面板（%s）", reason)
            if getattr(self.cfg.ui, "anim_enabled", True) and self._panel_anim > 0.02:
                self._panel_anim_target = 0.0        # 交给快 tick 播动画，播完自己 hide
                # 选项条与面板**一起收回**：这里不再立刻 hide，交给同一段动画淡出
                return
        self.panel.hide()
        self.options.hide()
        self._panel_anim = 0.0
        self._panel_anim_target = 0.0
        self._intentional_hide_ts = time.time()   # 供"自愈"判断：刚故意收起的别马上弹回来
        self._smooth_prev.clear()        # 下次出现直接落位，别从上一次的缓动中间态滑过来
        self._locked_panel_x = None      # 收起即解锁：下次定位重新选位置
        self._locked_panel_target = None
        # 收起时立刻摘掉 topmost：否则选项条/面板可能还压在其他应用上面（用户反馈过）
        self._drop_topmost()

    def _drop_topmost(self) -> None:
        for widget in (self.dot, self.panel, self.options,
                       self.context_button, self.context_viewer, self.hint):
            try:
                hwnd = int(widget.winId())
                if w32.is_topmost(hwnd):
                    w32.set_topmost(hwnd, False)
            except Exception:
                pass

    # ---------------- 目标与显示 ----------------
    def _hovered_message(self, padding: Optional[int] = None,
                         wide: bool = False) -> Optional[Message]:
        """命中测试：先看矩形包含（带容差），都没有时退到"同列最近的一条"。

        用**平移后的 bbox**，所以窗口一移动就立刻用新坐标命中（不需要等下一次读取）。
        `wide=True`（点击路径）时把命中范围横向放宽到整行自己那一侧，见 pick_hovered_message。
        """
        messages = self._translated_messages()
        if not messages:
            return None
        cursor = w32.get_cursor_pos()
        # 鼠标必须真的在 QQ 窗口（或我们的浮窗）上：否则会"透过其他应用"命中下面的消息
        top = w32.top_window_at_point(*cursor)
        if top:
            ours = {int(self.dot.winId()), int(self.panel.winId()), int(self.options.winId())}
            if top != self._owner_hwnd and top not in ours:
                return None
        if padding is None:
            padding = max(2, min(6, self.cfg.ui.hover_padding_px - 4))
        row_bounds = self._translated_list_box() if wide else None
        return pick_hovered_message(cursor, messages, padding=int(padding),
                                    only_other_party=self.cfg.ui.only_other_party,
                                    row_bounds=row_bounds)

    def _translated_messages(self) -> List[Message]:
        """把缓存的消息 bbox 按"当前窗口位置 - 上次读取时窗口位置"平移。

        这是"拖动 QQ 时面板也要跟手"的关键：消息坐标是读取那一刻的屏幕坐标，
        窗口移动后必须整体加上位移，否则面板会等到下一次读取（最多 500ms）才追上。
        """
        if not self.messages or self._read_window_rect is None or self.window_rect is None:
            return self.messages
        dx = int(self.window_rect[0]) - int(self._read_window_rect[0])
        dy = int(self.window_rect[1]) - int(self._read_window_rect[1])
        if dx == 0 and dy == 0:
            return self.messages
        shifted = []
        for msg in self.messages:
            bbox = (msg.bbox[0] + dx, msg.bbox[1] + dy, msg.bbox[2] + dx, msg.bbox[3] + dy)
            shifted.append(Message(msg_key=msg.msg_key, text=msg.text, side=msg.side,
                                   bbox=bbox, backend=msg.backend, is_image=msg.is_image,
                                   avatar_anchor=msg.avatar_anchor))
        return shifted

    def _translated_list_box(self) -> Optional[Sequence[int]]:
        if not self.list_box or self._read_window_rect is None or self.window_rect is None:
            return self.list_box
        dx = int(self.window_rect[0]) - int(self._read_window_rect[0])
        dy = int(self.window_rect[1]) - int(self._read_window_rect[1])
        return [self.list_box[0] + dx, self.list_box[1] + dy,
                self.list_box[2] + dx, self.list_box[3] + dy]

    def _original_message(self, msg_key: str,
                          near_bbox: Optional[Sequence[int]] = None) -> Optional[Message]:
        """按 msg_key 取"最近一次读取里的那条原始消息"。

        第 80 轮（用户实测："我和对方发了同样的文本，点下面那条只会被判成上面那条"）：
        同一个 key 可能对应**多条**消息（同侧同文本也会重复），以前无脑返回第一条 →
        点下面那条会被折算成上面那条（面板贴错位置、显示的是另一条的分析）。
        现在给 `near_bbox` 时取**纵向最接近**的那条；不传时保持原行为（返回第一条）。

        坐标口径：`near_bbox` 必须和命中测试一样是"**平移后**的坐标"
        （`_translated_messages()`），所以锚点那边用 `_translated_bbox_of(target)` 换算。
        """
        candidates = [m for m in self.messages if m.msg_key == msg_key]
        if not candidates:
            return None
        if near_bbox is None or len(candidates) == 1:
            return candidates[0]
        translated = self._translated_messages()
        indexes = [i for i, m in enumerate(self.messages) if m.msg_key == msg_key]
        best = min(indexes,
                   key=lambda i: (abs(int(translated[i].bbox[1]) - int(near_bbox[1])),
                                  abs(int(translated[i].bbox[0]) - int(near_bbox[0]))))
        return self.messages[best]

    def _translated_bbox_of(self, message: Optional[Message]) -> Optional[Sequence[int]]:
        """把某条消息的 bbox 换到"当前窗口位置"下的坐标（与命中测试同一套口径）。"""
        if message is None:
            return None
        for index, item in enumerate(self.messages):
            if item is message:
                translated = self._translated_messages()
                if index < len(translated):
                    return translated[index].bbox
                break
        return message.bbox

    def _update_target(self, auto: bool) -> None:
        self._last_activity_ts = time.time()      # 换目标也算"在用" → 读取提速 5s
        if not self.messages or self.window_rect is None:
            return
        hovered = self._hovered_message()
        if hovered is not None:
            # 命中测试用的是"平移后的副本"，换回原始对象（坐标系统一，避免上下文裁剪错位）
            hovered = self._original_message(hovered.msg_key, hovered.bbox) or hovered
        latest = self.reader.latest_other_party(self.messages)
        if self.cfg.analyzer.prioritize_latest and latest is not None:
            # （默认关闭）最新那条优先：它没出结果前悬停不抢
            latest_result = self.results.get(latest.msg_key)
            latest_done = latest_result is not None and not latest_result.partial
            target = (hovered or (latest if auto else None)) if latest_done else latest
        elif hovered is not None:
            target = hovered                      # 用户口径：鼠标指哪条就分析哪条
        elif not auto:
            target = None                         # 鼠标移开 → 保持当前目标，不自动切换
        elif self.cfg.analyzer.analyze_latest_automatically:
            target = latest                       # （默认关闭）自动分析屏幕最下面那条
        else:
            target = self.target                  # 什么都不做：保留现有目标
        if target is None:
            self.panel.hide()
            self.options.hide()
            return
        changed = self.target is None or target.msg_key != self.target.msg_key
        if changed:
            logger.info("切换目标 → [%s] %s（msg_key=%s）",
                        "对方" if target.is_other_party else "我",
                        self._log_text(target.text, 20), target.msg_key)
        self.target = target
        if target.is_image:
            # SPEC 第十章：图片消息不做臆测，只提示"未分析"
            self.panel.show_notice("图片消息（未分析）",
                                   "这是一条图片消息，没有文字可分析。",
                                   hint="可换到文字消息上查看分析")
            if not self._hidden_by_foreground:
                self._reveal_panel()
            self.options.hide()
            self._place_windows()
            return
        if changed:
            self._render_target()

    def _render_target(self) -> None:
        target = self.target
        if target is None or self.window_rect is None:
            return
        # 第 79 轮：点开/切到某条消息 = 新的一轮显示 → 允许选项条重新出现
        # （否则"点过选项"的抑制会一直生效，第二次点这条消息就再也看不到选项了）。
        self._options_dismissed_key = None
        # （选项条最右侧那个小圆点是**悬停指示**，由选项条自己按 `_hover` 画，这里不用管。）
        result = self.results.get(target.msg_key)
        if result is None:
            self.panel.show_analyzing(target)
            self.options.hide()
            self._request_analysis(target)
        elif result.partial:
            # 上次只跑到第一段（选项被抢占取消）→ 先显示已有内容，同时重新请求补全
            self._show_result(result)
            self._request_analysis(target)
        else:
            self._show_result(result)
        if not self._hidden_by_foreground:      # QQ 不在前台时不显示（避免压住别的应用）
            self._reveal_panel()
        self._place_windows()

    def _request_analysis(self, target: Message, force: bool = False) -> None:
        """`force=True`：同一条消息正在分析时也重新排一次（手动刷新用）。

        第 80 轮：选项条的刷新图标在"选项生成中"（占位行）时也能点到，那时
        `pending_key` 正好是这条消息 —— 旧代码会在这里直接 return，用户看到的就是
        "点了刷新没反应"。本地后端重新排队会先取消正在跑的那次（抢占），
        API 后端**不强制**（会白白多花一次 token，等这次回来再点即可）。
        """
        if not force and self.pending_key == target.msg_key:
            return
        context = self.reader.build_context(self.messages, target)
        self._last_context_count = len(context)
        self._pending_since = time.time()          # 看门狗：这次分析的开始时间
        self._stall_notified = False
        generation = self.queue.put(target, context)
        self.pending_key = target.msg_key
        logger.info("请求分析 #%s（gen=%d，上下文 %d 条）：%s",
                    target.msg_key, generation, len(context), self._log_text(target.text))

    def _log_text(self, text: str, limit: int = 24) -> str:
        """日志里的消息原文：`log_message_text=False` 时只记长度（脱敏，第 63 轮加固）。"""
        if not getattr(self.cfg, "log_message_text", True):
            return f"<{len(text)} 字，已脱敏>"
        return text[:limit]

    def _show_result(self, result: AnalysisResult) -> None:
        """把一条分析结果画到面板上（partial 与完整结果都走这里）。"""
        model_tag = (self.analyzer.model_path.stem.split("-")[1] if self.analyzer.model_path
                     else "本地")
        backend_note = f"本地{model_tag}" if not result.degraded else f"本地{model_tag}（降级）"
        if self.cfg.analyzer.backend == "api":
            backend_note = "API"
        self.panel.show_analysis(result, self.target, backend_note=backend_note,
                                 context_count=self._last_context_count)
        if not self._hidden_by_foreground:
            self._reveal_panel()
        # 第 79 轮：自己的消息 → 选项窗口标题改成"发言选项"（配置里可改名）
        self.options.set_self_mode(bool(getattr(result, "self_analysis", False)))
        # 第 79 轮：用户已经点过这条消息的选项 → 后续 partial 不再弹选项条
        # （面板内容照常刷新，只是不把选项条重新展开）。
        if self._options_suppressed(result):
            return
        options = []
        for reply in result.replies:
            style = ""
            if self.cfg.ui.show_reply_style_prefix:
                raw = (reply.style or "").strip()
                style = self.cfg.ui.style_label_map.get(raw, raw)
            options.append({"style": style, "text": reply.text})
        if options:
            # 内容变化（例如"选项生成中"占位 → 三条真选项）也要重新播一遍入场动画，
            # 否则占位阶段动画已经走到 1.0，真选项就是"啪"地出现（用户反馈"选项没动画"）。
            if not self.options.isVisible() or self.options.is_placeholder():
                self._options_anim = 0.0
            self._options_anim_target = 1.0
            self.options.set_options(options)
            if not self._hidden_by_foreground:      # 其他应用在上层时不许冒出来（用户反馈过）
                self.options.show()
                self.options.raise_()
        elif result.partial:
            # 第一段（意图/情绪/危险度/建议）已上屏 → 选项位置显示占位，等第二段补上
            if not self.options.isVisible():
                self._options_anim = 0.0
            self.options.show_placeholder("选项生成中")
            if not self._hidden_by_foreground:
                self.options.show()
                self.options.raise_()
        else:
            self.options.hide()

    def _options_suppressed(self, result: AnalysisResult) -> bool:
        """这条消息的选项是否已被用户用过（点过任意一条）→ 后续 partial 不再弹选项条。

        第 79 轮用户口径："选项在逐个上屏过程中也可以点击已生成的选项直接填入并收起选项面板，
        不用等所有选项加载完"。点了之后流式还在跑，若不抑制，下一次 partial 会把选项条
        重新展开（看起来像"点了没用"）。
        """
        if not result.replies:
            return False
        key = getattr(self, "_options_dismissed_key", None)
        return bool(key) and result.msg_key == key

    # ---------------- 首次启动提示（第 79 轮） ----------------
    def _place_first_run_hint(self, dot_logical: QRect) -> None:
        """把"首次启动提示"放在状态灯下方；会话不可用/QQ 不在前台时隐藏。

        用户口径：首次启动显示一行使用说明，点一下淡出关闭，后续启动不再出现。
        """
        if getattr(self, "_hint_done", True):
            return
        if not self._chat_available or self._hidden_by_foreground:
            if self.hint.isVisible():
                self.hint.hide()
            return
        screen = w32.get_virtual_screen_rect()
        logical_screen = [int(v / (self.scale or 1.0)) for v in screen]
        # 尽量别越过聊天界面：优先夹在"消息列表矩形"里（拿不到就退回 QQ 窗口矩形）
        keep_inside = None
        raw_box = self._translated_list_box() or self.window_rect
        if raw_box:
            keep_inside = self._logical_rect(raw_box)
            keep_inside = [keep_inside.x(), keep_inside.y(),
                           keep_inside.right(), keep_inside.bottom()]
        self.hint.place_under(dot_logical, logical_screen, keep_inside)
        if not self.hint.isVisible():
            self.hint.show()
            self.hint.raise_()
            self._ensure_owner(self.hint)
            # 显示过就记下来：**后续启动不再出现**（用户口径）。
            if not bool(getattr(self.cfg.ui, "first_run_hint_shown", False)):
                self.cfg.ui.first_run_hint_shown = True
                try:
                    app_config.save_config(self.cfg)
                    logger.info("首次启动提示已显示（下次启动不再出现）")
                except Exception as exc:
                    logger.warning("首次启动提示标记写入失败：%s", exc)

    def _dismiss_first_run_hint(self) -> None:
        """点了提示（或它自己淡出完）→ 本轮不再显示。"""
        self._hint_done = True
        if self.hint.isVisible():
            self.hint.start_fade_out()

    def _smooth_rect(self, key: str, target: QRect) -> QRect:
        """给面板位置做缓动（横竖都缓），让滚动/换位看起来连续。

        - 远处按上限匀速走（竖 22px/帧、横 60px/帧）；
        - 接近目标时按比例收尾（ease-out），不会"啪"地贴上去；
        - 目标跳得极远（>400px，通常是换目标）时直接落位，不做长距离滑动。
        横向也缓动是为了消掉用户反馈的"移出对话框外时向左瞬移"——那是定位策略切换
        （同排空白 → 气泡下方）导致的横向大位移，以前我刻意不缓动，看起来就是瞬移。
        """
        prev = self._smooth_prev.get(key)
        current = QRect(int(target.x()), int(target.y()), int(target.width()), int(target.height()))
        if time.time() < getattr(self, "_window_moving_until", 0.0):
            # 窗口正在被拖动 → 刚性跟随，不做缓动（缓动只会让它"追不上再回弹"）
            self._smooth_prev[key] = current
            return current
        if prev is not None:
            dx, dy = current.x() - prev.x(), current.y() - prev.y()
            if abs(dx) <= 400 and abs(dy) <= 400 and (dx or dy):
                # 两种模式（用户反馈"跟手但要丝滑"的折中）：
                #  - 追赶模式：滚动进行中（150ms 内有新的位移信号）→ 大步跟上，别落后一拍；
                #  - 落位模式：停手之后的收尾 → 小步缓动，落在目标上不抖。
                catching_up = (time.time() - getattr(self, "_last_motion_ts", 0.0)) < 0.15
                step_x = self._ease_step(dx, 110 if catching_up else 60)
                step_y = self._ease_step(dy, 70 if catching_up else 22)
                current = QRect(prev.x() + step_x, prev.y() + step_y,
                                current.width(), current.height())
        self._smooth_prev[key] = current
        return current

    @staticmethod
    def _ease_step(delta: int, cap: int) -> int:
        """缓动步长：远处匀速（上限 cap），接近目标时按比例收尾，形成 ease-out 手感。"""
        if delta == 0:
            return 0
        magnitude = abs(delta)
        step = min(cap, max(3, int(magnitude * 0.55)))
        if magnitude - step < 6:            # 快到位了就一次走完，避免最后抖动
            step = magnitude
        return step if delta > 0 else -step

    def _reveal_panel(self) -> None:
        """显示面板：首次显示时从 0 展开（配和缓动看起来像"从消息一侧滑出"）。"""
        if not self.panel.isVisible():
            self._panel_anim = 0.0
            self._panel_anim_target = 1.0
            # 新的一次"显示会话"：清掉上一次的滚轮预测残值与横向锁定，
            # 否则新面板会带着旧偏移/旧 x 打开（用户反馈"打开新面板位置不正确"）。
            self._predicted_dy = 0
            self._wheel_notches = 0
            self._locked_panel_x = None
            self._locked_panel_target = None
        elif self._panel_anim_target == 0.0:
            # 正在播放"收起"时又要求显示 → 立刻反向展开（否则会停在"可见但 0 宽"的隐形态，
            # 表现为"收起之后怎么点都展不开"）
            self._panel_anim_target = 1.0
        self.panel.show()          # ← 批量替换时这里被误改成 self._reveal_panel()，
        self.panel.raise_()        #   造成无限递归（点击回调直接抛异常 → 面板永远不出现）

    def _step_panel_anim(self) -> None:
        """推进展开/收起动画：30ms 快 tick × 6 帧 ≈ 180ms 走完一遍。"""
        # 滚轮预测位移自然衰减：滚轮停手后，让预测量在几帧内回到 0，
        # 由 UIA 真实坐标接管（避免预测偏差长期累积）。
        # 注意：这里**刻意不做衰减**。滚轮停手后由 UIA 纠正一次性对账并把预测清零
        # （同帧完成）。之前那版"停手 0.35s 后逐帧衰减"会让面板在等 UIA 期间往回漂
        # —— 用户看到的"先被拉回原位再拉到目标"就是这个漂移。
        if not getattr(self.cfg.ui, "anim_enabled", True):
            self._panel_anim = 1.0
            self._panel_anim_target = 1.0
            return
        if abs(self._panel_anim - self._panel_anim_target) < 0.001:
            if self._panel_anim_target == 0.0 and self.panel.isVisible():
                self.panel.hide()               # 收起到 0 → 真正隐藏
                self.options.hide()             # 选项条与面板**同步**收完
                self._smooth_prev.clear()
                self._locked_panel_x = None
                self._locked_panel_target = None
            return
        step = 1.0 / max(2, int(getattr(self.cfg.ui, "anim_frames", 6)))
        if self._panel_anim_target > self._panel_anim:
            self._panel_anim = min(self._panel_anim_target, self._panel_anim + step)
        else:
            self._panel_anim = max(self._panel_anim_target, self._panel_anim - step)
        if not self.panel.isVisible():
            return
        if self.target is not None:
            self._place_windows()
        else:                                   # 收起过程中 target 可能已清空 → 直接按动画几何收
            geometry = self.panel.geometry()
            full_w = getattr(self, "_panel_full_w", geometry.width())
            anim_w = max(4, int(full_w * self._panel_anim))
            # 第 79 轮：锚边为右（面板贴在自己气泡左侧）时，收起固定**右缘**向左缩
            anim_x = (geometry.x() + geometry.width() - anim_w
                      if getattr(self, "_panel_anchor", "left") == "right"
                      else geometry.x())
            self.panel.setGeometry(QRect(anim_x, geometry.y(), anim_w, geometry.height()))
            self.panel.set_anim_alpha(self._panel_anim)
            # 选项条也要跟着一起收（之前漏了这一步 → 收起时选项条毫无动画、最后被硬隐藏）
            if self.options.isVisible():
                self.options.set_anim_alpha(self._panel_anim)
                box = self.options.geometry()
                opt_w = max(4, int(box.width() * self._panel_anim))
                opt_x = (box.x() + box.width() - opt_w
                         if getattr(self, "_panel_anchor", "left") == "right" else box.x())
                self.options.setGeometry(QRect(opt_x, box.y(), opt_w, box.height()))

    def _write_ui_state(self, btn_phys: Sequence[int], dot_phys: Sequence[int]) -> None:
        """把浮窗关键位置/状态写到 `logs/ui_state.json`（最多 0.3s 一次）。

        用途：验收脚本与排查工具可以直接读它拿"状态灯的物理位置 / 粘在哪个窗口上 /
        会话窗口是否可用"，不用再去日志里 grep（那两行已降到 DEBUG，避免拖动时刷屏）。
        """
        now = time.time()
        if now - getattr(self, "_ui_state_ts", 0.0) < 0.3:
            return
        self._ui_state_ts = now
        try:
            payload = {
                "ts": round(now, 3),
                "chat_available": bool(self._chat_available),
                "hidden_by_foreground": bool(self._hidden_by_foreground),
                "qq_rect": list(self.window_rect) if self.window_rect else None,
                "list_box": list(self.list_box) if self.list_box else None,
                "dot_phys": list(dot_phys),
                "ctx_button_phys": list(btn_phys),
                "ctx_panel_rect": [self.context_viewer.geometry().x(),
                                   self.context_viewer.geometry().y(),
                                   self.context_viewer.geometry().right(),
                                   self.context_viewer.geometry().bottom()],
                "ctx_anim": round(self._ctx_anim, 3),
                "ctx_place_mode": getattr(self, "_ctx_place_mode", "?"),
                "scale": self.scale,
                # 注意：Qt 的 geometry() 是**逻辑坐标**；上面几个字段是物理坐标。
                # 这里再存一份物理换算值，避免排查时把两套坐标混起来看。
                "ctx_panel_phys": [int(self.context_viewer.geometry().x() * self.scale),
                                   int(self.context_viewer.geometry().y() * self.scale),
                                   int(self.context_viewer.geometry().right() * self.scale),
                                   int(self.context_viewer.geometry().bottom() * self.scale)],
                # 第 78 轮：UIA 结构漂移排查入口 —— QQ 更新后先看这三项就知道哪一层兜底在生效
                "read_status": getattr(getattr(self, "reader", None), "status", None),
                "list_strategy": getattr(getattr(getattr(self, "reader", None), "uia", None),
                                         "_list_strategy", None),
                "input_strategy": getattr(getattr(getattr(self, "reader", None), "uia", None),
                                          "_input_strategy", None),
            }
            (Path(app_config.LOG_DIR) / "ui_state.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:                    # 写状态文件绝不影响主流程
            logger.debug("写 ui_state.json 失败：%s", exc)

    def _place_context_viewer(self, screen_phys: Optional[Sequence[int]] = None) -> None:
        """上下文历史面板定位：优先 QQ 窗口右侧；右侧放不下就回到**旧位置**（小图标下方）。

        第 68 轮（用户口径）："当右侧没位置时请放到旧版那个位置，不然会遮挡 QQ 窗口 UI"。
        """
        if self.window_rect is None:
            return
        screen = screen_phys or w32.get_virtual_screen_rect()
        logical_screen = [int(v / (self.scale or 1.0)) for v in screen]
        qq = self._logical_rect(self.window_rect)
        # 上边对齐"聊天面板白色区域顶边"（配置项，逻辑像素；实测 50 ≈ 150% 缩放下 75 物理）
        top_offset = int(getattr(self.cfg.ui, "ctx_panel_top_offset_px", 50) or 0)
        placed = self.context_viewer.place_right_of(
            [qq.x(), qq.y(), qq.right(), qq.bottom()], logical_screen, top_offset=top_offset)
        self._ctx_place_mode = "right" if placed else "old"
        if not placed:
            anchor = getattr(self, "_last_context_button_phys", None)
            if anchor:
                rect = self._logical_rect(anchor)
                self.context_viewer.reset_size()          # 旧位置用默认 360×420
                self.context_viewer.move_to(
                    [rect.x(), rect.y(), rect.right(), rect.bottom()], logical_screen)
            else:
                self.context_viewer.place_right_of(
                    [qq.x(), qq.y(), qq.right(), qq.bottom()], logical_screen)
        self.context_viewer.set_anim(self._ctx_anim)

    def _step_ctx_anim(self) -> None:
        """上下文历史面板的展开/收起动画（与分析面板同款：3 帧一步，见 `anim_frames`）。"""
        if not getattr(self.cfg.ui, "anim_enabled", True):
            self._ctx_anim = self._ctx_anim_target
        elif abs(self._ctx_anim - self._ctx_anim_target) >= 0.001:
            step = 1.0 / max(2, int(getattr(self.cfg.ui, "anim_frames", 6)))
            if self._ctx_anim_target > self._ctx_anim:
                self._ctx_anim = min(self._ctx_anim_target, self._ctx_anim + step)
            else:
                self._ctx_anim = max(self._ctx_anim_target, self._ctx_anim - step)
        # 第 71 轮修复（用户反馈"启动时没有 QQ 会话窗口，上下文面板也会自动显示"）：
        # 这里必须同时看**会话是否可用** —— 面板默认是"展开"状态（`_ctx_anim_target=1`），
        # 启动时还没有会话就被 show() 出来，而且因为 `window_rect` 为空、`_place_context_viewer`
        # 直接 return，它会停在默认坐标（屏幕左上角附近）不跟随任何人。
        if self._ctx_anim_target > 0.0 and self._chat_available:
            if not self.context_viewer.isVisible() and not self._hidden_by_foreground:
                self.context_viewer.show()
                self.context_viewer.raise_()
        elif self._ctx_anim <= 0.001 and self.context_viewer.isVisible():
            self.context_viewer.hide()                 # 收到 0 → 真正隐藏
        # 双保险：会话不可用时**绝不能让它留在屏幕上**（例如启动阶段 hwnd 还没拿到、
        # 用户把 QQ 最小化、或 UIA 读不到消息列表）。
        if not self._chat_available and self.context_viewer.isVisible():
            self.context_viewer.hide()
        if self.context_viewer.isVisible():
            # 内容刷新由 on_messages 里的 0.6s 限流路径负责（避免重复重排富文本）；
            # 这里只推位置（跟着 QQ 窗口走）。
            self._place_context_viewer()

    def _smooth_rect_legacy(self, key: str, target: QRect) -> QRect:
        """给面板位置加一点缓动，让滚动时的位移看起来连续。

        实测（`tmp/probe_scroll_pattern.py`）：滚动过程中 Chromium 给的无障碍矩形
        **不是每帧更新的**（大约 10 次/秒）→ 坐标是"台阶式"到来的，无论我们读得多勤，
        面板都只能一跳一跳。这里用 30ms 快 tick 把面板朝目标推进（每次走 55%），
        一个 50-100px 的台阶被摊成 2-3 帧的小步，肉眼就连续了。
        水平位移 / 大幅移动 / 切换目标时直接落位（不缓动），避免"拖泥带水"。
        """
        prev = self._smooth_prev.get(key)
        current = QRect(int(target.x()), int(target.y()), int(target.width()), int(target.height()))
        if prev is not None:
            dx, dy = current.x() - prev.x(), current.y() - prev.y()
            if abs(dx) <= 8 and 0 < abs(dy) <= 400:
                # 限速匀速：单帧最多走 22px（不足 8px 就一次落位）。
                # 实测原来按 55% 走，遇到 Chromium 一次 120px 的台阶时首帧仍要跳 66px；
                # 限速后同样的台阶被摊成 5-6 帧的小步，肉眼就是连续滑动。
                step = max(-22, min(22, int(dy * 0.55)))
                if abs(dy) - abs(step) < 8:
                    step = dy
                current = QRect(prev.x(), prev.y() + step,
                                current.width(), current.height())
        self._smooth_prev[key] = current
        return current

    def _trace(self, kind: str, panel_y: int, target_y: int) -> None:
        """跟随轨迹记录（只在 QQCA_TRACE_FOLLOW=1 时写 tmp/follow_trace.csv，用于实测）。"""
        if not getattr(self, "_trace_follow", False):
            return
        try:
            with open(self._trace_path, "a", encoding="utf-8") as handle:
                handle.write(f"{time.time():.3f},{kind},{panel_y},{target_y}\n")
        except Exception:
            pass

    def _place_windows(self) -> None:
        if self.window_rect is None:
            return
        if not getattr(self, "_chat_available", False):
            # 会话窗口不可用（没消息列表/最小化）→ 不摆浮窗；恢复时由
            # `_set_chat_available(True)` 重新显示并摆位。
            return
        # 拖动/移动 QQ 窗口 = 整个场景刚性平移：检测到窗口位置变了就进入"刚性跟随"窗口期，
        # 期间面板**不走缓动**（直接贴到目标位置），否则会"跟不上 → 落后 → 松手才追回来"
        # （用户反馈的"拖到聊天界面时面板到处漂移再回到目标位置"）。
        if getattr(self, "_last_placed_window_rect", None) != list(self.window_rect):
            self._last_placed_window_rect = list(self.window_rect)
            self._window_moving_until = time.time() + 0.25
        # 状态灯：物理坐标算好后统一换算成 Qt 逻辑坐标（否则 150% 缩放下会偏 1.5 倍）
        self._ensure_owner(self.dot)
        dot_phys = self._dot_physical_rect()
        # 第 68c 轮：状态灯**不要缓动** —— 用户实测"加了缓动之后快拖还是会闪现漂移"。
        # 状态灯的坐标本来就是刚性来源（缓存的消息列表矩形 + 窗口移动钩子给的实时窗口位移），
        # 直接落位即可；缓动只会让它落后一拍再追上去，看起来就是漂。
        dot_target = self._logical_rect(dot_phys)
        self.dot.setGeometry(dot_target)
        self._place_first_run_hint(dot_target)     # 第 79 轮：首次启动提示贴在状态灯下方
        # 注意：这里**不能无条件 show()** —— 它在 30ms 快 tick 里被调用，
        # 会把"QQ 不在前台就隐藏"的逻辑立刻覆盖掉（用户反馈"切到别的应用图标不隐藏"）。
        if not self._hidden_by_foreground:
            self.context_button.show()
        # 上下文历史小图标：贴在状态灯**左侧**
        size = self.cfg.ui.dot_size_px
        gap = 6
        # 用"已经缓动过的"状态灯位置来算图标位置，两者才始终贴在一起（否则缓动期间会错开几像素）
        scale = self.scale or 1.0
        dot_now = [int(dot_target.x() * scale), int(dot_target.y() * scale),
                   int(dot_target.right() * scale), int(dot_target.bottom() * scale)]
        btn_x = int(dot_now[0]) - gap - size
        screen_rect = w32.get_virtual_screen_rect()
        if btn_x < screen_rect[0]:
            btn_x = int(dot_now[2]) + gap           # 左边放不下就挪到右侧
        btn_phys = [btn_x, int(dot_now[1]), btn_x + size, int(dot_now[1]) + size]
        self.context_button.setGeometry(self._logical_rect(btn_phys))
        if getattr(self, "_last_ctx_btn_phys", None) != btn_phys:
            self._last_ctx_btn_phys = list(btn_phys)
            # 拖动窗口时这两行会每帧触发（实测刷出几百行/秒）→ 降到 DEBUG，
            # 需要位置的工具改读 logs/ui_state.json（见 `_write_ui_state`）。
            logger.debug("上下文历史图标位置（物理）=%s（状态灯 %s）", btn_phys, dot_now)
        self._write_ui_state(btn_phys, dot_now)     # 内部按 0.3s 限流
        self._last_context_button_phys = btn_phys
        # 上下文历史面板：贴 QQ 窗口**右侧**（第 66 轮），展开尺寸按右侧可用空间夹取
        self._place_context_viewer(screen_rect)
        if getattr(self, "_last_dot_rect", None) != dot_phys:
            self._last_dot_rect = list(dot_phys)
            logger.debug("状态灯位置（物理）=%s｜QQ 窗口=%s｜在窗口上沿外侧=%s",
                         dot_phys, list(self.window_rect), dot_phys[3] <= self.window_rect[1])
        if not self.panel.isVisible() or self.target is None:
            return
        screen = w32.get_virtual_screen_rect()
        messages = self._translated_messages()
        target = next((m for m in messages if m.msg_key == self.target.msg_key), self.target)
        # 用面板"希望"的尺寸（宽=按标题算出来的期望宽度），
        # 而不是 self.panel.width()：后者是上一次被空白带夹窄后的结果，
        # 直接拿来算会让面板越夹越窄（实测 320 → 140 逻辑后再也回不去）。
        scale = self.scale or 1.0
        want_w, want_h = self.panel.content_size()
        panel_size = (max(1, int(round(want_w * scale))), max(1, int(round(want_h * scale))))
        plan = plan_panel_placement(target, messages,
                                    self._translated_list_box(),
                                    self.window_rect, screen,
                                    panel_size, self.cfg, scale=scale)
        if getattr(self, "_last_strategy", None) != plan["strategy"]:
            self._last_strategy = plan["strategy"]
            logger.info("面板定位策略=%s rect=%s 宽=%d 空白带=%d(需≥%d) 遮挡=%d 条（目标：%s）",
                        plan["strategy"], plan["rect"], plan["rect"][2] - plan["rect"][0],
                        plan.get("available", -1), plan.get("min_band", -1),
                        len(plan["overlapped"]),
                        self._log_text(self.target.text, 18))
        # 面板用**逻辑坐标**就位（move_to_logical 会按实际宽度重新换行，避免显示不全；
        # 同时避免把物理宽度塞给 Qt 导致尺寸指数膨胀而闪退）
        logical_panel = self._logical_rect(plan["rect"])
        # 叠加滚轮预测位移（在平滑之前 → 缓动会把它 smoothly 追上，而不是硬跳）
        if self._predicted_dy:
            scale_now = self.scale or 1.0
            logical_panel.moveTop(logical_panel.y() + int(self._predicted_dy / scale_now))
        # 【已移除横向位置锁】它原本是为了"滚动时不要横跳"，但副作用是**拖动 QQ 窗口时
        # 面板不再左右跟随**（用户反馈的 bug）。既然滚动改为"直接收起"，这个锁已无必要 →
        # 每次都按当前窗口/消息位置重新计算 x，拖动窗口时就正常跟随了。
        logical_panel = self._smooth_rect("panel", logical_panel)
        # 拖动窗口时的"刚性跟随"：冻结面板相对窗口的偏移，之后只按窗口的位移量平移，
        # 不再每帧重算（重算会因定位策略切换/边界夹取导致"位移量对不上、位置被重置"，用户反馈）。
        if time.time() < getattr(self, "_window_moving_until", 0.0):
            scale_now = self.scale or 1.0
            if getattr(self, "_drag_anchor", None) is None:
                self._drag_anchor = (list(self.window_rect), QRect(logical_panel))
            else:
                anchor_window, anchor_panel = self._drag_anchor
                dxw = int((self.window_rect[0] - anchor_window[0]) / scale_now)
                dyw = int((self.window_rect[1] - anchor_window[1]) / scale_now)
                logical_panel = QRect(anchor_panel.x() + dxw, anchor_panel.y() + dyw,
                                      anchor_panel.width(), anchor_panel.height())
        else:
            self._drag_anchor = None
        self._panel_full_w = logical_panel.width()
        # 第 79 轮：面板的展开锚边 —— 贴在"我自己发的消息"左边时固定右缘向左展开
        self._panel_anchor = plan.get("anchor", "left")
        if getattr(self.cfg.ui, "anim_enabled", True) and self._panel_anim < 0.999:
            # 展开/收起中：窗口宽度按动画系数裁切（内容左对齐 → 看起来从消息那侧滑出）
            self.panel.set_anim_alpha(self._panel_anim)
            anim_w = max(4, int(logical_panel.width() * self._panel_anim))
            anim_x = (logical_panel.x() + logical_panel.width() - anim_w
                      if self._panel_anchor == "right" else logical_panel.x())
            self.panel.setGeometry(QRect(anim_x, logical_panel.y(),
                                         anim_w, logical_panel.height()))
        else:
            self.panel.set_anim_alpha(1.0)
            self.panel.move_to_logical(logical_panel)
        self._trace("place", self.panel.geometry().y(), int(logical_panel.y()))
        logical_panel = self.panel.geometry()
        self._ensure_owner(self.panel)
        if self.options.isVisible():
            # 选项条与面板**同一段动画**：透明度同步；位置仍按面板的最终几何（不跟着变窄，
            # 免得选项文字在动画中间被挤换行）
            # 选项条自己的展开/收起动画（与面板同款：宽度从 0 展开 + 渐显 + 上浮）。
            # 之前只有淡入淡出，用户反馈"还是没动画" → 现在连宽度一起动画。
            # 时间基准推进（用户反馈"选项条收起太快"）：按真实经过时间算进度，
            # 不再按"本函数被调用的次数"——它被快 tick、几何 tick、跟随回调同时调用，
            # 按次数推进会导致实际时长只有预期的 1/3。
            now_ts = time.time()
            # dt 上限 50ms：填入操作会阻塞主线程 ~0.5-1s，若不夹住，恢复后第一帧会算出
            # 1 秒的 dt → 动画直接跳到终点（用户反馈"选完选项后动画直接没了"）。
            dt = min(0.05, max(0.001, now_ts - getattr(self, "_options_anim_ts", now_ts)))
            self._options_anim_ts = now_ts
            duration = max(0.05, getattr(self.cfg.ui, "anim_ms_options", 320) / 1000.0)
            step = dt / duration
            if self._options_anim_target > self._options_anim:
                self._options_anim = min(self._options_anim_target, self._options_anim + step)
            else:
                self._options_anim = max(self._options_anim_target, self._options_anim - step)
            if self._options_anim <= 0.02:
                self.options.hide()             # 收完（选完回复后）才真正隐藏
                return
            self.options.set_anim_alpha(self._panel_anim * self._options_anim)
            # 选项条也用逻辑坐标（它在 Qt 里定位，直接把物理 rect 传进去会偏 1.5 倍）
            logical_screen = [int(v / scale) for v in screen]
            self.options.set_width(logical_panel.width())    # 选项条与面板同宽（长文本不再被截）
            self._smooth_rect("options", QRect(logical_panel.left(), logical_panel.top(),
                                               logical_panel.right(), logical_panel.bottom()))
            rise = int(12 * (1.0 - self._options_anim))     # 入场时从下方 12px 浮上来
            self.options.move_to([logical_panel.left(), logical_panel.top() + rise,
                                  logical_panel.right(), logical_panel.bottom() + rise],
                                 logical_screen)
            if self._options_anim < 0.999:                  # 宽度也跟着展开（和面板一个观感）
                box = self.options.geometry()
                opt_w = max(4, int(box.width() * self._options_anim))
                # 第 79 轮：选项条跟分析面板用**同一条锚边** —— 面板贴在自己气泡左边
                # （anchor=right）时，选项条也固定右缘向左展开/收回。
                opt_x = (box.x() + box.width() - opt_w
                         if getattr(self, "_panel_anchor", "left") == "right" else box.x())
                self.options.setGeometry(QRect(opt_x, box.y(), opt_w, box.height()))
            self._ensure_owner(self.options)

    def _ensure_owner(self, widget) -> None:
        """窗口 show()/重建后 owner 会丢，导致浮窗被 QQ 盖住（实测"面板不见了"就是这个）。

        每次定位都校验一次，丢了就补回 QQ 句柄 —— 这样浮窗永远在 QQ 之上、
        但不会盖住其他应用（owned window 的语义）。
        """
        if not self.cfg.ui.use_owner_window or not self._owner_hwnd:
            return
        try:
            hwnd = int(widget.winId())
            if w32.get_window_owner(hwnd) != self._owner_hwnd:
                w32.set_owner_window(hwnd, self._owner_hwnd)
                logger.debug("补回 owner：%s -> %s", hwnd, self._owner_hwnd)
        except Exception:
            pass

    def _qq_recently_active(self, window_s: float = 8.0) -> bool:
        """QQ 最近是否真的在前台过 —— 避免"点选项时浮窗抢到前台"被误判成 QQ 还在用。"""
        return (time.time() - getattr(self, "_last_foreground_seen", 0.0)) < window_s

    def _sync_topmost(self) -> None:
        """只在 QQ 在前台时把浮窗设为 topmost。

        这样：QQ 在前台 → 浮窗压在 QQ 之上；切到别的应用 → 浮窗自动让到后面（不挡人）。
        比 owner 方案稳（owner 会反复改 QQ 的窗口关系，实测会导致 QQ 窗口漂移）。
        """
        if not self._owner_hwnd:
            return
        self._last_foreground_seen = getattr(self, "_last_foreground_seen", 0.0)
        if w32.get_foreground_window() == self._owner_hwnd:
            self._last_foreground_seen = time.time()
        now = time.time()
        if now - getattr(self, "_last_topmost_sync", 0.0) < self.cfg.ui.topmost_sync_ms / 1000.0:
            return
        self._last_topmost_sync = now
        try:
            fg = w32.get_foreground_window()
            # "QQ 在前台"或"鼠标正在用我们的浮窗（例如点选项）"才置顶：
            # 切到别的应用时浮窗一定会让到后面（用户反馈过"选项窗跑到上层应用上方"）。
            ours = {int(self.dot.winId()), int(self.panel.winId()), int(self.options.winId()),
                    int(self.context_button.winId()), int(self.context_viewer.winId()),
                    int(self.hint.winId())}      # 第 79 轮：首次启动提示也算"我们的窗口"
            active = fg == self._owner_hwnd or (fg in ours and self._qq_recently_active())
            for widget in (self.dot, self.panel if self.panel.isVisible() else None,
                           self.options if self.options.isVisible() else None,
                           self.context_button if self.context_button.isVisible() else None,
                           self.context_viewer if self.context_viewer.isVisible() else None,
                           self.hint if self.hint.isVisible() else None):
                if widget is None:
                    continue
                hwnd = int(widget.winId())
                if active and not w32.is_topmost(hwnd):
                    w32.set_topmost(hwnd, True)
                elif not active and w32.is_topmost(hwnd):
                    w32.set_topmost(hwnd, False)
        except Exception as exc:
            logger.debug("topmost 同步失败：%s", exc)

    def _sync_visibility_by_foreground(self) -> None:
        """QQ 不在前台时把面板/选项收起来 —— 确保浮窗不会压在别的应用上面。

        （用户明确要求"别挡着其他应用"；状态灯保留，非置顶时它自然会被别的窗口盖住。）
        """
        if not self._owner_hwnd or not self.cfg.ui.hide_panel_when_qq_not_foreground:
            return
        fg = w32.get_foreground_window()
        ours = {int(self.dot.winId()), int(self.panel.winId()), int(self.options.winId())}
        active = fg == self._owner_hwnd or fg in ours or self.settings_dialog is not None
        if not active:
            if not self._hidden_by_foreground:
                self._hidden_by_foreground = True
                self._hide_overlays(reason="QQ 不在前台")
                # 查看面板也一起收（一致的可见性规则）：第 66 轮起走动画收起，
                # 但**不动** `_ctx_user_open` —— 回到 QQ 前台时按用户意愿恢复。
                self._ctx_anim = 0.0
                self._ctx_anim_target = 0.0
                self.context_viewer.hide()
                if self.cfg.ui.hide_dot_when_qq_not_foreground:
                    self.dot.hide()
                    self.context_button.hide()
        elif self._hidden_by_foreground:
            self._hidden_by_foreground = False
            # QQ 回到前台：**立刻**恢复，别等下一个轮询周期（后台是 2000ms，会让状态灯慢半拍）。
            # 同时清掉可能残留的"安静期"（切会话时暂停过读取）。
            self.capture.set_paused(False)
            self.capture.request_now()
            self.dot.show()                  # 回到 QQ 前台：状态灯一起回来
            self.context_button.show()
            if self.target is not None:          # 回到 QQ 就恢复
                self._render_target()
            # 上下文历史面板：按用户意愿恢复（默认是开着的）
            if self._ctx_user_open:
                self._ctx_anim = 0.0
                self._ctx_anim_target = 1.0

    # ---------------- 推理回调 ----------------
    def on_model_loaded(self, ok: bool, status: Dict[str, Any]) -> None:
        self._model_ready = True                     # 进度日志到此为止（见 on_geometry_tick）
        waited = time.time() - getattr(self, "_load_started_ts", time.time())
        if waited > 8:
            logger.info("模型加载总耗时 %.0f 秒（首次用 GPU 会慢，第二次启动会快很多）", waited)
        if ok:
            logger.info("模型就绪：%s，%ss，GPU 层=%s",
                        status.get("model"), status.get("load_s"), status.get("gpu_layers"))
            self.dot.set_status("ready")
            self.toast.show_toast("模型已就绪", f"{status.get('model')} · {status.get('load_s')}s",
                                  near=self._dot_physical_rect() if self.window_rect else None)
        else:
            logger.error("模型加载失败：%s", status.get("last_error"))
            self.dot.set_status("error")
            self.toast.show_toast("模型加载失败", str(status.get("last_error") or "")[:60],
                                  duration_ms=4000)
            self.panel.show_notice("模型加载失败", str(status.get("last_error") or "请检查 models/ 目录"))
            self.panel.setGeometry(self._logical_rect(
                [self.window_rect[0] + 40 if self.window_rect else 200,
                 self.window_rect[1] + 80 if self.window_rect else 200,
                 (self.window_rect[0] + 400) if self.window_rect else 560,
                 (self.window_rect[1] + 200) if self.window_rect else 320]))
            if not self._hidden_by_foreground:
                self._reveal_panel()

    def on_analysis_started(self, message: Message) -> None:
        self._analyzing = True
        self.dot.set_status("analyzing")

    def on_analysis_ready(self, result: AnalysisResult) -> None:
        self._analyzing = False
        # 手动刷新用的一次性参数（新种子/升温）**不在这里清**：两段式的第一段结果
        # `partial=False` 也会走到这里，一清第二段就退回 0.2 温度（第 80 轮修的 bug）。
        # 现在由工作线程在"整条请求跑完"后还原 —— 见 analyzer.refresh_snapshot/refresh_release。
        self.results[result.msg_key] = result
        if len(self.results) > 200:                      # 长期运行别让 UI 侧结果字典无限增长
            for stale_key in list(self.results)[:len(self.results) - 200]:
                self.results.pop(stale_key, None)
        self.pending_key = None
        logger.info("分析%s #%s：intent=%s danger=%s 建议=%d字 选项=%d %sms%s",
                    "（第一段）" if result.partial else "完成",
                    result.msg_key,
                    (result.intent if getattr(self.cfg, "log_message_text", True)
                     else f"<{len(result.intent or '')} 字，已脱敏>"),
                    result.danger_level,
                    len(result.suggestion or ""), len(result.replies),
                    result.total_ms, "（降级）" if result.degraded else "")
        # 只有"当前目标"才上屏（过期结果直接丢弃，SPEC 4.1）
        if self.target is not None and result.msg_key == self.target.msg_key:
            self._show_result(result)
            self._place_windows()
        self.dot.set_status("analyzing" if result.partial else "ready")
        # API 返回不可用时给用户明确提示（否则面板只会显示"— /（无建议）"，看不出原因）
        if not result.partial and any("API 返回内容不合格" in n for n in (result.notes or [])):
            reason = next((n for n in result.notes if "API 返回内容不合格" in n), "API 返回异常")
            logger.warning("面板提示：%s", reason)
            self.toast.show_toast("API 返回异常", reason[:60],
                                  near=self._dot_physical_rect() if self.window_rect else None,
                                  duration_ms=5000)
        if not result.partial and self.cfg.ui.trigger_mode != "click":
            # 最新那条分析完成后，再看鼠标是不是指着别的消息 → 是就切过去分析
            self._update_target(auto=not self.cfg.analyzer.prioritize_latest)

    def on_analysis_partial(self, result: AnalysisResult) -> None:
        """流式提前上屏：标题/危险度一生成完就先画出来，不等整段 JSON。

        实测：第一段要 2.1s，但情绪·意图在 ~0.5s 就闭合 → 体感快约 4 倍。
        这里**不写缓存、不动 pending 标记、不改状态灯语义**（完整结果仍由
        on_analysis_ready 走一遍，字段更全时覆盖显示）。过期目标直接丢弃。
        """
        if self.target is None or result.msg_key != self.target.msg_key:
            return
        if self.pending_key is not None and self.pending_key != result.msg_key:
            return
        if not (result.intent or result.emotion):
            return
        self._show_result(result)
        self._place_windows()
        logger.info("流式上屏 #%s：%s · %s（危险度 %s）",
                    result.msg_key, result.emotion or "—", result.intent or "—",
                    result.danger_level if result.danger_level is not None else "—")

    def on_analysis_cancelled(self, msg_key: str) -> None:
        """抢占取消：清掉 pending 标记，否则回到这条消息时会被"已在分析中"挡住不再生成。"""
        if self.pending_key == msg_key:
            self.pending_key = None
            self._stall_notified = True          # 已取消 → 看门狗不用再提示
            logger.info("已取消 #%s（pending 标记已清理，回到这条会重新生成）", msg_key)
        elif self.pending_key is None and self.target is not None:
            pass

    # ---------------- 定时器 ----------------
    def on_mouse_tick(self) -> None:
        if self.messages and self.window_rect:
            hovered = self._hovered_message()
            # 轮询频率：鼠标在 QQ 窗口范围内 → 30ms（悬停切换才跟得上）；
            # 悬停在某条消息上 → 30ms；否则 200ms。
            # 实测：WinEvent 的光标事件在本机收不到（803 个事件里 0 个是光标），
            # 所以不能依赖事件，必须靠这个高频轮询。
            cursor = w32.get_cursor_pos()
            near_window = coord.point_in_rect(cursor, self.window_rect, 24)
            # 空闲时把整树读取间隔放宽：每次 UIA 整树读取都是在 QQ 进程里跑一遍，
            # 读太勤会让 QQ 自己变卡（用户反馈"切换对话窗口明显卡顿"）。
            interval = self.NEAR_POLL_MS if hovered else (
                300 if near_window else self.IDLE_POLL_MS)
            logger.debug("hover: cursor=%s near=%s hovered=%s target=%s msgs=%d interval=%d",
                         cursor, near_window,
                         None if hovered is None else (self._log_text(hovered.text, 10),
                                                       hovered.side),
                         None if self.target is None else self._log_text(self.target.text, 10),
                         len(self.messages), interval)
            if self.mouse_timer.interval() != interval:
                self.mouse_timer.setInterval(interval)
            # "我们自己的浮窗在前台"也要算作前台（否则打开上下文面板/点选项时，
            # 采集线程会退到 2000ms 轮询 → 用户反馈"开着面板就不再实时记录上下文"）
            foreground_now = w32.get_foreground_window()
            ours_now = {int(self.dot.winId()), int(self.panel.winId()), int(self.options.winId()),
                        int(self.context_button.winId()), int(self.context_viewer.winId())}
            self.capture.set_context(foreground=(foreground_now ==
                                                 (self.snapshot.get("window") or {}).get("hwnd")
                                                 or foreground_now in ours_now),
                                     hovering=bool(hovered))
            # 第 69 轮：读取节奏分两档 —— "正在用"（面板开着 / 刚点过 / 刚滚过）300ms，
            # 其它时候 700ms。整树 UIA 读取每次要 QQ 渲染进程烧 ~130ms CPU，
            # 之前一直 120ms 一次 → 我们 83% + QQ 43.8% 单核，风扇自然一直转。
            self.capture.set_fast(self._capture_is_fast())
            if hovered is not None and (self.target is None
                                        or hovered.msg_key != self.target.msg_key):
                if self.cfg.ui.trigger_mode == "hover":
                    self._update_target(auto=False)
                else:
                    # 点击触发模式：鼠标移动不切换目标，只跟着重算位置（省掉无意义的分析）
                    self._place_windows()
            elif hovered is not None:
                # 目标没变：仍然重算一次位置（滚动时目标消息会移动，面板要立刻跟上）
                self._place_windows()

    def _capture_is_fast(self) -> bool:
        """是否处于"正在用"状态 → 整树读取用快间隔（见 `CaptureWorker._interval_s`）。

        判据：**最近 5s 内有点击/滚轮/换目标**。注意"面板一直开着"**不算**在用 ——
        面板的丝滑跟随由 `follow_reader` 负责（单元素矩形刷新，1-3ms 一次，很便宜），
        而整树读取每次要让 QQ 渲染进程烧 ~100-200ms（实测 A/B：我们开着 29.4% →
        关掉 1.2%）。之前把"面板开着"当成快档条件，结果点一次消息后面板一直开着，
        整树读取就永远跑在 300ms 档 → 用户看到"没在分析时风扇也一直转"。
        慢档只影响"点下去那一刻用的是最多 1.2s 前的消息矩形"，而点击自带 ±24px 容差、
        滚动/换会话都有事件通道会立刻收起浮窗 + 提速，所以体验上没有区别。
        """
        return (time.time() - getattr(self, "_last_activity_ts", 0.0)) < 5.0

    def on_geometry_tick(self) -> None:
        if self.window_rect is None:
            return
        # 第 75 轮：分析超过 45s 还没结果 → 面板给一句明确提示（不再无声"分析中"）。
        # 实测 Qwen3.5(qwen35 混合 SSM) + 老版 llama.cpp 出现过"一个 token 都不吐"的真卡死，
        # 这种情况后端线程会一直阻塞，界面只能自己给结论：让用户知道可以重启。
        if (self.pending_key is not None and not getattr(self, "_stall_notified", True)
                and time.time() - getattr(self, "_pending_since", 0.0) > 45.0):
            self._stall_notified = True
            waited = time.time() - getattr(self, "_pending_since", 0.0)
            logger.warning("分析已等待 %.0fs 仍无结果（可能是本地模型卡住）", waited)
            if self.panel.isVisible():
                self.panel.show_notice("分析超时",
                                       f"已等待 {waited:.0f} 秒没有结果",
                                       hint="本地模型可能卡住了：可在托盘里“暂停分析”再恢复，"
                                            "或重启应用；也可以先在设置里换回 Qwen3-4B")
                self._place_windows()
        # 模型还没就绪时每 15s 写一行进度：首次 GPU 加载慢是正常的，别让人以为卡死
        if not getattr(self, "_model_ready", True):
            now_ts = time.time()
            waited = now_ts - getattr(self, "_load_started_ts", now_ts)
            if waited > 8 and now_ts - getattr(self, "_load_log_ts", 0.0) > 15.0:
                self._load_log_ts = now_ts
                logger.info("模型仍在加载中…已等待 %.0f 秒（首次用 GPU 加载会编译显卡内核，"
                            "30-120 秒属正常，请勿关闭；加载完状态灯会变绿）", waited)
        self._sync_topmost()
        self._sync_visibility_by_foreground()
        # 自愈：本该显示却没有显示的面板，重新显示（历史上出现过"分析完面板不见了"）
        if (not self.paused and not self._hidden_by_foreground and self.target is not None
                and not self.panel.isVisible() and self.panel.has_content
                and self._status_streak == 0
                and time.time() - getattr(self, "_intentional_hide_ts", 0.0) > 1.0):
            logger.info("面板自愈：重新显示（目标 %s）", self._log_text(self.target.text, 16))
            if not self._hidden_by_foreground:
                self._reveal_panel()
            if self.results.get(self.target.msg_key):
                self._show_result(self.results[self.target.msg_key])
        self._place_windows()

    def on_fast_tick(self) -> None:
        """30ms 级跟随：只刷新缓存元素的坐标，不重读整棵无障碍树。

        这是"面板跟着消息走"能做到丝滑的关键 —— 整树读取 ~80ms，而单元素矩形刷新
        只要 1-3ms，30ms 一次完全无压力。
        """
        self._poll_click()          # 点击触发模式：先受理点击（不受下面早退条件影响）
        self._step_panel_anim()     # 展开/收起动画（独立于跟随逻辑，收起时也要继续跑）
        self._step_ctx_anim()       # 上下文历史面板：默认展开 + 同款动画 + 跟随 QQ 窗口
        # 前台变化要"立刻收起"：原来只在 50ms 几何 tick 里检查，切到别的应用时会有明显延迟。
        # 这里只做一次 GetForegroundWindow + 很小的集合判断，30ms 一次开销可忽略。
        self._sync_visibility_by_foreground()
        if self.paused or self.target is None or not self.panel.isVisible():
            self.follow_reader.set_target(None)
            self.motion_tracker.set_area(None)
            return
        # 跟随读取交给后台线程（滚动时单次 UIA 读取可能阻塞 300-400ms，放主线程会冻住界面）；
        # 主线程这里只按"已知坐标 + 缓动"推进位置，30ms 一帧，视觉连续。
        self.follow_reader.set_target(self.target.msg_key)
        self.motion_tracker.set_area(self._motion_area())
        self._place_windows()

    def on_wheel_event(self, delta: int, x: int, y: int) -> None:
        self._last_activity_ts = time.time()        # 刚滚过 → 读取提速 2.5s（见 _capture_is_fast）
        """滚轮预测跟随：滚动发生时立刻按估算位移推面板，真实坐标到了再纠正。

        事件来源是 **Raw Input（INPUTSINK）**：系统投给我们的**事件副本**，
        我们不在输入投递链路上 → 即使这里处理慢了，也绝不会卡住系统鼠标（见 ui/wheel_sink.py）。

        只认"鼠标位于消息列表区域内"的滚轮（用户提醒）：输入框/侧边栏里滚轮不改变消息位置，
        那种情况推面板反而会错位。delta>0 = 向上滚 = 内容下移（消息 y 增大）。
        """
        # 用户口径（第 39 轮）：**聊天区一滚动就直接收起浮窗**（不再做滚动跟随）。
        # 放在 wheel_predict 判断之前，所以预测开关关掉也照样生效。
        if getattr(self.cfg.ui, "collapse_on_scroll", True):
            list_box_now = self._translated_list_box()
            in_list = bool(list_box_now) and (list_box_now[0] <= x <= list_box_now[2]
                                              and list_box_now[1] <= y <= list_box_now[3])
            if in_list and self.panel.isVisible():
                logger.info("检测到聊天区滚动 → 收起浮窗（目标：%s）",
                            None if self.target is None else self._log_text(self.target.text, 14))
                self._hide_overlays("滚轮滚动")
                self.target = None          # 清目标，避免 1s 后被"自愈"又弹回来
                self._predicted_dy = 0
                return
        if not getattr(self.cfg.ui, "wheel_predict", True):
            return
        if self.target is None or self.paused or not self.panel.isVisible():
            return
        list_box = self._translated_list_box()
        if not list_box:
            return
        if not (list_box[0] <= x <= list_box[2] and list_box[1] <= y <= list_box[3]):
            return
        base = int(getattr(self.cfg.ui, "wheel_step_px", 187))
        step = self._wheel_step_runtime if delta > 0 else -self._wheel_step_runtime
        # 不再直接改消息 bbox（那会和 UIA 校准互相拉 → 面板一闪一闪），
        # 改成累计"预测位移"，定位时叠加，并让它自然衰减回 0。
        self._predicted_dy += step
        self._wheel_notches += 1
        self._last_wheel_ts = time.time()
        # 用户方案（第 37 轮）：**滚动期间不做 UIA 纠正**，等滚完再一次性对账。
        # 之前两个源同时改位置 → 预测推一半被 UIA 拉回来，就是"回拉"的根因。
        self._scroll_burst_until = time.time() + 0.25
        self._last_motion_ts = time.time()      # 进入"追赶模式"，让缓动大步跟上
        self._place_windows()

    def _motion_area(self):
        """实时跟随的抓取区域：消息列表**左侧窄带**（避开我们自己的浮窗）。

        只在"面板可见 + QQ 在前台"时采集；否则返回 None 让采集线程歇着（不空转、不占 CPU）。
        ⚠ 目前**默认关闭**（`ui.scroll_motion_predict=False`）：位移估计器的单元自检还没过
        （平坦背景会给出假位移），错误位移会让面板乱跳，比"停下后平滑跟随"更糟。
        等自检全绿再打开。
        """
        if not getattr(self.cfg.ui, "scroll_motion_predict", False):
            return None
        if (self.target is None or self.paused or self._hidden_by_foreground
                or not self.panel.isVisible()):
            return None
        list_box = self._translated_list_box()
        if not list_box:
            return None
        left = int(list_box[0]) + 6
        right = left + 200
        top = int(list_box[1]) + 6
        bottom = int(list_box[3]) - 6
        if bottom - top < 80 or right - left < 40:
            return None
        return (left, top, right, bottom)

    def on_scroll_motion(self, dy: int) -> None:
        """帧间互相关给出的内容位移：整列平移消息 + 重排浮窗（主线程执行）。

        这里**不碰 UIA**（纯几何平移），真实坐标由 FollowReader 定期校准，
        所以不会有累积误差——平移只负责"滚动过程中的实时观感"。
        """
        if dy == 0 or self.target is None or self.paused or self._hidden_by_foreground:
            return
        if not self.panel.isVisible():
            return
        if time.time() - getattr(self, "_last_wheel_ts", 0.0) < 0.25:
            return          # 滚轮预测刚生效：让别的位移源等一拍，避免互相拉出闪烁
        dy = max(-500, min(500, int(dy)))
        self._last_motion_ts = time.time()
        for message in self.messages:
            message.bbox = (message.bbox[0], message.bbox[1] + dy,
                            message.bbox[2], message.bbox[3] + dy)
        self._place_windows()

    def on_wheel_event_legacy_doc(self) -> None:
        """滚轮预测跟随（主线程）——原始说明留档。

        Chromium 在快速连续滚动时**不更新无障碍矩形**（实测），所以"先读坐标再动"永远慢半拍；
        这里拿到滚轮事件就按"一格≈187px"先把面板推过去，真实坐标到手后再由跟随线程纠正。

        注意（用户提醒）：只有鼠标位于**消息列表区域**内时滚轮才会滚动聊天内容 ——
        在输入框/侧边栏滚动不改变消息位置，此时推面板反而会错位，所以必须校验落点。
        delta>0 = 向上滚 = 内容下移（消息 y 增大）。
        """
        if not getattr(self.cfg.ui, "wheel_predict", True):
            return
        if self.target is None or self.paused or not self.panel.isVisible():
            return
        list_box = self._translated_list_box()
        if not list_box:
            return
        if not (list_box[0] <= x <= list_box[2] and list_box[1] <= y <= list_box[3]):
            return                        # 不在消息列表里（输入框/侧边栏/别处）→ 忽略
        base = int(getattr(self.cfg.ui, "wheel_step_px", 187))
        step = self._wheel_step_runtime if delta > 0 else -self._wheel_step_runtime
        self._predicted_dy += step
        self._wheel_notches += 1
        self._last_wheel_ts = time.time()
        self._last_motion_ts = time.time()
        self._place_windows()

    def on_follow_rect(self, msg_key: str, rect) -> None:
        """后台跟随线程回报的锚点坐标：平移整列消息并重排浮窗（主线程执行）。"""
        """后台跟随线程回报的锚点坐标：平移整列消息并重排浮窗（主线程执行）。"""
        if rect is None or self.target is None or not self.panel.isVisible():
            return
        if time.time() < getattr(self, "_scroll_burst_until", 0.0):
            return          # 滚动进行中：不做 UIA 纠正，避免与预测互相拉（用户方案）
        if self.paused or self._hidden_by_foreground:
            return
        anchor = (self._original_message(msg_key, self._translated_bbox_of(self.target))
                  if self.target is not None else None) or self.target
        try:
            dx = int(rect[0]) - int(anchor.bbox[0])
            dy = int(rect[1]) - int(anchor.bbox[1])
            # 只信纵向位移：滚动只会让消息上下移动。横向差异来自"锚点换成了另一条消息"
            # （左右两列气泡左边缘本来就不一样），照抄会把整列消息连带面板横推走
            # —— 实测表现就是"自动收起前面板莫名往左跳一段"。这里只允许 ±2px 微校正。
            dx = max(-2, min(2, dx))
        except Exception:
            return
        if not dx and not dy:
            return
        # 真实位移到手 → 先"吃掉"等量的预测位移（两者描述同一次滚动），
        # 这样就不会出现"预测过冲 → 被拉回来"的上下弹动。同时顺手标定"一格滚多少像素"。
        if dy and self._wheel_notches > 0:
            measured = abs(dy) / self._wheel_notches
            if 20 <= measured <= 600:
                self._wheel_step_runtime = int(0.7 * self._wheel_step_runtime + 0.3 * measured)
                logger.debug("滚轮步长标定：实测 %.0fpx/格 → 采用 %dpx/格",
                             measured, self._wheel_step_runtime)
            self._wheel_notches = 0
        # **每次都吃掉等量预测**（不管刚滚过与否）：真实坐标一到位，预测就该退场，
        # 否则两边各占一部分位置，表现为"到了目标又上下弹一下"。
        # 滚完后的第一次纠正：真值已包含整段滚动 → 预测清零，并与 bbox 位移同帧完成，
        # 这样面板只会朝一个方向收敛，不会再被拉回原位。
        if dy:
            self._predicted_dy = 0
        self._last_motion_ts = time.time()      # UIA 校准也属于"正在跟随"
        for message in self.messages:          # 同屏整列同步位移
            message.bbox = (message.bbox[0] + dx, message.bbox[1] + dy,
                            message.bbox[2] + dx, message.bbox[3] + dy)
        self._place_windows()

    def _poll_click(self) -> None:
        """点击触发：鼠标移到某条消息上**点一下**才开始分析，再点同一条收起浮窗。

        - 只在 QQ 是前台时受理；命中测试自带"这个点确实属于 QQ 窗口"的校验，
          所以点我们自己的选项条/面板不会被当成点消息；
        - 只读按键状态（GetAsyncKeyState），不注入、不拦截任何输入。
        """
        if getattr(self.cfg.ui, "trigger_mode", "hover") != "click":
            return
        down = w32.is_key_down(w32.VK_LBUTTON)
        rising = down and not self._lbtn_down
        self._lbtn_down = down
        if not rising or self.paused or self.window_rect is None:
            return
        # 轮询是**主路径**（保证一定能点开）；Raw Input 若也上报了同一击，按时间去重，
        # 避免"先开又收"。教训：上一轮我把轮询关掉、只信 Raw Input，
        # 结果 Raw Input 的点击没送达 → 面板完全点不出来。
        #
        # 第 79 轮修 bug（用户："点击选项/气泡偶尔刚要展开马上又收起，多点几下才好"）：
        # 去重以前**只做了单向**（轮询看 _last_raw_click），于是"轮询先处理、Raw Input
        # 6ms 后到"这一支就能把同一击再处理一遍 → `_toggle_target` 看到"同一条且面板可见"
        # 就当成"再点一次收起" → 展开后立刻收起。现在两条路径共用一个"已处理过"时间戳。
        if self._click_recently_handled():
            return
        self._last_click_handled = time.time()
        # 注意：**不能要求"QQ 已经是前台"** —— 用户从别的应用点进 QQ 时，点击会先被我们
        # 这一帧看到，而"变成前台"还没完成，那一击就被丢掉了（表现为"第一次点没反应"）。
        # 改成只看"这个点是不是落在 QQ 窗口上"（_hovered_message 内部已用 WindowFromPoint 校验），
        # 点我们自己的选项条/面板不会被当成点消息。
        self._last_activity_ts = time.time()      # 刚点过 → 读取提速 2.5s（见 _capture_is_fast）
        # 命中测试用的矩形可能最多 0.7s 旧（慢档间隔）→ 顺手要一次加急读取，
        # 让下一次轮询立刻拿到新鲜坐标（不影响这一击的处理，只保证后续跟随是新的）。
        self.capture.request_now()
        hit = self._hovered_message(padding=self.cfg.ui.click_padding_px)
        if hit is None:
            # 第 79 轮续：两条点击通道（轮询 / Raw Input）共用同一套"没命中怎么办"，
            # 否则"哪条先到"会决定这一击是立刻收起、还是被挂起等新坐标。
            self._on_click_miss()
            return
        target = self._original_message(hit.msg_key, hit.bbox) or hit
        if (self.panel.isVisible() and self.target is not None
                and self.target.msg_key == target.msg_key):
            # 用户口径（第 33 轮）：选完回复后点同一条消息就是**收起整个浮窗**，
            # 不要再帮我"重新展开选项条"（那是上一轮为解决"展不开"加的，已经过时）。
            logger.info("点击同一条 → 收起浮窗：%s", self._log_text(target.text, 16))
            self._hide_overlays("再次点击收起")
            self.target = None
            return
        logger.info("点击消息 → 开始分析：[%s] %s",
                    "对方" if target.is_other_party else "我", self._log_text(target.text, 16))
        self.target = target
        self._render_target()

    def on_raw_click(self, x: int, y: int) -> None:
        """Raw Input 上报的左键按下：按"点击触发"逻辑选中/收起面板。

        比轮询可靠：Raw Input 每个按键事件都会投递，极短的点击也不会漏
        （用户反馈"极少数情况下点气泡没反应"就是轮询 30ms 漏掉了短按）。
        """
        if getattr(self.cfg.ui, "trigger_mode", "click") != "click":
            return
        # 同一次物理点击只处理一次（轮询与 Raw Input 都会看到它，见 _poll_click 注释）
        if self._click_recently_handled():
            return
        self._last_click_handled = time.time()
        self._last_raw_click = time.time()
        logger.debug("Raw Input 左键 @(%d,%d)", x, y)
        self._toggle_target(self.cfg.ui.click_padding_px)

    def _click_recently_handled(self) -> bool:
        """同一次物理点击是否已经被处理过（轮询 + Raw Input 双通道去重）。

        窗口取 0.18s：两条通道对同一击的到达间隔通常是几毫秒（实测最长 ~6ms），
        0.18s 足够挡住重复投递；再大就会把用户"点了没反应→马上再点一下"误吞掉
        （第 79 轮：用户反馈"要点一两下才行"，一部分就是这里的去重窗口太大）。
        """
        return time.time() - getattr(self, "_last_click_handled", 0.0) < 0.18

    def _toggle_target(self, padding: int) -> None:
        """点一下：命中消息就（打开分析 / 再点同一条收起）；点在会话列表则进安静期。"""
        if self.paused or self.window_rect is None:
            return
        # 第 79 轮：切换会话后的"安静期"里，用户已经在点气泡了 → 提前恢复读取并要求一次加急读。
        # 不这么做的话，这段时间读不到新坐标，前几下点击会全部落空（用户反馈的现象）。
        if time.time() < getattr(self, "_switch_quiet_until", 0.0):
            self._switch_quiet_until = 0.0
            self.capture.set_paused(False)
            self.capture.request_now()
            logger.info("切换会话后的安静期内用户点击 → 提前恢复读取")
        # 面板/选项条是**鼠标穿透**窗口（点击会穿到 QQ 上），系统不会把它们报成"我们被点了"
        # → 必须用几何判断排除：否则"点面板"会被当成"点空白"直接收起（用户反馈的 bug）。
        if self._point_in_overlays(*w32.get_cursor_pos()):
            # 第 79 轮：点到"首次启动提示"就是用户想关掉它（不依赖 Qt 是否送达鼠标事件，
            # 因为那个窗口是 WindowDoesNotAcceptFocus；这里给了第二条兜底路径）。
            cursor = w32.get_cursor_pos()
            if self.hint.isVisible() and self._point_in_widget(self.hint, *cursor):
                self._dismiss_first_run_hint()
            return
        hit = self._hovered_message(padding=padding, wide=True)
        if hit is None:
            self._on_click_miss()
            return
        target = self._original_message(hit.msg_key, hit.bbox) or hit
        if (self.panel.isVisible() and self.target is not None
                and self.target.msg_key == target.msg_key):
            logger.info("点击同一条 → 收起浮窗：%s", self._log_text(target.text, 16))
            self._hide_overlays("再次点击收起")
            self.target = None
            return
        logger.info("点击消息 → 开始分析：[%s] %s",
                    "对方" if target.is_other_party else "我", self._log_text(target.text, 16))
        self.target = target
        self._render_target()

    def _finish_empty_click(self, x: int, y: int) -> None:
        """重判后确认"没点在消息上"：点空白处收起浮窗；侧边栏那一击进切会话安静期。"""
        if self.panel.isVisible():
            logger.info("点击空白处 → 收起浮窗")
            self._hide_overlays("点击空白处")
            self.target = None
        self._maybe_quiet_on_sidebar_click()

    def _retry_pending_click(self) -> None:
        """有"等新坐标重判"的点击时，用刚到手的新消息再判一次（on_messages 里调）。"""
        pending = getattr(self, "_pending_click", None)
        if not pending:
            return
        window = float(pending.get("window", self.PENDING_CLICK_HIDDEN_S))
        if time.time() - pending["t"] > window:
            self._pending_click = None
            logger.info("挂起的点击重判超时（%.0fms）→ 按“点空白”处理",
                        (time.time() - pending["t"]) * 1000)
            self._finish_empty_click(pending["x"], pending["y"])
            return
        hit = self._hovered_message(padding=self.cfg.ui.click_padding_px, wide=True)
        if hit is None:
            return                                  # 再等下一次读取
        self._pending_click = None
        target = self._original_message(hit.msg_key, hit.bbox) or hit
        if (self.panel.isVisible() and self.target is not None
                and self.target.msg_key == target.msg_key):
            return                                  # 这一击其实指的就是当前那条 → 不折腾
        logger.info("补判命中（新坐标）→ 开始分析：[%s] %s",
                    "对方" if target.is_other_party else "我", self._log_text(target.text, 16))
        self.target = target
        self._render_target()

    def _on_click_miss(self) -> None:
        """这一击没命中任何消息（两条点击通道共用）。

        第 79 轮修 bug（用户："极少数情况下点击消息气泡会没有反应，要再点一两下"）时，
        这里改成了"先挂起这一击、等新坐标重判" —— 理由是命中测试用的是**上一次读取**的矩形
        （慢档 2s 一次），点气泡有可能被判成"点空白"。
        但第 79 轮续（用户："有时候按空白区域面板不会收回，要再点一两下才收回"）暴露了代价：
        **只要 Raw Input 先到**（它基本总是先到），一次真正的空白点击也会被挂起，
        重判窗口是 1.2s → 用户看到的"没反应"；等他再点一下（此时已有挂起记录）才立刻收起。
        现在按"面板是不是开着"分流：
          · 面板开着 + 坐标够新 → **立刻收起**（用户要的就是"点空白马上收"）；
          · 面板开着 + 坐标过期 → 挂起，但窗口只有 0.45s（一般 0.1-0.2s 就有新坐标）；
          · 面板没开着 → 挂起，窗口 1.2s（保证"点气泡要能开出来"不被过期坐标吃掉，
            切会话瞬间 self.messages 是空的，这一支必须留着）。
        """
        cursor = w32.get_cursor_pos()
        if self._point_in_overlays(*cursor):
            return                       # 点在自己面板/选项条上 → 什么都不做（第 42 轮口径）
        if getattr(self, "_pending_click", None) is not None:
            # 上一击还挂着（用户已经"没反应"到又点了一下）→ 立刻按点空白处理
            self._pending_click = None
            self._finish_empty_click(*cursor)
            return
        now = time.time()
        age = now - getattr(self, "_messages_ts", 0.0)
        visible = self.panel.isVisible()
        fresh = bool(self.messages) and age <= self.CLICK_FRESH_S
        if not fresh and self._point_in_qq_window(*cursor):
            if self.capture.is_paused():        # 安静期读不到新坐标 → 先恢复（用户已经在点了）
                self.capture.set_paused(False)
            window = self.PENDING_CLICK_VISIBLE_S if visible else self.PENDING_CLICK_HIDDEN_S
            self._pending_click = {"t": now, "x": cursor[0], "y": cursor[1], "window": window}
            self.capture.request_now()
            self._last_activity_ts = now
            logger.info("点击没命中（坐标 %.2fs 前读的，面板%s）→ 挂起重判 ≤%.2fs",
                        age, "开着" if visible else "关着", window)
            return
        self._finish_empty_click(*cursor)

    def _point_in_qq_window(self, x: int, y: int) -> bool:
        """点是否落在 QQ 会话窗口矩形里（用来判断"这一击值不值得等新坐标再判一次"）。"""
        rect = self.window_rect
        if not rect:
            return False
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def _point_in_overlays(self, x: int, y: int) -> bool:
        """（见下）点 (x,y) 是否落在我们自己的浮窗上。"""
        """点 (x,y) 是否落在我们自己的面板/选项条矩形内（物理坐标）。

        为什么需要它：**分析面板**是 `WA_TransparentForMouseEvents` 的穿透窗口（恒穿透、只读展示），
        系统会把点击投给下面的 QQ，我们收不到"点自己窗口"的事件 → 只能靠几何判断，
        否则"点面板"会被当成"点空白处"，直接触发收起（用户反馈的 bug）。
        （选项条自己收鼠标事件 —— 悬停高亮、右上角刷新、点某一条都靠它；
          按 `_toggle_target` 里的口径，点选项条同样不该被当成"点空白"。）
        """
        scale = self.scale or 1.0
        # 第 79 轮：首次启动提示也算"我们的窗口" —— 否则点它会被当成点空白 → 收起浮窗。
        for widget in (self.panel, self.options, self.hint):
            try:
                if not widget.isVisible():
                    continue
                geometry = widget.geometry()             # Qt 逻辑坐标
                x0, y0 = geometry.x() * scale, geometry.y() * scale
                x1 = x0 + geometry.width() * scale
                y1 = y0 + geometry.height() * scale
                if x0 <= x <= x1 and y0 <= y <= y1:
                    return True
            except Exception:
                continue
        return False

    def _point_in_widget(self, widget, x: int, y: int) -> bool:
        """点 (x,y)（物理坐标）是否落在某个浮窗矩形内。"""
        try:
            if not widget.isVisible():
                return False
            scale = self.scale or 1.0
            geometry = widget.geometry()
            x0, y0 = geometry.x() * scale, geometry.y() * scale
            x1 = x0 + geometry.width() * scale
            y1 = y0 + geometry.height() * scale
            return x0 <= x <= x1 and y0 <= y <= y1
        except Exception:
            return False

    def _context_snapshot_text(self) -> tuple:
        """把"累积池"和"本轮真正喂给模型的消息"对齐着显示：★=目标 ●=会进分析 ○=只在池子里。

        第 60 轮修正：之前 ●/○ 是**按预算估算**出来的（跟真实 prompt 不是同一份数据），
        拿它对账就会觉得"分析用的上下文根本不是这个池子里的"。现在直接调
        `MessageReader.build_context(...)`——与 `_request_analysis` 喂给模型的是**同一份**，
        另外把"进了 prompt 但不在池子里"的行（QQ 树里的无坐标历史行）单独列在下面。
        """
        history = list(getattr(self.reader, "_history", []))
        target = self.target or self.reader.latest_other_party(self.messages)
        context: List[Message] = []
        if target is not None:
            try:
                context = self.reader.build_context(self.messages, target)
            except Exception as exc:                 # 面板不能因为取快照失败而炸
                logger.debug("上下文面板取快照失败：%s", exc)
        used = {m.msg_key for m in context}
        pool_keys = {m.msg_key for m in history}
        rows = []
        for message in history:
            speaker = "对方" if message.side == "left" else "我"
            mark = ("★" if target is not None and message.msg_key == target.msg_key
                    else ("●" if message.msg_key in used else "○"))
            rows.append((mark, f"{speaker}: {message.text[:60]}"))
        outside = []
        for message in context:
            if message.msg_key in pool_keys:
                continue
            speaker = {"left": "对方", "right": "我", "history": "历史",
                       "after": "后文"}.get(message.side, message.side)
            outside.append(f"{speaker}: {message.text[:60]}")
        in_pool = sum(1 for mark, _ in rows if mark in ("★", "●"))
        title = (f"上下文历史（池 {len(history)} 条｜本轮进分析 {len(context)} 条 = "
                 f"池子 {in_pool} + 额外 {len(outside)}）")
        body_lines = [f"{mark} {line}" for mark, line in rows]
        if outside:
            body_lines.append("— 本轮也进了 prompt、但不在池子里的行 —")
            body_lines += [f"○ {line}" for line in outside]
        body = "\n".join(body_lines)
        return title, (body or "（还没有记录到上下文：先让 QQ 显示一些消息）")

    def toggle_context_viewer(self) -> None:
        """点状态灯左侧的小图标：开/关上下文历史面板（第 66 轮：走同一套展开/收起动画）。"""
        self._ctx_user_open = not (self._ctx_anim_target > 0.0 and self._ctx_user_open)
        if self._ctx_user_open:
            title, body = self._context_snapshot_text()
            self.context_viewer.set_history(title, body)
            self._ctx_anim_target = 1.0
            if self._ctx_anim <= 0.001:          # 从收起状态展开 → 从 0 开始播
                self._ctx_anim = 0.0
            if self.window_rect is not None:
                self._place_context_viewer()
            self.context_viewer.show()
            self.context_viewer.raise_()
            logger.info("展开上下文历史面板：%s", title)
        else:
            self._ctx_anim_target = 0.0          # 交给快 tick 播收起动画，播完自己 hide
            logger.info("收起上下文历史面板")

    def _maybe_quiet_on_sidebar_click(self) -> None:
        """点在"会话列表"（窗口左侧那一竖条）→ 预判要切会话，**立刻**进入安静期。

        实测：等检测到窗口标题变了再暂停已经太晚 —— 卡顿就发生在 QQ 重建无障碍树的那一刻，
        我们提前 1.5s 停止读取，把进程让给 QQ。
        """
        if self.window_rect is None:
            return
        cursor = w32.get_cursor_pos()
        x0, y0, x1, y1 = self.window_rect
        if not (x0 <= cursor[0] <= x1 and y0 <= cursor[1] <= y1):
            return
        if cursor[0] > x0 + (x1 - x0) * 0.32:      # 只有左侧会话列表那一条竖带
            return
        self.capture.set_paused(True)
        QTimer.singleShot(1500, lambda: self.capture.set_paused(False))
        logger.info("点到会话列表 → 暂停读取 1.5s（预判切会话，避免和 QQ 建树抢资源）")
        return          # ← 本方法到此为止。下面这段是早前补丁**误插**进来的"跟随读取"逻辑
        #   （它属于 on_fast_tick）：在这里执行会在 target 为 None 时抛
        #   AttributeError: 'NoneType' object has no attribute 'msg_key'，已确认修掉。
        # 注意：**这里不能因为 _status_streak>0 就直接 return** —— 滚动时读取偶尔失败会进入
        # 降级状态，若整段跟随逻辑被跳过，面板就只能等降级解除后"啪"地跳过去
        # （用户反馈的"滚轮停下后才开始响应"）。降级与否交给下面的锚点读取去判断：
        # 读得到坐标就照样跟，读不到再走整树重读。
        # 只问"目标那一条"的矩形（1-3ms）：原来每 30ms 把 10-20 个元素全问一遍，
        # 跨进程读累计 20-60ms，既拖慢跟随又给 QQ 添负担 → 换成单条读取。
        # 其余消息的坐标靠"整列同步位移"跟上（滚动时所有消息一起动）。
        one = self.reader.refresh_one(self.target.msg_key)
        anchor_key = self.target.msg_key
        if one is None:
            # 目标那条被虚拟化回收 → 换一条还读得到的当锚点（同屏整列同步位移，
            # 用谁的位移都一样）；这样就不必退化成 80ms 的整树重读。
            other = self.reader.refresh_any()
            if other is not None:
                anchor_key, one = str(other[0]), other[1]
        if one is not None:
            if self._status_streak:
                # 能读到坐标 = 无障碍树是好的 → 立刻解除降级，别让它卡住跟随
                logger.debug("跟随读到坐标 → 降级计数归零（原 %d）", self._status_streak)
                self._status_streak = 0
            anchor = (self._original_message(anchor_key,
                                            self._translated_bbox_of(self.target))
                      if self.target is not None else None) or self.target
            dx = int(one[0]) - int(anchor.bbox[0])
            dy = int(one[1]) - int(anchor.bbox[1])
            # 同上：横向只允许 ±2px（换锚点不等于整列横移）
            dx = max(-2, min(2, dx))
            if dx or dy:
                self._last_motion_ts = time.time()      # 走的是 on_follow_rect 的校准
                for message in self.messages:
                    message.bbox = (message.bbox[0] + dx, message.bbox[1] + dy,
                                    message.bbox[2] + dx, message.bbox[3] + dy)
        rects = {} if one is None else {anchor_key: one}
        stale = 0 if one is not None else 1
        # 滚动/虚拟化会让缓存元素的矩形失效 → 立刻整树重读一次（实测能把滚动跟随的
        # 延迟从"最多 200-500ms"压到 ~80ms）。限流 0.4s，避免持续重读把 QQ 拖卡。
        if stale and time.time() - getattr(self, "_last_stale_poke", 0.0) > 0.15:
            self._last_stale_poke = time.time()
            logger.debug("坐标失效 %d 条 → 立刻重读整棵树（滚动跟随）", stale)
            self.capture.request_now()
        if not rects:
            # 坐标全部失效（滚动中元素被虚拟化回收）→ 仍然推进一次缓动：
            # 否则"缓动"会在每个失效窗口里停住，等下一次读到新坐标时又变成大跳。
            # 实测：补上这一句后，位移从"p90 65px 的大台阶"变成连续小步。
            self._place_windows()
            return
        if stale:
            # 部分元素失效（滚动导致虚拟化重排）：用还能读到的那些先跟上，同时尽快重读一次
            self.capture.request_now()
        changed = False
        for message in self.messages:
            rect = rects.get(message.msg_key)
            if rect is not None and tuple(message.bbox) != tuple(rect):
                message.bbox = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
                changed = True
        if changed:
            # 坐标已经是"新鲜"的绝对屏幕坐标 → 清掉窗口位移补偿，避免二次平移
            if self.window_rect is not None:
                self._read_window_rect = list(self.window_rect)
            self._place_windows()

    # ---------------- 一键填入 ----------------
    def on_option_chosen(self, index: int) -> None:
        try:
            self._on_option_chosen(index)
        except Exception as exc:                  # 任何异常都不许把 app 带崩
            logger.exception("一键填入失败")
            self.panel.show_notice("填入失败", f"{type(exc).__name__}: {exc}",
                                   hint="可先手动复制文本")
            self._place_windows()

    def _on_option_chosen(self, index: int) -> None:
        if self.target is None:
            return
        # 第 79 轮（用户："流式生成时点击选项没有反应"）：正文优先取**选项条上已经上屏的那条**。
        # 原因：流式期间 `on_analysis_ready` 还没跑，`self.results[msg_key]` 里只有第一段结果
        # （replies 为空）→ 旧代码 `index >= len(result.replies)` 直接 return，点了毫无反应；
        # 只有三条全部生成完、结果落进 self.results 之后才点得动。
        text = ""
        try:
            text = self.options.option_text(index)
        except Exception:
            text = ""
        if not text:
            result = self.results.get(self.target.msg_key)
            if result is None or index >= len(result.replies):
                return
            text = result.replies[index].text
        # 第 79 轮：记下"这条消息的选项已经被用过" —— 流式还在生成时，后面到达的 partial
        # 不许再把选项条弹回来（用户口径："点了已生成的选项就填入并收起，别等全部加载完"）。
        self._options_dismissed_key = self.target.msg_key
        mode = "paste" if (self.cfg.fill.enabled and self.cfg.fill.mode == "paste") else "copy"
        # 用户口径（第 35 轮）：**先把选项条带动画收起来，再粘贴**（观感更顺）。
        # 第 64 轮（用户反馈"点选项后填入明显变慢"）：把"找输入框 / 切前台 / 存剪贴板"
        # 这些准备动作**和收起动画并行**跑 —— 原来要等动画播完才开始准备（白等 150-250ms），
        # 现在准备完只等动画结束就粘贴。粘贴时刻仍然是动画播完之后（口径不变）。
        self._options_anim_target = 0.0
        self._place_windows()
        delay_ms = max(160, int(getattr(self.cfg.ui, "anim_ms_options", 320)) + 40)
        paste_at = time.perf_counter() + delay_ms / 1000.0
        self._fill_after_collapse(text, mode, paste_at)

    def _fill_after_collapse(self, text: str, mode: str,
                             paste_at: Optional[float] = None) -> None:
        """把填入丢到**后台线程**执行（主线程不被冻住）；`paste_at` 是"最早可粘贴时刻"。"""
        worker = _FillWorker(lambda: self._fill_or_copy(text, mode, paste_at=paste_at))
        worker.done.connect(self._on_fill_done)
        self._fill_worker = worker          # 持住引用，避免线程对象被回收
        worker.start()

    def on_options_refresh(self) -> None:
        """点选项条右上角刷新图标 → 清缓存并重新生成三条回复（当前目标的上下文不变）。"""
        if self.target is None:
            return
        msg_key = self.target.msg_key
        self._options_dismissed_key = None      # 刷新 = 要看新选项（第 79 轮）
        try:
            self.analyzer.clear_cache(msg_key)           # 线程安全清缓存，强制重新推理
        except Exception:
            pass
        self.results.pop(msg_key, None)
        # 关键：固定 seed(1234)+低温度(0.2) 会让重算得到几乎逐字相同的结果，
        # 手动刷新时换一个随机种子并临时升温，才对得起"刷新"这个动作。
        #   第 80 轮（用户实测"刷新后还是完全一样"）：原来还把上一版三条原文喂回提示词
        #   要求"避开"，实测反而被 4B **照抄**（tmp/probe_refresh2.py C 情景 3/3 重合）；
        #   只留"新种子 + 0.9 温度"，同探针 D1/D2 与上一版 0/3 重合。别再加回去了。
        import random as _random
        self.analyzer.extra_seed = _random.randrange(1, 100000)
        self.analyzer.temp_override = float(getattr(self.cfg.analyzer, "refresh_temperature", 0.9))
        logger.info("手动刷新回复选项：#%s（%s）", msg_key[:8],
                    self._log_text(self.target.text, 14))
        self._options_anim = 0.0
        self._options_anim_target = 1.0
        self.panel.show_analyzing(self.target)           # 面板先回到"分析中"
        self._request_analysis(self.target,
                              force=self.cfg.analyzer.backend != "api")

    def _on_fill_done(self, ok: bool, note: str) -> None:
        """后台填入结束的回调（主线程执行）：只做提示，不阻塞。"""
        # 用户口径（第 31 轮）："回复已就绪"这个面板冗余 → 不再弹面板。
        # 成功就静默（文本已经进输入框了，用户看得见）；只有失败/退化到剪贴板时才提示。
        if not ok:
            self.toast.show_toast("填入未完成", note[:48] if note else "已复制到剪贴板",
                                  near=self._dot_physical_rect() if self.window_rect else None,
                                  duration_ms=3000)
            logger.warning("一键填入未完成：%s", note)
        else:
            logger.info("一键填入完成：%s", note)

    def _fill_or_copy(self, text: str, mode: str,
                      paste_at: Optional[float] = None) -> tuple[bool, str]:
        """一键填入：切前台 → 点输入框 → 一次 Ctrl+V → 回读校验（失败如实回报）。

        第 64 轮提速（用户反馈"点选项后黏贴明显变慢"，实测一次要 ~1.25s）：
          ① 准备动作（找输入框 / 切前台 / 存剪贴板）与"选项条收起动画"**并行**，
             准备完只等 `paste_at` 到点就粘贴（原来动画播完才开始准备）；
          ② 不再先试"不点击直接粘贴" —— 实测那条路 82% 会失败（点选项后 QQ 输入框
             已失去焦点），白花 ~200ms 回读 + ~120ms 点击才走到真正的粘贴；
             现在**固定：点一次输入框（拿焦点）→ 粘贴一次**；
          ③ 回读轮询间隔 60ms→40ms、首次等待 300ms；确认成功即返回；
          ④ **只发一次 Ctrl+V，绝不补粘**（第 79 轮修复"点击选项会复制两次"）：新版 QQ
             不把输入框内容暴露给 UIA，旧逻辑据此误判"没贴进去"又贴一次 → 输入框两份。
             现在回读不到内容就按"无法校验"处理：不重贴、不弹失败提示（文本留在剪贴板兜底）。
        全程只发一次 Ctrl+V（粘贴前会复核前台窗口），绝不回车。
        """
        t0 = time.perf_counter()
        if mode == "copy":
            return (w32.clip_write_text(text), "已写剪贴板")
        window = self.snapshot.get("window") or {}
        hwnd = int(window.get("hwnd") or 0)
        if not hwnd or self.window_rect is None:
            return False, "拿不到 QQ 窗口句柄"
        input_box = self.reader.uia.find_input_box(self.window_rect)
        if not input_box:
            # 找不到输入框就绝不盲点屏幕，退化为只复制（SPEC 的安全选择）
            ok = w32.clip_write_text(text)
            return ok, "未定位到输入框（或不在屏幕内），已复制到剪贴板"
        # 注意（踩过）：这里**不要**隐藏我们的浮窗。之前为了"让 Ctrl+V 一定打到 QQ"隐藏了
        # 面板+选项条，但 50ms 后的"面板自愈"会认为"有目标却没显示"又把它们弹回来，
        # 表现为"选完选项后两个窗口自动关闭又重新展开"。前台问题已由下面的
        # bring_window_to_front + ensure_foreground 解决。
        w32.bring_window_to_front(hwnd)
        if not w32.ensure_foreground(hwnd):
            time.sleep(0.2)
            w32.bring_window_to_front(hwnd)
            if not w32.ensure_foreground(hwnd):
                # 按键必须打在 QQ 上；切不到前台就只复制（否则会打到别的程序里）
                w32.clip_write_text(text)
                return False, "无法把 QQ 窗口切到前台（已复制到剪贴板，请手动 Ctrl+V）"
        cx = int((input_box[0] + input_box[2]) / 2)
        cy = int((input_box[1] + input_box[3]) / 2)

        # 第 63 轮加固：填入会用回复文本覆盖剪贴板 —— 先存一份，**成功填入后还原**，
        # 免得用户原来的剪贴板内容被吃掉。原来存的是图片/文件（text() 为空）就完全不碰，
        # 避免把非文本内容清掉。失败时故意**不还原**：那时文本要靠剪贴板手动 Ctrl+V。
        # （读剪贴板走原生 Win32：实测 0-1ms，且不依赖 GUI 线程的 Qt/OLE 剪贴板。）
        previous_clip = w32.clip_read_text() or ""

        def restore_clipboard() -> None:
            if previous_clip:
                w32.clip_write_text(previous_clip)

        # 准备完了：等"选项条收起动画"播完那一刻再粘贴（口径：先收面板再粘贴）
        wait_ms = 0
        if paste_at is not None:
            remaining = paste_at - time.perf_counter()
            if remaining > 0:
                wait_ms = int(remaining * 1000)
                time.sleep(remaining)

        prep_ms = int((time.perf_counter() - t0) * 1000)
        paste_t = time.perf_counter()
        # 第 74 轮（用户："自动填入一般没问题但挺容易失败，能不能确保成功"）：
        # ① **优先用 UIA SetFocus 拿焦点** —— 不注入任何输入事件、不动鼠标，
        #    比"点输入框中心"可靠得多（也不会出现点歪/鼠标卡住）；
        # ② UIA 聚焦失败才退回（第 73 轮加固过的）模拟点击，并且**用刚读到的矩形**去点，
        #    避免动画/窗口移动期间坐标过期点歪；
        # ③ `fill.allow_mouse_click=False` 仍然可以把鼠标注入完全关掉。
        allow_click = bool(getattr(self.cfg.fill, "allow_mouse_click", True))
        focused = False
        try:
            focused = bool(self.reader.uia.focus_input_box(self.window_rect))
        except Exception as exc:
            logger.debug("UIA 聚焦输入框异常：%s", exc)
        clicked = False
        if not focused and allow_click:
            fresh_box = None
            try:
                fresh_box = self.reader.uia.find_input_box(self.window_rect)
            except Exception:
                fresh_box = None
            box = fresh_box or input_box
            cx = int((box[0] + box[2]) / 2)
            cy = int((box[1] + box[3]) / 2)
            clicked = w32.click_at_physical(cx, cy)
            if clicked:
                time.sleep(0.06)
        logger.info("一键填入：聚焦方式=%s（UIA=%s，鼠标点击=%s）",
                    "UIA" if focused else ("点击" if clicked else "无"), focused, clicked)
        # 【只发这一次 Ctrl+V】第 79 轮修复（用户："点击选项会复制两次"）：
        # 新版 QQ **不把输入框内容暴露给 UIA**（回读永远是空/None），旧逻辑据此
        # 认为"没贴进去"→ 又补粘一次 → 输入框里出现两份。现在**绝不重贴**。
        w32.fill_text_into_foreground(text, expect_hwnd=hwnd)
        # 第 74 轮：回读窗口从 300ms 放宽到 600ms —— QQ 的 DOM→UIA 同步在慢机器上
        # 有时要 300-500ms，窗口太短会把"其实已经填进去"误判成失败（用户反馈"容易失败"）。
        if self._wait_input_contains(text, 600, interval=0.05):
            restore_clipboard()
            logger.info("一键填入：粘贴后回读通过（准备 %dms 含等待动画 %dms｜发送→确认 %dms）",
                        prep_ms, wait_ms, int((time.perf_counter() - paste_t) * 1000))
            return True, "已填入输入框（未发送，已回读校验）"

        # 回读没通过 → 先看"是不是读不出内容"（新版 QQ 的常态）：
        # 读不出 ≠ 没贴进去，这种情况**按成功处理**（不弹失败提示、不重贴），
        # 文本同时留在剪贴板里当作手动兜底。
        current = self.reader.uia.read_input_text(self.window_rect)
        if current is None or not str(current).strip():
            # 但"连焦点都没拿到"是另一回事：这时粘贴几乎不可能打进去（旧版本会在这里
            # 谎报成功 —— 实测日志里 SendInput 0/3、聚焦=无，却显示"已填入"）。
            if not focused and not clicked:
                logger.warning("一键填入未生效：既没拿到 UIA 焦点也没点中输入框"
                               "（文本已留在剪贴板）")
                return False, ("没拿到输入框焦点（已复制到剪贴板，请手动 Ctrl+V）——"
                               "若 QQ 是“以管理员身份运行”，请同样以管理员启动本工具，"
                               "或在设置里关掉“允许自动点击输入框”")
            logger.info("一键填入：已发出粘贴，但本机不暴露输入框内容（无法校验，未重贴）"
                        "｜UIA 聚焦=%s｜鼠标点击=%s", focused, clicked)
            return True, "已填入输入框（未发送；本机不暴露内容，未校验）"

        # 能读到内容但里面没有目标文本 → 才是真的没生效（不再补粘贴）
        if self._input_contains(text):
            restore_clipboard()
            logger.info("一键填入：回读晚到但仍确认成功（准备 %dms｜发送→确认 %dms）",
                        prep_ms, int((time.perf_counter() - paste_t) * 1000))
            return True, "已填入输入框（未发送，已回读校验）"

        # 还是不行 → 保留剪贴板内容，如实告知（不假称成功）
        w32.clip_write_text(text)
        logger.warning("一键填入未生效：UIA 聚焦=%s｜鼠标点击=%s｜允许点击=%s｜输入框回读=%s",
                       focused, clicked, allow_click,
                       "无法读取" if current is None else repr(current[:30]))
        if not focused and not clicked:
            # 连焦点都没拿到：最常见原因是 QQ 以管理员身份运行（UIPI 拦住输入注入），
            # 或 UIA 找不到输入框节点。
            # 点击没落地：最常见原因是 QQ 以管理员身份运行（UIPI 会拦住普通权限进程的输入注入）
            return False, ("没拿到输入框焦点（已复制到剪贴板，请手动 Ctrl+V）——"
                           "若 QQ 是“以管理员身份运行”，请同样以管理员启动本工具，"
                           "或在设置里关掉“允许自动点击输入框”")
        return False, "填入未生效（已复制到剪贴板，请手动 Ctrl+V）"

    def _input_contains(self, text: str) -> bool:
        """只读回读输入框，确认文本真的进去了（不猜、不假装成功）。"""
        current = self.reader.uia.read_input_text(self.window_rect)
        if not current:
            return False
        probe = text.strip()[:12]
        return bool(probe) and probe in current

    def _wait_input_contains(self, text: str, timeout_ms: int = 1200,
                             interval: float = 0.15) -> bool:
        """回读校验带重试。

        实测坑：Ctrl+V 之后 QQ 把文本渲染进 DOM、再到 UIA 能读到，有 200-600ms 的延迟，
        原来只等 250ms 就读一次 → 经常误判"填入未生效"（日志里 输入框回读='\\n'），
        其实文本已经进去了。这里改成轮询到 timeout，成功即返回。
        """
        deadline = time.time() + max(0.2, timeout_ms / 1000.0)
        while True:
            if self._input_contains(text):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(interval)

    # ---------------- 设置 ----------------
    def open_settings(self) -> None:
        """打开设置窗口。

        注意：**不要用 exec()**（应用级模态会阻塞本应用其他窗口的输入，表现为
        "点完状态灯后选项点不动"），改用非模态 show()。"""
        if self.settings_dialog is not None and self.settings_dialog.isVisible():
            # 第 79 轮（用户："打开设置面板并最小化后，再点状态灯希望能把它呼出来"）：
            # 最小化的窗口在 Qt 里 isVisible() 仍是 True，只 raise_()/activateWindow()
            # 不会把它还原 → 点了像"毫无反应"。这里先 showNormal() 还原再置前。
            if self.settings_dialog.isMinimized():
                self.settings_dialog.showNormal()
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        logger.info("打开设置窗口（非模态）")
        self._backend_before_settings = self.cfg.analyzer.backend   # 用于关闭时判断是否需要清缓存
        self._analysis_sig_before_settings = self._analysis_signature()   # 提示词/配置/采样
        self.panel.hide()
        self.options.hide()
        try:
            dialog = SettingsDialog(self.cfg)
        except Exception as exc:               # 构造失败也要把面板恢复，别让 UI 卡在"全收起"状态
            logger.exception("设置窗口创建失败")
            self.toast.show_toast("设置打不开", f"{type(exc).__name__}: {exc}", duration_ms=4000)
            if self.target is not None:
                self._render_target()
            return
        dialog.setModal(False)
        dialog.finished.connect(self.on_settings_closed)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        # Qt 会给无父窗口的对话框自动挂 owner（可能是我们隐藏中的浮窗）→ 会被一起隐藏。
        # 这里显式清掉 owner，让它成为独立顶层窗口。
        try:
            w32.set_owner_window(int(dialog.winId()), 0)
        except Exception:
            pass
        w32.force_show_window(int(dialog.winId()), topmost=True)
        # 用户刚点了我们的状态灯 → 本进程刚收到输入，frontground 锁已放开，可以把自己提到前台
        try:
            w32.ensure_foreground(int(dialog.winId()), timeout_s=0.6)
        except Exception:
            pass
        self.settings_dialog = dialog

    def _analysis_signature(self) -> str:
        """"影响分析结果的所有设置"的签名（提示词 / 当前配置 / 采样 / 后端）。

        第 80 轮（用户实测："导入配置后，自我分析好像不是按导入的配置的提示词控制的"）：
        设置窗口关闭时拿它和打开前比一比，**变过就把全部已算结果作废** ——
        否则换配置/导入新配置后，点到以前分析过的消息，面板显示的还是旧配置的结果
        （旧逻辑只重算了"当前这一条"）。
        """
        cfg = self.cfg
        blob = {
            "backend": cfg.analyzer.backend,
            "active": cfg.active_profile,
            "profiles": cfg.fields_profiles,
            "fields": getattr(cfg.fields, "__dict__", {}),
            "self_fields": getattr(cfg.self_fields, "__dict__", {}),
            "style": cfg.analysis_style_prompt,
            "self_style": cfg.self_analysis_style_prompt,
            "api": {"model": cfg.api.model, "quick": cfg.api.max_tokens_quick,
                    "replies": cfg.api.max_tokens_replies},
            "sample": {k: getattr(cfg.analyzer, k, None) for k in
                       ("temperature", "top_k", "top_p", "min_p", "repeat_penalty", "seed")},
        }
        payload = json.dumps(blob, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def on_settings_closed(self, _result: int) -> None:
        self.settings_dialog = None
        # 后端（本地/API）切换后旧缓存不再适用 → 整表清掉，避免"API 模式显示本地结果"这类混用
        previous_backend = getattr(self, "_backend_before_settings", None)
        if previous_backend is not None and previous_backend != self.cfg.analyzer.backend:
            logger.info("分析后端 %s → %s，清空结果缓存", previous_backend, self.cfg.analyzer.backend)
            self.analyzer.clear_cache()
            self.results.clear()
        # 第 80 轮（用户实测："导入配置后，自我分析好像不是按导入的配置的提示词控制的"）：
        # 以前只有"后端变了"才清缓存 —— 换配置 / 改提示词 / 导入一套新配置后，**别的消息**
        # 还留着旧配置算出来的结果，点开就是旧的（只有当前那条会被重算）。
        # 现在把"影响分析结果的所有设置"做个签名，变过就整表清掉。
        previous_sig = getattr(self, "_analysis_sig_before_settings", None)
        if previous_sig is not None and previous_sig != self._analysis_signature():
            logger.info("提示词/配置/采样设置变过 → 清空结果缓存（配置：%s）",
                        self.cfg.active_profile or "默认")
            self.analyzer.clear_cache()
            self.results.clear()
        self.dot.apply_config()
        self.geometry_timer.setInterval(self.cfg.ui.follow_poll_ms or self.GEOMETRY_POLL_MS)
        self._place_windows()
        logger.info("设置已关闭：backend=%s prefix=%s only_other=%s dot=%s",
                    self.cfg.analyzer.backend, self.cfg.ui.show_reply_style_prefix,
                    self.cfg.ui.only_other_party, self.cfg.ui.dot_size_px)
        if self.target is not None:
            self.results.pop(self.target.msg_key, None)   # Prompt/后端可能变了 → 重算
            self._render_target()


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.debug)
    # Qt 槽函数里未捕获的异常默认会终止进程；改成只记日志（Worker/UI 都不许把 app 带崩）
    def _excepthook(exc_type, exc, tb):
        logger.error("未捕获异常：%s: %s", exc_type.__name__, exc, exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook
    # 第 81 轮末（用户："退出程序后关机，有时会弹'xxx 不能为 read'的报错"）：
    # 排障工具：**只有 config.json 里 debug=true 时才开** faulthandler —— 开了以后万一真发生
    # 原生崩溃（访问冲突），会把当时所有线程的 Python 栈写进 logs/faulthandler.log。
    # 为什么默认关：在 Windows 上 faulthandler 会把**被内部处理掉的 first-chance 访问冲突**
    # 也打印出来（实测：连"只创建一个原生窗口"的独立脚本都会在 RegisterClassExW 上报 3 条，
    # 而进程照常跑完）→ 默认开着只会往日志里灌假警报。
    if cfg.debug:
        try:
            import faulthandler
            _fh = open(app_config.LOG_DIR / "faulthandler.log", "a", buffering=1,
                       encoding="utf-8", errors="replace")
            faulthandler.enable(file=_fh, all_threads=True)
            logger.info("faulthandler 已启用 → logs/faulthandler.log（debug=true）")
        except Exception as exc:        # 只影响诊断，不影响运行
            logger.debug("faulthandler 未启用：%s", exc)
    w32.set_dpi_awareness()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(load_app_icon())        # 对话框/任务栏统一用应用图标
    # 单实例（SPEC 4.5）：打包成 exe 后双击两次会起两套浮窗/两个读 QQ 的进程。
    # 用 QLocalServer/QLocalSocket（命名管道，不落文件）做互斥：先试着连，连上了说明
    # 已有实例在跑 → 提示并退出；连不上就自己 listen（顺便清掉上次异常退出留下的名字）。
    SOCKET_NAME = "ChatAssistant.single.v1"
    probe = QLocalSocket()
    probe.connectToServer(SOCKET_NAME)
    if probe.waitForConnected(300):
        # 第 80 轮末修（用户口径："每次退出好像都有残留"）：**这里原来弹的是模态框**。
        # 实测坑：用隐藏窗口/自启动/脚本拉起时那个框没人点 → 进程永远卡在等点击上，
        # 任务管理器里就多一个"残留"进程（我自己重启开发版时连着留了两个）。
        # 现在改成：给已有实例发一个字节的"activate"，让它把自己的设置面板拉出来，
        # 本次启动立刻退出 —— 不弹窗、不留进程，反馈也看得见（托盘里那个小图标就是它）。
        try:
            probe.write(b"activate")
            probe.flush()
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
        except Exception as exc:
            logger.debug("通知已有实例失败：%s", exc)
        logger.info("已有实例在运行 → 已通知它打开设置面板，本次启动直接退出（不弹窗、不留进程）")
        return 0
    single_server = QLocalServer()
    single_server.removeServer(SOCKET_NAME)      # 清理上次异常退出可能留下的名字
    if not single_server.listen(SOCKET_NAME):
        logger.warning("单实例服务监听失败：%s（继续启动，但可能允许重复启动）",
                       single_server.errorString())
    assistant = ChatAssistantApp(app, cfg)
    assistant.start()

    def _on_second_launch() -> None:
        """又启动了一次（双击 exe / 再次运行脚本）→ 把设置面板拉出来。

        替代原来那个"会卡住进程的模态框"：用户在托盘/状态灯上能找到入口，这里给个立刻可见的反馈。
        """
        while single_server.hasPendingConnections():
            conn = single_server.nextPendingConnection()
            try:
                conn.readAll()
                conn.disconnectFromServer()
            except Exception:
                pass
        logger.info("收到「又启动了一次」的通知 → 打开设置面板")
        assistant.open_settings()

    single_server.newConnection.connect(_on_second_launch)
    if "--open-settings" in sys.argv:       # 调试：不依赖"刚点过状态灯"，用于验证托盘路径
        QTimer.singleShot(3000, assistant.open_settings)
    app.aboutToQuit.connect(assistant.shutdown)
    code = app.exec()
    # 第 79 轮（用户："退出软件的速度似乎有点慢"）实测：
    #   · 我们自己的收尾（隐藏界面/停线程/存配置/释放模型）≈ 470ms；
    #   · 事件循环退出之后，**Python 解释器收尾 + 卸载 llama.dll/ggml-cuda.dll（800MB）
    #     + CUDA 上下文销毁**还要约 3.9 秒 → 进程总共 4.4s 才消失。
    # 这部分在 Python 里没法"优化"，而且它没有任何用户可见价值（配置已存盘、线程已停、
    # 显存由系统在进程消失时回收）。所以：日志刷盘后直接结束进程，让系统做最后回收。
    logger.info("事件循环已退出 → 跳过 DLL/CUDA 卸载收尾，直接结束进程")
    # 第 79 轮修正（用户反馈"退出好像还更慢了"）：跳过 DLL/CUDA 卸载之后进程是"啪"地消失，
    # 但 Qt 也就没机会注销托盘图标 → Windows 托盘区会留一个**残影**
    # （要等鼠标划过才消失），看起来就像"还没退干净、比之前更慢"。
    # 这里显式 hide()（Qt 会发 NIM_DELETE），再让事件循环把这条消息真正发出去。
    try:
        assistant.tray.hide()
        app.processEvents()
    except Exception as exc:
        logger.debug("注销托盘图标异常：%s", exc)
    try:
        logging.shutdown()          # 把缓冲的日志写进文件（避免丢退出日志）
    except Exception:
        pass
    os._exit(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
