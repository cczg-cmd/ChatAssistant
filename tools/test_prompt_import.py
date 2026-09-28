#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：把"AI 生成的一段文本"粘进设置面板就能装好整套提示词。

用户口径："不是所有 AI 都能生成 JSON 供下载，用户下载再导入也麻烦 —— 直接复制 AI 生成的内容
粘到面板的指定接口上就自动装好整套提示词。"

覆盖：
  A. 行式（我们模板的格式）`意图：…`
  B. 块式 `【意图】` + 换行正文
  C. Markdown（`**加粗**`、`1.`、全角冒号）
  D. JSON（我们的键名 / 中文键名）
  E. 完全无关的文字 → 明确报"没识别出"，不许瞎装
  F. 超长字段被裁剪并有提示
  G. 设置面板：粘贴 → 新建一套配置 + 界面刷成这套 + 全局风格/画像也装上

用法：python tools/test_prompt_import.py

注（第 81 轮）：**模板正文现在放在 `core/copy_template_default.py` 的 `DEFAULT_COPY_TEMPLATE`**
（用户在设置面板里调好的那套已提升为内置默认）。用例 H 断言的是这份正文里的关键不变量；
以后自己改模板时如果把这些短语删了，记得同步改用例 H（或者保留这些短语）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import config as app_config  # noqa: E402
from core.prompt_import import build_template, map_bundle_to_keys, parse_prompt_bundle  # noqa: E402
from ui import settings_dialog as sd  # noqa: E402

LABELED = """女仆配置
意图：用口语写对方想干什么，隐含意思也要写，最多 14 字
情绪：2-4 个字的情绪词
魅力评分：0-10 整数，越高越迷人；中规中矩 5 分
魅力评价：给这条消息一句评价，最多 30 字
发言选项：三条我接下来要发给对方的话，立场要有反差
整体风格：用女仆口吻，温柔但简短
用户画像：我说话很短，常用“确实/是的”
"""

BLOCK = """【意图】
写对方真正想做什么，言外之意也要写
【情绪】
日常情绪词
【发言选项】
三条能直接发的话
"""

MARKDOWN = """## 分析风格
1. **意图**：看他想干什么
2. **情绪**：两三个字
3. **回复选项**：三条，立场拉开
"""


def case_labeled() -> bool:
    fields, warnings, name = parse_prompt_bundle(LABELED)
    ok = (fields.get("intent_prompt", "").startswith("用口语写")
          # 第 79 轮：没写小节标记时按标签猜体系 —— 这份用的是魅力体系 → self_*
          and "女仆口吻" in fields.get("self_style_prompt", "")
          and "魅力" not in fields.get("style_prompt", "")
          and "用户画像" not in "".join(fields.values())     # 画像不由 AI 填写
          and name == "女仆配置")
    print(f"A 行式文本：{'对' if ok else '错'}（识别 {len(fields)} 项，名字={name!r}）")
    return ok


def case_block() -> bool:
    fields, _w, _n = parse_prompt_bundle(BLOCK)
    ok = ("言外之意" in fields.get("intent_prompt", "")
          and fields.get("emotion_prompt", "") == "日常情绪词"
          # 【发言选项】是魅力体系的叫法 → 落到 self_option
          and "三条" in (fields.get("self_option_prompt") or fields.get("option_prompt", "")))
    print(f"B 块式【标签】+ 换行正文：{'对' if ok else '错'}（{len(fields)} 项）")
    return ok


def case_markdown() -> bool:
    fields, _w, _n = parse_prompt_bundle(MARKDOWN)
    ok = ("看他想干什么" in fields.get("intent_prompt", "")
          and fields.get("emotion_prompt", "") == "两三个字"
          and "立场拉开" in (fields.get("other_option_prompt") or fields.get("option_prompt", "")))
    print(f"C Markdown 粗体/序号/全角冒号：{'对' if ok else '错'}（{len(fields)} 项）")
    return ok


def case_json() -> bool:
    text = ('{"intent_prompt":"看他想干什么","emotion_prompt":"好奇","魅力评分":"0-10",'
            '"发言选项":"三条话","style_prompt":"简短直接","用户画像":"爱说确实"}')
    fields, _w, _n = parse_prompt_bundle(text)
    ok = (fields.get("intent_prompt") == "看他想干什么"
          and (fields.get("self_danger_prompt") or fields.get("danger_prompt")) == "0-10"
          and (fields.get("self_option_prompt") or fields.get("option_prompt")) == "三条话"
          and fields.get("style_prompt") == "简短直接"
          and "user_profile" not in fields)                  # 画像被丢弃
    print(f"D JSON（含中文键）：{'对' if ok else '错'}（{len(fields)} 项）")
    return ok


