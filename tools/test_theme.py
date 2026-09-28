#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离屏渲染两套主题的面板与选项条，肉眼确认 galgame 风格 + 标题仍单行完整。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from core.analyzer import AnalysisResult, ReplyOption  # noqa: E402
from core.message_reader import Message  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from ui.overlay_panel import OverlayPanel, ReplyOptionsBar  # noqa: E402

EMOTION, INTENT = "好奇", "询问明天下午是否有水课"


def main() -> int:
    app = QApplication.instance() or QApplication([])   # noqa: F841
    cfg = config.load_config()
    target = Message(msg_key="t", text=INTENT, side="left", bbox=(0, 0, 300, 40))
    result = AnalysisResult(
        msg_key="t", intent=INTENT, emotion=EMOTION, danger_level=2,
        suggestion="照实回答就行，他只是在确认情况。", confidence=0.8,
        replies=[ReplyOption(style="稳当", text="明天下午没有，我看了课表。"),
                 ReplyOption(style="简单", text="没有水课，满课。"),
                 ReplyOption(style="俏皮", text="想摸鱼？明天可没这运气喵。")])
    for name in ("classic", "galgame"):
        cfg.ui.theme = name
        panel = OverlayPanel(cfg)
        panel.show_analysis(result, target, backend_note="本地4b", context_count=6)
        panel.grab().save(str(ROOT / "tmp" / f"theme_panel_{name}.png"))
        bar = ReplyOptionsBar(cfg)
        bar.set_options([{"style": r.style, "text": r.text} for r in result.replies])
        bar.set_width(panel.width())
        bar.grab().save(str(ROOT / "tmp" / f"theme_options_{name}.png"))
        print(f"{name}: 面板 {panel.width()}x{panel.height()}｜标题行数 "
              f"{len(panel._title_lines)}｜字号 {panel._title_size}pt｜"
              f"主题名 {config.__dict__.get('x') or name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
