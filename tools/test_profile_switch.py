#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：设置面板切换配置时，**不能把上一个配置的改动丢掉/覆盖**。

真实反馈："用户切换配置时原先的配置会被切换到的配置覆盖"。
原因：`_on_profile_selected()` 被 `currentRowChanged` 触发时，列表选中项**已经变成新的**，
而界面里装的还是**上一个配置**的内容 —— 旧代码既不写回上一个，也没有"写回指定配置"的能力，
一旦任何写回动作发生（比如切换后再保存），界面里的内容就会落到错误的那套配置上；
即使不写回，切走再切回来也会看到"我的改动没了"。

修法：切换前先调用 `_store_widgets_into(离开的那套)`（按名字找，而不是按当前选中行）。

本测试直接在对话框上操作列表/输入框（不调用 on_save，**不写盘**）。

用法：python tools/test_profile_switch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import config as app_config  # noqa: E402
from ui import settings_dialog as sd  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)          # noqa: F841
    cfg = app_config.load_config()
    dlg = sd.SettingsDialog(cfg)
    names = [str(entry.get("name")) for entry in dlg._profiles]
    if len(names) < 2:
        print("内置配置不足两套，跳过")
        return 0
    name_a, name_b = names[0], names[1]

    # A：改动第一套配置的"意图"字段
    dlg.profile_list.setCurrentRow(names.index(name_a))
    dlg.field_names["intent"].setText("A名字")
    dlg.field_prompts["intent"].setPlainText("A提示词")

    # 切到 B（这一步以前会把 A 的改动丢掉）
    dlg.profile_list.setCurrentRow(names.index(name_b))
    b_intent = dlg.field_prompts["intent"].toPlainText()

    # 切回 A：改动必须还在
    dlg.profile_list.setCurrentRow(names.index(name_a))
    a_name = dlg.field_names["intent"].text()
    a_prompt = dlg.field_prompts["intent"].toPlainText()

    entry_a = dlg._profile_by_name(name_a)
    entry_b = dlg._profile_by_name(name_b)
    ok_keep = (a_name == "A名字" and a_prompt == "A提示词")
    ok_not_leak = (str(entry_b.get("intent_name") or "") != "A名字"
                   and str(entry_b.get("intent_prompt") or "") != "A提示词"
                   and b_intent != "A提示词")
    ok_entry = (str(entry_a.get("intent_name") or "") == "A名字"
                and str(entry_a.get("intent_prompt") or "") == "A提示词")
    print(f"A 切回来改动还在：{'对' if ok_keep else '错'}（{a_name!r} / {a_prompt!r}）")
    print(f"B 写回的是离开的那套：{'对' if ok_entry else '错'}"
          f"（{name_a}.intent_prompt={str(entry_a.get('intent_prompt'))[:12]!r}）")
    print(f"C 没有把改动串进另一套配置：{'对' if ok_not_leak else '错'}"
          f"（{name_b}.intent_name={str(entry_b.get('intent_name'))[:12]!r}）")

    # D：新建配置要以"默认配置"为模板，而不是继承当前界面里那套（第 79 轮用户口径）
    dlg.profile_list.setCurrentRow(names.index(name_b))          # 站到猫娘那套上
    dlg.field_prompts["intent"].setPlainText("猫娘专用意图说明")
    default_entry = dlg._profile_by_name(sd.DEFAULT_PROFILE_NAME)
    saved_get_text = sd.QInputDialog.getText
    sd.QInputDialog.getText = staticmethod(lambda *a, **k: ("测试新建", True))   # type: ignore
    try:
        dlg._on_new_profile()
    finally:
        sd.QInputDialog.getText = saved_get_text                 # type: ignore
    new_entry = dlg._profile_by_name("测试新建")
    ok_template = (new_entry is not None
                   and str(new_entry.get("intent_prompt") or "")
                   == str((default_entry or {}).get("intent_prompt") or "")
                   and str(new_entry.get("intent_prompt") or "") != "猫娘专用意图说明")
    print(f"D 新建配置以“默认配置”为模板：{'对' if ok_template else '错'}"
          f"（新建.intent_prompt={str((new_entry or {}).get('intent_prompt'))[:12]!r}，"
          f"默认配置={str((default_entry or {}).get('intent_prompt'))[:12]!r}）")

    # E：自我分析那三项 + 两份整体风格也按配置隔离（用户："不同配置的全局分析风格会互相串"）
    dlg.profile_list.setCurrentRow(names.index(name_a))
    dlg.prompt_edit.setPlainText("A的全局风格")
    dlg.self_style_edit.setPlainText("A的自我风格")
    dlg.self_field_prompts["danger"].setPlainText("A的魅力说明")
    dlg.profile_list.setCurrentRow(names.index(name_b))     # 切走 → 先写回 A
    b_style = dlg.prompt_edit.toPlainText()
    dlg.profile_list.setCurrentRow(names.index(name_a))     # 切回 A
    entry_a2 = dlg._profile_by_name(name_a)
    ok_isolated = (dlg.prompt_edit.toPlainText() == "A的全局风格"
                   and dlg.self_style_edit.toPlainText() == "A的自我风格"
                   and dlg.self_field_prompts["danger"].toPlainText() == "A的魅力说明"
                   and str(entry_a2.get("style_prompt")) == "A的全局风格"
                   and str((dlg._profile_by_name(name_b) or {}).get("style_prompt") or "")
                   != "A的全局风格")
    print(f"E 自我分析字段+全局风格按配置隔离：{'对' if ok_isolated else '错'}"
          f"（切到B时风格={b_style[:10]!r}）")

    # F：重命名（导入出来的配置以前改不了名字）
    custom_name = "测试新建" if dlg._profile_by_name("测试新建") is not None else name_a
    dlg.profile_list.setCurrentRow(
        [str(e.get("name")) for e in dlg._profiles].index(custom_name))
    # 内置配置走"不能重命名"分支时会弹 QMessageBox（模态）→ 测试里必须垫掉，否则卡死
    saved_box_info, saved_box_q = sd.QMessageBox.information, sd.QMessageBox.question
    sd.QMessageBox.information = staticmethod(lambda *a, **k: None)      # type: ignore
    sd.QMessageBox.question = staticmethod(lambda *a, **k: None)         # type: ignore
    saved_get_text2 = sd.QInputDialog.getText
    sd.QInputDialog.getText = staticmethod(lambda *a, **k: ("改名后的配置", True))   # type: ignore
    try:
        dlg._on_rename_profile()
    finally:
        sd.QInputDialog.getText = saved_get_text2                                    # type: ignore
    renamed = dlg._profile_by_name("改名后的配置")
    # 内置的不允许改名（避免把"默认配置/猫娘配置"这两个代码认的名字改掉）
    names_now = [str(e.get("name")) for e in dlg._profiles]
    # 常量从 config 取（settings_dialog 里没用到的 import 已在第 80 轮清理掉）
    dlg.profile_list.setCurrentRow(names_now.index(app_config.CATGIRL_PROFILE_NAME)
                                   if app_config.CATGIRL_PROFILE_NAME in names_now else 0)
    sd.QInputDialog.getText = staticmethod(lambda *a, **k: ("乱改的名字", True))     # type: ignore
    try:
        dlg._on_rename_profile()
    finally:
        sd.QInputDialog.getText = saved_get_text2                                    # type: ignore
    sd.QMessageBox.information, sd.QMessageBox.question = saved_box_info, saved_box_q
    ok_rename = (renamed is not None and dlg._profile_by_name("乱改的名字") is None)
    print(f"F 配置可重命名（内置的挡掉）：{'对' if ok_rename else '错'}"
          f"（改名后存在={renamed is not None}）")

    results = [ok_keep, ok_entry, ok_not_leak, ok_template, ok_isolated, ok_rename]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
