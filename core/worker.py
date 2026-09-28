#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/worker.py - 线程模型（SPEC 4.1 / 5.5，B2-M5）

红线（与 SPEC 一致）：
  - UI 主线程只做渲染 + 托盘 + 两个定时器；Worker 里**绝不碰 UI**；
  - 队列 maxsize=1：新任务直接替换排队中的旧任务；
  - 同一时刻最多 1 个推理在飞；过期结果（msg_key 已不是当前目标）由主线程丢弃；
  - 主线程是窗口几何的唯一来源。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from config import Config
from core.analyzer import AnalysisResult, Analyzer, CancelToken, GenerationCancelled
from core.message_reader import Message, MessageReader

logger = logging.getLogger("core.worker")


# --------------------------------------------------------------------------- #
# 请求（maxsize=1，新任务替换旧任务）
# --------------------------------------------------------------------------- #
@dataclass
class AnalyzeRequest:
    message: Message
    context: List[Message] = field(default_factory=list)
    generation: int = 0
    requested_at: float = field(default_factory=time.time)


class LatestRequestQueue:
    """线程安全的"容量 1、新任务替换旧任务"队列（SPEC 4.1）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request: Optional[AnalyzeRequest] = None
        self._event = threading.Event()
        self._generation = 0
        self.active_token: Optional[object] = None      # 正在生成的那条的取消令牌
        self.preempt: bool = True                       # 是否为"抢占式取消"（本地后端开、API 关）

    def set_active_token(self, token: Optional[object]) -> None:
        with self._lock:
            self.active_token = token

    def cancel_active(self) -> bool:
        """取消"正在生成"的那条（只对本地后端用；API 调用不取消，避免重复计费）。"""
        with self._lock:
            token = self.active_token
        if token is None:
            return False
        cancel = getattr(token, "cancel", None)
        if callable(cancel):
            cancel()
            return True
        return False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def put(self, message: Message, context: List[Message]) -> int:
        # 新目标到来 → 立刻取消正在生成的那条（本地后端；API 模式由调用方关闭抢占，
        # 避免"取消了再重发"白白多花 token）
        if self.preempt:
            self.cancel_active()
        with self._lock:
            self._generation += 1
            self._request = AnalyzeRequest(message=message, context=list(context),
                                           generation=self._generation)
            self._event.set()
            return self._generation

    def get(self, timeout: float = 0.2) -> Optional[AnalyzeRequest]:
        if not self._event.wait(timeout):
            return None
        with self._lock:
            request, self._request = self._request, None
            self._event.clear()
            return request

    def clear(self) -> None:
        with self._lock:
            self._request = None
            self._event.clear()

    def wake(self) -> None:
        """把"等待取任务"的线程立刻叫醒（第 79 轮：退出时不用再干等 0.2s 轮询超时）。

        `get()` 是 `_event.wait(timeout=0.2)`：停线程后它还要等这次超时走完才回到循环顶
        检查停止标志 —— 实测让退出多花 150ms 左右。这里主动 set() 一下即可立刻返回。
        """
        self._event.set()

    def has_pending(self) -> bool:
        with self._lock:
            return self._request is not None


# --------------------------------------------------------------------------- #
# 读取线程
# --------------------------------------------------------------------------- #
class CaptureWorker(QThread):
    """按 SPEC 4.2.3 的节奏读取消息：前台 500ms / 后台 2s；暂停时不读。"""

    messagesUpdated = Signal(object, object)      # (list[Message], snapshot)

    def __init__(self, cfg: Config, reader: MessageReader, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.reader = reader
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = False
        self.foreground = True
        self.hovering = False
        self._fast = False              # 见 set_fast()：是否"正在用"（面板开着/刚操作过）

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request_now(self) -> None:
        """请求"立刻读一次"（元素失效/需要重建时的加急通道）。"""
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def is_paused(self) -> bool:
        """当前是否暂停读取（切会话/点会话列表后的安静期）。"""
        return bool(self._paused)

    def set_context(self, foreground: bool, hovering: bool) -> None:
        self.foreground, self.hovering = foreground, hovering

    def set_fast(self, fast: bool) -> None:
        """是否处于"正在用"状态（面板开着/刚点过/刚滚过）→ 用快读间隔。"""
        self._fast = bool(fast)

    def _interval_s(self) -> float:
        # 第 69 轮（用户反馈"没在分析时显卡风扇一直转"→ 实测是 CPU）：
        # 整树 UIA 读取既烧我们的 CPU，也逼 QQ 的渲染进程做无障碍序列化 ——
        # 实测鼠标在 QQ 窗口里时：我们 83% 单核 + QQ 渲染进程 43.8% 单核（关掉我们后 QQ 只剩 2.3%）。
        # 而 `trigger_mode="click"`（默认）下**根本不需要悬停即分析**：
        # 点击用"最近一次读取的消息矩形"做命中测试就够了，滚动/换会话也有各自的事件通道。
        # 所以点击模式下把读取间隔放宽到 `poll_click_mode_ms`（默认 400ms），
        # 悬停模式仍保留 120ms 的快读（那是 hover 触发的刚需）。
        click_mode = getattr(self.cfg.ui, "trigger_mode", "click") == "click"
        if click_mode:
            if not (self.hovering or self.foreground):
                base_ms = self.cfg.uia.poll_background_ms
            elif getattr(self, "_fast", False):
                base_ms = self.cfg.uia.poll_foreground_ms        # 正在用 → 300ms
            else:
                base_ms = int(getattr(self.cfg.uia, "poll_click_mode_ms", 700) or 700)
        elif self.hovering:
            base_ms = getattr(self.cfg.uia, "poll_hover_ms", 200)   # 悬停/滚动时读得更勤
        elif self.foreground:
            base_ms = self.cfg.uia.poll_foreground_ms
        else:
            base_ms = self.cfg.uia.poll_background_ms
        return max(0.05, base_ms / 1000.0)

    def run(self) -> None:
        logger.info("CaptureWorker 启动（轮询 %dms/%dms）",
                    self.cfg.uia.poll_foreground_ms, self.cfg.uia.poll_background_ms)
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                if self._paused:
                    time.sleep(0.3)
                    continue
                messages, snapshot = self.reader.read()
                self.messagesUpdated.emit(messages, snapshot)
            except Exception as exc:                      # Worker 永不把异常抛给 Qt
                logger.warning("读取循环异常：%s: %s", type(exc).__name__, exc)
                try:
                    self.messagesUpdated.emit([], {"status": "error",
                                                   "last_error": f"{type(exc).__name__}: {exc}"})
                except Exception:
                    pass
            elapsed = time.perf_counter() - started
            self._wake.wait(max(0.0, self._interval_s() - elapsed))
            self._wake.clear()
        logger.info("CaptureWorker 退出")


# --------------------------------------------------------------------------- #
# 跟随读取线程（B2 第 27 轮新增）
# --------------------------------------------------------------------------- #
# 【第 72 轮删除】这里原来有一个 `WheelWatcher`（WH_MOUSE_LL 全局鼠标钩子）——
# 2026-09-23 的事故代码：低级钩子回调跑在我们自己的线程里，线程被拖住时 Windows 会挂起
# **全系统**鼠标输入（用户实测鼠标失灵，只能从任务管理器结束进程）。它早已停用
# （main.py 里只有一行注释），属于纯死代码，删掉以免以后有人重新启用。
# 现在滚轮改用 `ui/wheel_sink.py` 的 **Raw Input（RIDEV_INPUTSINK）**：系统只投一份事件副本，
# 我们不在输入投递链路上，卡住也绝不会影响系统鼠标。
class ScrollMotionTracker(QThread):
    """实时滚动跟随：抓聊天区窄带做帧间互相关，算出内容真实位移。

    为什么不能用 UIA：实测连续滚动时 Chromium 不更新消息矩形（要等滚动停止），
    所以"读坐标再动"永远慢半拍。
    为什么不用鼠标钩子：低级钩子会让**系统级鼠标输入**等我们线程回调，
    一旦拖住就整机鼠标失灵（2026-09-23 事故）→ 已彻底弃用。
    本类只读屏幕像素（GDI BitBlt），不碰任何输入 API，**不可能卡住鼠标**。

    代价：面板可见且 QQ 在前台时才工作，约 25-30Hz，每次 1-3ms（窄带 BitBlt）。
    """

    motion = Signal(int)                   # 竖直位移（+ = 内容下移）

    def __init__(self, reader: MessageReader, cfg: Config, parent=None) -> None:
        super().__init__(parent)
        self.reader = reader
        self.cfg = cfg
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._area = None                  # (left, top, right, bottom) 抓取区域
        self._interval_ms = 35
        self._last_profile = None
        self.last_dy = 0
        self.last_ms = 0.0

    def set_area(self, area) -> None:
        """设定抓取区域（消息列表左侧窄带）；传 None 表示暂停采集。"""
        with self._lock:
            if area != self._area:
                self._area = area
                self._last_profile = None   # 区域变了，历史帧作废

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from core import screenshot
        while not self._stop.is_set():
            with self._lock:
                area = self._area
            if not area:
                time.sleep(0.05)
                continue
            t0 = time.perf_counter()
            strip = screenshot.grab_gray(*area)
            profile = screenshot.vertical_profile(strip, columns=180)
            self.last_ms = round((time.perf_counter() - t0) * 1000, 2)
            if profile.size:
                prev = self._last_profile
                self._last_profile = profile
                if prev is not None and prev.size == profile.size:
                    # 对比度太低（纯白/纯色区域）时不做估计，避免瞎猜
                    if float(profile.std()) >= 3.0:
                        dy, score = screenshot.best_shift(prev, profile)
                        # 残差要足够小才认，否则宁可不报（防止把噪声当位移）
                        if abs(dy) >= 2 and score <= 6.0:
                            self.last_dy = int(dy)
                            self.motion.emit(int(dy))
            sleep_s = (self._interval_ms - self.last_ms) / 1000.0
            time.sleep(max(0.01, sleep_s))


class FollowReader(QThread):
    """在**后台线程**里做"单条消息坐标"的跨进程 UIA 读取。

    实测（tmp/probe_scroll_pattern.py）：滚动时单次 UIA 读取可能阻塞 **300-400ms**
    （Chromium 渲染进程忙）。如果这个调用发生在 Qt 主线程，事件循环就被冻住 ——
    表现为"滚轮滚动过程中悬浮窗完全没反应，停下后才跳过去"。
    放到后台线程后：主线程只按"已知坐标 + 缓动"走动画，滚动时视觉连续；
    后台线程阻塞多久都不影响界面，读回来再纠正位置。
    """

    updated = Signal(str, object)          # (锚点 msg_key, rect 或 None)

    def __init__(self, reader: MessageReader, cfg: Config, on_need_rebuild=None,
                 parent=None) -> None:
        super().__init__(parent)
        self.reader = reader
        self.cfg = cfg
        self.on_need_rebuild = on_need_rebuild
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._key: Optional[str] = None
        self._interval_ms = 40             # 目标 25Hz（读数快时就是 25Hz，阻塞时就自然变慢）
        self.last_read_ms: float = 0.0

    def set_target(self, msg_key: Optional[str]) -> None:
        with self._lock:
            self._key = msg_key

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                key = self._key
            if not key or not self.reader.uia.available:
                time.sleep(0.05)
                continue
            t0 = time.perf_counter()
            rect = self.reader.refresh_one(key)
            if rect is None:
                other = self.reader.refresh_any()      # 目标被虚拟化回收 → 换锚点
                if other is not None:
                    key, rect = str(other[0]), other[1]
                elif self.on_need_rebuild is not None:
                    self.on_need_rebuild()             # 一条都读不到 → 请求整树重建
            self.last_read_ms = round((time.perf_counter() - t0) * 1000, 1)
            self.updated.emit(key, rect)
            sleep_s = (self._interval_ms - self.last_read_ms) / 1000.0
            time.sleep(max(0.015, sleep_s))


# --------------------------------------------------------------------------- #
# 分析线程
# --------------------------------------------------------------------------- #
class AnalyzerWorker(QThread):
    """串行推理；只处理队列里最新的一条请求（SPEC 4.1）。"""

    analysisStarted = Signal(object)              # Message
    analysisPartial = Signal(object)              # AnalysisResult（流式提前上屏，partial=True）
    analysisReady = Signal(object)                # AnalysisResult
    analysisCancelled = Signal(str)               # msg_key（被抢占取消）
    modelLoaded = Signal(bool, object)            # (ok, status dict)

    def __init__(self, cfg: Config, analyzer: Analyzer, queue: LatestRequestQueue,
                 parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.analyzer = analyzer
        self.queue = queue
        self._stop = threading.Event()
        self._last_generation = 0

    def stop(self) -> None:
        self._stop.set()

    def _on_partial(self, result) -> None:
        """流式提前上屏：标题（情绪·意图）一生成完就上报一次，危险度出来再报一次。

        只有开着流式开关、且还没被抢占时才发；主线程会按 msg_key 判定是不是当前目标。
        """
        if not self.cfg.analyzer.stream_partials:
            return
        token = getattr(self.queue, "active_token", None)
        if token is not None and getattr(token, "cancelled", False):
            return                          # 已被抢占：别再上屏过期内容
        if self.queue.has_pending():
            return
        self.analysisPartial.emit(result)

    def run(self) -> None:
        logger.info("AnalyzerWorker 启动，加载模型……")
        ok = False
        try:
            ok = self.analyzer.load()
        except Exception as exc:
            logger.error("模型加载异常：%s: %s", type(exc).__name__, exc)
        self.modelLoaded.emit(ok, self.analyzer.status())
        if not ok:
            return

        while not self._stop.is_set():
            request = self.queue.get(timeout=0.2)
            if request is None:
                continue
            if request.generation < self._last_generation:
                logger.debug("丢弃过期请求 gen=%s", request.generation)
                continue
            self._last_generation = request.generation
            self.analysisStarted.emit(request.message)
            wait_ms = (time.time() - request.requested_at) * 1000
            if wait_ms > 150:
                logger.info("排队等待 %.0f ms 后才开始分析（前面还有一条在跑）", wait_ms)
            # 本地后端才有取消令牌；API 模式不抢占（避免重复请求浪费 token）
            token = CancelToken() if self.queue.preempt else None
            self.queue.set_active_token(token)
            # 手动刷新用的一次性采样参数（新种子/升温）：本次请求开始时快照，
            # 请求结束再还原 —— 只有工作线程知道"整条请求（两段）跑完了"。
            burst = self.analyzer.refresh_snapshot()
            try:
                result = None
                cancelled = False
                # API 模式默认"单次合并调用"（省钱）：一次请求出全部字段，
                # 而不是发两次（两段式会让 prompt 发两遍，token 直接翻倍）。
                single_call = (self.cfg.analyzer.backend == "api"
                               and bool(getattr(self.cfg.api, "single_call", True)))
                if self.cfg.analyzer.two_stage and not single_call:
                    # 两段式：第一段先上屏（意图/情绪/危险度，约 1s），第二段再补三条回复
                    try:
                        quick = self.analyzer.analyze_quick(request.message, request.context,
                                                            cancel_token=token,
                                                            on_partial=self._on_partial)
                    except GenerationCancelled:
                        quick, cancelled = None, True
                        logger.info("第一段被取消（目标已切换）")
                        self.analysisCancelled.emit(request.message.msg_key)
                    if quick is not None:
                        quick.wait_ms = round(wait_ms, 1)
                        if self.queue.has_pending():      # 用户已经切到别的消息 → 不显示这一段
                            logger.debug("第一段完成但已有新目标，跳过上屏")
                        else:
                            self.analysisReady.emit(quick)
                        try:
                            replies = self.analyzer.analyze_replies(request.message,
                                                                    request.context, quick,
                                                                    cancel_token=token,
                                                                    on_partial=self._on_partial)
                        except GenerationCancelled:
                            replies, cancelled = None, True
                            logger.info("第二段（三条回复）被取消（目标已切换）")
                            self.analysisCancelled.emit(request.message.msg_key)
                        if replies is not None and replies.json_ok:
                            quick.replies = replies.replies
                            quick.degraded = replies.degraded
                            quick.notes.extend(replies.notes)
                            quick.total_ms = round((quick.total_ms or 0)
                                                   + (replies.total_ms or 0), 1)
                            quick.partial = False
                            result = quick
                if result is None and not cancelled:
                    # 第 79 轮：单次合并调用也传 on_partial —— API 模式默认走这条路，
                    # 不传的话流式就不会上屏（用户反馈"API 等待时间比较久"）。
                    result = self.analyzer.analyze(request.message, request.context,
                                                   on_partial=self._on_partial,
                                                   cancel_token=token)
            except GenerationCancelled:
                cancelled = True
                logger.info("生成被取消（目标已切换）")
                self.analysisCancelled.emit(request.message.msg_key)
            except Exception as exc:
                logger.warning("分析异常：%s: %s", type(exc).__name__, exc)
                result = None
            finally:
                self.queue.set_active_token(None)
                self.analyzer.refresh_release(burst)
            if cancelled:
                continue
            if result is not None:
                if self.queue.has_pending():
                    logger.debug("分析完成但队列里已有更新请求，结果仍上报（主线程按 msg_key 判定）")
                self.analysisReady.emit(result)
        logger.info("AnalyzerWorker 退出")
