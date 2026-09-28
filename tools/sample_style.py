#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印 3 条样本的完整分析输出（本地模型），用于比较"提示词改动前后"的 AI 味。

用法：python tmp/sample_style.py [标签]
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

SAMPLES = [
    ("你电脑有去清过灰嘛", ["对方: 我现在打开游戏就会掉网", "我: 那怎么办"]),
    ("你上次答应我的事怎么又没做", ["我: 昨天在忙", "对方: 忙什么"]),
    ("太神圣了", ["对方: 刚看完那个视频", "我: 怎么样"]),
]


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "当前"
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    if not analyzer.load():
        print("模型加载失败:", analyzer.last_error)
        return 1
    print(f"===== {label}（后端 {cfg.analyzer.backend}／配置 {cfg.active_profile}）=====")
    for text, background in SAMPLES:
        target = Message(msg_key=f"s{abs(hash(text))}", text=text, side="left",
                         bbox=(0, 0, 300, 40))
        context = [target] + [Message(msg_key=f"b{i}", text=line.split(": ", 1)[-1],
                                      side="left" if line.startswith("对方") else "right",
                                      bbox=(0, 0, 0, 0))
                              for i, line in enumerate(background)]
        analyzer.clear_cache()
        result = analyzer.analyze_quick(target, context, use_cache=False)
        replies = analyzer.analyze_replies(target, context, quick=result) if result else None
        print(f"\n目标：{text}")
        if result:
            print(f"  意图：{result.intent}｜情绪：{result.emotion}｜危险度：{result.danger_level}")
            print(f"  建议：{result.suggestion}")
        if replies:
            for index, option in enumerate(replies.replies, 1):
                print(f"  选项{index}：[{option.style}] {option.text}")
    # 同一条消息连跑 3 次，检查"风格标签是否会每次都一样"（小模型示例过拟合的典型症状）
    if len(sys.argv) > 2 and sys.argv[2] == "repeat":
        text, background = SAMPLES[0]
        target = Message(msg_key="rep", text=text, side="left", bbox=(0, 0, 300, 40))
        context = [target] + [Message(msg_key=f"b{i}", text=line.split(": ", 1)[-1],
                                      side="left" if line.startswith("对方") else "right",
                                      bbox=(0, 0, 0, 0)) for i, line in enumerate(background)]
        print("\n--- 同一条消息连续 3 次（看风格标签是否重复）---")
        for round_index in range(1, 4):
            analyzer.clear_cache()
            quick = analyzer.analyze_quick(target, context, use_cache=False)
            rep = analyzer.analyze_replies(target, context, quick=quick)
            labels = [f"[{o.style}]{o.text}" for o in (rep.replies if rep else [])]
            print(f"  第{round_index}次：{'｜'.join(labels)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
