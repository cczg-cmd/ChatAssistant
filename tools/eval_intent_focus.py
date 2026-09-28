#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B 实测：相邻消息被"当成同一个意思"到底是提示词问题还是上下文问题。

背景：系统提示词写的是"最后一行标了 `<<<` 需要分析的就是这条"，
但 `build_messages` 在 context_target_first=True 时把**目标放在第一行**、且没有 `<<<` 标记。
模型找不到"要做题的那条"，就会去抓相邻消息/整段话题。

本脚本对同一批真实样本跑 3 个变体，只有"提示词文本 / 目标位置"不同：
  baseline : 当前 config 里的提示词 + target 在最前（现网口径）
  v2-first : 改写后的提示词（与真实格式一致）+ target 在最前
  v2-last  : 改写后的提示词 + target 在最后（带 `<<<` 标记）
评测指标（自动）：
  ① 焦点：intent 的二字词与"目标行"的重合数 vs 与"只在背景里出现的词"的重合数；
     背景重合更多 → 判定 drift（跑偏到相邻消息）。
  ② 问句错配：目标行没有问号/疑问词，而 intent 以"询问/问/打听"开头 → 判错（把陈述句当提问）。
用法：python tmp/eval_intent_focus.py --prompt-file config --order first --out tmp/eval_x.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
import core.analyzer as analyzer_mod  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message, MessageReader  # noqa: E402

# 先注入 CUDA 运行库目录，llama_cpp 才能 import（与 Analyzer.load 同款）
import os as _os  # noqa: E402

_cuda_dirs = analyzer_mod._cuda_bin_dirs(app_config.load_config())
for _d in _cuda_dirs:
    try:
        _os.add_dll_directory(_d)
    except Exception:
        pass
_os.environ["PATH"] = ";".join(_cuda_dirs) + ";" + _os.environ.get("PATH", "")
from llama_cpp import Llama  # noqa: E402

QUESTION_MARKS = "?？"
QUESTION_WORDS = ("吗", "嘛", "么", "吧", "呢", "怎么", "为什么", "多少", "几",
                  "是否", "有没有", "啥", "什么", "哪")
ASK_PREFIXES = ("询问", "问", "打听", "咨询", "求问")

# 手写回归样本（覆盖"陈述句被当成提问""短回应""感谢"三类坑），用真实聊天里出现过的原句
HAND_SAMPLES = [
    ("H1 陈述被当提问", "我现在打开游戏就会掉网",
     ["对方: 你电脑有去清过灰嘛", "我: 没有啊", "对方: 我电脑之前也这样，清了灰就好了"]),
    ("H2 转述消息", "问豆包说去清灰试试",
     ["对方: 我现在打开游戏就会掉网", "我: 那怎么办", "对方: 我问了豆包"]),
    ("H3 极短回应", "嗯",
     ["我: 那明天下午三点老地方见", "我: 我先过去了"]),
    ("H4 感谢", "太谢谢你了",
     ["我: 我把你那个文件也带过去了", "对方: 真的假的"]),
]


def bigrams(text: str) -> set:
    clean = "".join(ch for ch in (text or "") if ch.strip())
    return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))}


def is_question(text: str) -> bool:
    return any(mark in text for mark in QUESTION_MARKS) or any(w in text for w in QUESTION_WORDS)


def evaluate(target_text: str, background: list, intent: str) -> dict:
    tb, ib = bigrams(target_text), bigrams(intent)
    bg_only = set()
    for line in background:
        bg_only |= bigrams(line)
    bg_only -= tb
    hit_target, hit_bg = len(tb & ib), len(bg_only & ib)
    mismatch = (not is_question(target_text)) and str(intent).startswith(ASK_PREFIXES)
    return {"hit_target": hit_target, "hit_background_only": hit_bg,
            "drift": hit_bg > hit_target, "question_mismatch": bool(mismatch)}


def collect_samples(reader: MessageReader) -> list:
    samples = []
    messages = []
    for _ in range(10):
        messages, snapshot = reader.read()
        if messages:
            break
        time.sleep(1.5)
    for msg in messages:
        if msg.side != "left" or msg.is_image or len(msg.text.strip()) < 2:
            continue
        context = reader.build_context(messages, msg)
        background = [f"{'对方' if m.side == 'left' else ('我' if m.side == 'right' else '历史')}: {m.text}"
                      for m in context if m.msg_key != msg.msg_key]
        samples.append((f"LIVE {msg.text[:14]}", msg.text, background))
    return samples


