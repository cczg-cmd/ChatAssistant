#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 UIA 诊断包（命令行版；应用里对应托盘菜单「导出 UIA 诊断包」）。

用法：python tools/diagnose_uia.py
产物：logs/uia_diagnose_<时间戳>.json / .txt
      txt 是给人看的摘要（窗口、树规模、容器候选+排除原因、输入框、当前策略配置）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core import diagnose  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402


def main() -> int:
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    messages, info = reader.read()
    print(f"读了一次：{len(messages)} 条，status={reader.status}，"
          f"list_strategy={info['uia'].get('list_strategy')!r}")
    path = diagnose.write(reader.uia, cfg)
    print("诊断包：", path)
    print("可读版：", path.with_suffix(".txt"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
