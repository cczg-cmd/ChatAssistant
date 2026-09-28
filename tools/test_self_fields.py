#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：① 自我分析换字段（质量评分/消息点评/发言选项）② API 流式 SSE 解析。

不依赖 QQ、不联网：
  A. build_system_prompt(self_mode=True) 必须换成自我分析那套（前言 + 字段名 + 说明），
     且不能残留"危险度"判定锚点；
  B. build_messages 按目标的左右自动选字段（左侧=对方→普通；右侧=自己→自我分析）；
  C. Analyzer._parse_sse_chunk 的 SSE 解析（增量文本 / usage / [DONE] / 坏 JSON）；
  D. AnalysisResult.self_analysis 默认 False（不影响老路径）。

用法：python tools/test_self_fields.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message  # noqa: E402


def case_prompt_switch() -> bool:
    # 用**纯默认配置**断言默认文本：load_config() 会套用用户当前那套配置，
    # 用户自己导入/改过的字段名会让"默认名字"断言假红（踩过一次）。
    cfg = app_config.Config()
    other = app_config.build_system_prompt(cfg, self_mode=False)
    mine = app_config.build_system_prompt(cfg, self_mode=True)
    checks = [
        ("魅力" in mine, "自我分析含'魅力'"),
        # 第 80 轮：字段列表改成"JSON 键名 + 编号要求"（不再把用户显示名放进提示词），
        # 所以这里断言**内容**而不是显示名。
        ("接着自己这条" in mine, "自我分析用的是'发言选项'那套说明"),
        ("魅力评价" in mine, "自我分析含'魅力评价'"),
        (app_config.json_key("option") in mine, "提示词里给了 JSON 键名（A5）"),
        ("越高越有魅力" in mine, "魅力方向写清楚了"),
        # 不能残留"对方的追责"那套锚点（自我分析里那一段必须换掉）
        ("明确在指责你" not in mine and "追责" not in mine, "自我分析里没有旧的危险度锚点"),
        ("自己发出去的" in mine, "前言点明目标是我自己的消息"),
        ("危险度" in other and "魅力" not in other, "对方消息仍是危险度那套"),
        # 第 79 轮：发言选项的三条立场固定为 继续展开 / 吐槽 / 终结
        ("继续展开当前话题" in mine and "吐槽当前话题" in mine and "终结当前话题" in mine,
         "发言选项的三种立场写进了字段说明"),
        # 第 79 轮（用户："自我分析给出的发言变成对我说的了"）：必须写明收件人是对方
        ("收件人是对方" in mine and "别人对我说的话" in mine,
         "发言选项写明了收件人是对方、并禁止写成对我说的话"),
    ]
    ok = all(flag for flag, _ in checks)
    print("A. 提示词切换：", "对" if ok else "错")
    for flag, desc in checks:
        if not flag:
            print(f"   ✗ {desc}")
    return ok


def case_target_side() -> bool:
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    left = Message(msg_key="L", text="在吗", side="left", bbox=(0, 0, 200, 40))
    right = Message(msg_key="R", text="在的", side="right", bbox=(0, 0, 200, 40))
    prompt_left = analyzer.build_messages([left], left)[0]["content"]
    prompt_right = analyzer.build_messages([right], right)[0]["content"]
    ok = (("魅力" not in prompt_left) and ("魅力" in prompt_right)
          and analyzer.is_self_target(right) and not analyzer.is_self_target(left))
    print(f"B. 按左右自动换字段：{'对' if ok else '错'}")
    return ok


def case_sse_parse() -> bool:
    parse = Analyzer._parse_sse_chunk
    piece, usage = parse('data: {"choices":[{"delta":{"content":"{\\"a\\":"}}]}')
    ok1 = piece == '{"a":' and usage is None
    piece2, _ = parse("data: [DONE]")
    ok2 = piece2 == ""
    piece3, _ = parse("data: {坏的 json")
    ok3 = piece3 == ""
    piece4, usage4 = parse('data: {"choices":[{"delta":{}}],"usage":'
                           '{"prompt_tokens":12,"completion_tokens":34}}')
    ok4 = piece4 == "" and usage4 == {"prompt_tokens": 12, "completion_tokens": 34}
    piece5, _ = parse('data: {"choices":[{"message":{"content":"整段给"}}]}')
    ok5 = piece5 == "整段给"
    ok6 = parse("")[0] == "" and parse(": keep-alive")[0] == ""
    ok = all([ok1, ok2, ok3, ok4, ok5, ok6])
    print(f"C. SSE 解析：{'对' if ok else '错'}"
          f"（增量={piece!r} usage={usage4} 兜底={piece5!r}）")
    return ok


def case_default_flag() -> bool:
    cfg = app_config.Config()
    from core.analyzer import AnalysisResult
    result = AnalysisResult(msg_key="x")
    ok = (result.self_analysis is False
          and getattr(cfg, "self_fields", None) is not None
          and (cfg.self_analysis_style_prompt or "") == ""
          and bool(getattr(cfg.api, "stream", True)))
    print(f"D. 默认值：{'对' if ok else '错'}"
          f"（self_analysis 默认关、api.stream 默认开、self_fields 存在）")
    return ok


def main() -> int:
    results = [case_prompt_switch(), case_target_side(), case_sse_parse(), case_default_flag()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
