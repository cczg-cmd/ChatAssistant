#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 59 轮回归：验证"刷新选项"路径也走立场分配。

第 80 轮：刷新不再把上一版原文喂回提示词（4B 会照抄，见 tmp/probe_refresh2.py），
改成"新种子 + 临时升温"，本用例跟着去掉 avoid_options。

用法：python tmp/check_refresh_stance.py
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core import analyzer as analyzer_mod  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message  # noqa: E402

_cuda = analyzer_mod._cuda_bin_dirs(app_config.load_config())
for _d in _cuda:
    try:
        os.add_dll_directory(_d)
    except Exception:
        pass
os.environ["PATH"] = ";".join(_cuda) + ";" + os.environ.get("PATH", "")


def main() -> int:
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("模型加载失败:", analyzer.last_error)
        return 1
    target = Message(msg_key="r1", text="太神圣了", side="left", bbox=(0, 0, 300, 40))
    context = [target, Message(msg_key="c1", text="刚看完那个视频", side="left",
                               bbox=(0, 0, 0, 0)),
               Message(msg_key="c2", text="怎么样", side="right", bbox=(0, 0, 0, 0))]
    quick = analyzer.analyze_quick(target, context, use_cache=False)
    first = analyzer.analyze_replies(target, context, quick=quick)
    print("第一版：" + "｜".join(f"[{o.style}]{o.text}" for o in first.replies))
    # 模拟用户点刷新（main.py on_options_refresh 的口径）
    analyzer.extra_seed = random.randrange(1, 100000)
    analyzer.temp_override = 0.9
    second = analyzer.analyze_replies(target, context, quick=quick)
    print("刷新后：" + "｜".join(f"[{o.style}]{o.text}" for o in second.replies))
    ok = [o.style.split("·")[0] for o in second.replies] == ["认同", "反调", "中立"]
    print("刷新后立场顺序正确:", ok)
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
