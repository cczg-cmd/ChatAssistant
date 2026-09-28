# ChatAssistant（QQ 聊天分析助手）

以**只读旁观者**的方式读取 QQ 会话窗口里的消息，用**本地模型**（默认）或你自己指定的 API，
在消息旁边浮出「意图 / 情绪 / 危险度 / 回复建议 / 三条回复选项」，点一下选项就填进输入框。

> ⚠️ **先读安全边界**：只读 Windows UIA 控件树与窗口坐标；**不注入、不修改 QQ、不读内存与数据库、
> 不自动发送消息**；一键填入只在 QQ 前台时发一次 Ctrl+V；不使用任何全局输入钩子；
> 所有分析默认在本机用本地模型完成（除非你自己切到 API）。
> **仅供个人学习与自用，请遵守 QQ 用户协议，使用风险自负。**

---

## 功能

- **点消息气泡就分析**：再点一次 / 点空白 / 滚动聊天区 → 收起面板
- **分析内容**：意图（言外之意）、情绪、危险度（或自己的「魅力评分」）、回复建议、三条回复选项
- **点选项一键填入**：把文本粘进 QQ 输入框，**绝不回车**；失败时退化为只复制到剪贴板
- **上下文联系**：自动记录当前会话的消息池（最多取最近 5 条做上下文），支持同一会话内滚动查看
- **角色包 / 自定义配置**：一套配置 = 一份「角色包」（人设与说话风格）+ 每个字段的说明；
  内置「默认配置」「猫娘配置」，也可以**把 AI 生成的提示词整段粘贴进来**自动装好一套
- **两段式 + 流式上屏**：第一段先出意图/情绪/危险度/建议（约 1 秒出标题），第二段补三条选项
- **本地 / API 双后端**：本地默认（llama.cpp + GGUF，GPU 加速）；也可接任意 OpenAI 兼容接口
  （带每日 token 上限与单次上限，防误花）
- **界面**：状态灯（聊天区右上角，悬停变齿轮=设置入口）、上下文历史面板、托盘菜单、
  两套主题（经典深色 / 樱花 galgame）

---

## 下载与安装

程序需要 **Windows 10 19041 或更高版本**，以及正在运行的 **QQ（PC 版）**。
模型不随源码仓库分发，需要单独下载一次（见下一节）。

### 方式 A1：免安装包（不含模型，约 0.8 GB）

1. 到本仓库的 **Releases** 页面下载 `ChatAssistant-<日期>.zip`（**不含模型**）。
2. 解压到**任意你自己可写的目录**（例如 `D:\ChatAssistant`）。
   ⚠️ 不要放进 `C:\Program Files` 这类需要管理员权限的目录 —— 程序会把自己的 `config.json`、
   `logs\`、`models\` 写在**自己所在目录**里。
3. 下载模型（见下一节），把它放进解压目录里的 `models\`（没有这个文件夹就新建一个），
   文件名保持 `qwen3-4b-instruct-2507-q4_k_m.gguf`。
4. 双击 `ChatAssistant.exe`。
   - 第一次启动会弹 Windows SmartScreen 警告（程序没有代码签名）→「更多信息」→「仍要运行」。
   - 打开一个 QQ 会话窗口，右上角会出现状态灯；点消息气泡即可看到分析。
   - 若日志里出现 `模型文件不存在`，就是第 3 步没放对位置（见「常见问题」）。

### 方式 A2：免安装包（含模型，分 3 卷，约 3.0 GB）

不想自己下模型就用这个 —— 包里已经把模型放好了：

1. 从 **Releases** 下载**全部三个**分卷：
   `ChatAssistant-1.0.0-with-model.7z.001` / `.002` / `.003`
2. 用 [7-Zip](https://www.7-zip.org/) 解压 **`.001`** 那一个（三个卷要放在同一个文件夹里，
   7-Zip 会自动把三卷拼起来）。
3. 解压出来是一个 `ChatAssistant\` 文件夹，双击里面的 `ChatAssistant.exe` 即可
   （在这个文件夹里读的是自带的模型，不需要再放模型）。

> 也可以从 Release 里下载 `ChatAssistant-<日期>-with-model.zip`（如果该版本提供了的话），
> 那是**不分卷**的整包，解压即用。

### 方式 B：从源码运行（开发者）

```powershell
git clone https://github.com/cczg-cmd/ChatAssistant.git
cd ChatAssistant

