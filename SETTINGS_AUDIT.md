# 至简Player 设置界面全量选项与控件形态审计报告

`SETTINGS_AUDIT.md` · 看板 task-98（[Task-97] 导出《设置界面全量选项与控件形态审计报告》）

| 项 | 值 |
| --- | --- |
| 报告编号 | 看板 task-98；交付物 = 项目根目录 `SETTINGS_AUDIT.md` |
| 审计对象（主体） | `src/zhijian/ui/settings_panel.py`（1393 行）<br>`src/zhijian/config.py`（437 行） |
| 交叉核对文件 | `src/zhijian/ui/widgets.py`、`ui/theme.py`、`ui/main_window.py`、`ui/subtitle_panel.py`、`ui/audio_view.py`、`ui/playlist_drawer.py`、`app.py`、`mpv/player.py` |
| 仓库基线 | git `main` @ `a81f735`（v1.0.46）；**`settings_panel.py` 与 `config.py` 在工作区无未提交改动**（另 6 个文件有并行任务的未提交改动，不影响本审计的两个主体文件） |
| 审计方式 | **纯只读静态审计**：AST 解析控件工厂调用 + grep 全仓交叉读取点 + 反射 `config.DEFAULTS` + 只读抽样现有 `%APPDATA%\ZhiJian\ZhiJianPlayer\config.json` |
| 未做的事 | 未启动播放器进程、未截图、未运行任何写操作；未修改任何产品代码（本轮唯一新增文件即本报告） |
| 扫描时间 | 2026-10-22（Asia/Shanghai） |
| 统计口径 | 8 个分类导航 / **59 个配置键 · 60 个设置行**（`ui.theme` 有两个控件视图）/ **26 个快捷键动作 × 2 槽位 = 52 个按键捕获框** / `config.DEFAULTS` 91 个扁平键 |

> **行号口径**：`settings_panel.py`、`config.py` 的行号在 HEAD `a81f735` 上稳定可复现；`main_window.py`、`widgets.py` 正被并行任务修改，引用其行号时标注「扫描时」，并对关键结论同时给出函数名，便于复审时按函数名重新定位。

---

## 结论摘要（先看这一页）

| # | 结论 | 量化证据 | 严重度 |
| --- | --- | --- | --- |
| 1 | **9 个选项完全无效**（写进 `config.json`，全仓无读取方）+ 1 个半失效（只控制提示不控制行为） | 占 60 个非快捷键设置行的 **16.7%**（10/60）；见 §3.3 逐条 | **P0** |
| 2 | **14 个配置键未登记进 `config.DEFAULTS`**（含全部 9 个音乐/频谱键），默认值只活在 UI 层 | `config.py` 91 个扁平键 vs UI 引用 59 键，差集逐条核对；见 §2.10-A | **P1** |
| 3 | **23 处面板兜底默认值与 `config.DEFAULTS` 不一致**；其中 **10 处是布尔反向**（配置缺键时界面显示 OFF、运行时按 ON 工作） | AST 自动化比对；见 §2.10-B | **P1** |
| 4 | **主题下拉只列 3 项，7 个预设中 5 个在设置界面不可达**；当前主题不在下拉清单时下拉回落到「浅色」，**深色主题会显示成浅色** | `settings_panel.py:824,874-901` + `theme.py:192-319`；见 §3.3-12 | **P0** |
| 5 | **默认快捷键冲突 1 组**：`S` 同时绑给「音量 −（副键）」和「截图」，且分发链是 if/elif 顺序判定 ⇒ `S` 永远只降音量，截图键形同虚设 | `config.py:115,124` + `main_window.py:3994/4023` | **P0** |
| 6 | **「应用 / 取消 / 恢复默认设置」三处语义名不副实**：每次改动已即时落盘；取消不回滚；恢复默认会清空播放列表/窗口几何/主题，且界面控件不刷新 | `settings_panel.py:1256-1264, 1348-1393`；见 §3.5 | **P0** |
| 7 | **每拖一格滑条/每敲一个字符都写一次磁盘 JSON**（`_set()` 内无条件 `cfg.save()`） | `settings_panel.py:1256-1264`；见 §3.6-3 | **P1** |
| 8 | 分类归属错位：`playback.*` 有 4 项塞在「常规」页，「界面」页里放了「关闭时二次确认」，出现「强调色」标签却是明暗模式按钮 | §1.3 / §3.1 逐条 | **P1** |
| 9 | 缺失高频项：倍速下限、进度记忆最短时长、抽屉自动隐藏、字幕阴影/随窗缩放、4 个「有默认键但界面不可见」的快捷键 | §3.4 / 附录 B | **P1** |
| 10 | 「关于」页无任何可操作项（版本 + 静态说明），快捷键页 52 个捕获框无搜索/分组/冲突校验 | `settings_panel.py:1179-1203, 1106-1140` | P2 |

---

## 0. 审计方法与证据链

### 0.1 三个自动化步骤（可复跑）

| 步骤 | 手段 | 产出 |
| --- | --- | --- |
| ① 控件清单提取 | 用 `ast` 解析 `settings_panel.py`，抓取全部 `_check/_spin/_dspin/_combo/_slider_row/_slider_row_d/_color_edit` 调用（**55 处**），得到 (行号, 工厂, 配置键, UI 标签, 面板内联兜底默认值) | 55 行结构化清单 |
| ② 默认值底座 | `import zhijian.config` 后把 `DEFAULTS` 扁平化为点号路径 → **91 个键** + 实际值 | 91 行默认值表 |
| ③ 交叉比对 | 逐键判断「是否在 DEFAULTS / 面板兜底是否等于 config 默认值」；再用 grep 在全仓定位每个键的**真实读取点**（排除 `settings_panel.py`、`config.py` 自身），判定「是否实时生效」 | 14 个未登记键 + 23 处兜底不一致 + 每键读取点 |

补充第 ④ 步：只读抽样真实用户配置 `%APPDATA%\ZhiJian\ZhiJianPlayer\config.json`（version 7 / 98 个扁平键），用于证明「未登记键在真实配置里确实以缺键形态存在，因而 UI 会走面板兜底值」。

### 0.2 判读「是否实时生效」的四个符号

| 符号 | 含义 | 判定依据 |
| --- | --- | --- |
| ✅ 立即 | 改动即刻作用于运行时 | 读取点是**操作时实时读** `cfg.get()`，或 `_on_setting_changed()` / `AudioView.apply_music_setting()` 显式施加 |
| ◐ 条件 | 特定时机才生效 | 读取点在「下次打开文件 / 切到某模式 / 下一次播放结束」等路径 |
| ▲ 重启 | 仅重启程序后生效 | 读取点只在 **启动/初始化** 路径（如 `_start_timers`、`app._apply_subtitle_defaults`） |
| ✖ 无效 | 写配置但无人读，或读取方被硬编码覆盖 | 全仓 grep 该键 = 0 处读取；或读取处被写死常量 |

---

## 1. 分类导航树与职责边界

### 1.1 面板整体骨架（唯一真源：`SECTIONS`，`settings_panel.py:41-50`）

```
设置面板 SettingsPanel(GlassFrame)  body 860×650（min 700×440 / 侧栏 168 / 内边距 26，theme.M:524-529）
├─ 左栏 168px
│   ├─ 标题「设置」（role=h1 / 14px Medium）
│   └─ 导航 QListWidget（8 项，图标 18px + 文字，未选中 text_dim / 选中 accent）
│       ├─ 常规        general    icon: sliders
│       ├─ 播放        playback   icon: play
│       ├─ 字幕        subtitle   icon: subtitle
│       ├─ 界面        ui         icon: palette
│       ├─ 声音        audio      icon: speaker
│       ├─ 音乐与歌词  music      icon: music_note     ★ task-26 从「界面/声音」拆出的独立分区
│       ├─ 快捷键      hotkeys    icon: keyboard
│       └─ 关于        about      icon: info
├─ 右栏
│   ├─ 页标题（随导航切换）+ 右侧关闭按钮 IconButton(close, 28px)
│   └─ QStackedWidget 8 页（每页 = QScrollArea 包一层 QVBoxLayout，零绝对坐标）
│       └─ 二级分组 =「分组标题 + QFormLayout + 1px 细分割线」，组间 24px（_card / _form_host）
└─ 底栏：「恢复默认设置」(link) ······ [确定](primary) [应用](ghost) [取消](ghost)  按钮 92×36
```

**实现要点（评审时值得知道的机制）**

| 机制 | 位置 | 说明 |
| --- | --- | --- |
| 分类顺序唯一真源 | `settings_panel.py:41-50` | `SECTIONS = [(key, 中文名, 图标名), ...]`，导航项、页标题、stack 索引全部按此顺序 |
| 页面构建两处 | `:410-417`（`__init__`）与 `:963-970`（`_rebuild_pages`） | **同一份 8 个 `_page_*()` 调用写了两遍**，新增/调整分类必须同步两处，否则换肤重建后会错位 |
| 换肤重建 | `reload_theme→_rebuild_safe→_rebuild_pages`（`:924-972`） | 换肤后**整块重建**右侧页面（因大量内联样式是构造时快照），并保留当前页索引 |
| 懒构造 | `main_window._ensure_settings()` | 面板首次打开才创建（不在启动路径上） |
| 可拖动 | `DRAG_BAND_H=46` / `mousePress/Move/Release`（`:267-339`） | 仅顶部 46px 标题带可起拖，边界夹在父容器内；拖动过会记 `_settings_dragged` |
| 分区定位锚点 | `show_section(key)`（`:1309-1338`） | QA/自动化按分类键定位并返回可读证据 |

