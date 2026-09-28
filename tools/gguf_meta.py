#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 GGUF 头部读取（不加载模型、秒级）：只看 general.name / architecture /
file_type / chat_template 里有没有 enable_thinking —— 用来确认磁盘上那几个
Qwen3 到底是"混合思考版"还是 "Instruct-2507 非思考版"。"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

TYPE_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
WANTED = ("general.name", "general.architecture", "general.size_label", "general.file_type",
          "tokenizer.chat_template", "general.quantization_version")


def read_string(handle) -> str:
    (length,) = struct.unpack("<Q", handle.read(8))
    return handle.read(length).decode("utf-8", "replace")


def read_value(handle, value_type: int):
    if value_type == 8:                     # string
        return read_string(handle)
    if value_type == 9:                     # array
        (item_type,) = struct.unpack("<I", handle.read(4))
        (count,) = struct.unpack("<Q", handle.read(8))
        if item_type == 8:
            # 数组元素是变长字符串，必须**全部读掉**才能保持文件指针同步
            for _ in range(count):
                read_string(handle)
            return f"[{count} strings]"
        size = TYPE_SIZES.get(item_type, 4)
        handle.read(size * count)
        return f"[{count} items]"
    size = TYPE_SIZES.get(value_type, 4)
    raw = handle.read(size)
    if value_type in (4, 5, 6):
        return struct.unpack("<I", raw)[0]
    if value_type in (10, 11, 12):
        return struct.unpack("<Q", raw)[0]
    if value_type in (2, 3):
        return struct.unpack("<H", raw)[0]
    return raw[0]


def main() -> int:
    for path_str in sys.argv[1:]:
        path = Path(path_str)
        with path.open("rb") as handle:
            magic = handle.read(4)
            if magic != b"GGUF":
                print(f"{path.name}: 不是 GGUF")
                continue
            (version,) = struct.unpack("<I", handle.read(4))
            (tensor_count,) = struct.unpack("<Q", handle.read(8))
            (kv_count,) = struct.unpack("<Q", handle.read(8))
            found = {}
            for _ in range(kv_count):
                key = read_string(handle)
                (value_type,) = struct.unpack("<I", handle.read(4))
                value = read_value(handle, value_type)
                if key in WANTED:
                    found[key] = value
                if key == "general.file_type":      # 后面是巨大的 tokenizer 数组，够了
                    break
        template = str(found.get("tokenizer.chat_template", ""))
        print(f"{path.name}（{path.stat().st_size / 2**30:.2f} GB，gguf v{version}）")
        for key in WANTED[:4]:
            if key in found:
                print(f"   {key} = {found[key]}")
        print(f"   chat_template 长度={len(template)}｜含 enable_thinking={'是' if 'enable_thinking' in template else '否'}"
              f"｜含 /no_think={'是' if '/no_think' in template else '否'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
