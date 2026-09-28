#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`core/fast_mask.py` 的**离线**回归（不加载模型、不生成）：状态机 / 掩码 / 处理器。

为什么必须先有这套：这个模块是"自己算 JSON 约束掩码"，出错的后果是"模型写不出合法
JSON"。所以这里用一份**假词表**把每条规则都钉住：
  A. 程序自检：四套程序的参考 JSON 能逐字符走完；错字段名 / 危险度 99 / 超字数 → 走不动；
  B. 掩码本身：字面量状态只放行"剩余字面量的前缀"；文本槽放行干净文本、挡住引号/换行、
     到上限后只能收尾；枚举（危险度）按前缀推进，`1` 之后还能写 `0` 凑成 `10`；
  C. 跨槽 token：形如 `好","` 的一个 token 要被正确切分（前面给文本槽、后面给字面量）；
  D. 处理器：喂一串 token 后掩码跟着状态走，走完程序后只放行 EOS（生成正常结束）；
  E. 缓存键：换模型/换词表大小不会串缓存。

用法：python tools/test_fast_mask.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import fast_mask as fm  # noqa: E402


class FakeLogitsProcessor:
    """离线测试用：只提供一个 `__call__` 签名，不 import llama_cpp（那会加载 DLL）。"""

    def __call__(self, input_ids, scores):       # noqa: D102
        raise NotImplementedError


import string  # noqa: E402

# 假词表：结构字符 + 小写字母（字段名用）+ 参考 JSON 里用到的汉字 + 几个多字符合并
CHARS = '{}":,[]0123456789. \t\n' + string.ascii_lowercase + string.ascii_uppercase + "_" \
    + "问在不在好奇挺迷人他等你回一句确认时间期待好呀算了离谱认同反调吐槽"
FAKE_PIECES = list(CHARS) + [
    '","', '","A2":"', '{"A1":"', '好","', '10', '0.8', '","text":"', '"}]',
]
# 保证 id 稳定：去重但保留顺序
_seen, PIECES = set(), []
for piece in FAKE_PIECES:
    if piece not in _seen:
        _seen.add(piece)
        PIECES.append(piece)
TABLE = fm.TokenTable(PIECES)
EOS_ID = len(PIECES)          # 假 EOS（真实实现里是 llm.token_eos()）
TABLE_WITH_EOS = fm.TokenTable(PIECES + ["<eos>"])

# 第 82 轮：JSON 键名改成抽象名（config.JSON_KEYS = A1/A2/…），假词表也要跟着换
GOOD = {
    fm.PROGRAM_QUICK: '{"A1":"问在不在","A2":"好奇","A3":1,'
                      '"A4":"他在等你回一句","A6":0.8}',
    fm.PROGRAM_QUICK_SELF: '{"A1":"确认时间","A2":"期待","A3":6,'
                           '"A4":"挺迷人","A6":0.6}',
    fm.PROGRAM_REPLY: '{"A5":[{"style":"认同","text":"好呀"},'
                      '{"style":"反调","text":"算了"},{"style":"吐槽","text":"离谱"}]}',
    fm.PROGRAM_FULL: '{"A1":"问在不在","A2":"好奇","A3":1,'
                     '"A4":"他在等你回一句","A5":['
                     '{"style":"认同","text":"好呀"},{"style":"反调","text":"算了"},'
                     '{"style":"吐槽","text":"离谱"}],"A6":0.8}',
}


def _ids(*strings: str) -> list:
    out = []
    for s in strings:
        out.extend(TABLE._by_piece.get(s, []))       # noqa: SLF001
    return out


def _mask_chars(mask) -> set:
    return {TABLE.pieces[int(i)] for i in np.nonzero(mask)[0] if len(TABLE.pieces[int(i)]) == 1}


def case_program_walk() -> bool:
    """A：四套程序的参考 JSON 走通；改坏的走不通。"""
    c = fm.FastConstrainer(TABLE, EOS_ID)
    ok_self = c.self_check()
    bad_cases = [
        (fm.PROGRAM_QUICK, GOOD[fm.PROGRAM_QUICK].replace('"A2"', '"emo"')),
        (fm.PROGRAM_FULL, GOOD[fm.PROGRAM_FULL].replace('"A3":1', '"A3":99')),
        (fm.PROGRAM_REPLY, GOOD[fm.PROGRAM_REPLY].replace('"text"', '"txt"')),
        # 第 80 轮：这里也**不能写死 15 字** —— 上限一旦调整（intent 14→20），
        # "15 字"就变成合法，用例会假红（同一次事故的两个地方）。改成按程序槽上限 +1。
        (fm.PROGRAM_QUICK, GOOD[fm.PROGRAM_QUICK].replace(
            '"问在不在"',
            '"' + "问" * (next(step.limit for step in fm.build_program(fm.PROGRAM_QUICK)
                               if step.kind == "text") + 1) + '"')),
        (fm.PROGRAM_QUICK, GOOD[fm.PROGRAM_QUICK][:-1]),          # 少最后的 }
        (fm.PROGRAM_QUICK, GOOD[fm.PROGRAM_QUICK].replace(
            '"他在等你回一句"', '"' + "问" * 41 + '"')),           # 超 40 字上限
    ]
    bad_ok = []
    for name, text in bad_cases:
        walked, _why = c.walk(name, text)
        bad_ok.append(not walked)
    ok = ok_self[0] and all(bad_ok)
    print(f"A 程序自检 + 非法 JSON 被挡：{'对' if ok else '错'}"
          f"（自检={ok_self[0]} {ok_self[1]}｜非法用例被挡住={bad_ok}）")
    return ok


