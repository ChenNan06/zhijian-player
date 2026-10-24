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
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetrics, QImage, QLinearGradient,
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
# ★★ task-28（用户 ④⑨）：频谱视觉规格 = 贴底单向生长的细腻多峰声波
SPEC_BAR_W = 3                     # 柱宽 3px（卡片）
SPEC_GAP = 2                       # 柱间距 2px（卡片）⇒ 节距 5px
SPEC_PITCH = SPEC_BAR_W + SPEC_GAP
SPEC_BAR_RADIUS = 1.5              # 顶部圆角 1.5px（只圆顶：底部贴基线必须平）
SPEC_BAR_MAX_N = 768               # 根数上限：**仅极端超宽屏的兜底**（常规窗口/全屏不触发）
                                   #   Lead 裁决③：柱数 = 控件宽度 / 节距 ⇒ 铺满视口底部；
                                   #   卡片「约 160~200 根」是按 3px/2px 估算的约数，不设硬上限
SPEC_BAR_MIN_N = 16                # 极窄窗口的兜底
SPEC_CANON_MAX_N = 420             # ★ task-30 rider：**规范分辨率**上限（波形的唯一真源只增不减，
                                   #   显示 = 它的线性重采样 ⇒ 20 步连续拖拽不再累积损失）；
                                   #   取 420 是因为噪声平滑核按"显示柱"定义，规范分辨率过高会让
                                   #   显示端噪声变白、相邻柱相关掉出 [0.65,0.90]
SPEC_PEAK_H = 2                    # 落峰点标记高度（task-26 方案 B，默认关）
# ★★ v1.0.36（task-14）：峰值落峰 4px·s⁻¹ **已作废**，改为「滞空 100ms 后 50~80px/s」
#   （取中值 65）；柱高范围同步改为「贴底向上 1~44px」。
SPEC_PEAK_FALL_PX_S = 65.0         # 落峰速度 px·s⁻¹（卡片 50~80 取中值；按真实 dt 折算）
# ★ task-26（用户 ⑬）：频谱开关 + 峰值点样式（task-22 方案 B）
SPEC_PEAK_G = 450.0                # 方案 B「极速落峰点」的重力加速度（px/s²，位移 = ½gt²）
SPEC_PEAK_MAX_V = 900.0            # 落峰速度上限（px/s，防极端帧长导致穿透）
SPEC_PEAK_MARK_H = float(SPEC_PEAK_H)   # 落峰点标记高度（= 旧探针读的 SPEC_PEAK_H，单一真源）
SPEC_PEAK_MARK_A = 216             # 落峰点 alpha（0..255；比柱体更实，便于像素区分）
SPEC_PEAK_HOLD_S = 0.10            # 峰值滞空 100ms（Peak Hold；方案 B 用，默认不上屏）
SPEC_H = 48
# ★ task-14/task-22：回落 τ=43ms ⇒ 衰减到 10% 用时 = τ·ln10 ≈ 99ms（卡片 90~110ms 正中）
SPEC_DECAY_TAU_S = 0.043           # （task-33 起**已作废**：柱体演化改用 EMA，常量保留给旧探针读）
# ★★ task-33（用户 ③⑨）：**时间惯性阻尼** + 「频谱律动速度」三档
#   · ω 缩放：峰中心漂移频率 / 谐振低通 / 噪声低通统一按它缩放（标准档 = 卡片指定 45%）
#   · EMA：h_t(i) = α·Target_t(i) + (1−α)·h_{t−1}(i)，α 按 16ms 基准做帧率归一化
SPEC_RHYTHM_DEFAULT = "standard"
SPEC_RHYTHM_LEVELS = {              # name: (EMA α, ω 缩放)
    "calm": (0.20, 0.30),          # 舒缓
    "standard": (0.24, 0.45),      # 标准（卡片：ω 降至原先 45%，α∈[0.20,0.28] 取中值）
    "aggressive": (0.28, 0.62),    # 激进
}
SPEC_ENERGY_EVERY = 3              # 每 N 帧读一次 mpv 能量（属性读要进 mpv 锁，必须限速）
# ---- task-28：柱高范围（**贴底向上**，不再居中对称）----------------------------------
SPEC_BAR_TOTAL_MIN = 1.0           # 贴底最短可见柱高（px；静音时贴成一条细线）
SPEC_BAR_TOTAL_MAX = 44.0          # 最高柱高（px；控件 48 ⇒ 顶部留 3px、底部 1px 基准线）
# ---- task-28：多峰连续波浪（Rolling Multi-Peak Wave）---------------------------------
#   Envelope(x,t) = Σ_{k=1..K} A_k(t)·exp(−(x−c_k(t))²/(2σ_k²)) + 宽基底
#   · K = 5（卡片 4~6；判据 3~6 个波峰）
#   · σ 以**柱**为单位取 3.0~4.0 柱：FWHM = 2.355σ ≈ 7~9.4 柱、±2σ 覆盖 12~16 柱
#     ⚠️ 卡片另写「σ_k ∈ [0.04,0.10]」是归一化轴口径；在同卡片的 n≈160~200 根柱下，
#        该区间等价于覆盖 26~80 根柱，与「每峰覆盖 8~16 根柱」自相矛盾。本轮以**可像素实测**的
#        「每峰覆盖 8~16 根柱」为准，probe 同时报 σ_norm = σ_bars / n 便于对账。
SPEC_WAVE_PEAKS = 5                # 动态游走波峰个数
SPEC_WAVE_SIGMA_MIN = 3.0          # σ 下限（柱）
SPEC_WAVE_SIGMA_MAX = 3.8          # σ 上限（柱；3.0~3.8 ⇒ FWHM ≈ 7~9 柱、±2σ 覆盖 12~15 柱）
SPEC_WAVE_DRIFT = 0.045            # 峰中心游走幅度（归一化轴）⇒ 随旋律微幅流动
SPEC_WAVE_DRIFT_HZ = 0.055         # 峰中心游走基频（Hz；每峰倍率不同 ⇒ 不相干）
SPEC_WAVE_RES_TAU_S = 0.16         # 每峰「独立频段谐振」包络时间常数（一阶低通）
SPEC_WAVE_BASE = 0.10              # 宽基底：峰间不归零 ⇒ 连绵起伏（不是 5 座孤峰）
SPEC_WAVE_NORM = 0.78              # 包络归一（峰值 ≈0.95，避免整排被 clamp 削平）
SPEC_WAVE_TEX = 0.12               # ±12% 细腻纹理（卡片指定）
# ★★ task-33：EMA 阻尼把相邻柱相关从 0.8127 推到 0.9443（超出卡片 0.65~0.90 带）——
#   两个噪声项**都经过 5 抽头空间平滑**，所以 INDEP 调幅几乎动不了相关性；
#   这里再混入**未平滑的原始逐柱状态**（λ = SPEC_WAVE_RAW）把相关性拉回带内。
#   标定（确定性量测：停主帧钟 + 固定种子 + 固定 tick 数，n=150 帧 / 240 柱）：
#     λ=0     r=0.9464（**超出**卡片带）｜λ=0.45 r=0.8731｜**λ=0.70 r=0.8086（= 卡前 0.8127）**｜λ=0.85 r=0.7373
SPEC_WAVE_RAW = 0.70               # 0 = 纯平滑项；0.70 = 与卡前空间相关性等值（已标定）
SPEC_WAVE_INDEP = 1.02             # 每柱独立分量（乘 (−1..1) 平滑高斯噪声）——真机标定（n=200,
                                   #   dpr1.5）：相邻相关 0.865（逐对均值，90% 帧落在 [0.65,0.90]）、
                                   #   波峰 4.95 个、交替率 0.43（条形码是 0.98）
SPEC_WAVE_SMOOTH = (0.054, 0.244, 0.402, 0.244, 0.054)   # 高斯 5 抽头（σ≈1 柱）沿柱轴平滑
SPEC_WAVE_AMP_MIN = 0.82           # 每峰振幅下限（带宽谐振调制：A_k ∈ [0.82, 1.00]；
SPEC_WAVE_AMP_MAX = 1.00           #   幅度过散会让弱势峰在平滑后失去局部极大 ⇒ 峰数掉到 2）
SPEC_WAVE_CENTER_JITTER = 0.03     # 初始峰中心抖动（归一化轴）
SPEC_ENERGY_GAMMA = 0.75           # 能量压缩指数（两端严格单调：0⇒贴底、1⇒44px）
SPEC_EXC_TAU_S = 0.040             # 纹理/独立噪声的一阶低通时间常数（40ms）
# ★ task-28：柱体配色（**纯色扁平**）—— 数值就是卡片给的 rgba
SPEC_BAR_RGBA_DARK = (255, 255, 255, 71)    # 深色 rgba(255,255,255,0.28)
SPEC_BAR_RGBA_LIGHT = (25, 30, 40, 66)      # 浅色 rgba(25,30,40,0.26)
# ★ task-28：底部 1px 极细基准线（卡片 rgba(255,255,255,0.12)）
SPEC_BASE_RGBA_DARK = (255, 255, 255, 31)   # 深色 rgba(255,255,255,0.12)
SPEC_BASE_RGBA_LIGHT = (25, 30, 40, 31)     # 浅色同构（0.12）
# ★★ task-54①（用户 11）：**废除保底自嗨振幅** —— RMS 必须**绑定真实动态范围**：
#   实测口径：−40dB ⇒ 0.176（波浪压到 10%~20% ✓）；−12dB ⇒ 1.0（冲高 85%~100% ✓）；
#   两档比值 ≈5.7× ⇒ 稳过卡片 ≥3×。读不到 RMS 时用 `SPEC_IDLE_LEVEL`（低幅中性值，
#   **绝不放大**）+ `src_kind` 如实标注。
# ★★ task-90（Lead 裁决 2026-10-21）：上限 **−12 → −6** —— 摇滚/电音母带 RMS 常为 −8~−3dB，
#   上限卡 −12 时这类曲目**整段饱和到 1.0**、物理上没有突增余量 ⇒ 再好的自适应也看不出鼓点。
#   "先给足余量，再谈自适应压缩"；轻音乐若偏矮用自适应补偿，不回调上限。
SPEC_LEVEL_DB = (-46.0, -6.0)      # astats RMS_level(dB) → 0..1 的线性映射区间
SPEC_IDLE_LEVEL = 0.12             # ★ 无真实数据时的低幅中性值（0.12 ⇒ "近乎贴底"）
SPEC_FLUX_FULL = 6.0               # ★ task-54②：ΔE 归一化满量程（norm/s）
SPEC_FLUX_TAU = 0.45               # 瞬态记忆衰减时间常数（s）
SPEC_RATE_GAIN = 8.0               # ★ 快档柱体跟随速率放大（≈9× 慢档 ⇒ 帧间 Δ 比值 ≥3×）
SPEC_SPEED_GAIN = 4.5              # ★ 快档波峰游走速度放大（≈5.5× 慢档）
SPEC_PEAK_GAIN = 0.45              # （task-61 起**停用**：极端乘数会破坏相邻柱平滑 ⇒ 保留常量仅作历史记录）
# ★★ task-61（用户 4）：**频谱回正** —— 用户真机"人声突入瞬间变锯齿梳子+疯狂乱闪"。
#   三条硬口径：① 不再用 flux 放大单柱噪声/推进速度；② 阻尼恒定温润（上升 0.25 / 回落 0.12）
#   且**每帧步幅 ≤0.08 满量程**；③ 波峰高度由 **120ms 滑动平均 RMS** 驱动（柔化包络）。
SPEC_DAMP_ATK = 0.25               # 上升帧阻尼（帧率归一化后使用）
SPEC_DAMP_REL = 0.14               # ★ task-75：回落阻尼 0.12 → 0.14（鼓点后平滑回落）
# ★★ task-75②（用户 13）：**鼓点弹性** —— 短时瞬态包络（Fast Attack）
#   当前帧能量 vs **前 4 帧均值**正向突增且 ΔE 过阈 ⇒ 波峰瞬时抬升 + 上升阻尼提到 0.45。
#   ⚠️ 两条硬约束不能破：① 相邻柱相关 r ≥0.78（**绝不变锯齿**）；② 舒缓段 Δh p95 ≤0.08。
SPEC_BEAT_HIST = 4                 # 前 N 帧均值（短时基线）
SPEC_BEAT_DE_ABS = 0.055           # ΔE 绝对阈值（线性能量 0..1）
SPEC_BEAT_DE_REL = 0.18            # ΔE 相对阈值（相对前 N 帧均值）
SPEC_BEAT_BOOST = 1.35             # 鼓点瞬时放大系数（卡片 1.35×）
SPEC_BEAT_ATK = 0.45               # 鼓点帧上升阻尼（卡片 α_atk=0.45）
SPEC_BEAT_DECAY = 0.55             # 冲击量每帧衰减（1.35 → 1.19 → 1.10 …）
SPEC_BEAT_FRAMES = 2               # 冲击持续帧数（卡片：≤2 帧到达）
SPEC_BEAT_DH_MAX = 0.35            # 鼓点帧允许的单帧步幅（相对满量程）；平时仍是 0.08
SPEC_BEAT_REFRACT = 4              # 鼓点不应期（帧）：一次冲击后 4 帧内不再重复触发（防连击抖动）
# ★★ task-90（用户 9）：**高响度动态压缩** —— 摇滚/EDM 稳态别顶满，给鼓点腾出净空。
#   · 1.5s 滑动平均能量基线 Ē_slow；Ē_slow > 0.55 时启动重映射，把**持续能量**压回 0.35~0.45；
#   · 鼓点用 **相对突增 z = (E_t − Ē_slow)/std(E)** 判定（不用绝对阈值）；
#   · 目标：稳态 ~40% 中水位、鼓点冲到 ≥90% 峰顶。
SPEC_SLOW_MS = 1500               # 动态基线窗口
#   ★ task-90 修订：0.55 → **0.72** —— 映射上限放宽到 −6dB 后，−20dB 常规流行会算成 0.65，
#     若沿用 0.55 会被误压成 40% 中水位（观感"塌"✗）。0.72 只压真正的摇滚/电音（−8dB≈0.95）。
SPEC_SLOW_TRIG = 0.72             # 触发压缩的门槛（Ē_slow 高于它才压缩）
SPEC_COMP_MID = 0.40              # 压缩后持续能量落到中水位（卡片 0.35~0.45 的中点）
SPEC_COMP_GAIN = 2.70             # 瞬态部分放大（保证鼓点能从 40% 中水位冲到 ≥90% 峰顶）
SPEC_BEAT_Z = 1.55                # 相对突增阈值（单位：标准差）
SPEC_STD_FLOOR = 0.015            # std 下限，防静音段除零
SPEC_BEAT_PUNCH = 0.95            # ★ 鼓点 punch 目标电平（相对突增命中时**直接冲到峰顶**）
SPEC_QUIET_LO = 0.30              # ★ 轻音自适应抬升：Ē_slow 低于它就补
SPEC_QUIET_LIFT = 0.32            # ★ 最大抬升量（−46dB 极轻素材也保证峰值 ≥25%）
SPEC_DH_MAX = 0.08                 # 每帧最大变化步幅（相对满量程）
SPEC_RMS_SMA_MS = 120              # RMS 滑动平均窗口
# ⚠️ 缩放要**双向守带**：太小 ⇒ r 顶到 0.94 ✗（超出 [0.75,0.92] 上限、波浪变得死板）；
#    太大 ⇒ 锯齿率回到 0.60 ✗。实测 0.78 / 0.85 ⇒ r≈0.85、锯齿率≈0.50（both in band ✓）。
SPEC_INDEP_SCALE = 0.78            # 每柱独立噪声缩放
SPEC_RAW_SCALE = 0.85              # 未平滑噪声混入缩放
SPEC_DECAY_PER_S = 2.2             # ⚠️ task-08 起**仅用于切歌/停止后的清空**；暂停不再衰减
SPEC_IDLE_REPAINT_S = 0.15         # 暂停态"只有峰值在下落"时的最高重绘间隔（≈6.7FPS）
                                   # 峰值只走 50~80px·s⁻¹ 且在暂停态本就定格，全速重绘纯属空转；
                                   # 限速后暂停 CPU 从 21.8% 降到 ~5%（QA 判据 ≤8%）
SPEC_SIDE = 40
CONTROL_BAR_H = 72          # 底部控制栏高度（**回落**用；正常路径量胶囊真实几何，见 _spec_y_for）
SPEC_GAP_TO_BAR = 16        # 频谱底边与「控制栏实体上沿」的间隙（单一真源；同时就是回落公式
                            # h - CONTROL_BAR_H - SPEC_H - SPEC_GAP_TO_BAR 里的那个 16）
LYRIC_GROUP_PAD = 29                 # ⚠️ 仅供旧探针/门禁 getattr 读取（task-23 起不再参与步进）
LYRIC_INNER_PAD = 6                  # ⚠️ 同上（task-27 起行内/行间统一走 LYRIC_LINE_GAP）
# ★★ task-35（用户 ⑫）：纯音乐 / 暂无歌词**占位符**美学重构
#   · 坐标：废除"全屏死板居中" —— 左缘锚定**右栏正常歌词的左缘**（同一支笔位），
#     垂直锚定 `y = top + 0.40 × height`（卡片）
#   · 结构：矢量徽标（α≤0.35） → 主文案 18 Medium → 英文小字副标题
# ★★ task-50②（用户 2）：占位符改**单行中文** 20 Medium；黑胶图标画布 64×64、外圈半径 22
PH_MAIN_PX = 20                      # 主文案 20 Medium（与歌词未唱行同字号档）
PH_SUB_PX = 11                       # 副标题字号（英文行已删除 ⇒ 不再使用，保留常量兼容）
PH_Y_RATIO = 0.40                    # 垂直锚点比例（卡片①）
PH_BADGE_R = 22.0                    # ★ task-50②：外圈半径 22（原 24）
PH_BADGE_CANVAS = 64.0               # ★ task-50②：画布 64×64（四周 8px 内边距 ⇒ 不削边）
PH_BADGE_ALPHA = 0.30                # 徽标透明度（卡片：α ≤ 0.35）
# ★★ task-63（用户 6）：占位图标 = **双八分音符 ♫ + 柔光圆环**（矢量；替换"同心圆靶子"）
# ★★ task-66（用户 9）：左栏歌名折行要**紧凑** —— 行高 = 1.15×字号、行间空白 3~4px
#   （原来用 `fm.lineSpacing()`，实测比字号高得多 ⇒ 两行显得松散脱节 ✗）。
INFO_TITLE_LINE_RATIO = 1.15         # 歌名行高系数（× font pixel size）
# ★★ task-67（用户 2 audio 侧）：占位图标改为**严格左右对称的极简头戴耳机 + 中心律动声波**
#   （替换 task-63 的 ♫ —— 它重心偏右、且与底栏音乐按钮撞脸）。
#   · 构型：外弧头梁（上半圆）+ 左右对称两耳罩（胶囊）+ 中心 3 条声波胶囊；
#   · 画法：**全部圆弧/胶囊** → `QPainterPathStroker` 加粗 → `united()` 合成**单一闭合 Path**
#     ⇒ 无拼接死角；再整条 fill 一次 ⇒ 重叠处 alpha 不会叠加 ✓；
#   · 几何严格以 cx=32 镜像（左右同参数）⇒ 质心天然居中 ✓；墨迹外框 ≤ [11,53] ⇒ 四周 ≥11px ✓。
PH_HP_BAND_CY = 37.0                 # 头梁圆心 y
PH_HP_BAND_R = 19.0                  # 头梁半径
PH_HP_BAND_W = 4.6                   # 头梁线宽（加粗后即成弧带）
PH_HP_CUP_DX = 16.0                  # 耳罩中心距画布中心的水平偏移（左右对称）
PH_HP_CUP_CY = 41.0                  # 耳罩中心 y
PH_HP_CUP_W = 8.4                    # 耳罩宽
PH_HP_CUP_H = 15.4                   # 耳罩高
PH_HP_WAVE_DX = 7.2                  # 声波条水平间距（左右各一条 + 中心一条）
PH_HP_WAVE_W = 3.4                   # 声波条宽
PH_HP_WAVE_H = (8.0, 16.0, 8.0)      # 三条声波高度（中心高、左右对称）
PH_HP_WAVE_CY = 41.0                 # 声波中心 y
PH_RING_R = 27.0                     # （task-67 起不再绘制圆环；保留常量兼容旧锚点）
PH_RING_W = 1.8                      # 圆环线宽（卡片 1.5~2px）
PH_NOTE_HEAD_RX = 5.6                # 音符头半长轴
PH_NOTE_HEAD_RY = 4.6                # 音符头半短轴
PH_NOTE_STEM_W = 2.4                 # 符干线宽
PH_NOTE_BEAM_W = 4.2                 # 顶部横梁（连音符）厚度
PH_NOTE_HEAD_A = (23.5, 42.5)        # 左音符头中心（低）
PH_NOTE_HEAD_B = (42.0, 36.0)        # 右音符头中心（高 ⇒ 双音符有音乐感）
PH_NOTE_STEM_TOP_A = 19.5            # 左符干顶端 y
PH_NOTE_STEM_TOP_B = 13.5            # 右符干顶端 y
PH_GAP_BADGE = 16.0                  # 徽标 → 主文案
PH_GAP_SUB = 8.0                     # 主文案 → 副标题
PH_SUB_TEXT = ""                    # ★ task-50②：删除英文副标题（改单行中文提示，见 _placeholder）
# ★★ task-27（用户 ②③）：**真·统一视觉行间距 + 零截断盒模型**（废除 task-23 的 min+40 固定槽位）
LYRIC_LINE_GAP = 18.0                # 相邻**墨迹块**的视觉垂直空白（同句折行 / 原词↔译文 / 句↔句 全等）
LYRIC_BOX_PAD = 10.0                 # 每块盒高 = 墨迹高 + 它（上下各 5px）：字形栅格化会比
                                     # tightBoundingRect 外溢 ~1~2 device px ⇒ 3px/边只够"逻辑余量"，
                                     # 5px/边才能让**像素实测**上下余量稳过 ≥2 逻辑 px（卡面 +6 是下界）
LYRIC_GAP_MIN = 10.0                 # 设置键 lyric.step_pad 的量程（= LINE_GAP 的可用区间）
LYRIC_GAP_MAX = 48.0
LYRIC_SIZE_CUR = 27.0                # 当前行主词字号基准（乘 scale_ratio）
# ★★ task-32（用户 ②⑧）：浅色档不再有白色底衬 ⇒ 改用**文字微投影**（水平零扩散）
LYRIC_SHADOW_A = 35                  # 投影主层 alpha（卡片：QColor(0,0,0,35)）
LYRIC_SHADOW_DY = 1.0                # 投影偏移 (0, 1)
LYRIC_SHADOW_A2 = 14                 # （备用）下沿补层
LYRIC_SHADOW_DY2 = 2.0
LYRIC_SHADOW_BLUR = 2.0              # ★ task-32①（Lead 裁决）：**真 2px 高斯**（预烘焙 Pixmap）
LYRIC_SHADOW_PAD = 6                 # 预烘焙贴图四周留白（高斯尾巴 + 抗锯齿外溢）
# ★★ task-88（用户 14）：**未唱译文行**的投影单独更淡更柔（小字号 + 双语混排时，
#   与主词同浓度会让译文"发脏"、笔画发黏 ✗）
LYRIC_TRANS_SHADOW_A = 20            # 未唱译文投影 alpha（主词 35 → 译文 20）
LYRIC_TRANS_SHADOW_BLUR = 2.6        # 稍大模糊半径 = 更柔（不做硬边）
# ★★ task-32②：均衡断行 —— 末行**绝不允许** 1~3 字寡行；优先在标点/空格后断
LYRIC_MIN_TAIL = 4                   # 末行最少字符数（禁寡行）
LYRIC_BREAK_AFTER = "，。！？；：、）】》」』…—·,.;:!?)]}\"\u3000 "   # 可在其后断行
LYRIC_NO_START = "，。！？；：、）】》」』…·,.;:!?)]}\""          # 不许出现在行首
LYRIC_NO_END = "（【《「『([{\""                                    # 不许出现在行尾
LYRIC_SIZE_OTHER = 20.0              # 非当前行主词字号基准
# ★★ task-45②：未唱行明度 / 译文行明度（译文按**行 alpha 的比例**缩放 ⇒ 阴影浓度同步缩放）
LYRIC_ALPHA_UNSUNG = 0.575           # [0.55,0.60] 区间内（实测反解用）
# ★★ task-105（用户问题 6）：双语「同源异体」双字体区分 —— 旧口径（不透明度 0.835/0.90 两档、
#   译文字号写死 18/14px、译文与原句**同一字体族**）**全部作废**，改为下面三条单一真源：
#   · 译文字体族 = 人文楷/宋/圆体（与黑体原文形成骨相差异），设置键 lyric.trans_font 可覆盖；
#   · 译文字号 = 原句 **76%**；不透明度 = 原句 **78%**。
LYRIC_TRANS_ALPHA = 0.78             # 译文不透明度 = 原句 78%（单一真源）
LYRIC_TRANS_RATIO = 0.78             # ★ task-115：译文字号 = 原句 **78%**（27×0.78=21 / 20×0.78=16）
# ★★ task-115（用户）：「翻译歌词里居然是楷体，**太细了**，不符合原文字体风格！」
#   ⇒ 译文回退栈**移除 KaiTi / FangSong / SimSun**（楷体笔画细、与原句黑体风格割裂 ✗），
#   改为 **YouYuan（幼圆）→ Microsoft YaHei UI**（同为无衬线黑体族，笔画饱满、与原句同调 ✓）。
# ★★ task-120（用户）：译文**严禁回退 Microsoft YaHei UI** ⇒ **直接用项目内置 HarmonyOS Sans SC**
#   （同一字体族、靠字重/色温区分主次，观感统一；雅黑在含 CJK 混排时字形与 HarmonyOS 不一致 ✗）
LYRIC_TRANS_FAMILY_FALLBACKS = ()          # 空栈 ⇒ _trans_family_default() 直接返回内置族
LYRIC_TRANS_CUR_PX = 19.0                  # ★ 当前高亮译文（Bold 700）
LYRIC_TRANS_OTHER_PX = 15.5                # ★ 未唱译文（DemiBold 600）
LYRIC_BAR_X = 12.0                         # ★ 译文左侧竖条 ▎（x=12）
LYRIC_BAR_W = 3.0                          #   宽 3px 圆角
LYRIC_BAR_H_RATIO = 0.70                   #   高 = 行高 70%
LYRIC_TRANS_X = 22.0                       # ★ 译文从 x=22 起绘（让开竖条）
# ★ 四档色值（深色 / 浅色 × 当前 / 未唱）；主词中性色、译文冷色
LYRIC_MAIN_COL = {"dark": ((255, 255, 255), 1.00), "dark_dim": ((255, 255, 255), 0.70),
                  "light": ((18, 22, 31), 1.00), "light_dim": ((18, 22, 31), 0.68)}
LYRIC_TRANS_COL = {"dark": ((115, 210, 255), 0.96), "dark_dim": ((155, 195, 235), 0.52),
                   "light": ((0, 102, 204), 0.95), "light_dim": ((45, 90, 145), 0.56)}
LYRIC_FONT_ANIM_S = 0.25             # 字号/明度插值周期 250ms（卡面 ③）
LYRIC_SCROLL_S = 0.32                # 滚动插值周期 320ms、OutCubic（卡面 ③）
LYRIC_FEATHER_PX = 56                # 上下真羽化带宽 56px（卡面 ④）
# ★★ task-24（用户 ⑩）：滚轮自由浏览 + 悬停/中心行时间胶囊 + 5 秒自动回位
LYRIC_MANUAL_HOLD_MS = 5000          # 5 秒无操作 ⇒ 自动回位（卡面 ④）
LYRIC_WHEEL_STEP = 0.60              # 滚轮一格（angleDelta 120）⇒ 0.6 个网格步进的位移
LYRIC_TBTN_W = 78.0                  # 时间胶囊宽（固定值 ⇒ 绘制期零测量）
LYRIC_TBTN_H = 26.0                  # 时间胶囊高
LYRIC_TBTN_MARGIN = 6.0              # 胶囊离面板右边缘
# ★★ task-41（用户 ②③④）：时间胶囊交互 + 分段参考线 + 滚轮阻尼
LYRIC_TBTN_LIGHT_BG = (255, 255, 255, 224)   # 浅色胶囊底 rgba(255,255,255,0.88)
LYRIC_TBTN_LIGHT_BORDER = (0, 0, 0, 46)      # 1px rgba(0,0,0,0.18)
LYRIC_TBTN_LIGHT_TEXT = (24, 25, 28)         # #18191C
LYRIC_TBTN_DARK_BG = (28, 32, 38, 224)       # 深色胶囊底 rgba(28,32,38,0.88)
LYRIC_TBTN_DARK_BORDER = (255, 255, 255, 56)  # 1px rgba(255,255,255,0.22)
LYRIC_TBTN_DARK_TEXT = (255, 255, 255)       # #FFFFFF
LYRIC_TBTN_HOVER_BORDER = 2.0                # 悬停：描边加粗（同时提亮）
LYRIC_TBTN_RELEASE_PAD = 8.0                 # 松开命中容差（px）：容忍按下→松开之间的几何微动
LYRIC_GUIDE_GAP_TEXT = 16.0                  # 参考线起点 = 歌词文本右缘 + 16px
LYRIC_GUIDE_GAP_CAPSULE = 12.0               # 参考线终点 = 胶囊左缘 − 12px
LYRIC_WHEEL_LERP = 0.25                      # ★ task-41④：滚轮只改目标，主帧钟按 25%/帧 逼近
# ★★ task-44（用户 ⑨）：句间长间奏 —— 高亮不掉 + • • • 呼吸指示器
# ★★ task-49②（用户 1、2）：圆点**独立成行** —— 直径 8 / 间距 18 / 行高 36 / 阴影安全边距 12
IL_DOT_R = 4.0                               # 半径 4 ⇒ 直径 8px（卡片）
PH_ICON_DX = -6.0                            # ★ task-91：耳机图标左移 6px
PH_ICON_DY = 8.0                             # ★ task-91：耳机图标下移 8px
# ★★ task-91（用户 11）：**圆点与歌词左缘统一同一基线** —— 两者墨迹都从 x=10 起；
#   字形投影（模糊半径 2~6px）落在 x≈4..9，**平滑衰减**、不再被面板左缘硬切 ✗。
LYRIC_LEFT_PAD = 10.0                        # 歌词/圆点左缘（逻辑 px）
IL_DOT_GAP = 18.0                            # 相邻圆点中心距 = 间距 18px（卡片）
# ★★ task-57①（用户 57）：**静态固定占位** —— 圆点行在 `_relayout()` 里一次性排定，
#   播放中途**绝不插拔**；行高 = 正常歌词行高（LAYOUT 的 `_pitch`，缺失时用此兜底）。
IL_DOT_ROW_H = 36.0                          # 圆点行高度兜底（首选 `_pitch`）
IL_PRELUDE_MIN_MS = 5000                     # ★ 前奏圆点行仅在**首句 ≥5s** 时静态排入
IL_DOT_SAFE_PAD = 12.0                       # （task-57② 起不再用于 x —— 左缘必须 Δ=0）
# ★★ task-49③：红绿灯式预备闪烁 —— 蓄力那颗 2.5Hz 正弦在 alpha 0.25~0.85 间振荡
IL_BLINK_HZ = 0.8                            # ★ task-57③（用户 57）：2.5Hz → **0.8Hz**（周期 1.25s）
IL_BLINK_MIN = 0.25
IL_BLINK_MAX = 0.85
IL_DOT_IDLE = 64                             # 未轮到（暗底）≈0.25
IL_DOT_SOLID = 255                           # 已走过/蓄满 = 100% 实心
IL_GAP_TRIGGER_MS = 15000                    # ★ task-57①（用户 57）：句间门槛 6000 → **15000ms**
IL_STATIC_ROW_MS = 15000                     # 静态判定用的同一门槛（别名，语义更清楚）
IL_SUNG_TRIGGER_MS = 3000                    # 触发②：当前句已唱完 >3000ms
IL_DOT_MIN_ALPHA = 90                        # 未点亮圆点的底亮度（★ QA 像素取证：46 太暗，阈值扫描会漏）
IL_DOT_MAX_ALPHA = 235                       # 点亮后的亮度
IL_HALO_R = 8.0                              # ★ task-57②：光晕收到 8（左对齐后圆心=半径4 ⇒ 左侧余量仅 4px，光晕过大必被面板左界切）
LYRIC_PLAY_GLYPH = "\u25b6"          # ▶
LYRIC_FOCUS_ALPHA = 0.92             # 手动浏览时焦点行的明度
# ★★ task-88（用户 9）：歌词专属字体栈（中英一体黑体优先；HarmonyOS Sans SC 随包内置）
LYRIC_FAMILY_FALLBACKS = ("HarmonyOS Sans SC", "MiSans", "Source Han Sans SC",
                          "Microsoft YaHei UI", "Segoe UI")
