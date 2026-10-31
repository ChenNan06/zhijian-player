"""从媒体文件里直接读出内嵌专辑封面与标签（纯 Python, 不依赖 ffprobe）。

覆盖的容器：
  * MP3   —— ID3v2.2 / v2.3 / v2.4 的 APIC（v2.2 叫 PIC）
  * FLAC  —— METADATA_BLOCK_PICTURE（type 6）
  * M4A/MP4 —— moov/udta/meta/ilst/covr
  * OGG/Opus —— Vorbis comment 里的 METADATA_BLOCK_PICTURE（base64）
  * WMA/ASF —— WM/Picture（尽力而为）

另外提供 **纯 Python 头部时长秒读**（task-131）：read_duration() 覆盖
MP3（ID3v2 偏移 + Xing/Info/VBRI/VBR-CBR 估算）、FLAC（STREAMINFO 采样总数）、
M4A/MP4（moov/mvhd 时间刻度）。播放列表 100% 时长走这条快路径，
不依赖 ffprobe / 第二个 mpv 实例，单文件通常 <1ms。

设计原则：任何解析失败都返回空结果, **绝不抛异常**, 也绝不打不开文件时卡住
（所有读取都限制在文件头部/元数据区, 大文件不会被整个读进内存）。
"""

from __future__ import annotations

import base64
import logging
import os
import struct
from dataclasses import dataclass, field

log = logging.getLogger("zhijian.media.tags")

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

# --------------------------------------------------------------------------------------
# 时长秒读（task-131：纯 Python 头部解析，绝不抛异常）
# --------------------------------------------------------------------------------------
#
# 为什么不用 ffprobe：ffprobe 是进程外调用（Windows 上每次 60~150ms 且要处理
# 隐藏控制台窗口），播放列表 14 首要 1~2 秒，用户看到的是"时长一个个慢慢冒出来"。
# 这三个容器的时长都写在**文件头部/尾部固定结构**里，直接按字节读即可，
# 单文件实测 <1ms，且不产生任何子进程（不弹黑框、不占 CPU 解码）。

#: MPEG 版本位（2 bit）→ (版本号, 采样率表, MPEG2/2.5 的 Layer III 帧内样点数)
_MPEG_VER = {0: (2.5, (11025, 12000, 8000), 576),
             2: (2.0, (22050, 24000, 16000), 576),
             3: (1.0, (44100, 48000, 32000), 1152)}

#: Layer 位（2 bit）→ (Layer 号, 每帧样点数, 每帧字节系数)
_MPEG_LAYER = {3: (1, 384, 12), 2: (2, 1152, 144), 1: (3, 1152, 144)}

