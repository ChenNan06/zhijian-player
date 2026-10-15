"""音乐模式视图（v1.0.31 架构级重写：背景零重算 + 单一主帧时钟 + 硬兜底）。

事故回溯（v1.0.26~30 的病灶，逐条对应本文件的写法）：
  1. `paintEvent` 里调 `_rebuild_blur()` ✗ ⇒ 任何 update()（歌词 16ms + 频谱 25ms +
     轮询 100ms，且子控件透明会连带父级重绘）都在重算模糊 + 全窗 blit ✗
     ⇒ 主线程 100% 饱和 ⇒ 全屏卡死/闪退 ✓
     【本文件】背景只在【换封面】或【resize 停稳 200ms 后】生成一次，
       绘制期只有一行 `drawPixmap`（FastTransformation 拉伸），零模糊计算 ✓
  2. 三个独立定时器各自 update() ✗ ⇒ 合并为 AudioView 里**唯一**的主帧时钟（33ms）✓
  3. 歌词折行用 width ≤ 0 计算 ⇒ 高度塌陷 ⇒ 整块不画 ✗
     【本文件】宽度至少 200px、逐行 try/except + `fm.height()+4` 保底 ✓
  4. 频谱没 raise、几何被推出视野、暂停时全隐形 ✗
     【本文件】强制 raise + 固定贴在控制栏上方 + 暂停保留 2px 底线 ✓
"""
from __future__ import annotations

import math
import random
import time

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

