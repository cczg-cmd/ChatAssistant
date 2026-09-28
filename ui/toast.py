#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui/toast.py - 启动/状态小提示（短暂浮现后自动消失）

用途：应用启动时提示"ChatAssistant 已启动"，模型加载完成时提示"模型已就绪"。
特点：无边框、鼠标穿透、不抢焦点、无 owner（独立顶层），2 秒后自动淡出。
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from config import Config


class ToastWindow(QWidget):
    def __init__(self, cfg: Config) -> None:
        super().__init__(None)
        self.cfg = cfg
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._text = ""
        self._sub = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.resize(320, 64)

    def show_toast(self, text: str, sub: str = "", near: Optional[Sequence[int]] = None,
                   duration_ms: int = 2200) -> None:
        self._text, self._sub = text, sub
        metrics = QFontMetrics(QFont("Microsoft YaHei UI", 10, QFont.Bold))
        width = max(240, min(520, metrics.horizontalAdvance(text) + 40))
        self.resize(width, 62 if sub else 44)
        if near:
            x = int(near[0])
            y = max(0, int(near[1]) - self.height() - 10)
            self.setGeometry(QRect(x, y, self.width(), self.height()))
        else:
            screen = self.screen().availableGeometry()
            self.setGeometry(QRect(screen.center().x() - self.width() // 2,
                                   screen.top() + 80, self.width(), self.height()))
        self.show()
        self.raise_()
        self._timer.start(duration_ms)
        self.update()

    def paintEvent(self, event) -> None:      # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, 10, 10)
        painter.fillPath(path, QColor(22, 27, 34, 235))
        painter.setPen(QColor(88, 166, 255, 200))
        painter.drawPath(path)
        painter.setPen(QColor("#E6EDF3"))
        painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Bold))
        painter.drawText(16, 26, self._text)
        if self._sub:
            painter.setPen(QColor("#8B949E"))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.drawText(16, 48, self._sub)
        painter.end()
