"""应用入口：进程初始化、日志、单实例、生命周期闭环。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback

# ---- 必须在 QApplication 之前做 DPI 感知 ----
from .platform import win  # noqa: E402

if win.IS_WINDOWS:
    win.enable_dpi_awareness()

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPalette, QSurfaceFormat  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from . import APP_ID, APP_NAME, ORG_NAME, __version__  # noqa: E402
from .config import Config  # noqa: E402
from .mpv import MpvEngine, libmpv_path, mpv_version_tuple, shutdown_probe  # noqa: E402
from .mpv.libmpv import MPV_FORMAT_FLAG, MPV_FORMAT_STRING  # noqa: E402
from .ui import theme, widgets  # noqa: E402
from .ui.main_window import MainWindow  # noqa: E402

log = logging.getLogger("zhijian")


# --------------------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------------------

def setup_logging(log_dir: str, verbose: bool = False) -> str:
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "zhijian.log")
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 2 * 1024 * 1024:
            os.replace(path, path + ".1")
    except OSError:
        pass
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
                                      datefmt="%H:%M:%S"))
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(sh)
    return path


def install_excepthook():
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log.error("未捕获异常:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))

    sys.excepthook = hook


def install_qt_message_handler():
    """把 Qt / PySide6 自己打的消息也写进日志。

    ⚠️ PySide6 对 Python 覆写里抛出的异常（例如 `paint()`、`eventFilter()`）的处理是
    **打一行 qWarning 然后继续**。那一行默认只到 stderr, 打包后根本看不到 ——
    于是表现为"功能静默失效"。把它接进日志文件是唯一可靠的兜底。
    """
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        level_map = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }

        def handler(mode, context, message):
            lvl = level_map.get(mode, logging.INFO)
            where = ""
            try:
                if context is not None and context.file:
                    where = f" [{context.file}:{context.line}]"
            except Exception:
                pass
            logging.getLogger("qt").log(lvl, "%s%s", message, where)

        qInstallMessageHandler(handler)
    except Exception:
        log.debug("安装 Qt 消息处理器失败", exc_info=True)


# --------------------------------------------------------------------------------------
# 单实例
# --------------------------------------------------------------------------------------

_SERVER: QLocalServer | None = None


def try_single_instance(name: str) -> bool:
    """返回 True 表示本进程是新实例（拿到了锁）；False 表示已有实例在跑。"""
    global _SERVER
    sock = QLocalSocket()
    sock.connectToServer(name)
    if sock.waitForConnected(300):
        sock.write(b"SHOW\n")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return False
    QLocalServer.removeServer(name)
    _SERVER = QLocalServer()
    _SERVER.listen(name)
    return True


def _install_ipc_handler(window: MainWindow, name: str):
    if _SERVER is None:
        return

    def on_conn():
        c = _SERVER.nextPendingConnection()
        if c is None:
            return
        c.readyRead.connect(lambda: _handle_ipc(c, window))
        c.disconnected.connect(c.deleteLater)

    _SERVER.newConnection.connect(on_conn)


def _handle_ipc(conn, window: MainWindow):
    data = bytes(conn.readAll()).decode("utf-8", "replace").strip()
    if data.startswith("SHOW"):
        window.showNormal()
        window.raise_()
        win.force_foreground(window)


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zhijian", description=f"{APP_NAME} {__version__} —— 全能媒体查看器")
    p.add_argument("files", nargs="*", help="要打开的文件或文件夹")
    p.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    p.add_argument("--hwdec", default=None,
                   help="覆盖硬件解码方式：auto-safe / nvdec / d3d11va-copy / dxva2 / no")
    p.add_argument("--portable", action="store_true", help="强制便携模式（配置写在程序目录）")
    p.add_argument("--probe", action="store_true", help="只打印环境信息后退出（自检用）")
    p.add_argument("--no-single-instance", action="store_true", help="允许多开（自动化测试用）")
    p.add_argument("--control", default=None,
                   help="启动自动化控制服务器（QLocalServer 名 = 该值 + '-ctl'）")
    p.add_argument("--profile-startup", type=float, default=0.0, metavar="SEC",
                   help="诊断用：开启 cProfile, SEC 秒后把最耗时的调用栈 dump 到日志")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p


def print_probe(cfg: Config) -> int:
    print(f"{APP_NAME} {__version__}")
    print(f"libmpv           : {libmpv_path()}")
    try:
        print(f"client API       : {mpv_version_tuple()}")
    except Exception as exc:
        print(f"client API       : 读取失败 {exc}")
    print(f"配置文件         : {cfg.path}")
    print(f"Python           : {sys.version.split()[0]}")
    try:
        import PySide6

        print(f"PySide6          : {PySide6.__version__}")
    except Exception:
        pass
    print(f"DPI awareness    : {win.enable_dpi_awareness()}")
    exe = sys.executable if not getattr(sys, "frozen", False) else sys.executable
    print(f"GPU 首选项       : {win.get_gpu_preference(exe) or '（未设置，默认由系统决定）'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # 启动耗时打点：用户反馈"启动很慢", 所以把每个阶段都记进日志, 不靠感觉优化
    _t0 = time.perf_counter()

    def _mark(label: str) -> None:
        nonlocal _tprev
        now = time.perf_counter()
        log.info("  [启动] %-22s 累计%6.0f ms  本段%6.0f ms",
                 label, (now - _t0) * 1000, (now - _tprev) * 1000)
        _tprev = now

    _tprev = _t0

    args = build_argparser().parse_args(argv)
    cfg = Config()
    log_dir = os.path.dirname(cfg.path)
    log_path = setup_logging(log_dir, args.verbose)
    install_excepthook()
    log.info("=" * 78)
    log.info("%s %s 启动  (日志: %s)", APP_NAME, __version__, log_path)
    _mark("配置加载")

    if args.probe:
        return print_probe(cfg)

    if args.portable and not os.path.isfile(os.path.join(os.path.dirname(cfg.path), "portable.txt")):
        pass  # 便携模式在 Config 里按 portable.txt 判定；这里只做提示

    # ---- 单实例 ----
    if cfg.get("ui.single_instance", True) and not args.no_single_instance:
        if not try_single_instance(APP_ID):
            log.info("已有实例在运行, 已请求它置前, 本进程退出")
            return 0

    # ---- Qt 应用 ----
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationVersion(__version__)
    QApplication.setOrganizationName(ORG_NAME)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    # ⚠️⚠️ 必须**在 QApplication 之前**把默认 GL 表面格式设成 swapInterval=0。
    #    只给视频控件 `setFormat()` 是不够的 —— `QOpenGLWidget` 的最终上屏是由
    #    **顶层窗口的合成上下文**做的，那个上下文取的是"应用默认格式"。
    #    不设的话它按 vsync 走，mpv 的 `mpv_render_context_render` 会被驱动的
    #    命令队列反压顶住 —— 实测单帧 **26.28ms**（换缩放器、换软解都不变，
    #    说明不是算力问题而是等垂直同步），主线程被它占满，
    #    于是"任何与动画有关的东西都掉帧"。设完降到个位数毫秒。
    from .mpv.player import default_surface_format as _dsf

    QSurfaceFormat.setDefaultFormat(_dsf())
    app = QApplication(sys.argv[:1])
    # 内置界面字体（HarmonyOS Sans SC，免费商用，许可允许随包）—— **必须在 setFont /
    # build_stylesheet 之前**注册，否则 QSS 里写的族名找不到，会静默回退到系统字体
    # （用户报的"字体重合"就是这么来的）。
    _ui_font = theme.install_bundled_fonts()
    log.info("界面字体：%s", _ui_font or "系统默认（内置字体未加载，检查 assets\\fonts）")
    # ⚠️ 启动时必须**先把上次保存的主题套进调色板**，再生成样式表。
    #    否则 `ui.theme` 只是个躺在配置里的字符串：调色板还是默认深色，
    #    样式表、自绘控件、GL 清屏色全是深色 —— 用户选了浅色重启后又变回深色。
    _saved_theme = str(cfg.get("ui.theme", theme.DEFAULT_THEME) or theme.DEFAULT_THEME)
    # 历史配置里可能是 `amber` / `sakura` 之类已经不在界面上暴露的主题。
    # 归一成「浅色 / 深色」两个模式之一，避免"设置界面里两个按钮都没选中"
    # 以及残留的历史强调色（实测切到深色后冒出琥珀黄按钮）。
    if _saved_theme not in ("qq-light", "qq-dark"):
        _saved_theme = "qq-light" if _saved_theme == "frost" else "qq-dark"
    theme.apply_theme(_saved_theme)
    # 诊断开关：ZHIJIAN_SKIP_QSS=1 时不套全局样式表（用来定位"show() 很慢"到底慢在哪）
    if os.environ.get("ZHIJIAN_SKIP_QSS"):
        log.warning("已按 ZHIJIAN_SKIP_QSS 跳过全局样式表（仅诊断）")
    else:
        app.setStyleSheet(theme.build_stylesheet())
    app.setQuitOnLastWindowClosed(False)
    install_qt_message_handler()

    pal = app.palette()
    pal.setColor(QPalette.Window, theme.qt_color(theme.P["bg_base"]))
    pal.setColor(QPalette.Base, theme.qt_color(theme.P["bg_deep"]))
    pal.setColor(QPalette.Text, theme.qt_color(theme.P["text"]))
    pal.setColor(QPalette.WindowText, theme.qt_color(theme.P["text"]))
    pal.setColor(QPalette.ButtonText, theme.qt_color(theme.P["text"]))
    pal.setColor(QPalette.Highlight, theme.qt_color(theme.P["accent"]))
    pal.setColor(QPalette.HighlightedText, theme.qt_color(theme.P["text_on_accent"]))
    pal.setColor(QPalette.ToolTipBase, theme.qt_color(theme.P["glass_panel"]))
    pal.setColor(QPalette.ToolTipText, theme.qt_color(theme.P["text"]))
    app.setPalette(pal)
    f = QFont()
    f.setFamilies(theme.ui_font_stack())
    f.setPixelSize(13)
    app.setFont(f)
    _mark("Qt 应用与主题")

    # ---- mpv 引擎 ----
    options = {}
    if args.hwdec:
        options["hwdec"] = args.hwdec
    try:
        engine = MpvEngine("main", options=options or None)
    except Exception as exc:
        log.exception("创建 mpv 引擎失败")
        QMessageBox.critical(None, APP_NAME,
                             f"无法初始化播放引擎：\n{exc}\n\n请确认 libmpv-2.dll 存在且完整。")
        return 3
    engine.start()
    _mark("mpv 引擎初始化")

    # ---- 主窗口 ----
    window = MainWindow(cfg, engine)
    _mark("主窗口构建")
    # ⚠️ 启动即按**当前主题**把图标/玻璃面的取色重解析一遍。
    #    控件签名里的默认色是**模块导入时**求值的快照（那时调色板还是深色基准），
    #    不重解析的话"浅色模式启动"会一直用深色主题的浅灰图标色 ——
    #    用户反馈："默认浅色启动时大多数按钮图标和文字特别浅，深色切浅色反而正常"。
    #    （切换路径本来就会调用它，所以只有启动路径中招。）
    _n = widgets.refresh_glass_theme()
    log.info("启动重解析控件配色：%d 个", _n)
    _mark("重解析控件配色")
    # 提示条：Qt 原生 QToolTip 是**方窗口**，QSS 圆角只画在窗口里面，四角照样露方底
    # （用户截图："这不还有吗"）⇒ 全应用换成自绘圆角提示条。
    # 装在 QApplication 上，所有 setToolTip() 调用点都不用改。
    from .ui.widgets import TooltipFilter

    _tip_filter = TooltipFilter(app)
    app.installEventFilter(_tip_filter)
    _mark("提示条换成自绘圆角")
    window.engine.set("volume", float(cfg.get("audio.volume", 100.0)))
    window.engine.set("mute", bool(cfg.get("audio.mute", False)), MPV_FORMAT_FLAG)
    window.capsule.set_volume(float(cfg.get("audio.volume", 100.0)))
    window.capsule.set_mute(bool(cfg.get("audio.mute", False)))
    _mark("应用音量/静音")
    # 字幕默认样式
    _apply_subtitle_defaults(window, cfg)
    _mark("字幕默认样式")

    # ---- 启动剖析（诊断用）：必须在 show() 之前开, 因为耗时全在 show() 里面 ----
    _prof = None

    def _prof_dump():
        pass

    if args.profile_startup > 0:
        import cProfile
        import io
        import pstats

        _prof = cProfile.Profile()
        _prof.enable()

        def _prof_dump():
            _prof.disable()
            s = io.StringIO()
            pstats.Stats(_prof, stream=s).sort_stats("tottime").print_stats(30)
            log.info("启动剖析（tottime 前 30, 覆盖到 window.show() 为止）:\n%s", s.getvalue())

    if cfg.get("ui.maximized", False):
        window.showMaximized()
    else:
        window.show()
    _mark("窗口显示")
    _prof_dump()
    # 画面（GL 上下文）延后到窗口已经显示之后再建 —— 用户先看到界面, 不干等黑屏
    QTimer.singleShot(0, window.stage.ensure_video)
    if cfg.get("ui.drawer_open", False):
        QTimer.singleShot(120, lambda: window._on_playlist_button(True))

    _install_ipc_handler(window, APP_ID)

    # ---- 自动化控制面（仅当显式传入 --control 时开启） ----
    control = None
    control_name = args.control or (APP_ID if os.environ.get("ZHJ_CONTROL") else None)
    if control_name:
        from .ipc import ControlServer

        control = ControlServer(window, control_name)

    # ---- 打开命令行传入的文件 ----
    targets = [a for a in args.files if os.path.exists(a)]
    if targets:
        QTimer.singleShot(160, lambda: window.handle_dropped_paths(targets))

    # ---- 退出闭环 ----
    exit_code = {"v": 0}

    def on_about_to_quit():
        log.info("shutdown: aboutToQuit")
        try:
            window.finalize()
        except Exception:
            log.exception("finalize 失败")
        try:
            shutdown_probe()
        except Exception:
            log.exception("关闭探针失败")
        try:
            cfg.save()
            for h in logging.getLogger().handlers:
                try:
                    h.flush()
                except Exception:
                    pass
        except Exception:
            pass
        log.info("shutdown: 完成")

    app.aboutToQuit.connect(on_about_to_quit)
    window.shutting_down.connect(lambda: QTimer.singleShot(0, app.quit))

    rc = app.exec()
    log.info("%s 退出 rc=%s", APP_NAME, rc)
    return rc or exit_code["v"]


def _apply_subtitle_defaults(window: MainWindow, cfg: Config):
    """把字幕默认样式推给 mpv。

    以前这里一次要 1.3 秒 —— 原因不是 set 本身慢（实测 11 个属性只要 1ms），
    而是事件线程在 `mpv_wait_event()` 阻塞期间**一直握着句柄锁**，UI 线程每次 set
    都要等它超时（~110ms/次）。修在 libmpv.MpvHandle.wait_event 里，这里保持同步即可。
    """
    eng = window.engine
    # ⚠️ **字幕也要能用内置字体**：Qt 注册的字体 mpv 看不见（那是两套字体查找），
    #    必须把内置字体目录告诉 mpv（`sub-fonts-dir`）。这样"字幕字体"下拉里选
    #    HarmonyOS Sans SC 才真的生效，用户没装这个字体也能用。
    #    依赖 mpv 的 fontconfig 支持：设了不生效的话下面的探针能测出来（见 _probe_subfont.py）。
    _fd = theme.bundled_font_dir()
    if _fd:
        eng.set("sub-fonts-dir", _fd, MPV_FORMAT_STRING)
        log.info("字幕字体目录：%s", _fd)
    eng.set("sub-font", str(cfg.get("subtitle.font_family", "Microsoft YaHei UI")), MPV_FORMAT_STRING)
    eng.set("sub-font-size", int(cfg.get("subtitle.font_size", 46)))
    eng.set("sub-bold", bool(cfg.get("subtitle.bold", False)), MPV_FORMAT_FLAG)
    eng.set("sub-italic", bool(cfg.get("subtitle.italic", False)), MPV_FORMAT_FLAG)
    eng.set("sub-color", str(cfg.get("subtitle.text_color", "#FFFFFF")), MPV_FORMAT_STRING)
    eng.set("sub-border-color", str(cfg.get("subtitle.border_color", "#101010")), MPV_FORMAT_STRING)
    eng.set("sub-border-size", float(cfg.get("subtitle.border_size", 2.4)))
    eng.set("sub-shadow-offset", float(cfg.get("subtitle.shadow_offset", 1.2)))
    eng.set("sub-pos", int(cfg.get("subtitle.position", 92)))
    eng.set("sub-delay", float(cfg.get("subtitle.delay", 0.0)))
    eng.set("sub-ass-override", str(cfg.get("subtitle.ass_override", "scale")), MPV_FORMAT_STRING)
    eng.set("sub-visibility", bool(cfg.get("subtitle.enabled", True)), MPV_FORMAT_FLAG)


if __name__ == "__main__":
    raise SystemExit(main())
