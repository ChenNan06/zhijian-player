# -*- coding: utf-8 -*-
"""Explore: control bar internals on 播放一级界面.png / 首界面.png."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gplib import *

def hs(label, prof, **kw):
    gs = groups(prof, **kw)
    print("%s: %d" % (label, len(gs)))
    for a, b in gs:
        print("   x %4d..%4d w=%3d | logical x %7.1f..%7.1f w=%5.1f" % (a, b, b - a + 1, L(a), L(b + 1), L(b - a + 1)))
    return gs

def vs(label, prof, **kw):
    gs = groups(prof, **kw)
    print("%s: %d" % (label, len(gs)))
    for a, b in gs:
        print("   y %4d..%4d h=%3d | logical y %7.1f..%7.1f h=%5.1f" % (a, b, b - a + 1, L(a), L(b + 1), L(b - a + 1)))
    return gs

for name in ("播放一级界面.png",):
    im, px = load(name)
    W, H = im.size
    print("\n===== %s  %dx%d  logical %.1fx%.1f =====" % (name, W, H, L(W), L(H)))

    print("\n[ctrlbar vertical structure @x=300]")
    prev = px[300, 780]
    for y in range(781, H):
        p = px[300, y]
        if max(abs(p[i] - prev[i]) for i in range(3)) > 10:
            print("   y=%4d %s -> %s" % (y, hexs(prev), hexs(p)))
        prev = p

    print("\n[ctrlbar bg colour]  avg(20..500, 810..830) =", avg(px, 20, 810, 500, 830))
    print("                     avg(900..1100,860..880)=", avg(px, 900, 860, 1100, 880))
    print("   dominant(20,810,300,880) =", dominant(px, 20, 810, 300, 880, 4))

    print("\n[progress bar] scan col x=200 y=780..815")
    for y in range(780, 816):
        print("   y=%3d %s" % (y, hexs(px[200, y])))
    print("  row y=798 whole width, groups thr=60:")
    hs("   progress row y=798", band_profile(px, 0, W, 797, 801, 60), mincount=2, gap=8)
    print("  row y=799 played/unplayed boundary:")
    prev = px[0, 799]
    for x in range(1, W):
        p = px[x, 799]
        if max(abs(p[i] - prev[i]) for i in range(3)) > 40:
            print("   x=%4d %s -> %s" % (x, hexs(prev), hexs(p)))
        prev = p

    print("\n[left icon group] band y=832..868 thr=70")
    hs("   left icons", band_profile(px, 0, 480, 832, 868, 70), mincount=1, gap=10)
    print("[time text] band y=832..868 thr=70, x=380..520")
    hs("   time", band_profile(px, 380, 520, 832, 868, 70), mincount=1, gap=10)
    p = vband_profile(px, 820, 883, 505, 590, 70)
    vs("   time vertical x=505..590", p, mincount=1, gap=3)
    print("[centre transport] band y=830..872 thr=70, x=480..900")
    hs("   centre", band_profile(px, 480, 900, 830, 872, 70), mincount=1, gap=8)
    print("[play button vertical] x=630..670")
    vs("   play vertical", vband_profile(px, 800, 883, 630, 670, 70), mincount=1, gap=4)
    print("[right group] band y=830..872 thr=70, x=1000..1299")
    hs("   right", band_profile(px, 1000, 1299, 830, 872, 70), mincount=1, gap=10)
    print("[right group vertical for 倍速 pill] x=1030..1100")
    vs("   pill vertical", vband_profile(px, 810, 883, 1030, 1100, 60), mincount=1, gap=3)
