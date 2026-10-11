"""至简Player 安装向导 / 卸载程序（同一个 exe，两种角色）。

* 默认角色 = 安装向导（四步：欢迎 → 选择位置 → 安装 → 完成）
* 带 `--uninstall <安装目录>` = 卸载程序

全部写在 HKCU 下（快捷方式、文件关联、卸载登记），**不需要管理员权限**。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "至简Player"
APP_ID = "ZhiJianPlayer"
VERSION = "1.0.12"
PUBLISHER = "ZhiJian"

# 与程序内同源的配色
#
# ⚠️ 这里**绝对不能出现 8 位十六进制**（如 `#FFFFFF2E`）。
#    Qt 的 8 位写法是 `#AARRGGBB`, 把 CSS 习惯的 `#RRGGBBAA` 交给它,
#    会把透明度和颜色对调 —— 本项目真实踩过：`#FFFFFF0F` 被解析成
#    "不透明的黄色", 安装向导的按钮和输入框整片变成亮黄。
#    要透明度就用 `rgba(r,g,b,a)`。
BG = "#0F1116"
CARD = "#171B23"
TEXT = "#F3F5F9"
DIM = "#A8B0BF"
MUTED = "#6D7686"
ACCENT = "#2FD3C0"
ACCENT_HOVER = "rgba(47,211,192,0.85)"
ON_ACCENT = "#04241F"
HAIR = "rgba(255,255,255,0.18)"
FILL_1 = "rgba(255,255,255,0.06)"
FILL_2 = "rgba(255,255,255,0.12)"
FILL_3 = "rgba(255,255,255,0.03)"
FILL_4 = "rgba(255,255,255,0.08)"

ASSOC = {
    # 扩展名 -> 描述
    ".mp4": "视频文件", ".mkv": "视频文件", ".mov": "视频文件", ".avi": "视频文件",
    ".wmv": "视频文件", ".flv": "视频文件", ".webm": "视频文件", ".m4v": "视频文件",
    ".ts": "视频文件", ".rmvb": "视频文件", ".mpg": "视频文件", ".mpeg": "视频文件",
    ".3gp": "视频文件", ".vob": "视频文件", ".m2ts": "视频文件",
    ".jpg": "图片文件", ".jpeg": "图片文件", ".png": "图片文件", ".gif": "图片文件",
    ".bmp": "图片文件", ".webp": "图片文件", ".avif": "图片文件", ".tif": "图片文件",
    ".tiff": "图片文件", ".ico": "图片文件",
    ".mp3": "音频文件", ".flac": "音频文件", ".wav": "音频文件", ".aac": "音频文件",
    ".m4a": "音频文件", ".ogg": "音频文件", ".opus": "音频文件", ".wma": "音频文件",
    ".ape": "音频文件", ".ac3": "音频文件", ".mka": "音频文件",
    ".srt": "字幕文件", ".ass": "字幕文件", ".vtt": "字幕文件",
}


def _out(msg: str) -> None:
    """安全输出。

    ⚠️ PyInstaller `--windowed` 打包后 **sys.stdout 是 None**, 直接 print() 会抛
    AttributeError。在无界面的静默安装路径上，这个异常会弹出 PyInstaller 自己的
    错误对话框并**永远卡在那里** —— 表现为"安装包跑到一半不动了"。
    """
    try:
        if sys.stdout is not None:
            sys.stdout.write(str(msg) + "\n")
            sys.stdout.flush()
    except Exception:
        pass
    try:
        if sys.stderr is not None and getattr(sys, "_zhijian_err", False):
            sys.stderr.write(str(msg) + "\n")
    except Exception:
        pass


def _logfile() -> str:
    try:
        base = os.environ.get("TEMP") or os.path.expanduser("~")
        return os.path.join(base, "zhijian_installer.log")
    except Exception:
        return ""


def _log(msg: str) -> None:
    _out(msg)
    p = _logfile()
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def app_icon() -> QIcon:
    for cand in (
        os.path.join(getattr(sys, "_MEIPASS", ""), "icon.ico"),
        os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "icon.ico"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "icon.ico"),
    ):
        if cand and os.path.isfile(cand):
            return QIcon(cand)
    return QIcon()


def stylesheet() -> str:
    return f"""
    * {{ font-family: "Microsoft YaHei UI","Microsoft YaHei","Segoe UI",sans-serif;
         font-size: 13px; color: {TEXT}; }}
    QWidget#Root {{ background: {BG}; }}
    QWidget#Card {{ background: {CARD}; border: 1px solid {HAIR}; border-radius: 10px; }}
    QLabel[role="h1"] {{ font-size: 20px; font-weight: 600; }}
    QLabel[role="h2"] {{ font-size: 15px; font-weight: 600; }}
    QLabel[role="dim"] {{ color: {DIM}; }}
    QLabel[role="muted"] {{ color: {MUTED}; font-size: 12px; }}
    QLineEdit {{ background: {FILL_1}; border: 1px solid {HAIR}; border-radius: 8px;
                 padding: 8px 10px; color: {TEXT}; }}
    QLineEdit:focus {{ border-color: {ACCENT}; }}
    QCheckBox {{ color: {DIM}; spacing: 9px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
        border: 1px solid {HAIR}; background: {FILL_3}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
    QPushButton[role="primary"] {{ background: {ACCENT}; color: {ON_ACCENT}; border: none;
        border-radius: 8px; padding: 9px 26px; font-weight: 600; }}
    QPushButton[role="primary"]:hover {{ background: {ACCENT_HOVER}; }}
    QPushButton[role="ghost"] {{ background: {FILL_1}; color: {DIM};
        border: 1px solid {HAIR}; border-radius: 8px; padding: 9px 22px; }}
    QPushButton[role="ghost"]:hover {{ background: {FILL_2}; color: {TEXT}; }}
    QPushButton:disabled {{ color: {MUTED}; background: {FILL_3}; }}
    QProgressBar {{ background: {FILL_4}; border: none; border-radius: 6px;
        height: 12px; text-align: center; color: {DIM}; font-size: 11px; }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}
    """


# ---------------------------------------------------------------------------
# 快捷方式 / 注册表
# ---------------------------------------------------------------------------

def _run_ps(script: str) -> str:
    """把 PowerShell 脚本写到临时 .ps1 再 -File 执行。

    ⚠️ 绝不要用 `powershell -Command "<长脚本>"`：参数会被 PowerShell 自己的
    命令行解析切碎（实测 `$sc.TargetPath = '...'` 被拆成 `= '...'`, 报
    "The term '=' is not recognized"）, 而且中文路径经参数传递还会乱码。
    写文件 + `-File` 两个问题一起解决。含中文的 .ps1 必须带 BOM。
    """
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8-sig") as f:
            # 不设 OutputEncoding 的话, 重定向时 PS 5.1 按 OEM 代码页吐字节,
            # 中文路径回读会变成一串 '?'（本项目实测）。
            f.write("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
                    "$OutputEncoding = [System.Text.Encoding]::UTF8\n")
            f.write(script)
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=40)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        if r.returncode != 0:
            _log(f"powershell 失败 rc={r.returncode}: {err.strip()[:300]}")
        return out
    except Exception as exc:
        _log(f"powershell 执行异常: {exc}")
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def make_shortcut(lnk: str, target: str, args: str = "", workdir: str = "",
                  icon: str = "", desc: str = "") -> bool:
    def q(s: str) -> str:
        return "'" + str(s).replace("'", "''") + "'"

    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$sc = $ws.CreateShortcut({q(lnk)})\n"
        f"$sc.TargetPath = {q(target)}\n"
        f"$sc.Arguments = {q(args)}\n"
        f"$sc.WorkingDirectory = {q(workdir or os.path.dirname(target))}\n"
        f"$sc.IconLocation = {q((icon or target) + ',0')}\n"
        f"$sc.Description = {q(desc)}\n"
        "$sc.Save()\n"
        "Write-Output 'OK'\n"
    )
    out = _run_ps(script)
    return "OK" in out and os.path.isfile(lnk)


def registry_associate(exe: str, exts: list[str], with_default: bool) -> None:
    import winreg

    prog_id = f"{APP_ID}.Media"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\{prog_id}", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f"{APP_NAME} 媒体文件")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\{prog_id}\DefaultIcon", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f"{exe},0")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\{prog_id}\shell\open\command", 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" "%1"')
    for ext in exts:
        try:
            if with_default:
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                        rf"Software\Classes\{ext}", 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, prog_id)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                    rf"Software\Classes\{ext}\OpenWithProgids", 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, prog_id, 0, winreg.REG_NONE, b"")
        except OSError:
            continue


def registry_unassociate(exts: list[str]) -> None:
    import winreg

    prog_id = f"{APP_ID}.Media"
    for ext in exts:
        for path in (rf"Software\Classes\{ext}\OpenWithProgids",):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, prog_id)
            except OSError:
                pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}", 0,
                                winreg.KEY_QUERY_VALUE) as k:
                if winreg.QueryValueEx(k, "")[0] == prog_id:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}")
        except OSError:
            pass
    try:
        shutil.rmtree  # noqa: B018
        import winreg as wr

        def deltree(root):
            try:
                with wr.OpenKey(wr.HKEY_CURRENT_USER, root, 0, wr.KEY_ALL_ACCESS) as k:
                    while True:
                        try:
                            sub = wr.EnumKey(k, 0)
                        except OSError:
                            break
                        deltree(root + "\\" + sub)
                wr.DeleteKey(wr.HKEY_CURRENT_USER, root)
            except OSError:
                pass

        deltree(rf"Software\Classes\{prog_id}")
    except Exception:
        pass


def register_uninstall(install_dir: str, exe: str, size_kb: int) -> None:
    import winreg

    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, f"{exe},0")
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ,
                          f'"{os.path.join(install_dir, "卸载 至简Player.exe")}"')
        winreg.SetValueEx(k, "QuietUninstallString", 0, winreg.REG_SZ,
                          f'"{os.path.join(install_dir, "卸载 至简Player.exe")}" --silent')
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, int(size_kb))
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


def unregister_uninstall() -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}")
    except OSError:
        pass


def payload_path() -> str:
    for cand in (
        os.path.join(getattr(sys, "_MEIPASS", ""), "payload.zip"),
        os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "payload.zip"),
    ):
        if cand and os.path.isfile(cand):
            return cand
    raise FileNotFoundError("安装包内缺少 payload.zip")


# ---------------------------------------------------------------------------
# 安装向导
# ---------------------------------------------------------------------------

class Installer(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle(f"{APP_NAME} {VERSION} 安装向导")
        self.setWindowIcon(app_icon())
        self.setFixedSize(660, 470)
        self.setStyleSheet(stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(QSize(46, 46)))
        head.addWidget(logo)
        tbox = QVBoxLayout()
        t1 = QLabel(f"安装 {APP_NAME}")
        t1.setProperty("role", "h1")
        t2 = QLabel(f"三合一全能媒体查看器 · 版本 {VERSION}")
        t2.setProperty("role", "dim")
        tbox.addWidget(t1)
        tbox.addWidget(t2)
        head.addLayout(tbox)
        head.addStretch(1)
        root.addLayout(head)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._page_welcome())
        self.stack.addWidget(self._page_path())
        self.stack.addWidget(self._page_progress())
        self.stack.addWidget(self._page_done())
        root.addWidget(self.stack, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self.btn_back = QPushButton("上一步")
        self.btn_back.setProperty("role", "ghost")
        self.btn_back.clicked.connect(self._back)
        self.btn_next = QPushButton("下一步")
        self.btn_next.setProperty("role", "primary")
        self.btn_next.clicked.connect(self._next)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setProperty("role", "ghost")
        self.btn_cancel.clicked.connect(self.close)
        bar.addWidget(self.btn_cancel)
        bar.addWidget(self.btn_back)
        bar.addWidget(self.btn_next)
        root.addLayout(bar)

        self._page = 0
        self._sync()
        self._busy = False

    # ---- 页面 ----
    def _card(self) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        w.setObjectName("Card")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)
        return w, lay

    def _page_welcome(self) -> QWidget:
        w, lay = self._card()
        t = QLabel("欢迎使用 至简Player")
        t.setProperty("role", "h2")
        lay.addWidget(t)
        body = QLabel(
            "· 视频 / 图片 / 音频 三合一，拖进去就能看\n"
            "· 基于 libmpv + OpenGL 硬件纹理共享，4K 硬解、随意拖拽进度条\n"
            "· 长按 → 方向键平滑升到 2 倍速，松开立即恢复\n"
            "· 字幕可调字体 / 字号 / 描边颜色 / 垂直位置 / 时间轴 ±0.5s\n"
            "· 绿色便携：不写系统目录、不留后台进程、关闭即完全退出\n\n"
            "本向导会把程序安装到你选择的位置，并可创建快捷方式与文件关联。\n"
            "全程只写当前用户（HKCU），不需要管理员权限。"
        )
        body.setProperty("role", "dim")
        body.setWordWrap(True)
        lay.addWidget(body)
        lay.addStretch(1)
        return w

    def _page_path(self) -> QWidget:
        w, lay = self._card()
        t = QLabel("选择安装位置")
        t.setProperty("role", "h2")
        lay.addWidget(t)
        row = QHBoxLayout()
        self.path_edit = QLineEdit(self._default_dir())
        browse = QPushButton("浏览…")
        browse.setProperty("role", "ghost")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        hint = QLabel("程序本体约 210 MB（其中 libmpv 解码内核 115 MB）。")
        hint.setProperty("role", "muted")
        lay.addWidget(hint)
        lay.addSpacing(6)
        self.chk_desktop = QCheckBox("创建桌面快捷方式")
        self.chk_desktop.setChecked(True)
        self.chk_start = QCheckBox("创建开始菜单快捷方式")
        self.chk_start.setChecked(True)
        self.chk_assoc = QCheckBox("把常见视频 / 图片 / 音频格式关联到 至简Player")
        self.chk_assoc.setChecked(True)
        self.chk_gpu = QCheckBox("登记为「高性能 GPU」应用（混合显卡笔记本走独显硬解）")
        self.chk_gpu.setChecked(True)
        for c in (self.chk_desktop, self.chk_start, self.chk_assoc, self.chk_gpu):
            lay.addWidget(c)
        lay.addStretch(1)
        return w

    def _page_progress(self) -> QWidget:
        w, lay = self._card()
        t = QLabel("正在安装…")
        t.setProperty("role", "h2")
        lay.addWidget(t)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        lay.addWidget(self.progress)
        self.status = QLabel("准备中…")
        self.status.setProperty("role", "dim")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch(1)
        return w

    def _page_done(self) -> QWidget:
        w, lay = self._card()
        t = QLabel("安装完成")
        t.setProperty("role", "h2")
        lay.addWidget(t)
        self.done_text = QLabel("")
        self.done_text.setProperty("role", "dim")
        self.done_text.setWordWrap(True)
        lay.addWidget(self.done_text)
        self.chk_launch = QCheckBox("立即启动 至简Player")
        self.chk_launch.setChecked(True)
        lay.addWidget(self.chk_launch)
        lay.addStretch(1)
        return w

    def _default_dir(self) -> str:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Programs", APP_NAME)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择安装位置", self.path_edit.text())
        if d:
            self.path_edit.setText(os.path.join(d, APP_NAME))

    # ---- 导航 ----
    def _sync(self):
        self.stack.setCurrentIndex(self._page)
        self.btn_back.setEnabled(self._page == 1)
        self.btn_back.setVisible(self._page in (1,))
        if self._page == 0:
            self.btn_next.setText("下一步")
            self.btn_next.setEnabled(True)
        elif self._page == 1:
            self.btn_next.setText("开始安装")
            self.btn_next.setEnabled(True)
        elif self._page == 2:
            self.btn_next.setText("安装中…")
            self.btn_next.setEnabled(False)
        else:
            self.btn_next.setText("完成")
            self.btn_next.setEnabled(True)
        self.btn_cancel.setVisible(self._page != 3)

    def _back(self):
        if self._page > 0 and not self._busy:
            self._page -= 1
            self._sync()

    def _next(self):
        if self._busy:
            return
        if self._page == 0:
            self._page = 1
            self._sync()
        elif self._page == 1:
            self._page = 2
            self._sync()
            threading.Thread(target=self._do_install, daemon=True).start()
        elif self._page == 3:
            if self.chk_launch.isChecked():
                self._launch()
            self.close()

    # ---- 安装 ----
    def _do_install(self):
        try:
            target = self.path_edit.text().strip()
            os.makedirs(target, exist_ok=True)
            zpath = payload_path()
            with zipfile.ZipFile(zpath) as z:
                names = z.namelist()
                total = max(1, len(names))
                for i, n in enumerate(names):
                    z.extract(n, target)
                    if i % 25 == 0:
                        self._ui(lambda p=i / total: self._set_progress(
                            p, f"正在释放文件 {i}/{total}…"))
            self._ui(lambda: self._set_progress(0.97, "正在创建快捷方式…"))
            exe = self._find_exe(target)
            icon = os.path.join(target, "_internal", "assets", "icon.ico")
            if not os.path.isfile(icon):
                icon = exe
            if self.chk_desktop.isChecked():
                desktop = self._desktop_dir()
                make_shortcut(os.path.join(desktop, f"{APP_NAME}.lnk"), exe,
                              workdir=target, icon=icon, desc=f"{APP_NAME} 全能媒体查看器")
            if self.chk_start.isChecked():
                start = os.path.join(os.environ.get("APPDATA", ""),
                                     "Microsoft", "Windows", "Start Menu", "Programs")
                os.makedirs(os.path.join(start, APP_NAME), exist_ok=True)
                make_shortcut(os.path.join(start, APP_NAME, f"{APP_NAME}.lnk"), exe,
                              workdir=target, icon=icon, desc=f"{APP_NAME} 全能媒体查看器")
            if self.chk_assoc.isChecked():
                self._ui(lambda: self._set_progress(0.99, "正在登记文件关联…"))
                registry_associate(exe, list(ASSOC.keys()), with_default=True)
            if self.chk_gpu.isChecked():
                try:
                    sys.path.insert(0, target)
                    import winreg

                    with winreg.CreateKeyEx(
                            winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\DirectX\UserGpuPreferences", 0,
                            winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, exe, 0, winreg.REG_SZ, "GpuPreference=2;")
                except Exception:
                    pass
            # 便携标记 & 卸载程序副本
            open(os.path.join(target, "portable.txt"), "w").close()
            size_kb = int(sum(os.path.getsize(os.path.join(r, f))
                              for r, _d, fs in os.walk(target) for f in fs) / 1024)
            register_uninstall(target, exe, size_kb)
            self._uninstaller_path = os.path.join(target, f"卸载 {APP_NAME}.exe")
            self._ui(lambda: self._finish(target, exe))
        except Exception as exc:
            self._ui(lambda e=exc: self._fail(e))

    def _desktop_dir(self) -> str:
        try:
            import winreg

            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
                return winreg.QueryValueEx(k, "Desktop")[0]
        except Exception:
            return os.path.join(os.path.expanduser("~"), "Desktop")

    @staticmethod
    def _find_exe(target: str) -> str:
        for f in sorted(os.listdir(target)):
            if f.lower().endswith(".exe") and "unins" not in f.lower() and "卸载" not in f:
                return os.path.join(target, f)
        return os.path.join(target, f"{APP_NAME}.exe")

    def _ui(self, fn):
        QTimer.singleShot(0, fn)

    def _set_progress(self, frac: float, text: str):
        self.progress.setValue(int(max(0, min(1, frac)) * 100))
        self.status.setText(text)

    def _finish(self, target: str, exe: str):
        self._busy = False
        self._target = target
        self._exe = exe
        self._set_progress(1.0, "完成")
        self.done_text.setText(
            f"已安装到：\n{target}\n\n"
            f"可执行文件：{os.path.basename(exe)}\n"
            "控制面板 / 设置 → 应用 里会出现「至简Player」，可随时卸载。"
        )
        self._page = 3
        self._sync()

    def _fail(self, exc: Exception):
        self._busy = False
        self._page = 1
        self._sync()
        self.status.setText(f"安装失败：{exc}")

    def _launch(self):
        try:
            subprocess.Popen([self._exe], cwd=self._target, close_fds=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 卸载
# ---------------------------------------------------------------------------

def do_uninstall(install_dir: str, silent: bool = False) -> int:
    # 优先交给安装目录里那个专门的小卸载程序（它知道怎么把自己挪走再删目录）
    native = os.path.join(install_dir, f"卸载 {APP_NAME}.exe")
    if os.path.isfile(native) and "--no-delegate" not in sys.argv:
        args = [native, "--uninstall", install_dir]
        if silent:
            args.append("--silent")
        try:
            return subprocess.run(args, timeout=120,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                  ).returncode
        except Exception:
            _log("委托给原生卸载程序失败, 自己来")

    if not silent:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox()
        box.setWindowTitle(f"卸载 {APP_NAME}")
        box.setText(f"确定要卸载 {APP_NAME} 吗？\n\n安装目录：{install_dir}\n"
                    "（你自己放在里面的媒体文件不会被删除）")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return 1

    # 1) 先删快捷方式与注册表
    try:
        import winreg

        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            desktop = winreg.QueryValueEx(k, "Desktop")[0]
    except Exception:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    for lnk in (os.path.join(desktop, f"{APP_NAME}.lnk"),):
        try:
            os.remove(lnk)
        except OSError:
            pass
    start = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                         "Start Menu", "Programs", APP_NAME)
    shutil.rmtree(start, ignore_errors=True)
    registry_unassociate(list(ASSOC.keys()))
    unregister_uninstall()
    # 用户配置
    shutil.rmtree(os.path.join(os.environ.get("APPDATA", ""), "ZhiJian", "ZhiJianPlayer"),
                  ignore_errors=True)

    # 2) 删安装目录 —— 但**必须等本进程退出**：卸载程序就在这个目录里
    #    ⚠️ .cmd 由 cmd.exe 按控制台代码页解码, 中文路径会乱码。用 8.3 短路径
    #       换成纯 ASCII, 彻底绕开代码页问题。
    short = install_dir
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetShortPathNameW.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        if k32.GetShortPathNameW(install_dir, buf, 1024):
            short = buf.value
    except Exception:
        pass
    marker = os.path.join(os.environ.get("TEMP", "."), f"zhijian_uninstall_{os.getpid()}.cmd")
    with open(marker, "w", encoding="ascii", errors="replace") as f:
        f.write("@echo off\r\n")
        f.write("ping -n 3 127.0.0.1 >nul\r\n")     # timeout 在无控制台时会失败
        f.write(f'rmdir /s /q "{short}"\r\n')
        f.write(f'del /q "{marker}"\r\n')
    # ⚠️ CREATE_NO_WINDOW 与 DETACHED_PROCESS **不能同时用**（Windows 直接拒绝创建进程），
    #    于是"延迟删除"那一步静默不执行 —— 表现成"卸载跑完了但目录还在"。
    DETACHED_PROCESS = 0x00000008
    try:
        subprocess.Popen(["cmd", "/c", marker], close_fds=True,
                         creationflags=DETACHED_PROCESS)
    except Exception:
        _log("启动延迟删除脚本失败, 改为同步删除")
        try:
            shutil.rmtree(install_dir, ignore_errors=True)
        except Exception:
            pass
    return 0


def silent_install(target: str, desktop: bool = True, start_menu: bool = True,
                   assoc: bool = False, gpu: bool = True) -> int:
    """无界面安装（企业批量部署 / 自动化验收用）。

    用法：`至简Player-Setup-x.y.z.exe --silent --dir "D:\\Apps\\至简Player" [--no-assoc]`
    """
    os.makedirs(target, exist_ok=True)
    with zipfile.ZipFile(payload_path()) as z:
        z.extractall(target)
    exe = Installer._find_exe(target)
    icon = os.path.join(target, "_internal", "assets", "icon.ico")
    if not os.path.isfile(icon):
        icon = exe
    try:
        import winreg

        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            desk = winreg.QueryValueEx(k, "Desktop")[0]
    except Exception:
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
    if desktop:
        make_shortcut(os.path.join(desk, f"{APP_NAME}.lnk"), exe, workdir=target,
                      icon=icon, desc=f"{APP_NAME} 全能媒体查看器")
    if start_menu:
        sm = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                          "Start Menu", "Programs", APP_NAME)
        os.makedirs(sm, exist_ok=True)
        make_shortcut(os.path.join(sm, f"{APP_NAME}.lnk"), exe, workdir=target,
                      icon=icon, desc=f"{APP_NAME} 全能媒体查看器")
    if assoc:
        registry_associate(exe, list(ASSOC.keys()), with_default=True)
    if gpu:
        try:
            import winreg

            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\DirectX\UserGpuPreferences", 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, exe, 0, winreg.REG_SZ, "GpuPreference=2;")
        except Exception:
            pass
    open(os.path.join(target, "portable.txt"), "w").close()
    size_kb = int(sum(os.path.getsize(os.path.join(r, f))
                      for r, _d, fs in os.walk(target) for f in fs) / 1024)
    register_uninstall(target, exe, size_kb)
    _log(f"OK installed to {target}")
    _log(f"EXE={exe}")
    _log(f"SIZE_KB={size_kb}")
    return 0


def _guarded_main() -> int:
    try:
        return main()
    except Exception:
        import traceback

        tb = traceback.format_exc()
        _log("INSTALLER FAILED:\n" + tb)
        # windowed 模式下没有控制台, 写一份到 TEMP 供排查; 静默模式不弹框
        if "--silent" not in sys.argv:
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox

                QApplication.instance() or QApplication(sys.argv[:1])
                QMessageBox.critical(None, "至简Player 安装程序", f"安装失败：\n\n{tb[-1200:]}")
            except Exception:
                pass
        return 3


def main() -> int:
    # ⚠️ 顺序要紧：`--uninstall` 必须排在 `--silent` 前面判断 ——
    #    否则 `--uninstall <dir> --silent` 会掉进静默安装分支, 变成"卸载器把程序又装了一遍"。
    if "--uninstall" in sys.argv:
        i = sys.argv.index("--uninstall")
        d = sys.argv[i + 1] if len(sys.argv) > i + 1 else os.path.dirname(
            os.path.abspath(sys.argv[0]))
        return do_uninstall(d, silent="--silent" in sys.argv)
    if "--silent" in sys.argv:
        d = None
        if "--dir" in sys.argv:
            d = sys.argv[sys.argv.index("--dir") + 1]
        d = d or os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                              "Programs", APP_NAME)
        return silent_install(d, desktop="--no-desktop" not in sys.argv,
                              start_menu="--no-startmenu" not in sys.argv,
                              assoc="--assoc" in sys.argv,
                              gpu="--no-gpu" not in sys.argv)

    QApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(stylesheet())
    w = Installer()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(_guarded_main())