### 1.2 八个分类的职责边界

| 分类 | 导航键 | 应有职责边界（应当放什么） | 实际承载的二级分组 | 现状越界 / 缺位（详见 §3.1） |
| --- | --- | --- | --- | --- |
| 常规 | `general` | 高频总开关 + 应用级行为（窗口记忆、单实例、外部打开）；不放具体播放参数、不放看图参数 | ①（无标题卡）10 项<br>②「看图」2 项<br>③「启动行为」2 项 | 越界：`playback.remember_position / auto_play / loop_mode / image_duration` 4 个播放参数混入；`image.*` 2 项自成一组却挂在常规页 |
| 播放 | `playback` | 播放行为：长按倍速、跳转/音量/倍速步长、解码与性能 | ①「长按倍速」4 项<br>②「跳转与音量」6 项<br>③「解码与性能」2 项 | 缺位：倍速下限、进度记忆最短时长未暴露；`playback.hwdec` / `playback.cache_secs` 两项**完全无实现**（§3.3-1/2） |
| 字幕 | `subtitle` | 字幕渲染样式 + 自动挂载 + ASS 覆盖策略 | ①「字幕」6 项<br>②「配色」2 项<br>③「时间轴微调」2 项<br>④「样式覆盖」4 项 | 越界：`subtitle.delay`（当前偏移）是**会话内状态量**，持久化后会被当作每次启动的默认偏移；`subtitle.step` 无人读取（§3.3-6） |
| 界面 | `ui` | 主题/明暗、面板与抽屉几何、看图缩放、窗口初始尺寸与退出确认 | ①「外观」4 项<br>②「图片查看」2 项<br>③「窗口」3 项 | 越界：`ui.confirm_exit` 属交互确认不属外观；`ui.image_fit` 无人读取；「强调色」标签下实际渲染的是明暗模式按钮（§3.1-3） |
| 声音 | `audio` | 启动音量、启动静音、输出设备 | ①「声音」3 项（单卡片） | 缺位：3 项中「输出设备」完全无效（候选列表从未填充、值从未下发给 mpv，§3.3-9）；高频的「音量步长」却在播放页 |
| 音乐与歌词 | `music` | 歌词排版（字体/字号/字重/行距/滚动速度）+ 频谱开关与样式 + 宽屏音瀑 | ①「音乐与歌词」9 项（**单卡片**） | 颗粒度问题：音瀑（宽屏特效）与歌词排版混在同一卡片；「峰值点样式/律动速度」是频谱细节，建议再拆二级分组（§3.1-6） |
| 快捷键 | `hotkeys` | 26 个动作 × 主/副两槽位的按键重绑定 + 一键恢复默认 | 无卡片：标题 + 2 行说明 + 26 行表单 + 「全部恢复默认快捷键」按钮 | 缺位：4 个有默认键的动作不可见（`escape/stop/larger_ui/smaller_ui`）；无冲突检测、无搜索、无分组（§3.4-6） |
| 关于 | `about` | 版本、许可声明（HarmonyOS Sans 字体许可要求）、运行环境说明 | 无卡片：1 个静态多行 QLabel | 缺位：无任何可操作入口（打开配置目录/日志目录/检查更新）（§3.4-7） |

### 1.3 二级分组（卡片）与表单项的真实顺序

顺序即用户从上到下的阅读顺序；下表用于 UX 评审时对照「同一屏里是否混了不同语义」。

| 页 | 卡片标题 | 表单项（左侧标签原文，按显示顺序） |
| --- | --- | --- |
| 常规 | **（空标题，无分组名）** | 记忆窗口大小及位置 → 播放时自动隐藏控制条 → 鼠标靠近右边缘时呼出播放列表 → 允许开启界面动画 → 显示操作提示（OSD） → 启用进度条悬停缩略图预览 → 只允许运行一个播放器实例 → 从外部打开新文件时 →〔说明行〕→ 记忆播放进度 → 打开后自动播放 |
| 常规 | 看图 | 取景框位置 → 滚轮方向 →〔说明行〕 |
| 常规 | 启动行为 | 默认循环方式 → 图片自动播放间隔 →〔说明行〕 |
| 播放 | 长按倍速 | 长按触发阈值 → 长按模式 → 原倍速倍率 → 固定档位 →〔说明行〕 |
| 播放 | 跳转与音量 | 短跳步长 → 长跳步长 → Shift 跳步长 →〔说明行〕→ 音量步长 → 倍速步长 → 最高倍速 →〔说明行〕 |
| 播放 | 解码与性能 | 硬件解码 → 预读缓存 →〔说明行〕 |
| 字幕 | 字幕 | 显示字幕 → 自动挂载同目录同名外挂字幕 (.srt/.ass/.vtt) → 字体 → 字号 → 垂直位置 → 描边粗细 |
| 字幕 | 配色 | 文字颜色 → 描边颜色 |
| 字幕 | 时间轴微调 | 微调步长 → 当前偏移 →〔说明行〕 |
| 字幕 | 样式覆盖 | ASS 特效字幕 → 加粗 → 斜体 |
| 界面 | 外观 | 强调色（实为浅色/深色两个模式按钮）→ 主题（下拉）→ 播放列表宽度 → 控制条自动隐藏延时 |
| 界面 | 图片查看 | 默认缩放 → 滚轮缩放步长 |
| 界面 | 窗口 | 初始窗口宽度 → 初始窗口高度 → 关闭窗口时二次确认 |
| 声音 | 声音 | 默认音量 → 启动时静音 → 输出设备 →〔说明行〕 |
| 音乐与歌词 | 音乐与歌词 | 歌词字体 → 歌词字号（当前行）→ 歌词字重 → 歌词行间距 → 滚动动画速度 → 音瀑 → 频谱 → 峰值点样式 → 频谱律动速度 → 2 行说明 |
| 快捷键 | —（无卡片） | 26 个动作，每个两行输入框（主键 / 副键） |
| 关于 | —（无卡片） | 单块静态文本（版本/许可/管线/图标/抽帧/便携声明） |

---

## 2. 全量设置项逐条明细表

**图例**：`sp` = `settings_panel.py`，`cfg` = `config.py`，`mw` = `ui/main_window.py`（行号为扫描时），`app` = `app.py`，`av` = `ui/audio_view.py`，`mpv` = `mpv/player.py`。<br>「默认值」列取 **`config.DEFAULTS` 的实际值**；带 `†` 的键**未登记进 `config.DEFAULTS`**，其默认值只存在于 UI 层（`MUSIC_DEFAULTS`）或读取端兜底，详见 §2.10-A。

### 2.1 常规（`general`，14 项 / 14 键）

| 所属分类 | 选项名称（UI Label） | 配置键（Config Key） | 当前控件类型 | 取值范围 / 候选项 | 默认值 | 是否实时生效 | 读取 / 施加点（依据） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 常规 | 记忆窗口大小及位置 | `ui.remember_window` | 自绘胶囊开关 `ToggleSwitch`（40×22） | 开 / 关 | `true` | ▲ 重启 | `mw._restore_geometry`(4414) 启动读；`mw._store_geometry`(4437) 关闭读 |
| 常规 | 播放时自动隐藏控制条 | `ui.capsule_autohide` | `ToggleSwitch` | 开 / 关 | `true` | ✅ 立即 | `mw._on_setting_changed`(938) |
| 常规 | 鼠标靠近右边缘时呼出播放列表 | `ui.drawer_edge_hover` | `ToggleSwitch` | 开 / 关 | `true` | ✅ 立即 | `mw`(3340) 鼠标采样时实时读 |
| 常规 | 允许开启界面动画 | `ui.animations` | `ToggleSwitch` | 开 / 关 | `true` | ✅ 立即 | `mw._on_setting_changed`(936) |
| 常规 | 显示操作提示（OSD） | `ui.osd` | `ToggleSwitch` | 开 / 关 | `true` | ✅ 立即 | `mw._on_setting_changed`(934) + 另 4 处实时读 |
| 常规 | 启用进度条悬停缩略图预览 | `ui.preview_thumb` † | `ToggleSwitch` | 开 / 关 | `true`（读取端兜底）/ `false`（面板兜底） | ✅ 立即 | `mw._on_setting_changed`(930) → `capsule.set_preview_enabled` |
| 常规 | 只允许运行一个播放器实例 | `ui.single_instance` | `ToggleSwitch` | 开 / 关 | `true` | ▲ 重启 | `app`(369) 启动时单实例判定 |
| 常规 | 从外部打开新文件时 | `ui.external_open_mode` † | `NoWheelComboBox` 下拉 | `switch` 在当前窗口切换播放（默认）<br>`new_window` 启动新窗口并存播放 | `switch` | ▲ 重启 | `app.external_open_mode`(116) + `app`(367) 启动读 |
| 常规 | 记忆播放进度 | `playback.remember_position` | `ToggleSwitch` | 开 / 关 | `true` | ◐ 下次开文件 / 写入进度时 | `mw`(4156、4175) |
| 常规 | 打开后自动播放 | `playback.auto_play` | `ToggleSwitch` | 开 / 关 | `true` | ✖ 无效 | 全仓 grep = 0 处读取（仅 `cfg` 默认值与 `sp` 写入） |
| 常规 · 看图 | 取景框位置 | `image.minimap_position` † | `NoWheelComboBox` 下拉 | `bottom_right` 右下角（默认，浮在控制栏上方）<br>`top_right` 右上角 | `bottom_right` | ◐ 进入图片模式时 | `mw._apply_image_settings`(1085)，由 `_apply_kind_ui` 在切模式时调用 |
| 常规 · 看图 | 滚轮方向 | `image.wheel_invert` † | `NoWheelComboBox` 下拉 | `normal` 上滑放大（默认）<br>`invert` 上滑缩小（反向） | `normal` | ✖ 无效 | `mw`(1084) 被硬编码 `v.wheel_invert = False` **覆盖**（注释明说不再读取） |
| 常规 · 启动行为 | 默认循环方式 | `playback.loop_mode` | `NoWheelComboBox` 下拉 | `sequence` 顺序 / `loop` 列表循环 / `single` 单个循环 / `random` 随机 | `sequence` | ◐ 下次开文件或播完一轮 | `mw`(4109 设置 `loop-file`、4128 播完分支)；底栏循环图标只在 `mw`(582) 构造与重置(4217) 时同步 |
| 常规 · 启动行为 | 图片自动播放间隔 | `playback.image_duration` | `NoWheelSpinBox` 数字框（120px 宽） | 0–120，步 1，后缀「 秒」；0 = 不自动切换 | `0` | ✖ 无效 | 全仓 grep = 0 处读取 |

