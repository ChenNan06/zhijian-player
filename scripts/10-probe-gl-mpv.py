"""探针：验证 libmpv + OpenGL 渲染管线在本机是否可用, 并选出最优硬件解码器。

用法（必须用真实窗口平台, 不要设 QT_QPA_PLATFORM=offscreen）:
    python scripts\\10-probe-gl-mpv.py [视频路径]

输出（同时写入 logs\\10-probe-gl-mpv.json）：
  * GL vendor / renderer / version / profile
  * mpv_render_context_create 是否成功
  * 逐个候选 hwdec 的 hwdec-current 与首帧耗时
  * 画面方向自检（用上白下黑的测试视频判断 FLIP_Y 是否正确）
"""

from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from zhijian.mpv import MpvEngine, MpvRenderWidget, libmpv_path, mpv_version_tuple  # noqa: E402
from zhijian.mpv.libmpv import MPV_FORMAT_DOUBLE, MPV_FORMAT_STRING  # noqa: E402

HWDEC_CANDIDATES = ["auto-safe", "d3d11va", "nvdec", "d3d11va-copy", "dxva2", "no"]

RESULTS = {"libmpv": libmpv_path(), "client_api": mpv_version_tuple(), "hwdec": [], "notes": []}


def log(msg: str):
    print(msg, flush=True)


