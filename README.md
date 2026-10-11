# 至简Player —— 三合一全能媒体查看器

> 视频 / 图片 / 音频 一站式本地播放器。基于 **libmpv + OpenGL 硬件纹理共享**的单一渲染管线，
> 界面几何与交互结构**逆向自爱奇艺万能联播 5.4.1**，视觉做了现代化升华。

---

## 🤖 给接手的 AI / 开发者

**先读 [`HANDOVER.md`](HANDOVER.md)** —— 项目全况交接文档，包含：

* 用户需求原文与**历次修正**（判断"做对了没有"的唯一标准）
* **四条架构红线**（改代码前必读）
* 当前实现状态（逐模块）+ 设计系统实测数值
* **⚠️ 已定位缺陷**（居中真因：`getattr` 少了个引号 + 异常被吞；含 5 次失败尝试的完整诊断）
* 运行/构建/验收方法 + **血泪教训**（含本轮新增的"源码修好 ≠ 交付物修好"）

精简版见 [`REVIEW.md`](REVIEW.md)（给代码审查者）。

---

## 成品在哪（先看这里）

| 交付物 | 路径 | 说明 |
|---|---|---|
| **安装包**（推荐） | `output/至简Player-Setup-1.0.4.exe` | 170 MB，双击即装。含安装向导（Qt 自写）+ 卸载程序；只写 HKCU，**不需要管理员** |
| **绿色便携版** | `output/至简Player-Portable-1.0.4.zip` | 128 MB，解压即用，不安装不写注册表（含内置字体 23 MB） |
| **源码启动** | [`run.bat`](run.bat) | 用 Python 直接跑（需要 `runtime/libmpv-2.dll`） |
| **项目交接文档** | [`HANDOVER.md`](HANDOVER.md) | ⭐ 接手必读 |
| 代码审查说明 | [`REVIEW.md`](REVIEW.md) | 已知问题清单 + 给审查者的请求 |
| 逆向对照表 | [`docs/01-爱奇艺5.4.1逆向对照表.md`](docs/01-爱奇艺5.4.1逆向对照表.md) | 160 个皮肤 XML / 512 个颜色键 / 10 张截图的逐像素测量 |
| 验收报告 | [`docs/03-运行报告.md`](docs/03-运行报告.md) | 全部实测数据与判据 |
| 架构说明 | [`docs/02-架构设计.md`](docs/02-架构设计.md) | 为什么这么设计、踩过哪些坑 |

> ✅ **1.0.4（2026-10-10 深夜·第三轮反馈）**：① 设置面板**输入控件统一**（下拉/单行/数值同为
> 34px 高、同圆角；下拉弹窗改成无边框+半透明，圆角才真的露出来；下拉框补了自绘箭头）；
> ② 长按倍速那张卡**重做**：说明压成一行、细节移进 tooltip，固定档位框带**实时解析预览**
> （→ 2x · 5x · 10x）；③ **字体"粗细不均/笔画重合"的根因是 Qt 合成粗体** ——
> QSS 里写的 600 在家族里没有对应字面，Qt 就描边重画（实测墨迹比真粗体还重 53.9%），
> 全项目改用真实 Medium(500)；④ 滚轮：未选中的控件不改值**且设置页能正常滚动**
> （原来 `ev.ignore()` 并不能让父级滚动区滚，实测滚动条 0→0）；⑤ 全屏关掉 DWM 原生圆角与
> 窗口边框（画面四角不再被裁、贴边不再有白线，四角像素实测 = 画面自己的 #262626）；
> ⑥ 全屏下**抽屉手柄跟控制栏一起久不动自动隐藏**，鼠标一动就回来；
> ⑦ 新增 **Tab 媒体信息浮层**（左上角，PotPlayer 风格：分辨率/编码/像素格式/硬解/音频/时长/码率/容器）。

> ⚠️ 打包产物不在仓库里（体积原因）。构建方法见 `HANDOVER.md` §8.2。

---

## 30 秒上手

```text
双击 output\至简Player-Setup-1.0.4.exe   →  下一步  →  安装  →  完成
```

