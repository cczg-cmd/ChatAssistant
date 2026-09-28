#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 80 轮末回归：「设置模板」按钮 —— 用户能自己改"复制模板"复制出去的那份说明。

用户口径："在设置模板中「复制模板」按钮那行加一个按钮「设置模板」，点开这个按钮跳出一个
文本窗口，可以自己在里面修改复制模板，窗口下面一个保存按钮，一个取消按钮。"

覆盖：
  A. 没自定义时 → 生效模板 = 内置默认模板（`build_template()`）
  B. 自定义后 → 生效模板 = 自定义文本；清空 → 回到内置默认
  C. 设置面板那一行确实有「设置模板」按钮，「复制模板」复制的是**生效模板**
  D. 「设置模板」窗口：保存（accept）会把内容写进 cfg.copy_template 并落盘；
     取消（reject）什么都不改

用法：python tools/test_template_edit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import config as app_config  # noqa: E402
from config import Config  # noqa: E402
from core.prompt_import import build_template, effective_template  # noqa: E402
from ui import settings_dialog as sd  # noqa: E402

CUSTOM = "我的自定义模板：{字段名[显示名]：内容}\n我的要求："


def case_default() -> bool:
    cfg = Config()
    ok = (cfg.copy_template == "" and effective_template(cfg) == build_template()
          and effective_template(None) == build_template())
    print(f"A 未自定义时用内置模板：{'对' if ok else '错'}")
    return ok


def case_custom() -> bool:
    cfg = Config()
    cfg.copy_template = CUSTOM
    ok1 = effective_template(cfg) == CUSTOM
    cfg.copy_template = "   "            # 只有空白 = 等于没自定义
    ok2 = effective_template(cfg) == build_template()
    print(f"B 自定义 / 清空回默认：{'对' if ok1 and ok2 else '错'}"
          f"（自定义={ok1} 空白回默认={ok2}）")
    return ok1 and ok2


def case_button_and_clipboard() -> bool:
    cfg = Config()
    cfg.copy_template = CUSTOM
    dlg = sd.SettingsDialog(cfg)
    has_btn = (dlg.edit_tpl_btn.text() == "设置模板"
               and dlg.copy_tpl_btn.text() == "复制模板")
    dlg._copy_import_template()
    clip_ok = QApplication.clipboard().text() == CUSTOM
    ok = has_btn and clip_ok
    print(f"C 按钮存在 + 复制的是生效模板：{'对' if ok else '错'}"
          f"（按钮={has_btn} 剪贴板={clip_ok}）")
    return ok


def case_editor_save_cancel() -> bool:
    """保存 = 写进 cfg.copy_template 并落盘；取消 = 什么都不改。"""
    saved: list = []

    unset = object()

    class _FakeDialog:
        # 下次"窗口里最终停留的文本"（None = 用构造时传进来的当前模板）
        next_text = unset

        def __init__(self, parent=None, current="", is_custom=False) -> None:
            self.current, self.is_custom, self.text = current, is_custom, current

        def exec(self):
            return _FakeDialog.result

        def template_text(self) -> str:
            body = (_FakeDialog.next_text if _FakeDialog.next_text is not unset
                    else self.text)
            return str(body)

    orig_dialog, orig_save = sd.TemplateEditDialog, sd.save_config
    cfg = Config()
    try:
        sd.TemplateEditDialog = _FakeDialog                    # type: ignore[assignment]
        sd.save_config = lambda c: saved.append(dict(copy_template=c.copy_template))  # type: ignore
        dlg = sd.SettingsDialog(cfg)

        # ① 取消：不动 cfg、不落盘
        _FakeDialog.result = QDialog.Rejected
        dlg._edit_import_template()
        cancel_ok = (cfg.copy_template == "" and not saved)

        # ② 保存：写进 cfg 并落盘
        _FakeDialog.result = QDialog.Accepted
        dlg._edit_import_template()          # 内部 stub 的当前文本 = cfg 的生效模板（内置默认）
        save_ok = (saved and saved[-1]["copy_template"] == build_template())
        # ③ 保存自定义文本
        _FakeDialog.next_text = CUSTOM
        dlg._edit_import_template()
        custom_ok = (cfg.copy_template == CUSTOM
                     and saved[-1]["copy_template"] == CUSTOM
                     and effective_template(cfg) == CUSTOM)
        # ④ 保存空文本 = 回到内置默认
        _FakeDialog.next_text = ""
        dlg._edit_import_template()
        back_ok = (cfg.copy_template == "" and effective_template(cfg) == build_template())
    finally:
        sd.TemplateEditDialog, sd.save_config = orig_dialog, orig_save
    ok = cancel_ok and save_ok and custom_ok and back_ok
    print(f"D 设置模板窗口：保存/取消语义：{'对' if ok else '错'}"
          f"（取消={cancel_ok} 保存默认={save_ok} 保存自定义={custom_ok} 清空回默认={back_ok}）")
    return ok


def main() -> int:
    app = QApplication(sys.argv)          # noqa: F841
    results = [case_default(), case_custom(), case_button_and_clipboard(),
               case_editor_save_cancel()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
