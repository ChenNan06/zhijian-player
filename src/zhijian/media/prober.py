"""后台时长探针：把播放列表里每一项的时长补齐。

用**进程内共享的只读探针实例**（`shared_probe()`），它永久带
`audio=no / video=no / aid=no / vid=no / ao=null / vo=null`：

  * 绝不可能发声 —— 不会出现"主播放器与后台探针同时出声"的双声事故；
  * 也绝不建第二个短命 mpv 实例（秒建秒销会让 libmpv 的 C++ 异常穿透,
    约 1/3 概率把进程打成 0xC0000005）。

工作线程顺序处理, 一次只探一个文件；列表被替换/程序退出时立即放弃。
"""

from __future__ import annotations

import logging
import os
import threading
import time

from PySide6.QtCore import QObject, Signal

from ..mpv.libmpv import MPV_FORMAT_DOUBLE
from ..mpv.player import shared_probe

log = logging.getLogger("zhijian.prober")


class DurationProber(QObject):
    duration_ready = Signal(str, float)
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: list[str] = []
        self._known: dict[str, float] = {}
        self._lock = threading.Lock()
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._running = False

    # ------------------------------------------------------------------
    def request(self, paths: list[str]) -> None:
        todo = [p for p in paths if os.path.normcase(p) not in self._known]
        with self._lock:
            self._generation += 1
            self._queue = todo
        if not todo:
            return
        if self._thread is None or not self._thread.is_alive():
            self._running = True
            self._thread = threading.Thread(target=self._worker, name="duration-prober", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 1.5) -> None:
        self._running = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        self._thread = None

    def known(self) -> dict[str, float]:
        return dict(self._known)

    # ------------------------------------------------------------------
    def _worker(self):
        try:
            probe = shared_probe()
        except Exception:
            log.exception("拿不到共享探针, 时长探测放弃")
            return
        while self._running:
            with self._lock:
                if not self._queue:
                    break
                path = self._queue.pop(0)
                gen = self._generation
            try:
                probe.set("pause", True)
                probe.load(path, options="vid=no,aid=no,audio=no,sub=no")
                dur = 0.0
                deadline = time.time() + 2.0
                while time.time() < deadline and self._running:
                    v = probe.get("duration", MPV_FORMAT_DOUBLE, None)
                    if v:
                        dur = float(v)
                        break
                    time.sleep(0.03)
                with self._lock:
                    if gen != self._generation:
                        continue
                    self._known[os.path.normcase(path)] = dur
                if dur > 0:
                    self.duration_ready.emit(path, dur)
            except Exception:
                log.debug("探测时长失败: %s", path, exc_info=True)
        self.finished.emit()
