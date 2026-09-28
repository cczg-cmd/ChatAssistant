#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置落盘加固的回归测试（第 63 轮）：原子写 + 备份 + 损坏自动恢复。

用法：python tmp/test_config_atomic.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402


def main() -> int:
    target = ROOT / "tmp" / "test_config_atomic.json"
    backup = target.with_name(target.name + ".bak")
    for path in (target, backup, target.with_name(target.name + ".tmp")):
        path.unlink(missing_ok=True)

    cfg = app_config.Config()
    cfg.analyzer.context_messages = 16
    cfg.ui.show_reply_style_prefix = False
    app_config.save_config(cfg, target)
    print("1) 首次保存：", "PASS" if target.exists() else "FAIL",
          "｜残留 .tmp：", target.with_name(target.name + ".tmp").exists())
    assert json.loads(target.read_text(encoding="utf-8"))["analyzer"]["context_messages"] == 16

    # 第二次保存 → 应该留下上一版备份
    cfg.analyzer.context_messages = 24
    app_config.save_config(cfg, target)
    ok_bak = backup.exists() and json.loads(
        backup.read_text(encoding="utf-8"))["analyzer"]["context_messages"] == 16
    print("2) 留下上一版 .bak：", "PASS" if ok_bak else "FAIL")

    # 主文件被写坏 → 应从 .bak 恢复，而不是静默回默认值
    target.write_text('{"analyzer": {"context_messa', encoding="utf-8")
    recovered = app_config.load_config(target)
    print("3) 主文件损坏时从 .bak 恢复：",
          "PASS" if recovered.analyzer.context_messages == 16 else "FAIL",
          f"（恢复值 {recovered.analyzer.context_messages}）")

    # 连备份都坏 → 回退默认值，且不抛异常
    backup.write_text("not json at all", encoding="utf-8")
    fallback = app_config.load_config(target)
    print("4) 备份也坏时回退默认值不崩：",
          "PASS" if fallback.analyzer.context_messages == 16 else "FAIL")

    # 清理
    for path in (target, backup, target.with_name(target.name + ".tmp")):
        path.unlink(missing_ok=True)
    print("已清理测试文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
