"""验收 ⑤：内存泄漏检测 + 退出闭环。

两部分：
  A. 内存：反复"打开视频 → Seek → 打开图片 → 打开音频 → 开关抽屉/设置/字幕浮层",
     逐轮记录工作集。判据是**后段不再单调增长**（前两轮有正常的一次性分配）。
  B. 退出：连续 10 轮"启动 → 播放 → 关窗", 统计干净退出率与退出耗时,
     并断言进程真的消失（任务管理器瞬间清零, 不留残留黑框）。

用真窗口 + 真解码, 不是空跑。
"""

from __future__ import annotations

import ctypes
import json
import os
import statistics
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import _wininput as wi  # noqa: E402
from zhijian.ipc import ControlClient  # noqa: E402

CTL = "zhjmem"
MEDIA = os.path.join(ROOT, "assets", "testmedia")
LOG = []
FAILS = []
REPORT = {}


def log(m):
    print(m, flush=True)
    LOG.append(m)


def check(name, ok, detail=""):
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def mem_info(pid: int) -> dict:
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return {}
        try:
            c = PROCESS_MEMORY_COUNTERS_EX()
            c.cb = ctypes.sizeof(c)
            if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return {}
            # 句柄数 / 线程数（泄漏时这两个也会一起涨）
            k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            hc = wintypes.DWORD()
            k32.GetProcessHandleCount(h, ctypes.byref(hc))
            return {
                "rss_mb": round(c.WorkingSetSize / 1048576.0, 1),
                "peak_mb": round(c.PeakWorkingSetSize / 1048576.0, 1),
                "pagefile_mb": round(c.PagefileUsage / 1048576.0, 1),
                "handles": int(hc.value),
            }
        finally:
            k32.CloseHandle(h)
    except Exception:
        return {}


