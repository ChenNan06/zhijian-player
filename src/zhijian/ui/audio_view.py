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
# PotPlayer AudioSkin 实测参数：柱宽 6 / 间距 3（节距 9）/ 峰值方点 2px / 40FPS
SPEC_BAR_W = 6
SPEC_GAP = 3
SPEC_PITCH = SPEC_BAR_W + SPEC_GAP
SPEC_PEAK_H = 2
SPEC_FPS_MS = 25
#: 峰值下落：严格线性重力 2px/500ms = 4 px/s（用户确证值）
SPEC_PEAK_FALL_PX_S = 4.0
SPEC_BARS = 72                     # 兜底值（实际按宽度自适应）
#: 歌词行距与黄金视觉线（当前行锁在歌词区垂直 40%）
#: 单行歌词块高度（双语时上下两行挤在 44px 内）
LYRIC_LINE_H = 46
#: 每一"组"（一句/一组双语）之后的组间距 —— 网易云那种呼吸感的关键（用户指定 24~28）
LYRIC_GROUP_PAD = 29
#: 组内双语的行距（紧凑）
LYRIC_INNER_PAD = 6
CURRENT_LINE_RATIO = 0.40
#: 网易云式弹性基准分辨率与缩放系数上下限（用户指定 0.85 ~ 1.45）
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
        # v1.0.28 ④：阴影与封面**全程只用同一个圆角路径**，不再画任何直角底板。
        #    之前的多层实心圆角矩形在右下角会露出直角尖角（用户红框）。
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
    """底部音频频谱：72 根柱 + 峰值回落。**只重绘自己**（护栏 ②）。

    数据源用轻量能量包络 + 多频段谐振模拟：不发起任何同步 IPC，
    暂停/静音时平滑归零。节拍由能量导数驱动，视觉上跟随音乐起伏。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._n = SPEC_BARS
        self._bars = [0.0] * SPEC_BARS
        self._peaks = [0.0] * SPEC_BARS
        self._phase = [random.random() * math.tau for _ in range(SPEC_BARS)]
        self._playing = False
        self._energy = 0.0
        self._beat = 0.0
        self.setMinimumHeight(56)
        self._timer = QTimer(self)
        # v1.0.29 ⑤：锁定 40 FPS（25ms）局部重绘
        self._timer.setInterval(SPEC_FPS_MS)
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
            # 峰值：柱更高就把峰值顶上去；否则**线性匀速**下落 4 px/s（绝不用指数衰减）
            if not hasattr(self, "_peak_px"):
                self._peak_px = [0.0] * self._n
            col_h = nv * (self.height() - 10)
            fall = SPEC_PEAK_FALL_PX_S * (SPEC_FPS_MS / 1000.0)
            if col_h >= self._peak_px[i]:
                self._peak_px[i] = col_h
                changed = True
            else:
                self._peak_px[i] = max(0.0, self._peak_px[i] - fall)
                if self._peak_px[i] > 0.0:
                    changed = True
            # 频率分箱：对数映射 + 开方压缩（低频不挤、高频不空）
            low = 1.0 - i / float(max(1, self._n))
            band = (nv * (0.45 + 0.55 * low)) ** 0.5
            self._bars[i] = max(0.0, min(1.0, band))
        if changed or self._playing:
            self.update()                          # ★ 只重绘频谱自身（护栏 ②）

    def paintEvent(self, _ev):            # noqa: N802
        h = self.height()
        w = self.width()
        if w < 20 or h < 10:
            return
        # v1.0.29 ⑤：柱数按宽度自适应 N = (w + 间距) // 节距（约 50~70 根）
        want = max(16, min(160, (w + SPEC_GAP) // SPEC_PITCH))
        if want != self._n:
            self._n = want
            self._bars = [0.0] * want
            self._peaks = [0.0] * want
            self._peak_px = [0.0] * want
            self._phase = [random.random() * math.tau for _ in range(want)]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        total = self._n * SPEC_BAR_W + (self._n - 1) * SPEC_GAP
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
        radius = 1.0
        for i, v in enumerate(self._bars):
            bh = max(1.0, v * (h - 10))
            x = x0 + i * SPEC_PITCH
            p.drawRoundedRect(QRectF(x, h - bh, SPEC_BAR_W, bh), radius, radius)
        # 峰值小方点（2px 高，线性匀速下落 4 px/s）
        pk = QColor(acc)
        pk.setAlpha(190)
        p.setBrush(pk)
        for i, y_px in enumerate(getattr(self, "_peak_px", [])):
            if y_px <= 1.0:
                continue
            x = x0 + i * SPEC_PITCH
            p.drawRect(QRectF(x, max(0.0, h - y_px - SPEC_PEAK_H), SPEC_BAR_W, SPEC_PEAK_H))
        p.end()


class LyricsPanel(QWidget):
    """滚动歌词（v1.0.28 重做排版）——对标网易云：组间距 + 强对比。

    排版参数（用户逐条指定）：
      * 组内（双语的原词/翻译之间）：6px 紧凑
      * 组间（上一句与下一句之间）：26px 呼吸间距 —— 这是"不糊成一团"的关键
      * 当前组：主词 23px Bold 纯白 alpha=1.0（浅色 #000000），翻译 17px alpha=0.90
      * 其他组：主词 15px alpha=0.35（浅色 0.38），翻译 13px alpha=0.28
      * 当前行锁定在歌词区垂直 40% 黄金线，16ms 缓动跟随，上下 32px 渐隐
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
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._step)
        self._anim.start()

    @staticmethod
    def _merge_bilingual(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
        """同一时间戳的连续两行合并为一个双语组（主词在上、翻译在下）。

        合并成"一个组"后，组内用紧凑行距、组间用大间距 —— 翻译不会顶走原词，
        也不会把行距搞乱（用户要求）。
        """
        out: list[tuple[int, str]] = []
        for t, txt in lines:
            if out and out[-1][0] == t and "\n" not in out[-1][1]:
                out[-1] = (t, out[-1][1] + "\n" + txt)
            else:
                out.append((t, txt))
        return out

    def set_lines(self, lines: list[tuple[int, str]], spec_text: str = ""):
        self._lines = self._merge_bilingual(list(lines or []))
        self._spec_text = spec_text or ""
        self._idx = -1
        self._offset = 0.0
        self._target = 0.0
        self._relayout()
        self.update()

    def _measure(self, text: str, pixel: int, bold: bool, width: int) -> float:
        """用 QFontMetrics 按可用宽度算**折行后**的真实高度（v1.0.30 ③）。

        之前直接单行 drawText ⇒ 长歌词被硬性截断（用户截图的"..."）。
        """
        f = QFont()
        f.setPixelSize(pixel)
        f.setBold(bold)
        fm = QFontMetrics(f)
        rect = fm.boundingRect(QRect(0, 0, max(40, width), 10000),
                               Qt.TextWordWrap | Qt.AlignLeft, text)
        return float(max(pixel * 1.4, rect.height()))

    def _relayout(self):
        """按折行后的真实高度排布每一组（组内 6px、组间 29px）。"""
        self._tops, self._heights = [], []
        avail = max(120, self.width() - 8)
        sr = 1.0
        try:
            sr = self.parentWidget().scale_ratio()
        except Exception:
            pass
        y = 0.0
        for i, (_t, txt) in enumerate(self._lines):
            cur = (i == self._idx)
            has_trans = "\n" in txt
            main, trans = (txt.split("\n", 1) if has_trans else (txt, ""))
            h = self._measure(main, int((27 if cur else 20) * sr), cur, avail)
            if has_trans:
                h += LYRIC_INNER_PAD + self._measure(trans, int((18 if cur else 14) * sr),
                                                    cur, avail)
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
            self._retarget()
            self.update()

    def _retarget(self):
        if not self._lines or self._idx < 0:
            self._target = 0.0
            return
        gold = self.height() * CURRENT_LINE_RATIO
        self._target = gold - (self._tops[self._idx] + self._heights[self._idx] / 2.0)

    def _step(self):
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
        self._relayout()                   # 宽度变了 ⇒ 折行结果与高度都要重算

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
        fade = 40            # v1.0.29：上下各 40px 渐隐蒙层（用户指定）
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
        if not self._tops:
            self._relayout()
        p.setClipRect(QRectF(0, 0, w, h))
        for i, (_t, text) in enumerate(self._lines):
            top = self._offset + self._tops[i]
            hh = self._heights[i]
            if top + hh < -fade or top > h + fade:
                continue
            cur = (i == self._idx)
            has_trans = "\n" in text
            main, trans = (text.split("\n", 1) if has_trans else (text, ""))
            # ---- 主词 ----
            sr = 1.0
            try:
                sr = self.parentWidget().scale_ratio()
            except Exception:
                pass
            f = QFont()
            # 网易云实测：当前 27px、未唱 20px（再乘弹性系数）
            f.setPixelSize(max(12, int((27 if cur else 20) * sr)))
            if cur:
                f.setWeight(QFont.Medium)     # 用户要求：Medium，严禁粗体发糊
            else:
                f.setBold(False)
            p.setFont(f)
            p.setPen(self._col(light, 1.0 if cur else 0.40, strong=cur))
            main_h = self._measure(main, f.pixelSize(), cur, max(120, w - 8))
            # 当前行加一层淡影防撞色（亮封面上也读得清）
            flag = Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop
            if cur:
                sh = QColor(0, 0, 0, 90) if not light else QColor(255, 255, 255, 120)
                p.setPen(sh)
                p.drawText(QRectF(1.2, top + 1.2, w, main_h), flag, main)
                p.setPen(self._col(light, 1.0, strong=cur))
            p.drawText(QRectF(0, top, w, main_h), flag, main)
            # ---- 翻译（组内紧凑：紧跟在主词下方）----
            if trans:
                f2 = QFont()
                f2.setPixelSize(max(10, int((18 if cur else 14) * sr)))
                f2.setBold(cur)
                p.setFont(f2)
                p.setPen(self._col(light, 0.90 if cur else 0.28))
                p.drawText(QRectF(0, top + main_h + LYRIC_INNER_PAD, w,
                                   self._measure(trans, f2.pixelSize(), cur, max(120, w - 8))),
                           flag, trans)
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
        # v1.0.28 ②：歌词**自驱动** —— 面板自己按 100ms 读"当前播放位置"，
        # 不再依赖主窗口那条定时器/信号链（那条链断一次歌词就整体不滚，
        # 用户实测"歌词完全不会滚动"）。position 提供者由主窗口注入。
        self._pos_provider = None
        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(100)
        self._pos_timer.timeout.connect(self._poll_position)
        self._pos_timer.start()
        self._font_title = QFont()
        self._font_title.setPixelSize(23)
        self._font_title.setBold(True)
        self._font_sub = QFont()
        self._font_sub.setPixelSize(14)

    def scale_ratio(self) -> float:
        """弹性缩放系数：`min(w/1200, h/800)` 限制在 0.85~1.45。

        这是"全屏不放大的根因"的解药：之前封面尺寸与字号全是**写死的绝对像素**，
        窗口/全屏变化时纹丝不动；现在几何与字号都乘这个系数，全屏时大气舒展、
        普通窗口时精致紧凑（对齐网易云 PC 以 10px 根 Em 等比缩放的机制）。
        """
        try:
            r = min(self.width() / BASE_W, self.height() / BASE_H)
        except Exception:
            return 1.0
        return max(SCALE_MIN, min(SCALE_MAX, float(r)))

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

    def set_position_provider(self, fn):
        """注入"当前播放位置（秒）"的取值函数（由主窗口提供，内部走 engine.cached）。"""
        self._pos_provider = fn

    def _poll_position(self):
        if self._pos_provider is None or not self.isVisible():
            return
        try:
            sec = float(self._pos_provider() or 0.0)
        except Exception:
            return
        self.set_position(sec)

    def set_position(self, seconds: float):
        """⚠️ mpv 的 time-pos 是**秒**（108.5），歌词时间轴是**毫秒**（108500）——
        这里必须显式 *1000 再比，否则永远停在第一行（用户实测的死锁点）。
        """
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
        # v1.0.30 ④：频谱固定在底部、**强制置顶**（之前被封面/歌词盖住 ⇒ 截图里一根都看不到）
        self.spec.setGeometry(40, max(0, h - spec_h - 10), max(80, w - 80), spec_h)
        try:
            self.spec.raise_()
        except Exception:
            pass
        sr = self.scale_ratio()
        col_w = int(w * 0.78)                   # 双栏整体占视口 75%~82%
        col_w = max(360, min(col_w, w - 40))
        x0 = (w - col_w) // 2
        left_w = int(col_w * 0.40)
        right_w = col_w - left_w - 32
        # 封面基准 260px × 弹性系数（全屏可到 360px+），仍受可用空间约束
        base = int(260 * sr)
        side = max(150, min(int(left_w * 0.86), int(content_h * 0.72), base))
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
        scrim = QColor(245, 247, 250, 199) if light else QColor(14, 16, 20, 173)
        p.fillRect(QRect(0, 0, w, h), scrim)
        # ---- 右栏文案（歌曲信息）----
        rx, rw = getattr(self, "_right_col", (0, w))
        sr = self.scale_ratio()
        ft = QFont(self._font_title)
        ft.setPixelSize(max(14, int(23 * sr)))       # 23px × scale_ratio
        fs = QFont(self._font_sub)
        fs.setPixelSize(max(11, int(14 * sr)))       # 14px × scale_ratio
        if self._title:
            p.setFont(ft)
            p.setPen(QColor(24, 28, 36) if light else QColor(245, 247, 250))
            fm = p.fontMetrics()
            title = fm.elidedText(self._title, Qt.ElideRight, rw)
            p.drawText(QRectF(rx, 40, rw, 32), Qt.AlignVCenter | Qt.AlignLeft, title)
        if self._sub:
            p.setFont(fs)
            p.setPen(QColor(70, 78, 90, 210) if light else QColor(200, 208, 220, 190))
            fm = p.fontMetrics()
            p.drawText(QRectF(rx, 74, rw, 22), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(self._sub, Qt.ElideRight, rw))
        p.end()
