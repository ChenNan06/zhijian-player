"""探明 mpv_set_property_async 的真实参数顺序。

libmpv 的文档写的是 `(ctx, reply_userdata, name, format, data)`，但 mpv_set_property 是
`(ctx, name, format, data)` —— 很容易照抄错。写错不会报错, 只会返回 -9 或者直接访问违例。
这里每个变体单独起一个进程试, 免得一个崩了看不见别的。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import c_char_p, c_double, c_int, c_uint64, c_void_p

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

VARIANT = r'''
import sys, ctypes, time
from ctypes import c_char_p, c_double, c_int, c_uint64, c_void_p
sys.path.insert(0, r"{src}")
from zhijian.mpv.libmpv import MpvHandle
h = MpvHandle(); h.initialize()
lib = h._lib
order = {order}
if order == "A":      # (ctx, userdata, name, fmt, data)
    lib.mpv_set_property_async.argtypes = [c_void_p, c_uint64, c_char_p, c_int, c_void_p]
else:                 # (ctx, name, fmt, data, userdata)
    lib.mpv_set_property_async.argtypes = [c_void_p, c_char_p, c_int, c_void_p, c_uint64]
lib.mpv_set_property_async.restype = c_int
v = c_double(80.0)
name = b"volume"
keep = [v, name]
if order == "A":
    rc = lib.mpv_set_property_async(h.handle, 0, name, 5, ctypes.addressof(v))
else:
    rc = lib.mpv_set_property_async(h.handle, name, 5, ctypes.addressof(v), 0)
time.sleep(0.6)
print("order", order, "rc =", rc, "volume =", h.get_property("volume", 5))
h.terminate_destroy()
'''.format(src=os.path.join(ROOT, "src"), order="{ORDER}")

for order in ("A", "B"):
    src = VARIANT.replace('order = "A"', 'order = "' + order + '"').replace("{ORDER}", order)
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True,
                       timeout=60)
    out = (r.stdout or "").strip().replace("\n", " | ")
    err = (r.stderr or "").strip().splitlines()
    print(f"变体 {order}: rc={r.returncode}  {out}  {err[-1] if err else ''}")
