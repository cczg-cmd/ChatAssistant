#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_uia.py - 临时探针：只用 Windows 无障碍接口（UIA）读 QQ 聊天窗口，看能不能拿到消息文本+位置。

目的：验证一条"不注入、不模拟键鼠、不读进程内存"的读消息路径是否可行
     （UIA 是给读屏软件用的标准跨进程只读 API；QQ NT 是 Chromium 内核，类名 Chrome_WidgetWin_1）。
输出：每个 QQ 顶层窗口的 UIA 树规模、控件类型分布、疑似消息文本节点（含 bbox）、以及枚举耗时。
"""
from __future__ import annotations

import sys
import time
from collections import Counter

WORKSPACE = r"D:\QQChatAssistant"
sys.path.insert(0, rf"{WORKSPACE}\spikes")
import spike_wgc as wgc  # noqa: E402  （隔离头：TMP/TEMP/缓存重定向）

MAX_NODES = 4000


def main() -> int:
    from pywinauto import Desktop

    print("DPI:", wgc.set_dpi_awareness())
    desktop = Desktop(backend="uia")

    t0 = time.perf_counter()
    windows = desktop.windows()
    print(f"顶层窗口 {len(windows)} 个，枚举耗时 {(time.perf_counter()-t0)*1000:.0f} ms")

    qq = []
    for w in windows:
        try:
            info = w.element_info
            cls = info.class_name or ""
            title = info.name or ""
            pid = info.process_id
        except Exception:
            continue
        if cls != "Chrome_WidgetWin_1":
            continue
        try:
            exe = wgc.get_process_image_path(pid).lower()
        except Exception:
            exe = ""
        if "qq" not in exe:
            continue
        try:
            rect = w.rectangle()
        except Exception:
            rect = None
        qq.append((w, title, pid, rect, exe))
        print(f"  QQ 窗口: pid={pid} rect={rect} title={title[:40]!r} exe={exe}")

    if not qq:
        print("没找到 QQ/Chromium 顶层窗口")
        return 1

    for w, title, pid, rect, exe in qq[:2]:
        print("=" * 78)
        print(f"遍历 UIA 树: pid={pid} title={title[:40]!r}")
        t0 = time.perf_counter()
        try:
            nodes = w.descendants()
        except Exception as exc:
            print(f"  descendants() 失败: {type(exc).__name__}: {exc}")
            continue
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"  节点数 {len(nodes)}，枚举耗时 {elapsed:.0f} ms")

        kinds = Counter()
        texts = []
        for node in nodes[:MAX_NODES]:
            try:
                info = node.element_info
                kinds[info.control_type or "?"] += 1
                name = (info.name or "").strip()
                if not name:
                    continue
                rect = node.rectangle()
                texts.append((info.control_type, name, rect))
            except Exception:
                continue
        print("  控件类型分布:", dict(kinds.most_common(12)))
        print(f"  有名字的节点 {len(texts)} 个，前 30 个：")
        for kind, name, rect in texts[:30]:
            print(f"    [{kind:<12}] {rect} {name[:44]!r}")

        # 疑似"消息列表"：名字较长、类型是 Text/ListItem/Group 的节点
        long_items = [(k, n, r) for k, n, r in texts if len(n) >= 4 and k in
                      ("Text", "ListItem", "Group", "Document", "Edit", "Hyperlink")]
        print(f"  疑似消息/文本节点 {len(long_items)} 个（>=4 字的 Text/ListItem/Group）：")
        for kind, name, rect in long_items[:20]:
            print(f"    [{kind:<10}] x={rect.left:5d} y={rect.top:5d} w={rect.width():4d} h={rect.height():4d} {name[:40]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
