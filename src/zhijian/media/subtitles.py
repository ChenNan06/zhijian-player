"""外挂字幕的自动发现。

要求：支持自动挂载同目录下的同名 .srt / .ass / .vtt。
实际挂载由 mpv 的 `sub-auto=fuzzy` 完成（它对编码的容错最好），
这里的扫描用于**界面展示与手动切换**，两者配合：
  * mpv 负责"能挂上"；
  * 本模块负责"用户看得见、点得动"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .router import SUBTITLE_EXT

# 常见语言后缀（用于把 `影片.chs.srt` 归到 `影片.mp4` 名下）
LANG_TOKENS = {
    "zh": "中文", "chi": "中文", "chs": "简体", "cht": "繁体", "chn": "中文",
    "sc": "简体", "tc": "繁体", "cn": "中文", "zh-cn": "简体", "zh-tw": "繁体",
    "zh-hans": "简体", "zh-hant": "繁体", "gb": "简体", "big5": "繁体",
    "en": "英文", "eng": "英文", "us": "英文", "uk": "英文",
    "ja": "日文", "jp": "日文", "jpn": "日文",
    "ko": "韩文", "kor": "韩文",
    "fr": "法文", "fre": "法文", "fra": "法文",
    "de": "德文", "ger": "德文", "deu": "德文",
    "es": "西语", "spa": "西语", "ru": "俄语", "rus": "俄语",
    "chs&eng": "简英", "cht&eng": "繁英", "zh&eng": "中英", "双语": "双语",
    "default": "默认", "forced": "强制", "sdh": "听障",
}

_SEPS = (".", "_", "-", " ", "[")


@dataclass
class SubTrack:
    path: str
    label: str
    lang: str = ""
    fmt: str = ""
    external: bool = True

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def _describe(stem: str, base_stem: str) -> tuple[str, str]:
    """从去掉主名后的尾巴里猜语言/说明。"""
    tail = stem[len(base_stem):].strip("".join(_SEPS))
    if not tail:
        return os.path.basename(stem), ""
    key = tail.lower()
    lang = LANG_TOKENS.get(key, "")
    label = lang or tail
    return label, lang


def find_subtitles(media_path: str, fuzzy: bool = True) -> list[SubTrack]:
    """返回与 media_path 同名（或带语言后缀）的外挂字幕列表。"""
    if not media_path:
        return []
    directory = os.path.dirname(os.path.abspath(media_path))
    if not os.path.isdir(directory):
        return []
    base_stem = os.path.splitext(os.path.basename(media_path))[0]
    low = base_stem.lower()
    out: list[SubTrack] = []
    try:
        with os.scandir(directory) as it:
            for e in it:
                try:
                    if not e.is_file():
                        continue
                except OSError:
                    continue
                stem, ext = os.path.splitext(e.name)
                if ext.lower() not in SUBTITLE_EXT:
                    continue
                if ext.lower() in (".idx", ".sup", ".sub"):
                    # 图形字幕一般与 .idx/.sub 成对, 只留 .idx/.sup 作为入口
                    if ext.lower() == ".sub":
                        continue
                s = stem.lower()
                if s == low:
                    label, lang = "默认字幕", ""
                elif fuzzy and s.startswith(low) and len(s) > len(low):
                    label, lang = _describe(stem, base_stem)
                else:
                    continue
                out.append(
                    SubTrack(
                        path=e.path,
                        label=f"{label} · {ext.lstrip('.').upper()}",
                        lang=lang,
                        fmt=ext.lstrip(".").lower(),
                        external=True,
                    )
                )
    except OSError:
        return []
    # 默认字幕排最前, 其余按名称自然序
    out.sort(key=lambda t: (0 if t.label.startswith("默认") else 1, t.name.lower()))
    return out


def detect_encoding(path: str) -> str:
    """探测字幕文本编码（只读前 256KB）。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(256 * 1024)
    except OSError:
        return "utf-8"
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in ("gb18030", "big5", "shift_jis", "cp1252"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"
