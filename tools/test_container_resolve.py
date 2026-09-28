#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 78 轮回归：消息列表容器定位的三级兜底 + 输入框几何兜底（合成树，不依赖 QQ）。

背景：QQ 9.9.36 把「消息列表」容器从 Window 改成 Pane，旧代码写死
`descendants(control_type="Window", title="消息列表")` → 全部功能失效。
本测试用假节点验证：
  ① 老结构（Window + 标题）仍然走旧路径（行为不变）；
  ② 新结构（Pane + 标题）能按标题找到，并且优先于"文本更多但没标题"的干扰容器；
  ③ 连标题都没了 → 几何兜底仍然选对；
  ④ 真找不到 → 返回 None（不许瞎选一个）；
  ⑤ 输入框：底部"有文本、内部没有按钮"的那条被选中（带按钮的发送行被排除）。

用法：python tools/test_container_resolve.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.message_reader import UiaBackend  # noqa: E402

TITLE = app_config.Config().uia.message_list_title


class FakeRect:
    def __init__(self, box: Sequence[int]) -> None:
        self.left, self.top, self.right, self.bottom = box


class FakeInfo:
    def __init__(self, name: str, control_type: str) -> None:
        self.name = name
        self.control_type = control_type


class FakeNode:
    """极简 UIA 元素替身：只实现 reader 用到的那几个方法。"""

    def __init__(self, name: str, ctype: str, box: Sequence[int],
                 texts: Optional[List[str]] = None,
                 children: Optional[List["FakeNode"]] = None) -> None:
        self.element_info = FakeInfo(name, ctype)
        self.box = list(box)
        self._texts = list(texts or [])
        self.children = list(children or [])

    def rectangle(self) -> FakeRect:
        return FakeRect(self.box)

    def descendants(self, control_type: Optional[str] = None,
                    title: Optional[str] = None) -> List["FakeNode"]:
        out: List[FakeNode] = []
        for child in self.children:
            if control_type and child.element_info.control_type != control_type:
                pass
            elif title and title not in (child.element_info.name or ""):
                pass
            else:
                out.append(child)
            out.extend(child.descendants(control_type=control_type, title=title))
        return out

    def texts(self) -> List[str]:
        return list(self._texts)


def text_node(text: str, box: Sequence[int]) -> FakeNode:
    return FakeNode(text, "Text", box, texts=[text])


def message_list_pane(name: str, ctype: str, box: Sequence[int],
                      texts: int) -> FakeNode:
    kids = [text_node(f"消息{i}", [box[0] + 20, box[1] + 20 + i * 60,
                                   box[2] - 20, box[1] + 50 + i * 60])
            for i in range(texts)]
    return FakeNode(name, ctype, box, children=kids)


WINDOW = [0, 0, 1400, 1400]


def make_backend(root: FakeNode) -> UiaBackend:
    backend = UiaBackend(app_config.Config())
    backend._wrapper = root
    return backend


def case_legacy() -> bool:
    """① 老 QQ：Window 类型 + 标题 → 走旧路径。"""
    pane = message_list_pane(TITLE, "Window", [40, 200, 1360, 880], 9)
    root = FakeNode("会话", "Window", WINDOW, children=[pane])
    backend = make_backend(root)
    got = backend.resolve_list_container(force=True)
    ok = got is pane and "旧版" in backend._list_strategy
    print(f"① 老结构 Window+标题 → {'对' if ok else '错'}（{backend._list_strategy!r}）")
    return ok


def case_title_new_qq() -> bool:
    """② 新 QQ：Pane + 标题，且有"文本更多的无标题容器"当干扰。"""
    intruder = message_list_pane("", "Pane", [40, 200, 1360, 880], 12)
    pane = message_list_pane(TITLE, "Pane", [40, 200, 1360, 880], 9)
    root = FakeNode("会话", "Window", WINDOW, children=[intruder, pane])
    backend = make_backend(root)
    got = backend.resolve_list_container(force=True)
    ok = got is pane and "标题" in backend._list_strategy
    print(f"② 新结构 Pane+标题（旁边有 12 条文本的干扰容器）→ {'对' if ok else '错'}"
          f"（{backend._list_strategy!r}）")
    return ok


