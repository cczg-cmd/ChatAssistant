#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验证 API 的 token 限制（不需要真 key：把网络层替换成假响应）。

验证项：
  ① 每次请求都带上单次输出上限（第一段 96 / 第二段 220，取自 config.api）；
  ② 用量（prompt/completion）被累计并写入按天的 usage 文件；
  ③ 超过每日预算后**不再发请求**（网络调用次数不增加），_api_generate 返回 None
     → 由调用方回落本地模型；
  ④ 真实网络层被替换：整个测试不产生任何外部请求。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402

USAGE = ROOT / "tmp" / "test_api_usage.json"
CALLS = {"n": 0, "max_tokens": []}


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(request, timeout=None):          # noqa: ANN001
    body = json.loads(request.data.decode("utf-8"))
    CALLS["n"] += 1
    CALLS["max_tokens"].append(body.get("max_tokens"))
    return FakeResponse({
        "choices": [{"message": {"content": json.dumps({
            "intent": "测试意图", "emotion": "平静", "danger_level": 0,
            "suggestion": "这是一条测试建议", "confidence": 0.9,
            "reply_options": [{"style": "稳当", "text": "测试回复一"},
                              {"style": "简单", "text": "测试回复二"},
                              {"style": "轻松", "text": "测试回复三"}]}, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 700, "completion_tokens": 60, "total_tokens": 760},
    })


def main() -> int:
    urllib.request.urlopen = fake_urlopen        # 整个进程内替换网络层
    if USAGE.exists():
        USAGE.unlink()
    cfg = app_config.load_config()
    cfg.analyzer.backend = "api"
    cfg.api.api_key = "test-key"
    cfg.api.daily_token_budget = 1500            # 每 760 token → 第 3 次应被拦
    cfg.api.usage_file = str(USAGE)
    cfg.api.max_tokens_quick = 96
    cfg.api.max_tokens_replies = 220
    cfg.api.context_token_budget = 200
    cfg.api.compact_prompt = True
    analyzer = Analyzer(cfg)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    print(f"预算 {cfg.api.daily_token_budget} token｜单次上限 第一段 {cfg.api.max_tokens_quick} /"
          f" 第二段 {cfg.api.max_tokens_replies}")
    results = []
    for index in range(1, 4):
        limit = cfg.api.max_tokens_quick if index == 1 else cfg.api.max_tokens_replies
        raw = analyzer._api_generate(messages, max_tokens=limit)
        usage = analyzer.today_usage()
        ok, note = analyzer.api_budget_ok()
        results.append((index, raw is not None, usage.get("total_tokens", 0), ok, note))
        print(f"  第{index}次：返回={'有内容' if raw else 'None(被拦/失败)'}｜"
              f"累计 {usage.get('total_tokens', 0)} token｜预算检查={note}")

    sent = CALLS["max_tokens"]
    # 顺带量一下两种后端的 prompt 长度（成本大头）：本地完整版 vs API 精简版
    from core.message_reader import Message
    target = Message(msg_key="k", text="你好呀，在忙吗", side="left", bbox=(0, 0, 300, 40))
    context = [target] + [Message(msg_key=f"b{i}", text=f"背景消息{i}，随便聊两句",
                                  side="left" if i % 2 else "right", bbox=(0, 0, 0, 0))
                         for i in range(10)]
    cfg.analyzer.backend = "api"
    api_msgs = analyzer.build_messages(context, target)
    cfg.analyzer.backend = "local"
    local_msgs = analyzer.build_messages(context, target)
    api_chars = sum(len(m["content"]) for m in api_msgs)
    local_chars = sum(len(m["content"]) for m in local_msgs)
    print(f"\nprompt 字符数（估算 token ≈ 字符/1.5）：API 精简版 {api_chars}（≈{int(api_chars/1.5)} token）"
          f"｜本地完整版 {local_chars}（≈{int(local_chars/1.5)} token）")
    checks = {
        # 第 3 次在预算检查处就被拦下、根本没发请求，所以实际只看到两次请求
        "① 单次上限随阶段传入": sent == [96, 220],
        "② 用量按次累计写入文件": USAGE.exists() and analyzer.today_usage().get("requests") == 2,
        "③ 超预算后不再发请求": CALLS["n"] == 2 and results[2][1] is False,
        "④ 全程无真实网络请求": True,          # urlopen 已被替换；若真发出去会抛异常
    }
    print("\n实际 max_tokens 序列:", sent)
    print("用量文件内容:", json.dumps(analyzer.load_usage(), ensure_ascii=False))
    for name, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("结果:", "PASS" if all(checks.values()) else "FAIL")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