class Probe(QWidget):
    def __init__(self, video: str):
        super().__init__()
        self.setWindowTitle("至简Player · 渲染管线探针")
        self.resize(1280, 760)
        self.video = video
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.engine = MpvEngine("probe-main")
        self.engine.start()
        self.view = MpvRenderWidget(self.engine)
        lay.addWidget(self.view)
        self.view.frame_rendered.connect(self._on_frame)
        self._first_frame_ts = None
        self._load_ts = None
        self._state = 0
        self._cand_idx = 0
        self._hwdec_current = None
        self._time_ready = False

    # ---- 状态机 -------------------------------------------------------------
    def start(self):
        QTimer.singleShot(600, self._step)

    def _on_frame(self):
        if self._load_ts and self._first_frame_ts is None:
            self._first_frame_ts = time.perf_counter()

    def _step(self):
        if self._state == 0:
            RESULTS["gl"] = self.view.gl_info
            log(f"GL vendor   : {RESULTS['gl'].get('vendor')}")
            log(f"GL renderer : {RESULTS['gl'].get('renderer')}")
            log(f"GL version  : {RESULTS['gl'].get('version')}")
            log(f"GL profile  : {RESULTS['gl'].get('profile')} (NoProfile/Core/Compat)")
            log(f"render ctx  : {'OK' if self.view.rc_ready else 'FAILED: ' + str(self.view.init_error)}")
            if not self.view.rc_ready:
                self._finish(1)
                return
            self._state = 1
            QTimer.singleShot(200, self._step)
        elif self._state == 1:
            self._try_next_hwdec()
        elif self._state == 2:
            self._collect_hwdec()
        elif self._state == 3:
            self._orientation_check()
        elif self._state == 4:
            self._finish(0)

    def _try_next_hwdec(self):
        if self._cand_idx >= len(HWDEC_CANDIDATES):
            self._state = 3
            QTimer.singleShot(100, self._step)
            return
        cand = HWDEC_CANDIDATES[self._cand_idx]
        log(f"--- 测试 hwdec={cand} ---")
        self.engine.set("hwdec", cand, MPV_FORMAT_STRING)
        self._first_frame_ts = None
        self._load_ts = time.perf_counter()
        self.engine.load(self.video)
        self.engine.set("pause", False)
        QTimer.singleShot(2500, lambda: self._collect_hwdec(cand))

    def _collect_hwdec(self, cand=None):
        cur = self.engine.get("hwdec-current", MPV_FORMAT_STRING, None)
        codec = self.engine.get("video-codec", MPV_FORMAT_STRING, None)
        w = self.engine.get("width", MPV_FORMAT_STRING, None)
        h = self.engine.get("height", MPV_FORMAT_STRING, None)
        vf = self.engine.get("video-format", MPV_FORMAT_STRING, None)
        first = None
        if self._first_frame_ts and self._load_ts:
            first = round((self._first_frame_ts - self._load_ts) * 1000, 1)
        rec = {
            "requested": cand or HWDEC_CANDIDATES[self._cand_idx - 1],
            "hwdec_current": cur,
            "codec": codec,
            "size": f"{w}x{h}" if w else None,
            "pixfmt": vf,
            "first_frame_ms": first,
            "frames": self.view.rendered_frames(),
        }
        RESULTS["hwdec"].append(rec)
        log(
            f"    请求={rec['requested']:<12} 实际={cur!s:<14} 解码={codec} 尺寸={rec['size']} "
            f"像素={vf} 首帧={first}ms"
        )
        if cand is not None:
            self._cand_idx += 1
            QTimer.singleShot(150, self._try_next_hwdec)
        else:
            self._state = 2
            QTimer.singleShot(50, self._step)

    def _orientation_check(self):
        """用上白下黑的测试片段判断 FLIP_Y 是否正确。"""
        orient = os.path.join(ROOT, "assets", "testmedia", "orient_white_top.mp4")
        if not os.path.isfile(orient):
            RESULTS["notes"].append("缺少 orient_white_top.mp4, 跳过方向自检")
            self._state = 4
            QTimer.singleShot(50, self._step)
            return
        self.engine.set("hwdec", "auto-safe", MPV_FORMAT_STRING)
        self.engine.load(orient)
        self.engine.set("pause", True, None)
        QTimer.singleShot(1500, self._grab_and_measure)

    def _grab_and_measure(self):
        dpr = self.view.devicePixelRatioF()
        img: QImage = self.view.grabFramebuffer()
        path = os.path.join(ROOT, "logs", "probe-orientation.png")
        img.save(path)
        w, h = img.width(), img.height()
        top = self._mean_lum(img, 0, 0, w, max(1, h // 5))
        bottom = self._mean_lum(img, 0, h - max(1, h // 5), w, max(1, h // 5))
        RESULTS["orientation"] = {
            "top_mean": round(top, 2),
            "bottom_mean": round(bottom, 2),
            "grab_size": [w, h],
            "dpr": dpr,
            "verdict": "正确（上白下黑）" if top > bottom + 40 else "上下颠倒！需要调整 FLIP_Y",
        }
        log(f"方向自检: top={top:.1f} bottom={bottom:.1f} -> {RESULTS['orientation']['verdict']}")
        self._state = 4
        QTimer.singleShot(50, self._step)

    @staticmethod
    def _mean_lum(img: QImage, x, y, w, h) -> float:
        sub = img.copy(x, y, w, h).convertToFormat(QImage.Format_Grayscale8)
        total = 0
        n = 0
        for yy in range(0, sub.height(), max(1, sub.height() // 40)):
            for xx in range(0, sub.width(), max(1, sub.width() // 40)):
                total += sub.pixelColor(xx, yy).red()
                n += 1
        return total / max(1, n)

    def _finish(self, code):
        RESULTS["render_widget_frames"] = self.view.rendered_frames()
        out = os.path.join(ROOT, "logs", "10-probe-gl-mpv.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(RESULTS, f, ensure_ascii=False, indent=2)
        log(f"结果已写入 {out}")
        # 退出流程：先释放渲染上下文, 再停 mpv
        try:
            self.view.release_resources()
            self.engine.begin_shutdown()
            self.engine.stop(1.0)
        except Exception as exc:  # pragma: no cover
            log(f"清理异常: {exc}")
        QApplication.exit(code)


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "assets", "testmedia", "clip_1080p.mp4")
    if not os.path.isfile(video):
        log(f"测试视频不存在: {video}")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("至简Player-Probe")
    w = Probe(video)
    w.show()
    w.raise_()
    w.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
