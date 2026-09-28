#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spike_llm.py - ChatAssistant Spike #3（SPEC.md 6.1-3 / 4.3 本地模型）

验证目标（严格对应 SPEC.md，不使用任何 Mock 数据）：
  1. 加载 models/qwen2.5-1.5b-instruct-q4_k_m.gguf（llama-cpp-python，GGUF Q4_K_M），
     n_ctx=2048、max_tokens=320、temperature=0.2、n_gpu_layers=-1（加载失败自动回退 0 并记录）。
  2. GBNF 只约束"字段 + 长度"，不做语义引导（intent 由模型自由发挥，不给类别清单、不限词表）：
       danger_level 0-10 整数 / intent<=14 字 / emotion<=10 字 /
       suggestion<=80 字 / suggested_reply<=120 字 / confidence 0-1 浮点；
     输出天然简洁；Python 层再做防御截断并记录（SPEC 4.3）。
  3. 读 spikes/fixtures/llm_inputs.txt（20 条真实聊天样本）逐条真实推理，统计：
       JSON 合法率（目标 100%）、字段非空率（>=90%）、输出简洁度（各字段平均/最大字数）、
       加载时间、首 token 延迟、单条总耗时 p50/p95（GPU 验收线 <10s / CPU <30s）。
  4. JSON 解析失败时打印该样本原始输出（截断 200 字符）。
  5. 另产出人工复核清单 spikes/results/spike_llm_review.md：
       20 条"原消息 + 模型六个字段"，留一列空判定给人过目（"判断是否合理"不伪装成机器结论）。
  6. 每项 PASS/FAIL/SKIP，报告写 spikes/results/spike_llm_report.json，最后一行 SPIKE_LLM: PASS|FAIL。

复用约定：
  - 文件隔离头部、DPI（Per-Monitor-V2）感知、工作区外写入审计，全部复用 spikes/spike_wgc.py
    （import 即触发 TMP/TEMP/HF_HOME/PIP_CACHE_DIR... 重定向，早于 llama_cpp 导入）。
  - 无 Mock：真实 GGUF 文件 + 真实 llama.cpp 推理；不造数据、不裁剪样本。

判定口径（写死在报告里，避免事后解释）：
  - 判定项（决定 PASS/FAIL）全部是机检项：JSON 合法率 100%、字段非空率 >=90%、
    字段长度受 GBNF 约束（Python 防御截断 0 次）、输出简洁（suggestion 平均 <=60 字、
    suggested_reply 平均 <=80 字且最大 <=120 字）、p95 延迟达标、GPU 全量 offload、语法自检、文件隔离。
  - 不再考核"意图命中率"（用户口径：意图由模型自由发挥，判断合理即可）。
    fixtures 的 6 类标签现在只作参考，不参与判定；`--intent-mode enum` 仍可复现上一轮的
    词表受限对比（那次 9/20），仅作历史记录。
  - "判断是否合理"交人工复核：报告 human_review.status=pending，产物为 spike_llm_review.md。
  - 字段都是独立可编辑的纯文本/数值（见报告 display_template），后续 UI/配置可直接自定义
    展示模板与字段顺序，模型输出里不夹带"建议：""回复："这类前缀，也不带 Markdown。

用法：
  <py3.11> spikes/spike_llm.py
  <py3.11> spikes/spike_llm.py --limit 3 --verbose
  <py3.11> spikes/spike_llm.py --skip-sha256 --skip-human-review
  <py3.11> spikes/spike_llm.py --intent-mode enum     # 复现上一轮的词表受限对比（不作判定）
  （本机 py3.11 = cache/py311/python.exe；llama-cpp-python 0.3.4 cu124 装在 embeddable 3.11 里）

