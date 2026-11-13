# 至简Player v1.0.57 · GIF 动画 + 副屏最大化修复报告

**主题**：GIF 不能播（全仓无 `QMovie`）· 副屏最大化越界（冷启动原生 `showMaximized` 失步）
**版本**：v1.0.55 → **v1.0.57**｜**提交**：`3775bb9`｜**日期**：2026-11-13
**交付**：[Setup](output/至简Player-Setup-1.0.57.exe) · [Portable](output/至简Player-Portable-1.0.57.zip)

> 纪律：用户在主屏玩游戏，全程只在副屏/离屏运行；未抢焦点、未移动真实鼠标、静音、截图只 `grab()`/`PrintWindow`。

---

## 一、GIF 不能播（Task-132）

### 真因

**全仓 `grep QMovie` 命中 0** —— `.gif` 只在扩展名/关联/mime 表里出现，图片管线走 `QImage/QPixmap`，**动画信息被直接丢弃**，所以只显示首帧、不动不循环。

### 修法

1. `image_view` 新增 **`QMovie` 管线**，仅 `.gif/.webp/.apng/.mng` 启用（**静态图不建 movie**，不为 jpg/png 付开销）；
2. `frameChanged → update()`；绘制统一走 `_current_image()`（**缩放/平移/适应窗口逻辑完全不变**）；
3. 切图 `stop() + deleteLater()`；`hideEvent` 暂停、`showEvent` 恢复；默认循环；
4. `isValid()==False` ⇒ `log.warning` + 回退静态首帧（不空屏）；
5. `debug_state()` 暴露 `{gif, movie_state, frame, frame_seq, frames, delay_ms}`。

### QA 抓到的两处追加缺陷（已修）

| # | 现象 | 真因 | 修法 |
|---|---|---|---|
| ① | `debug_state()['frame']` 1 秒 delta = 0 | **`currentFrameNumber()` 在 `CacheAll` 模式下不刷新**（实测恒 1） | 改读 `frameChanged` 记录的最后帧号，另加 `frame_seq` 单调计数 + `frame_view`（Qt 侧读数）|
| ② | 缩放 2× 把动画**重置回第 0 帧** | 缩放路径重进了 `load()`，`stop()` + 重建 `QMovie` | `load()` 加**同路径保护**：`_same_anim=True` 时不 stop 不重建，只刷新几何 + `update()` |

### 实测

| 判据 | 结果 |
|---|---|
| **widget 级像素**（`PrintWindow`，副屏真实窗口） | t0 `[227.3, 84.8, 84.8]`（红）→ t300 `[235.2, 211.4, 84.8]`（黄）→ t600 `[84.8, 195.6, 100.6]`（绿）⇒ **三点全不同** |
| 帧推进 | 1s 内 `frame_changes = 10`、`distinct = [0,1,2,3,4]`、`frame_seq` delta = 10 |
| 缩放 2× | `movie_id` **不变**、`frame_seq` 43→48→53 **连续不归零**、`state` 仍 Running(2) |
| 隐藏/显示 | `movie_state` **1 (Paused) / 2 (Running)** |
| 切到 jpg | `gif=false`、`_movie is None`、RSS Δ **14.9 MB**（< 20）|

> 口径更正：PySide6 `QMovie.State` 实际是 **NotRunning=0 / Paused=1 / Running=2**。

---

## 二、副屏最大化越界（Task-133）

### 真因

**冷启动 `ui.maximized=True` 走原生 `showMaximized()`** —— 在 frameless + PerMonitor DPI 下，HWND 与 Qt 逻辑几何**失步**：

```
期望  [-1600, -527, 914, 1415]
实测  [-1608, -226, 914, 1415]     ← 原点漂出屏幕，右/下越界
物理  1614 × 1083                  ← 与 914×1415@1.75 对不上
```

而标题栏按钮/双击路径（Qt 原生 toggle）反而是正确的 —— 所以这个 bug 只在「上次关窗时是最大化」的冷启动场景暴露。

### 修法

1. **`maximize_to_screen()`**：`self.screen().availableGeometry()` → 记 `_max_saved_geo` → `setGeometry(avail)` → `_max_by_us=True`，同步标题栏图标/`_sync_max_restore`/`apply_window_mask`/`place_capsule_host`；
2. **`restore_from_maximize()`**：按记录矩形**逐字段还原**；
3. **覆写 `isMaximized()`**（`_max_by_us or super().isMaximized()`）与 **`showMaximized()`**（冷启动与其它调用点统一走自有实现）；
4. **`_ensure_max_geometry()` 自愈**：最大化态下几何被后续重排挪走时，按 0/60/120/180/420/800ms 多帧拉回；
5. **四条入口统一**：标题栏按钮 / 双击标题栏 / 系统菜单 `WM_SYSCOMMAND SC_MAXIMIZE(0xF030)` `SC_RESTORE(0xF120)` / 冷启动 restored；
6. `stage.place_capsule_host()`：最大化按贴边 **gap=16**。

### 实测

| 入口 | 最大化后逻辑 rect | 物理 | 溢出 | 还原 |
|---|---|---|---|---|
| 标题栏按钮 / toggle | `[-1600,-527,914,1415]` | 1600×2476 | **0 / 0** | 逐字段相等 |
| 双击标题栏 | 同上 | 1600×2476 | **0 / 0** | 逐字段相等 |
| 系统菜单 SC_MAXIMIZE | 同上 | 1600×2476 | **0 / 0** | 逐字段相等 |
| 冷启动 maximized | 同上 | 1600×2476 | **0 / 0** | `[-1600,-527,914,1200]` |

`mask.isEmpty()` 走全屏分支 ✓；主屏侧**不可实测**（用户在游戏），已核对「全仓 8 处均为 `self.screen() or QApplication.primaryScreen()` 兜底，无 primaryScreen 硬编码参与最大化」。

---

## 三、交付

| 项 | 结果 |
|---|---|
| 打包门禁 | **64 / 64** |
| 交付 | Setup 185.1 MB / Portable 127.5 MB |
| 关联 / 清理 | 指向 1.0.57 + SHChangeNotify；`output/` 仅保留 1.0.57（1.0.55/1.0.56 已清）|
| 关机命令 | **0 次** |

---

## 四、方法论

1. **`QOpenGLWidget` 视口在离屏模式下 `grab()` 恒黑** —— widget 级像素证据必须用 **`PrintWindow(hwnd, hdc, 2)`**（真实窗口 + 副屏 + `SWP_NOACTIVATE`）；
2. **`QMovie.currentFrameNumber()` 在 `CacheAll` 下不可信** —— 进度类判据要自己数 `frameChanged` 次数；
3. **frameless + PerMonitor DPI 下不要依赖原生 `showMaximized()`** —— HWND 与 Qt 几何会脱钩，最大化必须自己按 `availableGeometry()` 摆位并覆写 `isMaximized()`；
4. 测试纪律要写成「可执行动作」：`WA_ShowWithoutActivating` **不够**，必须在 `show()` 后补 `SetWindowPos(HWND_BOTTOM, SWP_NOACTIVATE|SWP_NOMOVE|SWP_NOSIZE)` 并**每次操作后复测** `GetForegroundWindow() != hwnd`。