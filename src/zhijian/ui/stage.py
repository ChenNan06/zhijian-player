"""舞台（Stage）：视频画面 + 各类浮层的容器。

布局：一个 QGridLayout —— QOpenGLWidget 跨全部行/列铺满, 三排浮层压在它上面：
    第 0 行  顶部浮层（图片信息条 / 拖放提示）
    第 1 行  中部浮层（空状态 / 设置面板, 居中）
    第 2 行  底部浮层（字幕浮层, 右对齐）
**没有任何 setGeometry / 手工坐标** —— 位置完全由布局器与对齐标志决定。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..media.scanner import human_size
from ..mpv import MpvRenderWidget
from . import icons
from .theme import M, P, css, qt_color
from .widgets import (GlassFrame, IconButton, NoWheelSlider, OsdBadge, TextIconButton,
                      WrappedLabel, vspace)

log = logging.getLogger(__name__)


class EdgeZone(QWidget):
    """舞台左右两侧的**热区**：鼠标进入时高亮并显示箭头，区内按下即切换上/下一张。

    用户原话："给界面左右两边设定一个区域，当鼠标进入那个区域时把那个区域的亮度
    高亮一下，提示你进入了那个区域，同时显现一个左右箭头，然后只要在那个区域
    按下鼠标就切换上/下一张图片。"

    ⚠️ 定位只用**布局**（贴在舞台左右边缘、跨满高度），不写死坐标；
       宽度来自设置 `image.edge_zone_px`（0 = 关闭）。
    ⚠️ 不能设 `WA_TransparentForMouseEvents`：那样就收不到 hover 与点击了。
       它只占窄窄一条，且**只在图片模式下显示**，所以不会影响正常观看/拖拽。
    """

    activated = Signal()

    def __init__(self, side: str, parent=None):
        super().__init__(parent)
        self.side = "left" if str(side).startswith("l") else "right"
        self._hot = False
        self.setObjectName("EdgeZone" + self.side.title())
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setMouseTracking(True)

    def set_hot(self, on: bool):
        if bool(on) != self._hot:
            self._hot = bool(on)
            self.update()

    def is_hot(self) -> bool:
        return self._hot

    def enterEvent(self, ev):      # noqa: N802
        self.set_hot(True)
        super().enterEvent(ev)

    def leaveEvent(self, ev):      # noqa: N802
        self.set_hot(False)
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self.activated.emit()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def paintEvent(self, _ev):     # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect())
        # 高亮：从边缘往内淡出的竖条（"把那个区域的亮度高亮一下"）
        if self._hot:
            from PySide6.QtGui import QLinearGradient

            # 高亮是**横向**渐变：左侧从左边亮、右侧从右边亮（原来右侧终点写成了
            # bottomLeft ⇒ 变成斜向渐变，看着不对）。
            if self.side == "left":
                g = QLinearGradient(r.topLeft(), r.topRight())
            else:
                g = QLinearGradient(r.topRight(), r.topLeft())
            c = QColor("#FFFFFF")          # ⑫ 与箭头一致：白色高亮，不跟主题走
            c.setAlpha(46)
            g.setColorAt(0.0, c)
            c2 = QColor("#FFFFFF")
            c2.setAlpha(0)
            g.setColorAt(1.0, c2)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawRect(r)
            # 箭头
            name = "prev_image" if self.side == "left" else "next_image"
            # ⚠️ 签名是 render_pixmap(name, color, size, stroke) —— 参数顺序写错
            #    会让 paintEvent 直接抛异常，表现成"应用卡死"（实测踩过）。
            # ⑩ 用户："热区的上/下张图标有锯齿" ⇒ **2 倍分辨率渲染 + DPR**，
            #    这样在 150% 缩放的屏幕上也不会糊。
            # ⑫ 用户："切换浅色模式后热区图标暗淡" ⇒ 箭头**固定用白色**：
            #    它压在图片上，跟界面主题无关（原来取 P["text"]，浅色主题下是深灰，看不见）。
            pm = icons.render_pixmap(name, "#FFFFFF", 60, 3.0)
            if pm is not None and not pm.isNull():
                pm.setDevicePixelRatio(2.0)
                w = pm.width() / 2.0
                h = pm.height() / 2.0
                x = r.left() + (r.width() - w) / 2.0
                y = r.top() + (r.height() - h) / 2.0
                p.setOpacity(0.92)
                p.drawPixmap(int(x), int(y), int(w), int(h), pm)
        p.end()


class ImageInfoBar(GlassFrame):
    """图片模式下的顶部信息条：文件名 / 分辨率 / 大小 / 缩放比例。"""

    zoom_in = Signal()
    zoom_out = Signal()
    zoom_changed = Signal(float)      # 滑条拖动（单位 = video-zoom）
    playlist = Signal()               # ⑩ 图片控制栏上的播放列表按钮
    fullscreen = Signal()             # ⑦ 用户："控制栏没有全屏按钮"
    zoom_fit = Signal()
    zoom_one = Signal()
    rotate_left = Signal()
    rotate_right = Signal()
    flip_h = Signal()
    prev_image = Signal()
    next_image = Signal()
    reveal = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, radius=M["radius_lg"], fill=P["glass_panel"],
                         border=P["hairline"], shadow=0.0, shadow_spread=18,
                         pad=(12, 8, 12, 8))
        self.setObjectName("ImageBar")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        def btn(name, tip, sig, size=30, icon=16, checkable=False):
            b = IconButton(name, self, size=size, icon_size=icon, tooltip=tip, checkable=checkable)
            if sig is not None:
                b.clicked.connect(sig.emit)
            row.addWidget(b)
            return b

        def text_btn(label, tip, sig):
            """文字按钮：`fit`/`one_to_one` 原来只有图标，用户"看不懂这是干什么的"。

            ⚠️ 图标语义是有歧义的（四角括号像"全屏播放"、1:0 那个更像装饰），
               所以这两个动作直接写文字 —— 一眼就懂，比换图标更彻底。
            """
            b = QPushButton(label, self)
            b.setProperty("role", "ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setFixedHeight(28)
            b.clicked.connect(sig.emit)
            row.addWidget(b)
            return b

        # ⚠️ 文件名 / 分辨率 / 大小 **不在这里**了：用户要求"名字和像素可以放在窗口标题栏，
        #    不需要加到控制栏"。这里只留"跟看图操作有关"的东西。
        btn("prev_image", "上一张 (←)", self.prev_image)
        btn("next_image", "下一张 (→)", self.next_image)
        row.addSpacing(8)
        # 缩放：缩小 / 滑条 / 放大。滑条是用户点名要的（"可以加一个滑动条调整大小"）。
        btn("zoom_out", "缩小 (滚轮下 / Ctrl+-)", self.zoom_out)
        # ⑪ 用户："缩放数据可以放在缩放条上方而不是另起一块" ⇒ 数值 + 滑条**同一个竖块**
        zoom_box = QWidget(self)
        zb = QVBoxLayout(zoom_box)
        zb.setContentsMargins(0, 0, 0, 0)
        zb.setSpacing(0)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_label.setProperty("role", "dim12")
        self.zoom_label.setToolTip("当前缩放")
        zb.addWidget(self.zoom_label)
        # ⑪ 用户："缩放条设置成视频播放器的音量条一个样式就行" ⇒ 直接复用同一个自绘控件
        #    （`VolumeSlider`：细轨道 + 小圆点 + 悬停加粗 + 圆点内缩不被裁）。
        #    它的取值范围是 0..100，这里映射到 video-zoom 的 -6.0..8.0（log2 尺度）。
        from .widgets import VolumeSlider

        self.zoom_slider = VolumeSlider(self, width=78)
        self.zoom_slider.setToolTip("拖动调整缩放")
        self.zoom_slider.set_value(self._zoom_to_slider(0.0))
        self.zoom_slider.valueChanged.connect(
            lambda v: self.zoom_changed.emit(self._slider_to_zoom(v)))
        zb.addWidget(self.zoom_slider)
        row.addWidget(zoom_box)
        btn("zoom_in", "放大 (滚轮上 / Ctrl+=)", self.zoom_in)
        # ③ 「适应窗口」用**自绘图标**（用户："把适应窗口改成图标 自己绘制"）；
        #    ② 「原始大小」按用户要求**去掉**（对应的快捷键 2 仍可用）。
        btn("fit_window", "适应窗口 (1)", self.zoom_fit)
        row.addSpacing(8)
        btn("rotate_left", "向左旋转 (R / Shift+R)", self.rotate_left)
        btn("rotate_right", "向右旋转 (E)", self.rotate_right)
        # ⚠️ 原来用 `wall`（四个方块）当"水平镜像"图标 —— 看着像**占位图**，
        #    用户原话"水平镜像是摆设"。换成真正的镜像图标，并且做成**可勾选**的，
        #    开/关有状态可见（外加 OSD 提示）。
        self.btn_flip = btn("flip_h", "水平镜像 (H)", self.flip_h, checkable=True)
        btn("playlist", "播放列表 (L)", self.playlist)
        btn("fullscreen", "全屏 (F11)", self.fullscreen)      # ⑦
        btn("folder", "在资源管理器中显示", self.reveal)
        self.body_layout().addLayout(row)

    # ⑪ 缩放条映射（0..100 ↔ video-zoom 的 -6.0..8.0）
    ZOOM_MIN, ZOOM_MAX = -6.0, 8.0

    @classmethod
    def _zoom_to_slider(cls, z: float) -> float:
        k = (float(z) - cls.ZOOM_MIN) / (cls.ZOOM_MAX - cls.ZOOM_MIN)
        return max(0.0, min(100.0, k * 100.0))

    @classmethod
    def _slider_to_zoom(cls, v: float) -> float:
        return cls.ZOOM_MIN + (cls.ZOOM_MAX - cls.ZOOM_MIN) * max(0.0, min(100.0, float(v))) / 100.0

    def set_info(self, width: int, height: int, size: int, zoom: float, index: int, total: int):
        """刷新缩放百分比 + 滑条位置（文件名/分辨率已移到标题栏）。"""
        import math

        z = math.log2(max(0.01, float(zoom or 1.0)))
        self.zoom_label.setText(f"{float(zoom or 1.0) * 100:.0f}%")
        self.zoom_label.setToolTip(f"{width}×{height} · {human_size(size)} · {index}/{total}")
        # ⚠️ 用户："缩放条失效" —— 根因是**拖动过程中被回填值拽回去**：
        #    每帧刷新都会 setValue，滑条刚被拖到 A 又被写回 B，看着就是"拖不动"。
        #    正在拖动（isSliderDown）时**绝不回填**。
        # ⚠️ 拖动中不回填（否则把手会被刷新值拽回去 ⇒ 用户说"缩放条失效"）。
        #    VolumeSlider 用 `_dragging` 标记是否正在拖。
        if self.zoom_slider is not None and not getattr(self.zoom_slider, "_dragging", False):
            self.zoom_slider.set_value(self._zoom_to_slider(z))

    def set_flip_state(self, on: bool):
        try:
            self.btn_flip.setChecked(bool(on))
        except Exception:
            pass


class ImageNavigator(QWidget):
    """① 图片右下角的**导航取景框**：整图缩略图 + 可拖动的视野框。

    用户原话："放大缩小的时候可以在右下角加一个缩略图，显示缩放到图片的什么地方了，
    而且鼠标可以直接调整那个截取框来移动图片位置。"

    * 只在**放大状态**（画面比窗口大）时出现 —— 没放大时它没有意义；
    * 拖框 ⇒ 发 `pan_to(nx, ny)`（图片内归一化中心），由主窗换算成 mpv 的 pan；
    * 缩略图用 `QImageReader.setScaledSize` 缩放读取，**不会**把 4K 原图整个读进内存。
    """

    pan_to = Signal(float, float)      # 目标中心（图片内比例 0..1）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ImageNavigator")
        self.setFixedSize(184, 132)
        self.setMouseTracking(True)
        self.setCursor(Qt.OpenHandCursor)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setVisible(False)
        self._thumb = None            # QImage（等比缩放后的整图，可能已旋转/镜像）
        self._thumb_src = None        # QImage（未变换的原始缩略图）
        self._tf_key = None           # 当前变换 (rotation, flip)，避免重复变换
        self._view = (0.0, 0.0, 1.0, 1.0)   # 视野框（图片内比例：x, y, w, h）
        self._drag = None             # 拖动时记录指针与框中心的偏移
        self._src = ""
        self._zoom_on = False

    # ---------------------------------------------------------------- 数据
    def set_transform(self, rotation: int, flip: bool):
        """③ 取景框缩略图跟随**旋转/水平镜像**（否则用户转了图，取景框还是原样）。

        做法：把原始缩略图按同样角度/镜像做一次 QTransform 缓存起来，
        避免每次重绘都变换（性能）。
        """
        key = (int(rotation) % 360, bool(flip))
        if key == getattr(self, "_tf_key", None):
            return
        self._tf_key = key
        if self._thumb_src is None:
            return
        from PySide6.QtGui import QTransform

        tf = QTransform()
        tf.rotate(key[0])
        if key[1]:
            tf.scale(-1.0, 1.0)
        img = self._thumb_src.transformed(tf, Qt.SmoothTransformation)
        self._thumb = img if (img is not None and not img.isNull()) else self._thumb_src
        self.update()

    def set_zoom_state(self, zoomed: bool):
        """⑩ 由主窗告知"当前是否放大" —— 取景框的显示只跟这个状态走，避免闪烁。"""
        self._zoom_on = bool(zoomed)
        if not self._zoom_on:
            self.setVisible(False)
        else:
            self.setVisible(self._thumb is not None)
            if self.isVisible():
                # ⑩ 显示之后**自己抬到最上层**：右侧热区在图片模式下是 raise 过的，
                #    否则鼠标落在取景框上会先撞到热区 ⇒ 直接切图（用户实测反馈）。
                self.raise_()

    def set_source(self, path: str):
        """载入缩略图（缩放读取）。失败就留空，不影响其它功能。"""
        if not path or path == self._src:
            return
        self._src = path
        try:
            from PySide6.QtGui import QImageReader

            rd = QImageReader(path)
            rd.setAutoTransform(True)
            sz = rd.size()
            if sz.isValid() and sz.width() > 0:
                # ⚠️ 用 `sz.scaled(...)` 而不是 `QSize(...)`：本模块**没有**导入 QSize，
                #    直接引类名会 NameError，而它会被下面的 except 吞掉 ⇒ 缩略图永远是空
                #    （实测踩过：视野框算得对，但导航就是不出现）。
                # 用**整数重载** `scaled(w, h, mode)` —— 不引 QSize 类名，
                # 避免"模块没导入 QSize ⇒ NameError 被 except 吞掉 ⇒ 缩略图永远为空"。
                rd.setScaledSize(sz.scaled(max(8, self.width() - 8),
                                           max(8, self.height() - 8), Qt.KeepAspectRatio))
            img = rd.read()
            # 保存**原始**缩略图，变换后的放 `_thumb`（见 set_transform）
            self._thumb_src = img if (img is not None and not img.isNull()) else None
            self._thumb = self._thumb_src
            self._tf_key = None
        except Exception:
            log.debug("导航缩略图读取失败: %s", path, exc_info=True)
            self._thumb = None
        self.update()

    def set_view(self, view):
        """view = (x, y, w, h)，图片内比例。全 0 或 None ⇒ 隐藏。"""
        if not view:
            self.setVisible(False)
            return
        x, y, w, h = [max(0.0, min(1.0, float(v))) for v in view]
        w = max(1e-3, min(1.0, w))
        h = max(1e-3, min(1.0, h))
        x = min(x, 1.0 - w)
        y = min(y, 1.0 - h)
        self._view = (x, y, w, h)
        # ⑩ 用户："缩略图取景框功能时而显现时而不见" —— 原来的显示条件依赖
        #    "视野框比整图小"，而它在动画/补写过程中会瞬时等于整图 ⇒ 控件闪烁。
        #    改成看**是否处于放大状态**（由主窗显式告知），与瞬时几何解耦。
        self.setVisible(bool(self._thumb is not None and getattr(self, "_zoom_on", False)))
        self.update()

    # ---------------------------------------------------------------- 绘制
    def _thumb_rect(self):
        """缩略图在控件里的居中矩形（等比）。"""
        if self._thumb is None:
            return QRectF(0, 0, 0, 0)
        tw, th = self._thumb.width(), self._thumb.height()
        k = min(self.width() / max(1, tw), self.height() / max(1, th))
        w, h = tw * k, th * k
        return QRectF((self.width() - w) / 2.0, (self.height() - h) / 2.0, w, h)

    def paintEvent(self, _ev):        # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        tr = self._thumb_rect()
        if tr.isEmpty():
            p.end()
            return
        # 底板（半透明深色 + 圆角），保证在任何画面上都能看清
        bg = QPainterPath()
        bg.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0), 8.0, 8.0)
        p.fillPath(bg, QColor(0, 0, 0, 150))
        # 缩略图
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(tr.adjusted(-1, -1, 1, 1), 4.0, 4.0)
        p.setClipPath(clip)
        p.drawImage(tr, self._thumb)
        p.restore()
        # 视野框（框外压暗 + 白色描边）
        x, y, w, h = self._view
        vr = QRectF(tr.left() + x * tr.width(), tr.top() + y * tr.height(),
                    w * tr.width(), h * tr.height())
        shade = QPainterPath()
        shade.addRect(tr)
        hole = QPainterPath()
        hole.addRect(vr)
        p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 110))
        pen = QPen(QColor("#FFFFFF"))
        pen.setWidthF(1.6)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(vr)
        p.end()

    # ---------------------------------------------------------------- 交互
    def _hit_view(self, pos) -> bool:
        tr = self._thumb_rect()
        x, y, w, h = self._view
        vr = QRectF(tr.left() + x * tr.width(), tr.top() + y * tr.height(),
                    w * tr.width(), h * tr.height())
        return vr.adjusted(-4, -4, 4, 4).contains(QPointF(pos))

    def mousePressEvent(self, ev):    # noqa: N802
        if ev.button() == Qt.LeftButton:
            tr = self._thumb_rect()
            x, y, w, h = self._view
            cx = tr.left() + (x + w / 2.0) * tr.width()
            cy = tr.top() + (y + h / 2.0) * tr.height()
            self._drag = (float(ev.position().x()) - cx, float(ev.position().y()) - cy)
            self.setCursor(Qt.ClosedHandCursor)
            if not self._hit_view(ev.position()):
                # 点框外 ⇒ 直接把视野框中心挪过去
                self._drag = (0.0, 0.0)
                self._emit_center(ev.position())
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):     # noqa: N802
        if self._drag is not None:
            self._emit_center(QPointF(ev.position().x() - self._drag[0],
                                      ev.position().y() - self._drag[1]))
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

    def _emit_center(self, pos):
        tr = self._thumb_rect()
        if tr.isEmpty():
            return
        nx = (float(pos.x()) - tr.left()) / max(1.0, tr.width())
        ny = (float(pos.y()) - tr.top()) / max(1.0, tr.height())
        self.pan_to.emit(max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny)))


class MediaInfoOverlay(QWidget):
    """Tab 键呼出的「媒体信息」——**纯文字 + 柔和阴影**，没有底板（PotPlayer 那种）。

    * 用户明确要求："只需要文字，不需要浮窗，用文字模糊阴影保证易读性即可，不需要描边"；
    * 所以它不是一个玻璃面板，而是把每行字**直接画在画面上**，
      靠 `QGraphicsDropShadowEffect`（大模糊半径、纯黑、无偏移）做一圈柔和暗晕 ——
      亮画面暗画面都读得清，而且没有"框"；
    * 文字用白色：它只会在有画面时出现，白色 + 暗晕是唯一在任意画面上都成立的选择；
    * **不吃鼠标事件**：盖在画面上，绝不能挡住拖拽、双击全屏、右键菜单。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MediaInfoOverlay")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # ④ 用户："尺寸换行后被截断了，你就不能完整显示以后再显示下一个信息吗" ——
        #    固定宽度 + 值标签限宽会把换行后的第二行裁掉。改成"最小宽度 + 允许长宽"，
        #    让布局按内容撑开（`adjustSize` 在 set_info 末尾已经调用）。
        self.setMinimumWidth(M["media_info_w"])
        self.setMaximumWidth(int(M["media_info_w"] * 1.8))
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        self.title = QLabel("—")
        # ⑨ 用户："信息显示不全 若文件名过长会被自动截断 不会自动换行"
        self.title.setWordWrap(True)
        col.addWidget(self.title)
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(3)
        col.addLayout(self.grid)
        self.setVisible(False)

    @staticmethod
    def _shadow(label: QLabel) -> QLabel:
        """柔和暗晕：模糊半径给足、颜色纯黑、零偏移 —— 就是"文字模糊阴影"。"""
        eff = QGraphicsDropShadowEffect(label)
        eff.setBlurRadius(16.0)
        eff.setColor(QColor(0, 0, 0, 235))
        eff.setOffset(0.0, 0.0)
        label.setGraphicsEffect(eff)
        return label

    def set_info(self, title: str, rows) -> None:
        """rows: [(键, 值), ...]"""
        tf = QFont(self.font())
        tf.setPixelSize(13)
        tf.setWeight(QFont.Medium)
        self.title.setFont(tf)
        self.title.setStyleSheet("color:#FFFFFF;background:transparent;")
        self._shadow(self.title)
        # ⑨ 不再 elide：完整文件名换行显示（长了就占两行，总比看不全好）
        self.title.setText(title or "—")
        self.title.setToolTip(title or "")
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for i, (k, v) in enumerate(rows):
            lk = QLabel(str(k))
            lk.setStyleSheet("color:rgba(255,255,255,190);font-size:12px;background:transparent;")
            lv = QLabel(str(v))
            lv.setStyleSheet("color:#FFFFFF;font-size:12px;background:transparent;")
            # ⑨ 长值（例如"文件地址"）也要换行，否则一样被截断
            lv.setWordWrap(True)
            # ④ 不给死上限（原来 max(200, w-76) 会把长值挤成多行后又被裁）；
            #    只保证一个下限，剩下的交给布局。
            lv.setMinimumWidth(180)
            lv.setTextInteractionFlags(Qt.NoTextInteraction)
            self._shadow(lk)
            self._shadow(lv)
            self.grid.addWidget(lk, i, 0, Qt.AlignLeft | Qt.AlignTop)
            self.grid.addWidget(lv, i, 1, Qt.AlignLeft | Qt.AlignTop)
        self.adjustSize()


