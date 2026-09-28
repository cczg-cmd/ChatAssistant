#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量"一次真实请求"的 prompt 大小：系统提示 + 上下文 + 本次分配（两套配置各算一次）。

用法：python tmp/measure_request_now.py
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

BACKGROUND = ["哦对的就这种", "痛衣内搭外边格子衬衫然后牛仔裤", "这几个我都有",
              "还差一个眼镜，头巾", "背包再塞点周边海报", "手上最好来个袋子"]


def main() -> int:
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("模型加载失败:", analyzer.last_error)
        return 1
    llm = analyzer.llm
    target = Message(msg_key="t", text="还差一个眼镜，头巾", side="left", bbox=(0, 0, 300, 40))
    context = [target] + [
        Message(msg_key=f"b{i}", text=t, side="left", bbox=(0, 0, 300, 40))
        for i, t in enumerate(BACKGROUND)]

    def count(text: str) -> int:
        return len(llm.tokenize(text.encode("utf-8")))

    for profile in ("默认配置", "猫娘配置"):
        app_config.apply_profile(cfg, profile)
        messages = analyzer.build_messages(context, target)
        # 复刻 analyze_replies 里那段"本次分配"注入（最长形态）
        injected = ("\n（本次分配：① 认同(说法:附和一句) ② 反调(说法:只说自己感想) "
                    "③ 中立(说法:拐个小方向)；三条说法互不相同、像随口说的，"
                    "正文别重复标签，别编细节；下次换一组）")
        messages[1]["content"] += injected
        total = sum(count(m["content"]) for m in messages)
        print(f"{profile}：系统 {count(messages[0]['content'])} + 用户段 "
              f"{count(messages[1]['content'])} = **{total} token**"
              f"（用户段含 {len(context)} 条上下文）")
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
