#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文策略 A/B 实验台（本地模型，零 API 成本）。

三种用途：
  --snapshot            读一次真实 QQ 窗口，把"池子 + 可见消息"存成可复现的 fixture；
  默认                  对 fixture 里的每个场景 × 每个策略跑第一段（intent/emotion/danger/suggestion）；
  --replies <场景名>    额外跑第二段（三条回复）。

**故意跑在 CPU 上**（n_gpu_layers=0）：显卡只有 8G，分给实验进程会把正在运行的应用挤爆。
所有策略都在同一后端、同一 seed、同一温度下比较，相对结论有效。

策略：
  v0_current   复刻当前产品逻辑（含已知缺陷：比目标更晚的池内消息会被当"背景"）
  v1_order     前文严格早于目标（按池子时间轴切分），不要后文
  v2_order_after1 / v3_order_after2   在 v1 基础上补 1 / 2 条【后文】
  v4_compact   同 v1，但用精简版系统提示（build_system_prompt(compact=True)）
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message, MessageReader  # noqa: E402

FIXTURE = ROOT / "tmp" / "ctx_fixture.json"

CHAIN = ["哦对的就这种", "痛衣内搭外边格子衬衫然后牛仔裤", "这几个我都有",
         "还差一个眼镜，头巾", "背包再塞点周边海报", "手上最好来个袋子"]


def msg(text: str, side: str, y: int, key: str = "", visible: bool = True,
        is_image: bool = False) -> dict:
    return {"text": text, "side": side, "y0": y, "y1": y + 36,
            "key": key or f"k{abs(hash((text, y))) % 10 ** 8}",
            "visible": visible, "is_image": is_image}


def builtin_scenarios() -> list:
    """SPEC 第 45/46 轮的 cos 道具清单（真实对话）：后三条在接续同一份清单。"""
    msgs = [msg(t, "left" if i % 2 == 0 else "right", 100 + i * 80) for i, t in enumerate(CHAIN)]
    return [{"name": "cos-清单-还差眼镜",
             "messages": msgs, "target": msgs[3]["key"], "pool": None, "virtual_hist": []},
            {"name": "cos-清单-塞海报",
             "messages": msgs, "target": msgs[4]["key"], "pool": None, "virtual_hist": []}]


def snapshot_live(reads: int = 6) -> dict:
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    messages, snap = [], {}
    for _ in range(10):
        messages, snap = reader.read()
        if messages:
            break
        time.sleep(1.5)
    for _ in range(reads):
        messages, snap = reader.read()
        time.sleep(0.5)
    pool = list(reader._history)
    virtual = list(getattr(reader.uia, "history_texts", []))
    reader.close()
    data = {"taken_at": time.strftime("%H:%M:%S"),
            "visible": [msg(m.text, m.side, m.bbox[1], m.msg_key) for m in messages],
            "pool": [msg(m.text, m.side, m.bbox[1], m.msg_key, visible=False, is_image=m.is_image)
                     for m in pool],
            "virtual_hist": virtual}
    return data


# --------------------------------------------------------------------------- #
# 策略实现：都返回 build_context 那种"上下文列表"
# --------------------------------------------------------------------------- #
def _pool_and_visible(scn: dict):
    pool = [Message(msg_key=m["key"], text=m["text"], side=m["side"],
                    bbox=(0, m["y0"], 900, m["y1"]), is_image=m.get("is_image", False))
            for m in (scn.get("pool") or scn["messages"])]
    visible = [Message(msg_key=m["key"], text=m["text"], side=m["side"],
                       bbox=(0, m["y0"], 900, m["y1"]), is_image=m.get("is_image", False))
               for m in scn["messages"] if m.get("visible", True)]
    return pool, visible


def _target(scn: dict, pool) -> Message:
    key = scn["target"]
    return next(m for m in pool if m.msg_key == key)


def assemble(scn: dict, variant: str, limit: int, budget_tokens: int) -> list:
    pool, visible = _pool_and_visible(scn)
    target = _target(scn, pool)
    virtual = [Message(msg_key=f"h{i}", text=t, side="history", bbox=(0, 0, 0, 0))
               for i, t in enumerate(scn.get("virtual_hist") or [])]
    upto = [m for m in visible if m.bbox[1] <= target.bbox[3] and not m.is_image]
    shown = upto[-limit:]
    known = {m.msg_key for m in shown}
    if variant == "v0_current":
        # —— 当前产品逻辑：池内"不在可见集里"的一律当前文（这就是缺陷所在）
        older = [m for m in pool
                 if m.msg_key not in known and m.msg_key != target.msg_key and not m.is_image]
        need = max(0, limit - len(shown))
        older = older[-need:] if need else []
        base = list(virtual[-limit:]) + list(older) + list(shown)
        return base
    # —— 修正版：按池子时间轴切分"前 / 后"
    keys = [m.msg_key for m in pool]
    tidx = keys.index(target.msg_key) if target.msg_key in keys else None
    if tidx is None:                       # 兜底：退回 y 坐标判定
        before_pool = [m for m in pool if m.bbox[1] <= target.bbox[3]]
        after_pool = [m for m in pool if m.bbox[1] > target.bbox[3]]
    else:
        before_pool, after_pool = pool[:tidx], pool[tidx + 1:]
    before_pool = [m for m in before_pool
                   if m.msg_key not in known and m.msg_key != target.msg_key and not m.is_image]
    need = max(0, limit - len(shown))
    before_pool = before_pool[-need:] if need else []
    base = list(virtual[-limit:]) + list(before_pool) + list(shown)
    count = {"v2_order_after1": 1, "v3_order_after2": 2, "v6_slim_after1": 1}.get(variant, 0)
    if count:
        seen = {m.msg_key for m in base} | {target.msg_key}
        after = [m for m in after_pool if m.msg_key not in seen and not m.is_image][:count]
        base = base + [Message(msg_key=m.msg_key, text=m.text, side="after", bbox=m.bbox)
                       for m in after]
    return base


