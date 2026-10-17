r"""把「至简Player」注册为媒体文件的默认打开方式（只写 HKCU，不需要管理员）。

⚠️ 真正的实现在 src/zhijian/platform/win.py（**唯一真源**），本脚本只是命令行外壳。
   安装包（packaging/installer.py）与卸载器（packaging/uninstaller.py）调的是**同一个**
   函数 —— 旧版本是三份各写各的实现（ProgID、引号、Capabilities 三处不一致），
   现网残留的坏命令（相对路径 + 正斜杠）就是这么来的。

做了什么（全部在 HKCU，不动 HKLM、不需要管理员）：
  1. 按类型注册**独立 ProgID**：ZhiJianPlayer.Audio / .Video / .Image / .Subtitle
     （含 DefaultIcon、FriendlyTypeName、InfoTip、Application 描述、
      shell open command = **严格双引号 + 绝对路径**）；
     DefaultIcon 指向 assets\ 下按类别生成的 audio/video/image/subtitle.ico
     （多尺寸 ICO，由 scripts\05-make-icon.py 生成；按目标 exe 目录解析绝对路径）；
  2. 每个扩展名写 OpenWithProgids（⇒ 出现在"打开方式"列表里）+ 扩展名级 DefaultIcon；
  3. 写 Capabilities + RegisteredApplications（⇒ 出现在「默认应用」设置页）；
  4. --force-default（默认开）：把 Classes\.<ext> 的默认值指向对应 ProgID，
     并把原值/原图标备份到 HKCU\Software\ZhiJianPlayer\AssocBackup
     （反注册可**逐项还原**）；同时尝试删除 FileExts\.<ext> 的
     UserChoice 与 UserChoiceLatest（删不掉会如实报告，见下）。

⚠️ 诚实说明：Win10/11 的 UserChoice（以及 Win11 22H2+ 的第二套 UserChoiceLatest）
   带**哈希 + ACL 保护**，程序**无法合法改写**它。删掉之后系统回落到 Classes 默认值；
   若被系统拒绝（返回 denied），则需要用户**手动点一次**「始终使用此应用」——
   那一次点击就是 Windows 唯一合法的正常途径，之后会被写入并长期记住。

用法：
    python scripts/52-set-default-app.py                 # 自动找便携版/安装版 exe 并注册
    python scripts/52-set-default-app.py --exe <路径>     # 指定 exe
    python scripts/52-set-default-app.py --status         # 只查看当前关联
    python scripts/52-set-default-app.py --dump --json    # 导出注册表原值（证据）
    python scripts/52-set-default-app.py --unregister     # 反注册（逐项还原 + 残留检查）
    python scripts/52-set-default-app.py --no-force       # 只登记，不抢默认
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from zhijian.platform import win as W  # noqa: E402

APP_NAME = W.ASSOC_APP_NAME


def find_exe() -> str:
    """按优先级找可执行文件；返回**绝对路径**（相对路径写进注册表就是坏的）。"""
    cands = []
    out = os.path.join(ROOT, "output")
    if os.path.isdir(out):
        for name in sorted(os.listdir(out), reverse=True):
            p = os.path.join(out, name, APP_NAME + ".exe")
            if os.path.isfile(p):
                cands.append(p)
    for p in (os.path.join(ROOT, "build", "dist", APP_NAME, APP_NAME + ".exe"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", APP_NAME,
                           APP_NAME + ".exe")):
        if os.path.isfile(p):
            cands.append(p)
    return os.path.abspath(cands[0]) if cands else ""


def _print_status(json_out: bool) -> int:
    st = W.association_status()
    if json_out:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0
    print("RegisteredApplications[%s] = %s" % (W.ASSOC_REGISTERED_NAME,
                                               st.get("registered_app") or "-"))
    for progid, info in st["progids"].items():
        print("  %-24s command=%s" % (progid, info.get("command") or "-"))
        print("  %-24s icon=%s" % ("", info.get("icon") or "-"))
    for ext, info in st["exts"].items():
        print("  %-8s Classes=%-24s OpenWith=%-5s UserChoice=%s"
              % (ext, info.get("default") or "-", info.get("in_openwith"),
                 info.get("userchoice") or "-"))
    return 0


def _print_dump(as_json: bool) -> int:
    d = W.dump_associations()
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    for progid, info in d["ProgIDs"].items():
        print("[%s]" % progid)
        for k in ("(默认)", "FriendlyTypeName", "InfoTip", "ApplicationName",
                  "DefaultIcon", "command", "exists"):
            print("    %-18s %s" % (k, info.get(k)))
    print("[RegisteredApplications] %s -> %s"
          % (W.ASSOC_REGISTERED_NAME, d["RegisteredApplications"].get(W.ASSOC_REGISTERED_NAME)))
    print("[Capabilities] %s" % json.dumps(
        {k: v for k, v in d["Capabilities"].items() if k != "FileAssociations"},
        ensure_ascii=False))
    print("[Capabilities/FileAssociations] %s" % json.dumps(
        d["Capabilities"].get("FileAssociations") or {}, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="")
    ap.add_argument("--list", "--status", dest="status", action="store_true",
                    help="只查看当前关联，不改")
    ap.add_argument("--dump", action="store_true", help="导出注册表原值（证据用）")
    ap.add_argument("--unregister", action="store_true", help="反注册（可回滚）")
    ap.add_argument("--no-force", action="store_true", help="只登记，不改默认值")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--quiet", action="store_true", help="只输出一行结论（打包脚本用）")
    a = ap.parse_args()
    if a.status:
        return _print_status(a.json)
    if a.dump:
        return _print_dump(a.json)
    if a.unregister:
        rep = W.unregister_associations()
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print("已反注册；残留项: %s" % (rep.get("residual") or "无"))
        return 0 if not rep.get("residual") else 1
    exe = a.exe or find_exe()
    if not exe:
        print("!! 找不到可执行文件，用 --exe 指定")
        return 2
    rep = W.register_associations(exe, with_default=not a.no_force)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    elif a.quiet:
        print("assoc: exe=%s progids=%d exts=%d errors=%d denied=%d"
              % (rep.get("exe"), len(rep.get("progids", {})), len(rep.get("exts", {})),
                 len(rep.get("errors", [])),
                 sum(1 for v in rep.get("exts", {}).values()
                     if v.get("userchoice") == "denied")))
    else:
        print("注册: %s" % rep.get("exe"))
        print("命令: %s" % rep.get("command"))
        print("图标: %s" % rep.get("icon"))
        print("ProgID: %s" % ", ".join(rep.get("progids", {})))
        print("扩展名: %d 个（OpenWithProgids + 默认值；默认值原值已备份到 %s）"
              % (len(rep.get("exts", {})), W.ASSOC_BACKUP))
        denied = [e for e, v in rep.get("exts", {}).items() if v.get("userchoice") == "denied"]
        deleted = [e for e, v in rep.get("exts", {}).items() if v.get("userchoice") == "deleted"]
        print("UserChoice: 已删除 %d 个 / 被系统拒绝 %d 个%s"
              % (len(deleted), len(denied),
                 ("（%s）" % ", ".join(denied[:6])) if denied else ""))
        # Win11 22H2+ 还有第二套 UserChoiceLatest（.srt/.ass 实测被它锁住）
        denied_latest = [e for e, v in rep.get("exts", {}).items()
                         if v.get("userchoice_latest") == "denied"]
        deleted_latest = [e for e, v in rep.get("exts", {}).items()
                          if v.get("userchoice_latest") == "deleted"]
        print("UserChoiceLatest(Win11): 已删除 %d 个 / 被系统拒绝 %d 个%s"
              % (len(deleted_latest), len(denied_latest),
                 ("（%s）" % ", ".join(denied_latest[:6])) if denied_latest else ""))
        if denied:
            print("  ⚠️ 被拒绝的扩展名需要用户**手动点一次「始终使用此应用」**"
                  "（Windows 的哈希保护只认这一次点击）")
        for err in rep.get("errors", []):
            print("  ! %s" % err)
    return 0 if not rep.get("errors") else 1


if __name__ == "__main__":
    sys.exit(main())
