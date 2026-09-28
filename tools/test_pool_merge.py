#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文池合并的回归测试（不需要 QQ、不需要模型，毫秒级）。

做法：把 MessageReader 的 UIA 后端换成"脚本化的假窗口"，按剧本喂一串"可见窗口"，
然后断言累积池（reader._history）的顺序与真实对话一致。

覆盖三个场景：
  1) 重复短句（"嗯/好" 出现多次）时的锚点错配 —— 用户反馈的"上下文跟之前的池子串了"；
  2) 正常向上滚 / 向下滚 —— 池子要能正确累积（不能因为防错而退化成"每次重置"）；
  3) 完全没有重叠（跳跃滚动）—— 必须重置成当前窗口那一段。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core import message_reader as mr  # noqa: E402
from core.message_reader import Message, MessageReader, make_msg_key  # noqa: E402

SESSION = "1234:5678:测试会话"


class FakeWindow:
    hwnd = 1234
    pid = 5678
    title = "测试会话"
    rect = (0, 0, 1000, 800)
    iconic = False

    @property
    def session_id(self) -> str:
        return SESSION


class FakeUia:
    """假 UIA：read() 返回脚本里的"当前可见窗口"。"""

    def __init__(self) -> None:
        self.available = True
        self.hwnd = 1234
        self.last_error = None
        self.nodes = 60
        self.named = 20
        self.activation_s = 0.0
        self.last_list_box = (0, 0, 900, 800)
        self.history_texts: list[str] = []
        self.script: list[list[str]] = []

    def read(self, session_id: str) -> list[Message]:
        texts = self.script.pop(0) if self.script else []
        return [Message(msg_key=make_msg_key(session_id, t), text=t, side="left",
                        bbox=(0, i * 40, 900, i * 40 + 30))
                for i, t in enumerate(texts)]

    def close(self) -> None:
        pass


def make_reader() -> tuple[MessageReader, FakeUia]:
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    uia = FakeUia()
    reader.uia = uia
    mr.w32.find_qq_chat_window = lambda *a, **k: FakeWindow()
    return reader, uia


def feed(reader: MessageReader, uia: FakeUia, windows: list[list[str]]) -> None:
    uia.script = [list(w) for w in windows]
    while uia.script:
        reader.read()


def pool_texts(reader: MessageReader) -> list[str]:
    return [m.text for m in reader._history]


def check(name: str, got: list[str], expect: list[str]) -> bool:
    ok = got == expect
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       期望：{expect}")
        print(f"       实际：{got}")
    return ok


def main() -> int:
    results = []

    # --- 场景 1：重复短句导致的锚点错配 -------------------------------------
    # 对话： 嗯 好 A B 嗯 好 C D E F ——"嗯/好"出现两次。
    # 第一屏 = 前 6 条；随后窗口下移，落在**第二组**"嗯/好"上。
    # 旧实现按"池子里第一次出现的位置"定位锚点 → 会锚到第一组，
    # 于是 A/B 被搬到新消息后面（＝用户看到的"上下文跟之前的池子串了"）。
    conv = ["嗯", "好", "A", "B", "嗯", "好", "C", "D", "E", "F"]
    reader, uia = make_reader()
    feed(reader, uia, [conv[0:6], conv[4:10]])
    results.append(check("重复短句：池子顺序不能倒置/不能混进旧条目",
                         pool_texts(reader), list(conv)))

    # --- 场景 2：正常滚动（无重复文本）要能累积 ----------------------------
    conv2 = [f"消息{i:02d}" for i in range(20)]
    reader, uia = make_reader()
    feed(reader, uia, [conv2[8:16], conv2[4:12]])       # 向上滚 4 条
    results.append(check("向上滚：更旧的 4 条要插到前面",
                         pool_texts(reader), conv2[4:16]))
    feed(reader, uia, [conv2[8:16]])                     # 再向下滚回来
    results.append(check("向下滚：顺序仍然正确",
                         pool_texts(reader), conv2[4:16]))

    # --- 场景 3：完全没有重叠 → 重置成当前窗口 ------------------------------
    reader, uia = make_reader()
    feed(reader, uia, [conv2[10:18], ["另外一段A", "另外一段B", "另外一段C"]])
    results.append(check("无重叠：重置为当前窗口那一段（不拼接旧池）",
                         pool_texts(reader), ["另外一段A", "另外一段B", "另外一段C"]))

    # --- 场景 4：只重叠 1 条、但这句在两边都唯一 → 仍然要合并 -----------------
    reader, uia = make_reader()
    feed(reader, uia, [["A", "B", "C"], ["C", "D", "E"]])
    results.append(check("单条唯一重叠：正常合并", pool_texts(reader), ["A", "B", "C", "D", "E"]))

    # --- 场景 5：只重叠 1 条、且这句在池子里重复 → 不敢拼，重置 ---------------
    reader, uia = make_reader()
    feed(reader, uia, [["嗯", "A", "嗯", "B"], ["嗯", "C", "D"]])
    results.append(check("单条歧义重叠：宁缺毋滥（重置）",
                         pool_texts(reader), ["嗯", "C", "D"]))

    # --- 场景 6：重复短句 + 连续小步滚动 → 池子要覆盖走过的整段且顺序正确 -----
    conv3 = []
    for k in range(25):
        conv3.append(["嗯", "好", "哈哈"][k % 3] if k % 2 == 0 else f"话{k:02d}")
    reader, uia = make_reader()
    steps = [conv3[0:8], conv3[2:10], conv3[4:12], conv3[6:14], conv3[4:12], conv3[2:10]]
    feed(reader, uia, steps)
    results.append(check("重复短句 + 连续滚动：池子=走过的整段、顺序正确",
                         pool_texts(reader), conv3[0:14]))

    # --- 场景 7：长会话连续滚动 40 屏 → 池子要有上限，且目标始终还在池子里 -----
    long_conv = [f"长{i:03d}" for i in range(200)]
    reader, uia = make_reader()
    cap = int(app_config.load_config().analyzer.history_cache_size)
    tail_keep = int(app_config.load_config().analyzer.history_tail_keep)
    window = 8
    windows = [long_conv[o:o + window] for o in range(0, 160, 4)]     # 40 屏，每屏下移 4 条
    feed(reader, uia, windows)
    size = len(reader._history)
    limit = window + cap + tail_keep
    print(f"[{'PASS' if size <= limit else 'FAIL'}] 池子上限：{size} 条 ≤ "
          f"窗口 {window} + 之前 {cap} + 之后 {tail_keep} = {limit}")
    results.append(size <= limit)
    # 目标 = 当前窗口里的一条，必须在池子里（否则分析会退化成"没有上下文"）
    target_key = make_msg_key(SESSION, long_conv[156 + 3])
    in_pool = any(m.msg_key == target_key for m in reader._history)
    print(f"[{'PASS' if in_pool else 'FAIL'}] 当前窗口里的消息仍在池子里（目标不会被裁掉）")
    results.append(in_pool)
    order_ok = pool_texts(reader) == [t for t in long_conv if t in set(pool_texts(reader))]
    print(f"[{'PASS' if order_ok else 'FAIL'}] 裁剪后顺序仍然是从旧到新")
    results.append(order_ok)

    print(f"\n{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
