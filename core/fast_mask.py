#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自己算 JSON 约束掩码，替换 llama.cpp 的 GBNF（**同一套语言**，快 2.5 倍）。

【为什么要有这个模块】实测（`tmp/probe_decode_speed.py` / `probe_grammar_cost.py`，
同一台机、同一份 Qwen3-4B Q4_K_M）：
  · 不挂语法：**71.5 token/s**
  · 挂现在的 GBNF：**28.4 token/s**（2.5 倍差距）
  · 把语法简化到"只留 JSON 结构"、把 `{1,N}` 换成 `*`、把字符类换成 `[^"]`：
    27.6–28.5 token/s —— **一点没变**；
  · 换官方 0.3.34 的 CUDA 轮子重测：无语法 71.2 / 挂语法 28.4 —— **完全一样**。
  ⇒ 不是"语法写得太复杂"，而是 GBNF 每一步都要**对 15 万词表逐个模拟**的固定开销。

【这个模块怎么做】把同一套 JSON 骨架编译成：
  1) 一张**步进程序**：`lit`（固定字面量）／`text`（有上限的文本槽）／`num`（数字槽）／
     `alts`（枚举，如危险度 0-10）；
  2) 每个词表 token 的**预计算属性**（含不含引号/反斜杠/控制符、字符数、是否纯数字）；
  3) 每个状态的**布尔掩码**（懒算 + 复用），生成时每步只做一次
     `scores[~mask] = -inf`（实测 **0.22ms/token**）。

【和 GBNF 的等价性】程序按 `core/analyzer.build_grammar/build_quick_grammar/
build_reply_grammar` 逐字段照抄（字段名、顺序、字数上限、危险度枚举都一样）：
  · 文本槽：≥1 字、≤上限、字符集 = GBNF 的 `jchar`（除引号、反斜杠、控制符）；
  · 数字槽：`danger_level` 只允许 `0`…`9`/`10`（自我分析那套 3…9/10），
    `confidence` 允许 `[0-9.]` 最多 6 字符（解析端本来就会夹到 0-1）；
  · 结构字面量：只允许"剩余字面量的前缀"对应的 token，**不允许空转**（不会出现死胡同）。
  与 GBNF 的差异只有一处：GBNF 允许结构之间插任意空白（`ws`），这里按**紧凑 JSON**
  生成（和提示词里的示例一致）—— 也正因为如此，模型不能再"吐一堆空格"。

【出问题怎么办】`Analyzer` 保留原来的四个 GBNF 实例；本模块的开关是
`analyzer.constrain = "fast" | "gbnf"`，并且**连续两次解析失败会自动切回 gbnf**
（同一进程内），日志会写明原因。任何异常都不会影响"能不能用"，最坏就是回到现状。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from config import JSON_KEYS          # JSON 键名唯一来源（第 82 轮：A1/A2/A3…）

logger = logging.getLogger("core.fast_mask")

PROGRAM_QUICK = "quick"
PROGRAM_QUICK_SELF = "quick_self"
PROGRAM_REPLY = "reply"
PROGRAM_FULL = "full"

DIGITS = [str(d) for d in range(10)]
DANGER_ALL = DIGITS + ["10"]
DANGER_SELF = [str(d) for d in range(3, 10)] + ["10"]
CONF_CHARS = "0123456789."
CONF_MAX = 6


# --------------------------------------------------------------------------- #
# 1. 步进程序（照抄 analyzer 里的 GBNF）
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    """一步：`lit` 固定字面量 / `text` 文本槽 / `num` 数字槽 / `alts` 枚举。"""

    kind: str                      # "lit" | "text" | "num" | "alts"
    text: str = ""                 # lit
    limit: int = 0                 # text / num 的字数上限
    charset: str = "clean"         # text=clean（jchar）｜num 用 [0-9.]
    alts: Tuple[str, ...] = ()     # alts