# 建一个虚拟环境并装依赖（Python 3.11）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 下载模型（见下一节），放到 models\ 里，然后：
python main.py
```

---

## 模型（必须自己下载一次）

| 项 | 值 |
|---|---|
| 文件名 | `qwen3-4b-instruct-2507-q4_k_m.gguf` |
| 大小 | **2.33 GB** |
| SHA256 | `3605803B982CB64AEAD44F6C1B2AE36E3ACDB41D8E46C8A94C6533BC4C67E597` |
| 来源 | 魔搭 ModelScope：`Qwen/Qwen3-4B-Instruct-2507-GGUF`（仓库内文件名 `Qwen3-4B-Instruct-2507-Q4_K_M.gguf`） |
| 许可 | Apache-2.0（可自由分发/商用，需保留许可声明） |

**用脚本下载（推荐，支持断点续传）**：

```powershell
# 源码运行时（脚本在工作区里）
python tools\download_modelscope.py Qwen/Qwen3-4B-Instruct-2507-GGUF `
       Qwen3-4B-Instruct-2507-Q4_K_M.gguf models/qwen3-4b-instruct-2507-q4_k_m.gguf
```

免安装包用户没有这个脚本，直接在浏览器打开
<https://modelscope.cn/models/Qwen/Qwen3-4B-Instruct-2507-GGUF/files> 下载上面的文件，
再改名成 `qwen3-4b-instruct-2507-q4_k_m.gguf`，放进解压目录的 `models\` 里即可。

> 想换更小/更大的模型也可以：把任意 `*.gguf` 放进 `models\`，程序会按
> `qwen3-4b-instruct-2507 → qwen3.5-4b → …` 的顺序自动挑一个；也可以在设置面板里指定。

---

## 使用说明

| 操作 | 效果 |
|---|---|
| **点消息气泡** | 分析这条消息（可连点相邻消息来回切换） |
| 再点同一条 / 点空白 / 滚动聊天区 | 收起面板 |
| **点三条回复选项之一** | 文本填入 QQ 输入框（只粘贴、不发送；失败会退化为复制） |
| 状态灯（聊天区右上角） | 颜色=状态；鼠标移上去变齿轮，点击打开设置 |
| 状态灯左侧的小图标 | 展开 / 收起「上下文历史」面板 |
| 托盘图标 | 设置 / 暂停分析 / 立即重探窗口 / 退出 |

**设置面板里可以改**：分析后端（本地 / API，默认本地）、主题、状态灯大小与位置、
**角色包**（这套配置的人设与说话风格，两套分析共用一份，建议 600-1000 字）、
每个字段的显示名与说明、配置列表（新建/重命名/删除）、用户画像（你自己的口癖，默认空）、
「复制模板 / 设置模板」（把 AI 生成的一整套提示词粘进来就能装好一套配置）、一键还原默认设置。

> 「点右侧选项填入输入框」的操作**永远不会替你发送**，发送前请自己确认内容。

---

## 常见问题

**Q：启动后没有任何浮窗？**
必须有一个**打开的 QQ 会话窗口**。程序在"没有会话窗口"（QQ 最小化、只停在会话列表）时会
把浮窗全部隐藏、且不弹提示。把聊天窗口打开，1-2 秒内状态灯会自动出现。

**Q：日志里写着"模型文件不存在"？**
把 `*.gguf` 放到**和 exe 同级**的 `models\` 目录下（源码运行则放到工作区 `models\`）。
也可以在设置面板里手动指定模型路径。

**Q：第一次启动很慢 / GPU 没吃上？**
第一次用 GPU 加载要编译显卡内核（缓存到 `%LOCALAPPDATA%\NVIDIA\ComputeCache`），可能 30-120 秒；
第二次会快很多。没有 N 卡会自动跑 CPU（更慢）。启动日志里会写清楚是不是在用 GPU。

**Q：杀毒软件报毒 / SmartScreen 拦截？**
这是 PyInstaller 打包且未签名的常见现象，点「更多信息 → 仍要运行」即可；
不放心的可以直接用源码运行（方式 B）。

**Q：QQ 更新后读不到消息了？**
程序读的是 QQ 的无障碍（UIA）树，QQ 大版本更新可能改控件结构。把
`logs\` 里的日志和 `tools\diagnose_uia.py` 的输出开个 issue，我来适配。

**Q：会不会导致 QQ 封号？**
程序不注入、不改内存、不模拟键鼠，只在你要填入时发一次 `Ctrl+V`，也不自动发送 ——
风险显著低于外挂/机器人，但**任何第三方工具都不能保证零风险**，请自行判断。

---

## 开发与打包

```powershell
# 跑回归（26 个用例，输出 通过/失败）
cd ChatAssistant
$tests = @('test_fast_mask','test_prompt_import','test_click_retry','test_prompt_echo',
  'test_self_fields','test_dup_text_click','test_blank_click','test_ui_polish','test_diagnose',
  'test_profile_switch','test_click_at','test_click_dedup','test_first_run_hint','test_fill_once',
  'test_self_panel','test_container_resolve','test_pool_merge','test_stance_prompt',
  'test_config_atomic','test_user_profile','test_ctx_panel_68','test_dot_drag_freeze',
  'test_startup_hidden','test_refresh_params','test_template_edit','test_prompt_stages')