### 2.2 播放（`playback`，12 项 / 12 键）

| 所属分类 | 选项名称（UI Label） | 配置键（Config Key） | 当前控件类型 | 取值范围 / 候选项 | 默认值 | 是否实时生效 | 读取 / 施加点（依据） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 播放 · 长按倍速 | 长按触发阈值 | `playback.long_press_threshold_ms` | `NoWheelSpinBox` | 120–1200，步 10，后缀「 ms」 | `300` | ▲ 重启 | `mw._start_timers`(3230) 设 `QTimer` 间隔；重置(4218) 再设一次 |
| 播放 · 长按倍速 | 长按模式 | `playback.long_press_mode` | `NoWheelComboBox` | `multiply` 原倍速基础快进（默认）<br>`fixed` 固定档位快进 | `multiply` | ✅ 立即 | `mw._boost_target`(1584) 每次长按实时读 |
| 播放 · 长按倍速 | 原倍速倍率 | `playback.long_press_multiplier` | `NoWheelDoubleSpinBox` | 1.1–8.0，步 0.1，后缀「 倍」 | `2.0` | ✅ 立即（`fixed` 模式下控件自动置灰） | `mw._boost_target`(1594) |
| 播放 · 长按倍速 | 固定档位 | `playback.long_press_tiers` | `QLineEdit` 自由文本 + 右侧实时解析预览 `QLabel` | 逗号 / 中文逗号 / 分号 / 空格分隔的正数；解析后升序去重；大于最高倍速的档会被夹住 | `"2, 5, 10"` | ✅ 立即（`multiply` 模式下控件自动置灰） | `mw._boost_target`(1589) + `cfg.parse_speed_tiers`(271) |
| 播放 · 跳转与音量 | 短跳步长 | `playback.seek_step_small` | `NoWheelSpinBox` | 1–120，步 1，后缀「 秒」 | `5` | ✅ 立即 | `mw`(3884、3958、3963) 每次按键读 |
| 播放 · 跳转与音量 | 长跳步长 | `playback.seek_step_large` | `NoWheelSpinBox` | 5–600，步 5，后缀「 秒」 | `30` | ✅ 立即 | `mw`(3968、3971) |
| 播放 · 跳转与音量 | Shift 跳步长 | `playback.seek_step_shift` | `NoWheelSpinBox` | 1–120，步 1，后缀「 秒」 | `15` | ✅ 立即 | `mw`(3848) |
| 播放 · 跳转与音量 | 音量步长 | `playback.volume_step` | `NoWheelSpinBox` | 1–25，步 1，后缀「 %」 | `5` | ✅ 立即 | `mw`(3974、3978、4080) |
| 播放 · 跳转与音量 | 倍速步长 | `playback.speed_step` | `NoWheelDoubleSpinBox` | 0.05–1.00，步 0.05，后缀「 x」 | `0.25` | ✅ 立即 | `mw`(4010、4013) |
| 播放 · 跳转与音量 | 最高倍速 | `playback.speed_max` | `NoWheelDoubleSpinBox` | 2.0–64.0，步 1.0，后缀「 x」 | `16.0` | ✅ 立即 | `mw`(1495 夹取、1587 长按上限) |
| 播放 · 解码与性能 | 硬件解码 | `playback.hwdec` | `NoWheelComboBox` | `auto-safe` 自动（推荐）<br>`nvdec` NVIDIA NVDEC（零拷贝, 最快）<br>`d3d11va-copy` D3D11VA 回拷（兼容性好）<br>`dxva2` DXVA2（旧显卡）<br>`no` 纯软件解码 | `auto-safe` | ✖ 无效 | `mpv/player.py:132` 硬编码 `hwdec: auto-safe`；`app`(464) 只允许命令行 `--hwdec` 覆盖 |
| 播放 · 解码与性能 | 预读缓存 | `playback.cache_secs` | `NoWheelSpinBox` | 0–600，步 10，后缀「 秒」 | `30` | ✖ 无效 | `mpv/player.py:156` 硬编码 `cache-secs: 30`；全仓无该键读取 |

### 2.3 字幕（`subtitle`，13 项 / 13 键）

| 所属分类 | 选项名称（UI Label） | 配置键（Config Key） | 当前控件类型 | 取值范围 / 候选项 | 默认值 | 是否实时生效 | 读取 / 施加点（依据） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 字幕 | 显示字幕 | `subtitle.enabled` | `ToggleSwitch` | 开 / 关 | `true` | ▲ 重启 | `app._apply_subtitle_defaults`(658) 启动施加；`mw`(851) 仅在进入图片模式时重设 |
| 字幕 | 自动挂载同目录同名外挂字幕 (.srt/.ass/.vtt) | `subtitle.auto_load` | `ToggleSwitch` | 开 / 关 | `true` | ✖ 半失效 | `mw._auto_sub_notice`(1766) **只决定是否弹 OSD 提示**；真正挂载由 `mpv/player.py:172` `sub-auto=fuzzy` 常开，开关无法关闭 |
| 字幕 | 字体 | `subtitle.font_family` | **可编辑** `NoWheelComboBox`（可手输任意字体名） | Microsoft YaHei UI / Microsoft YaHei / SimHei / SimSun / HarmonyOS Sans SC / Source Han Sans SC / Noto Sans CJK SC / Arial / Segoe UI（内置字体经 `sub-fonts-dir` 暴露，选内置项真实可用） | `"Microsoft YaHei UI"` | ▲ 重启 | `app`(647) |
| 字幕 | 字号 | `subtitle.font_size` | `NoWheelSpinBox` | 12–160，步 1，后缀「 px」 | `46` | ▲ 重启 | `app`(648) |
| 字幕 | 垂直位置 | `subtitle.position` | `NoWheelSpinBox` | 0–100，步 1，后缀「 %」 | `92` | ▲ 重启 | `app`(655) |
| 字幕 | 描边粗细 | `subtitle.border_size` | `NoWheelDoubleSpinBox` | 0.0–8.0，步 0.2，后缀「 px」 | `2.4` | ▲ 重启 | `app`(653) |
| 字幕 · 配色 | 文字颜色 | `subtitle.text_color` | 色板 `QLabel`(26×26) + `QLineEdit`（最长 9 字符） | `#RRGGBB` / `#AARRGGBB` 文本；**无拾色器**，非法文本也会直接写入配置 | `"#FFFFFF"` | ▲ 重启 | `app`(651) |
| 字幕 · 配色 | 描边颜色 | `subtitle.border_color` | 同上 | 同上 | `"#101010"` | ▲ 重启 | `app`(652) |
| 字幕 · 时间轴微调 | 微调步长 | `subtitle.step` | `NoWheelDoubleSpinBox` | 0.05–1.0，步 0.05，后缀「 秒」 | `0.1` | ✖ 无效 | 全仓 0 处读取；字幕浮层(V)自带档位下拉（`subtitle_panel.py:252-312`） |
| 字幕 · 时间轴微调 | 当前偏移 | `subtitle.delay` | `NoWheelDoubleSpinBox` | -30.0 ~ +30.0，步 0.1，后缀「 秒」 | `0.0` | ▲ 重启 | `app`(656) 启动施加；浮层内另有 ± 实时微调（改的是同一个键） |
| 字幕 · 样式覆盖 | ASS 特效字幕 | `subtitle.ass_override` | `NoWheelComboBox` | `scale` 随窗口缩放（推荐）<br>`yes` 完全使用用户样式<br>`force` 强制用户样式（忽略特效）<br>`no` 保持原样式 | `scale` | ▲ 重启 | `app`(657) |
| 字幕 · 样式覆盖 | 加粗 | `subtitle.bold` | `ToggleSwitch` | 开 / 关 | `false` | ▲ 重启 | `app`(649) |
| 字幕 · 样式覆盖 | 斜体 | `subtitle.italic` | `ToggleSwitch` | 开 / 关 | `false` | ▲ 重启 | `app`(650) |

