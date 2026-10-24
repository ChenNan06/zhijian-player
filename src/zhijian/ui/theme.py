"""设计系统：色板、几何规范、材质与全局样式表。

几何来源：对「爱奇艺万能联播 5.4.1.5408」真实运行截图与皮肤资源的逆向测量
（对照表见 docs\\01-爱奇艺5.4.1逆向对照表.md）。视觉在此基础上做了现代化升华：
  * 抛弃纯黑死色 → 微冷色（偏蓝灰）通透底色；
  * 抛弃荧光绿长条 → 低饱和冷青强调色 + 极细已播放高光；
  * 控制条由贴底通栏改为**悬浮胶囊**，带 0.5px 内发光折射高光边与柔和弥散阴影。
"""

from __future__ import annotations

import os
import re
import sys

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
    "text_on_accent": "#08305C",

    # 强调色：**冰蓝 #2080FA**（旧冷青/荧光绿系已彻底删除，见 task-118）
    "accent": "#2080FA",
    "accent_dim": "#2080FAA6",
    "accent_soft": "#2080FA47",
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

    # 弹层底色（下拉 / 菜单 / SurfacePopup）。
    # ⚠️⚠️ 两个硬约束，缺一个就是线上事故：
    #   ① **必须存在于基座调色板**（就是这一行）：build_stylesheet() 用 P['popup_bg']
    #      直接下标渲染 QComboBox QAbstractItemView 的底色，基座缺键 ⇒ 任何没覆盖它的
    #      预设一切过去就 KeyError（v1.0.34 实测：旧多主题时代 7 个预设里 5 个中招）。
    #      本项目 QSS 引用到的键**一律**要在基座里定义 —— 与上面 divider/icon 那几条同理。
    #   ② **必须不透明（alpha=FF）**：弹层是"无边框 + 半透明"的独立顶层窗口，
    #      底色一旦带 alpha，背后的设置文字就会透上来（用户截图"文字穿透"）。
    #      这一条有断言守着（build/qa/task05_theme_probe.py 里 alpha == 255）。
    # 取值依据：与各自预设的 glass_panel **同色相**、去掉 alpha（面板实底化），
    # 保证弹层与它所属面板的层次一致；浅色玻璃预设另给浅色实底（见 glass-light）。
    "popup_bg": "#171B23",
}

# 基准深色调色板快照：`apply_theme` 每次都从这里重置，避免主题之间串色
# （浅色主题把 text 改深之后，切回深色主题如果只做 update，文字会永远留在深色）。
_BASE_PALETTE = dict(PALETTE)

# 预设里的**元数据键**（不是颜色）：apply_theme 只把颜色写进调色板。
#   label      —— 设置面板 / OSD 的显示名
#   hidden     —— 隐藏彩蛋皮肤：不进 `theme_names()` 全局列表，只在设置面板下拉里
#   mode       —— 明暗族（"light" ⇒ is_light() 为真 ⇒ Scrim 0.32 / Vibrance +18% 自动成立）
#   glass_skin —— 液态玻璃皮肤：跳过"胶囊 alpha ≥ 0x78"下限（高透就是它的定义）
_PRESET_META_KEYS = frozenset(("label", "hidden", "mode", "glass_skin",
                             "deprecated"))


