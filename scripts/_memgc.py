r"""用 gc 对象计数定位泄漏：跑 N 轮前后对比 Python 对象分类计数。

RSS 只能告诉你"涨了"，这个能告诉你"谁在涨"。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from zhijian.ipc import ControlClient  # noqa: E402

MEDIA = os.path.join(ROOT, "assets", "testmedia")
VIDEO = os.path.join(MEDIA, "clip_1080p.mp4")
IMAGE = os.path.join(MEDIA, "photo_4k.jpg")
AUDIO = os.path.join(MEDIA, "music_cover.mp3")


def main():
    app = QApplication(sys.argv[:1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([sys.executable, "-m", "zhijian", "--control", "zhjgc",
                          "--no-single-instance", VIDEO],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    ctl = ControlClient("zhjgc", timeout=60.0)
    for _ in range(200):
        try:
            ctl.call(cmd="ping")
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(4)

    def snap():
        r = ctl.call(cmd="gc")["result"]
        return r

    try:
        # 预热
        for _ in range(5):
            ctl.call(cmd="open", path=VIDEO); time.sleep(0.3)
            ctl.call(cmd="open", path=IMAGE); time.sleep(0.3)
            ctl.call(cmd="open", path=AUDIO); time.sleep(0.3)
        a = snap()
        print(f"预热后: 对象 {a['objects']}  线程 {a['threads']}")
        for _ in range(15):
            ctl.call(cmd="open", path=VIDEO); time.sleep(0.3)
            ctl.call(cmd="open", path=IMAGE); time.sleep(0.3)
            ctl.call(cmd="open", path=AUDIO); time.sleep(0.3)
        b = snap()
        print(f"15 轮后: 对象 {b['objects']}  线程 {b['threads']}")
        da = dict(a["top"]); db = dict(b["top"])
        keys = set(da) | set(db)
        rows = sorted(((k, db.get(k, 0) - da.get(k, 0), da.get(k, 0), db.get(k, 0))
                       for k in keys), key=lambda r: -r[1])
        print(f"\n  {'类型':<28}{'增量':>8}{'前':>8}{'后':>8}")
        for k, d, x, y in rows[:18]:
            flag = "  ⚠️" if d > 20 else ""
            print(f"  {k:<28}{d:>+8}{x:>8}{y:>8}{flag}")
        print(f"\n  对象总数增量: {b['objects'] - a['objects']:+d}"
              f"（15 轮 × 3 次 open = 45 次）")
        with open(os.path.join(ROOT, "logs", "memory-gc.json"), "w", encoding="utf-8") as f:
            json.dump({"before": a, "after": b, "diff": rows}, f, ensure_ascii=False, indent=2)
    finally:
        try:
            ctl.call(cmd="quit")
        except Exception:
            pass
        time.sleep(1.5)
        try:
            p.terminate()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
