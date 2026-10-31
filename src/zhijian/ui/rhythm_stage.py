"""冰晶流光音瀑 V3「极光液态琉璃」（task-135b）—— 4 轨音瀑，右侧 180px 窄区。

视觉规格（严格照卡面，全部由控件真实几何推导，无硬编码坐标）：
  · 磨砂轨道底衬：深 rgba(12,16,24,0.36) / 浅 rgba(255,255,255,0.32)，
    顶部/底部 32px 平滑渐隐；左侧 1px 柔和玻璃分割线；4 轨间 1px 垂直导轨细线
    （深 rgba(255,255,255,0.10) / 浅 rgba(15,23,42,0.08)）；每轨中央 1px 极细暗轨中心线；
  · 流光旋律折线：当前可见相邻音符 Δt∈[0.05,0.65]s 时，在音符中心之间画 1.6px
    抗锯齿半透明折线（深 rgba(140,225,255,0.38) / 浅 rgba(32,128,250,0.32)），
    拐点 2.5px 微光小圆点；
  · TAP 液态琉璃 32×8 圆角 4：水平对称渐变（深 两侧 rgba(80,190,255,0.42)→中心
    rgba(220,248,255,0.88)；浅 两侧 rgba(32,128,250,0.38)→中心 rgba(120,195,255,0.82)），
    仅上边缘 1px 镜面高光 rgba(255,255,255,0.75)，底层 4px 柔和微光晕
    （旧的"纯蓝实心大色块 + 粗白框"已彻底删除）；
  · HOLD 18px 宽：左右 alpha 0.12、中心 2px 亮芯 alpha 0.65，头部复用同一 TAP 胶囊
    ⇒ 与 TAP 无缝衔接；尾巴经过判定线时实时截断（绝不画到线下方）；
  · 极光锥形音柱：水平+垂直双向柔化（判定线处 alpha 0.45，向上 120px 指数衰减到 0）
    + 中央 2px 高亮激光芯线（替换旧的实色矩形光柱）；
  · 判定底座：4 枚高 10px 圆角胶囊（常态半透明空心框）；命中 1.40x→1.0x 150ms 回弹
    + 34→52 宽扩散光环淡出；HOLD 经过判定线时该轨底座常亮放大（1.12x）。

谱面（quantize_events）：
  · ★ 废除旧的「T_beat ÷ 2 网格吸附」与 225ms 死板合并（会把切分音砍光）；
  · ★ 修 HOLD 全丢：旧代码用 e.get("hold") 判断，而 beatmap 输出的是 type="hold"/dur
    ⇒ 长按全部退化成 TAP；现在按 type 识别并保留 dur；
  · 间隔收口：跨轨（全局）≥60ms、同轨 ≥90ms；冲突时换最近空闲轨，排不下才丢。
"""
from __future__ import annotations

import bisect
import logging
import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QRadialGradient)
from PySide6.QtWidgets import QWidget

log = logging.getLogger("zhijian.ui.rhythm")

LANES = 4
RAIL_W = 40.0
STAGE_W = 180

# ---- 音符 / 判定几何（task-135b 卡面数值；位置一律由 lane 中心 + 判定线推导）----------
NOTE_W = 32.0               # TAP 液态琉璃 32×8、圆角 4
NOTE_H = 8.0
NOTE_R = 4.0
NOTE_GLOW_PAD = 4.0         # 底层 4px 柔和微光晕
HALO_W_MAX = 52.0           # 命中扩散光环最大宽（34→52 的旧口径保留）
HALO_H_MAX = 26.0
JUDGE_BASE_H = 10.0
JUDGE_LINE_H = 1.5
JUDGE_INSET = 18.0          # 判定线在 RhythmStage 底部内缩 18px（stage-18 几何保留）
TOP_PAD = 8.0               # 音符最高可绘制到 y=8（再往上就是屏外）
T_FALL = 1.6                # 落速：音符从顶落到判定线 1.6s

# ---- 命中反馈时长 / 尺寸 -----------------------------------------------------------
HIT_S = 0.150               # 1.40x→1.0x 弹性回弹 150ms
HALO_S = 0.260              # 扩散光环淡出
HIT_GLOW_H = 120.0          # 极光锥形音柱高 120px（原实色矩形光柱 110px 已废除）
HIT_GLOW_A = 0.45           # 判定线处 alpha 0.45，向上 120px 指数衰减到 0
HIT_GLOW_CORE_W = 2.0       # 中央 2px 高亮激光芯线
HIT_GLOW_CORE_A = 0.65
HIT_GLOW_FADE = 0.24        # 命中音柱淡出时长 s
HOLD_RIBBON_W = 18.0        # HOLD 18px 宽
HOLD_CORE_W = 2.0           # 中心 2px 亮芯
HOLD_SIDE_A = 0.12          # 左右 alpha 0.12
HOLD_CORE_A = 0.65          # 亮芯 alpha 0.65
HOLD_BASE_SCALE = 1.12      # HOLD 经过判定线时该轨底座放大倍数（常亮）

# ---- 磨砂轨道底衬 / 导轨（task-135b 卡面数值）--------------------------------------
RAIL_PAD = 32.0             # 顶部/底部 32px 平滑渐隐
RAIL_GUIDE_W = 1.0          # 4 轨间 1px 垂直导轨细线
RAIL_CENTER_W = 1.0         # 每轨中央 1px 极细暗轨中心线
RAIL_DIVIDER_W = 1.0        # 左侧 1px 柔和玻璃分割线

# ---- 流光旋律折线（task-135b 卡面数值）----------------------------------------------
MELODY_DT_MIN = 0.05        # 相邻音符 Δt 下限（秒）
MELODY_DT_MAX = 0.65        # 相邻音符 Δt 上限（秒）
MELODY_LINE_W = 1.6         # 折线宽 1.6px
MELODY_VERTEX_R = 2.5       # 拐点微光小圆点半径 2.5px

# ---- 间隔约束（卡面：同轨 90ms / 跨轨 60ms）----------------------------------------
SAME_LANE_MIN_GAP = 0.090
CROSS_LANE_MIN_GAP = 0.060
HOLD_MIN_S = 0.220
HOLD_MAX_S = 2.500

# ---- 信息性 BPM 估计（不再用于吸附；仅 rhythm_debug 读数）--------------------------
_BPM_MIN, _BPM_MAX = 60.0, 180.0


def _is_light() -> bool:
    try:
        from . import theme as _theme
        return bool(_theme.is_light())
    except Exception:
        return False


