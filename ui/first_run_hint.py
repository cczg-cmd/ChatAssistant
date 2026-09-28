#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui/first_run_hint.py - 首次启动时的一行使用提示（第 79 轮）

用户口径："应用首次启动时，状态灯下方显示一行文本简单交代使用方式，后续应用启动不再显示，
用户点击提示使该文本淡出关闭。"

和 toast 的区别：**不是**自动消失，而是**等用户点它**才淡出；所以这个窗口必须能收到鼠标点击
（不能像面板/toast 那样 `WA_TransparentForMouseEvents`），同时用
`Qt.WindowDoesNotAcceptFocus` + `WA_ShowWithoutActivating` 保证点它**不抢 QQ 的前台**。
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from config import Config
from ui import theme as theme_mod

logger = logging.getLogger("ui.first_run_hint")

DEFAULT_HINT_TEXT = ("点聊天气泡看分析 · 点选项自动填入输入框 · 状态灯左边的图标是上下文记录")


class FirstRunHint(QWidget):
    clicked = Signal()

    PAD = 12

    def __init__(self, cfg: Config, text: str = "") -> None:
        super().__init__(None)
        self.cfg = cfg
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self._text = text or DEFAULT_HINT_TEXT
        self._alpha = 1.0
        self._hover = False
        self.resize_for_text()
        self._fade = QTimer(self)
        self._fade.setInterval(20)
        self._fade.timeout.connect(self._step_fade)

    # ---------------- 尺寸/位置 ----------------
    def resize_for_text(self) -> None:
        metrics = QFontMetrics(QFont("Microsoft YaHei UI", 9))
        width = max(240, min(720, metrics.horizontalAdvance(self._text) + self.PAD * 2 + 24))
        self.resize(width, 34)

    def place_under(self, anchor: QRect, screen_logical: list,
                    keep_inside: Optional[Sequence[int]] = None) -> None:
        """放在锚点（状态灯）下方；**向左延伸**（右缘对齐状态灯），并夹进 QQ 界面/屏幕。

        用户口径（第 79 轮）："首次启动提示应该向左延伸让文本在 QQ 聊天界面内，
        而不是向右延伸跑到界面外面去。" 状态灯本来就贴在聊天区右上角，所以右缘对齐它、
        往左铺开，文字才会一直落在聊天界面里。
        """
        right = int(anchor.right()) + 1
        x = right - self.width()
        if keep_inside:                      # 先夹进"聊天界面"（消息列表矩形）
            x = max(int(keep_inside[0]) + 4,
                    min(x, int(keep_inside[2]) - self.width() - 4))
        x = max(int(screen_logical[0]) + 4,
                min(x, int(screen_logical[2]) - self.width() - 4))
        y = int(anchor.y() + anchor.height() + 6)
        if y + self.height() > int(screen_logical[3]) - 4:
            y = max(int(screen_logical[1]) + 4, int(anchor.y()) - self.height() - 6)
        self.setGeometry(QRect(x, y, self.width(), self.height()))

    # ---------------- 动画 ----------------
    def set_alpha(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._alpha) > 0.005:
            self._alpha = value
            self.update()

    def start_fade_out(self) -> None:
        """点过之后淡出关闭（用户口径）。"""
        if not self._fade.isActive():
            self._fade.start()

    def _step_fade(self) -> None:
        if self._alpha <= 0.0:
            self._fade.stop()
            self.hide()
            return
        self.set_alpha(self._alpha - 0.12)
        if self._alpha <= 0.0:
            self._fade.stop()
            self.hide()

    # ---------------- 交互/绘制 ----------------
    def mousePressEvent(self, event) -> None:      # noqa: N802
        if event.button() == Qt.LeftButton:
            logger.info("首次启动提示被点击 → 淡出关闭")
            self.clicked.emit()
            self.start_fade_out()

    def enterEvent(self, event) -> None:           # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:           # noqa: N802
        self._hover = False
        self.update()

    def paintEvent(self, event) -> None:           # noqa: N802
        t = theme_mod.theme(self.cfg)
        bg = t["panel_bg"]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setOpacity(self._alpha)
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1,
                            t.get("radius", 10) / 1.5, t.get("radius", 10) / 1.5)
        painter.fillPath(path, QColor(bg[0], bg[1], bg[2],
                                      245 if self._hover else 228))
        border = QColor(t.get("accent", "#58A6FF"))
        border.setAlpha(230 if self._hover else 170)
        painter.setPen(border)
        painter.drawPath(path)
        painter.setPen(QColor(t.get("title", "#E6EDF3")))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        metrics = QFontMetrics(QFont("Microsoft YaHei UI", 9))
        baseline = (self.height() + metrics.ascent() - metrics.descent()) // 2
        painter.drawText(self.PAD, baseline, self._text)
        painter.end()
