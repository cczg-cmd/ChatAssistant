#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/diagnose.py - UIA 一键诊断包（第 79 轮）

用户口径："策略配置化 + 一键诊断包可以做"。目的很直接：**QQ 再更新时不用来回猜**——
导出一个文件（json + 人可读的 txt），里面写清：
  · 当前有哪些 QQ 窗口、选中了哪个；
  · 整棵无障碍树的规模与控件类型分布；
  · 「消息列表」容器是怎么定位的（哪一层兜底/矩形/类型）；
  · **每个容器候选的得分与"为什么被排除"**（这一条最关键：能直接看出该调哪个配置项）；
  · 输入框候选同样列出；
  · 当前生效的策略配置值（阈值/白名单）。

只读：不改任何 QQ 状态、不写工作区外的文件。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("core.diagnose")


def _knob(cfg: Any, name: str, default: Any) -> Any:
    return getattr(getattr(cfg, "uia", None), name, default)


def collect(backend: Any, cfg: Any) -> Dict[str, Any]:
    """收集诊断信息（backend = UiaBackend；可以是桩对象，便于单测）。"""
    from utils import win32_api as w32

    data: Dict[str, Any] = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "config": {
            "window_class": _knob(cfg, "window_class", ""),
            "message_list_title": _knob(cfg, "message_list_title", ""),
            "container_types": _knob(cfg, "container_types", ""),
            "container_min_area_ratio": _knob(cfg, "container_min_area_ratio", 0.05),
            "container_max_area_ratio": _knob(cfg, "container_max_area_ratio", 0.92),
            "container_min_width_px": _knob(cfg, "container_min_width_px", 200),
            "container_min_texts": _knob(cfg, "container_min_texts", 3),
            "container_geo_min_width_ratio": _knob(
                cfg, "container_geo_min_width_ratio", 0.55),
            "container_geo_min_left_ratio": _knob(
                cfg, "container_geo_min_left_ratio", 0.12),
            "input_top_ratio": _knob(cfg, "input_top_ratio", 0.55),
            "input_min_width_ratio": _knob(cfg, "input_min_width_ratio", 0.5),
            "input_height_min_px": _knob(cfg, "input_height_min_px", 60),
            "input_height_max_px": _knob(cfg, "input_height_max_px", 320),
        },
        "windows": [],
        "tree": {"nodes": None, "named": None, "by_type": {}},
        "list_container": {"strategy": None, "box": None, "type": None, "name": None},
        "container_candidates": [],
        "input_area": {"strategy": None, "box": None},
        "input_candidates": [],
    }

    # ① QQ 顶层窗口清单
    try:
        for window in w32.list_qq_windows(_knob(cfg, "window_class", ""),
                                          _knob(cfg, "exe_name", "qq.exe")):
            data["windows"].append({
                "hwnd": window.hwnd, "pid": window.pid, "class": _knob(cfg, "window_class", ""),
                "title": window.title, "rect": list(window.rect),
                "visible": window.visible, "iconic": window.iconic, "area": window.area,
            })
    except Exception as exc:
        data["windows_error"] = f"{type(exc).__name__}: {exc}"

    if backend is None or getattr(backend, "_wrapper", None) is None:
        data["note"] = "没有可用的 UIA 附着（没找到 QQ 窗口 / 附着失败）"
        return data

    # ② 整棵树
    root_box = backend._node_box(backend._wrapper) or [0, 0, 0, 0]
    data["window_box"] = list(root_box)
    nodes = backend._all_nodes_cached() or []
    by_type: Dict[str, int] = {}
    for node in nodes:
        ctype = backend._node_type(node) or "?"
        by_type[ctype] = by_type.get(ctype, 0) + 1
    data["tree"] = {
        "nodes": len(nodes),
        "named": sum(1 for n in nodes if backend._node_name(n)),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
    }

    # ③ 当前定位结果
    data["list_container"] = {
        "strategy": getattr(backend, "_list_strategy", ""),
        "box": list(getattr(backend, "last_list_box", None) or []),
        "type": backend._node_type(getattr(backend, "_list_container", None))
        if getattr(backend, "_list_container", None) is not None else None,
        "name": backend._node_name(getattr(backend, "_list_container", None))
        if getattr(backend, "_list_container", None) is not None else None,
        "input_strategy": getattr(backend, "_input_strategy", ""),
    }

    # ④ 容器候选打分明细（含排除原因）—— 排查时最有用的一段
    title = str(_knob(cfg, "message_list_title", "") or "").strip()
    min_ratio = float(_knob(cfg, "container_min_area_ratio", 0.05) or 0.05)
    max_ratio = float(_knob(cfg, "container_max_area_ratio", 0.92) or 0.92)
    min_width = int(_knob(cfg, "container_min_width_px", 200) or 200)
    min_texts = int(_knob(cfg, "container_min_texts", 3) or 3)
    geo_w = float(_knob(cfg, "container_geo_min_width_ratio", 0.55) or 0.0)
    geo_l = float(_knob(cfg, "container_geo_min_left_ratio", 0.12) or 0.0)
    window_area = max(1, (root_box[2] - root_box[0]) * (root_box[3] - root_box[1]))
    window_w = max(1, root_box[2] - root_box[0])
    allowed = set(backend._container_types())

    for node in nodes:
        box = backend._node_box(node)
        if box is None:
            continue
        name = backend._node_name(node)
        ctype = backend._node_type(node)
        width, height = box[2] - box[0], box[3] - box[1]
        ratio = width * height / window_area
        reasons: List[str] = []
        if ctype not in allowed:
            reasons.append(f"类型 {ctype!r} 不在 container_types 里")
        if ratio < min_ratio:
            reasons.append(f"面积占比 {ratio:.2f} < container_min_area_ratio {min_ratio}")
        if ratio > max_ratio:
            reasons.append(f"面积占比 {ratio:.2f} > container_max_area_ratio {max_ratio}（像整窗）")
        if width < min_width:
            reasons.append(f"宽度 {width} < container_min_width_px {min_width}")
        if width < window_w * geo_w:
            reasons.append(f"宽度不到窗口的 {geo_w:.0%}（像左侧会话列表那一条）")
        if box[0] < root_box[0] + window_w * geo_l:
            reasons.append(f"左缘太靠左（<窗口左+{geo_l:.0%}），像左侧会话列表")
        visible = 0
        try:
            for text_node in node.descendants(control_type="Text"):
                text_box = backend._node_box(text_node)
                if text_box is not None and backend._inside(text_box, box, slack=4):
                    visible += 1
        except Exception as exc:
            reasons.append(f"读文字失败 {type(exc).__name__}")
        if visible < min_texts:
            reasons.append(f"可见文字 {visible} < container_min_texts {min_texts}")
        # 只把"有戏的"候选写进诊断（有标题、面积够大、或含文字），避免文件里塞几百个按钮
        if not (name or ratio >= min_ratio or visible):
            continue
        data["container_candidates"].append({
            "type": ctype, "name": name, "box": list(box),
            "size": [width, height], "area_ratio": round(ratio, 4),
            "visible_texts": visible, "ok": not reasons, "reasons": reasons,
        })
    data["container_candidates"].sort(
        key=lambda item: (not item["ok"], -item["visible_texts"], -item["area_ratio"]))
    data["container_candidates"] = data["container_candidates"][:40]

    # ⑤ 输入框候选明细
    try:
        node = backend.find_input_element([0, 0, 0, 0])
        if node is not None:
            data["input_area"] = {"strategy": getattr(backend, "_input_strategy", ""),
                                  "box": backend._node_box(node),
                                  "type": backend._node_type(node),
                                  "name": backend._node_name(node)}
    except Exception as exc:
        data["input_error"] = f"{type(exc).__name__}: {exc}"

    return data


