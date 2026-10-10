"""真实 Win32 输入注入与窗口探测工具（自动化验收共用）。

只做三件事，但每件都必须"真"：
  1. SendInput 注入真实鼠标/键盘（**不用** 1990 年代的 mouse_event / keybd_event，
     它们绕过输入队列语义, 在 UIPI / 高 DPI / 远程会话下会静默失效）；
  2. 按"客户区面积最大"找主窗口（不是按标题 —— 本项目虽无浮动顶层窗口,
     但同一判据在别的程序上仍是唯一可靠的）；
  3. 截屏（含 DWM 阴影外的真实像素）。

⚠️ 注入前先隐藏**本进程自己的控制台窗口**, 否则它会盖住被测窗口的边缘,
   让"拉边框"实际点在控制台上 —— 典型的假失败。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

user32 = ctypes.WinDLL("user32", use_last_error=True) if IS_WINDOWS else None
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True) if IS_WINDOWS else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True) if IS_WINDOWS else None

# ---------------------------------------------------------------------------
# 结构体：一律用 ctypes.wintypes，不自己定义同形结构
#   （两个模块各绑一次 argtypes 会互相覆盖，是上一代产品最阴的 bug）
# ---------------------------------------------------------------------------


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_SCANCODE = 0x0008

VK = {
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "SPACE": 0x20, "ESC": 0x1B, "RETURN": 0x0D, "TAB": 0x09,
    "F": 0x46, "M": 0x4D, "L": 0x4C, "S": 0x53, "V": 0x56, "H": 0x48,
    "R": 0x52, "E": 0x45, "X": 0x58, "O": 0x4F,
    "1": 0x31, "2": 0x32, "3": 0x33,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "BACKSPACE": 0x08,
    "OEM_COMMA": 0xBC, "OEM_PERIOD": 0xBE,
    "OEM_4": 0xDB, "OEM_6": 0xDD, "OEM_MINUS": 0xBD, "OEM_PLUS": 0xBB,
    "CONTROL": 0x11, "SHIFT": 0x10, "MENU": 0x12,
}


def _bind():
    u = user32
    u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    u.SendInput.restype = wintypes.UINT
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u.SetCursorPos.restype = wintypes.BOOL
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.GetCursorPos.restype = wintypes.BOOL
    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                              wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetClientRect.restype = wintypes.BOOL
    u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u.ClientToScreen.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetForegroundWindow.argtypes = []
    u.GetForegroundWindow.restype = wintypes.HWND
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.AttachThreadInput.restype = wintypes.BOOL
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.restype = ctypes.c_void_p
    u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                               wintypes.UINT, wintypes.UINT]
    u.PeekMessageW.restype = wintypes.BOOL
    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.SetProcessDPIAware.argtypes = []
    u.SetProcessDPIAware.restype = wintypes.BOOL
    return u


_bind()


# ---------------------------------------------------------------------------
# 输入注入
# ---------------------------------------------------------------------------

def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        err = ctypes.get_last_error()
        raise OSError(f"GetCursorPos 失败 err={err}")
    return pt.x, pt.y


def move_to(x: int, y: int) -> None:
    if not user32.SetCursorPos(int(x), int(y)):
        raise OSError(f"SetCursorPos 失败 err={ctypes.get_last_error()}")


def _send(inputs: list[INPUT]) -> int:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise OSError(f"SendInput 只注入了 {sent}/{n} 条, err={ctypes.get_last_error()}")
    return sent


def _mouse_input(flags: int, dx=0, dy=0, data=0) -> INPUT:
    mi = MOUSEINPUT(dx, dy, data, flags, 0, None)
    return INPUT(type=INPUT_MOUSE, mi=mi)


def _key_input(vk: int, up: bool = False, extended: bool = False) -> INPUT:
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    ki = KEYBDINPUT(vk, 0, flags, 0, None)
    return INPUT(type=INPUT_KEYBOARD, ki=ki)


_EXTENDED = {VK["LEFT"], VK["RIGHT"], VK["UP"], VK["DOWN"], VK["PAGEUP"], VK["PAGEDOWN"],
             0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x2C}


def key_down(name: str) -> None:
    vk = VK[name.upper()] if name.upper() in VK else ord(name.upper()[0])
    _send([_key_input(vk, False, vk in _EXTENDED)])


def key_up(name: str) -> None:
    vk = VK[name.upper()] if name.upper() in VK else ord(name.upper()[0])
    _send([_key_input(vk, True, vk in _EXTENDED)])


def tap(name: str, hold_ms: int = 30) -> None:
    key_down(name)
    time.sleep(hold_ms / 1000.0)
    key_up(name)


def hold(name: str, ms: int) -> None:
    key_down(name)
    time.sleep(ms / 1000.0)
    key_up(name)


def click(x: int, y: int, button: str = "left", settle: float = 0.05) -> None:
    move_to(x, y)
    time.sleep(settle)
    down = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
    up = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
    _send([_mouse_input(down)])
    time.sleep(0.035)
    _send([_mouse_input(up)])


def double_click(x: int, y: int) -> None:
    move_to(x, y)
    time.sleep(0.05)
    for _ in range(2):
        _send([_mouse_input(MOUSEEVENTF_LEFTDOWN)])
        time.sleep(0.02)
        _send([_mouse_input(MOUSEEVENTF_LEFTUP)])
        time.sleep(0.05)


def wheel(delta: int, x: int | None = None, y: int | None = None) -> None:
    if x is not None and y is not None:
        move_to(x, y)
        time.sleep(0.03)
    _send([_mouse_input(MOUSEEVENTF_WHEEL, data=ctypes.c_ulong(delta & 0xFFFFFFFF).value)])


# ---------------------------------------------------------------------------
# 窗口查找
# ---------------------------------------------------------------------------

def list_windows(pid: int | None = None, min_w: int = 200, min_h: int = 150) -> list[dict]:
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if pid is not None and wpid.value != pid:
            return True
        cr = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(cr))
        cw, ch = cr.right - cr.left, cr.bottom - cr.top
        if cw < min_w or ch < min_h:
            return True
        wr = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        out.append({
            "hwnd": int(hwnd), "title": title, "pid": wpid.value,
            "client_w": cw, "client_h": ch, "area": cw * ch,
            "rect": (wr.left, wr.top, wr.right, wr.bottom),
        })
        return True

    user32.EnumWindows(cb, 0)
    out.sort(key=lambda d: -d["area"])
    return out


def find_main_window(title_contains: str | None = None, pid: int | None = None,
                     min_w: int = 600, min_h: int = 400) -> dict | None:
    """按**客户区面积最大**挑主窗口 —— 不要只看标题（EnumWindows 顺序不保证）。"""
    wins = list_windows(pid=pid, min_w=min_w, min_h=min_h)
    if title_contains:
        wins = [w for w in wins if title_contains in w["title"]] or wins
    return wins[0] if wins else None


def client_origin(hwnd: int) -> tuple[int, int]:
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt))
    return pt.x, pt.y


def client_to_screen(hwnd: int, cx: int, cy: int) -> tuple[int, int]:
    ox, oy = client_origin(hwnd)
    return ox + cx, oy + cy


SW_RESTORE = 9
SW_SHOW = 5


def _tid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    return int(user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid)) or 0)


def force_foreground(hwnd: int, attempts: int = 4) -> bool:
    """把窗口真正拉到前台。

    ⚠️ 非前台进程直接 SetForegroundWindow 会**静默失败**, 于是后面所有 SendInput
    按键都发到了别的窗口 —— 表现是"注入返回成功、被测程序毫无反应"。
    所以这里做三件事, 并且**每一步都读回校验**：
      1. AttachThreadInput 到当前前台线程 + 目标线程, 再 SetForegroundWindow；
      2. 不行就 SetWindowPos 到 TOPMOST 再弹回 NOTOPMOST（会触发前台切换）；
      3. 再不行就真的往标题栏点一下 —— 真实点击必然激活窗口（最土但最稳）。
    """
    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL

    for attempt in range(attempts):
        fg = user32.GetForegroundWindow()
        tid_fg = _tid_of(fg)
        tid_me = kernel32.GetCurrentThreadId()
        tid_t = _tid_of(hwnd)
        attached = []
        for t in (tid_fg, tid_t):
            if t and t != tid_me:
                if user32.AttachThreadInput(t, tid_me, True):
                    attached.append(t)
        try:
            user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
            user32.SetForegroundWindow(wintypes.HWND(hwnd))
            user32.BringWindowToTop(wintypes.HWND(hwnd))
            user32.SetFocus(wintypes.HWND(hwnd))
        finally:
            for t in attached:
                user32.AttachThreadInput(t, tid_me, False)
        if user32.GetForegroundWindow() == hwnd:
            return True

        if attempt == 1:
            HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
            SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
            user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE)
            user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_NOTOPMOST), 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE)
            time.sleep(0.15)
            if user32.GetForegroundWindow() == hwnd:
                return True

        if attempt >= 2:
            # 真实点击标题栏（避开任何按钮：取窗口左右各 1/3 处之外的空白带）
            r = wintypes.RECT()
            user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
            cx = (r.left + r.right) // 2
            cy = r.top + 12
            try:
                click(cx, cy)
            except Exception:
                pass
            time.sleep(0.25)
            if user32.GetForegroundWindow() == hwnd:
                return True
        time.sleep(0.12)
    return user32.GetForegroundWindow() == hwnd


def hide_own_console() -> None:
    # ⚠️ GetConsoleWindow 在 **kernel32**, 不在 user32
    kernel32.GetConsoleWindow.argtypes = []
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    h = kernel32.GetConsoleWindow()
    if h:
        user32.ShowWindow(h, 0)     # SW_HIDE


def pump(seconds: float) -> None:
    """在等待期间继续派发本线程的消息（注入放后台线程时主线程用）。"""
    end = time.time() + seconds
    msg = wintypes.MSG()
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.004)


def drag_in_thread(x0: int, y0: int, x1: int, y1: int, steps: int = 24,
                   settle: float = 0.012, button: str = "left") -> threading.Thread:
    """在**后台线程**里做拖拽。

    拖动会进系统模态循环, 它阻塞发起调用的那个线程直到按键松开；
    如果注入代码和事件循环在同一线程, 就永远走不到"松开"那一步 —— 直接死锁。
    """
    def worker():
        move_to(x0, y0)
        time.sleep(0.12)
        down = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
        up = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
        _send([_mouse_input(down)])
        time.sleep(0.10)
        for i in range(1, steps + 1):
            t = i / steps
            move_to(int(x0 + (x1 - x0) * t), int(y0 + (y1 - y0) * t))
            time.sleep(settle)
        time.sleep(0.08)
        _send([_mouse_input(up)])
        time.sleep(0.12)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    return th


def close_window(hwnd: int) -> None:
    WM_CLOSE = 0x0010
    user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0)


# ---------------------------------------------------------------------------
# 截屏（真实屏幕像素）
# ---------------------------------------------------------------------------

def _screen_grab():
    """抓**整个主屏**的物理像素图。

    ⚠️ QScreen.grabWindow(0, x, y, w, h) 吃的是**逻辑坐标**、吐的是物理像素
    （150% 缩放下 40x40 的请求会返回 60x60）。而 GetWindowRect 给的是物理像素。
    两者混用会让截图整体偏移 —— 所以这里整屏抓一次, 再按物理像素自己裁。
    """
    from PySide6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    pm = screen.grabWindow(0)
    return screen, pm


def grab_screen(x: int, y: int, w: int, h: int, path: str) -> str:
    screen, full = _screen_grab()
    dev = screen.devicePixelRatio() or 1.0
    # full 的物理尺寸
    fw, fh = full.width(), full.height()
    sx = int(round(x * dev / max(1e-6, dev)))   # GetWindowRect 已是物理像素
    pm = full.copy(int(x), int(y), int(w), int(h))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    pm.save(path, "PNG")
    return path


def grab_window(hwnd: int, path: str) -> str:
    r = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    return grab_screen(r.left, r.top, r.right - r.left, r.bottom - r.top, path)


def grab_window_client(hwnd: int, path: str, inset: int = 3) -> str:
    """只抓客户区（去掉 DWM 阴影 / 隐形边框）。

    ⚠️ `inset`：150% 缩放下 GetWindowRect 比**真正画出来的内容**大 2~3 物理像素
    （DWM 不可见边框 + 逻辑/物理坐标取整误差；实测内容高 1136 vs 声明 1140）。
    不内缩的话截图四周会带上一条桌面像素, 做视觉自审时会误判成"露白"。

    ⚠️⚠️ 这是**屏幕抓取**，抓到的是"该位置上屏幕显示的东西"——如果窗口没在前台
    （比如资源管理器刚被别的测试打开），抓回来的就是别人的窗口。
    留证截图请用 `grab_window_client_forced()`（走 PrintWindow，与 Z 序无关）。
    """
    r = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    ox, oy = client_origin(hwnd)
    cr = wintypes.RECT()
    user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(cr))
    return grab_screen(ox + inset, oy + inset,
                       max(1, cr.right - cr.left - inset * 2),
                       max(1, cr.bottom - cr.top - inset * 2), path)


def grab_window_client_forced(hwnd: int, path: str) -> str:
    """用 PrintWindow(PW_RENDERFULLCONTENT) 抓窗口自身 —— **不受 Z 序影响**。

    做视觉留证必须用这个：屏幕抓取拍到的是"该位置上屏幕显示的东西"，
    窗口没在前台时会把盖在上面的别的窗口拍进去
    （实测踩过：想拍播放器的设置面板，拍回来一张资源管理器，而且字节完全一致）。
    PW_RENDERFULLCONTENT(2) 是唯一能把 D3D/GL 合成内容也画出来的标志。
    """
    from PySide6.QtGui import QImage

    r = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    w, h = max(1, r.right - r.left), max(1, r.bottom - r.top)
    hdc = user32.GetWindowDC(wintypes.HWND(hwnd))
    gdi = ctypes.windll.gdi32

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h            # 负数 = 自上而下，省一次翻转
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0        # BI_RGB
    mem = gdi.CreateCompatibleDC(hdc)
    bits = ctypes.c_void_p()
    bmp = gdi.CreateDIBSection(hdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
    old = gdi.SelectObject(mem, bmp)
    PW_RENDERFULLCONTENT = 0x00000002
    ok = user32.PrintWindow(wintypes.HWND(hwnd), mem, PW_RENDERFULLCONTENT)
    img = None
    if ok and bits.value:
        buf = ctypes.string_at(bits.value, w * h * 4)
        img = QImage(buf, w, h, w * 4, QImage.Format_RGB32).copy()
        img.save(path)
    gdi.SelectObject(mem, old)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(mem)
    user32.ReleaseDC(wintypes.HWND(hwnd), hdc)
    if img is None:                        # 兜底：至少不会崩
        return grab_window_client(hwnd, path)
    return path


def print_window_rect(hwnd: int) -> dict:
    wr = wintypes.RECT()
    cr = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(wr))
    user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(cr))
    ox, oy = client_origin(hwnd)
    return {
        "window_rect": (wr.left, wr.top, wr.right, wr.bottom),
        "window_size": (wr.right - wr.left, wr.bottom - wr.top),
        "client_size": (cr.right - cr.left, cr.bottom - cr.top),
        "client_origin": (ox, oy),
    }


if __name__ == "__main__":
    hide_own_console()
    print("cursor:", cursor_pos())
    for w in list_windows()[:10]:
        print(w)