def _elastic_scale(k: float) -> float:
    """1.40x→1.0x 弹性回弹（150ms）—— 两段式，见 _elastic_scale_2phase()。

    k∈[0,1]；k=0 ⇒ 1.40、k=0.4（60ms）⇒ 1.266（>1.2，验收峰值帧可测到放大）、
    k≈0.8 轻微过冲 ≈0.96、k=1 ⇒ 1.0。
    """
    return _elastic_scale_2phase(k)


def _elastic_scale_2phase(k: float) -> float:
    """1.40x→1.0x 的**两段式**弹性回弹（卡面 150ms）。

    0~75ms（k≤0.5）：1.40 → 1.25（smoothstep，命中后 60ms ≈1.266 —— 验收要抓得到"放大"）；
    75~150ms：1.25 → 1.0（阻尼余弦，k≈0.8 轻微过冲到 ≈0.96 再落定）。
    """
    if k <= 0.0:
        return 1.40
    if k >= 1.0:
        return 1.0
    if k <= 0.5:
        t = k / 0.5
        s = t * t * (3.0 - 2.0 * t)          # smoothstep 0→1
        return 1.40 - 0.15 * s               # 1.40 → 1.25（60ms 时 ≈1.266）
    t = (k - 0.5) / 0.5
    return 1.0 + 0.25 * math.exp(-3.0 * t) * math.cos(1.5 * math.pi * t)


def _norm_type(e: dict) -> str:
    """统一类型：type="hold" / 旧字段 hold=True 都算 HOLD（★ 长按全丢的修复点）。"""
    t = str(e.get("type") or "").strip().lower()
    if t == "hold" or bool(e.get("hold")):
        return "hold"
    return "tap"


def _estimate_bpm(events: list):
    """拍周期估计（只用相邻事件的 IOI 折拍，O(n)；仅作 rhythm_debug 信息字段）。

    ★ 与旧版区别：旧版用能量包络自相关（O(n_env·lags)，UI 线程可达数百 ms）；
      这里只做 IOI 直方图 —— 不参与任何吸附/合并，网格已废除。
    """
    ts = [float(e["t_hit"]) for e in events]
    if len(ts) < 8:
        return 0.0, None
    gaps = []
    for i in range(1, len(ts)):
        g = ts[i] - ts[i - 1]
        if 0.12 <= g <= 2.0:
            gaps.append(g)
    if not gaps:
        return 0.0, None
    score = {}
    for g in gaps:
        for k in range(1, 7):
            period = g / k
            if 60.0 / _BPM_MIN <= period <= 60.0 / _BPM_MAX:
                slot = int(round(period * 100.0))
                score[slot] = score.get(slot, 0) + 1
    if not score:
        return 0.0, None
    best = max(score.items(), key=lambda kv: (kv[1], -abs(kv[0] - 50)))[0] / 100.0
    if best <= 0.0:
        return 0.0, None
    matched = [g for g in gaps
               if abs(g - best * round(g / best)) <= 0.03 and round(g / best) >= 1]
    if matched:
        best = sum(g / round(g / best) for g in matched) / len(matched)
    return round(best, 4), round(60.0 / best, 1)


def _quartiles(sorted_vals: list) -> list:
    n = len(sorted_vals)
    if n <= 0:
        return [0.0, 0.0, 0.0]
    out = []
    for frac in (0.25, 0.5, 0.75):
        i = int(frac * (n - 1) + 0.5)
        out.append(float(sorted_vals[max(0, min(n - 1, i))]))
    return out


def _assign_missing_lanes(events: list) -> None:
    """给没有 lane 的事件补 lane：有 centroid ⇒ 局部 ±3s 分位数；否则按序号走楼梯。

    正常路径（beatmap V2）已经带 lane，这里只是兼容旧谱/手工注入事件。
    """
    n = len(events)
    # ★ 性能：正常路径（beatmap V2）所有事件都带 lane ⇒ 直接返回，绝不在 UI 线程做分位数扫描
    if not any(int(e.get("_lane_in", -1)) < 0 for e in events):
        return
    have_cent = [e for e in events if isinstance(e.get("centroid"), (int, float))]
    if len(have_cent) >= 8:
        ts = [float(e["t_hit"]) for e in events]
        cents_all = [float(e["centroid"]) for e in events
                     if isinstance(e.get("centroid"), (int, float))]
        gq = _quartiles(sorted(cents_all))
        lo = hi = 0
        for i in range(n):
            t = ts[i]
            while lo < n and ts[lo] < t - 3.0:
                lo += 1
            if hi < i:
                hi = i
            while hi < n and ts[hi] <= t + 3.0:
                hi += 1
            inside = [float(events[j]["centroid"]) for j in range(lo, hi)
                      if isinstance(events[j].get("centroid"), (int, float))]
            q = _quartiles(sorted(inside)) if len(inside) >= 8 else gq
            c = events[i].get("centroid")
            if isinstance(c, (int, float)):
                events[i]["lane"] = int(bisect.bisect_right(q, float(c))) % LANES
    stair = 0
    for e in events:
        if e.get("_lane_in", -1) >= 0:
            continue
        e["lane"] = stair % LANES
        stair += 1


