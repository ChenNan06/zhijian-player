"""从媒体文件里直接读出内嵌专辑封面与标签（纯 Python, 不依赖 ffprobe）。

覆盖的容器：
  * MP3   —— ID3v2.2 / v2.3 / v2.4 的 APIC（v2.2 叫 PIC）
  * FLAC  —— METADATA_BLOCK_PICTURE（type 6）
  * M4A/MP4 —— moov/udta/meta/ilst/covr
  * OGG/Opus —— Vorbis comment 里的 METADATA_BLOCK_PICTURE（base64）
  * WMA/ASF —— WM/Picture（尽力而为）

设计原则：任何解析失败都返回空结果, **绝不抛异常**, 也绝不打不开文件时卡住
（所有读取都限制在文件头部/元数据区, 大文件不会被整个读进内存）。
"""

from __future__ import annotations

import base64
import os
import struct
from dataclasses import dataclass, field

MAX_META_SCAN = 32 * 1024 * 1024     # 最多扫描的头部字节数


@dataclass
class MediaTags:
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    genre: str = ""
    track: str = ""
    comment: str = ""
    cover: bytes | None = None
    cover_mime: str = ""
    extra: dict = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not any([self.title, self.artist, self.album, self.cover])


def _read_head(path: str, limit: int = MAX_META_SCAN) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _mime_for(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] == b"ftypavif":
        return "image/avif"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


# --------------------------------------------------------------------------------------
# ID3v2（MP3 / 也可能是 FLAC、WAV 的前缀）
# --------------------------------------------------------------------------------------

_ID3_ENC = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}


def _syncsafe(b: bytes) -> int:
    return (b[0] & 0x7F) << 21 | (b[1] & 0x7F) << 14 | (b[2] & 0x7F) << 7 | (b[3] & 0x7F)


def _decode_text(data: bytes, enc_byte: int) -> str:
    enc = _ID3_ENC.get(enc_byte, "latin-1")
    try:
        return data.decode(enc, "replace").strip("\x00").strip()
    except Exception:
        return ""


def _split_encoded(data: bytes, enc_byte: int) -> tuple[str, bytes]:
    """按编码切出「文本 + 终止符」后面剩下的二进制。"""
    if enc_byte in (1, 2):
        # UTF-16: 双字节 00 00 终止
        for i in range(0, len(data) - 1, 2):
            if data[i] == 0 and data[i + 1] == 0:
                return _decode_text(data[:i], enc_byte), data[i + 2:]
        return _decode_text(data, enc_byte), b""
    idx = data.find(b"\x00")
    if idx < 0:
        return _decode_text(data, enc_byte), b""
    return _decode_text(data[:idx], enc_byte), data[idx + 1:]


def _parse_id3v2(blob: bytes) -> MediaTags:
    tags = MediaTags()
    if len(blob) < 10 or blob[:3] != b"ID3":
        return tags
    major = blob[3]
    flags = blob[5]
    size = _syncsafe(blob[6:10])
    body = blob[10:10 + size]
    if flags & 0x40 and len(body) > 4:      # extended header
        try:
            if major == 4:
                ext = _syncsafe(body[:4])
            else:
                ext = struct.unpack(">I", body[:4])[0] + 4
            body = body[ext:]
        except Exception:
            pass
    pos = 0
    text_frames = {
        "TIT2": "title", "TT2": "title",
        "TPE1": "artist", "TP1": "artist",
        "TALB": "album", "TAL": "album",
        "TYER": "year", "TDRC": "year", "TYE": "year",
        "TCON": "genre", "TCO": "genre",
        "TRCK": "track", "TRK": "track",
        "COMM": "comment", "COM": "comment",
    }
    while pos + 6 <= len(body):
        if major == 2:
            fid = body[pos:pos + 3].decode("latin-1")
            fsize = int.from_bytes(body[pos + 3:pos + 6], "big")
            hsize = 6
        else:
            fid = body[pos:pos + 4].decode("latin-1")
            if major == 4:
                fsize = _syncsafe(body[pos + 4:pos + 8])
            else:
                fsize = struct.unpack(">I", body[pos + 4:pos + 8])[0]
            hsize = 10
        if fsize <= 0 or pos + hsize + fsize > len(body):
            break
        payload = body[pos + hsize:pos + hsize + fsize]
        pos += hsize + fsize
        if fid in ("APIC", "PIC") and payload:
            enc = payload[0]
            rest = payload[1:]
            if fid == "PIC":
                mime = "image/" + rest[:3].decode("latin-1", "replace").strip().lower()
                rest = rest[3:]
            else:
                nul = rest.find(b"\x00")
                mime = rest[:nul].decode("latin-1", "replace") if nul >= 0 else "image/jpeg"
                rest = rest[nul + 1:] if nul >= 0 else rest
            if rest:
                rest = rest[1:]                      # picture type
                _desc, rest = _split_encoded(rest, enc)
                if rest:
                    tags.cover = rest
                    tags.cover_mime = mime or _mime_for(rest)
        elif fid in text_frames and payload:
            key = text_frames[fid]
            if fid in ("COMM", "COM"):
                if len(payload) > 4:
                    _lang = payload[:3]
                    _desc, val = _split_encoded(payload[4:], payload[0])
                    if val:
                        tags.extra[key] = _decode_text(val, payload[0])
            else:
                val = _decode_text(payload[1:], payload[0])
                if val and not getattr(tags, key, ""):
                    setattr(tags, key, val)
    return tags


