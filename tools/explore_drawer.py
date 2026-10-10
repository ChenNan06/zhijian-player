# -*- coding: utf-8 -*-
"""Explore: right playlist drawer geometry on 首界面.png / 播放一级界面.png."""
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

im, px = load("首界面.png")
W, H = im.size
print("===== 首界面 %dx%d logical %.1fx%.1f =====" % (W, H, L(W), L(H)))

print("\n[drawer left edge] row y=600 x=850..960 detail")
prev = px[850, 600]
for x in range(851, 960):
    p = px[x, 600]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 6:
        print("   x=%4d %s -> %s" % (x, hexs(prev), hexs(p)))
    prev = p

print("\n[drawer colours]")
print("   bg list area   avg(1000..1240,600..760) =", avg(px, 1000, 1240, 600, 760) if False else avg(px, 1000, 600, 1240, 760))
print("   dominant list  ", dominant(px, 1000, 600, 1240, 700, 5))
print("   splitter line  avg(896,600..760) =", avg(px, 896, 600, 898, 760))
print("   row y=600 x=894..900:", [hexs(px[x, 600]) for x in range(893, 901)])

print("\n[list title row] band y=60..100 thr=80 x=900..1299")
hs("   title row", band_profile(px, 900, 1299, 60, 100, 80), mincount=1, gap=12)
print("[list title vertical] x=915..990")
vs("   title vert", vband_profile(px, 54, 130, 915, 990, 80), mincount=1, gap=3)

print("\n[search row] band y=105..150 thr=60 x=900..1299")
hs("   search row", band_profile(px, 900, 1299, 105, 150, 60), mincount=1, gap=12)

print("\n[item rows] vertical bright profile x=915..1120, y=150..800")
vs("   item text rows", vband_profile(px, 150, 800, 915, 1120, 90), mincount=1, gap=4)

print("\n[item horizontal structure] row through first item y=268..272 avg")
hs("   item row", band_profile(px, 900, 1299, 260, 285, 80), mincount=1, gap=14)

print("\n[scrollbar] band x=1240..1299 y=150..800, horizontal profile")
hs("   scrollbar cols", band_profile(px, 1255, 1299, 150, 800, 60), mincount=3, gap=4)

print("\n[drawer top divider / search underline] col x=1100 y=54..170")
prev = px[1100, 54]
for y in range(55, 170):
    p = px[1100, y]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 6:
        print("   y=%4d %s -> %s" % (y, hexs(prev), hexs(p)))
    prev = p
