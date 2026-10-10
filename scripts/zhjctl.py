"""控制面命令行客户端：`python scripts\\zhjctl.py state` / `geom capsule` / `open <路径>` ...

用法：
    python scripts\\zhjctl.py [--name zhjtest] <cmd> [args...]
    python scripts\\zhjctl.py state
    python scripts\\zhjctl.py geom capsule
    python scripts\\zhjctl.py open "D:\\x.mp4"
    python scripts\\zhjctl.py ui drawer on
    python scripts\\zhjctl.py seek_perf --span 40 --n 12
    python scripts\\zhjctl.py eval "w.capsule.width()"
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from zhijian.ipc import ControlClient  # noqa: E402


def main():
    args = sys.argv[1:]
    name = "zhjtest"
    if args and args[0] == "--name":
        name = args[1]
        args = args[2:]
    if not args:
        print(__doc__)
        return 2
    app = QApplication(sys.argv[:1])
    c = ControlClient(name)
    cmd = args[0]
    req = {"cmd": cmd}
    if cmd == "geom":
        req["widget"] = args[1] if len(args) > 1 else "root"
    elif cmd == "open":
        req["path"] = args[1]
        req["play"] = "--pause" not in args
    elif cmd == "ui":
        req["what"] = args[1]
        if len(args) > 2:
            req["on"] = args[2].lower() in ("1", "on", "true", "yes")
        if "--animate" in args:
            req["animate"] = True
        if "--text" in args:
            req["text"] = args[args.index("--text") + 1]
    elif cmd == "cmd":
        req["argv"] = args[1:]
    elif cmd == "set":
        req["name"] = args[1]
        raw = args[2]
        try:
            req["value"] = json.loads(raw)
        except Exception:
            req["value"] = raw
    elif cmd == "get":
        req["name"] = args[1]
    elif cmd == "eval":
        req["expr"] = args[1]
    elif cmd == "image":
        req["action"] = args[1]
        for key in ("factor", "mode", "deg", "delta"):
            if f"--{key}" in args:
                req[key] = float(args[args.index(f"--{key}") + 1])
    elif cmd == "seek_perf":
        for key in ("span", "n", "timeout"):
            if f"--{key}" in args:
                req[key] = float(args[args.index(f"--{key}") + 1])
        if "--targets" in args:
            req["targets"] = [float(x) for x in args[args.index("--targets") + 1].split(",")]
    elif cmd == "grab":
        req["widget"] = args[1]
        req["path"] = args[2]
    resp = c.call(**req)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