def _summary_lines(data: Dict[str, Any]) -> List[str]:
    lines = [f"UIA 诊断包  {data.get('time')}",
             f"窗口类 {data['config']['window_class']!r}｜消息列表标题 "
             f"{data['config']['message_list_title']!r}",
             f"QQ 顶层窗口 {len(data.get('windows') or [])} 个："]
    for window in (data.get("windows") or [])[:8]:
        lines.append(f"    hwnd={window['hwnd']} vis={window['visible']} "
                     f"iconic={window['iconic']} title={window['title'][:16]!r} "
                     f"rect={window['rect']}")
    tree = data.get("tree") or {}
    lines.append(f"树：{tree.get('nodes')} 节点 / {tree.get('named')} 个有名字")
    lines.append(f"控件类型分布：{tree.get('by_type')}")
    container = data.get("list_container") or {}
    lines.append(f"消息列表容器：定位方式={container.get('strategy')!r} "
                 f"矩形={container.get('box')} 类型={container.get('type')!r} "
                 f"名字={container.get('name')!r}")
    lines.append(f"输入框：{data.get('input_area')}")
    lines.append("容器候选（前 12 个，按可用/文字数排序）：")
    for item in (data.get("container_candidates") or [])[:12]:
        mark = "OK " if item["ok"] else "排除"
        lines.append(f"    [{mark}] {item['type']:<9} {str(item['box']):<26} "
                     f"文字{item['visible_texts']:<3} 面积{item['area_ratio']:.2f} "
                     f"name={item['name'][:14]!r}")
        if item["reasons"]:
            lines.append("           · " + "；".join(item["reasons"]))
    return lines


def write(backend: Any, cfg: Any, out_dir: Optional[Path] = None,
          stamp: Optional[str] = None) -> Path:
    """收集并落盘（json + 人可读 txt），返回生成的 json 路径。"""
    data = collect(backend, cfg)
    target_dir = Path(out_dir) if out_dir is not None else Path(cfg.paths.log_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    tag = stamp or time.strftime("%Y%m%d-%H%M%S")
    json_path = target_dir / f"uia_diagnose_{tag}.json"
    txt_path = target_dir / f"uia_diagnose_{tag}.txt"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text("\n".join(_summary_lines(data)), encoding="utf-8")
    logger.info("UIA 诊断包已导出：%s（可读版 %s）", json_path, txt_path.name)
    return json_path
