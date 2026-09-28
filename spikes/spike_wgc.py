#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spike_wgc.py - ChatAssistant Spike #1（SPEC.md 6.1-1 / 4.1 截图与降级链）

验证目标（严格对应 SPEC.md，不使用任何 Mock 数据）：
  1. 通过 EnumWindows + GetWindowThreadProcessId 找到 QQ.exe 的顶层窗口。
     本阶段 utils/win32_api.py 尚未实现，故先内联同等逻辑（返回值均为物理像素），
     后续抽出成模块时可直接搬运。
  2. 用 wgc_python 连续捕获该窗口客户区 100 帧，每帧间隔 100ms。
  3. 统计：非黑帧占比（黑帧 = 帧内平均亮度<5 且 标准差<3，或全黑像素占比>99%）、
     tracemalloc 峰值相对第 10 帧的增长率、最小化状态（IsIconic）下是否抛异常。
  4. 测试 DXGI Desktop Duplication 降级方案，并给出它在两种窗口几何场景下是否有效：
       场景A：窗口完全位于单一显示器可视区域内
       场景B：窗口右边缘距屏幕右边缘 < 50px
输出（全部写入 spikes/results/）：
  spike_wgc_report.json   机读报告
  spike_wgc_log.txt       诊断日志
  spike_wgc_shot_*.png    截图（最多保留 5 张）

用法：
  python spikes/spike_wgc.py
  python spikes/spike_wgc.py --frames 100 --interval-ms 100
  python spikes/spike_wgc.py --only-dxgi                 # 只调试 DXGI 降级链
  python spikes/spike_wgc.py --no-move-window --skip-minimize --skip-dxgi

退出码：0=PASS / 1=FAIL / 2=ABORT（WGC 初始化失败或全黑，按规则立刻停止并上报）
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 全局文件隔离约束（所有 Spike / 业务脚本开头必须原样复制本段）
# 项目工作区：D:\QQChatAssistant（用 SPEC.md 自动探测，避免外置路径写错）
# 严禁在 C 盘、用户目录、AppData、系统 Temp 或工作区之外创建/下载/缓存/写日志；
# 所有临时文件、缓存、日志、中间产物只能落在工作区内：
#   tmp/  cache/  logs/  spikes/results/
# --------------------------------------------------------------------------- #
import os as _os
import tempfile as _tempfile
from pathlib import Path as _Path


def _detect_workspace(start: _Path) -> _Path:
    """向上查找含 SPEC.md 的目录作为工作区根；找不到时退回到脚本上两级。"""
    for candidate in (start, *start.parents):
        if (candidate / "SPEC.md").exists():
            return candidate
    return start.parents[1] if len(start.parents) > 1 else start


WORKSPACE = str(_detect_workspace(_Path(__file__).resolve().parent))
TMP_DIR = _os.path.join(WORKSPACE, "tmp")
CACHE_DIR = _os.path.join(WORKSPACE, "cache")
LOG_DIR = _os.path.join(WORKSPACE, "logs")
for _d in (TMP_DIR, CACHE_DIR, LOG_DIR):
    _os.makedirs(_d, exist_ok=True)
_os.environ["TMP"] = TMP_DIR
_os.environ["TEMP"] = TMP_DIR
_os.environ["TMPDIR"] = TMP_DIR
_tempfile.tempdir = TMP_DIR
_os.environ["HF_HOME"] = _os.path.join(CACHE_DIR, "huggingface")
_os.environ["MODELSCOPE_CACHE"] = _os.path.join(CACHE_DIR, "modelscope")
_os.environ["TORCH_HOME"] = _os.path.join(CACHE_DIR, "torch")
# 同类缓存也一并钉在工作区内（pip / numba / matplotlib / XDG 通用缓存）
_os.environ["PIP_CACHE_DIR"] = _os.path.join(CACHE_DIR, "pip")
_os.environ["NUMBA_CACHE_DIR"] = _os.path.join(CACHE_DIR, "numba")
_os.environ["MPLCONFIGDIR"] = _os.path.join(LOG_DIR, "matplotlib")
_os.environ.setdefault("XDG_CACHE_HOME", _os.path.join(CACHE_DIR, "xdg"))


def workspace_paths() -> dict[str, str]:
    return {"workspace": WORKSPACE, "tmp": TMP_DIR, "cache": CACHE_DIR,
            "logs": LOG_DIR, "results": str(_Path(WORKSPACE) / "spikes" / "results")}


# 隔离审计：运行前后对比这些"默认会写 C 盘的第三方位置"的顶层条目
_HOME = _os.path.expanduser("~")
EXTERNAL_WATCH_DIRS = [
    _os.path.join(_HOME, "AppData", "Local", "Temp"),
    _os.path.join(_HOME, "AppData", "Local", "pip", "cache"),
    _os.path.join(_HOME, "AppData", "Local", "NVIDIA"),
    _os.path.join(_HOME, ".cache"),
    _os.path.join(_HOME, ".huggingface"),
    _os.path.join(_HOME, ".modelscope"),
    _os.path.join(_HOME, ".torch"),
    _os.path.join(_HOME, ".keras"),
    _os.path.join(_HOME, ".matplotlib"),
    _os.path.join(_HOME, ".numba"),
    _os.path.join(_HOME, ".config", "matplotlib"),
]
# 只有名字命中这些标记的新增条目才算"本项目第三方库外写"
EXTERNAL_ATTRIBUTION_MARKERS = ("wgc", "spike", "chatassistant", "rapidocr",
                                "onnx", "huggingface", "modelscope", "torch",
                                "llama", "numba", "matplotlib", "pip-")


def snapshot_external_dirs() -> dict[str, set]:
    """记录监控目录的顶层条目（不递归，避免扫描开销和噪声）。"""
    snapshot: dict[str, set] = {}
    for directory in EXTERNAL_WATCH_DIRS:
        try:
            snapshot[directory] = {entry.name for entry in _os.scandir(directory)}
        except OSError:
            snapshot[directory] = set()
    return snapshot


# 已知会被第三方库创建在工作区外的空缓存目录：审计时顺带清理（只删空树）
EXTERNAL_EMPTY_CACHE_CANDIDATES = [
    _os.path.join(_HOME, ".cache", "modelscope"),
    _os.path.join(_HOME, ".cache", "huggingface"),
    _os.path.join(_HOME, ".cache", "torch"),
    _os.path.join(_HOME, ".modelscope"),
    _os.path.join(_HOME, ".huggingface"),
]


def cleanup_empty_external_dirs() -> list[str]:
    """删除工作区外的"空缓存目录"（树内无任何文件时才删，自底向上收口）。"""
    removed: list[str] = []
    for root in EXTERNAL_EMPTY_CACHE_CANDIDATES:
        if not _os.path.isdir(root):
            continue
        has_file = any(files for _r, _d, files in _os.walk(root))
        if has_file:
            continue
        for current, dirs, _files in _os.walk(root, topdown=False):
            for name in dirs:
                try:
                    _os.rmdir(_os.path.join(current, name))
                except OSError:
                    pass
        try:
            _os.rmdir(root)
            removed.append(root)
        except OSError:
            pass
    return removed


def audit_isolation(before: dict[str, set], after: dict[str, set]) -> dict:
    """比对工作区外的写入痕迹，区分"可归因于本项目"与"系统/其他程序"。"""
    cleaned_empty_dirs = cleanup_empty_external_dirs()
    inside_ok = _Path(_tempfile.gettempdir()).resolve().is_relative_to(
        _Path(WORKSPACE).resolve())
    env_ok = all(_os.environ.get(key, "").startswith(WORKSPACE)
                 for key in ("TMP", "TEMP", "TMPDIR", "HF_HOME",
                             "MODELSCOPE_CACHE", "TORCH_HOME"))
    attributable: list[dict[str, str]] = []
    unattributed: list[dict[str, str]] = []
    for directory, names in after.items():
        new_names = names - before.get(directory, set())
        for name in sorted(new_names):
            item = {"dir": directory, "name": name}
            if any(marker in name.lower() for marker in EXTERNAL_ATTRIBUTION_MARKERS):
                attributable.append(item)
            else:
                unattributed.append(item)
    return {
        "workspace": WORKSPACE,
        "tempdir_inside_workspace": bool(inside_ok),
        "env_inside_workspace": bool(env_ok),
        "tempfile_gettempdir": _tempfile.gettempdir(),
        "env": {key: _os.environ.get(key) for key in
                ("TMP", "TEMP", "TMPDIR", "HF_HOME", "MODELSCOPE_CACHE",
                 "TORCH_HOME", "PIP_CACHE_DIR", "NUMBA_CACHE_DIR", "MPLCONFIGDIR")},
        "external_new_entries_attributable": attributable,
        "external_new_entries_unattributed": unattributed,
        "cleaned_empty_dirs": cleaned_empty_dirs,
        "clean": bool(inside_ok and env_ok and not attributable),
    }


import argparse
import ctypes
import ctypes.wintypes as wt
import gc
import json
import logging
import sys
import time
import tracemalloc
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


# --------------------------------------------------------------------------- #
# 常量（阈值全部来自 SPEC.md，改动必须先改 SPEC）
# --------------------------------------------------------------------------- #
SPIKE_ID = "spike_wgc"
SPIKE_TAG = "SPIKE_WGC"

SPIKES_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SPIKES_DIR / "results"
REPORT_JSON = RESULTS_DIR / f"{SPIKE_ID}_report.json"
LOG_TXT = RESULTS_DIR / f"{SPIKE_ID}_log.txt"
SHOT_PREFIX = f"{SPIKE_ID}_shot_"
MAX_SCREENSHOTS = 5

FRAME_COUNT_DEFAULT = 100
FRAME_INTERVAL_MS_DEFAULT = 100

TRACEMALLOC_BASELINE_FRAME = 10          # SPEC 4.1：内存基线 = 第10帧
TRACEMALLOC_GROWTH_LIMIT = 0.20          # SPEC 4.1：阈值 <20%

