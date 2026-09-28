#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/message_reader.py - 消息读取后端（SPEC 4.2 / 5.1，B1-M2）

v1 只用 UIA（无障碍接口，只读）：
  1) 找 QQ 聊天窗口（可见且未最小化；最小化按 SPEC 4.2.3 安静暂停）；
  2) 激活无障碍树（注册 UIA StructureChanged 事件监听 + 轮询，实测 0.2-14s）；
  3) 读取"消息列表"内的可见消息：文本 + 精确 bbox + 左/右（对方/自己）；
  4) 三重过滤：只在消息列表容器内 / 丢弃 0,0,0×0 虚拟化节点 / 剔除时间戳与发送者名行。

不做什么（安全红线 SPEC 2.2）：
  - 不注入、不读进程内存、不模拟键鼠、不写 UIA（只读控件树）；
  - 读不到就**安静降级**：返回空列表 + status="unavailable"，由 UI 把状态灯转灰。

OCR 兜底为 v2 预留：backend 抽象已留，v1 不实现、不装依赖。
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import Config
from utils import win32_api as w32

logger = logging.getLogger("core.message_reader")

# 时间戳行：00:26 / 今天 23:29 / 09-19 13:07:09 / 2026/9/23 10:00 等（实测有带日期的）
TIMESTAMP_RE = re.compile(
    r"^(?:(?:今天|昨天|星期[日一二三四五六]|周[日一二三四五六])\s*)?"
    r"(?:(?:\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?)\s*)?"
    r"\d{1,2}:\d{2}(?::\d{2})?$")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Message:
    msg_key: str
    text: str
    side: str                                   # "left"=对方 / "right"=自己
    bbox: Tuple[int, int, int, int]
    backend: str = "uia"
    is_image: bool = False
    avatar_anchor: bool = False
    row_index: Optional[int] = None          # 消息行在"消息列表"容器里的**子节点下标**
    #   —— 探针实测：下标单调、稳定、虚拟化行也占位（B2 第 51 轮新增，用于排序身份）。
    #   注意：它**不进 msg_key**（缓存/去重/面板定位口径保持不变，零风险）。
    segment: int = 0                         # 上下文分段：与池子"有重叠"归为同一段；
    #   完全无重叠（跳跃滚动）时开新段。取上下文只认目标所在段 → 绝不跨断点猜顺序。

    @property
    def is_other_party(self) -> bool:
        return self.side == "left"

    def as_dict(self) -> Dict[str, Any]:
        return {"msg_key": self.msg_key, "text": self.text, "side": self.side,
                "bbox": list(self.bbox), "backend": self.backend,
                "is_image": self.is_image, "avatar_anchor": self.avatar_anchor}


def normalize_text(text: str) -> str:
    """归一化文本：折叠空白、去掉零宽字符（用于 msg_key 与去重）。"""
    cleaned = (text or "").replace("\u200b", "").replace("\ufeff", "")
    return " ".join(cleaned.split()).strip()


def make_msg_key(session_id: str, text: str, side: str = "") -> str:
    """SPEC 4.4：msg_key = 会话ID + 左侧别 + 归一化文本（不含任何底部消息哈希）。

    第 80 轮加"侧别"：同一段文字可能**对方发一条、我也发一条**（用户实测：
    "我和对方发了同样的文本，点下面那条只会被判成上面那条"）。
    只按文本算 key 时这两条会共用一条缓存/上下文记录，点击下面那条也会被折算成上面那条；
    带上侧别后它们就是两条不同的消息（`side` 缺省空串 = 兼容老调用）。
    """
    raw = f"{session_id}|{side}|{normalize_text(text)}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# UIA 后端实现
