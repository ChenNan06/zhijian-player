"""应用入口：进程初始化、日志、单实例、生命周期闭环。"""

from __future__ import annotations

import argparse
import json
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


#: ★ task-48：外部打开行为设置（A=当前窗口切换 / B=新窗口并存播放）
EXTERNAL_OPEN_KEY = "ui.external_open_mode"
EXTERNAL_MODE_SWITCH = "switch"
EXTERNAL_MODE_NEW_WINDOW = "new_window"


def external_open_mode(cfg) -> str:
    """读取「从外部打开新文件时」的行为：switch（A，默认）/ new_window（B）。"""
    try:
        raw = str(cfg.get(EXTERNAL_OPEN_KEY, EXTERNAL_MODE_SWITCH) or "").strip().lower()
    except Exception:
        raw = EXTERNAL_MODE_SWITCH
    return EXTERNAL_MODE_NEW_WINDOW if raw == EXTERNAL_MODE_NEW_WINDOW else EXTERNAL_MODE_SWITCH


def instance_name() -> str:
    """单实例本地服务器名（= APP_ID）。

    ★ task-48：提供 **ZHJ_SINGLE_INSTANCE_NAME** 测试/自动化隔离钩子 —— 本机可能同时
    跑着用户已安装的旧版实例，自动化要验证"第二进程投递"必须用一个独立命名空间，
    否则会去干扰用户正在播放的那个实例。生产环境不设该变量时行为与原来逐位相同。
    """
    try:
        return os.environ.get("ZHJ_SINGLE_INSTANCE_NAME") or APP_ID
    except Exception:
        return APP_ID


def _abs_file_args(files) -> list:
    """命令行里的文件参数 → 绝对路径（保持顺序；不做存在性过滤，主实例会提示）。"""
    out = []
    for f in files or []:
        try:
            s = str(f).strip().strip('"')
            if s:
                out.append(os.path.abspath(s))
        except Exception:
            continue
    return out


def app_icon_path() -> str:
    """★ task-51：定位 assets/icon.ico（兼容源码运行 / PyInstaller _MEIPASS / 安装目录）。

    优先 win.assoc_icon_path（它已实现"目标 exe 目录 → _MEIPASS → 工程根"的完整顺序），
    再兜底 theme._assets_dir()，最后直接探 _MEIPASS。
    """
    try:
        p = win.assoc_icon_path("icon.ico")
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    except Exception:
        log.debug("win.assoc_icon_path 失败", exc_info=True)
    candidates = []
    try:
        from .ui import theme as _theme
        d = getattr(_theme, "_assets_dir", lambda: "")()
        if d:
            candidates.append(os.path.join(d, "icon.ico"))
    except Exception:
        pass
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(os.path.join(meipass, "assets", "icon.ico"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "assets", "icon.ico"))
    for p in candidates:
        try:
            p = os.path.abspath(p)
            if os.path.isfile(p):
                return p
        except Exception:
            continue
    return ""


def deliver_to_running(name: str, files, timeout_ms: int = 800) -> bool:
    """★ task-48：把「新进程要打开的文件」投递给已在运行的实例（JSON 一行）。

    返回 True = 已连上主实例并写完（主实例会在主线程立即切歌/置前）。
    ⚠️ 这里刻意**不创建 QApplication**：第二进程在单实例检查阶段就要退出，
       QLocalSocket 的 waitForConnected/waitForBytesWritten 不需要事件循环。
    """
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        try:
            sock.abort()
        except Exception:
            pass
        return False
    paths = _abs_file_args(files)
    req = {"cmd": "open", "paths": paths, "cwd": os.getcwd(),
           "pid": int(os.getpid()), "show": True}
    try:
        payload = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
        sock.write(payload)
        sock.flush()
        ok = bool(sock.waitForBytesWritten(timeout_ms))
    except Exception:
        ok = False
    try:
        sock.disconnectFromServer()
    except Exception:
        pass
    return ok


