r"""全屏切换到底黑多久、黑在哪一步。

用户描述："切换全屏画面会黑屏，过一下才显示画面，就跟游戏里切换独占全屏一样"。
这个脚本用**高频真实屏幕采样**量出黑屏时长，并同时记录：
  * mpv 渲染上下文是否被销毁重建（GL 上下文丢失的直接证据）
  * 原生窗口样式是否变化（独占全屏式切换的典型特征）
  * Qt 窗口尺寸何时才追上原生尺寸
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import _wininput as wi  # noqa: E402
from zhijian.ipc import ControlClient  # noqa: E402

MEDIA = os.path.join(ROOT, "assets", "testmedia")
u = ctypes.windll.user32
LOG = []
REPORT = {}


def log(m):
    print(m, flush=True)
    LOG.append(m)


def grab():
    return QGuiApplication.primaryScreen().grabWindow(0).toImage()


def region_stats(img, x, y, w, h):
    img = img.copy(int(x), int(y), int(w), int(h))
    if img.isNull() or img.width() < 4:
        return None
    tot = n = dark = 0
    for yy in range(0, img.height(), max(1, img.height() // 20)):
        for xx in range(0, img.width(), max(1, img.width() // 20)):
            c = img.pixelColor(xx, yy)
            v = (c.red() + c.green() + c.blue()) / 3.0
            tot += v
            n += 1
            if v < 14:
                dark += 1
    return {"mean": round(tot / max(1, n), 1), "dark": round(dark / max(1, n), 3)}


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env["ZHJ_TRACE_GL"] = "1"
    logfile = os.path.join(os.environ["APPDATA"], "ZhiJian", "ZhiJianPlayer", "zhijian.log")
    try:
        os.remove(logfile)
    except OSError:
        pass
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", "zhjfs2",
                          "--no-single-instance", os.path.join(MEDIA, "clip_1080p.mp4")],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    ctl = ControlClient("zhjfs2", timeout=60.0)
    for _ in range(200):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(6)
    hwnd = wi.find_main_window("至简Player", min_w=400, min_h=300)["hwnd"]
    wi.force_foreground(hwnd)
    time.sleep(1)

    def styles():
        GWL_STYLE, GWL_EX = -16, -20
        return (u.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF,
                u.GetWindowLongW(hwnd, GWL_EX) & 0xFFFFFFFF)

    def frames():
        return ctl.call(cmd="state")["state"]["render"]["frames"]

    def measure(tag, action):
        """action() 之后 2 秒内高频采样屏幕亮度 + 帧计数。"""
        s0 = styles()
        f0 = frames()
        samples = []
        stop = threading.Event()

        def sampler():
            t0 = time.perf_counter()
            while not stop.is_set():
                img = grab()
                r = wi.print_window_rect(hwnd)
                vr = r["window_rect"]
                st = region_stats(img, vr[0] + 80, vr[1] + 100,
                                  max(40, vr[2] - 160), max(40, vr[3] - 320))
                samples.append({"t": round(time.perf_counter() - t0, 3),
                                "frames": frames(), "screen": st})
                time.sleep(0.04)

        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        time.sleep(0.35)                 # 先采一段"切换前"的基线
        t_act = time.perf_counter()
        action()
        time.sleep(2.0)
        stop.set()
        th.join(timeout=3)
        s1 = styles()
        # 黑屏起点：从 action 时刻起，第一段 dark>0.85 的连续样本
        after = [x for x in samples if x["t"] >= 0]
        base = [x for x in after if x["screen"]][:3]
        dark_from = dark_to = None
        started = False
        for x in after:
            sc = x["screen"]
            if not sc:
                continue
            if sc["dark"] > 0.85:
                if not started:
                    started = True
                    dark_from = x["t"]
                dark_to = x["t"]
            elif started:
                break
        row = {
            "tag": tag, "style_before": f"0x{s0[0]:08X}/0x{s0[1]:08X}",
            "style_after": f"0x{s1[0]:08X}/0x{s1[1]:08X}",
            "style_changed": s0 != s1,
            "frames_before": f0, "frames_after": frames(),
            "baseline": base[0]["screen"] if base else None,
            "dark_from_s": dark_from, "dark_to_s": dark_to,
            "dark_span_s": round((dark_to - dark_from), 2) if dark_from is not None else 0.0,
        }
        log(f"  {tag}: 样式变化={row['style_changed']} "
            f"({row['style_before']} → {row['style_after']})  "
            f"黑屏 {row['dark_span_s']}s  帧 {f0}→{row['frames_after']}")
        return row

    rows = []
    try:
        log("=== 全屏切换 ===")
        rows.append(measure("进入全屏", lambda: ctl.call(cmd="ui", what="fullscreen", on=True)))
        time.sleep(1.0)
        rows.append(measure("退出全屏", lambda: ctl.call(cmd="ui", what="fullscreen", on=False)))
        time.sleep(1.0)
        rows.append(measure("最大化", lambda: ctl.call(cmd="eval", expr="m.showMaximized()")))
        time.sleep(1.0)
        rows.append(measure("还原", lambda: ctl.call(cmd="eval", expr="m.showNormal()")))
        REPORT["rows"] = rows
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

    # 日志里数"渲染上下文已建立"出现几次 —— 多于 1 次就是 GL 上下文被销毁重建了
    try:
        import io
        lines = io.open(logfile, encoding="utf-8", errors="replace").read().splitlines()
        created = [l for l in lines if "渲染上下文已建立" in l]
        released = [l for l in lines if "释放 mpv 渲染上下文" in l]
        REPORT["rc_created"] = len(created)
        REPORT["rc_released"] = len(released)
        log(f"=== mpv 渲染上下文：建立 {len(created)} 次，释放 {len(released)} 次 ===")
        for l in created:
            log("   " + l[11:60])
    except Exception as exc:
        log(f"读日志失败: {exc}")

    with open(os.path.join(ROOT, "logs", "fs-black.json"), "w", encoding="utf-8") as f:
        json.dump({"report": REPORT, "log": LOG}, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
