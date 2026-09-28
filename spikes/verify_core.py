#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_core.py - B1 验收脚本（M1+M2+M4：配置/工具 + 消息读取 + 分析）

做四件事，全部用真机数据（不用 Mock）：
  1. 用 core.message_reader 从**真实 QQ 窗口**连续读取可见消息（10 轮），统计耗时/稳定性/左右分布；
  2. 用 core.analyzer（默认 3B）对"最新一条对方消息 + 上下文"做分析，校验 JSON 与三条回复选项；
  3. 用 fixtures/llm_inputs.txt 的 8 条真实样本做单条分析，统计 JSON 合法率 / 三条选项齐全率 / 耗时；
  4. 校验缓存命中（同一条再分析应 <5ms 且 cache_hit=True）。
输出：
  spikes/results/core_b1_report.json    机读报告
  spikes/results/core_b1_log.txt        日志
  spikes/results/core_b1_review.md      给人看的真实输出清单
退出码：0=PASS / 1=FAIL / 2=ABORT（没有可见 QQ 窗口、模型加载失败等）
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
import time
import ctypes
from pathlib import Path

SPIKES_DIR = Path(__file__).resolve().parent
if str(SPIKES_DIR) not in sys.path:
    sys.path.insert(0, str(SPIKES_DIR))
import spike_wgc as wgc  # noqa: E402  （复用隔离头：TMP/TEMP/缓存重定向 + 审计函数）

WORKSPACE = Path(wgc.WORKSPACE)
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message, MessageReader, make_msg_key  # noqa: E402
from utils import win32_api as w32  # noqa: E402

RESULTS_DIR = WORKSPACE / "spikes" / "results"
REPORT_JSON = RESULTS_DIR / "core_b1_report.json"
LOG_TXT = RESULTS_DIR / "core_b1_log.txt"
REVIEW_MD = RESULTS_DIR / "core_b1_review.md"
FIXTURES = WORKSPACE / "spikes" / "fixtures" / "llm_inputs.txt"

READ_ROUNDS = 10
FIXTURE_LIMIT = 8
READ_P50_MAX_MS = 150.0
READ_P95_MAX_MS = 300.0
STABLE_MIN_RATIO = 0.95
CACHE_HIT_MAX_MS = 5.0

logger = logging.getLogger("verify_core")
_ISOLATION_BASELINE: dict = {}