def case_masks() -> bool:
    """B：掩码本身（字面量 / 文本槽上限 / 引号 / 换行 / 枚举前缀）。"""
    c = fm.FastConstrainer(TABLE, EOS_ID)
    quick = c.programs[fm.PROGRAM_QUICK]
    # 起点：只允许字面量 `{"A1":"` 的前缀（键名见 config.JSON_KEYS）
    start = quick.mask(0, 0, 0)
    start_ok = ('{' in _mask_chars(start)) and ('}' not in _mask_chars(start))
    # 字面量写完 → 文本槽
    state = (0, 0, 0)
    for ch in '{"A1":"':
        state = quick.advance(*state, ch)
    text_mask = quick.mask(*state)
    text_chars = _mask_chars(text_mask)
    text_ok = ('问' in text_chars) and ('"' not in text_chars) and ('\n' not in text_chars)
    # 写满 14 字 → 自己那部分空掉，只允许收尾（下一个字面量以 " 开头）
    full = (state[0], state[1], 14)
    full_mask = quick.mask(*full)
    own_ok = not (TABLE.clean & (TABLE.nchars <= 0)).any()
    close_ok = '"' in _mask_chars(full_mask)
    # 枚举：起点允许 0-9；写 `1` 之后还能写 `0`（凑 10），并且允许收尾（`,`）
    danger_index = next(i for i, s in enumerate(quick.steps) if s.kind == "alts")
    danger_start = _mask_chars(quick.mask(danger_index, 0, 0))
    node1 = quick.advance(danger_index, 0, 0, "1")
    danger_after1 = _mask_chars(quick.mask(*node1))
    danger_ok = ("0" in danger_start and "9" in danger_start
                 and "0" in danger_after1 and '"' not in danger_start)
    ok = start_ok and text_ok and own_ok and close_ok and danger_ok
    print(f"B 掩码规则：{'对' if ok else '错'}"
          f"（起点={start_ok} 文本槽={text_ok} 到上限只能收尾={close_ok} 枚举前缀={danger_ok}）")
    return ok


def case_cross_slot_token() -> bool:
    """C：一个 token 跨槽（`好","`）要被切开：前面给文本槽、后面给字面量。"""
    c = fm.FastConstrainer(TABLE, EOS_ID)
    quick = c.programs[fm.PROGRAM_QUICK]
    state = (0, 0, 0)
    for ch in '{"A1":"':
        state = quick.advance(*state, ch)
    after = quick.advance(*state, '好","')          # 文本 1 字 + 字面量 '","' 走完
    ok = after is not None and after[0] > state[0]
    print(f"C 跨槽 token `好\",\"` 被切开：{'对' if ok else '错'}（{state} → {after}）")
    return ok


def case_processor() -> bool:
    """D：LogitsProcessor —— 掩码随状态走，走完程序只放行 EOS。"""
    c = fm.FastConstrainer(TABLE_WITH_EOS, EOS_ID)
    program = c.programs[fm.PROGRAM_REPLY]
    cls = fm.make_processor_factory(base=FakeLogitsProcessor)
    proc = cls(program, EOS_ID, TABLE_WITH_EOS.n_vocab)
    scores = np.zeros(TABLE_WITH_EOS.n_vocab, dtype=np.float32)
    # 第一次调用：base_len 记下来，不做前进
    proc(np.asarray([1, 2, 3]), scores)
    first_ok = proc.base_len == 3 and proc.state == (0, 0, 0)
    # 逐字符喂（用单字符 token 的 id）
    ids = [1, 2, 3]
    text = GOOD[fm.PROGRAM_REPLY]
    for ch in text:
        tid = TABLE_WITH_EOS._by_piece[ch][0]        # noqa: SLF001
        ids.append(tid)
        scores[:] = 0.0
        proc(np.asarray(ids), scores)
    finished_ok = bool(proc.done) and proc.state[0] >= len(program.steps)
    eos_only = np.isfinite(scores).sum() == 1 and np.isfinite(scores[EOS_ID])
    ok = first_ok and finished_ok and eos_only and proc.anomalies == 0
    print(f"D 处理器状态推进与收尾：{'对' if ok else '错'}"
          f"（base={first_ok} 走完={finished_ok} 只剩 EOS={eos_only} 异常={proc.anomalies}）")
    return ok


