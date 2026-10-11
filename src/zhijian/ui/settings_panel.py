"""设置面板（主窗口内的浮层, 不是独立顶层窗口）。

逆向依据（爱奇艺 5.4.1 设置窗口 854x685 @1299x883）：左侧分类栏宽 153（占 18%）、
右侧内容区左右内边距 25、行高 40、分组标题 15px。
本实现按同一比例放大到 880x660，并全部使用 QFormLayout / QVBoxLayout，**零绝对坐标**。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
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
                      NoWheelSlider, NoWheelSpinBox, hline, round_popup)

SECTIONS = [
    ("general", "常规", "sliders"),
    ("playback", "播放", "play"),
    ("subtitle", "字幕", "subtitle"),
    ("ui", "界面", "palette"),
    ("audio", "声音", "speaker"),
    ("hotkeys", "快捷键", "keyboard"),
    ("about", "关于", "info"),
]

HOTKEY_LABELS = [
    ("play_pause", "播放 / 暂停"),
    ("seek_fwd_small", "快进 5 秒（长按 → 倍速）"),
    ("seek_back_small", "快退 5 秒（长按 ← 倍速）"),
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

    def __init__(self, action: str, parent=None):
        super().__init__(parent)
        self.action = action
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self._waiting = False

    def set_waiting(self, on: bool):
        self._waiting = on
        self.setText("请按下新的按键…" if on else self._current_text)
        self.setStyleSheet(f"color:{P['accent']};" if on else "")

    def set_current_text(self, text: str):
        self._current_text = text
        if not self._waiting:
            self.setText(text)

    def mousePressEvent(self, ev):
        self._waiting = True
        self.set_waiting(True)
        self.capturing.emit(self)
        super().mousePressEvent(ev)


class SettingsPanel(GlassFrame):
    """一体化设置浮层：左分类 + 右内容 + 底按钮, 全部流式布局。"""

    changed = Signal(str, object)        # key, value
    applied = Signal(str)                # 点了「应用」/「确定」后的提示文案（主窗口拿去弹 OSD）
    hotkey_changed = Signal(str, str)
    closed = Signal()
    reset_requested = Signal()
    theme_requested = Signal(str)        # 换肤：主题名
    setting_changed = Signal(str, object)  # 任一设置项变化（用于"即时生效"）

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
        self.page_title = _title("常规")
        top.addWidget(self.page_title)
        top.addStretch(1)
        btn_close = IconButton("close", right, size=28, icon_size=15, tooltip="关闭 (Esc)")
        btn_close.clicked.connect(self._close)
        top.addWidget(btn_close)
        rv.addLayout(top)
        rv.addSpacing(6)

        self.stack = QStackedWidget(right)
        self.stack.addWidget(self._page_general())
        self.stack.addWidget(self._page_playback())
        self.stack.addWidget(self._page_subtitle())
        self.stack.addWidget(self._page_ui())
        self.stack.addWidget(self._page_audio())
        self.stack.addWidget(self._page_hotkeys())
        self.stack.addWidget(self._page_about())
        rv.addWidget(self.stack, 1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(M["settings_pad"], 6, M["settings_pad"], 14)
        bottom.setSpacing(10)
        self.btn_reset = QPushButton("恢复默认设置")
        self.btn_reset.setProperty("role", "link")
        self.btn_reset.setCursor(Qt.PointingHandCursor)
        self.btn_reset.clicked.connect(self._on_reset)
        bottom.addWidget(self.btn_reset)
        bottom.addStretch(1)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setProperty("role", "primary")
        self.btn_ok.setCursor(Qt.PointingHandCursor)
        self.btn_ok.clicked.connect(self._on_ok)
        self.btn_apply = QPushButton("应用")
        self.btn_apply.setProperty("role", "ghost")
        self.btn_apply.setCursor(Qt.PointingHandCursor)
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setProperty("role", "ghost")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.clicked.connect(self._close)
        for b in (self.btn_ok, self.btn_apply, self.btn_cancel):
            b.setFixedWidth(92)
            b.setFixedHeight(36)
            bottom.addWidget(b)
        rv.addLayout(bottom)
        body.addWidget(right, 1)

        self.body_layout().addLayout(body)
        self.nav.setCurrentRow(0)
        self._dirty: dict = {}
        # 「应用」按钮的反馈计时器（见 _flash_apply_btn）
        self._apply_flash = QTimer(self)
        self._apply_flash.setSingleShot(True)
        self._apply_flash.timeout.connect(self._restore_apply_btn)

    # ------------------------------------------------------------------ 页面
    @staticmethod
    def _scroll(inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(inner)
        return sa

    def _form_host(self) -> tuple[QWidget, QVBoxLayout]:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(M["settings_pad"], 4, M["settings_pad"], 18)
        lay.setSpacing(12)   # 卡片之间 12px（QQ NT 规范）
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

        card = QFrame()
        card.setObjectName("SettingsCard")
        # ⚠️ 卡片要和面板底**拉开明度差**才看得出是卡片（用户连续三轮说"没卡片化"）。
        #    深色下卡片比面板亮一档、浅色下比面板白，边框用 divider 的 1.5 倍浓度。
        card.setStyleSheet(
            f"QFrame#SettingsCard{{background:{css(P['glass_card'])};"
            f"border:1px solid {css(P['hairline_strong'])};border-radius:10px;}}")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 16)
        v.setSpacing(10)
        if title:
            v.addWidget(_title(title))
        v.addLayout(form)
        return card

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
        f.addRow(self._check("playback.remember_position", "记忆播放进度"))
        f.addRow(self._check("playback.auto_play", "打开后自动播放"))
        lay.addWidget(self._card("", f))

        f2 = self._form()
        f2.addRow("默认循环方式", self._combo("playback.loop_mode", [
            ("sequence", "顺序播放"), ("loop", "列表循环"),
            ("single", "单个循环"), ("random", "随机播放"),
        ], "sequence"))
        f2.addRow("图片自动播放间隔", self._spin("playback.image_duration", 0, 120, 1, " 秒"))
        f2.addRow(_sub("0 秒表示不自动切换。"))
        lay.addWidget(self._card("启动行为", f2))
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
        lay.addWidget(self._card("长按倍速", f))

        f2 = self._form()
        f2.addRow("短跳步长", self._spin("playback.seek_step_small", 1, 120, 1, " 秒"))
        f2.addRow("长跳步长", self._spin("playback.seek_step_large", 5, 600, 5, " 秒"))
        f2.addRow("Shift 跳步长", self._spin("playback.seek_step_shift", 1, 120, 1, " 秒"))
        f2.addRow(_sub("Shift+方向键一次跳这么多，按住会连跳。"))
        f2.addRow("音量步长", self._spin("playback.volume_step", 1, 25, 1, " %"))
        f2.addRow("倍速步长", self._dspin("playback.speed_step", 0.05, 1.0, 0.05, " x"))
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
        f3.addRow("预读缓存", self._spin("playback.cache_secs", 0, 600, 10, " 秒"))
        f3.addRow(_sub("改动硬件解码方式需要重新打开文件才生效。"))
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
        # ⚠️ 这里**故意不列**内置的 HarmonyOS Sans SC：它只注册进了 Qt 的字体库，
        #    mpv 的字幕渲染看不到它（那是另一套字体查找）—— 列上去就是个"选了没反应"的死选项。
        #    要让字幕也用内置字体，得给 mpv 设 `sub-fonts-dir`（未做，见 HANDOVER §7.2）。
        fam.addItems(["Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun",
                      "Source Han Sans SC", "Noto Sans CJK SC", "Arial", "Segoe UI"])
        fam.setCurrentText(str(self.cfg.get("subtitle.font_family", "Microsoft YaHei UI")))
        fam.setMinimumWidth(220)
        fam.setMinimumHeight(30)
        fam.currentTextChanged.connect(lambda t: self._set("subtitle.font_family", t))
        f.addRow("字体", fam)
        f.addRow("字号", self._spin("subtitle.font_size", 12, 160, 1, " px"))
        f.addRow("垂直位置", self._spin("subtitle.position", 0, 100, 1, " %"))
        f.addRow("描边粗细", self._dspin("subtitle.border_size", 0.0, 8.0, 0.2, " px"))
        lay.addWidget(self._card("字幕", f))

        f2 = self._form()
        f2.addRow("文字颜色", self._color_edit("subtitle.text_color"))
        f2.addRow("描边颜色", self._color_edit("subtitle.border_color"))
        lay.addWidget(self._card("配色", f2))

        f3 = self._form()
        f3.addRow("微调步长", self._dspin("subtitle.step", 0.05, 1.0, 0.05, " 秒"))
        f3.addRow("当前偏移", self._dspin("subtitle.delay", -30.0, 30.0, 0.1, " 秒"))
        f3.addRow(_sub("也可以在播放界面按 V 打开字幕浮层, 用 ◀ ▶ 按钮以该步长微调。"))
        lay.addWidget(self._card("时间轴微调", f3))

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
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        sw = QLabel()
        sw.setFixedSize(26, 26)

        def repaint(color: str):
            from .theme import qt_color

            c = qt_color(color)
            sw.setStyleSheet(f"background:{c.name()};border:1px solid {css(P['hairline_strong'])};"
                             f"border-radius:6px;")

        ed = QLineEdit(str(self.cfg.get(key, "#FFFFFF")))
        ed.setFixedWidth(110)
        ed.setMaxLength(9)
        ed.textChanged.connect(lambda t, k=key: (repaint(t), self._set(k, t)))
        repaint(ed.text())
        h.addWidget(sw)
        h.addWidget(ed)
        h.addStretch(1)
        return w

    # ---- 界面 ----
    def _page_ui(self) -> QWidget:
        host, lay = self._form_host()
        f = self._form()
        f.addRow("强调色", self._color_edit("ui.accent_override") if False else self._accent_combo())
        f.addRow("播放列表宽度", self._slider_row("ui.drawer_width", 280, 560, lambda v: f"{v} px"))
        f.addRow("控制条自动隐藏延时", self._slider_row("ui.capsule_autohide_ms", 800, 8000, lambda v: f"{v} ms"))
        lay.addWidget(self._card("外观", f))

        f2 = self._form()
        f2.addRow("默认缩放", self._combo("ui.image_fit", [
            ("fit", "适应窗口"), ("one", "原始大小 100%"), ("free", "上次的缩放比例"),
        ], "fit"))
        f2.addRow("滚轮缩放步长", self._dspin("ui.image_zoom_step", 1.02, 1.5, 0.01, " x"))
        lay.addWidget(self._card("图片查看", f2))

        f3 = self._form()
        start_w = self._spin("ui.window_w", 640, 7680, 10, " px")
        start_h = self._spin("ui.window_h", 400, 4320, 10, " px")
        f3.addRow("初始窗口宽度", start_w)
        f3.addRow("初始窗口高度", start_h)
        f3.addRow(self._check("ui.confirm_exit", "关闭窗口时二次确认"))
        lay.addWidget(self._card("窗口", f3))
        lay.addStretch(1)
        return self._scroll(host)

    #: 只保留「浅色 / 深色」两个模式（用户："把界面里面的强调色给去掉，
    #  就只保留深浅色模式"）。七个色板对播放器来说是多余的噪音。
    THEME_MODES = (("qq-light", "浅色"), ("qq-dark", "深色"))

    def _accent_combo(self) -> QWidget:
        """外观模式：**浅色 / 深色** 两个按钮。

        用户反馈"把强调色去掉，只保留深浅色模式" —— 原来七个色板按钮是多余的。
        这里保留"点了真的会换肤"的行为，只是把可选项收敛成两个。
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
        # 当前若是别的主题名（历史配置），按明暗归一到两个模式之一
        if cur not in dict(self.THEME_MODES):
            cur = "qq-light" if _theme.is_light() else "qq-dark"
        for name, label in self.THEME_MODES:
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

    def _pick_theme(self, name: str):
        for n, b in getattr(self, "_theme_buttons", {}).items():
            b.setChecked(n == name)
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
        self.stack.addWidget(self._page_general())
        self.stack.addWidget(self._page_playback())
        self.stack.addWidget(self._page_subtitle())
        self.stack.addWidget(self._page_ui())
        self.stack.addWidget(self._page_audio())
        self.stack.addWidget(self._page_hotkeys())
        self.stack.addWidget(self._page_about())
        self.stack.setCurrentIndex(max(0, min(cur, self.stack.count() - 1)))
        _ = _theme  # 保留引用，便于以后在这里做主题相关的微调

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
    def _page_hotkeys(self) -> QWidget:
        host, lay = self._form_host()
        lay.addWidget(_title("快捷键"))
        lay.addWidget(_sub("点击右侧输入框后直接按下新按键即可修改。修改立即生效并写入配置文件。"))
        self._key_edits: dict[str, KeyCaptureEdit] = {}
        f = self._form()
        for action, label in HOTKEY_LABELS:
            keys = self.cfg.get(f"hotkeys.{action}", []) or []
            ed = KeyCaptureEdit(action)
            ed.set_current_text(" / ".join(keys) if keys else "（未绑定）")
            ed.setFixedWidth(230)
            ed.setMinimumHeight(30)
            ed.capturing.connect(self._begin_capture)
            self._key_edits[action] = ed
            f.addRow(label, ed)
        lay.addLayout(f)
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
        for other in self._key_edits.values():
            if other is not edit and other._waiting:
                other.set_waiting(False)
        edit.set_waiting(True)

    def active_capture(self) -> KeyCaptureEdit | None:
        for ed in getattr(self, "_key_edits", {}).values():
            if ed._waiting:
                return ed
        return None

    def apply_captured_key(self, action: str, spec: str):
        ed = self._key_edits.get(action)
        if ed is None:
            return
        ed._waiting = False
        ed.set_current_text(spec or "（未绑定）")
        self.hotkey_changed.emit(action, spec)

    def refresh_hotkeys(self):
        for action, ed in getattr(self, "_key_edits", {}).items():
            keys = self.cfg.get(f"hotkeys.{action}", []) or []
            ed.set_current_text(" / ".join(keys) if keys else "（未绑定）")

    def _reset_hotkeys(self):
        from ..config import DEFAULTS

        self.cfg.data["hotkeys"] = dict(DEFAULTS["hotkeys"])
        self.refresh_hotkeys()
        self.hotkey_changed.emit("*", "")

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

    def _on_nav(self, row: int):
        if 0 <= row < len(SECTIONS):
            self.page_title.setText(SECTIONS[row][1])
            self.stack.setCurrentIndex(row)

    def _set(self, key: str, value):
        self._dirty[key] = value
        self.cfg.set(key, value)
        self.cfg.save()
        # 让主窗口能对特定项做"即时生效"（用户："开关设置不能即时生效"）
        self.setting_changed.emit(key, value)

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
        self.btn_apply.setText("✓ 已应用" if had else "✓ 已是最新")
        self.btn_apply.setProperty("role", "primary")
        self._repolish(self.btn_apply)
        self._apply_flash.start(1600)

    def _restore_apply_btn(self):
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
        self.cfg.reset_all()
        self.cfg.save()
        self.reset_requested.emit()
        self.refresh_hotkeys()
