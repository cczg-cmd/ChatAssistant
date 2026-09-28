#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一量"读剪贴板"到底多贵（主线程 Qt / 工作线程 Qt / 原生 Win32），
用来定位第 63 轮剪贴板还原有没有拖慢"点选项 → 文本进输入框"。

用法：python tmp/measure_clip_cost.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

from PySide6.QtCore import QThread  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


class Probe(QThread):
    def run(self) -> None:
        clip = QApplication.clipboard()
        samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            text = clip.text()
            samples.append((time.perf_counter() - t0) * 1000)
        print("  工作线程 Qt clip.text()："
              + "、".join(f"{s:.1f}ms" for s in samples)
              + f"（读到 {len(text or '')} 字）")


def main() -> int:
    app = QApplication(sys.argv)          # noqa: F841
    clip = QApplication.clipboard()
    print("① 主线程 Qt clip.text()")
    for _ in range(5):
        t0 = time.perf_counter()
        text = clip.text()
        print(f"   {(time.perf_counter() - t0) * 1000:.1f}ms（{len(text or '')} 字）")
    print("② 原生 Win32 clip_read_text()")
    for _ in range(5):
        t0 = time.perf_counter()
        text = w32.clip_read_text()
        print(f"   {(time.perf_counter() - t0) * 1000:.1f}ms（{len(text or '')} 字）")
    print("③ 原生 Win32 clip_write_text()")
    for _ in range(3):
        t0 = time.perf_counter()
        w32.clip_write_text("chatassistant-clip-cost-probe")
        print(f"   {(time.perf_counter() - t0) * 1000:.1f}ms")
    print("④ 工作线程里调 Qt 剪贴板（第 63 轮的实际情形）")
    probe = Probe()
    probe.start()
    probe.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
