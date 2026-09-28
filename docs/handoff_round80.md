# 交接：ChatAssistant（D:\QQChatAssistant）—— 第 80 轮结束时的状态

> 用途：换新对话继续时，把这份文件 + 一句话需求贴给助手即可；SPEC.md 仍是**唯一权威**
> （逐轮改动记录在 SPEC 第 320-344 条）。

## 0. 项目与硬规则
- 工作区根目录：`D:\QQChatAssistant`（**一切文件/缓存必须写在工作区内**，config.py 已把
  TMP/TEMP/HF_HOME/PIP_CACHE_DIR 等钉死在工作区）。
- 后端：本地 `llama-cpp-python 0.3.22`（不可升 0.3.35：要求 AVX-512，本机 SIGILL）。
- 模型：默认 `models/qwen3-4b-instruct-2507-q4_k_m.gguf`；另有
  `qwen3.5-4b-q4_k_m.gguf`（**备选，已不再是默认** —— 第 347 条用户实测"3 明显更对路"）
  与 `lfm2.5-2.6b-q4_k_m.gguf`（未用，不删）。
- 安全红线：只读 UIA；**禁止全局鼠标钩子**；一键填入只发 Ctrl+V、绝不回车；
  破坏性操作（删文件/删代码/换模型/量化）**必须先问用户**。
- 打包：`cache\py311\python.exe tools\build_exe.py --model-name <gguf> --zip`
  （窗口 `tools/quit_app.py` 优雅退出；出包前先关掉正在跑的实例，否则文件占用会打包失败）。

## 1. 当前功能状态（可用）
- 读消息：UIA 只读，消息列表容器 + 左右气泡判定；`msg_key = 会话 + 侧别 + 归一化文本`
  （同一文本左右各一条不会再串）。
- 约束解码：**自算掩码**（`core/fast_mask.py`）为主线，解码 ~60 token/s；
  GBNF 只在配置 `analyzer.constrain="gbnf"` 时手动启用（**永远不自动回退**）。
- 两段式：第一段 意图/情绪/危险度（或魅力）/回复建议 → 第二段 三条回复选项/发言选项。
- 面板：分析面板 + 选项条（悬停小圆点、选中"已选"无、点击填入输入框）、上下文面板、
  状态灯（=设置入口）、首次启动提示；樱花(galgame) 为默认主题。
- 提示词：内置"默认配置/猫娘配置"两套 + 用户自建配置；设置面板可改名/改说明/粘贴导入整套。
- 上下文池：只保留一个连续区段，上限 120 条（超过自动清多余部分）。

## 2. 本轮（第 80 轮）已经做掉、且已验证的事
1. **提速**：把 JSON 约束从 GBNF 换成"自算掩码"（GBNF 每步要给 15-25 万词表算掩码，
   解码 28 → 60+ token/s）；配套 `analyzer.constrain` 开关、装载自检、状态显示。
   踩坑：**自检里写死的"超限样例"会随上限调整而误判** → 现在按程序槽上限推导；
   且"自检不过/连续解析失败"**都不再切 GBNF**（只记 ERROR 日志）。
2. **模型**：Qwen3.5-4B 接回并出包（`dist/ChatAssistant-2026-09-26.zip`，3.33GB，含 3.5）。
3. **两个 bug**（用户实测）：① 自我分析的建议会抄提示词内容（长字段名 + "字段名：说明"
   列表形状）→ 提示词结构改成"JSON 键名 + 编号要求"、显示名不进提示词；
   ② 同文本消息点错条 → `msg_key` 加侧别 + 按坐标取最近的一条。
4. **选项"少一两条"**：根因是模型把两条写成同一句（重复被丢弃）→ `repeat_penalty` 1.0→1.1
   （实测第二段 2/30 → 0/70）。
5. **发言选项套路化**：根因是我删掉的"方向示例句"（"周六下午两点别迟到…"）原来是
   短句锚点，模型照抄 → 删示例 + `repeat_penalty` 后不再出现；代价是选项写得更长。
6. **缓存失效**：导入/切换配置后，旧消息仍显示旧配置结果 → 新增
   `_analysis_signature()`，设置关闭时签名变了就清空全部结果缓存。
7. **上限**：`intent` 14→15 字（用户口径）；情绪上限 10 字（曾误改 6 已回退）；
   掩码槽与 `config.FIELD_LIMITS` **必须一致**（有用例 G 钉住）。

## 3. 正在进行 / 下一步
- **复制模板**（`core/prompt_import.build_template()`）**已按"三视角"重写完成**（第 345 条）：① 给生成 AI 只给"要点+硬指标"并要求"别照抄、展开成 2-4 句、写完自检"；
  ② 每条说明必须含 写法/口吻与口癖（具体到句尾·自称·示例词）/硬指标/不要怎么写 + 一个
  「原话 → 你要写出来的样子」微型示例；③ 要求写清"三条立场说法要拉开、别固定同一批开头"
  并给一条猫娘配置的合格说明当格式样板。模板 1261 字；
  验证 `tmp/check_template_import.py`：18 字段全识别落位；`test_prompt_import` 11/11。

### 第 80 轮续（本轮已做完，见 SPEC 第 346 条）
- **刷新 bug 的真凶**：不是"参数被提前清"，而是第 42 轮加的"把上一版三条**原文**列进提示词
  要求避开"—— 4B 会照抄那三行（`tmp/probe_refresh2.py` 的 C 情景与基线 3/3 重合，还抄出过
  半截 JSON）。现已删掉这段注入，刷新只靠"新种子 + 0.9 温度"（修复后探针：基线 vs 两次刷新
  **0/3** 重合）。一次性参数的清理也从主线程 `on_analysis_ready()` 挪到工作线程
  （`refresh_snapshot/refresh_release`），否则两段式的第一段结果会把第二段的温度退回 0.2。
- **复制模板**：口癖改成条件式（只有风格自带或用户点名才写，且必须写"自然带上、不必每句都带"），
  并禁止"每句/每条/每个字段都带同一个口头禅"；1555 字。
- 回归：**24 个测试全绿**（新增 `test_refresh_params`）。
- 待用户决定：是否删除 GBNF 相关代码（现为手动应急开关，不参与自动路径）。
- 未打包：模板改完、用户测试满意后再 `--zip` 出包。

## 4. 常用命令
```powershell
# 全量回归（23 项）
cd D:\QQChatAssistant; $tests=@('test_fast_mask','test_prompt_import','test_click_retry','test_prompt_echo','test_self_fields','test_dup_text_click','test_blank_click','test_ui_polish','test_diagnose','test_profile_switch','test_click_at','test_click_dedup','test_first_run_hint','test_fill_once','test_self_panel','test_container_resolve','test_pool_merge','test_stance_prompt','test_config_atomic','test_user_profile','test_ctx_panel_68','test_dot_drag_freeze','test_startup_hidden','test_refresh_params','test_template_edit'); foreach($t in $tests){ cache\py311\python.exe "tools\$t.py" 2>&1 | Select-Object -Last 1 }
# 重启开发版
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process cache\py311\python.exe -ArgumentList 'main.py' -WorkingDirectory D:\QQChatAssistant -WindowStyle Hidden
```
