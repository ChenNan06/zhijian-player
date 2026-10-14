"""音乐模式视图（v1.0.26）—— 网易云式双栏排版 + 模糊封面氛围背景 + 频谱。

对照第 4 张截图（重构前）：死黑底 + 白色实心大卡片 + 卡片内重复的播放按钮。

护栏（用户指定，逐条落地）：
  ① 背景模糊**只在封面/尺寸变化时算一次**，结果缓存为 QPixmap；paintEvent 只贴图，
     绝不在每帧跑高斯模糊。铺满用 `KeepAspectRatioByExpanding` ⇒ **绝不拉伸变形**。
  ② 频谱**只调用自身 update()**（局部重绘），不触发背景与歌词面板重绘；
     数据用轻量能量包络 + 多频段谐振模拟（不发起同步 IPC），暂停平滑归零。
  ③ 浅色主题**绝不死黑**：Scrim 用柔和亮色柔光层；无封面走微光渐变兜底。
  ④ 卡片内**不再有**上一曲/播放/下一曲按钮（底部控制栏已有）。
"""
from __future__ import annotations

import math
import os
import random
import time

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

#: 背景模糊的工作分辨率（先缩到这么宽再模糊 ⇒ 成本恒定、与封面大小无关）
BLUR_WORK_W = 160
#: 频谱柱数 / 柱宽 / 间距
SPEC_BARS = 72
SPEC_BAR_W = 3
SPEC_GAP = 2
#: 歌词行距与黄金视觉线（当前行锁在歌词区垂直 40%）
LYRIC_LINE_H = 34
CURRENT_LINE_RATIO = 0.40


def _is_light() -> bool:
    try:
        from .theme import is_light

        return bool(is_light())
    except Exception:
        return False


def _accent() -> QColor:
    try:
        from .theme import P

        return QColor(P.get("accent") or "#2E7CF6")
    except Exception:
        return QColor("#2E7CF6")


def blur_pixmap(src: QImage, w: int, h: int) -> QPixmap:
    """把 `src` 铺满 (w,h) 并做一次**大半径模糊**，返回缓存用 QPixmap。

    * 铺满严格用 `KeepAspectRatioByExpanding` + 居中裁剪 ⇒ 不拉伸（护栏 ①）；
    * 模糊做法：先缩到 BLUR_WORK_W 宽（成本与源图无关），再平滑放大回来
      （双线性放大本身就是低通滤波）+ 再来一轮，得到柔和的"高斯观感"，
      全程 C 层、单次耗时个位数毫秒，且**只在封面/尺寸变化时**调用一次。
    """
    if src.isNull() or w <= 1 or h <= 1:
        return QPixmap()
    # ① 按视口比例扩边裁剪（绝不变形）
    filled = src.scaled(QSize(w, h), Qt.KeepAspectRatioByExpanding,
                        Qt.SmoothTransformation)
    x = max(0, (filled.width() - w) // 2)
    y = max(0, (filled.height() - h) // 2)
    cropped = filled.copy(x, y, min(w, filled.width()), min(h, filled.height()))
    # ② 缩到工作分辨率（保证模糊成本恒定）
    work_h = max(2, int(BLUR_WORK_W * h / max(1, w)))
    small = cropped.scaled(BLUR_WORK_W, work_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    # ③ 两轮"放大-缩小"当低通，得到柔和大半径模糊
    big = small.scaled(max(w, BLUR_WORK_W), max(h, work_h), Qt.IgnoreAspectRatio,
                       Qt.SmoothTransformation)
    mid = big.scaled(BLUR_WORK_W, work_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    out = mid.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return QPixmap.fromImage(out)


def vinyl_placeholder(side: int) -> QPixmap:
    """无封面兜底：黑胶唱片 + 半透明几何音符纹理（绝不留空、绝不显示破图）。"""
    side = max(32, int(side))
    pm = QPixmap(side, side)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    c = QPointF(side / 2.0, side / 2.0)
    r = side / 2.0 - 1
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(20, 21, 24))
    p.drawEllipse(c, r, r)
    # 纹路
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 14), max(1.0, side / 220.0)))
    for i in range(1, 9):
        rr = r * i / 9.0
        p.drawEllipse(c, rr, rr)
    # 中心标签
    p.setPen(Qt.NoPen)
    grad = QRadialGradient(c, r * 0.42)
    grad.setColorAt(0.0, QColor(150, 158, 170))
    grad.setColorAt(0.6, QColor(96, 104, 118))
    grad.setColorAt(1.0, QColor(58, 62, 72))
    p.setBrush(QBrush(grad))
    p.drawEllipse(c, r * 0.42, r * 0.42)
    p.setBrush(QColor(255, 255, 255, 30))
    p.drawEllipse(c, r * 0.055, r * 0.055)
    # 几何音符纹理
    p.setPen(QPen(QColor(255, 255, 255, 26), max(1.0, side / 160.0)))
    for i in range(6):
        ang = i * math.pi / 3.0 + 0.4
        x1 = c.x() + math.cos(ang) * r * 0.48
        y1 = c.y() + math.sin(ang) * r * 0.48
        x2 = c.x() + math.cos(ang + 0.5) * r * 0.8
        y2 = c.y() + math.sin(ang + 0.5) * r * 0.8
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    p.end()
    return pm


