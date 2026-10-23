# 至简Player 设置面板分类与控件清单（v1.0.49 候选）

> 本文档描述 **v1.0.49 候选版（工作区未提交）** 的现行面板结构 —— 7 分类、无底部三按钮、主题合并为一行分段胶囊、控件现代化。
> 上一版基于 v1.0.48（`git 14d846c`）的文档已被**本文档覆盖**；历史基线见 §4 的逐项对照。

| 项 | 值 |
| --- | --- |
| 版本基线 | **v1.0.49 候选（工作区未提交）**；`__version__ = 1.0.49`（工作区已改、未提交）；`CONFIG_VERSION = 8`（`config.py` 本轮新增登记 1 键：`lyric.trans_font_family`） |
| 代码基线 | `git main` @ `14d846c` + 工作区未提交改动（`__init__.py` / `settings_panel.py` / `audio_view.py` / `main_window.py` 等 8 个文件） |
| 扫描对象（主体） | `src/zhijian/ui/settings_panel.py`（**1827 行**，补丁复核快照 md5 `5bfb8ff0…`；初版扫描快照为 1796 行 / md5 `3b71bfa8…`，差异仅为行号位移 + **两处空卡删除**，**46 处控件工厂调用、51 个设置行、9 个删除项复核一致**）、`src/zhijian/config.py`（532 行，md5 `b5fa6e4c…`，新增 `lyric.trans_font_family` 登记）、`src/zhijian/ui/audio_view.py` |
| 交叉核对 | `ui/theme.py`（主题清单，未改）、`ui/widgets.py`（控件基类）、`app.py`（实时生效链）、`ui/main_window.py`（运行时读取点）、`ui/subtitle_panel.py`、`mpv/player.py` |
| 采集方式 | **纯只读静态扫描**：AST 提取控件工厂调用（**46 处**）+ 手写控件 5 键 + `config.DEFAULTS` 反射（**102 键**）+ grep 逐键定位读取点 |
| 未做的事 | 未启动播放器进程、未截图、未改任何产品代码、未 commit/push；**只读扫描** |
| 扫描时间 | 2026-10-23（Asia/Shanghai） |

**统计速览**：7 个分类 · **51 个设置项**（v1.0.48 为 60：本轮 −9 行、−1 行主题合并、+1 行译文字体）· **26 个快捷键动作 × 2 槽位 = 52 个按键捕获框** · 合计 **77 行**；设置项控件 **9 种类型**：`ToggleSwitch` 17 / `NoWheelComboBox` 18 / `NoWheelSlider` 7 / `NoWheelDoubleSpinBox` 3 / `NoWheelSpinBox` 1 / `NoWheelSlider`（`_float_slider` 浮点包装）1 / 颜色控件（`QToolButton` 色块 + `QLineEdit` + `QColorDialog`）2 / `QLineEdit` 1 / `QToolButton` 分段胶囊 1。

---

## 0. 面板骨架（先建立空间感）

```
SettingsPanel(GlassFrame)  body 860×650（min 700×440 / 左栏 168 / 内容内边距 26）
├─ 左栏 168px
│   ├─ 标题「设置」（role=h1 / 14px Medium）
│   └─ 导航 QListWidget：7 项，图标 18px + 文本（选中 accent / 未选中 text_dim）
│       ├─ 1 常规与外观   general   icon: sliders
│       ├─ 2 视频与播放   playback  icon: play
│       ├─ 3 音乐与歌词   music     icon: music_note
│       ├─ 4 图片查看     image     icon: palette
│       ├─ 5 字幕与声音   subtitle  icon: subtitle
│       ├─ 6 快捷键       hotkeys   icon: keyboard
│       └─ 7 关于         about     icon: info
├─ 右栏
│   ├─ 页标题（随导航切换）+ 右上关闭按钮 IconButton(close, 28px)
│   └─ QStackedWidget 7 页；每页 = QScrollArea + QVBoxLayout（零绝对坐标）
│       · 滚动区已做**透明化 + 上下 28px Alpha 羽化**（ScrollFeatherEffect，本轮之前的 task-111 新增）
│       └─ 二级分组 =「分组标题 + QFormLayout + 1px 细分割线」，组间距 24px（_card / _form_host）
└─ 底部「确定 / 应用 / 取消」按钮栏：**已删除**（btn_ok/btn_apply/btn_cancel/btn_reset 均置 None 仅作兼容）
```

| 机制 | 位置 | 说明 |
| --- | --- | --- |
| 分类唯一真源 | `settings_panel.py:45-53` 的 `SECTIONS` | `(导航键, 显示名, 图标名)`；导航项 / 页标题 / stack 索引全部按此顺序 |
| 页面装配唯一真源 | `_build_stack_pages()`（`:1147`） | `__init__` 与换肤重建共用；合并类分类用 `_merge_pages()` 拼成单滚动页 |
| 分类页组成 | 同上 | 常规与外观 = `_page_general` + `_page_ui`；字幕与声音 = `_page_subtitle` + `_page_audio`；其余 5 类各 1 页 |
| 即时生效 | `_set()`（`:1610-1623`） | 每次改动 → `cfg.set` → **300ms 防抖落盘** → `_apply_music_key`（音乐键直推 AudioView）→ `setting_changed` 信号（主窗口 + app 实时链） |
| 落盘防抖 | `_save_timer` / `_flush_save`（`:1602-1608`） | 拖一次滑条只在脱手 300ms 后写 1 次磁盘（`_save_count` 计数） |
| 换肤重建 | `reload_theme → _rebuild_safe → _rebuild_pages`（`:1095`） | 换肤后整块重建右侧页面（内联样式是构造时快照），保留当前页索引 |
| 面板可拖动 | `DRAG_BAND_H = 46`（`:349`） | 仅顶部 46px 标题带可起拖，边界夹在父容器内 |
| 懒构造 | `main_window._ensure_settings()` | 面板首次打开才创建，不在启动路径上 |
| 分区定位锚点 | `show_section(key)`（`:1668-1697`） | 自动化/QA 按分类键定位并返回可读证据 |
| 透明化兜底 | `make_transparent()` + `_transparent_sweep()`（`:1158`） | 面板内所有容器/滚动区/视口统一透明（修「页面整块纯白」） |

---

## 1. 导航总览（7 分类）

### 1.1 分类与职责