def case_garbage() -> bool:
    fields, warnings, _n = parse_prompt_bundle("今天天气不错，我去买了杯咖啡，回来继续干活。")
    ok = (fields == {} and bool(warnings))
    print(f"E 无关文字被拒绝：{'对' if ok else '错'}（{warnings[0][:24] if warnings else ''}）")
    return ok


def case_truncate() -> bool:
    long_text = "意图：" + ("测" * 1200)
    fields, warnings, _n = parse_prompt_bundle(long_text)
    ok = (len(fields.get("intent_prompt", "")) == 900
          and any("截" in w for w in warnings))
    print(f"F 超长字段裁剪：{'对' if ok else '错'}（长度={len(fields.get('intent_prompt',''))}）")
    return ok


def case_dialog_import() -> bool:
    cfg = app_config.load_config()
    dlg = sd.SettingsDialog(cfg)
    user_profile_before = dlg.profile_edit.toPlainText()

    # G1：默认目标 = **整套替换**（用户口径：一般不会只导"自我分析"）
    before = len(dlg._profiles)
    dlg.import_target.setCurrentIndex(0)                 # 整套替换
    dlg.import_edit.setPlainText(TWO_SECTIONS)
    dlg._import_prompt_bundle()
    after = len(dlg._profiles)
    entry = dlg._profiles[-1]           # 刚追加的那套（名字可能带序号，别猜）
    ok1 = (after == before + 1 and entry is not None
           # 对方那套（危险度体系）来自第一个小节
           and str(entry.get("danger_name")) == "火药味"
           and str(entry.get("option_name")) == "三连斩"
           and str(entry.get("danger_prompt", "")).startswith("0-10整数")
           and str(entry.get("style_prompt", "")).startswith("直、猛、短")
           # 自我那套（魅力体系）来自第二个小节
           and str(entry.get("self_danger_name")) == "心动值"
           and str(entry.get("self_option_name")) == "出招"
           and str(entry.get("self_style_prompt", "")).startswith("放松、口语")
           and dlg.profile_edit.toPlainText() == user_profile_before)  # 画像没被覆盖
    print(f"G1 导入【整套替换】：{'对' if ok1 else '错'}"
          f"（配置 {before}→{after}，对方字段名={dlg.field_names['danger'].text()!r}，"
          f"自我字段名={dlg.self_field_names['danger'].text()!r}）")

    # G2：选"只导入分析对方" → 用户那份只写了对方那套（第三字段叫"压制力"）
    dlg.import_target.setCurrentIndex(1)                 # 只导入分析对方
    dlg.import_edit.setPlainText(BRACKET_NAME)
    dlg._import_prompt_bundle()
    entry2 = dlg._profiles[-1]
    ok2 = (entry2 is not None
           and str(entry2.get("danger_name")) == "战力值"      # 自定义叫法进了对方那套
           and str(entry2.get("option_name")) == "下一刀"
           and str(entry2.get("danger_prompt", "")).startswith("0-10整数")
           and str(entry2.get("style_prompt", "")).startswith("整体语气")
           and str(entry2.get("self_danger_prompt", "") or "") == ""   # 自我那套保持默认
           and dlg.field_prompts["danger"].toPlainText().startswith("0-10整数"))
    print(f"G2 导入【只导入分析对方】：{'对' if ok2 else '错'}"
          f"（对方字段名={dlg.field_names['danger'].text()!r}，"
          f"自我字段名={dlg.self_field_names['danger'].text()!r}）")
    return ok1 and ok2


def case_sections_and_mapper() -> bool:
    """小节标记（危险度体系 / 魅力体系）要分别落到两套；map 要能整套落位。"""
    fields, _w, _n = parse_prompt_bundle(TWO_SECTIONS)
    other_ok = (fields.get("other_danger_name") == "火药味"
                and fields.get("other_option_name") == "三连斩"
                and fields.get("other_style_prompt", "").startswith("直、猛、短"))
    self_ok = (fields.get("self_danger_name") == "心动值"
               and fields.get("self_option_name") == "出招"
               and fields.get("self_style_prompt", "").startswith("放松、口语"))
    mapped = map_bundle_to_keys(fields, "both")
    map_ok = (mapped.get("danger_name") == "火药味" and mapped.get("danger_prompt")
              and mapped.get("self_danger_name") == "心动值" and mapped.get("self_danger_prompt")
              and mapped.get("style_prompt") != mapped.get("self_style_prompt"))
    ok = other_ok and self_ok and map_ok
    print(f"K 两套体系分小节识别并整套落位：{'对' if ok else '错'}"
          f"（对方={fields.get('other_danger_name')!r} 自我={fields.get('self_danger_name')!r}）")
    return ok


