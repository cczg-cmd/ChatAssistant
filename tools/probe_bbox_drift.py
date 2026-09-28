#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：连续读取同一条消息的 bbox，看坐标是否在漂移。

动机：实机验收时鼠标**不动**，程序却在相邻 4 条消息间来回切换目标
（15:09:57 → 15:10:06 的日志）。如果每次读取的 bbox 都在小幅变化，
悬停命中就会在相邻消息之间抖动 → 用户看到"把相邻几条当成同一条/同一个面板"。
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
    for index in range(8):
        messages, snapshot = reader.read()
        rows = " | ".join(f"{m.text[:8]}:y{m.bbox[1]}-{m.bbox[3]}"
                          for m in messages if m.side == "left" and not m.is_image)
        print(f"第{index + 1}次 读到 {len(messages)} 条｜{rows}")
        time.sleep(1.0)
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
