"""设计系统：色板、几何规范、材质与全局样式表。

几何来源：对「爱奇艺万能联播 5.4.1.5408」真实运行截图与皮肤资源的逆向测量
（对照表见 docs\\01-爱奇艺5.4.1逆向对照表.md）。视觉在此基础上做了现代化升华：
  * 抛弃纯黑死色 → 微冷色（偏蓝灰）通透底色；
  * 抛弃荧光绿长条 → 低饱和冷青强调色 + 极细已播放高光；
  * 控制条由贴底通栏改为**悬浮胶囊**，带 0.5px 内发光折射高光边与柔和弥散阴影。
"""

from __future__ import annotations

import re

from PySide6.QtGui import QColor

# --------------------------------------------------------------------------------------
# 颜色（一律写成 #RRGGBB / #RRGGBBAA 的 CSS 习惯写法, 由 qt_color() 统一转换）
# --------------------------------------------------------------------------------------

PALETTE = {
    # 底色：微冷（蓝灰）而非死黑
    "bg_base": "#0F1116",
    "bg_deep": "#0A0C10",
    "bg_stage": "#07080B",          # 视频/图片舞台底（比窗口底更深, 让画面浮起来）

    # 玻璃材质：带 alpha 的冷色面（面板类要足够实以保证文字可读, 胶囊可以更透）
    "glass_chrome": "#161A21EB",     # 标题栏 / 抽屉 主体
    # 胶囊：用户要求"透明度高一点" —— 从 alpha 224 降到 185（约 73% 不透明）。
    # ⚠️ 不能更低：胶囊是**浮在画面上**的，视频亮的时候 150（59%）会让按钮和
    #    进度条读不出来（实测像素自审里胶囊区域均值亮度飙到 134，整块发白）。
    "glass_capsule": "#12161FB9",
    "glass_panel": "#171B23F9",      # 设置面板 / 弹层
    "glass_card": "#1C212BF7",       # 卡片（音频信息卡）
    "glass_hover": "#FFFFFF14",
    "glass_press": "#FFFFFF0A",
    "glass_track": "#FFFFFF1F",
    # 玻璃顶部光泽（浅色主题下需要更强的白色反光才看得出来）
    "glass_gloss": "#FFFFFF1A",
    # 输入类控件（下拉框 / 行编辑 / 数字框）的底色
    "input_bg": "#FFFFFF0F",
    # 滚动条滑块悬停色
    "scroll_hover": "#FFFFFF40",

    # 描边：0.5px 内发光折射高光
    "hairline": "#FFFFFF1A",
    "hairline_strong": "#FFFFFF2E",
    "inner_glow": "#FFFFFF24",

    # 文字
    "text": "#F3F5F9",
    # 深色模式的二级文字也要能读：原来 #A8B0BF 在小字号下发灰
    # （用户："你的深色模式也存在字不易读的情况"）
    "text_dim": "#C8D0DC",
    "text_muted": "#8E97A6",
    "text_on_accent": "#04241F",

    # 强调色：低饱和冷青（不是老版的荧光绿）
    "accent": "#2FD3C0",
    "accent_dim": "#2FD3C066",
    "accent_soft": "#2FD3C024",
    "danger": "#FF6B6B",
    "warn": "#FFC46B",

    # 阴影
    "shadow": "#00000000",
    # ⚠️ 下面这几个键**必须存在于基准调色板**：QSS 里引用了它们，
    #    只在某个主题预设里定义的话，用别的主题启动时 `P['divider']` 会直接 KeyError
    #    —— 实测整个 app 起不来（控制面都连不上）。
    #    "预设只写差异" 的前提是**基准里得有这个键**。
    "divider": "#FFFFFF1A",
    "icon": "#C8D0DC",
    "icon_hover": "#F3F5F9",
    "hover_bg": "#FFFFFF14",
    "hairline_white": "#FFFFFF00",
    # 悬浮层阴影：QQ NT 是 "0 8px 24px rgba(0,0,0,0.06)"，
    # 大半径 + 极低 alpha。原来 16 层黑色叠出来一圈发脏的灰边。
    # 用户规范：透明度 0.04~0.06、模糊半径 24~32px、垂直偏移 6~8px。
    # ⚠️ 安全降级：**不做位图模糊**。
    #    上一版用 QGraphicsBlurEffect 模糊"一整块实心黑圆角矩形"，
    #    内部高浓度黑全部扩散到外圈 → 出来是一团烟熏/烧焦的黑影
    #    （用户截图："像被大火烧焦一样的黑色浓烟"）。
    #    位图模糊只有在"被模糊的形状本身 alpha 就很低"时才是软阴影。
    #    浅色模式真正干净的做法：**极细描边 + 超轻阴影**（0.02~0.03、偏移 3px、不外扩）。
    "float_shadow_alpha": 0.0,
    "float_shadow_spread": 2,
    "float_shadow_dy": 3,
    "float_shadow_layers": 1,
    # OSD 专用：**不跟主题走**，永远深黑底 + 纯白字
    # OSD：**跟随主题**，但必须保证对比度 ——
    #   深色主题 = 深黑底 + 纯白字；浅色主题 = 浅白底 + 深黑字（见各预设覆盖）。
    "osd_bg": "#141416D9",
    "osd_text": "#FFFFFF",
    "osd_border": "#FFFFFF26",
}

