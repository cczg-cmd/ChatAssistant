#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui/wheel_sink.py - 安全的滚轮事件接收器（Raw Input + RIDEV_INPUTSINK）。

⚠ 为什么不用 WH_MOUSE_LL 全局钩子（2026-09-23 事故原因）：
   低级鼠标钩子的回调由系统在**我们线程**里调用，**我们必须返回后系统才继续投递鼠标事件**
   → 我们的线程一卡（GIL 争用、耗时布局计算…），整台机器的鼠标就跟着卡死。

本模块采用 Raw Input：
   - `RegisterRawInputDevices(..., RIDEV_INPUTSINK, hwndTarget=本窗口)` 让系统把鼠标事件的
     **副本**投进我们窗口的消息队列；
   - 我们**不在输入投递链路上**：不处理、处理慢、甚至线程卡死，都只影响我们自己收不到事件，
     系统鼠标与其它应用完全不受影响；
   - 解析在 Qt 的 nativeEvent 里做，只读 `RAWINPUT` 结构，不注入、不拦截、不改任何输入。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes as wt

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger("ui.wheel_sink")

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_INPUT = 0x00FF
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RI_MOUSE_WHEEL = 0x0400
RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
HWND_MESSAGE = -3


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class RAWBUTTONPAIR(ctypes.Structure):
    """C 里的匿名 struct：usButtonFlags 在偏移 0、usButtonData 在偏移 2。"""
    _fields_ = [("usButtonFlags", wt.USHORT), ("usButtonData", wt.USHORT)]


class RAWBUTTONS(ctypes.Union):
    """WinUser.h: RAWMOUSE 里按钮部分是 union { ULONG ulButtons;
    struct { USHORT usButtonFlags; USHORT usButtonData; }; } —— 必须按 union 布局，
    否则 usButtonFlags/usButtonData 会错位 2 字节，读到垃圾（踩过的坑）。"""
    _fields_ = [("ulButtons", wt.ULONG), ("pair", RAWBUTTONPAIR)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wt.USHORT), ("buttons", RAWBUTTONS),
                ("ulRawButtons", wt.ULONG), ("lLastX", wt.LONG), ("lLastY", wt.LONG),
                ("ulExtraInformation", wt.ULONG)]


class RAWINPUT(ctypes.Structure):
    """只关心鼠标：联合体的第一个成员就是 RAWMOUSE。"""
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt_x", wt.LONG),
                ("pt_y", wt.LONG)]


