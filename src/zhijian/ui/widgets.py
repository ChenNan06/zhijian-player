"""通用控件：玻璃面、图标按钮、进度条、音量条、OSD 提示。

所有面板都通过 Qt 布局器组织, 全项目**没有**任何手工 setGeometry / y += 30 的坐标计算。
"""

from __future__ import annotations

import logging
import weakref

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QWheelEvent
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QAbstractButton,
    QAbstractScrollArea,
    QApplication,
    QWidget,
    QMenu,
    QComboBox,
    QDoubleSpinBox,
    QSlider,
    QSpinBox,
)

# --------------------------------------------------------------------------------------
# 滚轮护栏：**先选中（聚焦）才允许滚轮改值**
# --------------------------------------------------------------------------------------
# 用户原话："滚轮调整数值是好的，但是要用户先选中输入框后再启用滚轮滚动功能，
#            否则用户向下滑的时候就不小心调设置了。"
# 做法：未聚焦时**把事件转给外层滚动区**（见 `wheelEvent`），于是"向下滑"变成
#      正常滚动设置页，而不是偷偷改掉一个数值。
# ⚠️ 只 `ev.ignore()` 是不够的（实测页面滚不动），必须显式转发。
class _FocusWheelGuard:
    """滚轮只在控件**已聚焦**时生效；否则让事件冒泡（设置页才能正常滚动）。"""

    def _wheel_armed(self) -> bool:
        """用户是否"已经选中"了这个控件。

        用 `hasFocus()` **或**"它就是本窗口的焦点控件"：
        后者在窗口不是前台时也成立（自动化探测里窗口常常不是前台），
        而真实使用中用户点一下就已经满足 `hasFocus()`。
        """
        if self.hasFocus():
            return True
        win = self.window()
        try:
            return win is not None and win.focusWidget() is self
        except Exception:
            return False

    def wheelEvent(self, ev):
        if self._wheel_armed():
            super().wheelEvent(ev)
            return
        # ⚠️ 实测：光 `ev.ignore()` **并不能**让设置页滚动（探针实测滚动条 0 → 0）——
        #    这些控件外面还套着 form/card 容器，事件在冒泡途中就没了。
        #    用户的原话是"用户只是想在设置页下滑"，所以必须**主动转给滚动区**：
        #    自己不吃这个事件，而是把它原样送给外层 QAbstractScrollArea 的 viewport。
        area = self._scroll_area()
        if area is not None:
            vp = area.viewport()
            fwd = QWheelEvent(ev.position(), ev.globalPosition(), ev.pixelDelta(), ev.angleDelta(),
                              ev.buttons(), ev.modifiers(), ev.phase(), ev.inverted())
            QApplication.sendEvent(vp, fwd)
            ev.accept()
            return
        ev.ignore()

    def _scroll_area(self):
        """往上找最近的滚动区（设置页/抽屉/字幕面板都是 QScrollArea）。"""
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, QAbstractScrollArea):
                return p
            p = p.parentWidget()
        return None


class NoWheelSpinBox(_FocusWheelGuard, QSpinBox):
    """见 `_FocusWheelGuard`。"""


class NoWheelDoubleSpinBox(_FocusWheelGuard, QDoubleSpinBox):
    """见 `_FocusWheelGuard`。"""


class NoWheelComboBox(_FocusWheelGuard, QComboBox):
    """下拉框同理：不聚焦时滚轮不该改选项。

    另外在**弹窗第一次显示前**把外层容器设成无边框+半透明，
    否则 QSS 里的圆角会被方形外壳包住（见 `round_popup()`）。
    弹窗是懒创建的，所以不能放在 `__init__` 里做。

    ⚠️ 自己画箭头：QSS 里把 `::drop-down` 的边框去掉之后，Qt 就不再画默认箭头了，
       下拉框看起来就是个普通输入框（用户根本不知道能点）。
       这里用调色板的弱色画一个小人字，跟着主题走。
    """

    _surface_popup = None

    def showPopup(self):                     # noqa: N802 (Qt 命名)
        """下拉弹窗**完全自管**（继承 Surface），不再用 Qt 的私有容器。

        为什么不用 `super().showPopup()`：Qt 的 `QComboBoxPrivateContainer` 是独立顶层窗口、
        比列表视图大一圈（实测 330×507 vs 220×338），内部滚动条的端点按钮还会以矩形
        压在圆角上 —— 那圈方角怎么修都修不干净。自管之后是一张画布，结构上不可能露方角。
        """
        if self.count() <= 0:
            return
        if self._surface_popup is None:
            self._surface_popup = SurfacePopup(self)
            self._surface_popup.picked.connect(self._on_surface_picked)
        self._surface_popup.show_for_combo()

    def _on_surface_picked(self, row: int):
        if 0 <= row < self.count():
            self.setCurrentIndex(row)
            self.currentIndexChanged.emit(row)
            self.activated.emit(row)
            self.textActivated.emit(self.itemText(row))
        self.update()

    def paintEvent(self, ev):                # noqa: N802
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        col = qt_color(P["text_muted"])
        if self.isEnabled() and self.underMouse():
            col = qt_color(P["text_dim"])
        w = 9.0
        cx = self.width() - 14.0
        cy = self.height() / 2.0
        pen = QPen(col)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        path = QPainterPath()
        path.moveTo(cx - w / 2, cy - 2.0)
        path.lineTo(cx, cy + 2.4)
        path.lineTo(cx + w / 2, cy - 2.0)
        p.drawPath(path)
        p.end()


class NoWheelSlider(_FocusWheelGuard, QSlider):
    """滑块同理。

    ⚠️ 实测（探针 `slider_probe`）：**QSlider 在未聚焦时也会被滚轮改值**
    （392 → 395），所以它和数值框一样必须加护栏，否则"向下滑设置页"会顺手改掉
    「播放列表宽度」这种值。

    ★ task-73（问题 ⑩）：QSS 的 handle 只能给纯色，画不出柔和投影，而且
      宽度=高度受 QSlider 样式几何影响很容易变椭/被上下边界削平。这里**自绘**
      滑块：控件 minHeight=28；圆点直径 13px 纯白 + 1px 细边 + ≤3px 柔投影，
      圆点+阴影离上下边界 ≥4px，三种主题一致。
    """

    HANDLE_R = 6.5          # 直径 13px（判据 12~14）
    SHADOW_R = 3.0          # 投影外扩 ≤3px

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setMinimumHeight(28)
        self.setMouseTracking(True)

    def paintEvent(self, _ev):  # noqa: N802
        from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
        from PySide6.QtWidgets import QStyle, QStyleOptionSlider

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        h = self.height()
        w = self.width()
        cy = h / 2.0
        # 轨道几何：左右各留 handle+阴影 + 1px，保证圆点不被边界裁切
        r = float(self.HANDLE_R)
        pad = r + self.SHADOW_R + 1.0
        track_h = 4.0
        tr = QRectF(pad, cy - track_h / 2.0, max(1.0, w - 2 * pad), track_h)
        # 值映射：与 QSlider 的实际命中范围一致（用 style 的 groove 做参照，若异常回退自算）
        frac = 0.0
        try:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(QStyle.CC_Slider, opt,
                                                 QStyle.SC_SliderGroove, self)
            handle = self.style().subControlRect(QStyle.CC_Slider, opt,
                                                 QStyle.SC_SliderHandle, self)
            if groove.width() > 1 and groove.height() >= 0:
                tr = QRectF(float(groove.left()), cy - track_h / 2.0,
                            max(1.0, float(groove.width())), track_h)
                frac = float(handle.center().x() - groove.left()) / max(1.0, float(groove.width()))
        except Exception:
            pass
        if frac <= 0.0:
            span = max(1, self.maximum() - self.minimum())
            frac = float(self.sliderPosition() - self.minimum()) / span
        frac = max(0.0, min(1.0, frac))
        hx = tr.left() + tr.width() * frac
        # 轨道
        p.setPen(Qt.NoPen)
        p.setBrush(qt_color(P["glass_track"]))
        p.drawRoundedRect(tr, track_h / 2.0, track_h / 2.0)
        # 已填充（玻璃皮肤走 task-72 的有色琉璃）
        from . import theme as _theme
        if _theme.is_glass_skin():
            grad = QLinearGradient(tr.topLeft(), tr.bottomLeft())
            grad.setColorAt(0.0, QColor(56, 148, 255, 158))
            grad.setColorAt(1.0, QColor(0, 102, 235, 199))
            p.setBrush(grad)
        else:
            p.setBrush(qt_color(P["accent"]))
        if hx > tr.left() + 0.5:
            p.drawRoundedRect(QRectF(tr.left(), tr.top(), hx - tr.left(), tr.height()),
                              track_h / 2.0, track_h / 2.0)
        # 柔投影（≤3px；圆心必须在控件内留 ≥4px）
        # 外圈 rgba(0,0,0,0.28) 起，向内略加深（≤3px，柔和但可测）
        for i, alpha in ((3.0, 40), (2.0, 56), (1.0, 71)):
            p.setBrush(QColor(0, 0, 0, alpha))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(hx, cy), r + i, r + i)
        # 纯白圆点 + 1px 半透明细边
        p.setBrush(QColor(255, 255, 255))
        p.setPen(QPen(QColor(0, 0, 0, 56), 1.0))
        p.drawEllipse(QPointF(hx, cy), r, r)
        p.end()

from . import icons
from .surface import Surface, SurfacePopup, slim_scrollbar_qss
from .theme import M, P, css, mix, qt_color

# ⚠️ 本模块原来直接用 `log.debug(...)` 却**没有定义 log**：
#    一旦走到那些 except 分支就会 NameError（把「记日志」变成「再抛一个异常」）。
log = logging.getLogger("zhijian.ui")

# 换肤广播的两张登记表（见 themed() / notify_theme_changed()）
_THEMED: list = []          # [(weakref(widget), 重放函数)]
_MENUS_LIVE: list = []      # [weakref(RoundedMenu)] —— 菜单是独立顶层窗口，要单独刷

# --------------------------------------------------------------------------------------
# 磨砂颗粒
# --------------------------------------------------------------------------------------
# ⚠️ 真正的"背景模糊"（苹果液态玻璃那种 backdrop blur）需要一次 GPU 后处理：
#    把玻璃后面那块画面降采样 → 分离式高斯模糊 → 玻璃再采样它。
#    在拿到那个之前，用**极低对比度的细颗粒**做磨砂质感 —— 这是业界通用的
#    "没有模糊时的玻璃替代方案"，比一块纯色板强得多。
#    颗粒只生成一次然后平铺，开销可以忽略。
_FROST_TILE = None


