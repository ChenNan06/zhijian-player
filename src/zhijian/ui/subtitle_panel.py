"""字幕自定义浮层。

用户要求：可调节**字体、字号、边框颜色、垂直位置偏移及时间轴微调（±0.5s）**,
并且**杜绝 PotPlayer 那种千项级联菜单** —— 所以这里是一个 4 行的紧凑卡片,
所有改动即时生效、即时预览。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .theme import M, P, css, qt_color
from .widgets import GlassFrame, IconButton, NoWheelComboBox, TextIconButton, hline

FONT_CHOICES = [
    "Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun", "KaiTi",
    "Source Han Sans SC", "Noto Sans CJK SC", "DengXian", "Arial", "Segoe UI", "Consolas",
]
BORDER_SWATCHES = ["#101010", "#000000", "#FFFFFF", "#2FD3C0", "#3A3F4B", "#5A2D82", "#8B2E2E", "#1E3A5F"]


class _Swatch(QWidget):
    picked = Signal(str)

    def __init__(self, color: str, parent=None, size: int = 20, active: bool = False):
        super().__init__(parent)
        self.color = color
        self._active = active
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setToolTip(color)

    def set_active(self, on: bool):
        self._active = on
        self.update()

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        self.picked.emit(self.color)
        super().mousePressEvent(ev)

    def paintEvent(self, _ev):
        from PySide6.QtGui import QPainter, QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.setBrush(qt_color(self.color))
        pen = QPen(qt_color(P["accent"] if self._active else (
            P["text_dim"] if self._hover else P["hairline_strong"])))
        pen.setWidthF(2.0 if self._active else 1.0)
        p.setPen(pen)
        p.drawRoundedRect(r, 6, 6)
        p.end()


class SubtitlePanel(GlassFrame):
    """字幕浮层（主窗口内的浮层, 不是独立窗口）。"""

    font_changed = Signal(str)
    size_changed = Signal(int)
    border_color_changed = Signal(str)
    position_changed = Signal(int)
    delay_changed = Signal(float)
    track_changed = Signal(int)          # sid, -1 = 关闭
    visibility_changed = Signal(bool)
    closed = Signal()
    open_file_requested = Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(
            parent, radius=M["radius_lg"], fill=P["glass_panel"], border=P["hairline_strong"],
            shadow=0.0, shadow_spread=26, pad=(16, 12, 16, 14),
        )
        self.cfg = cfg
        self.setObjectName("SubtitlePanel")
        self.set_body_width(392)

        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(9)

        # 标题行
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        t = QLabel("字幕")
        f = QFont(t.font())
        f.setPixelSize(14)
        f.setWeight(QFont.Medium)
        t.setFont(f)
        head.addWidget(t)
        head.addStretch(1)
        self.btn_visible = IconButton("subtitle", self, size=26, icon_size=15,
                                      tooltip="显示/隐藏字幕 (V)", checkable=True, checked=True)
        self.btn_visible.toggled.connect(self.visibility_changed.emit)
        self.btn_close = IconButton("close", self, size=26, icon_size=14, tooltip="关闭")
        self.btn_close.clicked.connect(self.closed.emit)
        head.addWidget(self.btn_visible)
        head.addWidget(self.btn_close)
        root.addLayout(head)
        root.addWidget(hline())

        # 轨道
        f0 = QFormLayout()
        f0.setHorizontalSpacing(14)
        f0.setVerticalSpacing(9)
        f0.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        f0.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.track_combo = NoWheelComboBox()
        self.track_combo.setMinimumHeight(30)
        self.track_combo.currentIndexChanged.connect(
            lambda i: self.track_changed.emit(int(self.track_combo.itemData(i))))
        row_track = QWidget()
        rt = QHBoxLayout(row_track)
        rt.setContentsMargins(0, 0, 0, 0)
        rt.setSpacing(8)
        rt.addWidget(self.track_combo, 1)
        self.btn_load = IconButton("folder", self, size=30, icon_size=16, tooltip="加载外挂字幕…")
        self.btn_load.clicked.connect(self.open_file_requested.emit)
        rt.addWidget(self.btn_load)
        f0.addRow("字幕轨道", row_track)
        root.addLayout(f0)

        # 字体 / 字号
        f1 = QFormLayout()
        f1.setHorizontalSpacing(14)
        f1.setVerticalSpacing(9)
        f1.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        f1.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.font_combo = NoWheelComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.addItems(FONT_CHOICES)
        self.font_combo.setCurrentText(str(cfg.get("subtitle.font_family", "Microsoft YaHei UI")))
        self.font_combo.setMinimumHeight(30)
        self.font_combo.currentTextChanged.connect(self.font_changed.emit)
        f1.addRow("字体", self.font_combo)

        size_row = QWidget()
        sr = QHBoxLayout(size_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(6)
        self.btn_size_down = IconButton("minus", self, size=28, icon_size=14, tooltip="减小字号")
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(14, 120)
        self.size_slider.setValue(int(cfg.get("subtitle.font_size", 46)))
        self.size_label = QLabel(str(self.size_slider.value()))
        self.size_label.setFixedWidth(34)
        self.size_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.size_label.setStyleSheet(f"color:{P['text_dim']};")
        self.btn_size_up = IconButton("plus", self, size=28, icon_size=14, tooltip="增大字号")
        self.btn_size_down.clicked.connect(lambda: self.size_slider.setValue(self.size_slider.value() - 2))
        self.btn_size_up.clicked.connect(lambda: self.size_slider.setValue(self.size_slider.value() + 2))
        self.size_slider.valueChanged.connect(self._on_size)
        for w in (self.btn_size_down, self.size_slider, self.size_label, self.btn_size_up):
            sr.addWidget(w)
        sr.setStretch(1, 1)
        f1.addRow("字号", size_row)
        root.addLayout(f1)

        # 边框颜色
        bcol = QWidget()
        bl = QVBoxLayout(bcol)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        sw_row = QHBoxLayout()
        sw_row.setContentsMargins(0, 0, 0, 0)
        sw_row.setSpacing(6)
        self._swatches: list[_Swatch] = []
        cur_border = str(cfg.get("subtitle.border_color", "#101010")).upper()
        for c in BORDER_SWATCHES:
            sw = _Swatch(c, active=c.upper() == cur_border)
            sw.picked.connect(self._pick_border)
            self._swatches.append(sw)
            sw_row.addWidget(sw)
        sw_row.addStretch(1)
        more = TextIconButton("更多…", self, height=22, pad=9, font_size=11, tooltip="自定义描边颜色")
        more.clicked.connect(self._custom_border)
        sw_row.addWidget(more)
        bl.addLayout(sw_row)

        stroke_row = QWidget()
        stl = QHBoxLayout(stroke_row)
        stl.setContentsMargins(0, 0, 0, 0)
        stl.setSpacing(8)
        lbl = QLabel("描边粗细")
        lbl.setStyleSheet(f"color:{P['text_dim']};")
        self.stroke_slider = QSlider(Qt.Horizontal)
        self.stroke_slider.setRange(0, 60)
        self.stroke_slider.setValue(int(float(cfg.get("subtitle.border_size", 2.4)) * 10))
        self.stroke_label = QLabel(f"{float(cfg.get('subtitle.border_size', 2.4)):.1f}")
        self.stroke_label.setFixedWidth(28)
        self.stroke_label.setStyleSheet(f"color:{P['text_muted']};font-size:12px;")
        self.stroke_slider.valueChanged.connect(self._on_stroke)
        stl.addWidget(lbl)
        stl.addWidget(self.stroke_slider, 1)
        stl.addWidget(self.stroke_label)
        bl.addWidget(stroke_row)
        root.addWidget(bcol)

        # 垂直位置
        pos_row = QWidget()
        pl = QHBoxLayout(pos_row)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)
        plb = QLabel("垂直位置")
        plb.setStyleSheet(f"color:{P['text_dim']};")
        self.pos_slider = QSlider(Qt.Horizontal)
        self.pos_slider.setRange(0, 100)
        self.pos_slider.setInvertedAppearance(True)
        self.pos_slider.setValue(int(cfg.get("subtitle.position", 92)))
        self.pos_label = QLabel(f"{self.pos_slider.value()}%")
        self.pos_label.setFixedWidth(38)
        self.pos_label.setStyleSheet(f"color:{P['text_muted']};font-size:12px;")
        self.pos_slider.valueChanged.connect(self._on_pos)
        pl.addWidget(plb)
        pl.addWidget(self.pos_slider, 1)
        pl.addWidget(self.pos_label)
        root.addWidget(pos_row)

        root.addWidget(hline())

        # 时间轴微调
        delay_title = QHBoxLayout()
        dt = QLabel("时间轴微调")
        dt.setStyleSheet(f"color:{P['text_dim']};")
        delay_title.addWidget(dt)
        delay_title.addStretch(1)
        self.step_combo = NoWheelComboBox()
        for v in (0.05, 0.1, 0.25, 0.5):
            self.step_combo.addItem(f"±{v:g}s", v)
        self.step_combo.setCurrentIndex(1)
        self.step_combo.setFixedWidth(78)
        self.step_combo.setMinimumHeight(26)
        delay_title.addWidget(self.step_combo)
        root.addLayout(delay_title)

        drow = QHBoxLayout()
        drow.setContentsMargins(0, 0, 0, 0)
        drow.setSpacing(6)
        self.btn_delay_back = TextIconButton("◀ 提前", self, height=30, pad=12, font_size=12,
                                             tooltip="字幕提前（更快出现）")
        self.btn_delay_fwd = TextIconButton("延后 ▶", self, height=30, pad=12, font_size=12,
                                            tooltip="字幕延后")
        self.delay_value = QLabel("0.00 s")
        self.delay_value.setAlignment(Qt.AlignCenter)
        self.delay_value.setStyleSheet(
            f"color:{P['accent']};font-size:13px;font-weight:500;"
            f"background:{css(P['accent_soft'])};border-radius:8px;padding:5px 0;")
        self.delay_value.setMinimumWidth(84)
        self.btn_delay_back.clicked.connect(lambda: self._nudge_delay(-1))
        self.btn_delay_fwd.clicked.connect(lambda: self._nudge_delay(1))
        btn_reset = TextIconButton("归零", self, height=30, pad=11, font_size=12, tooltip="把偏移恢复为 0")
        btn_reset.clicked.connect(lambda: self.set_delay(0.0, emit=True))
        drow.addWidget(self.btn_delay_back)
        drow.addWidget(self.delay_value, 1)
        drow.addWidget(self.btn_delay_fwd)
        drow.addWidget(btn_reset)
        root.addLayout(drow)

        self.set_delay(float(cfg.get("subtitle.delay", 0.0)), emit=False)
        self.body_layout().addLayout(root)

    # -- 内部 ----------------------------------------------------------------
    def _on_size(self, v: int):
        self.size_label.setText(str(v))
        self.size_changed.emit(int(v))

    def _on_stroke(self, v: int):
        self.stroke_label.setText(f"{v/10:.1f}")
        self.border_color_changed.emit("*stroke*")

    def _on_pos(self, v: int):
        self.pos_label.setText(f"{v}%")
        self.position_changed.emit(int(v))

    def _pick_border(self, color: str):
        for sw in self._swatches:
            sw.set_active(sw.color.upper() == color.upper())
        self.border_color_changed.emit(color)

    def _custom_border(self):
        cur = qt_color(str(self.cfg.get("subtitle.border_color", "#101010")))
        c = QColorDialog.getColor(cur, self, "选择描边颜色")
        if c.isValid():
            self._pick_border(c.name().upper())

    def _nudge_delay(self, sign: int):
        step = float(self.step_combo.currentData() or 0.1)
        self.set_delay(float(self.cfg.get("subtitle.delay", 0.0)) + sign * step, emit=True)

    def set_delay(self, value: float, emit: bool = True):
        value = round(max(-60.0, min(60.0, float(value))), 3)
        self.cfg.set("subtitle.delay", value)
        self.delay_value.setText(f"{value:+.2f} s")
        if emit:
            self.delay_changed.emit(value)

    def set_tracks(self, tracks: list[tuple[int, str]], current_sid: int, visible: bool):
        self.track_combo.blockSignals(True)
        self.track_combo.clear()
        self.track_combo.addItem("关闭字幕", -1)
        for sid, label in tracks:
            self.track_combo.addItem(label, sid)
        idx = self.track_combo.findData(current_sid)
        self.track_combo.setCurrentIndex(max(0, idx))
        self.track_combo.blockSignals(False)
        self.btn_visible.set_checked(visible, emit=False)

    def sync_from_config(self):
        self.font_combo.setCurrentText(str(self.cfg.get("subtitle.font_family", "Microsoft YaHei UI")))
        self.size_slider.setValue(int(self.cfg.get("subtitle.font_size", 46)))
        self.pos_slider.setValue(int(self.cfg.get("subtitle.position", 92)))
        self.stroke_slider.setValue(int(float(self.cfg.get("subtitle.border_size", 2.4)) * 10))
        self.set_delay(float(self.cfg.get("subtitle.delay", 0.0)), emit=False)
        cur = str(self.cfg.get("subtitle.border_color", "#101010")).upper()
        for sw in self._swatches:
            sw.set_active(sw.color.upper() == cur)
