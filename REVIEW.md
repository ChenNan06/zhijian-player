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
| 打包 | PyInstaller 6.22.3 + Inno Setup |

**代码规模**：`src/` 28 个文件、约 10,600 行；`scripts/` 36 个验收脚本。

## 2. ⚠️ 已知未修复问题（请重点看这里）

### 2.1 【未修复】底部核心播放按钮不居中（偏差 -39px）

**现象**：胶囊控制条里，左侧（循环/画中画/截图/时间戳）+ 中间（停止/上一首/播放/下一首）
+ 右侧（音量/倍速/字幕/全屏/列表）三段式布局。右侧比左侧宽，中组被挤到左边。

**已尝试**（全部失败，见 `src/zhijian/ui/control_bar.py` 的 `_balance_sides()`）：

1. 用「控件宽度求和 + 间距」建模算平衡量 → **恒定偏 39px**，
   因为布局里还有多个 `addSpacing(4/6/2)`，建模漏算。
2. 改读 `mapTo()` 真实几何 + `layout().activate()` → 仍偏 39px。
3. 改成在**窗口坐标系**里对齐窗口中线 → 偏 19px。
4. 自续调度（`QTimer.singleShot(16, ...)`）反复修正 → `_pad_l` 停在 116 不再变化。
5. 最终形态：一次调用内循环 8 次 + 自续调度，实测 `QSpacerItem.changeSize()`
   **似乎不反映到 `sizeHint()`**，`_pad_l` 读数恒为 0 或 116，不再收敛。

**可疑点**（未验证）：
- `QSpacerItem.changeSize()` 之后 `sizeHint()` 是否立即更新？
- 是否该改用 `QGridLayout` + 左右两列 `setColumnStretch(1, 1)` 等宽，
  而不是塞 spacer？
- `ControlBar` 继承 `GlassFrame`，其 `body_rect()` 扣了阴影外边距，
  「整控件宽度中点」≠「可视面板中点」——基准取哪个才对？

**关键约束**：项目红线规定**禁止硬编码坐标**，只能用 Qt 布局解决。
如果审查者认为这条红线在这里必须让步，请明确说明理由。

### 2.2 其他已知问题

| 问题 | 状态 |
|---|---|
| 验收 46 号「像素级截屏自审」 | 环境问题（抓屏抓到白屏），非产品缺陷 |
| 验收 45 号「内存泄漏」 | RSS 抖动较大，偶发假阳性（同一版本重跑可通过） |
| 设置面板卡片化 | 已实现 13 处，但用户反馈「看不出卡片」，疑似明度差不足 |
| `RoundedMenu` 的阴影是否完全消除 | 未确认 |

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

1. **`control_bar.py::_balance_sides()`** —— 见 §2.1，这是最需要帮助的地方。
2. **`widgets.py::GlassFrame`** —— 自绘玻璃面板 + 阴影的实现是否合理？
3. **`player.py`** —— ctypes 调用 libmpv 的资源管理、线程安全有无问题？
4. **`frame_grab.py`** —— 子进程抽帧的进程管理、退出路径有无竞态？
5. **整体** —— 10,600 行里哪些是明显的坏味道？

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
