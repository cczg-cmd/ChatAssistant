#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 81 轮末回归：新提示词架构 —— **角色包在最前 + 分阶段只写本段字段**。

用户口径："为每个配置搞个角色包文本，然后在字段中引用这个角色包，而不是每个字段都塞一大堆
风格描写"＋"一次性往模型里塞太多 token 可能降低生成质量，可能需要分两段"。

覆盖：
  A. 角色包：出现在 system prompt **最前面**（在字段列表之前），而且**只出现一次**；
  B. 分阶段裁剪：quick 段只列 意图/情绪/危险度/建议，replies 段只列 回复选项，
     两段的 system prompt 都比 full 短；
  C. 第二段（replies）不再带 confidence 那句（它不产出这个字段）；
  D. 自我分析（self_mode=True）同样成立。

用法：python tools/test_prompt_stages.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message  # noqa: E402

MARK = "测试专用角色包XYZ（自称“本喵”、句尾带“喵”、说话短）"
# 第 82 轮：JSON 键名抽象成 A1/A2/…（config.JSON_KEYS），这里用键名常量，别再写 intent 之类
import config as _C  # noqa: E402
K1, K2, K3, K4, K5, K6 = (_C.json_key(k) for k in
                          ("intent", "emotion", "danger", "suggestion", "option", "confidence"))


def _system(cfg, target, stage: str) -> str:
    analyzer = Analyzer(cfg)
    target_msg = Message(msg_key="t", text="在吗", side=target, bbox=(0, 0, 300, 40))
    return analyzer.build_messages([target_msg], target_msg, stage=stage)[0]["content"]


def case_role_pack_place() -> bool:
    cfg = app_config.load_config()
    cfg.analysis_style_prompt = MARK
    sysp = _system(cfg, "left", "quick")
    ok = (sysp.count("【角色包】") == 1
          and "{0}".format(MARK) in sysp
          and sysp.index("【角色包】") < sysp.index("各键要写什么"))
    print(f"A 角色包在最前且只有一份：{'对' if ok else '错'}"
          f"（位置={sysp.index('【角色包】')}，字段列表在 {sysp.index('各键要写什么')}）")
    return ok


def case_stage_trim() -> bool:
    cfg = app_config.load_config()
    cfg.analysis_style_prompt = MARK
    quick = _system(cfg, "left", "quick")
    replies = _system(cfg, "left", "replies")
    full = _system(cfg, "left", "full")
    ok_quick = (f"{K1} ——" in quick and f"{K2} ——" in quick and f"{K3} ——" in quick
                and f"{K4} ——" in quick and f"{K5} ——" not in quick)
    ok_replies = (f"{K5} ——" in replies and f"{K1} ——" not in replies
                  and f"{K2} ——" not in replies and f"{K3} ——" not in replies
                  and f"{K4} ——" not in replies)
    ok_full = all(f"{k} ——" in full for k in (K1, K2, K3, K4, K5))
    ok_short = len(quick) < len(full) and len(replies) < len(full)
    ok = ok_quick and ok_replies and ok_full and ok_short
    print(f"B 分阶段只写本段字段：{'对' if ok else '错'}"
          f"（quick {len(quick)} 字 / replies {len(replies)} 字 / full {len(full)} 字）")
    return ok


def case_replies_no_confidence() -> bool:
    cfg = app_config.load_config()
    replies = _system(cfg, "left", "replies")
    quick = _system(cfg, "left", "quick")
    # 只看"结尾那句要求"有没有被去掉（角色包/字段说明里可能自己提到 confidence，不算）
    ok = (f"{K6}：0-1" not in replies) and (f"{K6}：0-1" in quick)
    print(f"C 第二段不带 confidence：{'对' if ok else '错'}")
    return ok


def case_self_mode() -> bool:
    """第 82 轮末：**只有一份角色包，两套体系共用**（设 self_* 不再单独生效）。"""
    cfg = app_config.load_config()
    cfg.analysis_style_prompt = MARK
    cfg.self_analysis_style_prompt = "这段不该被用到"
    quick = _system(cfg, "right", "quick")
    replies = _system(cfg, "right", "replies")
    ok = (MARK in quick and MARK in replies and "这段不该被用到" not in quick
          and f"{K5} ——" not in quick
          and f"{K1} ——" in quick and f"{K5} ——" in replies)
    print(f"D 自我分析同样成立：{'对' if ok else '错'}")
    return ok


def main() -> int:
    results = [case_role_pack_place(), case_stage_trim(), case_replies_no_confidence(),
               case_self_mode()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
