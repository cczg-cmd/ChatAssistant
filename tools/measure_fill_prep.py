#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量"填入准备动作"的耗时（只读，不点击、不粘贴、不改前台）：
  · UiaBackend.find_input_box()  —— 定位输入框
  · win32_api.clip_read_text()   —— 存剪贴板快照
这两个动作在第 64 轮被挪进"选项条收起动画"的窗口里跑（并行），所以它们的耗时不再计到
用户感知里。这里量出来是为了证明"隐藏掉的时间"有多少。

用法：python tmp/measure_fill_prep.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402


def main() -> int:
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    window = None
    for _ in range(6):
        messages, snapshot = reader.read()
        window = snapshot.get("window")
        if messages and window:
            break
        time.sleep(1.0)
    if not window:
        print("读不到 QQ 窗口")
        return 1
    rect = tuple(window["rect"])
    print(f"QQ 窗口 rect={rect}")
    find_times, clip_times = [], []
    for _ in range(5):
        t0 = time.perf_counter()
        box = reader.uia.find_input_box(rect)
        find_times.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        text = w32.clip_read_text()
        clip_times.append((time.perf_counter() - t0) * 1000)
    print(f"find_input_box：{'、'.join(f'{t:.1f}ms' for t in find_times)}（输入框 {box}）")
    print(f"clip_read_text：{'、'.join(f'{t:.1f}ms' for t in clip_times)}"
          f"（读到 {len(text or '')} 字）")
    print(f"准备动作合计 ≈ {sum(find_times) / 5 + sum(clip_times) / 5:.1f}ms"
          "（这部分现在与收起动画并行，不再额外等待）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
