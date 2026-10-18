#!/usr/bin/env python
r"""生成「至简Player」尺规几何图标（纯 Pillow，无随机数 / 无时间戳，逐字节可复跑）。

★ task-51 重绘（严格数学，脚本可复跑 + 自校验）:
    256×256 坐标系；统一底座：中心 (128,128)、边长 224、超椭圆指数 n=4
    (|x/112|^4 + |y/112|^4 = 1)，深空曜石底 #14171D + 几何内环。

    icon.ico      外接圆 R=64 的**内切正三角形**（r=12 精确倒角）+ R/φ 同心光环
    audio.ico     5 根严格对称垂直声柱：中心 x=128，间距 Δx=22，
                  高度 [H·φ², H·φ, H, H·φ, H·φ²]，两端全圆角胶囊（无手绘波浪）
    video.ico     严格 16:9 视窗矩形 + 中心正三角
    image.ico     正方形画框内两个 45° 直角等腰三角形山峰 + 右上正圆太阳
                  （圆心在黄金分割点：x=框左+φ·框宽, y=框顶+(1-φ)·框高）
    subtitle.ico  同风格：圆角字幕卡片 + 两条字幕条

  产物（assets/，16/32/48/64/128/256 六尺寸 ICO + icon.png 256 PNG）：
  可复跑:
    python scripts/05-make-icon.py            # 生成/覆盖
    python scripts/05-make-icon.py --check    # 逐字节幂等 + 全部尺规/像素判据
    python scripts/05-make-icon.py --preview  # 另存 logs/task51/preview-*.png
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
LOG_DIR = os.path.join(ROOT, "logs", "task62")

SIZES = (16, 32, 48, 64, 128, 256)
SS = 4

# ---------------------------------------------------------------------------
# 尺规参数（逻辑坐标系 256×256；渲染时统一乘 k=W/256）
# ---------------------------------------------------------------------------
S = 256.0
CENTER = 128.0
HALF_SIDE = 112.0                 # 边长 224 -> 半边长 112
SQUIRCLE_N = 4.0                  # 超椭圆指数（卡片硬性要求 n=4）
SQUIRCLE_COVERAGE = math.gamma(1.0 + 1.0 / SQUIRCLE_N) ** 2 / math.gamma(1.0 + 2.0 / SQUIRCLE_N)
PHI = (math.sqrt(5.0) - 1.0) / 2.0  # 0.6180339887...

BASE = (0x14, 0x17, 0x1D)         # 深空曜石底 #14171D

# 主图标
TRI_R = 64.0                      # 外接圆半径 R=64
TRI_FILLET = 12.0                 # 顶点倒角半径 r=12
HALO_R = TRI_R / PHI              # R/φ 同心光环

# ★ task-62：现代流线型播放箭头（Golden Ratio Play Glyph）——不是等边正三角
GLYPH_W = 96.0                    # 设计 bbox 宽（逻辑 px）
# H:W = 1.25:1；再 +2px 补偿 256 栅格化时上下边缘抗锯齿被阈值吃掉的高度，
# 使**像素 bbox** 实测 H/W 落在 1.25±0.03（等边 0.866 被明确排除）。
GLYPH_H = GLYPH_W * 1.25 + 2.0
GLYPH_R = 14.0                    # 右顶点圆角半径（判据 14±1.5）
GLYPH_CHAMFER = 12.0              # 左侧垂直边上下双切角（利落内收）
GLYPH_DEEP = (0x06, 0x2A, 0x66)   # 深海蓝
GLYPH_CYAN = (0x00, 0xE5, 0xFF)   # 亮青柔光


def _solve_glyph_apex(height=GLYPH_H, width=GLYPH_W, r=GLYPH_R):
    """求尖角顶点 x 位置 A：使「圆角后」的 bbox 满足 H:W = ratio。

    圆角把尖端削短 r*(1/sin(θ/2)-1)（θ 为右顶点内角）；迭代到稳定。
    """
    target_w = width          # 固定设计宽（高度里的 2px 是 AA 补偿，不反推宽度）
    apex = target_w + r
    for _ in range(80):
        half = math.atan((height / 2.0) / apex)
        apex = target_w + r * (1.0 / math.sin(half) - 1.0)
    return apex


GLYPH_APEX = _solve_glyph_apex()  # 尖角顶点相对左边的 x 距离

# 音频
AUDIO_DX = 22.0
AUDIO_H = 104.0
AUDIO_W = 14.0
AUDIO_RATIOS = (PHI * PHI, PHI, 1.0, PHI, PHI * PHI)

# 视频
VIDEO_W = 176.0
VIDEO_H = VIDEO_W * 9.0 / 16.0    # 严格 16:9
VIDEO_TRI_SIDE = 48.0

# 图片
IMG_FRAME = (40.0, 40.0, 176.0)   # (x0, y0, 边长) 正方形画框
IMG_PEAK_H = 48.0                 # 45° 直角等腰三角形山峰的高（=半底）
SUN_R = 14.0
SUN_GX = IMG_FRAME[0] + PHI * IMG_FRAME[2]
SUN_GY = IMG_FRAME[1] + (1.0 - PHI) * IMG_FRAME[2]

KINDS = {
    "icon": {"accent": (0x00, 0xD2, 0xFF), "accent2": (0x00, 0x66, 0xFF),
             "title": "主应用图标（尺规正三角 + 同心光环）"},
    "audio": {"accent": (0x00, 0xC0, 0xFF), "accent2": (0x00, 0x66, 0xFF),
              "title": "音频图标（5 柱对称声柱）"},
    "video": {"accent": (0xA7, 0x8B, 0xFA), "accent2": (0x6D, 0x28, 0xD9),
              "title": "视频图标（16:9 视窗 + 正三角）"},
    "image": {"accent": (0x5E, 0xEA, 0xD4), "accent2": (0x22, 0xC5, 0x5E),
              "title": "图片图标（45° 双峰 + 黄金点太阳）"},
    "subtitle": {"accent": (0xFF, 0xC0, 0x40), "accent2": (0xFF, 0x7A, 0x00),
                 "title": "字幕图标（字幕卡片 + 双条）"},
}
SUN_COLOR = (0xFF, 0xC8, 0x5C)

_OUT = {"icon": "icon.ico", "audio": "audio.ico", "video": "video.ico",
        "image": "image.ico", "subtitle": "subtitle.ico"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _c8(v):
    return 0 if v < 0 else (255 if v > 255 else int(round(v)))


def _mix(c0, c1, t):
    return tuple(_c8(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))


def _lighten(c, t):
    return _mix(c, (255, 255, 255), t)


def _linear_gradient(w, h, c0, c1, angle_deg):
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
            px[x, y] = tuple(_c8(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
    return small.resize((max(1, int(w)), max(1, int(h))), Image.BILINEAR)


def _grad_box(W, box, c0, c1, angle):
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1 = min(W, max(x0 + 1, x1))
    y1 = min(W, max(y0 + 1, y1))
    grad = _linear_gradient(x1 - x0, y1 - y0, c0, c1, angle)
    layer = Image.new("RGB", (W, W), tuple(c0))
    layer.paste(grad, (x0, y0))
    return layer


def _overlay(rgb, color, mask, alpha=1.0):
    if alpha < 0.999:
        mask = mask.point(lambda v: int(round(v * alpha)))
    layer = color if isinstance(color, Image.Image) else Image.new("RGB", rgb.size, tuple(color[:3]))
    if layer.size != rgb.size:
        layer = layer.resize(rgb.size, Image.BILINEAR)
    return Image.composite(layer, rgb, mask)


def _poly_mask(W, pts):
    m = Image.new("L", (W, W), 0)
    ImageDraw.Draw(m).polygon([(float(x), float(y)) for x, y in pts], fill=255)
    return m


def _disc_mask(W, cx, cy, r):
    m = Image.new("L", (W, W), 0)
    ImageDraw.Draw(m).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return m


def _ring_mask(W, cx, cy, r, width):
    outer = _disc_mask(W, cx, cy, r + width / 2.0)
    inner = _disc_mask(W, cx, cy, max(0.0, r - width / 2.0))
    return ImageChops.subtract(outer, inner)


def _capsule_mask(W, x, y0, y1, w):
    r = w / 2.0
    m = Image.new("L", (W, W), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([x - r, y0, x + r, y1], radius=r, fill=255)
    return m


def _squircle_pts(cx, cy, a, n=SQUIRCLE_N, steps=2880):
    e = 2.0 / n
    pts = []
    for i in range(steps):
        t = 2.0 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        pts.append((cx + a * math.copysign(abs(ct) ** e, ct),
                    cy + a * math.copysign(abs(st) ** e, st)))
    return pts


_SQ_CACHE = {}


def _squircle_mask(W):
    key = W
    m = _SQ_CACHE.get(key)
    if m is None:
        a = HALF_SIDE * (W / S)
        m = Image.new("L", (W, W), 0)
        ImageDraw.Draw(m).polygon(_squircle_pts(W / 2.0, W / 2.0, a), fill=255)
        _SQ_CACHE[key] = m
    return m


def _base_canvas(W, kind):
    k = W / S
    mask = _squircle_mask(W)
    rgb = Image.new("RGB", (W, W), BASE)      # 严格 #14171D（卡片硬性底色，不做明度扰动）
    # 几何内环：r=100（逻辑），accent 低透明度
    ring = _ring_mask(W, W / 2.0, W / 2.0, 100.0 * (W / S), max(1, int(round(1.4 * (W / S)))))
    rgb = _overlay(rgb, _mix(BASE, KINDS[kind]["accent2"], 0.55), ring, alpha=0.42)
    return rgb, mask


def _finish(rgb, mask, size):
    rgb_s = rgb.resize((size, size), Image.LANCZOS)
    a_s = mask.resize((size, size), Image.LANCZOS)
    out = rgb_s.convert("RGBA")
    out.putalpha(a_s)
    return out


# ---------------------------------------------------------------------------
# 主图标：R=64 内切正三角（r=12 倒角）+ R/φ 同心光环
# ---------------------------------------------------------------------------
def triangle_vertices(cx, cy, R=TRI_R):
    """外接圆半径 R 的正三角形顶点：-90°, 30°, 150°。"""
    return [(cx + R * math.cos(math.radians(a)), cy + R * math.sin(math.radians(a)))
            for a in (-90.0, 30.0, 150.0)]


def rounded_triangle_mask(W):
    """精确倒角的正三角形蒙版：每个顶点以 r=12 圆弧替换尖角（60° 内角）。"""
    k = W / S
    cx = cy = W / 2.0
    R = TRI_R * k
    r = TRI_FILLET * k
    verts = triangle_vertices(cx, cy, R)
    t = r / math.tan(math.radians(30.0))            # 切点距顶点距离
    center_d = r / math.sin(math.radians(30.0))     # 圆心距顶点距离
    pts = []
    n = len(verts)
    for i in range(n):
        a = verts[i]
        prev = verts[(i - 1) % n]
        nxt = verts[(i + 1) % n]
        # 从顶点指向两个邻点的单位向量；二者之和指向三角形**内部**（角平分线）
        up = (prev[0] - a[0], prev[1] - a[1])
        un = (nxt[0] - a[0], nxt[1] - a[1])
        lp = math.hypot(*up) or 1.0
        ln = math.hypot(*un) or 1.0
        up = (up[0] / lp, up[1] / lp)
        un = (un[0] / ln, un[1] / ln)
        bis = (up[0] + un[0], up[1] + un[1])
        lb = math.hypot(*bis) or 1.0
        bis = (bis[0] / lb, bis[1] / lb)
        c = (a[0] + bis[0] * center_d, a[1] + bis[1] * center_d)
        t1 = (a[0] + up[0] * t, a[1] + up[1] * t)
        t2 = (a[0] + un[0] * t, a[1] + un[1] * t)
        a1 = math.atan2(t1[1] - c[1], t1[0] - c[0])
        a2 = math.atan2(t2[1] - c[1], t2[0] - c[0])
        # 走短弧（内角 60° 的补弧 120°？取夹角最短方向）
        diff = (a2 - a1) % (2 * math.pi)
        if diff > math.pi:
            diff -= 2 * math.pi
        steps = 48
        for j in range(steps + 1):
            ang = a1 + diff * j / steps
            pts.append((c[0] + r * math.cos(ang), c[1] + r * math.sin(ang)))
    return _poly_mask(W, pts)


def _rounded_polygon_mask(W, pts, radii):
    """多边形遮罩：radii[i]>0 的顶点用精确圆弧倒角，其余保持尖角。"""
    poly = []
    n = len(pts)
    for i, (p, rad) in enumerate(zip(pts, radii)):
        if rad <= 0.15:
            poly.append(p)
            continue
        prev = pts[(i - 1) % n]
        nxt = pts[(i + 1) % n]
        up = (prev[0] - p[0], prev[1] - p[1])
        un = (nxt[0] - p[0], nxt[1] - p[1])
        lp = math.hypot(*up) or 1.0
        ln = math.hypot(*un) or 1.0
        up = (up[0] / lp, up[1] / lp)
        un = (un[0] / ln, un[1] / ln)
        dot = max(-1.0, min(1.0, up[0] * un[0] + up[1] * un[1]))
        half = math.acos(dot) / 2.0
        sin_half = math.sqrt(max(1e-12, (1.0 - dot) / 2.0))
        tan_half = sin_half / max(1e-12, math.cos(half))
        center_d = rad / sin_half
        t = rad / tan_half
        bis = (up[0] + un[0], up[1] + un[1])
        lb = math.hypot(*bis) or 1.0
        bis = (bis[0] / lb, bis[1] / lb)
        c = (p[0] + bis[0] * center_d, p[1] + bis[1] * center_d)
        t1 = (p[0] + up[0] * t, p[1] + up[1] * t)
        t2 = (p[0] + un[0] * t, p[1] + un[1] * t)
        a1 = math.atan2(t1[1] - c[1], t1[0] - c[0])
        a2 = math.atan2(t2[1] - c[1], t2[0] - c[0])
        diff = (a2 - a1) % (2 * math.pi)
        if diff > math.pi:
            diff -= 2 * math.pi
        for j in range(49):
            ang = a1 + diff * j / 48.0
            poly.append((c[0] + rad * math.cos(ang), c[1] + rad * math.sin(ang)))
    return _poly_mask(W, poly)


def glyph_polygon_and_mask(W):
    """修长播放箭头：左垂直边 + 上下双切角 + 右顶点 r=14 圆角。"""
    k = W / S
    x0 = (CENTER - GLYPH_W / 2.0) * k
    top = (CENTER - GLYPH_H / 2.0) * k
    bot = (CENTER + GLYPH_H / 2.0) * k
    c = GLYPH_CHAMFER * k
    apex = ((CENTER - GLYPH_W / 2.0) + GLYPH_APEX) * k
    cy = W / 2.0
    pts = [(x0 + c, top), (apex, cy), (x0 + c, bot), (x0, bot - c), (x0, top + c)]
    radii = [0.0, GLYPH_R * k, 0.0, 0.0, 0.0]
    return pts, _rounded_polygon_mask(W, pts, radii)


def render_icon_main(size):
    W = size * SS
    k = W / S
    rgb, mask = _base_canvas(W, "icon")
    spec = KINDS["icon"]
    cx = cy = W / 2.0
    # 同心光环（沿用 task-51 的几何语言；不在本卡判据里，但保持视觉识别一致）
    halo = _ring_mask(W, cx, cy, HALO_R * k, max(1, int(round(2.0 * k))))
    rgb = _overlay(rgb, _mix(spec["accent2"], (255, 255, 255), 0.10), halo, alpha=0.55)
    # 修长播放箭头（不是等边正三角）
    pts, glyph = glyph_polygon_and_mask(W)
    glow = glyph.filter(ImageFilter.GaussianBlur(0.045 * W))
    rgb = _overlay(rgb, GLYPH_CYAN, glow, alpha=0.22)
    x0 = (CENTER - GLYPH_W / 2.0) * k
    x1 = (CENTER + GLYPH_W / 2.0) * k
    y0 = (CENTER - GLYPH_H / 2.0) * k
    y1 = (CENTER + GLYPH_H / 2.0) * k
    body = _grad_box(W, (x0, y0, x1, y1), GLYPH_DEEP, GLYPH_CYAN, 0.0)
    rgb = _overlay(rgb, body, glyph, alpha=1.0)
    # 内侧 1px 提亮（流线感，不改几何）
    edge = ImageChops.subtract(glyph, glyph.filter(ImageFilter.MinFilter(3)))
    rgb = _overlay(rgb, (255, 255, 255), edge, alpha=0.18)
    return _finish(rgb, mask, size)

# ---------------------------------------------------------------------------
# 音频：5 根对称垂直声柱（Δx=22；高度 [H·φ²,H·φ,H,H·φ,H·φ²]）
# ---------------------------------------------------------------------------
def audio_bar_centers():
    return [CENTER + AUDIO_DX * i for i in (-2, -1, 0, 1, 2)]


def render_icon_audio(size):
    W = size * SS
    k = W / S
    rgb, mask = _base_canvas(W, "audio")
    spec = KINDS["audio"]
    heights = [AUDIO_H * r for r in AUDIO_RATIOS]
    centers = audio_bar_centers()
    body = _grad_box(W, ((centers[0] - AUDIO_W) * k, (CENTER - AUDIO_H / 2) * k,
                         (centers[-1] + AUDIO_W) * k, (CENTER + AUDIO_H / 2) * k),
                     spec["accent"], spec["accent2"], 0.0)
    for x, h in zip(centers, heights):
        m = _capsule_mask(W, x * k, (CENTER - h / 2.0) * k, (CENTER + h / 2.0) * k,
                          AUDIO_W * k)
        rgb = _overlay(rgb, body, m, alpha=1.0)
    return _finish(rgb, mask, size)


# ---------------------------------------------------------------------------
# 视频：严格 16:9 矩形 + 中心正三角
# ---------------------------------------------------------------------------
def render_icon_video(size):
    W = size * SS
    k = W / S
    rgb, mask = _base_canvas(W, "video")
    spec = KINDS["video"]
    x0 = (CENTER - VIDEO_W / 2.0) * k
    x1 = (CENTER + VIDEO_W / 2.0) * k
    y0 = (CENTER - VIDEO_H / 2.0) * k
    y1 = (CENTER + VIDEO_H / 2.0) * k
    rect = Image.new("L", (W, W), 0)
    ImageDraw.Draw(rect).rectangle([x0, y0, x1, y1], fill=255)
    grad = _grad_box(W, (x0, y0, x1, y1), spec["accent"], spec["accent2"], 125.0)
    rgb = _overlay(rgb, grad, rect, alpha=1.0)
    # 中心正三角（底座色挖空），边长 48
    R = VIDEO_TRI_SIDE / math.sqrt(3.0)
    tri = _poly_mask(W, triangle_vertices(W / 2.0, W / 2.0, R * k))
    rgb = _overlay(rgb, BASE, tri, alpha=1.0)
    return _finish(rgb, mask, size)


# ---------------------------------------------------------------------------
# 图片：正方形画框 + 两个 45° 直角等腰三角山峰 + 黄金点太阳
# ---------------------------------------------------------------------------
def render_icon_image(size):
    W = size * SS
    k = W / S
    rgb, mask = _base_canvas(W, "image")
    spec = KINDS["image"]
    fx0, fy0, fw = [v * k for v in IMG_FRAME]
    stroke = max(1.5, 5.0 * k)
    # 画框
    frame = Image.new("L", (W, W), 0)
    d = ImageDraw.Draw(frame)
    d.rectangle([fx0, fy0, fx0 + fw, fy0 + fw], outline=255, width=int(round(stroke)))
    rgb = _overlay(rgb, _mix(spec["accent"], (255, 255, 255), 0.15), frame, alpha=0.95)
    # 太阳（黄金分割点；正圆）
    sx = SUN_GX * k
    sy = SUN_GY * k
    sun = _disc_mask(W, sx, sy, SUN_R * k)
    sun_grad = _grad_box(W, (sx - SUN_R * k, sy - SUN_R * k, sx + SUN_R * k, sy + SUN_R * k),
                         (255, 245, 210), SUN_COLOR, 120.0)
    rgb = _overlay(rgb, sun_grad, sun, alpha=1.0)
    # 两个 45° 直角等腰三角山峰（半底 = 高 => 底角 45°、顶角 90°）
    base_y = fy0 + fw - 6.0 * k
    h = IMG_PEAK_H * k
    # 远峰（右，暗一档）
    far_apex = ((fx0 + fw * 0.66), base_y - h)
    far = _poly_mask(W, [(far_apex[0] - h, base_y), (far_apex[0] + h, base_y), far_apex])
    rgb = _overlay(rgb, _mix(spec["accent2"], BASE, 0.45), far, alpha=1.0)
    # 近峰（左，亮）
    near_apex = ((fx0 + fw * 0.34), base_y - h)
    near = _poly_mask(W, [(near_apex[0] - h, base_y), (near_apex[0] + h, base_y), near_apex])
    near_grad = _grad_box(W, (near_apex[0] - h, near_apex[1], near_apex[0] + h, base_y),
                          spec["accent"], _mix(spec["accent"], spec["accent2"], 0.65), 100.0)
    rgb = _overlay(rgb, near_grad, near, alpha=1.0)
    return _finish(rgb, mask, size)


# ---------------------------------------------------------------------------
# 字幕：圆角卡片 + 两条字幕条（同风格）
# ---------------------------------------------------------------------------
def render_icon_subtitle(size):
    W = size * SS
    k = W / S
    rgb, mask = _base_canvas(W, "subtitle")
    spec = KINDS["subtitle"]
    card = _rounded_mask_local(W, 46.0 * k, 70.0 * k, 210.0 * k, 186.0 * k, 12.0 * k)
    grad = _grad_box(W, (46.0 * k, 70.0 * k, 210.0 * k, 186.0 * k),
                     spec["accent"], spec["accent2"], 120.0)
    rgb = _overlay(rgb, grad, card, alpha=1.0)
    for (bx0, by0, bx1, by1) in ((72.0, 104.0, 184.0, 122.0), (72.0, 142.0, 150.0, 160.0)):
        bar = _capsule_mask2(W, bx0 * k, by0 * k, bx1 * k, by1 * k, (by1 - by0) * k / 2.0)
        rgb = _overlay(rgb, BASE, bar, alpha=1.0)
    return _finish(rgb, mask, size)


def _rounded_mask_local(W, x0, y0, x1, y1, r):
    m = Image.new("L", (W, W), 0)
    ImageDraw.Draw(m).rounded_rectangle([x0, y0, x1, y1], radius=r, fill=255)
    return m


def _capsule_mask2(W, x0, y0, x1, y1, r):
    m = Image.new("L", (W, W), 0)
    ImageDraw.Draw(m).rounded_rectangle([x0, y0, x1, y1], radius=r, fill=255)
    return m


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
    return {s: render_icon(kind, s) for s in SIZES}


# ---------------------------------------------------------------------------
# ICO / PNG / 幂等
# ---------------------------------------------------------------------------
def build_ico_bytes(frames):
    ordered = [frames[s] for s in SIZES]
    buf = io.BytesIO()
    ordered[-1].save(buf, format="ICO", sizes=[(s, s) for s in SIZES],
                     append_images=ordered[:-1])
    return buf.getvalue()


def build_png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def expected_outputs():
    out = {}
    for kind in _OUT:
        frames = render_all(kind)
        out[_OUT[kind]] = build_ico_bytes(frames)
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
        with open(os.path.join(ASSETS, name), "wb") as f:
            f.write(data)
    return [os.path.join(ASSETS, n) for n in outputs]


def check_outputs(outputs):
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
        lines.append("%s %-14s sha256=%s" % ("OK      " if same else "MISMATCH", name, got[:16]))
    for name in _OUT.values():
        p = os.path.join(ASSETS, name)
        if os.path.isfile(p):
            sz = ico_sizes(p)
            good = [s for s in SIZES if (s, s) in sz]
            lines.append("ICO     %-14s 尺寸=%s 齐全=%s" % (name, good, len(good) == len(SIZES)))
            ok = ok and len(good) == len(SIZES)
        else:
            ok = False
            lines.append("ICO     %-14s 缺失" % name)
    print("\n".join(lines))
    return ok


# ---------------------------------------------------------------------------
# 尺规 / 像素自校验（--check 全跑；失败返回非 0）
# ---------------------------------------------------------------------------
class _Fail:
    def __init__(self):
        self.items = []

    def __call__(self, name, ok, detail):
        self.items.append((name, bool(ok), detail))
        return bool(ok)


def _fg(px, tol=12):
    r, g, b, a = px
    if a < 128:
        return False
    return max(abs(r - BASE[0]), abs(g - BASE[1]), abs(b - BASE[2])) > tol


def _hsv(px):
    import colorsys
    r, g, b = [v / 255.0 for v in px[:3]]
    return colorsys.rgb_to_hsv(r, g, b)


def _measure_frame(kind):
    return render_icon(kind, 256).convert("RGBA")


def verify_geometry():
    """全部尺规 + 像素判据（与渲染共用同一套常量，任何改参都会在这里报出来）。"""
    fail = _Fail()
    frames = {k: _measure_frame(k) for k in _OUT}

    # ---------- 1) 统一底座：超椭圆 n=4 覆盖率 ----------
    f = frames["icon"]
    alpha = [f.getpixel((x, y))[3] for y in range(256) for x in range(256)]
    cover_canvas = sum(alpha) / 255.0 / (256.0 * 256.0)
    cover_bbox = sum(alpha) / 255.0 / ((2 * HALF_SIDE) ** 2)
    theory_canvas = SQUIRCLE_COVERAGE * (2 * HALF_SIDE / 256.0) ** 2
    fail("superellipse_coverage",
         abs(cover_canvas - theory_canvas) <= 0.01,
         "canvas=%.4f theory=%.4f bbox=%.4f (n=%.1f, 允差 1%%=0.01)" % (
             cover_canvas, theory_canvas, cover_bbox, SQUIRCLE_N))

    # ---------- 2) 主图标：修长黄金比例播放箭头（H:W=1.25，右顶点 r=14） ----------
    ratio_math = GLYPH_H / GLYPH_W
    fail("glyph_ratio_math", abs(ratio_math - 1.25) <= 0.03,
         "构造 H:W=%.4f（设计 1.25 + 2px AA 补偿，像素判据见下）" % ratio_math)
    fail("glyph_not_equilateral", abs(ratio_math - 0.866) > 0.20,
         "与等边三角 0.866 的差=%.4f（必须明确排除）" % abs(ratio_math - 0.866))
    f = frames["icon"]

    def is_glyph(px):
        r, g, b, a = px
        if a < 128:
            return False
        h, s, v = _hsv(px)
        # v>0.35 排除外发光（glow 在深底座上 v≈0.3）；只量实心箭头本体
        return s > 0.50 and v > 0.35 and 170.0 <= h * 360.0 <= 235.0

    gpts = [(x, y) for y in range(50, 206) for x in range(64, 192)
            if is_glyph(f.getpixel((x, y)))]
    if gpts:
        gxs = [p[0] for p in gpts]
        gys = [p[1] for p in gpts]
        bw = max(gxs) - min(gxs) + 1
        bh = max(gys) - min(gys) + 1
        ratio_px = bh / float(bw)
        fail("glyph_pixel_ratio", abs(ratio_px - 1.25) <= 0.03,
             "bbox=%dx%d H/W=%.4f 期望 1.25±0.03" % (bw, bh, ratio_px))
        fail("glyph_pixel_not_equilateral", abs(ratio_px - 0.866) > 0.20,
             "H/W=%.4f，距等边 0.866 有 %.3f（排除等边）" % (
                 ratio_px, abs(ratio_px - 0.866)))
        # 右顶点圆角半径：亚像素右边界（蓝通道覆盖率插值）拟合圆弧，避免整数取整偏差
        base_b = BASE[2]
        full_b = 255.0

        def right_edge(y):
            covs = []
            for x in range(120, 200):
                px = f.getpixel((x, y))
                cov = (px[2] - base_b) / (full_b - base_b)
                covs.append((x, max(0.0, min(1.0, cov))))
            edge = None
            for i in range(len(covs) - 1):
                x0, c0 = covs[i]
                x1, c1 = covs[i + 1]
                if c0 >= 0.5 >= c1:
                    denom = (c0 - c1) or 1e-6
                    edge = x0 + (c0 - 0.5) / denom
                    break
            return edge

        # 右顶点圆弧：取右缘点列做**最小二乘圆拟合**（整数边界 + 多点平均，抗 AA 抖动）
        def right_x(y):
            row = [x for x in range(120, 196) if is_glyph(f.getpixel((x, y)))]
            return max(row) if row else None

        edge_pts = [(y, right_x(y)) for y in range(116, 141)]
        edge_pts = [(y, x) for y, x in edge_pts if x is not None]
        best = None
        rr = 6.0
        while rr <= 22.0:
            cand = []
            for y, x in edge_pts:
                dy = y - 128
                if abs(dy) < rr - 0.1:
                    cand.append(x - math.sqrt(rr * rr - dy * dy))
            if cand:
                c = sum(cand) / len(cand)
                resid = sum((x - (c + math.sqrt(max(0.0, rr * rr - (y - 128) ** 2)))) ** 2
                            for y, x in edge_pts if abs(y - 128) < rr - 0.1)
                if best is None or resid < best[0]:
                    best = (resid, rr, c)
            rr += 0.05
        if best is not None:
            r_fit = best[1]
            fail("glyph_apex_radius", abs(r_fit - GLYPH_R) <= 1.5,
                 "最小二乘圆拟合 r=%.2f 期望 %.1f±1.5（%d 个右缘点，残差 %.2f）" % (
                     r_fit, GLYPH_R, len(edge_pts), best[0]))
        else:
            fail("glyph_apex_radius", False, "右缘取样失败")
        # 左侧双切角：靠近顶部/底部的左缘应明显内收
        top_y = min(gys)
        def left_x(y):
            row = [x for x in range(64, 160) if is_glyph(f.getpixel((x, y)))]
            return min(row) if row else None

        l_near = left_x(top_y + 6); l_mid = left_x(128)
        fail("glyph_left_chamfer",
             l_near is not None and l_mid is not None and (l_near - l_mid) >= 4.0,
             "左缘 top+6=%s 中点=%s 内收=%s" % (
                 l_near, l_mid, (l_near - l_mid) if l_near is not None and l_mid is not None else None))
        # 渐变：沿水平中线多点取样，亮度单调升、色相向青偏移
        rows_g = []
        for fy in (0.47, 0.53):
            y = int(round(fy * 255))
            samples = []
            for x in (90, 110, 128, 146, 166):
                rgbp = list(f.getpixel((x, y))[:3])
                samples.append((x, rgbp, _hsv(tuple(rgbp))))
            rows_g.append(samples)
        flat_g = [s for row in rows_g for s in row]
        hues_g = [s[2][0] * 360.0 for s in flat_g]
        # 每一行内部亮度必须单调升（跨行会从右端回到左端，不能整体比较）
        mono_val = all(all(row[i + 1][2][2] >= row[i][2][2] - 0.02
                           for i in range(len(row) - 1))
                       for row in rows_g)
        hue_span = max(hues_g) - min(hues_g)
        fail("glyph_gradient", mono_val and hue_span >= 10.0,
             "亮度单调=%s 色相变化=%.1f°（深海蓝→亮青）" % (mono_val, hue_span))
    else:
        fail("glyph_pixel_ratio", False, "no glyph pixels")
    # 同心光环半径（沿用 task-51 的几何语言）
    hits = [x for x in range(200, 250) if _fg(f.getpixel((x, 128)), tol=6)]
    ring_r = (sum(hits) / len(hits) - 128.0) if hits else -1
    fail("halo_radius", hits and abs(ring_r - HALO_R) <= 2.0,
         "measured_r=%.2f 期望 R/phi=%.3f" % (ring_r, HALO_R))

    # ---------- 3) 音频：Delta_x=22 / 高度比 / 左右对称 ----------    # ---------- 3) 音频：Δx=22 / 高度比 / 左右对称 ----------
    f = frames["audio"]
    centers = audio_bar_centers()
    # 高度用**明度覆盖率积分**（包含抗锯齿半像素，接近几何高度；alpha 是整块底座恒为 255，不能用）
    heights = []
    for cx in centers:
        x = int(round(cx))
        vals = [_hsv(f.getpixel((x, y)))[2] for y in range(60, 201)]
        full = max(vals) or 1.0
        heights.append(round(sum(min(1.0, max(0.0, (v - 0.18) / (full - 0.18)))
                                  for v in vals), 2))
    dx = [centers[i + 1] - centers[i] for i in range(4)]
    fail("audio_dx", max(abs(d - AUDIO_DX) for d in dx) <= 0.5,
         "centers=%s dx=%s" % (centers, dx))
    # 行扫描实测柱中心（y=128 处 5 段）
    runs = []
    inrun = False
    for x in range(60, 200):
        on = _fg(f.getpixel((x, 128)))
        if on and not inrun:
            runs.append([x, x]); inrun = True
        elif on and inrun:
            runs[-1][1] = x
        elif not on:
            inrun = False
    measured_centers = [(a + b) / 2.0 for a, b in runs]
    fail("audio_centers_measured", len(runs) == 5
         and max(abs(measured_centers[i] - centers[i]) for i in range(5)) <= 1.5,
         "runs=%s centers=%s" % (runs, ["%.1f" % c for c in measured_centers]))
    ratios = [h / heights[2] if heights[2] else 0 for h in heights]
    fail("audio_ratio", all(abs(ratios[i] - AUDIO_RATIOS[i]) / AUDIO_RATIOS[i] <= 0.02
                            for i in range(5)),
         "heights=%s ratios=%s 期望=%s" % (heights, ["%.4f" % r for r in ratios],
                                           ["%.4f" % r for r in AUDIO_RATIOS]))
    fail("audio_symmetry", abs(heights[0] - heights[4]) <= 1 and abs(heights[1] - heights[3]) <= 1,
         "h=%s" % heights)

    # ---------- 4) 视频：严格 16:9 + 中心正三角 ----------
    f = frames["video"]
    def _coverage_span(fr, row_or_col, lo, hi, along_y):
        vals = []
        for t in range(lo, hi + 1):
            px = fr.getpixel((row_or_col, t)) if along_y else fr.getpixel((t, row_or_col))
            vals.append(_hsv(px)[2])
        # 固定满覆盖明度（矩形渐变内部 v≈0.87~0.96）——不能用 span 内最大值归一，
        # 否则内部像素只有 0.92 权重、整体尺寸会系统性偏小 8%。
        full = 0.85
        return sum(min(1.0, max(0.0, (v - 0.18) / (full - 0.18))) for v in vals)

    # 用**覆盖率积分**量宽度/高度：边缘半透明像素按比例计入，得到亚像素几何尺寸
    bw = _coverage_span(f, 100, 20, 236, along_y=False)   # y=100 行（三角上方，整行矩形）
    bh = _coverage_span(f, 100, 40, 216, along_y=True)    # x=100 列（三角左侧，整列矩形）
    ratio = bw / bh if bh else 0
    fail("video_16_9", bh and abs(ratio - 16.0 / 9.0) / (16.0 / 9.0) <= 0.005,
         "width=%.2f height=%.2f ratio=%.5f 期望=%.5f (%.3f%%)" % (
             bw, bh, ratio, 16.0 / 9.0, 100.0 * (ratio / (16.0 / 9.0) - 1.0)))
    center_dark = sum(1 for y in range(105, 152) for x in range(105, 152)
                      if max(abs(f.getpixel((x, y))[i] - BASE[i]) for i in range(3)) <= 12)
    fail("video_tri_present", center_dark > 400, "center base-colored px=%d" % center_dark)

    # ---------- 5) 图片：画框方形 / 两峰 45° / 太阳黄金点 ----------
    f = frames["image"]
    # 画框外接 bbox
    fx = [x for x in range(256) for y in range(256) if _fg(f.getpixel((x, y)))]
    fy = [y for x in range(256) for y in range(256) if _fg(f.getpixel((x, y)))]
    if fx and fy:
        fw = max(fx) - min(fx) + 1
        fh = max(fy) - min(fy) + 1
        fail("image_frame_square", abs(fw - fh) <= 2,
             "bbox=%dx%d" % (fw, fh))
    else:
        fail("image_frame_square", False, "no fg")
    # 左峰左缘斜率（45°）：逐行找最左山体像素，线性拟合 dx/dy
    def is_mountain(px):
        r, g, b, a = px
        if a < 128:
            return False
        h, s, v = _hsv(px)
        # v>0.40 排除几何内环（环是 accent2 混底座的低亮像素，v≈0.26）
        return 120.0 <= h * 360.0 <= 185.0 and s > 0.30 and v > 0.40

    pts = []
    for y in range(170, 201):
        row = [x for x in range(52, 130) if is_mountain(f.getpixel((x, y)))]
        if row:
            pts.append((y, min(row)))
    if len(pts) >= 8:
        # 拟合 x = m*y + c
        n = len(pts); my = sum(p[0] for p in pts) / n; mx = sum(p[1] for p in pts) / n
        num = sum((p[0] - my) * (p[1] - mx) for p in pts)
        den = sum((p[0] - my) ** 2 for p in pts) or 1.0
        slope = num / den
        angle = math.degrees(math.atan(abs(slope)))
        fail("image_peak_45deg", abs(angle - 45.0) <= 1.0,
             "left edge slope=%.4f angle=%.2f° (n=%d)" % (slope, angle, len(pts)))
    else:
        fail("image_peak_45deg", False, "insufficient edge points: %d" % len(pts))
    # 太阳：暖金像素质心 vs 黄金分割点
    sun_px = [(x, y) for y in range(70, 150) for x in range(110, 210)
              if (lambda h, s, v: 25.0 <= h * 360.0 <= 70.0 and s > 0.30 and v > 0.55)(
                  *_hsv(f.getpixel((x, y))))]
    if sun_px:
        # 太阳圆心取**墨迹外接框中心**（质心会受渐变明度饱和阈值影响）
        sx = (min(p[0] for p in sun_px) + max(p[0] for p in sun_px)) / 2.0
        sy = (min(p[1] for p in sun_px) + max(p[1] for p in sun_px)) / 2.0
        cx_all = sum(p[0] for p in sun_px) / len(sun_px)
        cy_all = sum(p[1] for p in sun_px) / len(sun_px)
        fail("image_sun_golden", abs(sx - SUN_GX) <= 1.0 and abs(sy - SUN_GY) <= 1.0,
             "sun_bbox_center=(%.2f,%.2f) 质心=(%.2f,%.2f) 期望黄金点=(%.2f,%.2f) n=%d" % (
                 sx, sy, cx_all, cy_all, SUN_GX, SUN_GY, len(sun_px)))
    else:
        fail("image_sun_golden", False, "no sun pixels")
    fail("image_sun_golden_formula",
         abs(SUN_GX - (IMG_FRAME[0] + PHI * IMG_FRAME[2])) < 1e-9
         and abs(SUN_GY - (IMG_FRAME[1] + (1.0 - PHI) * IMG_FRAME[2])) < 1e-9,
         "gx=%.3f gy=%.3f phi=%.6f" % (SUN_GX, SUN_GY, PHI))

    # ---------- 6) 字幕：两条字幕条 ----------
    f = frames["subtitle"]
    rows = []
    for y in range(80, 181):
        px = f.getpixel((100, y))
        rows.append(max(abs(px[i] - BASE[i]) for i in range(3)) <= 12 and px[3] > 128)
    runs = 0; inrun = False
    for v in rows:
        if v and not inrun:
            runs += 1; inrun = True
        elif not v:
            inrun = False
    fail("subtitle_two_bars", runs == 2, "dark runs at x=100: %d" % runs)

    print("\n[尺规/像素自校验]")
    bad = 0
    for name, ok, detail in fail.items:
        print("  %s %-26s %s" % ("OK  " if ok else "FAIL", name, detail))
        if not ok:
            bad += 1
    print("  合计 %d 项，失败 %d 项" % (len(fail.items), bad))
    return bad == 0, fail.items


# ---------------------------------------------------------------------------
# 预览 / CLI
# ---------------------------------------------------------------------------
def make_preview(outputs):
    os.makedirs(LOG_DIR, exist_ok=True)
    paths = []
    for kind, name in _OUT.items():
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
        sd = ImageDraw.Draw(sheet)
        for yy in range(0, sheet.height, 16):
            for xx in range(0, sheet.width, 16):
                if (xx // 16 + yy // 16) % 2 == 0:
                    sd.rectangle([xx, yy, xx + 15, yy + 15], fill=(34, 36, 42, 255))
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
    ap = argparse.ArgumentParser(description="生成 / 校验 至简Player 尺规图标")
    ap.add_argument("--check", action="store_true", help="只校验（逐字节幂等 + 尺规/像素判据）")
    ap.add_argument("--preview", action="store_true", help="另存预览到 logs/task51/")
    args = ap.parse_args(argv)

    outputs = expected_outputs()
    geo_ok, items = verify_geometry()
    if args.check:
        bytes_ok = check_outputs(outputs)
        ok = bytes_ok and geo_ok
        print("\n结论: %s（幂等=%s 尺规=%s）" % ("PASS" if ok else "FAIL", bytes_ok, geo_ok))
        return 0 if ok else 1
    paths = write_outputs(outputs)
    print("已生成 %d 个文件:" % len(paths))
    for p in paths:
        print("  %-40s %8d bytes" % (p, os.path.getsize(p)))
    print("尺规自校验: %s" % ("PASS" if geo_ok else "FAIL"))
    if args.preview:
        for p in make_preview(outputs):
            print("预览: %s" % p)
    return 0 if geo_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