def install_layout(analyzer_mod, layout: str, cfg) -> None:
    """layout=both：背景放在中间，**目标行再在最后出现一次**。

    动机：模型生成答案时对"紧邻的最后几行"注意力最强。target-first 时最后读到的是
    "目标前面那条消息" → 实测会把上一条的意思当成目标的意思（用户反馈的"相邻几条
    被当成同一个意思"）。把目标行补在最后，位置注意力就落回目标本身。
    """
    if layout != "both":
        return
    original = analyzer_mod.Analyzer.build_messages

    def build_messages_with_tail(self, context, target):        # noqa: ANN001
        messages = original(self, context, target)
        user = messages[1]["content"]
        speaker = {"left": "对方", "right": "我", "history": "历史"}
        tail = (f"\n【需要分析的消息】仍然是上面第一条（{speaker.get(target.side, '历史')}）："
                f"{target.text}   <<< 只分析这一条")
        messages[1]["content"] = user + tail
        return messages

    analyzer_mod.Analyzer.build_messages = build_messages_with_tail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-file", default="config")
    parser.add_argument("--order", choices=["first", "last"], default="first")
    parser.add_argument("--layout", choices=["plain", "both"], default="plain",
                        help="both = 目标行同时出现在最前与最后（抵消'最后一行'注意力偏置）")
    parser.add_argument("--out", default="tmp/eval_intent.json")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--model", default="", help="覆盖 cfg.paths.model（换模型对比）")
    parser.add_argument("--chat-format", default="",
                        help="强制 chat 模板，例如 chatml（用来关掉 Qwen3 的思考脚手架）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    cfg = app_config.load_config()
    cfg.analyzer.context_target_first = (args.order == "first")
    if args.model:
        cfg.paths.model = str(Path(args.model).resolve())
    if args.chat_format:
        import functools
        analyzer_mod.Llama = functools.partial(Llama, chat_format=args.chat_format)
        print(f"强制 chat_format={args.chat_format}")

    if args.prompt_file == "config":
        prompt_name = "config.py 当前提示词"
    else:
        analyzer_mod.SYSTEM_PROMPT_TEMPLATE = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        prompt_name = f"{args.prompt_file}（改写版）"

    print(f"变体：提示词={prompt_name}｜目标位置={'最前(first)' if args.order == 'first' else '最后(last)'}")
    install_layout(analyzer_mod, args.layout, cfg)
    samples = collect_samples(MessageReader(cfg))
    samples = (samples + HAND_SAMPLES)[:args.limit]
    print(f"样本 {len(samples)} 条（实时 {len(samples) - len([s for s in samples if s[0].startswith('H')])} + 手写回归）")

    analyzer = Analyzer(cfg)
    if not analyzer.load():
        print(f"模型加载失败：{analyzer.last_error}")
        return 1
    print(f"模型 {analyzer.model_path.name} 加载 {analyzer.load_s}s\n")

    rows, t_all = [], []
    for name, target_text, background in samples:
        target = Message(msg_key=f"eval-{abs(hash(name))}", text=target_text, side="left",
                         bbox=(0, 0, 300, 40))
        ctx = [Message(msg_key=target.msg_key, text=target_text, side="left", bbox=(0, 0, 300, 40))]
        for index, line in enumerate(background):
            side = "left" if line.startswith("对方") else ("right" if line.startswith("我") else "history")
            ctx.append(Message(msg_key=f"bg{index}", text=line.split(": ", 1)[-1], side=side,
                               bbox=(0, 0, 0, 0)))
        result = analyzer.analyze_quick(target, ctx, use_cache=False)
        if result is None:
            print(f"  ! {name} 生成失败")
            continue
        metrics = evaluate(target_text, background, result.intent)
        t_all.append(result.total_ms)
        rows.append({"name": name, "target": target_text, "intent": result.intent,
                     "emotion": result.emotion, "danger": result.danger_level,
                     "ms": result.total_ms, **metrics})
        flag = "跑偏!" if metrics["drift"] else ("问句错配!" if metrics["question_mismatch"] else "OK  ")
        print(f"  [{flag}] {name}")
        print(f"        目标：{target_text}")
        print(f"        intent={result.intent}｜emotion={result.emotion}｜danger={result.danger_level}"
              f"｜{result.total_ms:.0f}ms｜命中目标词 {metrics['hit_target']} vs 仅背景词 {metrics['hit_background_only']}")

    drift = sum(1 for r in rows if r["drift"])
    mismatch = sum(1 for r in rows if r["question_mismatch"])
    summary = {"prompt": prompt_name, "order": args.order, "samples": len(rows),
               "drift": drift, "question_mismatch": mismatch,
               "avg_ms": round(sum(t_all) / len(t_all), 1) if t_all else None,
               "rows": rows}
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n合计：跑偏 {drift}/{len(rows)}｜问句错配 {mismatch}/{len(rows)}"
          f"｜平均 {summary['avg_ms']}ms → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
