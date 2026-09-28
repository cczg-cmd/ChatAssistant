#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让正在跑的应用**优雅退出**，并测量从发出退出到进程消失的耗时（诊断用）。

做法：给应用主线程 PostThreadMessage(WM_QUIT) —— 等价于"从托盘菜单选退出"，
会走 aboutToQuit → shutdown 的完整收尾路径（不是 TerminateProcess）。
用法：python tools/quit_app.py           # 自动找 main.py 进程
用途：排查"退出慢"——对照日志里的 `退出计时：xxx` 行，看是收尾哪一步慢，
      还是卡在收尾之后（Qt/解释器/DLL 卸载）。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

WM_QUIT = 0x0012
u32 = ctypes.windll.user32


def find_pid() -> int:
    import subprocess
    # 同时支持开发版（python main.py）和打包版（ChatAssistant.exe）
    out = subprocess.run(["wmic", "process", "where",
                          "name='python.exe' or name='ChatAssistant.exe'",
                          "get", "ProcessId,CommandLine", "/format:csv"],
                         capture_output=True, text=True, errors="ignore").stdout
    for line in out.splitlines():
        low = line.lower()
        if ("main.py" in low and "qqchatassistant" in low) or "chatassistant.exe" in low:
            parts = [p for p in line.split(",") if p.strip()]
            for part in parts:
                if part.strip().isdigit():
                    return int(part.strip())
    return 0


def thread_of(pid: int) -> int:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.append(u32.GetWindowThreadProcessId(hwnd, None))
        return True

    u32.EnumWindows(cb, 0)
    return found[0] if found else 0


def main() -> int:
    pid = find_pid()
    if not pid:
        print("没找到 main.py 进程")
        return 1
    tid = thread_of(pid)
    if not tid:
        print(f"pid={pid} 没有顶层窗口，拿不到线程 id")
        return 1
    t0 = time.perf_counter()
    ok = u32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
    print(f"pid={pid} tid={tid} PostThreadMessage(WM_QUIT)={bool(ok)} → 开始计时")
    while time.perf_counter() - t0 < 15:
        alive = False
        try:
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # 查询权限
            if h:
                code = wintypes.DWORD()
                ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                alive = code.value == 259        # STILL_ACTIVE
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            alive = False
        if not alive:
            print(f"进程已退出，总耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
            return 0
        time.sleep(0.02)
    print(f"15 秒内还没退出（计时至 {(time.perf_counter() - t0) * 1000:.0f}ms）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
