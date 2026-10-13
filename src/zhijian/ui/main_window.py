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
        if kind == router.KIND_IMAGE and self._kind == router.KIND_IMAGE:
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
        load_opts = "vid=no,aid=auto" if kind == router.KIND_AUDIO else None
        self.engine.load(path, options=load_opts)
        # 图片模式下 mpv 是停止状态：pause 写进去毫无意义，只是白白一次同步 IPC
        if autoplay and self._kind != router.KIND_IMAGE:
            self.engine.set("pause", False, MPV_FORMAT_FLAG)
        self._update_empty_state()
        # 图片没有「播放位置」可言：跳过恢复，省掉一次 mpv 写入（连续翻图时每张都省）
        if kind != router.KIND_IMAGE:
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
        # 媒体路由（红线 1）：图片 = Qt 原生 GPU 视口；音视频 = mpv 渲染画布。
        # ⚠️ 1.0.11 基线里这里只切了 image_bar；视口的显隐必须一起切，
        #    否则视口虽然建好了却从不显示（几何 100x30 ⇒ 取景框"父级不可见"，实测踩到）。
        try:
            self.stage.image_view.setVisible(kind == router.KIND_IMAGE)
            self.stage.video.setVisible(kind != router.KIND_IMAGE)
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
        self.stage.audio_card.setVisible(kind == router.KIND_AUDIO)
        # #3 看图模式：**隐藏播放列表手柄**（看图时它意义不大且挡画面）
        # #6 看图模式**保留**抽屉手柄（用户明确要求）；它与左右热区箭头都在
        #    50% 垂直中心、水平方向互不重叠（热区箭头内缩 28px）。自动展开仍停用。
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
        """图片缩放统一入口：走 Qt GPU 视口（本地矩阵），音视频才用 mpv。"""
        _v = self._iv()
        if self._kind == router.KIND_IMAGE and _v is not None and _v.has_image():
            _v.zoom_at_cursor(float(factor))
            self._update_image_info()
            return
        return self._zoom_image_mpv(factor, at_cursor)

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

    def _step_image(self, delta: int):
        items = self.drawer.paths()
        if not items:
            return
        try:
            i = [os.path.normcase(p) for p in items].index(os.path.normcase(self._path))
        except ValueError:
            i = 0
        j = (i + delta) % len(items)
        self.open_path(items[j], remember_dir=False)

    def _update_window_title(self, path: str):
        """#7 标题栏显示「文件名 · 像素」。"""
        self._title_base = os.path.basename(path or "")
        self._title_px = ""
        self.titlebar.set_title(self._title_base or APP_NAME)

    def _title_text(self) -> str:
        base = getattr(self, "_title_base", "") or os.path.basename(self._path or "")
        px = getattr(self, "_title_px", "")
        if base and px:
            return "%s · %s" % (base, px)
        return base or APP_NAME

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
        if now - self._last_preview_seek < 0.08:
            return
        self._last_preview_seek = now
        self.engine.seek_absolute(seconds, precise=False)

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
        # v1.0.20：直接同步一次图标（不依赖 WindowStateChange 的到达时机）
        try:
            self.stage.image_bar.set_fullscreen(bool(self._fullscreen))
            self.capsule.set_fullscreen(bool(self._fullscreen))
        except Exception:
            log.debug("同步全屏图标失败", exc_info=True)
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
        self._hide_timer.setInterval(int(self.cfg.get("ui.capsule_autohide_ms", 2800)))
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
        # #6 图片沉浸模式用：底部图片控制栏 + 抽屉手柄各自一个淡入淡出器
        self._imgbar_fader = CapsuleFader(self.stage.image_bar_host, duration=240)
        self._handle_fader = CapsuleFader(self.stage.drawer_handle, duration=240)
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

        # #7 看图模式：手柄已隐藏 ⇒ 不能再靠"靠近右边缘"展开（用户要求停用）
        if self._kind == router.KIND_IMAGE:
            return
        if not self.cfg.get("ui.drawer_edge_hover", True):
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
        else:
            # #7 视频侧同样把像素写进标题栏（原来只有文件名）
            try:
                self._set_title_px(self.engine.cached("width", 0) or 0,
                                   self.engine.cached("height", 0) or 0)
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
            try:
                self.stage.sync_hotspots()
            except Exception:
                pass
            self.titlebar.set_maximized(self.isMaximized())
            # #10 全屏状态变化 ⇒ 同步「全屏 / 退出全屏」图标。
            #    ⚠️ 必须挂这里：双击标题栏、F11、系统菜单等路径都能改窗口状态，
            #    只在 toggle_fullscreen 里换图标会漏掉。
            try:
                on = bool(self.isFullScreen())
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
