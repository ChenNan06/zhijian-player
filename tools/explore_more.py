# -*- coding: utf-8 -*-
"""Final targeted measurements for the remaining screenshots."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gplib import *

def hs(label, prof, **kw):
    gs = groups(prof, **kw)
    print("%s: %d" % (label, len(gs)))
    for a, b in gs:
        print("   x %4d..%4d w=%3d | logical %7.1f..%7.1f w=%5.1f" % (a, b, b - a + 1, L(a), L(b + 1), L(b - a + 1)))
    return gs

def vs(label, prof, **kw):
    gs = groups(prof, **kw)
    print("%s: %d" % (label, len(gs)))
    for a, b in gs:
        print("   y %4d..%4d h=%3d | logical %7.1f..%7.1f h=%5.1f" % (a, b, b - a + 1, L(a), L(b + 1), L(b - a + 1)))
    return gs

print("############ 设置界面1.png (settings over main window) ############")
im, px = load("设置界面1.png")
W, H = im.size
print("size %dx%d" % (W, H))
print("[settings window bbox] row y=400 & col x=600")
prev = px[0, 400]
for x in range(1, W):
    p = px[x, 400]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 12:
        print("   x=%4d %s -> %s | logical %.1f" % (x, hexs(prev), hexs(p), L(x)))
    prev = p
print(" -- col x=600 --")
prev = px[600, 0]
for y in range(1, H):
    p = px[600, y]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 12:
        print("   y=%4d %s -> %s | logical %.1f" % (y, hexs(prev), hexs(p), L(y)))
    prev = p

print("\n[checkbox rows] band x=480..560 thr=90, y=150..600")
vs("   checkbox boxes", vband_profile(px, 150, 600, 470, 500, 90), mincount=1, gap=6)
print("[checkbox box horizontal] row y=186 x=460..540")
hs("   cb box", band_profile(px, 440, 560, 180, 195, 90), mincount=1, gap=6)
print("[checkbox label left edge] row y=186 x=470..700")
prev = px[470, 186]
for x in range(471, 700):
    p = px[x, 186]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 40:
        print("   x=%4d %s -> %s | logical %.1f" % (x, hexs(prev), hexs(p), L(x)))
    prev = p
print("[left option column] row y=230 x=250..420")
prev = px[250, 230]
for x in range(251, 430):
    p = px[x, 230]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 10:
        print("   x=%4d %s -> %s | logical %.1f" % (x, hexs(prev), hexs(p), L(x)))
    prev = p

print("\n############ 热键设置界面.png ############")
im, px = load("热键设置界面.png")
W, H = im.size
print("size %dx%d" % (W, H))
print("[sub-window bbox] row y=500")
prev = px[0, 500]
for x in range(1, W):
    p = px[x, 500]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 12:
        print("   x=%4d %s -> %s | logical %.1f" % (x, hexs(prev), hexs(p), L(x)))
    prev = p
print("[sub-window bbox] col x=400")
prev = px[400, 0]
for y in range(1, H):
    p = px[400, y]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 12:
        print("   y=%4d %s -> %s | logical %.1f" % (y, hexs(prev), hexs(p), L(y)))
    prev = p
print("[hotkey list rows] band x=340..600 thr=90, y=180..700")
vs("   rows", vband_profile(px, 180, 700, 340, 600, 90), mincount=1, gap=6)
print("[list left/right] row y=269 x=280..1030")
hs("   header/row cols", band_profile(px, 280, 1030, 218, 240, 80), mincount=1, gap=14)

print("\n############ 播放界面挪动鼠标.png (drawer handle + top toolbar) ############")
im, px = load("播放界面挪动鼠标.png")
W, H = im.size
print("size %dx%d" % (W, H))
print("[right-edge drawer handle] col x=1290 y=54..800")
prev = px[1290, 54]
for y in range(55, 800):
    p = px[1290, y]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 14:
        print("   y=%4d %s -> %s | logical %.1f" % (y, hexs(prev), hexs(p), L(y)))
    prev = p
print("[handle horizontal] row y=400 x=1230..1299")
prev = px[1230, 400]
for x in range(1231, 1299):
    p = px[x, 400]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 14:
        print("   x=%4d %s -> %s | logical %.1f" % (x, hexs(prev), hexs(p), L(x)))
    prev = p
print("[top toolbar row] band x=800..1299 thr=90 y=60..100")
hs("   top toolbar", band_profile(px, 800, 1299, 60, 100, 90), mincount=1, gap=12)
print("[progress bar played] row y=796 x=0..1299 detail")
prev = px[0, 796]
for x in range(1, W):
    p = px[x, 796]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 45:
        print("   x=%4d %s -> %s | logical %.1f" % (x, hexs(prev), hexs(p), L(x)))
    prev = p
print("[progress bar vertical] col x=600 y=780..815")
for y in range(780, 816):
    print("   y=%3d %s" % (y, hexs(px[600, y])))
