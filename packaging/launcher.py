"""PyInstaller 打包入口（仅用于构建可执行文件）。

之所以单独放一个文件而不是直接打 `-m zhijian`：
PyInstaller 需要一个真实的脚本路径作为分析入口。
"""

from __future__ import annotations

import os
import sys

if getattr(sys, "frozen", False):
    # onedir 模式下, 让 PyInstaller 的解包目录优先级最高
    sys.path.insert(0, getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))

from zhijian.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
