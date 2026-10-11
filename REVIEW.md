# 至简 Player — 代码审查说明

> 本文件专门给**审查代码的 AI / 工程师**看。
> 请优先读 §2「已知未修复问题」和 §3「设计约束」，再读代码。

## 1. 这是什么

一个 Windows 三合一媒体播放器（视频 / 图片 / 音频），对标爱奇艺万能联播 5.x，
目标是修掉它的缺陷（4K 拖动卡顿、格式覆盖、HDR、字幕挂载、三态路由）。

**技术栈**

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12.10 |
| UI | PySide6 6.9.1（Qt 6.9） |
| 播放内核 | libmpv 0.41，**裸 ctypes** 调用（client API 2.5），无 python-mpv 包装 |
| 渲染 | `mpv_render_context`（`MPV_RENDER_API_TYPE_OPENGL`）→ `QOpenGLWidget` |
| 缩略图 | **ffmpeg 子进程**抽帧（`imageio_ffmpeg` 自带二进制） |
| 打包 | PyInstaller 6.22.3（应用 onedir + 自写 Qt 安装向导；**不用 Inno Setup**） |
| 界面字体 | **内置 HarmonyOS Sans SC**（免费商用，许可允许随包；Regular/Medium/Bold 真实字重），不依赖系统字体 |

**代码规模**：`src/` 28 个文件、约 10,600 行；`scripts/` 36 个验收脚本。

## 2. ⚠️ 缺陷清单（本轮已定位一处根因，请重点看这里）

### 2.1 【已定位 · 源码已修】底部核心播放按钮不居中（打包版仍是 +58px）

**现象**：胶囊控制条里，左侧（循环/画中画/截图/时间戳）+ 中间（停止/上一首/播放/下一首）
+ 右侧（音量/倍速/字幕/全屏/列表）三段式布局。右侧比左侧宽，中组被挤到左边。

**真因（2026-10-10 实测确认，与前 5 次尝试的方向都无关）** ——
`src/zhijian/ui/control_bar.py::_balance_sides()` 里：

```python
n = getattr(self, _balancing, 0)     # ← 少了引号，_balancing 是未定义的名字
...
except Exception:
    pass                             # ← NameError 被吞掉，日志里毫无痕迹
```

**所以这个函数每次调用都在第一行就退出**，`_pad_l` 恒为 0。
前 5 次尝试（建模 / 读 `mapTo` 几何 / 换基准 / 自续调度 / 循环收敛）全都在改"算法"，
而它是**根本没跑起来**。

**实测证据**（`build\_probe_center_{live,src}.py` 可复跑）：

| 被测对象 | 1000 / 1280 / 1600 三种窗宽 | `_pad_l` |
|---|---|---|
| `output\…Portable-1.0.0\至简Player.exe`（修复前，已归档） | 恒 **+58.0 px** | 0 |
| 源码版 | 恒 **+0.0 px** | 116 |
| `output\…Portable-1.0.1\至简Player.exe`（修复后） | 恒 **+0.0 px** | 116 |
| `output\…Portable-1.0.2\至简Player.exe`（去掉停止键后） | 恒 **+0.0 px** | 114 |

**这个文件还被"转义污染"过**：文件里存在 0x07 / 0x08 控制字符，
`addSpacing`→`ddSpacing`、`activate`→`ctivate`、`return`→`eturn`，
`"_balancing"` 的引号就是这么丢的 —— 成因见全局知识库 §9.70。

**关键约束**：修复**不需要**违反"禁止硬编码坐标"的红线 ——
改的是 `QSpacerItem` 的尺寸，没有一处绝对坐标。

