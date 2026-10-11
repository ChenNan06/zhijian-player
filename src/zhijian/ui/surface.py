"""浮层统一基类（Surface）与集中工厂。

## 为什么要有这个文件

项目在"圆角"上反复翻车（菜单方角 → 子菜单没底 → 下拉方角 → 提示条方角 →
弹窗右下角方角 → setMask 锯齿），根因不是某处 QSS 写错，而是**浮层的构造方式**：

* Qt 的弹窗不是"一个控件"，而是**好几层各画各的矩形**：
  `QComboBoxPrivateContainer`（独立顶层窗口，比列表还大一圈）+ `QListView` +
  `viewport` + `QScrollBar`（槽/滑块/端点按钮）。`border-radius` 只作用于
  **每个控件自己画的那块底**，所以"给列表写圆角"永远管不住滚动条和容器。
* `QMenu` / `QToolTip` / 下拉容器都是**原生顶层窗口**，窗口的角天生是方的：
  圆角只能靠"半透明窗口 + 不画角上的像素"来伪装。
* 用 `setMask` 去裁窗口更糟：1 位掩码**没有抗锯齿**，圆角会变成锯齿台阶 + 灰边。

所以规则收敛成一句话：**任何浮在界面之上的东西，都必须继承 `Surface`（或由
`surface_*` 工厂创建），并由它自己一次性把整块面板画完**，子控件一律透明。

## 用它可以保证什么

1. 窗口：`FramelessWindowHint + WA_TranslucentBackground + WA_NoSystemBackground`
   ⇒ 圆角外**天然透明**（不用 setMask，边缘是抗锯齿的）；
2. 投影：Qt 会给弹窗自动挂 `QGraphicsDropShadowEffect`（"一层暗一层阴"）⇒ 一律中和；
3. 绘制：底色 + 1px 描边 + 圆角由 `surface_paint()` **一次画完**，
   内容控件（列表/菜单行/滚动条）全部透明 ⇒ 结构上画不出方角；
4. 滚动条：统一的**细自绘滚动条**（4px、半透明、端点高度归零、留边距），
   不会溢出到圆角外侧；
5. 合规闸门：`audit_surfaces()` 枚举所有可见浮层，凡不是 Surface 的一律报违规
   （配合 `scripts\\00-check-surfaces.py` 的静态扫描）。
"""
from __future__ import annotations

import logging
import weakref

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QListView,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .theme import P, css, qt_color

log = logging.getLogger("zhijian.ui.surface")

# 已注册的浮层（换肤 / 审计用）
_SURFACES: list = []


def slim_scrollbar_qss() -> str:
    """浮层内统一使用的**细自绘滚动条**：4px、半透明、端点高度归零。

    ⚠️ 端点按钮（`add-line`/`sub-line`）必须 `height: 0`：它们正好落在面板的
       右下角/右上角，会以**矩形**压在圆角上 —— 用户截图里那个"直角小方框"就是它。
    """
    return (
        "QScrollBar:vertical{background:transparent;width:4px;margin:2px 0 2px 0;}"
        "QScrollBar::handle:vertical{background:%s;border-radius:2px;min-height:24px;}"
        "QScrollBar::handle:vertical:hover{background:%s;}"
        "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;background:transparent;border:none;}"
        "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
        "QScrollBar:horizontal{background:transparent;height:4px;margin:0 2px 0 2px;}"
        "QScrollBar::handle:horizontal{background:%s;border-radius:2px;min-width:24px;}"
        "QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0;background:transparent;border:none;}"
        "QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{background:transparent;}"
        % (css(P.get("scroll_hover", "#00000033")), css(P.get("text_muted", "#86909C")),
           css(P.get("scroll_hover", "#00000033")))
    )


