# -*- coding: utf-8 -*-
"""Probe the GeePlayer screenshots: sizes, alpha, and locate the floating
capsule control bar by scanning for rounded-rect light areas near the bottom."""
import os, io, json, sys
from PIL import Image

SHOTS = r"D:\AI-Project\至简Player\爱奇艺播放器界面截图"
OUT = r"D:\AI-Project\至简Player\docs\_raw_shots_probe.json"

files = sorted(f for f in os.listdir(SHOTS) if f.lower().endswith(".png"))
info = {}
for fn in files:
    p = os.path.join(SHOTS, fn)
    im = Image.open(p)
    rgba = im.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    # corner samples
    corners = {
        "tl": px[0, 0], "tr": px[w - 1, 0], "bl": px[0, h - 1], "br": px[w - 1, h - 1],
        "center": px[w // 2, h // 2],
    }
    # alpha stats / opaque bbox (screenshots may have transparent rounded shadow margins)
    alpha = rgba.getchannel("A")
    bbox = alpha.getbbox()
    amin, amax = alpha.getextrema()
    info[fn] = {
        "path": p,
        "size": [w, h],
        "mode": im.mode,
        "alpha_extrema": [amin, amax],
        "opaque_bbox": list(bbox) if bbox else None,
        "corners": {k: "#%02X%02X%02X%02X" % v for k, v in corners.items()},
        "file_bytes": os.path.getsize(p),
    }
    print("%-28s %4dx%-5d mode=%-6s alpha=%s bbox=%s  TL=%s C=%s" % (
        fn, w, h, im.mode, (amin, amax), bbox,
        info[fn]["corners"]["tl"], info[fn]["corners"]["center"]))

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with io.open(OUT, "w", encoding="utf-8") as f:
    json.dump(info, f, ensure_ascii=False, indent=1)
print("wrote", OUT)