# --------------------------------------------------------------------------- #
class UiaBackend:
    """封装 pywinauto(UIA) 的只读访问；不注入、不写控件。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._wrapper: Any = None
        self._listener: Optional[Dict[str, Any]] = None
        self.hwnd: Optional[int] = None
        self.nodes = 0
        self.named = 0
        self.last_list_box: Optional[List[int]] = None
        # 缓存"元素句柄→msg_key"，用于低成本高频刷新坐标（见 refresh_positions）
        self._elements: List[Tuple[Any, str]] = []
        self._list_element: Any = None
        # 第 78 轮（QQ 9.9.36 改结构）：容器定位结果缓存 + 定位方式（便于日后排查）
        self._list_container: Any = None
        self._list_container_box: Optional[List[int]] = None
        self._list_container_ts: float = 0.0
        self._list_strategy: str = ""
        self._list_zero_streak: int = 0
        self._all_nodes: List[Any] = []
        self._all_nodes_ts: float = 0.0
        # 输入框：新版不再是 Edit/Document，走了几何兜底，也要缓存（回读校验会高频调用）
        self._input_element: Any = None
        self._input_box: Optional[List[int]] = None
        self._input_ts: float = 0.0
        self._input_strategy: str = ""
        self.last_refresh_stale = 0
        # 行下标身份（B2 第 51 轮）：RuntimeId → 行下标；跨读取复用，避免每次重走父链
        self._row_index_by_rid: Dict[str, int] = {}
        self._row_index_cache: Dict[str, int] = {}
        # 虚拟化历史：树里存在但不在可视区（bbox 全 0）的文本 —— 只能当**上下文**用，
        # 不能当"消息"（没有坐标就无法定位面板）。实测它们就是滚出屏幕的历史消息。
        self.history_texts: List[str] = []
        self.activation_s: Optional[float] = None
        self.last_error: Optional[str] = None
        self.available = False

    # ---------------- 基础 ----------------
    def close(self) -> None:
        self._remove_listener()
        self._wrapper = None
        self.available = False
        self.hwnd = None

    def _remove_listener(self) -> None:
        listener, self._listener = self._listener, None
        if not listener or not listener.get("ok"):
            return
        try:
            listener["uia"].RemoveAutomationEventHandler(
                listener["event_id"], listener["root"], listener["handler"])
        except Exception as exc:  # 进程退出会自然释放
            logger.debug("移除 UIA 监听失败：%s", exc)

    def _register_listener(self) -> bool:
        """注册 StructureChanged 事件监听（让 Chromium 打开无障碍树）。"""
        try:
            import comtypes.client
            from comtypes import COMObject

            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA

            uia = comtypes.client.CreateObject(
                "{FF48DBA4-60EF-4201-AA87-54103EEF594E}", interface=UIA.IUIAutomation)
            root = uia.ElementFromHandle(ctypes.c_void_p(self.hwnd))

            class _Handler(COMObject):
                _com_interfaces_ = [UIA.IUIAutomationEventHandler]

                def HandleAutomationEvent(self, sender, event_id):  # noqa: N802
                    return 0

            handler = _Handler()
            uia.AddAutomationEventHandler(UIA.UIA_StructureChangedEventId, root,
                                          UIA.TreeScope_Subtree, None, handler)
            self._listener = {"ok": True, "uia": uia, "root": root, "handler": handler,
                              "event_id": UIA.UIA_StructureChangedEventId}
            return True
        except Exception as exc:
            self.last_error = f"注册 UIA 监听失败: {type(exc).__name__}: {exc}"
            logger.debug(self.last_error)
            self._listener = {"ok": False}
            return False

    # ---------------- 附着 / 激活 ----------------
    def attach(self, hwnd: int) -> bool:
        if self.hwnd == hwnd and self._wrapper is not None:
            return True
        self.close()
        try:
            from pywinauto.controls.uiawrapper import UIAWrapper
            from pywinauto.uia_element_info import UIAElementInfo

            self._wrapper = UIAWrapper(UIAElementInfo(hwnd))
            self.hwnd = hwnd
            self._register_listener()
            return True
        except Exception as exc:
            self.last_error = f"UIA 附着失败: {type(exc).__name__}: {exc}"
            logger.warning(self.last_error)
            self._wrapper = None
            return False

    def probe(self) -> Tuple[int, int]:
        """统计树规模（节点数 / 有名字节点数），用于判断"树是否建好"。"""
        if self._wrapper is None:
            return (0, 0)
        try:
            nodes = self._wrapper.descendants()
        except Exception as exc:
            self.last_error = f"遍历失败: {type(exc).__name__}: {exc}"
            return (0, 0)
        named = 0
        for node in nodes:
            try:
                if (node.element_info.name or "").strip():
                    named += 1
            except Exception:
                continue
        self.nodes, self.named = len(nodes), named
        return (len(nodes), named)

    def ensure_available(self, timeout_s: Optional[float] = None) -> bool:
        """在预算内判定并尽量激活无障碍树（SPEC 4.2.3：必须有结论，不许卡住）。"""
        timeout_s = timeout_s if timeout_s is not None else self.cfg.uia.activate_timeout_s
        t0 = time.perf_counter()
        while True:
            nodes, named = self.probe()
            if nodes >= self.cfg.uia.min_tree_nodes and named >= self.cfg.uia.min_tree_named:
                self.available = True
                self.activation_s = round(time.perf_counter() - t0, 2)
                self.last_error = None
                return True
            if time.perf_counter() - t0 >= timeout_s:
                self.available = False
                self.activation_s = round(time.perf_counter() - t0, 2)
                self.last_error = self.last_error or (
                    f"无障碍树不可用（节点 {nodes}/{self.cfg.uia.min_tree_nodes}，"
                    f"有名字 {named}/{self.cfg.uia.min_tree_named}）")
                return False
            time.sleep(self.cfg.uia.activate_poll_s)

    # ---------------- 容器定位（第 78 轮：抗 QQ 改结构） ----------------
    #
    # 背景：QQ 9.9.36 把「消息列表」容器的 UIA 控件类型从 `Window` 改成了 `Pane`。
    # 旧代码写死 `descendants(control_type="Window", title="消息列表")` → 直接查空，
    # 消息读不到、面板全不显示（可用性自检却是过的：树里有 129 个节点）。
    #
    # 现在三级兜底，且**不再把控件类型写死**：
    #   ① 旧版精确匹配（Window + 标题）—— 老版本行为一字不变；
    #   ② 标题匹配任意控件类型 —— 候选取「它下面能看到多少 Text」最多的那个；
    #   ③ 连标题都没有（QQ 换名/换语言/换结构）→ 纯几何：窗口内面积占比合理、
    #      且包含 Text 最多的容器。
    _CONTAINER_TYPES = ("Window", "Pane", "Group", "Document", "List", "Custom", "ListItem")
    _LIST_TTL_S = 5.0          # 容器解析结果多久重找一次
    _NODES_TTL_S = 0.0         # 整棵树缓存的存活时间（0 = 只在单次解析内复用）

    def _node_name(self, node: Any) -> str:
        try:
            return (node.element_info.name or "").strip()
        except Exception:
            return ""

    def _node_type(self, node: Any) -> str:
        try:
            return str(node.element_info.control_type or "")
        except Exception:
            return ""

    def _node_box(self, node: Any) -> Optional[List[int]]:
        """节点的屏幕矩形；读不到或退化成 0 尺寸（虚拟化节点）返回 None。"""
        try:
            rect = self._rect(node)
        except Exception:
            return None
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            return None
        return rect

    @staticmethod
    def _inside(box: Sequence[int], outer: Sequence[int], slack: int = 2) -> bool:
        return (box[0] >= outer[0] - slack and box[2] <= outer[2] + slack
                and box[1] >= outer[1] - slack and box[3] <= outer[3] + slack)

    def _all_nodes_cached(self) -> List[Any]:
        """整棵树（平铺）。一次约 50ms，只用于容器定位，所以带缓存。"""
        if self._all_nodes:
            return self._all_nodes
        try:
            self._all_nodes = list(self._wrapper.descendants())
        except Exception as exc:
            self.last_error = f"遍历失败: {type(exc).__name__}: {exc}"
            self._all_nodes = []
        return self._all_nodes

    def _container_types(self) -> Tuple[str, ...]:
        """几何兜底时允许的控件类型（可配置：QQ 换类型时改配置即可，不用改代码）。"""
        raw = getattr(self.cfg.uia, "container_types", "") or ""
        items = tuple(part.strip() for part in str(raw).split(",") if part.strip())
        return items or self._CONTAINER_TYPES

    def _score_container(self, candidates: Sequence[Any], root_box: Sequence[int],
                         by_title: bool) -> Optional[Any]:
        """在候选里挑最像「消息列表」的容器：优先看它下面有多少**可见** Text。

        第 79 轮（用户："打开新会话时，有时会定位到左侧的会话列表而不是聊天界面"）：
        标题匹配失败退到**纯几何兜底**时，左侧"会话列表"本身就是一大片带文字的行
        （每个会话一行），按"可见 Text 数"打分它会赢 —— 于是面板贴到了会话列表上。
        现在几何兜底多两条硬约束（可用配置调）：
          · 宽度至少占窗口 `container_geo_min_width_ratio`（聊天区宽、会话列表窄）；
          · 左缘至少离窗口左边 `container_geo_min_left_ratio`（把左侧那一条排除掉）。
        标题匹配（by_title）不受影响 —— 老结构行为不变。
        """
        w_cfg = self.cfg.uia
        window_area = max(1, (root_box[2] - root_box[0]) * (root_box[3] - root_box[1]))
        window_w = max(1, root_box[2] - root_box[0])
        min_ratio = float(getattr(w_cfg, "container_min_area_ratio", 0.05) or 0.05)
        max_ratio = float(getattr(w_cfg, "container_max_area_ratio", 0.92) or 0.92)
        min_width = int(getattr(w_cfg, "container_min_width_px", 200) or 200)
        min_texts = int(getattr(w_cfg, "container_min_texts", 3) or 3)
        geo_min_w_ratio = float(getattr(w_cfg, "container_geo_min_width_ratio", 0.55) or 0.0)
        geo_min_left_ratio = float(getattr(w_cfg, "container_geo_min_left_ratio", 0.0) or 0.0)
        best: Any = None
        best_score = 0.0
        for node in candidates:
            box = self._node_box(node)
            if box is None or not self._inside(box, root_box):
                continue
            width, height = box[2] - box[0], box[3] - box[1]
            ratio = width * height / window_area
            if ratio < min_ratio:                # 太小：多半是按钮/图标
                continue
            if not by_title:
                # 几何兜底：整窗不算"消息列表"，且必须"够宽、不在左侧会话列表那一条里"
                if ratio > max_ratio or width < min_width:
                    continue
                if width < window_w * geo_min_w_ratio:
                    continue
                if box[0] < root_box[0] + window_w * geo_min_left_ratio:
                    continue
            try:
                texts = node.descendants(control_type="Text")
            except Exception:
                continue
            visible = 0
            for text_node in texts:
                text_box = self._node_box(text_node)
                if text_box is not None and self._inside(text_box, box, slack=4):
                    visible += 1
            if visible < (1 if by_title else min_texts):
                continue
            score = visible * 1_000_000 + width * height
            if score > best_score:
                best, best_score = node, score
        return best

    def resolve_list_container(self, force: bool = False) -> Any:
        """定位「消息列表」容器（三级兜底，见本节注释）。失败返回 None。"""
        if self._wrapper is None:
            return None
        now = time.perf_counter()
        if (not force and self._list_container is not None
                and now - self._list_container_ts < self._LIST_TTL_S):
            if self._node_box(self._list_container) is not None:
                return self._list_container
        title = (self.cfg.uia.message_list_title or "").strip()
        root_box = self._node_box(self._wrapper)
        if root_box is None:
            root_box = [0, 0, 10 ** 6, 10 ** 6]

        chosen: Any = None
        how = ""
        # ① 旧版精确匹配（老 QQ 行为完全不变）
        if title:
            try:
                hits = self._wrapper.descendants(control_type="Window", title=title)
            except Exception:
                hits = []
            for node in hits:
                box = self._node_box(node)
                if box is not None and self._inside(box, root_box):
                    chosen, how = node, "旧版 Window+标题"
                    break
        # ② 标题匹配任意控件类型（QQ 9.9.36：容器变成 Pane）
        if chosen is None and title:
            self._all_nodes = []
            nodes = self._all_nodes_cached()
            candidates = [n for n in nodes if title and title in self._node_name(n)]
            if candidates:
                chosen = self._score_container(candidates, root_box, by_title=True)
                if chosen is not None:
                    how = "标题匹配（任意类型）"
        # ③ 纯几何兜底（连标题都变了）
        if chosen is None:
            self._all_nodes = []
            nodes = self._all_nodes_cached()
            pool = [n for n in nodes if self._node_type(n) in self._container_types()]
            chosen = self._score_container(pool, root_box, by_title=False)
            if chosen is not None:
                how = "几何兜底"

        if chosen is None:
            self._list_strategy = ""
            return None
        self._list_container = chosen
        self._list_container_box = self._node_box(chosen)
        self._list_container_ts = time.perf_counter()
        if how != self._list_strategy:
            self._list_strategy = how
            logger.info("「消息列表」容器定位方式：%s（控件类型=%s，矩形=%s）",
                        how, self._node_type(chosen), self._list_container_box)
        return chosen

    # ---------------- 读取 ----------------
    def _rect(self, node: Any) -> List[int]:
        r = node.rectangle()
        return [r.left, r.top, r.right, r.bottom]

    @staticmethod
    def _runtime_id(node: Any) -> str:
        """读 UIA RuntimeId（元素存活期内唯一）。失败返回空串。"""
        try:
            ids = node.element_info.element.GetRuntimeId()
            return "-".join(str(v) for v in ids) if ids else ""
        except Exception:
            return ""

    def _row_index_of(self, node: Any) -> Optional[int]:
        """向上找这条文本所属的"消息行"，取出它的子节点下标（带缓存）。

        实测结构：Text 挂在"行 Group"下，行 Group 是"消息列表"的直接子节点。
        RuntimeId 稳定 → 每条消息只在第一次花几次跨进程调用，之后命中缓存（零开销）。
        """
        probe, seen = node, []
        for _ in range(3):                      # 最多向上 3 层
            if probe is None:
                break
            rid = self._runtime_id(probe)
            if rid:
                cached = self._row_index_cache.get(rid)
                if cached is not None:
                    return cached
                index = self._row_index_by_rid.get(rid)
                if index is not None:
                    for key in seen:            # 把路径上经过的节点也缓存起来
                        self._row_index_cache[key] = index
                    self._row_index_cache[rid] = index
                    return index
                seen.append(rid)
            try:
                probe = probe.parent()
            except Exception:
                break
        return None

    def read(self, session_id: str) -> List[Message]:
        """读取消息列表内的可见消息（三重过滤）。"""
        if self._wrapper is None:
            return []
        # 连续读空 → 强制重找容器（QQ 切会话/滚动会重建树，旧元素会失效）
        container = self.resolve_list_container(force=self._list_zero_streak >= 2)
        if container is None:
            self.last_error = ("未找到「消息列表」容器"
                               "（已试过：旧版 Window 匹配 / 标题匹配 / 几何兜底）")
            self._list_zero_streak += 1
            return []
        list_box = self._node_box(container)
        if list_box is None:
            self._list_container = None
            self.last_error = "「消息列表」矩形为 0"
            self._list_zero_streak += 1
            return []
        self.last_list_box = list_box
        self._list_element = container

        x0, y0, x1, y1 = list_box
        width = max(1, x1 - x0)
        # 【第 69 轮删除：行下标枚举】原来每次读取都 enumerate(boxes[0].children()) +
        # 每个子节点读一次 rect（实测 22ms/次，占整次读取 127ms 的 17%），
        # 用来填 `Message.row_index`；但"按 row_index 排序/判定前文"两个方案都已撤销
        # （虚拟化下标不稳定），**没有任何地方消费这个字段** → 纯浪费。
        # 删掉后 `row_index` 保持默认 None，行为不变。
        self._row_index_by_rid: Dict[str, int] = {}
        try:
            texts = container.descendants(control_type="Text")
            groups = container.descendants(control_type="Group")
            images = container.descendants(control_type="Image")
        except Exception as exc:
            self.last_error = f"读取节点失败: {type(exc).__name__}: {exc}"
            return []

        avatars: List[List[int]] = []
        for node in groups:
            rect = self._rect(node)
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                continue
            if not (x0 <= rect[0] and rect[2] <= x1 and y0 <= rect[1] and rect[3] <= y1):
                continue
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if (self.cfg.uia.avatar_min_px <= w <= self.cfg.uia.avatar_max_px
                    and self.cfg.uia.avatar_min_px <= h <= self.cfg.uia.avatar_max_px):
                avatars.append(rect)

        # 先收集"虚拟化历史"（bbox 全 0 的文本）：它们没有坐标，只能当上下文
        history = []
        for node in texts:
            name = (node.element_info.name or "").strip()
            if not name:
                continue
            rect = self._rect(node)
            if rect[2] > rect[0] and rect[3] > rect[1]:
                continue                      # 可见的走正常流程
            text = normalize_text(name)
            if TIMESTAMP_RE.match(text) or len(text) < self.cfg.uia.min_message_chars:
                continue
            history.append(text)
        # 实测（exp_context）：历史带太多会让小模型把"历史里的某条"当成要分析的目标
        # （3B 曾把"取消科三"当成"笑了"的意图）→ 只保留最近几条作背景。
        self.history_texts = history[-max(0, self.cfg.analyzer.context_history_max):]

        # 先收集候选文本行（容器内、非零 bbox、非时间戳），再统一判定"发送者名"
        candidates: List[Dict[str, Any]] = []
        for node in texts:
            name = (node.element_info.name or "").strip()
            rect = self._rect(node)
            if not name or rect[2] <= rect[0] or rect[3] <= rect[1]:
                continue
            if not (x0 <= rect[0] and rect[2] <= x1 and y0 <= rect[1] and rect[3] <= y1):
                continue
            text = normalize_text(name)
            if TIMESTAMP_RE.match(text):
                continue
            if len(text) < self.cfg.uia.min_message_chars:
                continue
            candidates.append({"text": text, "rect": rect, "node": node})

        sender_names = (self._sender_name_indices(candidates, avatars)
                        if self.cfg.uia.filter_sender_names else set())
        messages: List[Message] = []
        elements: List[Tuple[Any, str]] = []      # (UIA 节点, msg_key)：供高频刷新坐标用
        history: List[str] = []
        for index, item in enumerate(candidates):
            if index in sender_names:
                continue                      # 发送者名行，不是消息正文
            text, rect = item["text"], item["rect"]
            w = rect[2] - rect[0]
            has_avatar = any(abs(av[1] - rect[1]) <= 60 and av[0] < rect[0] for av in avatars)
            left_offset = rect[0] - x0
            # 左右判定（实测教训：只按"左边缘+宽度比例"会把**自己发的长消息**判成对方，
            # 于是自己的消息也被拿去分析）。优先级：头像位置 → 右对齐 → 宽度比例兜底。
            av_left = any(abs(av[1] - rect[1]) <= 60 and av[2] <= rect[0] + 20 for av in avatars)
            av_right = any(abs(av[1] - rect[1]) <= 60 and av[0] >= rect[2] - 20 for av in avatars)
            right_gap = x1 - rect[2]
            if av_right and not av_left:
                side = "right"
            elif av_left and not av_right:
                side = "left"
            elif right_gap <= width * 0.08:            # 气泡贴着聊天区右边缘 = 自己发的
                side = "right"
            elif (left_offset < width * self.cfg.uia.left_x_fraction
                  and w < width * self.cfg.uia.left_width_fraction):
                side = "left"
            else:
                side = "right"
            key = make_msg_key(session_id, text, side)
            messages.append(Message(msg_key=key, text=text,
                                    side=side, bbox=(rect[0], rect[1], rect[2], rect[3]),
                                    avatar_anchor=has_avatar))
            elements.append((item.get("node"), key))

        # 图片消息（大尺寸 Image 节点）：标记为不可分析，UI 显示"图片消息（未分析）"
        for node in images:
            rect = self._rect(node)
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                continue
            if not (x0 <= rect[0] and rect[2] <= x1 and y0 <= rect[1] and rect[3] <= y1):
                continue
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if w < self.cfg.uia.image_min_px or h < self.cfg.uia.image_min_px:
                continue
            left_offset = rect[0] - x0
            side = "left" if left_offset < width * self.cfg.uia.left_x_fraction else "right"
            # 【稳定性修复】图片的 msg_key 原来含坐标（rect）→ 每滚动一次就变一个 key，
            # 既没法当排序锚点、又会在池子里堆出重复项。改成按"尺寸+左右"取稳定键：
            # 同一张图（或同尺寸同侧的图）会得到同一个键，顺序锚点就不会被图片打断。
            messages.append(Message(msg_key=make_msg_key(session_id, f"[图片]{w}x{h}", side),
                                    text="[图片消息]", side=side,
                                    bbox=(rect[0], rect[1], rect[2], rect[3]),
                                    is_image=True))

        messages.sort(key=lambda m: (m.bbox[1], m.bbox[0]))
        # 记录元素句柄（与消息一一对应），供高频刷新坐标使用
        kept_keys = {m.msg_key for m in messages}
        self._elements = [(node, key) for node, key in elements
                          if key in kept_keys and node is not None]
        if messages:
            self._list_zero_streak = 0
        else:
            self._list_zero_streak += 1
        return messages

    def refresh_positions(self) -> Dict[str, List[int]]:
        """低成本刷新已缓存元素的屏幕坐标（每条一次跨进程属性读，实测 1-3ms）。

        这是"丝滑跟随"的关键：滚动/重排时不需要重读整棵树（80ms+），
        只要把目标那几条元素的矩形重新问一遍，就能 30ms 级别地跟着走。
        返回 {msg_key: [l,t,r,b]}；矩形失效（抛异常或全 0）的条数记在 last_refresh_stale。
        """
        out: Dict[str, List[int]] = {}
        stale = 0
        for node, key in self._elements:
            try:
                rect = self._rect(node)
            except Exception:
                stale += 1
                continue
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                stale += 1
                continue
            out[key] = rect
        if self._list_element is not None:
            try:
                box = self._rect(self._list_element)
                if box[2] > box[0] and box[3] > box[1]:
                    self.last_list_box = box
            except Exception:
                pass
        self.last_refresh_stale = stale
        return out

    def refresh_one(self, msg_key: str) -> Optional[List[int]]:
        """只刷**一条**消息的矩形（1-3ms）。

        滚动时整列消息同步位移，所以只问目标这一条的坐标就够推全体；
        比每 30ms 把 10-20 个元素全问一遍（跨进程读累计 20-60ms）轻得多，
        既让跟随更丝滑，也少给 QQ 进程添负担。失效（元素被虚拟化回收）返回 None。
        """
        for node, key in self._elements:
            if key != msg_key:
                continue
            try:
                rect = self._rect(node)
            except Exception:
                return None
            if rect[2] > rect[0] and rect[3] > rect[1]:
                return rect
            return None
        return None

    def refresh_any(self) -> Optional[Tuple[str, List[int]]]:
        """返回"随便一条还读得到的消息"的 (msg_key, rect)。

        滚动时目标那条可能已被虚拟化回收（refresh_one 返回 None），但同屏别的消息通常
        还读得到 —— 用它的位移同样能推整列，从而避免退化成整树重读（80ms+，被限流后
        表现为"面板隔 0.5s 才跳一次"）。
        """
        for node, key in self._elements:
            try:
                rect = self._rect(node)
            except Exception:
                continue
            if rect[2] > rect[0] and rect[3] > rect[1]:
                return (key, rect)
        return None

    def _sender_name_indices(self, candidates: Sequence[Dict[str, Any]],
                             avatars: Sequence[Sequence[int]]) -> set:
        """找出"发送者名"行的下标（成对判定，避免误杀短消息）。

        QQ 的两种布局：
          - 群聊：头像右上角一行昵称 + 头像下方正文 ⇒ 同一头像块里有 **≥2** 条文本；
          - 单聊：只有正文（没有昵称）⇒ 同一头像块里只有 **1** 条文本。
        规则（收紧版，默认关闭，群聊才需要）：昵称必须**同时**满足
          ① 很短（≤ sender_name_max_chars，默认 10）；② 顶部与头像对齐（±10px）；
          ③ 它下面 14-70px 内**紧跟**一条正文（同头像列）—— 只有群聊的"昵称+正文"才这样排。
        实测教训：不收紧会把 1v1 里三条相邻消息的前两条当成昵称删掉。
        """
        names: set = set()
        if not avatars:
            return names
        for av in avatars:
            name_rows = [i for i, item in enumerate(candidates)
                         if av[0] < item["rect"][0] <= av[2] + 140
                         and abs(item["rect"][1] - av[1]) <= 10
                         and len(item["text"]) <= self.cfg.uia.sender_name_max_chars]
            body_rows = [i for i, item in enumerate(candidates)
                         if av[0] < item["rect"][0] <= av[2] + 140
                         and 14 <= (item["rect"][1] - av[1]) <= 70]
            if name_rows and body_rows:
                names.add(min(name_rows, key=lambda i: candidates[i]["rect"][1]))
        return names

    def find_input_element(self, window_rect: Sequence[int]) -> Optional[Any]:
        """定位聊天输入框**节点**（只读；返回 UIA 元素，供取矩形/设焦点）。

        做法：在窗口下半部找 Edit/Document 控件，取面积最大的那个。
        找不到就返回 None —— 调用方必须退化为"只复制到剪贴板"，绝不盲点屏幕。
        判定放宽为"中心点在屏幕内 + 屏幕内面积占比 ≥60%"：实测 QQ 窗口常被拖到
        屏幕左边缘（输入框左边有几十像素出屏），用"整块在屏幕内"会误判为不可用。
        """
        if self._wrapper is None:
            return None
        # 第 74 轮修复（"自动填入容易失败"的真因之一）：参照矩形必须与 UIA 树**同源**。
        # 原来拿 `window_rect`（来自 Win32/移动钩子，可能比树新）去过滤树里的坐标，
        # 窗口刚被拖动/缩放时两者对不上 → 明明存在的输入框被判成"不在窗口内"→ 直接退化成只复制。
        # 现在优先用**根元素自己的矩形**（与树里的坐标必然一致），拿不到再退回传入值。
        try:
            root_rect = self._rect(self._wrapper)
            if root_rect[2] - root_rect[0] > 100 and root_rect[3] - root_rect[1] > 100:
                x0, y0, x1, y1 = root_rect
            else:
                x0, y0, x1, y1 = window_rect
        except Exception:
            x0, y0, x1, y1 = window_rect
        lower_bound = y0 + int((y1 - y0) * 0.55)
        screen = w32.get_virtual_screen_rect()
        best: Optional[Any] = None          # 第 74 轮：存**节点**（不只是矩形），
        best_area = 0                       #   这样还能用 UIA SetFocus 直接拿焦点
        for control_type in ("Edit", "Document"):
            try:
                nodes = self._wrapper.descendants(control_type=control_type)
            except Exception:
                continue
            for node in nodes:
                try:
                    rect = self._rect(node)
                except Exception:
                    continue
                if rect[2] <= rect[0] or rect[3] <= rect[1]:
                    continue
                if not (x0 <= rect[0] and rect[2] <= x1 and y0 <= rect[1] and rect[3] <= y1):
                    continue
                if rect[1] < lower_bound:
                    continue
                # 中心点必须在屏幕内（我们要点它的中心），且大部分面积在屏幕内
                cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
                if not (screen[0] <= cx <= screen[2] and screen[1] <= cy <= screen[3]):
                    continue
                area_total = (rect[2] - rect[0]) * (rect[3] - rect[1])
                if area_total <= 0 or (w32.on_screen_area(rect, screen) / area_total) < 0.6:
                    continue
                if rect[2] - rect[0] < 150 or rect[3] - rect[1] < 24:
                    continue
                if area_total > best_area:
                    best_area, best = area_total, node
        if best is not None:
            self._input_strategy = "Edit/Document"
            return best
        # 第 78 轮兜底：新版 QQ（9.9.36）输入框不再是 Edit/Document，
        # 树里只剩一个覆盖整窗的 Document（不满足"下半部"判定）→ 旧逻辑直接返回 None，
        # 填入退化成"只复制到剪贴板"。这里改用几何兜底：底部那条**最宽、且内部没有按钮**
        # 的容器就是输入区（带"关闭/发送"的那一行因为含按钮被排除，所以不会点错）。
        node = self._resolve_input_area(x0, y0, x1, y1)
        if node is not None:
            self._input_strategy = "几何兜底(底部输入区)"
        return node

    _INPUT_TTL_S = 10.0

    def _resolve_input_area(self, x0: int, y0: int, x1: int, y1: int) -> Optional[Any]:
        """几何兜底找输入区元素（结果带缓存：回读校验会高频调用它）。"""
        now = time.perf_counter()
        if (self._input_element is not None
                and now - self._input_ts < self._INPUT_TTL_S):
            box = self._node_box(self._input_element)
            if box is not None and self._inside(box, (x0, y0, x1, y1), slack=4):
                return self._input_element

        self._all_nodes = []
        nodes = self._all_nodes_cached()
        # 第 79 轮：这些阈值也可以配置（QQ 换输入框样式时改配置，不用改代码/重打包）
        u_cfg = self.cfg.uia
        min_top = y0 + int((y1 - y0) * float(getattr(u_cfg, "input_top_ratio", 0.55) or 0.55))
        min_width = max(200, int((x1 - x0) * float(
            getattr(u_cfg, "input_min_width_ratio", 0.5) or 0.5)))
        h_min = int(getattr(u_cfg, "input_height_min_px", 60) or 60)
        h_max = int(getattr(u_cfg, "input_height_max_px", 320) or 320)
        best: Any = None
        # 【坑】初值必须是 -inf：没文本的候选得分是 -面积（负数），用 -1.0 当门槛会把它们
        # 全判掉。之前能选到输入区是因为它带着"按住 Win+Alt…"占位文本（有文本=+1e9），
        # 而**输入框一被聚焦/用过，占位文本就消失** → 得分变负 → 又找不到输入框了
        # （用户反馈"点击选项又不自动粘贴了"就是这个）。
        best_score = float("-inf")
        for node in nodes:
            ctype = self._node_type(node)
            if ctype in ("Button", "Text", "Image", "Edit", "ScrollBar", "ToolBar"):
                continue
            box = self._node_box(node)
            if box is None or not self._inside(box, (x0, y0, x1, y1)):
                continue
            width, height = box[2] - box[0], box[3] - box[1]
            if width < min_width or not (h_min <= height <= h_max):
                continue
            if box[1] < min_top:
                continue
            # 必须在消息列表底边之下（拿不到消息列表时跳过这条判断）
            if self.last_list_box and box[1] < self.last_list_box[3] - 8:
                continue
            try:
                if node.descendants(control_type="Button"):
                    continue                  # 工具栏/发送行：里面有按钮，绝不能点
            except Exception:
                pass
            # 关键判据：真正的输入区**有文本内容**（空的时候是占位文案，支持 TextPattern），
            # 外层包裹容器则读不到文本。同样有文本时取面积**更小**的（=更内层、更贴近输入框本身）。
            has_text = False
            try:
                has_text = any(t and t.strip() for t in (node.texts() or []))
            except Exception:
                has_text = False
            score = (1_000_000_000.0 if has_text else 0.0) - (width * height)
            if score > best_score:
                best, best_score = node, score
        if best is not None:
            self._input_element = best
            self._input_box = self._node_box(best)
            self._input_ts = time.perf_counter()
        return best

    def find_input_box(self, window_rect: Sequence[int]) -> Optional[List[int]]:
        """只读地返回输入框的物理矩形（内部走 `find_input_element`）。"""
        node = self.find_input_element(window_rect)
        if node is None:
            return None
        try:
            return self._rect(node)
        except Exception:
            return None

    def focus_input_box(self, window_rect: Sequence[int]) -> bool:
        """用 **UIA SetFocus** 把键盘焦点交给输入框（第 74 轮新增）。

        为什么需要：一键填入以前靠"模拟点一下输入框中心"拿焦点 —— 在别人机器上会出现
        "点歪/点不上/鼠标卡住"（朋友那边就是这样）。UIA 直接设焦点：
        **不注入任何输入事件、不动鼠标**，既可靠又安全。

        边界：只改"焦点"，不写内容、不触发发送（比 SetValue/Invoke 温和得多；
        本文件其余部分仍严格只读）。返回 True 只表示"已请求焦点"，
        真正的成功与否由调用方回读输入框文本确认。
        """
        if self._wrapper is None:
            return False
        node = self.find_input_element(window_rect)
        if node is None:
            return False
        try:
            node.set_focus()
        except Exception as exc:
            logger.debug("UIA 设置输入框焦点失败：%s", exc)
            return False
        time.sleep(0.05)
        for attr in ("has_keyboard_focus", "is_keyboard_focused"):
            try:
                value = getattr(node, attr)
                return bool(value() if callable(value) else value)
            except Exception:
                continue
        return True

    def read_input_text(self, window_rect: Sequence[int]) -> Optional[str]:
        """只读地回读输入框当前文本（用于填入校验：不猜、不假装成功）。

        优先读 UIA ValuePattern 的 Value，退化读元素 Name；都拿不到返回 None。
        """
        if self._wrapper is None:
            return None
        box = self.find_input_box(window_rect)
        if box is None:
            return None
        # 几何兜底（新版 QQ 用 Group 承载）时没有 Edit/Document 可扫 —— 直接跳过那两次
        # 整树扫描：回读会被轮询调用十几次，每次省 ~20-50ms。
        nodes = []
        if self._input_strategy != "几何兜底(底部输入区)":
            try:
                nodes = list(self._wrapper.descendants(control_type="Edit")) + \
                    list(self._wrapper.descendants(control_type="Document"))
            except Exception:
                return None
        for node in nodes:
            try:
                rect = self._rect(node)
            except Exception:
                continue
            if rect != box:
                continue
            try:
                value = node.get_value()
                if isinstance(value, str):
                    return value
            except Exception:
                pass
            try:
                return (node.element_info.name or "").strip()
            except Exception:
                return None
        # 第 78 轮：新版 QQ 的输入框不是 Edit/Document（几何兜底拿到的是 Group），
        # 上面按矩形匹配必然落空 → 再针对"拿到的那个元素本身"读一次：
        #   ① ValuePattern ② TextPattern（实测新版输入区 Group 支持它，空输入时给占位文案）
        #   ③ 元素 Name。都拿不到才返回 None（调用方据此如实告知"无法校验"）。
        node = self.find_input_element(window_rect)
        if node is None:
            return None
        try:
            value = node.get_value()
            if isinstance(value, str) and value.strip():
                return value
        except Exception:
            pass
        try:
            texts = node.texts()
            joined = "".join(t for t in (texts or []) if t)
            if joined.strip():
                return joined
        except Exception:
            pass
        try:
            name = (node.element_info.name or "").strip()
            if name:
                return name
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------- #
# 对外统一接口
# --------------------------------------------------------------------------- #
class MessageReader:
    """读取后端统一出口：v1 只有 UIA；OCR 后端留接口（v2）。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.uia = UiaBackend(cfg)
        self.status: str = "init"            # init|ready|paused|unavailable|no_window
        self.backend: str = "uia"
        self.last_read_ms: float = 0.0
        self.last_error: Optional[str] = None
        self.last_window: Optional[w32.QqWindow] = None
        self._last_probe_ts: float = 0.0
        self._reads = 0
        self._failures = 0
        # 自己累积的"本会话已见消息"（含已滚出可视区的）：
        # 实测 QQ 会把滚出屏幕的消息**从无障碍树里移除**（history_texts 常常是空的），
        # 于是"目标附近只有 1-2 条前文"，上下文严重不足。这里把每轮读到的消息按顺序攒起来，
        # 滚出视区后仍能当**前文**用（纯只读，不滚动、不操作 QQ）。
        self._session_id: str = ""
        self._history: List[Message] = []
        self._row_index_cache: Dict[str, int] = {}   # RuntimeId → 行下标（跨读取复用）
        self._segment_id: int = 0                    # 上下文分段号（无重叠的跳跃滚动开新段）

    # ---------------- 状态 ----------------
    def is_paused(self) -> bool:
        """QQ 最小化/隐藏 → 安静暂停（不提示、灯灰，SPEC 4.2.3）。"""
        return self.status in ("paused", "no_window", "no_conversation")

    def snapshot(self) -> Dict[str, Any]:
        return {"status": self.status, "backend": self.backend,
                "last_read_ms": round(self.last_read_ms, 1),
                "last_error": self.last_error, "reads": self._reads,
                "failures": self._failures,
                "window": None if self.last_window is None else {
                    "hwnd": self.last_window.hwnd, "pid": self.last_window.pid,
                    "title": self.last_window.title,
                    "rect": list(self.last_window.rect), "iconic": self.last_window.iconic},
                "uia": {"available": self.uia.available, "nodes": self.uia.nodes,
                        "named": self.uia.named, "activation_s": self.uia.activation_s,
                        "list_box": self.uia.last_list_box,
                        # 第 78 轮：定位方式（旧版Window/标题匹配/几何兜底）——QQ 再改结构时
                        # 从日志就能看出是哪一层兜底在生效，省得重新猜
                        "list_strategy": getattr(self.uia, "_list_strategy", ""),
                        "input_strategy": getattr(self.uia, "_input_strategy", "")}}

    # ---------------- 主流程 ----------------
    def read(self) -> Tuple[List[Message], Dict[str, Any]]:
        """读一次可见消息；永不抛异常（异常一律转成 status + 空列表）。"""
        t0 = time.perf_counter()
        # 窗口选择优先级（第 68b 轮）：**前台那个 QQ 窗口** > 上一轮已绑定的窗口 > 屏幕内面积最大。
        # 旧实现只看面积，于是会把浮窗贴到"主窗口的会话列表"上（用户反馈的"跟随列表窗口"）。
        window = w32.find_qq_chat_window(
            self.cfg.uia.window_class, self.cfg.uia.exe_name,
            prefer_hwnd=int(self.uia.hwnd) if getattr(self.uia, "hwnd", 0) else None)
        if window is None:
            all_windows = w32.list_qq_windows(self.cfg.uia.window_class, self.cfg.uia.exe_name)
            self.status = "paused" if all_windows else "no_window"
            self.uia.close()
            self.last_window = None
            self.last_read_ms = (time.perf_counter() - t0) * 1000
            self.last_error = None if self.status == "paused" else "未发现 QQ 窗口"
            self._reads += 1
            return [], self.snapshot()

        self.last_window = window
        session_id = window.session_id
        # 第 68 轮修复（用户反馈"打开 QQ 窗口半天不亮灯"）：QQ 重开/换了窗口（hwnd 变了）
        # 时**立刻重探 UIA** —— 原来要等 `recheck_interval_s`(30s) 的闸门。
        # 第 70 轮补：只按 hwnd 判断还不够 —— **同一个窗口里之后才打开会话**（标题变了、
        # 句柄没变）同样要立刻重探，否则会再卡 30s。所以把"hwnd + 标题"一起当变化信号。
        probe_key = (window.hwnd, session_id)
        if getattr(self, "_last_probe_key", None) != probe_key:
            self._last_probe_key = probe_key
            self._last_probe_ts = 0.0
            self._failures = 0

        if not self.uia.available or self.uia.hwnd != window.hwnd:
            if not self.uia.attach(window.hwnd):
                self.status = "unavailable"
                self.last_error = self.uia.last_error
                self._failures += 1
                self.last_read_ms = (time.perf_counter() - t0) * 1000
                self._reads += 1
                return [], self.snapshot()
            now = time.time()
            # 不可用时按 recheck_interval_s 重探，避免每次都等满激活预算
            if (not self.uia.available and self.status == "unavailable"
                    and now - self._last_probe_ts < self.cfg.uia.recheck_interval_s):
                self.last_read_ms = (time.perf_counter() - t0) * 1000
                self._reads += 1
                return [], self.snapshot()
            self._last_probe_ts = now
            # 重探（上一次已经判过不可用）用**短超时**：25s 的激活预算只留给"第一次见到
            # 这个窗口"，否则每次重探都会把采集线程占住 25s，反而更慢恢复。
            retry = self._failures > 0
            if not self.uia.ensure_available(timeout_s=3.0 if retry else None):
                self.status = "unavailable"
                self.last_error = self.uia.last_error
                self._failures += 1
                self.last_read_ms = (time.perf_counter() - t0) * 1000
                self._reads += 1
                return [], self.snapshot()

        try:
            messages = self.uia.read(session_id)
        except Exception as exc:                     # 防御：任何异常都不能打断 UI 线程
            self.last_error = f"读取异常: {type(exc).__name__}: {exc}"
            logger.warning(self.last_error)
            messages = []

        # 累积历史：换会话就清空；同一会话里把"新见到的"消息按顺序追加（去重）
        if session_id != self._session_id:
            self._session_id = session_id
            self._history = []
        if messages:
            # 【顺序修复】按"可见列表"当锚点合并，而不是一律追加到尾部：
            # 原实现向上一滚（更旧的消息出现）会把它们追加到**新消息后面** → 池子顺序错乱
            # （用户反馈"记录没有按正确的时间顺序排好"）。这里以重叠的那条为锚，
            # 比它更早的插到前面、更晚的插到后面，两个方向都对。
            # 池子里**带上图片占位**（不再排除）：图片把文本序列隔开时，
            # 有占位才能把"图片前后两段"正确接起来（用户反馈的锚点被图片破坏）。
            visible = list(messages)
            if not self._history:
                self._history = list(visible)
            else:
                merged, overlap = self._merge_pool(visible, self._history)
                if overlap < 1:
                    # 用户口径（第 53 轮）：**只保留一个可拼接的连续区段**作为全部上下文。
                    # 无重叠 = 出现断点（跳跃滚动、DOM 回收）→ 直接以当前窗口为新的唯一区段，
                    # 丢掉旧段。宁可只留"确定连续"的一段，也不把不连续的两段拼在一起
                    # （那正会产生用户看到的"大段倒置"）。
                    self._history = [Message(**{**m.__dict__, "segment": 0}) for m in visible]
                    self._segment_id = 0
                else:
                    self._history = merged
            # 【不要再做全局去重】消息身份是"文本哈希"，聊天里出现重复文本（"好/嗯/哈哈"）时
            # 去重会把**较新的那条**删掉 → 顺序大段错乱（用户实测反馈）。
            # 只去掉**相邻重复**，其余保留（锚点合并本身已经避免了跨批重复）。
            deduped: List[Message] = []
            for message in self._history:
                if deduped and deduped[-1].msg_key == message.msg_key:
                    continue
                deduped.append(message)
            self._history = deduped
            # 【已撤销：按 row_index 排序】实测（diag_scroll_order.py）滚动后同一个下标会落到
            # 不同消息上（滚动前 row=16 是"这玩意儿…"，上滚 8 格后 row=16 变成"这爱抖露…"），
            # 因为 QQ 的消息列表是**虚拟化回收列表**，下标只是"当前 DOM 位置"、不是稳定身份。
            # 按它排序会把顺序彻底搞乱 → 撤销，改为依赖"重叠锚点合并"（见上面的 merge）。
            # 池子上限（第 62 轮，用户口径："池子太大也没什么用，超过 16 条就清掉多余的"）：
            # 只保留"当前窗口 + 窗口之前最多 history_cache_size(16) 条 + 窗口之后最多
            # history_tail_keep(4) 条"。依据见 config.py 里那两个键的注释。
            cap = int(getattr(self.cfg.analyzer, "history_cache_size", 16) or 16)
            tail_keep = int(getattr(self.cfg.analyzer, "history_tail_keep", 4) or 4)
            self._history = self._trim_pool(self._history, visible, cap, tail_keep)

        if messages:
            self.status = "ready"
            self.last_error = None
        elif self.uia.available:
            # 树是好的但没有消息列表 → 多半是没打开对话窗口（不弹提示，灯灰）
            self.status = "no_conversation"
            self.last_error = self.uia.last_error or "未找到「消息列表」容器（可能没打开对话窗口）"
        else:
            self.status = "unavailable"
            self._failures += 1
        self.backend = "uia"
        self.last_read_ms = (time.perf_counter() - t0) * 1000
        self._reads += 1
        return messages, self.snapshot()

    # ---------------- 便捷方法 ----------------
    @staticmethod
    def _trim_pool(pool: List[Message], visible: Sequence[Message],
                   cap: int, tail_keep: int) -> List[Message]:
        """只保留"当前窗口 + 窗口之前最近 cap 条 + 窗口之后最近 tail_keep 条"。

        第 62 轮（用户口径："池子太大好像也没什么用，超过 16 条就自动清一下多余的"）。
        依据：一次分析最多吃 `context_messages`(16) 条，而且**屏幕可见的优先**，
        所以"当前窗口之前第 cap+1 条"永远进不了 prompt；窗口之后的消息按后文口径
        （`context_following=0`）也不进 prompt，只留几条够下次滚动做重叠判断。
        当前窗口本身**永不裁** —— 否则目标可能不在池子里，`build_context` 的兜底会退化成
        "没有上下文"（第 60 轮那条修复）。
        用 key 找窗口范围（可能有重复文本导致的近似），近似方向是**多留**，不会误裁。
        """
        if not pool:
            return pool
        vis_keys = {m.msg_key for m in visible}
        indices = [k for k, m in enumerate(pool) if m.msg_key in vis_keys] if vis_keys else []
        if not indices:
            return list(pool[-cap:]) if len(pool) > cap else list(pool)
        first, last = indices[0], indices[-1]
        start = max(0, first - max(0, cap))
        end = min(len(pool), last + 1 + max(0, tail_keep))
        return list(pool[start:end])

    @staticmethod
    def _merge_at(visible: Sequence[Message], pool: Sequence[Message],
                  i: int, j: int, run: int) -> List[Message]:
        """把 visible[i:i+run] ↔ pool[j:j+run] 这段对齐块拼进池子。

        比对齐块更早的（可见窗口前缀）插到块前面，更晚的接在块后面。
        池子在块之后剩下的部分按**位置**保留：只有"与可见窗口后文逐位置同文本"的条目
        才认为是同一条消息（丢掉），其余一律保留 —— 早前按"文本去重"会把池子里
        另一条同文本消息也一起删掉（实测：池子少了中间那条"好"）。
        """
        head = list(pool[:j])
        head_keys = {m.msg_key for m in head}
        new_head = [m for m in visible[:i] if m.msg_key not in head_keys]
        block = list(visible[i:i + run])
        tail_new = list(visible[i + run:])
        tail_pool = list(pool[j + run:])
        kept_tail = [m for k, m in enumerate(tail_pool)
                     if not (k < len(tail_new) and tail_new[k].msg_key == m.msg_key)]
        return head + new_head + block + tail_new + kept_tail

    @classmethod
    def _merge_pool(cls, visible: Sequence[Message], pool: Sequence[Message]
                    ) -> Tuple[List[Message], int]:
        """把"当前可见窗口"合并进累积池；返回 (新池子, 重叠条数)。

        【第 60 轮修复 · 用户反馈"上下文跟之前的历史池串了"】
        消息身份是"会话 + 归一化文本"（SPEC 4.4，严禁含哈希），所以同一会话里
        "嗯/好/哈哈"这种短句会有多条**完全相同的 msg_key**。旧实现找锚点时用的是
        "池子里**第一次**出现该 key 的位置"，于是窗口滚到第二组"嗯/好"时会被锚到第一组，
        合并后**更旧的条目被搬到新消息后面**（实测复现：`tmp/test_pool_merge.py` 场景 1，
        池子从 [嗯,好,A,B,嗯,好,C,D,E,F] 变成 [嗯,好,C,D,E,F,A,B]）→
        之后 build_context 取"目标之前的池内消息"就会把旧池的内容混进来。

        现在改成**对齐打分**：
          ① 枚举所有 (可见行 i, 池子行 j) 且 key 相同的位置，算前后连续匹配长度 overlap；
          ② 取 overlap 最大的一组；并列时取"合并后保留条目最多"的一组（信息损失最小），
             再并列取池子位置更靠后的（即更靠近最新消息的那一组）；
          ③ overlap ≤ 1 且该文本在池子里重复出现（无法区分是哪一条）→ 视为无重叠，
             交给调用方按第 53 轮口径**重置成当前窗口**，不硬拼。
        """
        vis_keys = [m.msg_key for m in visible]
        pool_keys = [m.msg_key for m in pool]
        if not vis_keys or not pool_keys:
            return list(visible), 0
        candidates: List[Tuple[int, int, int]] = []      # (overlap, i, j)
        for i, key in enumerate(vis_keys):
            for j, other in enumerate(pool_keys):
                if key != other:
                    continue
                back = 0
                while (i - back - 1 >= 0 and j - back - 1 >= 0
                       and vis_keys[i - back - 1] == pool_keys[j - back - 1]):
                    back += 1
                forward = 1
                while (i + forward < len(vis_keys) and j + forward < len(pool_keys)
                       and vis_keys[i + forward] == pool_keys[j + forward]):
                    forward += 1
                candidates.append((back + forward, i, j))
        if not candidates:
            return list(visible), 0
        best_overlap = max(item[0] for item in candidates)
        # 并列时的判优顺序（都用过才定下来的，见 tmp/test_pool_merge.py）：
        #   ① 连续匹配最长（上面已过滤）；
        #   ② 对齐后"可见窗口前缀里有几条已经落在 head 里"——这种候选等于把同一批消息
        #      既当成更早的又当成新的（说明锚点取在了匹配块的尾端），越少越好；
        #   ③ 合并后保留条目最多（信息损失最小）；
        #   ④ 池子位置更靠后（更贴近最新消息）。
        best: Optional[Tuple[Tuple[int, int, int, int], List[Message]]] = None
        for overlap, i, j in candidates:
            if overlap != best_overlap:
                continue
            merged = cls._merge_at(visible, pool, i, j, overlap)
            head_keys = {m.msg_key for m in pool[:j]}
            duplicated = sum(1 for m in visible[:i] if m.msg_key in head_keys)
            score = (-duplicated, len(merged), j, -i)
            if best is None or score > best[0]:
                best = (score, merged)
        assert best is not None
        if best_overlap <= 1:
            # 单条重叠时，只有"这条文本在两边都唯一"才敢用
            i, j = candidates[0][1], candidates[0][2]
            for overlap, ci, cj in candidates:
                if overlap == best_overlap:
                    i, j = ci, cj
            if (vis_keys.count(vis_keys[i]) > 1 or pool_keys.count(pool_keys[j]) > 1):
                return list(visible), 0
        return best[1], best_overlap

    def latest_other_party(self, messages: Sequence[Message]) -> Optional[Message]:
        for msg in reversed(list(messages)):
            if msg.is_other_party and not msg.is_image:
                return msg
        return None

    def build_context(self, messages: Sequence[Message], target: Message,
                      max_messages: Optional[int] = None) -> List[Message]:
        """SPEC 5.4：同一会话、目标消息及其之前的最近 N 条（含自己说的话）。

        再往前补上"虚拟化历史"（树里有文本但没有坐标的旧消息）：它们以 side="history"
        进入上下文，prompt 里标成 `历史:` —— 说话人未知但能补足话题背景。
        """
        limit = max_messages or self.cfg.analyzer.context_messages
        # 虚拟化历史（无坐标无下标的旧消息）先算出来：行下标分支与兜底分支都要用
        history_msgs = [Message(msg_key=make_msg_key("history", t), text=t, side="history",
                                bbox=(0, 0, 0, 0))
                        for t in getattr(self.uia, "history_texts", [])]
        # 【优先用行下标判定"前文/后文"】下标是真实顺序，不受滚动/图片/重复文本影响。
        # 目标有下标时：直接从池子里取"下标比它小"的最近若干条当前文（比按坐标可靠得多，
        # 也解决了"目标在可视区顶部时前文只有一两条"的老问题）。
        # 【已撤销：按 row_index 判定前文/后文】同上的原因（虚拟化下标不稳定）。
        # 仍用"当前可见 + 重叠锚点合并出的池子"判断前文，见下面。
        upto = [m for m in messages if m.bbox[1] <= target.bbox[3] and not m.is_image]
        visible = upto[-limit:]
        # 用"累积历史"补足已经滚出可视区的前文（这是上下文关联性的主要来源）。
        # 只取目标之前的、且不在当前可见集里的；按观察顺序取最近的若干条。
        known = {m.msg_key for m in visible}
        # 【第 54 轮修正】前文/后文必须按**池子时间轴**切分：
        # 旧实现是"池内不在可见集里的一律当前文"，而"可见集"只算 y ≤ 目标底边的那些，
        # 于是**屏幕下方（比目标更晚）**的消息会被贴进【背景】——实测目标"便宜丈育猫娘"
        # 的背景里混进了它之后的"艹了，说便宜也8便宜了"（tmp/diag_context_order.py 复现），
        # 小模型会把它当成"目标在回应的话"，情绪/危险度/建议全变。
        keys = [m.msg_key for m in self._history]
        try:
            target_pool_index: Optional[int] = len(keys) - 1 - keys[::-1].index(target.msg_key)
        except ValueError:
            target_pool_index = None
        if target_pool_index is None:
            # 兜底：目标不在池里（刚切会话、池子刚被重置等）。
            # 旧实现把**整池**都当"目标之前的前文"塞进背景 —— 池子里明明有比目标更晚的
            # 消息，也会被当成上文（这是用户反馈"上下文串了"的另一条路径）。
            # 现在干脆不用池子：宁可少给背景，也不给错位的背景（第 53 轮"宁缺毋滥"口径）。
            older: List[Message] = []
            later: List[Message] = []
        else:
            older = [m for m in self._history[:target_pool_index]
                     if m.msg_key not in known and not m.is_image]
            later = [m for m in self._history[target_pool_index + 1:]
                     if m.msg_key not in known and not m.is_image]
        # 只取**与目标同一段**的池内消息：跨段（跳跃滚动造成的断点）之间顺序不可知，
        # 宁可少给前文，也不把别的位置的消息混进来（用户反馈的"大段倒置"根因）。
        older = [m for m in older if m.segment == target.segment]
        need = max(0, limit - len(visible))
        older = older[-need:] if need else []
        # 目标之后的一两条也带上（side="after"，只作理解用）：像"列清单"这种场景，
        # 单独看一条常常判断不出它在接续什么，必须知道后面还在继续列。
        following_count = int(getattr(self.cfg.analyzer, "context_following", 0) or 0)
        after: List[Message] = []
        if following_count > 0:
            # 后文 = 池子里比目标晚的（时间轴权威）+ 屏幕下方还没进池的，按时间顺序去重取前 N 条
            sequence = list(later)
            pooled = {m.msg_key for m in later}
            sequence += [m for m in messages
                         if m.bbox[1] > target.bbox[3] and not m.is_image
                         and m.msg_key not in pooled]
            picked, seen_after = [], set()
            for msg in sequence:
                if msg.msg_key in seen_after or msg.msg_key == target.msg_key:
                    continue
                seen_after.add(msg.msg_key)
                picked.append(msg)
                if len(picked) >= following_count:
                    break
            after = [Message(msg_key=m.msg_key, text=m.text, side="after", bbox=m.bbox)
                     for m in picked]
        base = list(history_msgs[-limit:]) + list(older) + list(visible)
        # 去重：累积历史里存的是**旧坐标**，同一条消息可能既落进"前文"又满足"后文"条件，
        # 从而在前文和后文里各出现一次（dump 里实测到过）→ 这里按 msg_key 排掉重复。
        seen = {m.msg_key for m in base} | {target.msg_key}
        after = [m for m in after if m.msg_key not in seen]
        return base + after

    def refresh_positions(self) -> Tuple[Dict[str, Sequence[int]], int]:
        """把已缓存消息的坐标快速刷新一遍（供 30ms 级跟随使用）。"""
        if not self.uia.available:
            return ({}, 0)
        rects = self.uia.refresh_positions()
        return (rects, self.uia.last_refresh_stale)

    def refresh_one(self, msg_key: str) -> Optional[Sequence[int]]:
        """只刷一条消息的矩形（30ms 级跟随用，1-3ms）。"""
        if not self.uia.available:
            return None
        return self.uia.refresh_one(msg_key)

    def refresh_any(self) -> Optional[tuple]:
        """任取一条还读得到的消息坐标（目标被回收时的备用锚点）。"""
        if not self.uia.available:
            return None
        return self.uia.refresh_any()

    def close(self) -> None:
        self.uia.close()


if __name__ == "__main__":
    import json
    import logging as _logging

    from config import load_config

    _logging.basicConfig(level=_logging.INFO, format="%(levelname)-7s %(message)s")
    w32.set_dpi_awareness()
    reader = MessageReader(load_config())
    msgs, snap = reader.read()
    print(json.dumps({"status": snap, "messages": [m.as_dict() for m in msgs]},
                     ensure_ascii=False, indent=2))