foreach ($t in $tests) { python "tools\$t.py" 2>&1 | Select-Object -Last 1 }

# 打包成"解压即用"目录（默认会把 models\ 里的模型一起打进去，约 3.1GB）
python tools\build_exe.py --zip
# 只打程序、不带模型（Release 里发的就是这个，约 0.8GB）
python tools\build_exe.py --no-model --zip
```

打包产物在 `dist\`。仓库里**不含**模型、`config.json`、`logs/`、截图（见 `.gitignore`）。

### 目录结构

```
main.py / config.py      程序入口与配置（默认值都在 config.py）
core/                    UIA 读取、LLM 分析与约束解码、工作线程、截图兜底
ui/                      状态灯 / 分析面板 / 选项条 / 上下文面板 / 设置面板 / 主题 / Raw Input
utils/                   Win32 API、坐标换算
assets/                  应用图标
tools/                   出包、模型下载，以及开发期的测试/诊断脚本
docs/                    交接与排障文档
SPEC.md                  开发规格与逐轮改动记录（唯一权威，含每轮实测数据）
models/                  GGUF 模型（不进 git）
logs/                    运行日志（不进 git）
```

### 几个实现要点（给二次开发的人）

- **只读**：所有消息都来自 UIA；`ui/wheel_sink.py` 用 Raw Input 的**事件副本**收滚轮，不是钩子。
- **JSON 键名是抽象的 `A1..A6`**（`config.JSON_KEYS`）：键名不带语义，所以字段名/含义可以随便改；
  改键名要同时改掩码（`core/fast_mask.py`）、GBNF、解析与系统提示词 —— 四处都从那张表取。
- **约束解码**：自己算掩码（每步 ~0.2ms）保证输出一定是合法 JSON 且不超过字段字数；
  GBNF 只在配置里手动指定时才用（慢 ~3 倍）。
- **两段式**：第一段只列意图/情绪/危险度/建议，第二段只列回复选项 —— 提示词更短、更专注。
- **角色包**：配置里的人设文本会被完整放进系统提示词最前面，字段里写"按角色包"来引用。

---

## 许可与致谢

- 本项目代码：**MIT**（见 `LICENSE`）。
- 模型：**Qwen3-4B-Instruct-2507**（Apache-2.0，由 Qwen 团队发布）。
- 依赖：`llama-cpp-python`（MIT）、`PySide6/Qt`（LGPL-3.0）、`numpy`（BSD-3）。

仅供个人学习与自用。请遵守 QQ 用户协议与当地法律法规，作者不对使用后果负责。
