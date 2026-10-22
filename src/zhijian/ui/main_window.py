"""主窗口：单一天然窗口承载视频 / 图片 / 音频三种形态。

架构红线（本文件是唯一有权决定窗口结构的地方）：
  1. **没有任何独立顶层窗口**（不用 Qt::Tool / Qt::Popup 做控制栏或标题栏），
     设置面板、字幕浮层、播放列表、OSD 全部是本窗口布局内的控件；
  2. 视频画面通过 mpv_render_context 渲染进本窗口内的 QOpenGLWidget，
     与 UI 共用同一个原生 HWND + 同一个 GL 上下文；
  3. 全项目零硬编码坐标 —— 位置一律由 QVBoxLayout / QHBoxLayout / QGridLayout 决定；
  4. 退出走严格的依赖反序析构, 不调用 TerminateProcess。
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes

from PySide6.QtCore import (QEvent, QObject, QPoint, QPointF, QPropertyAnimation, QEasingCurve,
                            QRect, QRectF, QRunnable, QThreadPool, QTimer, Qt,
                            Signal)   # QRect/QRectF/QPointF: task-68 PiP + task-87 四角擦净
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QRegion,          # task-87：四角擦净（QRegion 在 QtGui，不在 QtCore）
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit
)

from .. import APP_NAME, __version__
from ..config import Config, parse_speed_tiers
from ..media import router, scanner, subtitles, tags
from ..media.prober import DurationProber
from ..mpv import MpvEngine
from ..mpv.libmpv import MPV_FORMAT_DOUBLE, MPV_FORMAT_FLAG, MPV_FORMAT_INT64, MPV_FORMAT_STRING
from ..platform import win
from . import icons
from .control_bar import ControlBar
from .playlist_drawer import PlaylistDrawer
from .settings_panel import SettingsPanel
from .stage import Stage
from .subtitle_panel import SubtitlePanel
from .theme import M, P, css, qt_color
from .titlebar import TitleBar
from .widgets import (BlurBackdrop, GlassFrame, IconButton, TextIconButton,
                      fmt_time, prep_popup, RoundedMenu
)

log = logging.getLogger("zhijian.ui")

# 各媒体形态下控制条 / 舞台的差异
IMAGE_EXTS_HINT = "图片：← → 切换 · 滚轮无级缩放 · R/E 旋转 · 1 适应窗口 · 2 原始大小 · H 镜像"


class WindowBody(QWidget):
    """窗口主体：只负责底色与 1px 描边（圆角交给 Win11 DWM 原生处理）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("WindowBody")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setProperty("win_drag", False)


class _MetaSignals(QObject):
    """附属信息任务完成信号（Qt 信号跨线程投递是安全的，不用手写队列）。"""

    done = Signal(str, dict)


class _MetaTask(QRunnable):
    """后台收集图片附属信息（os.stat / EXIF）—— 这些是磁盘活，绝不能放主线程。

    v1.0.24：切图时只做"把图显示出来"这一件事；stat/EXIF 之类的附属信息一律
    推到后台，完成后静默回填（结果回来时若已切走则直接丢弃）。
    """

    def __init__(self, path: str, sig: "_MetaSignals"):
        super().__init__()
        self._path = path
        self._sig = sig
        self.setAutoDelete(True)

    def run(self):                        # noqa: D102
        meta: dict = {}
        try:
            st = os.stat(self._path)
            meta["size"] = int(st.st_size)
            meta["mtime"] = float(st.st_mtime)
        except Exception:
            pass
        try:
            from ..media.exif import read_exif

            ex = read_exif(self._path) or {}
            if ex:
                meta["exif"] = ex
        except Exception:
            pass
        try:
            self._sig.done.emit(self._path, meta)
        except RuntimeError:
            pass                          # 窗口已析构


class _PipButton(QWidget):
    """★ task-89：PiP 悬浮条上的**纯白**图标按钮（自绘，不跟主题色 ⇒ 任何皮肤都高对比）。"""
    clicked = Signal()

    def __init__(self, icon_name: str, size: int = 30, icon_size: float = 16.0, parent=None):
        super().__init__(parent)
        self._name = str(icon_name)
        self._icon_sz = float(icon_size)
        self._pm = None
        self.setFixedSize(int(size), int(size))
        self.setCursor(Qt.PointingHandCursor)

    def set_icon(self, name: str) -> None:
        if str(name) != self._name:
            self._name = str(name)
            self._pm = None
            self.update()

    def _pixmap(self):
        if self._pm is None:
            try:
                from . import icons as _icons
                self._pm = _icons.render_pixmap(self._name, "#FFFFFF", int(self._icon_sz),
                                                float(self.devicePixelRatioF() or 1.0))
            except Exception:
                self._pm = None
        return self._pm

    def paintEvent(self, ev):        # noqa: N802
        pm = self._pixmap()
        if pm is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        dpr = float(pm.devicePixelRatio() or 1.0)
        w, h = pm.width() / dpr, pm.height() / dpr
        p.drawPixmap(QPointF((self.width() - w) / 2.0, (self.height() - h) / 2.0), pm)
        p.end()

    def mouseReleaseEvent(self, ev):  # noqa: N802
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        ev.accept()


class _PipProgress(QWidget):
    """★ task-89：PiP 迷你进度条（可拖拽，纯白；seek 交给主窗执行）。"""
    seeked = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._v = 0.0
        self._drag = False
        self.setMinimumWidth(120)
        self.setFixedHeight(22)
        self.setCursor(Qt.PointingHandCursor)

    def set_value(self, v: float) -> None:
        try:
            self._v = max(0.0, min(1.0, float(v)))
        except Exception:
            self._v = 0.0
        self.update()

    def paintEvent(self, ev):        # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        x0, x1 = 2.0, float(self.width()) - 2.0
        cy = self.height() / 2.0
        h = 4.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawRoundedRect(QRectF(x0, cy - h / 2.0, max(0.0, x1 - x0), h), h / 2.0, h / 2.0)
        fw = max(0.0, (x1 - x0) * self._v)
        if fw > 0:
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawRoundedRect(QRectF(x0, cy - h / 2.0, fw, h), h / 2.0, h / 2.0)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(x0 + fw, cy), 4.5, 4.5)
        p.end()

    def _emit_at(self, x: float) -> None:
        span = max(1.0, float(self.width()) - 4.0)
        self.seeked.emit(max(0.0, min(1.0, (float(x) - 2.0) / span)))

    def mousePressEvent(self, ev):   # noqa: N802
        if ev.button() == Qt.LeftButton:
            self._drag = True
            self._emit_at(ev.position().x())
        ev.accept()

    def mouseMoveEvent(self, ev):    # noqa: N802
        if self._drag:
            self._emit_at(ev.position().x())
        ev.accept()

    def mouseReleaseEvent(self, ev): # noqa: N802
        self._drag = False
        ev.accept()


class _PipBar(QWidget):
    """★ task-89（用户 5）：PiP 小窗底部 **40px 磨砂控制条**。

    卡片规格：高 **40px**、底色 **rgba(18,22,28,0.78)**、**5 类纯白控件** ——
    上一曲 / 播放暂停 / 下一曲 / 可拖拽迷你进度条 / 退出与关闭。"""
    prev_clicked = Signal()
    play_clicked = Signal()
    next_clicked = Signal()
    seeked = Signal(float)
    exit_clicked = Signal()
    close_clicked = Signal()
    BAR_H = 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.BAR_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(6)
        self.btn_prev = _PipButton("prev", 28, 15)
        self.btn_play = _PipButton("pause", 30, 16)
        self.btn_next = _PipButton("next", 28, 15)
        self.progress = _PipProgress()
        self.btn_exit = _PipButton("pip", 28, 15)
        self.btn_close = _PipButton("close", 28, 15)
        self.btn_prev.clicked.connect(self.prev_clicked.emit)
        self.btn_play.clicked.connect(self.play_clicked.emit)
        self.btn_next.clicked.connect(self.next_clicked.emit)
        self.progress.seeked.connect(self.seeked.emit)
        self.btn_exit.clicked.connect(self.exit_clicked.emit)
        self.btn_close.clicked.connect(self.close_clicked.emit)
        for wdg in (self.btn_prev, self.btn_play, self.btn_next):
            lay.addWidget(wdg)
        lay.addWidget(self.progress, 1)
        for wdg in (self.btn_exit, self.btn_close):
            lay.addWidget(wdg)

    def paintEvent(self, ev):        # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(18, 22, 28, int(round(0.78 * 255))))
        p.drawRoundedRect(QRectF(0, 0, float(self.width()), float(self.height())), 10.0, 10.0)
        p.end()

