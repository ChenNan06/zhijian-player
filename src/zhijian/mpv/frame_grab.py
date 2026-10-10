r"""进度条悬停缩略图 —— **纯 ffmpeg 子进程抽帧**。

## 为什么不用 libmpv（重要，别再走回头路）

第一版用第二个 `libmpv` 实例 + SW 渲染离屏取帧。**脱离 GUI 单独跑完全正常**
（320x180，热态 2~21ms），但接进播放器后：播放正常 9 秒 → 一发起取帧 →
**主线程立刻卡死，控制面 IPC 连续 45s 无响应**。

原因：后台并存了**一整个播放引擎**。libmpv 实例会初始化 A/V 同步引擎，
和主实例在 Windows 图形栈上互斥争死。**取缩略图只需要"数据解码"，
根本不需要"全功能播放引擎"。**

## 现在的架构（工业标准做法）

    ffmpeg -ss <t> -i <file> -an -sn -frames:v 1 -vf scale=160:-2
           -f image2pipe -vcodec png -

* **进程级物理隔离**：ffmpeg 跑在独立子进程里，即使它卡住也**绝不可能**
  反噬主 UI 线程 —— 这是子进程方案最大的价值。
* `-an -sn`：不要音频、不要字幕，只解视频。
* `-ss` 放在 `-i` **之前**：走关键帧快速定位，比精确 seek 快一个数量级。
* 输出走 **stdout 管道**，不落盘。
* 主进程侧只做 `QImage.fromData` + LRU 缓存。

实测耗时（含 4K 源）：**135~376ms**。配合 LRU 缓存后重复悬停是零开销。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

THUMB_W = 160      # 160x90，体积小、解码快
# ⚠️ 上限不能大：每张 160x90 的 QImage 约 57KB，240 张就是 ~14MB，
#    而内存验收每轮都会重新打开媒体、重新预铺 —— 实测把 RSS 斜率
#    推到 +3.87 MB/轮（判定为泄漏）。96 张 ≈ 5.5MB，够覆盖一屏悬停。
_CACHE_MAX = 96
# Windows 下别弹控制台窗口
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _ffmpeg_exe() -> str:
    """找到可用的 ffmpeg：优先用 imageio_ffmpeg 自带的（可随包分发）。"""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    from shutil import which

    return which("ffmpeg") or "ffmpeg"


class FrameGrabber:
    """按时间点抽缩略图。**子进程隔离 + LRU 缓存**，线程安全。

    ⚠️ 必须在线程里调用（单次 135~376ms），不要在 GUI 线程直接调。
    """

    def __init__(self, width: int = THUMB_W):
        self._w = width
        self._exe = _ffmpeg_exe()
        self._lock = threading.Lock()
        self._cache: dict[tuple, object] = {}
        self._order: list[tuple] = []
        self._last_ms = 0.0
        self._fails = 0
        # ⚠️ 保存**当前正在跑的子进程**，让 close() 能立刻杀掉它。
        #    用 `subprocess.run` 拿不到句柄 —— 退出时线程还在等 ffmpeg（超时 12s），
        #    QThread 被析构就会崩（实测退出码 0xC0000409、耗时 4s）。
        self._proc = None

    # -- 内部 ---------------------------------------------------------------
    def _key(self, path: str, t: float) -> tuple:
        # 时间按 0.5s 分桶：悬停时鼠标一直在动，没必要每个像素都抽一帧
        return (path, int(t * 2))

    def _evict(self):
        while len(self._order) > _CACHE_MAX:
            self._cache.pop(self._order.pop(0), None)

    # -- 对外 ---------------------------------------------------------------
    def grab(self, path: str, t: float):
        """返回 QImage（失败返回 None）。命中缓存时几乎零开销。"""
        import time

        from PySide6.QtGui import QImage

        if not path or t < 0 or not os.path.exists(path):
            return None
        key = self._key(path, t)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            self._last_ms = 0.0
            return hit

        # ⚠️ 发起新任务前**必须先杀旧进程** —— 保证后台永远只有一个活跃 ffmpeg，
        #    否则快速滑动会积压一堆子进程。
        self.cancel_current()
        # ⚠️⚠️ 失败回退阶梯：`-skip_frame nokey` + 视频**尾部**时，
        #    `-ss` 之后找不到下一个关键帧会直接 EOF、不产出任何图像。
        #    光靠"夹到 时长-1"不够（实测 20 秒的视频夹到 19.0 仍然失败，
        #    因为最后一个关键帧可能更早）。所以失败后**逐级往回调**重试，
        #    直到拿到一帧为止 —— 保证任何位置都必然有画面，绝不卡在占位图上。
        for back in (0.0, 1.0, 2.0, 4.0, 8.0):
            img = self._grab_once(path, key, max(0.0, t - back))
            if img is not None:
                return img
        self._fails += 1
        return None

    def _grab_once(self, path: str, key: tuple, t: float):
        import time

        from PySide6.QtGui import QImage

        t0 = time.perf_counter()
        data = b""
        try:
            cmd = [
                self._exe, "-v", "error",
                # ⚠️ 三个提速关键，缺一不可：
                #   1) `-skip_frame nokey` —— **只解关键帧（I 帧）**，不解 P/B 帧，
                #      也不需要精确 A/V 同步。实测比默认快一个数量级。
                #   2) `-ss` 放在 `-i` **之前** —— 关键帧快速定位，不做精确 seek。
                #   3) `-an -sn` —— 不要音频、不要字幕。
                "-skip_frame", "nokey",
                "-ss", f"{max(0.0, t):.3f}",
                "-i", path,
                "-an", "-sn",
                "-frames:v", "1",
                "-vf", f"scale={self._w}:-2",
                "-f", "image2pipe", "-vcodec", "png", "-",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=_NO_WINDOW)
            self._proc = proc
            try:
                data = proc.communicate(timeout=12)[0] or b""
            finally:
                self._proc = None
        except Exception:
            return None

        if not data:
            return None
        img = QImage.fromData(data, "PNG")
        if img.isNull():
            return None
        with self._lock:
            self._cache[key] = img
            self._order.append(key)
            self._evict()
        self._last_ms = (time.perf_counter() - t0) * 1000.0
        return img

    def nearest(self, path: str, t: float, tol: float = 4.0):
        """找**已缓存**里时间最接近 t 的一张，返回 (QImage, 它的时间) 或 (None, 0)。

        ⚠️ 这是“零等待”的关键：精确帧没到之前，
        先拿一张时间相近的垫上，宁可略微不准也不要白屏/卡顿。
        超过 tol 秒就不算了（太远的画面反而会误导）。
        """
        best = None
        best_dt = tol
        with self._lock:
            for (pp, bucket), img in self._cache.items():
                if pp != path:
                    continue
                dt = abs(bucket / 2.0 - t)
                if dt < best_dt:
                    best_dt = dt
                    best = img
        return (best, t - best_dt) if best is not None else (None, 0.0)

    def prefetch(self, path: str, times) -> None:
        """预热一批时间点（可选，用于把整条时间轴的缩略图提前铺好）。"""
        for t in times:
            self.grab(path, t)

    @property
    def last_ms(self) -> float:
        return self._last_ms

    @property
    def cached(self) -> int:
        with self._lock:
            return len(self._cache)

    def blur_placeholder(self, img):
        """把一张图处理成"模糊占位图"（悬停时没有缓存帧就先拿它顶上）。

        ⚠️ 用户反馈“鼠标放上去是一团死灰的空框”——
        宁可给一张**模糊的当前画面**，也不能留空。
        """
        if img is None or img.isNull():
            return None
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene
            from PySide6.QtGui import QPainter, QPixmap

            pm = QPixmap.fromImage(img).scaledToWidth(self._w, Qt.SmoothTransformation)
            scene = QGraphicsScene()
            item = QGraphicsPixmapItem(pm)
            eff = QGraphicsBlurEffect()
            eff.setBlurRadius(9)
            item.setGraphicsEffect(eff)
            scene.addItem(item)
            out = QPixmap(pm.size())
            out.fill(Qt.transparent)
            p = QPainter(out)
            scene.render(p, out.rect(), pm.rect())
            p.end()
            scene.clear()
            return out.toImage()
        except Exception:
            return img

    def cancel_current(self):
        """杀掉正在跑的 ffmpeg（**不动缓存**）。

        ⚠️ 单任务抢占用：鼠标已经挪走了，这个帧解出来也没用，
        不如立刻杀掉让最新任务立即开始。
        """
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def close(self):
        """杀掉正在跑的 ffmpeg 并清缓存。**退出路径必须调它**，否则线程卡在 communicate。"""
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        with self._lock:
            self._cache.clear()
            self._order.clear()
