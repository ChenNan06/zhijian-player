# 至简Player 项目现状审计报告（PROJECT_STATUS_AUDIT）

> 审计对象：`D:\AI-Project\至简Player` 工作区**磁盘上的真实源码**
> 审计版本：`__version__ = "1.0.34"`（`src/zhijian/__init__.py`）
> 审计方式：静态代码检索与逐行核对（`Select-String` / `read`），**未修改任何业务代码、未编译、未推送**
> 审计日期：本次对话
> 原则：所有结论均标注**文件:行号**；未能实测验证的部分明确标注为「未验证」，不含"已完美修复"式表述。

---

## 一、项目架构全景与目录映射

### 1.1 源码规模（`src/zhijian/`，实测行数）

| 文件 | 行数 | 核心职责 |
|---|---:|---|
| `app.py` | 391 | 应用入口：QApplication 装配、单实例、主题、异常钩子、优雅退出 |
| `config.py` | 337 | 配置读写（JSON）、默认值、最近文件、脏标记与延迟落盘 |
| `ipc.py` | 605 | 本地 IPC 控制面（QLocalServer/QLocalSocket）：`ping/open/eval/quit/ui` 等 |
| `media/tags.py` | 379 | **纯 Python** 内嵌标签/封面读取（ID3v2.2/2.3/2.4 APIC、FLAC、MP4）——**不依赖 ffprobe/mutagen** |
| `media/scanner.py` | 95 | 目录扫描 + **自然排序** `natural_key()` |
| `media/router.py` | 75 | 媒体类型路由：`KIND_VIDEO / KIND_IMAGE / KIND_AUDIO / KIND_UNKNOWN` + `IMAGE_EXT` |
| `media/exif.py` | 127 | 轻量 EXIF（TIFF/EXIF）读取：曝光/光圈/焦距/时间 |
| `media/lyrics.py` | 153 | **歌词引擎**：外挂 `.lrc` + 内嵌歌词；编码探测；多标签时间轴解析 |
| `media/prober.py` | 80 | 后台时长探测（供播放列表显示 duration） |
| `media/subtitles.py` | 109 | 字幕轨道发现与选择 |
| `mpv/libmpv.py` | 533 | libmpv 动态库定位与 ctypes 绑定（`libmpv-2.dll`） |
| `mpv/player.py` | 714 | 播放引擎（`MpvEngine`）：load/set/cmd/cached、属性事件回调 |
| `mpv/frame_grab.py` | 211 | 抽帧（进度条悬停缩略图） |
| `platform/win.py` | 518 | Windows 平台：任务栏/窗口消息/DPI/默认应用等 |
| `ui/main_window.py` | 2558 | 主窗口：模式路由、播放控制、OSD、图片/音频接线、热切图 |
| `ui/widgets.py` | 1651 | 自绘控件库：`GlassFrame`、`LogSlider`、`IconButton`、`PreviewPopup` 等 |
| `ui/image_view.py` | 1057 | **看图视口**：解码管线、锚点缩放、自由画布、热区、鸟瞰图 |
| `ui/stage.py` | 973 | **舞台**：覆盖层层叠、抽屉手柄、热区摆放、音乐视图宿主 |
| `ui/settings_panel.py` | 806 | 设置面板（含「看图」分区） |
| `ui/audio_view.py` | 582 | **音乐模式总成**：背景缓存、封面、歌词面板、频谱 |
| `ui/control_bar.py` | 591 | 底部控制栏（胶囊）：进度条、音量、全屏、悬停预览 |
| `ui/theme.py` | 592 | 主题与调色板（`P`）、`is_light()`、`apply_theme()` |
| `ui/playlist_drawer.py` | 399 | 播放列表抽屉 + 排序（`resort`） |
| `ui/subtitle_panel.py` | 299 | 字幕浮层 |
| `ui/surface.py` | 274 | 视频渲染表面（QOpenGLWidget 封装） |
| `ui/icons.py` | 176 | 矢量图标绘制（含 `fullscreen_exit`、`fit`、`flip_h` 等） |
| `ui/titlebar.py` | 107 | 自绘标题栏 |
| 其余 `__init__.py` 等 | 20 | 包导出 |
| **合计** | **14436 行** | |

