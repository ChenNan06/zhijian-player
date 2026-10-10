r"""复现并量化：全屏 / 最大化 / 切走再切回 / 拖动窗口 / 开抽屉 的黑屏与掉帧。

对每个场景都同时采三样东西：
  * 窗口与视频控件的几何（看有没有尺寸错位）
  * 渲染帧计数（看画面有没有在出帧）
  * **真实屏幕像素**（看用户眼睛看到的是不是黑的 —— 这才是判据）
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

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import _wininput as wi  # noqa: E402
from zhijian.ipc import ControlClient  # noqa: E402

MEDIA = os.path.join(ROOT, "assets", "testmedia")
OUT = os.path.join(ROOT, "logs", "fsdiag")
LOG = []
REPORT = {}


def log(m):
    print(m, flush=True)
    LOG.append(m)


def screen_mean(x, y, w, h):
    """抓真实屏幕的一小块，返回平均亮度（0~255）与黑像素占比。"""
    from PySide6.QtGui import QImage

    img = _grab()
    if img is None:
        return None
    img = img.copy(int(x), int(y), int(w), int(h))
    if img.isNull() or img.width() < 2:
        return None
    tot = n = black = 0
    for yy in range(0, img.height(), max(1, img.height() // 24)):
        for xx in range(0, img.width(), max(1, img.width() // 24)):
            c = img.pixelColor(xx, yy)
            v = (c.red() + c.green() + c.blue()) / 3.0
            tot += v
            n += 1
            if v < 12:
                black += 1
    return {"mean": round(tot / max(1, n), 1), "black_ratio": round(black / max(1, n), 3)}


_SCREEN = None


def _grab():
    global _SCREEN
    if _SCREEN is None:
        _SCREEN = QGuiApplication.primaryScreen()
    return _SCREEN.grabWindow(0).toImage()      # grabWindow 返回 QPixmap，要转成 QImage


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    os.makedirs(OUT, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", "zhjfs",
                          "--no-single-instance", os.path.join(MEDIA, "clip_1080p.mp4")],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    ctl = ControlClient("zhjfs", timeout=60.0)
    for _ in range(200):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(5)
    win = wi.find_main_window("至简Player", min_w=400, min_h=300)
    hwnd = win["hwnd"]
    wi.force_foreground(hwnd)
    time.sleep(1)

    def snap(tag, sample=True):
        st = ctl.call(cmd="state")["state"]
        g = ctl.call(cmd="geom", widget="video")["geom"]
        r = wi.print_window_rect(hwnd)
        row = {
            "tag": tag,
            "win_size": r["window_size"],
            "win_rect": r["window_rect"],
            "video_in_window": g["in_window"],
            "frames": st["render"]["frames"],
            "rc_ready": st["render"]["rc_ready"],
        }
        if sample:
            vr = r["window_rect"]
            row["screen"] = screen_mean(vr[0] + 40, vr[1] + 60,
                                        max(40, vr[2] - 80), max(40, vr[3] - 220))
        log(f"  {tag:<26} win={row['win_size']} video={row['video_in_window']} "
            f"frames={row['frames']} screen={row.get('screen')}")
        return row

    rows = []
    try:
        # ---------- 1. 全屏 ----------
        log("=== 1. 全屏 ===")
        rows.append(snap("全屏前"))
        t0 = time.perf_counter()
        ctl.call(cmd="ui", what="fullscreen", on=True)
        # 每 100ms 采一次屏幕亮度，看"黑多久"
        seq = []
        for i in range(45):
            time.sleep(0.1)
            r = wi.print_window_rect(hwnd)
            vr = r["window_rect"]
            s = screen_mean(vr[0] + 60, vr[1] + 80, max(40, vr[2] - 120), max(40, vr[3] - 260))
            st = ctl.call(cmd="state")["state"]
            seq.append({"t": round(time.perf_counter() - t0, 2),
                        "win": r["window_size"], "frames": st["render"]["frames"],
                        "screen": s})
        black_until = None
        for s in seq:
            if s["screen"] and s["screen"]["black_ratio"] > 0.9:
                black_until = s["t"]
            else:
                break
        log(f"  全屏后连续黑屏时长: {black_until if black_until else 0} s")
        REPORT["fullscreen"] = {"seq": seq, "black_seconds": black_until}
        rows.append(snap("全屏后 +4.5s"))

        # ---------- 2. 全屏下切走再切回 ----------
        log("=== 2. 全屏下切到别的窗口再切回 ===")
        other = wi.list_windows(min_w=300, min_h=200)
        other = [w for w in other if w["hwnd"] != hwnd]
        if other:
            wi.force_foreground(other[0]["hwnd"])
            time.sleep(1.5)
            back_t0 = time.perf_counter()
            wi.force_foreground(hwnd)
            seq2 = []
            for i in range(35):
                time.sleep(0.1)
                r = wi.print_window_rect(hwnd)
                vr = r["window_rect"]
                st = ctl.call(cmd="state")["state"]
                s = screen_mean(vr[0] + 60, vr[1] + 80, max(40, vr[2] - 120),
                                max(40, vr[3] - 260))
                seq2.append({"t": round(time.perf_counter() - back_t0, 2),
                             "frames": st["render"]["frames"], "screen": s})
            bt = None
            for s in seq2:
                if s["screen"] and s["screen"]["black_ratio"] > 0.9:
                    bt = s["t"]
                else:
                    break
            log(f"  切回后连续黑屏时长: {bt if bt else 0} s")
            REPORT["switch_back"] = {"seq": seq2, "black_seconds": bt}
        else:
            log("  （没找到别的窗口可切）")

        # ---------- 3. 退出全屏 + 最大化 ----------
        log("=== 3. 退出全屏 → 最大化 ===")
        ctl.call(cmd="ui", what="fullscreen", on=False)
        time.sleep(1.5)
        rows.append(snap("退出全屏"))
        t0 = time.perf_counter()
        ctl.call(cmd="cmd", argv=["noop"])
        wi.click(wi.print_window_rect(hwnd)["window_rect"][0] + 1180,
                 wi.print_window_rect(hwnd)["window_rect"][1] + 30)  # 标题栏最大化按钮
        time.sleep(0.4)
        seq3 = []
        for i in range(25):
            time.sleep(0.1)
            r = wi.print_window_rect(hwnd)
            vr = r["window_rect"]
            st = ctl.call(cmd="state")["state"]
            s = screen_mean(vr[0] + 60, vr[1] + 80, max(40, vr[2] - 120), max(40, vr[3] - 260))
            seq3.append({"t": round(time.perf_counter() - t0, 2), "win": r["window_size"],
                         "frames": st["render"]["frames"], "screen": s})
        REPORT["maximize"] = {"seq": seq3}
        rows.append(snap("最大化后"))

        # ---------- 4. 拖动窗口时的帧率 ----------
        log("=== 4. 拖动窗口（真实鼠标按住标题栏拖 2 秒）===")
        ctl.call(cmd="ui", what="fullscreen", on=False)
        time.sleep(1.0)
        if ctl.call(cmd="state")["state"]["window"]["size"][0] > 1500:
            ctl.call(cmd="resize", w=1100, h=700)
            time.sleep(0.8)
        ctl.call(cmd="move", x=120, y=90)
        time.sleep(0.8)
        wi.force_foreground(hwnd)
        r0 = wi.print_window_rect(hwnd)
        x0, y0 = r0["window_rect"][0], r0["window_rect"][1]
        f0 = ctl.call(cmd="state")["state"]["render"]["frames"]
        t0 = time.perf_counter()
        wi.drag_in_thread(x0 + 300, y0 + 22, x0 + 620, y0 + 260, steps=70, settle=0.028).join()
        dur = time.perf_counter() - t0
        f1 = ctl.call(cmd="state")["state"]["render"]["frames"]
        log(f"  拖动 {dur:.2f}s 期间渲染 {f1 - f0} 帧 → {((f1-f0)/dur):.0f} fps")
        REPORT["drag_fps"] = {"seconds": round(dur, 2), "frames": f1 - f0,
                              "fps": round((f1 - f0) / dur, 1)}

        # ---------- 5. 开抽屉时的帧率 ----------
        log("=== 5. 播放列表抽屉展开动画 ===")
        ctl.call(cmd="ui", what="drawer", on=False, animate=False)
        time.sleep(0.6)
        f0 = ctl.call(cmd="state")["state"]["render"]["frames"]
        t0 = time.perf_counter()
        ctl.call(cmd="ui", what="drawer", on=True, animate=True)
        time.sleep(1.2)
        dur = time.perf_counter() - t0
        f1 = ctl.call(cmd="state")["state"]["render"]["frames"]
        log(f"  抽屉动画 {dur:.2f}s 期间渲染 {f1 - f0} 帧 → {((f1-f0)/dur):.0f} fps")
        REPORT["drawer_fps"] = {"seconds": round(dur, 2), "frames": f1 - f0,
                                "fps": round((f1 - f0) / dur, 1)}
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

    with open(os.path.join(ROOT, "logs", "fs-diag.json"), "w", encoding="utf-8") as f:
        json.dump({"report": REPORT, "log": LOG, "rows": rows}, f, ensure_ascii=False, indent=2)
    log("明细 -> logs\\fs-diag.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
