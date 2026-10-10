# -*- coding: utf-8 -*-
"""Assemble the final raw-data JSON deliverable."""
import json, io, os, sys

DOCS = r"D:\AI-Project\至简Player\docs"
SKIN = r"E:\IQIYI Video\GeePlayer\5.4.1.5408\skin"

xmlblob = json.load(io.open(os.path.join(DOCS, "_raw_xml_layouts.json"), encoding="utf-8"))
thm = json.load(io.open(os.path.join(DOCS, "_raw_theme.json"), encoding="utf-8"))
shots = json.load(io.open(os.path.join(DOCS, "_raw_shots_measured.json"), encoding="utf-8"))
assets = json.load(io.open(os.path.join(DOCS, "_raw_asset_sizes.json"), encoding="utf-8"))

# ---------- xml_layouts: {file: {control_name: {attrs...}}} ----------
layouts = {}
for f, rec in xmlblob["xml_layouts"].items():
    ctrls = {}
    for k, v in rec["controls"].items():
        d = {}
        for kk, vv in v.items():
            s = str(vv)
            if len(s) > 260:
                s = s[:260] + "…(truncated,%d chars)" % len(s)
            d[kk] = s
        ctrls[k] = d
    layouts[f] = ctrls

# ---------- theme_colors ----------
theme_colors = {}
for k, v in thm["theme_colors"].items():
    if "/color/" not in k:
        continue
    theme_name = "skinDefault" if k.startswith("theme[0]") else "skinPeeler"
    key = k.split("/color/", 1)[1]
    theme_colors.setdefault(theme_name, {})[key] = {
        "raw": v["raw"], "format": v["fmt"], "aarrggbb": v["aarrggbb"],
        "rgb": v["rgb"], "alpha": v["alpha"], "alpha_pct": v["alpha_pct"],
    }