BLACK_MEAN_LT = 5.0                      # 黑帧定义①：平均亮度 < 5
BLACK_STD_LT = 3.0                       # 黑帧定义①：标准差 < 3
BLACK_PIXEL_MAX_VALUE = 2                # “全黑像素”判定：三通道最大值 <= 2
BLACK_PIXEL_RATIO_GT = 0.99              # 黑帧定义②：全黑像素占比 > 99%
NON_BLACK_RATIO_MIN = 0.95               # SPEC 6.1：黑帧率 < 5%
MIN_FRAMES_ACQUIRED_RATIO = 0.95         # 100 帧里至少拿到 95 帧才算稳定

MINIMIZED_PROBE_SECONDS = 3.0            # 最小化状态下探测时长
DXGI_RIGHT_GAP_PX = 50                   # 场景B：右边缘距屏幕右边缘 < 50px
DXGI_SCENARIO_RIGHT_GAP = 20             # 场景B 实际摆放的间隙
DWM_MARGIN = 40                          # 场景A 摆放时与显示器边缘的留白
MAX_LOGGED_WINDOWS = 8                   # 诊断用：最多记录多少个 QQ 顶层窗口

QQ_EXE_NAME = "qq.exe"

# 懒加载的 cv2 句柄：None=未尝试，False=不可用（截图与亮度统计共用）
_CV2: Any = None

# 隔离审计基线：main() 启动时拍摄，finalize() 结束时比对
_ISOLATION_BASELINE: dict[str, set] = {}


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
logger = logging.getLogger(SPIKE_ID)


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


class _RTL_OSVERSIONINFOEXW(ctypes.Structure):
    _fields_ = [
        ("dwOSVersionInfoSize", wt.DWORD), ("dwMajorVersion", wt.DWORD),
        ("dwMinorVersion", wt.DWORD), ("dwBuildNumber", wt.DWORD),
        ("dwPlatformId", wt.DWORD), ("szCSDVersion", wt.WCHAR * 128),
        ("wServicePackMajor", wt.WORD), ("wServicePackMinor", wt.WORD),
        ("wSuiteMask", wt.WORD), ("wProductType", ctypes.c_byte),
        ("wReserved", ctypes.c_byte),
    ]


def windows_build() -> str:
    try:
        info = _RTL_OSVERSIONINFOEXW()
        info.dwOSVersionInfoSize = ctypes.sizeof(info)
        ctypes.WinDLL("ntdll").RtlGetVersion(ctypes.byref(info))
        return f"{info.dwMajorVersion}.{info.dwMinorVersion}.{info.dwBuildNumber}"
    except Exception as exc:  # pragma: no cover
        return f"unknown ({exc})"


def collect_environment() -> Dict[str, Any]:
    import platform
    env: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "windows_build": windows_build(),
        "numpy": np.__version__,
    }
    try:
        import cv2
        env["opencv"] = cv2.__version__
    except Exception as exc:
        env["opencv"] = f"unavailable: {exc}"
    try:
        import wgc_python
        env["wgc_python_file"] = str(Path(wgc_python.__file__).resolve())
        env["wgc_python_version"] = getattr(wgc_python, "__version__", "未知")
    except Exception as exc:
        env["wgc_python_file"] = f"unavailable: {exc}"
    return env


# --------------------------------------------------------------------------- #
# Win32 逻辑（等价于 SPEC 目录结构中的 utils/win32_api.py，本阶段先内联）
# 所有坐标 / 尺寸均为物理像素
# --------------------------------------------------------------------------- #
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.IsZoomed.argtypes = [wt.HWND]
user32.IsZoomed.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ClientToScreen.restype = wt.BOOL
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.GetMonitorInfoW.restype = wt.BOOL
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.SetWindowPos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


psapi.GetProcessMemoryInfo.argtypes = [
    wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD]
psapi.GetProcessMemoryInfo.restype = wt.BOOL


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
    ]


MONITOR_DEFAULTTONEAREST = 2
GA_ROOT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9

SW_MINIMIZE, SW_RESTORE, SW_MAXIMIZE = 6, 9, 3
SWP_NOZORDER, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0004, 0x0010, 0x0040

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def set_dpi_awareness() -> str:
    """SPEC：win32_api 返回值均为物理像素 → 必须开启 Per-Monitor-V2 DPI 感知。"""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system-aware"
    except Exception as exc:  # pragma: no cover
        return f"none ({exc})"


def enum_top_level_windows() -> List[Tuple[int, int]]:
    """EnumWindows + GetWindowThreadProcessId → [(hwnd, pid), ...]（顶层窗口）。"""
    found: List[Tuple[int, int]] = []

    @WNDENUMPROC
    def callback(hwnd, _lparam):
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            found.append((int(hwnd), int(pid.value)))
        return True

    if not user32.EnumWindows(callback, 0):
        err = ctypes.get_last_error()
        if err:
            raise OSError(err, "EnumWindows failed")
    return found


def get_process_image_path(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def get_window_rect(hwnd: int) -> Tuple[int, int, int, int]:
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(ctypes.get_last_error(), "GetWindowRect failed")
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_client_rect_size(hwnd: int) -> Tuple[int, int]:
    rect = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError(ctypes.get_last_error(), "GetClientRect failed")
    return (rect.right - rect.left, rect.bottom - rect.top)


def get_client_origin_on_screen(hwnd: int) -> Tuple[int, int]:
    point = wt.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return get_window_rect(hwnd)[:2]
    return (point.x, point.y)


def get_client_rect_on_screen(hwnd: int) -> Tuple[int, int, int, int]:
    origin_x, origin_y = get_client_origin_on_screen(hwnd)
    width, height = get_client_rect_size(hwnd)
    return (origin_x, origin_y, origin_x + width, origin_y + height)


def get_extended_frame_bounds(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """DWM 真实可见边界（剔除 Win10/11 不可见阴影边框），失败返回 None。"""
    rect = wt.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        wt.HWND(hwnd), wt.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect), ctypes.sizeof(rect))
    if hr != 0:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_monitor_info(hwnd: int) -> Dict[str, Any]:
    handle = user32.MonitorFromWindow(wt.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not handle or not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
        return {}
    return {
        "monitor_handle": int(handle),
        "monitor_rect": [info.rcMonitor.left, info.rcMonitor.top,
                         info.rcMonitor.right, info.rcMonitor.bottom],
        "work_rect": [info.rcWork.left, info.rcWork.top,
                      info.rcWork.right, info.rcWork.bottom],
        "is_primary": bool(info.dwFlags & 1),
    }


def get_monitor_count() -> int:
    return int(user32.GetSystemMetrics(80))  # SM_CMONITORS


def get_virtual_screen_rect() -> Tuple[int, int, int, int]:
    return (int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77)),
            int(user32.GetSystemMetrics(78)), int(user32.GetSystemMetrics(79)))


def rect_inside(inner: Sequence[int], outer: Sequence[int]) -> bool:
    return (inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def rect_size(rect: Sequence[int]) -> Tuple[int, int]:
    return (int(rect[2] - rect[0]), int(rect[3] - rect[1]))


def sample_occlusion_ratio(hwnd: int, rect: Sequence[int], grid: int = 6) -> float:
    """在窗口矩形上采样 grid×grid 个点，统计落在该窗口上的比例。

    DXGI 抓的是合成后的桌面，被遮挡就会拍到别的窗口，故必须量化遮挡程度。
    """
    width, height = rect_size(rect)
    if width <= 0 or height <= 0:
        return 0.0
    hits = total = 0
    for iy in range(grid):
        for ix in range(grid):
            x = int(rect[0] + (ix + 0.5) * width / grid)
            y = int(rect[1] + (iy + 0.5) * height / grid)
            root = user32.GetAncestor(user32.WindowFromPoint(wt.POINT(x, y)), GA_ROOT)
            total += 1
            if root and int(root) == int(hwnd):
                hits += 1
    return hits / total if total else 0.0


def get_process_working_set() -> int:
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                  ctypes.byref(counters), counters.cb):
        return int(counters.WorkingSetSize)
    return -1


def put_window_to_foreground(hwnd: int) -> bool:
    user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
    ok = bool(user32.SetForegroundWindow(wt.HWND(hwnd)))
    if not ok:
        # 备选：最小化再恢复强制前台（最终几何由调用方恢复）
        user32.ShowWindow(wt.HWND(hwnd), SW_MINIMIZE)
        time.sleep(0.15)
        user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
        ok = bool(user32.SetForegroundWindow(wt.HWND(hwnd)))
    return ok


# --------------------------------------------------------------------------- #
# QQ 顶层窗口发现（要求 1）
# --------------------------------------------------------------------------- #
class WindowInfo:
    def __init__(self, hwnd: int, pid: int, exe: str, title: str, cls: str):
        self.hwnd = hwnd
        self.pid = pid
        self.exe = exe
        self.title = title
        self.cls = cls
        self.visible = bool(user32.IsWindowVisible(wt.HWND(hwnd)))
        self.iconic = bool(user32.IsIconic(wt.HWND(hwnd)))
        self.zoomed = bool(user32.IsZoomed(wt.HWND(hwnd)))
        self.rect = get_window_rect(hwnd)
        self.client_size = get_client_rect_size(hwnd)
        self.ext_bounds = get_extended_frame_bounds(hwnd)
        self.monitor = get_monitor_info(hwnd)

    @property
    def client_area(self) -> int:
        return int(self.client_size[0] * self.client_size[1])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hwnd": self.hwnd, "pid": self.pid, "exe": self.exe,
            "title": self.title, "class": self.cls,
            "visible": self.visible, "iconic": self.iconic, "zoomed": self.zoomed,
            "window_rect": list(self.rect),
            "client_size": list(self.client_size),
            "extended_frame_bounds": list(self.ext_bounds) if self.ext_bounds else None,
            "monitor": self.monitor,
        }


def find_qq_windows() -> List[WindowInfo]:
    """EnumWindows → 过滤 QQ.exe 进程 → 组装顶层窗口信息。"""
    windows: List[WindowInfo] = []
    exe_cache: Dict[int, str] = {}
    for hwnd, pid in enum_top_level_windows():
        if pid not in exe_cache:
            exe_cache[pid] = get_process_image_path(pid)
        if Path(exe_cache[pid]).name.lower() != QQ_EXE_NAME:
            continue
        windows.append(WindowInfo(hwnd, pid, exe_cache[pid],
                                  get_window_text(hwnd), get_class_name(hwnd)))
    return windows