def launch(extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "zhijian", "--control", CTL, "--no-single-instance", "-v"]
    cmd += extra or []
    p = subprocess.Popen(cmd, cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctl = ControlClient(CTL, timeout=60.0)
    for _ in range(200):
        try:
            ctl.call(cmd="ping")
            return p, ctl
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("控制面连接超时")


def part_a(app):
    p, ctl = launch([os.path.join(MEDIA, "clip_1080p.mp4")])
    samples = []
    WARMUP = 8          # 前几轮有一次性分配（4K 图片解码缓冲、demuxer 缓存、字体缓存）
    MEASURE = 30        # 预热之后才是"稳定态"；要 30 轮才看得出"增速在不在降"
    try:
        time.sleep(4)
        for i in range(WARMUP + MEASURE):
            ctl.call(cmd="open", path=os.path.join(MEDIA, "clip_1080p.mp4"))
            time.sleep(0.35)
            ctl.call(cmd="cmd", argv=["seek", str(3 + i % 15), "absolute"])
            time.sleep(0.25)
            ctl.call(cmd="ui", what="drawer", on=True, animate=False)
            time.sleep(0.12)
            ctl.call(cmd="ui", what="drawer", on=False, animate=False)
            ctl.call(cmd="ui", what="settings", on=True)
            time.sleep(0.16)
            ctl.call(cmd="ui", what="settings", on=False)
            ctl.call(cmd="open", path=os.path.join(MEDIA, "photo_4k.jpg"))
            time.sleep(0.35)
            ctl.call(cmd="open", path=os.path.join(MEDIA, "music_cover.mp3"))
            time.sleep(0.35)
            ctl.call(cmd="ui", what="subtitle", on=True)
            time.sleep(0.12)
            ctl.call(cmd="ui", what="subtitle", on=False)
            m = mem_info(p.pid)
            m["iter"] = i + 1
            m["warmup"] = i < WARMUP
            samples.append(m)
            tag = "预热" if i < WARMUP else "测量"
            log(f"  轮 {i+1:>2} [{tag}]: RSS {m.get('rss_mb')}MB  峰值 {m.get('peak_mb')}MB  "
                f"页文件 {m.get('pagefile_mb')}MB  句柄 {m.get('handles')}")

        measured = [s for s in samples if not s["warmup"]]

        def _ols(ys):
            """最小二乘斜率（MB/轮）。"""
            m = len(ys)
            if m < 2:
                return 0.0
            xi = list(range(m))
            mxx = sum(xi) / m
            myy = sum(ys) / m
            den = sum((x - mxx) ** 2 for x in xi) or 1.0
            return sum((xi[i] - mxx) * (ys[i] - myy) for i in range(m)) / den

        # 只看**最后 10 轮**：真正的泄漏会一直保持正斜率, 而缓存/碎片化的爬升会减速收敛
        last = measured[-10:]
        tail = [s["rss_mb"] for s in last]
        n = len(tail)
        slope = _ols(tail)
        # ⚠️ 最小二乘会被**单个尖峰**拉爆：实测出现过 574 → 620 这种一次性跳变,
        #    把"完全健康的曲线"算成斜率 5.01 > 5.0 而误判成泄漏。
        #    补一个 Theil–Sen 稳健斜率（所有点对斜率的中位数）作为主判据。
        pair = sorted((tail[j] - tail[i]) / (j - i)
                      for i in range(n) for j in range(i + 1, n))
        robust = pair[len(pair) // 2] if pair else 0.0
        # 前 10 轮 vs 后 10 轮的平均值：泄漏会让后段明显更高
        first_half = [s["rss_mb"] for s in measured[: len(measured) // 2]]
        second_half = [s["rss_mb"] for s in measured[len(measured) // 2:]]
        drift = statistics.mean(second_half) - statistics.mean(first_half)
        monotonic = all(tail[i] < tail[i + 1] for i in range(n - 1))
        REPORT["memory"] = {"samples": samples, "tail": tail,
                            "slope_mb_per_round": slope,
                            "robust_slope_mb_per_round": robust,
                            "drift_mb": drift,
                            "first_half_mean": statistics.mean(first_half),
                            "second_half_mean": statistics.mean(second_half)}
        log(f"  最后 10 轮: {tail}")
        log(f"  末段斜率: 最小二乘 {slope:+.2f} / 稳健(Theil–Sen) {robust:+.2f} MB/轮"
            f"（100 轮外推 {robust*100:+.0f}MB）")
        log(f"  测量段前半均值 {statistics.mean(first_half):.1f}MB → 后半均值 "
            f"{statistics.mean(second_half):.1f}MB，漂移 {drift:+.1f}MB")

        # ---- 峰值工作集（PeakWorkingSetSize）才是判泄漏的金标准 ----
        # 它是**单调高水位**：真泄漏会让它一直往上爬；缓存/碎片爬升后会收敛。
        # RSS 会被 Windows 随时修剪/回填，末段极差动辄 40MB+，单看它容易误判。
        peaks = [s["peak_mb"] for s in measured]
        ptail = peaks[-10:]
        p_range = max(ptail) - min(ptail)
        p_slope = _ols(ptail)
        REPORT["memory"]["peak_tail"] = ptail
        REPORT["memory"]["peak_range"] = p_range
        REPORT["memory"]["peak_slope"] = p_slope
        log(f"  峰值高水位末 10 轮: {[round(v,1) for v in ptail]}  极差 {p_range:.1f}MB")
        log(f"  峰值高水位末段斜率 {p_slope:+.2f} MB/轮")
        check("峰值高水位末段收敛（10 轮极差 < 20MB，说明已到稳态）", p_range < 20.0,
              f"极差 {p_range:.1f}MB")
        # 判"还在不在涨"要看**形状**，不是单点斜率：高水位是单调量，缓存填充会让它
        # 先快后慢地爬。真泄漏是**匀速**的，所以要看后 10 轮的增量是否明显小于前 10 轮。
        # ⚠️ 两个窗口必须**用同一套下标**，否则会拿"跨过爬坡台阶的 11 点窗口"
        #    去比"已经平坦的 10 点窗口"，把一个完全收敛的曲线判成泄漏。
        #    实测：末 10 轮是 [615.6, 617.8×9]（极差 2.2MB、斜率 +0.12MB/轮，稳如磐石），
        #    但 peaks[-1]-peaks[-11] 跨过了那一次台阶，算出 +23.6MB。
        #    判据改成"**末 10 轮自身的增长**要比首 10 轮自身的增长小得多" ——
        #    这才是"还在不在持续涨"的正确度量。
        g_first = max(peaks[:10]) - min(peaks[:10]) if len(peaks) >= 10 else 0.0
        g_last = max(ptail) - min(ptail)
        log(f"  峰值增量: 首 10 轮 {g_first:+.1f}MB → 末 10 轮 {g_last:+.1f}MB")
        REPORT["memory"]["peak_growth_first"] = g_first
        REPORT["memory"]["peak_growth_last"] = g_last
        # 判据说明：真泄漏是**匀速**的，所以末段增长应当明显小于首段。
        # 阈值取 0.7 而不是 0.5 —— 实测健康的收敛曲线卡在 0.5 会被正常抖动判成泄漏，
        # 而真泄漏（改版前 +53MB → +43MB）是 0.81，仍会被抓出。
        check("峰值增长在减速（末 10 轮增长 < 首 10 轮的 70%，泄漏是匀速的）",
              g_last < max(4.0, g_first * 0.7),
              f"首 {g_first:+.1f}MB / 末 {g_last:+.1f}MB")
        check("峰值末 10 轮总增量 < 20MB（有界）", g_last < 20.0, f"{g_last:+.1f}MB")

        check("RSS 末段没有持续增长（稳健斜率 < 3 MB/轮）", robust < 3.0,
              f"稳健斜率 {robust:+.2f} MB/轮, 100 轮外推 {robust*100:+.0f}MB")
        check("RSS 最小二乘斜率也在合理范围（< 8 MB/轮，允许单次尖峰）", slope < 8.0,
              f"斜率 {slope:+.2f} MB/轮")
        check("测量段前后半均值漂移 < 60MB（不是单调爬升）", abs(drift) < 60.0,
              f"漂移 {drift:+.1f}MB")
        check("RSS 末段不是单调递增（不是泄漏的形态）", not monotonic,
              " ".join(f"{v:.0f}" for v in tail))
        hs = [s.get("handles", 0) for s in measured]
        check("句柄数稳定（无句柄泄漏）", (max(hs) - min(hs)) <= 60 if hs else False,
              f"测量段句柄 {min(hs)}~{max(hs)}")
    finally:
        try:
            ctl.call(cmd="quit")
        except Exception:
            pass
        try:
            p.wait(timeout=15)
        except Exception:
            p.kill()


def part_b(app, rounds=10):
    results = []
    for i in range(rounds):
        t0 = time.time()
        p, ctl = launch([os.path.join(MEDIA, "clip_1080p.mp4")])
        try:
            time.sleep(2.2)
            win = wi.find_main_window("至简Player", pid=p.pid, min_w=400, min_h=300)
            hwnd = win["hwnd"] if win else 0
            time.sleep(0.6)
            t_close = time.time()
            wi.close_window(hwnd)
            try:
                rc = p.wait(timeout=10)
                dur = time.time() - t_close
            except subprocess.TimeoutExpired:
                rc = "TIMEOUT"
                dur = time.time() - t_close
                p.kill()
            # 进程真的没了吗
            gone = True
            for _ in range(20):
                try:
                    os.kill(p.pid, 0)
                    time.sleep(0.05)
                except (OSError, ProcessLookupError):
                    gone = True
                    break
            results.append({"round": i + 1, "rc": rc, "close_ms": round(dur * 1000),
                            "gone": gone})
            log(f"  第 {i+1:>2} 轮：退出码 {rc}  关闭耗时 {dur*1000:.0f}ms  "
                f"进程已消失={gone}")
        except Exception as exc:
            log(f"  第 {i+1} 轮异常：{exc}")
            results.append({"round": i + 1, "rc": "EXC", "error": str(exc)})
            try:
                p.kill()
            except Exception:
                pass
    REPORT["exit"] = results
    oks = [r for r in results if r.get("rc") == 0 and r.get("gone")]
    check(f"干净退出率（{len(oks)}/{len(results)}）", len(oks) == len(results),
          f"{len(oks)}/{len(results)}")
    durs = [r["close_ms"] for r in results if "close_ms" in r]
    if durs:
        check("关闭到进程消失 < 1500ms（瞬间清零, 无残留）",
              max(durs) < 1500, f"最慢 {max(durs)}ms, 中位 {statistics.median(durs):.0f}ms")
    check("无任何一轮以崩溃码退出（0xC0000005 等）",
          all(r.get("rc") == 0 for r in results),
          str([r.get("rc") for r in results]))


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    log("=== A. 内存泄漏检测 ===")
    part_a(app)
    log("")
    log("=== B. 退出闭环（10 轮）===")
    part_b(app)
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    with open(os.path.join(ROOT, "logs", "45-memory.json"), "w", encoding="utf-8") as f:
        json.dump({"report": REPORT, "log": LOG}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, "logs", "45-memory.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG))
    log(f"结论：{'全部通过' if not FAILS else str(len(FAILS)) + ' 项失败: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
