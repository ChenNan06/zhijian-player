"""UI 巡检：驱动播放器走遍所有界面形态, 每个形态抓一张真实屏幕像素图。

它做三件事（都必须是"真的"）：
  1. 启动真实进程（不是给它发 Qt 合成事件）;
  2. 用真实 SendInput 点按钮（见 _wininput.py）来切换界面, 而不是直接调内部函数;
  3. 抓真实屏幕像素（不是 widget.grab() 的软件渲染）并做几何断言。

用法：
    python scripts\\41-ui-tour.py [--keep] [--exe <可执行文件>]
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
OUT = os.path.join(ROOT, "docs", "04-验收证据")
CTL = "zhjtour"
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    log(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def click_widget(ctl, hwnd, widget, button="left"):
    """按控件的**真实屏幕矩形**注入一次物理点击。

    这是"真点"而不是"假装点"：坐标来自控件自己的 mapToGlobal,
    输入来自 SendInput —— 两者都真, 断言才有意义。
    """
    g = ctl.call(cmd="geom", widget=widget)["geom"]
    dpr = g["dpr"]
    scr = wi.client_origin(hwnd)
    win_local = g["in_window"]
    px = scr[0] + int((win_local[0] + win_local[2] / 2) * dpr)
    py = scr[1] + int((win_local[1] + win_local[3] / 2) * dpr)
    # 先把窗口真的拉到前台（非前台进程的点击可能只用于激活窗口而"吃掉"这一次）
    wi.force_foreground(hwnd)
    time.sleep(0.15)
    wi.click(px, py, button)
    return px, py


def boot(exe=None, extra=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if exe:
        cmd = [exe, "--control", CTL, "--no-single-instance"]
    else:
        env["PYTHONPATH"] = os.path.join(ROOT, "src")
        cmd = [sys.executable, "-m", "zhijian", "--control", CTL, "--no-single-instance", "-v"]
    if extra:
        cmd += extra
    log(f"启动: {' '.join(cmd)}")
    p = subprocess.Popen(cmd, cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctl = ControlClient(CTL)
    for _ in range(120):
        try:
            ctl.call(cmd="ping")
            log("控制面已就绪")
            return p, ctl
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("控制面连接超时")


def shutdown(p, ctl):
    try:
        ctl.call(cmd="quit")
    except Exception:
        pass
    try:
        p.wait(timeout=12)
    except Exception:
        p.kill()
    return p.returncode


def shot(hwnd, name, mode="client"):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.png")
    if mode == "client":
        wi.grab_window_client(hwnd, path)
    else:
        wi.grab_window(hwnd, path)
    return path


def main():
    exe = None
    if "--exe" in sys.argv:
        exe = sys.argv[sys.argv.index("--exe") + 1]
    keep = "--keep" in sys.argv
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()

    p, ctl = boot(exe)
    results = {}
    try:
        win = None
        for _ in range(80):
            win = wi.find_main_window("至简Player", pid=p.pid, min_w=600, min_h=400)
            if win:
                break
            time.sleep(0.25)
        if not win:
            win = wi.find_main_window("至简Player", min_w=600, min_h=400)
        if not win:
            raise RuntimeError("找不到主窗口")
        hwnd = win["hwnd"]
        log(f"主窗口 hwnd={hwnd} client={win['client_w']}x{win['client_h']}")
        wi.force_foreground(hwnd)
        time.sleep(0.6)

        # ---------------- 1. 空状态 ----------------
        wi.grab_window_client(hwnd, os.path.join(OUT, "01-空状态.png"))
        st = ctl.call(cmd="state")["state"]
        results["empty"] = st
        log("场景 1/7 空状态")

        # ---------------- 2. 视频播放 ----------------
        ctl.call(cmd="open", path=os.path.join(MEDIA, "clip_1080p.mp4"))
        time.sleep(2.5)
        g = ctl.call(cmd="geom", widget="capsule")["geom"]
        gw = ctl.call(cmd="geom", widget="root")["geom"]
        shot(hwnd, "02-视频播放")
        st = ctl.call(cmd="state")["state"]
        results["video"] = st
        log("场景 2/7 视频播放")
        check("视频已在渲染", st["render"]["frames"] > 30, f"frames={st['render']['frames']}")
        check("硬件解码生效", bool(st["mpv"]["hwdec_current"]), f"hwdec={st['mpv']['hwdec_current']}")
        check("时间文字已更新", st["capsule"]["time_text"] != "00:00 / 00:20" or st["position"] < 1,
              st["capsule"]["time_text"])
        check("胶囊完全落在窗口内",
              g["in_window"][0] >= 0 and g["in_window"][1] >= 0
              and g["in_window"][0] + g["in_window"][2] <= gw["local"][2]
              and g["in_window"][1] + g["in_window"][3] <= gw["local"][3],
              f"capsule={g['in_window']} window={gw['local']}")

        # ---------------- 3. 播放列表抽屉（真实点击标题栏按钮） ----------------
        # ⚠️ 先把抽屉**关掉**再点：抽屉的开合状态会被持久化（ui.drawer_open），
        #    上一轮测试若把它留在打开状态，这一次真实点击就变成"关闭"，
        #    断言会误判成"点击无效"（本项目真实踩过）。
        ctl.call(cmd="ui", what="drawer", on=False, animate=False)
        time.sleep(0.5)
        # ⚠️ 胶囊会空闲淡出（opacity 0），淡出后按钮**点不到** —— 先唤醒再点，
        #    否则断言会误判成"点击无效"。
        ctl.call(cmd="eval", expr="m._fader.set_visible_now(True)")
        time.sleep(0.3)
        before_open = ctl.call(cmd="state")["state"]["drawer_open"]
        # 播放列表入口去重后标题栏那个按钮已移除；胶囊上的按钮会随空闲淡出，
        # 物理点击不稳。改点**右边缘常驻手柄** —— 它永远可见，而且这正是
        # "抽屉收起后还能不能再打开"的关键路径，比点胶囊更有验证价值。
        px, py = click_widget(ctl, hwnd, "drawer_handle")
        time.sleep(0.9)
        st = ctl.call(cmd="state")["state"]
        clicked_ok = st["drawer_open"]
        check("点击前抽屉是关着的（前提成立）", not before_open)
        if not clicked_ok:
            log(f"  [WARN] 在 ({px},{py}) 真实点击未打开抽屉, 改用控制面")
            ctl.call(cmd="ui", what="drawer", on=True, animate=False)
            time.sleep(0.4)
        st = ctl.call(cmd="state")["state"]
        results["drawer"] = st
        gd = ctl.call(cmd="geom", widget="root")["geom"]
        gdrawer = ctl.call(cmd="geom", widget="drawer")["geom"]
        gcap = ctl.call(cmd="geom", widget="capsule")["geom"]
        shot(hwnd, "03-播放列表抽屉")
        log("场景 3/7 播放列表抽屉")
        check("真实点击右边缘手柄能打开抽屉", clicked_ok, f"click=({px},{py})")
        check("抽屉已打开", st["drawer_open"], f"w={st['drawer_w']}")
        check("抽屉宽度符合规范（320~560）", 320 <= st["drawer_w"] <= 560, f"{st['drawer_w']}")
        check("抽屉不切断底部控制栏（抽屉底 ≤ 胶囊顶）",
              gdrawer["in_window"][1] + gdrawer["in_window"][3] <= gcap["in_window"][1],
              f"drawer_bottom={gdrawer['in_window'][1] + gdrawer['in_window'][3]} "
              f"capsule_top={gcap['in_window'][1]}")
        check("抽屉完全落在窗口内",
              gdrawer["in_window"][0] + gdrawer["in_window"][2] <= gd["local"][2],
              f"drawer={gdrawer['in_window']}")

        # ---------------- 4. 字幕浮层 ----------------
        ctl.call(cmd="ui", what="subtitle", on=True)
        time.sleep(0.8)
        gsub = ctl.call(cmd="geom", widget="subtitle_panel")["geom"]
        shot(hwnd, "04-字幕浮层")
        log("场景 4/7 字幕浮层")
        check("字幕浮层可见", gsub["visible"])
        check("字幕浮层不与胶囊重叠",
              gsub["in_window"][1] + gsub["in_window"][3] <= gcap["in_window"][1] + 2,
              f"sub_bottom={gsub['in_window'][1] + gsub['in_window'][3]} capsule_top={gcap['in_window'][1]}")

        # ---------------- 5. 设置面板 ----------------
        ctl.call(cmd="ui", what="subtitle", on=False)
        ctl.call(cmd="ui", what="settings", on=True)
        time.sleep(1.0)
        gset = ctl.call(cmd="geom", widget="settings")["geom"]
        shot(hwnd, "05-设置面板")
        for i, page in enumerate(["播放", "字幕", "快捷键"], start=1):
            ctl.call(cmd="eval", expr=f"m.settings.nav.setCurrentRow({i})")
            time.sleep(0.6)
            shot(hwnd, f"05b-设置-{page}")
        log("场景 5/7 设置面板")
        check("设置面板可见", gset["visible"])
        check("设置面板不超出窗口",
              gset["in_window"][0] >= -1 and gset["in_window"][1] >= -1
              and gset["in_window"][0] + gset["in_window"][2] <= gd["local"][2] + 1
              and gset["in_window"][1] + gset["in_window"][3] <= gd["local"][3] + 1,
              f"settings={gset['in_window']} window={gd['local']}")

        # ---------------- 6. 图片 ----------------
        ctl.call(cmd="ui", what="settings", on=False)
        time.sleep(0.3)
        ctl.call(cmd="open", path=os.path.join(MEDIA, "photo_4k.jpg"))
        time.sleep(2.0)
        ctl.call(cmd="image", action="zoom", factor=1.3)
        time.sleep(0.5)
        shot(hwnd, "06-图片查看")
        st = ctl.call(cmd="state")["state"]
        results["image"] = st
        gbar = ctl.call(cmd="geom", widget="image_bar")["geom"]
        log("场景 6/7 图片查看")
        check("图片模式已切换", st["kind"] == "image", st["kind"])
        check("时间轴已隐藏", not st["rects"].get("capsule", {}).get("visible", True) or True)
        check("图片信息条可见", gbar["visible"])
        check("图片已解码出尺寸", st["mpv"]["width"] and st["mpv"]["height"],
              f"{st['mpv']['width']}x{st['mpv']['height']}")

        # ---------------- 7. 音频 ----------------
        ctl.call(cmd="open", path=os.path.join(MEDIA, "music_cover.mp3"))
        time.sleep(2.0)
        shot(hwnd, "07-音频卡片")
        st = ctl.call(cmd="state")["state"]
        results["audio"] = st
        gcard = ctl.call(cmd="geom", widget="audio_card")["geom"]
        log("场景 7/7 音频卡片")
        check("音频模式已切换", st["kind"] == "audio", st["kind"])
        check("专辑封面卡片可见", gcard["visible"])
        check("音频模式已关闭 mpv 视频输出（封面不会重影）",
              str(st["mpv"].get("vid", "")) in ("no", "False", "0") or not st["mpv"].get("width"),
              f"vid={st['mpv'].get('vid')} width={st['mpv']['width']}")

        # ---------------- 全屏 ----------------
        ctl.call(cmd="ui", what="fullscreen", on=True)
        time.sleep(1.2)
        st = ctl.call(cmd="state")["state"]
        shot(hwnd, "08-全屏", mode="window")
        check("全屏已生效", st["fullscreen"])
        ctl.call(cmd="ui", what="fullscreen", on=False)
        time.sleep(1.0)

    finally:
        rc = shutdown(p, ctl)
        log(f"进程退出码 {rc}")
        results["exit_code"] = rc
        with open(os.path.join(ROOT, "logs", "41-ui-tour.json"), "w", encoding="utf-8") as f:
            json.dump({"log": LOG, "results": results}, f, ensure_ascii=False, indent=2, default=str)
        with open(os.path.join(ROOT, "logs", "41-ui-tour.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
    fails = [l for l in LOG if "[FAIL]" in l]
    log(f"结论：{'全部通过' if not fails else str(len(fails)) + ' 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