# --------------------------------------------------------------------------------------
# 液态玻璃材质（glass-light）—— **唯一风格源头**
# --------------------------------------------------------------------------------------
# 用户硬约束（task-17）：第三套皮肤必须**直接复用 v1.0.36 规格胶囊 SpecBadge
#   （audio_view.py，paintEvent）的绘制配方**，不许自创方案、不许引入着色器。
#   SpecBadge 的配方就两条：
#     ① 底板 = 高透纯白圆角填充（规范带宽 0.12~0.18）；
#     ② 边缘 = **1px** 高透纯白亮边（规范带宽 0.35~0.45）。
# 这里把这两条固化成唯一一组参数：
#   · 「液态玻璃」皮肤的三个容器（控制栏胶囊 / 播放列表抽屉 / 设置面板）直接用它们；
#   · SpecBadge 本体与容器共用 audio_view.paint_glass_plate() 这**一个**绘制函数。
#
# ⚠️ 三个底板键为什么取**互不相同**的 alpha：`widgets._key_of()` 是"按颜色值反查键名"
#    （换肤时要靠它把 GlassFrame 的填充重新解析回调色板），多个键同值会解析到别人的键
#    ⇒ 切出皮肤后串色。三者都落在规范带宽 [0.12,0.18] 内。
# ⚠️ 为什么取带宽**偏小**的一侧（0.129 / 0.133）：GlassFrame 底板之上还平铺了一层
#    磨砂颗粒（widgets._frost_tile，白 0~12/255）⇒ 取 0.15 时单像素合成 alpha 上界
#    1-(1-0.15)(1-0.047)=0.190 会冲出 0.18；取 0.13 则上界 0.170，稳在带宽内。
# ⚠️ 亮边**只有 1px**：它就是 GlassFrame 的描边笔 / 抽屉 QSS 的 1px border。
#    绝不能用 glass_gloss（那是覆盖容器顶部 0~65% 高度的**渐变光泽**，
#    一旦非 0，"高光"就变成多行、板体透射率也被拉低 —— 见 glass-light 预设置 0 的理由）。
GLASS_FILL = "#FFFFFF21"          # rgba(255,255,255,0.1294) SpecBadge 专用（保持；胶囊已改深色）
# ★★ task-83（用户第一句抱怨 / QA 实测）：玻璃皮肤原来的深色底不够深 ——
#    控制栏时间白字压在透出的浅色画面上实测只有 2.99:1（WCAG AA 小字要求 ≥4.5:1）。
#    这里把三块玻璃底统一加深（基色更暗 + 不透明度 0.68→0.71~0.74），仍保留
#    26%~29% 背景透过率（"玻璃仍可透出背景"），且**绝不加回文字衬影**。
# ★★★ task-84（用户问题 2/3/7/11）：**清澈冰晶白液态玻璃** —— 废除 task-83 的"脏黑底"。
#    现在的做法：实时区域模糊纹理（widgets.GlassBlurPlate）之上叠
#    rgba(255,255,255,0.52) 亮白琉璃 + 顶部对角光泽，文字改**深墨色**（底层已模糊 ⇒ 可读）。
#    深/浅主题的悬浮窗仍走各自的 palette，只是不透明度压到 0.90~0.92 并垫同款模糊底衬。
# ★★★ task-99：**双液态玻璃主题体系**（唯一核心视觉语言）
#    · glass-light：清透冰晶白磨砂底 rgba(255,255,255,0.55) + 深墨字 #141820 + 冰蓝强调
#    · glass-dark ：通透曜石黑磨砂底 rgba(20,24,32,0.62) + 纯白字 + 顶部 1px 镜面高光 + 电青蓝强调
GLASS_CAPSULE_DARK = "#FFFFFF8C"   # rgba(255,255,255,0.549) 控制栏胶囊（浅色玻璃）
GLASS_TITLEBAR_BG = "#FFFFFF8C"    # rgba(255,255,255,0.549) 玻璃标题栏
GLASS_PANEL_DARK = "#FFFFFF8C"     # rgba(255,255,255,0.549) 设置面板 / 抽屉
GLASS_FILL_PANEL = GLASS_PANEL_DARK
GLASS_FILL_DRAWER = "#FFFFFF8C"    # 与面板同值（同色相）
# 深色玻璃（glass-dark）三块底板：rgba(20,24,32,0.62) = alpha 158 = 0x9E
GLASS_DARK_FILL = "#1418209E"
GLASS_DARK_TITLEBAR = "#1418209E"
GLASS_DARK_MENU = "#141820B8"      # 菜单略实一点，保证深底白字可读
GLASS_DARK_OSD = "#141820D9"       # OSD 浮在画面上，再实一点
GLASS_DARK_POPUP = "#1A1F28"       # 独立顶层弹窗：不透明深底
# 深色玻璃的顶部 1px 镜面高光：rgba(255,255,255,0.18) = 46 = 0x2E
GLASS_TOP_HIGHLIGHT = "#FFFFFF2E"
GLASS_TOP_HIGHLIGHT_DIM = "#FFFFFF1F"
GLASS_PANEL_EDGE = "#1418202E"     # rgba(20,24,32,0.18) 白玻璃上的冷灰细边（比纯白边清晰）
GLASS_EDGE = GLASS_PANEL_EDGE     # 玻璃容器统一描边（胶囊 / 抽屉 / SpecBadge 同源）
GLASS_EDGE_STRONG = GLASS_PANEL_EDGE
GLASS_TITLEBAR_LINE = "#14182026"  # 标题栏底边 1px 分界
# ★★ task-84②：玻璃模式**深墨文字体系**（在清透白琉璃上优雅清晰）
GLASS_TEXT = "#141820"            # 主文字/标题：近黑墨色
GLASS_TEXT_DIM = "#4A4F57"        # 次级（≈ rgba(74,79,87) 实色，QSS color 只吃 6 位 hex）
GLASS_TEXT_MUTED = "#7C828B"      # 辅助
GLASS_TEXT_SHADOW = "#00000000"   # 已彻底废除文字衬影（task-81）
# ★★ task-84②：玻璃专属强调色（废除死蓝渐变）：浅蓝底 + 深蓝描边
GLASS_ACCENT = "#2080FA"          # ★ task-99：**冰蓝强调色**（比旧死蓝更亮更"冰"）
GLASS_ACCENT_SOFT = "#409EFF47"   # rgba(64,158,255,0.28) 选中/主按钮底板
GLASS_ACCENT_EDGE = "#2080FAA6"   # rgba(32,128,250,0.65) 1.5px 描边
GLASS_ACCENT_TEXT = "#123A66"     # 主按钮文字（深蓝黑）
GLASS_GHOST_FILL = "#FFFFFF66"    # rgba(255,255,255,0.40) 次按钮底
GLASS_BTN_PAD = "0 18px"          # 文字左右各 18px（配合 minWidth = advance + 36）
# ★★ task-84③：悬浮窗（菜单 / OSD）同样走冰晶白
GLASS_MENU_BG = "#FFFFFF8C"       # rgba(255,255,255,0.55) 菜单自绘底（下垫模糊纹理）
GLASS_POPUP_SOLID = "#F7F9FC"     # 独立顶层弹窗（下拉/提示条）**不透明**浅底：防文字穿透
GLASS_OSD_BG = "#FFFFFF8C"        # OSD：冰晶白（深字，浮在画面上仍高对比）
# 有色琉璃控件配方（进度条 / 激活胶囊仍在用）
GLASS_BLUE_TOP = "#3898FF9E"      # rgba(56,148,255,0.62)
GLASS_BLUE_BOTTOM = "#0066EBC7"   # rgba(0,102,235,0.78)
GLASS_BLUE_HILITE = "#FFFFFFA6"    # rgba(255,255,255,0.65)
GLASS_BLUE_DEEP = "#0028644D"      # rgba(0,40,100,0.30)
GLASS_EDGE_W = 1.0                # 亮边宽度（规范写死 1px）


