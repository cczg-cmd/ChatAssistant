#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/analyzer.py - 本地模型分析后端（SPEC 5.4，B1-M4）

职责：
  1) 加载 GGUF（发布默认 `qwen3-4b-instruct-2507-q4_k_m`；备选 qwen3.5-4b / 更小的型号，
     见 `config.MODEL_CANDIDATES` 与 SPEC 第 347 条），CUDA 运行库从工作区/包内注入，
     n_gpu_layers=-1，失败回退 CPU 并记录；
  2) GBNF 只约束"字段 + 长度"（intent/emotion/danger_level/suggestion/reply_options×3/confidence）；
     并做"语法自检"（llama.cpp 是惰性解析，必须真跑一次才知道语法生效）；
  3) 上下文分析：把"目标消息 + 之前最近 N 条"按 对方/我 标记喂进去；
  4) Python 侧校验：三条选项齐全、style 两两不同、text 两两不同、非空；不合格重试一次；
  5) msg_key 级缓存（LRU）+ 30s 去重（SPEC 4.4）；防御截断（SPEC 4.3）。

不做：注入 / 读内存 / 模拟键鼠 / 联网（API 后端为 v2 可选，此处不实现）。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import (DEFAULT_SELF_FIELD_NAMES, DEFAULT_SELF_FIELD_PROMPTS,
                    DEFAULT_SELF_STYLE_PROMPT, DEFAULT_STYLE_PROMPT, REPLY_OPTION_COUNT,
                    Config, JSON_KEYS, JSON_KEY_BY_LEGACY, build_system_prompt, field_name,
                    field_prompt)
from core import fast_mask as fm
from core.message_reader import Message

logger = logging.getLogger("core.analyzer")

# JSON 键名（第 82 轮：抽象成 A1/A2/A3…）—— 正则、GBNF、解析都从这几个常量取，
# 不要在别处手写键名；改键名只改 config.JSON_KEYS。
K_INTENT = JSON_KEYS["intent"]
K_EMOTION = JSON_KEYS["emotion"]
K_DANGER = JSON_KEYS["danger"]
K_SUGGESTION = JSON_KEYS["suggestion"]
K_OPTION = JSON_KEYS["option"]
K_CONFIDENCE = JSON_KEYS["confidence"]

# --------------------------------------------------------------------------- #
# 流式提前上屏用的正则 / 立场与方向池
#   （第 80 轮整理时这几个常量被一次区间删除误伤过，已从**上一版打包产物**里
#     原样反出来恢复；`_SUGGESTION_ANGLES*` 那两个"写作角度"池按用户口径不再使用，
#      所以没有恢复 —— 见 SPEC 第 336 条。）
# --------------------------------------------------------------------------- #
# 第 82 轮：JSON 键名抽象成 A1/A2/A3…（config.JSON_KEYS）——正则同时认新键名与旧键名，
# 免得 API 模型偶尔按老习惯回 intent/emotion 时流式上屏失效。
_RE_INTENT_FIELD = re.compile(r'"(?:' + K_INTENT + r'|intent)\s*:\s*"([^"]*)"')
_RE_EMOTION_FIELD = re.compile(r'"(?:' + K_EMOTION + r'|emotion)\s*:\s*"([^"]*)"')
_RE_DANGER_FIELD = re.compile(r'"(?:' + K_DANGER + r'|danger_level)\s*:\s*(\d{1,2})')
_RE_OPTION_ITEM = re.compile(
    r'\{\s*"style"\s*:\s*"([^"]*)"\s*,\s*"text"\s*:\s*"([^"]*)"\s*\}')

# 三条回复选项的"说法"池（第 59 轮：逐条指定立场，说法从三档里各抽一个再打乱）
_REPLY_ANGLES = ("关心", "吐槽", "反问", "玩笑", "冷淡", "认真", "催促", "卖萌",
                 "敷衍", "共情", "答应", "解释", "装傻", "调侃", "客气", "直给")
_REPLY_ANGLES_PLAIN = ("附和一句", "只说自己感想", "一两个词带过", "随口给个评价",
                       "说自己此刻状态", "短句带过")
_REPLY_ANGLES_OFFBEAT = ("拐个小方向", "摆烂式应付", "自嘲一句", "吐槽一句")

# 第 82 轮末（用户指出："套路化是**跨消息**用同一批开头，不是单批次内不变化"）：
# 跨消息重复的根因是"注入几乎每次都一样 + 低温" → 第一句话的分布被钉死在最可能的那个 token 上，
# 于是不同消息都用同一批起手词（实测：升温到 0.5 也没改善，38%→42%）。
# 解法：**每批随机指定三条各自的开头类型**（结构性提示，不给任何示例词）——
# 注入每次都不同，第一句话的分布就跟着变；实测能把跨消息重复率砍到一半以下。
_OPENING_TYPES = ("直述", "疑问或反问", "短叹词或半截话", "先接对方一句", "直接给结论",
                  "称呼对方", "用一个具体细节", "自问自答")



class GenerationCancelled(Exception):
    """用于"目标已切换 → 放弃当前生成"的控制流信号（不是错误）。"""


_QUOTE_CHARS = "\"“”'‘’「」『』《》()（）[]【】{}<>"


class CancelToken:
    """线程安全的取消标志：主线程/队列置位，生成线程在每个 token 边界检查。"""

    __slots__ = ("_flag",)

    def __init__(self) -> None:
        self._flag = False

    def cancel(self) -> None:
        self._flag = True

    @property
    def cancelled(self) -> bool:
        return self._flag


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #
@dataclass
class ReplyOption:
    style: str
    text: str

    def as_dict(self) -> Dict[str, str]:
        return {"style": self.style, "text": self.text}


@dataclass
class AnalysisResult:
    msg_key: str
    intent: str = ""
    emotion: str = ""
    danger_level: Optional[int] = None
    suggestion: str = ""
    replies: List[ReplyOption] = field(default_factory=list)
    confidence: Optional[float] = None
    json_ok: bool = False
    backend: str = "local"
    degraded: bool = False                 # 选项不全/语法失效等降级情况
    notes: List[str] = field(default_factory=list)
    raw: str = ""
    first_token_ms: Optional[float] = None
    total_ms: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    cache_hit: bool = False
    partial: bool = False                  # 两段式第一段：只有意图/情绪/危险度，回复还没生成
    wait_ms: Optional[float] = None        # 排队等待（诊断用）
    self_analysis: bool = False            # 第 79 轮：这条是"我发的消息"的分析
    #   （字段含义整体换成 质量评分 / 消息点评 / 发言选项 → 面板配色与标题也跟着换）

    def as_dict(self) -> Dict[str, Any]:
        return {"msg_key": self.msg_key, "intent": self.intent, "emotion": self.emotion,
                "danger_level": self.danger_level, "suggestion": self.suggestion,
                "replies": [r.as_dict() for r in self.replies],
                "confidence": self.confidence, "json_ok": self.json_ok,
                "backend": self.backend, "degraded": self.degraded, "notes": self.notes,
                "first_token_ms": self.first_token_ms, "total_ms": self.total_ms,
                "cache_hit": self.cache_hit}


# --------------------------------------------------------------------------- #
# CUDA 运行库注入（SPEC 6.2：pip 的 nvidia-*-cu12 即可，无需 Toolkit）
# --------------------------------------------------------------------------- #
def _nvidia_gpu_present() -> Tuple[bool, str]:
    """机器上**有没有 NVIDIA GPU/驱动**（决定要不要尝试 GPU 推理）。

    第 73 轮第一次写得太保守：还要求"包内必须有 cudart64_12.dll"，结果有 N 卡的机器
    也可能被误判成 CPU。用户口径是"**尽可能用 GPU**"，所以这里只认一条硬信号：
    `System32\\nvcuda.dll`（NVIDIA 驱动安装后必然存在）。有它就**一律尝试 GPU** ——
    CUDA 运行库缺失或驱动太旧时，llama.cpp 会报错，我们再回退 CPU 并记日志（那条路径本来就存在）。
    只有**确定没有 N 卡**（没有 nvcuda.dll）才直接走 CPU：那种机器上加载 758MB 的
    ggml-cuda.dll 只会失败/干等。
    """
    driver = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvcuda.dll"
    if not driver.exists():
        return False, "未检测到 NVIDIA 驱动（System32\\nvcuda.dll 不存在）"
    return True, "检测到 NVIDIA 驱动"