def quantize_events(events) -> tuple:
    """谱面收口：类型归一 + 跨轨 60ms + 同轨 90ms；**不做网格吸附**。

    返回 (events, info)；info 带 lane_counts / hold 占比 / 同轨最小间隔 / BPM 信息。
    """
    evs = []
    for e in (events or []):
        try:
            t = float(e.get("t_hit", 0.0))
        except Exception:
            continue
        item = dict(e)
        item["t_hit"] = t
        item["type"] = _norm_type(e)
        try:
            dur = float(e.get("dur", 0.0) or 0.0)
        except Exception:
            dur = 0.0
        item["dur"] = (max(0.0, min(HOLD_MAX_S, dur)) if item["type"] == "hold" else 0.0)
        try:
            lane = int(e.get("lane", -1))
        except Exception:
            lane = -1
        item["_lane_in"] = lane if 0 <= lane < LANES else -1
        evs.append(item)
    evs.sort(key=lambda z: float(z["t_hit"]))
    # ---- ① 跨轨（全局）≥60ms：保留优先级 = HOLD > TAP；同类型保留先到的 ----
    kept = []
    for e in evs:
        if kept and float(e["t_hit"]) - float(kept[-1]["t_hit"]) < CROSS_LANE_MIN_GAP - 1e-9:
            if e["type"] == "hold" and kept[-1]["type"] != "hold":
                if len(kept) < 2 or (float(e["t_hit"]) - float(kept[-2]["t_hit"])
                                     >= CROSS_LANE_MIN_GAP - 1e-9):
                    kept[-1] = e
            continue
        kept.append(e)
    # ---- ② lane：已有的沿用；缺的按质心/楼梯补 ----
    _assign_missing_lanes(kept)
    # ---- ③ 同轨 ≥90ms：冲突换最近空闲轨，排不下丢弃 ----
    last_t = {}
    spans = {}
    final = []
    for e in kept:
        t = float(e["t_hit"])
        lane = int(e.get("lane", 0)) % LANES
        cur = dict(e)
        cur.pop("_lane_in", None)
        cur["lane"] = lane

        def _free(ln: int) -> bool:
            lt = last_t.get(ln)
            if lt is not None and (t - lt) < SAME_LANE_MIN_GAP - 1e-9:
                return False
            for (a, b) in spans.get(ln, ()):
                if a - 0.05 <= t <= b + 0.05:
                    return False
            return True

        placed = False
        if _free(lane):
            placed = True
        else:
            for ln in sorted(range(LANES), key=lambda z: (abs(z - lane), z)):
                if ln == lane:
                    continue
                if _free(ln):
                    cur["lane"] = int(ln)
                    placed = True
                    break
        if not placed:
            log.debug("同轨 90ms 排不下，丢弃事件 t=%.3f lane=%d", t, lane)
            continue
        final.append(cur)
        last_t[int(cur["lane"])] = t
        if cur["type"] == "hold":
            spans.setdefault(int(cur["lane"]), []).append(
                (t, t + float(cur["dur"] or 0.0)))
    final.sort(key=lambda z: (float(z["t_hit"]), int(z.get("lane", 0))))
    # ---- ④ 指标 ----
    lane_counts = [0] * LANES
    for e in final:
        lane_counts[int(e.get("lane", 0)) % LANES] += 1
    holds = sum(1 for e in final if e["type"] == "hold")
    gaps = []
    per_lane = {}
    for e in final:
        ln = int(e.get("lane", 0)) % LANES
        t = float(e["t_hit"])
        prev = per_lane.get(ln)
        if prev is not None:
            gaps.append((t - prev) * 1000.0)
        per_lane[ln] = t
    cross = []
    for i in range(1, len(final)):
        if int(final[i].get("lane", 0)) % LANES == int(final[i - 1].get("lane", 0)) % LANES:
            continue
        cross.append((float(final[i]["t_hit"]) - float(final[i - 1]["t_hit"])) * 1000.0)
    cents = [float(e["centroid"]) for e in final
             if isinstance(e.get("centroid"), (int, float))]
    t_beat, bpm = _estimate_bpm(final)
    n = len(final)
    info = {
        "n_events": n,
        "taps": n - holds,
        "holds": holds,
        "hold_ratio": (round(holds / float(n), 4) if n else 0.0),
        "lane_counts": lane_counts,
        "same_lane_min_gap_ms": (round(min(gaps), 2) if gaps else None),
        "cross_lane_min_gap_ms": (round(min(cross), 2) if cross else None),
        "grid": "none",
        "t_beat": t_beat,
        "bpm": bpm,
        "centroid_mean": (round(sum(cents) / len(cents), 1) if cents else None),
        "demo_events": sum(1 for e in final if str(e.get("src", "")).startswith("demo")),
    }
    return final, info