### 2.4 界面（`ui`，9 行 / 8 键 —— `ui.theme` 有两个控件视图）

| 所属分类 | 选项名称（UI Label） | 配置键（Config Key） | 当前控件类型 | 取值范围 / 候选项 | 默认值 | 是否实时生效 | 读取 / 施加点（依据） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 界面 · 外观 | 强调色（**标签名与实际语义不符**） | `ui.theme` | 两个自绘 `QToolButton`（checkable 胶囊：浅色 / 深色；液态玻璃因 `hidden` 不渲染按钮） | `qq-light` 浅色 / `qq-dark` 深色 | 见 §2.10-C | ✅ 立即 | `sp._accent_combo`(826-872) → `_pick_theme` → `theme_requested` → `mw.apply_theme`(1939) 并落盘 `ui.theme` |
| 界面 · 外观 | 主题 | `ui.theme` | `NoWheelComboBox`（**仅 3 项**） | 浅色 / 深色 / 液态玻璃（彩蛋） | 见 §2.10-C | ✅ 立即 | `sp._theme_skin_combo`(874-901) → `_pick_theme` |
| 界面 · 外观 | 播放列表宽度 | `ui.drawer_width` | `NoWheelSlider` + 数值标签（58px） | 280–560，步 1，显示「px」 | `392` | ▲ 重启（或点「恢复默认设置」） | `mw`(477) 构造时下发；`mw._on_settings_reset`(4220) 是唯一二次下发点 |
| 界面 · 外观 | 控制条自动隐藏延时 | `ui.capsule_autohide_ms` | `NoWheelSlider` + 数值标签 | 800–8000，步 1，显示「ms」 | `2800` | ▲ 重启（或点「恢复默认设置」） | `mw._start_timers`(3242)；重置(4219) |
| 界面 · 图片查看 | 默认缩放 | `ui.image_fit` | `NoWheelComboBox` | `fit` 适应窗口 / `one` 原始大小 100% / `free` 上次的缩放比例 | `fit` | ✖ 无效 | 全仓只有 2 处 `cfg.set` 写入（`mw` 1234、1247），**无任何读取** |
| 界面 · 图片查看 | 滚轮缩放步长 | `ui.image_zoom_step` | `NoWheelDoubleSpinBox` | 1.02–1.50，步 0.01，后缀「 x」 | `1.12` | ✅ 立即 | `mw`(522、523、3920、3923) 每次缩放实时读 |
| 界面 · 窗口 | 初始窗口宽度 | `ui.window_w` | `NoWheelSpinBox` | 640–7680，步 10，后缀「 px」 | `1280` | ▲ 重启 | `mw._restore_geometry`(4415)；被 `_store_geometry`(4447) 反向覆盖 |
| 界面 · 窗口 | 初始窗口高度 | `ui.window_h` | `NoWheelSpinBox` | 400–4320，步 10，后缀「 px」 | `760` | ▲ 重启 | `mw`(4416)；被 `_store_geometry`(4448) 反向覆盖 |
| 界面 · 窗口 | 关闭窗口时二次确认 | `ui.confirm_exit` | `ToggleSwitch` | 开 / 关 | `false` | ✖ 无效 | 全仓 0 处读取（`closeEvent` 无确认分支） |

### 2.5 声音（`audio`，3 项 / 3 键）

| 所属分类 | 选项名称（UI Label） | 配置键（Config Key） | 当前控件类型 | 取值范围 / 候选项 | 默认值 | 是否实时生效 | 读取 / 施加点（依据） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 声音 | 默认音量 | `audio.volume` | `NoWheelSlider` + 数值标签（52px） | 0–100，显示「%」 | `100.0` | ▲ 重启（语义即「启动音量」，与标签一致） | `app`(495 引擎、497 胶囊) 启动施加；`mw._on_volume` 只写不读 |
| 声音 | 启动时静音 | `audio.mute` | `ToggleSwitch` | 开 / 关 | `false` | ▲ 重启 | `app`(496、498) |
| 声音 | 输出设备 | `audio.device` | `NoWheelComboBox`（设计为动态枚举） | `auto` 自动（跟随系统默认设备）/ 设备名-描述 | `auto` | ✖ 无效 | 无任何 mpv `audio-device` 下发；`sp.populate_audio_devices()`(1094) **全仓无调用方**，下拉永远只有「自动」一项 |

### 2.6 音乐与歌词（`music`，9 项 / 9 键，全部即时生效）

| 所属分类 | 选项名称（UI Label） | 配置键（Config Key） | 当前控件类型 | 取值范围 / 候选项 | 默认值 | 是否实时生效 | 读取 / 施加点（依据） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 音乐与歌词 | 歌词字体 | `lyric.font_family` † | `NoWheelComboBox`（按 `QFontDatabase.families()` 动态构建） | `system` 跟随系统（内置 HarmonyOS Sans SC）；系统已安装则追加 HarmonyOS Sans SC / MiSans / Source Han Sans SC / 思源黑体 / Microsoft YaHei UI | `system` | ✅ 立即 | `sp._apply_music_key`(1278) → `av.apply_music_setting`(5060) |
| 音乐与歌词 | 歌词字号（当前行） | `lyric.font_px` † | `NoWheelSlider` + 数值标签 | 16–28，步 1，显示「px」 | `27` | ✅ 立即 | `av`(5062) |
| 音乐与歌词 | 歌词字重 | `lyric.weight` † | `NoWheelComboBox` | `regular` 常规 / `medium` 加粗（Medium）/ `heavy` 特粗（Heavy） | `medium` | ✅ 立即 | `av`(5064) |
| 音乐与歌词 | 歌词行间距 | `lyric.step_pad` † | `NoWheelSlider` + 数值标签 | 10–48，步 1，显示「px」 | `18` | ✅ 立即 | `av`(5066) |
| 音乐与歌词 | 滚动动画速度 | `lyric.scroll_ms` † | `NoWheelComboBox`（配置值实际存 **int**） | 200 快 / 320 标准 / 500 慢 | `320` | ✅ 立即 | `av`(5068) |
| 音乐与歌词 | 音瀑 | `music.cascade` † | `ToggleSwitch` | 开 / 关（窗口宽度 ≥1320px 才显示） | `false` | ✅ 立即 | `av`(5070) `set_cascade_enabled` |
| 音乐与歌词 | 频谱 | `spectrum.enabled` † | `ToggleSwitch` | 开 / 关 | `true` | ✅ 立即 | `av`(5072) |
| 音乐与歌词 | 峰值点样式 | `spectrum.peak_style` † | `NoWheelComboBox` | `none` 纯双向声波（默认）/ `falling` 带极速落峰点 | `none` | ✅ 立即 | `av`(5074) |
| 音乐与歌词 | 频谱律动速度 | `spectrum.rhythm` † | `NoWheelComboBox` | `calm` 舒缓（α0.20 / ω30%）/ `standard` 标准（α0.24 / ω45%）/ `aggressive` 激进（α0.28 / ω62%） | `standard` | ✅ 立即 | `av`(5076) |

### 2.7 快捷键（`hotkeys`，26 个动作 / 52 个按键槽位）

> 控件形态：每个动作一行，行内 **两个** `KeyCaptureEdit`（只读 `QLineEdit`，`setFocusPolicy(NoFocus)`；点击进入「请按下新的按键…」等待态，真正抓键由**应用级事件过滤器**完成，这样焦点在任何控件上都能捕获）。值类型为 `list[str]`（第 0/1 项 = 主键/副键），按 `Delete` 清空当前槽位。