def case_placeholder_catgirl() -> bool:
    """用户口径：灰字提示要"以猫娘模板重写一份"，而且两套体系要分开展示。"""
    cfg = app_config.load_config()
    dlg = sd.SettingsDialog(cfg)
    hint = dlg.import_edit.placeholderText()
    ok = ("喵" in hint and "心动值" in hint and "猫爪三连" in hint and "不用 JSON" in hint
          and "危险度体系" in hint and "魅力体系" in hint
          and "火药味" in hint and "三选一" in hint)
    print(f"J 灰字提示（猫娘示例 + 两套体系分开）：{'对' if ok else '错'}"
          f"（{len(hint)} 字，含喵={('喵' in hint)}）")
    return ok


def case_template_text() -> bool:
    """模板结构自检（第 82 轮：用户口径"复制模板要完全重写，只要告诉 ai 角色包怎么写和
    字段配置最简格式规范就好了" → 模板 = 角色包写法 + 字段格式规范 + 一份骨架）。"""
    tpl = build_template()
    # 骨架必须能被解析器认出（AI 照它填，输出才装得进去）
    sample, _w, _n = parse_prompt_bundle(tpl)
    rows = ("意图[", "情绪[", "危险度[", "回复建议[", "回复选项[",
            "魅力评分[", "魅力评价[", "发言选项[")
    ok = (tpl.rstrip().endswith("我的要求：")
          and tpl.count("===== 分析") == 2
          and all(k in tpl for k in rows)
          # 第 82 轮末：角色包**只有一份**（放在最前面、无方括号），两套体系共用
          and tpl.count("\n角色包：") == 1
          # 两块内容：角色包怎么写（约 800 字）+ 字段格式规范
          and "角色包怎么写" in tpl and "约 800 字" in tpl
          and "人设特点与人设风格" in tpl and "标志性的" in tpl
          # 第 82 轮末再调（用户口径）：角色包尽量少放语言示例、以直接描述为主；
          # 没有标志性风格就不放示例（本地模型模仿示例的倾向很强）；"关心人/吐槽/生气/敷衍"
          # 那半句按用户要求删掉了
          and "尽量少放语言示例" in tpl and "不要放任何语言示例" in tpl
          and "关心人、吐槽、生气、敷衍" not in tpl
          # 第 82 轮末再调（用户：上一版强调"少放示例"后，有标志性风格的角色连口癖/自称都被漏掉了）：
          # 模板必须**明确要求**有标志性语言习惯的角色把口癖与自称写进角色包
          and "必须把标志性的口癖、自称" in tpl
          # 第 82 轮末再调（用户实测：按模板生成的配置里"回复选项/发言选项"只有立场、没有变化要求
          # → 每条消息的三条都以"真是的/哼"开头）：模板必须要求这两行写清"三条怎么拉开"
          and "必须写清三条怎么拉开" in tpl
          and "不要固定同一批起手词" in tpl
          and "字段配置的最简格式规范" in tpl
          # 格式硬要求
          and "每行一个字段" in tpl and "不要回车换行" in tpl and "不要分点" in tpl
          and "一个字都不能改" in tpl and "显示名按风格改" in tpl
          and "13 字" in tpl and "25 字" in tpl and "30 字" in tpl
          # 角色规则（联网搜 / 不写角色名）保留在这块里
          and "先联网搜索" in tpl and "不要把角色名或作品名写进配置里" in tpl
          # 用户口径：不要让 AI 去写"用户画像"
          and "用户画像[" not in tpl
          # 骨架里不能出现多行/分点（画布形状会被镜像）
          and "\n- " not in tpl and "\n  " not in tpl
          and "……" not in tpl          # 用户口径：骨架里不要"……"省略，要给基础格式参考
          and len(sample) >= 15)
    print(f"H 模板结构 + 骨架可解析：{'对' if ok else '错'}"
          f"（{len(tpl)} 字，骨架解析出 {len(sample)} 个键）")
    return ok


BRACKET_NAME = """意图[想砍啥]：用口语劈开对方真正想干啥，隐含意思也剁出来，最多14字
情绪[战意]：2-4个字的日常情绪词，如上头、憋火、爽翻、麻了
魅力评分[战力值]：0-10整数，越高越吸引人；0-2废柴废话，3-4干巴敷衍，5中规中矩，6有点意思，7-9有细节/钩子，10神级暴击
魅力评价[这一刀]：对我这条消息一句评价，最多30字，先给结论
发言选项[下一刀]：三条我接下来发给对方的话，立场分别继续展开/吐槽/终结话题，每条≤30字、口语、用「你」称呼对方
整体风格[狂战之道]：整体语气与关注点，直接说风格：狂暴直给、热血追击、重钩子、砍废话
用户画像[敌我档案]：可以留空；如果要我模仿某种口癖，就写在这里"""

