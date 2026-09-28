#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ChatAssistant 打成 Windows 可执行目录（PyInstaller onedir）。

用法（在工作区根目录）：
    cache\\py311\\python.exe tools\\build_exe.py                # 出包（模型不随包）
    cache\\py311\\python.exe tools\\build_exe.py --with-model   # 连当前在用模型一起放进去

产物：
    dist\\ChatAssistant\\ChatAssistant.exe        ← 双击运行
    dist\\ChatAssistant\\_internal\\...            ← 运行时（含 Qt、llama_cpp 的 DLL）
    dist\\ChatAssistant\\SPEC.md                  ← 顺带放一份（也用作"工作区锚点"）

为什么模型默认不随包：GGUF 2.3GB，zip 会翻倍且不能热更；放到 exe 同级 models/ 即可
（`resolve_model_path` 先找 exe 同级，再找工作区）。首启引导下载是后续步骤。

为什么复制 SPEC.md 到 exe 同级：`config._detect_workspace()` 是"从 exe 目录往上找 SPEC.md"，
找不到就退回 exe 目录。放一份进去可以把工作区**钉死在 dist 目录**（否则在开发机上会往上
找到 D:\\QQChatAssistant，日志/配置就写回源码树了）。
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
SPEC_DIR = ROOT / "build"
sys.path.insert(0, str(ROOT))               # 让 `python tools/build_exe.py` 也能 import config

