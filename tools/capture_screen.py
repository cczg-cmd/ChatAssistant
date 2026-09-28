#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓全屏（桌面复制，含我们自己的窗口），用于验证设置面板等 UI。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spikes"))

import numpy as np  # noqa: E402
import spike_wgc as wgc  # noqa: E402
from PIL import Image  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tmp" / "screen.png"
    wgc.set_dpi_awareness()
    dup = wgc.DxgiDuplicator()
    dup.open()
    try:
        shot, info = dup.grab(timeout_ms=1500)
    finally:
        dup.close()
    if shot is None:
        print("取帧失败:", info)
        return 1
    rgb = shot[:, :, [2, 1, 0]] if shot.ndim == 3 else shot
    Image.fromarray(np.ascontiguousarray(rgb)).save(out)
    print("已保存", out, rgb.shape[1], "x", rgb.shape[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
