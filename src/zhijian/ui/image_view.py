"""独立 GPU 图片查看视口 —— 图片不再经过 libmpv。

架构依据：《看图引擎底层架构逆向与技术方案调研报告》确证的工业级标准
（参考实现用 WIC 双路解码 + D2D 合成 + 独立取景框；本项目对应
`QImageReader` 两级异步解码 + `QOpenGLWidget` 下的 `QPainter` 硬件纹理映射）。

三条绝对红线（用户指令）：
  1. **媒体路由彻底分流** —— 不向 libmpv 发送任何图片加载/变换指令；
  2. **零全局事件劫持** —— 拖拽/切图/热区只在自身几何范围内按标准 Qt 分发流转，
     底部控制栏按钮拥有绝对优先响应权；
  3. **纯 GPU 渲染** —— 继承 `QOpenGLWidget`，`QPainter` 在 GL 上下文下走硬件纹理映射，
     不做 CPU 软光栅重采样。

模块构成：
  * `ImageViewerWidget` —— 视口主体（光标锚定缩放 / 长按平移 / 弹簧平滑 / 两级解码）
  * `MinimapNavigator`   —— 右上角交互式缩略图取景框（正向+反向映射）
  * `HotspotZone`        —— 左右全高热区（悬停淡入圆形箭头，单击切图）
"""
from __future__ import annotations

import logging
import math
import os
import time

from PySide6.QtCore import (Property, QObject, QPoint, QPointF, QRectF, QRunnable, QSize, Qt,
                            QThreadPool, QTimer, Signal)
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

log = logging.getLogger("zhijian.ui.imageview")

#: 支持的图片扩展名（媒体路由用它判断走 Qt 原生视口还是 mpv）
IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif",
              ".avif", ".tif", ".tiff", ".ico", ".svg", ".pbm", ".pgm", ".ppm"}

SCALE_MIN, SCALE_MAX = 0.1, 10.0      # 规范：10% ~ 1000%
SPRING_MS = 16                        # 弹簧时钟
SPRING_TAU = 0.045                    # 时间常数（16ms 下逼近系数 ≈ 0.30，落在建议区间 0.25~0.35）
SPRING_EPS = 0.001                    # 收敛阈值（达到即停表，空闲 CPU 0 开销）


def is_image(path: str) -> bool:
    """媒体路由判据：这个路径该交给 Qt 原生视口吗？"""
    return os.path.splitext(str(path or ""))[1].lower() in IMAGE_EXTS


# ---------------------------------------------------------------------------
# 两级异步解码（对标报告里的 LoadNavieImage：先轻量底图，再全分辨率替换）
# ---------------------------------------------------------------------------
class _DecodeSignals(QObject):
    staged = Signal(str, QImage, int)     # path, image, stage(1=底图 / 2=原图)


class _DecodeTask(QRunnable):
    """后台解码任务：阶段一按视口尺寸缩放解码，阶段二读全分辨率。"""

    def __init__(self, path: str, view_size: QSize, sig: _DecodeSignals, gen: int):
        super().__init__()
        self._path = path
        self._view = QSize(max(16, view_size.width()), max(16, view_size.height()))
        self._sig = sig
        self._gen = gen
        self.setAutoDelete(True)

    def run(self):                        # noqa: D102
        try:
            # ---- 阶段一：轻量底图（0ms 级上屏，先消黑场）----
            rd = QImageReader(self._path)
            rd.setAutoTransform(True)
            sz = rd.size()
            if sz.isValid() and sz.width() > 0:
                # 底图按视口的 2 倍解（留出放大余量，避免一放大就糊）
                rd.setScaledSize(sz.scaled(self._view * 2, Qt.KeepAspectRatio))
            staged = None
            img = rd.read()
            if img is not None and not img.isNull():
                staged = img
                self._sig.staged.emit(self._path, img, 1)
            # ---- 阶段二：全分辨率原图（就绪后无缝替换纹理，不动视口偏移）----
            full = None
            try:
                rd2 = QImageReader(self._path)
                rd2.setAutoTransform(True)
                full = rd2.read()
            except Exception:
                full = None
            if full is not None and not full.isNull():
                self._sig.staged.emit(self._path, full, 2)
            elif staged is not None:
                # ⚠️ 阶段二失败（例如 Qt 没有该格式插件：实测 AVIF 解不出来）
                #    ⇒ 必须**用底图兜底并标记为就绪**，否则界面永远停在"未就绪"，
                #      取景框/信息面板也拿不到全图状态。
                self._sig.staged.emit(self._path, staged, 2)
        except Exception:
            log.debug("图片解码失败: %s", self._path, exc_info=True)


