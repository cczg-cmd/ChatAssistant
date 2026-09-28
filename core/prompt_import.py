#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/prompt_import.py - 把"AI 生成的一段文本"解析成整套提示词（第 79 轮）

用户口径："不是所有 AI 都能生成一份 JSON 供用户下载，而且用户下载 JSON 再导入也比较麻烦，
能不能改成用户直接在网页版上复制 AI 生成的内容，直接粘贴到面板的指定接口上就自动装好
整套提示词？"

所以这里**不要求 JSON**：能认 JSON 最好，认不出就当"人写的七行文本"来拆。支持：
  · JSON（我们自己的键名，或中文键名）
  · `意图：xxxx` / `意图: xxxx` 行式
  · `【意图】` 后面换行接正文的块式
  · Markdown 的 `**加粗**`、`# 标题`、`- 列表`、`1. 序号`
  · 可选的中文引号/全角冒号
纯函数（不依赖 Qt），方便单测：`parse_prompt_bundle(text) -> (fields, warnings, name)`
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from core.copy_template_default import DEFAULT_COPY_TEMPLATE

# 每个键可以有很多说法；匹配时**长的先试**（"魅力评价" 必须先于 "魅力"）
# 标签 → **槽位**（不区分体系；体系由"小节标记"或关键词提示决定）
SLOT_BY_LABEL: Dict[str, str] = {
    "意图": "intent", "意图说明": "intent", "intent": "intent",
    "情绪": "emotion", "情绪说明": "emotion", "emotion": "emotion",
    "危险度": "danger", "危险度说明": "danger", "danger": "danger",
    "魅力评分": "danger", "质量评分": "danger", "压制力": "danger",
    "回复建议": "suggestion", "回复建议说明": "suggestion", "suggestion": "suggestion",
    "魅力评价": "suggestion", "消息点评": "suggestion", "战评": "suggestion",
    "回复选项": "option", "回复选项说明": "option", "option": "option",
    "发言选项": "option", "三连斩": "option", "选项": "option",
    "整体风格": "style", "分析风格": "style", "风格": "style", "style": "style",
    # 第 81 轮末：角色包 = 整体风格（"这套配置统一的说话风格"），
    # 面板和复制模板都改成这个名字；导入时两种叫法都认。
    "角色包": "style", "人物包": "style", "角色设定": "style",
    "用户画像": "user_profile", "我的说话习惯": "user_profile", "画像": "user_profile",
}

# 没有小节标记时，用标签本身猜这套属于哪个体系
SYSTEM_HINT: Dict[str, str] = {
    "危险度": "other", "回复建议": "other", "回复选项": "other",
    "魅力评分": "self", "魅力评价": "self", "发言选项": "self",
    "质量评分": "self", "消息点评": "self",
    # 第 80 轮末（用户实测："解析装入经常漏掉自我分析那块"）：`整体风格[自我风格]`
    # 这种**显示名**也要能判断体系，否则两行"整体风格"会挤进同一个键。
    "对方风格": "other", "自我风格": "self", "自己风格": "self", "我的风格": "self",
    # 角色包那两行写成 `角色包[分析对方]` / `角色包[分析自己]`，靠显示名区分体系
    "分析对方": "other", "分析自己": "self",
}

SECTION_WORDS: Tuple[Tuple[str, str], ...] = (
    ("分析对方", "other"), ("对方", "other"), ("危险度", "other"),
    ("分析自己", "self"), ("自我分析", "self"), ("自我", "self"), ("自己", "self"),
    ("魅力", "self"),
)

# 输出用的键：中性命名，最后交给 `map_bundle_to_keys()` 落到具体配置里
PROMPT_KEYS = ("intent_prompt", "emotion_prompt",
               "other_danger_prompt", "other_suggestion_prompt", "other_option_prompt",
               "other_style_prompt",
               "self_danger_prompt", "self_suggestion_prompt", "self_option_prompt",
               "self_style_prompt",
               "style_prompt")          # 配置里"对方那套"的整体风格就叫 style_prompt
NAME_KEYS = ("intent_name", "emotion_name", "other_danger_name", "other_suggestion_name",
             "other_option_name", "self_danger_name", "self_suggestion_name",
             "self_option_name")
USER_PROFILE_KEY = "user_profile"

MAX_NAME_CHARS = 8
MAX_PROMPT_CHARS = 900
MAX_PROFILE_CHARS = 400