class AudioCard(GlassFrame):
    """音频模式的极简控制卡片：内嵌专辑封面 + 标题/艺术家 + 播放控制。"""

    play_pause = Signal()
    prev = Signal()
    next = Signal()
    seek = Signal(float)
    reveal = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, radius=M["radius_lg"], fill=P["glass_card"],
                         border=P["hairline_strong"], shadow=0.0, shadow_spread=30,
                         pad=(26, 26, 26, 24))
        self.setObjectName("AudioCard")
        self.set_body_width(M["audio_card_w"])
        self._cover_px: QPixmap | None = None
        cover_side = M["audio_card_w"] - 2 * 26 - 8      # 26 是卡片内边距, 8 是余量

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        self.cover = QLabel()
        self.cover.setFixedSize(cover_side, cover_side)
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setStyleSheet(
            f"background:{css(P['bg_deep'])};border:1px solid {css(P['hairline_strong'])};"
            f"border-radius:{M['radius_lg']}px;color:{P['text_muted']};font-size:12px;")
        self.cover.setText("无内嵌封面")
        col.addWidget(self.cover, 0, Qt.AlignHCenter)

        self.title = QLabel("—")
        tf = QFont(self.font())
        tf.setPixelSize(17)
        tf.setWeight(QFont.Medium)
        self.title.setFont(tf)
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setWordWrap(False)
        col.addWidget(self.title)

        self.artist = QLabel("")
        self.artist.setAlignment(Qt.AlignCenter)
        self.artist.setProperty("role", "dim")
        col.addWidget(self.artist)

        self.album = QLabel("")
        self.album.setAlignment(Qt.AlignCenter)
        self.album.setProperty("role", "muted")
        col.addWidget(self.album)

        col.addSpacing(4)
        ctrl = QHBoxLayout()
        ctrl.setContentsMargins(0, 0, 0, 0)
        ctrl.setSpacing(10)
        ctrl.addStretch(1)
        self.b_prev = IconButton("prev", self, size=38, icon_size=20, tooltip="上一首 (PageUp)")
        self.b_prev.clicked.connect(self.prev.emit)
        self.b_play = IconButton("play", self, size=54, icon_size=26, tooltip="播放/暂停 (Space)")
        self.b_play.clicked.connect(self.play_pause.emit)
        self.b_next = IconButton("next", self, size=38, icon_size=20, tooltip="下一首 (PageDown)")
        self.b_next.clicked.connect(self.next.emit)
        for b in (self.b_prev, self.b_play, self.b_next):
            ctrl.addWidget(b)
        ctrl.addStretch(1)
        col.addLayout(ctrl)
        self.body_layout().addLayout(col)

    def set_cover(self, data: bytes | None):
        if not data:
            self._cover_px = None
            self.cover.setText("无内嵌封面")
            self.cover.setPixmap(QPixmap())
            return
        pm = QPixmap()
        if pm.loadFromData(data):
            self._cover_px = pm.scaled(self.cover.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.cover.setText("")
            self.cover.setPixmap(self._cover_px)
        else:
            self._cover_px = None
            self.cover.setText("封面解码失败")
            self.cover.setPixmap(QPixmap())

    def set_info(self, title: str, artist: str, album: str, paused: bool):
        fm = QFontMetrics(self.title.font())
        self.title.setText(fm.elidedText(title or "未知曲目", Qt.ElideMiddle, 300))
        self.title.setToolTip(title or "")
        self.artist.setText(artist or "未知艺术家")
        self.album.setText(album or "")
        self.b_play.set_icon("play" if paused else "pause")


class EmptyState(QWidget):
    """空状态：拖入文件 / 打开文件的引导。局部重绘, 不参与任何坐标计算。

    ⚠️ 三个必须守住的点（用户实测反馈"引导副标题被折断遮挡"就是这三条一起犯的）：
    1. **本控件必须拿到足够宽度**。它被父布局用 `Qt.AlignHCenter` 放进去,
       于是只能拿到自己的 sizeHint 宽度 —— 之前是 270px, 而副标题
       `maximumWidth(620)` 根本没机会生效, 文字被挤成 4 行, 高度不够就被腰斩。
       这里显式把宽度钉在 [MIN_W, MAX_W]。
    2. **换行标签要用 WrappedLabel**, 它的 minimumSizeHint 会顶到真实需要的高度,
       布局器压不动它（普通 wordWrap QLabel 是压得动的）。
    3. **副标题拆成两行短句**, 每行在 560px 宽下都能单行放下, 不依赖换行也能完整显示。
    """

    open_file = Signal()
    open_folder = Signal()
    recent_picked = Signal(str)

    MIN_W = 560
    MAX_W = 660
    MAX_RECENT = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setMinimumWidth(self.MIN_W)
        self.setMaximumWidth(self.MAX_W)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        col.addStretch(1)

        self.logo = QLabel()
        self.logo.setPixmap(icons.render_pixmap("playlist", P["accent"], 56,
                                                self.devicePixelRatioF() or 2.0, 1.3))
        self.logo.setAlignment(Qt.AlignCenter)
        col.addWidget(self.logo, 0, Qt.AlignHCenter)

        t = QLabel("把视频 / 图片 / 音频拖到这里")
        tf = QFont(self.font())
        tf.setPixelSize(19)
        tf.setWeight(QFont.Medium)
        t.setFont(tf)
        t.setAlignment(Qt.AlignCenter)
        col.addWidget(t)

        # 副标题拆成两行 —— 每行都短, 即使容器再窄也只会安全换行, 不会被腰斩
        self.sub1 = WrappedLabel("也可以直接双击文件打开")
        self.sub1.setProperty("role", "hint")
        col.addWidget(self.sub1)          # ← 不加对齐标志：要撑满容器宽度, 否则只会拿到
        self.sub2 = WrappedLabel(         #   sizeHint 宽度（~269px）而被硬折成两行
            "视频自动出时间轴 · 图片自动出缩放工具条 · 音频自动出封面卡片")
        self.sub2.setProperty("role", "hint")
        col.addWidget(self.sub2)

        btns = QHBoxLayout()
        btns.setAlignment(Qt.AlignCenter)
        btns.setSpacing(12)
        # ⚠️ 两个按钮必须同色，而且要用**一级文字色**。
        #    用 text_dim 时在浅色底上偏浅（用户反复反馈："打开文件夹""最近打开的文件"
        #    文字又变浅了）—— 空状态是首屏，可读性优先。
        b1 = TextIconButton("打开文件", self, height=38, pad=22, font_size=13,
                            color=P["text"], tooltip="Ctrl+O")
        b1.clicked.connect(self.open_file.emit)
        b2 = TextIconButton("打开文件夹", self, height=38, pad=22, font_size=13,
                            color=P["text"], tooltip="Ctrl+Shift+O")
        b2.clicked.connect(self.open_folder.emit)
        btns.addWidget(b1)
        btns.addWidget(b2)
        col.addSpacing(2)
        col.addLayout(btns)

        col.addSpacing(4)
        self.recent_head = QLabel("最近打开")
        self.recent_head.setProperty("role", "muted")
        self.recent_head.setAlignment(Qt.AlignCenter)
        col.addWidget(self.recent_head, 0, Qt.AlignHCenter)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(4)
        self.recent_box.setAlignment(Qt.AlignCenter)
        col.addLayout(self.recent_box)
        col.addStretch(1)
        self._recent_widgets: list[QWidget] = []

    def set_recent(self, files: list[str], max_show: int = MAX_RECENT):
        """设置"最近打开"。

        最近打开是**可选装饰**：先算出父容器真正能给多少高度，再决定放几条。
        宁可少放几条，也绝不让它把上面的引导文字挤变形。
        """
        self._recent_all = [p for p in (files or []) if p]
        self._recent_max = max_show
        self._sync_recent()

    def _avail_h(self) -> int:
        """父容器（middle_layer）能给本控件的高度；布局还没跑起来时给个宽松默认。"""
        par = self.parentWidget()
        if par is None or par.height() <= 0:
            return 620
        lay = par.layout()
        m = lay.contentsMargins() if lay is not None else None
        margins = (m.top() + m.bottom()) if m is not None else 20
        return max(120, par.height() - margins)

    def _recent_h(self, n: int) -> int:
        if n <= 0:
            return 0
        step = 28 + self.recent_box.spacing()
        head = self.recent_head.sizeHint().height() + self.recent_box.spacing()
        return head + n * step

    def _fixed_h(self) -> int:
        lay = self.layout()
        if lay is None:
            return 0
        h = 0
        for i in range(lay.count()):
            it = lay.itemAt(i)
            w = it.widget()
            if w is None or w in (self.recent_head,) or w in self._recent_widgets:
                continue
            h += max(w.sizeHint().height(), w.minimumSizeHint().height())
        h += lay.spacing() * max(0, lay.count() - len(self._recent_widgets) - 2)
        return h

    def _sync_recent(self):
        files = getattr(self, "_recent_all", [])
        max_show = getattr(self, "_recent_max", self.MAX_RECENT)
        avail = self._avail_h()
        fixed = self._fixed_h()
        show = min(max_show, len(files))
        while show > 0 and fixed + self._recent_h(show) > avail:
            show -= 1
        if show == len(self._recent_widgets) and show > 0:
            return                       # 已经是这个条数, 不要无谓地重建控件
        for w in self._recent_widgets:
            self.recent_box.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        self._recent_widgets.clear()
        self.recent_head.setVisible(show > 0)
        if show <= 0:
            return
        fm = QFontMetrics(self.font())
        for path in files[:show]:
            name = os.path.basename(path)
            b = TextIconButton(fm.elidedText(name, Qt.ElideMiddle, 420), self,
                               height=28, pad=14, font_size=12, tooltip=path)
            b.clicked.connect(lambda p=path: self.recent_picked.emit(p))
            self.recent_box.addWidget(b, 0, Qt.AlignHCenter)
            self._recent_widgets.append(b)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if getattr(self, "_recent_all", None) and self.height() < self.sizeHint().height():
            self._sync_recent()


class DrawerHandle(QWidget):
    """贴在**抽屉与画面的接缝**上的常驻手柄：收起时在窗口右边缘，展开时在抽屉左边缘。

    为什么要它：光靠"鼠标碰到右边缘就自动展开"是不够的 —— 用户用标题栏按钮或键盘
    收起抽屉时，鼠标往往正好停在右边缘，自动展开逻辑立刻把抽屉弹回来，
    那个收起按钮就等于摆设。爱奇艺同样在右侧留了一个常驻小按钮来展开列表。

    ⚠️ 展开态的收起**也用这一个手柄**。早先抽屉内部右上角另有一个收起按钮，
    用户反馈"展开以后收起跑到右上角了很割裂" —— 同一个动作应该永远是同一个控件、
    永远在同一条接缝上。手柄在布局里排在 slot 左边，所以收起态它落在窗口右边缘、
    展开态它落在抽屉左边缘，位置是连续的、不会跳。
    """

    clicked = Signal()
    W = 20
    H = 74

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("展开 / 收起播放列表（L）")
        self.setAttribute(Qt.WA_Hover, True)
        self._hover = False
        self._open = False

    def set_open(self, on: bool):
        on = bool(on)
        if on != self._open:
            self._open = on
            self.update()

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.W - 0.5, self.H - 1.0)
        rad = 9.0
        # 形状始终是"左边圆角、右边平边"：平边永远贴着它右边的东西
        # （收起态贴窗口右边缘，展开态贴抽屉左边缘），所以手柄不用换形状、只换箭头。
        path = QPainterPath()
        path.moveTo(r.right(), r.top())
        path.lineTo(r.left() + rad, r.top())
        path.quadTo(r.left(), r.top(), r.left(), r.top() + rad)
        path.lineTo(r.left(), r.bottom() - rad)
        path.quadTo(r.left(), r.bottom(), r.left() + rad, r.bottom())
        path.lineTo(r.right(), r.bottom())
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(qt_color(P["glass_capsule"]))
        p.drawPath(path)
        pen = QPen(qt_color(P["accent"] if self._hover else P["inner_glow"]))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        # ⚠️ 箭头方向（按爱奇艺的约定）：**收起态指左 `‹`、展开态指右 `›`**。
        #    箭头指的是"面板接下来会往哪边动"：收起时点一下，面板向左滑出来；
        #    展开时点一下，面板向右收回去。我第一版写反了（收起态用了 `›`），
        #    用户一眼就指出"原来的手柄箭头方向还错了"。
        name = "chevron_left" if not self._open else "chevron_right"
        pm = icons.render_pixmap(name, P["accent"] if self._hover else P["text_dim"],
                                 14, self.devicePixelRatioF() or 2.0, 1.9)
        p.drawPixmap(QPointF((self.W - 14) / 2.0, (self.H - 14) / 2.0), pm)
        p.end()