| # | 导航键 | 显示名 | 图标 | 职责（一句话） | 二级分组（卡片）名称 | 设置项数 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `general` | 常规与外观 | `sliders` | 应用级总开关（窗口记忆 / 单实例 / 外部打开 / OSD / 动画 / 缩略图）+ 外观（唯一一行主题分段胶囊、抽屉宽度、控制条延时）+ 退出确认 | ①（无标题卡片）<br>②「外观」<br>③「窗口」 | 12 |
| 2 | `playback` | 视频与播放 | `play` | 播放行为与性能：长按倍速、**播放与循环**（记忆进度 / 自动播放 / 默认循环方式）、跳转与音量档位、硬件解码 | ①「播放与循环」<br>②「跳转与音量」<br>③「解码与性能」 | 13 |
| 3 | `music` | 音乐与歌词 | `music_note` | 歌词排版（原词字体 / **译文字体** / 字号 / 字重 / 行距 / 滚动速度）+ 音频频谱（开关 / 律动速度）+ 宽屏特效（音瀑） | ①「歌词排版」<br>②「音频频谱」<br>③「宽屏特效」 | 9 |
| 4 | `image` | 图片查看 | `palette` | 看图参数：取景框位置、图片默认缩放、滚轮缩放步长 | ①「取景框」<br>②「图片查看」 | 3 |
| 5 | `subtitle` | 字幕与声音 | `subtitle` | 字幕渲染样式（字体 / 字号 / 位置 / 描边 / 颜色 / ASS 覆盖 / 粗斜体）+ 音频启动音量与输出设备 | ①「字幕」<br>②「配色」<br>③「样式覆盖」<br>④「声音」 | 14 |
| 6 | `hotkeys` | 快捷键 | `keyboard` | 26 个动作 × 主/副两槽位重绑定（含**表头**与**冲突红框**）+ 一键恢复默认快捷键 | 无卡片（标题 + 2 行说明 + **表头** + 26 行表单 + 「全部恢复默认快捷键」按钮） | 26 |
| 7 | `about` | 关于 | `info` | 版本与许可声明 + 打开配置/日志文件夹 + 恢复默认设置 | 无卡片（标题 + 静态文本 + 3 枚按钮） | 0 |

### 1.2 分类页的二级分组（卡片）一览

| 分类 | 卡片顺序 | 卡片内表单项（按显示顺序） |
| --- | --- | --- |
| 常规与外观 | ①（无标题） | 记忆窗口大小及位置 → 播放时自动隐藏控制条 → 鼠标靠近右边缘时呼出播放列表 → 允许开启界面动画 → 显示操作提示（OSD） → 启用进度条悬停缩略图预览 → 只允许运行一个播放器实例 → 从外部打开新文件时 →〔说明行〕 |
| 常规与外观 | ② 外观 | 外观主题（分段胶囊）→ 播放列表宽度 → 控制条自动隐藏延时 |
| 常规与外观 | ③ 窗口 | 关闭窗口时二次确认 |
| 视频与播放 | ① 播放与循环 | 长按触发阈值 → 长按模式 → 原倍速倍率 → 固定档位 →〔说明行〕→ 记忆播放进度 → 打开后自动播放 → 默认循环方式 |
| 视频与播放 | ② 跳转与音量 | 短跳步长 → 长跳步长 → 音量步长 → 倍速步长 → 最高倍速 →〔说明行〕 |
| 视频与播放 | ③ 解码与性能 | 硬件解码 |
| 音乐与歌词 | ① 歌词排版 | 原词字体 → 译文字体 → 歌词字号（当前行）→ 歌词字重 → 歌词行间距 → 滚动动画速度 |
| 音乐与歌词 | ② 音频频谱 | 频谱 → 律动速度 →〔说明行〕 |
| 音乐与歌词 | ③ 宽屏特效 | 音瀑 |
| 图片查看 | ① 取景框 | 取景框位置 |
| 图片查看 | ② 图片查看 | 默认缩放 → 滚轮缩放步长 |
| 字幕与声音 | ① 字幕 | 显示字幕 → 自动挂载同目录同名外挂字幕 (.srt/.ass/.vtt) → 字体 → 字号 → 垂直位置 → 描边粗细 |
| 字幕与声音 | ② 配色 | 文字颜色 → 描边颜色 |
| 字幕与声音 | ③ 样式覆盖 | ASS 特效字幕 → 加粗 → 斜体 |
| 字幕与声音 | ④ 声音 | 默认音量 → 启动时静音 → 输出设备 →〔说明行〕 |
| 快捷键 | —（无卡片） | 表头「动作 / 主快捷键 / 副快捷键」+ 26 个动作，每个两行输入框（主键 / 副键） |
| 关于 | —（无卡片） | 静态说明文本 + 3 枚按钮（见 1.3） |

### 1.3 「关于」页三个按钮与行为

| 按钮文案 | 控件类型 | 行为（代码事实） |
| --- | --- | --- |
| 打开配置文件夹 | `GlassButton`（`QPushButton` 子类，自绘矢量圆角，role=ghost，高 34） | 取 `os.path.dirname(cfg.path)` → `os.makedirs(exist_ok=True)` → `QDesktopServices.openUrl(QUrl.fromLocalFile(dir))`。默认 `%APPDATA%\ZhiJian\ZhiJianPlayer`；便携模式（存在 `portable.txt`）下为程序所在目录 |
| 打开日志文件夹 | `GlassButton`（同上） | **与上一枚共用同一个回调 `_open_dir()`**（`:1529` 的 for 循环把两枚按钮都连到 `_open_dir`）。日志 `zhijian.log` 与 `config.json` 同目录（`app.setup_logging(log_dir=os.path.dirname(cfg.path))`）⇒ **实际打开的目录相同且正确**，仅文案暗示两个目录 |
| 恢复默认设置 | `GlassButton`（同上，tooltip「仅重置设置项（不会清空播放列表/最近文件）」） | `_on_reset()`（`:1767`）：按 `config.DEFAULTS` 分区逐个 `cfg.reset_section()`，跳过 `keep = {recent, playlist, session, window, last_media, media}`（当前实际存在 `recent` 一区 ⇒ **最近文件列表保留**）→ 立即 `cfg.save()` → 清 `_dirty` → 发 `reset_requested`（主窗口重放 loop_mode / 长按阈值 / 控制条延时 / 抽屉宽度）→ `refresh_hotkeys()` → `QTimer.singleShot(0, _rebuild_safe)` **整页重建**，控件立即刷新为默认值 |

---

## 2. 逐分类明细表（每分类一节，逐项列全）

**列含义**：

| 列 | 说明 |
| --- | --- |
| 二级分组 | 该设置项所在卡片的分组标题；「（无标题）」= `_card("")` 未命名卡片 |
| 控件类型 | **实际 Qt 类名**（见 §3.2 的类型清单），不写笼统的「下拉/输入」 |
| 配置键 | `config.json` 中的点号路径；快捷键为 `hotkeys.<动作>[0]/[1]` |
| 默认值 | 取 `config.DEFAULTS`（CONFIG_VERSION=8，本轮未改）实际值；标注「面板默认」者为代码内联默认（未登记 DEFAULTS） |
| 是否实时生效 | `✅ 立即` = 改完马上作用；`◐ 条件` = 下次开文件/切模式等时机；`▲ 重启` = 仅启动路径读取；`✖ 无效` = 半失效或读取方被硬编码覆盖 |

### 2.1 常规与外观（`general`，12 项 / 12 键）

