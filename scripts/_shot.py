"""抓屏 + 裁剪工具（写自审脚本时反复要用）。

用法：
    python scripts\\_shot.py <输出png> [标题关键字] [client|window] [裁剪 x,y,w,h]
    python scripts\\_shot.py out.png 至简Player client 100,200,400,300
    python scripts\\_shot.py out.png --list        # 只列出可见窗口
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import _wininput as wi  # noqa: E402


def main():
    argv = sys.argv[1:]
    app = QApplication(sys.argv[:1])
    if not argv or argv[0] == "--list":
        for w in wi.list_windows(min_w=100, min_h=80):
            print(f"  hwnd={w['hwnd']:<12} pid={w['pid']:<7} {w['client_w']}x{w['client_h']} "
                  f"{w['title']!r}")
        return 0

    out = argv[0]
    title = argv[1] if len(argv) > 1 else "至简Player"
    mode = argv[2] if len(argv) > 2 else "client"
    crop = argv[3] if len(argv) > 3 else None

    wins = [w for w in wi.list_windows(min_w=200, min_h=150) if title in w["title"]]
    if not wins:
        print("找不到窗口。当前可见窗口：")
        for w in wi.list_windows(min_w=100, min_h=80):
            print("  ", w)
        return 2
    w = wins[0]
    info = wi.print_window_rect(w["hwnd"])
    print(f"hwnd={w['hwnd']} title={w['title']!r} window_size={info['window_size']} "
          f"client={info['client_size']} origin={info['client_origin']}")
    if mode == "client":
        wi.grab_window_client(w["hwnd"], out)
    else:
        wi.grab_window(w["hwnd"], out)

    if crop:
        from PySide6.QtGui import QImage

        x, y, cw, ch = (int(v) for v in crop.split(","))
        img = QImage(out)
        img.copy(x, y, cw, ch).save(out, "PNG")
        print(f"已裁剪 {crop}")
    print(f"截图 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