class _RawInputLoop(QThread):
    """自己的原生窗口 + 自己的消息循环（不用 Qt 的窗口，Qt 不会派发 WM_INPUT —— 实测踩过）。

    安全性：Raw Input（RIDEV_INPUTSINK）只是把鼠标事件的**副本**投进本窗体的消息队列，
    我们不在输入投递链路上 —— 这个线程卡死也只影响自己收不到事件，鼠标与其它应用不受影响。
    """

    wheeled = Signal(int, int, int)
    clicked = Signal(int, int)
    ready = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._hwnd = 0
        self._wndproc = None
        self._class_name = "ChatAssistantRawInput"
        # 第 81 轮末（用户："退出程序后关机，有时会弹'xxx 不能为 read'的报错"）：
        # 系统关机/注销时 Windows 会给每个顶层窗口发 WM_QUERYENDSESSION / WM_ENDSESSION。
        # 我们这个原生窗口是常驻的 → 在这里把"要关机了"这件事**直接**告诉上层
        # （回调在窗口线程里执行，不走 Qt 事件队列：关机时队列可能没机会跑）。
        self.on_session_end = None          # Callable[[bool], None]；True = 会话真的要结束了

    def stop(self) -> None:
        try:
            if self._hwnd:
                user32.PostMessageW(wt.HWND(self._hwnd), 0x0010, 0, 0)   # WM_CLOSE
        except Exception:
            pass

    def run(self) -> None:
        import ctypes
        from ctypes import wintypes as _wt

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", _wt.UINT), ("style", _wt.UINT),
                        ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", _wt.HINSTANCE),
                        ("hIcon", _wt.HICON), ("hCursor", _wt.HANDLE),
                        ("hbrBackground", _wt.HBRUSH), ("lpszMenuName", _wt.LPCWSTR),
                        ("lpszClassName", _wt.LPCWSTR), ("hIconSm", _wt.HICON)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, _wt.HWND, _wt.UINT,
                                     _wt.WPARAM, _wt.LPARAM)
        # 必须显式声明参数类型：否则 ctypes 会按 32 位猜，遇到大的 lParam 直接
        # OverflowError: int too long to convert（在 stderr 里刷 Traceback）。
        user32.DefWindowProcW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.CreateWindowExW.restype = _wt.HWND
        user32.RegisterClassExW.restype = _wt.ATOM
        # 第 81 轮末（faulthandler 实测抓到的 access violation 就落在这两个调用上）：
        # **必须显式声明 argtypes** —— 只声明 restype 时，ctypes 会按默认规则猜参数类型
        # （x64 下整型一律当 c_int 处理，指针/句柄靠调用方给的对象兜底），
        # 在某些环境下会在 Win32 内部触发访问冲突（表现为关机/退出时弹"xxx 不能为 read"）。
        # 这里把用到的 API 参数类型全部补全，和其它 ctypes 调用保持一致。
        user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
        user32.CreateWindowExW.argtypes = [
            _wt.DWORD, _wt.LPCWSTR, _wt.LPCWSTR, _wt.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            _wt.HWND, _wt.HMENU, _wt.HINSTANCE, ctypes.c_void_p]
        user32.ShowWindow.argtypes = [_wt.HWND, ctypes.c_int]
        user32.ShowWindow.restype = _wt.BOOL
        user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, _wt.UINT, _wt.UINT]
        user32.RegisterRawInputDevices.restype = _wt.BOOL
        user32.GetMessageW.argtypes = [ctypes.c_void_p, _wt.HWND, _wt.UINT, _wt.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.c_void_p]
        user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = [_wt.HWND]
        user32.DestroyWindow.restype = _wt.BOOL
        user32.PostMessageW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
        user32.PostMessageW.restype = _wt.BOOL
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.FindWindowW.argtypes = [_wt.LPCWSTR, _wt.LPCWSTR]
        user32.FindWindowW.restype = _wt.HWND

        def wndproc(hwnd, message, wparam, lparam):
            try:
                if message == WM_INPUT:
                    self._handle_input(int(lparam))
                elif message == 0x0011:                     # WM_QUERYENDSESSION
                    logger.info("系统要关机/注销了（WM_QUERYENDSESSION）→ 先停掉正在跑的生成")
                    cb = getattr(self, "on_session_end", None)
                    if callable(cb):
                        try:
                            cb(False)
                        except Exception as exc:
                            logger.warning("关机前置处理失败：%s: %s", type(exc).__name__, exc)
                    return 1                                # 允许系统继续关机
                elif message == 0x0016:                     # WM_ENDSESSION
                    if int(wparam):
                        logger.info("会话结束（WM_ENDSESSION）→ 直接结束进程，不做收尾")
                        cb = getattr(self, "on_session_end", None)
                        if callable(cb):
                            try:
                                cb(True)
                            except Exception:
                                pass
                        import os as _os
                        _os._exit(0)                        # 不走析构：避免在 CUDA/llama 里崩
                    return 0
                elif message == 0x0010:                     # WM_CLOSE
                    user32.DestroyWindow(hwnd)
                elif message == 0x0002:                     # WM_DESTROY
                    user32.PostQuitMessage(0)
            except Exception as exc:
                import time as _t2
                if _t2.time() - getattr(self, "_last_err_log", 0.0) > 2.0:
                    self._last_err_log = _t2.time()
                    logger.warning("WM_INPUT 处理失败：%s: %s", type(exc).__name__, exc)
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)
        instance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = instance
        wc.lpszClassName = self._class_name
        user32.RegisterClassExW(ctypes.byref(wc))
        # 第 74 轮（用户："启动后还有个 ChatAssistantRawInput 窗口，开着关着都没影响"）：
        # 这个窗口**有用**（RIDEV_INPUTSINK 要求有个窗口收 WM_INPUT：滚轮→收起面板、
        # 点击去重都走它），但它以前会在 Alt+Tab / 任务栏里露出来。
        # 加 `WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE` 之后：不进 Alt+Tab、不抢焦点、
        # 也永远不出现在任务栏 —— 功能不变，界面上彻底看不见。
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        self._hwnd = int(user32.CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                                self._class_name, "ChatAssistantRawInput",
                                                0x80000000, -32000, -32000, 2, 2,   # WS_POPUP
                                                None, None, instance, None) or 0)
        ok = False
        if self._hwnd:
            user32.ShowWindow(_wt.HWND(self._hwnd), 4)      # SW_SHOWNOACTIVATE（屏幕外）
            device = RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, _wt.HWND(self._hwnd))
            ok = bool(user32.RegisterRawInputDevices(ctypes.byref(device), 1,
                                                     ctypes.sizeof(RAWINPUTDEVICE)))
        self.ready.emit(ok)
        if not ok:
            logger.warning("Raw Input 注册失败，err=%s（滚轮预测/点击加速不可用，功能不受影响）",
                           ctypes.get_last_error())
            return
        logger.info("Raw Input 已就绪（独立原生窗口；不在输入投递链路上，不会阻塞系统鼠标）")
        msg = _wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self._hwnd:
            user32.UnregisterClassW(self._class_name, instance)

    def _handle_input(self, lparam: int) -> None:
        """解析一条 WM_INPUT（放在 _RawInputLoop 里：之前误留在旧类体里，导致每个事件都
        AttributeError 被吞掉 → 滚轮预测从未生效）。"""
        # 诊断：先记录"任何 WM_INPUT 到达"，再谈解析成功与否
        import time as _t
        if _t.time() - getattr(self, "_last_any_log", 0.0) > 1.0:
            self._last_any_log = _t.time()
            logger.info("收到 WM_INPUT（准备解析）")
        # 注意（踩过）：_parse_input 当时被留在了旧类体（MouseWheelSink）里，
        # 这里一直 AttributeError 被吞掉 → 滚轮预测从未生效。按类名调用即可。
        wheel, left_down = MouseWheelSink._parse_input(lparam)
        if not wheel and not left_down:
            return
        point = wt.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        import time as _time
        if _time.time() - getattr(self, "_last_log", 0.0) > 1.0:     # 低频诊断日志
            self._last_log = _time.time()
            logger.info("Raw Input 送达：wheel=%s left_down=%s @(%d,%d)",
                        wheel, left_down, point.x, point.y)
        if wheel:
            self.wheeled.emit(int(wheel), int(point.x), int(point.y))
        if left_down:
            self.clicked.emit(int(point.x), int(point.y))


