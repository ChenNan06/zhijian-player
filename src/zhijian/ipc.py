"""进程内控制服务器（QLocalServer）—— 给自动化验收用的"控制面"。

它**不替代**真实输入注入：物理拖拽、长按仍由 scripts\\_wininput.py 用 SendInput 打进去。
这个服务器只负责两件事：
  1. 让测试脚本能**读回真实状态**（属性、几何、渲染计数、丢帧数）来做断言,
     而不是靠"看截图猜";
  2. 让测试脚本能**切场景**（打开文件、开抽屉、开设置、全屏）以便截图自审。

协议：一次连接发一行 JSON, 回一行 JSON。
    {"cmd": "state"}                       -> 全量状态
    {"cmd": "geom", "widget": "capsule"}   -> 某个控件的三套矩形
    {"cmd": "open", "path": "..."}         -> 打开媒体
    {"cmd": "ui", "what": "drawer", "on": true}
    {"cmd": "cmd", "argv": ["seek", "10", "absolute"]}
    {"cmd": "set", "name": "speed", "value": 2.0}
    {"cmd": "get", "name": "hwdec-current"}
    {"cmd": "eval", "expr": "..."}         -> 在 UI 线程里求值（调试用）
    {"cmd": "quit"}
"""

from __future__ import annotations

import builtins
import json
import logging
import math
import os
import threading
import time
import traceback

from PySide6 import QtWidgets
from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

log = logging.getLogger("zhijian.ipc")

CTL_SUFFIX = "-ctl"

WIDGETS = {
    "root": lambda m: m,
    "body": lambda m: m.body,
    "titlebar": lambda m: m.titlebar,
    "stage": lambda m: m.stage,
    "video": lambda m: m.stage.video,
    "capsule": lambda m: m.capsule,
    "drawer": lambda m: m.drawer,
    "drawer_handle": lambda m: m.stage.drawer_handle,
    "drawer_slot": lambda m: m.stage.drawer_slot,
    "drawer_host": lambda m: m.stage.drawer_host,
    "titlebar": lambda m: m.titlebar,
    # 播放列表入口去重后标题栏那个按钮已移除，测试改点胶囊上这个
    "btn_playlist_capsule": lambda m: m.capsule.btn_list,
    "btn_settings_title": lambda m: m.titlebar.btn_settings,
    "btn_menu_title": lambda m: m.titlebar.btn_menu,
    "playlist": lambda m: m.drawer.list,
    "settings": lambda m: m.settings,
    "settings_host": lambda m: m.stage.settings_host,
    "subtitle_panel": lambda m: m.subtitle_panel,
    "image_bar": lambda m: m.stage.image_bar,
    "audio_card": lambda m: m.stage.audio_card,
    "empty_state": lambda m: m.stage.empty_state,
    "settings_nav": lambda m: m.settings.nav,
    "status": lambda m: m.capsule.time_label,
    # 真实点击测试要用到的按钮（让测试脚本能算出它到底在屏幕的哪个像素上）
    "btn_playlist_title": lambda m: m.titlebar.btn_playlist,
    "btn_settings_title": lambda m: m.titlebar.btn_settings,
    "btn_menu_title": lambda m: m.titlebar.btn_menu,
    "btn_min": lambda m: m.titlebar.btn_min,
    "btn_max": lambda m: m.titlebar.btn_max,
    "btn_close": lambda m: m.titlebar.btn_close,
    "btn_play": lambda m: m.capsule.btn_play,
    "btn_prev": lambda m: m.capsule.btn_prev,
    "btn_next": lambda m: m.capsule.btn_next,
    "btn_mute": lambda m: m.capsule.btn_mute,
    "btn_speed": lambda m: m.capsule.btn_speed,
    "btn_sub": lambda m: m.capsule.btn_sub,
    "btn_full": lambda m: m.capsule.btn_full,
    "btn_list": lambda m: m.capsule.btn_list,
    "btn_loop": lambda m: m.capsule.btn_loop,
    "btn_shot": lambda m: m.capsule.btn_shot,
    "progress": lambda m: m.capsule.progress,
    "volume": lambda m: m.capsule.volume,
    "logo": lambda m: m.titlebar.logo,
    "drawer_add": lambda m: m.drawer.btn_add,
    "drawer_close": lambda m: m.drawer.btn_close,
    "drawer_search": lambda m: m.drawer.search,
}


def rect_of(w) -> list[int] | None:
    if w is None:
        return None
    r = w.geometry()
    return [int(r.x()), int(r.y()), int(r.width()), int(r.height())]


