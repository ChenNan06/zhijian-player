"""音乐模式视图（v1.0.31 架构级重写：背景零重算 + 单一主帧时钟 + 硬兜底）。

事故回溯（v1.0.26~30 的病灶，逐条对应本文件的写法）：
  1. `paintEvent` 里调 `_rebuild_blur()` ✗ ⇒ 任何 update()（歌词 16ms + 频谱 25ms +
     轮询 100ms，且子控件透明会连带父级重绘）都在重算模糊 + 全窗 blit ✗
     ⇒ 主线程 100% 饱和 ⇒ 全屏卡死/闪退 ✓
     【本文件】背景只在【换封面】或【resize 停稳 200ms 后】生成一次，
       绘制期只有一行 `drawPixmap`（FastTransformation 拉伸），零模糊计算 ✓
  2. 三个独立定时器各自 update() ✗ ⇒ 合并为 AudioView 里**唯一**的主帧时钟（33ms）✓
  3. 歌词折行用 width ≤ 0 计算 ⇒ 高度塌陷 ⇒ 整块不画 ✗
     【本文件】宽度至少 200px、逐行 try/except + `fm.height()+4` 保底 ✓
  4. 频谱没 raise、几何被推出视野、暂停时全隐形 ✗
     【本文件】强制 raise + 固定贴在控制栏上方 + 暂停保留 2px 底线 ✓
"""
from __future__ import annotations

import logging
import math
import os
import random
import time

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

BLUR_WORK_W = 160
BG_DEBOUNCE_MS = 200
# ★★ v1.0.34-4：帧时钟 33 → **16ms（60 FPS）**。
#   为什么 33ms 只能跑到 ~21 FPS：Windows 的系统 tick 是 15.625ms，QTimer 的等待被量化到
#   tick 网格上 —— 33ms 请求需要 3 个 tick = 46.875ms（2 个 tick 只有 31.25ms < 33ms），
#   实测 p50 46.7~46.9ms 与 46.875 吻合到 0.2ms，且换 PreciseTimer 也一样。
#   16ms 请求只占 1 个 tick ⇒ 实测 p50 **15.70ms（≈63.7 FPS）**（同实例 setInterval 扫描：
#   33→46.69 / 16→15.70 / 15→15.49 / 12→14.31 / 8→8.22）。
#   ⚠️ 落峰与暂停归零已按**真实 dt** 折算（见 SpectrumWidget.tick / 本文件常量注释），
#      所以帧长从 33ms 变到 16ms，落峰速度与归零时长都不变。
FRAME_MS = 16
SPEC_BAR_W = 6
SPEC_GAP = 3
SPEC_PITCH = SPEC_BAR_W + SPEC_GAP
SPEC_PEAK_H = 2
SPEC_PEAK_FALL_PX_S = 4.0          # 峰值匀速下落 4px·s⁻¹（按**真实 dt** 折算，与帧率无关）
SPEC_H = 48
# ---- 对数频段动态映射（v1.0.34-2）--------------------------------------------
# 第 i 根柱代表**等比频段** f_i = SPEC_F_LO·(SPEC_F_HI/SPEC_F_LO)^(i/(n-1))：
# 像素轴等分 ⇒ 低频段细、高频段粗；旧写法 low = 1 - i/n 是线性频段（等价于把
# 低端压成一坨、高端拉长），配合 0.30 硬垫底 + 1.35 增益后绝大多数柱直接顶到 1.0，
# 实拍就是"一条高度接近的蓝带"（用户截图 + Lead 基线：中位极差仅 4.6px）。
SPEC_F_LO = 50.0                   # 模拟频段下限（Hz）
SPEC_F_HI = 16000.0                # 模拟频段上限（Hz）
SPEC_TILT = 0.35                   # 频谱倾斜指数（能量 ∝ f^-0.35，对数轴上近似线性 dB 斜率）
SPEC_BAR_MIN = 0.18                # 播放态最低柱高比例 ⇒ 0.18×44px ≈ 8px（卡片 8~44px 硬指标）
# ★★ v1.0.36（task-08）：频谱改为「居中基线 + 上下对称的山峰形态」。
#   ⚠️ 42px 是**柱体总长**（控件只有 48px ⇒ 上下各 21px、上下各留 3px 余量），
#      不是单侧高度；映射写成总长后再对半分配。
SPEC_BAR_TOTAL_MIN = 6.0           # 短柱总长（px）
SPEC_BAR_TOTAL_MAX = 42.0          # 高峰柱总长（px）
SPEC_MID_SIGMA = 0.26              # 对称高斯包络 σ：中间段最高、两翼按同一曲线镜像递减
SPEC_ENERGY_EVERY = 3              # 每 N 帧读一次 mpv 能量（属性读要进 mpv 锁，必须限速）
SPEC_LEVEL_DB = (-60.0, 0.0)       # astats RMS_level(dB) → 0..1 的映射区间
SPEC_DECAY_PER_S = 2.2             # ⚠️ task-08 起**仅用于切歌/停止后的清空**；暂停不再衰减
SPEC_IDLE_REPAINT_S = 0.15         # 暂停态"只有峰值在下落"时的最高重绘间隔（≈6.7FPS）
                                   # 峰值只走 4px·s⁻¹（0.063px/帧），63FPS 全速重绘纯属空转；
                                   # 限速后暂停 CPU 从 21.8% 降到 ~5%（QA 判据 ≤8%）
SPEC_SIDE = 40
CONTROL_BAR_H = 72          # 底部控制栏高度（**回落**用；正常路径量胶囊真实几何，见 _spec_y_for）
SPEC_GAP_TO_BAR = 16        # 频谱底边与「控制栏实体上沿」的间隙（单一真源；同时就是回落公式
                            # h - CONTROL_BAR_H - SPEC_H - SPEC_GAP_TO_BAR 里的那个 16）
LYRIC_GROUP_PAD = 29
LYRIC_INNER_PAD = 6
# 歌词滚动收敛：v1.0.34 的基准是"每帧(33ms)收敛 22%"，改成按真实 dt 折算后
# 每秒收敛率与帧长无关（33ms 与 16ms 的到达时间差 <2%，见 task-4 报告验算）。
LYRIC_EASE_REF_S = 0.033            # 基准帧长（v1.0.34 的 33ms），不是当前 FRAME_MS
LYRIC_EASE_ALPHA = 0.22             # 基准帧长下的每帧收敛比例（原实现写死的 0.22）
CURRENT_LINE_RATIO = 0.40
FADE_PX = 40
BASE_W, BASE_H = 1200.0, 800.0
SCALE_MIN, SCALE_MAX = 0.85, 1.45


def _is_light() -> bool:
    try:
        from .theme import is_light

        return bool(is_light())
    except Exception:
        return False


def _accent() -> QColor:
    try:
        from .theme import P

        return QColor(P.get("accent") or "#2E7CF6")
    except Exception:
        return QColor("#2E7CF6")


# ★★ v1.0.36（task-09）：浅色模式背景"去漂白"用到的两个量
#   · VIBRANCE：模糊 Pixmap 阶段做饱和度补偿 +18%（落在卡片的 +15%~20% 区间内）
#   · LIGHT_SCRIM_A：浅色遮罩由 199（0.78，接近漂白）降到 82（0.32）
VIBRANCE = 1.18
LIGHT_SCRIM_A = 82


def apply_vibrance(img: QImage, k: float = VIBRANCE) -> QImage:
    """饱和度补偿（**在缩放生成模糊 Pixmap 阶段**做，见卡片要求）。

    s' = min(1, s·k)；HSV 只改 S，不动 H/V ⇒
      · 灰阶像素 s=0 ⇒ s'=0，**绝不被染色**（卡片 B 的硬要求）；
      · 色相不变，冷暖过渡保留；
      · s=0.3 的高斯样张实测 +18%（见探针 task09 报告）。
    ⚠️ 只在 **160×work_h 的小图**上跑（约 1.4 万像素，Python 逐像素 ~20ms），
      绝不放到 1278×712 的成品图上（那会是 90 万次 Python 循环）。
    """
    if img.isNull() or k <= 1.0:
        return img
    out = img.convertToFormat(QImage.Format_ARGB32)
    for yy in range(out.height()):
        for xx in range(out.width()):
            c = out.pixelColor(xx, yy)
            hh, ss, vv, aa = c.getHsvF()
            if ss <= 0.0:
                continue                      # 灰阶原样返回（不染色）
            c.setHsvF(hh, min(1.0, ss * k), vv, aa)
            out.setPixelColor(xx, yy, c)
    return out


