#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui/settings_dialog.py - 应用设置（B2 追加需求）

入口：鼠标移到状态灯上（图标变成齿轮）→ 点击 → 打开本窗口。
包含：
  - 分析后端：本地部署（默认） / API 接口（OpenAI 兼容：base_url、api_key、模型名）
  - Prompt 自定义：文本框直接编辑系统提示词 + "还原默认 Prompt"
  - 显示：三条回复的风格前缀开关、状态灯大小、只分析对方消息
  - 一键还原全部默认设置
保存后写入工作区 config.json（下次启动生效；调用方也可即时应用）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

import config as app_config
from config import (ALL_PROFILE_FIELDS, DEFAULT_FIELD_NAMES,
                    DEFAULT_FIELD_PROMPTS, DEFAULT_PROFILE_NAME, DEFAULT_SELF_FIELD_NAMES,
                    DEFAULT_SELF_FIELD_PROMPTS, DEFAULT_SELF_STYLE_PROMPT, DEFAULT_STYLE_PROMPT,
                    Config, apply_profile, default_profiles, load_config, profile_entry,
                    save_config)
from core.prompt_import import effective_template, map_bundle_to_keys, parse_prompt_bundle
from ui import theme as theme_mod

logger = logging.getLogger("ui.settings_dialog")


def _local_model_name(cfg: Config) -> str:
    """本地模型的显示名：用实际解析到的 GGUF 文件名（不再写死型号，换模型自动跟着变）。"""
    try:
        path = app_config.resolve_model_path(cfg)
        if path is not None:
            return Path(str(path)).name
    except Exception as exc:
        logger.debug("解析本地模型名失败：%s", exc)
    return "未找到本地 GGUF 模型"


FIELD_ROWS = [
    ("intent", "第一条字段（默认“意图”）", "就是你最想看的那句话，比如“意图/主题/他在说什么”。"),
    ("emotion", "第二条字段（默认“情绪”）", "显示在标题里，和上一条同一行。"),
    ("danger", "第三条字段（默认“危险度”）", "可以改成别的含义，例如“兴趣水平”。"),
    ("suggestion", "回复建议", "面板中间那句给用户看的建议。"),
    ("option", "回复选项",
     "三条可直接发送的回复；风格完全由这里的说明决定（例如写成“按女仆口吻回复”）。"),
]

# 第 79 轮：自我分析（点自己发的消息）只用改这三个字段；意图/情绪与上面那套一致，
# 所以不在这里重复出现（避免出现"两处都改了、到底哪个生效"的混乱）。
SELF_FIELD_ROWS = [
    ("danger", "魅力（原“危险度”）",
     "3-10，越高越有魅力：没毛病 5；有细节/情绪/钩子更高；明显没内容才 3-4。"),
    ("suggestion", "魅力评价（原“回复建议”）",
     "对我这条消息的魅力评价（读起来想不想接、怎么更有魅力）。"),
    ("option", "发言选项（原“回复选项”）",
     "我在这条之后要发的**下一句**（有对方回复就回应它，没有就写接续句；不是重写目标消息）。"),
]