def clean_label(text: str) -> str:
    """把标签/键名洗干净用于比对：去掉 Markdown 记号、括号、空格、大小写差异。"""
    text = (text or "").strip()
    # 注意 `=`：小节标题常写成 `===== 分析对方 =====`，不剥掉就会长过 16 字而被判成正文
    # （踩过：self 小节没被识别，结果两套内容全挤进"分析对方"那一套）。
    text = re.sub(r"[*#>`~\-=\+|/\\\u2022\u3000\s]+", "", text)
    text = text.strip("【】[]（）(){}<>《》:：,，.。、;；'\"“”‘’")
    return text.lower()


def _alias_map() -> Dict[str, str]:
    """标签 → 槽位（长标签优先，避免"魅力评价"被"魅力"抢走）。"""
    out: Dict[str, str] = {}
    for label in sorted(SLOT_BY_LABEL, key=len, reverse=True):
        out.setdefault(clean_label(label), SLOT_BY_LABEL[label])
    return out


_ALIAS_MAP = _alias_map()
_VALID_KEYS = set(PROMPT_KEYS) | set(NAME_KEYS) | {USER_PROFILE_KEY}


def key_for(system: str, slot: str) -> str:
    """(体系, 槽位) → 中性键名。意图/情绪两边共用，只有一份。"""
    if slot in ("intent", "emotion"):
        return f"{slot}_prompt"
    if slot == "user_profile":
        return USER_PROFILE_KEY      # 画像不由 AI 生成（第 79 轮用户口径）
    return f"{system}_{slot}_prompt"
_LABEL_LINE = re.compile(r"^\s*(?:[*#>`\-\u2022]|\d+[.、)])*\s*(.{1,32}?)\s*[：:]\s*(.*)$")
_BRACKET_LINE = re.compile(r"^\s*(?:[*#>`\-\u2022]|\d+[.、)])?\s*[【\[]([^】\]]{1,16})[】\]]\s*(.*)$")


def split_label(raw: str) -> Tuple[str, str]:
    """把 `意图[想砍啥]` 拆成 (基础标签, 显示名)；没有方括号就返回 (标签, "")。

    第 79 轮（用户实测）：AI 很爱写成 `意图[想砍啥]：…` 这种"标签[自定义字段名]：内容"，
    旧解析把整串 `意图[想砍啥]` 当成标签名去查表 → 查不到 → 提示"没识别出字段"。
    """
    raw = (raw or "").strip()
    match = re.match(r"^(.*?)[\[【(（]\s*(.+?)\s*[\]】)）]\s*$", raw)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return raw, ""


def lookup_key(label: str) -> Optional[str]:
    """标签 → 我们的键。先精确查表；查不到再按"最长前缀/包含"兜底（容忍 AI 自己加词）。"""
    clean = clean_label(label)
    if not clean:
        return None
    if clean in _ALIAS_MAP:
        return _ALIAS_MAP[clean]
    for alias, key in sorted(_ALIAS_MAP.items(), key=lambda item: -len(item[0])):
        if alias and (clean.startswith(alias) or alias.startswith(clean)) and len(clean) >= 2:
            return key
    return None


def _sibling_key(key: str) -> Optional[str]:
    """`other_danger_prompt` ↔ `self_danger_prompt`（意图/情绪/画像没有另一套，返回 None）。"""
    if key.startswith("other_"):
        return "self_" + key[len("other_"):]
    if key.startswith("self_"):
        return "other_" + key[len("self_"):]
    return None


def guess_system(base: str, shown: str) -> str:
    """没写小节标记时，按"显示名 → 基础标签 → 体系关键词"猜这套属于哪个体系。

    第 80 轮末修的真 bug（用户实测："解析装入经常漏掉自我分析那块，有的提示词能全部装进去，
    有的就只装了分析对方那一块"）：旧代码拿**整串标签**去查表 ——
    `魅力评分[魅力]` 会被 `clean_label` 洗成 `魅力评分魅力`，查不到 → 一律 fallback 成
    "分析对方"，于是魅力评分/魅力评价/发言选项全部写进"对方那套"的同名键里
    （还会**追加**成一段），最后 `map_bundle_to_keys` 一看 self_* 是空的 → 自我分析整块没装进去。
    现在先看显示名（`整体风格[自我风格]` 靠"自我"判定），再看基础标签（`魅力评分` 本身）。
    """
    for cand in (shown, base):
        clean = clean_label(cand)
        if not clean:
            continue
        if clean in SYSTEM_HINT:
            return SYSTEM_HINT[clean]
        for word, system in SECTION_WORDS:
            if clean_label(word) in clean:
                return system
    return "other"