def _frost_tile():
    global _FROST_TILE
    if _FROST_TILE is None:
        import random

        n = 64
        img = QImage(n, n, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        rnd = random.Random(20261006)
        for y in range(n):
            for x in range(n):
                a = rnd.randint(0, 12)          # 0~12/255，肉眼只看到"细砂"，不脏
                img.setPixelColor(x, y, QColor(255, 255, 255, a))
        _FROST_TILE = QPixmap.fromImage(img)
    return _FROST_TILE


# --------------------------------------------------------------------------------------
# 玻璃面
# --------------------------------------------------------------------------------------

# 玻璃面背景缓存：key 里含尺寸与全部取色参数，所以换肤/改尺寸会自动失效。
#
# ⚠️ 必须按**总字节数**限流，不能只按条目数。一张抽屉大小的背景 pixmap
#    （392×590 逻辑 @1.5 = 588×885 物理 ×4B）就是 ~2MB；按"最多 8 条"来放，
#    上限是 16MB，而且尺寸一变就 churn 一次分配/释放 —— 实测 30 轮混合负载
#    峰值高水位会因此每轮涨 ~1MB（分配器碎片）。
#    现在：总量预算 6MB，单张超过 2MB 的直接不缓存（那种大块本来就很少重复画）。
_GLASS_BG: dict[tuple, QPixmap] = {}
_GLASS_BG_ORDER: list[tuple] = []
_GLASS_BG_BYTES_MAX = 6 * 1024 * 1024
_GLASS_BG_ONE_MAX = 2 * 1024 * 1024

# 所有活着的 GlassFrame。
# ⚠️ 必须用 **weakref**：用普通 list 存强引用会把已经从 Qt 侧销毁的控件
#    永远留在内存里（Python 层看不到它没了）—— 实测让内存测试的峰值
#    开始每轮涨 ~1MB。改成 weakref 后控件销毁就自动出列。
_GLASS_LIVE: list = []
_SHADOW_CACHE: dict = {}
_SHADOW_ORDER: list = []


# --------------------------------------------------------------------------------------
# 单层高斯软阴影
# --------------------------------------------------------------------------------------
# ⚠️⚠️ 这里换掉了"循环画 12/16 层递减 alpha 圆角矩形"的老算法。
#    那个做法**必然**在半透明边缘产生阶梯状脏边：每一层都是一个有硬边的圆角矩形，
#    十几层叠起来，抗锯齿边缘的 alpha 互相干涉，最外圈就成了一条能看出来的灰黑轮廓
#    —— 用户原话"像抠图没抠干净"。
#
#    现在：把圆角矩形**画一次**，然后交给 QGraphicsBlurEffect 做一次真正的高斯模糊。
#    因为 GlassFrame 的背景本来就整张缓存（_GLASS_BG），这次模糊一个尺寸只算一次。
def soft_shadow_pixmap(w: int, h: int, radius: float, blur: int,
                       dy: int, alpha: float) -> QPixmap:
    """生成一张"轻柔浮起"的单层弥散阴影图（四周留出 blur*2 的边距）。"""
    from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene

    pad = int(blur * 2)
    W, H = max(1, w + pad * 2), max(1, h + pad * 2)
    src = QPixmap(W, H)
    src.fill(Qt.transparent)
    sp = QPainter(src)
    sp.setRenderHint(QPainter.Antialiasing, True)
    sp.setPen(Qt.NoPen)
    sp.setBrush(QColor(0, 0, 0, max(1, int(round(255 * alpha)))))
    sp.drawRoundedRect(QRectF(pad, pad + dy, w, h), radius, radius)
    sp.end()

    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(src)
    eff = QGraphicsBlurEffect()
    eff.setBlurRadius(blur)
    eff.setBlurHints(QGraphicsBlurEffect.QualityHint)
    item.setGraphicsEffect(eff)
    scene.addItem(item)

    out = QPixmap(W, H)
    out.fill(Qt.transparent)
    op = QPainter(out)
    op.setRenderHint(QPainter.Antialiasing, True)
    scene.render(op, QRectF(0, 0, W, H), QRectF(0, 0, W, H))
    op.end()
    scene.clear()
    return out


def prep_popup(menu):
    """弹出菜单必须**透明 + 无原生边框**，否则 QSS 的圆角外侧会露出方形直角底。

    ⚠️⚠️ 关键在于**不能用 `|=` 去加** FramelessWindowHint。
    `QMenu.windowFlags()` 默认带着这些 hint：

        WindowTitleHint | WindowSystemMenuHint
        | WindowMinimizeButtonHint | WindowMaximizeButtonHint

    它们会让 **Windows 给菜单画一圈原生边框**（实测 flags = 0x4800f809）。
    用 `|=` 叠加 FramelessWindowHint 是盖不掉这些 hint 的 ——
    用户截图里那个“方形边框 bug 又复现”就是这个。
    **必须显式设定一个干净的 flags。**
    """
    menu.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    # 属性要在 `setWindowFlags` **之后** 设：
    # setWindowFlags 会销毁并重建原生窗口，之前设的属性会丢。
    menu.setAttribute(Qt.WA_TranslucentBackground, True)
    menu.setAttribute(Qt.WA_NoSystemBackground, True)
    # ⚠️⚠️ **绝对不要**给弹层加 `setMask` 圆角遮罩：
    #    setMask 是**硬边裁剪**（1 位掩码、没有抗锯齿），圆角会变成**锯齿台阶**，
    #    边缘还会被切出一圈灰边 —— 用户原话："为什么这个地方又会有锯齿阴影"。
    #    正确做法：窗口**正好等于**圆角面板的大小（RoundedMenu 自己画抗锯齿圆角），
    #    窗口半透明 ⇒ 圆角外侧天然透明且**平滑**。
    return menu


def _removed_clip_rounded_note():
    """（已删除的 `clip_rounded`）为什么不能用 `setMask` 做圆角：

    我一度给弹窗/菜单加了 `setMask(圆角矩形)`，想"结构性保证"不出方角 —— 结果更糟：
    `setMask` 是 **1 位掩码、没有抗锯齿**，圆角被切成**锯齿台阶**，边缘还多出一圈灰边。
    用户原话："为什么这个地方又会有锯齿阴影 边框也有阴影"。

    ⇒ 弹层的圆角只有一种正确做法：
      1. 窗口设 **无边框 + 半透明**（`WA_TranslucentBackground`）；
      2. 让窗口**正好等于**圆角面板的大小（QComboBox 要显式对齐，见 `fit_popup_to_view`）；
      3. 圆角交给**抗锯齿的绘制**（QSS `border-radius` 或 `QPainterPath`），
         窗口外自然透明、边缘平滑。
    """


def round_popup(combo):
    """让下拉框的**弹窗**也带圆角（设置页里"还有方角"就是它）。

    ⚠️ 只给 `QComboBox QAbstractItemView` 设 border-radius 是不够的：
       弹窗是**独立的顶层窗口**，外面还裹着一层 `QComboBoxPrivateContainer`，
       系统给它画的是方形边框 —— 里面那层圆角被方形外壳包着，看着就是方的。
       必须把弹窗设成 **无边框 + 半透明**，并把容器底色清掉，
       这样"圆角面板"才露得出来。道理与 `prep_popup()` 完全相同（见那里的注释）。

    ⚠️⚠️ 圆角**不要**用 `setMask` 去裁窗口：setMask 是 1 位掩码、**没有抗锯齿**，
       圆角会变成锯齿台阶（用户原话："为什么这个地方又会有锯齿阴影"）。
       正解是让窗口**正好等于**圆角面板的大小（见 `fit_popup_to_view`），
       圆角交给抗锯齿的绘制（QSS 的 border-radius），窗口半透明 ⇒ 圆角外天然透明且平滑。

    ⚠️ 属性必须在 `setWindowFlags()` **之后**设：它会重建原生窗口，之前的属性会丢。
    """
    try:
        win = combo.view().window()
        win.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        win.setAttribute(Qt.WA_TranslucentBackground, True)
        win.setAttribute(Qt.WA_NoSystemBackground, True)
        # 只清容器自己的底，别碰里面的 view（圆角面板是它画的）
        win.setStyleSheet("QComboBoxPrivateContainer{background:transparent;border:none;}")
        fit_popup_to_view(combo)
    except Exception:
        log.debug("下拉弹窗圆角设置失败", exc_info=True)
    return combo


def fit_popup_to_view(combo, margin: int = 6) -> None:
    """保证下拉弹窗的**可见部分**就是那张圆角卡片，多出来的区域必须是**全透明**。

    背景：Qt 的 QComboBox 弹窗容器会比列表视图大一圈（预留滚动条通道），
    实测 330×507 vs 220×338。那一圈**只要不画东西就是透明的**（不会露方角）；
    一旦那里被画上底色（半透明卡片色），就会看到"方角 + 灰边"。

    所以这里只做两件事，**不去跟 Qt 的尺寸策略较劲**：
      1. 容器底色保持**全透明**（圆角卡片由列表视图的 QSS 画，抗锯齿）；
      2. 容器布局内边距归零，视图尽量铺满。

    ⚠️ **不要用 `setMask` 做圆角**：1 位掩码没有抗锯齿，圆角会变成锯齿台阶 +
       一圈灰边（用户原话："为什么这个地方又会有锯齿阴影"）。
    ⚠️ 也不要 `setFixedSize` 硬改尺寸：Qt 会让弹窗宽度跟输入框对齐，
       硬改会打架且不生效（实测窗口仍是 402×306）。尺寸不完美是**留白**，
       而方角/锯齿是**缺陷** —— 优先保证后者。
    """
    try:
        win = combo.view().window()
        view = combo.view()
        win.setStyleSheet("QComboBoxPrivateContainer{background:transparent;border:none;}")
        lay = win.layout()
        if lay is not None:
            lay.setContentsMargins(0, 0, 0, 0)
        view.setStyleSheet(
            "QListView{background:%s;border:1px solid %s;border-radius:10px;padding:6px;}"
            % (P.get("popup_bg", "#FFFFFF"), css(P.get("hairline_strong", "#00000022"))))
    except Exception:
        log.debug("弹窗透明化失败", exc_info=True)


def _key_of(value: str, fallback: str) -> str:
    """反查一个颜色值当前对应调色板里的哪个键。

    `fill=P["glass_capsule"]` 传进来的是**值**，这里反查出**键名**，
    换肤时才能按新调色板重新解析。查不到就退回默认键。

    ⚠️⚠️ **必须先看 fallback 本人**：本项目多个键在深色主题下是同一个值
    （qq-dark：`hairline` == `hairline_strong` == `glass_hover` == #FFFFFF14）。
    只按"第一个同值的键"反查会把胶囊描边解析成 `glass_hover`，换到液态玻璃皮肤后
    描边就变成 rgba(0,0,0,0.06) —— 实测"1px 高光边"的 alpha 只有 0.069（规范要 0.42）。
    调用方传的就是它取的那个键，**它自己匹配时永远优先**。
    """
    try:
        if str(P.get(fallback, "")).lower() == str(value).lower():
            return fallback
    except Exception:
        pass
    try:
        for k, v in P.items():
            if isinstance(v, str) and v.lower() == str(value).lower():
                return k
    except Exception:
        pass
    return fallback


def themed(widget, fn) -> None:
    """登记"随主题变化的样式"，换肤时由 `notify_theme_changed()` 统一重放。

    ⚠️ 存在的理由：本项目原来到处写 `setStyleSheet(f"color:{P['x']}")`，
       值是**构造那一刻**的快照 —— 于是每换一次主题都能找出几个没跟上的地方
       （用户已经报过多次："设置图标变淡""空状态文字变浅"）。
       能静态化的已改成语义 `role`（全局 QSS 管，必然跟上）；
       真正动态的（取决于运行时状态、不是纯主题色）走这里登记，
       换肤时**一个入口**全部重放，不需要每个面板自己记得刷新。
    """
    try:
        _THEMED.append((weakref.ref(widget), fn))
    except TypeError:
        pass


def notify_theme_changed(app=None) -> int:
    """换肤广播：**一个入口**把所有"按主题取过色"的东西重刷一遍。

    步骤（缺一不可）：
      1. 重放登记过的动态样式（`themed()`）；
      2. `refresh_glass_theme()`：自绘控件（玻璃面板/图标按钮/OSD）按语义键重解析；
      3. 遍历控件树，调用实现了 `on_theme_changed()` 的控件（面板级重建，例如图标重渲染）；
      4. 弹出菜单重刷（它们不在窗口树里，是独立的顶层窗口）。

    返回重放的项数（诊断用）。
    """
    n = 0
    # 0) 图标缓存：key 含颜色，先清掉，后面重建的地方才会真的重渲染
    try:
        icons.clear_caches()
    except Exception:
        log.debug("清图标缓存失败", exc_info=True)
    # 1) 动态样式
    alive = []
    for ref, fn in _THEMED:
        w = ref()
        if w is None:
            continue
        try:
            _ = w.width()
        except RuntimeError:
            continue
        alive.append((ref, fn))
        try:
            fn(w)
            n += 1
        except Exception:
            log.debug("重放主题样式失败", exc_info=True)
    _THEMED[:] = alive
    # 2) 自绘控件
    n += refresh_glass_theme()
    # 3) 面板级钩子
    roots = []
    if app is not None:
        try:
            roots = list(app.topLevelWidgets())
        except Exception:
            roots = []
    seen = set()
    stack = list(roots)
    while stack:
        w = stack.pop()
        if id(w) in seen:
            continue
        seen.add(id(w))
        try:
            stack.extend(w.children())
        except Exception:
            pass
        hook = getattr(w, "on_theme_changed", None)
        if callable(hook):
            try:
                hook()
                n += 1
            except Exception:
                log.debug("on_theme_changed 失败: %r", type(w).__name__, exc_info=True)
    # 4) 菜单（独立顶层窗口，可能不在 topLevelWidgets 里）
    for w in list(_MENUS_LIVE):
        m = w() if isinstance(w, weakref.ref) else w
        if m is None:
            continue
        try:
            m.apply_menu_theme()
            m.refresh_rows()
            n += 1
        except Exception:
            pass
    return n


# ======================================================================================
# ★★ task-78：玻璃模式的两条应用级钩子（全局文字衬影 / 按钮文字防截断）
# ======================================================================================
_GLASS_HOOKS: dict = {"ready": False, "style": None, "btn_filter": None}


# ★★ task-81（用户问题 8）：**删除全局文字衬影** —— 玻璃皮肤下所有 QStyle 文字
#    原来会多画一层 1px rgba(0,0,0,0.35) 的偏移影，小字号下笔画互相粘连、发糊。
#    卡片要求"普通 UI 控件只绘一层干净矢量文本"（描边/投影只允许歌词）。实测双层
#    文字在 150% 缩放下肉眼可见变粗，已整体移除；非玻璃主题本来就不画。


class _RoleButtonAutosize(QObject):
    """★ task-78②：玻璃按钮**文字绝不截断** —— 按字体实测宽度解掉过窄的 setFixedWidth。

    A 链设置面板把「确定 / 应用 / 取消」写成 setFixedWidth(92)；「✓ 已是最新」在 92px
    里必然被裁成两半（用户截图）。本过滤器在按钮 Show / Polish / 文案变化时：
      · 只有**真的会截断**（文字宽度 > 可用宽度）才把 fixedWidth 解除；
      · 解除后 minimumWidth = fontMetrics.horizontalAdvance(text) + 36
        （= 左右各 18px，与 QSS padding: 0 18px 一致）。
    ⇒ 文案完整居中、左右余量 ≥18px；未截断的按钮（其它主题）**一律不动**。
    """

    PAD = 36                      # 左右各 18px
    ROLES = ("primary", "ghost")

    def eventFilter(self, obj, ev):  # noqa: N802
        try:
            if not isinstance(obj, QAbstractButton):
                return False
            t = ev.type()
            if t not in (QEvent.Show, QEvent.Polish, QEvent.StyleChange,
                         QEvent.FontChange, QEvent.DynamicPropertyChange):
                return False
            if obj.property("role") not in self.ROLES:
                return False
            self.fit(obj)
        except Exception:
            log.debug("按钮防截断失败", exc_info=True)
        return False

    def fit(self, b) -> None:
        if getattr(b, "_zhj_btn_fit", False):
            return
        try:
            b._zhj_btn_fit = True
            text = str(b.text() or "")
            adv = b.fontMetrics().horizontalAdvance(text)
            need = adv + self.PAD
            if b.maximumWidth() < 16777215:            # 仍被 setFixedWidth 钉死
                if adv > b.width() - self.PAD:         # 只有真的会截断才解锁
                    b.setMinimumWidth(need)
                    b.setMaximumWidth(16777215)
            elif b.minimumWidth() != need:             # 已解锁：文案变化跟着长/短
                b.setMinimumWidth(need)
        except Exception:
            log.debug("按钮宽度计算失败", exc_info=True)
        finally:
            try:
                b._zhj_btn_fit = False
            except Exception:
                pass


def ensure_glass_hooks() -> dict:
    """幂等安装两条应用级钩子（由 refresh_glass_theme() 在每次换肤/启动时调用）。"""
    if _GLASS_HOOKS["ready"]:
        return dict(_GLASS_HOOKS)
    app = QApplication.instance()
    if app is None:
        return dict(_GLASS_HOOKS)
    # 1) ★ task-81：全局 UI 字体锁定 + 抗锯齿/渲染质量。
    #    字体族由 theme.ui_font_stack() 统一（Segoe UI → Microsoft YaHei UI）；
    #    PreferAntialias | PreferQuality 让小字号笔画走高质量 AA，不靠描边加粗。
    try:
        from PySide6.QtGui import QFont as _QFont

        from . import theme as _theme_f

        f = app.font()
        fams = list(_theme_f.ui_font_stack() or [])
        if fams:
            f.setFamilies(fams)
        if f.pixelSize() <= 0:
            f.setPixelSize(13)
        f.setStyleStrategy(_QFont.PreferAntialias | _QFont.PreferQuality)
        app.setFont(f)
        _GLASS_HOOKS["style"] = None
        log.info("UI 字体锁定=%s（PreferAntialias|PreferQuality）", fams[:2])
    except Exception:
        log.debug("锁定 UI 字体失败", exc_info=True)
    # 2) 按钮防截断过滤器（应用级事件过滤）
    try:
        f = _RoleButtonAutosize(app)
        app.installEventFilter(f)
        _GLASS_HOOKS["btn_filter"] = f
        for w in app.allWidgets():
            try:
                if isinstance(w, QAbstractButton) and w.property("role") in f.ROLES:
                    f.fit(w)
            except RuntimeError:
                continue
        log.info("按钮防截断过滤器已安装")
    except Exception:
        log.debug("安装按钮防截断过滤器失败", exc_info=True)
    _GLASS_HOOKS["ready"] = True
    return dict(_GLASS_HOOKS)


def refresh_glass_theme() -> int:
    """换肤后让所有 GlassFrame 重新按调色板取色。

    返回刷新的控件数（诊断用）。
    """
    # ★★ task-78：先确保两条应用级钩子已装（玻璃文字衬影 / 按钮防截断）
    ensure_glass_hooks()
    n = 0
    alive = []
    for ref in _GLASS_LIVE:
        w = ref()                             # 控件已被 GC 回收 -> None，直接丢掉
        if w is None:
            continue
        try:
            _ = w.width()                     # 访问已删除的 C++ 对象会抛异常
        except RuntimeError:
            continue
        alive.append(ref)
        try:
            if getattr(w, "_is_time_label", False):
                # 时间戳：直接用一级文字色，不走任何间接映射
                w.setStyleSheet(f"color:{P['text']};")
                n += 1
                continue
            ck = getattr(w, "_color_key", None)
            if ck is not None:                     # IconButton / TextIconButton
                # ⚠️ 不按 `_color_key` 反查，直接给**语义化图标色**：
                #    反查依赖"构造时那个颜色值在当前调色板里能对上号"，
                #    深色主题构造的控件在浅色主题里就查不到（实测图标停在旧色 #A8B0BF）。
                # 图标用 text_dim：用户反馈加了独立的 icon token 之后"图标怎么那么浅"，
                # 回到上一版观感 —— text_dim 已经按对比度规范提亮过了。
                # 尊重构造时的语义选择：传了 `color=P["text"]` 的就维持一级色，
                # 不能一律改成 text_dim —— 否则空状态那些按钮永远提不亮
                # （用户反复反馈："打开文件夹""最近打开的文件"文字又变浅了）。
                _k = ck if ck in P else "text_dim"
                w.set_color(P.get(_k) or P["text"])
                # ★ task-70：hover 必须是**语义化的强调色**（不能一律 text）。
                #    换肤后旧的写死 text 会把 hover 变回白/黑，用户看到"hover 不变蓝"。
                _hk = getattr(w, "_hover_key", None)
                if _hk not in P:
                    _hk = "accent"
                w.set_hover_color(P.get(_hk) or P.get("accent") or P["text"])
                n += 1
                continue
            fk, bk = w._fill_key, w._border_key     # GlassFrame
            if fk in P:
                w._fill = P[fk]
            if bk in P:
                w._border = P[bk]
            w.update()
            n += 1
        except Exception:
            pass
    _GLASS_LIVE[:] = alive
    return n


# ======================================================================================
# ★★ task-84：实时局部背景高斯模糊（Live Regional Backdrop Blur）
# ======================================================================================
# 用户原话："你们为了凑对比度把 glass-light 改成了脏黑底色，完全变成第二个深色模式。"
# 本节把玻璃皮肤从"半透深色板"改回**真·清透琉璃**：
#   · 每 33ms 从舞台画面取一帧（视频走 QOpenGLWidget.grabFramebuffer，音乐/图片走各自视口），
#     降采样 1/6 后做高速高斯近似；
#   · 浮层绘制时**只裁剪自己矩形正下方的那块模糊纹理**放大铺底（不做全屏贴死图）；
#   · 模糊纹理之上再叠【清澈冰晶白】亮白琉璃 + 顶部对角光泽（见 theme.py glass-light）。
class GlassBlurPlate(QObject):
    """舞台画面的**实时模糊纹理**（降采样 1/6，33ms 刷新）。"""

    changed = Signal()

    INTERVAL_MS = 33          # 视频播放时每 33ms 刷新一次
    DOWNSCALE = 6             # 降采样 1/6
    ALLOWED = ("ControlBar", "SettingsPanel", "SubtitlePanel", "OsdBadge",
               "PlaylistDrawer", "ImageViewerWidget")

    _instance = None

    @classmethod
    def instance(cls) -> "GlassBlurPlate":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._consumers: list = []
        self._plate: QImage | None = None
        self._stage_size = (0, 0)
        self._blur_ms = 0.0
        self._capture_ms = 0.0
        self._ticks = 0
        self._source = "none"

    # -- 注册（浮层 show/hide 时调用）----------------------------------------
    def register(self, w) -> None:
        try:
            self._prune()
            for _r in self._consumers:
                if _r() is w:
                    return                     # 幂等：重复注册不叠加
            self._consumers.append(weakref.ref(w))
        except Exception:
            return
        self._prune()
        if self._consumers and not self._timer.isActive():
            self._timer.start()

    def unregister(self, w) -> None:
        self._consumers = [r for r in self._consumers if r() is not w]
        self._prune()
        if not self._consumers:
            self._timer.stop()

    def _prune(self) -> None:
        self._consumers = [r for r in self._consumers if r() is not None]

    def stats(self) -> dict:
        return {"ticks": self._ticks, "plate": None if self._plate is None else
                [self._plate.width(), self._plate.height()],
                "blur_ms": round(self._blur_ms, 3), "capture_ms": round(self._capture_ms, 3),
                "source": self._source, "consumers": len(self._consumers),
                "timer_active": self._timer.isActive()}

    # -- 取源 ----------------------------------------------------------------
    @staticmethod
    def _main_window():
        app = QApplication.instance()
        if app is None:
            return None
        for w in app.topLevelWidgets():
            if type(w).__name__ == "MainWindow" and getattr(w, "stage", None) is not None:
                return w
        return None

    def _grab_stage(self, stage) -> QImage | None:
        """取舞台一帧并降采样到 1/6。只取舞台，不抓全屏。"""
        tw = max(1, int(round(stage.width() / float(self.DOWNSCALE))))
        th = max(1, int(round(stage.height() / float(self.DOWNSCALE))))
        # 1) 视频：QOpenGLWidget 只能靠 grabFramebuffer 拿到 GL 画面
        video = getattr(stage, "video", None)
        try:
            if video is not None and video.isVisible():
                img = video.grabFramebuffer()
                if not img.isNull():
                    self._source = "video-gl"
                    return img.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        except Exception:
            log.debug("抓取视频帧失败", exc_info=True)
        # 2) 图片模式
        iv = getattr(stage, "image_view", None)
        try:
            if iv is not None and iv.isVisible():
                img = iv.grabFramebuffer()
                if not img.isNull():
                    self._source = "image-gl"
                    return img.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        except Exception:
            log.debug("抓取图片视口失败", exc_info=True)
        # 3) 音乐模式：audio_view 是 CPU 自绘，QWidget.grab 即可
        av = getattr(stage, "audio_view", None)
        try:
            if av is not None and av.isVisible():
                pm = av.grab()
                if not pm.isNull():
                    self._source = "audio-cache"
                    return pm.toImage().scaled(tw, th, Qt.IgnoreAspectRatio,
                                               Qt.SmoothTransformation)
        except Exception:
            log.debug("抓取音乐背景失败", exc_info=True)
        return None

    def _blur(self, img: QImage) -> QImage:
        """高速高斯近似：连续「下采样 1/2 → 平滑上采样」等价于大核高斯（Qt C++ 实现）。

        ⚠️ 不用 QGraphicsBlurEffect / QGraphicsScene：那两个要建离屏场景，
           240×137 的处理也要 ~1ms 且每帧分配；这里 4 次缩放实测 <0.4ms。
        """
        w, h = img.width(), img.height()
        cur = img
        for _ in range(2):
            cur = cur.scaled(max(1, w // 2), max(1, h // 2),
                             Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            cur = cur.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        cur = cur.scaled(max(1, w // 4), max(1, h // 4),
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return cur.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    def _tick(self) -> None:
        self._prune()
        if not self._consumers:
            self._timer.stop()
            return
        win = self._main_window()
        stage = getattr(win, "stage", None) if win is not None else None
        if stage is None or not stage.isVisible():
            return
        # 只有真有可见浮层时才花这笔钱
        visible = False
        for r in self._consumers:
            w = r()
            try:
                if w is not None and w.isVisible():
                    visible = True
                    break
            except RuntimeError:
                continue
        if not visible:
            return
        import time as _time
        t0 = _time.perf_counter()
        small = self._grab_stage(stage)
        self._capture_ms = (_time.perf_counter() - t0) * 1000.0
        if small is None:
            return
        t1 = _time.perf_counter()
        plate = self._blur(small)
        self._blur_ms = (_time.perf_counter() - t1) * 1000.0
        self._plate = plate
        self._stage_size = (stage.width(), stage.height())
        self._ticks += 1
        self.changed.emit()

    # -- 绘制 ----------------------------------------------------------------
    def paint_backdrop(self, painter: QPainter, widget, target: QRectF) -> bool:
        """把**该控件正下方**的模糊纹理裁剪后画到 target（target 为该控件局部坐标）。"""
        plate = self._plate
        if plate is None or target.width() <= 0 or target.height() <= 0:
            return False
        win = self._main_window()
        stage = getattr(win, "stage", None) if win is not None else None
        if stage is None:
            return False
        try:
            sg = stage.mapToGlobal(QPoint(0, 0))
            origin = widget
            if type(widget).__name__ in ("RoundedMenu", "RoundedTip", "PreviewPopup"):
                origin = widget
            wg = widget.mapToGlobal(QPoint(0, 0))
            sx = (wg.x() - sg.x()) + target.left()
            sy = (wg.y() - sg.y()) + target.top()
        except Exception:
            return False
        k = float(self.DOWNSCALE)
        src = QRectF(sx / k, sy / k, target.width() / k, target.height() / k)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(target, plate, src)
        return True


def _glass_bg(w: "GlassFrame") -> QPixmap:
    # ★ task-84：key 里必须带上"是否垫模糊 + 当前主题"，否则换肤/开模糊后仍命中旧缓存
    try:
        from . import theme as _t_key
        _theme_key = _t_key.P.get("_theme")
    except Exception:
        _theme_key = None
    key = (w.width(), w.height(), w._radius, w._fill, w._border,
           round(float(w._shadow), 3), w._spread, w._layers,
           bool(w._inner_glow), round(w.devicePixelRatioF() or 1.0, 2),
           bool(getattr(w, "_blur_active", False)), _theme_key)
    hit = _GLASS_BG.get(key)
    if hit is not None:
        return hit
    pm = w._render_background()
    nbytes = max(1, pm.width() * pm.height() * 4)
    if nbytes <= _GLASS_BG_ONE_MAX:
        _GLASS_BG[key] = pm
        _GLASS_BG_ORDER.append(key)
        while _GLASS_BG_ORDER:
            total = sum(max(1, _GLASS_BG[k].width() * _GLASS_BG[k].height() * 4)
                        for k in _GLASS_BG_ORDER)
            if total <= _GLASS_BG_BYTES_MAX:
                break
            _GLASS_BG.pop(_GLASS_BG_ORDER.pop(0), None)
    return pm


def glass_cache_stats() -> dict:
    total = sum(max(1, p.width() * p.height() * 4) for p in _GLASS_BG.values())
    return {"entries": len(_GLASS_BG), "bytes": total}


class GlassFrame(QWidget):
    """带「柔和弥散阴影 + 通透底色 + 0.5px 内发光折射高光边框」的圆角面板。

    阴影由本控件自己在 paintEvent 里画（不用 QGraphicsDropShadowEffect）——
    后者在半透明窗口 + OpenGL 子控件混合时会出现残影与掉帧。
    """

    def __init__(
        self,
        parent=None,
        radius: int = M["capsule_radius"],
        fill: str | None = None,
        border: str | None = None,
        shadow: float = 0.55,
        shadow_layers: int = 16,
        shadow_spread: int = 22,
        inner_glow: bool = True,
        pad: tuple[int, int, int, int] = (0, 0, 0, 0)
    ):
        # ⚠️⚠️ **不要**把默认值写成 `fill=P["glass_capsule"]`：
        #    默认参数在**模块导入时**求值一次，那时调色板还是深色基准，
        #    于是"浅色模式启动"的控件永远拿着深色快照（实测：图标 #C8D0DC vs 应有的 #4E5969，
        #    用户反馈"默认浅色启动时按钮图标和文字特别浅"）。必须在**调用时**解析。
        fill = P["glass_capsule"] if fill is None else fill
        border = P["hairline"] if border is None else border
        super().__init__(parent)
        self._radius = radius
        self._fill = fill
        self._border = border
        self._shadow = shadow
        self._layers = shadow_layers
        self._spread = shadow_spread
        self._inner_glow = inner_glow
        self._margin = shadow_spread
        # ⚠️⚠️ 记下"这个填充色是调色板里的哪个键"。
        #    `fill=P["glass_capsule"]` 是**构造时求值**的字符串快照 ——
        #    换肤时改的是 `P[...]`，控件手里的还是旧颜色字符串，
        #    于是浅色主题下标题栏/抽屉都变白了、**只有胶囊还是深色**（用户截图指出）。
        #    记住键名，换肤时就能重新解析。
        self._fill_key = _key_of(fill, "glass_capsule")
        self._border_key = _key_of(border, "hairline")
        import weakref

        _GLASS_LIVE.append(weakref.ref(self))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # ★ task-84：浮层底板是否垫"实时局部模糊"（ControlBar/SettingsPanel/OSD 等）。
        #    AudioCard / ImageInfoBar 自己就有整块背景，不重复采样。
        self._blur_enabled = type(self).__name__ in GlassBlurPlate.ALLOWED
        self._blur_active = False
        self._blur_sig = None
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(
            self._margin + pad[0], self._margin + pad[1], self._margin + pad[2], self._margin + pad[3]
        )
        self._lay.setSpacing(0)

    def body_layout(self) -> QVBoxLayout:
        return self._lay

    # ⚠️ setFixedWidth/Height 定的是**含阴影外边距**的整控件尺寸,
    #    而用户看到的"面板"是里面的 body。要按视觉尺寸排版就必须走下面两个方法 ——
    #    本项目踩过：音频卡片 setFixedWidth(380) 后 body 只有 320, 封面(320)直接溢出卡片。
    def body_width(self) -> int:
        return max(1, self.width() - 2 * self._margin)

    def body_height(self) -> int:
        return max(1, self.height() - 2 * self._margin)

    def set_body_width(self, w: int):
        self.setFixedWidth(int(w) + 2 * self._margin)

    def set_body_size(self, w: int, h: int):
        self.setFixedSize(int(w) + 2 * self._margin, int(h) + 2 * self._margin)

    def set_body_min_width(self, w: int):
        self.setMinimumWidth(int(w) + 2 * self._margin)

    def set_fill(self, fill: str):
        self._fill = fill
        self.update()

    def _shadow_pm(self) -> QPixmap:
        """按当前尺寸/参数生成（并缓存）**单层**软阴影。"""
        key = (self.width(), self.height(), round(self._radius, 1),
               int(self._spread), round(float(self._shadow), 3),
               round(self.devicePixelRatioF() or 1.0, 2))
        hit = _SHADOW_CACHE.get(key)
        if hit is not None:
            return hit
        pm = soft_shadow_pixmap(
            max(1, self.width() - self._spread * 2),
            max(1, self.height() - self._spread * 2),
            self._radius, int(self._spread),
            int(M.get("float_shadow_dy", 7)), float(self._shadow))
        _SHADOW_CACHE[key] = pm
        _SHADOW_ORDER.append(key)
        while len(_SHADOW_ORDER) > 6:
            _SHADOW_CACHE.pop(_SHADOW_ORDER.pop(0), None)
        return pm

    def body_rect(self) -> QRectF:
        m = self._margin
        return QRectF(m, m, max(1, self.width() - 2 * m), max(1, self.height() - 2 * m))

    def _hairline_rect(self) -> QRectF:
        """**按物理像素对齐**的 1px 描边矩形（模块三）。

        ⚠️ 原来用 `r.adjusted(0.5, 0.5, -0.5, -0.5)`：0.5 逻辑像素在 150% 缩放下
           落在**非整数物理像素**上 ⇒ 被反锯齿摊到两个像素 ⇒ 描边看起来
           「发虚、发灰」（用户反馈与逆向报告 §3.5.3 完全一致）。
           现在按 `devicePixelRatioF()` 换算成整数物理像素再折回逻辑坐标。
        """
        r = self.body_rect()
        dpr = self.devicePixelRatioF() or 1.0
        half = 0.5 / dpr                      # 半个物理像素对应的逻辑长度
        return QRectF(r.left() + half, r.top() + half,
                      max(1.0, r.width() - 2 * half), max(1.0, r.height() - 2 * half))

    def _blur_on(self) -> bool:
        """本浮层是否垫实时模糊底衬（玻璃皮肤 = 必须；其它主题 = 同垫薄薄一层）。"""
        if not getattr(self, "_blur_enabled", False):
            return False
        plate = GlassBlurPlate.instance()
        return plate._plate is not None

    def paintEvent(self, _ev):
        # ⚠️ 背景（阴影 + 底色 + 内发光 + 描边）整体**缓存成一张 pixmap**。
        #    16 层弥散阴影每帧要画 16 个抗锯齿圆角矩形，实测胶囊一次重绘 3.08ms ——
        #    窗口拖动/缩放时整窗高频重绘，这 3ms 就是掉帧的直接来源。
        #    缓存的 key 里含尺寸与全部取色参数，换肤/改尺寸会自动失效。
        # ★★ task-84：在缓存壳之下先铺**本控件正下方的实时模糊纹理**（每 33ms 刷新），
        #    壳里的白色琉璃层（玻璃皮肤）/ 微透底（深/浅主题）叠在它之上。
        self._blur_active = bool(self._blur_on())
        if getattr(self, "_blur_enabled", False) and not getattr(self, "_blur_registered", False):
            # 子类（SettingsPanel 等）若自己重写了 showEvent 没调 super()，这里补注册一次
            try:
                GlassBlurPlate.instance().register(self)
                self._blur_registered = True
            except Exception:
                pass
        pm = _glass_bg(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._blur_active:
            body = self.body_rect()
            path = QPainterPath()
            path.addRoundedRect(body, self._radius, self._radius)
            p.save()
            p.setClipPath(path)
            GlassBlurPlate.instance().paint_backdrop(p, self, body)
            p.restore()
        p.drawPixmap(0, 0, pm)
        p.end()
        self._paint_content()

    # -- 模糊纹理的注册 / 注销 ------------------------------------------------
    def showEvent(self, ev):  # noqa: N802
        super().showEvent(ev)
        if getattr(self, "_blur_enabled", False):
            plate = GlassBlurPlate.instance()
            plate.register(self)
            try:
                plate.changed.connect(self._on_blur_tick, Qt.UniqueConnection)
            except Exception:
                pass

    def hideEvent(self, ev):  # noqa: N802
        super().hideEvent(ev)
        if getattr(self, "_blur_enabled", False):
            try:
                GlassBlurPlate.instance().unregister(self)
            except Exception:
                pass

    def _on_blur_tick(self):
        # 只在真正可见时重绘；模糊纹理每帧变化 ⇒ 面板内的"动态毛玻璃"跟随
        if self.isVisible() and getattr(self, "_blur_enabled", False):
            self.update()

    def _render_background(self) -> QPixmap:
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.body_rect()

        # 1) 阴影 —— **安全降级：单层、极淡、小偏移，不做任何位图模糊**
        #
        # ⚠️⚠️ 上一版用 QGraphicsBlurEffect 去模糊"一整块实心黑圆角矩形"，
        #    结果内部的高浓度黑全部扩散到外圈，出来是一团**烟熏/烧焦**的黑影
        #    （用户截图："像被大火烧焦一样的黑色浓烟"）。
        #    位图模糊只有在"被模糊的形状本身 alpha 就很低"时才是软阴影；
        #    拿实心黑去模糊必然翻车。
        #
        #    浅色模式下真正干净的做法是"极细描边 + 超轻阴影"：
        #    alpha 0.02~0.03、偏移 3px、**不允许大面积外扩**。
        # ⚠️ 模块二（用户指令）：**视频层上方的浮层一律不画投影**。
        #    投影（哪怕是单层）在整窗高频重绘时仍是纯粹的 Overdraw，
        #    而 QQ NT 的做法是「1px 细描边 + 半透明微底色」——形体靠描边而非阴影。
        #    需要阴影的场景（弹窗）由 Surface 自己按需打开 `_GLASS_SHADOW_ENABLED`。
        if self._shadow > 0 and _GLASS_SHADOW_ENABLED:
            dy = int(M.get("float_shadow_dy", 3))
            grow = 1.5
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, max(1, int(round(255 * self._shadow)))))
            rr = r.adjusted(-grow, -grow + dy, grow, grow + dy)
            p.drawRoundedRect(rr, self._radius + grow, self._radius + grow)
            p.setPen(Qt.NoPen)

        # 2) 通透底色
        from . import theme as _theme_fill
        if _theme_fill.is_glass_skin():
            # ★★ task-84②：**清澈冰晶白** —— 模糊纹理上叠 rgba(255,255,255,0.52)
            #    （调色板 glass_panel 就是这个值），再补顶部对角微弱光泽。
            p.setBrush(qt_color(self._fill))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, self._radius, self._radius)
            gloss = QLinearGradient(r.topLeft(), r.bottomRight())
            gloss.setColorAt(0.0, QColor(255, 255, 255, 92))
            gloss.setColorAt(0.42, QColor(255, 255, 255, 18))
            gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setBrush(gloss)
            p.drawRoundedRect(r, self._radius, self._radius)
        else:
            fill_c = qt_color(self._fill)
            if getattr(self, "_blur_enabled", False):
                # ★ task-84④ / QA-2 必修 2：深/浅主题的悬浮窗不透明度必须落在
                #    **0.90~0.92** 区间 —— 过高（0xFA≈0.98）看不见模糊底衬，
                #    过低（qq-light 原 0xDB≈0.86）会锐利视频直接穿透。
                _lo, _hi = int(round(0.90 * 255)), int(round(0.92 * 255))
                if fill_c.alpha() > _hi:
                    fill_c.setAlpha(_hi)
                elif fill_c.alpha() < _lo:
                    fill_c.setAlpha(_lo)
            p.setBrush(fill_c)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, self._radius, self._radius)

        # 3) 顶部内发光（模拟玻璃折射的高光）
        if self._inner_glow:
            glow = QLinearGradient(r.topLeft(), r.bottomLeft())
            # ⚠️ 光泽色**必须走调色板**：深色主题下 26/255 的白刚好是"玻璃反光"，
            #    但浅色主题下白色玻璃 + 26/255 的白 = 完全看不见，
            #    液态玻璃的形体就塌了。浅色预设把 glass_gloss 提到 alpha 217。
            gc = qt_color(P.get("glass_gloss", "#FFFFFF1A"))
            a0 = gc.alpha()
            glow.setColorAt(0.0, QColor(gc.red(), gc.green(), gc.blue(), a0))
            glow.setColorAt(0.42, QColor(gc.red(), gc.green(), gc.blue(), int(a0 * 0.3)))
            glow.setColorAt(1.0, QColor(gc.red(), gc.green(), gc.blue(), 0))
            path = QPainterPath()
            path.addRoundedRect(r, self._radius, self._radius)
            p.setClipPath(path)
            p.setBrush(glow)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r.adjusted(0, 0, 0, -r.height() * 0.35), self._radius, self._radius)
            p.setClipping(False)

        # 3.5) 磨砂颗粒：只在玻璃体内部平铺，让"透"看起来是磨砂而不是单纯变淡
        #     ★★ task-78（用户问题 4/12）：**液态玻璃皮肤 = 零噪点**。
        #     深色磨砂琉璃靠「半透底 + 1px 亮边」成形；再平铺白色颗粒的话，
        #     深底上会看到密密麻麻的白点（用户原话："面板上全是噪点"）。
        #     其它 7 套主题行为**逐位不变**。
        from . import theme as _theme_frost
        if not _theme_frost.is_glass_skin():
            path2 = QPainterPath()
            path2.addRoundedRect(r, self._radius, self._radius)
            p.setClipPath(path2)
            p.drawTiledPixmap(r.toRect(), _frost_tile())
            p.setClipping(False)

        # 4) 0.5px 内发光折射高光边框（外描边 + 内描边各一层）
        p.setBrush(Qt.NoBrush)
        outer = QPen(qt_color(self._border))
        outer.setWidthF(1.0)
        p.setPen(outer)
        # 模块三：描边矩形按**物理像素对齐**（原来 0.5 逻辑像素在 150% 缩放下
        # 落在非整数物理像素 ⇒ 被反锯齿摊开 ⇒ 视觉上「发虚、发灰」）
        p.drawRoundedRect(self._hairline_rect(), self._radius, self._radius)
        inner = QPen(qt_color(P["inner_glow"]))
        inner.setWidthF(1.0)
        p.setPen(inner)
        p.drawRoundedRect(r.adjusted(1.0, 1.0, -1.0, -1.0), max(1.0, self._radius - 1), max(1.0, self._radius - 1))
        p.end()
        return pm

    def _paint_content(self):
        """子类在这里画自己的内容（背景已经由缓存画好了）。"""


# --------------------------------------------------------------------------------------
# 图标按钮
# --------------------------------------------------------------------------------------


class IconButton(QWidget):
    """自绘圆形图标按钮：悬停有柔和底色、可选中、可显示角标。

    自绘（而不是 QToolButton + QSS）是为了让悬停底色是「圆」而不是「方」,
    与浮动胶囊的造型统一。
    """

    clicked = Signal()
    toggled = Signal(bool)
    rightClicked = Signal(QPointF)

    def __init__(
        self,
        name: str,
        parent=None,
        size: int = M["btn_size"],
        icon_size: int = 18,
        tooltip: str = "",
        checkable: bool = False,
        checked: bool = False,
        # QQ NT 图标规范：常态中灰 #5E6166、悬停加深 #1D2129
        # （用户："悬停时图标反而变浅导致隐形"）。
        # ⚠️ 默认值**不能**写 `P["text_dim"]`：默认参数是**导入时**求值的快照，
        #    浅色启动时会拿到深色主题的浅灰图标色（用户："默认浅色启动图标特别浅"）。
        color: str | None = None,
        hover_color: str | None = None,
        accent_when_checked: bool = True,
        radius: int | None = None,
        stroke: float = 1.6,
        # ★ task-92：标题栏按钮必须"中心小圆/胶囊"，绝不铺满 rect()；
        #   且任何 hover 状态变化都通知 owner 整条重绘（原子图层）。
        hover_shape: str = "capsule",
        hover_inset: int = 3,
        hover_radius: float = 11.0
    ):
        # ★ task-80③：底栏/标题栏图标统一走 P["icon"]（不是各自散落的 text_dim 快照），
        #    并且统一画笔线宽 1.6px —— 否则「词」徽章与倍速/全屏/播放列表深浅不一。
        color = P["icon"] if color is None else color
        # ★ task-70（用户 ④）：**常态柔和深灰，只有 Hover / Checked/Active 才用强调色**。
        #    默认 hover 从 text 改成 accent；显式传 hover_color 的（如 close=danger）保持调用方语义。
        hover_color = P["accent"] if hover_color is None else hover_color
        # ★ task-74（问题 4②）：底栏「音乐与歌词设置」入口原为 ♫（music_note），
        #    与纯音乐占位音符撞脸。这里改成**「词」字圆角徽章**（自绘，见 paintEvent）。
        if name == "music_note":
            name = "lyric_badge"
        super().__init__(parent)
        self._name = name
        self._icon_size = icon_size
        self._color = color
        self._hover_color = hover_color
        # ⚠️ `color=P["text"]` 传进来的是**构造时的字符串快照**，换肤时不会跟着变 ——
        #    浅色主题下就是"白字画在白按钮上"（用户截图：'打开文件'几乎看不见、
        #    而没传 color 的'打开文件夹'正常）。记下键名，换肤时重新解析。
        self._color_key = _key_of(color, "text_dim")
        self._hover_key = _key_of(hover_color, "text")
        _GLASS_LIVE.append(__import__("weakref").ref(self))
        self._checkable = checkable
        self._checked = checked
        self._accent_checked = accent_when_checked
        self._stroke = stroke
        self._hover = False
        self._pressed = False
        self._hover_t = 0.0
        self._hover_shape = hover_shape
        self._hover_inset = int(hover_inset)
        self._hover_radius = float(hover_radius)
        self._repaint_owner = None          # ★ task-92：原子图层 owner（整条标题栏）
        self._radius = radius if radius is not None else size // 2
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        # ★★ task-80①：换肤（setStyleSheet/setPalette）时 Qt 绝不允许刷出原生矩形底板。
        #    本控件 100% 自绘（paintEvent 里**没有任何 super().paintEvent()**），
        #    显式两属性兜底：半透明背景 + 不自动填充。
        #    ⚠️ 绝不能加 WA_NoSystemBackground —— 它会让 Qt 不清理上一帧，
        #    「菜单已关闭但左上角蓝底残留」就是这么来的（实测复现）。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        if tooltip:
            self.setToolTip(tooltip)
        # ⚠️ 属性驱动的值必须在动画对象之前存在（QPropertyAnimation 构造时就会读一次）
        self._anim = QPropertyAnimation(self, b"hoverT", self)
        self._anim.setDuration(130)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    # -- 属性 ----------------------------------------------------------------
    def get_hover_t(self) -> float:
        return self._hover_t

    def set_hover_t(self, v: float):
        self._hover_t = float(v)
        self.update()
        owner = getattr(self, "_repaint_owner", None)
        if owner is not None:
            try:
                owner.update()          # ★ task-92：动画每帧也让整条标题栏重绘
            except Exception:
                pass

    hoverT = Property(float, get_hover_t, set_hover_t)

    def _notify_owner(self):
        """★ task-92：把 hover/press 状态变化通知给"原子图层"owner（整条标题栏）。"""
        owner = getattr(self, "_repaint_owner", None)
        if owner is not None:
            try:
                owner.update()
            except Exception:
                pass

    def set_icon(self, name: str):
        if name != self._name:
            self._name = name
            self.update()

    def set_color(self, color: str):
        self._color = color
        # ★ task-80③：同步刷新"颜色键"，否则换肤后按旧键回填（又回到 text_dim 深浅不一）
        self._color_key = _key_of(color, self._color_key)
        self.update()

    def set_hover_color(self, color: str):
        self._hover_color = color
        self._hover_key = _key_of(color, self._hover_key)
        self.update()

    def set_stroke(self, stroke: float):
        """★ task-80③：统一线宽（底栏整排图标 1.6px）。"""
        self._stroke = float(stroke)
        self.update()

    def icon_name(self) -> str:
        return self._name

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, value: bool, emit: bool = False):
        value = bool(value)
        if value == self._checked:
            return
        self._checked = value
        self.update()
        self._notify_owner()
        if emit:
            self.toggled.emit(value)

    def _animate_to(self, target: float):
        self._anim.stop()
        self._anim.setStartValue(self._hover_t)
        self._anim.setEndValue(target)
        self._anim.start()

    # -- 事件 ----------------------------------------------------------------
    def enterEvent(self, ev):
        self._hover = True
        self._animate_to(1.0)
        self._notify_owner()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self._pressed = False
        self._notify_owner()
        self._animate_to(0.0)
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._pressed = True
            self.update()
        elif ev.button() == Qt.RightButton:
            self.rightClicked.emit(ev.position())
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            self._notify_owner()
            if self.rect().contains(ev.position().toPoint()):
                if self._checkable:
                    self._checked = not self._checked
                    self.toggled.emit(self._checked)
                self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        from . import theme as _theme_ib
        _glass_ib = bool(_theme_ib.is_glass_skin())
        try:
            _light_ib = bool(_theme_ib.is_light())
        except Exception:
            _light_ib = False
        t = self._hover_t
        active = self._checked
        shape = getattr(self, "_hover_shape", "capsule")
        if shape == "circle11":
            # ★★ task-92：标题栏按钮的高亮 = **中心圆**（圆心严格 = rect 中心，
            #    半径严格 = hover_radius(11) ⇒ 32×32 按钮四周净空 5px），
            #    **绝不填满整个 rect()**（用户三大病灶里的"白色/深灰方块"就是铺满 rect）。
            ctr = QRectF(self.rect()).center()
            rr = float(getattr(self, "_hover_radius", 11.0))
            hot = QRectF(ctr.x() - rr, ctr.y() - rr, rr * 2.0, rr * 2.0)
            if active and self._accent_checked:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(P["accent"]))
                p.drawEllipse(hot)          # 360° 完整蓝圈（不再被圆角/边距削平）
            _a = 0.0
            if self._pressed:
                _a = 0.18 if not _light_ib else 0.14
            elif t > 0.01:
                _a = (0.12 if not _light_ib else 0.08) * min(1.0, t)
            if _a > 0.001:
                p.setPen(Qt.NoPen)
                hc = QColor(255, 255, 255) if not _light_ib else QColor(0, 0, 0)
                hc.setAlphaF(_a)
                p.setBrush(hc)
                p.drawEllipse(hot)
            r = hot
        else:
            inset = int(getattr(self, "_hover_inset", 3))
            r = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
            rad = min(float(self._radius), r.height() / 2.0, r.width() / 2.0)
            if active and self._accent_checked:
                p.setPen(Qt.NoPen)
                fill = qt_color(P["accent_soft"])
                fill.setAlphaF(min(1.0, fill.alphaF() + 0.12))
                p.setBrush(fill)
                p.drawRoundedRect(r, rad, rad)
            # ★ task-85/92：**常态只画图标，底板 100% 透明**；悬停才画内缩小胶囊
            if t > 0.01:
                p.setPen(Qt.NoPen)
                c = QColor(128, 128, 128)
                c.setAlphaF(0.15 * min(1.0, t))
                p.setBrush(c)
                p.drawRoundedRect(r, rad, rad)
            if self._pressed:
                p.setPen(Qt.NoPen)
                c = QColor(128, 128, 128)
                c.setAlphaF(0.24)
                p.setBrush(c)
                p.drawRoundedRect(r, rad, rad)

        if active and self._accent_checked:
            # 激活态是蓝底/蓝圈，图标一律纯白才读得出来
            color = "#FFFFFF"
        elif t > 0.5:
            color = self._hover_color
        else:
            color = self._color
        if self._name == "lyric_badge":
            # ★ task-74：底栏歌词设置入口 = **「词」字圆角徽章**（不是 ♫）
            from PySide6.QtGui import QFont, QPen
            side = max(12.0, self._icon_size * 0.92)
            rr = QRectF((self.width() - side) / 2.0, (self.height() - side) / 2.0,
                        side, side)
            c = qt_color(color)
            # ★ task-86①：小尺寸「词」笔画密度过高显得比相邻图标亮/重 ——
            #    外框线宽 1.2→1.3（更细更均匀），整体绘制**不透明度 ×0.85** 光学修正，
            #    使它与「全屏」「列表」等单笔画图标观感一致。
            p.setOpacity(0.85)
            # ★★ task-95（用户 94）：**「词」按钮常态不得有任何背景填充** ——
            #   原来这里 `setBrush(fill)` + `drawRoundedRect` 画了一个 15% 白底圆角方块，
            #   与「全屏」「列表」的 100% 纯透明底不一致 ✗（用户："词按钮有底、旁边两个没有"）。
            #   现在只画 **1.5px 同色描边框 + 字形**，与相邻图标**同线宽、同 `icon_fg` 色** ✓
            try:
                _fg = qt_color(P["icon_fg"])
            except Exception:
                _fg = QColor(c)
            p.setPen(QPen(_fg, 1.5))
            p.setBrush(Qt.NoBrush)          # ★ 常态零填充（仅 hover 由通用底色提供）
            p.drawRoundedRect(rr, side * 0.28, side * 0.28)
            f = QFont(self.font())
            f.setPixelSize(max(8, int(round(side * 0.74))))
            # ★ task-81：小字号**禁用 Bold(700)**，统一 Medium(500)，避免笔画糊成一坨
            f.setWeight(QFont.Medium)
            p.setFont(f)
            p.setPen(_fg)
            p.drawText(rr, Qt.AlignCenter, "词")
            p.end()
            return
        pm = icons.render_pixmap(self._name, color, self._icon_size, self.devicePixelRatioF() or 2.0, self._stroke)
        x = (self.width() - self._icon_size) / 2
        y = (self.height() - self._icon_size) / 2
        p.drawPixmap(QPointF(x, y), pm)
        p.end()