def pick_capture_target(windows: List[WindowInfo]) -> Optional[WindowInfo]:
    """优先：可见 & 未最小化 & 有标题 & 客户区最大（即 QQ 主聊天窗口）。"""
    candidates = [w for w in windows if w.client_area > 0 and w.title]
    if not candidates:
        candidates = [w for w in windows if w.client_area > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda w: (w.visible, not w.iconic, bool(w.title), w.client_area))


# --------------------------------------------------------------------------- #
# 帧指标 / 黑帧判定（要求 3；阈值来自 SPEC 4.1）
# --------------------------------------------------------------------------- #
def frame_metrics(bgra: np.ndarray) -> Dict[str, Any]:
    """输入 BGRA uint8；返回平均亮度 / 标准差 / 全黑像素占比 / 是否黑帧。

    这里刻意不生成 int16/int32 全图中间量：否则 tracemalloc 会把本函数自己的
    临时缓冲算进"采集管线内存增长"，掩盖真实泄漏或真实稳定性。
    """
    height, width = bgra.shape[:2]
    blue = bgra[:, :, 0]
    green = bgra[:, :, 1]
    red = bgra[:, :, 2]
    mean, std = luma_stats(bgra)
    total = int(height * width)
    channel_max = np.maximum(np.maximum(blue, green), red)
    black_pixels = int(np.count_nonzero(channel_max <= BLACK_PIXEL_MAX_VALUE))
    black_ratio = black_pixels / total if total else 1.0
    is_black = (mean < BLACK_MEAN_LT and std < BLACK_STD_LT) or (black_ratio > BLACK_PIXEL_RATIO_GT)
    return {
        "mean_luma": round(mean, 3),
        "std_luma": round(std, 3),
        "black_pixel_ratio": round(black_ratio, 6),
        "is_black": bool(is_black),
        "width": int(width),
        "height": int(height),
    }


def luma_stats(bgra: np.ndarray) -> Tuple[float, float]:
    """亮度均值 / 标准差。

    优先用 cv2 的 BT.601 定点实现（与 0.114B+0.587G+0.299R 等价，且不产生
    大块 Python 侧中间数组）；cv2 不可用时回退到 numpy，显式升到 uint16——
    直接 uint8 相乘会在 NEP50 弱标量规则下溢出；定点权值必须满足
    29+150+77 == 256（即 (29B+150G+77R)>>8 才是 0~255 的真实亮度），
    否则亮度会被整体放大，动摇"平均亮度<5"这条黑帧判据。
    """
    global _CV2
    if _CV2 is None:
        try:
            import cv2 as _cv2_module
            _CV2 = _cv2_module
        except ImportError:
            _CV2 = False
    if _CV2:
        gray = _CV2.cvtColor(bgra, _CV2.COLOR_BGRA2GRAY)
        mean_arr, std_arr = _CV2.meanStdDev(gray)
        return float(mean_arr[0, 0]), float(std_arr[0, 0])
    blue = bgra[:, :, 0].astype(np.uint16)
    green = bgra[:, :, 1].astype(np.uint16)
    red = bgra[:, :, 2].astype(np.uint16)
    luma = (29 * blue + 150 * green + 77 * red) >> 8
    return float(luma.mean()), float(luma.std())


def tracemalloc_top(limit: int = 8) -> List[Dict[str, Any]]:
    """诊断用：列出 tracemalloc 当前占用最大的若干分配点。"""
    try:
        snapshot = tracemalloc.take_snapshot()
    except Exception as exc:  # pragma: no cover
        return [{"error": str(exc)}]
    return [{"where": str(stat.traceback[0]), "size_bytes": int(stat.size),
             "blocks": int(stat.count)}
            for stat in snapshot.statistics("lineno")[:limit]]


def _gray_small(image: np.ndarray, size: int = 64) -> np.ndarray:
    """最近邻降采样成 size×size 灰度图（用于跨来源图像相似度）。"""
    height, width = image.shape[:2]
    ys = np.linspace(0, height - 1, size).astype(np.int64)
    xs = np.linspace(0, width - 1, size).astype(np.int64)
    sub = image[np.ix_(ys, xs)][:, :, :3].astype(np.int16)
    return (29 * sub[:, :, 0] + 57 * sub[:, :, 1] + 11 * sub[:, :, 2]) >> 6


def image_similarity(reference: np.ndarray, candidate: np.ndarray,
                     size: int = 64) -> Optional[float]:
    """两张同尺寸图像降采样后的皮尔逊相关系数（用于校验 DXGI 抓到的是同一画面）。"""
    if reference is None or candidate is None:
        return None
    if reference.shape[:2] != candidate.shape[:2]:
        return None
    left = _gray_small(reference, size).astype(np.float64).ravel()
    right = _gray_small(candidate, size).astype(np.float64).ravel()
    if left.std() < 1e-6 or right.std() < 1e-6:
        return None
    return float(np.corrcoef(left, right)[0, 1])


class ScreenshotBudget:
    """截图预算：最多保留 MAX_SCREENSHOTS 张，且只写入 spikes/results/。"""

    def __init__(self, limit: int = MAX_SCREENSHOTS):
        self.limit = limit
        self.saved: List[Dict[str, Any]] = []
        self.skipped: List[str] = []

    def clean_previous(self) -> List[str]:
        """只清理本 spike 自己的旧截图（限定目录 + 限定前缀）。"""
        removed: List[str] = []
        for path in sorted(RESULTS_DIR.glob(f"{SHOT_PREFIX}*.png")):
            if path.resolve().parent != RESULTS_DIR.resolve():
                continue
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                logger.warning("旧截图删除失败 %s: %s", path.name, exc)
        return removed

    def save(self, name: str, bgra: Optional[np.ndarray], note: str = "") -> bool:
        if bgra is None:
            self.skipped.append(f"{name}(无帧数据)")
            return False
        if len(self.saved) >= self.limit:
            self.skipped.append(f"{name}(超出 {self.limit} 张预算)")
            logger.info("截图预算已满，跳过：%s", name)
            return False
        path = RESULTS_DIR / f"{SHOT_PREFIX}{name}.png"
        try:
            import cv2
            if not cv2.imwrite(str(path), bgra):
                raise OSError("cv2.imwrite 返回 False")
        except Exception as exc:
            logger.warning("截图写入失败 %s: %s", name, exc)
            self.skipped.append(f"{name}(写入失败: {exc})")
            return False
        self.saved.append({"name": name, "file": path.name, "note": note,
                           "shape": list(bgra.shape)})
        logger.info("截图已保存：%s（%s）", path.name, note or "-")
        return True


# --------------------------------------------------------------------------- #
# wgc_python 捕获回环（要求 2）
# --------------------------------------------------------------------------- #
class WgcCapture:
    """wgc_python 封装：连续捕获（不 pause），每帧取完立刻 release。"""

    def __init__(self, title: str, class_name: str, client_area_only: bool = True):
        import wgc_python
        self.title = title
        self.class_name = class_name
        self.client_area_only = client_area_only
        self.capture = wgc_python.WindowCapture(
            title, class_name, client_area_only=client_area_only, capture_cursor=False)

    def acquire(self, timeout_s: float) -> Optional[np.ndarray]:
        """在 timeout_s 内等待一帧；返回 BGRA 副本，超时返回 None。"""
        deadline = time.perf_counter() + timeout_s
        while True:
            raw = self.capture.get_frame()
            if raw:
                ptr, width, height, row_pitch = raw
                try:
                    buffer = (ctypes.c_ubyte * (height * row_pitch)).from_address(ptr)
                    view = np.ndarray((height, width, 4), dtype=np.uint8, buffer=buffer,
                                      strides=(row_pitch, 4, 1))
                    return view.copy()
                finally:
                    self.capture.release_frame()
            if time.perf_counter() >= deadline:
                return None
            time.sleep(0.002)

    def is_capturing(self) -> bool:
        try:
            return bool(self.capture.is_capturing())
        except Exception:
            return False

    def close(self) -> None:
        try:
            self.capture.close()
        except Exception as exc:  # pragma: no cover
            logger.warning("WGC close 异常：%s", exc)


# --------------------------------------------------------------------------- #
# DXGI Desktop Duplication 降级链（要求 4）
# 纯 ctypes COM 调用；vtable 索引来自 dxgi1_2.h / d3d11.h 的稳定 ABI 布局
# --------------------------------------------------------------------------- #
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
DXGI_ERROR_NOT_CURRENTLY_AVAILABLE = 0x887A0022

D3D_DRIVER_TYPE_HARDWARE = 1
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_SDK_VERSION = 7

D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_MAP_READ = 1

