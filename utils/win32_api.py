#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""utils/win32_api.py - Win32 API 封装（SPEC 5.7）

约定：
  - 所有窗口/屏幕坐标返回值均为**物理像素**，调用方必须先 set_dpi_awareness()（Per-Monitor-V2）；
  - 全部为只读查询，唯二会改变系统状态的是 clip_write_text / send_paste /
    bring_window_to_front —— 它们只服务于"一键填入"（SPEC 2.2(d) 的允许范围），
    绝不发送回车、绝不点击 QQ 的发送按钮。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

Rect = Tuple[int, int, int, int]
logger = logging.getLogger("utils.win32_api")

# --------------------------------------------------------------------------- #
# 显式声明 64 位敏感的调用签名（不声明时 ctypes 会把句柄截断成 32 位 —— 实测会让
# GlobalAlloc/GlobalLock 返回 0 并在写剪贴板时崩掉）
# --------------------------------------------------------------------------- #
user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.IsIconic.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.GetDpiForWindow.argtypes = [wt.HWND]
user32.GetDpiForWindow.restype = wt.UINT
user32.OpenClipboard.argtypes = [wt.HWND]
user32.EmptyClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wt.UINT
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wt.DWORD,
                                                ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]


# --------------------------------------------------------------------------- #
# DPI / 版本
# --------------------------------------------------------------------------- #
def set_dpi_awareness() -> str:
    """Per-Monitor-V2；拿不到物理像素的根因几乎都是这里没设。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


class _OSVERSIONINFOEXW(ctypes.Structure):
    _fields_ = [("dwOSVersionInfoSize", wt.DWORD), ("dwMajorVersion", wt.DWORD),
                ("dwMinorVersion", wt.DWORD), ("dwBuildNumber", wt.DWORD),
                ("dwPlatformId", wt.DWORD), ("szCSDVersion", wt.WCHAR * 128)]


def windows_build() -> str:
    info = _OSVERSIONINFOEXW()
    info.dwOSVersionInfoSize = ctypes.sizeof(info)
    try:
        ctypes.windll.ntdll.RtlGetVersion(ctypes.byref(info))
        return f"{info.dwMajorVersion}.{info.dwMinorVersion}.{info.dwBuildNumber}"
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- #
# 窗口枚举与属性（只读）
# --------------------------------------------------------------------------- #
_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def enum_top_level_windows() -> List[Tuple[int, int]]:
    """返回 [(hwnd, pid)]（顶层窗口，按 Z 序）。"""
    out: List[Tuple[int, int]] = []

    def _cb(hwnd, _lparam):
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append((int(hwnd), int(pid.value)))
        return True

    user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return out


def get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_process_image_path(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def get_window_rect(hwnd: int) -> Rect:
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_client_rect_size(hwnd: int) -> Tuple[int, int]:
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return (rect.right - rect.left, rect.bottom - rect.top)


def get_client_rect_on_screen(hwnd: int) -> Rect:
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return (origin.x, origin.y,
            origin.x + (rect.right - rect.left), origin.y + (rect.bottom - rect.top))


def is_iconic(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def is_window_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def get_foreground_window() -> int:
    hwnd = user32.GetForegroundWindow()          # 可能为 NULL（无前台窗口）→ ctypes 返回 None
    return int(hwnd) if hwnd else 0


def get_virtual_screen_rect() -> Rect:
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_XVIRTUALSCREEN) + user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN) + user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def get_monitor_rect(hwnd: int) -> Rect:
    MONITOR_DEFAULTTONEAREST = 2
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return get_virtual_screen_rect()
    r = info.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def get_cursor_pos() -> Tuple[int, int]:
    point = wt.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return (point.x, point.y)


VK_LBUTTON = 0x01


def is_key_down(vk: int) -> bool:
    """只读按键状态（不注入、不拦截）：用于"点击消息才开始分析"的触发检测。"""
    try:
        return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:
        return False


user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND

GA_ROOT = 2


def top_window_at_point(x: int, y: int) -> int:
    """返回屏幕坐标处**最上层窗口的顶层句柄**。

    用途：鼠标停在别的应用上时，不该"透过去"命中下面 QQ 窗口里的消息。
    """
    try:
        hwnd = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
        if not hwnd:
            return 0
        root = user32.GetAncestor(hwnd, GA_ROOT)
        return int(root or hwnd)
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# QQ 聊天窗口定位（SPEC 4.2）
# --------------------------------------------------------------------------- #
@dataclass
class QqWindow:
    hwnd: int
    pid: int
    rect: Rect
    title: str
    exe: str
    iconic: bool
    visible: bool
    area: int

    @property
    def session_id(self) -> str:
        return f"{self.hwnd}:{self.pid}:{normalize_session_title(self.title)}"


UNREAD_RE = re.compile(r"[（(]\s*\d+\s*[)）]\s*$")
BRACKET_NUM_RE = re.compile(r"[（(]\s*\d+\s*[)）]")


def normalize_session_title(title: str) -> str:
    """SPEC 4.4：去掉未读数与括号数字，作为会话标识的一部分（严禁含消息哈希）。"""
    text = UNREAD_RE.sub("", (title or "").strip())
    text = BRACKET_NUM_RE.sub("", text)
    return " ".join(text.split()).strip()


def _class_matches(cls: str, wanted: str) -> bool:
    """窗口类名匹配。第 78 轮加固：QQ 若把窗口换成同一族的别的类名
    （`Chrome_WidgetWin_0/1/2`），也能兜住 —— 只在"精确同名一个都没找到"时才放宽，
    所以老版本行为不变。"""
    if cls == wanted:
        return True
    if wanted.startswith("Chrome_WidgetWin") and cls.startswith("Chrome_WidgetWin"):
        return True
    return False


def find_qq_chat_window(window_class: str = "Chrome_WidgetWin_1",
                        exe_name: str = "qq.exe",
                        require_visible: bool = True,
                        min_on_screen_ratio: float = 0.3,
                        prefer_hwnd: Optional[int] = None) -> Optional[QqWindow]:
    """挑一个"看得见、在屏幕上、面积最大"的 QQ 聊天顶层窗口。

    实测坑：qq.exe 有多个 Chrome_WidgetWin_1 窗口（主窗口 / 会话窗口 / 预渲染窗口），
    其中一些 visible=True 但整块在屏幕外（rect 跑到负坐标或屏幕下方）。如果选了它们，
    消息还能读到（无障碍树照常有内容），但"点输入框"会点到别的程序上 —— B2 实测就踩了这个坑。
    因此这里要求：可见 + 未最小化 + **与屏幕的交集面积占比 ≥ min_on_screen_ratio**，
    并按"屏幕内面积"排序，而不是按窗口总面积。
    """
    def scan(exact: bool) -> Sequence[Optional[QqWindow]]:
        best: Optional[QqWindow] = None
        best_on_screen = 0
        foreground: Optional[QqWindow] = None  # 前台那个 QQ 窗口（若合格）
        preferred: Optional[QqWindow] = None   # 上一轮已经绑定的那个窗口（若仍合格）
        fg_hwnd = get_foreground_window()
        screen = get_virtual_screen_rect()
        for hwnd, pid in enum_top_level_windows():
            try:
                cls = get_class_name(hwnd)
                matched = (cls == window_class) if exact else _class_matches(cls, window_class)
                if not matched:
                    continue
                exe = get_process_image_path(pid).lower()
            except Exception:
                continue
            if exe_name.lower() not in exe:
                continue
            iconic, visible = is_iconic(hwnd), is_window_visible(hwnd)
            if require_visible and (iconic or not visible):
                continue
            rect = get_window_rect(hwnd)
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area < 200 * 200:
                continue
            on_screen = on_screen_area(rect, screen)
            if on_screen <= 0 or (on_screen / area) < min_on_screen_ratio:
                continue
            candidate = QqWindow(hwnd=hwnd, pid=pid, rect=rect, title=get_window_text(hwnd),
                                 exe=exe, iconic=iconic, visible=visible, area=area)
            if hwnd == fg_hwnd:
                foreground = candidate          # 用户正在用的那个窗口优先级最高
            if prefer_hwnd and hwnd == prefer_hwnd:
                preferred = candidate           # 已经绑定的窗口，别因为切焦点就跳走
            if best is None or on_screen > best_on_screen:
                best = candidate
                best_on_screen = on_screen
        return (foreground, preferred, best)

    foreground, preferred, best = scan(exact=True)
    # 第 78 轮：精确类名一个都没找到时，才放宽到"同一族类名"（抗 QQ 换类名）
    if foreground is None and preferred is None and best is None and window_class.startswith("Chrome_WidgetWin"):
        foreground, preferred, best = scan(exact=False)
    # 第 68b 轮（用户反馈"QQ 列表窗口还是会被状态灯和上下文列表跟随"）：
    # 有多个 QQ 窗口时（主窗口 + 独立会话窗口 / 预渲染窗口），旧实现一律挑"屏幕内面积最大"的，
    # 于是会贴到主窗口的会话列表区域上 —— 而用户在看的是另一个窗口。
    # 现在**优先前台那个 QQ 窗口**（用户点哪个就跟哪个），没有合格前台窗口才退回最大面积。
    return foreground or preferred or best


def on_screen_area(rect: Sequence[int], screen: Optional[Sequence[int]] = None) -> int:
    """窗口与屏幕的交集面积（物理像素）。"""
    screen = screen or get_virtual_screen_rect()
    left = max(rect[0], screen[0])
    top = max(rect[1], screen[1])
    right = min(rect[2], screen[2])
    bottom = min(rect[3], screen[3])
    return max(0, right - left) * max(0, bottom - top)


def is_rect_on_screen(rect: Sequence[int], screen: Optional[Sequence[int]] = None) -> bool:
    screen = screen or get_virtual_screen_rect()
    return (screen[0] <= rect[0] and screen[1] <= rect[1]
            and rect[2] <= screen[2] and rect[3] <= screen[3])


def list_qq_windows(window_class: str = "Chrome_WidgetWin_1",
                    exe_name: str = "qq.exe") -> List[QqWindow]:
    out: List[QqWindow] = []
    for hwnd, pid in enum_top_level_windows():
        try:
            # 第 78 轮：与 find_qq_chat_window 同一套类名匹配（精确 → 同族放宽）
            if not _class_matches(get_class_name(hwnd), window_class):
                continue
            exe = get_process_image_path(pid).lower()
        except Exception:
            continue
        if exe_name.lower() not in exe:
            continue
        rect = get_window_rect(hwnd)
        out.append(QqWindow(hwnd=hwnd, pid=pid, rect=rect, title=get_window_text(hwnd),
                            exe=exe, iconic=is_iconic(hwnd), visible=is_window_visible(hwnd),
                            area=max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])))
    return out


# --------------------------------------------------------------------------- #
# 一键填入所需的最小动作（SPEC 2.2(d)）
# --------------------------------------------------------------------------- #
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def clip_write_text(text: str) -> bool:
    """把文本写入剪贴板（不触碰 QQ）。"""
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


def clip_read_text() -> str:
    """读剪贴板文本（用于填入校验/自测）。"""
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def bring_window_to_front(hwnd: int) -> bool:
    """把 QQ 窗口置前（一键填入第一步；只做窗口管理，不改 QQ 属性）。"""
    try:
        # 只在"最小化"时还原；**不要无条件 SW_RESTORE**——对最大化窗口调用它会把窗口
        # 取消最大化并跳到另一个位置（实测会让 QQ 窗口"漂移"）
        if is_iconic(hwnd):
            user32.ShowWindow(wt.HWND(hwnd), 9)      # SW_RESTORE
        else:
            user32.ShowWindow(wt.HWND(hwnd), 4)      # SW_SHOWNOACTIVATE
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_V = 0x11, 0x56
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _INPUTUNION(ctypes.Union):
    """真实的 INPUT 是 {DWORD type; union{MOUSEINPUT, KEYBDINPUT, HARDWAREINPUT}}。

    x64 上 union 大小取 MOUSEINPUT（32 字节），所以 sizeof(INPUT)=40。
    早期只放了 KEYBDINPUT，结构体只有 32 字节 → SendInput 直接返回 0（静默失败），
    表现为"点了选项但输入框没内容"（B2 实测）。
    """
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send_key(vk: int, up: bool = False) -> int:
    event = _INPUT(type=INPUT_KEYBOARD)
    event.ki = _KEYBDINPUT(wVk=vk, wScan=0,
                           dwFlags=KEYEVENTF_KEYUP if up else 0,
                           time=0, dwExtraInfo=None)
    sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
    if sent != 1:
        logger.warning("SendInput 失败：size=%d err=%d", ctypes.sizeof(_INPUT),
                       ctypes.get_last_error())
    return int(sent)


def send_paste() -> bool:
    """模拟一次 Ctrl+V（一键填入第二步）。

    SPEC 2.2(d)：**绝不发送回车**，也绝不点击 QQ 的任何按钮；本函数只发这 4 个按键事件。
    返回 True 表示 4 个事件都真的送出去了（SendInput 返回 1/事件）。
    """
    try:
        results = [_send_key(VK_CONTROL), _send_key(VK_V),
                   _send_key(VK_V, up=True), _send_key(VK_CONTROL, up=True)]
        return all(r == 1 for r in results)
    except Exception as exc:
        logger.warning("模拟粘贴异常：%s: %s", type(exc).__name__, exc)
        return False


def ensure_foreground(hwnd: int, timeout_s: float = 1.0) -> bool:
    """把窗口弄到前台并确认（填入前必须的检查：否则按键会打到别的程序上）。"""
    if get_foreground_window() == hwnd:
        return True
    bring_window_to_front(hwnd)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if get_foreground_window() == hwnd:
            return True
        time.sleep(0.05)
    return get_foreground_window() == hwnd


# --------------------------------------------------------------------------- #
# 窗口归属与位置事件（让浮窗"只在 QQ 之上"且跟随无延迟）
# --------------------------------------------------------------------------- #
GWLP_HWNDPARENT = -8

user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_void_p


def set_owner_window(hwnd: int, owner_hwnd: int) -> bool:
    """把浮窗设成 QQ 窗口的 owned window。

    这样浮窗**永远在 QQ 之上、但不会盖住其他应用**（别的应用激活时自然排到前面），
    并且 QQ 最小化时浮窗会跟着隐藏 —— 比"全局 TopMost"更符合"只在 QQ 上层"的需求。
    """
    try:
        user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWLP_HWNDPARENT,
                                 ctypes.c_void_p(owner_hwnd))
        return True
    except Exception as exc:
        logger.debug("设置 owner 失败：%s", exc)
        return False


def get_window_owner(hwnd: int) -> int:
    try:
        return int(user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWLP_HWNDPARENT) or 0)
    except Exception:
        return 0


SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2

user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL


def force_show_window(hwnd: int, topmost: bool = True) -> bool:
    """强制让窗口可见（Qt 有时把无父对话框判为不可见，实测 WS_VISIBLE=false）。

    topmost=True 时临时置顶，适合"用户主动打开的设置窗口"。
    """
    try:
        user32.ShowWindow(wt.HWND(hwnd), 5)          # SW_SHOW
        user32.SetWindowPos(wt.HWND(hwnd),
                            wt.HWND(HWND_TOPMOST if topmost else HWND_NOTOPMOST),
                            0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
        return True
    except Exception as exc:
        logger.debug("force_show_window 失败：%s", exc)
        return False


def set_topmost(hwnd: int, topmost: bool) -> bool:
    """切换窗口的 topmost（不改大小/位置/激活状态）。"""
    try:
        user32.SetWindowPos(wt.HWND(hwnd),
                            wt.HWND(HWND_TOPMOST if topmost else HWND_NOTOPMOST),
                            0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return True
    except Exception:
        return False


def is_topmost(hwnd: int) -> bool:
    try:
        return bool(int(user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -20) or 0) & 0x8)
    except Exception:
        return False


EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_SYSTEM_MOVESIZEEND = 0x000A
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

_WINEventProc = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, wt.HWND,
                                   wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)


def install_location_hook(pid: int, callback) -> Tuple[int, object]:
    """监听"窗口移动/缩放"事件（零延迟跟随，替代高频轮询）。

    返回 (hook_handle, hook_proc)：hook_proc 必须由调用方持有，否则会被 GC。
    回调签名 callback(event, hwnd)。
    """
    def _cb(_hook, event, hwnd, _id_obj, _id_child, _thread, _time):  # noqa: ANN001
        try:
            callback(event, int(hwnd) if hwnd else 0)
        except Exception:
            pass

    proc = _WINEventProc(_cb)
    hook = user32.SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE,
                                  None, proc, pid, 0, WINEVENT_OUTOFCONTEXT)
    return (int(hook) if hook else 0, proc)


OBJID_CURSOR = -7


def install_cursor_hook(callback) -> Tuple[int, object]:
    """监听鼠标移动（WinEvent 的 OBJID_CURSOR 事件），用于"悬停切消息"零延迟响应。

    注意：这里用的是 **SetWinEventHook（事件回调）**，不是 SetWindowsHookEx（全局钩子，本项目禁止）。
    callback(event, hwnd, id_object)。
    """
    def _cb(_hook, event, hwnd, id_obj, id_child, _thread, _time):  # noqa: ANN001
        try:
            callback(event, int(hwnd) if hwnd else 0, int(id_obj))
        except Exception:
            pass

    proc = _WINEventProc(_cb)
    hook = user32.SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE,
                                  None, proc, 0, 0, WINEVENT_OUTOFCONTEXT)
    return (int(hook) if hook else 0, proc)


def set_tool_window(hwnd: int, tool: bool = True) -> bool:
    """给窗口加/去 WS_EX_TOOLWINDOW（不在任务栏/Alt-Tab 出现）。"""
    try:
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        style = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
        style = int(style or 0)
        style = (style | WS_EX_TOOLWINDOW) if tool else (style & ~WS_EX_TOOLWINDOW)
        user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ctypes.c_void_p(style))
        return True
    except Exception:
        return False


def uninstall_hook(hook: int) -> None:
    if hook:
        try:
            user32.UnhookWinEvent(ctypes.c_void_p(hook))
        except Exception:
            pass


user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                   _WINEventProc, wt.DWORD, wt.DWORD, wt.DWORD]
user32.SetWinEventHook.restype = ctypes.c_void_p
user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]


INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000


class _INPUT_MOUSE(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("mi", _MOUSEINPUT)]


def _send_mouse(flags: int, abs_x: int, abs_y: int) -> int:
    """发一个鼠标事件，返回 SendInput 实际投递的事件数（1=成功，0=被拦/失败）。"""
    event = _INPUT_MOUSE(type=INPUT_MOUSE,
                         mi=_MOUSEINPUT(dx=abs_x, dy=abs_y, mouseData=0,
                                        dwFlags=flags, time=0, dwExtraInfo=None))
    return int(user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT_MOUSE)))


def _send_mouse_batch(events) -> int:
    """一次 SendInput 投递**多个**鼠标事件（按顺序执行，中间插不进别的输入）。

    第 79 轮新增：把"移过去 + 按下 + 抬起"打成一个批次 —— 用户手还在动鼠标时，
    分三次发的话中间那 30-60ms 会被真实移动打断（旧实现甚至会因为"光标没到位"直接放弃点击，
    表现就是"点选项的同时移动鼠标就填不进去"）。批次里的 DOWN/UP 自带绝对坐标，
    落点由我们决定，和"当前光标在哪"无关。
    """
    if not events:
        return 0
    array = (_INPUT_MOUSE * len(events))()
    for index, (flags, abs_x, abs_y) in enumerate(events):
        array[index] = _INPUT_MOUSE(type=INPUT_MOUSE,
                                    mi=_MOUSEINPUT(dx=abs_x, dy=abs_y, mouseData=0,
                                                   dwFlags=flags, time=0,
                                                   dwExtraInfo=None))
    # 注意：SendInput 的 cbSize 必须是**单个 INPUT 结构体**的大小（不是数组总长！）。
    # 传成 3×40 会直接失败：err=87（ERROR_INVALID_PARAMETER）—— 实测"点了选项没反应"
    # 就是这条（SendInput 返回 0/3）。
    return int(user32.SendInput(len(events), array, ctypes.sizeof(_INPUT_MOUSE)))


def click_at(x: int, y: int) -> bool:
    """在屏幕坐标处点击一次（一键填入时用于把焦点交给 QQ 输入框）。

    SPEC 2.2(d)：这是"最小必要动作"的一部分；只在定位到输入框后调用，
    绝不点击发送按钮、绝不回车。相对坐标按虚拟桌面换算（多显示器也正确）。

    ★ 第 73 轮加固（朋友机器上出现"点选项只触发复制 / 偶尔鼠标彻底失灵"）：
      **任何异常/部分失败的路径都补一次 `LEFTUP`** —— 左键卡在按下状态正是
      "整机鼠标像失灵一样"的典型原因（用户只能按 Ctrl+Alt+Del 或拔鼠标才恢复）。
      被 UIPI 拦（QQ 以管理员身份运行、本工具是普通权限）时 SendInput 返回 0，返回 False，
      调用方退化成"只复制"。

    ★ 第 79 轮修 bug（用户："点击的同时移动了鼠标就很容易没自动填进去"）：
      旧实现在 MOVE 之后要 `GetCursorPos` 确认光标**停在**目标点（±6px），
      没停住就**放弃点击** —— 用户手还在动时这一条几乎必然命中，于是点了等于没点。
      现在：
        1) `MOVE + LEFTDOWN + LEFTUP` **一个批次**发出（中间插不进真实移动）；
        2) 批次里 DOWN/UP 带绝对坐标 → 落点由我们决定，不受"当前光标在哪"影响；
        3) 只有"SendInput 一个事件都没投递"（被 UIPI 拦）才放弃，并补一次 LEFTUP；
        4) 顺手加保护：用户此刻**正按着左键**（在拖拽/选择）时不注入点击，避免打断他，
           交给调用方的"只复制到剪贴板"兜底。
    """
    screen = get_virtual_screen_rect()
    width = max(1, screen[2] - screen[0])
    height = max(1, screen[3] - screen[1])
    abs_x = int((x - screen[0]) * 65535 / width)
    abs_y = int((y - screen[1]) * 65535 / height)
    base = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    try:
        if is_key_down(0x01):            # VK_LBUTTON：用户正按着左键 → 别插一脚
            logger.info("用户此刻正按着左键（可能在拖拽）→ 本次不注入点击，交给剪贴板兜底")
            return False
        sent = _send_mouse_batch([(base | MOUSEEVENTF_MOVE, abs_x, abs_y),
                                  (base | MOUSEEVENTF_LEFTDOWN, abs_x, abs_y),
                                  (base | MOUSEEVENTF_LEFTUP, abs_x, abs_y)])
        if sent == 3:
            return True
        logger.warning("点击事件未完整投递（SendInput 返回 %d/3，err=%d）→ 补一次抬起",
                       sent, ctypes.get_last_error())
        try:
            _send_mouse(base | MOUSEEVENTF_LEFTUP, abs_x, abs_y)
        except Exception:
            pass
        return False
    except Exception as exc:                      # 任何异常都不许把左键留在按下状态
        logger.warning("点击异常：%s: %s → 补一次抬起", type(exc).__name__, exc)
        try:
            _send_mouse(base | MOUSEEVENTF_LEFTUP, abs_x, abs_y)
        except Exception:
            pass
        return False


def click_at_physical(x: int, y: int) -> bool:
    """语义别名：本模块所有坐标都是物理像素。"""
    return click_at(x, y)


def fill_text_into_foreground(text: str, timeout_ms: int = 1500,
                              expect_hwnd: Optional[int] = None) -> Tuple[bool, str]:
    """一键填入完整流程：写剪贴板 → 模拟一次粘贴。返回 (成功, 说明)。

    注意：调用方需保证 QQ 聊天窗口已在前台且输入框已获得焦点（B2 由面板点击时机负责）；
    本函数不做任何重试（SPEC 2.2(d)）。

    第 63 轮加固：`expect_hwnd` 非空时，**发出粘贴前再查一次前台窗口** ——
    调用方 `ensure_foreground` 与这里之间有 ~150ms，若这期间用户切走了窗口，
    原来的实现会把 Ctrl+V 打进别的程序（泄漏的是"建议回复"这段文本）。
    现在只要前台不是预期的 QQ 窗口就**不发按键**，直接返回失败让调用方退化为"只复制"。
    """
    if expect_hwnd and get_foreground_window() != int(expect_hwnd):
        logger.warning("粘贴前前台窗口已改变（期望 %s，实际 %s）→ 放弃粘贴",
                       expect_hwnd, get_foreground_window())
        clip_write_text(text)                     # 文本仍进剪贴板，用户可手动 Ctrl+V
        return False, "前台窗口已改变（已复制到剪贴板，请手动 Ctrl+V）"
    if not clip_write_text(text):
        return False, "写剪贴板失败"
    time.sleep(0.02)
    if not send_paste():
        return False, "模拟粘贴失败"
    return True, "已填入（未发送）"


def rect_size(rect: Sequence[int]) -> Tuple[int, int]:
    return (max(0, rect[2] - rect[0]), max(0, rect[3] - rect[1]))


def get_window_scale(hwnd: int) -> float:
    """窗口所在显示器的缩放（150% → 1.5），用于逻辑/物理换算自检。"""
    try:
        dpi = user32.GetDpiForWindow(hwnd)
        return round(dpi / 96.0, 3) if dpi else 1.0
    except Exception:
        return 1.0


if __name__ == "__main__":
    set_dpi_awareness()
    win = find_qq_chat_window()
    print("QQ 窗口:", win)
    print("虚拟屏:", get_virtual_screen_rect())
