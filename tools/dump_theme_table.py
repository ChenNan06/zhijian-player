# -*- coding: utf-8 -*-
"""Dump theme[0] (skinDefault) colour keys, grouped, as a markdown-ready table."""
import json, io, re, sys

BLOB = r"D:\AI-Project\至简Player\docs\_raw_theme.json"
b = json.load(io.open(BLOB, encoding="utf-8"))
cols = b["theme_colors"]

t0 = {k.split("/color/", 1)[1]: v for k, v in cols.items() if k.startswith("theme[0]/color/")}
t1 = {k: v for k, v in cols.items() if k.startswith("theme[1]/color/")}
print("theme[0] (skinDefault) colour keys : %d" % len(t0))
print("theme[1] (skinPeeler)  colour keys : %d" % len(t1))
print("total colour keys                  : %d" % len(cols))

GROUPS = {
    "窗口/框架": ["frame_", "wnd_", "title_", "list_top_line", "dialog", "msgbox", "shadow"],
    "底部控制条": ["color_player", "color_btn", "player_"],
    "播放列表": ["list_"],
    "设置面板": ["setting_", "hotkey"],
    "菜单": ["color_meun", "color_menu", "menu"],
    "搜索/首页": ["search_"],
    "图片查看器": ["image", "img"],
    "其他": [],
}
assigned = set()
out = []
for gname, prefixes in GROUPS.items():
    items = []
    for k in sorted(t0):
        if k in assigned:
            continue
        if gname == "其他" or any(k.startswith(p) for p in prefixes):
            items.append(k)
    for k in items:
        assigned.add(k)
    out.append((gname, items))

with io.open(r"D:\AI-Project\至简Player\docs\_theme_table.md", "w", encoding="utf-8") as f:
    f.write("| 分组 | 键名 | 原文 | 格式 | 归一化 AARRGGBB | #RRGGBB | Alpha |\n")
    f.write("|---|---|---|---|---|---|---|\n")
    for gname, items in out:
        for k in items:
            v = t0[k]
            f.write("| %s | `%s` | `%s` | %s | `%s` | `%s` | %s (%.0f%%) |\n" % (
                gname, k, v["raw"], v["fmt"], v["aarrggbb"], v["rgb"], v["alpha"], v["alpha_pct"]))
        print("  %-12s %3d keys" % (gname, len(items)))

for k in sorted(t0):
    v = t0[k]
    print("%-42s %-16s %-10s %-9s a=%3d" % (k, v["raw"], v["fmt"], v["rgb"], v["alpha"]))