def _cuda_bin_dirs(cfg: Config) -> List[str]:
    dirs: List[str] = []
    seen = set()
    roots = [
        Path(sys.prefix) / "Lib" / "site-packages" / "nvidia",   # pip 安装的 CUDA 运行库
        Path(cfg.paths.workspace) / "cache" / "py311" / "Lib" / "site-packages" / "nvidia",
        Path(cfg.paths.app_dir) / "nvidia",                       # 打包时可随包分发
        Path(cfg.paths.app_dir) / "cuda",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for bin_dir in sorted(root.glob("*/bin")) + [root / "bin"]:
            if bin_dir.is_dir() and str(bin_dir) not in seen:
                seen.add(str(bin_dir))
                dirs.append(str(bin_dir))
    # PATH 里已有的 CUDA 12 运行库（例如系统装了 CUDA 12.x）
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or entry in seen:
            continue
        try:
            if (Path(entry) / "cudart64_12.dll").exists():
                seen.add(entry)
                dirs.append(entry)
        except OSError:
            continue
    return dirs


# --------------------------------------------------------------------------- #
# GBNF 语法（只约束字段与长度；规则名不含下划线、组内不写转义引号——两条实测坑）
# --------------------------------------------------------------------------- #
def build_grammar(cfg: Config) -> str:
    lim = cfg.field_limits
    return f'''
root ::= "{{" ws k1 k2 k3 k4 k5 k6 ws "}}"
k1 ::= "\\"{K_INTENT}\\"" ws ":" ws stri
k2 ::= "," ws "\\"{K_EMOTION}\\"" ws ":" ws stre
k3 ::= "," ws "\\"{K_DANGER}\\"" ws ":" ws danger
k4 ::= "," ws "\\"{K_SUGGESTION}\\"" ws ":" ws strs
k5 ::= "," ws "\\"{K_OPTION}\\"" ws ":" ws "[" ws opt ws "," ws opt ws "," ws opt ws "]"
k6 ::= "," ws "\\"{K_CONFIDENCE}\\"" ws ":" ws conf
opt ::= "{{" ws "\\"style\\"" ws ":" ws strstyle ws "," ws "\\"text\\"" ws ":" ws strt ws "}}"
danger ::= "0" | [1-9] | "10"
conf ::= ("0" | "1") ("." [0-9]{{1,4}})?
stri ::= "\\"" jchar{{1,{lim["intent"]}}} "\\""
stre ::= "\\"" jchar{{1,{lim["emotion"]}}} "\\""
strs ::= "\\"" jchar{{1,{lim["suggestion"]}}} "\\""
strstyle ::= "\\"" jchar{{1,{lim["reply_style"]}}} "\\""
strt ::= "\\"" jchar{{1,{lim["reply_text"]}}} "\\""
jchar ::= [^"\\\\\\x00-\\x1F]
ws ::= [ \\t\\n]*
'''


def build_quick_grammar(cfg: Config, self_mode: bool = False) -> str:
    """两段式第一段：intent/emotion/danger_level/**suggestion**/confidence。

    用户口径：建议要跟意图一起出（面板先给结论+建议），三条回复再单独补。

    self_mode=True（第 79 轮）：第三个字段是"质量评分"（越高越好）。4B 会把
    "0 分"当成默认值乱写（提示词里写"不要写 0"反而更容易写 0 —— 实测 5 次里 2 次），
    所以在**语法层**把下限钉在 3：自我分析只允许 3-10。
    """
    lim = cfg.field_limits
    danger_rule = '[3-9] | "10"' if self_mode else '"0" | [1-9] | "10"'
    return f'''
root ::= "{{" ws k1 k2 k3 k4 k5 ws "}}"
k1 ::= "\\"{K_INTENT}\\"" ws ":" ws stri
k2 ::= "," ws "\\"{K_EMOTION}\\"" ws ":" ws stre
k3 ::= "," ws "\\"{K_DANGER}\\"" ws ":" ws danger
k4 ::= "," ws "\\"{K_SUGGESTION}\\"" ws ":" ws strs
k5 ::= "," ws "\\"{K_CONFIDENCE}\\"" ws ":" ws conf
danger ::= {danger_rule}
conf ::= ("0" | "1") ("." [0-9]{{1,4}})?
stri ::= "\\"" jchar{{1,{lim["intent"]}}} "\\""
stre ::= "\\"" jchar{{1,{lim["emotion"]}}} "\\""
strs ::= "\\"" jchar{{1,{lim["suggestion"]}}} "\\""
jchar ::= [^"\\\\\\x00-\\x1F]
ws ::= [ \\t\\n]*
'''


def build_reply_grammar(cfg: Config) -> str:
    """两段式第二段：只要三条 reply_options（suggestion 已经在第一段给出）。"""
    lim = cfg.field_limits
    return f'''
root ::= "{{" ws k1 ws "}}"
k1 ::= "\\"{K_OPTION}\\"" ws ":" ws "[" ws opt ws "," ws opt ws "," ws opt ws "]"
opt ::= "{{" ws "\\"style\\"" ws ":" ws strstyle ws "," ws "\\"text\\"" ws ":" ws strt ws "}}"
strstyle ::= "\\"" jchar{{1,{lim["reply_style"]}}} "\\""
strt ::= "\\"" jchar{{1,{lim["reply_text"]}}} "\\""
jchar ::= [^"\\\\\\x00-\\x1F]
    ws ::= [ \\t\\n]*
'''


def _make_cancel_processor(token: CancelToken) -> Any:
    """构造一个"token 级取消"的 logits 处理器。

    llama-cpp-python 的 chat completion 不暴露 stopping_criteria，但暴露 logits_processor。
    处理器在采样回调里抛异常即可中止生成；实测抛异常后模型仍可继续正常生成（见测试）。
    """
    from llama_cpp import LogitsProcessor

    class _CancelProcessor(LogitsProcessor):
        def __call__(self, input_ids, scores):
            if token.cancelled:
                raise GenerationCancelled("generation cancelled")
            return scores

    return _CancelProcessor()


# --------------------------------------------------------------- 模板兼容垫片
def patch_jinja_generation_tags() -> None:
    """让 llama-cpp-python 0.3.22 读懂带 `{% generation %}` 标签的 GGUF 模板。

    llama.cpp 在 2025 年加了 `{% generation %}` / `{% endgeneration %}` 这对 jinja 标签
    （标注"这段是模型生成的"，训练/RL 用），LFM2.5 等新模型的 GGUF 模板里就带着它。
    0.3.22 内置的 jinja2 不认识这个标签，`Llama()` 构造时直接抛 TemplateSyntaxError，
    于是模型无论 GPU 还是 CPU 都加载不了。

    这里在编译模板前把这对外层标签**去掉**：它们只做标注，不影响渲染出来的文本，
    所以去掉是安全的，也不会改动模型自己的对话内容。幂等，可重复调用。
    """
    import re

    import llama_cpp.llama_chat_format as cf

    fmt = cf.Jinja2ChatFormatter
    if getattr(fmt, "_generation_tag_patched", False):
        return
    orig_init = fmt.__init__
    tag_re = re.compile(r"\{%-?\s*(?:generation|endgeneration)\s*-?%\}")

    def init(self: Any, template: str, *args: Any, **kwargs: Any) -> None:
        if isinstance(template, str) and "generation" in template:
            template = tag_re.sub("", template)
        orig_init(self, template, *args, **kwargs)

    fmt.__init__ = init                 # type: ignore[method-assign]
    fmt._generation_tag_patched = True  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# 分析器
# --------------------------------------------------------------------------- #
class Analyzer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.model_path: Optional[Path] = None
        self.llm: Any = None
        self.grammar: Any = None
        self.grammar_quick: Any = None
        self.grammar_reply: Any = None
        self.load_s: Optional[float] = None
        self.gpu_layers_used: Optional[int] = None
        self.gpu_fallback = False
        self.grammar_ok = False
        self.grammar_error: Optional[str] = None
        # 第 80 轮：JSON 约束可以"自己算掩码"（GBNF 每步 25ms 的掩码开销 → 0.22ms）
        self.fast: Any = None
        self.fast_enabled = False
        self.fast_note = "未初始化"
        self._constrain_fail_streak = 0
        self.last_error: Optional[str] = None
        self.cuda_dirs: List[str] = []
        self.gpu_evidence: Dict[str, Any] = {}
        self._cache: "OrderedDict[str, AnalysisResult]" = OrderedDict()
        self._cache_lock = threading.Lock()     # 缓存会被主线程（手动刷新）与工作线程同时访问
        self._log_lines: List[str] = []
        self._log_callback = None
        self.last_prompt_tokens = 0
        self.last_completion_tokens: Optional[int] = None
        # 手动刷新回复时的一次性采样参数：换种子 + 临时升温（否则固定 seed + 0.2 温度
        # 会生成几乎逐字相同的结果）。
        #   第 80 轮实测（tmp/probe_refresh2.py，Qwen3.5-4B）：把上一版三条**原文**喂回提示词
        #   并不能"避重" —— 4B 会直接把那三行照抄回来（有时连注入的 JSON 片段一起抄，
        #   面板上出现半截 `"},`），用户看到的就是"刷新后一模一样"。现在刷新只靠
        #   **新种子 + 高温**：实测两轮刷新与上一版 0/3 重合。
        #   主线程写、工作线程在"本次请求"内取用并还原（见 refresh_snapshot/refresh_release）。
        self.extra_seed: int = 0
        self.temp_override: Optional[float] = None
        # 第 82 轮末（用户指出：套路化是**跨消息**用同一批起手词）：记住最近几次生成用过的
        # 起手词（只存前 2 个字，**不存整句** —— 存整句会被 4B 照抄回来，第 346 条踩过），
        # 下次生成时把它们贴进注入里要求换掉。只在本地路径、同一条会话里滚动。
        self.recent_openings: List[str] = []
        self.budget_blocked: bool = False       # 上一次是否因预算被拦（供 UI 提示）
        self._api_cooldown_until: float = 0.0   # API 刚失败时的冷却，避免同一轮重复请求

    def _temperature(self) -> float:
        return (self.temp_override if self.temp_override is not None
                else self.cfg.analyzer.temperature)

    def refresh_snapshot(self) -> Tuple[int, Optional[float]]:
        """本次请求开始时的"手动刷新"参数快照（工作线程调用，见 `refresh_release`）。"""
        return (self.extra_seed, self.temp_override)

    def refresh_release(self, snapshot: Tuple[int, Optional[float]]) -> None:
        """本次请求结束：把刷新用的一次性参数还原成默认。

        第 80 轮修 bug：原来是主线程在 `on_analysis_ready()` 里清 —— 但两段式的**第一段**
        结果也会走到那段清理，第二段往往就只剩 0.2 温度了（"刷新后内容一模一样"的另一半原因）。
        现在改成"谁用谁还"：工作线程这次请求用完了才还，且**期间用户又点了一次刷新就不还**
        （留着给下一次请求用）。
        """
        if (self.extra_seed, self.temp_override) == snapshot:
            self.extra_seed, self.temp_override = 0, None

    def _stage_max_tokens(self, stage: str) -> int:
        """两段式各阶段的输出上限。

        **本地与 API 分开**（用户口径：本地只花时间、不花钱，不必压 token）：
          - API：用 `api.max_tokens_quick` / `api.max_tokens_replies`（省钱，压到 72/200）；
          - 本地：用 `analyzer.max_tokens`（320，够长就行；实际生成到 EOS 就停）。
        """
        if self.api_enabled:
            api = self.cfg.api
            return int(api.max_tokens_quick if stage == "quick" else api.max_tokens_replies)
        return int(self.cfg.analyzer.max_tokens)

    # ---------------- API 用量与预算（B2 第 43 轮） ----------------
    def _usage_path(self) -> Path:
        api = self.cfg.api
        if (api.usage_file or "").strip():
            return Path(api.usage_file)
        return Path(self.cfg.paths.log_dir) / "api_usage.json"

    def load_usage(self) -> Dict[str, Any]:
        """读取按天累计的 API 用量（文件不存在/损坏都当空处理）。"""
        try:
            return json.loads(self._usage_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def today_usage(self) -> Dict[str, int]:
        day = time.strftime("%Y-%m-%d")
        return dict(self.load_usage().get(day, {}))

    def _add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        day = time.strftime("%Y-%m-%d")
        data = self.load_usage()
        entry = data.setdefault(day, {"requests": 0, "prompt_tokens": 0,
                                      "completion_tokens": 0, "total_tokens": 0})
        entry["requests"] += 1
        entry["prompt_tokens"] += int(prompt_tokens or 0)
        entry["completion_tokens"] += int(completion_tokens or 0)
        entry["total_tokens"] += int(prompt_tokens or 0) + int(completion_tokens or 0)
        try:
            self._usage_path().write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        except Exception as exc:
            logger.warning("写入 API 用量失败：%s", exc)

    def api_budget_ok(self) -> Tuple[bool, str]:
        """调用 API 前的预算检查。返回 (是否允许, 说明)。"""
        budget = int(getattr(self.cfg.api, "daily_token_budget", 0) or 0)
        if budget <= 0:
            return True, "未设上限"
        used = int(self.today_usage().get("total_tokens", 0))
        if used >= budget:
            return False, f"今日已用 {used} token ≥ 上限 {budget}"
        return True, f"今日已用 {used}/{budget}"

    # ---------------- 生命周期 ----------------
    @property
    def api_enabled(self) -> bool:
        """设置里选了 API 后端且填了 key（默认是本地部署）。"""
        return (self.cfg.analyzer.backend == "api"
                and bool((self.cfg.api.api_key or "").strip()))

    def _register_log_capture(self, llama_cpp: Any) -> None:
        def _cb(level: int, text: Any, _user: Any) -> None:      # noqa: ANN001
            try:
                message = (text or b"").decode("utf-8", "replace").rstrip("\r\n")
                if message:
                    self._log_lines.append(message)
                    logger.debug("[llama.cpp] %s", message)
            except Exception:
                pass

        self._log_callback = llama_cpp.llama_log_callback(_cb)
        llama_cpp.llama_log_set(self._log_callback, ctypes.c_void_p(0))

    def load(self, model_path: Optional[Path] = None) -> bool:
        """加载模型（一次）；失败返回 False 并记录原因。"""
        import config as app_config

        if self.api_enabled:
            # API 模式：不占显存、不加载本地模型；API 失败时才懒加载本地兜底
            logger.info("分析后端=API（%s / %s），本地模型按需加载",
                        self.cfg.api.base_url, self.cfg.api.model)
            self.last_error = None
            return True

        path = model_path or app_config.resolve_model_path(self.cfg)
        if path is None or not Path(path).exists():
            self.last_error = "模型文件不存在（检查 config.paths.model 或 models/ 目录）"
            logger.error(self.last_error)
            return False
        self.model_path = Path(path)

        self.cuda_dirs = _cuda_bin_dirs(self.cfg)
        for directory in self.cuda_dirs:
            try:
                os.add_dll_directory(directory)
            except OSError:
                pass
        if self.cuda_dirs:
            os.environ["PATH"] = ";".join(self.cuda_dirs) + ";" + os.environ.get("PATH", "")

        try:
            import llama_cpp
            from llama_cpp import Llama, LlamaGrammar
        except Exception as exc:
            self.last_error = f"llama_cpp 导入失败: {type(exc).__name__}: {exc}"
            logger.error(self.last_error)
            return False

        # 新模型的 GGUF 模板可能带 `{% generation %}` 标签，旧版后端不认 → 加载必崩。
        patch_jinja_generation_tags()

        self._register_log_capture(llama_cpp)
        a = self.cfg.analyzer
        # 第 73 轮：只有"确认没有 N 卡"才直接走 CPU；有 N 卡一律尝试 GPU（用户口径：
        # 尽可能用 GPU）。CUDA 运行库缺失/驱动太旧时下面的 try 会失败并回退 CPU。
        layers = int(a.n_gpu_layers)
        if layers != 0:
            gpu_ok, why = _nvidia_gpu_present()
            if gpu_ok:
                has_cudart = any((Path(d) / "cudart64_12.dll").exists() for d in self.cuda_dirs)
                logger.info("%s → 尝试 GPU 推理（n_gpu_layers=%d；CUDA 12 运行库%s）", why, layers,
                            "就绪" if has_cudart else "未随包找到，将由 llama.cpp 决定能否加载")
            else:
                logger.warning("%s → 这台机器没有 N 卡，改用 CPU（n_gpu_layers=0）", why)
                layers = 0
        logger.info("开始加载模型：%s（n_ctx=%d，n_gpu_layers=%d）…",
                    self.model_path.name, a.n_ctx, layers)
        if (getattr(a, "chat_format", "") or "").strip():
            logger.info("chat 模板被覆盖为 %r（统一走 ChatML：Qwen3-2507 本身就是 ChatML 非思考；"
                        "Qwen3.5/LFM2.5 靠它关掉模型自带的 thinking）", a.chat_format)
        elif "3.5" in self.model_path.name.lower():
            logger.warning("模型名看起来像 Qwen3.5，但 analyzer.chat_format 为空 → 可能仍会"
                           "先 thinking 再输出；要关闭请把 chat_format 设为 'chatml'")
        if layers != 0:
            # 首次在 GPU 上加载要初始化 CUDA 上下文 + 编译/缓存该显卡的内核，
            # 慢是正常的（第二次启动会命中缓存快很多）—— 写清楚，免得以为卡死。
            logger.info("提示：首次用 GPU 加载可能要 30-120 秒（编译显卡内核，缓存到 "
                        "%LOCALAPPDATA%\\NVIDIA\\ComputeCache）；第二次启动会快很多")

        def _construct(layers: int) -> Any:
            del self._log_lines[:]
            # 第 75 轮：`chat_format` 非空时覆盖模型自带模板 —— 用来"关 thinking"
            # （Qwen3.5 默认先思考；chatml = Qwen 的非思考格式，assistant 回合不带  thinking 块）。
            kwargs: Dict[str, Any] = {}
            if (getattr(a, "chat_format", "") or "").strip():
                kwargs["chat_format"] = a.chat_format.strip()
            # 提速参数（实测解码 67 → 72.5 tok/s，约 +8%，对输出内容没有任何影响）：
            #   n_batch/n_ubatch 1024：提示词分块更大，预填充更快
            #   flash_attn：CUDA 下的注意力内核，解码更快
            # 万一某个构建/显卡不支持，下面的 except 会用保守参数重试一次，不会因此加载失败。
            kwargs["n_batch"] = int(getattr(a, "n_batch", 1024) or 1024)
            kwargs["n_ubatch"] = int(getattr(a, "n_ubatch", 1024) or 1024)
            if getattr(a, "flash_attn", True):
                kwargs["flash_attn"] = True
            return Llama(model_path=str(self.model_path), n_ctx=a.n_ctx,
                         n_gpu_layers=layers, seed=a.seed,
                         temperature=a.temperature, verbose=True, **kwargs)

        try:
            t0 = time.perf_counter()
            self.llm = _construct(layers)
            self.load_s = round(time.perf_counter() - t0, 3)
            self.gpu_layers_used = layers
        except Exception as exc:
            # flash_attn / n_batch 之类的参数不被该构建支持时，先退回"不带这些参数"重试一次
            if getattr(a, "flash_attn", True) or int(getattr(a, "n_batch", 1024)) != 512:
                logger.warning("加载失败（%s: %s）→ 用保守参数重试一次", type(exc).__name__, exc)
                try:
                    a_flash, a_batch, a_ubatch = a.flash_attn, a.n_batch, a.n_ubatch
                    a.flash_attn, a.n_batch, a.n_ubatch = False, 512, 512
                    t0 = time.perf_counter()
                    self.llm = _construct(layers)
                    self.load_s = round(time.perf_counter() - t0, 3)
                    self.gpu_layers_used = layers
                    logger.warning("保守参数加载成功（本次不用 flash_attn / 大 batch）")
                    return True
                except Exception as exc2:
                    logger.warning("保守参数也失败：%s: %s", type(exc2).__name__, exc2)
                finally:
                    a.flash_attn, a.n_batch, a.n_ubatch = a_flash, a_batch, a_ubatch
            if not a.gpu_fallback_to_cpu:
                self.last_error = f"模型加载失败: {type(exc).__name__}: {exc}"
                logger.error(self.last_error)
                return False
            logger.warning("GPU 加载失败（%s: %s），回退 CPU", type(exc).__name__, exc)
            try:
                t0 = time.perf_counter()
                self.llm = _construct(0)
                self.load_s = round(time.perf_counter() - t0, 3)
                self.gpu_layers_used = 0
                self.gpu_fallback = True
            except Exception as exc2:
                self.last_error = f"CPU 回退也失败: {type(exc2).__name__}: {exc2}"
                logger.error(self.last_error)
                return False

        self.gpu_evidence = self._gpu_evidence()
        self.grammar = LlamaGrammar.from_string(build_grammar(self.cfg), verbose=False)
        self.grammar_quick = LlamaGrammar.from_string(build_quick_grammar(self.cfg), verbose=False)
        # 第 79 轮：自我分析的"质量评分"下限 3（见 build_quick_grammar 注释）
        self.grammar_quick_self = LlamaGrammar.from_string(
            build_quick_grammar(self.cfg, self_mode=True), verbose=False)
        self.grammar_reply = LlamaGrammar.from_string(build_reply_grammar(self.cfg), verbose=False)
        self.grammar_ok, self.grammar_error = self._grammar_self_check()
        # 第 80 轮：JSON 约束默认走"自己算掩码"（GBNF 每步要给 15 万词表算一次掩码，
        # 实测解码 71.5 → 28.4 token/s）。这里只做**离线**自检（走状态机），
        # 真出问题由 `_note_constrain_result()` 在连续失败后自动切回 GBNF。
        self._init_fast_constrain()
        logger.info("模型加载完成：%s，%ss，n_gpu_layers=%s，语法自检=%s",
                    self.model_path.name, self.load_s, self.gpu_layers_used,
                    "OK" if self.grammar_ok else self.grammar_error)
        return True

    def _init_fast_constrain(self) -> None:
        """准备"自己算掩码"的约束器；任何一步失败都自动退回 GBNF（不影响可用性）。"""
        mode = str(getattr(self.cfg.analyzer, "constrain", "fast") or "fast").lower()
        if mode != "fast" or self.llm is None:
            self.fast_enabled = False
            self.fast_note = (f"配置要求用 {mode}" if mode != "fast"
                              else "模型未就绪")
            logger.info("JSON 约束：使用 GBNF（%s）", self.fast_note)
            return
        try:
            from core import fast_mask as fm
            key = fm.default_cache_key(self.model_path, int(self.llm.n_vocab()))
            t0 = time.perf_counter()
            self.fast = fm.FastConstrainer.from_llm(self.llm, key)
            ok, why = self.fast.self_check()
            if not ok:
                # 第 80 轮（用户口径："不要让纯解码轻易退到 GBNF 解码，GBNF 太慢了"）：
                # 自检只是**一致性自测**，不过也照样用掩码 —— 绝不再因为它悄悄切到慢 3 倍的 GBNF。
                self.fast_enabled = True
                self.fast_note = f"自检不过（{why}）—— 仍用掩码"
                logger.error("JSON 约束：自己算掩码自检没过（%s）—— 仍然使用掩码（不回退 GBNF）",
                             why)
                return
            self.fast_enabled = True
            self.fast_note = "OK"
            logger.info("JSON 约束：自己算掩码已就绪（属性表 %d token，%.2fs；"
                        "预期解码 28 → 60+ token/s）", self.fast.table.n_vocab,
                        time.perf_counter() - t0)
        except Exception as exc:
            self.fast = None
            self.fast_enabled = False
            self.fast_note = f"{type(exc).__name__}: {exc}"
            logger.warning("JSON 约束：自己算掩码初始化失败（%s）→ 退回 GBNF", exc)

    def _note_constrain_result(self, ok: bool) -> None:
        """记一下解析成败（**只记日志**）。

        第 80 轮（用户口径："不要让纯解码轻易退到 GBNF 解码，GBNF 太慢了"）：
        以前"连续两次解析失败就退回 GBNF" —— 实测那条路会把解码从 ~60 token/s 打到 ~20，
        而且失败往往只是某一条消息的偶发，不该让整个会话都变慢。现在**永远不自动切 GBNF**，
        只把次数记进日志/状态，方便排查。"""
        if not getattr(self, "fast_enabled", False):
            return
        if ok:
            self._constrain_fail_streak = 0
            return
        self._constrain_fail_streak = int(getattr(self, "_constrain_fail_streak", 0)) + 1
        logger.warning("JSON 约束：掩码路径第 %d 次解析失败（**不切 GBNF**，继续用掩码）",
                       self._constrain_fail_streak)

    def _gpu_evidence(self) -> Dict[str, Any]:
        evidence: Dict[str, Any] = {"device": None, "offloaded": None, "buffers": []}
        for line in self._log_lines:
            if "using device CUDA" in line:
                evidence["device"] = line.split("using device", 1)[-1].strip()
            if "offloaded" in line and "layers to GPU" in line:
                evidence["offloaded"] = line.strip()
            if "buffer size" in line:
                evidence["buffers"].append(line.strip())
        return evidence

    def _grammar_self_check(self) -> Tuple[bool, Optional[str]]:
        """llama.cpp 惰性解析：必须真跑一次才知道语法是否生效。"""
        if self.llm is None or self.grammar is None:
            return (False, "模型或语法未就绪")
        del self._log_lines[:]
        try:
            for _ in self.llm.create_chat_completion(
                    messages=[{"role": "system", "content": "只输出 JSON。"},
                              {"role": "user", "content": "测试"}],
                    grammar=self.grammar, max_tokens=8, temperature=0.0, stream=True):
                pass
        except Exception as exc:
            return (False, f"{type(exc).__name__}: {exc}")
        errors = [l for l in self._log_lines if "error parsing grammar" in l]
        # 三个语法都要过（两段式用了 quick/reply 两个子语法）
        for extra in (getattr(self, "grammar_quick", None), getattr(self, "grammar_reply", None)):
            if extra is None:
                continue
            del self._log_lines[:]
            try:
                for _ in self.llm.create_chat_completion(
                        messages=[{"role": "system", "content": "只输出 JSON。"},
                                  {"role": "user", "content": "测试"}],
                        grammar=extra, max_tokens=8, temperature=0.0, stream=True):
                    pass
            except Exception as exc:
                return (False, f"{type(exc).__name__}: {exc}")
            errors += [l for l in self._log_lines if "error parsing grammar" in l]
        return (not errors, errors[0] if errors else None)

    def close(self) -> None:
        self.llm = None
        self.grammar = None

    # ---------------- 提示词 ----------------
    def build_messages(self, context: Sequence[Message], target: Message,
                       stage: str = "full") -> List[Dict[str, str]]:
        """拼一次请求的两条消息。`stage`（第 81 轮末）：quick / replies / full ——
        **只把本段要用的字段写进 system prompt**（实测 system 1123 → 928/701 token）。"""
        a = self.cfg.analyzer
        # API 模式用更小的上下文预算（省钱）；本地仍用 600
        if self.api_enabled:
            budget = int(getattr(self.cfg.api, "context_token_budget", a.context_token_budget)
                         or a.context_token_budget)
            limit = int(getattr(self.cfg.api, "context_messages", a.context_messages)
                        or a.context_messages)
            context = list(context)[-limit:]
        else:
            budget = a.context_token_budget
        speaker = {"left": "对方", "right": "我", "history": "历史", "after": "后文"}
        background: List[str] = []
        used_chars = 0
        # 【重要修复】预算不够时**丢最旧的、留最新的**。
        # 原实现是按"从旧到新"的顺序累加、超预算就 break → 被丢掉的恰恰是**紧邻的上文**
        # （最相关的那几条），留下的却是最老的，这正是"建议/选项跟上下文关联不够"的主因。
        # 第 82 轮（用户口径）：【后文】删掉；【背景】条数硬上限 5 条（"太多嚼不烂"）。
        limit = int(getattr(a, "context_messages", 5) or 5)
        if self.api_enabled:
            limit = min(limit, int(getattr(self.cfg.api, "context_messages", limit) or limit))
        ordered = [m for m in context if m.msg_key != target.msg_key]
        before_msgs = [m for m in ordered if m.side != "after"]
        for msg in reversed(before_msgs):          # 从最近的一条往回取
            if msg.msg_key == target.msg_key:
                continue                      # 目标单独处理
            text = msg.text
            if len(text) > a.summary_chars:
                text = text[: a.summary_chars] + "…"
            line = f"{speaker.get(msg.side, '历史')}: {text}"
            # 粗略按"1 token ≈ 1.5 字符"估算
            if used_chars + len(line) > budget * 1.5 or len(background) >= limit:
                break
            used_chars += len(line)
            background.append(line)
        background.reverse()                   # 还原成从旧到新的阅读顺序

        if getattr(a, "context_target_first", False):
            # 实测：目标埋在长上下文最后时，小模型容易把"历史里的某条"当目标（3B 曾把
            # "取消科三"当成"笑了"的意图）→ 改成**目标在最前**，背景另起一段。
            lines = [f"【需要分析的消息】（{speaker.get(target.side, '历史')}）：{target.text}"]
            if background:
                # 紧邻的上文单独标出来：它最可能说明"这一条在接续什么"
                near, earlier = background[-2:], background[:-2]
                lines += ["", "【紧邻的上文】（这一条很可能在接续它，用它判断“接续了什么”）："]
                lines += near
                if earlier:
                    lines += ["", "【背景】更早的对话，只作话题背景，**不要分析这些行**："]
                    lines += earlier
            else:
                lines += ["", "【背景】更早的对话，只用于理解话题，**不要分析这些行**：", "（无）"]
        else:
            lines = list(background)
            lines.append(f"对方: {target.text}   <<< 需要分析的就是这条")
        self_mode = not target.is_other_party      # 第 79 轮：目标是我自己发的 → 换一套字段
        # 固定格式 + 每个字段可自定义的说明（设置里能改字段名与说明，见 config.build_system_prompt）
        compact = self.api_enabled and bool(getattr(self.cfg.api, "compact_prompt", True))
        # 角色包（整体风格）现在由 build_system_prompt 放在**最前面**，这里不再重复贴在结尾
        # （第 81 轮末：以前"字段表在前、风格在后"，模型更听最近的指令，风格会被稀释）。
        system = build_system_prompt(self.cfg, compact=compact, self_mode=self_mode, stage=stage)
        logger.debug("上下文 %d 条，prompt 长度 %d 字符（自我分析=%s，stage=%s）",
                     len(lines), len(system), self_mode, stage)
        # 第 79 轮：自我分析时，把"谁是谁 + 三条要写成谁的话"放在 **user 消息最开头** ——
        # 4B 对"最近/最前"的位置最敏感；放在这里 + 末尾注入各说一次，方向才稳。
        header = ""
        if self_mode:
            header = ("【这是我（「我」）和对方（「对方」）的聊天记录。"
                      "要生成的三条发言选项，是我**接着自己那句话继续发给对方**的字 —— "
                      "像我自己打出去的话（不必每条都带「我」）。】\n")
        return [{"role": "system", "content": system},
                {"role": "user", "content": header + "对话记录:\n" + "\n".join(lines)}]

    @staticmethod
    def is_self_target(target: Message) -> bool:
        """目标是不是"我自己发的那条"（面板/日志用）。"""
        try:
            return not target.is_other_party
        except Exception:
            return False

    def _style_headline(self, target: Message) -> str:
        """当前这条消息该用的"整体风格"里的第一句（整句太长了，只取一句）。

        自我分析用 `self_analysis_style_prompt`，分析对方用 `analysis_style_prompt`
        —— 都是**用户配置里的原文**，这里只是把它搬到最后、离输出最近的位置再念一次。
        第 82 轮末：两套体系**共用同一份角色包**（`analysis_style_prompt`）；
        只有它为空时才退回旧的 `self_analysis_style_prompt`（兼容老配置）。
        """
        other = (getattr(self.cfg, "analysis_style_prompt", "") or "").strip()
        mine = (getattr(self.cfg, "self_analysis_style_prompt", "") or "").strip()
        style = other or mine
        first = re.split(r"[。；\n]", str(style).strip())[0].strip()
        return first[:40]

    def _field_spec(self, target: Message) -> Tuple[Any, Any, Any, bool]:
        """按目标消息选字段表：自己的消息 → self_fields + DEFAULT_SELF_*（第 79 轮）。

        返回 (fields, names, prompts, self_mode)；fields 为 None 表示走用户当前配置。
        """
        self_mode = self.is_self_target(target)
        if not self_mode:
            return None, None, None, False
        return (getattr(self.cfg, "self_fields", None),
                DEFAULT_SELF_FIELD_NAMES, DEFAULT_SELF_FIELD_PROMPTS, True)

    def _all_labels(self, fields: Any = None) -> List[str]:
        """配置里所有字段的显示名（含自我分析那套）——用来剥掉值开头的「字段名：」。"""
        # 第 82 轮：JSON 键名（A1/A2…）也加进去 —— 模型偶尔会把键名抄进值里（"A1：…"）
        labels: List[str] = [JSON_KEYS[k] for k in
                             ("intent", "emotion", "danger", "suggestion", "option")]
        for key in ("intent", "emotion", "danger", "suggestion", "option"):
            for spec_fields, spec_names in (
                    (None, None),                                    # 对方那套（当前配置）
                    (getattr(self.cfg, "self_fields", None), DEFAULT_SELF_FIELD_NAMES),
            ):
                try:
                    name = str(field_name(self.cfg, key, spec_fields, spec_names) or "").strip()
                except Exception:
                    name = ""
                if name and name not in labels:
                    labels.append(name)
        return sorted(labels, key=len, reverse=True)      # 长名优先（避免短名前缀误剥）

    def _clean_field(self, value: str, field: str, labels: Sequence[str]) -> str:
        """文本字段的**只清理、不丢弃**（第 80 轮最终口径）。

        历史（都按用户口径撤掉了，因为副作用大于收益）：
          · 第一版：判为"抄了提示词"就**清空**该字段；
          · 第二版：判为抄写就**就地重问**这个字段（多一次生成、还可能不带上文）。
        现在只做**不会丢内容**的三件事：
          ① 控制字符 → 空格（约束理论上不会出现，防万一）；
          ② 剪掉开头/结尾粘上的「字段名：」或"半个字段名"（40 字上限会把
             '本小姐的判词' 截成 '本小姐的' 粘在句尾）；
          ③ 剪掉尾部半个标点（撞上限留下的 '，'）。
        "别再抄提示词"交给提示词结构（`config.build_system_prompt` 的"键名 + 要求"格式）
        与采样参数（`analyzer.repeat_penalty`），不再在后处理里做取舍。
        """
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"[\x00-\x1f]+", " ", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        if not text:
            return ""
        for label in labels:
            if not label:
                continue
            for sep in ("：", ":", "＝", "="):
                if text.startswith(label + sep):
                    text = text[len(label) + len(sep):].strip()
                    break
            if text.startswith(label):
                text = text[len(label):].lstrip("：:＝= 　").strip()
            for sep in ("：", ":", "＝", "="):
                if text.endswith(label + sep):
                    text = text[: -len(label) - len(sep)].strip()
            for cut in range(len(label) - 1, 3, -1):
                head = label[:cut]
                if text.endswith(head) and text != head:
                    text = text[: -len(head)].rstrip("，。、；：,;: ").strip()
                    break
        return text.rstrip("，,、；;：: ").strip()

    # ---------------- 生成 ----------------
    @staticmethod
    def _parse_sse_chunk(line: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """解析一行 SSE（`data: {...}`）。返回 (增量文本, usage 或 None)。

        纯函数，方便单测（tools/test_api_stream.py）：不联网也能验证解析逻辑。
        `data: [DONE]` / 空行 / 注释行 / 坏 JSON 一律返回 ("", None)。
        """
        import json as _json

        line = (line or "").strip()
        if not line or line.startswith(":"):
            return "", None
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            return "", None
        try:
            payload = _json.loads(line)
        except Exception:
            return "", None
        usage = payload.get("usage") if isinstance(payload, dict) else None
        piece = ""
        try:
            delta = payload["choices"][0].get("delta") or {}
            piece = delta.get("content") or ""
            if not piece:
                # 少数服务只在非流式字段里给内容（或首包直接给 message）
                piece = (payload["choices"][0].get("message") or {}).get("content") or ""
        except Exception:
            piece = ""
        return piece, (usage if isinstance(usage, dict) else None)

    def _api_generate_stream(self, messages: List[Dict[str, str]],
                             max_tokens: Optional[int] = None,
                             cancel_token: Optional[CancelToken] = None,
                             on_delta: Optional[Any] = None) -> Optional[str]:
        """API **流式**调用（第 79 轮）：边收边 on_delta(累计文本) → 面板提前上屏。

        与 `_api_generate` 共用同一套前置检查（冷却/预算/单次上限/json 模式重试），
        区别只是 `stream: true` + 逐行解析 SSE。任何异常：
          · 还没收到内容 → 返回 None（调用方回落本地模型）；
          · 已经收到一部分 → 返回已有内容（宁可显示不完整，也别把已花的 token 扔掉）。
        取消：cancel_token 置位后立刻断开连接并返回已收内容。
        """
        import json as _json
        import urllib.request

        api = self.cfg.api
        if time.time() < self._api_cooldown_until:
            logger.info("API 冷却中（上次结果不可用），本次跳过 API 调用")
            return None
        ok, note = self.api_budget_ok()
        if not ok:
            self.last_error = f"API 预算已用尽（{note}）"
            logger.warning("跳过 API 调用：%s → %s", note,
                           "回落本地模型" if api.on_budget_exceeded == "fallback_local" else "不分析")
            self.budget_blocked = True
            return None
        if not getattr(api, "stream", True):
            return self._api_generate(messages, max_tokens=max_tokens)

        limit = int(max_tokens or api.max_tokens_replies)
        url = api.base_url.rstrip("/") + "/chat/completions"
        started = time.perf_counter()
        for use_json_mode in (True, False):
            payload: Dict[str, Any] = {
                "model": api.model, "messages": messages,
                "temperature": self._temperature(), "max_tokens": limit,
                "stream": True,
            }
            if use_json_mode:
                payload["response_format"] = {"type": "json_object"}
            request = urllib.request.Request(
                url, data=_json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Accept": "text/event-stream",
                         "Authorization": f"Bearer {api.api_key}"})
            for attempt in range(int(api.max_retries) + 1):
                pieces: List[str] = []
                usage: Optional[Dict[str, Any]] = None
                cancelled = False
                try:
                    with urllib.request.urlopen(request, timeout=api.timeout_s) as response:
                        for raw_line in response:
                            if cancel_token is not None and cancel_token.cancelled:
                                cancelled = True
                                break
                            piece, chunk_usage = self._parse_sse_chunk(
                                raw_line.decode("utf-8", "ignore"))
                            if chunk_usage:
                                usage = chunk_usage
                            if not piece:
                                continue
                            pieces.append(piece)
                            if on_delta is not None:
                                try:
                                    on_delta("".join(pieces))
                                except Exception as exc:    # 上屏失败不影响生成
                                    logger.debug("API 流式上屏失败：%s", exc)
                    content = "".join(pieces)
                    if content:
                        if usage:
                            self._add_usage(usage.get("prompt_tokens", 0),
                                            usage.get("completion_tokens", 0))
                        logger.info("API 流式返回 %d 字符（模型 %s，%s）｜本次 max_tokens=%d｜"
                                    "%s｜耗时 %.2fs｜今日累计 %s token / %s 次",
                                    len(content), api.model, "json_mode" if use_json_mode else "纯文本",
                                    limit, "已取消" if cancelled else "正常结束",
                                    time.perf_counter() - started,
                                    self.today_usage().get("total_tokens", 0),
                                    self.today_usage().get("requests", 0))
                        self.last_error = None
                        return content
                    logger.warning("API 流式没收到任何内容（json_mode=%s，第 %d 次）",
                                   use_json_mode, attempt + 1)
                except Exception as exc:
                    logger.warning("API 流式调用失败（json_mode=%s，第 %d 次）：%s: %s",
                                   use_json_mode, attempt + 1, type(exc).__name__, exc)
                    if pieces:      # 已经收到一部分：保留，不再重试/回落
                        logger.warning("API 流式中断但有部分内容（%d 字符），直接采用",
                                       len("".join(pieces)))
                        return "".join(pieces)
                    if attempt < int(api.max_retries):
                        time.sleep(0.5)
        return None

    def _api_generate(self, messages: List[Dict[str, str]],
                      max_tokens: Optional[int] = None) -> Optional[str]:
        """OpenAI 兼容接口调用（只发文本；失败返回 None，由调用方回落本地）。"""
        import json as _json
        import urllib.request

        api = self.cfg.api
        # ③ 冷却：上一次 API 返回不可用（解析失败/报错）后短时间内不再重复请求，
        #    否则"回落本地"会把同一个请求再发一遍（API 模式下本地模型没加载，纯浪费 token）。
        if time.time() < self._api_cooldown_until:
            logger.info("API 冷却中（上次结果不可用），本次跳过 API 调用")
            return None
        # ① 预算检查：超出每日上限就不再发请求（默认回落到本地模型）
        ok, note = self.api_budget_ok()
        if not ok:
            self.last_error = f"API 预算已用尽（{note}）"
            logger.warning("跳过 API 调用：%s → %s", note,
                           "回落本地模型" if api.on_budget_exceeded == "fallback_local" else "不分析")
            self.budget_blocked = True
            return None
        # ② 单次输出上限：按调用方给的上限（第一段 96 / 第二段 220），没给才用默认
        limit = int(max_tokens or api.max_tokens_replies)
        url = api.base_url.rstrip("/") + "/chat/completions"
        base_payload = {"model": api.model, "messages": messages,
                        "temperature": self._temperature(),
                        "max_tokens": limit}
        for use_json_mode in (True, False):     # 有些服务不支持 response_format，失败就去掉
            payload = dict(base_payload)
            if use_json_mode:
                payload["response_format"] = {"type": "json_object"}
            data = _json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api.api_key}"})
            for attempt in range(int(api.max_retries) + 1):
                try:
                    with urllib.request.urlopen(request, timeout=api.timeout_s) as response:
                        body = _json.loads(response.read().decode("utf-8"))
                    content = body["choices"][0]["message"]["content"]
                    finish = (body["choices"][0].get("finish_reason") or "").strip()
                    if finish == "length":
                        logger.warning("⚠ API 输出被 max_tokens=%d 截断（finish_reason=length）→ "
                                       "JSON 会不完整、面板会显示“—”。把 api.max_tokens_replies 调大即可",
                                       limit)
                    usage = body.get("usage") or {}
                    self._add_usage(usage.get("prompt_tokens", 0),
                                    usage.get("completion_tokens", 0))
                    today = self.today_usage()
                    logger.info("API 返回 %d 字符（模型 %s）｜本次 max_tokens=%d｜"
                                "finish=%s｜本次消耗 prompt %s + output %s｜今日累计 %s token / %s 次",
                                len(content), api.model, limit, finish or "stop",
                                usage.get("prompt_tokens"), usage.get("completion_tokens"),
                                today.get("total_tokens", 0), today.get("requests", 0))
                    logger.debug("API 原始内容：%s", content[:400])
                    return content
                except Exception as exc:
                    logger.warning("API 调用失败（json_mode=%s，第 %d 次）：%s: %s",
                                   use_json_mode, attempt + 1, type(exc).__name__, exc)
                    if attempt < int(api.max_retries):
                        time.sleep(0.5)
        return None

    def _generate(self, messages: List[Dict[str, str]], temperature: float,
                  seed: int, grammar: Any = None,
                  max_tokens: Optional[int] = None,
                  cancel_token: Optional[CancelToken] = None,
                  on_delta: Optional[Any] = None,
                  program: Optional[str] = None,
                  no_constraint: bool = False) -> Tuple[str, Optional[float], float]:
        """on_delta(累计文本)：每产出一个 token 调一次，用于"流式提前上屏"。"""
        a = self.cfg.analyzer
        # API 模式：两段式也走 API（此前只有"单次生成"兜底才用 API，导致 per-stage 上限形同虚设）。
        # 失败（含预算用尽）→ 继续往下走本地模型；本地没加载就返回空串。
        if self.api_enabled:
            started = time.perf_counter()
            # 第 79 轮：有 on_delta 就走**流式**（面板提前上屏，等待感大幅降低）；
            # 没有回调（例如内部自检）仍走一次性调用。
            if on_delta is not None:
                raw = self._api_generate_stream(messages, max_tokens=max_tokens,
                                                cancel_token=cancel_token,
                                                on_delta=on_delta)
            else:
                raw = self._api_generate(messages, max_tokens=max_tokens)
            if raw is not None:
                return raw, None, (time.perf_counter() - started) * 1000
            if self.llm is None:
                logger.warning("API 不可用且本地模型未加载 → 本次无结果")
                return "", None, (time.perf_counter() - started) * 1000
        prompt_tokens = 0
        try:
            joined = "".join(m.get("content", "") for m in messages)
            prompt_tokens = len(self.llm.tokenize(joined.encode("utf-8"), add_bos=False))
        except Exception:
            pass
        self.last_prompt_tokens = prompt_tokens
        t0 = time.perf_counter()
        first: Optional[float] = None
        pieces: List[str] = []
        # 看门狗基线（第 75 轮，见下）：距上一次 token / 总时长 / 进度日志
        stall_s = float(getattr(a, "gen_stall_timeout_s", 20.0) or 20.0)
        total_s = float(getattr(a, "gen_total_timeout_s", 150.0) or 150.0)
        last_chunk_ts = t0
        last_progress_ts = t0
        # 取消机制说明（实测教训）：**不能在 logits 回调里抛异常** —— 那个回调是 ctypes 从 C 层
        # 调进来的，ctypes 会吞掉异常（traceback 打印了，但生成照跑 3 秒）。
        # 正确做法：在我们自己的流式循环里检查标志并退出 → 生成器被关闭（GeneratorExit）
        # → llama.cpp 真正停下，通常在 1 个 token（~30ms）内生效。
        # 第 80 轮：约束怎么给 ——
        #   ① 默认走"自己算掩码"（同一套语言，解码快 ~2.5 倍）；
        #   ② **只在用户显式把 `analyzer.constrain` 设成 "gbnf" 时**才用 GBNF；
        #   ③ 掩码不可用（构建异常）时走"**无约束** + 提示词 + 解析兜底"，
        #      **绝不自动退到慢 3 倍的 GBNF**（用户口径："不要让纯解码轻易退到 GBNF 解码"）。
        mode = str(getattr(self.cfg.analyzer, "constrain", "fast") or "fast").lower()
        use_fast = (not no_constraint and mode != "gbnf"
                    and bool(getattr(self, "fast_enabled", False))
                    and self.fast is not None and bool(program))
        processors = None
        if use_fast:
            # program 可以是程序名（常规两段），也可以是 Program 对象（重问单字段）
            if isinstance(program, str):
                processors = [self.fast.processor(program)]
            else:
                processors = [self.fast.processor_for_program(program)]
        want_gbnf = (not no_constraint) and not use_fast and mode == "gbnf"
        try:
            for chunk in self.llm.create_chat_completion(
                    messages=messages,
                    grammar=(grammar or self.grammar) if want_gbnf else None,
                    logits_processor=processors,
                    max_tokens=max_tokens or a.max_tokens,
                    temperature=temperature, seed=seed, stream=True,
                    # 第 75 轮：采样参数可配（之前只传 temperature，其余用 llama.cpp 默认）。
                    # 换 Qwen3.5 后"发挥不出来"的一大原因就是**温度/采样与官方非思考配方不符**：
                    # Qwen3.5 非思考推荐 temp 0.7 / top_p 0.8 / top_k 20，而我们固定 0.2。
                    top_k=int(getattr(a, "top_k", 40)),
                    top_p=float(getattr(a, "top_p", 0.95)),
                    min_p=float(getattr(a, "min_p", 0.05)),
                    presence_penalty=float(getattr(a, "presence_penalty", 0.0)),
                    repeat_penalty=float(getattr(a, "repeat_penalty", 1.0))):
                # 第 75 轮（用户反馈"卡在分析中得不到结果"）：加看门狗。
                #   · 有新 token 时检查"距上一次 token 多久 / 总共跑了多久"；
                #   · 每 5s 写一条进度日志（这样日志里能看出"卡住"还是"在慢慢跑"）；
                #   · 超限就用与"目标切换"相同的 GeneratorExit 路径中止（llama.cpp 立刻停）。
                now_ts = time.perf_counter()
                if now_ts - last_chunk_ts > stall_s:
                    raise GenerationCancelled(f"生成卡住（{stall_s:.0f}s 没有新 token）")
                if now_ts - t0 > total_s:
                    raise GenerationCancelled(f"生成超时（>{total_s:.0f}s）")
                last_chunk_ts = now_ts
                if now_ts - last_progress_ts > 5.0:
                    last_progress_ts = now_ts
                    logger.info("生成进行中：已 %.0fs / %d 字符（首token %s）", now_ts - t0,
                                sum(len(p) for p in pieces),
                                "-" if first is None else f"{first * 1000:.0f}ms")
                if cancel_token is not None and cancel_token.cancelled:
                    raise GenerationCancelled("目标已切换")
                if first is None:
                    first = time.perf_counter() - t0
                pieces.append((chunk["choices"][0].get("delta", {}) or {}).get("content") or "")
                if on_delta is not None:
                    on_delta("".join(pieces))
        except GenerationCancelled:
            logger.info("生成被取消（目标已切换）：已产出 %d 字符，用时 %.0fms",
                        len("".join(pieces)), (time.perf_counter() - t0) * 1000)
            raise
        raw = "".join(pieces)
        total_ms = (time.perf_counter() - t0) * 1000
        try:
            self.last_completion_tokens = len(self.llm.tokenize(raw.encode("utf-8"), add_bos=False))
        except Exception:
            self.last_completion_tokens = None
        logger.info("生成完成：prompt %d token / 输出 %s token / 首token %s ms / 总 %d ms",
                    prompt_tokens, self.last_completion_tokens,
                    f"{first * 1000:.0f}" if first is not None else "-", total_ms)
        return raw, (None if first is None else first * 1000), total_ms

    # ---------------- 解析与校验 ----------------
    def _truncate(self, name: str, value: str, notes: List[str]) -> str:
        limit = self.cfg.field_limits.get(name)
        if limit and len(value) > limit:
            notes.append(f"{name} 超长 {len(value)}>{limit}，已截断")
            return value[:limit]
        return value

    def _pick(self, payload: Dict[str, Any], key: str, default: Any = None) -> Any:
        """从 JSON 里取字段：先英文键，再**当前显示名**（用户可能改名，如"兴趣水平"），
        最后几个常见别名。

        为什么需要：API 路径没有 GBNF 语法兜底，实测 DeepSeek 会直接拿显示名当键
        （{"意图":…} / {"兴趣水平":…}）→ 只认英文键会让整条结果变空、面板显示"—"。

        第 82 轮：JSON 键名换成抽象名（A1/A2/…）后，这里**先认新键名**，再兜底认旧键名
        （intent/danger_level…）与显示名/中文别名 —— 三种写法都能取到值。
        """
        slot = JSON_KEY_BY_LEGACY.get(key, key)
        candidates = [JSON_KEYS.get(slot, key)]
        if key != candidates[0]:
            candidates.append(key)          # 旧键名（intent / danger_level / reply_options…）
        try:
            candidates.append(field_name(self.cfg, {"danger_level": "danger",
                                                   "reply_options": "option"}.get(key, key)))
        except Exception:
            pass
        candidates += {"intent": ["意图", "意思", "主题"], "emotion": ["情绪", "语气"],
                       "danger_level": ["危险度", "危险等级", "危险水平", "风险"],
                       "suggestion": ["回复建议", "建议", "分析建议"],
                       "reply_options": ["回复选项", "选项", "回复"],
                       "confidence": ["置信", "置信度", "把握"],
                       }.get(key, [])
        for candidate in candidates:
            if candidate in (payload or {}):
                return payload[candidate]
        return default

    def parse(self, raw: str, msg_key: str) -> AnalysisResult:
        result = AnalysisResult(msg_key=msg_key, raw=raw)
        try:
            payload = json.loads(raw)
        except Exception as exc:
            result.notes.append(f"JSON 解析失败: {type(exc).__name__}: {exc}")
            return result
        if not isinstance(payload, dict):
            result.notes.append("顶层不是 JSON 对象")
            return result

        notes = result.notes
        result.json_ok = True
        result.intent = self._truncate("intent", str(self._pick(payload, "intent", "")).strip(), notes)
        result.emotion = self._truncate("emotion", str(self._pick(payload, "emotion", "")).strip(), notes)
        result.suggestion = self._truncate("suggestion",
                                           str(self._pick(payload, "suggestion", "")).strip(), notes)
        try:
            danger = int(float(str(self._pick(payload, "danger_level", 0)).strip()))
            if not 0 <= danger <= 10:
                notes.append(f"danger_level 越界 {danger}，已夹到 0-10")
            result.danger_level = max(0, min(10, danger))
        except Exception:
            notes.append("danger_level 不是数字")
        try:
            conf = float(str(self._pick(payload, "confidence", 0.5)).strip())
            if not 0.0 <= conf <= 1.0:
                notes.append(f"confidence 越界 {conf}，已夹到 0-1")
            result.confidence = max(0.0, min(1.0, conf))
        except Exception:
            notes.append("confidence 不是数字")

        options = self._pick(payload, "reply_options") or []
        if not isinstance(options, list):
            notes.append("reply_options 不是数组")
            options = []
        seen_styles, seen_texts = set(), set()
        for item in options:
            if not isinstance(item, dict):
                continue
            style = self._truncate("reply_style", str(item.get("style", "")).strip(), notes)
            text = self._truncate("reply_text", str(item.get("text", "")).strip(), notes)
            if not text:
                notes.append("丢弃空回复选项")
                continue
            if text in seen_texts:
                notes.append("丢弃重复回复文本")
                continue
            seen_texts.add(text)
            if style in seen_styles:
                notes.append(f"风格标签重复（{style}）")
            seen_styles.add(style)
            result.replies.append(ReplyOption(style=style or "回复", text=text))

        need = REPLY_OPTION_COUNT
        if len(result.replies) < need:
            notes.append(f"回复选项不足（{len(result.replies)}/{need}）")
            result.degraded = True
        return result

    # ---------------- 对外分析入口 ----------------
    def _apply_danger_rule(self, result: AnalysisResult, target: Message) -> None:
        """danger 兜底规则：句子里没提到"你/您"就不可能是"指向你的追责" → 最高 4。"""
        if self.is_self_target(target):
            return                      # 第 79 轮：自己的消息里这个字段是"质量评分"，越高越好
        if not getattr(self.cfg.fields, "danger_cap_rule", True):
            return                      # 用户把该字段改成别的含义（如"兴趣水平"）时应关掉
        cap = self.cfg.analyzer.danger_cap_without_second_person
        if result.danger_level is None or result.danger_level <= cap:
            return
        text = target.text or ""
        if ("你" in text) or ("您" in text):
            return
        result.notes.append(f"danger {result.danger_level}→{cap}（这条消息没有提到“你/您”，"
                            f"不构成指向你的追责）")
        result.danger_level = cap

    def analyze_quick(self, target: Message, context: Sequence[Message],
                      use_cache: bool = True,
                      cancel_token: Optional[CancelToken] = None,
                      on_partial: Optional[Any] = None) -> Optional[AnalysisResult]:
        """两段式第一段：只生成 intent/emotion/danger/confidence（约 1 秒）。

        on_partial(AnalysisResult)：**流式提前上屏**。JSON 字段顺序是固定的
        （intent → emotion → danger_level → suggestion → confidence），所以
        情绪+意图一出（约 0.4-0.6s，整段要 ~2s）就先回调一次，危险度出来再回调一次。
        实测：第一段 2.1s 里，标题在 ~0.5s 就能显示，体感快 4 倍。
        注意：partial 结果**不进缓存**（只走回调），缓存的永远是完整结果。
        """
        if self.llm is None:
            return None
        if use_cache:
            cached = self.get_cached(target.msg_key)
            if cached is not None:
                return cached
        messages = self.build_messages(context, target, stage="quick")
        base_user = messages[1]["content"]          # 注入前的 user 正文（第 80 轮防抄用）
        self_mode = self.is_self_target(target)     # 第 79 轮：自己的消息 → 另一套字段
        labels = self._all_labels()
        # 第 80 轮（用户口径："提示词不要搞得太过冗余"）：**不再注入"写作角度"那段**。
        # 实测（tmp/probe_self_suggestion.py / probe_echo_labels.py）：那段注入语里的短词
        # （"直接说""直接答应"）本身就常被 3.5 照抄成正文；而"建议别老一个味"这个目的
        # 改由**随机 seed** 实现（见下面的 seed 计算），提示词因此更短更干净。
        if getattr(self.cfg.analyzer, "vary_suggestion", True):
            fields_s, _names_s, prompts_s, _self_s = self._field_spec(target)
            sug_label = field_name(self.cfg, "suggestion", fields_s, _names_s)
            # 与回复选项同理：字段说明要求"喵"时，在离输出最近处再强调一次
            if "喵" in field_prompt(self.cfg, "suggestion", fields_s, prompts_s):
                messages[1]["content"] += f"\n（{sug_label}要带“喵”）"
        if self_mode:
            # 第 79 轮（实测规律：离输出最近的那句最管用 —— 同"喵"注入）：
            # 4B 会随机把第三个字段当成危险度写 0，这里在贴近输出处把基准钉死。
            fields_s, _names_s, prompts_s, _self_s = self._field_spec(target)
            danger_label = field_name(self.cfg, "danger", fields_s, _names_s)
            line = (f"\n（第三个字段 {danger_label} 只填 3-10："
                    "没内容 3-4；一般 5；有细节/情绪 6-7；有钩子 8-10）")
            messages[1]["content"] += line
        # 第 80 轮：这条收口提醒已并入系统提示的字段列表开头（同为"说明是要求、不是内容"），
        # 不再在 user 里重复 —— 用户口径："提示词不要搞得太过冗余，反而让小模型难以理解"。
        #
        # 第 80 轮续（用户实测："自我分析发言选项受提示词风格化影响似乎有点弱"）：
        # 实测 18 条自我选项里只有 2 条带配置要求的傲娇标记 —— 因为**离输出最近的那段**
        # 是代码里的通用方向要求（扮演我/立场/句式别雷同），里面一个字都没提风格，
        # 模型就跟着最近指令走。修法：把**配置自己的风格句**（整体风格字段说明的第一句）
        # 在最后再放一次 —— 用的是用户自己的文字，不新增发明，也只有二十来个字。
        style_head = self._style_headline(target)
        if style_head:
            messages[1]["content"] += f"\n（风格（务必遵守）：{style_head}）"
        stream_state = {"intent": "", "emotion": "", "danger": None, "emitted": 0,
                        "prev": (None, None, None)}

        def _on_delta(text: str) -> None:
            if on_partial is None or stream_state["emitted"] >= 3:
                return
            match = _RE_INTENT_FIELD.search(text)
            if match:
                stream_state["intent"] = match.group(1).strip()
            match = _RE_EMOTION_FIELD.search(text)
            if match:
                stream_state["emotion"] = match.group(1).strip()
            match = _RE_DANGER_FIELD.search(text)
            danger = int(match.group(1)) if match else None
            # 字段每"新闭合一个"就上屏一次：intent → +emotion → +danger（最多 3 次）。
            # 先只给 intent 是刻意的：它比 emotion 早闭合几百毫秒，标题能更早上屏，
            # 缺的部分显示成 "—"，随后一次 partial 补齐。
            current = (stream_state["intent"] or None, stream_state["emotion"] or None, danger)
            if current == stream_state["prev"]:
                return
            stream_state["prev"] = current
            stream_state["danger"] = danger
            stream_state["emitted"] += 1
            partial = AnalysisResult(msg_key=target.msg_key, json_ok=True,
                                     intent=stream_state["intent"],
                                     emotion=stream_state["emotion"],
                                     danger_level=danger, partial=True)
            partial.self_analysis = self_mode
            if danger is not None and not self_mode:
                self._apply_danger_rule(partial, target)
            try:
                on_partial(partial)
            except Exception as exc:        # 上屏失败绝不能影响生成
                logger.debug("partial 上屏失败：%s", exc)

        # 第 80 轮：原来的"写作角度"注入（打散"赶紧…"这类机械开头）改成**随机 seed**：
        # 提示词更短（用户口径："不要太冗余"），也不会再被抓去当正文照抄。
        seed = self.cfg.analyzer.seed + self.extra_seed
        if getattr(self.cfg.analyzer, "vary_suggestion", True):
            seed += random.randint(0, 999_999)
        raw, first_ms, total_ms = self._generate(messages, self._temperature(),
                                                 seed,
                                                grammar=(getattr(self, "grammar_quick_self", None)
                                                         if self_mode else self.grammar_quick),
                                                 max_tokens=self._stage_max_tokens("quick"),
                                                 cancel_token=cancel_token,
                                                 program=(fm.PROGRAM_QUICK_SELF if self_mode
                                                          else fm.PROGRAM_QUICK),
                                                 on_delta=_on_delta)
        result = self.parse_quick(raw, target.msg_key)
        self._note_constrain_result(result.json_ok)
        # 第 80 轮：把"抄了字段名/字段说明/写作角度"的值洗掉（AI 生成的配置尤其容易中招）
        # 第 80 轮最终口径：**只做清理，不丢弃**（清空/重问都已按用户要求撤掉）
        for key in ("intent", "emotion", "suggestion"):
            value = getattr(result, key)
            if not value:
                continue
            cleaned = self._clean_field(value, key, labels)
            if cleaned != value:
                logger.info("显示清理：%s 剪掉字段名/尾部标点（%d→%d 字）",
                            key, len(value), len(cleaned))
            setattr(result, key, cleaned)
        result.first_token_ms = round(first_ms, 1) if first_ms is not None else None
        result.total_ms = round(total_ms, 1)
        result.self_analysis = self_mode
        if not result.json_ok:
            logger.warning("第一段 JSON 不合法，回退单次模式：%s", "；".join(result.notes[-2:]))
            return None
        if not self_mode:
            self._apply_danger_rule(result, target)
        # 第 63 轮：intent 也是从聊天内容推出来的，`log_message_text=False` 时一并脱敏
        if getattr(self.cfg, "log_message_text", True):
            logger.info("第一段完成%s：intent=%s emotion=%s danger=%s %.0fms",
                        "（自我分析）" if self_mode else "",
                        result.intent, result.emotion, result.danger_level, total_ms)
        else:
            logger.info("第一段完成%s：intent=<%d 字，已脱敏> emotion=<%d 字> danger=%s %.0fms",
                        "（自我分析）" if self_mode else "",
                        len(result.intent or ""), len(result.emotion or ""),
                        result.danger_level, total_ms)
        return result

    def analyze_replies(self, target: Message, context: Sequence[Message],
                        quick: Optional[AnalysisResult] = None,
                        cancel_token: Optional[CancelToken] = None,
                        on_partial: Optional[Any] = None) -> Optional[AnalysisResult]:
        """两段式第二段：在已知意图的前提下生成 suggestion + 三条回复。"""
        if self.llm is None:
            return None
        messages = self.build_messages(context, target, stage="replies")
        base_user = messages[1]["content"]          # 注入前的 user 正文（第 80 轮防抄用）
        fields_s, names_s, prompts_s, self_mode = self._field_spec(target)
        labels = self._all_labels()
        if quick is not None and quick.intent:
            messages[1]["content"] += (
                f"\n（已经判断：{field_name(self.cfg, 'intent', fields_s, names_s)}="
                f"{quick.intent}，{field_name(self.cfg, 'emotion', fields_s, names_s)}="
                f"{quick.emotion}，"
                f"{field_name(self.cfg, 'danger', fields_s, names_s)}={quick.danger_level}）")
        # 第 59 轮（用户要求"三条立场要有反差"）：不再让模型自己挑方向，改成**逐条指定立场**
        # （认同／不认同／中立），顺序固定（第 1 条认同、第 2 条不认同、第 3 条中立）——
        # 实测：顺序打乱时 4B 会串位（出现三条同一立场），固定顺序后基本按分配写。
        # 说法从三档池里各抽一个再打乱，保证"立场拉开 + 说法不撞"同时成立。
        # （第 58 轮实测：纯给"方向菜单"时小模型会连出三条同一立场的短句。）
        # 手动刷新那条路径也走同一套立场分配，否则刷新后三条立场又会撞在一起
        # （每条各自随机取"说法"，所以刷新后措辞也不会撞）。
        styles = [random.choice(_REPLY_ANGLES), random.choice(_REPLY_ANGLES_PLAIN),
                  random.choice(_REPLY_ANGLES_OFFBEAT)]
        random.shuffle(styles)
        # 开头类型：每批随机抽三种不重复的（结构性提示，不含任何示例词）——
        # 这是**跨消息**去套路的关键：注入内容每次都不同，第一句话的分布才跟着变。
        openings = list(random.sample(_OPENING_TYPES, 3))
        # style 字段在 JSON 里排在 text 之前 → 让模型先写「立场·说法」标签，
        # 相当于先认下立场再写正文，实测比只在正文里要求立场更守规矩；
        # 顺带用户在面板上一眼就能看出哪条是反调（标签 ≤6 字，GBNF 上限）。
        # 立场含义与标签格式写在字段说明里（SYSTEM_PROMPT 里已有），这里只给三档"说法"，
        # 避免每次请求重复一大段说明 —— 本地/API 每次生成都要重发这一段（第 60 轮省 token）。
        if self_mode:
            # 第 79 轮（用户口径）：发言选项 = **我这条消息之后要发的下一句**，
            # 不是把目标消息换个说法重写；三条立场固定 = 继续展开 / 吐槽 / 终结话题。
            messages[1]["content"] += (
                f"\n（本次分配：三条都是**我下一轮要发出去的话**，立场固定 ——"
                f"① **继续展开当前话题**(说法:{styles[0]}；开头:{openings[0]}) "
                f"② **吐槽当前话题**(说法:{styles[1]}；开头:{openings[1]}) "
                f"③ **终结当前话题**(说法:{styles[2]}；开头:{openings[2]})；"

                f"**不要把上面那条目标消息重写一遍**；三条说法互不相同、正文别重复标签、"
                f"**三条的正文必须各不相同**（别用同一句话换两个立场），"
                f"**三条的开头也不许一样**（别都用同一批起手词/语气词开头，句长和语气也要错开），"
                f"别编细节；下次换一组）")
        else:
            messages[1]["content"] += (
                f"\n（本次分配：① 认同(说法:{styles[0]}；开头:{openings[0]}) "
                f"② 反调(说法:{styles[1]}；开头:{openings[1]}) "
                f"③ 吐槽(说法:{styles[2]}；开头:{openings[2]})；"
                f"三条说法互不相同、像随口说的，正文别重复标签，"
                f"**三条的正文必须各不相同**（别用同一句话换两个立场），"
                f"**三条的开头也不许一样**（别都用同一批起手词/语气词开头，句长和语气也要错开），"
                f"别编细节；下次换一组）")
        # 第 82 轮末（用户指出：套路化是**跨消息**用同一批起手词）：
        # 把最近几次用过的**起手词片段**（只存 2 个字，不存整句 —— 整句会被 4B 抄回来）贴出来
        # 要求换掉。实测这是唯一能明显压低"跨消息开头重复"的手段（升温/随机开头类型都没用）。
        if getattr(self.cfg.analyzer, "vary_options", True) and self.recent_openings:
            recent = "、".join(dict.fromkeys(self.recent_openings[-6:]))
            messages[1]["content"] += (
                f"\n（最近几次已经用过这些起手词：{recent}；这批**换个别的起手方式**，"
                f"别再以这些词开头）")
        if self_mode:
            # 第 79 轮（用户反馈"选项像是在回复我自己说的话"）：把"下一轮要发的话"
            # 这层意思放到离输出最近的位置，并明确禁止承接/复述目标消息。
            # 第 80 轮（用户实测："发言选项老是变成同一套『周六下午两点别迟到』
            # 『票价80肉疼』『改天吧』"）：那三句正是这里原来的**方向示例**原文 ——
            # 模型（3.5）会直接照抄示例，于是选项既重复又不是配置里的风格。
            # 现在**删掉示例句**，只留"扮演我 / 接着自己这句说 / 三条立场 / 句式别雷同"，
            # 风格完全交给配置自己的字段说明（同时提示词也更短，符合"别太冗余"）。
            messages[1]["content"] += (
                "\n（**你要扮演「我」**：三条读起来必须像**我发给对方的字** —— "
                "句中「我」＝我自己、「你」＝对方；三条是我**接着自己这条继续往下说**的话，"
                "可以是我做的事/计划/感受，也可以是**我对这件事的直接评价**；"
                "**不必每条都出现「我」**，但三条的**开头与句式不要一样**。"
                "吐槽要吐槽**这件事/这个安排**，不是吐槽对方说的话、更不是吐槽我自己那条；"
                "也不要把目标消息换个说法重写一遍。\n"

                "立场分别是 继续展开 / 吐槽 / 终结话题）")
        # 第 75 轮（用户反馈"猫娘配置的回复选项有的不带喵"）：
        # 根因是**离输出最近的这段注入里没提"喵"**，模型跟着最近指令走就漏了；
        # 字段说明（可能被用户改过）里要求"喵"时，这里再强调一次。
        # 判断依据是字段说明本身含"喵"，所以猫娘配置和任何自建"喵"风格都会自动生效。
        if "喵" in field_prompt(self.cfg, "option", fields_s, prompts_s):
            messages[1]["content"] += "\n（每条回复都要带“喵”）"
        # 用户画像（第 69 轮）：系统提示里已经写了完整画像，但 4B 对"中段的长说明"跟随很弱
        # （实测填了"每句话以'捏'结尾"也照旧不遵守）。这里在**最靠近输出**的位置再提醒一次，
        # 只带前 80 字摘要，避免每次请求都重发一大段。
        # 第 70 轮（用户口径）：**只在第二段注入** —— 第一段（回复建议）不再注入，
        # 注入开销减半；"回复建议"因此不再模仿口吻，只有三条回复选项模仿。
        profile = (getattr(getattr(self.cfg, "fields", None), "user_profile", "") or "").strip()
        if profile:
            messages[1]["content"] += (
                f"\n（我的说话习惯（务必模仿）：{profile[:80]}）")
        # 第 80 轮续：**配置自己的风格句**放到最靠近输出的位置（同第一段的理由）
        style_head = self._style_headline(target)
        if style_head:
            messages[1]["content"] += f"\n（风格（务必遵守）：{style_head}）"
        # 第 80 轮（用户实测"点刷新后生成的内容和上次完全一样"）：**不再把上一版三条原文
        # 喂回提示词**。4B 见到那三行会直接照抄（见 tmp/probe_refresh2.py 的 C 情景：
        # 与上一版 3/3 重合，甚至把注入片段抄成半截 JSON）。刷新改用"新种子 + 高温"，
        # 同探针 D1/D2 情景：两轮刷新与上一版 **0/3** 重合。

        def _on_delta(text: str) -> None:
            """选项也流式上屏：每闭合一条回复就上报一次（带上第一段已有内容）。"""
            if on_partial is None:
                return
            found = _RE_OPTION_ITEM.findall(text)
            if len(found) == stream_state["count"] or not found:
                return
            stream_state["count"] = len(found)
            partial = AnalysisResult(
                msg_key=target.msg_key, json_ok=True, partial=True,
                intent=(quick.intent if quick else ""),
                emotion=(quick.emotion if quick else ""),
                danger_level=(quick.danger_level if quick else None),
                suggestion=(quick.suggestion if quick else ""),
                confidence=(quick.confidence if quick else None),
                replies=[ReplyOption(style=style.strip() or "回复", text=text_item.strip())
                         for style, text_item in found])
            partial.self_analysis = self_mode
            try:
                on_partial(partial)
            except Exception as exc:
                logger.debug("选项 partial 上屏失败：%s", exc)

        stream_state = {"count": 0}
        # 第 81 轮（实验实测）：第二段的 seed 原来是写死的（`seed + 1 + extra_seed`，而 extra_seed
        # 只在点"刷新"时才变）→ 同一条消息重算，**开头 3/3 完全一样**（"一起去看展吧…"、
        # "怎么总是这样啊，人生啊…"）。现在按 `vary_options` 每次加一个随机抖动：
        # 实测开头前 3 字重复 8→5 次/27 条、整条逐字重复 1→0 条，内容质量无变化。
        # （点"刷新"的路径仍然靠 extra_seed + 高温，两者叠加。）
        reply_seed = self.cfg.analyzer.seed + 1 + self.extra_seed
        if getattr(self.cfg.analyzer, "vary_options", True):
            reply_seed += random.randint(0, 999_999)
        raw, first_ms, total_ms = self._generate(messages, self._temperature(),
                                                 reply_seed,
                                                 grammar=self.grammar_reply,
                                                 program=fm.PROGRAM_REPLY,
                                                 max_tokens=self._stage_max_tokens("replies"),
                                                 cancel_token=cancel_token,
                                                 on_delta=_on_delta)
        result = self.parse_replies(raw, target.msg_key)
        self._note_constrain_result(result.json_ok)
        # 第 80 轮：三条选项正文同样防抄（抄了注入语/字段说明/方向示例 → 该条清空）
        # 第 80 轮最终口径：**只做清理，不丢弃**（同第一段）
        if result.suggestion:
            result.suggestion = self._clean_field(result.suggestion, "suggestion", labels)
        for reply in result.replies:
            reply.style = self._clean_field(reply.style, "style", labels)
            reply.text = self._clean_field(reply.text, "option", labels)
        result.first_token_ms = round(first_ms, 1) if first_ms is not None else None
        result.total_ms = round(total_ms, 1)
        result.self_analysis = self_mode
        # 第 82 轮末：记下这批用过的起手词（前 2 字），供下一条消息避重
        try:
            if getattr(self.cfg.analyzer, "vary_options", True):
                for reply in result.replies:
                    head = (reply.text or "").strip()[:2]
                    if len(head) == 2 and head not in self.recent_openings:
                        self.recent_openings.append(head)
                del self.recent_openings[:-9]          # 只留最近 9 个
        except Exception as exc:
            logger.debug("记录起手词失败：%s", exc)
        logger.info("第二段完成：建议 %d 字，选项 %d 条，%.0fms",
                    len(result.suggestion), len(result.replies), total_ms)
        return result

    def parse_replies(self, raw: str, msg_key: str) -> AnalysisResult:
        """解析第二段输出（只有 reply_options）。"""
        result = AnalysisResult(msg_key=msg_key, raw=raw)
        try:
            payload = json.loads(raw)
        except Exception as exc:
            result.notes.append(f"JSON 解析失败: {type(exc).__name__}: {exc}")
            return result
        result.json_ok = True
        seen_texts = set()
        for item in (self._pick(payload, "reply_options") or []):
            if not isinstance(item, dict):
                continue
            style = self._truncate("reply_style", str(item.get("style", "")).strip(),
                                   result.notes)
            text = self._truncate("reply_text", str(item.get("text", "")).strip(),
                                  result.notes)
            if not text or text in seen_texts:
                result.notes.append("丢弃空/重复回复")
                continue
            seen_texts.add(text)
            result.replies.append(ReplyOption(style=style or "回复", text=text))
        if len(result.replies) < REPLY_OPTION_COUNT:
            result.notes.append(f"回复选项不足（{len(result.replies)}/{REPLY_OPTION_COUNT}）")
            result.degraded = True
        return result

    def parse_quick(self, raw: str, msg_key: str) -> AnalysisResult:
        """解析第一段输出（只有 intent/emotion/danger_level/confidence）。"""
        result = AnalysisResult(msg_key=msg_key, raw=raw, partial=True, backend="local")
        try:
            payload = json.loads(raw)
        except Exception as exc:
            result.notes.append(f"JSON 解析失败: {type(exc).__name__}: {exc}")
            return result
        if not isinstance(payload, dict):
            result.notes.append("顶层不是 JSON 对象")
            return result
        result.json_ok = True
        result.intent = self._truncate("intent", str(self._pick(payload, "intent", "")).strip(),
                                       result.notes)
        result.emotion = self._truncate("emotion", str(self._pick(payload, "emotion", "")).strip(),
                                        result.notes)
        result.suggestion = self._truncate("suggestion",
                                           str(self._pick(payload, "suggestion", "")).strip(),
                                           result.notes)
        try:
            danger = int(float(str(self._pick(payload, "danger_level", 0)).strip()))
            result.danger_level = max(0, min(10, danger))
        except Exception:
            result.notes.append("danger_level 不是数字")
        try:
            conf = float(str(self._pick(payload, "confidence", 0.5)).strip())
            result.confidence = max(0.0, min(1.0, conf))
        except Exception:
            result.notes.append("confidence 不是数字")
        return result

    def analyze(self, target: Message, context: Sequence[Message],
                use_cache: bool = True,
                on_partial: Optional[Any] = None,
                cancel_token: Optional[CancelToken] = None) -> Optional[AnalysisResult]:
        """分析一条消息；命中缓存直接返回（cache_hit=True）。异常返回 None。"""
        if use_cache:
            cached = self.get_cached(target.msg_key)
            if cached is not None:
                return cached

        # 两段式（默认）：先出意图/情绪/危险度，再补建议与三条回复
        if self.cfg.analyzer.two_stage and self.llm is not None:
            quick = self.analyze_quick(target, context, use_cache=False,
                                       cancel_token=cancel_token, on_partial=on_partial)
            if quick is not None:
                replies = self.analyze_replies(target, context, quick,
                                               cancel_token=cancel_token,
                                               on_partial=on_partial)
                if replies is not None and replies.json_ok:
                    quick.replies = replies.replies
                    quick.degraded = replies.degraded
                    quick.notes.extend(replies.notes)
                    quick.total_ms = round((quick.total_ms or 0) + (replies.total_ms or 0), 1)
                    quick.partial = False
                    self._put_cache(quick)
                    return quick
            logger.info("两段式不完整，回退单次生成")

        backend = "local"
        if self.api_enabled:
            messages = self.build_messages(context, target)
            self_mode = self.is_self_target(target)     # 第 79 轮：自己的消息用另一套字段
            state = {"count": 0, "emitted": 0, "prev": (None, None, None)}

            def _emit(text: str) -> None:
                """单次合并调用的流式上屏（第 79 轮）：先标题三件套，再逐条选项。"""
                if on_partial is None:
                    return
                match = _RE_INTENT_FIELD.search(text)
                intent = match.group(1).strip() if match else ""
                match = _RE_EMOTION_FIELD.search(text)
                emotion = match.group(1).strip() if match else ""
                match = _RE_DANGER_FIELD.search(text)
                danger = int(match.group(1)) if match else None
                found = _RE_OPTION_ITEM.findall(text)
                if found and len(found) != state["count"]:
                    state["count"] = len(found)
                    try:
                        on_partial(AnalysisResult(
                            msg_key=target.msg_key, json_ok=True, partial=True,
                            intent=intent, emotion=emotion, danger_level=danger,
                            self_analysis=self_mode,
                            replies=[ReplyOption(style=s.strip() or "回复", text=t.strip())
                                     for s, t in found]))
                    except Exception as exc:
                        logger.debug("API 单次调用 partial 上屏失败：%s", exc)
                    return
                current = (intent or None, emotion or None, danger)
                if state["emitted"] >= 3 or current == state["prev"]:
                    return
                state["prev"] = current
                state["emitted"] += 1
                try:
                    on_partial(AnalysisResult(
                        msg_key=target.msg_key, json_ok=True, partial=True,
                        intent=intent, emotion=emotion, danger_level=danger,
                        self_analysis=self_mode))
                except Exception as exc:
                    logger.debug("API 单次调用 partial 上屏失败：%s", exc)

            # 单次合并调用：一次请求出全部字段（prompt 只发一次，是最直接的省钱手段）；
            # 第 79 轮起默认走**流式**，边收边把已闭合的字段显示出来（等待感明显降低）。
            raw = self._api_generate_stream(messages,
                                            max_tokens=int(self.cfg.api.max_tokens_replies),
                                            cancel_token=cancel_token, on_delta=_emit)
            if raw is not None:
                result = self.parse(raw, target.msg_key)
                result.backend = "api"
                result.self_analysis = self_mode
                if result.json_ok and len(result.replies) >= REPLY_OPTION_COUNT:
                    result.notes.append(f"API 后端：{self.cfg.api.model}")
                    self._put_cache(result)
                    return result
                logger.warning("API 返回内容不合格，回落本地模型：%s", "；".join(result.notes[-2:]))
                # 记下原始返回前 200 字，便于定位（截断 / 模型没按 JSON 输出 / 服务返回了别的结构）
                logger.warning("API 原始返回（前 200 字）：%s", (raw or "")[:200].replace("\n", " "))
                result.notes.append("API 返回内容不合格（可能被 max_tokens 截断或未按 JSON 输出）")
                self._api_cooldown_until = time.time() + 3.0     # 别在同一轮里重复请求
                if self.llm is None:
                    # API 模式没加载本地模型 → 直接把这个"有诊断信息的结果"交给 UI，
                    # 而不是硬走一遍注定失败的本地分支（那会再发一次 API 请求）
                    result.backend = "api"
                    return result
            else:
                logger.warning("API 后端不可用，回落本地模型")
            if self.llm is None and not self.load():
                return None
            backend = "local(API回落)"

        if self.llm is None:
            logger.warning("分析器未加载，跳过：%s", target.msg_key)
            return None

        messages = self.build_messages(context, target)
        a = self.cfg.analyzer
        best: Optional[AnalysisResult] = None
        for attempt in range(max(1, a.retry_on_invalid + 1)):
            raw, first_ms, total_ms = self._generate(
                messages, a.temperature + 0.2 * attempt, a.seed + attempt,
                program=fm.PROGRAM_FULL)
            result = self.parse(raw, target.msg_key)
            self._note_constrain_result(result.json_ok)
            result.backend = backend
            self._apply_danger_rule(result, target)
            result.first_token_ms = round(first_ms, 1) if first_ms is not None else None
            result.total_ms = round(total_ms, 1)
            if not self.grammar_ok:
                result.degraded = True
                result.notes.append(f"语法未生效：{self.grammar_error}")
            if result.json_ok and len(result.replies) >= 3 and not result.degraded:
                best = result
                break
            best = best or result
            if attempt < a.retry_on_invalid:
                logger.info("选项校验不通过，重试一次：%s（%s）",
                            target.msg_key, "；".join(result.notes[-2:]))
        if best is not None:
            self._put_cache(best)
        return best

    # ---------------- 缓存 ----------------
    def get_cached(self, msg_key: str) -> Optional[AnalysisResult]:
        with self._cache_lock:
            result = self._cache.get(msg_key)
            if result is None:
                return None
            self._cache.move_to_end(msg_key)
        if time.time() - result.created_at > self.cfg.analyzer.dedupe_seconds:
            # 超过去重窗口仍可用缓存，但标记为"需要时可重算"
            result.notes.append("缓存超过去重窗口")
        hit = AnalysisResult(**{**result.__dict__, "notes": list(result.notes), "cache_hit": True})
        return hit

    def _put_cache(self, result: AnalysisResult) -> None:
        with self._cache_lock:
            self._cache[result.msg_key] = result
            self._cache.move_to_end(result.msg_key)
            while len(self._cache) > self.cfg.analyzer.cache_size:
                self._cache.popitem(last=False)

    def clear_cache(self, msg_key: Optional[str] = None) -> int:
        """清缓存（全部或某一条）。线程安全：

        主线程（手动刷新）与工作线程都会碰这个字典，之前直接 pop 有竞态风险。
        """
        with self._cache_lock:
            if msg_key is None:
                count = len(self._cache)
                self._cache.clear()
                return count
            return 1 if self._cache.pop(msg_key, None) is not None else 0

    def cache_size(self) -> int:
        return len(self._cache)

    def status(self) -> Dict[str, Any]:
        return {"loaded": self.llm is not None,
                "model": None if self.model_path is None else self.model_path.name,
                "load_s": self.load_s, "gpu_layers": self.gpu_layers_used,
                "gpu_fallback": self.gpu_fallback,
                "grammar_ok": self.grammar_ok, "grammar_error": self.grammar_error,
                "constrain": ("fast（自己算掩码）" if getattr(self, "fast_enabled", False)
                              else f"gbnf（{getattr(self, 'fast_note', '')}）"),
                "constrain_fast_ready": bool(getattr(self, "fast", None) is not None),
                "cuda_dirs": self.cuda_dirs, "gpu_evidence": self.gpu_evidence,
                "cache_size": self.cache_size(), "last_error": self.last_error}


if __name__ == "__main__":
    import logging as _logging

    from config import load_config
    from core.message_reader import Message, make_msg_key

    _logging.basicConfig(level=_logging.INFO, format="%(levelname)-7s %(message)s")
    cfg = load_config()
    analyzer = Analyzer(cfg)
    if not analyzer.load():
        raise SystemExit(f"加载失败：{analyzer.last_error}")
    demo = Message(msg_key=make_msg_key("demo", "中午吃啥"), text="中午吃啥", side="left",
                   bbox=(0, 0, 100, 20))
    out = analyzer.analyze(demo, [demo])
    print(json.dumps(out.as_dict() if out else None, ensure_ascii=False, indent=2))