# 基准深色调色板快照：`apply_theme` 每次都从这里重置，避免主题之间串色
# （浅色主题把 text 改深之后，切回深色主题如果只做 update，文字会永远留在深色）。
_BASE_PALETTE = dict(PALETTE)

# --------------------------------------------------------------------------------------
# 换肤（真实生效的主题预设）
# --------------------------------------------------------------------------------------
# 用户反馈"换肤效果依旧是摆设" —— 之前设置面板里只有一个图标、没有任何预设。
# 这里定义真正会改变界面的预设：每个预设覆盖强调色与玻璃底色，
# 由 MainWindow.apply_theme() 在运行时套用（重设样式表 + 全窗重绘 + 落盘）。
THEMES: dict[str, dict] = {
    "graphite": {
        "label": "石墨（冷青）",
        "accent": "#2FD3C0", "accent_dim": "#2FD3C066", "accent_soft": "#2FD3C024",
        "text_on_accent": "#04241F",
        # ⭐ QQ 夜间层次：卡片比底**亮**、容器比底**暗** —— 层次靠明度差，不靠透明。
        # 这是启动默认的深色主题，原来底是近黑 #0F1116、卡片比底还暗，
        # 所以"深色模式字不易读、也没有层次"。
        "bg_base": "#23262B", "bg_deep": "#1B1E22", "bg_stage": "#17191D",
        "glass_chrome": "#23262BFA", "glass_capsule": "#2B2F35FA",
        "glass_panel": "#262A30FA", "glass_card": "#33383F",
    },
    # ---- 苹果风格「液态玻璃」两套：一浅一深，透明度都拉得很高 ----
    # 做法：玻璃底色几乎全透（alpha 0x7A~0x99），靠**描边 + 顶部光泽**立住形体，
    # 而不是靠一块实心色 —— 这正是 iOS/macOS 那套液态玻璃的观感来源。
    # ---- QQ 风格两套（数值**从用户提供的 QQ 截图里量出来的**，不是我猜的）----
    # QQ 的 asar 是加密的（607 个 PNG 抽查 0 个有 PNG 魔数、package.json 是乱码），
    # 所以源码扒不出配色；下面这些是直接在截图上采样的结果：
    #   浅色：窗口/侧栏 #F5F5F5、卡片 #FFFFFF（比底亮）、选中 #EBEBEB/#DFDFDF
    #   深色：窗口 #262626、卡片 #3B3B3B（比底亮）、分组容器 #111111（比底暗）
    #   强调蓝：#0080F5（浅）/ #148FFF（深）
    # **QQ 的层次完全靠明度差拉开，不靠透明** —— 这就是它"自然"的原因，
    # 也是我前几轮"把面板调透明"走错的地方。
    "qq-light": {
        "label": "QQ 浅色",
        "accent": "#0080F5", "accent_dim": "#0080F566", "accent_soft": "#0080F51F",
        "text_on_accent": "#FFFFFF",
        # QQ NT 浅色规范：侧栏 #F2F3F5、内容区 #F7F8FA、卡片 #FFFFFF、分割线 #E5E6EB
        "bg_base": "#F7F8FA", "bg_deep": "#F2F3F5", "bg_stage": "#F7F8FA",
        # 玻璃一律做成**接近不透明**：层次交给底色明度，不交给 alpha
        # 严禁全白死色：浮层/侧栏一律用高透磨砂白（0.75~0.88），靠背后的亚克力出质感
        "glass_chrome": "#FFFFFFE0",
        "glass_capsule": "#FFFFFFD9",
        "glass_panel": "#FFFFFFDB",
        "glass_card": "#FFFFFF",
        "glass_hover": "#0000000F", "glass_press": "#00000008",
        # 未激活滑轨要一眼看得见（原来太淡，用户："音量条常态隐身"）
        "glass_track": "#E5E6EB", "glass_gloss": "#FFFFFF00",
        "input_bg": "#F2F3F5", "scroll_hover": "#00000033",
        "hairline_white": "#FFFFFF99",   # 1px 半透明白内发光（QQ NT 的软边）
        "icon": "#4E5969", "icon_hover": "#1D2129",   # 图标常态/悬停
        "hover_bg": "#0000000F",         # 悬停底色 rgba(0,0,0,0.06)
        "divider": "#E5E6EB",
        # 浅色模式下 OSD 也走浅色（用户："提示操作的悬浮窗为什么在浅色模式下还是深色的"）
        "osd_bg": "#FFFFFFF0", "osd_text": "#1D2129", "osd_border": "#00000014",
        # 描边要看得见：原来 #0000001C 在白底上几乎不可见，按钮/输入框没有形
        # （用户截图："你看这个文字看得清吗"）
        # 边框必须纯净：浅色模式统一 1px rgba(0,0,0,0.08) / #E5E6EB，
        # 圆角处严禁灰黑交界线
        "hairline": "#00000014", "hairline_strong": "#00000014",
        "inner_glow": "#FFFFFF00",
        # 图标用 text_dim，浅色主题下 #7A7A7A 在白底上偏淡（用户："按钮图标也不明显"）
        # QQ NT 文字层级（用户规范）：一级 #1D2129 / 二级 #4E5969，
        # 严禁低于 4.5:1 对比度的灰白混淆配色
        "text": "#1D2129", "text_dim": "#4E5969", "text_muted": "#86909C",
        "danger": "#E64340", "warn": "#C87A00",
    },
    "qq-dark": {
        "label": "QQ 深色",
        # 数值来自用户提供的 QQ 夜间截图实测（见知识库 §9.37）：
        #   窗口 #262626 / 侧栏 #1E1E1E / 卡片 #3B3B3B（比底**亮**）
        #   分组容器 #111111（比底**暗**）/ 选中 #2F2F2F、#464646
        #   强调蓝 #148FFF
        # ⭐ QQ 夜间的层次**完全靠明度差**：卡片比底亮、容器比底暗，
        #    而不是靠透明或模糊。
        "accent": "#148FFF", "accent_dim": "#148FFF66", "accent_soft": "#148FFF26",
        "text_on_accent": "#FFFFFF",
        "bg_base": "#262626", "bg_deep": "#1E1E1E", "bg_stage": "#1A1A1A",
        "glass_chrome": "#262626FA",
        "glass_capsule": "#2E2E2EFA",
        "glass_panel": "#2A2A2AFA",
        "glass_card": "#3B3B3B",          # 卡片比底亮 —— QQ 夜间的关键
        "glass_hover": "#FFFFFF14", "glass_press": "#FFFFFF0A",
        "glass_track": "#FFFFFF24", "glass_gloss": "#FFFFFF00",
        "input_bg": "#1E1E1E", "scroll_hover": "#FFFFFF3D",
        "hairline": "#FFFFFF14", "hairline_strong": "#FFFFFF14",
        "inner_glow": "#FFFFFF00",
        "divider": "#FFFFFF1A",
        "text": "#F5F5F5", "text_dim": "#C4C4C4", "text_muted": "#8A8A8A",
        "danger": "#FF6B6B", "warn": "#FFC46B",
    },
    "amber": {
        "label": "琥珀（暖金）",
        "accent": "#E8A33D", "accent_dim": "#E8A33D66", "accent_soft": "#E8A33D24",
        "text_on_accent": "#2A1A05",
        "bg_base": "#14110C", "bg_deep": "#0D0B07", "bg_stage": "#0A0805",
        "glass_chrome": "#1C1810EB", "glass_capsule": "#1A161096",
        "glass_panel": "#1E1911F9", "glass_card": "#241E14F7",
    },
    "sakura": {
        "label": "樱粉",
        "accent": "#F2739E", "accent_dim": "#F2739E66", "accent_soft": "#F2739E24",
        "text_on_accent": "#2B0716",
        "bg_base": "#140F14", "bg_deep": "#0D090D", "bg_stage": "#0A070A",
        "glass_chrome": "#1C141CEB", "glass_capsule": "#1A121A96",
        "glass_panel": "#1E161EF9", "glass_card": "#241A24F7",
    },
    "aurora": {
        "label": "极光紫",
        "accent": "#9B8CFF", "accent_dim": "#9B8CFF66", "accent_soft": "#9B8CFF24",
        "text_on_accent": "#120A33",
        "bg_base": "#100F18", "bg_deep": "#0A0910", "bg_stage": "#07060C",
        "glass_chrome": "#171624EB", "glass_capsule": "#14132296",
        "glass_panel": "#1A1928F9", "glass_card": "#201F30F7",
    },
    "mint": {
        "label": "薄荷绿",
        "accent": "#5BD98A", "accent_dim": "#5BD98A66", "accent_soft": "#5BD98A24",
        "text_on_accent": "#04240F",
        "bg_base": "#0E1310", "bg_deep": "#090D0A", "bg_stage": "#060A07",
        "glass_chrome": "#141C17EB", "glass_capsule": "#111A1596",
        "glass_panel": "#161F19F9", "glass_card": "#1B251FF7",
    },
}
DEFAULT_THEME = "graphite"


