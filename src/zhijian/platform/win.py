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


def enable_rounded_corners(win) -> None:
    """Win11 原生圆角（无边框窗口必须显式开，否则是直角）。"""
    if not IS_WINDOWS:
        return
    try:
        global _dwmapi
        if _dwmapi is None:
            _dwmapi = ctypes.WinDLL("dwmapi")
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        val = ctypes.c_int(DWMWCP_ROUND)
        _dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                  ctypes.c_void_p, wintypes.DWORD]
        _dwmapi.DwmSetWindowAttribute(wintypes.HWND(_hwnd(win)), DWMWA_WINDOW_CORNER_PREFERENCE,
                                      ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        log.debug("设置 Win11 圆角失败", exc_info=True)


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
