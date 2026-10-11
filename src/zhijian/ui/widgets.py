"""通用控件：玻璃面、图标按钮、进度条、音量条、OSD 提示。

所有面板都通过 Qt 布局器组织, 全项目**没有**任何手工 setGeometry / y += 30 的坐标计算。
"""

from __future__ import annotations

import weakref

from PySide6.QtCore import (
    Property,
    QEasingCurve,
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
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
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

    def showPopup(self):                     # noqa: N802 (Qt 命名)
        round_popup(self)
        super().showPopup()

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
    """

from . import icons
from .theme import M, P, css, mix, qt_color

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
    return menu


def round_popup(combo):
    """让下拉框的**弹窗**也带圆角（设置页里"还有方角"就是它）。

    ⚠️ 只给 `QComboBox QAbstractItemView` 设 border-radius 是不够的：
       弹窗是**独立的顶层窗口**，外面还裹着一层 `QComboBoxPrivateContainer`，
       系统给它画的是方形边框 —— 里面那层圆角被方形外壳包着，看着就是方的。
       必须把弹窗设成 **无边框 + 半透明**，并把容器底色清掉，
       这样"圆角面板"才露得出来。道理与 `prep_popup()` 完全相同（见那里的注释）。

    ⚠️ 属性必须在 `setWindowFlags()` **之后**设：它会重建原生窗口，之前的属性会丢。
    """
    try:
        win = combo.view().window()
        win.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        win.setAttribute(Qt.WA_TranslucentBackground, True)
        win.setAttribute(Qt.WA_NoSystemBackground, True)
        # 只清容器自己的底，别碰里面的 view（圆角面板是它画的）
        win.setStyleSheet("QComboBoxPrivateContainer{background:transparent;border:none;}")
    except Exception:
        log.debug("下拉弹窗圆角设置失败", exc_info=True)
    return combo


def _key_of(value: str, fallback: str) -> str:
    """反查一个颜色值当前对应调色板里的哪个键。

    `fill=P["glass_capsule"]` 传进来的是**值**，这里反查出**键名**，
    换肤时才能按新调色板重新解析。查不到就退回默认键。
    """
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


def refresh_glass_theme() -> int:
    """换肤后让所有 GlassFrame 重新按调色板取色。

    返回刷新的控件数（诊断用）。
    """
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
                w.set_hover_color(P.get("text") or P["text"])
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


def _glass_bg(w: "GlassFrame") -> QPixmap:
    key = (w.width(), w.height(), w._radius, w._fill, w._border,
           round(float(w._shadow), 3), w._spread, w._layers,
           bool(w._inner_glow), round(w.devicePixelRatioF() or 1.0, 2))
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

    def paintEvent(self, _ev):
        # ⚠️ 背景（阴影 + 底色 + 内发光 + 描边）整体**缓存成一张 pixmap**。
        #    16 层弥散阴影每帧要画 16 个抗锯齿圆角矩形，实测胶囊一次重绘 3.08ms ——
        #    窗口拖动/缩放时整窗高频重绘，这 3ms 就是掉帧的直接来源。
        #    缓存的 key 里含尺寸与全部取色参数，换肤/改尺寸会自动失效。
        pm = _glass_bg(self)
        p = QPainter(self)
        p.drawPixmap(0, 0, pm)
        p.end()
        self._paint_content()

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
        if self._shadow > 0:
            dy = int(M.get("float_shadow_dy", 3))
            grow = 1.5
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, max(1, int(round(255 * self._shadow)))))
            rr = r.adjusted(-grow, -grow + dy, grow, grow + dy)
            p.drawRoundedRect(rr, self._radius + grow, self._radius + grow)
            p.setPen(Qt.NoPen)

        # 2) 通透底色
        p.setBrush(qt_color(self._fill))
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
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius)
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
        stroke: float = 1.7
    ):
        color = P["text_dim"] if color is None else color
        hover_color = P["text"] if hover_color is None else hover_color
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
        self._radius = radius if radius is not None else size // 2
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
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

    hoverT = Property(float, get_hover_t, set_hover_t)

    def set_icon(self, name: str):
        if name != self._name:
            self._name = name
            self.update()

    def set_color(self, color: str):
        self._color = color
        self.update()

    def set_hover_color(self, color: str):
        self._hover_color = color
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
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self._pressed = False
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
            if self.rect().contains(ev.position().toPoint()):
                if self._checkable:
                    self._checked = not self._checked
                    self.toggled.emit(self._checked)
                self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        t = self._hover_t
        active = self._checked
        if active and self._accent_checked:
            p.setPen(Qt.NoPen)
            fill = qt_color(P["accent_soft"])
            fill.setAlphaF(min(1.0, fill.alphaF() + 0.12))
            p.setBrush(fill)
            p.drawRoundedRect(r, self._radius, self._radius)
        if t > 0.01:
            p.setPen(Qt.NoPen)
            c = QColor(255, 255, 255)
            c.setAlphaF(0.055 + 0.06 * t)
            p.setBrush(c)
            p.drawRoundedRect(r, self._radius, self._radius)
        if self._pressed:
            p.setPen(Qt.NoPen)
            c = QColor(0, 0, 0)
            c.setAlphaF(0.18)
            p.setBrush(c)
            p.drawRoundedRect(r, self._radius, self._radius)

        if active and self._accent_checked:
            color = P["accent"]
        elif t > 0.5:
            color = self._hover_color
        else:
            color = self._color
        pm = icons.render_pixmap(self._name, color, self._icon_size, self.devicePixelRatioF() or 2.0, self._stroke)
        x = (self.width() - self._icon_size) / 2
        y = (self.height() - self._icon_size) / 2
        p.drawPixmap(QPointF(x, y), pm)
        p.end()


class TextIconButton(IconButton):
    """带文字的胶囊按钮（如「倍速」）。"""

    def __init__(self, text: str, parent=None, height: int = 26, pad: int = 12,
                 color: str | None = None, font_size: int = 12, checkable: bool = False,
                 checked: bool = False, tooltip: str = ""):
        # 同上：默认值在调用时解析，避免"导入时快照"
        color = P["text_dim"] if color is None else color
        self._text = text
        self._pad = pad
        self._font_size = font_size
        QWidget.__init__(self, parent)
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
        self.setFixedWidth(fm.horizontalAdvance(self._text) + self._pad * 2)

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
            c = QColor(255, 255, 255)
            c.setAlphaF(0.05 + 0.06 * self._hover_t)
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

    def set_chapters(self, chapters: list[tuple[float, str]]):
        self._chapters = chapters or []
        self.update()

    # -- 几何 ----------------------------------------------------------------
    def _track_rect(self) -> QRectF:
        h = M["progress_h"] + (M["progress_h_hover"] - M["progress_h"]) * self._hover_t
        pad = M["progress_handle"] / 2 + 1
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
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self._hover_x = -1.0
        self.hoverChanged.emit(-1.0)
        self._anim.stop()
        self._anim.setStartValue(self._hover_t)
        self._anim.setEndValue(0.0)
        self._anim.start()
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
            self.seekStarted.emit()
            self.seekPreview.emit(self._drag_value)
            self.update()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
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
                p.setBrush(qt_color(P["accent"]))
                p.drawRoundedRect(QRectF(tr.left(), tr.top(), pw, tr.height()), radius, radius)
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
            r = M["progress_handle"] / 2 * (0.7 + 0.3 * max(self._hover_t, 1.0 if self._dragging else 0.0))
            p.setPen(Qt.NoPen)
            # 圆点一律**纯白**（用户："你怎么把浅色模式的圆点改得那么丑，给我改回白色去"）
            p.setBrush(QColor(255, 255, 255))
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
    """细音量条，带喇叭图标 + 悬停数值。"""

    valueChanged = Signal(float)   # 0..100

    def __init__(self, parent=None, width: int = 90):
        super().__init__(parent)
        self.setFixedSize(width, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self._value = 100.0
        self._dragging = False
        self._hover = False
        self._hover_t = 0.0
        self._anim = QPropertyAnimation(self, b"hoverT", self)
        self._anim.setDuration(140)

    def get_hover_t(self) -> float:
        return self._hover_t

    def set_hover_t(self, v: float):
        self._hover_t = float(v)
        self.update()

    hoverT = Property(float, get_hover_t, set_hover_t)

    def value(self) -> float:
        return self._value

    def set_value(self, v: float):
        v = max(0.0, min(100.0, float(v)))
        if abs(v - self._value) > 0.01:
            self._value = v
            self.update()

    DOT_R = 5.0

    def _track(self) -> QRectF:
        """音量轨道。

        ⚠️ 左右各内缩一个圆点半径：否则音量拉到 100% 时圆点圆心正好落在控件右边界上,
        右半个圆被裁掉（用户实测反馈"音量条拖到最右边以后小圆点被截断"）。
        """
        h = 3.0 + 3.0 * self._hover_t
        pad = self.DOT_R + 1.0
        return QRectF(pad, (self.height() - h) / 2, max(1.0, self.width() - 2 * pad), h)

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
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self._anim.stop()
        self._anim.setStartValue(self._hover_t)
        self._anim.setEndValue(0.0)
        self._anim.start()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._dragging = True
            self._apply(ev.position().x())
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._dragging:
            self._apply(ev.position().x())
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._dragging = False
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
            p.setBrush(qt_color(P["accent"]))
            p.drawRoundedRect(QRectF(tr.left(), tr.top(), w, tr.height()), r, r)
        if self._hover or self._dragging:
            x = tr.left() + w
            p.setBrush(qt_color(P["text"]))
            p.drawEllipse(QPointF(x, tr.center().y()), self.DOT_R, self.DOT_R)
        p.end()


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
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):  # noqa: N802
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
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(4.0, 2.0, -4.0, -2.0)
        if self._hover and self.isEnabled():
            p.setPen(Qt.NoPen)
            p.setBrush(qt_color(P["accent_soft"]))
            p.drawRoundedRect(r, 8.0, 8.0)
        on = self.isEnabled()
        title_col = P["accent"] if (self._hover and on and not self._danger) else (
            P["danger"] if self._danger else P["text"])
        if not on:
            title_col = P["text_muted"]
        p.setFont(self._title_font())
        p.setPen(qt_color(title_col))
        tr = QRectF(r.left() + self.PAD_L - 4, r.top(), r.width() - self.PAD_L - self.PAD_R + 8,
                    r.height())
        p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter, self._text)
        if self._hint:
            p.setFont(self._hint_font())
            p.setPen(qt_color(P["text_muted"] if on else P["text_muted"]))
            hr = QRectF(r.right() - self.PAD_R - 120, r.top(), 120.0, r.height())
            p.drawText(hr, Qt.AlignRight | Qt.AlignVCenter, self._hint)
        p.end()


class RoundedMenu(QMenu):
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
        prep_popup(self)
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
        self._rows.append(row)
        return row

    def addMenu(self, title):  # noqa: N802
        """子菜单也用 `RoundedMenu` —— 否则它会退回系统默认的**方角**菜单。

        （用户："这个方角边框普遍存在软件内所有地方"，子菜单就是漏网的一处。）
        """
        sub = RoundedMenu(self)
        sub.setTitle(title)
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
        # 面板底（跟主题走）。⚠️ 必须用**不透明**的 popup_bg：
        # 菜单窗口是"无边框+半透明"的，底色再半透明就会把背后内容透上来。
        p.fillPath(path, qt_color(P.get("popup_bg", P.get("glass_panel", "#FFFFFF"))))
        # 1px 干净描边
        p.setPen(QPen(qt_color(P.get("divider", "#E5E6EB")), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()
        # 菜单项 / 分隔线由 QMenu 自己画在上层（QSS 背景已设成透明，不会盖掉我们）
        super().paintEvent(ev)


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
        p.setPen(Qt.NoPen)
        r = self.H / 2.0
        rect = QRectF(0, 0, self.W, self.H)
        # 轨道：关=浅灰，开=品牌蓝（QQ NT 用同一个蓝）
        off = qt_color(P.get("glass_track", "#E5E6EB"))
        on = qt_color(P.get("accent", "#0080F5"))
        t = max(0.0, min(1.0, self._t))
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

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 8, 8)
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
