#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量"QQ 聊天区白色面板的顶边"在哪（用于把上下文面板上边与之对齐）。

做法：先用 tmp/capture_screen.py 截全屏，再在聊天区中间那一列自上而下扫，
找第一段"连续 80px 都是近白"的行；与 ui_state.json 里的 qq_rect / list_box 对比。

用法：python tmp/measure_chat_white_top.py tmp/screen.png
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QImage  # noqa: E402


def near_white(image: QImage, x: int, y: int, thr: int = 242) -> bool:
    color = image.pixelColor(x, y)
    return color.red() >= thr and color.green() >= thr and color.blue() >= thr


def main() -> int:
    image = QImage(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "tmp" / "screen.png"))
    if image.isNull():
        print("读不到截图")
        return 1
    state = json.loads((ROOT / "logs" / "ui_state.json").read_text(encoding="utf-8"))
    qq = state["qq_rect"]
    list_box = state["list_box"]
    # 在聊天区（x 取窗口高度的中间偏右，避开左侧会话列表）逐列找白色顶边
    xs = [int(qq[0] + (qq[2] - qq[0]) * ratio) for ratio in (0.55, 0.65, 0.75, 0.85)]
    found = []
    for x in xs:
        for y in range(int(qq[1]) + 1, int(qq[3]) - 80):
            if all(near_white(image, x, yy) for yy in range(y, y + 80, 8)):
                found.append((x, y))
                break
    print(f"QQ 窗口 = {qq}｜消息列表 = {list_box}")
    print(f"窗口顶边 y={qq[1]}｜消息列表顶边 y={list_box[1]}"
          f"｜差 {int(list_box[1]) - int(qq[1])}px")
    for x, y in found:
        print(f"  x={x} 处白色区顶边 y={y}（相对窗口顶 {y - int(qq[1])}px，"
              f"相对消息列表顶 {y - int(list_box[1])}px）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
