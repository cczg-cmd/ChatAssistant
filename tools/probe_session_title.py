#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取证：QQ 窗口标题（= 会话身份的来源）是否会随"切换聊天"变化。

用法：python tmp/probe_session_title.py [秒数]   （默认 40 秒，每 0.4s 采一次）
输出：tmp/probe_session_title_out.txt —— 只在标题变化时打印一行。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from utils import win32_api as w32  # noqa: E402


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    cfg = app_config.load_config()
    out = ROOT / "tmp" / "probe_session_title_out.txt"
    lines = []
    last = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        win = w32.find_qq_chat_window(cfg.uia.window_class, cfg.uia.exe_name)
        if win is None:
            cur = "<未找到窗口>"
        else:
            cur = (f"title={win.title!r} session={win.session_id!r}")
        if cur != last:
            line = f"{time.time() - t0:6.2f}s  {cur}"
            lines.append(line)
            print(line, flush=True)
            last = cur
        time.sleep(0.4)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n共 {len(lines)} 次变化，已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
