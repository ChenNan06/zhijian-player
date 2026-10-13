"""独立 GPU 图片查看视口 —— 图片不再经过 libmpv。

架构依据：《看图引擎底层架构逆向与技术方案调研报告》
  * §4.7 —— 对标 `Microsoft.UI.Composition` 的物理动画：**临界阻尼弹簧 + 速度交接**，
    而不是自写缓动曲线；"滚轮每格给目标值增量、显示值由弹簧追随"。
  * §3.1.2(b) —— 目标 B（Windows 11 照片）用
    `SpringScalarNaturalMotionAnimation` + `AnimationController`，
    缩放/平移/边界都由**同一个弹簧**积分产生（撞边界只换 target，不写回弹曲线）。
  * 两级解码 —— 先出轻量底图消黑场，再按需替换全分辨率纹理。

三条绝对红线（用户指令）：
  1. 媒体路由彻底分流：不向 libmpv 发送任何图片加载/变换指令；
  2. 零全局事件劫持：拖拽/切图/热区只在自身几何范围内按标准 Qt 分发流转；
  3. 纯 GPU 渲染：`QOpenGLWidget` + `QPainter` 硬件纹理映射，不做 CPU 软光栅重采样。

模块构成：
  * `ImageViewerWidget` —— 视口主体（锚定缩放 / 惯性平移 / 弹簧平滑 / 两级按需解码）
  * `MinimapNavigator`   —— 右上角交互式取景框（正向 + 反向映射）
  * `HotspotZone`        —— 左右全高热区（悬停淡入箭头，单击切图）
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time

from PySide6.QtCore import (Property, QObject, QPoint, QPointF, QRectF, QRunnable, QSize, Qt,
                            QThreadPool, QTimer, Signal)
from PySide6.QtGui import (QColor, QImage, QImageReader, QLinearGradient, QPainter, QPainterPath,
                           QPen, QTransform)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

log = logging.getLogger("zhijian.ui.imageview")

#: 支持的图片扩展名（媒体路由用它判断走 Qt 原生视口还是 mpv）
IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif",
              ".avif", ".tif", ".tiff", ".ico", ".svg", ".pbm", ".pgm", ".ppm"}

SCALE_MIN, SCALE_MAX = 0.1, 10.0        # 规范：10% ~ 1000%
WHEEL_STEP = 1.15                       # #1/#2 单格滚轮的缩放倍率（用户指定 1.15）
DRAG_MARGIN_PX = 28.0                   # 适应窗口时也允许拖动的安全边距
#: v1.0.21 「无限自由画布」：只要屏内还留有这么多像素的图片边缘，就允许拖到视口之外
FREE_PAN_MARGIN_PX = 60.0
FULL_DEBOUNCE_MS = 300                  # 原图解码防抖（用户规范 v1.0.22：停稳 300ms）
#: 只有**手动放大超过适应窗口**这个倍数才允许拉原图（切图浏览阶段 100% 走轻量底图）
FULL_MIN_SCALE = 1.05

# ---- 动画参数（对标 WinUI SpringScalarNaturalMotionAnimation 的两个参数）----
SPRING_MS = 16                          # 时钟周期
SPRING_PERIOD_S = 0.26                  # Period（ω0 = 2π/Period）
SPRING_ZETA = 1.0                       # DampingRatio：1.0 = 临界阻尼（**不过冲、无回弹**）
SPRING_EPS = 0.0015                     # 位置收敛阈值
SPRING_VEPS = 0.02                      # 速度收敛阈值

#: 底图按视口的多少倍解码（留给放大用的余量；超过它才需要全分辨率原图）
STAGE1_VIEW_FACTOR = 1.0
#: 内存缓存：**条目上限 5**（当前 + N-1/N+1/N+2 预加载）
STAGE_CACHE_MAX = 5
#: 字节预算（双上限，避免 5 张 4K 把内存拉爆）
STAGE_CACHE_BYTES = 96 * 1024 * 1024


class Spring1D:
    """一维**临界阻尼弹簧的解析解**（对标 `SpringScalarNaturalMotionAnimation`）。

    ⚠️⚠️ 为什么不用显式欧拉积分（我第一版就是那么写的，用户实测"有回弹"）：
        ω0 = 2π/Period ≈ 24 rad/s，而 GUI 定时器一帧 dt 可能到 16~50ms；
        显式欧拉在这个量级下**数值过冲** —— 弹簧本来 ζ=1 不该过冲，却因为积分误差
        冲过目标再荡回来，肉眼就是"回弹"。实测第一帧直接冲到 2.7（目标 2.2）。

    ζ = 1 的解析解（无条件稳定、**数学上不可能过冲**）：
        A = x0 - target,  B = v0 + ω·A
        x(t) = target + (A + B·t)·e^(-ω·t)
        v(t) = (B - ω·(A + B·t))·e^(-ω·t)
    """

    def __init__(self, period: float = SPRING_PERIOD_S, zeta: float = SPRING_ZETA):
        self.w0 = 2.0 * 3.141592653589793 / max(0.02, float(period))
        self.zeta = float(zeta)
        self.reset(0.0, 0.0, 0.0)

    def reset(self, x0: float, v0: float, target: float):
        self.x0 = float(x0)
        self.v0 = float(v0)
        self.target = float(target)
        self.t0 = None

    def start(self, now: float):
        self.t0 = float(now)

    def value(self, now: float):
        """返回 (x, v, done)。ζ=1 用解析解；ζ<1 用欠阻尼解析解。"""
        if self.t0 is None:
            return (self.target, 0.0, True)
        t = max(0.0, float(now) - self.t0)
        A = self.x0 - self.target
        w = self.w0
        if abs(self.zeta - 1.0) < 1e-6:
            B = self.v0 + w * A
            e = 2.718281828459045 ** (-w * t)
            x = self.target + (A + B * t) * e
            v = (B - w * (A + B * t)) * e
        else:                                   # 欠阻尼（zeta < 1）：会过冲，按需才用
            import math as _m

            z = self.zeta
            wd = w * _m.sqrt(max(1e-9, 1.0 - z * z))
            e = _m.exp(-z * w * t)
            c1 = A
            c2 = (self.v0 + z * w * A) / wd
            x = self.target + e * (c1 * _m.cos(wd * t) + c2 * _m.sin(wd * t))
            v = e * (-z * w * (c1 * _m.cos(wd * t) + c2 * _m.sin(wd * t))
                     + (-c1 * wd * _m.sin(wd * t) + c2 * wd * _m.cos(wd * t)))
        done = abs(x - self.target) < SPRING_EPS and abs(v) < SPRING_VEPS
        return (self.target if done else x, 0.0 if done else v, done)


def is_image(path: str) -> bool:
    """媒体路由判据：这个路径该交给 Qt 原生视口吗？"""
    return os.path.splitext(str(path or ""))[1].lower() in IMAGE_EXTS


# ---------------------------------------------------------------------------
# 两级异步解码（阶段一：视口尺寸底图；阶段二：**按需**全分辨率）
# ---------------------------------------------------------------------------
class _DecodeSignals(QObject):
    staged = Signal(str, QImage, int)     # path, image, stage(1=底图 / 2=原图)


def _pil_decode(path: str, want: "QSize | None" = None, draft: bool = True):
    """用 Pillow 解码（**解码期间释放 GIL**），失败返回 None（调用方回落到 Qt）。

    * `want` 给定时：先 `im.draft()` 让 libjpeg 做 **DCT 降采样**（1/2、1/4、1/8），
      再把结果缩放到精确的 `want` 尺寸 —— 这一步决定了"连切不卡"。
    * EXIF 方向用 `ImageOps.exif_transpose` 处理（等价于 Qt 的 setAutoTransform）。
    * 带透明通道的图保留 alpha（转 QImage.Format_RGBA8888）。
    """
    try:
        from PIL import Image, ImageOps
    except Exception:                       # 打包环境没带上 Pillow ⇒ 回落 Qt
        return None
    try:
        with Image.open(path) as im:
            # ⚠️⚠️ P0：**先按原图比例算等比 fit 尺寸**，绝不能直接缩到整个视口。
            #     v1.0.23 我写成 resize(视口宽, 视口高) ⇒ 竖图被拉成横版矮胖矩形
            #     （用户截图实证）。这里用 min 比例，且**永不放大**。
            def _fit(src_w: int, src_h: int):
                if want is None or want.width() <= 0 or want.height() <= 0:
                    return None
                if src_w <= 0 or src_h <= 0:
                    return None
                ratio = min(want.width() / float(src_w), want.height() / float(src_h), 1.0)
                return (max(1, int(src_w * ratio)), max(1, int(src_h * ratio)))

            if draft and want is not None:
                f0 = _fit(im.width, im.height)
                if f0 is not None:
                    try:
                        im.draft("RGB", f0)      # libjpeg DCT 降采样（等比目标）
                    except Exception:
                        pass
            im = ImageOps.exif_transpose(im) or im
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if has_alpha else "RGB")
            # EXIF 旋转会交换宽高 ⇒ 用变换后的尺寸重算一次等比目标
            f1 = _fit(im.width, im.height)
            if f1 is not None and (im.width, im.height) != f1:
                im = im.resize(f1, Image.BILINEAR)
            data = im.tobytes("raw", "RGBA" if has_alpha else "RGB")
            fmt = QImage.Format_RGBA8888 if has_alpha else QImage.Format_RGB888
            q = QImage(data, im.width, im.height, im.width * (4 if has_alpha else 3), fmt)
            return q.copy()                 # copy() 让 QImage 拥有自己的内存
    except Exception:
        log.debug("Pillow 解码失败，回落 Qt: %s", path, exc_info=True)
        return None


class _DecodeTask(QRunnable):
    """后台解码任务。

    ⚠️ 连续切图卡顿的根因是"每次切图都排一个全分辨率解码" —— 4K 图单次 100~300ms，
       连按方向键就会积压一串**已经过期**的重解码。
       现在：阶段一只解视口尺寸（快）；原图**按需**（放大到超过底图分辨率时才要），
       并且每个任务持有自己的 `abort` 事件，切图时**立刻置位**，
       过期任务在阶段之间就会主动退出，不再白跑。
    """

    def __init__(self, path: str, view_size: QSize, sig: _DecodeSignals, stage: int,
                 abort: threading.Event, gen: int = 0, gen_of=None):
        super().__init__()
        self._path = path
        self._view = QSize(max(16, view_size.width()), max(16, view_size.height()))
        self._sig = sig
        self._stage = stage
        self._abort = abort
        # #5 代数令牌：任务自己带一份"我是第几代"，解码前后各校验一次；
        #    切图时代数 +1，旧任务立刻作废（避免快速翻图时一堆过期解码塞死线程池）。
        self._gen = int(gen)
        self._gen_of = gen_of
        self.setAutoDelete(True)

    def _stale(self) -> bool:
        if self._abort.is_set():
            return True
        if self._gen_of is not None:
            try:
                return int(self._gen_of()) != self._gen
            except Exception:
                return True
        return False

    def run(self):                        # noqa: D102
        if self._stale():
            return
        try:
            if self._stage == 1:
                # ★ v1.0.23 路线 A：底图优先走 Pillow（C 层解码期间**释放 GIL**），
                #   并用 im.draft() 让 libjpeg 直接做 **DCT 降采样**（1/2、1/4、1/8）——
                #   解码量最多降到 1/16，主线程不再被后台解码拖死。
                want = QSize(self._view.width(), self._view.height())
                img = _pil_decode(self._path, want, draft=True)
                if img is None:
                    # 回落：Pillow 打不开的格式（svg/ico/cur/tga 等）仍由 Qt 解，
                    # 并保持 setScaledSize（Qt 对 JPEG 也能做整数倍降采样）。
                    rd = QImageReader(self._path)
                    rd.setAutoTransform(True)
                    sz = rd.size()
                    if sz.isValid() and sz.width() > 0:
                        scaled = sz.scaled(want, Qt.KeepAspectRatio)
                        if scaled.width() < sz.width():
                            rd.setScaledSize(scaled)
                    img = rd.read()
                if img is not None and not img.isNull() and not self._stale():
                    self._sig.staged.emit(self._path, img, 1)
                return
            full = _pil_decode(self._path, None, draft=False)
            if full is None:
                rd2 = QImageReader(self._path)      # 回落
                rd2.setAutoTransform(True)
                full = rd2.read()
            # 解码是长任务：**完成后再校验一次**代数，过期结果直接丢弃
            if full is not None and not full.isNull() and not self._stale():
                self._sig.staged.emit(self._path, full, 2)
        except Exception:
            log.debug("图片解码失败: %s", self._path, exc_info=True)


# ---------------------------------------------------------------------------
# 左右热区（规范 §4）
# ---------------------------------------------------------------------------
class HotspotZone(QWidget):
    """全高半透明热区：悬停淡入翻页箭头，单击切上/下一张。

    * 事件**严格在本控件几何范围内被捕获**（Qt 标准分发），不向下穿透到视口，
      因此不会误触发拖拽平移；底栏与其它按钮的优先级不受影响。
    * 视觉：只有**箭头**（带柔和暗晕保证任意画面上可读），
      **没有圆形底板**（用户明确要求去掉那圈"圆形遮罩"）。
    """

    clicked = Signal(int)                 # -1 上一张 / +1 下一张

    def __init__(self, side: int, parent=None):
        super().__init__(parent)
        self._side = -1 if side < 0 else 1
        self.setObjectName("HotspotZone")
        self.setFixedWidth(72)            # 规范：60~80px
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self._t = 0.0
        self._anim = None
        self._pressed = False

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
            self._anim.setDuration(150)
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
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):      # noqa: N802
        if self._pressed and ev.button() == Qt.LeftButton:
            self._pressed = False
            self.clicked.emit(self._side)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def arrow_points(self):
        """返回 (顶点, 上臂端点, 下臂端点) —— 绘制与验收判据**共用同一份几何**。

        ⚠️ 原来方向画反了（左热区的箭头指向右边）：`s` 的符号用在了错误的项上。
           现在的定义：顶点在**指向那一侧**，两条臂在反侧 ⇒
           左热区顶点 x < 臂 x（指向左），右热区顶点 x > 臂 x（指向右）。
        """
        r = QRectF(self.rect())
        cx = r.center().x()
        # 箭头**绝对居中**（50%）。看图模式下抽屉手柄已被隐藏，不再需要避让；
        # 左右两侧完全对称。
        cy = r.center().y()
        w = 9.0                            # 箭头的半高
        d = 6.5                            # 箭头的半深
        s = -1.0 if self._side < 0 else 1.0
        return (QPointF(cx + s * d, cy),
                QPointF(cx - s * d, cy - w),
                QPointF(cx - s * d, cy + w))

    def paintEvent(self, _ev):            # noqa: N802
        if self._t <= 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect())
        # 侧向渐隐高亮（只做"这里能点"的暗示，不画任何底板/圆形）
        g = (QLinearGradient(r.topLeft(), r.topRight()) if self._side < 0
             else QLinearGradient(r.topRight(), r.topLeft()))
        g.setColorAt(0.0, QColor(0, 0, 0, int(52 * self._t)))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(r, g)

        # 箭头（chevron）：**左热区指左、右热区指右**
        tip, a1, a2 = self.arrow_points()
        pp = QPainterPath()
        pp.moveTo(a1)
        pp.lineTo(tip)
        pp.lineTo(a2)
        # ① 先描一层加宽的半透明黑（柔和暗晕）—— 亮画面/暗画面上都读得清
        halo = QPen(QColor(0, 0, 0, int(120 * self._t)))
        halo.setWidthF(6.4)
        halo.setCapStyle(Qt.RoundCap)
        halo.setJoinStyle(Qt.RoundJoin)
        p.setPen(halo)
        p.setBrush(Qt.NoBrush)
        p.drawPath(pp)
        # ② 再描白色箭头本体
        pen = QPen(QColor(255, 255, 255, int(240 * self._t)))
        pen.setWidthF(2.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPath(pp)
        p.end()


# ---------------------------------------------------------------------------
# 交互式缩略图取景框（规范 §3）
# ---------------------------------------------------------------------------
class MinimapNavigator(QWidget):
    """右上角取景框：整图缩略图 + 当前视野框，可点击/拖拽反向居中。

    映射（规范给定）：
      正向 u=(vis.x-R.x)/R.w, v=(vis.y-R.y)/R.h, uw=vis.w/R.w, vh=vis.h/R.h
      反向 ox=(0.5-nu)*R.w,    oy=(0.5-nv)*R.h
    """

    pan_request = Signal(float, float)

    W, H = 160, 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Minimap")
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._img: QImage | None = None
        self._view = (0.0, 0.0, 1.0, 1.0)
        self._drag = None
        self._t = 0.0
        self._anim = None
        self.setVisible(False)

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

    def set_image(self, img: QImage | None):
        """共享主视口的图像引用（规范：严禁二次解码）。"""
        self._img = img
        self.update()

    def set_view(self, rect):
        if not rect:
            self._view = (0.0, 0.0, 1.0, 1.0)
        else:
            u, v, uw, vh = [max(0.0, min(1.0, float(x))) for x in rect]
            uw = max(1e-3, min(1.0, uw))
            vh = max(1e-3, min(1.0, vh))
            self._view = (min(u, 1.0 - uw), min(v, 1.0 - vh), uw, vh)
        self.update()

    def update_visibility(self, zoomed: bool):
        self._animate(1.0 if zoomed else 0.0)

    def _canvas(self) -> QRectF:
        r = QRectF(1, 1, self.W - 2, self.H - 2)
        if self._img is None or self._img.isNull():
            return r
        iw, ih = self._img.width(), self._img.height()
        k = min(r.width() / max(1, iw), r.height() / max(1, ih))
        w, h = iw * k, ih * k
        return QRectF(r.left() + (r.width() - w) / 2.0, r.top() + (r.height() - h) / 2.0, w, h)

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
        pen2 = QPen(QColor(255, 255, 255, 60))
        pen2.setWidthF(1.0)
        p.setPen(pen2)
        p.drawRoundedRect(outer, 8.0, 8.0)
        p.end()

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

    状态模型：
      * `_k_fit`        —— 适应窗口时 图片像素 → 控件像素 的基准比
      * `_target_scale` —— 目标缩放（滚轮/滑条瞬时更新，[0.1, 10.0] 截断）
      * `_scale`        —— 当前**渲染**缩放（**临界阻尼弹簧**积分输出）
      * `_off`          —— 视口中心偏移（控件像素）

    **锚定不变量**：一旦设定 `_anchor = (屏幕点, 图像归一化点)`，
    每帧在更新 `_scale` 之后**重算 `_off`**，使该图像点始终落回同一屏幕像素。
    这样弹簧追赶过程中画面不会"先跳到位再等缩放"，也就**没有回弹观感**。
    """

    scale_changed = Signal(float)
    viewport_resized = Signal()             # 视口尺寸变化 ⇒ 舞台重摆热区
    image_ready = Signal(str, bool)
    step_image = Signal(int)
    #: #4 只有**用户主动缩放**（滚轮/滑条/按钮）才发这个信号 ⇒ 用它驱动缩放 OSD。
    #: 内部复位走 `reset_view(silent=True)` / `load(reset=True)`，绝不发它。
    user_zoomed = Signal()
    load_failed = Signal(str, str)          # path, 原因（主窗口弹 OSD）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ImageViewer")
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.OpenHandCursor)

        self._k_fit = 1.0
        self._target_scale = 1.0
        self._scale = 1.0
        self._off = QPointF(0.0, 0.0)
        self._anchor = None                 # (ax, ay, au, av)

        self._path = ""
        self._src: QImage | None = None
        self._img: QImage | None = None
        self._rotation = 0
        self._flip = False
        self._ready = False                 # 至少已有可显示的图
        self._full = False                  # 是否已是全分辨率
        self._natural = (0, 0)              # 原图尺寸（不解码也能拿到）
        self._error = ""
        self._abort = threading.Event()
        self._full_pending = False
        self._load_generation = 0            # #5 代数令牌
        self.wheel_invert = False            # #1 配置项：滚轮方向反向
        self._minimap_pos = "bottom_right"   # #8 取景框位置
        # #5 全分辨率解码的防抖定时器：连续翻图时不触发，停稳 200ms 后才解原图
        self._full_timer = QTimer(self)
        self._full_timer.setSingleShot(True)
        self._full_timer.setInterval(FULL_DEBOUNCE_MS)
        self._full_timer.timeout.connect(self._do_request_full)

        # 弹簧时钟：同时驱动"缩放"与"平移惯性"（解析解，见 Spring1D 的说明）
        self._spr_scale = Spring1D()
        self._spr_off_x = Spring1D()
        self._spr_off_y = Spring1D()
        self._spring_v = 0.0
        self._off_v = QPointF(0.0, 0.0)
        self._off_target = QPointF(0.0, 0.0)
        self._timer = QTimer(self)
        self._timer.setInterval(SPRING_MS)
        self._timer.timeout.connect(self._tick)
        self._last_tick = time.perf_counter()

        self.minimap = MinimapNavigator(self)
        self.minimap.pan_request.connect(self._on_minimap_pan)
        self.zone_left = HotspotZone(-1, self)
        self.zone_right = HotspotZone(1, self)
        self.zone_left.clicked.connect(lambda _s: self.step_image.emit(-1))
        self.zone_right.clicked.connect(lambda _s: self.step_image.emit(1))

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        # ⚠️ 预解码必须走**独立线程池**：否则用户按方向键时，实时解码要排在
        #    预解码后面等线程（实测"下一张要 485ms"就是这个排队，而不是解码本身慢）。
        self._ppool = QThreadPool(self)
        self._ppool.setMaxThreadCount(1)
        self._pabort = threading.Event()
        # #5 原图解码**独立池、并发硬上限 1**：绝不让多张大图同时解码塞死多核
        self._fpool = QThreadPool(self)
        self._fpool.setMaxThreadCount(1)
        self._sig = _DecodeSignals()
        self._sig.staged.connect(self._on_stage_ready)

        self._neighbors: list[str] = []
        self._cache: dict[str, QImage] = {}
        self._cache_order: list[str] = []
        self._cache_bytes = 0
        self._drag = None
        self._drag_v = QPointF(0.0, 0.0)
        self._drag_last = None

    # ================================================================== 对外
    def path(self) -> str:
        return self._path

    def has_image(self) -> bool:
        return self._img is not None

    def is_ready(self) -> bool:
        return self._ready

    def is_full_res(self) -> bool:
        return self._full

    def natural_size(self) -> tuple[int, int]:
        return self._natural

    def scale(self) -> float:
        return self._scale

    def target_scale(self) -> float:
        return self._target_scale

    def rotation(self) -> int:
        return self._rotation

    def flip(self) -> bool:
        return self._flip

    def error(self) -> str:
        return self._error

    # ---- 底图缓存（字节预算 LRU）------------------------------------------
    def _cache_get(self, path: str):
        img = self._cache.get(path)
        if img is not None:
            try:
                self._cache_order.remove(path)
            except ValueError:
                pass
            self._cache_order.append(path)
        return img

    def _cache_put(self, path: str, img: QImage):
        if img is None or img.isNull() or path in self._cache:
            return
        nbytes = img.sizeInBytes()
        if nbytes > STAGE_CACHE_BYTES // 2:
            return                          # 单张就占掉半仓：不进缓存
        self._cache[path] = img
        self._cache_order.append(path)
        self._cache_bytes += nbytes
        while self._cache_bytes > STAGE_CACHE_BYTES and self._cache_order:
            old = self._cache_order.pop(0)
            victim = self._cache.pop(old, None)
            if victim is not None:
                self._cache_bytes -= victim.sizeInBytes()

    # ---- 加载 / 生命周期 --------------------------------------------------
    def load(self, path: str, reset: bool = True):
        """切图入口。

        * 主线程只用 `QImageReader.size()` 拿尺寸（微秒级）⇒ 立刻能算几何、清黑场；
        * 底图走缓存或后台解码；**原图按需**（放大超过底图分辨率时才请求）。
        """
        self._error = ""
        self._abort.set()                   # 让上一个任务立刻退出（连续切图不积压）
        self._abort = threading.Event()
        self._pabort.set()                  # 预解码也一起作废（它是"猜你要看哪张"）
        self._pabort = threading.Event()
        # #5 代数 +1：所有旧任务作废；并把线程池里**还没开始跑**的待办直接清掉
        self._load_generation += 1
        self._full_timer.stop()
        self._full_pending = False
        try:
            self._pool.clear()
        except Exception:
            pass
        self._path = str(path or "")
        self._ready = False
        self._full = False
        self._full_pending = False
        if reset:
            self._target_scale = 1.0
            self._scale = 1.0
            self._spring_v = 0.0
            self._off = QPointF(0.0, 0.0)
            self._off_target = QPointF(0.0, 0.0)
            self._off_v = QPointF(0.0, 0.0)
            self._rotation = 0
            self._flip = False
            self._anchor = None
            self._timer.stop()
        try:
            rd = QImageReader(self._path)
            rd.setAutoTransform(True)
            sz = rd.size()
            if sz.isValid() and sz.width() > 0:
                self._natural = (sz.width(), sz.height())
        except Exception:
            log.debug("取图片尺寸失败: %s", self._path, exc_info=True)

        # ⚠️⚠️ 先问一句"这台机器的 Qt 到底能不能解"。
        #    Qt 的图片插件是**有限集**（实测：无 avif / heic / jxl / jp2），
        #    而媒体路由的扩展名清单比它宽 —— 若不先判，AVIF 这种会一直解不出来，
        #    表现为"切到它就黑屏卡住"（实测 3000ms 超时，用户报的连续切图卡顿之一）。
        if not self._can_decode(self._path):
            self._error = "系统解码器不支持该格式：%s" % (
                os.path.splitext(self._path)[1].lstrip(".").upper() or "未知")
            self._src = self._img = None
            self._ready = False
            self.update()
            self._sync_minimap()
            self.load_failed.emit(self._path, self._error)
            return False

        hit = self._cache_get(self._path)
        if hit is not None:
            self._src = hit
            self._apply_transform()
            self._ready = True
            self.update()
            self._sync_minimap()
            self.image_ready.emit(self._path, self._full)
            self._maybe_request_full()
            return True

        self._src = None
        self._img = None
        self._recompute_fit()
        self.update()                       # 先清黑场（几何已按原图尺寸算好）
        self._pool.start(_DecodeTask(self._path, self._stage_size(),
                                     self._sig, 1, self._abort,
                                     self._load_generation, lambda: self._load_generation))
        return True

    def _stage_size(self) -> QSize:
        """底图解码尺寸 = 视口 **× devicePixelRatio**。

        ⚠️ 必须乘 DPR：150% 缩放下，视口 1180 逻辑像素实际要 1770 物理像素才 1:1。
           不乘的话"适应窗口"本身就等于把底图放大 1.5 倍 ⇒ 会被 `_needs_full()`
           判成"需要原图"，一打开就白解一次全分辨率（实测切图变慢的元凶）。
        """
        dpr = self.devicePixelRatioF() or 1.0
        return QSize(max(16, int(self.width() * dpr * STAGE1_VIEW_FACTOR)),
                     max(16, int(self.height() * dpr * STAGE1_VIEW_FACTOR)))

    @staticmethod
    def _can_decode(path: str) -> bool:
        """Qt 这一侧能不能解这个文件（不真正解码，只问插件）。"""
        try:
            rd = QImageReader(path)
            return bool(rd.canRead())
        except Exception:
            return False

    def set_neighbors(self, prev_path: str, next_path: str, next2_path: str = ""):
        """登记 **N±2** 预加载候选（v1.0.20 预加载管线）。

        停在第 Index 张时后台立刻预拉 Index-1 / +1 / +2 的**视口分辨率**底图进内存
        ⇒ 按方向键或点热区时命中缓存、**0ms 上屏**（无磁盘 IO、无解码）。
        """
        order = [next_path, next2_path, prev_path]     # 先下、再下下、最后上一张
        self._neighbors = [x for x in order
                           if x and x != self._path and is_image(x)
                           and x not in self._cache]
        if self._neighbors:
            QTimer.singleShot(140, self._prefetch_one)

    def _prefetch_one(self):
        """预加载**只解视口分辨率底图**（stage 1，经 setScaledSize），绝不预解原图。"""
        if not self._neighbors:
            return
        path = self._neighbors.pop(0)
        if path in self._cache or path == self._path or not self._can_decode(path):
            if self._neighbors:
                QTimer.singleShot(60, self._prefetch_one)
            return
        self._ppool.start(_DecodeTask(path, self._stage_size(),
                                      self._sig, 1, self._pabort))
        if self._neighbors:
            QTimer.singleShot(200, self._prefetch_one)

    def clear(self):
        self._abort.set()
        self._src = self._img = None
        self._path = ""
        self._ready = self._full = False
        self.reset_view()
        self.update()

    def reset_view(self, silent: bool = False):
        """复位为「适应窗口」（弹簧也一起清零）。

        #4 `silent=True` 时**不发射** scale_changed ⇒ 内部复位（切图/旋转/镜像）
        不会把"缩放: 100%"派发出去、顶掉"第 X / Y 张""水平镜像"等真正的用户提示。
        """
        self._target_scale = 1.0
        self._scale = 1.0
        self._spring_v = 0.0
        self._off = QPointF(0.0, 0.0)
        self._off_target = QPointF(0.0, 0.0)
        self._off_v = QPointF(0.0, 0.0)
        self._anchor = None
        for s in (self._spr_scale, self._spr_off_x, self._spr_off_y):
            s.t0 = None
        self._timer.stop()
        self.update()
        if not silent:
            self.scale_changed.emit(self._scale)
        self._sync_minimap()

    # ---- 旋转 / 镜像 ------------------------------------------------------
    def set_rotation(self, deg: int):
        self._rotation = int(deg) % 360
        self._apply_transform()
        self.reset_view(silent=True)

    def set_flip(self, on: bool):
        self._flip = bool(on)
        self._apply_transform()
        self.reset_view(silent=True)

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

    # ---- 缩放（锚定）------------------------------------------------------
    def zoom_by(self, factor: float, anchor: QPointF | None = None):
        self.set_target_scale(self._target_scale * float(factor), anchor)

    def set_scale_immediate(self, scale: float, anchor: QPointF | None = None):
        """#3 即时缩放：**跳过弹簧**，按滑块当前比例直接重绘（绝对物理跟手）。

        拖滑条时必须走这里 —— 否则每一格都要等弹簧追，手感是"迟钝 + 发涩"。
        锚点缺省用视口中心（滑条不好取光标），公式与 `set_target_scale` 完全一致。
        """
        new = max(SCALE_MIN, min(SCALE_MAX, float(scale)))
        k = new / self._scale if self._scale > 1e-6 else 1.0
        if anchor is None:
            cx, cy = self.width() / 2.0, self.height() / 2.0
        else:
            cx, cy = float(anchor.x()), float(anchor.y())
        vw, vh = float(self.width()), float(self.height())
        ox, oy = self._off.x(), self._off.y()
        self._off = QPointF(cx - vw / 2.0 - k * (cx - vw / 2.0 - ox),
                            cy - vh / 2.0 - k * (cy - vh / 2.0 - oy))
        self._scale = new
        self._target_scale = new
        self._spring_v = 0.0
        for s in (self._spr_scale, self._spr_off_x, self._spr_off_y):
            s.t0 = None
        self._timer.stop()
        self._clamp_off()
        self._off_target = QPointF(self._off)
        self._anchor = None                 # 即时缩放不走反解，避免后续 tick 拽回
        self.update()
        self.scale_changed.emit(self._scale)
        self.user_zoomed.emit()             # 拖滑条也是用户操作（#4）
        self._sync_minimap()
        self._maybe_request_full()

    def zoom_at_cursor(self, factor: float):
        pos = self.mapFromGlobal(self.cursor().pos())
        if not self.rect().contains(pos):
            pos = QPoint(self.width() // 2, self.height() // 2)
        self.zoom_by(factor, QPointF(pos))

    def set_target_scale(self, value: float, anchor: QPointF | None = None):
        """设定目标缩放，并按用户给定的锚点公式同步算出**目标偏移**。

        公式（用户指定，严格实现）：
            k      = s_target / s_cur
            ox_new = cx - VW/2 - k * (cx - VW/2 - ox)
            oy_new = cy - VH/2 - k * (cy - VH/2 - oy)

        缩放与偏移用**同一组弹簧参数**同时启动 ⇒ 归一化进度完全一致，
        两者按同一比例联动逼近，缩放过程中锚点不飘、不发涩。
        未给锚点时以视口中心为锚（公式退化为 ox_new = k·ox，即"绕中心缩放"）。
        """
        new = max(SCALE_MIN, min(SCALE_MAX, float(value)))
        if abs(new - self._target_scale) < 1e-9:
            return
        vw, vh = float(self.width()), float(self.height())
        if anchor is None:
            cx, cy = vw / 2.0, vh / 2.0
        else:
            cx, cy = float(anchor.x()), float(anchor.y())
        # 记下"光标下的图像点"：之后每帧由 scale 反解偏移（#2 零累计误差）
        if self._img is not None and not self._img.isNull() and self._img.width() > 0:
            au, av = self._point_in_image(QPointF(cx, cy))
        else:
            au, av = 0.5, 0.5
        self._anchor = (cx, cy, au, av)
        self._target_scale = new
        now = time.perf_counter()
        self._spr_scale.reset(self._scale, self._spring_v, new)
        self._spr_scale.start(now)
        if not self._timer.isActive():
            self._last_tick = now
            self._timer.start()
        self._maybe_request_full()

    # ---- 弹簧积分（对标 SpringScalarNaturalMotionAnimation）---------------
    def _tick(self):
        now = time.perf_counter()
        dt = max(0.001, min(0.05, now - self._last_tick))
        self._last_tick = now
        w0 = 2.0 * math.pi / max(0.02, SPRING_PERIOD_S)
        zeta = SPRING_ZETA
        busy = False

        # ---- 缩放弹簧（解析解，ζ=1 ⇒ 数学上不过冲）----
        if self._spr_scale.t0 is not None:
            x, v, done = self._spr_scale.value(now)
            self._scale, self._spring_v = x, v
            if done:
                self._spr_scale.t0 = None
            else:
                busy = True
        else:
            self._scale = self._target_scale
            self._spring_v = 0.0

        # #2 锚点：offset **作为当前 scale 的函数直接反解**，不交给独立弹簧
        #    （两份时间常数会引入累计误差）：
        #        off(t) = A - V/2 - (uv - 0.5) * W(scale(t))
        #    即逆向报告 §2.3.2 的"动态反解约束"——全程 QPointF 浮点、无中间取整、
        #    无逐帧累加 ⇒ 任意倍率下都严丝合缝。
        if self._anchor is not None and self._img is not None:
            ax, ay, au, av = self._anchor
            k = self._k_fit * self._scale
            w = self._img.width() * k
            h = self._img.height() * k
            self._off = QPointF(ax - self.width() / 2.0 - (au - 0.5) * w,
                                ay - self.height() / 2.0 - (av - 0.5) * h)
            self._off_target = QPointF(self._off)
            # ⚠️ 缩放插值未完成时**禁止夹取**：夹取会截断锚定结果 ⇒ 光标下的像素脱手。
            #    收敛后（busy=False 那一步）再做一次边界整理。
        # ---- 平移惯性弹簧（同样解析解；撞边界由夹取自然收束，不写过冲曲线）----
        if self._spr_off_x.t0 is not None or self._spr_off_y.t0 is not None:
            if self._spr_off_x.t0 is not None:
                ox, vx, dx_done = self._spr_off_x.value(now)
                if dx_done:
                    self._spr_off_x.t0 = None
            else:
                ox, vx = self._off.x(), 0.0
            if self._spr_off_y.t0 is not None:
                oy, vy, dy_done = self._spr_off_y.value(now)
                if dy_done:
                    self._spr_off_y.t0 = None
            else:
                oy, vy = self._off.y(), 0.0
            self._off = QPointF(ox, oy)
            self._off_v = QPointF(vx, vy)
            self._clamp_off()
            if self._spr_off_x.t0 is not None or self._spr_off_y.t0 is not None:
                busy = True
        else:
            self._off = QPointF(self._off_target)
            self._off_v = QPointF(0.0, 0.0)

        self.update()
        self.scale_changed.emit(self._scale)
        self._sync_minimap()
        if not busy:
            # 动画收敛：这时才做一次边界夹取（缩放过程中不夹，保证锚定不被破坏）
            self._clamp_off()
            self._off_target = QPointF(self._off)
            self._anchor = None
            self._timer.stop()             # 收敛即停表：空闲 CPU 0 开销

    # ---- 几何 -------------------------------------------------------------
    def _recompute_fit(self):
        w, h = (self._img.width(), self._img.height()) if self._img is not None else self._natural
        if not w or not h:
            return
        self._k_fit = min(max(1, self.width()) / w, max(1, self.height()) / h)

    def size_px(self) -> tuple[int, int]:
        """当前绘制用的图像尺寸（用于信息面板显示"已加载分辨率"）。"""
        if self._img is None:
            return (0, 0)
        return (self._img.width(), self._img.height())

    def image_rect(self) -> QRectF:
        w, h = (self._img.width(), self._img.height()) if self._img is not None else self._natural
        if not w or not h:
            return QRectF()
        k = self._k_fit * self._scale
        sw, sh = w * k, h * k
        cx = self.width() / 2.0 + self._off.x()
        cy = self.height() / 2.0 + self._off.y()
        return QRectF(cx - sw / 2.0, cy - sh / 2.0, sw, sh)

    def view_rect(self):
        r = self.image_rect()
        if r.isEmpty():
            return None
        vis = r.intersected(QRectF(self.rect()))
        if vis.isEmpty():
            return None
        return ((vis.x() - r.x()) / r.width(), (vis.y() - r.y()) / r.height(),
                vis.width() / r.width(), vis.height() / r.height())

    def _point_in_image(self, pos):
        r = self.image_rect()
        if r.isEmpty() or r.width() <= 0 or r.height() <= 0:
            return (0.5, 0.5)
        return ((float(pos.x()) - r.x()) / r.width(), (float(pos.y()) - r.y()) / r.height())

    def _bounds(self):
        """允许的偏移范围 —— v1.0.21 起改为「**无限自由画布**」。

        ⚠️⚠️ 旧模型是"牢房"：ox ∈ [-(W-VW)/2, (W-VW)/2] ⇒ 放大后画面被死死锁在
           满屏边界上，缩放收尾时必然发生"跳跃回溯"（用户附图对比爱奇艺）。
           爱奇艺/照片那类看图应用其实是**自由画布**：放大后可以随手把图拖到
           屏幕任意角落，只留一条边，背景留白，绝不暴力弹回。

        新模型（用户给定）：
            ox_min = -(W + VW)/2 + margin
            ox_max = +(W + VW)/2 - margin      （margin = 60px）
        即"屏内只要还留着 60px 的图片边缘"就允许继续拖。
        副产品：夹取范围极大放宽 ⇒ 光标锚点缩放在任何倍率下都不会撞到窗口物理边缘，
        "放大结束后的定位回溯闪烁"随之彻底消失。
        """
        w, h = (self._img.width(), self._img.height()) if self._img is not None else self._natural
        if not w or not h:
            return (0.0, 0.0)
        k = self._k_fit * max(self._scale, self._target_scale)
        iw, ih = w * k, h * k
        vw, vh = float(max(1, self.width())), float(max(1, self.height()))
        m = FREE_PAN_MARGIN_PX
        mx = max(0.0, (iw + vw) / 2.0 - m)
        my = max(0.0, (ih + vh) / 2.0 - m)
        return (mx, my)

    def _clamp_off(self):
        mx, my = self._bounds()
        self._off = QPointF(max(-mx, min(mx, self._off.x())),
                            max(-my, min(my, self._off.y())))

    def _clamp_target(self, off: QPointF) -> QPointF:
        mx, my = self._bounds()
        return QPointF(max(-mx, min(mx, off.x())), max(-my, min(my, off.y())))

    # ---- 取景框 -----------------------------------------------------------
    def _on_minimap_pan(self, nu: float, nv: float):
        r = self.image_rect()
        if r.isEmpty():
            return
        self._anchor = None
        self._off = QPointF(-(nu - 0.5) * r.width(), -(nv - 0.5) * r.height())
        self._clamp_off()
        self._off_target = QPointF(self._off)
        self._off_v = QPointF(0.0, 0.0)
        self.update()
        self._sync_minimap()

    def _sync_minimap(self):
        self.minimap.set_image(self._img)
        self.minimap.set_view(self.view_rect())
        self.minimap.update_visibility(self._target_scale > 1.001 or self._scale > 1.001)

    # ---- 按需全分辨率 -----------------------------------------------------
    def _needs_full(self) -> bool:
        """是否需要（且被允许）拉取全分辨率原图。

        v1.0.22 降采样纪律：正常切图浏览**一律**只走 `setScaledSize` 的轻量底图
        （libjpeg DCT 降采样，单张 ~30ms 量级）；只有同时满足
          ① 用户**手动放大超过适应窗口**（scale > FULL_MIN_SCALE）
          ② 停稳 FULL_DEBOUNCE_MS（300ms，由 `_full_timer` 把关）
        才允许后台异步拉一次原图。
        """
        if self._img is None:
            return False
        # ① 未放大到超过适应窗口 ⇒ 永不拉原图
        if self._scale <= FULL_MIN_SCALE:
            return False
        w, h = self._natural
        if not w or not h:
            return False
        # ⚠️ 底图**本身就是原尺寸**（小图，stage1 没做缩放）⇒ 再解一遍毫无意义，
        #    只会白花一次全文件解码（实测：480×320 的图被误判成"需要原图"）。
        if self._img.width() >= w or self._img.height() >= h:
            self._full = True
            return False
        k = self._k_fit * self._target_scale
        dpr = self.devicePixelRatioF() or 1.0
        # ⚠️⚠️ 阈值不能贴 1.0：高 DPI 下"适应窗口"本身就把底图放大约 1.5 倍
        #    （150% 缩放），贴 1.0 会导致**一打开就解全分辨率** ⇒ 切图又变慢（实测踩到）。
        #    1.4 倍以内允许轻微放大（肉眼几乎无损），超过才拉原图。
        return w * k * dpr > self._img.width() * 1.4

    def _maybe_request_full(self):
        """#5 请求全分辨率原图 —— 走 **200ms 防抖**。

        连续翻图时用户根本来不及看细节，却会为每一张都排一次全量解码 ⇒ CPU 塞死、
        越翻越卡。防抖后只有"在某张图上停稳 200ms"才会真正去解原图。
        """
        if self._full or self._full_pending or not self._path:
            return
        if not self._needs_full():
            return
        self._full_timer.start()               # 重新计时（连续切图 = 一直不解）

    def _do_request_full(self):
        if self._full or self._full_pending or not self._path:
            return
        if not self._needs_full():
            return
        self._full_pending = True
        try:
            self._fpool.clear()
        except Exception:
            pass
        self._fpool.start(_DecodeTask(self._path, self._stage_size(),
                                      self._sig, 2, self._abort,
                                      self._load_generation, lambda: self._load_generation))

    # ---- 解码回调 ---------------------------------------------------------
    def _on_stage_ready(self, path: str, img: QImage, stage: int):
        if path != self._path:
            # 预解码/已切走的图：进缓存即可（下次切到它时零等待）
            if stage == 1:
                self._cache_put(path, img)
            return
        if stage == 1:
            if self._full:
                return
            self._cache_put(path, img)
        self._src = img
        if stage == 2:
            self._full = True
        self._full_pending = False
        self._ready = True
        self._apply_transform()
        self.update()                       # 不动 _off / _scale（无缝替换纹理）
        self._sync_minimap()
        self.image_ready.emit(path, self._full)

    # ================================================================== 绘制
    def canvas_color(self) -> QColor:
        """视口底色：浅色模式**纯白**（照片像摆在白桌面上），深色模式近黑。"""
        try:
            from .theme import is_light

            if is_light():
                return QColor("#FFFFFF")
        except Exception:
            pass
        return QColor("#0B0B0C")

    def shadow_layers(self):
        """相纸投影的层参数（由内到外：扩散半径, alpha）—— 浅色模式才画。

        用若干层**低 alpha 圆角描边**模拟柔和外阴影：不需要位图模糊（那会有
        "烟熏"感和每帧 CPU 开销），层数固定、成本恒定，且天然跟随缩放/平移
        （每帧按当前 image_rect 重算）。
        """
        try:
            from .theme import is_light

            if not is_light():
                return ()
        except Exception:
            return ()
        return ((1.0, 20), (2.5, 16), (4.5, 12), (7.0, 8), (10.0, 5))

    def paintEvent(self, _ev):            # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), self.canvas_color())
        img = self._img
        if img is None:
            p.end()
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        r = self.image_rect()
        # v1.0.21 相纸投影：浅色模式下白底白图会"融"在一起，先在**图片外框外围**
        # 铺一层柔和阴影（画在图片之前 ⇒ 内圈被图片本身盖住，只露出外圈晕影）。
        layers = self.shadow_layers()
        if layers and not r.isEmpty():
            p.setPen(Qt.NoPen)
            for grow, alpha in layers:
                path = QPainterPath()
                path.addRoundedRect(r.adjusted(-grow, -grow, grow, grow), 3.0, 3.0)
                p.fillPath(path, QColor(0, 0, 0, alpha))
        vis = r.intersected(QRectF(self.rect()))
        if not vis.isEmpty() and r.width() > 0 and r.height() > 0:
            sx = (vis.x() - r.x()) / r.width() * img.width()
            sy = (vis.y() - r.y()) / r.height() * img.height()
            sw = vis.width() / r.width() * img.width()
            sh = vis.height() / r.height() * img.height()
            p.drawImage(vis, img, QRectF(sx, sy, sw, sh))
        p.end()

    # ================================================================== 事件
    def apply_minimap_position(self, mode: str):
        """#8 取景框位置：`bottom_right`（默认，浮在控制栏上方）/ `top_right`。"""
        self._minimap_pos = "top_right" if str(mode) == "top_right" else "bottom_right"
        self._place_minimap()

    def _place_minimap(self):
        m = self.minimap
        x = max(8, self.width() - m.width() - 16)
        if self._minimap_pos == "top_right":
            y = 12
        else:
            # 右下角：留出底部控制栏的高度（约 76px），不要压住它
            y = max(12, self.height() - m.height() - 76)
        m.move(x, y)

    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._recompute_fit()
        self._place_minimap()
        # ⚠️ 热区归**舞台**管（见 Stage.sync_hotspots）：只有挂在舞台上才能既浮在
        #    画面/浮层之上、又排在取景框与手柄之下。这里只在它们仍是自己子控件时兜底。
        for z in (self.zone_left, self.zone_right):
            if z.parentWidget() is self:
                z.setGeometry(self.width() - z.width() if z is self.zone_right else 0,
                              0, z.width(), self.height())
                z.raise_()
        self.minimap.raise_()
        try:
            self.viewport_resized.emit()
        except Exception:
            pass
        self._clamp_off()
        self._sync_minimap()

    def wheelEvent(self, ev):             # noqa: N802
        dy = ev.angleDelta().y()
        if not dy:
            ev.ignore()
            return
        # #1 滚轮上滑（delta > 0）= **放大** ×1.15；下滑 = 缩小 ÷1.15。
        #    `image.wheel_invert` 可整体反向（有些鼠标/驱动把滚轮方向报反）。
        if self.wheel_invert:
            dy = -dy
        self.zoom_by(WHEEL_STEP if dy > 0 else 1.0 / WHEEL_STEP, ev.position())
        self.user_zoomed.emit()             # #4 用户滚轮 ⇒ 允许发缩放 OSD
        ev.accept()

    def mousePressEvent(self, ev):        # noqa: N802
        if ev.button() == Qt.LeftButton and self._img is not None:
            self._drag = QPointF(ev.position())
            self._drag_last = (time.perf_counter(), QPointF(ev.position()))
            self._drag_v = QPointF(0.0, 0.0)
            self._anchor = None             # 手动平移优先，撤销锚定
            self._timer.stop()
            self.setCursor(Qt.ClosedHandCursor)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):         # noqa: N802
        if self._drag is not None:
            pos = QPointF(ev.position())
            d = pos - self._drag
            self._drag = pos
            self._off += d
            self._off_target = QPointF(self._off)
            self._clamp_off()
            now = time.perf_counter()
            if self._drag_last is not None:
                dt = max(0.004, now - self._drag_last[0])
                self._drag_v = (pos - self._drag_last[1]) / dt
            self._drag_last = (now, pos)
            self.update()
            self._sync_minimap()
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):      # noqa: N802
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)
            v = self._drag_v
            self._drag_v = QPointF(0.0, 0.0)
            # 速度交接（§4.7 要点 1）：把拖拽末速交给弹簧 ⇒ 自然滑行，
            # ζ=1 临界阻尼 ⇒ 到位即停，**不过冲、不回弹**。
            if abs(v.x()) > 60 or abs(v.y()) > 60:
                tgt = self._clamp_target(self._off + v * 0.16)
                now = time.perf_counter()
                self._off_target = tgt
                self._off_v = QPointF(v.x() * 0.55, v.y() * 0.55)
                self._spr_off_x.reset(self._off.x(), self._off_v.x(), tgt.x())
                self._spr_off_y.reset(self._off.y(), self._off_v.y(), tgt.y())
                self._spr_off_x.start(now)
                self._spr_off_y.start(now)
                if not self._timer.isActive():
                    self._last_tick = now
                    self._timer.start()
            else:
                self._off_target = QPointF(self._off)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)