class DropOverlay(QWidget):
    """拖放文件时的高亮层（虚线圆角框 + 提示文字）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setVisible(False)

    def paintEvent(self, _ev):
        from PySide6.QtGui import QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(14, 14, -14, -14)
        p.setBrush(qt_color(P["accent_soft"]))
        pen = QPen(qt_color(P["accent"]))
        pen.setWidthF(2.0)
        pen.setDashPattern([7, 5])
        p.setPen(pen)
        p.drawRoundedRect(r, 16, 16)

        text = "松开即可播放"
        f = QFont(self.font())
        f.setPixelSize(18)
        f.setWeight(QFont.Medium)
        p.setFont(f)
        p.setPen(qt_color(P["text"]))
        p.drawText(r, Qt.AlignCenter, text)
        p.end()


class Stage(QWidget):
    """内容舞台。"""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.setObjectName("Stage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAcceptDrops(True)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)

        # ---- 底层：mpv 画面 ----
        # ⚠️ 先**隐藏**：QOpenGLWidget 一旦可见就会在首次绘制时同步创建 GL 上下文 +
        #    mpv 渲染上下文，在本机实测要 ~1 秒，而且这段时间全卡在 window.show() 里 ——
        #    用户看到的就是"双击图标后三秒钟什么都没有"。
        #    先让窗口把 UI 画出来，再由 ensure_video() 把画面接上（app.py 在 show() 之后调）。
        self.video = MpvRenderWidget(engine, self)
        self.video.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        grid.addWidget(self.video, 0, 0, 4, 1)
        self.video.hide()
        self._video_ready = False

        # ---- 第 0 行：顶部浮层 ----
        self._edge_zone_px = 0
        self._kind = ""
        self.top_layer = QWidget(self)
        self.top_layer.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        tl = QHBoxLayout(self.top_layer)
        tl.setContentsMargins(16, 14, 16, 0)
        tl.setSpacing(0)
        self.image_bar = ImageInfoBar(self)          # 父级先挂舞台，稍后放进底部行
        self.image_bar.setVisible(False)
        # Tab 呼出的媒体信息浮层 —— **贴左上角**（PotPlayer 的位置），
        # 所以它排在拉伸项**之前**，右边的 image_bar 继续居中。
        self.media_info = MediaInfoOverlay(self.top_layer)
        tl.addWidget(self.media_info, 0, Qt.AlignLeft | Qt.AlignTop)
        tl.addStretch(1)
        tl.addStretch(1)
        grid.addWidget(self.top_layer, 0, 0)

        # ---- 第 1 行：中部浮层 ----
        self.middle_layer = QWidget(self)
        ml = QVBoxLayout(self.middle_layer)
        ml.setContentsMargins(20, 10, 20, 10)
        ml.setSpacing(10)
        ml.addStretch(1)
        self.empty_state = EmptyState(self.middle_layer)
        ml.addWidget(self.empty_state, 0, Qt.AlignHCenter)
        self.audio_card = AudioCard(self.middle_layer)
        self.audio_card.setVisible(False)
        ml.addWidget(self.audio_card, 0, Qt.AlignHCenter)
        ml.addStretch(1)
        grid.addWidget(self.middle_layer, 1, 0)

        # ---- 设置面板：**独立的覆盖层，不参与中间层的纵向布局** ----
        # ⚠️ 它原来和 empty_state / audio_card 挤在 middle_layer 的同一个 QVBoxLayout 里，
        #    一打开就要和落地页抢纵向空间 —— 落地页被顶上去、内容被挤扁
        #    （用户截图指出"怎么还会把主页的东西给顶上去呢，这个不应该相当于另起一个窗口的逻辑吗"）。
        #    现在它挂在 (0,0,4,1) 这个**跨四行的大格子**里居中：格子尺寸早被视频撑满，
        #    面板再大也不会推动任何兄弟，行为上就等于"另起一层"。
        self.settings_host = QWidget(self)
        sh = QVBoxLayout(self.settings_host)
        sh.setContentsMargins(0, 0, 0, 0)
        self.settings_host.setVisible(False)
        grid.addWidget(self.settings_host, 0, 0, 4, 1, Qt.AlignCenter)

        # ---- 第 2 行：底部浮层 ----
        self.bottom_layer = QWidget(self)
        bl = QHBoxLayout(self.bottom_layer)
        bl.setContentsMargins(16, 0, 16, 14)
        bl.setSpacing(10)
        bl.addStretch(1)
        self.subtitle_host = QWidget(self.bottom_layer)
        sub = QVBoxLayout(self.subtitle_host)
        sub.setContentsMargins(0, 0, 0, 0)
        self.subtitle_host.setVisible(False)
        bl.addWidget(self.subtitle_host, 0, Qt.AlignBottom)
        grid.addWidget(self.bottom_layer, 2, 0)

        # ---- OSD：**必须浮在视频区之上, 而不是占一行** ----
        # ⚠️ 以前 OSD 放在 bottom_layer 那一行里, 它一出现就把第 2 行从 14px 撑到 44px,
        #    网格只好从第 1 行（设置面板所在行）扣掉 30px —— 用户看到的就是
        #    "调音量时设置界面被挤下去"。现在它和视频共用 (0,0,3,1) 这个跨三行的大格,
        #    该格子的尺寸早已被视频撑满, OSD 再高也不会改变任何一行的分配。
        self.osd_host = QWidget(self)
        oh = QVBoxLayout(self.osd_host)
        oh.setContentsMargins(0, 0, 0, 0)
        oh.setSpacing(0)
        self.osd = OsdBadge(self.osd_host)
        self.osd.hide()                      # 否则开局就在底部挂一个空气泡
        oh.addWidget(self.osd, 0, Qt.AlignHCenter)
        oh.addSpacing(18)
        self.osd_host.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.osd_host.setVisible(False)
        grid.addWidget(self.osd_host, 0, 0, 4, 1, Qt.AlignHCenter | Qt.AlignBottom)

        # ---- 播放列表抽屉的挂载点（覆盖层）+ 接缝手柄 ----
        # 它和视频共用跨三行的大格子 + 右对齐：格子的尺寸早已被视频撑满，
        # 所以抽屉宽度怎么变都不会推动视频 —— 这正是"展开动画不掉帧"的关键。
        # minimumWidth 必须是 0，否则抽屉的宽度会顶进格子的最小尺寸里，又开始推动视频。
        self.drawer_host = QWidget(self)
        self.drawer_host.setMinimumWidth(0)
        dh = QHBoxLayout(self.drawer_host)
        dh.setContentsMargins(0, 0, 0, 0)
        dh.setSpacing(0)
        dh.addStretch(1)
        # ⚠️ 手柄排在 slot **左边**、和它同一个布局：收起时 slot 宽 0 ⇒ 手柄正好落在
        #    窗口右边缘；展开时 slot 变宽 ⇒ 手柄跟着被推到抽屉左边缘。
        #    这样"展开"和"收起"永远是同一个控件、永远在同一条接缝上（用户反馈过
        #    "展开以后收起跑到右上角了很割裂"）。
        self.drawer_handle = DrawerHandle(self.drawer_host)
        dh.addWidget(self.drawer_handle, 0, Qt.AlignVCenter)
        self.drawer_slot = QWidget(self.drawer_host)
        self.drawer_slot.setMinimumWidth(0)
        self.drawer_slot.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        dsl = QHBoxLayout(self.drawer_slot)
        dsl.setContentsMargins(0, 0, 0, 0)
        dsl.setSpacing(0)
        dh.addWidget(self.drawer_slot)
        grid.addWidget(self.drawer_host, 0, 0, 3, 1, Qt.AlignRight)

        # ---- 第 3 行：控制条区（胶囊浮在这里, 视频在它后面一直铺到窗口底） ----
        # 用户要的效果：视频铺满整个画面区（不因为控制条而"卡在上面"），
        # 控制条浮在视频之上，无操作时淡出 —— 这样既不影响读信息，也没有"视频被压缩"的割裂感。
        # 图片模式的信息/操作栏：与控制条**同一行**（第 3 行），二选一显示 ——
        # 用户要求"看图时底部控制栏去掉，把顶部图片控制栏挪到下面代替"。
        self.image_bar_host = QWidget(self)
        ibh = QHBoxLayout(self.image_bar_host)
        ibh.setContentsMargins(0, 0, 0, 0)
        ibh.addStretch(1)
        ibh.addWidget(self.image_bar, 0, Qt.AlignBottom)
        ibh.addStretch(1)
        self.image_bar_host.setVisible(False)
        grid.addWidget(self.image_bar_host, 3, 0)

        # ① 右下角导航取景框（图片放大时出现）
        self.navigator = ImageNavigator(self)
        self.navigator.setVisible(False)
        self._top_layers = []          # 由主窗登记的"必须能点到"的控件（见 _raise_top_layers）
        grid.addWidget(self.navigator, 2, 0, Qt.AlignRight | Qt.AlignBottom)

        # ⑥ 左右热区（图片模式专用；宽度跟设置走）
        self.zone_left = EdgeZone("left", self)
        self.zone_right = EdgeZone("right", self)
        for z, al in ((self.zone_left, Qt.AlignLeft), (self.zone_right, Qt.AlignRight)):
            z.setFixedWidth(0)
            z.setVisible(False)
            grid.addWidget(z, 0, 0, 4, 1, al)

        self.capsule_host = QWidget(self)
        ch = QHBoxLayout(self.capsule_host)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(0)
        self.capsule_host.setVisible(False)
        grid.addWidget(self.capsule_host, 3, 0)

        # 拖放高亮
        self._landing_wanted = True     # 当前是不是空状态页面（主窗口会更新）
        self.drop_overlay = DropOverlay(self)
        self.drop_overlay.setGeometry(self.rect())
        grid.addWidget(self.drop_overlay, 0, 0, 4, 1)

        grid.setRowStretch(0, 0)
        grid.setRowStretch(1, 1)
        grid.setRowStretch(2, 0)
        grid.setRowStretch(3, 0)
        # 浮层永远压在画面之上
        self.top_layer.raise_()
        self.middle_layer.raise_()
        self.bottom_layer.raise_()
        self.drop_overlay.raise_()

    def set_edge_zone_width(self, px: int) -> None:
        """设置左右热区宽度（0 = 关闭）。由主窗在启动/改设置时调用。"""
        self._edge_zone_px = max(0, int(px or 0))
        if getattr(self, "zone_left", None) is not None:
            self.set_media_mode(getattr(self, "_kind", ""))

    def set_media_mode(self, kind: str) -> None:
        """按媒体类型切换**底部那一行**：图片=图片栏，音视频=播放控制条。

        ⚠️ 两者放在同一个网格行里、同一时刻只显示一个 —— 这样切换类型时
           布局不会跳（用户看的是"同一个位置换了内容"，而不是面板在动）。
        """
        is_image = str(kind) == "image"
        try:
            zw = int(self._edge_zone_px or 0)
            for z in (self.zone_left, self.zone_right):
                z.setFixedWidth(zw if is_image else 0)
                z.setVisible(bool(is_image and zw > 0))
                z.set_hot(False)
                # ⚠️⚠️ 必须 raise 到最上层：实测 `widgetAt()` 命中的是**另一个匿名 QWidget**
                #    （top_layer / drop_overlay 铺满整个舞台）⇒ 真实鼠标永远到不了热区，
                #    用户"左右热区不见起作用"就是这个原因。探针当时直接调 set_hot()，
                #    绕过了命中测试，所以假通过了。
                if is_image and zw > 0:
                    z.raise_()
                    # ①⑥ 用户："热区层级太高了，比播放列表、手柄还高" ——
                    #    raise 之后必须把**抽屉手柄**再抬回来，否则手柄压在热区下面点不到，
                    #    而且鼠标想试导航取景框时会先撞到热区 ⇒ 直接切图。
                    try:
                        self.drawer_handle.raise_()
                    except Exception:
                        pass
            self.image_bar.setVisible(is_image)
            if not is_image:
                self.navigator.setVisible(False)
            self.image_bar_host.setVisible(is_image)
            self.capsule_host.setVisible(not is_image)
            if is_image:
                self.image_bar_host.raise_()
            else:
                self.capsule_host.raise_()
        except Exception:
            log.debug("切换媒体模式布局失败", exc_info=True)

    def attach_capsule(self, capsule, margin_lr: int, margin_b: int, shadow: int) -> None:
        """把控制条胶囊挂到舞台第 3 行（视频仍然在它后面铺满整个舞台）。

        胶囊控件本身含 `shadow` 厚度的阴影外框，所以外边距要扣掉它，
        用户看到的胶囊边缘才会正好是 `margin_lr` / `margin_b`。
        """
        self.capsule = capsule
        lay = self.capsule_host.layout()
        lay.setContentsMargins(max(0, margin_lr - shadow), 0, max(0, margin_lr - shadow), 0)
        lay.addWidget(capsule)
        cap_h = capsule.sizeHint().height()
        self.capsule_host.setFixedHeight(cap_h)
        self.capsule_host.setVisible(True)
        capsule.setVisible(True)
        # 字幕条要让开胶囊：它原来贴在第 2 行底部, 而第 2 行底部正好是胶囊控件的顶边,
        # 字幕下沿离胶囊可见上沿只剩一个阴影厚度（16px）, 实测会压在胶囊上。
        # 直接把胶囊那一整条的高度让出来。
        bl = self.bottom_layer.layout()
        bl.setContentsMargins(16, 0, 16, cap_h + 6)
        # OSD 同理：它原来贴在舞台最底部（只留 18px），整个被胶囊盖住 ——
        # 用户反馈"调整音量、进度条的这些悬浮弹窗都是在底下的被挡住了"。
        oh = self.osd_host.layout()
        last = oh.itemAt(oh.count() - 1) if oh.count() else None
        if last is not None and last.spacerItem() is not None:
            last.spacerItem().changeSize(0, cap_h + 10)
        else:
            oh.addSpacing(cap_h + 10)

    def ensure_video(self):
        """把视频画面接上（GL 上下文在这里才真正创建）。

        由 app.py 在 `window.show()` **之后**调用：这样"窗口出现"与"画面就绪"两件事
        彻底解耦，用户先看到完整的界面，画面晚 ~1 秒跟上，而不是干等三秒黑屏。
        """
        if getattr(self, "_video_ready", False):
            return
        self._video_ready = True
        import time as _t

        t0 = _t.perf_counter()
        self.video.setVisible(True)
        self.video.lower()               # 画面永远垫在最底层
        log.info("  [窗口] 视频面就绪(GLen 上下文)  %6.0f ms", (_t.perf_counter() - t0) * 1000)

    def attach_drawer(self, drawer) -> None:
        """把播放列表抽屉挂成舞台右侧的**覆盖层**（不参与视频的尺寸分配）。

        抽屉的宽度由 `drawer_slot` 承担：动画只改 slot 的宽度，
        抽屉自己保持目标宽度、被 slot 裁切 —— 动画期间抽屉内部一次布局都不跑，
        视频控件也一格都不动。实测动画从 11fps 提到 60fps。
        """
        self.drawer = drawer
        drawer._slot = self.drawer_slot
        self.drawer_slot.layout().addWidget(drawer)
        # 抽屉自己先撑到目标宽度（只跑这一次内部布局），宽度动画交给 slot
        drawer.setFixedWidth(int(getattr(drawer, "_target_w", 0) or 0))
        drawer.set_drawer_width(0)

    def set_landing_visible(self, on: bool) -> None:
        """落地页（空状态）显隐。

        设置面板打开时必须收起它 —— 两者都是"铺在画面上的一层"，
        同时显示会互相压字，信息完全读不了。
        """
        self.empty_state.setVisible(bool(on) and self._landing_wanted)

    def set_landing_wanted(self, wanted: bool) -> None:
        """记录"当前是不是空状态页面"（由主窗口在切换形态时调用）。"""
        self._landing_wanted = bool(wanted)

    def raise_settings(self) -> None:
        """把设置面板抬到最上层。

        ⚠️ 它挂的是跨四行的大格子，如果只 `raise_()` 它自己，
        `middle_layer`（落地页/音频卡）在构建时被 raise 过，仍然会压在它上面。
        """
        self.settings_host.raise_()
        if getattr(self, "settings", None) is not None:
            self.settings.raise_()

    def set_drawer_visible(self, on: bool) -> None:
        on = bool(on)
        self.drawer_host.setVisible(True)          # 挂载点常驻（手柄在里面）
        self.drawer_handle.set_open(on)
        # ⚠️ 手柄**任何模式都要在**。这里原来写的是
        #    `setVisible(not getattr(self, "_fullscreen", False))`，而 `_fullscreen`
        #    是 MainWindow 的属性、Stage 上根本没有 ⇒ 恒为 False（永远显示），
        #    与 MainWindow.toggle_fullscreen() 里那句"全屏隐藏手柄"互相打架，
        #    最终全屏下没有边缘入口能打开播放列表（用户："全屏时播放列表手柄消失"）。
        self.drawer_handle.setVisible(True)
        if on:
            self.drawer_host.raise_()
            self.drawer_handle.raise_()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.drop_overlay.setGeometry(self.rect())

    # ---- 拖放 --------------------------------------------------------------
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            self.drop_overlay.setVisible(True)
            self.drop_overlay.raise_()

    def dragLeaveEvent(self, ev):
        self.drop_overlay.setVisible(False)
        super().dragLeaveEvent(ev)

    def dropEvent(self, ev):
        self.drop_overlay.setVisible(False)
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.window().handle_dropped_paths(paths)
        ev.acceptProposedAction()
