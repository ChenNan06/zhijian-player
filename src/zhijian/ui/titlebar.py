"""自定义标题栏。

几何取自对爱奇艺万能联播 5.4.1 的逆向测量：标题栏高 44px、左内边距 12、
logo 26x26、窗口按钮 44x32、按钮之间 0 间距；右侧还有功能图标区（用竖线分隔）。
现代化处理：底色改为通透冷灰而非死黑, 分隔线改成极细 hairline。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from . import icons
from .theme import M, P, css
from .widgets import IconButton, Spacer


class TitleBar(QWidget):
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

        lay = QHBoxLayout(self)
        lay.setContentsMargins(M["titlebar_pad_l"], 0, M["titlebar_pad_r"], 0)
        lay.setSpacing(M["title_gap"])

        # ---- 左：应用标识 + 下拉菜单 ----
        self.logo = IconButton("playlist", self, size=M["logo_size"], icon_size=16,
                               tooltip="主菜单", color=P["accent"], hover_color=P["accent"],
                               stroke=2.0)
        self.logo.clicked.connect(lambda: self.menu_clicked.emit(self.logo))
        self._logo_pill = QWidget(self)
        self._logo_pill.setFixedSize(M["logo_size"], M["logo_size"])
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
        self.btn_settings = IconButton("settings", self, tooltip="设置 (Ctrl+,)")
        self.btn_settings.clicked.connect(self.settings_clicked.emit)
        # 日夜一键切换：图标显示"点一下会变成什么" —— 深色时显示太阳、浅色时显示月亮。
        self.btn_theme = IconButton("sun", self, tooltip="切换深色 / 浅色")
        self.btn_theme.clicked.connect(self.theme_toggled.emit)
        self.btn_menu = IconButton("more", self, tooltip="更多")
        self.btn_menu.clicked.connect(lambda: self.menu_clicked.emit(self.btn_menu))

        for b in (self.btn_theme, self.btn_settings, self.btn_menu):
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

        self.btn_min = IconButton("min", self, size=M["title_btn_w"], radius=6,
                                  icon_size=16, tooltip="最小化")
        self.btn_max = IconButton("max", self, size=M["title_btn_w"], radius=6,
                                  icon_size=15, tooltip="最大化")
        self.btn_close = IconButton("close", self, size=M["title_btn_w"], radius=6,
                                    icon_size=16, tooltip="关闭", hover_color=P["danger"])
        for b, sig in ((self.btn_min, self.minimize_clicked),
                       (self.btn_max, self.maximize_clicked),
                       (self.btn_close, self.close_clicked)):
            b.setFixedHeight(M["title_btn_h"])
            b.clicked.connect(sig.emit)
            lay.addWidget(b)

        self.setMouseTracking(True)

    # -- 对外 ----------------------------------------------------------------
    def set_title(self, text: str):
        self.title_label.setText(text or "至简Player")
        self.title_label.setToolTip(text or "")

    def set_light(self, light: bool):
        """按当前明暗换图标：深色时显示太阳（点一下变浅色），浅色时显示月亮。"""
        self.btn_theme.set_icon("moon" if light else "sun")
        self.btn_theme.setToolTip("切换到浅色（霜白液态玻璃）" if not light else "切换到深色")

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

    def paintEvent(self, _ev):
        from PySide6.QtGui import QColor, QPainter

        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 0))
        p.end()
