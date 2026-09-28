#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 80 轮回归（最终口径）：**提示词防抄 + 只清理不丢弃**（纯离线，不加载模型）。

背景（用户最终口径）：
  · "完全舍弃重问这个方案，也不要清空方案，它们带来的副作用会大于实际作用"；
  · "主要要求从提示词方向或模型控制方向去优化"。
所以现在：
  ① 提示词结构改成"**JSON 键名 + 编号要求**"——不再出现 `字段名：说明` 这种
     能被整行照抄的形状，用户自定义的显示名也不进提示词（面板照旧显示）；
  ② 后处理只做**不会丢内容**的清理（`Analyzer._clean_field`）：控制字符→空格、
     剪掉开头/结尾粘上的「字段名：」或"半个字段名"、剪掉尾部半个标点；
     **绝不**因为"像抄提示词"就把字段清空，也**绝不**再发第二次请求。

用法：python tools/test_prompt_echo.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402

# 与用户实测那套配置同形的字段名（不用读用户存档）
LABELS = ["本小姐的迷人指数", "本小姐的判词", "下一句本小姐这样", "意图", "情绪"]


def case_prompt_has_no_copyable_list() -> bool:
    """A：提示词里没有"字段名：说明"列表，也没有用户的显示名（面板照旧用它们）。"""
    cfg = app_config.Config()
    entry = dict(app_config.find_profile(cfg, app_config.CATGIRL_PROFILE_NAME) or {})
    entry.update({
        "self_danger_name": LABELS[0], "self_danger_prompt": "0-10整数，越高越迷人。",
        "self_suggestion_name": LABELS[1], "self_suggestion_prompt": "先给结论，40字内。",
        "self_option_name": LABELS[2], "self_option_prompt": "三条我接下来发给对方的话。",
    })
    cfg.fields_profiles = [entry]
    for key, value in entry.items():
        if isinstance(value, str):
            cfg.self_fields.__setattr__(key.replace("self_", "", 1), value)
    mine = app_config.build_system_prompt(cfg, self_mode=True)
    other = app_config.build_system_prompt(cfg, self_mode=False)
    all_labels = Analyzer(cfg)._all_labels()          # noqa: SLF001
    checks = [
        ("JSON 键名固定：" in mine, "给了 JSON 键名行"),
        ("各键要写什么（下面是**要求**，不是要你输出的文字）" in mine, "说明'要求≠内容'"),
        # 第 82 轮：JSON 键名抽象成 A1/A2/…（config.JSON_KEYS）
        (f"1) {app_config.json_key('intent')} ——" in mine
         and f"5) {app_config.json_key('option')} ——" in mine, "编号要求格式"),
        # 关键性质：显示名不再作为"列表头"出现（字段说明正文里提到这些词是正常的）
        (all(f"{lab}：" not in mine for lab in all_labels), "自我那侧没有'显示名：'列表头"),
        (all(f"{lab}：" not in other for lab in all_labels), "对方那侧同理"),
        ("危险度：" not in mine and "魅力评价：" not in mine, "没有可整行照抄的字段列表"),
    ]
    ok = all(flag for flag, _ in checks)
    print(f"A 提示词没有可整行照抄的字段列表：{'对' if ok else '错'}")
    for flag, desc in checks:
        if not flag:
            print(f"   ✗ {desc}")
    return ok


def case_clean_never_drops() -> bool:
    """B：`_clean_field` 只清理、绝不丢内容（抄写样本也必须原样留下）。"""
    analyzer = Analyzer(app_config.Config())
    cases = [
        ("suggestion", "本小姐的判词：先", True),          # 只剪掉开头字段名
        ("suggestion", "本小姐的迷人指数：0-10整数，越高越迷人。", True),
        ("suggestion", "继续展开", True),
        ("emotion", "急", True),
        ("intent", "约见面", True),
        ("suggestion", "还行但平，迟到两小时。本小姐的", True),   # 尾部半截字段名
        ("suggestion", "加个表情\n下一句本小姐这样：", True),      # 换行 + 整段字段名
    ]
    ok = True
    for field, value, _keep in cases:
        cleaned = analyzer._clean_field(value, field, LABELS)      # noqa: SLF001
        # 允许"剪掉标签/控制字符/尾部标点"，但**必须留下内容**
        bad = (not cleaned) or ("\n" in cleaned) or cleaned.endswith("：") \
            or cleaned.startswith("本小姐")
        if bad:
            ok = False
        print(f"    {field}: {value!r} → {cleaned!r}{'  ✗' if bad else ''}")
    print(f"B 只清理、绝不丢内容：{'对' if ok else '错'}")
    return ok


def case_no_repair_no_wipe_left() -> bool:
    """C：源码里不能再有"重问/清空"那套（用户明确要撤掉）。"""
    src = io.open(Path(ROOT) / "core" / "analyzer.py", encoding="utf-8").read()
    fast = io.open(Path(ROOT) / "core" / "fast_mask.py", encoding="utf-8").read()
    gone = ["_repair_field", "_recover_text", "_clean_reply_text", "_single_field_grammar",
            "抄写修复"]
    ok_core = not any(item in src for item in gone)
    ok_fast = "single_field_program" not in fast
    ok = ok_core and ok_fast
    print(f"C 重问/清空机制已彻底移除：{'对' if ok else '错'}"
          f"（analyzer 残留={[x for x in gone if x in src]}｜fast_mask 已清={ok_fast}）")
    return ok


def main() -> int:
    results = [case_prompt_has_no_copyable_list(), case_clean_never_drops(),
               case_no_repair_no_wipe_left()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
