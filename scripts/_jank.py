r"""动画掉帧到底花在哪：视频重绘 vs 控件重绘 vs 整窗合成。

用户："播放时拖动窗口、来回拖进度条音量条、展开播放列表，任何与动画有关的东西都会掉帧。
      为什么爱奇艺万能播放器就不会？"

三种假设，用数据分开：
  A. 每个控件重绘都迫使**整窗（含 GL 视频层）重新合成** ⇒ 动画期间视频帧数会远超视频本身的帧率
  B. 视频重绘本身太贵 ⇒ 停播后动画应该变流畅
  C. 某个控件自己的 paintEvent 太贵 ⇒ 单独量它
"""

from __future__ import annotations

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

from PySide6.QtWidgets import QApplication  # noqa: E402

import _wininput as wi  # noqa: E402
from zhijian.ipc import ControlClient  # noqa: E402

MEDIA = os.path.join(ROOT, "assets", "testmedia")
LOG = []
REPORT = {}


def log(m):
    print(m, flush=True)
    LOG.append(m)


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", "zhjjank",
                          "--no-single-instance", os.path.join(MEDIA, "clip_1080p.mp4")],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    ctl = ControlClient("zhjjank", timeout=60.0)
    for _ in range(200):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(6)
    hwnd = wi.find_main_window("至简Player", min_w=400, min_h=300)["hwnd"]
    ctl.call(cmd="resize", w=1280, h=760)
    time.sleep(0.8)
    wi.force_foreground(hwnd)
    time.sleep(0.6)

    def frames():
        return ctl.call(cmd="state")["state"]["render"]["frames"]

    def timed(tag, action, dur=2.0):
        """跑 action 一段时间，量事件循环间隔 + 视频重绘帧数。"""
        ctl.call(cmd="bench", what="loop_lag_start")
        f0 = frames()
        t0 = time.perf_counter()
        th = threading.Thread(target=action, daemon=True)
        th.start()
        time.sleep(dur)
        th.join(timeout=4)
        el = time.perf_counter() - t0
        f1 = frames()
        lag = ctl.call(cmd="bench", what="loop_lag_stop")["result"]
        row = {"tag": tag, "seconds": round(el, 2), "video_frames": f1 - f0,
               "video_fps": round((f1 - f0) / el, 1),
               "lag_p50": lag.get("p50_ms"), "lag_max": lag.get("max_ms"),
               "over_60": lag.get("over_60ms"), "over_150": lag.get("over_150ms"),
               "n": lag.get("n")}
        log(f"  {tag:<22} {el:.1f}s  视频重绘 {row['video_frames']:>4} 帧 "
            f"({row['video_fps']:>5.1f} fps)  事件循环 p50 {row['lag_p50']}ms "
            f"max {row['lag_max']}ms  >60ms {row['over_60']}")
        return row

    rows = []
    try:
        # 基线：什么都不做
        rows.append(timed("基线（静止播放）", lambda: time.sleep(2.5), 2.0))

        # A. 拖动音量条
        def drag_volume():
            for _ in range(14):
                ctl.call(cmd="bench", what="volume_drag", n=24)
                time.sleep(0.05)
        rows.append(timed("拖音量条", drag_volume, 2.0))

        # B. 拖进度条
        def drag_seek():
            for _ in range(8):
                ctl.call(cmd="bench", what="seek_drag", n=20)
                time.sleep(0.08)
        rows.append(timed("拖进度条", drag_seek, 2.0))

        # C. 抽屉开合动画
        def drawer_anim():
            for _ in range(5):
                ctl.call(cmd="ui", what="drawer", on=True, animate=True)
                time.sleep(0.42)
                ctl.call(cmd="ui", what="drawer", on=False, animate=True)
                time.sleep(0.42)
        rows.append(timed("抽屉开合动画", drawer_anim, 2.2))

        # D. 真实鼠标拖窗口
        r0 = wi.print_window_rect(hwnd)
        x0, y0 = r0["window_rect"][0], r0["window_rect"][1]

        def drag_window():
            wi.drag_in_thread(x0 + 300, y0 + 22, x0 + 620, y0 + 240,
                              steps=60, settle=0.03).join()
        rows.append(timed("真实拖动窗口", drag_window, 2.4))

        # E. 暂停后再拖音量条（对照：视频不重绘时动画是否流畅）
        ctl.call(cmd="eval", expr="m.toggle_pause()")
        time.sleep(1.0)
        log(f"  （已暂停，playing={ctl.call(cmd='state')['state']['playback']['playing']}）")
        rows.append(timed("暂停后拖音量条", drag_volume, 2.0))
        rows.append(timed("暂停后抽屉动画", drawer_anim, 2.2))
        ctl.call(cmd="eval", expr="m.toggle_pause()")
        time.sleep(0.8)

        # 各控件单次重绘成本
        cost = ctl.call(cmd="bench", what="paint_cost")["result"]
        log(f"  控件单次重绘: {cost}")
        REPORT["paint_cost"] = cost

        # 整窗一次强制重绘要多久（含 GL 层）
        whole = ctl.call(cmd="eval", expr=(
            "(lambda: (__import__('time').perf_counter()))()"))
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

    with open(os.path.join(ROOT, "logs", "jank.json"), "w", encoding="utf-8") as f:
        json.dump({"report": REPORT, "log": LOG}, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