def blur_pixmap(src: QImage, w: int, h: int, vibrance: float | None = None) -> QPixmap:
    """铺满 (w,h) + 大半径模糊（**只在换封面/停稳后调用**，绝不在绘制期调用）。

    vibrance=None ⇒ **浅色主题才做饱和度补偿**（深色保持不变，卡片 D）；
    显式传数值则强制该系数（探针用它做 A/B：同一张样张进 → 出）。
    """
    if vibrance is None:
        vibrance = VIBRANCE if _is_light() else 1.0
    if src.isNull() or w <= 1 or h <= 1:
        return QPixmap()
    try:
        filled = src.scaled(QSize(w, h), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = max(0, (filled.width() - w) // 2)
        y = max(0, (filled.height() - h) // 2)
        cropped = filled.copy(x, y, min(w, filled.width()), min(h, filled.height()))
        work_h = max(2, int(BLUR_WORK_W * h / max(1, w)))
        small = cropped.scaled(BLUR_WORK_W, work_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        big = small.scaled(max(w, BLUR_WORK_W), max(h, work_h), Qt.IgnoreAspectRatio,
                           Qt.SmoothTransformation)
        mid = big.scaled(BLUR_WORK_W, work_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        # ★ task-09：饱和度补偿在小图上做（灰阶不动、色相不动），再放大成背景
        mid = apply_vibrance(mid, vibrance)
        return QPixmap.fromImage(mid.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
    except Exception:
        return QPixmap()


def vinyl_placeholder(side: int) -> QPixmap:
    """无封面兜底：黑胶唱片 + 几何音符纹理（绝不留空）。"""
    side = max(32, int(side))
    pm = QPixmap(side, side)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    c = QPointF(side / 2.0, side / 2.0)
    r = side / 2.0 - 1
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(20, 21, 24))
    p.drawEllipse(c, r, r)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 14), max(1.0, side / 220.0)))
    for i in range(1, 9):
        rr = r * i / 9.0
        p.drawEllipse(c, rr, rr)
    p.setPen(Qt.NoPen)
    grad = QRadialGradient(c, r * 0.42)
    grad.setColorAt(0.0, QColor(150, 158, 170))
    grad.setColorAt(0.6, QColor(96, 104, 118))
    grad.setColorAt(1.0, QColor(58, 62, 72))
    p.setBrush(QBrush(grad))
    p.drawEllipse(c, r * 0.42, r * 0.42)
    p.setBrush(QColor(255, 255, 255, 30))
    p.drawEllipse(c, r * 0.055, r * 0.055)
    p.setPen(QPen(QColor(255, 255, 255, 26), max(1.0, side / 160.0)))
    for i in range(6):
        ang = i * math.pi / 3.0 + 0.4
        p.drawLine(QPointF(c.x() + math.cos(ang) * r * 0.48, c.y() + math.sin(ang) * r * 0.48),
                   QPointF(c.x() + math.cos(ang + 0.5) * r * 0.8,
                           c.y() + math.sin(ang + 0.5) * r * 0.8))
    p.end()
    return pm


# ★★ v1.0.36（task-07）：封面容器四周留 14px「阴影走廊」。
#   可见封面边长**不变**（窗口 231 / 全屏 346）—— 由布局按 side + 2*COVER_PAD 下发控件几何，
#   不是把封面缩小。3 圈阴影全部画在这 14px 内、绝不触碰控件边界（R1/R2 的物理保证）。
COVER_PAD = 14
#: (距封面边界的偏移, 描边带宽, alpha) —— 由内到外渐淡、且**整像素对齐**。
#  基准 rgba(0,0,0,0.08) ≈ alpha 20；三圈外沿落在距控件边 2px 处（最外 2px 全透明 ⇒ R1 最外 1px=0）。
#  ⚠️ 为什么偏移取半整数 + 带宽取整：描边以路径为中心，路径落在 x.0 边界上时
#     每条带正好覆盖整数像素（不会被抗锯齿摊成一半），径向剖面才有干净的
#     20 → 14 → 9 台阶（相邻阶跃 6 / 5）。第一版取 (3,3)/(6,3)/(9,3) 会落在
#     半像素上，剖面出现 10→20 这种假"上跳"，被自己的探针抓出来。
COVER_SHADOW_RINGS = ((1.5, 3.0, 20), (5.0, 4.0, 14), (9.0, 4.0, 9))


class CoverArt(QWidget):
    """专辑封面：**单一圆角正方形**（无封面时黑胶占位）。

    ⚠️ v1.0.34-1：这里原来还有两层"圆角投影"（for grow, alpha in ((3,22),(7,13))）。
    它们的矩形是 (x-grow+1.5, y-grow+3.0, side+2*grow, side+2*grow) —— 天生**超出
    控件矩形**（grow=7 时右/下各多出 5.5/4.0 px）。QWidget 画不到自身 rect 之外，
    超出的部分被控件边界**硬裁成直线** ⇒ 右下（浅色下右上/左下也有）出现一截
    死灰色**直角**底板 —— 这就是用户反复报的"封面右下角直角露底"的真因。
    投影在 231px 的封面上本来也几乎看不见，删掉它换来"角外像素 alpha 恒为 0"。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # ★★ v1.0.33 ①：**容器自身背景漏出直角**就是那块"右下角灰底"的真因 ——
        #    Qt 的 QWidget 默认会用自己的调色板底色填一个矩形，圆角只出现在我们
        #    自己画的路径上 ⇒ 圆角之外露出容器的方形底。必须让容器本身完全透明。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setStyleSheet("background: transparent;")
        self._cover: QPixmap | None = None
        self._scaled = QPixmap()
        self._key = (0, False)

    def set_cover(self, data: bytes | None):
        pm = None
        if data:
            img = QImage()
            if img.loadFromData(data) and not img.isNull():
                pm = QPixmap.fromImage(img)
        self._cover = pm
        self._scaled = QPixmap()
        self.update()

    def _current(self, side: int) -> QPixmap:
        if self._key != (side, self._cover is not None) or self._scaled.isNull():
            if self._cover is not None:
                s = min(self._cover.width(), self._cover.height())
                sq = self._cover.copy((self._cover.width() - s) // 2,
                                      (self._cover.height() - s) // 2, s, s)
                self._scaled = sq.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                self._scaled = vinyl_placeholder(side)
            self._key = (side, self._cover is not None)
        return self._scaled

    def paintEvent(self, _ev):            # noqa: N802
        # ★★ v1.0.36（task-07）：控件 = 可见封面 + 四周 COVER_PAD 阴影走廊。
        art = min(self.width(), self.height()) - 2 * COVER_PAD      # 可见封面边长
        if art < 8:
            return
        pm = self._current(art)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        x = (self.width() - art) / 2.0
        y = (self.height() - art) / 2.0
        R = 12.0
        # ---- 3 圈外发散阴影（全部落在 14px 走廊内，最外沿距控件边界 3.5px）----
        #   为什么用「逐圈描边」而不是一层大矩形：矩形会被控件边界硬裁成直角灰底
        #   （这正是 task-02 的病灶）。这里每圈都是一条**圆角矩形描边**：
        #     · 最外圈 9+1.5 = 10.5px < 14px ⇒ 绝不触碰边界（R1/R2 同时成立）；
        #     · 描边跟着半径 R+off 一起长 ⇒ 弧线绕开控件极角（R2 的"无直角"定义）；
        #     · alpha 由内到外 20 → 14 → 9 单调递减、相邻阶跃 ≤6（R3），3 个台阶（R4）。
        p.setBrush(Qt.NoBrush)
        for off, bw, alpha in COVER_SHADOW_RINGS:
            rr = QRectF(x - off, y - off, art + 2 * off, art + 2 * off)
            p.setPen(QPen(QColor(0, 0, 0, alpha), bw))
            p.drawRoundedRect(rr, R + off, R + off)
        # ---- 封面本体 ----
        # ★★ v1.0.34-1：绘制全流程只有**这一条** addRoundedRect(12,12) ——
        #   它同时充当裁剪路径与描边路径，零 fillRect / drawRect。
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, art, art), R, R)
        p.setClipPath(path)
        p.drawPixmap(QRectF(x, y, art, art), pm, QRectF(pm.rect()))
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + 0.5, y + 0.5, art - 1, art - 1), R, R)
        p.end()


class SpectrumWidget(QWidget):
    """底部频谱：柱宽 6 / 间距 3 / 峰值 2px / N 自适应 / 4px·s⁻¹ 线性落峰。

    **没有自己的定时器**（由主帧时钟调用 `tick()`）；暂停保留 2px 底线，绝不整块隐形。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._n = 0
        self._bars: list[float] = []
        self._peak_px: list[float] = []
        self._phase: list[float] = []
        self._playing = False
        self._energy = 0.0
        self._beat = 0.0
        self._engine = None                # 由 AudioView 每帧注入（可能为 None）
        self._src_kind = "sim"             # "mpv:astats" | "sim"（数据源如实上报）
        self._src_tried = False            # 挂 filter 只试一次
        self._level = 0.0                  # mpv 读到的能量 0..1（读不到时用模拟）
        self._frame_i = 0
        self._last_t = 0.0                 # 上一帧的真实时刻（落峰/归零按 dt 折算）
        self._last_idle_t = 0.0            # 上一次"仅峰值下落"的重绘时刻（空闲限速用）
        self.setMinimumHeight(SPEC_H)
        self._ensure(72)

    def _ensure(self, n: int):
        if n == self._n:
            return
        self._n = n
        self._bars = [0.0] * n
        self._peak_px = [0.0] * n
        self._phase = [random.random() * math.tau for _ in range(n)]

    def set_playing(self, on: bool):
        self._playing = bool(on)

    def attach_engine(self, engine) -> None:
        """注入播放引擎（用于尝试读取**真实音频能量**；为 None 时走模拟包络）。"""
        self._engine = engine

    def reset(self) -> None:
        """清空重置（**只在切歌/停止**时调用；暂停走冻结，绝不衰减 —— task-08）。"""
        if not any(self._bars) and not any(self._peak_px):
            return
        for i in range(self._n):
            self._bars[i] = 0.0
            self._peak_px[i] = 0.0
        self._energy = 0.0
        self._beat = 0.0
        self._level = 0.0
        self.update()

    def _try_mpv_energy(self) -> bool:
        """挂 astats 分析滤镜并读 af-metadata；**失败安全回落**（返回 False 即走模拟）。

        ⚠️ 这是"分析型"滤镜（lavfi astats），只旁路统计、不改音频数据；
           但为了**不因它影响播放**，全程 try/except 包裹，且只尝试一次：
           任何一步失败（设不上 / 读不到 / 结构不对）立刻把 af 还原为空并回落。
        """
        eng = self._engine
        if eng is None or self._src_tried:
            return False
        self._src_tried = True
        try:
            from ..mpv.libmpv import MPV_FORMAT_NODE

            eng.set("af", "@stats:lavfi=[astats=metadata=1:reset=1]")
            node = eng.get("af-metadata/stats", MPV_FORMAT_NODE, None)
            if not isinstance(node, dict):
                raise RuntimeError("af-metadata/stats 不可读: %r" % (node,))
            keys = [k for k in node.keys() if str(k).endswith("Overall.RMS_level")]
            if not keys:
                raise RuntimeError("af-metadata 无 RMS_level: %r" % (list(node)[:4],))
            self._src_kind = "mpv:astats"
            return True
        except Exception:
            log.debug("mpv 能量读取不可用，回落模拟包络", exc_info=True)
            self._src_kind = "sim"
            try:
                if self._engine is not None:
                    self._engine.set("af", "")       # 还原空滤镜链，绝不影响音频
            except Exception:
                pass
            return False

    def _poll_level(self) -> None:
        """限速读取真实能量（每 SPEC_ENERGY_EVERY 帧一次，避免每帧进 mpv 锁）。"""
        if self._src_kind != "mpv:astats" and not self._try_mpv_energy():
            self._level = -1.0                       # 标记：本帧无真实数据
            return
        try:
            from ..mpv.libmpv import MPV_FORMAT_NODE

            node = self._engine.get("af-metadata/stats", MPV_FORMAT_NODE, None)
            if isinstance(node, dict):
                for k, v in node.items():
                    if str(k).endswith("Overall.RMS_level"):
                        try:
                            db = float(v)
                        except Exception:
                            continue
                        lo, hi = SPEC_LEVEL_DB
                        self._level = max(0.0, min(1.0, (db - lo) / (hi - lo)))
                        return
        except Exception:
            log.debug("mpv 能量读取失败，本帧回落模拟", exc_info=True)
        self._level = -1.0

    def tick(self) -> bool:
        """主帧时钟驱动的一帧：**全部按真实 dt 折算**（帧率无关）。

        返回"本帧是否有可见变化"：暂停且柱体/峰值都已归零时返回 False，
        上层据此**跳过 update()** —— 否则 63FPS 的帧钟会带着整幅 AudioView
        （背景贴图 + 扫层）空转重绘：实测暂停时进程 CPU 59.4%，跳过空转重绘后 4.7%
        （QA 判据：暂停/空闲 ≤8%）。
        

        数据源如实说明：仍是**包络模拟**（能量包络 + 节拍 + 每柱相位随机），
        本轮**没有**接真实 FFT/PCM —— 但频段落位、倾斜、动态范围与落峰/归零
        全部按真实物理量建模，指标可量化复核。
        """
        now = time.perf_counter()
        dt = (now - self._last_t) if self._last_t else (FRAME_MS / 1000.0)
        self._last_t = now
        dt = max(0.001, min(0.10, dt))         # 首帧/卡顿夹住，异常 dt 不把落峰速度带飞
        w = max(80, self.width())
        self._ensure(max(16, min(160, (w + SPEC_GAP) // SPEC_PITCH)))
        h = float(max(6, self.height()))
        n = self._n
        if not self._playing:
            # ★★ task-08：暂停/停止时**无条件定格** —— 柱高与峰值一个像素都不动、
            #    不做任何衰减/计时（上一轮 task-03 的"暂停平滑归零"已按卡片作废）。
            #    返回 False ⇒ 上层跳过 update()，连重绘都不会发生（CPU 也就降下来了）。
            return False
        self._frame_i += 1
        if self._frame_i % SPEC_ENERGY_EVERY == 1:
            self._poll_level()
        # 能量：优先用 mpv 实测值（_level ≥ 0）；读不到时用包络模拟（如实标注 _src_kind）
        sim = max(0.0, min(1.0, self._energy * 0.86
                           + 0.14 * (0.42 + 0.34 * math.sin(now * 1.7)
                                     + 0.24 * random.random())))
        self._energy = sim
        self._beat = 0.72 * self._beat + 0.28 * (sim ** 2.1)
        e_use = self._level if self._level >= 0.0 else (0.55 * sim + 0.45 * self._beat)
        # ---- 对称山峰包络：w(u) = exp(−0.5·((u−0.5)/σ)²)，中间 1.0、两翼镜像递减 ----
        bars, peaks = self._bars, self._peak_px
        changed = False
        x0 = e_use
        for i in range(n):
            u = i / float(max(1, n - 1))
            wgt = math.exp(-0.5 * ((u - 0.5) / SPEC_MID_SIGMA) ** 2)     # 对称，可镜像验证
            osc = 0.30 + 0.70 * abs(math.sin(now * (1.1 + 4.2 * (1.0 - u)) + self._phase[i]))
            v = max(0.0, min(1.0, wgt * (0.45 + 0.55 * osc) * (0.55 + 0.75 * x0)))
            total = SPEC_BAR_TOTAL_MIN + (SPEC_BAR_TOTAL_MAX - SPEC_BAR_TOTAL_MIN) * v
            if abs(total - bars[i]) > 0.05:
                changed = True
            bars[i] = total
            if total >= peaks[i]:
                if abs(total - peaks[i]) > 0.05:
                    changed = True
                peaks[i] = total
            else:
                if peaks[i] > 0.0:
                    changed = True
                peaks[i] = max(0.0, peaks[i] - SPEC_PEAK_FALL_PX_S * dt)   # 落峰仍 4px/s·dt
        return changed

    def paintEvent(self, _ev):            # noqa: N802
        w, h = self.width(), self.height()
        if w < 20 or h < 6:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        acc = _accent()
        base = QColor(acc)
        base.setAlpha(70)
        # ★★ task-08：基线居中（y_mid = h/2），柱体以中线为轴**上下对称**伸展。
        #   几何用浮点 h/2，48px 高 ⇒ 24.0；柱体总长 v ∈ [6,42] ⇒ 上下各 v/2 ≤ 21px，
        #   上下各留 3px 余量，永不越界。
        y_mid = h / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(base)
        p.drawRect(QRectF(0, y_mid - 1.0, w, 2.0))     # 居中基线（替代原来的底部死地平线）
        self._ensure(max(16, min(160, (max(80, w) + SPEC_GAP) // SPEC_PITCH)))
        total = self._n * SPEC_BAR_W + (self._n - 1) * SPEC_GAP
        x0 = max(0.0, (w - total) / 2.0)
        # 渐变改成**对称**（中线最亮、上下两端渐暗），与对称山峰形态一致
        top = QColor(acc)
        top.setAlpha(70)
        midc = QColor(acc)
        midc.setAlpha(150)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, top)
        grad.setColorAt(0.5, midc)
        grad.setColorAt(1.0, top)
        p.setBrush(QBrush(grad))
        for i, v in enumerate(self._bars):
            # v 就是**柱体总长**（px）：上下各一半 ⇒ 逐柱 |上半−下半| = 0（±1px 判据稳过）
            if v < 0.5:
                continue
            half = v / 2.0
            p.drawRect(QRectF(x0 + i * SPEC_PITCH, y_mid - half, SPEC_BAR_W, v))
        pk = QColor(acc)
        pk.setAlpha(190)
        p.setBrush(pk)
        for i, ypx in enumerate(self._peak_px):
            if ypx <= 1.0:
                continue
            half = ypx / 2.0
            # 峰值标记**上下各一个**（与柱体同轴对称，暂停时一起冻结）
            p.drawRect(QRectF(x0 + i * SPEC_PITCH, max(0.0, y_mid - half - SPEC_PEAK_H),
                              SPEC_BAR_W, SPEC_PEAK_H))
            p.drawRect(QRectF(x0 + i * SPEC_PITCH, min(h - SPEC_PEAK_H, y_mid + half),
                              SPEC_BAR_W, SPEC_PEAK_H))
        p.end()


# ★★ v1.0.36（task-10）：音频规格胶囊
BADGE_FONT_PX = 11                 # 卡片指定字号
BADGE_PAD_X = 9
BADGE_PAD_Y = 5
BADGE_RADIUS = 10
BADGE_BELOW_COVER_GAP = 10         # 封面下沿到胶囊的留白
#: 有损格式（没有「位深」这个概念）——只在有损格式上显示 kbps，无损显示位深/采样率
_LOSSY_EXT = ("MP3", "M4A", "AAC", "OGG", "OPUS", "WMA", "MP4")


def flac_streaminfo(path: str):
    """读 FLAC STREAMINFO → (位深, 采样率)；任何异常/不合法都返回 (None, None)。

    STREAMINFO 数据在偏移 8（4 字节 fLaC 标记 + 4 字节块头），其内偏移 10..17 是
    20bit 采样率 + 3bit 声道 + 5bit(位深−1) + 36bit 总样本数。
    """
    try:
        with open(path, "rb") as fh:
            blob = fh.read(26)
        if len(blob) < 26 or blob[:4] != b"fLaC":
            return None, None
        x = int.from_bytes(blob[18:26], "big")
        rate = x >> 44
        bits = ((x >> 36) & 0x1F) + 1
        if not (8000 <= rate <= 768000) or bits not in (8, 16, 20, 24, 32):
            return None, None
        return bits, rate
    except Exception:
        return None, None


def badge_text_for(path: str, params=None, bitrate=None) -> str:
    """拼 badge 文本：FLAC · 24bit / 96kHz 或 MP3 · 320kbps。

    ⚠️ 拿不到的项**一律省略**（绝不写 None/0/空壳）；全空则返回空串 ⇒ 上层整枚隐藏。
    """
    ext = os.path.splitext(path or "")[1].lstrip(".").upper()
    rate = bits = kbps = None
    if isinstance(params, dict):
        try:
            rate = int(float(params.get("samplerate") or 0)) or None
        except Exception:
            rate = None
    if ext == "FLAC":
        b2, r2 = flac_streaminfo(path)
        bits = b2
        rate = r2 or rate
    if rate is not None and not (8000 <= rate <= 768000):
        rate = None
    if bits is not None and bits not in (8, 16, 20, 24, 32):
        bits = None
    try:
        kbps = int(round(float(bitrate) / 1000.0)) if bitrate else None
    except Exception:
        kbps = None
    if kbps is not None and not (8 <= kbps <= 2000):
        kbps = None
    if not (bits or rate or kbps):
        return ""            # 没有任何可测规格 ⇒ 整枚隐藏（卡片 B：不许留空壳胶囊）
    detail = [s for s in (("%dbit" % bits) if bits else "",
                          ("%gkHz" % (rate / 1000.0)) if rate else "") if s]
    if ext and detail:
        head = "%s · %s" % (ext, " / ".join(detail))
    elif ext:
        head = ext
    else:
        head = " / ".join(detail)
    if ext in _LOSSY_EXT and kbps:
        head = ("%s · %dkbps" % (head, kbps)) if head else ("%dkbps" % kbps)
    return head


class SpecBadge(QWidget):
    """音频规格胶囊（轻巧、微弱圆角描边）。

    **宽度预排版缓存**：_text_w 只在文本变化时算一次 ⇒ paintEvent 内零文本测量
    （task-04 的封版判据：paintEvent 内 fontMetrics/QFontMetrics/boundingRect/
     horizontalAdvance 调用数必须为 0）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setStyleSheet("background: transparent;")
        self._text = ""
        self._text_w = 0.0
        self.setVisible(False)

    def set_specs_text(self, text: str):
        text = (text or "").strip()
        if text != self._text:
            self._text = text
            self._text_w = self._measure(text)      # ← 全类唯一测量点（绘制期之外）
        self.setVisible(bool(text))
        self.update()

    @staticmethod
    def _measure(text: str) -> float:
        if not text:
            return 0.0
        try:
            f = QFont()
            f.setPixelSize(BADGE_FONT_PX)
            return float(QFontMetrics(f).horizontalAdvance(text))
        except Exception:
            return float(len(text) * 7)

    def hint_size(self) -> QSize:
        if not self._text:
            return QSize(0, 0)
        return QSize(int(self._text_w) + BADGE_PAD_X * 2, BADGE_FONT_PX + BADGE_PAD_Y * 2)

    def paintEvent(self, _ev):            # noqa: N802
        if not self._text:
            return
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        light = _is_light()
        r = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 26, 36, 24) if light else QColor(255, 255, 255, 28))
        p.drawRoundedRect(r, BADGE_RADIUS, BADGE_RADIUS)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(20, 26, 36, 44) if light else QColor(255, 255, 255, 58), 1.0))
        p.drawRoundedRect(r, BADGE_RADIUS, BADGE_RADIUS)
        f = QFont()
        f.setPixelSize(BADGE_FONT_PX)
        p.setFont(f)
        p.setPen(QColor(38, 46, 58, 225) if light else QColor(228, 234, 242, 215))
        # 文本已按缓存宽度排版，这里只居中绘制 ⇒ 零测量
        p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self._text)
        p.end()


class LyricsPanel(QWidget):
    """滚动歌词：动态折行 + 组内 6px / 组间 29px + 当前行 40% 黄金线。

    **没有自己的定时器**（主帧时钟调用 `step()`）；宽度/高度全部带兜底 ——
    宽度为 0 会让折行高度塌陷、整块不画（v1.0.30 歌词蒸发的根因）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._lines: list[tuple[int, str]] = []
        self._idx = -1
        self._tops: list[float] = []
        self._heights: list[float] = []
        self._main_h: list[float] = []      # 预排版缓存（主词折行高度）
        self._trans_h: list[float] = []     # 预排版缓存（翻译折行高度）
        self._offset = 0.0
        self._target = 0.0
        self._spec_text = ""
        self._spec_w = 0.0                  # ★ 预排版：占位药丸底的宽度（绘制期零测量）
        self._relayout_pending = False      # 绘制期发现缓存失效时的延后重排去重标记
        self._last_t = 0.0                  # 上一帧真实时刻（step 的 dt 折算用）
        self._placeholder = "纯音乐 / 暂无歌词"

    @staticmethod
    def _merge_bilingual(lines):
        out: list[tuple[int, str]] = []
        for t, txt in lines:
            if out and out[-1][0] == t and "\n" not in out[-1][1]:
                out[-1] = (t, out[-1][1] + "\n" + txt)
            else:
                out.append((t, txt))
        return out

    def _measure_spec_w(self, text: str) -> float:
        """预排版：占位分支那个药丸底的宽度（**只在这里**测量，绘制期零测量）。

        ⚠️ 旧实现把这个 horizontalAdvance 放在 paintEvent 里（audio_view.py 占位分支），
        60 FPS 下等于每帧一次文本测量 —— 现在挪到文本变化时算一次（KB §9.70：
        不许用"本帧不画"来掩盖，必须把测量搬出绘制期）。
        """
        if not text:
            return 0.0
        try:
            f = QFont()
            f.setPixelSize(12)
            return float(QFontMetrics(f).horizontalAdvance(text)) + 24.0
        except Exception:
            return float(len(text) * 8 + 24)

    def set_spec_text(self, text: str):
        """外部唯一的 spec 文本入口（AudioView.set_spec / set_lines 都走它）。"""
        text = text or ""
        if text != self._spec_text:
            self._spec_text = text
            self._spec_w = self._measure_spec_w(text)
        self.update()

    def set_lines(self, lines, spec_text: str = ""):
        self._lines = self._merge_bilingual(list(lines or []))
        self.set_spec_text(spec_text)
        self._idx = -1
        self._offset = 0.0
        self._target = 0.0
        self._relayout()
        self.update()

    def _content_width(self) -> int:
        return max(200, int(self.width()) - 8)

    def _measure(self, text: str, pixel: int, bold: bool, width: int) -> float:
        if not text:
            return float(pixel * 1.6)
        try:
            f = QFont()
            f.setPixelSize(max(8, int(pixel)))
            f.setBold(bool(bold))
            fm = QFontMetrics(f)
            rect = fm.boundingRect(QRect(0, 0, max(200, int(width)), 9999),
                                   Qt.TextWordWrap | Qt.AlignLeft, text)
            h = float(rect.height())
            return h if h > 0 else float(fm.height() + 4)
        except Exception:
            return float(pixel * 1.6 + 4)

    def _relayout(self):
        self._tops, self._heights = [], []
        w = self._content_width()
        sr = 1.0
        try:
            sr = self.parentWidget().scale_ratio()
        except Exception:
            pass
        # ★★ v1.0.32 ②：**在这里**（载入歌词 / resize 时）一次性算完所有折行高度，
        #    存进 self._main_h / self._trans_h / self._tops / self._heights；
        #    paintEvent 只读缓存 ⇒ 零 QFontMetrics 调用 ⇒ 不再掉帧。
        self._main_h: list[float] = []
        self._trans_h: list[float] = []
        y = 0.0
        for i, (_t, txt) in enumerate(self._lines):
            cur = (i == self._idx)
            main, trans = (txt.split("\n", 1) if "\n" in txt else (txt, ""))
            mh = self._measure(main, (27 if cur else 20) * sr, cur, w)
            th = self._measure(trans, (18 if cur else 14) * sr, cur, w) if trans else 0.0
            h = max(18.0, mh + (LYRIC_INNER_PAD + th if trans else 0.0))
            self._main_h.append(mh)
            self._trans_h.append(th)
            self._tops.append(y)
            self._heights.append(h)
            y += h + LYRIC_GROUP_PAD
        self._retarget()

    def set_position_ms(self, ms: int):
        if not self._lines:
            return
        idx = -1
        for i, (t, _txt) in enumerate(self._lines):
            if t <= ms:
                idx = i
            else:
                break
        if idx != self._idx:
            self._idx = idx
            self._relayout()
            self.update()

    def _retarget(self):
        if not self._lines or self._idx < 0 or self._idx >= len(self._tops):
            self._target = 0.0
            return
        gold = self.height() * CURRENT_LINE_RATIO
        self._target = gold - (self._tops[self._idx] + self._heights[self._idx] / 2.0)

    def step(self, dt: float | None = None):
        """滚动一帧：收敛比例按**真实 dt** 折算（帧长变了视觉速度不变）。

        旧实现每帧固定收敛 22%；33ms→16ms 后同样的每帧比例会让每秒收敛率翻倍
        （滚动变快一倍、观感更"急"）。这里保持**每秒收敛率**与 v1.0.34 完全一致：
            alpha(dt) = 1 − (1 − 0.22) ** (dt / 0.033)
        dt=33ms ⇒ 0.22（与旧版逐帧一致）；dt=16ms ⇒ 0.114；两者到达时间差 <2%。
        """
        if not self._lines:
            return
        now = time.perf_counter()
        if dt is None:
            dt = (now - self._last_t) if self._last_t else LYRIC_EASE_REF_S
        self._last_t = now
        dt = max(0.001, min(0.10, dt))
        d = self._target - self._offset
        if abs(d) < 0.4:
            if self._offset != self._target:
                self._offset = self._target
                self.update()
            return
        alpha = 1.0 - (1.0 - LYRIC_EASE_ALPHA) ** (dt / LYRIC_EASE_REF_S)
        self._offset += d * alpha
        self.update()

    def _deferred_relayout(self):
        """绘制期发现缓存失效时的一轮延后重排（一次性，不是新定时器）。"""
        self._relayout_pending = False
        self._relayout()
        self.update()                      # ← 下一帧内容恢复

    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._relayout()
        self.update()

    @staticmethod
    def _col(light: bool, alpha: float, strong: bool = False) -> QColor:
        if strong:
            return QColor(0, 0, 0) if light else QColor(255, 255, 255)
        c = QColor(20, 22, 26) if light else QColor(255, 255, 255)
        c.setAlphaF(max(0.0, min(1.0, alpha)))
        return c

    def paintEvent(self, _ev):            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        w, h = self.width(), self.height()
        light = _is_light()
        if not self._lines:
            f = QFont()
            f.setPixelSize(15)
            p.setFont(f)
            p.setPen(self._col(light, 0.60))
            p.drawText(QRectF(0, h * 0.42 - 20, w, 40), Qt.AlignCenter, self._placeholder)
            if self._spec_text:
                f2 = QFont()
                f2.setPixelSize(12)
                p.setFont(f2)
                # ★ 宽度用**预排版缓存**（set_spec_text 里算好），绘制期零测量
                tw = self._spec_w or 24.0
                rect = QRectF((w - tw) / 2.0, h * 0.42 + 24, tw, 26)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(20, 26, 36, 16) if light else QColor(255, 255, 255, 22))
                p.drawRoundedRect(rect, 13, 13)
                p.setPen(self._col(light, 0.70))
                p.drawText(rect, Qt.AlignCenter, self._spec_text)
            p.end()
            return
        if not self._tops or len(self._tops) != len(self._lines):
            # ★★ v1.0.34-4：绘制期**绝不测量**（KB §9.70：也不许用"本帧不画"糊过去）。
            #   这里只登记一次延后重排，交给下一轮事件循环；_deferred_relayout() 重建缓存后
            #   **立即 update()** ⇒ 下一帧一定有内容（内容恢复有像素级证据，见 task-4 探针）。
            if not self._relayout_pending:
                self._relayout_pending = True
                QTimer.singleShot(0, self._deferred_relayout)
            return
        p.setClipRect(QRectF(0, 0, w, h))
        sr = 1.0
        try:
            sr = self.parentWidget().scale_ratio()
        except Exception:
            pass
        flag = Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop
        cw = self._content_width()
        for i, (_t, text) in enumerate(self._lines):
            if i >= len(self._tops):
                break
            top = self._offset + self._tops[i]
            hh = self._heights[i]
            if top + hh < -FADE_PX or top > h + FADE_PX:
                continue
            cur = (i == self._idx)
            main, trans = (text.split("\n", 1) if "\n" in text else (text, ""))
            f = QFont()
            f.setPixelSize(max(10, int((27 if cur else 20) * sr)))
            f.setWeight(QFont.Medium if cur else QFont.Normal)
            p.setFont(f)
            main_h = self._heights[i] - (
                LYRIC_INNER_PAD + self._trans_h[i] if trans else 0.0)
            main_h = max(14.0, main_h)
            if cur:
                p.setPen(QColor(0, 0, 0, 90) if not light else QColor(255, 255, 255, 120))
                p.drawText(QRectF(1.2, top + 1.2, cw, main_h), flag, main)
                p.setPen(self._col(light, 1.0, strong=True))
            else:
                p.setPen(self._col(light, 0.40))
            p.drawText(QRectF(0, top, cw, main_h), flag, main)
            if trans:
                f2 = QFont()
                f2.setPixelSize(max(9, int((18 if cur else 14) * sr)))
                p.setFont(f2)
                p.setPen(self._col(light, 0.90 if cur else 0.28))
                p.drawText(QRectF(0, top + main_h + LYRIC_INNER_PAD, cw,
                                  max(12.0, self._trans_h[i])), flag, trans)
        p.end()


class AudioView(QWidget):
    """音乐模式总成：静态背景缓存 + 双栏 + 底部频谱 + **唯一主帧时钟**。"""

    request_drawer = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._cover_data: bytes | None = None
        # ★ task-10：规格胶囊（可读属性 _badge_text/_badge_rect 供探针/门禁直接读）
        self._audio_path = ""
        self._badge_text = ""
        self._badge_rect = QRect()
        self._badge_retry = 0
        self._badge_tick = 0
        self._stopped = False              # ★ task-11：媒体已停止/播完（end-file 事件置位）
        self._idle_probe_t = 0.0           # 上次直读 idle-active 的时刻（限速用）
        self._idle_last = False            # 上次直读结果
        self._engine_wired = None          # 已连接 end_file 的引擎对象（只连一次）
        self._bg_cache = QPixmap()          # ★ 静态背景缓存（绝不每帧重算）
        self._bg_img: QImage | None = None
        self.cover = CoverArt(self)
        self.badge = SpecBadge(self)          # ★ task-10 规格胶囊
        self.lyrics = LyricsPanel(self)
        self.spec = SpectrumWidget(self)
        self._title = ""
        self._sub = ""
        self._playing = False
        self._pos_provider = None
        self._cur_pos = 0.0
        self._last_pos: float | None = None
        self._pos_accum = 0
        self._last_move_t = 0.0        # 播放位置最后一次**前进**的时刻（停滞判据，见 _on_frame）
        self._spec_body_top_warned = False   # 实体几何读取失败只告警一次（R1）
        self._frame = QTimer(self)
        self._frame.setInterval(FRAME_MS)   # ★ 全组件唯一帧时钟
        self._frame.timeout.connect(self._on_frame)
        self._frame.start()
        self._bg_timer = QTimer(self)
        self._bg_timer.setSingleShot(True)
        self._bg_timer.setInterval(BG_DEBOUNCE_MS)
        self._bg_timer.timeout.connect(self._rebuild_bg_now)

    def _engine_obj(self):
        """拿播放引擎对象（拿不到返回 None）—— 能量读取与暂停位共用这一处。"""
        try:
            return getattr(self.window(), "engine", None)
        except Exception:
            return None

    def _on_media_ended(self, *_args) -> None:
        """媒体结束/停止（mpv end-file 事件，含用户点停止）⇒ 立即判停 + 清空频谱。

        胶囊不受影响（它是"这一首的规格"，停止后仍可读）；恢复播放时见 set_position。
        """
        self._stopped = True
        try:
            self.set_playing(False)
        except Exception:
            pass
        try:
            self.spec.reset()
        except Exception:
            pass

    def _engine_idle(self) -> bool:
        """是否处于"停止/无媒体"（用来触发频谱清空重置；暂停不算）。

        判据用 mpv 已观察属性：idle-active 为真，或当前没有 path。
        """
        eng = self._engine_obj()
        if eng is None:
            return False
        try:
            if not (eng.cached("path", "") or ""):
                return True
        except Exception:
            pass
        # ★★ task-11 实测：stop 之后 **cached 全不可用** ——
        #   cached("idle-active") = MISS、cached("path") 仍是旧路径、cached("pause") = false，
        #   而**直接读** idle-active / core-idle 立刻是 "yes"（time-pos 直接读是 None）。
        #   直读要进 mpv 锁 ⇒ 限速：只在"位置停滞 ≥0.25s"时才读，且最多每 0.1s 一次。
        #   正常播放时位置一直在前进 ⇒ 这条路径根本不执行，不会每帧抢锁。
        try:
            now = time.perf_counter()
            if self._last_move_t and (now - self._last_move_t) < 0.25:
                return False
            if (now - self._idle_probe_t) < 0.1:
                return self._idle_last
            self._idle_probe_t = now
            self._idle_last = False
            # ⚠️ 只认 idle-active / eof-reached：
            #    · core-idle 在**暂停**时也是 yes（"核心无事可做"）⇒ 会把暂停误判成停止，
            #      破坏 task-08 的暂停定格，故**排除**；
            #    · eof-reached 覆盖"播到结尾被 keep-open 停住"（此时 paused=true、
            #      idle-active 仍是 no），task-11 要求停止必须落平。
            for prop in ("idle-active", "eof-reached"):
                v = eng.get(prop, 0, None)
                yes = (isinstance(v, str) and v.strip().lower() in ("yes", "true")) or v is True
                if yes:
                    self._idle_last = True
                    break
            return self._idle_last
        except Exception:
            return self._idle_last

    def _engine_paused(self):
        """读 mpv 真实暂停位（只读；拿不到返回 None）。

        ⚠️ 为什么不问 MainWindow 要：MainWindow._drive_audio_view() 是**死代码**
        （全仓只有定义、没有任何调用点），所以 v1.0.33 起只能靠"位置 > 0.05"猜播放态。
        而 mpv 的 pause 是 engine 已经在观察的属性（player.py:262），
        engine.cached("pause") 直接可用 —— 不新增 IPC、不新增定时器、不改其他文件。
        """
        try:
            eng = getattr(self.window(), "engine", None)
            if eng is None:
                return None
            v = eng.cached("pause", None)
            return None if v is None else bool(v)
        except Exception:
            return None

    def _on_frame(self):
        idle = False
        try:
            # ★ 把引擎注入频谱（用于尝试真实能量读；为 None 时频谱自动走模拟包络）
            eng = self._engine_obj()
            if eng is not None and self.spec._engine is not eng:
                self.spec.attach_engine(eng)
            # ★★ task-11：停止/播完只能靠**事件**判定 —— 实测 stop 之后 mpv 的
            #   path 仍是旧路径、idle-active 没被观察（cached 直接 MISS）、pause 还是
            #   false，于是"位置停滞 1.5s"要等 1.5 秒才生效，这期间模拟包络会把柱体
            #   重新点亮（QA 实测「没声音了频谱还在跳舞」）。连接已有的 end_file 信号
            #   （player.py 事件线程已 emit，不新增定时器、不新增 IPC）。
            if eng is not None and eng is not self._engine_wired:
                self._engine_wired = eng
                try:
                    eng.end_file.connect(self._on_media_ended)
                except Exception:
                    log.debug("连接 end_file 失败", exc_info=True)
            # ★ 停止/无媒体 ⇒ 清空重置（**暂停不重置**：暂停走无条件定格，task-08）
            idle = self._engine_idle()
            if idle or self._stopped:
                # ⚠️ 必须**先**把播放态置 False 再进入本帧的 tick：tick() 在 _on_frame
                #    开头就跑，若此刻 _playing 还是 True，它会用模拟包络把刚清空的柱体
                #    重新点亮一帧（实测残留 ~27px 并就此冻结）。顺序反了就等于没清。
                self.spec.reset()
                if self._playing:
                    self.set_playing(False)
            # ★ task-10：规格数据可能晚于 set_lyrics 到位（mpv 的 audio-params 是
            #   异步观察属性）⇒ 复用**已有帧钟**做有限次补读，不新增定时器。
            if self._badge_retry > 0:
                self._badge_tick += 1
                if self._badge_tick >= 30:
                    self._badge_tick = 0
                    self._badge_retry -= 1
                    self.set_audio_specs(self._audio_path)
            # ★ 只有"这一帧真的画得出来不一样"才触发重绘（暂停定格后不再空转重绘）
            if self.spec.tick():
                self.spec.update()
        except Exception:
            pass
        try:
            self.lyrics.step()
        except Exception:
            pass
        self._pos_accum += 1
        if self._pos_accum >= 6:            # ~200ms 取一次播放位置
            self._pos_accum = 0
            self._poll_position()
        # ★★ v1.0.34-2：播放态**必须来自真实播放状态**，不能再拿"位置 > 0.05"当判据。
        #   旧写法（v1.0.33 ③）在**中途暂停**时位置恒 > 0.05 ⇒ 永远当"在播放" ⇒
        #   暂停后频谱照旧满幅跑（用户实测 + Lead 基线：pause 后 3s max(_bars) 恒 1.000、
        #   峰值恒 38.0 不落）。现在三条优先级：
        #     ① 首选 mpv 的 pause 属性（真实状态）；
        #     ② 拿不到引擎时退回"位置是否前进"；
        #     ③ 两条路径都叠加"位置停滞 > 1.5s 即判为停"（缓冲/卡死时不许假装在播放）。
        try:
            now = time.perf_counter()
            stalled = (now - self._last_move_t) > 1.5 if self._last_move_t else True
            paused = self._engine_paused()
            if self._stopped or idle:
                # ★★ task-11：停止/无媒体（end-file 事件或 idle）⇒ **直接判不在播放**。
                #   不这么写的话：stop 之后 mpv 的 pause 仍是 False，而"位置停滞"
                #   判据要等满 1.5s 才生效 ⇒ 这 1.5s 里模拟包络会把柱体重新点亮到
                #   ~35px（QA 实测「没声音了频谱还在跳舞」）。idle 优先，立即平躺。
                playing = False
            elif paused is None:
                # 回落路径（引擎不可用/属性读不到）：位置为 0 或停滞 ⇒ 不在播放
                playing = (self._cur_pos > 0.05) and not stalled
            else:
                playing = (not paused) and not stalled
            if playing != self._playing:
                self.set_playing(playing)
        except Exception:
            pass

    def _poll_position(self):
        if self._pos_provider is None or not self.isVisible():
            return
        try:
            self.set_position(float(self._pos_provider() or 0.0))
        except Exception:
            pass

    def scale_ratio(self) -> float:
        try:
            r = min(self.width() / BASE_W, self.height() / BASE_H)
        except Exception:
            return 1.0
        return max(SCALE_MIN, min(SCALE_MAX, float(r)))

    def set_position_provider(self, fn):
        self._pos_provider = fn

    def set_position(self, seconds: float):
        """mpv time-pos 是**秒**，歌词时间轴是**毫秒** ⇒ 必须 ×1000 再比。

        ⚠️ 这里同时维护"位置最后一次前进的时刻"：_on_frame 用它判断是否停滞
        （停滞 >1.5s ⇒ 认为不在播放）。阈值 0.02s 是为了滤掉 time-pos 的取整抖动。
        """
        v = max(0.0, float(seconds))
        if self._last_pos is None or abs(v - self._last_pos) >= 0.02:
            self._last_move_t = time.perf_counter()
            if v > 0.05:
                self._stopped = False       # ★ task-11：位置重新前进 ⇒ 解除停止态
        self._last_pos = v
        self._cur_pos = v
        self.lyrics.set_position_ms(int(v * 1000))

    def set_cover(self, data: bytes | None):
        self._cover_data = data or None
        self.cover.set_cover(self._cover_data)
        img = None
        if self._cover_data:
            tmp = QImage()
            if tmp.loadFromData(self._cover_data) and not tmp.isNull():
                img = tmp
        self._bg_img = img
        self._rebuild_bg_now()

    def set_meta(self, title: str, sub: str):
        self._title = title or ""
        self._sub = sub or ""
        self.update()

    def set_spec(self, spec: str):
        # ★ 走 LyricsPanel 的唯一入口：那里会顺手把占位药丸的宽度预排版缓存下来
        self.lyrics.set_spec_text(spec or "")

    def set_audio_specs(self, audio_path: str) -> None:
        """读规格并灌给胶囊（**唯一入口**）。

        数据源（如实声明）：
          · 采样率/比特率：mpv 已观察属性 audio-params / audio-bitrate（engine.cached）；
          · 位深：**自解析文件头**（目前实现 FLAC STREAMINFO；有损格式本无位深，按卡片省略）。
        失败/缺失 ⇒ 文本为空串 ⇒ 胶囊整枚隐藏（绝不留 None/0 空壳）。
        """
        self._audio_path = audio_path or ""
        params = bitrate = None
        # ★ 溯源守卫（探针抓到的真问题）：mpv 的 audio-params/bitrate 说的是**引擎里
        #   当前那首**，不是我们传进来的路径。文件不存在、或引擎里还是上一首时，
        #   绝不能把旧值安到新路径上（否则会显示「ZZZ · 44.1kHz」这种拼接垃圾）。
        if not (self._audio_path and os.path.isfile(self._audio_path)):
            self.badge.set_specs_text("")
            self._badge_retry = 0
            self._sync_badge_state()
            self._layout_children()
            return
        try:
            eng = self._engine_obj()
            if eng is not None:
                cur = ""
                try:
                    cur = str(eng.cached("path", "") or "")
                except Exception:
                    cur = ""
                if cur and os.path.abspath(cur) != os.path.abspath(self._audio_path):
                    eng = None          # 引擎里还是上一首 ⇒ 只信文件头，不信 mpv 参数
            if eng is not None:
                # ⚠️ 这两个属性**不在** player.py 的观察列表里（cached 恒为 None），
                #    必须直接读（一次/切歌，不是每帧）：audio-params 是节点(6)、
                #    audio-bitrate 是 double(5)。读不到再用「文件大小 ÷ 时长」兜底
                #    （实测与 ffprobe 的 stream bit_rate 差 <2%）。
                params = eng.cached("audio-params", None) or eng.get("audio-params", 6, None)
                bitrate = (eng.cached("audio-bitrate", None)
                           or eng.get("audio-bitrate", 5, None))
                if not bitrate:
                    try:
                        dur = float(eng.cached("duration", 0) or eng.get("duration", 5, 0) or 0)
                        sz = os.path.getsize(self._audio_path) if self._audio_path else 0
                        if dur > 1.0 and sz > 0:
                            bitrate = sz * 8.0 / dur
                    except Exception:
                        pass
        except Exception:
            params = bitrate = None
        try:
            text = badge_text_for(self._audio_path, params, bitrate)
        except Exception:
            log.debug("规格文本构建失败", exc_info=True)
            text = ""
        try:
            self.badge.set_specs_text(text)
        except Exception:
            log.debug("规格胶囊设置失败", exc_info=True)
        # 数据到齐（含采样率或比特率）就停止补读；否则靠帧钟做有限次重试
        self._badge_retry = 0 if (text and ("kHz" in text or "kbps" in text)) else 6
        self._badge_tick = 0
        self._sync_badge_state()
        self._layout_children()

    def _sync_badge_state(self) -> None:
        """把胶囊的**可见文本与矩形**同步到可读属性（任意状态下都可读，缺失即空串/空矩形）。"""
        try:
            t = getattr(self.badge, "_text", "") or ""
            # _badge_text：当前可见文本（无信息时为空串，绝不 None）；_badge_rect：可见时才给矩形
            self._badge_text = t
            self._badge_rect = (QRect(self.badge.geometry())
                                if (t and self.badge.isVisible()) else QRect())
        except Exception:
            self._badge_text = ""
            self._badge_rect = QRect()

    def set_lyrics(self, audio_path: str):
        # ★ task-08：切歌 ⇒ 清空重置频谱（卡片 F：只在切歌/停止时重置）
        try:
            self.spec.reset()
        except Exception:
            pass
        self._stopped = False               # ★ task-11：换歌 ⇒ 解除停止态
        # ★ task-10：换歌即更新规格胶囊（切歌的唯一入口）
        try:
            self.set_audio_specs(audio_path)
        except Exception:
            log.debug("切歌时更新规格胶囊失败", exc_info=True)
        lines = []
        try:
            from ..media.lyrics import load_lyrics

            lines, _src = load_lyrics(audio_path)
        except Exception:
            lines = []
        self.lyrics.set_lines(lines, self.lyrics._spec_text)

    def set_playing(self, on: bool):
        self._playing = bool(on)
        self.spec.set_playing(on)

    def _rebuild_bg_now(self):
        """真正重建背景缓存（只在换封面 / resize 停稳后调用，绝不在绘制期）。

        ⚠️ 这里顺带重跑一次 _layout_children()：这个去抖计时器（200ms）**已经是**
        "布局已落定"的信号 —— resizeEvent 当下胶囊几何可能还没排完，量到的会是旧值；
        停稳后再量一次才是最终几何（幂等，且**没有新增定时器**）。
        """
        try:
            self._layout_children()
        except Exception:
            pass
        w, h = max(2, self.width()), max(2, self.height())
        if self._bg_img is None or self._bg_img.isNull():
            self._bg_cache = QPixmap()
            self.update()
            return
        self._bg_cache = blur_pixmap(self._bg_img, w, h)
        self.update()

    def on_theme_changed(self) -> None:
        """主题切换钩子（widgets.notify_theme_changed 会遍历控件树调用它）。

        ★ task-09 复审打回的真缺陷：**切主题不会重建模糊背景缓存** ⇒ 深→浅时
          缓存里还是深色档（未做 Vibrance）的模糊图，盖上浅色遮罩后背景饱和度
          比正确值低 13~17%（QA 实测 0.2279 vs 0.2574/0.2752），用户第一眼就是
          「不通透/发霉」，必须换封面或 resize 才会自愈。
        修法（Lead 指定）：**复用已有的 200ms 去抖定时器**，只 start 不重算
        （钩子里绝不做重计算，避免切主题卡顿）；重建发生在去抖回调 _rebuild_bg_now。
        """
        try:
            self._bg_timer.start()
        except Exception:
            log.debug("主题切换后重启背景去抖失败", exc_info=True)
        self.update()

    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._layout_children()
        self._bg_timer.start()              # ★ 停稳 200ms 后才重建高清背景

    def _spec_body_top(self):
        """控制栏胶囊**实体**上沿在本控件坐标系里的 y（量实际几何；拿不到返回 None）。

        KB §9.65：不要建模，要量实际几何。胶囊是 GlassFrame（widgets.py:577），
        它的实体 = 控件矩形内缩 self._margin，而 self._margin = shadow_spread
        （widgets.py:610，构造参数存成了属性，语义=阴影外框厚度；ControlBar 传的是
        P["float_shadow_spread"]，实测 2）。所以「实体上沿」= 胶囊控件 y + _margin。
        ⚠️ 不能用 theme 的 M["capsule_shadow"]=16 去估：那 16 只是**宿主布局**的
        左右外边距，不是胶囊实体的内缩量（Lead 实测：控件盒 y=618、实体上沿 y=620）。
        """
        try:
            st = self.parentWidget()
            cap = getattr(st, "capsule", None)
            if cap is None or not cap.isVisible() or not cap.width():
                return None
            # ⚠️ 胶囊是 AudioView 的**兄弟**（挂在 Stage.capsule_host 里），不是本控件的
            #    子孙 —— QWidget.mapTo(ancestor) 要求传的是祖先，传兄弟会返回错误结果
            #    （实测窗口态直接吐回全局坐标 795，把 spec_y 算成 733）。必须走全局坐标换算。
            top = float(cap.mapToGlobal(QPoint(0, 0)).y() - self.mapToGlobal(QPoint(0, 0)).y())
            # 实体上沿：优先用 GlassFrame 自己的公开 API body_rect()（widgets.py:672），
            # 它内部就是 rect 内缩 self._margin；没有该 API 的旧控件退回读 _margin。
            body = getattr(cap, "body_rect", None)
            if callable(body):
                return top + float(body().y())
            return top + float(getattr(cap, "_margin", 0.0) or 0.0)
        except Exception:
            # ★ R1（Lead 追加条款）：主路径失败必须**留痕**，不能静默回落到旧公式 ——
            #   QA 实测过：注入抛异常的 body_rect，旧代码无日志无异常地回落到 576
            #   （间隙 −4px），主路径将来回归没人发现。每实例只打一次，避免刷屏。
            if not self._spec_body_top_warned:
                self._spec_body_top_warned = True
                log.warning("控制栏实体几何读取失败，频谱位置回落到常量公式", exc_info=True)
            return None

    def _spec_y_for(self, h: int) -> int:
        """频谱 y：底边落在**量出来的**控制栏实体上沿之上 SPEC_GAP_TO_BAR。

        主路径（胶囊可用）：spec_y = 胶囊实体上沿 − SPEC_H − SPEC_GAP_TO_BAR
        回落路径（胶囊未挂载/不可见）：用户给定公式 h − CONTROL_BAR_H − SPEC_H − SPEC_GAP_TO_BAR
        两条路径共用同一个 SPEC_GAP_TO_BAR，不散落魔数。
        """
        body_top = self._spec_body_top()
        if body_top is None:
            return max(0, int(h) - CONTROL_BAR_H - SPEC_H - SPEC_GAP_TO_BAR)
        return max(0, int(round(body_top)) - SPEC_H - SPEC_GAP_TO_BAR)

    def _layout_children(self):
        w, h = self.width(), self.height()
        sr = self.scale_ratio()
        content_h = max(120, h - CONTROL_BAR_H - SPEC_H - 40)
        # ★★ v1.0.34-3：频谱 y **量实际几何**（胶囊实体上沿 − SPEC_H − 16）。
        #    旧写法 spec_y = h-72-48-16 是"建模"：它假设控制栏实体从 h-120 开始，
        #    而真机上胶囊实体上沿在 h-92（=618+2），于是频谱矩形底边 624 压进实体 4px，
        #    柱体底部被盖住、8~44px 落差实际只剩 4~40px（Lead 逐行像素实测：行 620 起
        #    彩色柱体像素 = 0）。现在窗口/全屏都按实测重算，不缓存。
        spec_y = self._spec_y_for(h)
        self.spec.setGeometry(SPEC_SIDE, spec_y, max(80, w - SPEC_SIDE * 2), SPEC_H)
        try:
            self.spec.raise_()               # ★ 强制置顶
        except Exception:
            pass
        col_w = max(360, min(int(w * 0.78), w - 40))
        x0 = (w - col_w) // 2
        left_w = int(col_w * 0.40)
        right_w = max(220, col_w - left_w - 32)
        base = int(260 * sr)
        side = max(150, min(int(left_w * 0.86), int(content_h * 0.72), base))
        # ★ v1.0.36（task-07）：**可见封面边长仍是 side**（窗口 231 / 全屏 346），
        #   控件几何按 side + 2*14 下发 —— 那 14px 走廊用来画 3 圈阴影。
        #   居中算式两处都用 cov ⇒ 可见封面的中心与改动前完全一致（不会整体位移）。
        cov = side + COVER_PAD * 2
        self.cover.setGeometry(x0 + (left_w - cov) // 2, max(20, (content_h - cov) // 2),
                               cov, cov)
        rx = x0 + left_w + 32
        self.lyrics.setGeometry(rx, int(content_h * 0.22), right_w,
                                max(120, content_h - int(content_h * 0.22) - 8))
        self._right_col = (rx, right_w)
        # ★ task-10：规格胶囊摆在**封面下沿右侧**（卡片给的第二个位置）——
        #   与封面（下方）、歌词（右侧另一栏）、频谱（底部）、控制栏天然不相交；
        #   右对齐到封面右沿，留白 BADGE_BELOW_COVER_GAP。文本为空则整枚隐藏。
        try:
            bsz = self.badge.hint_size()
            if bsz.width() > 0:
                cover_x = x0 + (left_w - cov) // 2
                bx = cover_x + cov - bsz.width()
                by = max(20, (content_h - cov) // 2) + cov + BADGE_BELOW_COVER_GAP
                by = min(by, max(0, h - CONTROL_BAR_H - bsz.height() - 4))
                self.badge.setGeometry(max(0, bx), max(0, by), bsz.width(), bsz.height())
                self.badge.raise_()
        except Exception:
            log.debug("规格胶囊布局失败", exc_info=True)
        self._sync_badge_state()

    def paintEvent(self, _ev):            # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        light = _is_light()
        # ★★ 铁律：绘制期只有一行贴图（缓存模糊背景 + FastTransformation 拉伸）
        if not self._bg_cache.isNull():
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.drawPixmap(QRect(0, 0, w, h), self._bg_cache, QRect(self._bg_cache.rect()))
        else:
            grad = QLinearGradient(0, 0, 0, h)
            if light:
                grad.setColorAt(0.0, QColor("#F2F5F8"))
                grad.setColorAt(1.0, QColor("#E6ECF2"))
            else:
                grad.setColorAt(0.0, QColor("#1E222A"))
                grad.setColorAt(1.0, QColor("#14161B"))
            p.fillRect(QRect(0, 0, w, h), QBrush(grad))
        # ★★ task-09：浅色遮罩从 199（0.78）降到 82（0.32）—— 原来接近纯白，
        #    把封面色彩整片漂白；深色 173 保持不变（卡片要求深色不动）。
        p.fillRect(QRect(0, 0, w, h),
                   QColor(245, 247, 250, LIGHT_SCRIM_A) if light
                   else QColor(14, 16, 20, 173))
        rx, rw = getattr(self, "_right_col", (0, w))
        sr = self.scale_ratio()
        if self._title:
            f = QFont()
            f.setPixelSize(max(14, int(23 * sr)))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(24, 28, 36) if light else QColor(245, 247, 250))
            p.drawText(QRectF(rx, 40, rw, 34), Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(self._title, Qt.ElideRight, rw))
        if self._sub:
            f2 = QFont()
            f2.setPixelSize(max(11, int(14 * sr)))
            p.setFont(f2)
            p.setPen(QColor(70, 78, 90, 210) if light else QColor(200, 208, 220, 190))
            p.drawText(QRectF(rx, 78, rw, 24), Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(self._sub, Qt.ElideRight, rw))
        p.end()
