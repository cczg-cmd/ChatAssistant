#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 68 轮回归：上下文面板 ①右侧有位置就贴右侧 ②右侧没位置要放弃（交给旧位置）
③窄面板下标题用省略号而不是被裁掉。输出 tmp/ctx_title_elide.png 供肉眼确认。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import config as app_config  # noqa: E402
from ui.context_viewer import ContextViewer  # noqa: E402

SCREEN = [0, 0, 2560, 1600]


def main() -> int:
    app = QApplication(sys.argv)                      # noqa: F841
    cfg = app_config.load_config()
    viewer = ContextViewer(cfg)
    results = []

    # ① QQ 窗口右侧有 1000px 空间 → 应该贴右侧
    ok = viewer.place_right_of([71, 199, 1496, 1410], SCREEN)
    geo = viewer.geometry()
    print(f"① 右侧有空间：返回 {ok}，面板 x={geo.x()}（期望 ≥1502）宽 {viewer.full_width()}")
    results.append(ok and geo.x() >= 1502 and viewer.full_width() >= 240)

    # ② QQ 窗口贴屏幕右缘（右侧只剩 16px）→ 应该返回 False（调用方改走旧位置）
    ok2 = viewer.place_right_of([100, 100, 2540, 1400], SCREEN)
    print(f"② 右侧没空间：返回 {ok2}（期望 False，交给旧位置）")
    results.append(ok2 is False)

    # ③ 窄面板（240px）+ 长标题 → 画一张图看标题是不是省略号结尾
    viewer.reset_size()
    viewer.set_size(240, 300)
    viewer.setFixedSize(240, 300)       # 固定住尺寸，避免 grab 拿到 resize 前的 backing store
    viewer.set_anim(1.0)
    viewer.show()                       # grab 需要事件循环把 resize/重绘跑完
    app.processEvents()
    title = "上下文历史（池 8 条｜本轮进分析 6 条 = 池子 3 + 额外 3）"
    viewer.set_history(title, "● 对方: 这是一条比较长的消息，用来测试右侧不会再被裁掉\n"
                              "○ 我: 这样", keep_scroll=False)
    app.processEvents()
    # 用 render() 画到固定尺寸的图上（grab() 在这种"固定尺寸 + 透明背景"的窗口上
    # 会按 sizeHint 出图，尺寸不可靠 → 测试里不用它）
    from PySide6.QtGui import QImage
    image = QImage(viewer.width(), viewer.height(), QImage.Format_ARGB32)
    image.fill(0)
    viewer.render(image)
    viewer.setMinimumSize(0, 0)
    viewer.setMaximumSize(16777215, 16777215)
    viewer.hide()
    out = ROOT / "tmp" / "ctx_title_elide.png"
    image.save(str(out))
    print(f"③ 窄面板标题渲染已保存：{out}（宽 {image.width()}，标题应省略号结尾）")
    results.append(image.width() == 240)

    print(f"\n{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
