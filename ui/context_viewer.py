#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui/context_viewer.py - 状态灯左侧的"上下文历史"入口 + 查看面板（B2 第 49 轮）

两件东西：
  ContextButton  —— 贴在状态灯左侧的小图标（跟主题配色），点一下开/关下面的面板；
  ContextViewer  —— 显示当前**累积记录的上下文历史池**（● 表示会进当前 prompt，
                     ○ 表示只在池子里、暂不参与）。

安全边界：都只是"显示我们已经读到的内容"，不操作 QQ、不改窗口属性、不改任何输入。
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from config import Config
from ui import theme as theme_mod


class ContextButton(QWidget):
    """状态灯左侧的小图标：画一个"清单"图案，点一下开/关上下文历史面板。"""

    def __init__(self, cfg: Config, on_click: Optional[Callable[[], None]] = None) -> None:
        super().__init__(None)
        self.cfg = cfg
        self.on_click = on_click
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self._hover = False
        self._size = cfg.ui.dot_size_px
        self.setToolTip("查看上下文历史（已记录的消息池）")
        self.resize(self._size, self._size)

    def apply_config(self) -> None:
        self._size = self.cfg.ui.dot_size_px
        self.resize(self._size, self._size)
        self.update()

    def enterEvent(self, event) -> None:          # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:          # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:     # noqa: N802
        if event.button() == Qt.LeftButton and self.on_click is not None:
            self.on_click()

    def paintEvent(self, event) -> None:          # noqa: N802
        t = theme_mod.theme(self.cfg)
        alpha = int(255 * self.cfg.ui.panel_opacity)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        radius = max(6, self._size // 4)
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, radius, radius)
        painter.fillPath(path, QColor(*t["panel_bg"], alpha))
        painter.setPen(QColor(*t["panel_border"], alpha))
        painter.drawPath(path)
        # 图案：三行"清单"（像上下文列表）；悬停时用强调色
        color = QColor("#FFFFFF") if self._hover else QColor(t.get("accent", "#58A6FF"))
        painter.setPen(QPen(color, max(1.4, self._size / 14.0)))
        left = self.width() * 0.28
        right = self.width() * 0.72
        for index, ratio in enumerate((0.34, 0.5, 0.66)):
            width = right if index != 2 else self.width() * 0.58
            painter.drawLine(int(left), int(self.height() * ratio),
                             int(width), int(self.height() * ratio))
        painter.end()


