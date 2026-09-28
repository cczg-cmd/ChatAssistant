#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 69 轮回归：设置面板的"用户画像"接口 —— 读/写闭环 + 进提示词 + 默认空。

用法：python tmp/test_user_profile.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import config as app_config  # noqa: E402
import ui.settings_dialog as settings_mod  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)                      # noqa: F841
    cfg = app_config.load_config()
    results = []

    # ① 默认是空的，而且**不进提示词**
    cfg.fields.user_profile = ""
    prompt_empty = app_config.build_system_prompt(cfg)
    print("① 默认空：不进提示词 =", "说话习惯" not in prompt_empty)
    results.append("说话习惯" not in prompt_empty)

    # ② 填了之后进提示词（带"我自己"的说明）
    cfg.fields.user_profile = "我说话很短，爱用“确实/笑死”，句尾偶尔加“捏”。"
    prompt_filled = app_config.build_system_prompt(cfg)
    has_block = "【我（用户）的说话习惯" in prompt_filled and "确实/笑死" in prompt_filled
    print(f"② 填了之后进提示词 = {has_block}（+{len(prompt_filled) - len(prompt_empty)} 字）")
    results.append(has_block)

    # ③ 设置面板读/写闭环（把 save_config 换成 no-op，别动真实 config.json）
    saved: dict = {}
    real_save = settings_mod.save_config
    settings_mod.save_config = lambda c, *a, **k: saved.setdefault("cfg", c)
    try:
        dlg = SettingsDialog(cfg)
        dlg._load_into_widgets()
        loaded_ok = dlg.profile_edit.toPlainText().startswith("我说话很短")
        dlg.profile_edit.setPlainText("我习惯说“不不不”，很少用感叹号。")
        dlg.on_save()
        written_ok = cfg.fields.user_profile == "我习惯说“不不不”，很少用感叹号。"
        print(f"③ 面板读取 = {loaded_ok}｜面板保存回 cfg = {written_ok}")
        results += [loaded_ok, written_ok]
        # ④ 清空后应该又不进提示词
        cfg.fields.user_profile = ""
        print("④ 清空后不进提示词 =", "说话习惯" not in app_config.build_system_prompt(cfg))
        results.append("说话习惯" not in app_config.build_system_prompt(cfg))
    finally:
        settings_mod.save_config = real_save

    print(f"\n{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