def apply_theme(name: str) -> bool:
    """把预设套进 PALETTE（原地改，所有读 P[...] 的地方立刻拿到新值）。

    ⚠️ 先**整体重置回基准深色**再套预设。只做 update 的话，浅色主题把 `text`
    改成深色之后，切回深色主题时预设里没写 `text`，文字就永远留在深色 —— 看不见了。
    预设只写"和基准不一样的部分"，靠这一步保证来回切换是干净的。

    返回是否真的换了（同一个主题重复调用返回 False，调用方可以跳过重绘）。
    """
    preset = THEMES.get(name)
    if not preset:
        return False
    if P.get("_theme") == name:
        return False
    P.clear()
    P.update(_BASE_PALETTE)
    for k, v in preset.items():
        if k != "label":
            P[k] = v
    # 胶囊浮在画面上，**任何主题**都不允许它透到读不出字 —— 统一设一个下限（约 47%）。
    # 浅色"霜白"那套要的就是高透，所以下限比深色主题松，但不能不设。
    #
    # ⚠️ 本项目的调色板一律用 **CSS 顺序 `#RRGGBBAA`**（alpha 在**最后两位**），
    #    由 `qt_color()` / `css()` 负责转成 QColor。
    #    **不要在 QSS 里裸写 `{P['x']}`** —— Qt 会把 8 位十六进制按 `#AARRGGBB` 解析，
    #    于是 `#FFFFFF1A`（本意白 10%）渲染成不透明的 `#FFFF1A`（亮黄）。
    #    一律写 `{css(P['x'])}`。见知识库 §9.43。
    cap = P["glass_capsule"]
    if len(cap) == 9:
        alpha = int(cap[7:9], 16)
        if alpha < 0x78:
            P["glass_capsule"] = cap[:7] + "78"
    P["_theme"] = name
    P["_mode"] = "light" if name in ("qq-light",) else "dark"
    return True


