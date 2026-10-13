"""给安装向导拍几张真实屏幕截图（只为交付留证，不实际安装）。

流程：启动 Setup.exe → 抓欢迎页 → 真实点击「下一步」→ 抓路径页 → 关闭（不安装）。
"""

from __future__ import annotations

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

OUT = os.path.join(ROOT, "docs", "04-验收证据")


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    setup = os.path.join(ROOT, "output", "至简Player-Setup-1.0.24.exe")
    if not os.path.isfile(setup):
        print("找不到安装包")
        return 2
    p = subprocess.Popen([setup], cwd=ROOT)
    win = None
    for _ in range(160):
        wins = [w for w in wi.list_windows(min_w=400, min_h=300)
                if "安装" in w["title"] or "至简" in w["title"]]
        if wins:
            win = wins[0]
            break
        time.sleep(0.5)
    if not win:
        print("安装向导窗口没出现（onefile 解压可能较慢），当前可见窗口：")
        for w in wi.list_windows(min_w=200, min_h=150)[:12]:
            print("  ", w["title"], w["client_w"], w["client_h"])
        p.kill()
        return 2
    hwnd = win["hwnd"]
    wi.force_foreground(hwnd)
    time.sleep(1.2)
    os.makedirs(OUT, exist_ok=True)
    wi.grab_window_client(hwnd, os.path.join(OUT, "13-安装向导-欢迎.png"))
    print("已抓欢迎页", win["title"], win["client_w"], "x", win["client_h"])

    # 真实点击「下一步」：按钮在右下角，按客户区尺寸估算
    ox, oy = wi.client_origin(hwnd)
    bw = win["client_w"]
    bh = win["client_h"]
    dpr = 1.0
    try:
        from PySide6.QtGui import QGuiApplication

        dpr = QGuiApplication.primaryScreen().devicePixelRatio() or 1.0
    except Exception:
        pass
    # 「下一步」按钮：右侧第二排，距右 ~46 逻辑、距底 ~40 逻辑
    x = ox + int((bw / dpr - 46) * dpr)
    y = oy + int((bh / dpr - 40) * dpr)
    wi.click(x, y)
    time.sleep(1.0)
    wi.grab_window_client(hwnd, os.path.join(OUT, "14-安装向导-选择位置.png"))
    print(f"已抓第二页（点击 {x},{y}）")
    time.sleep(0.5)
    wi.close_window(hwnd)
    time.sleep(1.5)
    try:
        p.wait(timeout=8)
    except Exception:
        p.kill()
    print("安装向导已关闭, 未执行安装")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
