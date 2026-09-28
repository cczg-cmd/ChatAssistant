#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui/overlay_panel.py - 分析面板与三条回复选项（SPEC 4.3 / 5.6，B2-M6）

两个窗口分工：
  OverlayPanel     分析面板：恒穿透（WA_TransparentForMouseEvents），只读展示；
  ReplyOptionsBar  三条回复：**独立可交互小窗**（默认隐藏，悬停时出现，点完自动隐藏），
                   这样主面板保持穿透，同时"选项可点"也成立。

位置策略（SPEC 4.3，四级兜底，优先"同排右侧空白带"）：
  ① 目标消息附近"左侧内容列右边界 + gap"到"右侧内容列左边界 - gap"的空白带
  ② QQ 窗口外侧右边 ③ 窗口外侧左边 ④ 气泡下方（摘要态，允许轻微遮挡）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from config import DEFAULT_SELF_FIELD_NAMES, Config, field_name
from core.analyzer import AnalysisResult
from core.message_reader import Message
from utils import coordinate as coord
from utils import win32_api as w32
from . import theme as theme_mod

logger = logging.getLogger("ui.overlay_panel")

TITLE_SIZE_MAX = 11         # 标题基准字号（情绪 · 意图 同一行）
TITLE_SIZE_MIN = 8          # 缩到这个字号还放不下，才允许折成两行（仍然不省略）
TITLE_PAD = 24              # 标题左右各 12px 内边距