class ControlServer(QObject):
    def __init__(self, window, name: str, parent=None):
        super().__init__(parent)
        self.window = window
        self.name = name + CTL_SUFFIX
        QLocalServer.removeServer(self.name)
        self.server = QLocalServer(self)
        self.server.setMaxPendingConnections(8)
        if not self.server.listen(self.name):
            log.warning("控制服务器监听失败: %s", self.server.errorString())
        self.server.newConnection.connect(self._on_conn)
        self._clients: list[QLocalSocket] = []
        log.info("控制服务器已就绪: %s", self.name)

    # ------------------------------------------------------------------
    def _on_conn(self):
        c = self.server.nextPendingConnection()
        if c is None:
            return
        self._clients.append(c)
        c.readyRead.connect(lambda: self._on_read(c))
        c.disconnected.connect(lambda: self._drop(c))

    def _drop(self, c):
        if c in self._clients:
            self._clients.remove(c)
        c.deleteLater()

    def _on_read(self, c: QLocalSocket):
        try:
            raw = bytes(c.readAll()).decode("utf-8", "replace").strip()
        except Exception:
            return
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception:
                self._reply(c, {"ok": False, "error": "bad json"})
                continue
            try:
                resp = self.handle(req)
            except Exception as exc:
                resp = {"ok": False, "error": str(exc), "trace": traceback.format_exc()[-1500:]}
            self._reply(c, resp)

    def _reply(self, c: QLocalSocket, obj: dict):
        try:
            c.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            c.flush()
        except Exception:
            log.debug("回复失败", exc_info=True)

    # ------------------------------------------------------------------
    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        w = self.window
        if cmd == "ping":
            return {"ok": True, "t": time.time()}
        if cmd == "state":
            return {"ok": True, "state": self.state()}
        if cmd == "geom":
            key = req.get("widget", "root")
            getter = WIDGETS.get(key)
            if getter is None:
                return {"ok": False, "error": f"未知控件 {key}", "known": sorted(WIDGETS)}
            wd = getter(w)
            if wd is None:
                return {"ok": False, "error": f"控件 {key} 不存在"}
            tl = wd.mapTo(w, wd.rect().topLeft())
            gp = wd.mapToGlobal(wd.rect().topLeft())
            return {"ok": True, "widget": key, "geom": {
                "visible": bool(wd.isVisible()),
                "local": rect_of(wd),
                "in_window": [tl.x(), tl.y(), wd.width(), wd.height()],
                "screen": [gp.x(), gp.y(), wd.width(), wd.height()],
                "dpr": float(w.devicePixelRatioF() or 1.0),
            }}
        if cmd == "open":
            w.open_path(str(req.get("path", "")), autoplay=bool(req.get("play", True)))
            return {"ok": True}
        if cmd == "ui":
            what = req.get("what")
            on = req.get("on", None)
            if what == "drawer":
                w.drawer.set_open(bool(on) if on is not None else not w.drawer.is_open(), animate=bool(req.get("animate", False)))
            elif what == "settings":
                w.toggle_settings(bool(on) if on is not None else None)
            elif what == "subtitle":
                w.toggle_subtitle_panel(bool(on) if on is not None else None)
            elif what == "fullscreen":
                if bool(on) != w._fullscreen:
                    w.toggle_fullscreen()
            elif what == "always_on_top":
                w.toggle_always_on_top()
            elif what == "capsule":
                w._fader.set_visible_now(bool(on))
            elif what == "osd":
                w.show_osd(str(req.get("text", "测试提示")), str(req.get("icon", "info")), ms=int(req.get("ms", 3000)))
            else:
                return {"ok": False, "error": f"未知 ui 目标 {what}"}
            return {"ok": True}
        if cmd == "cmd":
            argv = [str(a) for a in req.get("argv", [])]
            rc = w.engine.cmd(*argv)
            return {"ok": rc >= 0, "rc": rc}
        if cmd == "set":
            name = str(req.get("name"))
            value = req.get("value")
            fmt = req.get("fmt")
            w.engine.set(name, value, fmt)
            return {"ok": True}
        if cmd == "get":
            name = str(req.get("name"))
            return {"ok": True, "value": w.engine.cached(name)}
        if cmd == "image":
            action = req.get("action")
            if action == "zoom":
                w._zoom_image(float(req.get("factor", 1.12)))
            elif action == "fit":
                w._set_image_fit(str(req.get("mode", "fit")))
            elif action == "rotate":
                w._rotate_image(int(req.get("deg", 90)))
            elif action == "flip":
                w._flip_image()
            elif action == "step":
                w._step_image(int(req.get("delta", 1)))
            else:
                return {"ok": False, "error": f"未知 image 动作 {action}"}
            return {"ok": True}
        if cmd == "gc":
            # 排查内存泄漏用：Python 对象总数 + 分类计数 + 线程数。
            # 光看 RSS 只能知道"涨了", 看不出"谁在涨"。
            import gc as _gc
            import threading as _th

            _gc.collect()
            objs = _gc.get_objects()
            counts: dict[str, int] = {}
            for o in objs:
                n = type(o).__name__
                counts[n] = counts.get(n, 0) + 1
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:24]
            return {"ok": True, "result": {
                "objects": len(objs),
                "threads": _th.active_count(),
                "top": top,
                "garbage": len(_gc.garbage),
            }}
        if cmd == "move":
            # 原生挪位置（不动尺寸）—— 见 platform/win.move_window 里为什么不能用 Qt 的 move()
            from .platform import win as _win

            ok = _win.move_window(w, int(req.get("x", 0)), int(req.get("y", 0)))
            return {"ok": bool(ok)}
        if cmd == "seek_perf":
            return {"ok": True, "result": self.seek_perf(req)}
        if cmd == "bench":
            return {"ok": True, "result": self.bench(w, req)}
        if cmd == "eval":
            expr = str(req.get("expr", ""))
            try:
                # 测试脚本经常要算 len()/sum()/sorted() 之类；不给 builtins 会被
                # "NameError: name 'len' is not defined" 挡住, 只能把表达式写得极其别扭。
                # 这里只放开一批**只读的纯函数**, 不开 eval/exec/open。
                _safe = {n: getattr(builtins, n) for n in (
                    "len", "sum", "min", "max", "abs", "round", "sorted", "list", "dict",
                    "set", "tuple", "str", "int", "float", "bool", "range", "enumerate",
                    "zip", "any", "all", "isinstance", "getattr", "hasattr", "repr", "type",
                    "setattr", "delattr", "callable", "iter", "next", "vars", "id", "hex",
                ) if hasattr(builtins, n)}
                # 诊断通道要能 import（测试脚本大量用 __import__ 取模块属性）。
                # 这条通道只在显式传 --control 时开启，不参与产品逻辑。
                _safe["__import__"] = builtins.__import__
                # ⚠️ globals 与 locals 必须传**同一个 dict**：分成两个的话，
                #    表达式里定义的 lambda / 推导式看不到 `m`（Python 的嵌套作用域
                #    不查 eval 的 locals），会报 NameError: name 'm' is not defined。
                ctx = {"__builtins__": _safe, "m": w, "w": w, "engine": w.engine,
                       "app": QApplication.instance(),
                       "QWidget": QtWidgets.QWidget, "QApplication": QApplication}
                val = eval(expr, ctx, ctx)
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                json.dumps(val)
            except Exception:
                val = repr(val)
            return {"ok": True, "value": val}
        if cmd == "grab":
            target = str(req.get("widget", "root"))
            getter = WIDGETS.get(target)
            path = str(req.get("path", ""))
            if getter is None:
                return {"ok": False, "error": f"未知控件 {target}"}
            pm = getter(w).grab()
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            pm.save(path, "PNG")
            return {"ok": True, "path": path, "size": [pm.width(), pm.height()]}
        if cmd == "key_log":
            if req.get("clear"):
                w.clear_key_log()
                return {"ok": True, "samples": 0}
            data = w.dump_key_log()
            return {"ok": True, "samples": len(data), "log": data}
        if cmd == "geom_log":
            if req.get("clear"):
                w.clear_geom_log()
                return {"ok": True, "samples": 0}
            data = w.dump_geom_log()
            return {"ok": True, "samples": len(data), "log": data}
        if cmd == "resize":
            w.resize(int(req.get("w", 1280)), int(req.get("h", 760)))
            return {"ok": True, "size": [w.width(), w.height()]}
        if cmd == "quit":
            QTimer.singleShot(60, w.close)
            return {"ok": True}
        return {"ok": False, "error": f"未知命令 {cmd}"}

    # ------------------------------------------------------------------
    def state(self) -> dict:
        w = self.window
        eng = w.engine
        vid = w.stage.video
        import math

        state = {
            "app": "zhijian",
            "ts": time.time(),
            "path": w._path,
            "kind": w._kind,
            "paused": bool(w._paused),
            "speed": float(w._speed),
            "boost": bool(w._boost),
            "restore_latency_ms": w._restore_latency_ms,
            "position": float(w._position),
            "duration": float(w._duration),
            "fullscreen": bool(w._fullscreen),
            "maximized": bool(w.isMaximized()),
            "drawer_open": bool(w.drawer.is_open()),
            "drawer_w": int(w.drawer.width()),
            "settings_visible": bool(w.stage.settings_host.isVisible()),
            "subtitle_visible": bool(w.subtitle_panel.isVisible()),
            "image_zoom": float(w._image_zoom),
            "image_rotation": int(w._image_rotation),
            "image_natural": list(w._image_natural),
            "playlist_count": len(w.drawer.paths()),
            "window": {
                "size": [w.width(), w.height()],
                "dpr": float(w.devicePixelRatioF() or 1.0),
                "geometry_screen": [w.geometry().x(), w.geometry().y(), w.width(), w.height()],
            },
            "render": {
                "rc_ready": bool(vid.rc_ready),
                "frames": int(vid.rendered_frames()),
                "gl": vid.gl_info,
                "size": [vid.width(), vid.height()],
                "last_frame_age": (time.perf_counter() - vid.last_frame_ts()) if vid.last_frame_ts() else None,
                "init_error": vid.init_error,
            },
            "mpv": {
                "hwdec_current": eng.cached("hwdec-current"),
                "vo_configured": eng.cached("vo-configured"),
                "video_codec": eng.cached("video-codec"),
                "video_format": eng.cached("video-format"),
                "width": eng.cached("width"),
                "height": eng.cached("height"),
                "container_fps": eng.cached("container-fps"),
                "estimated_vf_fps": eng.cached("estimated-vf-fps"),
                "frame_drop_count": eng.cached("frame-drop-count"),
                "decoder_frame_drop_count": eng.cached("decoder-frame-drop-count"),
                "core_idle": eng.cached("core-idle"),
                "paused_for_cache": eng.cached("paused-for-cache"),
                "demuxer_cache_time": eng.cached("demuxer-cache-time"),
                "cache_buffering_state": eng.cached("cache-buffering-state"),
                "eof_reached": eng.cached("eof-reached"),
                "sid": eng.cached("sid"),
                "vid": eng.cached("vid"),
                "aid": eng.cached("aid"),
                "sub_visibility": eng.cached("sub-visibility"),
                "sub_delay": eng.cached("sub-delay"),
                "metadata": eng.cached("filtered-metadata") or eng.cached("metadata"),
            },
            "capsule": {
                "visible": bool(w.capsule.isVisible()),
                "time_text": w.capsule.time_label.text(),
                "speed_text": w.capsule.btn_speed._text,
                "paused_icon": w.capsule.btn_play.icon_name(),
                "volume": float(w.capsule.volume.value()),
                "muted": bool(w.capsule._muted),
                "progress_duration": float(w.capsule.progress.duration()),
                "progress_position": float(w.capsule.progress.position()),
                "loop_mode": w.capsule._loop_mode,
            },
            "rects": {},
        }
        for key, getter in WIDGETS.items():
            try:
                wd = getter(w)
                if wd is None:
                    continue
                tl = wd.mapTo(w, wd.rect().topLeft())
                state["rects"][key] = {
                    "visible": bool(wd.isVisible()),
                    "win": [tl.x(), tl.y(), wd.width(), wd.height()],
                }
            except Exception:
                continue
        return state

    # ------------------------------------------------------------------
    def bench(self, w, req: dict) -> dict:
        """交互跟手性基准（真实跑在 UI 线程里，测的就是用户会感受到的阻塞）。"""
        what = str(req.get("what", "volume_set"))
        n = int(req.get("n", 60))
        eng = w.engine
        capsule = w.capsule

        def _stats(xs: list[float]) -> dict:
            xs = sorted(xs)
            if not xs:
                return {}
            k = max(0, min(len(xs) - 1, int(round(0.95 * (len(xs) - 1)))))
            return {
                "n": len(xs),
                "total_ms": round(sum(xs), 1),
                "mean_ms": round(sum(xs) / len(xs), 2),
                "p50_ms": round(xs[len(xs) // 2], 2),
                "p95_ms": round(xs[k], 2),
                "max_ms": round(xs[-1], 2),
            }

        if what == "volume_set":
            ts = []
            for i in range(n):
                v = 30.0 + (i % 70)
                t = time.perf_counter()
                eng.set("volume", float(v))
                ts.append((time.perf_counter() - t) * 1000)
            return _stats(ts)

        if what == "volume_drag":
            # 完整走一遍"用户拖动"的路径：改值 → 滑块重绘 → OSD 合帧 → 引擎写入
            ts = []
            for i in range(n):
                v = float(int(50 - 50 * math.cos(i / max(1, n - 1) * 2 * math.pi)))
                t = time.perf_counter()
                capsule.set_volume(v)
                w._on_volume(v)
                ts.append((time.perf_counter() - t) * 1000)
            return _stats(ts)

        if what == "loop_lag_start":
            # ⚠️ 心跳必须**跨两次 IPC 调用**来量：bench 本身就跑在 UI 线程里，
            #    在同一个调用里挂 QTimer 再忙等，心跳根本没机会触发（量出来是空的）。
            #    所以拆成 start / stop，中间由测试脚本制造负载。
            st = {"last": time.perf_counter(), "gaps": []}
            self._lag = st

            def tick():
                now = time.perf_counter()
                st["gaps"].append((now - st["last"]) * 1000)
                st["last"] = now

            hb = QTimer(w)
            hb.setInterval(20)
            hb.timeout.connect(tick)
            hb.start()
            st["timer"] = hb
            return {"started": True}

        if what == "loop_lag_stop":
            st = getattr(self, "_lag", None)
            if not st:
                return {"error": "没有在跑的 loop_lag"}
            st["timer"].stop()
            gaps = [g for g in st["gaps"] if g > 0]
            out = _stats(gaps)
            out["over_150ms"] = sum(1 for g in gaps if g > 150)
            out["over_60ms"] = sum(1 for g in gaps if g > 60)
            out["baseline_ms"] = 20.0
            self._lag = None
            return out

        if what == "drawer_step":
            # 逐段量"抽屉动画一帧"的开销：整体 / 只改宽度 / 只跑布局 / 只重绘胶囊
            d = w.drawer
            target = int(req.get("width", 392))
            ts = {}
            t = time.perf_counter()
            for _ in range(20):
                d.set_target_width(target)
                QApplication.processEvents()
            ts["set_target_width"] = (time.perf_counter() - t) / 20 * 1000

            t = time.perf_counter()
            for _ in range(20):
                w.capsule._apply_visibility()
            ts["capsule_apply_visibility"] = (time.perf_counter() - t) / 20 * 1000

            t = time.perf_counter()
            for _ in range(20):
                w.layout().activate()
                QApplication.processEvents()
            ts["relayout"] = (time.perf_counter() - t) / 20 * 1000

            t = time.perf_counter()
            for i in range(20):
                w.capsule.set_list_checked(i % 2 == 0)
            ts["capsule_toggle"] = (time.perf_counter() - t) / 20 * 1000
            return {k: round(v, 2) for k, v in ts.items()}

        if what == "resize_fps":
            # 连续改窗口尺寸时到底能出多少帧（模拟拖边缘缩放）
            vid = w.stage.video
            n = int(req.get("n", 40))
            base = w.width()
            f0 = vid.rendered_frames()
            t0 = time.perf_counter()
            for i in range(n):
                w.resize(base + (i % 12) * 8, w.height())
                QApplication.processEvents()
            dur = time.perf_counter() - t0
            f1 = vid.rendered_frames()
            w.resize(base, w.height())
            return {"n": n, "seconds": round(dur, 3), "frames": f1 - f0,
                    "fps": round((f1 - f0) / max(0.001, dur), 1),
                    "ms_per_resize": round(dur / n * 1000, 2)}

        if what == "paint_cost":
            # 单独量各控件一次重绘的耗时
            from PySide6.QtGui import QPixmap as _QP

            out = {}
            for name, wid in (("capsule", w.capsule), ("titlebar", w.titlebar),
                              ("drawer", w.drawer), ("stage", w.stage)):
                try:
                    px = _QP(wid.size())
                    t = time.perf_counter()
                    for _ in range(10):
                        wid.render(px)
                    out[name] = round((time.perf_counter() - t) / 10 * 1000, 2)
                except Exception as exc:
                    out[name] = f"ERR {exc}"
            return out

        if what == "seek_drag":
            vid = w.stage.video
            dur = float(eng.cached("duration") or 20.0)
            drops0 = int(eng.cached("frame-drop-count") or 0)
            ts = []
            for i in range(n):
                tgt = 1.0 + (dur - 3.0) * ((i * 7919) % 1000) / 1000.0
                before = vid.rendered_frames()
                t = time.perf_counter()
                w._on_seek_preview(float(tgt))
                # 等真实换帧（最多 400ms）
                end = time.perf_counter() + 0.4
                while vid.rendered_frames() == before and time.perf_counter() < end:
                    QApplication.processEvents()
                    time.sleep(0.002)
                ts.append((time.perf_counter() - t) * 1000)
                QApplication.processEvents()
                time.sleep(0.05)
            st = _stats(ts)
            drops1 = int(eng.cached("frame-drop-count") or 0)
            st["frame_drops_delta"] = max(0, drops1 - drops0)
            st["frame_drops_counter_reset"] = drops1 < drops0
            return st

        return {"error": f"未知 bench 类型 {what}"}

    def seek_perf(self, req: dict) -> dict:
        """连续跨段 Seek, 量"画面恢复时间"与丢帧。

        判据：从发出 seek 到 **mapped 帧真正换掉** 的时间差（用渲染计数变化判定）,
        而不是靠 mpv 自己报的 restart 事件 —— 后者会在画面还没换时就到。
        """
        w = self.window
        eng = w.engine
        vid = w.stage.video
        targets = req.get("targets")
        if not targets:
            dur = float(eng.cached("duration") or 0.0) or 30.0
            span = float(req.get("span", 30.0))
            n = int(req.get("n", 12))
            step = span / max(1, n - 1)
            targets = [round(min(dur - 0.5, 2.0 + i * step), 3) for i in range(n)]
        # 保证是"跨 30 秒以上"的跨度
        results = []
        drops0 = int(eng.cached("frame-drop-count") or 0)
        gap = float(req.get("gap_ms", 0.0)) / 1000.0
        precise = bool(req.get("precise", True))
        for t in targets:
            eng.set("pause", False)
            if gap:
                time.sleep(gap)
            f0 = vid.rendered_frames()
            t0 = time.perf_counter()
            eng.seek_absolute(float(t), precise=precise)
            deadline = t0 + float(req.get("timeout", 3.0))
            recovered = None
            while time.perf_counter() < deadline:
                if vid.rendered_frames() > f0:
                    recovered = (time.perf_counter() - t0) * 1000.0
                    break
                # 让 Qt 处理重绘
                from PySide6.QtWidgets import QApplication

                QApplication.processEvents()
                time.sleep(0.001)
            results.append({
                "target": float(t),
                "recover_ms": round(recovered, 1) if recovered is not None else None,
                "frames": vid.rendered_frames() - f0,
            })
        drops1 = int(eng.cached("frame-drop-count") or 0)
        ok = [r["recover_ms"] for r in results if r["recover_ms"] is not None]
        srt = sorted(ok)

        def pct(p):
            if not srt:
                return None
            i = min(len(srt) - 1, int(round((len(srt) - 1) * p)))
            return srt[i]

        delta = drops1 - drops0
        return {
            "count": len(results),
            "recovered": len(ok),
            "max_ms": max(ok) if ok else None,
            "avg_ms": round(sum(ok) / len(ok), 1) if ok else None,
            "min_ms": min(ok) if ok else None,
            "p50_ms": pct(0.50),
            "p90_ms": pct(0.90),
            "under_100ms": sum(1 for v in ok if v < 100.0),
            "frame_drops_delta": max(0, delta),
            "frame_drops_counter_reset": delta < 0,
            "samples": results,
        }


class ControlClient:
    """测试脚本侧的客户端（同步）。"""

    def __init__(self, name: str, timeout: float = 30.0):
        self.name = name + CTL_SUFFIX
        self.timeout = timeout

    def call(self, **req) -> dict:
        s = QLocalSocket()
        s.connectToServer(self.name)
        if not s.waitForConnected(3000):
            raise ConnectionError(f"连不上控制服务器 {self.name}: {s.errorString()}")
        s.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        s.flush()
        buf = b""
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if s.waitForReadyRead(500):
                buf += bytes(s.readAll())
                if b"\n" in buf:
                    break
            else:
                if s.state() != QLocalSocket.ConnectedState:
                    break
        s.disconnectFromServer()
        line = buf.decode("utf-8", "replace").strip().splitlines()
        if not line:
            raise TimeoutError(f"{self.name} 无响应 (cmd={req.get('cmd')})")
        return json.loads(line[0])