class Surface:
    """浮层统一基类（**mixin**）。

    ⚠️ 为什么是 mixin：PySide6 不允许"两个 QWidget 派生类"多重继承
       （布局冲突）。菜单必须继承 `QMenu`、下拉弹窗继承 `QWidget`，
       所以把"浮层该有的一切"做成 mixin，用 `surface_init()` 统一施加。
    """

    SURFACE_RADIUS = 8.0
    SURFACE_PAD = 6

    # -- 初始化 ---------------------------------------------------------------
    def surface_init(self, radius: float | None = None, pad: int | None = None):
        if radius is not None:
            self.SURFACE_RADIUS = float(radius)
        if pad is not None:
            self.SURFACE_PAD = int(pad)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        # ⚠️ 属性必须在 setWindowFlags **之后**设：它会重建原生窗口，之前的属性会丢。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.surface_kill_effects()
        _SURFACES.append(weakref.ref(self))
        return self

    # -- 投影中和（Qt 会给弹窗自动挂 QGraphicsDropShadowEffect）----------------
    def surface_kill_effects(self):
        try:
            self.setGraphicsEffect(None)
        except Exception:
            pass
        for c in self.findChildren(QWidget):
            try:
                if c.graphicsEffect() is not None:
                    c.setGraphicsEffect(None)
            except Exception:
                pass

    # -- 绘制：整块面板一次画完 -------------------------------------------------
    def surface_paint(self, p: QPainter):
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(qt_color(P.get("hairline_strong", "#00000022")), 1.0))
        p.setBrush(qt_color(P.get("popup_bg", "#FFFFFF")))
        rad = float(self.SURFACE_RADIUS)
        p.drawRoundedRect(r, rad, rad)

    # -- 显示：收敛到屏幕内 + 尽量让系统也磨一次角 ------------------------------
    def surface_show_at(self, global_pos: QPoint):
        self.adjustSize()
        scr = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        w, h = self.width(), self.height()
        if scr is not None:
            g = scr.availableGeometry()
            x = min(max(g.left(), global_pos.x()), max(g.left(), g.right() - w))
            y = min(max(g.top(), global_pos.y()), max(g.top(), g.bottom() - h))
            self.move(x, y)
        else:
            self.move(global_pos)
        self.show()
        self.raise_()
        QTimer.singleShot(0, self._surface_dwm_round)

    def _surface_dwm_round(self):
        """能上就上：让 DWM 也给窗口磨一次角（失败不影响自绘的圆角）。"""
        try:
            from ..platform import win as _win

            _win.enable_rounded_corners(self, True)
            _win.set_window_border(self, False)
        except Exception:
            log.debug("浮层 DWM 圆角不可用（不影响自绘圆角）", exc_info=True)


def setup_surface(w: QWidget, radius: float = 8.0, pad: int = 6) -> QWidget:
    """工厂入口：把一个普通控件变成合规浮层。"""
    if not isinstance(w, Surface):
        raise TypeError("setup_surface 只接受 Surface 子类：%r" % type(w).__name__)
    w.surface_init(radius=radius, pad=pad)
    return w


class _PopupItemDelegate(QStyledItemDelegate):
    """弹窗列表项：hover / 选中的底色**由我们自己画**。

    ⚠️ 为什么不用 QSS 的 `QListView::item:hover`：在自管弹窗里实测**不生效**
       （用 delegate 以 `State_MouseOver` 确定性渲染同一项，像素完全一样 ⇒ 样式表那条规则
       根本没参与绘制）。菜单行 `MenuRow` 一直是自绘 hover 的 —— 统一走自绘，
       既可控又和菜单一致。hover 用 `glass_hover`（轻反馈），选中用 `accent_soft`。
    """

    def paint(self, p, opt, index):
        from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

        r = QRectF(opt.rect).adjusted(2.0, 1.0, -2.0, -1.0)
        selected = bool(opt.state & QStyle.State_Selected)
        hovered = bool(opt.state & QStyle.State_MouseOver)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        if selected or hovered:
            p.setPen(Qt.NoPen)
            p.setBrush(qt_color(P.get("accent_soft", "#0080F526") if selected
                                else P.get("glass_hover", "#0000000F")))
            p.drawRoundedRect(r, 6.0, 6.0)
        p.setPen(qt_color(P.get("accent", "#0080F5") if selected
                          else P.get("text", "#1D2129")))
        p.drawText(r.adjusted(8.0, 0.0, -8.0, 0.0), Qt.AlignLeft | Qt.AlignVCenter,
                   str(index.data(Qt.DisplayRole) or ""))
        p.restore()


