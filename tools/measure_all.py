# -*- coding: utf-8 -*-
"""Final measurement pass over all 10 screenshots -> _raw_shots_measured.json"""
import os, sys, io, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gplib import *

SHOTS = r"D:\AI-Project\至简Player\爱奇艺播放器界面截图"
OUT = r"D:\AI-Project\至简Player\docs\_raw_shots_measured.json"
ALL = sorted(f for f in os.listdir(SHOTS) if f.lower().endswith(".png"))


def edge_row(px, y, x0, x1, tol=12):
    """list of (x, before, after) colour transitions along a row"""
    ev = []
    prev = px[x0, y]
    for x in range(x0 + 1, x1):
        p = px[x, y]
        if max(abs(p[i] - prev[i]) for i in range(3)) > tol:
            ev.append({"x": x, "logical_x": L(x), "from": hexs(prev), "to": hexs(p)})
        prev = p
    return ev


def edge_col(px, x, y0, y1, tol=12):
    ev = []
    prev = px[x, y0]
    for y in range(y0 + 1, y1):
        p = px[x, y]
        if max(abs(p[i] - prev[i]) for i in range(3)) > tol:
            ev.append({"y": y, "logical_y": L(y), "from": hexs(prev), "to": hexs(p)})
        prev = p
    return ev


def grp(px, x0, x1, y0, y1, thr, mincount=1, gap=8, axis="x"):
    prof = (band_profile(px, x0, x1, y0, y1, thr) if axis == "x"
            else vband_profile(px, y0, y1, x0, x1, thr))
    return [{"from": a, "to": b, "size": b - a + 1, "logical_from": L(a),
             "logical_size": L(b - a + 1)} for a, b in groups(prof, mincount, gap)]


def probe(px, name, x, y):
    return name, {"px": [x, y], "logical": [L(x), L(y)], "rgb": hexs(px[x, y])}


