"""右侧播放列表侧滑抽屉。

架构保证（用户红线）：抽屉是**主窗口布局内**的一个子控件（QVBoxLayout 的内容行，
右侧一格），不是独立顶层窗口, 也不覆盖底部控制栏 —— 它靠布局把视频区挤窄,
所以"背景向下穿透切断底部控制栏"在结构上就不可能发生。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QModelIndex,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget
)

from . import icons
from .settings_panel import ScrollFeatherEffect, make_transparent   # ★ task-111：28px 羽化/透明复用同一份实现
# ★ task-17：液态玻璃（glass-light）皮肤下抽屉底板/亮边**自绘** —— 参数与绘制函数
#    与规格胶囊 SpecBadge 完全同源（theme.GLASS_FILL_DRAWER / GLASS_EDGE +
#    audio_view.paint_glass_plate）。既有 7 套主题仍走 QSS（见 theme.build_stylesheet）。
from .audio_view import paint_glass_plate
from .theme import (GLASS_EDGE, GLASS_FILL_DRAWER, M, P, css, is_glass_skin,
                    qt_color)
from .widgets import GlassBlurPlate, IconButton, fmt_time, prep_popup, RoundedMenu

#: 玻璃底板/亮边预解析（绘制期零字符串解析）
_GLASS_FILL_DRAWER_COLOR = qt_color(GLASS_FILL_DRAWER)
_GLASS_EDGE_COLOR = qt_color(GLASS_EDGE)

log = logging.getLogger("zhijian.ui.playlist")

ROLE_DURATION = Qt.UserRole + 1
ROLE_PATH = Qt.UserRole + 2
ROLE_PLAYING = Qt.UserRole + 3
ROLE_KIND = Qt.UserRole + 4
ROLE_INDEX = Qt.UserRole + 5


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
        if selected or playing:
            painter.setPen(Qt.NoPen)
            painter.setBrush(qt_color(P["accent_soft"]))
            painter.drawRoundedRect(r, 8, 8)
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
            painter.setPen(qt_color(P["accent"] if selected else P["text_muted"]))
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
        self.btn_sort = IconButton("sort", self, tooltip="排序方式", size=26, icon_size=15)
        self.btn_sort.clicked.connect(self._show_sort_menu)
        # ⚠️ 这里**故意没有**收起按钮：收起由画面接缝上那个常驻手柄负责
        #    （收起时在窗口右边缘、展开时在抽屉左边缘，是同一个控件）。
        #    早先这里有个 chevron 按钮，用户反馈"展开以后收起跑到右上角了很割裂"。
        for b in (self.btn_add, self.btn_clear, self.btn_sort):
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
        self.list.setItemDelegate(PlaylistDelegate(self.list))
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.itemActivated.connect(self._on_double_click)
        self.list.setMouseTracking(True)
        # ★ task-111②：播放列表抽屉的滚动区同样做 28px 上下 Alpha 羽化 + 通透底色
        make_transparent(self.list, "zjPlaylist")
        try:
            make_transparent(self.list.viewport(), "zjPlaylistVp")
        except Exception:
            pass
        try:
            self._feather = ScrollFeatherEffect(28, 28, self.list)
            self.list.setGraphicsEffect(self._feather)
        except Exception:
            log.debug("播放列表羽化挂载失败", exc_info=True)
        root.addWidget(self.list, 1)

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
