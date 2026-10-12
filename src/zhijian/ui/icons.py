"""内联 SVG 图标集。

⚠️ QSvgRenderer **不认识** `#RRGGBBAA`（写成 8 位十六进制会解析失败 → 整条 path 一个像素
都不画, 表现是"图标有的地方画有的地方不画"）。所以这里只注入纯 `#RRGGBB`,
需要透明度时用 QPainter.setOpacity()。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .theme import P

# 24x24 viewBox, stroke 风格
_STROKE: dict[str, str] = {
    # ---- 标题栏 ----
    "min": '<path d="M5 12h14"/>',
    "max": '<rect x="5" y="5" width="14" height="14" rx="2.5"/>',
    "restore": '<rect x="4" y="8" width="12" height="12" rx="2.5"/><path d="M8 8V6.5A2.5 2.5 0 0 1 10.5 4H18a2.5 2.5 0 0 1 2.5 2.5V13a2.5 2.5 0 0 1-2.5 2.5h-1"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "pin": '<path d="M9 4h6l-.8 5.2 3.3 3.3H6.5l3.3-3.3z"/><path d="M12 12.5V20"/>',
    "chevron_down": '<path d="M6 9.5l6 6 6-6"/>',
    "chevron_right": '<path d="M9.5 6l6 6-6 6"/>',
    "chevron_left": '<path d="M14.5 6l-6 6 6 6"/>',
    "chevron_up": '<path d="M6 14.5l6-6 6 6"/>',

    # ---- 播放控制 ----
    "play": '<path d="M8 5.2l10.5 6.8L8 18.8z" fill="CURRENT" stroke="none"/>',
    "pause": '<rect x="7.5" y="5.5" width="3.4" height="13" rx="1.4" fill="CURRENT" stroke="none"/><rect x="13.1" y="5.5" width="3.4" height="13" rx="1.4" fill="CURRENT" stroke="none"/>',
    "stop": '<rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="CURRENT" stroke="none"/>',
    "prev": '<path d="M7 6v12"/><path d="M18.5 5.6L9.8 12l8.7 6.4z" fill="CURRENT" stroke="none"/>',
    "next": '<path d="M17 6v12"/><path d="M5.5 5.6L14.2 12l-8.7 6.4z" fill="CURRENT" stroke="none"/>',
    "forward": '<path d="M4 6.5l7 5.5-7 5.5z" fill="CURRENT" stroke="none"/><path d="M12 6.5l7 5.5-7 5.5z" fill="CURRENT" stroke="none"/>',
    "rewind": '<path d="M20 6.5l-7 5.5 7 5.5z" fill="CURRENT" stroke="none"/><path d="M12 6.5l-7 5.5 7 5.5z" fill="CURRENT" stroke="none"/>',
    "skip_start": '<path d="M6 6v12"/><path d="M18 6l-8 6 8 6z" fill="CURRENT" stroke="none"/>',
    "skip_end": '<path d="M18 6v12"/><path d="M6 6l8 6-8 6z" fill="CURRENT" stroke="none"/>',

    # ---- 音量 ----
    "volume_high": '<path d="M4 9.5h3.2L12 5.6v12.8L7.2 14.5H4z" fill="CURRENT" stroke="none"/><path d="M15.4 9a4.2 4.2 0 0 1 0 6"/><path d="M18 6.6a7.6 7.6 0 0 1 0 10.8"/>',
    "volume_low": '<path d="M4 9.5h3.2L12 5.6v12.8L7.2 14.5H4z" fill="CURRENT" stroke="none"/><path d="M15.4 9a4.2 4.2 0 0 1 0 6"/>',
    "volume_mute": '<path d="M4 9.5h3.2L12 5.6v12.8L7.2 14.5H4z" fill="CURRENT" stroke="none"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',

    # ---- 功能 ----
    "speed": '<path d="M12 4.5a8.5 8.5 0 1 1-8.5 8.5"/><path d="M12 12l4.6-4.6"/><circle cx="12" cy="12" r="1.4" fill="CURRENT" stroke="none"/>',
    "subtitle": '<rect x="3.5" y="5" width="17" height="14" rx="3"/><path d="M7 14h4M13 14h4M7 10.5h3.5M13.5 10.5H17"/>',
    # 六角螺母 + 内圈 + 六根短齿：小尺寸下比"圆形 + 放射线"更像齿轮。
    # ⚠️ 旧的 settings 图标是 `<circle> + 六条放射线>`，用户一眼就说是"日间/夜间模式切换"，
    #    那个形状确实就是亮度图标。这里换成真正的齿轮轮廓。
    "settings": '<circle cx="12" cy="12" r="3.1"/>'
                '<path d="M12 4.6l6.4 3.7v7.4L12 19.4l-6.4-3.7V8.3z"/>'
                '<path d="M12 4.6V2.6M18.4 8.3l1.7-1M18.4 15.7l1.7 1'
                'M12 19.4v2M5.6 15.7l-1.7 1M5.6 8.3l-1.7-1"/>',
    # 日夜一键切换：当前深色 → 显示太阳（点了变浅色）；当前浅色 → 显示月亮。
    "sun": '<circle cx="12" cy="12" r="4.2"/>'
           '<path d="M12 2.6v2.4M12 19v2.4M2.6 12h2.4M19 12h2.4'
           'M5.3 5.3l1.7 1.7M17 17l1.7 1.7M5.3 18.7L7 17M17 7l1.7-1.7"/>',
    "moon": '<path d="M20.4 14.2A8.6 8.6 0 0 1 9.8 3.6a8.6 8.6 0 1 0 10.6 10.6z"/>',
    "sliders": '<path d="M4 7.5h7M15 7.5h5M4 12h4M12 12h8M4 16.5h9M17 16.5h3"/><circle cx="13" cy="7.5" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="15" cy="16.5" r="2"/>',
    "fullscreen": '<path d="M9 4.5H6a1.5 1.5 0 0 0-1.5 1.5v3M15 4.5h3A1.5 1.5 0 0 1 19.5 6v3M9 19.5H6A1.5 1.5 0 0 1 4.5 18v-3M15 19.5h3a1.5 1.5 0 0 0 1.5-1.5v-3"/>',
    "fullscreen_exit": '<path d="M4.5 9H8a1 1 0 0 0 1-1V4.5M19.5 9H16a1 1 0 0 1-1-1V4.5M4.5 15H8a1 1 0 0 1 1 1v3.5M19.5 15H16a1 1 0 0 0-1 1v3.5"/>',
    "playlist": '<path d="M4 7h11M4 12h11M4 17h7"/><path d="M16.5 15.2v-6.4l4.5 3.2z" fill="CURRENT" stroke="none"/>',
    "screenshot": '<path d="M4 8.5A2.5 2.5 0 0 1 6.5 6h1.2l1-1.7h6.6L16.3 6h1.2A2.5 2.5 0 0 1 20 8.5v8A2.5 2.5 0 0 1 17.5 19h-11A2.5 2.5 0 0 1 4 16.5z"/><circle cx="12" cy="12.4" r="3.2"/>',
    "loop": '<path d="M4.5 11.5A5.5 5.5 0 0 1 10 6h7"/><path d="M14 3.2l3 2.8-3 2.8"/><path d="M19.5 12.5A5.5 5.5 0 0 1 14 18H7"/><path d="M10 20.8l-3-2.8 3-2.8"/>',
    "loop_one": '<path d="M4.5 11.5A5.5 5.5 0 0 1 10 6h7"/><path d="M14 3.2l3 2.8-3 2.8"/><path d="M19.5 12.5A5.5 5.5 0 0 1 14 18H7"/><path d="M10 20.8l-3-2.8 3-2.8"/><path d="M11 10.6l1.4-.9v4.6"/>',
    "shuffle": '<path d="M4 7h3.2l2.4 3M20 7h-4.6l-6.2 10H4M20 17h-4.6"/><path d="M17.4 4.6L20 7l-2.6 2.4M17.4 14.6L20 17l-2.6 2.4"/>',
    "order": '<path d="M4 7h16M4 12h11M4 17h7"/>',
    "pip": '<rect x="3.5" y="5" width="17" height="14" rx="2.5"/><rect x="12" y="11" width="7" height="6" rx="1.6" fill="CURRENT" stroke="none"/>',
    "music_note": '<path d="M9 18V6.6l10-1.6v11"/><circle cx="6.6" cy="18" r="2.6"/><circle cx="16.6" cy="16" r="2.6"/>',
    "list_music": '<path d="M4 7h10M4 12h10M4 17h6"/><circle cx="17.5" cy="16" r="2.5"/><path d="M20 16V8l-5 1"/>',

    # ---- 抽屉 ----
    "add": '<path d="M12 5.5v13M5.5 12h13"/>',
    "collapse": '<path d="M5 12h14"/>',
    "filter": '<path d="M4 6h16l-6 7v5.5l-4 2V13z"/>',
    "sort": '<path d="M7 4.5v15M7 19.5l-3-3M7 19.5l3-3M17 19.5v-15M17 4.5l-3 3M17 4.5l3 3"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M15.5 15.5L20 20"/>',
    "folder": '<path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h3.4l2 2.4h7.6a2 2 0 0 1 2 2v7.6a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "trash": '<path d="M5 7.5h14M9.5 7.5V5.8A1.3 1.3 0 0 1 10.8 4.5h2.4a1.3 1.3 0 0 1 1.3 1.3v1.7"/><path d="M6.6 7.5l.9 11a1.6 1.6 0 0 0 1.6 1.5h5.8a1.6 1.6 0 0 0 1.6-1.5l.9-11"/>',
    "more": '<circle cx="6" cy="12" r="1.6" fill="CURRENT" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="CURRENT" stroke="none"/><circle cx="18" cy="12" r="1.6" fill="CURRENT" stroke="none"/>',
    "clear": '<circle cx="12" cy="12" r="8"/><path d="M9 9l6 6M15 9l-6 6"/>',

    # ---- 图片 ----
    "zoom_in": '<circle cx="11" cy="11" r="6"/><path d="M15.5 15.5L20 20M11 8.6v4.8M8.6 11h4.8"/>',
    "zoom_out": '<circle cx="11" cy="11" r="6"/><path d="M15.5 15.5L20 20M8.6 11h4.8"/>',
    # #8 重绘：原来四个角括号会被读成「全屏」，与"适应窗口"语义冲突。
    #    改成「外框 + 居中的小图」——一眼就是"整张图装进窗口"。
    "fit": '<rect x="3.5" y="4.5" width="17" height="15" rx="2.4"/><rect x="8" y="8.6" width="8" height="6.8" rx="1.2" fill="CURRENT" stroke="none"/>',
    # #8 重绘：原来手绘的 "10" 在 16px 下糊成一团。
    #    改成「外框 + 2×2 像素格」——"原始像素 1:1"的通用视觉语言。
    "one_to_one": '<rect x="3.5" y="4.5" width="17" height="15" rx="2.4"/><rect x="8.6" y="8.8" width="3.3" height="3.1" fill="CURRENT" stroke="none"/><rect x="12.1" y="8.8" width="3.3" height="3.1" fill="CURRENT" stroke="none"/><rect x="8.6" y="12.1" width="3.3" height="3.1" fill="CURRENT" stroke="none"/><rect x="12.1" y="12.1" width="3.3" height="3.1" fill="CURRENT" stroke="none"/>',
    "rotate_left": '<path d="M4.5 9.5A8 8 0 1 1 4 14"/><path d="M4.5 4.5v5h5"/>',
    "rotate_right": '<path d="M19.5 9.5A8 8 0 1 0 20 14"/><path d="M19.5 4.5v5h-5"/>',
    "image": '<rect x="3.5" y="5" width="17" height="14" rx="2.5"/><circle cx="9" cy="10" r="1.8"/><path d="M4.5 17l4.6-4.3a1.6 1.6 0 0 1 2.2 0l4 3.7M14 15.6l1.6-1.5a1.6 1.6 0 0 1 2.2 0l2 1.9"/>',
    "video": '<rect x="3" y="6" width="12.5" height="12" rx="2.5"/><path d="M15.5 11l5.5-3.4v8.8L15.5 13z"/>',
    "next_image": '<path d="M5.5 5.5l9 6.5-9 6.5z" fill="CURRENT" stroke="none"/><path d="M18.5 5.5v13"/>',
    "prev_image": '<path d="M18.5 5.5l-9 6.5 9 6.5z" fill="CURRENT" stroke="none"/><path d="M5.5 5.5v13"/>',
    # #8 新增：原「水平镜像」按钮错用了 `wall`（2×2 网格图标）—— 与功能完全对不上。
    #    标准语言：中轴虚线 + 两侧镜像的图形。
    "flip_h": '<path d="M12 3.8v16.4" stroke-dasharray="2.2 2.4"/><path d="M9.8 7.6L4.2 12l5.6 4.4z" fill="CURRENT" stroke="none"/><path d="M14.2 7.6L19.8 12l-5.6 4.4z"/>',
    "wall": '<rect x="3.5" y="3.5" width="7.5" height="7.5" rx="1.6"/><rect x="13" y="3.5" width="7.5" height="7.5" rx="1.6"/><rect x="3.5" y="13" width="7.5" height="7.5" rx="1.6"/><rect x="13" y="13" width="7.5" height="7.5" rx="1.6"/>',

    # ---- 设置 / 信息 ----
    "check": '<path d="M5 12.8l4.4 4.4L19 7.2"/>',
    "info": '<circle cx="12" cy="12" r="8"/><path d="M12 11v5.4M12 7.8v.2"/>',
    "keyboard": '<rect x="3" y="6.5" width="18" height="11" rx="2.5"/><path d="M6.5 10h.01M10 10h.01M13.5 10h.01M17 10h.01M8 13.6h8"/>',
    "palette": '<path d="M12 3.5a8.5 8.5 0 1 0 0 17c1.4 0 2-.9 2-1.8 0-1.5-1.2-1.7-1.2-2.7 0-.8.7-1.4 1.6-1.4h1.4a4.7 4.7 0 0 0 4.7-4.7c0-3.6-3.7-6.4-8.5-6.4z"/><circle cx="8" cy="11" r="1.2" fill="CURRENT" stroke="none"/><circle cx="11.4" cy="8" r="1.2" fill="CURRENT" stroke="none"/><circle cx="15.6" cy="9.4" r="1.2" fill="CURRENT" stroke="none"/>',
    "monitor": '<rect x="3" y="4.5" width="18" height="12.5" rx="2.5"/><path d="M9 20.5h6M12 17v3.5"/>',
    "speaker": '<path d="M5 9.5h3.4L13 5.4v13.2L8.4 14.5H5z" fill="CURRENT" stroke="none"/><path d="M16 9.4a3.8 3.8 0 0 1 0 5.2"/>',
    "link": '<path d="M10.5 13.5a3.6 3.6 0 0 0 5.1 0l2.6-2.6a3.6 3.6 0 0 0-5.1-5.1l-1.3 1.3"/><path d="M13.5 10.5a3.6 3.6 0 0 0-5.1 0l-2.6 2.6a3.6 3.6 0 0 0 5.1 5.1l1.3-1.3"/>',
    "text": '<path d="M5 6.5V5h14v1.5M12 5v14M9 19h6"/>',
    "position": '<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M4 15h16" stroke-dasharray="2 2.4"/>',
    "clock": '<circle cx="12" cy="12" r="8"/><path d="M12 7.6V12l3.2 2"/>',
    "arrow_up": '<path d="M12 19V6M6.5 11.5L12 6l5.5 5.5"/>',
    "arrow_down": '<path d="M12 5v13M17.5 12.5L12 18l-5.5-5.5"/>',
    "arrow_left": '<path d="M19 12H6M11.5 6.5L6 12l5.5 5.5"/>',
    "arrow_right": '<path d="M5 12h13M12.5 6.5L18 12l-5.5 5.5"/>',
    "minus": '<path d="M5.5 12h13"/>',
    "plus": '<path d="M12 5.5v13M5.5 12h13"/>',
    "reload": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v5h-5"/>',
    "trash_all": '<path d="M5 7.5h14M9.5 7.5V5.8A1.3 1.3 0 0 1 10.8 4.5h2.4a1.3 1.3 0 0 1 1.3 1.3v1.7"/><path d="M6.6 7.5l.9 11a1.6 1.6 0 0 0 1.6 1.5h5.8a1.6 1.6 0 0 0 1.6-1.5l.9-11"/>',
    "bell": '<path d="M6.5 10a5.5 5.5 0 0 1 11 0c0 4 1.5 5.4 1.5 5.4H5S6.5 14 6.5 10z"/><path d="M10.2 18.6a2 2 0 0 0 3.6 0"/>',
}

_cache: dict[tuple, QIcon] = {}


def icon_svg(name: str, color: str | None = None, stroke: float = 1.7) -> str:
    # ⚠️ 默认色在**调用时**解析：写成默认参数会在模块导入时求值，
    #    浅色模式启动时会拿到深色主题的浅灰（见 widgets.py 里的同类注释）。
    color = P["text"] if color is None else color
    body = _STROKE.get(name)
    if body is None:
        body = _STROKE["info"]
    body = body.replace("CURRENT", color)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="{stroke}" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )


# --------------------------------------------------------------------------------------
# pixmap 缓存
# --------------------------------------------------------------------------------------
# ⚠️ 这是启动与拖动流畅度的关键：IconButton.paintEvent **每一次重绘**都会调 render_pixmap,
#    而它内部要 new 一个 QSvgRenderer 把 SVG 文本重新解析一遍。
#    界面上一百多个图标按钮 ⇒ 首帧要解析上百个 SVG ⇒ 实测 window.show() 光这一项就吃掉
#    2.6 秒（启动总耗时 3.3 秒）。加了缓存之后同一 (名字, 颜色, 尺寸, dpr, 描边) 只解析一次。
_PM_CACHE: dict[tuple, QPixmap] = {}
_PM_ORDER: list[tuple] = []
_PM_MAX = 400


def render_pixmap(name: str, color: str | None = None, size: int = 20,
                  dpr: float = 2.0, stroke: float = 1.7, opacity: float = 1.0) -> QPixmap:
    """把图标渲染成 pixmap（物理尺寸 size×dpr, 逻辑尺寸 size）。带缓存。

    ⚠️ 这里曾有一个**统一裁掉所有小图标**的 bug：
    给 QPixmap 调过 `setDevicePixelRatio(dpr)` 之后，**QPainter 的坐标系就已经按 dpr
    缩放好了**。这时再传 `QRectF(0, 0, size*dpr, size*dpr)` 就等于把内容按 dpr 放大画一遍，
    只有左上角约 1/dpr² 的区域落在 pixmap 内，右边与下边整片被裁掉。
    实测 135 个渲染里 87 个的右/下边缘有像素溢出 —— 表现为
    "所有小图标都缺一块"（X 号少一笔、播放键缺角、截图键断边、音量图标变形）。
    正确写法：传**逻辑尺寸** `QRectF(0, 0, size, size)`。
    """
    key = (name, color, int(size), round(float(dpr), 3), round(float(stroke), 3),
           round(float(opacity), 3))
    hit = _PM_CACHE.get(key)
    if hit is not None:
        return hit

    side = max(1, int(round(size * dpr)))
    px = QPixmap(side, side)
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)
    renderer = QSvgRenderer(QByteArray(icon_svg(name, color, stroke).encode("utf-8")))
    painter = QPainter(px)
    painter.setRenderHint(QPainter.Antialiasing, True)
    if opacity < 1.0:
        painter.setOpacity(opacity)
    renderer.render(painter, QRectF(0, 0, size, size))   # ← 逻辑尺寸, 不是 size*dpr
    painter.end()

    _PM_CACHE[key] = px
    _PM_ORDER.append(key)
    while len(_PM_ORDER) > _PM_MAX:                      # 简单 FIFO, 防止无限增长
        _PM_CACHE.pop(_PM_ORDER.pop(0), None)
    return px


def cache_size() -> int:
    return len(_PM_CACHE)


def clear_caches() -> int:
    """清空图标缓存，返回清掉的条数。

    ⚠️ 换肤时必须先清：缓存 key 里**含颜色**，不清也不会拿错图 ——
       但**已经画到控件上的**（QListWidgetItem 的 icon、菜单图标…）不会自己重画，
       它们会一直停在旧主题的颜色上（用户反复反馈的"深色切浅色图标变淡"就是这么来的）。
       所以换肤流程是：清缓存 → 重建那些"持有图标对象"的地方（见 panels 的 on_theme_changed）。
    """
    n = len(_cache) + len(_PM_CACHE)
    _cache.clear()
    _PM_CACHE.clear()
    _PM_ORDER.clear()
    return n


def make_icon(name: str, color: str | None = None, size: int = 20,
              dpr: float = 2.0, stroke: float = 1.7) -> QIcon:
    return QIcon(render_pixmap(name, color, size, dpr, stroke))


def get_icon(name: str, color: str | None = None, size: int = 20, stroke: float = 1.7) -> QIcon:
    color = P["text"] if color is None else color
    key = (name, color, size, round(stroke, 2))
    ic = _cache.get(key)
    if ic is None:
        ic = make_icon(name, color, size, 2.0, stroke)
        _cache[key] = ic
    return ic


def has_icon(name: str) -> bool:
    return name in _STROKE