# --------------------------------------------------------------------------------------
# 换肤（真实生效的主题预设）
# --------------------------------------------------------------------------------------
# 用户反馈"换肤效果依旧是摆设" —— 之前设置面板里只有一个图标、没有任何预设。
# 这里定义真正会改变界面的预设：每个预设覆盖强调色与玻璃底色，
# 由 MainWindow.apply_theme() 在运行时套用（重设样式表 + 全窗重绘 + 落盘）。
# ======================================================================================
# task-118: THEMES only keeps two liquid-glass themes (legacy palette dicts deleted)
# --------------------------------------------------------------------------------------
# Root cause of the ghost green accent in image mode: legacy theme dicts (mint/...) were
# still present, and some fallback path hit the green mint palette.
#   glass-light accent = #2080FA (ice blue)
#   glass-dark  accent = #3898FF (electric cyan blue)
# Legacy names still map to glass themes through LEGACY_THEME_MAP (names only, no palettes).
# len(THEMES) == 2 is enforced by QA reflection.
# ======================================================================================
class _ThemeTable(dict):
    """Mapping holding only the two real themes; legacy names are string aliases.

    Keeps THEMES.get("qq-light") / THEMES["mint"] from raising KeyError while
    len(THEMES) == 2 and THEMES.keys() lists only the two glass themes.
    """

    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            real = LEGACY_THEME_MAP.get(key)
            if real is not None:
                return dict.__getitem__(self, real)
            raise

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        return dict.__contains__(self, key) or key in LEGACY_THEME_MAP


#: legacy theme name -> glass theme (names only; no legacy palettes)
LEGACY_THEME_MAP = {
    'qq-light': 'glass-light', 'mint': 'glass-light', 'sakura': 'glass-light',
    'qq-dark': 'glass-dark', 'graphite': 'glass-dark', 'aurora': 'glass-dark',
    'amber': 'glass-dark', 'frost': 'glass-light',
}

THEMES: dict[str, dict] = _ThemeTable({
    'glass-light':         {'label': '冰晶玻璃（浅色）',
         'deprecated': False,
         'accent': '#2080FA',
         'accent_dim': '#2080FAA6',
         'accent_soft': '#2080FA47',
         'text_on_accent': '#123A66',
         'bg_base': '#F7F8FA',
         'bg_deep': '#F2F3F5',
         'bg_stage': '#F7F8FA',
         'glass_chrome': '#FFFFFF8C',
         'glass_capsule': '#FFFFFF8C',
         'glass_panel': '#FFFFFF8C',
         'glass_card': '#FFFFFF8C',
         'glass_hover': '#FFFFFF7A',
         'glass_press': '#FFFFFF4D',
         'glass_track': '#14182033',
         'glass_gloss': '#FFFFFF33',
         'input_bg': '#FFFFFF66',
         'scroll_hover': '#14182040',
         'hairline_white': '#FFFFFFCC',
         'icon': '#2A2F38',
         'icon_hover': '#141820',
         'hover_bg': '#0000000F',
         'divider': '#14182014',
         'osd_bg': '#FFFFFF8C',
         'osd_text': '#141820',
         'osd_border': '#1418202E',
         'popup_bg': '#F7F9FC',
         'hairline': '#1418201F',
         'hairline_strong': '#1418202E',
         'inner_glow': '#FFFFFF00',
         'text': '#141820',
         'text_dim': '#4A4F57',
         'text_muted': '#7C828B',
         'danger': '#E64340',
         'warn': '#C87A00',
         'hidden': False,
         'mode': 'light',
         'glass_skin': True,
         'scroll_handle': '#78849652',
         'scroll_handle_hover': '#64707E85',
         'scroll_w': 6,
         'scroll_min': 28,
         'glass_bg': '#FFFFFF8C',
         'glass_border': '#1418202E',
         'glass_menu': '#FFFFFF8C',
         'glass_top_highlight': '#FFFFFF40'},
    'glass-dark':         {'label': '曜石玻璃（深色）',
         'deprecated': False,
         'accent': '#3898FF',
         'accent_dim': '#3898FF73',
         'accent_soft': '#3898FF33',
         'text_on_accent': '#062A45',
         'bg_base': '#262626',
         'bg_deep': '#1E1E1E',
         'bg_stage': '#1A1A1A',
         'glass_chrome': '#1418209E',
         'glass_capsule': '#1418209E',
         'glass_panel': '#1418209E',
         'popup_bg': '#1A1F28',
         'glass_card': '#1418209E',
         'glass_hover': '#FFFFFF1A',
         'glass_press': '#FFFFFF0A',
         'glass_track': '#FFFFFF2E',
         'glass_gloss': '#FFFFFF0F',
         'input_bg': '#FFFFFF14',
         'scroll_hover': '#FFFFFF40',
         'hairline': '#FFFFFF29',
         'hairline_strong': '#FFFFFF3D',
         'inner_glow': '#FFFFFF00',
         'divider': '#FFFFFF1A',
         'text': '#FFFFFF',
         'text_dim': '#D6DCE6',
         'text_muted': '#AEB6C2',
         'danger': '#FF6B6B',
         'warn': '#FFC46B',
         'hidden': False,
         'mode': 'dark',
         'glass_skin': True,
         'icon': '#D0D6E0',
         'icon_hover': '#FFFFFF',
         'glass_bg': '#1418209E',
         'glass_border': '#FFFFFF3D',
         'glass_titlebar': '#1418209E',
         'glass_menu': '#141820B8',
         'glass_top_highlight': '#FFFFFF2E',
         'scroll_handle': '#A0AFC359',
         'scroll_handle_hover': '#BECDE185',
         'scroll_w': 6,
         'scroll_min': 28,
         'hairline_white': '#FFFFFFB3',
         'osd_bg': '#141820D9',
         'osd_text': '#FFFFFF',
         'osd_border': '#FFFFFF3D'},
})


