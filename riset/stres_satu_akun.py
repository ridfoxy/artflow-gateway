#!/usr/bin/env python3
"""STRESS TEST SATU AKUN: berapa banyak video bisa dihasilkan.

Akun dikunci lewat field `account` di /v1/vids/videos/create, jadi semua job
jatuh ke akun itu. Diukur:
  - berapa job BERHASIL sebelum akun kena batas
  - waktu per video (kirim->siap)
  - apakah muncul "You've reached your monthly limit" (batas bulanan)
  - berapa yang gagal dan kenapa

Pakai pola UI (background=true + poll) supaya tidak kena 504 Cloudflare.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

GW = "https://api.artflowgen.com"
KEY = re.search(r"gflow_live_[A-Za-z0-9]+",
                Path("/root/api-google-flow/app.py").read_text()).group(0)
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

AKUN = sys.argv[1] if len(sys.argv) > 1 else None
MAKS = int(sys.argv[2]) if len(sys.argv) > 2 else 25

if not AKUN:
    d = requests.get(f"{GW}/v1/vids/accounts", headers=H, timeout=25).json()
    ok = [a["email"] for a in d.get("accounts", []) if a.get("state") == "ok"]
    AKUN = ok[0] if ok else None
if not AKUN:
    print("tidak ada akun sehat")
    raise SystemExit(1)

print(f"=== STRESS SATU AKUN: {AKUN} (maks {MAKS} job) ===", flush=True)
print(f"mulai: {time.strftime('%H:%M:%S')}", flush=True)

hasil = []
t_mulai = time.time()
for i in range(1, MAKS + 1):
    body = {"prompt": f"stres-akun-{i}: pemandangan alam indah sinematik",
            "aspect": "landscape", "account": AKUN}
    t0 = time.time()
    try:
        r = requests.post(f"{GW}/v1/vids/videos/create", headers=H,
                          json=body, timeout=40)
    except Exception as e:
        hasil.append({"i": i, "ok": False, "err": f"kirim: {e!r}"[:80]})
        print(f"  {i:2}. kirim gagal: {e!r}", flush=True)
        break

    if r.status_code not in (200, 202):
        pesan = r.text[:160]
        print(f"  {i:2}. DITOLAK HTTP {r.status_code}: {pesan}", flush=True)
        hasil.append({"i": i, "ok": False, "http": r.status_code, "pesan": pesan})
        if "limit" in pesan.lower():
            print("  -> BATAS KUOTA tercapai", flush=True)
        break

    jid = r.json().get("jobId") or r.json().get("job_id")
    # tunggu selesai (poll)
    st = None
    for n in range(60):
        time.sleep(6)
        try:
            pj = requests.get(f"{GW}/v1/vids/jobs/{jid}", headers=H, timeout=25).json()
        except Exception:
            continue
        st = pj.get("status")
        if st in ("completed", "failed"):
            break
    dt = time.time() - t0
    akun_dipakai = pj.get("account") or "-"
    err = str(pj.get("error") or "")[:70]
    hasil.append({"i": i, "ok": st == "completed", "jid": jid,
                  "t": round(dt, 1), "akun": akun_dipakai, "err": err})
    print(f"  {i:2}. {st:10} {dt:6.1f}s akun={akun_dipakai} {err}", flush=True)

    if st == "failed":
        if "limit" in err.lower() or "kuota" in err.lower():
            print("  -> akun kehabisan kuota; berhenti", flush=True)
            break
        # kegagalan lain: lanjut tapi catat
    if time.time() - t_mulai > 3000:
        print("  -> batas waktu uji 50 menit tercapai", flush=True)
        break

t_total = time.time() - t_mulai
baik = [h for h in hasil if h.get("ok")]
gagal = [h for h in hasil if h.get("ok") is False]

print(f"\n=== HASIL AKHIR ({time.strftime('%H:%M:%S')}) ===")
print(f"  akun          : {AKUN}")
print(f"  dicoba        : {len(hasil)}")
print(f"  BERHASIL      : {len(baik)}")
print(f"  gagal         : {len(gagal)}")
print(f"  total waktu   : {t_total/60:.1f} menit")
if baik:
    t = [h["t"] for h in baik]
    print(f"  waktu/video   : rata {sum(t)/len(t):.1f}s "
          f"(min {min(t):.1f}, max {max(t):.1f})")
    print(f"  laju          : {len(baik)/(t_total/3600):.0f} video/jam")
if gagal:
    print("  contoh gagal  :")
    for g in gagal[:3]:
        print(f"    {g.get('err') or g.get('pesan') or g.get('http')}")

Path("/root/stres_akun.json").write_text(json.dumps(
    {"akun": AKUN, "dicoba": len(hasil), "berhasil": len(baik),
     "gagal": len(gagal), "menit": round(t_total/60, 1),
     "waktu_rata": round(sum(h["t"] for h in baik)/len(baik), 1) if baik else None,
     "detail": hasil}, indent=2), encoding="utf-8")
print("  ditulis: /root/stres_akun.json")