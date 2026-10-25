"""冰晶流光琴键（task-126③）—— 极简 4 轨音瀑，严禁铺满全屏（右侧 180px 窄区）。"""
from __future__ import annotations

import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

LANES = 4
RAIL_W = 40.0
NOTE_W, NOTE_H, NOTE_R = 32.0, 7.0, 3.5
JUDGE_LINE_H = 1.5
HIT_GLOW_H = 40.0
HIT_FADE_S = 0.140
T_FALL = 1.6
STAGE_W = 180



#: ---- task-126④：BPM 自相关 + 网格吸附 + 键型编排 -------------------------------
BPM_MIN, BPM_MAX = 60.0, 180.0
ENV_STEP = 0.01          # 能量包络分箱 10ms
SAME_LANE_MIN_GAP = 0.120  # 同轨最小间隔 120ms


def _estimate_t_beat(events) -> float:
    """能量包络自相关（Ellis 核心）：在 60~180 BPM 内找主拍周期 T_beat。"""
    if len(events) < 8:
        return 0.0
    t_end = max(float(e.get("t_hit", 0.0)) for e in events) + 0.1
    n = int(t_end / ENV_STEP) + 1
    env = [0.0] * n
    for e in events:
        i = int(float(e.get("t_hit", 0.0)) / ENV_STEP)
        if 0 <= i < n:
            env[i] += float(e.get("strength", 1) or 1)
    mean = sum(env) / float(len(env))
    env = [v - mean for v in env]
    lo = int((60.0 / BPM_MAX) / ENV_STEP)      # 180 BPM 周期
    hi = int((60.0 / BPM_MIN) / ENV_STEP)      # 60 BPM 周期
    best_lag, best = 0, -1e18
    for lag in range(max(1, lo), min(hi, len(env) - 1)):
        acc = 0.0
        for i in range(0, len(env) - lag):
            acc += env[i] * env[i + lag]
        if acc > best:
            best, best_lag = acc, lag
    return best_lag * ENV_STEP


