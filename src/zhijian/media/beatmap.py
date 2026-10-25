"""★ task-122：音瀑谱面生成（后台线程，ffmpeg 解 PCM ⇒ 四频段事件表）。

卡面口径：ffmpeg `-ac 1 -ar 11025 -f s16le -` 整曲流式读 PCM；四频段 20–130 /
130–500 / 500–2800 / 2800–8000 Hz；短时能量一阶正向差分；滑动窗自适应阈值
`LocalMean + 1.4×Std` 取峰；同轨最小间隔 ≥120ms；事件分 TAP / DOUBLE（±25ms）/ HOLD（≥450ms）。

⚠️ 工程约束：**不依赖 numpy**（应用运行时没有 numpy，纯标准库实现）。
   频段分离用三只一阶低通（130/500/2800Hz）的差分：
     band0 = lp130、band1 = lp500−lp130、band2 = lp2800−lp500、band3 = raw−lp2800。
   逐样本 O(1)，10ms 一块统计能量 ⇒ 整曲解码+打分在秒级完成。

事件 dict：`{t_hit: 秒, track: 0..3, type: "tap"/"double"/"hold", dur: 秒, band: int}`
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess

CMD_SPAWN_COUNT = 0   # ★ task-126：cmd 黑框检测计数（每次 spawn +1）
from array import array

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)

SR = 11025
HOP = 110                      # 10ms 帧移 ⇒ 分辨率 10ms（判据 ≤16ms）
THR_K = 1.4                    # 自适应阈值倍数
LOCAL_FRAMES = 60              # 0.6s 窗
MIN_GAP = 12                   # 120ms
HOLD_MIN = 45                  # 450ms
DBL_TOL = 2                    # ±2 帧 ≈ ±20ms（卡面 ±25ms 内）
BAND_NAMES = ("20-130", "130-500", "500-2800", "2800-8000")


def ffmpeg_exe() -> str:
    """优先 imageio_ffmpeg 自带（可随包分发），其次 PATH。"""
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg") or "ffmpeg"


def _read_pcm(path: str):
    """ffmpeg ⇒ int16 单声道样本数组（array('h')）。"""
    cmd = [ffmpeg_exe(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "-"]
    # ★ v1.0.51（task-126）：必须带 CREATE_NO_WINDOW，否则每次切歌都会弹出黑色 cmd 窗口。
    #   本项目已统一规则：任何 subprocess 调用都要 CREATE_NO_WINDOW + 隐藏 STARTUPINFO。
    _si = subprocess.STARTUPINFO()
    _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _si.wShowWindow = 0  # SW_HIDE
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                          startupinfo=_si)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg 解码失败: %s" % (proc.stderr or b"")[-160:])
    a = array("h")
    a.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    return a


def _band_flux(x):
    """四频段 × 每 10ms 的**能量**与**能量一阶正向差分**。

    返回 `(flux, energy)`：flux 用于取峰；energy 用于 **HOLD 的能量包络判定**（卡面 ④）。"""
    n_blocks = max(0, len(x) // HOP)
    flux = [[0.0] * n_blocks for _ in range(4)]
    energy = [[0.0] * n_blocks for _ in range(4)]
    if n_blocks == 0:
        return flux, energy
    a1 = math.exp(-2.0 * math.pi * 130.0 / SR)
    a2 = math.exp(-2.0 * math.pi * 500.0 / SR)
    a3 = math.exp(-2.0 * math.pi * 2800.0 / SR)
    lp1 = lp2 = lp3 = 0.0
    acc = [0.0, 0.0, 0.0, 0.0]
    prev = [0.0, 0.0, 0.0, 0.0]
    k = 0
    inv = 1.0 / HOP
    for i, s in enumerate(x):
        v = s / 32768.0
        lp1 = a1 * lp1 + (1.0 - a1) * v
        lp2 = a2 * lp2 + (1.0 - a2) * v
        lp3 = a3 * lp3 + (1.0 - a3) * v
        b0 = lp1
        b1 = lp2 - lp1
        b2 = lp3 - lp2
        b3 = v - lp3
        acc[0] += b0 * b0
        acc[1] += b1 * b1
        acc[2] += b2 * b2
        acc[3] += b3 * b3
        if (i + 1) % HOP == 0:
            if k < n_blocks:
                for bi in range(4):
                    e = acc[bi] * inv
                    energy[bi][k] = e
                    d = e - prev[bi]
                    prev[bi] = e
                    flux[bi][k] = d if d > 0.0 else 0.0
                    acc[bi] = 0.0
            k += 1
    return flux, energy


def _adaptive_peaks(flux: list) -> list:
    """滑动窗自适应阈值取峰（LocalMean + 1.4×Std；峰需大于左右邻居）。"""
    peaks = []
    n = len(flux[0]) if flux else 0
    for bi in range(len(flux)):
        v = flux[bi]
        last = -10 ** 9
        # ⚠️ 第一版用**从 0 累加**的均值/方差，窗口外样本全算进来 ⇒ 阈值随时间虚高 ⇒ **0 个峰** ✗。
        #   这里改成**真滑动窗**（0.6s = 60 帧，逐帧切片算 mean/std；纯 Python 可接受）
        for i in range(8, n - 1):
            a = max(0, i - LOCAL_FRAMES)
            w = v[a:i + 1]
            cnt = len(w)
            if cnt < 8:
                continue
            mean = sum(w) / cnt
            var = sum((t - mean) * (t - mean) for t in w) / cnt
            thr = mean + THR_K * math.sqrt(max(0.0, var))
            if v[i] >= thr and v[i] >= v[i - 1] and v[i] >= v[i + 1] and (i - last) >= MIN_GAP:
                # ★★ task-122②-1：**亚帧化** —— 三点抛物线插值求峰的真实位置（不再受 10ms 网格限制）
                #   `delta = 0.5*(y[-1]-y[+1]) / (y[-1] - 2y[0] + y[+1])`，单位=块 ⇒ ×HOP/SR。
                y0, y1, y2 = float(v[i - 1]), float(v[i]), float(v[i + 1])
                denom = y0 - 2.0 * y1 + y2
                if abs(denom) > 1e-12:
                    delta = 0.5 * (y0 - y2) / denom
                    delta = max(-0.5, min(0.5, delta))
                else:
                    delta = 0.0
                peaks.append({"t_hit": (i + delta) * HOP / float(SR), "track": bi, "band": bi,
                              "amp": float(v[i]), "thr": float(thr),
                              "subframe": round(float(delta), 3)})
                last = i
    peaks.sort(key=lambda p: p["t_hit"])
    return peaks


def _detect_holds(energy: list) -> list:
    """★ task-122④ 定稿：**独立**于峰值检测扫描能量平台，生成 HOLD 事件。

    ⚠️ 教训：把 HOLD 挂在"已检测到的峰"上会漏 —— 实测长音起音前有一个**零能量伪峰**，
    真实起音被 MIN_GAP(120ms) 不应期挡掉 ⇒ HOLD 恒 0 ✗。所以 HOLD 必须**自己扫**：
      · 起音对比：当前能量 ≥ 前 0.6s 中位能量 ×3（真·新音符）；
      · 平台：能量 ≥ 0.55×E0 的连续段 ≥450ms，且段内 max/min <2.5（平稳长音）；
      · 上限 3s（防串到下一句）。
    """
    holds = []
    for bi in range(len(energy)):
        env = energy[bi]
        n = len(env)
        i = 10
        while i < n - 1:
            pre = env[max(0, i - LOCAL_FRAMES):max(1, i - 5)]
            ps = sorted(pre)
            pre_med = ps[len(ps) // 2] if ps else 0.0
            e0 = float(env[i])
            if e0 <= max(pre_med * 3.0, 1e-7):
                i += 1
                continue
            thr = e0 * 0.55
            j = i
            while j + 1 < n and (j - i) * HOP / float(SR) < 3.0 and env[j + 1] >= thr:
                j += 1
            dur = (j - i) * HOP / float(SR)
            seg = env[i:j + 1]
            flat = (max(seg) / max(1e-9, min(seg))) if seg else 99.0
            if dur >= HOLD_MIN * HOP / float(SR) and flat < 2.5:
                holds.append({"t_hit": i * HOP / float(SR), "track": bi, "band": bi,
                              "type": "hold", "dur": round(dur, 3)})
                i = j + 1
                continue
            i += 1
    return holds


def _merge(peaks: list, flux: list, energy: list | None = None) -> list:
    """TAP / DOUBLE（±25ms 双频）/ HOLD（能量维持 ≥450ms）。"""
    ev = []
    used = set()
    for i, p in enumerate(peaks):
        if i in used:
            continue
        tol = DBL_TOL * HOP / float(SR)
        partners = [j for j in range(i + 1, min(len(peaks), i + 8))
                    if j not in used and abs(peaks[j]["t_hit"] - p["t_hit"]) <= tol
                    and peaks[j]["track"] != p["track"]]
        if partners:
            used.add(partners[0])
            ev.append({"t_hit": p["t_hit"], "track": p["track"], "type": "double",
                       "dur": 0.0, "band": p["band"], "subframe": p.get("subframe", 0.0)})
        else:
            ev.append({"t_hit": p["t_hit"], "track": p["track"], "type": "tap",
                       "dur": 0.0, "band": p["band"], "subframe": p.get("subframe", 0.0)})
    # ★★ task-122④（Lead 明确要求）：HOLD 判据改为 **"能量包络维持 ≥450ms"** ——
    #   旧的"峰密度启发式"会把密集鼓点合并成 106 个 HOLD（实测过合并 ✗），这里换成：
    #   从峰值帧出发，只要该频段**能量**保持在 `max(0.45×峰值能量, 局部均值×1.15)` 之上，
    #   就认为"还在持续"；持续时间 ≥450ms ⇒ HOLD，dur = 实测持续时长（上限 3s 防串到下一句）。
    if energy is None:
        return ev
    # ★★ task-122④（定稿）：HOLD = **能量平台宽度**判据
    #   取峰值邻域的最大能量 E0 ⇒ 向前后扩展"能量 ≥ 0.55·E0"的连续段 ⇒ 段长 ≥450ms 即 HOLD。
    #   ⚠️ 此前两版都错：①"含峰均值做门槛"⇒ 恒 0；②"onset 前基线"⇒ 对持续音基线≈峰值 ⇒
    #   被 `peak<=base*1.05` 否掉 ⇒ **合成 2s 长音仍然 0 个 HOLD**（实测踩到）✗。
    out = []
    for e in ev:
        f = int(round(e["t_hit"] * SR / HOP))
        bi = int(e["band"])
        env = energy[bi] if 0 <= bi < len(energy) else []
        if not (0 <= f < len(env)):
            out.append(e)
            continue
        lo = max(0, f - 5)
        hi = min(len(env), f + 6)
        e0 = max(env[lo:hi]) if hi > lo else 0.0
        if e0 <= 1e-9:
            out.append(e)
            continue
        # ⚠️ 只按"平台宽度"会把**整首流行乐全部判成 HOLD**（实测 475/475 ✗）—— 因为音乐里
        #   每个频段几乎总有能量。HOLD 的语义是"**从安静里起来的长音**"，必须加两道约束：
        #   ① 起音对比：E0 至少是**起音前**中位能量的 3 倍（真·新音符，而不是持续伴奏）；
        #   ② 平台平稳：平台内 max/min 能量比 <2.5（长音保持，而不是逐渐衰减的鼓点）。
        pre = env[max(0, f - 60):max(1, f - 10)]
        pre_sorted = sorted(pre)
        pre_med = pre_sorted[len(pre_sorted) // 2] if pre_sorted else 0.0
        if e0 < max(pre_med * 3.0, 1e-7):
            out.append(e)
            continue
        thr = e0 * 0.55
        a2 = f
        while a2 - 1 >= 0 and env[a2 - 1] >= thr:
            a2 -= 1
        b2 = f
        while b2 + 1 < len(env) and (b2 - a2) * HOP / float(SR) < 3.0 and env[b2 + 1] >= thr:
            b2 += 1
        out.append(e)
    # ★ 定稿：HOLD 由 `_detect_holds()` **独立**生成；落在 hold 区间内的 tap/double 丢弃
    if energy is not None:
        holds = _detect_holds(energy)
        if holds:
            kept = []
            for e in out:
                th = float(e["t_hit"])
                inside = any(float(h["t_hit"]) - 0.05 <= th <= float(h["t_hit"]) + float(h["dur"]) + 0.05
                             for h in holds)
                if not inside:
                    kept.append(e)
            out = sorted(kept + holds, key=lambda e: e["t_hit"])
    return out

    def run(self) -> None:  # noqa: D401
        try:
            x = _read_pcm(self._path)
            self.progress.emit(45)
            flux, energy = _band_flux(x)
            ev = _merge(_adaptive_peaks(flux), flux, energy)
            self.progress.emit(100)
            self.done.emit(ev)
        except Exception as exc:  # noqa: BLE001
            log.warning("beatmap 失败: %s", exc, exc_info=True)
            self.failed.emit(str(exc))


class BeatmapWorker(QThread):
    """后台线程：解 PCM ⇒ 四频段 flux ⇒ 自适应取峰 ⇒ TAP/DOUBLE/HOLD。"""

    done = Signal(list)
    failed = Signal(str)
    progress = Signal(int)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = str(path or "")

    def run(self) -> None:  # noqa: D401
        try:
            x = _read_pcm(self._path)
            self.progress.emit(45)
            flux, energy = _band_flux(x)
            ev = _merge(_adaptive_peaks(flux), flux, energy)
            self.progress.emit(100)
            self.done.emit(ev)
        except Exception as exc:  # noqa: BLE001
            log.warning("beatmap 失败: %s", exc, exc_info=True)
            self.failed.emit(str(exc))

def build_beatmap(path: str) -> list:
    """同步版本（探针/单测用）。"""
    x = _read_pcm(path)
    flux, energy = _band_flux(x)
    return _merge(_adaptive_peaks(flux), flux, energy)


def stats(events: list) -> dict:
    """谱面统计（`rhythm_debug()` 锚点用）。"""
    taps = sum(1 for e in events if e.get("type") == "tap")
    doubles = sum(1 for e in events if e.get("type") == "double")
    holds = sum(1 for e in events if e.get("type") == "hold")
    subs = [e.get("subframe") for e in events if isinstance(e.get("subframe"), (int, float))]
    return {"n_events": len(events), "taps": taps, "doubles": doubles, "holds": holds,
            "subframe_n": len(subs),
            "subframe_mean": (round(sum(subs) / len(subs), 4) if subs else None),
            "first_t_hits": [round(float(e["t_hit"]), 3) for e in events[:8]],
            "last_t_hit": round(max([e["t_hit"] for e in events], default=0.0), 3),
            "bands": list(BAND_NAMES)}

def _bump_spawn() -> None:
    global CMD_SPAWN_COUNT
    CMD_SPAWN_COUNT += 1
