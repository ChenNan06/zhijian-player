"""后台时长探针：把播放列表里每一项的时长补齐（task-131 起 100% 覆盖）。

链路（快 → 慢）：

  ① **纯 Python 头部秒读**（media.tags.read_duration）：MP3 / FLAC / M4A / MP4 的时长
     都写在容器固定结构里，单文件实测 0.2~1.1ms、零子进程、零解码 —— 播放列表
     14 项通常 10ms 内全部拿到。这是首选路径，不再依赖 ffprobe。
  ② **进程内共享只读探针**（shared_probe()）兜底：头部读不出的容器/异常文件才走这里。
     逐文件选项**必须重新打开轨道**再读 duration：
         audio=yes,video=yes,aid=auto,vid=auto,ao=null,vo=null,pause=yes,sub=no
     ⚠️ 旧实现传的是 vid=no,aid=no,audio=no,sub=no —— mpv 会以
        end_file reason=4 error=-16 (NOTHING_TO_PLAY) 直接结束文件，duration 属性
        永远读不到（实测 14/14 全 0）。探针仍然 **绝不发声/出画**（ao=null/vo=null），
        且 pause=yes 保证不跑帧解码；用完 stop 释放。
  ③ 图片等无时长素材直接记 0，不进 mpv（image 没有 duration，进去只会白等超时）。

工作线程顺序处理, 一次只探一个文件；列表被替换/程序退出时立即放弃。
"""

from __future__ import annotations

import logging
import os
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import router
from .tags import read_duration
from ..mpv.libmpv import MPV_FORMAT_DOUBLE
from ..mpv.player import shared_probe

log = logging.getLogger("zhijian.prober")

#: 兜底探针的逐文件选项 —— 关键是把被 PROBE_OPTIONS 关掉的轨道重新打开
#: （audio=no/video=no 会让 mpv 报 NOTHING_TO_PLAY，duration 永远不可得）。
PROBE_LOAD_OPTIONS = "audio=yes,video=yes,aid=auto,vid=auto,ao=null,vo=null,pause=yes,sub=no"

#: 兜底探针单文件等待上限（duration 正常 20~200ms 就有；超时按 0 处理）
PROBE_TIMEOUT = 1.5


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
        """请求补齐时长；已知道的路径自动跳过（幂等，可反复调用）。"""
        todo: list[str] = []
        seen: set[str] = set()
        with self._lock:
            for p in paths or []:
                if not p:
                    continue
                key = os.path.normcase(p)
                if key in self._known or key in seen:
                    continue
                seen.add(key)
                todo.append(p)
            if not todo:
                return
            self._generation += 1
            self._queue = todo
        self._ensure_thread()

    def stop(self, timeout: float = 1.5) -> None:
        self._running = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        self._thread = None

    def known(self) -> dict[str, float]:
        with self._lock:
            return dict(self._known)

    # ------------------------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._running = True
            self._thread = threading.Thread(target=self._worker, name="duration-prober",
                                            daemon=True)
            self._thread.start()

    # -- 单文件 -----------------------------------------------------------
    @staticmethod
    def _skip_probe(path: str) -> bool:
        """图片没有 duration 概念（mpv 只会白等超时）⇒ 不送兜底探针。"""
        try:
            return router.kind_of(path) == router.KIND_IMAGE
        except Exception:
            return False

    @staticmethod
    def _fast_duration(path: str) -> float:
        """① 纯 Python 头部秒读（失败返回 0.0，不抛）。"""
        try:
            d = float(read_duration(path) or 0.0)
            return d if d > 0.2 else 0.0
        except Exception:
            log.warning("头部读取时长失败: %s", path, exc_info=True)
            return 0.0

    def _mpv_duration(self, probe, path: str) -> float:
        """② 共享只读 mpv 探针兜底（重新打开轨道 + 静音静画 + 暂停）。"""
        try:
            probe.set("pause", True)
        except Exception:
            log.warning("探针设置暂停失败: %s", path, exc_info=True)
        try:
            rc = probe.load(path, options=PROBE_LOAD_OPTIONS)
            if rc < 0:
                log.warning("探针加载失败 rc=%s: %s", rc, path)
        except Exception:
            log.warning("探针加载异常: %s", path, exc_info=True)
            return 0.0
        deadline = time.time() + PROBE_TIMEOUT
        while time.time() < deadline and self._running:
            try:
                v = probe.get("duration", MPV_FORMAT_DOUBLE, None)
            except Exception:
                v = None
            if v:
                d = float(v)
                if d > 0.0:
                    return d
            time.sleep(0.03)
        return 0.0

    # ------------------------------------------------------------------
    def _worker(self) -> None:
        probe = None
        try:
            while self._running:
                with self._lock:
                    if not self._queue:
                        break
                    path = self._queue.pop(0)
                    gen = self._generation
                dur = self._fast_duration(path)
                if dur <= 0 and not self._skip_probe(path):
                    if probe is None:
                        try:
                            probe = shared_probe()
                        except Exception:
                            log.exception("拿不到共享探针, 时长探测放弃")
                            break
                    dur = self._mpv_duration(probe, path)
                with self._lock:
                    if gen != self._generation:
                        continue                     # 列表已替换：本项结果作废
                    self._known[os.path.normcase(path)] = float(dur)
                if dur > 0:
                    self.duration_ready.emit(path, float(dur))
        except Exception:
            log.exception("时长探测线程异常")
        finally:
            self._running = False
            if probe is not None:
                try:
                    probe.cmd("stop")                # 释放文件/解码器（绝不留后台解码）
                except Exception:
                    log.warning("探针 stop 失败", exc_info=True)
            self.finished.emit()
            # ⚠️ 收尾窗口内的竞态：request() 可能刚好在"队列清空 → 线程退出"之间
            #    塞进新任务，而那时 is_alive() 还是 True ⇒ 不会再起线程。这里补一刀：
            #    线程即将退出时若队列又有活，拉一个新线程接上（幂等，不会双跑）。
            with self._lock:
                pending = bool(self._queue)
            if pending:
                self._thread = None
                self._ensure_thread()