### 1.2 运行环境与依赖（实测）

| 项 | 实测值 | 证据 |
|---|---|---|
| Python | **3.12.10** | `python -c "import sys"` |
| PySide6 | **6.9.1** | `PySide6.__version__` |
| Pillow | **12.3.0（已安装，已用于解码）** | `PIL.__version__`；`image_view.py:133 _pil_decode` |
| libmpv | **`libmpv-2.dll` 116 MB** | 磁盘：`downloads/mpv/extract/libmpv-2.dll`、`build/dist/至简Player/_internal/` |
| libmpv 挂载方式 | `ctypes.CDLL()`（`mpv/libmpv.py:245`），搜索顺序：包内 → exe 同级 → `<root>/runtime` → `downloads/mpv/extract`（`libmpv.py:211~218`） | 硬编码候选路径，找不到抛 `找不到 libmpv-2.dll...`（`libmpv.py:226`） |
| **mutagen** | **未安装**（`find_spec('mutagen')` 为 False） | ⚠️ `media/lyrics.py` 的 mutagen 分支在本机**从未被执行**，只能走 ffprobe 回落 |
| ffprobe | 依赖系统 PATH 或 `<exe目录>/ffprobe.exe`（`lyrics.py:_embedded_via_ffprobe`） | 未在本机实测内嵌歌词路径 |
| 打包 | PyInstaller 6.22.3；`packaging/setup.spec` 的 `binaries=[]`、`datas=[build/payload.zip, assets/icon.ico]`、`hiddenimports=[]` | 说明 **PIL 依赖 PyInstaller 自带 `hook-PIL`**（构建日志确认处理了 `hook-PIL.py`/`hook-PIL.Image.py`），未在 spec 里显式声明 |

### 1.3 主生命周期与模式路由

- 打开文件：`MainWindow.open_path()`（`ui/main_window.py:452` 起）
  1. 校验存在 → `os.path.abspath` → `router.kind_of(path)`，`KIND_UNKNOWN` 兜底为 `KIND_VIDEO`
  2. 写 `self._path/_kind`、更新标题、`cfg.push_recent`、`cfg.save_if_dirty`
  3. 若列表为空或 `remember_dir=True`：`scanner.siblings()` 重建列表 + `prober.request()` 后台探测时长
  4. `_apply_kind_ui(kind)` 切换视口显隐
  5. **图片→图片快速路径**：跳过 `_prepare_engine_for`，直接 `_iv_load()`（`:452` 附近注释明确记录了"整段跳过等于再也不加载图片"的历史事故）
  6. `engine.load(path, options=load_opts)`

- **mpv 视频轨参数（`main_window.py:458-463`，v1.0.28 修复点）**：
  ```python
  if kind == router.KIND_AUDIO:   load_opts = "vid=no,aid=auto"     # 音频：关视频轨（藏内嵌封面）
  elif kind == router.KIND_VIDEO: load_opts = "vid=auto,aid=auto"   # 视频：显式恢复
  else:                           load_opts = "vid=auto"
  ```
  关键：`vid` 是 **per-file 选项**，必须在 `load()` 时下发；仅在 load 前 `set()` 会被覆盖（曾导致"音乐→视频黑屏"）。
  另在 `main_window.py:467` 段对非音频模式再补一次 `set("vid","auto")` 与 `video.setVisible(True)`。

- 视口显隐（`_apply_kind_ui` 内）：`image_view` 仅图片显示；`video` 仅视频显示（音频模式**也隐藏**，避免 mpv 渲染内嵌封面造成"双封面"）；`capsule` 非图片显示；`stage.set_audio_view_visible(kind == AUDIO)`。

---

## 二、核心三大模式真实实现

### 2.1 视频模式（KIND_VIDEO）