# ★ task-99：启动默认主题 = 冰晶玻璃（旧平面主题已废弃）
DEFAULT_THEME = "glass-light"



def apply_theme(name: str) -> bool:
    """把预设套进 PALETTE（原地改，所有读 P[...] 的地方立刻拿到新值）。

    ⚠️ 先**整体重置回基准深色**再套预设。只做 update 的话，浅色主题把 `text`
    改成深色之后，切回深色主题时预设里没写 `text`，文字就永远留在深色 —— 看不见了。
    预设只写"和基准不一样的部分"，靠这一步保证来回切换是干净的。

    返回是否真的换了（同一个主题重复调用返回 False，调用方可以跳过重绘）。
    """
    # ★ task-118：旧主题名（qq-light/mint/...）按**字符串映射**归一化到两套玻璃主题，
    #    绝不回退到任何旧配色字典（它们已物理删除）。
    real = LEGACY_THEME_MAP.get(name, name)
    if not dict.__contains__(THEMES, real):
        return False
    preset = dict.__getitem__(THEMES, real)
    if P.get("_theme") == real:
        return False
    P.clear()
    P.update(_BASE_PALETTE)
    for k, v in preset.items():
        # ⚠️ 预设里除颜色外还有**元数据键**（label / hidden / mode / glass_skin），
        #    不能写进调色板 —— 它们不是颜色，会被 css()/qt_color() 当成非法色值。
        if k not in _PRESET_META_KEYS:
            P[k] = v
    # 胶囊浮在画面上，**任何主题**都不允许它透到读不出字 —— 统一设一个下限（约 47%）。
    # 浅色"霜白"那套要的就是高透，所以下限比深色主题松，但不能不设。
    #
    # ⚠️ 本项目的调色板一律用 **CSS 顺序 `#RRGGBBAA`**（alpha 在**最后两位**），
    #    由 `qt_color()` / `css()` 负责转成 QColor。
    #    **不要在 QSS 里裸写 `{P['x']}`** —— Qt 会把 8 位十六进制按 `#AARRGGBB` 解析，
    #    于是 `#FFFFFF1A`（本意白 10%）渲染成不透明的 `#FFFF1A`（亮黄）。
    #    一律写 `{css(P['x'])}`。见知识库 §9.43。
    # ⚠️ 唯一例外：**液态玻璃皮肤**（glass_skin=True）。它的定义就是"高透玻璃底板"
    #    （卡片规范 0.12~0.18），加了这条下限它就不可能达标；可读性由浅色族的
    #    深墨文字（#1D2129 系）保证。除此之外任何主题都不许绕过这条下限。
    if not preset.get("glass_skin"):
        cap = P["glass_capsule"]
        if len(cap) == 9:
            alpha = int(cap[7:9], 16)
            if alpha < 0x78:
                P["glass_capsule"] = cap[:7] + "78"
    P["_theme"] = real
    # 明暗族：预设显式声明 mode（glass-light=light / glass-dark=dark）。
    P["_mode"] = "light" if preset.get("mode") == "light" else "dark"
    return True


def is_light() -> bool:
    return P.get("_mode") == "light"


def is_glass_skin() -> bool:
    """当前是否是「液态玻璃」皮肤（预设里标了 glass_skin=True 的那一套）。

    ⚠️ 读的是**预设元数据**而不是调色板：glass_skin 是标记、不是颜色，
       apply_theme 不会把它写进 P（P 里只允许颜色值，见 _PRESET_META_KEYS）。
       所以**不能**写 P.get("glass_skin") —— 那样永远是 False（我第一版就踩了：
       徽章在玻璃皮肤下仍走浅色族的深墨分支）。
    """
    return bool(THEMES.get(P.get("_theme"), {}).get("glass_skin"))


def is_deprecated(name: str) -> bool:
    """★ task-118：旧平面主题已**物理删除**（不存在可回退的旧配色字典）。

    该函数保留仅为兼容旧调用：当前两套玻璃主题都不是 deprecated，恒返回 False；
    旧主题名经 LEGACY_THEME_MAP 视为玻璃主题（同样 False）。
    """
    return bool(THEMES.get(name, {}).get("deprecated"))


def theme_names(include_hidden: bool = False) -> list[str]:
    """**全局**主题列表（标题栏日夜按钮 / 任何按主题枚举的地方都走它）。

    ★ task-99：只返回**非废弃**主题；默认再过滤 hidden 项。
    当前 UI 可用集合 = ["glass-light", "glass-dark"]（双液态玻璃体系）。
    """
    names = [n for n, v in THEMES.items()
             if isinstance(v, dict) and not v.get("deprecated")]
    if include_hidden:
        return names
    return [n for n in names if not THEMES[n].get("hidden")]


def theme_choices() -> list[tuple[str, str]]:
    """★ task-99：给 UI 下拉用的 (name, label) 列表 —— 只含未废弃主题。"""
    return [(n, str(THEMES[n].get("label") or n)) for n in theme_names()]


