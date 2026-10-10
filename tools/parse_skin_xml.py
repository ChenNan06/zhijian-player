# -*- coding: utf-8 -*-
"""Parse all GeePlayer skin XML layouts into a structured JSON blob.

Read-only against the install dir.
Output: D:\\AI-Project\\至简Player\\docs\\_raw_xml_layouts.json
"""
import os, sys, json, io, re
import xml.etree.ElementTree as ET
try:
    from lxml import etree as LET
except Exception:
    LET = None

SKIN = r"E:\IQIYI Video\GeePlayer\5.4.1.5408\skin"
OUT = r"D:\AI-Project\至简Player\docs\_raw_xml_layouts.json"

# Attributes we care about most (kept always); everything else is kept too but flagged.
GEOM_KEYS = ["name", "x", "y", "width", "height", "minwidth", "maxwidth", "minheight",
             "maxheight", "margin", "padding", "inset", "align", "pos", "weight",
             "bkcolor", "bkimage", "bordersize", "bordercolor", "textcolor",
             "hottextcolor", "pushedtextcolor", "disabledtextcolor", "indexfont",
             "textpadding", "float", "visible", "corner", "source", "dest", "text",
             "tooltip", "childpadding", "thumbsize", "value", "max", "min",
             "forepadding", "userdata", "tag", "adjustwidth", "wholedisplay"]

DPI_RE = re.compile(r'dpi(\d{3})', re.I)


def read_text(path):
    """Explicit utf-8, tolerating BOM; fall back to gbk if that fails."""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "latin-1/replace"


class Node(dict):
    pass


def LET_to_ET(el):
    """Convert an lxml element tree into xml.etree elements (keeping first value
    of any duplicated attribute, recording clashes under _duplicate_attrs)."""
    e = ET.Element(el.tag)
    seen = {}
    dups = {}
    for k, v in el.attrib.items():
        if k in seen:
            dups.setdefault(k, [seen[k]]).append(v)
            continue
        seen[k] = v
        e.set(k, v)
    if dups:
        e.set("_duplicate_attrs", json.dumps(dups, ensure_ascii=False))
    if el.text and el.text.strip():
        e.text = el.text
    for c in el:
        if not isinstance(c.tag, str):
            continue  # comment / PI
        e.append(LET_to_ET(c))
    return e


def parse_node(el, depth=0, path=""):
    node = {
        "tag": el.tag,
        "path": (path + "/" + el.tag) if path else el.tag,
        "depth": depth,
        "attrs": {k: v for k, v in el.attrib.items()},
    }
    # geometry shortcut
    geo = {k: el.attrib[k] for k in GEOM_KEYS if k in el.attrib}
    if geo:
        node["geometry"] = geo
    # find dpi references anywhere in values
    dpi = set()
    for k, v in el.attrib.items():
        for m in DPI_RE.finditer(str(v)):
            dpi.add(m.group(1))
        if "_dpi" in str(v):
            pass
    if dpi:
        node["dpi_variants"] = sorted(dpi)
    kids = [parse_node(c, depth + 1, node["path"]) for c in list(el)]
    if kids:
        node["children"] = kids
    if el.text and el.text.strip():
        node["text_content"] = el.text.strip()
    return node


def walk_flat(node, out, file_key):
    nm = node["attrs"].get("name") or ""
    key = nm if nm else ("%s@%d" % (node["tag"], len(out)))
    # make unique
    base = key
    i = 2
    while key in out:
        key = "%s#%d" % (base, i)
        i += 1
    rec = {"tag": node["tag"], "path": node["path"]}
    rec.update(node["attrs"])
    if node.get("dpi_variants"):
        rec["_dpi_variants"] = node["dpi_variants"]
    out[key] = rec
    for c in node.get("children", []):
        walk_flat(c, out, file_key)


def main():
    files = []
    for root, dirs, fns in os.walk(SKIN):
        for fn in fns:
            if fn.lower().endswith(".xml"):
                files.append(os.path.join(root, fn))
    files.sort()

    result = {}
    errors = {}
    recovered = {}
    tag_hist = {}
    attr_hist = {}
    enc_hist = {}
    total_nodes = 0

    for fp in files:
        rel = os.path.relpath(fp, SKIN).replace("\\", "/")
        txt, enc = read_text(fp)
        enc_hist[enc] = enc_hist.get(enc, 0) + 1
        mode = "ElementTree(strict)"
        try:
            root = ET.fromstring(txt.encode("utf-8"))
        except Exception as e:
            if LET is None:
                errors[rel] = "%s: %s" % (type(e).__name__, e)
                continue
            # tolerant re-parse: drop duplicate attributes on the same tag
            parser = LET.XMLParser(recover=True, resolve_entities=False,
                                   remove_comments=False, huge_tree=True)
            lroot = LET.fromstring(txt.encode("utf-8"), parser)
            if lroot is None:
                errors[rel] = "%s: %s | lxml recover also failed" % (type(e).__name__, e)
                continue
            root = LET_to_ET(lroot)
            mode = "lxml(recover) [strict failed: %s]" % type(e).__name__
            recovered[rel] = mode
        tree = parse_node(root)
        flat = {}
        walk_flat(tree, flat, rel)
        total_nodes += len(flat)
        for v in flat.values():
            tag_hist[v["tag"]] = tag_hist.get(v["tag"], 0) + 1
            for k in v.keys():
                if k in ("tag", "path", "_dpi_variants"):
                    continue
                attr_hist[k] = attr_hist.get(k, 0) + 1
        # root-level window attributes
        win_attrs = dict(root.attrib)
        result[rel] = {
            "window_attrs": win_attrs,
            "parse_mode": mode,
            "control_count": len(flat),
            "controls": flat,
        }

    blob = {
        "source_dir": SKIN,
        "xml_file_count": len(files),
        "parsed_ok": len(result),
        "parse_errors": errors,
        "recovered_with_lxml": recovered,
        "encoding_histogram": enc_hist,
        "total_control_nodes": total_nodes,
        "tag_histogram": dict(sorted(tag_hist.items(), key=lambda kv: -kv[1])),
        "attribute_histogram": dict(sorted(attr_hist.items(), key=lambda kv: -kv[1])),
        "xml_layouts": result,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(blob, f, ensure_ascii=False, indent=1)

    print("XML files found : %d" % len(files))
    print("parsed ok       : %d" % len(result))
    print("parse errors    : %d %s" % (len(errors), list(errors.items())[:5]))
    print("lxml recovered  : %d" % len(recovered))
    print("control nodes   : %d" % total_nodes)
    print("encodings       : %s" % enc_hist)
    print("top tags        : %s" % list(blob["tag_histogram"].items())[:14])
    print("wrote %s (%.1f KB)" % (OUT, os.path.getsize(OUT) / 1024.0))


if __name__ == "__main__":
    main()