- **渲染**：`ui/surface.py`（274 行）承载 QOpenGLWidget；`mpv/player.py` 创建 `mpv_render_context`，在 `initializeGL` 建立渲染回调；历史事故记录在代码注释里：`MPV_RENDER_PARAM_BLOCK_FOR_TARGET_TIME` 曾造成 26ms/帧停顿，已关闭。
- **控制栏悬浮**：胶囊 `ControlBar` 不占布局行，挂到舞台第 3 行（`Stage.attach_capsule`），视频铺满整个画面区。
- **全屏双向同步**：`MainWindow.changeEvent()`（`QEvent.WindowStateChange`）→ `image_bar.set_fullscreen()` + `capsule.set_fullscreen()`；`toggle_fullscreen()` 内亦直接同步一次。**真相源是应用自身的 `_fullscreen`**（自绘全屏路径下 `isFullScreen()` 不可靠）。
- **抽屉联动**：`drawer.open_state_changed` → `_on_drawer_state()` → `titlebar.set_playlist_checked` / `capsule.set_list_checked` / `stage.set_drawer_visible` / `stage.set_hotspots_enabled(not on)`；同时连接 `stage.sync_hotspots()`。
- **已知 Bug / 待优化**：
  1. 音频 → 视频切换的 `vid=auto` 修复**已在源码**（`:461`），但本轮审计未做真机回归；
  2. `Stage.resizeEvent` 存在**两份定义**（见 §3.1、§5.4）；
  3. 全屏/窗口状态变化仅靠 `changeEvent` + toggle 双路，**未见针对多显示器缩放变化（DPI 切换）的处理**。

### 2.2 看图模式（KIND_IMAGE）

- **热切图流水线**：`MainWindow._hot_switch_to()`（`main_window.py:906`），由 `_step_image()`（`:957`）优先调用，失败才回落 `open_path()`。
  只做 5 件事：① 更新 `_path` + `drawer.set_current`；② `_iv_load(path)`（缓存优先）；③ 标题/计数用 `QTimer.singleShot(0, self._update_image_info)` **延到下一轮事件循环**；④ `_update_empty_state()`；⑤ `_schedule_image_meta(path)` 把 `os.stat`/EXIF 推后台（`_MetaTask` + Qt 信号回填，过期结果丢弃）。
  **明确避开**的冷启动项（代码注释逐条列出）：`cfg.push_recent + cfg.save_if_dirty`（每切一张写一次配置文件）、`engine.load()`（每切一张一次同步 mpv IPC）、目录重扫与列表重建。
  **实测证据（曾记录）**：连切 12 次主线程阻塞 `max 2.83ms / avg 1.83ms`（改造前 `max 404ms / avg 107ms`），`open_path` 在连切期间零调用。
- **解码管线**：`image_view._pil_decode()`（`image_view.py:133`）
  - 等比目标：`ratio = min(want_w/src_w, want_h/src_h, 1.0)`（`:155`，**永不放大**）
  - DCT 降采样：`im.draft("RGB", f0)`（`:162`）→ EXIF 旋转后**再算一次**等比 → `im.resize(f1, Image.BILINEAR)`
  - `Pillow` 打不开的格式回落 `QImageReader`（含 `setScaledSize`）
  - 两阶段：stage1 = 视口×DPR（`_stage_size()`）；stage2 = 原图，**受 `FULL_DEBOUNCE_MS=300` 防抖 + `FULL_MIN_SCALE=1.05` 双重门槛**（`:47/:49`），且跑在**独立线程池** `_fpool`（`setMaxThreadCount(1)`，`:605`）