# ---------------------------------------------------------------------------
# 左右热区（规范 §4）
# ---------------------------------------------------------------------------
class HotspotZone(QWidget):
    """全高半透明热区：悬停淡入圆形翻页箭头，单击切上/下一张。

    ⚠️ 事件**严格在本控件几何范围内被捕获**（Qt 标准分发），不向下穿透到视口，
       因此不会误触发拖拽平移；底栏与其它按钮的优先级不受影响（本控件不在其上方）。
    """

    clicked = Signal(int)                 # -1 上一张 / +1 下一张

    def __init__(self, side: int, parent=None):
        super().__init__(parent)
        self._side = -1 if side < 0 else 1
        self.setObjectName("HotspotZone")
        self.setFixedWidth(72)            # 规范：60~80px
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self._t = 0.0                     # 悬停淡入进度 0..1
        self._anim = None
        self._pressed = False

    # 动画属性（用 QPropertyAnimation 驱动淡入淡出，避免自建定时器）
    def get_t(self) -> float:
        return self._t

    def set_t(self, v: float):
        self._t = max(0.0, min(1.0, float(v)))
        self.update()

    fadeT = Property(float, get_t, set_t)

    def _animate(self, to: float):
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation

        if self._anim is None:
            self._anim = QPropertyAnimation(self, b"fadeT", self)
            self._anim.setDuration(160)
            self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(float(to))
        self._anim.start()

    def enterEvent(self, ev):             # noqa: N802
        self._animate(1.0)
        super().enterEvent(ev)

    def leaveEvent(self, ev):             # noqa: N802
        self._animate(0.0)
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):        # noqa: N802
        if ev.button() == Qt.LeftButton:
            self._pressed = True
            ev.accept()                   # 捕获：不穿透到视口
            return
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):      # noqa: N802
        if self._pressed and ev.button() == Qt.LeftButton:
            self._pressed = False
            self.clicked.emit(self._side)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def paintEvent(self, _ev):            # noqa: N802
        if self._t <= 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect())
        # 侧向渐变高亮（越靠边越亮），再叠一个圆形箭头
        from PySide6.QtGui import QLinearGradient

        g = (QLinearGradient(r.topLeft(), r.topRight()) if self._side < 0
             else QLinearGradient(r.topRight(), r.topLeft()))
        c = QColor(255, 255, 255, int(38 * self._t))
        g.setColorAt(0.0, c)
        c2 = QColor(255, 255, 255, 0)
        g.setColorAt(1.0, c2)
        p.fillRect(r, g)
        # 圆形箭头底 + 箭头
        d = 46.0
        cx = r.left() + (r.width() - d) / 2.0 if self._side < 0 else r.right() - (r.width() + d) / 2.0
        cy = r.center().y() - d / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, int(96 * self._t)))
        p.drawEllipse(QRectF(cx, cy, d, d))
        pen = QPen(QColor(255, 255, 255, int(235 * self._t)))
        pen.setWidthF(2.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        m = d * 0.22
        ax, ay = cx + d / 2.0, cy + d / 2.0
        s = -1.0 if self._side < 0 else 1.0
        path = QPainterPath()
        path.moveTo(ax + s * m, ay - m)
        path.lineTo(ax - s * m, ay)
        path.lineTo(ax + s * m, ay + m)
        p.drawPath(path)
        p.end()


# ---------------------------------------------------------------------------
# 交互式缩略图取景框（规范 §3）
# ---------------------------------------------------------------------------
class MinimapNavigator(QWidget):
    """右上角取景框：显示整图缩略图 + 当前视野框，可点击/拖拽反向居中。

    映射（规范给定）：
      正向 u=(vis.x-R.x)/R.w, v=(vis.y-R.y)/R.h, uw=vis.w/R.w, vh=vis.h/R.h
      反向 ox=(0.5-nu)*R.w,    oy=(0.5-nv)*R.h
    """

    pan_request = Signal(float, float)    # 目标归一化中心 (nu, nv)

    W, H = 160, 100                       # 规范建议尺寸

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Minimap")
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._img: QImage | None = None   # **共享**主视口的 QImage 引用（零成本，不二次解码）
        self._view = (0.0, 0.0, 1.0, 1.0)
        self._drag = None
        self._t = 0.0                     # 淡入淡出
        self._anim = None
        self.setVisible(False)

    # -- 淡入淡出 ------------------------------------------------------------
    def get_t(self) -> float:
        return self._t

    def set_t(self, v: float):
        self._t = max(0.0, min(1.0, float(v)))
        self.setVisible(self._t > 0.01)
        self.update()

    fadeT = Property(float, get_t, set_t)

    def _animate(self, to: float):
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation

        if self._anim is None:
            self._anim = QPropertyAnimation(self, b"fadeT", self)
            self._anim.setDuration(180)
            self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(float(to))
        self._anim.start()

    # -- 数据 -----------------------------------------------------------------
    def set_image(self, img: QImage | None):
        """共享主视口的图像引用（规范：严禁二次解码）。"""
        self._img = img
        self.update()

    def set_view(self, rect):
        """rect = (u, v, uw, vh)（图片内归一化）。None ⇒ 全图可见。"""
        if not rect:
            self._view = (0.0, 0.0, 1.0, 1.0)
        else:
            u, v, uw, vh = [max(0.0, min(1.0, float(x))) for x in rect]
            uw = max(1e-3, min(1.0, uw))
            vh = max(1e-3, min(1.0, vh))
            self._view = (min(u, 1.0 - uw), min(v, 1.0 - vh), uw, vh)
        self.update()

    def update_visibility(self, zoomed: bool):
        """规范：图片 ≤100%（适应窗口）时淡出隐藏，放大后淡入。"""
        self._animate(1.0 if zoomed else 0.0)

    # -- 几何 -----------------------------------------------------------------
    def _canvas(self) -> QRectF:
        """缩略图在控件里的绘制矩形（等比 contain，留 1px 边框余量）。"""
        r = QRectF(1, 1, self.W - 2, self.H - 2)
        if self._img is None or self._img.isNull():
            return r
        iw, ih = self._img.width(), self._img.height()
        k = min(r.width() / max(1, iw), r.height() / max(1, ih))
        w, h = iw * k, ih * k
        return QRectF(r.left() + (r.width() - w) / 2.0, r.top() + (r.height() - h) / 2.0, w, h)

    # -- 绘制 -----------------------------------------------------------------
    def paintEvent(self, _ev):            # noqa: N802
        if self._t <= 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setOpacity(self._t)
        outer = QRectF(0.5, 0.5, self.W - 1.0, self.H - 1.0)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawRoundedRect(outer, 8.0, 8.0)
        c = self._canvas()
        if self._img is not None and not self._img.isNull():
            p.drawImage(c, self._img)
        # 视野框：正向映射
        u, v, uw, vh = self._view
        vr = QRectF(c.left() + u * c.width(), c.top() + v * c.height(),
                    uw * c.width(), vh * c.height())
        shade = QPainterPath()
        shade.addRect(c)
        hole = QPainterPath()
        hole.addRect(vr)
        p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 105))
        pen = QPen(QColor(255, 255, 255, 235))
        pen.setWidthF(1.4)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(vr)
        # 1px 细线边框
        pen2 = QPen(QColor(255, 255, 255, 60))
        pen2.setWidthF(1.0)
        p.setPen(pen2)
        p.drawRoundedRect(outer, 8.0, 8.0)
        p.end()

    # -- 反向映射（点击/拖拽居中）--------------------------------------------
    def _emit_center(self, pos):
        c = self._canvas()
        if c.width() <= 0 or c.height() <= 0:
            return
        nu = (float(pos.x()) - c.left()) / c.width()
        nv = (float(pos.y()) - c.top()) / c.height()
        self.pan_request.emit(max(0.0, min(1.0, nu)), max(0.0, min(1.0, nv)))

    def mousePressEvent(self, ev):        # noqa: N802
        if ev.button() == Qt.LeftButton:
            self._drag = True
            self._emit_center(ev.position())
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):         # noqa: N802
        if self._drag:
            self._emit_center(ev.position())
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):      # noqa: N802
        if self._drag:
            self._drag = None
            ev.accept()
            return
        super().mouseReleaseEvent(ev)