def _trim(value: str, key: str) -> Tuple[str, Optional[str]]:
    """按字段上限裁剪；返回 (值, 警告或 None)。"""
    value = re.sub(r"[ \t]+", " ", (value or "").strip())
    limit = (MAX_NAME_CHARS if key in set(NAME_KEYS)
             else MAX_PROFILE_CHARS if key == USER_PROFILE_KEY else MAX_PROMPT_CHARS)
    if len(value) > limit:
        return value[:limit], f"{key} 太长（{len(value)} 字）已截到 {limit} 字"
    return value, None


def _from_json(text: str) -> Optional[Dict[str, str]]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    out: Dict[str, str] = {}
    for raw_key, raw_value in payload.items():
        clean = clean_label(str(raw_key))
        # 先按"配置里的真实键名"认（danger_prompt / self_option_name…）
        direct = None
        for key in _VALID_KEYS:
            if clean == clean_label(key):
                direct = key
                break
        if direct is None:
            # 第 80 轮末：JSON 的键也可能是 `魅力评分[魅力]` 这种"标签[显示名]"写法，
            # 走和文本路径同一套拆分 + 体系判断（否则这类 JSON 会整条丢掉）。
            base, shown = split_label(str(raw_key))
            slot = _ALIAS_MAP.get(clean) or lookup_key(base)
            if slot:
                direct = key_for(guess_system(base, shown), slot)
        if direct and isinstance(raw_value, (str, int, float)):
            out[direct] = str(raw_value)
    return out or None


def is_section_line(line: str) -> Tuple[bool, str]:
    """判断这一行是不是"小节标题"（如 `===== 分析对方（危险度体系） =====`、`## 自我分析`）。

    只认**没有冒号**、且带体系关键词的行 —— 否则会把 `整体风格：…` 这种正文误判成小节。
    """
    stripped = (line or "").strip()
    if not stripped or ("：" in stripped) or (":" in stripped):
        return False, ""
    text = clean_label(stripped)
    # 小节标题必须"像标题"：短、且没有句读符号。
    # 踩过的坑：正文里的"写对方真正想做什么，言外之意也要写"含"对方"，
    # 被旧判定当成小节标题 → 之后所有字段都被算进"分析对方"那一套。
    if len(text) > 16 or any(ch in stripped for ch in "。，；、！？…,.!?;~"):
        return False, ""
    for word, system in SECTION_WORDS:
        if clean_label(word) in text:
            return True, system
    return False, ""


