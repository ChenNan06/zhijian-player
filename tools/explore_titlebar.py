# -*- coding: utf-8 -*-
"""Explore: title bar icon geometry, control bar element geometry."""
import os, sys, io, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gplib import *

def show_groups(label, prof, **kw):
    gs = groups(prof, **kw)
    print("%s: %d groups" % (label, len(gs)))
    for a, b in gs:
        print("   x %4d..%4d  w=%3d   (logical x=%7.1f w=%6.1f)" % (a, b, b - a + 1, L(a), L(b - a + 1)))
    return gs

def show_vgroups(label, prof, **kw):
    gs = groups(prof, **kw)
    print("%s: %d groups" % (label, len(gs)))
    for a, b in gs:
        print("   y %4d..%4d  h=%3d   (logical y=%7.1f h=%6.1f)" % (a, b, b - a + 1, L(a), L(b - a + 1)))
    return gs

im, px = load("首界面.png")
W, H = im.size
print("== 首界面 %dx%d ==" % (W, H))

print("\n--- title bar right icon group (band y=14..41, thr=90) ---")
p = band_profile(px, 950, 1299, 14, 41, 90)
show_groups("titlebar right", p, mincount=1, gap=6)

print("\n--- title bar left (band y=8..46, thr=70) ---")
p = band_profile(px, 0, 600, 8, 46, 70)
show_groups("titlebar left", p, mincount=1, gap=8)

print("\n--- title bar vertical extent of close X (x=1250..1299) ---")
p = vband_profile(px, 0, 54, 1250, 1299, 90)
show_vgroups("close X vertical", p, mincount=1, gap=3)

print("\n--- title bar vertical extent of min line (x=1100..1150) ---")
p = vband_profile(px, 0, 54, 1100, 1150, 90)
show_vgroups("min line vertical", p, mincount=1, gap=3)

print("\n--- titlebar colours ---")
print("  bar bg (x=600,y=27)      ", hexs(px[600, 27]))
print("  avg left region          ", avg(px, 300, 5, 900, 50))
print("  title text colour (max)  ", hexs(max(((px[x,27]) for x in range(70,233)), key=lum)))
print("  goldline row avg y=52..53", avg(px, 100, 52, 900, 54))
print("  goldline row avg y=54    ", avg(px, 100, 54, 900, 55))
