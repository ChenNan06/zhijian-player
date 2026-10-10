# -*- coding: utf-8 -*-
"""Scan a screenshot row/column and print colour runs. Exploration helper.

usage: scan.py <png> row <y> [x0] [x1] [minrun]
       scan.py <png> col <x> [y0] [y1] [minrun]
       scan.py <png> px <x> <y>
       scan.py <png> rowdiff <y> [x0] [x1] [tol]   (edges only)
       scan.py <png> coldiff <x> [y0] [y1] [tol]
"""
import sys
from PIL import Image

def hexs(p):
    return "#%02X%02X%02X" % p[:3]

def runs(pixels, labels, minrun=6, tol=10):
    out = []
    start = 0
    cur = pixels[0]
    for i in range(1, len(pixels)):
        p = pixels[i]
        if max(abs(p[j] - cur[j]) for j in range(3)) > tol:
            if i - start >= minrun:
                out.append((labels[start], labels[i - 1], cur))
            start = i
            cur = p
    if len(pixels) - start >= minrun:
        out.append((labels[start], labels[-1], cur))
    return out

def main():
    path = sys.argv[1]
    mode = sys.argv[2]
    im = Image.open(path).convert("RGBA")
    px = im.load()
    W, H = im.size
    if mode == "px":
        x, y = int(sys.argv[3]), int(sys.argv[4])
        print("(%d,%d) = %s  RGB=%s" % (x, y, "#%02X%02X%02X%02X" % px[x, y], hexs(px[x, y])))
    elif mode == "row":
        y = int(sys.argv[3]); x0 = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        x1 = int(sys.argv[5]) if len(sys.argv) > 5 else W
        mr = int(sys.argv[6]) if len(sys.argv) > 6 else 6
        pix = [px[x, y] for x in range(x0, x1)]
        for a, b, c in runs(pix, list(range(x0, x1)), mr):
            print("  x %4d..%4d  w=%4d  %s" % (a, b, b - a + 1, hexs(c)))
    elif mode == "col":
        x = int(sys.argv[3]); y0 = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        y1 = int(sys.argv[5]) if len(sys.argv) > 5 else H
        mr = int(sys.argv[6]) if len(sys.argv) > 6 else 6
        pix = [px[x, y] for y in range(y0, y1)]
        for a, b, c in runs(pix, list(range(y0, y1)), mr):
            print("  y %4d..%4d  h=%4d  %s" % (a, b, b - a + 1, hexs(c)))
    elif mode == "rowdiff":
        y = int(sys.argv[3]); x0 = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        x1 = int(sys.argv[5]) if len(sys.argv) > 5 else W
        tol = int(sys.argv[6]) if len(sys.argv) > 6 else 24
        prev = px[x0, y]
        print("  x=%d %s" % (x0, hexs(prev)))
        for x in range(x0 + 1, x1):
            p = px[x, y]
            if max(abs(p[j] - prev[j]) for j in range(3)) > tol:
                print("  x=%d %s -> %s  (d=%d)" % (x, hexs(prev), hexs(p),
                      max(abs(p[j] - prev[j]) for j in range(3))))
            prev = p
    elif mode == "coldiff":
        x = int(sys.argv[3]); y0 = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        y1 = int(sys.argv[5]) if len(sys.argv) > 5 else H
        tol = int(sys.argv[6]) if len(sys.argv) > 6 else 24
        prev = px[x, y0]
        print("  y=%d %s" % (y0, hexs(prev)))
        for y in range(y0 + 1, y1):
            p = px[x, y]
            if max(abs(p[j] - prev[j]) for j in range(3)) > tol:
                print("  y=%d %s -> %s  (d=%d)" % (y, hexs(prev), hexs(p),
                      max(abs(p[j] - prev[j]) for j in range(3))))
            prev = p
    else:
        print("unknown mode")

main()
