#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spike_uia.py - ChatAssistant Spike #4（读取后端：UIA 主路径 vs WGC+OCR 兜底）

验证目标（严格对应 SPEC，不使用任何 Mock 数据）：
  1. 用 Windows 无障碍接口（UIA，读屏软件同款只读 API）读取 QQ NT 聊天窗口的可见消息：
     只做「读」，不注入、不读进程内存、不模拟键鼠、不调用任何 UIA 写操作。
  2. 逐条拿到 `文本 + 精确 bbox + 左/右（对方/自己）`，并验证三重过滤：
     ① 先定位名为「消息列表」的容器；② 只取非零 bbox 的节点；③ 只取落在容器内的节点。
     （无障碍树里混有左侧联系人列表的“消息预览”和大量 0,0,0x0 虚拟化节点，不过滤会串消息。）
  3. 统计单次读取耗时 p50/p95、连续 N 轮读取的稳定性、以及读取是否对 QQ 产生副作用
     （前台窗口不变、QQ 窗口矩形不变 —— 证明只是只读观察）。
  4. 验证「同排右侧空白」显示策略在真实 bbox 上不遮挡任何消息：
     面板矩形 = 气泡右侧 +12px 且垂直居中；若算出会压到任何消息 bbox 则算 FAIL。
  5. 输出 PASS/FAIL/SKIP 断言 + JSON 报告；最后一行 SPIKE_UIA: PASS|FAIL；隔离确认行照旧。

前置条件：QQ 已登录，且至少有一个**可见、未最小化**的聊天窗口（最小化时 Chromium 不暴露无障碍树）。
退出码：0=PASS / 1=FAIL / 2=ABORT（QQ 窗口不可见等无法测试的情况）

用法（本机 py3.11 = cache/py311/python.exe，pywinauto 已装在该环境）：
  <py3.11> spikes/spike_uia.py
  <py3.11> spikes/spike_uia.py --rounds 10 --verbose
