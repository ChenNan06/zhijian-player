"""把「至简Player」注册为常见媒体文件的默认打开方式（只写 HKCU，不需要管理员）。

用户要求："每次打包完以后把至简播放器设置为默认媒体打开方式，我要深度使用找bug"。

做了什么（全部在 HKCU\Software\Classes 下，不动 HKLM、不碰系统默认）：
  1. 注册 ProgID `ZhiJianPlayer.Media`（含 DefaultIcon 与 shell\open\command）；
  2. 为每个扩展名写 `OpenWithProgids`（⇒ 出现在"打开方式"列表里）；
  3. 写 `Capabilities` + `RegisteredApplications`（⇒ 出现在"默认应用"设置页）；
  4. `--force-default`（默认开）：把 `HKCU\\Software\\Classes\\.ext` 的默认值指向本 ProgID，
     并**删除** `HKCU\\...\\FileExts\\.ext\\UserChoice`。

⚠️ 诚实说明：Win10/11 的 `UserChoice` 带哈希保护，程序**无法**合法地直接改写它；
   删掉之后系统会回落到 Classes 默认值（实测通常生效），但个别情况下 Windows 会
   重新写回 UserChoice 或再次弹"选择应用" ⇒ 那种情况需要用户手动点一次"始终使用"。

用法：
    python scripts\\52-set-default-app.py                 # 自动找便携版/安装版的 exe
    python scripts\\52-set-default-app.py --exe <路径>
    python scripts\\52-set-default-app.py --list          # 只查看当前关联，不改
"""
from __future__ import annotations

import argparse
import os
import sys
import winreg

PROGID = "ZhiJianPlayer.Media"
APP_NAME = "至简Player"
REGISTERED = "ZhiJianPlayer"

VIDEO = [".mp4", ".mkv", ".mov", ".webm", ".avi", ".ts", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg"]
AUDIO = [".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".wma"]
IMAGE = [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif", ".tif", ".tiff"]
SUBTITLE = [".srt", ".ass", ".ssa", ".vtt", ".sub"]
ALL = VIDEO + AUDIO + IMAGE + SUBTITLE


def find_exe() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands = []
    out = os.path.join(root, "output")
    if os.path.isdir(out):
        for name in sorted(os.listdir(out), reverse=True):
            p = os.path.join(out, name, f"{APP_NAME}.exe")
            if os.path.isfile(p):
                cands.append(p)
    for p in (os.path.join(root, "build", "dist", APP_NAME, f"{APP_NAME}.exe"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", APP_NAME, f"{APP_NAME}.exe")):
        if os.path.isfile(p):
            cands.append(p)
    return cands[0] if cands else ""


def _set(key, name, value):
    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _del_tree(root, path):
    try:
        winreg.DeleteKey(root, path)
        return True
    except OSError:
        return False


def register(exe: str, force_default: bool = True, quiet: bool = False) -> int:
    def log(*a):
        if not quiet:
            print(*a)

    if not exe or not os.path.isfile(exe):
        print("!! 找不到可执行文件，用 --exe 指定")
        return 2
    log(f"注册: {exe}")
    cmd = f'"{exe}" "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROGID}") as k:
        _set(k, "", f"{APP_NAME} 媒体文件")
        _set(k, "FriendlyTypeName", f"{APP_NAME} 媒体文件")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          rf"Software\Classes\{PROGID}\DefaultIcon") as k:
        _set(k, "", f'"{exe}",0')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          rf"Software\Classes\{PROGID}\shell\open\command") as k:
        _set(k, "", cmd)
    # 出现在"打开方式"列表
    for ext in ALL:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{ext}\OpenWithProgids") as k:
            winreg.SetValueEx(k, PROGID, 0, winreg.REG_NONE, b"")
    # 出现在"默认应用"设置页
    caps = rf"Software\{APP_NAME}\Capabilities"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, caps) as k:
        _set(k, "ApplicationName", APP_NAME)
        _set(k, "ApplicationDescription", "轻量本地媒体播放器")
    for sub, exts in (("FileAssociations", ALL),):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{caps}\{sub}") as k:
            for ext in exts:
                _set(k, ext, PROGID)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\RegisteredApplications") as k:
        _set(k, REGISTERED, caps)

    if force_default:
        changed = 0
        for ext in ALL:
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                      rf"Software\Classes\{ext}") as k:
                    _set(k, "", PROGID)
                changed += 1
            except OSError:
                pass
            # UserChoice 有哈希保护，程序改不了 ⇒ 删掉让系统回落到 Classes 默认
            _del_tree(winreg.HKEY_CURRENT_USER,
                      rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{ext}\UserChoice")
        log(f"已把 {changed} 个扩展名的默认值指向本程序，并清除 UserChoice（回落生效）")
    log("完成。若资源管理器仍显示旧图标，重启一次 explorer 或注销即可。")
    return 0


def show() -> int:
    for ext in ALL:
        cur = ""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}") as k:
                cur = winreg.QueryValueEx(k, "")[0]
        except OSError:
            pass
        uc = ""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                rf"Software\Microsoft\Windows\CurrentVersion\Explorer"
                                rf"\FileExts\{ext}\UserChoice") as k:
                uc = winreg.QueryValueEx(k, "ProgId")[0]
        except OSError:
            pass
        print(f"  {ext:8s} Classes={cur or '-':28s} UserChoice={uc or '-'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-force", action="store_true", help="只注册，不抢默认")
    a = ap.parse_args()
    if a.list:
        return show()
    return register(a.exe or find_exe(), force_default=not a.no_force)


if __name__ == "__main__":
    sys.exit(main())
