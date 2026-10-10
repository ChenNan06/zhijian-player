"""生成验收用的测试媒体（视频 / 图片 / 音频 / 字幕）。

用法：
    python scripts\\30-gen-testmedia.py            # 生成缺失的
    python scripts\\30-gen-testmedia.py --force    # 全部重建

产物落在 assets\\testmedia\\：
  * clip_1080p.mp4            1080p30 H.264 + AAC, 20s
  * clip_1080p.srt / .ass / .vtt  同名外挂字幕（用于验证 sub-auto=fuzzy）
  * orient_white_top.mp4      上白下黑, 用于判定画面是否上下颠倒
  * uhd4k_45m.mp4             3840x2160 30fps 120s ≈45Mbps, 4K 高码率 Seek 基准
  * photo_*.jpg/png/webp/avif/tiff/bmp  图片路由与缩放测试
  * music_cover.mp3/.flac/.m4a          内嵌专辑封面测试
  * tone.wav / tone.flac                纯音频
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "assets", "testmedia")
FFMPEG = os.environ.get("ZHIJIAN_FFMPEG", r"E:\DevTools\ffmpeg\bin\ffmpeg.exe")


def run(args, quiet=True):
    print("  $ " + " ".join(args[:6]) + (" ..." if len(args) > 6 else ""), flush=True)
    r = subprocess.run(
        args,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        sys.stderr.write(r.stderr.decode("utf-8", "replace")[-2000:] + "\n")
        raise SystemExit(f"命令失败: {args[0]} rc={r.returncode}")
    return r


def out(name):
    return os.path.join(OUT, name)


def need(name, force):
    p = out(name)
    return force or not (os.path.isfile(p) and os.path.getsize(p) > 1024)


def gen_video_1080(force):
    if not need("clip_1080p.mp4", force):
        return
    print("[video] 1080p 测试片 ...")
    run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        out("clip_1080p.mp4"),
    ])


def gen_video_4k(force):
    if not need("uhd4k_45m.mp4", force):
        return
    print("[video] 4K 高码率基准片（这一步比较久, 约 2~5 分钟）...")
    run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=30:duration=120",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=120",
        "-c:v", "libx264", "-preset", "faster", "-b:v", "45M", "-maxrate", "60M",
        "-bufsize", "120M", "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "30",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        out("uhd4k_45m.mp4"),
    ])


def gen_orient(force):
    if not need("orient_white_top.mp4", force):
        return
    print("[video] 方向自检片 ...")
    run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=white:s=640x180:d=6",
        "-f", "lavfi", "-i", "color=c=black:s=640x180:d=6",
        "-filter_complex", "[0:v][1:v]vstack=inputs=2,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-movflags", "+faststart",
        out("orient_white_top.mp4"),
    ])


def gen_subtitles(force):
    srt = out("clip_1080p.srt")
    if force or not os.path.isfile(srt):
        print("[sub] 同名外挂字幕 ...")
        lines = []
        for i in range(1, 9):
            lines.append(
                f"{i}\n00:00:{i*2:02d},000 --> 00:00:{i*2+1:02d},500\n"
                f"至简Player 字幕测试第 {i} 行\nSubtitle line {i}\n"
            )
        with open(srt, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    ass = out("clip_1080p.ass")
    if force or not os.path.isfile(ass):
        body = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei UI,64,&H00FFFFFF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,2.5,1.2,2,40,40,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.50,0:00:04.00,Default,,0,0,0,,{\\fad(200,200)}ASS 字幕渲染测试 —— 阴影 / 描边 / 淡入淡出
Dialogue: 0,0:00:04.50,0:00:09.00,Default,,0,0,0,,{\\c&H00D7FF&}彩色文字 {\\c&HFFFFFF&}与 {\\b1}加粗{\\b0} 样式
Dialogue: 0,0:00:09.50,0:00:15.00,Default,,0,0,0,,位置测试：本行应出现在画面下方约 92%
Dialogue: 0,0:00:15.50,0:00:19.50,Default,,0,0,0,,最后一行 —— 用于验证时间轴 ±0.5s 微调
"""
        with open(ass, "w", encoding="utf-8") as f:
            f.write(body)
    vtt = out("clip_1080p.vtt")
    if force or not os.path.isfile(vtt):
        with open(vtt, "w", encoding="utf-8") as f:
            f.write(
                "WEBVTT\n\n"
                "00:00:01.000 --> 00:00:03.000\nVTT 字幕测试 第一行\n\n"
                "00:00:05.000 --> 00:00:08.000\nVTT 字幕测试 第二行\n"
            )