def build_program(name: str) -> List[Step]:
    """把某个 GBNF 翻译成步进程序（字段名/顺序/上限与 GBNF 完全一致）。"""
    # 注意：文本槽**自己负责收尾的那个引号**（GBNF 里 stri/stre/strs/strt 都自带引号），
    # 所以槽后面那个字面量要从引号**之后**开始写（`,"emotion":"`、`,"confidence":` …）。
    intent = Step("lit", text='{"' + JSON_KEYS["intent"] + '":"')
    t_intent = Step("text", limit=13)      # 与 config.FIELD_LIMITS["intent"] 保持一致
    emotion = Step("lit", text=',"' + JSON_KEYS["emotion"] + '":"')
    t_emotion = Step("text", limit=10)
    danger_key = Step("lit", text=',"' + JSON_KEYS["danger"] + '":')
    danger = Step("alts", alts=tuple(DANGER_ALL))
    suggestion = Step("lit", text=',"' + JSON_KEYS["suggestion"] + '":"')
    t_suggestion = Step("text", limit=40)
    reply_open = Step("lit", text='{"' + JSON_KEYS["option"] + '":[')      # 单跑"三条选项"用
    reply_key = Step("lit", text=',"' + JSON_KEYS["option"] + '":[')       # 接在 suggestion 后（完整版）
    opt_a = Step("lit", text='{"style":"')
    t_style_a = Step("text", limit=6)
    text_a = Step("lit", text=',"text":"')
    t_text_a = Step("text", limit=50)
    opt_b = Step("lit", text='},{"style":"')
    t_style_b = Step("text", limit=6)
    text_b = Step("lit", text=',"text":"')
    t_text_b = Step("text", limit=50)
    opt_c = Step("lit", text='},{"style":"')
    t_style_c = Step("text", limit=6)
    text_c = Step("lit", text=',"text":"')
    t_text_c = Step("text", limit=50)
    close_opts = Step("lit", text='}]')
    conf_key = Step("lit", text=',"' + JSON_KEYS["confidence"] + '":')
    conf = Step("num", limit=CONF_MAX, charset="num")
    end = Step("lit", text="}")
    if name == PROGRAM_QUICK:
        return [intent, t_intent, emotion, t_emotion, danger_key, danger,
                suggestion, t_suggestion, conf_key, conf, end]
    if name == PROGRAM_QUICK_SELF:
        danger = Step("alts", alts=tuple(DANGER_SELF))
        return [intent, t_intent, emotion, t_emotion, danger_key, danger,
                suggestion, t_suggestion, conf_key, conf, end]
    if name == PROGRAM_REPLY:
        return [reply_open, opt_a, t_style_a, text_a, t_text_a,
                opt_b, t_style_b, text_b, t_text_b,
                opt_c, t_style_c, text_c, t_text_c, close_opts, end]
    if name == PROGRAM_FULL:
        return [intent, t_intent, emotion, t_emotion, danger_key, danger,
                suggestion, t_suggestion,
                reply_key, opt_a, t_style_a, text_a, t_text_a,
                opt_b, t_style_b, text_b, t_text_b,
                opt_c, t_style_c, text_c, t_text_c, close_opts,
                conf_key, conf, end]
    raise ValueError(f"未知程序：{name}")


