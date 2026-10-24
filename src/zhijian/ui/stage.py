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

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
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
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..media.scanner import human_size
from ..mpv import MpvRenderWidget
from . import icons
from .theme import M, P, css, is_light, qt_color
from .widgets import (GlassFrame, IconButton, LogSlider, NoWheelSlider, OsdBadge, TextIconButton,
                      WrappedLabel, vspace)

log = logging.getLogger(__name__)


class ImageInfoBar(GlassFrame):
    """图片模式下的顶部信息条：文件名 / 分辨率 / 大小 / 缩放比例。"""

    zoom_in = Signal()
    zoom_out = Signal()
    zoom_changed = Signal(float)      # 滑条：目标缩放倍数（1.0 = 100% = 适应窗口）
    fullscreen_toggled = Signal()     # #9 全屏按钮
    playlist_toggled = Signal()       # v1.0.17 播放列表按钮（看图模式用它替代手柄）
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

        # #7：文件名与像素移到**标题栏**，这里不再重复展示（用户要求）。
        #    图片栏只保留"操作类"信息：序号 + 缩放比例 + 按钮。
        self.idx_label = QLabel("")
        self.idx_label.setProperty("role", "muted")
        row.addWidget(self.idx_label)
        row.addSpacing(10)

        def btn(name, tip, sig, size=30, icon=16, checkable=False):
            b = IconButton(name, self, size=size, icon_size=icon, tooltip=tip, checkable=checkable)
            if sig is not None:
                b.clicked.connect(sig.emit)
            row.addWidget(b)
            return b

        # ---- 左段：只有缩放（#2 用户要求：全屏按钮挪到最右）----
        btn("zoom_out", "缩小 (滚轮下 / Ctrl+-)", self.zoom_out)
        zbox = QVBoxLayout()
        zbox.setContentsMargins(0, 0, 0, 0)
        zbox.setSpacing(1)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_label.setProperty("role", "dim12")
        zbox.addWidget(self.zoom_label)
        # #6 用户要求：胶囊更紧凑 ⇒ 滑条宽度收到 96px
        self.zoom_slider = LogSlider(self, width=96, lo=0.1, hi=10.0)
        self.zoom_slider.setObjectName("ZoomSlider")
        self.zoom_slider.setToolTip("缩放 10% ~ 1000%（100% = 适应窗口）")
        self.zoom_slider.set_scale(1.0)
        self.zoom_slider.valueChanged.connect(
            lambda _v: self.zoom_changed.emit(self.zoom_slider.scale()))
        zbox.addWidget(self.zoom_slider)
        row.addLayout(zbox)
        btn("zoom_in", "放大 (滚轮上 / Ctrl+=)", self.zoom_in)
        row.addSpacing(8)

        # ---- 右段：切图 ----
        btn("prev_image", "上一张 (←)", self.prev_image)
        btn("next_image", "下一张 (→)", self.next_image)
        row.addSpacing(8)
        btn("rotate_left", "向左旋转 (R / Shift+R)", self.rotate_left)
        btn("rotate_right", "向右旋转 (E)", self.rotate_right)
        btn("flip_h", "水平镜像 (H)", self.flip_h)      # #8 图标语义修正
        # #6 用户要求去掉「原始大小」，只保留「适应窗口」
        btn("fit", "适应窗口 (1)", self.zoom_fit)
        btn("folder", "在资源管理器中显示", self.reveal)
        # ---- 最右：播放列表（看图模式下用它代替抽屉手柄）+ 全屏 ----
        btn("playlist", "播放列表 (L)", self.playlist_toggled)
        row.addSpacing(4)
        self.btn_fullscreen = btn("fullscreen", "全屏 (F / 双击画面)",
                                  self.fullscreen_toggled)
        self.body_layout().addLayout(row)

    def set_fullscreen(self, on: bool):
        """#10 全屏状态双向同步：图标换成「退出全屏」，tooltip 也跟着换。"""
        try:
            self.btn_fullscreen.set_icon("fullscreen_exit" if on else "fullscreen")
            self.btn_fullscreen.setToolTip("退出全屏 (F / Esc)" if on else "全屏 (F / 双击画面)")
        except Exception:
            pass

    def set_zoom(self, scale: float):
        """**静默**同步滑条位置与百分比。

        ⚠️ 用户正在拖滑条时**绝不回写**：视口的 scale_changed 会把滑条拽回上一帧的值，
           等于和用户的手抢控制权 —— 现象就是「拖动时闪烁 + 回弹」（#14）。
        """
        try:
            if self.zoom_slider.is_dragging():
                self.zoom_label.setText("%d%%" % int(round(float(scale) * 100)))
                return
            self.zoom_slider.blockSignals(True)
            self.zoom_slider.set_scale(float(scale))
            self.zoom_slider.blockSignals(False)
            self.zoom_label.setText("%d%%" % int(round(float(scale) * 100)))
        except Exception:
            pass

    def set_info(self, name: str, width: int, height: int, size: int, zoom: float,
                 index: int, total: int):
        """#7：名字/像素已移交标题栏；这里只显示序号与缩放。

        参数保留（调用方与视频侧共用同一签名），但**不再**渲染文件名与分辨率。
        """
        try:
            self.idx_label.setText(("%d/%d" % (index, total)) if total else "")
            self.idx_label.setToolTip(name or "")
        except Exception:
            pass
        self.zoom_label.setText("%d%%" % int(round(float(zoom) * 100)))


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
        # #6：不再用 setFixedWidth —— 长值需要换行空间（宽度在 set_info 里按窗口算）
        self.setMinimumWidth(M["media_info_w"])
        col = QVBoxLayout(self)
        # #15 给柔和阴影留余量：贴边时模糊会被控件矩形裁掉（看起来像没有阴影）
        col.setContentsMargins(10, 8, 10, 10)
        col.setSpacing(4)
        self.title = QLabel("—")
        # #6：标题**允许换行**（文件名可能很长），不靠省略号截断
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
        fm = QFontMetrics(tf)
        self.title.setText(fm.elidedText(title or "—", Qt.ElideMiddle, M["media_info_w"] - 8))
        self.title.setToolTip(title or "")
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        # #6 关键：值一律**自动换行**，并且允许面板长到窗口宽度以内，
        #    绝不用省略号截断（用户明确要求："注意自动换行 不要被截断"）。
        avail = max(220, min(int(self.parent().width() * 0.62) if self.parent() else 520,
                             int(M["media_info_w"] * 2.2)))
        val_w = max(160, avail - 96)
        for i, (k, v) in enumerate(rows):
            lk = QLabel(str(k))
            lk.setStyleSheet("color:rgba(255,255,255,190);font-size:12px;background:transparent;")
            lk.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            lv = QLabel(str(v))
            lv.setStyleSheet("color:#FFFFFF;font-size:12px;background:transparent;")
            lv.setTextInteractionFlags(Qt.NoTextInteraction)
            lv.setWordWrap(True)
            lv.setMinimumWidth(val_w)
            lv.setMaximumWidth(val_w)
            lv.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            self._shadow(lk)
            self._shadow(lv)
            self.grid.addWidget(lk, i, 0, Qt.AlignLeft | Qt.AlignTop)
            self.grid.addWidget(lv, i, 1, Qt.AlignLeft | Qt.AlignTop)
        self.setMinimumWidth(M["media_info_w"])
        self.setMaximumWidth(avail)
        self.adjustSize()


class AudioCard(GlassFrame):
    """⚠️ v1.0.27 起**不再实例化**（旧版居中悬浮卡片，已被 AudioView 取代）。

    保留类定义只是为了避免第三方/旧脚本 import 报错；Stage 里已无任何实例。
    """

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


