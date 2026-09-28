#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升级 llama-cpp-python 后的一次功能验证（不是基准对比）：
加载当前配置里的模型 → 跑一段完整分析（意图/情绪/危险度/建议 + 三条选项），
打印耗时与输出，用来确认"卡在分析中"是否已经消失。

用法：python tools/probe_qwen35.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core import analyzer as am  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message  # noqa: E402

for _d in am._cuda_bin_dirs(app_config.load_config()):
    try:
        os.add_dll_directory(_d)
    except Exception:
        pass


def main() -> int:
    cfg = app_config.load_config()
    a = cfg.analyzer
    print(f"模型 {Path(cfg.paths.model).name}｜chat_format={a.chat_format!r}｜"
          f"temp={a.temperature} top_p={a.top_p} top_k={a.top_k} min_p={a.min_p}")
    analyzer = Analyzer(cfg)
    t0 = time.perf_counter()
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("加载失败：", analyzer.last_error)
        return 1
    print(f"加载完成 {time.perf_counter()-t0:.2f}s（GPU 层 {analyzer.gpu_layers_used}）")

    target = Message(msg_key="p", text="太神圣了", side="left", bbox=(0, 0, 300, 40))
    ctx = [target,
           Message(msg_key="c1", text="刚看完那个视频", side="left", bbox=(0, 0, 0, 0)),
           Message(msg_key="c2", text="怎么样", side="right", bbox=(0, 0, 0, 0))]
    t1 = time.perf_counter()
    quick = analyzer.analyze_quick(target, ctx, use_cache=False)
    print(f"第一段 {time.perf_counter()-t1:.2f}s → "
          f"{'（无结果！）' if quick is None else f'{quick.intent}｜{quick.emotion}｜{quick.danger_level}｜建议：{quick.suggestion}'}")
    if quick is None:
        analyzer.close()
        return 1
    t2 = time.perf_counter()
    rep = analyzer.analyze_replies(target, ctx, quick=quick)
    print(f"第二段 {time.perf_counter()-t2:.2f}s → "
          f"{len(rep.replies) if rep else 0} 条选项")
    for item in (rep.replies if rep else []):
        print(f"   [{item.style}] {item.text}")
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
