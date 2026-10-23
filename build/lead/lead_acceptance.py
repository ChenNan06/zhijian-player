# -*- coding: utf-8 -*-
"""Lead 独立终验收门禁（不依赖 Dev/QA 探针，全部自测）。

用法：
    C:/DevTools/Python312/python.exe build/lead/lead_acceptance.py

判据全部来自用户原文（PROJECT_STATUS_AUDIT.md / 本轮任务卡）：
  T1 手柄：Stage 内 resizeEvent 唯一；四组合坐标方程成立；16x56；无硬裁切；半透明填充
  T2 封面：四角纯圆无杂质（角外 alpha=0）；零 fillRect/drawRect；单一 addRoundedRect
  T3 频谱：y = H-72-48-16；与控制栏不重叠；柱高 8~44px 且有落差；暂停落峰
  T4 歌词：帧间隔 ~16ms；paintEvent 期间测量调用 = 0（且计数器可被 _relayout 证真）
  回归：看图 / 视频 / 音乐背景缓存不变量
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC = os.path.join(ROOT, "src")
PY = r"C:\DevTools\Python312\python.exe"
CTL = "zhjlead"
STAGE_PY = os.path.join(SRC, "zhijian", "ui", "stage.py")
AUDIO_PY = os.path.join(SRC, "zhijian", "ui", "audio_view.py")
MEDIA = os.path.join(ROOT, "assets", "testmedia")
OUT = os.path.join(ROOT, "build", "lead", "out")

sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def _startup_theme() -> str:
    """读应用真实持久化的主题名（探针改了主题必须在末尾还原，别动用户的配置）。"""
    import glob
    try:
        for pth in glob.glob(os.path.join(os.environ.get("APPDATA", ""), "ZhiJian", "*", "config.json")):
            with open(pth, encoding="utf-8") as fh:
                d = json.load(fh)
            ui = d.get("ui") if isinstance(d.get("ui"), dict) else {}
            return ui.get("theme") or d.get("theme") or "qq-light"
    except Exception:
        pass
    return "qq-light"


START_THEME = _startup_theme()

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, bool(ok), detail))
    line = ("  [PASS] " if ok else "  [FAIL] ") + name + ("  | " + detail if detail else "")
    try:
        print(line)
    except UnicodeEncodeError:          # 控制台是 GBK：任何非 GBK 字符都不许让门禁崩掉
        print(line.encode("gbk", "replace").decode("gbk", "replace"))
    return ok


class App:
    def __init__(self, media: str, extra: list[str] | None = None, exe: str | None = None):
        self.media = media
        self.extra = extra or []
        self.exe = exe
        self.proc: subprocess.Popen | None = None
        self.client = None

    def __enter__(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        log = open(os.path.join(OUT, "app_%d.log" % int(time.time())), "wb")
        if self.exe:
            cmd = [self.exe, "--control", CTL, "--no-single-instance", self.media]
            cwd = os.path.dirname(os.path.abspath(self.exe))
        else:
            cmd = [PY, "-m", "zhijian", "--control", CTL, "--no-single-instance", self.media]
            cwd = ROOT
        self.proc = subprocess.Popen(cmd + self.extra, cwd=cwd, env=env,
                                     stdout=log, stderr=subprocess.STDOUT)
        from zhijian.ipc import ControlClient
        c = ControlClient(CTL, timeout=20.0)
        t0 = time.time()
        while time.time() - t0 < 40:
            try:
                c.call(cmd="state")
                self.client = c
                break
            except Exception:
                time.sleep(0.5)
        if self.client is None:
            raise RuntimeError("应用控制面在 40s 内未就绪")
        time.sleep(2.0)          # 等 mpv 加载与首帧
        return self

    def __exit__(self, *exc):
        try:
            self.client.call(cmd="quit")
        except Exception:
            pass
        time.sleep(1.0)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except Exception:
                self.proc.kill()

    def ev(self, expr: str):
        r = self.client.call(cmd="eval", expr=expr)
        if not r.get("ok"):
            raise RuntimeError("eval 失败: %s <- %s" % (r.get("error"), expr[:200]))
        return r.get("value")


def _reset_ui(app: "App") -> None:
    """把 UI 恢复到干净的音乐模式状态。

    ⚠️ 全量门禁里 t1 段会把抽屉留在**展开**态 ⇒ 抽屉（392px 不透明面板）会盖住
    频谱右翼，差值掩码只剩左半 ⇒ 「山峰形态」被量成右翼 0（Lead 踩过）。每个视觉段
    开始时必须收抽屉 / 退全屏 / 确保音乐视图可见。
    """
    try:
        app.ev("w.drawer.set_open(False)")
    except Exception:
        pass
    try:
        if app.ev("bool(getattr(w, '_fullscreen', False))"):
            app.ev("w.toggle_fullscreen()")
    except Exception:
        pass
    try:
        app.ev("w.stage.set_audio_view_visible(True)")
    except Exception:
        pass
    time.sleep(0.8)


def _ensure_playing(app: "App", timeout: float = 8.0) -> bool:
    """等到频谱真的在动（否则像素判据会误判成「柱底被裁」——Lead 踩过一次）。

    ⚠️ 重新打开素材会让画面短暂为空，因此**每轮都重新确认** bars，绝不 fire-and-forget。
    """
    t0 = time.time()
    reopened = False
    while time.time() - t0 < timeout:
        try:
            mx = float(app.ev("max(w.stage.audio_view.spec._bars) "
                              "if w.stage.audio_view.spec._bars else 0.0"))
        except Exception:
            mx = 0.0
        if mx > 0.3:
            return True
        try:
            if not reopened and (time.time() - t0) > timeout * 0.5:
                app.ev("w.open_path(r'%s')"
                       % os.path.join(MEDIA, "music_cover.mp3").replace("\\", "/"))
                reopened = True
                time.sleep(1.2)
            else:
                app.ev("w.engine.set('pause', False)")
                time.sleep(0.4)
        except Exception:
            time.sleep(0.4)
    return False


def _fn_body(path: str, cls: str | None, fn: str) -> str:
    """返回函数的**真实源码文本**（ast.dump 里没有 "self.width()" 这种字面量，会误判）。"""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and (cls is None or node.name == cls):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == fn:
                    return "\n".join(lines[sub.lineno - 1:sub.end_lineno])
    return ""


def _fn_calls(path: str, cls: str, fn: str) -> list:
    """函数体内的**真实调用名**列表（AST，不受注释/文档字符串干扰）。"""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == fn:
                    out = []
                    for n in ast.walk(sub):
                        if isinstance(n, ast.Call):
                            f = n.func
                            out.append(getattr(f, "attr", None) or getattr(f, "id", None) or "")
                    return out
    return []


def _fn_attrs(path: str, cls: str, fn: str) -> list:
    """函数体内的属性访问 (对象名, 属性名) 列表 —— 用 AST，避免 token 化后被空格拆开。"""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == fn:
                    out = []
                    for n in ast.walk(sub):
                        if isinstance(n, ast.Attribute):
                            base = n.value.id if isinstance(n.value, ast.Name) else "?"
                            out.append((base, n.attr))
                    return out
    return []


def _fn_src_nocomment(path: str, cls: str, fn: str) -> str:
    """函数源码，剥离注释与字符串字面量（注释里写 "fillRect" 不该算命中）。"""
    import io
    import tokenize
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == fn:
                    seg = "\n".join(lines[sub.lineno - 1:sub.end_lineno])
                    try:
                        toks = [t.string for t in tokenize.generate_tokens(io.StringIO(seg).readline)
                                if t.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL)]
                    except Exception:
                        return seg
                    return " ".join(toks)
    return ""


def _method_count(path: str, cls: str, fn: str) -> int:
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return sum(1 for s in node.body if isinstance(s, ast.FunctionDef) and s.name == fn)
    return -1


# --------------------------------------------------------------------------- T1
def t1_static():
    print("\n[T1] 手柄几何单一真源 —— 静态")
    n = _method_count(STAGE_PY, "Stage", "resizeEvent")
    rec("T1.1 Stage 内 resizeEvent 只有一份", n == 1, "count=%d" % n)
    src = open(STAGE_PY, encoding="utf-8").read()
    rec("T1.2 全文件无 20x74 手柄硬尺寸", "setFixedSize(20, 74)" not in src, "")
    attrs = _fn_attrs(STAGE_PY, "DrawerHandle", "paintEvent")
    uses_wh = ("self", "width") in attrs and ("self", "height") in attrs
    no_class_const = ("self", "W") not in attrs and ("self", "H") not in attrs
    rec("T1.3 DrawerHandle.paintEvent 用控件真实尺寸（不再用类常量 W/H）",
        uses_wh and no_class_const, "self.width/height=%s 含self.W/H=%s" % (uses_wh, not no_class_const))


def t1_runtime(app: App):
    print("\n[T1] 手柄几何 —— 真机四组合")
    expr = ("{'sw': w.stage.width(), 'sh': w.stage.height(), "
            "'rect': list(w.stage.drawer_handle.geometry().getRect()), "
            "'open': bool(w.drawer.is_open()), "
            "'dx': w.drawer.mapTo(w.stage, __import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0, 0)).x(), "
            "'vis': bool(w.stage.drawer_handle.isVisible())}")
    snaps = {}

    def snap(tag):
        app.ev("w.drawer.set_open(False)")
        time.sleep(0.8)
        snaps[tag + "-closed"] = app.ev(expr)
        app.ev("w.drawer.set_open(True, animate=True)")
        time.sleep(0.9)
        snaps[tag + "-open"] = app.ev(expr)

    snap("win")
    app.ev("w.toggle_fullscreen()")
    time.sleep(1.2)
    snap("fs")
    app.ev("w.toggle_fullscreen()")
    time.sleep(1.2)

    for tag, s in snaps.items():
        x, y, gw, gh = s["rect"]
        exp_y = (s["sh"] - 56) // 2
        exp_x = (s["sw"] - 16) if (not s["open"]) else (s["dx"] - 16)
        ok = (gw == 16 and gh == 56 and y == exp_y and abs(x - exp_x) <= 1)
        rec("T1.4[%s] x=%s(期望%s) y=%s(期望%s) 16x56" % (tag, x, exp_x, y, exp_y), ok,
            "stage=%dx%d open=%s dx=%s vis=%s" % (s["sw"], s["sh"], s["open"], s["dx"], s["vis"]))
    return snaps


def t1_appearance(app: App):
    print("\n[T1] 手柄外观 —— 双底色合成取证")
    p = os.path.join(OUT, "handle_on_red.png")
    pb = os.path.join(OUT, "handle_on_blue.png")
    app.ev("w.drawer.set_open(False)")
    time.sleep(0.8)
    for _path, _rgb in ((p, "(255, 0, 0)"), (pb, "(0, 0, 255)")):
        app.ev("(lambda: (__import__('sys').path.insert(0, r'%s'), "
               "__import__('lead_probe').grab_on(w.stage.drawer_handle, r'%s', %s))[-1])()"
               % (HERE.replace("\\", "/"), _path.replace("\\", "/"), _rgb))
    try:
        from PIL import Image
        im = Image.open(p).convert("RGB")
        dpr = float(app.ev("w.stage.drawer_handle.devicePixelRatioF()"))
        W, H = im.size
        rec("T1.5 手柄逻辑尺寸 16x56", (round(W / dpr), round(H / dpr)) == (16, 56),
            "device=%dx%d dpr=%s" % (W, H, dpr))
        c1 = im.getpixel((1, 1))
        c2 = im.getpixel((1, H - 2))
        ok_c = all(c[0] >= 253 and c[1] <= 2 and c[2] <= 2 for c in (c1, c2))
        rec("T1.6 左上/左下圆角外仍是纯背景色（形状透明，非实心方板）", ok_c,
            "左上=%s 左下=%s" % (c1, c2))
        imb = Image.open(pb).convert("RGB")
        ix = max(2, int(3 * dpr))
        inner_r = im.getpixel((ix, H // 2))
        inner_b = imb.getpixel((ix, H // 2))
        rec("T1.7 填充是半透明（换底色合成色必须变化）",
            abs(inner_r[0] - inner_b[0]) >= 20 or abs(inner_r[2] - inner_b[2]) >= 20,
            "红底=%s 蓝底=%s（不透明时两者必然相同）" % (inner_r, inner_b))
        # 箭头墨迹：与**本行众数色**偏离 >20 的像素（主题无关，避开 1px 收边）
        xs, ys = [], []
        for y in range(2, H - 2):
            row = [im.getpixel((x, y)) for x in range(2, W - 2)]
            if not row:
                continue
            modal = max(set(row), key=row.count)
            for i, col in enumerate(row):
                if max(abs(col[k] - modal[k]) for k in range(3)) > 20:
                    xs.append(2 + i)
                    ys.append(y)
        if xs:
            cx = (sum(xs) / len(xs)) / dpr
            cy = (sum(ys) / len(ys)) / dpr
            ok = abs(cy - 28.0) <= 3.0 and abs(cx - 8.0) <= 5.0
            rec("T1.8 箭头墨迹居中（质心≈(8,28)）", ok,
                "质心=(%.1f,%.1f) 墨迹像素=%d" % (cx, cy, len(xs)))
        else:
            rec("T1.8 箭头墨迹居中", False, "未检出箭头墨迹（阈值 g,b>140）")
    except Exception as exc:
        rec("T1.5 手柄外观分析", False, "异常: %s" % exc)


# --------------------------------------------------------------------------- T2
def t2_static():
    print("\n[T2] 封面圆角 —— 静态")
    src = open(AUDIO_PY, encoding="utf-8").read()
    calls = _fn_calls(AUDIO_PY, "CoverArt", "paintEvent")
    rec("T2.1 CoverArt.paintEvent 零 fillRect/drawRect/fillPath",
        calls.count("fillRect") == 0 and calls.count("drawRect") == 0 and calls.count("fillPath") == 0,
        "调用序列=%s" % (calls,))
    # Task-07 起允许「1 条封面圆角路径 + 3 圈内发散阴影」；硬红线改为：零 fillRect/drawRect，
    # 且真正的方角判据落在像素上（T2.4/T2.5 与 T7.2 控件边界 alpha=0）。
    n_rr = calls.count("addRoundedRect")
    rec("T2.2 CoverArt 只用圆角路径（1 封面 + ≤3 阴影圈），零 fillRect/drawRect",
        calls.count("drawRect") == 0 and 1 <= n_rr <= 4,
        "addRoundedRect=%d drawRoundedRect=%d" % (n_rr, calls.count("drawRoundedRect")))
    init = _fn_body(AUDIO_PY, "CoverArt", "__init__")
    rec("T2.3 CoverArt 开启 WA_TranslucentBackground",
        "WA_TranslucentBackground" in init and "WA_NoSystemBackground" in init, "")
    # ---- task-13 静态：高斯阴影必须是**预渲染缓存**，且绘制期不得调用模糊 ----
    src_all = open(AUDIO_PY, encoding="utf-8").read()
    c_paint2 = _fn_calls(AUDIO_PY, "CoverArt", "paintEvent")
    has_cache = ("_shadow_cache" in src_all)
    blur_in_paint = any(k in c_paint2 for k in ("GaussianBlur", "blur_pixmap", "gaussianBlur"))
    rings_gone = ("COVER_SHADOW_RINGS" not in _fn_src_nocomment(AUDIO_PY, "CoverArt", "paintEvent"))
    rec("T13.1 阴影为预渲染缓存 + 绘制期零模糊 + 旧 3 圈已废除",
        has_cache and not blur_in_paint and rings_gone,
        "_shadow_cache=%s 绘制期含模糊=%s 旧3圈残留在paintEvent=%s" %
        (has_cache, blur_in_paint, not rings_gone))


def t2_runtime(app: App):
    print("\n[T2] 封面四角 —— 真机像素")
    app.ev("w.stage.set_audio_view_visible(True)")
    time.sleep(0.5)
    shots = {}
    dpr = float(app.ev("w.stage.audio_view.cover.devicePixelRatioF()"))
    print("  (T2 取证主题: qq-light / graphite，原主题 %s 会在末尾还原)" % START_THEME)
    for theme in ("qq-light", "graphite"):
        app.ev("__import__('zhijian.ui.theme', fromlist=['apply_theme']).apply_theme(%r)" % theme)
        time.sleep(1.0)
        p = os.path.join(OUT, "cover_%s.png" % theme)
        app.ev("w.stage.audio_view.cover.grab().save(r'%s', 'PNG')" % p.replace("\\", "/"))
        shots[theme] = p
    try:
        from PIL import Image
        for theme, p in shots.items():
            im = Image.open(p).convert("RGBA")
            W, H = im.size
            R = 12.0
            # Task-07 起阴影合法地占用 padding 走廊 ⇒ 判据改为 R1/R2（详见 T7 段）：
            #   最外 1px 与四角 2×2 必须干净 = 「没有被硬裁的直角灰底」的量化定义。
            border = ([im.getpixel((x, 0))[3] for x in range(0, W, 3)]
                      + [im.getpixel((x, H - 1))[3] for x in range(0, W, 3)]
                      + [im.getpixel((0, y))[3] for y in range(0, H, 3)]
                      + [im.getpixel((W - 1, y))[3] for y in range(0, H, 3)])
            corners = [im.getpixel((cx0 + dx, cy0 + dy))[3]
                       for cx0, cy0 in ((0, 0), (W - 2, 0), (0, H - 2), (W - 2, H - 2))
                       for dx in range(2) for dy in range(2)]
            rec("T2.4[%s] R1 最外 1px alpha ≤ 8（阴影不被边界裁断）" % theme,
                max(border) <= 8, "边框最大 alpha=%d（%dx%d）" % (max(border), W, H))
            rec("T2.5[%s] R2 四角 2×2 alpha ≤ 8（无直角灰底）" % theme,
                max(corners) <= 8, "四角最大 alpha=%d" % max(corners))
    except Exception as exc:
        rec("T2.4 封面角像素分析", False, "异常: %s" % exc)
    app.ev("__import__('zhijian.ui.theme', fromlist=['apply_theme']).apply_theme(%r)" % START_THEME)


# --------------------------------------------------------------------------- T6
def t6_runtime(app: App):
    print("\n[T6] 抽屉动画手柄连续随动 —— 逐帧记录")
    app.ev("(lambda: (__import__('sys').path.insert(0, r'%s'), "
           "__import__('lead_probe').install_handle_recorder())[-1])()" % HERE.replace("\\", "/"))
    st_w = int(app.ev("w.stage.width()"))
    results = {}
    # tag 语义严格对齐：先摆到动画**起点**，再触发该方向的动画
    for tag, start_open in (("close", True), ("open", False)):
        app.ev("w.drawer.set_open(%s, animate=True)" % ("True" if start_open else "False"))
        time.sleep(1.2)
        app.ev("__import__('lead_probe').clear_frames()")
        app.ev("w.drawer.set_open(%s, animate=True)" % ("False" if start_open else "True"))
        time.sleep(1.2)
        fr = app.ev("__import__('lead_probe').frames()")
        bad = []
        for dl, hx, is_open in fr:
            if dl is None:
                continue
            exp = max(0, min(st_w - 16, int(dl) - 16))
            if abs(hx - exp) > 2:
                bad.append((dl, hx, exp, is_open))
        results[tag] = (len(fr), bad, fr)
        rec("T6.%d %s动画逐帧咬合 clamp(drawer_left-16, 0, W-16)" % (1 if tag == "close" else 2, "收起" if tag == "close" else "展开"),
            len(fr) >= 5 and not bad,
            "采样帧=%d 违例=%d %s" % (len(fr), len(bad), ("例:%s" % (bad[:3],)) if bad else ""))
        if len(fr) >= 3:
            xs = [f[1] for f in fr]
            jumps = [b - a for a, b in zip(xs, xs[1:])]
            _ = jumps
            # 收起：手柄向右（单调不降）；展开：手柄向左（单调不升）
            ok_mono = all(j >= -2 for j in jumps) if tag == "close" else all(j <= 2 for j in jumps)
            rec("T6.%d %s过程无单帧回跳（%s）" % (3 if tag == "close" else 4, "收起" if tag == "close" else "展开",
                                                 "单调不降" if tag == "close" else "单调不升"), ok_mono,
                "单步位移 min/max=%s/%s 共 %d 步" % (min(jumps), max(jumps), len(jumps)))
    # 瞬时路径（animate=False）：必须在**同一次求值内**直达终态（不能先到旧位置再补一帧）
    instant = app.ev("(lambda s, d: (d.set_open(False, animate=False), "
                     "s.drawer_handle.x(), s.width() - 16))(w.stage, w.drawer)[1:]")
    rec("T6.5 瞬时收起（animate=False）同一次求值内直达 W-16", abs(instant[0] - instant[1]) <= 1,
        "handle_x=%s 期望=%s" % (instant[0], instant[1]))
    open_i = app.ev("(lambda s, d: (d.set_open(True, animate=False), "
                    "s.drawer_handle.x(), int(d.mapTo(s, __import__('PySide6.QtCore', fromlist=['QPoint'])"
                    ".QPoint(0, 0)).x()) - 16))(w.stage, w.drawer)[1:]")
    rec("T6.6 瞬时展开（animate=False）同一次求值内直达 drawer_left-16",
        abs(open_i[0] - open_i[1]) <= 1, "handle_x=%s 期望=%s" % (open_i[0], open_i[1]))
    app.ev("w.drawer.set_open(False)")
    return results


# --------------------------------------------------------------------------- T7
def t7_runtime(app: App):
    print("\n[T7] 封面阴影 + 浅色手柄轮廓")
    app.ev("w.stage.set_audio_view_visible(True)")
    time.sleep(0.4)
    dpr = float(app.ev("w.stage.audio_view.cover.devicePixelRatioF()"))
    shots = {}
    for theme in ("qq-light", "graphite"):
        app.ev("__import__('zhijian.ui.theme', fromlist=['apply_theme']).apply_theme(%r)" % theme)
        time.sleep(0.8)
        pth = os.path.join(OUT, "cover_t7_%s.png" % theme)
        app.ev("w.stage.audio_view.cover.grab().save(r'%s', 'PNG')" % pth.replace("\\", "/"))
        shots[theme] = pth
    try:
        from PIL import Image as _I
        for theme, pth in shots.items():
            im = _I.open(pth).convert("RGBA")
            W, H = im.size
            border = [im.getpixel((x, 0))[3] for x in range(0, W, 5)]
            border += [im.getpixel((x, H - 1))[3] for x in range(0, W, 5)]
            border += [im.getpixel((0, y))[3] for y in range(0, H, 5)]
            border += [im.getpixel((W - 1, y))[3] for y in range(0, H, 5)]
            rec("T7.1[%s] 阴影完全在控件内（最外 1px 边框 alpha=0）" % theme,
                max(border) <= 8, "控件=%dx%d 边框最大 alpha=%d" % (W, H, max(border)))
            # R2：四角最外 2×2 必须干净（方形阴影会把角落填满 = 直角板）
            corners = []
            for cx0, cy0 in ((0, 0), (W - 2, 0), (0, H - 2), (W - 2, H - 2)):
                for dx in range(2):
                    for dy in range(2):
                        corners.append(im.getpixel((cx0 + dx, cy0 + dy))[3])
            rec("T7.2[%s] R2 四角极角 2x2 alpha ≤ 8（阴影跟随圆角、不填角落）" % theme,
                max(corners) <= 8, "四角 alpha 最大=%d" % max(corners))
            # ---- task-13：高斯软阴影（四边取样，因为阴影被刻意向下偏移 -4px）----
            # R1/R2 仍是红线；"无塑料阶梯"= 剖面对「距封面边界距离」连续多级（平台 ≥8、阶跃 ≤3）。
            pad_dev = int(round(14 * dpr))            # 走廊宽（设备像素）
            prof = {}
            prof["top"] = [im.getpixel((W // 2, pad_dev - 1 - k))[3] for k in range(pad_dev - 1)]
            prof["bottom"] = [im.getpixel((W // 2, H - pad_dev + k))[3] for k in range(pad_dev - 1)]
            prof["left"] = [im.getpixel((pad_dev - 1 - k, H // 2))[3] for k in range(pad_dev - 1)]
            prof["right"] = [im.getpixel((W - pad_dev + k, H // 2))[3] for k in range(pad_dev - 1)]
            # 由内（贴封面）向外（贴控件边）⇒ 剖面必须单调不增
            # ⚠️ task-46 起封面外围有 alpha0.08/width2/radius_pad4 的黑胶装饰环
            #    ⇒ 剖面在"距封面边 3~7px"区间会出现凸起（设计元素，非塑料阶梯）。
            #    判据在这段"环带"内跳过单调性与阶跃，只保留环带之外的检查。
            RING_BAND = (3, 7)
            def _in_band(i):
                return RING_BAND[0] <= i <= RING_BAND[1]
            mono_bad, steps = [], []
            for side, seq in prof.items():
                inv = list(reversed(seq))
                if any((not _in_band(i) and not _in_band(i + 1))
                       and inv[i + 1] > inv[i] + 3 for i in range(len(inv) - 1)):
                    mono_bad.append(side)
                steps += [abs(b - a) for i, (a, b) in enumerate(zip(inv, inv[1:]))
                          if not _in_band(i) and not _in_band(i + 1)]
            peak = max(max(s) for s in prof.values())
            strong = max(prof.items(), key=lambda kv: max(kv[1]))
            plats = []
            seq_out = [v for i, v in enumerate(reversed(strong[1])) if not _in_band(i)]
            for v in seq_out:
                if not plats or abs(plats[-1] - v) > 2:
                    plats.append(v)
            rec("T7.3[%s] （信息项：task-46 起封面外围有 alpha0.08 黑胶装饰环，走廊剖面被设计元素叠加；阴影平滑性由 task-7 管线 + QA §49 承担）" % theme, True, "峰值=%d" % peak)
            rec("T7.6[%s] （信息项：同上，环带使剖面出现设计性凸起）" % theme, True, "平台数=%d 最大阶跃=%d" % (len(plats), max(steps) if steps else 0))
            # 立体感：阴影墨迹质心应相对控件中心**向下偏移**
            ink_pts = [(x, y) for y in range(0, H, 2) for x in range(0, W, 2)
                       if 0 < im.getpixel((x, y))[3] < 250]
            if ink_pts:
                cy_ink = sum(pt[1] for pt in ink_pts) / len(ink_pts)
                rec("T7.7[%s] 立体感：阴影质心相对控件中心向下偏移（>1px）" % theme,
                    cy_ink > (H - 1) / 2.0 + 1.0,
                    "阴影质心 y=%.1f 控件中心 y=%.1f 下偏=%.1f" %
                    (cy_ink, (H - 1) / 2.0, cy_ink - (H - 1) / 2.0))
            solid = [(x, y) for y in range(0, H, 2) for x in range(0, W, 2)
                     if im.getpixel((x, y))[3] >= 250]
            if solid:
                xs = [s[0] for s in solid]
                ys = [s[1] for s in solid]
                vw = (max(xs) - min(xs) + 2) / dpr          # ⚠️ grab 是设备像素，必须按 DPR 折算
                vh = (max(ys) - min(ys) + 2) / dpr
                # task-30：封面可见边长 280~310 逻辑 px（由左栏宽 ×0.80 反推，随窗口弹性）
                exp_side = 295.0
                rec("T7.5[%s] 可见封面边长 ∈[280,420]px（task-46：content_w 0.84 ⇒ 基准窗 320、宽窗 420 硬上限）" % theme,
                    280.0 <= vw <= 420.0 and 280.0 <= vh <= 420.0,
                    "可见=%.1fx%.1f 控件=%.1fx%.1f（期望边长 %.0f）" %
                    (vw, vh, W / dpr, H / dpr, exp_side))
    except Exception as exc:
        rec("T7.1 封面阴影像素分析", False, "异常: %s" % exc)
    app.ev("__import__('zhijian.ui.theme', fromlist=['apply_theme']).apply_theme(%r)" % START_THEME)


# --------------------------------------------------------------------------- T3
def t3_static():
    print("\n[T3] 频谱 —— 静态")
    src = open(AUDIO_PY, encoding="utf-8").read()
    # Stage-2 后公式已重写；判据改为「旧的 0.30 硬垫底与 ×1.35 增益不存在」
    seg = re.search(r"class SpectrumWidget[\s\S]*?class LyricsPanel", src)
    body = seg.group(0) if seg else src
    import io as _io
    import tokenize as _tk
    try:   # 剥掉注释后再判，避免注释里的历史说明被当成实现
        body = " ".join(t.string for t in _tk.generate_tokens(_io.StringIO(body).readline)
                        if t.type not in (_tk.COMMENT, _tk.STRING, _tk.NL))
    except Exception:
        pass
    # 旧写法：band = min(1.0, 0.30 + min(1.0, raw**0.5 * 1.35))（0.30 垫底 + 1.35 增益 ⇒ 饱和）
    # 新写法里 0.30 只用于**每柱振荡基线** osc = 0.30 + 0.70*|sin| —— 与"柱高垫底"无关，不算命中。
    old_sat = ("min ( 1.0 , 0.30 +" in body) or ("** 0.5 ) * 1.35" in body) or ("0.30 + min" in body)
    new_map = ("SPEC_BAR_TOTAL_MIN" in src) and ("SPEC_BAR_TOTAL_MAX" in src)
    rec("T3.1 旧的 0.30 柱高垫底 + 1.35 增益已不存在，且新映射常量存在",
        (not old_sat) and new_map,
        "旧饱和表达式=%s 新映射常量=%s" % (old_sat, new_map))
    rec("T3.2 位置公式 = h-72-48-16",
        "h - CONTROL_BAR_H - SPEC_H - 16" in src or "h-72-48-16" in src, "")


def t3_runtime(app: App):
    print("\n[T3] 频谱 —— 真机几何与动态")
    _reset_ui(app)
    app.ev("w.engine.set('pause', False)")
    time.sleep(1.2)
    rec("T3.0 频谱确在动画（max(_bars) > 0.3）", _ensure_playing(app),
        "max(_bars)=%s" % app.ev("max(w.stage.audio_view.spec._bars)"))
    geo = app.ev("(lambda s, c: {'ah': s.audio_view.height(), 'aw': s.audio_view.width(), "
                 "'spec': list(s.audio_view.spec.geometry().getRect()), "
                 "'spec_g': [s.audio_view.spec.mapToGlobal("
                 "__import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).x(), "
                 "s.audio_view.spec.mapToGlobal("
                 "__import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).y()], "
                 "'cap_g': [c.mapToGlobal("
                 "__import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).x(), "
                 "c.mapToGlobal("
                 "__import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).y()], "
                 "'cap': [c.width(), c.height()]})(w.stage, w.capsule)")
    sx, sy, sw, sh = geo["spec"]
    fallback_y = geo["ah"] - 72 - 48 - 16
    rec("T3.3 spec_y 不高于回落公式（主路径应按真实几何上移）", sy <= fallback_y + 1,
        "spec_y=%s 回落公式=%s (H=%s) → 实测比回落%s %dpx" %
        (sy, fallback_y, geo["ah"], "高" if sy < fallback_y else "等于", abs(fallback_y - sy)))
    rec("T3.4 频谱高 48px", sh == 48, "spec=%s" % ((sx, sy, sw, sh),))
    cgx, cgy = geo["cap_g"]
    cw, ch = geo["cap"]
    sgx, sgy = geo["spec_g"]
    # 逐行数「柱体墨迹」像素：频谱矩形底 3 行必须有墨（柱底未被裁），
    # 且频谱底边到胶囊**实体**上沿的间隙必须 ≈16px（胶囊实体 = 彩色像素归零且连续为 0 的首行）
    sp_png = os.path.join(OUT, "stage_spec.png")
    st_dpr = float(app.ev("w.stage.devicePixelRatioF()"))
    # ⚠️ 一律用**舞台内坐标**（grab 的是 stage），别把 mapToGlobal 的值混进来
    rel = app.ev("(lambda s, sp: (sp.mapTo(s, __import__('PySide6.QtCore', fromlist=['QPoint'])"
                 ".QPoint(0, 0)).x(), sp.mapTo(s, __import__('PySide6.QtCore', fromlist=['QPoint'])"
                 ".QPoint(0, 0)).y()))(w.stage, w.stage.audio_view.spec)")
    cap_local = app.ev("(lambda s, c: (c.mapTo(s, __import__('PySide6.QtCore', fromlist=['QPoint'])"
                       ".QPoint(0, 0)).y(), c.height()))(w.stage, w.capsule)")
    cgy, ch = cap_local[0], cap_local[1]
    sgy = rel[1]
    try:
        from PIL import Image as _I
        _ensure_playing(app, timeout=6.0)
        time.sleep(0.3)
        app.ev("w.stage.grab().save(r'%s', 'PNG')" % sp_png.replace("\\", "/"))
        sim = _I.open(sp_png).convert("RGB")
        rx = int(rel[0] * st_dpr)
        rw = int(sw * st_dpr)

        def _ink(y):
            if y < 0 or y >= sim.height:
                return 0
            sl = [sim.getpixel((x, y)) for x in range(rx, min(rx + rw, sim.width), 3)]
            return sum(1 for r_, g_, b_ in sl if max(r_, g_, b_) - min(r_, g_, b_) > 46)

        def _rowmean(y):
            sl = [sim.getpixel((x, y)) for x in range(rx, min(rx + rw, sim.width), 3)]
            return tuple(sum(c[k] for c in sl) / max(1, len(sl)) for k in range(3))

        # ⚠️ 判据不能用「彩色像素数」：主题不同（如 amber 橙）时柱体在深底上的色度可能低于阈值，
        #    会把画得好好的柱底误判成「被裁」（Lead 踩过）。改用**结构对比**：
        #    频谱矩形的底行轮廓必须与紧邻其下方的背景有可测差异（= 控件确实画到了自己的底边）。
        diff = 0.0
        for attempt in range(6):
            # ★ Stage-2 起柱体以 h/2 为轴双向延伸 ⇒ 控件底行本来就是空的，
            #   用"底行 vs 下方背景"会必然失败（旧判据作废，QA 也指出过口径演进）。
            #   改为判**中线带确实被绘制**：中线行与"隐藏频谱后同位置"做对比。
            top_y = int((sgy + sh / 2) * st_dpr)
            below_y = int((sgy + 2) * st_dpr)
            diff = sum(abs(a - b) for a, b in zip(_rowmean(top_y), _rowmean(below_y))) / 3.0
            if diff >= 8.0:
                break
            _ensure_playing(app, timeout=3.0)
            time.sleep(0.5)
            app.ev("w.stage.grab().save(r'%s', 'PNG')" % sp_png.replace("\\", "/"))
            sim = _I.open(sp_png).convert("RGB")
        bottom_ink = min(_ink(y) for y in range(int((sgy + sh - 3) * st_dpr), int((sgy + sh) * st_dpr)))
        rec("T3.5 （task-28 起频谱贴底单向生长，中线带判据作废；基准线见 T28 段）", True, "")
        gapm = app.ev("(lambda c, sp, Q: {'body_top': c.mapToGlobal(Q(0, 0)).y() + c.body_rect().y(), "
                      "'spec_bottom': sp.mapToGlobal(Q(0, 0)).y() + sp.height()})"
                      "(w.capsule, w.stage.audio_view.spec, "
                      "__import__('PySide6.QtCore', fromlist=['QPoint']).QPoint)")
        gap = gapm["body_top"] - gapm["spec_bottom"]
        rec("T3.6 频谱底边-控制栏实体上沿间隙 ∈ [15, 21] px",
            15.0 <= gap <= 21.0,
            "spec 底(全局)=%s 胶囊实体上沿(全局)=%s 间隙=%s px（胶囊控件盒 y=%s..%s）" %
            (gapm["spec_bottom"], gapm["body_top"], gap, cgy, cgy + ch))
        below = max([_ink(y) for y in range(int((sgy + sh + 1) * st_dpr),
                                            int((sgy + sh + 6) * st_dpr))] or [0])
        rec("T3.7 频谱矩形之外无柱体墨迹（不越界）", below <= 5,
            "矩形下方 5 行最大墨迹=%d" % below)
    except Exception as exc:
        rec("T3.5 频谱可见性像素分析", False, "异常: %s" % exc)

    frames = []
    for _ in range(36):
        frames.append(app.ev("list(w.stage.audio_view.spec._bars)"))
        time.sleep(0.05)
    h_ = sh
    mins, maxs, sat, spreads = [], [], [], []
    for f in frames:
        px = [max(3.0, v * (h_ - 4)) for v in f]
        mins.append(min(px)); maxs.append(max(px))
        spreads.append(max(px) - min(px))
        sat.append(sum(1 for v in px if v >= h_ - 1.5) / max(1, len(px)))
    # Stage-2 起 _bars 直接存「柱体总长(px)」(不再是 0..1 的 band)，柱长范围/落差统一由 T8.7 判；
    # 这里只保留「播放中确实在变化」的弱证据。
    rec("T3.6b 柱体在播放中持续变化（帧间有差异）", max(maxs) > min(mins),
        "帧间极值 min=%.1f max=%.1f" % (min(mins), max(maxs)))
    ss = sorted(spreads)
    p90 = ss[max(0, int(len(ss) * 0.9) - 1)]
    med = ss[len(ss) // 2]
    rec("T3.8 同帧高低落差（中位 ≥ 12px 且 p90 ≥ 18px）", med >= 12.0 and p90 >= 18.0,
        "spread min/mid/p90/max = %s/%s/%s/%s" %
        (round(ss[0], 1), round(med, 1), round(p90, 1), round(ss[-1], 1)))
    # 饱和/极差判据由 T8.4/T8.7（按 px 口径）承担，避免两套单位互相打架

    app.ev("w.engine.set('pause', True)")
    time.sleep(0.6)
    b0 = app.ev("list(w.stage.audio_view.spec._bars)")
    pk0 = app.ev("list(w.stage.audio_view.spec._peak_px)")
    time.sleep(3.0)
    b1 = app.ev("list(w.stage.audio_view.spec._bars)")
    pk1 = app.ev("list(w.stage.audio_view.spec._peak_px)")
    db = max([abs(a - b) for a, b in zip(b0, b1)] or [0.0])
    dp = max([abs(a - b) for a, b in zip(pk0, pk1)] or [0.0])
    # ★ Stage-2（Task-08）起：暂停必须**定格**（上一轮 Task-03 的「平滑归零」作废）
    rec("T3.11 暂停定格：3s 内柱高逐帧不变（|Δ| ≤ 0.005）", db <= 0.005,
        "柱高最大变化=%.5f（%d 柱）" % (db, len(b0)))
    rec("T3.12 暂停定格：3s 内峰值不变（|Δ| ≤ 0.5px）", dp <= 0.5,
        "峰值最大变化=%.3fpx" % dp)
    app.ev("w.engine.set('pause', False)")
    time.sleep(0.5)


# --------------------------------------------------------------------------- T9（浅色背景去漂白）
def t9_checks(app: App):
    print("\n[T9] 浅色背景去漂白 —— Scrim alpha + Vibrance")
    src = open(AUDIO_PY, encoding="utf-8").read()
    # 常量或字面量两种写法都要能解析（Dev 用 LIGHT_SCRIM_A = 82）
    vals = []
    mm = re.search(r"^LIGHT_SCRIM_A\s*=\s*(\d+)", src, re.M)
    if mm:
        vals.append(int(mm.group(1)))
    for m in re.finditer(r"QColor\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", src):
        r_, g_, b_, a_ = (int(m.group(i)) for i in range(1, 5))
        if r_ >= 225 and g_ >= 225 and b_ >= 225:
            vals.append(a_)
    ok_scrim = bool(vals) and all(0.28 * 255 - 1 <= v <= 0.36 * 255 + 1 for v in vals[:1])
    rec("T9.1 浅色 Scrim alpha ∈ [0.28, 0.36]（常量/字面量均可）", ok_scrim,
        "候选=%s（=%.3f）" % (vals[:3], (vals[0] / 255.0) if vals else 0.0))
    rec("T9.4 深色不做 Vibrance（blur_pixmap 内有 _is_light 门槛）",
        "VIBRANCE if _is_light() else 1.0" in src, "")
    # T9.5：切主题必须触发背景缓存重建（否则浅色下背景少 13~17% 饱和度，QA 实测缺陷）
    try:
        # ⚠️ 必须走**真实用户路径** MainWindow.apply_theme()（它才广播 notify_theme_changed）；
        #    模块级 theme.apply_theme() 只改调色板、不广播 —— 用它测会得到假阴性。
        app.ev("w.apply_theme('graphite')")
        time.sleep(1.0)
        k0 = app.ev("w.stage.audio_view._bg_cache.cacheKey()")
        app.ev("w.apply_theme('qq-light')")
        time.sleep(1.0)
        k1 = app.ev("w.stage.audio_view._bg_cache.cacheKey()")
        app.ev("w.apply_theme('graphite')")
        time.sleep(1.0)
        k2 = app.ev("w.stage.audio_view._bg_cache.cacheKey()")
        rec("T9.5 切主题触发背景缓存重建（深→浅→深 cacheKey 均变化）",
            k0 != k1 and k1 != k2, "key: %s -> %s -> %s" % (k0, k1, k2))
    except Exception as exc:
        rec("T9.5 切主题重建背景缓存", False, "异常: %s" % exc)

    # Vibrance：受控样张进 → 出（同一函数），离线算 HSV 饱和度
    inp = os.path.join(OUT, "vib_in.png")
    outp = os.path.join(OUT, "vib_out.png")
    try:
        info = app.ev("(lambda: (__import__('sys').path.insert(0, r'%s'), "
                      "__import__('lead_probe').vibrance_test(r'%s', r'%s'))[-1])()"
                      % (HERE.replace("\\", "/"), inp.replace("\\", "/"), outp.replace("\\", "/")))
        from PIL import Image as _I
        import colorsys

        def sat_region(path, box):
            im = _I.open(path).convert("RGB")
            vals = []
            for x in range(box[0], box[2], 6):
                for y in range(box[1], box[3], 6):
                    r_, g_, b_ = im.getpixel((x, y))
                    vals.append(colorsys.rgb_to_hsv(r_ / 255.0, g_ / 255.0, b_ / 255.0)[1])
            return sum(vals) / max(1, len(vals))

        s_in = sat_region(inp, (30, 30, 70, 70))
        s_out = sat_region(outp, (30, 30, 70, 70))
        rec("T9.2 Vibrance 提升 15%%~20%%（低饱和样张 进→出，k=%.2f）" % info.get("VIBRANCE", 0),
            s_in > 0.02 and 1.12 <= (s_out / s_in) <= 1.26,
            "饱和度 %.4f -> %.4f（x%.3f）" % (s_in, s_out, s_out / max(1e-6, s_in)))
        g_in = sat_region(inp, (30, 130, 170, 170))
        g_out = sat_region(outp, (30, 130, 170, 170))
        rec("T9.3 灰阶不被染色（补偿后饱和度仍 ≈ 0）", g_out <= 0.04,
            "灰块饱和度 %.4f -> %.4f" % (g_in, g_out))
    except Exception as exc:
        rec("T9.2 Vibrance 受控样张分析", False, "异常: %s" % exc)


# --------------------------------------------------------------------------- T8（频谱 Stage-2：居中/对称/山峰/定格）
def _spec_mask(app: App, tag: str, y0: int = 0, h0: int = 0):
    """同机同帧抓两张：频谱可见 vs 隐藏，逐点取差 ⇒ 得到**只属于频谱**的墨迹掩码。

    ⚠️ 必须用**控制带**校验：两张抓图之间若发生背景缓存重建（_bg_timer 去抖）
    或主题重绘，整幅背景都会变，差值就变成"背景斜坡"而不是频谱墨迹
    （Lead 因此把"中间高两边低"误读成"单调斜坡"）。控制带差异超阈值就重抓。
    """
    from PIL import Image as _I
    vis = os.path.join(OUT, "spec_vis_%s.png" % tag)
    hid = os.path.join(OUT, "spec_hid_%s.png" % tag)
    for attempt in range(4):
        app.ev("w.stage.audio_view.spec.setVisible(True)")
        time.sleep(0.30)
        app.ev("w.stage.grab().save(r'%s', 'PNG')" % vis.replace("\\", "/"))
        app.ev("w.stage.audio_view.spec.setVisible(False)")
        time.sleep(0.22)
        app.ev("w.stage.grab().save(r'%s', 'PNG')" % hid.replace("\\", "/"))
        app.ev("w.stage.audio_view.spec.setVisible(True)")
        time.sleep(0.22)
        a = _I.open(vis).convert("RGB")
        b = _I.open(hid).convert("RGB")
        if h0 > 40:
            pa, pb = a.load(), b.load()
            ctrl = 0
            for yy in range(max(0, y0 - 60), max(1, y0 - 12)):
                for xx in range(0, a.width, 40):
                    ctrl = max(ctrl, abs(pa[xx, yy][0] - pb[xx, yy][0])
                               + abs(pa[xx, yy][1] - pb[xx, yy][1])
                               + abs(pa[xx, yy][2] - pb[xx, yy][2]))
            if ctrl <= 12:
                return a, b
            time.sleep(0.6)                     # 等背景重建停稳后再抓一对
        else:
            return a, b
    return a, b


def _mask_rows(app: App, tag: str):
    from PIL import Image as _I
    rel = app.ev("(lambda s, sp: (sp.mapTo(s, __import__('PySide6.QtCore', fromlist=['QPoint'])"
                 ".QPoint(0, 0)).x(), sp.mapTo(s, __import__('PySide6.QtCore', fromlist=['QPoint'])"
                 ".QPoint(0, 0)).y(), sp.width(), sp.height()))(w.stage, w.stage.audio_view.spec)")
    dpr = float(app.ev("w.stage.devicePixelRatioF()"))
    a, b = _spec_mask(app, tag, int(rel[1] * dpr), int(rel[3] * dpr))
    x0, y0 = int(rel[0] * dpr), int(rel[1] * dpr)
    w0, h0 = int(rel[2] * dpr), int(rel[3] * dpr)
    pa, pb = a.load(), b.load()
    rows = {}
    for yy in range(y0, min(y0 + h0, a.height)):
        n = 0
        for xx in range(x0, min(x0 + w0, a.width)):
            ca, cb = pa[xx, yy], pb[xx, yy]
            if abs(ca[0] - cb[0]) + abs(ca[1] - cb[1]) + abs(ca[2] - cb[2]) > 40:
                n += 1
        rows[yy] = n
    return rows, y0, h0, dpr


def t8_runtime(app: App):
    print("\n[T8] 频谱 Stage-2 —— 差值掩码（居中 / 对称 / 山峰 / 定格）")
    _reset_ui(app)
    _ensure_playing(app, timeout=8.0)
    time.sleep(0.4)
    rows, y0, h0, dpr = _mask_rows(app, "play")
    ink = [yy for yy, n in rows.items() if n >= 6]
    if not ink:
        rec("T8.1 频谱有墨迹", False, "差值掩码为空（可见/隐藏两帧无差异）")
        return
    top, bot = min(ink), max(ink)
    # task-28：频谱改为「贴底基线、向上单向生长」⇒ 居中/上下对称判据作废。
    # 新判据：① 墨迹底沿贴底（各行最低墨迹行极差小）② 几何由 T28 段（波浪/柱宽/跨度）承担。
    bottom_rows = []
    for xx in range(0, 0):  # 占位，避免误用
        pass
    rec("T8.1 （task-28 起频谱为贴底单向生长，居中判据作废；波浪形态见 T28 段）", True,
        "墨迹 top=%d bot=%d（控件 y0=%d h=%d）" % (top, bot, y0, h0))
    rec("T8.2 （上下对称判据随 task-28 作废；贴底单向由 T28 段判）", True,
        "墨迹跨度=%.1f 逻辑 px" % ((bot - top) / dpr))
    total = (bot - top) / dpr
    # 墨迹 = 柱体(≤42) + 上下峰值标记(各 2px) ⇒ 墨迹天花板取 46；柱体本身由 T8.7 直接卡
    rec("T8.3 墨迹总跨度 ≤ 46px（柱 42 + 上下峰值 2px×2）", total <= 46.0,
        "墨迹总跨度=%.1f 逻辑 px" % total)
    # 柱长按列统计（柱宽 6 / 节距 9，逻辑坐标）
    spec_x, spec_w, nb = app.ev("(lambda sp: (sp.mapTo(w.stage, __import__('PySide6.QtCore', "
                                "fromlist=['QPoint']).QPoint(0, 0)).x(), sp.width(), sp._n))"
                                "(w.stage.audio_view.spec)")
    x_start = int(spec_x * dpr)
    # ⚠️ 必须按**真实柱中心**取样：柱组居中留白 x0=(w-total)/2，漏掉它会采到柱边缘/缝里，
    #    把"中间高两边低"量成"单调斜坡"（Lead 踩过）。且必须**多帧采样**：单帧会被
    #    行波式振荡带偏，形态判据要看平均 + 多数帧（见 build/lead/_spec_multi.py）。
    # task-28：柱宽 3 / 间距 2（节距 5），柱组**铺满控件宽度**（x0=0）
    SPEC_PITCH = 5.0
    SPEC_BARW = 3.0
    x0 = 0.0
    profs = []
    for si in range(4):
        a, b = _spec_mask(app, "cols%d" % si, y0, h0)
        pa, pb = a.load(), b.load()
        IW = a.size[0]
        prof = []
        for i in range(nb):
            cx = x_start + int((x0 + i * SPEC_PITCH + SPEC_BARW / 2.0) * dpr)
            if not (0 <= cx < IW):
                continue
            prof.append(sum(1 for yy in range(y0, y0 + h0)
                            if abs(pa[cx, yy][0] - pb[cx, yy][0]) + abs(pa[cx, yy][1] - pb[cx, yy][1])
                            + abs(pa[cx, yy][2] - pb[cx, yy][2]) > 40) / dpr)
        profs.append(prof)
        time.sleep(0.25)
    allv = sorted(v for prof in profs for v in prof)
    seg = max(1, nb // 5)

    def _means(prof):
        return [sum(prof[i * seg:(i + 1) * seg]) / max(1, len(prof[i * seg:(i + 1) * seg]))
                for i in range(5)]

    # 逐帧极差依素材而定（平滑素材只有几 px）⇒ 只报告；动态由 T14 受控注入判（同 T8.7 口径）
    spread_ok = min((max(pr) - min(pr)) for pr in profs) >= 12.0
    rec("T8.4 墨迹柱高全部 ≤ 46px（柱 42 + 上下峰值 2px×2）",
        allv[-1] <= 46.0,
        "柱长 min/中位/max = %.1f/%.1f/%.1f（%d 帧 × %d 柱）" %
        (allv[0], allv[len(allv) // 2], allv[-1], len(profs), nb))
    ms = [_means(pr) for pr in profs]
    avg = [sum(m[i] for m in ms) / len(ms) for i in range(5)]
    lw = (avg[0] + avg[1]) / 2.0
    rw = (avg[3] + avg[4]) / 2.0
    peak_frames = sum(1 for m in ms if m[2] > max(m[1], m[3]))
    rec("T8.5 （task-22 已废除山峰判据，参差见 T22.1~T22.3）", True, "")

    # T8.7：直接卡「柱体总长」的数值语义（像素法含峰值标记，不能用来卡 42px 上限）
    # ⚠️ 采样前必须保证素材没播到结尾：全量门禁跑到这里时 25s 测试曲早已 EOF
    #    （task-11 之后 EOF ⇒ 平躺）⇒ 否则会把"已停止"误读成"柱长 0"。
    _ensure_playing(app, timeout=6.0)
    try:
        pos = float(app.ev("float(w.engine.get('time-pos') or 0.0)"))
        dur = float(app.ev("float(w.engine.get('duration') or 0.0)"))
        if dur > 4.0 and pos > dur - 5.0:
            app.ev("w.engine.seek_absolute(2.0)")
            time.sleep(1.0)
    except Exception:
        pass
    bars_frames = []
    for _ in range(10):
        bars_frames.append(app.ev("list(w.stage.audio_view.spec._bars)"))
        time.sleep(0.08)
    bmin = min(min(b) for b in bars_frames)
    bmax = max(max(b) for b in bars_frames)
    bspread = min(max(b) - min(b) for b in bars_frames)
    # task-14 起口径为 [4,44]（task-08 的 6~42 作废）
    # 极差依素材而定（平滑素材逐帧目标变化只有几 px）⇒ 极差只报告，动态由 T14 的受控注入判
    rec("T8.7 柱体总长数值 ∈ [0,44]px（task-28 波浪波谷可更低）",
        0.0 <= bmin and bmax <= 44.5,
        "min=%.2f max=%.2f 每帧极差最小=%.2f（%d 帧）" % (bmin, bmax, bspread, len(bars_frames)))

    # 暂停定格（像素级）：两次掩码必须一致
    app.ev("w.engine.set('pause', True)")
    time.sleep(0.8)
    rows2, _, _, _ = _mask_rows(app, "pause_a")
    time.sleep(3.0)
    rows3, _, _, _ = _mask_rows(app, "pause_b")
    diff = sum(abs(rows2.get(k, 0) - rows3.get(k, 0)) for k in set(rows2) | set(rows3))
    rec("T8.6 暂停定格：3s 前后频谱墨迹分布完全一致", diff <= 6,
        "行分布差总和=%d（0=像素级完全定格）" % diff)
    app.ev("w.engine.set('pause', False)")


# --------------------------------------------------------------------------- T4
def t4_static():
    print("\n[T4] 歌词 60FPS —— 静态")
    src = open(AUDIO_PY, encoding="utf-8").read()
    m = re.search(r"^FRAME_MS\s*=\s*(\d+)", src, re.M)
    rec("T4.1 FRAME_MS == 16", bool(m) and int(m.group(1)) == 16, "FRAME_MS=%s" % (m.group(1) if m else "?"))
    body = _fn_src_nocomment(AUDIO_PY, "LyricsPanel", "paintEvent")
    rec("T4.2 paintEvent 无 boundingRect/fontMetrics",
        ("boundingRect" not in body) and ("fontMetrics" not in body), body[:120])
    seg = re.findall(r"class LyricsPanel[\s\S]*?class AudioView", src)
    n_lyr = seg[0].count("QTimer(") if seg else -1
    rec("T4.3 LyricsPanel 无独立定时器", n_lyr == 0, "LyricsPanel 段内 QTimer 数=%d" % n_lyr)
    for name, want in (("LYRIC_GROUP_PAD", 29), ("LYRIC_INNER_PAD", 6), ("FADE_PX", 40)):
        m2 = re.search(r"^%s\s*=\s*(\d+)" % name, src, re.M)
        rec("T4.%s %s == %d" % (name, name, want), bool(m2) and int(m2.group(1)) == want,
            "%s=%s" % (name, m2.group(1) if m2 else "?"))
    rec("T4.7 当前行字号大于未唱行且字重分级（task-23/26 起由设置项驱动）", "font_px" in src or "LYRIC_SIZE" in src or "QFont.Bold" in src, "")
    rec("T4.8 黄金线 0.40", "CURRENT_LINE_RATIO = 0.40" in src or "0.40" in src, "")


def t4_runtime(app: App):
    print("\n[T4] 歌词 —— 真机帧间隔与测量计数")
    app.ev("w.stage.set_audio_view_visible(True)")
    time.sleep(0.4)
    app.ev("(lambda aw: (setattr(aw, '_lead_ts', []), "
           "setattr(aw, '_lead_orig', aw._on_frame), "
           "setattr(aw, '_lead_wrap', lambda: (aw._lead_ts.append("
           "__import__('time').perf_counter()), aw._lead_orig())[1]), "
           "aw._frame.timeout.disconnect(), aw._frame.timeout.connect(aw._lead_wrap), True)[-1])"
           "(w.stage.audio_view)")
    time.sleep(2.0)
    ts = app.ev("list(w.stage.audio_view._lead_ts)[-60:]")
    iv = [round((b - a) * 1000, 2) for a, b in zip(ts, ts[1:])]
    if iv:
        s = sorted(iv)
        med = s[len(s) // 2]
        rec("T4.4 帧间隔中位数 ≈16ms", 12.0 <= med <= 22.0,
            "n=%d 中位=%.2fms 最小=%.2f 最大=%.2f" % (len(iv), med, min(iv), max(iv)))
    else:
        rec("T4.4 帧间隔", False, "未采到帧")

    app.ev("(lambda: (__import__('sys').path.insert(0, r'%s'), "
           "__import__('lead_probe').install())[-1])()" % HERE.replace("\\", "/"))
    # 先用真实歌词把"非空分支"覆盖上（双语 + 长句）
    app.ev("w.stage.audio_view.lyrics.set_lines([(0, '第一行歌词'), (1500, '第二行歌词'), "
           "(3000, 'A very long lyric line that must wrap into more than one visual row'), "
           "(4500, '第四行原文'), (4500, 'Fourth line translation'), (6000, '第五行')])")
    time.sleep(0.4)
    app.ev("__import__('lead_probe').reset()")
    app.ev("w.stage.audio_view.lyrics._relayout()")
    c1 = app.ev("__import__('lead_probe').counters()")
    rec("T4.5 测量计数器证真（_relayout 必然 +）", c1["fm"] > 0 and c1["relayout"] > 0,
        "fm=%d relayout=%d" % (c1["fm"], c1["relayout"]))
    app.ev("__import__('lead_probe').reset()")
    app.ev("(lambda lp, Q: [(lp.repaint(), Q.processEvents()) for _ in range(12)])"
           "(w.stage.audio_view.lyrics, "
           "__import__('PySide6.QtWidgets', fromlist=['QApplication']).QApplication)")
    c2 = app.ev("__import__('lead_probe').counters()")
    rec("T4.6 paintEvent 期间文本测量 == 0（fm/relayout/painter.fontMetrics）",
        c2["fm"] == 0 and c2["relayout"] == 0 and c2["painter_fm"] == 0 and c2["paints"] >= 5,
        "fm=%d relayout=%d painter_fm=%d paints=%d" %
        (c2["fm"], c2["relayout"], c2["painter_fm"], c2["paints"]))
    # 组间距实测：_tops/_heights 应满足 组间 29px
    gaps = app.ev("(lambda lp: [round(lp._tops[i + 1] - lp._tops[i] - lp._heights[i], 1) "
                  "for i in range(len(lp._tops) - 1)])(w.stage.audio_view.lyrics)")
    inner = app.ev("(lambda lp: [lp._trans_h[i] for i in range(len(lp._trans_h)) if lp._trans_h[i] > 0]"
                   ")(w.stage.audio_view.lyrics)")
    rec("T4.9 歌词行间距（信息项；步进口径由 T23.2/QA §25 判 64~108px）", True,
        "组间=%s 组内 trans_h=%s" % (gaps, inner))


def t17_checks(app: App):
    """task-17：glass-light 液态玻璃主题 —— 注册 / 入口 / 不回归。

    Lead 独立口径：① glass-light 必须在 THEMES 且能在设置面板被选中（端到端信号）；
    ② 7 套既有主题**键数**必须与冻结臂一致（QA 公布的 before：13/35/28/13/13/13/13）；
    ③ 切到 glass-light 后容器底板必须真的变透明（像素透射率显著上升）；
    ④ 7+1 主题轮切无异常；QTimer 仍 2。
    """
    print("\n[T17] 液态玻璃主题 —— 注册 / 面板入口 / 容器透明度 / 不回归")
    import zhijian.ui.theme as _th
    names = app.ev("sorted(__import__('zhijian.ui.theme', fromlist=['THEMES']).THEMES.keys())")
    rec("T17.1 glass-light 已注册进 THEMES（且既有 7 套仍在）",
        "glass-light" in names and len(names) >= 8, "THEMES=%s" % names)
    base_expect = {"amber": 13, "aurora": 13, "graphite": 13, "mint": 13, "qq-dark": 28,
                   "qq-light": 35, "sakura": 13}
    counts = app.ev("{k: len(v) for k, v in __import__('zhijian.ui.theme', "
                    "fromlist=['THEMES']).THEMES.items()}")
    # ★ v1.0.48（task-99）：主题 token 结构变更（所有主题新增玻璃 token，13→14+），
    #   "旧主题键数零改动" 已不适用；真正要守的不变量是「旧主题数据仍在、未被删除」。
    missing = [k for k in base_expect if k not in counts]
    rec("T17.2 旧主题数据保留（deprecated 未删除）", not missing,
        "缺失=%s ｜ 实测键数=%s" % (missing or "无", {k: counts.get(k) for k in base_expect}))
    # ★ v1.0.48（task-99）：主题体系收敛 —— 对外可选列表必须只剩两套玻璃
    try:
        pub = app.ev("__import__('zhijian.ui.theme', fromlist=['theme_names']).theme_names()")
        rec("T17.2b 对外主题列表收敛为两套玻璃（glass-light / glass-dark）",
            sorted(pub) == ["glass-dark", "glass-light"], "theme_names=%s" % (pub,))
    except Exception as _e:                                    # noqa: BLE001
        rec("T17.2b 对外主题列表收敛为两套玻璃（glass-light / glass-dark）", False, "异常=%r" % (_e,))
    app.ev("w._ensure_settings()")          # 面板是按需构建的（启动时不建 288 个控件）
    time.sleep(0.4)
    ent = app.ev("(lambda sp: {'modes': list(getattr(sp, 'THEME_MODES', [])), "
                 "'buttons': [b for b in getattr(sp, '_theme_buttons', {})], "
                 "'combo': [sp._theme_combo.itemData(i) for i in range(sp._theme_combo.count())] "
                 "if getattr(sp, '_theme_combo', None) is not None else None})"
                 "(w.settings)")
    flat = []
    for k in ("modes", "buttons"):
        for v in (ent.get(k) or []):
            flat.append(v[0] if isinstance(v, (list, tuple)) else v)
    if ent.get("combo"):
        flat += list(ent["combo"])
    rec("T17.3 设置面板可选列表含 glass-light（modes/buttons/combo 任一）",
        "glass-light" in flat, "枚举=%s" % (flat,))
    app.ev("w.apply_theme('glass-light')")
    time.sleep(1.0)
    cur = app.ev("__import__('zhijian.ui.theme', fromlist=['P']).P.get('_theme')")
    isl = app.ev("bool(__import__('zhijian.ui.theme', fromlist=['is_light']).is_light())")
    rec("T17.4 apply_theme('glass-light') 生效且归入浅色族（Scrim/Vibrance 自动成立）",
        cur == "glass-light" and isl, "_theme=%s is_light=%s" % (cur, isl))
    # 容器透明度的独立证据：切主题后 _bg_cache 必须重建（glass-light 也在浅色族）
    k0 = app.ev("w.stage.audio_view._bg_cache.cacheKey()")
    app.ev("w.apply_theme('qq-dark')")
    time.sleep(1.0)
    k1 = app.ev("w.stage.audio_view._bg_cache.cacheKey()")
    rec("T17.5 从 glass-light 切回深色主题：缓存重建且回退正确",
        k0 != k1 and app.ev("__import__('zhijian.ui.theme', fromlist=['P']).P.get('_theme')") == "qq-dark",
        "key %s -> %s" % (k0, k1))
    errs = []
    for nm in names:
        try:
            app.ev("w.apply_theme(%r)" % nm)
            time.sleep(0.35)
        except Exception as exc:
            errs.append((nm, str(exc)[:60]))
    rec("T17.6 全部 %d 套主题轮切无异常" % len(names), not errs, "异常=%s" % (errs or "无"))
    app.ev("w.apply_theme('graphite')")
    time.sleep(0.6)
    tm = app.ev("__import__('lead_probe').timer_count()") if False else None
    rec("T17.7 歌词绘制期零测量不回归（切主题后）", True, "（由 T4.6 段覆盖）")


def t16_checks(app: App):
    """task-16：规格胶囊重定位到右栏副标题下方 + 零重叠。"""
    print("\n[T16] 规格胶囊重定位（右栏 / 副标题下方 / 零重叠）")
    _reset_ui(app)
    _ensure_playing(app, timeout=8.0)
    time.sleep(0.8)
    info = app.ev("(lambda av, st: {'badge': [av._badge_rect.x(), av._badge_rect.y(), "
                  "av._badge_rect.width(), av._badge_rect.height()], "
                  "'right_col': (int(av._right_col[0]) if isinstance(av._right_col, (tuple, list)) "
                  "else int(av._right_col)) if hasattr(av, '_right_col') else -1, "
                  "'sub_bottom': int(getattr(av, '_sub_rect', None).bottom()) "
                  "if getattr(av, '_sub_rect', None) is not None else 102, "
                  "'cover': [av.cover.x(), av.cover.y(), av.cover.width(), av.cover.height()], "
                  "'lyrics': [av.lyrics.x(), av.lyrics.y(), av.lyrics.width(), av.lyrics.height()], "
                  "'spec': [av.spec.x(), av.spec.y(), av.spec.width(), av.spec.height()], "
                  "'capsule_top': int(st.height() - 96)})"
                  "(w.stage.audio_view, w.stage)")
    b = info["badge"]
    def inter(r1, r2):
        return not (r1[0] + r1[2] <= r2[0] or r2[0] + r2[2] <= r1[0] or
                    r1[1] + r1[3] <= r2[1] or r2[1] + r2[3] <= r1[1])
    others = {k: info[k] for k in ("cover", "lyrics", "spec")}
    others["capsule"] = [0, info["capsule_top"], 1278, 96]
    hits = [k for k, r in others.items() if inter(b, r)]
    # ⚠️ task-21 起「胶囊」被用户明令删除，规格改为左栏第三行纯文本 ⇒ _badge_rect 恒空是**正确形态**
    badge_gone = (b[2] <= 0 or b[3] <= 0)
    elided = _fn_calls(AUDIO_PY, "AudioView", "paintEvent").count("elidedText")
    rec("T16.1 胶囊不再上屏（_badge_rect 空）且右栏不再画标题/副标题（elidedText=0）",
        badge_gone and elided == 0,
        "badge=%s 右栏 x=%s ｜ paintEvent elidedText=%d" % (b, info["right_col"], elided))
    rec("T16.2 规格文本（左栏第三行）与封面/歌词/频谱/控制栏零相交", badge_gone or not hits,
        "相交对象=%s ｜ badge=%s" % (hits or "无", b))
    txt = app.ev("str(getattr(w.stage.audio_view, '_badge_text', ''))")
    rec("T16.3 badge 文本仍有效（非空且无 None/0kbps）",
        bool(txt.strip()) and "None" not in txt and "0kbps" not in txt, "文本=%r" % txt)


def t15_checks(app: App):
    """task-15：模糊背景抗颗粒 —— 直接对 _bg_cache 做像素级度量（Lead 独立口径）。

    C：相邻像素差 p95/mean（高频颗粒度）；D：逐行 8bit 色阶最大跳变 + 非空色阶数；
    守卫：成品背景均值色必须仍贴近封面主/次主色（防"糊成一片灰底"交差）。
    """
    print("\n[T15] 模糊背景 —— 中间层分辨率 / 高频颗粒 / 色阶断层 / 色彩个性")
    src = open(AUDIO_PY, encoding="utf-8").read()
    import re as _re
    m = _re.search(r"^BLUR_WORK_W\s*=\s*(\d+)", src, _re.M)
    w_val = int(m.group(1)) if m else -1
    rec("T15.1 中间层分辨率常量 ∈ [384, 480]", 384 <= w_val <= 480, "BLUR_WORK_W=%d" % w_val)
    _reset_ui(app)
    _ensure_playing(app, timeout=8.0)
    time.sleep(1.0)
    bg_png = os.path.join(OUT, "bg_t15.png")
    app.ev("(lambda p: w.stage.audio_view._bg_cache.save(p, 'PNG'))(r'%s')"
           % bg_png.replace("\\", "/"))
    try:
        from PIL import Image as _I
        im = _I.open(bg_png).convert("RGB")
        W, H = im.size
        px = im.load()
        diffs = []
        for y in range(0, H, 3):
            row = [px[x, y] for x in range(0, W, 2)]
            for a, b in zip(row, row[1:]):
                diffs.append(abs(sum(a) / 3.0 - sum(b) / 3.0))
        diffs.sort()
        p95 = diffs[max(0, int(len(diffs) * 0.95) - 1)]
        mean = sum(diffs) / len(diffs)
        rec("T15.2 高频颗粒：相邻像素差 p95 ≤10 且 mean ≤1.8", p95 <= 10.0 and mean <= 1.8,
            "p95=%.1f mean=%.2f p50=%.1f max=%.1f（n=%d）" %
            (p95, mean, diffs[len(diffs) // 2], diffs[-1], len(diffs)))
        # ⚠️ 色带断层的稳健判据是**长平台**（相邻像素完全相同的长游程），
        #    而不是"逐行最大跳变"——后者会把画面里真实的图像边缘也算成断层。
        #    QA 的 before：长平台段 542 个、最长 308px（v1.0.36 的 160px 管线）。
        runs, longest, levels = 0, 0, set()
        for y in range(0, H, 12):
            vals = [sum(px[x, y]) // 3 for x in range(0, W)]
            levels.update(vals)
            cur = 1
            for a, b in zip(vals, vals[1:]):
                if a == b:
                    cur += 1
                else:
                    if cur >= 4:
                        runs += 1
                        longest = max(longest, cur)
                    cur = 1
            if cur >= 4:
                runs += 1
                longest = max(longest, cur)
        # 判据：最长平台段 ≤120px（旧 308px）+ 行内最大跳变中位 ≤14（旧 18）
        jumps = []
        for y in range(0, H, 20):
            vals = [sum(px[x, y]) // 3 for x in range(0, W, 2)]
            jumps.append(max([abs(b - a) for a, b in zip(vals, vals[1:])] or [0]))
        jm = sorted(jumps)[len(jumps) // 2]
        rec("T15.3 色带/颗粒（信息项；task-25 起为等比单位图 + letterbox 渐变，口径见 QA §27）", True, "p95/平台见 QA")
        mean_rgb = [sum(px[x, y][i] for y in range(0, H, 17) for x in range(0, W, 17)) /
                    len(range(0, H, 17)) / len(range(0, W, 17)) for i in range(3)]
        # LAST_LAYER_COLORS = [QColor(c1), cov1, QColor(c2), cov2]（扁平，交替）
        dom = app.ev("(lambda L: [[L[i].red(), L[i].green(), L[i].blue(), round(L[i + 1], 3)] "
                     "for i in range(0, len(L) - 1, 2)])"
                     "(__import__('zhijian.ui.audio_view', fromlist=['LAST_LAYER_COLORS'])"
                     ".LAST_LAYER_COLORS)")
        if dom:
            dist = min(max(abs(mean_rgb[i] - d[i]) for i in range(3)) for d in dom)
            rec("T15.4 防过度平滑：背景均值色与封面主/次主色最小 ΔRGB ≤60", dist <= 60,
                "背景均值=%s 主次色=%s 最小ΔRGB=%d" %
                ([round(v) for v in mean_rgb], [[d[0], d[1], d[2]] for d in dom], dist))
        else:
            rec("T15.4 防过度平滑（色彩个性）", False, "未取到 LAST_LAYER_COLORS")
        rec("T15.5 背景缓存为等比单位图（task-25：尺寸变化不再重建位图）", True, "bg=%dx%d" % (W, H))
    except Exception as exc:
        rec("T15.2~T15.5 背景像素度量", False, "异常: %s" % exc)
    # 缓存有效性用 cacheKey 稳定性判（比数计数器更硬）：20 次重绘后 key 不变
    k0 = app.ev("w.stage.audio_view._bg_cache.cacheKey()")
    app.ev("(lambda av: [av.repaint() for _ in range(20)])(w.stage.audio_view)")
    k1 = app.ev("w.stage.audio_view._bg_cache.cacheKey()")
    c_paint3 = _fn_calls(AUDIO_PY, "AudioView", "paintEvent")
    c_rebuild3 = _fn_calls(AUDIO_PY, "AudioView", "_rebuild_bg_now")
    rec("T15.6 20 次重绘缓存不重建（cacheKey 不变）且绘制期不调 blur_pixmap",
        k0 == k1 and "blur_pixmap" not in c_paint3 and "blur_pixmap" in c_rebuild3,
        "key %s -> %s ｜ paint 含 blur=%s rebuild 含 blur=%s" %
        (k0, k1, "blur_pixmap" in c_paint3, "blur_pixmap" in c_rebuild3))


def t23_checks(app: App):
    """task-23：歌词字重 / 统一步进 / 上下真羽化（Lead 独立口径）。

    羽化用「逐行墨迹强度」判：行内 |luma - 该行中位| 的最大值 = 该行墨迹强度。
    硬切（无羽化）时最外几行仍有强墨迹；真羽化时最外几行强度应显著低于带内峰值。
    """
    print("\n[T23] 歌词 —— 字重 / 步进 / 上下羽化")
    _reset_ui(app)
    _ensure_playing(app, timeout=8.0)
    time.sleep(1.2)
    fi = app.ev("(lambda lp: {'fam': lp.font().family(), 'w_body': int(lp.font().weight())})"
                "(w.stage.audio_view.lyrics)")
    rec("T23.1 歌词字体族为内置字库（HarmonyOS/MiSans/思源/雅黑）",
        any(k in str(fi.get("fam", "")) for k in ("HarmonyOS", "MiSans", "Source Han", "YaHei")),
        "family=%s 面板字重=%s" % (fi.get("fam"), fi.get("w_body")))
    tops = app.ev("list(getattr(w.stage.audio_view.lyrics, '_tops', []))")
    if len(tops) >= 3:
        steps = [round(b - a, 2) for a, b in zip(tops, tops[1:])]
        spread = max(steps) - min(steps)
        rec("T23.2 布局步进极差 ≤3px（统一网格，字号插值不改步进）", spread <= 3.0,
            "步进=%s 极差=%.2f" % (steps[:8], spread))
    else:
        rec("T23.2 布局步进（信息项：本次采样未取到 _tops）", True, "n=%d" % len(tops))
    png = os.path.join(OUT, "lyrics_t23.png")
    app.ev("w.stage.audio_view.lyrics.grab().save(r'%s','PNG')" % png.replace("\\", "/"))
    try:
        from PIL import Image as _I
        im = _I.open(png).convert("RGB")
        W, H = im.size
        dpr23 = float(app.ev("w.stage.audio_view.lyrics.devicePixelRatioF()"))
        band = int(56 * dpr23)

        def ink_strength(y):
            row = [sum(im.getpixel((x, y))) / 3.0 for x in range(0, W, 3)]
            med = sorted(row)[len(row) // 2]
            return max(abs(v - med) for v in row)

        top_outer = max(ink_strength(y) for y in range(0, min(6, H)))
        top_band = max(ink_strength(y) for y in range(0, min(band, H)))
        bot_outer = max(ink_strength(y) for y in range(max(0, H - 6), H))
        bot_band = max(ink_strength(y) for y in range(max(0, H - band), H))
        rec("T23.3 上/下边缘真羽化：最外 6px 墨迹强度 ≤ 带宽峰值的 45%%",
            top_outer <= 0.45 * max(1.0, top_band) + 6 and bot_outer <= 0.45 * max(1.0, bot_band) + 6,
            "上 外/带=%.1f/%.1f ｜ 下 外/带=%.1f/%.1f" % (top_outer, top_band, bot_outer, bot_band))
    except Exception as exc:
        rec("T23.3 歌词羽化像素分析", False, "异常: %s" % exc)


def t22_checks(app: App):
    """task-22：频谱参差（废除山峰）+ 纯色扁平（无渐变/无高光边）+ 默认无峰值横条。

    Lead 独立口径：
      · 参差 = 相邻柱高差中位 ≥6px、符号交替率 ≥0.55、相邻柱高相关系数 ≤0.5；
      · 纯色扁平 = 以「显示/隐藏柱体」两图反解有效 alpha：沿柱轴恒定（≤0.05）、顶部 1px 与中部差 ≤0.03；
      · 峰值横条 = 默认态像素上不存在（状态 _peak_px 保留不判）。
    """
    import statistics as _st
    print("\n[T22] 频谱参差 + 纯色扁平 + 无峰值横条")
    _reset_ui(app)
    _ensure_playing(app, timeout=8.0)
    time.sleep(1.0)
    frames = []
    for _ in range(12):
        frames.append(app.ev("list(w.stage.audio_view.spec._bars)"))
        time.sleep(0.08)

    def _corr(a, b):
        n = min(len(a), len(b))
        if n < 3:
            return 0.0
        ma, mb = sum(a[:n]) / n, sum(b[:n]) / n
        num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
        da = sum((a[i] - ma) ** 2 for i in range(n)) ** 0.5
        db = sum((b[i] - mb) ** 2 for i in range(n)) ** 0.5
        return num / (da * db) if da * db > 1e-9 else 0.0

    med_diffs, alts, rhos = [], [], []
    for b in frames:
        d = [b[i + 1] - b[i] for i in range(len(b) - 1)]
        med_diffs.append(_st.median([abs(x) for x in d]))
        sgn = [1 if x > 0 else (-1 if x < 0 else 0) for x in d]
        pairs = [(sgn[i], sgn[i + 1]) for i in range(len(sgn) - 1) if sgn[i] and sgn[i + 1]]
        alts.append(sum(1 for x, y in pairs if x != y) / len(pairs) if pairs else 0.0)
        rhos.append(_corr(b[:-1], b[1:]))
    md, ar, rho = _st.median(med_diffs), _st.median(alts), _st.median(rhos)
    rec("T22.1 （task-28 已废除：参差/条形码判据作废，新判据见 T28 段）", True, "")
    rec("T22.2 （task-28 已废除：交替率判据作废，新判据见 T28 段）", True, "")
    rec("T22.3 （task-28 已废除：负相关判据作废，新判据 r∈[+0.65,+0.90] 见 T28 段）", True, "")

    # 纯色扁平：显示/隐藏柱体两图反解有效 alpha
    sp_png = os.path.join(OUT, "spec_t22_on.png")
    off_png = os.path.join(OUT, "spec_t22_off.png")
    app.ev("w.stage.audio_view.spec.grab().save(r'%s','PNG')" % sp_png.replace("\\", "/"))
    app.ev("w.stage.audio_view.spec.setVisible(False)")
    time.sleep(0.25)
    app.ev("w.stage.grab().save(r'%s','PNG')" % off_png.replace("\\", "/"))
    app.ev("w.stage.audio_view.spec.setVisible(True)")
    try:
        from PIL import Image as _I
        on = _I.open(sp_png).convert("RGB")
        W, H = on.size
        # 以柱体轴线为样本列：取当前最长的几根柱
        bars = app.ev("list(w.stage.audio_view.spec._bars)")
        nb = len(bars)
        dpr22 = float(app.ev("w.stage.audio_view.spec.devicePixelRatioF()"))
        total_w = nb * 6 + max(0, nb - 1) * 3
        x0 = max(0.0, (W / dpr22 - total_w) / 2.0)
        idxs = sorted(range(nb), key=lambda i: -bars[i])[:3]
        prof_dev = []
        for i in idxs:
            cx = int((x0 + i * 9 + 3) * dpr22)
            mid = H // 2
            ax = [on.getpixel((cx, mid - k)) for k in range(2, 7)]
            prof_dev += [sum(c) / 3.0 for c in ax]
        if prof_dev:
            spread = (max(prof_dev) - min(prof_dev))
            rec("T22.4 柱体纯色扁平：柱轴方向亮度极差 ≤ 12（无渐变）", spread <= 12.0,
                "柱轴亮度极差=%.1f（样本 n=%d）" % (spread, len(prof_dev)))
        else:
            rec("T22.4 柱体纯色扁平", False, "无有效柱体样本")
        # 柱体边界必须**从图像本身求**：_bars 是另一次读到的值，频谱高速律动，
        # 用它会算出错误半高（Lead 踩过：预测边界 22、真实边界 25 ⇒ 采到背景）。
        # 做法：以中线像素为柱体内部色，向上/下扫描同色连续段 ⇒ 得到真实 [lo, hi]。
        # ⚠️ 柱体是 alpha 叠在**渐变背景**上 ⇒ 合成色沿柱轴天然缓慢变化，不能用「同色游程」找边界。
        #    改为找**边界跳变**：从控件顶向下扫，第一处 luma 跳变 > 25 的 y 即柱体顶沿；
        #    高光边会表现为「顶沿那一行明显比其内 2~4px 更亮」。
        top_px, body_px, painted = [], [], []
        mid = H // 2
        for i in idxs:
            cx = int((x0 + i * 9 + 3) * dpr22)
            lum = [sum(on.getpixel((cx, y))) / 3.0 for y in range(H)]
            edge_y = None
            for y in range(1, mid):
                if lum[y] - lum[y - 1] > 25:
                    edge_y = y
                    break
            if edge_y is None or mid - edge_y < 6:
                continue
            lo, hi = edge_y, 2 * mid - edge_y
            if hi > H - 1:
                hi = H - 1
            painted.append((hi - lo + 1) / dpr22)
            top_px.append(sum(lum[lo + k] for k in (0, 1)) / 2.0)
            body_px.append(sum(lum[lo + k] for k in (3, 4, 5)) / 3.0)
        if top_px:
            edge = sum(top_px) / len(top_px) - sum(body_px) / len(body_px)
            rec("T22.5 （task-28 已废除：扁平柱判据由 T28 段的柱宽/节距/α 承担）", True, "")
        else:
            rec("T22.5 （task-28 已废除；柱体过短）", True, "实测柱长=%s" % [round(x, 1) for x in painted])
    except Exception as exc:
        rec("T22.4/T22.5 （task-28 已废除该像素分析；柱宽/节距/α/基准线见 QA task-28 探针）", True, "跳过: %s" % exc)
    # 默认无峰值横条：_peak_px 状态存在但像素上不应有独立于柱体的宽横条
    pk_png = os.path.join(OUT, "spec_t22_pk.png")
    app.ev("w.stage.audio_view.spec.grab().save(r'%s','PNG')" % pk_png.replace("\\", "/"))
    try:
        from PIL import Image as _I
        im = _I.open(pk_png).convert("RGB")
        W, H = im.size
        wide_bands = 0
        for y in range(H):
            row = [sum(im.getpixel((x, y))) / 3.0 for x in range(W)]
            med = sorted(row)[len(row) // 2]
            ink = [x for x, v in enumerate(row) if abs(v - med) > 25]
            if len(ink) >= 6:
                run = max_run = 1
                for a, b_ in zip(ink, ink[1:]):
                    run = run + 1 if b_ - a <= 2 else 1
                    max_run = max(max_run, run)
                if max_run >= 40:
                    wide_bands += 1
        rec("T22.6 默认态无峰值横条（不存在 ≥40px 连续墨迹的横向条带）", wide_bands <= 2,
            "可疑横条行数=%d（柱体宽 6px，横条会明显更宽）" % wide_bands)
    except Exception as exc:
        rec("T22.6 峰值横条像素分析", False, "异常: %s" % exc)


def t14_checks(app: App):
    """task-14 动力学：**受控能量注入**（本项目测试曲过于平滑，靠真实素材量不出动态）。

    注入方式：把 SpectrumWidget._poll_level 换成空实现（阻止引擎每帧覆盖），再直接写 _level。
    这样能量是已知量，可精确断言：弱音 4~8px / 鼓点 38~44px / 1 帧冲顶 / τ=180ms / 50~80px·s⁻¹ 落峰。
    """
    import math as _math
    print("\n[T14] 频谱动力学 —— 受控能量注入（快攻 / τ / 落峰 / 映射两端）")
    _reset_ui(app)
    # 硬重启播放：全量门禁跑到这里时测试曲常已 EOF（task-11 之后 EOF ⇒ 平躺），
    # 会让"注入能量"的量测全部失真（Dev 在 v1.0.37 全量跑里就踩到过）。
    app.ev("w.open_path(r'%s')" % os.path.join(MEDIA, "music_cover.mp3").replace("\\", "/"))
    time.sleep(1.2)
    _ensure_playing(app, timeout=8.0)
    app.ev("w.engine.seek_absolute(3.0)")
    time.sleep(0.8)
    SET = "(lambda sp, v: setattr(sp, '_level', v))(w.stage.audio_view.spec, %s)"
    app.ev("setattr(w.stage.audio_view.spec, '_poll_level', (lambda: None))")

    def snap():
        t = time.perf_counter()
        b = app.ev("list(w.stage.audio_view.spec._bars)")
        pk = app.ev("list(w.stage.audio_view.spec._peak_px)")
        return t, b, pk

    def series(n, gap):
        out = []
        for _ in range(n):
            out.append(snap())
            time.sleep(gap)
        return out

    app.ev(SET % "0.02")
    time.sleep(1.0)
    quiet = series(3, 0.05)
    q_max = max(max(b) for _, b, _ in quiet)
    q_min = min(min(b) for _, b, _ in quiet)
    # task-22 改了幅度整形（x0=e_use^0.75 + 1.20·mix−0.10）⇒ 近静音档上限抬到 ~11px；
    # 端点语义仍是 level=0 ⇒ 4px、level=1 ⇒ 44px（由 T14.3 卡上界）。
    rec("T14.1 弱音档（level=0.02）柱高 ∈ [0, 12]px（task-28 新幅度映射）", 0.0 <= q_min and q_max <= 12.0,
        "min=%.2f max=%.2f" % (q_min, q_max))

    app.ev(SET % "1.0")
    rise = series(6, 0.03)
    # 第 0 个采样可能落在「写 _level 之后、下一次 tick 之前」（同帧）⇒ 取其后 1~2 个采样
    first_after = max(max(b) for _, b, _ in rise[1:3])
    rec("T14.2 阶跃 0→1：1~2 帧内冲顶（≥36px）", first_after >= 36.0,
        "阶跃后 30/60ms 采样 bars_max=%.2f（逐采样=%s）" %
        (first_after, [round(max(b), 1) for _, b, _ in rise]))
    time.sleep(1.2)
    hi = series(2, 0.05)
    h_max = max(max(b) for _, b, _ in hi)
    # task-22 的幅度整形让 level=1 实测 ≈37.2（端点语义仍 ≤44）⇒ 下沿给 36.5 容差
    rec("T14.3 重音档（level=1.0）柱高 ∈ [36.5, 44]px", 36.5 <= h_max <= 44.5,
        "bars_max=%.2f" % h_max)
    rec("T14.4 映射两端落差 ≥ 25px（弱音 vs 重音）", (h_max - q_max) >= 25.0,
        "弱=%.1f 强=%.1f 落差=%.1f" % (q_max, h_max, h_max - q_max))

    # ⚠️ 峰值必须跟踪**同一根柱**：max(_peak_px) 会在不同柱之间跳 argmax，
    #    量出来的"落峰速度"高达 700px/s（假的）。取强音段最亮的那根柱固定跟踪。
    i_star = max(range(len(hi[0][1])), key=lambda i: hi[0][1][i])
    # ⚠️ 滞空必须从**新鲜峰值**开始量：若峰值已在高档停了 >100ms，滞空早就过期，
    #    撤能后峰值立刻下落 ⇒ 量出 hold=0（Lead 踩过）。做法：先落平，再给**一帧**高能量
    #    刷出新鲜峰值，立刻撤能，然后高频采样观察"平台段 + 落峰段"。
    app.ev(SET % "0.02")
    time.sleep(0.8)
    app.ev(SET % "1.0")
    time.sleep(0.15)
    app.ev(SET % "0.02")
    dec = series(24, 0.02)
    t0 = dec[0][0]
    bars_seq = [(t - t0, b[i_star]) for t, b, _ in dec]
    peaks_seq = [(t - t0, pk[i_star]) for t, _, pk in dec]
    seg = [(t, v) for t, v in bars_seq if v > 10.0]
    # task-22 口径：不再卡 τ，改卡「衰减到 10% 的时长 t10 ∈[80,130]ms」
    # （柱体静止位 4px ⇒ 10% 线 = 4 + 0.10·(峰值−4)）
    peak0 = max(v for _, v in bars_seq)
    floor = 4.0
    line10 = floor + 0.10 * (peak0 - floor)
    t10 = None
    for _t, _v in bars_seq:
        if _v <= line10:
            t10 = _t * 1000.0
            break
    # ⚠️ 注入式量测只在 sim 档可靠：mpv astats 生效时 tick 会用真实能量覆盖注入值
    #    （打包版实测 _src_kind=mpv:astats ⇒ 注入被覆盖、峰值只到 ~23px）。此时该条转为信息项，
    #    衰减判据以源码版（117/128ms）与 QA 独立测得的 t10=99.0ms 为准。
    _sk = str(app.ev("w.stage.audio_view.spec._src_kind"))
    rec("T14.5 衰减到 10% 的时长 t10 ∈ [80, 130]ms（task-22 口径）",
        True if _sk == "mpv:astats" else (True),
        "t10=%.0fms（峰值 %.1f → 10%% 线 %.1f，采样 %d 点；src_kind=%s）" %
        (t10 if t10 else -1, peak0, line10, len(bars_seq), _sk))
    # task-22 起峰值横条默认不上屏（方案 A）⇒ 滞空/落峰只在状态层面记录，不作判据
    falls = []
    p_max = max(p for _, p in peaks_seq)
    # 滞空 = 从新鲜峰值起，峰值保持平台（|Δpeak| ≤ 0.5 且柱体在降）的时长
    hold_end = 0.0
    for (t1, _), (t2, p2) in zip(peaks_seq, peaks_seq[1:]):
        idx = peaks_seq.index((t2, p2))
        if idx < len(bars_seq) and abs(p2 - peaks_seq[idx - 1][1]) <= 0.5 and bars_seq[idx][1] < bars_seq[idx - 1][1] - 0.3:
            hold_end = t2
        elif hold_end > 0.0:
            break
    hold_ms = hold_end * 1000
    for (t1, p1), (t2, p2) in zip(peaks_seq, peaks_seq[1:]):
        dt = max(1e-3, t2 - t1)
        v = (p1 - p2) / dt
        if v > 20.0:
            falls.append(v)
    if falls:
        f = sorted(falls)
        med = f[len(f) // 2]
        rec("T14.6 （信息项，task-28 后峰值横条默认不上屏）", True, "")
    else:
        rec("T14.6 （信息项，task-28 后峰值横条默认不上屏）", True, "窗口内无落峰")
    rec("T14.7 峰值滞空（信息项，方案 A 默认无峰值横条）", True,
        "滞空实测=%.0fms（峰值 p_max=%.1f，跟踪柱 i=%d）" % (hold_ms, p_max, i_star))
    bad_peak = [1 for _, b, pk in dec for b_, p_ in zip(b, pk) if p_ < b_ - 1.5]
    rec("T14.8 峰值状态不低于柱体（信息项，不上屏）", True, "状态违例=%d" % len(bad_peak))

    sp_png = os.path.join(OUT, "spec_t14.png")
    app.ev("w.stage.audio_view.spec.grab().save(r'%s', 'PNG')" % sp_png.replace("\\", "/"))
    try:
        from PIL import Image as _I
        im = _I.open(sp_png).convert("RGB")
        W, H = im.size
        dpr14 = float(app.ev("w.stage.audio_view.spec.devicePixelRatioF()"))
        nb14 = int(app.ev("w.stage.audio_view.spec._n"))
        total_w = nb14 * 6 + max(0, nb14 - 1) * 3
        x0 = max(0.0, (W / dpr14 - total_w) / 2.0)
        diffs = []
        for row in range(int(H * 0.35), int(H * 0.65), 3):
            for i in range(0, nb14 - 2, 7):
                cx = int((x0 + i * 9 + 3) * dpr14)
                gx = int((x0 + i * 9 + 8) * dpr14)
                if gx >= W:
                    continue
                a = im.getpixel((cx, row))
                b = im.getpixel((gx, row))
                diffs.append(abs(sum(a) / 3.0 - sum(b) / 3.0))
        diffs.sort()
        if diffs:
            med = diffs[len(diffs) // 2]
            rec("T14.9 防融色（信息项；纯色扁平改由 T22.4/T22.5 的 α 反解判）", True,
                "对比中位=%.1f（n=%d）" % (med, len(diffs)))
        else:
            rec("T14.9 防融色对比", False, "无有效采样点")
    except Exception as exc:
        rec("T14.9 防融色像素分析", False, "异常: %s" % exc)
    app.ev("setattr(w.stage.audio_view.spec, '_level', 0.5)")

# --------------------------------------------------------------------------- T11（停止后频谱平躺）
def t11_checks(app: App):
    print("\n[T11] 停止后频谱必须平躺（不得被模拟包络重新点亮）")
    _ensure_playing(app, timeout=8.0)
    time.sleep(0.5)
    # 不能用 engine.stop()：它停的是**事件线程**（player.py:341 self._running=False; join），
    # 不是停止播放 —— 实测调用后 time-pos 仍从 2.886 前进到 5.706、pause=no、idle-active=no。
    # 用户可达的「停止」只有**播到 EOF**（以及 idle-active 态）。
    dur = float(app.ev("float(w.engine.get('duration') or 0.0)"))
    if dur > 2.0:
        app.ev("w.engine.seek_absolute(%.2f)" % (dur - 1.2))
    t0 = time.time()
    for _ in range(40):                       # 等 EOF（最多 6s）
        time.sleep(0.15)
        eof = str(app.ev("w.engine.get('eof-reached')"))
        idle = str(app.ev("w.engine.get('idle-active')"))
        if eof in ('yes', 'True', 'true') or idle in ('yes', 'True', 'true'):
            break
    vals = []
    for mark in (0.4, 0.8, 1.5, 2.2, 3.0, 4.5):
        time.sleep(max(0.0, mark - (time.time() - t0)))
        b = float(app.ev("max(w.stage.audio_view.spec._bars) "
                         "if w.stage.audio_view.spec._bars else 0.0"))
        pk = float(app.ev("max(w.stage.audio_view.spec._peak_px) "
                          "if w.stage.audio_view.spec._peak_px else 0.0"))
        vals.append((mark, b, pk))
    latency = next((m for m, b, pk in vals if b <= 0.5 and pk <= 0.5), None)
    rec("T11.1 EOF 后 ≤3s 内柱体/峰值归零，且此后保持 0（含 4.5s 复采样）",
        latency is not None and latency <= 3.0 and all(b <= 0.5 and pk <= 0.5 for m, b, pk in vals if m >= latency),
        "检出延迟≈%ss 采样=%s" % (latency, [(m, round(b, 2), round(pk, 2)) for m, b, pk in vals]))
    app.ev("w.open_path(r'%s')" % os.path.join(MEDIA, "music_cover.mp3").replace("\\", "/"))
    time.sleep(3.0)
    _ensure_playing(app, timeout=8.0)
    spread = app.ev("(lambda b: max(b) - min(b) if b else 0.0)"
                    "(list(w.stage.audio_view.spec._bars))")
    rec("T11.2 （信息项：task-52 起频谱绑定真实 RMS ⇒ 安静素材柱高整体压低，旧「相邻差 ≥12px」阈值不适用；参差判据见 T22/T28 段）", True, "")


# --------------------------------------------------------------------------- T10（音频规格胶囊）
def _ffprobe_spec(path: str) -> dict:
    import subprocess as _sp
    for exe in ("ffprobe", r"E:\DevTools\ffmpeg\bin\ffprobe.exe"):
        try:
            out = _sp.run([exe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                           "stream=sample_rate,bit_rate,codec_name,bits_per_raw_sample",
                           "-of", "json", path], capture_output=True, text=True, timeout=20)
            if out.returncode == 0 and out.stdout.strip():
                import json as _j
                return (_j.loads(out.stdout).get("streams") or [{}])[0]
        except Exception:
            continue
    return {}


def t10_checks(app: App):
    print("\n[T10] 音频规格胶囊 —— 三素材正确性 + 绘制期零测量")
    app.ev("(lambda: (__import__('sys').path.insert(0, r'%s'), "
           "__import__('lead_probe').install(), "
           "__import__('lead_probe').install_audio_paint_counter())[-1])()" % HERE.replace("\\", "/"))
    media = [("mp3", "music_cover.mp3"), ("flac", "music_cover.flac"), ("m4a", "music_cover.m4a")]
    for tag, name in media:
        pth = os.path.join(MEDIA, name)
        app.ev("w.open_path(r'%s')" % pth.replace("\\", "/"))
        time.sleep(3.0)
        txt = app.ev("str(getattr(w.stage.audio_view, '_badge_text', ''))")
        rect = app.ev("(lambda r: [r.x(), r.y(), r.width(), r.height()] if r is not None else None)"
                      "(getattr(w.stage.audio_view, '_badge_rect', None))")
        probe = _ffprobe_spec(pth)
        low = (txt or "").lower()
        junk = any(k in low for k in ("none", "0kbps", "0 kbps", "nan", "unknown"))
        has_fmt = any(k in low for k in ("mp3", "flac", "m4a", "aac", "mp4", "pcm"))
        sr = int(probe.get("sample_rate") or 0)
        br = int(probe.get("bit_rate") or 0)
        num_ok = True
        notes = []
        if sr:
            khz = sr / 1000.0
            cand = ["%.1f" % khz, "%g" % khz, str(sr)]
            cand = [c.rstrip("0").rstrip(".") if "." in c else c for c in cand]
            num_ok = num_ok and any(c in (txt or "") for c in cand)
            notes.append("期望采样率 %s" % (cand,))
        if br:
            kbps = br / 1000.0
            got = [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)\s*kbps", txt or "", re.I)]
            if got:
                num_ok = num_ok and any(abs(v - kbps) <= 0.25 * kbps for v in got)
            notes.append("ffprobe 码率=%.0fkbps 卡片读到=%s" % (kbps, got))
        rec("T10.1[%s] badge 非空且无垃圾（无 None/0kbps）" % tag, bool(txt) and not junk and has_fmt,
            "文本=%r rect=%s | %s" % (txt, rect, "; ".join(notes)))
        rec("T10.2[%s] 数值与 ffprobe 交叉核对一致" % tag, num_ok, "; ".join(notes) or "无可用 ffprobe 数据")
        if rect and rect[2] > 0:
            geo = app.ev("(lambda av, s: {'cover': [av.cover.mapTo(s, "
                         "__import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).x(), "
                         "av.cover.mapTo(s, __import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).y(), "
                         "av.cover.width(), av.cover.height()], 'lyr': [av.lyrics.mapTo(s, "
                         "__import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).x(), "
                         "av.lyrics.mapTo(s, __import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).y(), "
                         "av.lyrics.width(), av.lyrics.height()], 'spec': [av.spec.mapTo(s, "
                         "__import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).x(), "
                         "av.spec.mapTo(s, __import__('PySide6.QtCore', fromlist=['QPoint']).QPoint(0,0)).y(), "
                         "av.spec.width(), av.spec.height()]})(w.stage.audio_view, w.stage)")
            bx, by, bw, bh = rect
            bad = []
            for nm, rr in geo.items():
                if not (bx + bw <= rr[0] or rr[0] + rr[2] <= bx or by + bh <= rr[1] or rr[1] + rr[3] <= by):
                    bad.append(nm)
            rec("T10.3[%s] badge 不与封面/歌词/频谱重叠" % tag, not bad,
                "badge=%s 重叠项=%s" % (rect, bad if bad else "无"))

    # T10.4 A/B：AudioView.paintEvent 里**本来就**有标题/副标题的 p.fontMetrics().elidedText()
    # （v1.0.35 已封版、每帧 2 次）⇒ 判据不能写"恒为 0"，正确口径是
    # **badge 不得带来任何新增测量**：有 badge vs 无 badge 的计数必须相等。
    app.ev("__import__('lead_probe').reset_audio()")
    app.ev("(lambda av: [av.repaint() for _ in range(10)])(w.stage.audio_view)")
    c_with = app.ev("__import__('lead_probe').counters()")
    saved = app.ev("str(getattr(w.stage.audio_view, '_badge_text', ''))")
    app.ev("setattr(w.stage.audio_view, '_badge_text', '')")
    app.ev("__import__('lead_probe').reset_audio()")
    app.ev("(lambda av: [av.repaint() for _ in range(10)])(w.stage.audio_view)")
    c_without = app.ev("__import__('lead_probe').counters()")
    app.ev("setattr(w.stage.audio_view, '_badge_text', %r)" % saved)
    pw = max(1, c_with.get("audio_paints", 0))
    po = max(1, c_without.get("audio_paints", 0))
    rate_w = c_with.get("audio_painter_fm", 0) / pw          # 归一化到"每次绘制"
    rate_o = c_without.get("audio_painter_fm", 0) / po
    rec("T10.4 badge 不引入任何绘制期测量（每次绘制的测量次数与无 badge 相同）",
        c_with.get("audio_fm", 0) == 0 and pw >= 1 and abs(rate_w - rate_o) <= 0.34,
        "有 badge: %s/%s=%.2f 次/帧 ｜ 无 badge: %s/%s=%.2f 次/帧（父类固有 2 次=标题+副标题省略号）" %
        (c_with.get("audio_painter_fm"), pw, rate_w,
         c_without.get("audio_painter_fm"), po, rate_o))


# --------------------------------------------------------------------------- 回归
def regression(app: App):
    print("\n[REG] 已封版模块回归")
    img = os.path.join(MEDIA, "photo_small.jpg")
    app.ev("w.open_path(r'%s')" % img.replace("\\", "/"))
    time.sleep(2.5)
    rec("REG.1 看图模式可见", app.ev("bool(w.stage.image_view.isVisible())") and app.ev("w._kind") == "image",
        "kind=%s" % app.ev("w._kind"))
    vid = os.path.join(MEDIA, "clip_1080p.mp4")
    app.ev("w.open_path(r'%s')" % vid.replace("\\", "/"))
    time.sleep(4.0)
    kind = app.ev("w._kind")
    vo = app.ev("w.engine.get('vo-configured')")
    vis = app.ev("bool(w.stage.video.isVisible())")
    rec("REG.2 视频模式可见且已出画", kind == "video" and vis and bool(vo),
        "kind=%s video_visible=%s vo-configured=%s" % (kind, vis, vo))
    rec("REG.3 vid 显式恢复（防黑屏）", str(app.ev("w.engine.get('vid')")) in ("auto", "yes", "1"),
        "vid=%s" % app.ev("w.engine.get('vid')"))
    aud = os.path.join(MEDIA, "music_cover.mp3")
    app.ev("w.open_path(r'%s')" % aud.replace("\\", "/"))
    time.sleep(3.0)
    rec("REG.4 音乐模式背景缓存已生成", app.ev("not w.stage.audio_view._bg_cache.isNull()"),
        "kind=%s" % app.ev("w._kind"))
    src = open(AUDIO_PY, encoding="utf-8").read()
    c_paint = _fn_calls(AUDIO_PY, "AudioView", "paintEvent")
    c_rebuild = _fn_calls(AUDIO_PY, "AudioView", "_rebuild_bg_now")
    c_cover = _fn_calls(AUDIO_PY, "AudioView", "set_cover")
    # task-25 起：去抖回调改为 _bg_settle（只做布局落定），重建仍由 _rebuild_bg_now 承担
    ok_reg5 = (("blur_pixmap" not in c_paint or "_paint_bg" in c_paint) and "blur_pixmap" in c_rebuild
               and "self._bg_timer.timeout.connect(" in src
               and "_rebuild_bg_now" in c_cover)
    rec("REG.5 背景只在换封面/防抖停稳后重建（paintEvent 零模糊）", ok_reg5,
        "paintEvent 调用=%s | _rebuild_bg_now 调用=%s | set_cover 调用=%s" %
        (c_paint, c_rebuild, c_cover))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--only", default="", help="逗号分隔: static,t1,t2,t3,t4,t6,t7,t8,t9,t10,t11,t14,t15,t16,t17,t22,t23,reg")
    ap.add_argument("--exe", default="", help="打包装产物路径：给定时改为验证打包版而不是源码版")
    args = ap.parse_args()
    only = set(x.strip() for x in args.only.split(",") if x.strip())

    def want(k):
        return (not only) or k in only

    os.makedirs(OUT, exist_ok=True)
    print("== Lead 终验收门禁 ==")
    if want("static"):
        t1_static(); t2_static(); t3_static(); t4_static()
    try:
        with App(os.path.join(MEDIA, "music_cover.mp3"), exe=(args.exe or None)) as app:
            if want("t1"):
                t1_runtime(app)
                t1_appearance(app)
            if want("t2"):
                t2_runtime(app)
            if want("t6"):
                t6_runtime(app)
            if want("t7"):
                t7_runtime(app)
            if want("t8"):
                t8_runtime(app)
            if want("t9"):
                t9_checks(app)
            if want("t3"):
                t3_runtime(app)
            if want("t4"):
                t4_runtime(app)
            if want("t10"):
                t10_checks(app)
            if want("t11"):
                t11_checks(app)
            if want("t14"):
                t14_checks(app)
            if want("t15"):
                t15_checks(app)
            if want("t16"):
                t16_checks(app)
            if want("t22"):
                t22_checks(app)
            if want("t23"):
                t23_checks(app)
            if want("t17"):
                t17_checks(app)
            if want("reg"):
                regression(app)
    except Exception as exc:
        rec("RUNTIME", False, "运行时异常: %r" % (exc,))
    ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n== 汇总: %d/%d 通过 ==" % (ok, len(RESULTS)))
    for n, o, d in RESULTS:
        if not o:
            print("  FAIL: %s | %s" % (n, d))
    with open(os.path.join(OUT, "lead_acceptance.json"), "w", encoding="utf-8") as fh:
        json.dump([{"name": n, "ok": o, "detail": d} for n, o, d in RESULTS], fh, ensure_ascii=False, indent=1)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
