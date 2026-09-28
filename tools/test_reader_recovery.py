#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 70 轮回归：读不到时（不可用）→ 之后同一个窗口里打开会话，要能**很快**恢复。

做法：连续读 4 次，打印每次耗时与状态；再在 QQ 里打开一个会话（用户手动），
再读一次应立刻 ready（不再等 30s 闸门）。也用于观察"重探用短超时"后的响应速度。

用法：python tmp/test_reader_recovery.py [次数]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    reader = MessageReader(app_config.load_config())
    for index in range(rounds):
        t0 = time.perf_counter()
        messages, snapshot = reader.read()
        elapsed = time.perf_counter() - t0
        window = snapshot.get("window") or {}
        list_box = (snapshot.get("uia") or {}).get("list_box")
        print(f"第{index + 1}次：{elapsed:5.2f}s｜状态={snapshot.get('status'):12s}"
              f"｜消息 {len(messages):2d} 条｜list_box={list_box}"
              f"｜窗口={window.get('title')!r}")
        time.sleep(0.5)
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
