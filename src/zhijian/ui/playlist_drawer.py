"""右侧播放列表侧滑抽屉。

架构保证（用户红线）：抽屉是**主窗口布局内**的一个子控件（QVBoxLayout 的内容行，
右侧一格），不是独立顶层窗口, 也不覆盖底部控制栏 —— 它靠布局把视频区挤窄,
所以"背景向下穿透切断底部控制栏"在结构上就不可能发生。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import (Property, QEasingCurve, QModelIndex, QPoint, QPointF,
                            QPropertyAnimation, QRect, QRectF, QSize, Qt, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QImage, QPainter,
                        QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListView,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget
)

from . import icons
from .settings_panel import install_feather, make_transparent   # ★ task-111：28px 羽化/透明复用同一份实现
# ★ task-17：液态玻璃（glass-light）皮肤下抽屉底板/亮边**自绘** —— 参数与绘制函数
#    与规格胶囊 SpecBadge 完全同源（theme.GLASS_FILL_DRAWER / GLASS_EDGE +
#    audio_view.paint_glass_plate）。既有 7 套主题仍走 QSS（见 theme.build_stylesheet）。
from .audio_view import paint_glass_plate
from .theme import (GLASS_EDGE, GLASS_FILL_DRAWER, M, P, css, is_glass_skin,
                    qt_color)
from .widgets import (GlassBlurPlate, IconButton, TextIconButton, fmt_time, prep_popup,
                      RoundedMenu)
from ..media.thumbnails import ThumbnailService   # ★ task-121③：后台异步缩略图管线

#: 玻璃底板/亮边预解析（绘制期零字符串解析）
_GLASS_FILL_DRAWER_COLOR = qt_color(GLASS_FILL_DRAWER)
_GLASS_EDGE_COLOR = qt_color(GLASS_EDGE)

log = logging.getLogger("zhijian.ui.playlist")

ROLE_DURATION = Qt.UserRole + 1
ROLE_PATH = Qt.UserRole + 2
ROLE_PLAYING = Qt.UserRole + 3
ROLE_KIND = Qt.UserRole + 4
ROLE_INDEX = Qt.UserRole + 5


def _is_light_theme() -> bool:
    """★ task-128：当前是否浅色玻璃主题（文字/选中色按主题分档）。"""
    try:
        from . import theme as _theme

        return bool(_theme.is_light())
    except Exception:
        return False


#: ★ task-121：视图模式（列表 / 四宫格缩略图）
VIEW_LIST = "list"
VIEW_GRID = "grid"
GRID_W = 160
GRID_H = 136
GRID_THUMB_H = 86
GRID_PAD = 6


class _ViewIconButton(QWidget):
    """★ task-128①：播放列表视图切换按钮 —— **常态 100% 透明无边框**，仅悬停浮现柔光圆底。

    图标按尺规自绘（不依赖 icons.py）：
      · 图标视图（当前是列表时显示它）：四宫格 = 4 个 5.5×5.5 圆角小方，间距 3px、线宽 1.5px
      · 列表视图（当前是图标时显示它）：三横线 = 3 条等距圆角短线，长 14px、间距 4px、线宽 1.6px
    """

    clicked = Signal()

    def __init__(self, parent=None, size: int = 26):
        super().__init__(parent)
        self.setObjectName("btnPlaylistView")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setAutoFillBackground(False)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("切换为四宫格缩略图")
        self._mode = VIEW_LIST          # 显示"当前视图"对应的切换目标图标
        self._hover = False

    def set_mode(self, mode: str) -> None:
        self._mode = VIEW_GRID if mode == VIEW_GRID else VIEW_LIST
        self.setToolTip("切换为列表视图" if self._mode == VIEW_GRID else "切换为四宫格缩略图")
        self.update()

    def enterEvent(self, ev):            # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):            # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def mouseReleaseEvent(self, ev):     # noqa: N802
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def paintEvent(self, _ev):           # noqa: N802
        try:
            self._paint_icon()
        except Exception:
            log.warning("视图切换按钮绘制失败", exc_info=True)

    def _paint_icon(self) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            w, h = float(self.width()), float(self.height())
            if self._hover:
                c = QColor(128, 128, 128)
                c.setAlphaF(0.15)
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                p.drawEllipse(QRectF(1.0, 1.0, w - 2.0, h - 2.0))
            col = qt_color(P["text_dim"] if not self._hover else P["text"])
            cx, cy = w / 2.0, h / 2.0
            if self._mode == VIEW_LIST:
                # 三横线：长 14、间距 4、线宽 1.6（圆头）
                pen = QPen(col, 1.6, Qt.SolidLine, Qt.RoundCap)
                p.setPen(pen)
                for dy in (-4.0, 0.0, 4.0):
                    p.drawLine(QPointF(cx - 7.0, cy + dy), QPointF(cx + 7.0, cy + dy))
            else:
                # 四宫格：4 个 5.5×5.5 圆角方，间距 3、线宽 1.5
                pen = QPen(col, 1.5)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                cell, gap, rad = 5.5, 3.0, 1.2
                total = cell * 2 + gap
                x0, y0 = cx - total / 2.0, cy - total / 2.0
                for iy in (0.0, cell + gap):
                    for ix in (0.0, cell + gap):
                        p.drawRoundedRect(QRectF(x0 + ix, y0 + iy, cell, cell), rad, rad)
        finally:
            p.end()


class GridDelegate(QStyledItemDelegate):
    """四宫格缩略图卡片（task-121②）：缩略图 + 双行标题 + 时长胶囊 + 当前播放高亮/徽标。"""

    def __init__(self, list_widget, service):
        super().__init__(list_widget)
        self._list = list_widget
        self._svc = service
        self._scaled: "dict[tuple, object]" = {}
        self._scaled_order: list = []
        self._font = QFont()
        self._font.setPixelSize(12)
        self._font_dur = QFont(self._font)
        self._font_dur.setPixelSize(11)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(GRID_W, GRID_H)

    def warm_one(self, item) -> None:
        """预热单张卡片的缩放 pixmap（供 _warm_grid 逐帧调用）。"""
        try:
            if item is None:
                return
            path = str(item.data(ROLE_PATH) or "")
            if not path:
                return
            img = self._svc.cached(path)
            if img is None:
                return
            tw, th = GRID_W - 2 * GRID_PAD, GRID_THUMB_H
            key = (path, tw, th)
            if key in self._scaled:
                return
            pm = QPixmap.fromImage(img.scaled(QSize(tw, th), Qt.KeepAspectRatioByExpanding,
                                              Qt.SmoothTransformation))
            self._scaled[key] = pm
            self._scaled_order.append(key)
            while len(self._scaled_order) > 240:
                self._scaled.pop(self._scaled_order.pop(0), None)
        except Exception:
            log.debug("网格预热单卡失败", exc_info=True)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        try:
            self._paint(painter, option, index)
        except Exception:
            log.warning("网格卡片绘制失败", exc_info=True)

    def _paint(self, painter: QPainter, option, index: QModelIndex):
        from PySide6.QtWidgets import QStyle

        path = str(index.data(ROLE_PATH) or "")
        kind = str(index.data(ROLE_KIND) or "")
        playing = bool(index.data(ROLE_PLAYING))
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        r = QRect(option.rect).adjusted(3, 3, -3, -3)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        # 卡片底
        card = QPainterPath()
        card.addRoundedRect(float(r.x()), float(r.y()), float(r.width()), float(r.height()), 10, 10)
        if playing:
            painter.fillPath(card, QColor(120, 200, 255, 34))       # 冰蓝底
        elif selected:
            painter.fillPath(card, qt_color(P["accent_soft"]))
        elif hovered:
            painter.fillPath(card, qt_color(P["glass_hover"]))
        else:
            painter.fillPath(card, QColor(255, 255, 255, 14))
        if playing:
            pen = QPen(QColor(126, 206, 255, 235), 1.6)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(card)

        # 缩略图区
        tr = QRect(r.x() + GRID_PAD, r.y() + GRID_PAD,
                   r.width() - 2 * GRID_PAD, min(GRID_THUMB_H, r.height() - 34))
        thumb_path = QPainterPath()
        thumb_path.addRoundedRect(float(tr.x()), float(tr.y()), float(tr.width()), float(tr.height()), 7, 7)
        img = self._svc.cached(path) if path else None
        if img is None and path:
            self._svc.request(path, kind or "video")
        painter.save()
        painter.setClipPath(thumb_path)
        if img is not None and not img.isNull():
            # ★ 性能：平滑缩放只在首次做一次并缓存（否则每帧每卡都做 ⇒ 切换瞬间掉帧）
            key = (path, tr.width(), tr.height(), int(getattr(self._svc, 'hits', 0)))
            key = (path, tr.width(), tr.height())
            pm = self._scaled.get(key)
            if pm is None:
                pm = QPixmap.fromImage(img.scaled(tr.size(), Qt.KeepAspectRatioByExpanding,
                                                  Qt.SmoothTransformation))
                self._scaled[key] = pm
                self._scaled_order.append(key)
                while len(self._scaled_order) > 240:
                    old = self._scaled_order.pop(0)
                    self._scaled.pop(old, None)
            painter.drawPixmap(tr, pm)
        else:
            painter.fillRect(tr, QColor(255, 255, 255, 16))
            painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
            painter.drawLine(tr.x() + 8, tr.y() + tr.height() // 2, tr.right() - 8, tr.y() + tr.height() // 2)
        painter.restore()

        # 时长胶囊（右下角，半透明）
        dur = float(index.data(ROLE_DURATION) or 0.0)
        if dur > 0.5:
            painter.setFont(self._font_dur)
            txt = fmt_time(dur)
            fm = QFontMetrics(self._font_dur)
            tw = fm.horizontalAdvance(txt) + 12
            cr = QRect(tr.right() - tw - 4, tr.bottom() - 20, tw, 16)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(10, 12, 16, 168))
            painter.drawRoundedRect(cr, 8, 8)
            painter.setPen(QColor(255, 255, 255, 232))
            painter.drawText(cr, Qt.AlignCenter, txt)

        # 当前播放徽标（左上角三角形/圆点）
        # ★ task-128③：角落**三柱小声波**徽标（替代旧实心三角）
        if playing:
            bw, bh, gap = 2.2, 8.0, 1.8
            total = bw * 3 + gap * 2
            bx = tr.right() - total - 6.0
            by = tr.bottom() - 6.0
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(10, 14, 22, 150))
            painter.drawRoundedRect(QRectF(bx - 4.0, by - bh - 4.0, total + 8.0, bh + 8.0), 5.0, 5.0)
            painter.setBrush(QColor(126, 206, 255, 245) if not _is_light_theme() else QColor(15, 98, 254, 245))
            for _bi, _hgt in enumerate((5.0, 8.0, 6.2)):
                painter.drawRoundedRect(QRectF(bx + _bi * (bw + gap), by - _hgt, bw, _hgt), 1.1, 1.1)

        # 标题（两行省略）
        # ★ task-128②：文字颜色按主题/选中态（修"浅色玻璃下白字看不清"）
        _f = QFont(self._font)
        if _sel:
            _f.setWeight(QFont.DemiBold)
        painter.setFont(_f)
        if _sel:
            painter.setPen(QColor(15, 98, 254) if _light else QColor(88, 166, 255))
        else:
            painter.setPen(QColor(24, 28, 36) if _light else QColor(255, 255, 255, 217))
        title = str(index.data(Qt.DisplayRole) or "")
        trect = QRect(r.x() + GRID_PAD, tr.bottom() + 4, r.width() - 2 * GRID_PAD, 30)
        fm = QFontMetrics(self._font)
        elided = fm.elidedText(title, Qt.ElideRight, trect.width())
        painter.drawText(trect, Qt.AlignLeft | Qt.AlignTop, elided)
        painter.restore()


class PlaylistDelegate(QStyledItemDelegate):
    """左：序号/播放指示 + 文件名（自动省略）；右：时长。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._font = QFont()
        self._font.setPixelSize(13)
        self._font_small = QFont(self._font)
        self._font_small.setPixelSize(12)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(120, M["drawer_item_h"])

    def paint(self, painter: QPainter, option, index: QModelIndex):
        # ⚠️ PySide6 会**静默吞掉** delegate.paint 里抛出的异常（只打一行日志, 列表
        #    直接变成空白）, 而且 `option.state` 是 StateFlag 枚举, 用整数掩码会抛
        #    `unsupported operand type(s) for &: 'StateFlag' and 'int'`。
        #    这两件事叠加 = "列表有数据但一个字都不显示", 极难查。所以这里整体兜底。
        try:
            self._paint(painter, option, index)
        except Exception:
            log.exception("播放列表项绘制失败")
            try:
                super().paint(painter, option, index)
            except Exception:
                pass

    def _paint(self, painter: QPainter, option, index: QModelIndex):
        from PySide6.QtWidgets import QStyle

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = QRect(option.rect).adjusted(6, 2, -6, -2)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        playing = bool(index.data(ROLE_PLAYING))
        # ★ task-128③：列表行 = 柔和流光底（无描边）+ 左侧 3×16px 主题色指示短柱
        _light = _is_light_theme()
        if selected or playing:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(32, 128, 250, 26) if _light else QColor(56, 152, 255, 38))
            painter.drawRoundedRect(r, 8, 8)
            painter.setBrush(qt_color(P["accent"]))
            painter.drawRoundedRect(QRectF(r.left() + 1.0, r.center().y() - 8.0, 3.0, 16.0), 1.5, 1.5)
        elif hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(qt_color(P["glass_hover"]))
            painter.drawRoundedRect(r, 8, 8)

        pad = M["drawer_item_pad"]
        # 序号 / 播放指示
        idx = index.data(ROLE_INDEX)
        marker_w = 26
        if playing:
            pm = icons.render_pixmap("play", P["accent"], 14,
                                     option.widget.devicePixelRatioF() if option.widget else 2.0, 2.0)
            painter.drawPixmap(r.left() + pad, r.center().y() - 7, pm)
        else:
            painter.setFont(self._font_small)
            _mc = qt_color(P["accent"]) if selected else (
                QColor(24, 28, 36) if _light else QColor(255, 255, 255, 140))
            painter.setPen(_mc)
            painter.drawText(QRect(r.left() + pad - 4, r.top(), marker_w, r.height()),
                             Qt.AlignVCenter | Qt.AlignLeft, str(idx if idx is not None else ""))

        # 时长（右）
        dur = index.data(ROLE_DURATION) or 0
        dur_text = fmt_time(dur) if dur else "--:--"
        painter.setFont(self._font_small)
        fm_small = QFontMetrics(self._font_small)
        dw = max(44, fm_small.horizontalAdvance("00:00:00") + 6)
        painter.setPen(qt_color(P["accent"] if playing else P["text_muted"]))
        painter.drawText(QRect(r.right() - dw - pad, r.top(), dw, r.height()),
                         Qt.AlignVCenter | Qt.AlignRight, dur_text)

        # 文件名
        name = index.data(Qt.DisplayRole) or ""
        text_rect = QRect(r.left() + pad + marker_w, r.top(),
                          r.width() - pad * 2 - marker_w - dw - 6, r.height())
        painter.setFont(self._font)
        fm = QFontMetrics(self._font)
        elided = fm.elidedText(name, Qt.ElideMiddle, max(20, text_rect.width()))
        painter.setPen(qt_color(P["accent"] if (selected or playing) else P["text_dim"]))
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.restore()