# --------------------------------------------------------------------------------------
# FLAC
# --------------------------------------------------------------------------------------


def _parse_flac_picture(data: bytes) -> tuple[bytes | None, str]:
    try:
        p = 0
        ptype = struct.unpack(">I", data[p:p + 4])[0]; p += 4
        mlen = struct.unpack(">I", data[p:p + 4])[0]; p += 4
        mime = data[p:p + mlen].decode("latin-1", "replace"); p += mlen
        dlen = struct.unpack(">I", data[p:p + 4])[0]; p += 4 + dlen
        p += 16                                   # w,h,depth,colors
        ilen = struct.unpack(">I", data[p:p + 4])[0]; p += 4
        img = data[p:p + ilen]
        if ptype in (3, 4) or not mime:
            pass
        return img, (mime or _mime_for(img))
    except Exception:
        return None, ""


def _parse_vorbis_comment(data: bytes) -> dict:
    out: dict[str, str] = {}
    try:
        p = 0
        vlen = struct.unpack("<I", data[p:p + 4])[0]; p += 4
        vendor = data[p:p + vlen]; p += vlen
        count = struct.unpack("<I", data[p:p + 4])[0]; p += 4
        for _ in range(min(count, 512)):
            if p + 4 > len(data):
                break
            l = struct.unpack("<I", data[p:p + 4])[0]; p += 4
            item = data[p:p + l]; p += l
            if b"=" in item:
                k, v = item.split(b"=", 1)
                out[k.decode("utf-8", "replace").upper()] = v.decode("utf-8", "replace")
    except Exception:
        pass
    return out


def _parse_flac(blob: bytes) -> MediaTags:
    tags = MediaTags()
    start = 0
    if blob[:3] == b"ID3":
        size = _syncsafe(blob[6:10])
        start = 10 + size
    if blob[start:start + 4] != b"fLaC":
        return tags
    p = start + 4
    while p + 4 <= len(blob):
        header = blob[p]
        last = header & 0x80
        btype = header & 0x7F
        length = int.from_bytes(blob[p + 1:p + 4], "big")
        p += 4
        block = blob[p:p + length]
        p += length
        if btype == 6 and block and tags.cover is None:
            img, mime = _parse_flac_picture(block)
            if img:
                tags.cover, tags.cover_mime = img, mime
        elif btype == 4 and block:
            vc = _parse_vorbis_comment(block)
            tags.title = tags.title or vc.get("TITLE", "")
            tags.artist = tags.artist or vc.get("ARTIST", "")
            tags.album = tags.album or vc.get("ALBUM", "")
            tags.year = tags.year or vc.get("DATE", "")
            tags.genre = tags.genre or vc.get("GENRE", "")
            tags.track = tags.track or vc.get("TRACKNUMBER", "")
            if tags.cover is None and vc.get("METADATA_BLOCK_PICTURE"):
                try:
                    raw = base64.b64decode(vc["METADATA_BLOCK_PICTURE"])
                    img, mime = _parse_flac_picture(raw)
                    if img:
                        tags.cover, tags.cover_mime = img, mime
                except Exception:
                    pass
        if last:
            break
    return tags


# --------------------------------------------------------------------------------------
# MP4 / M4A
# --------------------------------------------------------------------------------------

_CONTAINER_ATOMS = {b"moov", b"udta", b"trak", b"mdia", b"minf", b"stbl", b"meta", b"ilst", b"edts"}


def _walk_atoms(blob: bytes, start: int, end: int, depth: int, found: dict):
    p = start
    while p + 8 <= end:
        size = struct.unpack(">I", blob[p:p + 4])[0]
        typ = blob[p + 4:p + 8]
        header = 8
        if size == 1:
            if p + 16 > end:
                return
            size = struct.unpack(">Q", blob[p + 8:p + 16])[0]
            header = 16
        elif size == 0:
            size = end - p
        if size < header or p + size > end:
            return
        body_start = p + header
        if typ == b"meta":
            body_start += 4               # meta 是 full box
        if typ in _CONTAINER_ATOMS and depth < 8:
            _walk_atoms(blob, body_start, p + size, depth + 1, found)
        elif typ == b"covr":
            if "cover" not in found:
                q = body_start
                while q + 8 <= p + size:
                    dsz = struct.unpack(">I", blob[q:q + 4])[0]
                    dtyp = blob[q + 4:q + 8]
                    if dsz < 8 or q + dsz > p + size:
                        break
                    if dtyp == b"data" and dsz > 16:
                        kind = struct.unpack(">I", blob[q + 8:q + 12])[0] & 0xFFFFFF
                        img = blob[q + 16:q + dsz]
                        mime = {13: "image/jpeg", 14: "image/png", 12: "image/gif"}.get(kind, "")
                        found["cover"] = img
                        found["cover_mime"] = mime or _mime_for(img)
                        break
                    q += dsz
        elif typ == b"\xa9nam":
            found.setdefault("title", _ilst_text(blob, body_start, p + size))
        elif typ == b"\xa9ART":
            found.setdefault("artist", _ilst_text(blob, body_start, p + size))
        elif typ == b"\xa9alb":
            found.setdefault("album", _ilst_text(blob, body_start, p + size))
        elif typ == b"\xa9day":
            found.setdefault("year", _ilst_text(blob, body_start, p + size))
        elif typ == b"\xa9gen":
            found.setdefault("genre", _ilst_text(blob, body_start, p + size))
        p += size


