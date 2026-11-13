# 至简Player v1.0.55 · 副屏整窗点不动（HTNOWHERE）修复报告

**主题**：副屏上窗口**完全点不动**（连拖回主屏都不行）—— `WM_NCHITTEST` 返回 `HTNOWHERE` 导致整窗鼠标透明
**版本**：v1.0.54 → **v1.0.55**｜**提交**：`15403f9`｜**日期**：2026-11-13
**交付**：[Setup](output/至简Player-Setup-1.0.55.exe) · [Portable](output/至简Player-Portable-1.0.55.zip)

---

## 一、现象与证伪过程

**用户现象**：播放器拉到副屏（914×1463 @(-1600,-527)，DPR 1.75）后**点什么都没反应**，连把窗口拖回主屏都做不到，「像被遮挡」；主屏一切正常。

**第一轮取证（副屏真实鼠标，用户已授权）**：

| 实验 | 结果 |
|---|---|
| 真实 `SetCursorPos` + `SendInput` 点 10 个可达点 | `mouse.log` 中鼠标类事件 **0 条**（日志器本身记录了 1800+ 行其它事件，工作正常）|
| 同坐标 `PostMessage(WM_LBUTTONDOWN/UP)` | **能进**：`QWindow→IconButton→ControlBar→Stage→WindowBody→MainWindow` |
| `QTest.mouseClick(btn_play)` | **能触发** `clicked 0→1` |
| 只发 press 不 release | `btn_play._pressed` 仍 false ⇒ **物理 press 从未到达控件** |
| `WindowFromPoint` | = 我们的主窗口、**前台窗口**、`exstyle=0x100`（无 LAYERED/TRANSPARENT）、`IsWindowEnabled=true`、`mask` 空 |
| `activePopupWidget / activeModalWidget / mouseGrabber` | 全 None |
| 进程内窗口清单 | 仅 1 个可见播放器窗 + 2 个隐藏 1×1 Qt 辅助窗，**无浮层遮挡** |
| 对照：另一进程的最小 Qt 窗（副屏同区域） | 真实点击 **5 次全部收到** ⇒ **系统级输入通路正常** |
| `GetCapture()` / `GetGUIThreadInfo` | capture 为空、active/focus 都是本窗口 |

⇒ **物理鼠标消息在 Windows 输入队列层就没送到本窗口**，而 `PostMessage`（绕过 NCHITTEST）与 `QTest`（直接进 Qt）都能进 ⇒ **问题在窗口的命中返回码**。

---

## 二、真因：`local_point()` 假定屏幕逻辑原点为 (0,0)

`src/zhijian/platform/win.py::local_point()` 负责把 `WM_*` 消息里的**物理屏幕坐标**换成**窗口内逻辑坐标**，旧实现：

```python
gx, gy = x / dpr, y / dpr          # 物理 → 逻辑（假定屏幕逻辑原点 = 0）
p = win.mapFromGlobal(QPoint(gx, gy))   # 全局逻辑 → 窗口局部
```

主屏原点 (0,0)、DPR 1.0 ⇒ 成立。**副屏原点 (-1600,-527)、DPR 1.75** 时（物理 x = -802）：

```
-802 / 1.75 = -458        → mapFromGlobal → local x = 1142   # 窗口只有 914 宽！
1142 >= width(914)        ⇒ hit_test 判越界
                          ⇒ WM_NCHITTEST 返回 HTNOWHERE (0)
                          ⇒ Windows 认为整窗「鼠标透明」
                          ⇒ 所有物理点击/拖动穿透到桌面
```

**这解释了全部现象**：截图正常、`WindowFromPoint` 指向我们、`PostMessage`/`QTest` 能进、真实鼠标全穿透、用户感觉「被遮挡」。

---

## 三、修法（一处）

