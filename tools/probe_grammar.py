#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_grammar.py - 临时探针：确定本机 llama.cpp 能吃下的 GBNF 写法。

llama-cpp-python 0.3.4 对语法是"惰性解析"：from_string() 不报错，第一次 generate 才解析。
因此这里对每个候选写法做一次 1-token 生成，并检查 llama.cpp 日志里是否有解析错误。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

WORKSPACE = Path(r"D:\QQChatAssistant")
sys.path.insert(0, str(WORKSPACE / "spikes"))
import spike_llm as S  # noqa: E402  （复用 CUDA 路径准备 + 日志回调）

S.prepare_cuda_dll_dirs()
import llama_cpp  # noqa: E402
from llama_cpp import Llama, LlamaGrammar  # noqa: E402

S.register_llama_log_capture(llama_cpp)

MODEL = str(WORKSPACE / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf")

ENUM_GROUP_ESCAPED = r'''
root ::= "{" ws it ws "}"
it ::= "\"i\"" ws ":" ws (\"a\" | \"b\")
ws ::= [ \t\n]*
'''

ENUM_GROUP_PLAIN = r'''
root ::= "{" ws it ws "}"
it ::= "\"i\"" ws ":" ws "\"" ("a" | "b") "\""
ws ::= [ \t\n]*
'''

ENUM_RULE_ALT = r'''
root ::= "{" ws it ws "}"
it ::= "\"i\"" ws ":" ws e1 | "\"i\"" ws ":" ws e2
e1 ::= "\"a\""
e2 ::= "\"b\""
ws ::= [ \t\n]*
'''

ENUM_SINGLE_ALT = r'''
root ::= "{" ws it ws "}"
it ::= "\"i\"" ws ":" ws e1
e1 ::= "\"a\""
ws ::= [ \t\n]*
'''

CJK_LITERAL = r'''
root ::= "{" ws it ws "}"
it ::= "\"i\"" ws ":" ws "\"求助\""
ws ::= [ \t\n]*
'''

JCHAR_RULE = r'''
root ::= "{" ws it ws "}"
it ::= "\"i\"" ws ":" ws str4
str4 ::= "\"" jchar{1,4} "\""
jchar ::= [^"\\\x00-\x1F]
ws ::= [ \t\n]*
'''

CANDIDATES = {
    "enum_group_escaped": ENUM_GROUP_ESCAPED,
    "enum_group_plain": ENUM_GROUP_PLAIN,
    "enum_rule_alt": ENUM_RULE_ALT,
    "enum_single_alt": ENUM_SINGLE_ALT,
    "cjk_literal": CJK_LITERAL,
    "jchar_rule": JCHAR_RULE,
    "spike_enum_full": S.build_grammar("enum"),
    "spike_free_full": S.build_grammar("free"),
}


def try_grammar(llm: Llama, name: str, text: str) -> dict:
    del S._LLAMA_LOG_LINES[:]
    grammar = LlamaGrammar.from_string(text, verbose=False)
    messages = [{"role": "system", "content": "只输出JSON。"},
                {"role": "user", "content": "输出 i 字段"}]
    out = ""
    try:
        stream = llm.create_chat_completion(messages=messages, grammar=grammar,
                                            max_tokens=24, temperature=0.0, stream=True)
        for chunk in stream:
            out += (chunk["choices"][0].get("delta", {}) or {}).get("content") or ""
    except Exception as exc:
        out = f"<EXC {type(exc).__name__}: {exc}>"
    errors = [line for line in S._LLAMA_LOG_LINES if "error parsing grammar" in line]
    return {"name": name, "errors": errors, "output": out}


def main() -> int:
    llm = Llama(model_path=MODEL, n_ctx=512, n_gpu_layers=-1, verbose=True,
                seed=1, temperature=0.0)
    results = [try_grammar(llm, name, text) for name, text in CANDIDATES.items()]
    for r in results:
        status = "PARSE-ERROR" if r["errors"] else "ok"
        print(f"[{status:11s}] {r['name']:20s} out={r['output']!r}")
        for err in r["errors"]:
            print(f"              {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