# --------------------------------------------------------------------------- #
# 位置计算（纯函数，便于单测）
# --------------------------------------------------------------------------- #
def plan_panel_placement(target: Message, messages: Sequence[Message],
                         list_box: Optional[Sequence[int]], window_rect: Sequence[int],
                         screen_rect: Sequence[int], panel_size: Tuple[int, int],
                         cfg: Config, scale: float = 1.0,
                         min_width: Optional[int] = None) -> Dict[str, Any]:
    """panel_size / 返回的 rect 都是**物理像素**；scale 用于把配置里的逻辑阈值换算成物理。

    坑（已修）：panel_min_width 是逻辑像素，早前直接和物理 available 比较，
    150% 缩放下等于放宽了 1/3，空白带只有 140 逻辑宽也会被接受 → 面板被压窄、
    标题放不下。现在统一按 scale 换算成物理再比。
    """
    width, height = panel_size
    bx0, by0, bx1, by1 = target.bbox
    center_y = (by0 + by1) // 2
    gap = cfg.ui.panel_gap_px
    bounds = list_box or window_rect
    min_band = int(min_width) if min_width else int(round(cfg.ui.panel_min_width * (scale or 1.0)))

    def clamp_y(rect: List[int]) -> List[int]:
        top = max(bounds[1], min(rect[1], bounds[3] - height))
        return [rect[0], top, rect[2], top + height]

    # 候选：(策略名, rect, 允许遮挡谁, 展开锚边) —— "none"=不许挡任何消息，
    #       "right_only"=只许挡住自己发的，"any"=兜底允许挡；
    #       锚边 "left"=宽度从左边向右展开（老行为），"right"=固定右缘向左展开（贴在气泡左边时用）。
    candidates: List[Tuple[str, List[int], str, str]] = []
    available = 0

    # ① 同排右侧空白带：只看"垂直方向与面板重叠"的消息，算左右两列边界 → 保证横向不压到任何消息
    #    （早期用"目标消息附近 ±200px"筛消息，实测会漏掉上方那张图片消息，导致面板横向压到它）
    panel_top = max(bounds[1], min(center_y - height // 2, bounds[3] - height))
    y0, y1 = panel_top, panel_top + height

    def _v_overlap(rect: Sequence[int]) -> bool:
        return not (rect[3] <= y0 or rect[1] >= y1)

    lefts = [m.bbox for m in messages if m.side == "left" and _v_overlap(m.bbox)]
    rights = [m.bbox for m in messages if m.side == "right" and _v_overlap(m.bbox)]
    left_col_right = max((r[2] for r in lefts), default=bounds[0])
    right_col_left = min((r[0] for r in rights), default=bounds[2])
    start = left_col_right + gap
    # ①a 第 79 轮（用户口径）：目标是**我自己发的消息** → 面板摆在气泡**左边**，
    #     右缘贴住气泡左缘，展开动画也从右侧开始（"从右边展开、从右边收回"）。
    #     旧逻辑一律"从左列右缘起算"：当同一条横带里没有对方消息时，left_col_right 会
    #     退化成消息列表左缘 → 面板跑到界面最左侧（用户反馈的就是这个）。
    if target.side == "right":
        anchor_right = (right_col_left if rights else target.bbox[0]) - gap
        start_left = max(bounds[0] + 8, anchor_right - width)
        if anchor_right - start_left >= min_band:
            candidates.append(("同排左侧空白（贴自己气泡）",
                               [start_left, panel_top, anchor_right, y1], "none", "right"))
    # 用户口径（第 22 轮）：**宁可压住自己发的消息，也不要跑到 QQ 窗口外侧**（外侧容易找不到）。
    # 所以同排右侧一共有两级：先试不压任何消息的宽度，不够就直接占到自己消息上面（右列）。
    clean_available = (min(right_col_left - gap, bounds[2] - 8)) - start
    full_available = (bounds[2] - 8) - start        # 允许压住右列（自己发的）气泡
    available = max(clean_available, 0)
    if full_available >= min_band:
        if clean_available >= min_band and min(width, clean_available) == min(width, full_available):
            use_w = min(width, clean_available)
            candidates.append(("同排右侧空白", [start, panel_top, start + use_w, y1],
                               "none", "left"))
        else:
            use_w = min(width, full_available)
            policy = "none" if use_w <= clean_available else "right_only"
            name = "同排右侧空白" if policy == "none" else "同排右侧(压住自己的消息)"
            candidates.append((name, [start, panel_top, start + use_w, y1], policy, "left"))
    elif clean_available >= min_band:
        use_w = min(width, clean_available)
        candidates.append(("同排右侧空白", [start, panel_top, start + use_w, y1],
                           "none", "left"))

    # 窗口外侧（默认关闭：用户反馈"容易找不到"；需要时把 ui.allow_outside_window 打开）
    if getattr(cfg.ui, "allow_outside_window", False):
        if screen_rect[2] - (window_rect[2] + gap) >= min_band:
            candidates.append(("窗口外侧右边",
                               [window_rect[2] + gap, center_y - height // 2,
                                window_rect[2] + gap + width, center_y - height // 2 + height],
                               "none", "left"))
        if (window_rect[0] - gap) - screen_rect[0] >= min_band:
            candidates.append(("窗口外侧左边",
                               [window_rect[0] - gap - width, center_y - height // 2,
                                window_rect[0] - gap, center_y - height // 2 + height],
                               "none", "right"))
    # 兜底：气泡下方（还在 QQ 窗口内，允许遮挡）
    below_x = min(max(int(bx0), int(screen_rect[0]) + 8),
                  int(screen_rect[2]) - width - 8)      # 水平也夹进屏幕（否则会被屏幕裁掉）
    candidates.append(("气泡下方", clamp_y([below_x, by1 + 8, below_x + width, by1 + 8 + height]),
                       "any", "left"))

    others = [m for m in messages if m.msg_key != target.msg_key]
    for strategy, rect, policy, anchor in candidates:
        inside_screen = (screen_rect[0] <= rect[0] and screen_rect[1] <= rect[1]
                         and rect[2] <= screen_rect[2] and rect[3] <= screen_rect[3])
        if not inside_screen:
            continue
        hit = [m for m in others if coord.intersects(rect, m.bbox)]
        if policy == "none" and hit:
            continue
        if policy == "right_only" and any(m.side != "right" for m in hit):
            continue                      # 只允许盖住"我"发的，绝不盖对方的消息
        return {"strategy": strategy, "rect": rect, "overlapped": [m.text for m in hit],
                "fallback": strategy == "气泡下方", "available": available,
                "min_band": min_band, "anchor": anchor}
    rect = candidates[-1][1]
    return {"strategy": "气泡下方", "rect": rect, "fallback": True,
            "overlapped": [m.text for m in others if coord.intersects(rect, m.bbox)],
            "available": available, "min_band": min_band,
            "anchor": candidates[-1][3]}


# --------------------------------------------------------------------------- #
# 分析面板（穿透、只读）
# --------------------------------------------------------------------------- #
class OverlayPanel(QWidget):
    def __init__(self, cfg: Config) -> None:
        super().__init__(None)
        self.cfg = cfg
        # 不用全局 TopMost：作为 QQ 窗口的 owned window 时"只在 QQ 之上"
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._lines: List[str] = []
        self._title = ""
        self._badge = ""
        self._badge_color = QColor("#8B949E")
        self._footer = ""
        self._hint = ""
        self._width = cfg.ui.panel_width
        self._content_width = cfg.ui.panel_width   # 内容"希望"的宽度（不被放置结果夹窄）
        self._height = cfg.ui.panel_height
        self._state = "empty"
        self._raw_body = ""
        self._raw_title = ""
        self._title_lines: List[str] = []
        self._title_size = TITLE_SIZE_MAX
        self._anim_alpha = 1.0          # 展开/收起动画的透明度系数（0=全透明）
        # 第 79 轮：是否"自我分析"（自己的消息）→ 决定第三个字段名与色带。
        # 必须在 __init__ 里给默认值：面板在 show_analysis 之前就会算标题宽度/画一次，
        # 那时读不到这个属性会抛 AttributeError（"点哪个气泡都不显示"就是这个原因）。
        self._self_mode = False
        self.resize(self._width, self._height)      # 先按配置宽度定尺寸，换行才准

    def set_anim_alpha(self, value: float) -> None:
        """展开/收起动画用：整体透明度系数（1=正常）。"""
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._anim_alpha) > 0.01:
            self._anim_alpha = value
            self.update()

    # ---------------- 内容 ----------------
    def _band(self, danger: Optional[int]) -> Tuple[str, QColor]:
        # 第 79 轮：分析自己发的消息时第三个字段是"质量评分"（越高越好）→ 用另一套色带
        bands = (getattr(self.cfg.ui, "self_danger_bands", None) if self._self_mode
                 else self.cfg.ui.danger_bands) or self.cfg.ui.danger_bands
        for band in bands:
            if danger is not None and danger <= band["max"]:
                return str(band["label"]), QColor(band["color"])
        return "未知", QColor("#8B949E")

    @property
    def _danger_label(self) -> str:
        """第三个字段的显示名（默认"危险度"，用户可在设置里改成别的，如"兴趣水平"）。"""
        if self._self_mode:
            return field_name(self.cfg, "danger",
                              getattr(self.cfg, "self_fields", None),
                              DEFAULT_SELF_FIELD_NAMES)
        return field_name(self.cfg, "danger")

    def show_analysis(self, result: AnalysisResult, target: Message,
                      backend_note: str = "", context_count: int = 0) -> None:
        self._self_mode = bool(getattr(result, "self_analysis", False))
        label, color = self._band(result.danger_level)
        self._raw_title = f"{result.emotion or '—'} · {result.intent or '—'}"
        self._set_content_width(self._raw_title)     # 先定宽 → 标题才有机会一行放下
        self._title = self._raw_title
        self._title_lines = self._fit_title(self._raw_title)
        danger = "?" if result.danger_level is None else result.danger_level
        # 第 79 轮（用户口径）：徽标只留"字段名 + 分数"，**不要再跟"（干巴巴）"这类附加评价**
        # —— 档位含义已经由颜色表达（灰/绿/粉），文字重复反而啰嗦。
        self._badge = f"{self._danger_label} {danger}/10"
        self._badge_color = color
        if result.partial:
            # 建议已跟意图一起生成；只有"还没拿到建议"时（异常路径）才用占位文案
            self._raw_body = result.suggestion or "选项生成中"
        else:
            self._raw_body = result.suggestion or "（无建议）"
        self._lines = self._wrap(self._raw_body)
        conf = "" if result.confidence is None else f" · 置信 {result.confidence}"
        ctx = f" · 上下文 {context_count} 条" if context_count else ""
        self._footer = f"{backend_note or 'UIA'}{ctx}{conf}"
        self._hint = ""      # 用户要求：删掉这行黄字（避免与正文重复）
        self._state = "ready"
        self._resize_to_content()

    # ---------------- 标题：情绪 · 意图 同一行、完整显示 ----------------
    @staticmethod
    def _title_font(size: int) -> QFont:
        font = QFont("Microsoft YaHei UI", size)
        font.setBold(True)
        return font

    def _title_need(self, title: str, size: int) -> int:
        """标题在指定字号下需要多宽（含左右内边距），单位=逻辑像素。"""
        return QFontMetrics(self._title_font(size)).horizontalAdvance(title) + TITLE_PAD

    def _desired_width(self, title: str) -> int:
        """按标题决定面板"希望"的宽度：短标题保持 panel_width，长标题最多加到 panel_max_width。"""
        low = max(160, int(self.cfg.ui.panel_width))
        high = max(low, int(getattr(self.cfg.ui, "panel_max_width", low) or low))
        need = self._title_need(title or "", TITLE_SIZE_MAX)
        return int(max(low, min(high, need)))

    def _set_content_width(self, title: str) -> None:
        """每次上屏都重算期望宽度 —— 修掉"宽度棘轮"：

        早前 _width 只在 __init__ 里设一次，被空白带夹窄后就再也不恢复，
        之后所有放置都按那个被压窄的宽度算 → 标题永远放不下（实测 320→140 逻辑）。
        """
        self._content_width = self._desired_width(title or "")
        self._width = self._content_width

    def _fit_title(self, title: str) -> List[str]:
        """情绪与意图**同一行、完整显示、不省略**。

        ① 先按 panel_max_width 加宽（由 _set_content_width / 放置结果决定）；
        ② 仍放不下就逐级缩小字号（11 → 10 → 9 → 8）；
        ③ 只有空白带窄到不足 panel_min_width 的兜底布局才折两行 —— 依然不省略。
        实测：真实 QQ 窗口的"同排右侧空白"= 414~449 逻辑像素，
        11pt 下 16 字标题（情绪+意图）只需 264px，所以正常情况都是 11pt 单行。
        """
        limit = max(80, self._width - TITLE_PAD)
        for size in range(TITLE_SIZE_MAX, TITLE_SIZE_MIN - 1, -1):
            if QFontMetrics(self._title_font(size)).horizontalAdvance(title) <= limit:
                self._title_size = size
                return [title]
        self._title_size = TITLE_SIZE_MIN
        head, sep, tail = title.partition(" · ")
        if sep and tail:
            return [head + " ·", tail]
        return [title]

    def show_analyzing(self, target: Message) -> None:
        self._title = "分析中…"
        self._set_content_width(self._title)
        self._title_lines = ["分析中…"]
        self._title_size = TITLE_SIZE_MAX
        self._raw_title = self._title
        self._badge = f"{self._danger_label} —"
        self._badge_color = QColor("#D29922")
        self._lines = self._wrap(target.text[:60] + ("…" if len(target.text) > 60 else ""))
        self._footer = ""
        self._hint = ""
        self._state = "analyzing"
        self._resize_to_content()

    def show_notice(self, title: str, body: str, hint: str = "") -> None:
        self._raw_title = title
        self._set_content_width(title)
        self._title = title
        self._title_lines = self._fit_title(title)
        self._badge = ""
        self._badge_color = QColor("#8B949E")
        self._lines = self._wrap(body)
        self._footer = ""
        self._hint = hint
        self._state = "notice"
        self._resize_to_content()

    def _wrap(self, text: str) -> List[str]:
        # 按**实际宽度**换行（面板在窄空白带里会被压窄，按固定宽度换行会显示不全）
        metrics = QFontMetrics(self._font(10))
        limit = max(80, self._width - 24)
        lines: List[str] = []
        current = ""
        for ch in text:
            if metrics.horizontalAdvance(current + ch) > limit and current:
                lines.append(current)
                current = ch
            else:
                current += ch
        if current:
            lines.append(current)
        # 正文最多 2 行（用户要求"建议压缩在两行以内"）；其余情况保留更多行
        return lines[:2] if self._state == "ready" and not self._hint else lines[:6]

    def rewrap(self) -> None:
        """面板尺寸变化后重新换行并调整高度（修复"显示不全"）。"""
        if not self._raw_body:
            return
        self._lines = self._wrap(self._raw_body)
        if self._raw_title:
            self._title_lines = self._fit_title(self._raw_title)
        self._resize_to_content()

    def _font(self, size: int, bold: bool = False) -> QFont:
        font = QFont("Microsoft YaHei UI", size)
        font.setBold(bold)
        return font

    def _elide(self, text: str, width_px: int, size: int = 10, bold: bool = False) -> str:
        """超宽就省略（修复"最右边被裁掉一点"：标题/底栏原来是单行直绘）。"""
        metrics = QFontMetrics(self._font(size, bold))
        return metrics.elidedText(text, Qt.ElideRight, max(40, width_px))

    def _resize_to_content(self) -> None:
        line_h = QFontMetrics(self._font(10)).height()
        # 版式：上边距12 + 标题(每行22) + 危险度行22 + 正文(每行 line_h + 4) + 底栏18 + 下边距8
        title_h = 22 * max(1, len(self._title_lines))          # 标题可能占两行
        height = (12 + title_h + 22 + len(self._lines) * line_h + 4
                  + 18 + (20 if self._hint else 0) + 8)
        self._height = max(self.cfg.ui.panel_height, height)
        self.resize(self._width, self._height)
        self.update()

    def size_hint(self) -> Tuple[int, int]:
        return (self._width, self._height)

    def content_size(self) -> Tuple[int, int]:
        """面板**希望**占的逻辑尺寸（宽由内容决定，不含上次被夹窄的结果）。

        main 用这个而不是 self.width() 去算放置方案，否则"被夹窄 → 下次更窄"会自我强化。
        """
        return (int(self._content_width), int(self._height))

    @property
    def has_content(self) -> bool:
        """面板里有没有实质内容（用于"自愈重新显示"时避免弹空面板）。"""
        return self._state in ("ready", "analyzing", "notice") and bool(
            self._title or self._lines)

    def move_to_logical(self, rect: QRect) -> None:
        """按 **Qt 逻辑坐标**就位（调用方负责把物理坐标换算成逻辑坐标）。

        注意：绝不能把物理宽度直接塞给 Qt 控件 —— 那样每帧会被再乘一次缩放，
        几帧后面板会膨胀到几万像素并让 Qt 建 DIB 失败直接闪退（实测踩过）。
        """
        width = max(140, min(900, int(rect.width())))          # 防御性夹紧
        if width != self.width():
            self._width = width
            self.resize(self._width, self.height())
            self.rewrap()                    # 宽度变了 → 重新换行，避免文字被裁
        height = max(int(rect.height()), self.height())
        height = max(60, min(1200, height))
        self.setGeometry(QRect(int(rect.x()), int(rect.y()), self._width, height))

    def set_owner(self, owner_hwnd: int) -> None:
        try:
            w32.set_owner_window(int(self.winId()), owner_hwnd)
        except Exception:
            pass

    # ---------------- 绘制 ----------------
    def paintEvent(self, event) -> None:      # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        t = theme_mod.theme(self.cfg)
        alpha = int(255 * self.cfg.ui.panel_opacity * self._anim_alpha)
        radius = int(t.get("radius", 10))
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, radius, radius)
        painter.fillPath(path, QColor(*t["panel_bg"], alpha))
        painter.setPen(QColor(*t["panel_border"], alpha))
        painter.drawPath(path)
        # galgame 主题：顶端一条粉紫渐变细条（"更像个对话窗"的小装饰）
        strip = t.get("strip")
        if strip:
            from PySide6.QtGui import QLinearGradient
            grad = QLinearGradient(0, 0, self.width(), 0)
            grad.setColorAt(0.0, QColor(strip[0]))
            grad.setColorAt(1.0, QColor(strip[1]))
            # 第 79 轮（用户："樱花风格的分析面板上那条横线装饰似乎有点视觉小瑕疵"）：
            # 旧画法是直接画一条 3px 的圆角小条**压在面板描边上** → 顶端那条边框线被盖住，
            # 两端还和面板圆角对不齐（看起来"缺了一小截/多出一小截"）。
            # 现在改成：裁剪到"面板内缘（描边以内）"，再画一条贴着内缘的横向渐变条 ——
            # 圆角处自然被裁掉，边框完整保留。
            radius = float(t.get("radius", 10)) - 1.0
            painter.save()
            inner = QPainterPath()
            inner.addRoundedRect(1.5, 1.5, self.width() - 3.0, self.height() - 3.0,
                                 max(1.0, radius), max(1.0, radius))
            painter.setClipPath(inner)
            painter.setPen(Qt.NoPen)
            painter.setBrush(grad)
            painter.drawRect(0.0, 1.0, float(self.width()), 2.6)
            painter.restore()

        y = 12
        painter.setFont(self._title_font(self._title_size))
        painter.setPen(QColor(t["title"]))
        for index, title_line in enumerate(self._title_lines or [self._title]):
            painter.drawText(12, y + 16 + index * 22, title_line)
        y += 24 + 22 * max(0, len(self._title_lines) - 1)   # 标题两行时下移 22px
        if self._badge:
            painter.setBrush(self._badge_color)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(12, y + 4, 8, 8, 2, 2)
            painter.setFont(self._font(9))
            painter.setPen(QColor(self._badge_color))
            painter.drawText(26, y + 12, self._badge)
        y += 22
        painter.setFont(self._font(10))
        painter.setPen(QColor(t["body"]))
        line_h = QFontMetrics(self._font(10)).height()
        for line in self._lines:
            painter.drawText(12, y + line_h - 4, line)
            y += line_h
        painter.setFont(self._font(7))
        painter.setPen(QColor(t["footer"]))
        footer_y = self.height() - (20 if not self._hint else 34)
        if self._footer:
            painter.drawText(12, footer_y,
                             self._elide(self._footer, self.width() - 24, 7))
        if self._hint:
            painter.setPen(QColor("#D29922"))
            painter.drawText(12, self.height() - 12, self._hint)
        painter.end()


# --------------------------------------------------------------------------- #
# 三条回复选项（可交互独立小窗）
# --------------------------------------------------------------------------- #
class ReplyOptionsBar(QWidget):
    chosen = Signal(int)
    refresh = Signal()                     # 点右上角刷新图标 → 重新生成三条回复

    ROW_H = 30                 # 单行高度；两行时自动变高
    PAD = 8
    HEADER_H = 22              # 顶部留一行给"刷新"图标
    LINE_H = 15

    def __init__(self, cfg: Config) -> None:
        super().__init__(None)
        self.cfg = cfg
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self._options: List[Dict[str, str]] = []
        self._hover = -1
        self._width = max(cfg.ui.panel_width, 260)
        self._anim_alpha = 1.0
        self._row_alpha: List[float] = []          # 每条选项的缓入进度（流式入屏用）
        self._refresh_hover = False
        self._refresh_rect = QRect(0, 0, 0, 0)
        self._self_mode = False            # 第 79 轮：自己消息的分析 → 标题用"发言选项"
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(30)
        self._fade_timer.timeout.connect(self._step_row_fade)

    def set_anim_alpha(self, value: float) -> None:
        """与分析面板同步的展开/收起透明度（0=完全透明）。"""
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._anim_alpha) > 0.01:
            self._anim_alpha = value
            self.update()

    def set_self_mode(self, value: bool) -> None:
        """切换"回复选项 / 发言选项"标题（自己的消息用后者）。"""
        value = bool(value)
        if value != self._self_mode:
            self._self_mode = value
            self.update()

    def set_options(self, options: List[Dict[str, str]]) -> None:
        self._options = options
        self._hover = -1
        # 新增的条目（流式逐条到达）从 0 开始缓入；已在屏上的保持当前进度
        while len(self._row_alpha) < len(options):
            self._row_alpha.append(0.0)
        self._row_alpha = self._row_alpha[:len(options)]
        if any(a < 0.999 for a in self._row_alpha) and not self._fade_timer.isActive():
            self._fade_timer.start()
        self._width = max(self.cfg.ui.panel_width, 260)
        self.resize(self._width, self._content_height())
        self.update()

    def _step_row_fade(self) -> None:
        """每条选项的缓入：每帧推进 25%，全部到 1 后停表。"""
        done = True
        for index, value in enumerate(self._row_alpha):
            if value < 0.999:
                self._row_alpha[index] = min(1.0, value + 0.25)
                done = False
        self.update()
        if done:
            self._fade_timer.stop()

    def show_placeholder(self, text: str = "选项生成中") -> None:
        """选项还没生成完时，在选项条位置显示一行占位（替代面板里的黄字提示）。"""
        self._options = [{"style": "", "text": text, "_placeholder": "1"}]
        self._hover = -1
        self._row_alpha = [0.0]
        if not self._fade_timer.isActive():
            self._fade_timer.start()
        self._width = max(self.cfg.ui.panel_width, 260)
        self.resize(self._width, self._content_height())
        self.update()

    def set_width(self, width: int) -> None:
        """跟随分析面板的实际宽度（面板会按标题宽度在 panel_width~panel_max_width 之间变宽）。"""
        width = max(240, min(900, int(width)))
        if width == self._width:
            return
        self._width = width
        self.resize(self._width, self._content_height())   # 宽度变了行数会变，高度跟着重算
        self.update()

    def is_placeholder(self) -> bool:
        return bool(self._options) and self._options[0].get("_placeholder")

    def option_text(self, index: int) -> str:
        """取第 index 条选项的正文（占位行/越界返回空串）。

        第 79 轮：流式生成途中点击选项要用**已经上屏的那一条**，而不是"已完成结果"里的
        （那时 self.results 里还只有第一段结果、replies 是空的 → 点击会被判越界而毫无反应）。
        """
        if self.is_placeholder() or index < 0 or index >= len(self._options):
            return ""
        return str(self._options[index].get("text") or "").strip()

    def _row_lines(self, option: Dict[str, str]) -> List[str]:
        """每条选项最多两行（回复文本 ≤50 字，两行够；避免过长被截断）。"""
        metrics = QFontMetrics(QFont("Microsoft YaHei UI", 9))
        style = option.get("style", "")
        indent = metrics.horizontalAdvance(f"[{style}]") + 8 if style else 0
        limit = max(60, self._width - 20 - indent)
        text = option.get("text", "")
        lines, current = [], ""
        for ch in text:
            if current and metrics.horizontalAdvance(current + ch) > limit:
                lines.append(current)
                current = ch
            else:
                current += ch
        if current:
            lines.append(current)
        return lines[:2]

    def _content_height(self) -> int:
        if not self._options:
            return self.PAD * 2 + self.HEADER_H + self.ROW_H
        total = 0
        for option in self._options:
            lines = self._row_lines(option)
            total += max(self.ROW_H, self.LINE_H * len(lines) + 16)
        return self.PAD * 2 + self.HEADER_H + total

    @classmethod
    def _row_baseline(cls, top: int, row_h: int, line_count: int) -> int:
        """行内**垂直居中**后第一行文字的基线。

        第 79 轮续（用户："选项文本……有点在选项方框偏上的位置，能不能改成在选项框正中高度的位置"）：
        行高 = 文字块高（15×行数）+ 上下各 8 的留白，所以居中等价于"上下各留 8"；
        基线再 +11（15px 的行格里给下伸部留 4px）—— 9pt 微软雅黑的字形中心约在基线上方 4px，
        于是文字块的视觉中心正好落在行中心（实测残差 ≤0.5px）。
        """
        return int(top) + (int(row_h) - cls.LINE_H * max(1, int(line_count))) // 2 + 11

    def move_to(self, panel_rect: Sequence[int], screen_rect: Sequence[int]) -> None:
        """贴在面板下方；出屏则贴面板上方。"""
        height = self.height()
        top = int(panel_rect[3]) + 6
        if top + height > screen_rect[3]:
            top = max(screen_rect[1], int(panel_rect[1]) - 6 - height)
        left = min(int(panel_rect[0]), screen_rect[2] - self._width)
        left = max(screen_rect[0], left)
        self.setGeometry(QRect(left, top, self._width, height))

    def set_owner(self, owner_hwnd: int) -> None:
        try:
            w32.set_owner_window(int(self.winId()), owner_hwnd)
        except Exception:
            pass

    # ---------------- 交互 ----------------
    def _row_at(self, y: int) -> int:
        if self.is_placeholder():
            return -1                    # 占位行不可点
        offset = int(y) - self.PAD - self.HEADER_H
        for index, option in enumerate(self._options):
            height = max(self.ROW_H, self.LINE_H * len(self._row_lines(option)) + 16)
            if 0 <= offset < height:
                return index
            offset -= height
        return -1

    def mouseMoveEvent(self, event) -> None:      # noqa: N802
        inside = self._refresh_rect.contains(event.position().toPoint())
        if inside != self._refresh_hover:
            self._refresh_hover = inside
            self.update()
        self.setCursor(Qt.PointingHandCursor if inside else Qt.ArrowCursor)
        row = self._row_at(int(event.position().y()))
        if row != self._hover:
            self._hover = row
            self.update()

    def leaveEvent(self, event) -> None:          # noqa: N802
        self._hover = -1
        self.update()

    def mousePressEvent(self, event) -> None:     # noqa: N802
        if (event.button() == Qt.LeftButton
                and self._refresh_rect.contains(event.position().toPoint())):
            logger.info("点击刷新图标 → 重新生成三条回复")
            self.refresh.emit()
            return
        row = self._row_at(int(event.position().y()))
        if row >= 0 and event.button() == Qt.LeftButton:
            logger.info("选择第 %d 条回复：%s", row + 1,
                        self._options[row].get("text", "")[:24])
            self.chosen.emit(row)

    def paintEvent(self, event) -> None:          # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        t = theme_mod.theme(self.cfg)
        alpha = int(255 * self.cfg.ui.panel_opacity)
        alpha = int(alpha * self._anim_alpha)
        radius = int(t.get("radius", 10))
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, radius, radius)
        painter.fillPath(path, QColor(*t["panel_bg"], alpha))
        painter.setPen(QColor(*t["panel_border"], alpha))
        painter.drawPath(path)

        # 顶栏：右上角的"刷新"图标（两个主题各自配色）
        icon_size = 13
        icon_x = self.width() - self.PAD - icon_size - 2
        icon_y = self.PAD + 1
        self._refresh_rect = QRect(icon_x - 3, icon_y - 3, icon_size + 8, icon_size + 8)
        acc = t.get("accent", "#58A6FF")
        painter.setPen(QColor(acc) if not self._refresh_hover else QColor("#FFFFFF"))
        pen = painter.pen()
        pen.setWidthF(1.6)
        painter.setPen(pen)
        arc = QRect(icon_x, icon_y, icon_size, icon_size)
        painter.drawArc(arc, 40 * 16, 280 * 16)          # 环形箭头
        painter.drawLine(icon_x + icon_size - 5, icon_y - 1,
                         icon_x + icon_size - 5, icon_y + 4)   # 箭头
        painter.drawLine(icon_x + icon_size - 5, icon_y - 1,
                         icon_x + icon_size - 10, icon_y - 1)
        painter.setFont(QFont("Microsoft YaHei UI", 7))
        painter.setPen(QColor(t["footer"]))
        painter.drawText(self.PAD, self.PAD + 11,
                         field_name(self.cfg, "option",
                                    getattr(self.cfg, "self_fields", None) if self._self_mode else None,
                                    DEFAULT_SELF_FIELD_NAMES if self._self_mode else None))

        metrics = QFontMetrics(QFont("Microsoft YaHei UI", 9))
        top = self.PAD + self.HEADER_H
        for index, option in enumerate(self._options):
            lines = self._row_lines(option)
            row_h = max(self.ROW_H, self.LINE_H * len(lines) + 16)
            # 每条选项自己的缓入进度（流式逐条到达时不会"啪"地弹出）
            row_alpha = self._row_alpha[index] if index < len(self._row_alpha) else 1.0
            if row_alpha < 0.999:
                painter.save()
                painter.setOpacity(max(0.05, row_alpha))
            if option.get("_placeholder"):
                painter.setFont(QFont("Microsoft YaHei UI", 9))
                painter.setPen(QColor("#8B949E"))
                painter.drawText(10, self._row_baseline(top, row_h, len(lines)),
                                 option.get("text", ""))
                top += row_h
                if row_alpha < 0.999:          # 占位行也要把 save() 配平，
                    painter.restore()          # 否则 Qt 刷 "Painter ended with 1 saved states"
                continue
            if index == self._hover:
                painter.fillRect(4, top, self.width() - 8, row_h,
                                 QColor(*t["hover"]))
            painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
            painter.setPen(QColor(t["accent"]))
            style_text = option.get("style", "")
            style = f"[{style_text}]" if style_text else ""      # 前缀可在设置里关掉
            # 第 79 轮续（用户："选项文本和圆点在观感上有点在选项方框偏上的位置，能不能改成
            # 在选项框正中高度的位置"）：文字块（1 行或折成 2 行）整体在**行内垂直居中**。
            # 原来固定从 `top + 14` 起画，而一行行高 31 → 文字中心比行中心高约 5px（看着偏上）。
            base_y = self._row_baseline(top, row_h, len(lines))
            if style:
                painter.drawText(10, base_y, style)
            style_w = metrics.horizontalAdvance(style) + (6 if style else 0)
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            painter.setPen(QColor(t["title"]))
            for line_index, line in enumerate(lines):
                x = 10 + (style_w if line_index == 0 else 0)
                painter.drawText(x, base_y + line_index * self.LINE_H, line)
            # 小圆点 = **鼠标悬停指示**（第 79 轮，用户口径纠正："小圆点我是想鼠标放在选项上就显示"）：
            # 直接复用"鼠标放到选项上就高亮"的同一套 `_hover` 状态，不另设"已选"状态 ——
            # 鼠标在哪一条上，那一条最右侧就亮一个主题强调色的点。（原来那套"点过就记住"的
            # 实现口径理解错了：要收起再展开才看得到，实测就是"没显示出来"。）
            # 纵向对齐**第一行文字的垂直中心**（基线 top+14 上方 4px）：按整行高度的中心画，
            # 两行选项那一条会低 13px（用户："圆点位置是不是有点往下偏移了"）。
            # 第 79 轮续（同上）：文字块改成行内居中后，圆点也改成画在**选项框的正中高度**
            # （`top + row_h/2`），与文字块的视觉中心一致（实测偏差 ≤0.5px）。
            if index == self._hover and not option.get("_placeholder"):
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(t["accent"]))
                dot_r = 3
                cy = top + row_h // 2
                painter.drawEllipse(self.width() - 12 - dot_r,
                                    cy - dot_r, dot_r * 2, dot_r * 2)
            top += row_h
            if row_alpha < 0.999:
                painter.restore()
        painter.end()
