# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/AI-Project/至简Player/packaging/installer.py'],
    pathex=['D:/AI-Project/至简Player/src'],
    binaries=[],
    datas=[('D:/AI-Project/至简Player/build/payload.zip', '.'), ('D:/AI-Project/至简Player/assets/icon.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtWebEngineCore', 'PySide6.QtMultimedia', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtNetwork', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='至简Player-Setup-1.0.26',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['D:/AI-Project/至简Player/assets/icon.ico'],
)
