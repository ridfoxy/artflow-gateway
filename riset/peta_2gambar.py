#!/usr/bin/env python3
"""Petakan payload 2-GAMBAR: di mana tepatnya masing-masing gambar."""
import json
from pathlib import Path

D = Path("/root/api-google-flow/data")


def muat(nama):
    d = json.loads((D / nama).read_text())
    return json.loads(d["body"])


dua = muat("with_2images_payload_REAL.json")
satu = muat("with_image_payload.json")
teks = muat("portrait_template.json")


def ambil(n, jalur):
    for k in jalur:
        if n is None:
            return None
        try:
            n = n[k]
        except Exception:
            return None
    return n


print("=== UKURAN & PENANDA ===")
for nama, d in (("teks", teks), ("1 gambar", satu), ("2 gambar", dua)):
    w = ambil(d, [2, 25])
    print(f"  {nama:9} [0]={d[0]:4}  [2][25]={'ADA len=%d' % len(w) if isinstance(w, list) else 'NULL'}"
          f"  len(body)={len(json.dumps(d))}")

print("\n=== [2][25] PADA PAYLOAD 2 GAMBAR (penampung gambar) ===")
w = ambil(dua, [2, 25])
print(f"  len = {len(w)}")
for i, item in enumerate(w):
    print(f"\n  --- gambar ke-{i+1} (indeks [2][25][{i}]) ---")
    print(f"  {json.dumps(item)[:400]}")

print("\n=== DI MANA TOKEN AVL BERADA? ===")
def cari(n, target, p="", hasil=None):
    if hasil is None:
        hasil = []
    if isinstance(n, str):
        if target in n:
            hasil.append((p, n))
    elif isinstance(n, list):
        for i, v in enumerate(n):
            cari(v, target, f"{p}[{i}]", hasil)
    elif isinstance(n, dict):
        for k, v in n.items():
            cari(v, target, f"{p}.{k}", hasil)
    return hasil

for i, tok in enumerate(("AVL_",)):
    for nm, d in (("2 gambar", dua),):
        print(f"\n  {nm}:")
        for p, v in cari(d, "AVL_")[:4]:
            print(f"    {p}")
            print(f"      {v[:70]}")

print("\n=== DIFF: 1 gambar vs 2 gambar ===")
def beda(a, b, p="", hasil=None, batas=300):
    if hasil is None:
        hasil = []
    if len(hasil) > batas:
        return hasil
    if type(a) is not type(b):
        hasil.append((p, f"tipe {type(a).__name__} vs {type(b).__name__}", a, b))
        return hasil
    if isinstance(a, list):
        if len(a) != len(b):
            hasil.append((p, f"PANJANG {len(a)} vs {len(b)}", None, None))
        for i in range(min(len(a), len(b))):
            beda(a[i], b[i], f"{p}[{i}]", hasil, batas)
    elif isinstance(a, dict):
        for k in set(a) | set(b):
            beda(a.get(k), b.get(k), f"{p}.{k}", hasil, batas)
    elif a != b:
        hasil.append((p, "nilai beda", str(a)[:60], str(b)[:60]))
    return hasil

b = beda(satu, dua)
print(f"  {len(b)} perbedaan:")
for p, ket, x, y in b[:16]:
    print(f"  {p or '(akar)':40} {ket}")
    if ket == "nilai beda":
        print(f"       1 gambar: {x}")
        print(f"       2 gambar: {y}")