装完双击任意视频/图片/音乐，或把文件/文件夹**直接拖进窗口**。

| 操作 | 快捷键 |
|---|---|
| 播放 / 暂停 | `空格` |
| **停止**（回到开头） | `X`（或画面右键菜单；胶囊上的停止键已按你要求移除） |
| 快退 / 快进 5 秒 | `←` / `→` |
| **长按 → 快进（松开立即恢复原倍速）** | 按住 `→` 超过 300ms；两种模式见设置→播放 |
| **媒体信息浮层（左上角）** | `Tab` |
| 音量 | `↑` `↓`（或滚轮） |
| 全屏 / 播放列表 / 字幕设置 | `F` / `L` / `V` |
| 逐帧后退 / 前进 | `,` / `.` |
| 上/下一个文件 | `PageUp` / `PageDown` |
| 截图 · 设置 · 打开文件 | `S` · `Ctrl+,` · `Ctrl+O` |
| 图片模式：切换 / 缩放 / 旋转 / 镜像 / 适应 / 原始 | `←→` · 滚轮 · `R`·`E` · `H` · `1` · `2` |

倍速值、长按阈值、字幕样式、窗口默认尺寸**全部可在设置里改**，改完立即生效并落盘。

---

## 它解决了什么

对标「爱奇艺万能联播 5.0」，继承其**界面极简克制、功能合一、无广告**，
并逐条根治老版的历史痛点：

| 老版痛点 | 本项目的做法 | 实测结果 |
|---|---|---|
| 4K/高码率快进即卡死 | 硬件解码走 NVIDIA NVDEC 零拷贝；拖动进度条走关键帧跳转、松手才精确对位；`swapInterval=0` 不等垂直同步 | 4K 45Mbps 跨 40 秒连续跳转：**中位 69ms / P90 98ms，丢帧 0** |
| 格式支持少 | libmpv（ffmpeg）通吃 + Qt/Pillow 图片兜底 | AV1 / HEVC / ProRes / VP9 / FLAC / APE / WebP / AVIF 全部可开 |
| 无长按倍速 | 右方向键按下起 300ms 计时，到点平滑切倍速，松开立刻还原 | 真实键盘注入验证：单击 +5s、长按 2.0x、松开 <150ms 回 1.0x |
| 字幕不能自定义 | 4 行紧凑字幕浮层：字体 / 字号 / 描边色 / 垂直位置 / 时间轴 ±0.5s | 杜绝 PotPlayer 式千项级联菜单 |
| 三合一割裂 | 按媒体类型自动路由：视频出时间轴、图片出缩放条、音频出封面卡片 | 真实拖拽 + 双击均验证通过 |
| 窗口拉伸画面脱节 | 视频帧与 UI 在**同一个原生 HWND + 同一个 GL 上下文**里合成 | 800×600 ⇄ 1600×900 物理拖拽，全程贴合偏差 ≤1px |

---

## 技术选型

```
Python 3.12.10  +  PySide6 6.9.1 (Qt 6.9.1)  +  libmpv 0.41 (client-api 2.5)
                                                 ↑ runtime\libmpv-2.dll
渲染管线： mpv_render_context (MPV_RENDER_API_TYPE_OPENGL)
          → QOpenGLWidget 的 FBO（与 UI 同一窗口、同一 GL 上下文）
硬件解码： hwdec=auto-safe  →  本机解析为 nvdec（CUDA-GL 零拷贝）
```

**为什么用 `mpv_render_context` 而不是把 mpv 的窗口句柄塞进 Qt**：
后者的视频面是一个**原生子窗口**，永远盖在 Qt 控件之上 —— 想在上面浮一层半透明
控制条就只能另开独立顶层窗口，于是拖窗口/缩放时控制条和画面就必然对不上。
改用渲染 API 后视频是 Qt 自己 FBO 里的一张纹理，与其它控件在**同一次合成**里画出来，
边缘对齐在结构上就是成立的，不需要任何"追踪主窗口位置"的定时器。

---

## 目录结构

