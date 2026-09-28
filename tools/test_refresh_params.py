#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 80 轮回归：手动刷新用的一次性采样参数（新种子/升温）"谁用谁还"。

背景（用户实测："点刷新后生成的内容和上次完全一样"）：
  · 原来把上一版三条**原文**喂回提示词要求"避开" —— 4B 反而照抄（见
    `tmp/probe_refresh2.py`），已删；
  · 原来主线程在 `on_analysis_ready()` 里清参数 —— 两段式**第一段**的结果 `partial=False`
    也会走到那里，第二段就退回 0.2 温度；现在改成工作线程整条请求跑完才还原。

本用例守两件事：① 快照/还原语义；② 这两处调用没被改回去。

用法：python tools/test_refresh_params.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message  # noqa: E402


def case_snapshot_release() -> bool:
    analyzer = Analyzer(app_config.load_config())      # 不加载模型，只测参数语义
    ok = analyzer.refresh_snapshot() == (0, None)
    ok = ok and analyzer._temperature() == app_config.load_config().analyzer.temperature

    analyzer.extra_seed, analyzer.temp_override = 4321, 0.9      # 主线程点刷新
    burst = analyzer.refresh_snapshot()                          # 工作线程开始这次请求
    ok = ok and burst == (4321, 0.9) and analyzer._temperature() == 0.9
    analyzer.refresh_release(burst)                              # 请求结束
    ok = ok and (analyzer.extra_seed, analyzer.temp_override) == (0, None)

    # 请求进行期间用户又点了一次刷新 → 还原时不能把新参数吃掉
    analyzer.extra_seed, analyzer.temp_override = 111, 0.9
    burst = analyzer.refresh_snapshot()
    analyzer.extra_seed, analyzer.temp_override = 222, 0.9
    analyzer.refresh_release(burst)
    ok = ok and (analyzer.extra_seed, analyzer.temp_override) == (222, 0.9)

    # 残留参数会被下一次请求带上、并在那次请求结束时清干净（不会永久污染后续分析）
    plain = analyzer.refresh_snapshot()
    analyzer.refresh_release(plain)
    ok = ok and (analyzer.extra_seed, analyzer.temp_override) == (0, None)
    print(f"A 快照/还原语义（含期间再刷新）：{'对' if ok else '错'}")
    return ok


def case_wiring() -> bool:
    worker = (ROOT / "core" / "worker.py").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    analyzer = (ROOT / "core" / "analyzer.py").read_text(encoding="utf-8")
    checks = {
        "worker 请求开始时快照": "refresh_snapshot()" in worker,
        "worker 请求结束才还原": "refresh_release(burst)" in worker,
        "on_analysis_ready 不再清参数": (
            "self.analyzer.temp_override = None" not in main.split("def on_analysis_ready")[1]
            .split("def on_analysis_partial")[0]),
        "刷新不再喂上一版原文": "上一版已经用过的三条回复" not in analyzer,
        "刷新能绕过 pending 复用（点刷新不再没反应）": (
            "self._request_analysis(self.target,\n" in main.replace("\r\n", "\n")
            and "force: bool = False" in main),
    }
    ok = all(checks.values())
    print(f"B 调用点接线：{'对' if ok else '错'}（{'、'.join(k for k, v in checks.items() if not v) or '全部通过'}）")
    return ok


def case_vary_options() -> bool:
    """第 81 轮：常规生成的**第二段**每次换随机 seed（避免同一条消息重算时开头被锁死）。

    用替身 `_generate` 抓 seed，不需要加载模型。
    """
    analyzer = Analyzer(app_config.load_config())
    analyzer.llm = object()                     # 骗过 `if self.llm is None` 的早退
    canned = ('{"reply_options":[{"style":"认同·附和","text":"确实啊"},'
              '{"style":"反调·顶回","text":"这可不行"},'
              '{"style":"吐槽·玩笑","text":"太离谱了吧"}]}')
    seeds: list = []
    analyzer._generate = lambda messages, temperature, seed, **k: (   # type: ignore[assignment]
        seeds.append(seed) or (canned, 1.0, 2.0))
    target = Message(msg_key="t", text="在吗", side="left", bbox=(0, 0, 300, 40))

    analyzer.cfg.analyzer.vary_options = True
    for _ in range(5):
        analyzer.analyze_replies(target, [target])
    varied = len(set(seeds))

    seeds.clear()
    analyzer.cfg.analyzer.vary_options = False
    for _ in range(5):
        analyzer.analyze_replies(target, [target])
    fixed = len(set(seeds))
    ok = varied >= 3 and fixed == 1
    print(f"C 第二段每次换种子：{'对' if ok else '错'}"
          f"（开=5 次里出现 {varied} 个不同 seed；关=固定 {fixed} 个）")
    return ok


def main() -> int:
    results = [case_snapshot_release(), case_wiring(), case_vary_options()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