| 二级分组 | 选项名称（UI 显示文本） | 控件类型 | 配置键 | 取值范围 / 候选选项 | 默认值 | 是否实时生效 |
| --- | --- | --- | --- | --- | --- | --- |
| （无标题） | 记忆窗口大小及位置 | `ToggleSwitch` | `ui.remember_window` | 开 / 关 | `true` | ▲ 重启（启动 `_restore_geometry` 读；关闭 `_store_geometry` 写） |
| （无标题） | 播放时自动隐藏控制条 | `ToggleSwitch` | `ui.capsule_autohide` | 开 / 关 | `true` | ✅ 立即（`_on_setting_changed`） |
| （无标题） | 鼠标靠近右边缘时呼出播放列表 | `ToggleSwitch` | `ui.drawer_edge_hover` | 开 / 关 | `true` | ✅ 立即（鼠标采样时实时读） |
| （无标题） | 允许开启界面动画 | `ToggleSwitch` | `ui.animations` | 开 / 关 | `true` | ✅ 立即（`_on_setting_changed`） |
| （无标题） | 显示操作提示（OSD） | `ToggleSwitch` | `ui.osd` | 开 / 关 | `true` | ✅ 立即（`_on_setting_changed` + 4 处实时读） |
| （无标题） | 启用进度条悬停缩略图预览 | `ToggleSwitch` | `ui.preview_thumb` | 开 / 关 | `true` | ✅ 立即（`capsule.set_preview_enabled`） |
| （无标题） | 只允许运行一个播放器实例 | `ToggleSwitch` | `ui.single_instance` | 开 / 关 | `true` | ▲ 重启（启动时单实例判定） |
| （无标题） | 从外部打开新文件时 | `NoWheelComboBox` | `ui.external_open_mode` | `switch`=在当前窗口切换播放（默认）<br>`new_window`=启动新窗口并存播放 | `switch` | ▲ 重启（`app.external_open_mode` 启动读） |
| 外观 | 外观主题 | 两个 `QToolButton`（checkable 分段胶囊：左右各 8px 外圆角、选中填 accent、`autoExclusive=False`） | `ui.theme` | **仅两项**：浅色玻璃（`glass-light`）/ 深色玻璃（`glass-dark`） | `glass-dark` | ✅ 立即（`theme_requested` → `MainWindow.apply_theme` 落盘 `ui.theme`） |
| 外观 | 播放列表宽度 | `NoWheelSlider` + 数值 `QLabel`（58px 宽） | `ui.drawer_width` | 280–560，步 1，显示「px」 | `392` | ✅ 立即（本轮新增接线：`drawer.set_target_width` + `stage.sync_handle_geometry` 手柄随动） |
| 外观 | 控制条自动隐藏延时 | `NoWheelSlider` + 数值 `QLabel` | `ui.capsule_autohide_ms` | 800–8000，步 1，显示「ms」（运行时接线下限夹到 ≥400ms） | `2800` | ✅ 立即（本轮新增接线：`_hide_timer.setInterval`） |
| 窗口 | 关闭窗口时二次确认 | `ToggleSwitch` | `ui.confirm_exit` | 开 / 关 | `false` | ✅ 立即（关窗事件实时读 → `app.ConfirmExitDialog`） |

### 2.2 视频与播放（`playback`，13 项 / 13 键）

| 二级分组 | 选项名称（UI 显示文本） | 控件类型 | 配置键 | 取值范围 / 候选选项 | 默认值 | 是否实时生效 |
| --- | --- | --- | --- | --- | --- | --- |
| 播放与循环 | 长按触发阈值 | `NoWheelSpinBox` | `playback.long_press_threshold_ms` | 120–1200，步 10，后缀「 ms」 | `300` | ▲ 重启（QTimer 间隔只在初始化 / 重置时设置） |
| 播放与循环 | 长按模式 | `NoWheelComboBox` | `playback.long_press_mode` | `multiply` 原倍速基础快进（默认）<br>`fixed` 固定档位快进 | `multiply` | ✅ 立即（每次长按实时读） |
| 播放与循环 | 原倍速倍率 | `NoWheelDoubleSpinBox` | `playback.long_press_multiplier` | 1.1–8.0，步 0.1，后缀「 倍」 | `2.0` | ✅ 立即（`fixed` 模式下控件自动置灰） |
| 播放与循环 | 固定档位 | `QLineEdit` 自由文本 + 右侧解析预览 `QLabel` | `playback.long_press_tiers` | 逗号 / 中文逗号 / 分号 / 空格分隔的正数；解析后升序去重；大于最高倍速的档会被夹住 | `"2, 5, 10"` | ✅ 立即（`multiply` 模式下控件自动置灰） |
| 播放与循环 | 记忆播放进度 | `ToggleSwitch` | `playback.remember_position` | 开 / 关 | `true` | ◐ 下次开文件 / 写进度时读取（**本轮从「常规与外观」移入**） |
| 播放与循环 | 打开后自动播放 | `ToggleSwitch` | `playback.auto_play` | 开 / 关 | `true` | ◐ 下次开文件（`app._on_file_loaded` 按它决定是否暂停；**本轮移入**） |
| 播放与循环 | 默认循环方式 | `NoWheelComboBox` | `playback.loop_mode` | `sequence` 顺序播放<br>`loop` 列表循环<br>`single` 单曲循环<br>`random` 随机播放 | `sequence` | ◐ 下次开文件或播完一轮（底栏循环图标下次打开时同步；**本轮移入**） |
| 跳转与音量 | 短跳步长 | `NoWheelComboBox` | `playback.seek_step_small` | 3 秒 / 5 秒 / 10 秒（值存 int） | `5` | ✅ 立即（每次按键实时读） |
| 跳转与音量 | 长跳步长 | `NoWheelComboBox` | `playback.seek_step_large` | 15 秒 / 30 秒 / 60 秒（值存 int） | `30` | ✅ 立即 |
| 跳转与音量 | 音量步长 | `NoWheelComboBox` | `playback.volume_step` | 2 % / 5 % / 10 %（值存 int） | `5` | ✅ 立即 |
| 跳转与音量 | 倍速步长 | `NoWheelComboBox` | `playback.speed_step` | 0.1x / 0.25x / 0.5x（值存 float） | `0.25` | ✅ 立即 |
| 跳转与音量 | 最高倍速 | `NoWheelDoubleSpinBox` | `playback.speed_max` | 2.0–64.0，步 1.0，后缀「 x」 | `16.0` | ✅ 立即 |
| 解码与性能 | 硬件解码 | `NoWheelComboBox` | `playback.hwdec` | `auto-safe` 自动（推荐）<br>`nvdec` NVIDIA NVDEC（零拷贝, 最快）<br>`d3d11va-copy` D3D11VA 回拷（兼容性好）<br>`dxva2` DXVA2（旧显卡）<br>`no` 纯软件解码 | `auto-safe` | ▲ 重启（`app._mpv_options` 在引擎创建时下发 mpv） |

### 2.3 音乐与歌词（`music`，9 项 / 9 键，全部即时生效）