def parse_prompt_bundle(text: str) -> Tuple[Dict[str, str], List[str], str]:
    """把一段文本拆成整套提示词。

    返回 (fields, warnings, suggested_name)：fields 里只放**解析出来**的键；
    warnings 是给人看的提示；suggested_name 是可能的第一行标题（没识别到就空串）。
    """
    warnings: List[str] = []
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return {}, ["没有内容：请把 AI 生成的那段文字粘进来"], ""

    fields = _from_json(raw)
    source = "JSON"
    if fields is None:
        fields = {}
        source = "文本"
        section = "other"                    # 没写小节标记时先当"分析对方"
        has_sections = False
        current: Optional[str] = None            # 块式：标签行后面跟正文
        buffer: List[str] = []
        first_line = ""
        label_of_key: Dict[str, str] = {}        # 键 → 写它的那个标签（防两套挤进同一个键）

        def flush() -> None:
            if current and buffer:
                piece = "\n".join(buffer).strip()
                if piece:
                    fields[current] = (fields.get(current, "") + "\n" + piece).strip()
            buffer.clear()

        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            ok_section, which = is_section_line(stripped)
            if ok_section:
                flush()
                current = None
                section, has_sections = which, True
                continue
            match = _LABEL_LINE.match(stripped) or _BRACKET_LINE.match(stripped)
            slot = None
            shown_name = ""
            rest = ""
            if match:
                base, shown_name = split_label(match.group(1))
                slot = lookup_key(base)
                rest = (match.group(2) or "").strip()
            if slot:
                flush()
                # 体系：写了小节标记就用小节；没写就按"显示名 + 基础标签"猜
                # （**必须用拆分后的 base**——整串 `魅力评分[魅力]` 查不到表，见 guess_system）
                system = section if has_sections else guess_system(base, shown_name)
                key = key_for(system, slot)
                # 两行"整体风格"（对方一份、自我一份）且没写小节时：第二份只能是自我那套
                if (not has_sections and slot == "style" and system == "other"
                        and fields.get("other_style_prompt")
                        and not fields.get("self_style_prompt")):
                    system, key = "self", "self_style_prompt"
                # 兜底：同一个键已被**另一个标签**写过 → 说明这是另一套的同名字段
                #（AI 爱用自定义叫法）。另一套还空着就挪过去，别把两套内容追加成一段。
                prev_label = label_of_key.get(key)
                sibling = _sibling_key(key)
                if (prev_label and prev_label != clean_label(base)
                        and sibling and not fields.get(sibling)):
                    key = sibling
                label_of_key[key] = clean_label(base)
                current = key
                # `意图[想斩向]：` 里的中括号是**面板显示名** → 顺便写进对应的 name 键
                if shown_name and key.endswith("_prompt"):
                    name_key = key.replace("_prompt", "_name")
                    if name_key in NAME_KEYS:
                        fields.setdefault(name_key, shown_name)
                if rest:
                    fields[key] = (fields.get(key, "") + "\n" + rest).strip()
                    current = None               # 行内值：不进块模式
                continue
            if current:
                buffer.append(stripped)
                continue
            if not first_line:
                first_line = stripped
        flush()
        # 没有小节标记时：整份文本通常只写了一套体系 —— 「整体风格」要跟着那套走，
        # 否则魅力体系那份的风格会被塞进"分析对方"里（用户口径：两套要分开）。
        if not has_sections and "other_style_prompt" in fields:
            self_has = any(fields.get(f"self_{slot}_prompt") for slot in
                           ("danger", "suggestion", "option"))
            other_has = any(fields.get(f"other_{slot}_prompt") for slot in
                            ("danger", "suggestion", "option"))
            if self_has and not other_has:
                fields["self_style_prompt"] = fields.pop("other_style_prompt")
        suggested = ""
        if first_line and "：" not in first_line and len(first_line) <= 16:
            suggested = clean_label(first_line) or ""

    if not fields:
        return {}, ["没识别出字段。可以点「复制模板」，让 AI 按那 7 行的格式重写一遍再粘回来"], ""

    cleaned: Dict[str, str] = {}
    for key, value in fields.items():
        if key == USER_PROFILE_KEY:
            continue                      # 用户口径：画像绝不由 AI 填写
        value, warn = _trim(value, key)
        if value:
            cleaned[key] = value
            if warn:
                warnings.append(warn)

    found = [k for k in PROMPT_KEYS if cleaned.get(k)]
    if not found:
        return {}, ["识别到标签但没有内容：请检查每行是不是“字段名：内容”"], ""
    if len(found) < 3:
        warnings.append(f"只识别到 {len(found)} 项内容，其余字段会用内置默认说明")
    warnings.insert(0, f"来源格式：{source}")
    return cleaned, warnings, locals().get("suggested", "")