def run_scenario(analyzer: Analyzer, scn: dict, variant: str, cfg, want_replies: bool) -> None:
    limit = cfg.analyzer.context_messages
    budget = cfg.analyzer.context_token_budget
    context = assemble(scn, variant, limit, budget)
    pool, _visible = _pool_and_visible(scn)
    target = _target(scn, pool)
    # 精简版系统提示：本地后端下 build_messages 不会走 compact 分支（那是给 API 省钱的），
    # 所以这里临时替换该方法，只换 system 内容，其它（上下文、格式、语法）完全一致。
    # v4_compact   = 官方 compact（含"英文键名"段落，给 API 用的）
    # v5_local_slim= 真正的本地精简：砍掉危险度长示例，且**不加**英文键名段落（本地有 GBNF 约束）
    import types

    slim = variant in ("v4_compact", "v5_local_slim", "v6_slim_after1")
    original_build = analyzer.build_messages
    if slim:
        def _compact_build(self, context, target):        # noqa: ANN001
            messages = original_build(context, target)
            style = (self.cfg.analysis_style_prompt or "").strip() or app_config.DEFAULT_STYLE_PROMPT
            system = app_config.build_system_prompt(self.cfg, compact=True)
            if variant == "v5_local_slim" or variant == "v6_slim_after1":
                marker = system.find("JSON 的**键名必须严格用下面的英文")
                if marker > 0:
                    system = system[:marker].rstrip()
            messages[0]["content"] = system + "\n\n" + style
            return messages

        analyzer.build_messages = types.MethodType(_compact_build, analyzer)
    try:
        prompt = analyzer.build_messages(context, target)
        chars = sum(len(m["content"]) for m in prompt)
        sys_chars = len(prompt[0]["content"])
        t0 = time.time()
        quick = analyzer.analyze_quick(target, context, use_cache=False, cancel_token=None)
        ms = (time.time() - t0) * 1000
    finally:
        analyzer.build_messages = original_build
    if quick is None:
        print(f"  {variant:<18} 生成失败")
        return
    out = (f"  {variant:<18} ≈{int(chars / 1.5):>4}tok(system {int(sys_chars / 1.5)}) "
           f"{ms:>6.0f}ms｜{quick.emotion} · {quick.intent}｜danger={quick.danger_level}"
           f"｜建议={quick.suggestion[:26]}")
    print(out)
    if want_replies:
        replies = analyzer.analyze_replies(target, context, quick=quick)
        if replies and replies.replies:
            for item in replies.replies:
                print(f"        [{item.style}] {item.text}")


def main() -> int:
    cfg = app_config.load_config()
    if "--snapshot" in sys.argv:
        data = snapshot_live()
        FIXTURE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        vis = [m for m in data["visible"] if m["side"] == "left"]
        print(f"已写入 {FIXTURE}：可见 {len(data['visible'])} 条（对方 {len(vis)}）"
              f"｜池子 {len(data['pool'])} 条｜虚拟历史 {len(data['virtual_hist'])} 条")
        return 0
    if not FIXTURE.exists():
        print("没有 fixture：先跑 --snapshot")
        return 1
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scenarios = builtin_scenarios()
    # 真实快照：池子里**每条"对方"消息**都当一个场景（目标不必在屏幕上，靠池子时间轴定位）
    pool_targets = [m for m in data["pool"] if m["side"] == "left" and not m.get("is_image")]
    for i, target in enumerate(pool_targets, 1):
        scenarios.append({"name": f"live-{i}-{target['text'][:12]}",
                          "messages": data["visible"], "target": target["key"],
                          "pool": data["pool"], "virtual_hist": data.get("virtual_hist") or []})
    variants = ["v0_current", "v1_order", "v2_order_after1", "v3_order_after2",
                "v4_compact", "v5_local_slim", "v6_slim_after1"]
    want_replies = "--replies" in sys.argv
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    print(f"fixture 取自 {data.get('taken_at')}｜场景 {len(scenarios)} 个｜变体 {len(variants)} 个"
          f"｜CPU 后端（n_gpu_layers=0）")
    analyzer = Analyzer(cfg)
    analyzer.cfg.analyzer.n_gpu_layers = 0          # 不抢显存
    analyzer.cfg.analyzer.seed = 1234
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("模型加载失败")
        return 1
    analyzer.extra_seed = 0                          # 固定 seed，只看策略差异
    for scn in scenarios:
        if only and only not in scn["name"]:
            continue
        pool, _v = _pool_and_visible(scn)
        target = _target(scn, pool)
        keys = [m.msg_key for m in pool]
        tidx = keys.index(target.msg_key) if target.msg_key in keys else -1
        print(f"\n===== {scn['name']}｜目标[池内下标 {tidx}]：{target.text} =====")
        for variant in variants:
            run_scenario(analyzer, scn, variant, cfg, want_replies)
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