def case_geometry() -> bool:
    """③ 标题都没了 → 几何兜底选"包含可见 Text 最多"的容器。"""
    small = message_list_pane("", "Pane", [40, 200, 500, 500], 3)
    big = message_list_pane("聊天区", "Pane", [40, 200, 1360, 880], 11)
    root = FakeNode("会话", "Window", WINDOW, children=[small, big])
    backend = make_backend(root)
    got = backend.resolve_list_container(force=True)
    ok = got is big and "几何" in backend._list_strategy
    print(f"③ 标题缺失 → {'对' if ok else '错'}（{backend._list_strategy!r}）")
    return ok


def case_geometry_rejects_sidebar() -> bool:
    """⑥b（第 79 轮真 bug）主窗口场景：左侧"会话列表"不能当消息列表。

    用户反馈："打开新会话时，有时会定位到左侧的会话列表而不是聊天界面"。
    主窗口里左侧会话列表是"一大片带文字的行"，纯几何打分它会赢 → 必须靠
    container_geo_min_width_ratio / container_geo_min_left_ratio 排除。
    """
    sidebar = message_list_pane("", "Pane", [20, 130, 500, 1280], 17)      # 窄、贴左边
    chat = message_list_pane("", "Group", [560, 300, 1990, 1000], 7)       # 宽、在右边
    root = FakeNode("会话", "Window", [0, 100, 2000, 1300], children=[sidebar, chat])
    backend = make_backend(root)
    got = backend.resolve_list_container(force=True)
    ok = got is chat and "几何" in backend._list_strategy
    print(f"③b 几何兜底必须排除左侧会话列表 → {'对' if ok else '错'}"
          f"（选中 {None if got is None else got.box}）")
    return ok


def case_none() -> bool:
    """④ 什么都没有 → None（不许瞎选）。"""
    root = FakeNode("会话", "Window", WINDOW,
                    children=[FakeNode("按钮", "Button", [10, 10, 60, 60])])
    backend = make_backend(root)
    got = backend.resolve_list_container(force=True)
    ok = got is None
    print(f"④ 树里没有可用容器 → {'对' if ok else '错'}（返回 None）")
    return ok


def case_input_area() -> bool:
    """⑤ 输入框几何兜底：挑"有文本、无按钮"的底部容器，排除带按钮的发送行。"""
    strip = FakeNode("", "Group", [40, 1092, 1360, 1274],
                     texts=["按住 Win + Alt，使用语音输入文字"])
    send_row = FakeNode("", "Group", [40, 1274, 1360, 1380], children=[
        FakeNode("发送", "Button", [1200, 1290, 1250, 1340])])
    toolbar = FakeNode("会话", "ToolBar", [40, 1016, 1360, 1092], children=[
        FakeNode("表情", "Button", [70, 1030, 130, 1090])])
    wrapper = FakeNode("", "Group", [40, 1057, 1360, 1380])       # 外层包裹（无文本）
    root = FakeNode("会话", "Window", WINDOW,
                    children=[strip, send_row, toolbar, wrapper])
    backend = make_backend(root)
    backend.last_list_box = [40, 328, 1360, 1017]
    node = backend._resolve_input_area(*WINDOW)
    ok = node is strip
    print(f"⑤ 输入框兜底 → {'对' if ok else '错'}"
          f"（选中 {None if node is None else node.box}）")
    return ok


def case_input_area_without_text() -> bool:
    """⑥ 真事故复现：输入框**被聚焦/用过之后占位文本会消失** → texts() 变空。
    旧实现 best_score 初值 -1.0，而"无文本"候选得分是 -面积（负数）→ 全被拒，
    表现就是"点击选项又不自动粘贴了"。这里要求：没有文本也照样选中输入区。"""
    strip_notext = FakeNode("", "Group", [40, 1092, 1360, 1274])          # 无 texts
    send_row2 = FakeNode("", "Group", [40, 1274, 1360, 1380], children=[
        FakeNode("发送", "Button", [1200, 1290, 1250, 1340])])
    wrapper2 = FakeNode("", "Group", [40, 1057, 1360, 1380])
    root2 = FakeNode("会话", "Window", WINDOW,
                     children=[strip_notext, send_row2, wrapper2])
    backend2 = make_backend(root2)
    backend2.last_list_box = [40, 328, 1360, 1017]
    node2 = backend2._resolve_input_area(*WINDOW)
    ok2 = node2 is strip_notext
    print(f"⑥ 输入框无占位文本（聚焦后）→ {'对' if ok2 else '错'}"
          f"（选中 {None if node2 is None else node2.box}）")
    return ok2


def main() -> int:
    results = [case_legacy(), case_title_new_qq(), case_geometry(),
               case_geometry_rejects_sidebar(), case_none(), case_input_area(),
               case_input_area_without_text()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
