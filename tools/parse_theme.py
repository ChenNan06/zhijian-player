# -*- coding: utf-8 -*-
"""Parse GeePlayer theme.json (UTF-8 BOM) into a flat colour table."""
import json, io, os, re, collections

SRC = r"E:\IQIYI Video\GeePlayer\5.4.1.5408\theme.json"
OUT = r"D:\AI-Project\至简Player\docs\_raw_theme.json"

with open(SRC, "rb") as f:
    raw = f.read()
bom = raw[:3] == b"\xef\xbb\xbf"
txt = raw.decode("utf-8-sig")

data = json.loads(txt)

def norm_color(v):
    """Return (original, aarrggbb_hex, #RRGGBB, alpha, fmt)."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    out = {"raw": v}
    m = re.fullmatch(r"0[xX]([0-9a-fA-F]{8})", s)
    if m:
        h = m.group(1).upper()
        a = int(h[0:2], 16)
        out.update(fmt="0xAARRGGBB", aarrggbb="0x" + h,
                   rgb="#%s" % h[2:], alpha=a, alpha_pct=round(a / 255.0 * 100, 1))
        return out
    m = re.fullmatch(r"0[xX]([0-9a-fA-F]{6})", s)
    if m:
        h = m.group(1).upper()
        out.update(fmt="0xRRGGBB", aarrggbb="0xFF" + h, rgb="#%s" % h,
                   alpha=255, alpha_pct=100.0)
        return out
    m = re.fullmatch(r"#([0-9a-fA-F]{8})", s)
    if m:
        h = m.group(1).upper()
        out.update(fmt="#AARRGGBB", aarrggbb="0x" + h, rgb="#%s" % h[2:],
                   alpha=int(h[0:2], 16), alpha_pct=round(int(h[0:2], 16) / 2.55, 1))
        return out
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", s)
    if m:
        h = m.group(1).upper()
        out.update(fmt="#RRGGBB", aarrggbb="0xFF" + h, rgb="#%s" % h, alpha=255, alpha_pct=100.0)
        return out
    # bare 8 hex digits, e.g. 'ff1a1818' (AARRGGBB) -- GeePlayer theme.json style
    m = re.fullmatch(r"([0-9a-fA-F]{8})", s)
    if m:
        h = m.group(1).upper()
        out.update(fmt="AARRGGBB(bare)", aarrggbb="0x" + h, rgb="#%s" % h[2:],
                   alpha=int(h[0:2], 16), alpha_pct=round(int(h[0:2], 16) / 255.0 * 100, 1))
        return out
    # bare 6 hex digits, e.g. '00993d' (RRGGBB)
    m = re.fullmatch(r"([0-9a-fA-F]{6})", s)
    if m:
        h = m.group(1).upper()
        out.update(fmt="RRGGBB(bare)", aarrggbb="0xFF" + h, rgb="#%s" % h,
                   alpha=255, alpha_pct=100.0)
        return out
    out.update(fmt="non-color-literal")
    return out


def flatten(obj, prefix=""):
    flat = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            flat.update(flatten(v, prefix + ("/" if prefix else "") + str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            flat.update(flatten(v, "%s[%d]" % (prefix, i)))
    else:
        flat[prefix] = obj
    return flat


flat = flatten(data)
colors = {}
others = {}
for k, v in flat.items():
    nc = norm_color(v) if isinstance(v, str) else None
    if nc and nc["fmt"] != "non-color-literal":
        nc["key"] = k
        colors[k] = nc
    else:
        others[k] = v

# duplicate colour values -> group
byval = collections.defaultdict(list)
for k, v in colors.items():
    byval[v["aarrggbb"]].append(k)

blob = {
    "source": SRC,
    "bom": bom,
    "encoding_used": "utf-8-sig",
    "bytes": len(raw),
    "top_level_keys": list(data.keys()) if isinstance(data, dict) else None,
    "key_count_total": len(flat),
    "color_key_count": len(colors),
    "non_color_key_count": len(others),
    "theme_colors": colors,
    "non_color_entries": others,
    "distinct_color_values": len(byval),
    "color_value_groups": {k: sorted(v) for k, v in sorted(byval.items(), key=lambda kv: -len(kv[1]))},
    "raw_json": data,
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with io.open(OUT, "w", encoding="utf-8") as f:
    json.dump(blob, f, ensure_ascii=False, indent=1)

print("BOM            :", bom)
print("total keys     :", len(flat))
print("colour keys    :", len(colors))
print("non-colour keys:", len(others))
print("distinct values:", len(byval))
print("top-level      :", list(data.keys()) if isinstance(data, dict) else type(data))
print("--- non-colour entries ---")
for k, v in list(others.items())[:40]:
    print("  %s = %r" % (k, v))
print("wrote", OUT, "%.1f KB" % (os.path.getsize(OUT) / 1024.0))