```python
dpr  = float(win.devicePixelRatioF() or 1.0)
hwnd = _hwnd(win)
pt   = wintypes.POINT(int(x), int(y))
if _bind().ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt)):
    return QPoint(round(pt.x / dpr), round(pt.y / dpr))   # 系统处理负坐标 + 每屏 DPI
# 兜底（无 hwnd / 调用失败）：旧逻辑
gx, gy = _lparam_xy(lparam, dpr)
return win.mapFromGlobal(QPoint(gx, gy))
```

`ScreenToClient` 返回**客户区物理坐标**，`/dpr` 即逻辑客户坐标；主屏（原点 0,0）时与旧算法**数学恒等** ⇒ **无主屏回归**。

**影响面**：`local_point()` 全仓仅 3 处调用 —— `platform/win.py` 的 `hit_test()`（NCHITTEST 本体）、`main_window.py` 的 `nativeEvent → _classify_client`、调试日志行。**无热区/拖动阈值调用**。

---

## 四、修复验证

### 1. `WM_NCHITTEST` 真实返回码（同一实例、同一批点）

| 点位 | 修复前 | 修复后 |
|---|---|---|
| 标题栏中心 (-1144,-504) | **HTNOWHERE (0)**，local=(1142, 249) | **HTCAPTION (2)**，local=(456, 23) |
| 播放键 (-1144, 630) | **HTNOWHERE (0)**，local=(1142, 1382) | **HTCLIENT (1)**，local=(456, 1157) |
| 画面中央 (-1144, 85) | **HTNOWHERE (0)**，local=(1142, 838) | **HTCLIENT (1)**，local=(456, 612) |

### 2. 真实鼠标交互（副屏）

| 交互 | 结果 |
|---|---|
| 点播放键 | `time-pos` 4.23 → **4.50（冻结 1s）** → 再点 → **5.40** ⇒ 暂停/恢复生效 |
| 拖标题栏 | 窗口 y **-527 → -486**（QA 独立复验 `dy=+41`）⇒ 真实拖动跟手，**「拖不回主屏」同步解决** |

### 3. QA 独立复验

| 判据 | 结果 |
|---|---|
| NCHITTEST 三点 | **2 / 1 / 1**，全非 0 ✓✓ |
| 真实拖动 | `dy = +41` ✓✓ |
| `local_point` 副屏换算 | 物理 (−1500,−507) ⇒ local **(57, 11)**，手算 100/1.75=57.1 **精确吻合** ✓✓ |

---

## 五、交付

| 项 | 结果 |
|---|---|
| 打包门禁 | **64 / 64** |
| 交付 | Setup 185.1 MB / Portable 127.5 MB |
| 关联 / 清理 | 指向 1.0.55 + SHChangeNotify；`output/` 仅保留 1.0.55 |
| 关机命令 | **0 次** |

---

## 六、方法论（本轮最值钱的一条）

> **`HTNOWHERE` = 整窗鼠标透明。** Windows 的命中测试链是：
> `Cursor → WindowFromPoint（窗口层）→ 发 WM_NCHITTEST（窗口内部）→ 返回 HTCAPTION/HTCLIENT/… → 决定消息去向`；
> 只要应用在 `WM_NCHITTEST` 里返回 `HTNOWHERE`，**即使窗口在最上层、可见、可用、前台**，所有物理点击都会穿透 —— 而 `PostMessage`/`QTest` **不经过这一步**，所以会出现「自动化全通过、真人点不动」的诡异局面。

因此排查「点不动」时，必须**同时**验三层：

1. **窗口层**：`WindowFromPoint` 是否指回本窗口、`exstyle` 有没有 `WS_EX_TRANSPARENT`/`WS_EX_LAYERED`、`IsWindowEnabled`；
2. **命中返回码**：`SendMessage(WM_NCHITTEST)` 的返回值 —— **这一层最容易被忽略**；
3. **Qt 层**：`QApplication.widgetAt` / `hit_test` / `activePopup` / `mouseGrabber`。

而本轮 bug 的根，是**多显示器坐标换算**：任何「物理 → 逻辑」的算术都必须走 `ScreenToClient`（或 `MonitorFromPoint` + 每屏原点），**不能假定屏幕逻辑原点为 0**。