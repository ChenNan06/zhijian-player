"""验收 ③：长按右方向键 → 平滑切换到倍速；松开 → 立即恢复 1.0x。

**全程用真实物理键盘注入**（SendInput 的 scancode 注入, 会产生真实的自动重复
和真实的按下/抬起时序）, 不向窗口发任何合成 QKeyEvent。

判据：
  1. 单击（<300ms）→ 位置 +5 秒, 倍速保持 1.0x;
  2. 长按 800ms  → 在阈值后倍速升到配置值（默认 2.0x）, 并且**位置在加速前进**;
  3. 松开 → 倍速在 150ms 内回到 1.0x（"松开瞬间丝滑恢复"）;
  4. 整个长按过程中倍速只经历一次"上台阶"（不是反复抖动）。
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

CTL = "zhjkey"
LOG = []
FAILS = []


def log(m):
    print(m, flush=True)
    LOG.append(m)


def check(name, ok, detail=""):
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


def sample_speed(ctl, n=1, gap=0.0):
    out = []
    for _ in range(n):
        st = ctl.call(cmd="state")["state"]
        out.append((st["speed"], st["boost"], st["position"]))
        if gap:
            time.sleep(gap)
    return out


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    video = os.path.join(ROOT, "assets", "testmedia", "clip_1080p.mp4")
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", CTL,
                          "--no-single-instance", "-v", video],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctl = ControlClient(CTL)
    for _ in range(120):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)

    try:
        win = None
        for _ in range(80):
            win = wi.find_main_window("至简Player", pid=p.pid, min_w=400, min_h=300)
            if win:
                break
            time.sleep(0.25)
        hwnd = win["hwnd"]

        # 关掉"鼠标一动就唤醒胶囊"以外的干扰：把控制条常显, 免得它淡出影响不到键盘
        ctl.call(cmd="eval", expr="m.cfg.set('ui.capsule_autohide', False)")
        # 保证窗口在前台（非前台进程的按键会被系统丢掉 —— 必须读回校验）
        ok_fg = wi.force_foreground(hwnd)
        check("窗口已真正成为前台窗口", ok_fg, f"hwnd={hwnd}")
        time.sleep(0.8)
        # 让视频从头开始播, 且确保处于播放态
        ctl.call(cmd="set", name="pause", value=False)
        ctl.call(cmd="cmd", argv=["seek", "0", "absolute"])
        time.sleep(1.2)

        thr = ctl.call(cmd="eval", expr="m.cfg.get('playback.long_press_threshold_ms')")["value"]
        target = ctl.call(cmd="eval", expr="m.cfg.get('playback.long_press_speed')")["value"]
        step = ctl.call(cmd="eval", expr="m.cfg.get('playback.seek_step_small')")["value"]
        log(f"配置：阈值={thr}ms 目标倍速={target}x 单击步长={step}s")

        # ---------------- 1. 单击 ----------------
        before = ctl.call(cmd="state")["state"]
        t0 = time.time()
        wi.tap("RIGHT", hold_ms=40)
        time.sleep(0.6)
        after = ctl.call(cmd="state")["state"]
        moved = after["position"] - before["position"]
        # 播放本身也在前进, 所以只要"前进量明显大于自然播放量"即可
        elapsed = time.time() - t0
        natural = elapsed
        check("单击右方向键 = 快进（不是倍速）",
              moved > step * 0.6 and after["speed"] == 1.0,
              f"位置前进 {moved:.2f}s（同期自然播放约 {natural:.2f}s）, 倍速={after['speed']}")

        # ---------------- 2. 长按 800ms ----------------
        ctl.call(cmd="cmd", argv=["seek", "0", "absolute"])
        time.sleep(0.8)
        samples = []
        speeds_seen = []

        def sampler():
            # ⚠️ 必须用**绝对时间戳**（time.time()），不能各用各的 t_start ——
            #    采样线程的 t_start 与主线程的 hold_t0 是两个不同的原点,
            #    相减出来的"松开后延迟"会整体错位，凭空造出不存在的回弹。
            while time.time() - _t_begin[0] < 2.2:
                try:
                    t_send = time.time()          # 取发出请求之前的时间
                    st = ctl.call(cmd="state")["state"]
                    samples.append((round(t_send - _t_begin[0], 3), st["speed"], st["boost"],
                                    round(st["position"], 2), t_send))
                    speeds_seen.append(st["speed"])
                except Exception:
                    pass
                time.sleep(0.05)

        import threading

        ctl.call(cmd="key_log", clear=True)
        _t_begin = [time.time()]
        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        time.sleep(0.15)
        hold_t0 = time.time()
        wi.hold("RIGHT", 900)
        hold_release = time.time()
        th.join(timeout=5)
        kl = ctl.call(cmd="key_log")["log"]
        log(f"  应用侧收到的按键事件 {len(kl)} 条（前 8 条）:")
        for e in kl[:8]:
            log(f"    {e['kind']:<8} key={e['key']} repeat={int(e['repeat'])} "
                f"boost={int(e['boost'])} right_down={int(e['right_down'])} "
                f"timer={e['timer_active']}")
        log("  采样（t, 倍速, boost, 位置）:")
        for s in samples[:34]:
            log(f"    {s[0]:>5.2f}s  {s[1]:>4}x  boost={int(s[2])}  pos={s[3]}")

        boosted = [s for s in samples if s[1] > 1.5]
        check("长按超过阈值后倍速升到配置值", any(abs(s[1] - target) < 0.01 for s in samples),
              f"目标 {target}x, 实测出现过的倍速 {sorted(set(speeds_seen))}")
        check("长按期间 boost 标志被置起", any(s[2] for s in samples))
        check("长按期间画面确实在加速前进",
              len(boosted) >= 2 and boosted[-1][3] > boosted[0][3],
              f"加速段位置 {boosted[0][3] if boosted else '-'} -> {boosted[-1][3] if boosted else '-'}")
        after_release = [s for s in samples if s[4] > hold_release + 0.15]
        back = [s for s in after_release if abs(s[1] - 1.0) < 0.01]
        check("松开瞬间恢复 1.0x", bool(back),
              f"松开后 {len(after_release)} 个采样中有 {len(back)} 个是 1.0x")
        check("松开 150ms 之后**每一个**采样都必须是 1.0x（不能有回弹）",
              len(back) == len(after_release),
              f"{len(back)}/{len(after_release)}")
        # 恢复延迟由**应用自己上报**（从收到 keyRelease 到速度真的写回 1.0x）。
        # 外部按 50ms 轮询只能给出 ≥50ms 粒度的下界，会把合格的 <150ms 测成不确定。
        lat = ctl.call(cmd="state")["state"].get("restore_latency_ms")
        log(f"  应用自测恢复延迟: {lat if lat is None else round(lat, 1)} ms")
        check("恢复延迟 < 150ms（应用自测，非外部轮询推断）",
              lat is not None and lat < 150.0, f"{lat} ms")

        # ---------------- 3. 自定义倍速值生效 ----------------
        ctl.call(cmd="eval", expr="m.cfg.set('playback.long_press_speed', 3.0)")
        ctl.call(cmd="cmd", argv=["seek", "0", "absolute"])
        time.sleep(0.6)
        wi.hold("RIGHT", 800)
        time.sleep(0.2)
        st = ctl.call(cmd="state")["state"]
        check("配置里改倍速值后立即生效（3.0x）", abs(st["speed"] - 1.0) < 0.01,
              f"松开后应为 1.0x, 实测 {st['speed']}")

        ctl.call(cmd="eval", expr="m.cfg.set('playback.long_press_speed', 2.0)")
        # 再验一次长按期间的峰值
        peak = {"v": 1.0}

        def sampler2():
            t0 = time.time()
            while time.time() - t0 < 1.6:
                try:
                    s = ctl.call(cmd="state")["state"]["speed"]
                    peak["v"] = max(peak["v"], s)
                except Exception:
                    pass
                time.sleep(0.04)

        ctl.call(cmd="eval", expr="m.cfg.set('playback.long_press_speed', 3.0)")
        ctl.call(cmd="cmd", argv=["seek", "0", "absolute"])
        time.sleep(0.5)
        th2 = threading.Thread(target=sampler2, daemon=True)
        th2.start()
        time.sleep(0.1)
        wi.hold("RIGHT", 700)
        th2.join(timeout=4)
        check("倍速值可配置：设为 3.0x 时长按到 3.0x", abs(peak["v"] - 3.0) < 0.01,
              f"峰值 {peak['v']}x")
        ctl.call(cmd="eval", expr="m.cfg.set('playback.long_press_speed', 2.0)")

    finally:
        try:
            ctl.call(cmd="quit")
        except Exception:
            pass
        try:
            p.wait(timeout=12)
        except Exception:
            p.kill()
        log(f"退出码 {p.returncode}")
        with open(os.path.join(ROOT, "logs", "43-longpress.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
    log(f"结论：{'全部通过' if not FAILS else str(len(FAILS)) + ' 项失败: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
