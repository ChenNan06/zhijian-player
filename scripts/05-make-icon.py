"""生成应用图标 assets\\icon.ico（多尺寸）+ assets\\icon.png。

设计：微冷色深底圆角方块 + 冷青强调色的"播放 + 画框"复合标记,
与程序内控制条/强调色同一套设计 token。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtCore import QBuffer, QByteArray, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from zhijian.ui.theme import P, qt_color  # noqa: E402

SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size / 256.0

    # 圆角底（微冷色, 带一点点自上而下的提亮 —— 但不是渐变配色, 只是高光）
    r = QRectF(8 * s, 8 * s, 240 * s, 240 * s)
    path = QPainterPath()
    path.addRoundedRect(r, 56 * s, 56 * s)
    p.fillPath(path, qt_color(P["bg_base"]))
    pen = QPen(qt_color(P["hairline_strong"]))
    pen.setWidthF(max(1.0, 2.0 * s))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)

    # 顶部内发光
    glow = QPainterPath()
    glow.addRoundedRect(r.adjusted(3 * s, 3 * s, -3 * s, -3 * s), 52 * s, 52 * s)
    p.setClipPath(glow)
    p.fillRect(QRectF(r.left(), r.top(), r.width(), r.height() * 0.45),
               QColor(255, 255, 255, 16))
    p.setClipping(False)

    # 画框（暗示"看图/看视频"）
    fr = QRectF(52 * s, 66 * s, 152 * s, 112 * s)
    pen = QPen(qt_color(P["text"]))
    pen.setWidthF(max(1.2, 11 * s))
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(fr, 16 * s, 16 * s)

    # 播放三角（强调色）
    tri = QPainterPath()
    cx, cy = 128 * s, 122 * s
    w, h = 44 * s, 54 * s
    tri.moveTo(cx - w * 0.42, cy - h * 0.58)
    tri.lineTo(cx + w * 0.62, cy)
    tri.lineTo(cx - w * 0.42, cy + h * 0.58)
    tri.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(qt_color(P["accent"]))
    p.drawPath(tri)

    # 底部一条细强调线（呼应进度条）
    p.setBrush(qt_color(P["accent"]))
    p.drawRoundedRect(QRectF(52 * s, 196 * s, 152 * s, 9 * s), 4.5 * s, 4.5 * s)
    p.end()
    return pm


def main():
    app = QApplication(sys.argv[:1])
    assets = os.path.join(ROOT, "assets")
    os.makedirs(assets, exist_ok=True)
    pms = [(sz, draw(sz)) for sz in SIZES]
    # .ico：把多个尺寸塞进一个文件
    ico = os.path.join(assets, "icon.ico")
    pms[-1][1].save(ico, "ICO")
    # Qt 的 ICO 写入器不一定带全部尺寸, 用 Pillow 重新合成一份最稳的
    try:
        from PIL import Image

        frames = []
        for sz, pm in pms:
            buf = QBuffer()
            buf.open(QBuffer.WriteOnly)
            pm.save(buf, "PNG")
            buf.close()
            import io

            frames.append(Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA"))
        frames[-1].save(ico, format="ICO",
                        sizes=[(f.width, f.height) for f in frames])
        print(f"icon.ico 已写入（{len(frames)} 个尺寸: {SIZES}）")
    except ImportError:
        print("没有 Pillow, 只写入单尺寸 ico")
    pms[-1][1].save(os.path.join(assets, "icon.png"), "PNG")
    pms[6][1].save(os.path.join(ROOT, "logs", "icon_preview.png"), "PNG")
    print("assets\\icon.ico / assets\\icon.png 已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