class TemplateEditDialog(QDialog):
    """「设置模板」窗口（第 80 轮末，用户口径）：直接编辑"复制模板"复制出去的那份说明。

    下面两个按钮：**保存**（accept）/ **取消**（reject）。保存后由调用方写进
    `cfg.copy_template` 并落盘；把内容清空再保存 = 回到代码里的内置默认模板。
    """

    def __init__(self, parent: Optional[QWidget] = None, current: str = "",
                 is_custom: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置复制模板")
        self.resize(940, 660)
        layout = QVBoxLayout(self)
        hint = QLabel(
            "这里就是点「复制模板」复制给网页 AI 的那段说明；改完点「保存」立即生效，"
            "点「取消」不改动。\n"
            "⚠ 模板是**一行一条**（软件按行解析）：两个 `=====` 分割线和 "
            "`字段名[显示名]：内容` 的行格式不要改；字段上限数字要和程序里的上限一致。\n"
            + ("（当前用的是**你自己保存的**模板；全部清空再保存 = 回到内置默认模板）"
               if is_custom else "（当前用的是内置默认模板）"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._edit = QPlainTextEdit(current)
        # 第 81 轮末（用户口径："设置面板的文本不会按宽度上限自动换行显示，请调整一下"）：
        # 改成按窗口宽度自动折行。注意这只是**显示层**的折行：不会往文本里插入换行符，
        # 所以"一行一条"的模板格式和按行解析都不受影响。
        self._edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        font = self._edit.font()
        font.setFamily("Consolas")
        font.setPointSize(9)
        self._edit.setFont(font)
        layout.addWidget(self._edit, 1)
        row = QHBoxLayout()
        self.count_label = QLabel("")
        row.addWidget(self.count_label)
        row.addStretch(1)
        save_btn = QPushButton("保存")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(save_btn)
        row.addWidget(cancel_btn)
        layout.addLayout(row)
        self._edit.textChanged.connect(self._refresh_count)
        self._refresh_count()

    def _refresh_count(self) -> None:
        self.count_label.setText(f"{len(self._edit.toPlainText())} 字")

    def template_text(self) -> str:
        return self._edit.toPlainText()


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("ChatAssistant 设置")
        # 用户反馈窗口太小不方便看 → 放大（并按屏幕再自适应一次）
        self.setMinimumSize(900, 820)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(1180, int(available.width() * 0.82)),
                        min(1080, int(available.height() * 0.9)))
        else:
            self.resize(1020, 960)
        self._profiles: list = []
        self._loaded_profile = ""
        self._pending_restore: Optional[Config] = None   # "一键还原"只暂存，按保存才生效
        # 设置窗口是用户主动打开的临时窗口，允许置顶（否则会被 QQ 挡在后面看不见）
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        # 第 75 轮：QDialog 默认标题栏只有关闭键 → 用户反馈"没有最小化按钮"。
        # 补上最小化（用不着最大化：窗口尺寸已按屏幕自适应）。
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_backend_tab(), "分析后端")
        tabs.addTab(self._build_prompt_tab(), "Prompt 自定义")
        tabs.addTab(self._build_display_tab(), "显示与行为")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.on_save)
        buttons.rejected.connect(self.reject)

        restore = QPushButton("一键还原默认设置", self)
        restore.clicked.connect(self.on_restore_defaults)

        bottom = QHBoxLayout()
        bottom.addWidget(restore)
        bottom.addStretch(1)
        bottom.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("提示：设置保存在工作区 config.json；保存后即时生效（Prompt 立即生效）。"))
        layout.addWidget(tabs)
        layout.addLayout(bottom)
        self._load_into_widgets()

    # ---------------- 页签：分析后端 ----------------
    def _build_backend_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.backend_combo = QComboBox()
        # 第 75 轮修复（用户："设置面板里本地部署模型名称不对"）：
        # 原来这里写死了 "Qwen2.5-3B Q4_K_M"（早期方案的模型），换模型后一直没人改。
        # 现在**读实际解析到的模型文件名**，换模型、换路径都会自动跟着变。
        # 注意：这里没有名为 cfg 的局部变量（用户反馈过 `name 'cfg' is not defined` 这个崩溃），
        # 一律走 self.cfg。
        self.backend_combo.addItem(f"本地部署（默认，{_local_model_name(self.cfg)}）", "local")
        self.backend_combo.addItem("API 接口（OpenAI 兼容，内容将发送到该服务商）", "api")
        form.addRow("分析后端：", self.backend_combo)

        api_group = QGroupBox("API 参数（仅在选择 API 时使用）")
        api_form = QFormLayout(api_group)
        self.api_base = QLineEdit()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_model = QLineEdit()
        self.api_timeout = QSpinBox()
        self.api_timeout.setRange(3, 120)
        self.api_timeout.setSuffix(" 秒")
        # token 消耗限制（B2 第 43 轮）：测试 API 之前就能把花费框死
        self.api_daily_budget = QSpinBox()
        self.api_daily_budget.setRange(0, 100000000)
        self.api_daily_budget.setSingleStep(10000)
        self.api_daily_budget.setSuffix(" token/天（0=不限）")
        self.api_max_quick = QSpinBox()
        self.api_max_quick.setRange(32, 512)
        self.api_max_quick.setSuffix(" token（第一段）")
        self.api_max_replies = QSpinBox()
        self.api_max_replies.setRange(64, 1024)
        self.api_max_replies.setSuffix(" token（三条回复）")
        api_form.addRow("Base URL：", self.api_base)
        api_form.addRow("API Key：", self.api_key)
        api_form.addRow("模型名：", self.api_model)
        api_form.addRow("超时：", self.api_timeout)
        api_form.addRow("每日上限：", self.api_daily_budget)
        api_form.addRow("单次上限：", self.api_max_quick)
        api_form.addRow("", self.api_max_replies)
        api_form.addRow(QLabel("隐私：API 模式只发送聊天文本，不发送截图、QQ 号、昵称、群名。\n"
                              "失败会自动回落本地模型。"))
        form.addRow(api_group)
        return page

    # ---------------- 页签：Prompt ----------------
    def _build_prompt_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        # ---- 第 79 轮：把"从 AI 粘贴导入"放在**最上面**（用户口径：懒人一眼就能找到）----
        import_group = QGroupBox("① 从 AI 粘贴导入整套提示词（最省事，推荐先看这里）")
        import_layout = QVBoxLayout(import_group)
        import_layout.addWidget(QLabel(
            "点「复制模板」→ 粘到网页版 AI（DeepSeek / 豆包等）→ 在最后一行「我的要求：」后面"
            "直接写下你想要的风格 → 把 AI 的回复**整段**粘到下面 → 点「解析并装入」。\n"
            "会新建一套配置并选中，点「保存」后生效；不用 JSON、不用下载文件、不用一个个改。"))
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("这套提示词用于："))
        self.import_target = QComboBox()
        # 用户口径：一般不会单独导入"自我分析"，默认就是**整套**（两套一起换）
        self.import_target.addItem("整套替换：分析对方 + 自我分析（推荐）", "both")
        self.import_target.addItem("只导入“分析对方”（危险度 / 回复建议 / 回复选项）", "other")
        self.import_target.setToolTip(
            "默认「整套替换」= 按复制模板生成的那份提示词里，危险度体系那 5 项装到"
            "“分析对方”，魅力体系那 3 项装到“自我分析”，两份整体风格各归各的。\n"
            "只写了对方那套（例如第三字段叫“压制力”）时，选“只导入分析对方”即可；"
            "自我那套会保持默认。")
        target_row.addWidget(self.import_target, 1)
        import_layout.addLayout(target_row)
        self.import_edit = QPlainTextEdit()
        self.import_edit.setPlaceholderText(
            "把 AI 的回复整段粘到这里即可（不用 JSON、不用改格式）。\n"
            "点「复制模板」会让它按**两套体系**输出，粘回来大概长这样"
            "（示例仅供看清格式；方括号里是面板显示名，AI 会自己起名）：\n"
            "\n"
            "===== 分析对方（危险度体系）=====\n"
            "意图[想干嘛]：怎么看对方真正想干什么，隐含意思也要算，最多 14 字\n"
            "情绪[心情]：2-4 个字的情绪词，别用报告腔\n"
            "危险度[火药味]：0-10 整数，0-2 日常、5-6 明确不满、9-10 翻脸\n"
            "回复建议[递一句]：一句能直接用的话，最多 30 字\n"
            "回复选项[三选一]：三条能直接发的，立场分开（认同/反调/吐槽）\n"
            "整体风格[对方气场]：整体语气与关注点\n"
            "\n"
            "===== 分析自己（魅力体系）=====\n"
            "魅力评分[心动值]：0-10 整数，越高越迷人；中规中矩 5 分、句尾带“喵”\n"
            "魅力评价[猫眼一瞥]：一句猫娘口吻的评价，最多 30 字、句尾带“喵”\n"
            "发言选项[猫爪三连]：三条我接下来发给对方的话，立场=继续展开/吐槽/终结、句尾带“喵”\n"
            "整体风格[猫娘气场]：像猫娘撒娇但不叫主人，句尾带“喵”")
        self.import_edit.setFixedHeight(120)
        import_layout.addWidget(self.import_edit)
        import_row = QHBoxLayout()
        self.copy_tpl_btn = QPushButton("复制模板")
        self.copy_tpl_btn.clicked.connect(self._copy_import_template)
        # 第 80 轮末（用户口径）：紧挨着加一个「设置模板」，点开一个文本窗口自己改模板
        self.edit_tpl_btn = QPushButton("设置模板")
        self.edit_tpl_btn.setToolTip("自己修改上面「复制模板」复制出去的那段说明（保存后立即生效）")
        self.edit_tpl_btn.clicked.connect(self._edit_import_template)
        self.import_btn = QPushButton("解析并装入")
        self.import_btn.clicked.connect(self._import_prompt_bundle)
        self.clear_import_btn = QPushButton("清空")
        self.clear_import_btn.clicked.connect(lambda: self.import_edit.setPlainText(""))
        import_row.addWidget(self.copy_tpl_btn)
        import_row.addWidget(self.edit_tpl_btn)
        import_row.addWidget(self.import_btn)
        import_row.addWidget(self.clear_import_btn)
        import_row.addStretch(1)
        import_layout.addLayout(import_row)
        self.import_status = QLabel("")
        self.import_status.setWordWrap(True)
        import_layout.addWidget(self.import_status)
        layout.addWidget(import_group)

        layout.addWidget(QLabel(
            "② 手动微调（可选）：左边是配置列表（内置“默认配置”和“猫娘配置”，可新建/删除）；"
            "右边是一套配置的字段设置：填名字 + 这段内容要什么风格。\n"
            "说明留空 = 用程序默认说明；JSON 结构、字段顺序、长度上限由程序固定，怎么写都不会崩。"))

        split = QHBoxLayout()
        list_group = QGroupBox("配置列表")
        list_layout = QVBoxLayout(list_group)
        self.profile_list = QListWidget()
        self.profile_list.setFixedWidth(220)
        self.profile_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.profile_list.currentRowChanged.connect(self._on_profile_selected)
        list_layout.addWidget(self.profile_list)
        list_row = QHBoxLayout()
        self.new_profile_btn = QPushButton("新建")
        self.new_profile_btn.clicked.connect(self._on_new_profile)
        self.rename_profile_btn = QPushButton("重命名")   # 第 79 轮（用户："导入的配置没法重命名"）
        self.rename_profile_btn.clicked.connect(self._on_rename_profile)
        self.del_profile_btn = QPushButton("删除")
        self.del_profile_btn.clicked.connect(self._on_delete_profile)
        list_row.addWidget(self.new_profile_btn)
        list_row.addWidget(self.rename_profile_btn)
        list_row.addWidget(self.del_profile_btn)
        list_layout.addLayout(list_row)
        list_layout.addWidget(QLabel("内置的两套不能删除；新建时会把\n当前右边的设置存成一套新配置。"))
        list_layout.addStretch(1)
        split.addWidget(list_group)

        right = QVBoxLayout()
        # ---- 角色包（第 81 轮末，用户口径："把角色包的接口放到其他字段的最上面，方便找"）----
        # 说明：这就是以前那句"全局分析风格"，现在改成**角色包**语义 ——
        # 它会被完整放进系统提示词的最前面（所有字段都按它写），输出前还会再贴一句提醒。
        role_group = QGroupBox("角色包（分析对方）—— 这套配置统一的说话风格")
        role_layout = QVBoxLayout(role_group)
        role_layout.addWidget(QLabel(
            "写这个角色**是谁、怎么说话**：身份口气、自称、句尾习惯、常用词、句子长短、"
            "关心/吐槽/生气时分别怎么说、绝对不能出现什么。建议 600-1000 字（约 800 字最好）。\n"
            "所有字段都按它写；字段说明里写“按角色包”就是引用它 —— 各字段只写"
            "“这个字段判断什么/输出什么/不要怎么写”。"))
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(DEFAULT_STYLE_PROMPT)
        self.prompt_edit.setFixedHeight(110)
        role_layout.addWidget(self.prompt_edit)
        self.role_count = QLabel("")
        role_layout.addWidget(self.role_count)
        role_row = QHBoxLayout()
        reset_prompt = QPushButton("还原默认角色包")
        reset_prompt.clicked.connect(
            lambda: self.prompt_edit.setPlainText(DEFAULT_STYLE_PROMPT))
        role_row.addWidget(reset_prompt)
        role_row.addStretch(1)
        role_layout.addLayout(role_row)
        right.addWidget(role_group)
        self.prompt_edit.textChanged.connect(
            lambda: self.role_count.setText(f"当前 {len(self.prompt_edit.toPlainText())} 字"))

        fields_group = QGroupBox("字段自定义（名称 + 风格/内容 prompt）")
        grid = QGridLayout(fields_group)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 2)
        grid.addWidget(QLabel("字段"), 0, 0)
        grid.addWidget(QLabel("显示名"), 0, 1)
        grid.addWidget(QLabel("这个字段要什么内容/风格（prompt）"), 0, 2)
        self.field_names = {}
        self.field_prompts = {}
        for row, (key, label, hint) in enumerate(FIELD_ROWS, start=1):
            name_edit = QLineEdit()
            name_edit.setPlaceholderText(DEFAULT_FIELD_NAMES[key])
            name_edit.setMaximumWidth(160)
            prompt_edit = QPlainTextEdit()
            prompt_edit.setPlaceholderText(DEFAULT_FIELD_PROMPTS[key])
            prompt_edit.setFixedHeight(64)
            tip = QLabel(label)
            tip.setToolTip(hint)
            grid.addWidget(tip, row, 0)
            grid.addWidget(name_edit, row, 1)
            grid.addWidget(prompt_edit, row, 2)
            self.field_names[key] = name_edit
            self.field_prompts[key] = prompt_edit
        field_reset = QPushButton("把左边的字段名与说明还原成“内置默认”（点保存后写入该配置）")
        field_reset.clicked.connect(self._reset_field_widgets)
        grid.addWidget(field_reset, len(FIELD_ROWS) + 1, 0, 1, 3)
        right.addWidget(fields_group)

        # ---- 用户画像（第 69 轮，默认留空）----
        right.addWidget(QLabel("用户画像（我的聊天习惯 / 口癖；留空 = 不启用）"))
        self.profile_edit = QPlainTextEdit()
        self.profile_edit.setPlaceholderText(
            "例如：\n"
            "· 我平时说话很短，常用“确实/是这样/笑死/麻了”，很少打完整句子；\n"
            "· 从来不叫对方全名，喜欢直接说事；\n"
            "· 会用“哈哈哈”或“?”代替句子，很少用感叹号；\n"
            "· 不爱用书面语和表情包，偶尔自嘲。\n"
            "（这段会作为【我（用户）的说话习惯】写进系统提示词，"
            "让回复建议/回复选项模仿你的口吻；留空则完全不进提示词）")
        self.profile_edit.setFixedHeight(110)
        right.addWidget(self.profile_edit)
        row2 = QHBoxLayout()
        clear_profile = QPushButton("清空用户画像")
        clear_profile.clicked.connect(lambda: self.profile_edit.setPlainText(""))
        row2.addWidget(clear_profile)
        row2.addStretch(1)
        right.addLayout(row2)

        # ---- 第 79 轮：自我分析（点自己发的气泡）----
        right.addWidget(QLabel(
            "自我分析（点**自己发的气泡**时用）：意图 / 情绪跟上面那套**完全一致**，"
            "下面三项单独设（默认就是“质量评分 / 消息点评 / 发言选项”）。"))
        self_group = QGroupBox("自我分析字段（名称 + 风格/内容 prompt）")
        sgrid = QGridLayout(self_group)
        sgrid.setColumnStretch(1, 1)
        sgrid.setColumnStretch(2, 2)
        sgrid.addWidget(QLabel("字段"), 0, 0)
        sgrid.addWidget(QLabel("显示名"), 0, 1)
        sgrid.addWidget(QLabel("这个字段要什么内容/风格（prompt）"), 0, 2)
        self.self_field_names = {}
        self.self_field_prompts = {}
        for row_i, (key, label, hint) in enumerate(SELF_FIELD_ROWS, start=1):
            self_name = QLineEdit()
            self_name.setPlaceholderText(DEFAULT_SELF_FIELD_NAMES[key])
            self_name.setMaximumWidth(160)
            self_prompt = QPlainTextEdit()
            self_prompt.setPlaceholderText(DEFAULT_SELF_FIELD_PROMPTS[key])
            self_prompt.setFixedHeight(64)
            self_tip = QLabel(label)
            self_tip.setToolTip(hint)
            sgrid.addWidget(self_tip, row_i, 0)
            sgrid.addWidget(self_name, row_i, 1)
            sgrid.addWidget(self_prompt, row_i, 2)
            self.self_field_names[key] = self_name
            self.self_field_prompts[key] = self_prompt
        self_reset = QPushButton("把自我分析的三项还原成“内置默认”（点保存后生效）")
        self_reset.clicked.connect(self._reset_self_widgets)
        sgrid.addWidget(self_reset, len(SELF_FIELD_ROWS) + 1, 0, 1, 3)
        right.addWidget(self_group)

        # 第 82 轮末（用户："角色包只要一份就够了，自我分析和对方分析共用一套角色包"）：
        # 自我分析不再单独设角色包 —— 这格留着**只是为了兼容老配置**（老存档里存过的值
        # 仍然能被读到），界面上不再显示。
        right.addWidget(QLabel("自我分析共用上面那一份角色包（不再单独设置）。"))
        self.self_style_edit = QPlainTextEdit()
        self.self_style_edit.setPlaceholderText(DEFAULT_SELF_STYLE_PROMPT)
        self.self_style_edit.setFixedHeight(72)
        self.self_style_edit.setVisible(False)          # 兼容用：不显示，但仍在存/读逻辑里
        right.addWidget(self.self_style_edit)
        self_style_row = QHBoxLayout()
        reset_self_style = QPushButton("还原默认（自我分析语气）")
        reset_self_style.clicked.connect(
            lambda: self.self_style_edit.setPlainText(DEFAULT_SELF_STYLE_PROMPT))
        self_style_row.addWidget(reset_self_style)
        self_style_row.addStretch(1)
        right.addLayout(self_style_row)

        split.addLayout(right, 1)
        layout.addLayout(split, 1)
        layout.addWidget(QLabel("提示：留空的说明框会用默认内容；保存后立刻生效（下一段分析就用新 prompt）。"))
        # 内容较高，套一层滚动区，小屏幕上也能完整看到
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _reset_field_widgets(self) -> None:
        for key, _label, _hint in FIELD_ROWS:
            self.field_names[key].setText("")
            self.field_prompts[key].setPlainText("")

    def _reset_self_widgets(self) -> None:
        """自我分析那三项还原成"留空"（= 用内置默认说明与默认名字）。"""
        for key, _label, _hint in SELF_FIELD_ROWS:
            self.self_field_names[key].setText("")
            self.self_field_prompts[key].setPlainText("")
        self.self_style_edit.setPlainText("")

    # ---------------- 配置列表 ----------------
    def _reload_profile_list(self, select: str = "") -> None:
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for entry in self._profiles:
            self.profile_list.addItem(str(entry.get("name")))
        self.profile_list.blockSignals(False)
        names = [str(entry.get("name")) for entry in self._profiles]
        if select and select in names:
            index = names.index(select)
        else:
            index = next((i for i, e in enumerate(self._profiles) if e.get("builtin")), 0)
        self.profile_list.setCurrentRow(index)
        self._on_profile_selected(index)

    def _current_profile(self) -> Optional[dict]:
        row = self.profile_list.currentRow()
        if 0 <= row < len(self._profiles):
            return self._profiles[row]
        return None

    def _profile_by_name(self, name: str) -> Optional[dict]:
        if not name:
            return None
        return next((e for e in self._profiles if str(e.get("name") or "") == name), None)

    def _store_widgets_into(self, entry: Optional[dict]) -> None:
        """把界面上的字段设置写进**指定**的那套配置。

        第 79 轮修复（用户："用户切换配置时原先的配置会被切换到的配置覆盖"）：
        原来只有 `_store_into_current()`（写进"列表当前选中的那套"），而切换配置时
        `currentRowChanged` 已经先变了 → 界面里装的还是**上一个配置**的内容，
        一切换就丢，切回来看到的是旧的/别人的那套。
        现在把"写回指定配置"抽出来，切换前先写回**离开的那套**。
        """
        if entry is None:
            return
        for key, _label, _hint in FIELD_ROWS:
            entry[f"{key}_name"] = self.field_names[key].text().strip()
            entry[f"{key}_prompt"] = self.field_prompts[key].toPlainText().strip()
        # 第 79 轮：自我分析那三项 + 两份"整体风格"**也属于这套配置**
        # （否则切配置时全局风格会互相串，用户实测反馈过）
        for key, _label, _hint in SELF_FIELD_ROWS:
            entry[f"self_{key}_name"] = self.self_field_names[key].text().strip()
            entry[f"self_{key}_prompt"] = self.self_field_prompts[key].toPlainText().strip()
        entry["style_prompt"] = self.prompt_edit.toPlainText().strip()
        entry["self_style_prompt"] = self.self_style_edit.toPlainText().strip()
        # 第 75 轮（"保存逻辑有点不舒服"的真因之一）：内置两套配置在 `load_config()` 里
        # **每次启动都会被代码版本覆盖**，于是"我改了默认配置的字段说明，下次启动又变回去了"。
        # 这里打个标记：只要用户改得和代码内置版不一样，就记 `edited=True`，
        # 加载时对有标记的内置配置**保留用户的版本**（没改过的仍会自动跟随代码更新）。
        if entry.get("builtin"):
            defaults = {str(e.get("name")): e for e in default_profiles()}
            code_version = defaults.get(str(entry.get("name")))
            if code_version is not None:
                # 第 79 轮：只比较"用户真正填过的键"（非空）—— 否则代码给内置配置补新字段
                # （例如猫娘配置后来才加的自我分析那套）会被误判成"用户改过"，
                # 于是新字段的默认值永远生效不了（用户反馈"猫娘配置自我分析还是不带喵"）。
                changed = False
                for key in ALL_PROFILE_FIELDS:
                    mine = str(entry.get(key) or "").strip()
                    theirs = str(code_version.get(key) or "").strip()
                    if mine and mine != theirs:
                        changed = True
                        break
                entry["edited"] = changed

    def _on_profile_selected(self, row: int) -> None:
        if not (0 <= row < len(self._profiles)):
            return
        entry = self._profiles[row]
        # 内置两套（默认配置/猫娘配置）不允许删除 → 直接把按钮灰掉，省得点了才被拒
        self.del_profile_btn.setEnabled(not entry.get("builtin"))
        # 第 79 轮：**切换前先把离开的那套配置写回去**（否则切换等于丢掉刚才的改动）
        previous = self._profile_by_name(str(self._loaded_profile or ""))
        if previous is not None and previous is not entry:
            self._store_widgets_into(previous)
        for key, _label, _hint in FIELD_ROWS:
            self.field_names[key].setText(str(entry.get(f"{key}_name", "") or ""))
            self.field_prompts[key].setPlainText(str(entry.get(f"{key}_prompt", "") or ""))
        for key, _label, _hint in SELF_FIELD_ROWS:
            self.self_field_names[key].setText(str(entry.get(f"self_{key}_name", "") or ""))
            self.self_field_prompts[key].setPlainText(str(entry.get(f"self_{key}_prompt", "") or ""))
        self.prompt_edit.setPlainText(str(entry.get("style_prompt") or "")
                                      or DEFAULT_STYLE_PROMPT)
        self.self_style_edit.setPlainText(str(entry.get("self_style_prompt") or "")
                                          or DEFAULT_SELF_STYLE_PROMPT)
        self._loaded_profile = str(entry.get("name") or "")

    def _store_into_current(self) -> None:
        self._store_widgets_into(self._current_profile())

    # ---------------- 第 79 轮：从 AI 文本导入整套提示词 ----------------
    def _copy_import_template(self) -> None:
        """把"该让 AI 怎么回答"的模板复制到剪贴板（不含 JSON，用户粘到网页 AI 即可）。"""
        try:
            # 第 80 轮末：优先用「设置模板」里改过的那份（留空 = 内置默认）
            QApplication.clipboard().setText(effective_template(self.cfg))
            self.import_status.setText(
                "模板已复制 → 粘到网页版 AI（如 DeepSeek）里，末尾写上你要的风格，"
                "再把它的回复整段粘回上面的框。")
        except Exception as exc:
            self.import_status.setText(f"复制模板失败：{exc}")

    def _edit_import_template(self) -> None:
        """「设置模板」：打开一个文本窗口，直接编辑"复制模板"复制出去的那份说明。

        点保存 = 立刻写进 config.json（`copy_template`），下次点「复制模板」就是这一份；
        清空后保存 = 回到代码里的内置默认模板。
        """
        dialog = TemplateEditDialog(self, current=effective_template(self.cfg),
                                    is_custom=bool(
                                        str(getattr(self.cfg, "copy_template", "") or "").strip()))
        if dialog.exec() != QDialog.Accepted:
            return
        text = dialog.template_text().strip()
        try:
            self.cfg.copy_template = text
            save_config(self.cfg)          # 独立小窗口：保存即落盘（不等主面板的"保存"）
            self.import_status.setText(
                "✅ 模板已保存，点「复制模板」复制的就是这一份。"
                if text else "✅ 已清空自定义模板 → 回到内置默认模板。")
            logger.info("复制模板已更新（%d 字，自定义=%s）", len(text), bool(text))
        except Exception as exc:
            self.import_status.setText(f"❌ 模板保存失败：{type(exc).__name__}: {exc}")

    def _import_prompt_bundle(self) -> None:
        """解析粘贴进来的文本 → **新建一套配置**并选中（点保存才落盘）。"""
        raw = self.import_edit.toPlainText()
        fields, warnings, suggested = parse_prompt_bundle(raw)
        if not fields:
            self.import_status.setText("❌ " + (warnings[0] if warnings else "没识别出内容"))
            return
        # 配置名：优先用文本第一行的标题，否则自动编号
        name = (suggested or "").strip()
        existing = {str(entry.get("name")) for entry in self._profiles}
        if not name or name in existing:
            index = 1
            while f"导入配置{index}" in existing:
                index += 1
            name = f"导入配置{index}"
        entry = profile_entry(name, builtin=False)
        # 第 79 轮（用户口径修正）：默认"**整套替换**" —— 一次把危险度体系（分析对方）
        # 和魅力体系（自我分析）都换掉；也保留"只导入分析对方"。
        #   · 意图/情绪两边共用 → 只写一份；
        #   · 没写的那一侧保持"默认配置"（= 内置默认说明），两边不串；
        #   · **用户画像绝不从 AI 文本里取**（AI 会把模板里的解释照抄过来，用户口径：自己写）。
        target = str(self.import_target.currentData() or "both")
        base = self._profile_by_name(DEFAULT_PROFILE_NAME)
        if base is not None:
            for key in ALL_PROFILE_FIELDS:
                entry[key] = str(base.get(key) or "")
        mapped = map_bundle_to_keys(fields, target)
        for key, value in mapped.items():
            if key == "user_profile":
                continue                     # 绝不由 AI 填写
            entry[key] = value
        self._profiles.append(entry)
        self._reload_profile_list(select=name)      # 选中它 → 界面自动刷成这套
        self.del_profile_btn.setEnabled(True)
        filled_other = [name_zh for key, name_zh in
                        (("danger", "危险度"), ("suggestion", "回复建议"), ("option", "回复选项"))
                        if mapped.get(f"{key}_prompt")]
        filled_self = [name_zh for key, name_zh in
                       (("danger", "魅力评分"), ("suggestion", "魅力评价"), ("option", "发言选项"))
                       if mapped.get(f"self_{key}_prompt")]
        summary = "；".join(
            part for part in (
                ("分析对方：" + "、".join(filled_other)) if filled_other else "",
                ("自我分析：" + "、".join(filled_self)) if filled_self else "",
            ) if part) or "（没识别到正文）"
        notes = "；".join(w for w in warnings[1:2] if w)
        self.import_status.setText(
            f"✅ 已装入新配置「{name}」：{summary}。用户画像请自己填；点「保存」后生效。"
            + (f"（{notes}）" if notes else ""))
        logger.info("从文本导入提示词：配置=%s，目标=%s，识别字段 %d 项",
                    name, "自我分析" if target == "self" else "分析对方", len(fields))

    def _on_new_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "新建配置", "新配置名字（例如：女仆配置）")
        name = (name or "").strip()
        if not ok or not name:
            return
        if any(str(entry.get("name")) == name for entry in self._profiles):
            QMessageBox.warning(self, "重名", f"已经有一套叫“{name}”的配置了。")
            return
        entry = profile_entry(name, builtin=False)
        # 第 79 轮（用户口径）：新建配置**默认以"默认配置"为模板**，
        # 而不是以"当前界面里正在看的那套"为模板（否则从猫娘配置点新建，
        # 新配置会莫名其妙继承猫娘那套）。
        base = self._profile_by_name(DEFAULT_PROFILE_NAME)
        if base is not None:
            for key in ALL_PROFILE_FIELDS:
                entry[key] = str(base.get(key) or "")
        else:
            self._store_widgets_into(entry)
        self._profiles.append(entry)
        self._reload_profile_list(select=name)
        self.del_profile_btn.setEnabled(True)
        logger.info("新建配置：%s（共 %d 套）", name, len(self._profiles))

    def _on_rename_profile(self) -> None:
        """给当前这套配置改名（第 79 轮：用户反馈"导入的配置没法重命名"）。

        内置两套也允许改名吗？——不允许：内置名是代码里认的（"默认配置"/"猫娘配置"），
        改名会让"新建时以默认配置为模板"这类逻辑失效，提示用户另存为新的即可。
        """
        entry = self._current_profile()
        if entry is None:
            return
        old = str(entry.get("name") or "")
        if entry.get("builtin"):
            QMessageBox.information(self, "不能重命名", f"“{old}”是内置配置，不能改名。\n"
                                                     "可以点「新建」建一套自己的再改。")
            return
        name, ok = QInputDialog.getText(self, "重命名配置", "新名字：", text=old)
        name = (name or "").strip()
        if not ok or not name or name == old:
            return
        if any(str(item.get("name")) == name for item in self._profiles):
            QMessageBox.warning(self, "重名", f"已经有一套叫“{name}”的配置了。")
            return
        entry["name"] = name
        if self._loaded_profile == old:
            self._loaded_profile = name
        self._reload_profile_list(select=name)
        logger.info("重命名配置：%s → %s", old, name)

    def _on_delete_profile(self) -> None:
        """删除当前这套配置（内置两套不允许删）。"""
        entry = self._current_profile()
        if entry is None:
            return
        name = str(entry.get("name"))
        if entry.get("builtin"):
            QMessageBox.information(self, "不能删除", f"“{name}”是内置配置，不能删除。\n"
                                                     "想恢复原样可以点字段区的“还原成内置默认”。")
            return
        if QMessageBox.question(self, "删除配置", f"确定删除“{name}”这套配置？") != QMessageBox.Yes:
            return
        self._profiles = [item for item in self._profiles if item is not entry]
        self._reload_profile_list()
        logger.info("删除配置：%s（剩 %d 套）", name, len(self._profiles))

    # ---------------- 页签：显示与行为 ----------------
    def _build_display_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.prefix_check = QCheckBox("在三条回复前显示 [风格] 前缀")
        self.only_other_check = QCheckBox("只分析对方(左)消息，不分析自己发的")
        self.only_other_check.setToolTip(
            "第 79 轮起默认**不勾选**：点自己发的气泡也会分析，但换一套字段\n"
            "（危险度→质量评分、回复建议→消息点评、回复选项→发言选项）。\n"
            "勾上就退回旧行为：只有对方的消息能点。")
        self.allow_click_check = QCheckBox(
            "一键填入时允许自动点击输入框（关掉＝只发 Ctrl+V，更保守）")
        self.allow_click_check.setToolTip(
            "关掉后：点选项只把文本粘贴到 QQ 输入框（需要输入框已有焦点），\n"
            "不会再注入任何鼠标事件。若朋友机器上出现“点选项只复制”或“鼠标卡住”，\n"
            "或者 QQ 是“以管理员身份运行”，就把这项关掉。")
        self.dot_size = QSpinBox()
        self.dot_size.setRange(int(self.cfg.ui.dot_size_min_px), 48)
        self.dot_size.setSuffix(" px")
        self.gear_check = QCheckBox("鼠标移到状态灯上时显示齿轮（点击打开设置）")
        self.filter_names_check = QCheckBox("过滤群聊昵称行（1v1 建议关闭：实测会误删相邻短消息）")
        self.theme_combo = QComboBox()
        for name in theme_mod.theme_names():
            self.theme_combo.addItem(theme_mod.theme_label(name), name)
        self.trigger_combo = QComboBox()
        self.trigger_combo.addItem("点击消息弹出（推荐，省性能）", "click")
        self.trigger_combo.addItem("鼠标移到就分析（旧行为）", "hover")
        form.addRow(self.prefix_check)
        form.addRow(self.only_other_check)
        form.addRow(self.allow_click_check)
        form.addRow("触发方式：", self.trigger_combo)
        form.addRow("界面风格：", self.theme_combo)
        form.addRow("状态灯大小：", self.dot_size)
        form.addRow(self.gear_check)
        form.addRow(self.filter_names_check)
        form.addRow(QLabel("界面风格只改浮窗配色（经典深色 / 樱花 galgame），不改布局与交互。"))
        form.addRow(QLabel("风格前缀示例：[稳当] / [简单] / [轻松]（模型给出短标签时自动映射）"))
        return page

    # ---------------- 读写 ----------------
    def _load_into_widgets(self) -> None:
        self._load_from(self.cfg)

    def _load_from(self, cfg: Config) -> None:
        """把某份配置刷进界面（`self.cfg` 或"还原默认值"的临时配置都能用）。"""
        index = self.backend_combo.findData(cfg.analyzer.backend)
        self.backend_combo.setCurrentIndex(max(0, index))
        self.api_base.setText(cfg.api.base_url)
        self.api_key.setText(cfg.api.api_key)
        self.api_model.setText(cfg.api.model)
        self.api_timeout.setValue(int(cfg.api.timeout_s))
        self.api_daily_budget.setValue(int(getattr(cfg.api, "daily_token_budget", 0)))
        self.api_max_quick.setValue(int(getattr(cfg.api, "max_tokens_quick", 96)))
        self.api_max_replies.setValue(int(getattr(cfg.api, "max_tokens_replies", 220)))
        self.prompt_edit.setPlainText(cfg.analysis_style_prompt or DEFAULT_STYLE_PROMPT)
        # 第 79 轮：自我分析（自己的消息）那套字段 + 语气
        self_cfg = getattr(cfg, "self_fields", None)
        for key, _label, _hint in SELF_FIELD_ROWS:
            self.self_field_names[key].setText(
                str(getattr(self_cfg, f"{key}_name", "") or ""))
            self.self_field_prompts[key].setPlainText(
                str(getattr(self_cfg, f"{key}_prompt", "") or ""))
        self.self_style_edit.setPlainText(
            (getattr(cfg, "self_analysis_style_prompt", "") or "").strip()
            or DEFAULT_SELF_STYLE_PROMPT)
        self.profile_edit.setPlainText(getattr(cfg.fields, "user_profile", "") or "")
        self.prefix_check.setChecked(cfg.ui.show_reply_style_prefix)
        self.only_other_check.setChecked(cfg.ui.only_other_party)
        self.allow_click_check.setChecked(bool(getattr(cfg.fill, "allow_mouse_click", True)))
        self.dot_size.setValue(int(cfg.ui.dot_size_px))
        index = self.theme_combo.findData(getattr(cfg.ui, "theme", "classic"))
        self.theme_combo.setCurrentIndex(max(0, index))
        index = self.trigger_combo.findData(getattr(cfg.ui, "trigger_mode", "click"))
        self.trigger_combo.setCurrentIndex(max(0, index))
        self.gear_check.setChecked(cfg.ui.dot_hover_gear)
        self.filter_names_check.setChecked(cfg.uia.filter_sender_names)
        # 配置列表：复制一份到界面里编辑，保存时再写回（取消 = 不落盘）
        self._profiles = [dict(entry) for entry in (cfg.fields_profiles or default_profiles())]
        self._reload_profile_list(select=cfg.active_profile or "")

        # 第 81 轮末（用户口径："设置面板的文本不会按宽度上限自动换行显示，请调整一下"）：
        # 面板里的**长说明**（提示、字段说明那几列）默认不换行 → 超出宽度就被裁掉；
        # 但不能无脑给所有 QLabel 开折行：表单左列那些短标签（"Base URL：""模型名："）一旦
        # 允许折行 + 最小宽度 1，就会被压成"一个词一行"的竖排（第一版就是这么翻车的）。
        # 所以只处理文本较长（≥24 字）的标签。
        for label in self.findChildren(QLabel):
            if len(label.text()) >= 24:
                label.setWordWrap(True)
                label.setMinimumWidth(1)

    def on_save(self) -> None:
        cfg = self.cfg
        if self._pending_restore is not None:
            # 用户点过"一键还原"：此刻才真正把默认值落到运行中的 cfg（取消则什么都没发生）
            fresh = self._pending_restore
            cfg.analyzer, cfg.api, cfg.ui = fresh.analyzer, fresh.api, fresh.ui
            cfg.fields, cfg.field_limits = fresh.fields, fresh.field_limits
            cfg.self_fields = fresh.self_fields            # 第 79 轮：自我分析字段一起还原
            cfg.self_analysis_style_prompt = fresh.self_analysis_style_prompt
            cfg.fields_profiles = [dict(e) for e in fresh.fields_profiles]
            cfg.active_profile = fresh.active_profile
            self._profiles = [dict(e) for e in fresh.fields_profiles]
            self._loaded_profile = fresh.active_profile
            self._pending_restore = None
        cfg.analyzer.backend = self.backend_combo.currentData() or "local"
        cfg.api.base_url = self.api_base.text().strip() or cfg.api.base_url
        cfg.api.api_key = self.api_key.text().strip()
        cfg.api.model = self.api_model.text().strip() or cfg.api.model
        cfg.api.timeout_s = float(self.api_timeout.value())
        cfg.api.daily_token_budget = int(self.api_daily_budget.value())
        cfg.api.max_tokens_quick = int(self.api_max_quick.value())
        cfg.api.max_tokens_replies = int(self.api_max_replies.value())
        cfg.analysis_style_prompt = (self.prompt_edit.toPlainText().strip()
                                     or DEFAULT_STYLE_PROMPT)
        # 第 79 轮：自我分析的字段名/说明 + 整体语气（留空 = 用内置默认）
        for key, _label, _hint in SELF_FIELD_ROWS:
            setattr(cfg.self_fields, f"{key}_name",
                    self.self_field_names[key].text().strip())
            setattr(cfg.self_fields, f"{key}_prompt",
                    self.self_field_prompts[key].toPlainText().strip())
        cfg.self_analysis_style_prompt = (self.self_style_edit.toPlainText().strip()
                                         or DEFAULT_SELF_STYLE_PROMPT)
        # 用户画像：默认空（空 = 完全不进提示词）；填了就让模型模仿我的口吻
        cfg.fields.user_profile = self.profile_edit.toPlainText().strip()
        cfg.ui.show_reply_style_prefix = self.prefix_check.isChecked()
        cfg.ui.only_other_party = self.only_other_check.isChecked()
        cfg.fill.allow_mouse_click = self.allow_click_check.isChecked()
        cfg.ui.dot_size_px = max(int(cfg.ui.dot_size_min_px), int(self.dot_size.value()))
        cfg.ui.theme = self.theme_combo.currentData() or "classic"
        cfg.ui.trigger_mode = self.trigger_combo.currentData() or "click"
        cfg.ui.dot_hover_gear = self.gear_check.isChecked()
        cfg.uia.filter_sender_names = self.filter_names_check.isChecked()
        # 把界面上的字段设置写回"当前选中的那套配置"，并把该套配置设为生效配置
        self._store_into_current()
        cfg.fields_profiles = self._profiles
        if not apply_profile(cfg, self._loaded_profile or ""):
            logger.warning("生效配置 %r 不存在，保留原字段设置", self._loaded_profile)
        cfg.fields.danger_cap_rule = False      # 用户口径：模型够聪明，不再用硬封顶规则
        if cfg.analyzer.backend == "api" and not cfg.api.api_key:
            QMessageBox.warning(self, "提示", "选择 API 后端但没有填 API Key，已改回本地部署。")
            cfg.analyzer.backend = "local"
        path = save_config(cfg)
        logger.info("设置已保存：backend=%s prefix=%s only_other=%s dot=%s profile=%s → %s",
                    cfg.analyzer.backend, cfg.ui.show_reply_style_prefix,
                    cfg.ui.only_other_party, cfg.ui.dot_size_px,
                    cfg.active_profile, path)
        self.accept()

    def on_restore_defaults(self) -> None:
        if QMessageBox.question(self, "还原默认设置",
                                "确定把所有设置恢复为默认值？\n"
                                "（Prompt、后端、API 参数、显示选项都会重置，"
                                "已保存的 API Key 会被清空）") != QMessageBox.Yes:
            return
        # 第 75 轮（用户："设置保存的逻辑似乎有一点小问题"）：
        # 旧实现直接 `restore_defaults()` —— 那会**立刻写盘**，于是"还原完再点取消"也已经被保存了，
        # 而且它同时改掉了正在运行的 cfg。现在改成：只把"默认值"装进待保存状态 + 刷新界面，
        # **按保存才写盘，按取消什么都没有发生**。
        fresh = app_config.Config()
        fresh.paths = self.cfg.paths            # 保留工作区/模型路径，别把路径也重置掉
        fresh.analysis_style_prompt = DEFAULT_STYLE_PROMPT
        self._pending_restore = fresh
        self._load_from(fresh)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    dialog = SettingsDialog(load_config())
    dialog.show()
    sys.exit(app.exec())