BLUR_WORK_W = 160
BG_DEBOUNCE_MS = 200
FRAME_MS = 33
SPEC_BAR_W = 6
SPEC_GAP = 3
SPEC_PITCH = SPEC_BAR_W + SPEC_GAP
SPEC_PEAK_H = 2
SPEC_PEAK_FALL_PX_S = 4.0
SPEC_H = 40
SPEC_SIDE = 40
LYRIC_GROUP_PAD = 29
LYRIC_INNER_PAD = 6
CURRENT_LINE_RATIO = 0.40
FADE_PX = 40
BASE_W, BASE_H = 1200.0, 800.0
SCALE_MIN, SCALE_MAX = 0.85, 1.45


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
    """铺满 (w,h) + 大半径模糊（**只在换封面/停稳后调用**，绝不在绘制期调用）。"""
    if src.isNull() or w <= 1 or h <= 1:
        return QPixmap()
    try:
        filled = src.scaled(QSize(w, h), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = max(0, (filled.width() - w) // 2)
        y = max(0, (filled.height() - h) // 2)
        cropped = filled.copy(x, y, min(w, filled.width()), min(h, filled.height()))
        work_h = max(2, int(BLUR_WORK_W * h / max(1, w)))
        small = cropped.scaled(BLUR_WORK_W, work_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        big = small.scaled(max(w, BLUR_WORK_W), max(h, work_h), Qt.IgnoreAspectRatio,
                           Qt.SmoothTransformation)
        mid = big.scaled(BLUR_WORK_W, work_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return QPixmap.fromImage(mid.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
    except Exception:
        return QPixmap()


def vinyl_placeholder(side: int) -> QPixmap:
    """无封面兜底：黑胶唱片 + 几何音符纹理（绝不留空）。"""
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
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 14), max(1.0, side / 220.0)))
    for i in range(1, 9):
        rr = r * i / 9.0
        p.drawEllipse(c, rr, rr)
    p.setPen(Qt.NoPen)
    grad = QRadialGradient(c, r * 0.42)
    grad.setColorAt(0.0, QColor(150, 158, 170))
    grad.setColorAt(0.6, QColor(96, 104, 118))
    grad.setColorAt(1.0, QColor(58, 62, 72))
    p.setBrush(QBrush(grad))
    p.drawEllipse(c, r * 0.42, r * 0.42)
    p.setBrush(QColor(255, 255, 255, 30))
    p.drawEllipse(c, r * 0.055, r * 0.055)
    p.setPen(QPen(QColor(255, 255, 255, 26), max(1.0, side / 160.0)))
    for i in range(6):
        ang = i * math.pi / 3.0 + 0.4
        p.drawLine(QPointF(c.x() + math.cos(ang) * r * 0.48, c.y() + math.sin(ang) * r * 0.48),
                   QPointF(c.x() + math.cos(ang + 0.5) * r * 0.8,
                           c.y() + math.sin(ang + 0.5) * r * 0.8))
    p.end()
    return pm


class CoverArt(QWidget):
    """专辑封面：正圆角正方形 + 圆角投影（无封面时黑胶占位）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._cover: QPixmap | None = None
        self._scaled = QPixmap()
        self._key = (0, False)

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
        if self._key != (side, self._cover is not None) or self._scaled.isNull():
            if self._cover is not None:
                s = min(self._cover.width(), self._cover.height())
                sq = self._cover.copy((self._cover.width() - s) // 2,
                                      (self._cover.height() - s) // 2, s, s)
                self._scaled = sq.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                self._scaled = vinyl_placeholder(side)
            self._key = (side, self._cover is not None)
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
        R = 12.0
        for grow, alpha in ((3, 22), (7, 13)):
            if not _is_light() and grow > 3:
                continue
            sp = QPainterPath()
            sp.addRoundedRect(QRectF(x - grow + 1.5, y - grow + 3.0,
                                     side + grow * 2, side + grow * 2), R + grow, R + grow)
            p.fillPath(sp, QColor(0, 0, 0, alpha))
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, side, side), R, R)
        p.setClipPath(path)
        p.drawPixmap(QRectF(x, y, side, side), pm, QRectF(pm.rect()))
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + 0.5, y + 0.5, side - 1, side - 1), R, R)
        p.end()


class SpectrumWidget(QWidget):
    """底部频谱：柱宽 6 / 间距 3 / 峰值 2px / N 自适应 / 4px·s⁻¹ 线性落峰。

    **没有自己的定时器**（由主帧时钟调用 `tick()`）；暂停保留 2px 底线，绝不整块隐形。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._n = 0
        self._bars: list[float] = []
        self._peak_px: list[float] = []
        self._phase: list[float] = []
        self._playing = False
        self._energy = 0.0
        self._beat = 0.0
        self.setMinimumHeight(SPEC_H)
        self._ensure(72)

    def _ensure(self, n: int):
        if n == self._n:
            return
        self._n = n
        self._bars = [0.0] * n
        self._peak_px = [0.0] * n
        self._phase = [random.random() * math.tau for _ in range(n)]

    def set_playing(self, on: bool):
        self._playing = bool(on)

    def tick(self):
        w = max(80, self.width())
        self._ensure(max(16, min(160, (w + SPEC_GAP) // SPEC_PITCH)))
        now = time.perf_counter()
        if self._playing:
            self._energy = max(0.0, min(1.0, self._energy * 0.86
                                        + 0.14 * (0.42 + 0.34 * math.sin(now * 1.7)
                                                  + 0.24 * random.random())))
            self._beat = 0.72 * self._beat + 0.28 * (self._energy ** 2.1)
        else:
            self._energy *= 0.80
            self._beat *= 0.78
        fall = SPEC_PEAK_FALL_PX_S * (FRAME_MS / 1000.0)
        hh = max(10, self.height() - 10)
        for i in range(self._n):
            low = 1.0 - i / float(max(1, self._n))
            raw = (self._energy * (0.45 + 0.55 * low)
                   * (0.55 + 0.45 * abs(math.sin(now * (1.3 + low * 3.4) + self._phase[i])))
                   + self._beat * low * 0.55)
            band = max(0.0, min(1.0, raw)) ** 0.5          # 开方压缩
            self._bars[i] = band
            col_h = band * hh
            if col_h >= self._peak_px[i]:
                self._peak_px[i] = col_h
            else:
                self._peak_px[i] = max(0.0, self._peak_px[i] - fall)

    def paintEvent(self, _ev):            # noqa: N802
        w, h = self.width(), self.height()
        if w < 20 or h < 6:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        acc = _accent()
        base = QColor(acc)
        base.setAlpha(70)
        p.setPen(Qt.NoPen)
        p.setBrush(base)
        p.drawRect(QRectF(0, h - 2, w, 2))     # 常驻底线：证明控件真实存在
        self._ensure(max(16, min(160, (max(80, w) + SPEC_GAP) // SPEC_PITCH)))
        total = self._n * SPEC_BAR_W + (self._n - 1) * SPEC_GAP
        x0 = max(0.0, (w - total) / 2.0)
        top = QColor(acc)
        top.setAlpha(150)
        bot = QColor(acc)
        bot.setAlpha(70)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bot)
        p.setBrush(QBrush(grad))
        for i, v in enumerate(self._bars):
            bh = max(2.0, v * (h - 6))
            p.drawRect(QRectF(x0 + i * SPEC_PITCH, h - bh, SPEC_BAR_W, bh))
        pk = QColor(acc)
        pk.setAlpha(190)
        p.setBrush(pk)
        for i, ypx in enumerate(self._peak_px):
            if ypx <= 1.0:
                continue
            p.drawRect(QRectF(x0 + i * SPEC_PITCH, max(0.0, h - ypx - SPEC_PEAK_H),
                              SPEC_BAR_W, SPEC_PEAK_H))
        p.end()


class LyricsPanel(QWidget):
    """滚动歌词：动态折行 + 组内 6px / 组间 29px + 当前行 40% 黄金线。

    **没有自己的定时器**（主帧时钟调用 `step()`）；宽度/高度全部带兜底 ——
    宽度为 0 会让折行高度塌陷、整块不画（v1.0.30 歌词蒸发的根因）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._lines: list[tuple[int, str]] = []
        self._idx = -1
        self._tops: list[float] = []
        self._heights: list[float] = []
        self._offset = 0.0
        self._target = 0.0
        self._spec_text = ""
        self._placeholder = "纯音乐 / 暂无歌词"

    @staticmethod
    def _merge_bilingual(lines):
        out: list[tuple[int, str]] = []
        for t, txt in lines:
            if out and out[-1][0] == t and "\n" not in out[-1][1]:
                out[-1] = (t, out[-1][1] + "\n" + txt)
            else:
                out.append((t, txt))
        return out

    def set_lines(self, lines, spec_text: str = ""):
        self._lines = self._merge_bilingual(list(lines or []))
        self._spec_text = spec_text or ""
        self._idx = -1
        self._offset = 0.0
        self._target = 0.0
        self._relayout()
        self.update()

    def _content_width(self) -> int:
        return max(200, int(self.width()) - 8)

    def _measure(self, text: str, pixel: int, bold: bool, width: int) -> float:
        if not text:
            return float(pixel * 1.6)
        try:
            f = QFont()
            f.setPixelSize(max(8, int(pixel)))
            f.setBold(bool(bold))
            fm = QFontMetrics(f)
            rect = fm.boundingRect(QRect(0, 0, max(200, int(width)), 9999),
                                   Qt.TextWordWrap | Qt.AlignLeft, text)
            h = float(rect.height())
            return h if h > 0 else float(fm.height() + 4)
        except Exception:
            return float(pixel * 1.6 + 4)

    def _relayout(self):
        self._tops, self._heights = [], []
        w = self._content_width()
        sr = 1.0
        try:
            sr = self.parentWidget().scale_ratio()
        except Exception:
            pass
        y = 0.0
        for i, (_t, txt) in enumerate(self._lines):
            cur = (i == self._idx)
            main, trans = (txt.split("\n", 1) if "\n" in txt else (txt, ""))
            h = self._measure(main, (27 if cur else 20) * sr, cur, w)
            if trans:
                h += LYRIC_INNER_PAD + self._measure(trans, (18 if cur else 14) * sr, cur, w)
            h = max(18.0, h)
            self._tops.append(y)
            self._heights.append(h)
            y += h + LYRIC_GROUP_PAD
        self._retarget()

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
            self._relayout()
            self.update()

    def _retarget(self):
        if not self._lines or self._idx < 0 or self._idx >= len(self._tops):
            self._target = 0.0
            return
        gold = self.height() * CURRENT_LINE_RATIO
        self._target = gold - (self._tops[self._idx] + self._heights[self._idx] / 2.0)

    def step(self):
        if not self._lines:
            return
        d = self._target - self._offset
        if abs(d) < 0.4:
            if self._offset != self._target:
                self._offset = self._target
                self.update()
            return
        self._offset += d * 0.22
        self.update()

    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._relayout()
        self.update()

    @staticmethod
    def _col(light: bool, alpha: float, strong: bool = False) -> QColor:
        if strong:
            return QColor(0, 0, 0) if light else QColor(255, 255, 255)
        c = QColor(20, 22, 26) if light else QColor(255, 255, 255)
        c.setAlphaF(max(0.0, min(1.0, alpha)))
        return c

    def paintEvent(self, _ev):            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        w, h = self.width(), self.height()
        light = _is_light()
        if not self._lines:
            f = QFont()
            f.setPixelSize(15)
            p.setFont(f)
            p.setPen(self._col(light, 0.60))
            p.drawText(QRectF(0, h * 0.42 - 20, w, 40), Qt.AlignCenter, self._placeholder)
            if self._spec_text:
                f2 = QFont()
                f2.setPixelSize(12)
                p.setFont(f2)
                tw = p.fontMetrics().horizontalAdvance(self._spec_text) + 24
                rect = QRectF((w - tw) / 2.0, h * 0.42 + 24, tw, 26)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(20, 26, 36, 16) if light else QColor(255, 255, 255, 22))
                p.drawRoundedRect(rect, 13, 13)
                p.setPen(self._col(light, 0.70))
                p.drawText(rect, Qt.AlignCenter, self._spec_text)
            p.end()
            return
        if not self._tops or len(self._tops) != len(self._lines):
            self._relayout()
        p.setClipRect(QRectF(0, 0, w, h))
        sr = 1.0
        try:
            sr = self.parentWidget().scale_ratio()
        except Exception:
            pass
        flag = Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop
        cw = self._content_width()
        for i, (_t, text) in enumerate(self._lines):
            if i >= len(self._tops):
                break
            top = self._offset + self._tops[i]
            hh = self._heights[i]
            if top + hh < -FADE_PX or top > h + FADE_PX:
                continue
            cur = (i == self._idx)
            main, trans = (text.split("\n", 1) if "\n" in text else (text, ""))
            f = QFont()
            f.setPixelSize(max(10, int((27 if cur else 20) * sr)))
            f.setWeight(QFont.Medium if cur else QFont.Normal)
            p.setFont(f)
            main_h = self._measure(main, f.pixelSize(), False, cw)
            if cur:
                p.setPen(QColor(0, 0, 0, 90) if not light else QColor(255, 255, 255, 120))
                p.drawText(QRectF(1.2, top + 1.2, cw, main_h), flag, main)
                p.setPen(self._col(light, 1.0, strong=True))
            else:
                p.setPen(self._col(light, 0.40))
            p.drawText(QRectF(0, top, cw, main_h), flag, main)
            if trans:
                f2 = QFont()
                f2.setPixelSize(max(9, int((18 if cur else 14) * sr)))
                p.setFont(f2)
                p.setPen(self._col(light, 0.90 if cur else 0.28))
                p.drawText(QRectF(0, top + main_h + LYRIC_INNER_PAD, cw,
                                  self._measure(trans, f2.pixelSize(), False, cw)), flag, trans)
        p.end()


class AudioView(QWidget):
    """音乐模式总成：静态背景缓存 + 双栏 + 底部频谱 + **唯一主帧时钟**。"""

    request_drawer = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._cover_data: bytes | None = None
        self._bg_cache = QPixmap()          # ★ 静态背景缓存（绝不每帧重算）
        self._bg_img: QImage | None = None
        self.cover = CoverArt(self)
        self.lyrics = LyricsPanel(self)
        self.spec = SpectrumWidget(self)
        self._title = ""
        self._sub = ""
        self._playing = False
        self._pos_provider = None
        self._cur_pos = 0.0
        self._last_pos: float | None = None
        self._pos_accum = 0
        self._frame = QTimer(self)
        self._frame.setInterval(FRAME_MS)   # ★ 全组件唯一帧时钟
        self._frame.timeout.connect(self._on_frame)
        self._frame.start()
        self._bg_timer = QTimer(self)
        self._bg_timer.setSingleShot(True)
        self._bg_timer.setInterval(BG_DEBOUNCE_MS)
        self._bg_timer.timeout.connect(self._rebuild_bg_now)

    def _on_frame(self):
        try:
            self.spec.tick()
            self.spec.update()
        except Exception:
            pass
        try:
            self.lyrics.step()
        except Exception:
            pass
        self._pos_accum += 1
        if self._pos_accum >= 6:            # ~200ms 取一次播放位置
            self._pos_accum = 0
            self._poll_position()
        # v1.0.31：**位置在推进 = 正在播放** ⇒ 频谱自驱动跳动（不依赖外部接线）
        try:
            if self._last_pos is not None and self._cur_pos > self._last_pos + 0.001:
                if not self._playing:
                    self.set_playing(True)
            elif self._last_pos is not None and self._cur_pos <= self._last_pos:
                if self._playing:
                    self.set_playing(False)
            self._last_pos = self._cur_pos
        except Exception:
            pass

    def _poll_position(self):
        if self._pos_provider is None or not self.isVisible():
            return
        try:
            self.set_position(float(self._pos_provider() or 0.0))
        except Exception:
            pass

    def scale_ratio(self) -> float:
        try:
            r = min(self.width() / BASE_W, self.height() / BASE_H)
        except Exception:
            return 1.0
        return max(SCALE_MIN, min(SCALE_MAX, float(r)))

    def set_position_provider(self, fn):
        self._pos_provider = fn

    def set_position(self, seconds: float):
        """mpv time-pos 是**秒**，歌词时间轴是**毫秒** ⇒ 必须 ×1000 再比。"""
        self._cur_pos = max(0.0, float(seconds))
        self.lyrics.set_position_ms(int(self._cur_pos * 1000))

    def set_cover(self, data: bytes | None):
        self._cover_data = data or None
        self.cover.set_cover(self._cover_data)
        img = None
        if self._cover_data:
            tmp = QImage()
            if tmp.loadFromData(self._cover_data) and not tmp.isNull():
                img = tmp
        self._bg_img = img
        self._rebuild_bg_now()

    def set_meta(self, title: str, sub: str):
        self._title = title or ""
        self._sub = sub or ""
        self.update()

    def set_spec(self, spec: str):
        self.lyrics._spec_text = spec or ""
        self.lyrics.update()

    def set_lyrics(self, audio_path: str):
        lines = []
        try:
            from ..media.lyrics import load_lyrics

            lines, _src = load_lyrics(audio_path)
        except Exception:
            lines = []
        self.lyrics.set_lines(lines, self.lyrics._spec_text)

    def set_playing(self, on: bool):
        self._playing = bool(on)
        self.spec.set_playing(on)

    def _rebuild_bg_now(self):
        """真正重建背景缓存（只在换封面 / resize 停稳后调用，绝不在绘制期）。"""
        w, h = max(2, self.width()), max(2, self.height())
        if self._bg_img is None or self._bg_img.isNull():
            self._bg_cache = QPixmap()
            self.update()
            return
        self._bg_cache = blur_pixmap(self._bg_img, w, h)
        self.update()

    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._layout_children()
        self._bg_timer.start()              # ★ 停稳 200ms 后才重建高清背景

    def _layout_children(self):
        w, h = self.width(), self.height()
        sr = self.scale_ratio()
        content_h = max(120, h - SPEC_H - 60)
        self.spec.setGeometry(SPEC_SIDE, max(0, h - 50 - SPEC_H),
                              max(80, w - SPEC_SIDE * 2), SPEC_H)
        try:
            self.spec.raise_()               # ★ 强制置顶
        except Exception:
            pass
        col_w = max(360, min(int(w * 0.78), w - 40))
        x0 = (w - col_w) // 2
        left_w = int(col_w * 0.40)
        right_w = max(220, col_w - left_w - 32)
        base = int(260 * sr)
        side = max(150, min(int(left_w * 0.86), int(content_h * 0.72), base))
        self.cover.setGeometry(x0 + (left_w - side) // 2, max(20, (content_h - side) // 2),
                               side, side)
        rx = x0 + left_w + 32
        self.lyrics.setGeometry(rx, int(content_h * 0.22), right_w,
                                max(120, content_h - int(content_h * 0.22) - 8))
        self._right_col = (rx, right_w)

    def paintEvent(self, _ev):            # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        light = _is_light()
        # ★★ 铁律：绘制期只有一行贴图（缓存模糊背景 + FastTransformation 拉伸）
        if not self._bg_cache.isNull():
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.drawPixmap(QRect(0, 0, w, h), self._bg_cache, QRect(self._bg_cache.rect()))
        else:
            grad = QLinearGradient(0, 0, 0, h)
            if light:
                grad.setColorAt(0.0, QColor("#F2F5F8"))
                grad.setColorAt(1.0, QColor("#E6ECF2"))
            else:
                grad.setColorAt(0.0, QColor("#1E222A"))
                grad.setColorAt(1.0, QColor("#14161B"))
            p.fillRect(QRect(0, 0, w, h), QBrush(grad))
        p.fillRect(QRect(0, 0, w, h),
                   QColor(245, 247, 250, 199) if light else QColor(14, 16, 20, 173))
        rx, rw = getattr(self, "_right_col", (0, w))
        sr = self.scale_ratio()
        if self._title:
            f = QFont()
            f.setPixelSize(max(14, int(23 * sr)))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(24, 28, 36) if light else QColor(245, 247, 250))
            p.drawText(QRectF(rx, 40, rw, 34), Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(self._title, Qt.ElideRight, rw))
        if self._sub:
            f2 = QFont()
            f2.setPixelSize(max(11, int(14 * sr)))
            p.setFont(f2)
            p.setPen(QColor(70, 78, 90, 210) if light else QColor(200, 208, 220, 190))
            p.drawText(QRectF(rx, 78, rw, 24), Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(self._sub, Qt.ElideRight, rw))
        p.end()
