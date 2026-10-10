"""目录扫描与自然排序。

* 方向键平滑浏览同目录下的所有同类型媒体 —— 顺序必须"像人一样"排
  （`第2集` 要排在 `第10集` 前面, 而不是字典序的 10 < 2）。
* 只读目录内容, 绝不修改/移动/删除用户的任何文件。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import router

_NUM_RE = re.compile(r"(\d+)")


def natural_key(text: str):
    """自然排序键：把连续数字当整数比较。"""
    parts = _NUM_RE.split(text.lower())
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append((0, int(part), ""))
        else:
            out.append((1, 0, part))
    return out


@dataclass
class MediaItem:
    path: str
    kind: str = ""
    duration: float = 0.0
    title: str = ""
    size: int = 0
    tags: dict = field(default_factory=dict)
    failed: bool = False

    def __post_init__(self):
        if not self.kind:
            self.kind = router.kind_of(self.path)
        if not self.title:
            self.title = os.path.basename(self.path)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def list_dir(directory: str, kind: str | None = None, recursive: bool = False,
             limit: int = 20000) -> list[MediaItem]:
    """列出目录里的媒体文件（只列, 不动）。"""
    out: list[MediaItem] = []
    if not directory or not os.path.isdir(directory):
        return out
    exts = router.dir_kind_filter(kind) if kind else (router.VIDEO_EXT | router.AUDIO_EXT | router.IMAGE_EXT)
    if recursive:
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith((".", "$"))]
            for fn in files:
                if os.path.splitext(fn)[1].lower() in exts:
                    out.append(MediaItem(os.path.join(root, fn)))
                    if len(out) >= limit:
                        break
            if len(out) >= limit:
                break
    else:
        try:
            with os.scandir(directory) as it:
                for e in it:
                    try:
                        if not e.is_file():
                            continue
                    except OSError:
                        continue
                    if os.path.splitext(e.name)[1].lower() in exts:
                        out.append(MediaItem(e.path))
                    if len(out) >= limit:
                        break
        except OSError:
            return out
    out.sort(key=lambda m: natural_key(m.name))
    return out


def siblings(path: str, kind: str | None = None) -> tuple[list[MediaItem], int]:
    """返回 (同目录同类型媒体列表, 当前文件下标)。"""
    if not path:
        return [], -1
    directory = os.path.dirname(os.path.abspath(path))
    k = kind or router.kind_of(path)
    items = list_dir(directory, k)
    target = os.path.normcase(os.path.abspath(path))
    for i, it in enumerate(items):
        if os.path.normcase(os.path.abspath(it.path)) == target:
            return items, i
    # 当前文件不在同类型列表里（例如扩展名不认识）→ 手工插到正确位置
    items.append(MediaItem(os.path.abspath(path), kind=k))
    items.sort(key=lambda m: natural_key(m.name))
    for i, it in enumerate(items):
        if os.path.normcase(os.path.abspath(it.path)) == target:
            return items, i
    return items, -1


def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"
