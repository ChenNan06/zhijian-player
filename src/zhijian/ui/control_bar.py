"""底部悬浮胶囊控制条。

逆向依据（爱奇艺万能联播 5.4.1, 1299x883 窗口）：
  * 控制条总高 78：上排进度条 28 + 下排按钮 50；
  * 左组 循环 / 播放顺序 / 画中画 / 截图 + 时间「00:09/00:30」；
  * 中组 停止 / 上一集 / 大圆播放暂停(44) / 下一集；
  * 右组 音量 / 音量条 / 倍速 / 字幕 / 工具箱 / 全屏。

现代化：贴底通栏 → **悬浮胶囊**（左右 18 / 距底 16 外边距, 圆角 18）,
通透冷色底 + 0.5px 内发光高光边 + 柔和弥散阴影。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import (QEasingCurve, Property, QMutex, QPoint, QPropertyAnimation,
                            QThread, Qt, QTimer, QWaitCondition, Signal)
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget
)

from .theme import M, P, css, qt_color
from .widgets import (GlassFrame, IconButton, PreviewPopup, ProgressSlider, Spacer,
                      TextIconButton, _GLASS_LIVE, fmt_time, prep_popup, RoundedMenu
)

log = logging.getLogger("zhijian.ui")


LOOP_MODES = [
    ("sequence", "顺序播放", "order"),
    ("loop", "列表循环", "loop"),
    ("single", "单个循环", "loop_one"),
    ("random", "随机播放", "shuffle"),
]
LOOP_BY_KEY = {k: (label, ic) for k, label, ic in LOOP_MODES}


class TimeLabel(QLabel):
    """`00:09 / 00:30` —— 用等宽数字, 避免秒数跳动时整条控制栏左右抖。"""

    def __init__(self, parent=None, width: int = M["time_width"]):
        super().__init__("/", parent)
        self.setFixedWidth(width)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setStyleHint(QFont.Monospace)
        f.setFamilies(["Consolas", "Cascadia Mono", "Microsoft YaHei UI"])
        self.setFont(f)
        # ⚠️ 时间戳必须深黑高对比（用户："00:00 / 00:00 肉眼依然几乎无法辨识"）。
        #    原来这里写的是 `color:{P['text_dim']}` —— 又是**构造时快照**，
        #    换肤后不跟着变；而且它连调色板都没走对。
        #    现在直接写死一级文字色，并在主题切换时重刷。
        self.setProperty("role", "body")
        _GLASS_LIVE.append(__import__("weakref").ref(self))
        self._is_time_label = True
        self._pos = 0.0
        self._dur = 0.0
        self._shown = (-1, -1)
        self.update_text()

    def set_times(self, pos: float, dur: float):
        """只在**显示出来的秒数**变化时重排文本。

        ⚠️ 不要写成"比较 pos 与上次的 pos" —— 那样每次只差 0.03 秒,
        判据 `>= 0.25` 永远不成立, 文字会永远停在 00:00（本项目真实踩过）。
        """
        self._pos = float(pos or 0.0)
        self._dur = float(dur or 0.0)
        key = (int(self._pos), int(self._dur))
        if key != self._shown:
            self._shown = key
            self.update_text()

    def update_text(self):
        self.setText(f"{fmt_time(self._pos)} / {fmt_time(self._dur)}")


class PreviewWorker(QThread):
    """后台取帧线程。

    ⚠️⚠️ **取帧绝不能放在 GUI 线程上做。**
    首次取帧要起解码器（100ms~1s+），远距离 seek 也要 400ms+ ——
    实测放在主线程会把事件循环整个卡住，连控制面 IPC 都超时
    （`TimeoutError: zhjprev3-ctl 无响应`）。

    设计：只保留**最后一次**请求（悬停时鼠标一直在动，中间那些请求没有意义），
    用 QWaitCondition 睡眠等待，不空转。
    """

    ready = Signal(object, float)          # QImage, 秒

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mutex = QMutex()
        self._cond = QWaitCondition()
        self._req = None
        self._stop = False
        self._grabber = None
        # 预铺队列：整条时间轴的缩略图，空闲时一张张补上
        self._prefetch: list = []
        self._prefetch_path = ""

    def preheat(self, path: str, center: float, span: float = 6.0, step: float = 2.0):
        """**局部辐射预热**：以悬停点为中心，向前后各抽几张。

        ⚠️ 不再全量预铺：几小时的电影全量铺 150 张既耗性能也不现实。
        只要鼠标停在某个区域，就把周围 ±2/4/6 秒那几张补上，
        实现“局部随动缓存”。
        """
        if not path:
            return
        times = []
        for k in range(1, int(span / step) + 1):
            for s in (center - k * step, center + k * step):
                if s >= 0:
                    times.append(s)
        self._mutex.lock()
        self._prefetch_path = path
        self._prefetch = times
        self._cond.wakeAll()
        self._mutex.unlock()

    def request(self, path: str, seconds: float):
        """单任务抢占：**清空旧队列，只解最新这一个点**。

        ⚠️ 鼠标快速划动时，中间那些请求**全部是废弃的**；
        不清队列就会排着解一堆无用的帧，越排越滞后。
        """
        self._mutex.lock()
        self._req = (path, float(seconds))
        self._prefetch = []                 # 丢弃所有待做的预热
        self._cond.wakeAll()
        self._mutex.unlock()
        # 正在跑的 ffmpeg 若已过时，直接杀掉让最新任务插队
        try:
            if self._grabber is not None:
                self._grabber.cancel_current()
        except Exception:
            pass

    def run(self):
        while True:
            self._mutex.lock()
            while self._req is None and not self._prefetch and not self._stop:
                self._cond.wait(self._mutex, 250)
            if self._stop:
                self._mutex.unlock()
                break
            if self._req is not None:
                path, sec = self._req
                self._req = None
                live = True
            else:
                # 预铺：一次取一张，取完继续等（实时请求随时插队）
                path, sec = self._prefetch_path, self._prefetch.pop(0)
                live = False
            self._mutex.unlock()
            try:
                if self._grabber is None:
                    from ..mpv.frame_grab import FrameGrabber

                    self._grabber = FrameGrabber()
                img = self._grabber.grab(path, sec)
            except Exception:
                img = None
            if img is not None and live:
                self.ready.emit(img, sec)

    def stop(self):
        # ⚠️⚠️ 顺序很重要：**先杀掉正在跑的 ffmpeg**，再置停止位。
        #    否则线程还卡在 `communicate(timeout=12)` 里，wait 等不到，
        #    QThread 被析构 → 进程以 0xC0000409 崩溃、退出要 4 秒。
        try:
            if self._grabber is not None:
                self._grabber.close()
        except Exception:
            pass
        self._mutex.lock()
        self._stop = True
        self._cond.wakeAll()
        self._mutex.unlock()
        self.wait(2500)


class _BareSpeedButton(TextIconButton):
    """★ task-128①：底栏「1.0x」—— 常态**零边框零背景**，仅悬停浮现柔和圆底。

    TextIconButton 默认会画 1px 胶囊描边（checked 时还有 accent 底），
    这里重写 paintEvent：只画文字 + 悬停柔光圆底，彻底删掉外圈胶囊。
    """

    def paintEvent(self, _ev):  # noqa: N802
        from PySide6.QtGui import QColor, QPainter, QPen
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            t = max(0.0, min(1.0, float(getattr(self, "_hover_t", 0.0))))
            if t > 0.01:
                c = QColor(128, 128, 128)
                c.setAlphaF(0.15 * t)
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
                rad = min(float(getattr(self, "_radius", 13)), r.height() / 2.0)
                p.drawRoundedRect(r, rad, rad)
            color = P["accent"] if getattr(self, "_checked", False) else (
                P["text"] if t > 0.5 else getattr(self, "_color", P["text_dim"]))
            p.setPen(qt_color(color))
            p.setFont(self.font())
            p.drawText(self.rect(), Qt.AlignCenter, getattr(self, "_text", ""))
        finally:
            p.end()


class _BareBadgeButton(IconButton):
    """★ task-128①：底栏「词」—— 常态**只渲染文字**，删掉外层圆角方框，仅悬停有柔光圆底。"""

    def paintEvent(self, _ev):  # noqa: N802
        from PySide6.QtGui import QColor, QFont, QPainter
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            t = max(0.0, min(1.0, float(getattr(self, "_hover_t", 0.0))))
            if t > 0.01:
                c = QColor(128, 128, 128)
                c.setAlphaF(0.15 * t)
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                r = QRectF(self.rect()).adjusted(2.0, 2.0, -2.0, -2.0)
                p.drawEllipse(r)
            f = QFont(self.font())
            f.setPixelSize(max(9, int(round(self.height() * 0.52))))
            f.setWeight(QFont.Medium)
            p.setFont(f)
            color = P["accent"] if t > 0.5 else getattr(self, "_color", P["text_dim"])
            p.setPen(qt_color(color))
            p.drawText(self.rect(), Qt.AlignCenter, "词")
        finally:
            p.end()


class ControlBar(GlassFrame):
    play_pause_clicked = Signal()
    # ⚠️ 没有 stop_clicked：用户要求去掉中组的「停止」按钮（它让播放键无法居中）。
    #    停止仍可用快捷键 X（config.hotkeys.stop → MainWindow.stop）。
    prev_clicked = Signal()
    next_clicked = Signal()
    frame_back = Signal()
    frame_fwd = Signal()
    seek_started = Signal()
    seek_preview = Signal(float)
    seek_committed = Signal(float)
    volume_changed = Signal(float)
    mute_toggled = Signal(bool)
    speed_changed = Signal(float)
    subtitle_clicked = Signal()
    settings_clicked = Signal()
    fullscreen_toggled = Signal()
    # ⚠️ 必须带 bool：它由 `btn_list.toggled(bool)` 转发而来。写成 `Signal()` 会在 emit 时抛
    #    "playlist_toggled() only accepts 0 argument(s), 1 given!"，**而且只写进日志** ——
    #    表现就是"右下角播放列表按钮是摆设"（用户 2026-10-10 反馈；标题栏的同名信号声明是对的）。
    playlist_toggled = Signal(bool)
    screenshot_clicked = Signal()
    loop_mode_changed = Signal(str)
    pip_clicked = Signal()
    snapshot_clicked = Signal()

    def __init__(self, parent=None, speeds=(0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0)):
        super().__init__(
            parent,
            radius=M["capsule_radius"],
            fill=P["glass_capsule"],
            # QQ NT 的悬浮层阴影：大半径 + 极低 alpha。
            # 原来 16 层黑色叠出来一圈发脏的灰边（用户：
            # "底部悬浮条外围那圈生硬、发脏的重阴影必须彻底移除"）。
            shadow=float(P.get("float_shadow_alpha", 0.10)),
            shadow_layers=int(P.get("float_shadow_layers", 12)),
            shadow_spread=int(P.get("float_shadow_spread", 0)),
            border=P["hairline"],
            pad=(M["capsule_pad_h"], M["capsule_pad_v"], M["capsule_pad_h"], M["capsule_pad_v"])
        )
        self.setObjectName("Capsule")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setProperty("capsule", True)
        self._speeds = list(speeds)
        self._speed = 1.0
        self._loop_mode = "sequence"
        self._paused = True
        self._muted = False
        self._last_volume = 100.0
        self._fade_t = 1.0

        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------- 上排：进度条 ----------------
        self.progress = ProgressSlider(self)
        self.progress.seekStarted.connect(self.seek_started.emit)
        self.progress.seekPreview.connect(self.seek_preview.emit)
        self.progress.seekCommitted.connect(self.seek_committed.emit)
        self.progress.setFixedHeight(M["progress_row_h"])
        # 悬停预览：缩略图 + 时间
        self._preview = PreviewPopup(self)
        self._preview_path = ""
        # 占位图来源（主窗口注入）：没有缓存帧时拿"当前播放画面"模糊顶上
        self._placeholder_fn = None
        self._placeholder = None
        self._preview_on = True          # 悬停缩略图总开关（设置里可关）
        # v1.0.28 ③：模式守卫 —— 音乐模式**严禁**弹视频缩略图预览框（用户实测：
        # 悬停进度条冒出一个黑色空白预览框）。关掉后连 FrameGrabber 都不会启动。
        self._preview_mode_ok = True
        self._worker = PreviewWorker(self)
        self._worker.ready.connect(self._on_frame)
        _app = QApplication.instance()
        if _app is not None:
            _app.aboutToQuit.connect(self._shutdown_preview)
        # ⚠️ **不在这里 start()**：预览未启用时它只会让退出变慢
        #    （实测退出从 0.5s 变成 **5.2s**，就是 stop() 里的 wait(4000)）。
        #    真正需要时在 set_preview_source 里懒启动。
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        # ⚠️ 去抖 130ms：鼠标快速划过时**只移动弹窗 + 跳时间**，
        #    **完全不向后台发取帧任务** —— 否则会疯狂起停子进程。
        self._preview_timer.setInterval(130)
        self._preview_timer.timeout.connect(self._do_preview)
        self.progress.hoverChanged.connect(self._on_hover)
        root.addWidget(self.progress)

        # ---------------- 下排：按钮 ----------------
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(M["btn_gap"])

        # ⚠️⚠️ 核心播放按钮必须**绝对居中**。
        #    单条流式布局下，右侧（音量/倍速/字幕/全屏…）比左侧（循环/画中画/截图/时间戳）
        #    宽得多，中间那组就被挤到左边去了（用户："核心控制按钮明显往左偏离中心线"）。
        #    做法：在中组**左侧**和右侧各插一个可调间隔，让左右两组占据**等宽**，
        #    中组自然落在 50% 处。宽度在 resizeEvent 里按实际几何算。
        #    ⚠️ 平衡间隔必须放在**左组之后**，不能放在最左端：
        #       放最左端会让左边那几个按钮整体内缩（用户："最左边几个按钮为什么不挪到最左边，
        #       而是要空出来"）。放左组之后，左边贴边、播放键照样在 50%。
        self._pad_l = QSpacerItem(0, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
        self._pad_r = QSpacerItem(0, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
        self._left_group: list = []
        self._right_group: list = []

        # 左组
        self.btn_loop = IconButton("order", self, tooltip="顺序播放（点击切换）", size=30, icon_size=17)
        self.btn_loop.clicked.connect(self._cycle_loop)
        self.btn_pip = IconButton("pip", self, tooltip="画中画（置顶小窗）", size=30, icon_size=17)
        self.btn_pip.clicked.connect(self.pip_clicked.emit)
        self.btn_shot = IconButton("screenshot", self, tooltip="截图 (S)", size=30, icon_size=17)
        self.btn_shot.clicked.connect(self.screenshot_clicked.emit)
        self.time_label = TimeLabel(self)
        for w in (self.btn_loop, self.btn_pip, self.btn_shot):
            row.addWidget(w)
            self._left_group.append(w)
        row.addSpacing(4)
        row.addWidget(self.time_label)
        self._left_group.append(self.time_label)

        # 平衡间隔（左组之后）—— 见上面那段注释：放这里才能"左边贴边 + 播放键居中"
        row.addSpacerItem(self._pad_l)
        row.addStretch(1)

        # 中组（视觉居中）
        # ⚠️ 用户明确要求**去掉「停止」按钮**："把中间四个按钮的停止按钮去掉，
        #    否则最重要的播放暂停按钮无法居中、视觉不平衡。"
        #    四个按钮时组中心落在「上一首」和「播放」之间 ⇒ 播放键必然偏左；
        #    三个按钮时**播放键就是组中心**，与上面的平衡逻辑叠加后才是真正的 50%。
        #    停止功能仍然可用：快捷键 X（见 config 的 hotkeys.stop）。
        self.btn_prev = IconButton("prev", self, tooltip="上一个 (PageUp)", size=36, icon_size=20)
        self.btn_prev.clicked.connect(self.prev_clicked.emit)
        self.btn_play = IconButton("play", self, tooltip="播放/暂停 (Space)", size=M["btn_size_lg"],
                                   icon_size=22, color=P["text"], hover_color=P["text"])
        self.btn_play.clicked.connect(self.play_pause_clicked.emit)
        self.btn_next = IconButton("next", self, tooltip="下一个 (PageDown)", size=36, icon_size=20)
        self.btn_next.clicked.connect(self.next_clicked.emit)
        row.addWidget(self.btn_prev)
        row.addSpacing(4)
        row.addWidget(self.btn_play)
        row.addSpacing(4)
        row.addWidget(self.btn_next)
        row.addSpacerItem(self._pad_r)      # 中组之后：右侧平衡间隔
        row.addStretch(1)

        # 右组
        self.btn_mute = IconButton("volume_high", self, tooltip="静音 (M)", size=30, icon_size=17)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.volume = None
        from .widgets import VolumeSlider

        self.volume = VolumeSlider(self, width=M["volume_width"])
        self.volume.valueChanged.connect(self._on_volume)
        # ★★ task-95（用户 94）：倍速按钮恢复**紧凑 40~44px**（原 pad=11 ⇒ 视文本在 42~56 浮动）；
        #   `advance+pad*2` 规则只留给设置面板三按钮，别误伤底栏。
        self.btn_speed = _BareSpeedButton("1.0x", self, tooltip="播放速度", height=26, pad=8,
                                        width_range=(40, 44))
        self.btn_speed.clicked.connect(self._show_speed_menu)
        self.btn_sub = IconButton("subtitle", self, tooltip="字幕 (V)", size=30, icon_size=17)
        self.btn_sub.clicked.connect(self.subtitle_clicked.emit)
        self.btn_snapshot = IconButton("next_image", self, tooltip="下一帧 (.)", size=30, icon_size=17)
        self.btn_snapshot.clicked.connect(self.frame_fwd.emit)
        # ⚠️ 设置入口已去重：标题栏上有一个就够了（用户反馈"一个页面太多重复按钮了"）。
        #    控件对象保留（外部仍会引用它做菜单锚点等），但不在胶囊上显示。
        self.btn_settings = IconButton("settings", self, tooltip="播放设置", size=30, icon_size=17)
        self.btn_settings.clicked.connect(self.settings_clicked.emit)
        self.btn_settings.setVisible(False)
        # ★ task-26③：音乐模式专属快捷入口（Tooltip「歌词与音乐设置」）
        #   仅 KIND_AUDIO 可见（_sync_music_btn 从窗口读 _kind，零新定时器）；
        #   点击 ⇒ 打开设置面板并**定位到「音乐与歌词」分区**。
        self.btn_music = _BareBadgeButton("music_note", self, tooltip="歌词与音乐设置", size=30, icon_size=17)
        self.btn_music.setObjectName("btnMusicSettings")
        self.btn_music.clicked.connect(self._open_music_settings)
        self.btn_music.setVisible(False)          # 默认隐藏，等首次 _sync_music_btn
        self._music_btn_on = False
        self.btn_full = IconButton("fullscreen", self, tooltip="全屏 (F)", size=30, icon_size=17)
        self.btn_full.clicked.connect(self.fullscreen_toggled.emit)
        self.btn_list = IconButton("playlist", self, tooltip="播放列表 (L)", size=30, icon_size=17,
                                   checkable=True)
        self.btn_list.toggled.connect(self.playlist_toggled)   # 信号对信号（都是 bool）

        # ★★ task-80③：底栏整排图标**统一取色 + 统一线宽 1.6px**。
        #    以前各自散落：默认走 text_dim 快照、「词」徽章/倍速/全屏/播放列表
        #    用不同来源的颜色与笔宽 ⇒ 一排图标深浅不一（用户截图圈出）。
        #    统一锚到调色板 icon / icon_hover（换肤由 _key_of 自动重解析）。
        for _b in (self.btn_loop, self.btn_pip, self.btn_shot,
                   self.btn_prev, self.btn_play, self.btn_next,
                   self.btn_mute, self.btn_speed, self.btn_sub,
                   self.btn_snapshot, self.btn_settings, self.btn_music,
                   self.btn_full, self.btn_list):
            try:
                _b.set_color(P["icon"])
                _b.set_hover_color(P["icon_hover"])
                if hasattr(_b, "set_stroke"):
                    _b.set_stroke(1.6)
            except Exception:
                pass
        # ★ task-80①：胶囊自身也禁止换肤时刷原生底板（它是 GlassFrame 自绘）
        self.setAutoFillBackground(False)

        for w in (self.btn_mute, self.volume):
            row.addWidget(w)
            self._right_group.append(w)
        row.addSpacing(6)
        for w in (self.btn_speed, self.btn_sub, self.btn_snapshot,
                  self.btn_music, self.btn_settings, self.btn_full, self.btn_list):
            row.addWidget(w)
            self._right_group.append(w)
        root.addLayout(row)
        self.body_layout().addLayout(root)

        self.set_volume(self._last_volume, emit=False)

    def _balance_sides(self):
        """把中组顶到**绝对 50%**处。

        ⚠⚠四个坑（全部实测踩过）：
        1. **不能用控件宽度求和建模** —— 布局里还有 addSpacing()，
           建模必然漏算（实测恒定偏 39px）。必须读 mapTo() 的真实几何。
        2. **改完间隔必须 activate()** —— 只 invalidate() 是标记为脏，
           要等下一轮才重排，量到的还是旧几何。
        3. **基准取窗口中线、坐标取窗口坐标系** —— 用胶囊自身宽度中点
           会差约 20px（可视面板是 body_rect()，扣了阴影外边距）。
        4. **不能在循环内提前 return** —— activate() 后量到的几何会自认为收敛，
           而真正上屏后还差几十像素。改成**每次只量一次 + 自续调度**。
        """
        try:
            win = self.window()
            if win is None or win.width() <= 0:
                return
            lay = self.body_layout()
            lay.activate()
            x0 = self.btn_prev.mapTo(win, QPoint(0, 0)).x()
            x1 = self.btn_next.mapTo(win, QPoint(self.btn_next.width(), 0)).x()
            delta = win.width() / 2.0 - (x0 + x1) / 2.0
            n = getattr(self, "_balancing", 0)
            if abs(delta) >= 1.0 and n < 30:
                cur = self._pad_l.sizeHint().width()
                # 平衡间隔在中组左侧，每增 1px 中心右移约 0.5px；用 2.2 加自续调度兜底
                self._pad_l.changeSize(max(0, int(cur + 2.2 * delta)), 0,
                                       QSizePolicy.Fixed, QSizePolicy.Minimum)
                self._pad_r.changeSize(0, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
                lay.invalidate()
                self._balancing = n + 1
                QTimer.singleShot(16, self._balance_sides)
            else:
                self._balancing = 0
        except Exception:
            log.debug("居中平衡失败", exc_info=True)

    # -- 属性（淡出用） --------------------------------------------------------
    def get_fade(self) -> float:
        return self._fade_t

    def set_fade(self, v: float):
        self._fade_t = float(v)
        self.setWindowOpacityCompat(self._fade_t)

    def setWindowOpacityCompat(self, v: float):
        # 胶囊是普通子控件, 不能改窗口透明度；这里通过"整体可见性 + 位移"表达淡出：
        # 真正生效的是 MainWindow 里的 FadeController（见 main_window）。
        self.setVisible(v > 0.02)

    fade = Property(float, get_fade, set_fade)

    # -- 状态同步 -------------------------------------------------------------
    def set_times(self, pos: float, dur: float):
        # task-20 ②（用户⑧）：拖动期"滑块与时间文本都归鼠标"。
        # 滑块本身有 _dragging 守卫，时间文本没有；不在这里统一拦一次，
        # mpv 的 time-pos 回写会把左侧时间刷回去、和鼠标抢位（实测逐帧可见）。
        # 松手时 _on_seek_committed 会立刻用松手值刷一次（那时已不是拖动态）。
        # ★ task-26③：顺带对账"音乐模式快捷按钮"的可见性（零新增定时器，仅两次比较）
        self._sync_music_btn()
        if self.progress.is_dragging():
            return
        self.progress.set_duration(dur)
        self.progress.set_position(pos)
        self.time_label.set_times(pos, dur)

    def set_cache(self, cache_time: float):
        self.progress.set_cache(cache_time)

    def set_paused(self, paused: bool):
        self._paused = paused
        self.btn_play.set_icon("play" if paused else "pause")
        self.btn_play.setToolTip("播放 (Space)" if paused else "暂停 (Space)")

    def set_volume(self, value: float, emit: bool = False):
        value = max(0.0, min(100.0, float(value)))
        if value > 0.5:
            self._last_volume = value
        self.volume.set_value(value)
        self._update_volume_icon(value, self._muted)
        if emit:
            self.volume_changed.emit(value)

    def set_mute(self, muted: bool, emit: bool = False):
        self._muted = bool(muted)
        self.btn_mute.set_checked(self._muted)
        self._update_volume_icon(self.volume.value(), self._muted)
        if emit:
            self.mute_toggled.emit(self._muted)

    def _update_volume_icon(self, value: float, muted: bool):
        if muted or value <= 0.5:
            name = "volume_mute"
        elif value < 55:
            name = "volume_low"
        else:
            name = "volume_high"
        self.btn_mute.set_icon(name)
        self.btn_mute.setToolTip("取消静音 (M)" if muted else "静音 (M)")

    def set_speed(self, speed: float):
        self._speed = float(speed or 1.0)
        # 负速度 = 倒放（长按左方向键）：用 ◀ 标出来，别让人以为显示坏了
        self.btn_speed.set_text(f"{self._speed:g}x" if self._speed >= 0
                                else f"◀{abs(self._speed):g}x")
        self.btn_speed.set_checked(abs(abs(self._speed) - 1.0) > 1e-6)

    def set_loop_mode(self, mode: str):
        self._loop_mode = mode if mode in LOOP_BY_KEY else "sequence"
        label, ic = LOOP_BY_KEY[self._loop_mode]
        self.btn_loop.set_icon(ic)
        self.btn_loop.setToolTip(f"{label}（点击切换）")

    def set_fullscreen(self, on: bool):
        self.btn_full.set_icon("fullscreen_exit" if on else "fullscreen")
        self.btn_full.setToolTip("退出全屏 (F/Esc)" if on else "全屏 (F)")

    def set_list_checked(self, on: bool):
        self.btn_list.set_checked(on)

    # ★★ task-89（用户 5）：**PiP 进出时控制栏可见性快照 / 逐位恢复**
    def set_pip_mode(self, on: bool) -> None:
        """进入 PiP：快照所有控件可见性后整体隐藏；退出：逐位恢复 + 按当前宽度再对账。

        用户现象：**退出 PiP 后音量图标/滑块/1.0x 倍速消失** ✗。
        根因：PiP 期间窗口只有 480 宽，`_apply_visibility()` 用**旧宽度**把
        `volume(w<720) / btn_mute(w<620) / btn_speed(w<860) / btn_list(w<960)` 判成隐藏；
        退出时窗口虽恢复，但这些控件**不会自己回来**（没有重新对账）⇒ 永久消失。
        修法：**进入时快照 + 退出时逐位恢复**，再补一次按"当前媒体类型/宽度"的对账 ⇒ 双保险 ✓
        """
        on = bool(on)
        if on:
            snap = {}
            for wdg in self.findChildren(QWidget):
                try:
                    snap[wdg] = bool(wdg.isVisible())
                except Exception:
                    pass
            self._pip_vis_snapshot = snap
            self.setVisible(False)
            return
        self.setVisible(True)
        self.restore_from_pip()

    def restore_from_pip(self) -> None:
        """退出 PiP 后**逐位恢复**控件可见性，并按当前媒体类型/宽度重新对账。"""
        snap = getattr(self, "_pip_vis_snapshot", None) or {}
        for wdg, vis in list(snap.items()):
            try:
                wdg.setVisible(bool(vis))
            except Exception:
                pass
        self._pip_vis_snapshot = None
        # ⚠️⚠️ **不能在这里同步 `_apply_visibility()`** —— 退出 PiP 的瞬间胶囊宽度
        #   还是 PiP 时代的 460px（窗口刚恢复、布局还没跑）⇒ 又按窄宽度把
        #   volume(w<720)/mute(w<620)/speed(w<860)/list(w<960) 判成隐藏 ✗✗
        #   （我第一版就踩了：实测退出后 volume/speed/mute/list 仍 visible=False）。
        #   正确做法：**等布局跑完再对账** —— 0ms 一次、80ms 再一次（只有 PiP 退出触发，零每帧开销）。
        self.update()
        # 0/80ms 对账可见性；240ms 再补一次**居中平衡**（中组按钮的 pad_l 依赖最终宽度，
        # 实测少了这一步会在音频模式下让 播放/上一曲/下一曲 偏 +18px ✗）
        for _ms in (0, 80, 240):
            QTimer.singleShot(_ms, self._reapply_visibility_deferred)

    def _reapply_visibility_deferred(self) -> None:
        """PiP 退出后的**延迟可见性对账**（此时胶囊宽度已是恢复后的真实宽度）。"""
        try:
            self._apply_visibility()
        except Exception:
            pass
        try:
            self._balance_sides()
        except Exception:
            pass
        self.update()

    def set_media_kind(self, kind: str):
        # 切换媒体会显隐按钮，左右组宽度变了，重新平衡
        QTimer.singleShot(0, self._balance_sides)

        """按媒体类型切换控制条形态：图片隐藏时间轴, 音频隐藏画面类按钮。"""
        self._kind = kind if kind in ("video", "image", "audio") else "video"
        self._apply_visibility()

    # -- 自适应：窗口变窄时按优先级收起次要按钮, 杜绝文字/图标重叠 --------------
    def _apply_visibility(self):
        kind = getattr(self, "_kind", "video")
        w = self.width()
        is_image = kind == "image"
        is_audio = kind == "audio"
        # 媒体类型决定
        self.progress.setVisible(not is_image)
        self.time_label.setVisible(not is_image)
        self.btn_loop.setVisible(not is_image)
        self.btn_sub.setVisible(kind == "video")
        self.btn_shot.setVisible(kind == "video")
        self.btn_snapshot.setVisible(kind == "video")
        self.btn_pip.setVisible(kind == "video")
        self.btn_prev.setVisible(True)
        self.btn_next.setVisible(True)
        # ★★ task-89（用户 5）**根治「控件消失」**：宽度收起必须**双向判定**。
        #   旧实现只有上文那些 `if w < X: setVisible(False)`，**没有对应的 setVisible(True)**
        #   ⇒ 一旦在窄窗口（例如 PiP 的 480px）被隐藏，**窗口变宽后永远不会回来** ✗✗
        #   —— 这正是用户"退出 PiP 后音量图标/滑块/1.0x 倍速消失"的真正根因
        #   （不是 PiP 代码的问题，而是 `_apply_visibility()` 单向下发）。
        #   现在每个可收起控件都写成**完整三元表达式**：类型允许 且 宽度足够 ⇒ 可见 ✓
        self.btn_pip.setVisible(kind == "video" and w >= 1010)
        self.btn_snapshot.setVisible(kind == "video" and w >= 1080)
        self.btn_shot.setVisible(kind == "video" and w >= 900)
        # ★★ task-95（用户 94）：**底栏不再有设置齿轮** —— 原来的 `setVisible(w >= 1060)` 会把
        #   创建时隐藏的齿轮**重新显示出来** ✗（用户看到「词」与「全屏」之间多一个齿轮）。
        #   设置入口只保留标题栏一个，底栏这个**永久隐藏**。
        self.btn_settings.setVisible(False)
        self.btn_speed.setVisible(w >= 860)
        self.btn_list.setVisible(w >= 960)
        self.volume.setVisible(w >= 720)
        self.btn_mute.setVisible(w >= 620)
        # 左侧小组在极窄时再收一层（同样双向）
        self.btn_loop.setVisible((not is_image) and w >= 680)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply_visibility()
        self._balancing = 0
        # ⚠️⚠️ 居中平衡必须挂**这一个** resizeEvent。
        #    本文件里原本有两个同名方法，Python 只认**最后定义**的那个 ——
        #    之前把 `_balance_sides()` 写在前面那个里，成了死代码，
        #    表现就是"算法明明是对的，但自动流程里 pad_l 永远是 0、恒定偏 39px"。
        self._balance_sides()

    def showEvent(self, ev):  # noqa: N802
        super().showEvent(ev)
        # 首次显示后控件才有真实尺寸，这里补一次
        QTimer.singleShot(0, self._balance_sides)
        self._sync_music_btn()               # ★ task-26③：显示时按当前模式对账按钮可见性
        self._apply_persisted_music()        # ★ task-26②：启动自举已持久化的音乐设置

    # -- 交互 -----------------------------------------------------------------
    def _toggle_mute(self):
        self.set_mute(not self._muted)
        self.mute_toggled.emit(self._muted)

    def _on_volume(self, value: float):
        if value > 0.5 and self._muted:
            self._muted = False
            self.btn_mute.set_checked(False)
        self._update_volume_icon(value, self._muted)
        self.volume_changed.emit(value)

    # ---- task-26③：音乐模式快捷入口 -----------------------------------------------------
    def _open_music_settings(self):
        """打开设置面板并**定位到「音乐与歌词」分区**（面板是窗口属性，走既有 API）。"""
        win = self.window()
        try:
            win.toggle_settings(True)
        except Exception:
            log.debug("打开设置面板失败", exc_info=True)
        try:
            panel = getattr(win, "settings", None)
            res = panel.show_section("music") if panel is not None else None
            self.last_music_locate = res
        except Exception:
            self.last_music_locate = None
            log.debug("定位音乐分区失败", exc_info=True)

    def _sync_music_btn(self):
        """★ task-26③：快捷按钮**仅音乐模式可见**。

        模式从窗口读（window()._kind），在既有的每帧 set_times / showEvent 里对账 ——
        **不新增定时器**（常驻 QTimer 必须保持 2）。
        """
        try:
            kind = str(getattr(self.window(), "_kind", "") or "")
        except Exception:
            kind = ""
        on = (kind == "audio")
        if on != self._music_btn_on:
            self._music_btn_on = on
            self.btn_music.setVisible(on)

    def music_button_state(self) -> dict:
        """★ task-26③ 锚点：快捷按钮的可读状态（QA 的三模式可见性矩阵直接读它）。"""
        try:
            kind = str(getattr(self.window(), "_kind", "") or "")
        except Exception:
            kind = ""
        return {"object_name": self.btn_music.objectName(),
                "visible": bool(self.btn_music.isVisible()),
                "mode_on": bool(self._music_btn_on), "kind": kind,
                "tooltip": self.btn_music.toolTip()}

    def _apply_persisted_music(self):
        """★ task-26②：启动时把已持久化的「音乐与歌词」设置施加一遍。

        为什么在控制栏做：设置面板是**懒创建**的（不打开就没有实例），而控制栏一定是
        启动路径上的控件 ⇒ 这里自举一次，保证"改完重启仍生效"无需先打开设置。
        """
        if getattr(self, "_music_boot_done", False):
            return
        self._music_boot_done = True
        win = self.window()
        try:
            panel = getattr(win, "settings", None)
            if panel is not None:
                panel.apply_all_music_settings()
                return
            cfg = getattr(win, "cfg", None)
            av = getattr(getattr(win, "stage", None), "audio_view", None)
            if cfg is None or av is None:
                return
            from .settings_panel import MUSIC_DEFAULTS, MUSIC_KEYS
            for k in MUSIC_KEYS:
                av.apply_music_setting(k, cfg.get(k, MUSIC_DEFAULTS.get(k)))
        except Exception:
            log.debug("启动自举音乐设置失败", exc_info=True)

    def _cycle_loop(self):
        keys = [k for k, _l, _i in LOOP_MODES]
        idx = (keys.index(self._loop_mode) + 1) % len(keys)
        self.set_loop_mode(keys[idx])
        self.loop_mode_changed.emit(self._loop_mode)

    def _show_speed_menu(self):
        menu = RoundedMenu(self)
        for sp in self._speeds:
            menu.add_row(f"{sp:g}x", "✓" if abs(sp - self._speed) < 1e-6 else "",
                         on_click=lambda s=sp: self._pick_speed(s))
        menu.addSeparator()
        menu.add_row("恢复正常速度", on_click=lambda: self._pick_speed(1.0))
        menu.exec(self.btn_speed.mapToGlobal(self.btn_speed.rect().bottomLeft()))

    def _pick_speed(self, sp: float):
        self.set_speed(sp)
        self.speed_changed.emit(sp)

    # -- 悬停预览 --------------------------------------------------------------
    def set_preview_enabled(self, on: bool):
        """总开关。关掉时**彻底停用**后台 Worker 与弹窗。"""
        self._preview_on = bool(on)
        if not self._preview_on:
            try:
                self._preview.hide()
                self._preview_timer.stop()
                if self._worker.isRunning():
                    self._worker.stop()
            except Exception:
                pass

    def set_preview_mode_ok(self, on: bool):
        """音乐模式传 False：不弹预览框、不抽帧（v1.0.28 ③）。"""
        self._preview_mode_ok = bool(on)
        if not on:
            try:
                self._preview.hide()
            except Exception:
                pass
            try:
                self._preview_timer.stop()
            except Exception:
                pass

    def set_preview_source(self, path: str):
        """告诉控制栏"现在放的是哪个文件"，并**预热**取帧句柄。

        ⚠️ 预热很重要：首次取帧要起解码器（100ms~1s），
        放在第一次悬停时做会明显卡一下。这里在换文件时就提前做掉。
        """
        if not getattr(self, "_preview_on", True):
            # 开关关掉时连句柄都不建（不启动 Worker、不起 ffmpeg）
            self._preview_path = ""
            return
        if path == self._preview_path:
            return
        # ⚠️ 换文件必须**先清掉旧文件的缩略图缓存**：
        #    不清的话内存会随"每轮打开一个文件"持续爬（实测 RSS 斜率 +3.87 MB/轮）。
        try:
            if self._worker._grabber is not None:
                self._worker._grabber.close()
        except Exception:
            pass
        self._preview_path = path or ""
        self._preview.hide()
        if not self._preview_path:
            return
        if not self._worker.isRunning():
            self._worker.start()          # 懒启动：只有真要预览时才起线程
        QTimer.singleShot(120, self._warm_preview)
        # 整条时间轴预铺（时长已知时）—— 这是"跟手"的关键
        QTimer.singleShot(400, self._start_prefetch)

    def _warm_preview(self):
        """预热：在后台把句柄建起来，第一次悬停就不会卡。"""
        if self._preview_path:
            self._worker.request(self._preview_path, 0.0)

    def _start_prefetch(self):
        """打开文件后只建句柄，**不再全量预铺**。

        全量预铺已废弃：几小时的电影铺 150 张既耗性能也不现实。
        改成"鼠标停在哪里就热哪里"的**局部辐射预热**（见 `_do_preview`）。
        """
        return

    def _on_hover(self, seconds: float):
        """鼠标在进度条上移动 —— **主线程内 0 延迟跟手**。

        ⚠⚠ 这里**绝对不允许**碰任何后台资源：
        弹窗位置、时间文字都必须立刻更新，
        图像等后台抽到了再替换。
        （旧实现把弹窗移动放在 `_on_frame` 里 ——
        后台慢就把整个预览钉在老位置，用户反馈“严重脱节”。）
        """
        if seconds < 0 or not self._preview_path or not getattr(self, "_preview_on", True):
            self._preview.hide()
            self._preview_timer.stop()
            return
        # ---- 1) 立刻跟手：位置 + 时间文字 ----
        self._preview.set_time(seconds)
        from PySide6.QtGui import QCursor

        gp = QCursor.pos()
        self._preview.move_centered(gp.x(), self.mapToGlobal(QPoint(0, 0)).y())
        if not self._preview.isVisible():
            # v1.0.29 ③：音乐模式绝不允许唤起悬停预览浮层（不实例化/不计算/不显示）
            if not getattr(self, "_preview_mode_ok", True):
                return
            self._preview.show()
        self._preview.raise_()
        # ---- 2) 零等待垫片：缓存里有邻近帧就先顶上 ----
        got = False
        if self._worker._grabber is not None:
            # 容差 4.0 -> 2.0：`-skip_frame nokey` 下 2~4 秒内命中同一个 GOP 关键帧是正常物理现象，
            # 但容差太大会一直复用旧帧、画面没层次；收到 2 秒促使后台及时拉新切片。
            img, ts = self._worker._grabber.nearest(self._preview_path, self._safe_time(seconds), tol=2.0)
            if img is not None:
                self._preview.set_image(img, ts)
                got = True
        if not got:
            # ⚠️⚠️ **严禁留死灰空框**（用户："鼠标放上去是一团毫无信息的死灰色"）。
            #    降级顺序：模糊的当前播放画面 -> "载入中"骨架。
            if self._placeholder is None and self._placeholder_fn is not None:
                try:
                    cur = self._placeholder_fn()
                    if cur is not None and self._worker._grabber is not None:
                        self._placeholder = self._worker._grabber.blur_placeholder(cur)
                except Exception:
                    self._placeholder = None
            if self._placeholder is not None:
                self._preview.set_image(self._placeholder, seconds)
        # ---- 3) 局部辐射预热（去抖后）----
        self._preview_timer.start()

    def _do_preview(self):
        if not getattr(self, "_preview_mode_ok", True):
            try:
                self._preview.hide()
            except Exception:
                pass
            return
        t = self.progress.hover_seconds()
        if not self._preview_path or t < 0:
            return
        t = self._safe_time(t)
        # 丢给后台线程，GUI 线程立刻返回
        self._worker.request(self._preview_path, t)
        # 鼠标停住超过去抖时间 -> 以该点为中心辐射预热前后各 2/4/6 秒
        self._worker.preheat(self._preview_path, t)

    def _safe_time(self, t: float) -> float:
        """把请求时间夹在视频有效范围内。

        ⚠⚠ 为什么必须夹：`-skip_frame nokey` + `-ss` 落在视频**尾部**时，
        ffmpeg 向后找不到下一个关键帧，直接触发 **EOF** 退出、
        不产出任何图像 —— UI 于是永远停在模糊垫片上（用户：
        "视频尾部无法更新，卡在模糊垫片"）。
        夹到 `时长 - 1.0` 就能保证定位到最后一个有效关键帧。
        """
        dur = float(getattr(self.progress, "_duration", 0.0) or 0.0)
        if dur <= 1.0:
            return max(0.0, t)
        return max(0.0, min(t, dur - 1.0))

    def _on_frame(self, img, seconds: float):
        """后台取到帧了 —— 只有鼠标还在进度条上、且**位置没怎么变**时才弹。

        ⚠⚠ 必须丢弃过期帧：抽帧要 135~376ms，鼠标早就挪走了。
        不判就会出现用户描述的“鼠标都挪到右边了，才刚显示老位置的预览”。
        """
        if img is None:
            return
        cur = self.progress.hover_seconds()
        if cur < 0:
            return
        if abs(cur - seconds) > 1.2:
            # 已经过期：直接丢掉，并立刻按当前位置重新要一张
            self._worker.request(self._preview_path, cur)
            return
        # 只换图；位置与时间文字由 _on_hover 负责（已经跟手了）
        self._preview.set_image(img, seconds)

    def _shutdown_preview(self):
        """退出前收尾。

        ⚠️⚠️ **不能靠 `closeEvent`** —— `ControlBar` 是子控件，
        父窗口关闭时 Qt **不会**给子控件发 closeEvent（实测完全没被调用），
        于是后台线程还在跑，QThread 被析构 → `CRITICAL qt: QThread: Destroyed
        while thread is still running` + 退出码 0xC0000409。
        正确做法是挂 `QApplication.aboutToQuit`。
        """
        try:
            self._preview.hide()
        except Exception:
            pass
        try:
            self._worker.stop()
        except Exception:
            pass