DERIVED = {
    "dpi_scale": {
        "value": 1.5,
        "evidence": [
            "title bar 36 logical -> 54 physical px (topbk 35 + goldLine 1)",
            "control bar ctrlLayout height 52 -> 78 physical px",
            "FileItem height 32 -> item pitch 48 physical px",
            "playlist drawer minwidth 269 -> 403 physical px",
            "settings Window size 572,500 -> 设置界面3.png exactly 858x750 px",
        ],
        "dpi200_assets": "every skinDefault PNG has a *_dpi200.png twin; the XML names no dpi branch, "
                         "the loader appends '_dpi200' when system scaling >= 150% is detected by the app",
    },
    "main_window": {
        "measured_screenshot_window": "866 x 589 logical (1299x883 px @1.5x)",
        "min_size": "580 x 200 (skin/frame/skin.xml mininfo=\"580,200\")",
        "resize_border": "8 px (sizebox=\"8,8,8,8\")",
        "caption_drag_area": "0,0,0,38",
        "shadow": "true, frame/shadow.png",
        "background_asset": "skinplan/frame/root_bk.png = solid #1F1F1F",
        "content_area": "y 36 .. 536.7 logical (500 tall), between title bar and control bar",
    },
    "title_bar": {
        "total_height": 36,
        "breakdown": "35 px topbk.png content + 1 px goldLine (list_top_line.png, #FFFFFF @ 8% alpha)",
        "bg": "frame/topbk.png, gradient #1D1D1F -> #1E1E20",
        "separator_rendered": "#272728 on the #1D1D1F bar (measured 首界面 y=52..53)",
        "left_padding_to_logo_image": 8,
        "logo_button": "166 x 28 (frame/logo.png, 3 frames of 166x28; visible ink 142x24 starting 7 px in)",
        "logo_green_icon_measured": "x 14.7 .. 39.3 logical",
        "logo_text": "爱奇艺万能联播, measured peak #B2B2B2 (theme title_name_color #999999)",
        "dropdown_chevron_measured": "x 148.0 .. 157.3",
        "right_icon_buttons": "24 x 24, glyph 12-14 px, hover = green #45B000 (min/max) / red #FF4747 (close)",
        "measured_icon_centres": {"transfer": 687.7, "sharezone": 718.0, "history": 750.0,
                                  "splitter": 758.7, "min": 791.0, "max": 820.3, "close": 849.3},
        "button_pitch": "30.3 / 32.0 (nav group, padding-right 8), 29.3 / 29.0 (window group, padding 5)",
        "right_edge_rule": "close button 24 wide + 5 px right padding -> centre = width - 17 (866-17 = 849)",
        "nav_splitter": "1 x 12 px vertical line (frame/topsplitter.png)",
    },
    "bottom_control_bar": {
        "height": 52,
        "margins": "0 left / 0 right / 0 bottom - full-bleed bar, NO floating capsule, NO rounded corners",
        "top_edge": "1 px separator line, measured #313132 at logical y 536.3",
        "bg": "measured #0F1011 (avg #121314 over the whole bar)",
        "inner_padding": "padding 0,5,0,0 on ctrlLayout (5 px top)",
        "vertical_centre_of_buttons": 562.7,
        "icon_button_size": "25 x 25 (all transport/utility buttons, *_dpi200 twins exist)",
        "play_button": "41 x 41 frame (play.png/pause.png 164x41 = 4 frames), visible circle dia 36",
        "left_group_order": ["ctrl_file (25x25, pad-left 18)", "btn_playorder (25x25, pad-left 12)",
                             "ctrl_dlna (25x25, pad-left 12)", "ctrl_dlna_render (25x25, pad-left 12)"],
        "left_group_pitch": 37,
        "left_group_measured_centres": [30.3, 67.0, 104.7, 141.7],
        "time_label": {"x_start": 172, "width_budget": 105, "padding": "18,12,0,0",
                       "font": "player_14size (Tahoma 14)", "colour_key": "color_player_pos_time #999999",
                       "measured_ink": "x 172.0..250.0, y 556.0..569.3"},
        "centre_group": ["ctrl_stop (25x25)", "ctrl_last (25x25, pad 12/15)", "ctrl_play (41x41)",
                         "ctrl_next (25x25, pad-left 15)", "ctrl_mute (25x25, pad-left 12)",
                         "volume_slider (79 x 17, thumbsize 17x17)"],
        "centre_group_measured_centres": {"stop": 348.0, "prev": 384.7, "play": 432.7,
                                          "next": 480.7, "mute": 517.0},
        "play_button_centred_on_window": "433 = window width / 2",
        "right_group_order": ["ctrl_speed (65x25 box, visible pill 37x18)", "check_trace_play (25x25)",
                              "ctrl_image/toolbox (25x25)", "ctrl_full (25x25, pad-right 18)"],
        "right_group_measured": {"pill": [692.7, 729.3], "trace": [755.3, 767.3],
                                 "toolbox": [791.3, 804.7], "fullscreen": [828.7, 841.3]},
        "right_group_pitch": 37,
        "hidden_by_default": ["ctrl_subtitle", "ctrl_sound_track", "ctrl_full_screen_info"],
    },
    "progress_slider": {
        "widget_row_height": 20,
        "track_height": 3,
        "track_x_span": [33, 845],
        "track_y": [532.7, 535.7],
        "gap_to_bar_top": 1.0,
        "unplayed": "slider_bk.png 1x16 pure #FFFFFF at alpha 0x1A (10%) -> renders #2F3030",
        "played": "play_slider.png 512x16, horizontal gradient #146900 -> #7FF624 at alpha 179 (70%)",
        "played_measured": ["#1B580D (start)", "#62B425 (near end)"],
        "thumb": "playslider_ball.png 15x15 white circle, hover only",
        "skip_buttons": "btnleft / btnright 21 x 16, glyph ~12-13 px, padding 2,4,10,0 / 10,4,0,0",
    },
    "playlist_drawer": {
        "width": 269,
        "width_source": "skin/frame/skin.xml minwidth=\"269\" on fixed_list_l_con / leftspace / rightspace "
                        "and on the two 269 px spacers; measured 403 px = 268.7 logical",
        "position": "docked right, full height of the content area; x 597.3..865.3, y 36..536",
        "left_splitter": "1 px (list/splite_line.png #1D1D1F), measured #1C1C1D",
        "bg_measured": "#0D0D0D (asset list/list_bk.png = #131314, theme list_bk = #1D1D1F)",
        "title_row": {"height": 24, "padding": "12,7,0,0", "font": "yahei_14",
                      "colour_key": "list_title_text #CCCCCC",
                      "measured_text": "x 610.0..695.3, ink y 50.0..62.0"},
        "title_button_group": {"width": 130, "button": "24 x 24", "inter_button_padding": 2,
                               "order": ["addfile", "delfile", "filterFileMenu", "showmodel", "localmenu"],
                               "measured_centres": [749.0, 800.3, 826.0, 852.0]},
        "search_row": {"height": 24, "padding": "12,5,12,7", "icon": "12 x 12",
                       "edit_textpadding": "5,3,2,0", "font": "yahei_12",
                       "underline": "2 px #FFFFFF @ ~20%, measured y 94.7..96.0, measured max #515151"},
        "item_row": {"height": 32, "measured_pitch": 32, "right_inset": 10, "leading_space": 10,
                     "label": "height 30, font yahei_12, colour list_item_text_nor #CCCCCC",
                     "label_left_measured": 610.0, "right_control_width": 88,
                     "right_control_padding": "3,0,6,0",
                     "time_right_measured": 849.3,
                     "hover_bg": "list_item_hotbk / list_item_selected_bk #262629",
                     "selected_text": "list_item_selected_text #45B000",
                     "playing_text": "list_item_text_play #04B000",
                     "row_icons": ["playicon 12x12", "lastplayed_icon 16x12", "item_playbtn 24x24",
                                   "item_openfolder 24x24", "item_closebtn 24x24"]},
        "thumbnail_mode_row": {"height": 62, "bottom_padding": 2, "leading_space": 12,
                               "cover": "90 x 56 (list/default_invalidate.png)",
                               "title": "height 35, font yahei_12, textpadding 0,0,15,0",
                               "meta_row": "height 20", "play_overlay": "30 x 30"},
        "scrollbar": {"width": 8, "measured_x": [856.7, 864.7], "thumb": "frame/scroll.png 32x30, 4 states",
                      "thumb_corner": "0,3,0,3"},
        "hover_reveal_handle": {"measured": "two tabs at x 839.3..866 logical; tab1 y 220.7..267.3, "
                                           "tab2 starts y 272.7; ~5 px gap; bg #2B2828 over the video",
                                "content": "tab1 = playlist icon, tab2 = '<' collapse arrow"},
    },
    "settings_panel": {
        "window_size": "572 x 500",
        "verified": "设置界面3.png is exactly 858x750 px = 572x500 @1.5x",
        "border": "1 px #303033 (asset corner), inner #1D1D1F..#252528 gradient (config/settingbk.png)",
        "corner_radius": "corner='112,60,2,2' on the root bkimage -> 2 px outer radius",
        "content_bg_measured": "#252528",
        "close_row": {"height": 24, "padding": "0,4,5,0", "button": "24 x 24",
                      "measured_glyph": "x 550.0..560.0, y 10.7..21.3"},
        "left_category_column": {"width": 111, "inset_top": 44, "item_size": "111 x 36",
                                 "item_font": "yahei_14", "item_text_nor": "#666666",
                                 "item_text_hover": "#00993D", "item_text_select": "#00993D",
                                 "bg_hover": "#222224", "bg_selected": "#262629",
                                 "measured_first_item_centre_y": 90.7, "measured_pitch": 36,
                                 "items": ["常规", "播放", "列表", "画面", "歌词", "声音", "热键", "关联"]},
        "right_content": {"page_container_width": 456, "inset_left": 36,
                          "content_left_measured": 149,
                          "group_title": "Label height 14, font yahei_14b, #DDDDDD",
                          "inner_column_padding_left": 15,
                          "checkbox": {"glyph": "14 x 14 (public/ctrl/checkboxpicture.png)",
                                       "row_height": 16, "vertical_gap": 12,
                                       "pitch": 28, "measured_pitch": 28,
                                       "label_offset": 22, "textpadding": "22,-2",
                                       "label_font": "yahei_12", "label_colour": "#999999"},
                          "sub_group_title": "font yahei_12b, #AAAAAA",
                          "combo": "180 x 26 (or 88 x 26), padding-top 12",
                          "edit": "186 x 26 / 60 x 26",
                          "group_block_gap": 16},
        "bottom_bar": {"height": 28, "padding": "148,38,36,26",
                       "buttons": ["恢复默认设置 (78 wide, text-only, #00993D)",
                                   "确定 80 x 28 (#008000 normal / #009A00 hot)",
                                   "应用 80 x 28 (#006600)",
                                   "取消 80 x 28 (#006600)"],
                       "inter_button_gap": 12, "right_margin": 36,
                       "measured_ok_button": "x 272.0..352.0, y 446.0..474.0"},
        "hotkey_subwindow": {"size": "500 x 444", "caption": "0,0,0,32",
                             "bg": "#202020 (config/subwindowbk.png 322x170), border #34353A, corner 2",
                             "header_row": "height 25, padding 33,7,9,0, title yahei_14b #777777, close 24x24",
                             "list": "height 319, padding 33,14,33,0, itemheight 28, header height 28",
                             "columns": [180, 252], "itemtextpadding": "10,0,0,0",
                             "measured_row_pitch": 28,
                             "bottom_row": "height 28, padding 33,26,33,25, 恢复默认热键 78 wide + ok/cancel 80x28"},
    },
    "home_page": {
        "container": "AutoWrapHorizontalLayoutUI name=MiddleLayout, inset 30,30,30,30, childpadding 20, maxhorcount 5",
        "tile": "100 x 100",
        "tile_gap": 20,
        "max_columns": 5,
        "block_bg_measured": "#0F0F0F",
        "tile_bg_measured": "#161616",
        "tile_border_measured": "#202020..#242424",
        "tile_text": "textpadding 0,50,0,0, font yahei_14, #FFFFFF",
        "measured_5col": "block 660 wide (5x100 + 4x20 + 2x30 inset), 279 tall (2x100 + 20 + 2x30)",
        "measured_4col": "block 520.7 wide when the playlist drawer is open",
        "new_badge": "green corner ribbon, sampled #45B000",
    },
    "image_viewer": {
        "window": "mininfo 465,340; caption 0,0,0,26; sizebox 8,8,8,8; shadow off",
        "title_bar": "26 tall (imgview/viewframe/topbk.png); titleicon 26x26; skin/menu/min/max/close 28x26",
        "background": "viewroot #EDEDED, viewmain (empty) #F2F2F2, viewer stage #1D1D1F (color_imgbk)",
        "bottom_panel": "ViewPanel height 51, corner 5,5,5,5; buttons 35x35; prev/next 41x41; "
                        "splitters 1px with padding 10,19,17,19 / 17,19,0,19",
        "fullscreen_panel": "fullPanel 643 x 57, buttons 35x35, padding 4,11,0,0",
    },
    "menus": {
        "item_height": 28, "division": "1 px (menu/menu_line.png)",
        "bg": "#222222 (color_meun_bk)", "item_bg_nor": "#131313", "item_bg_hot": "#373333",
        "item_bg_sel": "#1F1F1F", "text_nor": "#999999", "text_hot": "#0BB005",
        "text_sel": "#0BB005", "text_disable": "#444444",
    },
    "top_toolbar_over_video": {
        "height": 28, "inset": "0,0,4,0",
        "bg": "player/miniplayer/middle/top_bk.png 1x40 = #121212 at alpha 235 (92%)",
        "buttons": "26 x 26 with 1 px padding -> 28 px pitch",
        "measured_centres": [553.3, 583.3, 610.0, 640.7, 724.7, 753.3, 781.3, 810.0, 837.3],
    },
}