| 所属分类 | 选项名称（UI Label） | 配置键（Config Key） | 当前控件类型 | 取值范围 / 候选项 | 默认值（主键 / 副键） | 是否实时生效 | 读取 / 施加点（依据） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 快捷键 | 播放 / 暂停 | `hotkeys.play_pause[0]/[1]` | `KeyCaptureEdit` ×2 | 任意单键或 Ctrl / Shift / Alt 组合；Delete 清空 | `Space` / （空） | ✅ 立即 | `mw._match`(3896) 每次按键实时读 `cfg`；写入 `mw._on_hotkey_changed`(4198) |
| 快捷键 | 快进 5 秒（长按 → 倍速） | `hotkeys.seek_fwd_small[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Right` / `D` | ✅ 立即 | 同上 |
| 快捷键 | 快退 5 秒（长按 ← 倍速） | `hotkeys.seek_back_small[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Left` / `A` | ✅ 立即 | 同上 |
| 快捷键 | 快进 15 秒（按住连跳） | `hotkeys.seek_fwd_shift[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Shift+Right` / `Shift+D` | ✅ 立即 | 同上 |
| 快捷键 | 快退 15 秒（按住连跳） | `hotkeys.seek_back_shift[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Shift+Left` / `Shift+A` | ✅ 立即 | 同上 |
| 快捷键 | 快进 30 秒 | `hotkeys.seek_fwd_large[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | （空） / （空） | ✅ 立即 | 同上 |
| 快捷键 | 快退 30 秒 | `hotkeys.seek_back_large[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | （空） / （空） | ✅ 立即 | 同上 |
| 快捷键 | 音量 + | `hotkeys.volume_up[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Up` / `W` | ✅ 立即 | 同上 |
| 快捷键 | 音量 − | `hotkeys.volume_down[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Down` / `S`（**与下一条截图 `S` 冲突**，见 §3.3-11） | ✅ 立即 | 同上 |
| 快捷键 | 静音 | `hotkeys.mute[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `M` / （空） | ✅ 立即 | 同上 |
| 快捷键 | 全屏 / 退出全屏 | `hotkeys.fullscreen[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `F` / `Return` | ✅ 立即 | 同上 |
| 快捷键 | 播放列表抽屉 | `hotkeys.playlist[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `L` / （空） | ✅ 立即 | 同上 |
| 快捷键 | 字幕设置浮层 | `hotkeys.subtitle_panel[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `V` / （空） | ✅ 立即 | 同上 |
| 快捷键 | 设置 | `hotkeys.settings[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Ctrl+,` / （空） | ✅ 立即 | 同上 |
| 快捷键 | 打开文件 | `hotkeys.open_file[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Ctrl+O` / （空） | ✅ 立即 | 同上 |
| 快捷键 | 打开文件夹 | `hotkeys.open_folder[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Ctrl+Shift+O` / （空） | ✅ 立即 | 同上 |
| 快捷键 | 截图 | `hotkeys.screenshot[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `S` / （空）（**永不触发**，被 `volume_down` 先命中） | ✅ 立即 | 同上 |
| 快捷键 | 媒体信息（左上角浮层） | `hotkeys.media_info[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Tab` / （空） | ✅ 立即 | 同上 |
| 快捷键 | 加速 | `hotkeys.speed_up[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `]` / `Ctrl+Up` | ✅ 立即 | 同上 |
| 快捷键 | 减速 | `hotkeys.speed_down[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `[` / `Ctrl+Down` | ✅ 立即 | 同上 |
| 快捷键 | 恢复正常速度 | `hotkeys.speed_reset[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Backspace` / （空） | ✅ 立即 | 同上 |
| 快捷键 | 上一个文件 | `hotkeys.prev_file[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `PageUp` / `Ctrl+Left` | ✅ 立即 | 同上 |
| 快捷键 | 下一个文件 | `hotkeys.next_file[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `PageDown` / `Ctrl+Right` | ✅ 立即 | 同上 |
| 快捷键 | 上一帧 | `hotkeys.frame_back[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `,` / （空） | ✅ 立即 | 同上（仅视频模式生效，见 `mw` 4041） |
| 快捷键 | 下一帧 | `hotkeys.frame_fwd[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `.` / （空） | ✅ 立即 | 同上（仅视频模式生效，见 `mw` 4044） |
| 快捷键 | 窗口置顶 | `hotkeys.always_on_top[0]/[1]` | `KeyCaptureEdit` ×2 | 同上 | `Ctrl+T` / （空） | ✅ 立即 | 同上 |

### 2.8 关于（`about`，0 个可配置项）

| 所属分类 | 选项名称（UI Label） | 配置键（Config Key） | 当前控件类型 | 取值范围 / 候选项 | 默认值 | 是否实时生效 | 读取 / 施加点（依据） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 关于 | 无任何可配置项（版本号 / 许可声明 / 渲染管线 / 便携声明，纯静态只读） | — | 静态多行 `QLabel`（`role=sub`，可换行，最小高 180） | — | — | — | `sp._page_about`(1179-1203)，读 `zhijian.__version__` |

### 2.9 汇总统计（逐项合计，无「等」省略）

| 分类 | 配置键数 | 设置行数 | ✅ 立即 | ◐ 条件 | ▲ 重启 | ✖ 无效 |
| --- | --- | --- | --- | --- | --- | --- |
| 常规 general | 14 | 14 | 5 | 3 | 3 | 3 |
| 播放 playback | 12 | 12 | 9 | 0 | 1 | 2 |
| 字幕 subtitle | 13 | 13 | 0 | 0 | 11 | 2 |
| 界面 ui | 8 | 9 | 3 | 0 | 4 | 2 |
| 声音 audio | 3 | 3 | 0 | 0 | 2 | 1 |
| 音乐与歌词 music | 9 | 9 | 9 | 0 | 0 | 0 |
| 快捷键 hotkeys | 26（×2 槽位） | 26 | 26 | 0 | 0 | 0 |
| 关于 about | 0 | 0 | 0 | 0 | 0 | 0 |
| **合计** | **85 个配置键** | **86 行**（+52 个按键捕获框） | **52** | **3** | **21** | **10** |

**读法**：`✖ 无效 10 行` = 9 项「写了没人读」+ 1 项「只控制提示、控制不了行为」（`subtitle.auto_load`）；占非快捷键 60 行的 **16.7%**。<br>**`▲ 重启 21 行`** 说明设置面板有一半以上的项「改完要重启」——这是当前面板最大的体验落差（用户改完看不到变化，会以为设置坏了）。

### 2.10 配置键一致性校验（三张对照表）

#### A. 14 个未登记进 `config.DEFAULTS` 的键（默认值只活在 UI 层）

`config.py:23-139` 的 `DEFAULTS` 共 91 个扁平键；设置界面引用 59 个键，其中 **14 个不在 `DEFAULTS` 里**。这直接违反 `config.py:5` 自定的规则「每次新增/修改默认键, 都要在 `MIGRATIONS` 里登记」：**改这些默认值对老用户无效，也没有迁移钩子**。

| # | 配置键 | 实际生效默认值 | 默认值实际所在处 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | `ui.preview_thumb` | `true` | `mw`(954) 读取端兜底 | 面板兜底是 `false` ⇒ 配置缺键时**显示 OFF、实际 ON**（实盘 `config.json` 已是 `false`） |
| 2 | `ui.external_open_mode` | `switch` | `app.external_open_mode`(116) | 读/写两处兜底一致，风险低 |
| 3 | `image.minimap_position` | `bottom_right` | `mw`(1085) | 一致 |
| 4 | `image.wheel_invert` | `normal` | 仅面板兜底 | **读取端被硬编码覆盖**，默认值无意义 |
| 5 | `ui.accent_override` | —（不存在） | 死代码分支 `sp`(792) `if False` | 该键永远不会被写入/读取 |
| 6 | `lyric.font_family` | `system` | `sp.MUSIC_DEFAULTS`(57-64) | — |
| 7 | `lyric.font_px` | `27` | 同上 | 与 task-23 封版值一致 |
| 8 | `lyric.weight` | `medium` | 同上 | — |
| 9 | `lyric.step_pad` | `18` | 同上 | `cfg._m_6_to_7`(247) 只对**已存在**的键做 40→18 迁移 |
| 10 | `lyric.scroll_ms` | `320` | 同上 | 配置里是 int，非字符串 |
| 11 | `music.cascade` | `false` | 同上 | — |
| 12 | `spectrum.enabled` | `true` | 同上 | — |
| 13 | `spectrum.peak_style` | `none` | 同上 | — |
| 14 | `spectrum.rhythm` | `standard` | 同上 | — |

#### B. 23 处「面板兜底默认值」与 `config.DEFAULTS` 不一致

正常情况下 `Config.load()` 会 `_deep_merge(DEFAULTS, raw)`，键必然存在，所以这些兜底**平时不会触发**；但一旦配置文件被手工删键、被旧版本写入不完整、或迁移缺口（即上表 14 个键），界面就会按兜底值渲染，出现「界面显示」与「运行时行为」不一致。**其中 10 处是布尔反向**，危害最大。

| # | 配置键 | `config.DEFAULTS` | 面板兜底 | 风险 |
| --- | --- | --- | --- | --- |
| 1 | `ui.remember_window` | `true` | `false` | **布尔反向**：显示关、实际按开 |
| 2 | `ui.capsule_autohide` | `true` | `false` | 同上 |
| 3 | `ui.drawer_edge_hover` | `true` | `false` | 同上 |
| 4 | `ui.animations` | `true` | `false` | 同上 |
| 5 | `ui.osd` | `true` | `false` | 同上 |
| 6 | `ui.single_instance` | `true` | `false` | 同上 |
| 7 | `playback.remember_position` | `true` | `false` | 同上 |
| 8 | `playback.auto_play` | `true` | `false` | 同上（且该键本身已无效） |
| 9 | `subtitle.enabled` | `true` | `false` | 同上 |
| 10 | `subtitle.auto_load` | `true` | `false` | 同上 |
| 11 | `playback.long_press_threshold_ms` | `300` | `120` | 显示值是下限（最差可用值），与实际不符 |
| 12 | `playback.seek_step_small` | `5` | `1` | 同上 |
| 13 | `playback.seek_step_large` | `30` | `5` | 同上 |
| 14 | `playback.seek_step_shift` | `15` | `1` | 同上 |
| 15 | `playback.volume_step` | `5` | `1` | 同上 |
| 16 | `playback.cache_secs` | `30` | `0` | 显示 0（关闭缓存），与实际 30 不符 |
| 17 | `subtitle.font_size` | `46` | `12` | 显示最小值 |
| 18 | `subtitle.position` | `92` | `0` | 显示顶部位置（最差） |
| 19 | `subtitle.border_color` | `#101010` | `#FFFFFF` | 颜色相反（`_color_edit` 对两个键都写死白色兜底） |
| 20 | `ui.drawer_width` | `392` | `280` | 显示最窄抽屉 |
| 21 | `ui.capsule_autohide_ms` | `2800` | `800` | 显示最短延时 |
| 22 | `ui.window_w` | `1280` | `640` | 显示最小窗口宽 |
| 23 | `ui.window_h` | `760` | `400` | 显示最小窗口高 |

#### C. 主题有三层真源，彼此不一致

| 层级 | 真源 | 值 |
| --- | --- | --- |
| ① 配置默认 | `cfg.DEFAULTS["ui"]["theme"]`（`config.py:86`） | `aurora`（极光紫，深色系） |
| ② 主题系统默认 | `theme.DEFAULT_THEME`（`theme.py:378`） | `graphite`（石墨冷青，启动默认深色） |
| ③ 设置面板可选 | `sp.THEME_MODES`（`settings_panel.py:824`） | `qq-light` 浅色 / `qq-dark` 深色 / `glass-light` 液态玻璃（彩蛋） |
| ④ 主题库实际数量 | `theme.THEMES`（`theme.py:192-319, 348`） | **8 个**：graphite / qq-light / qq-dark / amber / sakura / aurora / mint / glass-light |

由此产生的**两个可见缺陷**：

1. **5 个预设从设置界面不可达**：graphite、amber、sakura、aurora、mint 既不在下拉里、也没有快捷按钮；唯一入口是标题栏的日/夜切换（它只在 qq-light / qq-dark 间切）。
2. **下拉显示与实际主题不符**：`_theme_skin_combo`（`:892-894`）用 `findData(cur)` 定位当前项，当前主题是 `graphite`（实盘 `config.json` 就是 `graphite`）时 `findData` 返回 -1 → `setCurrentIndex(0)` ⇒ **深色主题下，主题下拉显示「浅色」**；用户在设置里看到的第一项状态是错的。

---

## 3. 当前存在的不合理点自查

> 本节是给 UX 评审用的「可执行」清单：每条都给出**代码位置**与**为什么是问题**，并尽量给出改法方向（不代表本卡要改代码——本卡只做只读审计）。

### 3.1 放错分类（8 条）

| # | 现象 | 证据（键 / 位置） | 影响与建议 |
| --- | --- | --- | --- |
| 1 | 「常规」页第一张无标题卡片里塞了 **4 个播放参数** | `playback.remember_position`(sp:607)、`playback.auto_play`(608)、`playback.loop_mode`(625)、`playback.image_duration`(629) | 用户按「常规」找总开关时会看到「默认循环方式」；建议全部迁到「播放」页，常规页只留窗口/单实例/外部打开等应用级行为 |
| 2 | 卡片标题「启动行为」与卡内内容不符 | sp:624-631：卡内是「默认循环方式」+「图片自动播放间隔」 | 「循环方式」不是启动行为，「图片间隔」也不是；建议改名为「播放与图片」或拆走 |
| 3 | 标签写「强调色」，实际控件是「浅色 / 深色」两个模式按钮 | sp:792 `self._color_edit("ui.accent_override") if False else self._accent_combo()` | 标签误导 + 死代码分支（`if False` 永不进入）；建议标签改「外观模式」，并删除 `ui.accent_override` 残留分支 |
| 4 | 「窗口」卡片里放「关闭窗口时二次确认」 | sp:812 `ui.confirm_exit` | 属交互确认不属窗口几何；且该键无实现（§3.3-7） |
| 5 | 「声音」页 3 项里 1 项无效，高频的「音量步长」却在别处 | 声音页 = 默认音量 / 启动时静音 / 输出设备（无效）；`playback.volume_step` 在 sp:698「播放 · 跳转与音量」 | 建议把「音量步长」并入声音页，让「声音」分类名副其实 |
| 6 | 「音乐与歌词」单卡片混装 3 类语义 | sp:978-1039：歌词排版 5 项 + 音瀑 1 项 + 频谱 3 项，**只有一个卡片标题** | 建议拆成「歌词排版」「频谱」「音瀑（宽屏特效）」三个二级分组，用户不必读完全部才能找到频谱开关 |
| 7 | 「当前偏移」是会话内临时状态，却当持久默认值 | `subtitle.delay`（sp:748）在 `app._apply_subtitle_defaults`(656) 每次启动施加 | 用户在浮层里调过一次字幕偏移，下次启动所有字幕都偏移；建议改名「默认字幕偏移」或与浮层临时偏移分离 |
| 8 | 快捷键页 / 关于页不走 `_card` 分组，与其它 6 页视觉层级不一致 | sp:1106-1140 / 1179-1203 | 建议统一到「标题 + 分组线」的同一套版式 |

### 3.2 控件选型（9 条，重点是「该用 Slider / Combo 的」）

| # | 现状控件 | 更适合的形态 | 理由 | 位置 |
| --- | --- | --- | --- | --- |
| 1 | `playback.long_press_tiers`：`QLineEdit` 自由文本 + 右侧解析预览 | **多选胶囊 / 可编辑 ComboBox 档位**（2 / 5 / 10 / 20） | 为了「档位可自定义」引入了容错解析（中文逗号、乱输回落默认）；而且 `textChanged` 每敲一个字符就写一次盘 | sp:650-679 |
| 2 | `subtitle.step` 0.05–1.0 `QDoubleSpinBox` 步 0.05 | **ComboBox 档位**（±0.05 / ±0.1 / ±0.25 / ±0.5） | 同一语义在字幕浮层已用 ComboBox（`subtitle_panel.py:252-258`）⇒ 同一概念两种控件；且 0.05 精度对用户无意义 | sp:747 |
| 3 | `playback.speed_step` 0.05–1.0 `QDoubleSpinBox` | **ComboBox 档位 / Slider** | 同上，倍速步长只有 0.05/0.1/0.25/0.5 四种现实取值 | sp:699 |
| 4 | `ui.image_zoom_step` 1.02–1.50 `QDoubleSpinBox` 步 0.01 | **Slider（1.02–1.30）** | 用户感知不到 1.12 与 1.13 的差别，却要按 8 次才能跨过 0.08 | sp:804 |
| 5 | `subtitle.font_size` 12–160 `QSpinBox` | **Slider**（与字幕浮层一致） | 字幕浮层用 Slider（`subtitle_panel.py:169`），设置页用 SpinBox；从 46 调到 60 要按 14 次 | sp:736 |
| 6 | `subtitle.position` 0–100 `QSpinBox` | **Slider 百分比** | 同上（浮层 `subtitle_panel.py:234` 是 Slider） | sp:737 |
| 7 | `ui.capsule_autohide_ms` 800–8000 `Slider`，步 1 | Slider **量化到 100/200ms** | 7200 个档位对一个「延时」没有意义，松手值随机且不可复述 | sp:797 |
| 8 | `ui.window_w` / `ui.window_h` `QSpinBox`（640–7680 / 400–4320，步 10） | **删除或改 ComboBox 预设** | 与「记忆窗口大小及位置」功能冲突：填完的值会被 `_store_geometry` 在关闭时覆盖，用户改它几乎等于白改 | sp:808-811 |
| 9 | 颜色项 = 色板 `QLabel` + `QLineEdit` 手填 hex（无拾色器） | **`QColorDialog` 拾色器 + hex 只读回显** | 普通用户不知道 `#RRGGBB`；且非法文本（如 `#GGG`）会原样写盘并传给 mpv | sp:763-786 |

### 3.3 冗余 / 无效项（16 条，逐条可复现）

#### A. 写进配置但**无人读取**（9 项完全 + 1 项半失效）

| # | 选项 | 配置键 | 症状 | 证据 | 建议 |
| --- | --- | --- | --- | --- | --- |
| 1 | 硬件解码 | `playback.hwdec` | 下拉 5 个选项都改不动实际解码方式 | `mpv/player.py:132` 硬编码 `hwdec: auto-safe`；`app.py:464` 只接受命令行 `--hwdec` | 要么把值下发到 mpv（重开文件生效），要么删掉选项 |
| 2 | 预读缓存 | `playback.cache_secs` | 改 0 或 600 都没反应 | `mpv/player.py:156` 硬编码 `cache-secs: 30`；全仓无读取 | 同上 |
| 3 | 打开后自动播放 | `playback.auto_play` | 关掉也会自动播放 | 全仓 grep = 0 处读取（只有 `config.py:43` 默认值与 `sp:608` 写入） | 接线或删除 |
| 4 | 图片自动播放间隔 | `playback.image_duration` | 设 5 秒图片也不会自动切换 | 全仓 0 处读取（图片自动轮播未实现） | 实现或删除 |
| 5 | 滚轮方向 | `image.wheel_invert` | 选「反向」后行为不变 | `mw`(1082-1084)：注释明写「硬性归正：上滑=放大，无条件，不再读取历史配置」，随后 `v.wheel_invert = False` | **直接删除该选项**（保留硬编码行为） |
| 6 | 微调步长（字幕） | `subtitle.step` | 改了不影响 V 浮层的 ◀ ▶ 步长 | 全仓 0 处读取；浮层自带 `step_combo`（`subtitle_panel.py:252-318`） | 删除该选项，或让浮层读它 |
| 7 | 关闭窗口时二次确认 | `ui.confirm_exit` | 关窗不会弹确认 | 全仓 0 处读取；`mw.closeEvent`(4452) 无确认分支 | 实现或删除 |
| 8 | 默认缩放 | `ui.image_fit` | 设「原始大小」后打开图片仍是适应窗口 | 全仓只有 2 处 `cfg.set` 写入（`mw` 1234/1247 = 用户缩放时反写当前模式），**0 处读取** | 打开图片时读取并套用 |
| 9 | 输出设备 | `audio.device` | 下拉永远只有「自动」；选了也无效果 | `sp.populate_audio_devices()`(1094) **全仓无调用方**；无任何 mpv `audio-device` 下发 | 接 `audio-device` 属性 + 调用枚举，或删除 |
| 10 | 自动挂载外挂字幕 | `subtitle.auto_load` | 关掉后字幕仍自动挂载（只是没了提示） | 真正挂载是 `mpv/player.py:172` `sub-auto=fuzzy` 常开；`mw._auto_sub_notice`(1766) 只用它决定**是否弹 OSD** | 要么下发 `sub-auto=no`，要么把标签改成「自动挂载时提示」 |

#### B. 冲突 / 死代码 / 状态错位（6 条）

| # | 问题 | 证据 | 影响 |
| --- | --- | --- | --- |
| 11 | **默认快捷键冲突：`S`** | `cfg` 的 `volume_down = ["Down", "S"]`（`config.py:115`）与 `screenshot = ["S"]`（`config.py:124`）重复；分发是**顺序 if 链**，`volume_down`(3994) 先于 `screenshot`(4023) | 按 `S` 永远只降音量；「截图」默认快捷键**实际不可用**，且用户在快捷键页看不出冲突 |
| 12 | `ui.accent_override` 死代码分支 | `sp:792` `self._color_edit("ui.accent_override") if False else ...` | 该键既不在 DEFAULTS、也无读取方；分支永久不执行，误导后来维护者 |
| 13 | 主题下拉只列 3 项，当前主题不在清单时回落第 0 项 | `sp:824` `THEME_MODES` 3 项 vs `theme.THEMES` 8 项；`sp:892-894` `findData` 失败 → `setCurrentIndex(0)` = 「浅色」 | 8 个主题里 5 个不可达；**深色主题时下拉显示「浅色」**（实盘 config 为 graphite） |
| 14 | 4 个快捷键动作「有默认键但界面不可见」 | `HOTKEY_LABELS`(66-93) 只有 26 项；`cfg.DEFAULTS["hotkeys"]`(100-138) 有 30 项 | `hotkeys.stop=["X"]` 有效但改不了；`escape/larger_ui/smaller_ui` 连代码都不消费（Esc 由硬编码 `Qt.Key_Escape` 处理，`mw` 358/3765/3836） |
| 15 | 「恢复默认设置」清空面过大且不回刷界面 | `sp._on_reset`(1389-1393) `cfg.reset_all()` → 重置**全部** DEFAULTS（含 `recent.files` 播放列表、窗口几何、`ui.theme` 主题、快捷键）；`mw._on_settings_reset`(4214-4221) 只补 4 项；面板控件不重建（只有 `refresh_hotkeys`） | 点一次「恢复默认」：播放列表（`recent.files`）被清空、主题跳变、面板里显示的仍是旧值 |
| 16 | 每次值变化都无条件 `cfg.save()` | `sp._set`(1256-1264)：`cfg.set()` + **`cfg.save()`**；`cfg.save`(366-375) 走 `tempfile.mkstemp` + `os.replace` | 拖一次滑条（如 280→560）触发 **280 次** JSON 写盘；文本输入每字符一次；SSD 寿命与输入卡顿风险 |

### 3.4 缺失的高频项（7 条）

| # | 缺失项 | 现状 | 建议 |
| --- | --- | --- | --- |
| 1 | 最低倍速 | `playback.speed_min = 0.25` 有默认值、被 `mw`(1494/1586) 真实使用，但设置页只有「最高倍速」 | 补「最低倍速」SpinBox |
| 2 | 进度记忆的最短时长 | `playback.resume_min_secs = 15` 被 `mw`(4163) 使用，界面不可见 | 补「短于 N 秒不记忆进度」 |
| 3 | 字幕阴影 | `subtitle.shadow_offset = 1.2` 被 `app`(654) 施加，界面不可见 | 补「阴影偏移」Slider |
| 4 | 最近播放条数 | `recent.max = 24` 被 `cfg.push_recent`(418) 使用，界面不可见 | 补「最近列表上限」 |
| 5 | 4 个隐藏快捷键动作 | `stop` 有效但不可改；`escape/larger_ui/smaller_ui` 是死绑定（详见 §3.3-14） | 进快捷键页；死绑定要么接线要么删 |
| 6 | 快捷键页没有「冲突检测 / 搜索 / 分组」 | `sp:1106-1140` 52 个捕获框平铺，无列标题（主/副槽只靠 tooltip） | 加列标题、实时冲突高亮、按功能分组折叠 |
| 7 | 「关于」页没有任何可操作入口 | `sp:1179-1203` 只有静态文本 | 加「打开配置目录 / 打开日志目录 / 检查更新」等入口（排查问题的高频路径） |

### 3.5 交互与语义（7 条）

| # | 问题 | 证据 | 说明 |
| --- | --- | --- | --- |
| 1 | **「应用」不应用、「取消」不取消** | `_set`(1256) 每次改动立即 `cfg.set + cfg.save`；`_on_apply`(1348) 只清 `_dirty` 并弹 OSD；`_close`(1386) 只发 `closed` | 顶部三个按钮的真实语义是「确定=关闭 / 应用=提示一句 / 取消=关闭」，与 Windows 惯例相反；`changed` 信号（`_on_apply` 里 emit）**全仓无接收者** |
| 2 | 「恢复默认设置」不回刷控件 | `_on_reset`(1389-1393) 只 `refresh_hotkeys()` | 用户看到的是重置前的值，会以为没生效（要等一次换肤触发整页重建） |
| 3 | 没有「未保存」状态提示 | `_dirty` 只用于「应用」按钮文案（`_flash_apply_btn` 1356） | 因为改动已即时落盘，这个状态本身是多余的；建议干脆去掉「应用」，保留「确定/关闭」 |
| 4 | 说明行插入表单项之间，打断阅读节奏 | 每页多处 `f.addRow(_sub(...))`（如 sp:605、621、630） | 说明文字没有跟它解释的那一项绑定；建议改用 tooltip 或缩进的次级说明 |
| 5 | 面板可拖动但不可调整大小 | `DRAG_BAND_H=46`；`fit_to`(1206) 只按可用空间缩 | 拖动后无「复位居中」入口（只有主窗口内部逻辑），用户拖到角落只能自己拖回来 |
| 6 | 主题的两个控件同一键双视图 | `_accent_combo`(826) + `_theme_skin_combo`(874)，都映射 `ui.theme` | 逻辑上用 `_pick_theme` 已做双向同步，但两处 UI 表达不同（按钮只有明暗，下拉有皮肤），用户容易以为「按钮=全部主题」 |
| 7 | 「固定档位」等控件靠置灰表达依赖，但没有解释 | `_sync_boost_fields`(682-690) 按模式置灰；注释里写了「免得用户改了没生效以为坏了」 | 置灰控件仍需 tooltip 说明「当前模式下不适用」 |

### 3.6 数据一致性与工程风险（9 条）

| # | 风险 | 证据 | 建议 |
| --- | --- | --- | --- |
| 1 | 14 个键未登记 `config.DEFAULTS` | §2.10-A | 统一登记进 `DEFAULTS`（音乐/频谱/看图/预览/外部打开），并按 `config.py:5` 的规则补迁移 |
| 2 | 23 处面板兜底与 config 默认不一致（10 处布尔反向） | §2.10-B | 让控件工厂不再自己兜底，统一改成 `cfg.get(key)`（键一定存在）或抽 `_default(key)` 单点 |
| 3 | 无防抖写盘 | `sp._set`(1259) | 只在 `editingFinished` / 滑条 `sliderReleased` / 防抖 300ms 后 `save()` |
| 4 | 主题三层真源 | §2.10-C | 以 `theme.THEMES` 为唯一清单，`ui.theme` 默认值改成 `theme.DEFAULT_THEME`，面板下拉遍历全部（彩蛋单独标记） |
| 5 | 页面构建代码写了两遍 | `sp:410-417` 与 `sp:963-970` | 抽一个 `_build_pages()` 两处共用，或列表驱动 |
| 6 | 分类顺序三处隐式耦合 | `SECTIONS`(41-50)、stack 添加顺序(410-417)、重建顺序(963-970) | 由 `SECTIONS` 驱动循环构建，消除手工同步 |
| 7 | 颜色值无校验 | `_color_edit`(778-782) 直接 `cfg.set` 任意文本 | 用 `QColor(text).isValid()` 把关，非法时红框且不落盘 |
| 8 | 音乐设置两套默认值真源 | `sp.MUSIC_DEFAULTS`(57-64) vs `config.DEFAULTS` | 合并到 config 层（与第 1 条一起做） |
| 9 | 需求与实现漂移未回写 | `sp:1005` 注释：「卡面写 10~36，但默认 pad=40 是 task-23 封版值 ⇒ 放宽上限以容纳默认值」，实际实现 10–48、默认 18 | 卡面/注释/实现三者对齐后再冻结 |

### 3.7 建议优先级清单（供 Lead / UX 评审排期）

| 优先级 | 事项 | 预估工作量 | 风险 | 验收判据 |
| --- | --- | --- | --- | --- |
| **P0** | 修默认快捷键 `S` 冲突（改默认值或加冲突检测） | 0.5h | 低 | 26 个动作默认值两两不重复；`S` 触发截图 |
| **P0** | 主题下拉补齐 8 个主题并修「不在清单就回落到第 0 项」 | 2h | 中（涉及 theme 清单与 fallback） | 8 个主题都能从面板选中；下拉显示项 == 当前 `theme.P["_theme"]` |
| **P0** | 9 个无效项：能接线的接线（`image_fit`、`audio.device`、`confirm_exit`），其余直接删（`hwdec`、`cache_secs`、`auto_play`、`image_duration`、`wheel_invert`、`subtitle.step`）；半失效的 `subtitle.auto_load` 另判（删或改标签） | 1~3 天 | 中（删选项需同步文档/QA） | 每个保留项都有真实读取点；删除项不再出现在面板 |
| **P0** | 修正「应用/取消/恢复默认设置」语义（或直接改成「确定/关闭」+ 即时生效） | 1h | 低 | 三按钮行为与文案一致；恢复默认后控件立即刷新 |
| **P1** | 14 个键登记进 `DEFAULTS` + 23 处兜底对齐 | 3h | 低 | 面板兜底 == `DEFAULTS`；新增键有迁移 |
| **P1** | 写盘防抖 | 1h | 低 | 拖满一次滑条磁盘写入 ≤ 3 次 |
| **P1** | 分类归位（§3.1 的 1/2/4/5/6） | 2h | 低 | 常规页不再出现 `playback.*`；声音页含音量步长；音乐页拆 3 组 |
| **P1** | 补齐 4 个缺失高频项（最低倍速/记忆最短时长/字幕阴影/最近条数）+ 4 个隐藏快捷键 | 2h | 低 | 8 个键在面板可见可改并生效 |
| **P2** | 控件形态升级（§3.2 的 1~9） | 1~2 天 | 中 | 同语义控件跨页面统一（颜色拾色器、档位 Combo、字幕 Slider） |
| **P2** | 快捷键页搜索/分组/冲突检测；关于页入口；卡片版式统一 | 1 天 | 低 | 52 个捕获框可检索；关于页有目录入口 |

---

## 4. 附录

### 附录 A：校验方法（可复跑）

**A1. 反射 `config.DEFAULTS`（得到 91 个扁平键与真实默认值）**

```python
import sys; sys.path.insert(0, "src")
from zhijian.config import DEFAULTS
def flat(d, pre=""):
    out = {}
    for k, v in d.items():
        p = (pre + "." + k) if pre else k
        if isinstance(v, dict): out.update(flat(v, p))
        else: out[p] = v
    return out
print(len(flat(DEFAULTS)))   # -> 91
```

**A2. AST 提取设置面板的控件工厂调用（得到 55 处 key + 标签 + 面板兜底值）**

- 遍历 `ast`，命中 `_check / _spin / _dspin / _combo / _slider_row / _slider_row_d / _color_edit` 的 `Call` 节点；
- 第 1 个字面量参数 = 配置键；`_check` 的第 2 参 = UI 标签、第 3 参 = 布尔兜底；`_combo` 的第 3 参 = 兜底默认值；
- 与 A1 结果逐键比对得到「14 个未登记键」与「23 处兜底不一致」两张表。

> 说明：55 处工厂调用里有 1 处是死分支（`sp:792` 的 `_color_edit("ui.accent_override") if False`），故**可见工厂键 = 54**；另有 5 个键由**手写控件**承载、不走工厂（`playback.long_press_tiers` 的 QLineEdit、`subtitle.font_family` 的可编辑下拉、`ui.theme` 的明暗按钮与主题下拉、`audio.volume` 的滑条、`audio.device` 的动态下拉），合计 **59 个可见配置键**。快捷键 26 个动作来自 `HOTKEY_LABELS`（`sp:66-93`）。

**A3. 判定「实时生效」的 grep 口径**

- 对每个键，在 `src/zhijian/**/*.py`（排除 `settings_panel.py`、`config.py`）中搜索 `"<完整键名>"`；
- 命中 = 有读取点 → 按读取点所在函数判定 ✅ / ◐ / ▲；0 命中 = ✖ 无效；
- 例外需人工核对：读取方被硬编码覆盖（`image.wheel_invert`）、半失效（`subtitle.auto_load`）、只写不读（`ui.image_fit`）。

**A4. 实盘配置抽样（只读）**

```
%APPDATA%\ZhiJian\ZhiJianPlayer\config.json   version=7, 98 个扁平键
  ui.preview_thumb = False        ← 键存在但不在 DEFAULTS（用户改过才落盘）
  ui.theme = graphite             ← 与 config 默认 aurora、theme 默认 graphite 的三层差异可见
  lyric.weight / image.wheel_invert / ui.external_open_mode 等 7 个键缺键（14 个未登记键中 7 个已落盘、7 个缺键）
  ← 缺键时 UI 走面板兜底值，其中 10 处与运行时默认相反（§2.10-B）
```

### 附录 B：19 个「有默认值但设置界面不可达」的配置键

| 归类 | 配置键 | 现状 |
| --- | --- | --- |
| 运行时状态（不该出现在设置里，共 6） | `version`、`ui.window_x`、`ui.window_y`、`ui.maximized`、`ui.drawer_open`、`recent.files` | 程序自己读写，正确做法是保持不可见 |
| 已实现但未暴露（共 4） | `playback.speed_min`、`playback.resume_min_secs`、`subtitle.shadow_offset`、`recent.max` | 有真实读取点，建议补进设置页（§3.4-1~4） |
| 未实现 / 死键（共 5） | `playback.long_press_speed`（v5 起废弃）、`playback.loop_all`、`subtitle.scale_with_window`、`ui.drawer_auto_hide`、`ui.language` | 全仓 0 处读取（`long_press_speed` 仅迁移用），建议清理或实现 |
| 隐藏快捷键（共 4） | `hotkeys.escape`、`hotkeys.stop`、`hotkeys.larger_ui`、`hotkeys.smaller_ui` | 见 §3.3-14 |

### 附录 C：术语

| 术语 | 含义 |
| --- | --- |
| 设置行 | 面板里一个「标签 + 控件」构成的一行；`ui.theme` 有两个控件视图，故 59 键对应 60 行 |
| 面板兜底 | 控件工厂构造控件时 `cfg.get(key, <内联默认>)` 里的内联默认值，仅在配置缺键时可见 |
| 实时生效 | 改动后无需重启、无需重开文件，运行时下一次读取即生效（本报告按读取点函数逐个判定） |
| 半失效 | 值被读取，但读取方只影响非核心行为（例如只控提示、不控功能） |

---

## 5. 交付说明与边界声明

1. **只读**：本报告全部数据来自静态扫描与只读抽样，未运行播放器、未改动任何产品代码；`SETTINGS_AUDIT.md` 是本轮唯一新增文件。
2. **主体文件稳定**：审计主体 `settings_panel.py` / `config.py` 在工作区**无未提交改动**，因此行号可复现。
3. **行号会漂移的部分**：`main_window.py`、`widgets.py`、`theme.py` 正被并行任务修改，本报告引用其行号时已标注「扫描时」，并尽量同时给出函数名；复审时请以**函数名**为准。
4. **未纳入**：本报告不含运行时观感（颜色对比度、模糊、动画）与像素级验收，那属于视觉审计任务；本报告只回答「有哪些项、长什么样、值对不对、改完有没有用」。
5. **建议下一步**：由 UX 评审对 §3.7 的 P0/P1 逐条裁决，形成下一轮的设置面板改版卡；本报告可直接作为改版卡的需求清单与验收依据。

