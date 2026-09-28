#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单独打开设置面板（只为验证渲染/截图；正式入口仍是状态灯齿轮与托盘菜单）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from config import load_config  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    dialog = SettingsDialog(load_config())
    dialog.move(60, 60)
    dialog.show()
    tabs = dialog.findChild(QTabWidget)
    if tabs is not None:          # 切到 "字段与 Prompt" 页，方便截图确认
        tabs.setCurrentIndex(1)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
