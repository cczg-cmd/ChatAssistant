#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 63 轮加固的验证：① 粘贴前前台复核 ② 日志脱敏开关 ③ 剪贴板回读。

用法：python tmp/test_hardening63.py
（不会真的往任何窗口发按键：第 ① 项故意用错的前台句柄，只验证"不发按键 + 退化为复制"。）
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core import analyzer as analyzer_mod  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message  # noqa: E402

_cuda = analyzer_mod._cuda_bin_dirs(app_config.load_config())
for _d in _cuda:
    try:
        os.add_dll_directory(_d)
    except Exception:
        pass
os.environ["PATH"] = ";".join(_cuda) + ";" + os.environ.get("PATH", "")


def main() -> int:
    print("① 粘贴前前台复核（故意给错句柄，期望：不发按键、退化为复制）")
    marker = "CHATASSISTANT_GUARD_TEST"
    ok, note = w32.fill_text_into_foreground(marker, expect_hwnd=1)
    clip = w32.clip_read_text() if hasattr(w32, "clip_read_text") else None
    print(f"   返回 {ok}｜说明 {note}")
    print(f"   剪贴板里是否留下了待粘贴文本：{'是' if clip == marker else '否/无法读取'}")
    passed_guard = (ok is False and "前台" in note)
    print(f"   [{'PASS' if passed_guard else 'FAIL'}] 前台不是目标窗口时绝不发按键")

    print("\n② 日志脱敏开关（log_message_text=False）")
    cfg = app_config.load_config()
    cfg.log_message_text = False
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(name)s %(message)s")
    analyzer = Analyzer(cfg)
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("   模型加载失败:", analyzer.last_error)
        return 1
    target = Message(msg_key="h1", text="这是一条不该出现在日志里的原文", side="left",
                     bbox=(0, 0, 300, 40))
    context = [target]
    analyzer.analyze_quick(target, context, use_cache=False)
    analyzer.close()
    print("   ↑ 上面日志里若出现 `<N 字，已脱敏>` 且没有 intent 原文＝通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
