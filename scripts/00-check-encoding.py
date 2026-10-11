"""编码门禁：含中文的 `.ps1` / `.bat` / `.cmd` **必须**是 UTF-8 with BOM。

为什么需要它（本机真实事故，2026-10-10）：
    用编辑器/工具改了一次 `scripts\\50-build-portable.ps1`，**BOM 被丢掉**。
    Windows PowerShell 5.1 读 .ps1 时按**当前代码页**（本机 GBK）解码，
    于是所有中文变成乱码、字符串引号配对全乱 —— 报的是
    `Unexpected token`、`The string is missing the terminator`，
    看起来像"脚本被写坏了"，其实只是少了 3 个字节。

用法：
    python scripts\\00-check-encoding.py            # 检查，出错返回 1
    python scripts\\00-check-encoding.py --fix      # 顺便把缺的 BOM 补上（内容必须是合法 UTF-8）

建议在**每次改完脚本 / 每次打包前**跑一次（`99-run-all-acceptance.ps1` 之前跑最合适）。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
BOM = b"\xef\xbb\xbf"
NEED_BOM = (".ps1", ".bat", ".cmd")
SKIP_DIRS = {"build", "output", "downloads", "logs", "runtime", "assets", "__pycache__", ".git"}


def targets():
    for dirpath, dirnames, files in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in files:
            if fn.lower().endswith(NEED_BOM):
                yield os.path.join(dirpath, fn)


def main() -> int:
    fix = "--fix" in sys.argv
    bad, fixed, ok = [], [], 0
    for p in sorted(targets()):
        raw = open(p, "rb").read()
        has_bom = raw.startswith(BOM)
        non_ascii = sum(1 for b in raw if b > 127)
        rel = os.path.relpath(p, ROOT)
        if has_bom:
            ok += 1
            continue
        if non_ascii == 0:
            continue                       # 纯 ASCII 的 .bat 不需要 BOM
        text = raw.decode("utf-8", "replace")
        if "\ufffd" in text:
            bad.append((rel, "内容不是合法 UTF-8，无法自动修复"))
            continue
        if fix:
            open(p, "wb").write(BOM + raw)
            fixed.append(rel)
        else:
            bad.append((rel, "含 %d 个非 ASCII 字节却**没有 BOM**" % non_ascii))

    print("检查完成：带 BOM %d 个" % ok)
    for rel in fixed:
        print("  已补 BOM: %s" % rel)
    for rel, why in bad:
        print("  ✗ %s —— %s" % (rel, why))
    if bad:
        print("\n修法：`python scripts\\00-check-encoding.py --fix`（或让写入工具保留 BOM）")
        return 1
    print("全部合格（含中文的脚本都带 UTF-8 BOM）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
