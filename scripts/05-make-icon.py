#!/usr/bin/env python
"""生成「至简Player」视觉识别系统图标（纯 Pillow，无随机数 / 无时间戳，可逐字节复跑）。

产物（assets/，全部为 16/32/48/64/128/256 六尺寸 ICO）:
    icon.ico       主图标：曜石黑 #121418 超级椭圆(Squircle)底座 + 青 #00D2FF → 蓝 #0066FF
                   渐变的多面播放徽标 + 边缘 1px 液态折射光晕
    audio.ico      音频：青蓝调底座 + 灵动声波 + 单色音乐徽章
    video.ico      视频：深紫罗兰底座 + 胶片齿孔质感 + 播放视窗切角
    image.ico      图片：翠绿/薄荷底座 + 双峰视差 + 光圈几何
    subtitle.ico   字幕：暖金底座 + 双行字幕条（第 4 个 ProgID 的配套图标）
    icon.png       主图标 256px PNG（安装器 / 规格沿用）

为什么是纯 Pillow（替换掉原来的 Qt 版 05-make-icon.py）:
  · 原脚本 import PySide6 + QApplication，无显示环境 / 打包机跑不起来；
  · 本脚本只用 Pillow + 标准库，同一 Pillow 版本下**输出逐字节一致**（幂等）；
  · 每个尺寸都独立在 4x 超采样下渲染再用 LANCZOS 缩小 —— 16px 不糊、256px 不虚；
  · 先渲染不透明 RGB 再整体贴 alpha 蒙版（不用 RGBA 直接缩放），避免透明边缘发黑。

可复跑:
    python scripts/05-make-icon.py            # 生成/覆盖全部图标
    python scripts/05-make-icon.py --check    # 只校验：磁盘文件 == 重新渲染的字节，且 ico 尺寸齐全
    python scripts/05-make-icon.py --preview  # 另存 logs/task40/preview-*.png 供肉眼核对

设计 token（与 reports 中一致）:
    BASE_MAIN = #121418  CYAN = #00D2FF  BLUE = #0066FF
    三类媒体图标主色相（背景估算）：音频 ~200° / 视频 ~262° / 图片 ~164°
"""
from __future__ import annotations

import argparse
import hashlib
import io
import math
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ASSETS = os.path.join(ROOT, "assets")
LOG_DIR = os.path.join(ROOT, "logs", "task40")

SIZES = (16, 32, 48, 64, 128, 256)
SS = 4                      # 超采样倍率（每个尺寸按 size*SS 渲染）
MARGIN = 0.032              # 超级椭圆留白（占图标边长比例）
SQUIRCLE_N = 4.5            # 超椭圆指数：n=2 是圆，n→∞ 是方；4.5 ≈ 现代 Squircle

BASE_MAIN = (0x12, 0x14, 0x18)
CYAN = (0x00, 0xD2, 0xFF)
BLUE = (0x00, 0x66, 0xFF)

#: 每个图标一套：底座色 / 两个强调色 / 光晕渐变两端
KINDS = {
    "icon": {
        "base": BASE_MAIN,
        "accent": CYAN,
        "accent2": BLUE,
        "halo": (CYAN, BLUE),
    },
    "audio": {
        "base": (0x0A, 0x1E, 0x2A),          # 青蓝调（墨青）
        "accent": (0x00, 0xC0, 0xFF),        # 荧光电青（略偏蓝，与图片图标的翠绿拉开色相）
        "accent2": (0x00, 0x66, 0xFF),       # 星芒宝蓝
        "halo": ((0x00, 0xD2, 0xFF), (0x00, 0x66, 0xFF)),
    },
    "video": {
        "base": (0x17, 0x0F, 0x2C),          # 深紫罗兰
        "accent": (0xA7, 0x8B, 0xFA),        # 浅紫罗兰
        "accent2": (0x6D, 0x28, 0xD9),       # 宝紫
        "halo": ((0xB4, 0x9B, 0xFF), (0x6D, 0x28, 0xD9)),
    },
    "image": {
        "base": (0x07, 0x26, 0x1A),          # 翠绿/薄荷底（色相 ~157°）
        "accent": (0x5E, 0xEA, 0xD4),        # 薄荷
        "accent2": (0x22, 0xC5, 0x5E),       # 翠绿（色相 ~142°，与音频青蓝明显分开）
        "halo": ((0x6E, 0xF7, 0xD8), (0x0E, 0x9F, 0x6E)),
    },
    "subtitle": {
        "base": (0x19, 0x15, 0x12),          # 暖炭黑
        "accent": (0xFF, 0xC0, 0x40),        # 暖金
        "accent2": (0xFF, 0x7A, 0x00),       # 琥珀橙
        "halo": ((0xFF, 0xC8, 0x5C), (0xFF, 0x7A, 0x00)),
    },
}

