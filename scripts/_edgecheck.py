"""诊断：窗口矩形 vs 屏幕真实像素的边界对齐（排查"抓屏整体偏移"）。

从窗口中心向四条边扫描"窗口底色"，得出真实可见边界与 GetWindowRect 的差值。
用于判断抓屏时该内缩多少像素（150% 缩放下 DWM 会有 1~3 物理像素的不可见边框）。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import _wininput as wi  # noqa: E402


def main():
    app = QApplication(sys.argv[:1])
    title = sys.argv[1] if len(sys.argv) > 1 else "至简Player"
    scr = QGuiApplication.primaryScreen()
    img = scr.grabWindow(0).toImage()
    print(f"屏幕 {scr.geometry()} dpr={scr.devicePixelRatio()} 抓图 {img.width()}x{img.height()}")
    wins = [x for x in wi.list_windows(min_w=400, min_h=300) if title in x["title"]]
    if not wins:
        print("找不到窗口:", title)
        return 2
    w = wins[0]
    x0, y0, x1, y1 = w["rect"]
    print(f"窗口 hwnd={w['hwnd']} rect=({x0},{y0},{x1},{y1}) size={x1-x0}x{y1-y0} "
          f"client={w['client_w']}x{w['client_h']}")

    # 以窗口正中心为基准色，向四边扫描"什么时候不再是窗口内容"
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    inside = img.pixelColor(cx, y0 + 6).name()      # 顶部往里 6px，一定是我们的背景

    def first_diff(seq, ref):
        for i, (x, y) in enumerate(seq):
            if img.pixelColor(x, y).name() != ref:
                return i, (x, y), img.pixelColor(x, y).name()
        return None, None, None

    # 从左边界往里扫（找到第一个等于 inside 的像素 = 内容真正开始处）
    res = {}
    for name, seq in (
        ("left", [(x, cy) for x in range(max(0, x0 - 12), x0 + 24)]),
        ("right", [(x, cy) for x in range(min(img.width() - 1, x1 + 12), x1 - 24, -1)]),
        ("top", [(cx, y) for y in range(max(0, y0 - 12), y0 + 24)]),
        ("bottom", [(cx, y) for y in range(min(img.height() - 1, y1 + 12), y1 - 24, -1)]),
    ):
        hit = None
        for (x, y) in seq:
            if img.pixelColor(x, y).name() == inside:
                hit = (x, y)
                break
        res[name] = hit
        print(f"  {name:>6}: 首个窗口底色像素 = {hit}")

    if res["left"] and res["right"]:
        print(f"  => 实测内容宽 {res['right'][0] - res['left'][0] + 1}  "
              f"声明 {x1 - x0}")
    if res["top"] and res["bottom"]:
        print(f"  => 实测内容高 {res['bottom'][1] - res['top'][1] + 1}  "
              f"声明 {y1 - y0}")
    print(f"  => 抓屏应内缩：left={res['left'][0]-x0 if res['left'] else '?'} "
          f"top={res['top'][1]-y0 if res['top'] else '?'} "
          f"right={x1-res['right'][0] if res['right'] else '?'} "
          f"bottom={y1-res['bottom'][1] if res['bottom'] else '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
