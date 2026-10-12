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

from PySide6.QtCore import QEvent, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QPalette
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
from .widgets import (GlassFrame, IconButton, TextIconButton, fmt_time,
                      prep_popup, RoundedMenu
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


class MainWindow(QWidget):
    """主窗口。"""

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
        self._speed = 1.0
        self._boost = False
        self._pre_boost_speed = 1.0
        self._fullscreen = False
        self._always_on_top = False
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
        self.drawer.request_sort.connect(lambda _k: None)
        self.drawer.open_state_changed.connect(self._on_drawer_state)
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

        # ---- 画布（图片工具条 / 音频卡片 / 空状态）连线 ----
        ib = self.stage.image_bar
        ib.prev_image.connect(lambda: self._step_image(-1))
        ib.next_image.connect(lambda: self._step_image(1))
        # ⚠️ 这两个**原来接反了**：`zoom_in` 接的是除法（=缩小）、`zoom_out` 接的是乘法（=放大）。
        #    用户一眼看出"放大缩小写反了"——图标没错，是接线错了。
        ib.zoom_in.connect(lambda: self._zoom_from_bar(
            float(self.cfg.get("ui.image_zoom_step", 1.12))))
        ib.zoom_out.connect(lambda: self._zoom_from_bar(
            1 / float(self.cfg.get("ui.image_zoom_step", 1.12))))
        ib.zoom_changed.connect(self._on_zoom_slider)
        ib.zoom_fit.connect(lambda: self._image_fit_or_one("fit"))
        ib.zoom_one.connect(lambda: self._image_fit_or_one("one"))
        ib.rotate_left.connect(lambda: self._rotate_image(-90))
        ib.rotate_right.connect(lambda: self._rotate_image(90))
        ib.flip_h.connect(self._flip_image)
        ib.reveal.connect(lambda: win.reveal_in_explorer(self._path))
        # ⑥ 左右热区：区内按下即切换上/下一张
        ib.playlist.connect(lambda: self._on_playlist_button_from_ui(not self.drawer.is_open()))
        ib.fullscreen.connect(self.toggle_fullscreen)       # ⑦ 控制栏全屏按钮
        # ① 导航取景框：拖框 ⇒ 把画面移到那个位置
        self.stage.navigator.pan_to.connect(self._nav_pan_to)
        # ② 播放列表视图切换：注入缩略图提供者 + 记住用户选择
        # ①⑥ 抽屉每次开合都重抬层级（手柄/抽屉/取景框必须在热区之上）
        self.drawer.open_state_changed.connect(lambda _on: self._raise_top_layers())
        self.drawer.set_thumb_provider(self._playlist_thumb)
        self.drawer.set_view_mode(str(self.cfg.get("ui.drawer_view", "list")), persist=False)
        self.drawer.view_changed.connect(self._on_drawer_view_changed)
        # ⑫ 左键拖动平移图片：画面控件上按下/移动/松开
        self.stage.video.installEventFilter(self)
        self._pan_drag = None
        self.stage.zone_left.activated.connect(lambda: self._step_image(-1))
        self.stage.zone_right.activated.connect(lambda: self._step_image(1))

        ac = self.stage.audio_card
        ac.play_pause.connect(self.toggle_pause)
        ac.prev.connect(lambda: self._step_file(-1))
        ac.next.connect(lambda: self._step_file(1))
        ac.reveal.connect(lambda: win.reveal_in_explorer(self._path))

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
            if self._kind != router.KIND_IMAGE:
                self.capsule.set_times(self._position, self._duration)
        elif name == "duration":
            self._duration = float(value or 0.0)
            self.capsule.set_times(self._position, self._duration)
            if self._path:
                self.drawer.update_duration(self._path, self._duration)
        elif name == "pause":
            self._paused = bool(value)
            self.capsule.set_paused(self._paused)
            self.stage.audio_card.set_info(
                self.engine.cached("media-title", "") or os.path.basename(self._path),
                self._tag("artist"), self._tag("album"), self._paused)
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
        self._path = path
        self._kind = kind
        self.titlebar.set_title(os.path.basename(path))
        self.cfg.push_recent(path)
        self.cfg.save_if_dirty()

        # 同目录播放列表（图片按图片列, 音频按音频列, 视频按视频列）
        if not self.drawer.paths() or remember_dir:
            items, idx = scanner.siblings(path, kind)
            if items:
                self.drawer.set_items(items, path)
                self.prober.request([i.path for i in items if not i.duration])

        self._apply_kind_ui(kind)
        self._prepare_engine_for(kind)
        # 音频模式在**加载的那一刻**就把视频输出关掉（vid 会被 mpv 按文件重置，
        # 先 set 再 load 是无效的）。视频/图片不带选项 —— 见 _prepare_engine_for 的说明。
        load_opts = "vid=no,aid=auto" if kind == router.KIND_AUDIO else None
        self.engine.load(path, options=load_opts)
        if autoplay:
            self.engine.set("pause", False, MPV_FORMAT_FLAG)
        self._update_empty_state()
        self._maybe_resume_position(path)

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
            # 分流器：图片**不再交给 mpv 解码**（用户架构要求：告别 mpv 枷锁）。
            # 直接让 mpv 停下，由 Qt 原生视口接管显示。
            try:
                self.engine.command("stop")
            except Exception:
                try:
                    self.engine.set("pause", True, MPV_FORMAT_FLAG)
                except Exception:
                    pass
            try:
                self._load_into_image_view()
            except Exception:
                log.debug("加载图片视口失败", exc_info=True)
            self.engine.set("loop-file", "inf", MPV_FORMAT_STRING)
            # ⑧ 用户："切换图片时图片若处于放大缩小状态，则会回复原状再切换图片，体验割裂"
            #    ⇒ **图片↔图片**切换时保留当前缩放与平移（只有从视频/音频进来才套默认适应方式）。
            keep_zoom, keep_pan = None, (0.0, 0.0)
            # ⚠️ 条件**不能**依赖 `_kind`/`_path`：切图过程中它们可能已被清空，
            #    结果 keep_zoom 永远是 None ⇒ 缩放照样被重置（实测：切图前 2.0 → 切图后 0.0）。
            #    只看引擎**当前**的缩放/平移即可 —— 有值就说明用户正处于放大状态。
            if True:
                try:
                    z_now = float(self.engine.get("video-zoom") or 0.0)
                    px_now = float(self.engine.get("video-pan-x") or 0.0)
                    py_now = float(self.engine.get("video-pan-y") or 0.0)
                    if abs(z_now) > 0.001 or abs(px_now) > 0.001 or abs(py_now) > 0.001:
                        keep_zoom, keep_pan = z_now, (px_now, py_now)
                except (TypeError, ValueError):
                    keep_zoom = None
            zoom = 0.0
            fit = self.cfg.get("ui.image_fit", "fit")
            if fit == "one":
                zoom = self._zoom_for_one()
            if keep_zoom is not None:
                zoom = keep_zoom
            self._image_zoom = 2 ** zoom
            self.engine.set("video-zoom", zoom, MPV_FORMAT_DOUBLE)
            self.engine.set("video-pan-x", keep_pan[0], MPV_FORMAT_DOUBLE)
            self.engine.set("video-pan-y", keep_pan[1], MPV_FORMAT_DOUBLE)
            self.engine.set("video-rotate", self._image_rotation, MPV_FORMAT_INT64)
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
        except Exception:
            log.debug("即时应用设置 %s 失败", key, exc_info=True)

    def _apply_kind_ui(self, kind: str):
        self.capsule.set_media_kind(kind)
        # 悬停缩略图预览：告诉控制栏"现在放的是哪个文件"。
        # 抽帧走 **ffmpeg 子进程**（见 mpv/frame_grab.py 顶部说明），
        # 进程级隔离，不会像第二个 libmpv 实例那样和主引擎争死。
        self.capsule.set_preview_enabled(bool(self.cfg.get("ui.preview_thumb", True)))
        self.capsule.set_preview_source("" if kind == "image" else str(self._path or ""))
        # 没有缓存缩略图时，用"当前播放画面"做模糊占位（严禁留死灰空框）
        self.capsule._placeholder_fn = self._current_frame_image
        self.stage._kind = kind
        self.stage.set_edge_zone_width(int(self.cfg.get("image.edge_zone_px", 96) or 0))
        self.stage.set_media_mode(kind)
        # ①⑥ 热区 raise 之后把抽屉与手柄抬回最上层（否则手柄点不到、热区抢走取景框操作）
        self._raise_top_layers()
        # ⚠️ 自动隐藏间隔必须**按当前类型重设**：它原来只在启动时设过一次，
        #    切到图片模式后仍用视频那套延时 ⇒ 沉浸模式永远等不到（实测踩过）。
        try:
            self._hide_timer.setInterval(self._autohide_ms())
            if self._hide_timer.isActive():
                self._hide_timer.start()
        except Exception:
            log.debug("重设自动隐藏间隔失败", exc_info=True)
        self.stage.audio_card.setVisible(kind == router.KIND_AUDIO)
        if kind == router.KIND_AUDIO:
            self._load_audio_tags()
        if kind == router.KIND_IMAGE:
            self._update_image_info()
        self.drawer.set_current(self._path)

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
        if self._kind != router.KIND_IMAGE:
            return
        zoom = self.engine.cached("video-zoom", 0.0) or 0.0
        import math

        dz = math.log2(max(0.01, factor))
        # ⑧ 用户："不是弹性动画，而是惯性动画 —— 这样放大缩小很丝滑，不用一直滚"。
        #    所以相对缩放给的是**速度脉冲**（冲量），由惯性循环按指数衰减推进；
        #    连续滚动会把脉冲叠加起来 ⇒ 滚一下能滑行一段。
        self._zoom_impulse(dz)

    def _zoom_impulse(self, dz: float):
        """给缩放一个**冲量**（惯性动画的入口）。dz 单位是 video-zoom（log2）。"""
        if self._kind != router.KIND_IMAGE:
            return
        if getattr(self, "_zoom_v", None) is None:
            self._zoom_v = 0.0
        self._zoom_v += float(dz) * 9.0          # 冲量 → 初速度（zoom 单位/秒）
        self._zoom_v = max(-14.0, min(14.0, self._zoom_v))
        if getattr(self, "_zoom_timer", None) is None:
            self._zoom_timer = QTimer(self)
            self._zoom_timer.setInterval(16)
            self._zoom_timer.timeout.connect(self._zoom_inertia_tick)
        self._zoom_anim = None                   # 惯性接管时取消绝对动画
        if not self._zoom_timer.isActive():
            self._zoom_timer.start()

    def _zoom_inertia_tick(self):
        """惯性滑行：每帧按当前速度推进，然后**指数衰减**（摩擦）。

        与弹性(OutBack)的区别：弹性是"冲过目标再回弹"，这里是"松手后继续滑行并减速"。
        """
        v = float(getattr(self, "_zoom_v", 0.0) or 0.0)
        if abs(v) < 0.015:
            self._zoom_v = 0.0
            self._zoom_timer.stop()
            return
        try:
            cur = float(self.engine.cached("video-zoom", 0.0) or 0.0)
        except (TypeError, ValueError):
            cur = 0.0
        z = max(-6.0, min(8.0, cur + v * 0.016))
        self.engine.set("video-zoom", z, MPV_FORMAT_DOUBLE)
        self._image_zoom = 2 ** z
        self._zoom_v = v * 0.87                  # 摩擦系数（每帧衰减 13%）
        self._apply_cursor_pan()                 # ③ 平移量跟着总缩放走
        if z <= -6.0 or z >= 8.0:
            self._zoom_v = 0.0
        self._apply_pending_pan()
        self._update_image_info()
        self._update_navigator()

    def _animate_zoom_to(self, target: float, ms: int = 240):
        """把 video-zoom **弹性**推过去（Windows 照片那种"过冲一下再回稳"的手感）。

        ⚠️ 为什么不直接 set：直接设是"瞬移"，用户要的是"很丝滑"；
           `OutBack` 缓动会略微过冲再回落 —— 这就是"弹性"的来源。
        """
        if self._kind != router.KIND_IMAGE:
            return
        start = float(self.engine.cached("video-zoom", 0.0) or 0.0)
        target = max(-6.0, min(8.0, float(target)))
        if abs(target - start) < 1e-3:
            return
        self._zoom_anim = {"i": 0, "n": max(6, int(ms / 16)), "a": start, "b": target}
        if getattr(self, "_zoom_timer", None) is None:
            self._zoom_timer = QTimer(self)
            self._zoom_timer.setInterval(16)
            self._zoom_timer.timeout.connect(self._zoom_anim_tick)
        self._zoom_timer.start()

    def _apply_cursor_pan(self):
        """按**当前总缩放**重算"朝鼠标位置"的平移量，并写进 mpv。

        推导：缩放以画面中心为基准，光标处（归一化偏移 p，±1 = 半宽）会被推到 p·k；
        要把它拉回来，需要平移 p·(1-k)/2（比例单位，mpv 的 pan 就是画面宽/高的比例）。
        每帧调用 ⇒ 平移量随惯性滑行一起增长，最终正好抵消总缩放。
        """
        cur = getattr(self, "_zoom_cursor", None)
        if not cur:
            return
        try:
            z_now = float(self.engine.cached("video-zoom", 0.0) or 0.0)
        except (TypeError, ValueError):
            z_now = 0.0
        k = 2.0 ** (z_now - float(getattr(self, "_zoom_pan_zoom0", 0.0) or 0.0))
        px, py = cur
        bx, by = getattr(self, "_zoom_pan_base", (0.0, 0.0))
        self._zoom_pan_target = (max(-2.0, min(2.0, bx + px * (1.0 - k) / 2.0)),
                                 max(-2.0, min(2.0, by + py * (1.0 - k) / 2.0)))
        self._apply_pending_pan()
        self._last_pan = self._zoom_pan_target

    def _apply_pending_pan(self):
        """把"朝鼠标缩放"算出的平移量写进 mpv（**必须在写完 zoom 之后**）。"""
        tgt = getattr(self, "_zoom_pan_target", None)
        if not tgt:
            return
        try:
            self.engine.set("video-pan-x", tgt[0], MPV_FORMAT_DOUBLE)
            self.engine.set("video-pan-y", tgt[1], MPV_FORMAT_DOUBLE)
        except Exception:
            log.debug("写 pan 失败", exc_info=True)

    @staticmethod
    def _ease_out_back(t: float) -> float:
        """绝对缩放的缓动：**缓出、不过冲**。

        ⚠️ 原来用的是 OutBack（冲过目标再回弹）—— 用户明确否掉：
           "不是弹性动画，而是惯性动画…不要弹性动画了，要惯性"。
           滑条这种"给定目标值"的场景用纯缓出即可（相对缩放走 `_zoom_impulse` 惯性）。
        """
        return 1.0 - (1.0 - t) ** 3

    def _zoom_anim_tick(self):
        a = getattr(self, "_zoom_anim", None)
        if not a:
            self._zoom_timer.stop()
            return
        a["i"] += 1
        t = min(1.0, a["i"] / float(a["n"]))
        z = a["a"] + (a["b"] - a["a"]) * self._ease_out_back(t)
        self.engine.set("video-zoom", z, MPV_FORMAT_DOUBLE)
        self._image_zoom = 2 ** z
        self._apply_cursor_pan()           # ③ 先按当前总缩放重算平移量
        self._apply_pending_pan()          # ← 顺序很重要：zoom 在前、pan 在后
        if t >= 1.0:
            self.engine.set("video-zoom", a["b"], MPV_FORMAT_DOUBLE)
            self._image_zoom = 2 ** a["b"]
            self._apply_pending_pan()
            self._update_navigator()
            self._zoom_pan_target = None
            self._zoom_anim = None
            self._zoom_timer.stop()
        self._update_image_info()

    def _load_into_image_view(self):
        """把 `self._path` 读进 Qt 原生视口。

        生命周期（用户要求）：切图**复位**为适应窗口；若用户把
        `image.keep_view_on_switch` 打开，则保留缩放比例（平移仍复位）。
        """
        v = getattr(self.stage, "image_view", None)
        if v is None or not self._path:
            return
        keep = bool(self.cfg.get("image.keep_view_on_switch", False))
        prev = v.zoom() if (keep and v.has_image()) else 1.0
        ok = v.load(self._path, reset=True)
        if ok:
            v.set_rotation(int(getattr(self, "_image_rotation", 0) or 0))
            v.set_flip(bool(getattr(self, "_image_flip", False)))
            if keep and abs(prev - 1.0) > 1e-3:
                v.set_zoom(prev)
        self._image_natural = ((v._img.width(), v._img.height()) if v.has_image() else (0, 0))
        self._update_image_info()
        self._update_navigator()

    def _image_viewport(self):
        """图片拖拽的**唯一**合法事件源：图片视口控件自身。

        ⚠️ 用户红线（P0）：拖拽视角的监听【严禁挂在全局或顶层覆盖层】，
           只能在视口控件内部触发 —— 否则底栏、热区、按钮的按下会被父层吃掉。
           现在返回视频控件；Qt 原生图片视口接入后由 `self._image_view` 顶替。
        """
        return getattr(self, "_image_view", None) or self.stage.video

    def _in_image_area(self, obj, ev) -> bool:
        """⑥ 判断这次按下是不是落在「图片显示区域」内（用于左键拖动平移）。

        ⚠️ 三条实测教训：
          1. 原来要求 `obj is self.stage.video` —— 按不到：视频控件上压着覆盖层，
             事件目标不是它 ⇒ 用户「长按左键不能移动视角」；
          2. 屏幕坐标（`mapToGlobal`/`mapFromGlobal`）在这台机器不可靠 ⇒ 一律用**控件内坐标**；
          3. **判定失败必须放行**（fail-open）：旧版异常时返回 False ⇒ 用户永远拖不动。
             宁可多让一次拖动，也不要让功能静默失效。
        """
        try:
            v = self.stage.video
            vr = QRect(v.mapTo(self, QPoint(0, 0)), v.size())
            try:
                lp = obj.mapTo(self, ev.position().toPoint())
            except Exception:
                return True
            # ⚠️ 只看**几何**，而且不做抽屉自己的坐标换算（它有宽度动画，实测换算会漏判）：
            #    抽屉永远贴在窗口右侧，占最右边 `drawer.width()` 那一条。
            if self.drawer.width() > 8 and lp.x() >= self.width() - self.drawer.width() - 2:
                return False
            return vr.contains(lp)
        except Exception:
            return True

    def _pan_to(self, pos):
        """把画面平移到"按住的那一点跟着鼠标走"（左键拖动）。

        `video-pan-x/y` 的单位是画面宽/高的比例 ⇒ 像素位移要除以画面尺寸。
        拖动的方向与画面移动方向一致（拖右 → 画面右移）。
        """
        d = self._pan_drag
        if not d:
            return
        try:
            v = self.stage.video
            dx = (float(pos.x()) - d[0]) / max(1.0, v.width())
            dy = (float(pos.y()) - d[1]) / max(1.0, v.height())
        except Exception:
            return
        d[0], d[1] = float(pos.x()), float(pos.y())
        self._update_navigator()
        try:
            px = float(self.engine.get("video-pan-x") or 0.0) + dx
            py = float(self.engine.get("video-pan-y") or 0.0) + dy
        except (TypeError, ValueError):
            px = py = 0.0
        self.engine.set("video-pan-x", max(-2.0, min(2.0, px)), MPV_FORMAT_DOUBLE)
        self.engine.set("video-pan-y", max(-2.0, min(2.0, py)), MPV_FORMAT_DOUBLE)
        self._last_pan = (max(-2.0, min(2.0, px)), max(-2.0, min(2.0, py)))

    def _cursor_in_video(self):
        """把当前光标位置换算到**画面控件坐标**；不在画面内则返回 None。"""
        try:
            v = self.stage.video
            gp = QCursor.pos()
            lp = v.mapFromGlobal(gp)
            if 0 <= lp.x() < v.width() and 0 <= lp.y() < v.height():
                return lp
        except Exception:
            log.debug("取光标位置失败", exc_info=True)
        return None

    def _zoom_from_bar(self, factor: float):
        """控制栏缩放入口：图片走 Qt 视口（本地矩阵），音视频才用 mpv。"""
        if self._kind == router.KIND_IMAGE:
            v = getattr(self.stage, "image_view", None)
            if v is not None and v.has_image():
                v.zoom_at_cursor(float(factor))
                self._update_image_info()
                self._update_navigator()
                return
            return
        self._zoom_from_bar_mpv(factor)

    def _zoom_from_bar_mpv(self, factor: float):
        """控制栏上的缩放动作（按钮 / 滑条）。

        ⚠️ 用户实测"根据鼠标位置缩放没实现"——因为原来**只有滚轮**走 `_zoom_image_at`，
           按钮与滑条一律以画面中心缩放。这里统一：开着 `image.zoom_at_cursor` 时，
           光标在画面里就朝光标缩放，否则回落到中心。
        """
        if self._kind != router.KIND_IMAGE:
            return
        pos = self._cursor_in_video() if bool(self.cfg.get("image.zoom_at_cursor", False)) else None
        if pos is not None:
            self._zoom_image_at(factor, pos)
        else:
            self._zoom_image(factor)

    def _zoom_image_at(self, factor: float, pos):
        """朝**鼠标位置**缩放：缩放的同时平移画面，让光标下那个点尽量不动。

        mpv 的 `video-pan-x/y` 单位是"视频矩形宽/高的比例"（0 = 居中）。
        缩放 k 倍是以画面中心为基准的，所以光标处（归一化偏移 p ∈ [-1,1]）
        会被推到 p·k；要把它拉回来，需要平移 p·(1-k)/2（比例单位）。
        """
        try:
            vw = max(1, self.stage.video.width())
            vh = max(1, self.stage.video.height())
            px = (float(pos.x()) - vw / 2.0) / (vw / 2.0)
            py = (float(pos.y()) - vh / 2.0) / (vh / 2.0)
        except Exception:
            px = py = 0.0
        k = float(factor)
        shift = (1.0 - k) / 2.0
        try:
            pan_x = float(self.engine.cached("video-pan-x", 0.0) or 0.0)
            pan_y = float(self.engine.cached("video-pan-y", 0.0) or 0.0)
        except (TypeError, ValueError):
            pan_x = pan_y = 0.0
        # ⚠️⚠️ 不能在这里直接写 pan：此刻 `video-zoom` 还是旧值（通常是 1.0），
        #    画面刚好铺满窗口 ⇒ **没有平移空间** ⇒ mpv 会把 pan 夹成 0（实测踩过）。
        #    必须等 zoom 改完再写 —— 交给缩放动画的每一拍（见 `_apply_pending_pan`）。
        # ⚠️ 原来这里只按**单步** factor 算平移量 ⇒ 惯性滑行把缩放放大了好几倍，
        #    平移却还停在单步的量上（用户实测"朝鼠标缩放没感觉"）。
        #    现在记住光标位置与起始状态，交给 `_apply_cursor_pan()` **每帧按当前总缩放重算**。
        try:
            self._zoom_pan_zoom0 = float(self.engine.cached("video-zoom", 0.0) or 0.0)
        except (TypeError, ValueError):
            self._zoom_pan_zoom0 = 0.0
        self._zoom_cursor = (px, py)
        self._zoom_pan_base = (pan_x, pan_y)
        self._apply_cursor_pan()
        self._zoom_image(k)
        # 缩放没变化时不会启动动画 ⇒ 这里补一次，免得 pan 永远没机会写
        if getattr(self, "_zoom_anim", None) is None:
            self._apply_pending_pan()

    def _on_zoom_slider(self, target_zoom: float):
        """滑条拖动：设置**绝对**缩放值（图片走 Qt 视口）。"""
        if self._kind != router.KIND_IMAGE:
            return
        v = getattr(self.stage, "image_view", None)
        if v is not None and v.has_image():
            v.set_zoom(2.0 ** float(target_zoom))
            self._update_image_info()
            self._update_navigator()
            return
        pos = self._cursor_in_video() if bool(self.cfg.get("image.zoom_at_cursor", False)) else None
        cur = float(self.engine.cached("video-zoom", 0.0) or 0.0)
        factor = 2.0 ** (float(target_zoom) - cur)
        if pos is not None:
            self._zoom_image_at(factor, pos)
        else:
            self._animate_zoom_to(target_zoom)

    def _image_fit_or_one(self, mode: str):
        """「适应窗口」/「原始大小」：图片走 Qt 视口（原始大小 = 1 图片像素对 1 屏幕像素）。"""
        v = getattr(self.stage, "image_view", None)
        if self._kind == router.KIND_IMAGE and v is not None and v.has_image():
            if mode == "one":
                fw, fh = v.fit_size()
                iw = max(1, v._img.width())
                v.set_zoom((iw / fw) if fw > 0 else 1.0)
            else:
                v.reset_view()
            self.cfg.set("ui.image_fit", mode)
            self._update_image_info()
            self._update_navigator()
            return
        self._set_image_fit(mode)

    def _set_image_fit(self, mode: str):
        if mode == "one":
            z = self._zoom_for_one()
        else:
            z = 0.0
        self.engine.set("video-zoom", z, MPV_FORMAT_DOUBLE)
        self.engine.set("video-pan-x", 0.0, MPV_FORMAT_DOUBLE)
        self.engine.set("video-pan-y", 0.0, MPV_FORMAT_DOUBLE)
        self._last_pan = (0.0, 0.0)
        self._image_zoom = 2 ** z
        self.cfg.set("ui.image_fit", mode)
        self._update_image_info()
        self._update_navigator()

    def _rotate_image(self, deg: int):
        if self._kind != router.KIND_IMAGE:
            return
        v = getattr(self.stage, "image_view", None)
        if v is not None and v.has_image():
            v.set_rotation((v.rotation() + int(deg)) % 360)
            self._image_rotation = v.rotation()
            self._update_image_info()
            self._update_navigator()
            self.show_osd("旋转 %d°" % self._image_rotation, "rotate_right")
            return
        self._image_rotation = (self._image_rotation + deg) % 360
        self.engine.set("video-rotate", self._image_rotation, MPV_FORMAT_INT64)
        self.show_osd(f"旋转 {self._image_rotation}°", "rotate_right")

    def _flip_image(self):
        if self._kind != router.KIND_IMAGE:
            return
        v = getattr(self.stage, "image_view", None)
        if v is not None and v.has_image():
            v.set_flip(not v.flip())
            self._image_flip = v.flip()
            self.stage.image_bar.set_flip_state(self._image_flip)
            self._update_image_info()
            self._update_navigator()
            self.show_osd("水平镜像 开" if self._image_flip else "水平镜像 关", "flip_h")
            return
        self._image_flip = not self._image_flip
        # ⚠️ 原来用 mpv 的 `video-flip` —— 实测对**静态图**不生效（用户："水平镜像功能不起作用"）。
        #    改成视频滤镜 `vf=hflip`：它是解码链上的真实翻转，静图/视频都有效。
        self.engine.set("vf", "hflip" if self._image_flip else "", MPV_FORMAT_STRING)
        # ④ 用户："水平镜像的按钮和悬浮胶囊提示的水平镜像提示图标不一致，以控制栏的为主"
        #    ⇒ OSD 也用控制栏那个 `flip_h`（原来用的是 `wall` 四个方块）。
        self.show_osd("水平镜像 开" if self._image_flip else "水平镜像 关", "flip_h")

    def _step_image(self, delta: int):
        items = self.drawer.paths()
        if not items:
            return
        try:
            i = [os.path.normcase(p) for p in items].index(os.path.normcase(self._path))
        except ValueError:
            i = 0
        j = (i + delta) % len(items)
        # ⑧ 用户："切换图片时图片若处于放大缩小状态，则会回复原状再切换图片，体验割裂"。
        #    ⚠️ 根因：mpv 的 `video-zoom`/`video-pan-*` 是**按文件生效**的属性，
        #    加载新文件时会自己重置 ⇒ 光在 open 路径里"保留"没用（实测切图后仍是 0.0），
        #    必须**加载完成后补写**。
        keep = None
        if self._kind == router.KIND_IMAGE:
            try:
                z = float(self.engine.get("video-zoom") or 0.0)
                px = float(self.engine.get("video-pan-x") or 0.0)
                py = float(self.engine.get("video-pan-y") or 0.0)
                if abs(z) > 0.001 or abs(px) > 0.001 or abs(py) > 0.001:
                    keep = (z, px, py)
            except (TypeError, ValueError):
                keep = None
        self.open_path(items[j], remember_dir=False)
        if keep is not None:
            self._pending_image_view = keep
            self._reapply_done = False
            self._reapply_image_view()      # 先补一次；file-loaded 时再补一次（见 _on_file_loaded）

    def _reapply_image_view(self, tries: int = 0):
        """把切换前的缩放/平移补写到新图上（mpv 换文件会重置这些属性）。

        文件加载是异步的，所以延迟一点再写；写失败就重试几次。
        """
        keep = getattr(self, "_pending_image_view", None)
        if not keep:
            return
        if self._kind != router.KIND_IMAGE or not self._path:
            return
        try:
            # ⚠️ file-loaded 会触发多次（实测补写被调用 6 次）。目标已经对上就不用再写，
            #    这样既不闪也不浪费（用户："切换图片不丝滑、反应慢"）。
            if abs(float(self.engine.cached("video-zoom", 0.0) or 0.0) - keep[0]) < 1e-4 \
                    and self._reapply_done:
                self._pending_image_view = None
                return
        except (TypeError, ValueError):
            pass
        self._reapply_done = True
        try:
            self.engine.set("video-zoom", keep[0], MPV_FORMAT_DOUBLE)
            self.engine.set("video-pan-x", keep[1], MPV_FORMAT_DOUBLE)
            self.engine.set("video-pan-y", keep[2], MPV_FORMAT_DOUBLE)
            self._image_zoom = 2 ** keep[0]
        except Exception:
            log.debug("补写缩放失败", exc_info=True)
        # ⚠️ 原来重试 6 次 × 90ms = 540ms，期间画面停在 100% ⇒ 用户看到「先闪回 100%」
        #    并且「反应慢」。改成只补 2 次短重试，真正的补写交给 `file-loaded` 事件。
        if tries < 2:
            QTimer.singleShot(45, lambda: self._reapply_image_view(tries + 1))
        else:
            self._pending_image_view = None
            self._reapply_done = False
            self._update_image_info()

    def _on_drawer_view_changed(self, mode: str):
        """② 记住视图选择；切到缩略图时安排一次补刷（视频缩略图是异步抓帧）。"""
        self.cfg.set("ui.drawer_view", str(mode))
        if str(mode) == "thumb":
            self.drawer._schedule_thumb_refresh()

    def _playlist_thumb(self, path: str):
        """② 缩略图提供者：图片同步缩；视频用胶囊里的抓帧 worker（有缓存）；其它返回 None。"""
        ext = os.path.splitext(path or "")[1].lower()
        if ext in (".mp4", ".mkv", ".mov", ".webm", ".avi", ".ts", ".m4v", ".flv", ".wmv"):
            try:
                worker = getattr(self.capsule, "_worker", None)
                grabber = getattr(worker, "_grabber", None)
                if grabber is None:
                    return None
                got = grabber.nearest(path, 0.0, tol=2.0)
                if got:
                    return got[0]
                worker.request(path, 0.0)      # 先预热，稍后由一次性定时器补刷
            except Exception:
                log.debug("视频缩略图失败: %s", path, exc_info=True)
            return None
        if ext in (".mp3", ".flac", ".m4a", ".wav", ".aac", ".ogg", ".opus"):
            return None                        # 音乐用类型图标（不抽封面，保持轻量）
        try:
            from PySide6.QtGui import QImageReader

            rd = QImageReader(path)
            rd.setAutoTransform(True)
            sz = rd.size()
            if sz.isValid() and sz.width() > 0:
                rd.setScaledSize(sz.scaled(168, 152, Qt.KeepAspectRatio))
            img = rd.read()
            return img if (img is not None and not img.isNull()) else None
        except Exception:
            log.debug("图片缩略图失败: %s", path, exc_info=True)
            return None

    def _nav_pan_to(self, nx: float, ny: float):
        """取景框拖拽：图片走 Qt 视口的本地偏移（音视频才用 mpv 的 pan）。"""
        v = getattr(self.stage, "image_view", None)
        if self._kind == router.KIND_IMAGE and v is not None and v.has_image():
            r = v.image_rect()
            if not r.isEmpty():
                # 让图片上的 (nx, ny) 落在视口中心 ⇒ 反向偏移
                v._off = __import__("PySide6.QtCore", fromlist=["QPointF"]).QPointF(
                    -(nx - 0.5) * r.width(), -(ny - 0.5) * r.height())
                v._clamp_off()
                v.update()
                v.view_changed.emit()
                self._update_navigator()
            return
        """① 导航取景框拖框 ⇒ 把图片上的 (nx, ny) 移到窗口中心。

        推导：画面矩形居中 + `pan_x·dwidth` 平移 ⇒ 要让图片比例 nx 落在窗口中心，
        需要 `pan_x = 0.5 - nx`（与 dwidth 无关，很干净）。
        """
        if self._kind != router.KIND_IMAGE:
            return
        try:
            self.engine.set("video-pan-x", max(-2.0, min(2.0, 0.5 - float(nx))), MPV_FORMAT_DOUBLE)
            self.engine.set("video-pan-y", max(-2.0, min(2.0, 0.5 - float(ny))), MPV_FORMAT_DOUBLE)
            self._last_pan = (max(-2.0, min(2.0, 0.5 - float(nx))),
                              max(-2.0, min(2.0, 0.5 - float(ny))))
        except Exception:
            log.debug("导航平移失败", exc_info=True)
        self._update_navigator()

    def _update_navigator(self):
        """① 按**本地计算**的几何算出「当前视野在图片里的比例框」，交给导航控件。

        ⚠️ 性能红线（用户实测「滚轮缩放特别卡」「拖缩放条特别卡」「取景框严重掉帧」）：
           原来这里每帧都调 `engine.get("dwidth"/"dheight"/"video-pan-*")` ——
           每次都是**同步 IPC 到 mpv**，一帧三次 ⇒ 卡顿就是这么来的。
           现在全部本地算：
             * 显示尺寸 = 图片原始尺寸 × 适应比例 × 2^zoom（不再问 mpv）
             * pan 用 `cached()`（事件驱动，不阻塞）
           另外**节流到 60ms**：动画期间没必要每帧重算视野框。
        """
        import time as _time

        now = _time.perf_counter()
        if now - getattr(self, "_nav_last", 0.0) < 0.06:
            return
        self._nav_last = now
        if self._kind != router.KIND_IMAGE or not self._path:
            try:
                self.stage.navigator.setVisible(False)
            except Exception:
                pass
            return
        # 分流：图片的取景框完全用 **Qt 视口**的本地换算（0ms，无 IPC）
        v = getattr(self.stage, "image_view", None)
        if v is not None and v.has_image() and self._kind == router.KIND_IMAGE:
            try:
                nav = self.stage.navigator
                nav.set_source(self._path)
                nav.set_transform(v.rotation(), v.flip())
                nav.set_zoom_state(v.zoom() > 1.001)
                nav.set_view(v.view_rect())
                return
            except Exception:
                log.debug("取景框（Qt 视口）刷新失败", exc_info=True)
        try:
            nav = self.stage.navigator
            nav.set_source(self._path)
            nav.set_transform(int(getattr(self, "_image_rotation", 0) or 0),
                              bool(getattr(self, "_image_flip", False)))
            nav.set_zoom_state(abs(float(self.engine.get("video-zoom") or 0.0)) > 0.005)
            v = self.stage.video
            ww, wh = max(1, v.width()), max(1, v.height())
            # ⚠️ 必须用**实时 get()**：`cached()` 只跟踪注册过观察者的属性，
            #    dwidth/dheight 不在其中 ⇒ 永远读到 0 ⇒ 导航框永不出现（实测踩过）。
            # 本地算显示尺寸（含 zoom）—— 不问 mpv，这是卡顿的关键修复
            iw, ih = getattr(self, "_image_natural", (0, 0)) or (0, 0)
            if iw <= 1 or ih <= 1:
                iw = float(self.engine.cached("width", 0) or 0)
                ih = float(self.engine.cached("height", 0) or 0)
            if iw <= 1 or ih <= 1:
                nav.set_view(None)
                return
            z = float(self.engine.cached("video-zoom", 0.0) or 0.0)
            fit = min(ww / iw, wh / ih)              # 适应窗口时的缩放比
            k = fit * (2.0 ** z)
            dw, dh = iw * k, ih * k
            # ⚠️ 不用 `cached()` 读 pan：它是事件缓存，刚写完还会是旧值（实测视野框不动）。
            #    我们自己每次写 pan 都记在 `_last_pan` 里，这里直接用（零 IPC、永远新鲜）。
            px, py = getattr(self, "_last_pan", (0.0, 0.0))
            left = (ww - dw) / 2.0 + px * dw
            top = (wh - dh) / 2.0 + py * dh
            # 窗口与画面矩形的交集 → 换算成图片内比例
            x0, y0 = max(0.0, -left), max(0.0, -top)
            x1, y1 = min(dw, ww - left), min(dh, wh - top)
            if x1 <= x0 or y1 <= y0:
                nav.set_view(None)
                return
            nav.set_view((x0 / dw, y0 / dh, (x1 - x0) / dw, (y1 - y0) / dh))
        except Exception:
            log.debug("导航刷新失败", exc_info=True)

    def _update_image_info(self):
        if self._kind != router.KIND_IMAGE or not self._path:
            return
        try:
            size = os.path.getsize(self._path)
        except OSError:
            size = 0
        # 分流：图片的尺寸与缩放从 Qt 视口取（mpv 已经停了）
        v = getattr(self.stage, "image_view", None)
        if v is not None and v.has_image():
            w, h = v._img.width(), v._img.height()
            self._image_zoom = float(v.zoom())
        else:
            w = int(self.engine.cached("width", 0) or 0)
            h = int(self.engine.cached("height", 0) or 0)
        self._image_natural = (w, h)
        items = self.drawer.paths()
        try:
            idx = [os.path.normcase(p) for p in items].index(os.path.normcase(self._path)) + 1
        except ValueError:
            idx = 1
        self.stage.image_bar.set_info(w, h, size, self._image_zoom, idx, max(1, len(items)))
        self.stage.image_bar.set_flip_state(bool(getattr(self, "_image_flip", False)))
        # 文件名 + 分辨率放**标题栏**（用户："名字和像素可以放在窗口标题栏，不需要加到控制栏"）
        if w and h:
            self.titlebar.set_title(f"{os.path.basename(self._path)} · {w}×{h}")
        # ① 导航取景框跟着刷 —— 挂这里最可靠：缩放（含惯性每帧）、平移、
        #    适应/原始大小、切图最终都会走到 `_update_image_info()`。
        self._update_navigator()

    # =====================================================================
    # 音频
    # =====================================================================
    def _load_audio_tags(self):
        tags_ = tags.read_tags(self._path)
        title = tags_.title or self.engine.cached("media-title", "") or os.path.basename(self._path)
        self.stage.audio_card.set_info(title, tags_.artist, tags_.album, self._paused)
        self.stage.audio_card.set_cover(tags_.cover)

    # =====================================================================
    # 播放控制
    # =====================================================================
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

    def _on_seek_preview(self, seconds: float):
        """拖动过程中的实时预览：走**关键帧跳转**, 不回溯解码。

        节流到 ~12Hz：mpv 侧每秒上百次精确 seek 会把解码器打死, 画面反而更卡。
        """
        self.capsule.time_label.setText(f"{fmt_time(seconds)} / {fmt_time(self._duration)}")
        now = time.perf_counter()
        # ⑭ 用户："拖动进度条一卡一卡，不丝滑"。实测：拖动**跟手且不回跳**（不是 bug），
        #    卡顿感来自"按关键帧跳"的**跳格**（GOP 粒度）。这里把预览频率从 12Hz 提到
        #    20Hz（跳转本身仍是 `precise=False` 的关键帧跳，代价低），跳格间隔更密 ⇒ 更顺。
        if now - self._last_preview_seek < 0.05:
            return
        self._last_preview_seek = now
        # ⑭ 实测：样片关键帧间隔 **8.3 秒**（20 秒只有 3 个关键帧）⇒ 关键帧跳只能落在
        #    那 3 个点上，拖动时画面"一格一格跳"（用户："一卡一卡"）。
        #    修法：**自适应** —— 短片 + 低分辨率用精确跳（落点准确，代价可接受）；
        #    长片 / 高分辨率仍用关键帧跳（避免拖动时把解码器打死）。
        cheap = True
        try:
            dur = float(self._duration or 0.0)
            h = int(self.engine.cached("height", 0) or 0)
            cheap = not (0 < dur <= 900.0 and 0 < h <= 1440)
        except (TypeError, ValueError):
            cheap = True
        self.engine.seek_absolute(seconds, precise=not cheap)

    def _on_seek_committed(self, seconds: float):
        # 松手 → 精确对位（这一次才需要帧级准确）
        self.engine.seek_absolute(seconds, precise=True)
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

    def _raise_top_layers(self):
        """①⑥ 把"必须能点到"的控件抬到最上层：抽屉、抽屉手柄、导航取景框。

        ⚠️ 顺序很重要：热区在图片模式下会 `raise_()`，它盖住手柄 ⇒ 手柄点不到；
           还会盖住右下角取景框 ⇒ 用户想拖取景框却先触发"下一张"（实测反馈）。
           所以每次改变层级（切类型、开合抽屉）之后都要按这个顺序重抬一遍。
        """
        try:
            self.stage.drawer_handle.raise_()
            self.drawer.raise_()
            self.stage.navigator.raise_()
        except Exception:
            log.debug("抬升层级失败", exc_info=True)

    def _after_drawer_toggle(self):
        """①⑥ 抽屉开合后重抬层级 —— 抽屉、手柄、取景框都必须能点到。"""
        self._raise_top_layers()

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
        if not on:
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
            self.stage.settings_host.setVisible(True)
            self.settings.setVisible(True)
            self._fit_settings()
            self.stage.raise_settings()
            self.settings.setFocus()
            # 打开设置时把控制条淡出：面板要占满整个舞台高度才不会被读成"被截断"，
            # 而胶囊是浮在画面上的，两者抢同一块地方。设置期间本来也不需要播放控制。
            self._fader.hide_now()
        else:
            self.stage.settings_host.setVisible(False)
            if self.settings is not None:
                self.settings.setVisible(False)
            self.stage.set_landing_visible(True)
            self._fader.wake()

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

    def _settings_open(self) -> bool:
        return bool(self.settings is not None and self.settings.isVisible())

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
        # ⚠️⚠️ 挂载点必须**跟面板一样大**。它挂在跨四行的大格子里，如果让它保持
        #    格子大小，它就会覆盖整个舞台、把鼠标事件全吃掉 ——
        #    用户实测"点开设置以后我什么都点不了"就是这个。
        self.stage.settings_host.setFixedSize(self.settings.size())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.stage.settings_host.isVisible():
            self._fit_settings()
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
        # ⚠️ 菜单要**贴着触发它的那个按钮**弹出。原来不管从哪儿点都锚在左上角的 logo 上,
        #    于是点右上角的「…」菜单跑到窗口左边去（用户截图指出"为什么右上角的选项
        #    打开面板在左边"）。现在由 TitleBar 把触发控件传进来, 并做右边界收敛 ——
        #    贴着右边缘的按钮弹出时菜单要往左让，不能出窗。
        btn = anchor if anchor is not None else self.titlebar.btn_menu
        pos = btn.mapToGlobal(btn.rect().bottomLeft())
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        size = menu.sizeHint()
        x = min(pos.x(), scr.right() - size.width() - 8)
        y = pos.y() + 4
        if y + size.height() > scr.bottom() - 8:
            y = btn.mapToGlobal(btn.rect().topLeft()).y() - size.height() - 4
        menu.exec(QPoint(max(scr.left() + 8, x), max(scr.top() + 8, y)))

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

    def _collect_image_info(self):
        """图片模式的信息字段（**与视频无关的项一律不出现**）。"""
        import math

        from ..media import exif as _exif

        e = self.engine
        rows = []
        try:
            w = int(e.cached("width", 0) or 0)
            h = int(e.cached("height", 0) or 0)
        except (TypeError, ValueError):
            w = h = 0
        path = self._path or ""
        ext = os.path.splitext(path)[1].lstrip(".").upper()
        if w and h:
            mp = w * h / 1e6
            rows.append(("尺寸", f"{w}×{h}    {mp:.2f} MP"))
            g = math.gcd(w, h) or 1
            rows.append(("宽高比", f"{w // g}:{h // g}"))
        if ext:
            rows.append(("格式", ext))
        pix = ""
        try:
            pix = str(e.cached("video-params/pixelformat", "") or "")
        except Exception:
            pix = ""
        if pix:
            rows.append(("像素格式", pix))
        try:
            size = os.path.getsize(path) if path and os.path.isfile(path) else 0
        except OSError:
            size = 0
        if size:
            rows.append(("文件大小", f"{size / 1048576:.2f} MB"))
        # 显示状态（看图时最关心的两项）
        try:
            z = 2 ** float(e.cached("video-zoom", 0.0) or 0.0)
        except (TypeError, ValueError):
            z = 1.0
        mode = "适应窗口" if abs(z - 1.0) < 0.02 else "已缩放"
        # ⑦ 用户："信息的缩放不能及时更新，而且这个信息也不重要不要了" ⇒ 去掉"显示"行
        if getattr(self, "_image_rotation", 0):
            rows.append(("旋转", f"{self._image_rotation}°"))
        if getattr(self, "_image_flip", False):
            rows.append(("镜像", "水平翻转"))
        items = self.drawer.paths()
        if items and path:
            try:
                idx = [os.path.normcase(x) for x in items].index(os.path.normcase(path)) + 1
                rows.append(("序号", f"{idx} / {len(items)}"))
            except ValueError:
                pass
        # EXIF（有才显示；解析失败静默跳过，绝不让浮层崩）
        try:
            rows.extend(_exif.exif_lines(_exif.read_exif(path)))
        except Exception:
            log.debug("EXIF 解析失败", exc_info=True)
        if path:
            rows.append(("文件地址", path))       # ⑥ 用户要求：信息里加文件地址
        return os.path.basename(path), rows

    def _collect_media_info(self):
        """现读一遍 mpv 属性（**不缓存** —— 按 Tab 就是想看此刻的真实值）。

        ⚠️⚠️ 必须**按媒体类型**出字段：图片没有"帧率 / 视频编码 / 硬件解码 / 容器"
           这些概念。原来一套视频模板套所有类型，用户看 jpg 时看到的是
           「1 fps / mjpeg / 硬件解码 关（软解）/ 容器 jpeg_pipe」——
           原话："既然是图片模式了，信息描述就应该按照图片来，你的信息还是视频的模板"。
        """
        if self._kind == router.KIND_IMAGE:
            return self._collect_image_info()
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
        if self._path:
            rows.append(("文件地址", self._path))   # ⑥ 视频侧也加文件地址
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
        """全屏 —— **无边框全屏，绝不换窗口样式**。

        ⚠️⚠️ 原来用的是 `showFullScreen()`。实测它会把窗口样式从
        `0x160F0000`（含 `WS_THICKFRAME`）换成 `0x96080000`（`WS_POPUP`），
        **Windows 因此重建窗口**，GL 交换链跟着重建 —— 用户看到的就是
        "切换全屏会黑屏，过一下才显示画面，跟游戏里切独占全屏一样"。

        爱奇艺/PotPlayer 的"全屏"其实**只是把窗口放大到整个显示器**，样式一个字节都不动。
        这里照做：保存几何 → 贴满显示器 → 临时置顶（否则盖不住任务栏）→ 隐藏自绘标题栏。
        退出时还原几何并取消置顶。全程同一个 HWND、同一个 GL 上下文，**零黑屏**。
        """
        self._fullscreen = not self._fullscreen
        self.titlebar.setVisible(not self._fullscreen)
        if self._fullscreen:
            self.drawer.set_open(False)
            self._fs_saved_geo = self.geometry()
            self._fs_saved_max = self.isMaximized()
            scr = self.screen() or QApplication.primaryScreen()
            g = scr.geometry()                    # 整个显示器（含任务栏区域）
            if self._fs_saved_max:
                self.showNormal()                 # 先退出最大化，否则几何会被系统改写
                QApplication.processEvents()
            win.set_topmost(self, True)
            win.move_window(self, g.x(), g.y())
            self.setGeometry(g.x(), g.y(), g.width(), g.height())
            self.raise_()
            # ⚠️ 全屏必须**关掉 DWM 的原生圆角与窗口边框**：
            #    圆角是"按窗口矩形裁"的，画面铺满时四个角会被切掉；
            #    边框是 DWM 自己画的一圈高光线，贴到屏幕边缘就是一圈"白边"。
            #    窗口化时留着好看，全屏时留着就是 bug（用户实测反馈）。
            win.enable_rounded_corners(self, False)
            win.set_window_border(self, False)
            # ⚠️ 还有**我们自己画的第二层**：`#WindowBody` 的 1px 描边 + 12px 圆角。
            #    浅色主题下那圈描边就是屏幕边缘的白线，圆角则会把画面四角再裁一次。
            #    DWM 关掉只是解决了系统那层，Qt 这层必须一起关（用户第二轮反馈"白边依旧"）。
            self._set_body_fullscreen(True)
        else:
            win.set_topmost(self, False)
            win.enable_rounded_corners(self, True)
            win.set_window_border(self, True)
            self._set_body_fullscreen(False)
            geo = getattr(self, "_fs_saved_geo", None)
            if geo is not None and geo.isValid():
                win.move_window(self, geo.x(), geo.y())
                self.setGeometry(geo)
            if getattr(self, "_fs_saved_max", False):
                self.showMaximized()
            self.titlebar.setVisible(True)
        self.capsule.set_fullscreen(self._fullscreen)
        # ⚠️ 全屏**也要留着手柄**：它是在全屏下唯一能打开播放列表的边缘入口
        #    （进全屏时抽屉会被收起，见上面 `self.drawer.set_open(False)`）。
        #    原来这里写的是 `not self._fullscreen and not self.drawer.is_open()`，
        #    于是全屏下彻底没有入口 —— 用户反馈："全屏时播放列表手柄消失"。
        self.stage.drawer_handle.setVisible(True)
        QTimer.singleShot(0, self.stage.video.update)

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
        self._hide_timer.setInterval(self._autohide_ms())
        self._hide_timer.timeout.connect(self._maybe_hide_capsule)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._tick_status)
        self._status_timer.start()

        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(120)
        self._edge_timer.timeout.connect(self._check_edge_hover)
        self._edge_timer.start()

        # OSD 合帧节流：把一秒钟上百次的拖动事件并成 ~14 次真正重排
        self._osd_pending = None
        self._osd_timer = QTimer(self)
        self._osd_timer.setSingleShot(True)
        self._osd_timer.setInterval(70)
        self._osd_timer.timeout.connect(self._flush_osd)

        self._fader = CapsuleFader(self.capsule)
        self._edge_suppress = False
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
        if self._fullscreen:
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
                    self._drawer_leave_ms += 120
                    if self._drawer_leave_ms >= 700:
                        self._drawer_leave_ms = 0
                        self._drawer_auto_opened = False
                        self._on_playlist_button(False)
            else:
                self._drawer_leave_ms = 0

        # ⑥ 用户："图片模式下鼠标靠近右侧自动展开播放列表的功能默认关闭（提供设置开关）
        #    毕竟鼠标在看图时要来回移动" ⇒ 图片模式看 `image.drawer_edge_open`（默认 False），
        #    视频/音频仍用通用开关 `ui.drawer_edge_hover`。
        if self._kind == router.KIND_IMAGE:
            if not self.cfg.get("image.drawer_edge_open", False):
                return
        elif not self.cfg.get("ui.drawer_edge_hover", True):
            return
        if self.drawer.is_open() or self._closing or not inside:
            return
        near_edge = (self.width() - local.x() <= M["drawer_edge_trigger"]
                     and local.y() > M["titlebar_h"])
        # ⚠️ 用户点抽屉自己的收起按钮 / 按 L 收起时, 鼠标**本来就停在右边缘**,
        #    自动展开会立刻把抽屉弹回来 —— 那个收起按钮等于摆设。
        #    所以"上一次是显式收起"之后要先解除抑制：指针必须先离开热区, 再重新靠近才展开。
        if self._edge_suppress:
            if not near_edge:
                self._edge_suppress = False
            return
        if near_edge:
            self._drawer_auto_opened = True     # 标记"这是自动开的", 离开就收回
            self._drawer_leave_ms = 0
            self._on_playlist_button(True)

    def _suppress_edge_hover(self):
        self._edge_suppress = True

    def _autohide_ms(self) -> int:
        """自动隐藏延时：**图片模式优先用 `image.immersive_after_s`**（0 = 用通用值）。

        用户要求"图片也可以长时间不操作进入沉浸模式"—— 图片的沉浸时长和
        视频的"控制条自动隐藏"是两件事，各有各的设置。
        """
        if self._kind == router.KIND_IMAGE:
            secs = float(self.cfg.get("image.immersive_after_s", 0) or 0)
            if secs > 0:
                return int(secs * 1000)
        return int(self.cfg.get("ui.capsule_autohide_ms", 2800))

    def _maybe_hide_capsule(self):
        if not self.cfg.get("ui.capsule_autohide", True):
            return
        # ⑤ 图片模式的"沉浸"：静止 `image.immersive_after_s` 秒后把 chrome 全收起来。
        #    ⚠️ 图片没有"暂停"概念，`self._paused` 对静图可能是 True ⇒ 不能照搬视频的判断。
        if self._kind == router.KIND_IMAGE:
            secs = float(self.cfg.get("image.immersive_after_s", 0) or 0)
            if secs > 0 and self._path and not self.drawer.is_open():
                self.stage.image_bar_host.setVisible(False)
                if self._fullscreen:
                    self.stage.drawer_handle.setVisible(False)
                for z in (self.stage.zone_left, self.stage.zone_right):
                    z.setVisible(False)
            return
        if not self._path or self._paused or self._fullscreen is False and False:
            pass
        if self._paused or not self._path:
            return
        if self.drawer.is_open():
            return
        # 指针停在控制条上就不收
        gp = QCursor.pos()
        r = self.capsule.rect()
        tl = self.capsule.mapToGlobal(r.topLeft())
        if (tl.x() <= gp.x() <= tl.x() + r.width()) and (tl.y() <= gp.y() <= tl.y() + r.height()):
            self._hide_timer.start()
            return
        self._fader.fade_out()
        # 全屏下抽屉手柄跟控制栏**一起收**（用户要求："手柄应该跟控制栏什么的一起久不动后自动隐藏"）。
        # 窗口化时留着它 —— 那是常驻的播放列表入口，收了反而不好找。
        if self._fullscreen and not self.drawer.is_open():
            self.stage.drawer_handle.setVisible(False)

    def _wake_capsule(self):
        self._fader.fade_in()
        self._hide_timer.start()
        # ⑤ 沉浸模式唤醒：把图片栏与热区放回来（切类型/开抽屉也会走到这里）
        if self._kind == router.KIND_IMAGE:
            self.stage.image_bar_host.setVisible(True)
            zw = int(self.cfg.get("image.edge_zone_px", 96) or 0)
            for z in (self.stage.zone_left, self.stage.zone_right):
                z.setFixedWidth(zw)
                z.setVisible(zw > 0)
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
            if t == QEvent.MouseMove:
                self._wake_capsule()
                # ⑫ 拖动平移（图片模式 + 左键按住）
                if (obj is self._image_viewport() and self._kind == router.KIND_IMAGE
                        and self._pan_drag is not None and (ev.buttons() & Qt.LeftButton)):
                    self._pan_to(ev.position())
                    return True
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
            elif t == QEvent.MouseButtonPress and self.drawer.is_open() \
                    and ev.button() == Qt.LeftButton:
                # ⑤ 用户："展开播放列表后点击图片（列表以外）就应该收起列表"
                gp = ev.globalPosition().toPoint() if hasattr(ev, "globalPosition") else None
                inside = False
                if gp is not None:
                    try:
                        tl = self.drawer.mapToGlobal(QPoint(0, 0))
                        inside = QRect(tl, self.drawer.size()).contains(gp)
                    except Exception:
                        inside = False
                if not inside:
                    self._on_playlist_button_from_ui(False)
                    # ⚠️ 绝不 return True：那会把这次按下**吃掉**，
                    #    底栏按钮/热区/列表项就全失灵了（用户实测 P0）。
                    #    这里只做「顺手收起」，事件继续按正常作用域分发。
                    return False
            elif (t == QEvent.MouseButtonPress and obj is self._image_viewport()
                  and self._kind == router.KIND_IMAGE and ev.button() == Qt.LeftButton):
                # ⑫ 左键按下 = 开始拖动平移（记录起点）
                self._pan_drag = [float(ev.position().x()), float(ev.position().y())]
                self.stage.video.setCursor(Qt.ClosedHandCursor)
                return True
            elif t == QEvent.MouseButtonRelease and obj is self._image_viewport() \
                    and self._pan_drag is not None:
                self._pan_drag = None
                self.stage.video.setCursor(Qt.ArrowCursor)
                return True
            elif t == QEvent.KeyRelease:
                if self._hotkey_release(ev):
                    return True
        except Exception:
            log.exception("eventFilter 异常")
        return super().eventFilter(obj, ev)

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
            # ⚠️ 滚轮行为**可配置**（用户："鼠标滚轮增加自定义选项 是图片切换还是放大缩小"）
            action = str(self.cfg.get("image.wheel_action", "zoom") or "zoom")
            if action == "switch":
                self._step_image(-1 if d > 0 else 1)      # 上滚 = 上一张
                ev.accept()
                return
            # ⚠️ 方向原来也是**反的**（`d < 0` 给正指数 ⇒ 上滚在缩小）。
            #    现在是：上滚放大、下滚缩小。
            factor = float(self.cfg.get("ui.image_zoom_step", 1.12)) ** (1 if d > 0 else -1)
            if bool(self.cfg.get("image.zoom_at_cursor", False)):
                self._zoom_image_at(factor, ev.position())
            else:
                self._zoom_image(factor)
        else:
            delta = float(self.cfg.get("playback.volume_step", 5)) * (1 if d > 0 else -1)
            self._on_volume(max(0.0, min(100.0, self.capsule.volume.value() + delta)))
            self.capsule.set_volume(self.capsule.volume.value())
        ev.accept()

    # =====================================================================
    # 事件结束 / 续播
    # =====================================================================
    def _on_file_loaded(self):
        # ⑤⑨ 新文件刚加载完 ⇒ 立刻补写缩放/平移（一次到位，避免"先闪回 100%"）
        if getattr(self, "_pending_image_view", None) and self._kind == router.KIND_IMAGE:
            self._reapply_image_view()
        # ⑧ 用户：「水平镜像居然是常驻开关，不应该换一张图片就重置了吗」
        #    ⇒ 换图时镜像复位（旋转保持，那是构图操作）。
        if self._kind == router.KIND_IMAGE and getattr(self, "_image_flip", False):
            self._image_flip = False
            try:
                self.engine.set("vf", "", MPV_FORMAT_STRING)
                self.stage.image_bar.set_flip_state(False)
            except Exception:
                log.debug("重置镜像失败", exc_info=True)
        # ⑩ 用户：「信息不会随着图片切换更新，每次必须手动按」
        #    ⇒ 浮层开着的时候，换文件就刷新它（视频侧同样）。
        try:
            if self.stage.media_info.isVisible():
                _t, rows = self._collect_media_info()
                self.stage.media_info.set_info(_t, rows)
        except Exception:
            log.debug("刷新信息浮层失败", exc_info=True)
        self._update_navigator()
        self._paused = False
        self.capsule.set_paused(False)
        if self._kind == router.KIND_AUDIO:
            self._load_audio_tags()
        if self._kind == router.KIND_IMAGE:
            self._update_image_info()
        mode = str(self.cfg.get("playback.loop_mode", "sequence"))
        self.engine.set("loop-file", "inf" if (mode == "single" or self._kind == router.KIND_IMAGE) else "no",
                        MPV_FORMAT_STRING)
        self._auto_sub_notice()
        if self.subtitle_panel is not None and self.subtitle_panel.isVisible():
            self._refresh_subtitle_tracks()

    def _on_end_file(self, reason: int, error: int):
        if self._closing:
            return
        if error:
            self.show_osd("播放失败：文件可能已损坏或格式不受支持", "info", P["danger"], ms=2600)
            return
        if reason in (0):   # EOF
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
        self._hide_timer.setInterval(self._autohide_ms())
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
        if not getattr(self, "_native_ready", False):
            self._native_ready = True
            import time as _t

            def _m(lbl, t0):
                log.info("  [窗口] %-20s %6.0f ms", lbl, (_t.perf_counter() - t0) * 1000)
                return _t.perf_counter()

            t = _t.perf_counter()
            win.make_resizable_frameless(self)
            t = _m("补 WS_THICKFRAME", t)
            win.enable_rounded_corners(self)
            t = _m("DWM 圆角", t)
            win.set_dark_mode(self, True)
            # 窗口级亚克力（QQ 那种边缘模糊）。
            # ⚠️ 只有**真的开上了**才把舞台底改成半透明 ——
            #    否则系统不支持时会露出黑底/垃圾。
            self._acrylic_on = win.set_acrylic(self, True)
            if self._acrylic_on:
                self._apply_acrylic_alpha()
            t = _m("DWM 深色", t)
            self.titlebar.raise_()
            _m("抬标题栏", t)
            log.info("  [窗口] 客户区 %sx%s  dpr=%s", self.width(), self.height(),
                     self.devicePixelRatioF())

    def changeEvent(self, ev):
        if ev.type() == QEvent.WindowStateChange:
            self.titlebar.set_maximized(self.isMaximized())
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
            w = max(M["win_min_w"], w)
            h = max(M["win_min_h"], h)
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
        self.cfg.set("ui.maximized", self.isMaximized())
        if not self.isMaximized():
            g = self.geometry()
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
            self.stage.video.release_resources()
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
        """窗口已关闭、Qt 拆控件之前调用：销毁 mpv 实例。"""
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
