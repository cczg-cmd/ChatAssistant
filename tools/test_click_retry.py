#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：点击聊天气泡"偶尔没反应"的修复。

日志证据（用户反馈时的真实日志）：
    01:20:02.389 点击空白处 → 收起浮窗        ← 其实点在气泡上，但用的是旧矩形
    01:20:03.378 点击消息 → 开始分析：[我] …   ← 同一位置再点一次才中

两处修法：
  A. `pick_hovered_message(..., row_bounds=…)`：点击路径把命中范围横向放宽到**整行自己那一侧**
     （点头像、点气泡旁的空白也算命中；中间那条空隙仍不算，不会误命中对面）。
  B. 没命中时不再立刻判"点空白"，而是**申请一次加急读取并挂起这一击**，
      等新坐标到手再判一次（`_retry_pending_click`）；只有重判仍落空才收起浮窗。
      【第 79 轮续修正】这一支原来对所有"没命中"都成立 → 真正的空白点击也会被挂起 1.2s，
      表现为"点空白不收起、要再点一两下"（用户反馈）。现在按"面板开着与否 + 坐标新鲜度"
      分流：见 `main.ChatAssistantApp._on_click_miss`（用例在 `tools/test_blank_click.py`）。
  C. 双通道去重窗口从 0.30s 收到 0.18s（同一次点击的两条通道实测最长 ~6ms 就到达，
     窗口太大会把用户"点了没反应→马上再点一下"误吞掉）。

用法：python tools/test_click_retry.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as app_main  # noqa: E402
from config import Config  # noqa: E402
from core.message_reader import Message  # noqa: E402

MESSAGES = [
    Message(msg_key="a", text="对方说的", side="left", bbox=(400, 300, 600, 340)),
    Message(msg_key="b", text="我说的", side="right", bbox=(1000, 500, 1300, 540)),
]
ROW = [100, 100, 1500, 900]


def case_row_hit() -> bool:
    def hit(x, y, wide=True, only_other=False):
        result = app_main.pick_hovered_message(
            (x, y), MESSAGES, only_other_party=only_other,
            row_bounds=ROW if wide else None)
        return result.msg_key if result else None

    ok = (hit(500, 320) == "a"          # 气泡本体
          and hit(120, 320) == "a"      # 对方那行的头像/左侧空白（新）
          and hit(120, 320, wide=False) is None      # 旧行为：这里会漏
          and hit(1450, 520) == "b"     # 我这一行的右侧空白（新）
          and hit(1100, 520) == "b"     # 我的气泡本体
          and hit(800, 520) is None)    # 中间空隙：仍然谁都不算（不会误命中对面）
    print(f"A 整行命中放宽（点头像/边上也算）：{'对' if ok else '错'}")
    return ok


class FakeWidget:
    def isVisible(self) -> bool:
        return False


class FakeCapture:
    def __init__(self) -> None:
        self.now_calls = 0
        self.paused = None

    def is_paused(self) -> bool:
        return bool(self.paused)

    def request_now(self) -> None:
        self.now_calls += 1

    def set_paused(self, value: bool) -> None:
        self.paused = value