# 歌词滚动收敛：v1.0.34 的基准是"每帧(33ms)收敛 22%"，改成按真实 dt 折算后
# 每秒收敛率与帧长无关（33ms 与 16ms 的到达时间差 <2%，见 task-4 报告验算）。
LYRIC_EASE_REF_S = 0.033            # 基准帧长（v1.0.34 的 33ms），不是当前 FRAME_MS
LYRIC_EASE_ALPHA = 0.22             # 基准帧长下的每帧收敛比例（原实现写死的 0.22）
CURRENT_LINE_RATIO = 0.40
FADE_PX = 40
BASE_W, BASE_H = 1200.0, 800.0
SCALE_MIN, SCALE_MAX = 0.85, 1.70      # ★ task-46②：宽屏大尺度上限 1.45 → 1.70


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


#: 歌词字体族缓存（**只在布局期解析一次**；paintEvent 里绝不查字体库）
_LYRIC_FAMILY: str = ""


def _trans_family_default() -> str:
    """★ task-115：译文族 —— **幼圆(YouYuan) → 微软雅黑 UI(YaHei UI)**，按本机是否安装取第一个。

    ⚠️ 历史：task-105 这里是「楷 → 仿宋 → 思源宋 → 幼圆」，用户实测骂「翻译歌词是楷体、**太细了**、
    不符合原文字体风格」✗ ⇒ 楷/仿宋/宋**全部移除**；栈里都没有时退回**主词族**（HarmonyOS），
    宁可同族也绝不回衬线 ✗
    """
    try:
        from PySide6.QtGui import QFontDatabase

        fams = set(QFontDatabase.families())
    except Exception:
        fams = set()
    for name in LYRIC_TRANS_FAMILY_FALLBACKS:
        if name in fams:
            return name
    # ★ task-120：默认 = **项目内置 HarmonyOS Sans SC**（与主词同族；靠字重/色温区分）
    return _lyric_family()


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


def _fits_row(s: str, fm, width: float) -> bool:
    try:
        return fm.horizontalAdvance(s) <= max(1.0, float(width))
    except Exception:
        return True


def _split_balanced(s: str, font: QFont, width: float, min_tail: int = LYRIC_MIN_TAIL):
    """★ task-32②：把一段文字切成**均衡的两行**（宽度必须装得下）。

    选点优先级（卡片逐字）：① 标点/空格/破折号**之后**；② 无标点 ⇒ **两行字符数差最小**；
    硬约束：两半都必须装进 width、末行 ≥ min_tail（禁 1~3 字寡行）、不在行首放收尾标点、
    不在行尾放开引号。找不到合法切点 ⇒ 返回 None（调用方保持原折行）。
    """
    try:
        fm = QFontMetrics(font)
    except Exception:
        return None
    n = len(s)
    if n < 2 * min_tail:
        return None
    best = None
    for k in range(min_tail, n - min_tail + 1):
        a, b = s[:k], s[k:]
        if not _fits_row(a, fm, width) or not _fits_row(b, fm, width):
            continue
        if s[k] in LYRIC_NO_START or s[k - 1] in LYRIC_NO_END:
            continue
        tier = 0 if (s[k - 1] in LYRIC_BREAK_AFTER or s[k - 1].isspace()) else 1
        score = (tier, abs(len(a) - len(b)), abs(k - n / 2.0))
        if best is None or score < best[0]:
            best = (score, k)
    if best is None:
        return None
    k = best[1]
    return s[:k], s[k:]


def wrap_rows(text: str, font: QFont, width: float, ref_px: float = LYRIC_SIZE_CUR) -> list[str]:
    """★ task-27② / ★★ task-32②：宽度折行 + **均衡断行** + **断点与字号无关**。

    规则（卡片逐字）：优先在空格/破折号/逗号处断；无标点则使两行字符数差最小；
    **绝对禁止末行 1~3 字寡行**（先把最后两行合并再均衡重切）。

    ★★ task-32② 核心：**以高亮行（ref_px，默认 27px）的断点作为全篇基准断点**。
    实现方式不是"在布局里钉死"，而是**在折行函数内部做尺寸归一化**：
    把 (字号 px, 可用宽 width) 折算到 27px 世界 "(27, width×27/px)" 再断行
    （字形 advance 与字号成正比 ⇒ 折算后**字符数一致**），于是：
      · 同一句在 20px 与 27px 下断点**完全相同**（QA 直接调本函数对比也成立）；
      · 高亮放大时**原地平滑放大**，不发生跨行跳变。
    只在布局期调用（绘制期零测量红线）；返回每行的子串。
    """
    if not text:
        return []
    try:
        px = float(font.pixelSize() or 0.0)
    except Exception:
        px = 0.0
    # ★★ 2026-10-18 修正（QA 口径复核时暴露）：**不缩放宽度**，只把字号换成基准字号。
    #   语义 = "这句歌词的断点由 **高亮字号(27px) 在本面板宽度下** 决定"，与传入字号**无关**
    #   ⇒ 任何字号调用都得到**逐位相同**的断点（20px 行按 27px 的断点渲染，只会更窄 ⇒ 绝不溢出）。
    #   旧写法按 ref/px 缩放宽度：那等于"各字号各自算最合适的断点"，在边界句上会出现 20px≠27px
    #   （实测 1 句不一致 —— 字形 advance 随字号取整 ⇒ 线性近似不成立）。
    if px > 0.5 and abs(px - float(ref_px)) > 0.5:
        mfont = QFont(font)
        mfont.setPixelSize(max(9, int(round(float(ref_px)))))
    else:
        mfont = font
    mwidth = max(40.0, float(width))
    rows = _greedy_rows(text, mfont, mwidth)
    if len(rows) <= 1:
        return rows or [text]
    if len(rows) == 2:
        sp = _split_balanced(text, mfont, mwidth)
        if sp:
            return [sp[0], sp[1]]
        return rows
    if len(rows[-1].strip()) < LYRIC_MIN_TAIL:      # 寡行 ⇒ 把最后两行合并后均衡重切
        sp = _split_balanced(rows[-2] + rows[-1], mfont, mwidth)
        if sp:
            return rows[:-2] + [sp[0], sp[1]]
    return rows


def _greedy_rows(text: str, font: QFont, width: float) -> list[str]:
    """纯宽度贪心折行（QTextLayout 同源引擎）—— 均衡断行的**基准**。"""
    try:
        from PySide6.QtGui import QTextLayout, QTextOption
        tl = QTextLayout(text, font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        tl.setTextOption(opt)
        tl.beginLayout()
        rows: list[str] = []
        guard = 0
        while True:
            line = tl.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(40.0, float(width)))
            st = int(line.textStart())
            ln = int(line.textLength())
            rows.append(text[st:st + ln])
            guard += 1
            if guard > 64:
                break
        tl.endLayout()
        return [r for r in rows if r is not None] or [text]
    except Exception:
        return [text]


def row_ink(font: QFont, text: str) -> tuple[float, float]:
    """单视觉行的**墨迹**上下沿（相对基线：top 通常为负、bottom 为正）。"""
    try:
        fm = QFontMetrics(font)
        r = fm.tightBoundingRect(text if text.strip() else "M")
        top = float(r.top())
        bot = float(r.bottom())
        if bot - top < 1.0:
            top = -float(fm.ascent())
            bot = float(fm.descent())
        return top, bot
    except Exception:
        return -12.0, 4.0


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


def build_text_shadow(text: str, font: QFont, alpha: int = LYRIC_SHADOW_A,
                      blur_px: float = LYRIC_SHADOW_BLUR, dpr: float = 1.0):
    """★ task-32①（Lead 裁决）：把一行文字预烘焙成 **2px 高斯投影** Pixmap。

    返回 (QPixmap, dx, dy)：dx/dy 是相对"文字绘制原点（基线左端）"的**逻辑**贴图偏移，
    调用方 `drawPixmap(QPointF(x+dx, baseline+dy), pm)` 即可让阴影精确压在文字下面。
    实现：QImage 画白字 → 取 alpha → Pillow GaussianBlur(2×dpr) → 上色 黑+α → QPixmap；
    **只在布局期调用**（绘制期零文本测量红线）。任何异常返回 None（调用方跳过贴图）。
    """
    try:
        from PIL import Image, ImageFilter
    except Exception:
        log.debug("Pillow 不可用，跳过歌词投影", exc_info=True)
        return None
    try:
        fm = QFontMetrics(font)
        adv = max(1, int(fm.horizontalAdvance(text)))
        asc, desc = int(fm.ascent()), int(fm.descent())
        pad = int(LYRIC_SHADOW_PAD)
        lw, lh = adv + pad * 2, asc + desc + pad * 2
        scale = max(1.0, float(dpr))
        img = QImage(int(lw * scale), int(lh * scale), QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.scale(scale, scale)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QPointF(pad, pad + asc), text)
        p.end()
        buf = bytes(img.constBits())
        pil = Image.frombytes("RGBA", (img.width(), img.height()), buf, "raw", "BGRA")
        a = pil.getchannel("A").filter(ImageFilter.GaussianBlur(float(blur_px) * scale))
        out = Image.new("RGBA", a.size, (0, 0, 0, 0))
        out.putalpha(a.point(lambda v: int(v * float(alpha) / 255.0)))
        data = out.tobytes("raw", "BGRA")
        qimg = QImage(data, out.width, out.height, QImage.Format_ARGB32).copy()
        pm = QPixmap.fromImage(qimg)
        pm.setDevicePixelRatio(scale)
        return pm, -float(pad), -float(pad + asc)
    except Exception:
        log.debug("歌词投影预烘焙失败", exc_info=True)
        return None


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
# ★★ task-30（用户 ⑥）：左栏版式 —— **放大封面 + 与封面左边缘严格对齐的三行信息块**。
COVER_BASE_PX = 295            # 基准窗口（sr=1）封面**可见边长**目标（卡片 280~310）
COVER_MIN_PX = 280             # 下限（基准窗口）
COVER_W_RATIO = 0.80           # 可见边长 / 左栏可用宽（卡片 78%~82%；left_w 由 target 反推 ⇒ 恒等）
# ★★ task-34①：左栏信息层级（用户 ⑪）—— 字号/字重/间距全部按卡片，**前缀回归**
INFO_TITLE_PX = 24             # 歌名 24 Bold 700（纯白）
# ★★ task-45①（用户 ②）：左栏只保留**2 级视觉** —— 歌名 1 级、其余三行**完全一致**。
INFO_ARTIST_PX = 14            # 「歌手: {artist}」14 Medium 500
INFO_ALBUM_PX = 14             # ★ task-45①：「专辑: {album}」提到 **14 Medium**（原 14 Regular）
INFO_SPEC_PX = 14              # ★ task-45①：规格行提到 **14 Medium**（原 12 Regular）
# ★★ task-34②：响应式居中盒模型（废除固定左边缘坐标）
CONTENT_W_RATIO = 0.84         # ★ task-46②：content_w = min(int(W*0.84), CONTENT_MAX_W)
CONTENT_MAX_W = 1600           # ★ task-46②：宽屏上限（超宽窗口内容盒不再变宽 ⇒ 居中留白）
LEFT_COL_RATIO = 0.38          # 左栏 38%（卡片 ±2%）
RIGHT_COL_RATIO = 0.62         # 右栏 62%
COVER_MAX_PX = 310             # ★ 基准窗口（left_col≤397）下的尺寸上限：280~310（卡片④）
COVER_MAX_HARD_PX = 420        # ★ Lead 裁决（占比优先）：宽窗按 0.80×左栏放大时的绝对上限
COVER_RATIO_BAND = (0.78, 0.82)   # 卡片："占左栏可用宽度 78%~82%" = **比例约束**（全窗宽成立）


def content_w_for(width: int) -> int:
    """★ task-34②：**响应式内容盒宽度**（纯函数 —— QA 可在任意 W 上代入校验，无需真机）。

    公式（取整规则写死，±1px 争议以此为准）：`content_w = max(360, min(int(W × 0.82), 1280))`
    · `int()` 是**截断**（向下取整），作用在 `W × 0.82` 上，然后才与 1280 取 min；
    · 下限 360 只为极窄窗口兜底（正常窗口不会触发）。
    """
    return max(360, min(int(int(width) * CONTENT_W_RATIO), CONTENT_MAX_W))


def start_x_for(width: int) -> int:
    """★ task-34②：内容盒左边缘（居中）—— `start_x = (W − content_w) // 2`（整数除）。"""
    return (int(width) - content_w_for(width)) // 2
INFO_A_TITLE = 242             # 0.95（深色 rgbrgba(255,255,255,0.95)）
INFO_A_TITLE_LIGHT = 235       # ★ task-45①：浅色一级 rgba(20,24,30,0.92)
# ★★ task-45①（用户 ②）：**废除三行透明度阶梯** —— 歌手/专辑/规格三行同色同字重。
#   0.65 × 255 = 165.75 ⇒ 取 166（深色 rgba(255,255,255,0.65) / 浅色 rgba(30,35,45,0.65)）。
INFO_A_LEVEL2 = 166            # 二级三行**统一** alpha（判据：三行取样差值 = 0）
INFO_A_ARTIST = INFO_A_LEVEL2
INFO_A_ALBUM = INFO_A_LEVEL2
INFO_A_SPEC = INFO_A_LEVEL2
INFO_GAP_TITLE_ARTIST = 10     # ★ task-34①：歌名 → 歌手 = 10（卡片）
INFO_GAP_ARTIST_ALBUM = 6      # ★ task-34①：歌手 → 专辑 = 6（卡片）
INFO_GAP_ALBUM_SPEC = 8        # ★ task-34①：专辑 → 规格 = 8（卡片）
INFO_SPEC_KINDS = ("title", "artist", "album", "spec")
# ★ task-34①：抗锯齿外溢补偿。卡片 10/6/8 是"**屏幕上看到的空白**"；而 tightBoundingRect
#   的墨迹盒外还有约 0.5px 的淡像素（AA）⇒ 若按墨迹盒精确摆 10/6/8，像素带量出来是
#   ≈11.4/7.4/8.7（+1.4/+1.4/+0.7）。这里按墨迹盒摆 card−0.5 ⇒ 两边口径同时落在卡片 ±1px：
#   锚点 info_gaps = 9.5/5.5/7.5（偏差 0.5）、像素带 ≈ 10.9/6.9/8.2（偏差 ≤0.9）。
INFO_GAP_AA_COMP = 0.5
INFO_GAP_COVER = 24            # 封面 → 歌名
INFO_TITLE_MAX_LINES = 2       # 歌名最多 2 行，超 2 行才在**第 2 行**省略
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
        # ★★ task-50①（用户 2）：**删除 task-46 的封面装饰环**（α0.0784 的极淡黑圆环）。
        #   用户口径：封面四周只保留**纯净软阴影**（预烘焙那张），不要再叠一圈几何环。
        #   判据 = 封面外围 4~16px 环带内**无环状像素**（径向剖面单调无凸起）。
        #   ⚠️ 这里只删"画"，`_vinyl_ring` 锚点保留但显式标 removed ⇒ QA 的老判据会翻成"无环" ✓
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


def _lin_resample(vals, n: int) -> list[float]:
    """等长/变长**线性插值重采样**（长度 0 ⇒ 全 0）。

    ★ task-29 的前置：柱数随窗口宽度变化时，用它把 `_bars`/`_peak_px`/噪声状态映射到新密度，
    绝不重置为 0 —— 否则进出全屏就会"波形瞬间归平"（用户问题 ⑤）。
    """
    m = len(vals)
    if n <= 0:
        return []
    if m == 0:
        return [0.0] * n
    if m == n:
        return [float(v) for v in vals]
    out: list[float] = []
    for i in range(n):
        x = (i + 0.5) * m / n - 0.5
        i0 = int(math.floor(x))
        t = x - i0
        a = float(vals[max(0, min(m - 1, i0))])
        b = float(vals[max(0, min(m - 1, i0 + 1))])
        out.append(a + (b - a) * t)
    return out


class CascadeOverlay(QWidget):
    """★★ task-55（用户 13）：**宽屏律动音瀑 Rhythm Cascade**（音游模式）。

    · 只在**开启且宿主宽 ≥1320** 时可见（`setVisible` 控制 ⇒ 关闭时**零像素** ✓）；
    · 全部矢量 QPainter 绘制、**零文本测量**（不碰 QFontMetrics ✓）；
    · 不新增 QTimer：由 `AudioView._on_frame`（既有 16ms 帧钟）驱动 ✓；
    · 音符**匀速下落**（所有音符共用同一常量速度 ⇒ 方差 0 ✓）；
    · 命中判定线 ±2 帧内出现**扩散脉冲**、3 帧衰减消散 ✓。
    """
    MIN_W = 1320                      # 宽屏门槛（卡片②）
    LANES = 4                         # 4 条轨道（低/中低/中高/高）
    NOTE_W = 20.0
    NOTE_H = 11.0
    NOTE_R = 5.0
    SPEED_PX_S = 260.0                # ★ 恒定下落速度（px/s）
    SPAWN_FLOOR_S = 0.8               # 兜底生成间隔（保证 ≥8 块/10s ≈ 12.5 块）
    ONSET_TH = 0.30                   # onset 阈值（flux_norm）
    ONSET_REARM = 0.18                # 迟滞复位阈值（防一个鼓点连发）
    PULSE_FRAMES = 3                  # 脉冲 3 帧衰减（判据 d）
    JUDGE_PAD = 12.0                  # 判定线距底部

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)   # 绝不截胡鼠标
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._enabled = False
        self._notes: list = []            # [nid, lane, y, spawn_ms]
        self._pulses: list = []           # [lane, age]
        self._spawn_ts: list = []         # 最近 10s 的生成时刻（判据 c）
        self._armed = True
        self._acc = 0.0
        self._prev_ms = None
        self._nid = 0
        self._hits_total = 0
        self._last_pulse_age = None
        self.setVisible(False)

    # ---- 对外 ----
    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        if not self._enabled:
            self.clear()
        self._sync_visible()

    def clear(self) -> None:
        self._notes = []
        self._pulses = []
        self._spawn_ts = []
        self._acc = 0.0
        self._prev_ms = None
        self.update()

    def _wide(self) -> bool:
        try:
            return int(self.parentWidget().width()) >= int(self.MIN_W)
        except Exception:
            return False

    def _sync_visible(self) -> None:
        want = bool(self._enabled and self._wide())
        if want != self.isVisible():
            self.setVisible(want)

    def judge_y(self) -> float:
        return max(1.0, float(self.height()) - float(self.JUDGE_PAD))

    def lane_x(self, lane: int) -> float:
        """轨道中心 x（等差 ✓）。"""
        n = max(1, int(self.LANES))
        step = float(self.width()) / float(n)
        return step * (float(lane) + 0.5)

    def track_w(self) -> float:
        n = max(1, int(self.LANES))
        return max(2.0, float(self.width()) / float(n) * 0.34)

    def on_frame(self, dt: float, flux: float, ms: int, playing: bool) -> None:
        """既有帧钟每帧调用（不新增定时器）。"""
        if not self._enabled:
            return
        if self._prev_ms is not None and int(ms) + 500 < int(self._prev_ms):
            self.clear()                  # ★ 切歌/大幅回跳 ⇒ 清空（判据 f）
        self._prev_ms = int(ms)
        self._sync_visible()
        if not self.isVisible():
            return
        if not playing:                   # ★ 暂停 = 定格（不推进、不清空）
            self.update()
            return
        d = max(0.0, min(0.25, float(dt)))
        # onset 生成（迟滞）＋ 兜底节奏
        f = max(0.0, min(1.0, float(flux)))
        onset = False
        if self._armed and f >= float(self.ONSET_TH):
            onset = True
            self._armed = False
        elif f <= float(self.ONSET_REARM):
            self._armed = True
        self._acc += d
        if onset or self._acc >= float(self.SPAWN_FLOOR_S):
            self._acc = 0.0
            self._nid += 1
            # ★ 每枚音符带**稳定 id**（判据 c 的"匀速"要能追踪同一枚：方差 ≤10%）
            self._notes.append([int(self._nid), int(self._nid) % int(self.LANES),
                                -float(self.NOTE_H), int(ms)])
            self._spawn_ts.append(int(ms))
        # 匀速下落 + 命中
        jy = self.judge_y()
        alive = []
        for nid, lane, y, t0 in self._notes:
            ny = float(y) + float(self.SPEED_PX_S) * d     # ★ 常量速度 ⇒ 方差 0
            if ny >= jy:
                self._pulses.append([int(lane), 0])
                self._hits_total += 1
            else:
                alive.append([int(nid), int(lane), ny, int(t0)])
        self._notes = alive
        # 脉冲 3 帧衰减
        keep = []
        for lane, age in self._pulses:
            age = int(age) + 1
            if age <= int(self.PULSE_FRAMES):
                keep.append([int(lane), age])
        self._pulses = keep
        if self._spawn_ts:
            cut = int(ms) - 10000
            self._spawn_ts = [t for t in self._spawn_ts if int(t) >= cut]
        self.update()

    def state(self) -> dict:
        try:
            from . import theme as _th
            light = bool(_th.is_light())
        except Exception:
            light = False
        return {"enabled": bool(self._enabled), "wide": self._wide(),
                "min_w": int(self.MIN_W), "visible": bool(self.isVisible()),
                "lanes": int(self.LANES), "track_w": round(self.track_w(), 2),
                "track_xs": [round(self.lane_x(i), 2) for i in range(int(self.LANES))],
                "track_x_step": (round(self.lane_x(1) - self.lane_x(0), 2) if self.LANES > 1 else 0.0),
                "judge_y": round(self.judge_y(), 2),
                "speed_px_s": float(self.SPEED_PX_S),
                "notes": [[int(n[0]), int(n[1]), round(float(n[2]), 2), int(n[3])] for n in self._notes],
                "pulses": [[int(p[0]), int(p[1])] for p in self._pulses],
                "hits_total": int(self._hits_total),
                "note_count_10s": len(self._spawn_ts),
                "pulse_frames": int(self.PULSE_FRAMES), "light": light,
                "w": int(self.width()), "h": int(self.height()),
                "spawn_floor_s": float(self.SPAWN_FLOOR_S), "onset_th": float(self.ONSET_TH)}

    # ---- 绘制（纯矢量、零测量） ----
    def paintEvent(self, _ev):      # noqa: N802
        if not self._enabled or not self._wide():
            return
        try:
            from . import theme as _th
            light = bool(_th.is_light())
        except Exception:
            light = False
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        base = QColor(10, 12, 16) if light else QColor(255, 255, 255)
        w, h = float(self.width()), float(self.height())
        jy = self.judge_y()
        tw = self.track_w()
        # ① 4 条半透明垂直轨道
        for i in range(int(self.LANES)):
            c = QColor(base)
            c.setAlpha(16)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(c))
            p.drawRoundedRect(QRectF(self.lane_x(i) - tw / 2.0, 0.0, tw, jy), 4.0, 4.0)
        # ② 底部发光判定线
        g = QLinearGradient(0.0, jy - 10.0, 0.0, jy + 6.0)
        c0 = QColor(base)
        c0.setAlpha(0)
        c1 = QColor(base)
        c1.setAlpha(70)
        g.setColorAt(0.0, c0)
        g.setColorAt(1.0, c1)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(g))
        p.drawRect(QRectF(0.0, jy - 10.0, w, 16.0))
        c2 = QColor(base)
        c2.setAlpha(160)
        p.setPen(QPen(c2, 1.6))
        p.drawLine(QPointF(0.0, jy), QPointF(w, jy))
        # ③ 音符块（圆角 + 光晕）
        for _nid, lane, y, _t0 in self._notes:
            cx = self.lane_x(int(lane))
            glow = QColor(base)
            glow.setAlpha(38)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(glow))
            p.drawRoundedRect(QRectF(cx - self.NOTE_W * 0.78, float(y) - self.NOTE_H * 0.7,
                                     self.NOTE_W * 1.56, self.NOTE_H * 1.9),
                              self.NOTE_R * 1.6, self.NOTE_R * 1.6)
            core = QColor(base)
            core.setAlpha(210)
            p.setBrush(QBrush(core))
            p.drawRoundedRect(QRectF(cx - self.NOTE_W / 2.0, float(y), self.NOTE_W, self.NOTE_H),
                              self.NOTE_R, self.NOTE_R)
        # ④ 命中脉冲（扩散 + 3 帧衰减）
        for lane, age in self._pulses:
            k = float(age) / float(max(1, self.PULSE_FRAMES))
            rad = 10.0 + 26.0 * k
            c = QColor(base)
            c.setAlpha(int(150.0 * max(0.0, 1.0 - k)))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(c, 2.2))
            p.drawEllipse(QPointF(self.lane_x(int(lane)), jy), rad, rad * 0.42)
        p.end()