def case_cache_key() -> bool:
    """E：缓存键含模型名与词表大小（换模型不串）。"""
    a = fm.default_cache_key(r"D:\x\qwen3-4b-instruct-2507-q4_k_m.gguf", 151936)
    b = fm.default_cache_key(r"D:\x\qwen3-4b-instruct-2507-q4_k_m.gguf", 151936)
    c2 = fm.default_cache_key(r"D:\x\qwen3.5-4b-q4_k_m.gguf", 151936)
    ok = (a == b) and (a != c2) and ("151936" in a)
    print(f"E 缓存键稳定且区分模型：{'对' if ok else '错'}（{a}）")
    return ok


def case_caps_in_sync() -> bool:
    """G：**自算掩码的槽上限必须与 config.FIELD_LIMITS 一致**，而且调了上限自检也要过。

    第 80 轮事故：`FIELD_LIMITS["intent"]` 从 14 提到 20，而 `self_check()` 里写死用
    15 字当"超限样例" → 15 字变合法 → 自检判失败 → **静默退回 GBNF**（解码掉到 1/3，
    用户实测"慢了几倍"）。这个用例把"上限同步"钉死，避免同一类事故再发生。
    """
    import config as app_config
    intent_limit = next(step.limit for step in fm.build_program(fm.PROGRAM_QUICK)
                        if step.kind == "text")
    ok_sync = intent_limit == int(app_config.FIELD_LIMITS["intent"])
    # 第 80 轮续（打包前自查发现的坑）：**运行时配置里的上限也必须等于代码常量** ——
    # 老 config.json 存着 `field_limits.intent=15` 会把代码里的 13 顶掉（掩码 13 / GBNF 15 打架）。
    # load_config() 现在每次都以代码常量为准，这里拿一份"带旧值"的配置验一下。
    import json
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(dir=str(app_config.TMP_DIR)) as td:
        stale = Path(td) / "config.json"
        stale.write_text(json.dumps({"field_limits": {"intent": 99, "emotion": 99,
                                                      "suggestion": 99, "reply_style": 99,
                                                      "reply_text": 99}}), encoding="utf-8")
        loaded = app_config.load_config(stale)
    ok_runtime = dict(loaded.field_limits) == dict(app_config.FIELD_LIMITS)
    # 自检本身必须过（当前上限下）—— 这正是事故里失败的那一步
    c = fm.FastConstrainer(TABLE, EOS_ID)
    ok_self, why = c.self_check()
    ok = ok_sync and ok_self and ok_runtime
    print(f"G 自算掩码上限与配置一致（intent={intent_limit}，旧配置也被纠正={ok_runtime}）"
          f"且自检通过：{'对' if ok else '错'}（同步={ok_sync} 自检={ok_self} {why}）")
    return ok


def case_switch_and_fallback() -> bool:
    """F：开关（`constrain="gbnf"` 不启用）＋"连续两次解析失败自动退回 GBNF"。"""
    import config as app_config
    from core.analyzer import Analyzer

    cfg = app_config.Config()
    cfg.analyzer.constrain = "gbnf"
    a = Analyzer(cfg)
    a._init_fast_constrain()                       # noqa: SLF001
    ok_off = (a.fast_enabled is False) and ("gbnf" in a.fast_note)

    cfg2 = app_config.Config()
    cfg2.analyzer.constrain = "fast"
    b = Analyzer(cfg2)
    b.fast_enabled = True                          # 假装已就绪（离线不加载模型）
    b._constrain_fail_streak = 0                   # noqa: SLF001
    b._note_constrain_result(True)                 # noqa: SLF001
    ok_keep = b.fast_enabled is True
    # 第 80 轮（用户口径："不要让纯解码轻易退到 GBNF 解码，GBNF 太慢了"）：
    # 连续解析失败**不再**切 GBNF —— 只记次数，继续用掩码。
    b._note_constrain_result(False)                # noqa: SLF001
    b._note_constrain_result(False)                # noqa: SLF001
    b._note_constrain_result(False)                # noqa: SLF001
    ok_still_fast = (b.fast_enabled is True) and b._constrain_fail_streak == 3   # noqa: SLF001
    ok = ok_off and ok_keep and ok_still_fast
    print(f"F 开关 + 永不自动退 GBNF：{'对' if ok else '错'}"
          f"（开关关闭={ok_off} 成功清零={ok_keep} 连失 3 次仍用掩码={ok_still_fast}）")
    return ok


def main() -> int:
    results = [case_program_walk(), case_masks(), case_cross_slot_token(),
               case_processor(), case_cache_key(), case_switch_and_fallback(),
               case_caps_in_sync()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