class ClickStub:
    """只实现点击判定用到的那几样东西。"""

    def __init__(self, hit_now) -> None:
        self.cfg = Config()
        self.cfg.ui.click_padding_px = 24
        self.paused = False
        self.window_rect = [100, 100, 1500, 900]
        self.panel = FakeWidget()
        self.hint = FakeWidget()
        self.capture = FakeCapture()
        self.target = None
        self.hit_now = hit_now
        self.finish_calls = 0
        self.render_calls = 0
        self._pending_click = None
        # `_on_click_miss` 要用：这份坐标"多久前读的"（这里故意很旧 → 走挂起分支）
        self.messages = MESSAGES
        self._messages_ts = time.time() - 3.0
        self._last_activity_ts = 0.0
        self.CLICK_FRESH_S = app_main.ChatAssistantApp.CLICK_FRESH_S
        self.PENDING_CLICK_VISIBLE_S = app_main.ChatAssistantApp.PENDING_CLICK_VISIBLE_S
        self.PENDING_CLICK_HIDDEN_S = app_main.ChatAssistantApp.PENDING_CLICK_HIDDEN_S

    # --- 被测逻辑的分支点 ---
    def _point_in_overlays(self, *_a) -> bool:
        return False

    def _hovered_message(self, padding=None, wide=False):
        return self.hit_now

    def _finish_empty_click(self, x, y) -> None:
        self.finish_calls += 1

    def _original_message(self, key, near_bbox=None):      # 第 80 轮：多了坐标参数
        return next((m for m in MESSAGES if m.msg_key == key), None)

    def _render_target(self) -> None:
        self.render_calls += 1

    def _log_text(self, text, limit=16):
        return text[:limit]

    _point_in_qq_window = app_main.ChatAssistantApp._point_in_qq_window
    _retry_pending_click = app_main.ChatAssistantApp._retry_pending_click
    _on_click_miss = app_main.ChatAssistantApp._on_click_miss


def case_retry_on_miss() -> bool:
    stub = ClickStub(hit_now=None)
    app_main.w32.get_cursor_pos = lambda: (500, 320)      # 假装点在对方气泡上
    app_main.ChatAssistantApp._toggle_target(stub, 24)
    deferred_ok = (stub._pending_click is not None and stub.capture.now_calls == 1
                   and stub.finish_calls == 0)
    print(f"B1 没命中 → 挂起并申请加急读取（不收起面板）：{'对' if deferred_ok else '错'}"
          f"（request_now={stub.capture.now_calls}，收起次数={stub.finish_calls}）")

    # 新坐标到手 → 补判命中 → 开始分析
    stub.hit_now = next(m for m in MESSAGES if m.msg_key == "a")
    stub._retry_pending_click()
    hit_ok = (stub.target is not None and stub.target.msg_key == "a"
              and stub.render_calls == 1 and stub._pending_click is None)
    print(f"B2 新坐标补判命中 → 开始分析：{'对' if hit_ok else '错'}"
          f"（target={getattr(stub.target, 'msg_key', None)}）")

    # 重判仍不中、且超过 1.2s → 才按"点空白"处理
    stub2 = ClickStub(hit_now=None)
    stub2._pending_click = {"t": time.time() - 2.0, "x": 500, "y": 320}
    stub2._retry_pending_click()
    timeout_ok = stub2.finish_calls == 1 and stub2._pending_click is None
    print(f"B3 重判仍落空且超时 → 按点空白处理：{'对' if timeout_ok else '错'}"
          f"（收起次数={stub2.finish_calls}）")
    return deferred_ok and hit_ok and timeout_ok


def case_dedup_window() -> bool:
    stub = ClickStub(hit_now=None)
    stub._last_click_handled = time.time() - 0.25       # 0.25s 前处理过
    recent = app_main.ChatAssistantApp._click_recently_handled(stub)
    stub._last_click_handled = time.time() - 0.05       # 0.05s 前（同一次点击的第二通道）
    same = app_main.ChatAssistantApp._click_recently_handled(stub)
    ok = (recent is False) and (same is True)
    print(f"C 去重窗口 0.18s（0.25s 前的可再点、0.05s 内的仍算同一击）：{'对' if ok else '错'}")
    return ok