class TextIconButton(IconButton):
    """带文字的胶囊按钮（如「倍速」）。"""

    def __init__(self, text: str, parent=None, height: int = 26, pad: int = 12,
                 color: str | None = None, font_size: int = 12, checkable: bool = False,
                 checked: bool = False, tooltip: str = "",
                 width_range: tuple | None = None):
        # 同上：默认值在调用时解析，避免"导入时快照"
        color = P["text_dim"] if color is None else color
        self._text = text
        self._pad = pad
        self._width_range = width_range      # ★ task-95：可选宽度夹取（None = 旧行为）
        self._font_size = font_size
        QWidget.__init__(self, parent)
        # ★ task-80①：与 IconButton 同一套兜底（自绘、无原生底板；不用 NoSystemBackground）
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self._name = ""
        self._icon_size = 0
        self._color = color
        self._hover_color = P["text"]
        # 与 IconButton 同理：颜色是构造时快照，换肤必须重新解析，
        # 否则浅色主题下"打开文件"是白字画在白按钮上（用户截图指出）。
        self._color_key = _key_of(color, "text_dim")
        self._hover_key = "text"
        _GLASS_LIVE.append(__import__("weakref").ref(self))
        self._checkable = checkable
        self._checked = checked
        self._accent_checked = True
        self._stroke = 1.7
        self._hover = False
        self._pressed = False
        self._hover_t = 0.0
        self._radius = height // 2
        f = QFont(self.font())
        f.setPixelSize(font_size)
        f.setWeight(QFont.Medium)
        self.setFont(f)
        self._recalc()
        self.setFixedHeight(height)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        if tooltip:
            self.setToolTip(tooltip)
        self._anim = QPropertyAnimation(self, b"hoverT", self)
        self._anim.setDuration(130)

    def _recalc(self):
        fm = QFontMetrics(self.font())
        # ★★ task-95（用户 94）：`advance + pad*2` 只该留给**设置面板三按钮**；
        #   底栏 `1.0x` 必须回到**紧凑 40~44px** ⇒ 这里支持显式夹取宽度区间。
        _w = int(fm.horizontalAdvance(self._text)) + int(self._pad) * 2
        _wr = getattr(self, "_width_range", None)
        if _wr:
            _w = int(max(int(_wr[0]), min(int(_wr[1]), _w)))
        self.setFixedWidth(max(28, int(_w)))

    def set_text(self, text: str):
        if text != self._text:
            self._text = text
            self._recalc()
            self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        if self._checked:
            p.setPen(Qt.NoPen)
            p.setBrush(qt_color(P["accent_soft"]))
            p.drawRoundedRect(r, self._radius, self._radius)
        if self._hover_t > 0.01:
            # ★ task-85：悬停=柔和内缩灰胶囊 rgba(128,128,128,0.15)（原白色叠色在浅底上=白块）
            c = QColor(128, 128, 128)
            c.setAlphaF(0.15 * min(1.0, self._hover_t))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r, self._radius, self._radius)
        pen = QPen(qt_color(P["hairline_strong"] if not self._checked else P["accent_dim"]))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius)
        color = P["accent"] if self._checked else (P["text"] if self._hover_t > 0.5 else self._color)
        p.setPen(qt_color(color))
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignCenter, self._text)
        p.end()


