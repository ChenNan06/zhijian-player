"""把图标集渲染成一张对照表 PNG（排查"图标画不出来/画不全"）。"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from zhijian.ui import icons  # noqa: E402


def main():
    app = QApplication(sys.argv[:1])
    names = sorted(icons._STROKE.keys())
    cell, cols = 64, 12
    rows = (len(names) + cols - 1) // cols
    pm = QPixmap(cols * cell, rows * (cell + 16))
    pm.fill(QColor("#0F1116"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    f = QFont()
    f.setPixelSize(9)
    p.setFont(f)
    for i, n in enumerate(names):
        r, c = divmod(i, cols)
        x, y = c * cell, r * (cell + 16)
        svg = icons.icon_svg(n, "#F3F5F9", 1.7)
        from PySide6.QtCore import QByteArray

        rend = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        print(f"{n:<16} valid={rend.isValid()}")
        rend.render(p, QRectF(x + 8, y + 8, 48, 48))
        p.setPen(QColor("#8A93A3"))
        p.drawText(QRectF(x, y + 56, cell, 14), Qt.AlignCenter, n)
    p.end()
    out = os.path.join(ROOT, "logs", "iconsheet.png")
    pm.save(out, "PNG")
    print("->", out, pm.width(), pm.height())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