# 明确排除：没用到却会把包撑大的模块（PySide6 的其余子模块由 PyInstaller 按导入收集）
EXCLUDES = (
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
    "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth", "PySide6.QtSerialPort",
    "tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest", "setuptools",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-model", action="store_true",
                        help="不把 GGUF 打进 dist（默认**会**打，用户口径：发出去的包要能直接用）")
    parser.add_argument("--with-model", action="store_true",
                        help="（兼容旧参数；现在这是默认行为）")
    parser.add_argument("--keep-build", action="store_true", help="保留 build 中间产物")
    parser.add_argument("--model-name", default="",
                        help="指定打进 dist 的 gguf 文件名（默认=当前 config 在用的那个；"
                             "开发时会把 paths.model 指向试用模型，出包要显式指回发布模型）")
    parser.add_argument("--zip", action="store_true",
                        help="出包后打成发布 zip（排除 logs/tmp/config.json；.gguf 用存储不压缩）")
    args = parser.parse_args()

    try:
        version = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                                 capture_output=True, text=True, check=True).stdout.strip()
    except Exception as exc:
        print(f"没装 PyInstaller（{exc}）→ 先跑："
              f"{sys.executable} -m pip install -r requirements.txt")
        return 1
    print(f"PyInstaller {version}｜Python {sys.version.split()[0]}｜工作区 {ROOT}")

    # 【关键】给 PyInstaller 一个干净的 PATH。
    # 踩过的坑（2026-09-24）：构建机 PATH 里带 Codex 运行时的 native/*/bin，
    # 里面有 ucrtbase.dll 和 90 个 api-ms-win-core-*.dll；PyInstaller 顺着 PATH 解析
    # Qt6Core.dll 的依赖，把这些"应用私有的 UCRT 桩"也收进了 _internal/ →
    # 跑起来直接 `DLL load failed while importing QtCore: 找不到指定的程序`（Qt 弹错误框，
    # 日志都没来得及写）。只留 Python 目录 + 系统目录即可。
    env = dict(os.environ)
    py_dir = str(Path(sys.executable).parent)
    system_root = env.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join([py_dir, py_dir + r"\Scripts",
                                   system_root + r"\system32", system_root])

    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean", "--windowed",
           "--name", "ChatAssistant",
           "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(SPEC_DIR),
           # 应用图标（tools/make_icon.py 生成）
           "--icon", str(ROOT / "assets" / "icon.ico"),
           # llama_cpp 的 DLL（ggml-*.dll / llama.dll）在包目录里，必须整包收集
           "--collect-all", "llama_cpp",
           # QLocalServer 走 QtNetwork（单实例/本地 IPC 用）
           "--hidden-import", "PySide6.QtNetwork",
           "--hidden-import", "PySide6.QtGui",
           "--hidden-import", "PySide6.QtCore",
           "--hidden-import", "PySide6.QtWidgets"]
    for name in EXCLUDES:
        cmd += ["--exclude-module", name]
    cmd.append(str(ROOT / "main.py"))

    print("开始打包（首次约 2-5 分钟，CUDA DLL 有 700+MB）……")
    started = time.time()
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    if result.returncode != 0:
        print(f"打包失败（退出码 {result.returncode}）")
        return result.returncode

    app_dir = DIST / "ChatAssistant"
    exe = app_dir / "ChatAssistant.exe"
    if not exe.exists():
        print("没找到产物 exe：", exe)
        return 1

    # 兜底清掉"绝不该随包分发"的 OS 自带件（万一 PATH 又漏进来）：
    # UCRT 与应用私有 api-ms-win-* 桩；留着会让 Qt/Python 的 DLL 解析版本错配。
    internal = app_dir / "_internal"
    removed = 0
    for pattern in ("ucrtbase.dll", "api-ms-win-*.dll"):
        for path in internal.rglob(pattern):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"已清掉不应随包的系统 DLL：{removed} 个（ucrtbase/api-ms-win-*）")

    # CUDA 运行库：ggml-cuda.dll 依赖 CUDA **12** 的 cudart64_12/cublas64_12
    # （构建机上装的是 CUDA 13.1，名字对不上；开发版能跑是因为工作区的 pip nvidia-* 包）。
    # 代码里 `analyzer._cuda_bin_dirs()` 会去找 `app_dir/nvidia/*/bin` → 这里按同样布局放一份，
    # 目标机器只要装了 NVIDIA 驱动（nvcuda.dll 来自驱动）就能直接上 GPU。
    # 实测只需要 cublas + cuda_runtime（nvrtc 是运行时编译内核用的，llama.cpp 用不到）。
    nvidia_src = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    cuda_bytes = 0
    if nvidia_src.is_dir():
        for pkg in ("cublas", "cuda_runtime"):
            src_bin = nvidia_src / pkg / "bin"
            if not src_bin.is_dir():
                continue
            dst_bin = app_dir / "nvidia" / pkg / "bin"
            dst_bin.mkdir(parents=True, exist_ok=True)
            for dll in src_bin.glob("*.dll"):
                shutil.copy2(dll, dst_bin / dll.name)
                cuda_bytes += dll.stat().st_size
        print(f"已随包放入 CUDA 12 运行库：{cuda_bytes / 1024**2:.0f} MB（nvidia/cublas + nvidia/cuda_runtime）")
    else:
        print(f"⚠ 没找到 {nvidia_src} → 打出来的包只能跑 CPU"
              "（可先 pip install nvidia-cublas-cu12 nvidia-cuda-runtime-cu12）")
    # 工作区锚点（见文件头注释）
    shutil.copy2(ROOT / "SPEC.md", app_dir / "SPEC.md")
    # 图标也放一份在 exe 同级（托盘/窗口图标从 assets/ 找）
    assets_src = ROOT / "assets"
    if assets_src.is_dir():
        shutil.copytree(assets_src, app_dir / "assets", dirs_exist_ok=True)

    if not args.no_model:
        models = app_dir / "models"
        models.mkdir(exist_ok=True)
        import config as app_config            # 只为了拿"当前在用模型"
        cfg_now = app_config.load_config()
        if args.model_name:
            wanted = (Path(app_config.WORKSPACE) / "models" / args.model_name)
            if not wanted.exists():
                print(f"指定的模型不存在：{wanted}")
                return 1
            model = wanted
            print(f"（--model-name）按指定模型出包，忽略 config.paths.model="
                  f"{cfg_now.paths.model!r}")
        else:
            model = app_config.resolve_model_path(cfg_now)
        if model and model.exists():
            print(f"复制模型 {model.name}（{model.stat().st_size / 1024**3:.2f} GB）……")
            shutil.copy2(model, models / model.name)
        else:
            print("没找到当前模型，跳过（把 .gguf 放到 dist/ChatAssistant/models/ 即可）")
    else:
        print("（--no-model）未把模型打进 dist")

    if not args.keep_build:
        shutil.rmtree(WORK, ignore_errors=True)

    if args.zip:
        # 运行痕迹/个人设置都不进发布包：别人解压后是"代码默认设置 + 自己的空配置"
        for stale in ("logs", "tmp"):
            shutil.rmtree(app_dir / stale, ignore_errors=True)
        (app_dir / "config.json").unlink(missing_ok=True)
        stamp = datetime.date.today().isoformat()
        zip_path = DIST / f"ChatAssistant-{stamp}.zip"
        print(f"正在打 zip（{zip_path.name}，GGUF 不压缩、其余 deflate）……")
        t_zip = time.time()
        with zipfile.ZipFile(zip_path, "w", allowZip64=True) as zf:
            for path in sorted(app_dir.rglob("*")):
                if path.is_dir():
                    continue
                rel = path.relative_to(DIST).as_posix()
                if path.suffix.lower() in (".gguf", ".zip"):
                    zf.write(path, rel, compress_type=zipfile.ZIP_STORED)
                else:
                    zf.write(path, rel, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        print(f"zip 完成：{zip_path}（{zip_path.stat().st_size / 1024**3:.2f} GB，"
              f"耗时 {time.time() - t_zip:.0f}s）")

    size = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file())
    print(f"\n完成，用时 {time.time() - started:.0f}s")
    print(f"产物：{app_dir}（{size / 1024**3:.2f} GB）")
    print(f"运行：{exe}")
    print("模型：把 models\\*.gguf 放到 exe 同级的 models\\ 目录（或 --with-model 重新打包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