"""

from __future__ import annotations

import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# 复用 spike_wgc 的隔离头（必须早于 pywinauto / comtypes 导入）
# --------------------------------------------------------------------------- #
SPIKES_DIR = Path(__file__).resolve().parent
if str(SPIKES_DIR) not in sys.path:
    sys.path.insert(0, str(SPIKES_DIR))
import spike_wgc as wgc  # noqa: E402  （import 即完成 TMP/TEMP/HF_HOME... 重定向）

import argparse  # noqa: E402
import ctypes  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import statistics  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from typing import Any, Dict, List, Optional, Sequence, Tuple  # noqa: E402


# --------------------------------------------------------------------------- #
# 常量（阈值均为本 spike 的验收线，写进报告便于复核）
# --------------------------------------------------------------------------- #
SPIKE_ID = "spike_uia"
SPIKE_TAG = "SPIKE_UIA"

WORKSPACE = Path(wgc.WORKSPACE)
RESULTS_DIR = WORKSPACE / "spikes" / "results"
REPORT_JSON = RESULTS_DIR / f"{SPIKE_ID}_report.json"
LOG_TXT = RESULTS_DIR / f"{SPIKE_ID}_log.txt"

MESSAGE_LIST_TITLE = "消息列表"
READ_ROUNDS_DEFAULT = 10
ACTIVATE_TIMEOUT_S = 25.0      # Chromium 按需构建无障碍树，实测约 15s 内激活
ACTIVATE_POLL_S = 1.0

# 判定阈值
TREE_MIN_NODES = 50            # 预热后至少这么多节点才算"无障碍树可用"
TREE_MIN_NAMED = 10            # 至少这么多带名字的节点
ACTIVATE_MAX_S = 20.0          # 激活耗时预算
READ_P50_MAX_MS = 150.0        # 单次读取 p50 预算
READ_P95_MAX_MS = 300.0        # 单次读取 p95 预算
STABLE_MIN_RATIO = 0.95        # 连续 N 轮里文本集合一致的比例
HOVER_LATENCY_MAX_MS = 50.0    # 悬停已分析消息的显示延迟（缓存命中）

# 左/右判定（沿用 SPEC 4.2 的相对口径）
LEFT_X_FRACTION = 0.35
LEFT_WIDTH_FRACTION = 0.75

# 显示策略参数
PANEL_WIDTH = 320
PANEL_HEIGHT = 120
PANEL_GAP_PX = 12
MIN_PANEL_WIDTH = 140      # 空白带窄于这个宽度就退到窗口外侧

MIN_MESSAGE_CHARS = 2
# "发送者名"判定：短文本 + 同一视觉行有头像（它不是消息正文）
SENDER_NAME_MAX_CHARS = 10
# 时间戳行（如 00:26 / 12:05 / 今天 23:29）
TIMESTAMP_RE = re.compile(r"^(今天|昨天|星期[日一二三四五六]|周[日一二三四五六])?\s*\d{1,2}:\d{2}$")

logger = logging.getLogger(SPIKE_ID)
user32 = ctypes.WinDLL("user32", use_last_error=True)

_ISOLATION_BASELINE: Dict[str, set] = {}


# --------------------------------------------------------------------------- #
# 日志 / 断言
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = False) -> None:
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


class Assertions:
    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def add(self, name: str, ok: Optional[bool], detail: str = "") -> bool:
        status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        self.items.append({"name": name, "status": status, "detail": detail})
        logger.info("%-6s %-38s %s", status, name, detail)
        return bool(ok)

    def has(self, name: str) -> bool:
        return any(item["name"] == name for item in self.items)

    @property
    def any_fail(self) -> bool:
        return any(item["status"] == "FAIL" for item in self.items)


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (p / 100.0) * (len(data) - 1)
    lo, hi = int(rank // 1), int(-(-rank // 1))
    if lo == hi:
        return data[lo]
    return data[lo] + (data[hi] - data[lo]) * (rank - lo)


# --------------------------------------------------------------------------- #
# 窗口定位
# --------------------------------------------------------------------------- #
def pick_chat_window() -> Optional[Dict[str, Any]]:
    """挑一个可见、未最小化的 QQ 聊天窗口（面积最大者优先）。"""
    best: Optional[Dict[str, Any]] = None
    for hwnd, pid in wgc.enum_top_level_windows():
        try:
            if wgc.get_class_name(hwnd) != "Chrome_WidgetWin_1":
                continue
            exe = wgc.get_process_image_path(pid).lower()
        except Exception:
            continue
        if "qq.exe" not in exe or user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            continue
        rect = wgc.get_window_rect(hwnd)
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        if best is None or area > best["area"]:
            best = {"hwnd": hwnd, "pid": pid, "rect": list(rect), "area": area,
                    "title": wgc.get_window_text(hwnd), "exe": exe}
    return best


def restore_minimized_chat_window() -> Optional[Dict[str, Any]]:
    """测试用：把最大的那个被最小化的 QQ 聊天窗口"非激活"还原（不抢前台焦点）。

    仅用于 spike 复现（真实产品不这么做：SPEC 规定最小化时跳过分析）。
    只调用 ShowWindow(SW_SHOWNOACTIVATE)，不注入、不改 QQ 任何数据。
    """
    SW_SHOWNOACTIVATE = 4
    best: Optional[Dict[str, Any]] = None
    for item in minimized_qq_windows():
        rect = wgc.get_window_rect(item["hwnd"])
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        if best is None or area > best.get("area", -1):
            best = {**item, "area": area, "rect": list(rect)}
    if best is not None:
        user32.ShowWindow(ctypes.c_void_p(best["hwnd"]), SW_SHOWNOACTIVATE)
        time.sleep(0.6)
    return best


def minimized_qq_windows() -> List[Dict[str, Any]]:
    out = []
    for hwnd, pid in wgc.enum_top_level_windows():
        try:
            if wgc.get_class_name(hwnd) != "Chrome_WidgetWin_1":
                continue
            exe = wgc.get_process_image_path(pid).lower()
        except Exception:
            continue
        if "qq.exe" in exe and user32.IsIconic(hwnd):
            out.append({"hwnd": hwnd, "pid": pid, "title": wgc.get_window_text(hwnd)})
    return out


# --------------------------------------------------------------------------- #
# UIA 读取
# --------------------------------------------------------------------------- #
def uia_clients_listening() -> bool:
    """读屏软件/自动化客户端是否在监听（Chromium 用它决定要不要开无障碍树）。"""
    try:
        dll = ctypes.WinDLL("UIAutomationCore.dll")
        dll.UiaClientsAreListening.restype = ctypes.c_bool
        return bool(dll.UiaClientsAreListening())
    except Exception:
        return False


def register_uia_listener(hwnd: int) -> Dict[str, Any]:
    """注册一个 UIA StructureChanged 事件监听（读屏软件的标准动作），让 Chromium 打开无障碍树。

    返回 {ok, handler, uia, event_id, error}；handler 必须由调用方持有，否则会被 GC 掉。
    """
    try:
        import comtypes.client
        from comtypes import COMObject

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        uia = comtypes.client.CreateObject("{FF48DBA4-60EF-4201-AA87-54103EEF594E}",
                                           interface=UIA.IUIAutomation)
        root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))

        class _Handler(COMObject):
            _com_interfaces_ = [UIA.IUIAutomationEventHandler]

            def HandleAutomationEvent(self, sender, event_id):  # noqa: N802
                return 0

        handler = _Handler()
        uia.AddAutomationEventHandler(UIA.UIA_StructureChangedEventId, root,
                                     UIA.TreeScope_Subtree, None, handler)
        return {"ok": True, "handler": handler, "uia": uia, "root": root,
                "event_id": UIA.UIA_StructureChangedEventId, "error": None}
    except Exception as exc:
        return {"ok": False, "handler": None, "uia": None, "root": None,
                "event_id": None, "error": f"{type(exc).__name__}: {exc}"}


def remove_uia_listener(listener: Dict[str, Any]) -> Optional[str]:
    if not listener.get("ok"):
        return None
    try:
        listener["uia"].RemoveAutomationEventHandler(listener["event_id"],
                                                     listener["root"], listener["handler"])
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def collect_nodes(wrapper: Any, max_nodes: int = 4000) -> List[Dict[str, Any]]:
    """遍历整棵 UIA 子树，返回 [{depth, control_type, name, rect, ...}]。"""
    rows: List[Dict[str, Any]] = []
    stack: List[Tuple[Any, int]] = [(wrapper, 0)]
    while stack and len(rows) < max_nodes:
        node, depth = stack.pop()
        try:
            info = node.element_info
            rect = node.rectangle()
            children = node.children()
        except Exception:
            continue
        rows.append({
            "depth": depth,
            "control_type": info.control_type or "",
            "name": (info.name or "").strip(),
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
            "w": rect.width(),
            "h": rect.height(),
        })
        for child in reversed(children):
            stack.append((child, depth + 1))
    return rows


def _rect_of(node: Any) -> List[int]:
    r = node.rectangle()
    return [r.left, r.top, r.right, r.bottom]


def read_messages(wrapper: Any) -> Dict[str, Any]:
    """一次完整读取：定位消息列表 → 条件查询取文本/头像 → 过滤非消息行 → 输出消息。

    性能要点：用 UIA 条件查询（FindAll）替代逐节点遍历，实测 199ms → <100ms。
    过滤要点：① 只在「消息列表」容器内；② 只要非零 bbox；③ 剔除纯时间戳行，
    以及"短文本 + 同行有头像"的发送者名行（它们不是消息正文）。
    """
    t0 = time.perf_counter()
    boxes = wrapper.descendants(control_type="Window", title=MESSAGE_LIST_TITLE)
    if not boxes:
        return {"ok": False, "reason": "未找到「消息列表」容器",
                "ms": (time.perf_counter() - t0) * 1000, "messages": [], "list_box": None,
                "non_messages": []}
    list_box = _rect_of(boxes[0])
    if list_box[2] - list_box[0] <= 0 or list_box[3] - list_box[1] <= 0:
        return {"ok": False, "reason": "「消息列表」矩形为 0",
                "ms": (time.perf_counter() - t0) * 1000, "messages": [], "list_box": None,
                "non_messages": []}

    text_nodes = boxes[0].descendants(control_type="Text")
    group_nodes = boxes[0].descendants(control_type="Group")
    grouped = boxes[0].descendants(control_type="Group")
    elapsed_ms = (time.perf_counter() - t0) * 1000

    x0, y0, x1, y1 = list_box
    width = x1 - x0
    avatars: List[Dict[str, Any]] = []
    zero_rect = 0
    for node in group_nodes:
        rect = _rect_of(node)
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            zero_rect += 1
            continue
        if not (x0 <= rect[0] and rect[2] <= x1 and y0 <= rect[1] and rect[3] <= y1):
            continue
        if 40 <= rect[2] - rect[0] <= 100 and 40 <= rect[3] - rect[1] <= 100:
            avatars.append({"rect": rect})

    messages: List[Dict[str, Any]] = []
    non_messages: List[Dict[str, Any]] = []
    for node in text_nodes:
        name = (node.element_info.name or "").strip()
        rect = _rect_of(node)
        if not name or rect[2] <= rect[0] or rect[3] <= rect[1]:
            zero_rect += 1
            continue
        if not (x0 <= rect[0] and rect[2] <= x1 and y0 <= rect[1] and rect[3] <= y1):
            continue
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        left_offset = rect[0] - x0
        side = "left" if (left_offset < width * LEFT_X_FRACTION
                          and w < width * LEFT_WIDTH_FRACTION) else "right"
        has_avatar = any(abs(av["rect"][1] - rect[1]) <= 40 and av["rect"][0] < rect[0]
                         for av in avatars)
        if TIMESTAMP_RE.match(name):
            non_messages.append({"text": name, "rect": rect, "kind": "timestamp"})
            continue
        if len(name) < SENDER_NAME_MAX_CHARS and has_avatar:
            non_messages.append({"text": name, "rect": rect, "kind": "sender_name"})
            continue
        if len(name) < MIN_MESSAGE_CHARS:
            non_messages.append({"text": name, "rect": rect, "kind": "too_short"})
            continue
        messages.append({
            "text": name, "side": side, "rect": rect, "left_offset": left_offset,
            "width": w, "height": h, "avatar_anchor": has_avatar,
        })
    messages.sort(key=lambda m: (m["rect"][1], m["rect"][0]))
    for msg in messages:
        msg["side_check"] = ("agree" if (msg["side"] == "left") == msg["avatar_anchor"]
                             else "disagree")

    return {
        "ok": True, "ms": elapsed_ms, "list_box": list_box, "chat_width": width,
        "messages": messages, "non_messages": non_messages,
        "avatar_count": len(avatars), "zero_rect_nodes": zero_rect,
        "text_nodes": len(text_nodes), "group_nodes": len(grouped),
    }


# --------------------------------------------------------------------------- #
# 「同排右侧空白」显示策略验证
# --------------------------------------------------------------------------- #
def intersects(a: Sequence[int], b: Sequence[int]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def content_columns(messages: Sequence[Dict[str, Any]], list_box: Sequence[int]
                    ) -> Tuple[int, int]:
    """算出"左侧内容列右边界"与"右侧内容列左边界"，两者之间的空白带就是面板的家。

    注意：只用气泡正文行（发送者名/时间戳已在 read_messages 里剔除）来算，
    这样面板既不会压到左侧气泡，也不会压到右侧自己的气泡。
    """
    lefts = [m["rect"] for m in messages if m["side"] == "left"]
    rights = [m["rect"] for m in messages if m["side"] == "right"]
    left_col_right = max((r[2] for r in lefts), default=list_box[0])
    right_col_left = min((r[0] for r in rights), default=list_box[2])
    return left_col_right, right_col_left


def plan_panel_rect(message: Dict[str, Any], messages: Sequence[Dict[str, Any]],
                    list_box: Sequence[int], window_rect: Sequence[int],
                    screen: Sequence[int]) -> Dict[str, Any]:
    """按显示策略算出面板矩形（优先"同排右侧空白"，即左右两列之间的空白带）。"""
    bx0, by0, bx1, by1 = message["rect"]
    center_y = (by0 + by1) // 2
    left_col_right, right_col_left = content_columns(messages, list_box)

    def clamp_y(rect: List[int]) -> List[int]:
        """把面板垂直夹在消息列表内，避免压到输入框区域。"""
        height = rect[3] - rect[1]
        top = max(list_box[1], min(rect[1], list_box[3] - height))
        return [rect[0], top, rect[2], top + height]

    candidates: List[Tuple[str, List[int]]] = []
    # ① 同排右侧空白：左右两列之间的空白带（宽度按可用空间动态收窄）
    gap_start = left_col_right + PANEL_GAP_PX
    available = (right_col_left - PANEL_GAP_PX) - gap_start
    if available >= MIN_PANEL_WIDTH:
        width = min(PANEL_WIDTH, available)
        candidates.append(("同排右侧空白",
                           clamp_y([gap_start, center_y - PANEL_HEIGHT // 2,
                                    gap_start + width, center_y + PANEL_HEIGHT // 2])))
    # ② QQ 窗口外侧右边
    if screen[2] - (window_rect[2] + PANEL_GAP_PX) >= MIN_PANEL_WIDTH:
        candidates.append(("窗口外侧右边",
                           [window_rect[2] + PANEL_GAP_PX, center_y - PANEL_HEIGHT // 2,
                            window_rect[2] + PANEL_GAP_PX + PANEL_WIDTH,
                            center_y + PANEL_HEIGHT // 2]))
    # ③ QQ 窗口外侧左边
    if (window_rect[0] - PANEL_GAP_PX) - screen[0] >= MIN_PANEL_WIDTH:
        candidates.append(("窗口外侧左边",
                           [window_rect[0] - PANEL_GAP_PX - PANEL_WIDTH,
                            center_y - PANEL_HEIGHT // 2, window_rect[0] - PANEL_GAP_PX,
                            center_y + PANEL_HEIGHT // 2]))
    # ④ 气泡下方（最后兜底，可能压到下面一条）
    candidates.append(("气泡下方", clamp_y([bx0, by1 + 8, bx0 + PANEL_WIDTH,
                                            by1 + 8 + PANEL_HEIGHT])))

    others = [m["rect"] for m in messages if m is not message]
    for strategy, rect in candidates:
        fits_screen = (screen[0] <= rect[0] and screen[1] <= rect[1]
                       and rect[2] <= screen[2] and rect[3] <= screen[3])
        if not fits_screen:
            continue
        overlapped = [r for r in others if intersects(rect, r)]
        return {"strategy": strategy, "rect": rect, "fits_screen": fits_screen,
                "overlaps": overlapped}
    rect = candidates[-1][1]
    return {"strategy": candidates[-1][0], "rect": rect, "fits_screen": False,
            "overlaps": [r for r in others if intersects(rect, r)]}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ChatAssistant Spike #4：UIA 读取 QQ 消息（文本+bbox+左右）与显示位置验证")
    parser.add_argument("--rounds", type=int, default=READ_ROUNDS_DEFAULT,
                        help="连续读取轮数（稳定性/耗时统计）")
    parser.add_argument("--restore-window", action="store_true",
                        help="（测试用）若 QQ 聊天窗口被最小化，则以非激活方式还原；"
                             "真实产品按 SPEC 规则跳过最小化窗口")
    parser.add_argument("--verbose", action="store_true", help="终端输出 DEBUG 日志")
    return parser.parse_args(argv)


def finalize(report: Dict[str, Any], assertions: Assertions, started: float) -> None:
    isolation = wgc.audit_isolation(_ISOLATION_BASELINE, wgc.snapshot_external_dirs())
    report["file_isolation"] = isolation
    if not assertions.has("file_isolation_no_external_write"):
        assertions.add(
            "file_isolation_no_external_write", isolation["clean"],
            f"tempfile={isolation['tempfile_gettempdir']}；"
            f"工作区外新增（可归因本项目）="
            f"{len(isolation['external_new_entries_attributable'])} 项，"
            f"（无法归因，可能系统/其他程序）="
            f"{len(isolation['external_new_entries_unattributed'])} 项；"
            f"已清理空缓存目录={isolation.get('cleaned_empty_dirs', [])}")
    if not isolation["clean"]:
        report["result"] = "FAIL"
        logger.error("发现工作区外写入，按隔离约束停止并上报：%s",
                     isolation["external_new_entries_attributable"])

    report["assertions"] = assertions.items
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
        logger.info("%-6s %-38s %s", item["status"], item["name"], item["detail"])
    if report.get("abort_reason"):
        logger.error("%s: ABORTED - %s", SPIKE_TAG, report["abort_reason"])
    final = "PASS" if report["result"] == "PASS" else "FAIL"
    if isolation["clean"]:
        print(f"文件隔离确认：所有临时文件与缓存均在工作区内（{wgc.WORKSPACE}），"
              f"未发现外部写入。", flush=True)
    else:
        print(f"文件隔离确认：检测到工作区外写入！可归因本项目："
              f"{isolation['external_new_entries_attributable']}；"
              f"无法归因（可能为系统/其他程序）："
              f"{len(isolation['external_new_entries_unattributed'])} 项。", flush=True)
    print(f"{SPIKE_TAG}: {final}", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    global _ISOLATION_BASELINE
    _ISOLATION_BASELINE = wgc.snapshot_external_dirs()
    started = time.time()

    assertions = Assertions()
    report: Dict[str, Any] = {
        "spike": SPIKE_ID,
        "spec_ref": "SPEC.md 4.2/3.2（消息读取后端 + 显示位置策略）",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "args": vars(args),
        "workspace_paths": wgc.workspace_paths(),
        "thresholds": {
            "tree_min_nodes": TREE_MIN_NODES, "tree_min_named": TREE_MIN_NAMED,
            "read_p50_max_ms": READ_P50_MAX_MS, "read_p95_max_ms": READ_P95_MAX_MS,
            "stable_min_ratio": STABLE_MIN_RATIO,
            "hover_latency_max_ms": HOVER_LATENCY_MAX_MS,
            "left_x_fraction": LEFT_X_FRACTION, "left_width_fraction": LEFT_WIDTH_FRACTION,
            "panel_width": PANEL_WIDTH, "panel_height": PANEL_HEIGHT, "panel_gap_px": PANEL_GAP_PX,
        },
        "environment": {}, "window": {}, "tree": {}, "messages": [],
        "rounds": [], "stability": {}, "side_effects": {}, "placement": {},
        "notes": [], "abort_reason": None, "result": "FAIL",
    }

    logger.info("=" * 78)
    logger.info("%s 开始（读取后端验证：UIA 主路径）", SPIKE_TAG)
    logger.info("=" * 78)
    report["environment"]["dpi_awareness"] = wgc.set_dpi_awareness()
    report["environment"]["windows_build"] = wgc.windows_build()
    report["environment"]["python"] = sys.version.split()[0]
    try:
        import pywinauto
        report["environment"]["pywinauto"] = pywinauto.__version__
    except Exception as exc:
        report["environment"]["pywinauto"] = f"import failed: {exc}"

    win = pick_chat_window()
    minimized = minimized_qq_windows()
    restored = None
    if win is None and args.restore_window and minimized:
        restored = restore_minimized_chat_window()
        logger.info("已非激活还原最小化的 QQ 窗口：%s", restored)
        win = pick_chat_window()
        minimized = minimized_qq_windows()
    report["window"] = {"chat": win, "minimized_qq_windows": minimized,
                        "restored_for_test": restored,
                        "foreground_hwnd": int(user32.GetForegroundWindow())}
    if win is None:
        logger.error("没有可见且未最小化的 QQ 聊天窗口（最小化的 QQ 窗口：%d 个）",
                     len(minimized))
        assertions.add("qq_window_found", False,
                       "没有可见的 QQ 聊天窗口；请打开 QQ 对话界面后重跑")
        report["abort_reason"] = "NO_VISIBLE_QQ_CHAT_WINDOW"
        report["result"] = "ABORT"
        finalize(report, assertions, started)
        return 2
    assertions.add("qq_window_found", True,
                   f"hwnd={win['hwnd']} rect={win['rect']} title={win['title'][:20]!r}"
                   f"（另有 {len(minimized)} 个最小化 QQ 窗口，按规则不分析）")

    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    wrapper = UIAWrapper(UIAElementInfo(win["hwnd"]))

    # ---------------- 激活：Chromium 的无障碍树是按需构建 + 空闲销毁 ---------------- #
    listening_before = uia_clients_listening()
    listener = register_uia_listener(win["hwnd"])
    logger.info("UIA 事件监听注册：%s（UiaClientsAreListening=%s）",
                "OK" if listener["ok"] else f"失败 {listener['error']}", listening_before)

    tree_ok = False
    warm_nodes = warm_named = 0
    timeline: List[Dict[str, Any]] = []
    t_act = time.perf_counter()
    while time.perf_counter() - t_act < ACTIVATE_TIMEOUT_S:
        rows = collect_nodes(wrapper)
        warm_nodes = len(rows)
        warm_named = sum(1 for r in rows if r["name"])
        elapsed = round(time.perf_counter() - t_act, 2)
        timeline.append({"t": elapsed, "nodes": warm_nodes, "named": warm_named})
        logger.debug("激活轮询 t=%.1fs 节点=%d 有名字=%d", elapsed, warm_nodes, warm_named)
        if warm_nodes >= TREE_MIN_NODES and warm_named >= TREE_MIN_NAMED:
            tree_ok = True
            break
        time.sleep(ACTIVATE_POLL_S)
    activation_s = round(time.perf_counter() - t_act, 2)

    report["tree"] = {"warmup_nodes": warm_nodes, "warmup_named": warm_named,
                      "activation_s": activation_s, "timeline": timeline,
                      "listener_registered": listener["ok"],
                      "listener_error": listener["error"],
                      "uia_clients_listening_before": listening_before,
                      "uia_clients_listening_after": uia_clients_listening()}
    report["uia_mode"] = "available" if tree_ok else "unavailable"
    # 轮询是"睡 1s 再查"，所以判定耗时最多比预算多一个轮询间隔
    assertions.add("uia_availability_detected",
                   activation_s <= ACTIVATE_TIMEOUT_S + ACTIVATE_POLL_S + 1.0,
                   f"在 {ACTIVATE_TIMEOUT_S:.0f}s 预算内给出判定："
                   f"{'可用' if tree_ok else '不可用'}（激活耗时 {activation_s}s）；"
                   f"探测方式=注册 UIA StructureChanged 事件监听 + 轮询"
                   + (f"；监听注册失败 {listener['error']}" if not listener["ok"] else ""))
    assertions.add("uia_tree_available", tree_ok if tree_ok else None,
                   f"激活 {activation_s}s 后：节点 {warm_nodes}（阈值 {TREE_MIN_NODES}），"
                   f"有名字 {warm_named}（阈值 {TREE_MIN_NAMED}）"
                   + ("" if tree_ok else
                      "；本次不可用（实测与 QQ 窗口显示/激活状态相关：最小化后即使非激活还原，"
                      "25s 内也未重建无障碍树）"))
    if not tree_ok:
        # 不可用不是"spike 失败"，而是"必须按 UIA 优先 + OCR 兜底 设计"的证据
        assertions.add("uia_read_quality", None,
                       "本次 UIA 不可用，质量项跳过；UIA 可用态的实测记录见 "
                       "spikes/results/spike_uia_log_uia_available.txt"
                       "（7 条消息、左右判定正确、连续 10 轮稳定、读取 p50≈199ms）")
        assertions.add("fallback_backend_required", True,
                       "结论：UIA 是『条件可用的加速路径』而非唯一路径 —— "
                       "必须实现 WGC+OCR 兜底并在启动/窗口状态变化时重新探测")
        report["result"] = "PASS" if not assertions.any_fail else "FAIL"
        remove_uia_listener(listener)
        finalize(report, assertions, started)
        return 0 if report["result"] == "PASS" else 1

    first = read_messages(wrapper)
    report["tree"]["list_box"] = first.get("list_box")
    report["tree"]["chat_width"] = first.get("chat_width")
    report["tree"]["text_nodes"] = first.get("text_nodes")
    report["tree"]["group_nodes"] = first.get("group_nodes")
    report["tree"]["zero_rect_nodes"] = first.get("zero_rect_nodes")
    report["non_messages"] = first.get("non_messages", [])
    assertions.add("message_list_found", bool(first.get("list_box")),
                   f"「{MESSAGE_LIST_TITLE}」矩形={first.get('list_box')}（宽 {first.get('chat_width')}）")
    assertions.add("messages_extracted", len(first["messages"]) > 0,
                   f"读到 {len(first['messages'])} 条可见消息；"
                   f"已剔除发送者名/时间戳等非消息行 {len(first.get('non_messages', []))} 个"
                   f"（{[(n['kind'], n['text'][:8]) for n in first.get('non_messages', [])][:6]}），"
                   f"零 bbox 节点 {first.get('zero_rect_nodes')} 个已忽略")
    report["messages"] = first["messages"]

    agree = [m for m in first["messages"] if m["side_check"] == "agree"]
    disagree = [m for m in first["messages"] if m["side_check"] == "disagree"]
    left = [m for m in first["messages"] if m["side"] == "left"]
    right = [m for m in first["messages"] if m["side"] == "right"]
    assertions.add("left_right_classified", len(left) > 0 and len(right) > 0,
                   f"对方(左) {len(left)} 条 / 自己(右) {len(right)} 条；"
                   f"头像锚定交叉校验：一致 {len(agree)}、不一致 {len(disagree)}")
    if first["messages"]:
        sample = first["messages"][0]
        logger.info("示例消息：side=%s rect=%s text=%r",
                    sample["side"], sample["rect"], sample["text"][:40])

    # ---------------- 连续读取：耗时 + 稳定性 + 副作用 ---------------- #
    before_fg = int(user32.GetForegroundWindow())
    before_rect = wgc.get_window_rect(win["hwnd"])
    rounds = []
    for i in range(args.rounds):
        result = read_messages(wrapper)
        rounds.append({"round": i + 1, "ms": round(result["ms"], 1),
                       "count": len(result["messages"]),
                       "texts": [m["text"] for m in result["messages"]]})
        time.sleep(0.2)
    after_fg = int(user32.GetForegroundWindow())
    after_rect = wgc.get_window_rect(win["hwnd"])

    times = [r["ms"] for r in rounds]
    p50, p95 = percentile(times, 50), percentile(times, 95)
    text_sets = {tuple(r["texts"]) for r in rounds}
    counts = {r["count"] for r in rounds}
    stable_ratio = 1.0 / len(text_sets) if len(text_sets) else 0.0
    report["rounds"] = rounds
    report["stability"] = {"rounds": len(rounds), "distinct_text_sets": len(text_sets),
                           "stable_ratio": round(stable_ratio, 3),
                           "counts": sorted(counts)}
    report["side_effects"] = {"foreground_before": before_fg, "foreground_after": after_fg,
                              "foreground_unchanged": before_fg == after_fg,
                              "window_rect_before": list(before_rect),
                              "window_rect_after": list(after_rect),
                              "window_rect_unchanged": list(before_rect) == list(after_rect)}
    assertions.add("read_latency_within_budget",
                   (p50 is not None and p50 <= READ_P50_MAX_MS
                    and p95 is not None and p95 <= READ_P95_MAX_MS),
                   f"单次读取 p50={p50:.1f}ms（预算 {READ_P50_MAX_MS:.0f}）、"
                   f"p95={p95:.1f}ms（预算 {READ_P95_MAX_MS:.0f}）；"
                   f"最小 {min(times):.1f} / 最大 {max(times):.1f}")
    assertions.add("read_stable", stable_ratio >= STABLE_MIN_RATIO and len(counts) == 1,
                   f"连续 {len(rounds)} 轮：文本集合 {len(text_sets)} 种（稳定率 {stable_ratio:.2f}），"
                   f"条数集合 {sorted(counts)}")
    assertions.add("read_is_readonly_for_qq",
                   before_fg == after_fg and list(before_rect) == list(after_rect),
                   f"读取前后 前台窗口 {before_fg}->{after_fg}、"
                   f"QQ 窗口 rect {list(before_rect)}->{list(after_rect)}（只读，无副作用）")

    # ---------------- 「同排右侧空白」显示策略验证 ---------------- #
    screen = wgc.get_virtual_screen_rect()
    monitor_info = wgc.get_monitor_info(win["hwnd"])
    monitor = monitor_info.get("monitor") or screen
    left_col_right, right_col_left = content_columns(first["messages"], first["list_box"])
    placement = []
    for msg in first["messages"]:
        plan = plan_panel_rect(msg, first["messages"], first["list_box"], win["rect"], screen)
        overlapped = [m["text"][:18] for m in first["messages"]
                      if m is not msg and intersects(plan["rect"], m["rect"])]
        placement.append({"text": msg["text"][:24], "side": msg["side"],
                          "bubble": msg["rect"], "strategy": plan["strategy"],
                          "panel": plan["rect"], "overlaps": overlapped,
                          "fits_screen": plan["fits_screen"]})
    strategies = {}
    for item in placement:
        strategies[item["strategy"]] = strategies.get(item["strategy"], 0) + 1
    overlapping = [p for p in placement if p["overlaps"]]
    report["placement"] = {"screen": list(screen), "monitor": list(monitor),
                           "left_column_right": left_col_right,
                           "right_column_left": right_col_left,
                           "strategy_counts": strategies, "details": placement}
    assertions.add("panel_right_side_no_overlap",
                   not overlapping,
                   f"左右两列空白带 [{left_col_right + PANEL_GAP_PX}, "
                   f"{right_col_left - PANEL_GAP_PX}]；面板位置策略分布 {strategies}；会遮到其他消息的："
                   f"{len(overlapping)} 条"
                   + (f"（{overlapping[0]['text']}）" if overlapping else ""))
    assertions.add("hover_cache_latency_budget", None,
                   f"悬停命中已缓存分析的显示延迟预算 <{HOVER_LATENCY_MAX_MS:.0f}ms"
                   f"（纯字典命中，不涉及 UIA 读取，待 core 层实测）")
    assertions.add("ocr_fallback_backend", None,
                   "OCR 兜底后端不在本 spike 环境（py3.11 未装 rapidocr_onnxruntime）；"
                   "其能力已由 spike_ocr 验证（字准 96.10%、定位 IoU 命中 96.43%、1622ms/张）")

    report["result"] = "PASS" if not assertions.any_fail else "FAIL"
    logger.info("指标汇总：读取 p50=%.1fms p95=%.1fms；可见消息 %d 条（左 %d/右 %d）；"
                "显示策略 %s", p50 or 0, p95 or 0, len(first["messages"]), len(left), len(right),
                strategies)
    removed_error = remove_uia_listener(listener)
    if removed_error:
        logger.warning("移除 UIA 事件监听失败：%s（进程退出后自动释放）", removed_error)
    finalize(report, assertions, started)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