class ContextViewer(QWidget):
    """上下文历史面板（状态灯左侧小图标开/关；第 66 轮起默认展开）。

    第 66 轮改动（用户口径）：
      · 位置改到 **QQ 窗口右侧**（原来是贴在小图标下方）；
      · 展开/收起用**与分析面板同一套动画**（宽度从 4px 展开 + 透明度渐显，30ms×6 帧）；
      · 删掉底部那行小字提示（"…｜Esc 关闭"）——用户按 Esc 会连带把 QQ 会话窗口关掉，
        这行提示属于误导。
    """

    PAD = 10

    def __init__(self, cfg: Config) -> None:
        super().__init__(None)
        self.cfg = cfg
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._full_w = 360
        self._full_h = 420
        self._pref_w = 360               # "想要的宽度"：不随可用空间缩小（否则会越用越窄）
        self._pref_h = 420               # 同理：高度也不能被"最小化恢复"那一瞬间压扁
        self._anim = 1.0                 # 0=收起 1=展开（与分析面板同一口径）
        self.resize(self._full_w, self._full_h)
        self._title = "上下文历史"
        self._body = ""
        self._edit = QPlainTextEdit(self)
        self._edit.setReadOnly(True)
        self._edit.setFrameStyle(0)
        self._edit.setStyleSheet("background: transparent; color: #E6EDF3; border: none;")
        self._edit.setFont(QFont("Microsoft YaHei UI", 9))

    def set_history(self, title: str, body: str, keep_scroll: bool = False) -> None:
        self._title = title
        self._body = body
        # 实时刷新时保留用户的滚动位置（否则每 0.6s 会被拉回底部，没法往上翻）
        scrollbar = self._edit.verticalScrollBar()
        position = scrollbar.value() if keep_scroll else scrollbar.maximum()
        self._edit.setPlainText(body)
        scrollbar.setValue(max(0, min(position, scrollbar.maximum())))
        self.update()

    # ---------------- 展开/收起动画（与分析面板一致的宽度+透明度） ----------------
    def full_width(self) -> int:
        return self._full_w

    def full_height(self) -> int:
        return self._full_h

    MIN_W = 240          # 太窄会看不清（用户反馈"面板被设得太小"）→ 低于这个就换位置

    def set_size(self, width: int, height: int) -> None:
        """设定"展开到底"的尺寸。`width` 会被夹到 [MIN_W, 期望宽度]。

        第 68b 轮修复（用户反馈"变成窄面板就回不去了""从最小化打开后又变小了"）：
        旧实现是 `_full_w = max(MIN_W, min(self._full_w, avail))` —— 拿**当前**宽度当上限，
        于是每窄一次就再也回不去（棘轮）。现在期望宽度单独存 `_pref_w`（固定 360），
        实际宽度 = clamp(期望宽度, MIN_W, 可用宽度)，空间恢复后自动变回 360。
        """
        self._full_w = max(self.MIN_W, min(int(self._pref_w), int(width)))
        # 高度同样不能"越用越矮"：QQ 最小化/恢复的瞬间窗口会很矮，旧实现把那一瞬的高度
        # 记成上限 → 恢复后还是压扁。现在期望高度固定（420），实际高度按可用空间夹取。
        self._full_h = max(160, min(int(self._pref_h), int(height)))

    def reset_size(self) -> None:
        """回到默认尺寸（旧位置/窗口右侧放不下时用）。"""
        self._pref_w = 360
        self._pref_h = 420
        self._full_w, self._full_h = 360, 420
        self.set_anim(self._anim)

    def set_anim(self, value: float) -> None:
        """透明度 + 宽度一起走（与 OverlayPanel.set_anim_alpha 同款效果）。"""
        self._anim = max(0.0, min(1.0, float(value)))
        width = max(4, int(self._full_w * self._anim))
        if width != self.width() or self._full_h != self.height():
            self.resize(width, self._full_h)
        self.update()

    def resizeEvent(self, event) -> None:         # noqa: N802
        # 底部原来那行提示已删除 → 文本区直接到底
        self._edit.setGeometry(self.PAD, 34, max(8, self.width() - self.PAD * 2),
                              max(8, self.height() - 34 - 6))

    def move_to(self, anchor: Sequence[int], screen_rect: Sequence[int]) -> None:
        """贴在小图标下方；出屏则往上/往左收。（保留给旧调用点，主路径见 place_right_of）"""
        width, height = self.width(), self.height()
        left = int(anchor[0]) - width + int(anchor[2] - anchor[0])
        top = int(anchor[1]) + int(anchor[3] - anchor[1]) + 6
        if left + width > screen_rect[2]:
            left = screen_rect[2] - width - 4
        if left < screen_rect[0]:
            left = screen_rect[0] + 4
        if top + height > screen_rect[3]:
            top = max(screen_rect[1] + 4, int(anchor[1]) - height - 6)
        self.setGeometry(QRect(left, top, width, height))

    def place_right_of(self, qq_rect: Sequence[int], screen_rect: Sequence[int],
                           gap: int = 6, top_offset: int = 0) -> bool:
        """放到 QQ 窗口**右侧**的空白区；右侧放不下就返回 False（调用方改走旧位置）。

        用户口径（第 68 轮）："当右侧没位置时请放到旧版那个位置，不然会遮挡 QQ 窗口 UI" ——
        所以这里**不再**退到窗口内侧，而是直接放弃右侧放置，让调用方用 `move_to()`
        （贴在小图标下方）。判据：右侧可用宽度 ≥ `MIN_W`。
        只改"位置 + 展开尺寸"，当前宽度仍由动画值决定（`set_anim()`）。
        `qq_rect` / `screen_rect` 都是**逻辑坐标**（调用方用 `_logical_rect()` 换算好）。
        """
        qq = [int(v) for v in qq_rect]
        screen = [int(v) for v in screen_rect]
        left = qq[2] + gap
        avail = screen[2] - left - 4
        if avail < self.MIN_W:
            return False                     # 右侧没位置 → 交给调用方走旧位置
        width = max(self.MIN_W, min(self._pref_w, avail))
        # 上边与"聊天面板白色区域"齐平（用户口径）：由调用方给 top_offset（逻辑像素）
        top = max(screen[1] + 4, qq[1] + int(top_offset))
        height = max(160, min(self._pref_h, qq[3] - top - 8, screen[3] - top - 8))
        self.set_size(width, height)
        self.setGeometry(QRect(left, top, max(4, int(self._full_w * self._anim)), self._full_h))
        return True

    def keyPressEvent(self, event) -> None:       # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.hide()

    def paintEvent(self, event) -> None:          # noqa: N802
        t = theme_mod.theme(self.cfg)
        # 第 66 轮：透明度也乘展开动画值，跟分析面板的 set_anim_alpha 一致
        alpha = int(255 * min(0.96, self.cfg.ui.panel_opacity + 0.06) * self._anim)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        radius = int(t.get("radius", 10))
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, radius, radius)
        painter.fillPath(path, QColor(*t["panel_bg"], alpha))
        painter.setPen(QColor(*t["panel_border"], alpha))
        painter.drawPath(path)
        strip = t.get("strip")
        if strip:
            from PySide6.QtGui import QLinearGradient
            gradient = QLinearGradient(0, 0, self.width(), 0)
            gradient.setColorAt(0.0, QColor(strip[0]))
            gradient.setColorAt(1.0, QColor(strip[1]))
            painter.setPen(Qt.NoPen)
            painter.setBrush(gradient)
            painter.drawRoundedRect(1, 1, self.width() - 2, 3, 1.5, 1.5)
        painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
        painter.setPen(QColor(t["title"]))
        # 标题太长时用省略号（原来直接 drawText 会被面板右缘**裁掉半截**，
        # 用户反馈"右侧字被裁掉一点"）
        metrics = painter.fontMetrics()
        title = metrics.elidedText(self._title, Qt.ElideRight,
                                   max(20, self.width() - self.PAD * 2))
        painter.drawText(self.PAD, 22, title)
        # 第 66 轮：底部不再画 "★/●/○ + Esc 关闭" 那行提示（用户按 Esc 会关掉 QQ 会话窗口）
        painter.end()
