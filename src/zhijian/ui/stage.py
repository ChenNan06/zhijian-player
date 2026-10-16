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
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..media.scanner import human_size
from ..mpv import MpvRenderWidget
from . import icons
from .theme import M, P, css, qt_color
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
        # 媒体路由：图片走 **Qt 原生 GPU 视口**（红线 1：不向 libmpv 发任何图片指令）
        from .image_view import ImageViewerWidget

        self.image_view = ImageViewerWidget(self)
        self.image_view.setVisible(False)
        grid.addWidget(self.image_view, 0, 0, 4, 1)
        self.video.hide()
        self._video_ready = False

        # ---- 第 0 行：顶部浮层 ----
        self.top_layer = QWidget(self)
        self.top_layer.setAttribute(Qt.WA_TransparentForMouseEvents, False)
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

    def set_image_mode(self, on: bool) -> None:
        """图片模式：底部显示图片控制条、隐藏播放胶囊，并**重排命中测试**。

        ⚠️⚠️ 结构性要点（命中测试实测）：`middle_layer` 原本**不是** mouse-transparent，
           它横跨整个中部 ⇒ 视口中央的**点击与滚轮全被它接走** ⇒
           "左键拖不动图片" + "滚轮冒泡到主窗口走那份反向实现" 两个症状同一根因。
           看图模式下这三层没有需要交互的内容（空状态/音频卡/字幕浮层都隐藏），
           所以整层设为透明，事件**直达视口**；退出看图模式时还原。
        """
        on = bool(on)
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
                # ⚠️ 该属性会**传递到子控件**；这三层在本模式下没有可交互子控件
                #    （图片控制条在 image_bar_host、媒体信息浮层本就是透明的）。
                layer.setAttribute(Qt.WA_TransparentForMouseEvents, on)
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

    def _handle_x_for(self, hw: int) -> int:
        """手柄 x：抽屉左边缘 − 手柄宽；抽屉收起时贴舞台右边缘。

        v1.0.32：抽屉左边缘必须用「宿主右边缘 − 抽屉宽度」算 ——
        `drawer.mapTo()` 给的是宿主原点，会让手柄跑进抽屉内部被盖住（截图 1）。
        """
        right = max(0, self.width() - hw)
        d = getattr(self, "_drawer", None)
        if d is None:
            return right
        # v1.0.34 ②：**只认开合状态**，收起时无条件贴最右（不再减 width）
        try:
            if not (d.isVisible() and getattr(d, "is_open", lambda: False)()):
                return right
            return max(0, int(d.mapTo(self, QPoint(0, 0)).x()) - hw)
        except Exception:
            return right

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

    def sync_handle_geometry(self) -> None:
        """把抽屉手柄钉在「舞台高度的 50%」+「抽屉左边缘」，**任何模式**都成立。

        ⚠️ v1.0.20 我把手柄 reparent 到舞台后，只在 sync_hotspots() 里给它定位，
           而 sync_hotspots() 只在图片相关路径被调用 ⇒ 切到视频/音乐模式、或舞台
           resize 时它停在旧坐标（用户截图：手柄飞到窗口最顶部）。
           现在 y 一律按**舞台自身高度**算，并被 resize / 模式切换 / 抽屉开合
           四条路径覆盖。
        """
        try:
            hd = self.drawer_handle
            if hd.parentWidget() is not self:
                hd.setParent(self)
            # v1.0.30 ①：固定物理规格 16x56（旧的 sizeHint 推导写出 12x48 ⇒ 绘制被裁成半截）
            hd.setFixedSize(16, 56)
            hw, hh = 16, 56
            hy = max(0, (self.height() - hh) // 2)      # ← 舞台高度 50% 绝对居中
            # 手柄很窄时宽度可能还没被样式算出来，兜一个最小值
            if hw <= 0:
                hw = max(1, hd.sizeHint().width())
                hd.resize(hw, hh)
            hd.setGeometry(self._handle_x_for(hw), (self.height() - 56) // 2, 16, 56)
            hd.show()
            hd.raise_()
        except Exception:
            pass

    def sync_handle_follow(self) -> None:
        """抽屉动画每帧回调：只移动手柄的 x（y 恒定 50% 居中，绝不改 y）。"""
        try:
            hd = self.drawer_handle
            if not hd.isVisible():
                return
            hy = max(0, (self.height() - 56) // 2)
            hd.setFixedSize(16, 56)
            hd.setGeometry(self._handle_x_for(16), hy, 16, 56)
        except Exception:
            pass

    def resizeEvent(self, ev):            # noqa: N802
        """舞台尺寸变化（含模式切换造成的布局变化）⇒ 手柄必须重新锁回 50%。

        ⚠️ 这就是"手柄飞到顶部"的漏点：原来只有图片相关路径会摆手柄，
           视频/音乐模式或窗口尺寸变化时不摆 ⇒ 停在旧坐标。
        """
        super().resizeEvent(ev)
        # v1.0.30 ①②：**只在这里**做几何不变量（废除到处散落的调用）。
        # 之前是"谁想起来谁调一次" ⇒ 首页/全屏都会漏 ⇒ 手柄半截在左上角、
        # AudioView 全屏不跟随（用户截图实证）。现在只要 Stage 尺寸一变就强制纠正。
        try:
            hd = self.drawer_handle
            hd.setFixedSize(16, 56)                  # 抽屉手柄标准物理规格
            hd.setGeometry(self.width() - 16, (self.height() - 56) // 2, 16, 56)
        except Exception:
            pass
        av = getattr(self, "audio_view", None)
        if av is not None and av.isVisible():
            av.setGeometry(0, 0, self.width(), self.height())
        try:
            self._sync_audio_view_geometry()
        except Exception:
            pass

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
        v = getattr(self, "image_view", None)
        for z in (getattr(v, "zone_left", None), getattr(v, "zone_right", None)):
            if z is None:
                continue
            try:
                z.setVisible(bool(on))
                z.setEnabled(bool(on))
            except Exception:
                pass
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
            # ⚠️ 抽屉展开期间热区必须保持失效（set_hotspots_enabled(False) 设过可见性），
            #    这里不能用 show() 把它强行拉回来。
            try:
                d = getattr(self, "_drawer", None)
                if d is not None and d.is_open():
                    z.hide()
                    continue
            except Exception:
                pass
            z.show()
            z.raise_()
        # ---- 抽屉手柄：改挂舞台，垂直**严格 50% 居中**（与热区箭头同一几何中心）----
        try:
            # 手柄定位统一交给 sync_handle_geometry（y 按舞台高度算，不依赖视口几何）
            self.sync_handle_geometry()
        except Exception:
            pass
        # ---- 鸟瞰图：改挂舞台并排在**热区之后**（Z 序高于热区，拖动不再被拦截）----
        try:
            v2 = getattr(self, "image_view", None)
            mm = getattr(v2, "minimap", None) if v2 is not None else None
            if mm is not None:
                if mm.parentWidget() is not self:
                    mm.setParent(self)
                mx = g.left() + max(8, (v2.width() if v2 is not None else g.width())
                                    - mm.width() - 16)
                my = (g.top() + 12 if getattr(v2, "_minimap_pos", "bottom_right") == "top_right"
                      else g.top() + max(12, (v2.height() if v2 is not None else g.height())
                                         - mm.height() - 76))
                mm.setGeometry(mx, my, mm.width(), mm.height())
                mm.show()
                mm.raise_()                                    # 最后抬升 ⇒ 热区之下不再拦它
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

    def resizeEvent(self, ev):
        # v1.0.31 ★★ 几何不变量（**必须放在最后一个 resizeEvent 里**：
        #   本类里 resizeEvent 定义过两次，后者会覆盖前者 —— 我上一版把不变量
        #   放在被覆盖的那个里，于是从未执行 ⇒ 首页手柄在左上角、全屏 AudioView
        #   不跟随。这是一切"手柄残缺/视口缩水"的唯一真因。）
        try:
            hd = self.drawer_handle
            hd.setFixedSize(16, 56)
            # ★★ v1.0.32 手柄 X 坐标解绑（截图 1 的"半截手柄"根因）：
            #   旧写法 `drawer.mapTo(self).x()` 拿到的是抽屉**宿主**的原点，
            #   抽屉本体是右对齐在宿主里的 ⇒ 算出来的 x 落在抽屉**内部** ⇒
            #   16px 的手柄被抽屉不透明面板盖掉大半。
            #   正确做法：抽屉左边缘 = 宿主右边缘 − 抽屉当前宽度。
            # v1.0.34 ②：用**明确的展开布尔状态**判定，收起时无条件贴最右。
            #   旧写法 `host_right - d.width()` 在收起时仍减去 392px ⇒ 手柄漂到
            #   屏幕中央 770px（用户截图）。
            d = getattr(self, "_drawer", None)
            is_open = False
            try:
                is_open = bool(d is not None and d.isVisible()
                               and getattr(d, "is_open", lambda: False)())
            except Exception:
                is_open = False
            if is_open and d is not None:
                try:
                    handle_x = int(d.mapTo(self, QPoint(0, 0)).x()) - 16
                except Exception:
                    handle_x = self.width() - 16
            else:
                handle_x = self.width() - 16
            hd.setGeometry(max(0, handle_x), (self.height() - 56) // 2, 16, 56)
            hd.raise_()          # 手柄必须在抽屉之上，绝不允许被盖住 1 像素
            hd.raise_()
        except Exception:
            pass
        try:
            av = getattr(self, "audio_view", None)
            if av is not None and av.isVisible():
                av.setGeometry(0, 0, self.width(), self.height())
        except Exception:
            pass
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