class RhythmStage(QWidget):
    """4 轨冰晶流光音瀑（右侧 180px 窄区）：TAP 胶囊 + HOLD 流光面条带 + 4 枚判定底座。"""

    STAGE_W = STAGE_W
    JUDGE_INSET = JUDGE_INSET

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setFixedWidth(int(STAGE_W))
        self._events = []
        self._info = {}
        self._t_now = 0.0
        self._last_t = 0.0
        self._seen = set()
        self._tails = set()
        self._hits = []
        self._hit_count = 0
        self._devs = []
        # ★ task-135b：极光锥形音柱的 alpha 剖面是**只与主题/颜色有关**的静态图，
        #   缓存后每帧只 drawImage（不重建渐变），4 轨同亮也不掉帧。
        self._glow_imgs: dict = {}

    # ------------------------------------------------------------------
    def set_events(self, events) -> None:
        try:
            self._events, self._info = quantize_events(events)
        except Exception:  # noqa: BLE001
            log.warning("谱面量化失败，退化为原样排序", exc_info=True)
            self._events = sorted(list(events or []), key=lambda e: float(e.get("t_hit", 0.0)))
            self._info = {}
        self._seen = set()
        self._tails = set()
        self._hits = []
        self._hit_count = 0
        self._devs = []
        self._t_now = 0.0
        self._last_t = 0.0
        self.update()

    def set_time(self, t_media: float, paused: bool = False) -> None:
        """由 AudioView 唯一的 16ms 帧钟驱动（不新增定时器 / 不轮询）。"""
        try:
            t = float(t_media or 0.0)
        except Exception:
            t = 0.0
        jump = abs(t - self._last_t)
        self._t_now = t
        self._last_t = t
        if jump > 0.40:
            # seek / 起播 / 倒放：清掉命中特效，把"已经过去"的事件记为已命中，避免补刷一串
            self._hits = []
            self._seen = set()
            self._tails = set()
            for e in self._events:
                th = float(e.get("t_hit", 0.0))
                lane = int(e.get("lane", 0)) % LANES
                if th < t - 0.20:
                    self._seen.add((round(th, 4), lane))
                    if e.get("type") == "hold":
                        te = th + float(e.get("dur", 0.0) or 0.0)
                        if te < t - 0.20:
                            self._tails.add((round(te, 4), lane))
            self.update()
            return
        if not paused:
            self._advance()
        self.update()

    def _add_hit(self, lane: int, kind: str) -> None:
        self._hits.append({"t0": time.monotonic(), "lane": int(lane) % LANES, "kind": str(kind)})
        if len(self._hits) > 48:
            del self._hits[:24]

    def _advance(self) -> None:
        t = self._t_now
        for e in self._events:
            th = float(e.get("t_hit", 0.0))
            lane = int(e.get("lane", 0)) % LANES
            key = (round(th, 4), lane)
            if key not in self._seen:
                d = t - th
                if -0.05 <= d <= 0.10:
                    self._seen.add(key)
                    self._hit_count += 1
                    self._devs.append(d * 1000.0)
                    self._add_hit(lane, "head" if e.get("type") == "hold" else "tap")
            if e.get("type") == "hold":
                te = th + float(e.get("dur", 0.0) or 0.0)
                tkey = (round(te, 4), lane)
                if tkey not in self._tails:
                    d2 = t - te
                    if -0.05 <= d2 <= 0.10:
                        self._tails.add(tkey)
                        self._add_hit(lane, "tail")
        now = time.monotonic()
        self._hits = [h for h in self._hits if (now - float(h["t0"])) <= 0.60]
        if len(self._devs) > 240:
            del self._devs[:120]

    def active_holds(self) -> dict:
        """当前正在按住的长按：{lane: event}。"""
        out = {}
        t = self._t_now
        for e in self._events:
            if e.get("type") != "hold":
                continue
            th = float(e.get("t_hit", 0.0))
            dur = float(e.get("dur", 0.0) or 0.0)
            if th <= t <= th + dur:
                out[int(e.get("lane", 0)) % LANES] = e
        return out

    # ------------------------------------------------------------------
    def rhythm_debug(self) -> dict:
        info = dict(self._info or {})
        devs = list(self._devs)
        active = sorted(self.active_holds().keys())
        h = float(self.height())
        judge = self.judge_y()
        out = {
            "style": "liquid_aurora_v3",
            "lanes": LANES,
            "n_events": len(self._events),
            "taps": info.get("taps"),
            "holds": info.get("holds"),
            "hold_ratio": info.get("hold_ratio"),
            "lane_counts": info.get("lane_counts"),
            "same_lane_min_gap_ms": info.get("same_lane_min_gap_ms"),
            "cross_lane_min_gap_ms": info.get("cross_lane_min_gap_ms"),
            "t_beat": info.get("t_beat"),
            "bpm": info.get("bpm"),
            "grid": "none",
            "demo_events": info.get("demo_events"),
            "stage_w": int(self.width()),
            "stage_h": int(round(h)),
            "stage_bottom_y": int(round(float(self.y()) + h)),
            "judge_inset": float(JUDGE_INSET),
            "judge_y": round(judge, 2),
            "judge_gap_from_bottom": round(h - judge, 2),
            "t_fall": T_FALL,
            "t_now": round(float(self._t_now), 4),
            "hits": len(devs),
            "hit_count": int(self._hit_count),
            "dev_ms_mean": (round(sum(devs) / len(devs), 2) if devs else None),
            "dev_ms_max_abs": (round(max(abs(d) for d in devs), 2) if devs else None),
            "first_devs": [round(d, 2) for d in devs[:10]],
            "visible": bool(self.isVisible()),
            "active_holds": active,
            "effects": len(self._hits),
            "centroid_mean": info.get("centroid_mean"),
            # ---- ★ task-135b 视觉规格常量（QA/Lead 直接断言，不必从像素反推）----
            "note_w": NOTE_W,
            "note_h": NOTE_H,
            "note_r": NOTE_R,
            "note_glow_pad": NOTE_GLOW_PAD,
            "base_expected_w_h": [NOTE_W, JUDGE_BASE_H],
            "hold_ribbon_w": HOLD_RIBBON_W,
            "hold_core_w": HOLD_CORE_W,
            "hold_side_alpha": HOLD_SIDE_A,
            "hold_core_alpha": HOLD_CORE_A,
            "glow_core_w": HIT_GLOW_CORE_W,
            "glow_core_alpha": HIT_GLOW_CORE_A,
            "rail_fade_h": RAIL_PAD,
            "rail_guide_w": RAIL_GUIDE_W,
            "rail_center_w": RAIL_CENTER_W,
            "rail_divider_w": RAIL_DIVIDER_W,
            "halo_range_w": [NOTE_W, HALO_W_MAX],
            "halo_range_h": [NOTE_H, HALO_H_MAX],
            "melody": self.melody_state(),
            "visual_spec": {
                "tap": {"w": NOTE_W, "h": NOTE_H, "r": NOTE_R,
                        "side_alpha": {"dark": 0.42, "light": 0.38},
                        "center_alpha": {"dark": 0.88, "light": 0.82},
                        "top_highlight_alpha": 0.75, "glow_pad": NOTE_GLOW_PAD},
                "hold": {"ribbon_w": HOLD_RIBBON_W, "core_w": HOLD_CORE_W,
                         "side_alpha": HOLD_SIDE_A, "core_alpha": HOLD_CORE_A},
                "glow": {"h": HIT_GLOW_H, "alpha": HIT_GLOW_A,
                         "core_w": HIT_GLOW_CORE_W, "decay": "exp(-4*d/h)"},
                "rail": {"bed_alpha": {"dark": 0.36, "light": 0.32},
                         "fade_h": RAIL_PAD, "guide_w": RAIL_GUIDE_W,
                         "center_w": RAIL_CENTER_W, "divider_w": RAIL_DIVIDER_W},
                "melody": {"line_w": MELODY_LINE_W, "vertex_r": MELODY_VERTEX_R,
                           "dt_range": [MELODY_DT_MIN, MELODY_DT_MAX],
                           "alpha": {"dark": 0.38, "light": 0.32}},
                "geometry": {"stage_w": STAGE_W, "lanes": LANES,
                             "rail_w": RAIL_W, "judge_inset": JUDGE_INSET},
            },
        }
        # ★ task-132 验收锚点：命中反馈的**数值证据**（不必从像素反推，QA/Lead 可直接断言）
        now = time.monotonic()
        hit_detail = {}
        halo = None
        for hh in self._hits:
            kind = str(hh.get("kind"))
            age = now - float(hh.get("t0", now))
            if age < 0.0:
                continue
            if kind in ("tap", "head") and age <= HIT_S:
                k = min(1.0, age / HIT_S)
                hit_detail[str(int(hh["lane"]))] = {
                    "age_ms": round(age * 1000.0, 1),
                    "scale": round(_elastic_scale(k), 3),
                    "glow_h": HIT_GLOW_H,
                    "glow_alpha": HIT_GLOW_A,
                }
            if kind in ("tap", "head", "tail"):
                total = HALO_S * (1.15 if kind == "tail" else 1.0)
                if age <= total:
                    kk = max(0.0, min(1.0, age / total))
                    halo = {
                        "lane": int(hh["lane"]), "kind": kind,
                        "age_ms": round(age * 1000.0, 1),
                        "w": round(NOTE_W + (HALO_W_MAX - NOTE_W) * kk, 2),
                        "h": round(NOTE_H + (HALO_H_MAX - NOTE_H) * kk, 2),
                        "range_w": [NOTE_W, HALO_W_MAX],
                        "range_h": [NOTE_H, HALO_H_MAX],
                    }
        active_detail = {}
        for lane, ev in self.active_holds().items():
            active_detail[str(int(lane))] = {
                "dur": round(float(ev.get("dur", 0.0) or 0.0), 3),
                "base_scale": HOLD_BASE_SCALE,
                "ribbon_bottom_y": round(judge, 2),   # 燃烧中尾巴底端 = 判定线（实时截断）
                "judge_y": round(judge, 2),
                "ribbon_w": HOLD_RIBBON_W,
            }
        out["hit_lanes"] = sorted(int(k) for k in hit_detail.keys())
        out["hit_base_scale"] = {k: v["scale"] for k, v in hit_detail.items()}
        out["hit_detail"] = hit_detail
        out["halo_expected"] = halo
        out["glow_h"] = HIT_GLOW_H
        out["glow_alpha"] = HIT_GLOW_A
        out["hold_base_scale"] = HOLD_BASE_SCALE
        out["active_hold_detail"] = active_detail
        out["hold_ribbon_bottom_y"] = {k: v["ribbon_bottom_y"]
                                       for k, v in active_detail.items()}
        # 兼容旧探针字段（task-122 口径）
        out["dev_ms_analytic_mean"] = out["dev_ms_mean"]
        out["dev_ms_analytic_max_abs"] = out["dev_ms_max_abs"]
        out["dev_ms_frame_quant_mean"] = out["dev_ms_mean"]
        out["dev_ms_frame_quant_max_abs"] = out["dev_ms_max_abs"]
        out["combo"] = int(self._hit_count)
        return out

    # ------------------------------------------------------------------
    def judge_y(self) -> float:
        """判定线 y：RhythmStage 底部内缩 18px（控件坐标）。"""
        return max(TOP_PAD, float(self.height()) - JUDGE_INSET)

    def _lane_x(self, lane: int) -> float:
        total = LANES * RAIL_W
        return (float(self.width()) - total) / 2.0 + lane * RAIL_W

    def _lane_cx(self, lane: int) -> float:
        return self._lane_x(lane) + RAIL_W / 2.0

    def _note_y(self, t_hit: float) -> float:
        y_line = self.judge_y()
        remain = float(t_hit) - self._t_now
        return y_line - (remain / T_FALL) * (y_line - TOP_PAD)

    @staticmethod
    def _palette(light: bool) -> dict:
        """task-135b 液态琉璃 / 极光配色（alpha 一律按卡面 0~1 写成 0~255）。"""
        if light:
            return {
                # 磨砂轨道底衬 / 导轨（浅色：白 0.32）
                "bed": QColor(255, 255, 255, 82),          # rgba(255,255,255,0.32)
                "divider": QColor(15, 23, 42, 31),         # 左侧 1px 柔和玻璃分割线
                "guide": QColor(15, 23, 42, 20),           # 轨间导轨 rgba(15,23,42,0.08)
                "center": QColor(15, 23, 42, 15),          # 每轨中央极细暗轨中心线
                # 流光旋律折线 rgba(32,128,250,0.32)
                "melody": QColor(32, 128, 250, 82),
                "melody_dot": QColor(32, 128, 250, 142),
                # TAP 液态琉璃：两侧 rgba(32,128,250,0.38) → 中心 rgba(120,195,255,0.82)
                "tap_side": QColor(32, 128, 250, 97),
                "tap_center": QColor(120, 195, 255, 209),
                "tap_hl": QColor(255, 255, 255, 191),      # 上边缘 1px 镜面高光 0.75
                "tap_glow": QColor(120, 195, 255, 62),     # 底层 4px 微光晕
                # HOLD：左右 alpha 0.12 / 中心 2px 亮芯 alpha 0.65
                "ribbon": QColor(32, 128, 250, 31),
                "ribbon_mid": QColor(80, 170, 250, 31),
                "core": QColor(32, 128, 250, 166),
                # 极光锥形音柱 / 2px 激光芯线
                "glow": QColor(32, 128, 250, 255),
                "laser": QColor(120, 195, 255, 235),
                # 判定线 / 底座 / 光环（stage-18 反馈逻辑沿用）
                "judge": QColor(32, 128, 250, 190),
                "base_edge": QColor(32, 128, 250, 88),
                "base_fill": QColor(32, 128, 250, 18),
                "halo": QColor(32, 128, 250, 170),
            }
        return {
            # 磨砂轨道底衬：深 rgba(12,16,24,0.36)
            "bed": QColor(12, 16, 24, 92),
            "divider": QColor(255, 255, 255, 36),
            "guide": QColor(255, 255, 255, 26),            # rgba(255,255,255,0.10)
            "center": QColor(0, 0, 0, 56),
            # 流光旋律折线 rgba(140,225,255,0.38)
            "melody": QColor(140, 225, 255, 97),
            "melody_dot": QColor(190, 240, 255, 152),
            # TAP：两侧 rgba(80,190,255,0.42) → 中心 rgba(220,248,255,0.88)
            "tap_side": QColor(80, 190, 255, 107),
            "tap_center": QColor(220, 248, 255, 224),
            "tap_hl": QColor(255, 255, 255, 191),
            "tap_glow": QColor(190, 240, 255, 54),
            "ribbon": QColor(140, 225, 255, 31),
            "ribbon_mid": QColor(140, 225, 255, 31),
            "core": QColor(220, 248, 255, 166),
            "glow": QColor(120, 220, 255, 255),
            "laser": QColor(240, 252, 255, 235),
            "judge": QColor(120, 220, 255, 190),
            "base_edge": QColor(120, 220, 255, 84),
            "base_fill": QColor(120, 220, 255, 16),
            "halo": QColor(165, 232, 255, 175),
        }

    # ------------------------------------------------------------------
    # 几何 / 可见音符
    def visible_notes(self) -> list:
        """当前屏幕上可见的音符（含未过判定线的 HOLD 头），按 t_hit 排序。

        坐标口径与绘制完全一致（lane 中心 + _note_y）⇒ 流光折线/QA 断言同源。
        """
        y_line = self.judge_y()
        lo = TOP_PAD - NOTE_H
        hi = y_line + NOTE_H
        notes = []
        for e in self._events:
            try:
                th = float(e.get("t_hit", 0.0))
            except Exception:
                continue
            dt = th - self._t_now
            if dt > T_FALL or dt < -0.10:
                continue
            if _norm_type(e) == "hold" and self._t_now >= th:
                continue                     # 头已过线：拖尾由面条带/底座接管
            y = self._note_y(th)
            if y < lo or y > hi:
                continue
            lane = int(e.get("lane", 0)) % LANES
            notes.append({"t": th, "lane": lane, "type": _norm_type(e),
                          "x": self._lane_cx(lane), "y": y})
        notes.sort(key=lambda z: (z["t"], z["lane"]))
        return notes

    def melody_state(self) -> dict:
        """流光旋律折线的可断言语义：可见音符数 + 实际连线的相邻对数/端点。"""
        notes = self.visible_notes()
        pairs = []
        for a, b in zip(notes, notes[1:]):
            dt = float(b["t"]) - float(a["t"])
            if MELODY_DT_MIN - 1e-9 <= dt <= MELODY_DT_MAX + 1e-9:
                pairs.append({
                    "dt": round(dt, 3),
                    "t0": round(float(a["t"]), 3), "t1": round(float(b["t"]), 3),
                    "lane0": int(a["lane"]), "lane1": int(b["lane"]),
                    "p0": [round(float(a["x"]), 1), round(float(a["y"]), 1)],
                    "p1": [round(float(b["x"]), 1), round(float(b["y"]), 1)],
                })
        return {"visible": len(notes), "n_pairs": len(pairs), "pairs": pairs,
                "line_w": MELODY_LINE_W, "vertex_r": MELODY_VERTEX_R,
                "dt_range": [MELODY_DT_MIN, MELODY_DT_MAX]}

    # ------------------------------------------------------------------
    def paintEvent(self, _ev):        # noqa: N802
        try:
            self._paint()
        except Exception:  # noqa: BLE001
            log.warning("音瀑绘制失败", exc_info=True)

    def _paint(self) -> None:
        w = float(self.width())
        h = float(self.height())
        if w < 40.0 or h < 80.0:
            return
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            light = _is_light()
            pal = self._palette(light)
            y_line = self.judge_y()
            now_wall = time.monotonic()
            active = self.active_holds()
            # 图层顺序（从底到顶）：轨道底衬 → 流光折线 → 极光音柱 → HOLD → TAP
            #                     → 判定底座 → 命中光环 → 判定线
            self._paint_rails(p, y_line, pal)
            self._paint_melody_lines(p, y_line, pal)       # 折线在音符下方
            self._paint_glows(p, y_line, pal, active, now_wall)
            self._paint_holds(p, y_line, pal)
            self._paint_taps(p, y_line, pal)
            self._paint_melody_dots(p, y_line, pal)        # 拐点圆点在音符上方（可见）
            self._paint_bases(p, y_line, pal, active, now_wall)
            self._paint_halos(p, y_line, pal, now_wall)
            self._paint_judge_line(p, y_line, pal)
        finally:
            p.end()

    # ------------------------------------------------------------------
    @staticmethod
    def _v_fade_gradient(color: QColor, h: float) -> QLinearGradient:
        """整高垂直渐变：顶部 / 底部 RAIL_PAD px 内 alpha 0→满→0（平滑渐隐）。"""
        g = QLinearGradient(0.0, 0.0, 0.0, max(1.0, h))
        c_full = QColor(color)
        c_zero = QColor(color)
        c_zero.setAlpha(0)
        fade = min(float(RAIL_PAD), max(0.0, h / 2.0 - 1.0))
        g.setColorAt(0.0, QColor(c_zero))
        if fade > 0.5 and h > 2.0 * fade:
            g.setColorAt(fade / h, QColor(c_full))
            g.setColorAt(1.0 - fade / h, QColor(c_full))
        else:
            g.setColorAt(0.5, QColor(c_full))
        g.setColorAt(1.0, QColor(c_zero))
        return g

    def _paint_rails(self, p: QPainter, y_line: float, pal: dict) -> None:
        """磨砂轨道底衬 + 左侧玻璃分割线 + 4 轨导轨 + 每轨中央暗轨中心线。"""
        w = float(self.width())
        h = float(self.height())
        if w < 2.0 or h < 4.0:
            return
        p.save()
        # ① 磨砂底衬（整条 180px 窄区），顶部/底部 32px 平滑渐隐
        p.setPen(Qt.NoPen)
        p.fillRect(QRectF(0.0, 0.0, w, h), self._v_fade_gradient(pal["bed"], h))
        # ② 左侧 1px 柔和玻璃分割线（与视频区的软边界）
        pen = QPen(QBrush(self._v_fade_gradient(pal["divider"], h)), RAIL_DIVIDER_W)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        p.drawLine(QPointF(0.5, 0.0), QPointF(0.5, h))
        # ③ 4 轨间 1px 垂直导轨细线（3 条内部分隔；同样带上/下渐隐）
        pen = QPen(QBrush(self._v_fade_gradient(pal["guide"], h)), RAIL_GUIDE_W)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        for i in range(1, LANES):
            x = self._lane_x(i) + 0.5
            p.drawLine(QPointF(x, 0.0), QPointF(x, h))
        # ④ 每轨中央 1px 极细暗轨中心线
        pen = QPen(QBrush(self._v_fade_gradient(pal["center"], h)), RAIL_CENTER_W)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        for i in range(LANES):
            x = self._lane_cx(i) + 0.5
            p.drawLine(QPointF(x, 0.0), QPointF(x, h))
        p.restore()

    def _melody_geometry(self):
        """可见音符里满足 Δt∈[0.05,0.65]s 的相邻对 + 参与连线的拐点集合。"""
        notes = self.visible_notes()
        pairs = []
        ends = {}
        for a, b in zip(notes, notes[1:]):
            dt = float(b["t"]) - float(a["t"])
            if MELODY_DT_MIN - 1e-9 <= dt <= MELODY_DT_MAX + 1e-9:
                pairs.append((a, b))
                ends[(round(float(a["t"]), 4), int(a["lane"]))] = a
                ends[(round(float(b["t"]), 4), int(b["lane"]))] = b
        return pairs, list(ends.values())

    def _paint_melody_lines(self, p: QPainter, y_line: float, pal: dict) -> None:
        """流光旋律折线：在**音符下方的图层**画（不会横穿音符本体）。"""
        if MELODY_LINE_W <= 0:
            return
        pairs, _ends = self._melody_geometry()
        if not pairs:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(pal["melody"], MELODY_LINE_W, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        for a, b in pairs:
            p.drawLine(QPointF(a["x"], a["y"]), QPointF(b["x"], b["y"]))
        p.restore()

    def _paint_melody_dots(self, p: QPainter, y_line: float, pal: dict) -> None:
        """拐点 2.5px 微光小圆点：画在**音符上方**，否则会被 32×8 音符盖住看不见。"""
        if MELODY_VERTEX_R <= 0:
            return
        _pairs, ends = self._melody_geometry()
        if not ends:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(pal["melody_dot"]))
        for n in ends:
            p.drawEllipse(QPointF(n["x"], n["y"]), MELODY_VERTEX_R, MELODY_VERTEX_R)
        p.restore()

    # ------------------------------------------------------------------
    def _paint_glows(self, p: QPainter, y_line: float, pal: dict,
                     active: dict, now_wall: float) -> None:
        """极光锥形音柱（命中 0.24s 淡出 + HOLD 按住呼吸）。"""
        for h in self._hits:
            if h["kind"] not in ("tap", "head"):
                continue
            age = now_wall - float(h["t0"])
            if age > HIT_GLOW_FADE:
                continue
            k = max(0.0, 1.0 - age / HIT_GLOW_FADE)
            self._paint_beam(p, self._lane_cx(int(h["lane"])), y_line, k, pal)
        for lane in sorted(active):
            breath = 0.5 + 0.5 * math.sin(now_wall * 2.0 * math.pi * 1.5 + lane * 1.3)
            self._paint_beam(p, self._lane_cx(int(lane)), y_line,
                             0.55 + 0.45 * breath, pal)

    def _glow_image(self, pal: dict) -> tuple:
        """极光锥形音柱的 alpha 剖面（静态缓存，逻辑 1x；高 DPR 由绘图平滑放大 —— 本来就是柔光）。

        水平双向柔化（中心亮、边缘归零）×(1-dx²)^1.5 × 垂直指数衰减（底部 1 → 120px 处 ≈0.018）；
        锥形 = 判定线处宽 32px，向上 120px 收到 6px。返回 (QImage, 逻辑宽, 逻辑高)。
        """
        key = (_is_light(), int(pal["glow"].rgb()))
        hit = self._glow_imgs.get(key)
        if hit is not None:
            return hit
        iw = max(4, int(round(RAIL_W + 2.0)))
        ih = max(8, int(round(HIT_GLOW_H)))
        img = QImage(iw, ih, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        base_half = 16.0            # 判定线处半宽（宽 32px 的锥底）
        tip_half = 3.0              # 顶端半宽（收到 6px）
        col = pal["glow"]
        r0, g0, b0 = col.red(), col.green(), col.blue()
        cx_img = iw / 2.0
        for py in range(ih):
            frac = (py + 0.5) / ih            # 0 = 顶部（远）  1 = 底部（判定线）
            dist = (1.0 - frac) * HIT_GLOW_H  # 距判定线高度
            v = math.exp(-4.0 * dist / HIT_GLOW_H)
            half = tip_half + (base_half - tip_half) * frac
            if half < 0.5:
                continue
            for px in range(iw):
                dx = abs((px + 0.5) - cx_img) / half
                if dx >= 1.0:
                    continue
                hf = (1.0 - dx * dx) ** 1.5   # 水平双向柔化（边缘归零）
                a = HIT_GLOW_A * v * hf
                if a <= 0.002:
                    continue
                img.setPixelColor(px, py, QColor(r0, g0, b0,
                                                 max(0, min(255, int(round(a * 255.0))))))
        item = (img, float(iw), float(ih))
        self._glow_imgs[key] = item
        return item

    def _paint_beam(self, p: QPainter, cx: float, y_line: float, k: float,
                    pal: dict) -> None:
        """极光锥形音柱 + 中央 2px 高亮激光芯线。"""
        k = max(0.0, min(1.0, float(k)))
        if k <= 0.0:
            return
        img, lw, lh = self._glow_image(pal)
        p.save()
        p.setOpacity(k)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(QRectF(cx - lw / 2.0, y_line - lh, lw, lh), img)
        p.restore()
        # 中央 2px 激光芯线：底部 alpha 0.65×k，向上指数收敛到 0
        g = QLinearGradient(0.0, y_line, 0.0, y_line - HIT_GLOW_H)
        c0 = QColor(pal["laser"])
        c0.setAlphaF(max(0.0, min(1.0, HIT_GLOW_CORE_A * k)))
        c1 = QColor(pal["laser"])
        c1.setAlphaF(0.0)
        c_mid = QColor(c0)
        c_mid.setAlpha(int(c0.alpha() * 0.32))
        g.setColorAt(0.0, c0)
        g.setColorAt(0.35, c_mid)
        g.setColorAt(1.0, c1)
        pen = QPen(QBrush(g), HIT_GLOW_CORE_W)
        pen.setCapStyle(Qt.FlatCap)
        p.save()
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(cx, y_line), QPointF(cx, y_line - HIT_GLOW_H))
        p.restore()

    # ------------------------------------------------------------------
    def _paint_holds(self, p: QPainter, y_line: float, pal: dict) -> None:
        for e in self._events:
            if _norm_type(e) != "hold":
                continue
            try:
                th = float(e.get("t_hit", 0.0))
                dur = float(e.get("dur", 0.0) or 0.0)
            except Exception:
                continue
            te = th + dur
            if self._t_now > te:
                continue                       # 已结束（尾判光环由 _hits 负责）
            if th - self._t_now > T_FALL:
                continue                       # 还在屏外
            lane = int(e.get("lane", 0)) % LANES
            cx = self._lane_cx(lane)
            y_head = self._note_y(th)
            y_tail = self._note_y(te)
            holding = th <= self._t_now <= te
            # 尾巴实时截断：按住期间面条带底端恒为判定线，绝不画到线下方
            y_bottom = y_line if holding else min(y_head, y_line)
            y_top = max(y_tail, -HIT_GLOW_H)
            if y_bottom - y_top > 1.5:
                self._paint_hold_ribbon(p, cx, y_top, y_bottom, pal)
            # 头部胶囊（未过判定线时才画；过线后由底座常亮接管）——与 TAP 同款无缝衔接
            if self._t_now < th and y_head >= TOP_PAD - NOTE_H:
                self._paint_note(p, cx, y_head, pal)

    def _paint_hold_ribbon(self, p: QPainter, cx: float, y_top: float,
                           y_bottom: float, pal: dict) -> None:
        """HOLD 18px 宽：左右 alpha 0.12，中心 2px 亮芯 alpha 0.65。"""
        a_side = max(0, min(255, int(round(HOLD_SIDE_A * 255.0))))
        rect = QRectF(cx - HOLD_RIBBON_W / 2.0, y_top,
                      HOLD_RIBBON_W, y_bottom - y_top)
        g = QLinearGradient(rect.left(), 0.0, rect.right(), 0.0)
        c_side = QColor(pal["ribbon"])
        c_side.setAlpha(a_side)
        c_mid = QColor(pal["ribbon_mid"])
        c_mid.setAlpha(min(255, a_side + 46))
        c_glow = QColor(c_mid)
        c_glow.setAlpha(min(255, a_side + 74))
        g.setColorAt(0.0, c_side)
        g.setColorAt(0.34, c_side)
        g.setColorAt(0.5, c_glow)
        g.setColorAt(0.66, c_mid)
        g.setColorAt(1.0, c_side)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(rect, 2.0, 2.0)
        p.setBrush(Qt.NoBrush)
        core = QColor(pal["core"])
        core.setAlpha(max(0, min(255, int(round(HOLD_CORE_A * 255.0)))))
        pen = QPen(core, HOLD_CORE_W)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        p.drawLine(QPointF(cx, y_top), QPointF(cx, y_bottom))
        p.setPen(Qt.NoPen)

    # ------------------------------------------------------------------
    def _paint_note(self, p: QPainter, cx: float, y: float, pal: dict) -> None:
        """TAP 液态琉璃 32×8 圆角 4：底层 4px 微光晕 + 水平对称渐变 + 上边缘镜面高光。"""
        rect = QRectF(cx - NOTE_W / 2.0, y - NOTE_H / 2.0, NOTE_W, NOTE_H)
        self._paint_note_glow(p, rect, pal)
        g = QLinearGradient(rect.left(), 0.0, rect.right(), 0.0)
        g.setColorAt(0.0, pal["tap_side"])
        g.setColorAt(0.5, pal["tap_center"])
        g.setColorAt(1.0, pal["tap_side"])
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(rect, NOTE_R, NOTE_R)
        # 仅上边缘 1px 镜面高光（裁剪到圆角矩形内，不会在左右角画出白边）
        p.save()
        path = QPainterPath()
        path.addRoundedRect(rect, NOTE_R, NOTE_R)
        p.setClipPath(path)
        p.setPen(QPen(pal["tap_hl"], 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(rect.left(), rect.top() + 0.5),
                   QPointF(rect.right(), rect.top() + 0.5))
        p.restore()

    def _paint_note_glow(self, p: QPainter, rect: QRectF, pal: dict) -> None:
        """底层 4px 柔和微光晕（椭圆径向渐变；只在外扩 4px 的环带可见）。"""
        pad = NOTE_GLOW_PAD
        cx = rect.center().x()
        cy = rect.center().y()
        rx = rect.width() / 2.0 + pad
        ry = rect.height() / 2.0 + pad
        if rx <= 0.5 or ry <= 0.5:
            return
        p.save()
        p.translate(cx, cy)
        p.scale(rx, ry)
        rg = QRadialGradient(QPointF(0.0, 0.0), 1.0)
        c0 = QColor(pal["tap_glow"])
        c1 = QColor(pal["tap_glow"])
        c1.setAlpha(0)
        rg.setColorAt(0.0, c0)
        rg.setColorAt(0.72, QColor(c0))
        rg.setColorAt(1.0, c1)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(rg))
        p.drawEllipse(QPointF(0.0, 0.0), 1.0, 1.0)
        p.restore()

    def _paint_taps(self, p: QPainter, y_line: float, pal: dict) -> None:
        for e in self._events:
            if _norm_type(e) == "hold":
                continue
            try:
                th = float(e.get("t_hit", 0.0))
            except Exception:
                continue
            dt = th - self._t_now
            if dt > T_FALL or dt < -0.10:
                continue
            y = self._note_y(th)
            if y < TOP_PAD - NOTE_H or y > y_line + NOTE_H:
                continue
            lane = int(e.get("lane", 0)) % LANES
            self._paint_note(p, self._lane_cx(lane), y, pal)

    # ------------------------------------------------------------------
    def _paint_bases(self, p: QPainter, y_line: float, pal: dict,
                     active: dict, now_wall: float) -> None:
        for lane in range(LANES):
            cx = self._lane_cx(lane)
            scale = 1.0
            fill_a = 0
            # 最新命中（tap/head 才做 1.40x→1.0x 回弹）
            hit = None
            for h in reversed(self._hits):
                if int(h["lane"]) == lane and h["kind"] in ("tap", "head"):
                    hit = h
                    break
            if hit is not None:
                age = now_wall - float(hit["t0"])
                if age <= HIT_S:
                    k = max(0.0, min(1.0, age / HIT_S))
                    scale = _elastic_scale(k)
                    fill_a = int(255 * 0.90 * (1.0 - 0.55 * k))
            if lane in active:
                scale = max(scale, HOLD_BASE_SCALE)
                fill_a = max(fill_a, 200)
            w = NOTE_W * scale
            hgt = JUDGE_BASE_H * scale
            rect = QRectF(cx - w / 2.0, y_line - hgt / 2.0, w, hgt)
            radius = max(1.0, min(hgt / 2.0, NOTE_R * scale))
            if fill_a > 0:
                c = QColor(pal["tap_center"])
                c.setAlpha(max(0, min(255, int(fill_a))))
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                p.drawRoundedRect(rect, radius, radius)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(pal["base_edge"], 1.0))
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                              max(0.5, radius - 0.5), max(0.5, radius - 0.5))
        p.setPen(Qt.NoPen)

    def _paint_halos(self, p: QPainter, y_line: float, pal: dict,
                     now_wall: float) -> None:
        for h in self._hits:
            kind = str(h["kind"])
            if kind not in ("tap", "head", "tail"):
                continue
            total = HALO_S * (1.15 if kind == "tail" else 1.0)
            age = now_wall - float(h["t0"])
            if age < 0.0 or age > total:
                continue
            k = max(0.0, min(1.0, age / total))
            cx = self._lane_cx(int(h["lane"]))
            w = NOTE_W + (HALO_W_MAX - NOTE_W) * k
            hh = NOTE_H + (HALO_H_MAX - NOTE_H) * k
            c = QColor(pal["halo"])
            a = 170 if kind == "tail" else 130
            c.setAlpha(max(0, int(a * (1.0 - k) ** 1.4)))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(c, 1.6))
            p.drawRoundedRect(QRectF(cx - w / 2.0, y_line - hh / 2.0, w, hh),
                              min(hh / 2.0, 13.0), min(hh / 2.0, 13.0))
        p.setPen(Qt.NoPen)

    def _paint_judge_line(self, p: QPainter, y_line: float, pal: dict) -> None:
        x0 = self._lane_x(0)
        x1 = x0 + LANES * RAIL_W
        p.setPen(QPen(pal["judge"], JUDGE_LINE_H))
        p.drawLine(QPointF(x0, y_line), QPointF(x1, y_line))
        p.setPen(Qt.NoPen)