| 二级分组 | 选项名称（UI 显示文本） | 控件类型 | 配置键 | 取值范围 / 候选选项 | 默认值 | 是否实时生效 |
| --- | --- | --- | --- | --- | --- | --- |
| 歌词排版 | 原词字体 | `NoWheelComboBox`（候选按 `QFontDatabase.families()` 动态构建） | `lyric.font_family` | `system` 跟随系统（内置 HarmonyOS Sans SC）；系统已安装则追加 HarmonyOS Sans SC / MiSans / Source Han Sans SC / 思源黑体 / Microsoft YaHei UI | `system` | ✅ 立即（`AudioView.apply_music_setting`） |
| 歌词排版 | 译文字体 | `NoWheelComboBox`（同源动态构建） | `lyric.trans_font_family` | `follow` 跟随原词字体 + 已安装字体候选（HarmonyOS Sans SC / MiSans / Source Han Sans SC / 思源黑体 / Microsoft YaHei UI）；无已安装项时退化为「跟随原词字体 / 跟随系统」两项 | `follow`（**已登记 `config.DEFAULTS["lyric"]`**） | ✅ 立即（`apply_music_setting` 同时认 `lyric.trans_font` 与 `lyric.trans_font_family`；**已加入 `MUSIC_KEYS`（10 键）⇒ 重启自动重放**） |
| 歌词排版 | 歌词字号（当前行） | `NoWheelSlider` + 数值 `QLabel` | `lyric.font_px` | 16–28，步 1，显示「px」 | `27` | ✅ 立即 |
| 歌词排版 | 歌词字重 | `NoWheelComboBox` | `lyric.weight` | `regular` 常规<br>`medium` 加粗（Medium）<br>`heavy` 特粗（Heavy） | `medium` | ✅ 立即 |
| 歌词排版 | 歌词行间距 | `NoWheelSlider` + 数值 `QLabel` | `lyric.step_pad` | 10–48，步 1，显示「px」 | `18` | ✅ 立即 |
| 歌词排版 | 滚动动画速度 | `NoWheelComboBox` | `lyric.scroll_ms` | 200 快 / 320 标准 / 500 慢（配置值存 **int**） | `320` | ✅ 立即 |
| 音频频谱 | 频谱 | `ToggleSwitch` | `spectrum.enabled` | 开 / 关 | `true` | ✅ 立即 |
| 音频频谱 | 律动速度 | `NoWheelComboBox` | `spectrum.rhythm` | `calm` 舒缓（α0.20 / ω30%）<br>`standard` 标准（α0.24 / ω45%）<br>`aggressive` 激烈（α0.28 / ω62%） | `standard` | ✅ 立即 |
| 宽屏特效 | 音瀑 | `ToggleSwitch` | `music.cascade` | 开 / 关（窄屏 <1320px 时音乐页做 4 列级联折叠动画） | `false` | ✅ 立即 |

### 2.4 图片查看（`image`，3 项 / 3 键）

| 二级分组 | 选项名称（UI 显示文本） | 控件类型 | 配置键 | 取值范围 / 候选选项 | 默认值 | 是否实时生效 |
| --- | --- | --- | --- | --- | --- | --- |
| 取景框 | 取景框位置 | `NoWheelComboBox` | `image.minimap_position` | `bottom_right` 右下角（默认，浮在控制栏上方）<br>`top_right` 右上角 | `bottom_right` | ◐ 切到图片模式时（`_apply_image_settings`） |
| 图片查看 | 默认缩放 | `NoWheelComboBox` | `ui.image_fit` | `fit` 适应窗口<br>`one` 原始大小 100%<br>`free` 上次的缩放比例 | `fit` | ✅ 立即（`app.apply_image_fit`） |
| 图片查看 | 滚轮缩放步长 | `NoWheelSlider`（`_float_slider`：滑块内部 ×100 用整数，写回 `/100`） | `ui.image_zoom_step` | 1.05–1.40 x（滑块值 105–140，显示「%.2fx」） | `1.12` | ✅ 立即（每次缩放实时读） |

### 2.5 字幕与声音（`subtitle`，14 项 / 14 键）

| 二级分组 | 选项名称（UI 显示文本） | 控件类型 | 配置键 | 取值范围 / 候选选项 | 默认值 | 是否实时生效 |
| --- | --- | --- | --- | --- | --- | --- |
| 字幕 | 显示字幕 | `ToggleSwitch` | `subtitle.enabled` | 开 / 关 | `true` | ✅ 立即（`SUB_STYLE_MAP` → `app.apply_subtitle`） |
| 字幕 | 自动挂载同目录同名外挂字幕 (.srt/.ass/.vtt) | `ToggleSwitch` | `subtitle.auto_load` | 开 / 关 | `true` | ✖ 半失效（只控 OSD 提示；实际挂载由 `mpv sub-auto=fuzzy` 常开） |
| 字幕 | 字体 | `NoWheelComboBox`（`setEditable(True)`，可手输任意字体名） | `subtitle.font_family` | Microsoft YaHei UI / Microsoft YaHei / SimHei / SimSun / HarmonyOS Sans SC / Source Han Sans SC / Noto Sans CJK SC / Arial / Segoe UI | `"Microsoft YaHei UI"` | ✅ 立即（`sub-font`） |
| 字幕 | 字号 | `NoWheelSlider` + 数值 `QLabel` | `subtitle.font_size` | 12–100，步 1，显示「px」（本轮由 `NoWheelSpinBox` 12–160 改为 Slider 12–100） | `46` | ✅ 立即（`sub-font-size`） |
| 字幕 | 垂直位置 | `NoWheelSlider` + 数值 `QLabel` | `subtitle.position` | 0–100，步 1，显示「%」（本轮由 SpinBox 改为 Slider） | `92` | ✅ 立即（`sub-pos`） |
| 字幕 | 描边粗细 | `NoWheelDoubleSpinBox` | `subtitle.border_size` | 0.0–8.0，步 0.2，后缀「 px」 | `2.4` | ✅ 立即（`sub-border-size`） |
| 配色 | 文字颜色 | **颜色控件**：`QToolButton` 色块 28×28（点击开 `QColorDialog`）+ `QLineEdit` 十六进制输入（非法输入红框且不落盘） | `subtitle.text_color` | `#RRGGBB` / `#AARRGGBB`；拾色器可选任意颜色 | `"#FFFFFF"` | ✅ 立即（`sub-color`） |
| 配色 | 描边颜色 | 同上（颜色控件） | `subtitle.border_color` | 同上 | `"#101010"` | ✅ 立即（`sub-border-color`） |
| 样式覆盖 | ASS 特效字幕 | `NoWheelComboBox` | `subtitle.ass_override` | `scale` 随窗口缩放（推荐）<br>`yes` 完全使用用户样式<br>`force` 强制用户样式（忽略特效）<br>`no` 保持原样式 | `scale` | ✅ 立即（`sub-ass-override`） |
| 样式覆盖 | 加粗 | `ToggleSwitch` | `subtitle.bold` | 开 / 关 | `false` | ✅ 立即（`sub-bold`） |
| 样式覆盖 | 斜体 | `ToggleSwitch` | `subtitle.italic` | 开 / 关 | `false` | ✅ 立即（`sub-italic`） |
| 声音 | 默认音量 | `NoWheelSlider` + 数值 `QLabel`（52px 宽） | `audio.volume` | 0–100，显示「%」 | `100.0` | ▲ 重启（语义即「启动音量」，启动时施加到引擎与胶囊） |
| 声音 | 启动时静音 | `ToggleSwitch` | `audio.mute` | 开 / 关 | `false` | ▲ 重启（启动时施加 mpv `mute`） |
| 声音 | 输出设备 | `NoWheelComboBox`（`showEvent` 每次打开面板动态枚举 mpv `audio-device-list`） | `audio.device` | `auto` 自动（跟随系统默认设备）+ 实际设备（名称/描述，取自 `engine.audio_devices()`；枚举失败为空列表并记 `_audio_devices_error`） | `auto` | ✅ 立即（`app.apply_audio_device` 下发 mpv `audio-device`） |

### 2.6 快捷键（`hotkeys`，26 个动作 / 52 个按键槽位）

**控件形态**：页面顶部为**表头**「动作 / 主快捷键 / 副快捷键」（三个 150px 宽 `QLabel`，`role=sub`）；每个动作一行，行内两个 `KeyCaptureEdit`（`QLineEdit` 子类：只读、`NoFocus`、点击进入「请按下新的按键…」等待态；真正抓键由**应用级事件过滤器**完成）。值类型 `list[str]`，第 0/1 项 = 主键/副键；按 `Delete` 清空当前槽位。**冲突标记**：构建页面时 `_mark_hotkey_conflicts()` 扫描全部槽位，同一按键被 ≥2 个动作/槽位占用 ⇒ 对应 `KeyCaptureEdit` 加 `border:1px solid #FF5A5A` 红框并写入 tooltip（刷新时机 = 页面构建/重建时）。页面底部另有一枚 `QPushButton`（role=ghost）「全部恢复默认快捷键」。