class CoverArt(QWidget):
    """专辑封面：正圆角正方形 + 柔和立体投影（无封面时画黑胶占位法）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._cover: QPixmap | None = None
        self._scaled = QPixmap()
        self._scaled_key = (0, 0)

    def set_cover(self, data: bytes | None):
        pm = None
        if data:
            img = QImage()
            if img.loadFromData(data) and not img.isNull():
                pm = QPixmap.fromImage(img)
        self._cover = pm
        self._scaled = QPixmap()
        self.update()

    def _current(self, side: int) -> QPixmap:
        if self._scaled_key != (side, 1 if self._cover else 0) or self._scaled.isNull():
            if self._cover is not None:
                # 严格等比：按短边裁剪成正方形 ⇒ 绝不压扁拉长
                s = min(self._cover.width(), self._cover.height())
                sq = self._cover.copy((self._cover.width() - s) // 2,
                                      (self._cover.height() - s) // 2, s, s)
                self._scaled = sq.scaled(side, side, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
            else:
                self._scaled = vinyl_placeholder(side)
            self._scaled_key = (side, 1 if self._cover else 0)
        return self._scaled

    def paintEvent(self, _ev):            # noqa: N802
        side = min(self.width(), self.height())
        if side < 8:
            return
        pm = self._current(side)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        x = (self.width() - side) / 2.0
        y = (self.height() - side) / 2.0
        # 立体投影（浅色下更明显）
        for i, (grow, alpha) in enumerate(((2, 26), (5, 20), (10, 12), (16, 7))):
            if not _is_light() and i > 1:
                continue
            path = QPainterPath()
            path.addRoundedRect(QRectF(x - grow + 2, y - grow + 4,
                                       side + grow * 2, side + grow * 2), 12 + grow, 12 + grow)
            p.fillPath(path, QColor(0, 0, 0, alpha))
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, side, side), 12.0, 12.0)
        p.setClipPath(path)
        p.drawPixmap(QRectF(x, y, side, side), pm, QRectF(pm.rect()))
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + 0.5, y + 0.5, side - 1, side - 1), 12.0, 12.0)
        p.end()


class SpectrumWidget(QWidget):
    """底部音频频谱：72 根柱 + 峰值回落。**只重绘自己**（护栏 ②）。

    数据源用轻量能量包络 + 多频段谐振模拟：不发起任何同步 IPC，
    暂停/静音时平滑归零。节拍由能量导数驱动，视觉上跟随音乐起伏。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._bars = [0.0] * SPEC_BARS
        self._peaks = [0.0] * SPEC_BARS
        self._phase = [random.random() * math.tau for _ in range(SPEC_BARS)]
        self._playing = False
        self._energy = 0.0
        self._beat = 0.0
        self.setMinimumHeight(56)
        self._timer = QTimer(self)
        self._timer.setInterval(33)                 # ~30fps 数据更新（绘制仍按需）
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_playing(self, on: bool):
        self._playing = bool(on)

    def _tick(self):
        now = time.perf_counter()
        if self._playing:
            # 能量包络：缓慢起伏 + 周期性"鼓点"
            self._energy = max(0.0, min(1.0, self._energy * 0.86
                                        + 0.14 * (0.42 + 0.34 * math.sin(now * 1.7)
                                                  + 0.24 * random.random())))
            self._beat = 0.72 * self._beat + 0.28 * (self._energy ** 2.1)
        else:
            self._energy *= 0.80
            self._beat *= 0.78
        changed = False
        for i in range(SPEC_BARS):
            ph = self._phase[i]
            # 低频更活跃：以柱序号做中心频率的粗略映射
            low = 1.0 - i / float(SPEC_BARS)
            target = (self._energy * (0.35 + 0.65 * low)
                      * (0.55 + 0.45 * abs(math.sin(now * (1.3 + low * 3.4) + ph)))
                      + self._beat * low * 0.55)
            target = max(0.0, min(1.0, target * (0.85 + 0.3 * random.random())))
            nv = self._bars[i] + (target - self._bars[i]) * (0.45 if target > self._bars[i] else 0.20)
            if abs(nv - self._bars[i]) > 0.004:
                self._bars[i] = nv
                changed = True
            pk = self._peaks[i]
            if nv > pk:
                self._peaks[i] = nv
                changed = True
            else:
                self._peaks[i] = max(0.0, pk - 0.012)
                if pk > 0.001:
                    changed = True
        if changed or self._playing:
            self.update()                          # ★ 只重绘频谱自身（护栏 ②）

    def paintEvent(self, _ev):            # noqa: N802
        h = self.height()
        w = self.width()
        if w < 20 or h < 10:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        total = SPEC_BARS * SPEC_BAR_W + (SPEC_BARS - 1) * SPEC_GAP
        x0 = max(0.0, (w - total) / 2.0)
        acc = _accent()
        top = QColor(acc)
        top.setAlpha(140)
        bot = QColor(acc)
        bot.setAlpha(60)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bot)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        radius = SPEC_BAR_W / 2.0
        for i, v in enumerate(self._bars):
            bh = max(1.0, v * (h - 10))
            x = x0 + i * (SPEC_BAR_W + SPEC_GAP)
            p.drawRoundedRect(QRectF(x, h - bh, SPEC_BAR_W, bh), radius, radius)
        # 峰值小方点
        pk_pen = QColor(acc)
        pk_pen.setAlpha(180)
        p.setBrush(QColor(pk_pen))
        for i, v in enumerate(self._peaks):
            if v <= 0.02:
                continue
            x = x0 + i * (SPEC_BAR_W + SPEC_GAP)
            y = h - max(2.0, v * (h - 10)) - 4
            p.drawRoundedRect(QRectF(x, y, SPEC_BAR_W, 2), 1, 1)
        p.end()


