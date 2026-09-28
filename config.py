#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config.py - ChatAssistant 配置层（SPEC 5.7 / M1）

职责：
  1) 文件隔离：把 TMP/TEMP/HF_HOME/PIP_CACHE_DIR... 全部钉在工作区内（早于任何第三方库导入）；
  2) 默认配置 + 用户在 config.json 里的覆盖（模型路径、prompt、字段上限、danger 锚点、
     展示模板、一键填入开关等都在这里，改行为不用改代码）；
  3) 路径解析：SPEC 5.1 要求模型与 exe 同级（打包后 dist/ChatAssistant/models/），
     开发期退回工作区 models/。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# 0. 文件隔离（必须在任何第三方库导入之前执行）
# --------------------------------------------------------------------------- #
def _app_dir() -> Path:
    """打包后 = exe 所在目录；开发期 = 仓库根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()


def _detect_workspace(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "SPEC.md").exists():
            return candidate
    return start


WORKSPACE = _detect_workspace(APP_DIR)
TMP_DIR = WORKSPACE / "tmp"
CACHE_DIR = WORKSPACE / "cache"
LOG_DIR = WORKSPACE / "logs"
RESULTS_DIR = WORKSPACE / "spikes" / "results"
CONFIG_JSON = WORKSPACE / "config.json"

for _d in (TMP_DIR, CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

os.environ["TMP"] = str(TMP_DIR)
os.environ["TEMP"] = str(TMP_DIR)
os.environ["TMPDIR"] = str(TMP_DIR)
tempfile.tempdir = str(TMP_DIR)
os.environ["HF_HOME"] = str(CACHE_DIR / "huggingface")
os.environ["MODELSCOPE_CACHE"] = str(CACHE_DIR / "modelscope")
os.environ["TORCH_HOME"] = str(CACHE_DIR / "torch")
os.environ["PIP_CACHE_DIR"] = str(CACHE_DIR / "pip")
os.environ["NUMBA_CACHE_DIR"] = str(CACHE_DIR / "numba")
os.environ["MPLCONFIGDIR"] = str(LOG_DIR / "matplotlib")
os.environ.setdefault("XDG_CACHE_HOME", str(CACHE_DIR / "xdg"))


# --------------------------------------------------------------------------- #
# 1. 常量（危险度锚点 / 字段上限 / 默认 prompt）
# --------------------------------------------------------------------------- #
DANGER_ANCHORS = (
    ("0-2", "纯日常：寒暄、约事、答话，没有情绪指向"),
    ("3-4", "有情绪但不针对你：普通请求、吐槽发泄、轻微不满"),
    ("5-6", "明确的不满、催促、追问，需要认真回应"),
    ("7-8", "指向你的追责、翻旧账、质疑，容易吵起来"),
    ("9-10", "明确冲突：最后通牒、人身攻击、要翻脸"),
)
DANGER_ANCHOR_TEXT = "；".join(f"{rng} {desc}" for rng, desc in DANGER_ANCHORS)

# 收紧长度（用户要求）：输出越短 → 推理越快、面板越不容易显示不全
#  第 80 轮：intent 14 → 20 → 15 → **13**（用户口径："intent 字数限制在 13 个以内，不然太长了"）。
#  用户实测"AI 生成的配置把意图写到 60 字、实际被这里的硬上限砍掉，面板标题（情绪·意图）挤在一起"，
#  15 字在标题行里仍然偏长 → 收到 13（模板里同步写 ≤13 字）。
FIELD_LIMITS = {"intent": 13, "emotion": 10, "suggestion": 40,
                "reply_style": 6, "reply_text": 50}
REPLY_OPTION_COUNT = 3

# --------------------------------------------------------------------------- #
# JSON 键名（第 82 轮，用户口径："JSON 键名可以把 intent/emotion 等改为 A1/A2/A3 等
# —— 用抽象键名会更好自定义字段"）：
#   键名不再带任何语义，字段的显示名/含义可以随便改，模型也不会因为"键名像某个概念"而跑偏；
#   顺带省几个 token（intent→A1、reply_options→A5）。
#   ⚠ 改这里 = 改协议：掩码（core/fast_mask）、GBNF、解析、系统提示词都从这张表取，
#     四处的字面量必须**只**通过 json_key() 取，别再手写键名。
#   顺序就是模型必须输出的顺序：A1 意图 → A2 情绪 → A3 危险度(魅力) → A4 建议(评价)
#   → A5 三条选项 → A6 把握度。
# --------------------------------------------------------------------------- #
JSON_KEYS = {"intent": "A1", "emotion": "A2", "danger": "A3", "suggestion": "A4",
             "option": "A5", "confidence": "A6"}
JSON_KEY_BY_LEGACY = {"intent": "intent", "emotion": "emotion", "danger_level": "danger",
                      "suggestion": "suggestion", "reply_options": "option",
                      "confidence": "confidence"}


def json_key(slot: str) -> str:
    """槽位名（intent/emotion/danger/suggestion/option/confidence）→ 实际 JSON 键名。"""
    return JSON_KEYS.get(slot, slot)

# --------------------------------------------------------------------------- #
# 1.1 可自定义字段：每个输出字段都有"显示名 + 说明（prompt）"，用户在设置里可改。
#     说明留空 = 用这里的默认说明。JSON 键名、字段顺序、长度上限由 GBNF/程序固定，
#     用户改的只是"这一字段要生成什么内容、什么风格"。
# --------------------------------------------------------------------------- #
DEFAULT_FIELD_NAMES = {
    "intent": "意图",
    "emotion": "情绪",
    "danger": "危险度",
    "suggestion": "回复建议",
    "option": "回复选项",
}

DEFAULT_FIELD_PROMPTS = {
    "intent": (
        "用**口语**写他**真正想干什么**（**言外之意也要写进去**，不是复述这句说了啥、"
        "不是话题名、不是心情），**最多 14 字**，像跟朋友转述这一行本身"
        "（\"中午吃啥\"→\"问中午吃啥\"；\"行吧你忙\"→\"不乐意、等你拦他\"）。"
        "确实没言外之意就照实写，别硬编动机；写长了砍修饰语、别搬上文的话；"
        "**别用\"询问/表示/分享/进行/确认\"这类书面词**，别出现\"对方/用户\""
    ),
    "emotion": (
        "2-4 字的日常词（好奇、嫌烦、有点得意…），像随口说的，"
        "**别用\"中性/无明显情绪\"这种报告腔**"
    ),
    "danger": (
        "0-10 整数。**日常闲聊 0-2**（普通请求/提问/通知/吐槽一律 ≤2，别因为对方有情绪就加分）；"
        "只有对方**明确在指责你**（点着“你/您”）才可能 ≥5：平和指责 5-6，"
        "追责/翻旧账 7-8，攻击/最后通牒 9-10；"
        "**句子里没“你/您”最高给 4**。"
    ),
    "suggestion": (
        "给用户**一句能直接用的话或动作**（不复述、不解释），最多 30 字，像随口说的，"
        "**结合上文点明是哪件事**。"
        "**首选直说自己当下的感想/感受**（不用回应、不用抛梗），也可以随口附和半句、"
        "拐个小方向，但别写成段子，**别给最常规的那句接话**。"
        "不知道答案的事别编（反问或回头再查都行）；**别补上文没有的细节**（地点/时间/人名/数字）。"
        "**别用\"先…\"\"建议…\"\"可以…\"\"注意…\"开头**，别写\"情绪/意图/危险度\""
    ),
    "option": (
        "恰好三条能直接发的话，**像你自己打字**（口语短句，每条 ≤30 字、意思完整能单独发出去）；"
        "**三条立场必须拉开**：一条**认同**（顺着他的话往下说）、一条**不认同**（轻轻唱个反调）、"
        "一条**吐槽**（站边吐槽这件事/这个人，别当“无所谓”的和事佬）；"
        "别三条一个立场，也别写成互相吵架。"
        "措辞**直白简短**（确实、是这样、爽了、我不这么觉得这种），**三条说法要互不相同**，"
        "**最多一条**可发散一点（拐个小方向、吐槽一句），但别段子感/小剧场/堆比喻/客服腔。"
        "**要接得住上文**（还是同一件事，别另起话题；不知道的细节不要编）；"
        "对方问的事不知道答案时别替他编（直说不了解就行）。"
        "style 写「立场·说法」（立场只取 认同／反调／吐槽 + 2 字说法，≤6 字；正文别重复标签）"
    ),
}

# 固定前言：与 core/analyzer.build_messages 真正输出的格式一致。
# 实测坑：早前这段写的是"最后一行标了 `<<<`"，但 target-first 布局把目标放在**第一行**且
# 没有 `<<<` → 模型找不到"要做题的那条"，就去抓相邻消息的意思。A/B 实测：目标放最后跑偏 2/6，
# 放最前 0/6；提示词与格式对齐后再无矛盾指令。
SYSTEM_PROMPT_HEAD = (
    "你是QQ聊天分析助手。输入是一段对话记录：“我”=用户自己，“对方”=聊天对象。\n"
    "其中【需要分析的消息】**只有一行**，它就是要分析的对象；其他【紧邻的上文】【背景】"
    "都**不是分析对象**，只用来判断这一条在接续什么。\n"
    "第一条字段先看懂**字面**，再写出他**真正想干什么**（言外之意、没直说的目的）："
    "\"这么晚还没睡啊\"→其实想找你聊两句；\"行吧你忙\"→不乐意、等你挽留。"
    "确实没言外之意（问个事实、答个话）就照实写，别硬编动机；"
    "它不是这行的复述，也不是心情（心情由情绪字段写）。\n"
    "接续规则：他在接着上面同一件事说话时，把“在接续什么”揉进目的里——"
    "上文列 cos 道具、这条写“还差一个眼镜，头巾”→写 `想让对面补上眼镜头巾`，"
    "写 `提到眼镜和头巾`（复述）或 `在接清单`（说形式）都是错的。\n"
    "硬性要求：第一条字段只能依据【需要分析的消息】那一行（把别的行当成这行说的话＝分析错行）；"
    "写完贴回那一行自检“他想干什么”，对不上就重写。\n"
    "那一行只是“嗯/哈哈/收到/？”时，写它在回应什么。\n"
    "回复建议和回复选项可以不像回应（直白说感想、附和、拐个小方向都行），但**不能造事实**"
    "（没做过的事、不知道的信息不许写成真的）。\n"
    # 第 80 轮（Qwen3.5 实测：会把「字段名：说明」当成要输出的内容誊进 JSON 值里）：
    # 这一句**替换**掉原来那句"按下面的字段顺序输出 JSON" —— 位置就在字段列表开头，
    # 明确"下面是要求、不是内容"，让模型不再把说明当成值（比在末尾再补一句更省、更准）。
    "下面是每个字段的**要求**（是给你看的规则，**不是要你输出的文字**）："
    "按这个顺序输出 JSON，每个键的值只写**内容本身**，说明里的“字数上限”是硬要求："
)
SYSTEM_PROMPT_TAIL = (
    "A6：0-1 的小数，表示你对上面判断的把握。\n"
    "所有文字用纯文本：不要前缀、不要 Markdown、不要换行。只输出 JSON。"
)

# --------------------------------------------------------------------------- #
# 1.1b 自我分析（第 79 轮）：点**自己发的消息**时用另一套字段
#      用户口径："分析自己的消息的提示词危险度改为质量评分，回复建议改为对这条自己
#      这条消息的质量评价，回复选项改为发言选项"。
#      实现：字段名/说明各一套（DEFAULT_SELF_*）+ 另一段前言（SELF_*_HEAD/TAIL），
#      JSON 键名与顺序完全不变（GBNF/解析都不动），所以两条路可以共用同一套解析。
# --------------------------------------------------------------------------- #
DEFAULT_SELF_FIELD_NAMES = {
    # 第 79 轮口径（用户要求）：意图 / 情绪跟分析对方消息时保持**完全一致**，
    # 只有后面三个字段换成自我分析的语义。
    "intent": DEFAULT_FIELD_NAMES["intent"],
    "emotion": DEFAULT_FIELD_NAMES["emotion"],
    "danger": "魅力",            # 第 79 轮改名（用户口径）：质量评分 → 魅力
    "suggestion": "魅力评价",     # 原来是"消息点评"
    "option": "发言选项",
}

# 第 79 轮（用户口径）：这个字段评价的是**魅力** —— 我这条消息读起来迷不迷人
# （对方看了想不想接、想不想接着聊）。不是"质量"，也不是危险度。
# 为了有**区分度**，把魅力拆成四个可判断的维度，并给出四档锚点（模型先定档再给分）：
#   ① 情绪温度（有没有情绪、暖不暖）② 钩子（有没有让对方想接的点）
#   ③ 信息量（有没有具体内容）④ 说话的味道（有趣的表达 / 自嘲 / 画面感）
# 注意：**只讲 3-10**。实测提"0 分"或写"不要写 0"反而更容易让 4B 写 0
# （越禁止越想），所以低分段干脆不提；语法层也把下限钉在 3（见 build_quick_grammar）。
SELF_APPEAL_ANCHORS = (
    ("3-4", "干巴巴：光答复、单字答话、没内容也没情绪，对方很难接"),
    ("5", "普通：话说清了、能接住，但只是信息传递（基准档）"),
    ("6-7", "有点味道：多了一点细节 / 情绪 / 画面感，对方愿意往下聊"),
    ("8-10", "很迷人：有钩子（玩笑、悬念、期待感、反问），对方基本会接着聊"),
)
SELF_APPEAL_ANCHOR_TEXT = "；".join(f"{rng} {desc}" for rng, desc in SELF_APPEAL_ANCHORS)

DEFAULT_SELF_FIELD_PROMPTS = {
    # 意图 / 情绪直接用分析对方消息那套（含当前配置画像），这里引用同一份文本
    "intent": DEFAULT_FIELD_PROMPTS["intent"],
    "emotion": DEFAULT_FIELD_PROMPTS["emotion"],
    "danger": (
        "**3-10 的整数，越高越有魅力** —— 这是**我这条消息的魅力**"
        "（对方看了想不想接、想不想接着聊），不是危险度。\n"
        "打分方法：**先看这四个维度**（每个只算有没有，不必细算）："
        "① 情绪温度（有情绪 / 暖不暖）② 钩子（有没有让对方想接的点）"
        "③ 信息量（有没有具体内容：时间、地点、细节）④ 说话的味道"
        "（有趣的表达、自嘲、画面感）。\n"
        "**再对档给分**（不要一律给 5，该差就差、该好就好）：\n"
        f"　· {SELF_APPEAL_ANCHOR_TEXT}。\n"
        "两个例子：对方在说展会 → 我发「行」：只有答复、没内容没情绪 → **3-4**；"
        "我发「我订好票了，周六两点门口见，顺便把海报带给你」：有时间、有细节、"
        "还带个小惊喜 → **7-8**。\n"
        "只看**读起来迷不迷人**，**跟消息长短无关**（短句也能很迷人），"
        "也不看我心情好不好"
    ),
    "suggestion": (
        "给**我这条消息**一句**魅力评价**：先说**哪里迷人**（情绪/钩子/细节/味道里"
        "占哪一条），没有迷人点就直说**哪里显得干**，最多 30 字，像朋友点评，"
        "**先给结论**，而且**结论必须和魅力分同档**（3-4 说\"有点干\"、5 说\"普通/还行\"、"
        "6-7 说\"有点意思\"、8-10 才说\"挺迷人\"），别客套。"
        "**别用\"建议…\"\"可以…\"开头**，也别复述我这句话"
    ),
    "option": (
        "恰好三条**我接下来要发出去的话**（就是「下一轮我会打的字」）；"
        "每条 ≤30 字、口语、能直接发出去。\n"
        "**关键（方向最容易写反）**：你要**扮演「我」** —— 句中「我」＝我自己、「你」＝对方；"
        "三条都是我**接着自己这条继续往下说**的话（像我发完这句又补了一句），"
        "写成像**我自己打出去的字**：可以是我做的事/计划/感受，也可以是**我对这件事的直接评价**"
        "（**不必每条都出现「我」**）；**三条的开头与句式不要一样**（别三条都用「我…」开头）。\n"
        "　方向示例（内容随便换，方向照这个）：继续展开→「那就周六下午两点？我提前到门口等你」；"
        "吐槽→「这展排的队也太夸张了，我腿都站酸了」（吐槽**这件事/这个安排**）；"
        "终结→「行，那我先去忙，晚点再聊」。\n"
        "　· 不要把它换个说法重写一遍，也别用它开头那几个字开头；\n"
        "　· 有对方的新消息时，先接住对方那条；没有就接着我自己这条往下说"
        "（补细节、往下展开、追问对方、或把话收住）。\n"
        "三条的**立场固定**：① **继续展开当前话题**（顺着往细里说、补信息）；"
        "② **吐槽当前话题**（站边吐槽这件事，别当和事佬）；"
        "③ **终结当前话题**（把话收住、改天再聊、或找个由头结束）；"
        "别三条一个意思，也别写成对我的评价。"
        "**人称**（第 79 轮修）：这三条都是我**发给对方**的话 —— 收件人是对方，"
        "句子里用「你」称呼对方；**绝不要写成“别人对我说的话”**"
        "（读起来像“对方在叮嘱我/安慰我/问我”，方向就反了，必须重写）。"
        "立场标签仍写「立场·说法」（2 字说法，≤6 字；正文别重复标签）"
    ),
}

SELF_SYSTEM_PROMPT_HEAD = (
    "你是QQ聊天分析助手。输入是一段对话记录：“我”=用户自己（就是软件的主人），"
    "“对方”=聊天对象。\n"
    "其中【需要分析的消息】**只有一行**，而且它**是“我”自己发出去的**那一句；"
    "其他【紧邻的上文】【背景】都不是分析对象，只用来判断我这句话在接什么。\n"
    "这次的任务**不是**判断对方想干什么，而是**看我自己这句话接得怎么样**："
    "说清了没有、有没有接住上文、对面好不好接、会不会把话聊死。\n"
    "接续规则：我这句话接着上面同一件事说时，把“在接什么”写进第一个字段——"
    "上文在列 cos 道具、我发“还差一个眼镜，头巾”→写 `补上还差的眼镜头巾`，"
    "写 `在列清单`（说形式）或 `提到眼镜`（复述）都是错的。\n"
    "硬性要求：第一个字段只能依据【需要分析的消息】那一行（把别的行当成我说的"
    "＝分析错行）；写完贴回那一行自检“我这句话想干什么”，对不上就重写。\n"
    "第三个字段是**魅力**：越高越让人想接话，只看这条消息读起来有没有魅力。\n"
    "第四、五个字段是**魅力评价**和**我接下来可以怎么发言**，都要贴着我平时的说法。\n"
    # 同上（自我分析那套）：字段说明是要求、不是内容
    "下面是每个字段的**要求**（是给你看的规则，**不是要你输出的文字**）："
    "按这个顺序输出 JSON，每个键的值只写**内容本身**，说明里的“字数上限”是硬要求："
)
SELF_SYSTEM_PROMPT_TAIL = (
    "A6：0-1 的小数，表示你对上面判断的把握。\n"
    "所有文字用纯文本：不要前缀、不要 Markdown、不要换行。只输出 JSON。"
)

DEFAULT_SELF_STYLE_PROMPT = (
    "【这一轮的分析风格（自我分析）】说话直、不客套，像朋友看我聊天记录随口点评："
    "看点全放在**魅力**上（对方看了想不想接话）：有钩子就夸一句，"
    "干巴巴就直说干巴巴，别和稀泥；"
    "不写“建议你…”“可以尝试…”这种说教句；"
    "**发言选项**要像我平时会打的字（口语、短），不要客服腔、不要小作文。"
)


def field_prompt(cfg: Any, key: str, fields: Any = None, defaults: Any = None) -> str:
    """取某字段的说明：用户自定义优先，留空则用内置默认。

    fields/defaults 显式传入时为"另一套提示词"服务（第 79 轮：分析自己发的消息时
    用 self_fields + DEFAULT_SELF_FIELD_PROMPTS）。
    """
    spec = fields if fields is not None else getattr(cfg, "fields", None)
    custom = (getattr(spec, f"{key}_prompt", "") or "").strip()
    return custom or (defaults or DEFAULT_FIELD_PROMPTS)[key]


def field_name(cfg: Any, key: str, fields: Any = None, defaults: Any = None) -> str:
    """取某字段的显示名（用户可改，例如把“危险度”改成“兴趣水平”）。"""
    spec = fields if fields is not None else getattr(cfg, "fields", None)
    custom = (getattr(spec, f"{key}_name", "") or "").strip()
    return custom or (defaults or DEFAULT_FIELD_NAMES)[key]


def build_system_prompt(cfg: Any, compact: bool = False, self_mode: bool = False,
                        stage: str = "full") -> str:
    """按当前配置拼出完整系统提示词（固定格式 + 每字段可自定义的说明）。

    stage（第 81 轮末新增）——**分阶段只写本段字段**，别把整份字段表每段都发一遍：
      · "quick"  ：只列 intent / emotion / danger / suggestion（第一段要生成的就是这四项）
      · "replies"：只列 reply_options（第二段只生成三条回复选项）
      · "full"   ：五个都列（API 单次合并调用用）
    实测（当前配置 + 真实 tokenizer）：system prompt 1123 token → quick 928 / replies 701。

    角色包（第 81 轮末，用户口径："为每个配置搞个角色包文本，然后在字段中引用这个角色包"）：
      把配置里的整体风格（`analysis_style_prompt` / `self_analysis_style_prompt`，设置面板里
      就叫"角色包"）**完整放在提示词最前面**，所有字段都按它写；字段说明里可以写
      "按角色包"来引用。输出前还会再贴一句（`analyzer._style_headline`），保证 4B 跟得住。

    compact=True（API 省钱模式）：危险度那一长串示例压缩成一句判定流程 ——
    实测长度从约 1300 字降到约 700 字，省掉的是每次请求都要重发的 prompt token
    （对按 token 计费的 API 是纯省钱，本地模型用不到这个模式）。

    self_mode=True（第 79 轮）：分析**我自己发的那条**消息时换一套字段
    （危险度→质量评分、回复建议→消息点评、回复选项→发言选项），字段名/说明/前言
    都换，但 JSON 键名与顺序不变 —— 解析、GBNF、面板结构都不用动。
    """
    fields = getattr(cfg, "self_fields", None) if self_mode else None
    names_t = DEFAULT_SELF_FIELD_NAMES if self_mode else DEFAULT_FIELD_NAMES
    prompts_t = DEFAULT_SELF_FIELD_PROMPTS if self_mode else DEFAULT_FIELD_PROMPTS
    lines = [SELF_SYSTEM_PROMPT_HEAD if self_mode else SYSTEM_PROMPT_HEAD]
    # 角色包：完整放在最前面（所有字段都按它写）。空 = 用内置默认风格。
    # 角色包（第 82 轮，用户口径："角色包重点在于充分体现这个角色的人设风格与标志特色，
    # 让系统能够根据角色包很好地扮演这个角色"）——完整放在最前面，语义是"你要扮演的角色"。
    # 第 82 轮末（用户："角色包只要一份就够了，自我分析和对方分析共用一套角色包"）：
    # **两套体系永远共用同一份**（`analysis_style_prompt`）；只有在它为空时，才退回旧的
    # `self_analysis_style_prompt`（兼容老配置），再不行用内置默认。
    other_pack = (getattr(cfg, "analysis_style_prompt", "") or "").strip()
    self_pack = (getattr(cfg, "self_analysis_style_prompt", "") or "").strip()
    role_pack = other_pack or self_pack or DEFAULT_STYLE_PROMPT
    if not role_pack:
        role_pack = DEFAULT_STYLE_PROMPT
    lines.append(
        "【角色包】下面是你这次要扮演的角色（人设、说话方式、标志特色）。"
        "请完全以这个角色的身份、语气和习惯说话；所有字段都按它写，"
        "字段说明里写“按角色包”就是指这里：\n" + role_pack + "\n")
    # 用户画像（第 69 轮）：默认空 → 不进 prompt；填了就让模型模仿"我的说话习惯/口癖"。
    profile = (getattr(getattr(cfg, "fields", None), "user_profile", "") or "").strip()
    if profile:
        sug_name = field_name(cfg, "suggestion", fields, names_t)
        opt_name = field_name(cfg, "option", fields, names_t)
        lines.append(
            "【我（用户）的说话习惯 / 口癖】（这是**我自己**平时说话的样子；"
            f"写“{sug_name}”“{opt_name}”时要贴合这些习惯，别写成我平时根本不会说的话；"
            "但**不要**因此违背上面的字段规则与字数上限）：\n" + profile + "\n")
    # 第 80 轮（Qwen3.5 实测：会把「字段名：说明」当成要输出的内容誊进 JSON 值里）：
    # 字段列表改成"**JSON 键名 + 编号要求**"两段式 —— 先给一行键序（说明值该长什么样），
    # 再逐条写要求。**不再出现"字段名：说明"这种能被整行照抄的形状**，
    # 也不再把用户自定义的显示名（如"本小姐的判词"）放进提示词（面板照旧显示它们）。
    full_keys = (("intent", "intent"), ("emotion", "emotion"), ("danger", "danger_level"),
                 ("suggestion", "suggestion"), ("option", "reply_options"))
    if stage == "replies":
        key_order = tuple(x for x in full_keys if x[0] == "option")
        lines.append('JSON 键名固定：{"A5":[{"style":"…","text":"…"} ×3]}')
    elif stage == "quick":
        key_order = tuple(x for x in full_keys if x[0] != "option")
        lines.append('JSON 键名固定：{"A1":"…","A2":"…",'
                     '"A3":0,"A4":"…","A6":0.5}')
    else:
        key_order = full_keys
        lines.append(
            'JSON 键名固定：{"A1":"…","A2":"…","A3":0,'
            '"A4":"…","A6":0.5}（三条回复选项放在 A5 里）')
    lines.append("各键要写什么（下面是**要求**，不是要你输出的文字）：")
    for index, (key, _legacy_key) in enumerate(key_order, 1):
        if self_mode and key in ("intent", "emotion"):
            # 第 79 轮口径：意图/情绪与"分析对方消息"**完全一致** —— 直接沿用当前配置
            # （含用户改过的说明/画像），不另开一套。
            text = field_prompt(cfg, key)
        else:
            text = field_prompt(cfg, key, fields, prompts_t)
        if compact and key == "danger" and not self_mode:
            text = ("0-10 整数。绝大多数日常对话是 0-2，不要因为对方有情绪就加分；"
                    "只有当对方**明确在指责你**（句子里点着“你”追责任）才可能 ≥5"
                    "（平和指责 5-6、追责翻旧账 7-8、攻击或最后通牒 9-10）；"
                    "句子里没出现“你/您”时最高给 4")
        # 第 82 轮：这里印的是**抽象键名**（A1/A2/…），键名不再带语义
        lines.append(f"  {index}) {json_key(key)} —— {text}")
    tail = SELF_SYSTEM_PROMPT_TAIL if self_mode else SYSTEM_PROMPT_TAIL
    if stage == "replies":
        # 第二段不产出 confidence，别留着那句话（少 20 token 左右，也少一处干扰）
        tail = "\n".join(ln for ln in tail.split("\n")
                         if not ln.startswith(json_key("confidence")))
    lines.append(tail)
    if compact:
        # API 没有 GBNF 语法约束 → 必须在提示里**写死英文键名**，否则模型会拿显示名当键
        # （实测 DeepSeek 返回 {"意图":…,"回复选项":…} → 解析全空 → 面板显示"—"）。
        # 显示名（可被用户改名，如"兴趣水平"）只用于字段说明，JSON 键固定英文。
        if stage == "replies":
            schema = ('{"A5": [{"style": "<风格>", "text": "<回复1>"}, '
                      '{"style": "<风格>", "text": "<回复2>"}, '
                      '{"style": "<风格>", "text": "<回复3>"}]}')
        elif stage == "quick":
            schema = ('{"A1": "<意图>", "A2": "<情绪>", '
                      '"A3": <0-10 整数>, '
                      '"A4": "<回复建议>", "A6": <0-1 小数>}')
        else:
            schema = ('{"A1": "<意图>", "A2": "<情绪>", '
                      '"A3": <0-10 整数>, '
                      '"A4": "<回复建议>", '
                      '"A5": [{"style": "<风格>", "text": "<回复1>"}, '
                      '{"style": "<风格>", "text": "<回复2>"}, {"style": "<风格>", "text": "<回复3>"}], '
                      '"A6": <0-1 小数>}')
        lines.append(
            "JSON 的**键名必须严格用下面的英文，照抄，不要用中文、不要改名**：\n" + schema)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 1.2 预设配置（设置面板里的"配置列表"）：每套 = 一份字段名 + 字段说明。
#     默认两套：默认配置（即内置默认说明）与猫娘配置（用户要求，由本条实现提供）。
#     用户可以新建/删除自定义配置，但这两套 builtin 不允许删除。
# --------------------------------------------------------------------------- #
FIELD_KEYS = ("intent", "emotion", "danger", "suggestion", "option")
PROFILE_FIELDS = tuple(f"{key}_{suffix}" for key in FIELD_KEYS
                       for suffix in ("name", "prompt"))

# 第 79 轮：一套"配置"要装下**两套字段**，否则切配置时自我分析那套和全局风格会互相串：
#   · 5 个对方字段（PROFILE_FIELDS）
#   · 自我分析的三项（魅力 / 魅力评价 / 发言选项）+ 自我分析整体语气
#   · 两边的"全局分析风格"（对方那份 + 自我那份）
SELF_PROFILE_FIELDS = ("self_danger_name", "self_danger_prompt",
                       "self_suggestion_name", "self_suggestion_prompt",
                       "self_option_name", "self_option_prompt")
STYLE_PROFILE_FIELDS = ("style_prompt", "self_style_prompt")
ALL_PROFILE_FIELDS = PROFILE_FIELDS + SELF_PROFILE_FIELDS + STYLE_PROFILE_FIELDS


def profile_entry(name: str, builtin: bool = False, **values: str) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"name": name, "builtin": bool(builtin)}
    for key in ALL_PROFILE_FIELDS:
        entry[key] = str(values.get(key, "") or "")
    return entry


DEFAULT_PROFILE_NAME = "默认配置"
CATGIRL_PROFILE_NAME = "猫娘配置"


def default_profiles() -> List[Dict[str, Any]]:
    """内置两套配置：默认（留空=内置默认说明）与猫娘配置。"""
    catgirl = profile_entry(
        CATGIRL_PROFILE_NAME, builtin=True,
        intent_name="意图",
        intent_prompt=(
            "用猫娘口吻写他**真正想干什么**（**言外之意也要算**，不是复述、不是话题名、"
            "不是心情），**最多 14 字、句尾带“喵”**，像跟人转述那一行本身"
            "（\"中午吃啥\"→\"问吃啥喵\"；\"行吧你忙\"→\"不乐意了、等你哄喵\"）。"
            "确实没言外之意就照实写，别硬编喵；写长了砍修饰语、别搬上文的话；"
            "**别用\"询问/表示/分享/确认\"这类书面词**"
        ),
        emotion_name="情绪",
        emotion_prompt="2-4 个字的猫娘情绪词，**句尾带“喵”**（开心喵／好奇喵／有点小委屈喵）",
        danger_name="危险度",
        danger_prompt=(
            "0-10 整数（猫娘视角的“会不会炸毛”）。**日常闲聊 0-2**"
            "（普通请求/提问/通知/吐槽一律 ≤2，别因为对方有情绪就加分）；"
            "只有对方**明确在指责你**（点着“你/您”）才可能 ≥5：平和指责 5-6，"
            "追责/翻旧账 7-8，攻击/最后通牒 9-10；**没“你/您”最高给 4**。"
        ),
        suggestion_name="回复建议",
        suggestion_prompt=(
            "用猫娘口吻给**一句能直接用的动作**（不复述、不分析），不超过 25 个字、**句尾带“喵”**，"
            "结合上文点明是哪件事；**别用\"先…\"\"建议…\"开头**；"
            "**首选直说自己当下的感想/感受**（不用回应、不用抛梗），也可以随口附和半句；"
            "不知道答案的事别编（反问一句就行）；**别补上文没有的细节**"
        ),
        option_name="回复选项",
        option_prompt=(
            "三条可直接发的话，每条 ≤30 字、**句尾带“喵”**、口语软萌（不要书面语、"
            "每条意思完整能单独发出去）；"
            "**三条立场必须拉开**：一条认同他（顺着他的话往下说）、一条不认同（轻轻唱个反调）、"
            "一条吐槽（站边吐槽这件事/这个人，别当“无所谓”的和事佬）；"
            "别三条一个立场，也别写成互相吵架；"
            "措辞**直白简短**（确实喵、是这样喵、爽了喵这种，不用刻意抛梗找话题）、"
            "**三条说法互不相同**（不要固定“直说/打哈哈/认真”）；**最多一条**可发散一点，"
            "但别段子感/小剧场；**要接得住上文**（还是同一件事，别另起话题）；"
            "对方问的事不知道答案时别替他编（直说不了解就行）；**不知道的细节不要编**"
            "（别凭空补地点/时间/人名/数字）；"
            "style 写「立场·说法」（立场只取 认同／反调／吐槽 + 2 字说法，≤6 字；正文别重复标签）"
        ),
        # 第 79 轮（用户："猫娘配置的回复选项和回复建议正常，但发言建议和发言选项会丢掉喵"）：
        # 根因是猫娘配置只有"对方那套"，**自我分析那套是空的** → 落回内置默认（不带"喵"）。
        # 现在给它配一整套猫娘版自我分析字段。
        self_danger_name="魅力",
        self_danger_prompt=(
            "**3-10 的整数，越高越有魅力**（对方看了想不想接话）。**没毛病就填 5**"
            "（把话说清、对方能接就是 5）；多了一点细节/情绪/钩子、对方更想回填 6-7；"
            "有信息量或能勾住对方接着聊填 8-10；只有明显没内容、答非所问才填 3-4。"
            "只看读起来迷不迷人，**跟消息长短无关**，也不看我心情好不好"
        ),
        self_suggestion_name="魅力评价",
        self_suggestion_prompt=(
            "用猫娘口吻给**我这条消息**一句魅力评价（读起来想不想接、为什么），不超过 30 字、"
            "**句尾带“喵”**，**先给结论**（“挺迷人喵”／“有点干喵”），结论要和分数同档；"
            "**别用\"建议…\"\"可以…\"开头**，也别复述我这句话"
        ),
        self_option_name="发言选项",
        self_option_prompt=(
            "恰好三条**我接着自己这条继续往下说的话**（**扮演「我」、对对方说**，"
            "句中「我」＝我、「你」＝对方），每条 ≤30 字、口语、**句尾带“喵”**；"
            "写成像**我自己打出去的字**（可以是我做的事/计划/感受，也可以是**我对这件事的直接评价**，"
            "**不必每条都出现「我」**）；**三条的开头与句式不要一样**（别三条都用「我…」开头）；"
            "三条立场固定：① 继续展开（顺着自己的话往细里说）② 吐槽（吐槽这件事/这个安排，"
            "不是吐槽对方的话、更不是吐槽我自己那条）③ 终结话题（把话收住、改天再聊）"
        ),
        self_style_prompt=(
            "整体用猫娘自省口吻：嘴甜但实在，**关键结论句尾带“喵”**；"
            "重点看这句话有没有魅力点、钩子、分寸和真实感，不写说教句"
        ),
    )
    return [profile_entry(DEFAULT_PROFILE_NAME, builtin=True), catgirl]


def profile_values(entry: Dict[str, Any]) -> Dict[str, str]:
    return {key: str(entry.get(key, "") or "") for key in PROFILE_FIELDS}


def find_profile(cfg: Any, name: str) -> Optional[Dict[str, Any]]:
    for entry in getattr(cfg, "fields_profiles", []) or []:
        if str(entry.get("name") or "") == name:
            return entry
    return None


def apply_profile(cfg: Any, name: str) -> bool:
    """把某套配置套用到 cfg.fields（分析时真正读取的就是 cfg.fields）。"""
    entry = find_profile(cfg, name)
    if entry is None:
        return False
    for key in PROFILE_FIELDS:
        setattr(cfg.fields, key, str(entry.get(key, "") or ""))
    # 第 79 轮：自我分析那三项 + 两份"整体风格"也属于这套配置（不再全局共用）
    for key in SELF_PROFILE_FIELDS:
        if key in entry:
            setattr(cfg.self_fields, key.replace("self_", "", 1), str(entry.get(key) or ""))
    if "style_prompt" in entry:
        cfg.analysis_style_prompt = str(entry.get("style_prompt") or "") or DEFAULT_STYLE_PROMPT
    if "self_style_prompt" in entry:
        cfg.self_analysis_style_prompt = (str(entry.get("self_style_prompt") or "")
                                          or DEFAULT_SELF_STYLE_PROMPT)
    cfg.active_profile = name
    return True

# 可自定义部分（设置里编辑）：只描述"分析风格 / 语气 / 关注点"，不涉及字段与格式。
DEFAULT_STYLE_PROMPT = (
    "分析风格：像朋友在微信里随口提醒一句，不要写成报告。\n"
    "- 用口语短句；不要\"建议/应当/需要/请注意/进行/表示/针对\"这类书面词；\n"
    "- 不要第三人称分析口吻（不要出现\"对方/用户/他\"），直接说事；\n"
    "- 不要把分析术语写进建议里（不要出现\"情绪/意图/危险度\"这些词）；\n"
    "- 反例（不要这样写）：\"建议您先安抚对方情绪\"、\"表示对方可能在试探\"；\n"
    "  正例：\"他在等你回一句\"、\"可能是想让你出钱\"、\"先问清楚哪一步报错\"；\n"
    # 第 75 轮（用户口径："立场设定很重要，要保证有；把中立改成吐槽"）：
    # 立场规则写进**全局**分析风格里 —— 它对所有配置（默认/猫娘/用户自建）都生效，
    # 不会因为某套配置的字段说明被改掉而丢失（字段说明里那份保留，作为双保险）。
    "- **【硬要求】三条回复选项的立场必须拉开**：一条认同（顺着他的说法往下走）、"
    "一条反调（不认同/唱反调）、一条吐槽（站边吐槽这件事或这个人，别当“无所谓”的和事佬）；"
    "三条的说法（措辞、角度）也要互不相同，别两条一个味道；\n"
    "- 如果上下文不足以判断，confidence 给低一些。"
)
# 兼容旧字段（运行时不再使用；真正的提示词由 build_system_prompt(cfg) 按配置拼出）
DEFAULT_SYSTEM_PROMPT = ""


# --------------------------------------------------------------------------- #
# 2. 配置数据结构
# --------------------------------------------------------------------------- #
@dataclass
class PathsConfig:
    workspace: str = str(WORKSPACE)
    app_dir: str = str(APP_DIR)
    model: str = ""            # 空 = 自动解析（见 resolve_model_path）
    log_dir: str = str(LOG_DIR)


@dataclass
class UiaConfig:
    """UIA 读取后端参数（SPEC 4.2）。"""
    window_class: str = "Chrome_WidgetWin_1"
    exe_name: str = "qq.exe"
    message_list_title: str = "消息列表"
    activate_timeout_s: float = 25.0      # 不可用时必须在预算内判定（SPEC 4.2.3）
    activate_poll_s: float = 1.0
    recheck_interval_s: float = 30.0      # 已就绪后兜底重探
    min_tree_nodes: int = 50
    min_tree_named: int = 10
    poll_foreground_ms: int = 300          # B2 第 48 轮：调密一点，快速滚动时少漏上下文
    poll_background_ms: int = 2000
    poll_hover_ms: int = 120               # 鼠标在聊天区里时更快读取（滚动/快速翻页少漏消息）
    poll_click_mode_ms: int = 1200         # "点击触发"且**没在用时**的整树读取间隔（第 69 轮）
    #   为什么能这么慢：点击触发只需要"最近一次读取的消息矩形"做命中测试，
    #   不需要悬停即分析。实测把 120ms 放宽到 400ms 后，我们的 CPU 与 QQ 渲染进程的
    #   无障碍开销同步降到约 1/3（用户反馈"没在分析时风扇一直转"就是这个开销）。
    min_on_screen_ratio: float = 0.30      # 窗口被拖到屏幕边缘时也要能用（实测 0.47 也要接受）
    sender_name_max_chars: int = 10       # 短文本 + 同行有头像 = 发送者名
    filter_sender_names: bool = False     # 昵称行过滤：**默认关闭**
    #   实测：三条相邻的对方消息（[3,1272]/[3,1388]/[3,1504]）会被"昵称行"规则误删两条，
    #   导致悬停在相邻消息间完全无法切换。1v1 聊天本来就没有昵称行，只有群聊才有，
    #   且几何上很难与"短消息"区分，所以默认关；需要时可在设置里打开（规则已收紧）。
    min_message_chars: int = 1            # 1 字消息（"嗯""?"）也要能悬停命中；
    #   实测坑：=2 时这类消息被丢弃，鼠标停在它上面命中测试返回 None，
    #   面板会继续显示**上一条**的分析 → 用户会感觉"把相邻消息当成同一条"。
    left_x_fraction: float = 0.35         # SPEC 4.2 左侧判定
    left_width_fraction: float = 0.75
    avatar_min_px: int = 40
    avatar_max_px: int = 100
    image_min_px: int = 150               # 列表内 >=150x150 的 Image 视为图片消息

    # ---------------- 兜底策略参数（第 79 轮：配置化，QQ 改结构时改这里即可） ----------------
    #   背景：这次 QQ 9.9.36 把"消息列表"的控件类型从 Window 改成 Pane，我们靠兜底链当场吃住；
    #   但阈值/白名单当时写死在代码里 → 任何微调都要改代码+重打包。现在全部提到配置里：
    #   出现"读不到/读错"时，先按诊断包里的候选明细改这些值试，不用动代码。
    container_types: str = "Window,Pane,Group,Document,List,Custom,ListItem"
    #   几何兜底时允许当成"消息列表"的控件类型（英文类型名，逗号分隔）
    container_min_area_ratio: float = 0.05    # 容器最小面积占比（低于此多半是按钮/图标）
    container_max_area_ratio: float = 0.92    # 几何兜底时超过此比例算"整窗"，不算消息列表
    container_min_width_px: int = 200         # 几何兜底时容器最小宽度（像素）
    container_min_texts: int = 3              # 几何兜底时容器内至少要有几条可见文字
    container_geo_min_width_ratio: float = 0.55
    #   几何兜底时容器宽度至少要占窗口这么大 —— 这是排除**左侧会话列表**的主力
    #   （用户反馈："打开新会话时，有时会定位到左侧的会话列表而不是聊天界面"）：
    #   主窗口里会话列表只占宽约 25%，聊天区占 70% 以上；而**独立聊天窗口**没有左侧栏，
    #   它的消息列表也接近满宽，所以用"宽度占比"区分两种窗口都成立。
    container_geo_min_left_ratio: float = 0.0
    #   可选的辅助规则：容器左缘至少离窗口左边这么远。**默认 0 = 关闭**——
    #   实测独立聊天窗口的消息列表就贴着窗口左缘（列表 x=46、窗口 x=39），
    #   开了这条会把正常情况误杀（踩过）。只有在"主窗口 + 会话列表特别宽"的怪布局下再调大它。
    input_top_ratio: float = 0.55             # 输入框兜底：只看窗口下方这一部分
    input_min_width_ratio: float = 0.50       # 输入框兜底：宽度至少占窗口一半
    input_height_min_px: int = 60             # 输入框兜底：高度范围
    input_height_max_px: int = 320


@dataclass
class AnalyzerConfig:
    """LLM 分析参数（SPEC 5.4）。"""
    n_ctx: int = 3072                      # 本地上下文窗口（B2 第 47 轮：上下文变厚后 2048 不够用）
    max_tokens: int = 480
    #   第 75 轮：320 → 480。实测换 Qwen3.5-4B 后"话更多"（研究里也提到它输出 token 偏多），
    #   第二段三条选项偶尔只产出 1 条就被上限截断；本地只花时间不花钱，给足即可。
    temperature: float = 0.2
    # 采样参数（第 75 轮可配；此前只传 temperature，其余吃 llama.cpp 默认）：
    #   换 Qwen3.5 后"发挥不出来"的一个主因就是采样配方不符 ——
    #   Qwen3.5 非思考模式官方推荐 temp 0.7 / top_p 0.8 / top_k 20；
    #   而 Qwen3-4B-Instruct-2507 这套提示词是在 temp 0.2 下调好的（更稳、更短）。
    #   换模型时记得一起调这四个数（GBNF 仍然保证 JSON 合法，不会跑出格式）。
    top_k: int = 40
    top_p: float = 0.95
    min_p: float = 0.05
    presence_penalty: float = 0.0
    #   重复惩罚。第 80 轮（用户实测："发言选项老是变成同一套『周六下午两点别迟到』…"、
    #   "选项偶尔少一两条"）：模型偶尔把两条选项写成**同一句**，而重复项按设计要丢掉，
    #   面板于是只剩 1-2 条。给"复述/重复"加一点惩罚就压住了 ——
    #   实测第二段 30 次基线 2 次少条 → `1.1` 时 **0/70 次**，且正文风格/长度/立场没退化。
    #   （LFM2 官方推荐 1.05；想完全关掉就改回 1.0。）
    repeat_penalty: float = 1.1
    gen_stall_timeout_s: float = 20.0       # 生成看门狗：多久没有新 token 就视作卡死并中止
    gen_total_timeout_s: float = 150.0      # 单次生成总时长上限（正常一次 1.5-3s）
    n_batch: int = 1024                     # 一次喂给 GPU 的 token 批量（提示词处理速度）
    n_ubatch: int = 1024                    # 同上（微批量）
    #   第 77 轮实测（RTX 4060 Laptop，Qwen3-4B Q4_K_M，同提示词同长度）：解码
    #     默认(512/关 FA) 66-69 tok/s → 1024/开 FA 71-74 tok/s，约 +8%，
    #     端到端每次分析省 ~0.5s，且**不改动任何输出内容**（只是算得更快）。
    #   不支持 flash_attn 的构建/显卡会在加载失败后自动用保守参数重试，不会因此起不来。
    #   （8-bit KV cache 也试过：本机只比"1024+FA"再快 0-3%，且有额外失败面 → 不采用。）
    flash_attn: bool = True                 # FlashAttention（该后端支持时更快；不支持会自动退回）
    #   第 80 轮（实测数字见 core/fast_mask.py 顶部）：JSON 约束怎么给——
    #     "fast" = 自己算掩码（每步 0.22ms，解码 71.5 token/s 那种速度）；
    #     "gbnf" = llama.cpp 的 GBNF（每步要给 15 万词表算掩码，解码只剩 28.4 token/s）。
    #   两者约束的**语言完全一致**；"fast" 连续两次解析失败会自动退回 gbnf（只影响本次运行）。
    constrain: str = "fast"
    #   第 75 轮：试用 Qwen3.5（qwen35 混合 SSM 架构）时实测出现过**一个 token 都不吐**的
    #   真卡死（线程阻塞、CPU 2%、GPU 16%、日志无输出），面板就永远停在“分析中”。
    #   这两个超时 + 每 5s 一条进度日志，至少能让它“有结论、有日志”，不再无声卡住。
    n_gpu_layers: int = -1                 # 加载失败自动回退 0
    chat_format: str = "chatml"            # 覆盖模型的 chat 模板（"" = 用 GGUF 自带模板）
    #   第 75 轮（试用 Qwen3.5-4B 时新增）：Qwen3.5 默认"先思考再回答"，而我们只要
    #   GBNF 约束下的短 JSON。llama-cpp-python 0.3.22 **不支持** chat_template_kwargs，
    #   所以"关 thinking"用 `chat_format="chatml"` 实现 —— ChatML 正是 Qwen 的
    #   非思考格式（assistant 回合不带  thinking 块）。Qwen3-4B-Instruct-2507 本身也是
    #   ChatML，写死 "chatml" 对两者都成立；LFM2.5-2.6B 自带模板默认追加 `<think>`
    #   （会先思考再回答），也必须用 ChatML 覆盖才不 thinking。
    seed: int = 1234
    context_messages: int = 5              # 上下文条数上限（第 82 轮：用户口径"背景别超过 5 条，
    #                                       太多嚼不烂"；API 另有独立上限）
    history_cache_size: int = 16           # 池子里"当前窗口**之前**"最多留多少条（第 62 轮）
    #   用户口径："池子太大好像也没什么用，超过 16 条就自动清掉多余的"。
    #   依据：一次分析最多吃 context_messages(16) 条，而且**屏幕可见的优先** ——
    #   所以"当前窗口之前第 17 条"永远进不了 prompt，留着只会让池子无限长大。
    #   当前窗口本身永不裁（否则目标可能不在池子里，兜底会退化成"没有上下文"）。
    history_tail_keep: int = 4             # 池子里"当前窗口**之后**"最多留几条
    #   窗口之后的消息不进 prompt（第 82 轮起【后文】整段删掉），
    #   只在池子里留几条够下次滚动做"重叠判断"（重叠 ≥2 条就能安全合并）。
    context_history_max: int = 3           # "虚拟化历史"最多带几条（实测带太多会把模型带偏）
    context_target_first: bool = True      # 要分析的消息放最前（实测比"埋在最后"更不容易跑偏）
    context_token_budget: int = 800        # 上下文 token 上限（本地；用户定 800）
    summary_chars: int = 400               # 单条上下文最多保留多少字
    retry_on_invalid: int = 1              # 选项校验不通过时的重试次数
    two_stage: bool = True                 # 两段式：先出意图/情绪/危险度，再补三条回复
    prioritize_latest: bool = False        # 关：改回"鼠标指哪条分析哪条"（滚屏时屏幕最下面那条不是最新消息）
    analyze_latest_automatically: bool = False   # 不悬停时是否自动分析屏幕最下面那条对方消息
    preempt_enabled: bool = True           # 悬停切换目标时抢占取消正在生成的那条（切换更跟手）
    stream_partials: bool = True           # 流式提前上屏：情绪·意图一生成完（~0.5s）就先显示，
    vary_suggestion: bool = True           # 每条建议随机换一个"角度"，避免总用同一个开头词
    #   第 81 轮实验（tmp/probe_open4.py，用用户那套"大叔"配置）：同一条消息重算 3 次，
    #   第二段的**开头 3/3 完全相同**（"一起去看展吧…"、"怎么总是这样啊，人生啊…"）——
    #   因为第二段的 seed 写死（`cfg.seed + 1 + extra_seed`，而 extra_seed 只在点"刷新"时才变）。
    #   改成每次随机抖动后：开头前 3 字重复 8→5 次／27 条、整条逐字重复 1→0 条。
    #   关掉它就回到"同一条消息每次生成几乎逐字相同"的老行为。
    vary_options: bool = True              # 回复选项/发言选项每次换随机 seed（避免开头被锁死）
    #   不等整段 JSON（~2s）。实测体感快约 4 倍；只影响显示时机，不改结果内容。
    danger_cap_without_second_person: int = 4   # 句子里没有"你/您"时，danger 最高只给这个值
    cache_size: int = 50                   # msg_key 结果缓存（LRU）
    dedupe_seconds: int = 30               # 同一 msg_key 30s 内不重复分析（SPEC 4.4）
    gpu_fallback_to_cpu: bool = True
    backend: str = "local"                 # local | api（默认本地部署）


@dataclass
class ApiConfig:
    """OpenAI 兼容接口（用户显式开启才用；只发文本，不发截图/QQ号/昵称）。"""
    # 默认改为 DeepSeek（用户口径）；仍是 OpenAI 兼容协议，可随时改回别家
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    timeout_s: float = 15.0
    max_retries: int = 1
    # ---- token 消耗限制（B2 第 43 轮新增，用户要求"测试 API 前先做好限制"）----
    max_tokens_quick: int = 72         # 第一段（意图/情绪/危险度/建议）单次输出上限
    max_tokens_replies: int = 200      # 第二段（三条回复）单次输出上限（也用于"单次合并调用"）
    daily_token_budget: int = 100000   # 每日 token 上限（prompt+completion 合计；0=不限）
    #   第 80 轮（用户口径）：预设从 1 万提到 **10 万/天**（旧值太紧，正常用一会儿就触顶）
    on_budget_exceeded: str = "fallback_local"   # fallback_local | stop
    #   fallback_local：超出预算就不再调 API（本地模型兜底）；stop：直接不分析
    usage_file: str = ""               # 空 = logs/api_usage.json（按天累计，重启不丢）
    # ---- API 省钱策略（B2 第 44 轮：用户要求"单次消耗尽可能压缩"）----
    single_call: bool = True           # **一次请求出全部字段**（否则两段式 = 两次请求，prompt 翻倍）
    context_token_budget: int = 200    # API 上下文预算（本地是 600）→ 只带最近几句
    context_messages: int = 5          # API 上下文条数上限
    compact_prompt: bool = True        # API 用精简版系统提示（去掉一长串危险度示例）
    stream: bool = True                # 第 79 轮：API 也走流式上屏（等待感更低；关掉则退回一次性返回）


@dataclass
class UiConfig:
    """悬停面板与显示位置（SPEC 4.3 / 5.6）。"""
    panel_width: int = 320
    panel_max_width: int = 460             # 标题（情绪 · 意图）单行完整显示所需的最大宽度
    panel_height: int = 120
    panel_min_width: int = 260             # **逻辑像素**：空白带窄于此则退到窗口外侧
    allow_outside_window: bool = False      # 是否允许把浮窗放到 QQ 窗口外侧
    #   用户口径（第 22 轮）：**不要**放到窗口最边侧（容易找不到）→ 默认关；
    #   同排右侧空白不够时，宁可压住"自己发的"消息，也留在窗口内。
    #   实测：真实 QQ 窗口（1425×1528 物理 @150%）的"同排右侧空白"= 621~674 物理
    #   = 414~449 逻辑，足够放下 11pt 的 16 字标题（264px）；低于 260 逻辑就会
    #   逼得标题缩字号/折行，所以宁可换到窗口外侧也不塞进去。
    panel_gap_px: int = 12
    panel_opacity: float = 0.88
    theme: str = "galgame"                  # 浮窗配色主题：galgame(樱花，默认) / classic(经典深色)
    anim_enabled: bool = True               # 展开/收起动画（从消息一侧滑出 / 滑回）
    #   注：首版曾因 `_reveal_panel` 被批量替换成自我递归导致"点不出来"，已修复。
    anim_frames: int = 6                    # 动画帧数（30ms/帧 → 6 帧≈180ms）
    anim_ms_options: int = 200              # 选项条展开/收起时长（毫秒，时间基准）
    #   200ms：短到"选完立刻能粘贴"，又长到看得见收/展过程（粘贴会等它播完）。
    ctx_panel_top_offset_px: int = 50       # 上下文面板上边相对 QQ 窗口顶边的偏移（**逻辑**像素）
    #   第 68c 轮（用户口径："上下文面板上边与聊天面板白色区域上方齐平"）。
    #   实测：白色聊天区顶边 = QQ 窗口顶边 + 75 物理 = +50 逻辑（150% 缩放；
    #   消息列表顶边在它下面 96 物理，因为白区还含联系人头部/工具条）。
    #   UIA 树里没有"白色聊天区"这个容器（Document 直接就是整窗），所以用实测偏移 + 配置项，
    #   QQ 改版后改这个数字即可，不用动代码。
    trigger_mode: str = "click"             # 触发方式：click=鼠标移到消息上点一下才分析（默认，省性能）
    #   hover=旧的"移到就分析"。用户口径（第 23 轮）：悬停即分析开销偏高 → 改成点击触发。
    fade_ms: int = 120
    hover_padding_px: int = 8              # 命中测试容差
    click_padding_px: int = 24             # 点击命中容差（比悬停大得多：整行都能点中）
    wheel_step_px: int = 187               # 一格滚轮≈187px（实测 183/192）
    wheel_predict: bool = False            # 滚轮预测跟随（默认关）：Raw Input 事件已经能拿到，
    #   但"预测位移 + UIA 校准"的交接始终有视觉瑕疵（过冲/回拉/回弹，用户实测反馈），
    #   而 UIA 跟随现在每格都能更新 → 先以"纯 UIA 跟随"为准，需要再开这个开关。
    #   （只收事件副本、不在输入投递链路上，绝不会卡系统鼠标）。
    #   历史教训：早前用 WH_MOUSE_LL 全局钩子实现过，会导致整机鼠标失灵（2026-09-23 事故），
    #   该实现已在 core/worker.py 里标注"不要启用"。
    scroll_motion_predict: bool = False    # 实时滚动跟随（只读屏幕像素做互相关）。
    collapse_on_scroll: bool = True        # 检测到在聊天区滚动 → **直接收起浮窗**（用户口径）
    #   取代了"滚动时跟随"：滚动过程中面板不再挪动，直接收走，滚完需要看哪条再点一下。
    #   代码已实现（core/screenshot.py + ScrollMotionTracker），但位移估计器的单元自检还没过
    #   （平坦背景会给出假位移）→ 默认关闭，等自检全绿再开。
    #   实测坑：悬停用的容差被夹在 2-6px，点击时"必须点在气泡正中间"否则不响应 →
    #   点击触发模式单独放大到 24px（消息行间距 ~77 逻辑像素，不会误命中相邻消息）。
    dot_size_px: int = 26                  # 状态灯做大（用户要求，也作为设置入口）
    dot_size_min_px: int = 20              # 状态灯最小尺寸（设置里不能小于它，否则齿轮看不清）
    dot_margin_px: int = 8
    dot_position: str = "chat_top_right"   # 状态灯位置：chat_top_right=聊天区右上角空白；window_top=窗口标题栏（旧）
    dot_avoid_buttons_px: int = 150        # 兜底贴在标题栏内时，避开 QQ 的最小化/最大化/关闭按钮
    dot_hover_gear: bool = True            # 鼠标移上去变成齿轮（设置入口）
    # 第 79 轮：首次启动时在状态灯**下方**显示一行使用提示（点一下淡出关闭）。
    # 显示过一次就置 True → 后续启动不再出现。
    first_run_hint_shown: bool = False
    follow_poll_ms: int = 50               # 窗口几何轮询（配合 WinEvent 钩子做到丝滑跟随）
    use_move_hook: bool = True             # 用 WinEvent 钩子监听窗口移动（推荐）
    use_owner_window: bool = False         # owner 方案（反复改 owner 实测会让 QQ 窗口漂移）→ 关闭
    topmost_sync_ms: int = 150             # "只在 QQ 之上"：按前台窗口同步 topmost 的检查间隔
    hide_panel_when_qq_not_foreground: bool = True   # QQ 不在前台时收起面板（不挡别的应用）
    hide_dot_when_qq_not_foreground: bool = True     # QQ 不在前台时状态灯也收起（保持一致）
    only_other_party: bool = False         # 只分析对方(左)消息，不分析自己发的
    #   第 79 轮（用户要求："点击自己气泡时也进行分析，但用另一套提示词"）：
    #   默认改成 False = **双方都能点**；点自己发的消息时自动换一套字段
    #   （危险度→质量评分、回复建议→消息点评、回复选项→发言选项，见 self_fields）。
    #   设置面板里那个"只分析对方消息"开关仍然有效，勾上就退回旧行为。
    show_reply_style_prefix: bool = False  # 三条回复的 [风格] 前缀是否显示（用户口径：默认关闭）
    style_label_map: Dict[str, str] = field(default_factory=lambda: {
        "稳": "稳当", "稳妥": "稳当", "正式": "稳重",
        "简": "简单", "简短": "简单", "简洁": "简单",
        "轻松": "轻松", "幽默": "幽默", "好奇": "好奇",
    })
    danger_bands: List[Dict[str, Any]] = field(default_factory=lambda: [
        {"max": 3, "label": "低", "color": "#3FB950"},
        {"max": 6, "label": "中", "color": "#D29922"},
        {"max": 10, "label": "高", "color": "#F85149"},
    ])
    # 第 79 轮：分析自己发的消息时，第三个字段是"质量评分"（**越高越好**）→ 色带要反过来
    # （低分才是红）。标签与颜色都在设置/配置文件里可改。
    self_danger_bands: List[Dict[str, Any]] = field(default_factory=lambda: [
        # 第 79 轮（用户口径）：低魅力**灰**、中魅力**绿**、高魅力**粉**
        {"max": 4, "label": "干巴巴", "color": "#8B949E"},
        {"max": 7, "label": "有魅力", "color": "#3FB950"},
        {"max": 10, "label": "很迷人", "color": "#FF9ECB"},
    ])
    template: Dict[str, str] = field(default_factory=lambda: {
        "title": "{emotion} · {intent}",
        "badge": "危险度 {danger_level}/10",
        "body": "{suggestion}",
        "footer": "confidence {confidence}",
    })


@dataclass
class FillConfig:
    """一键填入（SPEC 2.2(d)：用户已确认默认开启）。"""
    enabled: bool = True                   # True=粘贴到输入框；False=只复制到剪贴板
    mode: str = "paste"                    # paste | copy
    allow_mouse_click: bool = True         # 允许"点一下输入框"把焦点交给 QQ（第 73 轮新增）
    #   关掉后：只发 Ctrl+V（不注入任何鼠标事件）。朋友机器上出现过"点选项只复制"和
    #   "鼠标偶尔彻底失灵"，排查时把这个关掉就能排除鼠标注入；QQ 以管理员身份运行时
    #   （UIPI 会拦住普通权限进程的输入注入）也必须关掉它、改为手动粘贴。
    show_first_use_notice: bool = True
    notice_shown: bool = False
    paste_timeout_ms: int = 1500
    hide_panel_after_ms: int = 150


@dataclass
class FieldsConfig:
    """输出字段的"显示名 + 自定义说明"（设置面板里可改）。

    用户口径（B2 第 20 轮）：把 danger_level 改成"兴趣水平"时，只要改这里的
    `danger_name`，再在 `danger_prompt` 里写"0-10，越高越有兴趣"之类的说明，
    提示词与面板徽标都会跟着变。说明留空 = 用内置默认说明。
    """
    intent_name: str = ""
    intent_prompt: str = ""
    emotion_name: str = ""
    emotion_prompt: str = ""
    danger_name: str = ""
    danger_prompt: str = ""
    danger_cap_rule: bool = False     # 硬封顶规则（默认关）：用户口径是"模型够聪明不需要"，
    #   已把这条规则写进 danger 字段的默认说明里（由模型自己遵守），设置面板里不再出现开关。
    suggestion_name: str = ""
    suggestion_prompt: str = ""
    user_profile: str = ""            # 用户画像（第 69 轮）：我的聊天习惯/口癖，默认空
    #   用户口径："可以在 prompt 自定义面板加个用户画像的接口，默认为空，
    #   用户可以往里填入自己的聊天习惯、口癖等让模型模仿"。
    #   非空时会作为【我（用户）的说话习惯】写进系统提示词，要求回复建议/回复选项去贴合。
    option_name: str = ""
    option_prompt: str = ""


@dataclass
class Config:
    debug: bool = False
    log_message_text: bool = False         # 日志里是否记消息原文（第 63 轮加固）
    #   关掉后 `请求分析 …：<消息>` 只记长度（脱敏），便于把日志发给别人排查。
    #   第 77 轮（发布前）：代码默认改成 **False**（发给别人的包里，日志不带聊天原文）；
    #   开发者本机在 config.json 里写 `"log_message_text": true` 即可恢复看原文。
    paths: PathsConfig = field(default_factory=PathsConfig)
    uia: UiaConfig = field(default_factory=UiaConfig)
    analyzer: AnalyzerConfig = field(default_factory=AnalyzerConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    fill: FillConfig = field(default_factory=FillConfig)
    fields: FieldsConfig = field(default_factory=FieldsConfig)
    # 第 79 轮：**自我分析**用的另一套字段（点自己发的消息时生效）。默认留空 = 用
    # DEFAULT_SELF_* 那套内置说明（质量评分 / 消息点评 / 发言选项）。
    self_fields: FieldsConfig = field(default_factory=FieldsConfig)
    self_analysis_style_prompt: str = ""    # 留空 = DEFAULT_SELF_STYLE_PROMPT
    # 设置面板"配置列表"里的多套字段自定义（默认 + 猫娘 + 用户自建）
    fields_profiles: List[Dict[str, Any]] = field(default_factory=default_profiles)
    active_profile: str = DEFAULT_PROFILE_NAME
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # "复制模板"（给网页 AI 的那段说明）的可编辑副本：**留空 = 用代码里的内置模板**
    # （`core/prompt_import.build_template()`）。设置面板的「设置模板」按钮把它写在这里，
    # 保存后「复制模板」按钮复制的就是这一份（第 80 轮末新增：用户要自己微调模板）。
    copy_template: str = ""
    field_limits: Dict[str, int] = field(default_factory=lambda: dict(FIELD_LIMITS))
    danger_anchors: List[List[str]] = field(
        default_factory=lambda: [[rng, desc] for rng, desc in DANGER_ANCHORS])
    # 自定义部分只放"分析风格/样式"；字段与输出格式由固定模板保证（见 DEFAULT_STYLE_PROMPT）
    analysis_style_prompt: str = ""        # 空 = 用 DEFAULT_STYLE_PROMPT


# --------------------------------------------------------------------------- #
# 3. 读写
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT_TEMPLATE = build_system_prompt(Config())   # 默认提示词（兼容旧引用）


def _merge(dc: Any, data: Dict[str, Any]) -> None:
    """把 JSON 里的字段合并进 dataclass（未知键忽略，嵌套 dataclass 递归）。"""
    for key, value in (data or {}).items():
        if not hasattr(dc, key):
            continue
        current = getattr(dc, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(dc, key, value)


# 启动自检要**跳过**的键：这些本来就是"用户数据 / 机器相关"，与代码默认值不同是正常的。
# 留着不看的是"行为参数"（轮询、上下文、界面开关、API 预算…）——那才是会咬人的东西。
_CONFIG_SELFCHECK_SKIP = (
    "paths",                       # 绝对路径随机器变
    "debug",
    "log_message_text",            # 发布默认关、开发者本机开（用户偏好，非行为参数）
    "api.api_key",
    "fields", "fields_profiles", "active_profile",   # 提示词按当前配置回填
    "system_prompt", "analysis_style_prompt",
    "copy_template",               # "复制模板"的可编辑副本（用户数据，留空=内置默认）
    "self_fields", "self_analysis_style_prompt",     # 第 79 轮：自我分析那套提示词
    "fill.notice_shown",
    "ui.first_run_hint_shown",     # 第 79 轮：首次启动提示"已看过"的标记（用户数据）
)


def config_diff_vs_defaults(path: Optional[Path] = None) -> List[str]:
    """列出 config.json 里与**代码默认值**不一致的键（启动自检用）。

    为什么需要它：`config.json` 会覆盖代码默认值，"改代码默认值却忘了同步 JSON"的坑
    已经踩过 5 次（SPEC 6.1）。启动时打一行日志，改错/遗留的键立刻可见。
    """
    path = path or CONFIG_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                      # 文件不存在/损坏：交给 load_config 处理
        return []
    if not isinstance(data, dict):
        return []
    defaults = asdict(Config())
    diffs: List[str] = []

    def walk(prefix: str, default: Any, raw: Any) -> None:
        """prefix 以 '.' 结尾表示"当前层级"。"""
        if prefix[:-1] in _CONFIG_SELFCHECK_SKIP:
            return
        if isinstance(default, dict):
            if not isinstance(raw, dict):
                return
            for key, value in default.items():
                if key in raw:
                    walk(f"{prefix}{key}.", value, raw[key])
            return
        if raw != default:
            diffs.append(f"{prefix[:-1]}={raw!r}（默认 {default!r}）")

    for key, value in defaults.items():
        if key in data:
            walk(f"{key}.", value, data[key])
    return sorted(diffs)


def load_config(path: Optional[Path] = None) -> Config:
    cfg = Config()
    path = path or CONFIG_JSON
    if path.exists():
        try:
            _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # 配置损坏不应导致程序起不来
            # 第 63 轮：主文件坏了先试上一版备份（save_config 每次都会留 .bak），
            # 再不行才回退默认值 —— 否则一次写坏就等于把所有设置和 API Key 静默清空。
            backup = path.with_name(path.name + ".bak")
            recovered = False
            if backup.exists():
                try:
                    _merge(cfg, json.loads(backup.read_text(encoding="utf-8")))
                    recovered = True
                    print(f"[config] 读取 {path} 失败，已从 {backup.name} 恢复：{exc}",
                          file=sys.stderr)
                except Exception:
                    recovered = False
            if not recovered:
                print(f"[config] 读取 {path} 失败，使用默认配置：{exc}", file=sys.stderr)
    # 状态灯尺寸下限（设置里填了更小的值也要夹住）
    try:
        cfg.ui.dot_size_px = max(int(cfg.ui.dot_size_min_px), int(cfg.ui.dot_size_px))
    except Exception:
        cfg.ui.dot_size_px = 26
    # 字段长度上限是**程序固定**的（设置面板不暴露；GBNF、自算掩码、显示截断都按它走）。
    # 第 80 轮踩到的坑：老 config.json 里存着 `field_limits.intent=15`，**会把代码里的新上限顶掉** ——
    # 代码改成 13 之后运行时仍是 15，而自算掩码是按代码里的 13 建的，两边打架。
    # 所以每次加载都强制用代码常量（同 builtin 配置"以代码为准"的口径）。
    cfg.field_limits = dict(FIELD_LIMITS)
    if not (cfg.analysis_style_prompt or "").strip():
        cfg.analysis_style_prompt = DEFAULT_STYLE_PROMPT
    if not (cfg.self_analysis_style_prompt or "").strip():
        cfg.self_analysis_style_prompt = DEFAULT_SELF_STYLE_PROMPT
    # 配置列表：内置两套（默认配置/猫娘配置）**以代码为准**，每次加载都刷新，
    # 这样我改了猫娘提示词，用户不用手动重置就能拿到新版本；自建配置原样保留。
    stored = [entry for entry in (cfg.fields_profiles or [])
              if isinstance(entry, dict) and (entry.get("name") or "").strip()]
    builtin = {entry["name"]: entry for entry in default_profiles()}
    merged: List[Dict[str, Any]] = []
    for entry in stored:
        name = str(entry["name"])
        if entry.get("builtin") and entry.get("edited"):
            # 第 75 轮：内置配置若被用户改过（settings_dialog 会打 `edited=True`），
            # **保留用户的版本** —— 否则"改完下次启动又变回去"（用户反馈"保存逻辑不舒服"）。
            # 第 79 轮（用户："猫娘默认配置自我分析部分还是不带喵"）：
            # 用户改过的是"当时存在的那些字段"；后来代码给内置配置**补了新字段**
            # （例如猫娘配置的自我分析那套）时，老存档里这些键要么不存在、要么是空串 ——
            # 直接整份保留会让新字段永远生效不了。所以：**空的键从代码版本补齐**，
            # 非空的（用户真正填过的）原样保留。
            code_version = dict(builtin.get(name) or {})
            filled = dict(entry)
            # 有些"看起来像是用户填的"值其实是**全局默认文本**（设置面板保存时会把
            # `DEFAULT_STYLE_PROMPT` / `DEFAULT_SELF_STYLE_PROMPT` 当内容存进配置里）——
            # 它们同样代表"没自定义"，要跟着内置配置的新版本走。
            default_texts = {
                "style_prompt": DEFAULT_STYLE_PROMPT,
                "self_style_prompt": DEFAULT_SELF_STYLE_PROMPT,
            }
            for key, value in code_version.items():
                stored_value = str(filled.get(key) or "").strip()
                if not stored_value or stored_value == str(default_texts.get(key) or "\0").strip():
                    filled[key] = value
            merged.append(filled)
        elif entry.get("builtin") or name in builtin:
            merged.append(dict(builtin.get(name) or entry))
        else:
            merged.append(entry)
    for name, entry in builtin.items():           # 缺哪套内置就补哪套
        if name not in {item["name"] for item in merged}:
            merged.append(entry)
    cfg.fields_profiles = merged
    # 配置列表是"唯一事实来源"：按当前选中的那套回填 cfg.fields（分析时真正读的就是它）
    if cfg.active_profile and find_profile(cfg, cfg.active_profile) is not None:
        apply_profile(cfg, cfg.active_profile)
    return cfg


def save_config(cfg: Config, path: Optional[Path] = None) -> Path:
    """写配置：**原子替换** + 留一份 .bak（第 63 轮加固）。

    原来是直接 `path.write_text(...)`：崩溃/断电/被强杀正好停在写一半时，
    config.json 会变成半个 JSON —— `load_config` 解析失败就静默回退成默认值，
    用户的全部设置（含 API Key）一起丢。现在改成：
      写 config.json.tmp → fsync 落盘 → 把上一版留成 config.json.bak → os.replace 原子替换。
    （同目录 os.replace 在 Windows 上是原子的，读者永远看到完整的旧文件或新文件。）
    """
    path = path or CONFIG_JSON
    data = json.dumps(asdict(cfg), ensure_ascii=False, indent=2)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(data, encoding="utf-8")
    try:                                  # 尽量把数据真正刷到磁盘，断电也不容易留空文件
        with open(tmp_path, "rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass
    if path.exists():
        try:
            path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
        except Exception:
            pass
    os.replace(tmp_path, path)
    return path


# --------------------------------------------------------------------------- #
# 4. 路径解析（SPEC 5.1：模型与 exe 同级；开发期回退工作区）
# --------------------------------------------------------------------------- #
MODEL_CANDIDATES = (
    # 第 63 轮：把实际在用的模型放最前（旧清单里是 qwen2.5-3B/1.5B，早就退役了，
    # 新机器上若 paths.model 为空会直接找不到模型）。顺序 = 优先用哪个。
    # 第 80 轮末（用户实测结论）：同一套提示词/采样/掩码下 **Qwen3-4B 明显更对路** ——
    # 3.5-4B 在这条"短输出 + 严格 JSON + 关思考 + 0.2 低温"的管线上反而更差（见 SPEC 第 347 条）
    # → 发布/新装机默认回到 Qwen3-4B，3.5 退成备选（模型文件仍保留，想试把 paths.model 指过去）。
    "qwen3-4b-instruct-2507-q4_k_m.gguf",
    "qwen3.5-4b-q4_k_m.gguf",
    "qwen3-1.7b-instruct-q4_k_m.gguf",
    # 兼容旧包/旧工作区（有就顺带命中，没有就跳过）
    "qwen2.5-3b-instruct-q4_k_m.gguf",
    "qwen2.5-1.5b-instruct-q4_k_m.gguf",
)


def resolve_model_path(cfg: Config) -> Optional[Path]:
    if cfg.paths.model:
        candidate = Path(cfg.paths.model)
        if candidate.exists():
            return candidate
        # 第 80 轮（出包踩到的坑）：配的模型被删了 / 换了机器时**别直接失败**，
        # 继续往下按候选清单找 —— 否则装了个只有 3.5 的包、而某处残留的配置写着 4b，
        # 程序会一直报"模型文件不存在"，用户没有任何提示可循。
        print(f"[config] 配置里的模型不存在（{cfg.paths.model}）→ 改为按候选清单查找",
              file=sys.stderr)
    roots = (Path(cfg.paths.app_dir) / "models", Path(cfg.paths.workspace) / "models")
    for root in roots:
        for name in MODEL_CANDIDATES:
            candidate = root / name
            if candidate.exists():
                return candidate
    # 最后兜底：models/ 里任意一个 gguf（用户自己放进去、名字不在清单里也能直接用）
    for root in roots:
        if root.is_dir():
            for candidate in sorted(root.glob("*.gguf")):
                return candidate
    return None


def workspace_paths() -> Dict[str, str]:
    return {"workspace": str(WORKSPACE), "app_dir": str(APP_DIR), "tmp": str(TMP_DIR),
            "cache": str(CACHE_DIR), "logs": str(LOG_DIR), "results": str(RESULTS_DIR),
            "config": str(CONFIG_JSON)}


# --------------------------------------------------------------------------- #
# 5. 一键还原默认设置（设置界面用）
# --------------------------------------------------------------------------- #
def default_config_dict() -> Dict[str, Any]:
    cfg = Config()
    cfg.paths.workspace = str(WORKSPACE)
    cfg.paths.app_dir = str(APP_DIR)
    cfg.paths.log_dir = str(LOG_DIR)
    cfg.paths.model = ""
    return asdict(cfg)


def restore_defaults(path: Optional[Path] = None) -> Config:
    """把 config.json 恢复成默认值（保留 model 路径与已读提示标记由调用方决定）。"""
    cfg = Config()
    path = path or CONFIG_JSON
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    return cfg


if __name__ == "__main__":  # 快速自检
    c = load_config()
    print(json.dumps({"workspace_paths": workspace_paths(),
                      "model": str(resolve_model_path(c)),
                      "field_limits": c.field_limits,
                      "fill": asdict(c.fill)}, ensure_ascii=False, indent=2))