退出码：0=PASS / 1=FAIL / 2=ABORT（GPU 不可用、模型加载失败、语法非法，或 JSON 合法率<80% 时停止上报）
"""

from __future__ import annotations

import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# 复用 spike_wgc 的隔离头（必须早于 llama_cpp / 任何第三方库导入）
# --------------------------------------------------------------------------- #
SPIKES_DIR = Path(__file__).resolve().parent
if str(SPIKES_DIR) not in sys.path:
    sys.path.insert(0, str(SPIKES_DIR))
import spike_wgc as wgc  # noqa: E402  （import 即完成 TMP/TEMP/HF_HOME... 重定向）

import argparse  # noqa: E402
import ctypes  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from typing import Any, Dict, List, Optional, Sequence, Tuple  # noqa: E402


# --------------------------------------------------------------------------- #
# 常量（阈值来自 SPEC.md 4.3 / 6.1-3 及本任务要求）
# --------------------------------------------------------------------------- #
SPIKE_ID = "spike_llm"
SPIKE_TAG = "SPIKE_LLM"

WORKSPACE = Path(wgc.WORKSPACE)
FIXTURES_DIR = WORKSPACE / "spikes" / "fixtures"
LLM_INPUTS = FIXTURES_DIR / "llm_inputs.txt"
MODEL_DEFAULT = WORKSPACE / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
RESULTS_DIR = WORKSPACE / "spikes" / "results"
REPORT_JSON = RESULTS_DIR / f"{SPIKE_ID}_report.json"
LOG_TXT = RESULTS_DIR / f"{SPIKE_ID}_log.txt"
REVIEW_MD = RESULTS_DIR / f"{SPIKE_ID}_review.md"

# SPEC 4.3 / 6.1-3：推理参数
N_CTX_DEFAULT = 2048
MAX_TOKENS_DEFAULT = 320
TEMPERATURE_DEFAULT = 0.2
N_GPU_LAYERS_DEFAULT = -1
SEED_DEFAULT = 1234
# intent 默认自由发挥（不在 prompt 里给类别清单）；enum 仅用于复现上一轮的对比
INTENT_MODE_DEFAULT = "free"

# 字段长度上限（GBNF 强约束 + Python 层防御截断）
# SPEC 4.3 给的上限是 emotion<=10 / intent<=20 / suggestion<=80 / suggested_reply<=120；
# 本轮按用户口径把 intent 收到 14 字：面板一行约能放 20-25 个中文字，
# 14 字保证悬停面板里 intent 始终单行，不会撑成三四行。
FIELD_LIMITS = {"emotion": 10, "intent": 14, "suggestion": 80, "suggested_reply": 120}
REQUIRED_FIELDS = ("danger_level", "emotion", "intent", "suggestion",
                   "suggested_reply", "confidence")
STRING_FIELDS = ("emotion", "intent", "suggestion", "suggested_reply")

# danger_level 打分锚点（用户口径：给锚点说明，让 0-10 有可对齐的刻度）
DANGER_ANCHORS = (
    ("0-2", "纯日常：寒暄、约事、答话，没有情绪指向"),
    ("3-4", "有情绪但不针对你：普通请求、吐槽发泄、轻微不满"),
    ("5-6", "明确的不满、催促、追问，需要认真回应"),
    ("7-8", "指向你的追责、翻旧账、质疑，容易吵起来"),
    ("9-10", "明确冲突：最后通牒、人身攻击、要翻脸"),
)
DANGER_ANCHOR_TEXT = "；".join(f"{rng} {desc}" for rng, desc in DANGER_ANCHORS)

# 6 类意图标签（与 fixtures/llm_inputs.txt 的标注口径一致）
INTENT_LABELS = ("求助", "吐槽", "闲聊", "争议", "信息分享", "其他")

# 验收线
JSON_VALID_MIN = 1.00          # SPEC 6.1-3：JSON 合法率 100%
FIELD_NONEMPTY_MIN = 0.90      # 本任务：字段非空率 >=90%
LATENCY_P95_GPU_S = 10.0       # 本任务：GPU 验收线 p95<10s
LATENCY_P95_CPU_S = 30.0       # 本任务：CPU 验收线 p95<30s

# 输出简洁度（用户口径：字段不要过大、保持简洁；上限仍由 SPEC 4.3 的 GBNF 硬约束兜底）
SUGGESTION_MEAN_MAX_CHARS = 60     # 实测门槛：建议平均 <=60 字
REPLY_MEAN_MAX_CHARS = 80          # 实测门槛：回复平均 <=80 字
REPLY_HARD_MAX_CHARS = FIELD_LIMITS["suggested_reply"]   # 硬上限 120 字（GBNF）

# 中止线（全局规则 5：不自行改 prompt/GBNF 强行达标）
ABORT_JSON_VALID = 0.80

# llama.cpp 日志里"是否真的把层放上 GPU"的证据行
GPU_OFFLOAD_RE = re.compile(r"offloaded\s+(\d+)\s*/\s*(\d+)\s+layers to GPU")
GPU_DEVICE_RE = re.compile(r"using device CUDA\d+ \(([^)]+)\)")

logger = logging.getLogger(SPIKE_ID)

# 隔离审计基线：main() 启动时拍摄，finalize() 结束时比对
_ISOLATION_BASELINE: Dict[str, set] = {}

# llama.cpp C 日志回调（必须保持引用，否则会被 GC 掉导致崩溃）
_LLAMA_LOG_CALLBACK = None
_LLAMA_LOG_LINES: List[str] = []


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = False) -> None:
    # Windows 控制台默认 cp936，统一成 UTF-8 才能正常显示中文诊断
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
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


class Assertions:
    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def add(self, name: str, ok: Optional[bool], detail: str = "") -> bool:
        status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        self.items.append({"name": name, "status": status, "detail": detail})
        logger.info("%-6s %-38s %s", status, name, detail)
        return bool(ok)

    def has(self, name: str) -> bool:
        return any(item["name"] == name for item in self.items)

    @property
    def any_fail(self) -> bool:
        return any(item["status"] == "FAIL" for item in self.items)


# --------------------------------------------------------------------------- #
# 统计工具
# --------------------------------------------------------------------------- #
def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """线性插值百分位（与 numpy 默认 'linear' 口径一致）；空序列返回 None。"""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (p / 100.0) * (len(data) - 1)
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return data[int(lo)]
    return data[int(lo)] + (data[int(hi)] - data[int(lo)]) * (rank - lo)


def normalize_intent(text: str) -> str:
    """去掉空白与常见标点，便于做意图词的严格比较。"""
    return re.sub(r"[\s，。、,.!！?？:：;；\"'“”‘’\-—_/\\|]+", "", text or "")


def intent_matches(predicted: str, label: str) -> Tuple[bool, str]:
    """返回 (是否命中, 命中方式)。exact=完全一致；contains=标签是预测词的子串（如"求助类"）。"""
    pred = normalize_intent(predicted)
    gold = normalize_intent(label)
    if not pred or not gold:
        return False, "empty"
    if pred == gold:
        return True, "exact"
    if gold in pred:
        return True, "contains"
    return False, "mismatch"


# --------------------------------------------------------------------------- #
# 环境 / 依赖
# --------------------------------------------------------------------------- #
def prepare_cuda_dll_dirs() -> List[str]:
    """把 pip 里的 CUDA 12.4 运行库目录（nvidia-*-cu12）挂上，供 llama.dll 依赖解析。

    cu124 轮子的 llama.dll / ggml-cuda.dll 运行期需要 cudart64_12.dll、cublas64_12.dll、
    cublasLt64_12.dll。本机没有管理员权限装 CUDA 12.4 Toolkit（只需 UAC 即改走 pip 运行库），
    因此这里显式 os.add_dll_directory + PATH 注入，路径全部在工作区内。
    """
    dirs: List[str] = []
    for sp in {Path(p) for p in sys.path if p and Path(p).is_dir()}:
        root = sp / "nvidia"
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            bin_dir = child / "bin"
            if bin_dir.is_dir():
                dirs.append(str(bin_dir))
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except OSError as exc:  # 目录不存在或权限问题：记录但不致命
            logger.warning("add_dll_directory 失败 %s: %s", d, exc)
    if dirs:
        os.environ["PATH"] = ";".join(dirs) + ";" + os.environ.get("PATH", "")
    return dirs


def cuda_dll_inventory(dirs: Sequence[str]) -> List[Dict[str, Any]]:
    """记录实际用到的 CUDA 运行库文件（名称/大小），作为"真实调用 GPU"的旁证。"""
    wanted = ("cudart64", "cublas64", "cublasLt64", "nvrtc64")
    items: List[Dict[str, Any]] = []
    for d in dirs:
        for path in sorted(Path(d).glob("*.dll")):
            if any(path.name.lower().startswith(w.lower()) for w in wanted):
                items.append({"file": str(path), "size": path.stat().st_size})
    return items


def installed_versions() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        from importlib.metadata import version

        for pkg in ("llama-cpp-python", "numpy", "nvidia-cuda-runtime-cu12",
                    "nvidia-cublas-cu12", "nvidia-cuda-nvrtc-cu12"):
            try:
                out[pkg] = version(pkg)
            except Exception:
                out[pkg] = "unknown"
    except Exception as exc:  # pragma: no cover
        out["_error"] = f"{type(exc).__name__}: {exc}"
    return out


def sha256_of(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def register_llama_log_capture(llama_cpp: Any) -> None:
    """把 llama.cpp 的 C 层日志接进我们的 logger（文件里能看到 offload/KV/context 证据）。"""
    global _LLAMA_LOG_CALLBACK

    def _cb(level: int, text: Any, _user_data: Any) -> None:  # noqa: ANN001
        try:
            message = (text or b"").decode("utf-8", "replace").rstrip("\r\n")
            if not message:
                return
            _LLAMA_LOG_LINES.append(message)
            logger.debug("[llama.cpp] %s", message)
        except Exception:
            pass

    _LLAMA_LOG_CALLBACK = llama_cpp.llama_log_callback(_cb)
    llama_cpp.llama_log_set(_LLAMA_LOG_CALLBACK, ctypes.c_void_p(0))


def gpu_offload_evidence(lines: Sequence[str]) -> Dict[str, Any]:
    """从 llama.cpp 日志里提取"真的上 GPU 了"的证据。"""
    evidence: Dict[str, Any] = {"device": None, "offloaded_layers": None,
                                "total_layers": None, "matched_lines": []}
    for line in lines:
        dev = GPU_DEVICE_RE.search(line)
        if dev and evidence["device"] is None:
            evidence["device"] = dev.group(1)
        off = GPU_OFFLOAD_RE.search(line)
        if off:
            evidence["offloaded_layers"] = int(off.group(1))
            evidence["total_layers"] = int(off.group(2))
            evidence["matched_lines"].append(line)
    return evidence


def grammar_parse_errors(lines: Sequence[str]) -> List[str]:
    return [line for line in lines if "error parsing grammar" in line]


# --------------------------------------------------------------------------- #
# GBNF 语法（CJK 友好：把中文字符当成单个字符来计数）
# --------------------------------------------------------------------------- #
GRAMMAR_BASE = r'''
root ::= "{" ws dk it em sg rp cf ws "}"
dk ::= "\"danger_level\"" ws ":" ws danger
it ::= "," ws "\"intent\"" ws ":" ws __INTENT__
em ::= "," ws "\"emotion\"" ws ":" ws str10
sg ::= "," ws "\"suggestion\"" ws ":" ws str80
rp ::= "," ws "\"suggested_reply\"" ws ":" ws str120
cf ::= "," ws "\"confidence\"" ws ":" ws conf
danger ::= "0" | [1-9] | "10"
conf ::= ("0" | "1") ("." [0-9]{1,4})?
str10 ::= "\"" jchar{1,10} "\""
str14 ::= "\"" jchar{1,14} "\""
str20 ::= "\"" jchar{1,20} "\""
str80 ::= "\"" jchar{1,80} "\""
str120 ::= "\"" jchar{1,120} "\""
jchar ::= [^"\\\x00-\x1F]
ws ::= [ \t\n]*
'''


def build_grammar(intent_mode: str) -> str:
    """enum：只能输出 6 类标签之一（历史对比）；free：自由词（上限见 FIELD_LIMITS["intent"]）。"""
    if intent_mode == "enum":
        # 实测（tmp/probe_grammar.py）：组内不能写 \"....\" 转义引号字面量，否则 llama.cpp
        # 报 `error parsing grammar: expecting ')'` 并静默退化成"无语法约束"（照样出结果，
        # 但字段可以乱序/缺字段）。必须写成 "\"" (a | b) "\"" 这种形式。
        # 另外实测：GBNF 规则名不认下划线（`intent_enum` 报 `expecting newline or end`），
        # 规则名只能用 [a-zA-Z0-9-]，故用 intentenum。
        alternatives = " | ".join(f'"{label}"' for label in INTENT_LABELS)
        enum_rule = 'intentenum ::= "\\"" (' + alternatives + ') "\\""'
        return GRAMMAR_BASE.replace("__INTENT__", "intentenum") + enum_rule + "\n"
    # free：intent 自由发挥，长度上限 = FIELD_LIMITS["intent"]（当前 14 字，面板单行）
    intent_rule = {10: "str10", 14: "str14", 20: "str20"}.get(FIELD_LIMITS["intent"], "str14")
    return GRAMMAR_BASE.replace("__INTENT__", intent_rule)


def validate_grammar(llm: Any, grammar: Any, label: str) -> Dict[str, Any]:
    """probe 生成一小段，检查 llama.cpp 是否接受该 GBNF（它只在首次使用时才真正解析语法）。"""
    del _LLAMA_LOG_LINES[:]
    probe_messages = [{"role": "system", "content": "只输出 JSON。"},
                      {"role": "user", "content": "测试"}]
    error: Optional[str] = None
    try:
        stream = llm.create_chat_completion(messages=probe_messages, grammar=grammar,
                                            max_tokens=8, temperature=0.0, stream=True)
        for _chunk in stream:
            pass
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    errors = grammar_parse_errors(_LLAMA_LOG_LINES)
    if errors and error is None:
        error = errors[0]
    return {"label": label, "ok": error is None, "error": error}


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
# 自由意图（默认路径）：不给类别清单、不限词表，模型用自己的话概括意图
SYSTEM_PROMPT_FREE = (
    "你是QQ聊天内容分析助手。输入是从QQ聊天窗口中读到的“对方发来的消息”，"
    "请判断对方的意图和情绪、这条消息对接收者的危险程度，并给出处理建议和一条可直接发送的回复。\n"
    "intent：用你自己的话概括对方想干什么，词或短语都行，"
    "最多 14 个字（要把意思说完整，但别把消息原文抄进来）；\n"
    "emotion：用不超过 10 个字的词描述对方情绪；\n"
    "danger_level：0-10 的整数，按下面的锚点打分——" + DANGER_ANCHOR_TEXT +
    "；拿不准时宁可给低，不要给高；\n"
    "suggestion：给用户看的分析建议，不超过 80 字；\n"
    "suggested_reply：一条能直接发出去的回复，不超过 120 字、口吻自然；\n"
    "所有文字字段都用纯文本：不要“建议：”“回复：”这类前缀，不要 Markdown，不要换行，不要 emoji 堆砌。\n"
    "只输出 JSON，不要解释。"
)

# 词表受限模式（仅用于复现上一轮对比，不作判定）
SYSTEM_PROMPT_ENUM = (
    "你是QQ聊天内容分析助手。输入是从QQ聊天窗口中读到的“对方发来的消息”，"
    "请判断它的意图、对方的情绪、以及这条消息对接收者的危险程度，并给出处理建议和一条可直接发送的回复。\n"
    "intent 必须从下面六类里选一个（只填类别名，不要解释、不要加字）：\n"
    "- 求助：请求帮助、提问、希望你解决问题\n"
    "- 吐槽：抱怨、发泄情绪，不要求你解决\n"
    "- 闲聊：日常寒暄、找话题、约事\n"
    "- 争议：质疑、反驳、追责、表达不满，可能引发冲突\n"
    "- 信息分享：告知或转发一条信息、通知、新闻\n"
    "- 其他：确认、致谢、结束语等无法归入以上五类的内容\n"
    "danger_level 是 0-10 的整数：0 表示完全日常，10 表示极易引发冲突或需要非常谨慎。\n"
    "emotion 用不超过 10 个字的词描述对方情绪；suggestion 不超过 80 字；"
    "suggested_reply 不超过 120 字、口吻自然、可直接发送。\n"
    "只输出 JSON，不要解释、不要 Markdown 代码块。"
)

PROMPT_BY_MODE = {"enum": SYSTEM_PROMPT_ENUM, "free": SYSTEM_PROMPT_FREE}

# 字段 -> 展示位（后续 UI/配置可自定义模板、顺序、文案；这里是默认映射）
DISPLAY_TEMPLATE = {
    "fields": ["danger_level", "intent", "emotion", "suggestion", "suggested_reply",
               "confidence"],
    "default_layout": {
        "title": "{emotion} · {intent}",
        "badge": "危险度 {danger_level}/10",
        "body": "{suggestion}",
        "reply": "{suggested_reply}",
        "footer": "confidence {confidence}",
    },
    "editable": "每个字段都是独立纯文本/数值，UI 可让用户改模板、改字段顺序、"
                "直接编辑 suggestion/suggested_reply 后再发送",
}


def build_messages(text: str, intent_mode: str) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": PROMPT_BY_MODE[intent_mode]},
        {"role": "user", "content": f"对方消息：{text}"},
    ]


# --------------------------------------------------------------------------- #
# 结果解析（含 SPEC 4.3 的防御截断）
# --------------------------------------------------------------------------- #
def truncate_field(name: str, value: str, notes: List[str]) -> str:
    limit = FIELD_LIMITS.get(name)
    if limit and len(value) > limit:
        notes.append(f"{name} 超长 {len(value)}>{limit}，已截断")
        logger.warning("防御截断：字段 %s 长度 %d > %d", name, len(value), limit)
        return value[:limit]
    return value


def parse_model_json(raw: str, intent_mode: str) -> Dict[str, Any]:
    """解析模型输出。返回 dict：ok / parse_error / fields / notes / 各字段是否合法。"""
    result: Dict[str, Any] = {"ok": False, "parse_error": None, "fields": {},
                              "notes": [], "missing": [], "type_errors": []}
    try:
        payload = json.loads(raw)
    except Exception as exc:
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
        return result
    if not isinstance(payload, dict):
        result["parse_error"] = f"顶层不是 JSON 对象（{type(payload).__name__}）"
        return result

    notes: List[str] = result["notes"]
    missing = [key for key in REQUIRED_FIELDS if key not in payload]
    result["missing"] = missing

    # danger_level：0-10 整数
    danger = payload.get("danger_level")
    try:
        danger_int = int(float(str(danger).strip()))
    except Exception:
        danger_int = None
        result["type_errors"].append(f"danger_level 不是数字：{danger!r}")
    if danger_int is not None and not (0 <= danger_int <= 10):
        notes.append(f"danger_level 越界 {danger_int}，已夹到 0-10")
        danger_int = max(0, min(10, danger_int))

    # confidence：0-1 浮点
    conf = payload.get("confidence")
    try:
        conf_f = float(str(conf).strip())
    except Exception:
        conf_f = None
        result["type_errors"].append(f"confidence 不是数字：{conf!r}")
    if conf_f is not None and not (0.0 <= conf_f <= 1.0):
        notes.append(f"confidence 越界 {conf_f}，已夹到 0-1")
        conf_f = max(0.0, min(1.0, conf_f))

    strings: Dict[str, str] = {}
    for name in STRING_FIELDS:
        value = payload.get(name)
        if not isinstance(value, str):
            result["type_errors"].append(f"{name} 不是字符串：{type(value).__name__}")
            strings[name] = ""
            continue
        strings[name] = truncate_field(name, value.strip(), notes)

    result["fields"] = {
        "danger_level": danger_int,
        "emotion": strings["emotion"],
        "intent": strings["intent"],
        "suggestion": strings["suggestion"],
        "suggested_reply": strings["suggested_reply"],
        "confidence": conf_f,
    }
    result["ok"] = (not missing and not result["type_errors"]
                    and danger_int is not None and conf_f is not None)
    return result


# --------------------------------------------------------------------------- #
# 数据集
# --------------------------------------------------------------------------- #
def load_llm_inputs(path: Path) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    """读 llm_inputs.txt：每行 `文本 | 标签`。兼容无空格分隔（如第 7 行）与 UTF-8 BOM。"""
    if not path.exists():
        raise FileNotFoundError(f"缺少 fixtures：{path}")
    raw = path.read_text(encoding="utf-8-sig")
    samples: List[Dict[str, str]] = []
    malformed: List[Dict[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        if "|" not in text:
            malformed.append({"line": lineno, "content": line})
            continue
        body, label = text.split("|", 1)
        body, label = body.strip(), label.strip()
        if not body or not label:
            malformed.append({"line": lineno, "content": line})
            continue
        if label not in INTENT_LABELS:
            malformed.append({"line": lineno, "content": line,
                              "reason": f"未知标签 {label}"})
            continue
        samples.append({"text": body, "label": label, "line": lineno})
    return samples, malformed


# --------------------------------------------------------------------------- #
# 单条推理
# --------------------------------------------------------------------------- #
def run_sample(llm: Any, grammar: Any, sample: Dict[str, str], index: int,
               total: int, intent_mode: str, max_tokens: int,
               temperature: float, seed: int) -> Dict[str, Any]:
    messages = build_messages(sample["text"], intent_mode)
    t0 = time.perf_counter()
    first_token_s: Optional[float] = None
    pieces: List[str] = []
    try:
        stream = llm.create_chat_completion(
            messages=messages, grammar=grammar, max_tokens=max_tokens,
            temperature=temperature, seed=seed, stream=True,
        )
        for chunk in stream:
            if first_token_s is None:
                first_token_s = time.perf_counter() - t0
            delta = chunk["choices"][0].get("delta", {}) or {}
            pieces.append(delta.get("content") or "")
    except Exception as exc:
        logger.error("样本 %d 推理异常：%s: %s", index, type(exc).__name__, exc)
        return {"index": index, "text": sample["text"], "label": sample["label"],
                "line": sample["line"], "mode": intent_mode, "raw_output": "",
                "json_valid": False, "parse_error": f"{type(exc).__name__}: {exc}",
                "missing_fields": list(REQUIRED_FIELDS), "type_errors": [],
                "fields": {}, "defensive_truncations": [], "field_nonempty": False,
                "intent": "", "intent_hit": False, "intent_match": "error",
                "field_chars": {name: 0 for name in STRING_FIELDS},
                "first_token_ms": None,
                "total_ms": round((time.perf_counter() - t0) * 1000, 1),
                "completion_tokens": None, "chars": 0}

    total_s = time.perf_counter() - t0
    raw = "".join(pieces)
    parsed = parse_model_json(raw, intent_mode)

    fields = parsed["fields"] or {}
    nonempty = (parsed["ok"]
                and all(str(fields.get(name, "")).strip() for name in STRING_FIELDS)
                and fields.get("danger_level") is not None
                and fields.get("confidence") is not None)
    if intent_mode == "enum":
        hit, match_type = intent_matches(str(fields.get("intent", "")), sample["label"])
    else:
        # 自由意图：不做命中判定（用户口径），只保留模型原话
        hit, match_type = None, "n/a"

    if not parsed["ok"]:
        logger.error("样本 %d JSON 解析失败（%s），原始输出（截断200）：%s",
                     index, parsed["parse_error"], raw[:200].replace("\n", "\\n"))

    if first_token_s is None:
        first_token_s = total_s
    completion_tokens = None
    try:
        completion_tokens = len(llm.tokenize(raw.encode("utf-8"), add_bos=False))
    except Exception:
        pass

    record = {
        "index": index, "text": sample["text"], "label": sample["label"],
        "line": sample["line"], "mode": intent_mode,
        "raw_output": raw,
        "json_valid": bool(parsed["ok"]),
        "parse_error": parsed["parse_error"],
        "missing_fields": parsed["missing"],
        "type_errors": parsed["type_errors"],
        "fields": fields,
        "defensive_truncations": parsed["notes"],
        "field_nonempty": bool(nonempty),
        "intent": str(fields.get("intent", "")),
        "intent_hit": hit,
        "intent_match": match_type,
        "field_chars": {name: len(str(fields.get(name, ""))) for name in STRING_FIELDS},
        "first_token_ms": round(first_token_s * 1000, 1),
        "total_ms": round(total_s * 1000, 1),
        "completion_tokens": completion_tokens,
        "chars": len(raw),
    }
    logger.info("[%2d/%d] json=%s intent=%-8s danger=%-2s conf=%-4s "
                "first=%6.0fms total=%6.0fms 建议%2d字/回复%2d字%s",
                index, total, "OK" if record["json_valid"] else "BAD",
                record["intent"] or "-", fields.get("danger_level"),
                fields.get("confidence"),
                record["first_token_ms"], record["total_ms"],
                record["field_chars"]["suggestion"], record["field_chars"]["suggested_reply"],
                "" if not record["defensive_truncations"]
                else f" 截断={record['defensive_truncations']}")
    return record


def summarize(records: List[Dict[str, Any]], corpus_size: int) -> Dict[str, Any]:
    n = len(records)
    valid = [r for r in records if r["json_valid"]]
    judged = [r for r in records if r["intent_hit"] is not None]
    hits = [r for r in judged if r["intent_hit"]]
    exact_hits = [r for r in judged if r["intent_match"] == "exact"]
    total_ms = [r["total_ms"] for r in records]
    first_ms = [r["first_token_ms"] for r in records if r["first_token_ms"] is not None]
    nonempty = [r for r in records if r["field_nonempty"]]
    truncations = sum(len(r["defensive_truncations"]) for r in records)
    size_stats = {}
    for name in STRING_FIELDS:
        values = [r["field_chars"][name] for r in records if r["json_valid"]]
        size_stats[name] = {
            "limit": FIELD_LIMITS[name],
            "mean_chars": round(statistics.fmean(values), 1) if values else None,
            "max_chars": max(values) if values else None,
            "over_limit": sum(1 for v in values if v > FIELD_LIMITS[name]),
        }
    return {
        "samples": n,
        "corpus_size": corpus_size,
        "json_valid": len(valid),
        "json_valid_rate": round(len(valid) / n, 4) if n else 0.0,
        "field_nonempty": len(nonempty),
        "field_nonempty_rate": round(len(nonempty) / n, 4) if n else 0.0,
        "intent_judged": len(judged),
        "intent_hits": len(hits),
        "intent_hit_rate": round(len(hits) / len(judged), 4) if judged else None,
        "intent_exact_hits": len(exact_hits),
        "output_size": size_stats,
        "total_ms_mean": round(statistics.fmean(total_ms), 1) if total_ms else None,
        "total_ms_min": round(min(total_ms), 1) if total_ms else None,
        "total_ms_max": round(max(total_ms), 1) if total_ms else None,
        "total_ms_p50": round(percentile(total_ms, 50) or 0.0, 1) if total_ms else None,
        "total_ms_p95": round(percentile(total_ms, 95) or 0.0, 1) if total_ms else None,
        "first_token_ms_mean": round(statistics.fmean(first_ms), 1) if first_ms else None,
        "first_token_ms_p50": round(percentile(first_ms, 50) or 0.0, 1) if first_ms else None,
        "first_token_ms_p95": round(percentile(first_ms, 95) or 0.0, 1) if first_ms else None,
        "completion_tokens_total": sum(r["completion_tokens"] or 0 for r in records),
        "defensive_truncations": truncations,
        "per_label": _per_label(records),
        "confusion": _confusion(records),
    }


def _per_label(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for label in INTENT_LABELS:
        subset = [r for r in records if r["label"] == label]
        if not subset:
            continue
        out[label] = {"samples": len(subset),
                      "hits": sum(1 for r in subset if r["intent_hit"]),
                      "json_valid": sum(1 for r in subset if r["json_valid"])}
    return out


def _confusion(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """真值标签 -> 模型给出的 intent 计数，用于定位"哪两类最容易混"。"""
    table: Dict[str, Dict[str, int]] = {}
    for r in records:
        row = table.setdefault(r["label"], {})
        predicted = r["intent"] or "<解析失败>"
        row[predicted] = row.get(predicted, 0) + 1
    return table


def quality_observations(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """不用来做判定的"傻输出"体检：复读原文、字段同义重复、danger 分布。"""
    parrot = [r["index"] for r in records
              if r["json_valid"] and normalize_intent(r["fields"].get("suggested_reply", ""))
              == normalize_intent(r["text"])]
    echo_intent = [r["index"] for r in records
                   if r["json_valid"]
                   and r["fields"].get("intent") and r["fields"].get("emotion")
                   and r["fields"]["intent"] == r["fields"]["emotion"]]
    danger = [r["fields"].get("danger_level") for r in records
              if r["json_valid"] and isinstance(r["fields"].get("danger_level"), int)]
    distribution = {str(level): danger.count(level) for level in sorted(set(danger))}
    return {
        "reply_parrots_input": {"count": len(parrot), "rows": parrot,
                                "note": "suggested_reply 与输入消息完全相同的条数（应为 0）"},
        "intent_equals_emotion": {"count": len(echo_intent), "rows": echo_intent,
                                  "note": "intent 与 emotion 字段填了同一个词的条数"},
        "danger_level_distribution": distribution,
        "danger_level_mean": round(statistics.fmean(danger), 2) if danger else None,
        "danger_level_bands": {band: sum(1 for level in danger if danger_band(level) == band)
                               for band in ("0-2", "3-4", "5-6", "7-8", "9-10")},
    }


def danger_band(level: Any) -> str:
    """把一个 danger_level 落到锚点区间，便于人工复核时对齐刻度。"""
    if not isinstance(level, int):
        return "n/a"
    for band, _desc in DANGER_ANCHORS:
        low, high = band.split("-")
        if int(low) <= level <= int(high):
            return band
    return "n/a"


def write_review_markdown(records: List[Dict[str, Any]], path: Path,
                          intent_mode: str) -> None:
    """人工复核清单：20 条原消息 + 模型六个字段，判定列留空给人写。"""
    lines = [
        "# spike_llm 人工复核清单",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- intent 模式：{intent_mode}"
        + ("（自由发挥，不给类别清单）" if intent_mode == "free" else "（词表受限，仅对比用）"),
        f"- 样本：{len(records)} 条；字段上限：emotion {FIELD_LIMITS['emotion']} / "
        f"intent {FIELD_LIMITS['intent']} / suggestion {FIELD_LIMITS['suggestion']} / "
        f"suggested_reply {FIELD_LIMITS['suggested_reply']} 字",
        "",
        "## 判定口径（照这个看）",
        "",
        "- **danger_level 锚点**（0-10，本次已写进 prompt）：",
    ] + [
        f"  - `{rng}`：{desc}" for rng, desc in DANGER_ANCHORS
    ] + [
        "- **intent**：自由发挥，最多 "
        f"{FIELD_LIMITS['intent']} 个字（不要求套固定类别词）；看它是否真的概括了对方想干什么。",
        "- **emotion** 最多 "
        f"{FIELD_LIMITS['emotion']} 字、**suggestion** 最多 {FIELD_LIMITS['suggestion']} 字、"
        f"**suggested_reply** 最多 {FIELD_LIMITS['suggested_reply']} 字，且应当是纯文本、能直接编辑或发送。",
        "",
        "填法：在每条的「判定」列写 `OK` / `差` / `偏`（可追加一句理由，例如"
        "「danger 偏高」「intent 与原意相反」「建议是复读原文」）。"
        "这里只判断“模型的判断是否合理”，不看 intent 用词是否与 fixtures 标签字面一致。",
        "",
    ]
    for r in records:
        f = r["fields"] or {}
        lines += [
            f"## {r['index']}. {r['text']}",
            "",
            f"- 参考标签（仅参考）：{r['label']}",
            f"- intent：{f.get('intent', '')}",
            f"- emotion：{f.get('emotion', '')}",
            f"- danger_level：{f.get('danger_level', '')}"
            f"（锚点区间 {danger_band(f.get('danger_level'))}）"
            f"　confidence：{f.get('confidence', '')}",
            f"- suggestion：{f.get('suggestion', '')}",
            f"- suggested_reply：{f.get('suggested_reply', '')}",
            f"- 原始 JSON 合法：{r['json_valid']}"
            + (f"；耗时 {r['total_ms']} ms" if r.get("total_ms") else ""),
            "- 判定：",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ChatAssistant Spike #3：Qwen2.5-1.5B(GGUF) 结构化输出与延迟（SPEC.md 6.1-3）")
    parser.add_argument("--model", default=str(MODEL_DEFAULT), help="GGUF 模型路径")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部 20 条）")
    parser.add_argument("--n-ctx", type=int, default=N_CTX_DEFAULT)
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS_DEFAULT)
    parser.add_argument("--temperature", type=float, default=TEMPERATURE_DEFAULT)
    parser.add_argument("--n-gpu-layers", type=int, default=N_GPU_LAYERS_DEFAULT,
                        help="-1=全部层放 GPU；0=纯 CPU")
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    parser.add_argument("--intent-mode", choices=("free", "enum"), default=INTENT_MODE_DEFAULT,
                        help="free=意图自由发挥（默认，不作命中判定）；"
                             "enum=复现上一轮词表受限对比（仅记录，不作判定）")
    parser.add_argument("--skip-human-review", action="store_true",
                        help="不写 spikes/results/spike_llm_review.md 人工复核清单")
    parser.add_argument("--skip-sha256", action="store_true", help="跳过模型 sha256（省几秒）")
    parser.add_argument("--verbose", action="store_true", help="终端输出 DEBUG 日志")
    return parser.parse_args(argv)


def finalize(report: Dict[str, Any], assertions: Assertions, started: float) -> None:
    isolation = wgc.audit_isolation(_ISOLATION_BASELINE, wgc.snapshot_external_dirs())
    report["file_isolation"] = isolation
    if not assertions.has("file_isolation_no_external_write"):
        assertions.add(
            "file_isolation_no_external_write", isolation["clean"],
            f"tempfile={isolation['tempfile_gettempdir']}；"
            f"工作区外新增（可归因本项目）="
            f"{len(isolation['external_new_entries_attributable'])} 项，"
            f"（无法归因，可能系统/其他程序）="
            f"{len(isolation['external_new_entries_unattributed'])} 项；"
            f"已清理空缓存目录={isolation.get('cleaned_empty_dirs', [])}")
    if not isolation["clean"]:
        report["result"] = "FAIL"
        logger.error("发现工作区外写入，按隔离约束停止并上报：%s",
                     isolation["external_new_entries_attributable"])

    report["assertions"] = assertions.items
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report["duration_s"] = round(time.time() - started, 2)
    try:
        REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        logger.info("报告已写入：%s", REPORT_JSON)
    except OSError as exc:
        logger.error("报告写入失败：%s", exc)

    logger.info("-" * 78)
    for item in assertions.items:
        logger.info("%-6s %-38s %s", item["status"], item["name"], item["detail"])
    if report.get("abort_reason"):
        logger.error("%s: ABORTED - %s", SPIKE_TAG, report["abort_reason"])
    final = "PASS" if report["result"] == "PASS" else "FAIL"
    if isolation["clean"]:
        print(f"文件隔离确认：所有临时文件与缓存均在工作区内（{wgc.WORKSPACE}），"
              f"未发现外部写入。", flush=True)
    else:
        print(f"文件隔离确认：检测到工作区外写入！可归因本项目："
              f"{isolation['external_new_entries_attributable']}；"
              f"无法归因（可能为系统/其他程序）："
              f"{len(isolation['external_new_entries_unattributed'])} 项。", flush=True)
    print(f"{SPIKE_TAG}: {final}", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    global _ISOLATION_BASELINE
    _ISOLATION_BASELINE = wgc.snapshot_external_dirs()
    started = time.time()

    assertions = Assertions()
    report: Dict[str, Any] = {
        "spike": SPIKE_ID,
        "spec_ref": "SPEC.md 6.1-3 + 4.3",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "args": vars(args),
        "workspace_paths": wgc.workspace_paths(),
        "thresholds": {
            "json_valid_min": JSON_VALID_MIN,
            "field_nonempty_min": FIELD_NONEMPTY_MIN,
            "suggestion_mean_max_chars": SUGGESTION_MEAN_MAX_CHARS,
            "reply_mean_max_chars": REPLY_MEAN_MAX_CHARS,
            "reply_hard_max_chars": REPLY_HARD_MAX_CHARS,
            "latency_p95_gpu_s": LATENCY_P95_GPU_S,
            "latency_p95_cpu_s": LATENCY_P95_CPU_S,
            "abort_json_valid": ABORT_JSON_VALID,
            "intent_mode": args.intent_mode,
            "n_ctx": args.n_ctx, "max_tokens": args.max_tokens,
            "temperature": args.temperature, "n_gpu_layers_requested": args.n_gpu_layers,
            "field_limits": FIELD_LIMITS,
        },
        "environment": {}, "model": {}, "engine": {}, "dataset": {},
        "warmup": {}, "metrics": {}, "per_sample": [],
        "quality_observations": {}, "file_isolation": {},
        "display_template": DISPLAY_TEMPLATE,
        "rubric": {
            "intent_max_chars": FIELD_LIMITS["intent"],
            "intent_note": "intent 自由发挥，上限 14 字（比 SPEC 的 20 字更严）："
                           "悬停面板一行约 20-25 个中文字，14 字保证 intent 单行显示，"
                           "不会撑成三四行",
            "danger_level_anchors": [{"range": rng, "meaning": desc}
                                     for rng, desc in DANGER_ANCHORS],
            "danger_level_note": "锚点已写进 prompt；拿不准时要求模型给低不给高",
        },
        "human_review": {"required": True, "status": "pending",
                         "artifact": str(RESULTS_DIR / f"{SPIKE_ID}_review.md"),
                         "how": "在 spike_llm_review.md 的「判定」列写 OK/差/偏；"
                                "本 spike 只对可机检项给 PASS/FAIL，"
                                "不替人判断“模型的判断是否合理”"},
        "methodology": {
            "intent_mode": args.intent_mode,
            "free_mode": "prompt 不给类别清单、GBNF 不限定词表，intent 由模型自由发挥（<=14 字）——"
                         "用户口径：判断合理即可，不再考核意图命中率",
            "enum_mode": "prompt 给 6 类清单 + GBNF 枚举（上一轮口径，仅 --intent-mode enum 复现）",
            "reference_labels": list(INTENT_LABELS),
            "reference_labels_note": "fixtures 的 6 类标签现在只作人工复核时的参考，不参与判定",
            "judged_by_machine": ["JSON 合法率", "字段非空率", "字段长度（GBNF）", "输出简洁度",
                                  "单条耗时 p50/p95", "GPU offload", "语法自检", "文件隔离"],
            "judged_by_human": "模型的判断是否合理（spike_llm_review.md，逐条留判定列）",
            "prompt_tuned": False,
            "prompt_note": "prompt 未为提升任何指标做过调参；本轮改动来自用户口径（意图自由发挥、"
                           "只约束字段与长度）",
        },
        "design_experiment": {
            "source": "spikes/../tmp/probe_intent.py（临时探针：同一模型实例、同样 20 条样本、"
                      "temperature=0.2、seed=1234）",
            "purpose": "确定 GBNF 字段顺序这类结构选择，避免把实现缺陷当成模型能力问题；"
                       "同时留下“词表受限 vs 自由意图”的历史数据",
            "variants": [
                {"name": "V1_order_current", "field_order": "danger,emotion,intent,suggestion,reply,conf",
                 "intent_hits": "5/20", "json_valid": "20/20",
                 "note": "intent 在 emotion 之后，模型把意图类词填进 emotion"},
                {"name": "V2_order_intent_first", "field_order": "danger,intent,emotion,suggestion,reply,conf",
                 "intent_hits": "9/20", "json_valid": "20/20", "adopted": True,
                 "note": "当前采用的字段顺序"},
                {"name": "V3_order_agnostic", "field_order": "任意顺序（每键自带取值规则）",
                 "intent_hits": "10/20", "json_valid": "0/20",
                 "why_not_adopted": "顺序无关语法无法保证 6 个键齐全，Python 校验 0/20 通过"},
                {"name": "V4_intent_very_first", "field_order": "intent,danger,emotion,...",
                 "intent_hits": "9/20", "json_valid": "19/20"},
                {"name": "V5_short_prompt", "field_order": "intent,danger,emotion,...（短 prompt）",
                 "intent_hits": "10/20", "json_valid": "17/20",
                 "why_not_adopted": "短 prompt 换来 2 条 JSON 不完整，违反 100% 合法率目标"},
            ],
            "decision": "用户口径改为“意图自由发挥、不考核命中率”，因此 enum/命中率这条线不再参与判定；"
                        "字段顺序（intent 紧跟 danger_level）沿用本实验结论。",
            "caveat": "这些对比都在同一批 20 条样本上做，选中项存在过拟合风险；"
                      "正式结论应以留出集复测为准。",
            "history": [
                {"run": "2026-09-22 23:17 全量 20 条（intent=enum 词表受限）",
                 "result": "ABORT - INTENT_HIT_RATE_TOO_LOW: 45.00% < 50%",
                 "json_valid": "20/20", "field_nonempty": "20/20", "intent_hits": "9/20",
                 "load_s": 0.648, "total_ms_p50": 1931.8, "total_ms_p95": 2307.5,
                 "note": "同一份 spike_llm_report.json 已被自由意图版覆盖；指标留档于此，"
                         "需要复现可用 --intent-mode enum 重跑"},
            ],
        },
        "notes": [], "abort_reason": None, "result": "FAIL",
    }

    logger.info("=" * 78)
    logger.info("%s 开始（SPEC.md 6.1-3）", SPIKE_TAG)
    logger.info("=" * 78)
    report["environment"]["dpi_awareness"] = wgc.set_dpi_awareness()
    report["environment"]["windows_build"] = wgc.windows_build()
    report["environment"]["python"] = sys.version.split()[0]
    report["environment"]["python_executable"] = sys.executable
    report["environment"]["platform"] = platform.platform()

    # ---------------- 数据集 ---------------- #
    samples, malformed = load_llm_inputs(LLM_INPUTS)
    if args.limit:
        samples = samples[: args.limit]
    report["dataset"] = {"inputs_file": str(LLM_INPUTS), "samples": len(samples),
                         "malformed": malformed,
                         "label_distribution": {label: sum(1 for s in samples
                                                           if s["label"] == label)
                                                for label in INTENT_LABELS}}
    logger.info("数据集：%d 条（异常行 %d 条），标签分布 %s",
                len(samples), len(malformed), report["dataset"]["label_distribution"])
    assertions.add("dataset_loaded", len(samples) > 0 and not malformed,
                   f"{len(samples)} 条真实聊天样本"
                   + (f"，异常行 {malformed}" if malformed else ""))
    if not samples:
        report["abort_reason"] = "NO_VALID_SAMPLES"
        report["result"] = "ABORT"
        finalize(report, assertions, started)
        return 2

    # ---------------- CUDA 运行库 + llama_cpp 导入 ---------------- #
    cuda_dirs = prepare_cuda_dll_dirs()
    report["environment"]["cuda_dll_dirs"] = cuda_dirs
    report["environment"]["cuda_dlls"] = cuda_dll_inventory(cuda_dirs)
    try:
        import llama_cpp
    except Exception as exc:
        logger.error("llama_cpp 导入失败：%s: %s", type(exc).__name__, exc)
        assertions.add("llama_cpp_imported", False, f"{type(exc).__name__}: {exc}")
        report["abort_reason"] = f"LLAMA_CPP_IMPORT_FAILED: {type(exc).__name__}: {exc}"
        report["result"] = "ABORT"
        finalize(report, assertions, started)
        return 2
    report["environment"]["llama_cpp"] = llama_cpp.__version__
    report["environment"]["installed_packages"] = installed_versions()
    assertions.add("llama_cpp_imported", True, f"llama-cpp-python {llama_cpp.__version__}，"
                                              f"CUDA DLL 目录 {len(cuda_dirs)} 个")

    register_llama_log_capture(llama_cpp)
    system_info = llama_cpp.llama_print_system_info().decode("utf-8", "ignore")
    gpu_supported = bool(llama_cpp.llama_supports_gpu_offload())
    report["engine"]["system_info"] = system_info
    report["engine"]["gpu_offload_supported"] = gpu_supported
    logger.info("llama.cpp 系统信息：%s", system_info.strip())
    if not gpu_supported:
        # 交接规则：llama_supports_gpu_offload() 必须 True；False 就停下汇报
        assertions.add("gpu_offload_supported", False,
                       "llama_supports_gpu_offload()=False（轮子不是 CUDA 版）")
        report["abort_reason"] = "GPU_OFFLOAD_UNSUPPORTED"
        report["result"] = "ABORT"
        finalize(report, assertions, started)
        return 2
    assertions.add("gpu_offload_supported", True,
                   f"llama_supports_gpu_offload()=True；CUDA 运行库 {len(cuda_dirs)} 个目录")

    # ---------------- 模型加载（GPU 失败自动回退 CPU） ---------------- #
    model_path = Path(args.model)
    if not model_path.exists():
        assertions.add("model_file_present", False, f"缺失 {model_path}")
        report["abort_reason"] = f"MODEL_NOT_FOUND: {model_path}"
        report["result"] = "ABORT"
        finalize(report, assertions, started)
        return 2
    report["model"] = {"path": str(model_path),
                       "size_bytes": model_path.stat().st_size,
                       "size_gib": round(model_path.stat().st_size / 1024 ** 3, 3)}
    if not args.skip_sha256:
        t_hash = time.time()
        report["model"]["sha256"] = sha256_of(model_path)
        report["model"]["sha256_ms"] = round((time.time() - t_hash) * 1000, 1)
    assertions.add("model_file_present", True,
                   f"{model_path.name} {report['model']['size_gib']} GiB"
                   + (f" sha256={report['model']['sha256'][:12]}…"
                      if report["model"].get("sha256") else ""))

    from llama_cpp import Llama, LlamaGrammar  # noqa: PLC0415

    grammar = LlamaGrammar.from_string(build_grammar(args.intent_mode), verbose=False)

    gpu_layers_used = args.n_gpu_layers
    gpu_fallback = False
    load_s = None
    llm = None
    try:
        t_load = time.perf_counter()
        llm = Llama(model_path=str(model_path), n_ctx=args.n_ctx,
                    n_gpu_layers=args.n_gpu_layers, seed=args.seed,
                    temperature=args.temperature, verbose=True)
        load_s = time.perf_counter() - t_load
    except Exception as exc:
        logger.warning("n_gpu_layers=%d 加载失败（%s: %s），回退 CPU（n_gpu_layers=0）",
                       args.n_gpu_layers, type(exc).__name__, exc)
        gpu_fallback = True
        gpu_layers_used = 0
        try:
            t_load = time.perf_counter()
            llm = Llama(model_path=str(model_path), n_ctx=args.n_ctx,
                        n_gpu_layers=0, seed=args.seed,
                        temperature=args.temperature, verbose=True)
            load_s = time.perf_counter() - t_load
        except Exception as exc2:
            logger.error("CPU 回退也失败：%s: %s", type(exc2).__name__, exc2)
            assertions.add("model_loaded", False, f"{type(exc2).__name__}: {exc2}")
            report["abort_reason"] = f"MODEL_LOAD_FAILED: {type(exc2).__name__}: {exc2}"
            report["result"] = "ABORT"
            finalize(report, assertions, started)
            return 2

    evidence = gpu_offload_evidence(_LLAMA_LOG_LINES)
    report["engine"].update({
        "n_ctx": args.n_ctx,
        "n_gpu_layers_requested": args.n_gpu_layers,
        "n_gpu_layers_used": gpu_layers_used,
        "gpu_fallback": gpu_fallback,
        "load_s": round(load_s, 3) if load_s is not None else None,
        "chat_template": bool(llm.metadata.get("tokenizer.chat_template")),
        "architecture": llm.metadata.get("general.architecture"),
        "model_name": llm.metadata.get("general.name"),
        "gpu_evidence": evidence,
        "intent_mode": args.intent_mode,
        "grammar": build_grammar(args.intent_mode),
    })
    assertions.add("model_loaded", True,
                   f"{report['engine']['model_name']} 加载 {report['engine']['load_s']}s，"
                   f"n_ctx={args.n_ctx}, n_gpu_layers={gpu_layers_used}"
                   + ("（GPU 加载失败已回退 CPU）" if gpu_fallback else ""))
    if gpu_layers_used != 0:
        offloaded = evidence["offloaded_layers"] or 0
        total_layers = evidence["total_layers"] or 0
        assertions.add("gpu_layers_offloaded", total_layers > 0 and offloaded == total_layers,
                       f"{offloaded}/{total_layers} 层上 GPU；device={evidence['device']}"
                       + (f"；{evidence['matched_lines'][0]}" if evidence["matched_lines"] else ""))
    else:
        assertions.add("gpu_layers_offloaded", None, "已回退 CPU（n_gpu_layers=0），跳过")

    # ---------------- GBNF 自检（llama.cpp 惰性解析：必须显式跑一次才知道语法是否被接受） ---------------- #
    grammar_checks = [validate_grammar(llm, grammar, args.intent_mode)]
    report["engine"]["grammar_validation"] = grammar_checks
    bad_grammars = [c for c in grammar_checks if not c["ok"]]
    assertions.add("gbnf_grammar_valid", not bad_grammars,
                   "；".join(f"{c['label']}=" + ("OK" if c["ok"] else str(c["error"])[:120])
                             for c in grammar_checks))
    if bad_grammars:
        report["abort_reason"] = f"GRAMMAR_INVALID: {bad_grammars[0]['error']}"
        report["result"] = "ABORT"
        finalize(report, assertions, started)
        return 2

    # ---------------- 预热（不计入 p50/p95，单独记录） ---------------- #
    warmup = run_sample(llm, grammar,
                        {"text": "在吗", "label": "其他", "line": 0},
                        0, 1, args.intent_mode, args.max_tokens, args.temperature, args.seed)
    report["warmup"] = {k: warmup[k] for k in
                        ("text", "json_valid", "first_token_ms", "total_ms", "raw_output")}
    logger.info("预热完成：first=%.0fms total=%.0fms json=%s",
                warmup["first_token_ms"], warmup["total_ms"], warmup["json_valid"])

    # ---------------- 主评测（free = 默认；intent 由模型自由发挥） ---------------- #
    logger.info("-" * 78)
    logger.info("主评测（intent=%s，%s）：%d 条", args.intent_mode,
                "自由发挥，不给类别清单" if args.intent_mode == "free"
                else "词表受限（仅对比，不作判定）", len(samples))
    records = [run_sample(llm, grammar, sample, i + 1, len(samples), args.intent_mode,
                          args.max_tokens, args.temperature, args.seed)
               for i, sample in enumerate(samples)]
    metrics = summarize(records, len(samples))
    report["metrics"] = metrics
    report["per_sample"] = records
    report["quality_observations"] = quality_observations(records)

    # ---------------- 人工复核清单 ---------------- #
    if not args.skip_human_review:
        write_review_markdown(records, REVIEW_MD, args.intent_mode)
        logger.info("人工复核清单已写入：%s（%d 条，逐条留「判定」列）", REVIEW_MD, len(records))

    # ---------------- 断言 ---------------- #
    logger.info("-" * 78)
    assertions.add("json_valid_rate_100pct", metrics["json_valid_rate"] >= JSON_VALID_MIN,
                   f"{metrics['json_valid']}/{metrics['samples']} = "
                   f"{metrics['json_valid_rate'] * 100:.1f}%（目标 100%）")
    assertions.add("field_nonempty_rate_ge_90pct",
                   metrics["field_nonempty_rate"] >= FIELD_NONEMPTY_MIN,
                   f"{metrics['field_nonempty']}/{metrics['samples']} = "
                   f"{metrics['field_nonempty_rate'] * 100:.1f}%（目标 >=90%）")
    size = metrics["output_size"]
    concise_ok = (
        all(v["over_limit"] == 0 for v in size.values())
        and (size["suggestion"]["mean_chars"] or 0) <= SUGGESTION_MEAN_MAX_CHARS
        and (size["suggested_reply"]["mean_chars"] or 0) <= REPLY_MEAN_MAX_CHARS
        and (size["suggested_reply"]["max_chars"] or 0) <= REPLY_HARD_MAX_CHARS
    )
    latency_limit = LATENCY_P95_GPU_S if gpu_layers_used != 0 else LATENCY_P95_CPU_S
    latency_ok = (metrics["total_ms_p95"] or 0) < latency_limit * 1000
    assertions.add("field_lengths_within_limits", metrics["defensive_truncations"] == 0,
                   f"GBNF 长度约束生效：intent<={FIELD_LIMITS['intent']}/"
                   f"emotion<={FIELD_LIMITS['emotion']}/suggestion<={FIELD_LIMITS['suggestion']}/"
                   f"suggested_reply<={FIELD_LIMITS['suggested_reply']}，"
                   f"Python 层防御截断 {metrics['defensive_truncations']} 次")
    assertions.add("output_concise", concise_ok,
                   f"建议 mean={size['suggestion']['mean_chars']}字/max={size['suggestion']['max_chars']}字"
                   f"（门槛 mean<={SUGGESTION_MEAN_MAX_CHARS}），"
                   f"回复 mean={size['suggested_reply']['mean_chars']}字/"
                   f"max={size['suggested_reply']['max_chars']}字"
                   f"（门槛 mean<={REPLY_MEAN_MAX_CHARS}、max<={REPLY_HARD_MAX_CHARS}），"
                   f"intent mean={size['intent']['mean_chars']}字，"
                   f"emotion mean={size['emotion']['mean_chars']}字")
    assertions.add("latency_p95_under_limit", latency_ok,
                   f"p95={metrics['total_ms_p95']}ms（{('GPU' if gpu_layers_used != 0 else 'CPU')} "
                   f"验收线 <{latency_limit:.0f}s）；p50={metrics['total_ms_p50']}ms，"
                   f"首token p50={metrics['first_token_ms_p50']}ms/p95={metrics['first_token_ms_p95']}ms，"
                   f"加载={report['engine']['load_s']}s")
    obs = report["quality_observations"]
    assertions.add("no_parrot_reply", obs["reply_parrots_input"]["count"] == 0,
                   f"suggested_reply 照抄输入消息：{obs['reply_parrots_input']['count']} 条"
                   f"{obs['reply_parrots_input']['rows'] or ''}；"
                   f"intent 与 emotion 同词：{obs['intent_equals_emotion']['count']} 条"
                   f"{obs['intent_equals_emotion']['rows'] or ''}；"
                   f"danger_level mean={obs['danger_level_mean']}")
    if args.intent_mode == "enum":
        rate = metrics["intent_hit_rate"] or 0.0
        assertions.add("intent_hit_rate_reference", None,
                       f"词表受限对比（不作判定）：{metrics['intent_hits']}/{metrics['intent_judged']} = "
                       f"{rate * 100:.1f}%，完全一致 {metrics['intent_exact_hits']} 条")
    assertions.add("judgment_reasonableness_manual_review", None,
                   f"待人工复核：{REVIEW_MD.name}（{len(records)} 条原文+字段，逐条留「判定」列）；"
                   f"本 spike 不对“判断是否合理”下机器结论")

    # ---------------- 中止判定（规则 5：不自行改 prompt/GBNF） ---------------- #
    if metrics["json_valid_rate"] < ABORT_JSON_VALID:
        report["abort_reason"] = (f"JSON_VALID_RATE_TOO_LOW: "
                                  f"{metrics['json_valid_rate']:.2%} < {ABORT_JSON_VALID:.0%}")

    if report["abort_reason"]:
        report["result"] = "ABORT"
        logger.error("触发中止规则，停止并上报（不修改 prompt/GBNF）：%s", report["abort_reason"])
        finalize(report, assertions, started)
        return 2

    report["result"] = "PASS" if not assertions.any_fail else "FAIL"
    logger.info("指标汇总：json=%.1f%% 字段非空=%.1f%% 建议mean=%.0f字 回复mean=%.0f字 "
                "p50=%.0fms p95=%.0fms（intent=%s，不作命中判定）",
                metrics["json_valid_rate"] * 100, metrics["field_nonempty_rate"] * 100,
                size["suggestion"]["mean_chars"] or 0, size["suggested_reply"]["mean_chars"] or 0,
                metrics["total_ms_p50"] or 0, metrics["total_ms_p95"] or 0, args.intent_mode)
    finalize(report, assertions, started)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
