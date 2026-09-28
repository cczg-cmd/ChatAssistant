#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在魔搭上探仓库是否存在、有哪些文件（用官方 API，不用搜索页）。

用法：python tools/probe_modelscope_repo.py <owner/model> [<owner/model> ...]
"""

from __future__ import annotations

import json
import sys
import urllib.request


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main() -> int:
    repos = sys.argv[1:] or ["LiquidAI/LFM2.5-2.6B-GGUF"]
    for repo in repos:
        try:
            data = (get(f"https://modelscope.cn/api/v1/models/{repo}").get("Data") or {})
        except Exception as exc:
            print(f"{repo}: 不可用（{type(exc).__name__}）")
            continue
        name = data.get("Name")
        print(f"{repo}: 存在 ✓｜Name={name}｜下载量={data.get('Downloads')}")
        for key in ("Files", "Revision", "DefaultRevision"):
            value = data.get(key)
            if isinstance(value, list) and value:
                sample = [x.get("Path") if isinstance(x, dict) else x for x in value[:10]]
                print(f"    {key}: {sample}")
        # 常见的"文件清单"端点，拿 GGUF 列表
        for endpoint in ("repo/files?Revision=master", "repo?Revision=master"):
            try:
                info = get(f"https://modelscope.cn/api/v1/models/{repo}/{endpoint}")
            except Exception:
                continue
            files = (info.get("Data") or {}).get("Files") or info.get("Data") or []
            gguf = []
            if isinstance(files, list):
                for item in files:
                    path = item.get("Path") if isinstance(item, dict) else str(item)
                    if path and str(path).lower().endswith(".gguf"):
                        size = (item.get("Size") if isinstance(item, dict) else 0) or 0
                        gguf.append(f"{path}（{size/1024**3:.2f}GB）" if size else str(path))
            if gguf:
                print(f"    GGUF 文件：{gguf[:12]}")
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
