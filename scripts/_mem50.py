r"""内存长跑探针：40 轮混合负载，看 RSS 到底是"泄漏"还是"爬升后收敛"。

判据不是斜率阈值，而是**形状**：真泄漏会一路单调爬升；缓存/堆碎片会爬升后进入平台区。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import statistics
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from zhijian.ipc import ControlClient  # noqa: E402

MEDIA = os.path.join(ROOT, "assets", "testmedia")
CTL = "zhjmem40"


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def mem(pid):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return {}
    try:
        c = PROCESS_MEMORY_COUNTERS_EX()
        c.cb = ctypes.sizeof(c)
        psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
        k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        hc = wintypes.DWORD()
        k32.GetProcessHandleCount(h, ctypes.byref(hc))
        return {"rss": round(c.WorkingSetSize / 1048576.0, 1),
                "peak": round(c.PeakWorkingSetSize / 1048576.0, 1),
                "handles": int(hc.value)}
    finally:
        k32.CloseHandle(h)


def main():
    app = QApplication(sys.argv[:1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", CTL,
                          "--no-single-instance", os.path.join(MEDIA, "clip_1080p.mp4")],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    ctl = ControlClient(CTL, timeout=60.0)
    for _ in range(200):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(4)

    series = []
    ROUNDS = 50
    try:
        for i in range(ROUNDS):
            ctl.call(cmd="open", path=os.path.join(MEDIA, "clip_1080p.mp4"))
            time.sleep(0.3)
            ctl.call(cmd="cmd", argv=["seek", str(3 + i % 15), "absolute"])
            time.sleep(0.2)
            ctl.call(cmd="ui", what="drawer", on=True, animate=False)
            time.sleep(0.1)
            ctl.call(cmd="ui", what="drawer", on=False, animate=False)
            ctl.call(cmd="ui", what="settings", on=True)
            time.sleep(0.15)
            ctl.call(cmd="ui", what="settings", on=False)
            ctl.call(cmd="open", path=os.path.join(MEDIA, "photo_4k.jpg"))
            time.sleep(0.3)
            ctl.call(cmd="open", path=os.path.join(MEDIA, "music_cover.mp3"))
            time.sleep(0.3)
            ctl.call(cmd="ui", what="subtitle", on=True)
            time.sleep(0.1)
            ctl.call(cmd="ui", what="subtitle", on=False)
            m = mem(p.pid)
            m["i"] = i + 1
            series.append(m)
            print(f"  轮 {i+1:>2}: RSS {m['rss']:>6} MB  峰值 {m['peak']:>6} MB  句柄 {m['handles']}",
                  flush=True)
    finally:
        try:
            ctl.call(cmd="quit")
        except Exception:
            pass
        time.sleep(2)
        try:
            p.terminate()
        except Exception:
            pass

    rss = [s["rss"] for s in series]
    n = len(rss)
    thirds = [rss[: n // 3], rss[n // 3: 2 * n // 3], rss[2 * n // 3:]]
    print("\n  三段均值: " + " | ".join(f"{statistics.mean(t):.1f}" for t in thirds))
    print(f"  末段 10 轮: {rss[-10:]}")
    print(f"  全程极差 {max(rss) - min(rss):.1f} MB, 末段极差 {max(rss[-10:]) - min(rss[-10:]):.1f} MB")
    with open(os.path.join(ROOT, "logs", "memory-40rounds.json"), "w", encoding="utf-8") as f:
        json.dump({"series": series, "thirds": [statistics.mean(t) for t in thirds]},
                  f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
