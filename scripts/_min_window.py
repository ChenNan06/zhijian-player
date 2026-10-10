"""最小复现：无边框窗口 + QOpenGLWidget 的创建问题排查。"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

MODE = sys.argv[1] if len(sys.argv) > 1 else "a"


class W(QWidget):
    def __init__(self):
        super().__init__()
        if "f" in MODE:
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        if "m" in MODE:
            self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.resize(900, 600)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"mode={MODE}"))
        if "g" in MODE:
            gl = QOpenGLWidget(self)
            if "n" in MODE:
                gl.setAttribute(Qt.WA_NativeWindow, True)
                gl.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
            lay.addWidget(gl, 1)
        print(f"mode={MODE} flags={self.windowFlags()}", flush=True)


def main():
    app = QApplication(sys.argv[:1])
    w = W()
    w.show()
    QTimer.singleShot(2500, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
