"""libmpv 的 ctypes 绑定。

只覆盖「至简Player」实际用到的客户端 API（client.h）与渲染 API（render.h / render_gl.h）,
不引入任何第三方 mpv 绑定包, 以便 PyInstaller 打包时可完全自控。

设计要点（依据 AI-KNOWLEDGE-BASE §9.28 的踩坑记录）：
  * 每条函数都显式声明 argtypes / restype —— ctypes 默认把 Python int 当 32 位传,
    会把 64 位句柄截断, 造成静默失败或 0xC0000005 访问冲突。
  * 结构体一律用 ctypes.wintypes, 不自己定义同形结构（避免两个模块互相覆盖 argtypes）。
  * mpv_get_property(MPV_FORMAT_NODE) 必须用 mpv_free_node_contents 释放,
    绝不能用 mpv_free（那是释放栈地址 = 堆损坏）。
  * mpv_get_property(MPV_FORMAT_STRING) 返回的 char* 必须用 mpv_free 释放。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    Union,
    byref,
    c_char_p,
    c_double,
    c_int,
    c_int64,
    c_size_t,
    c_uint64,
    c_void_p,
)

# --------------------------------------------------------------------------------------
# 常量（与 mpv/client.h、mpv/render.h 逐一对照抄录, 不要凭记忆改）
# --------------------------------------------------------------------------------------

# enum mpv_format
MPV_FORMAT_NONE = 0
MPV_FORMAT_STRING = 1
MPV_FORMAT_OSD_STRING = 2
MPV_FORMAT_FLAG = 3
MPV_FORMAT_INT64 = 4
MPV_FORMAT_DOUBLE = 5
MPV_FORMAT_NODE = 6
MPV_FORMAT_NODE_ARRAY = 7
MPV_FORMAT_NODE_MAP = 8
MPV_FORMAT_BYTE_ARRAY = 9

# enum mpv_event_id
MPV_EVENT_NONE = 0
MPV_EVENT_SHUTDOWN = 1
MPV_EVENT_LOG_MESSAGE = 2
MPV_EVENT_GET_PROPERTY_REPLY = 3
MPV_EVENT_SET_PROPERTY_REPLY = 4
MPV_EVENT_COMMAND_REPLY = 5
MPV_EVENT_START_FILE = 6
MPV_EVENT_END_FILE = 7
MPV_EVENT_FILE_LOADED = 8
MPV_EVENT_IDLE = 11
MPV_EVENT_TICK = 14
MPV_EVENT_CLIENT_MESSAGE = 16
MPV_EVENT_VIDEO_RECONFIG = 17
MPV_EVENT_AUDIO_RECONFIG = 18
MPV_EVENT_SEEK = 20
MPV_EVENT_PLAYBACK_RESTART = 21
MPV_EVENT_PROPERTY_CHANGE = 22
MPV_EVENT_QUEUE_OVERFLOW = 24
MPV_EVENT_HOOK = 25

# enum mpv_end_file_reason
MPV_END_FILE_REASON_EOF = 0
MPV_END_FILE_REASON_STOP = 2
MPV_END_FILE_REASON_QUIT = 3
MPV_END_FILE_REASON_ERROR = 4
MPV_END_FILE_REASON_REDIRECT = 5

# enum mpv_render_param_type
MPV_RENDER_PARAM_INVALID = 0
MPV_RENDER_PARAM_API_TYPE = 1
MPV_RENDER_PARAM_OPENGL_INIT_PARAMS = 2
MPV_RENDER_PARAM_OPENGL_FBO = 3
MPV_RENDER_PARAM_FLIP_Y = 4
MPV_RENDER_PARAM_DEPTH = 5
MPV_RENDER_PARAM_ICC_PROFILE = 6
MPV_RENDER_PARAM_AMBIENT_LIGHT = 7
MPV_RENDER_PARAM_ADVANCED_CONTROL = 10
MPV_RENDER_PARAM_NEXT_FRAME_INFO = 11
MPV_RENDER_PARAM_BLOCK_FOR_TARGET_TIME = 12
MPV_RENDER_PARAM_SKIP_RENDERING = 13

MPV_RENDER_API_TYPE_OPENGL = b"opengl"

MPV_RENDER_UPDATE_FRAME = 1 << 0

# mpv_error
MPV_ERROR_SUCCESS = 0
MPV_ERROR_PROPERTY_UNAVAILABLE = -10


# --------------------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------------------


class MpvNodeList(Structure):
    pass


class MpvByteArray(Structure):
    _fields_ = [("data", c_void_p), ("size", c_size_t)]


class MpvNode(Structure):
    pass


class _MpvNodeUnion(Union):
    _fields_ = [
        ("string", c_char_p),
        ("flag", c_int),
        ("int64", c_int64),
        ("double_", c_double),
        ("list", POINTER(MpvNodeList)),
        ("ba", POINTER(MpvByteArray)),
    ]


MpvNode._fields_ = [("u", _MpvNodeUnion), ("format", c_int)]

MpvNodeList._fields_ = [
    ("num", c_int),
    ("values", POINTER(MpvNode)),
    ("keys", POINTER(c_char_p)),
]


class MpvEventProperty(Structure):
    _fields_ = [("name", c_char_p), ("format", c_int), ("data", c_void_p)]


class MpvEventEndFile(Structure):
    _fields_ = [
        ("reason", c_int),
        ("error", c_int),
        ("playlist_entry_id", c_int64),
        ("playlist_insert_id", c_int64),
        ("playlist_insert_num_entries", c_int),
    ]


class MpvEvent(Structure):
    _fields_ = [
        ("event_id", c_int),
        ("error", c_int),
        ("reply_userdata", c_uint64),
        ("data", c_void_p),
    ]


class MpvRenderParam(Structure):
    _fields_ = [("type", c_int), ("data", c_void_p)]


class MpvOpenGLInitParams(Structure):
    _fields_ = [("get_proc_address", c_void_p), ("get_proc_address_ctx", c_void_p)]


class MpvOpenGLFbo(Structure):
    _fields_ = [
        ("fbo", c_int),
        ("w", c_int),
        ("h", c_int),
        ("internal_format", c_int),
    ]


# 回调签名
GET_PROC_ADDRESS_FN = CFUNCTYPE(c_void_p, c_void_p, c_char_p)
UPDATE_FN = CFUNCTYPE(None, c_void_p)

log = logging.getLogger(__name__)

# `mpv_set_property_async` 对字符串**不做拷贝**，必须保证内存活到 mpv 处理完。
# 这里留一个有上限的保活表（见 set_property_async 的注释）。
_ASYNC_KEEPALIVE: list = []


class MpvError(RuntimeError):
    """mpv 返回的负错误码。"""


# --------------------------------------------------------------------------------------
# DLL 定位与加载
# --------------------------------------------------------------------------------------

_LIB_CACHE = None
_LIB_LOCK = threading.Lock()


def _candidate_paths():
    env = os.environ.get("ZHIJIAN_LIBMPV")
    if env:
        yield env
    if getattr(sys, "frozen", False):
        # PyInstaller onedir: sys._MEIPASS 指向 _internal
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        yield os.path.join(base, "libmpv-2.dll")
        yield os.path.join(os.path.dirname(sys.executable), "libmpv-2.dll")
    here = os.path.dirname(os.path.abspath(__file__))
    # src/zhijian/mpv/ -> 项目根
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    yield os.path.join(root, "runtime", "libmpv-2.dll")
    yield os.path.join(root, "downloads", "mpv", "extract", "libmpv-2.dll")
    yield os.path.join(root, "downloads", "mpv", "libmpv-2.dll")


def libmpv_path() -> str:
    for p in _candidate_paths():
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    raise FileNotFoundError(
        "找不到 libmpv-2.dll。请把它放到 <项目根>\\runtime\\libmpv-2.dll, "
        "或设置环境变量 ZHIJIAN_LIBMPV 指向该文件。"
    )


def load_libmpv():
    """加载 libmpv 并声明全部函数原型（进程内只加载一次）。"""
    global _LIB_CACHE
    with _LIB_LOCK:
        if _LIB_CACHE is not None:
            return _LIB_CACHE

        path = libmpv_path()
        # 让 libmpv 同目录下的其它 DLL（若有）能被找到
        try:
            os.add_dll_directory(os.path.dirname(path))
        except (AttributeError, OSError):
            pass

        lib = ctypes.CDLL(path)

        # ---- 句柄 / 生命周期 -------------------------------------------------
        lib.mpv_create.argtypes = []
        lib.mpv_create.restype = c_void_p
        lib.mpv_create_client.argtypes = [c_void_p, c_char_p]
        lib.mpv_create_client.restype = c_void_p
        lib.mpv_initialize.argtypes = [c_void_p]
        lib.mpv_initialize.restype = c_int
        lib.mpv_terminate_destroy.argtypes = [c_void_p]
        lib.mpv_terminate_destroy.restype = None
        lib.mpv_set_option_string.argtypes = [c_void_p, c_char_p, c_char_p]
        lib.mpv_set_option_string.restype = c_int
        lib.mpv_set_property_string.argtypes = [c_void_p, c_char_p, c_char_p]
        lib.mpv_set_property_string.restype = c_int
        lib.mpv_get_property_string.argtypes = [c_void_p, c_char_p]
        lib.mpv_get_property_string.restype = c_void_p
        lib.mpv_get_property.argtypes = [c_void_p, c_char_p, c_int, c_void_p]
        lib.mpv_get_property.restype = c_int
        lib.mpv_set_property.argtypes = [c_void_p, c_char_p, c_int, c_void_p]
        lib.mpv_set_property.restype = c_int
        lib.mpv_observe_property.argtypes = [c_void_p, c_uint64, c_char_p, c_int]
        lib.mpv_observe_property.restype = c_int
        lib.mpv_command.argtypes = [c_void_p, POINTER(c_char_p)]
        lib.mpv_command.restype = c_int
        lib.mpv_command_string.argtypes = [c_void_p, c_char_p]
        lib.mpv_command_string.restype = c_int
        lib.mpv_command_async.argtypes = [c_void_p, c_uint64, POINTER(c_char_p)]
        lib.mpv_command_async.restype = c_int
        # ⚠️ 参数顺序是 (ctx, reply_userdata, name, format, data) ——
        #    注意 reply_userdata 在**第二个**位置, 跟 mpv_set_property 的 (ctx,name,fmt,data)
        #    完全不同。写错不会崩, 只会返回 -9 "unsupported format", 值静默不生效。
        lib.mpv_set_property_async.argtypes = [c_void_p, c_uint64, c_char_p, c_int, c_void_p]
        lib.mpv_set_property_async.restype = c_int
        lib.mpv_wait_event.argtypes = [c_void_p, c_double]
        lib.mpv_wait_event.restype = POINTER(MpvEvent)
        lib.mpv_wakeup.argtypes = [c_void_p]
        lib.mpv_wakeup.restype = None
        lib.mpv_free.argtypes = [c_void_p]
        lib.mpv_free.restype = None
        lib.mpv_free_node_contents.argtypes = [POINTER(MpvNode)]
        lib.mpv_free_node_contents.restype = None
        lib.mpv_error_string.argtypes = [c_int]
        lib.mpv_error_string.restype = c_char_p
        lib.mpv_client_api_version.argtypes = []
        lib.mpv_client_api_version.restype = c_uint64
        lib.mpv_request_log_messages.argtypes = [c_void_p, c_char_p]
        lib.mpv_request_log_messages.restype = c_int

        # ---- 渲染上下文 -------------------------------------------------------
        lib.mpv_render_context_create.argtypes = [POINTER(c_void_p), c_void_p, POINTER(MpvRenderParam)]
        lib.mpv_render_context_create.restype = c_int
        lib.mpv_render_context_render.argtypes = [c_void_p, POINTER(MpvRenderParam)]
        lib.mpv_render_context_render.restype = c_int
        lib.mpv_render_context_set_update_callback.argtypes = [c_void_p, UPDATE_FN, c_void_p]
        lib.mpv_render_context_set_update_callback.restype = None
        lib.mpv_render_context_update.argtypes = [c_void_p]
        lib.mpv_render_context_update.restype = c_uint64
        lib.mpv_render_context_report_swap.argtypes = [c_void_p]
        lib.mpv_render_context_report_swap.restype = None
        lib.mpv_render_context_free.argtypes = [c_void_p]
        lib.mpv_render_context_free.restype = None
        lib.mpv_render_context_set_parameter.argtypes = [c_void_p, POINTER(MpvRenderParam)]
        lib.mpv_render_context_set_parameter.restype = c_int

        _LIB_CACHE = lib
        return lib


# --------------------------------------------------------------------------------------
# 高层封装
# --------------------------------------------------------------------------------------


class MpvNodeValue:
    """把 mpv_node 递归转成 Python 原生对象。"""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __repr__(self):  # pragma: no cover - 调试用
        return f"MpvNodeValue({self.value!r})"


def node_to_python(node: MpvNode):
    """递归转换 mpv_node -> dict / list / str / int / float / bool。"""
    fmt = node.format
    if fmt == MPV_FORMAT_STRING:
        raw = node.u.string
        return raw.decode("utf-8", "replace") if raw else ""
    if fmt == MPV_FORMAT_OSD_STRING:
        raw = node.u.string
        return raw.decode("utf-8", "replace") if raw else ""
    if fmt == MPV_FORMAT_FLAG:
        return bool(node.u.flag)
    if fmt == MPV_FORMAT_INT64:
        return int(node.u.int64)
    if fmt == MPV_FORMAT_DOUBLE:
        return float(node.u.double_)
    if fmt == MPV_FORMAT_NODE_ARRAY:
        lst = node.u.list
        if not lst:
            return []
        return [node_to_python(lst.contents.values[i]) for i in range(lst.contents.num)]
    if fmt == MPV_FORMAT_NODE_MAP:
        lst = node.u.list
        if not lst:
            return {}
        out = {}
        for i in range(lst.contents.num):
            key = lst.contents.keys[i]
            k = key.decode("utf-8", "replace") if key else ""
            out[k] = node_to_python(lst.contents.values[i])
        return out
    if fmt == MPV_FORMAT_BYTE_ARRAY:
        ba = node.u.ba
        if not ba:
            return b""
        return ctypes.string_at(ba.contents.data, ba.contents.size)
    return None


class EventProperty:
    """PROPERTY_CHANGE 事件的解码结果。"""

    __slots__ = ("name", "format", "value")

    def __init__(self, name, fmt, value):
        self.name = name
        self.format = fmt
        self.value = value

    def __repr__(self):  # pragma: no cover
        return f"EventProperty({self.name!r}, {self.value!r})"


class MpvHandle:
    """一个 mpv 实例。线程安全：所有 API 调用都会串行化到一把锁上。"""

    def __init__(self, client_name: str = "zhijian"):
        self._lib = load_libmpv()
        self._lock = threading.RLock()
        self._closed = False
        with self._lock:
            self.handle = self._lib.mpv_create()
            if not self.handle:
                raise MpvError("mpv_create() 返回 NULL")
            self._lib.mpv_create_client  # noqa: B018  (保持引用, 便于扩展)
            self._check(self._lib.mpv_set_option_string(self.handle, b"terminal", b"no"), "set terminal")
            self._check(self._lib.mpv_set_option_string(self.handle, b"msg-level", b"all=no"), "set msg-level")

    # -- 基础 -----------------------------------------------------------------
    def _check(self, status: int, what: str):
        if status < 0:
            err = self._lib.mpv_error_string(status)
            msg = err.decode("utf-8", "replace") if err else str(status)
            raise MpvError(f"{what} 失败: {msg} ({status})")
        return status

    def set_option(self, name: str, value) -> None:
        """必须在 initialize() 之前调用。"""
        if isinstance(value, bool):
            value = "yes" if value else "no"
        elif isinstance(value, (int, float)):
            value = repr(value)
        with self._lock:
            self._check(
                self._lib.mpv_set_option_string(self.handle, name.encode("utf-8"), str(value).encode("utf-8")),
                f"set_option {name}",
            )

    def initialize(self) -> None:
        with self._lock:
            self._check(self._lib.mpv_initialize(self.handle), "mpv_initialize")

    def terminate_destroy(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.handle:
                self._lib.mpv_terminate_destroy(self.handle)
                self.handle = None

    def close_handle_only(self) -> None:
        """只标记关闭, 不销毁（退出阶段用, 见 §9.28 第 14 条）。"""
        self._closed = True

    # -- 属性 -----------------------------------------------------------------
    def set_property(self, name: str, value, fmt: int | None = None) -> None:
        nb = name.encode("utf-8")
        with self._lock:
            if self._closed or not self.handle:
                return
            if fmt is None:
                if isinstance(value, bool):
                    fmt = MPV_FORMAT_FLAG
                elif isinstance(value, int):
                    fmt = MPV_FORMAT_INT64
                elif isinstance(value, float):
                    fmt = MPV_FORMAT_DOUBLE
                else:
                    fmt = MPV_FORMAT_STRING
            if fmt == MPV_FORMAT_STRING:
                self._check(
                    self._lib.mpv_set_property_string(self.handle, nb, str(value).encode("utf-8")),
                    f"set {name}",
                )
            elif fmt == MPV_FORMAT_FLAG:
                v = c_int(1 if value else 0)
                self._check(self._lib.mpv_set_property(self.handle, nb, fmt, byref(v)), f"set {name}")
            elif fmt == MPV_FORMAT_INT64:
                v = c_int64(int(value))
                self._check(self._lib.mpv_set_property(self.handle, nb, fmt, byref(v)), f"set {name}")
            elif fmt == MPV_FORMAT_DOUBLE:
                v = c_double(float(value))
                self._check(self._lib.mpv_set_property(self.handle, nb, fmt, byref(v)), f"set {name}")
            else:
                raise ValueError(f"不支持的 set 格式: {fmt}")

    def set_property_async(self, name: str, value, fmt: int | None = None,
                           reply_userdata: int = 0) -> int:
        """异步写属性（不阻塞）。

        一般**不需要**用它 —— 修掉 `wait_event` 持锁的问题之后，同步 `set_property`
        实测只要 1ms。保留它是为了"批量写十几个属性且不想有任何阻塞"的场合。

        ⚠️ 两个坑：
        1. libmpv **没有** `mpv_set_property_string_async`，字符串也只能走通用接口。
        2. 参数顺序是 `(ctx, reply_userdata, name, format, data)` —— reply_userdata
           在**第二个**位置，跟 `mpv_set_property` 的 `(ctx, name, format, data)` 不同。
        3. 异步接口**不拷贝**数据，所有 ctypes 对象都要保活（见 `_ASYNC_KEEPALIVE`）。
        """
        nb = name.encode("utf-8")
        with self._lock:
            if self._closed or not self.handle:
                return MPV_ERROR_PROPERTY_UNAVAILABLE
            if fmt is None:
                if isinstance(value, bool):
                    fmt = MPV_FORMAT_FLAG
                elif isinstance(value, int):
                    fmt = MPV_FORMAT_INT64
                elif isinstance(value, float):
                    fmt = MPV_FORMAT_DOUBLE
                else:
                    fmt = MPV_FORMAT_STRING
            if fmt == MPV_FORMAT_STRING:
                holder = c_char_p(str(value).encode("utf-8"))
                ptr = ctypes.cast(holder, c_void_p)
            elif fmt == MPV_FORMAT_FLAG:
                holder = c_int(1 if value else 0)
                ptr = c_void_p(ctypes.addressof(holder))
            elif fmt == MPV_FORMAT_INT64:
                holder = c_int64(int(value))
                ptr = c_void_p(ctypes.addressof(holder))
            elif fmt == MPV_FORMAT_DOUBLE:
                holder = c_double(float(value))
                ptr = c_void_p(ctypes.addressof(holder))
            else:
                raise ValueError(f"不支持的 set 格式: {fmt}")
            _ASYNC_KEEPALIVE.append(holder)
            if len(_ASYNC_KEEPALIVE) > 512:
                del _ASYNC_KEEPALIVE[:256]
            rc = self._lib.mpv_set_property_async(self.handle, reply_userdata, nb, fmt, ptr)
            if rc < 0:
                log.warning("异步设置属性 %s=%r 失败: %s", name, value, error_string(rc))
            return rc

    def get_property(self, name: str, fmt: int = MPV_FORMAT_STRING, default=None):
        nb = name.encode("utf-8")
        with self._lock:
            if self._closed or not self.handle:
                return default
            if fmt == MPV_FORMAT_STRING:
                ptr = self._lib.mpv_get_property_string(self.handle, nb)
                if not ptr:
                    return default
                try:
                    return ctypes.cast(ptr, c_char_p).value.decode("utf-8", "replace")
                finally:
                    self._lib.mpv_free(ptr)
            if fmt == MPV_FORMAT_FLAG:
                v = c_int(0)
                st = self._lib.mpv_get_property(self.handle, nb, fmt, byref(v))
                return bool(v.value) if st >= 0 else default
            if fmt == MPV_FORMAT_INT64:
                v = c_int64(0)
                st = self._lib.mpv_get_property(self.handle, nb, fmt, byref(v))
                return int(v.value) if st >= 0 else default
            if fmt == MPV_FORMAT_DOUBLE:
                v = c_double(0.0)
                st = self._lib.mpv_get_property(self.handle, nb, fmt, byref(v))
                return float(v.value) if st >= 0 else default
            if fmt == MPV_FORMAT_NODE:
                node = MpvNode()  # 栈上分配 -> 只能 mpv_free_node_contents
                st = self._lib.mpv_get_property(self.handle, nb, fmt, byref(node))
                if st < 0:
                    return default
                try:
                    return node_to_python(node)
                finally:
                    self._lib.mpv_free_node_contents(byref(node))
            raise ValueError(f"不支持的 get 格式: {fmt}")

    # -- 命令 -----------------------------------------------------------------
    def command(self, *args) -> int:
        arr = (c_char_p * (len(args) + 1))()
        for i, a in enumerate(args):
            arr[i] = str(a).encode("utf-8")
        arr[len(args)] = None
        with self._lock:
            if self._closed or not self.handle:
                return MPV_ERROR_PROPERTY_UNAVAILABLE
            return self._lib.mpv_command(self.handle, arr)

    def command_async(self, *args) -> int:
        arr = (c_char_p * (len(args) + 1))()
        for i, a in enumerate(args):
            arr[i] = str(a).encode("utf-8")
        arr[len(args)] = None
        with self._lock:
            if self._closed or not self.handle:
                return MPV_ERROR_PROPERTY_UNAVAILABLE
            return self._lib.mpv_command_async(self.handle, 0, arr)

    def command_string(self, text: str) -> int:
        with self._lock:
            if self._closed or not self.handle:
                return MPV_ERROR_PROPERTY_UNAVAILABLE
            return self._lib.mpv_command_string(self.handle, text.encode("utf-8"))

    def observe(self, name: str, fmt: int, userdata: int = 0) -> None:
        with self._lock:
            if self._closed or not self.handle:
                return
            self._lib.mpv_observe_property(self.handle, userdata, name.encode("utf-8"), fmt)

    # -- 事件 -----------------------------------------------------------------
    def wait_event(self, timeout: float = 0.1):
        """返回 (event_id:int, error:int, userdata:int, payload)。timeout=0 表示不阻塞。

        ⚠️ **绝不能在 `mpv_wait_event` 外面持句柄锁**。它是一个阻塞调用，握着锁就等于
        把所有其它 API（写属性、发命令、观测）全堵在门外，每个都要白等一个 timeout。
        本项目实测：事件线程用 0.1s 超时轮询时，UI 线程**每次 `mpv_set_property`
        都要 ~110ms** —— 启动时 12 个字幕属性就是 1.3 秒，拖动音量条每一像素都卡一下
        （用户反馈"启动很慢"+"拖动很不跟手"，根子是同一个）。
        `mpv_wait_event` 只允许从单一线程调用，我们的设计里只有事件线程会调它，
        所以放开锁是安全的；销毁句柄前会先 join 事件线程（见 MpvEngine.stop）。
        """
        with self._lock:
            if self._closed or not self.handle:
                return None
            handle = self.handle
        ev_ptr = self._lib.mpv_wait_event(handle, c_double(timeout))
        if not ev_ptr:
            return None
        ev = ev_ptr.contents
        if ev.event_id == MPV_EVENT_NONE:
            return None
        payload = None
        if ev.event_id == MPV_EVENT_PROPERTY_CHANGE and ev.data:
            prop = ctypes.cast(ev.data, POINTER(MpvEventProperty)).contents
            name = prop.name.decode("utf-8", "replace") if prop.name else ""
            payload = EventProperty(name, prop.format, self._decode_prop(prop))
        elif ev.event_id == MPV_EVENT_END_FILE and ev.data:
            ef = ctypes.cast(ev.data, POINTER(MpvEventEndFile)).contents
            payload = {"reason": ef.reason, "error": ef.error}
        return (ev.event_id, ev.error, ev.reply_userdata, payload)

    def _decode_prop(self, prop: MpvEventProperty):
        fmt = prop.format
        if not prop.data:
            return None
        if fmt == MPV_FORMAT_FLAG:
            return bool(ctypes.cast(prop.data, POINTER(c_int)).contents.value)
        if fmt == MPV_FORMAT_INT64:
            return int(ctypes.cast(prop.data, POINTER(c_int64)).contents.value)
        if fmt == MPV_FORMAT_DOUBLE:
            return float(ctypes.cast(prop.data, POINTER(c_double)).contents.value)
        if fmt == MPV_FORMAT_STRING:
            raw = ctypes.cast(prop.data, POINTER(c_char_p)).contents.value
            return raw.decode("utf-8", "replace") if raw else ""
        if fmt == MPV_FORMAT_NODE:
            node = ctypes.cast(prop.data, POINTER(MpvNode)).contents
            return node_to_python(node)  # 事件里的 node 由 mpv 释放, 不要自己 free
        return None

    def wakeup(self) -> None:
        if self.handle and not self._closed:
            self._lib.mpv_wakeup(self.handle)

    def request_log_messages(self, level: str = "warn") -> None:
        if self.handle and not self._closed:
            self._lib.mpv_request_log_messages(self.handle, level.encode("utf-8"))


def mpv_version_tuple():
    lib = load_libmpv()
    v = lib.mpv_client_api_version()
    return (v >> 16) & 0xFFFF, v & 0xFFFF


def error_string(code: int) -> str:
    lib = load_libmpv()
    s = lib.mpv_error_string(code)
    return s.decode("utf-8", "replace") if s else str(code)
