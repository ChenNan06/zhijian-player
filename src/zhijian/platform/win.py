"""Windows 平台适配：DPI 感知、无边框窗口的原生缩放/移动、资源管理器集成。

核心做法：**交给系统做**（WM_NCHITTEST + WM_NCCALCSIZE），
而不是自己用定时器读光标去 setGeometry —— 后者是上一代产品"拉边框不动/只在没有子控件的
那一侧拉得动"的根因。系统接管后顺带白送 Aero Snap 与 Win11 的贴靠布局。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes

log = logging.getLogger("zhijian.win")

IS_WINDOWS = sys.platform == "win32"

# ---- 常量 ------------------------------------------------------------------
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCACTIVATE = 0x0086
WM_GETMINMAXINFO = 0x0024
WM_DPICHANGED = 0x02E0
WM_SYSCOMMAND = 0x0112
SC_KEYMENU = 0xF100

GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_POPUP = 0x80000000
WS_CLIPCHILDREN = 0x02000000

HTTRANSPARENT = -1
HTNOWHERE = 0
HTCLIENT = 1
HTCAPTION = 2
HTSYSMENU = 3
HTMINBUTTON = 8
HTMAXBUTTON = 9
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

MONITOR_DEFAULTTONEAREST = 2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
HWND_TOP = 0
HWND_NOTOPMOST = -2
HWND_TOPMOST = -1

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

_user32 = None
_dwmapi = None
_shcore = None


def _u():
    global _user32
    if _user32 is None:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
    return _user32


# --------------------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------------------

def enable_dpi_awareness() -> bool:
    """开进程级 PMv2 DPI 感知。

    ⚠️ `SetProcessDpiAwarenessContext` 在 **User32.dll**, 不在 kernel32
    （放错会抛 AttributeError, 而它发生在 import 期 → "整个程序起不来"）。
    """
    if not IS_WINDOWS:
        return False
    try:
        u = _u()
        fn = getattr(u, "SetProcessDpiAwarenessContext", None)
        if fn is None:
            return False
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = wintypes.BOOL
        ok = bool(fn(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        return ok
    except Exception:
        log.debug("设置 DPI 感知失败", exc_info=True)
        return False


# --------------------------------------------------------------------------------------
# 结构体（一律用 ctypes.wintypes, 不自定义同形结构 —— 避免 argtypes 互相覆盖）
# --------------------------------------------------------------------------------------


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", wintypes.POINT),
        ("ptMaxSize", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


def _bind():
    u = _u()
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    u.SetWindowLongPtrW.restype = ctypes.c_void_p
    u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowLongPtrW.restype = ctypes.c_void_p
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.SetWindowPos.restype = wintypes.BOOL
    u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    u.MonitorFromWindow.restype = wintypes.HANDLE
    u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    u.GetMonitorInfoW.restype = wintypes.BOOL
    u.IsZoomed.argtypes = [wintypes.HWND]
    u.IsZoomed.restype = wintypes.BOOL
    u.ReleaseCapture.argtypes = []
    u.ReleaseCapture.restype = wintypes.BOOL
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.restype = ctypes.c_void_p
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.GetForegroundWindow.argtypes = []
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u.SetCursorPos.restype = wintypes.BOOL
    return u


def _hwnd(win) -> int:
    try:
        return int(win.winId())
    except Exception:
        return 0


def make_resizable_frameless(win) -> bool:
    """给无边框窗口补上 WS_THICKFRAME，让系统的边缘缩放重新可用。"""
    if not IS_WINDOWS:
        return False
    try:
        u = _bind()
        hwnd = _hwnd(win)
        if not hwnd:
            return False
        style = int(u.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_STYLE) or 0)
        style |= WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU | WS_CLIPCHILDREN
        style &= ~WS_POPUP
        u.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_STYLE, ctypes.c_void_p(style))
        u.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except Exception:
        log.exception("设置 WS_THICKFRAME 失败")
        return False


def _lparam_xy(lparam: int, dpr: float) -> tuple[int, int]:
    """WM_NCHITTEST 的 lParam 是**物理像素**屏幕坐标；转成逻辑像素。"""
    x = ctypes.c_short(lparam & 0xFFFF).value
    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
    if dpr and dpr > 0:
        return int(round(x / dpr)), int(round(y / dpr))
    return x, y


def hit_test(win, lparam: int, border: int = 6, titlebar_h: int = 46,
             buttons: dict | None = None, resize_enabled: bool = True) -> int:
    """根据光标位置返回 HT* 码。

    buttons: {"min": QRect, "max": QRect}（控件在窗口内的逻辑坐标矩形）
             —— 命中最大化按钮时返回 HTMAXBUTTON, Win11 会弹出「贴靠布局」。
    """
    lp = local_point(win, lparam)
    x, y = lp.x(), lp.y()
    w, h = win.width(), win.height()
    if x < 0 or y < 0 or x >= w or y >= h:
        return HTNOWHERE
    if buttons:
        for key, rect in buttons.items():
            if rect is not None and rect.contains(lp):
                return {"min": HTMINBUTTON, "max": HTMAXBUTTON}.get(key, HTCLIENT)
    if resize_enabled and not win.isMaximized() and not win.isFullScreen():
        left, right = x < border, x >= w - border
        top, bottom = y < border, y >= h - border
        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
    return HTCLIENT


def local_point(win, lparam: int):
    """把 WM_* 消息里的屏幕物理坐标换成窗口内逻辑坐标。"""
    from PySide6.QtCore import QPoint

    dpr = float(win.devicePixelRatioF() or 1.0)
    gx, gy = _lparam_xy(lparam, dpr)
    return win.mapFromGlobal(QPoint(gx, gy))


def handle_nc_calcsize(hwnd: int, lparam: int) -> None:
    """WM_NCCALCSIZE：把非客户区完全去掉（客户区 = 窗口矩形）；最大化时贴显示器工作区。

    ⚠️⚠️ 这里曾经有一行**把整个窗口尺寸钉死**的错误代码，值得单独记一笔：

        params.rgrc[0] = params.rgrc[1]        # ← 错得离谱

    `wParam = TRUE` 时系统给的三个矩形含义是：
        rgrc[0] = **系统算好的新窗口矩形**（我们要返回的就是它）
        rgrc[1] = 移动/缩放**之前**的窗口矩形
        rgrc[2] = 移动/缩放之前的客户区矩形
    也就是说 `rgrc[0]` 本来就已经是"新窗口矩形"了，**什么都不用做，直接返回 0 就是无边框**。
    把它赋成 `rgrc[1]` 等于每次都把客户区退回**旧尺寸** —— `GetClientRect` 于是永远比
    `GetWindowRect` 小一截（实测窗口 1200x900 时客户区报 2380x1330）。

    后果（用户报的"全屏黑屏 + 全屏效果出 BUG + 最大化同样"）：
    Qt 的窗口几何是从客户区算的，客户区被钉死在旧尺寸 ⇒ **Qt 永远以为窗口还是原来那么大**。
    全屏后原生窗口已经是 2560x1600，Qt 却报 1280x760，视频层只铺了左上角约 3/4，
    右边和下边一片黑。关掉 nativeEvent 立刻正常 —— 这就是定位到它的关键实验。

    ⚠️ 另一条：必须传入**裸 HWND**（消息里的 msg.hWnd），绝不能在这里调 `win.winId()` ——
    首次 WM_NCCALCSIZE 发生在 CreateWindowEx 完成之前，那时调 winId() 会让 Qt
    递归地再去创建平台窗口，结果是 CreateWindowEx 失败并无限重试。
    """
    try:
        if not hwnd:
            return
        if not IsZoomed(hwnd):
            # 非最大化：**一个字都不要改**（见上面的说明）
            return
        params = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
        u = _bind()
        hmon = u.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            params.rgrc[0] = mi.rcWork       # 最大化：贴工作区, 不遮任务栏
    except Exception:
        log.debug("WM_NCCALCSIZE 处理失败", exc_info=True)


def IsZoomed(hwnd: int) -> bool:
    if not hwnd:
        return False
    try:
        return bool(_bind().IsZoomed(wintypes.HWND(hwnd)))
    except Exception:
        return False


def set_topmost(win, on: bool) -> None:
    """临时置顶 / 取消置顶。

    无边框全屏要盖住任务栏，但**不能**改成 `WS_POPUP`（那会让 Windows 重建窗口、
    丢掉 GL 交换链，用户就看到"切全屏黑一下"）。
    `SetWindowPos(HWND_TOPMOST)` 只改 Z 序、不动样式，是这里唯一正确的做法。
    """
    try:
        u = _bind()
        hwnd = wintypes.HWND(int(win.winId()))
        HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
        SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
        SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0010, 0x0200
        u.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                       SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
    except Exception:
        log.debug("设置置顶失败", exc_info=True)


def move_window(win, x: int, y: int) -> bool:
    """只挪位置、**不动尺寸**地移动窗口（用原生 SetWindowPos）。

    ⚠️ 不要用 `QWidget.move()` 来做这件事。在"无边框 + WM_NCCALCSIZE 返回 0"的窗口上，
    Qt 的 `move()` 会让 **Qt 自己缓存的尺寸与 Windows 原生窗口的实际尺寸失步** ——
    实测：`resize(800,600)` 之后调 `move(4,40)`，Qt 侧仍是 800x600，
    原生窗口却跳回 1920x1140（上一次的尺寸），而且之后 Qt 认为"尺寸没变"不再下发
    SetWindowPos，两边就永久对不上了。
    真实用户拖动标题栏走的是系统模态循环（WM_NCLBUTTONDOWN/HTCAPTION），不受影响 ——
    只有程序化 move() 会踩到。这里统一改用带 SWP_NOSIZE 的原生调用。
    """
    try:
        u = _bind()
        hwnd = _hwnd(win)
        if not hwnd:
            return False
        dpr = float(getattr(win, "devicePixelRatioF", lambda: 1.0)() or 1.0)
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
        u.SetWindowPos(wintypes.HWND(hwnd), None, int(x * dpr), int(y * dpr), 0, 0,
                       SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return True
    except Exception:
        log.debug("移动窗口失败", exc_info=True)
        return False


def start_system_move(win, lparam: int = 0) -> None:
    """用系统的模态移动循环拖动窗口（白送 Aero Snap）。

    ⚠️ lParam 必须是屏幕坐标的 MAKELPARAM(x, y)；传 0 的话系统按光标在 (0,0)
       计算偏移, 移动循环会立刻放弃。
    """
    try:
        u = _bind()
        hwnd = _hwnd(win)
        if not hwnd:
            return
        if not lparam:
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            lparam = (pt.y << 16) | (pt.x & 0xFFFF)
        u.ReleaseCapture()
        u.SendMessageW(wintypes.HWND(hwnd), 0x00A1, HTCAPTION, lparam)   # WM_NCLBUTTONDOWN
    except Exception:
        log.debug("系统移动失败", exc_info=True)


def monitor_work_area(win) -> tuple[int, int, int, int] | None:
    if not IS_WINDOWS:
        return None
    try:
        u = _bind()
        hwnd = _hwnd(win)
        hmon = u.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        pass
    return None


def set_always_on_top(win, on: bool) -> None:
    try:
        u = _bind()
        hwnd = _hwnd(win)
        u.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST if on else HWND_NOTOPMOST),
                       0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    except Exception:
        log.debug("置顶失败", exc_info=True)


def force_foreground(win) -> bool:
    """把窗口拉到前台（非前台进程直接调 SetForegroundWindow 会静默失败）。"""
    try:
        u = _bind()
        hwnd = _hwnd(win)
        if not hwnd:
            return False
        if u.GetForegroundWindow() == hwnd:
            return True
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentThreadId.restype = wintypes.DWORD
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        pid = wintypes.DWORD()
        fg_thread = u.GetWindowThreadProcessId(u.GetForegroundWindow(), ctypes.byref(pid))
        my_thread = k32.GetCurrentThreadId()
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u.AttachThreadInput(fg_thread, my_thread, True)
        try:
            u.SetForegroundWindow(wintypes.HWND(hwnd))
        finally:
            u.AttachThreadInput(fg_thread, my_thread, False)
        return u.GetForegroundWindow() == hwnd
    except Exception:
        log.debug("置前台失败", exc_info=True)
        return False


def enable_rounded_corners(win, on: bool = True) -> None:
    """Win11 原生圆角（无边框窗口必须显式开，否则是直角）。

    ⚠️ 全屏时**必须关掉**（`on=False`）：DWM 的圆角是**按窗口矩形裁**的，
        视频铺满整个窗口时四角会被切掉；同时 DWM 还会画一圈边框高光 ——
        用户看到的就是"全屏了画面还是圆角的、四个角不完整、还有白边"。
    """
    if not IS_WINDOWS:
        return
    try:
        global _dwmapi
        if _dwmapi is None:
            _dwmapi = ctypes.WinDLL("dwmapi")
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        DWMWCP_DONOTROUND = 1
        val = ctypes.c_int(DWMWCP_ROUND if on else DWMWCP_DONOTROUND)
        _dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                  ctypes.c_void_p, wintypes.DWORD]
        _dwmapi.DwmSetWindowAttribute(wintypes.HWND(_hwnd(win)), DWMWA_WINDOW_CORNER_PREFERENCE,
                                      ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        log.debug("设置 Win11 圆角失败", exc_info=True)


def set_window_border(win, on: bool = True) -> None:
    """窗口外框那条 1px 高光线（DWM 画的，不是我们的 QSS）。

    全屏时也要关掉，否则贴着屏幕边缘会看到一圈"白边"。
    DWMWA_COLOR_NONE = 0xFFFFFFFE 表示"不画边框"。
    """
    if not IS_WINDOWS:
        return
    try:
        global _dwmapi
        if _dwmapi is None:
            _dwmapi = ctypes.WinDLL("dwmapi")
        DWMWA_BORDER_COLOR = 34
        DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
        DWMWA_COLOR_NONE = 0xFFFFFFFE
        val = ctypes.c_uint(DWMWA_COLOR_DEFAULT if on else DWMWA_COLOR_NONE)
        _dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                  ctypes.c_void_p, wintypes.DWORD]
        _dwmapi.DwmSetWindowAttribute(wintypes.HWND(_hwnd(win)), DWMWA_BORDER_COLOR,
                                      ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        log.debug("设置窗口边框失败", exc_info=True)


def set_dark_mode(win, dark: bool = True) -> None:
    """让系统绘制的弹菜单/工具提示也用深色（Win10 1809+）。"""
    if not IS_WINDOWS:
        return
    try:
        global _dwmapi
        if _dwmapi is None:
            _dwmapi = ctypes.WinDLL("dwmapi")
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        val = ctypes.c_int(1 if dark else 0)
        _dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                  ctypes.c_void_p, wintypes.DWORD]
        _dwmapi.DwmSetWindowAttribute(wintypes.HWND(_hwnd(win)), DWMWA_USE_IMMERSIVE_DARK_MODE,
                                      ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        log.debug("设置深色模式失败", exc_info=True)


# --------------------------------------------------------------------------------------
# 系统集成
# --------------------------------------------------------------------------------------

def reveal_in_explorer(path: str) -> None:
    """在资源管理器里定位文件。"""
    try:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)],
                             close_fds=True)
        elif os.path.isdir(path):
            os.startfile(os.path.normpath(path))  # noqa: S606
    except Exception:
        log.debug("打开资源管理器失败", exc_info=True)


def open_path(path: str) -> None:
    try:
        os.startfile(os.path.normpath(path))  # noqa: S606
    except Exception:
        log.debug("打开路径失败", exc_info=True)


def play_system_sound(name: str = "SystemAsterisk") -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK if name == "SystemAsterisk" else winsound.MB_OK)
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# 独显加速（Optimus / 混合显卡笔记本）
# --------------------------------------------------------------------------------------

def set_gpu_preference(exe_path: str, high_performance: bool = True) -> bool:
    """把本程序登记为「高性能 GPU」。

    写 `HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences`：
    等于在「设置 → 系统 → 显示 → 图形」里手工选「高性能」。不需要管理员。
    写完后需要**重启程序**才生效。
    """
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        key_path = r"Software\Microsoft\DirectX\UserGpuPreferences"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
            value = "GpuPreference=2;" if high_performance else "GpuPreference=0;"
            winreg.SetValueEx(k, os.path.abspath(exe_path), 0, winreg.REG_SZ, value)
        return True
    except Exception:
        log.debug("写入 GPU 首选项失败", exc_info=True)
        return False


def get_gpu_preference(exe_path: str) -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\DirectX\UserGpuPreferences") as k:
            return winreg.QueryValueEx(k, os.path.abspath(exe_path))[0]
    except Exception:
        return ""


# --------------------------------------------------------------------------------------
# 窗口级亚克力（QQ 那种"窗口边缘透出并模糊桌面"的效果）
# --------------------------------------------------------------------------------------
# ⚠️ 这不是"每个面板各做一次背景模糊"（那要 GPU 后处理，100+ 行 GL）。
#    QQ 用的是**窗口级**的：DWM 把窗口后面的东西模糊掉，
#    所以只有在窗口**自身是半透明**的地方才看得见。
#    Win11 22H2+ 走 DWMWA_SYSTEMBACKDROP_TYPE；老系统回落 SetWindowCompositionAttribute。
ACRYLIC_OFF = os.environ.get("ZHJ_NO_ACRYLIC") == "1"


def set_acrylic(win, on: bool = True) -> bool:
    """给窗口开启/关闭亚克力。返回是否真的开上了（调用方据此决定要不要做半透明底）。"""
    if not IS_WINDOWS or ACRYLIC_OFF:
        return False
    try:
        u = _bind()
        hwnd = wintypes.HWND(int(win.winId()))
        dwm = ctypes.WinDLL("dwmapi.dll")
        DWMWA_SYSTEMBACKDROP_TYPE = 38
        DWMSBT_TRANSIENTWINDOW = 3          # 亚克力
        DWMSBT_NONE = 1
        val = ctypes.c_int(DWMSBT_TRANSIENTWINDOW if on else DWMSBT_NONE)
        hr = dwm.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                                       ctypes.byref(val), ctypes.sizeof(val))
        if hr == 0:
            log.info("窗口亚克力: SYSTEMBACKDROP_TYPE = %s", val.value)
            return bool(on)
        # ---- 回落：SetWindowCompositionAttribute（Win10 1803+ 的未公开接口）----
        class ACCENTPOLICY(ctypes.Structure):
            _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                        ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]

        class WINCOMPATTRDATA(ctypes.Structure):
            _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                        ("SizeOfData", ctypes.c_size_t)]

        ACCENT_DISABLED, ACCENT_ENABLE_ACRYLICBLURBEHIND = 0, 4
        WCA_ACCENT_POLICY = 19
        policy = ACCENTPOLICY()
        policy.AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND if on else ACCENT_DISABLED
        policy.AccentFlags = 2
        policy.GradientColor = 0x01000000        # 近全透明
        data = WINCOMPATTRDATA()
        data.Attribute = WCA_ACCENT_POLICY
        data.Data = ctypes.cast(ctypes.byref(policy), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(policy)
        fn = getattr(u, "SetWindowCompositionAttribute", None)
        if fn is None:
            return False
        fn.argtypes = [wintypes.HWND, ctypes.POINTER(WINCOMPATTRDATA)]
        ok = bool(fn(hwnd, ctypes.byref(data)))
        log.info("窗口亚克力: SetWindowCompositionAttribute = %s", ok)
        return ok and on
    except Exception:
        log.debug("开启亚克力失败", exc_info=True)
        return False


# --------------------------------------------------------------------------------------
# 文件关联 / 默认打开方式（HKCU only，不需要管理员）
# --------------------------------------------------------------------------------------
# 用户现象：双击媒体文件 → 弹"你要如何打开这个文件" → 选至简Player → **记不住**，
#   下次还弹。逐条查下来是注册方式不符合 Windows Shell 规范：
#     ① 只有一个泛化 ProgID（ZhiJianPlayer.Media）—— 音频/视频/图片/字幕全挤在一起；
#     ② 历史写进去的 shell open command 是**相对路径 + 正斜杠**
#        （实测现网值 "output/至简Player-Portable-1.0.36/至简Player.exe"）——
#        这种命令根本启动不了，系统自然只剩"选择应用"这一条路；
#     ③ 缺 Capabilities / RegisteredApplications 规范登记，不入"默认应用"列表。
# 本节实现严格按 Shell 标准（全部在 HKCU 下，**绝不写 HKLM、不需要管理员**）：
#     · 按类型分 ProgID：ZhiJianPlayer.Audio / .Video / .Image / .Subtitle
#     · shell open command = 严格双引号 + **绝对路径**
#     · DefaultIcon / FriendlyTypeName / InfoTip / Application 描述
#     · Classes .<ext> OpenWithProgids 写入对应 ProgID（进"打开方式"列表）
#     · Software ZhiJianPlayer Capabilities + RegisteredApplications（进"默认应用"）
#     · 幂等（重复执行结果一致）与可回滚（反注册逐项还原，含**默认值备份**）
#     · **扩展名级 DefaultIcon**：42 个扩展名 → 各自类别的专用图标（audio/video/image/
#       subtitle.ico 的绝对路径）。图标由 scripts\05-make-icon.py（Pillow）生成，随包放
#       assets\；路径按"目标 exe 目录"解析，**绝不硬编码开发机路径**（见 _candidate_asset_dirs）。
ASSOC_APP_NAME = "至简Player"
ASSOC_APP_ID = "ZhiJianPlayer"
ASSOC_REGISTERED_NAME = "ZhiJianPlayer"          # RegisteredApplications 项名
ASSOC_ROOT = r"Software\ZhiJianPlayer"         # Capabilities / 备份都挂在这下面
ASSOC_CAPABILITIES = ASSOC_ROOT + r"\Capabilities"
ASSOC_BACKUP = ASSOC_ROOT + r"\AssocBackup"    # 逐个扩展名记下"抢默认"前的原值
ASSOC_PROGID_AUDIO = "ZhiJianPlayer.Audio"
ASSOC_PROGID_VIDEO = "ZhiJianPlayer.Video"
ASSOC_PROGID_IMAGE = "ZhiJianPlayer.Image"
ASSOC_PROGID_SUBTITLE = "ZhiJianPlayer.Subtitle"
#: 旧版单泛化 ProgID / 旧版 Capabilities 路径 —— 注册时清理、反注册时一并删掉
ASSOC_LEGACY_PROGIDS = ("ZhiJianPlayer.Media",)
ASSOC_LEGACY_CAPABILITIES = ("Software\\至简Player\\Capabilities",)

ASSOC_AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".wma",
                    ".ape", ".ac3", ".mka", ".dts")
ASSOC_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v",
                    ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".vob", ".rmvb")
ASSOC_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".avif", ".tif",
                    ".tiff", ".ico")
ASSOC_SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub")
#: (ProgID, 中文类型描述, 扩展名元组) —— 顺序即证据输出顺序
ASSOC_KINDS = (
    (ASSOC_PROGID_AUDIO, "音频文件", ASSOC_AUDIO_EXTS),
    (ASSOC_PROGID_VIDEO, "视频文件", ASSOC_VIDEO_EXTS),
    (ASSOC_PROGID_IMAGE, "图片文件", ASSOC_IMAGE_EXTS),
    (ASSOC_PROGID_SUBTITLE, "字幕文件", ASSOC_SUBTITLE_EXTS),
)

#: ProgID → assets 目录下的**专用文件图标**（多尺寸 ICO，随包分发）。
#: 由 scripts\05-make-icon.py（Pillow）生成：16/32/48/64/128/256 六尺寸。
ASSOC_ICON_FILES = {
    ASSOC_PROGID_AUDIO: "audio.ico",
    ASSOC_PROGID_VIDEO: "video.ico",
    ASSOC_PROGID_IMAGE: "image.ico",
    ASSOC_PROGID_SUBTITLE: "subtitle.ico",
}
#: 主程序图标（Capabilities / 找不到分类图标时的回落）
ASSOC_APP_ICON_FILE = "icon.ico"
#: 允许外部显式指定 assets 目录（自动化探针/测试用；正常安装**不需要**设置）
ASSOC_ICON_DIR_ENV = "ZHJ_ASSETS_DIR"


def assoc_ext_map() -> dict:
    """扩展名 → ProgID（唯一映射源；安装包/卸载器/脚本都从这里取）。"""
    out = {}
    for progid, _label, exts in ASSOC_KINDS:
        for ext in exts:
            out[ext] = progid
    return out


def assoc_all_exts() -> list:
    return list(assoc_ext_map().keys())


def assoc_progid_for(ext: str) -> str:
    return assoc_ext_map().get(str(ext or "").lower(), "")


def assoc_open_command(exe: str) -> str:
    r"""**严格双引号** + 绝对路径的打开命令（形如 "exe路径" "%1"）。

    ⚠️ 引号不是可选项：路径一旦含空格（C:\Temp\a b\至简Player.exe），
       没有引号时系统会把 C:\Temp\a 当 exe、b\至简Player.exe 当参数，
       直接启动失败 —— 这正是"选了也没反应/反复弹框"的一大来源。
    ⚠️ 必须**绝对路径**：相对路径写进注册表后工作目录是随机的，
       实测现网历史值就是 "output/至简Player-Portable-1.0.36/至简Player.exe"，
       永远找不到文件 ⇒ 系统只能继续弹"选择应用"。
    """
    exe = os.path.abspath(str(exe))
    if '"' in exe:
        raise ValueError("exe 路径含双引号，无法安全转义: %r" % exe)
    return '"%s" "%%1"' % exe


def assoc_default_icon(exe: str) -> str:
    exe = os.path.abspath(str(exe))
    return '"%s",0' % exe


def _candidate_asset_dirs(exe: str = "") -> list:
    """按优先级返回候选 assets 目录（绝对路径，去重保序）。

    ⚠️ **必须优先按"目标 exe 所在目录"解析**：scripts\\52-set-default-app.py 是在
       源码目录里运行的，却把 output\\便携版\\至简Player.exe 传进来 —— 如果只按当前
       进程解析（源码运行 ⇒ sys.frozen=False ⇒ 工程根 assets），写进注册表的就是
       开发机路径（D:\\AI-Project\\...）。用户把绿色版拷给别人/换个盘符后，
       42 个扩展名的图标立刻全丢。所以顺序是：
         ① 环境变量 ZHJ_ASSETS_DIR（探针/测试显式指定）
         ② **目标 exe 目录**：<exe>\\assets、<exe>\\_internal\\assets
         ③ PyInstaller 运行期：sys._MEIPASS\\assets
         ④ 冻结进程自身 exe 目录（同上两种布局）
         ⑤ 源码运行：工程根 assets（最后的兜底，仅开发环境会走到）
    """
    out = []
    env = os.environ.get(ASSOC_ICON_DIR_ENV, "").strip().strip('"')
    if env:
        out.append(os.path.abspath(env))
    if exe:
        exedir = os.path.dirname(os.path.abspath(str(exe)))
        out.append(os.path.join(exedir, "assets"))
        out.append(os.path.join(exedir, "_internal", "assets"))
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        out.append(os.path.join(meipass, "assets"))
    if getattr(sys, "frozen", False):
        exedir = os.path.dirname(os.path.abspath(sys.executable))
        out.append(os.path.join(exedir, "assets"))
        out.append(os.path.join(exedir, "_internal", "assets"))
    here = os.path.dirname(os.path.abspath(__file__))          # src/zhijian/platform
    out.append(os.path.abspath(os.path.join(here, "..", "..", "..", "assets")))
    seen, uniq = set(), []
    for p in out:
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen:
            seen.add(key)
            uniq.append(os.path.normpath(p))
    return uniq


def assoc_icon_dir(exe: str = "") -> str:
    """第一个真实存在的 assets 目录（证据输出用）；找不到返回空串。"""
    for d in _candidate_asset_dirs(exe):
        if os.path.isdir(d):
            return d
    return ""


def assoc_icon_path(filename: str, exe: str = "") -> str:
    """在候选 assets 目录里找一个图标文件；找不到返回空串（调用方回落 exe 图标）。"""
    name = os.path.basename(str(filename or ""))
    if not name:
        return ""
    for d in _candidate_asset_dirs(exe):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return os.path.abspath(p)
    return ""


def assoc_icon_map(exe: str = "") -> dict:
    """ProgID → 专用图标**绝对路径**（缺失的值为空串，调用方回落 exe 图标）。"""
    return {progid: assoc_icon_path(fn, exe) for progid, fn in ASSOC_ICON_FILES.items()}


def _icon_value(path: str) -> str:
    r"""注册表 DefaultIcon 取值格式：**严格双引号** + 绝对路径 + ",0"。

    引号不是可选项：路径含空格（C:\\Program Files\\...）时，不加引号系统会把
    路径截断成第一段，图标直接不显示 —— 与本文件 shell open command 的坑同源。
    """
    return '"%s",0' % os.path.abspath(str(path))


def assoc_app_icon_value(exe: str = "") -> str:
    """程序图标（Capabilities / ProgID\\Application\\ApplicationIcon）。

    优先 assets\\icon.ico；缺失时回落 exe 自身图标（旧版行为）。
    """
    p = assoc_icon_path(ASSOC_APP_ICON_FILE, exe)
    if p:
        return _icon_value(p)
    return assoc_default_icon(exe) if exe else ""


def assoc_default_icon_value(exe: str, progid: str, icons: dict | None = None) -> str:
    """某个 ProgID（类别）的 DefaultIcon 值：专用分类图标优先，回落 exe 图标。"""
    if icons is None:
        icons = assoc_icon_map(exe)
    path = icons.get(str(progid), "")
    if path:
        return _icon_value(path)
    return assoc_default_icon(exe)


def assoc_ext_icon_values(exe: str) -> dict:
    """扩展名 → DefaultIcon 值（42 项）。

    ⚠️ 这是**唯一映射源**：register_associations() 写它、scripts\\53-verify-icons.py
       核对它、安装包/卸载器也走同一份，避免"注册写一套、探针查另一套"。
    """
    icons = assoc_icon_map(exe)
    return {ext: assoc_default_icon_value(exe, progid, icons)
            for ext, progid in assoc_ext_map().items()}


def _icon_is_ours(value) -> bool:
    r"""判断一个 DefaultIcon 值是不是"我们写的"（反注册只删自己的，不碰用户自定义）。

    认两种：① assets\\{audio,video,image,subtitle,icon}.ico 的路径；
            ② 旧版把 DefaultIcon 指向 exe 自身的 "...,0" 值（ProgID 级由整树删除处理）。
    """
    if value is None:
        return False
    s = str(value).strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        s = s[1:end] if end > 0 else s[1:]
    s = s.split(",")[0].strip().strip('"')
    name = os.path.basename(s).lower()
    ours = {fn.lower() for fn in ASSOC_ICON_FILES.values()} | {ASSOC_APP_ICON_FILE.lower()}
    return name in ours


def _reg():
    import winreg
    return winreg


def _norm_exe(exe: str) -> str:
    return os.path.normpath(os.path.abspath(str(exe)))


def _set(root, path: str, name: str, value, kind=None) -> None:
    wr = _reg()
    if kind is None:
        kind = wr.REG_SZ
    with wr.CreateKeyEx(root, path, 0, wr.KEY_SET_VALUE) as k:
        wr.SetValueEx(k, name, 0, kind, value)


def _get(root, path: str, name: str = ""):
    """返回 (值, 类型)；键或值不存在返回 (None, None)。"""
    wr = _reg()
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_QUERY_VALUE) as k:
            return wr.QueryValueEx(k, name)
    except OSError:
        return None, None


def _values(root, path: str) -> dict:
    """枚举一个键下的**值**：{名字: 值}（不含子键）。键不存在返回 {}。"""
    wr = _reg()
    out = {}
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_QUERY_VALUE) as k:
            i = 0
            while True:
                try:
                    name, val, _kind = wr.EnumValue(k, i)
                except OSError:
                    break
                out[name] = val
                i += 1
    except OSError:
        return {}
    return out


def _values_typed(root, path: str) -> dict:
    """同上，但带类型（证据用）。"""
    wr = _reg()
    out = {}
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_QUERY_VALUE) as k:
            i = 0
            while True:
                try:
                    name, val, kind = wr.EnumValue(k, i)
                except OSError:
                    break
                out[name or "(默认)"] = {
                    "type": int(kind),
                    "value": val.decode("utf-8", "replace") if isinstance(val, bytes) else val,
                    "raw_bytes": list(val) if isinstance(val, bytes) else None,
                }
                i += 1
    except OSError:
        return {}
    return out


def _subkeys(root, path: str) -> list:
    wr = _reg()
    out = []
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_READ) as k:
            i = 0
            while True:
                try:
                    out.append(wr.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        return []
    return out


def _key_exists(root, path: str) -> bool:
    wr = _reg()
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_READ):
            return True
    except OSError:
        return False


def _delete_value(root, path: str, name: str) -> bool:
    wr = _reg()
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_SET_VALUE) as k:
            wr.DeleteValue(k, name)
        return True
    except OSError:
        return False


def _delete_tree(root, path: str) -> bool:
    """递归删键；键不存在也算成功（幂等）。"""
    wr = _reg()
    if not _key_exists(root, path):
        return True
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = wr.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(root, path + "\\" + sub)
        wr.DeleteKey(root, path)
        return True
    except OSError:
        return False


def _delete_userchoice(ext: str) -> str:
    """删掉 FileExts .<ext> UserChoice（带哈希保护，Win10/11 常常拒绝删除）。

    返回 "absent" / "deleted" / "denied" —— **如实回报**，不假装成功。
    """
    wr = _reg()
    path = (r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
            + "\\" + ext + r"\UserChoice")
    if not _key_exists(wr.HKEY_CURRENT_USER, path):
        return "absent"
    try:
        wr.DeleteKey(wr.HKEY_CURRENT_USER, path)
        return "deleted"
    except OSError:
        return "denied"


def _delete_userchoice_latest(ext: str) -> str:
    r"""删掉 FileExts .<ext> UserChoiceLatest（Win11 22H2+ 的**第二套**默认应用机制）。

    ⚠️ 为什么必须一起删：Win11 上有些扩展名（本机 .srt/.ass 实测）只认
       UserChoiceLatest —— 其中 ProgId\ProgId 仍指着旧播放器，
       于是 Classes 默认值 / 我们的 ProgID 全被绕过（双击仍用旧播放器，
       资源管理器也继续画旧图标）。只删 UserChoice 是不够的。
       它和 UserChoice 一样受哈希/ACL 保护，删不掉就如实回报。
    """
    wr = _reg()
    path = (r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
            + "\\" + ext + r"\UserChoiceLatest")
    if not _key_exists(wr.HKEY_CURRENT_USER, path):
        return "absent"
    try:
        wr.DeleteKey(wr.HKEY_CURRENT_USER, path)
        return "deleted"
    except OSError:
        return "denied"


def _backup_default(ext: str) -> str:
    """把"抢默认"之前的原值记到我们自己的备份键里（可回滚的关键）。

    ⚠️ 两条容易写错的地方，都踩过：
      ① **幂等**：已经备份过就不再覆盖 —— 否则第二次注册会把"我们自己的 ProgID"
         当成原值备份下来，反注册时反而把一个已经删掉的 ProgID 写回去；
      ② **历史残留**：现网 .mp3 的默认值是旧版的 ZhiJianPlayer.Media（也是"我们的"），
         这种要按"原本没有默认值"处理（记 __absent__），否则回滚会把旧 ProgID 复活。
    """
    wr = _reg()
    prev, _k = _get(wr.HKEY_CURRENT_USER, ASSOC_BACKUP + "\\" + ext, "PrevDefault")
    if prev is not None:
        return str(prev)
    cur, _kind = _get(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext, "")
    ours_all = {p for p, _l, _e in ASSOC_KINDS} | set(ASSOC_LEGACY_PROGIDS)
    marker = "__absent__" if (cur is None or str(cur) in ours_all) else str(cur)
    _set(wr.HKEY_CURRENT_USER, ASSOC_BACKUP + "\\" + ext, "PrevDefault", marker)
    return marker


def _restore_default(ext: str, ours: set) -> str:
    """按备份还原扩展名的默认值（只动我们自己抢过的那一个）。"""
    wr = _reg()
    cur, _kind = _get(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext, "")
    if cur is not None and str(cur) not in ours:
        return "skip(已被用户改成 %s)" % cur
    prev, _k = _get(wr.HKEY_CURRENT_USER, ASSOC_BACKUP + "\\" + ext, "PrevDefault")
    if prev is None:
        return "no-backup"
    if str(prev) == "__absent__":
        _delete_value(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext, "")
        return "restored(删除默认值)"
    _set(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext, "", str(prev))
    return "restored(%s)" % prev


def _backup_default_icon(ext: str) -> str:
    r"""把扩展名**原来的** DefaultIcon 记进 AssocBackup（反注册可逐项还原）。

    与 _backup_default 同一套幂等规则：
      · 已经备份过就不再覆盖（否则第二次注册会把我们自己的值当原值）；
      · 当前值如果是"我们写的图标"（例如上一版注册残留），按"原本没有"记 __absent__。
    """
    wr = _reg()
    prev, _k = _get(wr.HKEY_CURRENT_USER, ASSOC_BACKUP + "\\" + ext, "PrevDefaultIcon")
    if prev is not None:
        return str(prev)
    cur, _kind = _get(wr.HKEY_CURRENT_USER,
                      r"Software\Classes" + "\\" + ext + r"\DefaultIcon", "")
    marker = "__absent__" if (cur is None or _icon_is_ours(cur)) else str(cur)
    _set(wr.HKEY_CURRENT_USER, ASSOC_BACKUP + "\\" + ext, "PrevDefaultIcon", marker)
    return marker


def _delete_key_if_empty(root, path: str) -> bool:
    r"""删掉一个已经没有值、也没有子键的空键（例如 .mp3\DefaultIcon）。"""
    wr = _reg()
    if not _key_exists(root, path):
        return True
    if _values(root, path) or _subkeys(root, path):
        return False
    try:
        wr.DeleteKey(root, path)
        return True
    except OSError:
        return False


def _restore_default_icon(ext: str) -> str:
    """反注册：还原扩展名的 DefaultIcon（只动我们写的那个）。"""
    wr = _reg()
    path = r"Software\Classes" + "\\" + ext + r"\DefaultIcon"
    cur, _kind = _get(wr.HKEY_CURRENT_USER, path, "")
    if cur is not None and not _icon_is_ours(cur):
        return "skip(已被用户改成 %s)" % cur
    prev, _k = _get(wr.HKEY_CURRENT_USER, ASSOC_BACKUP + "\\" + ext, "PrevDefaultIcon")
    if prev is None:
        if cur is None:
            return "absent"
        _delete_value(wr.HKEY_CURRENT_USER, path, "")
        _delete_key_if_empty(wr.HKEY_CURRENT_USER, path)
        return "removed(无备份)"
    if str(prev) == "__absent__":
        _delete_value(wr.HKEY_CURRENT_USER, path, "")
        _delete_key_if_empty(wr.HKEY_CURRENT_USER, path)
        return "restored(删除默认值)"
    _set(wr.HKEY_CURRENT_USER, path, "", str(prev))
    return "restored(%s)" % prev


def _clean_legacy() -> dict:
    """清掉旧版单泛化 ProgID 与旧 Capabilities 路径（防重复/冲突项）。"""
    wr = _reg()
    out = {}
    for progid in ASSOC_LEGACY_PROGIDS:
        removed = _delete_tree(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + progid)
        out["progid:" + progid] = bool(removed)
        for ext in assoc_all_exts():
            _delete_value(wr.HKEY_CURRENT_USER,
                          r"Software\Classes" + "\\" + ext + r"\OpenWithProgids", progid)
    for caps in ASSOC_LEGACY_CAPABILITIES:
        out["caps:" + caps] = bool(_delete_tree(wr.HKEY_CURRENT_USER, caps))
    return out


def register_associations(exe: str, with_default: bool = True) -> dict:
    """把本程序注册为媒体文件的打开方式（**只写 HKCU**，幂等，可回滚）。

    with_default=True 时把 Classes .<ext> 的默认值指到我们的 ProgID
    （并在 AssocBackup 里记下原值），同时尝试删除 FileExts <ext> 的
    UserChoice / UserChoiceLatest（它们代表用户"始终使用"的历史选择；
    带哈希/ACL 保护，删不掉就如实回报）。

    图标：4 个 ProgID 的 DefaultIcon 分别指向 assets 目录下按类别生成的
    audio/video/image/subtitle.ico（绝对路径，解析顺序见 _candidate_asset_dirs），
    并为 42 个扩展名各写一份 DefaultIcon（可逐项核对）。

    返回逐项结果字典（探针/报告直接用它当证据）。
    """
    wr = _reg()
    exe_abs = _norm_exe(exe)
    rep = {"exe": exe_abs, "is_abs": os.path.isabs(exe_abs), "exists": os.path.isfile(exe_abs),
           "with_default": bool(with_default), "progids": {}, "exts": {},
           "legacy_cleanup": {}, "errors": []}
    if not rep["exists"]:
        rep["errors"].append("exe 不存在: %s" % exe_abs)
        return rep
    cmd = assoc_open_command(exe_abs)
    icon = assoc_default_icon(exe_abs)            # 兜底：exe 自身图标
    icons = assoc_icon_map(exe_abs)               # ProgID → 专用 ico 绝对路径
    ext_icons = assoc_ext_icon_values(exe_abs)    # 扩展名 → DefaultIcon 值（42 项）
    app_icon = assoc_app_icon_value(exe_abs)      # 程序图标（icon.ico 优先）
    rep["command"] = cmd
    rep["icon"] = icon
    rep["icons"] = icons
    rep["ext_icons"] = ext_icons
    rep["app_icon"] = app_icon
    rep["icon_dir"] = assoc_icon_dir(exe_abs)
    rep["missing_icons"] = [fn for progid, fn in ASSOC_ICON_FILES.items()
                            if not icons.get(progid)]
    rep["legacy_cleanup"] = _clean_legacy()
    ours = {p for p, _l, _e in ASSOC_KINDS}
    for progid, label, exts in ASSOC_KINDS:
        base = r"Software\Classes" + "\\" + progid
        desc = "%s %s" % (ASSOC_APP_NAME, label)
        icon_value = assoc_default_icon_value(exe_abs, progid, icons)
        try:
            _set(wr.HKEY_CURRENT_USER, base, "", desc)
            _set(wr.HKEY_CURRENT_USER, base, "FriendlyTypeName", desc)
            _set(wr.HKEY_CURRENT_USER, base, "InfoTip", "用%s打开%s" % (ASSOC_APP_NAME, label))
            _set(wr.HKEY_CURRENT_USER, base, "ApplicationName", ASSOC_APP_NAME)
            # ⚠️ DefaultIcon 用**分类专用图标**（音频/视频/图片/字幕各一枚），
            #    不再是四类共用一个 exe 图标 —— 这正是"视觉识别系统"的核心。
            _set(wr.HKEY_CURRENT_USER, base + r"\DefaultIcon", "", icon_value)
            _set(wr.HKEY_CURRENT_USER, base + r"\shell\open\command", "", cmd)
            app = base + r"\Application"
            _set(wr.HKEY_CURRENT_USER, app, "", ASSOC_APP_NAME)
            _set(wr.HKEY_CURRENT_USER, app, "ApplicationName", ASSOC_APP_NAME)
            _set(wr.HKEY_CURRENT_USER, app, "ApplicationDescription", "轻量本地媒体播放器")
            _set(wr.HKEY_CURRENT_USER, app, "ApplicationCompany", "ZhiJian")
            # ApplicationIcon 是"这个应用长什么样"，用主图标而不是分类图标
            _set(wr.HKEY_CURRENT_USER, app, "ApplicationIcon", app_icon or icon)
            rep["progids"][progid] = {"desc": desc, "exts": list(exts),
                                      "default_icon": icon_value}
        except OSError as exc:
            rep["errors"].append("%s: %s" % (progid, exc))
    # 打开方式列表（OpenWithProgids）
    for ext, progid in assoc_ext_map().items():
        item = {"progid": progid}
        try:
            _set(wr.HKEY_CURRENT_USER,
                 r"Software\Classes" + "\\" + ext + r"\OpenWithProgids", progid, b"",
                 wr.REG_NONE)
            item["openwith"] = True
        except OSError as exc:
            item["openwith"] = False
            rep["errors"].append("%s OpenWithProgids: %s" % (ext, exc))
        if with_default:
            try:
                item["prev_default"] = _backup_default(ext)
                _set(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext, "", progid)
                item["default_set"] = progid
            except OSError as exc:
                item["default_set"] = "failed: %s" % exc
                rep["errors"].append("%s default: %s" % (ext, exc))
            item["userchoice"] = _delete_userchoice(ext)
            item["userchoice_latest"] = _delete_userchoice_latest(ext)
        # 扩展名级 DefaultIcon：42 个扩展名各自指向"类别专用图标"。
        # ⚠️ 实测说明（2026-10，Win11 26200）：Shell 解析文件图标时**最终取的是
        #    ProgID 的 DefaultIcon**（本机用 AssocQueryString(ASSOCSTR_DEFAULTICON)
        #    对照验证：扩展名键与 ProgID 图标不同时，返回的是 ProgID 的那个）。
        #    这里仍然逐扩展名写入，原因：
        #      ① 任务要求"42 个扩展名的 DefaultIcon 值可逐项核对"，写入才能核对；
        #      ② 旧版 Shell / 部分第三方工具确实会读 .<ext>\DefaultIcon；
        #      ③ 无副作用：真正的类别区分同时由 4 个 ProgID 的 DefaultIcon 保证。
        try:
            item["prev_icon"] = _backup_default_icon(ext)
            ext_icon = ext_icons.get(ext, icon)
            _set(wr.HKEY_CURRENT_USER,
                 r"Software\Classes" + "\\" + ext + r"\DefaultIcon", "", ext_icon)
            item["default_icon"] = ext_icon
        except OSError as exc:
            item["default_icon"] = "failed: %s" % exc
            rep["errors"].append("%s DefaultIcon: %s" % (ext, exc))
        rep["exts"][ext] = item
    # Capabilities + RegisteredApplications（进"默认应用"设置页）
    try:
        _set(wr.HKEY_CURRENT_USER, ASSOC_CAPABILITIES, "ApplicationName", ASSOC_APP_NAME)
        _set(wr.HKEY_CURRENT_USER, ASSOC_CAPABILITIES, "ApplicationDescription",
             "轻量本地媒体播放器（视频 / 音频 / 图片 / 字幕）")
        _set(wr.HKEY_CURRENT_USER, ASSOC_CAPABILITIES, "ApplicationIcon", app_icon or icon)
        for ext, progid in assoc_ext_map().items():
            _set(wr.HKEY_CURRENT_USER, ASSOC_CAPABILITIES + r"\FileAssociations", ext, progid)
        _set(wr.HKEY_CURRENT_USER, r"Software\RegisteredApplications",
             ASSOC_REGISTERED_NAME, ASSOC_CAPABILITIES)
        rep["capabilities"] = ASSOC_CAPABILITIES
        rep["registered_applications"] = {ASSOC_REGISTERED_NAME: ASSOC_CAPABILITIES}
    except OSError as exc:
        rep["errors"].append("capabilities: %s" % exc)
    return rep


def unregister_associations() -> dict:
    """反注册：逐项还原（含默认值回滚、旧版残留清理），返回残留检查结果。"""
    wr = _reg()
    rep = {"removed_progids": {}, "exts": {}, "legacy_cleanup": {}, "errors": []}
    ours = {p for p, _l, _e in ASSOC_KINDS}
    for ext, progid in assoc_ext_map().items():
        item = {}
        _delete_value(wr.HKEY_CURRENT_USER,
                      r"Software\Classes" + "\\" + ext + r"\OpenWithProgids", progid)
        item["openwith_removed"] = progid
        item["default"] = _restore_default(ext, ours)
        item["default_icon"] = _restore_default_icon(ext)
        rep["exts"][ext] = item
    for progid, _label, _exts in ASSOC_KINDS:
        rep["removed_progids"][progid] = bool(
            _delete_tree(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + progid))
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, r"Software\RegisteredApplications", 0,
                        wr.KEY_SET_VALUE) as k:
            wr.DeleteValue(k, ASSOC_REGISTERED_NAME)
        rep["registered_applications_removed"] = True
    except OSError:
        rep["registered_applications_removed"] = False
    _delete_tree(wr.HKEY_CURRENT_USER, ASSOC_CAPABILITIES)
    _delete_tree(wr.HKEY_CURRENT_USER, ASSOC_BACKUP)
    try:
        if not _subkeys(wr.HKEY_CURRENT_USER, ASSOC_ROOT):
            wr.DeleteKey(wr.HKEY_CURRENT_USER, ASSOC_ROOT)
    except OSError:
        pass
    rep["legacy_cleanup"] = _clean_legacy()
    rep["residual"] = assoc_residuals()
    return rep


def assoc_residuals() -> list:
    """反注册后仍存在的"我们"的键（应为空）。"""
    wr = _reg()
    out = []
    for progid, _label, _exts in ASSOC_KINDS:
        if _key_exists(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + progid):
            out.append(r"Software\Classes" + "\\" + progid)
    for progid in ASSOC_LEGACY_PROGIDS:
        if _key_exists(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + progid):
            out.append(r"Software\Classes" + "\\" + progid)
    for path in (ASSOC_CAPABILITIES, ASSOC_BACKUP, ASSOC_ROOT):
        if _key_exists(wr.HKEY_CURRENT_USER, path):
            out.append(path)
    if _get(wr.HKEY_CURRENT_USER, r"Software\RegisteredApplications",
            ASSOC_REGISTERED_NAME)[0] is not None:
        out.append(r"Software\RegisteredApplications" + "\\" + ASSOC_REGISTERED_NAME)
    for ext, progid in assoc_ext_map().items():
        if progid in _values(wr.HKEY_CURRENT_USER,
                             r"Software\Classes" + "\\" + ext + r"\OpenWithProgids"):
            out.append("OpenWithProgids %s -> %s" % (ext, progid))
        if _get(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext, "")[0] == progid:
            out.append("Classes %s (default)" % ext)
        if _icon_is_ours(_get(wr.HKEY_CURRENT_USER,
                              r"Software\Classes" + "\\" + ext + r"\DefaultIcon", "")[0]):
            out.append("DefaultIcon %s" % ext)
    return out


def dump_associations(exe: str = "") -> dict:
    """把关联相关的注册表**原值**导出成字典（证据用：带类型与原始字节）。

    exe 可选：传入后额外给出"按该 exe 解析出来的专用图标绝对路径"，
    便于探针把**注册表实际值**与**期望值**逐项对照（scripts\\53-verify-icons.py）。
    """
    wr = _reg()
    d = {"RegisteredApplications": {}, "Capabilities": {}, "ProgIDs": {}, "Extensions": {},
         "AssocBackup": {}, "UserChoice": {}, "HKLM_ProgIDs": {}, "Icons": {},
         "ext_icons_expected": {}}
    d["RegisteredApplications"][ASSOC_REGISTERED_NAME] = _get(
        wr.HKEY_CURRENT_USER, r"Software\RegisteredApplications", ASSOC_REGISTERED_NAME)[0]
    d["Capabilities"]["subkeys"] = _subkeys(wr.HKEY_CURRENT_USER, ASSOC_CAPABILITIES)
    for name in ("ApplicationName", "ApplicationDescription", "ApplicationIcon"):
        d["Capabilities"][name] = _get(wr.HKEY_CURRENT_USER, ASSOC_CAPABILITIES, name)[0]
    d["Capabilities"]["FileAssociations"] = _values(
        wr.HKEY_CURRENT_USER, ASSOC_CAPABILITIES + r"\FileAssociations")
    for progid, _label, exts in ASSOC_KINDS:
        base = r"Software\Classes" + "\\" + progid
        d["ProgIDs"][progid] = {
            "(默认)": _get(wr.HKEY_CURRENT_USER, base, "")[0],
            "FriendlyTypeName": _get(wr.HKEY_CURRENT_USER, base, "FriendlyTypeName")[0],
            "InfoTip": _get(wr.HKEY_CURRENT_USER, base, "InfoTip")[0],
            "ApplicationName": _get(wr.HKEY_CURRENT_USER, base, "ApplicationName")[0],
            "DefaultIcon": _get(wr.HKEY_CURRENT_USER, base + r"\DefaultIcon", "")[0],
            "command": _get(wr.HKEY_CURRENT_USER, base + r"\shell\open\command", "")[0],
            "Application": _values(wr.HKEY_CURRENT_USER, base + r"\Application"),
            "exts": list(exts),
            "exists": _key_exists(wr.HKEY_CURRENT_USER, base),
        }
    d["Icons"] = assoc_icon_map(exe)
    d["app_icon"] = assoc_app_icon_value(exe) if exe else ""
    if exe:
        d["ext_icons_expected"] = assoc_ext_icon_values(exe)
    for ext, progid in assoc_ext_map().items():
        expected = d["ext_icons_expected"].get(ext, "")
        d["Extensions"][ext] = {
            "default": _get(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext, "")[0],
            "DefaultIcon": _get(wr.HKEY_CURRENT_USER,
                                r"Software\Classes" + "\\" + ext + r"\DefaultIcon")[0],
            "OpenWithProgids": _values_typed(
                wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext + r"\OpenWithProgids"),
            "expect_progid": progid,
            "expect_icon": expected,
        }
    d["AssocBackup"] = _values(wr.HKEY_CURRENT_USER, ASSOC_BACKUP)
    for ext in assoc_all_exts():
        d["UserChoice"][ext] = _get(
            wr.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts" + "\\"
            + ext + r"\UserChoice", "ProgId")[0]
    for progid, _label, _exts in ASSOC_KINDS:
        d["HKLM_ProgIDs"][progid] = _key_exists(
            wr.HKEY_LOCAL_MACHINE, r"Software\Classes" + "\\" + progid)
    return d


def association_status(exe: str = "") -> dict:
    """当前关联状态摘要（人读；raw 证据用 dump_associations()）。"""
    wr = _reg()
    st = {"registered_app": _get(wr.HKEY_CURRENT_USER, r"Software\RegisteredApplications",
                                 ASSOC_REGISTERED_NAME)[0], "progids": {}, "exts": {}}
    for progid, _label, _exts in ASSOC_KINDS:
        base = r"Software\Classes" + "\\" + progid
        st["progids"][progid] = {
            "command": _get(wr.HKEY_CURRENT_USER, base + r"\shell\open\command", "")[0],
            "icon": _get(wr.HKEY_CURRENT_USER, base + r"\DefaultIcon", "")[0],
            "FriendlyTypeName": _get(wr.HKEY_CURRENT_USER, base, "FriendlyTypeName")[0],
        }
    for ext, progid in assoc_ext_map().items():
        st["exts"][ext] = {
            "default": _get(wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext, "")[0],
            "icon": _get(wr.HKEY_CURRENT_USER,
                         r"Software\Classes" + "\\" + ext + r"\DefaultIcon", "")[0],
            "in_openwith": progid in _values(
                wr.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + ext + r"\OpenWithProgids"),
            "userchoice": _get(
                wr.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts" + "\\"
                + ext + r"\UserChoice", "ProgId")[0],
        }
    if exe:
        st["exe_matches"] = (st["progids"].get(ASSOC_PROGID_AUDIO, {}).get("command") ==
                             assoc_open_command(exe))
    return st
