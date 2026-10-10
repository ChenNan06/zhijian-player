# -*- coding: utf-8 -*-
"""Home-page grid (tiles) precise measurement."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gplib import *

im, px = load("首页无播放界面无列表.png")
W, H = im.size
print("首页无播放界面无列表 %dx%d logical %.1f x %.1f" % (W, H, L(W), L(H)))

def edges_row(y, x0, x1, tol=6):
    out = []
    prev = px[x0, y]
    for x in range(x0 + 1, x1):
        p = px[x, y]
        if max(abs(p[i] - prev[i]) for i in range(3)) > tol:
            out.append((x, L(x), hexs(prev), hexs(p)))
        prev = p
    return out

def edges_col(x, y0, y1, tol=6):
    out = []
    prev = px[x, y0]
    for y in range(y0 + 1, y1):
        p = px[x, y]
        if max(abs(p[i] - prev[i]) for i in range(3)) > tol:
            out.append((y, L(y), hexs(prev), hexs(p)))
        prev = p
    return out

print("\n[tile row y=320 (through first tile)] x=140..1200")
for x, lx, a, b in edges_row(320, 140, 1200):
    print("   x=%4d log %7.1f  %s -> %s" % (x, lx, a, b))

print("\n[tile col x=210 (first tile)] y=200..640")
for y, ly, a, b in edges_col(210, 200, 640):
    print("   y=%4d log %7.1f  %s -> %s" % (y, ly, a, b))

print("\n[outer block] row y=240 x=100..1250")
for x, lx, a, b in edges_row(240, 100, 1250, 4):
    print("   x=%4d log %7.1f  %s -> %s" % (x, lx, a, b))
print("[outer block] col x=1000 y=150..700")
for y, ly, a, b in edges_col(1000, 150, 700, 4):
    print("   y=%4d log %7.1f  %s -> %s" % (y, ly, a, b))

print("\n[carousel dots] row band y=98..118, x=500..800, thr=60")
gs = groups(band_profile(px, 500, 800, 98, 118, 60), 1, 6)
for a, b in gs:
    print("   x %4d..%4d w=%3d | logical %6.1f w=%5.1f" % (a, b, b - a + 1, L(a), L(b - a + 1)))
print("[dots vertical] x=633..680")
gs = groups(vband_profile(px, 90, 130, 600, 700, 60), 1, 3)
for a, b in gs:
    print("   y %4d..%4d h=%3d | logical %6.1f h=%5.1f" % (a, b, b - a + 1, L(a), L(b - a + 1)))
print("   colours:", [(hexs(px[x, 108])) for x in (634, 650, 665, 675)])