VT_IUNKNOWN_QUERYINTERFACE = 0
VT_IUNKNOWN_RELEASE = 2
VT_IDXGIDEVICE_GETADAPTER = 7                # IDXGIDevice
# IDXGIAdapter 的 vtable 顺序是 EnumOutputs(7) → GetDesc(8) → CheckInterfaceSupport(9)
VT_IDXGIADAPTER_ENUMOUTPUTS = 7              # IDXGIAdapter
VT_IDXGIADAPTER_GETDESC = 8                  # IDXGIAdapter
VT_IDXGIOUTPUT_GETDESC = 7                   # IDXGIOutput
# IDXGIOutput 有 12 个方法（7..18，含 Set/GetDisplaySurfaceData、GetFrameStatistics），
# 所以 IDXGIOutput1 的 DuplicateOutput 落在 22
VT_IDXGIOUTPUT1_DUPLICATEOUTPUT = 22         # IDXGIOutput1
# IDXGIOutputDuplication : IDXGIObject → GetDesc(7), AcquireNextFrame(8), ..., ReleaseFrame(14)
VT_IDXGIOUTDUP_ACQUIRENEXTFRAME = 8          # IDXGIOutputDuplication
VT_IDXGIOUTDUP_RELEASEFRAME = 14
VT_ID3D11DEVICE_CREATETEXTURE2D = 5          # ID3D11Device
VT_ID3D11TEXTURE2D_GETDESC = 10              # ID3D11Texture2D
VT_ID3D11CONTEXT_MAP = 14                    # ID3D11DeviceContext
VT_ID3D11CONTEXT_UNMAP = 15
VT_ID3D11CONTEXT_COPYRESOURCE = 47


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def make_guid(value: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


IID_IDXGIDEVICE = make_guid("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
IID_IDXGIOutput1 = make_guid("00cddea8-939b-4b83-a340-a685226666cc")
IID_ID3D11Texture2D = make_guid("6f15aaf2-d208-4e89-9ab4-489535d34f9c")


class DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [
        ("LastPresentTime", ctypes.c_int64),
        ("LastMouseUpdateTime", ctypes.c_int64),
        ("AccumulatedFrames", ctypes.c_uint32),
        ("RectsCoalesced", wt.BOOL),
        ("ProtectedContentMaskedOut", wt.BOOL),
        ("PointerPosition", wt.POINT),
        ("PointerVisible", wt.BOOL),
        ("TotalMetadataBufferSize", ctypes.c_uint32),
        ("PointerShapeBufferSize", ctypes.c_uint32),
    ]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Quality", ctypes.c_uint32)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", ctypes.c_uint32), ("Height", ctypes.c_uint32),
        ("MipLevels", ctypes.c_uint32), ("ArraySize", ctypes.c_uint32),
        ("Format", ctypes.c_uint32), ("SampleDesc", DXGI_SAMPLE_DESC),
        ("Usage", ctypes.c_uint32), ("BindFlags", ctypes.c_uint32),
        ("CPUAccessFlags", ctypes.c_uint32), ("MiscFlags", ctypes.c_uint32),
    ]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", ctypes.c_uint32),
                ("DepthPitch", ctypes.c_uint32)]


class DxgiError(RuntimeError):
    pass


def _param_type(arg: Any):
    """给 WINFUNCTYPE 构造参数类型：只接受带 from_param 的 ctypes 类型。"""
    if arg is None:
        return ctypes.c_void_p
    if isinstance(arg, ctypes._Pointer):        # POINTER(...) 实例
        return type(arg)
    if isinstance(arg, int) and not isinstance(arg, bool):
        return ctypes.c_void_p
    return type(arg)


def _vtbl_call(ptr: int, index: int, *args, restype=ctypes.c_long):
    """按 vtable 索引调用 COM 方法（this 指针自动作为第一个参数）。

    参数一律传 ctypes.pointer(...) / ctypes.c_xxx(...) 实例，
    这样 type(arg) 一定是合法的 ctypes 类型。
    """
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn_addr = vtable[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *[_param_type(a) for a in args])
    return proto(fn_addr)(ctypes.c_void_p(ptr), *args)


def _release(ptr: Optional[int]) -> None:
    if ptr:
        try:
            _vtbl_call(ptr, VT_IUNKNOWN_RELEASE, restype=ctypes.c_ulong)
        except Exception:  # pragma: no cover
            pass


def _query_interface(ptr: int, iid: GUID) -> int:
    out = ctypes.c_void_p()
    hr = _vtbl_call(ptr, VT_IUNKNOWN_QUERYINTERFACE,
                    ctypes.pointer(iid), ctypes.pointer(out))
    if hr < 0 or not out.value:
        raise DxgiError(f"QueryInterface failed hr=0x{hr & 0xFFFFFFFF:08X}")
    return int(out.value)


class DxgiDuplicator:
    """DXGI Desktop Duplication（主显示器输出）最小可用实现。"""

    def __init__(self):
        self.device = 0
        self.context = 0
        self.dup = 0
        self.feature_level = 0
        self.adapter_name = ""
        self.output_desc: Dict[str, Any] = {}

    def open(self) -> None:
        d3d11 = ctypes.WinDLL("d3d11")
        d3d11.D3D11CreateDevice.restype = ctypes.c_long
        device = ctypes.c_void_p()
        context = ctypes.c_void_p()
        feature_level = ctypes.c_uint32()
        hr = d3d11.D3D11CreateDevice(
            None, D3D_DRIVER_TYPE_HARDWARE, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            None, 0, D3D11_SDK_VERSION,
            ctypes.byref(device), ctypes.byref(feature_level), ctypes.byref(context))
        if hr < 0 or not device.value:
            raise DxgiError(f"D3D11CreateDevice failed hr=0x{hr & 0xFFFFFFFF:08X}")
        self.device = int(device.value)
        self.context = int(context.value)
        self.feature_level = int(feature_level.value)

        dxgi_device = _query_interface(self.device, IID_IDXGIDEVICE)
        try:
            adapter = ctypes.c_void_p()
            hr = _vtbl_call(dxgi_device, VT_IDXGIDEVICE_GETADAPTER,
                            ctypes.pointer(adapter))
            if hr < 0 or not adapter.value:
                raise DxgiError(f"IDXGIDevice::GetAdapter failed hr=0x{hr & 0xFFFFFFFF:08X}")
            adapter_ptr = int(adapter.value)
        finally:
            _release(dxgi_device)

        output_ptr = 0
        try:
            output = ctypes.c_void_p()
            hr = _vtbl_call(adapter_ptr, VT_IDXGIADAPTER_ENUMOUTPUTS,
                            ctypes.c_uint(0), ctypes.pointer(output))
            if hr < 0 or not output.value:
                raise DxgiError(f"IDXGIAdapter::EnumOutputs failed hr=0x{hr & 0xFFFFFFFF:08X}")
            output_ptr = int(output.value)
            self.adapter_name = self._adapter_desc(adapter_ptr)
            self.output_desc = self._output_desc(output_ptr)
        finally:
            _release(adapter_ptr)

        try:
            output1 = _query_interface(output_ptr, IID_IDXGIOutput1)
        finally:
            _release(output_ptr)
        try:
            dup = ctypes.c_void_p()
            hr = _vtbl_call(output1, VT_IDXGIOUTPUT1_DUPLICATEOUTPUT,
                            ctypes.c_void_p(self.device), ctypes.pointer(dup))
            if hr < 0 or not dup.value:
                raise DxgiError(
                    f"IDXGIOutput1::DuplicateOutput failed hr=0x{hr & 0xFFFFFFFF:08X}")
            self.dup = int(dup.value)
        finally:
            _release(output1)
        logger.info("DXGI Desktop Duplication 就绪：adapter=%s 输出=%sx%s 坐标=%s",
                    self.adapter_name or "?", self.output_desc.get("width"),
                    self.output_desc.get("height"), self.output_desc.get("desktop_coordinates"))

    def _adapter_desc(self, adapter_ptr: int) -> str:
        class DXGI_ADAPTER_DESC(ctypes.Structure):
            _fields_ = [("Description", wt.WCHAR * 128),
                        ("VendorId", ctypes.c_uint32), ("DeviceId", ctypes.c_uint32),
                        ("SubSysId", ctypes.c_uint32), ("Revision", ctypes.c_uint32),
                        ("DedicatedVideoMemory", ctypes.c_size_t),
                        ("DedicatedSystemMemory", ctypes.c_size_t),
                        ("SharedSystemMemory", ctypes.c_size_t),
                        ("AdapterLuid", ctypes.c_int64)]

        desc = DXGI_ADAPTER_DESC()
        try:
            if _vtbl_call(adapter_ptr, VT_IDXGIADAPTER_GETDESC, ctypes.pointer(desc)) == 0:
                return desc.Description
        except Exception as exc:  # pragma: no cover
            logger.debug("IDXGIAdapter::GetDesc 失败：%s", exc)
        return ""

    def _output_desc(self, output_ptr: int) -> Dict[str, Any]:
        class DXGI_OUTPUT_DESC(ctypes.Structure):
            _fields_ = [("DeviceName", wt.WCHAR * 32), ("DesktopCoordinates", wt.RECT),
                        ("AttachedToDesktop", wt.BOOL), ("Rotation", ctypes.c_uint32),
                        ("Monitor", ctypes.c_void_p)]

        desc = DXGI_OUTPUT_DESC()
        try:
            if _vtbl_call(output_ptr, VT_IDXGIOUTPUT_GETDESC, ctypes.pointer(desc)) == 0:
                return {
                    "device_name": desc.DeviceName,
                    "desktop_coordinates": [
                        desc.DesktopCoordinates.left, desc.DesktopCoordinates.top,
                        desc.DesktopCoordinates.right, desc.DesktopCoordinates.bottom],
                    "attached_to_desktop": bool(desc.AttachedToDesktop),
                    "rotation": int(desc.Rotation),
                    "width": desc.DesktopCoordinates.right - desc.DesktopCoordinates.left,
                    "height": desc.DesktopCoordinates.bottom - desc.DesktopCoordinates.top,
                }
        except Exception as exc:  # pragma: no cover
            logger.debug("IDXGIOutput::GetDesc 失败：%s", exc)
        return {}

    def close(self) -> None:
        _release(self.dup)
        _release(self.context)
        _release(self.device)
        self.dup = self.context = self.device = 0

    def grab(self, timeout_ms: int = 1000, retries: int = 5,
             require_content: bool = True) -> Tuple[np.ndarray, Dict[str, Any]]:
        """抓一帧桌面。

        require_content=True 时会跳过"会话初始空帧"：DuplicateOutput 之后的
        第一帧可能出现 AccumulatedFrames=0 且整屏全黑（实测本机如此），
        直接使用会误判 DXGI 无效。
        """
        last = "未尝试"
        empty_frames = 0
        for attempt in range(1, retries + 1):
            info = DXGI_OUTDUPL_FRAME_INFO()
            resource = ctypes.c_void_p()
            hr = _vtbl_call(self.dup, VT_IDXGIOUTDUP_ACQUIRENEXTFRAME,
                            ctypes.c_uint(timeout_ms), ctypes.pointer(info),
                            ctypes.pointer(resource))
            if hr == DXGI_ERROR_WAIT_TIMEOUT:
                last = f"DXGI_ERROR_WAIT_TIMEOUT（第 {attempt} 次）"
                continue
            if hr == DXGI_ERROR_NOT_CURRENTLY_AVAILABLE:
                last = f"DXGI_ERROR_NOT_CURRENTLY_AVAILABLE（第 {attempt} 次）"
                time.sleep(0.2)
                continue
            if hr < 0 or not resource.value:
                last = f"AcquireNextFrame failed hr=0x{hr & 0xFFFFFFFF:08X}"
                time.sleep(0.1)
                continue
            accumulated = int(info.AccumulatedFrames)
            try:
                frame = self._copy_out(int(resource.value))
            finally:
                _vtbl_call(self.dup, VT_IDXGIOUTDUP_RELEASEFRAME, restype=ctypes.c_long)
                _release(int(resource.value))
            if require_content and accumulated == 0:
                empty_frames += 1
                last = f"空帧（AccumulatedFrames=0，第 {attempt} 次）"
                time.sleep(0.05)
                continue
            meta = {"attempts": attempt, "accumulated_frames": accumulated,
                    "skipped_empty_frames": empty_frames,
                    "protected_content_masked_out": bool(info.ProtectedContentMaskedOut)}
            return frame, meta
        raise DxgiError(f"DXGI 抓帧失败：{last}")

    def _copy_out(self, resource_ptr: int) -> np.ndarray:
        texture = _query_interface(resource_ptr, IID_ID3D11Texture2D)
        try:
            desc = D3D11_TEXTURE2D_DESC()
            hr = _vtbl_call(texture, VT_ID3D11TEXTURE2D_GETDESC, ctypes.pointer(desc))
            if hr < 0:
                raise DxgiError(
                    f"ID3D11Texture2D::GetDesc failed hr=0x{hr & 0xFFFFFFFF:08X}")
            staging_desc = D3D11_TEXTURE2D_DESC(
                desc.Width, desc.Height, 1, 1, desc.Format, DXGI_SAMPLE_DESC(1, 0),
                D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0)
            staging = ctypes.c_void_p()
            hr = _vtbl_call(self.device, VT_ID3D11DEVICE_CREATETEXTURE2D,
                            ctypes.pointer(staging_desc), None, ctypes.pointer(staging))
            if hr < 0 or not staging.value:
                raise DxgiError(
                    f"ID3D11Device::CreateTexture2D(staging) failed hr=0x{hr & 0xFFFFFFFF:08X}")
            try:
                _vtbl_call(self.context, VT_ID3D11CONTEXT_COPYRESOURCE,
                           ctypes.c_void_p(staging.value), ctypes.c_void_p(texture),
                           restype=None)
                mapped = D3D11_MAPPED_SUBRESOURCE()
                hr = _vtbl_call(self.context, VT_ID3D11CONTEXT_MAP,
                                ctypes.c_void_p(staging.value), ctypes.c_uint(0),
                                ctypes.c_uint(D3D11_MAP_READ), ctypes.c_uint(0),
                                ctypes.pointer(mapped))
                if hr < 0 or not mapped.pData:
                    raise DxgiError(
                        f"ID3D11DeviceContext::Map failed hr=0x{hr & 0xFFFFFFFF:08X}")
                try:
                    raw = (ctypes.c_ubyte * (mapped.RowPitch * desc.Height)).from_address(
                        mapped.pData)
                    wide = np.frombuffer(raw, dtype=np.uint8).reshape(
                        desc.Height, mapped.RowPitch)
                    frame = wide[:, : desc.Width * 4].reshape(
                        desc.Height, desc.Width, 4).copy()
                finally:
                    _vtbl_call(self.context, VT_ID3D11CONTEXT_UNMAP,
                               ctypes.c_void_p(staging.value), ctypes.c_uint(0), restype=None)
            finally:
                _release(int(staging.value))
        finally:
            _release(texture)
        return frame