# ---------------------------------------------------------------------------
# 主视口
# ---------------------------------------------------------------------------
class ImageViewerWidget(QOpenGLWidget):
    """Qt 原生 GPU 图片视口（`QOpenGLWidget` + `QPainter` 硬件纹理映射）。

    状态模型（规范 §1）：
      * `_k_fit`        —— 适应窗口时 图片像素 → 控件像素 的基准比
      * `_target_scale` —— 目标缩放（滚轮/滑条瞬时更新，[0.1, 10.0] 截断）
      * `_scale`        —— 当前**渲染**缩放（由弹簧时钟逼近 target）
      * `_off`          —— 视口中心偏移（控件像素）
    """

    scale_changed = Signal(float)         # 缩放变化（供滑条静默同步）
    image_ready = Signal(str, bool)       # path, 是否全分辨率就绪

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ImageViewer")
        # 红线 3：不透明绘制 + GL 上下文下的 QPainter（硬件纹理映射，非软光栅）
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.OpenHandCursor)

        # ---- 几何状态 ----
        self._k_fit = 1.0
        self._target_scale = 1.0
        self._scale = 1.0
        self._off = QPointF(0.0, 0.0)

        # ---- 图像 ----
        self._path = ""
        self._src: QImage | None = None       # 原图（未变换）
        self._img: QImage | None = None       # 绘制用（已应用旋转/镜像）
        self._rotation = 0
        self._flip = False
        self._full = False                    # 是否已是全分辨率
        self._gen = 0                         # 解码代号（丢弃过期结果）
        self._error = ""

        # ---- 弹簧时钟（空闲即停 ⇒ CPU 0 开销）----
        self._spring = QTimer(self)
        self._spring.setInterval(SPRING_MS)
        self._spring.timeout.connect(self._spring_tick)
        self._last_tick = time.perf_counter()

        # ---- 子控件：取景框 + 左右热区（都在视口几何范围内，标准事件分发）----
        self.minimap = MinimapNavigator(self)
        self.minimap.pan_request.connect(self._on_minimap_pan)
        self.zone_left = HotspotZone(-1, self)
        self.zone_right = HotspotZone(1, self)
        self.zone_left.clicked.connect(lambda _s: self.step_image.emit(-1))
        self.zone_right.clicked.connect(lambda _s: self.step_image.emit(1))
        for z in (self.zone_left, self.zone_right):
            z.setVisible(True)

        # ---- 两级解码 ----
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._sig = _DecodeSignals()
        self._sig.staged.connect(self._on_stage_ready)

        self._drag = None

    # ================================================================== 对外
    step_image = Signal(int)              # 热区请求切图（-1/+1）

    def path(self) -> str:
        return self._path

    def has_image(self) -> bool:
        return self._img is not None

    def error(self) -> str:
        return self._error

    def scale(self) -> float:
        return self._scale

    def target_scale(self) -> float:
        return self._target_scale

    def is_full_res(self) -> bool:
        return self._full

    # ---- 加载 / 生命周期 --------------------------------------------------
    def load(self, path: str, reset: bool = True):
        """切图入口：立即用尺寸清黑场，随后两级异步解码。

        生命周期（规范 §6）：默认**复位**为适应窗口（`_target_scale=_scale=1.0`、
        `_off=0`），镜像强制归零；`image.keep_view_on_switch` 打开时保留缩放。
        """
        self._error = ""
        self._path = str(path or "")
        self._full = False
        self._gen += 1
        gen = self._gen
        if reset:
            self._target_scale = 1.0
            self._scale = 1.0
            self._off = QPointF(0.0, 0.0)
            self._rotation = 0
            self._flip = False
        # 阶段零：快速取尺寸（主线程，微秒级），先把几何算好、清掉黑场
        try:
            rd = QImageReader(self._path)
            rd.setAutoTransform(True)
            sz = rd.size()
            if sz.isValid() and sz.width() > 0:
                self._src = None
                self._img = None
                self._natural = (sz.width(), sz.height())
                self._recompute_fit()
                self.update()
        except Exception:
            log.debug("取图片尺寸失败: %s", self._path, exc_info=True)
        # 阶段一/二：后台解码
        self._pool.start(_DecodeTask(self._path, QSize(max(1, self.width()), max(1, self.height())),
                                     self._sig, gen))
        return True

    def clear(self):
        self._src = self._img = None
        self._path = ""
        self._gen += 1
        self.reset_view()
        self.update()

    def reset_view(self):
        """复位为「适应窗口」。"""
        self._target_scale = 1.0
        self._scale = 1.0
        self._off = QPointF(0.0, 0.0)
        self._spring.stop()
        self.update()
        self.scale_changed.emit(self._scale)
        self._sync_minimap()

    # ---- 旋转 / 镜像 ------------------------------------------------------
    def set_rotation(self, deg: int):
        self._rotation = int(deg) % 360
        self._apply_transform()
        self.reset_view()

    def rotation(self) -> int:
        return self._rotation

    def set_flip(self, on: bool):
        self._flip = bool(on)
        self._apply_transform()
        self.reset_view()

    def flip(self) -> bool:
        return self._flip

    def _apply_transform(self):
        if self._src is None:
            self._img = None
            return
        if self._rotation or self._flip:
            tf = QTransform()
            tf.rotate(self._rotation)
            if self._flip:
                tf.scale(-1.0, 1.0)
            self._img = self._src.transformed(tf, Qt.SmoothTransformation)
        else:
            self._img = self._src
        self._recompute_fit()

    # ---- 缩放（光标锚定）--------------------------------------------------
    def zoom_by(self, factor: float, anchor: QPointF | None = None):
        self.set_target_scale(self._target_scale * float(factor), anchor)

    def zoom_at_cursor(self, factor: float):
        """朝**当前光标**缩放（滚轮/按钮统一入口）。"""
        pos = self.mapFromGlobal(self.cursor().pos())
        if not self.rect().contains(pos):
            pos = QPoint(self.width() // 2, self.height() // 2)
        self.zoom_by(factor, QPointF(pos))

    def set_target_scale(self, value: float, anchor: QPointF | None = None):
        """设置目标缩放（规范 §2：与渲染值解耦，由弹簧时钟追随）。

        锚定公式（规范 §1.2）：
            ox_new = cx - VW/2 - α*(cx - VW/2 - ox_old)
            oy_new = cy - VH/2 - α*(cy - VH/2 - oy_old)
        其中 α = s_new / s_old；锚点给定时**立即**用目标尺度预置偏移，
        这样弹簧追赶过程中光标所指的图像细节始终对齐（不会先跳再回弹）。
        """
        new = max(SCALE_MIN, min(SCALE_MAX, float(value)))
        old_target = self._target_scale
        if abs(new - old_target) < 1e-9:
            return
        if anchor is not None and old_target > 0:
            alpha = new / old_target
            vw, vh = float(self.width()), float(self.height())
            cx, cy = float(anchor.x()), float(anchor.y())
            ox, oy = self._off.x(), self._off.y()
            self._off = QPointF(cx - vw / 2.0 - alpha * (cx - vw / 2.0 - ox),
                                cy - vh / 2.0 - alpha * (cy - vh / 2.0 - oy))
        self._target_scale = new
        self._clamp_off()
        if not self._spring.isActive():
            self._last_tick = time.perf_counter()
            self._spring.start()

    # ---- 弹簧时钟 ---------------------------------------------------------
    def _spring_tick(self):
        now = time.perf_counter()
        dt = max(0.001, min(0.1, now - self._last_tick))
        self._last_tick = now
        # 真实 dt 的一阶逼近（τ 固定 ⇒ 帧率无关；16ms 下系数 ≈0.30，落在建议 0.25~0.35）
        factor = 1.0 - math.exp(-dt / SPRING_TAU)
        diff = self._target_scale - self._scale
        if abs(diff) < SPRING_EPS:
            self._scale = self._target_scale
            self._spring.stop()           # 空闲即停 ⇒ CPU 0 开销
        else:
            self._scale += diff * factor
        self._clamp_off()
        self.update()
        self.scale_changed.emit(self._scale)
        self._sync_minimap()

    # ---- 几何 -------------------------------------------------------------
    def _recompute_fit(self):
        if self._img is None:
            return
        iw, ih = max(1, self._img.width()), max(1, self._img.height())
        vw, vh = max(1, self.width()), max(1, self.height())
        self._k_fit = min(vw / iw, vh / ih)

    def image_rect(self) -> QRectF:
        """图片当前在控件坐标里的矩形（含渲染缩放与偏移）。"""
        if self._img is None:
            return QRectF()
        k = self._k_fit * self._scale
        w, h = self._img.width() * k, self._img.height() * k
        cx = self.width() / 2.0 + self._off.x()
        cy = self.height() / 2.0 + self._off.y()
        return QRectF(cx - w / 2.0, cy - h / 2.0, w, h)

    def view_rect(self):
        """可见部分在图片内的归一化比例 (u, v, uw, vh)（规范 §3 正向映射）。"""
        r = self.image_rect()
        if r.isEmpty():
            return None
        vis = r.intersected(QRectF(self.rect()))
        if vis.isEmpty():
            return None
        return ((vis.x() - r.x()) / r.width(), (vis.y() - r.y()) / r.height(),
                vis.width() / r.width(), vis.height() / r.height())

    def _clamp_off(self):
        """边界安全夹取：放大时不让画面脱出视野；缩到比视口小时居中。

        ⚠️⚠️ 夹取界限必须按**目标尺度**算，不能按当前渲染尺度 ——
        弹簧动画的中间帧尺度更小，用它会算出 max_x=0（图片"比视口小"），
        于是每一帧都把 `_off` 强行归中 ⇒ 光标锚点在动画过程中被拉走，
        收敛后偏出几十像素（用户要求"严丝合缝、无跳动"，这是必须修的一环）。
        """
        if self._img is None:
            self._off = QPointF(0.0, 0.0)
            return
        k = self._k_fit * max(self._scale, self._target_scale)
        if k <= 0:
            return
        w = self._img.width() * k
        h = self._img.height() * k
        max_x = max(0.0, (w - self.width()) / 2.0)
        max_y = max(0.0, (h - self.height()) / 2.0)
        self._off = QPointF(max(-max_x, min(max_x, self._off.x())),
                            max(-max_y, min(max_y, self._off.y())))

    # ---- 取景框反向映射 ---------------------------------------------------
    def _on_minimap_pan(self, nu: float, nv: float):
        r = self.image_rect()
        if r.isEmpty():
            return
        # 规范：ox=(0.5-nu)*R.w —— 让 (nu,nv) 落到视口中心
        self._off = QPointF(-(nu - 0.5) * r.width(), -(nv - 0.5) * r.height())
        self._clamp_off()
        self.update()
        self._sync_minimap()

    def _sync_minimap(self):
        self.minimap.set_image(self._img)
        self.minimap.set_view(self.view_rect())
        self.minimap.update_visibility(self._target_scale > 1.001 or self._scale > 1.001)

    # ---- 两级解码回调 -----------------------------------------------------
    def _on_stage_ready(self, path: str, img: QImage, stage: int):
        if path != self._path:
            return                        # 过期结果（已切图）丢弃
        if stage == 1 and self._full:
            return                        # 已经有全图了，底图不要覆盖
        self._src = img
        self._full = (stage == 2)
        self._apply_transform()           # 会重算 _k_fit
        self.update()                     # 不动 _off / _scale（规范：无缝替换纹理）
        self._sync_minimap()
        self.image_ready.emit(path, self._full)

    # ================================================================== 绘制
    def paintEvent(self, _ev):            # noqa: N802
        # QOpenGLWidget + QPainter：Qt 会把绘制命令走 GL 纹理映射（硬件加速）
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0B0B0C"))
        img = self._img
        if img is None:
            p.end()
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        r = self.image_rect()
        vis = r.intersected(QRectF(self.rect()))
        if not vis.isEmpty() and r.width() > 0 and r.height() > 0:
            # 只画可见部分（放大时避免整图合成）
            sx = (vis.x() - r.x()) / r.width() * img.width()
            sy = (vis.y() - r.y()) / r.height() * img.height()
            sw = vis.width() / r.width() * img.width()
            sh = vis.height() / r.height() * img.height()
            p.drawImage(vis, img, QRectF(sx, sy, sw, sh))
        p.end()

    # ================================================================== 事件
    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._recompute_fit()
        # 子控件布局：取景框右上角；热区左右全高（**只在自己的几何范围内**响应）
        self.minimap.move(max(8, self.width() - self.minimap.width() - 12), 12)
        self.zone_left.setGeometry(0, 0, self.zone_left.width(), self.height())
        self.zone_right.setGeometry(self.width() - self.zone_right.width(), 0,
                                    self.zone_right.width(), self.height())
        self.zone_left.raise_()
        self.zone_right.raise_()
        self.minimap.raise_()             # 取景框在热区之上（否则拖框会先撞热区）
        self._clamp_off()
        self._sync_minimap()

    def wheelEvent(self, ev):             # noqa: N802
        dy = ev.angleDelta().y()
        if not dy:
            ev.ignore()
            return
        self.zoom_by(1.12 if dy > 0 else 1 / 1.12, ev.position())
        ev.accept()

    def mousePressEvent(self, ev):        # noqa: N802
        # 长按左键平移（仅在本视口收到事件时；子控件已各自捕获，不会传到这里）
        if ev.button() == Qt.LeftButton and self._img is not None:
            self._drag = QPointF(ev.position())
            self.setCursor(Qt.ClosedHandCursor)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):         # noqa: N802
        if self._drag is not None:
            d = QPointF(ev.position()) - self._drag
            self._drag = QPointF(ev.position())
            self._off += d
            self._clamp_off()
            self.update()
            self._sync_minimap()
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):      # noqa: N802
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)
