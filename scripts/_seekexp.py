"""对照实验：hr-seek / 缓存策略对 4K 跨段 Seek 恢复时间的影响。

同一台机器、同一素材、同一目标序列, **交错重复 3 轮**取中位数 ——
单次前后对比在这台机器上会得出错误结论（内存/磁盘状态会漂移）。
"""

from __future__ import annotations

import json
import os
import statistics
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

CTL = "zhjseek"
VARIANTS = [
    ("① cache=yes（流缓存 + demuxer 缓存）", {"cache": "yes", "precise": False}),
    ("② cache=no（只留 demuxer 缓存）", {"cache": "no", "precise": False}),
    ("③ cache=no + 精确跳转", {"cache": "no", "precise": True}),
    ("④ cache=yes + 800ms 间隔（看地板值）", {"cache": "yes", "precise": False, "gap_ms": 800}),
]
TARGETS = [round(2.0 + i * 3.64, 2) for i in range(12)]
GAP_MS = 150.0


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    video = os.path.join(ROOT, "assets", "testmedia", "uhd4k_45m.mp4")
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", CTL,
                          "--no-single-instance", video],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctl = ControlClient(CTL, timeout=240.0)
    for _ in range(160):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    out = {}
    try:
        time.sleep(4)
        ctl.call(cmd="set", name="pause", value=False)
        time.sleep(0.8)
        rounds = {name: [] for name, _ in VARIANTS}
        for rnd in range(3):
            for name, opts in VARIANTS:
                o = dict(opts)
                gap = o.pop("gap_ms", GAP_MS)
                if "cache" in o:
                    ctl.call(cmd="set", name="cache", value=o.pop("cache"))
                    time.sleep(0.4)
                r = ctl.call(cmd="seek_perf", targets=TARGETS, timeout=3.0,
                             gap_ms=gap, **o)["result"]
                vals = [s["recover_ms"] for s in r["samples"] if s["recover_ms"] is not None]
                rounds[name].append(vals)
                print(f"  轮{rnd+1} {name:<34} 中位 {statistics.median(vals):7.1f}ms  "
                      f"最大 {max(vals):7.1f}ms  丢帧 {r['frame_drops_delta']}", flush=True)
        print("\n=== 三轮汇总（间隔 150ms，模拟连续拖进度条）===")
        for name, _ in VARIANTS:
            allv = [v for r in rounds[name] for v in r]
            med = statistics.median(allv)
            p90 = sorted(allv)[int(len(allv) * 0.9)]
            out[name] = {"median": med, "max": max(allv), "p90": p90, "all": allv}
            print(f"  {name:<28} 中位 {med:7.1f}ms  P90 {p90:7.1f}ms  最大 {max(allv):7.1f}ms")
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "seek-ab.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    finally:
        try:
            ctl.call(cmd="quit")
        except Exception:
            pass
        try:
            p.wait(timeout=15)
        except Exception:
            p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
