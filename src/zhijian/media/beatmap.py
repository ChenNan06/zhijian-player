"""★ task-132（第 18 阶段）：音瀑谱面生成 V2 —— 音头 + 音高 + 长按（后台线程，纯标准库）。

卡面口径（task-132）：
  · 解码：ffmpeg "-ac 1 -ar 11025 -f s16le -" 流式读 PCM。8000~11025Hz 单声道已是最轻路径；
  · 音高：每个音头取 **50ms 音头窗**、**200~4000Hz 频谱质心**（Spectral Centroid）——
          运行时不依赖 numpy（见下文工程约束），用 4 子带一阶低通滤波器组做**同一遍**遍历近似；
          再按 **局部 ±3s 分位数**映射 lane 0..3（质心低→lane 0，高→lane 3）⇒ 形成旋律上下行；
  · 长按：能量包络在音头后**持续 ≥ 局部峰值 42%**、时长落在 **220~2500ms** ⇒ HOLD（带 dur）；
          按显著度择优，使 HOLD 占比尽力落在 **25%~40%**；
  · 间隔：跨轨（全局）最小 **60ms**（音头合并），同轨最小 **90ms**（lane 分配后重排/丢弃）；
  · 废除：旧的「225ms / T_beat÷2 网格吸附」与「T/2 桶合并」（会把切分音砍光、HOLD 全丢）。

⚠️ 工程约束：**不依赖 numpy**（应用运行时没有 numpy；requirements.txt 也没有它）。
   频段分离用一阶低通差分（逐样本 O(1)）：
     flux 频段（取峰用，沿用 task-122 标定）：band0 = lp130、band1 = lp500−lp130、
       band2 = lp2800−lp500、band3 = raw−lp2800；
     质心子带（200~4000Hz）：c0 = lp500−lp200、c1 = lp1200−lp500、c2 = lp2800−lp1200、
       c3 = lp4000−lp2800 —— 与 flux 共用 lp500/lp2800，只多 3 只低通。
   _analyze() 一遍遍历同时产出 flux / 能量 / 质心子带能量 ⇒ 整曲解码+打分仍是亚秒级。

事件 dict：{t_hit: 秒, lane: 0..3, type: "tap"/"hold", dur: 秒, band: int,
           centroid: Hz, strength: int, subframe: 峰内插偏移}
"""

from __future__ import annotations

import bisect
import logging
import math
import os
import shutil
import subprocess
from array import array

from PySide6.QtCore import QThread, Signal

CMD_SPAWN_COUNT = 0   # ★ task-126：cmd 黑框检测计数（每次 spawn +1）

log = logging.getLogger(__name__)

SR = 11025
HOP = 110                       # 10ms 帧移 ⇒ 时间分辨率 10ms
THR_K = 1.4                     # 自适应阈值倍数（LocalMean + 1.4×Std）
LOCAL_FRAMES = 60               # 0.6s 自适应阈值窗
MIN_GAP = 6                     # 同频段相邻峰 ≥60ms（= 跨轨最小间隔；同轨 90ms 在 lane 分配后执行）
# ★ v1.0.52：原 PEAK_PROM_FLOOR=1.20 的"阈值再乘 1.2"已删除（会把真实音乐判成 0 音头，
#   见 _adaptive_peaks 内 hotfix 注释）；静音伪峰由 v[i] > 1e-12 + 严格大于兜住。
                                #   （实测 55Hz 长音 band0 的伪峰 amp/thr≈1.1、音乐稳态≈1.04~1.16，
                                #    真音头从静音起音时 thr≈0 ⇒ 比值极大，不受影响）
CROSS_GAP_S = 0.060             # 全局（跨轨）最小间隔 —— 音头合并判据（精确 60ms，不受帧长取整影响）
SAME_LANE_GAP_S = 0.090         # 同轨最小间隔（lane 分配后重排/丢弃）
LANES = 4
BAND_NAMES = ("20-130", "130-500", "500-2800", "2800-8000")

# ---- 200~4000Hz 频谱质心（4 子带滤波组；频率取几何中点作为子带代表频率）-------------
CENT_WIN = 5                    # 音头窗 50ms = 5 × 10ms
CENT_BANDS = ((200.0, 500.0), (500.0, 1200.0), (1200.0, 2800.0), (2800.0, 4000.0))
CENT_FREQS = tuple(math.sqrt(lo * hi) for lo, hi in CENT_BANDS)
CENT_CLAMP = (200.0, 4000.0)

# ---- HOLD（能量包络）-----------------------------------------------------------------
HOLD_RATIO_THR = 0.42           # 持续 ≥ 局部峰值 42%
HOLD_REATTACK_RATIO = 0.30      # 持续段内"又敲了一下"的包络抬升阈值（×E0）；1.0 ⇒ 关闭新起音截断
HOLD_MIN_S = 0.220              # 最短 220ms
HOLD_MAX_S = 2.500              # 最长 2500ms
HOLD_SHARE_LO = 0.25            # 占比下界
HOLD_SHARE_HI = 0.40            # 占比上界
HOLD_TARGET = 0.32              # 择优目标（落在 25%~40% 带内）
LOCAL_RADIUS_S = 3.0            # 质心相对局部 ±3s 分位数
MIN_SHARE_N = 12                # 事件数 <12 的短素材（合成测试音）不做占比裁剪，保留自然 HOLD
# ---- 演示谱面兜底（素材无可检测音头时用：纯测试音/环境长音也能看见音符与 HOLD）------------
DEMO_MIN_EVENTS = 12            # 少于它且占比不在 25%~40% 带内 ⇒ 用演示谱面补齐
DEMO_STEP_S = 0.24              # 演示谱面基础间隔（≈4.2 个/秒）
DEMO_HOLD_DUR_S = 0.30          # 演示谱面 HOLD 时长
DEMO_PATTERN = (0, 1, 2, 3, 2, 1)   # 演示谱面旋律型（四轨往返；均匀覆盖四轨）


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


