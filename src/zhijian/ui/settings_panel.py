"""设置面板（主窗口内的浮层, 不是独立顶层窗口）。

逆向依据（爱奇艺 5.4.1 设置窗口 854x685 @1299x883）：左侧分类栏宽 153（占 18%）、
右侧内容区左右内边距 25、行高 40、分组标题 15px。
本实现按同一比例放大到 880x660，并全部使用 QFormLayout / QVBoxLayout，**零绝对坐标**。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (
    QGraphicsEffect,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import parse_speed_tiers
from . import icons
from .theme import M, P, css
from .widgets import (GlassFrame, IconButton, NoWheelComboBox, NoWheelDoubleSpinBox,
                      NoWheelSlider, NoWheelSpinBox, hline, round_popup, themed)

# ★★★ task-107③：**7 分类**（第一期；控形态暂不升级，只做归位）
#   general = 常规与外观（general + ui 两页内容合并）
#   subtitle = 字幕与声音（subtitle + audio 两页内容合并，键名保持 "subtitle" 兼容旧调用）
SECTIONS = [
    ("general", "常规与外观", "sliders"),
    ("playback", "视频与播放", "play"),
    ("music", "音乐与歌词", "music_note"),
    ("image", "图片查看", "palette"),
    ("subtitle", "字幕与声音", "subtitle"),
    ("hotkeys", "快捷键", "keyboard"),
    ("about", "关于", "info"),
]

# ★ task-26：「音乐与歌词」配置键与默认值（模块级 —— 控制栏启动自举时也要用）
MUSIC_KEYS = ("lyric.font_family", "lyric.trans_font_family", "lyric.font_px",
              "lyric.weight", "lyric.step_pad", "lyric.scroll_ms",
              "spectrum.enabled", "spectrum.peak_style", "spectrum.rhythm",
              "music.cascade")            # ★ task-55：宽屏律动音瀑（音游模式，默认关）
MUSIC_DEFAULTS = {"lyric.font_family": "system",
                  "lyric.trans_font_family": "follow",   # task-116③：译文字体（follow=跟随原词）
                  "lyric.font_px": 27,
                  "lyric.weight": "medium", "lyric.step_pad": 18,   # task-27：= LINE_GAP(18)
                  "lyric.scroll_ms": 320, "spectrum.enabled": True,
                  "spectrum.peak_style": "none",
                  # ★ task-33②：「频谱律动速度」三档（α/ω 映射见 audio_view.SPEC_RHYTHM_LEVELS）
                  "spectrum.rhythm": "standard",
                  # ★ task-55（用户 13）：宽屏律动音瀑 —— **默认关**（卡片①）
                  "music.cascade": False}

HOTKEY_LABELS = [
    ("play_pause", "播放 / 暂停"),
    ("seek_fwd_small", "快进 5 秒（长按 → 倍速）"),
    ("seek_back_small", "快退 5 秒（长按 ← 倍速）"),
    ("seek_fwd_shift", "快进 15 秒（按住连跳）"),
    ("seek_back_shift", "快退 15 秒（按住连跳）"),
    ("seek_fwd_large", "快进 30 秒"),
    ("seek_back_large", "快退 30 秒"),
    ("volume_up", "音量 +"),
    ("volume_down", "音量 −"),
    ("mute", "静音"),
    ("fullscreen", "全屏 / 退出全屏"),
    ("playlist", "播放列表抽屉"),
    ("subtitle_panel", "字幕设置浮层"),
    ("settings", "设置"),
    ("open_file", "打开文件"),
    ("open_folder", "打开文件夹"),
    ("screenshot", "截图"),
    ("media_info", "媒体信息（左上角浮层）"),
    ("speed_up", "加速"),
    ("speed_down", "减速"),
    ("speed_reset", "恢复正常速度"),
    ("prev_file", "上一个文件"),
    ("next_file", "下一个文件"),
    ("frame_back", "上一帧"),
    ("frame_fwd", "下一帧"),
    ("always_on_top", "窗口置顶"),
]


def _title(text: str) -> QLabel:
    """分组标题。

    ⚠️⚠️ **不要写内联 `setStyleSheet(f"color:{P['text']}")`**。
    内联样式是**构造时快照**，而设置面板的头部/导航是 `__init__` 里建的、
    不在 `_rebuild_pages()` 的重建范围内 —— 换肤后它们永远停在旧颜色
    （用户实测："黑底黑字 / 白底白字，根本看不清"）。
    改用 `role` 属性，颜色由**全局样式表**给，换肤自动生效。
    """
    lb = QLabel(text)
    lb.setProperty("role", "h1")
    f = QFont(lb.font())
    f.setPixelSize(14)
    f.setWeight(QFont.Medium)
    lb.setFont(f)
    return lb


def _sub(text: str) -> QLabel:
    """说明文字。同样交给全局 QSS（`role="sub"`），不写内联颜色。"""
    lb = QLabel(text)
    lb.setProperty("role", "sub")
    lb.setWordWrap(True)
    return lb


class KeyCaptureEdit(QLineEdit):
    """点击后进入"等待按键"状态；真正抓按键的是应用级事件过滤器
    （本控件永远拿不到键盘焦点时也能工作）。"""

    capturing = Signal(object)   # self
    captured = Signal(object, str)

    def __init__(self, action: str, slot: int = 0, parent=None):
        super().__init__(parent)
        self.action = action
        self.slot = int(slot)          # 0 = 主键，1 = 副键
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self._waiting = False
        self._current_text = ""
        # ⚠️ 这里的颜色取决于"是否正在等待按键"，不是纯主题色 ⇒ 不能改成 role，
        #    改成登记进换肤广播：换肤时按当前等待状态重放一次。
        themed(self, lambda w: w._apply_style())

    def set_waiting(self, on: bool):
        self._waiting = on
        self.setText("请按下新的按键…" if on else self._current_text)
        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet(f"color:{P['accent']};" if self._waiting else "")

    def set_current_text(self, text: str):
        self._current_text = text
        if not self._waiting:
            self.setText(text)

    def mousePressEvent(self, ev):
        self._waiting = True
        self.set_waiting(True)
        self.capturing.emit(self)
        super().mousePressEvent(ev)


log = logging.getLogger("zhijian.ui.settings")


class GlassButton(QPushButton):
    """★ task-84③：**矢量抗锯齿**玻璃按钮（废除 QSS border-radius 的锯齿/缺角）。

    卡片逐字要求：QPainter.Antialiasing + QPainterPath.addRoundedRect(
        QRectF(rect).adjusted(1, 1, -1, -1), 8, 8)
    玻璃皮肤：浅蓝琉璃底 rgba(64,158,255,0.28) + 1.5px 深蓝描边 + 深蓝黑文字；
    深/浅主题：沿用各自调色板（primary=accent 实底 / ghost=输入底色 + 细边）。
    """

    def __init__(self, text: str = "", role: str = "ghost", parent=None):
        super().__init__(text, parent)
        self._role = role
        self._hover = False
        self.setProperty("role", role)          # 兼容 widgets._RoleButtonAutosize（防截断）
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(36)
        self.setMinimumWidth(92)
        self.setFocusPolicy(Qt.NoFocus)

    def enterEvent(self, ev):                    # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):                    # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def paintEvent(self, _ev):                   # noqa: N802
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

        from . import theme as _theme

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # ★ 卡片逐字要求的矢量圆角：向内缩 1px，路径半径 8
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)
        glass = bool(_theme.is_glass_skin())
        if self._role == "primary":
            if glass:
                fill = _theme.qt_color(_theme.GLASS_ACCENT_SOFT)
                edge = _theme.qt_color(_theme.GLASS_ACCENT_EDGE)
                color = _theme.qt_color(_theme.GLASS_ACCENT_TEXT)
                width = 1.5
            else:
                fill = _theme.qt_color(_theme.P["accent"])
                edge = QColor(255, 255, 255, 40)
                color = _theme.qt_color(_theme.P["text_on_accent"])
                width = 0.0
        else:
            if glass:
                fill = _theme.qt_color(_theme.GLASS_GHOST_FILL)
                edge = _theme.qt_color(_theme.GLASS_PANEL_EDGE)
                color = _theme.qt_color(_theme.P["text"])
            else:
                fill = _theme.qt_color(_theme.P["input_bg"])
                edge = _theme.qt_color(_theme.P["hairline_strong"])
                color = _theme.qt_color(_theme.P["text_dim"])
            width = 1.0
        if self._hover:
            fill = QColor(fill)
            fill.setAlpha(min(255, fill.alpha() + 28))
        p.setPen(QPen(edge, width) if width > 0 else QPen(Qt.NoPen))
        p.setBrush(fill)
        p.drawPath(path)
        p.setPen(color)
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setWeight(QFont.Medium)
        if glass and self._role == "primary":
            f.setWeight(QFont.DemiBold if False else QFont.Medium)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, self.text())
        p.end()

    def sizeHint(self):                          # noqa: N802
        from PySide6.QtCore import QSize
        fm = self.fontMetrics()
        return QSize(max(92, fm.horizontalAdvance(self.text()) + 36), 36)


class ScrollFeatherEffect(QGraphicsEffect):
    """★ task-111②：滚动区上下 28px Alpha 羽化（真遮罩，复用 LyricsPanel 的技术）。

    做法：QGraphicsEffect 会把**整个控件（含子控件）**渲染进离屏 pixmap，
    我们在 draw() 里取 sourcePixmap（Qt 自动处理 devicePixelRatio），
    再对缓冲做 CompositionMode_DestinationIn 的垂直 alpha 渐变 ——
    「顶部 0→255、中部 255、底部 255→0」，文字**不做任何缩放** ⇒ 仍然锐利。
    """

    def __init__(self, top: int = 28, bottom: int = 28, parent=None):
        super().__init__(parent)
        self._top = max(0, int(top))
        self._bottom = max(0, int(bottom))

    def set_edges(self, top: int, bottom: int) -> None:
        self._top, self._bottom = max(0, int(top)), max(0, int(bottom))
        self.update()

    def boundingRectFor(self, source: QRectF) -> QRectF:      # noqa: N802
        return source.adjusted(-1, -1, 1, 1)

    def draw(self, painter: QPainter) -> None:                 # noqa: N802
        off = QPoint()
        try:
            src = self.sourcePixmap(Qt.LogicalCoordinates, off)
        except Exception:
            src = None
        if src is None or src.isNull() or self._top + self._bottom <= 0:
            return
        dpr = float(src.devicePixelRatio() or 1.0)
        logical_w = src.width() / dpr
        logical_h = src.height() / dpr
        # ⚠️ 渐变高度必须用**控件自身**的高度：sourcePixmap 的高度可能大于可见区
        #   （QListWidget 等）⇒ 底部那一段会落到视野外、看不到淡出。实测抽屉底部不淡就是这个原因。
        try:
            _sw = self.source()
            if _sw is not None and _sw.height() > 0:
                logical_h = float(_sw.height())
                logical_w = float(_sw.width())
        except Exception:
            pass
        buf = QPixmap(src.size())
        buf.setDevicePixelRatio(dpr)
        buf.fill(Qt.transparent)
        p = QPainter(buf)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.drawPixmap(0, 0, src)
            t = min(0.49, self._top / max(1.0, logical_h))
            b = max(0.51, 1.0 - self._bottom / max(1.0, logical_h))
            grad = QLinearGradient(0.0, 0.0, 0.0, logical_h)
            grad.setColorAt(0.0, QColor(0, 0, 0, 0))
            grad.setColorAt(t, QColor(0, 0, 0, 255))
            grad.setColorAt(b, QColor(0, 0, 0, 255))
            grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            p.fillRect(QRectF(0.0, 0.0, logical_w, logical_h), grad)
        finally:
            p.end()
        painter.drawPixmap(off, buf)


def make_transparent(w: QWidget, tag: str = "") -> None:
    """★ task-111①：强制容器透明（真因：默认 QPalette.Window 把滚动容器刷成实心纯白）。"""
    if w is None:
        return
    try:
        w.setAttribute(Qt.WA_TranslucentBackground, True)
        w.setAutoFillBackground(False)
    except Exception:
        pass
    try:
        name = tag or w.objectName() or "zjTransparent"
        if w.objectName() != name:
            w.setObjectName(name)
        w.setStyleSheet("#%s { background: transparent; border: none; }" % name)
    except Exception:
        pass

class SettingsPanel(GlassFrame):
    """一体化设置浮层：左分类 + 右内容 + 底按钮, 全部流式布局。"""

    changed = Signal(str, object)        # key, value
    applied = Signal(str)                # 点了「应用」/「确定」后的提示文案（主窗口拿去弹 OSD）
    hotkey_changed = Signal(str, str, int)      # action, spec, slot（0=主键 1=副键）
    closed = Signal()
    reset_requested = Signal()
    theme_requested = Signal(str)        # 换肤：主题名
    setting_changed = Signal(str, object)  # 任一设置项变化（用于"即时生效"）
    drag_moved = Signal()                  # ★ task-76：用户拖动过面板（主窗口记标记）

    # =====================================================================
    # ★★ task-76（用户 7）：**面板可鼠标拖动**（窗口内边界约束）
    # ---------------------------------------------------------------------
    # 交互口径：按住面板**顶部标题带**（左侧「设置」标题行 / 右侧页标题行）或面板空白处
    # 即可拖动；从滑条/列表/按钮上按下则**不拖**（照常操作控件 ✓）。
    # 边界：面板始终被夹在父容器（铺满舞台的挂载点）内 ⇒ 不会拖出主窗口 ✓。
    DRAG_BAND_H = 46        # 顶部可拖拽带高度（标题那一行）

    def _drag_allowed_at(self, pos) -> bool:
        """只有**顶部标题带**才允许起拖（避免抢走滑条/列表的手势）。"""
        return 0 <= int(pos.y()) <= int(self.DRAG_BAND_H)

    def mousePressEvent(self, ev):      # noqa: N802
        try:
            if ev.button() == Qt.LeftButton and self._drag_allowed_at(ev.position()):
                self._drag_off = ev.position().toPoint()
                self._dragging = True
                self.setCursor(Qt.ClosedHandCursor)
                ev.accept()
                return
        except Exception:
            pass
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):       # noqa: N802
        if not getattr(self, "_dragging", False):
            return super().mouseMoveEvent(ev)
        try:
            parent = self.parentWidget()
            gp = ev.globalPosition().toPoint()
            npos = parent.mapFromGlobal(gp) - self._drag_off
            self._drag_to(int(npos.x()), int(npos.y()))
            # 通知主窗口：面板已被用户拖动过（关闭再开不要强制回中）
            try:
                self.drag_moved.emit()
            except Exception:
                pass
            ev.accept()
        except Exception:
            super().mouseMoveEvent(ev)

    def _drag_to(self, x: int, y: int) -> list:
        """按父容器**边界夹取**后移动面板（mouseMove 与测试钩子共用同一路径）。"""
        parent = self.parentWidget()
        max_x = max(0, int(parent.width()) - int(self.width())) if parent else 0
        max_y = max(0, int(parent.height()) - int(self.height())) if parent else 0
        nx = min(max(0, int(x)), max_x)
        ny = min(max(0, int(y)), max_y)
        self.move(nx, ny)
        return [nx, ny]

    def drag_to(self, x: int, y: int) -> dict:
        """程序化拖到 (x,y)：与鼠标拖动走**同一夹取路径**（QA 判据 d 的确定性入口）。"""
        self._drag_to(x, y)
        try:
            self.drag_moved.emit()
        except Exception:
            pass
        return self.drag_state()

    def mouseReleaseEvent(self, ev):    # noqa: N802
        if getattr(self, "_dragging", False):
            self._dragging = False
            self.unsetCursor()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def drag_state(self) -> dict:
        """★ task-76 锚点：拖动态/面板几何/父容器尺寸（QA 判据 d 直接读）。"""
        parent = self.parentWidget()
        return {"dragging": bool(getattr(self, "_dragging", False)),
                "pos": [int(self.x()), int(self.y())],
                "size": [int(self.width()), int(self.height())],
                "parent_size": ([int(parent.width()), int(parent.height())] if parent else None),
                "in_bounds": bool(parent is not None and self.x() >= 0 and self.y() >= 0
                                  and self.x() + self.width() <= parent.width()
                                  and self.y() + self.height() <= parent.height()),
                "drag_band_h": int(self.DRAG_BAND_H)}

    def mousePressEvent_legacy(self, ev):   # noqa: D401
        super().mousePressEvent(ev)
    def __init__(self, cfg, parent=None):
        super().__init__(
            parent,
            radius=M["radius_lg"],
            fill=P["glass_panel"],
            border=P["hairline_strong"],
            shadow=0.0,
            shadow_spread=34,
            pad=(0, 0, 0, 0),
        )
        self.setObjectName("SettingsPanel")
        self.cfg = cfg
        # ★ task-76：拖动状态
        self._drag_off = None
        self._dragging = False
        self.set_body_size(M["settings_w"], M["settings_h"])
        self.setProperty("settings_panel", True)

        body = QHBoxLayout()
        body.setContentsMargins(6, 6, 6, 6)
        body.setSpacing(0)

        # ---------------- 左：分类 ----------------
        left = QWidget(self)
        left.setFixedWidth(M["settings_side_w"])
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 8, 8)
        lv.setSpacing(6)
        head = QHBoxLayout()
        head.setContentsMargins(6, 2, 0, 0)
        lb = _title("设置")
        head.addWidget(lb)
        head.addStretch(1)
        lv.addLayout(head)
        self.nav = QListWidget(left)
        self.nav.setObjectName("SettingsNav")
        self.nav.setFrameShape(QFrame.NoFrame)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # ⚠️ 必须显式设 iconSize：QSS 里给 ::item 设了 height/padding 之后,
        #    QStyleSheetStyle 会把 decoration 矩形算得很小, 图标会被裁成"只剩左上角"。
        self.nav.setIconSize(QSize(18, 18))
        for key, label, icon in SECTIONS:
            it = QListWidgetItem(icons.get_icon(icon, P["text_dim"], 18), label)
            it.setData(Qt.UserRole, key)
            self.nav.addItem(it)
        self.nav.currentRowChanged.connect(self._on_nav)
        lv.addWidget(self.nav, 1)
        body.addWidget(left)

        # ---------------- 右：内容 ----------------
        right = QWidget(self)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(M["settings_pad"], 12, 12, 0)
        self.page_title = _title(SECTIONS[0][1])
        top.addWidget(self.page_title)
        top.addStretch(1)
        btn_close = IconButton("close", right, size=28, icon_size=15, tooltip="关闭 (Esc)")
        btn_close.clicked.connect(self._close)
        top.addWidget(btn_close)
        rv.addLayout(top)
        rv.addSpacing(6)

        self.stack = QStackedWidget(right)
        self._build_stack_pages()
        rv.addWidget(self.stack, 1)
        # ★★★ task-107①：**删除底部「确定 / 应用 / 取消」按钮栏**
        #    （设置项全部即时生效；写盘由 _save_timer 300ms 防抖完成）
        #    保留同名属性为 None，避免外部旧引用报 AttributeError。
        self.btn_reset = None
        self.btn_ok = None
        self.btn_apply = None
        self.btn_cancel = None
        body.addWidget(right, 1)

        self.body_layout().addLayout(body)
        self.nav.setCurrentRow(0)
        self._dirty: dict = {}
        # ★★ task-107①：**写盘防抖** —— 拖一次滑条只在脱手后写 1 次磁盘
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._flush_save)
        self._save_count = 0
        # 「应用」按钮已删除，该计时器仅保留兼容（回调内有 None 保护）
        self._apply_flash = QTimer(self)
        self._apply_flash.setSingleShot(True)
        self._apply_flash.timeout.connect(self._restore_apply_btn)
        # ★ task-26②：**启动即施加**已持久化的音乐设置（面板是懒创建的、未必被打开过，
        #   所以不能只靠 showEvent）。用一次性 singleShot(0) 推迟到事件循环（非常驻定时器）。
        QTimer.singleShot(0, self.apply_all_music_settings)

    # ------------------------------------------------------------------ 页面
    @staticmethod
    def _scroll(inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(inner)
        # ★ task-111①：滚动区/视口/内层容器全部透明（否则 QPalette.Window 刷白 ⇒ 用户看到的"整块纯白"）
        make_transparent(sa, "zjScroll")
        try:
            make_transparent(sa.viewport(), "zjScrollVp")
        except Exception:
            pass
        make_transparent(inner, "zjScrollInner")
        # ★ task-111②：上下各 28px Alpha 羽化（DestinationIn 真遮罩，DPR 由 QGraphicsEffect 保证）
        try:
            eff = ScrollFeatherEffect(28, 28, sa)
            sa.setGraphicsEffect(eff)
            sa._zj_feather = eff
        except Exception:
            log.debug("滚动区羽化挂载失败", exc_info=True)
        return sa

    def _form_host(self) -> tuple[QWidget, QVBoxLayout]:
        host = QWidget()
        make_transparent(host, "zjFormHost")
        lay = QVBoxLayout(host)
        lay.setContentsMargins(M["settings_pad"], 4, M["settings_pad"], 18)
        lay.setSpacing(24)   # 分组之间 24px（用户规范：靠间距 + 细线分隔，不套卡片）
        return host, lay

    def _card(self, title: str, form: QFormLayout) -> QWidget:
        """把一个设置分组包进**圆角卡片**（QQ NT 风格）。

        用户覄三轮的东西：右侧内容区底色是淡灰，每个分组必须是一张
        带 1px 细边框的圆角卡片，而不是直接散在白板上。
        卡片色跟主题走：浅色下是白卡片比底**亮**，
        深色下是 #3B3B3B 卡片比 #262626 底**亮** —— 这正是 QQ NT 的层次做法。
        卡片**不带任何阴影**，只有 1px 分割线。
        """
        from PySide6.QtWidgets import QFrame

        # ⚠️ 用户明确要求**撕掉厚卡片**："设置面板右侧不要再套一层层实心的白色/灰色大方框，
        #    直接依附在通透的毛玻璃大底板上，模块之间仅靠自然垂直间距(24px) +
        #    1px 极细浅灰分割线(#E5E6EB)分隔"。
        #    所以这里不再画任何底/边框，只负责"标题 + 表单 + 一条细分割线"。
        box = QWidget()
        make_transparent(box, "zjCard")
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        if title:
            v.addWidget(_title(title))
        v.addLayout(form)
        v.addSpacing(6)
        v.addWidget(hline())
        return box

    @staticmethod
    def _form() -> QFormLayout:
        f = QFormLayout()
        f.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        f.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        f.setHorizontalSpacing(18)
        f.setVerticalSpacing(10)
        f.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        f.setRowWrapPolicy(QFormLayout.DontWrapRows)
        return f

    def _check(self, key: str, text: str, default: bool = False) -> QWidget:
        """一行「标签 + 右侧 QQ 风格开关」。

        ⚠️ 原来是 `QCheckBox` —— Windows 上勾选框由**原生主题**绘制，QSS 改不动
        背景/圆角/勾选色，和 QQ NT 的视觉语言也完全不一样。改成自绘的
        `ToggleSwitch`（右侧胶囊开关），跟主题严格一致。
        """
        from .widgets import ToggleSwitch

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        lb = QLabel(text)
        lb.setProperty("role", "body")
        h.addWidget(lb, 1)
        sw = ToggleSwitch(bool(self.cfg.get(key, default)), row)
        sw.toggled.connect(lambda v, k=key: self._set(k, bool(v)))
        h.addWidget(sw, 0, Qt.AlignRight)
        row.setMinimumHeight(30)
        return row

    def _spin(self, key: str, lo: int, hi: int, step: int = 1, suffix: str = "") -> QSpinBox:
        sb = NoWheelSpinBox()
        sb.setRange(lo, hi)
        sb.setSingleStep(step)
        sb.setValue(int(self.cfg.get(key, lo)))
        if suffix:
            sb.setSuffix(suffix)
        sb.setFixedWidth(120)
        sb.setMinimumHeight(30)
        sb.valueChanged.connect(lambda v, k=key: self._set(k, int(v)))
        return sb

    def _dspin(self, key: str, lo: float, hi: float, step: float, suffix: str = "",
               decimals: int = 2) -> QDoubleSpinBox:
        sb = NoWheelDoubleSpinBox()
        sb.setRange(lo, hi)
        sb.setSingleStep(step)
        sb.setDecimals(decimals)
        sb.setValue(float(self.cfg.get(key, lo)))
        if suffix:
            sb.setSuffix(suffix)
        sb.setFixedWidth(120)
        sb.setMinimumHeight(30)
        sb.valueChanged.connect(lambda v, k=key: self._set(k, float(v)))
        return sb

    def _combo(self, key: str, options: list[tuple[str, str]], default: str = "") -> QComboBox:
        cb = NoWheelComboBox()
        # ★ 给自动化/QA 一个稳定锚点（按配置键定位控件，例如
        #   findChild(QComboBox, "cfg_ui_external_open_mode")）；对 UI 无可见影响。
        cb.setObjectName("cfg_" + str(key).replace(".", "_"))
        for value, label in options:
            cb.addItem(label, value)
        cur = self.cfg.get(key, default)
        idx = max(0, cb.findData(cur))
        cb.setCurrentIndex(idx)
        cb.setMinimumWidth(180)
        cb.setMinimumHeight(30)
        cb.currentIndexChanged.connect(lambda i, k=key, c=cb: self._set(k, c.itemData(i)))
        return cb

    def _slider_row(self, key: str, lo: int, hi: int, fmt=lambda v: str(v)) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        s = NoWheelSlider(Qt.Horizontal)
        s.setObjectName("cfg_" + str(key).replace(".", "_"))   # ★ 锚点：QA 可 findChild(QSlider, "cfg_ui_drawer_width")
        s.setRange(lo, hi)
        s.setValue(int(self.cfg.get(key, lo)))
        s.setMinimumWidth(220)
        val = QLabel(fmt(s.value()))
        val.setFixedWidth(58)
        val.setProperty("role", "sub")
        s.valueChanged.connect(lambda v, k=key, lb=val, f=fmt: (lb.setText(f(v)), self._set(k, int(v))))
        h.addWidget(s, 1)
        h.addWidget(val)
        return w

    # ---- 常规 ----
    def _page_general(self) -> QWidget:
        host, lay = self._form_host()
        f = self._form()
        f.addRow(self._check("ui.remember_window", "记忆窗口大小及位置"))
        f.addRow(self._check("ui.capsule_autohide", "播放时自动隐藏控制条"))
        f.addRow(self._check("ui.drawer_edge_hover", "鼠标靠近右边缘时呼出播放列表"))
        f.addRow(self._check("ui.animations", "允许开启界面动画"))
        f.addRow(self._check("ui.osd", "显示操作提示（OSD）"))
        # 悬停缩略图预览的开关（默认开）。关掉后**彻底停用后台 ffmpeg Worker**，
        # 悬停只显示时间文字与指针，不再弹缩略图。
        f.addRow(self._check("ui.preview_thumb", "启用进度条悬停缩略图预览"))
        f.addRow(self._check("ui.single_instance", "只允许运行一个播放器实例"))
        # ★ task-48（用户问题 5）：外部双击/命令行打开新文件时的行为
        f.addRow("从外部打开新文件时", self._combo("ui.external_open_mode", [
            ("switch", "在当前窗口切换播放（默认）"),
            ("new_window", "启动新窗口并存播放"),
        ], "switch"))
        f.addRow(_sub("在资源管理器双击另一个媒体文件（或命令行传文件）时的行为："
                      "默认复用当前窗口立即切歌；选「新窗口」则保留当前播放并另开一窗。"))
        lay.addWidget(self._card("", f))

        lay.addStretch(1)
        return self._scroll(host)

    # ---- 播放 ----
    def _page_playback(self) -> QWidget:
        host, lay = self._form_host()
        f = self._form()
        f.addRow("长按触发阈值", self._spin("playback.long_press_threshold_ms", 120, 1200, 10, " ms"))
        mode = self._combo("playback.long_press_mode", [
            ("multiply", "原倍速基础快进（默认）"),
            ("fixed", "固定档位快进"),
        ], "multiply")
        mode.setToolTip("原倍速基础快进：目标 = 当前倍速 × 倍率（2x 按住 ×2 → 4x）\n"
                        "固定档位快进：目标 = 第一个比当前倍速大的档（档位 2/5/10 时 2x→5x）")
        mult = self._dspin("playback.long_press_multiplier", 1.1, 8.0, 0.1, " 倍")
        mult.setToolTip("当前 2x、倍率 2 时，按住 → 4x；松开回到 2x。")
        # 档位输入：**一个像样的框**（跟其它输入同高同圆角）+ 右边实时显示解析结果，
        # 免得用户打完字不知道到底生效成了什么。
        tiers = QLineEdit(str(self.cfg.get("playback.long_press_tiers", "2, 5, 10")))
        tiers.setPlaceholderText("2, 5, 10")
        tiers.setToolTip("逗号分隔，例如 2, 5, 10。\n比「最高倍速」还大的档会被夹到上限。")
        tiers.setMinimumWidth(140)
        tier_preview = QLabel()
        tier_preview.setProperty("role", "sub")
        tier_preview.setWordWrap(False)
        tier_box = QWidget()
        tb = QHBoxLayout(tier_box)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(10)
        tb.addWidget(tiers)
        tb.addWidget(tier_preview, 1)

        def _refresh_tiers(text: str):
            parsed = parse_speed_tiers(text)
            smax = float(self.cfg.get("playback.speed_max", 16.0) or 16.0)
            clipped = any(t > smax + 1e-6 for t in parsed)
            tier_preview.setText("→ " + " · ".join(f"{t:g}x" for t in parsed)
                                 + ("（超出最高倍速，会被夹住）" if clipped else ""))

        def _on_tiers(text: str):
            self._set("playback.long_press_tiers", text)
            _refresh_tiers(text)

        tiers.textChanged.connect(_on_tiers)
        _refresh_tiers(tiers.text())
        f.addRow("长按模式", mode)
        f.addRow("原倍速倍率", mult)
        f.addRow("固定档位", tier_box)
        f.addRow(_sub("松开立即恢复原倍速。"))

        def _sync_boost_fields(*_a):
            """按模式灰掉用不上的那一项 —— 免得用户改了没生效以为坏了。"""
            fixed = str(self.cfg.get("playback.long_press_mode", "multiply")) == "fixed"
            mult.setEnabled(not fixed)
            tiers.setEnabled(fixed)
            tier_preview.setEnabled(fixed)

        mode.currentIndexChanged.connect(_sync_boost_fields)
        _sync_boost_fields()
        f.addRow(self._check("playback.remember_position", "记忆播放进度"))
        f.addRow(self._check("playback.auto_play", "打开后自动播放"))
        f.addRow("默认循环方式", self._combo("playback.loop_mode", [
            ("sequence", "顺序播放"), ("loop", "列表循环"),
            ("single", "单曲循环"), ("random", "随机播放"),
        ], "sequence"))
        lay.addWidget(self._card("播放与循环", f))

        f2 = self._form()
        f2.addRow("短跳步长", self._combo("playback.seek_step_small", [(3, "3 秒"), (5, "5 秒"), (10, "10 秒")], 5))
        f2.addRow("长跳步长", self._combo("playback.seek_step_large", [(15, "15 秒"), (30, "30 秒"), (60, "60 秒")], 30))
        f2.addRow("音量步长", self._combo("playback.volume_step", [(2, "2 %"), (5, "5 %"), (10, "10 %")], 5))
        f2.addRow("倍速步长", self._combo("playback.speed_step", [(0.1, "0.1x"), (0.25, "0.25x"), (0.5, "0.5x")], 0.25))
        f2.addRow("最高倍速", self._dspin("playback.speed_max", 2.0, 64.0, 1.0, " x"))
        f2.addRow(_sub("长按档位超过这个上限会被夹住。"))
        lay.addWidget(self._card("跳转与音量", f2))

        f3 = self._form()
        f3.addRow("硬件解码", self._combo("playback.hwdec", [
            ("auto-safe", "自动（推荐）"),
            ("nvdec", "NVIDIA NVDEC（零拷贝, 最快）"),
            ("d3d11va-copy", "D3D11VA 回拷（兼容性好）"),
            ("dxva2", "DXVA2（旧显卡）"),
            ("no", "纯软件解码"),
        ], "auto-safe"))
        lay.addWidget(self._card("解码与性能", f3))
        lay.addStretch(1)
        return self._scroll(host)

    # ---- 字幕 ----
    def _page_subtitle(self) -> QWidget:
        host, lay = self._form_host()
        f = self._form()
        f.addRow(self._check("subtitle.enabled", "显示字幕"))
        f.addRow(self._check("subtitle.auto_load", "自动挂载同目录同名外挂字幕 (.srt/.ass/.vtt)"))
        fam = NoWheelComboBox()
        fam.setEditable(True)
        # 内置字体已经通过 mpv 的 `sub-fonts-dir` 暴露给字幕渲染了（见 app.py），
        # 所以这里把它列进来是**真的能用**，不是死选项。
        fam.addItems(["Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun",
                      "HarmonyOS Sans SC", "Source Han Sans SC", "Noto Sans CJK SC",
                      "Arial", "Segoe UI"])
        fam.setCurrentText(str(self.cfg.get("subtitle.font_family", "Microsoft YaHei UI")))
        fam.setMinimumWidth(220)
        fam.setMinimumHeight(30)
        fam.currentTextChanged.connect(lambda t: self._set("subtitle.font_family", t))
        f.addRow("字体", fam)
        f.addRow("字号", self._slider_row("subtitle.font_size", 12, 100, lambda v: "%d px" % v))
        f.addRow("垂直位置", self._slider_row("subtitle.position", 0, 100, lambda v: "%d %%" % v))
        f.addRow("描边粗细", self._dspin("subtitle.border_size", 0.0, 8.0, 0.2, " px"))
        lay.addWidget(self._card("字幕", f))

        f2 = self._form()
        f2.addRow("文字颜色", self._color_edit("subtitle.text_color"))
        f2.addRow("描边颜色", self._color_edit("subtitle.border_color"))
        lay.addWidget(self._card("配色", f2))

        f4 = self._form()
        f4.addRow("ASS 特效字幕", self._combo("subtitle.ass_override", [
            ("scale", "随窗口缩放（推荐）"), ("yes", "完全使用用户样式"),
            ("force", "强制用户样式（忽略特效）"), ("no", "保持原样式"),
        ], "scale"))
        f4.addRow(self._check("subtitle.bold", "加粗"))
        f4.addRow(self._check("subtitle.italic", "斜体"))
        lay.addWidget(self._card("样式覆盖", f4))
        lay.addStretch(1)
        return self._scroll(host)

    def _color_edit(self, key: str) -> QWidget:
        """★ task-116④：颜色控件 = 色块（点开 QColorDialog）+ 十六进制输入（带合法性校验）。"""
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog

        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        btn = QToolButton()
        btn.setObjectName("colorSwatch_" + key.replace(".", "_"))
        btn.setFixedSize(28, 28)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("点击选择颜色")

        def repaint(color: str) -> None:
            from .theme import qt_color
            try:
                c = qt_color(color)
            except Exception:
                c = QColor("#FFFFFF")
            btn.setStyleSheet("background:%s;border:1px solid %s;border-radius:7px;"
                              % (c.name(), css(P["hairline_strong"])))

        ed = QLineEdit(str(self.cfg.get(key, "#FFFFFF")))
        ed.setObjectName("cfg_" + str(key).replace(".", "_"))   # ★ 锚点：QA 可 findChild(QLineEdit, "cfg_subtitle_text_color")
        ed.setProperty("zj_key", str(key))
        ed.setFixedWidth(110)
        ed.setMaxLength(9)

        def valid(text: str) -> str:
            t = (text or "").strip().upper()
            if not t.startswith("#") or len(t) not in (7, 9):
                return ""
            try:
                return QColor(t).name().upper() if QColor(t).isValid() else ""
            except Exception:
                return ""

        last_valid = [valid(ed.text()) or str(self.cfg.get(key, "#FFFFFF")) or "#FFFFFF"]

        def on_text(text: str) -> None:
            okc = valid(text)
            if okc:
                last_valid[0] = okc
            # ★ 红框 = **widget 级 inline QSS**（border:1px solid #FF5A5A），同时写三处可读锚点：
            #   ① ed.property("invalid") ② sw.property("color") ③ self._color_state[key]
            ed.setStyleSheet("" if okc else "border:1px solid #FF5A5A;border-radius:6px;")
            try:
                ed.setProperty("invalid", (not okc))
                ed.setProperty("color_hex", okc or "")
            except Exception:
                pass
            # ★ 非法输入**不动色块**：用上一个合法色（QA 抓过的"输入框红了、色块变白"）
            show = last_valid[0]
            repaint(show)
            try:
                btn.setProperty("color", show)
                btn.setToolTip("当前颜色 %s（点击选择）" % show)
            except Exception:
                pass
            self._color_state[key] = {"hex": show, "invalid": (not okc),
                                      "typed": okc or "", "text": str(text)}
            if okc:
                self._set(key, okc)

        def pick() -> None:
            try:
                cur = QColor(valid(ed.text()) or "#FFFFFF")
                c = QColorDialog.getColor(cur, self, "选择颜色")
                if c.isValid():
                    ed.setText(c.name().upper())
                    repaint(c.name().upper())
                    self._set(key, c.name().upper())
            except Exception:
                log.debug("取色失败", exc_info=True)

        btn.clicked.connect(pick)
        ed.textChanged.connect(on_text)
        repaint(ed.text())
        if not hasattr(self, "_color_state"):
            self._color_state = {}
        _v0 = valid(ed.text()) or str(self.cfg.get(key, "#FFFFFF")) or "#FFFFFF"
        self._color_state[key] = {"hex": _v0, "invalid": not bool(valid(ed.text())),
                                  "typed": valid(ed.text()), "text": ed.text()}
        try:
            ed.setProperty("invalid", not bool(valid(ed.text())))
            ed.setProperty("color_hex", valid(ed.text()))
            btn.setProperty("color", _v0)
        except Exception:
            pass
        h.addWidget(btn)
        h.addWidget(ed)
        h.addStretch(1)
        return w

    # ---- 界面 ----
    def _page_ui(self) -> QWidget:
        host, lay = self._form_host()
        f = self._form()
        f.addRow("外观主题", self._theme_segment())
        # ★ task-17：隐藏彩蛋皮肤（液态玻璃）的**唯一入口** —— 全量主题下拉。
        #    theme.theme_names() 过滤 hidden 项 ⇒ 它不会出现在任何全局主题列表里。
        f.addRow("播放列表宽度", self._slider_row(
            "ui.drawer_width", int(M["drawer_min_w"]), int(M["drawer_max_w"]),
            lambda v: "%d px" % v))
        f.addRow("控制条自动隐藏延时", self._slider_row("ui.capsule_autohide_ms", 800, 8000, lambda v: f"{v} ms"))
        lay.addWidget(self._card("外观", f))

        f3 = self._form()
        f3.addRow(self._check("ui.confirm_exit", "关闭窗口时二次确认"))
        lay.addWidget(self._card("窗口", f3))
        lay.addStretch(1)
        return self._scroll(host)

    # ---- 图片查看（task-107③：从 常规/界面 两页里归位到一起）----
    def _page_image(self) -> QWidget:
        host, lay = self._form_host()
        fi = self._form()
        fi.addRow("取景框位置", self._combo("image.minimap_position", [
            ("bottom_right", "右下角（默认，浮在控制栏上方）"),
            ("top_right", "右上角"),
        ], "bottom_right"))
        lay.addWidget(self._card("取景框", fi))

        f2 = self._form()
        f2.addRow("默认缩放", self._combo("ui.image_fit", [
            ("fit", "适应窗口"), ("one", "原始大小 100%"), ("free", "上次的缩放比例"),
        ], "fit"))
        f2.addRow("滚轮缩放步长", self._float_slider("ui.image_zoom_step", 105, 140, lambda v: "%.2fx" % (v / 100.0)))
        lay.addWidget(self._card("图片查看", f2))
        lay.addStretch(1)
        return self._scroll(host)

    #: 设置面板**可选主题清单（唯一定义处）**，含隐藏彩蛋皮肤。
    #  · 前两项渲染成「浅色 / 深色」快捷按钮 —— 用户："把界面里面的强调色给去掉，
    #    就只保留深浅色模式"（七个色板对播放器来说是多余的噪音）；
    #  · 登记项 hidden=True 的彩蛋皮肤（glass-light 液态玻璃）**不渲染按钮**，
    #    只出现在下面「主题」下拉里 ⇒ 卡片要求的"仅在设置面板的主题下拉列表出现"。
    #  ⚠️ 全局列表 theme.theme_names() 默认**过滤**隐藏项（标题栏日夜按钮等入口看不到它）；
    #     但 apply_theme("glass-light") 这条程序化路径仍然有效（它读 theme.THEMES）。
    THEME_MODES = (("qq-light", "浅色"), ("qq-dark", "深色"), ("glass-light", "液态玻璃"))

    def _accent_combo(self) -> QWidget:
        """外观模式：**浅色 / 深色** 两个快捷按钮（彩蛋皮肤不在这里，见 _theme_skin_combo）。

        用户反馈"把强调色去掉，只保留深浅色模式" —— 原来七个色板按钮是多余的。
        这里保留"点了真的会换肤"的行为，只是把可选项收敛成两个；
        隐藏彩蛋皮肤（glass-light）只在下面的「主题」下拉里出现。
        """
        from . import theme as _theme

        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self._theme_buttons: dict[str, QToolButton] = {}
        # ⚠️⚠️ 必须读**实时调色板**里的主题名，不能读 cfg。
        #    主界面的日夜切换按钮走的是 `apply_theme()`；如果 cfg 的写入时机
        #    和面板重建的时机有偏差，面板就会停在旧值上 ——
        #    用户原话"用日夜间模式切换按钮，状态不会同步到设置界面"。
        #    `P["_theme"]` 是 `apply_theme()` 亲手写的，永远是最新的。
        cur = str(_theme.P.get("_theme") or self.cfg.get("ui.theme", _theme.DEFAULT_THEME))
        # 当前若是别的主题名（历史配置）**或隐藏彩蛋皮肤**，按明暗归一到两个模式之一
        # （彩蛋皮肤不渲染按钮，所以按钮层只表达"明暗模式"这一个语义）
        if cur not in dict(self.THEME_MODES) or _theme.THEMES.get(cur, {}).get("hidden"):
            cur = "qq-light" if _theme.is_light() else "qq-dark"
        for name, label in self.THEME_MODES:
            if _theme.THEMES.get(name, {}).get("hidden"):
                continue                     # ★ 彩蛋皮肤不进按钮（只在下面的下拉里）
            b = QToolButton(w)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(label)
            b.setText(label)
            b.setFixedHeight(30)
            b.setMinimumWidth(84)
            b.setCheckable(True)
            b.setChecked(name == cur)
            b.setStyleSheet(
                "QToolButton{border-radius:8px;padding:0 16px;font-size:13px;"
                f"border:1px solid {css(_theme.P['hairline'])};"
                f"background:{css(_theme.P['input_bg'])};color:{_theme.P['text']};}}"
                f"QToolButton:checked{{border:1px solid {_theme.P['accent']};"
                f"color:{_theme.P['accent']};}}"
                f"QToolButton:hover{{background:{css(_theme.P['glass_hover'])};}}")
            b.clicked.connect(lambda _=False, n=name: self._pick_theme(n))
            h.addWidget(b)
            self._theme_buttons[name] = b
        h.addStretch(1)
        return w

    def _theme_segment(self) -> QWidget:
        """★ task-116②：**外观主题**唯一一行分段胶囊 [浅色玻璃 | 深色玻璃]。

        彻底移除 qq-light/qq-dark/aurora 等旧候选（theme_name 只在 glass-* 两个之间）。
        当前主题准确高亮；点击 ⇒ _pick_theme（写 cfg + theme_requested.emit ⇒ 主窗口应用）。
        """
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        cur = str(self.cfg.get("ui.theme", "glass-dark") or "glass-dark")
        try:
            from ..config import normalize_theme
            cur = normalize_theme(cur)
        except Exception:
            pass
        self._theme_buttons = {}
        opts = (("glass-light", "浅色玻璃"), ("glass-dark", "深色玻璃"))
        for i, (name, label) in enumerate(opts):
            b = QToolButton(w)
            b.setObjectName("themeSeg_" + name)
            b.setText(label)
            b.setCheckable(True)
            b.setAutoExclusive(False)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(name == cur)
            radius = ("border-top-left-radius:8px;border-bottom-left-radius:8px;"
                      if i == 0 else
                      "border-top-right-radius:8px;border-bottom-right-radius:8px;")
            b.setStyleSheet(
                "QToolButton{border:1px solid %s;background:transparent;color:%s;"
                "padding:5px 16px;font-size:12px;%s}"
                "QToolButton:checked{background:%s;color:%s;border-color:%s;}"
                % (css(P["hairline_strong"]), css(P["text"]),
                   radius,
                   css(P["accent"]), css(P["text_on_accent"]), css(P["accent"])))
            b.setProperty("selected", name == cur)
            b.setProperty("zj_theme", name)
            b.clicked.connect(lambda _=False, nm=name: self._pick_theme(nm))
            self._theme_buttons[name] = b
            h.addWidget(b)
            if i == 0:
                h.addSpacing(0)
        h.addStretch(1)
        self._theme_seg_widget = w
        return w

    def _theme_skin_combo(self) -> QComboBox:
        """「主题」下拉：**唯一枚举全量主题（含隐藏彩蛋皮肤）**的入口。

        ⚠️ 全局列表 `theme.theme_names()` 会过滤掉 `hidden` 的登记项，所以
           「液态玻璃」只在这里出现（卡片：隐藏彩蛋皮肤，只在设置面板下拉里）。
        ⚠️ 换肤链路**不做第二套**：这里只把选择交给 `_pick_theme()`
           → `theme_requested` → `MainWindow.apply_theme()`（既有唯一入口）。
        ⚠️ 弹窗走 `NoWheelComboBox` 自管的 SurfacePopup（不透明底 + 圆角，task-05 封版项），
           所以玻璃皮肤下它**不会**跟着变成半透明。
        """
        from . import theme as _theme

        cb = NoWheelComboBox()
        for name, label in self.THEME_MODES:
            txt = label
            if _theme.THEMES.get(name, {}).get("hidden"):
                txt = "%s（彩蛋）" % label      # 标记出来，用户知道这是隐藏皮肤
            cb.addItem(txt, name)
        cur = str(_theme.P.get("_theme") or self.cfg.get("ui.theme", _theme.DEFAULT_THEME))
        idx = cb.findData(cur)
        cb.setCurrentIndex(idx if idx >= 0 else 0)
        cb.setMinimumWidth(180)
        cb.setMinimumHeight(30)
        # ⚠️ 信号必须**在 setCurrentIndex 之后**连：否则每次重建面板都会立刻触发一次换肤
        cb.currentIndexChanged.connect(
            lambda i, c=cb: self._pick_theme(str(c.itemData(i) or "")))
        self._theme_combo = cb
        return cb

    def color_state(self, key: str = "") -> dict:
        """★ 锚点：颜色控件的可读状态（QA 验红框/色块同步用，不必弹模态）。

        返回 {key: {"hex": 合法值或空, "invalid": 是否非法, "text": 当前文本}}；
        传 key 只取该项。红框机制 = widget 级 inline QSS（border:1px solid #FF5A5A）。
        """
        st = dict(getattr(self, "_color_state", {}) or {})
        return st.get(key, {}) if key else st

    def theme_selected(self) -> str:
        """★ 锚点：当前外观主题（glass-light / glass-dark）—— QA 直接读这个，别看按钮状态。"""
        cur = str(self.cfg.get("ui.theme", "glass-dark") or "glass-dark")
        try:
            from ..config import normalize_theme
            cur = normalize_theme(cur)
        except Exception:
            pass
        return cur

    def _pick_theme(self, name: str):
        for n, b in getattr(self, "_theme_buttons", {}).items():
            b.setChecked(n == name)
            try:
                b.setProperty("selected", n == name)
                b.style().unpolish(b)
                b.style().polish(b)
            except Exception:
                pass
        # ★ task-17：下拉与快捷按钮是**同一个入口**的两个视图，必须互相同步
        #    （否则从下拉选了彩蛋皮肤、面板重建之前，下拉会停在旧项上）。
        cb = getattr(self, "_theme_combo", None)
        if cb is not None:
            try:
                i = cb.findData(name)
                if i >= 0 and cb.currentIndex() != i:
                    cb.blockSignals(True)
                    cb.setCurrentIndex(i)
                    cb.blockSignals(False)
            except RuntimeError:
                self._theme_combo = None     # 面板重建后 C++ 对象已销毁
        from . import theme as _theme

        if hasattr(self, "_theme_label"):
            self._theme_label.setText(_theme.THEMES.get(name, {}).get("label", ""))
        self.theme_requested.emit(name)

    def reload_theme(self):
        """换肤后**整块重建**面板内容。

        ⚠⚠ 为什么不能只刷新几个控件：
        这个面板里有**大量内联样式表**（QLabel / QCheckBox / QComboBox 等在
        构造时就把 `P[...]` 的值拼进了 `setStyleSheet`）。
        全局样式表刷新**管不到内联样式表**，于是就出现：
          • 浅色下面板里的标题/标签还是白字（看不见）；
          • 深色下上一个主题的强调色残留（实测那个亮黄色按钮）。
        靠逐个补刷是补不完的（漏一个就是一个白字）—— **整块重建是唯一可靠的做法**。
        """
        # ⚠️⚠️ **必须延迟到事件循环的下一轮**再重建。
        #    因为触发换肤的正是面板里的那个按钮：如果在这里同步重建，
        #    会把"正在处理点击事件的那个按钮"当场销毁 —— 信号链被中断，
        #    面板可能停在半重建状态（表现为标题字色没跟上主题、按钮残留旧强调色）。
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self._rebuild_safe)

    def _rebuild_safe(self):
        try:
            self._rebuild_pages()
        except Exception:
            log.debug("重建设置面板失败", exc_info=True)

    def _merge_pages(self, *builders) -> QWidget:
        """★ task-107③：把多个既有页面**合并成一页**（内容纵向排列、单一滚动条）。

        实现要点：每个 _page_*() 返回 QScrollArea；用 takeWidget() 取出里面的
        inner QWidget 直接放进新容器，**不再嵌套两层滚动条**。
        """
        from PySide6.QtWidgets import QVBoxLayout as _VBox
        host = QWidget()
        make_transparent(host, "zjMergeHost")
        lay = _VBox(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        for b in builders:
            sa = b()
            inner = None
            try:
                inner = sa.takeWidget()
            except Exception:
                inner = None
            if inner is not None:
                make_transparent(inner, "zjMergeInner")
                lay.addWidget(inner)
                sa.deleteLater()
            else:
                lay.addWidget(sa)
        return self._scroll(host)

    def _build_stack_pages(self):
        """按 SECTIONS 的 7 个分类装配 stack 页面（__init__ 与重建共用同一份）。"""
        self.stack.addWidget(self._merge_pages(self._page_general, self._page_ui))
        self.stack.addWidget(self._page_playback())
        self.stack.addWidget(self._page_music())
        self.stack.addWidget(self._page_image())
        self.stack.addWidget(self._merge_pages(self._page_subtitle, self._page_audio))
        self.stack.addWidget(self._page_hotkeys())
        self.stack.addWidget(self._page_about())
        self._transparent_sweep()

    def _transparent_sweep(self) -> int:
        """★ task-111①：把面板右侧**所有**容器/滚动区/视口/页面统一切透明。

        真因：task-107 的 _merge_pages() 新容器与 QScrollArea.viewport() 没设透明 ⇒
        被系统默认 QPalette.Window 刷成实心纯白（用户截图里那块白）。
        这里做全量兜底，避免以后新增页面又漏。
        """
        n = 0
        roots = [self.stack]
        try:
            roots += [self.stack.widget(i) for i in range(self.stack.count())]
        except Exception:
            pass
        try:
            roots += self.findChildren(QScrollArea)
        except Exception:
            pass
        for w in roots:
            if w is None:
                continue
            make_transparent(w, "zjSweep")
            n += 1
            try:
                if isinstance(w, QScrollArea):
                    make_transparent(w.viewport(), "zjSweepVp")
                    n += 1
                    inner = w.widget()
                    if inner is not None:
                        make_transparent(inner, "zjSweepInner")
                        n += 1
            except Exception:
                pass
        self._transparent_count = n
        return n

    def _rebuild_pages(self):
        """把右侧的页面全部重建一遍（带当前调色板）。"""
        from . import theme as _theme

        if not hasattr(self, "stack"):
            return
        cur = self.stack.currentIndex()
        while self.stack.count():
            w = self.stack.widget(0)
            self.stack.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        self._theme_buttons = {}
        self._theme_combo = None         # ★ task-17：下拉随页面一起重建（旧对象 deleteLater）
        self._build_stack_pages()
        self.stack.setCurrentIndex(max(0, min(cur, self.stack.count() - 1)))
        _ = _theme  # 保留引用，便于以后在这里做主题相关的微调

    # ---- 音乐与歌词（task-26，用户 ⑬）----
    def _page_music(self) -> QWidget:
        """★ task-116③：「音乐与歌词」拆三卡 —— 歌词排版 / 音频频谱 / 宽屏特效。"""
        host, lay = self._form_host()
        # ---- 卡 1：歌词排版 ----
        f = self._form()
        music_fams = [("system", "跟随系统（内置 HarmonyOS Sans SC）")]
        try:
            from PySide6.QtGui import QFontDatabase
            have = set(QFontDatabase.families())
            for name in ("HarmonyOS Sans SC", "MiSans", "Source Han Sans SC",
                         "思源黑体", "Microsoft YaHei UI"):
                if name in have:
                    music_fams.append((name, name))
        except Exception:
            pass
        self.music_family = self._combo("lyric.font_family", music_fams, "system")
        self.music_family.setObjectName("musicFontFamily")
        f.addRow("原词字体", self.music_family)
        # ★ task-116③ 新增：**译文字体**（与原文分开选；audio_view 侧待接）
        self.music_trans_family = self._combo("lyric.trans_font_family",
                                              [("follow", "跟随原词字体")] + music_fams[1:]
                                              if len(music_fams) > 1 else
                                              [("follow", "跟随原词字体"), ("system", "跟随系统")],
                                              "follow")
        self.music_trans_family.setObjectName("musicTransFontFamily")
        f.addRow("译文字体", self.music_trans_family)
        f.addRow("歌词字号（当前行）", self._slider_row_d("lyric.font_px", 16, 28, 27,
                                                          lambda v: "%dpx" % int(v)))
        self.music_weight = self._combo("lyric.weight",
                                        [("regular", "常规"),
                                         ("medium", "加粗（Medium）"),
                                         ("heavy", "特粗（Heavy）")], "medium")
        self.music_weight.setObjectName("musicWeight")
        f.addRow("歌词字重", self.music_weight)
        f.addRow("歌词行间距", self._slider_row_d("lyric.step_pad", 10, 48, 18,
                                                  lambda v: "%dpx" % int(v)))
        self.music_speed = self._combo("lyric.scroll_ms",
                                       [(200, "快（200ms）"),
                                        (320, "标准（320ms）"),
                                        (500, "慢（500ms）")], 320)
        self.music_speed.setObjectName("musicScrollSpeed")
        f.addRow("滚动动画速度", self.music_speed)
        lay.addWidget(self._card("歌词排版", f))
        # ---- 卡 2：音频频谱 ----
        f2 = self._form()
        self.music_spec_on = self._check("spectrum.enabled", "显示底部频谱", True)
        self.music_spec_on.setObjectName("musicSpectrumEnabled")
        f2.addRow("频谱", self.music_spec_on)
        self.music_rhythm = self._combo("spectrum.rhythm",
                                        [("calm", "舒缓（α0.20 / ω30%）"),
                                         ("standard", "标准（α0.24 / ω45%）"),
                                         ("aggressive", "激烈（α0.28 / ω62%）")],
                                        "standard")
        self.music_rhythm.setObjectName("musicSpectrumRhythm")
        f2.addRow("律动速度", self.music_rhythm)
        f2.addRow(_sub("律动映射：EMA 一阶低通 α + 演化角速度 ω，切换后**立即生效**并写入 config。"))
        lay.addWidget(self._card("音频频谱", f2))
        # ---- 卡 3：宽屏特效 ----
        f3 = self._form()
        self.music_cascade = self._check("music.cascade", "音瀑（13 键级联动画）", False)
        self.music_cascade.setObjectName("musicCascadeEnabled")
        self.music_cascade.setToolTip("窄屏（<1320px）时音乐页做 4 列级联折叠动画")
        f3.addRow("音瀑", self.music_cascade)
        lay.addWidget(self._card("宽屏特效", f3))
        lay.addStretch(1)
        sc = self._scroll(host)
        self._music_scroll = sc
        self._music_anchor = self.music_family
        return sc

    def _float_slider(self, key: str, lo_x100: int, hi_x100: int, fmt) -> QWidget:
        """★ task-116④：0.01 粒度的 Slider（内部 ×100 用整数滑块，写回时 /100）。"""
        cur = float(self.cfg.get(key, (lo_x100 + hi_x100) / 200.0) or 1.0)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        s_ = NoWheelSlider(Qt.Horizontal)
        s_.setObjectName("cfg_" + str(key).replace(".", "_"))
        s_.setProperty("zj_key", str(key))
        s_.setRange(int(lo_x100), int(hi_x100))
        s_.setValue(int(round(cur * 100.0)))
        s_.setMinimumWidth(240)
        lb = QLabel(fmt(s_.value()))
        lb.setFixedWidth(64)
        lb.setProperty("role", "sub")
        s_.valueChanged.connect(lambda v, k=key, l=lb, f=fmt: (l.setText(f(v)), self._set(k, round(v / 100.0, 3))))
        h.addWidget(s_, 1)
        h.addWidget(lb)
        return w

    def _slider_row_d(self, key: str, lo: int, hi: int, default: int, fmt=lambda v: str(v)) -> QWidget:
        """带**显式默认值**的滑块行（_slider_row 的默认取 lo，音乐分区需要 27/40 这种默认）。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        s = NoWheelSlider(Qt.Horizontal)
        s.setObjectName("cfg_" + str(key).replace(".", "_"))
        s.setProperty("zj_key", str(key))
        s.setRange(int(lo), int(hi))
        s.setValue(int(self.cfg.get(key, default)))
        s.setMinimumWidth(220)
        val = QLabel(fmt(s.value()))
        val.setFixedWidth(58)
        val.setProperty("role", "sub")
        s.valueChanged.connect(lambda v, k=key, lb=val, f=fmt: (lb.setText(f(v)), self._set(k, int(v))))
        h.addWidget(s, 1)
        h.addWidget(val)
        return w

    # ---- 声音 ----
    def _page_audio(self) -> QWidget:
        host, lay = self._form_host()
        f = self._form()
        vol = QWidget()
        vh = QHBoxLayout(vol)
        vh.setContentsMargins(0, 0, 0, 0)
        s = NoWheelSlider(Qt.Horizontal)
        s.setRange(0, 100)
        s.setValue(int(float(self.cfg.get("audio.volume", 100.0))))
        s.setMinimumWidth(240)
        lb = QLabel(f"{s.value()}%")
        lb.setFixedWidth(52)
        lb.setProperty("role", "sub")
        s.valueChanged.connect(lambda v: (lb.setText(f"{v}%"), self._set("audio.volume", float(v))))
        vh.addWidget(s, 1)
        vh.addWidget(lb)
        f.addRow("默认音量", vol)
        f.addRow(self._check("audio.mute", "启动时静音"))
        self.device_combo = NoWheelComboBox()
        self.device_combo.addItem("自动（跟随系统默认设备）", "auto")
        self.device_combo.setMinimumWidth(260)
        self.device_combo.currentIndexChanged.connect(
            lambda i: self._set("audio.device", self.device_combo.itemData(i)))
        f.addRow("输出设备", self.device_combo)
        f.addRow(_sub("输出设备列表在打开本页时才会枚举（避免启动路径上创建短命 mpv 实例）。"))
        lay.addWidget(self._card("声音", f))
        lay.addStretch(1)
        return self._scroll(host)

    def populate_audio_devices(self, devices: list[tuple[str, str]]):
        cur = self.cfg.get("audio.device", "auto")
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("自动（跟随系统默认设备）", "auto")
        for name, desc in devices:
            self.device_combo.addItem(desc or name, name)
        idx = max(0, self.device_combo.findData(cur))
        self.device_combo.setCurrentIndex(idx)
        self.device_combo.blockSignals(False)

    # ---- 快捷键 ----
    def _hotkey_header(self) -> QWidget:
        """★ task-116⑥：快捷键页表头「动作 | 主快捷键 | 副快捷键」。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        a = QLabel("动作")
        a.setFixedWidth(150)
        b = QLabel("主快捷键")
        b.setFixedWidth(150)
        c = QLabel("副快捷键")
        c.setFixedWidth(150)
        for lb in (a, b, c):
            lb.setProperty("role", "sub")
            h.addWidget(lb)
        h.addStretch(1)
        return w

    def _mark_hotkey_conflicts(self) -> list:
        """★ task-116⑥：重复按键 ⇒ 红框高亮（返回冲突的 (action, slot, spec) 列表）。

        口径：同一 spec 被**两个以上**动作/槽位占用即冲突（空槽不计）。
        """
        seen: dict = {}
        for action, slots in getattr(self, "_key_edits", {}).items():
            keys = list(self.cfg.get("hotkeys." + action, []) or [])
            for i in range(len(slots)):
                spec = str(keys[i]).strip() if i < len(keys) and keys[i] else ""
                if spec:
                    seen.setdefault(spec.upper(), []).append((action, i, spec))
        conflicts = []
        for spec, owners in seen.items():
            if len(owners) > 1:
                conflicts.extend(owners)
        hot = set((a, i) for a, i, _s in conflicts)
        for action, slots in getattr(self, "_key_edits", {}).items():
            for i, ed in enumerate(slots):
                try:
                    if (action, i) in hot:
                        ed.setStyleSheet("border:1px solid #FF5A5A;border-radius:6px;")
                        ed.setToolTip("快捷键冲突：%s 被多个动作占用" % action)
                    else:
                        ed.setStyleSheet("")
                except Exception:
                    pass
        self._hotkey_conflicts = conflicts
        return conflicts

    def _page_hotkeys(self) -> QWidget:
        host, lay = self._form_host()
        lay.addWidget(_title("快捷键"))
        lay.addWidget(_sub("每项两个槽位（主键 / 副键），点框后按下新键即可；按 Delete 清空该槽位。"))
        lay.addWidget(_sub("支持组合键：Ctrl / Shift / Alt + 任意键，例如 Shift+Left、Ctrl+Up、Shift+A。"))
        self._key_edits: dict[str, list] = {}
        lay.addWidget(self._hotkey_header())
        f = self._form()
        for action, label in HOTKEY_LABELS:
            keys = list(self.cfg.get(f"hotkeys.{action}", []) or [])
            box = QWidget()
            hb = QHBoxLayout(box)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(8)
            slots = []
            for i in range(2):
                ed = KeyCaptureEdit(action, slot=i)
                ed.set_current_text(keys[i] if i < len(keys) and keys[i] else "（未绑定）")
                ed.setMinimumWidth(150)
                ed.setToolTip("主键" if i == 0 else "副键（可留空）")
                ed.capturing.connect(self._begin_capture)
                slots.append(ed)
                hb.addWidget(ed)
            self._key_edits[action] = slots
            f.addRow(label, box)
        lay.addLayout(f)
        self._mark_hotkey_conflicts()
        row = QHBoxLayout()
        btn = QPushButton("全部恢复默认快捷键")
        btn.setProperty("role", "ghost")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._reset_hotkeys)
        row.addWidget(btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return self._scroll(host)

    def _begin_capture(self, edit: KeyCaptureEdit):
        for slots in self._key_edits.values():
            for other in slots:
                if other is not edit and other._waiting:
                    other.set_waiting(False)
        edit.set_waiting(True)

    def active_capture(self) -> KeyCaptureEdit | None:
        for slots in getattr(self, "_key_edits", {}).values():
            for ed in slots:
                if ed._waiting:
                    return ed
        return None

    def apply_captured_key(self, action: str, spec: str, slot: int = 0):
        slots = self._key_edits.get(action)
        if not slots:
            return
        ed = slots[min(slot, len(slots) - 1)]
        ed._waiting = False
        ed.set_current_text(spec or "（未绑定）")
        self.hotkey_changed.emit(action, spec, int(slot))

    def refresh_hotkeys(self):
        for action, slots in getattr(self, "_key_edits", {}).items():
            keys = list(self.cfg.get(f"hotkeys.{action}", []) or [])
            for i, ed in enumerate(slots):
                ed.set_current_text(keys[i] if i < len(keys) and keys[i] else "（未绑定）")

    def _reset_hotkeys(self):
        from ..config import DEFAULTS

        self.cfg.data["hotkeys"] = dict(DEFAULTS["hotkeys"])
        self.refresh_hotkeys()
        self.hotkey_changed.emit("*", "", 0)

    # ---- 关于 ----
    def _page_about(self) -> QWidget:
        from .. import __version__

        host, lay = self._form_host()
        lay.addWidget(_title("至简Player"))
        info = QLabel(
            f"版本 {__version__}\n"
            "三合一全能媒体查看器 —— 视频 / 图片 / 音频\n\n"
            "渲染管线：libmpv（mpv_render_context + OpenGL 硬件纹理共享）\n"
            "界面参考：爱奇艺万能联播 5.4.1 的几何比例与交互结构（逆向提取）\n"
            "视觉材质：微冷色通透底 + 0.5px 内发光高光边 + 柔和弥散阴影\n\n"
            # ⚠️ 这段是**许可要求**，不是可选文案：HarmonyOS Sans 字体许可第 2 条第 1 款
            #    要求"在软件中作出显著声明，说明使用了 HarmonyOS Sans 字体"。
            "界面字体：HarmonyOS Sans（华为，免费商用；\n"
            "许可见 assets/fonts/LICENSE-HarmonyOS-Sans.txt）\n"
            "图标：自绘 SVG（内联，无第三方图标库）\n"
            "视频抽帧：ffmpeg（imageio_ffmpeg 自带二进制）\n\n"
            "本程序为绿色便携软件，不写注册表、不驻留后台、关闭即完全退出。"
        )
        info.setWordWrap(True)
        info.setProperty("role", "sub")
        info.setMinimumHeight(180)
        lay.addWidget(info)

        # ★★★ task-107④：目录入口 + 恢复默认设置（仅重置设置项）
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        import os as _os

        def _open_dir(_checked=False):
            try:
                d = _os.path.dirname(str(self.cfg.path))
                _os.makedirs(d, exist_ok=True)
                QDesktopServices.openUrl(QUrl.fromLocalFile(d))
            except Exception:
                log.debug("打开目录失败", exc_info=True)

        brow = QHBoxLayout()
        brow.setContentsMargins(0, 8, 0, 0)
        brow.setSpacing(10)
        for label in ("打开配置文件夹", "打开日志文件夹"):
            b = GlassButton(label, "ghost")
            b.setFixedHeight(34)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(_open_dir)
            brow.addWidget(b)
        brow.addStretch(1)
        lay.addLayout(brow)

        reset_row = QHBoxLayout()
        reset_row.setContentsMargins(0, 10, 0, 0)
        btn_reset = GlassButton("恢复默认设置", "ghost")
        btn_reset.setFixedHeight(34)
        btn_reset.setCursor(Qt.PointingHandCursor)
        btn_reset.setToolTip("仅重置设置项（不会清空播放列表/最近文件）")
        btn_reset.clicked.connect(self._on_reset)
        reset_row.addWidget(btn_reset)
        reset_row.addStretch(1)
        lay.addLayout(reset_row)
        lay.addStretch(1)
        return self._scroll(host)

    # ------------------------------------------------------------------ 行为
    def fit_to(self, avail_w: int, avail_h: int):
        """按舞台可用空间自适应面板尺寸（它必须完整待在窗口内, 不能溢出）。

        ⚠️ setFixedSize 定的是**含阴影外框**的整控件尺寸, 所以上限要按
        `body + 2×阴影厚度` 算。本项目踩过：只把 body 卡在可用高度上,
        结果是整个面板（body+68）比舞台还高 38px, 直接盖到控制条上,
        而像素自审会把它读成"面板大面积与视频串色"。
        """
        m = self._margin
        w_max = M["settings_w"] + 2 * m
        h_max = M["settings_h"] + 2 * m
        w_min = M["settings_min_w"] + 2 * m
        h_min = M["settings_min_h"] + 2 * m
        w = max(w_min, min(w_max, int(avail_w) - 40))
        h = max(h_min, min(h_max, int(avail_h) - 20))
        self.setFixedSize(w, h)

    def on_theme_changed(self):
        """换肤钩子：重建**持有图标对象**的东西。

        ⚠️ 导航栏的图标是构造时 `icons.get_icon(..., P['text_dim'])` 拿到的 **QIcon 实例**，
           它把当时那个颜色**烘进了 pixmap**。换肤后光清缓存、光重绘都没用 ——
           必须重新取一次图标，否则深色切浅色会一直用深色主题的浅灰图标
           （用户："深色模式切浅色模式老是会图标变淡"，根因就在这）。
        """
        self.refresh_nav_icons()

    def refresh_nav_icons(self):
        """按当前调色板重建导航图标：**选中态用强调色、其余用二级文字色**。

        用户要求："浅色模式下未选中图标必须显式深中灰(#4E5969)，选中态给品牌蓝"。
        """
        try:
            cur = self.nav.currentRow()
            for i, (_key, _label, icon) in enumerate(SECTIONS):
                it = self.nav.item(i)
                if it is None:
                    continue
                col = P["accent"] if i == cur else P["text_dim"]
                it.setIcon(icons.get_icon(icon, col, 18))
            self.nav.setIconSize(QSize(18, 18))
        except Exception:
            log.debug("重建设置导航图标失败", exc_info=True)

    def _on_nav(self, row: int):
        if 0 <= row < len(SECTIONS):
            self.page_title.setText(SECTIONS[row][1])
            self.stack.setCurrentIndex(row)
        self.refresh_nav_icons()

    def _flush_save(self):
        """★ task-107①：防抖落盘（拖一次滑条 → 只写 1 次磁盘）。"""
        try:
            self.cfg.save()
            self._save_count += 1
        except Exception:
            log.debug("设置落盘失败", exc_info=True)

    def _set(self, key: str, value):
        self._dirty[key] = value
        self.cfg.set(key, value)
        # ★★★ task-107①：**不再立即 cfg.save()** —— 改为 300ms 防抖；
        #    连续拖动滑条只在脱手 300ms 后写一次磁盘（QA 判据：≤2 次）。
        try:
            self._save_timer.start(300)
        except Exception:
            self.cfg.save()
        # ★ task-26①：「音乐与歌词」设置**即时生效**（设置面板是唯一施加点，
        #   直接作用到 AudioView，不依赖主窗口的 _on_setting_changed 兜底）
        self._apply_music_key(key, value)
        # 让主窗口能对特定项做"即时生效"（用户："开关设置不能即时生效"）
        self.setting_changed.emit(key, value)

    # ---- task-26：音乐与歌词（即时生效 + 分区定位）------------------------------------
    MUSIC_KEYS = MUSIC_KEYS
    MUSIC_DEFAULTS = MUSIC_DEFAULTS

    def _audio_view(self):
        """拿到运行时的 AudioView（拿不到返回 None）。"""
        try:
            win = self.window()
            return getattr(getattr(win, "stage", None), "audio_view", None)
        except Exception:
            return None

    def _apply_music_key(self, key: str, value) -> bool:
        av = self._audio_view()
        if av is None:
            return False
        try:
            return bool(av.apply_music_setting(key, value))
        except Exception:
            log.debug("即时应用音乐设置 %s 失败", key, exc_info=True)
            return False

    def apply_all_music_settings(self) -> int:
        """把 config 里**已持久化**的音乐设置全部施加一遍（启动 / 首次显示时调用）。

        返回成功施加的条数（用于重启后"仍生效"的双证：config 值 + 运行时实测值）。
        """
        n = 0
        for k in self.MUSIC_KEYS:
            if self._apply_music_key(k, self.cfg.get(k, self.MUSIC_DEFAULTS.get(k))):
                n += 1
        return n

    def current_section_key(self) -> str:
        i = self.nav.currentRow()
        return SECTIONS[i][0] if 0 <= i < len(SECTIONS) else ""

    def section_index(self, key: str) -> int:
        for i, (k, _label, _icon) in enumerate(SECTIONS):
            if k == key:
                return i
        return -1

    def show_section(self, key: str) -> dict:
        """★ task-26③：定位到指定分区（导航选中 + 页面切换 + 滚到分区内容）。

        返回**可读证据**（QA 用）：索引 / 键 / 标题 / page_title 文本 / 滚动条数值 / 锚点可见性。
        """
        i = self.section_index(key)
        if i < 0:
            return {"ok": False, "key": key, "index": -1}
        self.nav.setCurrentRow(i)
        self._on_nav(i)                       # 幂等兜底（防止 nav 信号被屏蔽）
        sc = getattr(self, "_music_scroll", None) if key == "music" else None
        anchor = getattr(self, "_music_anchor", None) if key == "music" else None
        if sc is not None:
            try:
                if anchor is not None:
                    sc.ensureWidgetVisible(anchor, 0, 0)
                else:
                    sc.verticalScrollBar().setValue(0)
            except Exception:
                log.debug("定位音乐分区失败", exc_info=True)
        bar = sc.verticalScrollBar() if sc is not None else None
        return {"ok": True, "key": key, "index": i,
                "nav_row": int(self.nav.currentRow()),
                "stack_index": int(self.stack.currentIndex()),
                "title": SECTIONS[i][1],
                "page_title": self.page_title.text(),
                "scroll_value": (int(bar.value()) if bar is not None else -1),
                "scroll_max": (int(bar.maximum()) if bar is not None else -1),
                "anchor_visible": (bool(anchor.isVisible()) if anchor is not None else None),
                "has_scroll": sc is not None}

    def showEvent(self, ev):                  # noqa: N802
        """首次/每次打开设置时把已持久化的音乐设置补齐（幂等，代价极小）。"""
        super().showEvent(ev)
        # ★ task-116⑤：每次打开面板都真实枚举输出设备（mpv audio-device-list）
        try:
            eng = getattr(self.window(), "engine", None)
            devs = eng.audio_devices() if eng is not None else []
            if devs:
                self.populate_audio_devices([(d.get("name", ""), d.get("description", "")) for d in devs])
                self._audio_devices_live = len(devs)
                self._audio_devices_error = ""
            else:
                self._audio_devices_live = 0
                self._audio_devices_error = "mpv 未返回设备列表"
        except Exception as exc:
            self._audio_devices_live = 0
            self._audio_devices_error = str(exc)
            log.debug("枚举输出设备失败", exc_info=True)
        try:
            self.apply_all_music_settings()
        except Exception:
            log.debug("打开设置时施加音乐设置失败", exc_info=True)

    def _on_apply(self):
        had = bool(self._dirty)
        self._dirty.clear()
        self.changed.emit("*", None)
        self.applied.emit("设置已应用" if had else "没有待应用的改动")
        self._flash_apply_btn(had)

    # -- 「应用」的可见反馈 ---------------------------------------------------
    def _flash_apply_btn(self, had: bool):
        """点「应用」必须**看得见**反馈（用户："设置应用按钮反馈不明显"）。

        三处一起给：按钮变强调色 + 文案变「✓ 已应用」+ 主窗口弹一条 OSD。
        只做其中一处都不够 —— 按钮变色的幅度在深色玻璃底上本来就小。
        """
        if self.btn_apply is None:          # ★ task-107①：按钮栏已删除，保留兼容
            return
        self.btn_apply.setText("✓ 已应用" if had else "✓ 已是最新")
        self.btn_apply.setProperty("role", "primary")
        self._repolish(self.btn_apply)
        self._apply_flash.start(1600)

    def _restore_apply_btn(self):
        if self.btn_apply is None:          # ★ task-107①：按钮栏已删除
            return
        self.btn_apply.setText("应用")
        self.btn_apply.setProperty("role", "ghost")
        self._repolish(self.btn_apply)

    @staticmethod
    def _repolish(w):
        """改了动态属性必须 unpolish + polish，否则 QSS 不会重新匹配（颜色不跟着变）。"""
        try:
            w.style().unpolish(w)
            w.style().polish(w)
            w.update()
        except Exception:
            pass

    def _on_ok(self):
        self._on_apply()
        self._close()

    def _close(self):
        self.closed.emit()

    def _on_reset(self):
        """★ task-107④：**仅重置设置项**（绝不清空播放列表 / 最近文件），
        并且重置后**立即刷新面板所有控件显示**（页面重建，延迟到下一轮事件循环）。"""
        keep = {"recent", "playlist", "session", "window", "last_media", "media"}
        did = False
        try:
            from ..config import DEFAULTS
            for sec in DEFAULTS.keys():
                if sec in keep:
                    continue
                self.cfg.reset_section(sec)
                did = True
        except Exception:
            log.debug("分区重置失败，退回全量重置", exc_info=True)
        if not did:
            self.cfg.reset_all()
        try:
            self.cfg.save()
            self._save_count += 1
        except Exception:
            log.debug("重置后落盘失败", exc_info=True)
        try:
            self._dirty.clear()
        except Exception:
            pass
        self.reset_requested.emit()
        self.refresh_hotkeys()
        # 立即刷新面板控件（重建页面）。延迟一轮避免销毁"正在处理点击"的这个按钮。
        QTimer.singleShot(0, self._rebuild_safe)
