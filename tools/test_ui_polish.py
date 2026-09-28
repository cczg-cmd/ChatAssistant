#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：界面收尾四项。

  A. 内置配置"被补过新字段"时，**空值/默认文本要从代码版本补齐**
     （用户："猫娘默认配置自我分析部分还是不带喵" —— 根因是存档被标记 edited=True 且
     自我那几项是空的/存的是全局默认文本，于是新版本永远生效不了）；
  B. 默认界面风格 = 樱花（galgame）；
  C. 樱花主题那条顶部渐变装饰能正常绘制（裁剪到内缘，不再压住描边），且能截图留证；
  D. 小圆点跟随"鼠标悬停"（复用选项高亮的同一套 `_hover`）；
  E. 设置面板最小化后再点状态灯 → 调 showNormal() 还原（而不是毫无反应）；
  G. 抓屏量像素：悬停那一条的**文字与圆点都在选项框正中高度**，鼠标移开圆点消失。

用法：python tools/test_ui_polish.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config as app_config  # noqa: E402
import main as app_main  # noqa: E402
import ui.theme as theme_mod  # noqa: E402
from core.analyzer import AnalysisResult  # noqa: E402
from core.message_reader import Message  # noqa: E402
from ui.overlay_panel import OverlayPanel, ReplyOptionsBar  # noqa: E402


def case_builtin_profile_refill() -> bool:
    """A：模拟"老存档"（edited=True、自我几项为空、语气存的是全局默认文本）→ 加载后应补上喵。"""
    cfg = app_config.load_config()
    cat = app_config.find_profile(cfg, app_config.CATGIRL_PROFILE_NAME)
    assert cat is not None
    # 造一份"老存档"：把自我那几项清空、语气写成全局默认文本，并标 edited
    stored = [dict(entry) for entry in (cfg.fields_profiles or [])]
    for entry in stored:
        if str(entry.get("name")) == app_config.CATGIRL_PROFILE_NAME:
            entry["edited"] = True
            entry["self_danger_prompt"] = ""
            entry["self_suggestion_prompt"] = ""
            entry["self_option_prompt"] = ""
            entry["self_style_prompt"] = app_config.DEFAULT_SELF_STYLE_PROMPT
    cfg.fields_profiles = stored
    tmp_path = ROOT / "tmp" / "test_ui_polish_config.json"
    app_config.save_config(cfg, tmp_path)
    back = app_config.load_config(tmp_path)
    entry = app_config.find_profile(back, app_config.CATGIRL_PROFILE_NAME)
    try:
        tmp_path.unlink()
    except OSError:
        pass
    ok = (entry is not None
          and "喵" in str(entry.get("self_suggestion_prompt") or "")
          and "喵" in str(entry.get("self_option_prompt") or "")
          and "喵" in str(entry.get("self_style_prompt") or ""))
    print(f"A 老存档的内置配置会被补齐（含喵）：{'对' if ok else '错'}"
          f"（风格={str(entry.get('self_style_prompt'))[:14]!r}）")
    return ok


def case_default_theme() -> bool:
    ok = app_config.Config().ui.theme == "galgame"
    print(f"B 默认界面风格=樱花：{'对' if ok else '错'}"
          f"（{app_config.Config().ui.theme!r}）")
    return ok


def case_galgame_strip_paint() -> bool:
    """C：樱花面板画一遍（含顶部渐变装饰）不报错，并落一张图便于肉眼确认。"""
    cfg = app_config.Config()
    cfg.ui.theme = "galgame"
    panel = OverlayPanel(cfg)
    target = Message(msg_key="L", text="在吗", side="left", bbox=(0, 0, 200, 40))
    panel.show_analysis(AnalysisResult(msg_key="L", json_ok=True, intent="问在不在",
                                       emotion="好奇", danger_level=2,
                                       suggestion="他大概想找你聊两句"), target)
    out = ROOT / "tmp" / "galgame_panel_strip.png"
    try:
        pixmap = panel.grab()          # 真正跑一遍 paintEvent（含 strip 绘制）
        pixmap.save(str(out))
        ok = (not pixmap.isNull()) and out.exists()
    except Exception as exc:
        print(f"   （绘制失败：{type(exc).__name__}: {exc}）")
        ok = False
    print(f"C 樱花面板顶部装饰绘制正常：{'对' if ok else '错'}（截图 {out.name}）")
    return ok