class PlaylistDrawer(QWidget):
    """侧滑抽屉。宽度用自定义属性驱动动画, 全程由布局接管。"""

    item_activated = Signal(str)          # path
    request_add_files = Signal()
    request_clear = Signal()
    request_remove = Signal(str)
    request_reveal = Signal(str)
    request_sort = Signal(str)
    #: 抽屉宽度逐帧变化（动画期间每帧一次）——手柄随动绑定用
    width_changed = Signal(int)
    open_state_changed = Signal(bool)
    hover_entered = Signal()
    hover_left = Signal()

    SORTS = [("name", "按名称"), ("path", "按路径"), ("duration", "按时长"), ("kind", "按类型")]

    def __init__(self, parent=None, width: int = M["drawer_w"]):
        super().__init__(parent)
        self.setObjectName("Drawer")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._target_w = int(width)
        self._w = 0
        self._open = False
        self._filter = ""
        self._sort = "name"
        self._asc = True
        self._duration_cache: dict[str, float] = {}
        self.setFixedWidth(0)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(6)

        # ---- 头部 ----
        head = QHBoxLayout()
        head.setContentsMargins(4, 0, 0, 0)
        head.setSpacing(2)
        self.title = QLabel("播放列表 (0)", self)
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setWeight(QFont.Medium)
        self.title.setFont(f)
        self.title.setProperty("role", "body")
        head.addWidget(self.title)
        head.addStretch(1)
        self.btn_add = IconButton("add", self, tooltip="添加文件", size=26, icon_size=15)
        self.btn_add.clicked.connect(self.request_add_files.emit)
        self.btn_clear = IconButton("trash_all", self, tooltip="清空列表", size=26, icon_size=15)
        self.btn_clear.clicked.connect(self.request_clear.emit)
        self.btn_view = _ViewIconButton(self, size=26)
        self.btn_view.setObjectName("btnPlaylistView")
        self.btn_view.setToolTip("切换为四宫格缩略图")
        self.btn_view.setCursor(Qt.PointingHandCursor)
        self.btn_view.clicked.connect(self.toggle_view)
        self.btn_sort = IconButton("sort", self, tooltip="排序方式", size=26, icon_size=15)
        self.btn_sort.clicked.connect(self._show_sort_menu)
        # ⚠️ 这里**故意没有**收起按钮：收起由画面接缝上那个常驻手柄负责
        #    （收起时在窗口右边缘、展开时在抽屉左边缘，是同一个控件）。
        #    早先这里有个 chevron 按钮，用户反馈"展开以后收起跑到右上角了很割裂"。
        for b in (self.btn_view, self.btn_add, self.btn_clear, self.btn_sort):
            head.addWidget(b)
        root.addLayout(head)

        # ---- 搜索 ----
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索播放列表…")
        self.search.setFixedHeight(M["drawer_search_h"])
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_filter)
        root.addWidget(self.search)

        # ---- 列表 ----
        self.list = QListWidget(self)
        self.list.setObjectName("Playlist")
        # ★ task-121：缩略图服务 + 两套 delegate + 视图模式（懒解码，主线程零阻塞）
        self._thumbs = ThumbnailService(cache_dir=self._thumb_cache_dir(), parent=self)
        self._thumbs.ready.connect(self._on_thumb_ready)
        self._delegate_list = PlaylistDelegate(self.list)
        self._delegate_grid = GridDelegate(self.list, self._thumbs)
        self._view_mode = VIEW_LIST
        self.list.setItemDelegate(self._delegate_list)
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.itemActivated.connect(self._on_double_click)
        self.list.itemClicked.connect(self._on_item_clicked)   # ★ task-121：grid 单击即切歌
        self.list.setMouseTracking(True)
        # ★ task-111②：播放列表抽屉的滚动区同样做 28px 上下 Alpha 羽化 + 通透底色
        make_transparent(self.list, "zjPlaylist")
        try:
            make_transparent(self.list.viewport(), "zjPlaylistVp")
        except Exception:
            pass
        try:
                self._feather = install_feather(self.list, 24)   # task-125②
        except Exception:
                log.warning("安装抽屉滚动遮罩失败", exc_info=True)
        root.addWidget(self.list, 1)

        # ★ task-121①：恢复持久化的视图模式（首次显示前摆好，避免闪一帧列表）
        self._apply_view_mode(self._cfg_view_mode(), persist=False)

        self.empty_hint = QLabel("列表为空 —— 把文件或文件夹拖进来，或点右上角 +", self)
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setProperty("role", "muted")
        self.empty_hint.setVisible(False)
        root.addWidget(self.empty_hint)

        self._anim = QPropertyAnimation(self, b"drawerWidth", self)
        self._anim.setDuration(M["drawer_anim_ms"])
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._on_anim_done)
        # v1.0.22：把动画的每一帧广播出去 —— 抽屉手柄据此**逐帧**贴合抽屉左边缘。
        # 旧实现只在开合状态变化时把手柄移到终点坐标 ⇒ 肉眼可见的"瞬移闪现"。
        self._anim.valueChanged.connect(
            lambda v: self.width_changed.emit(int(v)))

        self._items: list[dict] = []

    # -- 属性 ----------------------------------------------------------------
    def get_drawer_width(self) -> int:
        return self._w

    def set_drawer_width(self, v: int):
        v = max(0, int(v))
        self._w = v
        # ⚠️ 宽度改的是**外面那个 slot**（舞台里的覆盖层挂载点），不是抽屉自己。
        #    抽屉自己保持**目标宽度**、被 slot 裁掉 —— 这样动画期间抽屉内部
        #    （列表、搜索框、按钮）一次布局都不用重跑，也不会去推动视频控件。
        #    实测：改自己宽度时整个窗口 relayout 26.7ms/帧（11fps），改 slot 后 ~1ms。
        slot = getattr(self, "_slot", None)
        if slot is not None:
            slot.setFixedWidth(v)
            if self.width() != self._target_w:
                self.setFixedWidth(self._target_w)
        else:
            self.setFixedWidth(v)
        self.setVisible(v > 2)

    drawerWidth = Property(int, get_drawer_width, set_drawer_width)

    # -- 开关 ----------------------------------------------------------------
    def is_open(self) -> bool:
        return self._open

    def set_open(self, on: bool, animate: bool = True):
        on = bool(on)
        if on == self._open and self._w in (0, self._target_w):
            return
        self._open = on
        self._anim.stop()
        target = self._target_w if on else 0
        if animate:
            self._anim.setStartValue(self._w)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self.set_drawer_width(target)
        if on:
            self.setVisible(True)
            # 不要在这里 setFocus：搜索框一旦拿到焦点就会吃掉方向键,
            # 让"按右键快进 / 长按倍速"静默失效。用户点击搜索框时才给焦点。
        self.open_state_changed.emit(on)

    def _on_anim_done(self):
        if not self._open:
            self.setVisible(False)

    def toggle(self):
        self.set_open(not self._open)

    def set_target_width(self, w: int):
        self._target_w = max(M["drawer_min_w"], min(M["drawer_max_w"], int(w)))
        if self._open:
            self.set_drawer_width(self._target_w)

    # -- 数据 ----------------------------------------------------------------
    def set_items(self, items: list, current_path: str = "", durations: dict | None = None):
        """items: MediaItem 列表（或 (path, kind) 元组）。"""
        if durations:
            self._duration_cache.update(durations)
        self._items = []
        for it in items:
            if isinstance(it, str):
                self._items.append({"path": it, "kind": "", "name": os.path.basename(it)})
            else:
                self._items.append({
                    "path": it.path,
                    "kind": getattr(it, "kind", ""),
                    "name": getattr(it, "name", os.path.basename(it.path)),
                    "duration": getattr(it, "duration", 0.0) or 0.0,
                })
        self._rebuild(current_path)

    def update_duration(self, path: str, duration: float):
        self._duration_cache[os.path.normcase(path)] = float(duration or 0.0)
        key = os.path.normcase(path)
        for i in range(self.list.count()):
            item = self.list.item(i)
            if os.path.normcase(item.data(ROLE_PATH) or "") == key:
                item.setData(ROLE_DURATION, float(duration or 0.0))
                break

    def set_current(self, path: str):
        key = os.path.normcase(path or "")
        for i in range(self.list.count()):
            item = self.list.item(i)
            is_cur = os.path.normcase(item.data(ROLE_PATH) or "") == key
            item.setData(ROLE_PLAYING, is_cur)
            if is_cur:
                self.list.setCurrentItem(item)
                self.list.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        self.list.viewport().update()

    def add_paths(self, paths: list[str]):
        existing = {os.path.normcase(i["path"]) for i in self._items}
        for p in paths:
            if os.path.normcase(p) in existing:
                continue
            self._items.append({"path": p, "kind": "", "name": os.path.basename(p), "duration": 0.0})
            existing.add(os.path.normcase(p))
        self._rebuild(self.current_path())

    def current_path(self) -> str:
        it = self.list.currentItem()
        return it.data(ROLE_PATH) if it else ""

    def remove_path(self, path: str):
        key = os.path.normcase(path)
        self._items = [i for i in self._items if os.path.normcase(i["path"]) != key]
        self._rebuild(self.current_path())

    def clear(self):
        self._items = []
        self._rebuild("")

    def resort(self, key: str, current_path: str = "") -> None:
        """#7 按指定键**重排底层播放序列**（不只是界面行）。

        ⚠️ 原来主窗口把排序请求 `connect(lambda _k: None)` 丢掉了 —— 抽屉只重排了
           界面行，`paths()` 返回的顺序没变 ⇒ ←/→ 翻页、热区切图与列表看到的
           顺序**不一致**（用户实测反馈）。这里让真实序列与展示顺序强一致。
        """
        k = str(key or "name")
        rev = not bool(self._asc)          # 与 _rebuild 里保持同一套（升/降序）
        # ⚠️ `natural_key` 在本模块**没有模块级导入**（`_rebuild` 用的是函数内局部导入），
        #    直接引用会 NameError ⇒ 排序静默失效（实测踩到，日志里是
        #    "重排播放序列失败: name 'natural_key' is not defined"）。
        from ..media.scanner import natural_key
        try:
            if k == "path":
                self._items.sort(key=lambda r: natural_key(r.get("path", "")), reverse=rev)
            elif k == "duration":
                self._items.sort(
                    key=lambda r: float(r.get("duration")
                                       or self._duration_cache.get(
                                           os.path.normcase(r.get("path", "")), 0.0) or 0.0),
                    reverse=rev)
            elif k == "kind":
                self._items.sort(key=lambda r: (r.get("kind", ""),
                                                 natural_key(r.get("name", ""))),
                                 reverse=rev)
            else:
                self._items.sort(key=lambda r: natural_key(r.get("name", "")), reverse=rev)
        except Exception as exc:               # 不再静默吞掉（排查排序失效时被坑过）
            import logging

            logging.getLogger("zhijian.ui.drawer").warning("重排播放序列失败: %s", exc)
            return
        self._sort = k
        self._rebuild(current_path)
    def paths(self) -> list[str]:
        return [i["path"] for i in self._items]

    # -- 内部 ----------------------------------------------------------------
    def _on_filter(self, text: str):
        self._filter = (text or "").strip().lower()
        self._rebuild(self.current_path())

    def _show_sort_menu(self):
        menu = RoundedMenu(self)
        for key, label in self.SORTS:
            cur = key == self._sort
            menu.add_row(label, ("↑" if self._asc else "↓") if cur else "",
                         on_click=lambda k=key: self._set_sort(k))
        menu.addSeparator()
        menu.add_row("反转顺序", on_click=self._reverse)
        menu.exec(self.btn_sort.mapToGlobal(self.btn_sort.rect().bottomLeft()))

    def _set_sort(self, key: str):
        if key == self._sort:
            self._asc = not self._asc
        else:
            self._sort = key
            self._asc = True
        self.request_sort.emit(key)
        self._rebuild(self.current_path())

    def _reverse(self):
        self._items.reverse()
        self._rebuild(self.current_path())

    def _rebuild(self, current: str = ""):
        from ..media.scanner import natural_key

        self.list.clear()
        rows = list(self._items)
        if self._filter:
            rows = [r for r in rows if self._filter in r["name"].lower()]
        key = self._sort
        rev = not self._asc
        if key == "name":
            rows.sort(key=lambda r: natural_key(r["name"]), reverse=rev)
        elif key == "path":
            rows.sort(key=lambda r: natural_key(r["path"]), reverse=rev)
        elif key == "duration":
            rows.sort(key=lambda r: float(r.get("duration") or self._duration_cache.get(
                os.path.normcase(r["path"]), 0.0)), reverse=rev)
        elif key == "kind":
            rows.sort(key=lambda r: (r.get("kind", ""), natural_key(r["name"])), reverse=rev)

        cur_key = os.path.normcase(current or "")
        for n, r in enumerate(rows, 1):
            item = QListWidgetItem(r["name"])
            item.setData(ROLE_PATH, r["path"])
            dur = float(r.get("duration") or self._duration_cache.get(os.path.normcase(r["path"]), 0.0) or 0.0)
            item.setData(ROLE_DURATION, dur)
            item.setData(ROLE_INDEX, n)
            item.setData(ROLE_KIND, r.get("kind", ""))
            item.setData(ROLE_PLAYING, os.path.normcase(r["path"]) == cur_key)
            item.setToolTip(r["path"])
            self.list.addItem(item)
        total = len(rows)
        self.title.setText(f"播放列表 ({total})")
        self.empty_hint.setVisible(total == 0)
        self.list.setVisible(total > 0)
        if total and not self.list.currentItem():
            self.list.setCurrentRow(0)
        self._apply_view_mode(self._view_mode, persist=False)

    # ---- ★ task-121：视图切换（列表 ⇄ 四宫格） ----------------------------------------
    def _thumb_cache_dir(self) -> str:
        """磁盘缩略图缓存目录（跟 config.json 同级的 thumbs/，随用户配置走）。"""
        try:
            cfg = self._cfg() if hasattr(self, "_cfg") else None
            base = os.path.dirname(getattr(cfg, "path", "") or "") if cfg is not None else ""
            if not base:
                base = os.path.join(os.path.expanduser("~"), ".zhijian_thumbs")
            return base
        except Exception:
            return os.path.join(os.path.expanduser("~"), ".zhijian_thumbs")

    def _cfg(self):
        """拿配置对象（抽屉自己没有 cfg ⇒ 从窗口取；拿不到返回 None）。"""
        try:
            return getattr(self.window(), "cfg", None)
        except Exception:
            return None

    def _cfg_view_mode(self) -> str:
        cfg = self._cfg()
        cur = ""
        try:
            cur = str(cfg.get("ui.playlist_view_mode", VIEW_LIST) or VIEW_LIST) if cfg is not None else VIEW_LIST
        except Exception:
            cur = VIEW_LIST
        return VIEW_GRID if cur == VIEW_GRID else VIEW_LIST

    def _save_view_mode(self, mode: str) -> None:
        cfg = self._cfg()
        if cfg is None:
            return
        try:
            cfg.set("ui.playlist_view_mode", mode)
            cfg.save()
        except Exception:
            log.debug("保存播放列表视图模式失败", exc_info=True)

    def view_mode(self) -> str:
        """锚点：当前视图模式（list / grid）。"""
        return self._view_mode

    def _view_icon(self, mode: str) -> QIcon:
        """自绘视图图标（不改 icons.py 的资源表）：list = 三条横线；grid = 2×2 方块。"""
        px = 16
        pm = QPixmap(px * 2, px * 2)
        pm.setDevicePixelRatio(2.0)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            col = qt_color(P["text_dim"])
            p.setPen(QPen(col, 1.6, Qt.SolidLine, Qt.RoundCap))
            if mode == VIEW_LIST:
                for i, y in enumerate((4.0, 8.0, 12.0)):
                    p.drawLine(3.0, y, 13.0 - (3.0 if i == 2 else 0.0), y)
            else:
                p.setBrush(col)
                p.setPen(Qt.NoPen)
                for gx in (3.0, 9.0):
                    for gy in (3.0, 9.0):
                        p.drawRoundedRect(QRectF(gx, gy, 4.6, 4.6), 1.2, 1.2)
        finally:
            p.end()
        return QIcon(pm)

    def _sync_view_button(self) -> None:
        if not getattr(self, "btn_view", None):
            return
        try:
            self.btn_view.set_mode(self._view_mode)
        except Exception:
            log.debug("设置视图按钮形态失败", exc_info=True)

    def toggle_view(self, mode: str | None = None) -> str:
        """切换视图（None = 取反）；返回切换后的模式。"""
        target = mode if mode in (VIEW_LIST, VIEW_GRID) else (
            VIEW_LIST if self._view_mode == VIEW_GRID else VIEW_GRID)
        self._apply_view_mode(target, persist=True)
        self._view_switches = int(getattr(self, "_view_switches", 0)) + 1
        return self._view_mode

    def _apply_view_mode(self, mode: str, persist: bool = False) -> None:
        mode = VIEW_GRID if mode == VIEW_GRID else VIEW_LIST
        self._view_mode = mode
        lw = self.list
        try:
            if mode == VIEW_LIST:
                lw.setViewMode(QListView.ListMode)
                lw.setGridSize(QSize())          # 无效尺寸 = 无网格（QSize(0,0) 会让所有 item 叠在原点）
                lw.setWordWrap(False)
                lw.setSpacing(0)
                lw.setItemDelegate(self._delegate_list)
            else:
                lw.setViewMode(QListView.IconMode)
                lw.setResizeMode(QListView.Adjust)
                lw.setGridSize(QSize(GRID_W + 10, GRID_H + 10))
                lw.setWordWrap(True)
                lw.setSpacing(2)
                lw.setItemDelegate(self._delegate_grid)
        except Exception:
            log.warning("应用视图模式失败", exc_info=True)
        self._sync_view_button()
        if persist:
            self._save_view_mode(mode)
        try:
            dg = getattr(self, "_delegate_grid", None)
            if dg is not None:
                dg._scaled.clear()
                dg._scaled_order.clear()
        except Exception:
            pass
        # ★ 分帧：逐项 sizeHint + 重绘推到下一轮事件循环，避免切换瞬间一帧 >16ms
        from PySide6.QtCore import QTimer as _QT
        _QT.singleShot(0, lambda m=mode: self._apply_item_sizes(m))
        if mode == VIEW_GRID:
            self._warm_grid(0)

    def _warm_grid(self, start: int = 0) -> None:
        """★ 切换性能：**逐帧**预热网格卡片的缩放缓存（每帧最多 1 张），避免首帧集中缩放掉帧。"""
        from PySide6.QtCore import QTimer as _QT
        try:
            dg = getattr(self, "_delegate_grid", None)
            if dg is None or self._view_mode != VIEW_GRID:
                return
            n = self.list.count()
            if start >= n:
                return
            dg.warm_one(self.list.item(start))
            _QT.singleShot(0, lambda i=start + 1: self._warm_grid(i))
        except Exception:
            log.debug("网格预热失败", exc_info=True)

    def _apply_item_sizes(self, mode: str) -> None:
        """★ 分帧：给每一项设置当前模式的 sizeHint（大批量时也不会卡一帧）。"""
        lw = self.list
        try:
            grid = (mode == VIEW_GRID)
            for i in range(lw.count()):
                it = lw.item(i)
                if it is None:
                    continue
                want = QSize(GRID_W, GRID_H) if grid else QSize(120, M["drawer_item_h"])
                if it.sizeHint() != want:
                    it.setSizeHint(want)
            lw.viewport().update()
        except Exception:
            log.debug("设置卡片尺寸失败", exc_info=True)

    def _on_thumb_ready(self, path: str, img) -> None:
        """缩略图就绪 ⇒ 只重绘视口（主线程零解码）。"""
        self._thumb_ready = int(getattr(self, "_thumb_ready", 0)) + 1
        try:
            self.list.viewport().update()
        except Exception:
            pass

    # ---- ★ task-128：给 QA 的选中行/指示柱锚点（逻辑坐标，窗口坐标系） ----------------
    def selected_row(self) -> int:
        """当前选中行索引（-1 = 无选中）。"""
        try:
            return int(self.list.currentRow())
        except Exception:
            return -1

    def row_rect(self, row: int = -1) -> list:
        """指定行的窗口矩形 [x, y, w, h]（逻辑像素；默认取选中行）。"""
        try:
            i = self.selected_row() if row < 0 else int(row)
            it = self.list.item(i)
            if it is None:
                return []
            r = self.list.visualItemRect(it)
            p0 = self.list.viewport().mapTo(self.window(), r.topLeft())
            # 与 PlaylistDelegate 一致：行矩形 = item rect 内缩 (6,2,-6,-2)
            return [int(p0.x()) + 6, int(p0.y()) + 2, int(r.width()) - 12, int(r.height()) - 4]
        except Exception:
            return []

    def bar_rect(self, row: int = -1) -> list:
        """★ 指示柱矩形 [x, y, 3, 16]（逻辑像素）—— 与 paint 里完全同源，供像素复验。"""
        r = self.row_rect(row)
        if not r:
            return []
        return [int(r[0] + 1), int(r[1] + r[3] // 2 - 8), 3, 16]

    def _on_item_clicked(self, item) -> None:
        # grid 模式：单击即切歌（卡片语义）；list 模式保持"单击选中、双击切换"
        if getattr(self, "_view_mode", VIEW_LIST) == VIEW_GRID:
            p = item.data(ROLE_PATH) if item is not None else ""
            if p:
                self.item_activated.emit(p)

    def _on_double_click(self, item: QListWidgetItem):
        p = item.data(ROLE_PATH)
        if p:
            self.item_activated.emit(p)

    def _context_menu(self, pos):
        item = self.list.itemAt(pos)
        menu = RoundedMenu(self)
        if item:
            p = item.data(ROLE_PATH)
            menu.add_row("播放", on_click=lambda: self.item_activated.emit(p))
            menu.add_row("在资源管理器中显示", on_click=lambda: self.request_reveal.emit(p))
            menu.addSeparator()
            menu.add_row("从列表移除", danger=True, on_click=lambda: self.request_remove.emit(p))
        menu.add_row("添加文件…", on_click=self.request_add_files.emit)
        menu.add_row("清空列表", danger=True, on_click=self.request_clear.emit)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    # -- 绘制 ----------------------------------------------------------------
    def paintEvent(self, ev):            # noqa: N802
        """液态玻璃皮肤：底板与 1px 高光边**自绘**（其余 7 套主题仍由 QSS 画）。

        ⚠️ 为什么玻璃皮肤不能用 QSS：本 Qt 版本里 QSS 的 rgba 背景会被**合成两次**
           （实测 alpha 41 → 渲染 75），像素必然冲出 [0.12,0.18] 规范带；
           而玻璃底板要求像素级精确（卡片 C 项是像素取样判据）。
        ⚠️ 几何与 GlassFrame 同一套语义：底板铺满控件矩形，1px 亮边画在
           内缩 0.5px 的矩形上 ⇒ 描边那一圈像素底下是满覆盖底板，可直接反解 token。
        """
        # ★★ task-84（QA-2 必修 1 / lead 裁决 3）：抽屉是**悬浮窗**，两种主题下都要垫
        #    实时局部模糊底衬 —— 只采样抽屉自身正下方的舞台画面，供半透底板透出。
        blur = GlassBlurPlate.instance()
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(body, float(M["radius_lg"]), float(M["radius_lg"]))
        if not is_glass_skin():
            # 既有主题：QSS 底板（已按 0.90~0.92 调过 alpha）叠在模糊纹理之上
            pb = QPainter(self)
            pb.setRenderHint(QPainter.Antialiasing, True)
            pb.setClipPath(clip)
            try:
                blur.paint_backdrop(pb, self, QRectF(self.rect()))
            except Exception:
                log.debug("抽屉模糊底衬失败", exc_info=True)
            pb.end()
            super().paintEvent(ev)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setClipPath(clip)
        try:
            blur.paint_backdrop(p, self, QRectF(self.rect()))
        except Exception:
            log.debug("抽屉模糊底衬失败", exc_info=True)
        p.setClipping(False)
        paint_glass_plate(p, body,
                          M["radius_lg"], _GLASS_FILL_DRAWER_COLOR, _GLASS_EDGE_COLOR,
                          fill_rect=QRectF(self.rect()))
        p.end()

    # -- 模糊底衬的注册 ------------------------------------------------------
    def showEvent(self, ev):            # noqa: N802
        super().showEvent(ev)
        try:
            GlassBlurPlate.instance().register(self)
        except Exception:
            pass

    def hideEvent(self, ev):            # noqa: N802
        super().hideEvent(ev)
        try:
            GlassBlurPlate.instance().unregister(self)
        except Exception:
            pass

    # -- 边缘悬停 ------------------------------------------------------------
    def enterEvent(self, ev):
        self.hover_entered.emit()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self.hover_left.emit()
        super().leaveEvent(ev)