def normalize_theme_name(name: str) -> str:
    """旧主题名 / 未知主题名统一归一化到当前两套玻璃主题（启动读旧配置时用）。

    ★ task-118：旧名只走 LEGACY_THEME_MAP 字符串映射；旧配色字典已物理删除。
    """
    try:
        real = LEGACY_THEME_MAP.get(str(name), str(name))
        if dict.__contains__(THEMES, real) and not is_deprecated(real):
            return real
    except Exception:
        pass
    return DEFAULT_THEME


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
    "input_h": 32,                  # 输入类控件（下拉/单行/数值）统一内容高度
                                    # ⚠️ 数值框的 sizeHint 天生比别人高 2px（上下按钮），
                                    #    所以这里取 32 ⇒ 实际都是 34，实测已对齐
    "menu_min_w": 210,              # 弹出菜单最小宽度（自绘菜单行用）
    "menu_radius": 10,              # 菜单圆角（面板绘制 + 窗口遮罩共用同一个值）
    "input_radius": 8,              # 与按钮、卡片同族，别一处一个数

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
    "media_info_w": 330,            # Tab 媒体信息浮层的正文宽度（左上角）
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

# --------------------------------------------------------------------------------------
# 界面字体
# --------------------------------------------------------------------------------------
# 内置 **HarmonyOS Sans SC**（华为，免费商用；其许可**明确允许** embed / bundle / redistribute，
# 条件是：保留版权声明与协议原文、不得修改字体、软件内要有显著声明）。
# 授权原文见 assets\fonts\LICENSE-HarmonyOS-Sans.txt（**必须随包分发**）。
#
# 为什么内置而不是继续用系统字体：
#   ① 用户反馈"某些情况下字体重合" —— 系统字体回退 + **假粗（synthetic bold）**是主因。
#      内置 Regular + Medium + Bold 三个**真实字重**，Qt 不需要自己合成粗体。
#   ② QSS 里写的族名必须真的存在，否则 Qt **静默回退**（实测回退成宋体后，
#      小字号下笔画会挤在一起，正是用户截图里那个样子）。
#
# ⚠️ 曾试过 **MiSans（小米）**：外观更"设计感"，但小米官方许可**明确禁止再分发字体文件**
#    （"不得…进一步分发字体软件或其任何副本"），所以**不能随包**，已放弃。
BUNDLED_FONT_FAMILY = "HarmonyOS Sans SC"
_BUNDLED_FONT_FILES = ("HarmonyOS_Sans_SC_Regular.ttf",
                       "HarmonyOS_Sans_SC_Medium.ttf",
                       "HarmonyOS_Sans_SC_Bold.ttf")
# ★★ task-81（用户问题 8）：UI 字体**锁定系统字体** —— Segoe UI 优先、Microsoft YaHei UI 兜底。
#    内置 HarmonyOS Sans SC 不再作为 UI 首选：它在 150% 缩放下小字号笔画偏糊/发虚，
#    正是"笔画重合发糊"的观感来源之一（内置字体仍注册，歌词等需要时可显式引用）。
_UI_FONT_LOCKED = ("Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei")
_FALLBACK_STACK = _UI_FONT_LOCKED

# QSS 用的字体栈；`install_bundled_fonts()` 只注册内置字体，不再改变 UI 首选族。
FONT_FAMILY = ", ".join('"%s"' % f for f in _FALLBACK_STACK) + ", sans-serif"
_UI_FONT_STACK: list = list(_FALLBACK_STACK)