# --------------------------------------------------------------------------- #
# 断言收集
# --------------------------------------------------------------------------- #
class Assertions:
    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def add(self, name: str, ok: Optional[bool], detail: str = "") -> bool:
        status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        self.items.append({"name": name, "status": status, "detail": detail})
        logger.info("%-6s %-40s %s", status, name, detail)
        return bool(ok)

    def has(self, name: str) -> bool:
        return any(item["name"] == name for item in self.items)

    @property
    def any_fail(self) -> bool:
        return any(item["status"] == "FAIL" for item in self.items)

    def to_list(self) -> List[Dict[str, Any]]:
        return list(self.items)


# --------------------------------------------------------------------------- #
# 窗口几何工具（DXGI 场景摆放 / 还原）
# --------------------------------------------------------------------------- #
class WindowGuard:
    """保存并尽力还原窗口几何与前台状态，避免污染用户桌面。"""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.rect = get_window_rect(hwnd)
        self.iconic = bool(user32.IsIconic(wt.HWND(hwnd)))
        self.zoomed = bool(user32.IsZoomed(wt.HWND(hwnd)))
        self.foreground = int(user32.GetForegroundWindow() or 0)

    def restore(self) -> bool:
        left, top, right, bottom = self.rect
        width, height = right - left, bottom - top
        if self.zoomed:
            user32.ShowWindow(wt.HWND(self.hwnd), SW_MAXIMIZE)
        elif self.iconic:
            user32.ShowWindow(wt.HWND(self.hwnd), SW_MINIMIZE)
        else:
            user32.ShowWindow(wt.HWND(self.hwnd), SW_RESTORE)
        ok = bool(user32.SetWindowPos(wt.HWND(self.hwnd), None, left, top, width, height,
                                      SWP_NOZORDER | SWP_NOACTIVATE))
        if self.zoomed:
            user32.ShowWindow(wt.HWND(self.hwnd), SW_MAXIMIZE)
        if self.foreground and self.foreground != self.hwnd:
            try:
                user32.SetForegroundWindow(wt.HWND(self.foreground))
            except Exception:
                pass
        return ok


def place_window(hwnd: int, left: int, top: int, width: int, height: int) -> None:
    if not user32.SetWindowPos(wt.HWND(hwnd), None, left, top, width, height,
                               SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW):
        raise OSError(ctypes.get_last_error(), "SetWindowPos failed")
    time.sleep(0.4)  # 等 DWM 合成稳定


def run_dxgi_scenario(dup: DxgiDuplicator, hwnd: int, label: str,
                      shots: ScreenshotBudget, move: bool,
                      monitor: Dict[str, Any],
                      wgc: Optional["WgcCapture"] = None) -> Dict[str, Any]:
    """在指定场景几何下评估 DXGI 降级链是否有效（真实抓帧，不构造数据）。"""
    monitor_rect = monitor.get("monitor_rect")
    result: Dict[str, Any] = {
        "scenario": label, "window_rect": None, "extended_frame_bounds": None,
        "monitor_rect": monitor_rect, "fully_on_single_monitor": None,
        "right_gap_px": None, "frame_acquired": False, "region_metrics": None,
        "region_size_match": None, "occlusion_visible_ratio": None,
        "similarity_to_wgc": None,
        "dxgi_usable": False, "effective": False, "note": "", "error": None,
    }
    if not monitor_rect:
        result["note"] = "无法获取显示器信息"
        return result

    if move:
        width, height = rect_size(get_window_rect(hwnd))
        m_left, m_top, m_right, m_bottom = monitor_rect
        avail_w, avail_h = m_right - m_left, m_bottom - m_top
        fit_w = min(width, max(200, avail_w - 2 * DWM_MARGIN))
        fit_h = min(height, max(200, avail_h - 2 * DWM_MARGIN))
        if label.startswith("A"):
            left, top = m_left + DWM_MARGIN, m_top + DWM_MARGIN
        else:
            left, top = m_right - DXGI_SCENARIO_RIGHT_GAP - fit_w, m_top + DWM_MARGIN
        try:
            place_window(hwnd, left, top, fit_w, fit_h)
        except OSError as exc:
            result["error"] = f"SetWindowPos failed: {exc}"
            return result

    window_rect = get_window_rect(hwnd)
    ext = get_extended_frame_bounds(hwnd) or window_rect
    client_rect = get_client_rect_on_screen(hwnd)
    result["window_rect"] = list(window_rect)
    result["extended_frame_bounds"] = list(ext)
    result["client_rect_on_screen"] = list(client_rect)
    result["fully_on_single_monitor"] = (rect_inside(window_rect, monitor_rect)
                                         and rect_inside(ext, monitor_rect))
    result["right_gap_px"] = int(monitor_rect[2] - window_rect[2])

    result["foreground_ok"] = put_window_to_foreground(hwnd)
    time.sleep(0.3)
    result["occlusion_visible_ratio"] = round(sample_occlusion_ratio(hwnd, window_rect), 3)

    # 同刻用 WGC 取一张客户区参考帧，用于交叉验证 DXGI 抓到的是同一画面
    reference_frame: Optional[np.ndarray] = None
    if wgc is not None:
        try:
            reference_frame = wgc.acquire(timeout_s=0.6)
        except Exception as exc:
            result["reference_frame_error"] = f"{type(exc).__name__}: {exc}"

    # 最多抓 3 次，取第一个"窗口区域非黑"的帧（桌面静止时可能只拿到空帧）
    region = None
    for attempt in range(1, 4):
        try:
            frame, meta = dup.grab(timeout_ms=1500, retries=5)
        except DxgiError as exc:
            result["error"] = str(exc)
            return result
        result["frame_acquired"] = True
        result["grab_meta"] = meta
        screen = dup.output_desc.get("desktop_coordinates",
                                     [0, 0, int(frame.shape[1]), int(frame.shape[0])])
        x0 = max(int(client_rect[0]) - int(screen[0]), 0)
        y0 = max(int(client_rect[1]) - int(screen[1]), 0)
        x1 = min(int(client_rect[2]) - int(screen[0]), int(frame.shape[1]))
        y1 = min(int(client_rect[3]) - int(screen[1]), int(frame.shape[0]))
        if x1 <= x0 or y1 <= y0:
            result["note"] = "窗口客户区完全落在桌面帧之外"
            return result
        region = frame[y0:y1, x0:x1]
        result["desktop_frame_size"] = [int(frame.shape[1]), int(frame.shape[0])]
        result["region_rect"] = [x0, y0, x1, y1]
        result["grab_attempts"] = attempt
        if not frame_metrics(region)["is_black"]:
            break
        time.sleep(0.25)

    metrics = frame_metrics(region)
    result["region_metrics"] = metrics
    expected_w, expected_h = rect_size(client_rect)
    result["region_size_match"] = (metrics["width"] == expected_w
                                   and metrics["height"] == expected_h)
    result["similarity_to_wgc"] = (
        round(image_similarity(reference_frame, region), 4)
        if reference_frame is not None else None)
    result["dxgi_usable"] = bool(not metrics["is_black"] and result["region_size_match"])
    result["effective"] = bool(result["fully_on_single_monitor"] and result["dxgi_usable"])
    if not result["effective"]:
        reasons = []
        if not result["fully_on_single_monitor"]:
            reasons.append("窗口未完全位于单一显示器可视区域内（SPEC 启用条件不满足）")
        if metrics["is_black"]:
            reasons.append(f"窗口区域为黑帧（mean={metrics['mean_luma']}）")
        if not result["region_size_match"]:
            reasons.append(
                f"区域尺寸不匹配 {metrics['width']}x{metrics['height']} != "
                f"{expected_w}x{expected_h}")
        result["note"] = "；".join(reasons)
    elif result["occlusion_visible_ratio"] is not None and result["occlusion_visible_ratio"] < 1.0:
        result["note"] = (f"窗口可见率 {result['occlusion_visible_ratio']}（部分被遮挡，"
                          f"DXGI 抓的是合成桌面，相似度仅供诊断）")
    shots.save(f"dxgi_{label}", region,
               note=f"DXGI {label} 客户区 {expected_w}x{expected_h}")
    return result