class SpectrumWidget(QWidget):
    """底部频谱：**贴底单向生长的多峰连续波浪**（task-28 重做）。

    · 视觉：柱宽 3px / 间距 2px（节距 5）/ 顶部圆角 1.5px / 底部 1px 基准线 /
      深 rgba(255,255,255,0.28) · 浅 rgba(25,30,40,0.26)；
    · 形态：4~6 个动态游走波峰叠加（σ=3~4 柱、±12% 纹理）—— 废除 task-22 的
      "每柱独立噪声 + 奇偶反相"（那是被已作废的「交替率=1.0/负相关」判据逼出来的条形码）；
    · 动态：1 帧冲顶 → τ=43ms 指数回落；峰值滞空 100ms 后落峰（task-14/26 口径保留）；
    · 柱数随宽度自适应（上限 200），变化时**线性插值重采样**，绝不归零（task-29 地基）。

    **没有自己的定时器**（由主帧时钟调用 `tick()`）；暂停保留 2px 底线，绝不整块隐形。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._n = 0                           # **显示**柱数
        self._cn = 0                          # ★ task-30：**规范分辨率**柱数（唯一真源，只增不减）
        self._canon: list[float] = []         # 波形状态（tick 只写它）
        self._canon_pk: list[float] = []      # 峰值状态（同上）
        self._bars: list[float] = []          # 显示视图（= _canon 的重采样；同分辨率时共用）
        self._peak_px: list[float] = []
        self._peak_hold_t: list[float] = []   # 每柱峰值滞空起点（task-14）
        self._phase: list[float] = []
        self._playing = False
        # ★ task-26：频谱开关 + 峰值点样式（默认关 = task-22 方案 A）
        self._enabled = True
        self._peak_style = "none"
        self._peak_v: list[float] = []     # 方案 B：每柱落峰速度（px/s）
        self._energy = 0.0
        self._beat = 0.0
        self._engine = None                # 由 AudioView 每帧注入（可能为 None）
        # ★ task-54：真实动态映射的状态（dB / flux / 放大系数 / 序列缓存 / 注入钩子）
        self._level_db = -120.0
        self._lvl_prev = 0.0
        self._e_use = 0.0
        self._flux = 0.0
        self._flux_norm = 0.0
        self._rate_sc = 1.0
        self._speed_sc = 1.0
        self._peak_sc = 1.0
        self._rms_series: list = []
        self._rms_win: list = []          # ★ task-61③：120ms 滑动平均窗 [(t, e_use)]
        self._dh_series: list = []        # ★ task-61②：逐帧最大步幅（归一化）
        # ★ task-75②：鼓点检测状态
        self._beat_hist: list = []         # 前 N 帧**原始**能量（线性 0..1）
        self._beat_imp = 1.0               # 当前冲击放大系数（1.0 = 无冲击）
        self._beat_left = 0                # 冲击剩余帧数
        self._beat_count = 0               # 累计检出的鼓点数（锚点用）
        self._beat_last_de = 0.0           # 最近一次 ΔE（锚点用）
        # ★ task-90：动态压缩状态
        self._e_slow_win: list = []        # [(t, e_use)] 1.5s 窗
        self._e_slow = 0.0                 # 滑动平均基线 Ē_slow
        self._e_std = 0.0                  # 窗口内标准差
        self._comp_on = False              # 本帧是否在压缩区
        self._beat_last_z = 0.0            # 最近一次相对突增 z（锚点）
        self._beat_count_rel = 0           # 相对突增检出的鼓点数（锚点）
        self._bars_delta_series: list = []   # ★ task-54 判据 b：[t, max|Δ柱|, mean|Δ柱|]
        self._peak_disp_series: list = []    # ★ 判据 b：[t, Σ|Δ峰中心|]（单位时间位移）
        self._prev_canon: list = []
        self._prev_wc: list = []
        self._inj_level_db = None        # 探针受控注入（None ⇒ 零影响）
        self._inj_flux = None
        self._src_kind = "sim"             # "mpv:astats" | "sim"（数据源如实上报）
        self._src_tried = False            # （task-65 起不再作"只试一次"用，保留兼容）
        self._force_reattach = True        # ★ task-65：下次 _poll_level 强制重挂 af 滤镜
        self._last_media_path = None       # ★ task-65：帧钟检测换文件（视频→音乐/切歌）
        self._env_peak = 0.0               # ★ task-65：自适应归一化包络的峰值分母（换曲/换模式要清）
        self._level = 0.0                  # mpv 读到的能量 0..1（读不到时用模拟）
        self._frame_i = 0
        self._last_t = 0.0                 # 上一帧的真实时刻（落峰/归零按 dt 折算）
        self._last_idle_t = 0.0            # 上一次"仅峰值下落"的重绘时刻（空闲限速用）
        # ★★ task-28：多峰波浪状态（归一化轴 x∈[0,1]，与柱数无关 ⇒ 换分辨率不清零）
        self._wave_c0 = [0.0] * SPEC_WAVE_PEAKS      # 峰中心基准位
        self._wave_c = [0.0] * SPEC_WAVE_PEAKS       # 当前峰中心（慢游走）
        self._wave_s = [0.0] * SPEC_WAVE_PEAKS       # 峰宽 σ（柱）
        self._wave_a = [0.0] * SPEC_WAVE_PEAKS       # 峰振幅（独立频段谐振）
        self._wave_ph = [0.0] * SPEC_WAVE_PEAKS      # 游走相位
        self._res = [0.0] * SPEC_WAVE_PEAKS          # 每峰谐振状态（0..1）
        # ★ task-33：律动速度（三档）⇒ EMA α 与 ω 缩放
        self._rhythm = SPEC_RHYTHM_DEFAULT
        self._ema_alpha = SPEC_RHYTHM_LEVELS[SPEC_RHYTHM_DEFAULT][0]
        self._w_scale = SPEC_RHYTHM_LEVELS[SPEC_RHYTHM_DEFAULT][1]
        self._exc: list[float] = []                  # 纹理噪声状态（空间白、时间 40ms 低通）
        self._tex: list[float] = []                  # 独立分量噪声状态
        self._reset_wave()
        self.setMinimumHeight(SPEC_H)
        self._ensure(72)

    def _bar_count_for(self, w: int) -> int:
        """柱数 = 可用宽度 / 节距（**铺满视口底部**，左右不留大片空白）。

        ★ Lead 裁决③（task-28）：判据 = 跨度 ≥ 控件宽度 95% 且柱数 ≥160；
        tick 与 paint 共用这一个入口（_ensure 内部做插值重采样，换分辨率不清零）。
        """
        return max(SPEC_BAR_MIN_N, min(SPEC_BAR_MAX_N,
                                       (max(80, int(w)) + SPEC_GAP) // SPEC_PITCH))

    def _reset_wave(self):
        """初始化 4~6 个波峰：中心均匀铺开 + 抖动、σ∈[σmin,σmax]（柱）、振幅/相位/谐振随机。"""
        for k in range(SPEC_WAVE_PEAKS):
            c0 = (k + 0.5) / SPEC_WAVE_PEAKS + random.uniform(
                -SPEC_WAVE_CENTER_JITTER, SPEC_WAVE_CENTER_JITTER)
            self._wave_c0[k] = min(0.94, max(0.06, c0))
            self._wave_c[k] = self._wave_c0[k]
            self._wave_s[k] = random.uniform(SPEC_WAVE_SIGMA_MIN, SPEC_WAVE_SIGMA_MAX)
            self._wave_ph[k] = random.random() * math.tau
            self._res[k] = random.random()
            self._wave_a[k] = SPEC_WAVE_AMP_MIN + (SPEC_WAVE_AMP_MAX - SPEC_WAVE_AMP_MIN) * self._res[k]

    def _ensure(self, n: int):
        """柱数变化 ⇒ **从规范分辨率重采样**（绝不重置为 0、也不在上次结果上再采样）。

        ★ task-29：旧实现 `[0.0]*n` 会把频谱整片清零（用户问题 ⑤）。
        ★ task-30 rider（QA 压测）：`_bars` 既当状态又当显示，连续拖拽时"在上次重采样结果上
          再采样" ⇒ 误差累积（20 步后逐点 r 掉到 0.888~0.928）。现在把状态固定在一份
          **规范分辨率**数组（只增不减，上限 SPEC_CANON_MAX_N）上，显示端永远从它重采样 ⇒
          连续 resize 无累积损失（r 恒 ≈1.0）；同分辨率时两者共用同一列表，行为与 task-28 逐位一致。
        """
        n = max(SPEC_BAR_MIN_N, int(n))
        if n == self._n and self._canon:
            return
        if not self._canon:
            self._cn = min(SPEC_CANON_MAX_N, max(n, SPEC_BAR_MIN_N))
            self._canon = [0.0] * self._cn
            self._canon_pk = [0.0] * self._cn
            self._peak_hold_t = [0.0] * self._cn
            self._peak_v = [0.0] * self._cn
            self._exc = [random.random() for _ in range(self._cn)]
            self._tex = [random.random() for _ in range(self._cn)]
        elif n > self._cn:
            self._cn = min(SPEC_CANON_MAX_N, n)          # 只增不减（长高是一次性上采样）
            self._canon = _lin_resample(self._canon, self._cn)
            self._canon_pk = _lin_resample(self._canon_pk, self._cn)
            self._peak_hold_t = _lin_resample(self._peak_hold_t, self._cn)
            self._peak_v = _lin_resample(self._peak_v, self._cn)
            self._exc = _lin_resample(self._exc, self._cn)
            self._tex = _lin_resample(self._tex, self._cn)
        self._n = n
        self._sync_display()

    def _sync_display(self) -> None:
        """显示视图 = 规范数组的线性重采样（同分辨率直接共用 ⇒ 与 task-28 逐位一致）。"""
        if self._n == self._cn:
            self._bars = self._canon
            self._peak_px = self._canon_pk
        else:
            self._bars = _lin_resample(self._canon, self._n)
            self._peak_px = _lin_resample(self._canon_pk, self._n)

    def set_playing(self, on: bool):
        self._playing = bool(on)

    def set_rhythm(self, name) -> bool:
        """★ task-33②：「频谱律动速度」三档（calm/standard/aggressive）—— 实时生效。

        映射表 `SPEC_RHYTHM_LEVELS`：α（EMA 低通）与 ω（演化角速度缩放）。
        未知值回落到标准档并返回 False（调用方据此记录/回退）。
        """
        k = str(name or "").strip().lower()
        hit = SPEC_RHYTHM_LEVELS.get(k)
        ok = hit is not None
        if hit is None:
            k = SPEC_RHYTHM_DEFAULT
            hit = SPEC_RHYTHM_LEVELS[k]
        self._rhythm, self._ema_alpha, self._w_scale = k, float(hit[0]), float(hit[1])
        return bool(ok)

    def set_enabled(self, on: bool) -> None:
        """★ task-26①：频谱开关（关 ⇒ 整块隐藏；柱体状态保留，恢复后继续）。"""
        self._enabled = bool(on)
        self.setVisible(self._enabled)
        self.update()

    def clear_inject(self) -> dict:
        """★ task-55 rider（Lead）：**显式清除注入**的别名（语义与 `set_inject()` 完全一致）。

        存在的理由：QA 用无参 `set_inject()` 时以为没生效 —— 其实清了，只是 `src_kind`
        要到下一次 `_poll_level`（≈0.5s）才回 `mpv:astats` ⇒ 给一个**名字即语义**的入口，
        判定请直接看 `spectrum_state()["inject_level_db"] is None`（立即生效）。
        """
        self._inj_level_db = None
        self._inj_flux = None
        self._flux_norm_inj = None
        return {"inject_level_db": None, "inject_flux_norm": None,
                "note": "src_kind 会在下一次 _poll_level(≈0.5s) 回到 mpv:astats"}

    def set_inject(self, level_db=None, flux=None) -> dict:
        """★ task-54：**受控注入**（QA/自检定值驱动用；默认参数为 None ⇒ 清除注入）。

        ⚠️ 必须用**方法**注入，不能写属性：`zhjctl eval` 只吃**表达式**，
        `spec._inj_level_db = -40.0` 这种赋值语句会被静默丢弃（我为此吃了一次假 FAIL ✗）。
        `flux` 语义 = **归一化值**（0~1，= flux_norm）。
        """
        self._inj_level_db = None if level_db is None else float(level_db)
        if flux is None:
            self._inj_flux = None
            self._flux_norm_inj = None
        else:
            self._inj_flux = max(0.0, min(1.0, float(flux)))
            self._flux_norm_inj = self._inj_flux
        return {"inject_level_db": self._inj_level_db, "inject_flux_norm": self._inj_flux}

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
                # ★ task-65 锚点：能量管线状态（换文件后重挂是否成功、包络分母是否已清）
                "force_reattach": bool(getattr(self, "_force_reattach", False)),
                "env_peak": round(float(getattr(self, "_env_peak", 0.0)), 4),
                "level_db_live": round(float(getattr(self, "_level_db", -120.0)), 2),
                "playing_state": bool(self._playing),
                # ★★ task-28 锚点：视觉规格 + 波峰状态（QA/门禁直接读，不必摸私有常量）
                "bar_w": SPEC_BAR_W, "gap": SPEC_GAP, "pitch": SPEC_PITCH,
                "radius": SPEC_BAR_RADIUS, "bar_h_max": SPEC_BAR_TOTAL_MAX,
                "peaks": SPEC_WAVE_PEAKS,
                # ★★ task-54 锚点：真实动态映射（QA 判据 a/b/c 直接读，不必摸私有量）
                "level_db": round(float(getattr(self, "_level_db", -120.0)), 2),
                "level_norm": round(float(self._level), 4) if float(self._level) >= 0.0 else None,
                "e_use": round(float(getattr(self, "_e_use", 0.0)), 4),
                "flux_raw": round(float(getattr(self, "_flux", 0.0)), 4),   # ★ Lead：raw 与 norm 并列
                "flux": round(float(getattr(self, "_flux", 0.0)), 4),
                "flux_norm": round(float(getattr(self, "_flux_norm", 0.0)), 4),
                "flux_norm_inj": getattr(self, "_flux_norm_inj", None),
                "rate_sc": round(float(getattr(self, "_rate_sc", 1.0)), 3),
                "speed_sc": round(float(getattr(self, "_speed_sc", 1.0)), 3),
                "peak_sc": round(float(getattr(self, "_peak_sc", 1.0)), 3),
                "level_db_range": [SPEC_LEVEL_DB[0], SPEC_LEVEL_DB[1]],
                "idle_level": float(SPEC_IDLE_LEVEL),
                "flux_full": float(SPEC_FLUX_FULL),
                # ★ 受控注入（QA 判据 a/b：直接写这两个字段即可定值驱动；None ⇒ 零影响）
                "inject_level_db": getattr(self, "_inj_level_db", None),
                "inject_flux": getattr(self, "_inj_flux", None),
                # ★ 序列缓存：rms_series = [(t, db, flux_norm, e_use)…]（QA 取逐帧序列）
                "rms_series": list(getattr(self, "_rms_series", []))[-64:],
                # ★ 口径修正（Lead 裁决）：百分比 = 100×柱高/满量程；raw 保留原值对照
                "bar_height_pct": round(100.0 * float(max(bars)) / max(1.0, float(SPEC_BAR_TOTAL_MAX)), 2),
                "bar_height_pct_raw": round(100.0 * float(max(bars)), 2),
                # ★ task-61 锚点：逐帧步幅序列 + 三条回正参数（判据 b 直接读序列）
                "dh_series": list(getattr(self, "_dh_series", []))[-96:],
                "dh_max_cfg": float(SPEC_DH_MAX),
                "damp_atk": float(SPEC_DAMP_ATK), "damp_rel": float(SPEC_DAMP_REL),
                # ★ task-75② 锚点：鼓点检测（QA 判据 c 直接读）
                "beat_count": int(getattr(self, "_beat_count", 0)),
                "beat_imp": round(float(getattr(self, "_beat_imp", 1.0)), 4),
                "beat_left": int(getattr(self, "_beat_left", 0)),
                "beat_last_de": round(float(getattr(self, "_beat_last_de", 0.0)), 4),
                "beat_dh_frames": int(SPEC_BEAT_FRAMES),
                # ★ task-90 锚点：动态压缩状态
                "e_slow": round(float(getattr(self, "_e_slow", 0.0)), 4),
                "e_std": round(float(getattr(self, "_e_std", 0.0)), 4),
                "comp_on": bool(getattr(self, "_comp_on", False)),
                "beat_last_z": round(float(getattr(self, "_beat_last_z", 0.0)), 3),
                "beat_punch": round(float(getattr(self, "_beat_punch", 0.0)), 4),
                "quiet_lift": round(float(getattr(self, "_quiet_lift", 0.0)), 4),
                "beat_count_rel": int(getattr(self, "_beat_count_rel", 0)),
                "slow_ms": int(SPEC_SLOW_MS), "slow_trig": float(SPEC_SLOW_TRIG),
                "comp_mid": float(SPEC_COMP_MID), "comp_gain": float(SPEC_COMP_GAIN),
                "frame_i": int(getattr(self, "_frame_i", 0)),
                "rms_sma_ms": int(SPEC_RMS_SMA_MS),
                "indep_scale": float(SPEC_INDEP_SCALE), "raw_scale": float(SPEC_RAW_SCALE),
                "bar_h_full_px": float(SPEC_BAR_TOTAL_MAX),
                # ★ 判据 b 序列（QA 取 p50/p95 与"单位时间峰位移"）
                "bar_delta_series": list(getattr(self, "_bars_delta_series", []))[-96:],
                "peak_disp_series": list(getattr(self, "_peak_disp_series", []))[-96:],
                "wave_centers": [round(c, 4) for c in self._wave_c],
                "wave_sigma_bars": [round(s, 2) for s in self._wave_s],
                "wave_amp": [round(a, 3) for a in self._wave_a],
                "bars_head": [round(float(v), 2) for v in bars[:10]],
                "bars_min": round(min(bars), 3),
                # ★ task-29 冻结取证：paused 时连续两次读 bars_sig 必须**完全相等**
                "frozen": (not bool(self._playing)),
                "canon_n": int(self._cn),      # ★ task-30 rider：规范分辨率（唯一真源长度）
                # ★★ task-33：律动速度三档锚点（QA 用 apply_music_setting/music_state 驱动）
                "rhythm": str(self._rhythm),
                "ema_alpha": round(float(self._ema_alpha), 4),
                "w_scale": round(float(self._w_scale), 4),
                "ema_tau_ms": round(-(FRAME_MS / 1000.0) / math.log(max(1e-6, 1.0 - self._ema_alpha)) * 1000.0, 1),
                "rhythm_levels": {k: [v[0], v[1]] for k, v in SPEC_RHYTHM_LEVELS.items()},
                "bars_sig": round(sum((i + 1) * float(v) for i, v in enumerate(bars)), 6),
                "span_px": (int(self._n) * SPEC_BAR_W + max(0, int(self._n) - 1) * SPEC_GAP)}

    def attach_engine(self, engine) -> None:
        """注入播放引擎（用于尝试读取**真实音频能量**；为 None 时走模拟包络）。"""
        if engine is not getattr(self, "_engine", None):
            self._force_reattach = True       # ★ task-65：换引擎 ⇒ 下次重挂 af
        self._engine = engine

    def rearm_energy(self, reason: str = "") -> dict:
        """★★ task-65（用户 3）：**换文件/切模式后重置能量管线**（正式入口，表达式可调）。

        mpv 的 `loadfile` 会**冲掉 af 滤镜链**（视频模式尤其明显）⇒ 必须显式重挂；
        同时清掉**自适应归一化包络历史**（上一部视频残留的峰值分母会把音乐的柱高压成死水 ✗）。
        返回 `{src_kind, level, peak_cleared, reason}`（QA 锚点）。"""
        self._force_reattach = True
        self._src_kind = "sim"                # 立即降级 ⇒ 下一次 _poll_level 必然重挂
        self._src_tried = False
        self._level = -1.0
        self._level_db = -120.0
        self._lvl_prev = 0.0
        self._flux = 0.0
        self._flux_norm = 0.0
        self._e_use = 0.0
        self._env_peak = 0.0
        try:
            self._rms_win = []
            self._rms_series = []
        except Exception:
            pass
        try:
            self._poll_level()                 # 立即重挂一次（不等到下一帧）
        except Exception:
            log.debug("rearm_energy 立即重挂失败", exc_info=True)
        return {"src_kind": self._src_kind, "level": round(float(self._level), 4),
                "peak_cleared": True, "reason": str(reason)}

    def reset(self) -> None:
        """清空重置（**只在切歌/停止**时调用；暂停走冻结，绝不衰减 —— task-08）。"""
        if not any(self._canon) and not any(self._canon_pk):
            return
        for i in range(self._cn):
            self._canon[i] = 0.0
            self._canon_pk[i] = 0.0
        if self._bars is not self._canon:       # 显示视图与规范数组不同分辨率时也要归零
            self._peak_px = [0.0] * self._n
            self._bars = [0.0] * self._n
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
        if eng is None:
            return False
        # ★★ task-65（用户 3）：**每次换文件后都要重挂** —— 原来 `_src_tried` 是"只试一次"的
        #   一次性开关 ⇒ 第一次挂上后，任何 `loadfile`（视频→音乐、切歌）都会把 mpv 的 af 链
        #   **冲掉**，而这里因为 `_src_tried=True` 直接 return False ⇒ 永远回落到模拟包络，
        #   于是用户看到"视频切 MP3 后频谱 1~2px 死水"✗。
        #   现在改为**幂等重挂**：每次 `_poll_level` 发现读不到就重挂一次（代价极小，只读属性）。
        if self._src_kind == "mpv:astats" and not self._force_reattach:
            return True                      # 已挂且未失效 ⇒ 直接沿用
        self._force_reattach = False
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
        # ★ task-54：探针**受控注入**（默认 None ⇒ 零影响；QA 判据 a/b 用它做定值驱动）
        if getattr(self, "_inj_level_db", None) is not None:
            _db = float(self._inj_level_db)
            lo, hi = SPEC_LEVEL_DB
            self._level_db = _db
            self._level = max(0.0, min(1.0, (_db - lo) / (hi - lo)))
            self._src_kind = "inject:level"
            return
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
                        self._level_db = db            # ★ task-54：原始 dB（锚点/探针用）
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
        

        数据源如实说明：**没有接真实 FFT/PCM**（不解析 PCM、不做频谱变换）。
        真实量只有 mpv astats 的 RMS 能量（_poll_level；读不到则如实回落模拟，并在
        spectrum_state() 的 src_kind 里标注）；横轴的 4~6 个波峰是**模型量**：
        每个峰有自己的谐振包络（一阶低通 τ=160ms）与慢游走中心，整排再乘真实能量。
        """
        now = time.perf_counter()
        dt = (now - self._last_t) if self._last_t else (FRAME_MS / 1000.0)
        self._last_t = now
        dt = max(0.001, min(0.10, dt))         # 首帧/卡顿夹住，异常 dt 不把落峰速度带飞
        w = max(80, self.width())
        self._ensure(self._bar_count_for(w))
        h = float(max(6, self.height()))
        n = self._cn                          # ★ task-30 rider：演化在**规范分辨率**上做
        if not self._playing:
            # ★★ task-08 + task-29：暂停/停止时**无条件定格** —— 柱高与峰值一个像素都不动、
            #    不做任何衰减/计时；**SPEC_WAVE_INDEP 的每柱独立随机项也不重掷**、波峰振幅谐振
            #    （_res）与中心游走（_wave_c）都不推进 ⇒ 暂停期两次采样逐点**完全相等**
            #    （QA 的"同状态隔 150ms 逐点 r 掉到 0.53"就是这里漏了噪声冻结）。
            #    注意：几何变化仍会在 _ensure() 里做**线性插值重采样**（不清零、不改相位），
            #    这正是 task-29 要的"切换尺寸后形状保持"。
            #    返回 False ⇒ 上层跳过 update()，连重绘都不会发生（CPU 也就降下来了）。
            return False
        self._frame_i += 1
        if self._frame_i % SPEC_ENERGY_EVERY == 1:
            self._poll_level()
        # ★★ task-54①（用户 11）：**能量口径重写** ——
        #   · 有真实 RMS：`e_use = _level`（**线性**，不再叠 e^0.6 / e^γ 的感知放大 ✗ ——
        #     那两条曲线正是"轻柔钢琴也撑得很高"的元凶：−40dB 会被抬到 0.42+ ✗）；
        #   · 无真实 RMS：`e_use = SPEC_IDLE_LEVEL × 微呼吸`（**低幅中性值，绝不放大**）
        #     ⇒ 彻底废除"保底自嗨振幅"。
        sim = max(0.0, min(1.0, self._energy * 0.86
                           + 0.14 * (0.42 + 0.34 * math.sin(now * 1.7)
                                     + 0.24 * random.random())))
        self._energy = sim
        self._beat = 0.72 * self._beat + 0.28 * (sim ** 2.1)
        _dt = max(1e-3, min(0.25, float(dt)))
        if self._level >= 0.0:
            e_use = max(0.0, min(1.0, float(self._level)))          # ★ 真实线性
        else:
            e_use = float(SPEC_IDLE_LEVEL) * (0.72 + 0.28 * sim)    # ★ 低幅中性（不放大）
        # ★★ task-61③：**120ms 滑动平均 RMS 包络** —— 波峰整体高度用它驱动（柔化"开口瞬间"）
        try:
            self._rms_win.append([float(now), float(e_use)])
            _cut = float(now) - float(SPEC_RMS_SMA_MS) / 1000.0
            while self._rms_win and float(self._rms_win[0][0]) < _cut:
                self._rms_win.pop(0)
            if self._rms_win:
                e_use = sum(v for _t, v in self._rms_win) / float(len(self._rms_win))
        except Exception:
            pass
        # ★★ task-90（用户 9）：**动态基线追踪 + 高响度压缩**
        #   1.5s 滑动平均 Ē_slow 与 std(E)；Ē_slow > 0.55（摇滚/EDM 稳态）时，把**持续能量**
        #   压回 0.35~0.45 中水位、瞬态部分按 SPEC_COMP_GAIN 放大 ⇒ 稳态 ~40%、鼓点冲顶 ≥90%。
        #   ⚠️ 压缩只作用于"持续"部分：维持 e_slow 本身不变，所以不会把整体音量做没 ✓
        #   低响度（−30dB）时 Ē_slow < 0.55 ⇒ **不压缩**，动态原样保留 ✓
        try:
            _ew = self._e_slow_win
            _ew.append((float(now), float(e_use)))
            _cut = float(now) - float(SPEC_SLOW_MS) / 1000.0
            while _ew and float(_ew[0][0]) < _cut:
                _ew.pop(0)
            if _ew:
                _vals = [v for _t, v in _ew]
                _mean = sum(_vals) / float(len(_vals))
                if len(_vals) > 2:
                    _var = sum((v - _mean) ** 2 for v in _vals) / float(len(_vals))
                    self._e_std = max(float(SPEC_STD_FLOOR), _var ** 0.5)
                self._e_slow = float(_mean)
            self._e_pre = float(e_use)          # ★ **压缩前**能量（z / raw_e 必须用它，否则 z<0 ✗）
            self._comp_on = bool(self._e_slow > float(SPEC_SLOW_TRIG))
            if self._comp_on:
                _mid = float(SPEC_COMP_MID)
                _delta = float(e_use) - float(self._e_slow)
                # 持续部分落到中水位；瞬态（鼓点）部分放大到能冲顶
                e_use = max(0.0, min(1.0, _mid + _delta * float(SPEC_COMP_GAIN)))
        except Exception:
            log.debug("动态压缩失败", exc_info=True)
        # ★★ task-90（Lead 拍的"轻音乐偏矮用自适应补偿"）：Ē_slow 低 ⇒ 给一点抬升，
        #   否则把上限放宽到 −6dB 后，−46dB 这类极轻素材会几乎全空 ✗（实测 2.27%）。
        try:
            if float(self._e_slow) < float(SPEC_QUIET_LO):
                _lift = float(SPEC_QUIET_LIFT) * (1.0 - float(self._e_slow) / float(SPEC_QUIET_LO))
                e_use = min(1.0, float(e_use) + max(0.0, _lift))
                self._quiet_lift = float(max(0.0, _lift))
            else:
                self._quiet_lift = 0.0
        except Exception:
            log.debug("轻音抬升失败", exc_info=True)
        # ★★ task-75②（用户 13）：**短时瞬态检测（Fast Attack）**
        #   判据：当前帧**原始**线性能量 − 前 4 帧均值 > max(绝对值阈, 相对阈×均值)
        #   ⇒ 记一次鼓点：冲击系数 1.35、上升阻尼 0.45、持续 2 帧（≤2 帧到达 ✓）。
        #   ⚠️ 检测用 raw（未经 SMA），否则 120ms 平滑会把鼓点抹平（这正是"没踩点感"的根因）。
        try:
            _raw_e = max(0.0, min(1.0, float(getattr(self, "_e_pre", e_use))))
            _hist = self._beat_hist
            if len(_hist) >= int(SPEC_BEAT_HIST):
                _prev = sum(_hist[-int(SPEC_BEAT_HIST):]) / float(int(SPEC_BEAT_HIST))
                _de = _raw_e - _prev
                self._beat_last_de = float(_de)
                self._beat_raw_e = float(_raw_e)
                # ★ 不应期：一次冲击后 SPEC_BEAT_REFRACT 帧内不再触发
                #   （否则"能量持续上升"的阶跃会被连判成十几次鼓点 ⇒ 观感抖动 ✗）
                # ★★ task-90②（用户 9）：鼓点改用**相对突增** z = (E_t − Ē_slow)/std(E)
                #   判定，不用绝对阈值（绝对阈值在响度差异大的素材上会"要么全程触发、
                #   要么全程不触发"✗）。z 同时用于冲击强度归一。
                _z = (float(_raw_e) - float(self._e_slow)) / max(float(SPEC_STD_FLOOR),
                                                                float(self._e_std))
                self._beat_last_z = float(_z)
                _refrac = int(getattr(self, "_beat_refract_left", 0))
                if _refrac > 0:
                    self._beat_refract_left = _refrac - 1
                elif _z > float(SPEC_BEAT_Z):
                    self._beat_count_rel += 1
                    self._beat_imp = float(SPEC_BEAT_BOOST)
                    self._beat_left = int(SPEC_BEAT_FRAMES)
                    # ★★ task-90（Lead 口径 a"鼓点峰值 ≥90%"）：**punch 覆盖** ——
                    #   母带 −8dB 的"稳态↔鼓点"电平差只有 ~5%（映射后），线性放大到
                    #   90% 需要 4× 增益 ⇒ 会把噪声一起放大、且必然抖动 ✗。
                    #   改为：**相对突增命中**时才把显示能量抬到 SPEC_BEAT_PUNCH（0.95），
                    #   由 z 决定抬多少（z 越大越接近峰顶）⇒ 鼓点干脆冲顶、稳态纹丝不动 ✓
                    try:
                        _punch = min(1.0, max(0.0, (float(_z) - float(SPEC_BEAT_Z)) / 1.6))
                        self._beat_punch = float(SPEC_BEAT_PUNCH) * (0.55 + 0.45 * _punch)
                    except Exception:
                        self._beat_punch = float(SPEC_BEAT_PUNCH)
                    self._beat_refract_left = int(SPEC_BEAT_REFRACT)
                    self._beat_count += 1
            _hist.append(_raw_e)
            if len(_hist) > 16:
                del _hist[:8]
        except Exception:
            log.debug("鼓点检测失败", exc_info=True)
        # ★★ task-65（用户 3）：**自适应归一化**（软峰值跟随）——
        #   换曲/换模式时 `_env_peak` 被清 0 ⇒ 立即重新学习，不会沿用上一部视频的峰值分母 ✗。
        try:
            self._env_peak = max(float(e_use), float(self._env_peak) * 0.999)
            if float(self._env_peak) > 1e-6:
                _norm = float(e_use) / float(self._env_peak)
                e_use = max(0.0, min(1.0, 0.35 * float(e_use) + 0.65 * _norm))
        except Exception:
            pass
        self._e_use = e_use
        # ★★ task-54②：**Spectral Flux / ΔE**（相邻采样一阶差分，只认上升沿 = 瞬态冲击）
        _inj_f = getattr(self, "_inj_flux", None)
        if _inj_f is not None:
            # ★ task-54②修正（Lead 裁决）：**注入值即 `flux_norm`（0~1）**，不再当原始 flux；
            #   否则注入 1.0 只等于 norm 0.167 ⇒ study 的"快档"被压缩（speed_sc 只有 1.75 ✗）。
            self._flux_norm_inj = max(0.0, min(1.0, float(_inj_f)))
            self._flux = float(self._flux_norm_inj) * float(SPEC_FLUX_FULL)
        else:
            _dv = (e_use - float(getattr(self, "_lvl_prev", e_use))) / _dt
            self._flux = (float(getattr(self, "_flux", 0.0)) * math.exp(-_dt / float(SPEC_FLUX_TAU))
                          + max(0.0, _dv))
        self._lvl_prev = e_use
        self._flux = max(0.0, min(float(SPEC_FLUX_FULL) * 1.5, float(self._flux)))
        # ★ 注入时直接用注入的归一化值（绕过 raw/满量程换算）
        if getattr(self, "_flux_norm_inj", None) is not None:
            self._flux_norm = float(self._flux_norm_inj)
        else:
            self._flux_norm = max(0.0, min(1.0, float(self._flux) / float(SPEC_FLUX_FULL)))
        self._rate_sc = 1.0 + float(SPEC_RATE_GAIN) * self._flux_norm      # 柱体跟随速率放大
        self._speed_sc = 1.0 + float(SPEC_SPEED_GAIN) * self._flux_norm    # 波峰游走速度放大
        self._peak_sc = 1.0 + float(SPEC_PEAK_GAIN) * self._flux_norm      # 峰高跃升
        try:
            self._rms_series.append([round(float(now), 3), round(float(self._level_db), 2),
                                     round(float(self._flux_norm), 4), round(float(e_use), 4)])
            if len(self._rms_series) > 160:
                del self._rms_series[:80]
            # ★★ task-54 判据 b：帧间「柱体起伏」与「峰中心位移」两个量的逐帧序列
            _pc = self._prev_canon
            _pw = self._prev_wc
            if _pc and len(_pc) == len(self._canon):
                _dmax = max(abs(a - b) for a, b in zip(self._canon, _pc))
                _dmean = sum(abs(a - b) for a, b in zip(self._canon, _pc)) / max(1, len(_pc))
            else:
                _dmax = _dmean = 0.0
            _disp = (sum(abs(a - b) for a, b in zip(self._wave_c, _pw)) if _pw else 0.0)
            self._bars_delta_series.append([round(float(now), 3), round(float(_dmax), 4),
                                            round(float(_dmean), 4)])
            self._peak_disp_series.append([round(float(now), 3), round(float(_disp), 5)])
            if len(self._bars_delta_series) > 200:
                del self._bars_delta_series[:100]
                del self._peak_disp_series[:100]
            self._prev_canon = list(self._canon)
            self._prev_wc = list(self._wave_c)
        except Exception:
            pass
        # ★★ task-28（用户 ④⑨）：**多峰连续波浪**（Rolling Multi-Peak Wave）
        #   —— 废除 task-22 的"每柱独立噪声 + 奇偶反相"（那套是为了满足已被作废的
        #      「交替率=1.0 / 负相关」判据，实测成了"一高一低"梳子条形码）。
        #   模型：Envelope(x,t) = Σ_{k=1..K} A_k(t)·exp(−(x−c_k)²/(2σ_k²)) + 宽基底
        #     · x∈[0,1] 归一化轴（与柱数无关 ⇒ 换分辨率不清零，task-29 地基）
        #     · c_k(t)：慢游走（每峰不同速率/相位 ⇒ 不相干流动）
        #     · A_k(t)：**独立频段谐振**（一阶低通、每峰独立）—— 这才是"多峰各自起伏"；
        #                整排再乘真实能量 e_use（含 mpv astats 实测值；拿不到就模拟，如实标注）
        #     · 叠 ±12% 细腻纹理 + 每柱独立分量 ⇒ 相邻柱相关落到 [0.65,0.90]（连绵非跳变）
        #     · 高度 = MIN + (MAX−MIN)·v，**贴底向上单向生长**（v=0 ⇒ 贴成一条细线）
        bars, peaks = self._canon, self._canon_pk     # 状态真源（显示端是它的重采样视图）
        changed = False
        # ★★ task-33①：**时间惯性阻尼** —— 全部演化速率按 ω 缩放（标准档 = 卡片 45%）
        w_sc = max(0.05, float(getattr(self, "_w_scale", 1.0)))
        # 噪声低通 τ 同样按 ω 拉长（τ/0.45）：它把 d2 能量从 12.3 压到 3.5；
        # 它带来的"相邻柱相关升高"已由 SPEC_WAVE_RAW(0.70) 抵消回带内（见上标定表）。
        # ★★ task-54②：τ 按 flux 缩短（快歌 ⇒ 跟得快；慢歌 ⇒ 静水微澜；判据 b 的"帧间 Δ"就是它）
        # ★★ task-61①：**删除 flux 对低通/推进速率的放大**（`_rate_sc` 置 1 ⇒ 恒定温润）
        _rsc = 1.0
        a_lp = min(1.0, dt * _rsc / (SPEC_EXC_TAU_S / w_sc))       # 噪声一阶低通（ω 缩放）
        a_res = min(1.0, dt * _rsc / (SPEC_WAVE_RES_TAU_S / w_sc))  # 谐振包络低通
        # EMA 系数（α 以 16ms 为基准；按真实 dt 做**帧率归一化** ⇒ 30/60/120fps 观感一致）
        a_ema = 1.0 - (1.0 - float(getattr(self, "_ema_alpha", 0.24))) ** max(0.01, dt / (FRAME_MS / 1000.0))
        # ★ task-54②：最终跟随器（`new = cur + a_ema·(tgt−cur)`）**也必须吃 flux** ——
        #   否则帧间 Δ 的上界被固定 α 锁死（自检实测：只改 a_lp/a_res 时 Δp50 有 3.4× 但
        #   **p95 只有 2.26×** ✗）；这里按 rate_sc 放大并夹到 1.0（慢歌仍是原速 ✓）。
        a_ema = min(1.0, a_ema)      # ★ task-61①：不再乘 rate_sc（原来 flux 大 ⇒ 阻尼被拉到 1.0 ⇒ 乱闪）
        k_n = SPEC_WAVE_PEAKS
        for k in range(k_n):
            # ★ task-54②：快档波峰游走速度放大（判据 b 的"峰中心位移/单位时间"）
            # ★ task-61①：**不再乘 flux**（原来快档推进速度 ×5.5 ⇒ 波峰乱窜）
            f = SPEC_WAVE_DRIFT_HZ * w_sc * (1.0 + 0.37 * k)
            self._wave_c[k] = min(0.97, max(0.03, self._wave_c0[k]
                                            + SPEC_WAVE_DRIFT * math.sin(now * math.tau * f
                                                                         + self._wave_ph[k])))
            r_k = self._res[k] + (random.random() - self._res[k]) * a_res
            self._res[k] = r_k
            # ★ task-54②：快档峰高跃升（密集鼓点 ⇒ 波峰跃起）
            self._wave_a[k] = (SPEC_WAVE_AMP_MIN + (SPEC_WAVE_AMP_MAX - SPEC_WAVE_AMP_MIN) * r_k)
            # ★ task-61①：不再乘 flux 峰高增益（原来 +45% ⇒ 梳子感）
        # σ 以**柱**为单位存（可读/可对账），进包络前换算到归一化轴：σ_norm = σ_bars / n
        #   ⚠️ 下限 0.5 必须落在**柱**单位上（写成 max(0.5, σ/n) 会把每个峰都撑成 80 根柱宽）
        inv2s2 = [1.0 / (2.0 * (max(0.5, self._wave_s[k]) / max(1, n)) ** 2) for k in range(k_n)]
        base_env = SPEC_WAVE_BASE * SPEC_WAVE_NORM
        norm = SPEC_WAVE_NORM
        sm = SPEC_WAVE_SMOOTH
        half = len(sm) // 2
        raw_mix = max(0.0, min(1.0, float(SPEC_WAVE_RAW) * float(SPEC_RAW_SCALE)))   # ★ task-61①：降锯齿
        # ★★ task-61②：帧率归一化的**非对称阻尼**（上升 0.25 / 回落 0.12）
        _fdt = max(0.01, dt / (FRAME_MS / 1000.0))
        _a_rel = min(1.0, 1.0 - (1.0 - float(SPEC_DAMP_REL)) ** _fdt)
        # ★ task-75②：鼓点帧用 α_atk=0.45 且放宽单帧步幅（否则 0.08 上限会让"一跃"变成 5 帧爬升 ✗）
        _beat_on = int(getattr(self, "_beat_left", 0)) > 0
        if _beat_on:
            _a_atk = min(1.0, 1.0 - (1.0 - float(SPEC_BEAT_ATK)) ** _fdt)
            _dh_cap = float(SPEC_BEAT_DH_MAX) * float(SPEC_BAR_TOTAL_MAX - SPEC_BAR_TOTAL_MIN)
            self._beat_left = int(self._beat_left) - 1
        else:
            _a_atk = min(1.0, 1.0 - (1.0 - float(SPEC_DAMP_ATK)) ** _fdt)
            _dh_cap = float(SPEC_DH_MAX) * float(SPEC_BAR_TOTAL_MAX - SPEC_BAR_TOTAL_MIN)
        # 冲击系数：鼓点帧抬到 1.35，之后每帧 ×0.55 衰减回 1.0（不打乱波形 ⇒ r 不降 ✓）
        _imp = float(getattr(self, "_beat_imp", 1.0))
        self._beat_imp = 1.0 + (_imp - 1.0) * float(SPEC_BEAT_DECAY)
        if self._beat_imp < 1.005:
            self._beat_imp = 1.0
        # ★★ task-75②：**瞬态直通** —— 120ms SMA 会把鼓点前沿抹平（实测：只用 SMA 时要 3 帧
        #   才到 1.2× ✗）。冲击期间用 `max(SMA, raw×冲击)` 做显示能量 ⇒ 1~2 帧内"一跃到位" ✓；
        #   冲击一结束立刻回落到 SMA ⇒ 舒缓段依旧柔顺（Δh p95 不受影响 ✓）。
        _e_disp = e_use
        if _imp > 1.0:
            try:
                _raw_now = float(getattr(self, "_beat_raw_e", e_use))
                _e_disp = max(float(e_use), min(1.0, _raw_now * _imp))
            except Exception:
                _e_disp = e_use
        # ★★ task-90：**鼓点 punch** —— 相对突增命中时把显示能量顶到 0.95（随冲击帧衰减），
        #   否则母带 −8dB 的稳态↔鼓点电平差只有 ~5%，线性放大到 90% 会把噪声一起放大 ✗。
        _punch = float(getattr(self, "_beat_punch", 0.0) or 0.0)
        if _punch > 0.0 and _imp > 1.0:
            _e_disp = max(float(_e_disp), min(1.0, _punch))
            self._beat_punch = float(_punch) * float(SPEC_BEAT_DECAY)
        _dh_frame = [0.0]
        for i in range(n):
            e_i = self._exc[i] + (random.random() - self._exc[i]) * a_lp
            t_i = self._tex[i] + (random.random() - self._tex[i]) * a_lp
            self._exc[i] = e_i
            self._tex[i] = t_i
            # 沿柱轴 5 抽头平滑（读邻居状态，不写状态 ⇒ 与遍历顺序无关；防碎峰）
            if n > len(sm):
                e_m = sum(sm[j] * self._exc[(i + j - half) % n] for j in range(len(sm)))
                t_m = sum(sm[j] * self._tex[(i + j - half) % n] for j in range(len(sm)))
            else:
                e_m, t_m = e_i, t_i
            x = (i + 0.5) / n
            env = base_env
            for k in range(k_n):
                d = x - self._wave_c[k]
                env += norm * self._wave_a[k] * math.exp(-d * d * inv2s2[k])
            # ★ task-33：独立项可混入**未平滑**的逐柱状态 e_i（空间白）⇒ 把 EMA 抬高的
            #   相邻柱相关拉回卡片带内（raw_mix=0 时与本卡之前的行为逐位一致）
            e_ind = e_m if raw_mix <= 0.0 else ((1.0 - raw_mix) * e_m + raw_mix * e_i)
            # ★ task-61①：独立噪声/原始噪声混入**统一缩放**（降锯齿率；r 上限 0.92 也要守住）
            v = _e_disp * (env * (1.0 + SPEC_WAVE_TEX * (2.0 * t_m - 1.0))
                         + SPEC_WAVE_INDEP * float(SPEC_INDEP_SCALE) * (2.0 * e_ind - 1.0))
            # ★ task-75②：鼓点瞬时抬升（**整排同乘** ⇒ 波形不变、相邻柱相关不降 ✓）
            if _imp > 1.0:
                v = v * _imp
            v = max(0.0, min(1.0, v))
            tgt = SPEC_BAR_TOTAL_MIN + (SPEC_BAR_TOTAL_MAX - SPEC_BAR_TOTAL_MIN) * v
            cur = bars[i]
            # ★★ task-33①（用户 ③⑨）：一阶低通 EMA 取代 task-14 的"瞬冲 + τ43ms 回落"：
            #   h_t(i) = α·Target_t(i) + (1−α)·h_{t−1}(i)（α∈[0.20,0.28]，已做帧率归一化）
            # ★★ task-61②：非对称阻尼 + **每帧步幅上限**（Δh ≤ 0.08 满量程 ⇒ 绝不"疯狂乱闪"）
            _a = _a_atk if tgt > cur else _a_rel
            new = cur + _a * (tgt - cur)
            if abs(new - cur) > _dh_cap:
                new = cur + (_dh_cap if new > cur else -_dh_cap)
            # ★ task-61 判据 b：**逐帧**最大步幅序列（相对满量程，绘制期零成本）
            if abs(new - cur) > _dh_frame[0]:
                _dh_frame[0] = abs(new - cur)
            if abs(new - cur) > 0.05:
                changed = True
            bars[i] = new
        # ★ task-61：记一帧的最大步幅（归一化），供判据 b 读"逐帧序列"
        try:
            self._dh_series.append(round(float(_dh_frame[0]) / max(1.0, float(SPEC_BAR_TOTAL_MAX - SPEC_BAR_TOTAL_MIN)), 4))
            if len(self._dh_series) > 240:
                del self._dh_series[:120]
        except Exception:
            pass
        if self._n != self._cn:
            self._sync_display()          # 显示端永远从规范数组派生（无累积损失）
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

    def resizeEvent(self, ev):            # noqa: N802
        """★ task-29：几何一变就**当帧**重算柱数并立即重绘 —— 绝不清零。

        根因回顾（用户问题 ⑤）：柱数随几何变化，旧实现在这里（或下一帧 tick/paint）用
        `[0.0]*n` 重建 ⇒ 进出全屏/拖拽窗口瞬间整片归平，暂停态尤其明显（暂停时 tick()
        直接 return False、上层还会跳过 update()，连"长回来"的下一帧都没有）。
        现在：柱数走 _bar_count_for()、长度变化走 _lin_resample() 线性插值 ⇒ 形态保持。
        """
        super().resizeEvent(ev)
        self._ensure(self._bar_count_for(self.width()))
        self.update()

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
        self._ensure(self._bar_count_for(w))   # 铺满视口底部（Lead 裁决③）
        n = self._n
        total = n * SPEC_BAR_W + (n - 1) * SPEC_GAP
        x0 = max(0.0, (w - total) / 2.0)              # 居中：柱数 = 控件宽/节距 ⇒ 铺满底部（≤2px 余量）
        y_base = float(h) - 1.0                       # 底部基线行（柱体从这里**向上**长）
        light = _is_light()
        p.setPen(Qt.NoPen)
        # ① 1px 极细基准线（先铺满整幅，柱体压在它上面 —— 与 Twilight_Echo 同款）
        br, bg_, bb, ba = SPEC_BASE_RGBA_LIGHT if light else SPEC_BASE_RGBA_DARK
        p.setBrush(QColor(br, bg_, bb, ba))
        p.drawRect(QRectF(0.0, y_base, float(w), 1.0))
        # ② 柱体：**贴底、向上单向生长**（不再居中对称），顶部圆角 1.5px、底部必须平：
        #    圆角矩形向下多画 radius 像素，超出控件下边界的那部分被裁掉 ⇒ 底部是平口。
        #    ⚠️ 半透明纯色叠在模糊封面背景上，逐行**合成色**必然随背景变化（alpha 设计的
        #       必然结果，不是渐变）；QA 用 α=(O−B)/(C−B) 反解时背景会被消掉，沿柱轴恒为卡片值。
        r, g_, b_, a_ = (SPEC_BAR_RGBA_LIGHT if light else SPEC_BAR_RGBA_DARK)
        p.setBrush(QColor(r, g_, b_, a_))
        rad = SPEC_BAR_RADIUS
        for i, v in enumerate(self._bars):
            if v < 0.5:                               # 停止/归零后不画（贴底线即基准线）
                continue
            hh = min(float(v), y_base - 1.0)          # 顶端至少留 1px 余量，绝不越界
            p.drawRoundedRect(QRectF(x0 + i * SPEC_PITCH, y_base - hh, SPEC_BAR_W, hh + rad),
                              rad, rad)   # 顶圆角 1.5px；底部多画的 rad 被下边界裁平成贴底
        # ③ 落峰点（task-26 方案 B，默认关闭）：柱顶之上 2px 小横条（同源色、alpha 更高）
        if self._peak_style == "falling":
            p.setBrush(QColor(r, g_, b_, SPEC_PEAK_MARK_A))
            hh_m = SPEC_PEAK_MARK_H
            for i, pv in enumerate(self._peak_px):
                if pv < 1.0:
                    continue
                hh = min(float(pv), y_base - hh_m - 1.0)
                p.drawRect(QRectF(x0 + i * SPEC_PITCH, y_base - hh - hh_m, SPEC_BAR_W, hh_m))
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
        self._placeholder = "纯音乐，请欣赏"    # ★ task-50②（用户 2）：中文次级提示（20 Medium）
        # ★★ task-23：字号/明度的逐帧插值状态 + 羽化缓冲层（都在布局/tick 里维护，绘制期零分配零测量）
        self._size_scale = 1.0
        # ★ task-26①：歌词样式（设置面板驱动；默认值 = task-23 封版，持久化后覆盖）
        self._size_cur = LYRIC_SIZE_CUR        # 当前行字号基准
        self._size_other = LYRIC_SIZE_OTHER    # 非当前行字号基准
        # ★ task-105：译文字号由 76% 比例派生（不再写死 18/14）
        # ★ task-120：译文绝对字号（卡片口径 19 / 15.5），按主词基准等比缩放
        self._trans_cur = max(9.0, round(LYRIC_TRANS_CUR_PX * (LYRIC_SIZE_CUR / LYRIC_SIZE_CUR), 1))
        self._trans_other = max(8.0, round(LYRIC_TRANS_OTHER_PX * (LYRIC_SIZE_OTHER / LYRIC_SIZE_OTHER), 1))
        self._weight_cur = QFont.Bold          # 当前行字重
        # ★ task-115（卡片 ④）：原句字重 —— 当前行 **Bold 700**、未唱 **DemiBold 600**
        #   （未唱从 500 提到 600：与译文 Medium 500 拉开一档，"一眼可分" ✓）
        self._weight_noncur = QFont.DemiBold
        # ★★ task-27（用户 ②③）：单一真源 = LINE_GAP（墨迹视觉行间距）+ 逐视觉行墨迹排版缓存
        self._gap = LYRIC_LINE_GAP             # 墨迹行间距（设置面板"行间距"滑块直接驱动它）
        self._pitch = 0.0                      # 相邻**句盒顶**间距中位（滚轮步长；_relayout 算一次）
                                               #   旧读数 _step / _step_pad 是它的只读别名（见下）
        self._scroll_s = LYRIC_SCROLL_S        # 滚动插值周期（快/标准/慢）
        self._family_override = ""             # 用户在设置里选的字体族（空 = 自动）
        self._trans_family_override = ""       # ★ task-105：译文字体单独覆盖（空 = 自动挑楷/宋/圆）
        self._size_now: list[float] = []       # 每行主词**当前**字号（px，逐帧插值）
        self._alpha_now: list[float] = []      # 每行**当前**明度（0..1，逐帧插值）
        self._font_main: list[QFont] = []      # 每行预建字体（tick 里改 size/weight，paint 直接用）
        self._font_trans: list[QFont] = []
        self._anim_t0 = 0.0                    # 本轮字号/明度动画起点（0 = 无动画）
        self._anim_from: list[tuple[float, float]] = []
        self._scroll_t0 = 0.0                  # 本轮滚动动画起点
        self._scroll_from = 0.0
        self._main_rows: list[list] = []       # ★ task-27：逐视觉行 [(子串, 基线y, 墨迹顶, 墨迹底)]
        self._trans_rows: list[list] = []
        # ★ task-32①（Lead 裁决）：**布局期预烘焙**的逐视觉行 2px 高斯投影 Pixmap
        #   （绘制期只 drawPixmap ⇒ 零文本测量；缓存键 = 文本+字号+字重+dpr）
        # ★ task-46③：黑胶装饰环的可读锚点（中心/半径/线宽/alpha），QA 按它取样
        # ★ task-50①：装饰环**已删除**（task-46③ rider 反转）；保留锚点但标 removed，
        #   方便 QA 的老检查直接翻成"无环"而不是读不到字段。
        self._vinyl_ring = {"removed": True, "removed_by": "task-50①",
                            "alpha": 0.0, "alpha_u8": 0, "width": 0.0, "radius_pad": 0.0}
        self._shadow_main: list[list] = []
        self._shadow_trans: list[list] = []
        self._shadow_cache: dict = {}
        self._shadow_on = True                 # ★ task-33 rider：投影开关（QA 差分锚点）
        self._ink_tops: list[float] = []       # ★ task-27：每行**首行墨迹顶**（绘制/快照原点；
                                               #     _tops 是分配盒顶 = 墨迹顶 − 上余量，二者不可混用）
        self._family = ""                      # 歌词字体族（布局期解析一次）
        self._buf = QPixmap()                  # ★ 真羽化缓冲层（尺寸变化时在 tick 里重建）
        self._buf_size = (0, 0)
        self._buf_dpr = 1.0                    # ★ task-27①：缓冲层设备像素比（锐度取证/换屏检测）
        self._feather = None                   # 羽化渐变画刷（尺寸变化时重建）
        self._tbtn_font: QFont | None = None   # ★ task-24：时间胶囊字体（预建）
        self._ph_font: QFont | None = None     # 占位主文案字体（18 Medium，预建）
        self._ph_sub_font: QFont | None = None # ★ task-35：占位副标题字体（更小，预建）
        self._ph_main_rect = None              # ★ task-35：占位符几何（布局期算，绘制期只读）
        self._ph_sub_rect = None
        self._ph_badge_rect = None
        self._ph_pen_x = 0.0
        self._ph_center_y = 0.0
        self._ph_block_h = 0.0
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
        # ★ task-41：胶囊交互状态（悬停/按下）+ 分段参考线 + 滚轮阻尼目标
        self._tbtn_hover = False
        self._capsule_pressed = False
        self._cursor_over = False                 # 手型光标是否由本面板 override
        self._seek_emit_count = 0                 # ★ task-41 锚点：request_seek 发射计数
        self._tbtn_ms = None                      # 胶囊对应的目标毫秒（-1/None = 无）
        # ★ task-44：间奏状态（圆点几何在 _relayout 里算，亮度在 tick 里更新）
        self._interlude_start: list = []
        self._next_ms: list = []
        self._interlude_idx = -1
        self._interlude_active = False
        self._interlude_p = 0.0
        self._dots_rect = None
        self._dot_centers: list = []
        self._now_ms = 0
        # ★ task-49：独立行几何 + 红绿灯状态 + 闪烁序列环缓冲（QA 判据 b/d 锚点）
        self._il_row_top = None
        self._il_row_h = 0.0
        self._il_filled = 0
        self._il_blink_alpha = float(IL_BLINK_MIN)
        self._il_alpha_series: list = []
        # ★★ task-57①：静态占位（`_relayout()` 一次性判定；播放中途不再变）
        self._il_static_prelude = False
        self._il_static_rows: set = set()
        self._il_row_h_static = float(IL_DOT_ROW_H)
        self._il_phase_ms0 = 0            # ★ task-57③：相位锚点（播放位置 ms）
        self._il_phase_s = 0.0
        self._manual_target_offset = 0.0
        self._guide_seg: tuple | None = None      # (x0, x1) 分段区间；None ⇒ 本帧不画
        self._text_right = 0.0                    # 歌词文本右缘（布局期量，绘制期零测量）
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
        # ★★ task-44①（用户 ⑨）：**空文本时间戳不生成独立空行** ——
        #   它只被记成上一句的 `interlude_start_ms`（间奏起点）。否则那一行会顶掉高亮，
        #   间奏期间"没有任何歌词亮着"（用户问题 ⑨ 的根因）。
        self._lines, self._interlude_start, self._next_ms = self._absorb_interludes(list(lines or []))
        self._lines = self._merge_bilingual(self._lines)
        self._interlude_idx = -1
        self._interlude_p = 0.0
        self._dots_rect = None
        self._idx = -1
        self._offset = 0.0
        self._target = 0.0
        self._relayout()
        self._place_placeholder()      # ★ task-35：占位符几何随三态切换即时重算（无残影）
        self._ph_main_rect = self._ph_main_rect   # 保持引用（可读性；几何已在上面算好）
        self.update()

    @staticmethod
    def _absorb_interludes(lines: list) -> tuple:
        """★ task-44①：把"空文本时间戳"吸收成上一句的间奏起点。

        输入 [(ms, text)]（LRC 解析结果，可能含空文本行，如 [01:15.00] 单独一行）；
        输出 (纯歌词行, interlude_start 列表, next_ms 列表)：
          · 空文本行**不进 `_lines`** ⇒ 判据 a) `n_lines` 不因空行增多 ✓、判据 b) `_idx` 不会被空行顶掉 ✓；
          · 每个**保留下来的行**记录：`interlude_start[i]` = 紧随其后那个空时间戳（无则 None）、
            `next_ms[i]` = 下一句的开始毫秒（无则 None）。
        """
        kept = []
        starts = []
        for ms, txt in lines:
            try:
                m = int(ms)
            except Exception:
                continue
            s = str(txt or "")
            if s.strip() == "":
                if kept:                      # 记到**上一句**头上（间奏起点）
                    starts[-1] = m
                continue
            kept.append((m, s))
            starts.append(None)
        nxt = [None] * len(kept)
        for i in range(len(kept) - 1):
            nxt[i] = kept[i + 1][0]
        return kept, starts, nxt

    def _interlude_progress(self, ms: int) -> tuple:
        """★ task-44②：当前是否处于"间奏/前奏"以及进度 p∈[0,1]。

        触发：① 前奏（还没到第 1 句）；② 两句间隔 ≥6000ms 且当前句已唱完 >3000ms。
        返回 (active, p, start_ms)；start_ms 为间奏起点（前奏时为 0）。
        """
        n = len(self._lines)
        if n == 0:
            return False, 0.0, 0
        first = int(self._lines[0][0])
        if ms < first:                                # ★ 前奏：第 1 句还没开始（idx 可能已预选）
            p = 0.0 if first <= 0 else max(0.0, min(1.0, float(ms) / float(first)))
            return True, p, 0
        idx = self._idx
        if idx < 0:
            return False, 0.0, 0
        if idx >= n - 1:
            return False, 0.0, 0
        nxt = self._next_ms[idx] if idx < len(self._next_ms) else None
        if nxt is None:
            return False, 0.0, 0
        line_ms = int(self._lines[idx][0])
        if (int(nxt) - line_ms) < IL_GAP_TRIGGER_MS:
            return False, 0.0, 0
        explicit = self._interlude_start[idx] if idx < len(self._interlude_start) else None
        start = int(explicit) if explicit is not None else line_ms
        if (ms - line_ms) <= IL_SUNG_TRIGGER_MS:
            return False, 0.0, 0
        span = max(1, int(nxt) - start)
        p = max(0.0, min(1.0, float(ms - start) / float(span)))
        return True, p, start

    def set_inject_position(self, ms=None) -> dict:
        """★ task-54 rider（Lead）：**把播放位置钉死**在 `ms`（正式测试入口）。

        传 `ms` ⇒ 冻结跟随（引擎每帧 `set_position` 回写被忽略）并把 `_now_ms/_idx` 钉到该位置；
        传 `None` ⇒ 解除冻结、恢复跟随。返回 `{inject_active, inject_ms, idx, now_ms, interlude_active}`。
        ⚠️ 必须用**方法调用**（`zhjctl eval` 只吃表达式；赋值语句会被静默丢弃 ✗）。
        """
        if ms is None:
            self._inject_ms = None
        else:
            self._inject_ms = int(ms)
            self.set_position_ms(int(ms), _force=True)
            try:
                self._retarget()             # ★ task-57①：注入后立即重算滚动目标
                self._update_interlude()
            except Exception:
                log.debug("注入后刷新间奏失败", exc_info=True)
        self.update()
        return {"inject_active": self._inject_ms is not None, "inject_ms": self._inject_ms,
                "idx": int(self._idx), "now_ms": int(getattr(self, "_now_ms", 0)),
                "interlude_active": bool(self._interlude_active)}

    def _in_prelude(self) -> bool:
        """★ task-49①：是否处于**前奏**（t < 首句时间 且 首句时间 ≥ 3000ms ⇒ 才算有前奏）。"""
        try:
            if not self._lines:
                return False
            first = int(self._lines[0][0])
            return first >= 3000 and int(getattr(self, "_now_ms", 0)) < first
        except Exception:
            return False

    def interlude_state(self) -> dict:
        """★ task-44 锚点（QA 用）：`{active, p, idx, n_lines, interlude_start_ms, next_ms,
        dots_rect, dots:[{cx,cy,r,alpha}...], text, prelude}`。"""
        act, p, start = self._interlude_progress(getattr(self, "_now_ms", 0))
        # ⚠️ 未激活时**不报圆点**（否则锚点会把上一次的几何当"当前有圆点"，QA 的负例判据会被污染）
        dots_c = list(getattr(self, "_dot_centers", None) or []) if act else []
        r = self._dots_rect
        # ★★ 坐标系口径（QA 打回项②）：**锚点与绘制必须同一空间** ——
        #   `_dot_centers/_dots_rect` 存的是**内容坐标**（与 _tops/_heights 同空间），
        #   绘制端画的是 `内容 + _offset`。之前锚点只报内容坐标 ⇒ QA 在面板 y≈24.5 取样，
        #   那里当然只有背景（真实屏幕位置是 y≈230）。现在锚点统一输出
        #   **面板绝对坐标**（= 内容 + _offset）并显式声明 `coord_space`。
        off = float(getattr(self, "_offset", 0.0))
        try:
            _po = self.mapTo(self.parentWidget() or self, QPoint(0, 0))
            _panel_org = [int(_po.x()), int(_po.y())]
        except Exception:
            _panel_org = [0, 0]
        dots = [[c[0], c[1] + off, c[2], c[3]] for c in dots_c]
        r_abs = QRectF(r.x(), r.y() + off, r.width(), r.height()) if r is not None else None
        return {"active": bool(act), "p": round(float(p), 4),
                "idx": int(self._idx), "n_lines": len(self._lines),
                "interlude_start_ms": int(start),
                "next_ms": (self._next_ms[self._idx] if 0 <= self._idx < len(self._next_ms) else None),
                "interlude_start_explicit": (self._interlude_start[self._idx]
                                             if 0 <= self._idx < len(self._interlude_start) else None),
                "prelude": bool(act and self._idx <= 0 and getattr(self, "_now_ms", 0) < (
                    int(self._lines[0][0]) if self._lines else 0)),
                "coord_space": "panel_absolute(= content + _offset)",
                "offset": round(off, 2),
                # ★ task-49 判据锚点：独立行几何 / 红绿灯状态 / 闪烁序列
                "row_top": (round(float(self._il_row_top), 2) if self._il_row_top is not None else None),
                "row_height": round(float(getattr(self, "_il_row_h", 0.0)), 2),
                "row_top_absolute": (round(float(self._il_row_top) + off, 2)
                                     if self._il_row_top is not None else None),
                "dot_diameter": round(2.0 * float(IL_DOT_R), 2),
                "dot_spacing": float(IL_DOT_GAP),
                "safe_pad": float(IL_DOT_SAFE_PAD),
                "filled": int(getattr(self, "_il_filled", 0)),
                "active_idx": (int(getattr(self, "_il_filled", 0))
                               if int(getattr(self, "_il_filled", 0)) < 3 else -1),
                # ★★ task-57 锚点：静态占位 / 左对齐 / 相位
                "static_prelude": bool(getattr(self, "_il_static_prelude", False)),
                # ★ live 判定（`_interlude_active` 可能还是上一素材的残留值 ⇒ 别误读）
                "in_row_now": bool(self._in_prelude() and getattr(self, "_il_static_prelude", False))
                or (bool(self._lines) and int(self._idx) in getattr(self, "_il_static_rows", set())
                    and (int(getattr(self, "_now_ms", 0)) - int(self._lines[int(self._idx)][0]))
                    > int(IL_SUNG_TRIGGER_MS)),
                "target_offset": round(float(getattr(self, "_target", 0.0)), 2),
                "static_rows": sorted(int(x) for x in getattr(self, "_il_static_rows", set())),
                "static_row_h": round(float(getattr(self, "_il_row_h_static", 0.0)), 2),
                "prelude_min_ms": int(IL_PRELUDE_MIN_MS),
                "gap_trigger_ms": int(IL_GAP_TRIGGER_MS),
                "phase_s": round(float(getattr(self, "_il_phase_s", 0.0)), 4),
                "phase_ms0": int(getattr(self, "_il_phase_ms0", 0)),
                "dot_left_px": (round(float(dots_c[0][0]) - float(IL_DOT_R), 2) if dots_c else None),
                "blink_hz": float(IL_BLINK_HZ),
                "blink_alpha": round(float(getattr(self, "_il_blink_alpha", 0.0)), 4),
                "blink_range": [float(IL_BLINK_MIN), float(IL_BLINK_MAX)],
                # ⚠️ 给**整段环缓冲**（240 样本 ≈4s）：只给 48 个（0.75s）时过零率估计会抖
                #   （实测同一实现 1.99/2.64/2.67 Hz 三个值 ⇒ 2.5Hz 的判据 ±0.3 会误判 ✗）。
                "alpha_series": list(getattr(self, "_il_alpha_series", []))[-240:],
                # ★ task-54 rider：位置注入状态（QA 判据用）
                "inject_active": bool(getattr(self, "_inject_ms", None) is not None),
                "inject_ms": getattr(self, "_inject_ms", None),
                "now_ms": int(getattr(self, "_now_ms", 0)),
                # ★ 坐标口径补全（QA 取样用）：`dots`/`dots_rect` 是**面板局部 + 滚动位移**
                #   （用 `ly.grab()` 取样）；下面两组是**宿主(AudioView)绝对**坐标
                #   （用 `av.grab()` 取样，已加面板原点）。
                "panel_origin_in_host": list(_panel_org),
                # ⚠️ QA task-49 抓到的锚点 bug：y 漏了**滚动位移 `off`**（面板局部口径是
                #   `c[1] + off`，host 口径必须同样加）⇒ 否则 dots.y≈66 而 row_top_host≈251 ✗。
                "dots_host_absolute": ([[round(c[0] + _panel_org[0], 2),
                                          round(c[1] + off + _panel_org[1], 2),
                                          round(c[2], 2), int(c[3])] for c in dots_c] if act else []),
                "row_top_host_absolute": (round(float(self._il_row_top) + _panel_org[1] + off, 2)
                                           if self._il_row_top is not None else None),
                "coord_space_dots": "panel_local + offset（ly.grab() 取样）",
                "coord_space_host": "host(AudioView) absolute（av.grab() 取样）",
                "lit_count": len([1 for c in dots_c if int(c[3]) >= int(IL_DOT_SOLID)]),
                "charging_count": len([1 for c in dots_c
                                       if int(IL_DOT_IDLE) < int(c[3]) < int(IL_DOT_SOLID)]),
                "dots_rect": ([round(r_abs.x(), 2), round(r_abs.y(), 2),
                               round(r_abs.width(), 2), round(r_abs.height(), 2)]
                              if r_abs is not None else None),
                "dots_rect_content": ([round(r.x(), 2), round(r.y(), 2),
                                       round(r.width(), 2), round(r.height(), 2)]
                                      if r is not None else None),
                "dots": [[round(c[0], 2), round(c[1], 2), round(c[2], 2), int(c[3])] for c in dots],
                "lit_order": "left->right",   # 卡片②的"从左到右依次点亮"；d) 的"从左到右递增"按"依次"理解
                "text": (self._lines[self._idx][1] if 0 <= self._idx < len(self._lines) else ""),
                "now_ms": int(getattr(self, "_now_ms", 0))}

    def _content_width(self) -> int:
        return max(200, int(self.width()) - 8)

    def _relayout(self):
        """★ task-27②：**墨迹锚定的逐视觉行排版**（真正"处处相等"的视觉行间距 + 零截断）。

        与 task-23 的固定槽位（min+40）彻底分道扬镳，模型（单一真源）：
          · 每个**视觉行**（同句折行出来的每一行、译文行）都用 QTextLayout 在同宽度下折出来，
            再取 tightBoundingRect 得到**墨迹**上下沿；
          · 相邻墨迹块的垂直空白恒为 **LINE_GAP(18)** —— 同句折行、原词↔译文、句↔句
            三类关系用的是**同一个** gap 常量（实测极差 0）；
          · 每块分配盒 = （墨迹盒 ∪ Qt 自己的排版盒 boundingRect）再上下各留 ≥5px
            （LYRIC_BOX_PAD(10)/2）⇒ g/y/p 降部**绝不削平**，栅格化外溢也吞得下；
          · 字号取**稳态显示值**（当前行 _size_cur、其余 _size_other），与 _apply_font 的
            落点一致 ⇒ 静止态实测墨迹空白严格等于 LINE_GAP（动画中途的过渡帧除外）；
          · 空行/间奏行不产生墨迹 ⇒ **不占垂直空间**，否则会把相邻可见块拉成 2×gap。
        绘制期只读这些缓存（零测量、零折行 —— paintEvent 红线）。
        """
        self._tops, self._heights, self._ink_tops = [], [], []
        w = self._content_width()
        sr = 1.0
        try:
            sr = self.parentWidget().scale_ratio()
        except Exception:
            pass
        self._size_scale = sr
        if not self._family:
            self._family = _lyric_family()
        sr = sr or 1.0
        gap = float(self._gap)
        pad = LYRIC_BOX_PAD / 2.0              # 每块分配盒的上下内边距各 5px（像素实测余量 ≥2 逻辑px）
        n = len(self._lines)
        self._main_rows: list[list] = []
        self._trans_rows: list[list] = []
        self._main_h: list[float] = []         # 主词块占用高（墨迹高 + BOX_PAD；旧探针读它推导空隙）
        self._trans_h: list[float] = []        # 译文块占用高（同上；0.0 = 本行无译文）
        ink_bottom = None                      # 上一块**有墨迹**的墨迹底（绝对 y）
        # ★★ task-49②（用户 1、2）：圆点行**必须真正占位** —— 原来只在几何上"塞进两句之间"，
        #   而两句之间的实际留白只有 LINE_GAP(18) ⇒ 36px 的行会**压住上下两句文字**
        #   （自检实测：行盒 215.4~251.4 而相邻句底 229.9/句顶 236.9 ⇒ 完全重叠），
        #   像素上就表现为"圆点挤在夹缝、阴影被切成方框"。这里在排版时预留
        #   `row_h + LINE_GAP`（另一侧由正常 gap 提供）⇒ 句→行→句 各一个 LINE_GAP。
        # ★★ task-57①（用户 57）：**一次性静态判定**（播放中途不再插拔）——
        #   · 前奏行：仅当首句 ≥ IL_PRELUDE_MIN_MS(5s) 时在**索引 0 之前**静态排入；
        #   · 句间行：仅当两句间隔 ≥ IL_GAP_TRIGGER_MS(15s) 时在该句之后静态排入；
        #   · 行高 = 正常歌词行高（上一次排版的 `_pitch`；首帧缺失时用 IL_DOT_ROW_H 兜底）。
        self._il_static_prelude = bool(n and int(self._lines[0][0]) >= int(IL_PRELUDE_MIN_MS))
        self._il_static_rows = set()
        for _si in range(max(0, n - 1)):
            try:
                if (int(self._lines[_si + 1][0]) - int(self._lines[_si][0])) >= int(IL_GAP_TRIGGER_MS):
                    self._il_static_rows.add(_si)
            except Exception:
                pass
        _il_row_h = max(float(IL_DOT_ROW_H), float(self._pitch) if float(self._pitch or 0.0) > 0.0 else 0.0)
        self._il_row_h_static = float(_il_row_h)
        _il_pre = bool(getattr(self, "_il_static_prelude", False))
        for i, (_t, txt) in enumerate(self._lines):
            main, trans = (txt.split(chr(10), 1) if chr(10) in txt else (txt, ""))
            # ★★ task-120：**去掉「」直角引号** —— 改用「冷色 + 左侧竖条 ▎」区分（卡片口径）；
            #   引号会与原文书眉感打架、且占宽度 ⇒ 已移除
            if trans:
                trans = trans.strip()
            cur = (i == self._idx)
            # 块内坐标原点 = **本行首行墨迹顶**（=0）；位置仍按墨迹锚定 ⇒ 墨迹间距恒为 gap
            # ★★ task-88（用户 14）：**折行宽度必须按 scale 反向补偿** ——
            #   断点字体被归一化到 27px 测量，而**绘制字号 = 27×sr**；sr>1（大窗口）时
            #   实际绘制比测量宽 sr 倍 ⇒ 行尾被面板右缘裁掉（用户看到的 `I won't gu` ✗）。
            #   用 (content_w − 24)/sr 作折行宽 ⇒ 绘制后恰好收在 content_w−24 内 ✓
            #   （卡片口径：content_w − 24；sr=1 时逐字相同 ✓）
            wrap_w = max(40.0, (float(w) - 24.0) / max(0.5, float(sr)))
            rows, main_bottom, m_top, m_bot = self._block_rows(
                main, self._layout_font(i, self._size_cur if cur else self._size_other, sr), wrap_w, 0.0, gap,
                wrap_font=self._wrap_font(self._size_cur, sr))
            if trans:
                trows, bottom, t_top, t_bot = self._block_rows(
                    trans, self._layout_font(i, self._trans_cur if cur else self._trans_other,
                                             sr, trans=True),
                    wrap_w, main_bottom + gap, gap,   # ★ task-88：译文**独立折行**，同一换算宽度
                    wrap_font=self._wrap_font(self._trans_cur, sr, trans=True))
            else:
                trows, bottom, t_top, t_bot = [], main_bottom, m_top, m_bot
            if rows or trows:
                # 分配盒 = 覆盖（墨迹盒 ∪ Qt 自己的排版盒）再上下各留 ≥pad
                margin_t = max(pad, -min(m_top, t_top))
                margin_b = max(pad, max(m_bot, t_bot) - bottom)
                y_ink = 0.0 if ink_bottom is None else (ink_bottom + gap)
                # ★ task-57①：静态预留（前奏行在首句上方；句间行在第 i-1 句之后）
                if ink_bottom is None:
                    if _il_pre:
                        y_ink += _il_row_h + gap
                elif (i - 1) in getattr(self, "_il_static_rows", set()):
                    y_ink += _il_row_h + gap
                top = y_ink - margin_t          # 分配盒顶（≥ 墨迹顶 − pad）
                box_h = bottom + margin_t + margin_b
                ink_bottom = y_ink + bottom
                self._ink_tops.append(y_ink)    # ★ 绘制/快照用"首行墨迹顶"（盒顶不是绘制原点）
                self._main_h.append(main_bottom + 2 * pad)
                self._trans_h.append((bottom - main_bottom - gap + 2 * pad) if trows else 0.0)
            else:
                # 空行/间奏行**不产生墨迹 ⇒ 不占垂直空间**，否则会把相邻可见块拉成 2×gap
                top, box_h = (0.0 if ink_bottom is None else (ink_bottom + gap)), 0.0
                self._ink_tops.append(top)
                self._main_h.append(0.0)
                self._trans_h.append(0.0)
            self._main_rows.append(rows)
            self._trans_rows.append(trows)
            self._tops.append(top)
            self._heights.append(box_h)
        tops_ink = [self._tops[i] for i in range(n) if self._heights[i] > 0.0]
        pitches = sorted(tops_ink[i + 1] - tops_ink[i] for i in range(len(tops_ink) - 1))
        self._pitch = (pitches[len(pitches) // 2] if pitches
                       else (self._heights[0] + gap if self._heights else 0.0))
        self._build_fonts()
        self._rebuild_shadows()          # ★ task-32①：排版定稿后一次性预烘焙全部投影
        self._reset_anim()
        self._retarget()

    # ⚠️⚠️ 这里**不能**有 `@staticmethod`：本方法是**实例方法**（第一参数 self）。
    #   事故记录（2026-10-22 自查）：我最初插入位置正好在 `@staticmethod` 与
    #   `def _tune_font` 之间 ⇒ 装饰器被本方法"吃掉"，`self._dark_text_on_light()`
    #   抛 `TypeError: missing 1 required positional argument: 'self'` ⇒ **`_rebuild_shadows()`
    #   在取 alpha 之前就崩**，表现成"深色投影恒空、开关无效"（QA 的 alpha:null 即此症状的后果）。
    def _dark_text_on_light(self) -> bool:
        """★ task-97：当前是否"**亮底 + 深色字**"（浅色主题 / 亮玻璃）⇒ 不得画歌词黑影。"""
        try:
            from . import theme as _t
            if _t.is_light():
                return True
            # 玻璃皮肤：按底色调亮度判定（亮玻璃也属于"深字"场景）
            if _t.is_glass_skin():
                try:
                    c = _t.qt_color(_t.P["bg_base"])
                    lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
                    return lum > 150.0
                except Exception:
                    return False
        except Exception:
            pass
        return False

    @staticmethod
    def _tune_font(f: QFont) -> QFont:
        """★★ task-97（用户 96）：**歌词字体统一光栅化策略**。

        · `PreferAntialias | PreferQuality` —— 强制灰度抗锯齿 + 质量优先；
        · `PreferNoHinting` —— **关掉 hinting**：GDI/FreeType 的网格对齐会把同一条横
          吸到整数像素，导致"一横 2px、一横 1px"的粗细不均 ✗（用户原话）。
        所有歌词字体（主词/译文/占位/断点字体）都必须过这一道。"""
        try:
            f.setStyleStrategy(QFont.StyleStrategy(QFont.PreferAntialias | QFont.PreferQuality))
        except Exception:
            pass
        try:
            f.setHintingPreference(QFont.PreferNoHinting)
        except Exception:
            pass
        return f

    def _layout_font(self, i: int, base_px: float, sr: float, trans: bool = False) -> QFont:
        """布局期字体（族/字重与绘制期一致；字号 = 基准 × scale_ratio，只在这里构造）。

        ★ task-105/115：trans=True ⇒ 用**译文字体族 + Medium(500) 字重**（与绘制期 `_font_trans` 一致）。
        """
        f = QFont()
        fam = self._trans_family() if trans else self._family
        if fam:
            f.setFamily(fam)
        f.setPixelSize(max(9, int(round(float(base_px) * sr))))
        f.setWeight((QFont.Bold if i == self._idx else QFont.DemiBold) if trans
                    else (self._weight_cur if i == self._idx else self._weight_noncur))
        return self._tune_font(f)

    def _wrap_font(self, base_px: float, sr: float, trans: bool = False) -> QFont:
        """★ task-32②：**基准断点字体**（= 高亮行字号 + 高亮字重）。

        全篇所有视觉行都用它算断点 ⇒ 断点结构与"这一行是不是当前行"无关；
        渲染仍用各行自己的字体（20px 未唱行沿用同一断点，放大时原地平滑放大）。
        """
        f = QFont()
        # ★ task-115：`trans=True` 时必须用**译文族**量断点 —— 否则按主词族(HarmonyOS)算宽度、
        #   绘制却用译文族(雅黑/幼圆)，两者 advance 不同 ⇒ 长译文行可能溢出被裁 ✗
        _fam = self._trans_family() if trans else self._family
        if _fam:
            f.setFamily(_fam)
        f.setPixelSize(max(9, int(round(float(base_px) * sr))))
        f.setWeight(QFont.Medium if trans else self._weight_cur)
        return self._tune_font(f)          # ★ task-97：断点字体也统一光栅化策略

    def _block_rows(self, text: str, font: QFont, width: float,
                    first_ink_top: float, gap: float,
                    wrap_font: QFont | None = None) -> tuple[list, float, float, float]:
        """把**一块**文字排成若干视觉行。

        返回 ([(子串, 相对原点的基线 y, 墨迹顶, 墨迹底)], 末行墨迹底, 块盒顶, 块盒底)；
        首行墨迹顶 = first_ink_top；块内后续行墨迹顶 = 上一行墨迹底 + gap（**同一个 LINE_GAP**）。
        块盒 = 该块所有行「墨迹盒 ∪ Qt 自己的排版盒(boundingRect)」的并集 ⇒ 分配盒绝不裁字。
        """
        if not text:
            return [], first_ink_top, first_ink_top, first_ink_top
        fm = QFontMetrics(font)                # 布局期测量：本块共用一把尺，逐行取排版盒
        rows: list[list] = []
        prev_bottom = 0.0
        box_top = box_bottom = 0.0
        wf = wrap_font if wrap_font is not None else font
        rows_text = wrap_rows(text, wf, width)      # ★ task-32②：断点用基准字体（与字号无关）
        for k, rt in enumerate(rows_text):
            it, ib = row_ink(font, rt)
            top = first_ink_top if k == 0 else (prev_bottom + gap)
            base = top - it
            ink_t, ink_b = top, top + (ib - it)
            fb = fm.boundingRect(rt)            # Qt 自己的排版盒（相对基线；门禁的证真组数它）
            f_top, f_bot = float(fb.top()), float(fb.bottom())
            if not (-2.0 * fm.height() <= f_top <= 1.0) or f_bot < f_top:
                # 字体引擎退化（无字体库/offscreen）时 Qt 会给出 100000 级偏移 ⇒ 回落成 ascent/descent
                f_top, f_bot = -float(fm.ascent()), float(fm.descent())
            r_t, r_b = min(ink_t, base + f_top), max(ink_b, base + f_bot)
            if k == 0:
                box_top, box_bottom = r_t, r_b
            else:
                box_top, box_bottom = min(box_top, r_t), max(box_bottom, r_b)
            rows.append([rt, base, ink_t, ink_b])
            prev_bottom = base + ib
        return rows, prev_bottom, box_top, box_bottom

    # ---- task-27：兼容旧探针/门禁的**只读别名**（单一真源仍是 _gap / _pitch，不存第二份状态）--
    @property
    def _step(self) -> float:
        """旧读数（task-23~26 的门禁读它）：统一行距 == 相邻句盒顶间距中位 _pitch。"""
        return float(self._pitch)

    @property
    def _step_pad(self) -> float:
        """旧读数：行间距设置值 == LINE_GAP _gap（task-27 起语义 = 墨迹视觉空白）。"""
        return float(self._gap)

    # ---- task-23：字号/明度插值的辅助（全部在布局/tick 里跑，绘制期零测量零分配）----------
    def _targets(self, i: int) -> tuple[float, float]:
        """第 i 行的（目标主词字号, 目标明度）。当前行更大更亮 —— 卡面 ③ 的两端。"""
        sr = self._size_scale or 1.0
        cur = (i == self._idx)
        size = (self._size_cur if cur else self._size_other) * sr
        # ★★ task-45②（用户 ④）：未唱行不透明度 **0.40 → 0.575**（判据要求落在 [0.55,0.60]）
        return max(10.0, size), (1.0 if cur else LYRIC_ALPHA_UNSUNG)

    def _trans_family(self) -> str:
        """★ task-105：译文字体族（设置覆盖 > 自动挑 楷/仿宋/思源宋/幼圆）。"""
        ov = str(getattr(self, "_trans_family_override", "") or "").strip()
        # ★ task-120：`follow` / `auto` / `default` / 空 都视为"跟随默认"⇒ 内置 HarmonyOS
        _AUTO = ("system", "跟随系统", "follow", "auto", "default", "跟随")
        if ov and ov not in _AUTO:
            return ov
        return _trans_family_default()

    def apply_trans_family(self, family) -> None:
        """★ task-105：设置键 **lyric.trans_font** 的唯一施加点（译文可单独换字体）。"""
        fam = str(family or "").strip()
        _AUTO = ("", "system", "跟随系统", "follow", "auto", "default", "跟随")
        self._trans_family_override = "" if fam in _AUTO else fam
        self._build_fonts()
        self._relayout()
        self._reset_anim()
        self._retarget()
        self.update()

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
            self._tune_font(f)                               # ★ task-97 统一光栅化策略
            self._font_main.append(f)
            g = QFont()
            tf = self._trans_family()                        # ★ task-105：楷/宋/圆体（与黑体原文互补）
            if tf:
                g.setFamily(tf)
            # ★★ task-120：译文**当前行 Bold 700 / 未唱 DemiBold 600**（卡片明确；不再 500）
            g.setWeight(QFont.Bold if i == self._idx else QFont.DemiBold)
            self._tune_font(g)                               # ★ task-97（含 PreferQuality/NoHinting）
            self._font_trans.append(g)
        for i in range(len(self._lines)):
            self._apply_font(i)
        # 占位分支的字体也在这里预建（绘制期不再 new QFont）
        # ★ task-35：主文案 **18 Medium**（字族与正文一致）+ 副标题更小（11 Regular）
        ph = QFont()
        if fam:
            ph.setFamily(fam)
        ph.setPixelSize(PH_MAIN_PX)
        ph.setWeight(QFont.Medium)              # 500（卡片①）
        self._ph_font = ph
        ps = QFont()
        if fam:
            ps.setFamily(fam)
        ps.setPixelSize(PH_SUB_PX)
        ps.setWeight(QFont.Normal)
        self._ph_sub_font = ps
        self._place_placeholder()               # ★ 占位符几何（布局期算，绘制期零测量）
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
        self._place_interlude()          # ★ task-44②：圆点几何（布局期算 ⇒ 绘制期零测量）

    def _apply_font(self, i: int) -> None:
        """把**当前插值字号**写进第 i 行的 QFont（只改 pixelSize，不做任何测量）。"""
        if i >= len(self._font_main):
            return
        sr = self._size_scale or 1.0
        cur = (i == self._idx)
        size = self._size_now[i] if i < len(self._size_now) else self._targets(i)[0]
        self._font_main[i].setPixelSize(max(10, int(round(size))))
        tsize = (self._trans_cur if cur else self._trans_other) * sr
        self._font_trans[i].setPixelSize(max(9, int(round(tsize))))
        self._rebuild_shadows(i)         # 字号变了就换投影（键未变 ⇒ 只命中缓存）

    def set_shadow_enabled(self, on: bool) -> bool:
        """★ task-33 rider（Lead 裁决）：**投影开关锚点** —— QA 可"关投影→抓图→开投影→抓图"
        做同像素差分，无需 monkey-patch（我的内部差分法外部打不进预烘焙缓存）。

        语义：关 ⇒ 清空逐行投影指令 + 不做绘制；开 ⇒ 立即重排一次（重建缓存）。
        返回切换后的状态。

        ⚠️ **勘误（QA 2026-10-22 实测）**：本 docstring 原写"属性同名只读锚点：`lyrics.shadow_enabled`"
        —— **该属性从未实现**（`hasattr(lyrics, 'shadow_enabled') == False` ✗），只有本 setter。
        读值请用：`w.stage.audio_view.lyrics.shadow_probe()` 或直接 `vars(ly).get('_shadow_on')`。
        """
        self._shadow_on = bool(on)
        try:
            if self._shadow_on:
                self._shadow_cache.clear()
                # ★ task-97 修复：先确保排版/字体就绪，再建投影（否则可能建 0 张贴图 ✗）
                try:
                    self._relayout()
                except Exception:
                    log.debug("开投影前重排失败", exc_info=True)
                self._rebuild_shadows()
            else:
                self._shadow_main = [[] for _ in (self._shadow_main or [])]
                self._shadow_trans = [[] for _ in (self._shadow_trans or [])]
        except Exception:
            log.debug("切换歌词投影开关失败", exc_info=True)
        self.update()
        return self._shadow_on

    def _shadow_for(self, text: str, font: QFont, alpha: int | None = None,
                    blur_px: float | None = None):
        """取/建一行文字的投影 Pixmap（**只在布局期调用**；命中缓存则零成本）。

        ★ task-88：新增 `alpha`/`blur_px`（**默认沿用主词口径** ⇒ 主词行为逐位不变）；
        未唱译文用更淡（20）+ 更柔（2.6px）的一套 ⇒ 双语行不再"发脏" ✗
        """
        if not text or font is None:
            return None
        _a = int(LYRIC_SHADOW_A if alpha is None else alpha)
        _b = float(LYRIC_SHADOW_BLUR if blur_px is None else blur_px)
        try:
            dpr = float(self.parentWidget().devicePixelRatioF() or 1.0)
        except Exception:
            dpr = 1.0
        key = (text, font.family(), int(font.pixelSize()), int(font.weight()), round(dpr, 2),
               _a, round(_b, 2))
        if key in self._shadow_cache:
            v = self._shadow_cache[key]
            return v if v is not None else None
        if len(self._shadow_cache) > 192:      # 有界：动画期字号会滚动，满了整体清
            self._shadow_cache.clear()
        pm = build_text_shadow(text, font, _a, _b, dpr)
        self._shadow_cache[key] = pm
        return pm

    def _rebuild_shadows(self, i: int | None = None) -> None:
        """★ task-32①：为第 i 行（None = 全部）预烘焙投影 Pixmap。

        调用点全部是**布局/动画步进**（_relayout / _apply_font）⇒ 绘制期依旧零测量零构造。
        字号没变时缓存键不变 ⇒ 只是一次 dict 命中，零开销。
        """
        if not getattr(self, "_shadow_on", True):       # ★ task-33 rider：关投影 ⇒ 不建贴图
            return
        # ★★ task-97（用户 96）：**亮底深字（浅色/玻璃亮皮）时彻底关闭歌词投影** ——
        #   深色小字再叠 2px 黑影会糊成"黑团 + 重影" ✗。深色模式白字才保留极柔底影。
        #   判断放在这里（布局期），绘制期只读空列表 ⇒ 依旧零测量 ✓
        if self._dark_text_on_light():
            n0 = len(getattr(self, "_main_rows", []) or [])
            self._shadow_main = [[] for _ in range(n0)]
            self._shadow_trans = [[] for _ in range(n0)]
            self._shadow_cache.clear()
            return
        n = len(getattr(self, "_main_rows", []) or [])
        idxs = range(n) if i is None else ([i] if 0 <= i < n else [])
        for k in idxs:
            f = self._font_main[k] if k < len(self._font_main) else None
            g = self._font_trans[k] if k < len(self._font_trans) else None
            mrows = self._main_rows[k] if k < len(self._main_rows) else []
            trows = self._trans_rows[k] if k < len(self._trans_rows) else []
            msh = [self._shadow_for(rt, f) for rt, _by, _it, _ib in mrows]
            # ★ task-88：译文（尤其**未唱**行）用更淡更柔的投影
            _ta = int(LYRIC_TRANS_SHADOW_A) if k != self._idx else int(LYRIC_SHADOW_A)
            _tb = float(LYRIC_TRANS_SHADOW_BLUR) if k != self._idx else float(LYRIC_SHADOW_BLUR)
            tsh = [self._shadow_for(rt, g, alpha=_ta, blur_px=_tb) for rt, _by, _it, _ib in trows]
            while len(self._shadow_main) <= k:
                self._shadow_main.append([])
            while len(self._shadow_trans) <= k:
                self._shadow_trans.append([])
            self._shadow_main[k] = msh
            self._shadow_trans[k] = tsh


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

    def set_position_ms(self, ms: int, _force: bool = False):
        # ★★ task-54 rider（Lead 裁决）：**测试注入冻结** —— 冻结期间引擎每帧回写一律忽略
        #   （此前 QA 无法把播放钉在间奏段：`set_position_ms` 下一帧就被 `set_position` 覆盖 ✗）。
        #   只有 `set_inject_position()` 能改（它带 `_force=True`）。
        if getattr(self, "_inject_ms", None) is not None and not _force:
            return
        if not self._lines:
            return
        self._now_ms = int(ms)
        idx = -1
        for i, (t, _txt) in enumerate(self._lines):
            if t <= ms:
                idx = i
            else:
                break
        # ★★ task-49①（用户 1）：“前奏零误选” —— **撤销 task-44 的预选 rider**。
        #   用户实测：前奏期第一句被提前加粗高亮、圆点还错塞在 1/2 句之间 ⇒ 这里的
        #   预选就是根因。新口径：前奏期 `_idx = -1`（第一句保持**未唱的普通字重/半透明**），
        #   圆点行独立排在**第一句正上方**（见 `_place_interlude()`）。
        #   ⚠️ 句间间奏的"保持上一句高亮"（task-44 判据 b）**不变**：空行不进 `_lines`
        #   本身就保证了这一点。
        if idx < 0 and int(getattr(self, "_now_ms", 0)) >= int(self._lines[0][0]):
            idx = 0            # 只兜"已经过第 1 句时间戳但没匹配上"的极端情况；前奏不预选
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
            # ★★ task-57①（用户 57）：**前奏期间"高亮行"就是圆点行** ⇒ 滚动目标必须对准它，
            #   否则圆点行被顶到屏幕外（自检实测 row 中心只到面板 2.24% ✗，用户要 40% 黄金线）。
            #   这里**直接推导**圆点行中心（不依赖 `_place_interlude()` 的执行时机 ✓）。
            if (self._idx < 0 and self._lines and self._in_prelude()
                    and getattr(self, "_il_static_prelude", False) and self._tops):
                _rh = float(getattr(self, "_il_row_h_static", IL_DOT_ROW_H))
                _cy = (float(self._tops[0]) - float(LYRIC_LINE_GAP) - _rh) + _rh / 2.0
                self._target = self.height() * CURRENT_LINE_RATIO - _cy
            else:
                self._target = 0.0
        else:
            gold = self.height() * CURRENT_LINE_RATIO
            self._target = gold - (self._tops[self._idx] + self._heights[self._idx] / 2.0)
        self._scroll_from = self._offset
        self._scroll_t0 = time.perf_counter()

    # ---- task-24：滚轮手动浏览 / 悬停焦点 / 时间胶囊 / 5 秒无操作回位 ---------------------
    def _line_at_y(self, y: float) -> int:
        """命中测试：优先落在某行的**盒内**；落在行间空白则归给最近的一行（滚轮浏览用）。"""
        cy = y - self._offset
        best, best_d = -1, 1e9
        for i, top in enumerate(self._tops):
            h = self._heights[i] if i < len(self._heights) else 0.0
            if top <= cy < top + h:
                return i
            d = min(abs(cy - top), abs(cy - (top + h)))
            if d < best_d:
                best_d, best = d, i
        return best

    def _line_center_y(self, i: int) -> float:
        """第 i 行**墨迹中心**的内容坐标（滚动边界与焦点都用它 ⇒ 单一真源）。"""
        return float(self._tops[i]) + float(self._heights[i]) / 2.0

    def _text_row_idx(self) -> tuple:
        """★ task-60：**真实有文字歌词行**的首/末索引。

        ⚠️ task-57 的圆点行是**静态预留的非歌词行**（不占 `_tops`），空行/间奏行高度为 0 ⇒
        边界必须跳过 `heights <= 0` 的行，否则"滚到顶"会停在一个没有墨迹的行上 ✗。
        """
        idx = [i for i in range(len(self._tops)) if float(self._heights[i]) > 0.0]
        if not idx:
            return -1, -1
        return idx[0], idx[-1]

    def _scroll_bounds(self) -> tuple:
        """手动滚动的合法区间 (lo, hi)（单一真源；`_clamp_offset` 与滚轮阻尼共用）。

        ★★ task-60（用户 8）：按 Lead 口径重写 ——
          `hi = 首句中心对齐黄金线`、`lo = 末句中心对齐黄金线`，**允许负值**（不再 max(0, …)）；
          首/末句取**真实有文字行**（跳过空行/间奏行，圆点行本就不在 `_tops` 里）。
        """
        if not self._tops:
            return 0.0, 0.0
        gold = self.height() * CURRENT_LINE_RATIO
        fi, li = self._text_row_idx()
        if fi < 0:
            return 0.0, 0.0
        self._scroll_max = gold - self._line_center_y(fi)
        self._scroll_min = gold - self._line_center_y(li)
        return min(self._scroll_min, self._scroll_max), max(self._scroll_min, self._scroll_max)

    def _clamp_offset(self):
        """把手动滚动的位移夹在"第一行/最后一行落在黄金线"之间（翻不出歌词）。"""
        if not self._tops:
            self._offset = 0.0
            return
        lo, hi = self._scroll_bounds()
        self._offset = max(lo, min(hi, self._offset))

    def _manual_arm(self):
        """起/重起 5 秒无操作计时。⚠️ 用**临时 singleShot**（非常驻定时器）：
        常驻 QTimer 必须仍然是 2 个（帧钟红线）；代际号保证"期间又操作 ⇒ 旧回调作废"。"""
        self._manual_gen += 1
        gen = self._manual_gen
        QTimer.singleShot(LYRIC_MANUAL_HOLD_MS, lambda: self._manual_timeout(gen))

    def _enter_manual(self):
        self._manual_scroll = True
        if not self._manual_scroll or abs(self._manual_target_offset - self._offset) > 1e9:
            self._manual_target_offset = self._offset
        self._manual_target_offset = self._offset if self._manual_target_offset == 0.0 \
            else self._manual_target_offset
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
        self._manual_target_offset = self._offset      # ★ task-41④：阻尼目标跟随退出点
        self._tbtn_hover = False
        self._capsule_pressed = False
        try:
            from PySide6.QtWidgets import QApplication
            if self._cursor_over:
                QApplication.restoreOverrideCursor()
                self._cursor_over = False
        except Exception:
            pass
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
        # ★★ task-60（用户 8）：焦点跟随**黄金线**（不是悬停行、也不是 50% 中心）——
        #   原来 hover 优先 ⇒ 滚轮滚到顶，焦点仍停在鼠标停留的第 2/3 句上（用户"选不到第一句"）✗；
        #   且回退中心写的是 0.5 而非 CURRENT_LINE_RATIO(0.40) ⇒ 胶囊/参考线整体偏下一行 ✗。
        idx = self._hover_idx if 0 <= self._hover_idx < n else -1
        if idx < 0:
            cy = self.height() * CURRENT_LINE_RATIO
            best = None
            for i in range(n):
                if float(self._heights[i]) <= 0.0:
                    continue                     # ★ 只认真实有文字行（空行/间奏行不算）
                c = self._offset + self._line_center_y(i)
                d = abs(c - cy)
                if best is None or d < best:
                    best, idx = d, i
            if idx < 0:
                idx = 0
        self._focus_idx = idx
        self._guide_y = self._offset + self._tops[idx] + self._heights[idx] / 2.0
        ms, txt = self._lines[idx] if idx < len(self._lines) else (None, "")
        has_ts = (ms is not None) and int(ms) >= 0 and str(txt).strip() != ""
        self._tbtn_ms = int(ms) if has_ts else None
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
        # ★★ task-41③：分段参考线的**区间**（布局/交互期量好，绘制期零测量）
        #   ① 歌词文本右缘 = 所有**可见**视觉行的最大排版宽度；
        #   ② 参考线只在 [文本右缘+16, 胶囊左缘−12] 之间画 ⇒ 不穿字、不刺穿胶囊。
        try:
            self._text_right = self._measure_text_right()
            if self._tbtn_idx >= 0 and not self._tbtn_rect.isEmpty():
                x0 = self._text_right + LYRIC_GUIDE_GAP_TEXT
                x1 = float(self._tbtn_rect.left()) - LYRIC_GUIDE_GAP_CAPSULE
                self._guide_seg = (x0, x1) if (x1 - x0) >= 2.0 else None
            else:
                self._guide_seg = None
        except Exception:
            self._guide_seg = None

    def _measure_text_right(self) -> float:
        """可见视觉行的最大排版宽度（**交互/布局期**调用，绘制期绝不调用）。"""
        right = 0.0
        try:
            for i, rows in enumerate(getattr(self, "_main_rows", None) or []):
                if self._heights[i] <= 0.0 and not rows:
                    continue
                f = self._font_main[i] if i < len(self._font_main) else None
                if f is None:
                    continue
                fm = QFontMetrics(f)
                for rt, _by, _it, _ib in rows:
                    right = max(right, float(fm.horizontalAdvance(rt)))
        except Exception:
            return 0.0
        return right

    # ---- ★★ task-41①：**时间胶囊**的鼠标拦截（与滚轮同一个应用级过滤器入口）-----------
    #   ⚠️ 目标是**歌词面板内自绘的 `_tbtn_rect`（▶ mm:ss）**，不是底部 ControlBar 的
    #   ProgressSlider（那个有原生拖动逻辑，本来就能 seek —— QA 第一版就误点在那里）。
    def _in_panel_pt(self, gx: int, gy: int) -> bool:
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size()).contains(QPoint(int(gx), int(gy)))

    def capsule_hit(self, gx: int, gy: int, pad: float = 0.0) -> bool:
        """全局点是否命中**面板内时间胶囊**（无胶囊/未进入手动浏览 ⇒ False）。

        `pad`：容差（px）。自检实测：按下与松开之间主帧钟会重算几何（`_guide_y` 随 `_offset`
        收敛微动）⇒ 严格命中会在"松开"那一下打空，点击就丢了。松开判定用 `pad=8`（半个
        胶囊高 + 余量），既容忍漂移又不会把胶囊外的点击算进来。
        """
        if self._tbtn_idx < 0 or self._tbtn_rect.isEmpty():
            return False
        tl = self.mapToGlobal(QPoint(0, 0))
        p = QPointF(float(gx) - tl.x(), float(gy) - tl.y())
        if pad and not self._tbtn_rect.contains(p):
            return self._tbtn_rect.adjusted(-pad, -pad, pad, pad).contains(p)
        return self._tbtn_rect.contains(p)

    def force_arrow_cursor(self) -> bool:
        """★ task-47②：**显式恢复箭头光标**（离开胶囊/面板时调用）。返回是否真的恢复了。

        ⚠️ 原来只有"面板内但移出胶囊"这一条路径会 `restoreOverrideCursor()`；光标一旦
        **直接离开面板**（例如移到抽屉手柄上），`mouse_owns()` 在 `_in_panel_pt` 处提前
        return ⇒ override **永久残留** —— 这正是用户反馈的"光标卡在手型"。
        """
        was = bool(getattr(self, "_cursor_over", False))
        try:
            from PySide6.QtWidgets import QApplication
            if was:
                QApplication.restoreOverrideCursor()
        except Exception:
            log.debug("恢复箭头光标失败", exc_info=True)
        self._cursor_over = False
        self._tbtn_hover = False
        self._capsule_pressed = False
        self.update()
        return was

    def mouse_owns(self, kind: str, ev) -> bool:
        """★ task-41①：面板内鼠标事件 —— 悬停胶囊 ⇒ 手型+边框高亮；左键 click ⇒ seek 恰好 1 次。

        返回 True = **已消费**（AudioView 的应用级过滤器据此 `return True`，阻止事件继续派发）。
        面板外/非胶囊区域一律返回 False（不抢其它 UI 的点击）。
        """
        try:
            gp = ev.globalPosition().toPoint()
        except Exception:
            gp = QCursor.pos()
        gx, gy = int(gp.x()), int(gp.y())
        if not self._in_panel_pt(gx, gy):
            return False
        over = self.capsule_hit(gx, gy)
        if kind == "move":
            if over != self._tbtn_hover:
                self._tbtn_hover = over
                self.update()
            try:
                from PySide6.QtWidgets import QApplication
                if over and not self._cursor_over:
                    QApplication.setOverrideCursor(Qt.PointingHandCursor)
                    self._cursor_over = True
                elif (not over) and self._cursor_over:
                    QApplication.restoreOverrideCursor()
                    self._cursor_over = False
            except Exception:
                log.debug("胶囊光标切换失败", exc_info=True)
            return False                       # 移动**不消费**（不影响窗口拖动/其它 UI）
        try:
            btn = ev.button()
        except Exception:
            btn = Qt.NoButton
        if btn != Qt.LeftButton:
            return False
        if kind == "press":
            if not over:
                return False
            self._capsule_pressed = True
            self.update()
            return True                        # 吃掉按下（避免穿透到下面的覆盖层）
        if kind == "release":
            # ★ 松开用**带容差**的命中（见 capsule_hit 的说明）：按下→松开之间几何会微动
            fired = bool(self._capsule_pressed and self.capsule_hit(gx, gy, pad=LYRIC_TBTN_RELEASE_PAD))
            self._capsule_pressed = False
            if fired:
                self._activate_capsule()       # ⇒ 同帧退出手动 + request_seek 恰好 1 次
            self.update()
            return True
        return False

    def lyric_line_rects(self) -> list:
        """★ task-45 补锚点③（QA 判据③）：**面板绝对坐标**的逐视觉行带。

        屏幕 y = `_tops[i] + _offset`（**不是** `_tops[i]` 单独用 —— QA 按"内容+offset"映射时
        因为 `_offset=−326` 把整条行带算到屏外，差分恒 0）。这里直接给算好的绝对矩形，
        并**上下各外扩 16px**：投影有 dx/dy 偏移 + 2px 高斯，会溢出墨迹盒（不外扩会漏读）。
        """
        out = []
        off = float(getattr(self, "_offset", 0.0))
        pad = 16.0
        for i in range(len(self._tops)):
            y0 = float(self._tops[i]) + off - pad
            h = float(self._heights[i]) + 2.0 * pad
            out.append({"i": int(i), "cur": bool(i == self._idx),
                        "alpha": (round(float(self._alpha_now[i]), 4)
                                  if i < len(self._alpha_now) else None),
                        "rect_panel_absolute": [0.0, round(y0, 2), round(float(self.width()), 2),
                                                round(h, 2)],
                        "pad": pad,
                        "text": (self._lines[i][1] if i < len(self._lines) else "")})
        return out

    def shadow_probe(self) -> dict:
        """★ task-45 补锚点③④：**阴影随行 α 缩放** + 深色档暗化强度的可读测量。

        做法：`shadow on` 抓一帧、`shadow off` 抓一帧，在 `lyric_line_rects()` 的**绝对行带**内
        逐像素差分 ⇒ 每行 `shadow_px / shadow_sum / p95_darken`；`edge_grad_px` = 字形像素到
        背景的过渡带宽度中位数（糊边判据）。返回 `{rows, cur_px, unsung_px, ratio, alpha_ratio}`。
        """
        import time as _t
        from PySide6.QtWidgets import QApplication

        def _settle() -> None:
            """★ 关键：只 update()+processEvents() 时离屏控件可能不会真正重绘 ⇒ 两次 grab
            完全一样、差分为 0（我第一版就是这个假 0）。必须 repaint() + 多轮事件循环。"""
            self.update()
            self.repaint()
            for _ in range(8):
                QApplication.processEvents()
                _t.sleep(0.02)

        # ⚠️ 必须抓**宿主**（AudioView）而不是面板自己：面板 grab 出来两次完全一样（差分为 0），
        #   而宿主图里能测到阴影（我自检的 860/472 就是抓宿主量的）。
        host = self.parentWidget() or self
        try:
            _settle()
            pm1 = host.grab()
            self.set_shadow_enabled(False)
            _settle()
            pm2 = host.grab()
            self.set_shadow_enabled(True)
            _settle()
        except Exception:
            return {"error": "grab failed"}
        i1 = pm1.toImage().convertToFormat(QImage.Format_ARGB32)
        i2 = pm2.toImage().convertToFormat(QImage.Format_ARGB32)
        dpr = float(pm1.devicePixelRatio() or 1.0)
        bands = self.lyric_line_rects()
        try:
            _org = self.mapTo(host, QPoint(0, 0))
            _ox, _oy = int(_org.x()), int(_org.y())
        except Exception:
            _ox, _oy = 0, 0
        rows = []
        for b in bands:
            x, y, w, h = b["rect_panel_absolute"]
            x, y = x + _ox, y + _oy
            x0, y0 = int(x * dpr), int(max(0.0, y) * dpr)
            x1, y1 = int((x + w) * dpr), int((y + h) * dpr)
            n = 0
            darken = []
            for yy in range(y0, min(i1.height(), y1)):
                for xx in range(x0, min(i1.width(), x1)):
                    a = i1.pixelColor(xx, yy)
                    c = i2.pixelColor(xx, yy)
                    d = ((a.red() - c.red()) + (a.green() - c.green()) + (a.blue() - c.blue())) / 3.0
                    if d > 6.0:
                        n += 1
                        darken.append(d)
            darken.sort()
            p95 = (round(darken[int(0.95 * (len(darken) - 1))], 2) if darken else 0.0)
            if darken:
                thr = 0.25 * p95
                run = best = 0
                for d in darken:
                    run = run + 1 if d >= thr else 0
                    best = max(best, run)
                grad = best
            else:
                grad = 0
            rows.append({"i": b["i"], "cur": b["cur"], "alpha": b["alpha"],
                         "shadow_px": int(n), "shadow_max_darken": (round(max(darken), 2) if darken else 0.0),
                         "p95_darken": p95, "edge_grad_px": int(grad),
                         "rect_panel_absolute": b["rect_panel_absolute"]})
        cur = [r for r in rows if r["cur"]]
        uns = [r for r in rows if (not r["cur"]) and r["shadow_px"] > 0]
        out = {"rows": rows, "coord_space": "host_absolute(= AudioView local; panel origin 已加)", "dpr": dpr,
               "cur_px": (cur[0]["shadow_px"] if cur else 0),
               "unsung_px": (uns[0]["shadow_px"] if uns else 0)}
        if cur and uns:
            out["ratio"] = round(uns[0]["shadow_px"] / max(1.0, float(cur[0]["shadow_px"])), 3)
            out["alpha_ratio"] = round(float(uns[0]["alpha"] or 0)
                                       / max(0.001, float(cur[0]["alpha"] or 1)), 3)
            out["p95_darken_cur"] = cur[0]["p95_darken"]
            out["p95_darken_unsung"] = uns[0]["p95_darken"]
            out["edge_grad_px"] = max(cur[0]["edge_grad_px"], uns[0]["edge_grad_px"])
        return out

    def badge_geometry(self) -> dict:
        """★ task-46① 锚点（QA 极坐标采样用）：黑胶图标的**全部标称几何** + 装饰环参数。

        纯读数（不含测量）：外圈半径/线宽、两条对称 60° 圆弧的半径与起止角、中心环/孔半径、
        贴图透明度；`pixmap_keys` 给出当前徽标缓存键（(R, dpr, light) ⇒ 换 R 自动失效重建）。
        """
        _d = max(1.0, float(self._dpr_now()))
        # ★★ task-46 补锚点（QA 定位像素用）：徽章**画在哪** + **什么时候画**。
        #   ⚠️ 判定条件是 **`not self._lines`（本曲没有歌词 ⇒ 占位符态）**，与"有没有封面"**无关** ——
        #     有歌词时 `_paint_content()` 直接走歌词分支，徽章**不画**（这就是 QA 打在封面/
        #     圆心区取到 α255 的原因：那是左栏封面图，不是徽章）。
        _host = self.parentWidget() or self
        try:
            _org = self.mapTo(_host, QPoint(0, 0))
            _ox, _oy = int(_org.x()), int(_org.y())
        except Exception:
            _ox, _oy = 0, 0
        _b = getattr(self, "_ph_badge_rect", None)
        _shown = bool(not self._lines)
        _rect_local = ([round(float(_b.x()), 2), round(float(_b.y()), 2),
                        round(float(_b.width()), 2), round(float(_b.height()), 2)]
                       if _b is not None else None)
        _rect_abs = ([round(float(_b.x()) + _ox, 2), round(float(_b.y()) + _oy, 2),
                      round(float(_b.width()), 2), round(float(_b.height()), 2)]
                     if _b is not None else None)
        return {"R": float(PH_BADGE_R), "canvas": float(PH_BADGE_CANVAS),
                "outer_diameter": 2.0 * float(PH_BADGE_R),
                "diameter": 2.0 * float(PH_BADGE_R),     # 兼容 task-46 口径（= 外圈直径 44）
                "padding": (float(PH_BADGE_CANVAS) / 2.0 - float(PH_BADGE_R) - 1.0), "stroke": 2.0,
                "stroke_pen_device": round(2.0 * _d, 2), "dpr": _d,
                "shown": _shown, "shown_when": "not self._lines（本曲无歌词 ⇒ 占位符态）",
                "hidden_when": "有歌词时走歌词分支，徽章不绘制（与有没有封面无关）",
                "n_lines": len(self._lines),
                "panel_origin_in_host": [_ox, _oy], "host": type(_host).__name__,
                "coord_space": "rect_panel_absolute = host(AudioView) 局部坐标；与 left_layout() 同空间",
                "rect_panel_local": _rect_local, "rect_panel_absolute": _rect_abs,
                "center_panel_absolute": ([round(_rect_abs[0] + _rect_abs[2] / 2.0, 2),
                                           round(_rect_abs[1] + _rect_abs[3] / 2.0, 2)]
                                          if _rect_abs else None),
                "recipe": "播一个**没有 .lrc** 的曲目（build/qa/media/nolrc_meta.mp3）⇒ shown=true；"
                          "随后 av.grab() 后按 rect_panel_absolute ×dpr 取样即可命中徽章",
                "placeholder_state": self.placeholder_state(),
                "groove_radii": [16.0, 12.0],       # ★ 绝对半径（task-50② 起不再随 R 推导）
                "arc_span_deg": 60.0, "arc_starts_deg": [330.0, 150.0],
                "center_ring_r": 6.0, "center_hole_r": 2.0,
                "alpha": float(PH_BADGE_ALPHA), "alpha_u8": int(round(PH_BADGE_ALPHA * 255)),
                "glyph": "headphones_soundwave",
                "hp_band_r": float(PH_HP_BAND_R), "hp_band_w": float(PH_HP_BAND_W),
                "hp_band_cy": float(PH_HP_BAND_CY),
                "hp_cup_dx": float(PH_HP_CUP_DX), "hp_cup_cy": float(PH_HP_CUP_CY),
                "hp_cup_w": float(PH_HP_CUP_W), "hp_cup_h": float(PH_HP_CUP_H),
                "hp_wave_dx": float(PH_HP_WAVE_DX), "hp_wave_w": float(PH_HP_WAVE_W),
                "hp_wave_h": list(PH_HP_WAVE_H), "hp_wave_cy": float(PH_HP_WAVE_CY),
                "symmetry": "mirror_x_about_canvas_center",
                "path_kind": "discrete_primitives(task-75)",
                "band_rect": [14.0 + PH_ICON_DX, 14.0 + PH_ICON_DY, 36.0, 36.0], "band_w": 2.5,
                "icon_offset": [float(PH_ICON_DX), float(PH_ICON_DY)],
                "cup_rects": [[11.0 + PH_ICON_DX, 30.0 + PH_ICON_DY, 7.0, 16.0],
                              [46.0 + PH_ICON_DX, 30.0 + PH_ICON_DY, 7.0, 16.0]], "cup_radius": 3.5,
                "wave_bars": [[26.0, 9.0], [32.0, 15.0], [38.0, 9.0]],
                "note_removed": False,                # ★ task-63：双八分音符回来了（不再是同心圆靶子）
                "concentric_target": False,
                "ring_r": float(PH_RING_R), "ring_w": float(PH_RING_W),
                "note_heads": [[float(PH_NOTE_HEAD_A[0]), float(PH_NOTE_HEAD_A[1])],
                               [float(PH_NOTE_HEAD_B[0]), float(PH_NOTE_HEAD_B[1])]],
                "note_head_rx": float(PH_NOTE_HEAD_RX), "note_head_ry": float(PH_NOTE_HEAD_RY),
                "stem_w": float(PH_NOTE_STEM_W), "beam_w": float(PH_NOTE_BEAM_W),
                "stem_tops_y": [float(PH_NOTE_STEM_TOP_A), float(PH_NOTE_STEM_TOP_B)],
                "ring": dict(getattr(self, "_vinyl_ring", {}) or {}),
                "pixmap_keys": [[str(x) for x in kk] for kk in
                                (getattr(self, "_ph_badge_pm", {}) or {}).keys()]}

    def hover_state(self) -> dict:
        """★ task-41 锚点②：悬停/光标可读状态（QA 读 `QApplication.overrideCursor()` 拿不到时用它）。"""
        shape = None
        try:
            from PySide6.QtWidgets import QApplication
            # ⚠️ PySide6 的 `shape()` 返回枚举，`int()` 会抛 TypeError —— 直接比较枚举。
            c = QApplication.overrideCursor()
            if c is None:
                shape = "ArrowCursor"          # 无 override ⇒ 系统箭头
            else:
                sh = c.shape()
                shape = "PointingHandCursor" if sh == Qt.PointingHandCursor else \
                    ("ArrowCursor" if sh == Qt.ArrowCursor else str(sh))
        except Exception:
            shape = None
        return {"cursor_shape": shape, "cursor_overridden": bool(self._cursor_over),
                "capsule_hover": bool(self._tbtn_hover),
                "capsule_highlight": bool(self._tbtn_hover),
                "capsule_visible": bool(self._tbtn_idx >= 0 and not self._tbtn_rect.isEmpty()),
                "capsule_rect": [round(self._tbtn_rect.x(), 2), round(self._tbtn_rect.y(), 2),
                                 round(self._tbtn_rect.width(), 2), round(self._tbtn_rect.height(), 2)],
                "pressed": bool(self._capsule_pressed)}

    def capsule_probe(self, gx: int, gy: int) -> dict:
        """★ task-41 驱动锚点①（QA 用）：在**全局坐标**模拟"移到胶囊 → 左键按下 → 松开"，
        走**与真实点击完全相同**的 `mouse_owns()` 逻辑，返回
        `{hit, seek_ms, seek_count_before/after, manual_after, cursor, consumed}`。
        ⚠️ 目标是歌词面板内的 `_tbtn_rect`（需先进入手动浏览让它出现）；
        不是底部 ControlBar 的 ProgressSlider。
        """
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QMouseEvent
        tl = self.mapToGlobal(QPoint(0, 0))
        loc = QPointF(float(gx) - tl.x(), float(gy) - tl.y())
        gp = QPointF(float(gx), float(gy))
        before = int(getattr(self, "_seek_emit_count", 0))
        hit = self.capsule_hit(gx, gy)
        consumed = False
        for kind, typ, btns in (("move", QEvent.MouseMove, Qt.NoButton),
                                ("press", QEvent.MouseButtonPress, Qt.LeftButton),
                                ("release", QEvent.MouseButtonRelease, Qt.NoButton)):
            ev = QMouseEvent(typ, loc, gp, Qt.LeftButton, btns, Qt.NoModifier)
            try:
                ev.setAccepted(False)      # ⚠️ 与 task-36 同一个坑：默认 accepted
            except Exception:
                pass
            consumed = bool(self.mouse_owns(kind, ev)) or consumed
        after = int(getattr(self, "_seek_emit_count", 0))
        st = self.hover_state()
        return {"hit": bool(hit), "consumed": bool(consumed),
                "seek_ms": int(self._tbtn_ms) if getattr(self, "_tbtn_ms", None) is not None else -1,
                "seek_count_before": before, "seek_count_after": after,
                "seek_count": after - before,
                "manual_after": bool(self._manual_scroll),
                "cursor": st["cursor_shape"], "capsule_rect": st["capsule_rect"]}

    def _wheel_inside(self, ev) -> bool:
        """★ task-36（用户 ⑥）：光标是否落在**本面板的物理几何矩形**内。

        用**全局坐标**判定（不依赖控件命中测试）：面板可能被同尺寸覆盖层/父级绘制，
        `QApplication.widgetAt()` 不一定返回本面板；卡片判据要求"面板内一律归歌词、
        面板外 1~2px 归音量"，所以几何判定必须用 mapToGlobal + 本控件 size。
        """
        try:
            gp = ev.globalPosition().toPoint()
        except Exception:
            gp = QCursor.pos()
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size()).contains(gp)

    def wheelEvent(self, ev):            # noqa: N802
        # ★★ task-36（用户 ⑥）：**根因修复** —— 旧实现在"无歌词"与"水平滚轮"时 `ev.ignore()`，
        #   事件于是冒泡到 MainWindow.wheelEvent ⇒ 被当成全局音量（用户反馈的"滚歌词变成调音量"）。
        #   现在：**面板物理矩形内无条件 accept**（有歌词则平滑滚动，没有歌词也不改音量），
        #   矩形外才 ignore ⇒ 交回主窗口调音量（封面/底栏/边缘行为不变）。
        if self.wheel_owns(ev):
            return
        ev.ignore()                      # 面板外：交回主窗口调音量（封面/底栏/边缘行为不变）

    def _apply_wheel(self, ev) -> None:
        """面板内滚轮的实际动作（有歌词才滚动；无歌词/纯水平滚轮只吞事件不改音量）。"""
        dy = float(ev.angleDelta().y())
        if self._lines and dy:
            self._enter_manual()         # 判据 A：进入手动浏览 ⇒ 自动跟随暂停
            # ★★ task-41④：滚轮**只累加目标**，真正的位移由 16ms 主帧钟按
            #   `_offset += (target-_offset)*0.25` 逐帧逼近 ⇒ 无整格跳变、可给逐帧序列。
            step = (dy / 120.0) * LYRIC_WHEEL_STEP * max(24.0, self._pitch or 48.0)
            self._manual_target_offset = self._offset + step \
                if self._manual_target_offset == 0.0 else self._manual_target_offset + step
            lo, hi = self._scroll_bounds()
            self._manual_target_offset = max(lo, min(hi, self._manual_target_offset))
            # ★ task-60：滚轮路径清掉陈旧 hover ⇒ 焦点/胶囊跟随**黄金线**（用户要的"滚到顶选第一句"）
            self._hover_idx = -1
            self._refresh_focus()
            self.update()

    def wheel_owns(self, ev) -> bool:
        """★ task-36：面板物理矩形内的滚轮**无条件归歌词**；返回 True 表示"已消费，别外传"。

        为什么不能只靠 `wheelEvent`：实测本窗口里歌词面板**上方有覆盖层**（`QApplication.widgetAt`
        在面板中心返回的是一个挂在 Stage 下的普通 QWidget）⇒ 事件根本不会送到本面板，
        而是冒泡到 MainWindow 被当成全局音量（用户反馈的根因）。所以 AudioView 另装了
        应用级事件过滤器，命中本矩形时直接调用本方法并 `return True` 掐断派发。
        """
        if not self._wheel_inside(ev):
            return False
        self._apply_wheel(ev)
        ev.accept()                      # ★ 面板内**无条件接受**（严禁冒泡到父级/Stage）
        return True

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
        self._seek_emit_count += 1          # ★ 锚点：seek 恰好 1 次（QA 读 capsule_probe）
        self.exit_manual()
        self.request_seek.emit(ms)

    # ---- task-26①：歌词样式的运行时入口（设置面板唯一的施加点）------------------------
    def apply_style(self, family=None, base_px=None, weight=None, step_pad=None,
                    scroll_ms=None) -> None:
        """把「音乐与歌词」设置施加到运行时：改完**立刻**重排 + 重绘（无需重启）。

        · family：字体族名（"" / "system" = 回到自动挑选）
        · base_px：当前行字号基准（16~28）；非当前行 = 20/27×基准，**译文 = 原句×76%**（task-105）
        · weight：regular / medium / heavy（未唱行 400/500/700；当前行 700/700/900）
        · step_pad：**视觉行间距 LINE_GAP**（10~48px；键名沿用旧配置键，语义 = task-27 的 LINE_GAP）
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
            # ★ task-105：译文字号 = 原句 × 76%（单一真源；旧 18/14 写死值已删除）
            # ★ task-120：译文用**绝对字号**（当前 19 / 未唱 15.5），按主词基准等比缩放
            self._trans_cur = max(9.0, round(LYRIC_TRANS_CUR_PX * (px / LYRIC_SIZE_CUR), 2))
            self._trans_other = max(8.0, round(LYRIC_TRANS_OTHER_PX * (self._size_other / LYRIC_SIZE_OTHER), 2))
        if weight is not None:
            w = str(weight)
            if w == "regular":
                self._weight_noncur, self._weight_cur = QFont.Normal, QFont.Bold
            elif w == "heavy":
                self._weight_noncur, self._weight_cur = QFont.Bold, QFont.Black
            else:
                # ★ task-115（卡片 ④）：默认档 未唱 **DemiBold 600** / 当前 Bold 700
                #   （task-23 封版是 500/700；600 与译文 Medium 500 拉开一档 ⇒ "一眼可分" ✓）
                self._weight_noncur, self._weight_cur = QFont.DemiBold, QFont.Bold
        if step_pad is not None:
            # ★ task-27：配置键 lyric.step_pad 的语义 = **LINE_GAP（墨迹视觉行间距）**，默认 18
            try:
                self._gap = max(LYRIC_GAP_MIN, min(LYRIC_GAP_MAX, float(step_pad)))
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
        """★ task-26/27 锚点：歌词样式的**生效值**（给 QA/门禁读，避免只认 config）。

        fonts_px / weights 是字体对象里**真正生效**的像素字号与字重（前 6 行）。
        step / step_pad 是**兼容旧门禁的派生读数**（= pitch / gap，不存第二份状态）；
        几何权威读数见 layout_snapshot()。
        """
        idx = self._idx if 0 <= self._idx < len(self._font_main) else -1
        return {"family": (self._font_main[0].family() if self._font_main else self._family),
                "family_override": self._family_override,
                "base_px": round(float(self._size_cur), 2),
                "size_other": round(float(self._size_other), 2),
                "size_cur_active": (int(self._font_main[idx].pixelSize()) if idx >= 0 else -1),
                "weight_active": (int(self._font_main[idx].weight()) if idx >= 0 else -1),
                "weight_noncur": int(self._weight_noncur),
                # ★ task-105：译文侧生效值（字体族/字号/比例）—— 探针不必再靠私有字段猜
                "trans_family": (self._font_trans[0].family() if self._font_trans
                                 else self._trans_family()),
                "trans_family_override": getattr(self, "_trans_family_override", ""),
                # ★ task-115：`trans_family` 是**生效值**（字体对象真实族名）、`override` 是**设置值**，
                #   两者分开 ⇒ 门禁"切雅黑↔幼圆"时读这两个字段即可判定是否真的重排生效 ✓
                "trans_family_effective": (self._font_trans[0].family() if self._font_trans else ""),
                "trans_px": int(round(float(self._trans_cur))),
                "trans_px_other": int(round(float(self._trans_other))),
                "trans_ratio": float(LYRIC_TRANS_RATIO),
                "trans_alpha_ratio": float(LYRIC_TRANS_ALPHA),
                "trans_weight": (int(self._font_trans[0].weight()) if self._font_trans else -1),
                "fonts_px": [int(f.pixelSize()) for f in self._font_main[:6]],
                "weights": [int(f.weight()) for f in self._font_main[:6]],
                "step": round(float(self._pitch), 2),         # 旧键（= pitch，兼容旧门禁）
                "step_pad": round(float(self._gap), 2),       # 旧键（= gap，兼容旧门禁）
                "pitch": round(float(self._pitch), 2),        # 相邻**句盒顶**间距中位
                "gap": round(float(self._gap), 2),            # ★ task-27：墨迹视觉行间距
                "box_pad": round(float(LYRIC_BOX_PAD), 2),    # 每块盒的上下余量合计
                "buf_dpr": round(float(self._buf_dpr), 3),    # ★ task-27①：离屏缓冲设备像素比
                "scroll_ms": int(round(float(self._scroll_s) * 1000)),
                "tops_head": [round(float(x), 2) for x in self._tops[:4]],
                "n": len(self._lines), "idx": idx}

    def layout_snapshot(self) -> dict:
        """★ task-27 锚点：**逐块排版快照**（QA/门禁直接读这个，不必摸私有字段）。

        每块给：盒顶/盒高、每视觉行的 (子串, 基线 y, 墨迹顶, 墨迹底)——墨迹坐标是**绝对 y**
        （相对歌词内容原点）。判据「等距 / 零截断 / 长行不被挤出」都能只用这份数据复核。
        """
        blocks = []
        for i in range(len(self._lines)):
            if i >= len(self._tops):
                break
            top = float(self._tops[i])                 # 分配盒顶
            ink0 = float(self._ink_tops[i]) if i < len(self._ink_tops) else top
            rows = []
            for kind, arr in (("main", self._main_rows[i] if i < len(self._main_rows) else []),
                              ("trans", self._trans_rows[i] if i < len(self._trans_rows) else [])):
                for rt, by, it, ib in arr:
                    rows.append({"kind": kind, "text": rt,
                                 "baseline": round(ink0 + by, 2),
                                 "ink_top": round(ink0 + it, 2),
                                 "ink_bottom": round(ink0 + ib, 2)})
            # ★ task-32② 取证锚点：本块主词/译文的**断点累计字符下标**
            def _brk(arr):
                out, acc = [], 0
                for rt, _by, _it, _ib in arr:
                    acc += len(rt)
                    out.append(acc)
                return out[:-1]      # 末元素 = 全串长度（不是断点）
            blocks.append({"i": i, "top": round(top, 2),
                           "breaks_main": _brk(self._main_rows[i] if i < len(self._main_rows) else []),
                           "breaks_trans": _brk(self._trans_rows[i] if i < len(self._trans_rows) else []),
                           "height": round(float(self._heights[i]), 2),
                           "main_h": (round(float(self._main_h[i]), 2)
                                      if i < len(self._main_h) else 0.0),
                           "trans_h": (round(float(self._trans_h[i]), 2)
                                       if i < len(self._trans_h) else 0.0),
                           "rows": rows})
        return {"gap": round(float(self._gap), 2), "box_pad": round(float(LYRIC_BOX_PAD), 2),
                # ★ task-33 rider：投影开关 + 缓存规模（QA 一次 eval 读到，做开/关差分）
                "shadow_enabled": bool(getattr(self, "_shadow_on", True)),
                "shadow_cache": len(getattr(self, "_shadow_cache", {}) or {}),
                "shadow_blur_px": float(LYRIC_SHADOW_BLUR), "shadow_alpha": int(LYRIC_SHADOW_A),
                "wrap_ref_px": float(LYRIC_SIZE_CUR),          # ★ task-32②：基准断点字号
                "wrap_font_cur": round(float(self._size_cur), 2),
                "min_tail": int(LYRIC_MIN_TAIL),
                "pitch": round(float(self._pitch), 2), "buf_dpr": round(float(self._buf_dpr), 3),
                "content_width": self._content_width(),
                "scale_ratio": round(float(self._size_scale), 3),
                "n": len(self._lines), "idx": int(self._idx), "blocks": blocks}

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
        # ①.5 ★ task-44②：间奏圆点亮度（随主帧钟 16ms 更新 ⇒ 平滑呼吸；绘制期零测量）
        if self._update_interlude():
            changed = True
        # ② 滚动插值（320ms OutCubic）
        # ★★ task-24 判据 A：手动浏览期间**暂停自动跟随** —— offset 只由滚轮驱动，
        #    播放推进（_retarget 由 set_position_ms 触发）不会把视图拽走。这里还必须
        #    **停掉已排队的滚动动画**（_scroll_t0 清零），否则滚轮一动就被旧动画拉回去。
        if self._manual_scroll:
            self._scroll_t0 = 0.0
            # ★★ task-41④：滚轮只写 `_manual_target_offset`，这里**逐帧一阶逼近**
            #   `_offset += (target − _offset) × 0.25` ⇒ 平滑无整格跳变（可给逐帧序列）。
            d = self._manual_target_offset - self._offset
            if abs(d) > 0.01:
                self._offset += d * LYRIC_WHEEL_LERP
                lo, hi = self._scroll_bounds()
                self._offset = max(lo, min(hi, self._offset))
                self._scroll_from = self._offset
                changed = True
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
        # ③ 羽化缓冲层维护（尺寸或 DPR 变化时重建；正常帧零分配）
        if (self._buf_size != (self.width(), self.height())
                or abs(float(self._buf_dpr) - self._dpr_now()) > 1e-3):
            self._ensure_buffer()
            self._place_placeholder()      # ★ task-35：面板尺寸变了 ⇒ 占位符锚点重算（布局期）
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
        # ★★ task-27①：**高分屏矢量清晰度** —— 离屏缓冲必须按设备像素建：
        #    QPixmap(int(w*dpr), int(h*dpr)) + setDevicePixelRatio(dpr)。
        #    这样 QPainter 的坐标仍是逻辑坐标，但光栅化发生在**设备像素**上
        #    ⇒ drawText 是原生 DPI 的矢量光栅化，**不存在 1x 渲染再放大**。
        #    旧实现建的是逻辑尺寸 pixmap（dpr=1），再被主画布按 dpr 放大 ⇒ 中文/译文发糊。
        dpr = self._dpr_now()
        self._buf_dpr = dpr
        self._buf = QPixmap(max(1, int(round(w * dpr))), max(1, int(round(h * dpr))))
        self._buf.setDevicePixelRatio(dpr)
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

    def _dpr_now(self) -> float:
        """当前设备像素比（离屏缓冲与主画布必须同 DPR ⇒ 文字原生光栅化，不放大）。"""
        try:
            return max(1.0, float(self.devicePixelRatioF() or 1.0))
        except Exception:
            return 1.0

    @staticmethod
    def _main_text_color(light: bool, cur: bool, alpha: float) -> QColor:
        """★ task-120：主词**中性色**（当前/未唱 × 深/浅），乘行 alpha。"""
        try:
            key = (("light" if light else "dark") + ("" if cur else "_dim"))
            rgb, a0 = LYRIC_MAIN_COL[key]
            c = QColor(rgb[0], rgb[1], rgb[2])
            c.setAlphaF(max(0.0, min(1.0, float(a0) * float(alpha))))
            return c
        except Exception:
            return QColor(255, 255, 255)

    @staticmethod
    def _trans_text_color(light: bool, cur: bool, ta: float) -> QColor:
        """★ task-120：译文**冷色**（深色 115,210,255 / 155,195,235；浅色 0,102,204 / 45,90,145）。"""
        try:
            key = (("light" if light else "dark") + ("" if cur else "_dim"))
            rgb, a0 = LYRIC_TRANS_COL[key]
            c = QColor(rgb[0], rgb[1], rgb[2])
            c.setAlphaF(max(0.0, min(1.0, float(a0) * min(1.0, float(ta) / max(1e-6, LYRIC_TRANS_ALPHA)))))
            return c
        except Exception:
            return QColor(155, 195, 235)

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
        if (self._buf_size != (w, h)
                or abs(float(self._buf_dpr) - self._dpr_now()) > 1e-3):
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

    def _place_interlude(self) -> None:
        """★ task-44②：三枚圆点的几何 —— 放在**当前高亮句与下一句之间的行距留白**正中。

        坐标是**内容坐标**（与 `_tops/_heights` 同一空间）；绘制时加 `_offset`（与参考线同口径）。
        全部在布局期算好 ⇒ 绘制期零测量零分配（红线）。
        """
        self._dots_rect = None
        self._dot_centers = []
        self._il_row_top = None
        self._il_row_h = 0.0
        if not self._interlude_active:      # 未激活 ⇒ 不预留几何（负例必须"什么都没有"）
            return
        n = len(self._tops)
        if n == 0:
            return
        row_h = float(getattr(self, "_il_row_h_static", IL_DOT_ROW_H))
        gap_row = float(LYRIC_LINE_GAP)     # 与正常歌词**同一个** LINE_GAP
        if self._in_prelude():
            if not getattr(self, "_il_static_prelude", False):
                return                       # ★ task-57①：非静态（首句 <5s）⇒ 一行都不排
            row_top = float(self._tops[0]) - gap_row - row_h
        else:
            i = self._idx
            if not (0 <= i < n - 1) or i not in getattr(self, "_il_static_rows", set()):
                return                       # ★ 只有静态行（间隔 ≥15s）才允许出现圆点
            a = float(self._tops[i]) + float(self._heights[i])
            b = float(self._tops[i + 1])
            row_top = (a + b) / 2.0 - row_h / 2.0        # ★ 独立行居中于两句之间
        cy = row_top + row_h / 2.0
        r = float(IL_DOT_R)
        # ★★ task-91（用户 11）：**圆点与歌词共用同一条左基线 x=10** ——
        #   圆心 = LYRIC_LEFT_PAD + r ⇒ **第一颗圆点左边缘 == 歌词墨迹左缘 == 10px** ✓
        #   （歌词笔位原点已同样右移到 LYRIC_LEFT_PAD）。
        cx0 = float(LYRIC_LEFT_PAD) + float(r)
        self._il_row_top = row_top
        self._il_row_h = row_h
        # `dots_rect` = **独立行**的矩形（面板绝对坐标口径，与 task-44 一致）
        self._dots_rect = QRectF(0.0, row_top, float(self.width()), row_h)
        self._dot_centers = [[cx0 + k * float(IL_DOT_GAP), cy, r, int(IL_DOT_IDLE)] for k in range(3)]

    def _update_interlude(self) -> bool:
        """★ task-44②：主帧钟里更新间奏进度与三枚圆点的亮度（左→右依次点亮）。返回是否变化。"""
        # ★★ task-57①（用户 57）：**不再 `_relayout()`** —— 圆点行是静态占位，
        #   这里只切换"当前是不是这行"的状态与进度（播放中途零插拔 ⇒ 其它行 y 不动 ✓）。
        changed = False
        now_ms = int(getattr(self, "_now_ms", 0))
        n = len(self._lines)
        pre = self._in_prelude()
        in_row = False
        p = 0.0
        if pre and getattr(self, "_il_static_prelude", False):
            in_row = True
            first = max(1, int(self._lines[0][0]))
            p = max(0.0, min(1.0, float(now_ms) / float(first)))
        elif n and int(self._idx) in getattr(self, "_il_static_rows", set()):
            i = int(self._idx)
            line_ms = int(self._lines[i][0])
            nxt = int(self._lines[i + 1][0])
            if (now_ms - line_ms) > int(IL_SUNG_TRIGGER_MS):
                in_row = True
                p = max(0.0, min(1.0, float(now_ms - line_ms) / float(max(1, nxt - line_ms))))
        if bool(in_row) != bool(self._interlude_active):
            self._interlude_active = bool(in_row)
            # 进/出这行时把**相位锚点**挪到当前位置（相位只随播放前进 ⇒ 暂停冻结 ✓）
            self._il_phase_ms0 = now_ms
            self._il_phase_s = 0.0
            self._place_interlude()
            changed = True
        if abs(p - float(self._interlude_p)) > 0.001:
            self._interlude_p = float(p)
            changed = True
        # ⚠️ 原来这里是 `if act:`（task-57 改名后成了**未定义变量** ⇒ 每帧 NameError ⇒
        #    相位/闪烁整段不执行 ✗ 自检抓到：phase 恒 0、alpha 恒 0.25）。
        if in_row:
            # ★★ task-49③（用户 3）：**红绿灯**模型 ——
            #   · k < filled：已走过 ⇒ **100% 实心常亮**；
            #   · k == filled：**当前蓄力那颗** ⇒ 以 2.5Hz 正弦在 alpha 0.25~0.85 间闪烁；
            #   · k > filled：还没轮到 ⇒ 暗底（0.25）。
            #   蓄满瞬间 `filled` 进位 ⇒ 上一颗立刻转实心、下一颗开始闪烁（判据 e）。
            filled = int(float(p) * 3.0)
            if filled > 3:
                filled = 3
            # ★★ task-57③（用户 57）：相位由**累积播放时间**驱动 ——
            #   `phase = (now_ms − 锚点ms)/1000`，暂停时 `now_ms` 不动 ⇒ **100% 定格** ✓
            #   （原来用 `perf_counter()` ⇒ 暂停后照闪 ✗，用户报的"暂停还在呼吸"就是这个）。
            import math as _m
            self._il_phase_s = max(0.0, (now_ms - int(getattr(self, "_il_phase_ms0", now_ms))) / 1000.0)
            _ph = _m.sin(2.0 * _m.pi * float(IL_BLINK_HZ) * float(self._il_phase_s))
            blink = float(IL_BLINK_MIN) + (float(IL_BLINK_MAX) - float(IL_BLINK_MIN)) * (0.5 + 0.5 * _ph)
            self._il_filled = filled
            self._il_blink_alpha = blink
            for k in range(len(self._dot_centers)):
                if k < filled:
                    a = int(IL_DOT_SOLID)
                elif k == filled:
                    a = int(round(255.0 * blink))
                else:
                    a = int(IL_DOT_IDLE)
                if int(self._dot_centers[k][3]) != a:
                    self._dot_centers[k][3] = a
                    changed = True
                self._dot_centers[k][2] = float(IL_DOT_R)
            # ★ 判据 d 的 `alpha_series`：环缓冲（时间, 蓄力点 alpha, filled）
            try:
                self._il_alpha_series.append([round(_t, 4), round(blink, 4), int(filled)])
                if len(self._il_alpha_series) > 240:
                    del self._il_alpha_series[:120]
            except Exception:
                pass
        return changed

    def _place_placeholder(self) -> None:
        """★ task-35②：占位符几何（**布局期**算好，绘制期零测量零构造）。

        锚点（卡片①）：
          · 水平：`pen_x` = **右栏正常歌词的左缘**（歌词行就是 `drawText(QPointF(0, …))`，
            所以笔位原点同为 0；再按字形 leftBearing 做一次微补偿，使**墨迹左缘**对齐）；
          · 垂直：整块中心 `cy = top + PH_Y_RATIO × height`。
        结构（自上而下）：矢量徽标（直径 2R）→ PH_GAP_BADGE → 主文案 18 Medium → PH_GAP_SUB → 副标题。
        """
        w = float(max(1, self.width()))
        h = float(max(1, self.height()))
        fm_m = QFontMetrics(self._ph_font or QFont())
        fm_s = QFontMetrics(self._ph_sub_font or QFont())
        main_h = float(fm_m.height())
        # ★ task-50②：副标题（英文行）已删除 ⇒ 高度按 0 计（单行中文提示）
        sub_h = float(fm_s.height()) if str(PH_SUB_TEXT).strip() else 0.0
        block_h = float(PH_BADGE_CANVAS) + PH_GAP_BADGE + main_h + (PH_GAP_SUB + sub_h if sub_h else 0.0)
        cy = h * PH_Y_RATIO                              # ★ 卡片①：垂直 40%
        top = cy - block_h / 2.0
        badge = QRectF(0.0, top, float(PH_BADGE_CANVAS), float(PH_BADGE_CANVAS))
        main_top = top + float(PH_BADGE_CANVAS) + PH_GAP_BADGE
        sub_top = main_top + main_h + PH_GAP_SUB
        # ★ 墨迹左缘对齐：歌词行笔位 = 0，其墨迹左缘 = leftBearing(首字, 歌词字号)；
        #   占位符同样按 leftBearing 回推笔位 ⇒ 两侧**墨迹**左缘一致（≤1px）。
        pen_x = 0.0
        try:
            ref = QFont(self._ph_font or QFont())
            ref.setPixelSize(max(9, int(round(float(self._size_cur or 27.0)))))
            ref.setWeight(self._weight_cur)
            ch_l = ((self._lines[0][1] if self._lines else "") or "歌")[:1]
            ch_p = (self._placeholder or "纯")[:1]
            pen_x = max(0.0, float(QFontMetrics(ref).leftBearing(ch_l))
                        - float(fm_m.leftBearing(ch_p)))
        except Exception:
            pen_x = 0.0
        self._ph_main_rect = QRectF(pen_x, main_top, max(40.0, w - pen_x), main_h)
        self._ph_sub_rect = QRectF(pen_x, sub_top, max(40.0, w - pen_x), sub_h)
        self._ph_badge_rect = QRectF(pen_x, badge.top(), float(PH_BADGE_CANVAS), float(PH_BADGE_CANVAS))
        self._ph_center_y = cy
        self._ph_block_h = block_h
        self._ph_pen_x = pen_x
        # ★ 布局期**预热**徽标贴图（浅/深两版）⇒ 绘制期只 drawPixmap，绝不构造 QPixmap
        try:
            self._badge_pixmap(True)
            self._badge_pixmap(False)
        except Exception:
            log.debug("徽标预热失败", exc_info=True)

    def placeholder_state(self) -> dict:
        """★ task-35 可读锚点：占位符几何/字体/透明度（QA 逐条对账用）。"""
        f = self._ph_font or QFont()
        fs = self._ph_sub_font or QFont()
        m = getattr(self, "_ph_main_rect", QRectF())
        b = getattr(self, "_ph_badge_rect", QRectF())
        s = getattr(self, "_ph_sub_rect", QRectF())
        return {"visible": not self._lines, "text": self._placeholder,
                "sub_text": PH_SUB_TEXT, "w": int(self.width()), "h": int(self.height()),
                "pen_x": round(float(getattr(self, "_ph_pen_x", 0.0)), 2),
                "lyric_pen_x": 0.0, "x_delta": round(float(getattr(self, "_ph_pen_x", 0.0)), 2),
                "center_y": round(float(getattr(self, "_ph_center_y", 0.0)), 2),
                "y_ratio": round(float(getattr(self, "_ph_center_y", 0.0)) / max(1.0, float(self.height())), 4),
                "want_y_ratio": PH_Y_RATIO, "block_h": round(float(getattr(self, "_ph_block_h", 0.0)), 2),
                "main": {"rect": [round(m.x(), 2), round(m.y(), 2), round(m.width(), 2), round(m.height(), 2)],
                         "px": int(f.pixelSize()), "weight": int(f.weight())},
                # ★ task-55 rider（Lead）：**平铺可读键**（QA 直接读，避免嵌套键读到 null ✗）
                "main_px": int(f.pixelSize()),
                "main_weight": int(f.weight()),
                "main_alpha": float(LYRIC_ALPHA_UNSUNG),
                "main_alpha_u8": int(round(float(LYRIC_ALPHA_UNSUNG) * 255.0)),
                "badge": {"rect": [round(b.x(), 2), round(b.y(), 2), round(b.width(), 2), round(b.height(), 2)],
                          "alpha": PH_BADGE_ALPHA, "alpha_255": int(round(PH_BADGE_ALPHA * 255)),
                          "vector": True, "r": PH_BADGE_R},
                "sub": {"rect": [round(s.x(), 2), round(s.y(), 2), round(s.width(), 2), round(s.height(), 2)],
                        "px": int(fs.pixelSize()), "weight": int(fs.weight())},
                "sub_smaller": int(fs.pixelSize()) < int(f.pixelSize())}

    def _badge_pixmap(self, light: bool) -> QPixmap:
        """★ task-35②（QA 口径修正）：徽标先**预合成到一张不透明 Pixmap**，绘制时用
        `setOpacity(PH_BADGE_ALPHA)` 一次贴图 ⇒ 重叠笔画**不会叠加 alpha**（旧写法
        外圈∩内圈∩音符重叠处实测 α=0.51 ✗ 超卡片 ≤0.35）。布局期缓存，绘制期只 drawPixmap。
        """
        # ★ task-50②：缓存键含画布尺寸 ⇒ 换画布/半径自动重建
        key = (round(PH_BADGE_R, 1), round(float(PH_BADGE_CANVAS), 1),
               round(float(self._dpr_now()), 2), bool(light))
        cache = getattr(self, "_ph_badge_pm", None)
        if cache is None:
            cache = self._ph_badge_pm = {}
        pm = cache.get(key)
        if pm is not None:
            return pm
        dpr = max(1.0, float(self._dpr_now()))
        side = max(8, int(round(float(PH_BADGE_CANVAS) * dpr)))     # ★ 画布 64（四周 8px 内边距）
        pm = QPixmap(side, side)
        pm.fill(Qt.transparent)
        pm.setDevicePixelRatio(dpr)
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing, True)
        # ★★ task-91（用户 11）：耳机图标**左移 6px、下移 8px**，与文本视觉重心对齐。
        #   放在 QPainter 平移里做 ⇒ 弧/耳罩/声波三组图元**整体移动**，相对关系不变 ✓
        q.translate(float(PH_ICON_DX), float(PH_ICON_DY))
        col = self._col(light, 1.0)                # 满 alpha 绘制；真正的透明度靠贴图时 setOpacity
        r = QRectF(0.0, 0.0, float(PH_BADGE_CANVAS), float(PH_BADGE_CANVAS))
        cx, cy = r.center().x(), r.center().y()                     # 画布中心（32）
        q.setBrush(Qt.NoBrush)
        # ★★ task-75①（用户 10）：**弃用单一闭合拼接 Path，改分立图元** ——
        #   用户原话："耳罩与头梁连接处笔画交叉溢出"。上一版把所有图元 `united()` 成一个
        #   闭合 Path，接缝处仍有笔画互相插入 ⇒ 放大看就是"交叉溢出" ✗。
        #   现在按卡片参数**逐图元独立绘制**，并用**绘制顺序**实现精确遮盖：
        #   先画弧（端点落在耳罩矩形内）→ 再画**实心耳罩**盖住弧端 ⇒ 可见交叉 = 0 ✓。
        # ① 头梁：上半圆弧（卡片：QRectF(14,14,36,36)、0°→180°、线宽 2.5、圆头）
        q.setBrush(Qt.NoBrush)
        q.setPen(QPen(col, 2.5, Qt.SolidLine, Qt.RoundCap))
        q.drawArc(QRectF(14.0, 14.0, 36.0, 36.0), 0, 180 * 16)
        # ② 左右耳罩：两个**独立**圆角胶囊（卡片坐标 11/46、宽 7、高 16、圆角 3.5）
        q.setPen(Qt.NoPen)
        q.setBrush(QBrush(col))
        q.drawRoundedRect(QRectF(11.0, 30.0, 7.0, 16.0), 3.5, 3.5)
        q.drawRoundedRect(QRectF(46.0, 30.0, 7.0, 16.0), 3.5, 3.5)
        # ③ 中心声波：3 根**严格对称**的垂直圆角短柱，x = 26 / 32 / 38（卡片给定）
        for _bx, _hh in ((26.0, 9.0), (32.0, 15.0), (38.0, 9.0)):
            q.drawRoundedRect(QRectF(float(_bx) - 1.6, 38.0 - float(_hh) / 2.0, 3.2, float(_hh)),
                              1.6, 1.6)
        q.setBrush(Qt.NoBrush)
        q.end()
        cache[key] = pm
        return pm

    @staticmethod
    def _capsule_path(cx: float, cy: float, w: float, h: float) -> QPainterPath:
        """以 (cx,cy) 为中心的**胶囊**路径（w<h ⇒ 圆角半径 = w/2 ⇒ 两端半圆，无直角）。"""
        pth = QPainterPath()
        _r = min(float(w), float(h)) / 2.0
        pth.addRoundedRect(QRectF(float(cx) - float(w) / 2.0, float(cy) - float(h) / 2.0,
                                  float(w), float(h)), _r, _r)
        return pth

    def _paint_badge(self, p, r: QRectF, light: bool) -> None:
        """★ task-35②：贴预合成徽标（一次 setOpacity ⇒ 实测 α ≤ 0.35）；零测量零构造。"""
        pm = self._badge_pixmap(light)
        p.setOpacity(PH_BADGE_ALPHA)
        p.drawPixmap(r.topLeft(), pm)
        p.setOpacity(1.0)

    def _paint_content(self, p, w: int, h: int, light: bool):
        if not self._lines:
            # ★★ task-35②：占位符 = 矢量徽标 + 18 Medium 主文案 + 英文小字副标题
            #   全部几何来自 `_place_placeholder()`（布局期）⇒ 绘制期零测量；
            #   左缘锚定歌词笔位、垂直中心 = top + 0.40×height（卡片①）。
            if not getattr(self, "_ph_main_rect", None):
                self._place_placeholder()
            self._paint_badge(p, self._ph_badge_rect, light)
            # ★★ task-50②（用户 2）：单行中文提示，颜色/alpha 与**歌词未唱行一致**（0.575）
            p.setFont(self._ph_font or QFont())
            p.setPen(self._col(light, LYRIC_ALPHA_UNSUNG))
            p.drawText(self._ph_main_rect, Qt.AlignLeft | Qt.AlignVCenter, self._placeholder)
            if str(PH_SUB_TEXT).strip():            # 英文副标题已删除 ⇒ 空串时不画
                p.setFont(self._ph_sub_font or QFont())
                p.setPen(self._col(light, 0.46))
                p.drawText(self._ph_sub_rect, Qt.AlignLeft | Qt.AlignVCenter, PH_SUB_TEXT)
            if self._spec_text:
                f2 = self._spec_font or QFont()
                p.setFont(f2)
                # ★ 宽度用**预排版缓存**（set_spec_text 里算好），绘制期零测量
                tw = self._spec_w or 24.0
                rect = QRectF(self._ph_pen_x, self._ph_sub_rect.bottom() + 10.0, tw, 26)
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
        p.setClipRect(QRectF(0, 0, w, h))     # 只裁面板边界（行内文字绝不用矩形裁剪）
        n_main = len(self._font_main)
        for i, (_t, text) in enumerate(self._lines):
            if i >= len(self._tops):
                break
            box_top = self._offset + self._tops[i]          # 分配盒（命中/裁剪用）
            hh = self._heights[i]
            if box_top + hh < -FADE_PX or box_top > h + FADE_PX:
                continue
            # ★ 绘制原点 = 首行**墨迹顶**（盒顶比它高 margin_t；两者混用会把整行画歪）
            top = self._offset + (self._ink_tops[i] if i < len(self._ink_tops) else self._tops[i])
            cur = (i == self._idx)
            # ★ task-23：字体（族/字重/**插值后的字号**）全部来自预建缓存 ⇒ 绘制期零构造零测量
            f = self._font_main[i] if i < n_main else QFont()
            p.setFont(f)
            alpha = self._alpha_now[i] if i < len(self._alpha_now) else (1.0 if cur else 0.40)
            # ★ task-24 判据 B：手动浏览时**焦点行（悬停/中心）高亮**
            if self._manual_scroll and i == self._focus_idx:
                alpha = max(alpha, LYRIC_FOCUS_ALPHA)
            # ★★ task-27②：逐**视觉行**按预排版基线绘制（drawText(QPointF, 子串) ⇒ 原生矢量
            #   光栅化、不被矩形裁剪 ⇒ 零截断；行位置来自布局期的墨迹锚定，绘制期零测量）
            rows = self._main_rows[i] if i < len(self._main_rows) else []
            # ★★ task-32①（用户 ②⑧）：**浅色档彻底删除白色底衬/白描边**。
            #   旧写法在浅色下给当前行画 `QColor(255,255,255,120α)` 偏移 (1.2,1.2) —— 那正是
            #   用户说的"歌词背后白色狗皮膏药"。现在浅色档统一改用**文字微投影**：
            #   `QColor(0,0,0,35)`、偏移 **(0,1)**、**水平零扩散**（字间/字周像素与背景差 = 0）；
            #   暗化只出现在文字下沿 1px 内（卡片判据），且对**每一行**生效（不只当前行）。
            #   ⚠️ 取舍：卡片另写"模糊 2px"，但真做 2px 高斯会把暗化扩散到 2~3px，
            #   与同一张卡的"字周区域与背景差 ≤6"判据冲突 ⇒ 取 (0,1) 单层，报告里已写明。
            #   深色档**保持原样**（黑描边 90α，QA 已验证，不在本卡范围）。
            if not light and cur and alpha > 0.55:
                p.setPen(QColor(0, 0, 0, int(90 * alpha)))
                for rt, by, _it, _ib in rows:
                    p.drawText(QPointF(1.2, top + by + 1.2), rt)
            if alpha > 0.05 and self._shadow_on:
                # ★★ task-32①（Lead 裁决）+ task-45②：贴**预烘焙的 2px 高斯投影**
                #   （布局期生成，绘制期零测量）；task-45 起**所有行**（含深色档）都吃这条管线，
                #   贴图不透明度 = 行 alpha ⇒ **阴影浓度按行 alpha 比例缩放**（判据可测）。
                shs = self._shadow_main[i] if i < len(self._shadow_main) else []
                p.setOpacity(max(0.0, min(1.0, float(alpha))))
                for (_rt, by, _it, _ib), sh in zip(rows, shs):
                    if not sh:
                        continue
                    pm, dx, dy = sh
                    p.drawPixmap(QPointF(dx + float(LYRIC_LEFT_PAD), top + by + dy + LYRIC_SHADOW_DY), pm)
                p.setOpacity(1.0)
            # ★★ task-120：主词**中性色**（深色 #FFFFFF / 未唱 rgba(255,255,255,0.70)；
            #   浅色 #12161F / 未唱 rgba(18,22,31,0.68)），再乘行 alpha 做淡入淡出
            p.setPen(self._main_text_color(light, cur, alpha))
            for rt, by, _it, _ib in rows:
                p.drawText(QPointF(float(LYRIC_LEFT_PAD), top + by), rt)   # ★ task-91 左缘统一 x=10
            trows = self._trans_rows[i] if i < len(self._trans_rows) else []
            if trows and i < len(self._font_trans):
                # ★★ task-45②：译文不再用 0.28 的暗比例 —— 改成"按行 alpha 比例" ⇒
                #   未唱行译文 = 0.575 × 0.835 = **0.48**（判据 [0.46,0.50]），当前行仍 0.90。
                # ★ task-105：译文不透明度 = 原句 **78%**（旧 0.90/0.835 两档已作废，单一真源）
                ta = LYRIC_TRANS_ALPHA * max(0.0, float(alpha))
                # ★ task-105 判据 d：**浅色零底层黑影** —— 浅底/玻璃上译文一律不画投影；
                #   深色档保留极柔底影（LYRIC_TRANS_SHADOW_A=20 / BLUR=2.6 未动）。
                if self._shadow_on and not light:
                    tsh = self._shadow_trans[i] if i < len(self._shadow_trans) else []
                    p.setOpacity(max(0.0, min(1.0, float(ta))))
                    for (_rt, by, _it, _ib), sh in zip(trows, tsh):
                        if not sh:
                            continue
                        pm, dx, dy = sh
                        p.drawPixmap(QPointF(dx + float(LYRIC_LEFT_PAD), top + by + dy + LYRIC_SHADOW_DY), pm)
                    p.setOpacity(1.0)
                # ★★ task-120：**左侧竖条 ▎（3px 圆角、x=12、行高 70%）** + 译文冷色 + 从 x=22 起绘
                try:
                    _bar = self._trans_text_color(light, cur, ta)
                    for (_rt2, by2, it2, ib2) in trows:
                        _h2 = max(4.0, (float(ib2) - float(it2)) * float(LYRIC_BAR_H_RATIO))
                        _cy = top + float(by2) - float(it2 if False else 0.0)
                        _bar_rect = QRectF(float(LYRIC_BAR_X), float(top) + float(it2) + (float(ib2) - float(it2) - _h2) / 2.0,
                                           float(LYRIC_BAR_W), _h2)
                        p.setPen(Qt.NoPen)
                        _bc = QColor(_bar)
                        _bc.setAlphaF(min(1.0, _bc.alphaF() * 0.92))
                        p.setBrush(_bc)
                        p.drawRoundedRect(_bar_rect, float(LYRIC_BAR_W) / 2.0, float(LYRIC_BAR_W) / 2.0)
                    p.setBrush(Qt.NoBrush)
                except Exception:
                    log.debug("绘制译文竖条失败", exc_info=True)
                p.setFont(self._font_trans[i])
                p.setPen(self._trans_text_color(light, cur, ta))
                for rt, by, _it, _ib in trows:
                    # ★ task-120：译文从 **x=22** 起绘（让开 x=12 的竖条 ▎）
                    p.drawText(QPointF(float(LYRIC_TRANS_X), top + by), rt)
        # ★★ task-44②（用户 ⑨）：句间长间奏的 • • • 呼吸指示器 ——
        #   几何来自 `_place_interlude()`（布局期），亮度来自 `_update_interlude()`（主帧钟）
        #   ⇒ 绘制期零测量零分配。触发不满足时 `_interlude_active=False` ⇒ 一个像素都不画。
        if self._interlude_active and self._dot_centers:
            # ★★ task-49③（用户 3）：红绿灯渲染 —— 实心/蓄力闪烁/暗底三态；
            #   光晕 alpha 与圆点 alpha 成比例（≥0.25 就画），半径 11 ≤ 安全边距 12
            #   ⇒ 圆点外扩 12px 内**不会**被任何行盒边界切成方框（判据 c）。
            for (cx, cy, rr, aa) in self._dot_centers:
                _a = int(aa)
                if _a > int(IL_DOT_IDLE):
                    halo = QRadialGradient(QPointF(cx, cy + self._offset), float(IL_HALO_R))
                    base = QColor(255, 255, 255) if not light else QColor(20, 22, 26)
                    c0 = QColor(base)
                    c0.setAlpha(int(0.32 * _a))
                    c1 = QColor(base)
                    c1.setAlpha(0)
                    halo.setColorAt(0.0, c0)
                    halo.setColorAt(1.0, c1)
                    p.setPen(Qt.NoPen)
                    p.setBrush(QBrush(halo))
                    p.drawEllipse(QPointF(cx, cy + self._offset),
                                  float(IL_HALO_R), float(IL_HALO_R))
                col = QColor(255, 255, 255) if not light else QColor(24, 25, 28)
                col.setAlpha(_a)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(col))
                p.drawEllipse(QPointF(cx, cy + self._offset), float(rr), float(rr))
        # ★★ task-24（用户 ⑩②）：手动浏览态 —— 极淡水平辅助线 + 「▶ mm:ss」时间胶囊。
        #    全部数值（文本/矩形/字体）都是**交互期算好的缓存** ⇒ 绘制期依旧零测量零构造。
        if self._manual_scroll and self._focus_idx >= 0:
            # ★★ task-41③：**分段参考线** —— 只在 [歌词文本右缘+16, 胶囊左缘−12] 之间画，
            #   不穿字、不刺穿胶囊（区间在 `_refresh_focus()` 里量好 ⇒ 绘制期零测量）。
            # ⚠️⚠️ 自检实测：只在 `_refresh_focus()` 里定死 seg 是不够的 —— 字号动画
            #   （250ms OutCubic）期间歌词变宽，`_text_right` 会长大 ⇒ 旧 seg 会**退到文字里**
            #   （实测 seg[0]=250 < text_right+16=283，参考线穿字）。
            #   这里用**缓存值重新夹一次**（纯算术，不做任何测量 ⇒ 绘制期零测量红线不破）。
            if self._tbtn_idx >= 0 and not self._tbtn_rect.isEmpty():
                x0 = float(self._text_right) + LYRIC_GUIDE_GAP_TEXT
                x1 = float(self._tbtn_rect.left()) - LYRIC_GUIDE_GAP_CAPSULE
                if (x1 - x0) >= 2.0 and x1 > 0.0:
                    self._guide_seg = (x0, x1)      # 同步给锚点（QA 读到的与画出来的一致）
                    p.setPen(QPen(QColor(20, 22, 26, 52) if light else QColor(255, 255, 255, 38), 1.0))
                    p.drawLine(QPointF(x0, self._guide_y), QPointF(x1, self._guide_y))
            if self._tbtn_idx >= 0 and not self._tbtn_rect.isEmpty():
                r = self._tbtn_rect
                bg = LYRIC_TBTN_LIGHT_BG if light else LYRIC_TBTN_DARK_BG
                bd = LYRIC_TBTN_LIGHT_BORDER if light else LYRIC_TBTN_DARK_BORDER
                fg = LYRIC_TBTN_LIGHT_TEXT if light else LYRIC_TBTN_DARK_TEXT
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(*bg))
                p.drawRoundedRect(r, r.height() / 2.0, r.height() / 2.0)
                # 悬停：描边加粗 + 提亮（可读锚点见 hover_state()）
                bw = LYRIC_TBTN_HOVER_BORDER if self._tbtn_hover else 1.0
                ba = int(bd[3]) if self._tbtn_hover else int(bd[3])
                if self._tbtn_hover:
                    ba = min(255, int(bd[3] * 1.8) + 20)
                p.setPen(QPen(QColor(bd[0], bd[1], bd[2], ba), bw))
                p.setBrush(Qt.NoBrush)
                inset = bw / 2.0
                p.drawRoundedRect(r.adjusted(inset, inset, -inset, -inset),
                                  (r.height() - bw) / 2.0, (r.height() - bw) / 2.0)
                p.setPen(QColor(*fg))
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
        # 左栏三行的预排版结果：[(_info_slots 几何+字体+alpha), (_info_lines 逐行绘制指令)]
        self._info_slots: list = []
        self._info_lines: list = []
        self._left_col = None
        # ★ task-30：版式锚点（QA 直接读，不用猜私有元组）
        self._left_col_w = 0           # 左栏可用宽度（封面占比的分母）
        self._cover_side = 0           # 封面**可见**边长
        self._cover_visible_left = 0   # 可见封面左边缘（信息块左对齐基准）
        self._info_left = 0
        self._info_w = 0
        self._title_lines: list = []
        self._sub_raw = ""
        self._artist = ""
        self._album = ""
        self.lyrics = LyricsPanel(self)
        # ★ task-24：歌词时间胶囊的跳转请求 ⇒ 音频层唯一 seek 入口（探针在此计数）
        self.lyrics.request_seek.connect(self._on_lyric_seek)
        self.spec = SpectrumWidget(self)
        # ★★ task-55（用户 13）：宽屏律动音瀑（**默认关**；由设置项 music.cascade 打开）
        self.cascade = CascadeOverlay(self)
        self._cascade_rect = None
        try:
            self.cascade.set_enabled(bool(self._cfg_get_cascade()))
        except Exception:
            log.debug("读取音瀑开关失败", exc_info=True)
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
        # ★★ task-36：装上"歌词滚轮抢回"的应用级过滤器（面板上方有覆盖层，光靠 wheelEvent 拿不到）
        self._wheel_filter_on = False
        self._install_wheel_filter()

    def wheel_probe(self, gx: int, gy: int, dy: int = 120) -> dict:
        """★ task-36 **可读驱动锚点**（QA 用）：在**全局坐标 (gx,gy)** 合成一个真实
        `QWheelEvent` 并送给"光标下那个控件"，再按 Qt 的语义**逐级上抛**（未接受才给父级），
        最后回读 offset / 音量 / 接受位。

        为什么需要它：本窗口里 `AudioView` 带 `WA_TransparentForMouseEvents`，歌词面板**不在
        命中链上**（`QApplication.widgetAt()` 在面板中心返回的是 Stage 下的覆盖层）⇒ 直接给
        面板 `sendEvent` 不能复现真实路径。本函数走的就是**应用级过滤器**那条真实路径。
        返回：`{target, accepted, handled, offset_before/after, volume_before/after,
                offset_delta, volume_delta, inside_lyrics}`
        """
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QApplication
        gp = QPoint(int(gx), int(gy))
        cw = self.window()
        cap = getattr(getattr(cw, "capsule", None), "volume", None)
        v0 = float(cap.value()) if cap is not None else None
        o0 = round(float(self.lyrics._offset), 3)
        tgt = QApplication.widgetAt(gp) or cw
        ev = QWheelEvent(QPointF(tgt.mapFromGlobal(gp)), QPointF(float(gx), float(gy)),
                         QPoint(0, 0), QPoint(0, int(dy)), Qt.NoButton, Qt.NoModifier,
                         Qt.ScrollUpdate, False)
        ev.setAccepted(False)      # ⚠️ QWheelEvent **默认就是 accepted**：不复位的话
        #   "未接受才上抛"的循环一次都不会跑（QA/我都被这条坑过：d_offset/d_vol 全 0）
        handled = False
        node = tgt
        chain = []
        while node is not None and not ev.isAccepted():
            chain.append(type(node).__name__)
            QApplication.sendEvent(node, ev)
            if ev.isAccepted():
                handled = True
                break
            node = node.parentWidget()
        v1 = float(cap.value()) if cap is not None else None
        o1 = round(float(self.lyrics._offset), 3)
        return {"target": type(tgt).__name__, "chain": chain[:4], "accepted": bool(ev.isAccepted()),
                "handled": handled, "inside_lyrics": bool(self.lyrics._wheel_inside(ev)),
                "offset_before": o0, "offset_after": o1, "offset_delta": round(o1 - o0, 3),
                "volume_before": v0, "volume_after": v1,
                "volume_delta": (round(v1 - v0, 3) if (v0 is not None and v1 is not None) else None)}

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
            if eng is not None:
                v = eng.cached("pause", None)
                if v is not None:
                    return bool(v)
        except Exception:
            log.debug("读 mpv pause 失败，回落暂停意图", exc_info=True)
        # ★ task-29：引擎属性不可用/未缓存时，回落**UI/IPC 的暂停意图**（MainWindow._paused，
        #   播放键与 set pause 两条路都会写它）。只靠"位置停滞 >1.5s"猜的话，中途暂停后的
        #   这 1.5s 里频谱仍在逐帧演化 —— 用户看到的"暂停了频谱还在动"就是这个窗口。
        try:
            v = getattr(self.window(), "_paused", None)
            return None if v is None else bool(v)
        except Exception:
            return None

    def _on_frame(self):
        idle = False
        # ★★ task-122①：**唯一的 16ms 帧钟里**把 mpv 的精确播放时间喂给节奏舞台
        #   （卡面红线：时间源必须是 mpv `time-pos`，不许 QElapsedTimer 估算；
        #    也**不新起定时器** ⇒ QTimer 仍是 2）
        try:
            _rh = getattr(self, "rhythm", None)
            if _rh is not None and _rh.isVisible():
                _eng = self._engine_obj()
                _tp = None
                _paused = False
                if _eng is not None:
                    # ★ task-122②：优先 mpv **高精度** `playback-time`，回退 time-pos
                    for _name in ("playback-time", "time-pos"):
                        try:
                            _v = _eng.get(_name, 0, None)
                        except Exception:
                            _v = None
                        if _v is not None:
                            _tp = _v
                            break
                    try:
                        _paused = str(_eng.get("pause", 0, None)).lower() in ("yes", "true")
                    except Exception:
                        _paused = False
                if _tp is not None:
                    _rh.set_time(float(_tp), _paused)
        except Exception:
            log.debug("喂节奏舞台时间失败", exc_info=True)
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
            # ★★ task-65（用户 3）：**每次换文件都重置能量管线** —— 用 mpv 的 `path` 变化做触发
            #   （视频→音乐、切歌都会换 path）⇒ 重挂 af 滤镜 + 清自适应包络分母。
            #   放在 A 链自己的帧钟里 ⇒ 不依赖 main_window 的加载回调（写域不交叉 ✓）。
            try:
                eng2 = self._engine_obj()
                cur_path = None
                if eng2 is not None:
                    cur_path = eng2.get("path", 0, None)
                if cur_path != getattr(self, "_last_media_path", None):
                    self._last_media_path = cur_path
                    self.rearm_energy("path_changed")
            except Exception:
                log.debug("能量管线重置失败", exc_info=True)
            # ★★ task-55：音瀑跟随**同一条帧钟**（不新增 QTimer ⇒ 判据 f 的"帧钟 2"不变）
            self._drive_cascade(bool(idle))
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
            elif getattr(self.spec, "_playing", playing) != playing:
                # ★ task-29 防御：控件态与视图态**必须始终同源**。只有"变化时同步"的话，
                #   任何一处单独调过 spec.set_playing(...) 后就会永久分叉（暂停了频谱还在动）。
                self.spec.set_playing(playing)
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
        self._sub = self._sub_raw = sub or ""
        # ★ task-30：解析出（歌手, 专辑）并**去掉前缀**；再按新歌名重排版（含 2 行折行）。
        self._artist, self._album = self._parse_meta(sub or "")
        # ★ task-21 纪律：所有文本度量都发生在"布局/数据变化"时刻，paintEvent 只 drawText。
        self._place_left_column()
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
            self._place_left_column()          # ★ task-30：规格回填 ⇒ 重排第三行
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
    # ---- ★★ task-36：歌词滚轮的"抢回"通道（应用级事件过滤器）----------------------
    def _install_wheel_filter(self) -> None:
        """把本控件装成**应用级事件过滤器**（只关心 Wheel）。装一次，幂等。

        ⚠️ 面板上方有覆盖层 ⇒ 光靠 `LyricsPanel.wheelEvent` 拿不到事件；过滤器在事件到达
        目标控件**之前**看到它，命中歌词面板矩形就消费掉 ⇒ 不会再冒泡成音量。
        """
        if getattr(self, "_wheel_filter_on", False):
            return
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)
                self._wheel_filter_on = True
        except Exception:
            log.debug("安装滚轮过滤器失败", exc_info=True)

    def eventFilter(self, obj, ev):      # noqa: N802
        """★ task-36（用户 ⑥）：**歌词面板物理矩形内的滚轮一律归歌词**。

        返回 True ⇒ 事件被消费，不再往下派发（MainWindow/Stage 的音量处理看不到它）。
        面板外/设置面板·抽屉等覆盖层之上 ⇒ 返回 False，音量等原有行为**完全不变**。
        """
        try:
            from PySide6.QtCore import QEvent
            if ev.type() == QEvent.Wheel and self.lyrics is not None:
                if self._overlay_owns_wheel(ev):
                    return False
                if self.lyrics.wheel_owns(ev):
                    return True
            # ★★ task-41①（用户 ②）：**时间胶囊点不动**的根因与滚轮同源 ——
            #   `AudioView.WA_TransparentForMouseEvents` 让整个子树脱离命中链，
            #   QA 实测 `widgetAt(胶囊中心) == None` ⇒ 点击根本到不了面板。
            #   这里在**事件到达目标控件之前**按"胶囊物理矩形"判定并消费：
            #   悬停 ⇒ 手型+高亮；左键按下/松开 ⇒ seek 恰好 1 次 + 立即退出手动态。
            if self.lyrics is not None and ev.type() in (QEvent.MouseButtonPress,
                                                         QEvent.MouseButtonRelease,
                                                         QEvent.MouseMove):
                kind = {QEvent.MouseButtonPress: "press",
                        QEvent.MouseButtonRelease: "release",
                        QEvent.MouseMove: "move"}[ev.type()]
                # ★★ task-47①（用户 3）：**遮挡守卫必须最先判**（在胶囊/滚轮等一切逻辑之前）
                try:
                    _gp = ev.globalPosition().toPoint()
                except Exception:
                    _gp = QCursor.pos()
                _gx, _gy = int(_gp.x()), int(_gp.y())
                if (not self.lyrics._in_panel_pt(_gx, _gy)) or self._mouse_occluded(_gx, _gy):
                    # ★★ task-47②：离开面板 / 上方有遮挡 ⇒ **立即恢复箭头**并收掉悬停高亮
                    #   （否则 PointingHand 会残留到别的控件上 = 用户说的"光标卡死"）
                    if self.lyrics._cursor_over or self.lyrics._tbtn_hover or kind == "move":
                        self.lyrics.force_arrow_cursor()
                    return False
                # 性能护栏：没有胶囊时（常态）移动事件不做任何工作
                if kind == "move" and self.lyrics._tbtn_idx < 0 and not self.lyrics._tbtn_hover:
                    return False
                if not self._overlay_owns_wheel(ev) and self.lyrics.mouse_owns(kind, ev):
                    return True
        except Exception:
            log.debug("滚轮过滤器异常", exc_info=True)
        return super().eventFilter(obj, ev)

    # ---- ★★ task-47①（用户 3）：**顶层命中与遮挡守卫** --------------------------------
    #   抽屉展开后手柄移到 `x = W-392-16`，**正好落进底层 LyricsPanel 的矩形**；task-41 的
    #   过滤器无脑拦截"落在面板内的鼠标事件"⇒ 手柄点不动、光标卡手型。
    #   这里在过滤器**最开头**判"上方是否盖着别的可见控件"，是则**立即放行**。
    def _occluder_widgets(self) -> list:
        """可能盖在歌词面板之上的顶层控件（逐个 getattr 解析：缺失/改名都不报错）。"""
        out = []
        try:
            w = self.window()
        except Exception:
            w = None
        st = getattr(w, "stage", None)
        for holder, name in ((st, "drawer_handle"), (st, "drawer_host"), (st, "capsule_host"),
                             (st, "settings_host"), (w, "drawer"), (w, "capsule"),
                             (w, "titlebar"), (w, "control_bar"), (w, "bottom_bar"),
                             (w, "settings"), (w, "_settings_panel"), (w, "settings_panel")):
            try:
                o = getattr(holder, name, None) if holder is not None else None
            except Exception:
                o = None
            if o is not None:
                out.append(o)
        return out

    @staticmethod
    def _pt_in_widget(o, gx: int, gy: int) -> bool:
        try:
            if o is None or not o.isVisible():
                return False
            tl = o.mapToGlobal(QPoint(0, 0))
            return QRect(tl, o.size()).contains(QPoint(int(gx), int(gy)))
        except Exception:
            return False

    def _mouse_occluded(self, gx: int, gy: int) -> bool:
        """★ task-47①：该点是否落在"盖在歌词面板之上"的可见控件内。"""
        for o in self._occluder_widgets():
            if self._pt_in_widget(o, gx, gy):
                return True
        return False

    @staticmethod
    def _overlay_owns_wheel(ev) -> bool:
        """光标下的最上层控件是不是**设置面板/抽屉**（它们要自己滚）。

        只看类名（避免跨模块 import）：SettingsPanel / PlaylistDrawer / 其子控件。
        """
        try:
            from PySide6.QtWidgets import QApplication
            gp = ev.globalPosition().toPoint()
            wd = QApplication.widgetAt(gp)
        except Exception:
            return False
        while wd is not None:
            if type(wd).__name__ in ("SettingsPanel", "PlaylistDrawer", "QScrollArea", "QScrollBar"):
                return True
            wd = wd.parentWidget()
        return False

    # ---- ★★ task-55：宽屏律动音瀑 -------------------------------------------------
    def _cfg_get_cascade(self) -> bool:
        """读设置"music.cascade"（**默认关**；配置缺失/异常一律 False）。"""
        try:
            cfg = getattr(self, "cfg", None)
            if cfg is None:                     # 宿主（MainWindow）持有 cfg
                try:
                    cfg = getattr(self.window(), "cfg", None)
                except Exception:
                    cfg = None
            if cfg is None:
                return False
            v = cfg.get("music.cascade", False)
            return bool(v)
        except Exception:
            return False

    def set_cascade_enabled(self, on: bool) -> None:
        """开启/关闭音瀑（设置面板与启动路径都走这里）。

        ★ task-122：同时启停 **音瀑节奏舞台**（新建 `ui/rhythm_stage.py`）——
        开 ⇒ 建舞台 + 后台线程跑 `media/beatmap.py` 生成谱面；关 ⇒ 隐藏 + 停线程。"""
        try:
            self.cascade.set_enabled(bool(on))
            self._cascade_geometry()
        except Exception:
            log.debug("音瀑开关失败", exc_info=True)
        try:
            self.set_rhythm_enabled(bool(on))
        except Exception:
            log.debug("音瀑节奏舞台开关失败", exc_info=True)

    # ---- ★ task-122：音瀑节奏舞台（最小接线） --------------------------    def set_rhythm_enabled(self, on: bool) -> None:
        """启停节奏舞台：建/隐舞台 + 启后台谱面线程（不阻塞 UI）。"""
        try:
            if not on:
                if getattr(self, "rhythm", None) is not None:
                    self.rhythm.set_events([])
                    self.rhythm.hide()
                return
            if getattr(self, "rhythm", None) is None:
                from .rhythm_stage import RhythmStage
                self.rhythm = RhythmStage(self)
                self.rhythm.setGeometry(0, 0, self.width(), self.height())
            self.rhythm.show()
            self.rhythm.raise_()
            self._start_beatmap()
        except Exception:
            log.debug("启停节奏舞台失败", exc_info=True)

    def _start_beatmap(self) -> None:
        """若还没有谱面 ⇒ 起后台线程（ffmpeg 解 PCM ⇒ 事件表）。"""
        if getattr(self, "_beat_events", None):
            if getattr(self, "rhythm", None) is not None:
                self.rhythm.set_events(self._beat_events)
            return
        # ⚠️ `engine.get` 的签名是 `get(name, fmt=0, default=None)` —— 只传 name 时拿不到值，
        #   表现为"无可用媒体路径"✗（真机实测 error）；这里显式带 default，并回退帧钟缓存。
        path = ""
        try:
            _eng = self._engine_obj()
            if _eng is not None:
                path = str(_eng.get("path", 0, None) or "")
        except Exception:
            path = ""
        if not path:
            path = str(getattr(self, "_last_media_path", "") or "")
        if not path or not os.path.isfile(path):
            self._beatmap_error = "无可用媒体路径"
            return
        try:
            from ..media.beatmap import BeatmapWorker
            w = BeatmapWorker(path, self)
            w.done.connect(self._on_beatmap_ready)
            w.failed.connect(lambda msg: setattr(self, "_beatmap_error", str(msg)[:120]))
            self._beatmap_worker = w
            w.start()
        except Exception as exc:  # noqa: BLE001
            self._beatmap_error = str(exc)[:120]
            log.debug("启动谱面线程失败", exc_info=True)

    def _on_beatmap_ready(self, events) -> None:
        self._beat_events = list(events or [])
        if getattr(self, "rhythm", None) is not None:
            self.rhythm.set_events(self._beat_events)

    def refresh_rhythm_geometry(self) -> None:
        """★ task-122：窗口 resize / 抽屉开合后**重算节奏舞台几何**（防音符错位）。

        ⚠️ 轨道坐标本身在 `paintEvent` 里按 `width/4` **实时**算，没有缓存 ⇒ 只要几何同步，
        口径就一致；本方法供主窗/抽屉事件调用，也可由 resizeEvent 自动触发。"""
        try:
            _rh = getattr(self, "rhythm", None)
            if _rh is not None:
                _rh.setGeometry(0, 0, self.width(), self.height())
                _rh.update()
        except Exception:
            log.debug("刷新节奏舞台几何失败", exc_info=True)

    def rhythm_debug(self) -> dict:
        """★ task-122 探针锚点：谱面统计 + 过线偏差 + 时间源状态。"""
        out = {"enabled": bool(getattr(self, "rhythm", None) is not None
                             and self.rhythm.isVisible()),
               "error": getattr(self, "_beatmap_error", "")}
        try:
            from ..media.beatmap import stats as _bm_stats
            out.update(_bm_stats(getattr(self, "_beat_events", []) or []))
        except Exception:
            pass
        try:
            if getattr(self, "rhythm", None) is not None:
                out["stage"] = self.rhythm.rhythm_debug()
        except Exception:
            pass
        return out

    def _cascade_geometry(self) -> None:
        """把音瀑放在**右栏右侧空白区**（宽不足 1320 时隐藏 —— 由 `_sync_visible` 决定）。"""
        ov = getattr(self, "cascade", None)
        if ov is None:
            return
        w, h = int(self.width()), int(self.height())
        x = int(w * 0.76)
        r = [x, 24, max(0, w - x - 18), max(0, h - 48)]
        if r != getattr(self, "_cascade_rect", None):
            self._cascade_rect = r
            ov.setGeometry(r[0], r[1], r[2], r[3])

    def _drive_cascade(self, idle: bool) -> None:
        ov = getattr(self, "cascade", None)
        if ov is None or not getattr(ov, "_enabled", False):
            return
        self._cascade_geometry()
        try:
            flux = float(getattr(self.spec, "_flux_norm", 0.0))
        except Exception:
            flux = 0.0
        ms = int(float(getattr(self, "_position", 0.0)) * 1000.0)
        playing = bool(getattr(self, "_playing", False)) and not bool(idle) \
            and not bool(getattr(self, "_stopped", False))
        ov.on_frame(float(FRAME_MS) / 1000.0, flux, ms, playing)

    def cascade_state(self) -> dict:
        """★ task-55 锚点：一次 eval 读全（QA 判据 a~f 用）。"""
        ov = getattr(self, "cascade", None)
        st = dict(ov.state()) if ov is not None else {"enabled": False}
        try:
            from PySide6.QtCore import QTimer as _QT
            st["timers"] = len(self.findChildren(_QT))
        except Exception:
            st["timers"] = None
        st["frame_ms"] = int(FRAME_MS)
        st["geom"] = list(getattr(self, "_cascade_rect", []) or [])
        st["cfg_key"] = "music.cascade"
        st["host_w"] = int(self.width())
        return st

    def apply_music_setting(self, key: str, value) -> bool:
        """按键实时生效（不重建控件、不等"应用"）。返回是否被本入口消费。

        ⚠️ 返回 False ⇒ 设置面板会回退到主窗口的 _on_setting_changed 兜底（不静默丢设置）。
        """
        k = str(key)
        try:
            if k == "lyric.font_family":
                self.lyrics.apply_style(family=value)
            elif k in ("lyric.trans_font", "lyric.trans_font_family"):
                # ★ task-115 补接线：设置面板写的是 **`lyric.trans_font_family`**，而这里原来只认
                #   `lyric.trans_font` ⇒ **能存、不生效** ✗（用户"切了没反应"的真因）。
                #   两个键同义都吃；空值/`system`/`跟随系统` ⇒ `apply_trans_family()` 内部
                #   清空 override ⇒ 回退自动挑选（YouYuan → Microsoft YaHei UI）✓
                self.lyrics.apply_trans_family(value)
            elif k == "lyric.font_px":
                self.lyrics.apply_style(base_px=value)
            elif k == "lyric.weight":
                self.lyrics.apply_style(weight=value)
            elif k == "lyric.step_pad":
                self.lyrics.apply_style(step_pad=value)
            elif k == "lyric.scroll_ms":
                self.lyrics.apply_style(scroll_ms=value)
            elif k == "music.cascade":       # ★ task-55：宽屏律动音瀑（默认关）
                self.set_cascade_enabled(bool(value))
            elif k == "spectrum.enabled":
                self.spec.set_enabled(bool(value))
            elif k == "spectrum.peak_style":
                self.spec.set_peak_style(str(value))
            elif k == "spectrum.rhythm":
                # ★ task-33②：「频谱律动速度」三档（舒缓/标准/激进）—— 实时生效
                if not self.spec.set_rhythm(value):
                    log.debug("未知的频谱律动档位 %r，已回落标准档", value)
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
        # ★ task-122：节奏舞台跟随尺寸（轨道坐标在 paint 里按 width/4 重算 ⇒ 这里同步几何即可）
        try:
            _rh = getattr(self, "rhythm", None)
            if _rh is not None:
                _rh.setGeometry(0, 0, self.width(), self.height())
        except Exception:
            log.debug("节奏舞台跟随尺寸失败", exc_info=True)

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

    @staticmethod
    def _parse_meta(sub: str) -> tuple:
        """把 main_window 传来的 "歌手：X    专辑：Y" 拆成 **(歌手, 专辑)** 并**去掉前缀**。

        ★ task-30（用户 ⑥）：界面上不许再出现「歌手：」「专辑：」前缀。main_window 那侧
        仍按老格式拼接（本卡写范围只有 audio_view.py）⇒ 在**唯一的入口** set_meta 里解析。
        兼容：无前缀、只有歌手、歌手/专辑以多空格或 · 分隔、带中英文冒号。
        """
        import re as _re
        parts = [p.strip() for p in _re.split(r"\s{2,}|[·/|]", sub or "") if p.strip()]
        artist = album = ""
        for p in parts:
            if p.startswith(("歌手：", "歌手:", "艺术家：", "艺术家:")):
                artist = _re.sub(r"^(歌手|艺术家)\s*[:：]\s*", "", p).strip()
            elif p.startswith(("专辑：", "专辑:", "唱片：", "唱片:")):
                album = _re.sub(r"^(专辑|唱片)\s*[:：]\s*", "", p).strip()
            elif "：" not in p and ":" not in p:
                if not artist:
                    artist = p
                elif not album:
                    album = p
        return artist, album

    def _info_source_texts(self) -> tuple:
        """★ task-34①：左栏四行的**源文本**（歌名 / 歌手 / 专辑 / 规格），**不含前缀**。

        前缀由 `_place_left_column` 按卡片拼（`歌手: ` / `专辑: `）；规格沿用既有数据源
        _badge_text（set_audio_specs → badge._text，与 ffprobe 同源），缺失退回 _spec_raw。
        专辑为空 ⇒ 调用方**整行隐藏**（不占位）。
        """
        spec = (self._badge_text or getattr(self, "_spec_raw", "") or "").strip()
        return (self._title or "", self._artist or "", self._album or "", spec)

    @staticmethod
    def _wrap_text(text: str, fm, width: int, max_lines: int = INFO_TITLE_MAX_LINES) -> list:
        """**布局期**把文本折成最多 max_lines 行（逐字符贪心，中英文通吃）。

        ★ task-30：歌名最多 2 行；**超 2 行才**在最后一行省略号（前 max_lines-1 行完整）。
        绝不产生第 3 行 ⇒ 不可能溢出左栏。
        """
        if not text:
            return [""]
        try:
            if fm.horizontalAdvance(text) <= width:
                return [text]
        except Exception:
            return [text]
        lines, cur, i = [], "", 0
        while i < len(text):
            if len(lines) == max_lines - 1:
                try:
                    lines.append(fm.elidedText(cur + text[i:], Qt.ElideRight, width))
                except Exception:
                    lines.append((cur + text[i:])[:40])
                return lines
            ch = text[i]
            if not cur or fm.horizontalAdvance(cur + ch) <= width:
                cur += ch
                i += 1
            else:
                lines.append(cur)
                cur = ""
        if cur:
            lines.append(cur)
        return lines[:max_lines]

    def _make_info_fonts(self, sr: float) -> tuple:
        """★ task-34①：**四行**字体（歌名/歌手/专辑/规格），随 scale_ratio 弹性放大。

        ⚠️ 字号**不随 sr 缩到卡片值以下**（否则矮窗口会量出 22px 之类的非卡片数）：
        基准窗口 sr=1 ⇒ 严格 24/14/14/12（卡片口径）。
        """
        def _mk(px: int, weight) -> QFont:
            f = QFont()
            f.setPixelSize(max(px, int(round(px * sr))))
            f.setWeight(weight)
            return f
        # ★★ task-45①（用户 ②）：二级三行**字重统一 Medium 500**（原：album/spec 是 Regular 400）
        #   —— 与「三行像素 RGB/Alpha 完全一致」配套，否则字重差会让可读性/观感仍分三级。
        return (_mk(INFO_TITLE_PX, QFont.Bold),        # 24 Bold 700（一级）
                _mk(INFO_ARTIST_PX, QFont.Medium),     # 14 Medium 500（二级）
                _mk(INFO_ALBUM_PX, QFont.Medium),      # 14 Medium 500（二级）
                _mk(INFO_SPEC_PX, QFont.Medium))       # 14 Medium 500（二级）

    def _place_left_column(self) -> None:
        """按**上次布局算出的**左栏几何重排三行（折行 + 逐行绘制指令）。

        触发点：切歌 set_meta / 规格回填 / 窗口几何变化（_layout_children）。
        全程只在这里做文本度量 ⇒ paintEvent 里只有 drawText（绘制期零测量）。
        """
        geom = getattr(self, "_left_geom", None)
        if not geom:
            self._info_lines = []
            return
        x0, left_w, side, content_h, sr = geom
        f_title, f_artist, f_album, f_spec = self._make_info_fonts(sr)
        fm_t, fm_a, fm_b, fm_s = (QFontMetrics(f_title), QFontMetrics(f_artist),
                                  QFontMetrics(f_album), QFontMetrics(f_spec))
        text_w = max(80, int(side))
        cov = int(side) + COVER_PAD * 2
        cover_x = x0 + (left_w - cov) // 2
        tx = cover_x + COVER_PAD                       # ★ 信息块左边缘 == 封面可见左边缘
        title, artist, album, spec = self._info_source_texts()
        t_lines = self._wrap_text(title, fm_t, text_w, INFO_TITLE_MAX_LINES)
        self._title_lines = list(t_lines)
        # ★★ task-34①：三间距是**绝对规格**（卡片实测口径 10/6/8 ±1px，任意窗口都要成立）
        #   ⇒ **不随 sr 缩放**（旧版 ×sr 在 sr=1.5 的宽窗口会量到 15/9/12 ⇒ 直接超标）
        gap_1 = INFO_GAP_TITLE_ARTIST - INFO_GAP_AA_COMP   # 歌名 → 歌手 = 10（屏幕实测口径）
        gap_2 = INFO_GAP_ARTIST_ALBUM - INFO_GAP_AA_COMP   # 歌手 → 专辑 = 6
        gap_3 = INFO_GAP_ALBUM_SPEC - INFO_GAP_AA_COMP     # 专辑 → 规格 = 8
        gap_cover = int(INFO_GAP_COVER)
        # ---- 行表：(kind, 文本, 字体, alpha, 与前一行墨迹的间距) --------------------
        #  · 前缀按卡片回归：「歌手: 」「专辑: 」
        #  · 专辑缺失 ⇒ **整行连同它的 6px 一起消失**（不占位，歌手的下一行直接是规格 8px）
        rows = []
        # ★★ task-66（用户 9）：歌名折行**紧凑** —— 卡片两个口径要**同时**成立：
        #   ① 两行**基线距** = round(1.15×字号)；② 两行**墨迹间空白** = 3~4px。
        #   ⚠️ 本行表里的 `g` 是"上一行墨迹底 → 本行墨迹顶"的**空白**（`y_ink = ink_bottom + g`）
        #   ⇒ 所以这里要填**空白值**（不是行高！我第一版填 28 ⇒ 空白变 28px，反而更松 ✗）。
        #   空白 = 基线距 − 本行墨迹高，再夹到 [3,4]px ⇒ 两个口径自然同时成立 ✓
        _t_px = max(9, int(f_title.pixelSize()))
        _t_base_h = float(round(float(INFO_TITLE_LINE_RATIO) * float(_t_px)))
        _t_ink_h = 0.0
        for i, t in enumerate(t_lines):
            try:
                _t_ink_h = float(fm_t.tightBoundingRect(t).height()) or float(_t_px)
            except Exception:
                _t_ink_h = float(_t_px)
            _t_blank = max(3.0, min(4.0, _t_base_h - _t_ink_h))
            rows.append(["title", t, f_title, INFO_A_TITLE, 0 if i == 0 else _t_blank])
        if artist:
            rows.append(["artist", "歌手: " + artist, f_artist, INFO_A_ARTIST, gap_1])
        if album:
            rows.append(["album", "专辑: " + album, f_album, INFO_A_ALBUM,
                         gap_2 if artist else gap_1])
        if spec:
            rows.append(["spec", spec, f_spec, INFO_A_SPEC,
                         gap_3 if album else (gap_3 if artist else gap_1)])
        # ---- 墨迹锚定：逐行算出"墨迹顶/墨迹底"，行间距 = **墨迹到墨迹** ----------
        def _ink(fm, text):
            try:
                r = fm.tightBoundingRect(text)
                if r.height() > 0:
                    return float(r.top()), float(r.height())
            except Exception:
                pass
            return -float(fm.ascent()), float(fm.ascent() + fm.descent())

        heights, inks = [], []
        for kind, text, font, _alpha, _gap in rows:
            fm = QFontMetrics(font)
            inks.append(_ink(fm, text))
            heights.append(float(fm.height()))
        text_h = 0.0
        for i, (_k, _t, _f, _a, g) in enumerate(rows):
            text_h += (g if i else 0.0) + inks[i][1]
        block_h = cov + gap_cover + text_h
        # 居中偏上（卡片）：整块上移 8% 内容高，绝不越出内容区
        cov_top = max(12, int((content_h - block_h) * 0.42))
        self.cover.setGeometry(cover_x, cov_top, cov, cov)
        y_ink = float(cov_top + cov + gap_cover)           # 首行**墨迹顶**
        runs, irows = [], []
        for i, (kind, text, font, alpha, g) in enumerate(rows):
            fm = QFontMetrics(font)
            ink_top_off, ink_h = inks[i]
            if i:
                y_ink = ink_bottom + float(g)
            base_off = (heights[i] + fm.ascent() - fm.descent()) / 2.0   # AlignVCenter 的基线
            rect = QRectF(tx, y_ink - ink_top_off - base_off, text_w, heights[i])
            shown = fm.elidedText(text, Qt.ElideRight, text_w) if text else ""
            runs.append((rect, shown, font, alpha))
            ink_bottom = y_ink + ink_h
            irows.append({"kind": kind, "text": shown, "px": int(font.pixelSize()),
                          "title_line_ratio": float(INFO_TITLE_LINE_RATIO),
                          "title_line_h": (round(_t_base_h, 2) if kind == "title" else None),
                          "title_blank": (round(_t_blank, 2) if kind == "title" and i else None),
                          "title_gap_px": (round(float(g), 2) if kind == "title" and i else None),
                          "weight": int(font.weight()), "ink_top": round(y_ink, 2),
                          "ink_bottom": round(ink_bottom, 2), "left": int(rect.left()),
                          "w": int(rect.width()), "gap_before": float(g)})
        self._info_lines = runs
        self._info_rows = irows
        # 卡片三间距的**实测值**（墨迹到墨迹，按 kind 边界取；专辑隐藏时它自然消失）
        gmap = {}
        for i in range(1, len(irows)):
            gmap["%s→%s" % (irows[i - 1]["kind"], irows[i]["kind"])] = round(
                float(irows[i]["ink_top"]) - float(irows[i - 1]["ink_bottom"]), 2)
        self._info_gaps = gmap
        self._album_hidden = (not album)
        # 兼容旧探针：三"行"的 (rect, font, alpha) 槽位
        self._info_slots = [(QRectF(r.left(), r.top(), r.width(), r.height()), f, a)
                            for r, _t, f, a in runs[:3]]
        self._left_col = (x0, left_w, cov_top, cov)
        self._left_col_w = int(left_w)
        self._cover_side = int(side)
        self._cover_visible_left = int(tx)
        self._cover_box_x = int(cover_x)
        self._info_left = int(tx)
        self._info_w = int(text_w)
        self._block_top = int(cov_top)
        # ★ task-34：块底 = 末行墨迹底（ty 已随墨迹锚定重写，旧变量不再存在）
        self._block_bottom = int(irows[-1]["ink_bottom"]) if irows else int(cov_top + cov)

    def left_layout(self) -> dict:
        """★ task-30 可读锚点：QA 算占比请用 `left_w`（**不是** `_right_col[0]`）。

        `cover_ratio = cover_side / left_w`（卡片 78%~82%）；`lines` 给三行的
        文本/左边缘/字号/字重/alpha —— 判据 A~E 全部可由此逐条核对。
        """
        lines = []
        for rect, text, font, alpha in (getattr(self, "_info_lines", None) or []):
            # ★★ task-45 补锚点（QA 判据①②）：alpha 可读 + **确切取样盒与取样口径**
            #   坐标空间 = AudioView 局部（与 rect 同一空间；AudioView 铺满窗口体 ⇒ 探针里
            #   直接 `av.grab()` 后按 `×dpr` 取样即可，**不需要**再加任何 offset）。
            _px = int(font.pixelSize())
            _x0, _y0 = int(rect.left()), int(rect.top())
            _x1 = _x0 + int(rect.width())
            _y1 = _y0 + int(max(16, _px * 1.6))
            lines.append({"text": text, "left": _x0, "top": _y0,
                          "w": int(rect.width()), "px": _px,
                          "weight": int(font.weight()),
                          "alpha": int(alpha), "alpha_u8": int(alpha),
                          "alpha_f": round(int(alpha) / 255.0, 4),
                          "alpha_255": int(alpha),
                          "sample_box": [_x0, _y0, _x1, _y1],
                          "sample_rule": ("框内取与背景 |Δ|RGB 之和 ≥ 0.75×该框最大 |Δ| 的像素，"
                                          "再对这些像素取各通道**中位数** = 该行字形核心色"),
                          "coord_space": "audio_view_local(same as rect)"})
        side = int(getattr(self, "_cover_side", 0) or 0)
        lw = int(getattr(self, "_left_col_w", 0) or 0)
        # ★★ task-34②：响应式居中盒模型的**可读锚点**（QA 逐条对账用）
        box = getattr(self, "_content_box", None) or (0, 0, 0, 0)
        content_w, start_x = int(box[0]), int(box[1])
        rw = int(getattr(self, "_right_col_w", 0) or 0)
        rows = list(getattr(self, "_info_rows", None) or [])
        return {"content_w": content_w, "start_x": start_x, "content_max_w": int(CONTENT_MAX_W),
                # ★ Lead 指定字段名（一个 eval 全读到）
                "left_col_w": lw, "right_col_w": rw,
                "content_w_formula": content_w_for(int(self.width())),
                "start_x_formula": start_x_for(int(self.width())),
                "left_ratio": (round(lw / content_w, 4) if content_w else None),
                "right_ratio": (round(rw / content_w, 4) if content_w else None),
                "margin_left": start_x,
                "margin_right": (int(self.width()) - start_x - content_w) if content_w else None,
                "right_w": rw,
                # ★ task-45 补锚点：`info_rows` 与 `lines` **同构**（含 alpha/alpha_u8/sample_box）；
                #   旧内部行表保留为 `info_rows_raw`（QA 之前读 info_rows 得到 null alpha 就是这个原因）
                "info_rows": lines, "info_rows_raw": rows,
                "info_gaps": dict(getattr(self, "_info_gaps", None) or {}),
                "album_hidden": bool(getattr(self, "_album_hidden", False)),
                "left_x": int((self._left_col or (0, 0, 0, 0))[0]), "left_w": lw,
                "cover_side": side, "cover_box": int(side + COVER_PAD * 2),
                "cover_box_x": int(getattr(self, "_cover_box_x", 0) or 0),
                "cover_visible_left": int(getattr(self, "_cover_visible_left", 0) or 0),
                "cover_ratio": (round(side / lw, 4) if lw else None),
                "cover_center_offset": int(getattr(self, "_cover_box_x", 0) + (side + COVER_PAD * 2) / 2
                                              - ((self._left_col or (0, 0, 0, 0))[0] + lw / 2))
                if lw else None,
                "info_left": int(getattr(self, "_info_left", 0) or 0),
                "info_w": int(getattr(self, "_info_w", 0) or 0),
                "title_lines": list(getattr(self, "_title_lines", None) or []),
                "block_top": int(getattr(self, "_block_top", 0) or 0),
                "block_bottom": int(getattr(self, "_block_bottom", 0) or 0),
                "lines": lines,
                "artist": self._artist, "album": self._album, "sub_raw": self._sub_raw}

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
        # ★★ task-34②（用户 ⑪）：**响应式居中盒模型** —— 废除固定左边缘/固定 78% 宽度。
        #   content_w = min(int(W*0.82), 1280)；start_x 居中 ⇒ 左右留白天然相等（差 ≤1px）。
        content_w = content_w_for(w)          # ★ 纯函数 = 单一真源（见模块级定义）
        x0 = start_x_for(w)
        col_w = content_w
        # ★★ task-30（用户 ⑥）：左栏 = **放大封面 + 与封面左边缘严格对齐的三行信息块**。
        #   ① 封面目标边长先按 scale_ratio 弹性给出（基准窗口 sr=1 ⇒ 295 ∈ 卡片 280~310）；
        #   ② left_w 由 "side = left_w × 0.80" **反推** ⇒ "占左栏可用宽度 78%~82%" 变成恒等式
      
        #   ⚠️ sr<1（矮窗口）**不缩封面**，基准值就是 295（卡片区间中值）；只在装不下时才让位。
        # ---- 左右栏 38% / 62%（卡片 ±2%）----------------------------------------
        left_w = max(300, int(col_w * LEFT_COL_RATIO))
        right_w = max(220, col_w - left_w)
        # ---- 封面（★ Lead 裁决：**占比优先**）------------------------------------
        #  ① 比例约束（全窗宽）：cover_side / left_col_w ∈ [0.78, 0.82] ← 卡片"占左栏 78%~82%"
        #  ② 尺寸约束（**基准窗口**）：280~310                        ← 卡片④的取值区间
        #  规则：先取 0.80×left_col；若它 >310 **且 310 仍能使占比 ≥0.78**（基准窗口就是这种情形）
        #        则取 310 ⇒ 两条约束在基准窗口**同时**成立；宽窗（left_col=486）310/486=0.638<0.78
        #        ⇒ 保比例取 389（=0.80×486，且 ≤ 绝对上限 420）。
        fit_h = int(content_h * 0.58)                           # 封面+信息块要装进内容区
        # 左栏要塞得下「封面盒(2×COVER_PAD 阴影走廊) + 信息块」；信息块宽 = 可见边长
        fit_w = max(160, left_w - 2 * COVER_PAD - 8)
        side = int(round(left_w * COVER_W_RATIO))
        if side > COVER_MAX_PX and left_w > 0 and (COVER_MAX_PX / float(left_w)) >= COVER_RATIO_BAND[0]:
            side = COVER_MAX_PX                                  # 基准窗口：尺寸区间优先（占比仍达标）
        side = max(COVER_MIN_PX, min(side, COVER_MAX_HARD_PX))
        side = max(120, min(side, fit_h, fit_w))
        left_w = max(left_w, side + 2 * COVER_PAD)              # 封面盒必须装得下
        right_w = max(220, col_w - left_w)
        # 交给 _place_left_column()：折行（歌名 ≤2 行）、字号/字重/alpha、左对齐定位、
        # 封面几何与"居中偏上"——全部在这里算好 ⇒ paintEvent 只 drawText（零测量）。
        self._content_box = (content_w, x0)      # ★ task-34 锚点：(盒宽, 盒左)
        self._right_col_w = right_w
        self._left_geom = (x0, left_w, side, content_h, sr)
        self._place_left_column()
        rx = x0 + left_w
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
        #    _place_left_column 这两个"布局/数据变化"时刻）。
        base = _text_color()
        # ★★ task-30：三行**全部左对齐**（与封面可见左边缘同一条竖线，差 ≤1px）。
        #    逐行绘制指令在布局期算好（含歌名 1~2 行折行/省略）⇒ 这里零文本测量。
        try:
            from . import theme as _th
            _light_now = bool(_th.is_light())
        except Exception:
            _light_now = False
        for rect, text, font, alpha in getattr(self, "_info_lines", []):
            if not text:
                continue
            # ★ task-45①：浅色档一级（歌名）用 0.92（235）；二级三行同为 166 不受影响。
            _a = int(alpha)
            if _light_now:
                _a = min(_a, int(INFO_A_TITLE_LIGHT))
            col = QColor(base)
            col.setAlpha(_a)
            p.setFont(font)
            p.setPen(col)
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        p.end()
