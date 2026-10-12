"""Qt 原生图片查看视口 —— 图片不再经过 libmpv。

架构依据（用户纠偏）：用视频引擎承载图片查看是错的 —— 高频缩放/平移会塞满 IPC 掉帧、
切图必定销毁流而闪烁、事件劫持还会瘫痪控制栏。图片应当走 **Qt 原生 2D 引擎**：

* 缩放/平移 = 本地 `QTransform` 矩阵，GPU 合成 ⇒ 满帧、跟手；
* 取景框 = 视口 QRect 与图片原始尺寸的比例换算，纯内存 0ms；
* 切图 = 直接换 `QImage` ⇒ 0 延迟、0 闪回、0 黑屏；
* 状态生命周期 = 切图**复位**为「适应窗口」（可用配置改成锁定比例）；
* 事件作用域 = 拖拽只在本控件内部处理，绝不挂全局/顶层（底栏与热区优先）。
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QTransform
from PySide6.QtWidgets import QWidget

log = logging.getLogger("zhijian.ui.imageview")

#: 支持的图片扩展名（分流器用它判断走 Qt 原生还是 mpv）
IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif",
              ".avif", ".tif", ".tiff", ".ico", ".jfif", ".svg", ".pbm", ".pgm", ".ppm"}

MIN_SCALE, MAX_SCALE = 0.05, 40.0


def is_image(path: str) -> bool:
    return os.path.splitext(str(path or ""))[1].lower() in IMAGE_EXTS


class ImageViewerWidget(QWidget):
    """纯 Qt 图片视口：滚轮缩放（朝光标）、左键拖拽、取景框换算，全部本地完成。"""

    view_changed = Signal()          # 视图变化（缩放/平移/换图）⇒ 导航取景框刷新

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ImageViewer")
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.OpenHandCursor)
        self._src: QImage | None = None      # 原始图（未旋转/镜像）
        self._img: QImage | None = None      # 实际绘制用（已应用旋转/镜像）
        self._path = ""
        self._scale = 1.0                    # 用户缩放倍数（1.0 = 适应窗口）
        self._off = QPointF(0.0, 0.0)        # 平移（控件像素，相对居中）
        self._rotation = 0
        self._flip = False
        self._drag = None                    # 拖拽起点（控件坐标）
        self._error = ""

    # ------------------------------------------------------------------ 数据
    def path(self) -> str:
        return self._path

    def has_image(self) -> bool:
        return self._img is not None

    def load(self, path: str, reset: bool = True) -> bool:
        """读入图片（缩放读取不做，原图保留精度；QPainter 负责平滑缩放）。"""
        self._error = ""
        try:
            rd = QImageReader(path)
            rd.setAutoTransform(True)        # 尊重 EXIF 方向
            img = rd.read()
            if img is None or img.isNull():
                self._error = rd.errorString() or "无法读取"
                log.warning("图片读取失败 %s: %s", path, self._error)
                self._src = self._img = None
                self._path = ""
                self.update()
                return False
            self._src = img
            self._path = path
            self._apply_transform()
            if reset:
                self.reset_view()
            else:
                self.update()
                self.view_changed.emit()
            return True
        except Exception as exc:
            self._error = "%s: %s" % (type(exc).__name__, exc)
            log.debug("图片加载异常 %s", path, exc_info=True)
            self._src = self._img = None
            self.update()
            return False

    def clear(self):
        self._src = self._img = None
        self._path = ""
        self.reset_view()
        self.update()

    def reset_view(self):
        """复位为「适应窗口」——切图默认走这里（用户要求：状态一律重置）。"""
        self._scale = 1.0
        self._off = QPointF(0.0, 0.0)
        self.update()
        self.view_changed.emit()

    # ------------------------------------------------------------------ 旋转/镜像
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
        tf = QTransform()
        tf.rotate(self._rotation)
        if self._flip:
            tf.scale(-1.0, 1.0)
        img = self._src
        if self._rotation or self._flip:
            img = self._src.transformed(tf, Qt.SmoothTransformation)
        self._img = img

    # ------------------------------------------------------------------ 几何
    def fit_size(self) -> tuple[float, float]:
        """「适应窗口」时图片应显示的尺寸（等比 contain）。"""
        if self._img is None:
            return (0.0, 0.0)
        iw, ih = max(1, self._img.width()), max(1, self._img.height())
        ww, wh = max(1, self.width()), max(1, self.height())
        k = min(ww / iw, wh / ih)
        return (iw * k, ih * k)

    def image_rect(self) -> QRectF:
        """图片当前在控件坐标里的矩形（含缩放与平移）。"""
        fw, fh = self.fit_size()
        if fw <= 0 or fh <= 0:
            return QRectF()
        w, h = fw * self._scale, fh * self._scale
        cx = self.width() / 2.0 + self._off.x()
        cy = self.height() / 2.0 + self._off.y()
        return QRectF(cx - w / 2.0, cy - h / 2.0, w, h)

    def view_rect(self):
        """当前**可见**部分在图片内的归一化比例 (x, y, w, h) —— 取景框用，纯内存。"""
        r = self.image_rect()
        if r.isEmpty():
            return None
        vis = r.intersected(QRectF(self.rect()))
        if vis.isEmpty():
            return None
        return ((vis.x() - r.x()) / r.width(), (vis.y() - r.y()) / r.height(),
                vis.width() / r.width(), vis.height() / r.height())

    def zoom(self) -> float:
        return self._scale

    def set_zoom(self, scale: float, anchor=None):
        """设置缩放倍数；`anchor`（控件坐标）给定时**朝该点缩放**。"""
        new = max(MIN_SCALE, min(MAX_SCALE, float(scale)))
        if abs(new - self._scale) < 1e-6:
            return
        if anchor is not None and self._img is not None:
            before = self._point_in_image(anchor)
            self._scale = new
            after = self._point_in_image(anchor)
            if before and after:
                fw, fh = self.fit_size()
                self._off += QPointF((after[0] - before[0]) * fw * self._scale,
                                     (after[1] - before[1]) * fh * self._scale)
        else:
            self._scale = new
        self._clamp_off()
        self.update()
        self.view_changed.emit()

    def zoom_by(self, factor: float, anchor=None):
        self.set_zoom(self._scale * float(factor), anchor)

    def zoom_at_cursor(self, factor: float):
        """朝**当前光标**缩放（滚轮/按钮/滑条统一入口）。"""
        pos = self.mapFromGlobal(self.cursor().pos())
        if not self.rect().contains(pos):
            pos = QPoint(self.width() // 2, self.height() // 2)
        self.zoom_by(factor, QPointF(pos))

    def _point_in_image(self, pos):
        """控件坐标 → 图片内归一化坐标 (0..1)，越界返回 None 之外仍给值（便于锚定）。"""
        r = self.image_rect()
        if r.isEmpty() or r.width() <= 0 or r.height() <= 0:
            return None
        return ((float(pos.x()) - r.x()) / r.width(), (float(pos.y()) - r.y()) / r.height())

    def _clamp_off(self):
        """限制平移：图片小于控件时居中，否则不让边缘离开窗口太多。"""
        r = self.image_rect()
        if r.isEmpty():
            self._off = QPointF(0.0, 0.0)
            return
        max_x = max(0.0, (r.width() - self.width()) / 2.0)
        max_y = max(0.0, (r.height() - self.height()) / 2.0)
        self._off = QPointF(max(-max_x, min(max_x, self._off.x())),
                            max(-max_y, min(max_y, self._off.y())))

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, _ev):       # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0B0B0C"))
        img = self._img
        if img is None:
            p.end()
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.image_rect()
        # 只画可见部分（大图放大时避免整图合成 ⇒ 更省）
        src = QRectF(img.rect())
        vis = r.intersected(QRectF(self.rect()))
        if not vis.isEmpty() and r.width() > 0 and r.height() > 0:
            sx = (vis.x() - r.x()) / r.width() * src.width()
            sy = (vis.y() - r.y()) / r.height() * src.height()
            sw = vis.width() / r.width() * src.width()
            sh = vis.height() / r.height() * src.height()
            p.drawImage(vis, img, QRectF(sx, sy, sw, sh))
        p.end()

    # ------------------------------------------------------------------ 交互（只在本控件内）
    def wheelEvent(self, ev):        # noqa: N802
        dy = ev.angleDelta().y()
        if not dy:
            ev.ignore()
            return
        step = 1.12 if dy > 0 else 1 / 1.12
        self.zoom_by(step, ev.position())
        ev.accept()

    def mousePressEvent(self, ev):   # noqa: N802
        if ev.button() == Qt.LeftButton and self._img is not None:
            self._drag = QPointF(ev.position())
            self.setCursor(Qt.ClosedHandCursor)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):    # noqa: N802
        if self._drag is not None:
            d = QPointF(ev.position()) - self._drag
            self._drag = QPointF(ev.position())
            self._off += d
            self._clamp_off()
            self.update()
            self.view_changed.emit()
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):  # noqa: N802
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def resizeEvent(self, ev):       # noqa: N802
        super().resizeEvent(ev)
        self._clamp_off()
        self.view_changed.emit()