def is_light() -> bool:
    return P.get("_mode") == "light"


def theme_names() -> list[str]:
    return list(THEMES.keys())


# --------------------------------------------------------------------------------------
# 几何（单位：逻辑像素 @100% 缩放）
# --------------------------------------------------------------------------------------

METRICS = {
    # 窗口
    "win_w": 1280,
    "win_h": 760,
    "win_min_w": 800,
    "win_min_h": 560,
    "resize_border": 6,             # 无边框窗口的拖拽缩放热区
    "win_radius": 12,

    # 标题栏（逆向值 44px @1299 宽；按现代比例取 46）
    "titlebar_h": 46,
    "titlebar_pad_l": 12,
    "titlebar_pad_r": 8,
    "logo_size": 26,
    "title_btn_w": 44,
    "title_btn_h": 32,
    "title_gap": 10,

    # 底部悬浮胶囊（逆向：控制条总高 52、进度槽 3px、按钮 25px、播放键 41px）
    # 说明：我们刻意做了两处现代化偏离 —— ① 贴底通栏直角 → 悬浮胶囊（圆角 18、
    # 左右 18 / 距底 16 外边距）；② 中央播放键 41 → 44。理由写在逆向对照表里。
    "capsule_h": 80,
    "capsule_margin_lr": 18,
    "capsule_margin_b": 16,
    "capsule_shadow": 16,           # 阴影扩散半径（同时也是控件的"外框"厚度）
    "capsule_radius": 18,
    "capsule_pad_h": 14,
    "capsule_pad_v": 9,
    "progress_row_h": 28,
    "button_row_h": 46,
    "progress_h": 5,                # 进度条轨道高度（老版是 2px 细线）
    "progress_h_hover": 8,
    "progress_handle": 13,
    "btn_size": 32,                 # 普通圆形按钮
    "btn_size_lg": 44,              # 中央播放/暂停
    "btn_gap": 6,
    "btn_gap_lg": 14,
    "time_width": 108,              # "00:00/00:00" 的宽度预算
    "volume_width": 96,

    # 右侧播放列表抽屉（逆向：269 / 866 = 31.1% → 1280 宽取 392）
    "drawer_w": 392,
    "drawer_min_w": 320,
    "drawer_max_w": 560,
    "drawer_header_h": 42,
    "drawer_search_h": 32,
    "drawer_item_h": 38,
    "drawer_item_pad": 11,
    "drawer_thumb_h": 0,            # 列表不设缩略图（保持紧凑, 与逆向一致）
    "drawer_anim_ms": 220,
    "drawer_edge_trigger": 24,      # 靠近右边缘多少像素触发

    # 设置面板（逆向：572x500 @866x589 → 66% x 85%；作为窗口内浮层会被舞台高度约束,
    # 所以给"首选值 + 最小值", 运行时按可用空间自适应）
    "settings_w": 860,
    "settings_h": 650,
    "settings_min_w": 700,
    "settings_min_h": 440,
    "settings_side_w": 168,
    "settings_pad": 26,
    "settings_row_h": 40,
    "settings_title_h": 30,
    "settings_group_gap": 18,

    # 图片工具条 / 音频卡片
    "img_toolbar_h": 52,
    "audio_card_w": 380,

    # 圆角
    "radius_sm": 6,
    "radius": 10,
    "radius_lg": 14,
    "radius_pill": 999,
}

FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'


# --------------------------------------------------------------------------------------
# 颜色工具
# --------------------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def qt_color(spec: str, alpha: float | None = None) -> QColor:
    """把 CSS 习惯的 #RRGGBB / #RRGGBBAA 转成 QColor。

    ⚠️ Qt 的 8 位十六进制是 #AARRGGBB, **不是** #RRGGBBAA。
    直接把这个串交给 QColor 会把透明度与颜色对调（本项目踩过：整个界面变成宝蓝色）。
    """
    if isinstance(spec, QColor):
        c = QColor(spec)
        if alpha is not None:
            c.setAlphaF(alpha)
        return c
    m = _HEX_RE.match(str(spec).strip())
    if not m:
        c = QColor(spec)
        if alpha is not None:
            c.setAlphaF(alpha)
        return c
    h = m.group(1)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    a = int(h[6:8], 16) if len(h) == 8 else 255
    c = QColor(r, g, b, a)
    if alpha is not None:
        c.setAlphaF(alpha)
    return c


def css(spec: str, alpha: float | None = None) -> str:
    """返回可直接写进 QSS 的 `rgba(r,g,b,a)`。"""
    c = qt_color(spec, alpha)
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


def mix(a: str, b: str, t: float) -> str:
    ca, cb = qt_color(a), qt_color(b)
    return "#%02X%02X%02X%02X" % (
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
        round(ca.alpha() + (cb.alpha() - ca.alpha()) * t),
    )


