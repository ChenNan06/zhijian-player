# -*- coding: utf-8 -*-
"""Explore: settings window (设置界面3.png = 858x750 = exactly 572x500 @1.5x)."""
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

im, px = load("设置界面3.png")
W, H = im.size
print("===== 设置界面3.png %dx%d -> logical %.1fx%.1f =====" % (W, H, L(W), L(H)))

print("\n[vertical structure @x=300 (content area)]")
prev = px[300, 0]
for y in range(1, H):
    p = px[300, y]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 8:
        print("   y=%4d %s -> %s" % (y, hexs(prev), hexs(p)))
    prev = p

print("\n[window border / bg colours]")
print("   (0,0)=", hexs(px[0, 0]), " (1,1)=", hexs(px[1, 1]), " (2,2)=", hexs(px[2, 2]), " (5,5)=", hexs(px[5, 5]))
print("   centre (429,375)=", hexs(px[429, 375]))
print("   left col bg (60,300)=", hexs(px[60, 300]))
print("   right content bg (500,650)=", hexs(px[500, 650]))
print("   bottom bar (429,700)=", hexs(px[429, 700]), " (429,730)=", hexs(px[429, 730]))

print("\n[option column: selected row highlight] col x=90 y=100..600")
prev = px[90, 100]
for y in range(101, 620):
    p = px[90, y]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 8:
        print("   y=%4d %s -> %s | logical y=%.1f" % (y, hexs(prev), hexs(p), L(y)))
    prev = p

print("\n[option selected row horizontal extent] row y=299 (画面 selected) x=0..260")
prev = px[0, 299]
for x in range(1, 260):
    p = px[x, 299]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 8:
        print("   x=%4d %s -> %s | logical x=%.1f" % (x, hexs(prev), hexs(p), L(x)))
    prev = p

print("\n[option text rows] band x=20..160 thr=90, y=100..600")
vs("   option text", vband_profile(px, 100, 600, 20, 160, 90), mincount=1, gap=6)

print("\n[content column: row structure] band x=230..700 thr=90, y=20..700")
vs("   content rows", vband_profile(px, 20, 700, 230, 700, 90), mincount=1, gap=10)

print("\n[content text left edge] row y=145 (常规 title) x=200..600")
prev = px[200, 145]
for x in range(201, 600):
    p = px[x, 145]
    if max(abs(p[i] - prev[i]) for i in range(3)) > 30:
        print("   x=%4d %s -> %s | logical %.1f" % (x, hexs(prev), hexs(p), L(x)))
    prev = p

print("\n[title 设置] band y=30..90 x=0..250 thr=90")
hs("   title", band_profile(px, 0, 250, 30, 90, 90), mincount=1, gap=10)
vs("   title vert", vband_profile(px, 20, 100, 30, 200, 90), mincount=1, gap=6)

print("\n[close button] band y=10..60 x=780..858 thr=90")
hs("   close", band_profile(px, 780, 858, 10, 60, 90), mincount=1, gap=8)
vs("   close vert", vband_profile(px, 5, 70, 790, 858, 90), mincount=1, gap=4)

print("\n[bottom buttons] band y=660..720 thr=60 x=200..858")
hs("   bottom btns", band_profile(px, 200, 858, 660, 720, 60), mincount=1, gap=10)
vs("   ok button vert", vband_profile(px, 640, 750, 400, 500, 60), mincount=1, gap=4)
print("   ok btn colour  avg(430..500, 685..700) =", avg(px, 430, 685, 500, 700))
print("   cancel btn colour avg(710..770, 685..700)=", avg(px, 710, 685, 770, 700))
print("   apply btn colour  avg(560..620, 685..700)=", avg(px, 560, 685, 620, 700))

print("\n[checkbox geometry] first checkbox row")
hs("   cb1", band_profile(px, 230, 700, 175, 200, 90), mincount=1, gap=10)
vs("   cb1 vert", vband_profile(px, 165, 210, 300, 340, 90), mincount=1, gap=3)
