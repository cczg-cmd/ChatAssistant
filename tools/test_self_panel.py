#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：自我分析的面板/设置面板接线。

背景（真实事故）：`OverlayPanel._self_mode` 只在 show_analysis 里赋值，
但面板在 show_analysis 之前就会画一次/算标题宽度 → 抛
`AttributeError: 'OverlayPanel' object has no attribute '_self_mode'`
→ 用户看到的是"点哪个气泡都不显示分析"。这里把它钉住。

覆盖：
  A. 面板**在 show_analysis 之前**读 _danger_label/_band 不报错（旧 bug）；
  B. 自我分析结果 → 第三条字段名叫"质量评分"，低分=红/高分=绿（方向反了）；
  C. 选项窗口 set_self_mode 后标题取"发言选项"；
  D. 设置面板：载入 self_fields → 改 → 保存写回 cfg。

用法：python tools/test_self_panel.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import config as app_config  # noqa: E402
import ui.settings_dialog as sd  # noqa: E402
from core.analyzer import AnalysisResult  # noqa: E402
from core.message_reader import Message  # noqa: E402
from ui.overlay_panel import OverlayPanel, ReplyOptionsBar  # noqa: E402
from ui.overlay_panel import plan_panel_placement  # noqa: E402


def case_panel_before_show() -> bool:
    """旧 bug：没经过 show_analysis 就取第三个字段名 → AttributeError。"""
    cfg = app_config.Config()          # 用默认配置，避免受用户当前那套影响
    panel = OverlayPanel(cfg)
    try:
        label = panel._danger_label
        band = panel._band(3)
        ok = bool(label) and bool(band[0])
        print(f"A. show 之前取字段名/色带：{'对' if ok else '错'}（{label!r} / {band[0]!r}）")
        return ok
    except Exception as exc:
        print(f"A. show 之前取字段名/色带：错 → {type(exc).__name__}: {exc}")
        return False


def case_self_bands() -> bool:
    cfg = app_config.Config()          # 默认名字/默认色带
    panel = OverlayPanel(cfg)
    target = Message(msg_key="R", text="行", side="right", bbox=(0, 0, 200, 40))
    result = AnalysisResult(msg_key="R", json_ok=True, intent="答应看展", emotion="敷衍",
                            danger_level=0, suggestion="接得有点敷衍", self_analysis=True)
    panel.show_analysis(result, target)
    label_self = panel._danger_label
    # 必须在"自我分析还是当前模式"时取色带：切回对方模式后 _band 用的是另一套
    band_low, band_mid, band_high = panel._band(3), panel._band(6), panel._band(9)
    # 徽标只留"字段名 + 分数"（用户口径：不要"（干巴巴）"这种附加评价）
    badge_ok = "（" not in panel._badge and panel._badge.endswith("/10")
    panel.show_analysis(AnalysisResult(msg_key="L", json_ok=True, intent="问吃啥",
                                       emotion="好奇", danger_level=3),
                        Message(msg_key="L", text="吃啥", side="left", bbox=(0, 0, 200, 40)))
    label_other = panel._danger_label
    # 第 79 轮：自我分析第三个字段 = 魅力；色带 = 低灰 / 中绿 / 高粉
    colors = [band_low[1].name(), band_mid[1].name(), band_high[1].name()]
    ok = (label_self == "魅力" and label_other == "危险度"
          and band_low[0] == "干巴巴" and band_mid[0] == "有魅力" and band_high[0] == "很迷人"
          and colors == ["#8b949e", "#3fb950", "#ff9ecb"] and badge_ok)
    print(f"B. 自我分析字段名/色带：{'对' if ok else '错'}"
          f"（自己→{label_self!r}；三档={[(b[0], b[1].name()) for b in (band_low, band_mid, band_high)]}；"
          f"对方→{label_other!r}）")
    return ok