```
至简Player\
├── output\                       ★ 成品（安装包 / 便携版 zip / 解压好的目录）
├── run.bat                       源码启动器
├── requirements.txt
├── src\zhijian\                  应用源码
│   ├── app.py                    进程入口 / 日志 / 单实例 / 生命周期闭环
│   ├── config.py                 配置 + schema 迁移
│   ├── ipc.py                    自动化控制面（测试脚本用它读状态、切场景）
│   ├── mpv\                      libmpv ctypes 绑定 + 引擎 + OpenGL 渲染控件
│   ├── media\                    类型路由 / 目录扫描 / 标签与封面 / 外挂字幕 / 时长探针
│   ├── platform\win.py           Win32：DPI、无边框缩放、圆角、独显偏好
│   └── ui\                       设计系统 + 标题栏 / 悬浮胶囊 / 侧滑抽屉 / 设置 / 字幕浮层
├── packaging\                    PyInstaller spec + 安装向导
├── scripts\                      可复跑脚本（编号 = 执行顺序）
├── docs\                         逆向对照表 / 架构 / 运行报告 / 验收证据截图
├── runtime\libmpv-2.dll          解码内核
├── assets\testmedia\             验收用测试媒体（4K / 图片 / 带封面音频 / 字幕）
├── downloads\                    下载缓存（可删）
├── logs\                         脚本日志与证据（可删）
└── build\                        编译中间物（可删）
```

---

## 从源码跑

```powershell
# 1) 装依赖
C:\DevTools\Python312\python.exe -m pip install -r requirements.txt

# 2) 准备解码内核（约 31MB 压缩包，解压出 116MB 的 libmpv-2.dll）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\01-setup.ps1

# 3) 生成验收用测试媒体（4K 高码率那片要几分钟）
C:\DevTools\Python312\python.exe scripts\30-gen-testmedia.py

# 4) 直接启动
run.bat
# 或者
set PYTHONPATH=src && C:\DevTools\Python312\python.exe -m zhijian "D:\some\video.mkv"
```

## 重新打包

```powershell
# 绿色便携版（onedir + zip）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\50-build-portable.ps1

# 安装包（单文件 Setup.exe，含向导与卸载程序）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\51-build-installer.ps1
```

> ⚠️ 含中文的 `.ps1` **必须存成 UTF-8 with BOM**。用不带 BOM 的编辑器改过之后，
> Windows PowerShell 5.1 会按 GBK 解码，脚本里所有中文都会变成乱码并引发语法错误。

---

## 验收怎么复跑

全部是**真实系统级**验证，不是"给自己发 Qt 合成事件"：

```powershell
python scripts\41-ui-tour.py            # 界面巡检（真实点击 + 真实截屏 + 几何断言）
python scripts\42-accept-resize.py      # 物理鼠标拖拽拉伸 800x600 <-> 1600x900
python scripts\43-accept-longpress.py   # 物理键盘长按右方向键 -> 2 倍速 -> 松开还原
python scripts\44-accept-seek4k.py      # 4K 高码率连续 Seek 性能 + 丢帧率
python scripts\45-accept-memory.py      # 内存/句柄泄漏 + 10 轮退出闭环
python scripts\46-visual-selfcheck.py   # 像素级截屏自审
python scripts\47-accept-installer.py   # 安装 -> 运行 -> 卸载 端到端
```

结果分别落在 `logs\4x-*.log|json`，截图落在 `docs\04-验收证据\`。
逐项实测数据见 [`docs/03-运行报告.md`](docs/03-运行报告.md)。

---

## 已知边界

* **只支持 Windows**（用了 DWM 圆角、Win32 无边框命中测试、SendInput 验收）。
* 首次冷启动约 1.5~2.5 秒（要加载 116MB 的 libmpv 和 Qt 运行库）。
* 极少数 4K 精确跳转会出现 150~300ms 的 I/O 抖动（磁盘/调度引起，非解码瓶颈）；
  拖动过程中的关键帧跳转 P90 稳定在 100ms 以内。
* HEIC/JXL 等 Qt 与 ffmpeg 都不覆盖的图片格式，需要 Pillow 插件在场（安装包已含）。
