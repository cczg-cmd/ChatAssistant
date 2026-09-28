#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui/theme.py - 浮窗配色主题（设置里可切换：经典 / 樱花 galgame）。

只改配色与圆角，不改布局与交互；面板、选项条共用同一套主题，
危险度色块仍走 cfg.ui.danger_bands（语义色不受主题影响）。
"""

from __future__ import annotations

from typing import Any, Dict

THEMES: Dict[str, Dict[str, Any]] = {
    # 现状：深灰 GitHub 风
    "classic": {
        "label": "经典深色",
        "panel_bg": (22, 27, 34),
        "panel_border": (48, 54, 61),
        "title": "#E6EDF3",
        "body": "#C9D1D9",
        "footer": "#8B949E",
        "accent": "#58A6FF",
        "hover": (56, 68, 90, 160),
        "radius": 10,
        "strip": None,
    },
    # 樱花 galgame：暗紫底 + 粉紫描边 + 顶部渐变细条
    "galgame": {
        "label": "樱花",
        "panel_bg": (38, 28, 52),
        "panel_border": (112, 74, 140),
        "title": "#FFE7F4",
        "body": "#EFDFF7",
        "footer": "#B69BCB",
        "accent": "#FF9ECB",
        "hover": (92, 60, 116, 170),
        "radius": 14,
        "strip": ("#FF9ECB", "#A98BFF"),
    },
}

DEFAULT_THEME = "classic"


def theme_names() -> list:
    return list(THEMES.keys())


def theme_label(name: str) -> str:
    return str(THEMES.get(name, THEMES[DEFAULT_THEME])["label"])


def theme(cfg: Any) -> Dict[str, Any]:
    name = str(getattr(getattr(cfg, "ui", None), "theme", "") or DEFAULT_THEME)
    return THEMES.get(name, THEMES[DEFAULT_THEME])
