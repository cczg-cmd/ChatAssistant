#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 54 轮 prompt 改动的开销：用模型自带分词器量"新增/删除"片段的 token。

做法：把本轮 apply_patch 里的每一段新增、删除文本原样列出来，直接数 token；
净增量 = 新增 - 删除。比"新旧整份提示词对比"更省事，也不会漏。
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

ADDED = {
    "HEAD-发散≠编造": (
        "发散不等于编造：回复建议和回复选项可以反着接、夸张、把话头拐歪，但**不能造事实**——"
        "没发生过的事、不知道的信息不能当成真的写（别编“我查过了/他们说…”这类没做过的动作），"
        "编造的东西不算发散，只会牛头不对马嘴。\n"
    ),
    "HEAD-第一步字面": (
        "第一步先把那一行的**字面意思**看懂：他到底在问什么/说什么/想要什么？"
        "不要因为语气词就判定成负面；只是感叹或夸赞，就照实理解。\n"
    ),
    "HEAD-第二步改写": "第二步写第一条字段：它**不是**这一行的复述，也**不是心情**",
    "intent-言外之意": (
        "**言外之意、没直说的目的都要写进去**，别只把字面翻一遍（**不是**复述这句话说了啥、"
        "**不是**写话题名、"
    ),
    "intent-第四例": "\"行吧你忙\"→\"有点不乐意、等你拦他\"",
    "intent-不硬编": "确实没有言外之意（就是问个事实、答个话）时，照实写，别硬编动机。",
    "suggestion-发散": (
        "**别给最常规、最顺口的那句接话**——那种回答最容易因为上下文不够而牛头不对马嘴；"
        "可以反着接、把话头拐到一个意想不到的小方向、拿夸张或荒谬的比喻把它化解掉、"
        "或者特别随意地一句带过；走哪一路按这句话的语境定，不套固定套路。"
        "对方问的事你不知道答案时别编一个出来（反问一句、或者回头再查都行）。"
        "**别补上文没有的细节**（别凭空补出地点、时间、人名、数字）。"
    ),
    "option-发散": (
        "**不要给最常规、最“应该这么回”的那三句**——那类回答最容易因为上下文不够而牛头不对马嘴；"
        "试着反着接、把话头拐到一个意想不到的方向、用夸张或荒谬的比喻化解、或者特别随意地一带而过；"
        "哪条走哪一路由你按这句的语境定，不套固定模板。"
        "对方问的事你不知道答案时，别替他编一个（反问、打岔、直说不了解都行）。"
        "**要接得住上文的话题**（说的还是同一件事，别另起话题；"
    ),
    "analyzer-选项方向注入": (
        "\n（这一次请优先从这几个方向里挑 3 个**互不相同**的："
        "关心、吐槽、反问、玩笑、冷淡；其中**至少一条要走「用荒谬的比喻／夸张得不像真的」"
        "这类不按常理的路子**，别三条都给最常规的接话；"
        "下次会换其他方向，不要总是用同一组）"
    ),
    "analyzer-建议角度注入": "\n（这条回复建议尽量从\"拿这件事开个夸张的玩笑\"这个角度写，开头措辞换一种；**这个角度词本身不要写进正文**）",
}

REMOVED = {
    "HEAD-旧第一步": (
        "第一步先理解对方这句话的**字面意思**：他到底在问什么/说什么/想要什么？"
        "不要过度解读，不要因为语气词就判定成负面；如果只是感叹或夸赞，就照实概括。\n"
    ),
    "HEAD-旧开头": "第一条字段写的是**对方这句话真正想干什么**——要读出**言外之意、没直说的目的**，别只把字面翻一遍。例如：",
    "HEAD-旧但不要过度解读": (
        "但**不要过度解读**：如果这一行确实没有言外之意（就是问个事实、答个话），照实写就行，别硬编动机。"
        "**不是**这一行的复述，也**不是心情**——心情由后面的情绪字段单独写。\n"
    ),
    "intent-旧": "用**口语**概括这句话**想干什么**（不是复述这句话说了啥、不是写话题名、**也不要写心情**——心情交给情绪字段单独写），",
    "suggestion-旧结尾": "**别用\"先…\"\"建议…\"\"可以…\"\"注意…\"开头**，也别把\"情绪/意图/危险度\"写进去",
    "option-旧结构": (
        "**要接得住上文的话题**（对方在列清单就跟着清单说，别另起话题）。"
        "三条的**角度要明显不同**、每次换组合（别固定同一组）；style 自己起 2-4 字。"
        "**不知道的细节不要编**（别凭空补出具体地点、时间、人名、数字——就用“上次那件事”这种原话）；"
    ),
    "analyzer-旧选项注入": (
        "\n（这一次请优先从这几个方向里挑 3 个**互不相同**的："
        "关心、吐槽、反问、玩笑、冷淡；下次会换其他方向，不要总是用同一组）"
    ),
    "analyzer-旧建议注入": "\n（这条回复建议尽量从\"催他给个准信\"这个角度写，开头措辞换一种）",
}


def main() -> int:
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    llm = analyzer._load_llm() if hasattr(analyzer, "_load_llm") else None
    if llm is None and not analyzer.load(app_config.resolve_model_path(cfg)):
        print("模型加载失败:", analyzer.last_error)
        return 1
    llm = analyzer.llm

    def count(text: str) -> int:
        return len(llm.tokenize(text.encode("utf-8")))

    plus = sum(count(t) for t in ADDED.values())
    minus = sum(count(t) for t in REMOVED.values())
    for name, text in ADDED.items():
        print(f"  + {name}: {len(text)} 字 / {count(text)} token")
    for name, text in REMOVED.items():
        print(f"  - {name}: {len(text)} 字 / {count(text)} token")
    print(f"\n新增合计 {plus} token，删除合计 {minus} token，净增约 {plus - minus} token")
    for name in ("默认配置", "猫娘配置"):
        app_config.apply_profile(cfg, name)
        prompt = app_config.build_system_prompt(cfg)
        print(f"{name}系统提示词：{len(prompt)} 字 / {count(prompt)} token")
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