def gen_images(force):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[img] 跳过：没有 Pillow")
        return
    fonts = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    font_path = next((f for f in fonts if os.path.isfile(f)), None)

    specs = [
        ("photo_4k.jpg", (3840, 2160), "JPEG"),
        ("photo_portrait.jpg", (1080, 1920), "JPEG"),
        ("photo_square.png", (1600, 1600), "PNG"),
        ("photo_wide.webp", (2560, 1080), "WEBP"),
        ("photo_small.jpg", (480, 320), "JPEG"),
        ("photo_avif.avif", (1280, 720), "AVIF"),
        ("photo_tiff.tiff", (1024, 768), "TIFF"),
        ("photo_bmp.bmp", (800, 600), "BMP"),
    ]
    for name, size, fmt in specs:
        p = out(name)
        if not force and os.path.isfile(p) and os.path.getsize(p) > 1024:
            continue
        print(f"[img] {name} {size[0]}x{size[1]} {fmt}")
        img = Image.new("RGB", size, (18, 20, 26))
        d = ImageDraw.Draw(img)
        w, h = size
        # 渐变底
        for y in range(0, h, max(1, h // 240)):
            t = y / max(1, h - 1)
            d.rectangle(
                [0, y, w, y + max(1, h // 240)],
                fill=(int(24 + 40 * t), int(28 + 60 * t), int(40 + 110 * t)),
            )
        # 网格
        step = max(40, w // 24)
        for x in range(0, w, step):
            d.line([(x, 0), (x, h)], fill=(255, 255, 255, 24), width=1)
        for y in range(0, h, step):
            d.line([(0, y), (w, y)], fill=(255, 255, 255, 24), width=1)
        # 四角标记（用于验证缩放/旋转/镜像是否正确）
        m = max(24, w // 30)
        d.rectangle([0, 0, m, m], fill=(255, 64, 64))
        d.rectangle([w - m, 0, w, m], fill=(64, 255, 128))
        d.rectangle([0, h - m, m, h], fill=(64, 160, 255))
        d.rectangle([w - m, h - m, w, h], fill=(255, 214, 64))
        # 中心十字
        cx, cy = w // 2, h // 2
        d.line([(cx - m, cy), (cx + m, cy)], fill=(255, 255, 255), width=max(2, w // 400))
        d.line([(cx, cy - m), (cx, cy + m)], fill=(255, 255, 255), width=max(2, w // 400))
        label = f"至简Player\n{name}\n{w} x {h}"
        if font_path:
            try:
                fnt = ImageFont.truetype(font_path, max(20, w // 22))
                tb = d.multiline_textbbox((0, 0), label, font=fnt, align="center", spacing=8)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
                d.multiline_text(
                    ((w - tw) / 2, (h - th) / 2 + m),
                    label, font=fnt, fill=(255, 255, 255), align="center", spacing=8,
                )
            except Exception:
                pass
        save_kw = {}
        if fmt == "JPEG":
            save_kw = {"quality": 92, "subsampling": 0}
        img.save(p, fmt, **save_kw)
        # 顺带做一张带 EXIF 旋转标记的 JPEG（验证 EXIF Orientation 处理）
    rot = out("photo_exif_rot90.jpg")
    if force or not os.path.isfile(rot):
        img = Image.open(out("photo_small.jpg"))
        exif = img.getexif()
        exif[274] = 6  # Orientation = Rotate 90 CW
        img.save(rot, "JPEG", quality=92, exif=exif)


def gen_audio(force):
    cover = out("cover_art.png")
    if force or not os.path.isfile(cover):
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (600, 600), (16, 18, 26))
            d = ImageDraw.Draw(img)
            for i in range(0, 600, 6):
                d.rectangle([0, i, 600, i + 3], fill=(int(20 + i * 0.25), int(40 + i * 0.18), int(90 + i * 0.22)))
            d.ellipse([170, 170, 430, 430], outline=(255, 255, 255), width=6)
            d.ellipse([255, 255, 345, 345], fill=(240, 244, 255))
            img.save(cover, "PNG")
        except ImportError:
            pass

    if need("tone.flac", force):
        print("[audio] tone.flac ...")
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
             "-af", "volume=0.4", "-c:a", "flac", out("tone.flac")])

    if need("music_cover.mp3", force):
        print("[audio] music_cover.mp3 (ID3v2 APIC 内嵌封面) ...")
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=392:duration=25",
             "-af", "volume=0.35", "-c:a", "libmp3lame", "-b:a", "192k",
             "-metadata", "title=至简Player 测试曲目", "-metadata", "artist=ZhiJian",
             "-metadata", "album=引擎验收", out("music_cover.mp3")])
        if os.path.isfile(cover):
            tmp = out("_tmp_cover.mp3")
            run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", out("music_cover.mp3"), "-i", cover,
                 "-map", "0:a", "-map", "1:v", "-c:a", "copy", "-c:v", "copy",
                 "-id3v2_version", "3",
                 "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
                 tmp])
            os.replace(tmp, out("music_cover.mp3"))

    if need("music_cover.flac", force):
        print("[audio] music_cover.flac (FLAC PICTURE 块) ...")
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=523:duration=25",
             "-af", "volume=0.35", "-c:a", "flac",
             "-metadata", "title=至简Player 测试曲目 FLAC", "-metadata", "artist=ZhiJian",
             out("music_cover.flac")])
        if os.path.isfile(cover):
            tmp = out("_tmp_cover.flac")
            run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", out("music_cover.flac"), "-i", cover,
                 "-map", "0:a", "-map", "1:v", "-c:a", "copy", "-c:v", "copy",
                 "-disposition:v", "attached_pic", tmp])
            os.replace(tmp, out("music_cover.flac"))

    if need("music_cover.m4a", force):
        print("[audio] music_cover.m4a (MP4 covr 原子) ...")
        args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=659:duration=25"]
        if os.path.isfile(cover):
            args += ["-i", cover, "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                     "-disposition:v", "attached_pic"]
        # -af 必须紧挨着输出文件（放在第二个 -i 之前会被当成输入选项而报错）
        args += ["-af", "volume=0.35", "-c:a", "aac", "-b:a", "192k",
                 "-metadata", "title=至简Player 测试曲目 M4A", "-metadata", "artist=ZhiJian",
                 out("music_cover.m4a")]
        run(args)


def main():
    force = "--force" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    if not os.path.isfile(FFMPEG):
        raise SystemExit(f"找不到 ffmpeg: {FFMPEG}（可用 ZHIJIAN_FFMPEG 覆盖）")
    gen_video_1080(force)
    gen_orient(force)
    gen_subtitles(force)
    gen_images(force)
    gen_audio(force)
    gen_video_4k(force)
    print("\n完成。assets\\testmedia 内容：")
    total = 0
    for n in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, n)
        if os.path.isfile(p):
            sz = os.path.getsize(p)
            total += sz
            print(f"  {n:<28} {sz/1048576:9.2f} MB")
    print(f"  {'合计':<28} {total/1048576:9.2f} MB")


if __name__ == "__main__":
    main()