# --------------------------------------------------------------------------- #
# 最小化状态测试（要求 3：IsIconic 判定，不依赖 WGC 返回 None）
# --------------------------------------------------------------------------- #
def test_minimized(wgc: WgcCapture, hwnd: int) -> Dict[str, Any]:
    info: Dict[str, Any] = {"is_iconic": False, "exception": None, "frames_acquired": 0,
                            "frame_is_black": None, "note": ""}
    user32.ShowWindow(wt.HWND(hwnd), SW_MINIMIZE)
    deadline = time.perf_counter() + 2.0
    while time.perf_counter() < deadline and not user32.IsIconic(wt.HWND(hwnd)):
        time.sleep(0.05)
    info["is_iconic"] = bool(user32.IsIconic(wt.HWND(hwnd)))
    logger.info("最小化状态测试：IsIconic=%s，探测 %.1fs",
                info["is_iconic"], MINIMIZED_PROBE_SECONDS)
    frames: List[np.ndarray] = []
    probe_end = time.perf_counter() + MINIMIZED_PROBE_SECONDS
    try:
        while time.perf_counter() < probe_end:
            frame = wgc.acquire(timeout_s=0.4)
            if frame is not None:
                info["frames_acquired"] += 1
                frames.append(frame)
            else:
                time.sleep(0.05)
    except Exception as exc:
        info["exception"] = f"{type(exc).__name__}: {exc}"
        logger.error("最小化状态下 WGC 抛异常：%s", info["exception"])
    if frames:
        metrics = frame_metrics(frames[-1])
        info["frame_is_black"] = metrics["is_black"]
        info["last_frame_metrics"] = metrics
    info["note"] = ("最小化时必须用 IsIconic 判断，不能依赖 WGC 返回 None"
                    if info["is_iconic"] else "IsIconic 未能在 2s 内变为真")
    user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
    time.sleep(0.5)
    info["restored_capturing"] = wgc.is_capturing()
    return info


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ChatAssistant Spike #1：WGC 捕获 + DXGI 降级链验证（SPEC.md 6.1-1）")
    parser.add_argument("--frames", type=int, default=FRAME_COUNT_DEFAULT,
                        help="连续采集帧数（默认 100）")
    parser.add_argument("--interval-ms", type=int, default=FRAME_INTERVAL_MS_DEFAULT,
                        help="帧间隔毫秒（默认 100）")
    parser.add_argument("--no-move-window", action="store_true",
                        help="不临时移动 QQ 窗口；仅在当前几何满足时评估 DXGI 场景")
    parser.add_argument("--skip-minimize", action="store_true", help="跳过最小化状态测试")
    parser.add_argument("--skip-dxgi", action="store_true", help="跳过 DXGI 降级链测试")
    parser.add_argument("--only-dxgi", action="store_true",
                        help="只跑 DXGI 初始化 + 单帧抓取（调试用）")
    parser.add_argument("--verbose", action="store_true", help="终端输出 DEBUG 日志")
    return parser.parse_args(argv)


def finalize(report: Dict[str, Any], shots: ScreenshotBudget, started: float,
             assertions: Assertions) -> None:
    # ---- 文件隔离审计（要求 5/6）：每次运行结束都要给出结论 ----
    isolation = audit_isolation(_ISOLATION_BASELINE, snapshot_external_dirs())
    report["file_isolation"] = isolation
    if not assertions.has("file_isolation_no_external_write"):
        assertions.add(
            "file_isolation_no_external_write", isolation["clean"],
            f"tempfile={isolation['tempfile_gettempdir']}；"
            f"工作区外新增（可归因本项目）="
            f"{len(isolation['external_new_entries_attributable'])} 项，"
            f"（无法归因，可能系统/其他程序）="
            f"{len(isolation['external_new_entries_unattributed'])} 项")
    if not isolation["clean"]:
        report["result"] = "FAIL"
        logger.error("发现工作区外写入，按隔离约束停止并上报：%s",
                     isolation["external_new_entries_attributable"])

    report["screenshots"]["saved"] = shots.saved
    report["screenshots"]["skipped"] = shots.skipped
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
        logger.info("%-6s %-40s %s", item["status"], item["name"], item["detail"])
    if report.get("abort_reason"):
        logger.error("%s: ABORTED - %s", SPIKE_TAG, report["abort_reason"])
    logger.info("截图 %d 张（上限 %d）%s", len(shots.saved), shots.limit,
                f"，跳过 {len(shots.skipped)} 张" if shots.skipped else "")
    final = "PASS" if report["result"] == "PASS" else "FAIL"
    logger.info("%s: %s", SPIKE_TAG, final)
    if isolation["clean"]:
        print(f"文件隔离确认：所有临时文件与缓存均在工作区内（{WORKSPACE}），"
              f"未发现外部写入。", flush=True)
    else:
        print(f"文件隔离确认：检测到工作区外写入！"
              f"可归因本项目的条目："
              f"{isolation['external_new_entries_attributable']}；"
              f"无法归因（可能为系统/其他程序）："
              f"{len(isolation['external_new_entries_unattributed'])} 项。",
              flush=True)
    print(f"{SPIKE_TAG}: {final}", flush=True)


