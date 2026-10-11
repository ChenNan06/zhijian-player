"""EXIF 摘要读取（**不依赖 Pillow**）。

为什么自己写：项目是 PyInstaller 打包，为了几个标签引入 Pillow（几 MB + 一堆
DLL）不划算；而 EXIF 头部结构很简单（TIFF IFD），自己解析几十行就够。

只取"看图时有用"的字段：相机、镜头、拍摄时间、曝光、光圈、ISO、方向、像素尺寸。
解析失败一律返回 `{}`（**绝不抛异常** —— 坏文件不该让信息浮层崩掉）。
"""
from __future__ import annotations

import os

# 需要的 TIFF 标签
_TAGS = {
    0x010F: "make",         # 厂商
    0x0110: "model",        # 机型
    0x0132: "datetime",     # 拍摄时间
    0x0112: "orientation",  # 方向
    0x8769: "exif_ifd",     # EXIF 子目录
    0x829A: "exposure",     # 曝光时间（RATIONAL）
    0x829D: "fnumber",      # 光圈（RATIONAL）
    0x8827: "iso",          # ISO（SHORT）
    0x920A: "focal",        # 焦距（RATIONAL）
    0xA002: "px_w",
    0xA003: "px_h",
}
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _parse_tiff(buf: bytes) -> dict:
    if len(buf) < 8 or buf[:2] not in (b"II", b"MM"):
        return {}
    bo = "little" if buf[:2] == b"II" else "big"

    def u16(o: int) -> int:
        return int.from_bytes(buf[o:o + 2], bo)

    def u32(o: int) -> int:
        return int.from_bytes(buf[o:o + 4], bo)

    def value(typ: int, cnt: int, entry: int):
        size = _TYPE_SIZE.get(typ, 1) * cnt
        base = entry + 8 if size <= 4 else u32(entry + 8)
        if base < 0 or base + size > len(buf):
            return None
        if typ == 2:                                   # ASCII
            raw = buf[base:base + cnt]
            return raw.split(b"\x00")[0].decode("utf-8", "replace").strip()
        if typ == 3:
            return u16(base)
        if typ == 4:
            return u32(base)
        if typ == 5:                                   # RATIONAL
            num, den = u32(base), u32(base + 4)
            return (num / den) if den else None
        return None

    out: dict = {}

    def read_ifd(off: int) -> int:
        """读一个 IFD，返回下一个 IFD 的偏移（0 表示没有）。"""
        if off <= 0 or off + 2 > len(buf):
            return 0
        n = u16(off)
        nxt = u32(off + 2 + n * 12) if off + 2 + n * 12 + 4 <= len(buf) else 0
        for k in range(n):
            e = off + 2 + k * 12
            if e + 12 > len(buf):
                break
            tag, typ, cnt = u16(e), u16(e + 2), u32(e + 4)
            key = _TAGS.get(tag)
            if not key:
                continue
            v = value(typ, cnt, e)
            if v not in (None, ""):
                out[key] = v
        return nxt

    ifd0 = u32(4)
    read_ifd(ifd0)
    sub = out.pop("exif_ifd", 0)
    if isinstance(sub, int) and sub:
        read_ifd(sub)
    return out


def read_exif(path: str) -> dict:
    """读图片的 EXIF 摘要；没有 EXIF 或解析失败返回 `{}`。"""
    try:
        if not path or not os.path.isfile(path):
            return {}
        with open(path, "rb") as fh:
            data = fh.read(256 * 1024)          # EXIF 一定在文件头部
    except OSError:
        return {}
    try:
        if data[:2] == b"\xff\xd8":             # JPEG：找 APP1/Exif
            i = 2
            while i + 4 <= len(data):
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                size = int.from_bytes(data[i + 2:i + 4], "big")
                if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
                    return _parse_tiff(data[i + 10:i + 2 + size])
                if marker in (0xDA, 0xD9):      # 到图像数据就停
                    break
                i += 2 + size
            return {}
        if data[:4] in (b"II*\x00", b"MM\x00*"):   # 纯 TIFF
            return _parse_tiff(data)
    except Exception:
        return {}
    return {}


def exif_lines(ex: dict) -> list:
    """把 EXIF 摘要整理成"标签 + 值"的行（给信息浮层用）。"""
    if not ex:
        return []
    rows = []
    cam = " ".join(x for x in (str(ex.get("make", "")).strip(),
                               str(ex.get("model", "")).strip()) if x)
    if cam:
        rows.append(("相机", cam))
    dt = str(ex.get("datetime", "")).strip()
    if dt:
        # "2024:05:01 12:30:45" -> "2024-05-01 12:30:45"
        rows.append(("拍摄时间", dt.replace(":", "-", 2)))
    exp = ex.get("exposure")
    fn = ex.get("fnumber")
    iso = ex.get("iso")
    focal = ex.get("focal")
    shot = []
    if exp:
        shot.append(("1/%.0f s" % (1 / exp)) if exp < 1 else ("%.1f s" % exp))
    if fn:
        shot.append("f/%.1f" % fn)
    if iso:
        shot.append("ISO %s" % iso)
    if focal:
        shot.append("%.0f mm" % focal)
    if shot:
        rows.append(("拍摄参数", "    ".join(shot)))
    return rows