# ★★ 抽屉手柄物理规格（**唯一真源**）：手柄的尺寸只在这里定义一次 —— Stage 的几何
#    不变量（apply_handle_geometry）与 DrawerHandle 的绘制都引用它，两者永远不会再打架。
#    ⚠️ 旧代码在 DrawerHandle 里另写了 `W = 20 / H = 74`，而全局四处强制
#    setFixedSize(16, 56) ⇒ paintEvent 把 20×74 的形状画进 16×56 的控件：
#    右边/下边被控件边界硬裁、箭头中心 (3,30) 也不是真实的 (8,28) ——
#    用户看到的就是"半截手柄"。现在 paintEvent 一律按 self.width()/self.height() 画，
#    控件真实尺寸与绘制尺寸永远是同一个数，物理上不可能再被裁。
HANDLE_W = 16
HANDLE_H = 56


class DrawerHandle(QWidget):

    def on_theme_changed(self):
        """换肤回调（主题合规门禁要求）：本控件在 paintEvent 里直接读调色板，
        必须在这里重新取色并重绘，否则换肤后会留着旧主题的颜色。
        """
        try:
            self.update()
        except Exception:
            pass


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(HANDLE_W, HANDLE_H)      # 尺寸唯一真源（见模块顶部常量）
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
        # ⚠️⚠️ 几何一律按**控件真实尺寸**算（self.width()/self.height()），绝不再用类常量：
        #   旧写法（W=20/H=74）在 16×56 的控件里画 20×74 的形状，右/下被控件边界硬裁
        #   ⇒ 用户报的"半截手柄"。现在绘制尺寸与控件尺寸来自同一真源，裁切不可能发生。
        w = max(1, self.width())
        h = max(1, self.height())
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        # 半径最多到高度的一半/宽度 − 1：控件再矮再窄也只会自动退化，不会把形状算穿
        rad = min(9.0, (h - 1.0) / 2.0, max(0.0, w - 1.0))
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
        # ---- 填充：半透明「微磨砂」----
        # ⚠️ 不能直接实心填 P["glass_capsule"]：深色主题下它是 #12161FB9（alpha 185），
        #    而且 theme.apply_theme 还给它强制了 alpha ≥ 0x78 的下限 ⇒ 实心感极强，
        #    用户看到的就是"丑陋的深黑实心底板"（截图实证）。
        #    这里**当场**取主题胶囊色（每次 paintEvent 现读 P，绝不构造时快照），
        #    把 alpha 压薄成上下两个档位做纵向渐变 = 磨砂玻璃的受光面/背光面，
        #    背后的画面始终透得出来；形体靠 1px 收边立住。
        # ★★ v1.0.36（task-07）：**浅色主题专用高对比轮廓**。
        #   浅色下原来的"半透明白磨砂 + 白色内发光"在白底上退化成细弱游丝（用户报"边缘发虚"）。
        #   按卡片指定的两个色值：底板温润磨砂浅灰 QColor(230,233,238,220)、描边 QColor(0,0,0,38)。
        #   ⚠️ 仍然**每次绘制现读** is_light()（不做构造时快照），切主题立刻生效。
        # ★★★ task-102③（lead 授权：仅改本 paintEvent）：
        #   玻璃皮肤下**复用抽屉左边缘同一份实时模糊纹理** + 完全相同的
        #   P['glass_bg'] / P['glass_border'] ⇒ 手柄与抽屉浑然一体、接缝不割裂。
        from . import theme as _theme_h
        _glass_h = bool(_theme_h.is_glass_skin())
        if _glass_h:
            try:
                from .widgets import GlassBlurPlate
                _bp = GlassBlurPlate.instance()
                if not getattr(self, "_blur_registered", False):
                    _bp.register(self)
                    self._blur_registered = True
                p.save()
                p.setClipPath(path)
                _bp.paint_backdrop(p, self, QRectF(self.rect()))
                p.restore()
            except Exception:
                pass
        light = bool(_theme_h.is_light())
        if _glass_h:
            # 与抽屉完全同源：同一 P['glass_bg'] 半透玻璃底（不再另调 alpha 档位）
            base = qt_color(P.get("glass_bg") or P["glass_panel"])
            top_c = QColor(base)
            bot_c = QColor(base)
        elif light:
            base = QColor(230, 233, 238, 220)
            cap_a = 220
            top_c = QColor(base)
            bot_c = QColor(base)
        else:
            base = qt_color(P["glass_capsule"])
            cap_a = max(0, int(base.alpha()))
            bot_c = QColor(base)
            bot_c.setAlpha(max(22, min(82, int(cap_a * 0.30))))
            top_c = QColor(base)
            top_c.setAlpha(max(34, min(118, int(cap_a * 0.46))))
        grad = QLinearGradient(r.topLeft(), r.bottomLeft())
        grad.setColorAt(0.0, top_c)
        grad.setColorAt(1.0, bot_c)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPath(path)
        if self._hover:
            # hover：叠一层 accent 微光（不是换成不透明实心色）—— 主题色只在绘制时取
            glow = qt_color(P["accent"])
            glow.setAlpha(46)
            p.setBrush(QBrush(glow))
            p.drawPath(path)
        # ---- 1px 收边：常态内发光、hover 走 accent ----
        # ⚠️ 浅色主题（qq-light/qq-dark）里 inner_glow = #FFFFFF00（**全透明**）：
        #    半透明白填充 + 全透明描边 ⇒ 手柄在白底上几乎不成形。这里当场判断 alpha，
        #    退化到该主题的 hairline（浅色下是 #00000014 的深色细线），
        #    形体永远立得住；颜色依旧每次绘制现读 P（无构造时快照）。
        if _glass_h:
            # 与抽屉同款 1px 描边 token（hover 时仍叠 accent）
            edge = qt_color(P["accent"] if self._hover
                            else (P.get("glass_border") or P["hairline_strong"]))
        elif light and not self._hover:
            edge = QColor(0, 0, 0, 38)          # 卡片指定：浅色描边
        else:
            edge = qt_color(P["accent"] if self._hover else P["inner_glow"])
            if not self._hover and edge.alpha() < 24:
                edge = qt_color(P["hairline"])
        pen = QPen(edge)
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        # ⚠️ 箭头方向（按爱奇艺的约定）：**收起态指左 `‹`、展开态指右 `›`**。
        #    箭头指的是"面板接下来会往哪边动"：收起时点一下，面板向左滑出来；
        #    展开时点一下，面板向右收回去。我第一版写反了（收起态用了 `›`），
        #    用户一眼就指出"原来的手柄箭头方向还错了"。
        name = "chevron_left" if not self._open else "chevron_right"
        icon_side = 14
        pm = icons.render_pixmap(name, P["accent"] if self._hover else P["text_dim"],
                                 icon_side, self.devicePixelRatioF() or 2.0, 1.9)
        # 箭头按**真实控件尺寸**居中：16×56 ⇒ 落在 (1, 21)，控件几何中心 (8, 28)，
        # 旧写法 ((W-14)/2, (H-14)/2) = (3, 30) 是按 20×74 算的 ⇒ 箭头偏右下。
        p.drawPixmap(QPointF((w - icon_side) / 2.0, (h - icon_side) / 2.0), pm)
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

    #: ★ task-43：左上角"幽灵区域"（舞台局部坐标 (0..150,0..150)）。
    #: 判据 a) 的扫描范围；也是 QA 复核时 mw.cursor_scan() 默认范围。
    GHOST_RECT = (0, 0, 150, 150)

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
        # 媒体路由：图片走 **Qt 原生 GPU 视口**（红线 1：不向 libmpv 发任何图片指令）
        from .image_view import ImageViewerWidget

        self.image_view = ImageViewerWidget(self)
        self.image_view.setVisible(False)
        grid.addWidget(self.image_view, 0, 0, 4, 1)
        # ★ task-43：幽灵守卫审计缓存（详见 apply_ghost_guard / ghost_state）。
        #   ⚠️ Lead 裁决（2026-10-18）：**不回退**图片视口自身的 OpenHand/ClosedHand
        #   拖拽手势光标 —— 那是真实交互隐喻，不是幽灵；幽灵只有 Minimap 等浮层。
        self._ghost_guard = {}
        self.video.hide()
        self._video_ready = False

        # ---- 第 0 行：顶部浮层 ----
        # ★★ task-64（用户 64a）：三层浮层**一律鼠标穿透** ——
        #   原来只有图片模式设 True ✗ ⇒ 视频模式三层(False)挡在 video 表面**吞掉点击**
        #   （用户"点画面没反应、无法暂停"的真因）。这三层里没有需要交互的控件
        #   （媒体信息浮层/空状态/字幕宿主/图片条都在各自宿主里，且 Qt 的该属性**只作用于
        #    该控件本身**，子控件照常可点 ⇒ 真实按钮不受影响 ✓）。
        self.top_layer = QWidget(self)
        self.top_layer.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        tl = QHBoxLayout(self.top_layer)
        tl.setContentsMargins(16, 14, 16, 0)
        tl.setSpacing(0)
        # #4 用户要求：图片模式**不要**进度条/音量条，图片控制条放**底部**。
        self.image_bar_host = QWidget(self)
        ibh = QVBoxLayout(self.image_bar_host)
        ibh.setContentsMargins(0, 0, 0, 14)
        ibh.setSpacing(0)
        self.image_bar_host.setVisible(False)
        self.image_bar = ImageInfoBar(self.image_bar_host)
        ibh.addWidget(self.image_bar, 0, Qt.AlignHCenter)
        self.image_bar.setVisible(False)
        # Tab 呼出的媒体信息浮层 —— **贴左上角**（PotPlayer 的位置），
        # 所以它排在拉伸项**之前**，右边的 image_bar 继续居中。
        self.media_info = MediaInfoOverlay(self.top_layer)
        tl.addWidget(self.media_info, 0, Qt.AlignLeft | Qt.AlignTop)
        tl.addStretch(1)
        tl.addStretch(1)
        grid.addWidget(self.top_layer, 0, 0)

        # 图片栏宿主：第 3 行（底部），与胶囊同层（互斥显示）
        grid.addWidget(self.image_bar_host, 3, 0)

        # ---- 第 1 行：中部浮层 ----
        self.middle_layer = QWidget(self)
        self.middle_layer.setAttribute(Qt.WA_TransparentForMouseEvents, True)   # ★ task-64
        ml = QVBoxLayout(self.middle_layer)
        ml.setContentsMargins(20, 10, 20, 10)
        ml.setSpacing(10)
        ml.addStretch(1)
        self.empty_state = EmptyState(self.middle_layer)
        ml.addWidget(self.empty_state, 0, Qt.AlignHCenter)
        # v1.0.26 音乐模式总成：模糊封面背景 + 双栏（封面 / 信息+歌词）+ 频谱。
        # ⚠️ 旧的白色 AudioCard 一律不再显示（用户要求撕掉那张实心白卡片）。
        from .audio_view import AudioView

        self.audio_view = AudioView(self)
        self.audio_view.setVisible(False)
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
        self.bottom_layer.setAttribute(Qt.WA_TransparentForMouseEvents, True)   # ★ task-64
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
        # ★★ task-64：**画面中央**的播放状态 OSD（单击反馈；鼠标穿透，绝不挡点击）
        self.center_host = QWidget(self)
        ch = QVBoxLayout(self.center_host)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(0)
        ch.addStretch(1)
        self.center_osd = OsdBadge(self.center_host)
        self.center_osd.hide()
        ch.addWidget(self.center_osd, 0, Qt.AlignHCenter)
        ch.addStretch(1)
        self.center_host.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.center_host.setVisible(False)
        grid.addWidget(self.center_host, 0, 0, 4, 1, Qt.AlignHCenter | Qt.AlignVCenter)
        self._click_n = 0
        self._dbl_n = 0
        self._last_click = None
        self._center_osd_gen = 0
        try:
            self.video.installEventFilter(self)      # ★ 视频表面的点击/双击归本类处理
        except Exception:
            pass

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
        self._drawer = None
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
        self.raise_overlays()

    # ---- ★★ task-64：视频表面点击 / 双击（用户 64a）--------------------------------
    def eventFilter(self, obj, ev):      # noqa: N802
        """只接管**视频表面**的鼠标：单击 ⇒ 播放/暂停 + 中央 OSD；双击 ⇒ 全屏切换。

        ⚠️ 应用级过滤器（task-41/47 的歌词热区）只在音乐模式生效且以歌词面板矩形为准，
        这里是**本类挂在 video 上**的局部过滤器 ⇒ 两条链互不干扰 ✓。
        """
        try:
            if obj is getattr(self, "video", None):
                from PySide6.QtCore import QEvent
                # ★ 只在**视频模式且表面可见**时接管（音乐/图片模式下 video 隐藏/不激活 ⇒ 不处理；
                #   这样"图片模式点击不触发暂停"与"音乐模式行为不变"都成立 ✓）
                if bool(getattr(self, "_image_mode", False)) or not self.video.isVisible():
                    return super().eventFilter(obj, ev)
                t = ev.type()
                if t == QEvent.MouseButtonDblClick:
                    self._dbl_n += 1
                    self._toggle_fullscreen()
                    return True
                if t == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton:
                    self._click_n += 1
                    try:
                        self._last_click = [int(ev.position().x()), int(ev.position().y())]
                    except Exception:
                        self._last_click = None
                    self._toggle_pause_with_osd()
                    return True
        except Exception:
            log.debug("视频点击处理失败", exc_info=True)
        return super().eventFilter(obj, ev)

    def click_state(self) -> dict:
        """★ task-64 锚点：点击/双击计数、中央 OSD 状态（QA 真机交互判据用）。"""
        try:
            # ★ 真源 = mpv 的 pause 属性（`w._paused` 之类字段并不存在 ⇒ 之前读到 null ✗）
            paused = bool(self.window().engine.get("pause", 0, False))
        except Exception:
            paused = None
        return {"clicks": int(getattr(self, "_click_n", 0)),
                "dblclicks": int(getattr(self, "_dbl_n", 0)),
                "last_click": getattr(self, "_last_click", None),
                "center_osd_visible": bool(getattr(self, "center_osd", None) is not None
                                           and self.center_osd.isVisible()),
                "center_osd_text": str(getattr(self, "_center_text", "")),
                "paused": paused,
                "image_mode": bool(getattr(self, "_image_mode", False)),
                "video_visible": bool(getattr(self, "video", None) is not None and self.video.isVisible()),
                "layers_transparent": bool(getattr(self, "middle_layer", None) is not None
                                           and self.middle_layer.testAttribute(
                                               Qt.WA_TransparentForMouseEvents))}

    def _toggle_pause_with_osd(self) -> None:
        """切播放/暂停 + 中央 OSD 提示（▶ / ⏸），800ms 后自动收起。"""
        win = self.window()
        playing_before = None
        try:
            playing_before = bool(win._playing)          # noqa: SLF001
        except Exception:
            pass
        try:
            win.toggle_pause()
        except Exception:
            log.debug("toggle_pause 失败", exc_info=True)
            return
        icon = "pause" if bool(playing_before) else "play"     # 点了"暂停"⇒ 显示暂停图标
        self._center_text = "已暂停" if bool(playing_before) else "播放"
        try:
            self.center_host.setVisible(True)
            self.center_osd.show_message(self._center_text, icon, None, 800)
            self._center_osd_gen += 1
            gen = self._center_osd_gen
            from PySide6.QtCore import QTimer
            QTimer.singleShot(900, lambda: self._hide_center_osd(gen))
        except Exception:
            log.debug("中央 OSD 显示失败", exc_info=True)

    def _hide_center_osd(self, gen: int) -> None:
        if int(getattr(self, "_center_osd_gen", 0)) != int(gen):
            return
        try:
            self.center_osd.hide_badge()
            self.center_host.setVisible(False)
        except Exception:
            pass

    def _toggle_fullscreen(self) -> None:
        try:
            self.window().toggle_fullscreen()
        except Exception:
            log.debug("双击全屏失败", exc_info=True)

    def set_image_mode(self, on: bool) -> None:
        """图片模式：底部显示图片控制条、隐藏播放胶囊，并**重排命中测试**。

        ⚠️⚠️ 结构性要点（命中测试实测）：`middle_layer` 原本**不是** mouse-transparent，
           它横跨整个中部 ⇒ 视口中央的**点击与滚轮全被它接走** ⇒
           "左键拖不动图片" + "滚轮冒泡到主窗口走那份反向实现" 两个症状同一根因。
           看图模式下这三层没有需要交互的内容（空状态/音频卡/字幕浮层都隐藏），
           所以整层设为透明，事件**直达视口**；退出看图模式时还原。
        """
        on = bool(on)
        self._image_mode = on          # ★ task-64：锚点/门控用
        # ★★ task-112①：图片模式**必须彻底隐藏 GL 视频控件**（QOpenGLWidget 留在窗内
        #    会继续占着一个 GL 上下文：玻璃模糊板每 33ms 采样时极易把图片视口的 FBO
        #    刷成空白 ⇒ 静置黑屏、缩放瞬间才闪出）。这里在**任何**布局/浮层操作之前
        #    就 hide + 关掉它的 update，退出图片模式时再恢复（视频模式自己会 show）。
        try:
            if on:
                self.video.hide()
                self.video.setUpdatesEnabled(False)
            else:
                self.video.setUpdatesEnabled(True)
        except Exception:
            pass
        try:
            self.image_bar_host.setVisible(on)
            self.image_bar.setVisible(on)
            # 图片模式：胶囊宿主整体收起（否则它会接走底部区域的点击）
            self.capsule_host.setVisible(not on)
        except Exception:
            pass
        for layer in (getattr(self, "top_layer", None), getattr(self, "middle_layer", None),
                      getattr(self, "bottom_layer", None)):
            if layer is None:
                continue
            try:
                # ★★ task-64：**始终穿透**（不再随模式还原成 False ✗）—— 视频/音乐模式同样
                #   需要空白处点击直达 video 表面；该属性只作用于本控件，子控件照常可点 ✓
                #   （图片控制条在 image_bar_host、媒体信息浮层本就是透明的）。
                layer.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            except Exception:
                pass
        # 模式切换后立刻把热区与手柄摆正（含 y=50%），不依赖后续事件
        try:
            self.sync_handle_geometry()
            self.raise_overlays()
        except Exception:
            pass
        if on:
            self.raise_overlays()
            # ★★ task-112①：进图片模式后补一次"真绘制"。若 GL 视口的 FBO 在切换
            #    过程中被污染（黑屏），仅 update() 会被合并丢弃；这里 update() + 下一
            #    个事件循环再 update() + repaint()，保证至少一帧真正落到上屏表面。
            try:
                iv = getattr(self, "image_view", None)
                if iv is not None:
                    iv.update()
                    from PySide6.QtCore import QTimer as _QT

                    _QT.singleShot(0, iv.update)
                    _QT.singleShot(0, iv.repaint)
            except Exception:
                pass

    def _handle_x_for(self, hw: int) -> int:
        """手柄 x 的**唯一**算式（展开/收起共用一条动态方程，v1.0.36）。

            handle_x = clamp(drawer_left − hw, 0, self.width() − hw)
            drawer_left = 抽屉**本体**在 Stage 坐标系里的左边缘

        ⚠️ 为什么不再用 is_open() 布尔分支（v1.0.34② 的写法）：
           收起动画期间 is_open 立即变 False ⇒ 手柄**第 1 帧就瞬移**回最右，
           而抽屉还在补间滑出（QA 实测：收起 14/14 帧违例、展开 0 违例）。
           现在全程只认"抽屉本体此刻在哪"这一个真实量：
             · 展开到底：本体左边缘 = 右对齐后的真实位置 ⇒ 手柄咬在接缝上；
             · 收起到底：slot 宽 0 ⇒ 本体左边缘 == 舞台右边缘 ⇒ 手柄正好 W−hw（终态不变）；
             · 动画中：本体逐帧移动 ⇒ 手柄逐帧咬合，无瞬移。
        ⚠️ 认的是**抽屉本体**（self._drawer = PlaylistDrawer），不是抽屉宿主
           drawer_host：动画期间 slot 被裁切，本体左边缘恰好等于可见区域左边界；
           用宿主原点算会让手柄落进抽屉内部被盖住（v1.0.32 的错注释已删）。
        """
        right = max(0, self.width() - hw)
        d = getattr(self, "_drawer", None)
        if d is None:
            return right                      # 未挂载：安全回落（卡片 D）
        try:
            if d.isVisible():
                left = int(d.mapTo(self, QPoint(0, 0)).x())
            else:
                # 抽屉宽度收到 ≤2px 时 set_drawer_width 会把它 setVisible(False)，
                # 这时改用**挂载点 slot** 的左边缘（可见期间两者恒等：抽屉左对齐在 slot 里）。
                # ⚠️ 不能在这里硬回落 W−hw：实测那样会在最后一帧多跳 2px
                #    （1256 → 1262，其中 4px 是抽屉自身位移、2px 是回落造成的）。
                slot = getattr(self, "drawer_slot", None)
                if slot is None or slot.width() <= 0:
                    return right              # 彻底收起：正好就是 W−hw（终态一致）
                left = int(slot.mapTo(self, QPoint(0, 0)).x())
        except Exception:
            return right
        return max(0, min(right, left - hw))

    def apply_handle_geometry(self) -> None:
        """★★ 抽屉手柄几何的**唯一**计算/应用入口（单一真源）。

        所有会改变手柄位置的路径都必须走这里，**禁止**在别处再写一遍坐标算式：
            resizeEvent / sync_handle_geometry / sync_handle_follow /
            set_drawer_visible（抽屉开合回调）/ set_hotspots_enabled / 模式切换。
        坐标方程（与验收标准逐条对应）：
            w, h = HANDLE_W, HANDLE_H                    # 16 × 56，唯一真源
            x    = _handle_x_for(HANDLE_W)               # clamp(drawer_left-16, 0, width()-16)
            y    = (self.height() - HANDLE_H) // 2       # 舞台高度 50%，任何模式/全屏都不变

        ⚠️ 这里**不无条件 show()**：全屏自动隐藏手柄（main_window.py:2193-2194 的
           `setVisible(False)`）必须不被后续事件树复活；需要强制可见的老路径
           （模式切换）由 sync_handle_geometry 走 ensure_visible=True。
        """
        hd = getattr(self, "drawer_handle", None)
        if hd is None:
            return
        try:
            if hd.parentWidget() is not self:
                hd.setParent(self)
            # ★★ v1.0.36：把两级布局**同步**算完再读抽屉几何。
            #   抽屉动画只改 drawer_slot 的固定宽度，布局要到下一轮事件循环才生效 ——
            #   直接读几何会拿到"上一帧"的位置：实测手柄整整慢一帧
            #   （每帧 diff == 抽屉单帧位移：114 → 63 → 27 …，正好是上一帧的落差）。
            #   activate() 是同步、幂等、很便宜的（只重排这两个容器的子控件）。
            for _holder in (getattr(self, "drawer_host", None),
                            getattr(self, "drawer_slot", None)):
                _lay = _holder.layout() if _holder is not None else None
                if _lay is not None:
                    _lay.activate()
            hd.setFixedSize(HANDLE_W, HANDLE_H)          # 与 paintEvent 同一真源
            hd.setGeometry(self._handle_x_for(HANDLE_W),
                           max(0, (self.height() - HANDLE_H) // 2),
                           HANDLE_W, HANDLE_H)
            hd.raise_()          # 手柄必须在抽屉之上，绝不允许被盖住 1 像素
        except Exception:
            log.debug("手柄几何应用失败", exc_info=True)

    def _sync_audio_view_geometry(self) -> None:
        """音乐视图铺满内容区（覆盖视频层），底部给控制栏让位。"""
        av = getattr(self, "audio_view", None)
        if av is None:
            return
        try:
            # v1.0.27：**铺满整个舞台**（从顶到底 100%），控制栏作为透明浮层悬在其上；
            # 之前按控制栏高度留白 ⇒ 沉浸模式控制栏淡出后底部露出死黑断层（用户截图）。
            av.setGeometry(0, 0, self.width(), self.height())
        except Exception:
            pass

    def set_audio_view_visible(self, on: bool) -> None:
        """音频模式：显示音乐视图（并抬到视频层之上、控制栏之下）。"""
        av = getattr(self, "audio_view", None)
        if av is None:
            return
        av.setVisible(bool(on))
        if on:
            # ★ task-37①：切入音频模式的**必经之路** ⇒ 顺手把图片翻页热区彻底关掉
            self.set_zone_kind("audio")
            av.setGeometry(0, 0, self.width(), self.height())
            self._sync_audio_view_geometry()
            try:
                av.raise_()
            except Exception:
                pass
            # 控制栏与抽屉必须仍在它之上
            try:
                self.capsule_host.raise_()
            except Exception:
                pass
            self.raise_overlays()

    def sync_handle_geometry(self, ensure_visible: bool = True) -> None:
        """把抽屉手柄钉在「舞台高度的 50%」+「抽屉左边缘」，**任何模式**都成立。

        ⚠️ v1.0.20 我把手柄 reparent 到舞台后，只在 sync_hotspots() 里给它定位，
           而 sync_hotspots() 只在图片相关路径被调用 ⇒ 切到视频/音乐模式、或舞台
           resize 时它停在旧坐标（用户截图：手柄飞到窗口最顶部）。
           现在 y 一律按**舞台自身高度**算，并被 resize / 模式切换 / 抽屉开合
           四条路径覆盖 —— 四条路径共用 apply_handle_geometry 这一个入口，
           本函数只剩"顺带强制可见"这一条老语义（全屏自动隐藏不能被复活）。
        """
        self.apply_handle_geometry()
        if not ensure_visible:
            return
        hd = getattr(self, "drawer_handle", None)
        if hd is not None:
            try:
                hd.show()
            except Exception:
                pass

    def sync_handle_follow(self) -> None:
        # ⚠️（v1.0.36）本函数只做"转发"，**不许**再写第二份坐标算式 ——
        #    算式只有 _handle_x_for() 一份，应用只有 apply_handle_geometry() 一个入口。
        """抽屉动画每帧回调：只移动手柄的 x（y 恒定 50% 居中，绝不改 y）。"""
        hd = getattr(self, "drawer_handle", None)
        if hd is None or not hd.isVisible():
            return
        self.apply_handle_geometry()

    # ⚠️ 这里曾有**第二份** Stage.resizeEvent（v1.0.30 的"几何不变量"）。Python 里
    #    后定义的会覆盖先定义的 ⇒ 那一份**从未执行**，这正是"首页手柄飞到左上角 /
    #    全屏 AudioView 不跟随"（用户截图）的唯一真因。
    #    v1.0.34-1：死代码已删除 —— Stage 只保留文件末尾**唯一**一份 resizeEvent，
    #    坐标算式只存在于 apply_handle_geometry 与 _handle_x_for 两处。

    # ★★ task-37（用户 ⑦）：**模式泄漏**根治 —— 非图片模式下两个翻页热区必须
    #   隐藏 + 禁用（真机：音频模式左上角仍有幽灵箭头浮层，且仍接收悬停）。
    #   热区是 `HotspotZone(QWidget)`（image_view.py:255），实例挂在视口上，
    #   但 `sync_hotspots()` 可能把它们 reparent 到 Stage ⇒ 两处都要找。
    def _zone_widgets(self) -> list:
        out = []
        for holder in (getattr(self, "image_view", None), self):
            for name in ("zone_left", "zone_right"):
                z = getattr(holder, name, None)
                if z is not None and z not in out:
                    out.append(z)
        return out

    def _apply_zone_visibility(self) -> None:
        """★ task-37：热区可见性的**唯一决策点**（可见 = 图片模式 且 抽屉未展开）。

        任何一条不满足 ⇒ `hide()` + `setEnabled(False)`（既不画箭头，也不吃悬停/点击）。
        """
        allow = (str(getattr(self, "_zone_kind", "image")) == "image") \
            and not bool(getattr(self, "_zones_blocked", False))
        for z in self._zone_widgets():
            try:
                z.setVisible(bool(allow))
                z.setEnabled(bool(allow))
                # ★ task-43：判据 a) 要求左上角网格 100% ArrowCursor。图片模式下
                #   左侧热区正好落在 (0..72, 0..150) 扫描范围内，所以这里统一把
                #   热区光标钉成 Arrow（**点击翻页不受影响**，只有手型图标不再出现）。
                #   非图片模式下热区本就 hide+disable，清光标是双保险。
                z.setCursor(Qt.ArrowCursor)
            except Exception:
                log.debug("热区可见性切换失败", exc_info=True)

    def set_zone_kind(self, kind: str) -> None:
        """★ task-37/43：**模式入口** —— 热区存在性 + 幽灵浮层清理都收口在这里。

        · task-37：只有 `"image"` 允许热区存在，audio/video 一律 hide+disable；
        · task-43：非图片模式把图片专属浮层（MinimapNavigator 等）彻底 hide 并清
          PointingHand 光标（详见 apply_ghost_guard）。

        调用点：`MainWindow._apply_kind_ui(kind)`（唯一模式 UI 收口）与
        `Stage.set_audio_view_visible(True)`（卡片①要求的"所有切入音频视频的路径"兜底）。
        """
        self._zone_kind = "image" if str(kind) == "image" else str(kind or "other")
        self._apply_zone_visibility()
        self.apply_ghost_guard(self._zone_kind)

    # ==================================================================================
    # ★★ task-43：左上角「幽灵小手光标」根治
    # ==================================================================================
    # 用户现象（③）：音频模式下鼠标移到窗口左上角（标题栏正下方 (0,0,120,60)）指针
    #   变成小手，点击无任何反应。
    # 真因（本机逐点 widgetAt 普查定位）：
    #   Stage.sync_hotspots() 原来**无条件** `mm.show()` —— 图片模式的鸟瞰导航
    #   `MinimapNavigator`（PointingHandCursor）会被抬到 Stage 上并 show()。音频/视频
    #   模式下 image_view 是隐藏的、几何还是默认 (0,0,100,30)，于是鸟瞰图被摆到
    #   **舞台左上角 (8,12,160,100)** 且可见 ⇒ 正好盖住用户说的 (0,0,120,60) 区域。
    #   点击无反应是因为 Minimap 在无图/未缩放时没有可拖拽内容（或根本不归它管）。
    # 本卡做法：
    #   ① 模式入口 `set_zone_kind()` 调 `apply_ghost_guard()`：非图片模式
    #      Minimap hide + disable + ArrowCursor；图片模式可见性仍由
    #      image_view._sync_minimap()（缩放>1 才显示）单独管；
    #   ② `sync_hotspots()` 只在图片模式摆/抬鸟瞰图，非图片模式一律 hide；
    #   ③ 热区光标统一 Arrow（判据 a 要求左上角网格 100% ArrowCursor）；
    #   ④ **图片视口保留自身 OpenHand/ClosedHand 拖拽手势**（Lead 裁决：
    #      那是真实交互隐喻，不是幽灵；幽灵只有 Minimap 等浮层）；
    #   ⑤ 兜底：直接挂在 Stage 上、默认停在 (0,0)、没有任何布局管理的小控件
    #      （历史残留 IconButton）一律 hide + ArrowCursor。
    def apply_ghost_guard(self, kind: str | None = None) -> dict:
        """★ task-43：非本模式浮层的 hide + 光标清理（幂等；返回可给 QA 的审计报告）。"""
        k = str(kind if kind is not None else getattr(self, "_zone_kind", "other"))
        is_image = (k == "image")
        rep = {"kind": k, "hidden": [], "cursor_cleared": [], "leftovers": []}
        # ---- ① 鸟瞰导航（MinimapNavigator）：只属于图片模式 ----
        mm = getattr(getattr(self, "image_view", None), "minimap", None)
        if mm is not None:
            try:
                if is_image:
                    # 图片模式：Lead 裁决「Minimap 等幽灵控件必须 Arrow」；可见性仍由
                    # _sync_minimap（缩放>1）管，拖拽功能不受光标影响。
                    mm.setEnabled(True)
                    if mm.cursor().shape() != Qt.ArrowCursor:
                        mm.setCursor(Qt.ArrowCursor)
                        rep["cursor_cleared"].append("MinimapNavigator(image)")
                else:
                    if mm.isVisible():
                        rep["hidden"].append({
                            "class": type(mm).__name__, "obj": mm.objectName(),
                            "parent": type(mm.parentWidget()).__name__ if mm.parentWidget() else None,
                            "geom": list(mm.geometry().getRect()),
                        })
                    mm.hide()
                    mm.setEnabled(False)
                    if mm.cursor().shape() != Qt.ArrowCursor:
                        mm.setCursor(Qt.ArrowCursor)
                        rep["cursor_cleared"].append("MinimapNavigator")
            except Exception:
                log.debug("task-43 鸟瞰图清理失败", exc_info=True)
        # ---- ② 图片视口光标：**保留**它自己的 OpenHand/ClosedHand 拖拽手势（Lead 裁决）----
        iv = getattr(self, "image_view", None)
        if iv is not None:
            try:
                if is_image:
                    # 基线行为：查看区 = OpenHand（按下 ClosedHand、松开 OpenHand）
                    if iv.cursor().shape() != Qt.OpenHandCursor:
                        iv.setCursor(Qt.OpenHandCursor)
                else:
                    # 非图片模式下视口是隐藏的：清成 Arrow，防止任何残留手型泄漏
                    if iv.cursor().shape() != Qt.ArrowCursor:
                        iv.setCursor(Qt.ArrowCursor)
                        rep["cursor_cleared"].append("ImageViewerWidget(hidden)")
            except Exception:
                log.debug("task-43 图片视口光标恢复失败", exc_info=True)
        # ---- ③ 历史残留 AudioCard（v1.0.27 已物理销毁；若有复活也不许在
        #        非音频模式可见 —— 这里对"非图片模式"一并 hide，避免幽灵按钮）----
        ac = getattr(self, "audio_card", None)
        if ac is not None and not is_image:
            try:
                if ac.isVisible():
                    rep["hidden"].append({"class": type(ac).__name__, "obj": ac.objectName()})
                ac.hide()
                ac.setCursor(Qt.ArrowCursor)
                rep["cursor_cleared"].append("AudioCard")
            except Exception:
                log.debug("task-43 AudioCard 清理失败", exc_info=True)
        # ---- ④ 兜底：默认停在 (0,0)、未进布局、带手型光标的孤立小控件 ----
        try:
            for w in self.findChildren(QWidget):
                if w is self or not w.isVisible():
                    continue
                if w.cursor().shape() == Qt.ArrowCursor:
                    continue
                g = w.geometry()
                if (g.x() == 0 and g.y() == 0 and 0 < g.width() <= 200
                        and 0 < g.height() <= 200 and w.parentWidget() is self):
                    lay = self.layout()
                    if lay is not None and lay.indexOf(w) >= 0:
                        continue
                    w.hide()
                    w.setCursor(Qt.ArrowCursor)
                    rep["leftovers"].append("%s#%s" % (type(w).__name__, w.objectName()))
        except Exception:
            log.debug("task-43 幽灵控件兜底清扫失败", exc_info=True)
        self._ghost_guard = rep
        if rep["hidden"] or rep["cursor_cleared"] or rep["leftovers"]:
            log.info("task-43 幽灵守卫[%s]: hidden=%s cursor=%s leftovers=%s",
                     k, rep["hidden"], rep["cursor_cleared"], rep["leftovers"])
        return rep

    # ---- ★ task-43：不依赖前台的命中测试 / 生效 cursor ----------------------------
    # ⚠️ 为什么不用 QApplication.widgetAt：它走**原生 Windows 命中测试**，当本应用
    #    窗口不在前台/被别的窗口盖住时对 (0..150,0..150) 网格会 **256/256 全返回
    #    None**（QA 环境实测），导致"生效 cursor"读不到。这里改为**控件树递归命中**：
    #    child 反向 Z 序遍历 → 跳过 hidden / WA_TransparentForMouseEvents →
    #    坐标映射进 child → 命中则继续下钻，否则返回该 child。纯 Qt 内部几何计算，
    #    与应用是否在前台完全无关。
    def hit_test(self, pt) -> QWidget:
        def _rec(parent, pos):
            try:
                children = parent.children()
            except Exception:
                children = []
            for child in reversed(children):
                if not isinstance(child, QWidget):
                    continue
                try:
                    if not child.isVisible():
                        continue
                    if child.testAttribute(Qt.WA_TransparentForMouseEvents):
                        continue
                    cp = child.mapFrom(parent, pos)
                    if child.rect().contains(cp):
                        deeper = _rec(child, cp)
                        return deeper if deeper is not None else child
                except Exception:
                    continue
            return None

        try:
            w = _rec(self, pt)
        except Exception:
            w = None
        return w if w is not None else self

    def effective_cursor_at(self, pt) -> dict:
        """★ task-43：单点生效 cursor（背景安全；QA 可直接 eval）。

        返回 {"xy": [x,y], "widget": 类名, "obj": objectName, "cursor": 形状名,
              "chain": [类名/父链...]}。cursor 取命中控件自身的 cursor()
        （Qt 会沿父链继承未显式设置的 cursor）。
        """
        w = self.hit_test(pt)
        shape = w.cursor().shape().name if w is not None else "ArrowCursor"
        chain, p = [], w
        while p is not None and len(chain) < 6:
            chain.append(type(p).__name__
                         + (("." + p.objectName()) if p.objectName() else ""))
            p = p.parentWidget()
        iv = getattr(self, "image_view", None)
        under_iv = bool(w is not None and iv is not None and (w is iv or iv.isAncestorOf(w)))
        return {"xy": [int(pt.x()), int(pt.y())],
                "widget": (type(w).__name__ if w is not None else None),
                "obj": (w.objectName() if w is not None else None),
                "cursor": shape, "chain": chain, "under_image_view": under_iv}

    def ghost_state(self) -> dict:
        """★ task-43 可读锚点：幽灵守卫审计 + 当前关键控件 + 最近一次 cursor_scan 矩阵摘要。"""
        mm = getattr(getattr(self, "image_view", None), "minimap", None)
        iv = getattr(self, "image_view", None)
        return {
            "kind": str(getattr(self, "_zone_kind", "other")),
            "guard": dict(getattr(self, "_ghost_guard", {}) or {}),
            "minimap": ({"visible": bool(mm.isVisible()), "enabled": bool(mm.isEnabled()),
                         "cursor": mm.cursor().shape().name,
                         "geom": list(mm.geometry().getRect())} if mm is not None else None),
            "image_view": ({"visible": bool(iv.isVisible()),
                            "cursor": iv.cursor().shape().name} if iv is not None else None),
            "ghost_rect": list(self.GHOST_RECT),
            # 最近一次 mw.cursor_scan() 的逐点矩阵摘要（QA 不依赖前台也能读）
            "last_cursor_matrix": dict(getattr(self, "_last_cursor_matrix", {}) or {}),
            "zones": self.zone_state(),
        }

    def zone_state(self) -> dict:
        """★ task-37 可读锚点：两个热区的真实可见性/可用性/类型/父级（QA 逐条对账）。"""
        out = {}
        for i, z in enumerate(self._zone_widgets()):
            key = getattr(z, "_dir", None)
            name = "zone_left" if (key == -1 or (key is None and i == 0)) else "zone_right"
            try:
                out[name] = {"exists": True, "visible": bool(z.isVisible()),
                             "enabled": bool(z.isEnabled()), "type": type(z).__name__,
                             "parent": type(z.parentWidget()).__name__ if z.parentWidget() else None,
                             "geometry": list(z.geometry().getRect())}
            except Exception as e:
                out[name] = {"exists": True, "error": str(e)}
        out["kind"] = str(getattr(self, "_zone_kind", "image"))
        out["blocked"] = bool(getattr(self, "_zones_blocked", False))
        return out

    def set_hotspots_enabled(self, on: bool) -> None:
        """抽屉展开时**立即失效热区**，关闭后恢复（v1.0.21）。

        ⚠️ 透明热区覆盖整个右侧，抽屉展开后它依然浮在抽屉之上 ⇒ 点抽屉里的
           排序按钮/条目会被误判成"切下一张"（用户实测）。这里做两件事：
             1. 抽屉展开 → 两个热区 setVisible(False)（同时禁用命中测试）；
             2. 抽屉面板抬到热区之上（Z 序），任何抽屉内点击 100% 优先。
        """
        # ⚠️ 热区挂在**视口**上（ImageviewerWidget.zone_left/right），不是舞台的属性。
        #    写成 self.zone_left 会 AttributeError ⇒ 被 except 吞掉 ⇒ 整个"抽屉展开时
        #    失效热区"静默不生效（探针实测抓到的）。
        # ★ task-37：不再直接写可见性（否则抽屉一关就会把**音频模式**下的热区拉回来 ⇒ 幽灵箭头复活）；
        #   统一走 `_apply_zone_visibility()`：抽屉状态与**模式状态**同时参与判断。
        self._zones_blocked = not bool(on)
        self._apply_zone_visibility()
        if not on:
            try:
                self.drawer_host.raise_()          # 抽屉/手柄压在热区之上
            except Exception:
                pass
            try:
                d = getattr(self, "_drawer", None)
                if d is not None and d.is_open():
                    d.raise_()
            except Exception:
                pass
        else:
            self.sync_hotspots()
        # ★ v1.0.34-1：热区开关同样是几何触发路径，统一走单一真源入口
        #   （收起抽屉恢复热区时手柄必须同步归位；且必须排在抽屉之上）。
        self.apply_handle_geometry()

    def sync_hotspots(self) -> None:
        """把左右热区摆到视口左右边缘（避开抽屉手柄，不与它重叠）。

        ⚠️ 热区是**舞台**的子控件（不是视口的子控件）—— 只有这样才能排到浮层之上
           （否则被浮层盖住收不到悬停，用户实测「热区不见了」），
           同时又必须排在**取景框与抽屉手柄之下**（用户明确要求）。
        """
        v = getattr(self, "image_view", None)
        if v is None:
            return
        g = v.geometry()
        for z, left in ((getattr(v, "zone_left", None), True),
                        (getattr(v, "zone_right", None), False)):
            if z is None:
                continue
            if z.parentWidget() is not self:
                z.setParent(self)
            zw = z.width()
            # #1 热区**贴边**：上一版把右热区左移了一个手柄宽，结果渐变被手柄「挤掉」
            #    一截（用户截图红框标出）。现在恢复贴边 —— 渐变完整；
            #    与手柄的重合改由「箭头位置」和「层级」解决：手柄画在热区之上，
            #    箭头上移到 40% 高度，避开竖直居中的手柄。
            # v1.0.20 用户要求：右热区**严格 100% 贴靠右边缘**（删除坐标避让），
            # 渐变自然贴边展开；与手柄的重叠由 **Z 序** 解决（手柄在热区之上）。
            x = g.left() if left else max(g.left(), g.right() - zw)
            z.setGeometry(x, g.top(), zw, max(1, g.height()))
            # ⚠️⚠️ task-37（幽灵箭头的**第二现场**）：这里原来无条件 `z.show()`，
            #   于是任何一次 sync_hotspots()（resize / 模式切换 / 布局刷新）都会把
            #   音频/视频模式下已经隐藏的热区**重新拉回可见** —— 我第一版只在
            #   模式切换时 hide，探针立刻抓到 audio/video 下 visible 又变回 True。
            #   现在只调唯一决策点（模式 + 抽屉两个条件一起判），随后抬 Z 序。
            try:
                d = getattr(self, "_drawer", None)
                if d is not None and d.is_open():
                    z.hide()
                    continue
            except Exception:
                pass
            self._apply_zone_visibility()
            if z.isVisible():
                z.raise_()
        # ---- 抽屉手柄：改挂舞台，垂直**严格 50% 居中**（与热区箭头同一几何中心）----
        try:
            # 手柄定位统一走 apply_handle_geometry（y 按舞台高度算，不依赖视口几何）；
            # 这里保留 sync_handle_geometry 是为了那一条"强制可见"的老语义。
            self.sync_handle_geometry()
        except Exception:
            pass
        # ---- 鸟瞰图：改挂舞台并排在**热区之后**（Z 序高于热区，拖动不再被拦截）----
        # ★ task-43：**只在图片模式**摆放/抬升鸟瞰图，且**不再无条件 show()**。
        #   旧代码在这里无条件 mm.show() ⇒ 音频/视频模式下 image_view 是隐藏的、
        #   几何还是默认 (0,0,100,30)，鸟瞰图被摆到舞台左上角 (8,12,160,100) 并显示，
        #   带着 PointingHandCursor —— 这就是用户 ③「左上角小手、点击无反应」的幽灵。
        #   现在可见性只由 MinimapNavigator.set_t（image_view._sync_minimap：缩放>1）决定。
        try:
            v2 = getattr(self, "image_view", None)
            mm = getattr(v2, "minimap", None) if v2 is not None else None
            if mm is not None:
                if str(getattr(self, "_zone_kind", "other")) == "image":
                    if mm.parentWidget() is not self:
                        mm.setParent(self)
                    mx = g.left() + max(8, (v2.width() if v2 is not None else g.width())
                                        - mm.width() - 16)
                    my = (g.top() + 12 if getattr(v2, "_minimap_pos", "bottom_right") == "top_right"
                          else g.top() + max(12, (v2.height() if v2 is not None else g.height())
                                             - mm.height() - 76))
                    mm.setGeometry(mx, my, mm.width(), mm.height())
                    mm.raise_()                                # 只抬 Z 序，可见性交给 set_t
                else:
                    if mm.isVisible():
                        mm.hide()
                    if mm.cursor().shape() != Qt.ArrowCursor:
                        mm.setCursor(Qt.ArrowCursor)
        except Exception:
            pass

    def raise_hotspots_above_view(self) -> None:
        """热区浮在画面之上，但仍在取景框/手柄之下。"""
        self.sync_hotspots()
        try:
            self.drawer_handle.raise_()
        except Exception:
            pass
        # 鸟瞰图与手柄已在 sync_hotspots() 里按「热区 → 手柄 → 鸟瞰图」顺序抬升

    def set_capsule_visible(self, on: bool) -> None:
        """★ task-117：无条件显示/隐藏控制条宿主，并**显示之后再抬一次**。

        真因（真机取证）：视频画布是 QOpenGLWidget，video.show() 之后它会重新合成到
        最上层 —— 浮层即使在逻辑上 raise 过，也会被 GL 子表面盖住 ⇒ 窗口模式直接开视频时
        「底栏完全不显示，只有最底下露出几个像素」。所以必须**在显示之后**再 raise。
        """
        try:
            self.capsule_host.setVisible(bool(on))
            if on:
                self.capsule_host.raise_()
                self.raise_overlays()
        except Exception:
            pass

    def set_video_row_span(self, rows: int) -> None:
        """★★ task-117：视频画布占几行 grid —— **4 = 与控制栏同区重叠，3 = 让出底栏一行**。

        根因（真机实验确认）：mpv 画布是 QOpenGLWidget，Windows 上是**原生子窗口**，
        原生子窗口永远盖在非原生兄弟之上 ⇒ 窗口模式下底栏无论怎么 raise_() 都被盖住
        （实测底部"灰比" 0.013 = 全是彩条视频；把画布改成 3 行让出底栏后 1.0 = 底栏正常）。
        raise_()/WA_AlwaysStackOnTop/WA_NativeWindow 三种做法都试过：前者无效，后者变黑块。
        """
        rows = 4 if int(rows) >= 4 else 3
        try:
            if getattr(self, "_video_rows", 4) == rows:
                return
            lay = self.layout()
            if lay is None:
                return
            lay.addWidget(self.video, 0, 0, rows, 1)
            lay.activate()
            self._video_rows = rows
            self.raise_overlays()
        except Exception:
            pass

    def raise_overlays(self):
        """把所有浮层抬到画面控件之上（**不要**反过来抬画面）。

        ⚠️ 媒体路由后，图片视口与 mpv 画布共用跨四行的大格子。谁要是直接
           `image_view.raise_()`，就会把**底部控制栏、顶部信息栏、抽屉手柄**
           一起压住 —— 用户实测现象就是「控制栏整个不见了」。
           正确顺序：画面留在底层，浮层统一抬升。
        """
        for w in (getattr(self, "top_layer", None), getattr(self, "middle_layer", None),
                  getattr(self, "bottom_layer", None), getattr(self, "osd_host", None),
                  getattr(self, "drawer_host", None), getattr(self, "capsule_host", None),
                  getattr(self, "settings_host", None), getattr(self, "drop_overlay", None),
                  # #3 ⚠️⚠️ image_bar_host 必须排在 **capsule_host 之后**抬升：
                  #    两者同处第 3 行、capsule_host 后加入 ⇒ 默认压在图片控制条上面，
                  #    把整条控制栏的点击全部吃掉（用户实测「控制栏无法点击」）。
                  getattr(self, "image_bar_host", None)):
            try:
                if w is not None:
                    w.raise_()
            except Exception:
                pass
        # #2/#3 热区：必须浮在**画面与浮层**之上（否则收不到悬停 ⇒ 热区不见了），
        #         但必须排在**取景框与抽屉手柄之下**（用户要求，且不得与手柄重叠）。
        try:
            self.raise_hotspots_above_view()
        except Exception:
            pass

    def set_capsule_fullscreen_gap(self, on: bool) -> None:
        """★ task-103：全屏时控制条**脱离 grid、绝对定位**到底部并留 16px 视频。

        诊断依据（真机）：`childAt` 显示胶囊确实在 video **之上**（不是 z 序遮挡 ✗），
        但 grid 的底部边距怎么设都被覆盖（实测 gap 恒 0）⇒ 按 Lead 口径改为 **setGeometry**：
        `Y = stage.height() - cap_h - 16`、`raise_()`；退出全屏再放回第 3 行。
        全屏期间跟随 `resizeEvent` 重新落位（见 _relayout_capsule_fs）。"""
        try:
            self._cap_fs = bool(on)
            if on:
                h = max(76, int(getattr(self, "_cap_h", 76)))
                self._cap_fs_h = h
                # ⚠️ 必须用 `self.layout()`（真正的 QGridLayout）—— 本类**没有** `self.grid`
                #   属性，写 `self.grid.xxx` 会静默失败 ⇒ 胶囊仍留在布局里 ⇒ 立刻被重排回贴底位置 ✗
                #   （实测：手动调本方法 y=958 ✓，但 toggle_fullscreen 之后又被重排回 974 ✗）。
                try:
                    lay = self.layout()
                    if lay is not None:
                        lay.removeWidget(self.capsule_host)
                except Exception:
                    pass
                self._relayout_capsule_fs()
                # 再补两帧（进全屏后 Qt 还会跑一次布局，会把绝对几何覆盖掉）
                try:
                    from PySide6.QtCore import QTimer as _QT
                    _QT.singleShot(0, self._relayout_capsule_fs)
                    _QT.singleShot(80, self._relayout_capsule_fs)
                except Exception:
                    pass
            else:
                try:
                    lay = self.layout()
                    if lay is not None:
                        lay.addWidget(self.capsule_host, 3, 0)   # 放回原位
                    self.capsule_host.setFixedHeight(max(76, int(getattr(self, "_cap_h", 76))))
                except Exception:
                    pass
            self.capsule_host.raise_()
        except Exception:
            pass

    def _relayout_capsule_fs(self) -> None:
        """全屏布局：胶囊贴底但**留 16px**（下方直接透视频，不黑垫底）。"""
        try:
            if not getattr(self, "_cap_fs", False):
                return
            h = int(getattr(self, "_cap_fs_h", 76))
            y = max(0, int(self.height()) - h - 16)
            self.capsule_host.setGeometry(0, y, int(self.width()), h)
            self.capsule_host.raise_()
        except Exception:
            pass


    def attach_capsule(self, capsule, margin_lr: int, margin_b: int, shadow: int) -> None:
        """把控制条胶囊挂到舞台第 3 行（视频仍然在它后面铺满整个舞台）。

        胶囊控件本身含 `shadow` 厚度的阴影外框，所以外边距要扣掉它，
        用户看到的胶囊边缘才会正好是 `margin_lr` / `margin_b`。
        """
        self.capsule = capsule
        lay = self.capsule_host.layout()
        lay.setContentsMargins(max(0, margin_lr - shadow), 0, max(0, margin_lr - shadow), 0)
        lay.addWidget(capsule)
        # ★★ task-103（P0 全屏控制栏腰斩）：**高度必须完整容纳整条控制栏** ——
        #   旧实现用 `sizeHint()`（含阴影 16px ⇒ 实测 94px）本身没问题，但全屏时
        #   grid 底边距为 0 ⇒ 胶囊**贴死在屏幕最下沿**，形状被任务栏/屏幕边界裁掉下半截 ✗。
        #   这里保证 ≥76px（卡片口径）并在全屏时给底部留 16px 视频。
        cap_h = max(76, int(capsule.sizeHint().height()))
        self._cap_h = cap_h
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
        self._drawer = drawer          # sync_hotspots 判断抽屉是否展开（决定手柄 x）
        # v1.0.22：绑定抽屉动画的逐帧变化 ⇒ 手柄平滑随动（不再瞬移）
        try:
            drawer.width_changed.connect(lambda _w: self.sync_handle_follow())
        except Exception:
            pass
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
        # ★ v1.0.34-1：抽屉开合回调也必须把几何重新钉一遍（走单一真源入口）。
        #   抽屉**无动画**开合（ui drawer on 不带 --animate、进/出全屏复位）时
        #   width_changed 一帧都不发 ⇒ 手柄会停在旧 x 上被抽屉盖住；这里补上，
        #   「收起/展开 × 窗口/全屏」四种组合才全部自洽。
        self.apply_handle_geometry()

    def resizeEvent(self, ev):
        # ★★ v1.0.34-1 几何不变量（本类是**唯一**的 Stage.resizeEvent：旧的第二份
        #    定义已删除，见本类中段那段 ⚠️ 死代码说明）。
        #    ⚠️ 这里不再写任何坐标算式 —— 手柄的 (x, y, 16, 56) 只在
        #    apply_handle_geometry 一处算出来，本路径只是触发器之一。
        #    （历史教训：算式散落 ⇒ "谁想起来谁调一次" ⇒ 首页/全屏漏摆 ⇒ 手柄半截在左上角。）
        self.apply_handle_geometry()
        av = getattr(self, "audio_view", None)
        if av is not None and av.isVisible():
            av.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(ev)
        self.drop_overlay.setGeometry(self.rect())
        # ★ task-103：全屏时胶囊是**绝对定位**的（脱离 grid 才能拿到 16px 底部间隙）⇒
        #   尺寸一变必须重算，否则切分辨率/进出全屏后会错位。
        try:
            self._relayout_capsule_fs()
        except Exception:
            pass

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
