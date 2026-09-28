#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui/overlay_dot.py - 状态灯 / 设置入口（SPEC 3.2，B2-M6 + 后续需求）

- 贴 QQ 窗口右上角，随窗口几何移动（主线程统一驱动）；
- 颜色即状态：绿=就绪 / 黄=分析中 / 灰=暂停或未就绪 / 红=错误；
- **鼠标移上去时图标变成齿轮，点击打开设置**（设置入口）；
  ∴ 这个窗口现在是"可交互"的（不再全穿透），但只占很小一块（默认 26px），
    且不改 QQ 的任何属性；不想点它时不会影响 QQ 的其他区域。
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from config import Config
from utils import win32_api as w32

STATUS_COLORS = {
    "ready": "#3FB950",
    "analyzing": "#D29922",
    "paused": "#6E7681",
    "no_conversation": "#6E7681",
    "no_window": "#6E7681",
    "unavailable": "#6E7681",
    "error": "#F85149",
    "init": "#6E7681",
}


class OverlayDot(QWidget):
    def __init__(self, cfg: Config, on_click: Optional[Callable[[], None]] = None) -> None:
        super().__init__(None)
        self.cfg = cfg
        self.on_click = on_click
        # 不再用全局 TopMost：改成"QQ 窗口的 owned window"（见 set_owner），
        # 这样它只在 QQ 之上，不会盖住其他应用。
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.resize(cfg.ui.dot_size_px, cfg.ui.dot_size_px)
        self._status = "init"
        self._hover = False
        self.setToolTip("ChatAssistant 状态灯")

    def apply_config(self) -> None:
        size = self.cfg.ui.dot_size_px
        self.resize(size, size)
        self.update()

    def set_status(self, status: str) -> None:
        if status != self._status:
            self._status = status
            self.setToolTip(f"ChatAssistant：{status}")
            self.update()

    @property
    def status(self) -> str:
        return self._status

    def move_to(self, window_rect: Sequence[int]) -> None:
        """默认贴在 QQ 窗口**上沿外侧**（不与标题栏的最小化/最大化/关闭按钮重叠）；
        上方没有屏幕空间时，退到标题栏内、窗口按钮的左侧。"""
        margin = self.cfg.ui.dot_margin_px
        size = self.cfg.ui.dot_size_px
        screen = w32.get_virtual_screen_rect()
        x = int(window_rect[2]) - margin - size
        y = int(window_rect[1]) - size - 6                  # 首选：窗口上方外侧
        if y < screen[1]:
            y = int(window_rect[1]) + margin                # 兜底：标题栏内
            x = int(window_rect[2]) - margin - size - self.cfg.ui.dot_avoid_buttons_px
        x = max(screen[0], min(x, screen[2] - size))
        y = max(screen[1], min(y, screen[3] - size))
        self.setGeometry(QRect(x, y, size, size))

    def set_owner(self, owner_hwnd: int) -> None:
        """把状态灯挂到 QQ 窗口下（只在 QQ 之上，不盖其他应用）。"""
        try:
            w32.set_owner_window(int(self.winId()), owner_hwnd)
        except Exception:
            pass

    # ---------------- 交互：悬停变齿轮、点击开设置 ----------------
    def enterEvent(self, event) -> None:          # noqa: N802
        self._hover = True
        self.setToolTip("点击打开 ChatAssistant 设置")
        self.update()

    def leaveEvent(self, event) -> None:          # noqa: N802
        self._hover = False
        self.setToolTip(f"ChatAssistant：{self._status}")
        self.update()

    def mousePressEvent(self, event) -> None:     # noqa: N802
        if event.button() == Qt.LeftButton and self.on_click is not None:
            self.on_click()

    def paintEvent(self, event) -> None:          # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(STATUS_COLORS.get(self._status, "#6E7681"))
        size = min(self.width(), self.height())
        # 外圈半透明底 + 内圈实心，保证在任何背景上都看得清
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 90))
        painter.drawEllipse(0, 0, size, size)
        painter.setBrush(color)
        inset = max(2, size // 10)
        painter.drawEllipse(inset, inset, size - 2 * inset, size - 2 * inset)
        if self._hover and self.cfg.ui.dot_hover_gear:
            self._draw_gear(painter, size)
        painter.end()

    def _draw_gear(self, painter: QPainter, size: int) -> None:
        """在圆点中央画一个齿轮（纯绘制，不依赖图片资源）。"""
        center = QPointF(size / 2.0, size / 2.0)
        outer = size * 0.30
        inner = size * 0.16
        pen = QPen(QColor("#0D1117"))
        pen.setWidthF(max(1.6, size * 0.075))
        painter.setPen(pen)
        painter.setBrush(QColor("#0D1117"))
        teeth = 8
        for i in range(teeth):
            angle = 2 * math.pi * i / teeth
            tx = center.x() + math.cos(angle) * outer
            ty = center.y() + math.sin(angle) * outer
            painter.drawEllipse(QPointF(tx, ty), size * 0.055, size * 0.055)
        painter.setPen(QPen(QColor("#0D1117"), max(1.6, size * 0.075)))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center, inner + size * 0.07, inner + size * 0.07)
        painter.setBrush(QColor("#0D1117"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(center, inner * 0.45, inner * 0.45)
