# -*- coding: utf-8 -*-
"""Measurement helpers for GeePlayer screenshot reverse-engineering."""
import os
from PIL import Image

SHOTS = r"D:\AI-Project\至简Player\爱奇艺播放器界面截图"
SCALE = 1.5  # physical px per logical px (confirmed by 36->54, 52->78, 32->48, 269->403)


def load(name):
    im = Image.open(os.path.join(SHOTS, name)).convert("RGB")
    return im, im.load()


def hexs(p):
    return "#%02X%02X%02X" % p[:3]


def lum(p):
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def band_profile(px, x0, x1, y0, y1, thr):
    """count pixels brighter than thr per column"""
    return [(x, sum(1 for y in range(y0, y1) if lum(px[x, y]) > thr)) for x in range(x0, x1)]


def vband_profile(px, y0, y1, x0, x1, thr):
    return [(y, sum(1 for x in range(x0, x1) if lum(px[x, y]) > thr)) for y in range(y0, y1)]


def groups(profile, mincount=1, gap=0):
    """contiguous runs where count >= mincount, merging gaps <= gap"""
    out = []
    cur = None
    for pos, c in profile:
        if c >= mincount:
            if cur is None:
                cur = [pos, pos]
            elif pos - cur[1] <= gap + 1:
                cur[1] = pos
            else:
                out.append(tuple(cur))
                cur = [pos, pos]
    if cur:
        out.append(tuple(cur))
    return out


def bbox_bright(px, x0, y0, x1, y1, thr=70):
    xs, ys = [], []
    for y in range(y0, y1):
        for x in range(x0, x1):
            if lum(px[x, y]) > thr:
                xs.append(x); ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def dominant(px, x0, y0, x1, y1, topn=6):
    hist = {}
    for y in range(y0, y1):
        for x in range(x0, x1):
            c = px[x, y]
            hist[c] = hist.get(c, 0) + 1
    tot = sum(hist.values()) or 1
    top = sorted(hist.items(), key=lambda kv: -kv[1])[:topn]
    return [{"rgb": hexs(c), "n": n, "pct": round(n * 100.0 / tot, 1)} for c, n in top]


def avg(px, x0, y0, x1, y1):
    r = g = b = n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            p = px[x, y]
            r += p[0]; g += p[1]; b += p[2]; n += 1
    n = n or 1
    return "#%02X%02X%02X" % (r // n, g // n, b // n)


def L(v):
    """physical px -> logical px at SCALE"""
    return round(v / SCALE, 2)