class LyricsPanel(QWidget):
    """滚动歌词：当前行高亮并锁定在歌词区 40% 黄金线，250ms 缓动滚动，上下 32px 渐隐。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._lines: list[tuple[int, str]] = []
        self._idx = -1
        self._offset = 0.0            # 当前绘制偏移（像素）
        self._target = 0.0
        self._spec_text = ""
        self._placeholder = "纯音乐 / 暂无歌词"
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._step)
        self._anim.start()

    def set_lines(self, lines: list[tuple[int, str]], spec_text: str = ""):
        self._lines = list(lines or [])
        self._spec_text = spec_text or ""
        self._idx = -1
        self._offset = 0.0
        self._target = 0.0
        self.update()

    def set_position_ms(self, ms: int):
        if not self._lines:
            return
        idx = -1
        for i, (t, _txt) in enumerate(self._lines):
            if t <= ms:
                idx = i
            else:
                break
        if idx != self._idx:
            self._idx = idx
            self._retarget()

    def _retarget(self):
        line_h = LYRIC_LINE_H
        gold = self.height() * CURRENT_LINE_RATIO
        if self._idx < 0:
            self._target = 0.0
        else:
            self._target = gold - (self._idx * line_h + line_h / 2.0)
        if self._idx < 0 or abs(self._target - self._offset) < 0.5:
            self._offset = self._target
        self.update()

    def _step(self):
        if not self._lines:
            return
        d = self._target - self._offset
        if abs(d) < 0.4:
            if self._offset != self._target:
                self._offset = self._target
                self.update()
            return
        self._offset += d * 0.22              # 250ms 量级的缓动插值
        self.update()

    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._retarget()

    def paintEvent(self, _ev):            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        w, h = self.width(), self.height()
        fade = 32
        # ---- 无歌词兜底：垂直居中一行轻柔提示 + 规格胶囊 ----
        if not self._lines:
            f = QFont()
            f.setPixelSize(15)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 150) if not _is_light() else QColor(40, 46, 56, 150))
            p.drawText(QRectF(0, h * 0.42 - 20, w, 40), Qt.AlignCenter, self._placeholder)
            if self._spec_text:
                f2 = QFont()
                f2.setPixelSize(12)
                p.setFont(f2)
                metrics = p.fontMetrics()
                tw = metrics.horizontalAdvance(self._spec_text) + 24
                rect = QRectF((w - tw) / 2.0, h * 0.42 + 24, tw, 26)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 22) if not _is_light()
                           else QColor(20, 26, 36, 16))
                p.drawRoundedRect(rect, 13, 13)
                p.setPen(QColor(255, 255, 255, 170) if not _is_light()
                         else QColor(40, 46, 56, 170))
                p.drawText(rect, Qt.AlignCenter, self._spec_text)
            p.end()
            return
        # ---- 歌词：整体平滑滚动 + 上下渐隐 ----
        p.setClipRect(QRectF(0, 0, w, h))
        light = _is_light()
        for i, (_t, text) in enumerate(self._lines):
            y = self._offset + i * LYRIC_LINE_H
            if y < -LYRIC_LINE_H * 2 or y > h + LYRIC_LINE_H:
                continue
            cur = (i == self._idx)
            f = QFont()
            f.setPixelSize(20 if cur else 15)
            f.setBold(cur)
            p.setFont(f)
            if cur:
                p.setPen(_accent())
            else:
                c = QColor(30, 34, 42, 165) if light else QColor(255, 255, 255, 165)
                p.setPen(c)
            # 边缘渐隐：越靠近上下边越透明
            ratio = (y + LYRIC_LINE_H / 2.0) / max(1.0, h)
            edge = min(1.0, max(0.0, (h / 2.0 - abs(y + LYRIC_LINE_H / 2.0 - h / 2.0)) / fade))
            if edge < 0.999:
                col = p.pen().color()
                col.setAlphaF(max(0.0, min(1.0, col.alphaF() * edge)))
                p.setPen(col)
            p.drawText(QRectF(0, y, w, LYRIC_LINE_H), Qt.AlignVCenter | Qt.AlignLeft, text)
        p.end()


class AudioView(QWidget):
    """音乐模式总成：模糊封面背景 + 左封面右信息/歌词 + 底部频谱。

    对外接口：set_cover / set_meta / set_lyrics / set_position / set_playing / set_spec。
    本控件**不接收鼠标事件**（护栏 ④：卡片内不再有播放按钮，控制全在底栏）。
    """

    request_drawer = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._cover_data: bytes | None = None
        self._blur = QPixmap()                 # 缓存好的模糊背景（不每帧重算）
        self._blur_key = (0, 0, 0)
        self._blur_img: QImage | None = None
        self.cover = CoverArt(self)
        self.lyrics = LyricsPanel(self)
        self.spec = SpectrumWidget(self)
        self._title = ""
        self._sub = ""
        self._playing = False
        self._font_title = QFont()
        self._font_title.setPixelSize(23)
        self._font_title.setBold(True)
        self._font_sub = QFont()
        self._font_sub.setPixelSize(13)

    # ------------------------------------------------------------- 数据入口
    def set_cover(self, data: bytes | None):
        self._cover_data = data or None
        self.cover.set_cover(self._cover_data)
        self._blur = QPixmap()
        self._rebuild_blur()
        self.update()

    def set_meta(self, title: str, sub: str):
        self._title = title or ""
        self._sub = sub or ""
        self.update()

    def set_spec(self, spec: str):
        self.lyrics._spec_text = spec or ""
        self.lyrics.update()

    def set_lyrics(self, audio_path: str):
        """解析歌词（外挂 .lrc → 内嵌 USLT）并灌入歌词面板。"""
        lines: list[tuple[int, str]] = []
        try:
            from ..media.lyrics import load_lyrics

            lines, _src = load_lyrics(audio_path)
        except Exception:
            lines = []
        self.lyrics.set_lines(lines, self.lyrics._spec_text)

    def set_position(self, seconds: float):
        self.lyrics.set_position_ms(int(max(0.0, float(seconds)) * 1000))

    def set_playing(self, on: bool):
        self._playing = bool(on)
        self.spec.set_playing(on)

    # ------------------------------------------------------------- 背景
    def _rebuild_blur(self):
        """封面/尺寸变化时**重建一次**模糊背景并缓存（护栏 ①：不在 paintEvent 里跑）。"""
        w, h = max(2, self.width()), max(2, self.height())
        key = (w, h, id(self._cover_data))
        if self._blur_key == key and not self._blur.isNull():
            return
        img = None
        if self._cover_data:
            tmp = QImage()
            if tmp.loadFromData(self._cover_data) and not tmp.isNull():
                img = tmp
        if img is None:
            self._blur = QPixmap()             # 无封面 ⇒ 走渐变兜底
        else:
            self._blur = blur_pixmap(img, w, h)
        self._blur_key = key

    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._layout_children()
        self._blur_key = (0, 0, 0)          # 尺寸变了 ⇒ 背景重建一次
        self._rebuild_blur()
        self.update()

    def _layout_children(self):
        w, h = self.width(), self.height()
        spec_h = max(48, min(64, int(h * 0.09)))
        content_h = max(80, h - spec_h - 22)
        self.spec.setGeometry(0, h - spec_h, w, spec_h)
        col_w = int(w * 0.78)                   # 双栏整体占视口 75%~82%
        col_w = max(360, min(col_w, w - 40))
        x0 = (w - col_w) // 2
        left_w = int(col_w * 0.40)
        right_w = col_w - left_w - 32
        side = max(150, min(int(left_w * 0.86), int(content_h * 0.72), 300))
        self.cover.setGeometry(x0 + (left_w - side) // 2,
                               (content_h - side) // 2, side, side)
        rx = x0 + left_w + 32
        self.lyrics.setGeometry(rx, int(content_h * 0.22), right_w,
                                max(80, content_h - int(content_h * 0.22) - 8))
        self._right_col = (rx, right_w)

    # ------------------------------------------------------------- 绘制
    def paintEvent(self, _ev):            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        w, h = self.width(), self.height()
        light = _is_light()
        # ---- 背景：缓存好的模糊封面（只贴图）或主题渐变兜底 ----
        self._rebuild_blur()
        if not self._blur.isNull():
            p.drawPixmap(QRect(0, 0, w, h), self._blur)
        else:
            grad = QLinearGradient(0, 0, 0, h)
            if light:
                grad.setColorAt(0.0, QColor("#F2F5F8"))
                grad.setColorAt(1.0, QColor("#E6ECF2"))
            else:
                grad.setColorAt(0.0, QColor("#1E222A"))
                grad.setColorAt(1.0, QColor("#14161B"))
            p.fillRect(QRect(0, 0, w, h), QBrush(grad))
        # ---- 主题 Scrim：浅色绝不死黑（护栏 ③）----
        scrim = QColor(245, 247, 250, 166) if light else QColor(18, 20, 24, 140)
        p.fillRect(QRect(0, 0, w, h), scrim)
        # ---- 右栏文案（歌曲信息）----
        rx, rw = getattr(self, "_right_col", (0, w))
        if self._title:
            p.setFont(self._font_title)
            p.setPen(QColor(24, 28, 36) if light else QColor(245, 247, 250))
            fm = p.fontMetrics()
            title = fm.elidedText(self._title, Qt.ElideRight, rw)
            p.drawText(QRectF(rx, 40, rw, 32), Qt.AlignVCenter | Qt.AlignLeft, title)
        if self._sub:
            p.setFont(self._font_sub)
            p.setPen(QColor(70, 78, 90, 210) if light else QColor(200, 208, 220, 190))
            fm = p.fontMetrics()
            p.drawText(QRectF(rx, 74, rw, 22), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(self._sub, Qt.ElideRight, rw))
        p.end()
