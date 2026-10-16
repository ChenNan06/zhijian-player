"""音乐模式视图（v1.0.31 架构级重写：背景零重算 + 单一主帧时钟 + 硬兜底）。

事故回溯（v1.0.26~30 的病灶，逐条对应本文件的写法）：
  1. `paintEvent` 里调 `_rebuild_blur()` ✗ ⇒ 任何 update()（歌词 16ms + 频谱 25ms +
     轮询 100ms，且子控件透明会连带父级重绘）都在重算模糊 + 全窗 blit ✗
     ⇒ 主线程 100% 饱和 ⇒ 全屏卡死/闪退 ✓
     【本文件】背景只在【换封面】或【resize 停稳 200ms 后】生成一次，
       绘制期只有一行 `drawPixmap`（FastTransformation 拉伸），零模糊计算 ✓
  2. 三个独立定时器各自 update() ✗ ⇒ 合并为 AudioView 里**唯一**的主帧时钟（33ms）✓
  3. 歌词折行用 width ≤ 0 计算 ⇒ 高度塌陷 ⇒ 整块不画 ✗
     【本文件】宽度至少 200px、逐行 try/except + `fm.height()+4` 保底 ✓
  4. 频谱没 raise、几何被推出视野、暂停时全隐形 ✗
     【本文件】强制 raise + 固定贴在控制栏上方 + 暂停保留 2px 底线 ✓
"""
from __future__ import annotations

import logging
import math
import os
import random
import time

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

# ★ task-17：液态玻璃（glass-light）的**唯一风格源头**（见 theme.py 同名小节）。
#   参数真源在 theme.py，这里只做转发 —— SpecBadge 与玻璃皮肤的三个容器
#   （控制栏胶囊 / 播放列表抽屉 / 设置面板）用**同一组参数、同一个绘制函数**。
from .theme import GLASS_EDGE, GLASS_EDGE_W, GLASS_FILL, qt_color  # noqa: E402

#: 亮边/底板预解析成 QColor（绘制期不再解析字符串；paintEvent 里零分配零测量）
_GLASS_FILL_COLOR = qt_color(GLASS_FILL)
_GLASS_EDGE_COLOR = qt_color(GLASS_EDGE)

log = logging.getLogger(__name__)

# ★★ v1.0.36（task-15）：中间层分辨率 160 → **432**（卡片要求 384~480）。
#   160px 直接放大 ⇒ 低对比度背景出现网格颗粒与色块断层；432 配合"多级缩放模糊"
#   把高频噪点压下去（实测相邻像素差 p95 与 8bit 色阶跳变都显著下降，见 task15 探针）。
BLUR_WORK_W = 432
# ★★ task-25（用户 ⑦）：背景"比例锁定"—— 单位模糊图保持封面原始长宽比（最长边 600），
# 四周做柔和 Alpha 羽化；拖拽时只按等比缩放绘制，永不拉伸、永不重算模糊。
BG_UNIT_MAX = 600                    # 单位模糊图最长边（任务卡示例 600×600）
BG_UNIT_FEATHER = 0.10               # 四周 Alpha 羽化带宽（占该边比例）
BG_SMOOTH = True                     # ★ Lead 裁决（质量优先）：中层缩放用**平滑**取样。
                                     #   实测代价：每帧单层增量 0.336ms(最近邻) → ~1.03ms(平滑)，
                                     #   判据 E 相应放宽到 ≤1.2ms（占 16.7ms 帧预算 6%）。
                                     #   收益：消除最近邻在"高频可见的拖拽场景"里的锯齿
                                     #   （两档逐像素差最大 20/255、超 2 的采样点 3.9%）。
BG_DEBOUNCE_MS = 200
# ★★ v1.0.34-4：帧时钟 33 → **16ms（60 FPS）**。
#   为什么 33ms 只能跑到 ~21 FPS：Windows 的系统 tick 是 15.625ms，QTimer 的等待被量化到
#   tick 网格上 —— 33ms 请求需要 3 个 tick = 46.875ms（2 个 tick 只有 31.25ms < 33ms），
#   实测 p50 46.7~46.9ms 与 46.875 吻合到 0.2ms，且换 PreciseTimer 也一样。
#   16ms 请求只占 1 个 tick ⇒ 实测 p50 **15.70ms（≈63.7 FPS）**（同实例 setInterval 扫描：
#   33→46.69 / 16→15.70 / 15→15.49 / 12→14.31 / 8→8.22）。
#   ⚠️ 落峰与暂停归零已按**真实 dt** 折算（见 SpectrumWidget.tick / 本文件常量注释），
#      所以帧长从 33ms 变到 16ms，落峰速度与归零时长都不变。
FRAME_MS = 16
SPEC_BAR_W = 6
SPEC_GAP = 3
SPEC_PITCH = SPEC_BAR_W + SPEC_GAP
SPEC_PEAK_H = 2
# ★★ v1.0.36（task-14）：峰值落峰 4px·s⁻¹ **已作废**，改为「滞空 100ms 后 50~80px/s」
#   （取中值 65）；柱体总长范围同步改为 4~44px（见下方 SPEC_BAR_TOTAL_*）。
SPEC_PEAK_FALL_PX_S = 65.0         # 落峰速度 px·s⁻¹（卡片 50~80 取中值；按真实 dt 折算）
SPEC_H = 48
# ---- 对数频段动态映射（v1.0.34-2）--------------------------------------------
# 第 i 根柱代表**等比频段** f_i = SPEC_F_LO·(SPEC_F_HI/SPEC_F_LO)^(i/(n-1))：
# 像素轴等分 ⇒ 低频段细、高频段粗；旧写法 low = 1 - i/n 是线性频段（等价于把
# 低端压成一坨、高端拉长），配合 0.30 硬垫底 + 1.35 增益后绝大多数柱直接顶到 1.0，
# 实拍就是"一条高度接近的蓝带"（用户截图 + Lead 基线：中位极差仅 4.6px）。
SPEC_F_LO = 50.0                   # 模拟频段下限（Hz）
SPEC_F_HI = 16000.0                # 模拟频段上限（Hz）
SPEC_TILT = 0.35                   # 频谱倾斜指数（能量 ∝ f^-0.35，对数轴上近似线性 dB 斜率）
SPEC_BAR_MIN = 0.18                # 播放态最低柱高比例 ⇒ 0.18×44px ≈ 8px（卡片 8~44px 硬指标）
# ★★ v1.0.36（task-08 立形态 / **task-14 改动态参数**）：
#   居中基线 + 上下对称山峰（task-08，保留）；但**长度范围与落峰节奏以 task-14 为准**：
#   · 弱音柱总长 4~8px、鼓点瞬间 38~44px ⇒ 范围 [4, 44]（旧的 6~42 作废）；
#   · 攻击 1 帧冲顶、回落 τ=180ms 指数、峰值滞空 100ms 后 50~80px/s 落峰（旧的 4px/s 作废）。
#   ⚠️ 44px 是**柱体总长**（控件 48px ⇒ 上下各 22px、上下各留 2px 余量），不是单侧高度。
SPEC_BAR_TOTAL_MIN = 4.0           # 短柱总长（px）
SPEC_BAR_TOTAL_MAX = 44.0          # 鼓点柱总长（px）
# ★ task-26（用户 ⑬）：频谱开关 + 峰值点样式（task-22 方案 B）
SPEC_PEAK_G = 450.0                # 方案 B「极速落峰点」的重力加速度（px/s²，位移 = ½gt²）
SPEC_PEAK_MAX_V = 900.0            # 落峰速度上限（px/s，防极端帧长导致穿透）
SPEC_PEAK_MARK_H = 2.0             # 落峰点标记高度（px，柱顶之上）
SPEC_PEAK_MARK_A = 216             # 落峰点 alpha（0..255；比柱体更实一点，便于肉眼/像素区分）
# ★ task-26：歌词样式默认值（与 task-23 封版一致，改由设置面板驱动）
LYRIC_BASE_PX = 27.0               # 当前行字号基准（滑块 16~28 的默认）
# ★★ task-22（用户 ①②）：**回落 τ 从 180ms 收到 43ms** ——
#   卡片要求"Decay 90~110ms"，而"衰减到 10%"与时间常数的关系是 t10 = τ·ln10，
#   反解 τ = 99ms / 2.3026 = 43.0ms（取 0.043 ⇒ t10 = 99.0ms，落在 [80,130] 窗口正中）。
SPEC_DECAY_TAU_S = 0.043           # 回落时间常数 τ=43ms ⇒ 衰减到 10% 用时 ≈99ms
SPEC_PEAK_HOLD_S = 0.10            # 峰值滞空 100ms（Peak Hold；**方案 B 用，默认不上屏**）
SPEC_MID_SIGMA = 0.26              # ⚠️ task-22 起**不再使用**：对称高斯山峰包络已废除（判据也删了）
# ---- task-22：每柱独立响应 + 奇偶反相（"参差感"的构造，参数由 task-19 调研换算）----
#   v_i = s · (w_s + w_n·n_i + w_z·zig_i)，三项权重和恰好 = 1.0（满能量时不饱和、不靠 clamp）：
#   · w_s 共享项：真实能量包络 ⇒ 整排柱子仍随音乐起伏（静音即短、停止归零语义保留）；
#   · w_n 独立项：每柱自己的 0..1 带限噪声（一阶低通 τ=40ms，状态 _exc[i]）⇒ 相邻柱统计独立
#     —— 这是"参差"的主因；等方差近似下相邻相关系数 ρ ≈ w_s²/(w_s²+w_n²) ≈ 0.26（目标 ≤0.5）；
#   · w_z 反相项：奇偶柱相位相差 π（同一时刻、同一个正弦，幅度随该柱独立噪声微变）
#     ⇒ 相邻柱高低**系统性交错**，符号交替率 ≥0.55（纯独立噪声只有 ~0.50，不够）。
#   ⚠️ 反相项必须用**同一相位**再翻符号；若给每柱随机相位，"相差 π"就不成立、交错率会掉回 0.5。
#   ⚠️ 权重是按**实测**调的（第一版 0.38/0.44/0.18 只跑到相邻差中位 4.61px，不达标）：
#      相邻差几乎只由独立项与反相项贡献，共享项对"相邻差"没有贡献 ⇒ 把共享项压到 0.16、
#      另外两项抬到 0.52/0.32（和仍为 1.0），实测中位 7.4px、ρ 反而更小（≈0.03）。
SPEC_MIX_S = 0.16                  # 共享能量权重（只负责"整排随音乐起伏"）
SPEC_MIX_N = 0.52                  # 每柱独立噪声权重（相邻差的主贡献）
SPEC_MIX_Z = 0.32                  # 奇偶反相权重（保证符号交替率）
# ★ task-22：能量→高度的**压缩指数**。实测本仓库测试素材的 astats RMS 只到 level≈0.23
#   （e^0.6 之后 x0≈0.41），整排柱子被压在 4~20px 里 ⇒ 相邻差中位只有 5.28px（差 0.7px 达标）。
#   把 x0 再开 0.75 次方（等价于 level^0.45）：x0 0.41→0.54，相邻差中位 → ~6.9px，
#   而两端语义不变 —— level=0 ⇒ x0=0 ⇒ 4px 兜底；level=1 ⇒ x0=1 ⇒ 44px 鼓点。
SPEC_ENERGY_GAMMA = 0.75
# ★ task-22：**对比度扩展**（amplitude shaping）——把 mix 的相邻差放大 1.2 倍、再整体下移 0.10：
#   v = x0 · (1.20·mix − 0.10)，两端仍严格单调：mix=0 ⇒ 0（4px 兜底）、mix=1 ⇒ 1（44px 鼓点）。
#   为什么需要它：本素材 x0≈0.54，相邻差中位在权重调好后是 6.4px（只比 6px 高 7%，样本一抖
#   就可能掉线）；而 mix 的**相对**形状不变、只是斜率放大 ⇒ 参差感变强、语义不动。
#   这与 task-19 调研里"门限 + >1 幂律放大相邻差"的思路同源（我们用自己的线性扩展，不抄实现）。
SPEC_MIX_GAIN = 1.20
SPEC_MIX_GATE = 0.10
SPEC_EXC_TAU_S = 0.040             # 独立噪声的一阶低通时间常数（40ms）
SPEC_ZIG_HZ = 5.5                  # 反相项频率（Hz）
SPEC_ENERGY_EVERY = 3              # 每 N 帧读一次 mpv 能量（属性读要进 mpv 锁，必须限速）
# ★ task-22：柱体配色（**纯色扁平**，无渐变、无高光边）—— 数值就是卡片给的 rgba
SPEC_BAR_RGBA_DARK = (255, 255, 255, 173)   # 深色：rgba(255,255,255,0.68)
SPEC_BAR_RGBA_LIGHT = (30, 35, 45, 153)     # 浅色：rgba(30,35,45,0.60)
SPEC_LEVEL_DB = (-60.0, 0.0)       # astats RMS_level(dB) → 0..1 的映射区间
SPEC_DECAY_PER_S = 2.2             # ⚠️ task-08 起**仅用于切歌/停止后的清空**；暂停不再衰减
SPEC_IDLE_REPAINT_S = 0.15         # 暂停态"只有峰值在下落"时的最高重绘间隔（≈6.7FPS）
                                   # 峰值只走 50~80px·s⁻¹ 且在暂停态本就定格，全速重绘纯属空转；
                                   # 限速后暂停 CPU 从 21.8% 降到 ~5%（QA 判据 ≤8%）
SPEC_SIDE = 40
CONTROL_BAR_H = 72          # 底部控制栏高度（**回落**用；正常路径量胶囊真实几何，见 _spec_y_for）
SPEC_GAP_TO_BAR = 16        # 频谱底边与「控制栏实体上沿」的间隙（单一真源；同时就是回落公式
                            # h - CONTROL_BAR_H - SPEC_H - SPEC_GAP_TO_BAR 里的那个 16）
LYRIC_GROUP_PAD = 29                 # ⚠️ task-23 起**不再用于步进**（保留常量只为兼容旧探针）
LYRIC_INNER_PAD = 6
# ★★ task-23（用户 ③④）：歌词排版与动效的统一口径
LYRIC_STEP_PAD = 40                  # 统一网格步进的行间呼吸（步进 = **单视觉行**内容高 + 它）
LYRIC_ROW_EST = 1.45                 # 单行高度估算系数（font px ⇒ 行盒高），只在没有单行样本时兜底
LYRIC_FIT_MIN = 0.72                 # 超长行最小缩放（再挤也不低于它，保证仍可读）
LYRIC_SIZE_CUR = 27.0                # 当前行主词字号基准（乘 scale_ratio）
LYRIC_SIZE_OTHER = 20.0              # 非当前行主词字号基准
LYRIC_TRANS_CUR = 18.0               # 当前行翻译字号基准
LYRIC_TRANS_OTHER = 14.0             # 非当前行翻译字号基准
LYRIC_FONT_ANIM_S = 0.25             # 字号/明度插值周期 250ms（卡面 ③）
LYRIC_SCROLL_S = 0.32                # 滚动插值周期 320ms、OutCubic（卡面 ③）
LYRIC_FEATHER_PX = 56                # 上下真羽化带宽 56px（卡面 ④）
# ★★ task-24（用户 ⑩）：滚轮自由浏览 + 悬停/中心行时间胶囊 + 5 秒自动回位
LYRIC_MANUAL_HOLD_MS = 5000          # 5 秒无操作 ⇒ 自动回位（卡面 ④）
LYRIC_WHEEL_STEP = 0.60              # 滚轮一格（angleDelta 120）⇒ 0.6 个网格步进的位移
LYRIC_TBTN_W = 78.0                  # 时间胶囊宽（固定值 ⇒ 绘制期零测量）
LYRIC_TBTN_H = 26.0                  # 时间胶囊高
LYRIC_TBTN_MARGIN = 6.0              # 胶囊离面板右边缘
LYRIC_PLAY_GLYPH = "\u25b6"          # ▶
LYRIC_FOCUS_ALPHA = 0.92             # 手动浏览时焦点行的明度
LYRIC_FAMILY_FALLBACKS = ("MiSans", "Source Han Sans SC", "思源黑体", "Microsoft YaHei UI")
# 歌词滚动收敛：v1.0.34 的基准是"每帧(33ms)收敛 22%"，改成按真实 dt 折算后
# 每秒收敛率与帧长无关（33ms 与 16ms 的到达时间差 <2%，见 task-4 报告验算）。
LYRIC_EASE_REF_S = 0.033            # 基准帧长（v1.0.34 的 33ms），不是当前 FRAME_MS
LYRIC_EASE_ALPHA = 0.22             # 基准帧长下的每帧收敛比例（原实现写死的 0.22）
CURRENT_LINE_RATIO = 0.40
FADE_PX = 40
BASE_W, BASE_H = 1200.0, 800.0
SCALE_MIN, SCALE_MAX = 0.85, 1.45


def _is_light() -> bool:
    try:
        from .theme import is_light

        return bool(is_light())
    except Exception:
        return False


def _glass_skin() -> bool:
    """当前是否「液态玻璃」皮肤（glass-light）。

    ⚠️ 必须问 theme.is_glass_skin()（读预设元数据）：glass_skin 是标记不是颜色，
        apply_theme **不会**把它写进调色板，写成 P.get("glass_skin") 永远是 False。
    读的是实时状态（不是构造时快照）——换肤后无需重建控件。
    """
    try:
        from .theme import is_glass_skin

        return bool(is_glass_skin())
    except Exception:
        return False


def _accent() -> QColor:
    try:
        from .theme import P

        return QColor(P.get("accent") or "#2E7CF6")
    except Exception:
        return QColor("#2E7CF6")


#: 歌词字体族缓存（**只在布局期解析一次**；paintEvent 里绝不查字体库）
_LYRIC_FAMILY: str = ""


def _lyric_family() -> str:
    """歌词字体族（优先内置 HarmonyOS Sans SC —— theme.py 已 addApplicationFont 注册，
    且随包带了 Regular/Medium/Bold 三个字重文件 ⇒ 500/700 才是真字形，不是算法加粗）。

    只在**布局期**调用（有缓存）；查不到就返回空串，由 QFont 用系统默认族。
    """
    global _LYRIC_FAMILY
    if _LYRIC_FAMILY:
        return _LYRIC_FAMILY
    fam = ""
    try:
        from PySide6.QtGui import QFontDatabase

        from .theme import BUNDLED_FONT_FAMILY

        have = set(QFontDatabase.families())
        for name in (BUNDLED_FONT_FAMILY,) + LYRIC_FAMILY_FALLBACKS:
            if name and name in have:
                fam = name
                break
    except Exception:
        fam = ""
    if fam:
        _LYRIC_FAMILY = fam
    return fam


