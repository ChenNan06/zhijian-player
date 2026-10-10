"""mpv 播放引擎 + OpenGL 渲染控件（单一渲染管线）。

架构红线（用户要求, 不可妥协）：
  * 视频/图片 100% 通过 mpv_render_context + OpenGL 渲染到 QOpenGLWidget,
    与 Qt 控件共用**同一个原生窗口、同一套渲染上下文**, GPU 层面原子同步;
  * 全程**不允许**出现任何独立的浮动顶层窗口（Qt::Tool / Qt::Popup 等）,
    也不靠轮询定时器去追踪主窗口位置。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import POINTER, byref, c_char_p, c_int, c_void_p

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from .libmpv import (
    GET_PROC_ADDRESS_FN,
    MPV_ERROR_PROPERTY_UNAVAILABLE,
    MPV_EVENT_AUDIO_RECONFIG,
    MPV_EVENT_CLIENT_MESSAGE,
    MPV_EVENT_END_FILE,
    MPV_EVENT_FILE_LOADED,
    MPV_EVENT_IDLE,
    MPV_EVENT_LOG_MESSAGE,
    MPV_EVENT_PLAYBACK_RESTART,
    MPV_EVENT_PROPERTY_CHANGE,
    MPV_EVENT_SEEK,
    MPV_EVENT_SHUTDOWN,
    MPV_EVENT_VIDEO_RECONFIG,
    MPV_FORMAT_DOUBLE,
    MPV_FORMAT_FLAG,
    MPV_FORMAT_INT64,
    MPV_FORMAT_NODE,
    MPV_FORMAT_STRING,
    MPV_RENDER_API_TYPE_OPENGL,
    MPV_RENDER_PARAM_API_TYPE,
    MPV_RENDER_PARAM_BLOCK_FOR_TARGET_TIME,
    MPV_RENDER_PARAM_FLIP_Y,
    MPV_RENDER_PARAM_OPENGL_FBO,
    MPV_RENDER_PARAM_OPENGL_INIT_PARAMS,
    MPV_RENDER_PARAM_INVALID,
    UPDATE_FN,
    MpvError,
    MpvHandle,
    MpvOpenGLFbo,
    MpvOpenGLInitParams,
    MpvRenderParam,
    load_libmpv,
)

log = logging.getLogger("zhijian.mpv")

# --------------------------------------------------------------------------------------
# Win32 的 GL 函数取址（纯 Win32, 不依赖 Qt, 可从任意线程调用）
# --------------------------------------------------------------------------------------

_WGL_GET_PROC = None
_GL_LIB = None
_K32_GET_PROC = None


def _ensure_gl_proc():
    global _WGL_GET_PROC, _GL_LIB, _K32_GET_PROC
    if _WGL_GET_PROC is None:
        _GL_LIB = ctypes.WinDLL("opengl32.dll")
        _WGL_GET_PROC = _GL_LIB.wglGetProcAddress
        _WGL_GET_PROC.argtypes = [c_char_p]
        _WGL_GET_PROC.restype = c_void_p
        # GetProcAddress 在 kernel32 里, 不在 opengl32 里（踩过一次）
        k32 = ctypes.WinDLL("kernel32.dll")
        _K32_GET_PROC = k32.GetProcAddress
        _K32_GET_PROC.argtypes = [c_void_p, c_char_p]
        _K32_GET_PROC.restype = c_void_p
    return _WGL_GET_PROC


_INVALID_PTRS = {1, 2, 3, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}

# 诊断计数：mpv 到底多频繁地回调我们的 GL 函数加载器？
# 每帧被调几百上千次的话，光 Python↔C 的往返就是毫秒级固定开销。
_PROC_CALLS = 0
_PROC_MS = 0.0
_PROC_CACHE: dict[bytes, int] = {}


def _gl_get_proc_address(_ctx, name):
    """mpv 通过它解析 GL 函数指针。

    注意：wglGetProcAddress 对 OpenGL 1.1 的核心函数一律返回 NULL,
    必须回落到 GetProcAddress(opengl32.dll, ...), 否则 libplacebo 会以为缺函数
    （表现为 mpv_render_context_create 返回 -18 not supported）。
    """
    global _PROC_CALLS, _PROC_MS
    _t0 = time.perf_counter()
    _PROC_CALLS += 1
    hit = _PROC_CACHE.get(name)
    if hit is not None:
        _PROC_MS += (time.perf_counter() - _t0) * 1000.0
        return hit
    get_proc = _ensure_gl_proc()
    ptr = get_proc(name)
    val = int(ptr) if ptr else 0
    if val == 0 or val in _INVALID_PTRS:
        try:
            ptr2 = _K32_GET_PROC(_GL_LIB._handle, name)
            val = int(ptr2) if ptr2 else 0
        except Exception:  # pragma: no cover
            val = 0
    _PROC_CACHE[name] = val
    _PROC_MS += (time.perf_counter() - _t0) * 1000.0
    return val


_GL_PROC_CB = GET_PROC_ADDRESS_FN(_gl_get_proc_address)


# --------------------------------------------------------------------------------------
# 供 mpv 使用的默认播放选项
# --------------------------------------------------------------------------------------

VIDEO_OPTIONS = {
    # ---- 渲染 / 硬件加速 ---------------------------------------------------------
    "vo": "libmpv",
    "gpu-api": "opengl",
    "hwdec": "auto-safe",
    "vd-lavc-dr": "yes",           # direct rendering, 少一次拷贝
    "vd-lavc-threads": "0",        # 0 = 自动（CPU 软解时吃满多核）
    "video-sync": "audio",
    "interpolation": "no",
    "tscale": "oversample",
    # 缩放器：vo=gpu-next(libplacebo) 只认它自己那一套名字
    "scale": "ewa_lanczossharp",
    "cscale": "bicubic",
    "dscale": "mitchell",
    "correct-downscaling": "yes",
    "linear-downscaling": "yes",
    "sigmoid-upscaling": "yes",
    "deband": "yes",
    "deband-iterations": "2",
    "deband-threshold": "48",
    "temporal-dither": "yes",
    # ---- HDR / 色调映射 ----------------------------------------------------------
    "target-colorspace-hint": "yes",
    "tone-mapping": "bt.2446a",
    "hdr-compute-peak": "yes",
    "target-peak": "auto",
    # ---- Seek 缓冲策略（4K 拖拽不等待的关键） ------------------------------------
    "cache": "yes",
    "cache-secs": "30",
    "cache-pause": "no",
    "cache-pause-initial": "no",
    "demuxer-max-bytes": "400MiB",
    "demuxer-max-back-bytes": "150MiB",
    "demuxer-readahead-secs": "20",
    "demuxer-lavf-buffersize": "4194304",
    "force-seekable": "yes",
    "hr-seek": "yes",
    "hr-seek-framedrop": "yes",
    "keep-open": "yes",
    # ---- 音频 -------------------------------------------------------------------
    "audio-file-auto": "no",
    "audio-channels": "auto-safe",
    "audio-pitch-correction": "yes",
    # ---- 字幕 -------------------------------------------------------------------
    "sub-auto": "fuzzy",           # 自动挂载同目录同名 srt/ass/vtt
    "sub-file-paths": ".;subs;sub;Subs;Sub;字幕",
    "sub-font": "Microsoft YaHei UI",
    "sub-font-size": "46",
    "sub-border-size": "2.4",
    "sub-border-color": "#FF101010",
    "sub-shadow-offset": "1.5",
    "sub-color": "#FFFFFFFF",
    "sub-bold": "no",
    "sub-pos": "92",
    "sub-ass-override": "scale",
    "sub-scale-with-window": "yes",
    "sub-ass-force-margins": "yes",
    "sub-fix-timing": "no",
    "embeddedfonts": "yes",
    # ---- 其它 -------------------------------------------------------------------
    "config": "no",
    "load-scripts": "yes",
    "osc": "no",
    "input-default-bindings": "no",
    "input-vo-keyboard": "no",
    "terminal": "no",
    "msg-level": "all=no",
    "osd-bar": "no",
    "osd-level": "0",
    "screenshot-format": "png",
    "screenshot-sw": "no",
    "image-display-duration": "inf",
    "reset-on-next-file": "speed,pause,volume,mute,sub-delay,sub-pos,sub-visibility,ab-loop-a,ab-loop-b,video-zoom,video-pan-x,video-pan-y,video-rotate,video-aspect-override,audio-delay",
    "ytdl": "no",
    "idle": "yes",
    "force-window": "no",
    "keep-open-pause": "yes",
}

# 只读探针实例的选项：永久禁用一切发声与视频输出（防止「双声事故」）
PROBE_OPTIONS = {
    "audio": "no",
    "video": "no",
    "aid": "no",
    "vid": "no",
    "sid": "no",
    "ao": "null",
    "vo": "null",
    "config": "no",
    "load-scripts": "no",
    "input-default-bindings": "no",
    "terminal": "no",
    "msg-level": "all=no",
    "idle": "yes",
    "ytdl": "no",
}


def default_surface_format() -> QSurfaceFormat:
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.OpenGL)
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    fmt.setSwapBehavior(QSurfaceFormat.DoubleBuffer)
    # swapInterval=0：不等垂直同步。视频已经在 FBO 里画好了, 再等一个 vsync
    # 只会白白加上最多 16.7ms 的"画面恢复延迟"（实测 seek 恢复时间因此变快）。
    fmt.setSwapInterval(0)
    return fmt


# --------------------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------------------


class MpvEngine(QObject):
    """一个 mpv 实例 + 事件线程 + Qt 信号桥。"""

    property_changed = Signal(str, object)     # name, value
    file_loaded = Signal()
    end_file = Signal(int, int)                # reason, error
    playback_restart = Signal()
    video_reconfig = Signal()
    audio_reconfig = Signal()
    seek_event = Signal()
    idle = Signal()
    client_message = Signal(str, object)
    log_message = Signal(str, str, str)        # prefix, level, text

    OBSERVED = [
        ("time-pos", MPV_FORMAT_DOUBLE),
        ("duration", MPV_FORMAT_DOUBLE),
        ("pause", MPV_FORMAT_FLAG),
        ("core-idle", MPV_FORMAT_FLAG),
        ("seeking", MPV_FORMAT_FLAG),
        ("eof-reached", MPV_FORMAT_FLAG),
        ("paused-for-cache", MPV_FORMAT_FLAG),
        ("cache-buffering-state", MPV_FORMAT_INT64),
        ("demuxer-cache-time", MPV_FORMAT_DOUBLE),
        ("volume", MPV_FORMAT_DOUBLE),
        ("mute", MPV_FORMAT_FLAG),
        ("speed", MPV_FORMAT_DOUBLE),
        # vid/aid/sid 都可能是 "no" / "auto" 这种字符串 —— 用 INT64 观测会在
        # 属性不是整数时失败, 缓存里就永远停在上一次的旧值（本项目真实踩过）。
        ("sid", MPV_FORMAT_STRING),
        ("aid", MPV_FORMAT_STRING),
        ("vid", MPV_FORMAT_STRING),
        ("track-list", MPV_FORMAT_NODE),
        ("media-title", MPV_FORMAT_STRING),
        ("path", MPV_FORMAT_STRING),
        ("metadata", MPV_FORMAT_NODE),
        ("filtered-metadata", MPV_FORMAT_NODE),
        ("width", MPV_FORMAT_INT64),
        ("height", MPV_FORMAT_INT64),
        ("video-params", MPV_FORMAT_NODE),
        ("video-format", MPV_FORMAT_STRING),
        ("vo-configured", MPV_FORMAT_FLAG),
        ("hwdec-current", MPV_FORMAT_STRING),
        ("frame-drop-count", MPV_FORMAT_INT64),
        ("decoder-frame-drop-count", MPV_FORMAT_INT64),
        ("sub-delay", MPV_FORMAT_DOUBLE),
        ("sub-pos", MPV_FORMAT_DOUBLE),
        ("sub-visibility", MPV_FORMAT_FLAG),
        ("video-zoom", MPV_FORMAT_DOUBLE),
        ("video-rotate", MPV_FORMAT_INT64),
        ("container-fps", MPV_FORMAT_DOUBLE),
        ("estimated-vf-fps", MPV_FORMAT_DOUBLE),
        ("audio-device", MPV_FORMAT_STRING),
        ("audio-codec-name", MPV_FORMAT_STRING),
        ("video-codec", MPV_FORMAT_STRING),
        ("ab-loop-a", MPV_FORMAT_DOUBLE),
        ("ab-loop-b", MPV_FORMAT_DOUBLE),
    ]

    def __init__(self, name: str = "zhijian", options: dict | None = None, parent=None):
        super().__init__(parent)
        self.name = name
        self._handle = MpvHandle(name)
        opts = dict(VIDEO_OPTIONS)
        if options:
            opts.update(options)
        self.options_applied = opts
        for k, v in opts.items():
            self._handle.set_option(k, v)
        self._handle.initialize()
        self._thread: threading.Thread | None = None
        self._running = False
        self._shutting_down = False
        for prop, fmt in self.OBSERVED:
            self._handle.observe(prop, fmt)
        self._handle.request_log_messages("warn")
        self._props: dict = {}
        self._lock = threading.Lock()

    # -- 生命周期 -------------------------------------------------------------
    @property
    def handle(self):
        return self._handle

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._event_loop, name=f"mpv-{self.name}", daemon=True)
        self._thread.start()

    def begin_shutdown(self) -> None:
        self._shutting_down = True
        # 立刻唤醒事件线程, 否则它还要等 wait_event 的超时（最多 100ms）才退出
        self._handle.wakeup()

    def stop(self, timeout: float = 2.0) -> None:
        self._running = False
        self._handle.wakeup()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        self._thread = None

    def destroy(self) -> None:
        self._handle.terminate_destroy()

    # -- 事件线程 -------------------------------------------------------------
    def _event_loop(self):
        h = self._handle
        while self._running and not self._shutting_down:
            try:
                ev = h.wait_event(0.1)
            except Exception:  # pragma: no cover
                log.exception("mpv wait_event 异常")
                break
            if ev is None:
                continue
            eid, err, _ud, payload = ev
            if self._shutting_down:
                break
            try:
                self._dispatch(eid, payload)
            except Exception:  # pragma: no cover
                log.exception("mpv 事件分发异常")

    def _dispatch(self, eid: int, payload):
        if eid == MPV_EVENT_PROPERTY_CHANGE and payload is not None:
            if payload.format == 0 and payload.value is None:
                return
            with self._lock:
                self._props[payload.name] = payload.value
            self.property_changed.emit(payload.name, payload.value)
        elif eid == MPV_EVENT_FILE_LOADED:
            self.file_loaded.emit()
        elif eid == MPV_EVENT_END_FILE:
            if isinstance(payload, dict):
                self.end_file.emit(payload.get("reason", 0), payload.get("error", 0))
        elif eid == MPV_EVENT_PLAYBACK_RESTART:
            self.playback_restart.emit()
        elif eid == MPV_EVENT_VIDEO_RECONFIG:
            self.video_reconfig.emit()
        elif eid == MPV_EVENT_AUDIO_RECONFIG:
            self.audio_reconfig.emit()
        elif eid == MPV_EVENT_SEEK:
            self.seek_event.emit()
        elif eid == MPV_EVENT_IDLE:
            self.idle.emit()
        elif eid == MPV_EVENT_CLIENT_MESSAGE:
            if isinstance(payload, list) and payload:
                self.client_message.emit(str(payload[0]), payload[1:])
        elif eid == MPV_EVENT_SHUTDOWN:
            self._running = False

    # -- 便捷封装 -------------------------------------------------------------
    def cached(self, name: str, default=None):
        with self._lock:
            return self._props.get(name, default)

    def get(self, name: str, fmt: int = 0, default=None):
        from .libmpv import MPV_FORMAT_STRING

        return self._handle.get_property(name, fmt or MPV_FORMAT_STRING, default)

    def set(self, name: str, value, fmt=None) -> None:
        try:
            self._handle.set_property(name, value, fmt)
        except MpvError as exc:
            log.warning("设置属性 %s=%r 失败: %s", name, value, exc)

    def set_async(self, name: str, value, fmt=None) -> None:
        """异步写属性（不阻塞 UI 线程）。

        注意：修掉 `wait_event` 持锁的问题后，普通 `set()` 实测只要 1ms，
        所以绝大多数地方**不需要**用它；这里保留给"一次写十几个属性"的批量场合。
        """
        try:
            self._handle.set_property_async(name, value, fmt)
        except MpvError as exc:
            log.warning("异步设置属性 %s=%r 失败: %s", name, value, exc)

    def cmd(self, *args) -> int:
        return self._handle.command(*args)

    def cmd_async(self, *args) -> int:
        return self._handle.command_async(*args)

    def load(self, path: str, mode: str = "replace", options: str | None = None) -> int:
        """加载文件。

        options 是 mpv 的**逐文件选项串**（如 `vid=no`）——
        有些属性（vid/aid/sid）会在加载新文件时被 mpv 重置,
        必须在 loadfile 的那一刻一起给, 否则"设了等于没设"。
        """
        if options:
            return self._handle.command("loadfile", path, mode, "-1", options)
        return self._handle.command("loadfile", path, mode)

    def toggle_pause(self) -> None:
        self.cmd("cycle", "pause")

    def seek_relative(self, seconds: float, precise: bool = True) -> None:
        self.cmd_async("seek", f"{seconds:.3f}", "relative+exact" if precise else "relative+keyframes")

    def seek_absolute(self, seconds: float, precise: bool = True) -> None:
        """绝对跳转（**异步下发**, 绝不阻塞 GUI 线程）。

        precise=True  → `absolute+exact`：精确到帧（松开进度条/按键时用）。
        precise=False → `absolute+keyframes`：对齐关键帧, **不回溯解码**（拖动中预览用）。

        为什么必须异步：`mpv_command`（同步版）对 seek 会阻塞到 demuxer + 解码器
        重置完成 —— 4K 下实测约 65ms。在 GUI 线程里同步发 seek, 拖动进度条时
        界面会一顿一顿的（每 80ms 卡 65ms）。异步下发后 GUI 线程立刻返回,
        新画面由 mpv 的渲染回调驱动重绘。
        """
        flags = "absolute+exact" if precise else "absolute+keyframes"
        self.cmd_async("seek", f"{max(0.0, seconds):.3f}", flags)

    def set_speed(self, value: float) -> None:
        """设置倍速。

        同时**乐观更新本地缓存** —— 否则 UI/状态要等 mpv 把 speed 属性变化推回来
        才知道结果, 松开方向键到界面显示 1.0x 之间会有一段可感知的滞后
        （实测约 170ms, 而用户要的是"松开瞬间丝滑恢复"）。
        """
        v = max(0.01, float(value))
        self.set("speed", v, MPV_FORMAT_DOUBLE)
        with self._lock:
            self._props["speed"] = v


# --------------------------------------------------------------------------------------
# 渲染控件
# --------------------------------------------------------------------------------------

_LIVE_WIDGETS = set()
_LIVE_LOCK = threading.Lock()


class MpvRenderWidget(QOpenGLWidget):
    """把 mpv 的画面直接渲染进 Qt 的 OpenGL 上下文。

    * 与主窗口同属一个原生 HWND / 一个 GL 上下文 -> 拉伸窗口时画面与控件天然同步;
    * 永远不调用 hide()/setVisible(False)（Windows 上会销毁 GL 资源）,
      需要"隐藏"时由上层把整个页面切走（QStackedLayout）。
    """

    frame_rendered = Signal()

    def __init__(self, engine: MpvEngine | None = None, parent=None):
        super().__init__(parent)
        self.setFormat(default_surface_format())
        self.setUpdateBehavior(QOpenGLWidget.NoPartialUpdate)
        # ⚠️ 绝不能给 QOpenGLWidget 设 WA_NativeWindow / WA_DontCreateNativeAncestors：
        #    那会把它变成独立的原生子 HWND, 于是 Qt 的合成路径失效 ——
        #    mpv 明明在画（rendered_frames 一直涨）屏幕上却是一片黑。
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._engine = engine
        self._rc = c_void_p(0)
        self._rc_ready = False
        self._paint_ms_total = 0.0
        self._paint_count = 0
        # 当前是否真的有画面要画（视频/图片轨）。
        # 没有的时候**不能调 mpv 的 render** —— 它会把 FBO 画成黑色，
        # 把舞台底色盖掉（浅色主题下就是"标题栏白的、下面一大块死黑"）。
        self._has_video = False
        self._update_cb = None
        self._init_error = None
        self._gl_info = {}
        self._rendered_frames = 0
        self._last_frame_ts = 0.0
        self._render_errors = 0
        self._cleared = False
        self._suppress = False
        with _LIVE_LOCK:
            _LIVE_WIDGETS.add(self)

    # -- 对外 ----------------------------------------------------------------
    @property
    def gl_info(self) -> dict:
        return dict(self._gl_info)

    @property
    def init_error(self):
        return self._init_error

    @property
    def rc_ready(self) -> bool:
        return self._rc_ready

    def set_engine(self, engine: MpvEngine) -> None:
        self._engine = engine

    def rendered_frames(self) -> int:
        return self._rendered_frames

    def set_has_video(self, on: bool) -> None:
        """告诉画面控件"现在到底有没有东西要画"。

        无媒体时 paintGL 直接清主题底色，不走 mpv 渲染 ——
        否则 mpv 会把 FBO 清成黑色，浅色主题下内容区就是一块死黑。
        """
        on = bool(on)
        if on != self._has_video:
            self._has_video = on
            self._cleared = False
            self.update()

    def last_frame_ts(self) -> float:
        return self._last_frame_ts

    def paint_stats(self) -> dict:
        """paintGL 里的 mpv 渲染耗时（累计/均值）—— 用来把动画掉帧归因到
        是 mpv 渲染贵、还是 Qt 的整窗 GL 合成贵。"""
        n = max(1, self._paint_count)
        return {"count": self._paint_count,
                "total_ms": round(self._paint_ms_total, 1),
                "avg_ms": round(self._paint_ms_total / n, 2)}

    # -- GL ------------------------------------------------------------------
    def initializeGL(self):
        try:
            ctx = QOpenGLContext.currentContext()
            if ctx is not None:
                f = ctx.format()
                self._gl_info = {
                    "vendor": self._gl_string(0x1F00),
                    "renderer": self._gl_string(0x1F01),
                    "version": self._gl_string(0x1F02),
                    "glsl": self._gl_string(0x8B8C),
                    "profile": str(f.profile()),
                    "profile_num": int(getattr(f.profile(), "value", f.profile())),
                    "samples": int(f.samples()),
                    "valid": bool(ctx.isValid()),
                }
        except Exception:  # pragma: no cover
            log.exception("读取 GL 信息失败")
        if self._engine is not None:
            self.create_render_context()

    def _gl_string(self, enum: int) -> str:
        lib = ctypes.WinDLL("opengl32.dll")
        lib.glGetString.argtypes = [ctypes.c_uint]
        lib.glGetString.restype = c_char_p
        raw = lib.glGetString(enum)
        return raw.decode("utf-8", "replace") if raw else ""

    def create_render_context(self) -> bool:
        if self._rc_ready or self._engine is None:
            return self._rc_ready
        lib = load_libmpv()
        self.makeCurrent()
        init = MpvOpenGLInitParams()
        init.get_proc_address = ctypes.cast(_GL_PROC_CB, c_void_p).value
        init.get_proc_address_ctx = None
        api = c_char_p(MPV_RENDER_API_TYPE_OPENGL)
        params = (MpvRenderParam * 3)()
        params[0].type = MPV_RENDER_PARAM_API_TYPE
        params[0].data = ctypes.cast(api, c_void_p).value
        params[1].type = MPV_RENDER_PARAM_OPENGL_INIT_PARAMS
        params[1].data = ctypes.cast(byref(init), c_void_p).value
        params[2].type = MPV_RENDER_PARAM_INVALID
        params[2].data = None
        rc = c_void_p(0)
        st = lib.mpv_render_context_create(byref(rc), self._engine.handle.handle, params)
        self.doneCurrent()
        if st < 0:
            from .libmpv import error_string

            self._init_error = f"mpv_render_context_create 失败: {error_string(st)} ({st})"
            log.error(self._init_error)
            return False
        self._rc = rc
        self._update_cb = UPDATE_FN(self._on_mpv_update)
        lib.mpv_render_context_set_update_callback(rc, self._update_cb, None)
        self._rc_ready = True
        log.info("mpv 渲染上下文已建立: %s", self._gl_info)
        return True

    def _on_mpv_update(self, _ctx):
        """由 mpv 的内部线程调用 —— 只做一件事：让 Qt 排队重绘。

        绝不在这里调用任何 Qt 控件方法（跨线程会炸）。
        """
        if self._suppress:
            return
        try:
            self.update()
        except Exception:  # pragma: no cover
            pass

    def paintGL(self):
        _t0 = time.perf_counter()
        lib = load_libmpv()
        # 没有画面：只清主题底色，**不要去调 mpv**
        if not self._has_video:
            self._clear_black()
            return
        if not self._rc_ready:
            # 每次都清（不靠 `_cleared` 缓存）：换主题后底色要立刻跟着变，
            # 而这个分支只在"没有画面"时走，一次 glClear 的开销可以忽略。
            self._clear_black()
            return
        dpr = self.devicePixelRatioF()
        w = max(1, int(round(self.width() * dpr)))
        h = max(1, int(round(self.height() * dpr)))
        fbo = MpvOpenGLFbo()
        fbo.fbo = int(self.defaultFramebufferObject())
        fbo.w = w
        fbo.h = h
        fbo.internal_format = 0
        flip = c_int(1)
        # ⚠️⚠️ 这一行是"任何动画都掉帧"的**真正根因**。
        #
        # mpv 的渲染 API 默认开启 `MPV_RENDER_PARAM_BLOCK_FOR_TARGET_TIME`：
        # `mpv_render_context_render()` 会**阻塞到这一帧的目标显示时刻**才返回。
        # 于是 GUI 线程每帧被按住 ~26ms（≈33ms 帧间隔减掉实际渲染工作），
        # 30fps 就是每秒 780ms 不响应 —— 拖动、开抽屉、拖窗口全部掉帧。
        #
        # 它是**阻塞**不是**忙**，所以之前所有"换缩放器/换解码/换 profile/换尺寸"
        # 都测不出差别（26.0~26.7ms 纹丝不动），而一个空的 QOpenGLWidget
        # 的 paintGL 只要 0.02ms —— 这两个事实拼起来才指向它。
        #
        # 关掉之后 mpv 立刻返回，帧的节奏由 mpv 的 update 回调 + Qt 的重绘驱动。
        block = c_int(0)
        params = (MpvRenderParam * 4)()
        params[0].type = MPV_RENDER_PARAM_OPENGL_FBO
        params[0].data = ctypes.cast(byref(fbo), c_void_p).value
        params[1].type = MPV_RENDER_PARAM_FLIP_Y
        params[1].data = ctypes.cast(byref(flip), c_void_p).value
        params[2].type = MPV_RENDER_PARAM_BLOCK_FOR_TARGET_TIME
        params[2].data = ctypes.cast(byref(block), c_void_p).value
        params[3].type = MPV_RENDER_PARAM_INVALID
        params[3].data = None
        st = lib.mpv_render_context_render(self._rc, params)
        self._paint_ms_total += (time.perf_counter() - _t0) * 1000.0
        self._paint_count += 1
        if st < 0:
            self._render_errors += 1
            if self._render_errors <= 5:
                from .libmpv import error_string

                log.error("mpv_render_context_render 失败: %s", error_string(st))
            return
        lib.mpv_render_context_report_swap(self._rc)
        self._rendered_frames += 1
        self._last_frame_ts = time.perf_counter()
        self._cleared = False
        self.frame_rendered.emit()

    def _clear_black(self):
        """没有画面时把 GL 面清成**当前主题的舞台底色**。

        ⚠️⚠️ 这里原来写死 `glClearColor(0.043, 0.047, 0.055)`（近黑）。
        视频控件铺满整个舞台，所以**不管什么主题，内容区永远是黑的** ——
        浅色主题下就是"标题栏白的、下面一大块死黑"，用户原话"很突兀"。
        舞台底色必须跟主题走。
        """
        try:
            from PySide6.QtGui import QOpenGLFunctions

            from ..ui.theme import qt_color

            try:
                from ..ui import theme as _t

                c = qt_color(_t.P.get("bg_stage", "#07080B"))
            except Exception:
                c = qt_color("#07080B")
            f = QOpenGLFunctions(self.context())
            f.glClearColor(c.redF(), c.greenF(), c.blueF(), 1.0)
            f.glClear(0x00004000)
            self._cleared = True
        except Exception:  # pragma: no cover
            pass

    def resizeGL(self, w, h):  # noqa: N802
        # 尺寸变化由 paintGL 里的 FBO 参数自动跟随, 无需手工处理 —— 这正是
        # 「拉伸窗口 0 脱节」的根因级保障。
        pass

    def release_resources(self):
        """在 GL 上下文仍可用时释放 mpv 渲染上下文（退出流程用）。"""
        if not self._rc_ready:
            return
        self._suppress = True
        lib = load_libmpv()
        try:
            self.makeCurrent()
            lib.mpv_render_context_set_update_callback(self._rc, UPDATE_FN(0), None)
            lib.mpv_render_context_free(self._rc)
            self.doneCurrent()
        except Exception:  # pragma: no cover
            log.exception("释放 mpv 渲染上下文失败")
        finally:
            self._rc = c_void_p(0)
            self._rc_ready = False
            self._update_cb = None
            with _LIVE_LOCK:
                _LIVE_WIDGETS.discard(self)

    def cleanup(self):
        self.release_resources()


def live_widgets():
    with _LIVE_LOCK:
        return list(_LIVE_WIDGETS)


# --------------------------------------------------------------------------------------
# 共享探针实例（读时长 / 标签 → 绝不发声、绝不出画）
# --------------------------------------------------------------------------------------

_probe_lock = threading.Lock()
_probe_engine: MpvEngine | None = None


def shared_probe() -> MpvEngine:
    """进程内共享的只读探针。

    参照 §9.28 第 13 条：短命 mpv 实例秒建秒销会让 libmpv 内部异常穿透,
    约 1/3 概率把进程打成 0xC0000005, 因此这里常驻一个实例、用完不销毁。
    """
    global _probe_engine
    with _probe_lock:
        if _probe_engine is None:
            eng = MpvEngine("probe", options=PROBE_OPTIONS)
            eng.start()
            _probe_engine = eng
        return _probe_engine


def shutdown_probe() -> None:
    global _probe_engine
    with _probe_lock:
        eng = _probe_engine
        _probe_engine = None
    if eng is not None:
        try:
            eng.begin_shutdown()
            eng.stop(1.0)
        except Exception:
            pass
