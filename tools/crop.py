# -*- coding: utf-8 -*-
"""Crop a region of a screenshot and upscale it for visual inspection.

usage: crop.py <png> <out.png> <x0> <y0> <x1> <y1> [scale]
"""
import sys, os
from PIL import Image

src, dst, x0, y0, x1, y1 = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
scale = float(sys.argv[7]) if len(sys.argv) > 7 else 3.0
im = Image.open(src).convert("RGB").crop((x0, y0, x1, y1))
im = im.resize((int(im.width * scale), int(im.height * scale)), Image.NEAREST)
os.makedirs(os.path.dirname(dst), exist_ok=True)
im.save(dst)
print("%s  %dx%d -> %dx%d (x%.1f)" % (dst, x1 - x0, y1 - y0, im.width, im.height, scale))