- **视口交互数学模型**
  - 滚轮：`WHEEL_STEP = 1.15`（`:43`）；`dy > 0`（上滑）放大 ×1.15，反之 ÷1.15；`image.wheel_invert` 强制 False
  - 锚点：`set_target_scale()`（`:895`）记录 `(cx, cy, au, av)`；`_tick()` 内按**当前 scale 反解**偏移 `off = A - V/2 - (uv-0.5)*W(scale)`；缩放插值未完成时**禁止 clamp**，收敛后才整理边界
  - 即时缩放（拖滑条）：`set_scale_immediate()`（`:858`）跳过弹簧
  - 自由画布：`_bounds()`（`:1038`）改为 `mx = (W+VW)/2 - margin`、`my = (H+VH)/2 - margin`，`FREE_PAN_MARGIN_PX = 60.0`（`:46`）⇒ 只留 60px 边缘也允许继续拖
  - 实测（曾记录）：10× 时锚点偏差 `[0.0, 0.0]px`；`strict 251 → free 1469`；可拖到"视口内仅剩 60.0px"
- **目录排序**：`media/scanner.py:19 natural_key()`（正则分块 + 数字转 int，**非** ASCII 排序）；用于 `scanner.py:84/101`；抽屉侧 `playlist_drawer.resort()`（`:350`）真正重排**底层序列**，`paths()` 与列表展示一致。
  ⚠️ **注意**：用户提到的"调用 Windows 原生 `StrCmpLogicalW`"**当前代码里并不存在**——实现是纯 Python 自然排序键，效果等价但机制不同。

### 2.3 音乐模式（KIND_AUDIO）——重点

#### （1）背景模糊
- **缓存字段**：`self._bg_cache = QPixmap()`，定义于 `ui/audio_view.py:491`（注释「静态背景缓存（绝不每帧重算）」）
- **生成时机**：`_rebuild_bg_now()`（`:595`）
  - 换封面：`set_cover()` 内直接调用一次
  - 窗口尺寸变化：`resizeEvent()` 只 `self._bg_timer.start()`（`BG_DEBOUNCE_MS = 200`，`:27/:509`）⇒ **停稳 200ms 后**才重建
- **模糊算法**：`blur_pixmap()`（`:63`）居中裁剪（`KeepAspectRatioByExpanding`）→ 缩到 `BLUR_WORK_W = 160`（`:26`）→ 两轮放大/缩小低通 → 输出目标尺寸
- **绘制期**：`paintEvent`（`:639-641`）只有一行 `p.drawPixmap(...)`，**不做任何模糊计算**；再叠主题 Scrim
- **全屏铺满**：几何由 `Stage.resizeEvent` 无条件 `av.setGeometry(0, 0, self.width(), self.height())`；实测 `AudioView = stage = 1707×1067` ✓

#### （2）专辑封面 CoverArt
- 尺寸：`side = max(150, min(left_w*0.86, content_h*0.72, int(260 * scale_ratio)))`（`_layout_children`）
- 圆角路径：`QPainterPath.addRoundedRect(QRectF(x, y, side, side), 12, 12)` → `setClipPath` → `drawPixmap`；阴影为 **2 层 `addRoundedRect` 扩散**（`grow 3/7`，仅圆角）
- **无 `fillRect` / `drawRect` / `QGraphicsDropShadowEffect`**
- **直角露底真因与现状**：`CoverArt` 作为 QWidget 默认会用自身调色板**填一个矩形**，圆角之外露出方形底（v1.0.33 用户截图红框）。修复：`__init__` 里
  ```python
  self.setAttribute(Qt.WA_TranslucentBackground, True)
  self.setAttribute(Qt.WA_NoSystemBackground, True)
  self.setStyleSheet("background: transparent;")
  ```
  实测截图中四角已无露底 ✓

#### （3）歌词引擎 `media/lyrics.py`（153 行）
- 来源优先级：**同名 `.lrc`（大小写不敏感）→ 内嵌标签**（`load_lyrics()`）
- 编码探测：`ENCODINGS = ("utf-8-sig", "gb18030", "utf-16")`，并对解出文本做**替换符比例校验**（因为 gb18030 几乎不抛异常，仅靠 `except` 不足以判断）
- 时间轴：`_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")` ⇒ 支持**一行多标签**与 **1/2/3 位毫秒**
- 元信息标签 `[ti:]/[ar:]...` 被 `_META_RE` 过滤，不作为歌词
- 双语合并：`LyricsPanel._merge_bilingual()`（`audio_view.py:311` 附近）把**同一时间戳的连续两行**合并为 `"原文\n翻译"` 单节点
- 内嵌歌词：`_embedded_via_mutagen()`（本机无 mutagen ⇒ 不生效）+ `_embedded_via_ffprobe()`（回落）