# --------------------------------------------------------------------------------------
# 进度条（时间轴）
# --------------------------------------------------------------------------------------


def fmt_time(seconds: float, with_ms: bool = False) -> str:
    if seconds is None or seconds != seconds or seconds < 0:
        seconds = 0.0
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if with_ms:
        ms = int(round((seconds - s) * 1000))
        base = f"{h:d}:{m:02d}:{sec:02d}.{ms:03d}" if h else f"{m:02d}:{sec:02d}.{ms:03d}"
        return base
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class ProgressSlider(QWidget):
    """时间轴：已播放高光 + 缓冲区间 + 悬停/拖拽手柄 + 悬停时间气泡。"""

    seekStarted = Signal()
    seekPreview = Signal(float)      # 秒
    seekCommitted = Signal(float)    # 秒
    hoverChanged = Signal(float)     # 秒, -1 表示离开

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(M["progress_row_h"])
        self.setCursor(Qt.PointingHandCursor)
        self._duration = 0.0
        self._position = 0.0
        self._cache = 0.0
        self._hover_x = -1.0
        self._hover = False
        self._dragging = False
        self._drag_value = 0.0
        self._chapters: list[tuple[float, str]] = []
        self._hover_t = 0.0
        # ★ task-79：手柄半径双态 + 平滑插值（常态 4.0 / 悬停拖拽 7.0；高度仍由 progress_row_h 控制）
        self._dot_r = float(self.DOT_R_NORMAL)
        self._dot_anim_target = float(self.DOT_R_NORMAL)
        self._dot_anim = QPropertyAnimation(self, b"dotR", self)
        self._dot_anim.setDuration(self.DOT_ANIM_MS)
        self._dot_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim = QPropertyAnimation(self, b"hoverT", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    # -- 属性 ----------------------------------------------------------------
    def get_hover_t(self) -> float:
        return self._hover_t

    def set_hover_t(self, v: float):
        self._hover_t = float(v)
        self.update()

    hoverT = Property(float, get_hover_t, set_hover_t)

    # -- 手柄半径（★ task-79：4.0 ↔ 7.0 平滑插值）-----------------------------
    DOT_R_NORMAL = 4.0
    DOT_R_ACTIVE = 7.0
    DOT_ANIM_MS = 150

    def get_dot_r(self) -> float:
        return self._dot_r

    def set_dot_r(self, v: float):
        self._dot_r = float(v)
        self.update()

    dotR = Property(float, get_dot_r, set_dot_r)

    def _dot_target(self) -> float:
        active = bool(self._dragging or self._hover)
        return float(self.DOT_R_ACTIVE if active else self.DOT_R_NORMAL)

    def _animate_dot(self):
        target = float(self._dot_target())
        if abs(target - self._dot_anim_target) < 0.01 and abs(target - self._dot_r) < 0.01:
            return
        self._dot_anim_target = target
        self._dot_anim.stop()
        self._dot_anim.setStartValue(float(self._dot_r))
        self._dot_anim.setEndValue(target)
        self._dot_anim.start()

    def dot_state(self) -> dict:
        """★ task-79 可读锚点（进度手柄半径/高度）。"""
        return {"radius": round(float(self._dot_r), 3),
                "diameter": round(float(self._dot_r) * 2.0, 3),
                "target_radius": self._dot_target(),
                "hover": bool(self._hover), "dragging": bool(self._dragging),
                "height": int(self.height()),
                "center_y": round(self.height() / 2.0, 3)}

    # -- 数据 ----------------------------------------------------------------
    def set_duration(self, value: float):
        self._duration = max(0.0, float(value or 0.0))
        self.update()

    def set_position(self, value: float):
        if not self._dragging:
            self._position = max(0.0, float(value or 0.0))
            self.update()

    def set_cache(self, value: float):
        self._cache = max(0.0, float(value or 0.0))
        self.update()

    def duration(self) -> float:
        return self._duration

    def position(self) -> float:
        return self._position

    def is_dragging(self) -> bool:
        """是否正在拖拽（task-20 ②）。

        拖动期主窗必须**屏蔽 time-pos 回写**：滑块自己的 `set_position` 有
        `_dragging` 守卫，但左侧时间文本没有 —— 于是"拖到一半被回写跳回去"。
        把状态公开出去，让控制栏与主窗用同一个判据。
        """
        return bool(self._dragging)

    def set_chapters(self, chapters: list[tuple[float, str]]):
        self._chapters = chapters or []
        self.update()

    # -- 几何 ----------------------------------------------------------------
    def _track_rect(self) -> QRectF:
        h = M["progress_h"] + (M["progress_h_hover"] - M["progress_h"]) * self._hover_t
        # ★ task-73：圆点 13 + 投影 3 + 1px 余量；左右内缩保证投影不被裁
        pad = M["progress_handle"] / 2 + 3.0 + 1.0
        r = QRectF(self.rect()).adjusted(pad, 0, -pad, 0)
        y = r.center().y() - h / 2
        return QRectF(r.left(), y, r.width(), h)

    def _x_to_time(self, x: float) -> float:
        tr = self._track_rect()
        if tr.width() <= 0 or self._duration <= 0:
            return 0.0
        t = (x - tr.left()) / tr.width()
        return max(0.0, min(1.0, t)) * self._duration

    def _time_to_x(self, sec: float) -> float:
        tr = self._track_rect()
        if self._duration <= 0:
            return tr.left()
        return tr.left() + tr.width() * max(0.0, min(1.0, sec / self._duration))

    # -- 事件 ----------------------------------------------------------------
    def enterEvent(self, ev):
        self._hover = True
        self._anim.stop()
        self._anim.setStartValue(self._hover_t)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._animate_dot()                  # ★ task-79
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self._hover_x = -1.0
        self.hoverChanged.emit(-1.0)
        self._anim.stop()
        self._anim.setStartValue(self._hover_t)
        self._anim.setEndValue(0.0)
        self._anim.start()
        self._animate_dot()                  # ★ task-79
        super().leaveEvent(ev)

    def hover_seconds(self) -> float:
        """当前鼠标位置对应的时间（秒）。没有悬停时返回 -1。"""
        if not self._hover or self._hover_x < 0 or self._duration <= 0:
            return -1.0
        return max(0.0, min(self._duration, self._x_to_time(self._hover_x)))

    def mouseMoveEvent(self, ev):
        self._hover_x = ev.position().x()
        if self._dragging:
            self._drag_value = self._x_to_time(self._hover_x)
            self.seekPreview.emit(self._drag_value)
        self.hoverChanged.emit(self._x_to_time(self._hover_x))
        self.update()
        super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._duration > 0:
            self._dragging = True
            self._drag_value = self._x_to_time(ev.position().x())
            self._animate_dot()              # ★ task-79
            self.seekStarted.emit()
            self.seekPreview.emit(self._drag_value)
            self.update()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._animate_dot()              # ★ task-79
            self.seekCommitted.emit(self._drag_value)
            self.update()
        super().mouseReleaseEvent(ev)

    # -- 绘制 ----------------------------------------------------------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        tr = self._track_rect()
        radius = tr.height() / 2
        # 轨道
        p.setPen(Qt.NoPen)
        # 未激活滑轨：浅灰 #E5E6EB（用户："音量条常态隐身"）
        p.setBrush(qt_color(P["glass_track"]))
        p.drawRoundedRect(tr, radius, radius)
        # 缓冲
        if self._duration > 0 and self._cache > 0:
            cw = tr.width() * max(0.0, min(1.0, self._cache / self._duration))
            p.setBrush(qt_color("#FFFFFF30"))
            p.drawRoundedRect(QRectF(tr.left(), tr.top(), cw, tr.height()), radius, radius)
        # 章节刻度
        if self._chapters and self._duration > 0:
            p.setBrush(qt_color("#00000055"))
            for sec, _name in self._chapters:
                x = self._time_to_x(sec)
                p.drawRect(QRectF(x - 0.5, tr.top(), 1.0, tr.height()))
        # 已播放
        pos = self._drag_value if self._dragging else self._position
        if self._duration > 0:
            pw = tr.width() * max(0.0, min(1.0, pos / self._duration))
            if pw > 0.5:
                from . import theme as _theme_ps
                _glass_ps = bool(_theme_ps.is_glass_skin())
                play_r = QRectF(tr.left(), tr.top(), pw, tr.height())
                if _glass_ps:
                    # ★ task-72：进度条已播段 = 有色琉璃（上亮下暗宝石蓝 + 1px 高光/深边）
                    from PySide6.QtGui import QLinearGradient, QPainterPath
                    grad = QLinearGradient(tr.topLeft(), tr.bottomLeft())
                    grad.setColorAt(0.0, QColor(56, 148, 255, 158))
                    grad.setColorAt(1.0, QColor(0, 102, 235, 199))
                    p.setBrush(grad)
                    p.drawRoundedRect(play_r, radius, radius)
                    clip = QPainterPath()
                    clip.addRoundedRect(play_r, radius, radius)
                    p.setClipPath(clip)
                    p.fillRect(QRectF(play_r.left(), play_r.top(), play_r.width(), 1.0),
                               QColor(255, 255, 255, 166))
                    p.fillRect(QRectF(play_r.left(), play_r.bottom() - 1.0,
                                      play_r.width(), 1.0), QColor(0, 40, 100, 77))
                    p.setClipping(False)
                else:
                    p.setBrush(qt_color(P["accent"]))
                    p.drawRoundedRect(play_r, radius, radius)
                # 已播放端点的一点点辉光，让"高光"更精致
                glow = QColor(qt_color(P["accent"]))
                glow.setAlphaF(0.35)
                p.setBrush(glow)
                p.drawEllipse(QPointF(tr.left() + pw, tr.center().y()), tr.height() * 0.95, tr.height() * 0.95)
        # ---- 悬停预选填充条 ------------------------------------------------
        # ⚠️⚠️ **必须画在手柄之前**。原来画在手柄之后，它的**左圆角帽**正好落在
        #    播放头圆点上，看起来就是"蓝色缓冲条从圆点内部透出来 / 圆点被撞碎"
        #    （用户截图圈出的就是这一块）。
        head = self._time_to_x(self._drag_value if self._dragging else self._position)
        hx = -1.0
        if self._hover and self._hover_x >= 0 and self._duration > 0:
            hx = self._time_to_x(self._x_to_time(self._hover_x))
            if hx > head + 1.0:
                fill = QColor(qt_color(P["accent"]))
                fill.setAlphaF(0.25)
                p.setPen(Qt.NoPen)
                p.setBrush(fill)
                p.drawRoundedRect(
                    QRectF(head, tr.top(), hx - head, tr.height()), radius, radius)

        # ---- 手柄（画在最后，压住所有下层元素）------------------------------
        show_handle = self._hover_t > 0.02 or self._dragging
        if show_handle and self._duration > 0:
            # ★ task-79：半径取动画插值值（4.0 ↔ 7.0 平滑；不再用 hover_t 线性公式瞬变）
            r = max(0.5, float(self._dot_r))
            # ★ task-73：≤3px 柔和微投影（先画阴影再画纯白圆点 + 1px 细边）
            p.setPen(Qt.NoPen)
            for i, alpha in ((3.0, 40), (2.0, 56), (1.0, 71)):
                p.setBrush(QColor(0, 0, 0, alpha))
                p.drawEllipse(QPointF(head, tr.center().y()), r + i, r + i)
            # 圆点一律**纯白**（用户："你怎么把浅色模式的圆点改得那么丑，给我改回白色去"）
            p.setBrush(QColor(255, 255, 255))
            p.setPen(QPen(QColor(0, 0, 0, 56), 1.0))
            p.drawEllipse(QPointF(head, tr.center().y()), r, r)

        # ---- 悬停竖线指针：距圆点 < 8px 时**不画**，避免切割圆点 -------------
        # ⚠️ 避让阈值 8px -> 12px（用户反馈 8px 仍会视觉上贴到圆点）：
        #    距圆点 12px 内**完全不画**竖线。手柄本身已经画在最后（最顶层）。
        if hx >= 0 and abs(hx - head) >= 12.0:
            pen = QPen(qt_color("#FFFFFF66"))
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawLine(QPointF(hx, tr.top() - 4), QPointF(hx, tr.bottom() + 4))
        p.end()


class VolumeSlider(QWidget):
    """细音量条，带喇叭图标 + 悬停数值。

    ★★ task-79（用户问题 2、11）：**双态半径 + 平滑插值 + 零削平**
      · 常态（未悬停/未拖拽）r = 4.5px（直径 9）；
      · 悬停（underMouse）或拖拽 ⇒ r = 7.0px（直径 14）；
      · 半径用 QPropertyAnimation 平滑插值（150ms OutCubic），不是瞬跳；
      · 控件定高 **32**（中心 y=16）：最大 7 + 投影 3 = 10 < 16 ⇒ 上下各留 **6px**；
      · 绘制圆点**不做任何 setClipRect**（360° 圆润）。
    """

    valueChanged = Signal(float)   # 0..100

    DOT_R_NORMAL = 4.5             # 常态半径（判据 4.5±0.5）
    DOT_R_ACTIVE = 7.0             # 悬停/拖拽半径（判据 7.0±0.5）
    DOT_SHADOW = 3.0               # 柔和微投影外扩（总半径上界 10）
    DOT_ANIM_MS = 150              # 平滑插值时长（动画而非瞬跳）

    def __init__(self, parent=None, width: int = 90):
        super().__init__(parent)
        # ★ task-79：控件高度 32（中心 y=16）；最大半径 7 + 投影 3 = 10 ⇒ 上下各留 6px 真空余量
        self.setFixedSize(width, 32)
        self.setMinimumHeight(32)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self._value = 100.0
        self._dragging = False
        self._hover = False
        self._hover_t = 0.0
        self._dot_r = float(self.DOT_R_NORMAL)
        self._dot_anim_target = float(self.DOT_R_NORMAL)
        self._anim = QPropertyAnimation(self, b"hoverT", self)
        self._anim.setDuration(140)
        self._dot_anim = QPropertyAnimation(self, b"dotR", self)
        self._dot_anim.setDuration(self.DOT_ANIM_MS)
        self._dot_anim.setEasingCurve(QEasingCurve.OutCubic)

    def get_hover_t(self) -> float:
        return self._hover_t

    def set_hover_t(self, v: float):
        self._hover_t = float(v)
        self.update()

    hoverT = Property(float, get_hover_t, set_hover_t)

    # -- 圆点半径（★ task-79：双态 + 平滑插值）-------------------------------
    def get_dot_r(self) -> float:
        return self._dot_r

    def set_dot_r(self, v: float):
        self._dot_r = float(v)
        self.update()

    dotR = Property(float, get_dot_r, set_dot_r)

    def _dot_target(self) -> float:
        """圆点目标半径：悬停（underMouse）/ 拖拽 ⇒ ACTIVE，否则 NORMAL。"""
        active = bool(self._dragging or self._hover or self.underMouse())
        return float(self.DOT_R_ACTIVE if active else self.DOT_R_NORMAL)

    def _animate_dot(self):
        """把圆点半径**平滑**插值到目标值（幂等：目标没变就不重启动画）。"""
        target = float(self._dot_target())
        if abs(target - self._dot_anim_target) < 0.01 and abs(target - self._dot_r) < 0.01:
            return
        self._dot_anim_target = target
        self._dot_anim.stop()
        self._dot_anim.setStartValue(float(self._dot_r))
        self._dot_anim.setEndValue(target)
        self._dot_anim.start()

    def dot_state(self) -> dict:
        """★ task-79 可读锚点（QA 真机判据直接读）。"""
        tr = self._track()
        cx = tr.left() + tr.width() * self._value / 100.0
        return {"radius": round(float(self._dot_r), 3),
                "diameter": round(float(self._dot_r) * 2.0, 3),
                "target_radius": self._dot_target(),
                "hover": bool(self._hover), "dragging": bool(self._dragging),
                "under_mouse": bool(self.underMouse()),
                "height": int(self.height()),
                "center_y": round(self.height() / 2.0, 3),
                "dot_center": [round(cx, 3), round(tr.center().y(), 3)],
                "track": [round(tr.left(), 3), round(tr.top(), 3),
                          round(tr.width(), 3), round(tr.height(), 3)],
                "shadow_extent": self.DOT_SHADOW,
                "min_margin_y": round(self.height() / 2.0 - (float(self._dot_r) + self.DOT_SHADOW), 3)}

    def value(self) -> float:
        return self._value

    def set_value(self, v: float):
        v = max(0.0, min(100.0, float(v)))
        if abs(v - self._value) > 0.01:
            self._value = v
            self.update()

    def _track(self) -> QRectF:
        """音量轨道。

        ⚠️ 左右内缩一个**最大**圆点半径：否则音量拉到 100% / 0% 时圆点圆心正好落在控件
        边界上，半个圆被裁掉（用户实测反馈"音量条拖到最右边以后小圆点被截断"）。
        ★ task-79：最大半径按 ACTIVE(7) 算 + 投影 3 + 1px 余量 = 11。
        """
        h = 3.0 + 3.0 * self._hover_t
        pad = self.DOT_R_ACTIVE + self.DOT_SHADOW + 1.0
        return QRectF(pad, (self.height() - h) / 2.0, max(1.0, self.width() - 2 * pad), h)

    def _apply(self, x: float):
        tr = self._track()
        v = max(0.0, min(1.0, (x - tr.left()) / max(1.0, tr.width()))) * 100.0
        self.set_value(v)
        self.valueChanged.emit(self._value)

    def enterEvent(self, ev):
        self._hover = True
        self._anim.stop()
        self._anim.setStartValue(self._hover_t)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._animate_dot()                  # ★ task-79：r 4.5 → 7.0 平滑放大
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self._anim.stop()
        self._anim.setStartValue(self._hover_t)
        self._anim.setEndValue(0.0)
        self._anim.start()
        self._animate_dot()                  # ★ task-79：r 7.0 → 4.5 平滑收回（拖拽中保持大）
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._dragging = True
            self._animate_dot()              # ★ task-79：按下立即驶向 ACTIVE
            self._apply(ev.position().x())
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._dragging:
            self._apply(ev.position().x())
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._dragging = False
        self._animate_dot()                  # ★ task-79：松手后光标还在条上就保持大，否则收回
        super().mouseReleaseEvent(ev)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        tr = self._track()
        r = tr.height() / 2
        p.setPen(Qt.NoPen)
        # 未激活滑轨：浅灰 #E5E6EB（用户："音量条常态隐身"）
        p.setBrush(qt_color(P["glass_track"]))
        p.drawRoundedRect(tr, r, r)
        w = tr.width() * self._value / 100.0
        if w > 0.5:
            # ⚠️ 常驻品牌蓝，与主进度条完全看齐（用户：
            # "取消常态发灰的跳变逻辑，全程常驻显示蓝色已填充轨道"）。
            # 原来常态是 #FFFFFFB0（白色），在浅色底上就是一段发灰的空轨。
            from . import theme as _theme_v
            if _theme_v.is_glass_skin():
                from PySide6.QtGui import QLinearGradient
                grad = QLinearGradient(tr.topLeft(), tr.bottomLeft())
                grad.setColorAt(0.0, QColor(56, 148, 255, 158))
                grad.setColorAt(1.0, QColor(0, 102, 235, 199))
                p.setBrush(grad)
            else:
                p.setBrush(qt_color(P["accent"]))
            p.drawRoundedRect(QRectF(tr.left(), tr.top(), w, tr.height()), r, r)
        # ★ task-73：圆点常驻纯白（不再只有 hover 才出现；浅色档不再有黑点）
        # ★ task-79：半径 = 动画插值中的 self._dot_r（4.5 ↔ 7.0 平滑）；
        #    投影 3px 外扩后总半径 ≤ 10 < 控件半高 16 ⇒ 上下各留 6px，**零削平**；
        #    这里**没有**任何 setClipRect / setClipPath —— 圆点 360° 完整。
        x = tr.left() + w
        from PySide6.QtGui import QPen
        dot = max(0.5, float(self._dot_r))
        p.setPen(Qt.NoPen)
        for i, alpha in ((3.0, 40), (2.0, 56), (1.0, 71)):
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawEllipse(QPointF(x, tr.center().y()), dot + i, dot + i)
        p.setBrush(QColor(255, 255, 255))
        p.setPen(QPen(QColor(0, 0, 0, 56), 1.0))
        p.drawEllipse(QPointF(x, tr.center().y()), dot, dot)
        p.end()


class LogSlider(VolumeSlider):
    """把音量条的外观**照搬**到任意「对数标度」参数上（用户要求：缩放条照抄音量条）。

    * 圆点**常驻**（音量条是悬停/拖动才画圆点）；
    * 用 **log2 标度**：10%~1000% 在轨道上均匀铺开，**100%（适应窗口）正好落在正中**——
      线性百分比会把 100% 挤到最左边 9% 的位置，滑条几乎没法用；
    * 拖动期间外部不要回写（否则和用户的手抢控制权 ⇒ 闪烁回弹）。
    """

    def __init__(self, parent=None, width: int = 150, lo: float = 0.1, hi: float = 10.0):
        super().__init__(parent, width=width)
        self._lo, self._hi = float(lo), float(hi)
        self._scale = 1.0
        self._hover = True
        self._hover_t = 1.0
        # ★ task-79：本控件"圆点常驻" ⇒ 半径恒为 ACTIVE（悬停不放大、离开不缩小）
        self._dot_anim.stop()
        self._dot_anim_target = float(self.DOT_R_ACTIVE)
        self.set_dot_r(self.DOT_R_ACTIVE)

    def _dot_target(self) -> float:
        return float(self.DOT_R_ACTIVE)

    def enterEvent(self, ev):             # noqa: N802
        self._hover = True
        super().enterEvent(ev)

    def leaveEvent(self, ev):             # noqa: N802
        self._hover = True                # 圆点常驻
        self._dot_anim.stop()
        self.set_dot_r(self.DOT_R_ACTIVE)
        ev.accept()

    def is_dragging(self) -> bool:
        return bool(self._dragging)

    def _t_of(self, scale: float) -> float:
        import math

        lo, hi = math.log2(self._lo), math.log2(self._hi)
        return max(0.0, min(1.0, (math.log2(max(1e-6, float(scale))) - lo) / (hi - lo)))

    def scale(self) -> float:
        return self._scale

    def set_scale(self, scale: float, emit: bool = False):
        """按倍数设置（1.0 = 100% = 适应窗口）。emit=False 供外部静默回填。"""
        s = max(self._lo, min(self._hi, float(scale)))
        self._scale = s
        self.set_value(self._t_of(s) * 100.0)
        if emit:
            self.valueChanged.emit(self._value)

    def _apply(self, x: float):
        import math

        tr = self._track()
        t = max(0.0, min(1.0, (x - tr.left()) / max(1.0, tr.width())))
        lo, hi = math.log2(self._lo), math.log2(self._hi)
        self._scale = 2.0 ** (lo + t * (hi - lo))
        self.set_value(t * 100.0)
        self.valueChanged.emit(self._value)


class WrappedLabel(QLabel):
    """自动换行的 QLabel，**但会把自己真正需要的高度顶进 minimumSizeHint**。

    ⚠️ 普通 `wordWrap=True` 的 QLabel 的 `minimumSizeHint()` 非常小（"反正能换行"），
    于是布局器有权把它压到比实际文字还矮 —— 表现成**文字被水平腰斩 / 上下两行压在一起**
    （用户实测反馈：落地页副标题被折断遮挡）。
    这里按当前宽度用 `heightForWidth()` 算出真实需要的高度，同时改写 sizeHint 与
    minimumSizeHint，布局器就再也压不动它了。
    """

    def __init__(self, text: str = "", parent=None, align=Qt.AlignCenter):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setAlignment(align)
        sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def _need_h(self) -> int:
        # 参考宽度优先取**父容器**的宽度：本控件自己的 width() 在布局第一轮里还是旧的，
        # 用它算高度会来回震荡（量出 2 行 → 给 34px → 下一轮按新宽度只算 1 行 → …）。
        ref = 0
        par = self.parentWidget()
        if par is not None and par.width() > 0:
            ref = par.width()
        ref = max(ref, self.width(), 320)
        h = self.heightForWidth(ref)
        return h if h > 0 else super().sizeHint().height()

    def sizeHint(self) -> QSize:
        s = super().sizeHint()
        return QSize(s.width(), max(s.height(), self._need_h()))

    def minimumSizeHint(self) -> QSize:
        s = super().minimumSizeHint()
        return QSize(s.width(), max(s.height(), self._need_h()))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        need = self._need_h()
        if need > self.minimumHeight():
            self.setMinimumHeight(need)


# --------------------------------------------------------------------------------------
# OSD
# --------------------------------------------------------------------------------------


class OsdBadge(GlassFrame):
    """屏幕中央/底部的操作提示。

    ⚠️ 用户规范：OSD 一律用**半透明深黑底 + 纯白字**（rgba(20,20,22,0.85) / #FFFFFF），
    不允许浅灰字 —— 它是浮在画面上的，背景是什么颜色都有可能。
    """

    """居中的 OSD 提示胶囊（音量 / 倍速 / 跳转 / 状态）。

    ⚠️ 性能要点：音量条拖动时 `valueChanged` 每移动一像素就发一次，
    如果每次都 `setStyleSheet` + `adjustSize`（会触发样式重算与整棵子控件重新 polish），
    拖动就会明显发涩。所以这里对**内容完全相同的重复调用**直接短路，
    只续期隐藏定时器，不做任何重排；并且只用一个 QTimer 反复 start，不再每次
    `QTimer.singleShot` 新建定时器对象。
    """

    def __init__(self, parent=None):
        super().__init__(
            parent,
            radius=M["radius_lg"],
            # ⚠️⚠️ OSD 一律**深黑半透明底**，不跟主题走。
            #    它是浮在**画面**上的，背后可能是任何颜色（浅色画面、白墙…），
            #    用主题的浅色面板底就会变成"浅白底 + 白字"= 隐形
            #    （用户截图：视频里那个 00:39 / 01:28 胶囊几乎看不见）。
            fill=P.get("osd_bg", "#141416D9"),
            shadow=0.0,
            shadow_spread=0,
            border=P.get("osd_border", "#FFFFFF26"),
            pad=(14, 10, 14, 10)
        )
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._row = QHBoxLayout()
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(10)
        self._icon = QLabel(self)
        self._icon.setFixedSize(22, 22)
        self._text = QLabel(self)
        # 文字与图标强制纯白：深黑底上必须高对比
        self._text.setStyleSheet(f"color:{P.get('osd_text', '#FFFFFF')};")
        f = QFont(self.font())
        f.setPixelSize(14)
        f.setWeight(QFont.Medium)
        self._text.setFont(f)
        self._row.addWidget(self._icon)
        self._row.addWidget(self._text)
        self.body_layout().addLayout(self._row)

        self._cur = None            # (text, icon, color)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_hide_timeout)

    def _on_hide_timeout(self):
        self.hide()
        # 承载它的浮层盒子只是个定位容器, 没有内容时不应继续占位
        holder = self.parentWidget()
        if holder is not None and holder.layout() is not None:
            holder.setVisible(False)

    def show_message(self, text: str, icon_name: str = "", color: str | None = None, ms: int = 1100):
        # 默认色在调用时解析（不要写成默认参数，那是导入时快照 —— 浅色启动会错）
        color = P["text"] if color is None else color
        key = (text, icon_name, color)
        if key == self._cur and self.isVisible():
            # 内容没变：只续期，零重排
            self._hide_timer.start(max(200, int(ms)))
            return
        first = self._cur is None
        self._cur = key
        if self._text.text() != text:
            self._text.setText(text)
        # ⚠️⚠️ 颜色**强制为 OSD 专用白**，忽略调用方传进来的 color。
        #    调用方常传 `P["accent"]`（品牌蓝）或 `P["text"]`，在深黑 OSD 底上
        #    蓝色发闷、深色主题的 `text` 更是黑字黑底 —— 都会糊。
        #    实测这里原来把上面设好的白色又覆盖成了 #0080F5。
        color = P.get("osd_text", "#FFFFFF")
        self._text.setStyleSheet(f"color:{color};")
        if icon_name:
            if self._icon.property("icon_name") != icon_name or self._icon.property("icon_color") != color:
                self._icon.setPixmap(icons.render_pixmap(
                    icon_name, color, 22, self.devicePixelRatioF() or 2.0, 1.8))
                self._icon.setProperty("icon_name", icon_name)
                self._icon.setProperty("icon_color", color)
            self._icon.show()
        else:
            self._icon.hide()
        self.setVisible(True)
        if first or not self.isVisible():
            self.raise_()
            self.adjustSize()
        self._hide_timer.start(max(200, int(ms)))

    def hide_badge(self):
        self._cur = None
        self._hide_timer.stop()
        self.hide()


# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------


class RoundedTip(QWidget, Surface):
    """自绘的圆角提示条 —— **替掉 Qt 原生的 QToolTip**。

    为什么必须自己画（用户截图："这不还有吗"）：`QToolTip` 是**原生顶层窗口**，
    QSS 的 `border-radius` 只能把"圆角卡片"画在方窗口**里面**，
    窗口本身的四个角照样是方的、照样露出底色 ⇒ 看着就是"圆角卡片 + 四个方角"。
    只有"无边框 + 半透明 + 自绘圆角 + 圆角遮罩"才可能真圆角。

    用法：应用级事件过滤器 `TooltipFilter` 捕获 `QEvent.ToolTip`，
    显示本控件并**吃掉事件**（Qt 就不会再弹原生提示条）。
    """

    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # ★ task-85 自洁：**不要** WA_NoSystemBackground —— 它会阻止 Qt 在重绘前清理
        #    上层小窗 surface，旧位置/旧尺寸的像素会留下白色方块（实测：真实鼠标 hover 复现）。
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._text = ""
        self._radius = 8.0
        self._pad_x, self._pad_y = 10, 6
        # ★★ task-85 复整：自洁回调 —— 提示条隐藏后必须让下方主窗重绘该矩形，
        #    否则半透明顶层小窗被合成后会在原地留下白色方块残影（真实鼠标 hover 复现）。
        self._clean_cb = None
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(self.hide)

    def show_for(self, text: str, global_pos) -> None:
        self._text = str(text)
        f = QFont(self.font())
        f.setPixelSize(12)
        self.setFont(f)
        fm = QFontMetrics(f)
        w = min(460, fm.horizontalAdvance(self._text) + self._pad_x * 2 + 2)
        h = fm.height() + self._pad_y * 2
        self.setFixedSize(w, h)
        self.move(global_pos)
        self.show()
        self.raise_()
        self.update()
        # ⚠️ 不加 setMask（硬边裁剪会有锯齿）；窗口半透明 + 自绘抗锯齿圆角即可。
        self._hide.start(6000)

    def hideEvent(self, ev):  # noqa: N802
        super().hideEvent(ev)
        cb = self._clean_cb
        if cb is not None:
            try:
                cb(self)
            except Exception:
                log.debug("提示条自洁失败", exc_info=True)

    def paintEvent(self, _ev):  # noqa: N802
        from . import theme as _theme_tip

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if _theme_tip.is_glass_skin():
            # ★★ task-78③：Tooltip 玻璃化 —— 半透磨砂琉璃底 + 1px rgba(255,255,255,0.28)
            #    折射亮边 + 纯白文字（+1px 极弱衬影）。本控件是自绘半透明窗口，
            #    可以直接用带 alpha 的底色（不像原生 QToolTip 会文字穿透）。
            p.setPen(QPen(qt_color(_theme_tip.GLASS_PANEL_EDGE), 1.0))
            p.setBrush(qt_color(_theme_tip.GLASS_MENU_BG))
            p.drawRoundedRect(r, self._radius, self._radius)
            tr = r.adjusted(self._pad_x, 0, -self._pad_x, 0)
            # ★ task-81：UI 文字只画一层（不再画 1px 偏移衬影 → 笔画不再粘连）
            p.setPen(qt_color(_theme_tip.GLASS_TEXT))
            p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter, self._text)
        else:
            p.setPen(QPen(qt_color(P.get("hairline_strong", "#00000022")), 1.0))
            p.setBrush(qt_color(P.get("popup_bg", "#FFFFFF")))
            p.drawRoundedRect(r, self._radius, self._radius)
            p.setPen(qt_color(P.get("text", "#1D2129")))
            p.drawText(r.adjusted(self._pad_x, 0, -self._pad_x, 0),
                       Qt.AlignLeft | Qt.AlignVCenter, self._text)
        p.end()