# 复制模板要求 AI 输出的形状：两个小节，两套体系各写全（危险度体系 / 魅力体系）
TWO_SECTIONS = """===== 分析对方（危险度体系）=====
意图[斩向]：先看对方字面诉求，再抓潜台词，不超过30字
情绪[血气]：2到4个情绪词，标清强度
危险度[火药味]：0-10整数，0-2日常，7-8追责，9-10翻脸
回复建议[递刀]：给一句能直接用的话，最多30字
回复选项[三连斩]：三条能直接发的，立场认同/反调/吐槽
整体风格[对方气场]：直、猛、短

===== 分析自己（魅力体系）=====
魅力评分[心动值]：0-10整数，越高越迷人，中规中矩5分
魅力评价[猫眼一瞥]：一句魅力评价，最多30字
发言选项[出招]：三条我接下来发给对方的话，立场继续展开/吐槽/终结
整体风格[自我气场]：放松、口语、带点自嘲
我的要求："""


def case_bracket_display_name() -> bool:
    """用户实测踩到的格式：`字段名[面板显示名]：内容`（旧解析把它整串当标签名 → 认不出）。"""
    fields, _w, _n = parse_prompt_bundle(BRACKET_NAME)
    # 这份用的是魅力体系叫法（魅力评分/魅力评价/发言选项）→ 没写小节时落到 self_*
    ok = (fields.get("intent_name") == "想砍啥"
          and fields.get("emotion_name") == "战意"
          and (fields.get("self_danger_name") or fields.get("other_danger_name")
               or fields.get("danger_name")) == "战力值"
          and (fields.get("self_suggestion_name") or fields.get("other_suggestion_name")
               or fields.get("suggestion_name")) == "这一刀"
          and (fields.get("self_option_name") or fields.get("other_option_name")
               or fields.get("option_name")) == "下一刀"
          and fields.get("intent_prompt", "").startswith("用口语劈开")
          and "狂暴直给" in (fields.get("self_style_prompt") or fields.get("other_style_prompt")
                              or fields.get("style_prompt", ""))
          and "user_profile" not in fields)
    print(f"I 带显示名的格式（用户实测那段）：{'对' if ok else '错'}（识别 {len(fields)} 项，"
          f"名字={fields.get('danger_name')!r}）")
    return ok