def _assets_dir() -> str:
    """定位 assets\\：打包后随包放在 _internal\\assets，源码运行时在工程根。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        cand = [os.path.join(base, "assets"),
                os.path.join(os.path.dirname(sys.executable), "assets")]
    else:
        here = os.path.dirname(os.path.abspath(__file__))          # src/zhijian/ui
        cand = [os.path.abspath(os.path.join(here, "..", "..", "..", "assets"))]
    for c in cand:
        if os.path.isdir(c):
            return c
    return ""


def install_bundled_fonts() -> str:
    """把 assets\\fonts 里的字体注册进 Qt，返回实际生效的族名（失败返回空串）。

    ⚠️ 必须在 `QApplication` 建好之后、`setFont()` 与 `build_stylesheet()` **之前**调用。
    ⚠️ 只有 `addApplicationFont()` 注册过的族名才能被 QSS 引用；写错名字**不报错**，
       只会静默回退到系统字体（见文件头注释 ②）。
    """
    global FONT_FAMILY, _UI_FONT_STACK
    fams: list = []
    d = _assets_dir()
    if d:
        try:
            from PySide6.QtGui import QFontDatabase
            for fn in _BUNDLED_FONT_FILES:
                p = os.path.join(d, "fonts", fn)
                if not os.path.isfile(p):
                    continue
                fid = QFontDatabase.addApplicationFont(p)
                if fid >= 0:
                    fams.extend(QFontDatabase.applicationFontFamilies(fid))
        except Exception:
            fams = []
    ok = BUNDLED_FONT_FAMILY in fams
    # ★ task-81：UI 字体族**不随内置字体改变**（锁定 Segoe UI / Microsoft YaHei UI）。
    _UI_FONT_STACK[:] = list(_FALLBACK_STACK)
    FONT_FAMILY = ", ".join('"%s"' % f for f in _UI_FONT_STACK) + ", sans-serif"
    return "" if ok else ""          # 内置字体仍注册（歌词可用），但不再作为 UI 字体上报


def bundled_font_dir() -> str:
    """内置字体所在目录（给 mpv 的 `sub-fonts-dir` 用）；找不到返回空串。

    为什么需要它：Qt 的 `addApplicationFont` 只让 **Qt 自己**看得见字体，
    mpv 渲染字幕走的是另一套字体查找（fontconfig / DirectWrite）——
    不把目录告诉 mpv，字幕里选"内置字体"就是选了没反应。
    """
    d = _assets_dir()
    if not d:
        return ""
    p = os.path.join(d, "fonts")
    return p if os.path.isdir(p) else ""


def ui_font_stack() -> list:
    """给 `QFont.setFamilies()` 用的字体栈（内置优先、系统兜底）。"""
    return list(_UI_FONT_STACK)


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
# 弹出菜单：**只挂给菜单控件自己的**样式表
# --------------------------------------------------------------------------------------
# ⚠️ 不能写进应用级 QSS：应用级里一旦出现 `QMenu` 规则，QStyleSheetStyle 就会给菜单窗口
#    挂一层平台投影（本项目踩过"一层暗一层阴"）。
# 面板底色与圆角由 `widgets.RoundedMenu.paintEvent` 自己画，这里只管**菜单项**。
def menu_qss() -> str:
    # ★ task-78③：玻璃模式 —— 菜单 = 半透磨砂琉璃（RoundedMenu 自绘底 + 1px 白亮边），
    #    文字/快捷键**纯白系**；既有 7 套主题保持原字符串（逐字不变）。
    if is_glass_skin():
        return (
            "QMenu{background:transparent;border:none;padding:6px;}"
            f"QMenu::item{{padding:7px 22px 7px 14px;color:{P['text']};border-radius:8px;}}"
            f"QMenu::item:selected{{background:transparent;color:{P['accent']};}}"
            f"QMenu::item:disabled{{color:{P['text_muted']};}}"
            f"QMenu::separator{{height:1px;background:{css(P['hairline'])};margin:5px 8px;}}"
            f"QMenu::right-arrow{{width:10px;height:10px;margin-right:10px;}}"
        )
    return (
        "QMenu{background:transparent;border:none;padding:6px;}"
        f"QMenu::item{{padding:7px 22px 7px 14px;color:{P['text_dim']};border-radius:8px;}}"
        # ★ task-70：QMenu 原生 selected 背景**必须透明** —— 快速划过菜单时 Qt 的
        #    currentAction 可能滞后，原生蓝底 + 自绘行 hover 会同时出现两块高亮。
        #    高亮统一由 widgets.MenuRow 自绘（全局唯一排他）。
        f"QMenu::item:selected{{background:transparent;color:{P['accent']};}}"
        f"QMenu::item:disabled{{color:{P['text_muted']};}}"
        f"QMenu::separator{{height:1px;background:{css(P['hairline'])};margin:5px 8px;}}"
        f"QMenu::right-arrow{{width:10px;height:10px;margin-right:10px;}}"
    )


# --------------------------------------------------------------------------------------
# 全局样式表
# --------------------------------------------------------------------------------------

# ★★ task-39②（用户 ⑪）：**QSS 模板缓存** —— 换肤时不再每次全量重解析模板。
#   键 = 主题名 + 关键调色板变量（防止"P 被改但 _theme 没变"时读到陈旧样式）。
_QSS_CACHE: dict = {}


def _qss_cache_key() -> tuple:
    try:
        return (str(P.get("_theme") or ""), str(P.get("bg_base") or ""), str(P.get("bg_deep") or ""),
                str(P.get("accent") or ""), str(P.get("text_main") or ""), bool(is_glass_skin()))
    except Exception:
        return ("?",)


_QSS_STATS = {"hits": 0, "miss": 0}


def qss_cache_stats() -> dict:
    """★ task-39 结构指标锚点：模板缓存命中/重解析次数（比 ms 更能证明改造有效）。"""
    return dict(_QSS_STATS, entries=len(_QSS_CACHE))


def build_stylesheet() -> str:
    """★ task-39②：**带内存模板缓存的 QSS 构建**（换肤热路径）。

    原全量实现降为 `_build_stylesheet_raw()`；键 = 主题名 + 关键调色板变量，
    命中则直接返回上次结果 ⇒ 同主题重复切换/多处重复取样式不再重解析模板。
    """
    k = _qss_cache_key()
    hit = _QSS_CACHE.get(k)
    if hit is not None:
        _QSS_STATS["hits"] += 1
        return hit
    _QSS_STATS["miss"] += 1
    s = _build_stylesheet_raw()
    if len(_QSS_CACHE) > 32:            # 有界（主题数很少，32 足够且防异常增长）
        _QSS_CACHE.clear()
    _QSS_CACHE[k] = s
    return s


def _build_stylesheet_raw() -> str:
    # ⚠️ 抽屉底板：既有 7 套主题照旧走 QSS（与 v1.0.36 **逐字节相同**）；
    #    液态玻璃皮肤改由 PlaylistDrawer.paintEvent 用 paint_glass_plate() 自绘 ——
    #    因为本 Qt 版本里 QSS 的 rgba 背景会被**合成两次**（实测：alpha 41 渲染成 75，
    #    1-(1-0.161)² = 0.296），像素必然冲出卡片要求的 [0.12,0.18] 带。
    if is_glass_skin():
        _drawer_bg, _drawer_border = "transparent", "none"
        # ★ task-72：标题栏彻底透明 —— WindowBody 在玻璃皮肤下也透明，
        #    让标题栏区域直接透出窗后模糊背景（不再是一块纯白卡片）。
        _body_bg = "transparent"
        _body_border = "1px solid " + css(P['hairline_strong'])
        # ★★ task-84②：玻璃按钮**废除死蓝渐变** —— 浅蓝琉璃底 + 1.5px 深蓝描边 + 深蓝黑文字。
        #    与冰晶白面板同一套语言：底色是 rgba(64,158,255,0.28)，描边 rgba(32,128,250,0.65)。
        _primary_bg = css(GLASS_ACCENT_SOFT)
        _primary_edge = "border: 1.5px solid %s;" % css(GLASS_ACCENT_EDGE)
        _primary_color = GLASS_ACCENT_TEXT
        _primary_pad = GLASS_BTN_PAD
        _ghost_bg = css(GLASS_GHOST_FILL)
        _ghost_color = P['text']
        _ghost_edge = "1px solid " + css(GLASS_PANEL_EDGE)
        _ghost_pad = GLASS_BTN_PAD
        _ghost_hover_bg = css("#FFFFFF99")
        _link_color = P['accent']
    else:
        _drawer_bg = css(P['glass_chrome'])
        _drawer_border = "1px solid " + css(P['hairline'])
        _body_bg = css(P['bg_base'])
        _body_border = "1px solid " + css(P['hairline_strong'])
        _primary_bg = css(P['accent'])
        _primary_edge = "border: none;"
        _primary_color = P['text_on_accent']
        _primary_pad = "8px 22px"
        _ghost_bg = css(P['input_bg'])
        _ghost_color = P['text_dim']
        _ghost_edge = "1px solid " + css(P['hairline_strong'])
        _ghost_pad = "8px 20px"
        _ghost_hover_bg = css(P['glass_hover'])
        _link_color = P['accent']
    # ⚠️ QToolTip（原生提示条）是**独立顶层窗口**（无边框 + 半透明），底色必须不透明，
    #    否则背后的字会透上来（task-05 封版项）。既有 7 套主题沿用 glass_panel ——
    #    它们的面板色本来就接近不透明，行为**逐位不变**；液态玻璃皮肤的面板色是
    #    0.13 的高透白，所以对玻璃皮肤改走 popup_bg（不透明实底）。
    #    ⇒ 这条三元表达式对 7 套既有主题输出的是与 v1.0.36 **完全相同**的字符串。
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
        background: {_body_bg};
        border: {_body_border};
        border-radius: {M['win_radius']}px;
    }}
    /* ⚠️ 全屏必须**去掉这条 1px 描边和圆角**：浅色主题下它就是贴着屏幕边缘的一圈白线，
       圆角还会把画面的四个角再裁一次（DWM 那边已经关过了，这是 Qt 侧画的第二层）。
       用户原话："全屏了视频本身也还是被裁剪成一个圆角画面…还有白边"。 */
    QWidget#WindowBody[fullscreen="true"] {{
        border: none;
        border-radius: 0;
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
        background: {_drawer_bg};
        border: {_drawer_border};
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
    QLabel[role="h1"] {{ font-size: 14px; font-weight: 500; color: {P['text']}; }}
    /* 说明文字：比正文小一号、弱一档色，**一行说完**（长段落交给 tooltip） */
    QLabel[role="sub"] {{ font-size: 11px; color: {P['text_muted']}; }}
    /* 13px 的弱化说明（空状态副标题那类） */
    QLabel[role="hint"] {{ font-size: 13px; color: {P['text_muted']}; }}
    /* 12px 的次级色（图片缩放比例那类） */
    QLabel[role="dim12"] {{ font-size: 12px; color: {P['text_dim']}; }}
    /* 标题栏那条竖分隔线：以前是内联样式，换肤后不会跟着变 */
    QWidget[role="vsep"] {{ background: {css(P['hairline_strong'])}; }}
    QLabel[role="body"] {{ color: {P['text']}; }}
    QLabel[role="h2"] {{ font-size: 13px; font-weight: 500; color: {P['text']}; }}
    QLabel[role="dim"] {{ color: {P['text_dim']}; }}
    QLabel[role="muted"] {{ color: {P['text_muted']}; font-size: 12px; }}
    QLabel[role="accent"] {{ color: {P['accent']}; }}

    QToolTip {{
        background: {css(P['popup_bg'] if is_glass_skin() else P['glass_panel'])};
        color: {P['text']};
        border: 1px solid {css(P['hairline_strong'])};
        border-radius: 6px;
        padding: 5px 9px;
    }}

    /* ---- 滚动条：极细、悬停才明显 ---- */
    QScrollBar:vertical {{
        background: transparent; width: {P.get('scroll_w', 10)}px; margin: 2px 2px 2px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {css(P.get('scroll_handle') or P['glass_track'], 1.0)};
        min-height: {P.get('scroll_min', 32)}px; border-radius: 3px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {css(P.get('scroll_handle_hover') or P['scroll_hover'])};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{
        background: transparent; height: {P.get('scroll_w', 10)}px; margin: 0 2px 2px 2px;
    }}
    QScrollBar::handle:horizontal {{
        background: {css(P.get('scroll_handle') or P['glass_track'])};
        min-width: {P.get('scroll_min', 32)}px; border-radius: 3px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {css(P.get('scroll_handle_hover') or P['scroll_hover'])};
    }}
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
        background: {css(P['accent_soft'])}; color: {P['accent']}; font-weight: 500;
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
        border-radius: {M['input_radius']}px; padding: 0 10px; min-height: {M['input_h']}px;
        color: {P['text']};
    }}
    QComboBox:hover {{ border-color: {css(P['accent_dim'])}; }}
    QComboBox:focus {{ border-color: {css(P['accent'])}; }}
    QComboBox::drop-down {{ border: none; width: 24px; background: transparent; }}
    /* ⚠️ 弹窗是**独立的顶层窗口**，只给它设 border-radius 是没用的（外层窗口仍是方的）。
       真正的圆角要靠代码把弹窗设成 无边框 + 半透明（见 widgets.round_popup()），
       这里负责"里面那层"的圆角与**不透明**底色 —— 底色半透明就会文字穿透。 */
    QComboBox QAbstractItemView {{
        background: {P['popup_bg']};
        border: 1px solid {css(P['hairline_strong'])};
        border-radius: 10px; padding: 6px;
        /* ★ task-78③：玻璃模式 = 磨砂琉璃底 + 1px 白亮边 + **纯白文字** */
        color: {P['text']};
        selection-background-color: {css(P['accent_soft'])};
        selection-color: {P['text']};
        outline: none;
    }}
    /* ⚠️ 行高给足：min-height 24 + padding 2 时，13px 字的**降部会被裁掉**
       （实测截图里 "YaHei" 的下半截没了）。28 + 上下 4 才完整。 */
    /* ⚠️ 这条规则对应的 Qt 原生下拉视图**已经不用了**（弹窗改由 SurfacePopup 自管，
        hover/选中样式在 surface.py 里统一写）。留个空注释说明，避免以后有人以为
        "下拉样式写在这里"而改错地方。 */
    /* ⚠️ 弹窗里的滚动条**不能贴到边**：它会以方形画到右下角，
       把圆角切掉的那个角重新填成不透明（实测右下角 alpha=176）。
       注意选择器要**同时**覆盖 container 与 view 两种父子关系 ——
       QComboBox 的滚动条挂在哪个下面跟 Qt 版本有关，只写一种会漏。 */
    QComboBox QAbstractItemView QScrollBar:vertical,
    QComboBoxPrivateContainer QScrollBar:vertical {{
        background: transparent; width: 8px; margin: 2px 2px 2px 0;
    }}
    QComboBox QAbstractItemView QScrollBar::add-line:vertical,
    QComboBox QAbstractItemView QScrollBar::sub-line:vertical,
    QComboBoxPrivateContainer QScrollBar::add-line:vertical,
    QComboBoxPrivateContainer QScrollBar::sub-line:vertical {{
        height: 0; background: transparent; border: none;
    }}
    QComboBox QAbstractItemView QScrollBar::add-page:vertical,
    QComboBox QAbstractItemView QScrollBar::sub-page:vertical,
    QComboBoxPrivateContainer QScrollBar::add-page:vertical,
    QComboBoxPrivateContainer QScrollBar::sub-page:vertical {{
        background: transparent;
    }}
    /* ⚠️ 这里**不再需要**给 Qt 的私有弹窗容器写样式了：下拉弹窗已改成自管的
       `SurfacePopup`（见 surface.py），根本不会创建 QComboBoxPrivateContainer。
       （合规闸门 build\\_check_surfaces.py 会把这类写法判为违规。） */

    QLineEdit {{
        background: {css(P['input_bg'])}; border: 1px solid {css(P['hairline_strong'])};
        border-radius: {M['input_radius']}px; padding: 0 10px; min-height: {M['input_h']}px;
        color: {P['text']};
        selection-background-color: {css(P['accent_soft'])};
        selection-color: {P['accent']};
    }}
    QLineEdit:focus {{ border-color: {css(P['accent'])}; }}

    QSlider::groove:horizontal {{
        height: 4px; background: {css(P['glass_track'])}; border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{
        background: {css(P['accent'])}; border-radius: 2px;
    }}
    /* ⚠️ 圆点必须"宽=高"才是正圆：横向滑块的**高度**由 槽高 + margin×2 决定，
       而宽度由 `width` 决定。原来写 width:12 + margin:-5px ⇒ 实际 12×14 的椭圆
       （用户："滑动条的圆点有点丑，不是标准的圆"）。现在槽 4 + margin -5/-5 = 14 高，
       宽度也取 14、圆角取一半 7 ⇒ 正圆。 */
    QSlider::handle:horizontal {{
        width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
        background: {P['text']};
    }}
    QSlider::handle:horizontal:hover {{ background: {css(P['accent'])}; }}

    QSpinBox, QDoubleSpinBox {{
        background: {css(P['input_bg'])}; border: 1px solid {css(P['hairline_strong'])};
        border-radius: {M['input_radius']}px; padding: 0 8px;
        /* ⚠️ 数值框天生比下拉/单行高 2px（上下按钮那点内边距），
           所以这里减 2 —— 实测三者的最终高度才真的都是 34。 */
        min-height: {M['input_h'] - 2}px;
        color: {P['text']};
        selection-background-color: {css(P['accent_soft'])};
        selection-color: {P['accent']};
    }}
    QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {css(P['accent'])}; }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        width: 18px; border: none; background: transparent;
    }}

    /* ★★ task-78②：标准绘制顺序 = 先圆角背景 + 1px 边，**最后一步**才画纯白文字。
       QSS 的 color 由样式引擎在背景之后绘制 ⇒ 不存在"高光盖住文字"的叠加层。
       左右 padding 0 18px + 最小宽度（widgets 里按 fontMetrics 动态设 minWidth）⇒ 永不截断。 */
    QPushButton[role="primary"] {{
        background: {_primary_bg}; color: {_primary_color};
        {_primary_edge} border-radius: 8px; padding: {_primary_pad};
        min-height: 22px; font-weight: 500;
    }}
    QPushButton[role="primary"]:hover {{ background: {_primary_bg}; color: {_primary_color}; }}
    QPushButton[role="primary"]:pressed {{ background: {_primary_bg}; color: {_primary_color}; }}
    QPushButton[role="ghost"] {{
        background: {_ghost_bg}; color: {_ghost_color};
        border: {_ghost_edge};
        border-radius: 8px; padding: {_ghost_pad};
    }}
    QPushButton[role="ghost"]:hover {{ background: {_ghost_hover_bg}; color: {P['text']}; }}
    QPushButton[role="link"] {{
        background: transparent; border: none; color: {_link_color}; padding: 6px 4px;
    }}
    QPushButton[role="link"]:hover {{ color: {css(_link_color, 0.8)}; }}

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