> ✅ **交付物已同步**：2026-10-10 晚重新打包为 **1.0.1**（版本号、安装包内部版本、
> 注册表 DisplayVersion 一并更新），打包版复验三种窗宽均为 **0.0 px**。
> 含缺陷的 1.0.0 已归档到 `build\old-1.0.0\`。

### 2.2 其他已知问题

| 问题 | 状态 |
|---|---|
| 验收 46 号「像素级截屏自审」 | 环境问题（抓屏抓到白屏），非产品缺陷 |
| 验收 45 号「内存泄漏」 | RSS 抖动较大，偶发假阳性（同一版本重跑可通过） |
| 设置面板卡片化 | 已实现 13 处，但用户反馈「看不出卡片」，疑似明度差不足 |
| `RoundedMenu` 的阴影是否完全消除 | 未确认 |
| ~~1.0.0 打包版不含居中修复~~ | ✅ **已解决**：重新打包为 **1.0.1**（10-10 晚）并复验 —— 打包版三种窗宽 0.0 px，见 §2.1 |
| **`except Exception: pass` 共 6 处** | 已修 1 处（`_balance_sides`，改成带 `exc_info` 的日志）；**其余 5 处未审** —— 这类写法会让"功能没生效"与"功能正常"在任何观测手段下长得一模一样 |
| 文件内曾出现控制字符污染 | 已清理（`control_bar.py` 4 处）；成因是"转义在写入途中被解释"，全局知识库 §9.70 有判据与扫描脚本 |
| **1.0.2 的四项改动** | 已实现并**行为断言**通过（源码版 + 打包版各一遍）：中组去停止键（播放键偏差 0.0 px）、内置字体、滚轮先聚焦、应用按钮反馈。脚本 `build\_probe_ui_round4.py` |
| **死配置绑定** | `hotkeys.stop` 曾是死配置（已补分支 + 右键菜单）；`larger_ui` / `smaller_ui` **仍是死绑定**（配置里有、无人消费），见 HANDOVER §7.2 |
| **1.0.3 的五项改动** | 已实现并行为断言通过：浅色启动取色（§9.14）、全屏手柄、播放列表按钮（§9.13 信号签名）、长按倍速两模式 + 持续 OSD、字体换 HarmonyOS Sans SC |
| **1.0.4 的七项改动** | 已实现并行为断言通过：输入控件统一/弹窗圆角、档位框重做、**合成粗体**（§9.15）、滚轮与页面滚动（§9.16）、全屏 DWM 圆角与白边、全屏手柄自动隐藏、Tab 媒体信息浮层 |
| **1.0.5 的六项改动** | 已实现并行为断言通过：全屏白边（§9.17 三层里的后两层）、控制栏左贴边、长按左=快退（§9.18）、Shift±15s 连跳、Tab 纯文字信息、GitHub 备份 |
| **源码备份** | `git@github.com:ChenNan06/zhijian-player.git`（SSH over 443）；构建产物不入库（`build/` `output/` `downloads/` `assets/testmedia/`） |
| **1.0.6 的五项重构** | 换肤统一（role 化 + 单一广播）、菜单自绘行 + 圆角子菜单、滑块正圆、快捷键双槽位 + WASD/Ctrl 组合、字幕内置字体（实测生效） |
| **主题相关代码纪律** | 新代码里**不要再写** `setStyleSheet(f"color:{{P[...]}}")`：能静态化就用 `role`，动态的用 `themed()` 登记 |
| **弹层纪律** | 任何"浮在别的界面之上"的面板（菜单/下拉/提示）底色必须用**不透明**的 `popup_bg`，窗口设无边框+半透明，**并且加圆角遮罩**（`clip_rounded`）—— QSS 圆角只画在方窗口里，遮罩才是结构性保证；内部滚动条要留边距 |
| **提示条纪律** | 不要用 Qt 原生 QToolTip（方窗口圆不了）；写 `setToolTip()` 即可，显示由应用级 `TooltipFilter` 接管 |
| **浮层纪律（1.0.10 起）** | 任何浮层必须继承 `ui/surface.py` 的 `Surface`（或由 `SurfacePopup` 工厂创建）；**禁止** `setMask`、禁止直接用 `QMenu`/`QToolTip`/Qt 私有下拉容器 —— 由 `build\_check_surfaces.py` 静态扫描 + `audit_surfaces()` 运行时审计把关 |
| **图标纪律** | 图标颜色是**烘进 pixmap** 的：换肤后必须重新 `setIcon`/`set_color`，只清缓存无效 |
| **倒放（1.0.7）** | 先 `pause=yes` 再按真实 dt 精确后退；mpv 无负速度，`frame-back-step` 也撑不起帧率 |
| **字重纪律** | 全项目只用家族里**真实存在**的字重（400 / 500）；`font-weight: 600` 会触发 Qt 合成粗体（实测墨迹比真粗体还重 53.9%） |
| **默认参数快照** | `IconButton/GlassFrame/OsdBadge/hline/show_osd/icons.*` 的 `= P[...]` 默认值已全部改成运行时解析；启动时补一次 `refresh_glass_theme()` |

## 3. 架构红线（改代码前必读）

1. **单一渲染管线**：禁止任何独立顶层浮窗（`Qt::Tool` 禁用、禁止轮询定时器）。
   视频必须经 `mpv_render_context` 渲染进同一个原生窗口里的 `QOpenGLWidget`。
2. **零硬编码坐标**：只能用 Qt 布局。
3. **单例生命周期 + 严格逆序析构**：禁止 `TerminateProcess`。
4. **播放列表必须是窗口内贴内边缘滑出的抽屉**。

## 4. 目录结构

```
src/zhijian/
├── app.py                  启动、主题归一化、单实例
├── ipc.py                  控制面（QLocalServer），验收脚本靠它驱动 UI
├── mpv/
│   ├── libmpv.py           ctypes 绑定（client API 2.5）
│   ├── player.py           MpvHandle：渲染上下文、事件循环、属性
│   └── frame_grab.py       ⭐ ffmpeg 子进程抽帧 + LRU 缓存
└── ui/
    ├── theme.py            调色板（_BASE_PALETTE + qq-light/qq-dark/graphite）
    ├── widgets.py          GlassFrame / IconButton / ProgressSlider /
    │                       PreviewPopup / ToggleSwitch / RoundedMenu
    ├── main_window.py      主窗口、无边框窗口过程、路由
    ├── control_bar.py      ⭐ 胶囊控制条（含 _balance_sides 未解决问题）
    ├── stage.py            舞台（视频/图片/音频三态）
    ├── settings_panel.py   设置面板
    ├── drawer.py           播放列表抽屉
    └── osd.py              操作提示

