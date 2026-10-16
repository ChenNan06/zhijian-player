"""至简Player 卸载程序（独立小体积 exe，随程序一起装进安装目录）。

为什么单独做一个而不是复用安装包：安装包是 118 MB 的单文件,
塞进安装目录会让安装体积凭空多出 118 MB；而卸载逻辑本身只有几 KB。
本程序**不依赖 Qt**（用 Win32 MessageBox 做确认）, 打包后约 8 MB。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time

APP_NAME = "至简Player"
APP_ID = "ZhiJianPlayer"

# ⚠️ 扩展名清单**不再在这里维护**：唯一定义在 src/zhijian/platform/win.py
#    （ASSOC_KINDS：音频/视频/图片/字幕各一个 ProgID）。旧版本这里抄了一份，
#    与安装包/脚本三处不同步（.ssa/.sub 等扩展名就这么漏掉的）。


def msgbox(text: str, title: str, flags: int = 0x00000004 | 0x00000020) -> int:
    """MB_YESNO | MB_ICONQUESTION"""
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except Exception:
        return 6  # IDYES





def main() -> int:
    silent = "--silent" in sys.argv
    install_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    if "--dir" in sys.argv:
        install_dir = sys.argv[sys.argv.index("--dir") + 1]

    if not silent:
        r = msgbox(f"确定要卸载 {APP_NAME} 吗？\n\n安装目录：\n{install_dir}\n\n"
                   "快捷方式、文件关联与配置都会一并清理；\n"
                   "你自己放进这个目录的媒体文件会随目录一起删除。",
                   f"卸载 {APP_NAME}")
        if r != 6:      # IDYES
            return 1

    # 1) 快捷方式
    import winreg

    try:
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
    shutil.rmtree(os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                               "Start Menu", "Programs", APP_NAME), ignore_errors=True)

    # 2) 文件关联：交给**唯一实现**（src/zhijian/platform/win.py）逐项还原 + 残留检查
    #    ⚠️ 旧版本在这里删掉整个 Classes\.<ext> 键 —— 那会把用户自己的
    #    PerceivedType / 备份值（实测 .mp3 下有 DaumLive64Backup.bak）一起删掉。
    try:
        from zhijian.platform import win as _win

        rep = _win.unregister_associations()
        residual = rep.get("residual") or []
        if residual:
            print("文件关联残留: %s" % residual)
    except Exception as exc:
        print("文件关联清理失败: %s" % exc)

    # 3) 控制面板登记
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}")
    except OSError:
        pass

    # 4) 用户配置
    shutil.rmtree(os.path.join(os.environ.get("APPDATA", ""), "ZhiJian", "ZhiJianPlayer"),
                  ignore_errors=True)

    # 5) 安装目录：本进程就在里面跑, 必须**先把自己挪到 TEMP** 再删。
    #    ⚠️ 试过"写一个 .cmd 延迟 rmdir"：.cmd 由 cmd.exe 按控制台代码页解码, 中文路径
    #       会乱码；改用 8.3 短路径也不行 —— 现代 Windows 在非系统卷上默认关闭 8.3 名称,
    #       GetShortPathNameW 会原样返回中文长路径。所以走"自我复制到 TEMP 再回来删"。
    me = os.path.abspath(sys.argv[0])
    temp = os.environ.get("TEMP") or os.path.expanduser("~")
    if "--stage2" not in sys.argv:
        try:
            tmp = os.path.join(temp, f"zhijian_unins_{os.getpid()}.exe")
            shutil.copy2(me, tmp)
            DETACHED_PROCESS = 0x00000008
            subprocess.Popen([tmp, "--dir", install_dir, "--silent", "--stage2"],
                             close_fds=True, creationflags=DETACHED_PROCESS)
        except Exception:
            import traceback

            try:
                with open(os.path.join(temp, "zhijian_unins_err.log"), "w",
                          encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            except Exception:
                pass
    else:
        # stage2：等原进程退出（它的 exe 不再被占用）再整目录删掉
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                probe = os.path.join(install_dir, f"{APP_NAME}.exe")
                if os.path.isfile(probe):
                    os.rename(probe, probe + ".tmp")
                    os.rename(probe + ".tmp", probe)
                else:
                    break
            except OSError:
                time.sleep(0.4)
                continue
            break
        time.sleep(0.5)
        for _ in range(10):
            shutil.rmtree(install_dir, ignore_errors=True)
            if not os.path.isdir(install_dir):
                break
            time.sleep(0.6)
        # 自删除（自己在 TEMP 里, 删不掉就留给系统清理）
        try:
            os.remove(me)
        except OSError:
            pass

    if not silent and "--stage2" not in sys.argv:
        msgbox(f"{APP_NAME} 已卸载。", f"卸载 {APP_NAME}", 0x00000040)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback

        try:
            with open(os.path.join(os.environ.get("TEMP", "."), "zhijian_unins_err.log"),
                      "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise SystemExit(3)
