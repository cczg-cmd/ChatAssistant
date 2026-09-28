#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 71 轮回归：启动时**没有会话窗口**时，状态灯 / 上下文图标 / 上下文面板都不能出现。

用户反馈的 bug：面板默认是"展开"状态，启动时还没有会话就被 show() 出来，
而且因为拿不到窗口矩形，它会停在默认坐标飘着。

用法：python tmp/test_startup_hidden.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import config as app_config  # noqa: E402
from main import ChatAssistantApp  # noqa: E402


def main() -> int:
    app_qt = QApplication(sys.argv)                  # noqa: F841
    cfg = app_config.load_config()
    gui = ChatAssistantApp(app_qt, cfg)
    results = []

    # ① 刚构造完（还没 start()、也没有会话）：三个浮窗都不该可见
    hidden_at_construct = not any(w.isVisible() for w in
                                  (gui.dot, gui.context_button, gui.context_viewer))
    print(f"① 构造后不显示任何浮窗：{hidden_at_construct}")
    results.append(hidden_at_construct)

    # ② 模拟"启动后快 tick 跑了很久"：面板目标仍是展开（默认），但会话不可用
    gui._chat_available = False
    gui._ctx_anim_target = 1.0
    gui._ctx_anim = 0.0
    for _ in range(20):
        gui._step_ctx_anim()
        gui._step_panel_anim()
    still_hidden = not gui.context_viewer.isVisible()
    print(f"② 无会话时反复跑动画 tick，面板仍隐藏：{still_hidden}"
          f"（bug 版本这里会 True→显示）")
    results.append(still_hidden)

    # ③ 会话变成可用 → 面板才允许出现（这里只验内部状态机，不真的 show 到屏幕外）
    gui.window_rect = [100, 100, 1000, 900]
    gui.list_box = [110, 200, 990, 800]
    gui._set_chat_available(True)
    allowed = gui._chat_available and gui._ctx_anim_target > 0.0
    print(f"③ 会话可用后允许展开（target={gui._ctx_anim_target}）：{allowed}")
    results.append(allowed)

    # ④ 再变回不可用 → 必须马上隐藏 + 目标归零
    gui._set_chat_available(False)
    hidden_again = (not gui._chat_available and gui._ctx_anim_target == 0.0
                    and not gui.context_viewer.isVisible())
    print(f"④ 失去会话立刻隐藏：{hidden_again}")
    results.append(hidden_again)

    gui.dot.hide()
    gui.context_button.hide()
    gui.context_viewer.hide()
    print(f"\n{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