def map_bundle_to_keys(bundle: Dict[str, str], target: str) -> Dict[str, str]:
    """把解析出来的"中性键"落到具体配置里的键名。

    target = "both"  → 两套都装：对方那套来自 other_*，自我那套来自 self_*
                        （文本只给了一套时，另一套留空 = 继承默认，不硬塞）
    target = "other" → 只装"分析对方"那套；文本若只写了 self_*（例如对方那套用了
                       "魅力评分[压制力]"这种自定义叫法），就把它当对方那套用
    target = "self"  → 只装"自我分析"那套（同理做反向兜底）
    """
    out: Dict[str, str] = {}
    # 意图/情绪两边共用
    for key in ("intent_name", "intent_prompt", "emotion_name", "emotion_prompt"):
        if bundle.get(key):
            out[key] = bundle[key]

    def take(system: str, name: str) -> Optional[str]:
        return bundle.get(f"{system}_{name}")

    other_slots = ("danger", "suggestion", "option")
    other_has = any(bundle.get(f"other_{slot}_prompt") for slot in other_slots)
    self_has = any(bundle.get(f"self_{slot}_prompt") for slot in other_slots)

    if target == "both":
        for slot in other_slots:
            src = "other" if other_has else "self"
            if bundle.get(f"{src}_{slot}_prompt"):
                out[f"{slot}_name"] = bundle.get(f"{src}_{slot}_name", "")
                out[f"{slot}_prompt"] = bundle[f"{src}_{slot}_prompt"]
            if bundle.get(f"self_{slot}_prompt"):
                out[f"self_{slot}_name"] = bundle.get(f"self_{slot}_name", "")
                out[f"self_{slot}_prompt"] = bundle[f"self_{slot}_prompt"]
        for src, dst in (("other", "style_prompt"), ("self", "self_style_prompt")):
            value = bundle.get(f"{src}_style_prompt")
            if not value and src == "other":
                value = bundle.get("self_style_prompt") if not other_has else ""
            if value:
                out[dst] = value
        # JSON 里若直接给了配置键名 style_prompt（=对方那套风格）也要认
        if not out.get("style_prompt") and bundle.get("style_prompt"):
            out["style_prompt"] = bundle["style_prompt"]
        return out

    if target == "other":
        src = "other" if other_has else "self"
        for slot in other_slots:
            if bundle.get(f"{src}_{slot}_prompt"):
                out[f"{slot}_name"] = bundle.get(f"{src}_{slot}_name", "")
                out[f"{slot}_prompt"] = bundle[f"{src}_{slot}_prompt"]
        style = (bundle.get("other_style_prompt") or bundle.get("style_prompt")
                 or bundle.get("self_style_prompt"))
        if style:
            out["style_prompt"] = style
        return out

    # target == "self"
    src = "self" if self_has else "other"
    for slot in other_slots:
        if bundle.get(f"{src}_{slot}_prompt"):
            out[f"self_{slot}_name"] = bundle.get(f"{src}_{slot}_name", "")
            out[f"self_{slot}_prompt"] = bundle[f"{src}_{slot}_prompt"]
    style = (bundle.get("self_style_prompt") or bundle.get("other_style_prompt")
             or bundle.get("style_prompt"))
    if style:
        out["self_style_prompt"] = style
    return out


def effective_template(cfg: Any = None, style_hint: str = "") -> str:
    """当前**生效**的复制模板：设置面板里改过就用改过的，没改过用代码里的内置默认。

    第 80 轮末（用户口径："点开这个按钮跳出一个文本窗口，可以自己在里面修改复制模板"）：
    用户在设置面板「设置模板」里保存的副本存在 `cfg.copy_template`；留空 = 内置默认
    （`build_template()`）。「复制模板」按钮走这个函数，所以改完立刻生效。
    """
    custom = str(getattr(cfg, "copy_template", "") or "").strip()
    return custom or build_template(style_hint)