def program_signature() -> str:
    """所有程序内容的指纹（用于缓存失效：程序一改，缓存键就变）。"""
    blob = []
    for name in (PROGRAM_QUICK, PROGRAM_QUICK_SELF, PROGRAM_REPLY, PROGRAM_FULL):
        for step in build_program(name):
            blob.append([step.kind, step.text, step.limit, step.charset, list(step.alts)])
    return hashlib.sha1(json.dumps(blob, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 2. 词表属性表（预计算 + 落盘缓存）
# --------------------------------------------------------------------------- #
def _classify(piece: str) -> Tuple[bool, bool, int]:
    """返回 (是否干净文本, 是否纯数字字符, 字符数)。"""
    if not piece:
        return False, False, 0
    clean = True
    num = True
    for ch in piece:
        code = ord(ch)
        if ch in '"\\' or code < 0x20:
            clean = False
        if ch not in CONF_CHARS:
            num = False
    return clean, num, len(piece)


class TokenTable:
    """词表里每个 token 的属性（干净文本 / 纯数字 / 字符数 / 原文）。"""

    def __init__(self, pieces: Sequence[str]) -> None:
        self.pieces: List[str] = list(pieces)
        n = len(self.pieces)
        self.n_vocab = n
        self.clean = np.zeros(n, dtype=bool)
        self.num = np.zeros(n, dtype=bool)
        self.nchars = np.zeros(n, dtype=np.int16)
        for tid, piece in enumerate(self.pieces):
            ok_text, ok_num, chars = _classify(piece)
            self.clean[tid] = ok_text
            self.num[tid] = ok_num
            self.nchars[tid] = min(chars, 32767)
        # 建立"字符串 → id 列表"，字面量/枚举的掩码直接从它查
        self._by_piece: Dict[str, List[int]] = {}
        for tid, piece in enumerate(self.pieces):
            self._by_piece.setdefault(piece, []).append(tid)

    # ---- 掩码构造 ----
    def _ids(self, strings: Sequence[str]) -> List[int]:
        out: List[int] = []
        for s in strings:
            out.extend(self._by_piece.get(s, ()))
        return out

    def literal_mask(self, rest: str) -> np.ndarray:
        """允许"剩余字面量的某个前缀"，且**必须推进**（不允许空 token/整段跳过）。"""
        prefixes = [rest[:i + 1] for i in range(len(rest))]
        mask = np.zeros(self.n_vocab, dtype=bool)
        ids = self._ids(prefixes)
        if ids:
            mask[np.asarray(ids, dtype=np.int64)] = True
        return mask

    def alts_forward_mask(self, allowed: Sequence[str]) -> np.ndarray:
        """枚举步：允许所有"合法前缀"（`1` 既可能是 1、也可能是 10 的一半）。"""
        prefixes = {s[:i + 1] for s in allowed for i in range(len(s))}
        mask = np.zeros(self.n_vocab, dtype=bool)
        ids = self._ids(sorted(prefixes))
        if ids:
            mask[np.asarray(ids, dtype=np.int64)] = True
        return mask

    def text_own_mask(self, remaining: int, kind: str = "clean") -> np.ndarray:
        """文本槽"继续写字"的掩码（不含收尾）。"""
        base = self.clean if kind == "clean" else self.num
        if remaining <= 0:
            return np.zeros(self.n_vocab, dtype=bool)
        return base & (self.nchars <= remaining)

    @property
    def clean_ids(self) -> List[int]:
        return [int(i) for i in np.nonzero(self.clean)[0]]

    @property
    def quote_ids(self) -> List[int]:
        return list(self._by_piece.get('"', ()))

    def quote_span_mask(self, next_literal: str) -> np.ndarray:
        """文本槽收尾用的掩码：以引号开头、并且"引号后面的内容"正好是下一步字面量前缀的 token。

        这样 `"`、`","`、`","emotion":"` 这些**跨槽 token** 都能被接受
        （GBNF 也是这个行为），同时不会放行会把 JSON 写坏的引号。
        """
        ids: List[int] = []
        for tid, piece in enumerate(self.pieces):
            if not piece.startswith('"'):
                continue
            rest = piece[1:]
            if not rest or next_literal.startswith(rest):
                ids.append(tid)
        mask = np.zeros(self.n_vocab, dtype=bool)
        if ids:
            mask[np.asarray(ids, dtype=np.int64)] = True
        return mask


def _cache_dir() -> Path:
    from config import WORKSPACE          # 延迟导入，避免循环依赖
    path = Path(WORKSPACE) / "cache" / "fast_mask"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_token_table(llm: Any, cache_key: str) -> Tuple[TokenTable, float]:
    """构造（或从磁盘读）属性表。返回 (表, 构造耗时秒)。"""
    n_vocab = int(llm.n_vocab())
    sig = program_signature()
    tag = f"{cache_key}-n{n_vocab}-{sig}"
    cache_file = _cache_dir() / f"{tag}.json"
    t0 = time.perf_counter()
    if cache_file.exists():
        try:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            pieces = raw.get("pieces")
            if isinstance(pieces, list) and len(pieces) == n_vocab:
                logger.info("fast_mask：属性表命中磁盘缓存（%d 个 token）", n_vocab)
                return TokenTable(pieces), time.perf_counter() - t0
        except Exception as exc:
            logger.warning("fast_mask：缓存读取失败（%s）→ 重新构造", exc)
    pieces: List[str] = []
    for tid in range(n_vocab):
        try:
            pieces.append(llm.detokenize([tid]).decode("utf-8", "replace"))
        except Exception:
            pieces.append("")
    table = TokenTable(pieces)
    try:
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pieces": pieces}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_file)
    except Exception as exc:
        logger.warning("fast_mask：缓存写入失败（%s，下次启动会重建）", exc)
    return table, time.perf_counter() - t0


# --------------------------------------------------------------------------- #
# 3. 状态机（纯逻辑，可离线单测）
# --------------------------------------------------------------------------- #
class Program:
    """把一个步进程序跑起来：给"当前状态"算出允许的 token 掩码，并按 token 前进。"""

    def __init__(self, steps: Sequence[Step], table: TokenTable) -> None:
        self.steps = list(steps)
        self.table = table
        self._literal_cache: Dict[int, np.ndarray] = {}
        self._state_cache: Dict[Tuple[int, int, int], np.ndarray] = {}
        self._table_cache: Dict[Tuple[str, int], np.ndarray] = {}
        self._alt_prefix_cache: Dict[int, np.ndarray] = {}

    # ---- 掩码 ----
    def _lit_mask(self, index: int, pos: int) -> np.ndarray:
        key = (index, pos)
        mask = self._literal_cache.get(key)
        if mask is None:
            mask = self.table.literal_mask(self.steps[index].text[pos:])
            self._literal_cache[key] = mask
        return mask

    def _slot_mask(self, remaining: int, kind: str) -> np.ndarray:
        key = (kind, remaining)
        mask = self._table_cache.get(key)
        if mask is None:
            mask = self.table.text_own_mask(remaining, kind)
            self._table_cache[key] = mask
        return mask

    def _close_mask(self, index: int) -> np.ndarray:
        """文本槽的"收尾"掩码（引号 + 引号后面紧跟下一步字面量）。"""
        key = ("close", index)
        mask = self._table_cache.get(key)
        if mask is None:
            mask = self.table.quote_span_mask(self._lit_text(index + 1))
            self._table_cache[key] = mask
        return mask

    def _lit_text(self, index: int) -> str:
        """第 index 步如果是字面量就返回它的文本（用来算"收尾+"跨槽 token"）。"""
        if 0 <= index < len(self.steps) and self.steps[index].kind == "lit":
            return self.steps[index].text
        return ""

    def _alts_mask(self, index: int) -> np.ndarray:
        mask = self._alt_prefix_cache.get(index)
        if mask is None:
            mask = self.table.alts_forward_mask(self.steps[index].alts)
            self._alt_prefix_cache[index] = mask
        return mask

    def mask(self, index: int, sub: int, used: int) -> Optional[np.ndarray]:
        """当前状态允许的 token 掩码；None = 程序已结束（只允许 EOS，由调用方处理）。"""
        if index >= len(self.steps):
            return None
        step = self.steps[index]
        key = (index, sub, used)
        cached = self._state_cache.get(key)
        if cached is not None:
            return cached
        if step.kind == "lit":
            mask = self._lit_mask(index, sub)
        elif step.kind == "alts":
            mask = self._alts_prefix_mask_node(index, sub)
            if self._alt_is_complete(index, sub):
                mask = mask | self._next_mask(index, used)
        else:                                    # text / num
            remaining = step.limit - used
            mask = self._slot_mask(remaining, step.charset)
            if used >= 1:                        # 至少写一个字才允许收尾（GBNF 的 {1,N}）
                if step.kind == "text":
                    mask = mask | self._close_mask(index)
                else:
                    mask = mask | self._next_mask(index, used)
        self._state_cache[key] = mask
        return mask

    def _next_mask(self, index: int, used: int) -> np.ndarray:
        """下一步的"起步掩码"（用来把"收尾"并进当前槽）。"""
        nxt = self.mask(index + 1, 0, 0)
        if nxt is None:
            return self._eos_mask()
        return nxt

    def _eos_mask(self) -> np.ndarray:
        """程序走完：只允许一个 token（调用方会给 EOS；这里空掩码表示"别再写字符"）。"""
        mask = np.zeros(self.table.n_vocab, dtype=bool)
        return mask

    # ---- 枚举（危险度）----
    def _nodes(self, index: int) -> List[Tuple[str, bool]]:
        """把枚举展开成"前缀节点"列表：[(前缀, 是否是完整值)]。"""
        steps = self.steps[index]
        seen: Dict[str, bool] = {"": False}          # 空前缀 = 起点（必须允许第一个字符）
        for alt in steps.alts:
            for i in range(len(alt)):
                prefix = alt[:i + 1]
                seen[prefix] = seen.get(prefix, False) or (prefix == alt)
        return [(p, complete) for p, complete in sorted(seen.items())]

    def _alts_prefix_mask_node(self, index: int, sub: int) -> np.ndarray:
        nodes = self._nodes(index)
        if not (0 <= sub < len(nodes)):
            return np.zeros(self.table.n_vocab, dtype=bool)
        current = nodes[sub][0]
        # 允许"当前值 + 若干字符"仍是某个合法值的前缀。
        # 注意不能直接放行"下一个节点的整串"：从节点 `1` 出发放行 `10` 会得到 `110`；
        # 正确做法是拿"剩余后缀"的所有前缀（`1` → 后缀 `0` → 放行 `0`，不放行 `10`）。
        extensions = set()
        for node, _complete in nodes:
            if node.startswith(current) and len(node) > len(current):
                tail = node[len(current):]
                extensions.update(tail[:i] for i in range(1, len(tail) + 1))
        strings = sorted(extensions)
        mask = np.zeros(self.table.n_vocab, dtype=bool)
        ids = self.table._ids(strings)          # noqa: SLF001 （同模块内直接用）
        if ids:
            mask[np.asarray(ids, dtype=np.int64)] = True
        return mask

    def _alt_is_complete(self, index: int, sub: int) -> bool:
        nodes = self._nodes(index)
        return bool(0 <= sub < len(nodes) and nodes[sub][1])

    def _alts_advance(self, index: int, sub: int, piece: str) -> Optional[int]:
        nodes = self._nodes(index)
        if not (0 <= sub < len(nodes)):
            return None
        target = nodes[sub][0] + piece
        for i, (prefix, _complete) in enumerate(nodes):
            if prefix == target:
                return i
        return None

    # ---- 前进 ----
    def advance(self, index: int, sub: int, used: int,
                piece: str) -> Optional[Tuple[int, int, int]]:
        """吃掉一个 token 后到哪；None = 这个 token 不该出现在这里（异常）。

        允许"一个 token 跨过槽边界"（GBNF 也允许）：例如文本槽那一个 token 可能是
        `好","` —— 前面喂给文本槽，剩下的 `","` 交给下一个字面量。找不到合法切法才判异常。
        """
        if index >= len(self.steps):
            return None
        step = self.steps[index]
        if step.kind == "lit":
            rest = step.text[sub:]
            if piece and rest.startswith(piece):
                pos = sub + len(piece)
                if pos >= len(step.text):
                    return (index + 1, 0, 0)
                return (index, pos, 0)
            return None
        if step.kind == "alts":
            for cut in range(len(piece), 0, -1):          # 从长到短试：能喂多少给枚举
                node = self._alts_advance(index, sub, piece[:cut])
                if node is None:
                    continue
                rest = piece[cut:]
                if not rest:
                    return (index, node, 0)
                if self._alt_is_complete(index, node):
                    nxt = self.advance(index + 1, 0, 0, rest)
                    if nxt is not None:
                        return nxt
                return None
            if self._alt_is_complete(index, sub):         # 整个 token 都是下一步的内容
                return self.advance(index + 1, 0, 0, piece)
            return None
        # text / num：先吃"本槽允许的最长前缀"，剩下的交给下一步
        remaining = step.limit - used
        cut = 0
        for i in range(1, min(len(piece), remaining) + 1):
            if self._slot_accepts(piece[:i], step.charset):
                cut = i
        rest = piece[cut:]
        new_used = used + cut
        if step.kind == "text":
            if cut and not rest:
                return (index, sub, new_used)             # 只写了字（可能正好写满上限）
            if new_used >= 1 and rest.startswith('"'):
                after = rest[1:]                          # 引号归本槽（GBNF 里也是这样）
                if not after:
                    return (index + 1, 0, 0)
                return self.advance(index + 1, 0, 0, after)
            return None
        # num 槽：没有引号，写完就交给下一步（下一步以 `}` / `,` 开头）
        if cut and not rest:
            return (index, sub, new_used)
        if used + cut >= 1:
            return self.advance(index + 1, 0, 0, rest)
        return None

    @staticmethod
    def _slot_accepts(segment: str, charset: str) -> bool:
        """这一段字符能不能当作该槽的内容（与 token 掩码同一套判据）。"""
        if not segment:
            return False
        for ch in segment:
            code = ord(ch)
            if charset == "clean":
                if ch in '"\\' or code < 0x20:
                    return False
            elif ch not in CONF_CHARS:
                return False
        return True


# --------------------------------------------------------------------------- #
# 4. 生成时用的 LogitsProcessor
# --------------------------------------------------------------------------- #
def make_processor_factory(base: Any = None):
    """返回 LogitsProcessor 的子类。

    `base=None` 时才去 `import llama_cpp`（离线单测可以传一个假基类进来，
    这样这个模块不加载模型也能测）。
    """
    if base is None:
        from llama_cpp import LogitsProcessor as base       # noqa: N813

    class FastMaskProcessor(base):
        """每步只做一次 `scores[~mask] = -inf`；状态靠"上一个采样出来的 token"前进。"""

        def __init__(self, program: Program, eos_id: int, n_vocab: int,
                     on_anomaly=None) -> None:
            self.program = program
            self.eos_id = int(eos_id)
            self.n_vocab = int(n_vocab)
            self.state: Tuple[int, int, int] = (0, 0, 0)
            self.base_len: Optional[int] = None
            self.consumed = 0
            self.anomalies = 0
            self.done = False
            self._on_anomaly = on_anomaly
            self._last_tid: Optional[int] = None

        # llama-cpp-python 每步都会调用；input_ids 是"到现在为止的完整 token 序列"
        def __call__(self, input_ids, scores):      # noqa: D102
            ids = np.asarray(input_ids)
            if self.base_len is None:
                self.base_len = int(ids.shape[0])
            while self.consumed < int(ids.shape[0]) - self.base_len:
                tid = int(ids[self.base_len + self.consumed])
                self._consume(tid)
                self.consumed += 1
            mask = self.program.mask(*self.state)
            if mask is None or not mask.any():
                # 程序走完（或极端情况没得选）→ 只放行 EOS，让生成正常结束
                allowed = np.zeros(self.n_vocab, dtype=bool)
                if 0 <= self.eos_id < self.n_vocab:
                    allowed[self.eos_id] = True
                scores[~allowed] = -np.inf
                self.done = True
                return scores
            scores[~mask] = -np.inf
            return scores

        def _consume(self, tid: int) -> None:
            if tid < 0 or tid >= self.n_vocab:
                return
            piece = self.program.table.pieces[tid]
            nxt = self.program.advance(*self.state, piece)
            if nxt is None:
                self.anomalies += 1
                if self._on_anomaly is not None:
                    self._on_anomaly(piece)
                return
            self.state = nxt
            self._last_tid = tid

        # 便于日志/诊断
        def describe(self) -> str:
            index, sub, used = self.state
            steps = self.program.steps
            if index >= len(steps):
                return "已收尾"
            step = steps[index]
            if step.kind == "lit":
                return f"字面量 {index}@ {step.text[sub:sub + 12]!r}"
            return f"{step.kind} 槽 {index}（已写 {used}/{step.limit}）"

    return FastMaskProcessor


# --------------------------------------------------------------------------- #
# 5. 门面：Analyzer 只用这个
# --------------------------------------------------------------------------- #
class FastConstrainer:
    """持有属性表 + 四个程序；给一次生成发一个新的 processor。"""

    def __init__(self, table: TokenTable, eos_id: int) -> None:
        self.table = table
        self.build_s = 0.0
        self._processor_cls: Any = None
        self.programs: Dict[str, Program] = {
            name: Program(build_program(name), self.table)
            for name in (PROGRAM_QUICK, PROGRAM_QUICK_SELF, PROGRAM_REPLY, PROGRAM_FULL)
        }
        self.eos_id = int(eos_id)

    @property
    def processor_cls(self):
        """处理器基类要 `import llama_cpp`（会加载 DLL）→ **用到时才建**。"""
        if self._processor_cls is None:
            self._processor_cls = make_processor_factory()
        return self._processor_cls

    @classmethod
    def from_llm(cls, llm: Any, cache_key: str) -> "FastConstrainer":
        """从真的 llama_cpp 实例构造（属性表可能走磁盘缓存）。"""
        table, build_s = build_token_table(llm, cache_key)
        obj = cls(table, int(llm.token_eos()))
        obj.build_s = build_s
        return obj

    def processor(self, name: str, on_anomaly=None):
        program = self.programs[name]
        return self.processor_for_program(program, on_anomaly=on_anomaly)

    def processor_for_program(self, program: Program, on_anomaly=None):
        """按**程序对象**发一个 processor（重问单字段那种临时程序走这里）。"""
        return self.processor_cls(program, self.eos_id, self.table.n_vocab,
                                  on_anomaly=on_anomaly)

    # ---- 离线自检（不需要模型）：把一个参考 JSON 走一遍状态机 ----
    def self_check(self) -> Tuple[bool, str]:
        """用"逐字符走一遍"的方式验证程序：合法 JSON 能走完、非法 JSON 走不动。"""
        good = {
            PROGRAM_QUICK: '{"A1":"问在不在","A2":"好奇","A3":1,'
                           '"A4":"他在等你回一句","A6":0.8}',
            PROGRAM_QUICK_SELF: '{"A1":"确认时间","A2":"期待","A3":6,'
                                '"A4":"挺迷人","A6":0.6}',
            PROGRAM_REPLY: '{"A5":[{"style":"认同","text":"好呀"},'
                           '{"style":"反调","text":"算了"},{"style":"吐槽","text":"离谱"}]}',
            PROGRAM_FULL: '{"A1":"问在不在","A2":"好奇","A3":1,'
                          '"A4":"他在等你回一句","A5":['
                          '{"style":"认同","text":"好呀"},{"style":"反调","text":"算了"},'
                          '{"style":"吐槽","text":"离谱"}],"A6":0.8}',
        }
        bad = {
            PROGRAM_QUICK: good[PROGRAM_QUICK].replace('"A2"', '"emo"'),
            PROGRAM_REPLY: good[PROGRAM_REPLY].replace('"text"', '"txt"'),
            PROGRAM_FULL: good[PROGRAM_FULL].replace('"A3":1', '"A3":99'),
        }
        for name, text in good.items():
            ok, why = self.walk(name, text)
            if not ok:
                return False, f"{name} 参考 JSON 走不通：{why}"
        for name, text in bad.items():
            ok, _why = self.walk(name, text)
            if ok:
                return False, f"{name} 非法 JSON 竟然走通了"
        # 长度上限也要真的挡住。
        # 第 80 轮（**事故**）：这里原来写死 15 字（= 当时 intent 上限 14 + 1）。后来把
        # `FIELD_LIMITS["intent"]` 提到 20，15 字就变成**合法**了 → 自检判"超过字数上限竟然
        # 走通了" → `fast_enabled=False` → **静默退回 GBNF**（解码从 ~60 掉到 ~20 token/s，
        # 用户实测"选项生成慢了几倍"）。所以现在**从程序本身的槽上限推导**，
        # 上限怎么调都不会再让自检误判。
        limit = next((step.limit for step in self.programs[PROGRAM_QUICK].steps
                      if step.kind == "text"), 14)
        long_intent = good[PROGRAM_QUICK].replace('"问在不在"', '"' + "问" * (int(limit) + 1) + '"')
        ok, _why = self.walk(PROGRAM_QUICK, long_intent)
        if ok:
            return False, "超过字数上限竟然走通了"
        return True, "OK"

    def walk(self, name: str, text: str) -> Tuple[bool, str]:
        """把 text 逐字符喂给状态机；走完且不越界 → True。"""
        program = self.programs[name]
        state: Tuple[int, int, int] = (0, 0, 0)
        i = 0
        while i < len(text):
            mask = program.mask(*state)
            if mask is None:
                return False, f"程序已结束，还剩 {text[i:]!r}"
            index = state[0]
            if index >= len(program.steps):
                return False, f"程序已结束，还剩 {text[i:]!r}"
            step = program.steps[index]
            # 当前状态允许的字符（与 token 掩码同一套判据）
            unit = self._allowed_unit(program, state, mask)
            if text[i] not in unit:
                return False, (f"状态 {index}/{state[1]} 不接受 {text[i]!r}"
                               f"（允许 {sorted(unit)[:12]}）")
            nxt = program.advance(*state, text[i])
            if nxt is None:
                return False, f"状态 {index}/{state[1]} 吃不下 {text[i]!r}"
            state = nxt
            i += 1
        index = state[0]
        if index < len(program.steps):
            return False, f"还没写完（停在 {program.steps[index].kind} {index}）"
        return True, "OK"

    @staticmethod
    def _allowed_unit(program: Program, state: Tuple[int, int, int],
                      mask: np.ndarray) -> set:
        """把当前掩码翻译成"允许哪些字符"（只用于自检/报错，不在热路径上）。"""
        chars = set()
        for tid in np.nonzero(mask)[0]:
            piece = program.table.pieces[int(tid)]
            if len(piece) == 1:
                chars.add(piece)
        return chars


def default_cache_key(model_path: Any, n_vocab: int) -> str:
    """缓存键：模型文件名 + 词表大小（换模型不会串）。"""
    stem = Path(str(model_path)).stem or "model"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in stem)
    return f"{safe}-{n_vocab}"