def case_options_title() -> bool:
    cfg = app_config.Config()
    bar = ReplyOptionsBar(cfg)
    bar.set_self_mode(True)
    name_self = app_config.field_name(cfg, "option", getattr(cfg, "self_fields", None),
                                      app_config.DEFAULT_SELF_FIELD_NAMES)
    bar.set_self_mode(False)
    name_other = app_config.field_name(cfg, "option")
    ok = name_self == "发言选项" and name_other == "回复选项" and bar._self_mode is False
    print(f"C. 选项窗口标题：{'对' if ok else '错'}（自己→{name_self!r} 对方→{name_other!r}）")
    return ok


def case_settings_roundtrip() -> bool:
    cfg = app_config.Config()
    dlg = sd.SettingsDialog(cfg)
    dlg.self_field_names["danger"].setText("魅力X")
    dlg.self_field_prompts["danger"].setPlainText("0-10，越高越好")
    dlg.self_field_names["option"].setText("发言选项X")
    dlg.self_style_edit.setPlainText("随便点评两句")
    written = {}

    def fake_save(c, path=None):          # 测试里绝不写盘
        written["cfg"] = c
        return Path("tmp/not_written.json")

    orig_save, orig_accept = sd.save_config, sd.SettingsDialog.accept
    sd.save_config = fake_save             # type: ignore[assignment]
    sd.SettingsDialog.accept = lambda self: None   # type: ignore[assignment]
    try:
        dlg.on_save()
    finally:
        sd.save_config = orig_save         # type: ignore[assignment]
        sd.SettingsDialog.accept = orig_accept     # type: ignore[assignment]
    saved = written.get("cfg")
    ok = (saved is not None
          and saved.self_fields.danger_name == "魅力X"
          and saved.self_fields.danger_prompt == "0-10，越高越好"
          and saved.self_fields.option_name == "发言选项X"
          and saved.self_analysis_style_prompt == "随便点评两句"
          # 意图/情绪沿用普通那套 → 不写 self_fields，保持空
          and saved.self_fields.intent_name == ""
          and saved.self_fields.intent_prompt == "")
    print(f"D. 设置面板读写：{'对' if ok else '错'}"
          f"（魅力名={getattr(saved.self_fields, 'danger_name', None)!r}）")
    return ok


def case_place_left_of_my_bubble() -> bool:
    """第 79 轮：点自己的消息 → 面板贴在自己气泡**左边**，并从右边展开（anchor=right）。"""
    cfg = app_config.load_config()
    window = [0, 0, 1400, 1000]
    list_box = [46, 120, 1354, 880]
    screen = [0, 0, 2560, 1440]
    other = Message(msg_key="L", text="对方说的", side="left", bbox=(60, 200, 560, 240))
    mine = Message(msg_key="R", text="我说的", side="right", bbox=(900, 400, 1340, 440))
    messages = [other, mine]
    panel_size = (300, 120)
    plan_mine = plan_panel_placement(mine, messages, list_box, window, screen,
                                     panel_size, cfg, scale=1.0)
    ok_mine = (plan_mine["anchor"] == "right"
               and abs(plan_mine["rect"][2] - (mine.bbox[0] - cfg.ui.panel_gap_px)) <= 2
               and plan_mine["rect"][0] >= list_box[0]
               and plan_mine["rect"][2] <= mine.bbox[0])
    plan_other = plan_panel_placement(other, messages, list_box, window, screen,
                                      panel_size, cfg, scale=1.0)
    ok_other = plan_other["anchor"] == "left"
    ok = ok_mine and ok_other
    print(f"E. 自己的消息贴左/右锚：{'对' if ok else '错'}"
          f"（自己 rect={plan_mine['rect']} 锚={plan_mine['anchor']}｜"
          f"对方 锚={plan_other['anchor']}）")
    return ok


def main() -> int:
    app = QApplication(sys.argv)          # noqa: F841
    results = [case_panel_before_show(), case_self_bands(),
               case_options_title(), case_settings_roundtrip(),
               case_place_left_of_my_bubble()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
