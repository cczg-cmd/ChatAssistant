#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""精确量"用户画像"的开销（用模型自带分词器，不是估字数）：
  · 空画像时提示词是否**逐字不变**（与未加该功能时一致）；
  · 填了画像后：系统提示词 + 每次生成注入段 各多多少 token；
  · 几种常见长度（50/100/200 字画像）的总增量。

用法：python tmp/measure_user_profile_cost.py
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

SAMPLE_PROFILE = ("我说话很短，基本 6-10 个字；几乎不用书面词；喜欢用“确实”“笑死”“麻了”"
                  "这类词；句尾偶尔加个“捏”；不爱用感叹号和表情包。")


def main() -> int:
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("模型加载失败:", analyzer.last_error)
        return 1
    llm = analyzer.llm

    def tok(text: str) -> int:
        return len(llm.tokenize(text.encode("utf-8")))

    cfg.fields.user_profile = ""
    base_prompt = app_config.build_system_prompt(cfg)
    base_tokens = tok(base_prompt)
    print(f"① 空画像：系统提示词 {len(base_prompt)} 字 / {base_tokens} token")
    print(f"   （第 68 轮时的长度是 1585 字 —— 现在逐字相同："
          f"{len(base_prompt) == 1585}）")

    for label, profile in (("示例画像(约70字)", SAMPLE_PROFILE),
                           ("短画像(20字)", "我说话短，爱用“确实”“笑死”。"),
                           ("长画像(约200字)", SAMPLE_PROFILE * 3)):
        cfg.fields.user_profile = profile
        prompt = app_config.build_system_prompt(cfg)
        delta_sys = tok(prompt) - base_tokens
        # 第 70 轮起：画像**只在第二段**注入（用户口径），第一段不注入
        reply_inject = f"\n（我的说话习惯（务必模仿）：{profile.strip()[:80]}）"
        delta_req = tok(reply_inject)
        print(f"\n② {label}（{len(profile)} 字）：")
        print(f"   系统提示词 +{delta_sys} token（{len(prompt)} 字）")
        print(f"   第二段注入 +{delta_req} token（第一段不注入）")
        print(f"   → 一次完整分析合计多 ≈ {delta_sys + delta_req} token prompt")

    # ③ 空画像时，注入段是否真的一个字都不加
    cfg.fields.user_profile = ""
    messages = analyzer.build_messages(
        [Message(msg_key="t", text="测试", side="left", bbox=(0, 0, 300, 40))],
        Message(msg_key="t", text="测试", side="left", bbox=(0, 0, 300, 40)))
    before = messages[1]["content"]
    profile = (getattr(getattr(cfg, "fields", None), "user_profile", "") or "").strip()
    if profile:                                    # 与 analyze_* 里的判断完全一致
        messages[1]["content"] += "（不该发生）"
    print(f"\n③ 空画像时注入段是否原样：{messages[1]['content'] == before}")
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