#### （4）排版与渲染 `LyricsPanel`
- 预排版缓存 `_relayout()`（`:340`）：仅在 `set_lines()` 与 `resizeEvent()` 触发；缓存字段 `_main_h / _trans_h / _tops / _heights`
- 常量（`audio_view.py:37-40`）：`LYRIC_GROUP_PAD = 29`（组间）、`LYRIC_INNER_PAD = 6`（组内）、`CURRENT_LINE_RATIO = 0.40`（黄金线）、`FADE_PX = 40`（渐隐）
- 字号：主词 `27×scale_ratio`（当前）/ `20×ratio`（其余）；翻译 `18×ratio` / `14×ratio`；透明度 1.0 / 0.40（翻译 0.90 / 0.28）
- `paintEvent` 内**零 `QFontMetrics.boundingRect` 调用**（值全部读缓存），并有视口裁剪 `if top+hh < -FADE_PX or top > h+FADE_PX: continue`
- 滚动驱动：**共享主帧时钟**（见（6）），非独立定时器

#### （5）音频频谱 `SpectrumWidget`
- 几何公式（`:615`）：`spec_y = max(0, h - CONTROL_BAR_H - SPEC_H - 16)`，其中 `CONTROL_BAR_H = 72`、`SPEC_H = 48`、左右留白 `SPEC_SIDE = 40`
  实测 `h = 1067` ⇒ `spec_y = 931` ✓ 公式吻合
- 柱体：`SPEC_BAR_W = 6`（`:29`）、`SPEC_GAP = 3`（`:30`）、`SPEC_PITCH = 9`、数量 `N = clamp((w + SPEC_GAP) // SPEC_PITCH, 16, 160)`（实测 1178px → 131 根）
- 峰值：`SPEC_PEAK_H = 2`（`:32`）、落峰 `SPEC_PEAK_FALL_PX_S = 4.0`（`:33`）⇒ 每帧下落 `4.0 × 33/1000 ≈ 0.13px`，**线性匀速**
- **数据源（必须如实说明）**：`tick()`（`:215`）用的是 **纯时间包络 + 多频段谐振模拟**：
  ```python
  energy = energy*0.86 + 0.14*(0.42 + 0.34*sin(t*1.7) + 0.24*random())
  beat   = 0.72*beat + 0.28*energy**2.1
  raw    = energy*(0.45+0.55*low)*(0.55+0.45*|sin(t*(1.3+low*3.4)+phase)|) + beat*low*0.55
  band   = min(1.0, 0.30 + min(1.0, raw**0.5 * 1.35))       # audio_view.py:237
  ```
  **不读取 mpv 音频能量、不读 PCM、不做 FFT**。
- **为什么曾出现 `max(_bars) = 0` / 1px 死线**：播放判定旧写法要求"本次位置必须**大于**上次"，采样抖动一次即永远为 False ⇒ 频谱恒零。现改为「`self._cur_pos > 0.05` 即视为播放」（成功实测量到 `max(_bars) = 1.0`）。
- **遗留观感问题（客观）**：`band` 加了 0.30 下限并乘 1.35 ⇒ 大量柱体直接饱和到 1.0，实拍呈"高度接近的蓝色带"，**长短落差不足**。

#### （6）帧时钟
`AudioView._frame = QTimer(interval=FRAME_MS=33)`（`:503-504`），**全组件唯一**帧时钟，统一调用 `spec.tick() + spec.update()`、`lyrics.step()`，并每 6 帧（≈200ms）取一次播放位置。⇒ 帧率 **30 FPS**（非 60）。

---

## 三、界面布局、层叠与抽屉手柄

### 3.1 `Stage` 组件树与 `resizeEvent`

`Stage`（`ui/stage.py:625` 起）直接子控件/覆盖层（按 `raise_overlays()` 的抬升顺序，**后者在上**）：

