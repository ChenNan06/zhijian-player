r"""隔离实验：一个**什么都不画**的 QOpenGLWidget，paintGL 要多久？

如果它也要 ~26ms，那 26ms 就根本不是 mpv 的开销，而是"Qt + 这块机器的 GL 栈"
每帧的固定成本 —— 那样的话改原生子窗口能不能救、怎么救，结论完全不同。
"""

from __future__ import annotations

import os
import statistics
import sys
import time

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtGui import QOpenGLFunctions, QSurfaceFormat  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402
from PySide6.QtOpenGLWidgets import QOpenGLWidget  # noqa: E402

COSTS: list[float] = []
COMPOSITE: list[float] = []


class Bare(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.n = 0

    def initializeGL(self):
        ctx = self.context()
        f = ctx.format()
        print(f"  GL 上下文: samples={f.samples()} swapInterval={f.swapInterval()} "
              f"depth={f.depthBufferSize()} stencil={f.stencilBufferSize()}", flush=True)

    def paintGL(self):
        t0 = time.perf_counter()
        fn = QOpenGLFunctions(self.context())
        fn.glClearColor(0.1, 0.2, 0.3, 1.0)
        fn.glClear(0x00004000)
        COSTS.append((time.perf_counter() - t0) * 1000.0)
        self.n += 1


def main():
    app = QApplication(sys.argv[:1])
    # 三种表面格式各测一遍：默认 / swapInterval=0 / 无 stencil
    for tag, fmt in (
        ("默认 QSurfaceFormat", None),
        ("swapInterval=0", "nofsync"),
        ("swapInterval=0 + 无 stencil/depth", "lean"),
    ):
        COSTS.clear()
        if fmt is None:
            QSurfaceFormat.setDefaultFormat(QSurfaceFormat())
        else:
            f = QSurfaceFormat()
            f.setSwapInterval(0)
            if fmt == "lean":
                f.setStencilBufferSize(0)
                f.setDepthBufferSize(0)
            QSurfaceFormat.setDefaultFormat(f)
        w = QMainWindow()
        v = Bare(w)
        w.setCentralWidget(v)
        w.resize(1280, 760)
        w.show()
        for _ in range(40):
            app.processEvents()
            time.sleep(0.01)
        COSTS.clear()
        t0 = time.perf_counter()
        for _ in range(90):
            v.update()
            app.processEvents()
            time.sleep(0.033)
        dur = time.perf_counter() - t0
        if COSTS:
            print(f"  {tag:<32} paintGL 中位 {statistics.median(COSTS):6.2f}ms  "
                  f"最大 {max(COSTS):6.2f}ms  ({len(COSTS)} 帧 / {dur:.1f}s)", flush=True)
        else:
            print(f"  {tag:<32} 没采到帧", flush=True)
        w.close()
        v.deleteLater()
        app.processEvents()
        time.sleep(0.4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
