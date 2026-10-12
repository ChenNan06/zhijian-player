"""极简 EXIF 读取（只依赖标准库）—— 给「图片信息」面板用。

只做一件事：从 JPEG（APP1/Exif）里取出**最常看的几个字段**，
读不到就返回空字典（绝不抛异常、绝不阻塞）。

设计取舍：
  * 不做完整 EXIF 实现（不解析 MakerNote、不做 GPS、不做缩略图）；
  * 不做浮点格式化以外的转换 —— 格式化交给调用方；
  * 只读文件头部若干字节，长文件不会因此变慢。
"""
from __future__ import annotations

import struct

#: 需要的 TIFF 标签 → 友好名
_TAGS = {
    0x010F: "make",            # 制造商
    0x0110: "model",           # 型号
    0x0112: "orientation",
    0x0132: "datetime",        # 修改时间（相机写入）
    0x829A: "exposure",        # 曝光时间
    0x829D: "fnumber",         # 光圈
    0x8827: "iso",
    0x920A: "focal",           # 焦距
    0x9003: "datetime_orig",   # 拍摄时间（原始）
    0x9004: "datetime_digi",
    0xA002: "pix_w",
    0xA003: "pix_h",
    0xA405: "focal35",
    0x011A: "xres",
}
#: 需要进 Exif 子 IFD 才有的标签
_SUB_IFD = 0x8769
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _read_value(buf: bytes, endian: str, typ: int, count: int, off: int):
    size = _TYPE_SIZE.get(typ, 1) * max(1, count)
    if size > 4:
        if off + size > len(buf):
            return None
        raw = buf[off:off + size]
    else:
        raw = buf[off:off + size] if off + size <= len(buf) else b""
    try:
        if typ == 2:                                  # ASCII
            return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        if typ in (5, 10):                            # RATIONAL
            n = size // 8
            vals = []
            for i in range(n):
                a, b = struct.unpack(endian + "II", raw[i * 8:i * 8 + 8])
                vals.append(a / b if b else 0.0)
            return vals[0] if len(vals) == 1 else vals
        fmt = {1: "B", 3: "H", 4: "I", 7: "B", 9: "i"}.get(typ)
        if fmt is None:
            return None
        n = max(1, size // struct.calcsize(fmt))
        vals = list(struct.unpack(endian + fmt * n, raw[:n * struct.calcsize(fmt)]))
        return vals[0] if len(vals) == 1 else vals
    except Exception:
        return None


def _walk(buf: bytes, endian: str, base: int, out: dict, depth: int = 0):
    if depth > 2 or base + 2 > len(buf):
        return
    try:
        (count,) = struct.unpack(endian + "H", buf[base:base + 2])
    except Exception:
        return
    for i in range(min(count, 128)):
        e = base + 2 + i * 12
        if e + 12 > len(buf):
            return
        tag, typ, n = struct.unpack(endian + "HHI", buf[e:e + 8])
        val = _read_value(buf, endian, typ, n, e + 8)
        if tag == _SUB_IFD and isinstance(val, int):
            _walk(buf, endian, val, out, depth + 1)
            continue
        name = _TAGS.get(tag)
        if name and val not in (None, "", []):
            out.setdefault(name, val)


def read_exif(path: str, max_bytes: int = 512 * 1024) -> dict:
    """读取 JPEG 的 EXIF 摘要。任何异常都吞掉并返回 {}。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            if head[:2] != b"\xff\xd8":               # 不是 JPEG
                return {}
            data = head
            # 顺序扫描 APP 段，找 APP1/Exif
            while len(data) < max_bytes:
                chunk = f.read(4)
                if len(chunk) < 4:
                    return {}
                data += chunk
                if chunk[0] != 0xFF:
                    return {}
                marker, seglen = chunk[1], struct.unpack(">H", chunk[2:4])[0]
                body = f.read(max(0, seglen - 2))
                data += body
                if marker == 0xE1 and body[:6] == b"Exif\x00\x00":
                    tiff = body[6:]
                    if len(tiff) < 8:
                        return {}
                    endian = "<" if tiff[:2] == b"II" else ">"
                    out: dict = {}
                    _walk(tiff, endian, 8, out)
                    return out
                if marker in (0xDA, 0xD9):            # 进入压缩数据，结束
                    return {}
        return {}
    except Exception:
        return {}


def fmt_exposure(v) -> str:
    """曝光时间：1/250 s 这种读法比 0.004 直观。"""
    try:
        s = float(v)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    if s >= 1:
        return f"{s:g} s"
    return f"1/{round(1 / s)} s"


def fmt_fnumber(v) -> str:
    try:
        return f"f/{float(v):g}"
    except (TypeError, ValueError):
        return ""


def fmt_focal(v, v35=None) -> str:
    try:
        s = f"{float(v):g} mm"
    except (TypeError, ValueError):
        return ""
    try:
        if v35 and abs(float(v35) - float(v)) > 0.6:
            s += f"（等效 {float(v35):g} mm）"
    except (TypeError, ValueError):
        pass
    return s