class MouseWheelSink(QObject):
    """对外的滚轮/左键事件源（内部跑一个独立原生窗口线程）。"""

    wheeled = Signal(int, int, int)
    clicked = Signal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.registered = False
        self._thread = _RawInputLoop()
        self._thread.wheeled.connect(self.wheeled)
        self._thread.clicked.connect(self.clicked)
        self._thread.ready.connect(self._on_ready)
        self._thread.start()

    def _on_ready(self, ok: bool) -> None:
        self.registered = bool(ok)

    def stop(self) -> None:
        self._thread.stop()

    @staticmethod
    def _parse_input(lparam: int):
        """返回 (滚轮增量, 是否左键按下)。

        Raw Input 会把**每一个**按键事件都投给我们，所以不会像 GetAsyncKeyState 轮询那样
        漏掉"极短的点击"（用户反馈的"偶尔点了没反应"）。
        """
        size = wt.UINT(0)
        header = ctypes.sizeof(RAWINPUTHEADER)
        user32.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, ctypes.c_void_p,
                                           ctypes.POINTER(wt.UINT), wt.UINT]
        user32.GetRawInputData.restype = wt.UINT
        if user32.GetRawInputData(wt.HANDLE(lparam), RID_INPUT, None,
                                  ctypes.byref(size), header) != 0 or size.value == 0:
            return (0, False)
        buffer = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(wt.HANDLE(lparam), RID_INPUT, buffer,
                                     ctypes.byref(size), header)
        if got != size.value:
            return (0, False)
        raw = ctypes.cast(buffer, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType != RIM_TYPEMOUSE:
            return (0, False)
        # 注意：两个字段都要从 pair（真正的 struct）里取 —— 直接在 union 上取会都落在偏移 0，
        # 于是滚轮增量会读成标志位本身（实测读到 1024 = 0x400）。
        flags = raw.mouse.buttons.pair.usButtonFlags
        wheel = (ctypes.c_short(raw.mouse.buttons.pair.usButtonData).value
                 if flags & RI_MOUSE_WHEEL else 0)
        return (int(wheel), bool(flags & RI_MOUSE_LEFT_BUTTON_DOWN))
