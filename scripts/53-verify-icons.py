#!/usr/bin/env python
r"""task-40 自检器：视觉识别系统图标 + 42 扩展名注册表绑定（可反复复跑）。

用法：
    python scripts/53-verify-icons.py                 # 全量：核对图标 → 注册两遍验证幂等 → 写报告
    python scripts/53-verify-icons.py --no-register   # 只读核对（不动注册表）
    python scripts/53-verify-icons.py --exe <路径>     # 指定要核对/注册的 exe
    python scripts/53-verify-icons.py --no-preview    # 跳过 shell 图标预览图

判据分两类：
  HARD（失败 → 退出码 1）:
    · 5 枚 ICO 各自含 16/32/48/64/128/256 六尺寸；磁盘文件与"现在重新渲染"逐字节一致（幂等）
    · 主图标底色 #121418(±2)、超椭圆覆盖率（区别于圆/方）、青→蓝色相过渡、1px 边缘光晕、折面接缝
    · 三类媒体图标底色/强调色均值可测，主色相两两相差 ≥30°
    · 注册表 42 个扩展名的 DefaultIcon 全部等于"按 exe 解析出来的类别图标绝对路径"，且文件真实存在
    · 4 个 ProgID 的 DefaultIcon / 命令 / ApplicationIcon / FriendlyTypeName 正确
    · 只写 HKCU（HKLM 下 4 个 ProgID 均不存在）；AssocBackup 42 项 PrevDefault + PrevDefaultIcon 完好
    · 连续注册两次后注册表 dump 与备份完全一致（注册幂等）
  SOFT（不判失败，但逐项如实报告）:
    · 资源管理器/Shell 实际解析到的 DefaultIcon（本机有别的播放器用 Win11
      UserChoiceLatest 哈希锁住了部分扩展名，属 Windows 保护机制，需用户手动点一次）

产出（logs/task40/）：
    selfcheck.json / selfcheck.md / icon-sheet.png / shell-icons.png
"""
from __future__ import annotations

import argparse
import colorsys
import ctypes
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import time
from ctypes import wintypes

from PIL import Image, ImageDraw

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ASSETS = os.path.join(ROOT, "assets")
LOG_DIR = os.path.join(ROOT, "logs", "task40")
SRC = os.path.join(ROOT, "src")

SIZES = (16, 32, 48, 64, 128, 256)
MAIN_BASE = (0x12, 0x14, 0x18)
KIND_FILES = {
    "icon": "icon.ico",
    "audio": "audio.ico",
    "video": "video.ico",
    "image": "image.ico",
    "subtitle": "subtitle.ico",
}
KIND_LABELS = {"icon": "主图标", "audio": "音频", "video": "视频", "image": "图片",
               "subtitle": "字幕"}
#: 每类图标的"纯底色"取样点（避开图形元素、光晕、以及 1px 边缘环）。
SAFE_POINTS_BY_KIND = {
    "icon": ((0.22, 0.18), (0.78, 0.18), (0.22, 0.82),
             (0.78, 0.82), (0.50, 0.11), (0.50, 0.89)),
    "audio": ((0.18, 0.14), (0.82, 0.14), (0.10, 0.30),
              (0.90, 0.30), (0.50, 0.08), (0.18, 0.60)),
    "video": ((0.18, 0.14), (0.82, 0.14), (0.32, 0.10),
              (0.68, 0.10), (0.50, 0.88), (0.30, 0.90)),
    "image": ((0.20, 0.14), (0.85, 0.14), (0.50, 0.10),
              (0.50, 0.88), (0.15, 0.50), (0.85, 0.60)),
    "subtitle": ((0.10, 0.14), (0.90, 0.14), (0.50, 0.10),
                 (0.50, 0.90), (0.10, 0.50), (0.90, 0.50)),
}

failures = []
soft_notes = []


def fail(msg):
    failures.append(msg)
    print("  [FAIL] %s" % msg)


def soft(msg):
    soft_notes.append(msg)
    print("  [soft] %s" % msg)


# ======================================================================================
# 通用图像工具
# ======================================================================================

def load_frame(path, size=256):
    im = Image.open(path)
    im.size = (size, size)
    return im.convert("RGBA").copy()


def hsv(rgb):
    h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    return {"h": round(h * 360.0, 1), "s": round(s, 3), "v": round(v, 3)}


def hue_of(rgb):
    return hsv(rgb)["h"]


