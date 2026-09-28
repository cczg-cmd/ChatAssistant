#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从魔搭（ModelScope）下载 GGUF（用户口径：魔搭速度最快）。

用法：python tmp/download_modelscope.py <owner/model> <仓库内文件名> <本地文件名>
例：  python tmp/download_modelscope.py Qwen/Qwen3-4B-Instruct-2507-GGUF \
            Qwen3-4B-Instruct-2507-Q4_K_M.gguf models/qwen3-4b-instruct-2507-q4_k_m.gguf
落到工作区 models/ 内，带断点续传与进度日志。
"""

from __future__ import annotations

import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def url_for(repo: str, file_path: str, revision: str = "master") -> str:
    query = urllib.parse.urlencode({"Revision": revision, "FilePath": file_path})
    return f"https://modelscope.cn/api/v1/models/{repo}/repo?{query}"


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    repo, remote, out_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    out = Path(out_arg)
    out = out if out.is_absolute() else ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    url = url_for(repo, remote)
    print(f"仓库 {repo}｜文件 {remote}\nURL {url}\n保存到 {out}", flush=True)

    have = out.stat().st_size if out.exists() else 0
    request = urllib.request.Request(url)
    if have:
        request.add_header("Range", f"bytes={have}-")
    t0 = time.perf_counter()
    with urllib.request.urlopen(request, timeout=60) as response:
        status = getattr(response, "status", 200)
        total = response.headers.get("Content-Length")
        total = int(total) + have if total else None
        print(f"HTTP {status}｜Content-Type={response.headers.get('Content-Type')}"
              f"｜总大小≈{total}", flush=True)
        if have and status != 206:
            print("服务器不支持断点续传，从头下载", flush=True)
            have = 0
        mode = "ab" if have else "wb"
        written, last = have, 0.0
        with out.open(mode) as handle:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                now = time.perf_counter()
                if now - last > 3:
                    last = now
                    speed = (written - have) / 1024 / 1024 / max(0.001, now - t0)
                    pct = f"{written * 100 / total:.1f}%" if total else "?"
                    print(f"  {written / 2**20:.1f} MB / {pct}｜{speed:.1f} MB/s", flush=True)
    size_gb = out.stat().st_size / 2**30
    print(f"完成：{out}（{size_gb:.2f} GB，用时 {time.perf_counter() - t0:.0f}s）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