def _hover_move(bar: ReplyOptionsBar, y: int) -> None:
    """造一个真实的鼠标移动事件 → 走 `mouseMoveEvent`（与"悬停高亮"完全同一套逻辑）。"""
    pos = QPointF(30.0, float(y))
    event = QMouseEvent(QEvent.MouseMove, pos, bar.mapToGlobal(pos),
                        Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    bar.mouseMoveEvent(event)


def case_hover_dot_logic() -> bool:
    """D：小圆点跟随"鼠标悬停"（复用 `_hover` 那一套，不另设"已选"状态）。"""
    cfg = app_config.Config()
    bar = ReplyOptionsBar(cfg)
    bar.set_options([{"style": "认同", "text": "好呀"}, {"style": "反调", "text": "算了"},
                     {"style": "吐槽", "text": "离谱"}])
    row_step = max(ReplyOptionsBar.ROW_H, ReplyOptionsBar.LINE_H + 16)     # 一行选项的行高
    top = ReplyOptionsBar.PAD + ReplyOptionsBar.HEADER_H
    _hover_move(bar, top + row_step + 5)                       # 落在第 2 条上
    ok_second = bar._hover == 1
    _hover_move(bar, top + 5)                                  # 挪到第 1 条
    ok_first = bar._hover == 0
    bar.leaveEvent(None)                                       # 鼠标移开 → 不再有圆点
    ok_left = bar._hover == -1
    bar.show_placeholder("选项生成中")                          # 占位行不可点、也不画圆点
    ok_placeholder = (bar._row_at(top + 5) == -1 and bar.is_placeholder())
    ok = ok_second and ok_first and ok_left and ok_placeholder
    print(f"D 小圆点跟随鼠标悬停（同一套 _hover 逻辑）：{'对' if ok else '错'}"
          f"（第2条={ok_second} 第1条={ok_first} 移开={ok_left} 占位行={ok_placeholder}）")
    return ok




def case_hover_dot_pixels() -> bool:
    """G：抓屏量像素 —— 悬停那一条的**文字与圆点都在选项框正中高度**，移开后圆点消失。"""
    cfg = app_config.Config()
    cfg.ui.theme = "galgame"
    bar = ReplyOptionsBar(cfg)
    long_text = "周六两点行，我提前去占个好位置，到时候在门口等你，别迟到啊，真的别迟到啊喂"
    bar.set_options([{"style": "认同", "text": "好呀"},
                     {"style": "反调", "text": long_text},      # 折两行 → 行高 46（最挑对齐）
                     {"style": "吐槽", "text": "离谱"}])
    bar._row_alpha = [1.0] * 3              # 跳过缓入（否则整行透明度会把圆点冲淡，测不准）
    bar._fade_timer.stop()
    bar.resize(460, 190)
    bar.show()
    top = ReplyOptionsBar.PAD + ReplyOptionsBar.HEADER_H
    row1_h = max(ReplyOptionsBar.ROW_H, ReplyOptionsBar.LINE_H + 16)          # 31
    hover_top = top + row1_h                                                  # 第 2 条顶部
    row_h = ReplyOptionsBar.LINE_H * 2 + 16                                   # 两行 → 46
    _hover_move(bar, hover_top + 5)
    QApplication.processEvents()
    accent = QColor(theme_mod.theme(cfg)["accent"])
    dot, text_ys = _scan_accent(bar, accent, hover_top, row_h)
    if not dot or not text_ys:
        print(f"G 悬停圆点的位置/范围：错（圆点={bool(dot)} 选项文字={bool(text_ys)}）")
        return False
    scale = bar.grab().toImage().width() / max(1, bar.width())
    dot_cy = (min(dot) + max(dot)) / 2 / scale
    text_cy = (min(text_ys) + max(text_ys)) / 2 / scale
    row_center = hover_top + row_h / 2.0                                      # 选项框正中高度
    dot_off = dot_cy - row_center
    text_off = text_cy - row_center
    inside = min(dot) >= int(hover_top * scale) and max(dot) <= int((hover_top + row_h) * scale)
    bar.leaveEvent(None)                    # 鼠标移开 → 圆点必须消失
    QApplication.processEvents()
    dot_after, _ = _scan_accent(bar, accent, hover_top, row_h)
    ok = abs(dot_off) <= 1.5 and abs(text_off) <= 1.5 and inside and not dot_after
    print(f"G 悬停圆点位置与范围：{'对' if ok else '错'}"
          f"（文字偏离框中心 {text_off:+.1f}px／圆点偏离 {dot_off:+.1f}px／"
          f"落在悬停行内={inside}／移开后消失={not dot_after}）")
    return ok


def _scan_accent(bar: ReplyOptionsBar, accent, hover_top: int, row_h: int):
    """抓屏找两样东西：右边缘的圆点（y 列表）、悬停行里**选项文字**的 y 列表（整块）。"""
    image = bar.grab().toImage()
    scale = image.width() / max(1, bar.width())
    top = ReplyOptionsBar.PAD + ReplyOptionsBar.HEADER_H
    dot_ys, bright_ys = [], set()
    for y in range(int((top - 2) * scale), image.height()):      # 从第一行起（排除顶栏刷新图标）
        for x in range(image.width()):
            pix = image.pixelColor(x, y)
            if (abs(pix.red() - accent.red()) < 26 and abs(pix.green() - accent.green()) < 34
                    and abs(pix.blue() - accent.blue()) < 34):
                if x > image.width() - int(40 * scale):           # 右边缘 = 小圆点
                    dot_ys.append(y)
            elif (pix.alpha() > 200 and int(10 * scale) <= x < int(240 * scale)
                    and pix.red() + pix.green() + pix.blue() > 3 * 150):
                if int(hover_top * scale) <= y < int((hover_top + row_h) * scale):
                    bright_ys.add(y)                              # 悬停行里的正文（两行都算）
    return dot_ys, sorted(bright_ys)


class FakeDialog:
    def __init__(self) -> None:
        self.calls: list = []

    def isVisible(self) -> bool:
        return True

    def isMinimized(self) -> bool:
        return True

    def showNormal(self) -> None:
        self.calls.append("showNormal")

    def raise_(self) -> None:
        self.calls.append("raise")

    def activateWindow(self) -> None:
        self.calls.append("activate")


class StubApp:
    def __init__(self, dialog) -> None:
        self.settings_dialog = dialog


def case_settings_restore() -> bool:
    """E：设置面板最小化后再点状态灯 → showNormal()+raise_+activateWindow。"""
    dialog = FakeDialog()
    app_main.ChatAssistantApp.open_settings(StubApp(dialog))
    ok = dialog.calls == ["showNormal", "raise", "activate"]
    print(f"E 最小化后能呼出设置面板：{'对' if ok else '错'}（{dialog.calls}）")
    return ok


def main() -> int:
    app = QApplication(sys.argv)          # noqa: F841
    results = [case_builtin_profile_refill(), case_default_theme(),
               case_galgame_strip_paint(), case_hover_dot_logic(),
               case_settings_restore(), case_hover_dot_pixels()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