class TooltipFilter(QObject):
    """应用级过滤器：把 Qt 原生提示条换成自绘圆角提示条。

    装到 `QApplication` 上即可，所有 `setToolTip()` 照旧写，不用改调用点。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tip = RoundedTip()
        self._tip._clean_cb = self._clean_after_tip

    @staticmethod
    def _clean_after_tip(tip) -> None:
        """★ task-85 自洁：提示条消失后，把主窗上被它盖过的那块**强制重绘**。

        半透明顶层小窗 + 主窗 WA_TranslucentBackground 的组合下，小窗消失后
        主窗该区域可能停留在旧 surface（实测屏幕抓取有白块、而 w.grab() 干净）；
        这里给主窗一个局部 update()，让 surface 变新。
        """
        try:
            from PySide6.QtCore import QRect
            rect = tip.geometry()
            app = QApplication.instance()
            if app is None:
                return
            for w in app.topLevelWidgets():
                if w is tip or not w.isVisible():
                    continue
                if type(w).__name__ != "MainWindow":
                    continue
                local = w.mapFromGlobal(rect.topLeft())
                w.update(QRect(local, rect.size()).adjusted(-8, -8, 8, 8))
        except Exception:
            log.debug("提示条自洁重绘失败", exc_info=True)

    def eventFilter(self, obj, ev):  # noqa: N802
        try:
            t = ev.type()
            if t == QEvent.ToolTip:
                text = ""
                try:
                    text = obj.toolTip() or ""
                except Exception:
                    text = ""
                if text:
                    # ⚠️ ToolTip 事件实际是 **QHelpEvent**（有 globalPos）。
                    #    但别直接调 `ev.globalPos()`：万一是别的类型会抛异常，
                    #    被下面的 except 吞掉 ⇒ 提示条静默不显示（实测踩过）。
                    gp = None
                    getter = getattr(ev, "globalPos", None)
                    if callable(getter):
                        try:
                            gp = getter()
                        except Exception:
                            gp = None
                    if gp is None:
                        gp = QCursor.pos()
                    self._tip.show_for(text, QPoint(gp.x() + 14, gp.y() + 20))
                    return True          # 吃掉 ⇒ Qt 不再显示原生提示条
            elif t in (QEvent.Leave, QEvent.MouseButtonPress, QEvent.Wheel,
                       QEvent.KeyPress, QEvent.WindowDeactivate, QEvent.Hide):
                self._tip.hide()
        except Exception:
            log.debug("提示条过滤器异常", exc_info=True)
        return False


def hline(color: str | None = None, height: int = 1) -> QWidget:
    """一条 1px 分隔线。

    ⚠️ 颜色是**构造时**写进内联样式的，所以这里把它登记进换肤广播 ——
       否则"深色切浅色"之后它会留着深色主题的那条线（本项目的老毛病）。
       显式传了自定义颜色的（比如红色警示线）不登记，保持调用方本意。
    """
    auto = color is None
    color = P["hairline"] if auto else color
    w = QWidget()
    w.setFixedHeight(height)
    w.setStyleSheet(f"background:{css(color)};border:none;")
    if auto:
        themed(w, lambda x: x.setStyleSheet(f"background:{css(P['hairline'])};border:none;"))
    return w


def label(text: str, role: str = "", parent=None, color: str | None = None,
          size: int | None = None, weight: int | None = None) -> QLabel:
    lb = QLabel(text, parent)
    if role:
        lb.setProperty("role", role)
    bits = []
    if color:
        bits.append(f"color:{color};")
    if size:
        bits.append(f"font-size:{size}px;")
    if weight:
        bits.append(f"font-weight:{weight};")
    if bits:
        lb.setStyleSheet("".join(bits))
    return lb


def elide(text: str, width: int, metrics: QFontMetrics, mode=Qt.ElideRight) -> str:
    return metrics.elidedText(text, mode, max(10, width))


class Spacer(QWidget):
    def __init__(self, w: int = 0, h: int = 0):
        super().__init__()
        if w:
            self.setFixedWidth(w)
        if h:
            self.setFixedHeight(h)
        self.setSizePolicy(QSizePolicy.Fixed if w else QSizePolicy.Expanding,
                           QSizePolicy.Fixed if h else QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)


def vspace(h: int) -> QWidget:
    w = QWidget()
    w.setFixedHeight(h)
    return w


# --------------------------------------------------------------------------------------
# 圆角弹出菜单
# --------------------------------------------------------------------------------------
class MenuRow(QWidget):
    """菜单里的一行：**左标题 + 右快捷键提示**（自绘）。

    ⚠️ 为什么不用原生 QAction 的 `\\t` 快捷键写法：
       Qt 会把 `\\t` 后面的文字右对齐画出来，但用的是**和标题一模一样的颜色与字号**，
       而 QSS 里没有 `QMenu::shortcut` 这种选择器（`QMenu::item` 只能管整行）。
       用户要求的是现代桌面端的层级：标题 13px 主色、快捷键 11px 次级灰、
       严格右对齐到 16px 内边距 —— 只有自绘才能做到，顺带把 hover 高亮做成圆角。

    交互：`WA_Hover` + `mouseReleaseEvent`（**不用 QAction 的 triggered**）。
    """

    clicked = Signal()
    HEIGHT = 32
    PAD_L = 14
    PAD_R = 16
    HINT_GAP = 28

    def __init__(self, text: str, hint: str = "", enabled: bool = True,
                 danger: bool = False, parent=None):
        super().__init__(parent)
        self._text = str(text)
        self._hint = str(hint or "")
        self._hover = False
        self._danger = bool(danger)
        self._owner_menu = None          # ★ task-70：由 RoundedMenu.add_row 注入
        self.setAttribute(Qt.WA_Hover, True)
        self.setMouseTracking(True)
        self.setEnabled(bool(enabled))
        self.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)

    # -- 尺寸 ----------------------------------------------------------------
    def _title_font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(13)
        return f

    def _hint_font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(11)
        return f

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self._title_font())
        hm = QFontMetrics(self._hint_font())
        w = (self.PAD_L + fm.horizontalAdvance(self._text) + self.HINT_GAP
             + (hm.horizontalAdvance(self._hint) if self._hint else 0) + self.PAD_R)
        return QSize(max(M["menu_min_w"], w), self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    # -- 交互 ----------------------------------------------------------------
    def enterEvent(self, ev):  # noqa: N802
        # ★ task-70：hover **全局唯一排他** —— 先让所属菜单把所有其它行清掉，
        #    再点亮自己（快速划过时绝不会残留多行蓝色高亮）。
        own = self._owner_menu
        if own is not None:
            try:
                own.exclusive_hover(self)
            except RuntimeError:
                self._hover = True
        else:
            self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):  # noqa: N802
        own = self._owner_menu
        if own is not None:
            try:
                # 只有"自己仍是当前 hover 行"时才清；已经切到下一行时别把新行熄灭
                if getattr(own, "_hover_row", None) is self:
                    own.exclusive_hover(None)
            except RuntimeError:
                self._hover = False
        else:
            self._hover = False
        self.update()
        super().leaveEvent(ev)

    def mouseReleaseEvent(self, ev):  # noqa: N802
        if ev.button() == Qt.LeftButton and self.isEnabled() and self.rect().contains(
                ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    # -- 绘制 ----------------------------------------------------------------
    def paintEvent(self, _ev):  # noqa: N802
        from . import theme as _theme_row

        glass = bool(_theme_row.is_glass_skin())
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(4.0, 2.0, -4.0, -2.0)
        if self._hover and self.isEnabled():
            p.setPen(Qt.NoPen)
            p.setBrush(qt_color(P["accent_soft"]))
            p.drawRoundedRect(r, 8.0, 8.0)
        on = self.isEnabled()
        # ★ task-78③：玻璃菜单里标题 = **纯白**（hover 才用强调色）；既有主题不变
        _base = _theme_row.GLASS_TEXT if glass else P["text"]
        title_col = P["accent"] if (self._hover and on and not self._danger) else (
            P["danger"] if self._danger else _base)
        if not on:
            title_col = P["text_muted"]
        hint_col = P["text_muted"]
        p.setFont(self._title_font())
        tr = QRectF(r.left() + self.PAD_L - 4, r.top(), r.width() - self.PAD_L - self.PAD_R + 8,
                    r.height())
        # ★ task-81：标题只画一层干净文本（删掉原来的 1px 衬影，避免笔画粘连）
        p.setPen(qt_color(title_col))
        p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter, self._text)
        if self._hint:
            p.setFont(self._hint_font())
            hr = QRectF(r.right() - self.PAD_R - 120, r.top(), 120.0, r.height())
            # ★ task-81：快捷键也**只画一层**
            p.setPen(qt_color(hint_col))
            p.drawText(hr, Qt.AlignRight | Qt.AlignVCenter, self._hint)
        p.end()


class RoundedMenu(QMenu, Surface):
    """**自己画圆角面板**的弹出菜单。

    为什么不靠 QSS 的 `border-radius`：
      * `QMenu` 是独立顶层窗口，QSS 的圆角只能圆角化它**自己画的背景**，
        窗口本身的矩形边界靠 QSS 是管不到的；
      * 实测反复出现“圆角外侧残留方形直角底”，调 QSS 怎么都不干净。

    做法：把 QSS 的背景设成**完全透明**，然后在 `paintEvent` 里
    **自己用 QPainterPath 填一个圆角矩形 + 1px 描边**，
    再调 `super().paintEvent()` 让 QMenu 把菜单项画在上面。
    """
    RADIUS = 10.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []
        self._submenus: list = []
        # ★ task-70：菜单内 hover 唯一排他的当前行（None = 无高亮）
        self._hover_row = None
        # 浮层统一基类：窗口属性 + 禁投影 + 圆角绘制一次到位（见 surface.py）
        self.surface_init(radius=float(self.RADIUS), pad=6)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        _MENUS_LIVE.append(weakref.ref(self))
        # 背景交给 paintEvent 自己画，QSS 只管菜单项（样式集中在 theme.menu_qss，
        # 换肤时由 refresh_glass_theme() 统一重刷 —— 见那里的注释）
        self.apply_menu_theme()
        # ⚠️⚠️ Qt 会给 QMenu 自动挂一个 `QGraphicsDropShadowEffect`（平台不支持
        #    原生阴影时）。它会在菜单外面**再画一层黑影** —— 用户原话
        #    "这个悬浮窗怎么还有个莫名其妙的叠层，一层暗一层阴" 就是它。
        #    我们既然自己画圆角面板，这层投影必须去掉。
        self.setGraphicsEffect(None)

    def setGraphicsEffect(self, effect):  # noqa: N802
        """拦截 **Python 侧**的图形效果安装（全部拒绝）。"""
        super().setGraphicsEffect(None)

    # -- 主题 / 菜单项 --------------------------------------------------------
    def apply_menu_theme(self):
        """（重新）套用菜单项样式。换肤时由 `refresh_glass_theme()` 调用。"""
        from . import theme as _theme

        self.setStyleSheet(_theme.menu_qss())

    def add_row(self, text: str, hint: str = "", on_click=None, enabled: bool = True,
                danger: bool = False) -> "MenuRow":
        """加一行**自绘菜单项**（左标题 + 右快捷键）。

        返回 MenuRow，调用方需要动态改文案时可以直接用。
        """
        from PySide6.QtWidgets import QWidgetAction

        row = MenuRow(text, hint, enabled=enabled, danger=danger, parent=self)
        if on_click is not None:
            row.clicked.connect(lambda: on_click())
        act = QWidgetAction(self)
        act.setDefaultWidget(row)
        self.addAction(act)
        row._owner_menu = self
        # ★ task-70：把行的 Enter/Leave/MouseMove 统一转给菜单重算"当前唯一 hover 行"，
        #    不依赖 Qt 在快速移动时是否漏发 leaveEvent。
        row.installEventFilter(self)
        self._rows.append(row)
        return row

    def exclusive_hover(self, row):
        """★ task-70：点亮 row、同时熄灭其它所有行；row=None 表示清空全部高亮。"""
        try:
            self._hover_row = row
            for r in list(self._rows):
                want = (r is row) and r.isEnabled() and r.isVisible()
                if bool(getattr(r, "_hover", False)) != want:
                    r._hover = want
                    r.update()
            # 防止 QMenu 自己的 currentAction/QSS 选中态残留成第二块高亮
            self.setActiveAction(None)
            self.update()
        except Exception:
            log.debug("菜单 hover 排他失败", exc_info=True)

    def _sync_hover_at(self, menu_pos):
        target = None
        try:
            for r in list(self._rows):
                if r.isVisible() and r.isEnabled() and r.geometry().contains(menu_pos):
                    target = r
                    break
        except Exception:
            target = None
        self.exclusive_hover(target)

    def eventFilter(self, obj, ev):  # noqa: N802
        # ★ task-70：以**真实光标位置**为准重算 hover（每帧最多一行高亮）。
        if obj in getattr(self, "_rows", ()):  # noqa: E501
            try:
                t = ev.type()
                if t in (QEvent.Enter, QEvent.Leave, QEvent.MouseMove):
                    pos = self.mapFromGlobal(QCursor.pos())
                    self._sync_hover_at(pos)
            except RuntimeError:
                pass
        return super().eventFilter(obj, ev)

    def hideEvent(self, ev):  # noqa: N802
        self.exclusive_hover(None)
        super().hideEvent(ev)

    def addMenu(self, title):  # noqa: N802
        """子菜单也用 `RoundedMenu` —— 否则它会退回系统默认的**方角**菜单。

        （用户："这个方角边框普遍存在软件内所有地方"，子菜单就是漏网的一处。）
        """
        sub = RoundedMenu(self)
        sub.setTitle(title)
        # ★ task-70：子菜单展开时清掉父菜单行高亮，保证屏幕任意时刻最多一行蓝色高亮。
        try:
            sub.aboutToShow.connect(lambda: self.exclusive_hover(None))
        except Exception:
            pass
        super().addMenu(sub)
        self._submenus.append(sub)
        return sub

    def refresh_rows(self):
        """换肤后让自绘行重画（它们的颜色是画的时候现取的，只要 update 一下）。"""
        for r in list(self._rows) + list(self._submenus):
            try:
                if isinstance(r, RoundedMenu):
                    r.apply_menu_theme()
                    r.refresh_rows()
                else:
                    r.update()
            except RuntimeError:
                pass

    def _kill_effect(self):
        """让 Qt 给弹出菜单自动挂的投影**画不出任何东西**。

        ⚠️⚠️ 踩坑全过程（都试过，都无效）：
          1. 在 `__init__` / `showEvent` / `paintEvent` 里 `setGraphicsEffect(None)`
             —— `setGraphicsEffect` 在 Qt 里**不是虚函数**，Qt 内部（C++）安装时
             绕过 Python 覆写，而且会在**绘制之后**重新挂上；
          2. 覆写 `setGraphicsEffect` 拒绝安装 —— 同上，C++ 不走 Python；
          3. `QTimer.singleShot(0, ...)` 延迟清 —— 清了又被挂回来；
          4. 把 `ch.setEnabled(False)` —— Qt 会重新启用。

        最终做法：**直接改那个 effect 自己的参数**，让它渲染出完全透明的东西：
        颜色 alpha=0、模糊半径=0、偏移=0。这样无论 Qt 怎么挂、怎么启用，
        它都画不出任何像素。**不依赖任何时序。**
        """
        try:
            super().setGraphicsEffect(None)
        except Exception:  # pragma: no cover
            pass
        self._neutralize_effects()

    def _neutralize_effects(self):
        """把子对象里所有投影效果调成"画不出东西"。"""
        from PySide6.QtGui import QColor

        try:
            for ch in self.children():
                if "DropShadowEffect" not in type(ch).__name__:
                    continue
                try:
                    ch.setEnabled(False)
                    ch.setBlurRadius(0.0)
                    ch.setOffset(0.0, 0.0)
                    ch.setColor(QColor(0, 0, 0, 0))
                except Exception:
                    pass
        except Exception:  # pragma: no cover
            pass

    def showEvent(self, ev):  # noqa: N802
        super().showEvent(ev)
        self._kill_effect()
        QTimer.singleShot(0, self._kill_effect)

    def paintEvent(self, ev):  # noqa: N802
        from PySide6.QtGui import QPainterPath, QPen

        # 每帧兜一次底（一次子对象遍历，开销可忽略）
        self._neutralize_effects()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, self.RADIUS, self.RADIUS)
        from . import theme as _theme
        glass = bool(_theme.is_glass_skin())
        if glass:
            # ★★ task-84：冰晶白玻璃菜单 —— 先垫**菜单正下方**的实时模糊纹理，
            # 再叠 rgba(255,255,255,0.55) 亮白琉璃 + 冷灰细边（深墨字清晰）。
            p.setPen(Qt.NoPen)
            for grow, alpha in ((3.0, 12), (2.0, 16), (1.0, 20)):
                p.setBrush(QColor(0, 0, 0, alpha))
                p.drawRoundedRect(r.adjusted(-grow, -grow, grow, grow),
                                  self.RADIUS + grow, self.RADIUS + grow)
            p.save()
            p.setClipPath(path)
            try:
                GlassBlurPlate.instance().paint_backdrop(p, self, r)
            except Exception:
                pass
            p.restore()
            p.fillPath(path, qt_color(_theme.GLASS_MENU_BG))
            p.setPen(QPen(qt_color(_theme.GLASS_PANEL_EDGE), 1.0))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        else:
            # ★ task-84（lead 裁决 3）：深/浅主题的 QMenu 同样垫**局部模糊**并微透 0.90~0.92
            p.save()
            p.setClipPath(path)
            try:
                GlassBlurPlate.instance().paint_backdrop(p, self, r)
            except Exception:
                pass
            p.restore()
            _mc = qt_color(P.get("popup_bg", P.get("glass_panel", "#FFFFFF")))
            _lo, _hi = int(round(0.90 * 255)), int(round(0.92 * 255))
            if _mc.alpha() > _hi:
                _mc.setAlpha(_hi)
            elif _mc.alpha() < _lo:
                _mc.setAlpha(_lo)
            p.fillPath(path, _mc)
            p.setPen(QPen(qt_color(P.get("divider", "#E5E6EB")), 1.0))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        p.end()
        # 菜单项 / 分隔线由 QMenu 自己画在上层（QSS 背景已设成透明，不会盖掉我们）
        super().paintEvent(ev)


# --------------------------------------------------------------------------------------
# task-71：二级模态 —— 全窗高斯模糊暗化背板
# --------------------------------------------------------------------------------------
class BlurBackdrop(QWidget):
    """SettingsPanel 打开时铺在舞台上的**模糊+暗化背板**（Blur Radius = 24px）。

    - 打开瞬间用 QScreen 抓当前舞台真实画面（含 QOpenGLWidget 视频，QWidget.grab 抓不到 GL）；
    - 用 QGraphicsBlurEffect(24) 在离屏 scene 里渲染成模糊图，paintEvent 再叠一层暗化；
    - 背板盖住整个舞台 ⇒ 底层舞台收不到鼠标/滚轮（点击只发 clicked 信号，由主窗 beep+回弹）；
    - 设置面板是 stage.settings_host 的子控件，打开后 raise 到背板之上，内部交互不受影响。
    """

    clicked = Signal()
    BLUR_RADIUS = 24.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingsBackdrop")
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.ArrowCursor)
        self._pix = None
        self._blur_radius = float(self.BLUR_RADIUS)
        self._source = "none"

    # -- 生成 ----------------------------------------------------------------
    def capture_stage(self, stage) -> dict:
        """抓 stage 当前真实画面并生成模糊图；返回给探针的可读证据。"""
        self.hide()
        from PySide6.QtCore import QPoint, QRectF, Qt as _Qt
        from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPixmap
        # ⚠️ PySide6 里 QGraphicsScene / QGraphicsPixmapItem / QGraphicsBlurEffect
        #    属于 QtWidgets（不是 QtGui），从 QtGui import 会 ImportError。
        from PySide6.QtWidgets import (QGraphicsBlurEffect, QGraphicsPixmapItem,
                                       QGraphicsScene)

        w = max(1, int(stage.width()))
        h = max(1, int(stage.height()))
        self.setGeometry(0, 0, w, h)
        g = stage.mapToGlobal(QPoint(0, 0))
        dpr = float(stage.devicePixelRatioF() or 1.0)
        pm = None
        try:
            screen = QGuiApplication.screenAt(g) or QGuiApplication.primaryScreen()
            full = screen.grabWindow(0)
            if not full.isNull():
                pm = full.copy(int(round(g.x() * dpr)), int(round(g.y() * dpr)),
                               max(1, int(round(w * dpr))), max(1, int(round(h * dpr))))
                self._source = "screen"
        except Exception:
            pm = None
        if pm is None or pm.isNull():
            pm = stage.grab()
            self._source = "widget-grab"
        try:
            pm = pm.scaled(w, h, _Qt.IgnoreAspectRatio, _Qt.SmoothTransformation)
        except Exception:
            pass
        # 离屏 scene + QGraphicsBlurEffect(24) 生成模糊位图
        out = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        out.fill(0)
        try:
            scene = QGraphicsScene()
            item = QGraphicsPixmapItem(QPixmap.fromImage(pm.toImage() if not isinstance(pm, QPixmap) else pm))
            eff = QGraphicsBlurEffect()
            eff.setBlurRadius(float(self._blur_radius))
            item.setGraphicsEffect(eff)
            scene.addItem(item)
            scene.setSceneRect(0, 0, w, h)
            p = QPainter(out)
            p.setRenderHint(QPainter.Antialiasing, True)
            scene.render(p, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
            p.end()
            self._pix = QPixmap.fromImage(out)
        except Exception:
            log.debug("生成模糊背板失败", exc_info=True)
            self._pix = QPixmap.fromImage(pm.toImage()) if not isinstance(pm, QPixmap) else pm.copy()
        self.update()
        return {"ok": self._pix is not None and not self._pix.isNull(),
                "size": [w, h], "blur_radius": self._blur_radius,
                "source": self._source, "device_pixel_ratio": dpr}

    def blur_radius(self) -> float:
        return float(self._blur_radius)

    # -- 绘制 / 事件 ----------------------------------------------------------
    def paintEvent(self, _ev):  # noqa: N802
        from PySide6.QtGui import QColor, QPainter

        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if self._pix is not None and not self._pix.isNull():
            p.drawPixmap(self.rect(), self._pix)
        # 暗化底衬：保证任何透明主题下设置面板文字高对比可读
        # （116 太浅：实测模糊后高频能量只降 ~46%；加深到 170 让底层彻底退后）
        p.fillRect(self.rect(), QColor(0, 0, 0, 170))
        p.end()

    def mousePressEvent(self, ev):  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        ev.accept()

    def mouseReleaseEvent(self, ev):  # noqa: N802
        ev.accept()

    def wheelEvent(self, ev):  # noqa: N802
        # ★ task-71：面板外滚轮严禁下传（不调音量/不滚歌词）—— 直接吞掉
        ev.accept()

    def contextMenuEvent(self, ev):  # noqa: N802
        ev.accept()


# --------------------------------------------------------------------------------------
# QQ 风格开关
# --------------------------------------------------------------------------------------
class ToggleSwitch(QAbstractButton):
    """QQ NT 那种胶囊开关（右侧滑块），替代 QCheckBox。

    ⚠️ 为什么要自己画：Qt 自带的 QCheckBox 勾选框在 Windows 上由
    原生主题画，**不受 QSS 控制**（背景、圆角、勾选色都改不了），
    而且和 QQ NT 的视觉语言完全不一样。
    自己画反而更简单、且严格跟主题走。
    """

    W, H = 40, 22

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(bool(checked))
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.W, self.H)
        self._t = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knobT", self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on: bool):
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def get_knobT(self) -> float:
        return self._t

    def set_knobT(self, v: float):
        self._t = float(v)
        self.update()

    knobT = Property(float, get_knobT, set_knobT)

    def sizeHint(self):  # noqa: N802
        return QSize(self.W, self.H)

    def paintEvent(self, _ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.H / 2.0
        rect = QRectF(0, 0, self.W, self.H)
        t = max(0.0, min(1.0, self._t))
        kr = r - 3.0
        kx = 3.0 + kr + (self.W - 6.0 - 2 * kr) * t
        from . import theme as _ts_theme
        if _ts_theme.is_glass_skin():
            # ★★ task-94②：**清透玻璃开关**（彻底告别死黑底）
            #    Off 轨道 rgba(145,155,170,0.35) / On 轨道 rgba(56,152,255,0.78)，
            #    1px 白色高光边（0.55 → 0.65），圆球纯白 + 微投影。
            off = QColor(145, 155, 170, int(round(0.35 * 255)))
            on = QColor(56, 152, 255, int(round(0.78 * 255)))
            col = QColor(
                round(off.red() + (on.red() - off.red()) * t),
                round(off.green() + (on.green() - off.green()) * t),
                round(off.blue() + (on.blue() - off.blue()) * t),
                round(off.alpha() + (on.alpha() - off.alpha()) * t),
            )
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(rect, r, r)
            pen = QPen(QColor(255, 255, 255, round((0.55 + 0.10 * t) * 255)))
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), r - 0.5, r - 0.5)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 40))          # 圆球微投影
            p.drawEllipse(QPointF(kx, r + 0.8), kr, kr)
            p.setBrush(QColor(255, 255, 255))        # 纯白圆球
            p.drawEllipse(QPointF(kx, r), kr, kr)
            p.end()
            return
        # 既有 7 套主题：原行为（逐位不变）
        p.setPen(Qt.NoPen)
        # 轨道：关=浅灰，开=品牌蓝（QQ NT 用同一个蓝）
        off = qt_color(P.get("glass_track", "#E5E6EB"))
        on = qt_color(P.get("accent", "#0080F5"))
        col = QColor(
            round(off.red() + (on.red() - off.red()) * t),
            round(off.green() + (on.green() - off.green()) * t),
            round(off.blue() + (on.blue() - off.blue()) * t),
        )
        p.setBrush(col)
        p.drawRoundedRect(rect, r, r)
        # 滑块：纯白圆
        kr = r - 3.0
        kx = 3.0 + kr + (self.W - 6.0 - 2 * kr) * t
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(kx, r), kr, kr)
        p.end()


# --------------------------------------------------------------------------------------
# 进度条悬停预览
# --------------------------------------------------------------------------------------
class PreviewPopup(QWidget):
    """鼠标悬停在进度条上时弹出的"该时间点画面"小窗。

    ⚠️ 用 `Qt.ToolTip` 窗口类型：它是平台层面的**瞬态浮层**，
    不参与任何布局、不抢焦点、不进任务栏 —— 和菜单同类。
    （主视口那条"不得独立悬浮顶层窗口"的红线针对的是**视频渲染**，
    不适用于这种瞬态提示。）
    """

    #: 缩略图自身的圆角半径（要比外框 8px 略小，才不会露出边框外的背景）
    IMG_RADIUS = 6.0

    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._img = None
        self._time = 0.0
        self._stale = False
        self._w, self._h = 200, 130
        self.hide()

    #: 时间文字变化时是否需要重绘（图像区域不动）
    def set_time(self, seconds: float):
        """**只更新时间文字**（主线程调用，零成本）。

        ⚠⚠ 这是解耦的关键：时间文字必须跟手，
        不能等后台抽帧 —— 用户原话“鼠标都到右边了，
        预览浮窗还被钉在左边老位置”。
        """
        if abs(seconds - self._time) < 0.05:
            return
        self._time = seconds
        # 只重绘底部那条文字带，不碰图片
        self.update(0, self._h - 24, self._w, 24)

    def set_placeholder(self):
        """没有可用图时显示一个带加载指示的微暗占位（骨架屏）。"""
        if self._img is None:
            self.update()

    def set_image(self, img, seconds: float):
        """图像到位后替换（可能来自邻近兜底帧）。**不改时间文字**。"""
        from PySide6.QtGui import QPixmap

        if img is None:
            return
        pm = QPixmap.fromImage(img)
        # ⚠️⚠️ 宽度必须是 `面板宽 - 2`（左右各留 1px）。
        #    旧代码是 `max(160, pm.width())` 却从 x=4 开始画 ——
        #    图片右边缘直接**冲出面板**（用户截图里顶部两个尖角就是这么来的）。
        target_w = min(240, max(158, pm.width()))
        pm = pm.scaledToWidth(target_w - 2, Qt.SmoothTransformation)
        if pm.cacheKey() == getattr(self._img, "cacheKey", lambda: -1)():
            return
        self._img = pm
        w = pm.width() + 2
        h = pm.height() + 24
        if (w, h) != (self._w, self._h):
            self._w, self._h = w, h
            self.setFixedSize(w, h)
        self._stale = abs(seconds - self._time) > 1.5
        self.update()

    def move_centered(self, global_x: int, top_global_y: int):
        """把预览窗水平居中到鼠标位置、底边贴着给定的 y。**纯几何，零成本。**"""
        x = global_x - self._w // 2
        y = top_global_y - self._h - 8
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def paintEvent(self, _ev):  # noqa: N802
        from PySide6.QtGui import QPainterPath, QPen

        from . import theme as _theme_pv

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 8, 8)
        if _theme_pv.is_glass_skin():
            # ★ task-78③：进度缩略图浮层 = 半透磨砂琉璃底 + 1px 白 .28 折射亮边
            p.fillPath(path, qt_color(_theme_pv.GLASS_MENU_BG))
            p.setPen(QPen(qt_color(_theme_pv.GLASS_PANEL_EDGE), 1.0))
        else:
            p.fillPath(path, qt_color(P.get("glass_panel", "#FFFFFF")))
            p.setPen(QPen(qt_color(P.get("divider", "#E5E6EB")), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        # ⚠️⚠️ 缩略图**必须 1px 内缩 + 圆角裁剪**：
        #    外框画笔本身占 1px，图片如果贴着外框的物理极限画，
        #    边角像素就会从画笔外面露出来（用户截图里顶部的尖角）。
        #    几何：图区 = QRectF(1, 1, 面板宽-2, 图高)，裁剪圆角比外框小 1px。
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        y = 1
        if self._img is not None:
            iw, ih = self._img.width(), self._img.height()
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(1, 1, iw, ih),
                                self.IMG_RADIUS, self.IMG_RADIUS)
            p.save()
            p.setClipPath(clip)
            p.drawPixmap(QRectF(1, 1, iw, ih), self._img,
                         QRectF(0, 0, iw, ih))
            if self._stale:
                # 邻近兜底帧：盖一层很淡的暗罩（同样裁在圆角内）
                p.fillRect(QRect(1, 1, iw, ih), QColor(0, 0, 0, 46))
            p.restore()
            y = ih + 1
        else:
            # 占位骨架：微暗底 + "载入中"
            ph = max(60, self._h - 24 - 2)
            p.fillRect(QRect(1, 1, self._w - 2, ph), QColor(0, 0, 0, 28))
            p.setPen(qt_color(P.get("text_muted", "#8A8A8A")))
            p.drawText(QRect(0, 1, self._w, ph), Qt.AlignCenter, "\u8f7d\u5165\u4e2d\u2026")
            p.setPen(qt_color(P.get("text", "#1D2129")))
            y = ph + 1
        f = QFont(self.font())
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(qt_color(P.get("text", "#1D2129")))
        p.drawText(QRectF(0, y, self._w, 20), Qt.AlignCenter, fmt_time(self._time))
        p.end()