res = {}
for fn in ALL:
    im, px = load(fn)
    W, H = im.size
    rec = {"file": fn, "size": [W, H], "logical_size": [L(W), L(H)], "scale": SCALE,
           "sampled_colors": {}, "measured_regions": {}}
    S, M = rec["sampled_colors"], rec["measured_regions"]

    def add(k, x, y):
        n, v = probe(px, k, x, y)
        S[k] = v

    if fn in ("首界面.png", "播放一级界面.png", "设置界面1.png", "设置界面2.png",
              "热键设置界面.png", "首页无播放界面无列表.png"):
        # ---- main-window chrome ----
        M["title_bar"] = {"y": [0, 53], "h": 54, "logical_h": 36, "note": "0..53 = topbk.png(35)+goldLine(1) @1.5x"}
        M["title_separator"] = {"y": [52, 53], "logical_h": 1.33}
        M["content_top_px"] = 54
        # control bar
        ev = edge_col(px, 300, H - 110, H - 1, 8)
        M["bottom_region_col_x300"] = ev
        # find ctrlbar top separator: first edge after H-100
        M["ctrl_bar"] = {"y": [H - 78, H - 1], "h": 78, "logical_h": 52}
        add("ctrlbar_bg", 300, H - 30)
        add("window_bg_video", 650, H - 120)
        add("titlebar_bg", 600, 27)
        add("title_separator_px", 600, 53)
        # title bar icon groups
        M["title_icons"] = {"groups": grp(px, 950, W, 14, 41, 90, 1, 6)}
        M["title_left"] = {"groups": grp(px, 0, 600, 8, 46, 70, 1, 8)}
    if fn in ("首界面.png", "播放一级界面.png", "设置界面1.png", "设置界面2.png",
              "热键设置界面.png", "首页无播放界面无列表.png", "设置界面4.png"):
        if W == 1299:
            M["drawer_probe_row600"] = edge_row(px, 600, 860, 960, 6)
            M["item_text_rows"] = grp(px, 915, 1120, 150, 800, 90, 1, 4, "y")
            add("drawer_bg", 1000, 620)
            add("drawer_splitter", 896, 600)

    if fn == "首界面.png":
        M["ctrl_left_icons"] = grp(px, 0, 480, 832, 868, 70, 1, 10)
        M["ctrl_centre"] = grp(px, 480, 900, 830, 872, 70, 1, 8)
        M["ctrl_right"] = grp(px, 1000, W, 830, 872, 70, 1, 10)
        M["drawer_title_row"] = grp(px, 900, W, 60, 100, 80, 1, 12)
        M["drawer_title_text_vert"] = grp(px, 915, 990, 54, 130, 80, 1, 3, "y")
        M["home_grid_cols"] = grp(px, 0, 900, 260, 400, 60, 1, 12)
        M["home_grid_rows"] = grp(px, 90, 800, 200, 700, 60, 1, 12, "y")
        add("home_tile_border", 100, 330)
        add("home_tile_fill", 165, 330)
        add("home_block_bg", 80, 240)
        M["scrollbar_cols"] = grp(px, 1255, W, 150, 800, 60, 3, 4)

    if fn == "播放一级界面.png":
        M["ctrl_left_icons"] = grp(px, 0, 480, 832, 868, 70, 1, 10)
        M["ctrl_centre"] = grp(px, 480, 900, 830, 872, 70, 1, 8)
        M["ctrl_right"] = grp(px, 1000, W, 830, 872, 70, 1, 10)
        M["progress_row798"] = grp(px, 0, W, 797, 801, 60, 2, 8)
        M["progress_col_x300"] = [{"y": y, "rgb": hexs(px[300, y])} for y in range(792, 810)]
        M["progress_played_boundary"] = edge_row(px, 799, 0, W, 40)
        M["playbtn_vert"] = grp(px, 630, 670, 800, H - 1, 70, 1, 4, "y")
        add("progress_played_max", 400, 800)
        add("progress_played_min", 60, 800)
        add("progress_unplayed", 900, 800)
        # currently playing row highlight
        M["playing_item_row"] = edge_col(px, 1100, 380, 470, 10)
        add("playing_item_bg", 1150, 425)
        add("playing_item_border", 900, 425)

    if fn == "播放界面挪动鼠标.png":
        M["ctrl_left_icons"] = grp(px, 0, 480, 832, 868, 70, 1, 10)
        M["ctrl_centre"] = grp(px, 480, 900, 830, 872, 70, 1, 8)
        M["ctrl_right"] = grp(px, 1000, W, 830, 872, 70, 1, 10)
        M["top_toolbar"] = grp(px, 800, W, 60, 100, 90, 1, 12)
        M["handle_col_x1290"] = edge_col(px, 1290, 54, 800, 14)
        M["handle_row_y400"] = edge_row(px, 400, 1230, W, 14)
        M["progress_col_x600"] = [{"y": y, "rgb": hexs(px[600, y])} for y in range(788, 806)]
        M["progress_row796"] = edge_row(px, 796, 0, W, 45)
        add("handle_bg", 1292, 400)
        add("top_toolbar_bg", 1000, 79)

    if fn in ("设置界面1.png", "设置界面4.png"):
        M["settings_bbox_row400"] = edge_row(px, 400, 180, 260, 12)
        M["settings_bbox_col600"] = edge_col(px, 600, 40, 120, 12)
        add("settings_outer_border", 221, 400)
        add("settings_inner_bg", 225, 400)
        add("settings_left_col_bg", 300, 400)
        add("settings_content_bg", 700, 700)
        M["checkbox_boxes"] = grp(px, 470, 500, 150, 620, 90, 1, 6, "y")
        M["checkbox_box_h"] = grp(px, 440, 560, 180, 195, 90, 1, 6)
        add("checkbox_tick_green", 476, 314)
        M["bottom_buttons"] = grp(px, 200, 1080, 660, 720, 60, 1, 10)
        add("btn_ok_green", 940, 761)
        add("btn_apply_bg", 1240, 761)
        add("bottom_row_bg", 700, 760)

    if fn == "设置界面3.png":
        M["note"] = "pure settings window 858x750 = 572x500 logical @1.5x"
        M["window_border"] = {"px": [0, 0], "rgb": hexs(px[0, 0])}
        add("outer_border", 0, 0)
        add("border_inner", 1, 1)
        add("left_col_bg", 60, 640)
        add("left_col_selected_bg", 60, 300)
        add("content_bg", 500, 650)
        add("content_bg2", 429, 375)
        M["option_text_rows"] = grp(px, 20, 160, 100, 600, 90, 1, 6, "y")
        M["content_rows"] = grp(px, 230, 700, 20, 700, 90, 1, 10, "y")
        M["bottom_buttons"] = grp(px, 200, 858, 660, 720, 60, 1, 10)
        M["close_button"] = grp(px, 780, 858, 10, 60, 90, 1, 8)
        M["close_button_vert"] = grp(px, 790, 858, 5, 70, 90, 1, 4, "y")
        M["ok_button_vert"] = grp(px, 400, 500, 640, 750, 60, 1, 4, "y")
        add("btn_ok_green", 450, 690)
        add("btn_cancel_bg", 740, 690)
        M["content_col_edges_row145"] = edge_row(px, 145, 200, 600, 30)
        M["selected_option_row_col90"] = edge_col(px, 90, 100, 620, 8)

    if fn == "热键设置界面.png":
        M["subwindow_row500"] = edge_row(px, 500, 250, 1100, 12)
        M["subwindow_col400"] = edge_col(px, 400, 100, 850, 12)
        M["list_rows"] = grp(px, 340, 600, 180, 700, 90, 1, 6, "y")
        add("subwindow_bg", 700, 600)
        add("list_bg", 700, 400)
        add("list_header_bg", 700, 228)

    if fn in ("首页无播放界面无列表.png", "播放界面无列表.png"):
        M["ctrl_left_icons"] = grp(px, 0, 480, 832, min(868, H - 5), 70, 1, 10)
        M["home_grid_cols"] = grp(px, 0, 900, 260, 400, 60, 1, 12)
        M["home_grid_rows"] = grp(px, 90, 800, 180, min(700, H - 10), 60, 1, 12, "y")
        add("home_tile_border", 100, 330)
        add("home_block_bg", 80, 240)

    res[fn] = rec

with io.open(OUT, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print("wrote %s  %.1f KB" % (OUT, os.path.getsize(OUT) / 1024.0))
for fn in ALL:
    print("  %-28s %s" % (fn, res[fn]["size"]))
