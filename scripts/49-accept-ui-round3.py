r"""本轮 UI 修复的验收：换肤 / 抽屉自动收回 / 齿轮图标 / 玻璃面缓存 / 布局。

用文件而不是命令行 here-string 来跑 —— 嵌套引号在 PowerShell 里会被吃掉。
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
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", "zhjuichk",
                          "--no-single-instance", os.path.join(MEDIA, "clip_1080p.mp4")],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    ctl = ControlClient("zhjuichk", timeout=60.0)
    for _ in range(200):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(5)

    def ev(expr):
        r = ctl.call(cmd="eval", expr=expr)
        return r.get("value") if r.get("ok") else f"ERR:{r.get('error')}"

    try:
        # ---------------- 1. 换肤真的生效 ----------------
        log("=== 1. 换肤 ===")
        accents = {}
        for name in ("amber", "sakura", "aurora", "mint", "graphite"):
            ok = ev(f'm.apply_theme("{name}")')
            time.sleep(0.35)
            accents[name] = ev('__import__("zhijian.ui.theme", fromlist=["P"]).P["accent"]')
            log(f"   {name:<10} apply={ok}  accent={accents[name]}")
        REPORT["themes"] = accents
        check("5 个主题的强调色各不相同",
              len({str(v) for v in accents.values()}) == 5, str(accents))
        check("切到琥珀后强调色确实是琥珀",
              str(accents.get("amber", "")).lower().startswith("#e8a3"))
        # 全局样式表也被重设了（不是只改了内存里的字典）。
        # 注意：build_stylesheet 里的颜色走 css() 转成 rgba(...)，不会出现 #RRGGBB 字面量。
        ev('m.apply_theme("graphite")')
        time.sleep(0.4)
        qss_g = str(ev('app.styleSheet()'))
        ev('m.apply_theme("amber")')
        time.sleep(0.4)
        qss_a = str(ev('app.styleSheet()'))
        log(f"   样式表 graphite 含冷青={('47,211,192' in qss_g)}  "
            f"amber 含琥珀={('232,163,61' in qss_a)}  两者相同={qss_g == qss_a}")
        check("全局样式表随主题真的换了（不是只改内存字典）",
              qss_a != qss_g and "232,163,61" in qss_a and "47,211,192" in qss_g)
        check("换肤后 app.styleSheet() 与 build_stylesheet() 一致",
              qss_a == str(ev('__import__("zhijian.ui.theme", fromlist=["x"]).build_stylesheet()')))
        ev('m.apply_theme("graphite")')
        time.sleep(0.3)

        # ---------------- 2. 抽屉：自动展开要自动收回 ----------------
        log("=== 2. 抽屉自动收回 ===")
        ctl.call(cmd="ui", what="drawer", on=False, animate=False)
        time.sleep(0.5)
        ev('(lambda: setattr(m, "_drawer_auto_opened", True))()')
        ctl.call(cmd="ui", what="drawer", on=True, animate=False)
        time.sleep(0.4)
        st = ev("(m.drawer.is_open(), m._drawer_auto_opened)")
        log(f"   自动展开后: {st}")
        # 把鼠标挪到窗口**中间偏左**再等：自动收回的判据是
        # "指针不在抽屉上、也不在右边缘"，而测试机的指针停在哪儿是不确定的
        # （停在抽屉上就永远不会收回）—— 不固定指针位置，这条断言就是随机的。
        _pt = ev('m.mapToGlobal(__import__("PySide6.QtCore", fromlist=["QPoint"]).QPoint(200, 300)).toTuple()')
        if isinstance(_pt, list):
            _dpr = ctl.call(cmd="geom", widget="root")["geom"]["dpr"]
            wi.move_to(int(_pt[0] * _dpr), int(_pt[1] * _dpr))
        time.sleep(2.0)                      # 鼠标不在抽屉上, 应该收回
        st2 = ev("(m.drawer.is_open(), m._drawer_auto_opened)")
        log(f"   2 秒后:     {st2}")
        check("自动展开的抽屉在鼠标离开后自动收回", st2 == [False, False], str(st2))

        ctl.call(cmd="ui", what="drawer", on=False, animate=False)
        time.sleep(0.5)
        ev('(lambda: setattr(m, "_drawer_auto_opened", False))()')
        ctl.call(cmd="ui", what="drawer", on=True, animate=False)
        time.sleep(2.0)
        st3 = ev("m.drawer.is_open()")
        check("手动打开的抽屉保持常驻（不自动收回）", st3 is True, str(st3))
        ctl.call(cmd="ui", what="drawer", on=False, animate=False)

        # ---------------- 3. 齿轮图标 ----------------
        log("=== 3. 设置图标 ===")
        svg = ev('__import__("zhijian.ui.icons", fromlist=["x"])._STROKE["settings"]')
        log(f"   {str(svg)[:90]}…")
        check("settings 图标是齿轮（六角轮廓 + 内圈），不再是太阳",
              isinstance(svg, str) and "6.4 3.7" in svg and "circle" in svg, str(svg)[:60])
        check("不再有六条放射状'太阳光'", isinstance(svg, str) and "M12 3.5v2.2" not in svg)

        # ---------------- 4. 玻璃面缓存 ----------------
        log("=== 4. 玻璃面重绘耗时 ===")
        cost = ctl.call(cmd="bench", what="paint_cost")["result"]
        log(f"   {cost}")
        REPORT["paint_cost"] = cost
        check("胶囊单次重绘 < 1.5ms（缓存前是 3.08ms）",
              isinstance(cost.get("capsule"), (int, float)) and cost["capsule"] < 1.5,
              f"capsule={cost.get('capsule')}ms")

        # ---------------- 5. 布局：视频铺到底 / 抽屉不切控制栏 ----------------
        log("=== 5. 布局 ===")
        ctl.call(cmd="resize", w=1280, h=760)
        time.sleep(0.8)
        gw = ctl.call(cmd="geom", widget="root")["geom"]["local"]
        gv = ctl.call(cmd="geom", widget="video")["geom"]["in_window"]
        gc = ctl.call(cmd="geom", widget="capsule")["geom"]["in_window"]
        ctl.call(cmd="ui", what="drawer", on=True, animate=False)
        time.sleep(0.6)
        gd = ctl.call(cmd="geom", widget="drawer")["geom"]["in_window"]
        ctl.call(cmd="ui", what="drawer", on=False, animate=False)
        log(f"   窗口 {gw}  视频 {gv}  胶囊 {gc}  抽屉 {gd}")
        check("视频铺到窗口底部（视频底 ≈ 胶囊底）",
              abs((gv[1] + gv[3]) - (gc[1] + gc[3])) <= 4,
              f"video_bottom={gv[1]+gv[3]} capsule_bottom={gc[1]+gc[3]}")
        check("视频比胶囊顶边更低（说明控制条是浮在画面上）",
              (gv[1] + gv[3]) > gc[1], f"{gv[1]+gv[3]} > {gc[1]}")
        check("抽屉底边 = 胶囊顶边（不切断控制栏）",
              abs((gd[1] + gd[3]) - gc[1]) <= 2,
              f"drawer_bottom={gd[1]+gd[3]} capsule_top={gc[1]}")
        check("抽屉右边缘贴窗", abs((gd[0] + gd[2]) - gw[2]) <= 2,
              f"{gd[0]+gd[2]} vs {gw[2]}")

        # ---------------- 6. 全屏 / 最大化 ----------------
        log("=== 6. 全屏与最大化 ===")
        wi.force_foreground(wi.find_main_window("至简Player", min_w=400, min_h=300)["hwnd"])
        time.sleep(0.4)
        hwnd = wi.find_main_window("至简Player", min_w=400, min_h=300)["hwnd"]
        ctl.call(cmd="ui", what="fullscreen", on=True)
        time.sleep(2.0)
        r = wi.print_window_rect(hwnd)
        qt = ctl.call(cmd="state")["state"]["window"]["size"]
        gvf = ctl.call(cmd="geom", widget="video")["geom"]["in_window"]
        log(f"   全屏 native={r['window_size']} qt={qt} video={gvf}")
        check("全屏后 Qt 尺寸跟上了原生尺寸",
              abs(qt[0] * 1.5 - r["window_size"][0]) <= 6, f"qt={qt} native={r['window_size']}")
        check("全屏后视频铺满整个窗口",
              gvf[2] >= qt[0] - 6 and gvf[3] >= qt[1] - 6, f"video={gvf} qt={qt}")
        ctl.call(cmd="ui", what="fullscreen", on=False)
        time.sleep(1.5)

        # 最大化：直接调 Qt API（比点按钮稳）
        ev("m.showMaximized()")
        time.sleep(1.5)
        r2 = wi.print_window_rect(hwnd)
        qt2 = ctl.call(cmd="state")["state"]["window"]["size"]
        log(f"   最大化 native={r2['window_size']} qt={qt2}")
        check("最大化后 Qt 尺寸跟上了原生尺寸",
              abs(qt2[0] * 1.5 - r2["window_size"][0]) <= 6, f"qt={qt2} native={r2['window_size']}")
        ev("m.showNormal()")
        time.sleep(1.0)
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

    with open(os.path.join(ROOT, "logs", "uicheck.json"), "w", encoding="utf-8") as f:
        json.dump({"report": REPORT, "log": LOG, "fails": FAILS}, f,
                  ensure_ascii=False, indent=2)
    log(f"结论：{'全部通过' if not FAILS else str(len(FAILS)) + ' 项失败: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