# 用户实测（第 80 轮末）："解析装入经常漏掉自我分析那块，有的提示词可以全部装进去，
# 有的就只装了分析对面那一块。" —— 这份**没有小节标记**、而且每个字段都写成
# `标签[显示名]：内容`。旧解析拿整串标签（`魅力评分魅力`）查体系表，查不到就一律
# 算成"分析对方"，魅力评分/魅力评价/发言选项全被追加进对方那套的同名键 → 自我那套整块为空。
USER_NO_SECTION = """意图[意图]：她嘴上说的和心里真正想要往往差一层，别照搬她的话面意思，想想她有没有在讨好、在回避、或者在试探你会不会讨厌她。用她那种软乎乎却偶尔冒傻劲的口气写，不超过13个字，别写成话题概括或者心情描述。微型示例：原话 → 「其实想让你多陪一会儿吧」这样的判断。
情绪[情绪]：写一个2到4个字的词，像她平时会用的那种带着点元气又容易漏出怯意的感觉，比如期待、不安、逞强、窃喜、失落。只写一个词，别写“情绪比较复杂”这种分析腔，也别用“无明显情绪”来糊弄。微型示例：原话 → 「不安…」或「偷偷开心」。
危险度[危险度]：0到10的整数，0-2是她只是随口聊聊完全没当回事，3-4是她有点在意但不会真的做什么，5-6是她开始较真了、情绪在往一个方向偏，7-8是她在用开玩笑的方式藏真心、话里带刺或带钩，9-10是她已经放弃假装没事了、说出的话连她自己都害怕。写整数，别写区间也别写小数。微型示例：原话 → 「这个…有点难说呢」暗示5-6档。
回复建议[回复建议]：给她一句能直接发出去的话，≤25字。要顺着她那种想靠近又怕被推开的劲儿，用短句、口语，别写书面语也别写太长。微型示例：原话 → 「嗯嗯我懂，别怕嘛」这种节奏。
回复选项[回复选项]：三条各≤25字。第一条认同她，说“对对就是这样”；第二条跟她唱反调，但别凶，带点她那种笨拙的反驳感；第三条吐槽，用调侃但善意的口吻，可以带点“你是小笨狗吗”那种宠溺式的吐槽。三条开头句式不要一模一样，别每条都是“我觉得”。微型示例：认同 → 「对的对的，就是那个！」；反调 → 「诶——我倒是觉得没那么糟啊」；吐槽 → 「你刚才是不是又走神了」。
整体风格[对方风格]：分析对方时用她那种有点黏人、容易把别人的冷淡当成讨厌的视角。重点看对方有没有在推开她、有没有用玩笑藏真心、是不是嘴上说没事其实很在意。语气要软，句尾可以自然带一点“呀”“呢”“吧”这种尾音，但不必每句都加。微型示例：原话 → 「感觉是在用开玩笑躲过去呢…」。
魅力评分[魅力]：0到10整数。3-4是说话很平、没什么个人色彩，5是能感觉到一点她的脾气和喜好，6-7是有明显的个性、语气有辨识度、能让人记住，8-10是每句话都带着她的气息、哪怕在说很普通的事也能让人感觉到“这是她的声音”。微型示例：原话 → 从「今天天气不错」到「今天的风好舒服呀，不觉得吗？」的差别。
魅力评价[魅力评价]：先给结论，结论要和评分同档。≤30字。用她那种软绵绵但偶尔冒出一句真心话的口气，别用“表达清晰/逻辑通顺”这种评价报告腔。微型示例：原话 → 「有那种让人想多聊两句的亲切感呢」。
发言选项[发言选项]：三条各≤25字，是你接着自己刚发的那条继续发给对方的。第一条继续展开，把你刚才说的那个话题再多说一点；第二条吐槽这件事本身，用她那种有点傻乎乎的吐槽方式；第三条终结话题，但别冷冰冰的，可以带点“那下次再说嘛”的软收尾。三条开头句式要有变化，别每条都用“我觉得”开头，也不必每条都带“我”。微型示例：展开 → 「而且啊，那个地方还有更多好玩的事」；吐槽 → 「不过说真的，那也太离谱了吧」；收尾 → 「好啦好啦，下次再聊这个～」。
整体风格[自我风格]：分析自己时用她那种故作开朗但心里在打鼓的视角。关注自己有没有在讨好、有没有把真心话咽回去、有没有在故意犯错来躲开被讨厌。语气要轻快里带一点不自信，可以偶尔用“欸嘿”那种带过去的笑声，但别每句都来。微型示例：原话 → 「刚才是不是又说太多了…欸嘿」。
"""


def case_no_section_two_systems() -> bool:
    """用户实测原文：没有小节标记 + 每行 `标签[显示名]` → 两套都要装进去、不许串。"""
    fields, _w, _n = parse_prompt_bundle(USER_NO_SECTION)
    mapped = map_bundle_to_keys(fields, "both")
    ok = (
        # 对方那套
        "随口聊聊" in mapped.get("danger_prompt", "")
        and "唱反调" in mapped.get("option_prompt", "")
        and "有点黏人" in mapped.get("style_prompt", "")
        # 自我那套（以前整块是空的）
        and "说话很平" in mapped.get("self_danger_prompt", "")
        and "结论要和评分同档" in mapped.get("self_suggestion_prompt", "")
        and "接着自己刚发的那条" in mapped.get("self_option_prompt", "")
        and "故作开朗" in mapped.get("self_style_prompt", "")
        # 显示名要跟着落位
        and mapped.get("danger_name") == "危险度" and mapped.get("option_name") == "回复选项"
        and mapped.get("self_danger_name") == "魅力" and mapped.get("self_option_name") == "发言选项"
        # 不许串味：自我那段的正文不能出现在对方那套里（旧 bug 就是被追加进去）
        and "接着自己刚发的那条" not in mapped.get("option_prompt", "")
        and "故作开朗" not in mapped.get("style_prompt", "")
    )
    print(f"L 无小节标记 + [显示名] 原文两套都装入：{'对' if ok else '错'}"
          f"（识别 {len(fields)} 键｜对方 {len([k for k in mapped if not k.startswith('self_')])} 项"
          f"｜自我 {len([k for k in mapped if k.startswith('self_')])} 项）")
    return ok


def main() -> int:
    app = QApplication(sys.argv)          # noqa: F841
    results = [case_labeled(), case_block(), case_markdown(), case_json(),
               case_garbage(), case_truncate(), case_dialog_import(), case_template_text(),
               case_bracket_display_name(), case_placeholder_catgirl(),
               case_sections_and_mapper(), case_no_section_two_systems()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