scripts/                    验收脚本（41~49 号用例 + 打包 50/51 + 总控 99）
docs/                       设计文档与逆向资料
```

## 5. 怎么跑

```bat
run.bat
```

依赖见 `requirements.txt`。需要 `runtime/libmpv-2.dll`（见 `docs/`）。

## 6. 怎么验收

```powershell
powershell -ExecutionPolicy Bypass -File scripts\99-run-all-acceptance.ps1
```

9 个用例，覆盖：界面巡检、**物理拖拽拉伸**（SendInput 真鼠标）、
长按方向键倍速、4K 连续 seek 性能、内存泄漏与退出闭环、
像素级截屏自审、安装包端到端、交互跟手性、全屏/换肤/抽屉/布局。

## 7. 给审查者的具体请求

1. **`control_bar.py::_balance_sides()`** —— 见 §2.1：真因已定位并修复（少了个引号 + 异常被吞）。
   **请复核这个修复**，并顺手判断同文件其余 5 处 `except Exception: pass` 是否也该补日志。
2. **`widgets.py::GlassFrame`** —— 自绘玻璃面板 + 阴影的实现是否合理？
3. **`player.py`** —— ctypes 调用 libmpv 的资源管理、线程安全有无问题？
4. **`frame_grab.py`** —— 子进程抽帧的进程管理、退出路径有无竞态？
5. **整体** —— 10,600 行里哪些是明显的坏味道？
6. **交付物一致性** —— 请复核"源码改了但 `output\` 没重打包"这类漂移（1.0.0 就栽在这上面）。
   建议把 `build\_probe_center_live.py` / `build\_probe_ui_round4.py --exe …`
   这类"打产物本体"的探针纳入交付前检查。
7. **配置项与实现的对账** —— 请扫一遍 `config.py` 的 DEFAULTS 与 hotkeys：
   有没有别的"配置里有、界面能改、没人消费"的键（已知 `larger_ui` / `smaller_ui`）。

## 8. 开发过程中的重要教训（避免重复踩坑）

- **Qt 的 8 位十六进制是 `#AARRGGBB`**，但本项目调色板约定是 CSS 顺序 `#RRGGBBAA`，
  由 `qt_color()` / `css()` 转换。样式表里写裸值会出诡异颜色（曾导致"黄色按钮"）。
- **内联 `setStyleSheet(f"color:...")` 是构造时快照**，换肤不会更新。
  主题相关的颜色一律走 `setProperty("role", ...)` + 全局 QSS。
- **同一个类里不要出现两个同名方法** —— Python 只认最后一个，
  前面那个成了死代码（本项目的居中 bug 就是这么来的）。
- **不要在同一个进程里并存两个完整播放引擎** —— 第二個 libmpv 实例会和主实例
  在 Windows 图形栈上争死，主线程卡死。取缩略图用 ffmpeg 子进程。
- **子控件的 `closeEvent` 不会被调用**（父窗口关闭时），后台线程收尾要挂
  `QApplication.aboutToQuit`。