| 序 | 对象 | 说明 |
|---:|---|---|
| 1 | `video` / `image_view` / `audio_view` | 三种视口（同一时刻基本只显示一个） |
| 2 | `top_layer` / `middle_layer` / `bottom_layer` | 稀疏浮层；**图片模式**下三层统一 `WA_TransparentForMouseEvents=True`（避免吃掉中央点击/滚轮） |
| 3 | `osd_host` | OSD 胶囊（始终鼠标穿透） |
| 4 | `drawer_host` | 播放列表抽屉宿主 |
| 5 | `capsule_host` | 底部控制栏宿主 |
| 6 | `settings_host` | 设置面板浮层 |
| 7 | `drop_overlay` | 拖放提示 |
| 8 | `image_bar_host` | **最后抬升** ⇒ 图片控制栏可点击（曾有被 `capsule_host` 吃掉点击的事故） |
| — | `zone_left` / `zone_right`（**挂在视口上**） | 热区；`sync_hotspots()` 里摆位并抬升 |
| — | `drawer_handle`（**改挂舞台**） | 抽屉手柄，`sync_handle_geometry()` 定位 |
| — | `minimap`（**改挂舞台**） | 鸟瞰图，排在热区之后抬升（否则拖动被热区判成"切下一张"） |

⚠️ **`Stage.resizeEvent` 在文件中存在两份定义**：
- `stage.py:915`（较早，**被覆盖 ⇒ 死代码**）
- `stage.py:1171`（生效者，v1.0.31 起几何不变量写在这里）

生效版本内做两件事（无条件）：
```python
hd.setFixedSize(16, 56)
hd.setGeometry(handle_x, (self.height() - 56) // 2, 16, 56)   # handle_x 见下
av = getattr(self, "audio_view", None)
if av is not None and av.isVisible():
    av.setGeometry(0, 0, self.width(), self.height())
```

### 3.2 `DrawerHandle` 真实逻辑

- 类定义：`stage.py:502`；尺寸固定 `16×56`（`stage.py:890/910/927/1178` 多处 `setFixedSize(16, 56)`）
- `paintEvent`：**已按用户指令原样回滚**为 git `33d8aec`（v1.0.32）的原文（通透/磨砂/细线居中折线箭头），v1.0.33 我临时加的深色实心底板已删除
- 坐标计算函数：
  - `_handle_x_for(hw)`（`stage.py:826`）
  - `sync_handle_geometry()`（`:876`）
  - `sync_handle_follow()`（`:903`，抽屉动画逐帧回调，只动 X、Y 恒 `(height-56)//2`）
- **`x = 770` 漂移的真因（已定位并修复）**：抽屉收起时**并不会把 `width` 设为 0**，它是靠宿主裁剪滑出（收起时 `width()` 仍为 392）；而公式写成 `host_right - d.width()` ⇒ 永远多减 392px ⇒ 手柄落到屏幕内部。
- **当前判定依据（`stage.py:838` 与 `:1188-1190`）**：
  ```python
  if not (d.isVisible() and getattr(d, "is_open", lambda: False)()):
      return right                      # right = self.width() - hw   ⇒ 收起无条件贴最右
  return max(0, int(d.mapTo(self, QPoint(0, 0)).x()) - hw)         # 展开贴抽屉左外侧
  ```
  即**同时使用 `isVisible()` 与 `is_open()`**，不再使用 `width()` 判定。
- 实测（真机读取几何）：窗口态首页 `(1162, 308) 16×56`；全屏收起 `(1691, 505) 16×56`；全屏展开 `(1299, 505)`（抽屉宽 392，`1299 + 392 = 1691` = 抽屉左沿）

---

## 四、全局定时器与事件循环

