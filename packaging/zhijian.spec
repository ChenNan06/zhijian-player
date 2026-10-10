# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格文件：至简Player（onedir 绿色便携版）。

⚠️ PyInstaller 打包 PySide6「最容易看起来成功、其实什么都没打进去」。
   判据是日志里必须出现 hook-PySide6.py / hook-PySide6.QtWidgets.py 这些行,
   而且**冒烟测试不能只看"进程还活着"** —— bootloader 会赖着不死,
   必须同时满足：进程存活 + 应用日志出现启动横幅 + EnumWindows 找到主窗口标题。
"""

import os

block_cipher = None

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
RUNTIME = os.path.join(ROOT, "runtime")
ASSETS = os.path.join(ROOT, "assets")

datas = []
if os.path.isdir(ASSETS):
    datas.append((os.path.join(ASSETS, "icon.ico"), "assets"))
    datas.append((os.path.join(ASSETS, "icon.png"), "assets"))

# 进度条悬停缩略图用的 ffmpeg（子进程抽帧）。
# ⚠️ 必须**显式打进包**：imageio_ffmpeg 把二进制放在
#    site-packages/imageio_ffmpeg/binaries/，PyInstaller 的自动收集不一定能发现它。
#    打不进去的话，打包版的悬停预览会静默失效。
binaries = [(os.path.join(RUNTIME, "libmpv-2.dll"), ".")]
try:
    import imageio_ffmpeg as _iff
    _ff = _iff.get_ffmpeg_exe()
    if _ff and os.path.exists(_ff):
        binaries.append((_ff, "imageio_ffmpeg/binaries"))
except Exception:
    pass

# PySide6 里用不到的模块：全部剔除（瘦身 ~120MB）
EXCLUDES = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtSql", "PySide6.QtTest", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtHelp",
    "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtHttpServer", "PySide6.QtGraphs",
    "PySide6.QtNetworkAuth", "PySide6.QtLocation", "PySide6.QtOpenGL",
    "tkinter", "unittest", "pydoc", "doctest", "test", "lib2to3",
    "numpy", "pandas", "matplotlib", "scipy", "PIL.ImageQt",
]

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[SRC],                      # 包不在工程根目录时必须显式加, 否则一个模块都不收集
    binaries=binaries,
    datas=datas,
    hiddenimports=["PySide6.QtSvg", "PySide6.QtNetwork", "PySide6.QtOpenGLWidgets"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="至简Player",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                     # 播放器不该弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ASSETS, "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="至简Player",
)