def case_prompt_wording() -> bool:
    """发言选项：要"接着自己这条继续说"，不是"回应自己发的消息"。"""
    import config as app_config
    from core import analyzer as am
    from core.prompt_import import build_template
    cfg = app_config.Config()
    field = app_config.field_prompt(cfg, "option", cfg.self_fields,
                                    app_config.DEFAULT_SELF_FIELD_PROMPTS)
    tpl = build_template()
    # 第 80 轮（用户实测："发言选项老是变成同一套『周六下午两点别迟到』『票价80肉疼』
    # 『改天吧』"）：那正是方向示范池的原文 —— 模型会照抄，所以示范池整体删掉，
    # 风格完全交给配置自己的字段说明。这里断言"不再有可照抄的示例句"。
    demos_ok = not hasattr(am, "_SELF_OPTION_DEMOS")
    # 第 79 轮：只给正面约束（❌ 反例会喂给模型）；且**不再要求每条都带「我」**
    # （用户反馈那样太模板化、多样性下降）→ 改成"不必每条都出现「我」+ 三条开头句式要有变化"
    ok = ("接着自己这条继续往下说" in field
          and "不必每条都出现「我」" in field and "开头与句式不要一样" in field
          and "扮演「我」" in field
          # 第 82 轮：模板改成"角色包怎么写 + 字段格式规范 + 骨架"，只校验骨架里有这两个字段
          and "发言选项[" in tpl and "角色包：" in tpl
          and demos_ok)
    print(f"D 发言选项语义（扮演「我」、接着自己这句、无示例句可抄）：{'对' if ok else '错'}")
    return ok


def case_catgirl_self_keeps_meow() -> bool:
    """第 79 轮：猫娘配置的**自我分析**那套也要带"喵"（以前只有对方那套有 → 自我那侧丢喵）。"""
    import config as app_config
    from core import analyzer as am
    from core.message_reader import Message

    profiles = {entry["name"]: entry for entry in app_config.default_profiles()}
    cat = profiles[app_config.CATGIRL_PROFILE_NAME]
    field_ok = all("喵" in str(cat.get(key) or "") for key in
                   ("self_suggestion_prompt", "self_option_prompt", "self_style_prompt"))

    cfg = app_config.Config()
    cfg.fields_profiles = app_config.default_profiles()
    app_config.apply_profile(cfg, app_config.CATGIRL_PROFILE_NAME)
    analyzer = am.Analyzer(cfg)
    target = Message(msg_key="me", text="在吗", side="right", bbox=(0, 0, 100, 40))
    prompt = analyzer.build_messages([target], target)[1]["content"]
    header_ok = prompt.startswith("【这是我") and "接着自己那句话继续发给对方" in prompt
    meow_field = app_config.field_prompt(cfg, "suggestion", cfg.self_fields,
                                        app_config.DEFAULT_SELF_FIELD_PROMPTS)
    ok = field_ok and header_ok and ("喵" in meow_field)
    print(f"E 猫娘配置自我那套带喵 + 方向头：{'对' if ok else '错'}"
          f"（配置字段含喵={field_ok}，方向头={header_ok}）")
    return ok


def case_click_during_switch_quiet() -> bool:
    """第 79 轮：切换会话后的安静期内点击气泡 —— 必须提前结束安静期并要求加急读取。

    旧行为：安静期 1.5s 内不读新坐标，而缓存里还是**上一个会话**的矩形 →
    点新会话的气泡必然判"没命中"，前几下点击全落空（用户反馈）。
    """
    stub = ClickStub(hit_now=None)
    stub._switch_quiet_until = time.time() + 1.0        # 刚切完会话
    app_main.w32.get_cursor_pos = lambda: (500, 320)
    app_main.ChatAssistantApp._toggle_target(stub, 24)
    ok = (stub._switch_quiet_until == 0.0 and stub.capture.paused is False
          and stub.capture.now_calls >= 1)
    print(f"F 安静期内点击 → 提前恢复读取 + 加急读：{'对' if ok else '错'}"
          f"（quiet={stub._switch_quiet_until}，paused={stub.capture.paused}，"
          f"request_now={stub.capture.now_calls}）")
    return ok


def main() -> int:
    results = [case_row_hit(), case_retry_on_miss(), case_dedup_window(),
               case_prompt_wording(), case_catgirl_self_keeps_meow(),
               case_click_during_switch_quiet()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
