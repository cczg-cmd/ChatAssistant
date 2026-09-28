#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单次 API 连通性测试（用 config.json 里已填的 key，只发 1 个最小请求）。

输出：HTTP 状态 / 错误信息 / 原始返回（前 500 字）/ usage / finish_reason。
用途：当面板只显示"—"时，用它区分 401（key 错）、404（URL 错）、
      截断（finish_reason=length）、格式不符（模型没按 JSON 输出）等情况。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402


def main() -> int:
    cfg = app_config.load_config()
    api = cfg.api
    key = (api.api_key or "").strip()
    if not key:
        print("config.json 里没有 api_key，先在设置面板填好再来")
        return 2
    url = api.base_url.rstrip("/") + "/chat/completions"
    print(f"URL：{url}\n模型：{api.model}｜key：{key[:6]}…{key[-4:]}（共 {len(key)} 字符）")
    payload = {"model": api.model,
               "messages": [{"role": "system", "content": "你只输出 JSON。"},
                            {"role": "user", "content": '请输出 JSON：{"ok": true}'}],
               "temperature": 0.2, "max_tokens": 64,
               "response_format": {"type": "json_object"}}
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=float(api.timeout_s)) as response:
            body = json.loads(response.read().decode("utf-8"))
        choice = (body.get("choices") or [{}])[0]
        print("HTTP 200 ✓")
        print("finish_reason:", choice.get("finish_reason"))
        print("content:", repr((choice.get("message") or {}).get("content"))[:300])
        print("usage:", body.get("usage"))
        return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        print(f"HTTP {exc.code} ✗  {exc.reason}\n返回体：{detail}")
        return 1
    except Exception as exc:
        print(f"请求异常：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