OUT = {
    "meta": {
        "source_app": "爱奇艺万能联播 GeePlayer 5.4.1.5408",
        "install_dir": r"E:\IQIYI Video\GeePlayer\5.4.1.5408",
        "screenshots_dir": r"D:\AI-Project\至简Player\爱奇艺播放器界面截图",
        "read_only": True,
        "dpi_scale_of_screenshots": 1.5,
        "counts": {
            "xml_files": xmlblob["xml_file_count"],
            "xml_parsed_ok": xmlblob["parsed_ok"],
            "xml_recovered_with_lxml": len(xmlblob["recovered_with_lxml"]),
            "control_nodes": xmlblob["total_control_nodes"],
            "theme_color_keys_total": thm["color_key_count"],
            "theme_color_keys_skinDefault": len(theme_colors.get("skinDefault", {})),
            "theme_color_keys_skinPeeler": len(theme_colors.get("skinPeeler", {})),
            "distinct_color_values": thm["distinct_color_values"],
            "screenshots_measured": len(shots),
            "png_assets_in_skin": 2623,
        },
        "xml_encoding": "all 160 XML files are UTF-8 WITH BOM (utf-8-sig)",
        "theme_json_encoding": "UTF-8 WITH BOM (EF BB BF), 23238 bytes, valid JSON after strip",
    },
    "xml_layouts": layouts,
    "theme_colors": theme_colors,
    "screenshots": shots,
    "asset_sizes": assets,
    "derived": DERIVED,
    "not_obtained": [
        "skin/skinDefault/skinplan/** contains NO .xml files - only PNG/GIF. The layout XMLs all live in "
        "skin/<module>/*.xml; skinDefault only supplies the images referenced by those XMLs.",
        "No dpi150/dpi200 conditional branch exists in any XML: only a filename suffix convention "
        "(*_dpi200.png) which the loader applies at runtime, selected by system scaling.",
        "The real default main-window size is not written in any XML (no <Window size=...> in frame/skin.xml); "
        "only mininfo=580,200 is declared. The 866x589 value is measured from the screenshots.",
        "Drawer bg #0D0D0D measured vs list_bk.png #131314 asset - the difference comes from an undocumented "
        "runtime alpha/`fade` composite; exact formula not recovered.",
        "Text anti-aliasing means measured ink boxes are ~1 logical px larger than the nominal font metrics.",
    ],
}

txt = json.dumps(OUT, ensure_ascii=False, indent=1)
with io.open(os.path.join(DOCS, "01-爱奇艺5.4.1逆向原始数据.json"), "w", encoding="utf-8", newline="\n") as f:
    f.write(txt)
print("wrote 01-爱奇艺5.4.1逆向原始数据.json  %.0f KB" % (len(txt.encode("utf-8")) / 1024.0))
print("xml files:", len(layouts), " theme keys:", sum(len(v) for v in theme_colors.values()),
      " shots:", len(shots))