class SurfacePopup(Surface, QWidget):
    """下拉框的弹窗（**完全自管**，不用 Qt 的 QComboBoxPrivateContainer）。

    为什么必须自管：Qt 的容器是**独立顶层窗口**，且比列表视图大一圈
    （实测 330×507 vs 220×338，那圈就是露方角的地方），内部滚动条的端点按钮
    还会以矩形压在圆角上。自管之后：**一张画布 + 一个透明列表 + 细自绘滚动条**，
    结构上不可能再出现方角。
    """

    picked = Signal(int)

    def __init__(self, combo, parent=None):
        QWidget.__init__(self, parent)
        self.surface_init(radius=8.0, pad=6)
        self._combo = combo
        self._view = QListView(self)
        self._view.setModel(combo.model())
        self._view.setFrameShape(QFrame.NoFrame)
        self._view.setUniformItemSizes(True)
        self._view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 列表自己**透明**：圆角卡片由 Surface 画，子控件画不出方角
        # ⚠️ hover 反馈（用户："QQ 里鼠标放上去不点也有轻反馈，颜色稍深，你的没有"）：
        #    控制栏按钮/导航栏/播放列表都有，弹窗列表**漏了** —— 这是"同一类控件
        #    该有一致反馈"的问题，不是某个列表的问题。
        #    ⚠️ `:hover` 必须写在 `:selected` **前面**：两者选择器权重相同，
        #       后写的赢；否则悬停在选中项上会把选中态盖掉。
        self._view.setStyleSheet(
            "QListView{background:transparent;border:none;outline:none;}"
            "QListView::item{min-height:26px;padding:3px 8px;border-radius:6px;}"
            "QListView::item:hover{background:%s;color:%s;}"
            "QListView::item:selected{background:%s;color:%s;}"
            "QListView::item:selected:hover{background:%s;color:%s;}"
            % (css(P.get("glass_hover", "#0000000F")), P.get("text", "#1D2129"),
               css(P.get("accent_soft", "#0080F526")), P.get("accent", "#0080F5"),
               css(P.get("accent_soft", "#0080F526")), P.get("accent", "#0080F5")))
        # 让 `::item:hover` 真的触发：视图与视口都要开 hover 跟踪
        self._view.setMouseTracking(True)
        self._view.viewport().setMouseTracking(True)
        self._view.viewport().setAttribute(Qt.WA_Hover, True)
        # 底色（hover/选中）交给自绘 delegate —— QSS 的 ::item:hover 在这里不生效
        self._view.setItemDelegate(_PopupItemDelegate(self._view))
        self._view.verticalScrollBar().setStyleSheet(slim_scrollbar_qss())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(self.SURFACE_PAD, self.SURFACE_PAD,
                               self.SURFACE_PAD, self.SURFACE_PAD)
        lay.setSpacing(0)
        lay.addWidget(self._view)
        self._view.clicked.connect(self._on_clicked)
        self._view.installEventFilter(self)

    # -- 尺寸：按内容算，不再出现"卡片比文字宽一大截" ---------------------------
    def fit_to_combo(self, max_rows: int = 12):
        rows = max(1, min(self._combo.count(), max_rows))
        rh = max(24, self._view.sizeHintForRow(0) + 6)
        w = max(self._combo.width(), self._view.sizeHintForColumn(0) + 34, 160)
        h = rows * rh
        self.setFixedSize(w + self.SURFACE_PAD * 2, h + self.SURFACE_PAD * 2)

    def show_for_combo(self):
        self.fit_to_combo()
        cur = self._combo.currentIndex()
        if cur >= 0:
            self._view.setCurrentIndex(self._combo.model().index(cur, 0))
        self._view.setFocus()
        anchor = self._combo.mapToGlobal(QPoint(0, self._combo.height() + 2))
        scr = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            if anchor.y() + self.height() > g.bottom():
                up = self._combo.mapToGlobal(QPoint(0, -self.height() - 2))
                if up.y() >= g.top():
                    anchor = up
        self.surface_show_at(anchor)

    def _on_clicked(self, index):
        self.picked.emit(index.row())
        self.hide()

    def eventFilter(self, obj, ev):  # noqa: N802
        if obj is self._view and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Escape, Qt.Key_Enter, Qt.Key_Return):
                if k != Qt.Key_Escape:
                    idx = self._view.currentIndex()
                    if idx.isValid():
                        self.picked.emit(idx.row())
                self.hide()
                return True
        return False

    def paintEvent(self, _ev):  # noqa: N802
        p = QPainter(self)
        self.surface_paint(p)
        p.end()


def audit_surfaces(app=None) -> dict:
    """合规闸门：枚举所有**可见浮层**，凡不是 Surface 的一律算违规。

    判定"浮层"的依据：窗口带 `Qt.Popup`，或者是 Qt 的私有弹层类
    （`QComboBoxPrivateContainer` / `QTipLabel` / `QMenu`）。
    """
    app = app or QApplication.instance()
    alive = []
    registered = set()
    for ref in list(_SURFACES):
        w = ref()
        if w is None:
            continue
        alive.append(ref)
        registered.add(id(w))
    _SURFACES[:] = alive
    out = {"surfaces": len(registered), "violations": [], "checked": 0}
    if app is None:
        return out
    for w in app.topLevelWidgets():
        try:
            if not w.isVisible():
                continue
            name = type(w).__name__
            flags = w.windowFlags()
            # ⚠️ 必须按**窗口类型掩码**判定：直接 `flags & Qt.Popup` 会把主窗口
            #    （Window=0x1）也算进来，审计就会误报（实测踩过）。
            wtype = flags & Qt.WindowType_Mask
            is_popup = wtype in (Qt.Popup, Qt.ToolTip) or name in (
                "QMenu", "QComboBoxPrivateContainer", "QTipLabel")
            if not is_popup:
                continue
            out["checked"] += 1
            if id(w) not in registered:
                out["violations"].append("%s(未继承 Surface)" % name)
            elif not w.mask().isEmpty():
                out["violations"].append("%s(用了 setMask：会有锯齿)" % name)
            elif w.graphicsEffect() is not None:
                out["violations"].append("%s(挂着投影特效)" % name)
        except Exception:
            pass
    return out


def visible_surfaces(app=None) -> list:
    """当前可见的浮层（诊断用）。"""
    app = app or QApplication.instance()
    out = []
    for ref in list(_SURFACES):
        w = ref()
        if w is None:
            continue
        try:
            if w.isVisible():
                out.append(w)
        except RuntimeError:
            pass
    return out
