#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫一遍"只出现在 import 行、正文里再没出现过"的名字（纯读，不改任何文件）。

用途：维护期清理冗余 import。注意这是**粗筛**：`from __future__ import annotations`
下写在类型注解里的名字、`getattr` 里当字符串用的名字都不会被算作"用到"，
所以删之前要人工确认。用法：python tools/check_unused_imports.py
"""

from __future__ import annotations

import ast
import io
import os
import re

DIRS = (".", "core", "ui", "utils", "tools", "spikes")


def main() -> int:
    rows = []
    for directory in DIRS:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not (name.endswith(".py") and os.path.isfile(path)):
                continue
            source = io.open(path, encoding="utf-8").read()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            imported = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported[(alias.asname or alias.name).split(".")[0]] = node.lineno
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name != "*":
                            imported[alias.asname or alias.name] = node.lineno
            for name_, line in imported.items():
                if name_ == "annotations":
                    continue
                hits = len(re.findall(r"\b" + re.escape(name_) + r"\b", source))
                if hits <= 1:
                    rows.append((path, line, name_))
    for path, line, name_ in rows:
        print("%-30s line %-4d %s" % (path, line, name_))
    print("共 %d 个（粗筛结果；类型注解/getattr 字符串会被误报）" % len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
