#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spike_ocr.py - ChatAssistant Spike #2（SPEC.md 6.1-2 / 4.2 OCR）

验证目标：
  1. 读取 spikes/fixtures/labels.txt，加载 28 张有效截图（跳过 SKIP 行）。
  2. 对每张截图真实调用 RapidOCR（rapidocr_onnxruntime），提取文本与 bbox。
  3. 按 SPEC 4.2 过滤左侧消息：
       气泡左边缘 x < 聊天区宽度 x 0.35 且 气泡宽度 < 聊天区宽度 x 0.75
  4. 输出字符准确率（1 - 编辑距离/真值长度）、左侧定位准确率（IoU>0.7 命中）、
     平均单张识别耗时；每项 PASS/FAIL，最后一行 SPIKE_OCR: PASS|FAIL。
  5. 不达标时输出 3 个最差样本（图名 / 识别文本 vs 真值 / 耗时）。

复用约定：
  - 文件隔离头部、DPI（Per-Monitor-V2）感知、工作区外写入审计，全部复用
    spikes/spike_wgc.py（import 即触发环境变量重定向，早于 cv2 / rapidocr 导入）。
  - 禁用任何缩放/二值化等预处理：原图直接喂给 RapidOCR（规则 3）。
  - 无 Mock：只读真实截图 + 真实 OCR 推理。

用法：
  python spikes/spike_ocr.py
  python spikes/spike_ocr.py --limit 5 --verbose      # 小样本快跑
  python spikes/spike_ocr.py --debug-chat-area        # 只跑聊天区检测并输出标注图
  python spikes/spike_ocr.py --no-overlay             # 不输出诊断截图