def try_single_instance(name: str) -> bool:
    """返回 True 表示本进程是新实例（拿到了锁）；False 表示已有实例在跑。"""
    global _SERVER
    sock = QLocalSocket()
    sock.connectToServer(name)
    if sock.waitForConnected(300):
        # 兼容旧协议：老版本只认 "SHOW"；新版本走 JSON（见 deliver_to_running）。
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


def _raise_main_window(window: MainWindow):
    """★ task-48：把主实例唤到前台（最小化先还原；不抢全屏状态）。"""
    try:
        if window.isMinimized():
            window.showNormal()
        window.raise_()
        window.activateWindow()
        win.force_foreground(window)
    except Exception:
        log.debug("唤出主窗口失败", exc_info=True)


def _handle_ipc(conn, window: MainWindow):
    """单实例 IPC 收件口（运行在**主线程**，可直接操作窗口）。"""
    data = bytes(conn.readAll()).decode("utf-8", "replace").strip()
    if not data:
        return
    if data.startswith("{"):
        try:
            req = json.loads(data)
        except Exception:
            req = {}
        cmd = str(req.get("cmd") or "").strip().lower()
        paths = _abs_file_args(req.get("paths") or [])
        if cmd == "open" and paths:
            # ★ task-48：**主线程立即**切歌，再把窗口唤到前台。
            #   多文件/文件夹走 handle_dropped_paths（会重建同目录播放列表）；
            #   单文件走 open_path（与用户双击语义一致，保留同目录列表）。
            try:
                if len(paths) == 1 and os.path.isfile(paths[0]):
                    window.open_path(paths[0], autoplay=True)
                else:
                    window.handle_dropped_paths(paths)
                log.info("IPC 外部打开：%s", paths)
            except Exception:
                log.exception("IPC 打开文件失败：%s", paths)
            _raise_main_window(window)
            return
        # 其它 JSON（仅 show）落到下面统一置前
    # 兼容旧版 "SHOW" 与单纯重新启动（不新增窗口）
    _raise_main_window(window)


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

    # ---- 单实例 / 外部打开行为（★ task-48） ----
    # A 档（switch，默认）：已有实例 ⇒ 把 argv 里的文件绝对路径经 IPC 投递给主实例，
    #                          主实例在主线程立即 open_path + 置前，本进程退出；
    # B 档（new_window）：不抢单实例锁，直接起独立窗口（两窗并存播放）；
    # --no-single-instance / ui.single_instance=false：保持旧行为（可多开）。
    _ext_mode = external_open_mode(cfg)
    _inst = instance_name()
    _single_ok = bool(cfg.get("ui.single_instance", True)) and not args.no_single_instance
    _files_abs = _abs_file_args(args.files)
    if _single_ok and _ext_mode == EXTERNAL_MODE_NEW_WINDOW:
        log.info("外部打开行为=B（新窗口并存播放）：跳过单实例锁，启动独立窗口")
    elif _single_ok:
        if not try_single_instance(_inst):
            if deliver_to_running(_inst, _files_abs):
                log.info("已有实例在运行：已投递 %d 个文件 %s，本进程退出",
                         len(_files_abs), _files_abs)
                return 0
            log.warning("已有实例在运行但文件投递失败 ⇒ 本进程独立启动（避免丢文件）")

    # ---- ★ task-51：AppUserModelID（必须在 QApplication 创建之前）----
    if win.IS_WINDOWS:
        try:
            _aumid_ok = win.set_app_user_model_id(win.APP_USER_MODEL_ID)
            _aumid = win.get_app_user_model_id()
            log.info("AppUserModelID: set=%s 读回=%r", _aumid_ok, _aumid)
        except Exception:
            log.debug("设置 AppUserModelID 失败", exc_info=True)

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
    # ★ task-51：QApplication 级图标（显式绝对路径，兼容 _MEIPASS；WM_SETICON 是第三道保险）
    _icon_path = app_icon_path()
    if _icon_path:
        try:
            app.setWindowIcon(QIcon(_icon_path))
            log.info("应用图标: %s", _icon_path)
        except Exception:
            log.debug("app.setWindowIcon 失败", exc_info=True)
    else:
        log.warning("找不到 assets/icon.ico（任务栏图标将回落 exe 内嵌图标）")
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
    # ★ task-17：**隐藏彩蛋皮肤**（glass-light，预设里标了 glass_skin）是用户显式选过的
    #    第三套模式，不能被上面这条归一化吃掉 —— 否则"选了液态玻璃、重启就变回深色"。
    #    其余历史主题名（amber/sakura/aurora/mint/graphite）行为**逐位不变**。
    if (_saved_theme not in ("qq-light", "qq-dark")
            and not theme.THEMES.get(_saved_theme, {}).get("glass_skin")):
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

    # ---- ★ task-51：WM_SETICON 原生窗口图标注入（第三道保险）----
    if win.IS_WINDOWS and _icon_path:
        def _apply_window_icon(tag: str = "first") -> None:
            try:
                rep = win.set_window_icon(window, _icon_path)
                got = win.window_icon_handles(window)
                log.info("原生窗口图标[%s]: %s  WM_GETICON=%s", tag,
                         {k: rep.get(k) for k in ("ok", "hwnd", "big", "small", "icon_path")}, got)
            except Exception:
                log.debug("注入原生窗口图标失败[%s]", tag, exc_info=True)

        _apply_window_icon("show")
        # show()/首帧/主题切换后系统都可能重设图标，补两次幂等注入
        QTimer.singleShot(0, lambda: _apply_window_icon("deferred0"))
        QTimer.singleShot(300, lambda: _apply_window_icon("deferred300"))

    _install_ipc_handler(window, _inst)

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
        """退出收尾：**并发**执行各清理动作，总预算 2.0 秒（审计报告 T5）。

        ⚠️ 原来 `finalize()` 与 `shutdown_probe()` 是**串行**的，各自的超时
           （2.5s + 12s + join 若干）会叠加 ⇒ 最坏情况退出被拖到十几秒且无日志。
           现在：两条线并发跑，总预算硬性 2.0s；超预算就记日志并强制结束等待，
           保证进程绝不挂起、退出码始终为 0。
        """
        import threading
        import time as _time

        BUDGET = 2.0
        t0 = _time.perf_counter()
        log.info("shutdown: aboutToQuit 开始（总预算 %.1fs）", BUDGET)
        jobs = [("finalize", window.finalize), ("probe", shutdown_probe)]
        threads = []
        for name, fn in jobs:
            def run(nm=name, f=fn):
                ts = _time.perf_counter()
                try:
                    f()
                    log.info("shutdown: %s 完成 %.0fms", nm, (_time.perf_counter() - ts) * 1000)
                except Exception:
                    log.exception("shutdown: %s 失败", nm)
            th = threading.Thread(target=run, name="shutdown-" + name, daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            left = BUDGET - (_time.perf_counter() - t0)
            if left <= 0:
                break
            th.join(timeout=left)
        stuck = [th.name for th in threads if th.is_alive()]
        if stuck:
            # 超预算：**记录并放行**（daemon 线程不阻塞进程退出），保证不挂起
            log.warning("shutdown: 超过 %.1fs 仍未结束的收尾线程: %s（已放行，进程正常退出）",
                        BUDGET, ", ".join(stuck))
        else:
            log.info("shutdown: 全部收尾完成，用时 %.0fms", (_time.perf_counter() - t0) * 1000)
        try:
            win.clear_window_icons()          # ★ task-51：销毁我们注入的 HICON
        except Exception:
            log.debug("清理窗口图标句柄失败", exc_info=True)
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
