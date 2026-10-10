r"""那 26ms 到底是不是硬件解码互操作的同步开销？

上一轮我测 `hwdec=no` 得到 22.57ms，但那是**播放中热改属性** —— mpv 对已加载的文件
不会重开解码链，所以那次测量是无效的。

这次改成：**先起一个不打开媒体的实例 → 设好 hwdec → 再打开文件 → 再量 paintGL**，
保证解码链从一开始就是目标配置。
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

MEDIA = os.path.join(ROOT, "assets", "testmedia", "clip_1080p.mp4")
LOG = []
REPORT = {}


def log(m):
    print(m, flush=True)
    LOG.append(m)


def run_case(app, hwdec, extra=None):
    ctl_name = "zhjhw"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", ctl_name,
                          "--no-single-instance"],          # ← 故意不带媒体
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    ctl = ControlClient(ctl_name, timeout=60.0)
    for _ in range(200):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(3.5)

    def ev(expr):
        r = ctl.call(cmd="eval", expr=expr)
        return r.get("value") if r.get("ok") else f"ERR:{r.get('error')}"

    hwnd = wi.find_main_window("至简Player", pid=p.pid, min_w=400, min_h=300)["hwnd"]
    ctl.call(cmd="resize", w=1280, h=760)
    time.sleep(0.6)
    # 打开媒体**之前**就把解码链配置好
    ev(f'm.engine.handle.set_property("hwdec", "{hwdec}")')
    for k, v in (extra or {}).items():
        ev(f'm.engine.handle.set_property("{k}", "{v}")')
    time.sleep(0.4)
    ev(f'm.open_path(r"{MEDIA}")')
    time.sleep(5.0)
    ev('m.engine.handle.set_property("loop-file", "inf")')
    time.sleep(1.2)

    actual = ev('m.engine.handle.get_property("hwdec-current")')
    p0 = ev("m.stage.video.paint_stats()")
    time.sleep(3.0)
    p1 = ev("m.stage.video.paint_stats()")
    d = max(1, p1["count"] - p0["count"])
    ms = (p1["total_ms"] - p0["total_ms"]) / d
    log(f"  hwdec={hwdec:<16} 实际={str(actual):<12} {d:>3} 帧/3s  "
        f"mpv 单帧 {ms:6.2f}ms")
    try:
        ctl.call(cmd="quit")
    except Exception:
        pass
    time.sleep(1.5)
    try:
        p.terminate()
    except Exception:
        pass
    time.sleep(1.5)
    return {"hwdec": hwdec, "actual": actual, "frames": d, "ms_per_frame": round(ms, 2)}


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    log("=== 每种配置都从**冷启动**建解码链 ===")
    rows = []
    for hwdec, extra in (
        ("auto-safe", None),                       # 现状
        ("no", None),                              # 纯软解
        ("nvdec", None),                           # 显式 nvdec
        ("nvdec-copy", None),                      # 解码在 GPU、拷回内存再上传
        ("auto-safe", {"gpu-api": "opengl", "vd-lavc-dr": "no"}),
        ("no", {"profile": "fast"}),               # 软解 + 最省渲染
    ):
        rows.append(run_case(app, hwdec, extra))
    REPORT["rows"] = rows
    best = min(rows, key=lambda r: r["ms_per_frame"])
    log(f"最快配置: hwdec={best['hwdec']} → {best['ms_per_frame']}ms/帧")
    with open(os.path.join(ROOT, "logs", "hwdec-probe.json"), "w", encoding="utf-8") as f:
        json.dump({"report": REPORT, "log": LOG}, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
