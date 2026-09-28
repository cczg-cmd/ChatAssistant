#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复现"连续几条消息串不起来"的问题：用用户给的那串 cos 道具对话。

对其中 3 条分别分析，看 intent 有没有体现"在接着列道具清单"。
用法：python tmp/test_context_chain.py [标签]
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

# 用户给的真实一串（全部是对方发的）
CHAIN = ["哦对的就这种",
         "痛衣内搭外边格子衬衫然后牛仔裤",
         "这几个我都有",
         "还差一个眼镜，头巾",
         "背包再塞点周边海报",
         "手上最好来个袋子"]
TARGETS = ["还差一个眼镜，头巾", "背包再塞点周边海报", "手上最好来个袋子"]


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "当前"
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    if not analyzer.load():
        print("模型加载失败:", analyzer.last_error)
        return 1
    messages = [Message(msg_key=f"m{i}", text=text, side="left",
                        bbox=(0, i * 60, 900, i * 60 + 40)) for i, text in enumerate(CHAIN)]
    print(f"===== {label}（后端 {cfg.analyzer.backend}）=====")
    for target_text in TARGETS:
        target = next(m for m in messages if m.text == target_text)
        context = [m for m in messages if m.bbox[1] <= target.bbox[3]]
        # 带上 1 条【后文】（side="after"），模拟真实读取行为
        following = int(getattr(cfg.analyzer, "context_following", 0) or 0)
        for m in messages:
            if m.bbox[1] > target.bbox[3] and following > 0:
                context.append(Message(msg_key=m.msg_key + "a", text=m.text,
                                       side="after", bbox=m.bbox))
                following -= 1
        analyzer.clear_cache()
        if target_text == TARGETS[0]:
            msgs = analyzer.build_messages(context, target)
            print("（prompt 里的对话记录部分）")
            print("  " + msgs[1]["content"].replace("\n", "\n  ")[:420])
        quick = analyzer.analyze_quick(target, context, use_cache=False)
        print(f"\n目标：{target_text}")
        if quick:
            print(f"  意图：{quick.intent}｜情绪：{quick.emotion}｜危险度：{quick.danger_level}")
            print(f"  建议：{quick.suggestion}")
            replies = analyzer.analyze_replies(target, context, quick=quick)
            if replies:
                for index, option in enumerate(replies.replies, 1):
                    print(f"  选项{index}：[{option.style}] {option.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
