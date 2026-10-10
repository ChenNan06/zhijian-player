# -*- coding: utf-8 -*-
"""Report PNG asset dimensions for key skin resources (ground truth for 1x logical sizes)."""
import os, io, json
from PIL import Image

SKIN = r"E:\IQIYI Video\GeePlayer\5.4.1.5408\skin"
KEYS = [
    "skinDefault/skinplan/player/miniplayer/ctrl/play.png",
    "skinDefault/skinplan/player/miniplayer/ctrl/pause.png",
    "skinDefault/skinplan/player/miniplayer/ctrl/prev.png",
    "skinDefault/skinplan/player/miniplayer/ctrl/openfile.png",
    "skinDefault/skinplan/player/miniplayer/ctrl/volume_stat2.png",
    "skinDefault/skinplan/player/miniplayer/ctrl/full.png",
    "skinDefault/skinplan/player/miniplayer/ctrl/speed_btn.png",
    "skinDefault/skinplan/player/miniplayer/ctrl/volume_now.png",
    "skinDefault/skinplan/player/miniplayer/ctrl/toolbox.png",
    "skinDefault/skinplan/player/miniplayer/ctrl/stop.png",
    "skinDefault/skinplan/frame/root_bk.png",
    "skinDefault/skinplan/frame/topbk.png",
    "skinDefault/skinplan/frame/logo.png",
    "skinDefault/skinplan/frame/maxsize.png",
    "skinDefault/skinplan/frame/list_top_line.png",
    "skinDefault/skinplan/list/list_bk.png",
    "skinDefault/skinplan/list/splite_line.png",
    "skinDefault/skinplan/list/itembk.png",
    "skinDefault/skinplan/list/add_file.png",
    "skinDefault/skinplan/list/default_invalidate.png",
    "skinDefault/skinplan/config/settingbk.png",
    "skinDefault/skinplan/config/option_sel_bk.png",
    "skinDefault/skinplan/config/subwindowbk.png",
    "skinDefault/skinplan/public/ctrl/okbtn.png",
    "skinDefault/skinplan/public/ctrl/cancelbtn.png",
    "skinDefault/skinplan/public/ctrl/mainclose.png",
    "skinDefault/skinplan/frame/scroll.png",
    "player/miniplayer/middle/top_bk.png",
    "player/miniplayer/ctrl/play_slider.png",
    "player/miniplayer/ctrl/slider_line.png",
    "player/miniplayer/ctrl/playslider_ball.png",
]
out = {}
for rel in KEYS:
    p = os.path.join(SKIN, rel.replace("/", os.sep))
    if not os.path.exists(p):
        out[rel] = None
        print("%-70s MISSING" % rel)
        continue
    im = Image.open(p)
    out[rel] = {"size": [im.width, im.height], "mode": im.mode,
                "bytes": os.path.getsize(p)}
    print("%-70s %4dx%-5d %s" % (rel, im.width, im.height, im.mode))

# also: survey all PNG sizes under player/miniplayer/ctrl and list to see the 1x grid
print("\n--- all distinct sizes in skinDefault player ctrl ---")
d = os.path.join(SKIN, "skinDefault", "skinplan", "player", "miniplayer", "ctrl")
sizes = {}
for fn in os.listdir(d):
    if fn.endswith(".png") and "_dpi200" not in fn:
        im = Image.open(os.path.join(d, fn))
        sizes.setdefault((im.width, im.height), []).append(fn)
for k in sorted(sizes, key=lambda x: -len(sizes[x]))[:12]:
    print("  %4dx%-5d x%d  e.g. %s" % (k[0], k[1], len(sizes[k]), sizes[k][:3]))

os.makedirs(r"D:\AI-Project\至简Player\docs", exist_ok=True)
with io.open(r"D:\AI-Project\至简Player\docs\_raw_asset_sizes.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("\nwrote _raw_asset_sizes.json")