def build_template(style_hint: str = "") -> str:
    """给用户复制到网页版 AI 的模板（不需要 JSON，也不用下载文件）。

    第 80 轮（用户实测："ai 给的提示词照抄模板提示词的部分太多，对风格化基本没有具体描述，
    只是单纯的关键词替换，导致本地模型输出套路化很严重"）：旧模板把**字段要点写成了句子**
    （"（他真正想干什么／言外之意；≤13 字；风格要求；不要复述…）"），生成提示词的 AI 直接
    照着誊 + 换几个词就交差了。新版按三个角度设计：

      ① **给生成提示词的 AI**：字段清单只给"要点 + 硬指标"（故意写成短词条、不成句），
         并明确"不要照抄示例和要点，要展开写成 2-4 句真正的说明"，还给了自检清单；
      ② **让生成的说明能真正约束本地模型**：每条说明按固定顺序写全六样 ——
         怎么判断／**角色语言特征**（自称、句尾语气词 2-3 个 + 频率、句长、用词偏好）／
         硬指标（照抄数字；数字类字段"只给数字、不加语气词"）／不要怎么写（具体到句子）／
         档位判据／**微型示例 `情境或原话 → 你要写出来的样子`**（只示范语气，一两个就够）。
         这是对着用户实测"效果最好的那份提示词"反推出来的规格：它的每一段都是
         "判断口径 + 具体语言特征（自称/尾音/句长）+ 加粗的硬指标 + 档位判据 + 微型示例"。
         第 80 轮续（用户实测"没点名风格也硬塞口癖、本地模型照样套路化"）：禁止的是
         **自作主张**（没要求却给每个字段安排口癖）；用户点名"猫娘/每句带喵"这类角色风格时，
         每个字段都带口癖是该风格本身的要求，照写。
      ③ **特定角色风格怎么办（第 80 轮末新增）**：用户点名某个**具体角色/作品人物**时，
         先生成 AI **联网搜索**该角色的台词/口癖/自称/语气；但**最终提示词里不许出现角色名或
         作品名**（4B 不认识它，写名字只会被照抄或无视），必须把角色**翻译成语言特征**
         （自称、尾音、句长、常用词、吐槽与关心时的说法），并且**回复选项/发言选项里
         尤其要体现人物特色**。搜不到就退化成"风格关键词"描述，不许编台词。
      ④ **保证本地模型输出多样、合风格**：要求写清"三条立场/说法必须拉开、不许固定同一批
         开头/句式"，并给一条猫娘配置的**合格说明**当格式样板（只对格式、不许照抄内容）。
          （第 80 轮末用户实测："ai 给的回复选项提示词模板经常比较死，本地模型三个选项开头
          都用固定句式" → "三条"字段的变化必须写成**规则**（开头类型/句长/语气怎么错开），
          示例只示范语气（一两个短例）、并标注"这是语气示范、不是固定开场白"，不许指定
          "第一条必须用『××』开头"。上一版曾干脆禁止给示例句，但用户实测效果最好的那份
          提示词**是带示例的** → 现在改成"可以有示例，但必须写成可变化、非固定开场白"。
          第 80 轮末再微调：**示例不再按立场逐条给**（用户口径："按立场给本地模型更容易套路化
          模仿"）—— 只要写成"示范语气的短例"，不按 认同/反调/吐槽 对号入座。）

    ## 想自己微调模板？看这里（第 80 轮末补，第 81 轮更新）

    **模板正文现在放在 `core/copy_template_default.py` 的 `DEFAULT_COPY_TEMPLATE`**（第 81 轮：
    把用户在设置面板里调好的那套提升为内置默认），本函数只负责在末尾接上 `style_hint`。
    改模板有三种方式：
      ① 应用内：设置面板 →「设置模板」→ 改完保存（存进 `config.json` 的 `copy_template`，
         **优先于**代码里的默认；清空再保存 = 回到默认）；
      ② 直接改 `core/copy_template_default.py` 里那段字符串（改完重启开发版）；
      ③ 改完想固化给别人：把内容同步到 `core/copy_template_default.py`。
    模板各段管什么：
      · `【输出格式（先看这条，不照做会装不进去）】` 在最前面：**每条说明写成一行、不许另起
        "风格说明：/三条变化："这种带冒号的小标题** —— 导入是**按行解析**的，这类小标题会被
        当成新字段（"风格说明："会被"整体风格"的别名吃掉）或者直接丢掉（用户实测：回复选项/
        发言选项里那两段没装进去）。想让字段说明能换行，得同时改解析器，别只改模板；
      · `1)`~`7)` 在 `【怎么写才算合格】` 段里：1 别照抄／2 每条说明的六样规格
        （含 ⑥ 微型示例的写法）／3 角色风格规则（联网搜索、不许写角色名）／4 口癖条款／
        5 三条字段（回复选项、发言选项）／6 数字评分字段不许被风格带偏／7 自检；
      · `【一条合格说明的样例】` 只给格式样板（现在是猫娘的情绪行）；
      · `【字段清单】` 是**给 AI 的要点**，用 `字段名[显示名]：要点｜硬指标` 的行式；
      · 最后一行 `我的要求：` 由 `style_hint` 接在后面（用户自己打字）。
    ⚠ 两条硬约束：
      ① `===== 分析对方（危险度体系）=====` / `===== 分析自己（魅力体系）=====` 两个分割线
         和"`字段名[显示名]：内容`"的行格式**别改**（`parse_prompt_bundle` 靠它们认字段，
         见 core/prompt_import.py 顶部的 `SLOT_BY_LABEL` / `SYSTEM_HINT` / `guess_system`）；
      ② 改完跑 `cache\\py311\\python.exe tools\\test_prompt_import.py`（用例 H 有断言盯着
         模板里的若干措辞，改文案时把它一起改，或者保留那些关键短语）；重启开发版后生效
         （设置面板点"复制模板"时才会调用本函数）。
    模板长度、字段上限（≤13／≤25／≤30 字）等数字要和 `config.FIELD_LIMITS` 保持一致。
    """
    return DEFAULT_COPY_TEMPLATE + style_hint
