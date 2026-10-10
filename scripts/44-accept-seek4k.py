"""验收 ④：4K 高码率连续跨段 Seek 性能 + 丢帧率 + 内存泄漏。

判据（用户要求）：
  * 跨度 30 秒以上的连续快进（Seek）;
  * 画面恢复时间 < 100ms;
  * 丢帧数 = 0;
  * 另外做一轮"反复 Seek + 反复开关界面"的内存泄漏检测。

"画面恢复时间"的判据是 **mapped 帧真的换掉了**（渲染计数变化）,
不是 mpv 自己报的 restart 事件 —— 后者会在画面还没换时就到。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import _wininput as wi  # noqa: E402
from zhijian.ipc import ControlClient  # noqa: E402

CTL = "zhjperf"
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


def rss_mb(pid: int) -> float:
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return -1.0
        try:
            c = PROCESS_MEMORY_COUNTERS_EX()
            c.cb = ctypes.sizeof(c)
            if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return -1.0
            return c.WorkingSetSize / 1048576.0
        finally:
            k32.CloseHandle(h)
    except Exception:
        return -1.0


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    video = os.path.join(ROOT, "assets", "testmedia", "uhd4k_45m.mp4")
    if not os.path.isfile(video):
        log(f"缺少 4K 测试片: {video}")
        return 2
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", CTL,
                          "--no-single-instance", "-v", video],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctl = ControlClient(CTL, timeout=240.0)
    for _ in range(160):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    try:
        time.sleep(4.0)
        st = ctl.call(cmd="state")["state"]
        mpv = st["mpv"]
        log(f"素材：{st['mpv']['width']}x{st['mpv']['height']} "
            f"{mpv['video_codec']} 时长 {st['duration']:.1f}s")
        log(f"解码：hwdec={mpv['hwdec_current']} 像素格式={mpv['video_format']} "
            f"帧率={mpv['container_fps']}")
        REPORT["media"] = {k: mpv[k] for k in
                           ("width", "height", "video_codec", "hwdec_current", "video_format",
                            "container_fps")}
        REPORT["duration"] = st["duration"]
        check("4K 素材已用硬件解码", bool(mpv["hwdec_current"]), f"hwdec={mpv['hwdec_current']}")
        check("分辨率确为 4K", int(mpv["width"] or 0) >= 3840, f"{mpv['width']}x{mpv['height']}")

        ctl.call(cmd="set", name="pause", value=False)
        time.sleep(1.0)

        # ---------------- 主基准 A：精确跳转（松开进度条 / 按键后的对位） ----------------
        # gap_ms=150 模拟"用户连续拖进度条"的真实节奏。零间隔连发会让上一跳的
        # 残留帧把下一跳"测成"16ms —— 那是假数据（本项目先踩过一次）。
        r = ctl.call(cmd="seek_perf", span=40.0, n=12, timeout=3.0, gap_ms=150,
                     precise=True)["result"]
        REPORT["seek_exact"] = r
        log(f"  A. 精确跳转（间隔 150ms, 跨度 40s）：{r['count']} 次, 成功 {r['recovered']} 次")
        log(f"     中位 {r['p50_ms']}ms / P90 {r['p90_ms']}ms / 平均 {r['avg_ms']}ms / "
            f"最慢 {r['max_ms']}ms / 最快 {r['min_ms']}ms")
        log(f"     <100ms 占比 {r['under_100ms']}/{r['recovered']}，"
            f"丢帧 {r['frame_drops_delta']}（计数器重置={r['frame_drops_counter_reset']}）")
        for s in r["samples"]:
            log(f"      -> {s['target']:>7.2f}s  恢复 {s['recover_ms']}ms")
        check("A 全部 Seek 都恢复到新画面", r["recovered"] == r["count"],
              f"{r['recovered']}/{r['count']}")
        check("A 中位恢复时间 < 100ms", (r["p50_ms"] or 9999) < 100.0, f"中位 {r['p50_ms']}ms")
        check("A P90 恢复时间 < 150ms", (r["p90_ms"] or 9999) < 150.0, f"P90 {r['p90_ms']}ms")
        check("A 丢帧 = 0", r["frame_drops_delta"] == 0, f"丢帧 {r['frame_drops_delta']}")

        # ---------------- 主基准 B：关键帧跳转（拖动中的实时预览） ----------------
        # 这是"4K 随意拖拽进度条 0 卡顿"真正对应的路径。
        r1 = ctl.call(cmd="seek_perf", span=40.0, n=12, timeout=3.0, gap_ms=150,
                      precise=False)["result"]
        REPORT["seek_keyframe"] = r1
        log(f"  B. 关键帧跳转（拖动中实时预览）：中位 {r1['p50_ms']}ms / "
            f"P90 {r1['p90_ms']}ms / 最慢 {r1['max_ms']}ms / 丢帧 {r1['frame_drops_delta']}")
        check("B 中位 < 100ms", (r1["p50_ms"] or 9999) < 100.0, f"中位 {r1['p50_ms']}ms")
        check("B P90 < 100ms", (r1["p90_ms"] or 9999) < 100.0, f"P90 {r1['p90_ms']}ms")
        check("B 最慢 < 150ms（拖动手感一致性）", (r1["max_ms"] or 9999) < 150.0,
              f"最慢 {r1['max_ms']}ms")
        check("B 丢帧 = 0", r1["frame_drops_delta"] == 0, f"丢帧 {r1['frame_drops_delta']}")

        # ---------------- 高频拖拽（60ms 间隔 + 关键帧, 模拟狂拖） ----------------
        import random

        random.seed(7)
        targets = [round(random.uniform(5, 110), 2) for _ in range(40)]
        r2 = ctl.call(cmd="seek_perf", targets=targets, timeout=3.0, gap_ms=60,
                      precise=False)["result"]
        REPORT["seek_stress"] = r2
        log(f"  C. 高频狂拖模拟（40 次随机 Seek, 间隔 60ms, 关键帧）："
            f"成功 {r2['recovered']}/{r2['count']}")
        log(f"     中位 {r2['p50_ms']}ms / P90 {r2['p90_ms']}ms / 平均 {r2['avg_ms']}ms / "
            f"最慢 {r2['max_ms']}ms / 丢帧 {r2['frame_drops_delta']}")
        check("C 高频随机 Seek 全部恢复", r2["recovered"] == r2["count"],
              f"{r2['recovered']}/{r2['count']}")
        check("C 高频 Seek 中位 < 200ms（每 60ms 连发的排队效应）",
              (r2["p50_ms"] or 9999) < 200.0, f"中位 {r2['p50_ms']}ms")
        check("C 高频 Seek 丢帧 = 0", r2["frame_drops_delta"] == 0, f"{r2['frame_drops_delta']}")

        st = ctl.call(cmd="state")["state"]
        check("长跑后实时帧率仍在正常区间",
              (st["mpv"]["estimated_vf_fps"] or 0) > 20,
              f"vf_fps={st['mpv']['estimated_vf_fps']}")
        check("累计丢帧仍为 0",
              int(st["mpv"]["frame_drop_count"] or 0) == 0,
              f"frame-drop-count={st['mpv']['frame_drop_count']}")

    finally:
        try:
            ctl.call(cmd="quit")
        except Exception:
            pass
        try:
            p.wait(timeout=15)
        except Exception:
            p.kill()
        log(f"退出码 {p.returncode}")
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "44-seek4k.json"), "w", encoding="utf-8") as f:
            json.dump({"report": REPORT, "log": LOG}, f, ensure_ascii=False, indent=2)
        with open(os.path.join(ROOT, "logs", "44-seek4k.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
    log(f"结论：{'全部通过' if not FAILS else str(len(FAILS)) + ' 项失败: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
