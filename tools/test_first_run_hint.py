#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：① 首次启动提示（一行、点一下淡出、只出现一次）② 选项条"点过就不再弹回来"。

不依赖 QQ：
  A. 提示条是**一行**文本，且淡出动画能走到 0 并自动 hide；
  B. `place_under()` 把提示放在状态灯下方，并在贴屏幕右缘时夹回屏幕内；
  C. `ui.first_run_hint_shown` 能落盘/读回（"后续启动不再显示"靠它）；
  D. `_options_suppressed()`：同一条消息点过选项后，后续 partial 不再弹选项条；
     换消息/刷新（清 key）后恢复正常。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config as app_config  # noqa: E402
import main as app_main  # noqa: E402
from core.analyzer import AnalysisResult, ReplyOption  # noqa: E402
from ui.first_run_hint import FirstRunHint  # noqa: E402


def case_single_line_and_fade() -> bool:
    cfg = app_config.load_config()
    hint = FirstRunHint(cfg)
    one_line = "\n" not in hint._text and len(hint._text) > 8
    for _ in range(12):                    # 不用事件循环，手动推帧
        hint._step_fade()
    ok = one_line and hint._alpha == 0.0 and not hint.isVisible()
    print(f"A 一行文本 + 淡出到 0：{'对' if ok else '错'}"
          f"（文本={hint._text[:24]!r}… alpha={hint._alpha}）")
    return ok


def case_place_under_dot() -> bool:
    cfg = app_config.load_config()
    hint = FirstRunHint(cfg)
    dot = QRect(1200, 300, 24, 24)
    chat = [300, 200, 1500, 1400]          # 聊天界面（消息列表矩形）
    hint.place_under(dot, [0, 0, 2560, 1440], chat)
    g = hint.geometry()
    # 用户口径：**向右延伸**改成**向左延伸** —— 右缘对齐状态灯，文字留在聊天界面内
    ok_right_aligned = (g.right() <= dot.right() + 2
                        and g.y() == dot.y() + dot.height() + 6
                        and g.x() >= chat[0] + 4)
    # 状态灯贴聊天界面右缘时，文字也不能越出聊天界面
    hint.place_under(QRect(1470, 300, 24, 24), [0, 0, 2560, 1440], chat)
    ok_clamped = hint.geometry().right() <= chat[2] - 4 + 1
    ok = ok_right_aligned and ok_clamped
    print(f"B 贴在状态灯下方并**向左延伸**（留在聊天界面内）：{'对' if ok else '错'}"
          f"（rect={g.x()},{g.y()},{g.right()} 灯右缘={dot.right()} 聊天区右缘={chat[2]}）")
    return ok


def case_flag_persist() -> bool:
    cfg = app_config.load_config()
    cfg.ui.first_run_hint_shown = True
    tmp_path = ROOT / "tmp" / "test_first_run_hint_config.json"
    app_config.save_config(cfg, tmp_path)
    back = app_config.load_config(tmp_path)
    ok = bool(back.ui.first_run_hint_shown) is True
    # 默认值必须是 False（否则新用户看不到提示）
    ok = ok and app_config.Config().ui.first_run_hint_shown is False
    try:
        tmp_path.unlink()
    except OSError:
        pass
    print(f"C 标记落盘/读回（默认 False）：{'对' if ok else '错'}")
    return ok


class Stub:
    def __init__(self, dismissed=None):
        self._options_dismissed_key = dismissed


def case_options_suppressed() -> bool:
    result = AnalysisResult(msg_key="K", json_ok=True,
                            replies=[ReplyOption(style="展开", text="好呀")])
    hit = app_main.ChatAssistantApp._options_suppressed(Stub("K"), result)
    other = app_main.ChatAssistantApp._options_suppressed(Stub("OTHER"), result)
    cleared = app_main.ChatAssistantApp._options_suppressed(Stub(None), result)
    no_replies = app_main.ChatAssistantApp._options_suppressed(
        Stub("K"), AnalysisResult(msg_key="K", json_ok=True))
    ok = hit is True and other is False and cleared is False and no_replies is False
    print(f"D 点过的目标不再弹选项条：{'对' if ok else '错'}"
          f"（同目标={hit} 换目标={other} 清空后={cleared} 无选项={no_replies}）")
    return ok


class FakeBar:
    def __init__(self, text: str) -> None:
        self._text = text

    def option_text(self, index: int) -> str:
        return self._text if index == 0 else ""


class ClickStub:
    """只实现 _on_option_chosen 用到的东西（模拟"流式途中点击"）。"""

    def __init__(self, key: str) -> None:
        from core.message_reader import Message

        self.target = Message(msg_key=key, text="我发的", side="right", bbox=(0, 0, 10, 10))
        # 关键：流式期间 self.results 里只有第一段结果（replies 为空）
        self.results = {key: AnalysisResult(msg_key=key, json_ok=True, intent="答应")}
        self.cfg = app_config.Config()
        self.cfg.fill.enabled = True
        self.cfg.fill.mode = "paste"
        self.options = FakeBar("流式已经上屏的第一条")
        self._options_anim_target = 1.0
        self.filled: list = []

    def _place_windows(self) -> None:
        pass

    def _fill_after_collapse(self, text, mode, paste_at=None) -> None:
        self.filled.append(text)


def case_click_mid_stream() -> bool:
    """流式途中点击：必须按选项条上的文本填，而不是被判"越界"直接忽略。"""
    stub = ClickStub("K")
    app_main.ChatAssistantApp._on_option_chosen(stub, 0)
    ok = (stub.filled == ["流式已经上屏的第一条"]
          and stub._options_dismissed_key == "K"
          and stub._options_anim_target == 0.0)
    print(f"E 流式途中点选项就能填：{'对' if ok else '错'}（填了 {stub.filled!r}）")
    return ok


def main() -> int:
    app = QApplication(sys.argv)          # noqa: F841
    results = [case_single_line_and_fade(), case_place_under_dot(),
               case_flag_persist(), case_options_suppressed(), case_click_mid_stream()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
