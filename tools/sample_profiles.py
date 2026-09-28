#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两套内置配置（默认配置／猫娘配置）在同样 3 条样本上的输出对比（本地模型，GPU）。

用法：python tmp/sample_profiles.py [repeat]
"""

from __future__ import annotations

import os
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

SAMPLES = [
    ("你电脑有去清过灰嘛", ["对方: 我现在打开游戏就会掉网", "我: 那怎么办"]),
    ("你上次答应我的事怎么又没做", ["我: 昨天在忙", "对方: 忙什么"]),
    ("太神圣了", ["对方: 刚看完那个视频", "我: 怎么样"]),
    # 一条"没有言外之意"的事实问答，用来确认模型没被写坏成硬编动机
    ("明天几点开门", ["我: 我明天想去一趟", "对方: 那你去吧"]),
]


def build(text: str, background: list[str]) -> tuple[Message, list[Message]]:
    target = Message(msg_key=f"s{abs(hash(text))}", text=text, side="left",
                     bbox=(0, 0, 300, 40))
    context = [target] + [
        Message(msg_key=f"b{i}", text=line.split(": ", 1)[-1],
                side="left" if line.startswith("对方") else "right", bbox=(0, 0, 0, 0))
        for i, line in enumerate(background)]
    return target, context


def main() -> int:
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("模型加载失败:", analyzer.last_error)
        return 1
    repeat = len(sys.argv) > 1 and sys.argv[1] == "repeat"
    for profile in ("默认配置", "猫娘配置"):
        app_config.apply_profile(cfg, profile)
        cfg.active_profile = profile
        print(f"\n########## {profile} ##########")
        for text, background in SAMPLES:
            target, context = build(text, background)
            analyzer.clear_cache()
            analyzer.extra_seed = 0
            quick = analyzer.analyze_quick(target, context, use_cache=False)
            replies = analyzer.analyze_replies(target, context, quick=quick) if quick else None
            print(f"\n目标：{text}")
            if quick:
                print(f"  意图：{quick.intent}｜情绪：{quick.emotion}｜危险度：{quick.danger_level}")
                print(f"  建议：{quick.suggestion}")
            for index, option in enumerate(replies.replies if replies else [], 1):
                print(f"  选项{index}：[{option.style}] {option.text}")
        if repeat:
            text, background = SAMPLES[0]
            target, context = build(text, background)
            print("\n--- 同一条消息连续 3 次（看风格标签是否重复）---")
            for round_index in range(1, 4):
                analyzer.clear_cache()
                analyzer.extra_seed = 0
                q = analyzer.analyze_quick(target, context, use_cache=False)
                rep = analyzer.analyze_replies(target, context, quick=q)
                labels = [f"[{o.style}]{o.text}" for o in (rep.replies if rep else [])]
                print(f"  第{round_index}次：{'｜'.join(labels)}")
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
