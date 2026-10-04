#!/usr/bin/env python3
"""Analisa: per akun berhasil generate berapa sampai kena limit? (read-only)"""
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

DB = "/root/api-google-flow/data/vids_jobs.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

def fmt(t):
    try:
        return datetime.fromtimestamp(int(t), timezone.utc).strftime("%d %b %H:%M")
    except Exception:
        return "-"

print("=== A. TOTAL PER STATUS ===")
for s, n in c.execute("SELECT status, COUNT(*) FROM vids_jobs GROUP BY status"):
    print(f"  {s:12} {n}")

print("\n=== B. RIWAYAT AKUN @gumdel (batch sekarang) ===")
rows = c.execute("""SELECT account, status, COUNT(*) FROM vids_jobs
                    WHERE account LIKE '%gumdel.com' GROUP BY account, status
                    ORDER BY account""").fetchall()
per = defaultdict(dict)
for acc, st, n in rows:
    per[acc][st] = n

print(f"  {'akun':24} {'sukses':>7} {'gagal':>6} {'total':>6}")
sukses_list = []
for acc in sorted(per, key=lambda a: -(per[a].get("completed", 0))):
    d = per[acc]
    ok = d.get("completed", 0)
    gg = sum(v for k, v in d.items() if k != "completed")
    sukses_list.append(ok)
    print(f"  {acc:24} {ok:>7} {gg:>6} {ok+gg:>6}")

print("\n=== C. RINGKASAN ===")
if sukses_list:
    import statistics as st
    print(f"  akun terpakai     : {len(sukses_list)}")
    print(f"  total sukses      : {sum(sukses_list)}")
    print(f"  rata-rata/akun    : {st.mean(sukses_list):.1f}")
    print(f"  median            : {st.median(sukses_list):.0f}")
    print(f"  min / maks        : {min(sukses_list)} / {max(sukses_list)}")
    cek = Counter(sukses_list)
    print("  sebaran:", dict(sorted(cek.items())))

print("\n=== D. ERROR TERATAS (penyebab berhenti) ===")
for e, n in c.execute("""SELECT substr(error,1,52), COUNT(*) FROM vids_jobs
                         WHERE status='failed' AND error IS NOT NULL
                         GROUP BY substr(error,1,52) ORDER BY COUNT(*) DESC LIMIT 10"""):
    print(f"  {n:5}  {e}")

print("\n=== E. BATAS KODE ===")
import os
print("  VIDS_SESSION_JOBS (kode) :", os.environ.get("VIDS_SESSION_JOBS", "12 (bawaan)"))
print("  akun dengan >= 12 sukses :", sum(1 for s in sukses_list if s >= 12))