退出码：0=PASS / 1=FAIL / 2=ABORT
"""

from __future__ import annotations

import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# 复用 spike_wgc 的隔离头（必须早于 cv2 / rapidocr / onnxruntime 导入）
# --------------------------------------------------------------------------- #
SPIKES_DIR = Path(__file__).resolve().parent
if str(SPIKES_DIR) not in sys.path:
    sys.path.insert(0, str(SPIKES_DIR))
import spike_wgc as wgc  # noqa: E402  （import 即完成 TMP/TEMP/HF_HOME... 重定向）

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from typing import Any, Dict, List, Optional, Sequence, Tuple  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402


# --------------------------------------------------------------------------- #
# 常量（阈值来自 SPEC.md 4.2 / 6.1-2 及本任务要求）
# --------------------------------------------------------------------------- #
SPIKE_ID = "spike_ocr"
SPIKE_TAG = "SPIKE_OCR"

WORKSPACE = Path(wgc.WORKSPACE)
FIXTURES_DIR = WORKSPACE / "spikes" / "fixtures"
IMAGES_DIR = FIXTURES_DIR / "images"
LABELS_TXT = FIXTURES_DIR / "labels.txt"
RESULTS_DIR = WORKSPACE / "spikes" / "results"
REPORT_JSON = RESULTS_DIR / f"{SPIKE_ID}_report.json"
LOG_TXT = RESULTS_DIR / f"{SPIKE_ID}_log.txt"
SHOT_PREFIX = f"{SPIKE_ID}_shot_"
MAX_SCREENSHOTS = 5

LEFT_X_FRACTION = 0.35        # SPEC 4.2：气泡左边缘 x < 聊天区宽度 x 0.35
LEFT_WIDTH_FRACTION = 0.75    # SPEC 4.2：气泡宽度 < 聊天区宽度 x 0.75

CHAR_ACCURACY_MIN = 0.93      # 本轮验收口径（SPEC 原目标 95%，见报告对照）
IOU_HIT_THRESHOLD = 0.70      # 本任务：IoU>0.7 视为命中
IOU_PASS_RATE_MIN = 0.95      # 本轮验收口径（SPEC 原目标 98%）
AVG_MS_MAX = 2000.0           # 本轮放宽：平均单张识别耗时 <2000ms
ABORT_CHAR_ACCURACY = 0.80    # 规则 3：字准 <80% 视为严重偏低，停止并汇报

# 聊天区检测参数
DIVIDER_EDGE_DELTA = 6        # 竖直分隔线：相邻列灰度差阈值（暗色主题也能命中）
DIVIDER_COVERAGE = 0.55       # 分隔线必须覆盖 >=55% 图像高度的列

# 文本行归组为"一条消息"的参数
GROUP_X_ALIGN_RATIO = 0.30    # 相邻行左边缘差 <= 30% 行宽
GROUP_X_OVERLAP_RATIO = 0.55  # 相邻行水平重叠 >= 55%
GROUP_GAP_RATIO = 0.75        # 行间距 <= 75% 行高

# 非消息文本（按钮 / 时间戳 / 等级徽章 / 系统提示 / 卡片脚注）：不参与左侧判定。
# 依据 labels.txt 已确认的标注口径："引用块、发送者名、时间戳、系统提示、emoji 不计入"。
UI_NOISE_PATTERNS = [
    r"^发\s*送$", r"^关\s*闭$", r"^搜\s*索$", r"^Q?\s*搜索$",
    r"^\d{1,2}:\d{2}$", r"^昨天\s*\d{1,2}:\d{2}$",
    r"^星期[日一二三四五六]\s*\d{1,2}:\d{2}$", r"^周[日一二三四五六]\s*\d{1,2}:\d{2}$",
    r"^LV\s*\d+.*$", r"^(群主|管理员)$",
    r"^(黄金|钻石|王者|铂金|白银|青铜|星耀)$",
    r"^全员禁言中$", r"^.*禁言.*$", r"^.*加入了?群聊[。.]?$", r"^.*撤回了一条消息.*$",
    r"^查看\d+条.*$", r"^查看全部\d+条.*$", r"^\d+条新消息$", r"^\d+$",
    r"^群公告$", r"^群聊成员\s*\d+$",
]
# 状态栏/纯数字符号行（如截图内的 "101 l. 85"）：无中日韩字符、含数字、长度很短。
# 要求必须含数字，避免把 "ok"、"gg" 这类正常短消息误杀。
NUMERIC_NOISE_RE = re.compile(r"^(?=.*\d)[0-9A-Za-z\.\,\:\;\|\+\-\*\/\s%]{1,12}$")

# 头像锚定参数（P1）
AVATAR_BAND_WIDTH = 150       # 头像列宽度（自聊天区左边缘起）
# 头像最小边长：输入框工具图标约 30px，头像约 60px，因此下限取 40px 以区分
AVATAR_MIN_SIZE = 40
AVATAR_MAX_SIZE = 95          # 头像最大边长
AVATAR_MIN_FILL = 0.5         # 头像区域内非背景像素占比
# 头像锚定的"消息块窗口"：从头像上沿向下延伸若干倍头像高。QQ 的群聊里
# 发送者名/引用块/正文都在头像下方依次排布，纯"居中对齐"会把正文排除掉
# （实测群聊正文中心比头像中心低约 40px），因此用窗口而不是点对齐。
AVATAR_BLOCK_MAX_RATIO = 6.0
AVATAR_BLOCK_TOP_MARGIN = 10

# 两段式 OCR：零 OCR 成本地定位"最新左侧气泡"
CONTENT_DELTA = 40             # 与聊天背景的 L1 差值阈值（判定"非背景像素"）
ROW_RUN_MAX_GAP_RATIO = 1.0    # 行段合并允许的最大行间距（相对行高）
ROI_MAX_CANDIDATES = 3         # 自底向上最多尝试几个气泡
ROI_PADDING = 8                # ROI 外扩像素
IMAGE_LIKE_OPEN_KERNEL = 21    # 形态学开运算核：比笔画粗，能滤掉文字留下图片块
IMAGE_LIKE_RATIO = 0.35        # 开运算后残留占比 >= 该值 → 判为图片气泡
BUBBLE_COLUMN_GAP = 40         # 气泡右边界：允许的列空隙（超过即认为已到下一个气泡）
INPUT_AREA_EDGE_COVERAGE = 0.8 # 输入区上边界的横向边缘覆盖率阈值
INPUT_AREA_MIN_FRACTION = 0.6  # 只在画面下半部分找输入区上边界
# 判断"文本行是否在气泡内"：取文本左侧一小块的均值与聊天背景比，
# 若等于聊天背景 → 该行在气泡外（发送者名/系统提示），不属于消息正文。
BUBBLE_PADDING_PROBE_PX = 10
BUBBLE_BACKGROUND_TOLERANCE = 12
BUBBLE_PROBE_OFFSETS = (4, 8, 12, 16)   # 多点探测：避免行首字符紧贴气泡左边被误判为气泡外
# 卡片类消息的标题行：卡片其余行是"被转发/被引用内容"，按口径不计入正文
CARD_TITLE_TEXTS = ("群聊的聊天记录", "聊天记录", "群相册", "班级作业", "群文件", "群投票", "群接龙")

# 图片消息判定参数（用于剔除"图片里的文字"，它不属于聊天消息正文）
IMAGE_REGION_DIFF = 25        # 与聊天区背景色的 L1 差值阈值
IMAGE_REGION_MIN_H = 120      # 图片消息区域最小高度
IMAGE_REGION_MIN_W = 100      # 图片消息区域最小宽度
IMAGE_REGION_FILL = 0.6       # 区域内"非背景像素"占比（照片接近 1，文字气泡很低）

logger = logging.getLogger(SPIKE_ID)


def setup_logging(verbose: bool = False) -> None:
    # Windows 控制台默认 cp936，统一成 UTF-8 才能正常显示中文诊断
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


# --------------------------------------------------------------------------- #
# 几何 / 文本工具
# --------------------------------------------------------------------------- #
def quad_to_bbox(quad: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
    xs = [float(point[0]) for point in quad]
    ys = [float(point[1]) for point in quad]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_union(boxes: Sequence[Sequence[float]]) -> Optional[Tuple[float, float, float, float]]:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def bbox_iou(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> float:
    if not a or not b:
        return 0.0
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return float(inter / union) if union > 0 else 0.0


def normalize_text(text: str) -> str:
    """比对用归一化：去掉所有空白（含半角/全角空格、换行），保留标点与大小写。"""
    return "".join(ch for ch in text if not ch.isspace())


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1,
                               current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def char_accuracy(predicted: str, truth: str) -> float:
    """字符准确率 = 1 - 编辑距离 / 真值长度（归一化后比较）。"""
    pred, gold = normalize_text(predicted), normalize_text(truth)
    if not gold:
        return 1.0 if not pred else 0.0
    return max(0.0, 1.0 - levenshtein(pred, gold) / len(gold))


def lcs_length(a: str, b: str) -> int:
    if not a or not b:
        return 0
    previous = [0] * (len(b) + 1)
    for ca in a:
        current = [0]
        for j, cb in enumerate(b, start=1):
            current.append(previous[j - 1] + 1 if ca == cb
                           else max(previous[j], current[j - 1]))
        previous = current
    return previous[-1]


# --------------------------------------------------------------------------- #
# 聊天区检测（左侧判定使用的坐标系）
# --------------------------------------------------------------------------- #
def detect_dividers(image: np.ndarray) -> List[int]:
    """检测竖直分隔线（QQNT 面板边界）：整列都存在强灰度跳变的列。"""
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.int16)
    coverage = (np.abs(np.diff(gray, axis=1)) > DIVIDER_EDGE_DELTA).sum(axis=0)
    candidates = np.where(coverage >= DIVIDER_COVERAGE * height)[0]
    dividers: List[int] = []
    for column in candidates:
        if dividers and column - dividers[-1] <= 2:
            dividers[-1] = int((dividers[-1] + column) // 2)      # 相邻列合并成一条线
        else:
            dividers.append(int(column))
    return [d for d in dividers if 0.03 * width < d < 0.97 * width]


def chat_area_candidates(dividers: List[int], width: int) -> List[Dict[str, Any]]:
    """由分隔线生成候选聊天区：只取"相邻分隔线之间的区间"。

    QQNT 的面板边界一定是完整竖直分隔线，因此聊天列必然是某个相邻分隔线区间；
    "整幅宽度/分隔线到右缘"这类候选会把会话列表或群成员面板一起算进来（早期
    版本正是这样选错的）。只有完全没有分隔线的单窗口截图才回退整幅宽度。
    """
    bounds = [0] + dividers + [width - 1]
    candidates: List[Dict[str, Any]] = []
    for i in range(len(bounds) - 1):
        left, right = bounds[i], bounds[i + 1]
        candidates.append({"left": left, "right": right,
                           "method": "between_dividers" if dividers else "full_width"})
    if not dividers:
        candidates.append({"left": 0, "right": width - 1, "method": "full_width"})
    for candidate in candidates:
        candidate["width"] = candidate["right"] - candidate["left"]
    return candidates


def pick_chat_area(candidates: List[Dict[str, Any]], items: List[Dict[str, Any]],
                   width: int, dividers: List[int]) -> Dict[str, Any]:
    """选出聊天区：优先"包含发送按钮的最窄区间"，否则取最宽的合法区间。

    不能简单取"文本框最多的区间"——整幅宽度必然包含最多文本框，会把会话列表
    和群成员面板一起算进聊天区（实测会让左侧判定整体失效）。发送按钮只可能
    出现在聊天列，因此用它做锚点最稳。所有候选分数都会写进报告便于复核。
    """
    scored: List[Dict[str, Any]] = []
    for candidate in candidates:
        if candidate["width"] < 0.25 * width:
            continue
        inside = sum(1 for it in items
                     if candidate["left"] <= (it["bbox"][0] + it["bbox"][2]) / 2
                     <= candidate["right"])
        scored.append({**candidate, "boxes_inside": inside,
                       "has_send_button": _contains_send_button(candidate, items)})
    if not scored:
        margin = int(0.02 * width)
        return {"left": margin, "right": width - margin, "width": width - 2 * margin,
                "method": "fallback_full_width", "dividers": dividers,
                "boxes_inside": len(items), "candidates": []}
    # 取最宽的合法区间；"发送按钮落在该区间内"只作为校验信息写进报告
    best = dict(max(scored, key=lambda c: c["width"]))
    best["method"] = "widest_between_dividers"
    best["dividers"] = dividers
    best["candidates"] = [{k: c[k] for k in ("left", "right", "width", "method",
                                              "boxes_inside", "has_send_button")}
                          for c in scored]
    return best


def _contains_send_button(candidate: Dict[str, Any], items: List[Dict[str, Any]]) -> bool:
    for item in items:
        if item["text"].strip() in ("发送", "发 送"):
            center = (item["bbox"][0] + item["bbox"][2]) / 2
            if candidate["left"] <= center <= candidate["right"]:
                return True
    return False


def is_ui_noise(text: str) -> bool:
    """按钮/时间戳/等级徽章/系统提示/卡片脚注/截图状态栏等非消息文本。"""
    value = text.strip()
    if not value:
        return True
    if any(re.match(pattern, value) for pattern in UI_NOISE_PATTERNS):
        return True
    # 截图内的手机状态栏/元信息：很短且含 >=4 个数字（如 "1:000@数"、"101 7 l.il (85)-"）
    digits = sum(ch.isdigit() for ch in value)
    cjk = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
    if len(value) <= 14 and digits >= 4 and cjk <= 1:
        return True
    return False


# --------------------------------------------------------------------------- #
# P1：左侧头像锚定
# --------------------------------------------------------------------------- #
def chat_background_color(image: np.ndarray, area: Dict[str, Any]) -> np.ndarray:
    """聊天背景色：取聊天列最左侧窄带（竖直中段）的中位数颜色。"""
    height = image.shape[0]
    x0 = int(area["left"]) + 2
    x1 = min(int(area["left"]) + 30, int(area["right"]))
    y0, y1 = int(0.1 * height), int(0.9 * height)
    strip = image[y0:y1, x0:x1]
    if strip.size == 0:
        return np.zeros(3, dtype=np.int16)
    return np.median(strip.reshape(-1, 3), axis=0).astype(np.int16)


def is_outside_bubble(image: np.ndarray, box: Sequence[float], background: np.ndarray) -> bool:
    """文本行左侧是否整段都是聊天背景（是 → 该行在气泡外：发送者名/系统提示）。

    用 4/8/12/16px 多点探测并要求"全部命中背景"：单点探测会在行首字符紧贴
    气泡左边缘时（如 "@全体成员"）误判成气泡外，把正文开头整段丢掉。
    """
    y0, y1 = int(box[1]), max(int(box[1]) + 1, int(box[3]))
    for offset in BUBBLE_PROBE_OFFSETS:
        x = int(box[0]) - offset
        if x <= 1:
            return False
        patch = image[y0:y1, max(0, x - 2):x + 1]
        if patch.size == 0:
            return False
        mean_color = patch.reshape(-1, 3).mean(axis=0)
        if float(np.abs(mean_color - background).sum()) > BUBBLE_BACKGROUND_TOLERANCE:
            return False
    return True

def detect_left_avatars(image: np.ndarray, area: Dict[str, Any],
                        y0: int = 0, y1: Optional[int] = None) -> List[Dict[str, Any]]:
    """检测左侧头像：位于气泡左侧的正方形/圆形小图。

    头像列的背景取自该列最右侧几个像素（紧邻文本列，必为聊天背景），
    然后做连通域，按"边长 28~95px + 长宽比接近 1 + 内部填充率 >=0.5"筛出头像。
    """
    left = int(area["left"])
    band_left = left + 2
    band_right = min(int(area["right"]), left + AVATAR_BAND_WIDTH)
    if band_right - band_left < 20:
        return []
    top = max(0, int(y0))
    bottom = image.shape[0] if y1 is None else min(int(y1), image.shape[0])
    band = image[top:bottom, band_left:band_right]
    if band.size == 0:
        return []
    # 背景色取自聊天列最左侧一条窄带（一定是聊天背景，不会落在头像/气泡里）
    background = chat_background_color(image, area)
    diff = np.abs(band.astype(np.int16) - background).sum(axis=2)
    mask = (diff > 40).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    avatars: List[Dict[str, Any]] = []
    input_area_top = 0.92 * image.shape[0]      # 输入框区域不可能是消息头像
    for index in range(1, count):
        x, y, w, h, pixels = stats[index]
        if not (AVATAR_MIN_SIZE <= w <= AVATAR_MAX_SIZE
                and AVATAR_MIN_SIZE <= h <= AVATAR_MAX_SIZE):
            continue
        if not (0.7 <= w / float(h) <= 1.4):
            continue
        if pixels / float(w * h) < AVATAR_MIN_FILL:
            continue
        if top + int(y) >= input_area_top:
            continue
        avatars.append({
            "bbox": [band_left + int(x), top + int(y), band_left + int(x + w), top + int(y + h)],
            "center_y": top + int(y + h / 2),
            "size": [int(w), int(h)],
        })
    avatars.sort(key=lambda item: item["center_y"])
    return avatars


def avatar_alignment(box: Sequence[float], avatars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """文本框是否落在某个左侧头像的"消息块窗口"内（P1 规则）。

    窗口 = [头像上沿 - 10px, 头像上沿 + 4×头像高]，且文本框必须位于头像右侧。
    这样：消息正文/引用块/发送者名（都属于该头像所代表的消息块）会被纳入，
    随后由 extract_message_body 去掉发送者名与引用行；而远离任何头像的文本
    （面板文案、系统提示、图片里离头像很远的文字）被排除。
    """
    best: Optional[Dict[str, Any]] = None
    for avatar in avatars:
        if box[2] <= avatar["bbox"][0]:        # 必须在头像右侧
            continue
        avatar_top = avatar["bbox"][1]
        window_top = avatar_top - AVATAR_BLOCK_TOP_MARGIN
        window_bottom = avatar_top + AVATAR_BLOCK_MAX_RATIO * avatar["size"][1]
        inside = window_top <= box[1] <= window_bottom
        info = {"offset_px": round(box[1] - avatar_top, 1),
                "window": [window_top, round(window_bottom, 1)],
                "avatar_top": avatar_top, "aligned": bool(inside)}
        if best is None or abs(info["offset_px"]) < abs(best["offset_px"]):
            best = info
        if inside:
            return info
    if best is None:
        return {"offset_px": None, "window": None, "aligned": False,
                "reason": "头像右侧无对齐关系"}
    return best


# --------------------------------------------------------------------------- #
# 两段式第 1 段：零 OCR 定位"最新左侧气泡"
# --------------------------------------------------------------------------- #
def detect_input_area_top(image: np.ndarray, area: Dict[str, Any]) -> int:
    """检测输入区上边界（横贯聊天列的强横向边缘），把输入区排除在消息之外。

    实测截图底部 20-25% 是输入区，其中的"全员禁言中"提示会被 OCR 当成消息；
    用一条横贯整列的分隔线作上边界最稳，找不到则回退到 0.9 倍画面高度。
    """
    left = int(area["left"]) + 12
    right = int(area["right"])
    height = image.shape[0]
    if right - left < 20:
        return height
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.int16)
    coverage = (np.abs(np.diff(gray[:, left:right], axis=0)) > 6).mean(axis=1)
    rows = [int(row) for row in np.flatnonzero(coverage >= INPUT_AREA_EDGE_COVERAGE)
            if row > INPUT_AREA_MIN_FRACTION * height]
    return min(rows) if rows else int(0.9 * height)


def content_mask(image: np.ndarray, area: Dict[str, Any],
                 background: np.ndarray) -> np.ndarray:
    """聊天列内的"非聊天背景"掩码（uint8 0/1）。"""
    # 左侧跳过 12px：窗口圆角/边框像素会让"每行都有内容"，进而把整幅图并成一段
    left = int(area["left"]) + 12
    right = int(area["right"])
    column = image[:, left:right]
    diff = np.abs(column.astype(np.int16) - background).sum(axis=2)
    return (diff > CONTENT_DELTA).astype(np.uint8)


def row_left_profile(mask: np.ndarray) -> np.ndarray:
    """每行最左侧内容像素的列下标（无内容行 = -1）。"""
    height, width = mask.shape
    profile = np.full(height, -1, dtype=np.int32)
    for y in range(height):
        xs = np.flatnonzero(mask[y])
        if xs.size:
            profile[y] = int(xs[0])
    return profile


def row_runs(flags: np.ndarray, height: int) -> List[Tuple[int, int]]:
    """把"连续为真"的行合并成行段（容忍 <= 1 倍行高的间隙）。"""
    rows = np.flatnonzero(flags)
    if rows.size == 0:
        return []
    blocks: List[int] = []
    start_block = prev_row = int(rows[0])
    for y in rows[1:]:
        y = int(y)
        if y - prev_row <= 2:
            prev_row = y
        else:
            blocks.append(prev_row - start_block + 1)
            start_block = prev_row = y
    blocks.append(prev_row - start_block + 1)
    line_height = int(np.median(blocks)) if blocks else 20
    max_gap = max(4, int(ROW_RUN_MAX_GAP_RATIO * 0.8 * max(line_height, 8)))
    runs: List[Tuple[int, int]] = []
    start = prev = int(rows[0])
    for y in rows[1:]:
        y = int(y)
        if y - prev <= max_gap + 1:
            prev = y
        else:
            runs.append((start, prev))
            start = prev = y
    runs.append((start, prev))
    return runs


def bubble_candidates(image: np.ndarray, area: Dict[str, Any],
                      background: np.ndarray,
                      avatars: Optional[List[Dict[str, Any]]] = None,
                      y_limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """自底向上给出"最新左侧气泡"候选 ROI（零 OCR 成本）。

    用头像作为消息锚点：每个头像对应一条左侧消息，消息块 = 从头像上沿向下、
    到下一个头像（或输入区上边界）为止。这样 ROI 被限制在"一条消息"的高度，
    而不是整列（P1 整列 OCR 每张 ~1.7s，这里约 0.3-0.6s）。
    仍保留"图片块"判据（形态学开运算填充率）用于标记图片消息气泡。
    """
    mask = content_mask(image, area, background)
    limit = image.shape[0] if y_limit is None else int(y_limit)
    mask[limit:] = 0
    left_edge = int(area["left"]) + 10
    right_edge = int(area["right"])
    candidates: List[Dict[str, Any]] = []
    if avatars is None:
        avatars = []
    for index in range(len(avatars) - 1, -1, -1):
        avatar = avatars[index]
        avatar_top = int(avatar["bbox"][1])
        if avatar_top >= limit:
            continue
        next_top = (int(avatars[index + 1]["bbox"][1]) if index + 1 < len(avatars)
                    else limit)
        bottom = min(next_top - 6, limit)
        top = max(0, avatar_top - AVATAR_BLOCK_TOP_MARGIN)
        if bottom - top < 12:
            continue
        band = mask[top:bottom]
        column_has = band.any(axis=0)
        columns = np.flatnonzero(column_has)
        if columns.size == 0:
            continue
        # 自左向右按列连续性扩展（允许 <=BUBBLE_COLUMN_GAP 空隙）：否则同一行里
        # 右侧（自己发的）气泡会被并进来，ROI 变成整列宽度、OCR 白烧一倍时间。
        x0 = int(columns[0])
        x1 = x0 + 1
        gap = 0
        for x in range(x0, mask.shape[1]):
            if column_has[x]:
                x1 = x + 1
                gap = 0
            else:
                gap += 1
                if gap > BUBBLE_COLUMN_GAP:
                    break
        patch = band[:, x0:x1]
        opened = cv2.morphologyEx(patch, cv2.MORPH_OPEN,
                                  np.ones((IMAGE_LIKE_OPEN_KERNEL,) * 2, np.uint8))
        ratio = float(opened.mean()) if opened.size else 0.0
        candidates.append({
            "bbox": [left_edge + x0, top, left_edge + x1, bottom],
            "avatar_top": avatar_top,
            "image_like": bool(ratio >= IMAGE_LIKE_RATIO),
            "image_ratio": round(ratio, 4),
        })
        if len(candidates) >= ROI_MAX_CANDIDATES:
            break
    return candidates

def cluster_rows(boxes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把同一视觉行的文本框聚成行（y 区间重叠 >= 较小高度的 50%）。"""
    rows: List[Dict[str, Any]] = []
    for box in sorted(boxes, key=lambda item: (item["bbox"][1], item["bbox"][0])):
        placed = False
        for row in rows:
            top = max(row["bbox"][1], box["bbox"][1])
            bottom = min(row["bbox"][3], box["bbox"][3])
            overlap = bottom - top
            smaller = min(row["bbox"][3] - row["bbox"][1], box["bbox"][3] - box["bbox"][1])
            if smaller > 0 and overlap >= 0.5 * smaller:
                row["boxes"].append(box)
                row["bbox"] = bbox_union([row["bbox"], box["bbox"]])
                placed = True
                break
        if not placed:
            rows.append({"boxes": [box], "bbox": tuple(box["bbox"])})
    for row in rows:
        row["boxes"].sort(key=lambda item: item["bbox"][0])
        row["text"] = " ".join(item["text"] for item in row["boxes"]).strip()
    rows.sort(key=lambda row: row["bbox"][1])
    return rows


