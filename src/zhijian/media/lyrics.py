"""歌词引擎：外挂 LRC + 音频内嵌歌词标签解析（v1.0.26）。

护栏（用户指定）：
  1. **编码探测优先级**：`utf-8-sig` → `gb18030` → `utf-16`
     （utf-8-sig 处理带 BOM 的 UTF-8；gb18030 比 gbk 覆盖更全，兼容繁体与生僻字）
  2. **时间轴**：支持一行多个时间标签（`[00:12.34][01:25.00]歌词`）
     以及毫秒 2 位 / 3 位（`.xx` 与 `.xxx`）
  3. 无同名 `.lrc` 时，从音频内嵌标签提取 `lyrics` / `USLT` 文本
     （优先 mutagen；不可用或未命中时回落 ffprobe）

对外接口：
    lines, source = load_lyrics(audio_path)
    lines 是 [(毫秒:int, 文本:str), ...]（已按时间排序、已剔除空行与元信息标签）
    source 是 "lrc" / "embedded" / "" （用于 UI 提示与排查）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

#: 编码探测顺序（用户护栏 ①）
ENCODINGS = ("utf-8-sig", "gb18030", "utf-16")

#: 时间标签：mm:ss.xx / mm:ss.xxx / mm:ss / [mm:ss:xx]（浪人格式）
_TAG_RE = re.compile(
    r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]"
)
#: 元信息标签（[ti:] [ar:] [al:] [by:] [offset:] …）—— 不当作歌词
_META_RE = re.compile(r"^\[(ti|ar|al|by|offset|re|ve|length|kana|language):", re.I)


def decode_bytes(raw: bytes) -> str:
    """按用户指定的优先级探测编码。"""
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 探测成功但解出一堆替换符/控制符 ⇒ 换下一个（gb18030 几乎不会抛错，必须*内容*校验）
        if text.count("\ufffd") <= max(0, len(text) // 200):
            return text
    return raw.decode("utf-8", errors="replace")


def parse_lrc(text: str) -> list[tuple[int, str]]:
    """解析 LRC 文本 ⇒ [(毫秒, 文本)]。

    支持一行多个时间标签（会在每个时间点各生成一行）与 2/3 位毫秒。
    """
    out: list[tuple[int, str]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip("\ufeff").rstrip()
        if not line or _META_RE.match(line):
            continue
        tags = list(_TAG_RE.finditer(line))
        body = _TAG_RE.sub("", line).strip()
        if not tags:
            # 没有时间轴的纯文本歌词：整行作为"无时间"内容（用 -1 标出）
            if body:
                out.append((-1, body))
            continue
        for m in tags:
            mm, ss, frac = int(m.group(1)), int(m.group(2)), m.group(3) or ""
            if len(frac) == 0:
                ms = 0
            elif len(frac) == 1:
                ms = int(frac) * 100
            elif len(frac) == 2:
                ms = int(frac) * 10
            else:
                ms = int(frac[:3])
            out.append((mm * 60000 + ss * 1000 + ms, body))
    # 有时间的排在前面按时间排序；无时间行（-1）原样附在后面
    timed = sorted((x for x in out if x[0] >= 0), key=lambda x: x[0])
    untimed = [x for x in out if x[0] < 0]
    return timed + untimed if not timed else timed


def load_lrc_file(path: str) -> list[tuple[int, str]]:
    try:
        with open(path, "rb") as fh:
            return parse_lrc(decode_bytes(fh.read()))
    except OSError:
        return []


# --------------------------------------------------------------------- 内嵌歌词
def _embedded_via_mutagen(path: str) -> str:
    """内嵌歌词：USLT（ID3v2）、LYRICS/UNSYNCEDLYRICS（Vorbis/FLAC）、©lyr（MP4）。"""
    try:
        from mutagen import File as MutaFile          # 应用已依赖 mutagen 读标签
    except Exception:
        return ""
    try:
        f = MutaFile(path)
    except Exception:
        return ""
    if f is None:
        return ""
    want = ("USLT", "LYRICS", "UNSYNCEDLYRICS", "UNSYNCED LYRICS", "©lyr", "LYRICS-XXX")
    try:
        for key in f.keys():
            k = str(key).upper()
            if any(k.startswith(w) or k == w for w in want):
                v = f[key]
                text = getattr(v, "text", None)
                if isinstance(text, (list, tuple)) and text:
                    return str(text[0])
                s = str(v)
                return s
    except Exception:
        pass
    # ID3 里 USLT 的 frameid 可能不同大小写，再按类型扫一遍
    try:
        for key in f.keys():
            if str(key).upper().startswith("USLT"):
                v = f[key]
                return str(getattr(v, "text", v))
    except Exception:
        pass
    return ""


def _embedded_via_ffprobe(path: str) -> str:
    """回落：ffprobe 读 format/stream 的歌词类标签（含 bundled ffprobe）。"""
    exe = os.environ.get("ZHIJIAN_FFPROBE") or "ffprobe"
    if getattr(sys, "frozen", False):
        cand = os.path.join(os.path.dirname(sys.executable), "ffprobe.exe")
        if os.path.isfile(cand):
            exe = cand
    try:
        proc = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            return ""
        data = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
    except Exception:
        return ""
    keys = ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS", "unsynced_lyrics")
    for node in [data.get("format", {})] + list(data.get("streams", []) or []):
        tags = (node or {}).get("tags") or {}
        for k, v in tags.items():
            if str(k).strip().lower() in [x.lower() for x in keys] and str(v).strip():
                return str(v)
    return ""


def load_embedded_lyrics(path: str) -> str:
    return _embedded_via_mutagen(path) or _embedded_via_ffprobe(path)


# --------------------------------------------------------------------- 统一入口
def find_lrc(audio_path: str) -> str:
    """同目录同名 .lrc（大小写不敏感）；找不到返回空串。"""
    d, base = os.path.dirname(audio_path), os.path.splitext(os.path.basename(audio_path))[0]
    try:
        names = os.listdir(d)
    except OSError:
        return ""
    low = base.lower()
    for n in names:
        if n.lower() == low + ".lrc":
            return os.path.join(d, n)
    return ""


def load_lyrics(audio_path: str) -> tuple[list[tuple[int, str]], str]:
    """返回 (行列表, 来源)。来源取值：lrc / embedded / "" 。"""
    if not audio_path:
        return [], ""
    p = find_lrc(audio_path)
    if p:
        lines = load_lrc_file(p)
        if lines:
            return lines, "lrc"
    text = load_embedded_lyrics(audio_path)
    if text.strip():
        lines = parse_lrc(text)
        if lines:
            return lines, "embedded"
    return [], ""
