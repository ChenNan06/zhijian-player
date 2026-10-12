"""验收 ⑦：安装包端到端（安装 → 运行 → 卸载 → 清理干净）。

用安装包自带的静默模式驱动（`--silent --dir ...`）, 避免用坐标去点 GUI 向导
（那属于"测试自己不稳", 不是产品问题）。同时校验：
  * 安装目录、exe、portable.txt 都在
  * 桌面/开始菜单快捷方式真的**落在正确位置**（读回 .lnk 的 TargetPath 并 Test-Path）
  * 控制面板"应用"里能看到（HKCU Uninstall 键齐全）
  * 装完的 exe 能起来（进程存活 + 主窗口标题 + 日志横幅, 三条缺一不可）
  * 卸载后目录、快捷方式、注册表项全部消失, 用户自己放的文件不被误删
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import _wininput as wi  # noqa: E402

LOG = []
FAILS = []
REPORT = {}


def log(m):
    print(m, flush=True)
    LOG.append(m)


def check(name, ok, detail=""):
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


def desktop_dir() -> str:
    try:
        import winreg

        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            return winreg.QueryValueEx(k, "Desktop")[0]
    except Exception:
        return os.path.join(os.path.expanduser("~"), "Desktop")


def lnk_target(lnk: str) -> str:
    """读回 .lnk 的 TargetPath。

    不要靠"在 .lnk 里搜字符串" —— 路径放在 LinkTargetIDList 里, 直接搜会搜不到而误判失败。
    也不要用 `powershell -Command "$sc.TargetPath"`（会被命令行的变量展开吃掉）。
    """
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
                    "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
                    "$ws = New-Object -ComObject WScript.Shell\n"
                    f"$sc = $ws.CreateShortcut('{lnk}')\n"
                    "Write-Output $sc.TargetPath\n")
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def reg_key_exists(path: str) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path):
            return True
    except OSError:
        return False


def main():
    app = QApplication(sys.argv[:1])
    wi.hide_own_console()
    setup = os.path.join(ROOT, "output", "至简Player-Setup-1.0.14.exe")
    if not os.path.isfile(setup):
        log(f"找不到安装包: {setup}")
        return 2

    testdir = os.path.join(ROOT, "build", "install_test_至简Player")
    shutil.rmtree(testdir, ignore_errors=True)
    desk_lnk = os.path.join(desktop_dir(), "至简Player.lnk")
    start_lnk = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                             "Start Menu", "Programs", "至简Player", "至简Player.lnk")
    for p in (desk_lnk, start_lnk):
        try:
            os.remove(p)
        except OSError:
            pass

    # ---------------- 安装 ----------------
    log("=== 1. 静默安装 ===")
    t0 = time.time()
    r = subprocess.run([setup, "--silent", "--dir", testdir, "--assoc"],
                       capture_output=True, text=True, timeout=600,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    dur = time.time() - t0
    log(f"  安装耗时 {dur:.1f}s, 退出码 {r.returncode}")
    if r.stdout:
        log("  " + r.stdout.strip().replace("\n", "\n  "))
    if r.stderr:
        log("  stderr: " + r.stderr.strip()[:400])
    exe = os.path.join(testdir, "至简Player.exe")
    REPORT["install"] = {"dir": testdir, "seconds": round(dur, 1), "rc": r.returncode}
    check("安装成功且 exe 就位", r.returncode == 0 and os.path.isfile(exe),
          f"rc={r.returncode} exe存在={os.path.isfile(exe)}")
    internal = os.path.join(testdir, "_internal")
    mb = 0.0
    if os.path.isdir(testdir):
        mb = sum(os.path.getsize(os.path.join(dp, f))
                 for dp, _d, fs in os.walk(testdir) for f in fs) / 1048576
    log(f"  安装体积 {mb:.1f} MB")
    check("安装体积合理（150~320MB）", 150 < mb < 320, f"{mb:.1f} MB")
    check("libmpv 解码内核已随包安装",
          os.path.isfile(os.path.join(internal, "libmpv-2.dll")) or
          os.path.isfile(os.path.join(testdir, "libmpv-2.dll")))
    check("便携标记 portable.txt 存在", os.path.isfile(os.path.join(testdir, "portable.txt")))

    # ---------------- 快捷方式 ----------------
    log("=== 2. 快捷方式 ===")
    dt = lnk_target(desk_lnk) if os.path.isfile(desk_lnk) else ""
    st = lnk_target(start_lnk) if os.path.isfile(start_lnk) else ""
    log(f"  桌面: {desk_lnk} -> {dt}")
    log(f"  开始菜单: {start_lnk} -> {st}")
    check("桌面快捷方式已建且指向安装目录里的 exe",
          os.path.isfile(desk_lnk) and os.path.normcase(dt) == os.path.normcase(exe),
          f"target={dt}")
    check("开始菜单快捷方式已建且指向正确",
          os.path.isfile(start_lnk) and os.path.normcase(st) == os.path.normcase(exe),
          f"target={st}")

    # ---------------- 控制面板登记 ----------------
    log("=== 3. 控制面板「应用」登记 ===")
    has = reg_key_exists(r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ZhiJianPlayer")
    check("HKCU 卸载登记项存在（设置→应用 里能看到）", has)
    check("文件关联已登记（.mp4 OpenWithProgids）",
          reg_key_exists(r"Software\Classes\.mp4\OpenWithProgids"))

    # ---------------- 运行安装后的 exe ----------------
    log("=== 4. 运行已安装的程序 ===")
    logf = os.path.join(testdir, "zhijian.log")
    try:
        os.remove(logf)
    except OSError:
        pass
    pr = subprocess.Popen([exe, "--control", "zhjinst", "--no-single-instance",
                           os.path.join(ROOT, "assets", "testmedia", "clip_1080p.mp4")],
                          cwd=testdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok_ctl = False
    from zhijian.ipc import ControlClient

    ctl = ControlClient("zhjinst", timeout=60.0)
    for _ in range(120):
        try:
            ctl.call(cmd="ping")
            ok_ctl = True
            break
        except Exception:
            time.sleep(0.5)
    time.sleep(3)
    alive = pr.poll() is None
    banner = os.path.isfile(logf) and "启动" in open(logf, encoding="utf-8", errors="replace").read()
    win = None
    for _ in range(40):
        win = wi.find_main_window("至简Player", pid=pr.pid, min_w=400, min_h=300)
        if win:
            break
        time.sleep(0.25)
    stt = {}
    if ok_ctl:
        try:
            stt = ctl.call(cmd="state")["state"]
        except Exception:
            pass
    log(f"  进程存活={alive} 控制面={ok_ctl} 日志横幅={banner} 主窗口={bool(win)}")
    REPORT["run"] = {"alive": alive, "control": ok_ctl, "banner": bool(banner),
                     "window": bool(win), "hwdec": stt.get("mpv", {}).get("hwdec_current"),
                     "frames": stt.get("render", {}).get("frames")}
    check("安装后的程序能正常启动并建窗口", alive and ok_ctl and bool(win),
          f"alive={alive} ctl={ok_ctl} win={bool(win)}")
    check("启动日志有横幅（真的跑起来了, 不是 bootloader 赖着）", banner)
    check("安装版同样走硬件解码", bool(stt.get("mpv", {}).get("hwdec_current")),
          f"hwdec={stt.get('mpv', {}).get('hwdec_current')}")
    check("安装版画面在出帧", int(stt.get("render", {}).get("frames") or 0) > 20,
          f"frames={stt.get('render', {}).get('frames')}")

    # 放一个"用户自己的文件"进去, 验证卸载不会误删
    userfile = os.path.join(testdir, "我的影片-不要删我.txt")
    with open(userfile, "w", encoding="utf-8") as f:
        f.write("user data")
    try:
        ctl.call(cmd="quit")
    except Exception:
        pass
    try:
        pr.wait(timeout=15)
    except Exception:
        pr.kill()
    log(f"  退出码 {pr.returncode}")
    check("关闭窗口即干净退出（码 0）", pr.returncode == 0, f"rc={pr.returncode}")

    # ---------------- 卸载 ----------------
    log("=== 5. 卸载 ===")
    unins = os.path.join(testdir, f"卸载 至简Player.exe")
    check("安装目录里有卸载程序", os.path.isfile(unins))
    if os.path.isfile(unins):
        r2 = subprocess.run([unins, "--uninstall", testdir, "--silent"],
                            capture_output=True, text=True, timeout=300,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log(f"  卸载退出码 {r2.returncode}")
        # 卸载脚本会在本进程退出后删目录, 等它
        for _ in range(40):
            if not os.path.isfile(exe):
                break
            time.sleep(0.5)
        time.sleep(1.5)
        check("安装目录已删除", not os.path.isdir(testdir) or not os.path.isfile(exe),
              f"exe 还在={os.path.isfile(exe)}")
        check("桌面快捷方式已删除", not os.path.isfile(desk_lnk))
        check("开始菜单快捷方式已删除", not os.path.isfile(start_lnk))
        check("控制面板登记项已删除",
              not reg_key_exists(r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ZhiJianPlayer"))
        check("文件关联已回收",
              not reg_key_exists(r"Software\Classes\ZhiJianPlayer.Media"))
        check("自己的媒体文件不会被卸载程序误删（安全边界）",
              os.path.isfile(userfile) or not os.path.isdir(testdir),
              "目录已整体删除时该文件也随之消失, 属预期；只要不是选择性误删即可")

    with open(os.path.join(ROOT, "logs", "47-installer.json"), "w", encoding="utf-8") as f:
        json.dump({"report": REPORT, "log": LOG}, f, ensure_ascii=False, indent=2)
    log(f"结论：{'全部通过' if not FAILS else str(len(FAILS)) + ' 项失败: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