def luma(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def hue_dist(a, b):
    d = abs(a - b) % 360.0
    return round(min(d, 360.0 - d), 1)


def base_samples(frame, expected, kind, tol=2):
    rows = []
    for fx, fy in SAFE_POINTS_BY_KIND[kind]:
        x, y = int(round(fx * 255)), int(round(fy * 255))
        rgb = list(frame.getpixel((x, y))[:3])
        dev = max(abs(rgb[i] - expected[i]) for i in range(3))
        rows.append({"point": [fx, fy], "xy": [x, y], "rgb": rgb, "dev": dev})
    worst = max(r["dev"] for r in rows)
    return {"rows": rows, "max_dev": worst, "ok": worst <= tol}


def accent_stats(frame):
    """高饱和像素的均值/色相（= 图标主色，与深色底分开统计）。"""
    tot = [0, 0, 0]
    n = 0
    w, h = frame.size
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b, a = frame.getpixel((x, y))
            if a < 128:
                continue
            _h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            if s > 0.35 and v > 0.25:
                tot[0] += r
                tot[1] += g
                tot[2] += b
                n += 1
    mean = [int(round(c / n)) for c in tot] if n else [0, 0, 0]
    return {"mean_rgb": mean, "hue": hue_of(mean), "sampled_pixels": n}


def read_ico(path):
    """手工解析 ICO 目录（不用 Pillow 的 sizes，避免读到缩放后的缓存尺寸）。"""
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 6:
        return {"error": "文件过短"}
    reserved, typ, count = struct.unpack_from("<HHH", raw, 0)
    entries = []
    for i in range(count):
        w, h, ncol, res, planes, bpp, size, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + 16 * i)
        w = w or 256
        h = h or 256
        entries.append({
            "w": w, "h": h, "bpp": bpp, "bytes": size, "offset": offset,
            "png": raw[offset:offset + 8] == b"\x89PNG\r\n\x1a\n",
        })
    return {"file_bytes": len(raw), "reserved": reserved, "type": typ,
            "count": count, "entries": entries}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_generator():
    path = os.path.join(ROOT, "scripts", "05-make-icon.py")
    spec = importlib.util.spec_from_file_location("zhj_icons_gen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ======================================================================================
# ICO 结构 + 生成脚本幂等
# ======================================================================================

def check_ico_files():
    print("[1] ICO 目录结构")
    res = {}
    for kind, name in KIND_FILES.items():
        path = os.path.join(ASSETS, name)
        if not os.path.isfile(path):
            fail("%s 不存在" % name)
            continue
        d = read_ico(path)
        dims = sorted((e["w"], e["h"]) for e in d.get("entries", []))
        want = [(s, s) for s in SIZES]
        ok = (d.get("reserved") == 0 and d.get("type") == 1 and d.get("count") == 6
              and sorted(dims) == want
              and all(e["w"] == e["h"] for e in d["entries"])
              and all(e["png"] for e in d["entries"]))
        if not ok:
            fail("%s ICO 结构不符: count=%s dims=%s png=%s"
                 % (name, d.get("count"), dims, [e["png"] for e in d.get("entries", [])]))
        d["dims"] = ["%dx%d" % (w, h) for w, h in dims]
        d["ok"] = ok
        res[name] = d
        print("  %-11s %6d bytes  %d 帧  %s" % (name, d["file_bytes"], d["count"],
                                                " ".join(d["dims"])))
    return res


def check_idempotent():
    print("[2] 生成脚本幂等（重新渲染 == 磁盘）")
    gen = load_generator()
    t0 = time.time()
    outputs = gen.expected_outputs()
    res = {}
    for name in sorted(outputs):
        path = os.path.join(ASSETS, name)
        if not os.path.isfile(path):
            fail("%s 不存在" % name)
            continue
        disk = sha256_file(path)
        fresh = sha256_bytes(outputs[name])
        ok = disk == fresh
        if not ok:
            fail("%s 与重新渲染的字节不一致（非幂等）" % name)
        res[name] = {"disk_sha256": disk, "fresh_sha256": fresh, "same": ok,
                     "bytes": len(outputs[name])}
        print("  %-11s sha256=%s %s" % (name, disk[:16], "一致" if ok else "不一致"))
    res["_elapsed_s"] = round(time.time() - t0, 2)
    return res


# ======================================================================================
# 主图标像素判据
# ======================================================================================

def check_main_icon():
    print("[3] 主图标像素判据")
    path = os.path.join(ASSETS, "icon.ico")
    frame = load_frame(path, 256)
    res = {}
    # 3.1 底色
    b = base_samples(frame, MAIN_BASE, "icon", tol=2)
    res["base_color"] = b
    print("  底色取样 %s  最大偏差=%d" % (
        ["#%02X%02X%02X" % tuple(r["rgb"]) for r in b["rows"]], b["max_dev"]))
    if not b["ok"]:
        fail("主图标底色不是 #121418(±2)：最大偏差 %d" % b["max_dev"])
    # 3.2 超椭圆覆盖率（Squircle ≈ 0.82，圆 ≈ 0.69，方 ≈ 0.88）
    alpha = frame.getchannel("A")
    opaque = sum(alpha.histogram()[129:])
    coverage = opaque / (256.0 * 256.0)
    res["squircle_coverage"] = round(coverage, 4)
    print("  超椭圆覆盖率=%.4f" % coverage)
    if not (0.79 <= coverage <= 0.86):
        fail("轮廓不是超椭圆（覆盖率 %.4f，应在 0.79~0.86）" % coverage)
    # 3.3 青→蓝渐变：在三角形内部沿三条水平线多点取样（x 都落在 0.37~0.73，
    #     避开三角形圆角边缘的 AA 混色 —— 实测 x=0.80 已在形状外）
    xs = (0.37, 0.46, 0.55, 0.64, 0.73)
    rows = []
    for fy in (0.46, 0.54):
        row = []
        for fx in xs:
            rgb = list(frame.getpixel((int(fx * 255), int(fy * 255)))[:3])
            row.append({"x": fx, "y": fy, "rgb": rgb, "hue": hue_of(rgb)})
        rows.append(row)
    samples = [s for row in rows for s in row]
    hues = [s["hue"] for s in samples]
    span = round(max(hues) - min(hues), 1)
    mono = all(row[i + 1]["hue"] >= row[i]["hue"] - 3
               for row in rows for i in range(len(row) - 1))
    res["gradient"] = {"samples": rows, "flat": samples, "span": span,
                       "monotonic": mono, "start_hue": hues[0], "end_hue": hues[-1]}
    print("  渐变取样色相 y=0.46: %s" % [s["hue"] for s in rows[0]])
    print("  渐变取样色相 y=0.54: %s  span=%.1f°" % ([s["hue"] for s in rows[1]], span))
    if span < 15 or not mono:
        fail("徽标渐变不足（跨度 %.1f°，需 ≥15° 且单调）" % span)
    if not (183.0 <= hues[0] <= 200.0):
        fail("渐变起点色相 %.1f° 不是电青(#00D2FF≈190.6°)" % hues[0])
    if not (205.0 <= hues[-1] <= 225.0):
        fail("渐变终点色相 %.1f° 不指向宝蓝(#0066FF≈216°)" % hues[-1])
    # 3.4 边缘 1px 光晕
    halo = measure_halo(frame)
    res["halo"] = halo
    if not halo["ok"]:
        fail("边缘 1px 液态折射光晕不成立（%s）" % halo["summary"])
    # 3.5 折面接缝
    seam = measure_seam(frame)
    res["seam"] = seam
    if not seam["ok"]:
        fail("多面折痕缺失（中心 %.1f vs 两侧 %s）"
             % (seam["center_luma"], seam["neighbors_luma"]))
    return res


def measure_halo(frame):
    """在四条边的中心扫描，量边缘亮环相对内侧底色的亮度差与宽度。"""
    w, h = frame.size
    scans = {
        "left": [(x, h // 2) for x in range(0, w)],
        "right": [(x, h // 2) for x in range(w - 1, -1, -1)],
        "top": [(w // 2, y) for y in range(0, h)],
        "bottom": [(w // 2, y) for y in range(h - 1, -1, -1)],
    }
    rows = {}
    best = {"delta": -1.0}
    for key, pts in scans.items():
        prof = []
        for (x, y) in pts:
            r, g, b, a = frame.getpixel((x, y))
            prof.append({"xy": [x, y], "rgba": [r, g, b, a],
                         "luma": round(luma((r, g, b)), 1)})
        edge = None
        for i, p in enumerate(prof):
            if p["rgba"][3] >= 128:
                edge = i
                break
        if edge is None or edge + 14 >= len(prof):
            continue
        inner = prof[edge + 14:edge + 24]
        base_l = sorted(p["luma"] for p in inner)[len(inner) // 2]
        ring = prof[edge:edge + 4]
        peak = max(ring, key=lambda p: p["luma"])
        delta = round(peak["luma"] - base_l, 1)
        width = 0
        for p in prof[edge:edge + 8]:
            if p["luma"] > base_l + 5:
                width += 1
            else:
                break
        rows[key] = {"edge_xy": prof[edge]["xy"], "edge_rgba": prof[edge]["rgba"],
                     "ring_peak_luma": peak["luma"], "base_luma": base_l,
                     "delta": delta, "width_px": width,
                     "ring_hue": hue_of(tuple(peak["rgba"][:3]))}
        if delta > best["delta"]:
            best = dict(rows[key], side=key)
    ok = (best["delta"] >= 10.0 and 1 <= best["width_px"] <= 3
          and 170.0 <= best["ring_hue"] <= 225.0)
    summary = ("最强边=%s Δluma=%.1f 宽度=%dpx 色相=%.1f°"
               % (best.get("side"), best["delta"], best["width_px"],
                  best.get("ring_hue", 0)))
    print("  光晕: %s" % summary)
    return {"sides": rows, "best": best, "ok": ok, "summary": summary}


def measure_seam(frame):
    w, h = frame.size
    cx, cy = w // 2, h // 2
    center = luma(frame.getpixel((cx, cy))[:3])
    neighbors = [luma(frame.getpixel((cx, cy - 5))[:3]),
                 luma(frame.getpixel((cx, cy + 5))[:3])]
    mn = min(neighbors)
    ok = center < mn - 8.0
    print("  折面接缝: 中心 luma=%.1f 两侧=%s" % (center, [round(v, 1) for v in neighbors]))
    return {"center_luma": round(center, 1),
            "neighbors_luma": [round(v, 1) for v in neighbors], "ok": ok}


# ======================================================================================
# 媒体图标像素判据（尺寸/底色/主色相/图形元素）
# ======================================================================================

def _count(frame, box, pred):
    x0, y0, x1, y1 = [int(v) for v in box]
    n = 0
    for y in range(max(0, y0), min(frame.size[1], y1)):
        for x in range(max(0, x0), min(frame.size[0], x1)):
            r, g, b, a = frame.getpixel((x, y))
            if a >= 128 and pred(r, g, b):
                n += 1
    return n


def _count_disc(frame, cx, cy, radius, pred):
    """只统计圆心半径内的像素（避免方形包围盒四角把"底色"算成挖空/线条）。"""
    n = 0
    r2 = radius * radius
    for y in range(max(0, int(cy - radius)), min(frame.size[1], int(cy + radius) + 1)):
        for x in range(max(0, int(cx - radius)), min(frame.size[0], int(cx + radius) + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 > r2:
                continue
            rr, gg, bb, aa = frame.getpixel((x, y))
            if aa >= 128 and pred(rr, gg, bb):
                n += 1
    return n


def _region_stats(frame, fx0, fy0, fx1, fy1, pred):
    """矩形区域内的命中像素数 + 平均 V（亮度），用于"双峰视差"的明暗对比。"""
    W, H = frame.size
    n = 0
    tot = 0.0
    for y in range(int(fy0 * H), int(fy1 * H)):
        for x in range(int(fx0 * W), int(fx1 * W)):
            rr, gg, bb, aa = frame.getpixel((x, y))
            if aa >= 128 and pred(rr, gg, bb):
                n += 1
                tot += _val(rr, gg, bb)
    return {"count": n, "mean_value": round(tot / n, 3) if n else 0.0}


def _sat(r, g, b):
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)[1]


def _val(r, g, b):
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)[2]


def _run_count(values):
    runs = 0
    prev = False
    for v in values:
        if v and not prev:
            runs += 1
        prev = v
    return runs


def check_media_icons():
    print("[4] 媒体图标像素判据")
    gen = load_generator()
    res = {}
    for kind in ("audio", "video", "image", "subtitle"):
        path = os.path.join(ASSETS, KIND_FILES[kind])
        frame = load_frame(path, 256)
        base = tuple(gen.KINDS[kind]["base"])
        b = base_samples(frame, base, kind, tol=2)
        acc = accent_stats(frame)
        item = {"base_color": b, "accent": acc}
        print("  %-8s 底色 #%02X%02X%02X(偏差%d)  主色均值 #%02X%02X%02X 色相=%.1f°"
              % (kind, base[0], base[1], base[2], b["max_dev"],
                 acc["mean_rgb"][0], acc["mean_rgb"][1], acc["mean_rgb"][2], acc["hue"]))
        if not b["ok"]:
            fail("%s 底色偏差 %d > 2" % (kind, b["max_dev"]))
        res[kind] = item
        if kind == "audio":
            item["elements"] = check_audio_elements(frame, gen)
        elif kind == "video":
            item["elements"] = check_video_elements(frame, gen)
        elif kind == "image":
            item["elements"] = check_image_elements(frame, gen)
        else:
            item["elements"] = check_subtitle_elements(frame, gen)
        if not item["elements"]["ok"]:
            fail("%s 图形元素像素特征不达标：%s" % (kind, item["elements"]["summary"]))
    # 色相两两可分（音频/视频/图片）
    trio = {k: res[k]["accent"]["hue"] for k in ("audio", "video", "image")}
    pairs = {}
    keys = list(trio)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            pairs["%s-%s" % (keys[i], keys[j])] = hue_dist(trio[keys[i]], trio[keys[j]])
    res["_hues"] = trio
    res["_pairwise"] = pairs
    print("  三主色相: %s  两两差: %s" % (trio, pairs))
    for pair, d in pairs.items():
        if d < 30.0:
            fail("主色相可分性不足 %s 仅差 %.1f°（需 ≥30°）" % (pair, d))
    return res


def check_audio_elements(frame, gen):
    """单色音乐徽章（实心圆 + 挖空音符）+ 下方灵动声波。"""
    W = 256
    cx, cy, R = W / 2.0, 0.425 * W, 0.275 * W
    base = tuple(gen.KINDS["audio"]["base"])

    def bright(r, g, b):
        return _sat(r, g, b) > 0.55 and _val(r, g, b) > 0.55

    def dark(r, g, b):
        return max(abs(r - base[0]), abs(g - base[1]), abs(b - base[2])) <= 10

    badge = _count_disc(frame, cx, cy, R, bright)
    note = _count_disc(frame, cx, cy, R, dark)
    wave = _count(frame, (0.10 * W, 0.68 * W, 0.90 * W, 0.88 * W),
                  lambda r, g, b: _sat(r, g, b) > 0.45 and _val(r, g, b) > 0.35)
    ok = badge > 8000 and note > 800 and wave > 500
    summary = ("徽章亮像素=%d 音符挖空=%d 声波=%d" % (badge, note, wave))
    print("    音频元素: %s" % summary)
    return {"badge_px": badge, "note_px": note, "wave_px": wave, "ok": ok,
            "summary": summary}


def check_video_elements(frame, gen):
    """胶片齿孔（左右各 4 个）+ 切角播放视窗 + 播放三角挖空。"""
    W = 256
    base = tuple(gen.KINDS["video"]["base"])

    def bright(r, g, b):
        return _sat(r, g, b) > 0.25 and _val(r, g, b) > 0.32

    left_runs = right_runs = 0
    for x0, x1 in ((0.085, 0.165), (0.835, 0.915)):
        rows = []
        for y in range(int(0.20 * W), int(0.82 * W), 2):
            hit = any(bright(*frame.getpixel((x, y))[:3])
                      for x in range(int(x0 * W), int(x1 * W)))
            rows.append(hit)
        runs = _run_count(rows)
        if x0 < 0.5:
            left_runs = runs
        else:
            right_runs = runs
    corner_px = frame.getpixel((int(0.790 * W), int(0.275 * W)))[:3]
    inside_px = frame.getpixel((int(0.500 * W), int(0.320 * W)))[:3]
    chamfer_ok = (max(abs(corner_px[i] - base[i]) for i in range(3)) <= 8
                  and _val(*inside_px) > 0.35)
    center_px = frame.getpixel((W // 2, W // 2))[:3]
    play_ok = max(abs(center_px[i] - base[i]) for i in range(3)) <= 8
    ok = (left_runs == 4 and right_runs == 4 and chamfer_ok and play_ok)
    summary = ("左齿孔=%d 右齿孔=%d 切角=%s 播放挖空=%s"
               % (left_runs, right_runs, chamfer_ok, play_ok))
    print("    视频元素: %s" % summary)
    return {"left_holes": left_runs, "right_holes": right_runs,
            "chamfer_ok": chamfer_ok, "play_cutout_ok": play_ok, "ok": ok,
            "summary": summary}


def check_image_elements(frame, gen):
    """双峰视差（两座山、近峰更亮）+ 光圈几何（亮圆盘 + 六边形光圈暗线）。"""
    W = 256
    base = tuple(gen.KINDS["image"]["base"])
    sun_cx, sun_cy, sun_r = 0.655 * W, 0.305 * W, 0.155 * W

    def bright(r, g, b):
        return _sat(r, g, b) > 0.30 and _val(r, g, b) > 0.55

    def darkish(r, g, b):
        # 光圈叶片线 = 底座色与薄荷盘按 0.78 alpha 混合后的暗线（不是纯底色）
        return _val(r, g, b) < 0.50 and _sat(r, g, b) > 0.20

    def green(r, g, b):
        # 山体：绿色相、明显饱和、亮度高于底座（#07261A 的 V≈0.149，必须排除）
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        return 120 <= h * 360 <= 185 and s > 0.35 and 0.20 <= v <= 0.98

    sun = _count_disc(frame, sun_cx, sun_cy, sun_r, bright)
    iris = _count_disc(frame, sun_cx, sun_cy, sun_r, darkish)
    # 双峰：y=0.50W 水平线上，绿色山体应恰好分成两段（远峰 + 近峰，中间被切出缺口）
    scan_y = int(0.50 * W)
    runs = _run_count([
        green(*frame.getpixel((x, scan_y))[:3])
        for x in range(int(0.05 * W), int(0.95 * W))
    ])
    # 视差：远峰（左）整体更暗，近峰（右）更亮
    far = _region_stats(frame, 0.16, 0.55, 0.30, 0.78, green)
    near = _region_stats(frame, 0.62, 0.55, 0.88, 0.78, green)
    ok = (sun > 1200 and iris > 200 and runs == 2
          and far["count"] > 300 and near["count"] > 300
          and near["mean_value"] > far["mean_value"] + 0.10)
    summary = ("光圈亮=%d 光圈线=%d 双峰扫描段数=%d 远峰(v=%.2f,%dpx) 近峰(v=%.2f,%dpx)"
               % (sun, iris, runs, far["mean_value"], far["count"],
                  near["mean_value"], near["count"]))
    print("    图片元素: %s" % summary)
    return {"sun_px": sun, "iris_px": iris, "peak_runs": runs,
            "far_peak": far, "near_peak": near, "ok": ok, "summary": summary}


def check_subtitle_elements(frame, gen):
    """字幕卡片 + 两行字幕条（两段暗色横条）。"""
    W = 256
    base = tuple(gen.KINDS["subtitle"]["base"])

    def darkish(r, g, b):
        return max(abs(r - base[0]), abs(g - base[1]), abs(b - base[2])) <= 12

    card = _count(frame, (0.16 * W, 0.29 * W, 0.84 * W, 0.71 * W),
                  lambda r, g, b: _sat(r, g, b) > 0.45 and _val(r, g, b) > 0.35)
    rows = []
    for y in range(int(0.30 * W), int(0.70 * W)):
        px = frame.getpixel((int(0.40 * W), y))[:3]
        rows.append(darkish(*px))
    bars = _run_count(rows)
    ok = card > 8000 and bars == 2
    summary = "卡片亮像素=%d 字幕条数=%d" % (card, bars)
    print("    字幕元素: %s" % summary)
    return {"card_px": card, "bars": bars, "ok": ok, "summary": summary}


# ======================================================================================
# 注册表：42 扩展名 × 4 ProgID × 备份
# ======================================================================================

def shlwapi_query(ext, assocstr=15):
    """AssocQueryStringW(ASSOCSTR_DEFAULTICON=15)：Shell 实际会用的图标位置。"""
    if not hasattr(shlwapi_query, "_dll"):
        shlwapi_query._dll = ctypes.WinDLL("shlwapi", use_last_error=True)
    dll = shlwapi_query._dll
    buf = ctypes.create_unicode_buffer(4096)
    n = wintypes.DWORD(len(buf))
    hr = dll.AssocQueryStringW(0, assocstr, ext, "open", buf, ctypes.byref(n))
    return buf.value if hr == 0 else ""


def icon_location_path(value):
    """从 "path,0" / "path",0 / path 里取出路径。"""
    s = str(value or "").strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        s = s[1:end] if end > 0 else s[1:]
    return s.split(",")[0].strip().strip('"')


def norm(path):
    return os.path.normcase(os.path.normpath(str(path)))


def uic_progid(ext):
    import winreg
    path = (r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
            + "\\" + ext + r"\UserChoiceLatest\ProgId")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_QUERY_VALUE) as k:
            return str(winreg.QueryValueEx(k, "ProgId")[0])
    except OSError:
        return ""


def backup_entries():
    import winreg
    sys.path.insert(0, SRC)
    from zhijian.platform import win as W
    out = {}
    for ext in W.assoc_all_exts():
        path = W.ASSOC_BACKUP + "\\" + ext
        item = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_QUERY_VALUE) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    name, val, _typ = winreg.EnumValue(k, i)
                    item[name] = val
        except OSError:
            pass
        out[ext] = item
    return out


def check_mechanism(exe, W):
    """机制自证：临时扩展名（不受本机 UserChoice 干扰）是否解析到我们的 ProgID 图标。

    这一步回答"到底是不是程序绑定写错了"：把临时 .zhjprobe 关联到 ZhiJianPlayer.Audio
    后，Shell 的 ASSOCSTR_DEFAULTICON 必须返回 audio.ico。若返回，说明 42 项里那些
    "未生效"的扩展名是 Windows 的 UserChoiceLatest 保护在起作用，而不是我们的绑定错。
    临时键用完即删（不碰任何真实扩展名）。
    """
    import winreg
    ext = ".zhjprobe"
    progid = W.ASSOC_PROGID_AUDIO
    want = W.assoc_default_icon_value(exe, progid)
    base = r"Software\Classes" + "\\" + ext
    keys = [base, base + r"\DefaultIcon"]
    res = {"ext": ext, "progid": progid, "expect": want, "actual": "", "ok": False}
    try:
        for k in keys:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, k, 0, winreg.KEY_SET_VALUE):
                pass
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, keys[0], 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, progid)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, keys[1], 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, want)
        actual = shlwapi_query(ext)
        res["actual"] = actual
        res["ok"] = (bool(actual) and norm(icon_location_path(actual))
                     == norm(icon_location_path(want)))
    finally:
        for k in reversed(keys):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
            except OSError:
                pass
    return res


def check_registry(exe, do_register):
    print("[5] 注册表绑定（42 扩展名 × 4 ProgID × 备份）")
    sys.path.insert(0, SRC)
    from zhijian.platform import win as W
    res = {"exe": exe, "registered": False, "idempotent": None, "errors": []}
    if not os.path.isfile(exe):
        fail("exe 不存在：%s" % exe)
        return res
    if do_register:
        rep1 = W.register_associations(exe, with_default=True)
        d1 = W.dump_associations(exe)
        b1 = backup_entries()
        rep2 = W.register_associations(exe, with_default=True)
        d2 = W.dump_associations(exe)
        b2 = backup_entries()
        same_dump = d1 == d2
        same_backup = b1 == b2
        res["registered"] = True
        res["idempotent"] = {"dump_same": same_dump, "backup_same": same_backup,
                             "errors_run1": rep1.get("errors", []),
                             "errors_run2": rep2.get("errors", []),
                             "missing_icons": rep1.get("missing_icons", [])}
        print("  注册两遍: dump 一致=%s 备份一致=%s 缺失图标=%s"
              % (same_dump, same_backup, rep1.get("missing_icons") or "无"))
        if not same_dump:
            fail("注册不幂等：两次 dump 不一致")
        if not same_backup:
            fail("注册不幂等：两次备份不一致")
        if rep1.get("missing_icons"):
            fail("缺少专用图标文件：%s" % rep1.get("missing_icons"))
        if rep1.get("errors") or rep2.get("errors"):
            fail("注册过程报错：%s / %s" % (rep1.get("errors"), rep2.get("errors")))
    dump = W.dump_associations(exe)
    expected = W.assoc_ext_icon_values(exe)
    ext_map = W.assoc_ext_map()
    labels = {p: l for p, l, _e in W.ASSOC_KINDS}

    # 5.1 逐扩展名
    rows = []
    for ext, progid in ext_map.items():
        actual = dump["Extensions"][ext].get("DefaultIcon") or ""
        want = expected[ext]
        want_path = icon_location_path(want)
        got_path = icon_location_path(actual)
        match = norm(got_path) == norm(want_path) if got_path else False
        exists = os.path.isfile(want_path)
        eff = shlwapi_query(ext)
        eff_path = icon_location_path(eff)
        effective = bool(eff_path) and norm(eff_path) == norm(want_path)
        reason = ""
        if not effective:
            uic = uic_progid(ext)
            if uic and uic != progid:
                reason = "Win11 UserChoiceLatest 锁定 %s" % uic
            else:
                reason = "Shell 解析到 %s" % (eff or "（空）")
        rows.append({
            "ext": ext, "progid": progid, "label": labels[progid],
            "icon_file": os.path.basename(want_path),
            "actual": actual, "expected": want, "match": match,
            "file_exists": exists, "shell_effective": effective,
            "shell_value": eff, "shell_reason": reason,
        })
        if not match:
            fail("%s DefaultIcon 不符：实际=%r 期望=%r" % (ext, actual, want))
        if not exists:
            fail("%s 的类别图标文件不存在：%s" % (ext, want_path))
    res["extensions"] = rows
    n_eff = sum(1 for r in rows if r["shell_effective"])
    res["shell_effective_count"] = n_eff
    print("  42 扩展名 DefaultIcon 全部匹配=%s；Shell 实际生效=%d/42"
          % (all(r["match"] for r in rows), n_eff))
    for r in rows:
        if not r["shell_effective"]:
            soft("%s 图标未在 Shell 生效（%s）" % (r["ext"], r["shell_reason"]))
    # 5.2 ProgID
    prog_rows = []
    for progid, label, exts in W.ASSOC_KINDS:
        item = dump["ProgIDs"][progid]
        want_icon = W.assoc_default_icon_value(exe, progid)
        app_icon = W.assoc_app_icon_value(exe)
        ok_icon = norm(icon_location_path(item.get("DefaultIcon"))) == norm(
            icon_location_path(want_icon))
        ok_cmd = item.get("command") == W.assoc_open_command(exe)
        prog_rows.append({
            "progid": progid, "label": label, "ext_count": len(exts),
            "default_icon": item.get("DefaultIcon"), "expect_icon": want_icon,
            "icon_ok": ok_icon, "command": item.get("command"),
            "command_ok": ok_cmd,
            "friendly_type_name": item.get("FriendlyTypeName"),
            "application_icon": item.get("Application").get("ApplicationIcon"),
            "application_icon_ok": norm(icon_location_path(
                item.get("Application").get("ApplicationIcon"))) == norm(
                    icon_location_path(app_icon)),
            "exists": item.get("exists"),
        })
        if not (ok_icon and ok_cmd and item.get("exists")):
            fail("ProgID %s 不正确（icon=%s cmd=%s exists=%s）"
                 % (progid, ok_icon, ok_cmd, item.get("exists")))
        if not prog_rows[-1]["application_icon_ok"]:
            fail("ProgID %s 的 ApplicationIcon 不是主图标" % progid)
    res["progids"] = prog_rows
    print("  4 个 ProgID: %s" % ["%s(%s)" % (r["progid"],
                                              "OK" if r["icon_ok"] else "BAD")
                                for r in prog_rows])
    # 5.2b 机制自证：干净临时关联能解析到我们的图标（排除"绑定写错"）
    mech = check_mechanism(exe, W)
    res["mechanism"] = mech
    print("  机制自证（临时扩展名 %s）: 期望=%s 实际=%s -> %s"
          % (mech["ext"], mech["expect"], mech["actual"] or "（空）",
             "OK" if mech["ok"] else "FAIL"))
    if not mech["ok"]:
        fail("机制自证失败：干净临时关联也解析不到我们的图标（绑定逻辑有问题）")
    # 5.3 只写 HKCU
    hklm = dump.get("HKLM_ProgIDs", {})
    res["hklm_progids"] = hklm
    if any(hklm.values()):
        fail("HKLM 下出现了我们的 ProgID（违反 HKCU-only）：%s" % hklm)
    win_py = os.path.join(SRC, "zhijian", "platform", "win.py")
    with open(win_py, encoding="utf-8") as f:
        src = f.read()
    hklm_lines = [i + 1 for i, line in enumerate(src.splitlines())
                  if "HKEY_LOCAL_MACHINE" in line]
    res["hklm_source_lines"] = hklm_lines
    print("  HKCU-only: HKLM ProgID 均不存在=%s；源码 HKLM 引用行=%s"
          % (not any(hklm.values()), hklm_lines))
    # 5.4 备份键
    backups = backup_entries()
    res["backup"] = backups
    miss = [ext for ext in ext_map
            if "PrevDefault" not in backups.get(ext, {})
            or "PrevDefaultIcon" not in backups.get(ext, {})]
    res["backup_missing"] = miss
    res["backup_stats"] = {
        "prev_default_absent": sum(
            1 for ext in ext_map
            if backups.get(ext, {}).get("PrevDefault") == "__absent__"),
        "prev_icon_absent": sum(
            1 for ext in ext_map
            if backups.get(ext, {}).get("PrevDefaultIcon") == "__absent__"),
    }
    print("  备份键: 42 项 PrevDefault+PrevDefaultIcon 缺失=%s  %s"
          % (miss or "无", res["backup_stats"]))
    if miss:
        fail("AssocBackup 备份不完整：缺少 %s" % miss)
    return res


# ======================================================================================
# 预览图 + 报告
# ======================================================================================

def make_icon_sheet():
    os.makedirs(LOG_DIR, exist_ok=True)
    cell = 264
    sheet = Image.new("RGB", (cell * 6 + 16, cell * 5 + 16), (24, 26, 30))
    d = ImageDraw.Draw(sheet)
    for row, kind in enumerate(KIND_FILES):
        for col, size in enumerate((256, 128, 64, 48, 32, 16)):
            with Image.open(os.path.join(ASSETS, KIND_FILES[kind])) as im:
                im.size = (size, size)
                f = im.convert("RGBA").copy()
            if size < 256:
                f = f.resize((256, 256), Image.NEAREST)
            box = [16 + col * cell, 16 + row * cell,
                   16 + col * cell + 256, 16 + row * cell + 256]
            d.rectangle(box, fill=(34, 36, 42))
            sheet.paste(f, (box[0], box[1]), f)
    p = os.path.join(LOG_DIR, "icon-sheet.png")
    sheet.save(p)
    return p


def make_shell_icon_sheet():
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "windows")
        from PySide6.QtCore import QFileInfo, QSize
        from PySide6.QtGui import QColor, QFont, QImage, QPainter
        from PySide6.QtWidgets import QApplication, QFileIconProvider
    except Exception as exc:  # pragma: no cover - 无 Qt 环境
        return {"ok": False, "error": "PySide6 不可用: %s" % exc}
    try:
        _app = QApplication.instance() or QApplication(sys.argv[:1])
        tmp = tempfile.mkdtemp(prefix="zhj_shell_icons_")
        groups = [
            ("audio", ".mp3 .flac .m4a .aac .wav .ogg .opus .wma .ape .ac3 .mka .dts"),
            ("video", ".mp4 .mkv .mov .avi .wmv .flv .webm .m4v .ts .m2ts .mpg .mpeg "
                      ".3gp .vob .rmvb"),
            ("image", ".jpg .jpeg .png .gif .bmp .webp .avif .tif .tiff .ico"),
            ("subtitle", ".srt .ass .ssa .vtt .sub"),
        ]
        prov = QFileIconProvider()
        cell_w, cell_h = 64, 78
        max_cols = 15
        width = 120 + cell_w * max_cols + 16
        height = 30 + cell_h * len(groups) + 16
        img = QImage(width, height, QImage.Format_RGBA8888)
        img.fill(QColor(24, 26, 30))
        painter = QPainter(img)
        painter.setPen(QColor(210, 214, 222))
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        painter.drawText(8, 20, "Shell 实际解析到的文件图标（按类别/扩展名）")
        for gi, (kind, exts) in enumerate(groups):
            y = 30 + gi * cell_h
            painter.drawText(8, y + 40, KIND_LABELS[kind])
            for ci, ext in enumerate(exts.split()):
                path = os.path.join(tmp, "probe" + ext)
                with open(path, "wb") as f:
                    f.write(b"")
                pm = prov.icon(QFileInfo(path)).pixmap(QSize(44, 44))
                x = 120 + ci * cell_w
                painter.drawPixmap(x, y + 2, 44, 44, pm)
                painter.drawText(x, y + 62, ext)
        painter.end()
        out = os.path.join(LOG_DIR, "shell-icons.png")
        img.save(out)
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": True, "path": out}
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": "生成 shell 图标预览失败: %s" % exc}


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def write_report(result):
    os.makedirs(LOG_DIR, exist_ok=True)
    json_path = os.path.join(LOG_DIR, "selfcheck.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    reg = result.get("registry", {})
    ext_rows = reg.get("extensions", [])
    main = result["main"]
    rows_out = []
    rows_out.append("# task-40 自检报告（视觉识别系统：图标）")
    rows_out.append("")
    rows_out.append("> 由 scripts/53-verify-icons.py 自动生成，时间 %s。"
                    % result["generated_at"])
    rows_out.append("")
    rows_out.append("## 0. 结论")
    rows_out.append("")
    rows_out.append("- HARD 失败项：**%d**%s" % (
        len(result["failures"]),
        "（" + "；".join(result["failures"][:6]) + "）"
        if result["failures"] else "（全部通过）"))
    rows_out.append("- SOFT 提示项：**%d**（多数是本机其它播放器占用 Win11 受保护默认项，"
                    "详见 5.3）" % len(result["soft_notes"]))
    rows_out.append("- Shell 实际生效：**%d/42**" % reg.get("shell_effective_count", 0))
    rows_out.append("")
    rows_out.append("## 1. ICO 目录（6 尺寸逐项）")
    rows_out.append("")
    rows = []
    for name, d in result.get("ico", {}).items():
        if "dims" in d:
            rows.append([name, d["file_bytes"], d["count"], " ".join(d["dims"]),
                         "PNG 帧" if all(e["png"] for e in d["entries"]) else "含 DIB"])
    rows_out.append(md_table(["文件", "字节", "帧数", "尺寸", "帧格式"], rows))
    rows_out.append("")
    rows_out.append("## 2. 生成脚本幂等（重新渲染的字节 == 磁盘）")
    rows_out.append("")
    idem = result.get("idempotent_icons", {})
    rows = []
    for name, d in idem.items():
        if name.startswith("_"):
            continue
        rows.append([name, d["disk_sha256"][:16], d["fresh_sha256"][:16],
                     "一致" if d["same"] else "**不一致**"])
    rows_out.append(md_table(["文件", "磁盘 sha256", "重新渲染 sha256", "幂等"], rows))
    rows_out.append("")
    rows_out.append("## 3. 主图标像素判据")
    rows_out.append("")
    rows_out.append("- 底色 #121418：最大偏差 **%d**（%s）" % (
        main["base_color"]["max_dev"],
        "，".join("#%02X%02X%02X" % tuple(r["rgb"])
                 for r in main["base_color"]["rows"])))
    rows_out.append("- 超椭圆覆盖率：**%.4f**（Squircle≈0.82 / 圆≈0.69 / 方≈0.88）"
                    % main["squircle_coverage"])
    rows_out.append("- 青→蓝渐变取样（y=0.46 / y=0.54 两行）：")
    rows_out.append("")
    rows_out.append(md_table(
        ["x", "y", "RGB", "色相"],
        [[s["x"], s["y"], "#%02X%02X%02X" % tuple(s["rgb"]), "%.1f°" % s["hue"]]
         for s in main["gradient"]["flat"]]))
    rows_out.append("")
    rows_out.append("  - 跨度 **%.1f°**，起点 %.1f°，终点 %.1f°" % (
        main["gradient"]["span"], main["gradient"]["start_hue"],
        main["gradient"]["end_hue"]))
    rows_out.append("- 边缘 1px 液态折射光晕：")
    rows_out.append("")
    rows = []
    for side, d in main["halo"]["sides"].items():
        rows.append([side, d["edge_xy"], "#%02X%02X%02X" % tuple(d["edge_rgba"][:3]),
                     d["ring_peak_luma"], d["base_luma"], d["delta"],
                     d["width_px"], "%.1f°" % d["ring_hue"]])
    rows_out.append(md_table(
        ["边", "边缘像素", "色", "环 luma", "内侧 luma", "Δ", "宽度", "色相"], rows))
    rows_out.append("")
    rows_out.append("- 折面接缝：中心 luma **%.1f** vs 两侧 %s（%s）" % (
        main["seam"]["center_luma"], main["seam"]["neighbors_luma"],
        "成立" if main["seam"]["ok"] else "缺失"))
    rows_out.append("")
    rows_out.append("## 4. 三类媒体图标")
    rows_out.append("")
    rows_out.append("### 4.1 底色 / 主色均值 / 色相")
    rows_out.append("")
    rows = []
    for kind in ("audio", "video", "image", "subtitle"):
        d = result["media"][kind]
        rows.append([KIND_LABELS[kind],
                     "#%02X%02X%02X" % tuple(d["base_color"]["rows"][0]["rgb"]),
                     d["base_color"]["max_dev"],
                     "#%02X%02X%02X" % tuple(d["accent"]["mean_rgb"]),
                     "%.1f°" % d["accent"]["hue"], d["accent"]["sampled_pixels"]])
    rows_out.append(md_table(["类别", "底色(实测)", "偏差", "主色均值", "色相角",
                              "采样像素"], rows))
    rows_out.append("")
    rows_out.append("### 4.2 图形元素像素特征")
    rows_out.append("")
    rows = []
    for kind in ("audio", "video", "image", "subtitle"):
        d = result["media"][kind]["elements"]
        rows.append([KIND_LABELS[kind], d["summary"],
                     "通过" if d["ok"] else "**不通过**"])
    rows_out.append(md_table(["类别", "实测像素特征", "结论"], rows))
    rows_out.append("")
    rows_out.append("### 4.3 三主色相两两可分")
    rows_out.append("")
    rows_out.append("- 音频 %.1f° / 视频 %.1f° / 图片 %.1f°"
                    % tuple(result["media"]["_hues"][k]
                            for k in ("audio", "video", "image")))
    rows_out.append("- 两两色相角差：%s（判据 ≥30°）"
                    % json.dumps(result["media"]["_pairwise"], ensure_ascii=False))
    rows_out.append("")
    rows_out.append("## 5. 注册表绑定（42 扩展名 → 类 → DefaultIcon）")
    rows_out.append("")
    rows_out.append("注册目标 exe：%s" % reg.get("exe", ""))
    rows_out.append("")
    rows_out.append("- 备份键：42 项 PrevDefault+PrevDefaultIcon 缺失 **%s**"
                    % (reg.get("backup_missing") or "无"))
    rows_out.append("- HKCU-only：HKLM 下 4 个 ProgID 均不存在 = **%s**；"
                    "win.py 中 HKLM 引用行=%s"
                    % (not any(reg.get("hklm_progids", {}).values()),
                       reg.get("hklm_source_lines")))
    rows_out.append("- 注册幂等（连续两遍 dump/备份一致）：%s"
                    % json.dumps(reg.get("idempotent"), ensure_ascii=False))
    rows_out.append("")
    rows_out.append("### 5.1 4 个 ProgID")
    rows_out.append("")
    rows = []
    for p in reg.get("progids", []):
        rows.append([p["progid"], p["label"], p["ext_count"],
                     "是" if p["icon_ok"] else "**否**",
                     "是" if p["command_ok"] else "**否**",
                     "是" if p["exists"] else "**否**",
                     p["application_icon"]])
    rows_out.append(md_table(["ProgID", "类型", "扩展名数", "DefaultIcon", "命令",
                              "键存在", "ApplicationIcon"], rows))
    rows_out.append("")
    rows_out.append("### 5.2 42 扩展名逐项")
    rows_out.append("")
    rows = []
    for r in ext_rows:
        rows.append([r["ext"], r["progid"], r["icon_file"],
                     "✓" if r["match"] else "**✗**",
                     "✓" if r["file_exists"] else "**✗**",
                     "✓" if r["shell_effective"] else "—",
                     r["actual"].replace("|", "\\|")])
    rows_out.append(md_table(["扩展名", "ProgID", "类别图标", "值匹配", "文件存在",
                              "Shell 生效", "实际注册表值"], rows))
    rows_out.append("")
    rows_out.append("### 5.3 Shell 实际生效说明（SOFT）")
    rows_out.append("")
    if reg.get("shell_effective_count", 0) == len(ext_rows):
        rows_out.append("全部 42 个扩展名的 Shell 图标均指向我们的类别图标。")
    else:
        locked = [r for r in ext_rows if not r["shell_effective"]]
        rows_out.append("本机 %d/42 生效。未生效项及原因（Windows 保护，不伪造）："
                        % (len(ext_rows) - len(locked)))
        rows_out.append("")
        for r in locked:
            rows_out.append("- %s：%s（实际解析到 %s）"
                            % (r["ext"], r["shell_reason"],
                               r["shell_value"] or "空"))
        rows_out.append("")
        rows_out.append("> 这些扩展名的 HKCU\\...\\FileExts\\.ext\\UserChoiceLatest "
                        "里仍是旧播放器的 ProgID，且该键受 Win11 哈希/ACL 保护"
                        "（整键删除返回 denied）。需要用户在「设置 → 应用 → 默认应用」"
                        "或首次双击时点一次「始终使用此应用」。这不是本程序的写入错误："
                        "42 项 DefaultIcon 值本身全部正确。")
    rows_out.append("")
    rows_out.append("### 5.4 机制自证（干净临时关联）")
    rows_out.append("")
    mech = reg.get("mechanism") or {}
    rows_out.append("把临时扩展名 %s 关联到 %s（用完即删），Shell 解析到："
                    % (mech.get("ext", "?"), mech.get("progid", "?")))
    rows_out.append("")
    rows_out.append("- 期望：%s" % mech.get("expect", "?"))
    rows_out.append("- 实际：%s" % (mech.get("actual") or "（空）"))
    rows_out.append("- 结论：%s" % ("**通过**（绑定逻辑本身正确）"
                                    if mech.get("ok") else "**失败**（绑定逻辑有问题）"))
    rows_out.append("")
    rows_out.append("> 这一步排除「程序写错」的可能：在不受 UserChoice 干扰的干净关联上，"
                    "Shell 确实按我们写入的 ProgID DefaultIcon 取图标。")
    rows_out.append("")
    rows_out.append("## 6. 预览图")
    rows_out.append("")
    for p in result.get("previews", []):
        rows_out.append("- %s" % p)
    rows_out.append("")
    rows_out.append("## 7. 退出码")
    rows_out.append("")
    rows_out.append("HARD 失败 %d 项 → %s"
                    % (len(result["failures"]),
                       "退出码 0" if not result["failures"] else "退出码 1"))
    md_path = os.path.join(LOG_DIR, "selfcheck.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(rows_out))
    return json_path, md_path


# ======================================================================================
# 主流程
# ======================================================================================

def find_exe(arg):
    if arg:
        return os.path.abspath(arg)
    out = os.path.join(ROOT, "output")
    cands = []
    if os.path.isdir(out):
        for name in sorted(os.listdir(out), reverse=True):
            p = os.path.join(out, name, "至简Player.exe")
            if os.path.isfile(p):
                cands.append(p)
    for p in (os.path.join(ROOT, "build", "dist", "至简Player", "至简Player.exe"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "至简Player",
                           "至简Player.exe")):
        if os.path.isfile(p):
            cands.append(p)
    return os.path.abspath(cands[0]) if cands else ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="task-40 图标与注册表绑定自检")
    ap.add_argument("--exe", default="", help="要核对/注册的可执行文件")
    ap.add_argument("--no-register", action="store_true", help="只读核对，不动注册表")
    ap.add_argument("--no-preview", action="store_true", help="不生成 shell 图标预览图")
    args = ap.parse_args(argv)

    os.makedirs(LOG_DIR, exist_ok=True)
    print("== task-40 视觉识别系统自检 ==")
    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "failures": [],
        "soft_notes": [],
    }
    result["ico"] = check_ico_files()
    result["idempotent_icons"] = check_idempotent()
    result["main"] = check_main_icon()
    result["media"] = check_media_icons()
    exe = find_exe(args.exe)
    result["registry"] = check_registry(exe, do_register=not args.no_register)
    previews = []
    try:
        previews.append(make_icon_sheet())
    except Exception as exc:
        soft("生成 icon-sheet.png 失败：%s" % exc)
    if not args.no_preview:
        sheet = make_shell_icon_sheet()
        if sheet.get("ok"):
            previews.append(sheet["path"])
        else:
            soft(sheet.get("error", "shell 图标预览失败"))
    result["previews"] = previews
    result["failures"] = list(failures)
    result["soft_notes"] = list(soft_notes)
    json_path, md_path = write_report(result)
    print("")
    print("== 结果 ==")
    print("HARD 失败: %d" % len(failures))
    for f in failures:
        print("  - %s" % f)
    print("SOFT 提示: %d 条（见报告 5.3）" % len(soft_notes))
    print("报告: %s" % md_path)
    print("      %s" % json_path)
    for p in previews:
        print("预览: %s" % p)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
