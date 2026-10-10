r"""内存泄漏二分：把混合负载拆开，逐项跑 20 轮，看谁在涨。

每项都用一个**全新的进程**，避免互相污染。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
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
VIDEO = os.path.join(MEDIA, "clip_1080p.mp4")
IMAGE = os.path.join(MEDIA, "photo_4k.jpg")
AUDIO = os.path.join(MEDIA, "music_cover.mp3")


class PMC(ctypes.Structure):
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
        return 0.0
    try:
        c = PMC()
        c.cb = ctypes.sizeof(c)
        psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
        return round(c.WorkingSetSize / 1048576.0, 1)
    finally:
        k32.CloseHandle(h)


def one_case(name, fn, rounds=20, warm=6):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    ctl_name = "zhjbis"
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", ctl_name,
                          "--no-single-instance"],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    ctl = ControlClient(ctl_name, timeout=60.0)
    for _ in range(200):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(4)
    rss = []
    try:
        for i in range(rounds):
            fn(ctl, i)
            rss.append(mem(p.pid))
    finally:
        try:
            ctl.call(cmd="quit")
        except Exception:
            pass
        time.sleep(1.5)
        try:
            p.terminate()
        except Exception:
            pass
    tail = rss[warm:]
    grow = (tail[-1] - tail[0]) / max(1, len(tail) - 1) if len(tail) > 1 else 0
    print(f"  {name:<26} 起 {rss[0]:>6} → 末 {rss[-1]:>6} MB   "
          f"预热后每轮 {grow:+.2f} MB   {'⚠️ 涨' if grow > 0.5 else 'OK'}", flush=True)
    return {"name": name, "rss": rss, "per_round": round(grow, 3)}


def main():
    app = QApplication(sys.argv[:1])
    cases = [
        ("视频↔图片", lambda c, i: (c.call(cmd="open", path=VIDEO), time.sleep(0.35),
                                   c.call(cmd="open", path=IMAGE), time.sleep(0.35))),
        ("视频↔音频", lambda c, i: (c.call(cmd="open", path=VIDEO), time.sleep(0.35),
                                   c.call(cmd="open", path=AUDIO), time.sleep(0.35))),
        ("图片↔音频", lambda c, i: (c.call(cmd="open", path=IMAGE), time.sleep(0.35),
                                   c.call(cmd="open", path=AUDIO), time.sleep(0.35))),
        ("视频↔图片↔音频 轮换", lambda c, i: (c.call(cmd="open", path=VIDEO), time.sleep(0.35),
                                              c.call(cmd="open", path=IMAGE), time.sleep(0.35),
                                              c.call(cmd="open", path=AUDIO), time.sleep(0.35))),
    ]
    out = []
    for name, fn in cases:
        try:
            out.append(one_case(name, fn))
        except Exception as exc:
            print(f"  {name:<26} 失败: {exc}")
    with open(os.path.join(ROOT, "logs", "memory-bisect.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