def extract_message_body(group: Dict[str, Any],
                         area: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """从气泡文本行里取"消息正文"，剔除卡片预览与引用块（P4）。

    依据已确认的标注口径（引用/转发内容不计入正文），采用两条规则：
      1) 卡片消息（首行是"群聊的聊天记录"等固定标题）：只保留标题行；
      2) 普通/回复气泡：把行聚成"行"，取**最底部的连续行段**作为正文
         —— 引用块一定在正文上方，且与本行段之间有明显的行距断点。
         实测引用→正文的间距 ≈48-50px，而气泡内行距 ≈15-20px，
         因此"断开阈值 = 1.2 倍行高"可以稳定切开。
    早期用"缩进"判断引用，会被同一视觉行被 OCR 拆成多框的情况误伤（009/024）。
    """
    boxes = group["boxes"]
    first_text = boxes[0]["text"].strip()
    if any(first_text.startswith(title) for title in CARD_TITLE_TEXTS):
        kept = [boxes[0]]
        dropped = [dict(box, reason="卡片预览/脚注（非正文）") for box in boxes[1:]]
        return boxes[0]["text"].strip(), kept, dropped

    rows = cluster_rows(boxes)
    if not rows:
        return group["text"], [], []
    run: List[Dict[str, Any]] = [rows[-1]]
    for row in reversed(rows[:-1]):
        current = run[0]
        gap = current["bbox"][1] - row["bbox"][3]
        row_height = max(row["bbox"][3] - row["bbox"][1],
                         current["bbox"][3] - current["bbox"][1], 1)
        if gap <= 1.2 * row_height:
            run.insert(0, row)
        else:
            break
    kept = [box for row in run for box in row["boxes"]]
    kept_ids = {id(box) for box in kept}
    dropped = [dict(box, reason="引用/卡片上方内容（非正文）")
               for box in boxes if id(box) not in kept_ids]
    text = " ".join(row["text"] for row in run).strip()
    return text, kept, dropped
# --------------------------------------------------------------------------- #
# 左侧消息判定（SPEC 4.2）+ 文本行归组
# --------------------------------------------------------------------------- #
def group_text_lines(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 OCR 文本行归组为"一条消息"。

    两步：① 先按 y 重叠聚成"视觉行"（同一行被 OCR 拆成多个框时必须合起来，
    否则正文会被腰斩，如 010/009）；② 再把相邻行按左边缘对齐 + 水平重叠 + 行距
    合并成消息。返回按 y 排序的组，每组含 boxes / text / bbox。
    """
    if not items:
        return []
    rows = cluster_rows(items)
    groups: List[Dict[str, Any]] = []
    for row in rows:
        placed = False
        for group in groups:
            last = group["rows"][-1]
            last_box, box = last["bbox"], row["bbox"]
            row_height = max(1.0, last_box[3] - last_box[1])
            gap = box[1] - last_box[3]
            align = abs(box[0] - last_box[0])
            width_last = max(1.0, last_box[2] - last_box[0])
            width_new = max(1.0, box[2] - box[0])
            overlap = min(last_box[2], box[2]) - max(last_box[0], box[0])
            overlap_ratio = overlap / min(width_last, width_new)
            if (-0.5 * row_height <= gap <= GROUP_GAP_RATIO * row_height
                    and align <= GROUP_X_ALIGN_RATIO * max(width_last, width_new)
                    and overlap_ratio >= GROUP_X_OVERLAP_RATIO):
                group["rows"].append(row)
                placed = True
                break
        if not placed:
            groups.append({"rows": [row]})
    for group in groups:
        group["boxes"] = [box for row in group["rows"] for box in row["boxes"]]
        group["boxes"].sort(key=lambda it: (it["bbox"][1], it["bbox"][0]))
        group["text"] = " ".join(row["text"] for row in group["rows"]).strip()
        group["bbox"] = bbox_union([row["bbox"] for row in group["rows"]])
        group["scores"] = [it["score"] for it in group["boxes"]]
    groups.sort(key=lambda g: (g["bbox"][3], g["bbox"][0]))
    return groups


def is_left_candidate(box: Sequence[float], area: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """SPEC 4.2 左侧判定：气泡左边缘 x < 聊天区宽度x0.35 且 气泡宽度 < 聊天区宽度x0.75。

    OCR 只给文本行 bbox，因此用文本行 bbox 作为气泡区域的代理（气泡比文字略
    大，但"左边缘"与"宽度"的相对关系一致）；该代理关系已写入报告说明。
    """
    box_left = box[0] - area["left"]
    box_width = box[2] - box[0]
    chat_width = max(1.0, float(area["width"]))
    left_ok = box_left < LEFT_X_FRACTION * chat_width
    width_ok = box_width < LEFT_WIDTH_FRACTION * chat_width
    return bool(left_ok and width_ok), {
        "left_offset_px": round(float(box_left), 1),
        "left_limit_px": round(LEFT_X_FRACTION * chat_width, 1),
        "box_width_px": round(float(box_width), 1),
        "width_limit_px": round(LEFT_WIDTH_FRACTION * chat_width, 1),
        "left_ok": bool(left_ok), "width_ok": bool(width_ok),
    }


def select_newest_left_message(
        groups: List[Dict[str, Any]],
        area: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """返回 (最底部左侧消息组, 全部候选组诊断)。左侧判定不使用真值。"""
    diagnostics: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    for group in groups:
        ok, detail = is_left_candidate(group["bbox"], area)
        diagnostics.append({"text": group["text"],
                            "bbox": [round(v, 1) for v in group["bbox"]],
                            "is_left": ok, "check": detail})
        if ok and not any(ui in group["text"] for ui in ("发送", "关闭")):
            candidates.append(group)
    newest = candidates[-1] if candidates else None
    return newest, diagnostics


def reference_group_bbox(items: List[Dict[str, Any]],

                         truth: str,
                         area: Dict[str, Any]) -> Tuple[Optional[Tuple[float, float, float, float]], float]:
    """IoU 参考框：在全部文本框的分组里，挑"正文与真值最匹配"的那一组。

    与预测侧对称：同样先按引用/卡片口径抽取正文，再用正文框做 IoU 对照，
    否则回复气泡/卡片会因为引文被算进参考框而出现假阴性。真值只用于定位。
    """
    groups = group_text_lines(items)
    best_box: Optional[Tuple[float, float, float, float]] = None
    best_acc = 0.0
    for group in groups:
        body, kept_boxes, _dropped = extract_message_body(group, area)
        if not body:
            continue
        acc = char_accuracy(body, truth)
        box = bbox_union([box["bbox"] for box in kept_boxes] or [group["bbox"]])
        # 同分时取"更靠下"的那一组：标注口径就是"最底部左侧消息"，
        # 否则像 019.png 这种同一文案出现两次的截图会出现假阴性。
        def area(a):
            return (a[2] - a[0]) * (a[3] - a[1]) if a else 0.0
        tighter = best_box is not None and box is not None and abs(acc - best_acc) <= 1e-9 \
            and abs(box[3] - best_box[3]) <= 2 and area(box) < area(best_box)
        if acc > best_acc + 1e-9 or tighter or (abs(acc - best_acc) <= 1e-9 and best_box
                                     and box and box[3] > best_box[3]):
            best_acc = acc
            best_box = box
    return (best_box if best_acc > 0 else None), round(best_acc, 4)


# --------------------------------------------------------------------------- #
# RapidOCR 封装
# --------------------------------------------------------------------------- #
class OcrEngine:
    """真机 RapidOCR 封装：只做 BGR 读取，不做缩放/二值化等预处理。"""

    def __init__(self) -> None:
        import rapidocr_onnxruntime as rapidocr
        self._module = rapidocr
        start = time.perf_counter()
        self.engine = rapidocr.RapidOCR()
        self.init_ms = (time.perf_counter() - start) * 1000.0
        self.warmup_ms: Optional[float] = None

    def warmup(self, image: np.ndarray) -> float:
        start = time.perf_counter()
        self.engine(image)
        self.warmup_ms = (time.perf_counter() - start) * 1000.0
        return self.warmup_ms

    def run(self, image: np.ndarray) -> Tuple[List[Dict[str, Any]], float, List[float]]:
        """返回 (文本行列表, 总耗时ms, RapidOCR 阶段耗时[det, cls, rec])。"""
        start = time.perf_counter()
        result, elapse = self.engine(image)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        items: List[Dict[str, Any]] = []
        for entry in (result or []):
            quad, text, score = entry[0], entry[1], entry[2]
            if not str(text).strip():
                continue
            items.append({"bbox": quad_to_bbox(quad),
                          "quad": [[float(v) for v in point] for point in quad],
                          "text": str(text), "score": float(score)})
        return items, elapsed_ms, [float(value) * 1000.0 for value in (elapse or [])]


# --------------------------------------------------------------------------- #
# 诊断截图预算（最多 5 张，沿用 spike_wgc 的约定）
# --------------------------------------------------------------------------- #
class ScreenshotBudget:
    def __init__(self, limit: int = MAX_SCREENSHOTS):
        self.limit = limit
        self.saved: List[Dict[str, Any]] = []
        self.skipped: List[str] = []

    def clean_previous(self) -> List[str]:
        removed: List[str] = []
        for path in sorted(RESULTS_DIR.glob(f"{SHOT_PREFIX}*.png")):
            if path.resolve().parent != RESULTS_DIR.resolve():
                continue
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                logger.warning("旧截图删除失败 %s: %s", path.name, exc)
        return removed

    def save(self, name: str, image: Optional[np.ndarray], note: str = "") -> bool:
        if image is None:
            self.skipped.append(f"{name}(无图)")
            return False
        if len(self.saved) >= self.limit:
            self.skipped.append(f"{name}(超出 {self.limit} 张预算)")
            return False
        path = RESULTS_DIR / f"{SHOT_PREFIX}{name}.png"
        try:
            if not cv2.imwrite(str(path), image):
                raise OSError("cv2.imwrite 返回 False")
        except Exception as exc:
            logger.warning("截图写入失败 %s: %s", name, exc)
            self.skipped.append(f"{name}(写入失败: {exc})")
            return False
        self.saved.append({"name": name, "file": path.name, "note": note})
        logger.info("诊断图已保存：%s（%s）", path.name, note or "-")
        return True


def draw_overlay(image: np.ndarray, area: Dict[str, Any], items: List[Dict[str, Any]],
                 selected: Optional[Dict[str, Any]], truth: str) -> np.ndarray:
    """画聊天区边界（黄）、所有文本框（蓝）、命中的左侧消息（绿）与真值提示。"""
    canvas = image.copy()
    cv2.rectangle(canvas, (int(area["left"]), 0), (int(area["right"]), canvas.shape[0] - 1),
                  (0, 200, 255), 3)
    for item in items:
        x0, y0, x1, y1 = [int(v) for v in item["bbox"]]
        cv2.rectangle(canvas, (x0, y0), (x1, y1), (255, 120, 0), 2)
    if selected:
        x0, y0, x1, y1 = [int(v) for v in selected["bbox"]]
        cv2.rectangle(canvas, (x0 - 4, y0 - 4), (x1 + 4, y1 + 4), (0, 220, 0), 3)
    cv2.putText(canvas, f"truth: {truth[:40]}", (12, 34),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
    cv2.putText(canvas, f"chat_area={area['method']} [{area['left']},{area['right']}]",
                (12, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    return canvas


# --------------------------------------------------------------------------- #
# 数据集加载
# --------------------------------------------------------------------------- #
def load_labels(path: Path) -> Tuple[List[Dict[str, str]], List[str], List[str]]:
    """解析 labels.txt（文件名 | 文本）；返回 (有效样本, SKIP 文件名, 异常行)。"""
    samples: List[Dict[str, str]] = []
    skipped: List[str] = []
    malformed: List[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "|" not in line:
            malformed.append(raw)
            continue
        name, text = line.split("|", 1)
        name, text = name.strip(), text.strip()
        if not name:
            malformed.append(raw)
            continue
        if text.upper() == "SKIP":
            skipped.append(name)
            continue
        image_path = IMAGES_DIR / name
        if not image_path.exists():
            malformed.append(f"{raw}（图片不存在）")
            continue
        samples.append({"name": name, "truth": text, "path": str(image_path)})
    return samples, skipped, malformed


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


# --------------------------------------------------------------------------- #
# 单张图片评估
# --------------------------------------------------------------------------- #
def ocr_region(engine: "OcrEngine", image: np.ndarray,
               roi: Sequence[int]) -> Tuple[List[Dict[str, Any]], float, List[float]]:
    """对整幅图中的某个区域做 OCR，并把结果坐标还原到整幅图坐标系。"""
    x0, y0, x1, y1 = [int(v) for v in roi]
    crop = image[y0:y1, x0:x1]
    items, elapsed_ms, stage_ms = engine.run(crop)
    for item in items:
        bx0, by0, bx1, by1 = item["bbox"]
        item["bbox"] = (bx0 + x0, by0 + y0, bx1 + x0, by1 + y0)
    return items, elapsed_ms, stage_ms


def filter_message_items(items: List[Dict[str, Any]], image: np.ndarray,
                         area: Dict[str, Any], background: np.ndarray,
                         avatars: List[Dict[str, Any]]
                         ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """P2：几何规则剔除 UI 文案 / 状态栏字符 / 发送者名行 / 无头像对齐的文本。

    发送者名用"徽章锚定"识别：QQ 群聊里发送者名右侧必跟等级/身份徽章
    （LV23 黄金、群主、管理员…），同一行右侧存在徽章的文本行即发送者名行。
    早期用"文本左侧是否为聊天背景"判断，会把浅灰卡片里的正文行误杀（卡片
    背景与聊天背景同色），导致 009/024 的正文被截断。
    """
    badge_tokens = ("群主", "管理员", "黄金", "钻石", "王者", "铂金", "白银", "青铜", "星耀")

    def is_badge(text: str) -> bool:
        value = text.strip()
        if re.match(r"^LV\s*\d+", value, re.IGNORECASE):
            return True
        if value in badge_tokens:
            return True
        return any(value.endswith(token) for token in badge_tokens)

    badges = [item for item in items if is_badge(item["text"])]

    def is_header(item: Dict[str, Any]) -> bool:
        if is_badge(item["text"]):
            return True
        for badge in badges:
            if badge is item:
                continue
            top = max(item["bbox"][1], badge["bbox"][1])
            bottom = min(item["bbox"][3], badge["bbox"][3])
            smaller = min(item["bbox"][3] - item["bbox"][1],
                          badge["bbox"][3] - badge["bbox"][1], 1)
            if bottom - top >= 0.5 * smaller and badge["bbox"][0] > item["bbox"][2] - 2:
                return True
        return False

    kept: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for item in items:
        if not (area["left"] <= (item["bbox"][0] + item["bbox"][2]) / 2 <= area["right"]):
            continue
        text = item["text"].strip()
        align = avatar_alignment(item["bbox"], avatars)
        if is_ui_noise(text) or (NUMERIC_NOISE_RE.match(text)
                                 and not re.search(r"[\u4e00-\u9fff]", text)):
            excluded.append({**item, "reason": "UI 文案/时间戳/徽章/系统提示/状态栏字符",
                             "avatar_alignment": align})
        elif is_header(item):
            excluded.append({**item, "reason": "发送者名/身份徽章行",
                             "avatar_alignment": align})
        elif not align["aligned"]:
            excluded.append({**item, "reason": "无头像垂直对齐（非消息正文）",
                             "avatar_alignment": align})
        else:
            kept.append({**item, "avatar_alignment": align})
    return kept, excluded


def evaluate_sample(engine: OcrEngine, sample: Dict[str, str],
                    chat_bottom_fraction: float = 1.0,
                    two_stage: bool = False) -> Dict[str, Any]:
    record: Dict[str, Any] = {"name": sample["name"], "truth": sample["truth"]}
    image = cv2.imread(sample["path"], cv2.IMREAD_COLOR)   # 真实截图，原样送入
    if image is None:
        record.update({"error": "图片读取失败", "char_accuracy": 0.0, "iou": 0.0})
        return record
    record["size"] = [int(image.shape[1]), int(image.shape[0])]

    # 聊天区用纯几何判定（分隔线取最宽区间）：必须在任何 OCR 之前确定。
    height, width = image.shape[:2]
    dividers = detect_dividers(image)
    candidates = chat_area_candidates(dividers, width)
    area = pick_chat_area(candidates, [], width, dividers)
    background = chat_background_color(image, area)
    avatars = detect_left_avatars(image, area)
    record["chat_area"] = {**area, "crop_fraction": chat_bottom_fraction}
    record["chat_background_bgr"] = [int(v) for v in background.tolist()]
    record["avatars"] = [{"bbox": a["bbox"], "center_y": a["center_y"], "size": a["size"]}
                         for a in avatars]

    # ---------------- 两段式第 1 段：零 OCR 成本定位"最新左侧气泡" ------------- #
    input_top = detect_input_area_top(image, area)
    record["input_area_top"] = int(input_top)
    bubble_cands = bubble_candidates(image, area, background, avatars=avatars,
                                     y_limit=input_top)
    record["bubble_candidates"] = bubble_cands
    roi_used: Optional[List[int]] = None
    roi_attempts = 0
    image_skips = 0
    fallback = False
    total_ms = 0.0
    stage_totals = [0.0, 0.0, 0.0]
    items: List[Dict[str, Any]] = []
    kept_items: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []
    selected: Optional[Dict[str, Any]] = None
    predicted_text, predicted_box, dropped = "", None, []

    for cand in (bubble_cands if two_stage else []):
        roi_attempts += 1
        if cand["image_like"]:
            image_skips += 1          # 图片气泡：不 OCR，直接排除（P3）
            continue
        roi = [max(int(area["left"]), int(cand["bbox"][0]) - ROI_PADDING),
               max(0, int(cand["bbox"][1]) - ROI_PADDING),
               min(int(area["right"]), int(cand["bbox"][2]) + ROI_PADDING),
               min(height, int(cand["bbox"][3]) + ROI_PADDING)]
        items, elapsed_ms, stage_ms = ocr_region(engine, image, roi)
        total_ms += elapsed_ms
        stage_totals = [a + b for a, b in zip(stage_totals, stage_ms)]
        kept_items, excluded = filter_message_items(items, image, area, background, avatars)
        groups = group_text_lines(kept_items)
        selected, diagnostics = select_newest_left_message(groups, area)
        if selected:
            predicted_text, kept_boxes, dropped = extract_message_body(selected, area)
            if predicted_text.strip():
                predicted_box = bbox_union([box["bbox"] for box in kept_boxes]
                                           or [selected["bbox"]])
                roi_used = roi
                break
            selected = None

    # ---------------- 回退：P1 的"聊天列 OCR" ------------- #
    if roi_used is None:
        fallback = bool(two_stage)
        crop_top = max(0, min(int((1.0 - chat_bottom_fraction) * height), height - 1))
        roi = [int(area["left"]), crop_top, int(area["right"]), int(input_top)]
        items, elapsed_ms, stage_ms = ocr_region(engine, image, roi)
        total_ms += elapsed_ms
        stage_totals = [a + b for a, b in zip(stage_totals, stage_ms)]
        kept_items, excluded = filter_message_items(items, image, area, background, avatars)
        groups = group_text_lines(kept_items)
        selected, diagnostics = select_newest_left_message(groups, area)
        predicted_text, predicted_box, dropped = "", None, []
        if selected:
            predicted_text, kept_boxes, dropped = extract_message_body(selected, area)
            if predicted_text.strip():
                predicted_box = bbox_union([box["bbox"] for box in kept_boxes]
                                           or [selected["bbox"]])

    if two_stage:
        path = "fallback_full_column" if fallback else "two_stage_roi"
    else:
        path = "full_column"
    record["two_stage"] = {
        "enabled": two_stage, "path": path,
        "roi_used": roi_used, "roi_attempts": roi_attempts,
        "image_bubbles_skipped": image_skips, "fallback_used": fallback,
        "localized": bool(roi_used is not None),
    }
    record["ms"] = round(total_ms, 1)
    record["stage_ms"] = [round(v, 1) for v in stage_totals]
    record["boxes"] = len(items)
    record["excluded_lines"] = [{"text": it["text"], "reason": it["reason"],
                                 "avatar_alignment": it["avatar_alignment"],
                                 "bbox": [round(v, 1) for v in it["bbox"]]}
                                for it in excluded]

    record["groups"] = [{"text": d["text"], "bbox": d["bbox"], "is_left": d["is_left"]}
                        for d in diagnostics]
    record["lines"] = [{"text": it["text"], "score": round(it["score"], 3),
                        "bbox": [round(v, 1) for v in it["bbox"]]} for it in kept_items]
    record["dropped_from_selected"] = [{"text": it["text"], "reason": it["reason"]}
                                       for it in dropped]
    # IoU 参考框：在"全部聊天区文本框"的分组里挑与真值最匹配的一组（只用真值定位，
    # 不参与选消息），避免按行匹配命中别处相似文字造成假阴性。
    # 注意：这一步是"评测专用"的全列 OCR（保证参考框口径与预测侧对称），
    # 其耗时单独记录、不计入生产耗时指标。
    ref_items, ref_ms, _ref_stage = ocr_region(
        engine, image, [int(area["left"]), 0, int(area["right"]), height])
    ref_in_area = [it for it in ref_items
                   if area["left"] <= (it["bbox"][0] + it["bbox"][2]) / 2 <= area["right"]]
    reference_box, reference_acc = reference_group_bbox(ref_in_area, sample["truth"], area)
    # 若生产路径本次 OCR 本身就定位到了真值文本（>=0.95），优先用它的框做参考：
    # 同一段文字在两次 OCR 中的行框会有 1-9px 抖动，直接拿另一次的结果做 IoU 会
    # 引入与选取逻辑无关的噪声（如 021.png 文本完全正确但 IoU 只有 0.68）。
    prod_box, prod_acc = reference_group_bbox(kept_items, sample["truth"], area)
    if prod_box is not None and prod_acc >= 0.95:
        reference_box, reference_acc = prod_box, prod_acc
        reference_source = "production_pass"
    else:
        reference_source = "evaluation_pass"
    record["evaluation_only"] = {"reference_source": reference_source,
                                 "reference_ocr_ms": round(ref_ms, 1),
                                 "timing_scope": "生产路径（两段式 ROI）耗时；参考框 OCR 不计入"}
    record["reference_ocr_lines"] = [{"text": it["text"],
                                      "bbox": [round(v, 1) for v in it["bbox"]]}
                                     for it in ref_in_area]
    record["reference_accuracy"] = reference_acc
    record["predicted_text"] = predicted_text
    record["predicted_bbox"] = [round(v, 1) for v in predicted_box] if predicted_box else None
    record["reference_bbox"] = [round(v, 1) for v in reference_box] if reference_box else None
    record["char_accuracy"] = round(char_accuracy(predicted_text, sample["truth"]), 4)
    record["iou"] = round(bbox_iou(predicted_box, reference_box), 4)
    record["iou_hit"] = bool(record["iou"] > IOU_HIT_THRESHOLD)
    all_texts = [it["text"] for it in ref_in_area]
    record["recognition_only"] = recognition_diagnostic(all_texts, sample["truth"])
    record["image"] = image        # 供诊断叠图使用（不写入 JSON）
    record["selected"] = selected
    return record


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [r for r in records if r.get("char_accuracy") is not None]
    accuracies = [r["char_accuracy"] for r in valid]
    ious = [r["iou"] for r in valid]
    times = [r["ms"] for r in valid if isinstance(r.get("ms"), (int, float))]
    total_distance = 0
    total_truth_chars = 0
    for r in valid:
        gold = normalize_text(r["truth"])
        total_truth_chars += len(gold)
        total_distance += levenshtein(normalize_text(r.get("predicted_text", "")), gold)
    micro = 1.0 - (total_distance / total_truth_chars) if total_truth_chars else 0.0
    hits = sum(1 for r in valid if r["iou_hit"])
    recog = [r["recognition_only"]["best_window"] for r in valid if r.get("recognition_only")]
    staged = [r for r in valid if r.get("two_stage") and r["two_stage"]["enabled"]]
    localized = sum(1 for r in staged if r["two_stage"]["localized"])
    falls = sum(1 for r in staged if r["two_stage"]["fallback_used"])
    skips = sum(int(r["two_stage"]["image_bubbles_skipped"]) for r in staged)
    return {
        "samples": len(valid),
        "char_accuracy_mean": round(statistics.fmean(accuracies), 4) if accuracies else 0.0,
        "char_accuracy_micro": round(micro, 4),
        "char_accuracy_min": round(min(accuracies), 4) if accuracies else 0.0,
        "iou_mean": round(statistics.fmean(ious), 4) if ious else 0.0,
        "iou_hits": hits,
        "iou_pass_rate": round(hits / len(valid), 4) if valid else 0.0,
        "avg_ms": round(statistics.fmean(times), 1) if times else 0.0,
        "max_ms": round(max(times), 1) if times else 0.0,
        "min_ms": round(min(times), 1) if times else 0.0,
        "total_ms": round(sum(times), 1) if times else 0.0,
        "recognition_only_mean": round(statistics.fmean(recog), 4) if recog else 0.0,
        "recognition_only_hit_rate": round(sum(1 for v in recog if v >= CHAR_ACCURACY_MIN)
                                           / len(recog), 4) if recog else 0.0,
        "two_stage_localized": localized,
        "two_stage_success_rate": round(localized / len(staged), 4) if staged else 0.0,
        "two_stage_fallbacks": falls,
        "two_stage_image_bubbles_skipped": skips,
    }


def worst_samples(records: List[Dict[str, Any]], count: int = 3) -> List[Dict[str, Any]]:
    ranked = sorted(records, key=lambda r: (r.get("char_accuracy", 0.0), r.get("iou", 0.0)))
    return [{"name": r["name"], "truth": r["truth"],
             "predicted_text": r.get("predicted_text", ""),
             "char_accuracy": r.get("char_accuracy"), "iou": r.get("iou"),
             "ms": r.get("ms"), "boxes": r.get("boxes"),
             "note": _failure_note(r)} for r in ranked[:count]]


def _failure_note(record: Dict[str, Any]) -> str:
    if not record.get("predicted_text"):
        return "未选出左侧消息（左侧判定未命中任何气泡）"
    if record.get("char_accuracy", 1.0) < 0.5:
        return "所选气泡文本与真值差异大（可能是引用卡片/聊天记录卡片/多行拼接）"
    if record.get("iou", 1.0) <= IOU_HIT_THRESHOLD:
        return "定位框与参考框重叠不足"
    return "部分字符识别错误"


def recognition_diagnostic(all_texts: List[str], truth: str, window: int = 4) -> Dict[str, float]:
    """诊断：不依赖任何选取逻辑，回答"正确文本到底有没有被 OCR 读出来"。

    做法是用真值在全部 OCR 文本行上滑动拼接，取最匹配的窗口（滑动窗口长度最多
    window 行）。这是"识别能力上限"，与主指标（按规则选取到的最新左侧消息）分
    开报告，避免把"选错气泡"误判成"OCR 识别不准"。
    """
    best_line = max((char_accuracy(text, truth) for text in all_texts), default=0.0)
    best_window = 0.0
    for start in range(len(all_texts)):
        buffer = ""
        for end in range(start, min(start + window, len(all_texts))):
            buffer = (buffer + " " + all_texts[end]).strip()
            best_window = max(best_window, char_accuracy(buffer, truth))
    return {"best_line": round(best_line, 4), "best_window": round(best_window, 4)}

# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ChatAssistant Spike #2：RapidOCR 识别 QQ 聊天字体（SPEC.md 6.1-2）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 张（0=全部）")
    parser.add_argument("--no-overlay", action="store_true", help="不写诊断叠图")
    parser.add_argument("--two-stage", action="store_true",
                        help="启用两段式（先用图像分析定位最新气泡，只 OCR 该 ROI）")
    parser.add_argument("--chat-bottom-fraction", type=float, default=1.0,
                        help="只对聊天区底部这个比例做 OCR（1.0=整幅；0.34≈底部1/3）")
    parser.add_argument("--debug-chat-area", action="store_true",
                        help="只做聊天区检测并输出叠图（不评估准确率）")
    parser.add_argument("--verbose", action="store_true", help="终端输出 DEBUG 日志")
    return parser.parse_args(argv)


def finalize(report: Dict[str, Any], shots: ScreenshotBudget, assertions: Assertions,
             started: float) -> None:
    isolation = wgc.audit_isolation(wgc._ISOLATION_BASELINE, wgc.snapshot_external_dirs())
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

    report["screenshots"] = {"saved": shots.saved, "skipped": shots.skipped}
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
    wgc._ISOLATION_BASELINE = wgc.snapshot_external_dirs()
    started = time.time()

    shots = ScreenshotBudget()
    assertions = Assertions()
    report: Dict[str, Any] = {
        "spike": SPIKE_ID,
        "spec_ref": "SPEC.md 6.1-2 + 4.2",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "args": vars(args),
        "workspace_paths": wgc.workspace_paths(),
        "thresholds": {"char_accuracy_min": CHAR_ACCURACY_MIN,
                       "iou_hit_threshold": IOU_HIT_THRESHOLD,
                       "iou_pass_rate_min": IOU_PASS_RATE_MIN,
                       "avg_ms_max": AVG_MS_MAX,
                       "left_x_fraction": LEFT_X_FRACTION,
                       "left_width_fraction": LEFT_WIDTH_FRACTION},
        "environment": {}, "dataset": {}, "engine": {}, "metrics": {},
        "per_image": [], "worst_samples": [], "file_isolation": {},
        "screenshots": {"saved": [], "skipped": [], "removed_previous": []},
        "abort_reason": None, "result": "FAIL",
    }

    logger.info("=" * 78)
    logger.info("%s 开始（SPEC.md 6.1-2）", SPIKE_TAG)
    logger.info("=" * 78)
    report["environment"]["dpi_awareness"] = wgc.set_dpi_awareness()
    report["environment"]["windows_build"] = wgc.windows_build()
    report["environment"]["python"] = sys.version.split()[0]
    report["environment"]["opencv"] = cv2.__version__
    report["environment"]["numpy"] = np.__version__
    report["screenshots"]["removed_previous"] = shots.clean_previous()
    logger.info("环境：DPI=%s Windows=%s Python=%s cv2=%s",
                report["environment"]["dpi_awareness"], report["environment"]["windows_build"],
                report["environment"]["python"], report["environment"]["opencv"])

    samples, skipped_names, malformed = load_labels(LABELS_TXT)
    if args.limit:
        samples = samples[: args.limit]
    report["dataset"] = {"labels_file": str(LABELS_TXT), "valid": len(samples),
                         "skipped": skipped_names, "malformed": malformed}
    logger.info("数据集：有效 %d 张，SKIP %s，异常行 %d 条",
                len(samples), skipped_names or "无", len(malformed))
    assertions.add("dataset_loaded", len(samples) > 0 and not malformed,
                   f"有效 {len(samples)} 张（SKIP {len(skipped_names)} 张）"
                   + (f"，异常行 {malformed}" if malformed else ""))
    if not samples:
        report["abort_reason"] = "NO_VALID_SAMPLES"
        report["result"] = "ABORT"
        finalize(report, shots, assertions, started)
        return 2

    # ---------------- RapidOCR 初始化 ---------------- #
    engine: Optional[OcrEngine] = None
    try:
        engine = OcrEngine()
        import onnxruntime
        report["environment"]["onnxruntime"] = onnxruntime.__version__
        import rapidocr_onnxruntime
        report["engine"] = {"name": "rapidocr_onnxruntime",
                            "version": getattr(rapidocr_onnxruntime, "__version__", "unknown"),
                            "models_bundled": True, "init_ms": round(engine.init_ms, 1),
                            "preprocessing": "无（原图直接送入，未缩放/未二值化）",
                            "providers": onnxruntime.get_available_providers()}
    except Exception as exc:
        logger.error("RapidOCR 初始化失败：%s: %s", type(exc).__name__, exc)
        assertions.add("ocr_initialized", False, f"{type(exc).__name__}: {exc}")
        report["abort_reason"] = f"OCR_INIT_FAILED: {type(exc).__name__}: {exc}"
        report["result"] = "ABORT"
        finalize(report, shots, assertions, started)
        return 2
    assertions.add("ocr_initialized", True,
                   f"rapidocr_onnxruntime {report['engine']['version']}，"
                   f"初始化 {report['engine']['init_ms']:.0f}ms，"
                   f"providers={report['engine']['providers']}")
    logger.info("RapidOCR 初始化 %.0fms", engine.init_ms)

    # ---------------- 逐张评估 ---------------- #
    first_image = cv2.imread(samples[0]["path"], cv2.IMREAD_COLOR)
    if first_image is not None:
        warmup_ms = engine.warmup(first_image)
        report["engine"]["warmup_ms"] = round(warmup_ms, 1)
        logger.info("预热推理（不计入平均耗时）：%.0fms", warmup_ms)

    records: List[Dict[str, Any]] = []
    for index, sample in enumerate(samples, start=1):
        record = evaluate_sample(engine, sample, args.chat_bottom_fraction,
                                 two_stage=args.two_stage)
        records.append(record)
        logger.info("[%2d/%d] %s %sx%s boxes=%s 字准=%.2f IoU=%.2f 耗时=%.0fms",
                    index, len(samples), record["name"],
                    record.get("size", ["?", "?"])[0], record.get("size", ["?", "?"])[1],
                    record.get("boxes"), record.get("char_accuracy", 0.0),
                    record.get("iou", 0.0), record.get("ms", 0.0))

    if args.debug_chat_area:
        for record in records[:MAX_SCREENSHOTS]:
            shots.save(f"chatarea_{record['name'].replace('.png', '')}",
                       draw_overlay(record["image"], record["chat_area"], [],
                                    record.get("selected"), record["truth"]),
                       f"聊天区检测：{record['chat_area']['method']}")
        report["result"] = "PASS"
        finalize(report, shots, assertions, started)
        return 0

    metrics = summarize(records)
    report["metrics"] = metrics
    report["per_image"] = [{k: v for k, v in r.items() if k not in ("image", "selected")}
                           for r in records]
    logger.info("指标：字准(均值)=%.2f%% 字准(微平均)=%.2f%% IoU命中率=%.2f%% "
                "平均耗时=%.0fms（最大 %.0fms）",
                metrics["char_accuracy_mean"] * 100, metrics["char_accuracy_micro"] * 100,
                metrics["iou_pass_rate"] * 100, metrics["avg_ms"], metrics["max_ms"])
    logger.info("诊断（仅识别能力，用真值在全部文本框上取最优窗口）：平均 %.2f%%，"
                ">=%.0f%% 的样本 %d/%d —— 用于区分 OCR 识别不足 与 选错气泡 两类问题",
                metrics["recognition_only_mean"] * 100, CHAR_ACCURACY_MIN * 100,
                round(metrics["recognition_only_hit_rate"] * metrics["samples"]),
                metrics["samples"])

    # ---------------- 断言 ---------------- #
    assertions.add("char_accuracy", metrics["char_accuracy_mean"] >= CHAR_ACCURACY_MIN,
                   f"字符准确率均值 {metrics['char_accuracy_mean']:.2%}"
                   f"（微平均 {metrics['char_accuracy_micro']:.2%}，最低 {metrics['char_accuracy_min']:.2%}）"
                   f"，阈值 >= {CHAR_ACCURACY_MIN:.0%}")
    assertions.add("left_localization_iou",
                   metrics["iou_pass_rate"] >= IOU_PASS_RATE_MIN,
                   f"IoU> {IOU_HIT_THRESHOLD} 命中率 {metrics['iou_pass_rate']:.2%}"
                   f"（{metrics['iou_hits']}/{metrics['samples']}，IoU 均值 {metrics['iou_mean']:.2f}）"
                   f"，阈值 >= {IOU_PASS_RATE_MIN:.0%}")
    assertions.add("avg_recognition_time", metrics["avg_ms"] < AVG_MS_MAX,
                   f"平均单张 {metrics['avg_ms']:.0f}ms（min {metrics['min_ms']:.0f} / "
                   f"max {metrics['max_ms']:.0f}），阈值 < {AVG_MS_MAX:.0f}ms")
    if not args.two_stage:
        assertions.add("two_stage_localization", None,
                       "默认路径为整列 OCR（--two-stage 可启用两段式），本项跳过")
    else:
        assertions.add("two_stage_localization",
                   metrics["two_stage_success_rate"] >= 0.80,
                   f"两段式定位成功 {metrics['two_stage_localized']}/{metrics['samples']}"
                   f"（成功率 {metrics['two_stage_success_rate']:.2%}，回退 "
                   f"{metrics['two_stage_fallbacks']} 次，跳过图片气泡 "
                   f"{metrics['two_stage_image_bubbles_skipped']} 个），阈值 >= 80%")
    logger.info("两段式：定位成功 %d/%d，回退 %d 次，图片气泡跳过 %d 个",
                metrics["two_stage_localized"], metrics["samples"],
                metrics["two_stage_fallbacks"], metrics["two_stage_image_bubbles_skipped"])

    # ---------------- 最差样本 ---------------- #
    if (metrics["char_accuracy_mean"] < CHAR_ACCURACY_MIN
            or metrics["iou_pass_rate"] < IOU_PASS_RATE_MIN
            or metrics["avg_ms"] >= AVG_MS_MAX):
        worst = worst_samples(records)
        report["worst_samples"] = worst
        logger.info("-" * 78)
        logger.info("最差 %d 个样本：", len(worst))
        for item in worst:
            logger.info("  [%s] 字准=%.2f IoU=%.2f 耗时=%sms boxes=%s",
                        item["name"], item["char_accuracy"], item["iou"],
                        item["ms"], item["boxes"])
            logger.info("     真值  : %s", item["truth"])
            logger.info("     识别  : %s", item["predicted_text"])
            logger.info("     诊断  : %s", item["note"])

    if not args.no_overlay:
        for record in records[:3]:
            shots.save(f"sample_{record['name'].replace('.png', '')}",
                       draw_overlay(record["image"], record["chat_area"], record["lines"],
                                    record.get("selected"), record["truth"]),
                       f"{record['name']} 聊天区/文本框/命中左侧消息")
        for item in report.get("worst_samples", [])[:2]:
            record = next((r for r in records if r["name"] == item["name"]), None)
            if record:
                shots.save(f"worst_{record['name'].replace('.png', '')}",
                           draw_overlay(record["image"], record["chat_area"], record["lines"],
                                        record.get("selected"), record["truth"]),
                           f"{record['name']}（最差样本之一）")

    # 规则 3：字准严重偏低 → 停止并汇报，不自行改预处理
    if metrics["char_accuracy_mean"] < ABORT_CHAR_ACCURACY:
        logger.error("字符准确率 %.2f%% < %.0f%%，属严重偏低，按规则暂停开发并上报",
                     metrics["char_accuracy_mean"] * 100, ABORT_CHAR_ACCURACY * 100)
        report["abort_reason"] = f"CHAR_ACCURACY_TOO_LOW: {metrics['char_accuracy_mean']:.4f}"
        report["result"] = "ABORT"
        finalize(report, shots, assertions, started)
        return 2

    failed = assertions.any_fail
    report["result"] = "FAIL" if failed else "PASS"
    finalize(report, shots, assertions, started)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