| 所属类 / 文件 | 用途 | 间隔(ms) | 是否直接触发重绘/UI |
|---|---|---:|---|
| `AudioView._frame`（audio_view.py:503） | **主帧时钟**：频谱 tick + 歌词滚动 + 每 6 帧取位置 | **33** | 是（`spec.update()` / `lyrics.update()`） |
| `AudioView._bg_timer`（:507） | 背景模糊重建防抖（singleShot） | **200** | 是（重建后 `update()`） |
| `ImageViewerWidget._timer`（image_view.py:585） | 缩放/平移弹簧动画 | `SPRING_MS` | 是（`update()`） |
| `ImageViewerWidget._full_timer`（:573） | 原图解码防抖 | **300** | 否（只排队解码） |
| ~~`MainWindow._audio_timer`~~ | **不存在**（审计更正：磁盘上无此定时器；仅存 `_drive_audio_view()` 函数，`main_window.py:1070`，**没有任何定时器/信号调用它 ⇒ 死代码**） | — | — |
| `MainWindow._status_timer`（:2067） | 状态栏/进度刷新 | 500 | 是 |
| `MainWindow._edge_timer`（:2072） | 右边缘悬停呼出抽屉（图片/音频模式**已停用**） | 120 | 是 |
| `MainWindow._hide_timer`（:2062） | 胶囊自动隐藏 | 配置项 | 是（淡出） |
| `MainWindow._osd_timer`（:2079） | OSD 淡出 | 70 | 是 |
| `MainWindow._long_timer`（:2050） | 长按快进判定 | 配置项 | 是 |
| `MainWindow._rewind_timer`（:2055） | 长按回退 | 60 | 是（seek） |
| `ControlBar._preview_timer`（control_bar.py:275） | 进度条悬停缩略图防抖 | **130** | 是（弹 `PreviewPopup`） |
| `SettingsPanel._apply_flash`（settings_panel.py:266） | 设置生效闪烁 | — | 是 |
| `widgets.py` 若干 `_hide_timer/_hide`（1523/1600） | 浮层自动隐藏 | — | 是 |
| `PreviewWorker(QThread)`（control_bar.py:89） | 抽帧线程（**唯一 QThread**） | — | 否（经信号回主线程） |
| `QThreadPool` ×3（image_view.py:597/601/605） | 底图(2) / 预加载(1) / 原图(1) | — | 否（信号回主线程） |
| `QPropertyAnimation` ×11 | 抽屉宽度、悬停淡入淡出、音量旋钮、鸟瞰图淡入等 | 时长各异 | 是 |

**潜在隐患（客观陈述）**：
1. `AudioView` 的子控件（歌词/频谱）为**透明控件** ⇒ 其 `update()` 会连带父级 `AudioView` 重绘；父级绘制已是单次 `drawPixmap`，故成本可控，但**仍未做严格局部失效**。
2. `MainWindow` 中 7 个 QTimer 并存（500/200/120/70/60/配置项），**存在与其他刷新重复触发同一 UI 更新的可能**（例如 `_audio_timer` 与 `AudioView._frame` 都走播放位置）。
3. 主帧时钟为 **30 FPS**，与"60Hz/144Hz 丝滑"的目标存在差距。
4. `ipc.py` 的 `eval` 服务端默认 **3s** 超时（`req.get("timeout", 3.0)`），耗时自检必须显式传 `timeout`，否则误报"无响应"。

---

## 五、遗留缺陷、技术债与未闭环问题（客观清算）

### 5.1 抽屉手柄联动与坐标漂移
- **已修复**：`x = 770` 漂移（改用 `is_open()` 判定，实测收起 1691 / 展开 1299）；`paintEvent` 回滚为原版外观。
- **未闭环**：
  - `Stage.resizeEvent` **两份定义**（915 死 / 1171 生效），死代码未清理，极易再次踩坑；
  - 手柄定位目前**同时**散落在 `resizeEvent`、`sync_handle_geometry`、`sync_handle_follow`、`set_hotspots_enabled` 等多处，**仍非单一真源**；
  - 抽屉动画期间手柄随动的平滑度**未经真机录像/逐帧核验**。

