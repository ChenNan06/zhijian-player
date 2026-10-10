# -*- coding: utf-8 -*-
"""Print a compact summary of the measured screenshot data for report writing."""
import json, io, sys

B = json.load(io.open(r"D:\AI-Project\至简Player\docs\_raw_shots_measured.json", encoding="utf-8"))
want = sys.argv[1] if len(sys.argv) > 1 else None

for fn, r in B.items():
    if want and want not in fn:
        continue
    print("=" * 100)
    print("%s   %dx%d  logical %.1fx%.1f" % (fn, *r["size"], *r["logical_size"]))
    print("-- sampled_colors --")
    for k, v in r["sampled_colors"].items():
        print("   %-26s px%-12s logical%-16s %s" % (k, v["px"], [round(x, 1) for x in v["logical"]], v["rgb"]))
    print("-- measured_regions --")
    for k, v in r["measured_regions"].items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "logical_from" in v[0]:
            print("   %s:" % k)
            for g in v:
                print("      px %4d..%-4d size %-4d | logical %7.1f..%-7.1f size %6.1f" % (
                    g["from"], g["to"], g["size"], g["logical_from"],
                    g["logical_from"] + g["logical_size"], g["logical_size"]))
        elif isinstance(v, list):
            print("   %s: %d raw events" % (k, len(v)))
        else:
            print("   %s: %s" % (k, json.dumps(v, ensure_ascii=False)[:400]))