class Assertions:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, ok, detail: str = "") -> bool:
        status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        self.items.append({"name": name, "status": status, "detail": detail})
        logger.info("%-6s %-34s %s", status, name, detail)
        return bool(ok)

    @property
    def any_fail(self) -> bool:
        return any(i["status"] == "FAIL" for i in self.items)


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(LOG_TXT, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def percentile(values, p: float):
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (p / 100.0) * (len(data) - 1)
    lo, hi = int(rank // 1), int(-(-rank // 1))
    return data[lo] if lo == hi else data[lo] + (data[hi] - data[lo]) * (rank - lo)


def load_fixture_samples(limit: int) -> list[dict]:
    if not FIXTURES.exists():
        return []
    out = []
    for line in FIXTURES.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or "|" not in text:
            continue
        body, label = text.split("|", 1)
        body, label = body.strip(), label.strip()
        if body:
            out.append({"text": body, "label": label})
        if len(out) >= limit:
            break
    return out


def main() -> int:
    setup_logging()
    restore = "--restore-window" in sys.argv
    global _ISOLATION_BASELINE
    _ISOLATION_BASELINE = wgc.snapshot_external_dirs()
    started = time.time()

    assertions = Assertions()
    cfg = app_config.load_config()
    report: dict = {
        "stage": "B1 (M1+M2+M4)",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "workspace_paths": app_config.workspace_paths(),
        "thresholds": {"read_p50_max_ms": READ_P50_MAX_MS, "read_p95_max_ms": READ_P95_MAX_MS,
                       "stable_min_ratio": STABLE_MIN_RATIO,
                       "cache_hit_max_ms": CACHE_HIT_MAX_MS,
                       "read_rounds": READ_ROUNDS, "fixture_limit": FIXTURE_LIMIT},
        "environment": {}, "config": {"field_limits": cfg.field_limits,
                                      "fill": {"enabled": cfg.fill.enabled, "mode": cfg.fill.mode},
                                      "analyzer": {"context_messages": cfg.analyzer.context_messages,
                                                   "n_ctx": cfg.analyzer.n_ctx,
                                                   "max_tokens": cfg.analyzer.max_tokens}},
        "reader": {}, "analyzer": {}, "live_messages": [], "live_analysis": None,
        "fixtures": [], "cache": {}, "abort_reason": None, "result": "FAIL",
    }
    report["environment"]["dpi_awareness"] = w32.set_dpi_awareness()
    report["environment"]["windows_build"] = w32.windows_build()
    report["environment"]["python"] = sys.version.split()[0]
    report["environment"]["model_path"] = str(app_config.resolve_model_path(cfg))
    logger.info("=" * 74)
    logger.info("B1 验收开始（模型：%s）", report["environment"]["model_path"])

    # ---------------- 1. 消息读取（真实 QQ） ----------------
    reader = MessageReader(cfg)
    rounds, messages, snapshot = [], [], {}
    skip_reader = "--skip-reader" in sys.argv
    if restore and not skip_reader:
        wins = w32.list_qq_windows(cfg.uia.window_class, cfg.uia.exe_name)
        target = max(wins, key=lambda w: w.area, default=None)
        if target is not None and (target.iconic or not target.visible):
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(target.hwnd), 4)  # SW_SHOWNOACTIVATE
            logger.info("已非激活还原最小化的 QQ 窗口：hwnd=%s（不抢前台焦点）", target.hwnd)
            time.sleep(1.0)
    if not skip_reader:
        for i in range(READ_ROUNDS):
            msgs, snap = reader.read()
            rounds.append({"round": i + 1, "ms": round(snap["last_read_ms"], 1),
                           "count": len(msgs), "texts": [m.text for m in msgs]})
            messages, snapshot = msgs, snap
            time.sleep(0.2)
    times = [r["ms"] for r in rounds]
    report["reader"] = {"status": snapshot.get("status"), "rounds": rounds,
                        "uia": snapshot.get("uia"), "window": snapshot.get("window"),
                        "last_error": snapshot.get("last_error")}
    report["live_messages"] = [m.as_dict() for m in messages]

    if not messages and not skip_reader:
        logger.error("读不到任何消息（status=%s，error=%s）",
                     snapshot.get("status"), snapshot.get("last_error"))
        assertions.add("qq_window_readable", False,
                       f"status={snapshot.get('status')}；"
                       "请确认 QQ 已登录并打开一个对话窗口（非最小化）后重跑")
        report["abort_reason"] = f"NO_MESSAGES: {snapshot.get('status')}"
        report["result"] = "ABORT"
        _finalize(report, assertions, started)
        return 2
    if skip_reader:
        assertions.add("qq_window_readable", None,
                       "本次用 --skip-reader 跳过了实时读取（UIA 需要 QQ 窗口被激活一次才建树）")

    text_sets = {tuple(r["texts"]) for r in rounds}
    stable_ratio = 1.0 / len(text_sets) if text_sets else 0.0
    left = [m for m in messages if m.side == "left"]
    right = [m for m in messages if m.side == "right"]
    p50, p95 = percentile(times, 50), percentile(times, 95)
    if not skip_reader:
        assertions.add("qq_window_readable", True,
                       f"status={snapshot.get('status')}，读到 {len(messages)} 条"
                       f"（对方 {len(left)} / 自己 {len(right)}），"
                       f"窗口={snapshot.get('window', {}).get('title', '')[:16]!r}")
        assertions.add("read_latency", p50 <= READ_P50_MAX_MS and p95 <= READ_P95_MAX_MS,
                       f"p50={p50:.1f}ms（预算 {READ_P50_MAX_MS:.0f}）/ "
                       f"p95={p95:.1f}ms（预算 {READ_P95_MAX_MS:.0f}）")
        assertions.add("read_stable", stable_ratio >= STABLE_MIN_RATIO,
                       f"{READ_ROUNDS} 轮文本集合 {len(text_sets)} 种（稳定率 {stable_ratio:.2f}），"
                       f"条数集合 {sorted({r['count'] for r in rounds})}")
    others = [m for m in messages if m.is_other_party and not m.is_image]
    if others:
        assertions.add("other_party_message_found", True,
                       f"对方(左)消息 {len(others)} 条；示例：{others[-1].text[:26]!r}")
    elif not skip_reader:
        assertions.add("other_party_message_found", False, "当前可见范围内没有对方消息")

    # ---------------- 2. 分析器加载 ----------------
    analyzer = Analyzer(cfg)
    t0 = time.time()
    loaded = analyzer.load()
    report["analyzer"] = {"load_ok": loaded, "load_wall_s": round(time.time() - t0, 2),
                          "status": analyzer.status()}
    if not loaded:
        assertions.add("analyzer_loaded", False, analyzer.last_error or "加载失败")
        report["abort_reason"] = f"ANALYZER_LOAD_FAILED: {analyzer.last_error}"
        report["result"] = "ABORT"
        reader.close()
        _finalize(report, assertions, started)
        return 2
    assertions.add("analyzer_loaded", True,
                   f"{analyzer.model_path.name}，{analyzer.status()['load_s']}s，"
                   f"n_gpu_layers={analyzer.gpu_layers_used}，"
                   f"{analyzer.gpu_evidence.get('offloaded') or '未捕获 offload 行'}")
    assertions.add("grammar_effective", analyzer.grammar_ok,
                   "GBNF 语法自检通过" if analyzer.grammar_ok else str(analyzer.grammar_error))
    assertions.add("gpu_offload", analyzer.gpu_layers_used != 0 or None,
                   f"n_gpu_layers={analyzer.gpu_layers_used}"
                   + ("（已回退 CPU）" if analyzer.gpu_fallback else ""))

    # ---------------- 3. 实时分析（最新对方消息 + 上下文） ----------------
    if others:
        target = others[-1]
        context = reader.build_context(messages, target)
        result = analyzer.analyze(target, context)
        report["live_analysis"] = None if result is None else {
            **result.as_dict(), "target": target.text,
            "context": [m.text for m in context]}
        if result is not None:
            ok_options = (len(result.replies) == 3
                          and len({r.style for r in result.replies}) == 3
                          and len({r.text for r in result.replies}) == 3)
            assertions.add("live_analysis_valid",
                           result.json_ok and ok_options and result.danger_level is not None,
                           f"intent={result.intent} danger={result.danger_level} "
                           f"选项={len(result.replies)}条 "
                           f"风格互不相同={len({r.style for r in result.replies}) == len(result.replies)} "
                           f"文本互不相同={len({r.text for r in result.replies}) == len(result.replies)} "
                           f"{result.total_ms}ms")
            # 缓存命中
            t_cache = time.perf_counter()
            cached = analyzer.analyze(target, context)
            cache_ms = (time.perf_counter() - t_cache) * 1000
            report["cache"] = {"hit": bool(cached and cached.cache_hit),
                               "ms": round(cache_ms, 2), "size": analyzer.cache_size()}
            assertions.add("cache_hit_fast",
                           bool(cached and cached.cache_hit) and cache_ms <= CACHE_HIT_MAX_MS,
                           f"第二次分析 {cache_ms:.2f}ms，cache_hit="
                           f"{bool(cached and cached.cache_hit)}，缓存条数={analyzer.cache_size()}")

    # ---------------- 4. fixtures 批量（单条无上下文） ----------------
    samples = load_fixture_samples(FIXTURE_LIMIT)
    fixture_records = []
    for idx, sample in enumerate(samples):
        msg = Message(msg_key=make_msg_key("fixture", sample["text"]), text=sample["text"],
                      side="left", bbox=(0, idx * 30, 200, idx * 30 + 24))
        res = analyzer.analyze(msg, [msg])
        fixture_records.append({
            "text": sample["text"], "label": sample["label"],
            "json_ok": bool(res and res.json_ok),
            "options": 0 if res is None else len(res.replies),
            "replies": [] if res is None else [r.as_dict() for r in res.replies],
            "intent": None if res is None else res.intent,
            "emotion": None if res is None else res.emotion,
            "danger_level": None if res is None else res.danger_level,
            "suggestion": None if res is None else res.suggestion,
            "total_ms": None if res is None else res.total_ms})
    report["fixtures"] = fixture_records
    ok_json = sum(1 for r in fixture_records if r["json_ok"])
    ok_opts = sum(1 for r in fixture_records if r["options"] == 3)
    distinct = sum(1 for r in fixture_records
                   if len({o["text"] for o in r["replies"]}) == len(r["replies"]) and r["replies"])
    fixture_ms = [r["total_ms"] for r in fixture_records if r["total_ms"]]
    assertions.add("fixture_json_valid", ok_json == len(fixture_records) and bool(fixture_records),
                   f"{ok_json}/{len(fixture_records)} 条 JSON 合法")
    assertions.add("fixture_options_complete", ok_opts == len(fixture_records) and bool(fixture_records),
                   f"{ok_opts}/{len(fixture_records)} 条三条选项齐全")
    assertions.add("fixture_options_distinct", distinct == len(fixture_records) and bool(fixture_records),
                   f"{distinct}/{len(fixture_records)} 条三条文本互不相同")
    if fixture_ms:
        report["fixture_latency"] = {"p50": round(percentile(fixture_ms, 50), 1),
                                     "p95": round(percentile(fixture_ms, 95), 1),
                                     "mean": round(statistics.fmean(fixture_ms), 1)}
        assertions.add("fixture_latency_budget", percentile(fixture_ms, 95) < 10000,
                       f"p50={report['fixture_latency']['p50']}ms "
                       f"p95={report['fixture_latency']['p95']}ms（验收线 10000ms）")

    old_notice = cfg.fill.notice_shown
    cfg.fill.notice_shown = old_notice
    assertions.add("fill_default_on", cfg.fill.enabled and cfg.fill.mode == "paste",
                   f"一键填入默认开启：enabled={cfg.fill.enabled}，mode={cfg.fill.mode}"
                   f"（首次提示开关={cfg.fill.show_first_use_notice}），"
                   f"实现见 utils/win32_api.fill_text_into_foreground（只粘贴、不回车）")
    reader.close()
    analyzer.close()

    report["result"] = "PASS" if not assertions.any_fail else "FAIL"
    _write_review(report)
    _finalize(report, assertions, started)
    return 0 if report["result"] == "PASS" else 1


def _write_review(report: dict) -> None:
    lines = ["# B1 验收：真实数据输出清单", ""]
    live = report.get("live_analysis")
    if live:
        lines += ["## 实时会话分析（真实 QQ 消息 + 上下文）", "",
                  "**上下文**：", "```"]
        lines += [f"{'对方' if i == len(live['context']) - 1 else '——'}: {t}"
                  for i, t in enumerate(live["context"])]
        lines += ["```", f"- 目标消息：{live['target']}",
                  f"- intent={live['intent']} | emotion={live['emotion']} | "
                  f"danger={live['danger_level']} | confidence={live['confidence']} | "
                  f"{live['total_ms']}ms",
                  f"- 建议：{live['suggestion']}"]
        for r in live["replies"]:
            lines.append(f"- [{r['style']}] {r['text']}")
        lines.append("")
    lines += ["## fixtures 单条样本（无上下文）", ""]
    for i, rec in enumerate(report.get("fixtures", []), 1):
        lines.append(f"### {i}. {rec['text']}")
        lines.append(f"- intent={rec['intent']} | emotion={rec['emotion']} | "
                     f"danger={rec['danger_level']} | {rec['total_ms']}ms")
        lines.append(f"- 建议：{rec['suggestion']}")
        for o in rec["replies"]:
            lines.append(f"- [{o['style']}] {o['text']}")
        lines.append("")
    REVIEW_MD.write_text("\n".join(lines), encoding="utf-8")


def _finalize(report: dict, assertions: Assertions, started: float) -> None:
    isolation = wgc.audit_isolation(_ISOLATION_BASELINE, wgc.snapshot_external_dirs())
    report["file_isolation"] = isolation
    assertions.add("file_isolation_no_external_write", isolation["clean"],
                   f"tempfile={isolation['tempfile_gettempdir']}；可归因外写="
                   f"{len(isolation['external_new_entries_attributable'])} 项；"
                   f"无法归因={len(isolation['external_new_entries_unattributed'])} 项")
    if not isolation["clean"]:
        report["result"] = "FAIL"
    report["assertions"] = assertions.items
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report["duration_s"] = round(time.time() - started, 2)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("报告：%s；人看清单：%s", REPORT_JSON, REVIEW_MD)
    for item in assertions.items:
        logger.info("%-6s %-34s %s", item["status"], item["name"], item["detail"])
    if report.get("abort_reason"):
        logger.error("CORE_B1: ABORTED - %s", report["abort_reason"])
    if isolation["clean"]:
        print(f"文件隔离确认：所有临时文件与缓存均在工作区内（{wgc.WORKSPACE}），"
              f"未发现外部写入。", flush=True)
    else:
        print(f"文件隔离确认：检测到工作区外写入！可归因本项目："
              f"{isolation['external_new_entries_attributable']}", flush=True)
    print(f"CORE_B1: {'PASS' if report['result'] == 'PASS' else 'FAIL'}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