def quantize_events(events) -> tuple:
    """★ ④：把 t_hit 吸附到 T_beat/2 网格 + 合并同格重复 + 键型编排。

    返回 (events, info)，info 里带 T_beat/BPM/网格偏差/每轨事件数/同轨最小间隔。
    """
    evs = []
    for e in (events or []):
        try:
            t = float(e.get("t_hit", 0.0))
        except Exception:
            continue
        item = dict(e)
        item["t_hit"] = t
        evs.append(item)
    evs.sort(key=lambda e: float(e["t_hit"]))
    T = _estimate_t_beat(evs)
    info = {"t_beat": round(T, 4), "bpm": (round(60.0 / T, 1) if T > 0 else None),
            "grid": ("T/2" if T > 0 else "none")}
    if T <= 0.0 or not evs:
        info.update({"snapped": 0, "grid_dev_max_ms": None, "lane_counts": [0] * LANES,
                     "same_lane_min_gap_ms": None})
        return evs, info
    half = T / 2.0
    # ② 吸附 + 同网格点合并
    buckets = {}
    dev_max = 0.0
    for e in evs:
        t = float(e["t_hit"])
        g = int(round(t / half))
        t2 = g * half
        dev_max = max(dev_max, abs(t - t2))
        e["t_hit"] = t2
        cur = buckets.get(g)
        if cur is None:
            buckets[g] = e
        else:
            cur["strength"] = max(int(cur.get("strength", 1) or 1), int(e.get("strength", 1) or 1))
            cur["hold"] = bool(cur.get("hold")) or bool(e.get("hold"))
    out = sorted(buckets.values(), key=lambda e: float(e["t_hit"]))
    # ③ 键型编排：顺楼梯 / 折返 / 重拍双押；同轨 ≥120ms
    last_t = {}
    stair = 0
    lane_counts = [0] * LANES
    final = []
    for i, e in enumerate(out):
        t = float(e["t_hit"])
        st = int(e.get("strength", 1) or 1)
        heavy = bool(e.get("hold")) or st >= 2 or (i % 8 == 0)
        if heavy:
            pair = (0, 3) if ((i // 8) % 2 == 0) else (1, 2)
            cands = list(pair)
        elif i % 4 == 2:
            cands = [1] if ((i // 2) % 2 == 0) else [2]        # 折返 1↔2
        else:
            cands = [stair % LANES]                            # 顺楼梯 0→1→2→3
            stair += 1
        lanes = []
        for cand in cands:
            if t - last_t.get(cand, -1e9) >= SAME_LANE_MIN_GAP and cand not in lanes:
                lanes.append(cand)
        if not lanes:                       # 全部被 120ms 规则挡住 ⇒ 顺延到空闲轨
            for cand in range(LANES):
                if t - last_t.get(cand, -1e9) >= SAME_LANE_MIN_GAP:
                    lanes = [cand]
                    break
        if not lanes:
            continue                        # 实在排不下就丢弃（避免同轨连发）
        for lane in lanes:
            ev2 = dict(e)
            ev2["lane"] = int(lane)
            ev2["strength"] = int(st) + (1 if len(lanes) > 1 else 0)
            ev2["heavy"] = bool(heavy)
            final.append(ev2)
            last_t[lane] = t
            lane_counts[lane] += 1
    final.sort(key=lambda e: float(e["t_hit"]))
    gaps = []
    per_lane_t = {}
    for e in final:
        ln = int(e["lane"])
        prev = per_lane_t.get(ln)
        if prev is not None:
            gaps.append((float(e["t_hit"]) - prev) * 1000.0)
        per_lane_t[ln] = float(e["t_hit"])
    info.update({
        "snapped": len(out), "merged": len(evs) - len(out),
        "grid_dev_max_ms": 0.0,                 # 吸附后相对 T/2 网格的偏差（构造上恒 0）
        "snap_shift_max_ms": round(dev_max * 1000.0, 3),   # 吸附前最大偏移（≤ T/4）
        "lane_counts": lane_counts,
        "same_lane_min_gap_ms": (round(min(gaps), 1) if gaps else None),
        "heavy_doubles": sum(1 for e in final if e.get("heavy")),
    })
    return final, info


def _is_light() -> bool:
    try:
        from . import theme as _theme
        return bool(_theme.is_light())
    except Exception:
        return False


class RhythmStage(QWidget):
    """4 轨极简音瀑（右侧 180px 窄区专用）：4×40px 轨 + 1px 分线 + 1.5px 判定线 + 32×7 胶囊音符 + 40px 呼吸光柱。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setFixedWidth(STAGE_W)
        self._events = []
        self._grid_info = {}
        self._t_now = 0.0
        self._devs = []
        self._devs_analytic = []
        self._devs_frame = []
        self._seen = set()
        self._hit_glow = []
        self._anchor_media = 0.0
        self._anchor_wall = 0.0

    def set_events(self, events) -> None:
        # ★ ④ BPM 自相关 + T/2 网格吸附 + 键型编排（失败则退化为原样）
        try:
            self._events, self._grid_info = quantize_events(events)
        except Exception:
            import logging
            logging.getLogger("zhijian.ui.rhythm").warning("节拍网格化失败", exc_info=True)
            self._events = sorted(list(events or []), key=lambda e: float(e.get("t_hit", 0.0)))
            self._grid_info = {}
        self._devs = []
        self._devs_analytic = []
        self._devs_frame = []
        self._seen = set()
        self._hit_glow = []
        self._t_now = 0.0
        self._anchor_media = 0.0
        self._anchor_wall = 0.0
        self.update()

    def set_time(self, t_media: float, paused: bool = False) -> None:
        try:
            t = float(t_media or 0.0)
        except Exception:
            t = 0.0
        if abs(t - self._t_now) > 0.25:
            self._t_now = t
            self._anchor_media = t
            self._anchor_wall = time.monotonic()
            self.update()
            return
        self._t_now = t
        self._anchor_media = t
        self._anchor_wall = time.monotonic()
        if not paused:
            self._advance()
        self.update()

    def _advance(self) -> None:
        for e in self._events:
            th = float(e.get("t_hit", 0.0))
            lane = int(e.get("lane", 0)) % LANES
            key = (th, lane)
            if key in self._seen:
                continue
            d = self._t_now - th
            if -0.08 <= d <= 0.12:
                self._seen.add(key)
                self._hit_glow.append((time.monotonic(), lane))
                ms = d * 1000.0
                self._devs.append(ms)
                self._devs_analytic.append(ms)
                self._devs_frame.append(ms)
        now = time.monotonic()
        self._hit_glow = [(t0, ln) for (t0, ln) in self._hit_glow if (now - t0) <= HIT_FADE_S]

    def rhythm_debug(self) -> dict:
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
            "combo": 0,
            "hits": len(devs),
            "dev_ms_mean": (round(sum(devs) / len(devs), 2) if devs else None),
            "dev_ms_max_abs": (round(max(abs(d) for d in devs), 2) if devs else None),
            "first_devs": devs[:10],
            "visible": bool(self.isVisible()),
            "stage_w": int(self.width()),
            "lanes": LANES,
            "style": "piano_keys_minimal",
            "t_beat": (self._grid_info or {}).get("t_beat"),
            "bpm": (self._grid_info or {}).get("bpm"),
            "grid_dev_max_ms": (self._grid_info or {}).get("grid_dev_max_ms"),
            "lane_counts": (self._grid_info or {}).get("lane_counts"),
            "same_lane_min_gap_ms": (self._grid_info or {}).get("same_lane_min_gap_ms"),
            "heavy_doubles": (self._grid_info or {}).get("heavy_doubles"),
            "merged": (self._grid_info or {}).get("merged"),
        }

    def _lane_x(self, lane: int) -> float:
        total = LANES * RAIL_W
        return (self.width() - total) / 2.0 + lane * RAIL_W

    def _note_y(self, t_hit: float) -> float:
        y_line = float(self.height()) - 10.0
        remain = t_hit - self._t_now
        if remain > T_FALL:
            return -NOTE_H * 2
        return y_line - (remain / T_FALL) * (y_line - 8.0)

    def paintEvent(self, ev):        # noqa: N802
        try:
            self._paint()
        except Exception:
            import logging
            logging.getLogger("zhijian.ui.rhythm").warning("音瀑绘制失败", exc_info=True)

    def _paint(self) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            h = float(self.height())
            light = _is_light()
            y_line = h - 10.0
            for i in range(LANES):
                x = self._lane_x(i)
                p.fillRect(QRectF(x, 0.0, RAIL_W, y_line),
                           QColor(255, 255, 255, 10) if not light else QColor(24, 28, 36, 12))
                p.setPen(QPen(QColor(255, 255, 255, 20) if not light else QColor(24, 28, 36, 22), 1.0))
                p.drawLine(int(x), 0, int(x), int(y_line))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(80, 175, 255, 150) if not light else QColor(15, 98, 254, 120))
            p.drawRect(QRectF(self._lane_x(0), y_line, LANES * RAIL_W, JUDGE_LINE_H))
            base = QColor(255, 255, 255, 166) if not light else QColor(30, 45, 70, 140)
            p.setPen(Qt.NoPen)
            p.setBrush(base)
            for e in self._events:
                th = float(e.get("t_hit", 0.0))
                dt = th - self._t_now
                if dt > T_FALL or dt < -0.25:
                    continue
                lane = int(e.get("lane", 0)) % LANES
                y = self._note_y(th)
                x = self._lane_x(lane) + (RAIL_W - NOTE_W) / 2.0
                r = QRectF(x, y - NOTE_H / 2.0, NOTE_W, NOTE_H)
                p.setBrush(base)
                p.drawRoundedRect(r, NOTE_R, NOTE_R)
                if bool(e.get("hold")) or int(e.get("strength", 0)) >= 2:
                    p.setPen(QPen(QColor(80, 175, 255, 204), 1.0))
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(r.adjusted(-0.5, -0.5, 0.5, 0.5), NOTE_R + 0.5, NOTE_R + 0.5)
                    p.setPen(Qt.NoPen)
            now = time.monotonic()
            for (t0, lane) in self._hit_glow:
                k = 1.0 - (now - t0) / HIT_FADE_S
                if k <= 0.0:
                    continue
                x = self._lane_x(lane)
                g = QLinearGradient(0.0, y_line, 0.0, y_line - HIT_GLOW_H)
                g.setColorAt(0.0, QColor(120, 200, 255, int(110 * k)))
                g.setColorAt(1.0, QColor(120, 200, 255, 0))
                p.fillRect(QRectF(x + 2.0, y_line - HIT_GLOW_H, RAIL_W - 4.0, HIT_GLOW_H), g)
        finally:
            p.end()
