#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 config.json 的 paths.model 指到指定模型（只改这一个键，其余原样写回）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：set_model.py models/xxx.gguf")
        return 2
    path = ROOT / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    before = dict(data.get("paths") or {})
    data["paths"]["model"] = sys.argv[1]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("paths.model:", before.get("model"), "->", data["paths"]["model"])
    other = [k for k in set(before) | set(data["paths"])
             if json.dumps(before.get(k)) != json.dumps(data["paths"].get(k))]
    print("paths 段变化的键:", other)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
