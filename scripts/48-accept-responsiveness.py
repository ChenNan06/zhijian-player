r"""验收 ⑧：交互跟手性（音量条 / 进度条拖动）。

判据不是"感觉"，而是三组可复现的数字：
  A. 单次音量写入的**阻塞时长**（UI 线程真实耗时）
  B. 模拟一次完整拖动（120 次连续写入）的**总耗时与最大单次耗时**
  C. 拖动期间事件循环有没有被堵住（用 QTimer 心跳测最大间隔）
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import _wininput as wi  # noqa: E402
from zhijian.ipc import ControlClient  # noqa: E402

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


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    name = "zhjperf"
    logfile = os.path.join(os.environ.get("APPDATA", ""), "ZhiJian", "ZhiJianPlayer",
                           "zhijian.log")
    try:
        os.remove(logfile)
    except OSError:
        pass
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "zhijian", "--control", name, "--no-single-instance",
         os.path.join(MEDIA, "clip_1080p.mp4")],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    ctl = ControlClient(name, timeout=60)
    ok = False
    for _ in range(120):
        if proc.poll() is not None:
            break
        try:
            ctl.call(cmd="ping")
            ok = True
            break
        except Exception:
            time.sleep(0.5)
    if not ok:
        err = b""
        try:
            err = proc.stderr.read() or b""
        except Exception:
            pass
        log("应用没起来：\n" + err.decode("utf-8", "replace")[-1500:])
        return 2
    time.sleep(4)

    try:
        # ---- A. 单次写入阻塞时长 ----
        log("=== A. 单次音量写入阻塞时长（UI 线程真实耗时）===")
        r = ctl.call(cmd="bench", what="volume_set", n=70)
        b = r.get("result", {})
        log(f"  70 次连续写 volume: 总计 {b.get('total_ms')}ms  "
            f"中位 {b.get('p50_ms')}ms  最大 {b.get('max_ms')}ms")
        REPORT["volume_set"] = b
        check("单次音量写入中位 < 5ms（不阻塞 UI）", (b.get("p50_ms") or 99) < 5.0,
              f"p50={b.get('p50_ms')}ms")
        check("单次音量写入最大 < 20ms", (b.get("max_ms") or 999) < 20.0,
              f"max={b.get('max_ms')}ms")

        # ---- B. 拖动全程 ----
        log("=== B. 模拟一次完整拖动（音量 0→100→0，每像素一次写入）===")
        r = ctl.call(cmd="bench", what="volume_drag", n=240)
        d = r.get("result", {})
        log(f"  240 次写入（含滑块重绘）: 总计 {d.get('total_ms')}ms  "
            f"中位 {d.get('p50_ms')}ms  最大 {d.get('max_ms')}ms")
        REPORT["volume_drag"] = d
        check("整段拖动平均每帧 < 4ms（60fps 预算内）",
              (d.get("mean_ms") or 99) < 4.0, f"mean={d.get('mean_ms')}ms")
        check("整段拖动最大单次 < 25ms（不会掉到 40fps 以下）",
              (d.get("max_ms") or 999) < 25.0, f"max={d.get('max_ms')}ms")

        # ---- C. 事件循环心跳（跨两次调用量）----
        log("=== C. 拖动期间事件循环心跳（QTimer 20ms 基线）===")
        ctl.call(cmd="bench", what="loop_lag_start")
        time.sleep(1.5)                       # 先让心跳跑出一个基线
        for _ in range(8):                    # 连续 8 段拖动，累计约 1000 次写入
            ctl.call(cmd="bench", what="volume_drag", n=120)
        time.sleep(1.0)
        r = ctl.call(cmd="bench", what="loop_lag_stop")
        lag = r.get("result", {})
        log(f"  应用侧事件循环间隔: 中位 {lag.get('p50_ms')}ms  最大 {lag.get('max_ms')}ms  "
            f"（心跳基线 20ms；>60ms {lag.get('over_60ms')} 次，>150ms {lag.get('over_150ms')} 次）")
        REPORT["loop_lag"] = lag
        check("拖动期间事件循环最大间隔 < 60ms（没有长卡顿）",
              (lag.get("max_ms") or 999) < 60.0, f"max={lag.get('max_ms')}ms")
        # ⚠️ 别写成 `(x or 1) == 0` —— 值本来就是 0 时 `or` 会把它换成 1，永远判失败
        check("拖动期间没有 >150ms 的僵死", int(lag.get("over_150ms", 1)) == 0,
              f"超 150ms 次数={lag.get('over_150ms')}")
        check("心跳次数足够多（样本有效）", (lag.get("n") or 0) >= 30,
              f"n={lag.get('n')}")

        # ---- D. 进度条拖动（关键帧 seek 预览）----
        log("=== D. 进度条拖动（40 次关键帧 seek 预览）===")
        r = ctl.call(cmd="bench", what="seek_drag", n=40)
        s = r.get("result", {})
        log(f"  40 次预览 seek: 中位 {s.get('p50_ms')}ms  最大 {s.get('max_ms')}ms  "
            f"丢帧 {s.get('frame_drops_delta')}")
        REPORT["seek_drag"] = s
        check("进度条拖动单次 < 120ms", (s.get("max_ms") or 999) < 120.0,
              f"max={s.get('max_ms')}ms")
        check("进度条拖动不丢帧", (s.get("frame_drops_delta") or 0) == 0,
              f"drops={s.get('frame_drops_delta')}")
    finally:
        try:
            ctl.call(cmd="quit")
        except Exception:
            pass
        time.sleep(2)
        try:
            proc.terminate()
        except Exception:
            pass

    with open(os.path.join(ROOT, "logs", "48-responsiveness.json"), "w",
              encoding="utf-8") as f:
        json.dump({"report": REPORT, "log": LOG}, f, ensure_ascii=False, indent=2)
    log(f"结论：{'全部通过' if not FAILS else str(len(FAILS)) + ' 项失败: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
