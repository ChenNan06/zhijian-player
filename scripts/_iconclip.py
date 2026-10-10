"""图标裁剪诊断：把 IconButton 内部真正画出来的像素放大 6 倍, 逐边检查是否被切。

判据：图标绘制区四边各留 1px 边距时, 边距像素必须全为背景色 —— 只要有一列/一行
画到了最外沿, 就说明这个图标在实际尺寸下被裁掉了（stroke 溢出 viewBox）。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from zhijian.ui import icons  # noqa: E402

# 实际用到的 (图标名, 尺寸, 描边) 组合 —— 从代码里抄的
COMBOS = [
    ("close", 16, 1.7), ("min", 16, 1.7), ("max", 15, 1.7), ("restore", 15, 1.7),
    ("play", 22, 1.7), ("pause", 22, 1.7), ("stop", 16, 1.7),
    ("prev", 20, 1.7), ("next", 20, 1.7), ("screenshot", 17, 1.7),
    ("volume_high", 17, 1.7), ("subtitle", 17, 1.7), ("fullscreen", 17, 1.7),
    ("settings", 17, 1.7), ("loop", 17, 1.7), ("order", 17, 1.7),
    ("pip", 17, 1.7), ("playlist", 16, 2.0), ("more", 18, 1.7),
    ("search", 15, 1.7), ("minus", 14, 1.7), ("plus", 14, 1.7),
    ("chevron_right", 15, 1.7), ("add", 15, 1.7), ("trash_all", 15, 1.7),
    ("sort", 15, 1.7), ("sliders", 18, 1.7), ("info", 18, 1.7),
    ("keyboard", 18, 1.7), ("palette", 18, 1.7), ("speaker", 18, 1.7),
    ("zoom_in", 16, 1.7), ("zoom_out", 16, 1.7), ("fit", 16, 1.7),
    ("one_to_one", 16, 1.7), ("rotate_left", 16, 1.7), ("rotate_right", 16, 1.7),
    ("wall", 16, 1.7), ("folder", 16, 1.7), ("next_image", 17, 1.7),
    ("prev_image", 16, 1.7), ("clock", 22, 1.8), ("forward", 22, 1.8),
    ("image", 18, 1.7), ("video", 18, 1.7),
]


def main():
    app = QApplication(sys.argv[:1])
    rows = []
    bad = []
    for name, size, stroke in COMBOS:
        for dpr in (1.0, 1.5, 2.0):
            pm = icons.render_pixmap(name, "#F3F5F9", size, dpr, stroke)
            img = pm.toImage()
            w, h = img.width(), img.height()
            if w < 3 or h < 3:
                bad.append((name, size, dpr, "size too small"))
                continue
            # 统计四条边上的"有内容"像素数（alpha > 8）
            def rowink(y):
                return sum(1 for x in range(w) if img.pixelColor(x, y).alpha() > 8)

            def colink(x):
                return sum(1 for y in range(h) if img.pixelColor(x, y).alpha() > 8)

            edges = {
                "上": rowink(0), "下": rowink(h - 1),
                "左": colink(0), "右": colink(w - 1),
            }
            worst = max(edges.values())
            if worst > 0:
                bad.append((name, size, dpr, f"边上有内容 {edges}"))
            rows.append((name, size, dpr, w, h, edges))
    print(f"检查 {len(COMBOS)} 个图标 × 3 种 DPR = {len(COMBOS)*3} 个渲染结果")
    if bad:
        print(f"\n⚠️ 有 {len(bad)} 个被裁到边：")
        for b in bad:
            print(f"   {b[0]:<14} size={b[1]:<3} dpr={b[2]}  {b[3]}")
    else:
        print("\n✅ 没有任何图标画到最外沿")

    # 出一张 6 倍放大的对照图（拿实际按钮尺寸）
    names = [c[0] for c in COMBOS[:24]]
    cell = 30 * 6
    cols = 8
    rowsn = (len(names) + cols - 1) // cols
    sheet = QPixmap(cols * cell, rowsn * (cell + 22))
    sheet.fill(QColor("#0F1116"))
    p = QPainter(sheet)
    p.setRenderHint(QPainter.SmoothPixmapTransform, False)
    from PySide6.QtGui import QFont

    f = QFont()
    f.setPixelSize(11)
    p.setFont(f)
    for i, n in enumerate(names):
        r, c = divmod(i, cols)
        x, y = c * cell, r * (cell + 22)
        pm = icons.render_pixmap(n, "#F3F5F9", 18, 2.0, 1.7)
        # 画一个 cell 大小的"按钮底", 再用最近邻放大 6 倍画图标
        p.fillRect(QRectF(x + 2, y + 2, cell - 4, cell - 4), QColor("#20242C"))
        scaled = pm.scaled(pm.width() * 6, pm.height() * 6,
                           Qt.KeepAspectRatio, Qt.FastTransformation)
        p.drawPixmap(int(x + (cell - scaled.width() / 2) / 2), int(y + (cell - scaled.height() / 2) / 2),
                     scaled)
        p.setPen(QColor("#8A93A3"))
        p.drawText(QRectF(x, y + cell, cell, 20), Qt.AlignCenter, n)
    p.end()
    out = os.path.join(ROOT, "logs", "icon_zoom6x.png")
    sheet.save(out, "PNG")
    print(f"6 倍放大对照图 -> {out}  ({sheet.width()}x{sheet.height()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