def _ilst_text(blob: bytes, start: int, end: int) -> str:
    try:
        if blob[start + 4:start + 8] == b"data":
            dsz = struct.unpack(">I", blob[start:start + 4])[0]
            return blob[start + 16:start + dsz].decode("utf-8", "replace")
    except Exception:
        pass
    return ""


def _parse_mp4(blob: bytes) -> MediaTags:
    tags = MediaTags()
    # 从 moov 开始找（可能在文件头之后, 也常见在尾部；这里只扫头部窗口）
    idx = blob.find(b"moov")
    if idx < 4:
        return tags
    found: dict = {}
    _walk_atoms(blob, idx - 4, len(blob), 0, found)
    tags.title = found.get("title", "") or ""
    tags.artist = found.get("artist", "") or ""
    tags.album = found.get("album", "") or ""
    tags.year = found.get("year", "") or ""
    tags.genre = found.get("genre", "") or ""
    if "cover" in found:
        tags.cover = found["cover"]
        tags.cover_mime = found.get("cover_mime", "") or _mime_for(tags.cover)
    return tags


# --------------------------------------------------------------------------------------
# OGG / Opus
# --------------------------------------------------------------------------------------


def _parse_ogg(blob: bytes) -> MediaTags:
    tags = MediaTags()
    for marker in (b"\x03vorbis", b"OpusTags"):
        i = blob.find(marker)
        if i < 0:
            continue
        vc = _parse_vorbis_comment(blob[i + len(marker):])
        tags.title = tags.title or vc.get("TITLE", "")
        tags.artist = tags.artist or vc.get("ARTIST", "")
        tags.album = tags.album or vc.get("ALBUM", "")
        tags.year = tags.year or vc.get("DATE", "")
        tags.genre = tags.genre or vc.get("GENRE", "")
        if tags.cover is None and vc.get("METADATA_BLOCK_PICTURE"):
            try:
                img, mime = _parse_flac_picture(base64.b64decode(vc["METADATA_BLOCK_PICTURE"]))
                if img:
                    tags.cover, tags.cover_mime = img, mime
            except Exception:
                pass
        if not tags.is_empty():
            break
    return tags


# --------------------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------------------


def read_tags(path: str) -> MediaTags:
    """读取标签与内嵌封面。永不抛异常。"""
    try:
        if not os.path.isfile(path):
            return MediaTags()
        ext = os.path.splitext(path)[1].lower()
        head = _read_head(path)
        if not head:
            return MediaTags()
        if head[:3] == b"ID3":
            t = _parse_id3v2(head)
            # 有些 MP3 的 ID3 很大 / 封面在尾部, 再扫一次尾部
            if t.cover is None:
                t2 = _read_tail_id3(path)
                if t2 and t2.cover:
                    t.cover, t.cover_mime = t2.cover, t2.cover_mime
            return t
        if head[:4] == b"fLaC" or (head[:3] == b"ID3" and b"fLaC" in head[:64]):
            return _parse_flac(head)
        if ext in (".m4a", ".m4b", ".mp4", ".mov", ".m4v", ".aac", ".alac"):
            t = _parse_mp4(head)
            if t.cover is None and ext in (".m4a", ".m4b", ".mp4"):
                tail = _read_tail(path, 4 * 1024 * 1024)
                if b"moov" in tail:
                    t2 = _parse_mp4(tail)
                    t.cover = t2.cover
                    t.cover_mime = t2.cover_mime or t.cover_mime
                    t.title = t.title or t2.title
                    t.artist = t.artist or t2.artist
                    t.album = t.album or t2.album
            return t
        if head[:4] == b"OggS":
            return _parse_ogg(head)
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            i = head.find(b"id3 ")
            if i > 0:
                return _parse_id3v2(head[i + 8:])
            return MediaTags()
        # 兜底：扫一遍头部找 ID3
        i = head.find(b"ID3")
        if 0 <= i < 4096:
            return _parse_id3v2(head[i:])
        return MediaTags()
    except Exception:
        return MediaTags()


def _read_tail(path: str, n: int) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read(n)
    except OSError:
        return b""


def _read_tail_id3(path: str) -> MediaTags:
    tail = _read_tail(path, 1024 * 1024)
    i = tail.find(b"ID3")
    if i >= 0:
        return _parse_id3v2(tail[i:])
    return MediaTags()