#: 纯色底座之外的"品牌"说明（自检报告直接引用）
KIND_TITLES = {
    "icon": "主应用图标（Minimalist Prism）",
    "audio": "音频文件图标（声波 + 音乐徽章）",
    "video": "视频文件图标（胶片 + 切角视窗）",
    "image": "图片文件图标（双峰视差 + 光圈）",
    "subtitle": "字幕文件图标（字幕条）",
}

_out_names = {
    "icon": "icon.ico",
    "audio": "audio.ico",
    "video": "video.ico",
    "image": "image.ico",
    "subtitle": "subtitle.ico",
}


# ======================================================================================
# 基础工具
# ======================================================================================

def _clamp8(v):
    return 0 if v < 0 else (255 if v > 255 else int(v))


def _linear_gradient(size, c0, c1, angle_deg):
    """任意角度的线性渐变（先 64x64 精确算，再双线性放大 —— 线性渐变放大后无误差）。"""
    w, h = size
    res = 64
    ang = math.radians(angle_deg)
    dx, dy = math.cos(ang), math.sin(ang)
    proj = (0.0, dx, dy, dx + dy)
    lo, hi = min(proj), max(proj)
    span = (hi - lo) or 1.0
    small = Image.new("RGB", (res, res))
    px = small.load()
    for y in range(res):
        yy = y / (res - 1)
        for x in range(res):
            t = ((x / (res - 1)) * dx + yy * dy - lo) / span
            px[x, y] = tuple(_clamp8(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(3))
    return small.resize((w, h), Image.BILINEAR)


def _linear_gradient_box(size, box, c0, c1, angle_deg):
    """只在 box 范围内铺满 c0→c1 的线性渐变（box 外统一填 c0，反正会被蒙版裁掉）。

    ⚠️ 不要用**整张画布**铺渐变再拿局部：三角形只占画布约 1/4，实测色相跨度会被
       压缩到 5° 左右（看起来像纯色）。按图形自身包围盒铺，才能拿到完整的青→蓝过渡。
    """
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1 = min(size[0], max(x0 + 1, x1))
    y1 = min(size[1], max(y0 + 1, y1))
    grad = _linear_gradient((x1 - x0, y1 - y0), c0, c1, angle_deg)
    layer = Image.new("RGB", size, tuple(c0))
    layer.paste(grad, (x0, y0))
    return layer


def _mask_scale(mask, factor):
    f = 0.0 if factor < 0 else (1.0 if factor > 1 else factor)
    return mask.point(lambda v: int(round(v * f)))


def _overlay(rgb, color, mask, alpha=1.0):
    """把 color 按 mask（可再乘 alpha）盖到 RGB 画布上。"""
    m = mask if alpha >= 0.999 else _mask_scale(mask, alpha)
    if isinstance(color, Image.Image):
        layer = color if color.mode == "RGB" else color.convert("RGB")
    else:
        layer = Image.new("RGB", rgb.size, tuple(color[:3]))
    return Image.composite(layer, rgb, m)


def _scale_color(img, factor):
    return img.point(lambda v: _clamp8(round(v * factor)))


def _poly_mask(size, pts, round_blur=0.0):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    if round_blur > 0:
        m = m.filter(ImageFilter.GaussianBlur(round_blur)).point(
            lambda v: 255 if v >= 128 else 0)
    return m


def _disc_mask(size, cx, cy, r):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return m


def _rect_mask(size, box):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rectangle(box, fill=255)
    return m


def _rounded_mask(size, box, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def _line_mask(size, points, width):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).line(points, fill=255, width=max(1, int(width)), joint="curve")
    return m


def _squircle_points(cx, cy, r, n=SQUIRCLE_N, steps=720):
    """超椭圆 |x|^n + |y|^n = 1 的参数化取点（n=4.5 ≈ 现代 Squircle）。"""
    e = 2.0 / n
    pts = []
    for i in range(steps):
        t = 2.0 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        pts.append((cx + r * math.copysign(abs(ct) ** e, ct),
                    cy + r * math.copysign(abs(st) ** e, st)))
    return pts


_SQUIRCLE_CACHE = {}


def _squircle_mask(W, inset):
    key = (W, round(inset, 4))
    m = _SQUIRCLE_CACHE.get(key)
    if m is None:
        m = Image.new("L", (W, W), 0)
        ImageDraw.Draw(m).polygon(
            _squircle_points(W / 2.0, W / 2.0, W / 2.0 - inset), fill=255)
        _SQUIRCLE_CACHE[key] = m
    return m


def _squircle_canvas(W, kind):
    """渲染统一底座：Squircle 纯色底 + 边缘 1px 液态折射光晕（沿上亮下暗的强度包络）。

    返回 (rgb 画布, 外形蒙版)。画布在轮廓外也是底座色 —— 最后统一贴 alpha，
    这样缩放不会出现"透明边缘发黑"。
    """
    spec = KINDS[kind]
    base = spec["base"]
    outer = _squircle_mask(W, W * MARGIN)
    rgb = Image.new("RGB", (W, W), tuple(base))
    # 1px 光晕：外圈 - 内圈（内圈内缩 1 个目标像素 = SS 个超采样像素）
    inner = _squircle_mask(W, W * MARGIN + SS)
    ring = ImageChops.subtract(outer, inner)
    ramp = Image.linear_gradient("L").resize((W, W), Image.BILINEAR)   # 0=顶, 255=底
    ring = ImageChops.multiply(
        ring, ramp.point(lambda v: int(round(255.0 * (0.40 - 0.26 * v / 255.0)))))
    ring_color = _linear_gradient((W, W), spec["halo"][0], spec["halo"][1], 135)
    rgb = Image.composite(ring_color, rgb, ring)
    return rgb, outer


def _finish(rgb, outer, size):
    """超采样 RGB + 蒙版 → 目标尺寸 RGBA（颜色与 alpha 分开缩放，边缘干净）。"""
    rgb_s = rgb.resize((size, size), Image.LANCZOS)
    a_s = outer.resize((size, size), Image.LANCZOS)
    out = rgb_s.convert("RGBA")
    out.putalpha(a_s)
    return out


# ======================================================================================
# 主图标
# ======================================================================================

def render_icon_main(size):
    W = size * SS
    rgb, outer = _squircle_canvas(W, "icon")
    cx = cy = W / 2.0

    # ---- 几何多面播放徽标 -------------------------------------------------------------
    tw = 0.50 * W
    th = 0.56 * W
    x0 = cx - tw / 3.0          # 三角形视觉重心在 bbox 左 1/3，故左边界只退 1/3 宽
    x1 = x0 + tw
    y0 = cy - th / 2.0
    y1 = cy + th / 2.0
    tri_pts = [(x0, y0), (x1, cy), (x0, y1)]
    tri = _poly_mask((W, W), tri_pts, round_blur=0.045 * W)

    # 荧光外发光（电青，微弱）
    glow = tri.filter(ImageFilter.GaussianBlur(0.050 * W))
    rgb = _overlay(rgb, CYAN, glow, alpha=0.24)

    # 青→蓝渐变主体（角度 35°，右下方为蓝；水平取样也能看到明显色相过渡）
    # 渐变方向用**水平 0°**：三角形的包围盒左缘 = 电青 #00D2FF、右缘 = 宝蓝 #0066FF，
    # 这样水平取样能拿到完整 190.6°→216° 的色相过渡（斜向会把跨度压到 ~15°）。
    body = _linear_gradient_box((W, W), (x0, y0, x1, y1), CYAN, BLUE, 0.0)
    upper = ImageChops.multiply(tri, _rect_mask((W, W), (0, 0, W, cy)))
    lower = ImageChops.multiply(tri, _rect_mask((W, W), (0, cy, W, W)))
    # 多面感：上半面提亮、下半面压暗（只动明度不动色相，渐变仍连续）
    faceted = Image.composite(_scale_color(body, 1.07), _scale_color(body, 0.90), upper)
    rgb = Image.composite(faceted, rgb, tri)

    # 折面接缝：沿中线一条 1px 暗线，把"上半面/下半面"读成折纸的多面体
    seam = ImageChops.multiply(
        _line_mask((W, W), [(x0 + 0.035 * W, cy), (x1 - 0.02 * W, cy)],
                   max(2, int(round(1.0 * SS)))), tri)
    rgb = _overlay(rgb, _scale_color(body, 0.62), seam, alpha=0.85)

    # 折面高光：沿两条斜边内侧 1px 的微弱白色反光
    edge = Image.new("L", (W, W), 0)
    ed = ImageDraw.Draw(edge)
    wdt = max(2, int(round(1.1 * SS)))
    ed.line([(x0 + 0.05 * W, y0 + 0.06 * W), (x1 - 0.10 * W, cy - 0.05 * W)],
            fill=255, width=wdt)
    ed.line([(x0 + 0.05 * W, y1 - 0.06 * W), (x1 - 0.10 * W, cy + 0.05 * W)],
            fill=255, width=wdt)
    edge = ImageChops.multiply(edge, tri)
    rgb = _overlay(rgb, (255, 255, 255), edge, alpha=0.20)

    return _finish(rgb, outer, size)


# ======================================================================================
# 音频图标：单色音乐徽章（实心圆徽 + 挖空八分音符）+ 下方灵动声波
# ======================================================================================

def _note_mask(W, cx, cy, R):
    """双音符（beamed eighth notes）挖空蒙版；坐标以徽章半径 R 为单位。

    ⚠️ 16/24px 下按比例算出来的符干只有 0.4 个目标像素，缩小后整枚音符会糊成
       一个洞 —— 所以所有线宽/符头都有**最小目标像素**下限（1 目标像素 = SS 超采样像素）。
    """
    Rn = 0.86 * R
    m = Image.new("L", (W, W), 0)
    d = ImageDraw.Draw(m)
    hx = max(0.30 * Rn, 0.95 * SS)         # 符头半宽（下限 ~1 目标像素）
    hy = max(0.22 * Rn, 0.80 * SS)         # 符头半高
    head1 = (cx - 0.30 * Rn, cy + 0.42 * Rn)
    head2 = (cx + 0.30 * Rn, cy + 0.22 * Rn)
    d.ellipse([head1[0] - hx, head1[1] - hy, head1[0] + hx, head1[1] + hy], fill=255)
    d.ellipse([head2[0] - hx, head2[1] - hy, head2[0] + hx, head2[1] + hy], fill=255)
    stem_w = max(0.11 * Rn, 1.25 * SS)
    # 符干：贴着符头右缘向上
    d.rectangle([head1[0] + hx - stem_w, cy - 0.45 * Rn,
                 head1[0] + hx, head1[1]], fill=255)
    d.rectangle([head2[0] + hx - stem_w, cy - 0.62 * Rn,
                 head2[0] + hx, head2[1]], fill=255)
    # 符杠：连接两根符干顶端（右高左低，标准八分音符连杠）
    t1 = (head1[0] + hx - stem_w, cy - 0.45 * Rn)
    t2 = (head2[0] + hx - stem_w, cy - 0.62 * Rn)
    bt = max(0.22 * Rn, 1.5 * SS)          # 符杠厚度（下限 ~1.5 目标像素）
    d.polygon([t1, t2, (t2[0], t2[1] + bt), (t1[0], t1[1] + bt)], fill=255)
    return m


def _wave_mask(W, y0, amp, cycles, phase, lw, x_from, x_to):
    pts = []
    n = 160
    for i in range(n + 1):
        t = i / n
        x = x_from + (x_to - x_from) * t
        y = y0 + amp * math.sin(2.0 * math.pi * (cycles * t) + phase)
        pts.append((x, y))
    return _line_mask((W, W), pts, lw)


def render_icon_audio(size):
    W = size * SS
    rgb, outer = _squircle_canvas(W, "audio")
    cx, cy = W / 2.0, W / 2.0
    spec = KINDS["audio"]

    # ---- 灵动声波：两条不同振幅/相位的正弦波（下三分之一，托住徽章）------------------
    lw = max(SS, int(round(0.020 * W)))
    wave_a = _wave_mask(W, 0.735 * W, 0.052 * W, 1.5, 0.5, lw, 0.10 * W, 0.90 * W)
    wave_b = _wave_mask(W, 0.805 * W, 0.030 * W, 2.5, 1.7, max(SS, int(round(0.014 * W))),
                        0.14 * W, 0.86 * W)
    wave_grad = _linear_gradient_box((W, W), (0.10 * W, 0.66 * W, 0.90 * W, 0.86 * W),
                                     spec["accent"], spec["accent2"], 0.0)
    rgb = _overlay(rgb, wave_grad, wave_a, alpha=0.92)
    rgb = _overlay(rgb, wave_grad, wave_b, alpha=0.45)

    # ---- 单色音乐徽章：实心荧光电青圆徽 + 挖空音符 --------------------------------
    R = 0.275 * W
    bcy = 0.425 * W
    badge = _disc_mask((W, W), cx, bcy, R)
    note = _note_mask(W, cx, bcy, R)
    badge_ring = ImageChops.subtract(badge, note)
    badge_color = Image.new("RGB", (W, W), spec["accent"])
    rgb = Image.composite(badge_color, rgb, badge_ring)

    # 徽章外沿一点点同色反光，避免"平贴"（仍是单色）
    rim = ImageChops.subtract(_disc_mask((W, W), cx, bcy, R * 1.045),
                              _disc_mask((W, W), cx, bcy, R))
    rgb = _overlay(rgb, spec["accent"], rim, alpha=0.28)
    return _finish(rgb, outer, size)


# ======================================================================================
# 视频图标：胶片齿孔 + 切角播放视窗
# ======================================================================================

def _chamfer_rect_mask(W, box, radius, chamfer):
    """圆角矩形再切掉右上、左下两个 45° 角（"播放视窗切角"）。"""
    x0, y0, x1, y1 = box
    m = _rounded_mask((W, W), box, radius)
    d = ImageDraw.Draw(m)
    c = chamfer
    d.polygon([(x1 - c, y0 - 2), (x1 + 2, y0 - 2), (x1 + 2, y0 + c)], fill=0)
    d.polygon([(x0 - 2, y1 - c), (x0 - 2, y1 + 2), (x0 + c, y1 + 2)], fill=0)
    return m


def render_icon_video(size):
    W = size * SS
    rgb, outer = _squircle_canvas(W, "video")
    cx, cy = W / 2.0, W / 2.0
    spec = KINDS["video"]

    # ---- 胶片齿孔：左右各 4 个（16px 太小则省略，避免糊成一团）---------------------
    if size >= 32:
        hole_w, hole_h = 0.048 * W, 0.070 * W
        for i in range(4):
            hy = (0.305 + 0.135 * i) * W
            for hx in (0.100 * W, 0.852 * W):
                hm = _rounded_mask((W, W), [hx, hy, hx + hole_w, hy + hole_h],
                                   0.018 * W)
                rgb = _overlay(rgb, spec["accent"], hm, alpha=0.55)

    # ---- 播放视窗（切角）+ 内部播放三角挖空 --------------------------------------
    box = [0.205 * W, 0.265 * W, 0.795 * W, 0.735 * W]
    panel = _chamfer_rect_mask(W, box, radius=0.040 * W, chamfer=0.105 * W)
    panel_grad = _linear_gradient_box((W, W), box, spec["accent"], spec["accent2"], 125.0)
    rgb = Image.composite(panel_grad, rgb, panel)

    # 视窗内描边 1px（提亮，压住边缘）
    rim = ImageChops.subtract(panel, _rounded_mask(
        (W, W), [box[0] + 0.012 * W, box[1] + 0.012 * W,
                 box[2] - 0.012 * W, box[3] - 0.012 * W], 0.034 * W))
    rgb = _overlay(rgb, (255, 255, 255), rim, alpha=0.16)

    # 播放三角（挖空 = 底座色）
    tw, th = 0.24 * W, 0.28 * W
    tx0 = cx - tw / 3.0
    ty0 = cy - th / 2.0
    tri = _poly_mask((W, W), [(tx0, ty0), (tx0 + tw, cy), (tx0, ty0 + th)],
                     round_blur=0.020 * W)
    rgb = _overlay(rgb, spec["base"], tri, alpha=1.0)
    return _finish(rgb, outer, size)


# ======================================================================================
# 图片图标：光圈几何（太阳/镜头）+ 双峰视差
# ======================================================================================

def _peak_mask(W, pts, blur):
    return _poly_mask((W, W), pts, round_blur=blur)


def render_icon_image(size):
    W = size * SS
    rgb, outer = _squircle_canvas(W, "image")
    spec = KINDS["image"]

    # ---- 光圈/太阳：薄荷渐变圆盘 + 内六边形光圈 + 6 片螺旋叶片（不是"太阳芒刺"）------
    scx, scy, sr = 0.655 * W, 0.305 * W, 0.155 * W
    sun = _disc_mask((W, W), scx, scy, sr)
    sun_grad = _linear_gradient_box((W, W), (scx - sr, scy - sr, scx + sr, scy + sr),
                                    (0xD1, 0xFA, 0xE5), spec["accent2"], 125.0)
    rgb = Image.composite(sun_grad, rgb, sun)
    iris = Image.new("L", (W, W), 0)
    idr = ImageDraw.Draw(iris)
    hr = 0.56 * sr
    hexagon = [(scx + hr * math.cos(math.radians(60.0 * i + 30.0)),
                scy + hr * math.sin(math.radians(60.0 * i + 30.0))) for i in range(6)]
    idr.line(hexagon + [hexagon[0]], fill=255,
             width=max(2, int(round(0.013 * W))), joint="curve")
    for i in range(6):
        a_vertex = math.radians(60.0 * i + 30.0)
        a_rim = a_vertex - math.radians(24.0)      # 外缘叶片向同一侧旋转 => 光圈旋叶
        r_in, r_out = hr * 1.02, sr * 0.995
        idr.line([(scx + r_in * math.cos(a_vertex), scy + r_in * math.sin(a_vertex)),
                  (scx + r_out * math.cos(a_rim), scy + r_out * math.sin(a_rim))],
                 fill=255, width=max(2, int(round(0.015 * W))))
    iris = ImageChops.multiply(iris, sun)
    rgb = _overlay(rgb, spec["base"], iris, alpha=0.78)

    # ---- 远峰（低对比，靠后）-----------------------------------------------------
    far = _peak_mask(W, [(0.050 * W, 0.780 * W), (0.355 * W, 0.395 * W),
                         (0.660 * W, 0.780 * W)], blur=0.012 * W)
    far_grad = _linear_gradient_box((W, W), (0.050 * W, 0.395 * W, 0.660 * W, 0.780 * W),
                                     (0x15, 0x6B, 0x47), (0x07, 0x33, 0x22), 90.0)
    rgb = Image.composite(far_grad, rgb, far)

    # ---- 近峰（高对比，靠前；与远峰错位形成视差）---------------------------------
    near = _peak_mask(W, [(0.285 * W, 0.800 * W), (0.590 * W, 0.445 * W),
                          (0.895 * W, 0.800 * W)], blur=0.012 * W)
    near_grad = _linear_gradient_box((W, W), (0.285 * W, 0.445 * W, 0.895 * W, 0.800 * W),
                                      spec["accent"], spec["accent2"], 100.0)
    rgb = Image.composite(near_grad, rgb, near)

    # ---- 地平线（一条细薄荷线，收住山脚）------------------------------------------
    ground = _line_mask((W, W), [(0.115 * W, 0.800 * W), (0.885 * W, 0.800 * W)],
                        max(2, int(round(0.012 * W))))
    rgb = _overlay(rgb, spec["accent"], ground, alpha=0.55)
    return _finish(rgb, outer, size)


# ======================================================================================
# 字幕图标：暖金字幕卡片 + 双行字幕条
# ======================================================================================

def render_icon_subtitle(size):
    W = size * SS
    rgb, outer = _squircle_canvas(W, "subtitle")
    spec = KINDS["subtitle"]

    card = _rounded_mask((W, W), [0.160 * W, 0.295 * W, 0.840 * W, 0.705 * W],
                         radius=0.055 * W)
    card_grad = _linear_gradient_box((W, W), (0.160 * W, 0.295 * W, 0.840 * W, 0.705 * W),
                                      spec["accent"], spec["accent2"], 120.0)
    rgb = Image.composite(card_grad, rgb, card)

    # 卡片内描边（1px 提亮）
    rim = ImageChops.subtract(card, _rounded_mask(
        (W, W), [0.160 * W + 0.012 * W, 0.295 * W + 0.012 * W,
                 0.840 * W - 0.012 * W, 0.705 * W - 0.012 * W], 0.046 * W))
    rgb = _overlay(rgb, (255, 255, 255), rim, alpha=0.14)

    # 两行字幕条（挖空 = 底座色）
    line1 = _rounded_mask((W, W), [0.255 * W, 0.395 * W, 0.745 * W, 0.445 * W],
                          radius=0.025 * W)
    line2 = _rounded_mask((W, W), [0.255 * W, 0.520 * W, 0.620 * W, 0.570 * W],
                          radius=0.025 * W)
    lines = ImageChops.lighter(line1, line2)
    rgb = Image.composite(Image.new("RGB", (W, W), spec["base"]), rgb, lines)
    return _finish(rgb, outer, size)


# ======================================================================================
# 渲染 / 保存 / 自检
# ======================================================================================

_RENDERERS = {
    "icon": render_icon_main,
    "audio": render_icon_audio,
    "video": render_icon_video,
    "image": render_icon_image,
    "subtitle": render_icon_subtitle,
}


def render_icon(kind, size):
    return _RENDERERS[kind](size)


def render_all(kind):
    """返回 {尺寸: RGBA Image}（不含磁盘 I/O）。"""
    return {s: render_icon(kind, s) for s in SIZES}


def build_ico_bytes(frames):
    """把各尺寸帧按"每帧原生尺寸"写成多尺寸 ICO，返回字节。

    Pillow 的 ICO 写入器支持 append_images：尺寸完全匹配的帧直接使用，不重采样。
    """
    ordered = [frames[s] for s in SIZES]
    big = ordered[-1]
    buf = io.BytesIO()
    big.save(buf, format="ICO", sizes=[(s, s) for s in SIZES],
             append_images=ordered[:-1])
    return buf.getvalue()


def build_png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def expected_outputs():
    """{文件名: 字节} —— 生成与校验共用同一份内存渲染结果。"""
    out = {}
    for kind in _out_names:
        frames = render_all(kind)
        out[_out_names[kind]] = build_ico_bytes(frames)
        if kind == "icon":
            out["icon.png"] = build_png_bytes(frames[256])
    return out


def ico_sizes(path):
    with Image.open(path) as im:
        return sorted(im.ico.sizes())


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_outputs(outputs):
    os.makedirs(ASSETS, exist_ok=True)
    for name, data in outputs.items():
        p = os.path.join(ASSETS, name)
        with open(p, "wb") as f:
            f.write(data)
    return [os.path.join(ASSETS, n) for n in outputs]


def check_outputs(outputs):
    """幂等自检：磁盘上的每个文件必须与"现在重新渲染的结果"逐字节一致。"""
    ok = True
    lines = []
    for name in sorted(outputs):
        p = os.path.join(ASSETS, name)
        want = sha256(outputs[name])
        if not os.path.isfile(p):
            ok = False
            lines.append("MISSING  %-14s 期望 sha256=%s" % (name, want[:16]))
            continue
        with open(p, "rb") as f:
            got = sha256(f.read())
        same = got == want
        ok = ok and same
        lines.append("%s %-14s sha256=%s" % ("OK     " if same else "MISMATCH",
                                             name, got[:16]))
    for name in ("icon.ico", "audio.ico", "video.ico", "image.ico", "subtitle.ico"):
        p = os.path.join(ASSETS, name)
        if os.path.isfile(p):
            sz = ico_sizes(p)
            good = [s for s in SIZES if (s, s) in sz]
            lines.append("ICO    %-14s 尺寸=%s 齐全=%s" % (name, good, len(good) == len(SIZES)))
            ok = ok and len(good) == len(SIZES)
    print(chr(10).join(lines))
    return ok


def make_preview(outputs):
    """给肉眼核对用的预览图：左侧 256 原图，右侧 5 个小尺寸按 NEAREST 放大。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    paths = []
    for kind, name in _out_names.items():
        src = os.path.join(ASSETS, name)
        with Image.open(src) as im:
            im.size = (256, 256)
            big = im.convert("RGBA").copy()
        smalls = []
        for s in (16, 32, 48, 64, 128):
            with Image.open(src) as im2:
                im2.size = (s, s)
                smalls.append(im2.convert("RGBA").copy().resize((128, 128), Image.NEAREST))
        sheet = Image.new("RGBA", (256 + 8 + len(smalls) * 136 + 8, 256 + 16), (24, 26, 30, 255))
        # 棋盘格底（看清透明边缘）
        cd = ImageDraw.Draw(sheet)
        for yy in range(0, sheet.height, 16):
            for xx in range(0, sheet.width, 16):
                if (xx // 16 + yy // 16) % 2 == 0:
                    cd.rectangle([xx, yy, xx + 15, yy + 15], fill=(34, 36, 42, 255))
        sheet.alpha_composite(big, (8, 8))
        x = 256 + 16
        for sm in smalls:
            sheet.alpha_composite(sm, (x, 8))
            x += 136
        p = os.path.join(LOG_DIR, "preview-%s.png" % kind)
        sheet.convert("RGB").save(p)
        paths.append(p)
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 / 校验 至简Player 全套图标")
    ap.add_argument("--check", action="store_true", help="只校验（不写文件）")
    ap.add_argument("--preview", action="store_true", help="另存预览图到 logs/task40/")
    args = ap.parse_args(argv)

    outputs = expected_outputs()
    if args.check:
        ok = check_outputs(outputs)
        return 0 if ok else 1

    paths = write_outputs(outputs)
    print("已生成 %d 个文件:" % len(paths))
    for p in paths:
        print("  %-40s %8d bytes" % (p, os.path.getsize(p)))
    if args.preview:
        for p in make_preview(outputs):
            print("预览: %s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