### 5.2 音频频谱
- **已修复**：位置（实测 `spec_y = 931 = 1067-72-48-16`）、可见性（`raise_()`）、播放判定（`_cur_pos > 0.05`）。
- **未闭环（硬伤）**：
  1. **数据源是时间包络模拟，不是真实音频频谱**（无 mpv 能量/PCM/FFT）⇒ 与音乐节奏**无因果对应**；
  2. `band` 下限 0.30 + ×1.35 ⇒ **多数柱体饱和**，实拍为"高度接近的蓝带"，**缺 12~42px 的长短落差**；
  3. 采样间隔：`spec.update()` 每 33ms 一次（30 FPS），非用户要求的 40 FPS。

### 5.3 歌词滚动、换行与间距
- **已修复**：`paintEvent` 内零文本测量（改读 `_main_h/_trans_h/_tops/_heights` 缓存）；长句折行（实测首行高度 96px = 两行）；组间 29px / 组内 6px 常量落地；0.40 黄金线与 40px 渐隐落地。
- **未闭环**：
  - 滚动由 **30 FPS** 帧时钟驱动（非 60 FPS），"丝滑"未逐帧核验；
  - **双语组（同时间戳两行）从未用真实双语 LRC 出图验证**；
  - 非当前行透明度取 0.40（用户曾要求 0.35、后又给 0.40），**数值存在多次往复，以代码 0.40 为准**；
  - 极长文本（>2 行）与右栏窄宽度下的表现未做边界测试。

### 5.4 其它审计发现
1. **`Stage.resizeEvent` 重复定义**（915 / 1171）——死代码；
2. **`AudioCard` 类仍保留**（`stage.py:248`）但已无实例化（v1.0.27 物理销毁），属保留兼容的死代码；
3. **`image_view.py:290/419` 两处 `QPropertyAnimation(self, b"fadeT")`** 存在于不同类（鸟瞰图/热区），需注意同名属性重复；
4. **`mutagen` 未安装** ⇒ `lyrics.py` 的 `_embedded_via_mutagen()` 分支为**死分支**（本机从未执行）；内嵌歌词实际只能走 ffprobe；
5. `packaging/setup.spec` 的 `hiddenimports=[]`、`binaries=[]`：PIL 依赖 PyInstaller 内置 hook（已确认生效），但**若上游 hook 变更，Pillow 会静默缺失** ⇒ 代码会静默回落到 `QImageReader`（`_pil_decode` 返回 None），**性能特性会无声退化**；
6. `image_view` 中 `QImageReader` 仍在两处使用：主线程取尺寸（µs 级，可接受）与 `_can_decode()` 格式探测（AVIF/HEIC/JXL 等 Qt 不支持的格式会 fast-fail + OSD 提示）；
7. `_hot_switch_to()` 仅覆盖**图片→图片**；视频/音频切换仍走完整 `open_path()`（含配置落盘与 mpv IPC）；
8. 仓库 `build/` 下留存大量 `_probe_*.py` / `_checks_*.py` / `_fix*.py` 补丁脚本与探针（历史调试资产），**未做归档清理**；
9. `docs/04-验收证据/` 中同时存在 `v1026_*`/`v1027_*`/`v1028_*`/`v1030_*`/`v1033_*` 多代截图，**命名与版本不对齐**（部分早期图与实际版本不符），作证据引用时需注意；
10. **无自动化测试**：`tests/` 目录不存在；所有验证依赖 `build/_probe_*.py` + `ipc eval` 临时脚本，不可回归、不可 CI。

---

## 六、附：本轮审计未做（明确声明）

1. **未修改任何业务代码**（仅新增本文件 `PROJECT_STATUS_AUDIT.md`）；
2. **未编译、未打包、未推送、未打新 Tag**；
3. 未做任何真机运行验证 —— 本文所列"实测"数据均来自**本次会话此前已完成并记录**的运行结果，正文中已逐条标注来源语境；
4. 以下项目**没有证据支持"已修复"**，故列为未闭环：频谱真实数据源、60FPS 歌词滚动、双语组实拍、音频→视频切换回归、手柄动画随动平滑度、多显示器 DPI 切换。