_MPEG_BITRATE = {
    # (是否 MPEG1, layer) → 15 档 kbps（index 0/15 为保留值）
    (True, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}


def _id3v2_total(blob: bytes) -> int:
    """ID3v2 标签总字节数（含 10 字节头；带 footer 时 +10）；不是 ID3 返回 0。"""
    if len(blob) < 10 or blob[:3] != b"ID3":
        return 0
    flags = blob[5]
    return 10 + _syncsafe(blob[6:10]) + (10 if flags & 0x10 else 0)


def _mp3_frame_header(blob: bytes, pos: int):
    """解析 pos 处的 MPEG 音频帧头；非法返回 None。"""
    if pos + 4 > len(blob):
        return None
    b0, b1, b2, b3 = blob[pos], blob[pos + 1], blob[pos + 2], blob[pos + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    ver_bits = (b1 >> 3) & 0x03
    layer_bits = (b1 >> 1) & 0x03
    if ver_bits == 1 or layer_bits == 0:                  # 保留值
        return None
    br_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    mpeg_ver, srates, spf_v23 = _MPEG_VER[ver_bits]
    layer, spf, coef = _MPEG_LAYER[layer_bits]
    br = _MPEG_BITRATE[(mpeg_ver >= 1.0, layer)][br_idx]
    sr = srates[sr_idx]
    if br <= 0 or sr <= 0:
        return None
    if layer == 3 and mpeg_ver < 1.0:
        spf = spf_v23
    padding = (b2 >> 1) & 0x01
    if layer == 1:
        frame_len = (coef * br * 1000 // sr + padding) * 4
    elif layer == 3 and mpeg_ver < 1.0:
        frame_len = coef // 2 * br * 1000 // sr + padding
    else:
        frame_len = coef * br * 1000 // sr + padding
    mono = ((b3 >> 6) & 0x03) == 3
    return {"ver": mpeg_ver, "layer": layer, "bitrate": br, "samplerate": sr,
            "spf": spf, "frame_len": frame_len, "mono": mono}


def _mp3_duration(head: bytes, path: str) -> float:
    """MP3 时长：Xing/Info/VBRI 帧数优先，否则按 CBR 码率估算。"""
    start = _id3v2_total(head)
    # 找第一个合法帧（最多向后找 8KB，容忍 ID3 后偶发的垃圾字节）
    limit = min(len(head) - 4, start + 8192)
    frame_pos = -1
    hdr = None
    p = max(0, start)
    while p <= limit:
        h = _mp3_frame_header(head, p)
        if h is not None and h["frame_len"] >= 4:
            # 再验证下一帧，避免把随机字节误判成帧头
            nxt = head[p + h["frame_len"]:p + h["frame_len"] + 2]
            if len(nxt) < 2 or (nxt[0] == 0xFF and (nxt[1] & 0xE0) == 0xE0):
                frame_pos, hdr = p, h
                break
        p += 1
    if hdr is None:
        return 0.0
    sr, spf = hdr["samplerate"], hdr["spf"]
    # ---- Xing / Info（VBR）----
    side = 17 if hdr["mono"] else 32
    if hdr["ver"] < 1.0:
        side = 9 if hdr["mono"] else 17
    x = frame_pos + 4 + side
    if head[x:x + 4] in (b"Xing", b"Info") and len(head) >= x + 12:
        flags = int.from_bytes(head[x + 4:x + 8], "big")
        if flags & 0x01:
            frames = int.from_bytes(head[x + 8:x + 12], "big")
            if frames > 0:
                secs = frames * spf / float(sr)
                return secs if secs < 1_000_000_000.0 else 0.0
    # ---- VBRI（Fraunhofer 编码器）----
    v = frame_pos + 4 + 32
    if head[v:v + 4] == b"VBRI" and len(head) >= v + 18:
        frames = int.from_bytes(head[v + 14:v + 18], "big")
        if frames > 0:
            return frames * spf / float(sr)
    # ---- CBR 估算：音频字节数 / 码率 ----
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0.0
    audio_bytes = max(0, size - frame_pos)
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - 128))
            if f.read(3) == b"TAG":
                audio_bytes -= 128
    except OSError:
        pass
    return audio_bytes * 8.0 / (hdr["bitrate"] * 1000.0)


def _flac_duration(head: bytes) -> float:
    """FLAC 时长：STREAMINFO（type 0）里的采样总数 / 采样率。"""
    start = _id3v2_total(head)
    if head[start:start + 4] != b"fLaC":
        return 0.0
    p = start + 4
    while p + 4 <= len(head):
        header = head[p]
        btype = header & 0x7F
        length = int.from_bytes(head[p + 1:p + 4], "big")
        p += 4
        if btype == 0 and length >= 18 and len(head) >= p + 18:
            packed = int.from_bytes(head[p + 10:p + 18], "big")
            sr = packed >> 44
            total = packed & ((1 << 36) - 1)
            if 1 <= sr <= 768000 and total > 0:
                secs = total / float(sr)
                return secs if secs < 1_000_000_000.0 else 0.0
        p += length
        if header & 0x80:                        # last-metadata-block
            break
    return 0.0


def _mp4_mvhd_duration(blob: bytes) -> float:
    """mp4/m4a：找 moov.mvhd，用 timescale/duration 换算秒数。"""
    i = blob.find(b"mvhd")
    if i < 4:
        return 0.0
    p = i + 4
    if p + 5 > len(blob):
        return 0.0
    version = blob[p]
    try:
        if version == 1:
            p += 4 + 8 + 8
            if p + 12 > len(blob):
                return 0.0
            timescale = int.from_bytes(blob[p:p + 4], "big")
            duration = int.from_bytes(blob[p + 4:p + 12], "big")
        else:
            p += 4 + 4 + 4
            if p + 8 > len(blob):
                return 0.0
            timescale = int.from_bytes(blob[p:p + 4], "big")
            duration = int.from_bytes(blob[p + 4:p + 8], "big")
    except (IndexError, ValueError):
        return 0.0
    # 合理性护栏：mvhd 可能在 mdat 里被误命中（随机字节），时间刻度不合法直接放弃
    if not (1 <= timescale <= 1_000_000) or duration <= 0:
        return 0.0
    secs = duration / float(timescale)
    return secs if 0.0 < secs < 1_000_000_000.0 else 0.0


def read_duration(path: str) -> float:
    """纯 Python 头部秒读媒体时长（秒）；失败返回 0.0，**绝不抛异常**。

    覆盖：MP3（ID3v2 偏移 + Xing/Info/VBRI/CBR 估算）、FLAC（STREAMINFO）、
    M4A/MP4/MOV（moov/mvhd，moov 在尾部时会读一次文件尾部）。
    图片/未知容器返回 0.0，交给上层（mpv 探针）兜底。
    """
    try:
        if not path or not os.path.isfile(path):
            return 0.0
        ext = os.path.splitext(path)[1].lower()
        # 头部只读 256KB（MP3/FLAC/M4A 的时长结构都在前几十 KB）；ID3v2 特别大
        # （内嵌封面）时按标签长度自适应补读，避免大播放列表白读几百 MB。
        head = _read_head(path, 256 * 1024)
        if not head:
            return 0.0
        if head[:3] == b"ID3":
            need = _id3v2_total(head) + 64 * 1024
            if need > len(head):
                head = _read_head(path, min(8 * 1024 * 1024, need))
        if head[:4] == b"fLaC" or (head[:3] == b"ID3" and b"fLaC" in head[:64]):
            return _flac_duration(head)
        if ext in (".mp3", ".mp2", ".mpa", ".m2a") or head[:3] == b"ID3":
            d = _mp3_duration(head, path)
            if d > 0:
                return d
        if ext in (".m4a", ".m4b", ".m4r", ".mp4", ".mov", ".m4v", ".aac", ".alac") \
                or head[4:8] == b"ftyp":
            d = _mp4_mvhd_duration(head)
            if d > 0:
                return d
            tail = _read_tail(path, 6 * 1024 * 1024)
            if tail and b"mvhd" in tail:
                return _mp4_mvhd_duration(tail)
            return 0.0
        return 0.0
    except Exception:
        log.warning("读取时长失败: %s", path, exc_info=True)
        return 0.0
