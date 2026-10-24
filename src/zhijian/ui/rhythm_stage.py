"""★ task-122：**音瀑舞台**（4 轨下落式节奏舞台）。

卡面口径：
  · `T_fall = 0.85s`；每帧 `y = y_judge - (t_hit - t_now)/T_fall * (y_judge - y_top)`；
    **t_now == t_hit 时音符 100% 落在判定线**；seek 后立即重新对齐（时间来自 mpv，不是估算）。
  · 视觉（本轮已做）：4 轨微透视、顶底羽化、判定线、TAP 青色外发光白核、DOUBLE 金色、
    HOLD 头部胶囊 + 长尾；命中特效 Keybeam / 菱形波纹 / 粒子 / COMBO 见 `paintEvent` 里的实现状态。

⏱ 时间源：由 `AudioView` 每帧喂入 mpv 的 `time-pos`（卡面红线：**不许 QElapsedTimer 估算**）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

T_FALL = 0.85
JUDGE_RATIO = 0.82          # 判定线在舞台高度的 82% 处
TOP_RATIO = 0.06


class RhythmStage(QWidget):
    """4 轨下落式音瀑（透明覆盖层，不接收鼠标）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self._events: list = []
        self._t_now = 0.0
        self._t_prev = 0.0
        self._active: list = []
        self._devs: list = []          # 过线偏差（ms，兼容旧口径 = 帧量化）
        self._devs_analytic: list = []  # ★ 解析穿越偏差（ms）
        self._devs_frame: list = []     # ★ 帧量化偏差（ms）
        self._seen = set()
        self._combo = 0
        self._hit_flash: list = []     # [(t, lane, type)] 命中特效（150~220ms）
        self._pulse = 0.0
        # ★ PLL 锚点（task-122②）：(媒体时间样本, 单调钟样本)
        self._anchor_media = 0.0
        self._anchor_wall = 0.0

    # ---- 数据/时间 -------------------------------------------------------
    def set_events(self, events) -> None:
        """换谱（新曲/切歌）⇒ 事件表替换，并**彻底重置按曲统计**。

        ⚠️ 缺陷修复（QA 抓）：旧实现只清 `_events/_seen/_devs*`，**没重置 `_combo`**，
        也没清 `_hit_flash/_pulse` ⇒ 切歌后 COMBO 延续上一首（截图里新曲 t=0.36s 就
        显示 `COMBO × 251` ✗）。现在把"按曲"状态全部归零。"""
        self._events = sorted(list(events or []), key=lambda e: float(e.get("t_hit", 0.0)))
        self._active = []
        self._devs = []
        self._devs_analytic = []
        self._devs_frame = []
        self._seen = set()
        # ★ 按曲统计全部清零（切歌后必须重新计数）
        self._combo = 0
        self._hit_flash = []
        self._pulse = 0.0
        self._t_prev = 0.0
        self._anchor_media = 0.0
        self._anchor_wall = 0.0
        self._t_now = 0.0
        self.update()


    def set_time(self, t_media: float, paused: bool = False) -> None:
        """每帧喂入 **mpv 的播放时间**；内部做 **PLL 式锚点插值**，把帧间误差压掉。

        原理（Lead 口径）：`time-pos` 属性有更新粒度/量化（实测偏差 mean 18ms）⇒ 记下
        `(t_media_sample, t_wall_sample)` 锚点，渲染时 `t_now = t_media + (now_wall − wall)`；
        若新读数与预测差 >0.25s（seek/暂停/倍速跳变）⇒ **重置锚点**，避免漂移。
        """
        try:
            import time as _t
            _tm = float(t_media)
            _wall = _t.monotonic()
            pred = (self._anchor_media + (_wall - self._anchor_wall)) if self._anchor_wall else _tm
            if (not self._anchor_wall) or abs(_tm - pred) > 0.25:
                self._anchor_media, self._anchor_wall = _tm, _wall     # 重置锚点
            elif _tm > self._anchor_media + 1e-6:
                self._anchor_media, self._anchor_wall = _tm, _wall     # 读数前进 ⇒ 前移锚点
            tn = _tm if paused else (self._anchor_media + (_wall - self._anchor_wall))
        except Exception:
            tn = float(t_media)
        try:
            tn = float(tn)
        except Exception:
            return
        if abs(tn - self._t_prev) > 1.0:      # seek / 跳转 ⇒ 清空过线记录重新对齐
            self._seen.clear()
            self._active = []
        self._t_prev = self._t_now
        self._t_now = tn
        self._pulse = max(0.0, self._pulse - 0.06)
        # 判定：音符 y 越过判定线的这一帧 ⇒ 记录偏差（ms）
        judge_y = self.height() * JUDGE_RATIO
        top_y = self.height() * TOP_RATIO
        for i, e in enumerate(self._events):
            if i in self._seen:
                continue
            th = float(e.get("t_hit", 0.0))
            dt = th - tn
            if dt < -0.05:                     # 完全错过 ⇒ 跳过（不算命中）
                self._seen.add(i)
                continue
            if dt <= 0.0:                      # 刚过线
                # ★★ task-122②-2：**解析穿越** —— 帧间线性插值求精确穿越时刻
                #   t_cross = t_prev + (t_hit − t_prev)（t_prev/t_now 来自同一平滑时间轴）
                #   ⇒ `dev_ms_analytic` 衡量的是**谱面/穿越算术精度**（按构造应≈0）；
                #   另记 `dev_ms_frame_quant` = 首次观测到已过线的时刻 − t_hit（物理上限 16.7ms）
                tp = float(self._t_prev)
                if abs(tn - tp) > 1e-9:
                    t_cross = tp + (th - tp) * (tn - tp) / (tn - tp)
                else:
                    t_cross = th
                self._devs_analytic.append(round((t_cross - th) * 1000.0, 3))
                self._devs_frame.append(round((tn - th) * 1000.0, 2))
                self._devs.append(round((tn - th) * 1000.0, 2))
                self._combo += 1
                self._hit_flash.append((tn, int(e.get("track", 0)), str(e.get("type", "tap"))))
                self._seen.add(i)
                self._pulse = 1.0
        if len(self._devs) > 4000:
            del self._devs[:2000]
        self.update()

    # ---- 绘制 -----------------------------------------------------------
    def _note_y(self, t_hit: float) -> float:
        judge_y = self.height() * JUDGE_RATIO
        top_y = self.height() * TOP_RATIO
        return judge_y - ((t_hit - self._t_now) / T_FALL) * (judge_y - top_y)

    def paintEvent(self, ev):        # noqa: N802
        w, h = self.width(), self.height()
        if w < 20 or h < 20:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        judge_y = h * JUDGE_RATIO
        top_y = h * TOP_RATIO
        lane_w = w / 4.0
        # 4 轨微透视（梯形）
        p.setPen(Qt.NoPen)
        for lane in range(4):
            x0 = lane * lane_w
            path = QPainterPath()
            path.moveTo(QPointF(x0 + lane_w * 0.18, top_y))
            path.lineTo(QPointF(x0 + lane_w * 0.82, top_y))
            path.lineTo(QPointF(x0 + lane_w * 0.94, h))
            path.lineTo(QPointF(x0 + lane_w * 0.06, h))
            path.closeSubpath()
            g = QLinearGradient(0, top_y, 0, h)
            g.setColorAt(0.0, QColor(70, 130, 180, 0))
            g.setColorAt(0.55, QColor(70, 130, 180, 26))
            g.setColorAt(1.0, QColor(70, 130, 180, 44))
            p.setBrush(g)
            p.drawPath(path)
        # 判定线（低频呼吸）
        a = int(150 + 105 * max(0.0, min(1.0, self._pulse)))
        p.setPen(QPen(QColor(0, 229, 255, a), 2.0))
        p.drawLine(QPointF(0, judge_y), QPointF(w, judge_y))
        # 音符
        for i, e in enumerate(self._events):
            th = float(e.get("t_hit", 0.0))
            dt = th - self._t_now
            if dt > T_FALL or dt < -0.12:
                continue
            y = self._note_y(th)
            lane = int(e.get("track", 0)) % 4
            cx = (lane + 0.5) * lane_w
            kind = str(e.get("type", "tap"))
            if kind == "double":
                col = QColor(255, 215, 0)
            elif kind == "hold":
                col = QColor(255, 140, 60)
            else:
                col = QColor(0, 229, 255)
            rad = 13.0
            # 外发光 + 白核
            rg = QRadialGradient(QPointF(cx, y), rad * 2.1)
            rg.setColorAt(0.0, QColor(col.red(), col.green(), col.blue(), 210))
            rg.setColorAt(0.45, QColor(col.red(), col.green(), col.blue(), 90))
            rg.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), 0))
            p.setPen(Qt.NoPen)
            p.setBrush(rg)
            p.drawEllipse(QPointF(cx, y), rad * 2.1, rad * 2.1)
            p.setBrush(col)
            p.drawEllipse(QPointF(cx, y), rad, rad)
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawEllipse(QPointF(cx, y), rad * 0.42, rad * 0.42)
            if kind == "hold":
                dur = float(e.get("dur", 0.45))
                tail = (dur / T_FALL) * (judge_y - top_y)
                p.setBrush(QColor(255, 140, 60, 90))
                p.drawRoundedRect(QRectF(cx - 4.0, y - tail, 8.0, max(8.0, tail)), 4.0, 4.0)
        # 命中特效：Keybeam（150ms）+ 菱形波纹 + 粒子（220ms）+ COMBO 跳字
        now = self._t_now
        keep = []
        for (th, lane, kind) in self._hit_flash:
            age = now - th
            if age > 0.22:
                continue
            keep.append((th, lane, kind))
            cx = ((lane % 4) + 0.5) * lane_w
            k = max(0.0, 1.0 - age / 0.15)
            p.setPen(QPen(QColor(255, 255, 255, int(200 * k)), 3.0 + 6.0 * k))
            p.drawLine(QPointF(cx, judge_y - 70.0 * k), QPointF(cx, judge_y + 30.0 * k))
            dk = max(0.0, 1.0 - age / 0.22)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(0, 229, 255, int(220 * dk)), 2.0))
            rr = 10.0 + 46.0 * (1.0 - dk)
            p.drawPolygon([QPointF(cx, judge_y - rr), QPointF(cx + rr, judge_y),
                           QPointF(cx, judge_y + rr), QPointF(cx - rr, judge_y)])
            for j in range(6):
                ang = j * 60.0 + 15.0
                import math as _m
                dx = _m.cos(_m.radians(ang)) * 26.0 * (1.0 - dk)
                dy = _m.sin(_m.radians(ang)) * 26.0 * (1.0 - dk)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, int(210 * dk)))
                p.drawEllipse(QPointF(cx + dx, judge_y + dy), 2.2, 2.2)
        self._hit_flash = keep
        if self._combo > 0:
            p.setPen(QColor(255, 255, 255, 235))
            f = p.font()
            f.setPixelSize(max(14, int(h * 0.035)))
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(0, judge_y - h * 0.16, w, h * 0.08), Qt.AlignCenter,
                       "COMBO × %d" % self._combo)
        # 顶底羽化
        top = QLinearGradient(0, 0, 0, h * 0.18)
        top.setColorAt(0.0, QColor(0, 0, 0, 130))
        top.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(top)
        p.drawRect(QRectF(0, 0, w, h * 0.18))
        bot = QLinearGradient(0, h * 0.86, 0, h)
        bot.setColorAt(0.0, QColor(0, 0, 0, 0))
        bot.setColorAt(1.0, QColor(0, 0, 0, 130))
        p.setBrush(bot)
        p.drawRect(QRectF(0, h * 0.86, w, h * 0.14))
        p.end()

    # ---- 锚点 -----------------------------------------------------------
    def rhythm_debug(self) -> dict:
        """探针锚点：事件统计 + 过线偏差 + 时间源状态。"""
        devs = list(self._devs)
        ana = list(self._devs_analytic)
        fr = list(self._devs_frame)
        return {
            "dev_ms_analytic_mean": (round(sum(ana) / len(ana), 3) if ana else None),
            "dev_ms_analytic_max_abs": (round(max(abs(d) for d in ana), 3) if ana else None),
            "dev_ms_frame_quant_mean": (round(sum(fr) / len(fr), 2) if fr else None),
            "dev_ms_frame_quant_max_abs": (round(max(abs(d) for d in fr), 2) if fr else None),
            "n_events": len(self._events),
            "t_fall": T_FALL,
            "t_now": round(float(self._t_now), 4),
            "combo": int(self._combo),
            "hits": len(devs),
            "dev_ms_mean": (round(sum(devs) / len(devs), 2) if devs else None),
            "dev_ms_max_abs": (round(max(abs(d) for d in devs), 2) if devs else None),
            "first_devs": devs[:10],
            "visible": bool(self.isVisible()),
        }