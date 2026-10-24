"""异步缩略图服务（内存 + 磁盘 LRU，**主线程零阻塞**）。

设计要点（task-121③）：
  * 所有解码/抽帧/磁盘 IO 都在 QThreadPool 的工作线程里做；
  * 主线程只做三件事：查内存缓存（O(1)）、请求（去重入队）、收到 ready 信号后重绘；
  * 内存 LRU（默认 160 张）+ 磁盘 LRU（默认 600 张 PNG，按"路径+大小+时间戳+尺寸"哈希命名）；
  * 三类素材：图片 = QImageReader 缩放解码；音频 = media.tags 内嵌封面，无封面 ⇒ 渐变黑胶默认封面；
    视频 = mpv.frame_grab.FrameGrabber 抽第 10% 位置（未知时长则第 3 秒）关键帧。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QLinearGradient, QPainter, QPainterPath, QPen

log = logging.getLogger("zhijian.media.thumbs")

MEM_MAX = 160
DISK_MAX = 600
THUMB_W = 288          # 解码目标宽（2x 160，给高 DPI 留余量）
THUMB_H = 162
VIDEO_T_FALLBACK = 3.0


def _cover_size(w: int, h: int) -> QSize:
    return QSize(max(16, int(w)), max(9, int(h)))


def _center_crop(img: QImage, w: int, h: int) -> QImage:
    """等比缩放后居中裁切成 w×h（保持卡片比例，不拉伸）。"""
    if img is None or img.isNull():
        return img
    scaled = img.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = max(0, (scaled.width() - w) // 2)
    y = max(0, (scaled.height() - h) // 2)
    return scaled.copy(x, y, min(w, scaled.width()), min(h, scaled.height()))


def default_cover(kind: str, w: int = THUMB_W, h: int = THUMB_H) -> QImage:
    """无封面时的默认图：音频 = 渐变黑胶；视频 = 深色胶片；图片 = 灰阶占位。"""
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        if kind == "audio":
            g = QLinearGradient(0, 0, w, h)
            g.setColorAt(0.0, QColor(38, 42, 52))
            g.setColorAt(1.0, QColor(16, 18, 24))
            p.fillRect(0, 0, w, h, g)
            cx, cy, r = int(w * 0.5), int(h * 0.5), int(min(w, h) * 0.34)
            p.setBrush(QColor(24, 26, 32))
            p.setPen(QPen(QColor(255, 255, 255, 40), 2))
            p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
            p.setBrush(QColor(200, 210, 225, 200))
            p.setPen(Qt.NoPen)
            rr = max(3, r // 5)
            p.drawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)
            p.drawEllipse(cx - 2, cy - int(r * 0.62), 4, 4)
        elif kind == "video":
            g = QLinearGradient(0, 0, w, h)
            g.setColorAt(0.0, QColor(28, 32, 44))
            g.setColorAt(1.0, QColor(12, 14, 20))
            p.fillRect(0, 0, w, h, g)
            p.setPen(QPen(QColor(255, 255, 255, 26), 2))
            for yy in (int(h * 0.22), int(h * 0.78)):
                p.drawLine(0, yy, w, yy)
            p.setBrush(QColor(255, 255, 255, 70))
            p.setPen(Qt.NoPen)
            tri = QPainterPath()
            cx, cy = w * 0.5, h * 0.5
            tri.moveTo(cx - 8, cy - 11)
            tri.lineTo(cx + 13, cy)
            tri.lineTo(cx - 8, cy + 11)
            tri.closeSubpath()
            p.drawPath(tri)
        else:
            p.fillRect(0, 0, w, h, QColor(40, 44, 52))
            p.setBrush(QColor(255, 255, 255, 28))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(int(w * 0.18), int(h * 0.22), int(w * 0.64), int(h * 0.56), 10, 10)
    finally:
        p.end()
    return img


class _Job(QRunnable):
    def __init__(self, svc: "ThumbnailService", path: str, kind: str):
        super().__init__()
        self._svc = svc
        self._path = path
        self._kind = kind
        self.signals = svc._signals

    def run(self) -> None:
        img = None
        try:
            img = self._svc._decode(self._path, self._kind)
        except Exception:
            log.debug("缩略图解码失败 %s", self._path, exc_info=True)
        if img is not None and not img.isNull():
            self._svc._remember(self._path, img)
        self.signals.ready.emit(self._path, img)


class _Signals(QObject):
    ready = Signal(str, object)


class ThumbnailService(QObject):
    """线程池 + 内存/磁盘 LRU 的缩略图服务（每个窗口一个实例即可）。"""

    ready = Signal(str, object)      # path, QImage|None

    def __init__(self, cache_dir: str = "", parent=None):
        super().__init__(parent)
        self._signals = _Signals(self)
        self._signals.ready.connect(self._on_job_done)
        self._mem: "OrderedDict[str, QImage]" = OrderedDict()
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._cache_dir = cache_dir or os.path.join(os.path.expanduser("~"), ".zhijian_thumbs")
        self._frames = None
        self.hits = 0
        self.misses = 0
        self.disk_hits = 0
        self.jobs = 0

    # -- 公共 ---------------------------------------------------------------
    def cached(self, path: str):
        with self._lock:
            img = self._mem.get(path)
            if img is not None:
                self._mem.move_to_end(path)
            return img

    def request(self, path: str, kind: str = "video") -> None:
        """异步请求缩略图（主线程只做去重入队，绝不阻塞）。"""
        if not path:
            return
        with self._lock:
            if path in self._mem or path in self._pending:
                return
            self._pending.add(path)
        self.jobs += 1
        self._pool.start(_Job(self, path, kind))

    def clear(self) -> None:
        with self._lock:
            self._mem.clear()

    # -- 内部 ---------------------------------------------------------------
    def _remember(self, path: str, img: QImage) -> None:
        with self._lock:
            self._mem[path] = img
            self._mem.move_to_end(path)
            while len(self._mem) > MEM_MAX:
                self._mem.popitem(last=False)

    def _on_job_done(self, path, img) -> None:
        with self._lock:
            self._pending.discard(path)
        self.ready.emit(path, img)

    def _disk_path(self, path: str, kind: str) -> str:
        try:
            st = os.stat(path)
            key = "%s|%d|%d|%s|%dx%d" % (os.path.normcase(path), int(st.st_size),
                                         int(st.st_mtime), kind, THUMB_W, THUMB_H)
        except OSError:
            key = "%s|%s" % (path, kind)
        h = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:24]
        return os.path.join(self._cache_dir, "thumbs", h + ".png")

    def _decode(self, path: str, kind: str) -> QImage:
        # ① 磁盘缓存（在工作线程里读，主线程不沾 IO）
        dp = self._disk_path(path, kind)
        try:
            if os.path.isfile(dp):
                img = QImage(dp)
                if not img.isNull():
                    self.disk_hits += 1
                    return img
        except Exception:
            pass
        # ② 真正解码
        img = None
        if kind == "image":
            img = self._decode_image(path)
        elif kind == "audio":
            img = self._decode_audio(path)
        else:
            img = self._decode_video(path)
        if img is None or img.isNull():
            self.misses += 1
            return None
        # ③ 写磁盘缓存（工作线程）+ 磁盘 LRU 修剪
        try:
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            img.save(dp, "PNG")
            self._trim_disk(os.path.dirname(dp))
        except Exception:
            log.debug("缩略图写盘失败 %s", dp, exc_info=True)
        return img

    def _trim_disk(self, folder: str) -> None:
        """磁盘 LRU：超过 DISK_MAX 张就按 mtime 从旧到新删（每 32 次写盘检查一次，避免频繁 stat）。"""
        self._writes = int(getattr(self, "_writes", 0)) + 1
        if self._writes % 32:
            return
        try:
            names = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".png")]
            if len(names) <= DISK_MAX:
                return
            names.sort(key=lambda p: os.path.getmtime(p))
            for old in names[: max(0, len(names) - DISK_MAX)]:
                try:
                    os.remove(old)
                except OSError:
                    pass
            self.disk_trimmed = int(getattr(self, "disk_trimmed", 0)) + 1
        except OSError:
            pass

    def _decode_image(self, path: str):
        try:
            rd = QImageReader(path)
            rd.setAutoTransform(True)
            size = rd.size()
            if size.isValid() and size.width() > 0 and size.height() > 0:
                sc = max(THUMB_W / float(size.width()), THUMB_H / float(size.height()))
                rd.setScaledSize(QSize(max(1, int(size.width() * sc)), max(1, int(size.height() * sc))))
            img = rd.read()
            if img.isNull():
                img = QImage(path)
            return _center_crop(img, THUMB_W, THUMB_H) if not img.isNull() else None
        except Exception:
            log.debug("图片缩略失败 %s", path, exc_info=True)
            return None

    def _decode_audio(self, path: str):
        try:
            from .tags import read_tags
            tags = read_tags(path)
            data = getattr(tags, "cover", None)
            if data:
                img = QImage.fromData(bytes(data))
                if not img.isNull():
                    return _center_crop(img, THUMB_W, THUMB_H)
        except Exception:
            log.debug("内嵌封面读取失败 %s", path, exc_info=True)
        return default_cover("audio", THUMB_W, THUMB_H)

    def _decode_video(self, path: str):
        try:
            if self._frames is None:
                from ..mpv.frame_grab import FrameGrabber
                self._frames = FrameGrabber(width=THUMB_W)
            dur = 0.0
            try:
                from .prober import probe_media
                info = probe_media(path)
                dur = float(getattr(info, "duration", 0.0) or 0.0)
            except Exception:
                dur = 0.0
            t = dur * 0.10 if dur > 0.5 else VIDEO_T_FALLBACK
            img = self._frames.grab(path, max(0.0, t))
            if img is None or img.isNull():
                return default_cover("video", THUMB_W, THUMB_H)
            return _center_crop(img, THUMB_W, THUMB_H)
        except Exception:
            log.debug("视频抽帧失败 %s", path, exc_info=True)
            return default_cover("video", THUMB_W, THUMB_H)

    def close(self) -> None:
        try:
            self._pool.clear()
            self._pool.waitForDone(1500)
        except Exception:
            log.debug("缩略图线程池收尾失败", exc_info=True)
        try:
            if self._frames is not None:
                self._frames.close()
                self._frames = None
        except Exception:
            pass
