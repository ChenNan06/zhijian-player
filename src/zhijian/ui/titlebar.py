"""自定义标题栏。

几何取自对爱奇艺万能联播 5.4.1 的逆向测量：标题栏高 44px、左内边距 12、
logo 26x26、窗口按钮 44x32、按钮之间 0 间距；右侧还有功能图标区（用竖线分隔）。
现代化处理：底色改为通透冷灰而非死黑, 分隔线改成极细 hairline。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from . import icons
from .theme import M, P, css
from .widgets import IconButton, Spacer

# ★★★ task-110②：标题栏**恒定色**（废除"随视频内容采样"——视频一变就忽白忽灰忽黑）。
#   150% DPI 真机验证：播放画面大幅变化时，标题栏同点色值 5 帧色差≈0。
#   浅色玻璃：rgba(242,245,250,0.82) = #F2F5FAD1
#   深色玻璃：rgba(24,28,36,0.82)   = #181C24D1
TITLEBAR_LIGHT_RGBA = (242, 245, 250, int(round(0.82 * 255)))     # 0.82*255 = 209
TITLEBAR_DARK_RGBA = (24, 28, 36, int(round(0.82 * 255)))
# 底部 1px 柔和分割线（恒定，不随视频变化）
TITLEBAR_DIVIDER_LIGHT_RGBA = (0, 0, 0, 15)
TITLEBAR_DIVIDER_DARK_RGBA = (255, 255, 255, 20)


class TitleBar(QWidget):

    def on_theme_changed(self):
        """换肤回调（主题合规门禁要求）：本控件在 paintEvent 里直接读调色板，
        必须在这里重新取色并重绘，否则换肤后会留着旧主题的颜色。
        """
        try:
            self.update()
        except Exception:
            pass


    """标题栏。整条栏的空白区域由主窗口在 WM_NCHITTEST 里判为 HTCAPTION,
    所以这里**不需要**任何鼠标拖动代码。"""

    menu_clicked = Signal(object)   # 参数：触发它的那个按钮（菜单要贴着它弹）
    theme_toggled = Signal()        # 日夜一键切换
    playlist_toggled = Signal(bool)
    settings_clicked = Signal()
    minimize_clicked = Signal()
    maximize_clicked = Signal()
    close_clicked = Signal()
    double_clicked = Signal()

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(M["titlebar_h"])
        self.setProperty("win_drag", True)
        self.setAttribute(Qt.WA_StyledBackground, False)
        # ★ task-80①：换肤时禁止 Qt 用调色板刷原生底板；本控件 100% 自绘
        #    （paintEvent 只画透明底 + 分隔线，绝不调用 super().paintEvent()）。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(M["titlebar_pad_l"], 0, M["titlebar_pad_r"], 0)
        lay.setSpacing(M["title_gap"])

        # ---- 左：应用标识 + 下拉菜单 ----
        # ★ task-70（用户 ⑥/⑦/⑧）：左上角主菜单改**三圆点/汉堡**（不是播放列表图标），
        #    常态柔和深灰、Hover/展开时才强调色；右下角播放列表仍用带小三角的队列图标，
        #    两者不再撞衫。
        # ★★ task-92③：「...」按钮固定 32×32、中心圆 r=11.0
        #    （圆心=按钮中心 (16,16)，四周净空 5px）—— 展开菜单时蓝圈必须 360° 完整。
        self.logo = IconButton("more", self, size=32, icon_size=17,
                               tooltip="主菜单", hover_color=P["accent"],
                               checkable=True, stroke=2.0,
                               hover_shape="circle11", hover_radius=11.0)
        self.logo.clicked.connect(lambda: self.menu_clicked.emit(self.logo))
        # ★ task-70：删除历史上遗留的 _logo_pill 覆盖层 —— 它是 26×26 的裸 QWidget，
        #    叠在左上角 logo 上（geometry 0,0），childAt 命中它 ⇒ logo 收不到 Enter/Click
        #    （实测 hover 不亮、点不开菜单）。没有任何地方引用它，直接去掉。
        lay.addWidget(self.logo)
        lay.addSpacing(2)

        self.title_label = QLabel(title or "至简Player", self)
        self.title_label.setObjectName("TitleText")
        self.title_label.setProperty("win_drag", True)
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.title_label.setTextInteractionFlags(Qt.NoTextInteraction)
        lay.addWidget(self.title_label, 1)

        # ---- 右：功能按钮区 ----
        # ⚠️ 入口去重（用户反馈"一个页面太多重复按钮了，设置、播放列表什么的放了好几个"）：
        #    · **播放列表**：标题栏这里原来有一个，胶囊上有一个，右边缘手柄还有一个 = 三处。
        #      标题栏这个删掉 —— 胶囊上的是主入口，右边缘手柄是抽屉贴边的收起/展开affordance，
        #      两个各有明确用途，第三个纯属重复。
        #    · **设置**：标题栏保留（挨着窗口控制键，位置符合直觉），胶囊上那个删掉。
        self.btn_settings = IconButton("settings", self, tooltip="设置 (Ctrl+,)",
                                       hover_shape="circle11", hover_radius=11.0)
        self.btn_settings.clicked.connect(self.settings_clicked.emit)
        # 日夜一键切换：图标显示"点一下会变成什么" —— 深色时显示太阳、浅色时显示月亮。
        self.btn_theme = IconButton("sun", self, tooltip="切换深色 / 浅色",
                                    hover_shape="circle11", hover_radius=11.0)
        self.btn_theme.clicked.connect(self.theme_toggled.emit)
        # ★ task-70（用户 ⑥）：**删除右上角多余的「…」按钮**（与左上角主菜单重复）。
        #    为兼容 ipc/自动化里旧的 btn_menu 引用，保留同名属性指向左上角入口（不是新控件）。
        self.btn_menu = self.logo

        for b in (self.btn_theme, self.btn_settings):
            lay.addWidget(b)

        lay.addSpacing(4)
        sep = QWidget(self)
        sep.setFixedSize(1, 18)
        sep.setProperty("role", "vsep")
        # ⚠️ 普通 QWidget 要吃**应用级** QSS 的背景，必须显式开 WA_StyledBackground；
        #    以前这里写的是内联 setStyleSheet（那样 Qt 会自动开），换成 role 之后要补这一句。
        sep.setAttribute(Qt.WA_StyledBackground, True)
        lay.addWidget(sep)
        lay.addSpacing(4)

        # 窗口控制键：悬停只画内缩胶囊（inset 6px ⇒ 44×32 里实际 32×20），绝不铺满 rect
        self.btn_min = IconButton("min", self, size=M["title_btn_w"], radius=6,
                                  icon_size=16, tooltip="最小化",
                                  hover_shape="capsule", hover_inset=6)
        self.btn_max = IconButton("max", self, size=M["title_btn_w"], radius=6,
                                  icon_size=15, tooltip="最大化",
                                  hover_shape="capsule", hover_inset=6)
        self.btn_close = IconButton("close", self, size=M["title_btn_w"], radius=6,
                                    icon_size=16, tooltip="关闭", hover_color=P["danger"],
                                    hover_shape="capsule", hover_inset=6)
        for b, sig in ((self.btn_min, self.minimize_clicked),
                       (self.btn_max, self.maximize_clicked),
                       (self.btn_close, self.close_clicked)):
            b.setFixedHeight(M["title_btn_h"])
            b.clicked.connect(sig.emit)
            lay.addWidget(b)

        self.setMouseTracking(True)

        # ★★ task-92①：**标题栏 = 单一原子图层** —— 任何子按钮的 hover/checked/press
        #    状态变化都强制整条 TitleBar 重绘（子按钮绝不在不一致的帧缓冲上局部擦写）。
        for _b in (self.logo, self.btn_theme, self.btn_settings,
                   self.btn_min, self.btn_max, self.btn_close):
            try:
                _b._repaint_owner = self
            except Exception:
                pass

    # -- 对外 ----------------------------------------------------------------
    def set_title(self, text: str):
        self.title_label.setText(text or "至简Player")
        self.title_label.setToolTip(text or "")

    def set_light(self, light: bool):
        """按当前明暗换图标：深色时显示太阳（点一下变浅色），浅色时显示月亮。"""
        self.btn_theme.set_icon("moon" if light else "sun")
        self.btn_theme.setToolTip("切换到浅色（霜白液态玻璃）" if not light else "切换到深色")

    def set_menu_checked(self, on: bool):
        """★ task-70：主菜单展开时左上角按钮进入 Checked（强调色），关闭即恢复常态。"""
        try:
            self.logo.set_checked(bool(on))
        except Exception:
            pass
        self.update()               # ★ task-92：菜单开合也让整条标题栏重绘

    def set_playlist_checked(self, on: bool):
        """播放列表入口已经从标题栏移除（去重），这里保留成空实现以免调用方报错。"""
        return

    def set_maximized(self, on: bool):
        self.btn_max.set_icon("restore" if on else "max")
        self.btn_max.setToolTip("向下还原" if on else "最大化")

    def button_rects(self) -> dict:
        """给 WM_NCHITTEST 用：最大化按钮的矩形（窗口内逻辑坐标）。"""
        r = self.btn_max.geometry()
        top_left = self.btn_max.mapTo(self.window(), r.topLeft() - r.topLeft())
        return {
            "max": self.btn_max.geometry().translated(
                self.btn_max.mapTo(self.window(), self.btn_max.rect().topLeft())
            )
        }

    # ==================================================================================
    # ★★★ task-101：**原生硬件拖拽 / 边缘缩放**（startSystemMove / startSystemResize）
    # ----------------------------------------------------------------------------------
    # 目的：把拖动/缩放的几何计算交给 Windows DWM 硬件合成（系统模态循环），
    #      彻底摆脱 Python 逐帧 window.move() / 每帧同步取帧造成的掉帧卡顿。
    # 语义保护：按钮点击、双击最大化照旧；API 不可用 / 非 Windows 时回退旧 move() 路径。
    # ⚠️ 现状说明：A 链在 WM_NCHITTEST 里已把标题栏空白区判为 HTCAPTION ⇒ 系统会先一步
    #    进入原生拖拽（Qt 收不到 press 时下面的 startSystemMove 不会被触发，也不会与之冲突）。
    #    本方法保留并可直接调用（见 probe），一旦 A 链改回 HTCLIENT 即立刻生效。
    # ==================================================================================
    def _window_handle(self):
        try:
            w = self.window()
            return w.windowHandle() if w is not None else None
        except Exception:
            return None

    def _edge_at(self, pos) -> "Qt.Edges":
        """标题栏内命中的窗口边缘（仅上/左/右；下边缘在控制栏，不归这里）。"""
        b = int(M.get("resize_border", 6))
        e = Qt.Edges()
        if pos.x() <= b:
            e |= Qt.LeftEdge
        if pos.x() >= self.width() - b:
            e |= Qt.RightEdge
        if pos.y() <= b:
            e |= Qt.TopEdge
        return e

    def native_drag_start(self) -> bool:
        """尝试系统级拖拽；返回是否由系统接管。"""
        wh = self._window_handle()
        if wh is None:
            return False
        try:
            return bool(wh.startSystemMove())
        except Exception:
            return False

    def native_resize_start(self, edges) -> bool:
        wh = self._window_handle()
        if wh is None:
            return False
        try:
            return bool(wh.startSystemResize(edges))
        except Exception:
            return False

    # -- 回退路径（老 move()/resize()；API 不可用时仍不崩）----------------------
    def _begin_fallback_move(self, pos):
        try:
            self._fb_mode = "move"
            self._fb_press = pos
            self._fb_geo = self.window().geometry()
        except Exception:
            self._fb_mode = ""

    def _begin_fallback_resize(self, pos, edges):
        try:
            self._fb_mode = "resize"
            self._fb_press = pos
            self._fb_edges = edges
            self._fb_geo = self.window().geometry()
        except Exception:
            self._fb_mode = ""

    def mousePressEvent(self, ev):      # noqa: N802
        # ★★★ task-101 回归修复（lead ①）：**press 时不再立刻 startSystemMove()**。
        #    否则 Windows 的 caption 模态循环会抢走后续 MouseButtonDblClick 事件 ⇒
        #    双击最大化失效（实测真实双击 is_max:false）。改为：
        #    · press 只记录起点；边角仍按热区进入 resize（那是真拖拽语义）；
        #    · **move 超过系统拖拽阈值**才 startSystemMove（拖拽照旧跟手）；
        #    · 全程没超过阈值 = 单击；双击走 mouseDoubleClickEvent 最大化分支。
        if ev.button() == Qt.LeftButton:
            pos = ev.position().toPoint()
            edges = self._edge_at(pos)
            if edges:
                if not self.native_resize_start(edges):
                    self._begin_fallback_resize(pos, edges)
                ev.accept()
                return
            self._pending_press = pos
            self._pending_global = ev.globalPosition().toPoint()
            self._fb_mode = ""
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):       # noqa: N802
        # ★ task-101 修复：超过拖拽阈值才进入系统拖拽（单击/双击不受影响）
        pending = getattr(self, "_pending_press", None)
        if pending is not None and (ev.buttons() & Qt.LeftButton):
            try:
                from PySide6.QtWidgets import QApplication as _QA
                thr = int(_QA.startDragDistance())
            except Exception:
                thr = 6
            if (ev.position().toPoint() - pending).manhattanLength() >= thr:
                self._pending_press = None
                if not self.native_drag_start():
                    self._begin_fallback_move(pending)
                ev.accept()
                return
        mode = getattr(self, "_fb_mode", "")
        if mode and (ev.buttons() & Qt.LeftButton):
            try:
                win = self.window()
                delta = ev.position().toPoint() - self._fb_press
                g = self._fb_geo
                if mode == "move":
                    win.move(g.x() + delta.x(), g.y() + delta.y())
                else:
                    e = self._fb_edges
                    nx, ny, nw, nh = g.x(), g.y(), g.width(), g.height()
                    if e & Qt.LeftEdge:
                        nx = g.x() + delta.x(); nw = g.width() - delta.x()
                    if e & Qt.RightEdge:
                        nw = g.width() + delta.x()
                    if e & Qt.TopEdge:
                        ny = g.y() + delta.y(); nh = g.height() - delta.y()
                    win.setGeometry(nx, ny, max(320, nw), max(200, nh))
                ev.accept()
                return
            except Exception:
                pass
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):    # noqa: N802
        self._pending_press = None
        self._fb_mode = ""
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):  # noqa: N802
        # ★★★ task-101 修复（lead ①）：双击标题栏 = 最大化/还原，**绝不进入 system move**。
        #    优先直接调主窗口的 toggle_maximize()（A 链既有 API），同时仍发 double_clicked
        #    供外部接线；两者幂等（主窗口的 toggle 是自己翻状态）。
        if ev.button() == Qt.LeftButton:
            self._pending_press = None
            self._fb_mode = ""
            done = False
            try:
                win = self.window()
                fn = getattr(win, "toggle_maximize", None)
                if callable(fn):
                    fn()
                    done = True
            except Exception:
                done = False
            try:
                self.double_clicked.emit()
            except Exception:
                pass
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    # ==================================================================================
    # ★★★ **测试钩子（仅 QA / 自动化取证使用，不进生产路径）**
    #   生产运行期间本组方法**永远不会被调用**，产品逻辑（沉浸自动隐藏）不受影响。
    #   用法（控制台 eval）：
    #     w.titlebar.set_pinned(True)         # 钉住标题栏显示（opacity=1 + 停自动隐藏定时器）
    #     w.titlebar.set_pinned_for(30)       # 钉住 30 秒后自动解除
    #     w.titlebar.set_pinned(False)        # 解除：交还自动隐藏（wake + hide_timer 重新计时）
    #     w.titlebar.pinned_state()           # 只读：pinned/opacity/可见性/隐藏定时器状态
    #   幂等性：重复 True 不叠加副作用；False 后自动隐藏系统恢复原状
    #   （空闲 N 秒仍会淡出，与未钉住时一致）。
    # ==================================================================================
    def set_pinned(self, pinned: bool = True) -> dict:
        on = bool(pinned)
        self._pinned_flag = on
        try:
            win = self.window()
            f = getattr(win, "_top_fader", None) if win is not None else None
            if f is not None:
                try:
                    f._ensure()
                except Exception:
                    pass
                f._visible = on
                anim = getattr(f, "_anim", None)
                if anim is not None:
                    try:
                        anim.stop()
                    except Exception:
                        pass
                eff = getattr(f, "_eff", None)
                if eff is not None:
                    try:
                        eff.setOpacity(1.0 if on else 0.0)
                    except Exception:
                        pass
            if on:
                ht = getattr(win, "_hide_timer", None)
                if ht is not None:
                    ht.stop()
                self.show()
                try:
                    self.raise_()
                except Exception:
                    pass
                self.update()
            else:
                # ★ 解除钉住：**把显示权交还自动隐藏系统** ——
                #   先 wake 一次让标题栏回到"显示中"，再重启隐藏定时器，
                #   ⇒ 空闲 N 秒后仍按原逻辑淡出（与未钉住时行为一致）。
                try:
                    if f is not None:
                        f.wake()
                except Exception:
                    pass
                ht2 = getattr(win, "_hide_timer", None)
                if ht2 is not None:
                    try:
                        ht2.start()
                    except Exception:
                        pass
                self.update()
        except Exception:
            pass
        return self.pinned_state()

    def set_pinned_for(self, seconds: float = 30.0) -> dict:
        st = self.set_pinned(True)
        try:
            QTimer.singleShot(int(max(1.0, float(seconds)) * 1000),
                              lambda: self.set_pinned(False))
        except Exception:
            pass
        return st

    def pinned_state(self) -> dict:
        try:
            win = self.window()
            f = getattr(win, "_top_fader", None) if win is not None else None
            eff = getattr(f, "_eff", None)
            op = None
            if eff is not None:
                try:
                    op = round(float(eff.opacity()), 3)
                except Exception:
                    op = None
            ht = getattr(win, "_hide_timer", None)
            return {"pinned": bool(getattr(self, "_pinned_flag", False)),
                    "opacity": op,
                    "titlebar_visible": bool(self.isVisible()),
                    "hide_timer_active": bool(ht.isActive()) if ht is not None else None}
        except Exception:
            return {"pinned": False, "opacity": None,
                    "titlebar_visible": False, "hide_timer_active": None}

    def paintEvent(self, _ev):
        from PySide6.QtGui import QColor, QPainter

        from . import theme as _theme

        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        # ★★★ task-110②：标题栏**废除随视频内容采样/模糊**，改为**恒定半透玻璃色**。
        #    用户问题 2/3：标题栏在视频上方、并不遮挡视频，却采样视频顶部像素 ⇒
        #    视频一变标题栏就忽白忽灰忽黑。现在两套玻璃主题各一个固定色，
        #    播放任何画面（包括大幅明暗变化）都**绝对不闪**。
        #    ⚠️ task-92 的成果全部保留：子按钮强制整条重绘 / 常态零背景 /
        #       悬停只画 r=11 中心圆（见 widgets.IconButton hover_shape）。
        glassed = bool(_theme.is_glass_skin())
        if glassed:
            light = bool(_theme.is_light())
            fr, fg, fb, fa = TITLEBAR_LIGHT_RGBA if light else TITLEBAR_DARK_RGBA
            p.fillRect(self.rect(), QColor(fr, fg, fb, fa))
            # 底边 1px 柔和分割线（标题栏与画面之间唯一分隔；颜色同样恒定）
            if self.height() > 1:
                dr, dg, db, da = (TITLEBAR_DIVIDER_LIGHT_RGBA if light
                                  else TITLEBAR_DIVIDER_DARK_RGBA)
                p.fillRect(0, self.height() - 1, self.width(), 1, QColor(dr, dg, db, da))
            # 顶部 1px 镜面高光边（task-99 玻璃标识；取自调色板，换肤即时生效）
            try:
                _th = _theme.P.get("glass_top_highlight") or "#FFFFFF26"
                p.fillRect(0, 0, self.width(), 1, QColor(_theme.qt_color(_th)))
            except Exception:
                pass
        else:
            # 非玻璃皮肤（旧主题兼容）：保持调色板实底，本轮不变
            p.fillRect(self.rect(), QColor(_theme.qt_color(_theme.P["bg_base"])))
        p.end()
