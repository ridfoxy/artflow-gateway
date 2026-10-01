#!/usr/bin/env python3
"""UJI TERPISAH fungsi murni rakit_body_untuk().

Tanpa browser, tanpa jaringan, tanpa menyentuh produksi. Menguji semua cabang:
teks L/P, 1 gambar, 2 gambar, token kurang, flag mati, template kosong, prompt kosong.
"""
import json
import sys

sys.path.insert(0, "/root/api-google-flow")
import vids_runner as V

tpl_lan = V.load_aspect_template("landscape")
tpl_port = V.load_aspect_template("portrait")
T1 = "AVL_0q" + "A" * 120
T2 = "AVL_0q" + "B" * 120

lulus = gagal_n = 0


def cek(nama, benar, catatan=""):
    global lulus, gagal_n
    if benar:
        lulus += 1
        print(f"  OK   {nama} {catatan}")
    else:
        gagal_n += 1
        print(f"  GAGAL {nama} {catatan}")


def ambil(n, j):
    for k in j:
        n = n[k]
    return n


print("=== UJI 1: job TEKS ===")
for asp, harap in (("landscape", 1), ("portrait", 2)):
    b, g = V.rakit_body_untuk({"prompt": "uji " + asp, "aspect": asp}, tpl_lan, tpl_port)
    cek(f"teks {asp:9}", b is not None and g is None,
        f"| rasio {ambil(json.loads(b),[4,15,4]) if b else '-'} (harap {harap})")
    if b:
        cek(f"  rasio {asp:9}", ambil(json.loads(b), [4, 15, 4]) == harap)

print("\n=== UJI 2: job BERGAMBAR ===")
b, g = V.rakit_body_untuk(
    {"prompt": "1 gambar", "aspect": "landscape", "images": ["u1"]},
    tpl_lan, tpl_port, [T1])
cek("1 gambar landscape", b is not None and g is None, f"| {g or ''}")
if b:
    a = json.loads(b)
    cek("  [2][25] len 1", len(a[2][25]) == 1)
    cek("  rasio landscape", ambil(a, [4, 15, 4]) == 1)

b, g = V.rakit_body_untuk(
    {"prompt": "2 gambar", "aspect": "portrait", "images": ["u1", "u2"]},
    tpl_lan, tpl_port, [T1, T2])
cek("2 gambar portrait", b is not None and g is None, f"| {g or ''}")
if b:
    a = json.loads(b)
    cek("  [2][25] len 2", len(a[2][25]) == 2)
    cek("  rasio portrait", ambil(a, [4, 15, 4]) == 2)
    cek("  AVL1 benar", ambil(a, [2, 25, 0, 0, 7, 1, 11, 8, 3]) == T1)
    cek("  AVL2 benar", ambil(a, [2, 25, 1, 0, 7, 1, 11, 8, 3]) == T2)

print("\n=== UJI 3: jalur KEGAGALAN (harus turun ke urut, bukan error) ===")
b, g = V.rakit_body_untuk(
    {"prompt": "kurang token", "aspect": "landscape", "images": ["u1", "u2"]},
    tpl_lan, tpl_port, [T1])
cek("token kurang -> ditolak", b is None and g is not None, f"| {g}")

b, g = V.rakit_body_untuk(
    {"prompt": "tanpa token", "aspect": "landscape", "images": ["u1"]},
    tpl_lan, tpl_port, None)
cek("tanpa token -> ditolak", b is None and g is not None, f"| {g}")

b, g = V.rakit_body_untuk({"prompt": "x", "aspect": "landscape"}, None, None)
cek("template kosong -> ditolak", b is None and g is not None, f"| {g}")

print("\n=== UJI 4: flag mati (VIDS_IMG_PARALEL=0) ===")
V.IMG_XHR_PARALEL = False
b, g = V.rakit_body_untuk(
    {"prompt": "flag mati", "aspect": "landscape", "images": ["u1"]},
    tpl_lan, tpl_port, [T1])
cek("flag mati -> bergambar ditolak", b is None and g is not None, f"| {g}")
b, g = V.rakit_body_untuk({"prompt": "teks tetap", "aspect": "landscape"},
                          tpl_lan, tpl_port)
cek("flag mati -> teks tetap jalan", b is not None)
V.IMG_XHR_PARALEL = True

print("\n=== UJI 5: ketahanan masukan aneh ===")
for nama, info in (("prompt kosong", {"prompt": "", "aspect": "landscape"}),
                   ("aspect kosong", {"prompt": "x", "aspect": ""}),
                   ("images kosong", {"prompt": "x", "images": []}),
                   ("images None", {"prompt": "x", "images": None}),
                   ("tanpa aspect", {"prompt": "x"})):
    try:
        b, g = V.rakit_body_untuk(info, tpl_lan, tpl_port)
        cek(nama, (b is not None) or (g is not None), f"| {'body ok' if b else g}")
    except Exception as e:
        cek(nama, False, f"| MELEDAK: {type(e).__name__}: {e}")

print(f"\n=== HASIL: {lulus} lulus, {gagal_n} gagal ===")
sys.exit(1 if gagal_n else 0)