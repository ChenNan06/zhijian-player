# -*- coding: utf-8 -*-
"""Sample the actual colours of the key background/solid PNG assets (ground truth)."""
import os
from PIL import Image

SKIN = r"E:\IQIYI Video\GeePlayer\5.4.1.5408\skin\skinDefault\skinplan"
CASES = [
    ("frame/root_bk.png", None),          # window background
    ("frame/topbk.png", None),            # title bar
    ("frame/list_top_line.png", None),    # 1px separator under title bar
    ("frame/splite_line.png", None),
    ("frame/scroll.png", None),
    ("list/list_bk.png", None),           # playlist drawer background
    ("list/splite_line.png", None),       # drawer left splitter
    ("list/itembk.png", None),            # list item hover/fore background
    ("list/searchline.png", None),
    ("list/search.png", None),
    ("config/settingbk.png", None),       # settings window background
    ("config/option_sel_bk.png", None),   # settings left category item
    ("config/subwindowbk.png", None),
    ("public/ctrl/okbtn.png", None),
    ("public/ctrl/cancelbtn.png", None),
    ("player/miniplayer/ctrl/play.png", None),
    ("player/miniplayer/ctrl/play_slider.png", None),
    ("player/miniplayer/ctrl/slider_bk.png", None),
    ("player/miniplayer/ctrl/slider_line.png", None),
    ("player/miniplayer/ctrl/playslider_ball.png", None),
    ("player/miniplayer/ctrl/volume_now.png", None),
    ("player/miniplayer/ctrl/speed_btn.png", None),
    ("player/miniplayer/middle/top_bk.png", None),
]
print("%-46s %-9s %s" % ("asset", "size", "sampled colours (x,y)=#RRGGBBAA"))
for rel, _ in CASES:
    p = os.path.join(SKIN, rel.replace("/", os.sep))
    if not os.path.exists(p):
        print("%-46s MISSING" % rel); continue
    im = Image.open(p).convert("RGBA")
    W, H = im.size
    pts = [(W // 2, H // 2), (0, 0), (W - 1, H - 1)]
    if W >= 8 and H >= 8:
        pts += [(1, 1), (W // 4, H // 4), (3 * W // 4, H // 4)]
    pts = [p for p in dict.fromkeys(pts) if 0 <= p[0] < W and 0 <= p[1] < H]
    s = " ".join("(%d,%d)=#%02X%02X%02X%02X" % (x, y, *im.getpixel((x, y))) for x, y in pts)
    print("%-46s %3dx%-3d %s" % (rel, W, H, s))

# progress-bar frames: play_slider.png is 512x3 strips
print("\n--- play_slider.png strip colours (y=1) ---")
im = Image.open(os.path.join(SKIN, "player/miniplayer/ctrl/play_slider.png")).convert("RGBA")
print("   size", im.size)
for x in [0, 64, 128, 200, 300, 400, 500, 510]:
    print("   x=%3d #%02X%02X%02X%02X" % (x, *im.getpixel((x, 1))))

print("\n--- volume_now.png ---")
im = Image.open(os.path.join(SKIN, "player/miniplayer/ctrl/volume_now.png")).convert("RGBA")
print("   size", im.size)
for xy in [(5, 4), (30, 4), (60, 4), (5, 8), (30, 8), (0, 8), (35, 10)]:
    print("   %s #%02X%02X%02X%02X" % (xy, *im.getpixel(xy)))

print("\n--- slider_bk.png (1x16) ---")
im = Image.open(os.path.join(SKIN, "player/miniplayer/ctrl/slider_bk.png")).convert("RGBA")
for y in range(im.height):
    print("   y=%2d #%02X%02X%02X%02X" % (y, *im.getpixel((0, y))))

print("\n--- playslider_ball.png (15x15) centre/edges ---")
im = Image.open(os.path.join(SKIN, "player/miniplayer/ctrl/playslider_ball.png")).convert("RGBA")
for xy in [(7, 7), (0, 7), (7, 0), (3, 3), (11, 11)]:
    print("   %s #%02X%02X%02X%02X" % (xy, *im.getpixel(xy)))

print("\n--- speed_btn.png frame0 (51x25) ---")
im = Image.open(os.path.join(SKIN, "player/miniplayer/ctrl/speed_btn.png")).convert("RGBA")
for xy in [(7, 12), (25, 12), (43, 12), (25, 3), (25, 21), (25, 4), (25, 8)]:
    print("   %s #%02X%02X%02X%02X" % (xy, *im.getpixel(xy)))
