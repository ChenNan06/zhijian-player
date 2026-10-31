"""冰晶流光琴键 V2（task-132）—— 4 轨音瀑，右侧 180px 窄区，严禁铺满全屏。

视觉规格（严格照卡面，全部由控件真实几何推导，无硬编码坐标）：
  · TAP：高 10px × 宽 34px、圆角 5px；渐变底 + 1px 高亮描边
         （浅色 rgba(15,98,254,0.85) / 深色 rgba(120,220,255,0.90)）；
  · HOLD：头部 10px 胶囊 + 身后 24px 宽半透明冰蓝流光面条带 + 中轴亮芯线；
         经过判定线时尾巴实时截断（绝不画到判定线下方）；
  · 判定底座：4 枚高 10px 圆角胶囊（常态半透明空心框）；
         命中：底座填亮 + 1.40x→1.0x 150ms 弹性回弹 + 110px 高 / 不透明度 0.50 渐变光柱
               + 34×10→52×26 扩散光环淡出；
  · HOLD 经过判定线：该轨底座常亮放大 + 持续呼吸光柱；结束尾判光环。

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
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

log = logging.getLogger("zhijian.ui.rhythm")

LANES = 4
RAIL_W = 40.0
STAGE_W = 180

# ---- 音符 / 判定几何（卡面数值；位置一律由 lane 中心 + 判定线推导）--------------------
NOTE_W = 34.0
NOTE_H = 10.0
NOTE_R = 5.0
JUDGE_BASE_H = 10.0
JUDGE_LINE_H = 1.5
JUDGE_INSET = 18.0          # 判定线在 RhythmStage 底部内缩 18px
TOP_PAD = 8.0               # 音符最高可绘制到 y=8（再往上就是屏外）
T_FALL = 1.6                # 落速：音符从顶落到判定线 1.6s

# ---- 命中反馈时长 / 尺寸（卡面数值）-----------------------------------------------
HIT_S = 0.150               # 1.40x→1.0x 弹性回弹 150ms
HALO_S = 0.260              # 34×10→52×26 扩散光环淡出
HIT_GLOW_H = 110.0          # 光柱高 110px
HIT_GLOW_A = 0.50           # 光柱不透明度 0.50
HOLD_RIBBON_W = 24.0        # HOLD 面条带 24px
HOLD_CORE_W = 2.0           # 中轴亮芯线宽
HOLD_BASE_SCALE = 1.12      # HOLD 经过判定线时该轨底座放大倍数（常亮）

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
            "style": "pitch_ribbon_v2",
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
                        "w": round(NOTE_W + (52.0 - NOTE_W) * kk, 2),
                        "h": round(NOTE_H + (26.0 - NOTE_H) * kk, 2),
                        "range_w": [NOTE_W, 52.0], "range_h": [NOTE_H, 26.0],
                    }
        active_detail = {}
        for lane, ev in self.active_holds().items():
            active_detail[str(int(lane))] = {
                "dur": round(float(ev.get("dur", 0.0) or 0.0), 3),
                "base_scale": HOLD_BASE_SCALE,
                "ribbon_bottom_y": round(judge, 2),   # 燃烧中尾巴底端 = 判定线（实时截断）
                "judge_y": round(judge, 2),
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
        if light:
            return {
                "tap_a": QColor(15, 98, 254, 217),      # rgba(15,98,254,.85)
                "tap_b": QColor(126, 176, 255, 242),    # 冰白高亮端
                "edge": QColor(255, 255, 255, 235),
                "rail": QColor(24, 28, 36, 12),
                "rail_line": QColor(24, 28, 36, 22),
                "judge": QColor(15, 98, 254, 190),
                "base_edge": QColor(15, 98, 254, 88),
                "base_fill": QColor(15, 98, 254, 18),
                "glow": QColor(15, 98, 254, 128),
                "ribbon": QColor(15, 98, 254, 44),
                "core": QColor(15, 98, 254, 168),
                "halo": QColor(15, 98, 254, 170),
            }
        return {
            "tap_a": QColor(120, 220, 255, 230),        # rgba(120,220,255,.90)
            "tap_b": QColor(235, 250, 255, 240),        # 冰白高亮端
            "edge": QColor(240, 252, 255, 235),
            "rail": QColor(255, 255, 255, 10),
            "rail_line": QColor(255, 255, 255, 20),
            "judge": QColor(120, 220, 255, 190),
            "base_edge": QColor(120, 220, 255, 84),
            "base_fill": QColor(120, 220, 255, 16),
            "glow": QColor(120, 220, 255, 128),
            "ribbon": QColor(120, 220, 255, 50),
            "core": QColor(210, 245, 255, 190),
            "halo": QColor(165, 232, 255, 175),
        }

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
            self._paint_rails(p, y_line, pal)
            self._paint_glows(p, y_line, pal, active, now_wall)
            self._paint_holds(p, y_line, pal)
            self._paint_taps(p, y_line, pal)
            self._paint_bases(p, y_line, pal, active, now_wall)
            self._paint_halos(p, y_line, pal, now_wall)
            self._paint_judge_line(p, y_line, pal)
        finally:
            p.end()

    def _paint_rails(self, p: QPainter, y_line: float, pal: dict) -> None:
        for i in range(LANES):
            x = self._lane_x(i)
            p.fillRect(QRectF(x, 0.0, RAIL_W, y_line), pal["rail"])
            p.setPen(QPen(pal["rail_line"], 1.0))
            p.drawLine(QPointF(x, 0.0), QPointF(x, y_line))
        p.setPen(Qt.NoPen)

    def _paint_glows(self, p: QPainter, y_line: float, pal: dict,
                     active: dict, now_wall: float) -> None:
        top = y_line - HIT_GLOW_H
        # ① 命中光柱（110px 高、不透明度 0.50 的渐变）
        for h in self._hits:
            if h["kind"] not in ("tap", "head"):
                continue
            age = now_wall - float(h["t0"])
            if age > 0.24:
                continue
            k = max(0.0, 1.0 - age / 0.24)
            cx = self._lane_cx(int(h["lane"]))
            g = QLinearGradient(0.0, y_line, 0.0, top)
            c0 = QColor(pal["glow"])
            c0.setAlpha(max(0, min(255, int(255 * HIT_GLOW_A * k))))
            c1 = QColor(pal["glow"])
            c1.setAlpha(0)
            g.setColorAt(0.0, c0)
            g.setColorAt(1.0, c1)
            p.fillRect(QRectF(cx - RAIL_W / 2.0 + 2.0, top, RAIL_W - 4.0, HIT_GLOW_H), g)
        # ② HOLD 呼吸光柱（按住期间常亮 + 呼吸）
        for lane in sorted(active):
            breath = 0.5 + 0.5 * math.sin(now_wall * 2.0 * math.pi * 1.5 + lane * 1.3)
            a = int(255 * HIT_GLOW_A * (0.55 + 0.45 * breath))
            cx = self._lane_cx(int(lane))
            g = QLinearGradient(0.0, y_line, 0.0, top)
            c0 = QColor(pal["glow"])
            c0.setAlpha(max(0, min(255, a)))
            c1 = QColor(pal["glow"])
            c1.setAlpha(0)
            g.setColorAt(0.0, c0)
            g.setColorAt(1.0, c1)
            p.fillRect(QRectF(cx - RAIL_W / 2.0 + 2.0, top, RAIL_W - 4.0, HIT_GLOW_H), g)

    def _paint_holds(self, p: QPainter, y_line: float, pal: dict) -> None:
        for e in self._events:
            if e.get("type") != "hold":
                continue
            th = float(e.get("t_hit", 0.0))
            dur = float(e.get("dur", 0.0) or 0.0)
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
                g = QLinearGradient(0.0, y_top, 0.0, y_bottom)
                c0 = QColor(pal["ribbon"])
                c0.setAlpha(max(0, int(pal["ribbon"].alpha() * 0.55)))
                g.setColorAt(0.0, c0)
                g.setColorAt(1.0, QColor(pal["ribbon"]))
                p.setPen(Qt.NoPen)
                p.setBrush(g)
                p.drawRoundedRect(QRectF(cx - HOLD_RIBBON_W / 2.0, y_top,
                                         HOLD_RIBBON_W, y_bottom - y_top), 2.0, 2.0)
                # 流光高光条（随时间向判定线流动）
                phase = (self._t_now * 140.0) % 46.0
                yy = y_top + phase
                while yy < y_bottom:
                    hh = min(14.0, y_bottom - yy)
                    if hh > 1.0:
                        c = QColor(pal["core"])
                        c.setAlpha(64)
                        p.setBrush(c)
                        p.drawRoundedRect(QRectF(cx - HOLD_RIBBON_W / 2.0 + 2.0, yy,
                                                 HOLD_RIBBON_W - 4.0, hh), 2.0, 2.0)
                    yy += 46.0
                p.setBrush(Qt.NoBrush)
                # 中轴亮芯线
                p.setPen(QPen(pal["core"], HOLD_CORE_W))
                p.drawLine(QPointF(cx, y_top), QPointF(cx, y_bottom))
                p.setPen(Qt.NoPen)
            # 头部胶囊（未过判定线时才画；过线后由底座常亮接管）
            if self._t_now < th and y_head >= TOP_PAD - NOTE_H:
                self._paint_note_capsule(p, cx, y_head, pal)

    def _paint_note_capsule(self, p: QPainter, cx: float, y: float, pal: dict) -> None:
        rect = QRectF(cx - NOTE_W / 2.0, y - NOTE_H / 2.0, NOTE_W, NOTE_H)
        g = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        g.setColorAt(0.0, pal["tap_b"])
        g.setColorAt(1.0, pal["tap_a"])
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(rect, NOTE_R, NOTE_R)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(pal["edge"], 1.0))
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                          max(0.5, NOTE_R - 0.5), max(0.5, NOTE_R - 0.5))
        p.setPen(Qt.NoPen)

    def _paint_taps(self, p: QPainter, y_line: float, pal: dict) -> None:
        for e in self._events:
            if e.get("type") == "hold":
                continue
            th = float(e.get("t_hit", 0.0))
            dt = th - self._t_now
            if dt > T_FALL or dt < -0.10:
                continue
            lane = int(e.get("lane", 0)) % LANES
            y = self._note_y(th)
            if y < TOP_PAD - NOTE_H or y > y_line + NOTE_H:
                continue
            self._paint_note_capsule(p, self._lane_cx(lane), y, pal)

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
                c = QColor(pal["tap_a"])
                c.setAlpha(max(0, min(255, int(fill_a))))
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                p.drawRoundedRect(rect, radius, radius)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(pal["base_edge"], 1.0))
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                              max(0.5, radius - 0.5), max(0.5, radius - 0.5))
        p.setPen(Qt.NoPen)

    def _paint_halos(self, p: QPainter, y_line: float, pal: dict, now_wall: float) -> None:
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
            w = NOTE_W + (52.0 - NOTE_W) * k
            hh = NOTE_H + (26.0 - NOTE_H) * k
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
