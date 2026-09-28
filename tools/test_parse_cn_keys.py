#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验证：解析器能认出"中文键"（DeepSeek 实际返回的形态），不花任何 token。

数据就是刚才真实 API 返回的那一段（见 tmp/test_api_real.py 的输出）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402

REAL_CN = ('{"意图":"问电脑有没有清过灰","情绪":"平常询问","危险度":0,'
           '"回复建议":"如实说清没清过，顺便提掉网可能是散热或网卡问题",'
           '"回复选项":[{"style":"如实回答","text":"没清过，一直懒得拆"},'
           '{"style":"反问确认","text":"没清过，你是怀疑积灰导致掉网？"},'
           '{"style":"主动帮忙","text":"没清过，要不我帮你看看怎么清"}],"confidence":0.9}')
REAL_EN = ('{"intent":"问电脑有没有清过灰","emotion":"平常询问","danger_level":0,'
           '"suggestion":"如实回答即可","reply_options":[{"style":"稳当","text":"没清过"},'
           '{"style":"简单","text":"没有"},{"style":"轻松","text":"懒得拆"}],"confidence":0.9}')


def main() -> int:
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    ok = True
    for name, raw in (("中文键（DeepSeek 实测返回）", REAL_CN), ("英文键（提示词要求）", REAL_EN)):
        result = analyzer.parse(raw, "k")
        print(f"\n{name}：json_ok={result.json_ok}")
        print(f"  intent={result.intent}｜emotion={result.emotion}｜danger={result.danger_level}"
              f"｜confidence={result.confidence}")
        print(f"  建议={result.suggestion}")
        print(f"  选项={[(o.style, o.text) for o in result.replies]}")
        good = bool(result.intent) and len(result.replies) == 3 and result.emotion
        print(f"  [{'PASS' if good else 'FAIL'}] 关键字段齐全")
        ok = ok and good
    # 顺便验证"用户改名"情形：把危险度改名成"兴趣水平"，模型按显示名给键
    cfg.fields.danger_name = "兴趣水平"
    analyzer2 = Analyzer(cfg)
    raw = REAL_CN.replace('"危险度"', '"兴趣水平"')
    result = analyzer2.parse(raw, "k")
    print(f"\n改名后（兴趣水平）：danger={result.danger_level}｜intent={result.intent}")
    print(f"  [{'PASS' if result.intent and result.danger_level == 0 else 'FAIL'}] 改名后仍能解析")
    ok = ok and bool(result.intent) and result.danger_level == 0
    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