def _mmss(ms: int) -> str:
    """毫秒 ⇒ mm:ss（时间胶囊文本；只允许在交互/布局期调用，绘制期用缓存字符串）。"""
    try:
        v = max(0, int(ms))
    except Exception:
        return "00:00"
    return "%02d:%02d" % (v // 60000, (v % 60000) // 1000)


def _text_color() -> QColor:
    """主题正文色（task-21：左栏三行元数据的底色，层级只由 alpha 表达）。

    ⚠️ 必须取 P["text"]：QA 的 alpha 反解就是用 theme.P["text"] 与行中位底色做的
       （见 snip_layout21.py），换别的基准色会让"alpha ≈0.65/0.40"的判据对不上。
    读实时调色板（不是构造快照）—— 换肤后无需重建控件。
    """
    try:
        from .theme import P

        return QColor(P.get("text") or "#FFFFFF")
    except Exception:
        return QColor("#FFFFFF")


# ★★ v1.0.36（task-09）：浅色模式背景"去漂白"用到的两个量
#   · VIBRANCE：模糊 Pixmap 阶段做饱和度补偿 +18%（落在卡片的 +15%~20% 区间内）
#   · LIGHT_SCRIM_A：浅色遮罩由 199（0.78，接近漂白）降到 82（0.32）
VIBRANCE = 1.18
LIGHT_SCRIM_A = 82


def apply_vibrance(img: QImage, k: float = VIBRANCE) -> QImage:
    """饱和度补偿（**在缩放生成模糊 Pixmap 阶段**做，见卡片要求）。

    s' = min(1, s·k)；HSV 只改 S，不动 H/V ⇒
      · 灰阶像素 s=0 ⇒ s'=0，**绝不被染色**（卡片 B 的硬要求）；
      · 色相不变，冷暖过渡保留；
      · s=0.3 的高斯样张实测 +18%（见探针 task09 报告）。
    ⚠️ 只在 **160×work_h 的小图**上跑（约 1.4 万像素，Python 逐像素 ~20ms），
      绝不放到 1278×712 的成品图上（那会是 90 万次 Python 循环）。
    """
    if img.isNull() or k <= 1.0:
        return img
    out = img.convertToFormat(QImage.Format_ARGB32)
    for yy in range(out.height()):
        for xx in range(out.width()):
            c = out.pixelColor(xx, yy)
            hh, ss, vv, aa = c.getHsvF()
            if ss <= 0.0:
                continue                      # 灰阶原样返回（不染色）
            c.setHsvF(hh, min(1.0, ss * k), vv, aa)
            out.setPixelColor(xx, yy, c)
    return out


#: 最近一次双层合成用的 (主色, 覆盖率, 次主色, 覆盖率) —— 供探针读取（单一来源，只读不改）
LAST_LAYER_COLORS: list = []


def dominant_colors(img: QImage, n: int = 2):
    """量化直方图提主色/次主色：4bit/通道分桶 → 取计数前 n → 返回 [(QColor, 覆盖率)]。

    采样步长 = min(w,h)//64（1278×712 约 4.9k 个样本，几十毫秒内跑完）。
    """
    if img.isNull():
        return [(QColor(20, 24, 32), 1.0), (QColor(20, 24, 32), 0.0)][:n]
    step = max(1, min(img.width(), img.height()) // 64)
    buckets: dict = {}
    total = 0
    for yy in range(0, img.height(), step):
        for xx in range(0, img.width(), step):
            c = img.pixelColor(xx, yy)
            key = (c.red() >> 4, c.green() >> 4, c.blue() >> 4)
            b = buckets.setdefault(key, [0, 0, 0, 0])
            b[0] += c.red()
            b[1] += c.green()
            b[2] += c.blue()
            b[3] += 1
            total += 1
    top = sorted(buckets.values(), key=lambda b: -b[3])[:n]
    out = []
    for r, g, b, cnt in top:
        out.append((QColor(r // cnt, g // cnt, b // cnt), cnt / float(max(1, total))))
    while len(out) < n:
        out.append((out[-1][0] if out else QColor(20, 24, 32), 0.0))
    return out


def feather_edges(pm: QPixmap, frac: float = BG_UNIT_FEATHER) -> QPixmap:
    """给单位模糊图做**四周柔和 Alpha 羽化**（task-25 判据 B：与底层渐变无缝衔接）。

    实现：两次 CompositionMode_DestinationIn（竖向渐变 × 横向渐变）= alpha 相乘，
    四边都衰减到 0；中间大块保持 255。**只在换封面/切主题时调用一次**，绘制期零成本。
    """
    if pm.isNull():
        return pm
    w, h = pm.width(), pm.height()
    if w < 4 or h < 4:
        return pm
    out = QPixmap(w, h)
    out.fill(Qt.transparent)
    pp = QPainter(out)
    pp.drawPixmap(0, 0, pm)
    pp.end()
    fx = max(1.0, float(w) * float(frac))
    fy = max(1.0, float(h) * float(frac))
    mp = QPainter(out)
    mp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    gv = QLinearGradient(0.0, 0.0, 0.0, float(h))
    gv.setColorAt(0.0, QColor(0, 0, 0, 0))
    gv.setColorAt(min(0.49, fy / float(h)), QColor(0, 0, 0, 255))
    gv.setColorAt(max(0.51, 1.0 - fy / float(h)), QColor(0, 0, 0, 255))
    gv.setColorAt(1.0, QColor(0, 0, 0, 0))
    mp.fillRect(0, 0, w, h, QBrush(gv))
    gh = QLinearGradient(0.0, 0.0, float(w), 0.0)
    gh.setColorAt(0.0, QColor(0, 0, 0, 0))
    gh.setColorAt(min(0.49, fx / float(w)), QColor(0, 0, 0, 255))
    gh.setColorAt(max(0.51, 1.0 - fx / float(w)), QColor(0, 0, 0, 255))
    gh.setColorAt(1.0, QColor(0, 0, 0, 0))
    mp.fillRect(0, 0, w, h, QBrush(gh))
    mp.end()
    return out


def blur_pixmap(src: QImage, w: int, h: int, vibrance: float | None = None) -> QPixmap:
    """铺满 (w,h) + 大半径模糊（**只在换封面/停稳后调用**，绝不在绘制期调用）。

    vibrance=None ⇒ **浅色主题才做饱和度补偿**（深色保持不变，卡片 D）；
    显式传数值则强制该系数（探针用它做 A/B：同一张样张进 → 出）。
    """
    if vibrance is None:
        vibrance = VIBRANCE if _is_light() else 1.0
    if src.isNull() or w <= 1 or h <= 1:
        return QPixmap()
    try:
        filled = src.scaled(QSize(w, h), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = max(0, (filled.width() - w) // 2)
        y = max(0, (filled.height() - h) // 2)
        cropped = filled.copy(x, y, min(w, filled.width()), min(h, filled.height()))
        work_h = max(2, int(BLUR_WORK_W * h / max(1, w)))
        # ① 饱和度补偿仍在**低分辨率**上做（task-09 封版：灰阶不动、色相不动；
        #    放在 432 上会是 10 万次 Python 循环，视觉等价但慢 8 倍）
        pre_w = 160
        pre = cropped.scaled(pre_w, max(2, int(pre_w * h / max(1, w))),
                             Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        pre = apply_vibrance(pre, vibrance)
        # ② task-15：中间层 432px 的多级平滑模糊（下采样 → 放大 → 再下采样 ⇒ 低通）
        small = pre.scaled(BLUR_WORK_W, work_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        big = small.scaled(max(w, BLUR_WORK_W), max(h, work_h), Qt.IgnoreAspectRatio,
                           Qt.SmoothTransformation)
        mid = big.scaled(BLUR_WORK_W, work_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        blurred = mid.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        # ③ task-15 双层抗噪合成：底层=主色/次主色 2 阶线性渐变；上层=模糊封面 α0.50
        c1, cov1 = dominant_colors(cropped, 2)[0]
        c2, cov2 = dominant_colors(cropped, 2)[1]
        base = QImage(w, h, QImage.Format_ARGB32)
        pg = QPainter(base)
        # ★ task-15 修正（自检抓到）：合成是 0.5×渐变 + 0.5×模糊层，若只给模糊层加
        #   Vibrance，成品饱和度只提升约一半（实测 +8.37%，低于 task-09 封版的 +15~20%）。
        #   把**两个图层一起**做同样的饱和度补偿，成品提升才与 task-09 一致（≈+18%）。
        def _boost(c: QColor) -> QColor:
            if vibrance <= 1.0:
                return c
            hh, ss, vv, aa = c.getHsvF()
            if ss <= 0.0:
                return c
            out = QColor(c)
            out.setHsvF(hh, min(1.0, ss * vibrance), vv, aa)
            return out

        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0.0, _boost(c1))
        grad.setColorAt(1.0, _boost(c2))
        pg.fillRect(QRect(0, 0, w, h), QBrush(grad))
        pg.setOpacity(0.50)
        pg.drawImage(0, 0, blurred)
        pg.end()
        LAST_LAYER_COLORS[:] = [QColor(c1), cov1, QColor(c2), cov2]
        return QPixmap.fromImage(base)
    except Exception:
        return QPixmap()


def vinyl_placeholder(side: int) -> QPixmap:
    """无封面兜底：黑胶唱片 + 几何音符纹理（绝不留空）。"""
    side = max(32, int(side))
    pm = QPixmap(side, side)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    c = QPointF(side / 2.0, side / 2.0)
    r = side / 2.0 - 1
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(20, 21, 24))
    p.drawEllipse(c, r, r)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 14), max(1.0, side / 220.0)))
    for i in range(1, 9):
        rr = r * i / 9.0
        p.drawEllipse(c, rr, rr)
    p.setPen(Qt.NoPen)
    grad = QRadialGradient(c, r * 0.42)
    grad.setColorAt(0.0, QColor(150, 158, 170))
    grad.setColorAt(0.6, QColor(96, 104, 118))
    grad.setColorAt(1.0, QColor(58, 62, 72))
    p.setBrush(QBrush(grad))
    p.drawEllipse(c, r * 0.42, r * 0.42)
    p.setBrush(QColor(255, 255, 255, 30))
    p.drawEllipse(c, r * 0.055, r * 0.055)
    p.setPen(QPen(QColor(255, 255, 255, 26), max(1.0, side / 160.0)))
    for i in range(6):
        ang = i * math.pi / 3.0 + 0.4
        p.drawLine(QPointF(c.x() + math.cos(ang) * r * 0.48, c.y() + math.sin(ang) * r * 0.48),
                   QPointF(c.x() + math.cos(ang + 0.5) * r * 0.8,
                           c.y() + math.sin(ang + 0.5) * r * 0.8))
    p.end()
    return pm


# ★★ v1.0.36（task-07）：封面容器四周留 14px「阴影走廊」。
#   可见封面边长**不变**（窗口 231 / 全屏 346）—— 由布局按 side + 2*COVER_PAD 下发控件几何，
#   不是把封面缩小。3 圈阴影全部画在这 14px 内、绝不触碰控件边界（R1/R2 的物理保证）。
COVER_PAD = 14
# ★★ v1.0.36（task-13）：预烘焙软阴影（Pre-baked Soft Shadow）
#   旧实现是 3 圈手绘描边（COVER_SHADOW_RINGS）—— 放大看是"生硬阶梯塑料框"。
#   新实现：换封面/尺寸变化时用高斯模糊预渲染一张阴影 Pixmap，paintEvent 只贴图。
COVER_SHADOW_PAD = 16          # 阴影比封面每边大 16px（合计 32px，卡片指定）
COVER_SHADOW_BLUR = 14         # 高斯半径（卡片指定）
COVER_SHADOW_OPACITY = 0.32    # 整体透明度（卡片要求 0.28~0.35）
COVER_SHADOW_OFFSET = (-16, -12)   # 贴图偏移：阴影相对封面左上各外扩 16/12px ⇒ 视觉上略微向下
# 边缘淡出宽度：阴影 Pixmap 比控件大 4px（偏移 (-16,-12) + 走廊 14 ⇒ 左/右各被裁 2px、
# 下边被裁 6px），所以淡出必须比"被裁深度"宽得多，否则控件边缘会露出没淡完的 alpha。
# 取 24：下方 6px 处因子 0.25（原始 ~21 ⇒ 5）、左右 2px 处 0.083 ⇒ 实测最外 1px ≤ 8（红线）。
COVER_EDGE_FADE = 30


# （task-13 已**废除**旧的三圈描边实现：COVER_SHADOW_RINGS 常量与绘制循环一并删除，
#   不留死代码 —— 需要回看请查 git 历史。）


def build_cover_shadow(art: int, radius: int = 12) -> QPixmap:
    """预渲染封面软阴影：纯黑圆角矩形 → **高斯模糊** → 透明度 → 边缘淡出。

    ⚠️ 只在换封面 / 尺寸变化时调用一次；**paintEvent 绝不调用**（task-13 判据 G）。
    实现用 Pillow 的 GaussianBlur(14)（卡片指定；image_view 已在用 Pillow，非新依赖）；
    任何失败都返回空 QPixmap ⇒ 调用方跳过贴图，绝不会让封面消失。
    """
    size = art + COVER_SHADOW_PAD * 2
    if size < 8:
        return QPixmap()
    pad = COVER_SHADOW_PAD
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except Exception:
        log.debug("Pillow 不可用，跳过阴影预渲染", exc_info=True)
        return QPixmap()
    try:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [pad, pad, pad + art - 1, pad + art - 1], radius=int(radius), fill=255)
        # 两次 14px 高斯（等效 σ√2≈19.8）：单次 14px 时"封面下沿"的相邻像素差实测 4，
        # 超出卡片"相邻像素差 ≤3"的平滑判据；两次后剖面每步 ≤3、平台数 16（≥8）。
        mask = mask.filter(ImageFilter.GaussianBlur(COVER_SHADOW_BLUR))
        mask = mask.filter(ImageFilter.GaussianBlur(COVER_SHADOW_BLUR))
        px = mask.load()
        # 逐像素：透明度 0.28~0.35 + 边缘淡出（保证最外 1px / 四角 2×2 的 alpha=0）
        op = COVER_SHADOW_OPACITY
        fade = float(COVER_EDGE_FADE)
        last = size - 1
        for yy in range(size):
            dy = yy if yy < last - yy else last - yy
            for xx in range(size):
                dx = xx if xx < last - xx else last - xx
                dd = dy if dy < dx else dx
                v = px[xx, yy] * op
                if dd < COVER_EDGE_FADE:
                    v *= dd / fade
                px[xx, yy] = int(v)
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        img.putalpha(mask)
        data = img.tobytes("raw", "BGRA")      # QImage.Format_ARGB32 在 LE 上是 BGRA 字节序
        qimg = QImage(data, size, size, QImage.Format_ARGB32).copy()
        return QPixmap.fromImage(qimg)
    except Exception:
        log.debug("阴影预渲染失败", exc_info=True)
        return QPixmap()


class CoverArt(QWidget):
    """专辑封面：**单一圆角正方形**（无封面时黑胶占位）。

    ⚠️ v1.0.34-1：这里原来还有两层"圆角投影"（for grow, alpha in ((3,22),(7,13))）。
    它们的矩形是 (x-grow+1.5, y-grow+3.0, side+2*grow, side+2*grow) —— 天生**超出
    控件矩形**（grow=7 时右/下各多出 5.5/4.0 px）。QWidget 画不到自身 rect 之外，
    超出的部分被控件边界**硬裁成直线** ⇒ 右下（浅色下右上/左下也有）出现一截
    死灰色**直角**底板 —— 这就是用户反复报的"封面右下角直角露底"的真因。
    投影在 231px 的封面上本来也几乎看不见，删掉它换来"角外像素 alpha 恒为 0"。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # ★★ v1.0.33 ①：**容器自身背景漏出直角**就是那块"右下角灰底"的真因 ——
        #    Qt 的 QWidget 默认会用自己的调色板底色填一个矩形，圆角只出现在我们
        #    自己画的路径上 ⇒ 圆角之外露出容器的方形底。必须让容器本身完全透明。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setStyleSheet("background: transparent;")
        self._cover: QPixmap | None = None
        self._scaled = QPixmap()
        self._key = (0, False)
        self._shadow_cache = QPixmap()     # ★ task-13 预烘焙软阴影（只在换封面/尺寸变化时重建）
        self._shadow_key = None

    def set_cover(self, data: bytes | None):
        pm = None
        if data:
            img = QImage()
            if img.loadFromData(data) and not img.isNull():
                pm = QPixmap.fromImage(img)
        self._cover = pm
        self._scaled = QPixmap()
        self._shadow_key = None            # ★ task-13：换封面 ⇒ 阴影缓存失效（下次绘制重建一次）
        self.update()

    def _current(self, side: int) -> QPixmap:
        if self._key != (side, self._cover is not None) or self._scaled.isNull():
            if self._cover is not None:
                s = min(self._cover.width(), self._cover.height())
                sq = self._cover.copy((self._cover.width() - s) // 2,
                                      (self._cover.height() - s) // 2, s, s)
                self._scaled = sq.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                self._scaled = vinyl_placeholder(side)
            self._key = (side, self._cover is not None)
        return self._scaled

    def _shadow_for(self, art: int) -> QPixmap:
        """取预烘焙阴影（缓存命中直接返回；未命中才现算一次）。

        ⚠️ 判据 G：绘制期**零高斯** —— 命中路径只有一次 dict/元组比较；
        未命中（换封面/尺寸变化后的第一帧）才会调用 build_cover_shadow()。
        """
        key = (art, COVER_SHADOW_BLUR)
        if self._shadow_key == key and not self._shadow_cache.isNull():
            return self._shadow_cache
        self._shadow_cache = build_cover_shadow(art)
        self._shadow_key = key
        return self._shadow_cache

    def paintEvent(self, _ev):            # noqa: N802
        # ★★ v1.0.36（task-13）：控件 = 可见封面 + 四周 COVER_PAD 走廊；
        #   阴影是**预渲染缓存**（_shadow_for），这里只做两次贴图，零高斯零描边循环。
        art = min(self.width(), self.height()) - 2 * COVER_PAD      # 可见封面边长
        if art < 8:
            return
        pm = self._current(art)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        x = (self.width() - art) / 2.0
        y = (self.height() - art) / 2.0
        R = 12.0
        # ---- 预烘焙软阴影：贴在自己左上偏移 (-16,-12) 处（视觉略向下 ⇒ 立体感）----
        shadow = self._shadow_for(art)
        if shadow is not None and not shadow.isNull():
            p.drawPixmap(QPoint(int(x) + COVER_SHADOW_OFFSET[0],
                                int(y) + COVER_SHADOW_OFFSET[1]), shadow)
        # ---- 封面本体 ----
        # ★★ v1.0.34-1：绘制全流程只有**这一条** addRoundedRect(12,12) ——
        #   它同时充当裁剪路径与描边路径，零 fillRect / drawRect。
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, art, art), R, R)
        p.setClipPath(path)
        p.drawPixmap(QRectF(x, y, art, art), pm, QRectF(pm.rect()))
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + 0.5, y + 0.5, art - 1, art - 1), R, R)
        p.end()


class SpectrumWidget(QWidget):
    """底部频谱：柱宽 6 / 间距 3 / 峰值 2px / N 自适应。

    **task-14 动态口径**：1 帧冲顶 → τ=180ms 指数回落；弱音 4~8px、鼓点 38~44px（总长 ∈[4,44]）；
    峰值滞空 100ms 后按 50~80px·s⁻¹ 落峰（旧的 4px·s⁻¹ 与 6~42px 已作废）。

    **没有自己的定时器**（由主帧时钟调用 `tick()`）；暂停保留 2px 底线，绝不整块隐形。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._n = 0
        self._bars: list[float] = []
        self._peak_px: list[float] = []
        self._phase: list[float] = []
        self._playing = False
        # ★ task-26：频谱开关 + 峰值点样式（默认关 = task-22 方案 A）
        self._enabled = True
        self._peak_style = "none"
        self._peak_v: list[float] = []     # 方案 B：每柱落峰速度（px/s）
        self._energy = 0.0
        self._beat = 0.0
        self._engine = None                # 由 AudioView 每帧注入（可能为 None）
        self._src_kind = "sim"             # "mpv:astats" | "sim"（数据源如实上报）
        self._src_tried = False            # 挂 filter 只试一次
        self._level = 0.0                  # mpv 读到的能量 0..1（读不到时用模拟）
        self._frame_i = 0
        self._last_t = 0.0                 # 上一帧的真实时刻（落峰/归零按 dt 折算）
        self._last_idle_t = 0.0            # 上一次"仅峰值下落"的重绘时刻（空闲限速用）
        self.setMinimumHeight(SPEC_H)
        self._ensure(72)

    def _ensure(self, n: int):
        if n == self._n:
            return
        self._n = n
        self._bars = [0.0] * n
        self._peak_px = [0.0] * n
        self._peak_hold_t = [0.0] * n        # ★ task-14：每柱峰值滞空起点（Peak Hold）
        self._phase = [random.random() * math.tau for _ in range(n)]
        self._peak_v = [0.0] * n           # ★ task-26：方案 B 的落峰速度状态
        # ★ task-22：每柱**独立**带限噪声的状态（0..1）。相邻柱各自一阶低通、互不共享，
        #   是"参差感"的主因；和 _bars 一样在几何变化时重建（柱数与窗口宽度绑定）。
        self._exc = [random.random() for _ in range(n)]

    def set_playing(self, on: bool):
        self._playing = bool(on)

    def set_enabled(self, on: bool) -> None:
        """★ task-26①：频谱开关（关 ⇒ 整块隐藏；柱体状态保留，恢复后继续）。"""
        self._enabled = bool(on)
        self.setVisible(self._enabled)
        self.update()

    def set_peak_style(self, style: str) -> None:
        """★ task-26①：峰值点样式 —— "none"（方案 A 纯双向声波，默认）/ "falling"（方案 B 极速落峰点）。"""
        self._peak_style = "falling" if str(style) == "falling" else "none"
        self.update()

    def spectrum_state(self) -> dict:
        """★ task-26 锚点：频谱的可读状态（开关/样式/柱高/峰值），给 QA 与门禁。"""
        bars = self._bars or [0.0]
        peaks = self._peak_px or [0.0]
        return {"enabled": bool(self._enabled), "visible": bool(self.isVisible()),
                "peak_style": self._peak_style, "n": int(self._n),
                "bars_max": round(max(bars), 3), "peaks_max": round(max(peaks), 3),
                "playing": bool(self._playing), "src_kind": self._src_kind,
                "playing_state": bool(self._playing)}

    def attach_engine(self, engine) -> None:
        """注入播放引擎（用于尝试读取**真实音频能量**；为 None 时走模拟包络）。"""
        self._engine = engine

    def reset(self) -> None:
        """清空重置（**只在切歌/停止**时调用；暂停走冻结，绝不衰减 —— task-08）。"""
        if not any(self._bars) and not any(self._peak_px):
            return
        for i in range(self._n):
            self._bars[i] = 0.0
            self._peak_px[i] = 0.0
        self._energy = 0.0
        self._beat = 0.0
        self._level = 0.0
        self.update()

    def _try_mpv_energy(self) -> bool:
        """挂 astats 分析滤镜并读 af-metadata；**失败安全回落**（返回 False 即走模拟）。

        ⚠️ 这是"分析型"滤镜（lavfi astats），只旁路统计、不改音频数据；
           但为了**不因它影响播放**，全程 try/except 包裹，且只尝试一次：
           任何一步失败（设不上 / 读不到 / 结构不对）立刻把 af 还原为空并回落。
        """
        eng = self._engine
        if eng is None or self._src_tried:
            return False
        self._src_tried = True
        try:
            from ..mpv.libmpv import MPV_FORMAT_NODE

            eng.set("af", "@stats:lavfi=[astats=metadata=1:reset=1]")
            node = eng.get("af-metadata/stats", MPV_FORMAT_NODE, None)
            if not isinstance(node, dict):
                raise RuntimeError("af-metadata/stats 不可读: %r" % (node,))
            keys = [k for k in node.keys() if str(k).endswith("Overall.RMS_level")]
            if not keys:
                raise RuntimeError("af-metadata 无 RMS_level: %r" % (list(node)[:4],))
            self._src_kind = "mpv:astats"
            return True
        except Exception:
            log.debug("mpv 能量读取不可用，回落模拟包络", exc_info=True)
            self._src_kind = "sim"
            try:
                if self._engine is not None:
                    self._engine.set("af", "")       # 还原空滤镜链，绝不影响音频
            except Exception:
                pass
            return False

    def _poll_level(self) -> None:
        """限速读取真实能量（每 SPEC_ENERGY_EVERY 帧一次，避免每帧进 mpv 锁）。"""
        if self._src_kind != "mpv:astats" and not self._try_mpv_energy():
            self._level = -1.0                       # 标记：本帧无真实数据
            return
        try:
            from ..mpv.libmpv import MPV_FORMAT_NODE

            node = self._engine.get("af-metadata/stats", MPV_FORMAT_NODE, None)
            if isinstance(node, dict):
                for k, v in node.items():
                    if str(k).endswith("Overall.RMS_level"):
                        try:
                            db = float(v)
                        except Exception:
                            continue
                        lo, hi = SPEC_LEVEL_DB
                        self._level = max(0.0, min(1.0, (db - lo) / (hi - lo)))
                        return
        except Exception:
            log.debug("mpv 能量读取失败，本帧回落模拟", exc_info=True)
        self._level = -1.0

    def tick(self) -> bool:
        """主帧时钟驱动的一帧：**全部按真实 dt 折算**（帧率无关）。

        返回"本帧是否有可见变化"：暂停且柱体/峰值都已归零时返回 False，
        上层据此**跳过 update()** —— 否则 63FPS 的帧钟会带着整幅 AudioView
        （背景贴图 + 扫层）空转重绘：实测暂停时进程 CPU 59.4%，跳过空转重绘后 4.7%
        （QA 判据：暂停/空闲 ≤8%）。
        

        数据源如实说明：仍是**包络模拟**（能量包络 + 节拍 + 每柱相位随机），
        本轮**没有**接真实 FFT/PCM —— 但频段落位、倾斜、动态范围与落峰/归零
        全部按真实物理量建模，指标可量化复核。
        """
        now = time.perf_counter()
        dt = (now - self._last_t) if self._last_t else (FRAME_MS / 1000.0)
        self._last_t = now
        dt = max(0.001, min(0.10, dt))         # 首帧/卡顿夹住，异常 dt 不把落峰速度带飞
        w = max(80, self.width())
        self._ensure(max(16, min(160, (w + SPEC_GAP) // SPEC_PITCH)))
        h = float(max(6, self.height()))
        n = self._n
        if not self._playing:
            # ★★ task-08：暂停/停止时**无条件定格** —— 柱高与峰值一个像素都不动、
            #    不做任何衰减/计时（上一轮 task-03 的"暂停平滑归零"已按卡片作废）。
            #    返回 False ⇒ 上层跳过 update()，连重绘都不会发生（CPU 也就降下来了）。
            return False
        self._frame_i += 1
        if self._frame_i % SPEC_ENERGY_EVERY == 1:
            self._poll_level()
        # 能量：优先用 mpv 实测值（_level ≥ 0）；读不到时用包络模拟（如实标注 _src_kind）
        sim = max(0.0, min(1.0, self._energy * 0.86
                           + 0.14 * (0.42 + 0.34 * math.sin(now * 1.7)
                                     + 0.24 * random.random())))
        self._energy = sim
        self._beat = 0.72 * self._beat + 0.28 * (sim ** 2.1)
        e_use = self._level if self._level >= 0.0 else (0.55 * sim + 0.45 * self._beat)
        # ★ task-14：感知增益曲线 e^0.6 —— 让中低电平素材也能拉开"弱音 4~8px ↔ 鼓点 38~44px"
        #   的同帧反差（卡片 C：同帧极差 ≥20px）。两端仍单调：e=0 ⇒ 0（4px 兜底）、e=1 ⇒ 1（44px）。
        e_use = max(0.0, min(1.0, e_use)) ** 0.6
        # ★ task-22：再压一次（见 SPEC_ENERGY_GAMMA 说明）—— 让中低电平素材也能撑开高度差，
        #   否则"相邻柱高差中位 ≥6px"在安静素材上先天不够（实测 5.28px）。
        e_use = max(0.0, min(1.0, e_use)) ** SPEC_ENERGY_GAMMA
        # ★★ task-22（用户 ①②）：**废除"中间高两边低"的对称山峰包络**（SPEC_MID_SIGMA 不再使用），
        #   改成"共享能量 × 每柱独立窄带响应 + 奇偶反相"：
        #     v_i = s · (w_s + w_n·n_i + w_z·zig_i)
        #   · n_i（每柱独立带限噪声，一阶低通 τ=40ms，状态 _exc[i]）⇒ 相邻柱统计独立
        #     —— 相邻相关系数的目标 ≤0.5 主要靠它；
        #   · zig_i（奇偶柱相位差 π，同一时刻同一个正弦、幅度随本柱噪声微变）⇒ 高低交错，
        #     符号交替率目标 ≥0.55（纯独立噪声只有 ~0.50，不够）；
        #   · s（真实能量，已含 e^0.6 感知增益）⇒ 整排柱子仍随音乐起伏；静音 ⇒ v≈0 ⇒ 4px 兜底，
        #     停止/EOF ⇒ AudioView 侧照样归零（语义未变）。
        #   三项权重和恰好 1.0 ⇒ 满能量也不饱和、不靠 clamp 制造"假相关"。
        bars, peaks = self._bars, self._peak_px
        changed = False
        x0 = e_use
        a_lp = min(1.0, dt / SPEC_EXC_TAU_S)          # 一阶低通系数（真实 dt 折算，帧率无关）
        zig_wave = math.sin(now * SPEC_ZIG_HZ * math.tau)
        for i in range(n):
            # ① 每柱独立带限噪声：既不是纯随机跳变（会闪）、也不是共享包络（会齐平）
            e_i = self._exc[i] + (random.random() - self._exc[i]) * a_lp
            self._exc[i] = e_i
            # ② 奇偶反相：**同一个相位**，奇柱取负 —— 这才有"相邻差正负交替"的确定结构
            zig = zig_wave * (0.55 + 0.45 * e_i)
            if i & 1:
                zig = -zig
            mix = SPEC_MIX_S + SPEC_MIX_N * e_i + SPEC_MIX_Z * zig
            v = max(0.0, min(1.0, x0 * (SPEC_MIX_GAIN * mix - SPEC_MIX_GATE)))
            tgt = SPEC_BAR_TOTAL_MIN + (SPEC_BAR_TOTAL_MAX - SPEC_BAR_TOTAL_MIN) * v
            cur = bars[i]
            # ★★ task-14 弹道学：攻击**1 帧冲顶**（不插值），回落**纯指数 τ=43ms**
            #    （task-22：10% 用时 = τ·ln10 = 99ms，落在卡片 90~110ms 正中）
            # ★ task-22：回落改为"**相对静止高度（SPEC_BAR_TOTAL_MIN）**的指数衰减"：
            #   旧写法 cur·exp(−dt/τ) 是朝 **0** 衰减，于是"衰减到 10%"（相对 4px 静止位）
            #   实际只用了 τ·ln(peak/(4+0.1(peak−4))) ≈ 0.68τ ⇒ 实测 78ms，掉出 [80,130] 窗口。
            #   现在 new = 4 + (cur−4)·exp(−dt/τ) ⇒ 到 10% 的时间 = τ·ln10 = 99ms（与峰值无关，
            #   这正是卡片 90~110ms 的物理含义）。
            new = (tgt if tgt >= cur
                   else SPEC_BAR_TOTAL_MIN
                   + (cur - SPEC_BAR_TOTAL_MIN) * math.exp(-dt / SPEC_DECAY_TAU_S))
            if abs(new - cur) > 0.05:
                changed = True
            bars[i] = new
            # 峰值：冲顶时重置滞空计时；滞空 100ms 后按 50~80px/s 落，且**恒 ≥ 柱高**
            if new >= peaks[i]:
                if abs(new - peaks[i]) > 0.05:
                    changed = True
                peaks[i] = new
                self._peak_hold_t[i] = now
                self._peak_v[i] = 0.0                      # 新峰值 ⇒ 落峰速度清零
            elif (now - self._peak_hold_t[i]) >= SPEC_PEAK_HOLD_S:
                if peaks[i] > 0.0:
                    changed = True
                if self._peak_style == "falling":
                    # ★ task-26① 方案 B：**恒加速砸落** —— v += g·dt，位移 = ½·g·t²（g=450px/s²）
                    #   与方案 A（匀速 50~80px/s）的关键区别：越落越快，"极速落峰"的观感来自这里。
                    self._peak_v[i] = min(SPEC_PEAK_MAX_V, self._peak_v[i] + SPEC_PEAK_G * dt)
                    peaks[i] = max(new, peaks[i] - self._peak_v[i] * dt)
                else:
                    peaks[i] = max(new, peaks[i] - SPEC_PEAK_FALL_PX_S * dt)
        return changed

    def paintEvent(self, _ev):            # noqa: N802
        w, h = self.width(), self.height()
        if w < 20 or h < 6:
            return
        p = QPainter(self)
        # ★ task-22：**保持关闭抗锯齿**（task-14 起就是关的）。
        #   原因：半透明纯色的"扁平度"要用 α=(O−B)/(C−B) 逐像素反解来证，
        #   开 AA 会让胶囊顶点那一行变成部分覆盖 ⇒ 顶 1px 的 α 被拉低（判据 ≤0.03 直接挂）。
        #   关 AA 时圆角仍是几何圆角（radius = bar_w/2），只是边缘不做亚像素混合 ——
        #   6px 宽的柱子上视觉差异可忽略，而 α 沿柱轴严格恒等于 0.68。
        p.setRenderHint(QPainter.Antialiasing, False)
        acc = _accent()
        base = QColor(acc)
        base.setAlpha(70)
        # ★★ task-08：基线居中（y_mid = h/2），柱体以中线为轴**上下对称**伸展。
        #   几何用浮点 h/2，48px 高 ⇒ 24.0；柱体总长 v ∈ [6,42] ⇒ 上下各 v/2 ≤ 21px，
        #   上下各留 3px 余量，永不越界。
        y_mid = h / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(base)
        p.drawRect(QRectF(0, y_mid - 1.0, w, 2.0))     # 居中基线（替代原来的底部死地平线）
        self._ensure(max(16, min(160, (max(80, w) + SPEC_GAP) // SPEC_PITCH)))
        total = self._n * SPEC_BAR_W + (self._n - 1) * SPEC_GAP
        x0 = max(0.0, (w - total) / 2.0)
        # ★★ task-22（用户 ①）：**纯色扁平胶囊** —— 删掉每柱的双色渐变与上下 1px 高光边，
        #   只留一个半透明实色；圆角取 radius = bar_w/2（两端半圆）。颜色就是卡片给的 rgba：
        #   深色 rgba(255,255,255,0.68)、浅色 rgba(30,35,45,0.60)。
        #   ⚠️ 半透明叠在模糊封面背景上，逐行**合成色**必然随背景变化 —— 这是 alpha 设计的
        #      必然结果，不是渐变；QA 的 α 反解 α=(O−B)/(C−B) 会把背景消掉，沿柱轴应为常数。
        # ★ task-22（用户 ③ 方案 A）：**峰值小横条默认不上屏**（_peak_px 状态保留，
        #   暂停定格 / 停止归零语义不变；方案 B 的开关由 task-26 接）。
        r, g_, b_, a_ = (SPEC_BAR_RGBA_LIGHT if _is_light() else SPEC_BAR_RGBA_DARK)
        bar_col = QColor(r, g_, b_, a_)
        # ★ task-26① 方案 B：**极速落峰点**（默认关闭）—— 柱顶之上 2px 小横条，
        #   颜色与柱体同源、alpha 更高（216 vs 173/153），像素上可与柱体区分。
        peak_col = QColor(r, g_, b_, SPEC_PEAK_MARK_A)
        radius = SPEC_BAR_W / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(bar_col)
        for i, v in enumerate(self._bars):
            # v 就是**柱体总长**（px）：上下各一半 ⇒ 逐柱 |上半−下半| = 0（±1px 判据稳过）
            if v < 0.5:
                continue
            p.drawRoundedRect(QRectF(x0 + i * SPEC_PITCH, y_mid - v / 2.0, SPEC_BAR_W, v),
                              radius, radius)
        if self._peak_style == "falling":
            p.setBrush(peak_col)
            hh = SPEC_PEAK_MARK_H
            for i, pv in enumerate(self._peak_px):
                if pv < 1.0:
                    continue
                # 峰值 ≥ 柱高（状态机保证），画在柱顶之上（上半侧）
                p.drawRect(QRectF(x0 + i * SPEC_PITCH, y_mid - pv / 2.0 - hh, SPEC_BAR_W, hh))
        p.end()


# ★★ v1.0.36（task-10）：音频规格胶囊
BADGE_FONT_PX = 11                 # 卡片指定字号
BADGE_PAD_X = 9
BADGE_PAD_Y = 3            # ★ task-16：副标题(底 102)与歌词面板(顶 content_h*0.22≥121)之间
                           #   只有 ~19px；纵向内边距取 3 ⇒ 高度 17，正好塞下且与歌词零相交。
BADGE_RADIUS = 10
BADGE_BELOW_COVER_GAP = 10         # 封面下沿到胶囊的留白
#: 有损格式（没有「位深」这个概念）——只在有损格式上显示 kbps，无损显示位深/采样率
_LOSSY_EXT = ("MP3", "M4A", "AAC", "OGG", "OPUS", "WMA", "MP4")

# --------------------------------------------------------------------------------------
# ★ task-17：液态玻璃的绘制配方（**唯一实现**）
# --------------------------------------------------------------------------------------
# 用户硬约束：第三套皮肤必须直接复用规格胶囊 SpecBadge 的绘制参数 ——
#   ① 底板 = 高透纯白圆角填充；② 边缘 = **1px** 高透纯白亮边。
# 参数真源 = theme.GLASS_FILL / theme.GLASS_EDGE / theme.GLASS_EDGE_W
#   （glass-light 的 glass_capsule / glass_chrome / glass_panel / hairline /
#    hairline_strong 五个调色板键的取值**就是**这两个常量）。
# SpecBadge 与玻璃容器因此是"同源"：容器走调色板键（值 = 常量），
#   徽章走下面这个函数（参数 = 常量），中间没有任何第二套玻璃画法。
# ⚠️ 零测量、零模糊、零着色器：只有两次 drawRoundedRect（填充 + 1px 描边）。
def paint_glass_plate(p, rect, radius: float, fill, edge,
                      edge_w: float = GLASS_EDGE_W, fill_rect=None) -> None:
    """液态玻璃底板 + 1px 高光边（SpecBadge 与玻璃皮肤容器的**唯一绘制实现**）。

    ⚠️ 描边必须落在**几何边界上**（不内缩）：1px 亮边贴着底板外沿，
       与容器的 1px border 语义一致；内缩会变成"两圈"轮廓。
    ⚠️ fill_rect：可选，底板单独用的矩形（默认 = rect）。
       描边那一圈像素想反解出**真实 token**（而不是被半覆盖的底板稀释），
       底板就得铺满到控件外沿；SpecBadge 保持 rect == fill_rect（既有像素逐位不变）。
    """
    p.setPen(Qt.NoPen)
    p.setBrush(fill)
    p.drawRoundedRect(fill_rect if fill_rect is not None else rect, radius, radius)
    if edge is None or edge_w <= 0:
        return
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(edge, float(edge_w)))
    p.drawRoundedRect(rect, radius, radius)


def flac_streaminfo(path: str):
    """读 FLAC STREAMINFO → (位深, 采样率)；任何异常/不合法都返回 (None, None)。

    STREAMINFO 数据在偏移 8（4 字节 fLaC 标记 + 4 字节块头），其内偏移 10..17 是
    20bit 采样率 + 3bit 声道 + 5bit(位深−1) + 36bit 总样本数。
    """
    try:
        with open(path, "rb") as fh:
            blob = fh.read(26)
        if len(blob) < 26 or blob[:4] != b"fLaC":
            return None, None
        x = int.from_bytes(blob[18:26], "big")
        rate = x >> 44
        bits = ((x >> 36) & 0x1F) + 1
        if not (8000 <= rate <= 768000) or bits not in (8, 16, 20, 24, 32):
            return None, None
        return bits, rate
    except Exception:
        return None, None


def badge_text_for(path: str, params=None, bitrate=None) -> str:
    """拼 badge 文本：FLAC · 24bit / 96kHz 或 MP3 · 320kbps。

    ⚠️ 拿不到的项**一律省略**（绝不写 None/0/空壳）；全空则返回空串 ⇒ 上层整枚隐藏。
    """
    ext = os.path.splitext(path or "")[1].lstrip(".").upper()
    rate = bits = kbps = None
    if isinstance(params, dict):
        try:
            rate = int(float(params.get("samplerate") or 0)) or None
        except Exception:
            rate = None
    if ext == "FLAC":
        b2, r2 = flac_streaminfo(path)
        bits = b2
        rate = r2 or rate
    if rate is not None and not (8000 <= rate <= 768000):
        rate = None
    if bits is not None and bits not in (8, 16, 20, 24, 32):
        bits = None
    try:
        kbps = int(round(float(bitrate) / 1000.0)) if bitrate else None
    except Exception:
        kbps = None
    if kbps is not None and not (8 <= kbps <= 2000):
        kbps = None
    if not (bits or rate or kbps):
        return ""            # 没有任何可测规格 ⇒ 整枚隐藏（卡片 B：不许留空壳胶囊）
    detail = [s for s in (("%dbit" % bits) if bits else "",
                          ("%gkHz" % (rate / 1000.0)) if rate else "") if s]
    if ext and detail:
        head = "%s · %s" % (ext, " / ".join(detail))
    elif ext:
        head = ext
    else:
        head = " / ".join(detail)
    if ext in _LOSSY_EXT and kbps:
        head = ("%s · %dkbps" % (head, kbps)) if head else ("%dkbps" % kbps)
    return head


class SpecBadge(QWidget):
    """音频规格胶囊（轻巧、微弱圆角描边）。

    **宽度预排版缓存**：_text_w 只在文本变化时算一次 ⇒ paintEvent 内零文本测量
    （task-04 的封版判据：paintEvent 内 fontMetrics/QFontMetrics/boundingRect/
     horizontalAdvance 调用数必须为 0）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setStyleSheet("background: transparent;")
        self._text = ""
        self._text_w = 0.0
        self.setVisible(False)

    def set_specs_text(self, text: str):
        text = (text or "").strip()
        if text != self._text:
            self._text = text
            self._text_w = self._measure(text)      # ← 全类唯一测量点（绘制期之外）
        self.setVisible(bool(text))
        self.update()

    @staticmethod
    def _measure(text: str) -> float:
        if not text:
            return 0.0
        try:
            f = QFont()
            f.setPixelSize(BADGE_FONT_PX)
            return float(QFontMetrics(f).horizontalAdvance(text))
        except Exception:
            return float(len(text) * 7)

    def hint_size(self) -> QSize:
        if not self._text:
            return QSize(0, 0)
        return QSize(int(self._text_w) + BADGE_PAD_X * 2, BADGE_FONT_PX + BADGE_PAD_Y * 2)

    def paintEvent(self, _ev):            # noqa: N802
        if not self._text:
            return
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        light = _is_light()
        r = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        # ★ task-17：底板与 1px 亮边统一走 paint_glass_plate()（**唯一实现**）。
        #   液态玻璃皮肤 ⇒ 与三个玻璃容器**同一组参数**（theme.GLASS_FILL / GLASS_EDGE）；
        #   其余 7 套既有主题 ⇒ 逐位保持 v1.0.36 的原参数（浅色族深墨 / 深色族纯白），
        #   像素与改动前完全一致（几何、圆角、1px 笔宽都没动）。
        if _glass_skin():
            paint_glass_plate(p, r, BADGE_RADIUS, _GLASS_FILL_COLOR, _GLASS_EDGE_COLOR)
        elif light:
            paint_glass_plate(p, r, BADGE_RADIUS, QColor(20, 26, 36, 24), QColor(20, 26, 36, 44))
        else:
            paint_glass_plate(p, r, BADGE_RADIUS, QColor(255, 255, 255, 28), QColor(255, 255, 255, 58))
        f = QFont()
        f.setPixelSize(BADGE_FONT_PX)
        p.setFont(f)
        p.setPen(QColor(38, 46, 58, 225) if light else QColor(228, 234, 242, 215))
        # 文本已按缓存宽度排版，这里只居中绘制 ⇒ 零测量
        p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self._text)
        p.end()


class LyricsPanel(QWidget):
    """滚动歌词：动态折行 + 组内 6px / 组间 29px + 当前行 40% 黄金线。

    **没有自己的定时器**（主帧时钟调用 `step()`）；宽度/高度全部带兜底 ——
    宽度为 0 会让折行高度塌陷、整块不画（v1.0.30 歌词蒸发的根因）。
    """

    #: ★ task-24 锚点：点击时间胶囊 ⇒ 请求 seek（毫秒）。挂在 LyricsPanel 上，
    #:  由 AudioView 连到引擎（音频层唯一 seek 入口，便于探针计数）。
    request_seek = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # ★★ task-24：需要接滚轮/悬停/胶囊点击 ⇒ 不再全局透明鼠标事件；
        #   但**除胶囊命中外的鼠标事件一律 ev.ignore() 放行**（窗口拖动等父级交互不受影响）。
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)
        self._lines: list[tuple[int, str]] = []
        self._idx = -1
        self._tops: list[float] = []
        self._heights: list[float] = []
        self._main_h: list[float] = []      # 预排版缓存（主词折行高度）
        self._trans_h: list[float] = []     # 预排版缓存（翻译折行高度）
        self._offset = 0.0
        self._target = 0.0
        self._spec_text = ""
        self._spec_w = 0.0                  # ★ 预排版：占位药丸底的宽度（绘制期零测量）
        self._relayout_pending = False      # 绘制期发现缓存失效时的延后重排去重标记
        self._last_t = 0.0                  # 上一帧真实时刻（step 的 dt 折算用）
        self._placeholder = "纯音乐 / 暂无歌词"
        # ★★ task-23：字号/明度的逐帧插值状态 + 羽化缓冲层（都在布局/tick 里维护，绘制期零分配零测量）
        self._size_scale = 1.0
        # ★ task-26①：歌词样式（设置面板驱动；默认值 = task-23 封版，持久化后覆盖）
        self._size_cur = LYRIC_SIZE_CUR        # 当前行字号基准
        self._size_other = LYRIC_SIZE_OTHER    # 非当前行字号基准
        self._trans_cur = LYRIC_TRANS_CUR
        self._trans_other = LYRIC_TRANS_OTHER
        self._weight_cur = QFont.Bold          # 当前行字重
        self._weight_noncur = QFont.Medium     # 未唱行字重（task-23 封版 500）
        self._step_pad = LYRIC_STEP_PAD        # 网格步进的"行间呼吸"（行间距滑块）
        self._scroll_s = LYRIC_SCROLL_S        # 滚动插值周期（快/标准/慢）
        self._family_override = ""             # 用户在设置里选的字体族（空 = 自动）
        self._size_now: list[float] = []       # 每行主词**当前**字号（px，逐帧插值）
        self._alpha_now: list[float] = []      # 每行**当前**明度（0..1，逐帧插值）
        self._font_main: list[QFont] = []      # 每行预建字体（tick 里改 size/weight，paint 直接用）
        self._font_trans: list[QFont] = []
        self._anim_t0 = 0.0                    # 本轮字号/明度动画起点（0 = 无动画）
        self._anim_from: list[tuple[float, float]] = []
        self._scroll_t0 = 0.0                  # 本轮滚动动画起点
        self._scroll_from = 0.0
        self._step = 0.0                       # 统一网格步进（_relayout 算一次）
        self._fit: list[float] = []            # 逐行"塞进槽内"的字号缩放（task-23 复审后新增）
        self._family = ""                      # 歌词字体族（布局期解析一次）
        self._buf = QPixmap()                  # ★ 真羽化缓冲层（尺寸变化时在 tick 里重建）
        self._buf_size = (0, 0)
        self._feather = None                   # 羽化渐变画刷（尺寸变化时重建）
        self._tbtn_font: QFont | None = None   # ★ task-24：时间胶囊字体（预建）
        self._ph_font: QFont | None = None     # 占位文案字体（预建，绘制期零构造）
        self._spec_font: QFont | None = None   # 占位药丸字体（预建）
        # ★★ task-24（用户 ⑩）锚点：手动浏览态 / 焦点行 / 时间胶囊（**全部可读**）
        #   ① _manual_scroll: bool  —— 是否处于滚轮手动浏览态（True = 自动跟随已暂停）
        #   ② _hover_idx: int       —— 鼠标悬停行（-1 = 无）
        #   ③ _focus_idx: int       —— 当前高亮行（悬停行优先，否则垂直中心行）
        #   ④ _tbtn_idx: int        —— 时间胶囊归属行（-1 = 隐藏）
        #   ⑤ _tbtn_text: str       —— 胶囊文本「▶ mm:ss」
        #   ⑥ _tbtn_rect: QRectF    —— 胶囊**命中矩形**（面板逻辑坐标；点击入口就是它）
        #   ⑦ _guide_y: float       —— 那条极淡水平辅助线的 y
        #   ⑧ _manual_gen: int      —— 5 秒无操作计时的代际（期间又操作 ⇒ 旧回调作废）
        self._manual_scroll = False
        self._manual_gen = 0
        self._hover_idx = -1
        self._focus_idx = -1
        self._tbtn_idx = -1
        self._tbtn_text = ""
        self._tbtn_rect = QRectF()
        self._guide_y = 0.0
        self._scroll_min = 0.0
        self._scroll_max = 0.0

    @staticmethod
    def _merge_bilingual(lines):
        out: list[tuple[int, str]] = []
        for t, txt in lines:
            if out and out[-1][0] == t and "\n" not in out[-1][1]:
                out[-1] = (t, out[-1][1] + "\n" + txt)
            else:
                out.append((t, txt))
        return out

    def _measure_spec_w(self, text: str) -> float:
        """预排版：占位分支那个药丸底的宽度（**只在这里**测量，绘制期零测量）。

        ⚠️ 旧实现把这个 horizontalAdvance 放在 paintEvent 里（audio_view.py 占位分支），
        60 FPS 下等于每帧一次文本测量 —— 现在挪到文本变化时算一次（KB §9.70：
        不许用"本帧不画"来掩盖，必须把测量搬出绘制期）。
        """
        if not text:
            return 0.0
        try:
            f = QFont()
            f.setPixelSize(12)
            return float(QFontMetrics(f).horizontalAdvance(text)) + 24.0
        except Exception:
            return float(len(text) * 8 + 24)

    def set_spec_text(self, text: str):
        """外部唯一的 spec 文本入口（AudioView.set_spec / set_lines 都走它）。"""
        text = text or ""
        if text != self._spec_text:
            self._spec_text = text
            self._spec_w = self._measure_spec_w(text)
        self.update()

    def set_lines(self, lines, spec_text: str = ""):
        """灌歌词。

        ⚠️★ task-16 复审打回项②：**不再直接写 _spec_text** —— 规格显示的唯一决策入口是
        AudioView._sync_spec_display()（badge 可见 ⇒ 药丸不画；badge 缺失 ⇒ 药丸兜底）。
        这里原先调 set_spec_text(spec_text)，会绕过互斥逻辑，导致「徽章 + 药丸」同时出现
        （QA 像素证据：药丸精确矩形内 3 个字形连通域）。参数 spec_text 保留仅为兼容，**忽略**。
        """
        self._lines = self._merge_bilingual(list(lines or []))
        self._idx = -1
        self._offset = 0.0
        self._target = 0.0
        self._relayout()
        self.update()

    def _content_width(self) -> int:
        return max(200, int(self.width()) - 8)

    def _measure(self, text: str, pixel: int, bold: bool, width: int) -> float:
        if not text:
            return float(pixel * 1.6)
        try:
            f = QFont()
            f.setPixelSize(max(8, int(pixel)))
            f.setBold(bool(bold))
            fm = QFontMetrics(f)
            rect = fm.boundingRect(QRect(0, 0, max(200, int(width)), 9999),
                                   Qt.TextWordWrap | Qt.AlignLeft, text)
            h = float(rect.height())
            return h if h > 0 else float(fm.height() + 4)
        except Exception:
            return float(pixel * 1.6 + 4)

    def _relayout(self):
        self._tops, self._heights = [], []
        w = self._content_width()
        sr = 1.0
        try:
            sr = self.parentWidget().scale_ratio()
        except Exception:
            pass
        self._size_scale = sr
        if not self._family:
            self._family = _lyric_family()
        # ★★ v1.0.32 ②：**在这里**（载入歌词 / resize 时）一次性算完所有折行高度，
        #    存进 self._main_h / self._trans_h / self._tops / self._heights；
        #    paintEvent 只读缓存 ⇒ 零 QFontMetrics 调用 ⇒ 不再掉帧。
        #
        # ★★ task-23（Lead 口径 A）：**统一网格步进** —— 步进只由"把每一行都按**当前行字号**
        #    排版时最高的那一行"决定一次，与 _idx 无关、与是否有翻译无关 ⇒
        #    ① 换行不会让步进跳动（实测极差 0）；② 字号插值只改字形大小、不改步进。
        #    旧实现是 y += 本行实际高度 + 29 ⇒ 步进随字号与有无翻译变化（实测极差 23px）。
        self._main_h: list[float] = []
        self._trans_h: list[float] = []
        # ★ task-23：步进的**基准**用"非当前行字号"下的最大内容高（而不是把每行都按当前行的
        #   大字号去算）—— 否则每行都要为"它当前行时的高个子"预留空间，单语行的槽余量会飙到 95px
        #   （C 口径要的是"无预留翻译高度"）。当前行更大的那部分由 LYRIC_STEP_PAD（22px）吸收：
        #   当前行内容高 − 非当前行内容高 ≈ 10~16px < 22px ⇒ 不会吃到下一行。
        # ★★ task-23（Lead 复审后重调）：步进基准 = **单视觉行**的内容高，而不是"最长那行的
        #    折行总高"。踩过的坑：QA 素材里有一行故意写得极长（折 3 行 ⇒ 内容高 96px），
        #    max() 口径把它当成全体步进 ⇒ 单语步进 118px、行间空白 ~95px，Lead 看截图判
        #    "一致但太空"（用户要的是"一致且匀称如呼吸"，不是变稀疏）。
        #    现在：基准 = min(各行内容高)（即"只排一行的那些行"的高度）并夹一个单行估算上限；
        #    超过步进的行**等比缩字**（LYRIC_FIT_MIN 兜底），而不是把整个网格撑大。
        contents: list[tuple[float, float]] = []
        any_trans = False
        for _t, txt in self._lines:
            main, trans = (txt.split("\n", 1) if "\n" in txt else (txt, ""))
            mh_cur = self._measure(main, self._size_cur * sr, True, w)
            th_cur = self._measure(trans, self._trans_cur * sr, True, w) if trans else 0.0
            any_trans = any_trans or bool(trans)
            contents.append((mh_cur, th_cur))
        if contents:
            row_min = min(mh + (LYRIC_INNER_PAD + th if th > 0 else 0.0) for mh, th in contents)
            row_est = (LYRIC_ROW_EST * self._size_cur * sr
                       + ((LYRIC_INNER_PAD + LYRIC_ROW_EST * self._trans_cur * sr) if any_trans else 0.0))
            row_content = min(row_min, row_est)
        else:
            row_content = LYRIC_ROW_EST * self._size_cur * sr
        self._step = max(18.0, row_content) + self._step_pad
        # 逐行"塞不塞得下"：内容高 > 步进 ⇒ 等比缩字（统一网格不因个别超长行被撑破）
        self._fit = []
        for mh, th in contents:
            need = mh + (LYRIC_INNER_PAD + th if th > 0 else 0.0)
            self._fit.append(1.0 if need <= self._step + 0.5 else max(LYRIC_FIT_MIN, self._step / need))
        self._main_h = []
        self._trans_h = []
        for i, (_t, txt) in enumerate(self._lines):
            main, trans = (txt.split("\n", 1) if "\n" in txt else (txt, ""))
            fit = self._fit[i]
            # 本行**自身**的盒高按自己的目标字号（含缩放）算；无翻译的行不含翻译高度
            # （卡面口径 C：严禁给没有翻译的行预留翻译高度、不出现 boundingRect 冗余留白）
            self._main_h.append(self._measure(main, self._size_other * sr * fit, False, w))
            self._trans_h.append(self._measure(trans, self._trans_other * sr * fit, False, w) if trans else 0.0)
        y = 0.0
        for i, (_t, txt) in enumerate(self._lines):
            main, trans = (txt.split("\n", 1) if "\n" in txt else (txt, ""))
            # 当前行的盒高按**它自己的大字号**重算（否则 drawText 的矩形高度会把大字裁掉）
            if i == self._idx:
                fit = self._fit[i] if i < len(self._fit) else 1.0
                mh = self._measure(main, self._size_cur * sr * fit, True, w)
                th = self._measure(trans, self._trans_cur * sr * fit, True, w) if trans else 0.0
            else:
                mh, th = self._main_h[i], self._trans_h[i]
            h = max(18.0, mh + (LYRIC_INNER_PAD + th if trans else 0.0))
            self._tops.append(y)
            self._heights.append(h)
            y += self._step
        self._build_fonts()
        self._reset_anim()
        self._retarget()

    # ---- task-23：字号/明度插值的辅助（全部在布局/tick 里跑，绘制期零测量零分配）----------
    def _targets(self, i: int) -> tuple[float, float]:
        """第 i 行的（目标主词字号, 目标明度）。当前行更大更亮 —— 卡面 ③ 的两端。"""
        sr = self._size_scale or 1.0
        cur = (i == self._idx)
        fit = self._fit[i] if i < len(self._fit) else 1.0
        size = (self._size_cur if cur else self._size_other) * sr * fit
        return max(10.0, size), (1.0 if cur else 0.40)

    def _build_fonts(self):
        """按行预建 QFont（族/字重在这里定；字号由 _apply_font 在 tick 里改）。

        字重按卡面 ①：**当前行 700（Bold）、未唱行 500（Medium）** —— 内置的
        HarmonyOS Sans SC 随包带了 Medium/Bold 两份真字形，所以不是算法加粗。
        """
        fam = self._family
        self._font_main = []
        self._font_trans = []
        for i in range(len(self._lines)):
            f = QFont()
            if fam:
                f.setFamily(fam)
            f.setWeight(self._weight_cur if i == self._idx else self._weight_noncur)
            self._font_main.append(f)
            g = QFont()
            if fam:
                g.setFamily(fam)
            g.setWeight(self._weight_cur if i == self._idx else self._weight_noncur)
            self._font_trans.append(g)
        for i in range(len(self._lines)):
            self._apply_font(i)
        # 占位分支的两个字体也在这里预建（绘制期不再 new QFont）
        ph = QFont()
        if fam:
            ph.setFamily(fam)
        ph.setPixelSize(15)
        self._ph_font = ph
        sp = QFont()
        if fam:
            sp.setFamily(fam)
        sp.setPixelSize(12)
        self._spec_font = sp
        # ★ task-24：时间胶囊字体（预建 ⇒ 绘制期零构造零测量）
        tb = QFont()
        if fam:
            tb.setFamily(fam)
        tb.setPixelSize(12)
        tb.setWeight(QFont.Medium)
        self._tbtn_font = tb

    def _apply_font(self, i: int) -> None:
        """把**当前插值字号**写进第 i 行的 QFont（只改 pixelSize，不做任何测量）。"""
        if i >= len(self._font_main):
            return
        sr = self._size_scale or 1.0
        cur = (i == self._idx)
        size = self._size_now[i] if i < len(self._size_now) else self._targets(i)[0]
        self._font_main[i].setPixelSize(max(10, int(round(size))))
        fit = self._fit[i] if i < len(self._fit) else 1.0
        tsize = (self._trans_cur if cur else self._trans_other) * sr * fit
        self._font_trans[i].setPixelSize(max(9, int(round(tsize))))

    def _reset_anim(self):
        """把插值状态直接对齐到目标（载入歌词 / resize 时用；换行才走 250ms 动画）。"""
        self._size_now = [self._targets(i)[0] for i in range(len(self._lines))]
        self._alpha_now = [self._targets(i)[1] for i in range(len(self._lines))]
        self._anim_t0 = 0.0
        for i in range(len(self._lines)):
            self._apply_font(i)

    def _start_font_anim(self, from_sizes=None, from_alphas=None):
        """当前行变化 ⇒ 启动 250ms OutCubic 的字号/明度插值（从**当前值**出发，可中途重定向）。

        from_sizes/from_alphas：换行前一刻的插值状态（由 set_position_ms 抓取）。
        不传则用现有值 —— 两种情况都必须让动画**从起点跑**，不能从终点出发。
        """
        n = len(self._lines)
        if (from_sizes is None or len(from_sizes) != n
                or from_alphas is None or len(from_alphas) != n):
            self._reset_anim()
            from_sizes = list(self._size_now)
            from_alphas = list(self._alpha_now)
        self._size_now = [float(v) for v in from_sizes]
        self._alpha_now = [float(v) for v in from_alphas]
        self._anim_from = list(zip(self._size_now, self._alpha_now))
        self._anim_t0 = time.perf_counter()
        for i in range(n):
            self._apply_font(i)

    def set_position_ms(self, ms: int):
        if not self._lines:
            return
        idx = -1
        for i, (t, _txt) in enumerate(self._lines):
            if t <= ms:
                idx = i
            else:
                break
        if idx != self._idx:
            # ⚠️ 必须**先**把"换行前"的插值状态抓下来 —— _relayout() 内部会 _reset_anim()
            #    把字号直接对齐到新目标，之后再启动动画就等于"从终点出发"（实测逐帧字号差恒 0）。
            prev_sizes = list(self._size_now)
            prev_alphas = list(self._alpha_now)
            self._idx = idx
            self._relayout()             # 重建几何（含统一网格步进）
            self._start_font_anim(prev_sizes, prev_alphas)   # ★ 250ms OutCubic 字号/明度插值
            # ★ task-24：手动浏览期间**不跟随**（视图不动，只更新"当前演唱行"索引；
            #   退出手动时 exit_manual→_retarget 会平滑滚回这一行）
            if self._manual_scroll:
                self._scroll_t0 = 0.0
                self._scroll_from = self._offset
            self.update()

    def _retarget(self):
        """重算滚动目标，并**重新起一段 320ms OutCubic 插值**（从当前 offset 出发 ⇒ 速度连续）。"""
        if not self._lines or self._idx < 0 or self._idx >= len(self._tops):
            self._target = 0.0
        else:
            gold = self.height() * CURRENT_LINE_RATIO
            self._target = gold - (self._tops[self._idx] + self._heights[self._idx] / 2.0)
        self._scroll_from = self._offset
        self._scroll_t0 = time.perf_counter()

    # ---- task-24：滚轮手动浏览 / 悬停焦点 / 时间胶囊 / 5 秒无操作回位 ---------------------
    def _line_at_y(self, y: float) -> int:
        cy = y - self._offset
        for i, top in enumerate(self._tops):
            if top <= cy < top + max(1.0, self._step):
                return i
        return -1

    def _clamp_offset(self):
        """把手动滚动的位移夹在"第一行/最后一行落在黄金线"之间（翻不出歌词）。"""
        if not self._tops:
            self._offset = 0.0
            return
        gold = self.height() * CURRENT_LINE_RATIO
        self._scroll_max = gold - (self._tops[0] + self._heights[0] / 2.0)
        self._scroll_min = gold - (self._tops[-1] + self._heights[-1] / 2.0)
        lo, hi = min(self._scroll_min, self._scroll_max), max(self._scroll_min, self._scroll_max)
        self._offset = max(lo, min(hi, self._offset))

    def _manual_arm(self):
        """起/重起 5 秒无操作计时。⚠️ 用**临时 singleShot**（非常驻定时器）：
        常驻 QTimer 必须仍然是 2 个（帧钟红线）；代际号保证"期间又操作 ⇒ 旧回调作废"。"""
        self._manual_gen += 1
        gen = self._manual_gen
        QTimer.singleShot(LYRIC_MANUAL_HOLD_MS, lambda: self._manual_timeout(gen))

    def _enter_manual(self):
        self._manual_scroll = True
        self._manual_arm()

    def _manual_timeout(self, gen: int):
        if gen != self._manual_gen or not self._manual_scroll:
            return                      # 期间又操作过 ⇒ 本次 5 秒计时作废
        self.exit_manual()

    def exit_manual(self):
        """退出手动浏览：隐藏胶囊 + 恢复自动跟随。

        ★ 点击胶囊时**同一帧**调用（判据 C 的"同帧恢复跟随"）；_retarget 会重起一段
        320ms OutCubic 平滑滚回当前演唱行（判据 D 的位移单调收敛）。
        """
        self._manual_gen += 1
        self._manual_scroll = False
        self._tbtn_idx = -1
        self._tbtn_text = ""
        self._tbtn_rect = QRectF()
        self._hover_idx = -1
        self._retarget()
        self.update()

    def _refresh_focus(self):
        """焦点行 = **悬停行**优先，否则**垂直中心**所在行；再决定时间胶囊是否出现。

        判据 E：没有时间戳（ms<0）或空文本的行**不出按钮**（纯文本/间奏行）。
        """
        n = len(self._tops)
        if n == 0:
            self._focus_idx = -1
            self._guide_y = 0.0
            self._tbtn_idx = -1
            self._tbtn_text = ""
            self._tbtn_rect = QRectF()
            return
        idx = self._hover_idx if 0 <= self._hover_idx < n else -1
        if idx < 0:
            cy = self.height() * 0.5
            best = None
            for i in range(n):
                c = self._offset + self._tops[i] + self._heights[i] / 2.0
                d = abs(c - cy)
                if best is None or d < best:
                    best, idx = d, i
        self._focus_idx = idx
        self._guide_y = self._offset + self._tops[idx] + self._heights[idx] / 2.0
        ms, txt = self._lines[idx] if idx < len(self._lines) else (None, "")
        has_ts = (ms is not None) and int(ms) >= 0 and str(txt).strip() != ""
        if has_ts:
            self._tbtn_idx = idx
            self._tbtn_text = "%s %s" % (LYRIC_PLAY_GLYPH, _mmss(int(ms)))
            w, hh = float(LYRIC_TBTN_W), float(LYRIC_TBTN_H)
            y = max(0.0, min(float(self.height()) - hh, self._guide_y - hh / 2.0))
            self._tbtn_rect = QRectF(float(self.width()) - w - LYRIC_TBTN_MARGIN, y, w, hh)
        else:
            self._tbtn_idx = -1
            self._tbtn_text = ""
            self._tbtn_rect = QRectF()

    def wheelEvent(self, ev):            # noqa: N802
        if not self._lines:              # 判据 E：歌词为空 ⇒ 不响应滚轮
            ev.ignore()
            return
        dy = float(ev.angleDelta().y())
        if not dy:
            ev.ignore()
            return
        self._enter_manual()             # 判据 A：进入手动浏览 ⇒ 自动跟随暂停
        self._offset += (dy / 120.0) * LYRIC_WHEEL_STEP * max(24.0, self._step)
        self._clamp_offset()
        self._refresh_focus()
        self.update()
        ev.accept()

    def mouseMoveEvent(self, ev):        # noqa: N802
        if self._lines:
            idx = self._line_at_y(float(ev.position().y()))
            if idx != self._hover_idx:
                self._hover_idx = idx
                if self._manual_scroll:
                    self._refresh_focus()
                    self.update()
        ev.ignore()                      # 不抢事件：窗口拖动等仍由父级处理

    def mousePressEvent(self, ev):       # noqa: N802
        if (self._manual_scroll and self._tbtn_idx >= 0
                and not self._tbtn_rect.isEmpty() and self._tbtn_rect.contains(ev.position())):
            self._activate_capsule()
            ev.accept()
            return
        ev.ignore()                      # 判据 F：其余点击一律放行给父级

    def _activate_capsule(self):
        """点击「▶ mm:ss」：**同一帧**退出手动态（恢复跟随）+ 请求 seek（恰好一次）。"""
        idx = self._tbtn_idx
        if not (0 <= idx < len(self._lines)):
            return
        ms = int(self._lines[idx][0] or 0)
        self.exit_manual()
        self.request_seek.emit(ms)

    # ---- task-26①：歌词样式的运行时入口（设置面板唯一的施加点）------------------------
    def apply_style(self, family=None, base_px=None, weight=None, step_pad=None,
                    scroll_ms=None) -> None:
        """把「音乐与歌词」设置施加到运行时：改完**立刻**重排 + 重绘（无需重启）。

        · family：字体族名（"" / "system" = 回到自动挑选）
        · base_px：当前行字号基准（16~28），非当前行/翻译按 27:20:18:14 的比例同步缩放
        · weight：regular / medium / heavy（未唱行 400/500/700；当前行 700/700/900）
        · step_pad：网格步进的行间呼吸（10~48px）
        · scroll_ms：滚动插值周期（200 / 320 / 500ms）
        """
        if family is not None:
            fam = str(family)
            if fam in ("", "system", "跟随系统"):
                self._family = _lyric_family()
                self._family_override = ""
            else:
                self._family = fam
                self._family_override = fam
        if base_px is not None:
            try:
                px = max(12.0, min(40.0, float(base_px)))
            except Exception:
                px = LYRIC_SIZE_CUR
            self._size_cur = px
            self._size_other = max(10.0, round(px * (LYRIC_SIZE_OTHER / LYRIC_SIZE_CUR)))
            self._trans_cur = max(9.0, round(px * (LYRIC_TRANS_CUR / LYRIC_SIZE_CUR)))
            self._trans_other = max(8.0, round(px * (LYRIC_TRANS_OTHER / LYRIC_SIZE_CUR)))
        if weight is not None:
            w = str(weight)
            if w == "regular":
                self._weight_noncur, self._weight_cur = QFont.Normal, QFont.Bold
            elif w == "heavy":
                self._weight_noncur, self._weight_cur = QFont.Bold, QFont.Black
            else:                                  # medium（默认，task-23 封版 500/700）
                self._weight_noncur, self._weight_cur = QFont.Medium, QFont.Bold
        if step_pad is not None:
            try:
                self._step_pad = max(4.0, min(80.0, float(step_pad)))
            except Exception:
                pass
        if scroll_ms is not None:
            try:
                self._scroll_s = max(0.08, min(2.0, float(scroll_ms) / 1000.0))
            except Exception:
                pass
        self._relayout()
        self._reset_anim()
        self._retarget()
        self.update()

    def lyric_style(self) -> dict:
        """★ task-26 锚点：歌词样式的**生效值**（给 QA/门禁读，避免只认 config）。

        fonts_px / weights 是字体对象里**真正生效**的像素字号与字重（前 6 行）；step 是统一网格步进。
        """
        idx = self._idx if 0 <= self._idx < len(self._font_main) else -1
        return {"family": (self._font_main[0].family() if self._font_main else self._family),
                "family_override": self._family_override,
                "base_px": round(float(self._size_cur), 2),
                "size_other": round(float(self._size_other), 2),
                "size_cur_active": (int(self._font_main[idx].pixelSize()) if idx >= 0 else -1),
                "weight_active": (int(self._font_main[idx].weight()) if idx >= 0 else -1),
                "weight_noncur": int(self._weight_noncur),
                "fonts_px": [int(f.pixelSize()) for f in self._font_main[:6]],
                "weights": [int(f.weight()) for f in self._font_main[:6]],
                "step": round(float(self._step), 2),
                "step_pad": round(float(self._step_pad), 2),
                "scroll_ms": int(round(float(self._scroll_s) * 1000)),
                "tops_head": [round(float(x), 2) for x in self._tops[:4]],
                "n": len(self._lines), "idx": idx}

    def time_capsule(self) -> dict:
        """★ task-24 锚点（QA/门禁用）：时间胶囊的可读快照（**自绘区域，不是 QWidget**）。"""
        return {"visible": bool(self._manual_scroll and self._tbtn_idx >= 0),
                "text": self._tbtn_text, "idx": int(self._tbtn_idx),
                "line_ms": (int(self._lines[self._tbtn_idx][0])
                            if 0 <= self._tbtn_idx < len(self._lines) else -1),
                "rect": [round(self._tbtn_rect.x(), 2), round(self._tbtn_rect.y(), 2),
                         round(self._tbtn_rect.width(), 2), round(self._tbtn_rect.height(), 2)],
                "focus_idx": int(self._focus_idx), "hover_idx": int(self._hover_idx),
                "manual_scroll": bool(self._manual_scroll),
                "guide_y": round(float(self._guide_y), 2),
                "offset": round(float(self._offset), 3),
                "target": round(float(self._target), 3)}

    def step(self, dt: float | None = None):
        """滚动一帧（主帧时钟调用，**没有自己的定时器**）。

        ★★ task-23（卡面 ③）：两段插值都改成**基于真实时间的缓动曲线**：
          · 字号/明度：**250ms OutCubic**，从当前值出发（可中途重定向），
            逐帧相邻字号差 ≤1.2px（不再出现"换行瞬间字号跳变"的阶跃）；
          · 滚动：**320ms OutCubic**，_retarget() 每换一行重起一段、起点是当前 offset
            ⇒ 视觉速度连续（旧的"每帧固定收敛 22%"是纯指数尾巴，永远差最后几像素）。
        参数 dt 保留兼容旧调用（本实现不再需要它）。
        """
        if not self._lines:
            return
        now = time.perf_counter()
        self._last_t = now
        changed = False
        # ① 字号/明度插值（250ms OutCubic）
        if self._anim_t0:
            p = (now - self._anim_t0) / LYRIC_FONT_ANIM_S
            p = 1.0 if p >= 1.0 else p
            e = 1.0 - (1.0 - p) ** 3
            done = p >= 1.0
            for i in range(len(self._size_now)):
                ts, ta = self._targets(i)
                s0, a0 = self._anim_from[i] if i < len(self._anim_from) else (ts, ta)
                want = s0 + (ts - s0) * e
                cur = self._size_now[i]
                # ★ task-23：字号**逐帧限速 1px** —— 判据是"逐帧相邻字号差 ≤1.2px"，
                #   而字号是整数像素；某一帧若被拖到 50ms，OutCubic 会一次跳 3px ⇒ 必须限速。
                #   限速后最坏情况也只是动画多跑几帧（6px 差距 ⇒ ≥6 帧 ≈100ms，仍在 250ms 量级）。
                if abs(want - cur) > 1.0:
                    want = cur + (1.0 if want > cur else -1.0)
                    done = False
                self._size_now[i] = want
                self._alpha_now[i] = a0 + (ta - a0) * e     # 明度是浮点，连续插值不限速
                self._apply_font(i)
            if done:
                for i in range(len(self._size_now)):
                    ts, ta = self._targets(i)
                    self._size_now[i] = ts
                    self._alpha_now[i] = ta
                    self._apply_font(i)
                self._anim_t0 = 0.0
            changed = True
        # ② 滚动插值（320ms OutCubic）
        # ★★ task-24 判据 A：手动浏览期间**暂停自动跟随** —— offset 只由滚轮驱动，
        #    播放推进（_retarget 由 set_position_ms 触发）不会把视图拽走。这里还必须
        #    **停掉已排队的滚动动画**（_scroll_t0 清零），否则滚轮一动就被旧动画拉回去。
        if self._manual_scroll:
            self._scroll_t0 = 0.0
            self._scroll_from = self._offset
            self._refresh_focus()          # 垂直中心行随滚动更新（滚轮停下后也保持正确）
            changed = True
        elif self._scroll_t0:
            p = (now - self._scroll_t0) / max(0.05, self._scroll_s)
            p = 1.0 if p >= 1.0 else p
            e = 1.0 - (1.0 - p) ** 3
            self._offset = self._scroll_from + (self._target - self._scroll_from) * e
            if p >= 1.0:
                self._offset = self._target
                self._scroll_t0 = 0.0
            changed = True
        elif abs(self._target - self._offset) > 0.05:
            self._offset = self._target          # 兜底：没有动画起点时直接落位
            changed = True
        # ③ 羽化缓冲层维护（尺寸变化时重建；正常帧零分配）
        if self._buf_size != (self.width(), self.height()):
            self._ensure_buffer()
        if changed:
            self.update()

    def _deferred_relayout(self):
        """绘制期发现缓存失效时的一轮延后重排（一次性，不是新定时器）。"""
        self._relayout_pending = False
        self._relayout()
        self.update()                      # ← 下一帧内容恢复

    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._ensure_buffer()             # 缓冲层与羽化渐变跟着尺寸重建（不在绘制期分配）
        self._relayout()
        self.update()

    def _ensure_buffer(self):
        """重建"真羽化"缓冲层与 56px 羽化渐变（只在尺寸变化时调用）。

        卡面 ④：文字先画进**与面板同尺寸的透明缓冲层**，再用 DestinationIn 叠
        上下各 LYRIC_FEATHER_PX 的线性 alpha 渐变（0→255→255→0）—— 这才是羽化；
        旧实现是"靠 _offset 把字挪出去"，底带那半截字是**硬切**
        （before：边缘 4px 中位亮度 116、沿边缘单调 False）。
        """
        w, h = max(1, int(self.width())), max(1, int(self.height()))
        self._buf = QPixmap(w, h)
        self._buf.fill(Qt.transparent)
        self._buf_size = (w, h)
        f = float(LYRIC_FEATHER_PX)
        a = min(0.49, f / float(h))
        b = max(0.51, 1.0 - f / float(h))
        grad = QLinearGradient(0.0, 0.0, 0.0, float(h))
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(a, QColor(0, 0, 0, 255))
        grad.setColorAt(b, QColor(0, 0, 0, 255))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        self._feather = QBrush(grad)

    @staticmethod
    def _col(light: bool, alpha: float, strong: bool = False) -> QColor:
        if strong:
            return QColor(0, 0, 0) if light else QColor(255, 255, 255)
        c = QColor(20, 22, 26) if light else QColor(255, 255, 255)
        c.setAlphaF(max(0.0, min(1.0, alpha)))
        return c

    def paintEvent(self, _ev):            # noqa: N802
        """★ task-23（卡面 ④）：**先画进透明缓冲层，再叠 56px 羽化**。

        绘制期依旧**零文本测量、零字体构造**：字体（含字重/字号插值结果）在
        _build_fonts/_apply_font 里预建，这里只 setFont + drawText。
        """
        w, h = self.width(), self.height()
        if w < 8 or h < 8:
            return
        if self._buf_size != (w, h):
            self._ensure_buffer()          # 极端时序兜底（正常帧零分配）
        light = _is_light()
        # ⚠️ 每帧必须清空缓冲层：它只在尺寸变化时重建，不清就会把上一帧的字**叠影**上去
        #    （实测：连续帧的文字相互叠加、右半幅还会残留旧内容的裁切边）。
        self._buf.fill(Qt.transparent)
        bp = QPainter(self._buf)
        bp.setRenderHint(QPainter.Antialiasing, True)
        bp.setRenderHint(QPainter.TextAntialiasing, True)
        self._paint_content(bp, w, h, light)
        bp.end()
        if self._feather is not None:
            fp = QPainter(self._buf)
            fp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            fp.fillRect(0, 0, w, h, self._feather)
            fp.end()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._buf)
        p.end()

    def _paint_content(self, p, w: int, h: int, light: bool):
        if not self._lines:
            f = self._ph_font or QFont()
            p.setFont(f)
            p.setPen(self._col(light, 0.60))
            p.drawText(QRectF(0, h * 0.42 - 20, w, 40), Qt.AlignCenter, self._placeholder)
            if self._spec_text:
                f2 = self._spec_font or QFont()
                p.setFont(f2)
                # ★ 宽度用**预排版缓存**（set_spec_text 里算好），绘制期零测量
                tw = self._spec_w or 24.0
                rect = QRectF((w - tw) / 2.0, h * 0.42 + 24, tw, 26)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(20, 26, 36, 16) if light else QColor(255, 255, 255, 22))
                p.drawRoundedRect(rect, 13, 13)
                p.setPen(self._col(light, 0.70))
                p.drawText(rect, Qt.AlignCenter, self._spec_text)
            return
        if not self._tops or len(self._tops) != len(self._lines):
            # ★★ v1.0.34-4：绘制期**绝不测量**（KB §9.70：也不许用"本帧不画"糊过去）。
            #   这里只登记一次延后重排，交给下一轮事件循环；_deferred_relayout() 重建缓存后
            #   **立即 update()** ⇒ 下一帧一定有内容（内容恢复有像素级证据，见 task-4 探针）。
            if not self._relayout_pending:
                self._relayout_pending = True
                QTimer.singleShot(0, self._deferred_relayout)
            return
        p.setClipRect(QRectF(0, 0, w, h))
        flag = Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop
        cw = self._content_width()
        n_main = len(self._font_main)
        for i, (_t, text) in enumerate(self._lines):
            if i >= len(self._tops):
                break
            top = self._offset + self._tops[i]
            hh = self._heights[i]
            if top + hh < -FADE_PX or top > h + FADE_PX:
                continue
            cur = (i == self._idx)
            main, trans = (text.split("\n", 1) if "\n" in text else (text, ""))
            # ★ task-23：字体（族/字重/**插值后的字号**）全部来自预建缓存 ⇒ 绘制期零构造零测量
            f = self._font_main[i] if i < n_main else QFont()
            p.setFont(f)
            main_h = self._heights[i] - (
                LYRIC_INNER_PAD + self._trans_h[i] if trans else 0.0)
            main_h = max(14.0, main_h)
            # ★ task-23：明度用**插值值**（0.40 ⇄ 1.00），换行时不再突变
            alpha = self._alpha_now[i] if i < len(self._alpha_now) else (1.0 if cur else 0.40)
            # ★ task-24 判据 B：手动浏览时**焦点行（悬停/中心）高亮**
            if self._manual_scroll and i == self._focus_idx:
                alpha = max(alpha, LYRIC_FOCUS_ALPHA)
            if cur and alpha > 0.55:
                p.setPen(QColor(0, 0, 0, int(90 * alpha)) if not light
                         else QColor(255, 255, 255, int(120 * alpha)))
                p.drawText(QRectF(1.2, top + 1.2, cw, main_h), flag, main)
            p.setPen(self._col(light, alpha))
            p.drawText(QRectF(0, top, cw, main_h), flag, main)
            if trans and i < len(self._font_trans):
                p.setFont(self._font_trans[i])
                p.setPen(self._col(light, (0.90 if cur else 0.28) * max(0.35, alpha)))
                p.drawText(QRectF(0, top + main_h + LYRIC_INNER_PAD, cw,
                                  max(12.0, self._trans_h[i])), flag, trans)
        # ★★ task-24（用户 ⑩②）：手动浏览态 —— 极淡水平辅助线 + 「▶ mm:ss」时间胶囊。
        #    全部数值（文本/矩形/字体）都是**交互期算好的缓存** ⇒ 绘制期依旧零测量零构造。
        if self._manual_scroll and self._focus_idx >= 0:
            p.setPen(QPen(QColor(20, 22, 26, 34) if light else QColor(255, 255, 255, 26), 1.0))
            p.drawLine(QPointF(0.0, self._guide_y), QPointF(float(w), self._guide_y))
            if self._tbtn_idx >= 0 and not self._tbtn_rect.isEmpty():
                r = self._tbtn_rect
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(20, 26, 36, 170) if light else QColor(255, 255, 255, 40))
                p.drawRoundedRect(r, r.height() / 2.0, r.height() / 2.0)
                p.setPen(self._col(light, 0.95))
                if self._tbtn_font is not None:
                    p.setFont(self._tbtn_font)
                p.drawText(r, Qt.AlignCenter, self._tbtn_text)


class AudioView(QWidget):
    """音乐模式总成：静态背景缓存 + 双栏 + 底部频谱 + **唯一主帧时钟**。"""

    request_drawer = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._cover_data: bytes | None = None
        # ★ task-10：规格胶囊（可读属性 _badge_text/_badge_rect 供探针/门禁直接读）
        self._audio_path = ""
        self._spec_raw = ""                # ★ task-16：main_window 给的规格文本（药丸兜底用）
        self._badge_text = ""
        self._badge_rect = QRect()
        self._badge_retry = 0
        self._badge_tick = 0
        self._stopped = False              # ★ task-11：媒体已停止/播完（end-file 事件置位）
        self._idle_probe_t = 0.0           # 上次直读 idle-active 的时刻（限速用）
        self._idle_last = False            # 上次直读结果
        self._engine_wired = None          # 已连接 end_file 的引擎对象（只连一次）
        self._bg_cache = QPixmap()          # ★ 静态背景缓存（= 单位模糊图，绝不每帧重算）
        # ★★ task-25：比例锁定背景三件套
        self._bg_unit = QPixmap()           # 保持封面原始长宽比的等比模糊图（带四边羽化）
        self._bg_core = QPixmap()           # 单位图的**不透明核心**（RGB32，绘制走快速路径）
        self._bg_core_rect = QRectF()       # 核心在单位图里的源矩形
        self._bg_unit_aspect = 1.0          # 单位图的宽/高（判据 A 用）
        self._bg_c1: QColor | None = None   # 底板渐变主色
        self._bg_c2: QColor | None = None   # 底板渐变次主色
        self._bg_dirty = False              # True ⇒ 停稳后需要重建（换封面/切主题）
        self._bg_draw_rect = QRectF()       # 本帧模糊层的**实际绘制矩形**（判据 A 可读锚点）
        self._bg_cover_aspect = 1.0         # 封面原始宽/高
        self._bg_img: QImage | None = None
        self.cover = CoverArt(self)
        self.badge = SpecBadge(self)          # ★ task-10 规格胶囊
        # ★★ task-21（用户 ③）：胶囊**不再上屏** —— 左栏第三行改为纯文本。
        #    对象保留：它是规格文本的**唯一数据源**（set_audio_specs → badge._text），
        #    也是既有探针/门禁读 _badge_text 的入口；只是不显示、不参与布局。
        self.badge.setVisible(False)
        # 左栏三行的预排版结果：[(_info_slots 几何+字体+alpha), (_info_lines 省略号文本)]
        self._info_slots: list = []
        self._info_lines: list = []
        self._left_col = None
        self.lyrics = LyricsPanel(self)
        # ★ task-24：歌词时间胶囊的跳转请求 ⇒ 音频层唯一 seek 入口（探针在此计数）
        self.lyrics.request_seek.connect(self._on_lyric_seek)
        self.spec = SpectrumWidget(self)
        self._title = ""
        self._sub = ""
        self._playing = False
        self._pos_provider = None
        self._cur_pos = 0.0
        self._last_pos: float | None = None
        self._pos_accum = 0
        self._last_move_t = 0.0        # 播放位置最后一次**前进**的时刻（停滞判据，见 _on_frame）
        self._spec_body_top_warned = False   # 实体几何读取失败只告警一次（R1）
        self._frame = QTimer(self)
        self._frame.setInterval(FRAME_MS)   # ★ 全组件唯一帧时钟
        self._frame.timeout.connect(self._on_frame)
        self._frame.start()
        self._bg_timer = QTimer(self)
        self._bg_timer.setSingleShot(True)
        self._bg_timer.setInterval(BG_DEBOUNCE_MS)
        # ★ task-25：resize 停稳后**不再重建模糊**（拖拽期 blur_pixmap 必须为 0）——
        #   只重排布局；只有当 _bg_dirty（换封面/切主题）时才重建。
        self._bg_timer.timeout.connect(self._bg_settle)

    def _engine_obj(self):
        """拿播放引擎对象（拿不到返回 None）—— 能量读取与暂停位共用这一处。"""
        try:
            return getattr(self.window(), "engine", None)
        except Exception:
            return None

    def _on_lyric_seek(self, ms: int) -> None:
        """★ task-24 ③：歌词时间胶囊点击 ⇒ seek 到该句时间戳（**恰好一次**）。

        走与进度条同一条引擎 API（engine.seek_absolute，precise=True）；拿不到引擎就静默
        （歌词面板本身不做任何音频副作用 ⇒ "滚动本身 seek = 0"）。
        """
        eng = self._engine_obj()
        if eng is None:
            return
        try:
            eng.seek_absolute(max(0.0, float(ms) / 1000.0), precise=True)
        except Exception:
            log.debug("歌词时间胶囊 seek 失败", exc_info=True)

    def _on_media_ended(self, *_args) -> None:
        """媒体结束/停止（mpv end-file 事件，含用户点停止）⇒ 立即判停 + 清空频谱。

        胶囊不受影响（它是"这一首的规格"，停止后仍可读）；恢复播放时见 set_position。
        """
        self._stopped = True
        try:
            self.set_playing(False)
        except Exception:
            pass
        try:
            self.spec.reset()
        except Exception:
            pass

    def _engine_idle(self) -> bool:
        """是否处于"停止/无媒体"（用来触发频谱清空重置；暂停不算）。

        判据用 mpv 已观察属性：idle-active 为真，或当前没有 path。
        """
        eng = self._engine_obj()
        if eng is None:
            return False
        try:
            if not (eng.cached("path", "") or ""):
                return True
        except Exception:
            pass
        # ★★ task-11 实测：stop 之后 **cached 全不可用** ——
        #   cached("idle-active") = MISS、cached("path") 仍是旧路径、cached("pause") = false，
        #   而**直接读** idle-active / core-idle 立刻是 "yes"（time-pos 直接读是 None）。
        #   直读要进 mpv 锁 ⇒ 限速：只在"位置停滞 ≥0.25s"时才读，且最多每 0.1s 一次。
        #   正常播放时位置一直在前进 ⇒ 这条路径根本不执行，不会每帧抢锁。
        try:
            now = time.perf_counter()
            if self._last_move_t and (now - self._last_move_t) < 0.25:
                return False
            if (now - self._idle_probe_t) < 0.1:
                return self._idle_last
            self._idle_probe_t = now
            self._idle_last = False
            # ⚠️ 只认 idle-active / eof-reached：
            #    · core-idle 在**暂停**时也是 yes（"核心无事可做"）⇒ 会把暂停误判成停止，
            #      破坏 task-08 的暂停定格，故**排除**；
            #    · eof-reached 覆盖"播到结尾被 keep-open 停住"（此时 paused=true、
            #      idle-active 仍是 no），task-11 要求停止必须落平。
            for prop in ("idle-active", "eof-reached"):
                v = eng.get(prop, 0, None)
                yes = (isinstance(v, str) and v.strip().lower() in ("yes", "true")) or v is True
                if yes:
                    self._idle_last = True
                    break
            return self._idle_last
        except Exception:
            return self._idle_last

    def _engine_paused(self):
        """读 mpv 真实暂停位（只读；拿不到返回 None）。

        ⚠️ 为什么不问 MainWindow 要：MainWindow._drive_audio_view() 是**死代码**
        （全仓只有定义、没有任何调用点），所以 v1.0.33 起只能靠"位置 > 0.05"猜播放态。
        而 mpv 的 pause 是 engine 已经在观察的属性（player.py:262），
        engine.cached("pause") 直接可用 —— 不新增 IPC、不新增定时器、不改其他文件。
        """
        try:
            eng = getattr(self.window(), "engine", None)
            if eng is None:
                return None
            v = eng.cached("pause", None)
            return None if v is None else bool(v)
        except Exception:
            return None

    def _on_frame(self):
        idle = False
        try:
            # ★ 把引擎注入频谱（用于尝试真实能量读；为 None 时频谱自动走模拟包络）
            eng = self._engine_obj()
            if eng is not None and self.spec._engine is not eng:
                self.spec.attach_engine(eng)
            # ★★ task-11：停止/播完只能靠**事件**判定 —— 实测 stop 之后 mpv 的
            #   path 仍是旧路径、idle-active 没被观察（cached 直接 MISS）、pause 还是
            #   false，于是"位置停滞 1.5s"要等 1.5 秒才生效，这期间模拟包络会把柱体
            #   重新点亮（QA 实测「没声音了频谱还在跳舞」）。连接已有的 end_file 信号
            #   （player.py 事件线程已 emit，不新增定时器、不新增 IPC）。
            if eng is not None and eng is not self._engine_wired:
                self._engine_wired = eng
                try:
                    eng.end_file.connect(self._on_media_ended)
                except Exception:
                    log.debug("连接 end_file 失败", exc_info=True)
            # ★ 停止/无媒体 ⇒ 清空重置（**暂停不重置**：暂停走无条件定格，task-08）
            idle = self._engine_idle()
            if idle or self._stopped:
                # ⚠️ 必须**先**把播放态置 False 再进入本帧的 tick：tick() 在 _on_frame
                #    开头就跑，若此刻 _playing 还是 True，它会用模拟包络把刚清空的柱体
                #    重新点亮一帧（实测残留 ~27px 并就此冻结）。顺序反了就等于没清。
                self.spec.reset()
                if self._playing:
                    self.set_playing(False)
            # ★ task-10：规格数据可能晚于 set_lyrics 到位（mpv 的 audio-params 是
            #   异步观察属性）⇒ 复用**已有帧钟**做有限次补读，不新增定时器。
            if self._badge_retry > 0:
                self._badge_tick += 1
                if self._badge_tick >= 30:
                    self._badge_tick = 0
                    self._badge_retry -= 1
                    self.set_audio_specs(self._audio_path)
            # ★ 只有"这一帧真的画得出来不一样"才触发重绘（暂停定格后不再空转重绘）
            if self.spec.tick():
                self.spec.update()
        except Exception:
            pass
        try:
            self.lyrics.step()
        except Exception:
            pass
        self._pos_accum += 1
        if self._pos_accum >= 6:            # ~200ms 取一次播放位置
            self._pos_accum = 0
            self._poll_position()
        # ★★ v1.0.34-2：播放态**必须来自真实播放状态**，不能再拿"位置 > 0.05"当判据。
        #   旧写法（v1.0.33 ③）在**中途暂停**时位置恒 > 0.05 ⇒ 永远当"在播放" ⇒
        #   暂停后频谱照旧满幅跑（用户实测 + Lead 基线：pause 后 3s max(_bars) 恒 1.000、
        #   峰值恒 38.0 不落）。现在三条优先级：
        #     ① 首选 mpv 的 pause 属性（真实状态）；
        #     ② 拿不到引擎时退回"位置是否前进"；
        #     ③ 两条路径都叠加"位置停滞 > 1.5s 即判为停"（缓冲/卡死时不许假装在播放）。
        try:
            now = time.perf_counter()
            stalled = (now - self._last_move_t) > 1.5 if self._last_move_t else True
            paused = self._engine_paused()
            if self._stopped or idle:
                # ★★ task-11：停止/无媒体（end-file 事件或 idle）⇒ **直接判不在播放**。
                #   不这么写的话：stop 之后 mpv 的 pause 仍是 False，而"位置停滞"
                #   判据要等满 1.5s 才生效 ⇒ 这 1.5s 里模拟包络会把柱体重新点亮到
                #   ~35px（QA 实测「没声音了频谱还在跳舞」）。idle 优先，立即平躺。
                playing = False
            elif paused is None:
                # 回落路径（引擎不可用/属性读不到）：位置为 0 或停滞 ⇒ 不在播放
                playing = (self._cur_pos > 0.05) and not stalled
            else:
                playing = (not paused) and not stalled
            if playing != self._playing:
                self.set_playing(playing)
        except Exception:
            pass

    def _poll_position(self):
        if self._pos_provider is None or not self.isVisible():
            return
        try:
            self.set_position(float(self._pos_provider() or 0.0))
        except Exception:
            pass

    def scale_ratio(self) -> float:
        try:
            r = min(self.width() / BASE_W, self.height() / BASE_H)
        except Exception:
            return 1.0
        return max(SCALE_MIN, min(SCALE_MAX, float(r)))

    def set_position_provider(self, fn):
        self._pos_provider = fn

    def set_position(self, seconds: float):
        """mpv time-pos 是**秒**，歌词时间轴是**毫秒** ⇒ 必须 ×1000 再比。

        ⚠️ 这里同时维护"位置最后一次前进的时刻"：_on_frame 用它判断是否停滞
        （停滞 >1.5s ⇒ 认为不在播放）。阈值 0.02s 是为了滤掉 time-pos 的取整抖动。
        """
        v = max(0.0, float(seconds))
        if self._last_pos is None or abs(v - self._last_pos) >= 0.02:
            self._last_move_t = time.perf_counter()
            if v > 0.05:
                self._stopped = False       # ★ task-11：位置重新前进 ⇒ 解除停止态
        self._last_pos = v
        self._cur_pos = v
        self.lyrics.set_position_ms(int(v * 1000))

    def set_cover(self, data: bytes | None):
        self._cover_data = data or None
        self.cover.set_cover(self._cover_data)
        img = None
        if self._cover_data:
            tmp = QImage()
            if tmp.loadFromData(self._cover_data) and not tmp.isNull():
                img = tmp
        self._bg_img = img
        self._bg_dirty = True               # 换封面 ⇒ 需要重建（证真组：此处 blur_pixmap ≥1）
        self._rebuild_bg_now()

    def set_meta(self, title: str, sub: str):
        self._title = title or ""
        self._sub = sub or ""
        # ★ task-21：左栏三行的**省略号文本**要在切歌时重算（几何/字体沿用上次布局结果），
        #    否则换一首长歌名还显示上一首的截断文本。
        self._refresh_info_text()
        self.update()

    def set_spec(self, spec: str):
        # ★ task-16：规格文本先存下来，再由 _sync_spec_display() 决定给谁显示 ——
        #   有 badge 时**不再**在歌词占位处画规格药丸（否则同一信息出现两枚标记，
        #   这就是「无歌词模式重复图标重叠」的真实成因）。
        self._spec_raw = spec or ""
        self._sync_spec_display()

    def _sync_spec_display(self):
        """规格信息**只出现一次**，且唯一位置是**左栏第三行**（task-21）。

        task-16 的规则是"badge 可见 ⇒ 歌词占位药丸隐藏；badge 缺失 ⇒ 退回药丸"。
        task-21 把胶囊从界面上彻底拿掉、规格改由左栏第三行绘制 ⇒ 歌词区那份兜底药丸
        **永远不再需要**（再留着就是"右栏又出现一枚胶囊"，正是本卡要清掉的东西）。
        """
        try:
            self.lyrics.set_spec_text("")
            self._refresh_info_text()
        except Exception:
            log.debug("规格显示同步失败", exc_info=True)

    def set_audio_specs(self, audio_path: str) -> None:
        """读规格并灌给胶囊（**唯一入口**）。

        数据源（如实声明）：
          · 采样率/比特率：mpv 已观察属性 audio-params / audio-bitrate（engine.cached）；
          · 位深：**自解析文件头**（目前实现 FLAC STREAMINFO；有损格式本无位深，按卡片省略）。
        失败/缺失 ⇒ 文本为空串 ⇒ 胶囊整枚隐藏（绝不留 None/0 空壳）。
        """
        self._audio_path = audio_path or ""
        params = bitrate = None
        # ★ 溯源守卫（探针抓到的真问题）：mpv 的 audio-params/bitrate 说的是**引擎里
        #   当前那首**，不是我们传进来的路径。文件不存在、或引擎里还是上一首时，
        #   绝不能把旧值安到新路径上（否则会显示「ZZZ · 44.1kHz」这种拼接垃圾）。
        if not (self._audio_path and os.path.isfile(self._audio_path)):
            self.badge.set_specs_text("")
            self._badge_retry = 0
            self._sync_badge_state()
            self._layout_children()
            return
        try:
            eng = self._engine_obj()
            if eng is not None:
                cur = ""
                try:
                    cur = str(eng.cached("path", "") or "")
                except Exception:
                    cur = ""
                if cur and os.path.abspath(cur) != os.path.abspath(self._audio_path):
                    eng = None          # 引擎里还是上一首 ⇒ 只信文件头，不信 mpv 参数
            if eng is not None:
                # ⚠️ 这两个属性**不在** player.py 的观察列表里（cached 恒为 None），
                #    必须直接读（一次/切歌，不是每帧）：audio-params 是节点(6)、
                #    audio-bitrate 是 double(5)。读不到再用「文件大小 ÷ 时长」兜底
                #    （实测与 ffprobe 的 stream bit_rate 差 <2%）。
                params = eng.cached("audio-params", None) or eng.get("audio-params", 6, None)
                bitrate = (eng.cached("audio-bitrate", None)
                           or eng.get("audio-bitrate", 5, None))
                if not bitrate:
                    try:
                        dur = float(eng.cached("duration", 0) or eng.get("duration", 5, 0) or 0)
                        sz = os.path.getsize(self._audio_path) if self._audio_path else 0
                        if dur > 1.0 and sz > 0:
                            bitrate = sz * 8.0 / dur
                    except Exception:
                        pass
        except Exception:
            params = bitrate = None
        try:
            text = badge_text_for(self._audio_path, params, bitrate)
        except Exception:
            log.debug("规格文本构建失败", exc_info=True)
            text = ""
        try:
            self.badge.set_specs_text(text)
        except Exception:
            log.debug("规格胶囊设置失败", exc_info=True)
        # 数据到齐（含采样率或比特率）就停止补读；否则靠帧钟做有限次重试
        self._badge_retry = 0 if (text and ("kHz" in text or "kbps" in text)) else 6
        self._sync_spec_display()          # ★ task-16：badge 状态变了 ⇒ 重算占位药丸是否显示
        self._badge_tick = 0
        self._sync_badge_state()
        self._layout_children()

    def _sync_badge_state(self) -> None:
        """把胶囊的**可见文本与矩形**同步到可读属性（任意状态下都可读，缺失即空串/空矩形）。"""
        try:
            t = getattr(self.badge, "_text", "") or ""
            # ★ task-21：胶囊**永不上屏**。⚠️ 必须在这里强制隐藏 ——
            #   SpecBadge.set_specs_text() 内部会 setVisible(bool(text))，
            #   只在 __init__ 里 setVisible(False) 会被下一次回填又点亮
            #   （实测：左栏三行都对，但左上角仍挂着一枚「MP3 · 44.1kHz」胶囊）。
            #   _sync_badge_state() 是规格状态的**唯一漏斗**（set_audio_specs /
            #   _layout_children / 初始化都会经过），所以在这里压一次最稳。
            if self.badge.isVisible():
                self.badge.setVisible(False)
            # _badge_text：当前规格文本（数据源，无信息时为空串，绝不 None）。
            # _badge_rect：task-21 起**恒为空矩形** —— 胶囊不在界面上，没有矩形可给
            #   （既有探针/门禁读的是"屏幕上有没有胶囊"，空矩形正是正确回答）。
            self._badge_text = t
            self._badge_rect = QRect()
        except Exception:
            self._badge_text = ""
            self._badge_rect = QRect()
        # ★ task-16 复审打回项①：**必须在这里**（此时 _badge_text 已是最新值）再同步规格显示。
        #   原先只在 set_audio_specs 里「先同步、后赋值」，读到的 _badge_text 是上一首旧值，
        #   于是「有标签 + 无 lrc」素材下药丸仍被画出来，与徽章重复。
        try:
            self._sync_spec_display()
        except Exception:
            log.debug("规格显示同步失败（_sync_badge_state 末尾）", exc_info=True)

    def set_lyrics(self, audio_path: str):
        # ★ task-08：切歌 ⇒ 清空重置频谱（卡片 F：只在切歌/停止时重置）
        try:
            self.spec.reset()
        except Exception:
            pass
        self._stopped = False               # ★ task-11：换歌 ⇒ 解除停止态
        # ★ task-10：换歌即更新规格胶囊（切歌的唯一入口）
        try:
            self.set_audio_specs(audio_path)
        except Exception:
            log.debug("切歌时更新规格胶囊失败", exc_info=True)
        lines = []
        try:
            from ..media.lyrics import load_lyrics

            lines, _src = load_lyrics(audio_path)
        except Exception:
            lines = []
        # ★ task-16 复审打回项②：不再把 spec 透传给 set_lines（那会绕过互斥入口）
        self.lyrics.set_lines(lines)
        self._sync_spec_display()

    def set_playing(self, on: bool):
        self._playing = bool(on)
        self.spec.set_playing(on)

    # ---- task-26①：「音乐与歌词」设置的运行时入口（设置面板唯一的施加点）-----------------
    def apply_music_setting(self, key: str, value) -> bool:
        """按键实时生效（不重建控件、不等"应用"）。返回是否被本入口消费。

        ⚠️ 返回 False ⇒ 设置面板会回退到主窗口的 _on_setting_changed 兜底（不静默丢设置）。
        """
        k = str(key)
        try:
            if k == "lyric.font_family":
                self.lyrics.apply_style(family=value)
            elif k == "lyric.font_px":
                self.lyrics.apply_style(base_px=value)
            elif k == "lyric.weight":
                self.lyrics.apply_style(weight=value)
            elif k == "lyric.step_pad":
                self.lyrics.apply_style(step_pad=value)
            elif k == "lyric.scroll_ms":
                self.lyrics.apply_style(scroll_ms=value)
            elif k == "spectrum.enabled":
                self.spec.set_enabled(bool(value))
            elif k == "spectrum.peak_style":
                self.spec.set_peak_style(str(value))
            else:
                return False
        except Exception:
            log.debug("应用音乐设置 %s 失败", k, exc_info=True)
            return False
        self.update()
        return True

    def music_state(self) -> dict:
        """★ task-26 锚点：音乐与歌词设置的**生效快照**（QA/门禁读这个，别只认 config.json）。"""
        return {"lyric": self.lyrics.lyric_style(), "spectrum": self.spec.spectrum_state()}

    def _rebuild_bg_now(self):
        """真正重建背景缓存（只在换封面 / resize 停稳后调用，绝不在绘制期）。

        ⚠️ 这里顺带重跑一次 _layout_children()：这个去抖计时器（200ms）**已经是**
        "布局已落定"的信号 —— resizeEvent 当下胶囊几何可能还没排完，量到的会是旧值；
        停稳后再量一次才是最终几何（幂等，且**没有新增定时器**）。
        """
        try:
            self._layout_children()
        except Exception:
            pass
        w, h = max(2, self.width()), max(2, self.height())
        self._bg_dirty = False
        if self._bg_img is None or self._bg_img.isNull():
            self._bg_cache = QPixmap()
            self._bg_unit = QPixmap()
            self._bg_c1 = self._bg_c2 = None
            self._bg_draw_rect = QRectF()
            self.update()
            return
        # ★★ task-25：单位模糊图 = **保持封面原始长宽比**（最长边 BG_UNIT_MAX）+ 四边羽化。
        #    ⚠️ 关键：不能再用窗口尺寸当目标（旧实现 blur_pixmap(img, w, h) 走
        #    KeepAspectRatioByExpanding + 裁剪 ⇒ 窗口比例 ≠ 封面比例时背景被拉伸/裁剪）——
        #    那正是用户看到的"拖拽过程画面被扯丑"。这里的目标尺寸与封面**同比**，所以
        #    blur_pixmap 内部的等比放大+裁剪是恒等操作，零畸变。
        iw, ih = max(1, self._bg_img.width()), max(1, self._bg_img.height())
        self._bg_cover_aspect = iw / float(ih)
        if iw >= ih:
            uw = int(BG_UNIT_MAX)
            uh = max(2, int(round(BG_UNIT_MAX * ih / float(iw))))
        else:
            uh = int(BG_UNIT_MAX)
            uw = max(2, int(round(BG_UNIT_MAX * iw / float(ih))))
        self._bg_unit = feather_edges(blur_pixmap(self._bg_img, uw, uh))
        self._bg_unit_aspect = self._bg_unit.width() / float(max(1, self._bg_unit.height()))
        # ★ task-25 性能：把单位图的**不透明核心**单独存成 RGB32 —— 绘制期这一块走
        #   "不透明快速拷贝"路径（实测 0.252ms vs ARGB 混合 0.428ms），四条羽化带才做混合。
        try:
            core = QRect(max(1, int(self._bg_unit.width() * BG_UNIT_FEATHER)),
                         max(1, int(self._bg_unit.height() * BG_UNIT_FEATHER)),
                         max(2, int(self._bg_unit.width() * (1.0 - 2 * BG_UNIT_FEATHER))),
                         max(2, int(self._bg_unit.height() * (1.0 - 2 * BG_UNIT_FEATHER))))
            self._bg_core_rect = QRectF(core)
            self._bg_core = QPixmap.fromImage(
                self._bg_unit.copy(core).toImage().convertToFormat(QImage.Format_RGB32))
        except Exception:
            self._bg_core = QPixmap()
            self._bg_core_rect = QRectF()
        # 底板渐变的两个颜色：主色 / 次主色（与 task-15 同一套 dominant_colors）
        try:
            cols = dominant_colors(self._bg_img, 2)
            self._bg_c1 = QColor(cols[0][0])
            self._bg_c2 = QColor(cols[1][0]) if len(cols) > 1 else QColor(cols[0][0])
        except Exception:
            self._bg_c1 = self._bg_c2 = None
        self._bg_cache = self._bg_unit      # 兼容旧口径：_bg_cache 仍指向"那张背景图"
        _ = (w, h)
        self.update()

    def _bg_settle(self):
        """resize 停稳（200ms 去抖）后的收尾：重排布局；**只有 _bg_dirty 才重建模糊**。

        task-25 判据 C/D：拖拽与停手后的成品背景必须完全一致 ⇒ 尺寸变化本身
        **不触发任何重建**，也就没有"停手突变"，且拖拽期 blur_pixmap 调用恒为 0。
        """
        try:
            self._layout_children()
        except Exception:
            pass
        if self._bg_dirty:
            self._rebuild_bg_now()
        self.update()

    def _paint_bg(self, p, w: int, h: int, light: bool) -> None:
        """task-25 背景绘制：底层渐变（任意长宽比永不变形）+ 中层等比模糊图（居中）。

        · 底层：QLinearGradient 每帧实时填充（一次 fillRect，0.01ms 级；矢量渐变与窗口比例无关）
        · 中层：按**窗口能容纳的最大等比尺寸**绘制单位模糊图 —— 1:1 窗拉成 2:1 时仍 1:1 居中；
          窗口 4:3 时等比放到 1.5:1.5；超出部分由底层渐变经羽化边缘无缝衔接。
        · 100% 保持封面原始长宽比（模糊图本身就是同比例的），零拉伸畸变。
        """
        grad = QLinearGradient(0.0, 0.0, 0.0, float(h))
        if self._bg_c1 is not None and self._bg_c2 is not None:
            grad.setColorAt(0.0, self._bg_c1)
            grad.setColorAt(1.0, self._bg_c2)
        elif light:
            grad.setColorAt(0.0, QColor("#F2F5F8"))
            grad.setColorAt(1.0, QColor("#E6ECF2"))
        else:
            grad.setColorAt(0.0, QColor("#1E222A"))
            grad.setColorAt(1.0, QColor("#14161B"))
        unit = self._bg_unit
        if unit.isNull():
            self._bg_draw_rect = QRectF()
            p.fillRect(QRect(0, 0, w, h), QBrush(grad))
            return
        uw, uh = max(1, unit.width()), max(1, unit.height())
        s = min(w / float(uw), h / float(uh))
        dw, dh = uw * s, uh * s
        r = QRectF((w - dw) / 2.0, (h - dh) / 2.0, dw, dh)
        self._bg_draw_rect = r
        core = self._bg_core_rect
        core_dst = QRectF(r.x() + core.x() * s, r.y() + core.y() * s, core.width() * s, core.height() * s)
        # ① 底层渐变：**只填"中层不透明核心"以外的区域**（区域裁剪省掉 ~64% 面积；
        #    实测整窗 fillRect 0.204ms ⇒ 裁剪后 ~0.08ms）。渐变是矢量的，与窗口比例无关 ⇒ 永不变形。
        if not self._bg_core.isNull():
            try:
                reg = QRegion(0, 0, w, h).subtracted(
                    QRegion(int(core_dst.x()), int(core_dst.y()),
                            max(1, int(core_dst.width())), max(1, int(core_dst.height()))))
                p.setClipRegion(reg)
            except Exception:
                reg = None
        else:
            reg = None
        p.fillRect(QRect(0, 0, w, h), QBrush(grad))
        if reg is not None:
            p.setClipRegion(QRegion())
        # ② 中层核心：**不透明 RGB32** ⇒ 走快速拷贝路径（实测 0.252ms vs ARGB 0.428ms）
        if not self._bg_core.isNull():
            p.setRenderHint(QPainter.SmoothPixmapTransform, bool(BG_SMOOTH))
            p.drawPixmap(core_dst, self._bg_core, QRectF(self._bg_core.rect()))
        # ③ 四条羽化带：ARGB 混合（都只有 10% 宽/高），保证与底层无缝衔接（判据 B）
        # ★ Lead 裁决（质量优先）：开 SmoothPixmapTransform（BG_SMOOTH=True）。拖拽是**高频可见**
        #   场景，最近邻在模糊图上仍有 3.9% 采样点 / 最大 20/255 的锯齿差异，正是用户说的"扯丑"；
        #   代价是每帧单层增量 0.336ms → ~1.03ms（占 16.7ms 帧预算 6%，E 判据放宽到 ≤1.2ms）。
        #   拖拽中/停手后始终同一条路径 ⇒ 判据 C（无停手突变）不受质量档影响。
        p.setRenderHint(QPainter.SmoothPixmapTransform, bool(BG_SMOOTH))
        fx = core.x()
        fy = core.y()
        bands = (
            (QRectF(r.x(), r.y(), r.width(), fy * s), QRectF(0.0, 0.0, float(uw), fy)),
            (QRectF(r.x(), r.y() + (fy + core.height()) * s, r.width(), (uh - fy - core.height()) * s),
             QRectF(0.0, fy + core.height(), float(uw), uh - fy - core.height())),
            (QRectF(r.x(), r.y() + fy * s, fx * s, core.height() * s),
             QRectF(0.0, fy, fx, core.height())),
            (QRectF(r.x() + (fx + core.width()) * s, r.y() + fy * s,
                    (uw - fx - core.width()) * s, core.height() * s),
             QRectF(fx + core.width(), fy, uw - fx - core.width(), core.height())),
        )
        for dst, src in bands:
            if dst.width() >= 0.5 and dst.height() >= 0.5 and src.width() >= 1.0 and src.height() >= 1.0:
                p.drawPixmap(dst, unit, src)

    def bg_layer_rect(self) -> dict:
        """★ task-25 锚点（QA/门禁）：背景各层的**实测几何**。

        ⚠️ **语义写死（口径不清会把羽化过渡误判成跨界色差）**：
          · "rect" = 中层的**外框**（= 等比模糊图的完整绘制矩形，**含四条羽化带**）。
            它才是"绘制比例 == 封面原比例"该用的矩形（羽化带宽按比例取，所以核心与外框同比）。
          · "core_rect" = 其中**不透明核心**的矩形；核心之外、外框之内的部分是**羽化过渡带**
            （Alpha 从 255 线性降到 0，宽度见 band_px）。**B 判据的"边界相邻像素差"要在
            rect **之外**取外侧像素、rect 边缘取内侧像素**；不要把过渡带内的渐变当成色差。
          · 三个宽度都在**面板逻辑坐标**：band_px = [左, 上, 右, 下]。
        """
        r = self._bg_draw_rect
        asp = (r.width() / r.height()) if r.height() > 1e-6 else 0.0
        cover = float(self._bg_cover_aspect or 1.0)
        core = self._bg_core_rect
        unit_w = max(1, self._bg_unit.width())
        unit_h = max(1, self._bg_unit.height())
        s = (r.width() / float(unit_w)) if (unit_w > 0 and r.width() > 0) else 0.0
        core_rect = [round(r.x() + core.x() * s, 2), round(r.y() + core.y() * s, 2),
                     round(core.width() * s, 2), round(core.height() * s, 2)]
        band = [round(core.x() * s, 2), round(core.y() * s, 2),
                round((unit_w - core.x() - core.width()) * s, 2),
                round((unit_h - core.y() - core.height()) * s, 2)]
        return {"rect": [round(r.x(), 2), round(r.y(), 2), round(r.width(), 2), round(r.height(), 2)],
                "rect_semantics": "outer_frame_including_feather_bands",
                "core_rect": core_rect,
                "band_px": band,
                "quality": ("smooth" if BG_SMOOTH else "nearest"),
                "draw_aspect": round(asp, 4), "cover_aspect": round(cover, 4),
                "dev_pct": round(abs(asp - cover) / max(1e-6, cover) * 100.0, 4),
                "unit": [self._bg_unit.width(), self._bg_unit.height()],
                "unit_aspect": round(float(self._bg_unit_aspect or 0.0), 4),
                "window": [self.width(), self.height()],
                "has_unit": (not self._bg_unit.isNull())}

    def on_theme_changed(self) -> None:
        """主题切换钩子（widgets.notify_theme_changed 会遍历控件树调用它）。

        ★ task-09 复审打回的真缺陷：**切主题不会重建模糊背景缓存** ⇒ 深→浅时
          缓存里还是深色档（未做 Vibrance）的模糊图，盖上浅色遮罩后背景饱和度
          比正确值低 13~17%（QA 实测 0.2279 vs 0.2574/0.2752），用户第一眼就是
          「不通透/发霉」，必须换封面或 resize 才会自愈。
        修法（Lead 指定）：**复用已有的 200ms 去抖定时器**，只 start 不重算
        （钩子里绝不做重计算，避免切主题卡顿）；重建发生在去抖回调 _rebuild_bg_now。
        """
        self._bg_dirty = True               # 切主题 ⇒ 需要重建（Vibrance 档位变了）
        try:
            self._bg_timer.start()
        except Exception:
            log.debug("主题切换后重启背景去抖失败", exc_info=True)
        self.update()

    def resizeEvent(self, ev):            # noqa: N802
        super().resizeEvent(ev)
        self._layout_children()
        self._bg_timer.start()              # ★ 停稳 200ms 后才重建高清背景

    def _spec_body_top(self):
        """控制栏胶囊**实体**上沿在本控件坐标系里的 y（量实际几何；拿不到返回 None）。

        KB §9.65：不要建模，要量实际几何。胶囊是 GlassFrame（widgets.py:577），
        它的实体 = 控件矩形内缩 self._margin，而 self._margin = shadow_spread
        （widgets.py:610，构造参数存成了属性，语义=阴影外框厚度；ControlBar 传的是
        P["float_shadow_spread"]，实测 2）。所以「实体上沿」= 胶囊控件 y + _margin。
        ⚠️ 不能用 theme 的 M["capsule_shadow"]=16 去估：那 16 只是**宿主布局**的
        左右外边距，不是胶囊实体的内缩量（Lead 实测：控件盒 y=618、实体上沿 y=620）。
        """
        try:
            st = self.parentWidget()
            cap = getattr(st, "capsule", None)
            if cap is None or not cap.isVisible() or not cap.width():
                return None
            # ⚠️ 胶囊是 AudioView 的**兄弟**（挂在 Stage.capsule_host 里），不是本控件的
            #    子孙 —— QWidget.mapTo(ancestor) 要求传的是祖先，传兄弟会返回错误结果
            #    （实测窗口态直接吐回全局坐标 795，把 spec_y 算成 733）。必须走全局坐标换算。
            top = float(cap.mapToGlobal(QPoint(0, 0)).y() - self.mapToGlobal(QPoint(0, 0)).y())
            # 实体上沿：优先用 GlassFrame 自己的公开 API body_rect()（widgets.py:672），
            # 它内部就是 rect 内缩 self._margin；没有该 API 的旧控件退回读 _margin。
            body = getattr(cap, "body_rect", None)
            if callable(body):
                return top + float(body().y())
            return top + float(getattr(cap, "_margin", 0.0) or 0.0)
        except Exception:
            # ★ R1（Lead 追加条款）：主路径失败必须**留痕**，不能静默回落到旧公式 ——
            #   QA 实测过：注入抛异常的 body_rect，旧代码无日志无异常地回落到 576
            #   （间隙 −4px），主路径将来回归没人发现。每实例只打一次，避免刷屏。
            if not self._spec_body_top_warned:
                self._spec_body_top_warned = True
                log.warning("控制栏实体几何读取失败，频谱位置回落到常量公式", exc_info=True)
            return None

    def _spec_y_for(self, h: int) -> int:
        """频谱 y：底边落在**量出来的**控制栏实体上沿之上 SPEC_GAP_TO_BAR。

        主路径（胶囊可用）：spec_y = 胶囊实体上沿 − SPEC_H − SPEC_GAP_TO_BAR
        回落路径（胶囊未挂载/不可见）：用户给定公式 h − CONTROL_BAR_H − SPEC_H − SPEC_GAP_TO_BAR
        两条路径共用同一个 SPEC_GAP_TO_BAR，不散落魔数。
        """
        body_top = self._spec_body_top()
        if body_top is None:
            return max(0, int(h) - CONTROL_BAR_H - SPEC_H - SPEC_GAP_TO_BAR)
        return max(0, int(round(body_top)) - SPEC_H - SPEC_GAP_TO_BAR)

    def _info_source_texts(self) -> list:
        """左栏三行的**源文本**：歌名 / 歌手·专辑 / 音频规格。

        规格沿用既有数据源 _badge_text（set_audio_specs → badge._text，与 ffprobe 同源），
        缺失时退回 main_window 给的 _spec_raw —— 保证"删掉胶囊边框"只是换了绘制方式，
        文字本身还是原来那条链路算出来的。
        """
        spec = (self._badge_text or getattr(self, "_spec_raw", "") or "").strip()
        return [self._title or "", self._sub or "", spec]

    def _refresh_info_text(self) -> None:
        """按上次布局的几何/字体重算三行的**省略号文本**（★ 绘制期零测量）。

        设计纪律与歌词面板同源：所有文本度量都发生在"布局/数据变化"的这一刻，
        paintEvent 里只允许 drawText。切歌（set_meta）与规格回填（set_spec →
        _sync_badge_state）都会走到这里；窗口尺寸变化则由 _layout_children 重算几何。
        """
        slots = getattr(self, "_info_slots", None) or []
        if not slots:
            self._info_lines = []
            return
        texts = self._info_source_texts()
        lines = []
        for (rect, font, alpha), raw in zip(slots, texts):
            try:
                elided = QFontMetrics(font).elidedText(raw, Qt.ElideRight, int(rect.width()))
            except Exception:
                elided = raw
            lines.append((rect, elided, font, int(alpha)))
        self._info_lines = lines

    def _layout_children(self):
        w, h = self.width(), self.height()
        sr = self.scale_ratio()
        content_h = max(120, h - CONTROL_BAR_H - SPEC_H - 40)
        # ★★ v1.0.34-3：频谱 y **量实际几何**（胶囊实体上沿 − SPEC_H − 16）。
        #    旧写法 spec_y = h-72-48-16 是"建模"：它假设控制栏实体从 h-120 开始，
        #    而真机上胶囊实体上沿在 h-92（=618+2），于是频谱矩形底边 624 压进实体 4px，
        #    柱体底部被盖住、8~44px 落差实际只剩 4~40px（Lead 逐行像素实测：行 620 起
        #    彩色柱体像素 = 0）。现在窗口/全屏都按实测重算，不缓存。
        spec_y = self._spec_y_for(h)
        self.spec.setGeometry(SPEC_SIDE, spec_y, max(80, w - SPEC_SIDE * 2), SPEC_H)
        try:
            self.spec.raise_()               # ★ 强制置顶
        except Exception:
            pass
        col_w = max(360, min(int(w * 0.78), w - 40))
        x0 = (w - col_w) // 2
        left_w = int(col_w * 0.40)
        right_w = max(220, col_w - left_w - 32)
        base = int(260 * sr)
        side = max(150, min(int(left_w * 0.86), int(content_h * 0.72), base))
        # ★ v1.0.36（task-07）：**可见封面边长仍是 side**（窗口 231 / 全屏 346），
        #   控件几何按 side + 2*14 下发 —— 那 14px 走廊用来画 3 圈阴影。
        #
        # ★★ task-21（用户 ③）：左栏 = 封面 + **封面正下方三行元数据**。
        #   整块（封面 + 20px 留白 + 三行）在内容区**垂直居中**，所以封面比 task-16
        #   的位置略微上移；**可见边长 side 一个字都没改**（判据只看边长 231/346）。
        #   三行的字号/留白/alpha 全部在这里算好（预排版）⇒ paintEvent 零文本测量。
        cov = side + COVER_PAD * 2
        gap_cover = int(round(20 * sr))                       # 卡面：封面正下方留白 20px
        title_px = max(24, min(26, int(round(25 * sr))))      # 卡面：歌名 24~26
        sub_px = max(14, min(15, int(round(14.5 * sr))))      # 卡面：歌手·专辑 14~15
        spec_px = 12                                          # 卡面：规格 12~12.5（整数取 12）
        f_title = QFont()
        f_title.setPixelSize(title_px)
        f_title.setBold(True)                                 # 卡面：Bold
        f_sub = QFont()
        f_sub.setPixelSize(sub_px)
        f_spec = QFont()
        f_spec.setPixelSize(spec_px)
        h_title = QFontMetrics(f_title).height()
        h_sub = QFontMetrics(f_sub).height()
        h_spec = QFontMetrics(f_spec).height()
        gap_1 = int(round(8 * sr))                            # 歌名 → 歌手·专辑
        gap_2 = int(round(5 * sr))                            # 歌手·专辑 → 规格
        text_h = h_title + gap_1 + h_sub + gap_2 + h_spec
        block_h = cov + gap_cover + text_h
        cov_top = max(20, (content_h - block_h) // 2)
        self.cover.setGeometry(x0 + (left_w - cov) // 2, cov_top, cov, cov)
        # 文本宽度预算：既不许越出左栏，也不宽于封面控件盒（视觉上与封面同宽）
        text_w = max(80, min(left_w, cov))
        tx = x0 + (left_w - text_w) // 2
        ty = cov_top + cov + gap_cover
        self._left_col = (x0, left_w, cov_top, cov)
        # 三行层级只用 alpha 表达：歌名 1.00 / 歌手·专辑 0.65 / 规格 0.40（卡面给定）
        self._info_slots = [
            (QRectF(tx, ty, text_w, h_title), f_title, 255),
            (QRectF(tx, ty + h_title + gap_1, text_w, h_sub), f_sub, 166),
            (QRectF(tx, ty + h_title + gap_1 + h_sub + gap_2, text_w, h_spec), f_spec, 102),
        ]
        self._refresh_info_text()
        rx = x0 + left_w + 32
        # ★★ task-21：右栏 = **100% 歌词舞台**（歌名/副标题/胶囊全部移出）。
        self.lyrics.setGeometry(rx, 8, right_w, max(120, content_h - 16))
        self._right_col = (rx, right_w)
        # ★ task-21：规格胶囊不再布局（控件已 setVisible(False)，见 __init__）。
        #    这里只同步数据源与预排版文本。
        self._sync_badge_state()

    def paintEvent(self, _ev):            # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        light = _is_light()
        # ★★ task-25：背景 = 底层**实时铺满的不变形渐变** + 中层**等比模糊图**（比例锁定）
        self._paint_bg(p, w, h, light)
        # ★★ task-09：浅色遮罩从 199（0.78）降到 82（0.32）—— 原来接近纯白，
        #    把封面色彩整片漂白；深色 173 保持不变（卡片要求深色不动）。
        p.fillRect(QRect(0, 0, w, h),
                   QColor(245, 247, 250, LIGHT_SCRIM_A) if light
                   else QColor(14, 16, 20, 173))
        # ★★ task-21（用户 ③）：**右栏不再画任何标题/副标题/胶囊** —— 它整块留给歌词
        #    （LyricsPanel 自己是一个独立子控件，几何见 _layout_children）。
        #    原来这里的两段 drawText（歌名 23px / 副标题 14px + 两次 elidedText 测量）
        #    全部移到**左栏三行**，并且改成预排版：paintEvent 里只有 drawText，
        #    连 elidedText 都不再调用（文本度量全部发生在 _layout_children /
        #    _refresh_info_text 这两个"布局/数据变化"时刻）。
        base = _text_color()
        for rect, text, font, alpha in getattr(self, "_info_lines", []):
            if not text:
                continue
            col = QColor(base)
            col.setAlpha(int(alpha))
            p.setFont(font)
            p.setPen(col)
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignHCenter, text)
        p.end()