P = PALETTE
M = METRICS


# --------------------------------------------------------------------------------------
# 全局样式表
# --------------------------------------------------------------------------------------

def build_stylesheet() -> str:
    return f"""
    * {{
        font-family: {FONT_FAMILY};
        font-size: 13px;
        color: {P['text']};
        outline: none;
    }}
    QWidget#RootWindow {{
        background: transparent;
    }}
    QWidget#WindowBody {{
        background: {css(P['bg_base'])};
        border: 1px solid {css(P['hairline_strong'])};
        border-radius: {M['win_radius']}px;
    }}
    QWidget#TitleBar {{
        background: transparent;
    }}
    QWidget#Stage {{
        background: {css(P['bg_stage'])};
    }}
    QWidget#Capsule {{
        background: transparent;
    }}
    QWidget#Drawer {{
        background: {css(P['glass_chrome'])};
        border: 1px solid {css(P['hairline'])};
        border-radius: {M['radius_lg']}px;
    }}
    QWidget#SettingsPanel {{
        background: {css(P['glass_panel'])};
        border: 1px solid {css(P['hairline_strong'])};
        border-radius: {M['radius_lg']}px;
    }}
    QWidget#AudioCard {{
        background: {css(P['glass_card'])};
        border: 1px solid {css(P['hairline_strong'])};
        border-radius: {M['radius_lg']}px;
    }}
    QLabel#TitleText {{
        color: {P['text_dim']};
        font-size: 13px;
    }}
    QLabel[role="h1"] {{ font-size: 14px; font-weight: 600; color: {P['text']}; }}
    QLabel[role="sub"] {{ font-size: 12px; color: {P['text_muted']}; }}
    QLabel[role="body"] {{ color: {P['text']}; }}
    QLabel[role="h2"] {{ font-size: 13px; font-weight: 600; color: {P['text']}; }}
    QLabel[role="dim"] {{ color: {P['text_dim']}; }}
    QLabel[role="muted"] {{ color: {P['text_muted']}; font-size: 12px; }}
    QLabel[role="accent"] {{ color: {P['accent']}; }}

    QToolTip {{
        background: {css(P['glass_panel'])};
        color: {P['text']};
        border: 1px solid {css(P['hairline_strong'])};
        border-radius: 6px;
        padding: 5px 9px;
    }}

    /* ---- 滚动条：极细、悬停才明显 ---- */
    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 2px 2px 2px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {css(P['glass_track'], 1.0)};
        min-height: 32px; border-radius: 3px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {css(P['scroll_hover'])}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{
        background: transparent; height: 10px; margin: 0 2px 2px 2px;
    }}
    QScrollBar::handle:horizontal {{
        background: {css(P['glass_track'])};
        min-width: 32px; border-radius: 3px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {css(P['scroll_hover'])}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

    /* ---- 设置面板 ---- */
    QListWidget#SettingsNav {{
        background: transparent; border: none;
        padding: 8px 8px 8px 8px;
    }}
    QListWidget#SettingsNav::item {{
        height: 38px; border-radius: 8px; padding-left: 14px;
        color: {P['text_dim']};
    }}
    QListWidget#SettingsNav::item:hover {{ background: {css(P['glass_hover'])}; color: {P['text']}; }}
    QListWidget#SettingsNav::item:selected {{
        background: {css(P['accent_soft'])}; color: {P['accent']}; font-weight: 600;
    }}

    QCheckBox {{ spacing: 9px; color: {P['text_dim']}; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: 4px;
        border: 1px solid {css(P['hairline_strong'])};
        background: {css(P['input_bg'])};
    }}
    QCheckBox::indicator:hover {{ border-color: {css(P['accent_dim'])}; }}
    QCheckBox::indicator:checked {{
        background: {css(P['accent'])}; border-color: {css(P['accent'])};
        image: none;
    }}
    QCheckBox::indicator:checked:disabled {{ background: {css(P['text_muted'])}; }}

    QComboBox {{
        background: {css(P['input_bg'])}; border: 1px solid {css(P['hairline_strong'])};
        border-radius: 8px; padding: 6px 10px; min-height: 22px; color: {P['text']};
    }}
    QComboBox:hover {{ border-color: {css(P['accent_dim'])}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {css(P['glass_panel'])};
        border: 1px solid {css(P['hairline_strong'])};
        border-radius: 8px; padding: 4px;
        selection-background-color: {css(P['accent_soft'])};
        selection-color: {P['accent']};
        outline: none;
    }}

    QLineEdit {{
        background: {css(P['input_bg'])}; border: 1px solid {css(P['hairline_strong'])};
        border-radius: 8px; padding: 6px 10px; color: {P['text']};
        selection-background-color: {css(P['accent_soft'])};
        selection-color: {P['accent']};
    }}
    QLineEdit:focus {{ border-color: {css(P['accent_dim'])}; }}

    QSlider::groove:horizontal {{
        height: 4px; background: {css(P['glass_track'])}; border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{
        background: {css(P['accent'])}; border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        width: 12px; height: 12px; margin: -5px 0; border-radius: 6px;
        background: {P['text']};
    }}
    QSlider::handle:horizontal:hover {{ background: {css(P['accent'])}; }}

    QSpinBox, QDoubleSpinBox {{
        background: {css(P['input_bg'])}; border: 1px solid {css(P['hairline_strong'])};
        border-radius: 8px; padding: 5px 8px; color: {P['text']};
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        width: 16px; border: none; background: transparent;
    }}

    QPushButton[role="primary"] {{
        background: {css(P['accent'])}; color: {P['text_on_accent']};
        border: none; border-radius: 8px; padding: 8px 22px; font-weight: 600;
    }}
    QPushButton[role="primary"]:hover {{ background: {css(P['accent'], 0.86)}; }}
    QPushButton[role="ghost"] {{
        background: {css(P['input_bg'])}; color: {P['text_dim']};
        border: 1px solid {css(P['hairline_strong'])};
        border-radius: 8px; padding: 8px 20px;
    }}
    QPushButton[role="ghost"]:hover {{ background: {css(P['glass_hover'])}; color: {P['text']}; }}
    QPushButton[role="link"] {{
        background: transparent; border: none; color: {P['accent']}; padding: 6px 4px;
    }}
    QPushButton[role="link"]:hover {{ color: {css(P['accent'], 0.8)}; }}

    QListWidget#Playlist {{
        background: transparent; border: none; outline: none;
    }}
    QListWidget#Playlist::item {{
        border-radius: 8px; color: {P['text_dim']};
    }}
    QListWidget#Playlist::item:hover {{ background: {css(P['glass_hover'])}; color: {P['text']}; }}
    QListWidget#Playlist::item:selected {{
        background: {css(P['accent_soft'])}; color: {P['accent']};
    }}

    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    """