class _SettingsDim(QWidget):
    """★★ task-76（用户 6/7）：设置面板的**极淡平滑暗化柔光层**（零抓图 / 零模糊 / 零噪点）。

    用户原话："打开设置时底层视频定格卡死 + 全屏铺一层极丑颗粒噪点"。
    task-71 的做法是 `QScreen.grabWindow` 抓**静态画面**→离屏 `QGraphicsBlurEffect`→再加噪点：
      · 静态图盖住 GL 视频 ⇒ 视频不再刷新 ⇒ 用户看到的就是"定格"✗；
      · 模糊+噪点纹理 ⇒ "颗粒噪点"✗。
    本类只做一件事：**纯色半透明填充** rgba(10,14,20,0.35)，底层视频照常逐帧渲染 ✓。
    同时承担 **MouseGuard**：吃掉点击/滚轮，绝不穿透到底层播放器 ✓。
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setCursor(Qt.ArrowCursor)

    def paintEvent(self, ev):      # noqa: N802
        # ⚠️ 只有这一层：平滑暗化，**零噪点/零纹理/零静态图**
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(10, 14, 20, int(round(0.35 * 255))))
        p.end()

    def mousePressEvent(self, ev):     # noqa: N802
        ev.accept()                    # MouseGuard：吞掉 ⇒ 点画面不暂停 ✓
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()

    def mouseReleaseEvent(self, ev):   # noqa: N802
        ev.accept()

    def wheelEvent(self, ev):          # noqa: N802
        ev.accept()                    # 滚轮不动音量/歌词

class _SettingsKeyGuard(QObject):
    """★ task-71：设置面板打开期间**最后安装**的应用级键盘闸门。

    为什么不能只靠 MainWindow.eventFilter：audio_view 等模块也装了应用级
    eventFilter，而且可能后装（Qt 先调用后装的 filter）。键盘方向键会被它们
    抢在 MainWindow 之前处理 ⇒ 实测「设置打开时按 → 仍然 seek」。
    面板打开时把这个 guard 装到 QApplication（最后安装⇒最先收到事件），
    关闭即卸掉；只拦播放控制键，且热键录制捕获态放行。
    """

    BLOCKED = (Qt.Key_Space, Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
               Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End)

    def __init__(self, win):
        super().__init__(win)
        self._win = win
        self._on = False

    def install(self):
        if not self._on:
            QApplication.instance().installEventFilter(self)
            self._on = True

    def uninstall(self):
        if self._on:
            try:
                QApplication.instance().removeEventFilter(self)
            except Exception:
                pass
            self._on = False

    def eventFilter(self, obj, ev):  # noqa: N802
        try:
            w = self._win
            if not self._on or not w._settings_open():
                return False
            t = ev.type()
            if t not in (QEvent.KeyPress, QEvent.ShortcutOverride, QEvent.KeyRelease):
                return False
            if w.settings is not None and w.settings.active_capture() is not None:
                return False          # 正在录制快捷键：按键交给设置面板
            if ev.key() in self.BLOCKED:
                ev.accept()
                return True
            # 焦点不在设置面板内：其它按键也一律不穿透（Esc 除外，用于关闭）
            if t != QEvent.KeyRelease and not w._target_in_settings(obj):
                if t == QEvent.KeyPress and ev.key() == Qt.Key_Escape:
                    w.toggle_settings(False)
                    return True
                ev.accept()
                return True
        except Exception:
            log.debug("设置键盘闸门异常", exc_info=True)
        return False


class MainWindow(QWidget):
    """主窗口。"""

    # task-20 ①：起播校验延迟（ms）。只用 QTimer.singleShot，不新增常驻 QTimer。
    PLAY_VERIFY_MS = 260

    shutting_down = Signal()

    def __init__(self, cfg: Config, engine: MpvEngine, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.engine = engine
        self._kind = router.KIND_VIDEO
        self._path = ""
        self._duration = 0.0
        self._position = 0.0
        self._paused = True
        # task-20 ②：进度条拖拽中 —— 禁止 mpv 的 time-pos 回写抢位
        self._is_seeking = False
        self._speed = 1.0
        self._boost = False
        self._pre_boost_speed = 1.0
        self._fullscreen = False
        self._always_on_top = False
        # ★★ task-68：画中画（PiP）状态 —— 原几何/最大化态/置顶前状态都要逐位记住
        self._pip = False
        self._pip_saved_geo = None
        self._pip_saved_max = False
        self._pip_saved_topmost = False
        self._pip_overlay = None
        self._pip_error = ""
        self._flush_src = False      # ★ task-82：下一帧是否"擦净"半透明窗口的上一帧
        self._right_down = False
        self._boost_dir = 1          # 1 = 右方向键（快进）；-1 = 左方向键（快退）
        self._closing = False
        self._image_zoom = 1.0
        self._image_rotation = 0
        self._image_flip = False
        self._image_natural = (0, 0)
        self._positions: dict = cfg.load_positions()
        self._last_save_pos = 0.0
        self._drag_pos = None
        self.prober = DurationProber(self)
        self.prober.duration_ready.connect(self._on_duration_probed)
        self._geom_log: list = []
        self._key_log: list = []
        self._last_preview_seek = 0.0

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowMinMaxButtonsHint)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setMinimumSize(M["win_min_w"], M["win_min_h"])
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

        self._build_ui()
        self._wire_engine()
        self._restore_geometry()
        self._install_hotkeys()
        self._start_timers()
        self._update_empty_state()

    # =====================================================================
    # 构建
    # =====================================================================
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.body = WindowBody(self)
        outer.addWidget(self.body)

        col = QVBoxLayout(self.body)
        # 窗口化时留 1px 给 `#WindowBody` 的描边（不然内容会盖住那圈线）；
        # ⚠️ 全屏时必须把它也归零，否则画面比窗口小 2px ——
        #    浅色主题下这一圈就是用户说的"白边"（见 _set_body_fullscreen）。
        col.setContentsMargins(1, 1, 1, 1)
        col.setSpacing(0)

        # ---- 标题栏 ----
        self.titlebar = TitleBar(APP_NAME, self.body)
        self.titlebar.menu_clicked.connect(self._show_main_menu)
        # 播放列表入口只保留两处：胶囊上的按钮 + 右边缘手柄。\n        # 标题栏那个是第三处重复（用户反馈"一个页面太多重复按钮了"），已移除。
        self.titlebar.settings_clicked.connect(lambda: self.toggle_settings(True))
        self.titlebar.theme_toggled.connect(self.toggle_light_dark)
        self.titlebar.minimize_clicked.connect(self.showMinimized)
        self.titlebar.maximize_clicked.connect(self.toggle_maximize)
        self.titlebar.close_clicked.connect(self.close)
        col.addWidget(self.titlebar)

        # ---- 内容行：舞台 + 抽屉 ----
        content = QWidget(self.body)
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        crow = QHBoxLayout(content)
        crow.setContentsMargins(0, 0, 0, 0)
        crow.setSpacing(0)

        self.stage = Stage(self.engine, content)
        crow.addWidget(self.stage, 1)

        # 播放列表抽屉：**挂在舞台里当覆盖层**，而不是和舞台并排占布局。
        # ⚠️ 早先它和舞台是同一行的兄弟，于是展开动画的每一帧都要重新分配宽度 ⇒
        #    整个窗口 relayout（实测 26.7ms/帧）+ QOpenGLWidget 重新分配 FBO，
        #    动画只有 11 fps（用户报"播放列表弹开很卡"）。
        #    改成覆盖层后，动画期间**视频控件尺寸完全不变**，一帧只剩抽屉自己的重绘。
        #    它仍然在主窗口内、仍然贴右内边缘、底边仍然正好是胶囊顶边（不切断控制栏）。
        self.drawer = PlaylistDrawer(self.stage, width=int(self.cfg.get("ui.drawer_width", M["drawer_w"])))
        self.drawer.item_activated.connect(lambda p: self.open_path(p))
        self.drawer.request_add_files.connect(self.open_files_dialog)
        self.drawer.request_clear.connect(self._clear_playlist)
        self.drawer.request_remove.connect(self.drawer.remove_path)
        self.drawer.request_reveal.connect(win.reveal_in_explorer)
        # #7 ⚠️⚠️ 这里原来写的是 `lambda _k: None` —— 排序请求被**直接丢弃**，
        #    于是抽屉只重排了界面行，底层真实播放序列没变 ⇒ 用 ←/→、热区翻页时
        #    顺序和列表看到的**不一致**。现在真正重排底层序列。
        self.drawer.request_sort.connect(self._on_sort_requested)
        self.drawer.open_state_changed.connect(self._on_drawer_state)
        # 抽屉开合会改变手柄的 x（跟随接缝）⇒ 重新摆一次（y 始终 50% 居中）
        self.drawer.open_state_changed.connect(lambda _on: self.stage.sync_hotspots())
        self.stage.attach_drawer(self.drawer)
        col.addWidget(content, 1)

        # ---- 底部悬浮胶囊 ----
        # ⚠️ 胶囊**不再占布局的一行**，而是挂进舞台的第 3 行（视频在它后面一直铺到窗口底）。
        #    用户要的正是这个：视频铺满整个画面区、不因为控制条而"卡在上面"，
        #    无操作时胶囊淡出，视频就是完整的一块 —— 既不影响读信息，也没有"视频被压缩"
        #    的割裂感。抽屉跨 0..2 行，底边正好落在胶囊顶边，仍然不会切断控制栏。
        self.capsule = ControlBar(self.stage)
        self.stage.attach_capsule(self.capsule, M["capsule_margin_lr"],
                                  M["capsule_margin_b"], M["capsule_shadow"])

        # ---- 浮层：设置面板 / 字幕浮层 ----
        # ⚠️ **懒构建**。设置面板 288 个控件 + 字幕面板 63 个控件, 而整窗一共才 443 个 ——
        #    它俩一开始就建出来的话, 全局样式表要在启动时给这 351 个隐藏控件各做一次 polish,
        #    实测 window.show() 因此要 1.5~3 秒（用户反馈"启动很慢"就是这个）。
        #    改成首次真正打开时才构建, 启动路径上的控件数从 443 掉到 92。
        self.settings = None
        self.subtitle_panel = None
        # ★★ task-76：设置面板的二级模态 —— **纯平滑暗化浮层**（取代 task-71 的抓图模糊背板）
        self._settings_dim = None
        # （task-71 的 BlurBackdrop 已不再实例化；保留变量仅为兼容外部 getattr）
        self._settings_backdrop = None
        self._settings_bounce_anim = None
        self._settings_key_guard = None

        # ---- 画布（图片工具条 / 音频卡片 / 空状态）连线 ----
        ib = self.stage.image_bar
        ib.prev_image.connect(lambda: self._step_image(-1))
        ib.next_image.connect(lambda: self._step_image(1))
        # ⚠️ 1.0.11 里放大/缩小是**接反的**（zoom_in 接除法 = 缩小）—— 一并修正；
        #    并接上缩放滑条（规范 §5 双向绑定：滑条 → 视口，静默回填走 scale_changed）
        ib.zoom_in.connect(lambda: self._iv_zoom(float(self.cfg.get("ui.image_zoom_step", 1.12))))
        ib.zoom_out.connect(lambda: self._iv_zoom(1 / float(self.cfg.get("ui.image_zoom_step", 1.12))))
        ib.zoom_changed.connect(self._iv_set_scale)
        # #7 「适应窗口」直连视口复位：不再经 `_set_image_fit` 的多级分派
        #    （实测按钮/信号链走到底没生效），这里保证按一下就回到适应窗口。
        ib.zoom_fit.connect(self._iv_fit)
        ib.zoom_one.connect(lambda: self._set_image_fit("one"))
        ib.rotate_left.connect(lambda: self._rotate_image(-90))
        ib.rotate_right.connect(lambda: self._rotate_image(90))
        ib.flip_h.connect(self._flip_image)
        # #9 全屏按钮（用户要求放进控制栏）
        ib.fullscreen_toggled.connect(self.toggle_fullscreen)
        # #7 看图模式的播放列表入口（替代被隐藏的抽屉手柄）
        ib.playlist_toggled.connect(self._toggle_drawer)
        # 视口 → 滑条（静默）+ 热区 → 切图（红线 2：事件只在各自几何范围内流转）
        try:
            self.stage.image_view.scale_changed.connect(
                lambda s: self.stage.image_bar.set_zoom(float(s)))
            # #3 OSD 时序（用户规范）：**文本 0ms 刷新**，定时器只管**隐藏**。
            #    每次数值变化就重发一次 OSD（show_osd 内部会重启 1.0s 倒计时）⇒
            #    滚动不停时数值实时跳动且常驻，停手 1 秒后平滑淡出。
            #    ⚠️ 这里**不再**接 scale_changed：视口内部复位（切图/旋转/镜像）也会
            #       发这个信号，接了就会用"100%"顶掉真正的用户提示（#4）。
            self.stage.image_view.user_zoomed.connect(self._show_zoom_osd)
            self.stage.image_view.step_image.connect(self._step_image)
            self.stage.image_view.load_failed.connect(self._on_image_failed)
            self.stage.image_view.viewport_resized.connect(self.stage.sync_hotspots)
        except Exception:
            log.debug("图片视口信号接线失败", exc_info=True)
        ib.reveal.connect(lambda: win.reveal_in_explorer(self._path))

        # v1.0.27 旧 AudioCard 已物理销毁（连带其信号连线一并移除）

        es = self.stage.empty_state
        es.open_file.connect(self.open_files_dialog)
        es.open_folder.connect(self.open_folder_dialog)
        es.recent_picked.connect(lambda p: self.open_path(p))

        # ---- 胶囊连线 ----
        cb = self.capsule
        cb.play_pause_clicked.connect(self.toggle_pause)
        # 「停止」按钮已按用户要求移除（它让播放键无法居中）；停止只剩快捷键 X。
        cb.prev_clicked.connect(lambda: self._step_file(-1))
        cb.next_clicked.connect(lambda: self._step_file(1))
        cb.seek_started.connect(self._on_seek_started)
        cb.seek_committed.connect(self._on_seek_committed)
        cb.seek_preview.connect(self._on_seek_preview)
        cb.volume_changed.connect(self._on_volume)
        cb.mute_toggled.connect(self._on_mute)
        cb.speed_changed.connect(self.set_speed)
        cb.subtitle_clicked.connect(self.toggle_subtitle_panel)
        cb.settings_clicked.connect(self._show_play_menu)
        cb.fullscreen_toggled.connect(self.toggle_fullscreen)
        cb.playlist_toggled.connect(self._on_playlist_button_from_ui)
        cb.screenshot_clicked.connect(self.take_screenshot)
        cb.loop_mode_changed.connect(self._on_loop_mode)
        cb.frame_back.connect(lambda: self._step_frame(-1))
        cb.frame_fwd.connect(lambda: self._step_frame(1))
        # ★★ task-68（用户 5）：**画中画**按钮接线（该按钮早已存在，只是从未连过 ✗）
        cb.pip_clicked.connect(self.toggle_pip)
        self.capsule.set_loop_mode(str(self.cfg.get("playback.loop_mode", "sequence")))

        # ---- 字幕浮层 ----
        sp = self._ensure_subtitle_panel()
        sp.font_changed.connect(lambda v: self._apply_sub_style("sub-font", v))
        sp.size_changed.connect(lambda v: self._apply_sub_style("sub-font-size", v))
        sp.border_color_changed.connect(lambda c: self._on_sub_border(c))
        sp.position_changed.connect(lambda v: self._apply_sub_style("sub-pos", v))
        sp.delay_changed.connect(self._set_sub_delay)
        sp.track_changed.connect(self._set_sub_track)
        sp.visibility_changed.connect(self._set_sub_visible)
        sp.closed.connect(lambda: self.toggle_subtitle_panel(False))
        sp.open_file_requested.connect(self.load_subtitle_dialog)

        # ★★ task-56（Lead 读码定案）：**浅色档冷启动图标反了** —— 图标只在
        #   `TitleBar.set_light(light)` 里设置（`on_theme_changed()` **只 `update()`、不设图标** ✗）；
        #   启动时没人调它 ⇒ 首帧永远是构造默认值 `IconButton("sun")`：
        #     · 深色档**碰巧对**（深色就该显示 sun ✓）
        #     · **浅色档错**（应为 moon ✗）
        #   ⇒ 这里"设图标 + 重绘"两件都做（幂等：读的是当前主题）。
        try:
            from . import theme as _theme
            self.titlebar.set_light(bool(_theme.is_light()))
            self.titlebar.on_theme_changed()          # 顺带重绘一次（取色）
            self._theme_icon_boot_sync = True
        except Exception:
            self._theme_icon_boot_sync = False
            log.debug("启动同步主题图标失败", exc_info=True)

    # =====================================================================
    # 引擎连线
    # =====================================================================
    def _wire_engine(self):
        self.engine.property_changed.connect(self._on_prop)
        self.engine.file_loaded.connect(self._on_file_loaded)
        self.engine.end_file.connect(self._on_end_file)
        self.engine.video_reconfig.connect(lambda: self.stage.video.update())

    def _on_prop(self, name: str, value):
        if name == "time-pos":
            self._position = float(value or 0.0)
            if getattr(self, "_pip", False):
                self._pip_sync_progress()      # ★ task-89：PiP 进度条跟随（无新定时器）
            # task-20 ②：拖动期一律不回写（滑块与时间文本都归鼠标）
            if self._kind != router.KIND_IMAGE and not self._is_seeking:
                self.capsule.set_times(self._position, self._duration)
        elif name == "duration":
            self._duration = float(value or 0.0)
            if not self._is_seeking:
                self.capsule.set_times(self._position, self._duration)
            if self._path:
                self.drawer.update_duration(self._path, self._duration)
        elif name == "pause":
            self._paused = bool(value)
            self.capsule.set_paused(self._paused)
            # ★ task-68：PiP 悬浮按钮同步（暂停 ⇒ 显示 play 图标）
            try:
                if self._pip and getattr(self, "_pip_btn_play", None) is not None:
                    self._pip_btn_play.set_icon("play" if self._paused else "pause")
            except Exception:
                log.debug("PiP 按钮同步失败", exc_info=True)
            # v1.0.27 旧 AudioCard 引用已移除（实例已物理销毁）
        elif name == "speed":
            self._speed = float(value or 1.0)
            self.capsule.set_speed(self._speed)
        elif name == "demuxer-cache-time":
            self.capsule.set_cache(float(value or 0.0))
        elif name == "sid" or name == "track-list":
            if self.subtitle_panel is not None and self.subtitle_panel.isVisible():
                self._refresh_subtitle_tracks()
        elif name == "width" or name == "height":
            if self._kind == router.KIND_IMAGE:
                self._update_image_info()
        elif name in ("paused-for-cache"):
            if value and self.cfg.get("ui.osd", True):
                self.show_osd("缓冲中…", "clock")
        elif name == "volume":
            self.capsule.set_volume(float(value or 0.0))
        elif name == "mute":
            self.capsule.set_mute(bool(value))

    def _tag(self, key: str) -> str:
        meta = self.engine.cached("metadata", {}) or {}
        val = meta.get(key) or meta.get(key.capitalize()) or ""
        return str(val) if val else ""

    # =====================================================================
    # 打开 / 路由
    # =====================================================================
    def handle_dropped_paths(self, paths: list[str]):
        files: list[str] = []
        for p in paths:
            if os.path.isdir(p):
                items = scanner.list_dir(p, None)
                files.extend(i.path for i in items)
            else:
                files.append(p)
        files = [f for f in files if router.is_media(f)]
        if not files:
            self.show_osd("没有可播放的媒体文件", "info", P["warn"])
            return
        first = files[0]
        self._playlist_from(files, first)
        self.open_path(first)

    def _playlist_from(self, files: list[str], current: str):
        items = [scanner.MediaItem(f) for f in files]
        self.drawer.set_items(items, current)
        self.drawer.set_current(current)
        self.prober.request([i.path for i in items if not i.duration])

    def _on_duration_probed(self, path: str, duration: float):
        self.drawer.update_duration(path, duration)

    def open_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "打开媒体文件", "", router.file_filter_string())
        if files:
            self.handle_dropped_paths(files)

    def open_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "打开文件夹")
        if d:
            self.handle_dropped_paths([d])

    def open_path(self, path: str, autoplay: bool = True, remember_dir: bool = True):
        """按媒体类型路由：视频 / 图片 / 音频。"""
        if not path or not os.path.isfile(path):
            self.show_osd(f"文件不存在：{os.path.basename(path or '')}", "info", P["danger"])
            return
        path = os.path.abspath(path)
        kind = router.kind_of(path)
        if kind == router.KIND_UNKNOWN:
            kind = router.KIND_VIDEO          # 交给 mpv 试一次
        # ★★ task-58：**打开任何新文件都无条件唤醒/显示顶栏**（先退出沉浸隐藏态）。
        #    用户 ⑦：音乐播放中双击图片 ⇒ 顶栏整条消失（音频 idle 时 TitleBar 被
        #    QGraphicsOpacityEffect 动画到 opacity=0，旧的图片切换路径只唤醒图片底栏）。
        #    放在改 _kind 之前也覆盖「同模式再开一个文件」（沉浸态下换歌同样要恢复）。
        self._ensure_titlebar_visible("open_path:%s" % kind)
        # task-20 ①：**必须先记下"从哪个模式切过来"**，再改 self._kind。
        # 原来下面那句快速路径判据写的是 `kind == KIND_IMAGE and self._kind == KIND_IMAGE`，
        # 而 self._kind 在上一行刚被赋成 kind ⇒ 对图片**恒真**，于是
        # `_prepare_engine_for(IMAGE)`（里面才有 `engine.cmd("stop")`）**永远不执行**：
        # 切到图片只是"被 loadfile 顶掉"，mpv 里还挂着前一个媒体（实测 mpv path 仍是
        # 上一张 cover_art.png）——"后台还有声音/还挂着媒体"就是这么来的。
        prev_kind = self._kind
        self._path = path
        self._kind = kind
        # ★★ task-69：切入音频/图片前**无条件清空分辨率残留**（严禁把上一部视频的
        #    1920×1080 带进 MP3/图片标题栏；视频的尺寸由 _on_file_loaded 重新写入）。
        if kind != router.KIND_VIDEO:
            self._title_px = ""
        self._update_window_title(path)
        self.cfg.push_recent(path)
        self.cfg.save_if_dirty()

        # 同目录播放列表（图片按图片列, 音频按音频列, 视频按视频列）
        if not self.drawer.paths() or remember_dir:
            items, idx = scanner.siblings(path, kind)
            if items:
                self.drawer.set_items(items, path)
                self.prober.request([i.path for i in items if not i.duration])

        self._apply_kind_ui(kind)
        # #16 图片↔图片：完全不需要碰 mpv（每条指令都是一次同步 IPC）。
        #     连续翻图时这是"每张多花一两百毫秒"的主要来源之一。
        if kind == router.KIND_IMAGE and prev_kind == router.KIND_IMAGE:
            # ⚠️⚠️ 快速路径必须**自己**完成"把图交给视口"这件事 ——
            #    图片的加载动作原本写在 `_prepare_engine_for` 里，整段跳过就等于
            #    **再也不加载图片**（实测：连续切图全部超时、画面停在上一次）。
            try:
                if self._path:
                    self._iv_load(self._path)
            except Exception:
                log.debug("快速路径加载图片失败", exc_info=True)
        else:
            self._prepare_engine_for(kind)
        # 音频模式在**加载的那一刻**就把视频输出关掉（vid 会被 mpv 按文件重置，
        # 先 set 再 load 是无效的）。视频/图片不带选项 —— 见 _prepare_engine_for 的说明。
        # v1.0.28 ①：**视频必须显式带上 vid=auto** —— 音频模式为藏封面关过视频轨，
        # 而 mpv 的 vid 是 per-file 选项：只在 load 前 set 属性会被随后的 load 覆盖，
        # 于是"音乐→视频"永远是黑的（用户实测：只有声音没有画面）。
        if kind == router.KIND_AUDIO:
            load_opts = "vid=no,aid=auto"
        elif kind == router.KIND_VIDEO:
            load_opts = "vid=auto,aid=auto"
        else:
            load_opts = "vid=auto" 
        # v1.0.28 ①：切到**视频**必须把视频轨显式恢复回来 —— 音频模式为了藏封面
        # 关掉过 vid（video=no / vid=no），mpv 的属性是**持久**的，不恢复就黑屏
        # （用户实测：音乐→视频只有声音没有画面）。
        if kind != router.KIND_AUDIO:
            try:
                self.engine.set("vid", "auto", MPV_FORMAT_STRING)
                self.engine.set("video", "auto", MPV_FORMAT_STRING)
            except Exception:
                log.debug("恢复视频轨失败", exc_info=True)
            # task-20 ①：**图片模式不能让 mpv 画布再显形**。
            # 以前这里无条件 setVisible(True)，而图片当时确实交给 mpv 画（所以看着正常）；
            # 现在图片只走 Qt 视口（见下面的 load 分流）——再亮这块空画布就只是让
            # mpv 在图片底下空转/刷主题底色，和"红线 1：图片不交给 libmpv"相悖。
            try:
                self.stage.video.setVisible(kind != router.KIND_IMAGE)
            except Exception:
                pass
        if kind == router.KIND_AUDIO:
            # v1.0.26：音频模式下 mp3/flac 的内嵌封面会被 mpv 当画面渲染，
            # 叠在我们的音乐视图上形成"幽灵封面"（截图实测）。
            # vid=no 对部分容器不稳，这里**再补两道**：关视频轨 + 藏掉视频控件。
            try:
                self.engine.set("vid", "no", MPV_FORMAT_STRING)
            except Exception:
                log.debug("关闭视频轨失败", exc_info=True)
            try:
                self.stage.video.setVisible(False)
            except Exception:
                pass
        # task-20 ①：**图片绝不进 mpv**（红线 1：图片交给 Qt GPU 视口）。
        # 原来这里对所有类型都 loadfile —— 于是刚在 `_prepare_engine_for` 里
        # `cmd("stop")` 掉的 mpv 立刻又被塞进一张图，图片模式下 mpv 里永远挂着东西。
        if kind != router.KIND_IMAGE:
            self.engine.load(path, options=load_opts)
            if autoplay:
                self.engine.set("pause", False, MPV_FORMAT_FLAG)
        self._update_empty_state()
        # 图片没有「播放位置」可言：跳过恢复，省掉一次 mpv 写入（连续翻图时每张都省）
        if kind != router.KIND_IMAGE:
            self._maybe_resume_position(path)
            # task-20 ①：起播校验（"必要时显式播放"）—— 只在 QTimer.singleShot 里跑，
            # 不新增常驻 QTimer（帧钟/定时器红线不动）。
            QTimer.singleShot(self.PLAY_VERIFY_MS, lambda p=path: self._verify_playback_started(p, 0))

    def _prepare_engine_for(self, kind: str):
        # 音频：关掉一切视频输出（包括 mp3 的内嵌封面流）—— 封面由我们自己的
        # AudioCard 展示, 否则会和 mpv 的画面重叠成"两张封面"。
        #
        # ⚠️ 这里**只在音频时**动 `vid`, 而且只走 loadfile 的逐文件选项（见 open_path）。
        #    以前每个文件都写一次 `vid`（auto/no 来回切）+ 每次都带逐文件选项,
        #    实测每次"视频↔音频"转换会稳定泄漏 1~2 MB（40 轮长跑 508→648 MB 不收敛）。
        #    原因在 mpv 侧：反复 关/开 视频链会重建 VO, 而重建过程有东西不回收。
        #    现在视频/图片一律**不碰 vid**（保持默认 auto）, 只在进音频时关一次。
        if kind == router.KIND_IMAGE:
            # 红线 1（媒体路由彻底分流）：图片**不交给 libmpv**。
            # 让 mpv 停下，显示交给 Qt 原生 GPU 视口 ImageViewerWidget。
            try:
                self.engine.cmd("stop")
            except Exception:
                try:
                    self.engine.set("pause", True, MPV_FORMAT_FLAG)
                except Exception:
                    pass
            self.engine.set("loop-file", "inf", MPV_FORMAT_STRING)
            try:
                if self._path:
                    self._iv_load(self._path)
            except Exception:
                log.debug("加载 Qt 图片视口失败", exc_info=True)
            # ⚠️⚠️ 这里原来是 `video-zoom / video-pan-x / video-pan-y / video-rotate`
            #    四连写 —— 那正是"向 libmpv 发送图片变换指令"（红线 1 严禁）。
            #    现在图片的缩放/平移/旋转/镜像全部由 Qt GPU 视口负责，mpv 只被要求停下。
            #    （删掉这几行后 `zoom` 变量也不再需要，避免悬空引用。）
            self._set_sub_visible(False)
        else:
            self.engine.set("loop-file", "no", MPV_FORMAT_STRING)
            self.engine.set("video-zoom", 0.0, MPV_FORMAT_DOUBLE)
            self.engine.set("video-rotate", 0, MPV_FORMAT_INT64)
            self.engine.set("video-pan-x", 0.0, MPV_FORMAT_DOUBLE)
            self.engine.set("video-pan-y", 0.0, MPV_FORMAT_DOUBLE)
            self._image_rotation = 0
            self._image_zoom = 1.0
            if self.cfg.get("subtitle.enabled", True):
                self._set_sub_visible(True)

    def _verify_playback_started(self, path: str, attempt: int = 0):
        """task-20 ①：起播校验 —— "必要时显式播放"，绝不让"发过 loadfile"充当成功。

        背景：QA 在反向路径（图片 → 音频）实测过 3s 内 time-pos=0 不前进、duration=0；
        本机干净环境复现同一条路径是**能起播**的（0.3s→0.22s、1.0s→0.96s、3.0s→2.97s），
        说明它是竞态而不是必然失败。所以这里不赌：load 之后 260ms 回看一眼，
        只要 time-pos 与 duration 都还是 0/None（且不是用户主动暂停），就先显式再
        play 一次；再 260ms 仍起不来就重发一次 loadfile。全程只用 QTimer.singleShot
        （常驻 QTimer 计数不变，16ms 帧钟红线不动）。
        """
        if self._closing or path != self._path or self._kind == router.KIND_IMAGE:
            return
        if self._paused:                 # 用户主动暂停：不打扰
            return
        started = False
        for value in (self.engine.cached("time-pos", None), self.engine.cached("duration", None)):
            try:
                if value is not None and float(value) > 0.0:
                    started = True
            except (TypeError, ValueError):
                pass
        if started:
            return
        if attempt == 0:
            log.info("起播校验：%dms 内无进度/时长，显式 play 一次（%s）",
                     self.PLAY_VERIFY_MS, os.path.basename(path))
            self.engine.set("pause", False, MPV_FORMAT_FLAG)
            QTimer.singleShot(self.PLAY_VERIFY_MS, lambda: self._verify_playback_started(path, 1))
            return
        if attempt == 1:
            log.warning("起播校验：显式 play 后仍无进度，重发一次 loadfile（%s）",
                        os.path.basename(path))
            try:
                opts = "vid=no,aid=auto" if self._kind == router.KIND_AUDIO else "vid=auto,aid=auto"
                self.engine.load(path, options=opts)
                self.engine.set("pause", False, MPV_FORMAT_FLAG)
            except Exception:
                log.debug("起播校验重发 loadfile 失败", exc_info=True)

    def _current_frame_image(self):
        """抓**当前正在播放的那一帧**（用于悬停预览的模糊占位）。

        ⚠️ 用 mpv 自己的 `screenshot-to-file` —— 帧已经在内存里，
        几乎零成本（几毫秒），比另起 ffmpeg 子进程快得多。
        失败时返回 None，调用方会退回"载入中"骨架。
        """
        import os
        import tempfile

        from PySide6.QtGui import QImage

        if not self._path or self._kind == router.KIND_AUDIO:
            return None
        try:
            tmp = os.path.join(tempfile.gettempdir(), "zhj_preview_placeholder.png")
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if self.engine.cmd("screenshot-to-file", tmp, "video") < 0:
                return None
            if not os.path.exists(tmp):
                return None
            img = QImage(tmp)
            return None if img.isNull() else img
        except Exception:
            return None

    def _on_setting_changed(self, key: str, value):
        """设置项变化后**立刻**作用到运行时，而不是等重启或点"应用"。

        ⚠️ 新增设置项时，如果它影响运行时行为，
        **必须在这里加一条** —— 否则就是"开关点了没反应"。
        """
        try:
            if key == "ui.preview_thumb":
                self.capsule.set_preview_enabled(bool(value))
                if value and self._path:
                    self.capsule.set_preview_source(str(self._path))
            elif key == "ui.osd":
                self.stage.osd.setVisible(bool(value))
            elif key == "ui.animations":
                self._animations = bool(value)
            elif key == "ui.capsule_autohide":
                self._autohide = bool(value)
                if not value:
                    self._fader.wake()
                    self._top_fader.wake()
        except Exception:
            log.debug("即时应用设置 %s 失败", key, exc_info=True)

    def _apply_kind_ui(self, kind: str):
        # ★★ task-58：**任何模式切换都无条件唤醒/显示顶栏**（四个方向全覆盖）。
        #    放在函数最顶：后面所有分支/早退都不可能绕过这次恢复。
        self._ensure_titlebar_visible("apply_kind:%s" % kind)
        self.capsule.set_media_kind(kind)
        # 悬停缩略图预览：告诉控制栏"现在放的是哪个文件"。
        # 抽帧走 **ffmpeg 子进程**（见 mpv/frame_grab.py 顶部说明），
        # 进程级隔离，不会像第二个 libmpv 实例那样和主引擎争死。
        self.capsule.set_preview_enabled(bool(self.cfg.get("ui.preview_thumb", True)))
        self.capsule.set_preview_source("" if kind == "image" else str(self._path or ""))
        # 没有缓存缩略图时，用"当前播放画面"做模糊占位（严禁留死灰空框）
        self.capsule._placeholder_fn = self._current_frame_image
        # 媒体路由（红线 1）：图片 = Qt 原生 GPU 视口；音视频 = mpv 渲染画布。
        # ⚠️ 1.0.11 基线里这里只切了 image_bar；视口的显隐必须一起切，
        #    否则视口虽然建好了却从不显示（几何 100x30 ⇒ 取景框"父级不可见"，实测踩到）。
        try:
            self.stage.image_view.setVisible(kind == router.KIND_IMAGE)
            # ★★ task-37（用户 ⑦）：**模式泄漏**根治 —— 唯一的模式 UI 收口就在这里，
            #   顺手把图片翻页热区（HotspotZone）按模式失效/恢复：非图片模式一律
            #   hide + setEnabled(False)（真机：音频模式左上角仍有幽灵箭头且接收悬停）。
            try:
                self.stage.set_zone_kind(kind)
            except Exception:
                log.debug("切换热区模式失败", exc_info=True)
            # v1.0.26：音乐模式下**必须隐藏视频层** —— mp3/flac 的内嵌封面会被 mpv
            # 当画面渲染，和我们的封面重叠成两张封面（截图实测的幽灵）。
            # 靠 mpv 的 vid=no 不够稳，直接不显示那个控件最可靠。
            self.stage.video.setVisible(kind not in (router.KIND_IMAGE, router.KIND_AUDIO))
            # ⚠️⚠️ 绝不能 `image_view.raise_()` —— 它和 mpv 画布共用跨四行的大格子，
            #    抬到最上层会把**底部控制栏、顶部信息栏、抽屉手柄**一起压住
            #    （用户实测：「控制栏不见了」）。正确做法是抬升**浮层栈**。
            self.stage.raise_overlays()
        except Exception:
            log.debug("切换图片视口可见性失败", exc_info=True)
        # #4 图片模式：**不要**播放进度条/音量条 ⇒ 整个胶囊隐藏；
        #    图片控制条移到**底部**（stage.image_bar_host）。
        try:
            self.stage.set_image_mode(kind == router.KIND_IMAGE)
            self.capsule.setVisible(kind != router.KIND_IMAGE)
            # v1.0.26 音乐模式：显示新总成（模糊背景 + 双栏 + 频谱）；旧白卡片永久隐藏
            try:
                # v1.0.28 ②/③：歌词位置提供者 + 音乐模式关掉进度条预览框
                av0 = self.stage.audio_view
                av0.set_position_provider(
                    lambda: self.engine.cached("time-pos", 0.0) or 0.0)
                self.capsule.set_preview_mode_ok(kind != router.KIND_AUDIO)
                self.stage.set_audio_view_visible(kind == router.KIND_AUDIO)
                # v1.0.27 旧 AudioCard 引用已移除（实例已物理销毁）
            except Exception:
                log.debug("切换音乐视图失败", exc_info=True)
            if kind == router.KIND_IMAGE:
                # #6 刚切进图片模式：控制栏与手柄先亮着，随后交给自动隐藏计时
                try:
                    self._imgbar_fader.fade_in()
                    self._handle_fader.fade_in()
                    self._hide_timer.start()
                except Exception:
                    pass
        except Exception:
            log.debug("切换图片模式底栏失败", exc_info=True)
        # v1.0.27 旧 AudioCard 引用已移除（实例已物理销毁）
        # #3 看图模式：**隐藏播放列表手柄**（看图时它意义不大且挡画面）
        # #6 看图模式**保留**抽屉手柄（用户明确要求）；它与左右热区箭头都在
        #    50% 垂直中心、水平方向互不重叠（热区箭头内缩 28px）。
        # ★ task-42：看图模式的"悬停 450ms 自动展开"已恢复（三模式统一），
        #   不再有 kind==KIND_IMAGE 的提前 return。
        try:
            self.stage.drawer_handle.setVisible(True)
        except Exception:
            log.debug("切换抽屉手柄可见性失败", exc_info=True)
        if kind == router.KIND_IMAGE and self.drawer.is_open():
            self._on_playlist_button(False)
        if kind == router.KIND_AUDIO:
            self._load_audio_tags()
        if kind == router.KIND_IMAGE:
            self._apply_image_settings()
            self._update_image_info()
        else:
            # 视频/音频模式：全屏按钮恢复「全屏」图标（#10 双模式同步）
            try:
                self.stage.image_bar.set_fullscreen(bool(self._fullscreen))
            except Exception:
                pass
        self.drawer.set_current(self._path)
        # ★ task-42：模式切换后**统一**确保边缘悬停定时器在工作（三模式一致）。
        self._sync_edge_hover_timer()

    # =====================================================================
    # 图片
    # =====================================================================
    def _zoom_for_one(self) -> float:
        """算出「原始大小 100%」对应的 video-zoom（log2 尺度）。"""
        import math

        h = self.engine.cached("height", 0) or 0
        if not h:
            return 0.0
        win_px = max(1, int(self.stage.video.height() * (self.stage.video.devicePixelRatioF() or 1.0)))
        # mpv 的 video-zoom 是在"适应窗口"基础上的额外缩放
        return max(-6.0, min(6.0, math.log2(max(1, win_px) / float(h))))

    def _zoom_image(self, factor: float):
        """图片缩放统一入口：走 Qt GPU 视口（本地矩阵），音视频才用 mpv。

        ★ task-43 rider（HEAD v1.0.40 既有缺陷修复）：
          原代码最后一行是 return self._zoom_image_mpv(factor, at_cursor) ——
          at_cursor 是未定义名，**空图态 / 解码中（has_image() 仍为 False）调用缩放
          必抛 NameError**（图片缩放按钮/快捷键/自动测试都会崩）。而 _zoom_image_mpv
          的签名本来就只接收 factor（缩放点对 mpv zoom 无意义，视口路径才用
          zoom_at_cursor），所以这里改为只传 factor。空图/解码中 → 走 mpv 分支且
          kind==IMAGE 时只是写一次 video-zoom，不会崩、不会改变任何 UI 状态。
        """
        _v = self._iv()
        if self._kind == router.KIND_IMAGE and _v is not None and _v.has_image():
            _v.zoom_at_cursor(float(factor))
            self._update_image_info()
            return
        return self._zoom_image_mpv(factor)

    def _zoom_image_mpv(self, factor: float):
        if self._kind != router.KIND_IMAGE:
            return
        zoom = self.engine.cached("video-zoom", 0.0) or 0.0
        import math

        zoom = max(-6.0, min(8.0, float(zoom) + math.log2(max(0.01, factor))))
        self.engine.set("video-zoom", zoom, MPV_FORMAT_DOUBLE)
        self._image_zoom = 2 ** zoom
        self._update_image_info()

    def _apply_image_settings(self):
        """把看图相关的配置推给视口（#1 滚轮方向、#8 取景框位置）。"""
        v = self._iv()
        if v is None:
            return
        try:
            # #1 硬性归正：**上滑 = 放大**，无条件。不再读取历史配置里的反向开关
            #    （旧配置若曾设为反向，会让人以为"又反了"）。永远保持标准行为。
            v.wheel_invert = False
            v.apply_minimap_position(str(self.cfg.get("image.minimap_position", "bottom_right")))
        except Exception:
            log.debug("应用看图配置失败", exc_info=True)

    def _iv(self):
        """取 Qt 图片视口（未接入时返回 None）。"""
        return getattr(self.stage, "image_view", None)

    def _show_zoom_osd(self):
        """#3/#4 缩放百分比提示 —— **只由用户主动操作触发**（滚轮/滑条/缩放按钮）。

        内部复位一律静默，所以不会出现"切图/镜像时被缩放 100% 顶掉"的情况。
        """
        v = self._iv()
        if v is None or not v.has_image() or self._kind != router.KIND_IMAGE:
            return
        self.show_osd("缩放: %d%%" % int(round(v.target_scale() * 100)), "zoom_in", ms=1000)

    def _on_image_failed(self, path: str, reason: str):
        """图片解不出来时给一句**明确**的话（而不是让用户对着黑屏猜）。"""
        self.show_osd("%s — %s" % (os.path.basename(path), reason), "info",
                      P["danger"], ms=2600)
        log.warning("图片加载失败: %s (%s)", path, reason)

    def _prefetch_neighbors(self):
        """把播放列表里当前图的**前后各一张**交给视口预解码。

        连续切图卡顿的主因就是"每切一张都要现场解码"；预解码后按方向键是命中缓存 ⇒ 0 等待。
        """
        v = self._iv()
        if v is None or self._kind != router.KIND_IMAGE:
            return
        try:
            items = [p for p in self.drawer.paths()
                     if router.kind_of(p) == router.KIND_IMAGE]
            if len(items) < 2:
                return
            i = [os.path.normcase(p) for p in items].index(os.path.normcase(self._path))
            # v1.0.20：预加载 **Index-1 / +1 / +2**（先下、再下下、最后上一张）
            v.set_neighbors(items[(i - 1) % len(items)],
                            items[(i + 1) % len(items)],
                            items[(i + 2) % len(items)])
        except Exception:
            log.debug("预解码相邻图失败", exc_info=True)

    def _iv_load(self, path: str):
        """把图片交给 Qt 视口（规范 §6：默认复位为适应窗口 + 镜像归零）。"""
        v = self._iv()
        if v is None:
            return
        keep = bool(self.cfg.get("image.keep_view_on_switch", False))
        v.load(path, reset=not keep)
        was_image = (self._kind == router.KIND_IMAGE)
        v.set_rotation(0)
        v.set_flip(False)
        self._prefetch_neighbors()
        # #4 切图提示（用户规范）：显示"第 X / Y 张"，且**不会被缩放消息顶掉**
        #    （内部复位已静默，缩放 OSD 只由用户操作触发）。
        if was_image:
            try:
                items = self.drawer.paths()
                idx = [os.path.normcase(x) for x in items].index(
                    os.path.normcase(path)) + 1
                self.show_osd("第 %d / %d 张" % (idx, max(1, len(items))),
                              "image", ms=1000)
            except Exception:
                pass
        self._image_rotation = 0
        self._image_flip = False
        try:
            self.stage.image_bar.set_flip_state(False)
        except Exception:
            pass
        self._update_image_info()

    def _iv_fit(self):
        """#7 「适应窗口」：直接把视口复位（缩放 100% + 平移归零 + 弹簧清零）。"""
        v = self._iv()
        if v is None or not v.has_image():
            return
        v.reset_view()
        try:
            self.stage.image_bar.set_zoom(1.0)
        except Exception:
            pass
        self.show_osd("适应窗口", "fit", ms=1200)      # #9 OSD 反馈
        self._update_image_info()

    def _emit_user_zoomed(self):
        """把"这是用户主动缩放"广播出去（#4 只有它会触发缩放 OSD）。"""
        v = self._iv()
        if v is not None:
            try:
                v.user_zoomed.emit()
            except Exception:
                pass

    def _on_sort_requested(self, key: str):
        """#7 排序：**真正重排底层播放序列**，并保持当前文件仍被选中。

        排序键沿用抽屉里那套（name / path / duration / kind），底层顺序变了以后，
        ←/→ 翻页、热区切图、列表展示三者才会 100% 一致。
        """
        try:
            self.drawer.resort(key, self._path)
            self.drawer.set_current(self._path)
        except Exception:
            log.debug("重排播放序列失败", exc_info=True)

    def _iv_zoom(self, factor: float):
        """控制栏放大/缩小：朝**当前光标**缩放（规范 §1.2 锚定缩放）。"""
        v = self._iv()
        if self._kind == router.KIND_IMAGE and v is not None and v.has_image():
            v.zoom_at_cursor(float(factor))
            self._emit_user_zoomed()       # #3 按钮缩放：立即提示
            self._update_image_info()
            return
        self._zoom_image(float(factor))

    def _iv_set_scale(self, scale: float):
        """滑条 → 视口。

        #3 关键区别：**用户正在拖滑条时走即时接口**（跳过弹簧，直接重绘 ⇒ 物理跟手）；
        滑条松手后的静默同步才走平滑弹簧。
        """
        v = self._iv()
        if self._kind == router.KIND_IMAGE and v is not None and v.has_image():
            try:
                dragging = self.stage.image_bar.zoom_slider.is_dragging()
            except Exception:
                dragging = False
            if dragging:
                v.set_scale_immediate(float(scale))
                self._show_zoom_osd()      # #3 拖滑条：0ms 刷新 + 1.0s 淡出
            else:
                v.set_target_scale(float(scale))
            self._update_image_info()
            return
        self._zoom_image(float(scale))

    def _set_image_fit(self, mode: str):
        if mode == "one":
            z = self._zoom_for_one()
        else:
            z = 0.0
        self.engine.set("video-zoom", z, MPV_FORMAT_DOUBLE)
        self.engine.set("video-pan-x", 0.0, MPV_FORMAT_DOUBLE)
        self.engine.set("video-pan-y", 0.0, MPV_FORMAT_DOUBLE)
        self._image_zoom = 2 ** z
        self.cfg.set("ui.image_fit", mode)
        self._update_image_info()

    def _set_image_fit_qt(self, mode: str) -> bool:
        """「适应窗口」/「原始大小」：Qt 视口实现（原始大小 = 1 图片像素对 1 屏幕像素）。"""
        v = self._iv()
        if self._kind != router.KIND_IMAGE or v is None or not v.has_image():
            return False
        if mode == "one":
            k = v._k_fit or 1.0
            v.set_target_scale(1.0 / k if k > 0 else 1.0)
        else:
            v.reset_view()
        self.cfg.set("ui.image_fit", mode)
        self._update_image_info()
        return True

    def _rotate_image(self, deg: int):
        if self._kind != router.KIND_IMAGE:
            return
        v = self._iv()
        if v is not None and v.has_image():
            v.set_rotation((v.rotation() + int(deg)) % 360)
            self._image_rotation = v.rotation()
            self._update_image_info()
            self.show_osd("向左旋转 90°" if int(deg) < 0 else "向右旋转 90°",
                          "rotate_left" if int(deg) < 0 else "rotate_right", ms=1200)
            return
        self._image_rotation = (self._image_rotation + deg) % 360
        self.engine.set("video-rotate", self._image_rotation, MPV_FORMAT_INT64)
        self.show_osd(f"旋转 {self._image_rotation}°", "rotate_right")

    def _flip_image(self):
        if self._kind != router.KIND_IMAGE:
            return
        v = self._iv()
        if v is not None and v.has_image():
            v.set_flip(not v.flip())
            self._image_flip = v.flip()
            try:
                self.stage.image_bar.set_flip_state(self._image_flip)
            except Exception:
                pass
            self._update_image_info()
            self.show_osd("水平镜像" if self._image_flip else "恢复正常", "flip_h", ms=1200)
            return
        self._image_flip = not self._image_flip
        self.engine.set("video-flip", "yes" if self._image_flip else "no", MPV_FORMAT_STRING)
        self.show_osd("水平镜像 开" if self._image_flip else "水平镜像 关", "wall")

    def _hot_switch_to(self, path: str) -> bool:
        """列表内**热切图**：绕开 open_path 的冷启动重活，主线程只做 5 件事。

        ★ 性能剖析实证（open_path_share_pct = 100）：切图时那 100~400ms 的主线程阻塞
          全部来自 `open_path()`，其中两个大头是：
            ① `cfg.push_recent()` + `cfg.save_if_dirty()` —— **每切一张就写一次配置文件**
            ② `self.engine.load(path)` —— **每切一张一次同步 mpv IPC**（图片根本不需要）
          另外还有目录重扫、播放列表重建、全量状态重置等，都是"打开文件"才需要的。

        热切图只做：
          ① 更新当前索引/路径（`self._path` + 列表选中项）
          ② 把图交给视口（命中预加载缓存 ⇒ 0ms 上屏；否则后台降采样解码）
          ③ 立即更新标题与计数（"第 X / Y 张 · 宽x高"）
          ④ 重活（EXIF 等附属信息）交给后台，完成后静默回填
        """
        if self._kind != router.KIND_IMAGE:
            return False
        if not path or router.kind_of(path) != router.KIND_IMAGE or not os.path.isfile(path):
            return False
        try:
            self._path = path
            self._update_window_title(path)
            self._iv_load(path)                     # ② 缓存优先，0ms 上屏
            self.drawer.set_current(path)           # ① 列表选中项
            # ③ 标题/计数延到**下一轮事件循环**结算：本函数要保证主线程尽快返回，
            #    任何次要刷新都不该算进这次切图的阻塞时间里。
            QTimer.singleShot(0, self._update_image_info)
            self._update_empty_state()
            self._schedule_image_meta(path)         # ④ 后台补附属信息
            return True
        except Exception:
            log.debug("热切图失败，回落 open_path", exc_info=True)
            return False

    def _schedule_image_meta(self, path: str):
        """把 os.stat / EXIF 这类磁盘活推到后台，完成后再回填信息面板。"""
        try:
            self._meta_pool.start(_MetaTask(path, self._meta_sig))
        except Exception:
            log.debug("投递附属信息任务失败", exc_info=True)

    def _on_image_meta(self, path: str, meta: dict):
        """后台附属信息回填（只在仍是当前图时更新，避免过期覆盖）。"""
        if not path or os.path.normcase(path) != os.path.normcase(self._path or ""):
            return
        self._img_meta = meta
        try:
            self._update_image_info()
        except Exception:
            log.debug("回填图片信息失败", exc_info=True)

    def _step_image(self, delta: int):
        """列表内切图（←/→、热区、控制栏上/下一张）—— 走**热切图**，不碰 open_path。"""
        items = self.drawer.paths()
        if not items:
            return
        try:
            i = [os.path.normcase(p) for p in items].index(os.path.normcase(self._path))
        except ValueError:
            i = 0
        j = (i + delta) % len(items)
        if self._hot_switch_to(items[j]):
            return
        self.open_path(items[j], remember_dir=False)

    def _update_window_title(self, path: str):
        """#7 标题栏显示「文件名 · 像素」。"""
        self._title_base = os.path.basename(path or "")
        self._title_px = ""
        self.titlebar.set_title(self._title_base or APP_NAME)

    def _title_text(self) -> str:
        # v1.0.29 ② / task-69：**音频/图片严禁拼分辨率** —— mp3 没有画面尺寸，
        # 之前把内嵌封面/上一部视频的 1920x1080 带进了标题栏（用户截图）。
        # ⚠️ 原实现先 return base 后赋值 base ⇒ NameError 被 except 吞掉，音频照样
        #    走到底部拼 px。现在先算 base，再按模式硬收口。
        base = getattr(self, "_title_base", "") or os.path.basename(self._path or "")
        try:
            # 音频严禁分辨率；图片保留**自身**尺寸（e 判据允许"图片尺寸显示（若有）"），
            # open_path 已先清空，随后 _update_image_info 写入的是当前图片的尺寸。
            if self._kind == router.KIND_AUDIO:
                return base or APP_NAME
        except Exception:
            pass
        px = getattr(self, "_title_px", "")
        if base and px:
            return "%s · %s" % (base, px)
        return base or APP_NAME

    def _schedule_video_title_px(self, attempt: int = 0):
        """★ task-69：视频首帧加载时 width/height 可能还没进 mpv 缓存（首次启动视频
        标题会暂时只有文件名）。这里用少量 singleShot 重试补写，拿到尺寸就停；
        只对视频生效，音频/图片一律不写（避免又把分辨率带到 MP3 标题）。
        """
        try:
            if self._closing or self._kind != router.KIND_VIDEO or not self._path:
                return
            w = int(self.engine.cached("width", 0) or 0)
            h = int(self.engine.cached("height", 0) or 0)
            if w and h:
                self._set_title_px(w, h)
                return
            if attempt < 10:
                QTimer.singleShot(120, lambda: self._schedule_video_title_px(attempt + 1))
        except Exception:
            log.debug("补写视频标题分辨率失败", exc_info=True)

    def _set_title_px(self, w, h):
        px = "%d×%d" % (int(w), int(h)) if (w and h) else ""
        if px != getattr(self, "_title_px", None):
            self._title_px = px
            try:
                self.titlebar.set_title(self._title_text())
            except Exception:
                log.debug("更新标题失败", exc_info=True)

    def _update_image_info(self):
        if self._kind != router.KIND_IMAGE or not self._path:
            return
        try:
            size = os.path.getsize(self._path)
        except OSError:
            size = 0
        # 图片模式下 mpv 已停止 ⇒ 尺寸/缩放一律取自 Qt 视口
        v = self._iv()
        if v is not None:
            w, h = v.natural_size()
            self._image_zoom = float(v.scale())
        else:
            w = int(self.engine.cached("width", 0) or 0)
            h = int(self.engine.cached("height", 0) or 0)
        self._image_natural = (w, h)
        self._set_title_px(w, h)          # #7 标题栏显示像素
        # #12 信息浮层开着时，切图必须**同步刷新**（原来只在按 Tab 的那一刻取一次）
        try:
            ov = self.stage.media_info
            if ov.isVisible():
                title, rows = self._collect_media_info()
                ov.set_info(title, rows)
        except Exception:
            log.debug("刷新信息浮层失败", exc_info=True)
        items = self.drawer.paths()
        try:
            idx = [os.path.normcase(p) for p in items].index(os.path.normcase(self._path)) + 1
        except ValueError:
            idx = 1
        self.stage.image_bar.set_info(os.path.basename(self._path), w, h, size,
                                      self._image_zoom, idx, max(1, len(items)))

    # =====================================================================
    # 音频
    # =====================================================================
    def _load_audio_tags(self):
        tags_ = tags.read_tags(self._path)
        title = tags_.title or self.engine.cached("media-title", "") or os.path.basename(self._path)
        # v1.0.27 旧 AudioCard 引用已移除（实例已物理销毁）
        # v1.0.27 旧 AudioCard 引用已移除（实例已物理销毁）
        # v1.0.26 音乐视图：封面 / 元信息 / 规格 / 歌词 / 进度
        try:
            av = self.stage.audio_view
            av.set_cover(tags_.cover)
            sub_parts = []
            if tags_.artist:
                sub_parts.append("歌手：%s" % tags_.artist)
            if tags_.album:
                sub_parts.append("专辑：%s" % tags_.album)
            av.set_meta(title, "    ".join(sub_parts))
            spec = []
            ext = os.path.splitext(self._path)[1].lstrip(".").upper()
            if ext:
                spec.append(ext)
            params = self.engine.cached("audio-params", None)
            if isinstance(params, dict) and params.get("samplerate"):
                spec.append("%g kHz" % (float(params["samplerate"]) / 1000.0))
            bit = self.engine.cached("audio-bitrate", None)
            if bit:
                spec.append("%d kbps" % int(float(bit) / 1000.0))
            av.set_spec(" · ".join(spec))
            av.set_lyrics(self._path)
            av.set_position(float(self.engine.cached("time-pos", 0.0) or 0.0))
        except Exception:
            log.debug("音乐视图灌数据失败", exc_info=True)

    # =====================================================================
    # 播放控制
    # =====================================================================
    def _drive_audio_view(self):
        """把播放进度与播放态喂给音乐视图（频谱与歌词靠它驱动，无额外 IPC）。"""
        try:
            if self._kind != router.KIND_AUDIO:
                return
            av = self.stage.audio_view
            av.set_position(float(self.engine.cached("time-pos", 0.0) or 0.0))
            av.set_playing(not bool(self._paused))
        except Exception:
            pass

    def toggle_pause(self):
        if not self._path:
            self.open_files_dialog()
            return
        self.engine.toggle_pause()

    def stop(self):
        self._save_position()
        self.engine.cmd("stop")
        self._position = 0.0
        self.capsule.set_times(0.0, self._duration)

    def set_speed(self, speed: float):
        speed = max(float(self.cfg.get("playback.speed_min", 0.25)),
                    min(float(self.cfg.get("playback.speed_max", 4.0)), float(speed)))
        self.engine.set_speed(speed)
        self.capsule.set_speed(speed)
        self.show_osd(f"{speed:g}x", "speed")

    def _on_seek_started(self):
        """按下进度条：进入"拖拽态"（task-20 ②）。

        从这里开始到松手为止：**一次 seek 都不发**，并且屏蔽 mpv 的 time-pos 回写
        （否则滑块/时间文本会和鼠标抢位）。
        """
        self._is_seeking = True

    def _on_seek_preview(self, seconds: float):
        """拖动过程中的**预览**：只改左侧时间文本。

        task-20 ②（用户⑧ "拖进度条鬼畜"）：以前这里每 80ms 向 mpv 发一次
        `seek_absolute(precise=False)`（"拖到哪跳到哪"）——实测拖一次能打出十几条
        seek，解码器被打死、画面来回跳。现在拖动期**零 seek**（判据：拖动期 seek
        计数 = 0），松手才由 `_on_seek_committed` 发唯一一次精确 seek。
        """
        self.capsule.time_label.setText(f"{fmt_time(seconds)} / {fmt_time(self._duration)}")

    def _on_seek_committed(self, seconds: float):
        # 松手 → **本次拖拽唯一一次** seek，且要帧级准确
        self.engine.seek_absolute(seconds, precise=True)
        # 先解锁再刷显示：解锁后 time-pos 回写接管的正是"松手后的真实位置"
        self._is_seeking = False
        self.capsule.set_times(seconds, self._duration)
        if self.cfg.get("ui.osd", True):
            self.show_osd(f"{fmt_time(seconds)} / {fmt_time(self._duration)}", "clock", ms=800)

    def _on_volume(self, value: float):
        self.engine.set("volume", float(value), MPV_FORMAT_DOUBLE)
        self.cfg.set("audio.volume", float(value))
        self.cfg.set("audio.mute", False)
        if value > 0.5 and self.engine.cached("mute", False):
            self.engine.set("mute", False, MPV_FORMAT_FLAG)
        if self.cfg.get("ui.osd", True):
            self.show_osd(f"音量 {int(value)}%", "volume_high" if value > 0.5 else "volume_mute", ms=700)

    def _on_mute(self, muted: bool):
        self.engine.set("mute", bool(muted), MPV_FORMAT_FLAG)
        self.cfg.set("audio.mute", bool(muted))
        self.show_osd("静音" if muted else "取消静音", "volume_mute" if muted else "volume_high", ms=700)

    def _step_frame(self, delta: int):
        if self._paused:
            self.engine.cmd("frame-step" if delta > 0 else "frame-back-step")
        else:
            self.engine.set("pause", True, MPV_FORMAT_FLAG)
            self.engine.cmd("frame-step" if delta > 0 else "frame-back-step")

    def _step_file(self, delta: int):
        items = self.drawer.paths()
        if not items:
            return
        try:
            i = [os.path.normcase(p) for p in items].index(os.path.normcase(self._path))
        except ValueError:
            i = 0
        j = i + delta
        if j < 0 or j >= len(items):
            mode = str(self.cfg.get("playback.loop_mode", "sequence"))
            if mode == "sequence" and j >= len(items):
                self.show_osd("已经是最后一个", "info")
                return
            j = j % len(items)
        self.open_path(items[j], remember_dir=False)

    def _on_loop_mode(self, mode: str):
        self.cfg.set("playback.loop_mode", mode)
        labels = {"sequence": "顺序播放", "loop": "列表循环", "single": "单个循环", "random": "随机播放"}
        self.engine.set("loop-file", "inf" if mode == "single" else "no", MPV_FORMAT_STRING)
        self.show_osd(labels.get(mode, mode), "loop")

    # =====================================================================
    # 长按倍速（核心体验）
    # =====================================================================
    def _boost_target(self) -> float:
        """长按倍速的目标值（两种模式，见 config 的 `playback.long_press_mode`）。

        * `multiply`（默认）：**原倍速基础快进** —— 当前倍速 × 用户设定的倍率。
          例：2x 长按 ×2 → 4x；3x 长按 ×2 → 6x；松开恢复原倍速。
        * `fixed`：**固定档位快进** —— 取"第一个比当前倍速大的档"。
          例：档位 2/5/10 时，0.5x→2x、2x→5x、6x→10x（已是最高档就停在最高档）。

        返回**正值**；方向由调用方按 `_boost_dir` 取符号（左方向键 = 倒放）。
        """
        mode = str(self.cfg.get("playback.long_press_mode", "multiply") or "multiply")
        base = abs(self._speed) if abs(abs(self._speed) - 1.0) > 1e-6 else 1.0
        smin = float(self.cfg.get("playback.speed_min", 0.25))
        smax = float(self.cfg.get("playback.speed_max", 16.0))
        if mode == "fixed":
            tiers = parse_speed_tiers(self.cfg.get("playback.long_press_tiers", "2, 5, 10"))
            # 取"第一个比当前大的档"；已经比所有档都快时**保持原样**
            # （退回最高档会变成"长按反而变慢"，那是 bug 不是特性）
            target = next((t for t in tiers if t > base + 1e-6), base)
        else:
            target = base * float(self.cfg.get("playback.long_press_multiplier", 2.0))
        return max(smin, min(smax, target))

    def _begin_speed_boost(self):
        if self._boost:
            return
        self._boost = True
        self._pre_boost_speed = self._speed if abs(abs(self._speed) - 1.0) > 1e-6 else 1.0
        target = self._boost_target()
        if self._boost_dir < 0:
            # 左方向键 = 快退（"倒放"）。
            # ⚠️⚠️ mpv **没有**负速度：实测 5 种组合（裸设 / aid=no / audio=no / video-sync=no /
            #      字符串形式）全部把 speed 读回 1.0，位置照常向前 —— 底层就没有倒放这条路。
            #      所以"倒放"只能模拟，而模拟的关键是**先暂停**：
            #      不暂停的话"后退一步 + 继续往前播"会互相拉扯，看起来就是
            #      "闪一下又往前播"（用户原话："闪烁一次就想往前播放一次，倒退不像倒退"）。
            #      暂停之后按固定节奏做**精确后退**（实测 12 步 × -0.2s 正好退 2.4s，跟得上），
            #      位置严格单调后退 ⇒ 视觉上就是连贯倒放。
            self._rewind_rate = target
            self._rewind_was_paused = bool(self._paused)
            self._rewind_last_ts = 0.0
            self.engine.set("pause", True, MPV_FORMAT_FLAG)
            self._rewind_timer.start()
            self._rewind_tick()                 # 立刻退一步，别等第一个周期
            self.capsule.set_speed(-target)     # 胶囊上显示 ◀2x
            self.show_osd(f"{target:g}x 倒放中…", "rewind", P["accent"], ms=3_600_000)
            return
        self.engine.set_speed(target)
        self._speed = target
        self.capsule.set_speed(target)
        # ⚠️ 用户要求：长按期间**一直显示**「快进中」，不要一闪而过。
        #    做法是给一个超长的 ms（OSD 会一直挂着），松开时用下面那条
        #    「恢复正常速度」把它替换掉 —— 内容变了就会重新计时并按时消失。
        base = abs(self._pre_boost_speed)
        word = "快退中…" if target < 0 else "快进中…"
        label = (f"{base:g}x → {abs(target):g}x {word}" if abs(base - 1.0) > 1e-6
                 else f"{abs(target):g}x {word}")
        self.show_osd(label, "rewind" if target < 0 else "forward", P["accent"], ms=3_600_000)

    def _end_speed_boost(self):
        if not self._boost:
            return
        self._boost = False
        self._rewind_timer.stop()
        # 倒放期间强制暂停过：松开要**还原成原来的状态**（原本在播就继续播）
        if getattr(self, "_rewind_was_paused", None) is False:
            self.engine.set("pause", False, MPV_FORMAT_FLAG)
        self._rewind_was_paused = None
        restore = self._pre_boost_speed if abs(self._pre_boost_speed - 1.0) > 1e-6 else 1.0
        self.engine.set_speed(restore)
        self._speed = restore
        self.capsule.set_speed(restore)
        # 自己上报"从松开按键到速度真的回到 1.0x"的耗时 ——
        # 外部按 50ms 轮询采样只能给出 ≥50ms 粒度的下界, 会把 <150ms 的合格结果测成不确定。
        if self._release_ts:
            self._restore_latency_ms = (time.perf_counter() - self._release_ts) * 1000
            self._release_ts = 0.0
        self.show_osd("恢复正常速度", "play", P["text"], ms=600)

    def _on_long_press_timer(self):
        if self._right_down and not self._boost:
            self._begin_speed_boost()

    def _rewind_tick(self):
        """倒放的一小步：**暂停状态下**精确后退。

        步长按**真实耗时**算（`now - 上次 tick`），而不是按定时器标称间隔：
        精确后退每步耗时是浮动的（实测 40~90ms），按标称 60ms 算会导致
        "要 2 倍速、实际只有 1.2 倍速"。按真实 dt 算，速率就准了。
        """
        if not self._boost or self._boost_dir >= 0:
            return
        now = time.perf_counter()
        last = getattr(self, "_rewind_last_ts", 0.0)
        self._rewind_last_ts = now
        dt = now - last if last else self._rewind_timer.interval() / 1000.0
        dt = max(0.02, min(0.25, dt))              # 夹一下，防止卡顿时一次退太多
        step = max(0.02, float(getattr(self, "_rewind_rate", 2.0)) * dt)
        try:
            self.engine.seek_relative(-step, precise=True)
        except Exception:
            log.debug("倒放后退失败", exc_info=True)

    # =====================================================================
    # 字幕
    # =====================================================================
    def _apply_sub_style(self, prop: str, value):
        self.engine.set(prop, value)
        self.cfg.save_if_dirty()

    def _on_sub_border(self, color: str):
        if color == "*stroke*":
            size = float(self.cfg.get("subtitle.border_size", 2.4))
            self.engine.set("sub-border-size", size, MPV_FORMAT_DOUBLE)
        else:
            self.engine.set("sub-border-color", color)

    def _set_sub_delay(self, seconds: float):
        self.engine.set("sub-delay", float(seconds), MPV_FORMAT_DOUBLE)
        self.show_osd(f"字幕偏移 {seconds:+.2f}s", "clock", ms=700)

    def _set_sub_visible(self, on: bool):
        self.engine.set("sub-visibility", bool(on), MPV_FORMAT_FLAG)
        self.cfg.set("subtitle.enabled", bool(on))

    def _set_sub_track(self, sid: int):
        self.engine.set("sid", int(sid) if int(sid) >= 0 else "no", MPV_FORMAT_STRING)
        self.cfg.save_if_dirty()

    @staticmethod
    def _int_or(value, default: int = -1) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _refresh_subtitle_tracks(self):
        tracks = self.engine.cached("track-list", []) or []
        rows: list[tuple[int, str]] = []
        for t in tracks:
            if t.get("type") != "sub":
                continue
            sid = self._int_or(t.get("id"), 0)
            label = t.get("title") or t.get("lang") or f"轨道 {sid}"
            if t.get("external"):
                label = "外挂 · " + str(label)
            rows.append((sid, str(label)))
        # 兜底：把同目录同名外挂字幕也列出来（即使 mpv 还没挂上）
        if self._path:
            known = {r[1] for r in rows}
            for st in subtitles.find_subtitles(self._path):
                if st.label not in known:
                    rows.append((-2, f"待加载 · {st.label} ({st.name})"))
        sid = self._int_or(self.engine.cached("sid", -1), -1)
        visible = bool(self.engine.cached("sub-visibility", True))
        if self.subtitle_panel is not None:
            self.subtitle_panel.set_tracks(rows, sid, visible)

    def _ensure_subtitle_panel(self):
        """按需构建字幕浮层（启动时不建, 见 __init__ 里的说明）。"""
        if self.subtitle_panel is None:
            self.subtitle_panel = SubtitlePanel(self.cfg, self.stage.subtitle_host)
            self.stage.subtitle_host.layout().addWidget(self.subtitle_panel)
        return self.subtitle_panel

    def toggle_subtitle_panel(self, on: bool | None = None):
        show = (self.subtitle_panel is None or not self.subtitle_panel.isVisible()) \
            if on is None else bool(on)
        if not show and self.subtitle_panel is None:
            return
        self._ensure_subtitle_panel()
        # ⚠️ 父容器不显示时子控件 setVisible(True) 是无效的（本项目真实踩过）
        self.stage.subtitle_host.setVisible(show)
        self.subtitle_panel.setVisible(show)
        if show:
            self._refresh_subtitle_tracks()
            self.subtitle_panel.sync_from_config()
            self.stage.subtitle_host.raise_()
            self.stage.bottom_layer.raise_()

    def load_subtitle_dialog(self):
        f, _ = QFileDialog.getOpenFileNames(
            self, "加载字幕", os.path.dirname(self._path or ""),
            "字幕文件 (*.srt *.ass *.ssa *.vtt *.sub *.idx *.sup);;所有文件 (*)")
        for p in f:
            self.engine.cmd("sub-add", p, "select")
        if f:
            self.show_osd(f"已加载 {len(f)} 个字幕", "subtitle")

    def _auto_sub_notice(self):
        if not self._path or self._kind != router.KIND_VIDEO:
            return
        if not self.cfg.get("subtitle.auto_load", True):
            return
        found = subtitles.find_subtitles(self._path)
        if found:
            self.show_osd(f"已自动挂载字幕：{found[0].label}", "subtitle", ms=2000)

    # =====================================================================
    # 截图
    # =====================================================================
    def take_screenshot(self):
        if not self._path:
            return
        d = os.path.join(os.path.expanduser("~"), "Pictures", "至简Player")
        os.makedirs(d, exist_ok=True)
        base = os.path.splitext(os.path.basename(self._path))[0]
        stamped = time.strftime("%Y%m%d-%H%M%S")
        target = os.path.join(d, f"{base}_{stamped}.png")
        r = self.engine.cmd("screenshot-to-file", target, "video")
        if r >= 0:
            self.show_osd("截图已保存", "screenshot")
            win.reveal_in_explorer(target)
        else:
            self.show_osd("截图失败", "info", P["danger"])

    # =====================================================================
    # 播放列表 / 设置 / 全屏
    # =====================================================================
    def _on_playlist_button(self, on: bool):
        if not on:
            self._drawer_auto_opened = False
            self._drawer_leave_ms = 0
        self.drawer.set_open(on, animate=True)
        self.titlebar.set_playlist_checked(on)
        self.capsule.set_list_checked(on)

    def _on_playlist_button_manual(self):
        """用户**手动**点标题栏/胶囊/手柄/按 L —— 这种要常驻，不自动收回。"""
        self._drawer_auto_opened = False
        self._on_playlist_button(not self.drawer.is_open())

    def _on_playlist_button_from_ui(self, on: bool):
        """胶囊上的播放列表按钮（带选中态的那种）。

        直接**用按钮给的 on**，而不是"取反当前抽屉状态"：两者不一致时（例如
        抽屉是碰边缘自动开的、按钮还没选中）取反会立刻又关掉，看起来就是"按钮没用"。
        """
        self._drawer_auto_opened = False
        self._drawer_leave_ms = 0
        self._on_playlist_button(bool(on))

    def _on_drawer_state(self, on: bool):
        self.titlebar.set_playlist_checked(on)
        self.capsule.set_list_checked(on)
        self.cfg.set("ui.drawer_open", on)
        self.stage.set_drawer_visible(on)
        # v1.0.21：抽屉展开 ⇒ **立即失效热区**（否则点排序按钮/条目会被当成切下一张）；
        # 抽屉完全关闭后恢复。同时把抽屉抬到热区之上。
        try:
            self.stage.set_hotspots_enabled(not on)
        except Exception:
            log.debug("切换热区可用性失败", exc_info=True)
        if not on:
            # ★★ task-31 闸门③：**任何**收起动作（手柄 / Esc / 外部空白 / 底栏按钮 / 自动收回 /
            #   进全屏 ……）都会走到这个唯一回调 ⇒ 冷却与解除武装在这里一次性生效，
            #   不依赖各入口各自记得调用。
            self._close_cd_until = time.monotonic() + self.EDGE_CD_S
            self._edge_rearmed = False
            self._hot_since = None
            self._suppress_edge_hover()

    def _toggle_drawer(self):
        self._on_playlist_button(not self.drawer.is_open())

    def toggle_light_dark(self):
        """日夜一键切换（标题栏那个太阳/月亮按钮）。

        记住"上一次的深色主题"：从深色切浅色再切回来时，回到用户原来那套深色，
        而不是一律跳回默认 —— 不然换了琥珀色的人点一下日夜就被重置了。
        """
        from . import theme as _theme

        # ⚠️⚠️ 只在这两套之间切，**不再"记住上一次的深色主题"**。
        #    旧逻辑会回到历史主题（比如 `amber`），于是切到深色后界面里
        #    冒出一个**琥珀黄按钮** —— 用户原话"白切黑依旧会有莫名其妙的黄色出现"。
        #    既然设置界面只提供「浅色 / 深色」两个模式，日夜按钮就必须和它一致。
        self.apply_theme("qq-dark" if _theme.is_light() else "qq-light")

    # =====================================================================
    # 换肤（真实生效）
    # =====================================================================
    def apply_theme(self, name: str, save: bool = True):
        """运行时换肤：改调色板 → 重设全局样式表 → 全窗重绘 → 落盘。

        图标是**按颜色缓存**的（`icons.render_pixmap` 的 key 含颜色），
        所以换色后旧缓存不会被复用，不需要手动清缓存。
        """
        from . import theme as _theme

        if not _theme.apply_theme(name):
            return False
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(_theme.build_stylesheet())
            pal = app.palette()
            pal.setColor(QPalette.Window, _theme.qt_color(_theme.P["bg_base"]))
            pal.setColor(QPalette.Base, _theme.qt_color(_theme.P["bg_deep"]))
            pal.setColor(QPalette.Highlight, _theme.qt_color(_theme.P["accent"]))
            pal.setColor(QPalette.HighlightedText, _theme.qt_color(_theme.P["text_on_accent"]))
            app.setPalette(pal)
        # ★ task-72（用户 ⑪）：玻璃皮肤下标题栏必须**真正透明**（透出窗后模糊背景），
        #    仅靠 QSS/子控件透明不够 —— 顶层窗口自己的 QPalette.Window 还会刷一层
        #    bg_base（实测玻璃皮肤标题栏仍是 #F7F8FA 纯色块）。这里把窗口调成
        #    WA_TranslucentBackground + 透明 Window 角色；其它主题强制恢复不透明。
        try:
            self.setAttribute(Qt.WA_TranslucentBackground, bool(_theme.is_glass_skin()))
            self.setAutoFillBackground(False)
            own = self.palette()
            own.setColor(QPalette.Window, _theme.qt_color(
                "#00000000" if _theme.is_glass_skin() else _theme.P["bg_base"]))
            self.setPalette(own)
            self.update()
        except Exception:
            log.debug("设置窗口透明背景失败", exc_info=True)
        # ⚠️ GlassFrame 的填充色是**构造时快照**的字符串，光重绘不会变色
        #    —— 必须让它们按新调色板重新取色（否则浅色主题下
        #    标题栏/抽屉都白了、**只有胶囊还是深色**）。
        from . import widgets as _widgets

        # ⚠️⚠️ 换肤**只走这一个入口**：登记过的动态样式 + 自绘控件 + 面板钩子 + 菜单，
        #      全部由 notify_theme_changed() 广播。以前是"谁想起来谁自己刷一下"，
        #      于是每轮都能被用户找出一两处没跟上的（"设置图标淡""空状态文字浅"…）。
        n = _widgets.notify_theme_changed(QApplication.instance())
        log.info("换肤广播：重放 %d 项", n)
        self._apply_acrylic_alpha()
        # 自绘控件读的是 P[...]，样式表管不到它们，必须显式重绘
        for w in [self, self.stage, self.capsule, self.titlebar, self.drawer,
                  self.stage.video, self.stage.empty_state, self.stage.drawer_handle]:
            try:
                w.update()
            except Exception:
                pass
        if self.settings is not None:
            self.settings.reload_theme()
        # 标题栏的日夜按钮图标要跟着翻（深色显示太阳、浅色显示月亮）
        self.titlebar.set_light(_theme.is_light())
        # ★★ task-72：**最后**再落一次透明窗口调色板。前面 app.setStyleSheet() /
        #     _apply_acrylic_alpha() 的 re-polish 会把 widget palette 覆盖回 bg_base
        #     （实测玻璃皮肤标题栏仍是 #F7F8FA 纯色块，手动 setPalette 立刻变透明）。
        try:
            glass = bool(_theme.is_glass_skin())
            self.setAttribute(Qt.WA_TranslucentBackground, glass)
            self.setAutoFillBackground(False)
            win_color = _theme.qt_color("#00000000" if glass else _theme.P["bg_base"])
            pal = self.palette()
            pal.setColor(QPalette.Window, win_color)
            self.setPalette(pal)
            app_pal = QApplication.palette()
            app_pal.setColor(QPalette.Window, win_color)
            QApplication.setPalette(app_pal)
            self.update()
        except Exception:
            log.debug("落定透明窗口调色板失败", exc_info=True)
        self.apply_window_mask()          # ★ task-87：换肤后重算圆角遮罩（半径可能随主题变）
        # ★★ task-97：换肤后**重烘焙歌词投影** —— 浅色（亮底深字）要彻底关掉黑影，
        #   否则切主题后会残留深色模式预烘焙的投影贴图 ✗
        try:
            _ly = getattr(getattr(self.stage, "audio_view", None), "lyrics", None)
            if _ly is not None:
                _ly._rebuild_shadows()
                _ly.update()
        except Exception:
            log.debug("换肤后重烘焙歌词投影失败", exc_info=True)
        # ★★ task-87（用户问题 1）：**换肤后设置面板必须重新定位** ——
        #   未拖拽 ⇒ 精确居中；拖拽过 ⇒ 保持相对中心偏移（此前会因 QSS 尺寸变化
        #   被布局器甩到左下角 ✗）。这里连做两帧：一帧交给样式/布局生效，一帧实际落位。
        if self._settings_open():
            try:
                self._fit_settings()
                QTimer.singleShot(0, self._fit_settings)
            except Exception:
                log.debug("换肤后重排设置面板失败", exc_info=True)
        if save:
            self.cfg.set("ui.theme", name)
            self.cfg.save()
        self.show_osd(f"已切换到「{_theme.THEMES[name]['label']}」", "palette",
                      _theme.P["accent"], ms=1200)
        return True

    def _clear_playlist(self):
        self.drawer.clear()
        self.show_osd("播放列表已清空", "trash_all")

    def _ensure_settings(self):
        """按需构建设置面板（288 个控件, 启动时不建 —— 见 __init__ 里的说明）。"""
        if self.settings is None:
            self.settings = SettingsPanel(self.cfg, self.stage.settings_host)
            self.settings.hotkey_changed.connect(self._on_hotkey_changed)
            self.settings.reset_requested.connect(self._on_settings_reset)
            self.settings.closed.connect(lambda: self.toggle_settings(False))
            self.settings.theme_requested.connect(self.apply_theme)
            # ★ task-76：用户拖动过面板 ⇒ 再次打开时保留位置（不强行回中）
            def _on_panel_dragged():
                """★ task-87：**拖拽结束的瞬间**就锁定「相对中心偏移」。

                ⚠️ 必须在这里算：换肤会在 `host.setFixedSize()` 时触发 QVBoxLayout 重排，
                把面板挪回布局默认位置（实测挪到 (0,0)）⇒ 事后再读 `settings.pos()`
                反推偏移会得到 (-175,0) ✗（我第一版就是这样，换肤后面板跳到左上角）。"""
                self._settings_dragged = True
                try:
                    W, H = int(self.stage.width()), int(self.stage.height())
                    pw, ph = int(self.settings.width()), int(self.settings.height())
                    cx0, cy0 = max(0, (W - pw) // 2), max(0, (H - ph) // 2)
                    self._settings_rel = (int(self.settings.x()) - cx0,
                                          int(self.settings.y()) - cy0)
                except Exception:
                    self._settings_rel = None
            self.settings.drag_moved.connect(_on_panel_dragged)
            # 设置项**即时生效**（不用点"应用"）—— 用户反馈"开关设置不能即时生效"
            self.settings.setting_changed.connect(self._on_setting_changed)
            # 「应用 / 确定」后的可见反馈（用户："设置应用按钮反馈不明显"）：
            # 面板自己会把按钮刷成强调色 + 改文案，这里再补一条 OSD。
            self.settings.applied.connect(lambda msg: self.show_osd(msg, "check", ms=1500))
            self.stage.settings_host.layout().addWidget(self.settings)
        return self.settings

    def toggle_settings(self, on: bool | None = None):
        show = (not self.stage.settings_host.isVisible()) if on is None else bool(on)
        if show:
            self._ensure_settings()
            # ⚠️ 落地页必须让位：它和设置面板都是"铺在画面上的一层"，
            #    同时显示就会互相压字（用户截图："这样信息完全看不了了"）。
            self.stage.set_landing_visible(False)
            # 打开设置时把控制条/顶栏淡出（面板要占满舞台，且二级模态不应再看见底层控件）。
            self._fader.hide_now()
            self._top_fader.hide_now()      # task-20 ③：顶栏同样让位
            # ★★ task-76：**不再抓图/不再模糊/不再加噪点** —— 只铺一层平滑暗化。
            #    （原来这里是 `processEvents()` + 抓静态画面套 Blur24：静态图盖住 GL 视频
            #     ⇒ 用户看到"底层定格"✗，噪点纹理 ⇒ "颗粒噪点"✗）
            self._prepare_settings_dim()
            self.stage.settings_host.setVisible(True)
            self.settings.setVisible(True)
            self._fit_settings()
            self.stage.settings_host.raise_()
            self.settings.raise_()
            self.stage.raise_settings()
            self.settings.setFocus()
            # ★ task-71：最后安装键盘闸门（先于 audio_view 等后装的 app eventFilter）
            try:
                if self._settings_key_guard is None:
                    self._settings_key_guard = _SettingsKeyGuard(self)
                self._settings_key_guard.install()
            except Exception:
                log.exception("安装设置键盘闸门失败")
        else:
            try:
                if self._settings_key_guard is not None:
                    self._settings_key_guard.uninstall()
            except Exception:
                log.debug("卸载设置键盘闸门失败", exc_info=True)
            self._hide_settings_dim()
            self.stage.settings_host.setVisible(False)
            if self.settings is not None:
                self.settings.setVisible(False)
            self.stage.set_landing_visible(True)
            # ★ task-71：关闭设置后把焦点还给主窗口 —— 否则焦点可能留在已隐藏的
            #    设置面板上，Space/方向键会"消失"（真机实测 Space 不再切换暂停）。
            try:
                self.setFocus()
            except Exception:
                pass
            self._fader.wake()
            self._top_fader.wake()          # task-20 ③：顶栏恢复

    # =====================================================================
    # ★★ task-76：设置面板的二级模态 = **纯平滑暗化层 + KeyGuard/MouseGuard + 外点回弹**
    #   （task-71 的抓图/离屏模糊/噪点背板已在 task-76 整段移除 ✗）
    # =====================================================================
    def _prepare_settings_dim(self):
        """★★ task-76：显示**纯平滑暗化柔光层**（rgba(10,14,20,0.35)，零抓图/零模糊/零噪点）。

        层级：暗化层在**舞台最底**（紧贴视频之上），设置面板随后 raise 到它上面 ⇒
          · 面板外的点击/滚轮被暗化层吃掉（MouseGuard ✓，不会暂停底层视频）；
          · 视频仍逐帧渲染（暗化层只是半透明填充，不遮挡 GL 刷新 ✓）。
        """
        try:
            if self._settings_dim is None:
                self._settings_dim = _SettingsDim(self.stage)
                self._settings_dim.clicked.connect(self._on_settings_outside_click)
            dim = self._settings_dim
            dim.setGeometry(0, 0, self.stage.width(), self.stage.height())
            dim.show()
            dim.raise_()          # 先抬到视频/落地页之上
            log.info("设置模态：纯平滑暗化层 %s（零抓图/零模糊/零噪点）",
                     [dim.width(), dim.height()])
        except Exception:
            log.debug("显示设置暗化层失败", exc_info=True)

    def _hide_settings_dim(self):
        """★★ task-76：隐藏暗化层（零抓图/零模糊，故只需 hide）。"""
        try:
            if self._settings_bounce_anim is not None:
                self._settings_bounce_anim.stop()
            if getattr(self, "_settings_dim", None) is not None:
                self._settings_dim.hide()
        except Exception:
            log.debug("隐藏设置暗化层失败", exc_info=True)

    def _settings_click_outside(self, obj, ev) -> bool:
        """★★★ P0 热修（v1.0.45 就地修）：判定「这次鼠标事件是不是【设置面板外部点击】」。

        三重安全防线（**任一命中 ⇒ 判为面板内部，放行正常交互**）：
          ① 事件接收者就是设置面板、或它是面板的**后代控件**（isAncestorOf）；
             （子控件里的事件根本不该被当成外部点击 —— 这是本次 Bug 的主因）
          ② 事件的**全局屏幕坐标**落在面板的全局矩形内（坐标系不混用：
             一律用 mapToGlobal + globalPosition，绝不用子控件 event.pos() 比 geometry）；
          ③ 面板不可见/未打开 ⇒ 不归本判定管，返回 False。
        只有三条全不满足，才算「真·外部点击」（即暗化遮罩区）。
        """
        try:
            pan = self.settings
            if pan is None or not pan.isVisible():
                return False
            # 防线 1：目标是面板自身或其任何子控件 ⇒ 内部
            try:
                if obj is not None and (obj is pan or pan.isAncestorOf(obj)):
                    return False
            except Exception:
                pass
            # 防线 2：全局屏幕坐标落在面板全局矩形内 ⇒ 内部
            gp = None
            try:
                gp = ev.globalPosition().toPoint()
            except Exception:
                try:
                    gp = ev.globalPos()
                except Exception:
                    gp = None
            if gp is not None:
                try:
                    tl = pan.mapToGlobal(QPoint(0, 0))
                    if QRect(tl, pan.size()).contains(gp):
                        return False
                except Exception:
                    pass
            return True
        except Exception:
            log.debug("设置外点判定失败", exc_info=True)
            return False

    def _on_settings_outside_click(self):
        """点击面板外遮罩：beep + 面板轻微回弹，**绝不穿透**到底层舞台。"""
        try:
            QApplication.beep()
        except Exception:
            log.debug("beep 失败", exc_info=True)
        self._bounce_settings_panel()

    def _bounce_settings_panel(self):
        """面板轻微下移再弹回（180ms）；不改变布局尺寸，不闪黑、不影响播放。"""
        try:
            if self.settings is None:
                return
            if self._settings_bounce_anim is not None:
                self._settings_bounce_anim.stop()
            start = self.settings.pos()
            anim = QPropertyAnimation(self.settings, b"pos", self)
            anim.setDuration(180)
            anim.setKeyValueAt(0.0, start)
            anim.setKeyValueAt(0.42, start + QPoint(0, 6))
            anim.setKeyValueAt(1.0, start)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start()
            self._settings_bounce_anim = anim
        except Exception:
            log.debug("设置面板回弹失败", exc_info=True)

    def _target_in_settings(self, obj) -> bool:
        """事件目标是否在设置面板内部（面板内键盘/滚轮应正常工作）。"""
        try:
            w = obj
            while w is not None:
                if w is self.settings or w is self.stage.settings_host:
                    return True
                w = w.parentWidget()
        except Exception:
            return False
        return False

    def _apply_acrylic_alpha(self):
        """亚克力生效时，把舞台底色加一点透明，让窗口后面的模糊透出来。

        ⚠️ 只改**浅色主题**：深色主题下半透明会把视频周围弄脏，
        而且用户对深色的诉求是"不要死黑"，不是"要透"。
        """
        from . import theme as _theme

        if not getattr(self, "_acrylic_on", False):
            return
        # ⚠⚠ 只把舞台底做透是**不够**的：窗口自己的底色（bg_base）
        #    和侧栏底（bg_deep）还是不透明的，亚克力被它们整个盖住 ——
        #    实测就是"标题栏和圆角处放在任何颜色上都没模糊"。
        #    三层底色要一起透。
        preset = _theme.THEMES.get(_theme.P.get("_theme"), {})
        keys = ("bg_base", "bg_deep", "bg_stage")
        for key in keys:
            base = preset.get(key) or _theme.P[key]
            if _theme.is_light() and len(base) == 7:
                # 分层透明度：窗口底最透（模糊最明显）、
                # 内容区次之、侧栏最实（保证文字可读）。
                alpha = {"bg_base": "B8", "bg_deep": "D0", "bg_stage": "C4"}[key]
                _theme.P[key] = base + alpha
            else:
                _theme.P[key] = base[:7]
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(_theme.build_stylesheet())
        self.update()

    def settings_modal_state(self) -> dict:
        """★★ task-76 锚点：模态状态（暗化层/无抓图/拖动/KeyGuard），QA 判据全可读。"""
        dim = getattr(self, "_settings_dim", None)
        st = {}
        try:
            if self.settings is not None:
                st = self.settings.drag_state()
        except Exception:
            st = {}
        return {"open": bool(self._settings_open()),
                "dim_exists": dim is not None,
                "dim_visible": bool(dim is not None and dim.isVisible()),
                "dim_alpha": 0.35,
                "dim_rgba": [10, 14, 20, int(round(0.35 * 255))],
                "dim_geometry": ([int(dim.x()), int(dim.y()), int(dim.width()), int(dim.height())]
                                 if dim is not None else None),
                "backdrop_none": getattr(self, "_settings_backdrop", None) is None,
                "key_guard": self._settings_key_guard is not None,
                "key_guard_installed": bool(getattr(self._settings_key_guard, "_installed", False)),
                "panel": st,
                "paused": bool(self.engine.get("pause", 0, False)),
                "time_pos": self.engine.get("time-pos")}

    def _settings_open(self) -> bool:
        return bool(self.settings is not None and self.settings.isVisible())

    def _place_settings_panel(self) -> None:
        """★ task-87：把设置面板摆到位（未拖拽⇒精确居中；拖拽过⇒相对中心偏移不变）。

        ⚠️ 必须在**布局生效之后**调用：挂载点 `settings_host` 带一个 QVBoxLayout，
        换肤/改尺寸会触发重排，把面板 move 回布局默认位置（实测换肤后面板"掉到左下角"
        正是布局在 move() 之后又跑了一遍 ✗）⇒ 这里除了立即摆位，再补一次 0ms 延迟摆位，
        确保**最后一句是我们在说话** ✓"""
        try:
            W, H = int(self.stage.width()), int(self.stage.height())
            pw, ph = int(self.settings.width()), int(self.settings.height())
            cx0, cy0 = max(0, (W - pw) // 2), max(0, (H - ph) // 2)
            if not getattr(self, "_settings_dragged", False):
                self._settings_rel = None
                self.settings.move(cx0, cy0)
            else:
                if getattr(self, "_settings_rel", None) is None:
                    self._settings_rel = (int(self.settings.x()) - cx0,
                                          int(self.settings.y()) - cy0)
                rx, ry = self._settings_rel
                x = min(max(0, cx0 + int(rx)), max(0, W - pw))
                y = min(max(0, cy0 + int(ry)), max(0, H - ph))
                self.settings.move(x, y)
        except Exception:
            log.debug("设置面板摆位失败", exc_info=True)
    def _fit_settings(self):
        """把设置面板收进舞台里（**并且不钻到悬浮胶囊底下**）。

        先按理论可用空间算一次, 再用**实测几何**迭代收敛 —— 布局器给的实际
        可用高度受各处 margin/间距影响, 纯算容易差几像素, 而差几像素就会
        压到控制条上（像素自审会立刻抓出来）。

        ⚠️ 视频铺到窗口底之后, 舞台高度包含了胶囊那一块 —— 可用高度必须**扣掉胶囊**,
        否则面板会伸到胶囊底下被盖住。
        ⚠️ 但**设置面板打开时胶囊已经淡出了**, 这时就该把整块高度都给面板 ——
        否则面板永远只有 570 高、内容滚动, 用户看到的就是"设置界面像是被截断了"。
        """
        if self.settings is None:
            return
        cap_h = 0 if self._settings_open() else (
            self.stage.capsule_host.height() if hasattr(self.stage, "capsule_host") else 0)
        # ⚠️ 再留 12px 余量：`setFixedSize` 之后几何要等布局跑完才更新，
        #    纯按理论值卡边界会正好差几像素（实测面板底 763 溢出窗口底 760）。
        avail_h = max(200, self.stage.height() - cap_h - 12)
        self.settings.fit_to(self.stage.width(), avail_h)
        for _ in range(6):
            # 让布局立刻生效，否则下一轮读到的还是旧几何，循环白跑
            self.stage.settings_host.layout().activate()
            QApplication.processEvents()
            tl = self.settings.mapTo(self.stage, QPoint(0, 0))
            over_h = (tl.y() + self.settings.height()) - self.stage.height()
            over_w = (tl.x() + self.settings.width()) - self.stage.width()
            if over_h <= 0 and over_w <= 0:
                break
            nh = self.settings.height() - max(0, over_h) - 10
            nw = self.settings.width() - max(0, over_w) - 10
            self.settings.setFixedSize(max(400, nw), max(320, nh))
        # ★★ task-76：挂载点铺满舞台（面板要能自由拖动，挂载点若只有面板大小，一拖就被裁）。
        #
        # ⚠️⚠️ P0 热修（v1.0.45 就地修）：这里**绝不能**给挂载点设
        #    `WA_TransparentForMouseEvents=True` —— 旧注释写的"只影响它自己、子控件照常可点"
        #    **是错的**！Qt 文档原文：该属性会 "disable the delivery of mouse events to the
        #    widget **and its children**" ⇒ 挂载点铺满舞台又整棵子树穿透 ⇒ **整个设置面板
        #    变成鼠标空洞**，所有点击穿透到下层暗化层（MouseGuard）⇒ 被当成"点了面板外面"
        #    ⇒ 每次都 beep + 回弹，选项卡/下拉/滑块/按钮/关闭叉全部点不动，只能 Esc 退出。
        #    正确做法：挂载点保持**可接收**鼠标（只有面板外的空白区域会落到它身上），
        #    再由 eventFilter 的**三重防线**（_settings_click_outside）判定外部点击。
        host = self.stage.settings_host
        try:
            host.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        except Exception:
            log.debug("设置挂载点穿透设置失败", exc_info=True)
        try:
            host.installEventFilter(self)        # 面板外空白区域的点击 ⇒ 交给三重防线
        except Exception:
            log.debug("设置挂载点事件过滤器安装失败", exc_info=True)
        host.setFixedSize(self.stage.width(), self.stage.height())
        # ★★ task-87（用户问题 1）：面板定位 —— **未拖拽：任何换肤/缩放后都精确居中**；
        #   **拖拽过：保持「相对中心的偏移」不变**（换肤改尺寸时也不会跳去左下角 ✗）。
        # 布局先落地，再摆位（否则 move 会被 QVBoxLayout 覆盖 ✗）；再补一帧防延迟重排
        try:
            lay = host.layout()
            if lay is not None:
                lay.activate()
        except Exception:
            log.debug("激活设置挂载点布局失败", exc_info=True)
        self._place_settings_panel()
        try:
            QTimer.singleShot(0, self._place_settings_panel)
        except Exception:
            pass

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.apply_window_mask()          # ★ task-87：尺寸变了要重算圆角遮罩
        if getattr(self, "_pip", False):
            self._pip_overlay_seat()          # ★ task-68：小窗尺寸变化时两按钮保持底部居中
        if self.stage.settings_host.isVisible():
            self._fit_settings()
            # ★★ task-76：暗化层始终铺满舞台（缩放时跟随）
            try:
                if getattr(self, "_settings_dim", None) is not None and self._settings_dim.isVisible():
                    self._settings_dim.setGeometry(0, 0, self.stage.width(), self.stage.height())
            except Exception:
                log.debug("缩放暗化层失败", exc_info=True)
        self._record_geometry()

    # 拉伸窗口时逐帧记录"贴合关系" —— 测试脚本靠它证明"0 脱节、0 露白",
    # 而不是只看拉伸之后的终态（终态对了中间过程照样可能露白）。
    GEOM_LOG_MAX = 4000

    def _record_geometry(self):
        if not hasattr(self, "_geom_log"):
            return
        try:
            w, h = self.width(), self.height()
            draw = self.drawer
            cap = self.capsule
            # ⚠️ 必须以**窗口坐标**记录：capsule 的父是 body、drawer 的父是 content,
            #    直接读 .x()/.y() 是父坐标系, 两者根本不在同一个空间里比较
            #    （本项目就这样误报过"抽屉比胶囊低 47px"的假缝隙）。
            from PySide6.QtCore import QPoint as _QPoint

            ctl = cap.mapTo(self, _QPoint(0, 0))
            rec = {
                "t": round(time.perf_counter(), 4),
                "win": [w, h],
                "capsule": [ctl.x(), ctl.y(), cap.width(), cap.height()],
                "drawer": None,
            }
            if draw.isVisible() and draw.width() > 2:
                dtl = draw.mapTo(self, _QPoint(0, 0))
                rec["drawer"] = [dtl.x(), dtl.y(), draw.width(), draw.height()]
            log_ = self._geom_log
            log_.append(rec)
            if len(log_) > self.GEOM_LOG_MAX:
                del log_[: len(log_) - self.GEOM_LOG_MAX]
        except Exception:
            pass

    def clear_geom_log(self):
        self._geom_log = []
        self._record_geometry()

    def dump_geom_log(self) -> list:
        return list(getattr(self, "_geom_log", []))

    # 键盘事件环形缓冲：断言失败时能一眼分清"键没收到"还是"收到了但语义错了"
    # （用户按住键时产生的自动重复也要看得见）。
    KEY_LOG_MAX = 400

    def _note_key(self, kind: str, ev):
        try:
            e = {
                "t": round(time.perf_counter(), 4),
                "kind": kind,
                "key": int(ev.key()),
                "text": ev.text(),
                "repeat": bool(ev.isAutoRepeat()),
                "mods": int(ev.modifiers().value),
                "boost": bool(self._boost),
                "right_down": bool(self._right_down),
                "timer_active": bool(self._long_timer.isActive()) if hasattr(self, "_long_timer") else None,
            }
            self._key_log.append(e)
            if len(self._key_log) > self.KEY_LOG_MAX:
                del self._key_log[: len(self._key_log) - self.KEY_LOG_MAX]
        except Exception:
            pass

    def dump_key_log(self) -> list:
        return list(getattr(self, "_key_log", []))

    def clear_key_log(self):
        self._key_log = []

    def _show_play_menu(self):
        menu = RoundedMenu(self)
        menu.add_row("播放速度…", on_click=lambda: self.capsule._show_speed_menu())
        menu.add_row("字幕设置…", on_click=lambda: self.toggle_subtitle_panel(True))
        menu.addSeparator()
        menu.add_row("画面：适应窗口",
                     on_click=lambda: self.engine.set("video-zoom", 0.0, MPV_FORMAT_DOUBLE))
        menu.add_row("画面：原始大小",
                     on_click=lambda: self.engine.set("video-zoom", self._zoom_for_one(),
                                                      MPV_FORMAT_DOUBLE))
        menu.addSeparator()
        menu.add_row("硬件解码信息…", on_click=self._show_hwdec_info)
        # 胶囊上的设置按钮已去重移除，这里锚到标题栏那个
        menu.exec(self.titlebar.btn_settings.mapToGlobal(self.titlebar.btn_settings.rect().bottomLeft()))

    def _show_hwdec_info(self):
        cur = self.engine.cached("hwdec-current", None)
        fmt = self.engine.cached("video-format", "")
        codec = self.engine.cached("video-codec", "")
        drops = self.engine.cached("frame-drop-count", 0)
        self.show_osd(f"解码 {cur or '软件'} · {fmt} · 丢帧 {drops}", "info", ms=2600)


    def _show_main_menu(self, anchor=None):
        menu = RoundedMenu(self)
        menu.add_row("打开文件…", "Ctrl + O", on_click=self.open_files_dialog)
        menu.add_row("打开文件夹…", "Ctrl + Shift + O", on_click=self.open_folder_dialog)
        menu.addSeparator()
        rec = menu.addMenu("最近打开")
        files = self.cfg.get("recent.files", []) or []
        if not files:
            rec.add_row("（还没有最近打开的文件）", enabled=False)
        for p in files[:12]:
            row = rec.add_row(os.path.basename(p), on_click=lambda pp=p: self.open_path(pp))
            row.setToolTip(p)
        menu.addSeparator()
        menu.add_row("设置…", "Ctrl + ,", on_click=lambda: self.toggle_settings(True))
        row_top = menu.add_row("窗口置顶", "✓" if self._always_on_top else "",
                               on_click=self.toggle_always_on_top)
        row_top.setToolTip("勾选后窗口始终在最前")
        menu.addSeparator()
        menu.add_row("全屏", "F", on_click=self.toggle_fullscreen)
        menu.add_row("退出", on_click=self.close)
        # ★ task-70：主菜单展开期间左上角入口保持 Checked（强调色），关闭即恢复常态。
        try:
            menu.aboutToShow.connect(lambda: self.titlebar.set_menu_checked(True))
            menu.aboutToHide.connect(lambda: self.titlebar.set_menu_checked(False))
        except Exception:
            pass
        # ⚠️ 菜单要**贴着触发它的那个按钮**弹出；task-70 删掉右上角「…」后，
        #    默认锚点就是左上角三圆点主菜单入口（TitleBar 仍会把实际按钮传进来）。
        btn = anchor if anchor is not None else self.titlebar.logo
        pos = btn.mapToGlobal(btn.rect().bottomLeft())
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        size = menu.sizeHint()
        x = min(pos.x(), scr.right() - size.width() - 8)
        y = pos.y() + 4
        if y + size.height() > scr.bottom() - 8:
            y = btn.mapToGlobal(btn.rect().topLeft()).y() - size.height() - 4
        menu.exec(QPoint(max(scr.left() + 8, x), max(scr.top() + 8, y)))
        # ★★ task-82（dev-agent-5 报的跨链遗留）：**玻璃皮肤下菜单关闭后左上角残留蓝圆**。
        #   根因：顶层 `WA_TranslucentBackground` ⇒ Qt 自动置 `WA_NoSystemBackground`
        #   ⇒ **子控件局部重绘时 Qt 不擦上一帧**，菜单弹出期间盖住的按钮按下态像素被
        #   "永久化"（控件级 grab 看不到，因为 grab 会重绘 ⇒ 这也是为什么只有窗口级抓能看到）。
        #   修法：菜单收起后**主动作废整窗 + 擦净**（见 `_clear_stale_after_menu()`）。
        self._clear_stale_after_menu()

    # ★★ task-82：清除「半透明顶层 + 菜单弹出」留下的陈旧像素
    def _clear_stale_after_menu(self, _again: bool = False) -> None:
        """菜单收起后强制**整窗重绘并擦净**上一帧（玻璃皮肤专用问题）。

        为什么需要：顶层 `WA_TranslucentBackground` ⇒ Qt 置 `WA_NoSystemBackground`
        ⇒ 不擦上一帧；菜单弹出时盖住的标题栏按钮处于"悬停/按下"蓝色态，其像素被
        留在窗口缓冲里，菜单一收就变成"左上角一个蓝圆"✗（控件自身早已重绘正确）。

        为什么 `w.repaint()` 不够：它只**重画**不**擦除**，半透明像素是叠在旧内容上的
        ⇒ 残留仍在。这里先 `update()` 作废整窗，再由 `paintEvent` 用
        `CompositionMode_Source` **覆盖写入**（Source 模式 = 替换而非叠加）来真正清掉 ✓。
        """
        try:
            self._flush_src = True
            for w in (self, getattr(self, "body", None), getattr(self, "stage", None),
                      getattr(self, "titlebar", None)):
                if w is not None:
                    w.update()
            self.repaint()                              # 同步强制一帧（批处理会拖延）
            # ⚠️ 菜单关闭是**异步**的：收起动画/延迟重绘可能在这一帧之后又把旧像素刷回来
            #   （实测 5 次里有 1 次抓到 3471 个蓝像素 ✗）⇒ 再补两次延迟擦净（合计 ~100ms，
            #   只在菜单收起时发生，零每帧开销）。
            #   ⚠️⚠️ 必须用 `_again` 守卫：否则每次擦净都再排 2 次 ⇒ **无限递归把事件循环打死** ✗
            #   （我第一版就踩了：单测直接 Timeout「无响应」）
            if not _again:
                # Lead 定向建议：残留出现在关闭后 ~450ms ⇒ 补 500ms；再留 900ms 兜底
                for _ms in (0, 60, 500, 900):
                    QTimer.singleShot(_ms, lambda: self._clear_stale_after_menu(True))
            log.debug("task-82：菜单收起后已整窗擦净（含 2 次延迟补擦）")
        except Exception:
            log.debug("清除菜单残留失败", exc_info=True)

    # ★★ task-87（用户问题 4）：**窗口四角「灰黑方角」溢出**的根治
    def _corner_radius_px(self) -> float:
        """窗口圆角半径（逻辑 px）——与 `#WindowBody` 的 QSS border-radius 同源。"""
        try:
            return float(M["radius_lg"])
        except Exception:
            return 12.0

    def _rounded_region(self) -> "QRegion":
        """窗口的圆角区域（用于求「矩形减去圆角」= 四角待擦净区）。"""
        r = self._corner_radius_px()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, float(self.width()), float(self.height())), r, r)
        return QRegion(path.toFillPolygon().toPolygon())

    def _corner_clear_painter(self, region) -> None:
        """★ task-93：回退路径 —— 用 `CompositionMode_Source` 擦圆角之外的四个角。"""
        try:
            p = QPainter(self)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.setClipRegion(region)
            p.fillRect(self.rect(), Qt.transparent)
            p.end()
        except Exception:
            log.debug("擦净四角失败", exc_info=True)

    def dwm_state(self) -> dict:
        """★ task-93/QA：窗口圆角的**唯一权威锚点**（QA 取样请用这个）。

        返回：
          · `mask`     —— 是否还挂着 1-bit `setMask`（应为 False）；
          · `fallback` —— 非 None 表示走了"抗锯齿路径擦角"回退（DWM 不可用时才有）；
          · `dwm_ok`   —— DWM 圆角调用是否成功（None = 尚未调用）。"""
        return {
            "mask": bool(self.mask()),
            "fallback": getattr(self, "_corner_fallback", None),
            "dwm_ok": getattr(self, "_dwm_ok", None),
            "fullscreen": bool(getattr(self, "_fullscreen", False)),
            "maximized": bool(self.isMaximized()),
        }

    def apply_window_mask(self) -> None:
        """★★ task-87：**把整窗裁进圆角矩形**（根源修复「四角灰黑方角溢出」）。

        ⚠️ 为什么不是"擦角"：试过在 `paintEvent` 里用 `Source` 擦四角 —— **无效** ✗。
        因为 `#WindowBody` 的阴影是在父级 paintEvent **之后**由子控件画的，
        父级擦完它又画回来（实测擦后四角反而 161/161 全是灰 ✗）。
        唯一可靠的做法是给**顶层窗口**设 `setMask(圆角区域)`：
          · 子控件一切绘制都被窗口遮罩裁掉 ⇒ 四角根本不存在像素 ✓；
          · 全屏/最大化时必须 `clearMask()`（要铺满到边、且圆角已关掉）。"""
        try:
            # ★★ task-93（用户 92b）：**废除 1-bit setMask** —— 它把窗口形状硬裁成楼梯，
            #   四角出现**锯齿 + 暗影**（软件遮罩没有抗锯齿）。改走 Win11 **DWM 原生圆角**：
            #   由合成器按系统半径裁切，边缘平滑、且不产生任何"窗口外像素" ✓
            try:
                self.clearMask()                      # 先清掉旧版残留遮罩（必须，否则仍锯 齿）
            except Exception:
                pass
            from ..platform import win as _win
            if getattr(self, "_fullscreen", False) or self.isMaximized() or self.isFullScreen():
                _win.enable_rounded_corners(self, False)   # 全屏/最大化：要铺满到边
                self._corner_fallback = None
                return
            _ok = bool(_win.enable_rounded_corners(self, True))
            self._dwm_ok = _ok
            if _ok:
                self._corner_fallback = None               # DWM 生效 ⇒ 不需要回退
            else:
                # 回退（Win10 等无 DWM 圆角）：保留 QSS 圆角 + 在 paintEvent 里做**抗锯齿**圆角
                # 路径裁剪（不设 mask、不产生锯齿）
                self._corner_fallback = self._corner_radius_px()
                self.update()
        except Exception:
            log.debug("应用窗口圆角失败", exc_info=True)

    def clear_window_corners(self) -> None:
        """★★ task-87：把**圆角之外的四角**用 `Source` 模式擦成透明（防灰黑方角溢出）。

        原理：顶层是**无边框矩形**窗口；`#WindowBody` 用 QSS 画圆角，但窗口本身的矩形
        四角仍会留下**未绘制/半透明**的旧像素 —— 用户看到的就是「左上/右上角灰黑方角」。
        用 `CompositionMode_Source` + 圆角路径反选（矩形 − 圆角）填充透明 = 真正擦除 ✓
        （普通 fillRect 是 SourceOver 叠加，擦不掉，这一点与 task-82 同因）。

        只在**非全屏/非最大化**时执行（全屏要铺满到边、圆角也关掉了）。"""
        try:
            if getattr(self, "_fullscreen", False) or self.isMaximized() or self.isFullScreen():
                return
            region = QRegion(self.rect()) - self._rounded_region()
            if region.isEmpty():
                return
            p = QPainter(self)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.setClipRegion(region)
            p.fillRect(self.rect(), Qt.transparent)
            p.end()
        except Exception:
            log.debug("擦净窗口四角失败", exc_info=True)
    def paintEvent(self, ev):        # noqa: N802
        """★ task-82：半透明顶层**自己负责擦干净**上一帧。

        只在本窗设置了 `WA_TranslucentBackground`（玻璃皮肤）时擦；其它皮肤走默认。
        `CompositionMode_Source` = **替换**目标像素（含 alpha），这才是真"擦除"；
        普通 fillRect 默认是 SourceOver（叠加）⇒ 擦不掉 ✗（这就是 `repaint()` 无效的原因）。
        """
        try:
            # ⚠️ 只在**菜单收起那一次**擦净（`_flush_src` 由 `_clear_stale_after_menu()` 置位）
            #    ⇒ 平时每帧零额外开销 ✓（否则等于每帧多一次全窗填充，白烧 CPU）
            # ★ task-93：DWM 不可用时**回退**——按抗锯齿圆角路径把四角擦净
            _fb = getattr(self, "_corner_fallback", None)
            if _fb:
                try:
                    _r = float(_fb)
                    _path = QPainterPath()
                    _path.addRoundedRect(QRectF(0, 0, float(self.width()), float(self.height())), _r, _r)
                    _reg = QRegion(self.rect()) - QRegion(_path.toFillPolygon().toPolygon())
                    if not _reg.isEmpty():
                        self._corner_clear_painter(_reg)
                except Exception:
                    log.debug("回退圆角擦净失败", exc_info=True)
            if bool(getattr(self, "_flush_src", False)) and bool(self.testAttribute(Qt.WA_TranslucentBackground)):
                self._flush_src = False
                p = QPainter(self)
                p.setCompositionMode(QPainter.CompositionMode_Source)
                p.fillRect(self.rect(), Qt.transparent)
                p.end()
        except Exception:
            log.debug("擦净窗口失败", exc_info=True)
        super().paintEvent(ev)

    # =====================================================================
    # ★★ task-68（用户 5）：**画中画（PiP）迷你悬浮窗**
    # ---------------------------------------------------------------------
    # 用户原话："视频左下角那个画中画按钮点了毫无反应，纯摆设"。
    # 侦察结论：按钮/信号早就在（`control_bar.py` 的 `btn_pip` → `pip_clicked`），
    #           **只是 MainWindow 从来没连过它** ⇒ 本卡把它实装。
    # 口径（卡片）：480×270 无边框置顶小窗、屏幕右下角 32px、鼠标移入只出现
    #           暂停/退出两个按钮、退出后**几何逐位复原**、播放不中断。
    PIP_W, PIP_H, PIP_MARGIN = 480, 270, 32

    def toggle_pip(self):
        """进出画中画（PiP）。按钮/双击小窗都走这里 ⇒ 单一真源。"""
        self._pip_enter() if not self._pip else self._pip_exit()

    def _pip_enter(self) -> bool:
        """进入 PiP：记几何/最大化态 → 藏顶栏收抽屉 → 480×270 无边框 → 右下 32px → 置顶。"""
        if self._kind != router.KIND_VIDEO:
            self.show_osd("画中画仅视频可用", "info")
            return False
        try:
            self._pip_saved_geo = self.geometry()
            self._pip_saved_max = bool(self.isMaximized())
            self._pip_saved_topmost = bool(self._always_on_top)
            # ⚠️⚠️ **必须临时放宽最小尺寸**：本窗口 `setMinimumSize(M["win_min_w"], M["win_min_h"])`
            #   （800×560）⇒ 直接 setGeometry(480×270) 会被 Qt **夹到 800×560** ✗
            #   （我第一版实测：设 (100,100,480,270) 实际得到 800×560）。
            self._pip_saved_min = self.minimumSize()
            self._pip_saved_max_size = self.maximumSize()
            self.setMinimumSize(160, 90)
            self._pip = True
            # 最大化态必须先退出 —— 否则几何会被系统改写（与全屏同一处教训）
            if self._pip_saved_max:
                self.showNormal()
            self._fullscreen = False
            self._ensure_titlebar_visible("pip:enter")
            self.titlebar.setVisible(False)          # 无边框小窗：藏顶栏
            try:
                self.drawer.set_open(False, animate=False)
            except Exception:
                log.debug("PiP 收抽屉失败", exc_info=True)
            # 停靠屏幕右下角（**可用区**，避开任务栏），边距 32px
            scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
            x = int(scr.right() + 1 - self.PIP_W - self.PIP_MARGIN)
            y = int(scr.bottom() + 1 - self.PIP_H - self.PIP_MARGIN)
            # ⚠️ 本工程窗口是"无边框 + WM_NCCALCSIZE 返回 0"：只调 setGeometry 即可，
            #    不要再用 win.move_window 二次挪动（task-59 实测双调用会多 ~4ms 且易失步）。
            self.setGeometry(QRect(x, y, self.PIP_W, self.PIP_H))
            win.set_topmost(self, True)              # 只改 Z 序、不重建窗口（不黑屏）
            win.enable_rounded_corners(self, True)
            win.set_window_border(self, True)
            # ★★ task-89：胶囊进入 PiP 时**快照可见性并整体隐藏**（退出时逐位恢复）
            try:
                self._pip_capsule_was = bool(self.capsule.isVisible())
                self.capsule.set_pip_mode(True)
                self.stage.drawer_handle.setVisible(False)
            except Exception:
                log.debug("PiP 收起胶囊失败", exc_info=True)
            self._pip_overlay_show()
            self._pip_overlay_seat()
            log.info("PiP 进入: geo=%s screen=%s", list(self.geometry().getRect()), list(scr.getRect()))
            return True
        except Exception:
            # ★ 把真实原因**留在锚点里**（这类"按钮点了没反应"最怕静默失败）
            import traceback as _tb
            self._pip_error = _tb.format_exc()[-500:]
            log.warning("进入 PiP 失败: %s", self._pip_error)
            self._pip = False
            return False

    def _pip_exit(self) -> bool:
        """退出 PiP：取消置顶 + **几何逐位复原** + 恢复顶栏与完整 UI。"""
        try:
            self._pip = False
            self._pip_overlay_hide()
            win.set_topmost(self, bool(self._pip_saved_topmost))
            geo = getattr(self, "_pip_saved_geo", None)
            if geo is not None and geo.isValid():
                self.setGeometry(geo)                # 精确复原（含位置与尺寸）
            _mn = getattr(self, "_pip_saved_min", None)
            if _mn is not None:
                self.setMinimumSize(_mn)
            _mx = getattr(self, "_pip_saved_max_size", None)
            if _mx is not None:
                self.setMaximumSize(_mx)
            if bool(getattr(self, "_pip_saved_max", False)):
                self.showMaximized()
            self._ensure_titlebar_visible("pip:exit")
            self.titlebar.setVisible(True)
            # ★★ task-89（用户 5）：退出 PiP 后**逐位恢复**控制栏控件（否则音量/倍速等永久消失 ✗）
            try:
                self.capsule.set_pip_mode(False)          # 内部：逐位恢复快照 + 按当前宽度对账
                self.capsule.restore_from_pip()
                self.stage.drawer_handle.setVisible(True)
            except Exception:
                log.debug("PiP 恢复胶囊失败", exc_info=True)
            # 再按当前媒体类型完整对账一次（视频/音频两模式都验 ✓）
            try:
                self._apply_kind_ui(self._kind)          # 立即一次
                QTimer.singleShot(90, lambda: self._apply_kind_ui(self._kind))   # 布局落定后再一次
            except Exception:
                log.debug("PiP 退出后重应用媒体类型失败", exc_info=True)
            log.info("PiP 退出: geo=%s", list(self.geometry().getRect()))
            return True
        except Exception:
            log.debug("退出 PiP 失败", exc_info=True)
            return False

    # ---- 鼠标移入时浮现的**极简两按钮**（暂停/播放 + 退出 PiP）------------------------
    def _pip_overlay_show(self) -> None:
        ov = getattr(self, "_pip_overlay", None)
        if ov is None:
            ov = self._pip_build_overlay()
        if ov is not None:
            ov.setVisible(True)
            ov.raise_()

    def _pip_overlay_hide(self) -> None:
        ov = getattr(self, "_pip_overlay", None)
        if ov is not None:
            ov.setVisible(False)

    def _pip_build_overlay(self):
        """★ task-89：构建 PiP 底部 **40px 磨砂控制条**（5 类纯白控件）。"""
        try:
            bar = _PipBar(self)
            bar.prev_clicked.connect(lambda: self._step_file(-1))
            bar.play_clicked.connect(self._pip_toggle_pause)
            bar.next_clicked.connect(lambda: self._step_file(1))
            bar.seeked.connect(self._pip_seek_fraction)
            bar.exit_clicked.connect(self._pip_exit)
            bar.close_clicked.connect(self.close)
            self._pip_overlay = bar
            return bar
        except Exception:
            log.debug("构建 PiP 控制条失败", exc_info=True)
            return None

    def _pip_toggle_pause(self) -> None:
        try:
            self.toggle_pause()
        except Exception:
            log.debug("PiP 播放/暂停失败", exc_info=True)
        self._pip_sync_play_icon()

    def _pip_sync_play_icon(self) -> None:
        bar = getattr(self, "_pip_overlay", None)
        if bar is not None and hasattr(bar, "btn_play"):
            try:
                bar.btn_play.set_icon("play" if self._paused else "pause")
            except Exception:
                log.debug("PiP 播放图标同步失败", exc_info=True)

    def _pip_seek_fraction(self, frac: float) -> None:
        """PiP 迷你进度条 drag ⇒ 按比例 seek（沿用主窗口既有 seek 口径）。"""
        try:
            dur = float(self._duration or 0.0)
            if dur <= 0.0:
                return
            pos = max(0.0, min(1.0, float(frac))) * dur
            self.engine.set("time-pos", pos)
            self._position = pos
            self._pip_sync_progress()
        except Exception:
            log.debug("PiP seek 失败", exc_info=True)

    def _pip_sync_progress(self) -> None:
        bar = getattr(self, "_pip_overlay", None)
        prog = getattr(bar, "progress", None) if bar is not None else None
        if prog is None:
            return
        try:
            dur = float(self._duration or 0.0)
            prog.set_value((float(self._position) / dur) if dur > 0 else 0.0)
        except Exception:
            pass

    def _pip_build_overlay_legacy(self):
        """构建 PiP 悬浮按钮条 —— **只有两个按钮**（卡片判据 c）。"""
        try:
            from .widgets import IconButton
            ov = QWidget(self)
            lay = QHBoxLayout(ov)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(10)
            self._pip_btn_play = IconButton("pause", ov, tooltip="播放/暂停 (Space)", size=34, icon_size=18)
            self._pip_btn_play.clicked.connect(self.toggle_pause)
            self._pip_btn_exit = IconButton("close", ov, tooltip="退出画中画", size=34, icon_size=18)
            self._pip_btn_exit.clicked.connect(self._pip_exit)
            lay.addWidget(self._pip_btn_play)
            lay.addWidget(self._pip_btn_exit)
            ov.adjustSize()
            self._pip_overlay = ov
            return ov
        except Exception:
            log.debug("构建 PiP 悬浮按钮失败", exc_info=True)
            return None

    def _pip_overlay_seat(self) -> None:
        """★ task-89：控制条贴小窗**底部**、左右各 10px、高 40px（尺寸变化/进入时调用）。"""
        ov = getattr(self, "_pip_overlay", None)
        if ov is None:
            return
        try:
            w = max(160, int(self.width()) - 20)
            ov.setGeometry(10, max(0, int(self.height()) - int(_PipBar.BAR_H) - 8), w, int(_PipBar.BAR_H))
            self._pip_sync_progress()
            self._pip_sync_play_icon()
        except Exception:
            log.debug("PiP 控制条摆位失败", exc_info=True)

    def _pip_hover_recheck(self) -> None:
        """Leave 之后复核光标是否真的离开小窗（子控件切换也会发 Leave ⇒ 不能直接收）。"""
        if not self._pip:
            return
        try:
            inside = self.frameGeometry().contains(QCursor.pos())
        except Exception:
            inside = False
        if inside:
            self._pip_overlay_show()
            self._pip_overlay_seat()
        else:
            self._pip_overlay_hide()

    def pip_state(self) -> dict:
        """★ task-68 锚点：QA 真机判据全可读（几何/置顶/按钮/原几何）。"""
        ov = getattr(self, "_pip_overlay", None)
        btns = []
        if ov is not None:
            btns = [b for b in (getattr(self, "_pip_btn_play", None),
                                getattr(self, "_pip_btn_exit", None)) if b is not None]
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        g = self.geometry()
        # ★ 判据 b：**真实 Z 序**用 Win32 直读（Qt 的 windowFlags 只是"请求值"，
        #   不代表系统真的把它放到了 topmost 段；实测 SetWindowPos 后才置位 ✓）
        _topmost_real = None
        _has_caption = None
        try:
            import ctypes as _ct
            _u = _ct.windll.user32
            _u.GetWindowLongW.restype = _ct.c_long
            _hwnd = int(self.winId())
            _ex = _u.GetWindowLongW(_hwnd, -20)      # GWL_EXSTYLE
            _st = _u.GetWindowLongW(_hwnd, -16)      # GWL_STYLE
            _topmost_real = bool(_ex & 0x00000008)   # WS_EX_TOPMOST
            _has_caption = bool(_st & 0x00C00000)    # WS_CAPTION（无边框应为 False）
        except Exception:
            log.debug("读取窗口样式失败", exc_info=True)
        return {"pip": bool(self._pip),
                "topmost_real": _topmost_real,
                "has_caption": _has_caption,
                "frameless_flag": bool(self.windowFlags() & Qt.FramelessWindowHint),
                "geometry": list(g.getRect()),
                "size": [int(g.width()), int(g.height())],
                "screen_available": list(scr.getRect()),
                "margin_right": int(scr.right() + 1 - (g.x() + g.width())),
                "margin_bottom": int(scr.bottom() + 1 - (g.y() + g.height())),
                "saved_geometry": (list(self._pip_saved_geo.getRect())
                                   if getattr(self, "_pip_saved_geo", None) is not None else None),
                "saved_maximized": bool(getattr(self, "_pip_saved_max", False)),
                "titlebar_visible": bool(self.titlebar.isVisible()),
                "overlay_visible": bool(ov is not None and ov.isVisible()),
                "overlay_buttons": len(btns),
                "overlay_button_visible": [bool(b.isVisible()) for b in btns],
                "play_icon": (str(getattr(getattr(self, "_pip_btn_play", None), "_name", ""))),
                "always_on_top": bool(self._always_on_top),
                "kind": str(self._kind),
                "pip_button_visible": bool(self.capsule.btn_pip.isVisible()),
                "winflags": int(self.windowFlags().value),
                "min_size": [int(self.minimumWidth()), int(self.minimumHeight())],
                "pip_error": str(getattr(self, "_pip_error", ""))[-300:],
                "saved_min_size": ([int(getattr(self, "_pip_saved_min").width()),
                                    int(getattr(self, "_pip_saved_min").height())]
                                   if getattr(self, "_pip_saved_min", None) is not None else None)}

    def toggle_always_on_top(self):
        self._always_on_top = not self._always_on_top
        win.set_always_on_top(self, self._always_on_top)
        self.show_osd("窗口置顶 开" if self._always_on_top else "窗口置顶 关", "info")

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self.titlebar.set_maximized(self.isMaximized())

    # =====================================================================
    # Tab 媒体信息浮层（PotPlayer 风格，左上角）
    # =====================================================================
    def _toggle_media_info(self):
        ov = self.stage.media_info
        if ov.isVisible():
            ov.setVisible(False)
            return
        if not self._path:
            self.show_osd("还没有打开文件", "info", ms=900)
            return
        title, rows = self._collect_media_info()
        ov.set_info(title, rows)
        ov.setVisible(True)
        ov.raise_()

    def _collect_media_info(self):
        """Tab 信息：**图片与音视频是两套模板**（字段本就不同）。

        通用行（两边都有）：文件大小 / 路径。
        图片专属：尺寸+百万像素、格式、当前纹理、缩放、旋转/镜像、EXIF。
        """
        if self._kind == router.KIND_IMAGE:
            return self._collect_image_info()
        return self._collect_av_info()

    def _collect_image_info(self):
        """图片信息模板（不读 mpv —— 图片模式下它已经停了）。"""
        import datetime
        import time as _time

        from PySide6.QtGui import QImageReader

        path = self._path or ""
        v = self._iv()
        rows = []
        w, h = v.natural_size() if v is not None else (0, 0)
        if w and h:
            rows.append(("尺寸", "%d × %d    %.1f MP" % (w, h, w * h / 1e6)))
        # #13 去重：原来还有一行「当前纹理」，与「尺寸」在多数情况下完全相同（
        #     底图已是原尺寸时尤其如此）⇒ 只在**两者不同**时才显示，并说明差异原因。
        # 格式（QImageReader 能给出真实解码器名，比扩展名可信）
        fmt = os.path.splitext(path)[1].lstrip(".").upper()
        try:
            rd = QImageReader(path)
            real = bytes(rd.format()).decode("ascii", "replace")
            if real:
                fmt = "%s（%s）" % (real.upper(), fmt) if real.upper() != fmt else fmt
            sup = rd.supportsAnimation()
            if sup:
                fmt += "    动画"
        except Exception:
            pass
        if fmt:
            rows.append(("格式", fmt))
        if v is not None and v.has_image():
            sw, sh = v.size_px()
            if (sw, sh) != (w, h):
                # 只有确实用了降采样底图时才提示（否则和「尺寸」重复 —— #13）
                rows.append(("当前纹理", "%d × %d    底图（放大后自动换原图）" % (sw, sh)))
            # #11 用户明确要求：不要「缩放」这一行（既不实时、也没必要）
            state = []
            if v.rotation():
                state.append("旋转 %d°" % v.rotation())
            if v.flip():
                state.append("水平镜像")
            rows.append(("变换", "    ".join(state) if state else "无"))
        # ---- 通用行 ----
        try:
            size = os.path.getsize(path) if path and os.path.isfile(path) else 0
        except OSError:
            size = 0
        if size:
            # #13 去重：原来同时给「x.xx MB」和「human_size」两种表述，纯冗余
            rows.append(("文件大小", scanner.human_size(size)))
        try:
            mt = os.path.getmtime(path)
            rows.append(("修改时间", datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")))
        except OSError:
            pass
        # ---- EXIF（JPEG 才有；读不到就整段省略）----
        try:
            from ..media import exif as exif_mod

            ex = exif_mod.read_exif(path)
        except Exception:
            ex = {}
        if ex:
            cam = " ".join(x for x in (str(ex.get("make", "")).strip(),
                                       str(ex.get("model", "")).strip()) if x)
            if cam:
                rows.append(("相机", cam))
            shot = []
            if ex.get("fnumber"):
                shot.append(exif_mod.fmt_fnumber(ex["fnumber"]))
            if ex.get("exposure"):
                shot.append(exif_mod.fmt_exposure(ex["exposure"]))
            if ex.get("iso"):
                shot.append("ISO %s" % ex["iso"])
            if shot:
                rows.append(("曝光", "    ".join(shot)))
            if ex.get("focal"):
                rows.append(("焦距", exif_mod.fmt_focal(ex["focal"], ex.get("focal35"))))
            tstr = str(ex.get("datetime_orig") or ex.get("datetime") or "").strip()
            if tstr:
                rows.append(("拍摄时间", tstr.replace(":", "-", 2)))
        if path:
            # #12 用户要求：地址要**具体到文件名**
            rows.append(("位置", path))
        return os.path.basename(path or ""), rows

    def _collect_av_info(self):
        """音视频信息模板（现读一遍 mpv 属性 —— 按 Tab 就是想看此刻的真实值）。"""
        e = self.engine

        def g(name: str, default: str = "") -> str:
            try:
                v = e.get(name)
            except Exception:
                log.debug("读属性 %s 失败", name, exc_info=True)
                return default
            if v is None:
                return default
            s = str(v).strip()
            return default if s in ("", "no") else s

        def num(name: str, default=None):
            try:
                return float(g(name))
            except (TypeError, ValueError):
                return default

        rows = []
        w, h = g("width"), g("height")
        fps = num("container-fps") or num("estimated-vf-fps")
        if w and h:
            line = f"{w}×{h}"
            if fps:
                line += f"    {fps:.4g} fps"
            rows.append(("画面", line))
        vfmt = g("video-format") or g("video-codec-name") or g("video-codec")
        if vfmt:
            rows.append(("视频编码", vfmt))
        pix = g("video-params/pixelformat")
        if pix:
            rows.append(("像素格式", pix))
        hw = g("hwdec-current")
        rows.append(("硬件解码", hw if hw else "关（软解）"))
        acodec = g("audio-codec-name") or g("audio-codec")
        if acodec:
            ch = g("audio-params/channel-count")
            sr = num("audio-params/samplerate")
            extra = "    ".join(x for x in (
                f"{ch} 声道" if ch else "",
                f"{sr / 1000:.1f} kHz" if sr else "") if x)
            rows.append(("音频编码", acodec + (("    " + extra) if extra else "")))
        dur = num("duration")
        if dur:
            rows.append(("时长", fmt_time(dur)))
        try:
            size = os.path.getsize(self._path) if self._path and os.path.isfile(self._path) else 0
        except OSError:
            size = 0
        br = num("video-bitrate") or num("audio-bitrate")
        if not br and size and dur:
            br = size * 8.0 / dur
        if br:
            rows.append(("平均码率", f"{br / 1e6:.2f} Mb/s"))
        if size:
            rows.append(("文件大小", f"{size / 1048576:.1f} MB"))
        fmt = g("file-format")
        if fmt:
            rows.append(("容器", fmt))
        spd = num("speed")
        if spd and abs(spd - 1.0) > 1e-6:
            rows.append(("播放速度", f"{spd:g}x"))
        return os.path.basename(self._path or ""), rows

    def _set_body_fullscreen(self, on: bool):
        """切换 `#WindowBody` 的全屏态：去掉 1px 描边、圆角**以及那 1px 布局内边距**。

        ⚠️ 三样缺一不可：
        * QSS 里的 `border` / `border-radius`（浅色主题下描边就是屏幕边缘的白线）；
        * **布局内边距** —— 它给描边留位，全屏时不归零的话画面比窗口小 2px，
          屏幕上就是一整圈背景色（实测 `video_geo` 1705×1065 vs `body` 1707×1067，
          浅色主题下采样到的就是 #f7f8fa 的白边）。这一条是第二轮"白边依旧"的真凶。
        * 改完属性必须 unpolish/polish，否则样式表里那条 `[fullscreen="true"]` 形同虚设。
        """
        try:
            self.body.setProperty("fullscreen", "true" if on else "false")
            self.body.style().unpolish(self.body)
            self.body.style().polish(self.body)
            lay = self.body.layout()
            if lay is not None:
                m = 0 if on else 1
                lay.setContentsMargins(m, m, m, m)
            self.body.update()
        except Exception:
            log.debug("切换全屏态窗口样式失败", exc_info=True)

    def toggle_fullscreen(self):
        """全屏 —— **无边框几何全屏（Borderless Geometry Fullscreen）**，零黑屏。

        ★★ task-59（用户 ⑤）：task-38 改用 setWindowState(Qt.WindowFullScreen) 后，
        挂 QOpenGLWidget 的无边框窗口一旦尺寸恰等于屏幕分辨率，驱动/DWM 会切到
        **硬件独立翻转（Exclusive Fullscreen）** ⇒ GL 上下文重置，进出全屏黑屏闪半秒。

        本卡改回「只把窗口几何放大到整个显示器」：**严禁** setWindowState(WindowFullScreen)
        与 showFullScreen()；保留普通无边框窗口 + 普通 Z 序（Alt+Tab 可切走）。
        进入：保存当前几何/最大化态 → setGeometry(screen.x, screen.y, w, h + 1)；
              **高度 +1px 故意比屏幕高 1px**，绕过 Windows 的独占翻转判定，
              仍 100% 盖住任务栏（底部超出屏幕 1px，用户不可见）。
        退出：setGeometry(saved) 瞬间恢复（同 HWND/同 GL 上下文，0 黑屏 0 闪烁）。
        """
        self._fullscreen = not self._fullscreen
        # v1.0.20：直接同步一次图标（不依赖 WindowStateChange 的到达时机）
        try:
            self.stage.image_bar.set_fullscreen(bool(self._fullscreen))
            self.capsule.set_fullscreen(bool(self._fullscreen))
        except Exception:
            log.debug("同步全屏图标失败", exc_info=True)
        self.titlebar.setVisible(not self._fullscreen)
        # task-20 ③：全屏时标题栏本来就 setVisible(False)；把淡入淡出状态一起复位，
        # 否则「全屏时刚好被淡出过」会让退出全屏的标题栏以 alpha=0 回来（隐身）。
        try:
            self._top_fader.set_visible_now(not self._fullscreen)
            if not self._fullscreen:
                self._top_fader.wake()
        except Exception:
            log.debug("标题栏淡入淡出状态复位失败", exc_info=True)
        if self._fullscreen:
            self.drawer.set_open(False)
            self._fs_saved_geo = self.geometry()
            self._fs_saved_max = self.isMaximized()
            scr = self.screen() or QApplication.primaryScreen()
            g = scr.geometry()                    # 整个显示器（含任务栏区域）
            if self._fs_saved_max:
                self.showNormal()                 # 先退出最大化，否则几何会被系统改写
                QApplication.processEvents()
            # 若历史状态里残留 FullScreen（旧版本升级/异常退出），先清掉它；
            # 本卡之后**绝不再设置** WindowFullScreen。
            try:
                st = self.windowState()
                if st & Qt.WindowFullScreen:
                    self.setWindowState(st & ~Qt.WindowFullScreen)
            except Exception:
                log.debug("清除残留 FullScreen 状态失败", exc_info=True)
            # ★ 关键：普通无边框窗口 + 几何放大；高度 +1px 绕过独占翻转
            self.setGeometry(g.x(), g.y(), g.width(), g.height() + 1)
            # ⚠️ 全屏必须**关掉 DWM 的原生圆角与窗口边框**：
            #    圆角是「按窗口矩形裁」的，画面铺满时四角会被切掉；
            #    边框是 DWM 自己画的一圈高光线，贴到屏幕边缘就是一圈「白边」。
            win.enable_rounded_corners(self, False)
            win.set_window_border(self, False)
            # 还有我们自己画的第二层：WindowBody 的 1px 描边 + 12px 圆角 + 布局内边距。
            self._set_body_fullscreen(True)
            log.info("全屏: geometry=%s screen=%s state=0x%X",
                     list(self.geometry().getRect()), list(g.getRect()),
                     int(self.windowState().value))
        else:
            # ★ 退出：不碰 WindowState，只把几何精确还原（同一 HWND/GL 上下文）
            win.enable_rounded_corners(self, True)
            win.set_window_border(self, True)
            self._set_body_fullscreen(False)
            geo = getattr(self, "_fs_saved_geo", None)
            if geo is not None and geo.isValid():
                # ★ task-59：**只调一次 setGeometry**（几何还原走 Qt，非额外 SetWindowPos），
                #   实测调用→几何稳定 ≤1 帧；旧的 move_window+setGeometry 双调用会多 ~4ms。
                self.setGeometry(geo)
            if getattr(self, "_fs_saved_max", False):
                self.showMaximized()
            self.titlebar.setVisible(True)
            self.apply_window_mask()      # ★ task-87：退出全屏恢复圆角遮罩
            log.info("退出全屏: geometry=%s saved=%s state=0x%X",
                     list(self.geometry().getRect()),
                     list(geo.getRect()) if geo is not None and geo.isValid() else None,
                     int(self.windowState().value))
        self.capsule.set_fullscreen(self._fullscreen)
        # ⚠️ 全屏**也要留着手柄**：它是在全屏下唯一能打开播放列表的边缘入口
        #    （进全屏时抽屉会被收起，见上面 self.drawer.set_open(False)）。
        #    原来这里写的是 not self._fullscreen and not self.drawer.is_open()，
        #    于是全屏下彻底没有入口 —— 用户反馈：「全屏时播放列表手柄消失」。
        self.stage.drawer_handle.setVisible(True)
        # ★ task-42：全屏往返后恢复/确保边缘悬停定时器（并清掉全屏前的陈旧驻留计时）。
        self._sync_edge_hover_timer()
        QTimer.singleShot(0, self.stage.video.update)

    def fullscreen_state(self) -> dict:
        """★ task-59 可读锚点：全屏态 / 几何 / 屏幕矩形 / 窗口状态标志（QA 直接读）。"""
        try:
            scr = self.screen() or QApplication.primaryScreen()
            sg = scr.geometry()
            geo = self.geometry()
            saved = getattr(self, "_fs_saved_geo", None)
            st = self.windowState()
            return {
                "fullscreen": bool(self._fullscreen),
                "geometry": list(geo.getRect()),
                "screen": list(sg.getRect()),
                "saved": (list(saved.getRect())
                          if saved is not None and saved.isValid() else None),
                "saved_max": bool(getattr(self, "_fs_saved_max", False)),
                "window_state": int(st.value),
                "state_maximized": bool(st & Qt.WindowMaximized),
                "state_fullscreen": bool(st & Qt.WindowFullScreen),
                "is_fullscreen_q": bool(self.isFullScreen()),
                "is_maximized": bool(self.isMaximized()),
                "titlebar_visible": bool(self.titlebar.isVisible()),
                "titlebar_opacity": self._topbar_opacity(),
                "cover_taskbar": bool(geo.y() + geo.height() >= sg.y() + sg.height()),
            }
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}
    # =====================================================================
    # OSD / 空状态
    # =====================================================================
    def show_osd(self, text: str, icon: str = "", color: str | None = None, ms: int = 1100):
        """显示 OSD 提示。

        ⚠️ 这里做了**合帧节流**：音量条拖动时 `valueChanged` 每一像素发一次,
        直接每次都去重排 OSD 会让拖动明显发涩（用户实测反馈"长按来回拖动音量条很不跟手"）。
        现在只把最后一次的内容记下来, 由 70ms 的合并定时器统一刷一次 —— 视觉上仍是实时的
        （>14Hz）, 但真正做布局与重绘的次数被压掉一个数量级。
        """
        if not self.cfg.get("ui.osd", True):
            return
        self._osd_pending = (text, icon, color, ms)
        if not self._osd_timer.isActive():
            self._osd_timer.start()

    def _flush_osd(self):
        if self._osd_pending is None:
            return
        text, icon, color, ms = self._osd_pending
        self._osd_pending = None
        self.stage.osd_host.setVisible(True)
        self.stage.osd.show_message(text, icon, color, ms)

    def _update_empty_state(self):
        has = bool(self._path)
        # 记录"当前是不是空状态页面"，再由 set_landing_visible 统一裁决显隐
        # （设置面板打开时它必须让位，否则两边互相压字）
        self.stage.set_landing_wanted(not has)
        self.stage.set_landing_visible(not self._settings_open())
        # 同步"有没有画面"给渲染控件：无媒体时它只清主题底色，不走 mpv
        # —— 否则 mpv 会把 FBO 画成黑色，浅色主题下内容区就是一块死黑。
        self.stage.video.set_has_video(has)
        if not has:
            self.stage.empty_state.set_recent(self.cfg.get("recent.files", []) or [])

    # =====================================================================
    # 胶囊自动隐藏 / 鼠标活动
    # =====================================================================
    def _start_timers(self):
        self._long_timer = QTimer(self)
        self._long_timer.setSingleShot(True)
        self._long_timer.setInterval(int(self.cfg.get("playback.long_press_threshold_ms", 300)))
        self._long_timer.timeout.connect(self._on_long_press_timer)
        # 快退回跳（mpv 不支持负速度，只能定时往回跳，见 _begin_speed_boost）
        self._rewind_timer = QTimer(self)
        self._rewind_timer.setSingleShot(False)
        # 60ms / 步：实测精确后退每步 < 60ms 跟得上（12 步 × -0.2s 正好退 2.4s）
        self._rewind_timer.setInterval(60)
        self._rewind_timer.timeout.connect(self._rewind_tick)
        self._rewind_rate = 2.0
        self._rewind_was_paused = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(int(self.cfg.get("ui.capsule_autohide_ms", 2800)))
        self._hide_timer.timeout.connect(self._maybe_hide_capsule)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._tick_status)
        self._status_timer.start()

        self._edge_timer = QTimer(self)
        # ★ task-31：120ms 的采样粒度对"450ms 驻留 / 中途移出立即重置"太粗（边界会误判），
        #   降到 40ms。**仍是同一只常驻定时器**（不新增定时器、不碰帧钟 16ms）。
        self._edge_timer.setInterval(40)
        self._edge_timer.timeout.connect(self._check_edge_hover)
        self._edge_timer.start()

        # OSD 合帧节流：把一秒钟上百次的拖动事件并成 ~14 次真正重排
        self._osd_pending = None
        self._osd_timer = QTimer(self)
        self._osd_timer.setSingleShot(True)
        self._osd_timer.setInterval(70)
        self._osd_timer.timeout.connect(self._flush_osd)

        self._fader = CapsuleFader(self.capsule)
        # task-20 ③：**顶栏 = 窗口标题栏 self.titlebar**（用户口径，Lead 已裁决）。
        # 注意别指错控件：stage.top_layer 只是"Tab 媒体信息浮层"的容器，平时是空的，
        # 对它淡出等于什么都没做。标题栏（标题/菜单/设置/主题/最小化/最大化/关闭）
        # 才是用户说的"上面那条"，非全屏时它同时还是窗口拖动把手。
        self._top_fader = CapsuleFader(self.titlebar, duration=260)
        # #6 图片沉浸模式用：底部图片控制栏 + 抽屉手柄各自一个淡入淡出器
        self._imgbar_fader = CapsuleFader(self.stage.image_bar_host, duration=240)
        self._handle_fader = CapsuleFader(self.stage.drawer_handle, duration=240)
        self._edge_suppress = False
        # ★★ task-31：三道闸门的运行时状态（时间一律用 time.monotonic()）
        self._close_cd_until = 0.0     # 冷却截止时刻（任何收起动作 ⇒ now + EDGE_CD_S）
        self._edge_rearmed = True      # 是否已"重武装"（收起后必须离开右侧 80px 才置 True）
        self._hot_since = None         # 进入热区的时刻（None = 当前不在热区）
        self._hot_in = False           # 上一次采样是否在热区内（探针/门禁可读）
        # 抽屉是"碰右边缘自动展开"还是"手动打开"：只有自动展开的才会在鼠标离开后收回
        self._drawer_auto_opened = False
        self._drawer_leave_ms = 0
        self._release_ts = 0.0            # 长按松开时刻（自测"恢复延迟"用）
        self._restore_latency_ms = None   # 松开 → 速度真的回到 1.0x 的耗时
        self.stage.drawer_handle.clicked.connect(self._on_playlist_button_manual)

    def _tick_status(self):
        if self._path and self._kind != router.KIND_IMAGE:
            pos = self.engine.cached("time-pos", None)
            dur = self.engine.cached("duration", None)
            if pos is not None:
                self._position = float(pos)
            if dur:
                self._duration = float(dur)
            self.capsule.set_times(self._position, self._duration)
        self._save_position(force=False)

    def _check_edge_hover(self):
        # ★★ task-42：**全模式统一**启用（视频 / 音频 / 图片一视同仁）。
        #    这里只保留两条**与媒体类型无关**的早退：
        #      ① 全屏（task-12 语义：全屏下不自动弹抽屉，手柄点击仍可用）；
        #         顺手清掉驻留计时，避免"进全屏前已驻留 300ms → 退出全屏瞬间展开"。
        #      ② 设置里把 ui.drawer_edge_hover 关掉（三模式一致）。
        #    ⚠️ 曾经这里有一条 if kind == KIND_IMAGE: return —— 看图模式因此
        #       永远进不了三道闸门（用户 ⑦「音乐/图片模式下悬停不展开」的真因之一）。
        if self._fullscreen:
            self._hot_in = False
            self._hot_since = None
            return
        gp = QCursor.pos()
        local = self.mapFromGlobal(gp)
        inside = (0 <= local.x() <= self.width() and 0 <= local.y() <= self.height())

        # ---- 自动收起：只有"碰边缘自动展开"的抽屉才会自动收回 ----
        # 用户明确要求："自动靠近展开的，鼠标离开后应该自动收回；只有手动点击/操作过才常驻"。
        if self.drawer.is_open() and self._drawer_auto_opened:
            if inside:
                d = self.drawer
                tl = d.mapTo(self, QPoint(0, 0))
                over = (tl.x() <= local.x() <= tl.x() + d.width()
                        and tl.y() <= local.y() <= tl.y() + d.height())
                near_edge = self.width() - local.x() <= M["drawer_edge_trigger"]
                if over or near_edge:
                    self._drawer_leave_ms = 0
                else:
                    # ★ task-42：离开计时步长必须跟**当前采样间隔**（40ms）走。
                    #   task-31 把采样从 120ms 改成 40ms 后，这里仍按 +120/tick 累加，
                    #   于是"离开 700ms 才收回"实际变成 ~240ms 就收回 —— 抽屉刚展开、
                    #   鼠标稍微一挪就消失（图片模式恢复自动展开后会非常明显）。
                    self._drawer_leave_ms += max(1, int(self._edge_timer.interval()))
                    if self._drawer_leave_ms >= 700:
                        self._drawer_leave_ms = 0
                        self._drawer_auto_opened = False
                        self._on_playlist_button(False)
            else:
                self._drawer_leave_ms = 0

        # ★ task-42：三模式统一走同一套闸门 —— 图片模式不再在这里提前 return。
        #   （手柄在 _apply_kind_ui 里对所有模式都保持可见；此前"看图模式自动展开
        #     仍停用"是旧需求，本卡已由用户明确改为"全模式都要能悬停展开"。）
        if not self.cfg.get("ui.drawer_edge_hover", True):
            self._hot_in = False
            self._hot_since = None
            return
        if self.drawer.is_open() or self._closing or not inside:
            self._hot_in = False
            self._hot_since = None
            return
        # ================= ★★ task-31 三道闸门 =================
        # 坐标一律取**舞台局部坐标**（卡片原文：x >= stage.width()−24 且 |y − stage.height()//2| ≤ 56）
        sp = self.stage.mapFromGlobal(gp)
        sw, sh = self.stage.width(), self.stage.height()
        in_hot = (sp.x() >= sw - self.EDGE_HOT_W
                  and abs(sp.y() - sh // 2) <= self.EDGE_HOT_HALF_H)
        right_band = (sw - sp.x() <= self.EDGE_REARM_PX)   # 是否仍在右侧 80px 区域内
        now = time.monotonic()
        # 闸门③-a 重武装：完全离开右侧 80px 区域 ⇒ 允许下一次自动展开
        if not right_band:
            self._edge_rearmed = True
        was_in = self._hot_in
        self._hot_in = in_hot
        if not in_hot:
            self._hot_since = None            # 闸门②：中途移出 ⇒ **立即重置**计时
            return
        # 闸门③-b 冷却：2.5s 内即使一直停在热区也绝不展开
        if now < self._close_cd_until:
            self._hot_since = None
            return
        # 闸门③-c 未重武装（收起后从未离开过右侧 80px）⇒ 展不开
        if not self._edge_rearmed:
            self._hot_since = None
            return
        # 闸门② 驻留：连续停留满 450ms 才展开
        if self._hot_since is None:
            self._hot_since = now
            return
        if now - self._hot_since < self.EDGE_DWELL_S:
            return
        self._hot_since = None
        self._drawer_auto_opened = True     # 标记"这是自动开的", 离开就收回
        self._drawer_leave_ms = 0
        self._on_playlist_button(True)

    def _sync_edge_hover_timer(self) -> bool:
        """★ task-42：**全模式统一**启用边缘悬停定时器（幂等；返回是否在跑）。

        视频 / 音频 / 图片三种媒体模式必须共用同一只 40ms 常驻定时器：
          · 模式切换（_apply_kind_ui）后调一次 —— 任何路径把定时器停掉都会被拉回来；
          · 退出全屏后调一次 —— 全屏期间 _check_edge_hover 自己早退，不在这里停表；
          · 幂等：已在跑就不动它（不重置 40ms 间隔、不丢已有计时）。
        顺手清掉热区驻留计时，避免跨模式/跨全屏残留一个"半截 450ms"。
        """
        timer = getattr(self, "_edge_timer", None)
        if timer is None:
            return False
        try:
            if not timer.isActive():
                timer.start()
        except Exception:
            log.debug("恢复边缘悬停定时器失败", exc_info=True)
        self._hot_in = False
        self._hot_since = None
        return bool(timer.isActive())

    def _suppress_edge_hover(self):
        """显式收起后抑制自动展开：旧标志位（兼容）+ task-31 的"解除重武装"。"""
        self._edge_suppress = True
        self._edge_rearmed = False
        self._hot_since = None

    def drawer_edge_state(self) -> dict:
        """★ task-31 可读锚点：三道闸门的实时状态 + 热区几何（QA/门禁直接读，不用摸私有量）。

        坐标：`hot_w/hot_half_h/rearm_px` 是卡片参数；`stage_w/stage_h` 是当前舞台尺寸；
        `cd_remaining_ms` 为 0 表示冷却已过；`in_hot` 为本次采样的热区判定结果。
        """
        now = time.monotonic()
        st = self.stage
        return {"hot_w": self.EDGE_HOT_W, "hot_half_h": self.EDGE_HOT_HALF_H,
                "dwell_ms": int(self.EDGE_DWELL_S * 1000), "cd_ms": int(self.EDGE_CD_S * 1000),
                "rearm_px": self.EDGE_REARM_PX,
                # ★ task-42 证据锚点：当前媒体模式 / 全屏态 / 定时器是否真的在跑
                "kind": str(self._kind), "fullscreen": bool(self._fullscreen),
                "edge_timer_active": bool(self._edge_timer.isActive()),
                "stage_w": int(st.width()), "stage_h": int(st.height()),
                "center_y": int(st.height() // 2),
                "close_cd_until": round(float(self._close_cd_until), 3),
                "cd_remaining_ms": max(0, int((self._close_cd_until - now) * 1000)),
                "edge_rearmed": bool(self._edge_rearmed),
                "in_hot": bool(getattr(self, "_hot_in", False)),
                "hot_since": (None if self._hot_since is None else round(self._hot_since, 3)),
                "auto_opened": bool(self._drawer_auto_opened),
                "drawer_open": bool(self.drawer.is_open()),
                "edge_timer_ms": int(self._edge_timer.interval())}

    # =====================================================================
    # ★ task-43：左上角「幽灵小手光标」可读锚点
    # =====================================================================
    def cursor_scan(self, x0: int = 0, y0: int = 0, x1: int = 150, y1: int = 150,
                    step: int = 10) -> dict:
        r"""★ task-43：左上角网格逐点「生效 cursor」扫描（QA/门禁复核入口；**后台可用**）。

        宿主：MainWindow 方法 —— eval 里写 m.cursor_scan(0, 0, 150, 150, 10)。
        坐标：**舞台局部像素**（用户说的「标题栏正下方 (0,0,120,60)」就在这里）。

        ⚠️ 不依赖前台：**不用** QApplication.widgetAt（Windows 原生命中测试在窗口
        被其它应用盖住时会 256/256 全返回 None，QA 环境实测）。这里走
        Stage.effective_cursor_at()（控件树递归命中：反向 Z 序 → 跳过 hidden /
        WA_TransparentForMouseEvents → 坐标映射下钻），纯 Qt 几何计算，后台稳定。

        判据（Lead 2026-10-18 裁决后的口径）：
          · 音频/视频模式：(0..150,0..150) 网格 **100% ArrowCursor** ⇒ violations 必须为空；
          · 图片模式：**幽灵控件**（Minimap / 热区等）必须 Arrow；图片视口自身的
            OpenHand（拖拽按下 ClosedHand）属**预期交互** ⇒ 计入 functional，不算违例。

        返回字段：
          kind / fullscreen / matrix_size [nx,ny] / points（紧凑矩阵 [[x,y,widget,cursor],...]）
          detail（逐点 dict：xy/widget/obj/cursor/chain/functional_expected）
          violations（非功能性手型点，判据必须为空）/ violation_count
          functional（图片视口功能性手型点）/ functional_count / non_arrow_count
          ghost_rect / ghost_guard / hit_path / stage_origin / stage_size
          同时把摘要写进 stage._last_cursor_matrix，可由 m.stage.ghost_state() 读回。
        """
        st = self.stage
        ghost_rect = getattr(st, "GHOST_RECT", (0, 0, 150, 150))
        step_i = max(1, int(step))
        xs = list(range(int(x0), int(x1) + 1, step_i))
        ys = list(range(int(y0), int(y1) + 1, step_i))
        is_image = str(self._kind) == router.KIND_IMAGE
        detail: list[dict] = []
        bad: list[dict] = []
        functional: list[dict] = []
        compact: list[list] = []
        for x in xs:
            for y in ys:
                hit = st.effective_cursor_at(QPoint(x, y))
                shape = str(hit.get("cursor") or "ArrowCursor")
                under_iv = bool(hit.get("under_image_view"))
                viewer_hand = bool(is_image and under_iv)
                rec = {"xy": [x, y], "widget": hit.get("widget"), "obj": hit.get("obj"),
                       "cursor": shape, "chain": hit.get("chain") or [],
                       "functional_expected": viewer_hand}
                detail.append(rec)
                compact.append([x, y, hit.get("widget"), shape])
                if shape != "ArrowCursor":
                    if viewer_hand:
                        functional.append(rec)
                    else:
                        bad.append(rec)
        summary = {
            "kind": str(self._kind),
            "fullscreen": bool(self._fullscreen),
            "matrix_size": [len(xs), len(ys)],
            "points": compact,
            "detail": detail,
            "violations": bad,
            "violation_count": len(bad),
            "non_arrow_count": len(bad) + len(functional),
            "functional": functional,
            "functional_count": len(functional),
            "ghost_rect": list(ghost_rect),
            "ghost_guard": dict(getattr(st, "_ghost_guard", {}) or {}),
            "hit_path": "Stage.effective_cursor_at (child-tree recursive; background-safe)",
            "stage_origin": [int(st.mapToGlobal(st.rect().topLeft()).x()),
                             int(st.mapToGlobal(st.rect().topLeft()).y())],
            "stage_size": [int(st.width()), int(st.height())],
        }
        try:
            st._last_cursor_matrix = {k: summary[k] for k in (
                "kind", "matrix_size", "violation_count", "functional_count",
                "non_arrow_count", "points", "violations", "ghost_rect")}
        except Exception:
            pass
        return summary

    def effective_cursor_at(self, x: int, y: int) -> dict:
        """★ task-43：不依赖前台的单点生效 cursor（QA 可直接 eval）。

        示例：m.effective_cursor_at(20, 20) →
        {"xy":[20,20], "widget":"HotspotZone", "obj":"HotspotZone",
         "cursor":"ArrowCursor", "chain":[...], "under_image_view":false}
        """
        return self.stage.effective_cursor_at(QPoint(int(x), int(y)))

    def _maybe_hide_capsule(self):
        if not self.cfg.get("ui.capsule_autohide", True):
            return
        # #6 图片模式：**沉浸模式** —— 静止一段时间后把底部图片控制栏与抽屉手柄一起淡出。
        #    指针停在控制栏上则重新计时（否则鼠标停在按钮上会突然消失，点不到）。
        if self._kind == router.KIND_IMAGE:
            try:
                if self.drawer.is_open():
                    return
                gp = QCursor.pos()
                bar = self.stage.image_bar
                tl = bar.mapToGlobal(bar.rect().topLeft())
                if (tl.x() <= gp.x() <= tl.x() + bar.width()
                        and tl.y() <= gp.y() <= tl.y() + bar.height()):
                    self._hide_timer.start()
                    return
                self._imgbar_fader.fade_out()
                self._handle_fader.fade_out()
            except Exception:
                log.debug("图片沉浸模式隐藏失败", exc_info=True)
            return
        # task-20 ③（用户⑫）：**暂停/空闲态也要沉浸** —— 原来这里写的是
        # `if self._paused or not self._path: return`，于是暂停后上下栏永远不收
        # （用户："暂停时那两条栏一直杵着挡画面"）。现在只保留"没有媒体不隐藏"
        # 与"浮层打开时不隐藏"（设置面板/抽屉/媒体信息）。
        if not self._path:
            return
        if self._settings_open():
            return
        if self.drawer.is_open():
            return
        if self.stage.media_info.isVisible():
            return
        # task-20 ③：指针停在**标题栏**上也不收（它现在是沉浸目标之一；
        # 和上面"指针停在控制条上就不收"同一个道理：鼠标还压着它就不该消失）。
        try:
            if self.titlebar.isVisible():
                gp = QCursor.pos()
                tr = self.titlebar.rect()
                tl = self.titlebar.mapToGlobal(tr.topLeft())
                if (tl.x() <= gp.x() <= tl.x() + tr.width()
                        and tl.y() <= gp.y() <= tl.y() + tr.height()):
                    self._hide_timer.start()
                    return
        except Exception:
            log.debug("标题栏悬停判定失败", exc_info=True)
        # 指针停在控制条上就不收
        gp = QCursor.pos()
        r = self.capsule.rect()
        tl = self.capsule.mapToGlobal(r.topLeft())
        if (tl.x() <= gp.x() <= tl.x() + r.width()) and (tl.y() <= gp.y() <= tl.y() + r.height()):
            self._hide_timer.start()
            return
        self._fader.fade_out()
        # task-20 ③：标题栏一起收（沉浸模式是"上下栏都让开画面"）
        try:
            self._top_fader.fade_out()
        except Exception:
            log.debug("标题栏淡出失败", exc_info=True)
        # 全屏下抽屉手柄跟控制栏**一起收**（用户要求："手柄应该跟控制栏什么的一起久不动后自动隐藏"）。
        # 窗口化时留着它 —— 那是常驻的播放列表入口，收了反而不好找。
        if self._fullscreen and not self.drawer.is_open():
            self.stage.drawer_handle.setVisible(False)

    # =====================================================================
    # ★★ task-58：顶栏强制恢复（沉浸淡出后再切模式/开文件不得「整条消失」）
    # =====================================================================
    def _topbar_opacity(self):
        """当前 TitleBar 的实际不透明度（没有 effect 时视为 1.0；供锚点/日志）。"""
        try:
            eff = getattr(self._top_fader, "_eff", None)
            return 1.0 if eff is None else round(float(eff.opacity()), 3)
        except Exception:
            return 1.0

    def _exit_immersive_if_needed(self):
        """★ task-58：退出沉浸隐藏态 —— 顶栏/胶囊/图片底栏/抽屉手柄全部恢复。

        背景（用户 ⑦ 真因）：音频模式空闲后 _maybe_hide_capsule() 调
        self._top_fader.fade_out() —— 它给 TitleBar 装了 QGraphicsOpacityEffect，
        并把 opacity 动画到 0.0。TitleBar 本身 isVisible 仍为 True、仍然接收鼠标，
        但整条栏（标题/主题/设置/最小化/最大化/关闭）**像素全透明** ⇒ 用户看到
        「标题栏整条消失」。切换到图片模式时旧的收口只唤醒 image_bar/handle，
        从不复位这个 effect。这里把所有淡出器一次性复位到可见 + opacity 1。
        """
        try:
            for f in (self._top_fader, self._fader, self._imgbar_fader, self._handle_fader):
                try:
                    f.set_visible_now(True)
                    anim = getattr(f, "_anim", None)
                    if anim is not None:
                        anim.stop()
                    eff = getattr(f, "_eff", None)
                    if eff is not None:
                        eff.setOpacity(1.0)
                except Exception:
                    log.debug("复位淡出器失败", exc_info=True)
            # 重新开始「空闲才隐藏」的计时，给用户一个完整的操作窗口
            try:
                self._hide_timer.stop()
                self._hide_timer.start()
            except Exception:
                pass
            if not self._fullscreen:
                try:
                    self.titlebar.show()
                except Exception:
                    pass
        except Exception:
            log.debug("退出沉浸态失败", exc_info=True)

    def _ensure_titlebar_visible(self, reason: str = ""):
        """★ task-58：**无条件**显示顶栏 + 透明度恢复 1.0（含 GraphicsOpacityEffect）。

        顺序（Lead 指定）：_exit_immersive_if_needed() → titlebar.show() →
        透明度 1.0（含 effect）→ titlebar.raise_()。
        全屏模式遵守全屏规矩：不强行 show（toggle_fullscreen 退出时会恢复并 wake）；
        但 effect/动画仍会被复位，保证退出全屏时不会以 alpha=0 回来。
        """
        try:
            self._exit_immersive_if_needed()
            if not self._fullscreen:
                self.titlebar.show()
            eff = getattr(self._top_fader, "_eff", None)
            if eff is not None:
                anim = getattr(self._top_fader, "_anim", None)
                if anim is not None:
                    anim.stop()
                eff.setOpacity(1.0)
            try:
                self._top_fader._visible = True
            except Exception:
                pass
            self.titlebar.raise_()
            log.info("顶栏恢复[%s]：visible=%s fullscreen=%s opacity=%s",
                     reason, bool(self.titlebar.isVisible()), bool(self._fullscreen),
                     self._topbar_opacity())
        except Exception:
            log.debug("恢复顶栏失败[%s]", reason, exc_info=True)

    def _wake_capsule(self):
        # #4 图片模式没有播放胶囊（进度条/音量条都不需要），不要把它唤出来；
        # #6 但要把**图片控制栏与手柄**唤回来（沉浸模式：动一下就回来）
        if self._kind == router.KIND_IMAGE:
            try:
                self._imgbar_fader.fade_in()
                self._handle_fader.fade_in()
                self._hide_timer.start()
            except Exception:
                log.debug("图片沉浸模式唤醒失败", exc_info=True)
            return
        self._fader.fade_in()
        # task-20 ③：标题栏跟着一起回来（沉浸模式是"上下栏都让开画面"）。
        # ⚠️ 非全屏下标题栏还是窗口拖动把手 —— 它用 alpha 淡出、**仍然接收鼠标事件**，
        # 且任意鼠标移动都会经应用级 eventFilter 走到这里 fade_in（260ms），
        # 所以"移到顶部→按下拖动"时它已经可见，不需要额外的 8px 热区。
        try:
            self._top_fader.fade_in()
        except Exception:
            log.debug("标题栏淡入失败", exc_info=True)
        self._hide_timer.start()
        if self._fullscreen and not self.stage.drawer_handle.isVisible() and not self.drawer.is_open():
            self.stage.drawer_handle.setVisible(True)

    def mouseMoveEvent(self, ev):
        self._wake_capsule()
        super().mouseMoveEvent(ev)

    def eventFilter(self, obj, ev):
        """应用级事件过滤器：胶囊自动隐藏 + **全部快捷键** + 长按倍速。

        ⚠️ 为什么快捷键必须放在这里而不是 MainWindow.keyPressEvent：
        按键先送给**焦点控件**。抽屉里的搜索框（QLineEdit）会吃掉方向键,
        列表（QAbstractItemView）也会 —— 于是"按右键快进"在有控件获得焦点时
        静默失效（本项目真实踩过：只有 keyRelease 冒泡上来, keyPress 被吃了,
        表现成"单击还能快进、长按死活不出倍速"）。
        事件过滤器在事件到达目标控件**之前**看到它, 才是唯一可靠的位置。

        ⚠️ 另外 PySide6 会**吞掉** eventFilter 里抛出的异常（只打一行日志继续）,
        一次 NameError 就能让整个键盘静默失效 —— 所以整体包 try/except。
        """
        try:
            t = ev.type()
            # ★★★ P0 热修（v1.0.45 就地修）：设置面板**外部点击**判定（三重防线）。
            #   只有在「目标不在面板子树内 **且** 全局坐标也不在面板矩形内」时才算外部，
            #   才 beep + 回弹；面板内任何点击一律放行（此前 100% 被误判成外部 ⇒ 面板死锁）。
            if obj is getattr(self.stage, "settings_host", None) and t in (
                    QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
                    QEvent.MouseButtonDblClick, QEvent.Wheel):
                if self._settings_open():
                    if self._settings_click_outside(obj, ev):
                        if t == QEvent.MouseButtonPress:
                            self._on_settings_outside_click()
                        ev.accept()
                        return True
                    return False
            # ★★ task-68：**画中画**期间的鼠标语义（必须在其它分支之前处理）——
            #    · 移入小窗 ⇒ 浮现"暂停/退出"两个按钮（离开 ⇒ 收起，且用位置复核防误收）；
            #    · **双击小窗 ⇒ 退出 PiP**（吞掉事件 ⇒ 不会再去走 stage 的"双击全屏"✗）。
            if self._pip and obj is getattr(self.stage, "video", None):
                if t == QEvent.MouseButtonDblClick:
                    self._pip_exit()
                    return True
                if t in (QEvent.Enter, QEvent.MouseMove):
                    self._pip_overlay_show()
                    self._pip_overlay_seat()
                elif t == QEvent.Leave:
                    QTimer.singleShot(160, self._pip_hover_recheck)
            # ★★ task-71：设置面板打开时 = 二级模态 —— 焦点不在面板内时**硬锁所有键盘**，
            #     Space / 方向键 / 其它播放快捷键一律不能穿透（Esc 仅用于关闭面板）。
            #     同时吃掉 ShortcutOverride，避免 QShortcut 在事件过滤器之后仍然触发。
            if t in (QEvent.KeyPress, QEvent.ShortcutOverride) and self._settings_open():
                cap = self.settings.active_capture() if self.settings is not None else None
                if cap is None:
                    in_settings = self._target_in_settings(obj)
                    # ★ task-71：播放控制键（Space / 方向键 / 翻页键）即使在设置面板内
                    #    获得焦点时也必须阻断；面板普通控件所需的其它键允许通过。
                    BLOCKED = (Qt.Key_Space, Qt.Key_Left, Qt.Key_Right, Qt.Key_Up,
                               Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown,
                               Qt.Key_Home, Qt.Key_End)
                    if ev.key() in BLOCKED:
                        ev.accept()
                        return True
                    if not in_settings:
                        if t == QEvent.KeyPress and ev.key() == Qt.Key_Escape:
                            self.toggle_settings(False)
                            return True
                        ev.accept()
                        return True
            if t == QEvent.MouseMove:
                self._wake_capsule()
            elif t == QEvent.KeyPress:
                if self.stage.settings_host.isVisible():
                    cap = self.settings.active_capture() if self.settings is not None else None
                    if cap is not None and not ev.isAutoRepeat():
                        # Delete/Backspace = 清空该槽位（用户想解绑时不用去改配置文件）
                        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                            self.settings.apply_captured_key(cap.action, "", cap.slot)
                            return True
                        spec = _key_to_spec(ev)
                        if spec:
                            self.settings.apply_captured_key(cap.action, spec, cap.slot)
                            return True
                        return True
                if self._hotkey_press(ev):
                    return True
            elif t == QEvent.KeyRelease:
                if self._hotkey_release(ev):
                    return True
        except Exception:
            log.exception("eventFilter 异常")
        return super().eventFilter(obj, ev)

    # ★★ task-31（用户 ⑦）：抽屉自动展开的**三道闸门**（用户指定参数，逐字实现）。
    EDGE_HOT_W = 24          # 热区：距舞台右边缘 ≤24px（**仅手柄周边**，废除全高度右边缘）
    EDGE_HOT_HALF_H = 56     # 热区半高：|y − stage.height()//2| ≤ 56（手柄上下各 28px）
    EDGE_DWELL_S = 0.45      # 驻留确认：连续停留满 450ms 才展开（移出立即重置）
    EDGE_CD_S = 2.5          # 冷却：任何收起动作后 2.5s 内绝对禁止自动展开
    EDGE_REARM_PX = 80       # 重武装：必须先完全离开右侧 80px 区域，才能再次触发

    TEXT_INPUTS = (QLineEdit, QAbstractSpinBox, QComboBox)

    def _text_input_focused(self) -> bool:
        fw = QApplication.focusWidget()
        try:
            from PySide6.QtWidgets import QAbstractSpinBox as _S, QLineEdit as _L, QTextEdit, QPlainTextEdit

            return isinstance(fw, (_L, _S, QTextEdit, QPlainTextEdit))
        except Exception:
            return isinstance(fw, self.TEXT_INPUTS)

    def _hotkeys_allowed(self, ev) -> bool:
        """什么情况下不该抢按键。"""
        if self._closing:
            return False
        app = QApplication.instance()
        if app.activeModalWidget() is not None:
            return False            # 原生/模态对话框优先（比如文件选择框里输入路径）
        if app.activeWindow() is not None and app.activeWindow() is not self:
            return False
        if self._text_input_focused():
            # 正在文本框里打字：只保留 Esc（退出输入态）, 其余交给文本框
            return ev.key() == Qt.Key_Escape
        return True

    def _hotkey_press(self, ev) -> bool:
        if not self._hotkeys_allowed(ev):
            return False
        self._note_key("press", ev)
        # ---- Tab：媒体信息浮层（PotPlayer 风格，左上角）----
        # ⚠️ 必须在**事件过滤器**里吃掉：Tab 本来是 Qt 的焦点切换键，
        #    放给控件就永远轮不到我们（这也是快捷键统一放这里的原因）。
        if ev.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            self._toggle_media_info()
            return True
        if ev.key() == Qt.Key_Escape:
            if self.stage.settings_host.isVisible():
                self.toggle_settings(False)
                return True
            if self.subtitle_panel is not None and self.subtitle_panel.isVisible():
                self.toggle_subtitle_panel(False)
                return True
            if self.stage.media_info.isVisible():
                self.stage.media_info.setVisible(False)
                return True
            if self._fullscreen:
                self.toggle_fullscreen()
                return True
            if self.drawer.is_open():
                self._on_playlist_button(False)
                return True
            if self._text_input_focused():
                self.setFocus(Qt.OtherFocusReason)
                return True
            return False
        # ---- ±15 秒（"加强版方向键"）：**走配置**，默认 Shift+←/→ 与 Shift+A/D ----
        # ⚠️ 以前这里是硬编码的 `Shift+方向键`；现在它是普通快捷键
        #    （hotkeys.seek_fwd_shift / seek_back_shift），用户能改、也能绑到 WASD 上。
        # ⚠️ 这一条**放行自动重复**（不拦 isAutoRepeat）：按住就是连跳。
        if self._match(ev, "seek_fwd_shift") or self._match(ev, "seek_back_shift"):
            fwd = self._match(ev, "seek_fwd_shift")
            if self._kind == router.KIND_IMAGE:
                self._step_image(1 if fwd else -1)
                return True
            step = float(self.cfg.get("playback.seek_step_shift", 15))
            self.engine.seek_relative(step if fwd else -step)
            self.show_osd(f"{'+' if fwd else '-'}{step:g}s", "forward" if fwd else "rewind", ms=600)
            return True
        # ---- 长按倍速：快进/快退**跟随用户绑定**（默认 →/D 快进，←/A 快退）----
        if self._match(ev, "seek_fwd_small") or self._match(ev, "seek_back_small"):
            if ev.isAutoRepeat():
                return True          # 自动重复一律不处理（否则按住会疯狂 ±5s）
            fwd = self._match(ev, "seek_fwd_small")
            if self._kind == router.KIND_IMAGE:
                self._step_image(1 if fwd else -1)
                return True
            self._boost_dir = 1 if fwd else -1
            self._right_down = True          # 旧名字保留：探针与既有逻辑都在读它
            self._long_timer.start()
            return True
        if self._kind == router.KIND_IMAGE and self._handle_image_keys(ev):
            return True
        return self._dispatch_hotkey(ev)

    def _hotkey_release(self, ev) -> bool:
        if not (self._match(ev, "seek_fwd_small") or self._match(ev, "seek_back_small")):
            return False
        if ev.isAutoRepeat():
            return True
        if self._kind == router.KIND_IMAGE:
            return True
        self._long_timer.stop()
        self._right_down = False
        self._release_ts = time.perf_counter()      # 供"恢复延迟"自测用
        if self._boost:
            self._end_speed_boost()
        elif self._match(ev, "seek_fwd_shift") or self._match(ev, "seek_back_shift"):
            pass                                    # ±15s 档在按下时已经跳过，松开不再补跳
        else:
            fwd = self._match(ev, "seek_fwd_small")
            step = float(self.cfg.get("playback.seek_step_small", 5))
            self.engine.seek_relative(step if fwd else -step)
            self.show_osd(f"{'+' if fwd else '-'}{step:g}s", "forward" if fwd else "rewind", ms=600)
        return True

    # =====================================================================
    # 快捷键
    # =====================================================================
    def _install_hotkeys(self):
        QApplication.instance().installEventFilter(self)

    def _match(self, ev, action: str) -> bool:
        keys = self.cfg.get(f"hotkeys.{action}", []) or []
        if not keys:
            return False
        spec = _key_to_spec(ev)
        return spec in keys

    def keyPressEvent(self, ev):
        """兜底：未被应用级过滤器拦下的（正常不会走到这里）。"""
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        super().keyReleaseEvent(ev)

    def _handle_image_keys(self, ev) -> bool:
        k = ev.key()
        if k == Qt.Key_Left:
            self._step_image(-1)
            return True
        if k == Qt.Key_Right:
            self._right_down = False
            self._long_timer.stop()
            self._step_image(1)
            return True
        if k in (Qt.Key_Up):
            self._zoom_image(1 / float(self.cfg.get("ui.image_zoom_step", 1.12)))
            return True
        if k in (Qt.Key_Down):
            self._zoom_image(float(self.cfg.get("ui.image_zoom_step", 1.12)))
            return True
        if k == Qt.Key_R and ev.modifiers() & Qt.ShiftModifier:
            self._rotate_image(-90)
            return True
        if k == Qt.Key_R:
            self._rotate_image(90)
            return True
        if k == Qt.Key_E:
            self._rotate_image(-90)
            return True
        if k == Qt.Key_H:
            self._flip_image()
            return True
        if k == Qt.Key_1:
            self._set_image_fit("fit")
            return True
        if k == Qt.Key_2:
            self._set_image_fit("one")
            return True
        return False

    def _dispatch_hotkey(self, ev) -> bool:
        kind = self._kind
        if self._match(ev, "play_pause"):
            self.toggle_pause()
            return True
        # ⚠️ 这条以前是**缺的**：config 里写着 hotkeys.stop = ["X"]，但没有任何分支消费它。
        #    2026-10-10 用户要求去掉胶囊上的「停止」按钮，才发现停止功能此前**只靠那个按钮**，
        #    X 键是死配置 —— 不补上就等于把功能删了。
        if self._match(ev, "stop"):
            self.stop()
            self.show_osd("停止", "stop", ms=700)
            return True
        if self._match(ev, "seek_back_small"):
            step = float(self.cfg.get("playback.seek_step_small", 5))
            self.engine.seek_relative(-step)
            self.show_osd(f"-{step:g}s", "rewind", ms=600)
            return True
        if self._match(ev, "seek_fwd_small"):
            step = float(self.cfg.get("playback.seek_step_small", 5))
            self.engine.seek_relative(step)
            self.show_osd(f"+{step:g}s", "forward", ms=600)
            return True
        if self._match(ev, "seek_back_large"):
            self.engine.seek_relative(-float(self.cfg.get("playback.seek_step_large", 30)))
            return True
        if self._match(ev, "seek_fwd_large"):
            self.engine.seek_relative(float(self.cfg.get("playback.seek_step_large", 30)))
            return True
        if self._match(ev, "volume_up"):
            self._on_volume(min(100.0, self.capsule.volume.value() + float(self.cfg.get("playback.volume_step", 5))))
            self.capsule.set_volume(self.capsule.volume.value())
            return True
        if self._match(ev, "volume_down"):
            self._on_volume(max(0.0, self.capsule.volume.value() - float(self.cfg.get("playback.volume_step", 5))))
            self.capsule.set_volume(self.capsule.volume.value())
            return True
        if self._match(ev, "mute"):
            self._on_mute(not self.capsule._muted)
            self.capsule.set_mute(self.capsule._muted)
            return True
        if self._match(ev, "media_info"):
            self._toggle_media_info()
            return True
        if self._match(ev, "fullscreen"):
            self.toggle_fullscreen()
            return True
        if self._match(ev, "playlist"):
            self._on_playlist_button(not self.drawer.is_open())
            return True
        if self._match(ev, "subtitle_panel"):
            self.toggle_subtitle_panel()
            return True
        if self._match(ev, "settings"):
            self.toggle_settings()
            return True
        if self._match(ev, "open_file"):
            self.open_files_dialog()
            return True
        if self._match(ev, "open_folder"):
            self.open_folder_dialog()
            return True
        if self._match(ev, "screenshot"):
            self.take_screenshot()
            return True
        if self._match(ev, "speed_up"):
            self.set_speed(self._speed + float(self.cfg.get("playback.speed_step", 0.25)))
            return True
        if self._match(ev, "speed_down"):
            self.set_speed(self._speed - float(self.cfg.get("playback.speed_step", 0.25)))
            return True
        if self._match(ev, "speed_reset"):
            self.set_speed(1.0)
            return True
        if self._match(ev, "prev_file"):
            self._step_file(-1)
            return True
        if self._match(ev, "next_file"):
            self._step_file(1)
            return True
        if self._match(ev, "frame_back") and kind == router.KIND_VIDEO:
            self._step_frame(-1)
            return True
        if self._match(ev, "frame_fwd") and kind == router.KIND_VIDEO:
            self._step_frame(1)
            return True
        if self._match(ev, "always_on_top"):
            self.toggle_always_on_top()
            return True
        return False

    def _wheel_over_panel(self, ev) -> bool:
        """滚轮是不是发生在"自己的面板"上面（设置 / 字幕 / 播放列表）。

        ⚠️ 必须显式判断, 不能指望子控件吃掉事件：QScrollArea 滚到顶/底之后
        会把 wheel 事件**忽略掉**, 它就会冒泡到主窗口 —— 于是用户在设置面板里
        滚到底再滚一下, 音量就被改了, OSD 弹出来把设置面板挤下去（用户实测反馈）。
        """
        try:
            gp = ev.globalPosition().toPoint()
        except Exception:
            gp = QCursor.pos()
        w = QApplication.widgetAt(gp)
        panel = getattr(self, "settings", None)
        sub = getattr(self, "subtitle_panel", None)
        drw = getattr(self, "drawer", None)
        while w is not None and w is not self:
            if w in (panel, sub, drw):
                return True
            if panel is not None and panel.isAncestorOf(w):
                return True
            if sub is not None and sub.isAncestorOf(w):
                return True
            if drw is not None and drw.isAncestorOf(w):
                return True
            w = w.parentWidget()
        return False

    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if d == 0:
            return
        # 面板上的滚轮归面板（设置面板要滚动、列表要滚动）, 不能拿来调音量
        if self._wheel_over_panel(ev):
            ev.ignore()
            return
        if self._kind == router.KIND_IMAGE:
            # ⚠️⚠️ 这里曾经是 `step ** (1 if d < 0 else -1)` —— 上滑(d>0)取 -1 = **缩小**，
            #    与视口内部的实现**方向正好相反**。滚轮只要落在任意覆盖层（顶栏/底栏/
            #    图片条/中间层）上就会冒泡到主窗口，于是"上滑变小"复现。
            #    现在两处**共用同一个常量与同一方向**，不再有两份真相。
            from .image_view import WHEEL_STEP as _WS

            self._iv_zoom(_WS if d > 0 else 1.0 / _WS)
            self._show_zoom_osd()          # #3 立即刷新（不防抖），1.0s 后淡出
        else:
            delta = float(self.cfg.get("playback.volume_step", 5)) * (1 if d > 0 else -1)
            self._on_volume(max(0.0, min(100.0, self.capsule.volume.value() + delta)))
            self.capsule.set_volume(self.capsule.volume.value())
        ev.accept()

    # =====================================================================
    # 事件结束 / 续播
    # =====================================================================
    def _on_file_loaded(self):
        self._paused = False
        self.capsule.set_paused(False)
        if self._kind == router.KIND_AUDIO:
            self._load_audio_tags()
        if self._kind == router.KIND_IMAGE:
            self._update_image_info()
        elif self._kind == router.KIND_VIDEO:
            # #7 只有**视频**把像素写进标题栏；音频/图片必须保持 _title_px 为空
            #    （task-69：音频之前会走到这里，把上一部视频/内嵌封面的 1920×1080
            #     重新塞回标题栏，只用 open_path 清空挡不住后续的 loaded 事件）。
            try:
                w = int(self.engine.cached("width", 0) or 0)
                h = int(self.engine.cached("height", 0) or 0)
                if w and h:
                    self._set_title_px(w, h)
                else:
                    # 首次启动视频时尺寸可能晚到：短暂重试补写（不新增常驻定时器）
                    self._schedule_video_title_px()
            except Exception:
                pass
        mode = str(self.cfg.get("playback.loop_mode", "sequence"))
        if self._kind != router.KIND_IMAGE:
            # 图片模式下 mpv 是停止状态，不需要（也不应该）再写它 —— 每条指令都是同步 IPC
            self.engine.set("loop-file", "inf" if mode == "single" else "no", MPV_FORMAT_STRING)
        self._auto_sub_notice()
        if self.subtitle_panel is not None and self.subtitle_panel.isVisible():
            self._refresh_subtitle_tracks()

    def _on_end_file(self, reason: int, error: int):
        if self._closing:
            return
        if error:
            self.show_osd("播放失败：文件可能已损坏或格式不受支持", "info", P["danger"], ms=2600)
            return
        # ⚠️ 原写法 `reason in (0)`：`(0)` 是 **int** 而非元组 ⇒ `reason in 0` 抛
        #    TypeError: argument of type 'int' is not iterable。图片播放每次 EOF 都会
        #    命中这里 ⇒ 异常风暴（30 张压测日志里刷屏的就是它，也拖垮了事件循环）。
        if int(reason or 0) == 0:   # EOF
            self._save_position(final=True)
            mode = str(self.cfg.get("playback.loop_mode", "sequence"))
            if self._kind == router.KIND_IMAGE:
                return
            if mode == "single":
                self.engine.seek_absolute(0.0)
                self.engine.set("pause", False, MPV_FORMAT_FLAG)
                return
            items = self.drawer.paths()
            if not items:
                return
            try:
                i = [os.path.normcase(p) for p in items].index(os.path.normcase(self._path))
            except ValueError:
                i = -1
            nxt = i + 1
            if nxt >= len(items):
                if mode == "loop":
                    nxt = 0
                else:
                    self.show_osd("播放结束", "info", ms=1600)
                    return
            elif mode == "random":
                import random

                nxt = random.randrange(len(items))
            self.open_path(items[nxt], remember_dir=False)

    def _maybe_resume_position(self, path: str):
        if not self.cfg.get("playback.remember_position", True):
            return
        rec = self._positions.get(os.path.normcase(path))
        if not rec:
            return
        pos = float(rec.get("pos", 0.0) or 0.0)
        dur = float(rec.get("dur", 0.0) or 0.0)
        if pos < float(self.cfg.get("playback.resume_min_secs", 15)):
            return
        if dur and pos > dur - 10:
            return

        def do_resume():
            self.engine.seek_absolute(pos)
            self.show_osd(f"已从 {fmt_time(pos)} 继续播放", "clock", ms=1800)

        QTimer.singleShot(400, do_resume)

    def _save_position(self, force: bool = False, final: bool = False):
        if not self._path or not self.cfg.get("playback.remember_position", True):
            return
        now = time.time()
        if not force and now - self._last_save_pos < 5.0:
            return
        self._last_save_pos = now
        pos = self.engine.cached("time-pos", None)
        dur = self.engine.cached("duration", None)
        if pos is None:
            return
        if dur and float(pos) > float(dur) - 5:
            self._positions.pop(os.path.normcase(self._path), None)
        else:
            self._positions[os.path.normcase(self._path)] = {"pos": float(pos), "dur": float(dur or 0.0),
                                                             "t": now, "path": self._path}
        try:
            self.cfg.save_positions(dict(list(self._positions.items())[-400:]))
        except Exception:
            log.debug("保存进度失败", exc_info=True)

    # =====================================================================
    # 设置面板回调
    # =====================================================================
    def _on_hotkey_changed(self, action: str, spec: str, slot: int = 0):
        """设置页改了某个槽位：写回 `hotkeys.<action>[slot]`。

        ⚠️ 每个功能是**两个槽位**（主键/副键），别整条覆盖 —— 否则改副键会把主键冲掉。
        """
        if action == "*":
            self.cfg.save()
            return
        keys = list(self.cfg.get(f"hotkeys.{action}", []) or [])
        while len(keys) <= slot:
            keys.append("")
        keys[slot] = spec or ""
        self.cfg.set(f"hotkeys.{action}", keys)
        self.cfg.save()
        self.show_osd("快捷键已更新", "keyboard", ms=900)

    def _on_settings_reset(self):
        if self.settings is not None:
            self.settings.refresh_hotkeys()
        self.capsule.set_loop_mode(str(self.cfg.get("playback.loop_mode", "sequence")))
        self._long_timer.setInterval(int(self.cfg.get("playback.long_press_threshold_ms", 300)))
        self._hide_timer.setInterval(int(self.cfg.get("ui.capsule_autohide_ms", 2800)))
        self.drawer.set_target_width(int(self.cfg.get("ui.drawer_width", M["drawer_w"])))
        self.show_osd("已恢复默认设置", "reload")

    # =====================================================================
    # 原生窗口：无边框 + 系统级缩放/移动 + 拖放
    # =====================================================================
    def _nc_hit_buttons(self) -> dict:
        r = self.titlebar.btn_max
        return {"max": r.geometry().translated(r.mapTo(self, QPoint(0, 0)))}

    def _is_interactive(self, w) -> bool:
        from .widgets import IconButton, TextIconButton

        if w is None:
            return False
        return isinstance(w, (QAbstractButton, QAbstractSlider, QLineEdit, QAbstractItemView,
                              QComboBox, QAbstractSpinBox, IconButton, TextIconButton))

    def _classify_client(self, lp) -> int:
        w = self.childAt(lp)
        if w is None:
            return win.HTCLIENT
        if self._is_interactive(w):
            return win.HTCLIENT
        node = w
        while node is not None and node is not self:
            if node.property("win_drag") is True:
                return win.HTCAPTION
            node = node.parentWidget()
        return win.HTCLIENT

    def nativeEvent(self, eventType, message):  # noqa: N802
        if win.IS_WINDOWS and os.environ.get("ZHJ_NO_NATIVE") != "1":
            try:
                et = bytes(eventType) if not isinstance(eventType, str) else eventType.encode()
                if et == b"windows_generic_MSG":
                    try:
                        addr = int(message)
                    except Exception:
                        addr = message.__int__()
                    msg = ctypes.cast(addr, ctypes.POINTER(wintypes.MSG)).contents
                    hwnd = int(msg.hWnd or 0)
                    if (msg.message == win.WM_NCCALCSIZE and msg.wParam
                            and os.environ.get("ZHJ_NO_NCCALC") != "1"):
                        win.handle_nc_calcsize(hwnd, msg.lParam)
                        # 返回 True = 不再交给 Qt 的默认处理（我们自己已经把客户区定好了）。
                        # ⚠️ 早先"全屏后 Qt 以为窗口还是原来那么大"的锅**不在这个 True**，
                        #    而在 handle_nc_calcsize 里把 rgrc[0] 赋成了 rgrc[1]（见那边的注释）。
                        #    那行修掉之后，True/False 都能正常更新几何，这里保持 True 更干净。
                        return True, 0
                    if (msg.message == win.WM_NCHITTEST
                            and os.environ.get("ZHJ_NO_HITTEST") != "1"):
                        code = win.hit_test(self, msg.lParam, M["resize_border"],
                                            M["titlebar_h"], self._nc_hit_buttons(),
                                            resize_enabled=not self._fullscreen)
                        if code == win.HTCLIENT:
                            code = self._classify_client(win.local_point(self, msg.lParam))
                        if os.environ.get("ZHJ_LOG_HITTEST"):
                            lp = win.local_point(self, msg.lParam)
                            log.info("  [HT] local=(%s,%s) win=%sx%s -> %s",
                                     lp.x(), lp.y(), self.width(), self.height(), code)
                        return True, code
                    if msg.message == win.WM_SYSCOMMAND and (msg.wParam & 0xFFF0) == win.SC_KEYMENU:
                        return True, 0
            except Exception:
                log.debug("nativeEvent 处理失败", exc_info=True)
        return super().nativeEvent(eventType, message)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.apply_window_mask()          # ★ task-87：首次显示/从隐藏恢复都套圆角遮罩

        super().showEvent(ev)
        if not getattr(self, "_native_ready", False):
            self._native_ready = True
            import time as _t

            def _m(lbl, t0):
                log.info("  [窗口] %-20s %6.0f ms", lbl, (_t.perf_counter() - t0) * 1000)
                return _t.perf_counter()

            t = _t.perf_counter()
            self._t_first_frame = t          # ★ task-39①：首帧（showEvent 首次到达）时刻
            win.make_resizable_frameless(self)
            t = _m("补 WS_THICKFRAME", t)
            win.enable_rounded_corners(self)
            t = _m("DWM 圆角", t)
            # ★★ task-39①（用户 ⑪）：**懒初始化** —— DWM 深色 + 窗口级亚克力实测 **152ms**
            #   （日志 [窗口] DWM 深色），它们只影响标题栏色调/边缘模糊，**与首帧内容无关**。
            #   改为首帧之后下一轮事件循环再上 ⇒ 窗口提前 ~150ms 上屏"骨架"，
            #   一帧后拿到完全相同的观感（卡片："主窗口先秒级上屏骨架"）。
            def _apply_chrome_deferred() -> None:
                _t0 = _t.perf_counter()
                try:
                    win.set_dark_mode(self, True)
                    # ⚠️ 只有**真的开上了**才把舞台底改成半透明 ——
                    #    否则系统不支持时会露出黑底/垃圾。
                    self._acrylic_on = win.set_acrylic(self, True)
                    if self._acrylic_on:
                        self._apply_acrylic_alpha()
                except Exception:
                    log.debug("延迟窗口材质失败", exc_info=True)
                self._t_chrome_done = _t.perf_counter()
                log.info("  [窗口] DWM 深色+亚克力(首帧后延后) %6.0f ms",
                         (self._t_chrome_done - _t0) * 1000)

            self._t_deferred_start = _t.perf_counter()
            QTimer.singleShot(0, _apply_chrome_deferred)
            self.titlebar.raise_()
            _m("抬标题栏", t)
            _m("窗口骨架就绪(延后项已排队)", self._t_first_frame)
            log.info("  [窗口] 客户区 %sx%s  dpr=%s", self.width(), self.height(),
                     self.devicePixelRatioF())

    def startup_report(self) -> dict:
        """★ task-39① 可读锚点：冷启动阶段与**被延后项**的实例化/执行时间戳（秒，perf_counter）。

        `t_first_frame` = 首帧到达（showEvent 首次）；`t_deferred_*` = 被延后项（DWM 深色+亚克力）
        的排队/完成时刻 ⇒ "被延后项在首帧之后多久执行"一目了然（QA 取证用）。
        """
        from . import theme as _theme
        d = {"t_first_frame": getattr(self, "_t_first_frame", None),
             "t_deferred_start": getattr(self, "_t_deferred_start", None),
             "t_deferred_done": getattr(self, "_t_chrome_done", None),
             "items": ["DWM 深色 + 窗口级亚克力（原 152ms，已延后到首帧之后）"],
             "qss_cache": _theme.qss_cache_stats()}
        if d["t_first_frame"] and d["t_deferred_start"]:
            d["deferred_after_first_frame_ms"] = round((d["t_deferred_start"] - d["t_first_frame"]) * 1000, 2)
        if d["t_deferred_done"] and d["t_deferred_start"]:
            d["deferred_work_ms"] = round((d["t_deferred_done"] - d["t_deferred_start"]) * 1000, 2)
        return d

    def changeEvent(self, ev):
        if ev.type() == QEvent.WindowStateChange:
            try:
                self.stage.sync_hotspots()
            except Exception:
                pass
            self.titlebar.set_maximized(self.isMaximized())
            # #10 全屏状态变化 ⇒ 同步「全屏 / 退出全屏」图标。
            #    ⚠️ 必须挂这里：双击标题栏、F11、系统菜单等路径都能改窗口状态，
            #    只在 toggle_fullscreen 里换图标会漏掉。
            try:
                # ★ task-59：几何全屏不再设置 WindowFullScreen 状态，改看 _fullscreen 标志
                on = bool(self._fullscreen or self.isFullScreen())
                self.stage.image_bar.set_fullscreen(on)
                self.capsule.set_fullscreen(on)
            except Exception:
                log.debug("同步全屏图标失败", exc_info=True)
        super().changeEvent(ev)

    def contextMenuEvent(self, ev):
        menu = RoundedMenu(self)
        menu.add_row("播放 / 暂停", "Space", on_click=self.toggle_pause)
        # 胶囊上的「停止」按钮已移除 ⇒ 这里给一个可见入口（另有快捷键 X）
        menu.add_row("停止", "X", on_click=self.stop)
        menu.add_row("全屏", "F", on_click=self.toggle_fullscreen)
        menu.addSeparator()
        menu.add_row("上一张 / 上一集", "←", on_click=lambda: (
            self._step_image(-1) if self._kind == router.KIND_IMAGE else self._step_file(-1)))
        menu.add_row("下一张 / 下一集", "→", on_click=lambda: (
            self._step_image(1) if self._kind == router.KIND_IMAGE else self._step_file(1)))
        menu.addSeparator()
        menu.add_row("截图", "S", on_click=self.take_screenshot)
        menu.add_row("在资源管理器中显示", on_click=lambda: win.reveal_in_explorer(self._path))
        menu.add_row("文件信息…", "Tab", on_click=self._show_file_info)
        menu.addSeparator()
        menu.add_row("设置…", "Ctrl + ,", on_click=lambda: self.toggle_settings(True))
        menu.exec(ev.globalPos())

    def _show_file_info(self):
        if not self._path:
            return
        try:
            st = os.stat(self._path)
            size = scanner.human_size(st.st_size)
        except OSError:
            size = "?"
        hw = self.engine.cached("hwdec-current", None) or "软件"
        rows = [
            f"文件：{os.path.basename(self._path)}",
            f"大小：{size}",
            f"时长：{fmt_time(self._duration)}",
            f"分辨率：{self.engine.cached('width', '?')}×{self.engine.cached('height', '?')}",
            f"视频编码：{self.engine.cached('video-codec', '—')}",
            f"音频编码：{self.engine.cached('audio-codec-name', '—')}",
            f"硬件解码：{hw}",
        ]
        self.show_osd(" · ".join(rows[:3]), "info", ms=3200)

    # =====================================================================
    # 几何 / 关闭
    # =====================================================================
    def _restore_geometry(self):
        if self.cfg.get("ui.remember_window", True):
            w = int(self.cfg.get("ui.window_w", M["win_w"]))
            h = int(self.cfg.get("ui.window_h", M["win_h"]))
            x = self.cfg.get("ui.window_x")
            y = self.cfg.get("ui.window_y")
            # ★★ task-96（用户 95）：**冷启动尺寸夹取** —— 即使配置文件被写成 480×270（PiP 尺寸），
            #   也至少要 960×640 起来（用户下一次打开不能是"小窗"✗）。
            w = max(960, int(M["win_min_w"]), w)
            h = max(640, int(M["win_min_h"]), h)
            # ⚠️ 位置与尺寸必须**一次性**给（setGeometry）。分开 resize + move 时,
            #    无边框窗口上的 QWidget.move() 会让 Qt 缓存尺寸与原生窗口失步（见 win.move_window）。
            if x is not None and y is not None:
                wa = win.monitor_work_area(self)
                if wa:
                    mx, my, mw, mh = wa
                    if not (mx - 50 <= int(x) <= mx + mw - 100 and my - 50 <= int(y) <= my + mh - 100):
                        x, y = None, None
                if x is not None:
                    self.setGeometry(int(x), int(y), w, h)
                    return
            self.resize(w, h)
        else:
            self.resize(M["win_w"], M["win_h"])

    def _store_geometry(self):
        if not self.cfg.get("ui.remember_window", True):
            return
        # 无边框全屏下窗口就是整个显示器，存下来下次会把"全屏尺寸"当成普通窗口尺寸
        if self.isFullScreen() or self._fullscreen:
            return
        self.cfg.set("ui.maximized", bool(self.isMaximized()) and not bool(getattr(self, "_pip", False)))
        if not self.isMaximized() or bool(getattr(self, "_pip", False)):
            g = self.geometry()
            # ★★ task-96（用户 95）：**PiP 下关闭程序会污染几何** —— 小窗是 480×270，
            #   直接存进 config ⇒ 下次启动就是个小窗 ✗。这里若在 PiP 且记录过正常几何，
            #   **一律写回进入 PiP 前的宽高/位置**（判据：PiP 关程序后 config 非 480×270 ✓）
            _pip_on = bool(getattr(self, "_pip", False))
            _saved = getattr(self, "_pip_saved_geo", None)
            if _pip_on and _saved is not None:
                try:
                    g = _saved
                except Exception:
                    log.debug("PiP 恢复几何用于落盘失败", exc_info=True)
            self.cfg.set("ui.window_x", int(g.x()))
            self.cfg.set("ui.window_y", int(g.y()))
            self.cfg.set("ui.window_w", int(g.width()))
            self.cfg.set("ui.window_h", int(g.height()))
        self.cfg.set("ui.drawer_open", self.drawer.is_open())
        self.cfg.save()

    def closeEvent(self, ev):
        if self._closing:
            ev.accept()
            return
        self._closing = True
        self.shutting_down.emit()
        log.info("shutdown: 开始")
        # 1) 停掉所有定时器
        for t in ("_long_timer", "_hide_timer", "_status_timer", "_edge_timer", "_osd_timer"):
            tm = getattr(self, t, None)
            if tm is not None:
                tm.stop()
        log.info("shutdown: 定时器已停")
        # 2) 落盘
        try:
            self._save_position(force=True, final=True)
            self._store_geometry()
        except Exception:
            log.exception("shutdown: 保存状态失败")
        log.info("shutdown: 状态已保存")
        # 3) 释放 mpv 渲染上下文（必须在 GL 上下文仍可用时做）
        try:
            self.prober.stop(1.0)
        except Exception:
            log.exception("shutdown: 停止时长探针失败")
        try:
            self.stage.video.destroy_render_context()
        except Exception:
            log.exception("shutdown: 释放渲染上下文失败")
        log.info("shutdown: 渲染上下文已释放")
        # 4) 让 mpv 停下并 join 事件线程
        try:
            self.engine.set("vid", "no", MPV_FORMAT_STRING)
            self.engine.cmd("stop")
            self.engine.begin_shutdown()
            self.engine.stop(1.5)
        except Exception:
            log.exception("shutdown: 停止引擎失败")
        log.info("shutdown: 引擎线程已回收")
        ev.accept()

    def finalize(self):
        """窗口已关闭、Qt 拆控件之前调用：**先释放 GL 渲染上下文，再销毁 mpv 实例**。

        ⚠️⚠️ 顺序不能反：`mpv_render_context_free` 必须在 mpv 实例仍然存活、
           且 GL 上下文 current 时调用。原实现只在这里 `engine.destroy()`，
           渲染上下文的释放依赖 `closeEvent` 是否走到 —— 异常退出时就会
           **留下悬挂的渲染上下文**（审计报告 T2）。
           现在两条路径都走 `destroy_render_context()`，且它自带幂等保护。
        """
        try:
            if self.stage.video.destroy_render_context():
                log.info("shutdown: 渲染上下文已安全释放")
        except Exception:
            log.exception("shutdown: 释放渲染上下文失败")
        try:
            self.engine.destroy()
        except Exception:
            log.exception("shutdown: 销毁 mpv 实例失败")
        log.info("shutdown: mpv 实例已销毁")


# --------------------------------------------------------------------------------------
# 胶囊淡出
# --------------------------------------------------------------------------------------


class CapsuleFader:
    """控制条淡入淡出。

    用 QGraphicsOpacityEffect 驱动透明度, 但**只在需要淡出时才安装** ——
    常驻 effect 会让整个胶囊走一遍离屏合成, 属于不必要的开销。
    """

    def __init__(self, widget, duration: int = 260):
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        from PySide6.QtWidgets import QGraphicsOpacityEffect

        self.w = widget
        self._eff = None
        self._anim = None
        self._duration = duration
        self._EasingCurve = QEasingCurve
        self._Anim = QPropertyAnimation
        self._Effect = QGraphicsOpacityEffect
        self._visible = True

    def _ensure(self):
        if self._eff is None:
            self._eff = self._Effect(self.w)
            self._eff.setOpacity(1.0)
            self.w.setGraphicsEffect(self._eff)
            self._anim = self._Anim(self._eff, b"opacity", self.w)
            self._anim.setDuration(self._duration)
            self._anim.setEasingCurve(self._EasingCurve.OutCubic)

    def fade_out(self):
        if not self._visible:
            return
        self._visible = False
        self._ensure()
        self._anim.stop()
        self._anim.setStartValue(self._eff.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def fade_in(self):
        if self._visible:
            return
        self._visible = True
        self._ensure()
        self._anim.stop()
        self._anim.setStartValue(self._eff.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()

    def set_visible_now(self, on: bool):
        self._visible = bool(on)
        if self._eff is not None:
            self._eff.setOpacity(1.0 if on else 0.0)

    # -- 给"打开设置"用的两个语义化入口 -------------------------------------
    def hide_now(self):
        """立刻淡出（不走动画）—— 设置面板打开时要把整块舞台让出来。"""
        self.fade_out()

    def wake(self):
        """重新显示并重置空闲计时。"""
        self.fade_in()


# --------------------------------------------------------------------------------------
# 按键 -> 规格串
# --------------------------------------------------------------------------------------

_MOD_MAP = [
    (Qt.ControlModifier, "Ctrl"),
    (Qt.AltModifier, "Alt"),
    (Qt.ShiftModifier, "Shift"),
    (Qt.MetaModifier, "Meta"),
]

_SPECIAL = {
    Qt.Key_Space: "Space",
    Qt.Key_Return: "Return",
    Qt.Key_Enter: "Return",
    Qt.Key_Escape: "Esc",
    Qt.Key_Tab: "Tab",
    Qt.Key_Backspace: "Backspace",
    Qt.Key_Delete: "Delete",
    Qt.Key_Insert: "Insert",
    Qt.Key_Home: "Home",
    Qt.Key_End: "End",
    Qt.Key_PageUp: "PageUp",
    Qt.Key_PageDown: "PageDown",
    Qt.Key_Left: "Left",
    Qt.Key_Right: "Right",
    Qt.Key_Up: "Up",
    Qt.Key_Down: "Down",
    Qt.Key_Plus: "+",
    Qt.Key_Equal: "=",
    Qt.Key_Minus: "-",
    Qt.Key_Comma: ",",
    Qt.Key_Period: ".",
    Qt.Key_Slash: "/",
    Qt.Key_BracketLeft: "[",
    Qt.Key_BracketRight: "]",
    Qt.Key_Backslash: "\\",
    Qt.Key_Semicolon: ";",
    Qt.Key_Apostrophe: "'",
    Qt.Key_QuoteLeft: "`",
}


def _key_to_spec(ev) -> str:
    """把 QKeyEvent 转成可写进配置的规格串（如 `Ctrl+Shift+O`）。"""
    key = ev.key()
    mods = ev.modifiers()
    parts = []
    for flag, name in _MOD_MAP:
        if mods & flag:
            parts.append(name)
    if key in _SPECIAL:
        base = _SPECIAL[key]
    elif Qt.Key_F1 <= key <= Qt.Key_F35:
        base = f"F{key - Qt.Key_F1 + 1}"
    elif Qt.Key_0 <= key <= Qt.Key_9:
        base = chr(ord("0") + (key - Qt.Key_0))
    elif Qt.Key_A <= key <= Qt.Key_Z:
        base = chr(ord("A") + (key - Qt.Key_A))
    else:
        txt = ev.text()
        if not txt or not txt.strip():
            return ""
        base = txt.upper()
    if base in ("Ctrl", "Alt", "Shift", "Meta"):
        return ""
    parts.append(base)
    return "+".join(parts)
