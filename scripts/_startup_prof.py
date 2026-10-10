r"""启动耗时剖析：从进程创建到主窗口真正可交互，分阶段打点。

用法：python scripts/_startup_prof.py [--runs 3] [--packaged <exe路径>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

LOG = os.path.join(os.environ.get("APPDATA", ""), "ZhiJian", "ZhiJianPlayer", "zhijian.log")


def kill_all():
    """清掉上一轮残留的播放器进程。

    ⚠️ 绝不能直接 `taskkill /F /IM python.exe` —— 那会把**本剖析脚本自己**也杀掉
    （本脚本就是 python.exe），表现为"跑起来一点输出都没有、退出码 1"。
    必须用 PID 过滤器把自己排除掉。
    """
    me = os.getpid()
    for exe in ("python.exe", "pythonw.exe", "至简Player.exe"):
        subprocess.run(["taskkill", "/F", "/IM", exe, "/FI", f"PID ne {me}"],
                       capture_output=True, creationflags=0x08000000)
    time.sleep(2.0)


def one_run(app, packaged: str | None, tag: str):
    kill_all()
    try:
        os.remove(LOG)
    except OSError:
        pass
    if packaged:
        cmd = [packaged, "--no-single-instance"]
        cwd = os.path.dirname(packaged)
    else:
        cmd = [sys.executable, "-m", "zhijian", "--no-single-instance"]
        cwd = ROOT
        os.environ["PYTHONPATH"] = os.path.join(ROOT, "src")
    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, cwd=cwd,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 外部判据：EnumWindows 能找到窗口 **且** 它已经画出了内容
    t_win = t_ready = None
    deadline = t0 + 40
    while time.perf_counter() < deadline:
        if p.poll() is not None:
            break
        w = wi.find_main_window("至简Player", pid=p.pid, min_w=400, min_h=300)
        if w:
            if t_win is None:
                t_win = time.perf_counter() - t0
            # 再抓一次像素，确认真有内容（不是空白壳）
            try:
                img = wi.grab_window_client(w["hwnd"], os.path.join(ROOT, "logs", "_probe.png"))
                if img and img.width() > 100:
                    t_ready = time.perf_counter() - t0
                    break
            except Exception:
                pass
        time.sleep(0.02)

    marks = []
    for _ in range(30):
        try:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                txt = f.read()
            marks = re.findall(r"\[启动\] (.+?)\s+(\d+) ms", txt)
            if marks:
                break
        except OSError:
            pass
        time.sleep(0.2)

    try:
        p.terminate()
        p.wait(timeout=8)
    except Exception:
        p.kill()
    return {
        "tag": tag,
        "window_ms": round((t_win or 0) * 1000),
        "ready_ms": round((t_ready or 0) * 1000),
        "phases": [(k, int(v)) for k, v in marks],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--packaged", default="")
    a = ap.parse_args()
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()

    results = []
    for i in range(a.runs):
        r = one_run(app, a.packaged or None, f"run{i+1}")
        results.append(r)
        print(f"  第 {i+1} 次: 窗口 {r['window_ms']}ms / 首帧内容 {r['ready_ms']}ms")
        for k, v in r["phases"]:
            print(f"      {k:<12} {v:>6} ms")
    if results:
        print(f"\n  首帧内容中位: {statistics.median([r['ready_ms'] for r in results]):.0f} ms")
    with open(os.path.join(ROOT, "logs", "startup-profile.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    kill_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
