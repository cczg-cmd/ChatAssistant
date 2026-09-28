#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：UIA 一键诊断包（配置化排查入口）。

要求：一个文件里要能看出"为什么选了这个容器、别的候选为什么被排除、当前阈值是多少"，
QQ 再更新时不用来回猜、也不用重打包就能定位该改哪个配置项。

本测试用合成树 + 桩 backend（不碰真 QQ）：
  A. collect() 给出 树规模/类型分布/容器候选（含排除原因）/输入框/策略配置；
  B. 主窗口场景：左侧会话列表候选必须被标成"排除"，聊天区候选标成 OK；
  C. write() 落盘 json + 人可读 txt（写进 tmp/，不污染 logs）。

用法：python tools/test_diagnose.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core import diagnose  # noqa: E402
from tools.test_container_resolve import FakeNode, message_list_pane  # noqa: E402


class FakeBackend:
    """给 diagnose.collect 用的最小 backend（字段名与 UiaBackend 一致）。"""

    def __init__(self, root, strategy="几何兜底", list_box=None, input_box=None) -> None:
        self._wrapper = root
        self._all_nodes = [root] + list(root.descendants())
        self._list_strategy = strategy
        self._input_strategy = "几何兜底(底部输入区)"
        self.last_list_box = list_box
        self._list_container = None
        self._input_element = None

    # --- UiaBackend 里被 diagnose 用到的几个方法 ---
    _node_name = staticmethod(lambda node: node.element_info.name)
    _node_type = staticmethod(lambda node: node.element_info.control_type)
    _inside = staticmethod(lambda box, outer, slack=2: (
        box[0] >= outer[0] - slack and box[2] <= outer[2] + slack
        and box[1] >= outer[1] - slack and box[3] <= outer[3] + slack))

    @staticmethod
    def _node_box(node):
        rect = node.rectangle()
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return [rect.left, rect.top, rect.right, rect.bottom]

    def _all_nodes_cached(self):
        return self._all_nodes

    def _container_types(self):
        return ("Window", "Pane", "Group", "Document", "List", "Custom", "ListItem")

    def find_input_element(self, _rect):
        return None


def main() -> int:
    cfg = app_config.load_config()
    sidebar = message_list_pane("", "Pane", [20, 130, 500, 1280], 17)
    chat = message_list_pane("", "Group", [560, 300, 1990, 1000], 7)
    root = FakeNode("会话", "Window", [0, 100, 2000, 1300], children=[sidebar, chat])
    backend = FakeBackend(root, list_box=[560, 300, 1990, 1000])

    data = diagnose.collect(backend, cfg)
    ok_a = (data["tree"]["nodes"] and data["tree"]["by_type"]
            and data["list_container"]["strategy"] == "几何兜底"
            and data["config"]["container_geo_min_width_ratio"] == 0.55
            and data["container_candidates"])
    print(f"A collect() 给出树/容器/候选/策略配置：{'对' if ok_a else '错'}"
          f"（节点 {data['tree']['nodes']}，候选 {len(data['container_candidates'])} 个）")

    candidates = data["container_candidates"]
    side = next((c for c in candidates if c["box"] == [20, 130, 500, 1280]), None)
    chat = next((c for c in candidates if c["box"] == [560, 300, 1990, 1000]
                 and c["type"] == "Group"), None)
    root_row = next((c for c in candidates if c["box"] == [0, 100, 2000, 1300]), None)
    ok_b = (side is not None and chat is not None
            and side["ok"] is False and chat["ok"] is True
            and any("像左侧会话列表那一条" in r for r in side["reasons"])
            # 整窗也必须被排除（否则会"以窗口为消息列表"导致面板贴到左上角）
            and (root_row is None or root_row["ok"] is False))
    print(f"B 左侧会话列表被标排除、聊天区标 OK：{'对' if ok_b else '错'}")
    if side:
        print(f"   （窄候选排除原因示例：{side['reasons']}）")

    out_dir = ROOT / "tmp"
    path = diagnose.write(backend, cfg, out_dir=out_dir, stamp="test")
    payload = json.loads(path.read_text(encoding="utf-8"))
    txt = path.with_suffix(".txt").read_text(encoding="utf-8")
    ok_c = (path.exists() and payload["time"] and "容器候选" in txt
            and "几何兜底" in txt)
    print(f"C 落盘 json + 可读 txt：{'对' if ok_c else '错'}（{path.name}）")
    for stale in (path, path.with_suffix(".txt")):
        try:
            stale.unlink()
        except OSError:
            pass

    results = [ok_a, ok_b, ok_c]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