| 二级分组 | 动作（UI 显示文本） | 主快捷键（默认） | 副快捷键（默认） | 控件类型 | 配置键 | 是否实时生效 |
| --- | --- | --- | --- | --- | --- | --- |
| （无卡片） | 播放 / 暂停 | `Space` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.play_pause[0]/[1]` | ✅ 立即（每次按键实时读 `cfg`） |
| （无卡片） | 快进 5 秒（长按 → 倍速） | `Right` | `D` | `KeyCaptureEdit` ×2 | `hotkeys.seek_fwd_small[0]/[1]` | ✅ 立即 |
| （无卡片） | 快退 5 秒（长按 ← 倍速） | `Left` | `A` | `KeyCaptureEdit` ×2 | `hotkeys.seek_back_small[0]/[1]` | ✅ 立即 |
| （无卡片） | 快进 15 秒（按住连跳） | `Shift+Right` | `Shift+D` | `KeyCaptureEdit` ×2 | `hotkeys.seek_fwd_shift[0]/[1]` | ✅ 立即 |
| （无卡片） | 快退 15 秒（按住连跳） | `Shift+Left` | `Shift+A` | `KeyCaptureEdit` ×2 | `hotkeys.seek_back_shift[0]/[1]` | ✅ 立即 |
| （无卡片） | 快进 30 秒 | （空） | （空） | `KeyCaptureEdit` ×2 | `hotkeys.seek_fwd_large[0]/[1]` | ✅ 立即 |
| （无卡片） | 快退 30 秒 | （空） | （空） | `KeyCaptureEdit` ×2 | `hotkeys.seek_back_large[0]/[1]` | ✅ 立即 |
| （无卡片） | 音量 + | `Up` | `W` | `KeyCaptureEdit` ×2 | `hotkeys.volume_up[0]/[1]` | ✅ 立即 |
| （无卡片） | 音量 − | `Down` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.volume_down[0]/[1]` | ✅ 立即 |
| （无卡片） | 静音 | `M` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.mute[0]/[1]` | ✅ 立即 |
| （无卡片） | 全屏 / 退出全屏 | `F` | `Return` | `KeyCaptureEdit` ×2 | `hotkeys.fullscreen[0]/[1]` | ✅ 立即 |
| （无卡片） | 播放列表抽屉 | `L` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.playlist[0]/[1]` | ✅ 立即 |
| （无卡片） | 字幕设置浮层 | `V` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.subtitle_panel[0]/[1]` | ✅ 立即 |
| （无卡片） | 设置 | `Ctrl+,` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.settings[0]/[1]` | ✅ 立即 |
| （无卡片） | 打开文件 | `Ctrl+O` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.open_file[0]/[1]` | ✅ 立即 |
| （无卡片） | 打开文件夹 | `Ctrl+Shift+O` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.open_folder[0]/[1]` | ✅ 立即 |
| （无卡片） | 截图 | `S` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.screenshot[0]/[1]` | ✅ 立即 |
| （无卡片） | 媒体信息（左上角浮层） | `Tab` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.media_info[0]/[1]` | ✅ 立即 |
| （无卡片） | 加速 | `]` | `Ctrl+Up` | `KeyCaptureEdit` ×2 | `hotkeys.speed_up[0]/[1]` | ✅ 立即 |
| （无卡片） | 减速 | `[` | `Ctrl+Down` | `KeyCaptureEdit` ×2 | `hotkeys.speed_down[0]/[1]` | ✅ 立即 |
| （无卡片） | 恢复正常速度 | `Backspace` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.speed_reset[0]/[1]` | ✅ 立即 |
| （无卡片） | 上一个文件 | `PageUp` | `Ctrl+Left` | `KeyCaptureEdit` ×2 | `hotkeys.prev_file[0]/[1]` | ✅ 立即 |
| （无卡片） | 下一个文件 | `PageDown` | `Ctrl+Right` | `KeyCaptureEdit` ×2 | `hotkeys.next_file[0]/[1]` | ✅ 立即 |
| （无卡片） | 上一帧 | `,` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.frame_back[0]/[1]` | ✅ 立即（仅视频模式生效） |
| （无卡片） | 下一帧 | `.` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.frame_fwd[0]/[1]` | ✅ 立即（仅视频模式生效） |
| （无卡片） | 窗口置顶 | `Ctrl+T` | （空） | `KeyCaptureEdit` ×2 | `hotkeys.always_on_top[0]/[1]` | ✅ 立即 |

### 2.7 关于（`about`，0 个设置项 + 3 枚按钮）

| 二级分组 | 元素 | 控件类型 | 配置键 | 内容 / 行为 | 默认值 | 是否实时生效 |
| --- | --- | --- | --- | --- | --- | --- |
| （无卡片） | 版本与许可说明块 | `QLabel`（`role=sub`，wordWrap，最小高 180） | — | 版本 `__version__`、三合一说明、渲染管线、界面参考、字体许可声明、图标来源、ffmpeg 抽帧、便携声明 | — | —（静态） |
| （无卡片） | 打开配置文件夹 | `GlassButton`（role=ghost，高 34） | — | 打开 `dirname(config.json)`；默认 `%APPDATA%\ZhiJian\ZhiJianPlayer`，便携模式为程序目录 | — | 点击即执行 |
| （无卡片） | 打开日志文件夹 | `GlassButton`（role=ghost，高 34） | — | 与上一枚共用 `_open_dir()`，打开的也是 `dirname(config.json)`（`zhijian.log` 同目录） | — | 点击即执行 |
| （无卡片） | 恢复默认设置 | `GlassButton`（role=ghost，高 34） | — | 按分区重置设置项（保留 `recent` 最近文件）→ 落盘 → 重建页面立即刷新 | — | 点击即执行（详见 §1.3） |

---

## 3. 统计汇总

### 3.1 每分类项数与实时生效分布

| 分类 | 设置项数 | 配置键数 | ✅ 立即 | ◐ 条件 | ▲ 重启 | ✖ 无效 |
| --- | --- | --- | --- | --- | --- | --- |
| 常规与外观 | 12 | 12 | 9 | 0 | 3 | 0 |
| 视频与播放 | 13 | 13 | 8 | 3 | 2 | 0 |
| 音乐与歌词 | 9 | 9 | 9 | 0 | 0 | 0 |
| 图片查看 | 3 | 3 | 2 | 1 | 0 | 0 |
| 字幕与声音 | 14 | 14 | 11 | 0 | 2 | 1 |
| 快捷键 | 26 | 26（×2 槽位） | 26 | 0 | 0 | 0 |
| 关于 | 0 | 0 | — | — | — | — |
| **合计** | **77 行** | **77 键** | **65** | **4** | **7** | **1** |

**读法**：`77 行` = 51 个设置项行 + 26 个快捷键动作行；`77 键` = 51 个设置键 + 26 个快捷键键（本轮 `ui.theme` 只剩 1 行，故行数 = 键数）。<br>**`✅ 立即 65 行（84.4%）`** 较 v1.0.48 的 66/86 进一步集中：本轮新增 `ui.drawer_width`、`ui.capsule_autohide_ms`、`lyric.trans_font_family` 三条实时接线；字幕 11 项经 `app.Application.on_setting_changed` 实时下发；音乐 9 项经 `AudioView.apply_music_setting` 实时施加。<br>**`✖ 无效 仅剩 1 行`**（`subtitle.auto_load` 半失效），4 个死键控件行已全部删除，详见 §5①。

### 3.2 控件类型分布（按设置项行计，共 51 行）

| 控件类型（实际类名） | 出现行数 | 实例/备注 | 分布（分类 → 行数） |
| --- | --- | --- | --- |
| `ToggleSwitch` | 17 | 每行 1 枚；40×22 自绘胶囊开关（`QAbstractButton` 子类） | 常规与外观 8 / 字幕与声音 5 / 视频与播放 2 / 音乐与歌词 2 |
| `NoWheelComboBox` | 18 | 每行 1 个（`QComboBox` 子类，未聚焦时滚轮不改值；字幕字体那个 `setEditable(True)`） | 视频与播放 7 / 音乐与歌词 5 / 字幕与声音 3 / 图片查看 2 / 常规与外观 1 |
| `NoWheelSlider` + 数值 `QLabel` | 7 | 每行 1 条水平滑条（最小宽 220/240）+ 1 个只读数值标签（52/58px） | 字幕与声音 3 / 常规与外观 2 / 音乐与歌词 2 |
| `NoWheelSlider`（`_float_slider` ×100 浮点包装） | 1 | 滑块内部走整数 105–140，写回时 `/100`（0.01 粒度） | 图片查看 1 |
| `NoWheelDoubleSpinBox` | 3 | 每行 1 个（`QDoubleSpinBox` 子类，固定宽 120、高 ≥30） | 视频与播放 2 / 字幕与声音 1 |
| `NoWheelSpinBox` | 1 | 每行 1 个（`QSpinBox` 子类） | 视频与播放 1 |
| 颜色控件（`QToolButton` 色块 + `QLineEdit` + `QColorDialog`） | 2 | 每行 1 套：28×28 色块点击拾色；`QLineEdit` 固定宽 110、最长 9 字符、非法值红框不落盘 | 字幕与声音 2 |
| `QLineEdit` | 1 | 「固定档位」自由文本 + 右侧解析预览 `QLabel` | 视频与播放 1 |
| `QToolButton`（分段胶囊，2 枚） | 1 | 两个 checkable `QToolButton`（左/右各 8px 外圆角、选中填 accent）= 浅色玻璃/深色玻璃 | 常规与外观 1 |
| **小计（设置项）** | **51** | — | — |
| `KeyCaptureEdit` | 26（行） | 26 个动作 × 2 槽位 = **52 个实例**（只读 `QLineEdit` 子类 + 应用级事件过滤器抓键 + 冲突红框） | 快捷键 26 |
| `QPushButton`（role=ghost） | 1 | 「全部恢复默认快捷键」 | 快捷键页脚 |
| `GlassButton`（`QPushButton` 子类，自绘矢量圆角） | 3 | 关于页：打开配置文件夹 / 打开日志文件夹 / 恢复默认设置 | 关于 |
| `QLabel` | 辅助 | 分组标题（`role=h1`）/ 说明行（`role=sub`）/ 页标题 / 滑块数值 / 快捷键表头（3 枚 150px 宽）—— 不算设置项 | 全部页 |

### 3.3 面板级固定元素（非设置项）

| 元素 | 控件类型 | 说明 |
| --- | --- | --- |
| 左栏标题「设置」 | `QLabel`（`role=h1`，14px Medium） | 构造时创建，不参与页面重建 |
| 分类导航 | `QListWidget`（objectName=`SettingsNav`，iconSize 18） | 7 项，选中图标 accent / 其余 text_dim |
| 页标题 | `QLabel`（`role=h1`） | 初值 `SECTIONS[0][1]`（=「常规与外观」），随导航切换 |
| 关闭按钮 | `IconButton("close")`（28px，icon 15px，tooltip「关闭 (Esc)」） | 点击发 `closed` → 主窗口 `toggle_settings(False)` |
| 快捷键表头 | 3 枚 `QLabel`（各 150px，`role=sub`） | 「动作 / 主快捷键 / 副快捷键」，仅快捷键页 |
| 卡片总数 | 15 个 `_card()` 分组 | v1.0.49 已删除「启动行为」「时间轴微调」两处空卡；静态复核 **空分组 = 0**（7 页均无空 `QFormLayout`） |
| 底部按钮栏 | — | **已删除**（`btn_ok/btn_apply/btn_cancel/btn_reset` 全部置 `None`） |

---

## 4. 附：与上一版（v1.0.48）的对应关系

### 4.1 分类级：7 分类保持不变

| 分类（导航键） | v1.0.48 | v1.0.49 候选 | 变化 |
| --- | --- | --- | --- |
| 常规与外观 `general` | 19 项 | **12 项** | 3 个播放项移出到「视频与播放」；删 3 行（图片间隔 / 初始宽 / 初始高）；主题两行合并为一行 |
| 视频与播放 `playback` | 12 项 | **13 项** | 移入 3 个播放与循环项；删 2 行（Shift 跳步长 / 预读缓存）；4 个步长改档位下拉 |
| 音乐与歌词 `music` | 9 项 | **9 项** | 单卡拆三卡（歌词排版 / 音频频谱 / 宽屏特效）；新增译文字体；删峰值点样式 |
| 图片查看 `image` | 4 项 | **3 项** | 删滚轮方向；滚轮缩放步长改 Slider |
| 字幕与声音 `subtitle` | 16 项 | **14 项** | 删微调步长 / 当前偏移；字号与垂直位置改 Slider；颜色改拾色控件；输出设备接真实枚举 |
| 快捷键 `hotkeys` | 26 项 | **26 项** | 内容不变；新增表头与冲突红框 |
| 关于 `about` | 0 项 + 3 按钮 | **0 项 + 3 按钮** | 不变 |
| **合计** | **86 行** | **77 行** | −9 行删除、−1 行主题合并、+1 行译文字体 |

### 4.2 设置项逐项迁移 / 变化

| 设置项 | v1.0.48 位置与形态 | v1.0.49 候选 |
| --- | --- | --- |
| 记忆播放进度 | 常规与外观 ·（无标题） | **视频与播放 · 播放与循环**（移入） |
| 打开后自动播放 | 常规与外观 ·（无标题） | **视频与播放 · 播放与循环**（移入） |
| 默认循环方式 | 常规与外观 · 启动行为 | **视频与播放 · 播放与循环**（移入；「单曲循环」文案由「单个循环」更名） |
| 外观主题 | 常规与外观 · 外观：两行（「强调色」双 `QToolButton` + 「主题」`NoWheelComboBox` 3 项） | **一行分段胶囊 `[浅色玻璃 / 深色玻璃]`**（`ui.theme`，旧候选 qq-*/aurora 彻底移除） |
| 字幕字号 | `NoWheelSpinBox` 12–160 | **`NoWheelSlider` 12–100**（滑块 + 数值标签） |
| 字幕垂直位置 | `NoWheelSpinBox` 0–100 | **`NoWheelSlider` 0–100**（滑块 + 数值标签） |
| 图片滚轮缩放步长 | `NoWheelDoubleSpinBox` 1.02–1.50 步 0.01 | **`NoWheelSlider`（`_float_slider` 浮点包装）1.05–1.40** |
| 短跳步长 / 长跳步长 / 音量步长 / 倍速步长 | 依次为 `NoWheelSpinBox` / `NoWheelSpinBox` / `NoWheelSpinBox` / `NoWheelDoubleSpinBox` | **全部改为 `NoWheelComboBox` 档位**（3/5/10 秒、15/30/60 秒、2/5/10 %、0.1/0.25/0.5x） |
| 文字颜色 / 描边颜色 | 色板 `QLabel` + `QLineEdit`（无校验、无拾色器） | **`QToolButton` 色块（点开 `QColorDialog`）+ `QLineEdit`（非法值红框且不落盘）** |
| 输出设备 | `NoWheelComboBox`，但候选列表从未填充（只有 auto） | `NoWheelComboBox` + **`showEvent` 每次打开面板真实枚举**（`engine.audio_devices()`） |
| 快捷键页 | 无表头、无冲突提示 | **表头「动作 / 主快捷键 / 副快捷键」+ 重复键红框（#FF5A5A）** |
| 译文字体 | （无） | **新增**：`lyric.trans_font_family`（`NoWheelComboBox`，默认 follow） |
| 频谱峰值点样式 | 音乐与歌词 ·「峰值点样式」 | **行删除**（键 `spectrum.peak_style` 保留且音频仍支持，默认 `none`） |
| 字幕微调步长 | 字幕与声音 · 时间轴微调 | **行删除**（键 `subtitle.step` 已列入 `DEAD_KEYS`，UI 与键均清） |
| 字幕当前偏移 | 字幕与声音 · 时间轴微调 | **行删除**（键 `subtitle.delay` 保留、启动仍下发 `sub-delay`，仅 UI 不再暴露） |
| 初始窗口宽度 / 高度 | 常规与外观 · 窗口 | **行删除**（键 `ui.window_w/h` 保留，启动还原几何 / 关闭回写仍用，仅 UI 不再暴露） |
| Shift 跳步长 | 视频与播放 · 跳转与音量 | **行删除**（键 `playback.seek_step_shift` 保留并仍被按键处理读取，默认 15s） |
| 图片自动播放间隔 | 常规与外观 · 启动行为 | **行删除**（`playback.image_duration` 死键，从 DEFAULTS 移除 + `DEAD_KEYS`） |
| 预读缓存 | 视频与播放 · 解码与性能 | **行删除**（`playback.cache_secs` 死键，同上） |
| 滚轮方向 | 图片查看 · 取景框 | **行删除**（`image.wheel_invert` 死键 + 视口侧硬编码，同上） |

### 4.3 删除清单（9 个 UI 行 + 1 次合并）

| # | 被删除行 | 原位置 | 配置键现状 |
| --- | --- | --- | --- |
| 1 | 图片自动播放间隔 | 常规与外观 · 启动行为 | `playback.image_duration`：已从 `DEFAULTS` 移除 + 列入 `DEAD_KEYS`（**UI 与键均清**） |
| 2 | 预读缓存 | 视频与播放 · 解码与性能 | `playback.cache_secs`：同上 |
| 3 | 滚轮方向 | 图片查看 · 取景框 | `image.wheel_invert`：从未登记 DEFAULTS，列入 `DEAD_KEYS` |
| 4 | 微调步长 | 字幕与声音 · 时间轴微调 | `subtitle.step`：已从 `DEFAULTS` 移除 + 列入 `DEAD_KEYS` |
| 5 | 当前偏移 | 字幕与声音 · 时间轴微调 | `subtitle.delay`：**键保留**（DEFAULTS 有、`SUB_STYLE_MAP` 仍下发），仅 UI 不再暴露 |
| 6 | 初始窗口宽度 | 常规与外观 · 窗口 | `ui.window_w`：**键保留**（启动还原 / 关闭回写），仅 UI 不再暴露 |
| 7 | 初始窗口高度 | 常规与外观 · 窗口 | `ui.window_h`：同上 |
| 8 | Shift 跳步长 | 视频与播放 · 跳转与音量 | `playback.seek_step_shift`：**键保留并仍被读取**（默认 15s） |
| 9 | 峰值点样式 | 音乐与歌词（原单卡） | `spectrum.peak_style`：**键保留且音频仍支持**（默认 none） |
| （合并） | 「强调色」+「主题」两行 | 常规与外观 · 外观 | `ui.theme` 合并为一行分段胶囊（不是删除；候选仅剩 glass-light / glass-dark） |

### 4.4 新增 / 升级清单

| # | 新增或升级 | 说明 |
| --- | --- | --- |
| 1 | 新增设置行「译文字体」 | `lyric.trans_font_family`，`NoWheelComboBox`，默认 follow（跟随原词字体） |
| 2 | 音乐页拆三卡 | 歌词排版（6 行）/ 音频频谱（2 行）/ 宽屏特效（1 行），取代原「音乐与歌词」单卡（9 行） |
| 3 | 视频页首卡更名扩容 | 「长按倍速」→「**播放与循环**」，接收移入的 3 个播放项 |
| 4 | 主题唯一一行分段胶囊 | `_theme_segment()`：两个 `QToolButton`，仅 浅色玻璃 / 深色玻璃 |
| 5 | 档位下拉 ×4 | 短跳 / 长跳 / 音量步长 / 倍速步长 由数值框改为固定候选下拉 |
| 6 | Slider 化 ×3 | 字幕字号、字幕垂直位置、图片滚轮缩放步长（含 `_float_slider` 浮点包装） |
| 7 | 颜色拾取升级 | `QColorDialog` 拾色 + 十六进制校验（非法红框且不落盘） |
| 8 | 快捷键表头 + 冲突红框 | `_hotkey_header()` + `_mark_hotkey_conflicts()` |
| 9 | 实时接线 ×3 | `ui.drawer_width`（抽屉立即变宽 + 手柄随动）、`ui.capsule_autohide_ms`（计时器立即更新）、`lyric.trans_font_family`（立即换译文字体） |
| 10 | 输出设备真实枚举 | `showEvent` → `engine.audio_devices()` → `populate_audio_devices()` |

### 4.5 配置层（`config.py` 本轮新增 1 处登记）

| 项 | 状态 |
| --- | --- |
| `CONFIG_VERSION` | **8**（未变） |
| `DEFAULTS` 扁平键 | **102 个**（101 → 102：新增 `lyric.trans_font_family = "follow"`） |
| `DEAD_KEYS` | **仍为 4 项**：`playback.cache_secs` / `playback.image_duration` / `subtitle.step` / `image.wheel_invert`（本轮只是把对应 UI 行删掉，剥离逻辑保留用于兼容旧配置） |
| 主题默认 | `ui.theme = "glass-dark"`（未变）；`LEGACY_THEME_MAP` / `normalize_theme()` 仍把历史主题名归一为 glass-light / glass-dark |
| 新增 UI 键 | `lyric.trans_font_family`：**已登记** `DEFAULTS["lyric"] = "follow"`（`config.py:103-105`），并已加入 `MUSIC_KEYS`（**10 键**，`settings_panel.py:56-59`）⇒ 重启自动重放 |
| 快捷键默认 | `volume_down = ["Down", ""]`（S 冲突已在上轮修复，本轮未动） |

---

## 5. 附注（本轮要求更新的三条 + 其他观察）

### 5.1 死键是否已全部清除？

**UI 侧：已全部清除。** 4 个死键对应的控件行已全部从面板消失：`playback.image_duration`（图片自动播放间隔）、`playback.cache_secs`（预读缓存）、`image.wheel_invert`（滚轮方向）、`subtitle.step`（微调步长）。<br>**配置层：`config.DEAD_KEYS` 仍保留 4 项** —— 它的作用只剩「启动时剥离旧 `config.json` 里残留的值」，属于兼容层而非 UI 缺陷；`config.py` 本轮未改动，因此 `DEAD_KEYS` 与 `normalize_config()` 保持原样。

另需知道：本轮删除的 9 行里，只有上述 **4 行是真死键**；另外 **5 行是「键保留、UI 不再暴露」仍生效的项** —— `subtitle.delay`（0.0，启动仍下发 `sub-delay`）、`ui.window_w`（1280）与 `ui.window_h`（760，启动还原几何仍读）、`playback.seek_step_shift`（15s，按键处理仍读）、`spectrum.peak_style`（none，音频侧仍支持）。它们不会造成「改了没反应」，而是「用户改不了」。

### 5.2 主题候选是否只剩两套？

**是。** 面板内唯一主题入口 `_theme_segment()`（`:982`）只渲染 **浅色玻璃（`glass-light`）/ 深色玻璃（`glass-dark`）** 两个 `QToolButton`，`config.normalize_theme()` 会把历史主题名归一；`theme.theme_names()` 当前也只返回 `["glass-light", "glass-dark"]`（`theme.py` 本轮未改）。旧的 `THEME_MODES`、`_accent_combo()`、`_theme_skin_combo()` 仍留在文件中但**已无调用点**，属于待清理的死代码（不影响 UI）。

### 5.3 输出设备是否已枚举？

**是。** `showEvent()`（`:1699`）在**每次打开设置面板**时取 `self.window().engine.audio_devices()`，把 `(name, description)` 交给 `populate_audio_devices()` 重建下拉；失败时记录 `_audio_devices_live = 0` 与 `_audio_devices_error`（可被探针读取）。`audio.device` 变更由 `app.apply_audio_device()` 实时下发 mpv `audio-device`。

### 5.4 其他观察（a / b 两项已在本轮修复，c~e 待排期）

| # | 观察 | 证据 | 建议 |
| --- | --- | --- | --- |
| a | ~~两个空分组仍渲染~~ **已修复（v1.0.49）** | `_page_general` 的 `_card("启动行为")` 与 `_page_subtitle` 的 `_card("时间轴微调")` **已整块删除**；静态复核：15 个 `_card()` 分组、**空分组 = 0**（7 页无空 `QFormLayout`） | 无需后续动作（已在 §1.1 / §1.2 / §3.3 同步为「不存在空分组」） |
| b | ~~`lyric.trans_font_family` 未登记 `config.DEFAULTS`、不在 `MUSIC_KEYS`~~ **已修复（v1.0.49）** | `DEFAULTS["lyric"]["trans_font_family"] = "follow"`（`config.py:103-105`，扁平键 101 → **102**）；`MUSIC_KEYS` 已含该键（**10 键**，`settings_panel.py:56-59`） | 跨进程重启实测重放成功（Lead 自测）；无需后续动作（§2.3 默认值 / §4.5 配置层已同步） |
| c | **快捷键冲突红框只在页面构建时刷新** | `_mark_hotkey_conflicts()` 仅在 `_page_hotkeys()` 末尾调用一次（`:1439`），`apply_captured_key()` 捕获后不重算 | 捕获新键后立刻再调一次 `_mark_hotkey_conflicts()`，用户不必重开面板才能看到冲突 |
| d | **关于页两枚目录按钮共用同一回调** | `:1529` for 循环把「打开配置文件夹」「打开日志文件夹」都连到 `_open_dir()`；日志与配置同目录 | 行为正确，可保持；若未来日志迁出，需拆成两个回调 |
| e | **`ui.accent_override` 仍是「已登记但无 UI、无读取」的键** | `config.DEFAULTS` 有（`""`），面板无控件，全仓无读取方 | 清理该键，或接成真正的自定义强调色 |

---

## 附录 A：采集与校验方法（可复跑）

| 步骤 | 做法 | 产出 |
| --- | --- | --- |
| ① 控件清单 | `ast` 解析 `settings_panel.py`，抓全部 `_check/_spin/_dspin/_combo/_slider_row/_slider_row_d/_color_edit/_float_slider` 调用 = **46 处工厂调用**；另补 5 个手写控件使用的键（`subtitle.font_family` / `audio.volume` / `audio.device` / `playback.long_press_tiers` / `ui.theme`） | **51 个配置键 / 51 行** |
| ② 默认值 | `import zhijian.config` 后把 `DEFAULTS` 扁平化 | **102 个键**（`CONFIG_VERSION=8`） |
| ③ 实时生效判定 | 对每个键在 `src/zhijian/**/*.py`（排除 `settings_panel.py`/`config.py`）搜索完整键名 + 读本轮新增接线（`main_window._on_setting_changed` 的 `ui.drawer_width`/`ui.capsule_autohide_ms`、`audio_view.apply_music_setting` 的 `lyric.trans_font_family`） | 65 / 4 / 7 / 1 |
| ④ 控件类型 | 直接读各 `_page_*()` 构造语句，以**实际实例化类名**为准 | §2 各行 |
| ⑤ 旧版对照 | `git status` 确认改动文件 + 逐页比对 v1.0.48 文档与当前代码 | §4 各表 |

**复跑核心命令**

```bash
# 1) 配置版本与默认键数（应输出 8 / 102）
python -c "import sys; sys.path.insert(0,'src'); from zhijian.config import CONFIG_VERSION, DEFAULTS; print(CONFIG_VERSION, sum(1 for _ in DEFAULTS))"
# 2) 当前可选主题（应输出 [glass-light, glass-dark]）
python -c "import sys; sys.path.insert(0,'src'); from zhijian.ui import theme; print(theme.theme_names())"
# 3) 死键清单（应输出 4 项）
python -c "import sys; sys.path.insert(0,'src'); from zhijian.config import DEAD_KEYS; print(DEAD_KEYS)"
# 4) 工作区改动范围（本轮为未提交改动）
git status --short
```

**口径声明**

1. 本文档为**只读扫描**产物：未启动播放器、未截图、未改任何产品代码；唯一写入的文件即本文档（覆盖上一版）。
2. 版本口径：扫描时工作区 `__version__` 已改为 `1.0.49`（未提交）；文档标题按 Lead 口径记为 **v1.0.49 候选**。
3. 「是否实时生效」按**工作区当前代码**的读取点判定：三处实时链（`app.Application.on_setting_changed` / `MainWindow._on_setting_changed` / `SettingsPanel._apply_music_key`）+ 各「用时实时读」点。
4. 行号基线为**扫描快照**（`settings_panel.py` 1796 行，md5 `3b71bfa8…`）；发送前复查时工作区已推进到 md5 `5fa54f49…`（1798 行），差异仅为行号 +2 位移（已复核 46 处工厂调用 / 17 个卡片 / 9 个已删项均与本文档一致）。工作区仍在被并行任务修改，若提交前再有改动，行号可能继续漂移，但**分类、控件类型、配置键、默认值**四项不受影响；引用代码时请优先用函数名（本文档已同时标注）。

