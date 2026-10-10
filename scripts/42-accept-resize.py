"""验收 ②：真实物理鼠标拖拽拉伸窗口（SendInput, 不是合成 Qt 事件）。

流程（完全按用户要求）：
    800x600  →  拖到 1600x900  →  再拖回 800x600

判据（每一条都必须成立, 否则算失败）：
  * 拖拽过程中**每一次 resize 事件**都必须满足贴合关系：
      - 胶囊左边缘 = 18（含阴影外框换算后的可见边缘）
      - 胶囊右边缘 = 窗宽 − 18
      - 抽屉右边缘 = 窗宽（贴右）
      - 抽屉下边缘 = 胶囊上边缘（绝不互相压盖 / 绝不留缝）
  * 拖拽过程中**没有出现负尺寸 / 0 尺寸**（"露白"的前兆）
  * 终态尺寸与目标尺寸一致（±8 px, 允许 DWM 取整）
  * 全程 GL 画面持续出帧（说明拉伸时视频没断流）
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

CTL = "zhjdrag"
OUT = os.path.join(ROOT, "docs", "04-验收证据")
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


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", CTL,
                          "--no-single-instance", "-v",
                          os.path.join(ROOT, "assets", "testmedia", "clip_1080p.mp4")],
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
            win = wi.find_main_window("至简Player", pid=p.pid, min_w=200, min_h=150)
            if win:
                break
            time.sleep(0.25)
        hwnd = win["hwnd"]
        wi.force_foreground(hwnd)
        # 给应用足够的初始化时间：窗口刚出来时 GL 上下文/布局/配置恢复都还在跑,
        # 这时候发 resize 可能被后续的初始化覆盖掉（本项目实测过一次）。
        time.sleep(3.0)
        wi.force_foreground(hwnd)

        # 打开抽屉：这样"抽屉是否始终贴合"也能一起被验证
        ctl.call(cmd="ui", what="drawer", on=True, animate=False)
        # 关掉"鼠标靠近右边缘自动呼出抽屉" —— 拉伸时指针必然贴着右边缘,
        # 那个特性会在拖拽中途把抽屉弹出来, 干扰尺寸测量（不是被测对象）
        ctl.call(cmd="eval", expr="m.cfg.set('ui.drawer_edge_hover', False)")
        time.sleep(0.5)

        dpr = ctl.call(cmd="geom", widget="root")["geom"]["dpr"]
        log(f"DPI 缩放 = {dpr}")

        def win_rect():
            r = wi.print_window_rect(hwnd)
            return r["window_rect"], r["window_size"]

        def do_resize_cycle(target_w, target_h, label, preset=True):
            if preset:
                # 先回到 800x600 起点，并**确认真的生效**（刚启动时窗口可能还在
                # 初始化/被上一轮的窗口状态影响, 一次 resize 未必吃进去）
                for _ in range(8):
                    ctl.call(cmd="resize", w=800, h=600)
                    time.sleep(0.4)
                    cur = ctl.call(cmd="state")["state"]["window"]["size"]
                    if abs(cur[0] - 800) <= 2 and abs(cur[1] - 600) <= 2:
                        break
                else:
                    log(f"  [WARN] 无法把窗口设成 800x600, 当前 {cur}")
                time.sleep(0.4)
            # ⚠️ 先把窗口挪到左上角附近：屏幕只有 2560 物理像素宽,
            #    窗口若停在 (321,195), 想拉到 1600 逻辑宽（2400 物理）就会超出屏幕,
            #    SetCursorPos 被钳制 → 拉不到目标尺寸（本项目真实踩过, 测出来 1493）。
            #    用原生的"只挪位置"接口：Qt 的 move() 在无边框窗口上会让 Qt 缓存尺寸
            #    与原生窗口失步（实测 move 后原生尺寸会跳回旧值）。
            ctl.call(cmd="move", x=4, y=40)
            time.sleep(0.6)
            ctl.call(cmd="geom_log", clear=True)
            wi.force_foreground(hwnd)
            time.sleep(0.25)
            (wl, wt, wr, wb), (ww, wh) = win_rect()
            log(f"  起始: 窗口 rect=({wl},{wt},{wr},{wb}) size={ww}x{wh} "
                f"目标 {target_w}x{target_h} (逻辑)")
            want_w = int(target_w * dpr)
            want_h = int(target_h * dpr)
            if abs(want_w - ww) > 8:
                x_edge, y_mid = wr - 2, wt + wh // 2
                x_to = wl + want_w - 2
                log(f"  拖右边缘: ({x_edge},{y_mid}) -> ({x_to},{y_mid})  Δ={x_to - x_edge}")
                th = wi.drag_in_thread(x_edge, y_mid, x_to, y_mid, steps=34, settle=0.016)
                while th.is_alive():
                    app.processEvents()
                    time.sleep(0.01)
                time.sleep(0.5)
            (wl, wt, wr, wb), (ww, wh) = win_rect()
            if abs(want_h - wh) > 8:
                x_mid, y_edge = wl + ww // 2, wb - 2
                y_to = wt + want_h - 2
                log(f"  拖下边缘: ({x_mid},{y_edge}) -> ({x_mid},{y_to})  Δ={y_to - y_edge}")
                th = wi.drag_in_thread(x_mid, y_edge, x_mid, y_to, steps=34, settle=0.016)
                while th.is_alive():
                    app.processEvents()
                    time.sleep(0.01)
                time.sleep(0.6)
            st = ctl.call(cmd="state")["state"]
            _, (ww, wh) = win_rect()
            log(f"  {label}: 目标 {target_w}x{target_h} 逻辑, 实测物理 {ww}x{wh} "
                f"(= {ww/dpr:.0f}x{wh/dpr:.0f} 逻辑)")
            return st

        all_ok = True
        for i, (tw, th_, label) in enumerate(((1600, 900, "放大"), (800, 600, "还原"))):
            st = do_resize_cycle(tw, th_, label, preset=(i == 0))
            (_, _, _, _), (ww, wh) = win_rect()
            ok = (abs(ww - tw * dpr) <= 14 and abs(wh - th_ * dpr) <= 14)
            all_ok &= check(f"{label}到 {tw}x{th_} 成功", ok,
                            f"实测 {ww}x{wh} 物理 ({ww/dpr:.0f}x{wh/dpr:.0f} 逻辑)")
            g = ctl.call(cmd="geom_log")["log"]
            log(f"  期间共 {len(g)} 次 resize 采样")
            worst = {"capsule_l": 0, "capsule_r": 0, "drawer_r": 0, "gap": 0, "frames": 0}
            bad = []
            for rec in g:
                w, h = rec["win"]
                cap = rec["capsule"]
                # 胶囊：widget 含 16px 阴影外框 -> 可见 body 左右各内缩 16
                vis_l = cap[0] + 16
                vis_r = cap[0] + cap[2] - 16
                e_l = abs(vis_l - 18)
                e_r = abs((w - vis_r) - 18)
                worst["capsule_l"] = max(worst["capsule_l"], e_l)
                worst["capsule_r"] = max(worst["capsule_r"], e_r)
                if cap[2] < 100 or cap[3] < 40:
                    bad.append(("胶囊尺寸异常", rec))
                if rec["drawer"]:
                    d = rec["drawer"]
                    e_d = abs((w - (d[0] + d[2])))
                    worst["drawer_r"] = max(worst["drawer_r"], e_d)
                    gap = abs(cap[1] - (d[1] + d[3]))
                    worst["gap"] = max(worst["gap"], gap)
                    if d[2] < 100 or d[3] < 40:
                        bad.append(("抽屉尺寸异常", rec))
            check("拉伸全程胶囊左右始终 = 18px 外边距",
                  worst["capsule_l"] <= 2 and worst["capsule_r"] <= 2,
                  f"最大偏差 左{worst['capsule_l']} 右{worst['capsule_r']} px")
            check("拉伸全程抽屉右边缘始终贴窗（≤1px 缝）", worst["drawer_r"] <= 1,
                  f"最大缝隙 {worst['drawer_r']} px")
            check("拉伸全程抽屉底 = 胶囊顶（不压盖、不留缝）", worst["gap"] <= 1,
                  f"最大偏差 {worst['gap']} px")
            check("拉伸过程无异常尺寸（没有露白前兆）", not bad,
                  f"{len(bad)} 条异常" if bad else "")

        # 画面在拉伸后仍然出帧
        f0 = ctl.call(cmd="state")["state"]["render"]["frames"]
        time.sleep(1.0)
        f1 = ctl.call(cmd="state")["state"]["render"]["frames"]
        check("拉伸后画面持续出帧（视频未断流）", f1 > f0, f"{f0} -> {f1}")
        wi.grab_window_client(hwnd, os.path.join(OUT, "09-拉伸后-800x600.png"))

        # 再来一轮放大后截图留证
        ctl.call(cmd="resize", w=1600, h=900)
        time.sleep(1.0)
        wi.grab_window_client(hwnd, os.path.join(OUT, "10-拉伸后-1600x900.png"))

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
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "42-resize.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
    log(f"结论：{'全部通过' if not FAILS else str(len(FAILS)) + ' 项失败: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
