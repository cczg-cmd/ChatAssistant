#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验证（离屏渲染，不碰 QQ）：
  1) 情绪 · 意图 是否同一行、字号多少、有没有省略号；
  2) 宽度棘轮是否修掉（被窄空白带夹窄后，再上屏能否恢复期望宽度）；
  3) 选项条是否跟随面板宽度。
输出 PNG 到 tmp/ 供人眼确认。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config  # noqa: E402
from core.analyzer import AnalysisResult, ReplyOption  # noqa: E402
from core.message_reader import Message  # noqa: E402
from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from ui.overlay_panel import OverlayPanel, ReplyOptionsBar  # noqa: E402

CASES = [
    ("最长实测标题", "平淡", "询问明天下午是否有水课"),
    ("加到上限", "好奇", "询问对方电脑是否已经清理过灰尘了"),
    ("短标题", "开心", "夸赞"),
]


def make_result(emotion: str, intent: str) -> AnalysisResult:
    return AnalysisResult(
        msg_key="k1", intent=intent, emotion=emotion, danger_level=2,
        suggestion="照实回答就行，他只是在确认情况，别过度解读。", confidence=0.8,
        replies=[ReplyOption(style="稳当", text="昨天下午没有，我看了课表，明天两节都是正课。"),
                 ReplyOption(style="简单", text="没有水课，明天下午满课。"),
                 ReplyOption(style="轻松", text="想摸鱼啊？明天下午没这门运气，满课走起。")],
    )


def describe(panel: OverlayPanel) -> str:
    lines = panel._title_lines
    text = "".join(lines)
    elided = "…" in text or "..." in text
    return (f"行数={len(lines)} 字号={panel._title_size}pt 宽={panel._width} "
            f"期望宽={panel._content_width} 省略={'有(!)' if elided else '无'} "
            f"内容={lines!r}")


def main() -> int:
    app = QApplication.instance() or QApplication([])   # noqa: F841
    cfg = config.load_config()
    print(f"配置：panel_width={cfg.ui.panel_width} panel_max_width={cfg.ui.panel_max_width} "
          f"panel_min_width={cfg.ui.panel_min_width}")
    panel = OverlayPanel(cfg)
    options = ReplyOptionsBar(cfg)
    target = Message(msg_key="k1", text="明天下午有水课吗", side="left",
                     bbox=(485, 561, 722, 596))
    out = ROOT / "tmp"
    wrong = 0

    for index, (name, emotion, intent) in enumerate(CASES):
        panel.show_analysis(make_result(emotion, intent), target,
                            backend_note="UIA读取·本地7B", context_count=6)
        print(f"\n[{name}] {emotion} · {intent}")
        print(f"  ① 正常上屏：{describe(panel)} 高={panel._height}")
        if "…" in "".join(panel._title_lines):
            wrong += 1
        panel.grab().save(str(out / f"panel_title_{index}.png"))

        # ② 模拟"空白带只有 200 逻辑宽"（会被夹窄）→ 应该缩字号，仍然单行不省略
        panel.move_to_logical(QRect(300, 200, 200, panel._height))
        print(f"  ② 被夹到200宽：{describe(panel)}")
        if "…" in "".join(panel._title_lines):
            wrong += 1

        # ③ 模拟"极端窄带 150 逻辑" → 允许折两行，依然不省略
        panel.move_to_logical(QRect(300, 200, 150, panel._height))
        print(f"  ③ 被夹到150宽：{describe(panel)}")
        if "…" in "".join(panel._title_lines):
            wrong += 1

        # ④ 棘轮回归测试：再上屏一次，宽度必须回到期望值
        panel.show_analysis(make_result(emotion, intent), target,
                            backend_note="UIA读取·本地7B", context_count=6)
        print(f"  ④ 再次上屏（棘轮回归）：{describe(panel)}")
        if panel._width != panel._content_width:
            print("     ! 宽度没有恢复")
            wrong += 1

    # 选项条跟随宽度
    options.set_options([{"style": "稳当", "text": "昨天下午没有，我看了课表，明天两节都是正课。"},
                         {"style": "简单", "text": "没有水课，明天下午满课。"},
                         {"style": "轻松", "text": "想摸鱼啊？明天下午没这门运气，满课走起。"}])
    options.set_width(320)
    before = (options.width(), options.height())
    options.set_width(460)
    print(f"\n选项条跟随宽度：320 → {before}，460 → {(options.width(), options.height())}")
    options.grab().save(str(out / "options_bar_460.png"))
    options.set_width(320)
    options.grab().save(str(out / "options_bar_320.png"))
    for index, option in enumerate(options._options):
        lines = options._row_lines(option)
        print(f"  选项{index + 1}：{len(lines)} 行 {lines!r}")

    print(f"\n失败项={wrong}（0 = 全部通过）")
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
