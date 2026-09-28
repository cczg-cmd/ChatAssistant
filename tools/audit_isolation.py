#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作区外写入审计（SPEC 第九章承诺的 file_isolation_no_external_write）。

用法：
  python tmp/audit_isolation.py snapshot   # 记录基线（重启应用之前跑）
  python tmp/audit_isolation.py diff       # 与基线对比（应用跑一会儿之后再跑）

审计范围（只看"该由应用负责"的位置，不扫整个 C 盘）：
  %TEMP% 的新增/变化文件、%APPDATA%/%LOCALAPPDATA% 的新增顶层条目、
  %USERPROFILE% 下的新增点目录（.cache/.modelscope/.ollama 这类），
  以及工作区外是否出现名字含 ChatAssistant 的目录。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "tmp" / "isolation_baseline.json"
MAX_ENTRIES = 4000


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or str(Path.home()))


def scan_temp() -> dict:
    temp = Path(os.environ.get("TEMP") or (home() / "AppData" / "Local" / "Temp"))
    out: dict = {}
    try:
        for entry in temp.iterdir():
            try:
                stat = entry.stat()
            except OSError:
                continue
            out[entry.name] = f"{'d' if entry.is_dir() else 'f'}:{stat.st_size}:{int(stat.st_mtime)}"
            if len(out) >= MAX_ENTRIES:
                break
    except OSError:
        pass
    return out


def scan_top(dir_path: Path) -> list:
    names = []
    try:
        for entry in dir_path.iterdir():
            names.append(entry.name)
    except OSError:
        pass
    return sorted(names)


def snapshot() -> dict:
    h = home()
    return {
        "temp": scan_temp(),
        "appdata": scan_top(h / "AppData" / "Roaming"),
        "localappdata": scan_top(h / "AppData" / "Local"),
        "profile_dots": sorted(p.name for p in h.iterdir()
                               if p.is_dir() and p.name.startswith(".")),
    }


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "diff"
    data = snapshot()
    if mode == "snapshot":
        BASE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"已记录基线：TEMP {len(data['temp'])} 项、"
              f"APPDATA {len(data['appdata'])} 项、LOCALAPPDATA {len(data['localappdata'])} 项、"
              f"点目录 {len(data['profile_dots'])} 项 → {BASE}")
        return 0
    if not BASE.exists():
        print("还没有基线，先跑 snapshot")
        return 1
    old = json.loads(BASE.read_text(encoding="utf-8"))
    problems: list[str] = []
    new_temp = [k for k in data["temp"] if k not in old["temp"]]
    changed_temp = [k for k, v in data["temp"].items()
                    if k in old["temp"] and old["temp"][k] != v]
    new_appdata = [n for n in data["appdata"] if n not in old["appdata"]]
    new_local = [n for n in data["localappdata"] if n not in old["localappdata"]]
    new_dots = [n for n in data["profile_dots"] if n not in old["profile_dots"]]
    print(f"TEMP 新增 {len(new_temp)} / 变化 {len(changed_temp)}")
    for name in new_temp[:15]:
        print(f"   + {name}")
    if new_appdata:
        print(f"APPDATA 新增顶层条目：{new_appdata}")
        problems += [f"APPDATA/{n}" for n in new_appdata]
    if new_local:
        print(f"LOCALAPPDATA 新增顶层条目：{new_local}")
        problems += [f"LOCALAPPDATA/{n}" for n in new_local]
    if new_dots:
        print(f"家目录新增点目录：{new_dots}")
        problems += [f"~/{n}" for n in new_dots]
    suspicious = [n for n in new_temp if "chat" in n.lower() or "qqchat" in n.lower()]
    if suspicious:
        print(f"TEMP 里疑似本应用的新文件：{suspicious}")
        problems += [f"TEMP/{n}" for n in suspicious]
    print("file_isolation_no_external_write=" + ("FAIL" if problems else "PASS"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
