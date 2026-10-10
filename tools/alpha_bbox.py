# -*- coding: utf-8 -*-
"""Alpha bounding boxes of each frame inside the frame-strip PNGs."""
import os
from PIL import Image

SKIN = r"E:\IQIYI Video\GeePlayer\5.4.1.5408\skin\skinDefault\skinplan"
CASES = [
    ("player/miniplayer/ctrl/play.png", 41, 41),
    ("player/miniplayer/ctrl/pause.png", 41, 41),
    ("player/miniplayer/ctrl/openfile.png", 25, 25),
    ("player/miniplayer/ctrl/prev.png", 25, 25),
    ("player/miniplayer/ctrl/stop.png", 25, 25),
    ("player/miniplayer/ctrl/next.png", 25, 25),
    ("player/miniplayer/ctrl/volume_stat2.png", 25, 25),
    ("player/miniplayer/ctrl/full.png", 25, 25),
    ("player/miniplayer/ctrl/toolbox.png", 25, 25),
    ("player/miniplayer/ctrl/open_trace.png", 25, 25),
    ("player/miniplayer/ctrl/speed_btn.png", 51, 25),
    ("player/miniplayer/ctrl/volume_now.png", None, None),
    ("frame/logo.png", 166, 28),
    ("frame/maxsize.png", 24, 24),
    ("public/ctrl/mainclose.png", 24, 24),
    ("list/add_file.png", 24, 24),
    ("list/default_invalidate.png", None, None),
]
for rel, fw, fh in CASES:
    p = os.path.join(SKIN, rel.replace("/", os.sep))
    im = Image.open(p).convert("RGBA")
    W, H = im.size
    a = im.getchannel("A")
    if fw:
        # frame 0 only
        sub = a.crop((0, 0, fw, fh))
        bb = sub.getbbox()
        print("%-48s %3dx%-3d frame0=%-10s alpha_bbox(frame0)=%s -> visible %s" % (
            rel, W, H, "%dx%d" % (fw, fh), bb,
            (bb[2] - bb[0], bb[3] - bb[1]) if bb else None))
    else:
        bb = a.getbbox()
        print("%-48s %3dx%-3d whole alpha_bbox=%s -> visible %s" % (
            rel, W, H, bb, (bb[2] - bb[0], bb[3] - bb[1]) if bb else None))
