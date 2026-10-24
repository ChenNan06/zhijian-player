"""配置：JSON 持久化 + 默认值 + schema 迁移。

⚠️ 关键设计（来自上一代产品的教训）：用户配置文件是「用户值覆盖默认值」。
   只改 Python 里的默认值, 老用户机器上**永远不生效** —— 必须写迁移。
   每次新增/修改默认键, 都要在 MIGRATIONS 里登记。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
import tempfile
from typing import Any

log = logging.getLogger("zhijian.config")

CONFIG_VERSION = 8

DEFAULTS: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "playback": {
        # 长按右方向键 -> 倍速；单击 -> 快进。两种模式与数值都可在设置里改。
        #   multiply（默认）：目标 = 当前倍速 × long_press_multiplier
        #   fixed           ：目标 = long_press_tiers 里"第一个比当前倍速大的档"
        "long_press_mode": "multiply",
        "long_press_multiplier": 2.0,
        "long_press_tiers": "2, 5, 10",
        "long_press_speed": 2.0,     # 旧键（v5 起不再使用，仅迁移时用来推倍率）
        "long_press_threshold_ms": 300,
        "seek_step_small": 5,
        "seek_step_large": 30,
        "seek_step_shift": 15,       # Shift+方向键：一次跳 15 秒，按住连跳
        "volume_step": 5,
        "speed_step": 0.25,
        "speed_min": 0.25,
        # ⚠️ 4.0 是 v4 的旧默认；固定档位里的 5x/10x 会被它夹掉，所以 v5 抬到 16。
        #    老配置靠 _m_4_to_5 迁移，光改这里对老用户无效。
        "speed_max": 16.0,
        "auto_play": True,
        "remember_position": True,
        "hwdec": "auto-safe",      # task-106：**已真正接线下发**（app._mpv_options）
        "loop_mode": "sequence",     # sequence | loop | single | random
        "loop_all": True,
        "resume_min_secs": 15,       # 短于这个时长的文件不记忆进度
    },
    "subtitle": {
        "enabled": True,
        "auto_load": True,
        "font_family": "Microsoft YaHei UI",
        "font_size": 46,
        "bold": False,
        "italic": False,
        "text_color": "#FFFFFF",
        "border_color": "#101010",
        "border_size": 2.4,
        "shadow_offset": 1.2,
        "position": 92,              # 0(顶) ~ 100(底)
        "delay": 0.0,                # 秒, 支持 ±0.5 微调
        "scale_with_window": True,
        "ass_override": "scale",     # no | yes | scale | force
    },
    "ui": {
        "window_w": 1280,
        "window_h": 760,
        "window_x": None,
        "window_y": None,
        "maximized": False,
        "remember_window": True,
        "drawer_width": 392,
        "drawer_open": False,
        "drawer_edge_hover": True,
        "drawer_auto_hide": False,
        "capsule_autohide": True,
        "capsule_autohide_ms": 2800,
        "animations": True,
        "osd": True,
        "image_fit": "fit",          # fit | one | free
        "image_zoom_step": 1.12,
        "theme": "glass-dark",       # task-106：旧名 aurora/qq-dark/graphite/amber 自动映射到 glass-dark
        "preview_thumb": True,       # task-106 登记：文本预览缩略图（main_window:930/954 消费）
        "external_open_mode": "switch",   # task-106 登记：switch | new_window（app.external_open_mode 消费）
        "language": "zh_CN",
"confirm_exit": False,
        "playlist_view_mode": "list",   # ★ task-121：播放列表视图 list | grid
        "single_instance": True,
    },
    "audio": {
        "volume": 100.0,
        "mute": False,
        "device": "auto",
    },
    # ★ task-106：**此前漏登记**的运行时分组（都已有消费方，只是从没进 DEFAULTS）
    "lyric": {
        "font_family": "system",     # system | 具体族名（audio_view.LyricsPanel.apply_style）
        "font_px": 27,               # 16~28 基准字号
        "weight": "medium",          # regular | medium | heavy
        "step_pad": 18,              # 视觉行间距 LINE_GAP（task-27 起语义）
        "scroll_ms": 320,            # 200 | 320 | 500
        # ★ task-116 登记：译文字体族（设置面板 lyric.trans_font_family 写的就是它）。
        #   缺这条 ⇒ 重启后不重放值（"存了不生效"）。默认 follow = 自动挑楷/仿宋/思源宋/幼圆。
        "trans_font_family": "follow",
    },
    "spectrum": {
        "enabled": True,
        "peak_style": "none",        # none | falling
        "rhythm": "standard",        # audio_view.SPEC_RHYTHM_LEVELS
    },
    "music": {
        "cascade": False,            # task-55：音乐分区级联折叠
    },
    "image": {
        "minimap_position": "bottom_right",   # main_window:1085 消费
    },
    "recent": {
        "files": [],
        "max": 24,
    },
    "hotkeys": {
        # 每个功能支持**主键 + 副键**两个槽位（列表的第 0/1 项），都可在设置里改；
        # 组合键写 "Ctrl+Left" / "Shift+A" 这种形式（见 ui/_key_to_spec）。
        "play_pause": ["Space"],
        # 方向键的替身：WASD（用户要求"方向键还可以用 wasd 代替"）
        "seek_back_small": ["Left", "A"],
        "seek_fwd_small": ["Right", "D"],
        # ±15 秒（"加强版方向键"，按住连跳）。原来是硬编码在按键处理里的，
        # 现在变成**可自定义的普通快捷键**，这样用户能改、也能和别的键组合。
        "seek_back_shift": ["Shift+Left", "Shift+A"],
        "seek_fwd_shift": ["Shift+Right", "Shift+D"],
        # ⚠️ 这两条原来占着 Shift+左右，会跟上面的 ±15s 打架 —— 默认清空，想要自己绑。
        "seek_back_large": [],
        "seek_fwd_large": [],
        "volume_up": ["Up", "W"],
        "volume_down": ["Down", ""],   # task-106：**S 唯一留给截图**（空串占位=不绑定第二键）
        "mute": ["M"],
        "fullscreen": ["F", "Return"],
        "escape": ["Esc"],
        "open_file": ["Ctrl+O"],
        "open_folder": ["Ctrl+Shift+O"],
        "playlist": ["L"],
        "settings": ["Ctrl+,"],
        "subtitle_panel": ["V"],
        "screenshot": ["S"],
        "media_info": ["Tab"],           # PotPlayer 风格：左上角媒体信息浮层
        # Ctrl+上下 = 倍速（用户指定）；Ctrl+左右 = 上/下一个文件
        "speed_up": ["]", "Ctrl+Up"],
        "speed_down": ["[", "Ctrl+Down"],
        "speed_reset": ["Backspace"],
        "prev_file": ["PageUp", "Ctrl+Left"],
        "next_file": ["PageDown", "Ctrl+Right"],
        "stop": ["X"],
        "larger_ui": ["Ctrl+="],
        "smaller_ui": ["Ctrl+-"],
        "always_on_top": ["Ctrl+T"],
        "frame_back": [","],
        "frame_fwd": ["."],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# --------------------------------------------------------------------------------------
# 迁移：函数名 -> 从 version N 升到 N+1
# --------------------------------------------------------------------------------------

def _m_1_to_2(cfg: dict) -> dict:
    """v1 -> v2：长按倍速功能上线；补齐播放分组。"""
    pb = cfg.setdefault("playback", {})
    pb.setdefault("long_press_speed", 2.0)
    pb.setdefault("long_press_threshold_ms", 300)
    return cfg


def _m_2_to_3(cfg: dict) -> dict:
    """v2 -> v3：字幕微调步长 / 抽屉宽度 / 图片缩放步长。"""
    cfg.setdefault("subtitle", {}).setdefault("step", 0.1)
    cfg.setdefault("ui", {}).setdefault("drawer_width", 384)
    cfg.setdefault("ui", {}).setdefault("image_zoom_step", 1.12)
    return cfg


def _m_3_to_4(cfg: dict) -> dict:
    """v3 -> v4：抽屉宽度对齐逆向比例（269/866 = 31.1% → 1280 宽取 392）。

    ⚠️ 这正是"只改 DEFAULTS 里的值、老用户机器上永远不生效"的典型场景 ——
       必须在这里显式把旧的默认值 384 抬上去, 否则改的是一句空话。
    """
    ui = cfg.setdefault("ui", {})
    if int(ui.get("drawer_width", 0) or 0) == 384:
        ui["drawer_width"] = 392
    return cfg


def _m_4_to_5(cfg: dict) -> dict:
    """v4 -> v5：长按倍速分两种模式 + 档位/倍率可自定义；倍速上限 4.0 -> 16.0。

    ⚠️ 这是"只改 DEFAULTS 等于没改"的又一个实例：老配置里 `speed_max` 还是 4.0，
       固定档位里的 5x / 10x 会被夹回 4x。必须在这里显式抬 ——
       但只抬"仍是旧默认 4.0"的那种，用户自己改过的值不动。
    """
    pb = cfg.setdefault("playback", {})
    pb.setdefault("long_press_mode", "multiply")
    pb.setdefault("long_press_tiers", "2, 5, 10")
    # 旧键 long_press_speed 的语义是"1.0x 时长按到的倍速"，正好等于倍率
    try:
        old = float(pb.get("long_press_speed", 2.0) or 2.0)
    except (TypeError, ValueError):
        old = 2.0
    pb.setdefault("long_press_multiplier", max(1.1, old))
    try:
        cur_max = float(pb.get("speed_max", 4.0) or 4.0)
    except (TypeError, ValueError):
        cur_max = 4.0
    if abs(cur_max - 4.0) < 1e-6:
        pb["speed_max"] = 16.0
    return cfg


def _m_5_to_6(cfg: dict) -> dict:
    """v5 -> v6：快捷键支持主/副两槽 + 组合键；补 WASD / Ctrl 组合的默认绑定。

    ⚠️ 关键的一条：`seek_back_large` / `seek_fwd_large` 原来默认占着 `Shift+←/→`，
       而 v6 把 Shift+方向键定义成"±15 秒"。**只改 DEFAULTS 对老配置无效**，
       老配置里那两条还在，会跟新键抢 —— 所以这里显式把它们清空
       （只清"仍是旧默认"的那种，用户自己改过的不动）。
    """
    hk = cfg.setdefault("hotkeys", {})
    defaults = DEFAULTS["hotkeys"]

    def add(action: str, spec: str):
        cur = hk.get(action)
        if cur is None:
            hk[action] = list(defaults[action])
            return
        if spec and spec not in cur:
            cur.append(spec)

    # 旧默认占位先让出来
    for action in ("seek_back_large", "seek_fwd_large"):
        if hk.get(action) in (["Shift+Left"], ["Shift+Right"]):
            hk[action] = []
    # 新增的两条 ±15s
    hk.setdefault("seek_back_shift", list(defaults["seek_back_shift"]))
    hk.setdefault("seek_fwd_shift", list(defaults["seek_fwd_shift"]))
    # 副键默认值（追加，不覆盖用户已有绑定）
    add("seek_back_small", "A")
    add("seek_fwd_small", "D")
    add("volume_up", "W")
    add("volume_down", "S")
    add("prev_file", "Ctrl+Left")
    add("next_file", "Ctrl+Right")
    add("speed_up", "Ctrl+Up")
    add("speed_down", "Ctrl+Down")
    return cfg


def _m_6_to_7(cfg: dict) -> dict:
    """v6 -> v7：歌词行间距语义升级 —— 「网格步进附加(step_pad)」→「墨迹视觉行间距 LINE_GAP」，默认 18。

    ⚠️ 又一个「只改 DEFAULTS 等于没改」的实例：settings_panel 的默认表虽然已改成 18，
       但老用户 config 里存着**旧默认 40**，升级后会被当成「用户自定义值」保留 ⇒
       卡片要求的「默认 18」在真机不成立。所以必须在这里显式抬 ——
       **只抬「仍是旧默认 40」的那种，用户自己改过的值（如 28）原样不动**。
       键缺失时不在这里造键：缺失由 settings_panel.MUSIC_DEFAULTS 供默认 18（语义不变）。
    """
    lyric = cfg.get("lyric")
    if not isinstance(lyric, dict) or lyric.get("step_pad") is None:
        return cfg
    try:
        cur = float(lyric.get("step_pad"))
    except (TypeError, ValueError):
        return cfg
    if abs(cur - 40.0) < 1e-6:
        lyric["step_pad"] = 18
    return cfg


# --------------------------------------------------------------------------------------
# ★ task-106：旧主题名 → 新玻璃主题（读配置时**无感迁移**）
# --------------------------------------------------------------------------------------
LEGACY_THEME_MAP = {
    "qq-light": "glass-light", "mint": "glass-light", "sakura": "glass-light",
    "qq-dark": "glass-dark", "graphite": "glass-dark", "aurora": "glass-dark",
    "amber": "glass-dark",
}

#: 已删除的**死项**（曾经写进过 config.json，但没有任何消费方）
DEAD_KEYS = ("playback.cache_secs", "playback.image_duration", "subtitle.step",
             "image.wheel_invert")


def normalize_theme(name, available=None) -> str:
    """旧主题名 → 新名；available 给了就只在目标确实存在时迁移（防主题卡尚未合并）。"""
    t = str(name or "").strip()
    target = LEGACY_THEME_MAP.get(t)
    if not target:
        return t or "glass-dark"
    if available is not None and target not in available:
        return t
    return target


def _strip_key(data: dict, path: str) -> bool:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.get(part) if isinstance(cur, dict) else None
        if not isinstance(cur, dict):
            return False
    if parts[-1] in cur:
        cur.pop(parts[-1], None)
        return True
    return False


def normalize_config(data: dict) -> dict:
    """对所有已存在配置做一次无感规范化（幂等）：旧主题名 / 死键 / S 冲突。"""
    if not isinstance(data, dict):
        return data
    ui = data.get("ui")
    if isinstance(ui, dict) and "theme" in ui:
        ui["theme"] = normalize_theme(ui.get("theme"))
    for dead in DEAD_KEYS:
        _strip_key(data, dead)
    hk = data.get("hotkeys")
    if isinstance(hk, dict):
        vd = hk.get("volume_down")
        if isinstance(vd, (list, tuple)):
            hk["volume_down"] = [k for k in vd if str(k).strip().upper() != "S"] or ["Down"]
    return data


def _m_7_to_8(cfg: dict) -> dict:
    """v7 -> v8（task-106）：S 冲突 / 死键清理 / 旧主题名迁移 / 新分组键由 DEFAULTS 深合并补齐。"""
    return normalize_config(cfg)

MIGRATIONS = {1: _m_1_to_2, 2: _m_2_to_3, 3: _m_3_to_4, 4: _m_4_to_5, 5: _m_5_to_6,
              6: _m_6_to_7, 7: _m_7_to_8}


def parse_speed_tiers(text: Any) -> list:
    """把「2, 5, 10」这类文本解析成**升序去重的正数档位**；解析不出来就退回默认三档。

    用户要求"档位也要支持自定义"，所以输入是自由文本 —— 必须容错：
    中文逗号、空格、分号、重复值、乱输都要能活下来（乱输就用默认档，别抛异常）。
    """
    if isinstance(text, (list, tuple)):
        raw = [str(x) for x in text]
    else:
        raw = re.split(r"[,，;；\s]+", str(text or ""))
    out = []
    for tok in raw:
        tok = tok.strip().rstrip("xX倍")
        if not tok:
            continue
        try:
            v = float(tok)
        except ValueError:
            continue
        if v > 1.0 and all(abs(v - o) > 1e-6 for o in out):
            out.append(v)
    if not out:
        return [2.0, 5.0, 10.0]
    return sorted(out)


def _config_dir() -> str:
    # 便携模式：exe / 项目根目录下有 portable.txt 就把配置放旁边
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    if os.path.isfile(os.path.join(base, "portable.txt")):
        return base
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(appdata, "ZhiJian", "ZhiJianPlayer")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return base


class Config:
    """全局配置（字典语义 + 点号路径访问 + 自动落盘）。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(_config_dir(), "config.json")
        self.data: dict = copy.deepcopy(DEFAULTS)
        self._dirty = False
        self.load()

    # -- 读写 ----------------------------------------------------------------
    def load(self) -> None:
        if not os.path.isfile(self.path):
            self.save()
            return
        try:
            # ⚠️ utf-8-sig：记事本等编辑器会给 JSON 加 BOM，用 utf-8 读会直接抛
            #    "Unexpected UTF-8 BOM"，被当成"配置文件损坏"整个重置 —— 用户的设置全丢。
            with open(self.path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
        except Exception as exc:
            log.warning("配置文件损坏, 用默认值重建: %s", exc)
            self._backup_broken()
            raw = {}
        raw = self._migrate(raw)
        self.data = _deep_merge(DEFAULTS, raw)
        normalize_config(self.data)      # ★ task-106：旧主题名/死键/S 冲突 无感清理
        self.data["version"] = CONFIG_VERSION
        self._dirty = True

    def _backup_broken(self):
        try:
            if os.path.isfile(self.path):
                os.replace(self.path, self.path + ".broken")
        except OSError:
            pass

    @staticmethod
    def _migrate(raw: dict) -> dict:
        v = int(raw.get("version", 1) or 1)
        guard = 0
        while v < CONFIG_VERSION and guard < 32:
            fn = MIGRATIONS.get(v)
            if fn is None:
                break
            raw = fn(raw)
            v += 1
            raw["version"] = v
            guard += 1
        raw["version"] = CONFIG_VERSION
        return raw

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception as exc:
            log.warning("保存配置失败: %s", exc)

    def save_if_dirty(self) -> None:
        if self._dirty:
            self.save()

    # -- 访问 ----------------------------------------------------------------
    def get(self, path: str, default=None):
        cur: Any = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, path: str, value) -> None:
        parts = path.split(".")
        cur = self.data
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        if cur.get(parts[-1]) != value:
            cur[parts[-1]] = value
            self._dirty = True

    def reset_section(self, section: str) -> None:
        if section in DEFAULTS:
            self.data[section] = copy.deepcopy(DEFAULTS[section])
            self._dirty = True

    def reset_all(self) -> None:
        self.data = copy.deepcopy(DEFAULTS)
        self._dirty = True

    # -- 常用 ----------------------------------------------------------------
    def push_recent(self, path: str) -> None:
        files = list(self.get("recent.files", []) or [])
        p = os.path.abspath(path)
        files = [f for f in files if os.path.normcase(f) != os.path.normcase(p)]
        files.insert(0, p)
        self.set("recent.files", files[: int(self.get("recent.max", 24))])

    @staticmethod
    def leaf_keys(data: dict | None = None, prefix: str = "") -> list[str]:
        """把 DEFAULTS（或给的数据）摊平成点号叶子键列表（task-106 反射校验用）。"""
        out: list[str] = []
        d = DEFAULTS if data is None else data
        for k, v in d.items():
            if isinstance(v, dict):
                out.extend(Config.leaf_keys(v, prefix + k + "."))
            else:
                out.append(prefix + k)
        return out

    def positions_path(self) -> str:
        return os.path.join(os.path.dirname(self.path), "positions.json")

    def load_positions(self) -> dict:
        try:
            with open(self.positions_path(), "r", encoding="utf-8-sig") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def save_positions(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.positions_path()), exist_ok=True)
            with open(self.positions_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
