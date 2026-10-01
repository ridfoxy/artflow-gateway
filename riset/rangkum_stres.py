#!/usr/bin/env python3
"""Rangkum hasil stress test (data nyata dari DB)."""
import sqlite3
from collections import Counter

c = sqlite3.connect("/root/api-google-flow/data/vids_jobs.db")
c.row_factory = sqlite3.Row

rows = c.execute("""SELECT job_id, status, account, created_at, updated_at
                    FROM vids_jobs WHERE prompt LIKE 'stres-akun-%'
                    ORDER BY created_at""").fetchall()
print(f"job stres-akun: {len(rows)}")
print("status:", dict(Counter(r["status"] for r in rows)))

ok = [r for r in rows if r["status"] == "completed"]
gagal = [r for r in rows if r["status"] == "failed"]
print(f"\nBERHASIL: {len(ok)} | gagal: {len(gagal)}")
if ok:
    t = sorted(r["updated_at"] - r["created_at"] for r in ok)
    print(f"waktu/video: rata {sum(t)/len(t):.1f}s "
          f"(median {t[len(t)//2]}, min {t[0]}, max {t[-1]})")

# sebaran per akun
per = Counter(r["account"] for r in rows if r["account"])
print(f"\npakai {len(per)} akun berbeda:")
for a, n in per.most_common():
    okn = sum(1 for r in rows if r["account"] == a and r["status"] == "completed")
    print(f"  {a:26} {n} job ({okn} ok)")

# rentang waktu uji
if rows:
    import time
    t0, t1 = rows[0]["created_at"], max(r["updated_at"] for r in rows)
    print(f"\nrentang: {time.strftime('%H:%M:%S', time.localtime(t0))} - "
          f"{time.strftime('%H:%M:%S', time.localtime(t1))} "
          f"({(t1-t0)/60:.1f} menit)")
    print(f"laju   : {len(ok)/((t1-t0)/3600):.0f} video/jam")
c.close()