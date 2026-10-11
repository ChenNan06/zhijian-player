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
        # 用户："文字下拉框常态下取消实心深灰底色，改用轻盈的微透底 + 1px 细线"
        "input_bg": "#0000000A", "scroll_hover": "#00000033",
        "hairline_white": "#FFFFFF99",   # 1px 半透明白内发光（QQ NT 的软边）
        "icon": "#4E5969", "icon_hover": "#1D2129",   # 图标常态/悬停
        "hover_bg": "#0000000F",         # 悬停底色 rgba(0,0,0,0.06)
        "divider": "#E5E6EB",
        # 浅色模式下 OSD 也走浅色（用户："提示操作的悬浮窗为什么在浅色模式下还是深色的"）
        "osd_bg": "#FFFFFFF0", "osd_text": "#1D2129", "osd_border": "#00000014",
        # ⚠️ 弹出层（菜单 / 下拉弹窗 / 提示条）必须**不透明**：
        #    glass_panel 是半透明的（#FFFFFFDB = 86%），而弹窗窗口是"无边框+半透明"的，
        #    两者一叠加，背后的设置文字就**透上来了**（用户截图："文字穿透"）。
        "popup_bg": "#FFFFFF",
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
        "popup_bg": "#2A2A2A",            # 弹出层用不透明底（理由见浅色主题那段注释）
        "glass_card": "#3B3B3B",          # 卡片比底亮 —— QQ 夜间的关键
        "glass_hover": "#FFFFFF14", "glass_press": "#FFFFFF0A",
        "glass_track": "#FFFFFF24", "glass_gloss": "#FFFFFF00",
        "input_bg": "#FFFFFF0F", "scroll_hover": "#FFFFFF3D",
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
_FALLBACK_STACK = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")

# QSS 用的字体栈；`install_bundled_fonts()` 会把内置族名插到最前面。
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
    _UI_FONT_STACK = ([BUNDLED_FONT_FAMILY] if ok else []) + list(_FALLBACK_STACK)
    FONT_FAMILY = ", ".join('"%s"' % f for f in _UI_FONT_STACK) + ", sans-serif"
    return BUNDLED_FONT_FAMILY if ok else ""


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
    return (
        "QMenu{background:transparent;border:none;padding:6px;}"
        f"QMenu::item{{padding:7px 22px 7px 14px;color:{P['text_dim']};border-radius:8px;}}"
        f"QMenu::item:selected{{background:{css(P['accent_soft'])};color:{P['accent']};}}"
        f"QMenu::item:disabled{{color:{P['text_muted']};}}"
        f"QMenu::separator{{height:1px;background:{css(P['hairline'])};margin:5px 8px;}}"
        f"QMenu::right-arrow{{width:10px;height:10px;margin-right:10px;}}"
    )


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
        selection-background-color: {css(P['accent_soft'])};
        selection-color: {P['accent']};
        outline: none;
    }}
    /* ⚠️ 行高给足：min-height 24 + padding 2 时，13px 字的**降部会被裁掉**
       （实测截图里 "YaHei" 的下半截没了）。28 + 上下 4 才完整。 */
    QComboBox QAbstractItemView::item {{
        min-height: 28px; padding: 4px 10px; border-radius: 6px;
    }}
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

    QPushButton[role="primary"] {{
        background: {css(P['accent'])}; color: {P['text_on_accent']};
        border: none; border-radius: 8px; padding: 8px 22px; font-weight: 500;
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
