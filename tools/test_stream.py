#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证流式提前上屏：打印每次 partial 的到达时刻，和最终结果对比。
期望：情绪·意图在 ~0.5s 到达（第一次 partial），危险度紧随其后，整段约 2s。
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

_cuda_dirs = analyzer_mod._cuda_bin_dirs(app_config.load_config())
for _d in _cuda_dirs:
    try:
        os.add_dll_directory(_d)
    except Exception:
        pass
os.environ["PATH"] = ";".join(_cuda_dirs) + ";" + os.environ.get("PATH", "")

SAMPLES = [
    ("我现在打开游戏就会掉网", ["对方: 你电脑有去清过灰嘛", "我: 没有啊"]),
    ("问豆包说去清灰试试", ["对方: 我现在打开游戏就会掉网", "我: 那怎么办"]),
]


def main() -> int:
    cfg = app_config.load_config()
    # 默认直接吃 config.json 里当前生效的那套配置（例如"猫娘配置"）；
    # 想手动覆盖就设环境变量 QQCA_OVERRIDE=1
    if os.environ.get("QQCA_OVERRIDE") == "1":
        cfg.fields.option_prompt = ("三条回复都用女仆口吻，称呼对方为「主人」，"
                                    "语气恭敬可爱、简短（每条不超过 30 字）")
        cfg.fields.suggestion_prompt = "给主人一句不超过 15 字的应对提示"
    print("生效配置:", cfg.active_profile, "| option prompt:",
          app_config.field_prompt(cfg, "option")[:60])
    analyzer = Analyzer(cfg)
    if not analyzer.load():
        print("加载失败:", analyzer.last_error)
        return 1
    print(f"模型 {analyzer.model_path.name}｜stream_partials={cfg.analyzer.stream_partials}")
    for target_text, background in SAMPLES:
        target = Message(msg_key="stream-target", text=target_text, side="left", bbox=(0, 0, 300, 40))
        ctx = [target] + [Message(msg_key=f"bg{i}", text=line.split(": ", 1)[-1],
                                  side="left" if line.startswith("对方") else "right",
                                  bbox=(0, 0, 0, 0)) for i, line in enumerate(background)]
        marks = []
        t0 = time.perf_counter()

        def on_partial(result, _t0=t0):
            marks.append(((time.perf_counter() - _t0) * 1000, result.emotion,
                          result.intent, result.danger_level))

        final = analyzer.analyze_quick(target, ctx, use_cache=False, on_partial=on_partial)
        total = (time.perf_counter() - t0) * 1000
        print(f"\n目标：{target_text}")
        for ms, emotion, intent, danger in marks:
            print(f"   partial @ {ms:6.0f}ms → {emotion} · {intent}（危险度 {danger}）")
        print(f"   最终   @ {total:6.0f}ms → {final.emotion} · {final.intent}"
              f"（危险度 {final.danger_level}）建议 {len(final.suggestion or '')} 字")
        print(f"   首屏占比：{marks[0][0] / total * 100:.0f}%（{len(marks)} 次 partial）")

        # 第二段：三条回复也流式上屏（逐条到达）
        option_marks = []
        t1 = time.perf_counter()

        def on_option_partial(result, _t1=t1):
            option_marks.append(((time.perf_counter() - _t1) * 1000, len(result.replies)))

        replies = analyzer.analyze_replies(target, ctx, quick=final, on_partial=on_option_partial)
        print(f"   选项流式：{[(f'{ms:.0f}ms', f'{n}条') for ms, n in option_marks]}"
              f"｜最终 {len(replies.replies)} 条 / {(time.perf_counter() - t1) * 1000:.0f}ms")
        for option in replies.replies:
            print(f"      [{option.style}] {option.text}")
        print(f"   建议：{replies.suggestion}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
