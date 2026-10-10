"""媒体类型识别与路由。

拖入/打开任何文件或目录时, 由这里决定走哪条界面分支：
    VIDEO -> 完整播放与时间轴体系
    IMAGE -> 隐藏时间轴, 展示上一张/下一张、无级缩放、快捷旋转
    AUDIO -> 极简控制卡片 + 内嵌专辑封面
"""

from __future__ import annotations

import os

VIDEO_EXT = {
    ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".m2ts",
    ".mpg", ".mpeg", ".mpe", ".vob", ".rmvb", ".rm", ".3gp", ".3g2", ".asf", ".ogv",
    ".mxf", ".mts", ".f4v", ".divx", ".m2v", ".dat", ".qsv", ".hevc", ".265", ".av1",
    ".mpv", ".y4m", ".dv", ".trp", ".tp", ".rec", ".bik", ".smk", ".nsv", ".amv", ".ivf",
}
IMAGE_EXT = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".webp", ".avif", ".heic",
    ".heif", ".tif", ".tiff", ".ico", ".jxl", ".jp2", ".tga", ".dds", ".ppm", ".pgm",
    ".pbm", ".pnm", ".pcx", ".xbm", ".xpm", ".exr", ".hdr", ".qoi", ".svgz",
}
AUDIO_EXT = {
    ".mp3", ".flac", ".wav", ".aac", ".m4a", ".m4b", ".ogg", ".oga", ".opus", ".wma",
    ".ape", ".alac", ".aiff", ".aif", ".aifc", ".mka", ".ac3", ".eac3", ".dts", ".amr",
    ".ra", ".wv", ".tta", ".dsf", ".dff", ".mp2", ".mpga", ".caf", ".au", ".snd", ".spx",
}
SUBTITLE_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup", ".smi", ".lrc", ".ttml", ".dfxp"}
PLAYLIST_EXT = {".m3u", ".m3u8", ".pls", ".xspf", ".wpl"}

KIND_VIDEO = "video"
KIND_IMAGE = "image"
KIND_AUDIO = "audio"
KIND_SUBTITLE = "subtitle"
KIND_PLAYLIST = "playlist"
KIND_UNKNOWN = "unknown"

KIND_ORDER = (KIND_VIDEO, KIND_AUDIO, KIND_IMAGE)

SUPPORTED_OPEN = sorted(VIDEO_EXT | IMAGE_EXT | AUDIO_EXT)


def kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return KIND_VIDEO
    if ext in IMAGE_EXT:
        return KIND_IMAGE
    if ext in AUDIO_EXT:
        return KIND_AUDIO
    if ext in SUBTITLE_EXT:
        return KIND_SUBTITLE
    if ext in PLAYLIST_EXT:
        return KIND_PLAYLIST
    return KIND_UNKNOWN


def is_media(path: str) -> bool:
    return kind_of(path) in (KIND_VIDEO, KIND_IMAGE, KIND_AUDIO)


def kind_label(kind: str) -> str:
    return {
        KIND_VIDEO: "视频",
        KIND_IMAGE: "图片",
        KIND_AUDIO: "音频",
        KIND_SUBTITLE: "字幕",
        KIND_PLAYLIST: "播放列表",
    }.get(kind, "文件")


def file_filter_string() -> str:
    """给 QFileDialog 用的过滤器串。"""
    def pats(exts):
        return " ".join(f"*{e}" for e in sorted(exts))

    return ";;".join([
        f"所有支持的媒体 ({pats(VIDEO_EXT | IMAGE_EXT | AUDIO_EXT)})",
        f"视频文件 ({pats(VIDEO_EXT)})",
        f"音频文件 ({pats(AUDIO_EXT)})",
        f"图片文件 ({pats(IMAGE_EXT)})",
        "所有文件 (*)",
    ])


def dir_kind_filter(kind: str) -> set[str]:
    """浏览同目录时的"同类型"扩展名集合。"""
    if kind == KIND_VIDEO:
        return VIDEO_EXT
    if kind == KIND_AUDIO:
        return AUDIO_EXT
    if kind == KIND_IMAGE:
        return IMAGE_EXT
    return VIDEO_EXT | AUDIO_EXT | IMAGE_EXT