def build_report(args: argparse.Namespace, started: float) -> Dict[str, Any]:
    return {
        "spike": SPIKE_ID,
        "spec_ref": "SPEC.md 6.1-1 + 4.1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "args": vars(args),
        "workspace_paths": workspace_paths(),
        "file_isolation": {},
        "thresholds": {
            "black_mean_lt": BLACK_MEAN_LT,
            "black_std_lt": BLACK_STD_LT,
            "black_pixel_max_value": BLACK_PIXEL_MAX_VALUE,
            "black_pixel_ratio_gt": BLACK_PIXEL_RATIO_GT,
            "non_black_ratio_min": NON_BLACK_RATIO_MIN,
            "tracemalloc_growth_limit": TRACEMALLOC_GROWTH_LIMIT,
            "tracemalloc_baseline_frame": TRACEMALLOC_BASELINE_FRAME,
            "dxgi_right_gap_px": DXGI_RIGHT_GAP_PX,
        },
        "environment": {}, "qq_windows": [], "target_window": None,
        "wgc": {}, "minimized": {}, "dxgi": {},
        "assertions": [], "screenshots": {"saved": [], "skipped": [], "removed_previous": []},
        "abort_reason": None, "result": "FAIL",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    global _ISOLATION_BASELINE
    args = parse_args(argv)
    setup_logging(args.verbose)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # 文件隔离审计基线：必须在任何第三方库开始工作前拍摄
    _ISOLATION_BASELINE = snapshot_external_dirs()

    started = time.time()
    report = build_report(args, started)
    assertions = Assertions()
    shots = ScreenshotBudget()

    logger.info("=" * 78)
    logger.info("%s 开始（SPEC.md 6.1-1）", SPIKE_TAG)
    logger.info("=" * 78)

    report["environment"]["dpi_awareness"] = set_dpi_awareness()
    report["environment"].update(collect_environment())
    report["environment"]["monitor_count"] = get_monitor_count()
    report["environment"]["virtual_screen_rect"] = list(get_virtual_screen_rect())
    report["screenshots"]["removed_previous"] = shots.clean_previous()
    logger.info("环境：DPI=%s 显示器数=%d 虚拟桌面=%s",
                report["environment"]["dpi_awareness"],
                report["environment"]["monitor_count"],
                report["environment"]["virtual_screen_rect"])
    if report["screenshots"]["removed_previous"]:
        logger.info("已清理上一轮截图：%s", report["screenshots"]["removed_previous"])

    # ---------------- 1. 找 QQ 顶层窗口 ---------------- #
    qq_windows = find_qq_windows()
    report["qq_windows"] = [w.to_dict() for w in qq_windows[:MAX_LOGGED_WINDOWS]]
    logger.info("EnumWindows 找到 QQ.exe 顶层窗口 %d 个", len(qq_windows))
    for win in qq_windows[:MAX_LOGGED_WINDOWS]:
        logger.info("  hwnd=%d pid=%d visible=%s iconic=%s title=%r class=%s rect=%s client=%s",
                    win.hwnd, win.pid, win.visible, win.iconic, win.title, win.cls,
                    win.rect, win.client_size)
    target = pick_capture_target(qq_windows)
    assertions.add("qq_top_level_window_found", target is not None,
                   f"候选 {len(qq_windows)} 个" + (f"，选中 hwnd={target.hwnd}" if target else ""))
    if target is None:
        report["assertions"] = assertions.to_list()
        report["result"] = "FAIL"
        finalize(report, shots, started, assertions)
        return 1
    report["target_window"] = target.to_dict()
    hwnd = target.hwnd
    logger.info("捕获目标：hwnd=%d title=%r class=%s 客户区=%s（物理像素）",
                hwnd, target.title, target.cls, target.client_size)

    guard = WindowGuard(hwnd)
    wgc: Optional[WgcCapture] = None
    dup: Optional[DxgiDuplicator] = None
    try:
        if target.iconic:
            logger.info("目标窗口处于最小化状态，先恢复以完成采集")
            user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
            time.sleep(0.6)

        expected_client = get_client_rect_size(hwnd)
        report["target_window"]["client_origin_on_screen"] = list(
            get_client_origin_on_screen(hwnd))

        # 调试捷径：只验证 DXGI 降级链
        if args.only_dxgi:
            dup = DxgiDuplicator()
            try:
                dup.open()
                frame, meta = dup.grab(timeout_ms=2000, retries=5)
                logger.info("DXGI 单帧：%sx%s meta=%s", frame.shape[1], frame.shape[0], meta)
                shots.save("dxgi_only_frame", frame, "DXGI 整屏单帧")
                report["dxgi"] = {"init_error": None, "adapter": dup.adapter_name,
                                  "output": dup.output_desc, "supported": True,
                                  "grab_meta": meta,
                                  "frame_size": [int(frame.shape[1]), int(frame.shape[0])]}
                assertions.add("dxgi_initialized", True, "调试模式：仅验证 DXGI")
                report["result"] = "PASS"
            except Exception as exc:
                logger.error("DXGI 调试失败：%s: %s", type(exc).__name__, exc)
                report["dxgi"] = {"init_error": f"{type(exc).__name__}: {exc}",
                                  "supported": False}
                assertions.add("dxgi_initialized", False, str(exc))
                report["result"] = "FAIL"
            report["assertions"] = assertions.to_list()
            finalize(report, shots, started, assertions)
            return 0 if report["result"] == "PASS" else 1

        # ---------------- 2. wgc_python 初始化 ---------------- #
        tracemalloc.start()
        init_error: Optional[str] = None
        try:
            wgc = WgcCapture(title=target.title, class_name=target.cls, client_area_only=True)
        except Exception as exc:
            init_error = f"{type(exc).__name__}: {exc}"
        report["wgc"]["init_error"] = init_error
        if init_error is not None:
            # 规则：初始化失败 → 立刻停止并汇报，绝不自行改降级链
            logger.error("wgc_python 初始化失败：%s", init_error)
            assertions.add("wgc_init", False, init_error)
            report["abort_reason"] = f"WGC_INIT_FAILED: {init_error}"
            report["result"] = "ABORT"
            report["assertions"] = assertions.to_list()
            finalize(report, shots, started, assertions)
            return 2
        assertions.add("wgc_init", True,
                       f"title={target.title!r} class={target.cls} client_area_only=True")

        # ---------------- 3. 连续采集帧 ---------------- #
        frames_acquired = 0
        no_frame_count = 0
        exceptions: List[str] = []
        sizes: Dict[str, int] = {}
        tm_baseline: Optional[int] = None
        tm_baseline_peak: Optional[int] = None
        rss_baseline: Optional[int] = None
        first_black_frame: Optional[np.ndarray] = None
        screenshot_frames: List[Dict[str, Any]] = []
        frames_requested = args.frames
        interval_s = args.interval_ms / 1000.0
        # 需求规定基线取第 10 帧；帧数少于 10 时退化为中点，避免基线缺失
        baseline_frame = (TRACEMALLOC_BASELINE_FRAME
                          if frames_requested > TRACEMALLOC_BASELINE_FRAME
                          else max(2, frames_requested // 2))

        # 定长预分配：逐帧诊断数据不随帧数增长，避免把 spike 自己的簿记内存
        # 算进"采集管线内存增长"（实测 numpy 缓冲同样被 tracemalloc 计入）。
        metrics_arr = np.zeros((frames_requested, 5), dtype=np.float64)
        #               列: mean, std, black_ratio, is_black, acquired
        tm_samples = np.zeros((frames_requested, 2), dtype=np.int64)
        #               列: tracemalloc current, tracemalloc allocator peak
        rss_samples_arr = np.full(frames_requested, -1, dtype=np.int64)
        loop_start = time.perf_counter()

        logger.info("开始连续采集：%d 帧 / 间隔 %dms", frames_requested, args.interval_ms)
        for index in range(1, frames_requested + 1):
            tick = time.perf_counter()
            try:
                frame = wgc.acquire(timeout_s=max(interval_s, 0.05))
            except Exception as exc:  # 采集异常必须显式记录，不许吞掉
                exceptions.append(f"frame {index}: {type(exc).__name__}: {exc}")
                logger.error("第 %d 帧采集异常：%s: %s", index, type(exc).__name__, exc)
                frame = None
            if frame is None:
                no_frame_count += 1
            else:
                frames_acquired += 1
                metrics = frame_metrics(frame)
                row = index - 1
                metrics_arr[row, 0] = metrics["mean_luma"]
                metrics_arr[row, 1] = metrics["std_luma"]
                metrics_arr[row, 2] = metrics["black_pixel_ratio"]
                metrics_arr[row, 3] = 1.0 if metrics["is_black"] else 0.0
                metrics_arr[row, 4] = 1.0
                key = f"{metrics['width']}x{metrics['height']}"
                sizes[key] = sizes.get(key, 0) + 1
                if metrics["is_black"] and first_black_frame is None:
                    first_black_frame = frame.copy()
                    # 黑帧诊断价值最高，发现即写盘（截图预算仍受 5 张上限约束）
                    if shots.save("wgc_black_frame", first_black_frame,
                                  f"第 {index} 帧（黑帧，最高优先级诊断）"):
                        screenshot_frames.append({"index": index, "file": shots.saved[-1]["file"]})
                elif index in (1, max(1, frames_requested // 2), frames_requested):
                    # 截图立即落盘，循环内不保留任何帧副本（否则会污染内存基线）
                    if shots.save(f"wgc_frame_{index:03d}", frame,
                                  f"第 {index} 帧（WGC 客户区）"):
                        screenshot_frames.append({"index": index, "file": shots.saved[-1]["file"]})
                del frame
            if index >= baseline_frame:
                # 基线与后续采样必须完全同相位：都在"本帧缓冲已释放 + 先 GC"之后读，
                # 否则 ctypes/numpy 临时对象的 GC 时机差异会被当成长率波动。
                gc.collect()
                current, allocator_peak = tracemalloc.get_traced_memory()
                if index == baseline_frame:
                    # "第10帧"这一定义要同时记录两种量：
                    #   - 分配器峰值高水位 peak_so_far（MB 量级，抗 GC 时机噪声）
                    #   - 当前存活量 current（KB 量级，仅作诊断）
                    tm_baseline = int(current)
                    tm_baseline_peak = int(allocator_peak)
                    tracemalloc.reset_peak()
                    rss_baseline = get_process_working_set()
                    logger.info("第 %d 帧内存基线：tracemalloc current=%d, peak=%d, RSS=%d",
                                index, tm_baseline, tm_baseline_peak, rss_baseline)
                tm_samples[index - 1, 0] = int(current)
                tm_samples[index - 1, 1] = int(allocator_peak)
                rss_samples_arr[index - 1] = get_process_working_set()
            sleep_for = interval_s - (time.perf_counter() - tick)
            if sleep_for > 0:
                time.sleep(sleep_for)
        loop_duration = time.perf_counter() - loop_start
        # 每次采样都会先 gc.collect()，故此处 close 掉采样期后再读最终峰值
        current_end, peak_after = tracemalloc.get_traced_memory()
        trace_top = tracemalloc_top()  # 必须在展开逐帧报告之前取样

        # 主指标（用户要求）：tracemalloc 峰值相对第 10 帧的增长率。
        # 分母用第 10 帧的分配器峰值高水位（MB 量级），这是 tracemalloc 里
        # 能稳定表征"采集管线内存水位"的量；用 KB 量级的 current 作分母会让
        # ctypes/interpreter 的微小抖动变成几十个百分点的假增长。
        peak_growth = None
        if tm_baseline_peak:
            peak_growth = (int(peak_after) - tm_baseline_peak) / tm_baseline_peak

        # 诊断指标：同相位 current 序列的峰值增长（分母很小，仅供参考）
        sampled_slice = tm_samples[baseline_frame - 1:]
        sampled_peak = int(sampled_slice[:, 0].max()) if sampled_slice.size else 0
        allocator_peak = int(sampled_slice[:, 1].max()) if sampled_slice.size else 0
        live_growth = (sampled_peak - tm_baseline) / tm_baseline if tm_baseline else None
        rss_samples = [int(value) for value in rss_samples_arr if value > 0]
        rss_growth = None
        if rss_baseline and rss_baseline > 0 and rss_samples:
            rss_growth = (max(rss_samples) - rss_baseline) / rss_baseline

        # 内存判定结束后再展开逐帧报告（展开动作本身不再影响上面的测量）
        frame_metrics_list: List[Dict[str, Any]] = [
            {
                "index": i + 1,
                "acquired": bool(metrics_arr[i, 4]),
                "mean_luma": round(float(metrics_arr[i, 0]), 3),
                "std_luma": round(float(metrics_arr[i, 1]), 3),
                "black_pixel_ratio": round(float(metrics_arr[i, 2]), 6),
                "is_black": bool(metrics_arr[i, 3]),
            }
            for i in range(frames_requested)
        ]
        black_frames = [m for m in frame_metrics_list
                        if m["acquired"] and m["is_black"]]
        non_black_ratio = ((frames_acquired - len(black_frames)) / frames_acquired
                           if frames_acquired else 0.0)
        dominant_size = max(sizes, key=sizes.get) if sizes else None

        report["wgc"].update({
            "title_used_for_match": target.title,
            "class_used_for_match": target.cls,
            "client_area_only": True,
            "frames_requested": frames_requested,
            "interval_ms": args.interval_ms,
            "frames_acquired": frames_acquired,
            "no_frame_count": no_frame_count,
            "exceptions": exceptions,
            "captured_size_dominant": dominant_size,
            "captured_size_counts": sizes,
            "expected_client_size": list(expected_client),
            "loop_duration_s": round(loop_duration, 3),
            "avg_period_ms": round(loop_duration / frames_requested * 1000, 2)
            if frames_requested else None,
            "black_frame_count": len(black_frames),
            "black_frame_ratio": round(len(black_frames) / frames_acquired, 4)
            if frames_acquired else None,
            "non_black_ratio": round(non_black_ratio, 4),
            "tracemalloc": {
                "baseline_frame": baseline_frame,
                "peak_baseline_bytes": tm_baseline_peak,
                "peak_after_bytes": int(peak_after),
                "peak_growth_ratio": round(peak_growth, 4) if peak_growth is not None else None,
                "live_baseline_bytes": tm_baseline,
                "live_peak_bytes": sampled_peak,
                "live_current_end_bytes": int(current_end),
                "live_growth_ratio": round(live_growth, 4) if live_growth is not None else None,
                "live_sampled_allocator_peak_bytes": allocator_peak,
                "limit": TRACEMALLOC_GROWTH_LIMIT,
                "peak_definition": (
                    "peak_growth_ratio = (结束时的 tracemalloc 分配器峰值 - 第10帧时的"
                    "分配器峰值) / 第10帧时的分配器峰值，均为 MB 量级高水位，"
                    "对 GC 时机不敏感；live_growth_ratio 为同相位 gc.collect() 后 "
                    "current 的峰值增长，分母仅 KB 量级，只作诊断不参与判定"),
            },
            "rss_bytes": {
                "baseline_frame": TRACEMALLOC_BASELINE_FRAME,
                "baseline": rss_baseline,
                "max_after_baseline": max(rss_samples) if rss_samples else None,
                "growth_ratio": round(rss_growth, 4) if rss_growth is not None else None,
                "note": "RSS 为补充诊断；判定主指标按需求使用 tracemalloc",
            },
            "frame_metrics": frame_metrics_list,
            "screenshot_frames": screenshot_frames,
        })
        report["wgc"]["tracemalloc"]["top_allocations"] = trace_top
        logger.info("采集完成：success=%d/%d no_frame=%d 异常=%d 平均周期=%.1fms",
                    frames_acquired, frames_requested, no_frame_count, len(exceptions),
                    report["wgc"]["avg_period_ms"] or 0.0)

        del first_black_frame

        # ---------------- 4. 断言：帧数 / 尺寸 / 黑帧 / 内存 ---------------- #
        assertions.add("wgc_frames_acquired",
                       frames_acquired >= int(frames_requested * MIN_FRAMES_ACQUIRED_RATIO)
                       and not exceptions,
                       f"{frames_acquired}/{frames_requested} 帧，异常 {len(exceptions)} 次")
        size_ok = (dominant_size == f"{expected_client[0]}x{expected_client[1]}"
                   if dominant_size else False)
        assertions.add("wgc_client_size_match", size_ok,
                       f"捕获 {dominant_size} vs GetClientRect "
                       f"{expected_client[0]}x{expected_client[1]}")
        assertions.add("wgc_non_black_ratio", non_black_ratio >= NON_BLACK_RATIO_MIN,
                       f"非黑帧占比 {non_black_ratio:.2%}（阈值 >= "
                       f"{NON_BLACK_RATIO_MIN:.0%}），黑帧 {len(black_frames)}/{frames_acquired}")
        assertions.add("wgc_tracemalloc_peak_growth",
                       peak_growth is not None and peak_growth < TRACEMALLOC_GROWTH_LIMIT,
                       (f"峰值增长率 {peak_growth:.2%}"
                        f"（{tm_baseline_peak} -> {int(peak_after)} bytes）"
                        if peak_growth is not None else "未取到峰值基线")
                       + f"，阈值 < {TRACEMALLOC_GROWTH_LIMIT:.0%}")
        assertions.add("wgc_rss_growth",
                       rss_growth is not None and rss_growth < TRACEMALLOC_GROWTH_LIMIT,
                       (f"RSS 增长率 {rss_growth:.2%}（SPEC 4.1 基线=第10帧 RSS）"
                        if rss_growth is not None else "未取到 RSS 基线")
                       + f"，阈值 < {TRACEMALLOC_GROWTH_LIMIT:.0%}")

        # 规则：捕获全黑 → 立刻停止并汇报，不自行修改降级链
        if frames_acquired > 0 and len(black_frames) == frames_acquired:
            logger.error("WGC 捕获到的 %d 帧全部为黑帧，按规则停止并上报", frames_acquired)
            report["abort_reason"] = "WGC_ALL_FRAMES_BLACK"
            report["result"] = "ABORT"
            report["assertions"] = assertions.to_list()
            finalize(report, shots, started, assertions)
            return 2
        if frames_acquired == 0:
            logger.error("WGC 在 %d 帧内没有返回任何帧", frames_requested)
            report["abort_reason"] = "WGC_NO_FRAME_RETURNED"
            report["result"] = "ABORT"
            report["assertions"] = assertions.to_list()
            finalize(report, shots, started, assertions)
            return 2

        # ---------------- 5. 最小化状态测试 ---------------- #
        if args.skip_minimize:
            assertions.add("wgc_minimized_no_exception", None, "被 --skip-minimize 跳过")
            report["minimized"] = {"skipped": True}
        else:
            minimized = test_minimized(wgc, hwnd)
            report["minimized"] = minimized
            assertions.add("wgc_minimized_no_exception",
                           bool(minimized["is_iconic"]) and minimized["exception"] is None,
                           f"IsIconic={minimized['is_iconic']} "
                           f"异常={minimized['exception']} "
                           f"最小化期间帧数={minimized['frames_acquired']}")

        # ---------------- 6. DXGI 降级链测试 ---------------- #
        if args.skip_dxgi:
            for name in ("dxgi_initialized", "dxgi_effective_single_monitor_visible",
                         "dxgi_effective_near_right_edge"):
                assertions.add(name, None, "被 --skip-dxgi 跳过")
            report["dxgi"] = {"skipped": True}
        else:
            dup = DxgiDuplicator()
            dxgi_init_error: Optional[str] = None
            try:
                dup.open()
            except Exception as exc:
                dxgi_init_error = f"{type(exc).__name__}: {exc}"
            report["dxgi"] = {
                "init_error": dxgi_init_error, "adapter": dup.adapter_name,
                "output": dup.output_desc, "supported": dxgi_init_error is None,
                "feature_level": dup.feature_level, "scenario_a": {}, "scenario_b": {},
            }
            assertions.add("dxgi_initialized", dxgi_init_error is None,
                           dxgi_init_error or "Desktop Duplication 就绪")
            if dxgi_init_error is not None:
                assertions.add("dxgi_effective_single_monitor_visible", False,
                               "DXGI 不可用")
                assertions.add("dxgi_effective_near_right_edge", False, "DXGI 不可用")
            else:
                monitor = get_monitor_info(hwnd)
                report["dxgi"]["monitor"] = monitor
                move = not args.no_move_window
                logger.info("DXGI 场景测试：move_window=%s monitor=%s",
                            move, monitor.get("monitor_rect"))
                try:
                    scenario_a = run_dxgi_scenario(
                        dup, hwnd, "A_single_monitor_visible", shots, move, monitor,
                        wgc=wgc)
                    report["dxgi"]["scenario_a"] = scenario_a
                    assertions.add(
                        "dxgi_effective_single_monitor_visible", scenario_a["effective"],
                        f"完全可见={scenario_a['fully_on_single_monitor']} "
                        f"抓帧={scenario_a['frame_acquired']} "
                        f"遮挡可见率={scenario_a.get('occlusion_visible_ratio')}"
                        f" 与WGC相似度={scenario_a.get('similarity_to_wgc')}"
                        + (f"｜{scenario_a['note']}" if scenario_a["note"] else ""))

                    scenario_b = run_dxgi_scenario(
                        dup, hwnd, "B_near_right_edge", shots, move, monitor,
                        wgc=wgc)
                    report["dxgi"]["scenario_b"] = scenario_b
                    assertions.add(
                        "dxgi_effective_near_right_edge", scenario_b["effective"],
                        f"右边缘间隙={scenario_b.get('right_gap_px')}px"
                        f"（场景要求 <{DXGI_RIGHT_GAP_PX}px）"
                        f" 完全可见={scenario_b['fully_on_single_monitor']} "
                        f"抓帧={scenario_b['frame_acquired']} "
                        f"遮挡可见率={scenario_b.get('occlusion_visible_ratio')}"
                        f" 与WGC相似度={scenario_b.get('similarity_to_wgc')}"
                        + (f"｜{scenario_b['note']}" if scenario_b["note"] else ""))
                except Exception as exc:
                    logger.error("DXGI 场景测试异常：%s: %s", type(exc).__name__, exc)
                    report["dxgi"]["scenario_error"] = f"{type(exc).__name__}: {exc}"
                    for name in ("dxgi_effective_single_monitor_visible",
                                 "dxgi_effective_near_right_edge"):
                        if not assertions.has(name):
                            assertions.add(name, False, f"异常：{exc}")
    finally:
        if wgc is not None:
            wgc.close()
        if dup is not None:
            dup.close()
        try:
            restored = guard.restore()
            logger.info("窗口几何已还原：%s（原始 %s）", restored, guard.rect)
            report["window_restored"] = restored
        except Exception as exc:
            logger.error("窗口还原失败：%s", exc)
            report["window_restored"] = False

    report["assertions"] = assertions.to_list()
    failed = assertions.any_fail
    report["result"] = "FAIL" if failed else "PASS"
    finalize(report, shots, started, assertions)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