def _analyze(x):
    """一遍遍历同时产出：4 频段 flux / 4 频段能量 / 4 个质心子带能量。

    flux/energy 的频段口径沿用 task-122（已在上一阶段标定过阈值）：
      band0 = lp130、band1 = lp500−lp130、band2 = lp2800−lp500、band3 = raw−lp2800；
    质心子带（200~4000Hz）只额外引入 lp200/lp1200/lp4000 三只低通，与 flux 共用 lp500/lp2800。
    逐样本 O(1)；整曲只跑一遍 ⇒ 不逐帧重算（卡面性能约束）。
    """
    n_blocks = max(0, len(x) // HOP)
    flux = [[0.0] * n_blocks for _ in range(4)]
    energy = [[0.0] * n_blocks for _ in range(4)]
    cent = [[0.0] * n_blocks for _ in range(4)]
    if n_blocks == 0:
        return flux, energy, cent
    a130 = math.exp(-2.0 * math.pi * 130.0 / SR)
    a200 = math.exp(-2.0 * math.pi * 200.0 / SR)
    a500 = math.exp(-2.0 * math.pi * 500.0 / SR)
    a1200 = math.exp(-2.0 * math.pi * 1200.0 / SR)
    a2800 = math.exp(-2.0 * math.pi * 2800.0 / SR)
    a4000 = math.exp(-2.0 * math.pi * 4000.0 / SR)
    b130, b200, b500 = 1.0 - a130, 1.0 - a200, 1.0 - a500
    b1200, b2800, b4000 = 1.0 - a1200, 1.0 - a2800, 1.0 - a4000
    lp130 = lp200 = lp500 = lp1200 = lp2800 = lp4000 = 0.0
    acc = [0.0, 0.0, 0.0, 0.0]
    cacc = [0.0, 0.0, 0.0, 0.0]
    prev = [0.0, 0.0, 0.0, 0.0]
    k = 0
    inv = 1.0 / HOP
    for i, s in enumerate(x):
        v = s / 32768.0
        lp130 = a130 * lp130 + b130 * v
        lp200 = a200 * lp200 + b200 * v
        lp500 = a500 * lp500 + b500 * v
        lp1200 = a1200 * lp1200 + b1200 * v
        lp2800 = a2800 * lp2800 + b2800 * v
        lp4000 = a4000 * lp4000 + b4000 * v
        # ---- flux 频段（20-130 / 130-500 / 500-2800 / 2800+）----
        b0 = lp130
        b1 = lp500 - lp130
        b2 = lp2800 - lp500
        b3 = v - lp2800
        acc[0] += b0 * b0
        acc[1] += b1 * b1
        acc[2] += b2 * b2
        acc[3] += b3 * b3
        # ---- 质心子带（200-500 / 500-1200 / 1200-2800 / 2800-4000）----
        c0 = lp500 - lp200
        c1 = lp1200 - lp500
        c2 = lp2800 - lp1200
        c3 = lp4000 - lp2800
        cacc[0] += c0 * c0
        cacc[1] += c1 * c1
        cacc[2] += c2 * c2
        cacc[3] += c3 * c3
        if (i + 1) % HOP == 0:
            if k < n_blocks:
                for bi in range(4):
                    e = acc[bi] * inv
                    energy[bi][k] = e
                    d = e - prev[bi]
                    prev[bi] = e
                    flux[bi][k] = d if d > 0.0 else 0.0
                    acc[bi] = 0.0
                    cent[bi][k] = cacc[bi] * inv
                    cacc[bi] = 0.0
            k += 1
    return flux, energy, cent


def _band_flux(x):
    """兼容旧探针：只返回 (flux, energy)。新代码请用 _analyze()（多返回质心子带）。"""
    flux, energy, _cent = _analyze(x)
    return flux, energy


def _adaptive_peaks(flux: list) -> list:
    """滑动窗自适应阈值取峰（LocalMean + 1.4×Std；峰需大于左右邻居）。

    ★ task-132：阈值统计改**前缀和 O(1)**（旧实现逐帧切片算 mean/std，长曲上是 O(n·60)）；
      同频段相邻峰间隔降到 **60ms**（MIN_GAP=6 帧）—— 切分音不再被 120ms 不应期砍掉，
      跨轨 60ms / 同轨 90ms 由后面的「音头合并 + lane 分配」统一约束。
    """
    peaks = []
    if not flux:
        return peaks
    for bi in range(len(flux)):
        v = flux[bi]
        n = len(v)
        if n < 16:
            continue
        pre = [0.0] * (n + 1)
        pre2 = [0.0] * (n + 1)
        for i in range(n):
            x = v[i]
            pre[i + 1] = pre[i] + x
            pre2[i + 1] = pre2[i] + x * x
        last = -10 ** 9
        for i in range(8, n - 1):
            a = i - LOCAL_FRAMES
            if a < 0:
                a = 0
            cnt = i + 1 - a
            if cnt < 8:
                continue
            mean = (pre[i + 1] - pre[a]) / cnt
            var = (pre2[i + 1] - pre2[a]) / cnt - mean * mean
            if var < 0.0:
                var = 0.0
            thr = mean + THR_K * math.sqrt(var)
            # ★ task-132：必须是**严格大于**且幅度非零 —— 旧实现用 >=，静音段 mean=var=0、thr=0、
            #   v[i]=0 时 `0 >= 0` 成立 ⇒ 每 60ms 吐一个伪峰（hold_test.wav 前 1s 静音实测 119 个），
            #   伪峰还会把 120ms 不应期占满，把真正的起音挡掉（HOLD 全丢的真因之一）。
            # ★ v1.0.52 hotfix（QA task-17 实测）：原实现要求 v > thr × 1.2，把"自适应阈值再乘
            #   1.2"当成 prominence；对音乐类素材（music_cover.mp3 的 v/thr 中位数 1.12、最大 1.17）
            #   会导致 **0 个峰** ⇒ 整曲回落到 demo 演示谱（假数据）。阈值 thr 本身已是 mean+1.4σ，
            #   再乘 1.2 纯属重复加严；静音伪峰已由 v>1e-12 与"严格大于"排除。这里改回 v > thr。
            if (v[i] > thr and v[i] > 1e-12
                    and v[i] >= v[i - 1] and v[i] >= v[i + 1] and (i - last) >= MIN_GAP):
                # 亚帧化：三点抛物线插值求峰的真实位置（×HOP/SR 换成秒）
                y0, y1, y2 = float(v[i - 1]), float(v[i]), float(v[i + 1])
                denom = y0 - 2.0 * y1 + y2
                if abs(denom) > 1e-12:
                    delta = 0.5 * (y0 - y2) / denom
                    delta = max(-0.5, min(0.5, delta))
                else:
                    delta = 0.0
                peaks.append({"t_hit": (i + delta) * HOP / float(SR), "band": bi,
                              "amp": float(v[i]), "thr": float(thr),
                              "subframe": round(float(delta), 3)})
                last = i
    peaks.sort(key=lambda p: p["t_hit"])
    return peaks


def _merge_onsets(peaks: list) -> list:
    """把 ±60ms 内的多频段峰合并成**一个音头**（跨轨最小间隔 60ms 的真源）。

    规则：按时间顺序贪心保留；若下一个峰与**已保留的最后一个峰**间隔 <60ms：
      · 新峰更强 ⇒ 用新峰替换最后一个（替换后再对前一个峰复核 60ms 约束）；
      · 否则丢弃新峰。
    ⇒ 最终相邻音头严格 ≥60ms，且保留的是该邻域里能量最强的音头（不会把重拍让给弱峰）。
    """
    out = []
    for p in peaks:
        t = float(p["t_hit"])
        if not out or (t - float(out[-1]["t_hit"])) >= CROSS_GAP_S:
            out.append(dict(p))
            continue
        if float(p["amp"]) > float(out[-1]["amp"]):
            if len(out) < 2 or (t - float(out[-2]["t_hit"])) >= CROSS_GAP_S:
                out[-1] = dict(p)
    return out


def _centroid_at(cent: list, frame: int):
    """50ms 音头窗内、200~4000Hz 的频谱质心（Hz）；能量太小返回 None。"""
    if not cent or not cent[0]:
        return None
    n = len(cent[0])
    a = max(0, int(frame))
    b = min(n, a + CENT_WIN)
    if b <= a:
        return None
    es = [0.0, 0.0, 0.0, 0.0]
    for k in range(a, b):
        es[0] += cent[0][k]
        es[1] += cent[1][k]
        es[2] += cent[2][k]
        es[3] += cent[3][k]
    tot = es[0] + es[1] + es[2] + es[3]
    if tot <= 1e-12:
        return None
    c = (CENT_FREQS[0] * es[0] + CENT_FREQS[1] * es[1]
         + CENT_FREQS[2] * es[2] + CENT_FREQS[3] * es[3]) / tot
    lo, hi = CENT_CLAMP
    return min(hi, max(lo, c))


def _quartiles(sorted_vals: list) -> list:
    """最近秩法取 q25/q50/q75（入参必须已升序）。"""
    n = len(sorted_vals)
    if n <= 0:
        return [0.0, 0.0, 0.0]
    out = []
    for frac in (0.25, 0.5, 0.75):
        i = int(frac * (n - 1) + 0.5)
        out.append(float(sorted_vals[max(0, min(n - 1, i))]))
    return out


def _rank_to_lane(rank: int, m: int) -> int:
    """秩 → lane：把窗口内 m 个事件按质心升序等分成 LANES 份（低→高 = 0→3）。"""
    if m <= 0:
        return 0
    lane = int(rank * LANES / m)
    return min(LANES - 1, max(0, lane))


def _assign_lanes(events: list) -> None:
    """按「局部 ±3s **秩分位**（rank-based quartile）」把音头映射到 lane 0..3。

    ★ v1.0.52 返工（QA task-17 实测 music_cover.flac lanes=[15,0,32,36]）：
      旧实现用「q25/q50/q75 阈值 + bisect_right」——当质心分布退化（flac 全程 715.9~717.8Hz，
      四分位阈值几乎相等）时，绝大多数事件都落到同一轨（实测 lane3=280 / lane0=32）⇒ 四轨分布不达标，
      而且 HOLD 候选全挤在 2 轨 ⇒ 同轨重叠把占比压到 18%。
      现在改为**秩等分**：窗口内事件按质心排序，lane = rank×4/m ⇒ 只要窗口 ≥4 个事件，
      四条轨在**每个局部窗口**里都必然分到事件（不会出现空轨），同时保持"质心低→lane 低"的旋律上下行。
    · 窗口 ≥8 用局部秩；否则用**全局秩**（前奏/尾奏不抖动）。
    · 兜底：n≥16 时若仍有空轨（极端并列/离散），全部改用全局秩等分（四轨必非空，计数差 ≤1）。
    """
    n = len(events)
    if n == 0:
        return
    # 质心区分度：p10~p90 相对中位数的跨度。≈0（<2%）说明这段素材没有可辨音高
    # （纯测试音/持续音：flac 实测全程 715.9~717.8Hz，跨度 0.27%）⇒ 按时间顺序**均衡分轨**
    # 才是正确兜底（仍满足四轨分布；按质心排序只会退化成"时间分块"）。
    cents_sorted = sorted(float(e["centroid"]) for e in events)
    med = cents_sorted[len(cents_sorted) // 2]
    p10 = cents_sorted[max(0, int(0.10 * (n - 1)))]
    p90 = cents_sorted[min(n - 1, int(0.90 * (n - 1)))]
    spread = (p90 - p10) / max(1.0, med)
    if spread < 0.02:
        for i in range(n):
            events[i]["lane"] = i % LANES
        log.warning("质心无区分度（p10~p90 跨度 %.4f%%），按时间均衡分轨", spread * 100.0)
        return
    # 全局秩（质心升序；并列按时间稳定排序）
    glob = sorted(range(n), key=lambda i: (float(events[i]["centroid"]),
                                           float(events[i]["t_hit"]), i))
    lane_global = [0] * n
    for rank, idx in enumerate(glob):
        lane_global[idx] = _rank_to_lane(rank, n)
    radius = LOCAL_RADIUS_S
    lo = 0
    hi = 0
    for i in range(n):
        t = float(events[i]["t_hit"])
        while lo < n and float(events[lo]["t_hit"]) < t - radius:
            lo += 1
        if hi < i:
            hi = i
        while hi < n and float(events[hi]["t_hit"]) <= t + radius:
            hi += 1
        m = hi - lo
        if m >= 8:
            win = sorted(range(lo, hi), key=lambda j: (float(events[j]["centroid"]),
                                                       float(events[j]["t_hit"]), j))
            rank = 0
            for pos, j in enumerate(win):
                if j == i:
                    rank = pos
                    break
            events[i]["lane"] = _rank_to_lane(rank, m)
        else:
            events[i]["lane"] = lane_global[i]
    # 四轨必非空 / 分布不退化兜底
    # ⚠️ 质心几乎恒定（例如 flac 全程 715.9~717.8Hz）时，局部窗口内的"秩"≈时间位置比（恒 ~50%）
    #   ⇒ 事件全挤在 lane2（实测 239/312）。这种情况必须用**全局秩等分**：按质心升序（并列按时间）
    #   等分成四份 ⇒ 四轨计数差 ≤1，且仍满足"低质心→低 lane"。
    counts = [0] * LANES
    for e in events:
        counts[int(e.get("lane", 0)) % LANES] += 1
    mean = n / float(LANES)
    degenerate = (0 in counts) or (max(counts) > 2.0 * mean)
    if n >= 16 and degenerate:
        for i in range(n):
            events[i]["lane"] = lane_global[i]
        log.warning("lane 局部秩分布退化 %s（n=%d），已改用全局秩等分", counts, n)


def _energy_env(energy: list):
    """总能量包络（4 频段能量之和，10ms 一格）——HOLD 判据用。"""
    n = len(energy[0]) if energy else 0
    env = [0.0] * n
    for bi in range(len(energy)):
        e = energy[bi]
        for k in range(n):
            env[k] += e[k]
    return env


def _hold_candidates(env: list, events: list) -> list:
    """从**已检测到的音头**出发扫能量包络，生成 HOLD 候选（带实测 dur）。

    判据（卡面 ②）：
      · 起点 = 音头帧；局部峰值 E0 = 音头后 0.45s 内的包络最大值；
      · 持续段 = 从音头起、包络 ≥ **0.42×E0** 的连续帧；
      · 时长落在 **220~2500ms** ⇒ 候选，dur = 实测持续时长（到这里还只是候选，占比在
        _select_holds() 里按显著度择优控制）；
      · **新起音截断**：持续段内若包络从局部低点抬升 >0.30×E0（= 又敲了一下），
        就在该重击处截断 —— 否则一个 2.5s HOLD 会把后面的旋律全吞掉。
    """
    n = len(env)
    cands = []
    if n <= 0:
        return cands
    frames_per_s = SR / float(HOP)
    min_frames = int(math.ceil(HOLD_MIN_S * frames_per_s))
    max_frames = int(math.floor(HOLD_MAX_S * frames_per_s))
    look_frames = int(0.45 * frames_per_s)
    for idx, e in enumerate(events):
        f = int(round(float(e["t_hit"]) * frames_per_s))
        if f < 0 or f >= n:
            continue
        # 局部峰值 = 音头后 0.45s 内的能量最大值（卡面"局部峰值"口径；实测 c_light 32.1% HOLD）
        hi = min(n, f + look_frames)
        e0 = max(env[f:hi]) if hi > f else 0.0
        if e0 <= 1e-9:
            continue
        thr = HOLD_RATIO_THR * e0
        # 起音帧可能在包络峰的前 1~2 帧（能量一阶差分取峰）⇒ 向后找 3 帧内第一个过阈帧
        start = f
        if env[f] < thr:
            start = -1
            for k in range(f, min(n, f + 4)):
                if env[k] >= thr:
                    start = k
                    break
            if start < 0:
                continue
        j = start
        cap = min(n - 1, start + max_frames)
        while j + 1 < n and j + 1 <= cap and env[j + 1] >= thr:
            j += 1
            # 新起音截断：从上一帧到这一帧的抬升超过 0.30×E0 ⇒ 这是另一个音的起音
            if j > start + 2 and (env[j] - env[j - 1]) > HOLD_REATTACK_RATIO * e0:
                j -= 1
                break
        dur = (j - start + 1) * HOP / float(SR) + (start - f) * HOP / float(SR)
        if dur < HOLD_MIN_S - 1e-9:
            continue
        if dur > HOLD_MAX_S:
            dur = HOLD_MAX_S
        cands.append({"i": idx, "t": float(e["t_hit"]), "dur": round(dur, 3),
                      "e0": float(e0), "band": int(e.get("band", 0))})
    return cands


def _detect_holds(energy: list) -> list:
    """兼容旧探针：纯包络扫描（不锚定音头）——整曲每 10ms 找 ≥220ms 的过阈平台。

    正式谱面不走这条（_hold_candidates() 锚定音头、避免把伴奏长音错判成 HOLD），
    这里只保留给旧 QA 探针/单测调用。
    """
    holds = []
    n = len(energy[0]) if energy else 0
    if n <= 0:
        return holds
    env = _energy_env(energy)
    fps = SR / float(HOP)
    i = 5
    while i < n - 5:
        e0 = env[i]
        if e0 <= 1e-9:
            i += 1
            continue
        thr = HOLD_RATIO_THR * e0
        j = i
        cap = min(n - 1, i + int(HOLD_MAX_S * fps))
        while j + 1 < n and j + 1 <= cap and env[j + 1] >= thr:
            j += 1
        dur = (j - i + 1) * HOP / float(SR)
        if HOLD_MIN_S <= dur <= HOLD_MAX_S:
            holds.append({"t_hit": i * HOP / float(SR), "type": "hold", "dur": round(dur, 3)})
            i = j + 1
            continue
        i += 1
    return holds


def _select_holds(cands: list, events: list) -> None:
    """按显著度择优，把 HOLD 占比控制到 25%~40% 带内（带内目标 32%）。

    择优顺序 = dur 长 → 局部峰值 E0 大（长音/重音优先）；同轨 HOLD 区间不允许重叠
    （ribbon 是单轨面条带，重叠会互相穿透）。
    """
    n = len(events)
    if n <= 0 or not cands:
        return
    target = int(round(HOLD_TARGET * n))
    lo = int(math.ceil(HOLD_SHARE_LO * n))
    hi = int(math.floor(HOLD_SHARE_HI * n))
    if hi < lo:
        hi = lo
    target = max(lo, min(hi, target))
    target = max(0, min(n, target))
    # 关键修复（v1.0.52 返工）：HOLD 时长**不得超过同轨下一个事件**（留 90ms 间隔）。
    # 旧实现里稳态素材的候选全是 2.5s（能量不衰减），同轨重叠会把可选 HOLD 数锁死在
    # 4 轨 × 片长/2.5s（实测 music_cover.flac 25s 只能选 15 个 ⇒ HOLD 占比 18%）。
    # 夹紧后每条 HOLD 只占用到下一个同轨音符之前，候选几乎不再被重叠否掉。
    per_lane = {}
    for e in events:
        per_lane.setdefault(int(e.get("lane", 0)) % LANES, []).append(float(e["t_hit"]))
    for lst in per_lane.values():
        lst.sort()
    cands = sorted(cands, key=lambda c: (-float(c["dur"]), -float(c["e0"]), float(c["t"])))
    chosen_spans = []
    for c in cands:
        if len(chosen_spans) >= target:
            break
        idx = int(c["i"])
        t0 = float(events[idx]["t_hit"])
        lane = int(events[idx].get("lane", 0)) % LANES
        dur = float(c["dur"])
        lst = per_lane.get(lane) or []
        p = bisect.bisect_right(lst, t0)
        if p < len(lst):
            avail = float(lst[p]) - t0 - SAME_LANE_GAP_S
            # 优先夹紧到"下一个同轨音符之前"；若夹完不足 220ms，则保留原时长，
            # 让后面的同轨 TAP 按 HOLD 区间规则丢弃（否则 c_light 这类 16 分音符素材
            # 会因同轨相邻音太近而几乎选不出 HOLD，占比掉到 15.6%）。
            if avail >= HOLD_MIN_S - 1e-9:
                dur = min(dur, avail)
        if dur < HOLD_MIN_S - 1e-9:
            continue
        t1 = t0 + dur
        overlap = False
        for (a, b, ln) in chosen_spans:
            if ln == lane and t0 < b - 0.05 and t1 > a + 0.05:
                overlap = True
                break
        if overlap:
            continue
        chosen_spans.append((t0, t1, lane))
        events[idx]["type"] = "hold"
        events[idx]["dur"] = round(dur, 3)
        c["dur_clamped"] = round(dur, 3)


def _drop_taps_inside_holds(events: list) -> list:
    """同一轨上落在 HOLD 区间内 / 紧跟 HOLD 起音（<90ms）的 TAP 去掉。

    ribbon 上再叠音符会糊成一团；同时把"起音前 90ms 的 TAP"提前清掉，
    避免后面的同轨 90ms 约束把 HOLD 挤掉（HOLD 优先级更高）。
    """
    spans = {}
    for e in events:
        if e.get("type") != "hold":
            continue
        lane = int(e.get("lane", 0)) % LANES
        t0 = float(e["t_hit"])
        spans.setdefault(lane, []).append((t0, t0 + float(e.get("dur", 0.0) or 0.0)))
    if not spans:
        return events
    kept = []
    for e in events:
        if e.get("type") == "hold":
            kept.append(e)
            continue
        lane = int(e.get("lane", 0)) % LANES
        t = float(e["t_hit"])
        inside = False
        for (a, b) in spans.get(lane, ()):
            if a - SAME_LANE_GAP_S < t < b - 0.04:
                inside = True
                break
        if not inside:
            kept.append(e)
    return kept


def _active_span(env: list):
    """能量包络的活跃区间（首/尾帧）：阈值 = 全曲峰值的 2%。"""
    n = len(env)
    if n <= 0:
        return 0, 0
    mx = max(env)
    if mx <= 1e-9:
        return 0, 0
    thr = mx * 0.02
    a = 0
    while a < n and env[a] < thr:
        a += 1
    b = n - 1
    while b > a and env[b] < thr:
        b -= 1
    return a, b


def _promote_demo_holds(events: list) -> list:
    """把演示谱面里的 TAP 按目标占比升成 HOLD（时长 0.30s；不超过同轨下一个事件的间隔）。

    只在「素材没有可检测音头」的兜底路径里调用 —— 真实音乐的 HOLD 仍由能量包络判据生成。
    """
    n = len(events)
    if n <= 0:
        return events
    holds = [e for e in events if e.get("type") == "hold"]
    lo = max(1, int(math.ceil(HOLD_SHARE_LO * n)))
    hi = max(lo, int(math.floor(HOLD_SHARE_HI * n)))
    target = max(lo, min(hi, int(round(HOLD_TARGET * n))))
    need = target - len(holds)
    if need <= 0:
        return events
    per_lane = {}
    for e in events:
        per_lane.setdefault(int(e.get("lane", 0)) % LANES, []).append(float(e["t_hit"]))
    for lst in per_lane.values():
        lst.sort()
    spans = {}
    for e in holds:
        lane = int(e.get("lane", 0)) % LANES
        t = float(e["t_hit"])
        spans.setdefault(lane, []).append((t, t + float(e.get("dur", 0.0) or 0.0)))
    for e in events:
        if need <= 0:
            break
        if e.get("type") != "tap":
            continue
        lane = int(e.get("lane", 0)) % LANES
        t = float(e["t_hit"])
        bad = False
        for (a, b) in spans.get(lane, ()):
            if a - 0.05 <= t <= b + 0.05:
                bad = True
                break
        if bad:
            continue
        lst = per_lane[lane]
        i = bisect.bisect_right(lst, t)
        nxt = lst[i] if i < len(lst) else None
        avail = (nxt - t - 0.06) if nxt is not None else (DEMO_HOLD_DUR_S + 0.10)
        dur = min(DEMO_HOLD_DUR_S, avail)
        if dur < HOLD_MIN_S:
            continue
        e["type"] = "hold"
        e["dur"] = round(dur, 3)
        e["src"] = str(e.get("src", "audio")) + "_hold"
        spans.setdefault(lane, []).append((t, t + dur))
        need -= 1
    return events


def _demo_fallback(events: list, env: list, cent: list):
    """演示谱面兜底（只在**素材没有可检测音头**时触发，真实音乐永远走音头算法）。

    触发条件（两者之一）：
      · n_events == 0（纯测试音/环境长音 —— 例如 QA 的 25s 恒定测试音：没有任何起音，但
        音游模式不能是一片空白）；
      · n_events < 12 且 HOLD 占比不在 25%~40% 带内（例如 1s 静音 + 2s 长音的 hold_test.wav：
        只有 1 个长音 HOLD，占比 100% ⇒ 补几个 TAP 把占比拉回带内并让四轨都出现）。
    生成规则：活跃区里按 0.24s 网格铺 TAP（避开已有事件 60ms 内），lane 走确定性旋律型
      DEMO_PATTERN（四轨往返 ⇒ 四轨都有音符）；再按占比目标把部分 TAP 升成 0.30s HOLD。
    事件带 src="demo"/"demo_hold" 标记，rhythm_debug/stats 会如实报 demo_events。
    返回 (events, demo_n)。
    """
    n = len(events)
    holds = sum(1 for e in events if e.get("type") == "hold")
    ratio = (holds / float(n)) if n else 0.0
    if n >= DEMO_MIN_EVENTS:
        return events, 0
    if n > 0 and HOLD_SHARE_LO <= ratio <= HOLD_SHARE_HI:
        return events, 0
    a, b = _active_span(env)
    frames_per_s = SR / float(HOP)
    if (b - a) < int(0.6 * frames_per_s):
        return events, 0
    t_lo = a * HOP / float(SR) + 0.12
    t_hi = (b - int(0.10 * frames_per_s)) * HOP / float(SR)
    existing_t = sorted(float(e["t_hit"]) for e in events)
    demo = []
    idx = 0
    t = t_lo
    while t <= t_hi:
        if all(abs(t - et) >= CROSS_GAP_S for et in existing_t):
            lane = int(DEMO_PATTERN[idx % len(DEMO_PATTERN)]) % LANES
            fr = int(round(t * frames_per_s))
            c = _centroid_at(cent, fr)
            demo.append({"t_hit": round(t, 4), "lane": lane, "type": "tap", "dur": 0.0,
                         "band": 0, "centroid": round(float(c if c is not None else 800.0), 1),
                         "strength": 1, "subframe": 0.0, "src": "demo"})
            idx += 1
        t += DEMO_STEP_S
    if not demo:
        return events, 0
    merged = list(events) + demo
    merged.sort(key=lambda e: float(e["t_hit"]))
    merged = _enforce_lane_gaps(merged)
    merged = _promote_demo_holds(merged)
    return merged, len(demo)


def _enforce_lane_gaps(events: list) -> list:
    """同轨 ≥90ms 约束：冲突时优先换到最近的空闲轨（保持音高序的邻域），否则丢弃。

    同时保证事件不落在同轨 HOLD 的区间内（视觉上音符不能压在面条带上）。
    """
    last_t = {}
    hold_spans = {}
    out = []
    for e in events:
        t = float(e["t_hit"])
        lane = int(e.get("lane", 0)) % LANES
        e = dict(e)

        def _free(ln: int) -> bool:
            lt = last_t.get(ln)
            if lt is not None and (t - lt) < SAME_LANE_GAP_S - 1e-9:
                return False
            for (a, b) in hold_spans.get(ln, ()):
                if a - 0.05 <= t <= b + 0.05:
                    return False
            return True

        if _free(lane):
            out.append(e)
            last_t[lane] = t
            if e.get("type") == "hold":
                hold_spans.setdefault(lane, []).append((t, t + float(e.get("dur", 0.0) or 0.0)))
            continue
        moved = False
        for ln in sorted(range(LANES), key=lambda z: (abs(z - lane), z)):
            if ln == lane:
                continue
            if _free(ln):
                e["lane"] = int(ln)
                out.append(e)
                last_t[ln] = t
                if e.get("type") == "hold":
                    hold_spans.setdefault(ln, []).append(
                        (t, t + float(e.get("dur", 0.0) or 0.0)))
                moved = True
                break
        if not moved:
            log.debug("lane 排不下，丢弃事件 t=%.3f lane=%d type=%s", t, lane,
                      str(e.get("type")))
    return out


def _cap_hold_share(events: list) -> list:
    """占比超过 40% 时把最短的 HOLD 降级成 TAP（时间不变 ⇒ 不破坏已满足的间隔约束）。

    ★ 短素材（n<12，如 1s 静音 + 2s 长音的 hold_test.wav）不做占比裁剪 ——
      单个长音素材的自然结果就是 1 个 HOLD，硬压成 TAP 反而把"HOLD 能生成"这一条打掉。
    """
    n = len(events)
    if n <= 0 or n < MIN_SHARE_N:
        return events
    holds = [e for e in events if e.get("type") == "hold"]
    share = len(holds) / float(n)
    if share <= HOLD_SHARE_HI:
        return events
    holds.sort(key=lambda e: float(e.get("dur", 0.0) or 0.0))
    # 上限留 0.4% 余量：卡面写"25%~40%"，若恰好 40.0% 会被严格口径（<40%）判出界。
    over = len(holds) - int(math.floor(HOLD_SHARE_HI * n - 1e-9))
    for e in holds[:max(0, over)]:
        e["type"] = "tap"
        e["dur"] = 0.0
    return events


def _min_same_lane_gap_ms(events: list):
    gaps = []
    per_lane = {}
    for e in sorted(events, key=lambda z: float(z["t_hit"])):
        lane = int(e.get("lane", 0)) % LANES
        t = float(e["t_hit"])
        prev = per_lane.get(lane)
        if prev is not None:
            gaps.append((t - prev) * 1000.0)
        per_lane[lane] = t
    return (round(min(gaps), 2) if gaps else None)


def _min_cross_lane_gap_ms(events: list):
    """相邻两个事件（不同轨）的最小时间差；同轨间隔不计入这里。"""
    ev = sorted(events, key=lambda z: float(z["t_hit"]))
    gaps = []
    for i in range(1, len(ev)):
        if int(ev[i].get("lane", 0)) % LANES == int(ev[i - 1].get("lane", 0)) % LANES:
            continue
        gaps.append((float(ev[i]["t_hit"]) - float(ev[i - 1]["t_hit"])) * 1000.0)
    return (round(min(gaps), 2) if gaps else None)


def _enforce_cross_gap(peaks: list) -> list:
    """收口：把亚帧插值带来的 ±5ms 抖动抹掉，保证相邻音头**严格 ≥60ms**。

    _merge_onsets() 用未插值的峰时间做 60ms 判定，插值后可能变成 59.8ms；
    验收读的是事件表里的 t_hit，所以这里再按**最终时间**过一遍（只丢后一个）。
    """
    out = []
    for p in peaks:
        t = float(p["t_hit"])
        if not out or (t - float(out[-1]["t_hit"])) >= CROSS_GAP_S - 1e-9:
            out.append(p)
        else:
            log.debug("音头间隔 %.1fms <60ms，丢弃 t=%.3f", (t - float(out[-1]["t_hit"])) * 1000.0, t)
    return out


def build_from_pcm(x) -> list:
    """谱面主流水线（纯函数，便于探针注入合成 PCM）：PCM ⇒ 事件表。"""
    flux, energy, cent = _analyze(x)
    peaks = _adaptive_peaks(flux)
    onsets = _enforce_cross_gap(_merge_onsets(peaks))
    env = _energy_env(energy)
    frames_per_s = SR / float(HOP)
    events = []
    for p in onsets:
        f = int(round(float(p["t_hit"]) * frames_per_s))
        c = _centroid_at(cent, f)
        if c is None:
            continue
        events.append({"t_hit": round(float(p["t_hit"]), 4), "lane": 0,
                       "type": "tap", "dur": 0.0, "band": int(p["band"]),
                       "centroid": round(float(c), 1), "strength": 1,
                       "subframe": float(p.get("subframe", 0.0)), "src": "audio"})
    if events:
        _assign_lanes(events)
        cands = _hold_candidates(env, events)
        _select_holds(cands, events)
        events = _drop_taps_inside_holds(events)
        events = _enforce_lane_gaps(events)
    # ★ 演示谱面兜底：只对"没有可检测音头"的素材补事件（真实音乐不触发，见 _demo_fallback）
    events, _demo_n = _demo_fallback(events, env, cent)
    events = _cap_hold_share(events)
    events.sort(key=lambda e: (float(e["t_hit"]), int(e.get("lane", 0))))
    return events


def build_beatmap(path: str) -> list:
    """同步版本（探针/单测用）：解码 ⇒ 音头 ⇒ 质心 lane ⇒ HOLD ⇒ 间隔约束。"""
    x = _read_pcm(path)
    return build_from_pcm(x)


class BeatmapWorker(QThread):
    """后台线程：解 PCM ⇒ 音高/长按事件表（不阻塞 UI）。"""

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
            ev = build_from_pcm(x)
            self.progress.emit(100)
            self.done.emit(ev)
        except Exception as exc:  # noqa: BLE001
            log.warning("beatmap 失败: %s", exc, exc_info=True)
            self.failed.emit(str(exc))


def stats(events: list) -> dict:
    """谱面统计（rhythm_debug() 锚点用）。"""
    ev = sorted(list(events or []), key=lambda e: float(e.get("t_hit", 0.0)))
    n = len(ev)
    lane_counts = [0] * LANES
    for e in ev:
        try:
            lane_counts[int(e.get("lane", 0)) % LANES] += 1
        except Exception:
            lane_counts[0] += 1
    taps = sum(1 for e in ev if e.get("type") == "tap")
    holds = [e for e in ev if e.get("type") == "hold"]
    subs = [e.get("subframe") for e in ev if isinstance(e.get("subframe"), (int, float))]
    cents = [float(e.get("centroid")) for e in ev
             if isinstance(e.get("centroid"), (int, float))]
    durs = [float(e.get("dur", 0.0) or 0.0) for e in holds]
    demo_n = sum(1 for e in ev if str(e.get("src", "")).startswith("demo"))
    return {"n_events": n, "taps": taps, "doubles": 0, "holds": len(holds),
            "demo_events": demo_n,
            "chart_source": ("demo_fallback" if demo_n else "onsets"),
            "hold_ratio": (round(len(holds) / float(n), 4) if n else 0.0),
            "lane_counts": lane_counts,
            "same_lane_min_gap_ms": _min_same_lane_gap_ms(ev),
            "cross_lane_min_gap_ms": _min_cross_lane_gap_ms(ev),
            "centroid_mean": (round(sum(cents) / len(cents), 1) if cents else None),
            "centroid_min": (round(min(cents), 1) if cents else None),
            "centroid_max": (round(max(cents), 1) if cents else None),
            "hold_dur_mean": (round(sum(durs) / len(durs), 3) if durs else None),
            "hold_dur_max": (round(max(durs), 3) if durs else None),
            "subframe_n": len(subs),
            "subframe_mean": (round(sum(subs) / len(subs), 4) if subs else None),
            "first_t_hits": [round(float(e["t_hit"]), 3) for e in ev[:8]],
            "last_t_hit": round(max([float(e["t_hit"]) for e in ev], default=0.0), 3),
            "bands": list(BAND_NAMES)}


def _bump_spawn() -> None:
    global CMD_SPAWN_COUNT
    CMD_SPAWN_COUNT += 1
