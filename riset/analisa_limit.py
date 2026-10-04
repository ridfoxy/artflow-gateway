#!/usr/bin/env python3
"""Pisahkan: akun berhenti karena BATAS KODE (12/sesi) atau KUOTA Google (429)?"""
import sqlite3
from collections import defaultdict

DB = "/root/api-google-flow/data/vids_jobs.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

per = defaultdict(lambda: {"ok": 0, "429": 0, "lain": 0, "total": 0})
for acc, st, err in c.execute(
        "SELECT account, status, error FROM vids_jobs "
        "WHERE account IS NOT NULL AND account != ''"):
    d = per[acc]
    d["total"] += 1
    if st == "completed":
        d["ok"] += 1
    elif err and "429" in str(err):
        d["429"] += 1
    else:
        d["lain"] += 1

tepat12 = [a for a, d in per.items() if d["ok"] == 12]
kena429 = [a for a, d in per.items() if d["429"] > 0]
punya_ok = [a for a, d in per.items() if d["ok"] > 0]

print("=== BERHENTI KARENA APA ===")
print(f"  akun dipakai total            : {len(per)}")
print(f"  akun yang pernah sukses       : {len(punya_ok)}")
print(f"  akun sukses TEPAT 12 (=batas kode): {len(tepat12)}")
print(f"  akun kena 429 (kuota Google)  : {len(kena429)}")

print("\n=== 10 AKUN DENGAN SUKSES TERBANYAK ===")
print(f"  {'akun':24} {'sukses':>6} {'429':>5} {'lain':>5}")
for a in sorted(per, key=lambda x: -per[x]["ok"])[:10]:
    d = per[a]
    print(f"  {a:24} {d['ok']:>6} {d['429']:>5} {d['lain']:>5}")

print("\n=== SEBARAN TITIK BERHENTI ===")
from collections import Counter
akhir = Counter(d["ok"] for d in per.values() if d["ok"] > 0)
print("  (jumlah sukses -> berapa akun)")
for k in sorted(akhir):
    if k >= 6 or akhir[k] >= 5:
        print(f"    {k:3} sukses : {akhir[k]:3} akun")

print("\n=== KESIMPULAN ANGKA ===")
ok_list = [d["ok"] for d in per.values() if d["ok"] > 0]
ok_list.sort()
import statistics as st
print(f"  rata-rata : {st.mean(ok_list):.1f} video/akun")
print(f"  median    : {st.median(ok_list):.0f} video/akun")
print(f"  puncak    : {max(ok_list)} video (akun dipakai berulang antar-sesi)")
print(f"  batas kode: 12 video per SESI (VIDS_SESSION_JOBS)")
print(f"  kuota     : 429 Google = {sum(d['429'] for d in per.values())} kegagalan")