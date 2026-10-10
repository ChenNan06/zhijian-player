# -*- coding: utf-8 -*-
"""Sample assets that live in the base skin/ (not skinDefault)."""
import os
from PIL import Image
BASE = r"E:\IQIYI Video\GeePlayer\5.4.1.5408\skin"

def show(rel, pts=None, rows=False):
    p = os.path.join(BASE, rel.replace("/", os.sep))
    if not os.path.exists(p):
        print("%-52s MISSING" % rel); return
    im = Image.open(p).convert("RGBA")
    W, H = im.size
    print("%-52s %3dx%-3d" % (rel, W, H))
    if rows:
        for y in range(H):
            print("    y=%2d %s" % (y, " ".join("#%02X%02X%02X%02X" % im.getpixel((x, y))
                                                for x in range(0, W, max(1, W // 6)))))
    if pts:
        for xy in pts:
            if 0 <= xy[0] < W and 0 <= xy[1] < H:
                print("    %-10s #%02X%02X%02X%02X" % (str(xy), *im.getpixel(xy)))

print("== play_slider.png (progress played strip) ==")
show("player/miniplayer/ctrl/play_slider.png", [(0, 1), (100, 1), (256, 1), (400, 1), (511, 1),
                                                (0, 0), (256, 0)])
im = Image.open(os.path.join(BASE, r"player\miniplayer\ctrl\play_slider.png")).convert("RGBA")
print("    size", im.size)
for y in range(im.height):
    print("    y=%d  x0=%s x256=%s x511=%s" % (y, im.getpixel((0, y)), im.getpixel((256, y)), im.getpixel((511, y))))

print("\n== slider_bk.png ==")
show("player/miniplayer/ctrl/slider_bk.png", rows=True)

print("\n== slider_line.png ==")
show("player/miniplayer/ctrl/slider_line.png", rows=True)

print("\n== playslider_ball.png ==")
show("player/miniplayer/ctrl/playslider_ball.png", [(7, 7), (0, 7), (7, 0), (3, 3), (14, 14)])

print("\n== middle/top_bk.png ==")
show("player/miniplayer/middle/top_bk.png", [(0, 0), (0, 20), (0, 39)])

print("\n== public/ctrl/minsize.png / mainclose.png frame colours ==")
for rel in ["public/ctrl/minsize.png", "public/ctrl/mainclose.png", "frame/maxsize.png"]:
    p = os.path.join(BASE, "skinDefault", "skinplan", rel.replace("/", os.sep))
    im = Image.open(p).convert("RGBA")
    hi = max(((im.getpixel((x, y)), (x, y)) for y in range(im.height) for x in range(im.width)),
             key=lambda t: t[0][3])
    print("    %-40s %s  max-alpha px %s" % (rel, im.size, hi))
