# tools —— 出包、图标、模型下载、开发期脚本

所有脚本都在**工作区根目录**下运行（脚本里用 `parents[1]` 取根目录，所以必须放在 `tools/`
这一层，不要再往下建子目录）。统一用工作区的便携 Python：

```powershell
cache\py311\python.exe tools\<脚本名>.py [参数]
```

## 正式工具（日常会用到）

| 脚本 | 用途 |
|---|---|
| `build_exe.py` | 出包：PyInstaller onedir +（默认）把模型打进去 + `--zip` 打发布包 |
| `make_icon.py` | 生成 `assets/icon.ico`（紫粉线条镂空风格）+ 预览用 PNG |
| `download_modelscope.py` | 从魔搭下载 GGUF：`<owner/model> <仓库内文件> <本地文件>` |
| `audit_isolation.py` | 工作区外写入审计：`snapshot` 记基线 → 跑应用 → `diff` 出结论 |
| `capture_screen.py` | 全屏截图（排查 UI 位置用） |
| `click_context_button.py` | 用日志里的图标坐标注入一次点击（验证上下文面板开关） |
| `set_model.py` / `open_settings.py` | 快速改模型路径 / 打开设置面板 |

## 测试（改完代码跑这些，输出 `x/y 通过`）

`test_*.py`：

| 脚本 | 覆盖 |
|---|---|
| `test_pool_merge.py` | 上下文池合并：重复短句错配、上下滚、无重叠重置（10 项） |
| `test_dot_drag_freeze.py` | 拖动窗口时"过期 UIA 坐标"不能让状态灯闪回（3 项） |
| `test_ctx_panel_68.py` | 上下文面板：右侧放置 / 没位置回旧位 / 标题省略号（3 项） |
| `test_config_atomic.py` | 配置原子写 + 备份 + 损坏自动恢复（4 项） |
| `test_user_profile.py` | 用户画像：空不进 prompt / 填了进 / 面板读写（5 项） |
| `test_startup_hidden.py` | 启动无会话时三个浮窗都不显示（4 项） |
| `test_context_chain.py` | 真实模型：上下文串联（cos 清单那串）是否仍能读对 |
| `test_stream.py` / `test_theme.py` / `test_field_prompt.py` / `test_parse_cn_keys.py` / `test_profiles.py` | 流式上屏、主题、字段说明、中文键名解析、配置列表 |

> 需要模型的脚本会加载本地 GGUF（约 2-3s 加载 + 每次生成 2-4s）；其余都是毫秒级、不需要模型。

## 诊断与测量（排查问题 / 调参时用）

| 脚本 | 用途 |
|---|---|
| `dump_context.py [--live]` | 打印"某条消息实际喂给模型的上下文"（零成本，不调模型） |
| `explain_context.py` / `explain_context_logic.py` | 上下文池对账：哪些进了 prompt、为什么没用上 |
| `diag_scroll_order.py` / `diag_context_order.py` | 滚动后池子顺序 vs 屏幕顺序的对照 |
| `probe_uia_raw.py` / `probe_uia_index.py` / `probe_gguf`? | UIA 原始属性 / 行下标 / GGUF 元数据探针 |
| `measure_prompt_split.py` / `measure_request_now.py` / `measure_user_profile_cost.py` | 提示词各段 token 占用 |
| `measure_clip_cost.py` / `measure_fill_prep.py` | 剪贴板读写、填入准备动作的耗时 |
| `bench_speed.py` / `bench_models.py` / `probe_model_bench.py` | 各模型速度/质量基准（结果见 docs/、tmp/*.json） |
| `live_*.py` / `verify_*.py` / `check_*.py` | 实机验收小脚本（点击、悬停、滚动、面板标题、API 限额…） |
| `sample_style.py` / `sample_profiles.py` | 打印两套配置在几条样本上的分析输出（调 prompt 必用） |
| `ab_context.py` / `ab_*.py` / `eval_*.py` | 上下文方案 A/B、意图评测 |

## 约定

- **只读**：这些脚本一律不得注入输入（除了 `click_context_button.py` 这种明确用于验收的
  单次点击脚本，且只点我们自己的图标）。
- 输出文件统一写到 `tmp/`（脚本里已经这么写），不要把产物丢到工作区外。
