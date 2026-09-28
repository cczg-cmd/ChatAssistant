# tmp —— 开发期临时产物（可以随时整个删掉）

这里放的是开发/调试过程中产生的东西，**不参与运行、不进发布包**：

- `*.txt` / `*.log`：各轮实验与验收的输出（例如 `sample_profiles_r59e.txt`、`stdout_run*.log`）
- `*.png`：界面截图（面板位置、图标预览、上下文面板对账等）
- `*.json`：基准/评测结果（`model_bench.json`、`eval_*.json`）与夹具（`ctx_fixture.json`）
- `installers/`：早期搭便携 Python 用的安装包（现已装好，留着只为重建环境）

**注意两点**：

1. `ctx_fixture.json` 等**夹具**被 `tools/` 里的脚本引用（`ab_context.py`、`check_default_profile.py`、
   `measure_prompt_split.py`），删 tmp 之前先确认不再需要它们；
2. 程序运行时的临时文件也走这里（`config.TMP_DIR = 工作区/tmp`），所以别把整个目录设成只读。

脚本本身都搬到 `../tools/` 了（见 `tools/README.md`），这里只剩产物。
