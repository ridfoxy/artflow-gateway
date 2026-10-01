#!/usr/bin/env python3
"""UJI STABILITAS: 3 job berturut-turut, pastikan perbaikan konsisten.

Yang diperiksa:
  - berapa yang completed (harusnya 3/3)
  - waktu tiap job
  - ukuran & tipe berkas
  - apakah berkasnya gambar nyata (magic bytes)
"""
import json
import re
import time
from pathlib import Path

import requests

APP = Path("/root/api-google-flow")
KEY = re.search(r"gflow_live_[A-Za-z0-9]+", (APP / "app.py").read_text()).group(0)
BASE = "https://api.artflowgen.com"
H = {"X-API-Key": KEY}

PROMPTS = [
    ("s1", "kucing oranye lucu di jendela"),
    ("s2", "pemandangan gunung saat matahari terbit"),
    ("s3", "robot kecil lucu memegang bunga"),
]

hasil = []
for kode, prompt in PROMPTS:
    print(f"\n=== {kode}: {prompt} ===")
    try:
        r = requests.post(f"{BASE}/v1/google-flow/images/create", headers=H,
                          json={"prompt": f"{prompt}", "aspect": "landscape"},
                          timeout=40)
        d = r.json()
        jid = d.get("jobId")
        print(f"  jobId {str(jid)[:8]} | akun {d.get('account')}")
    except Exception as e:
        print(f"  gagal kirim: {str(e)[:90]}")
        hasil.append({"kode": kode, "status": "kirim_gagal"})
        continue

    t0 = time.time()
    j = {}
    for i in range(30):
        time.sleep(8)
        try:
            j = requests.get(f"{BASE}/v1/google-flow/jobs/{jid}",
                             headers=H, timeout=25).json()
        except Exception:
            continue
        st = j.get("status")
        if st in ("completed", "failed"):
            break
    dt = time.time() - t0
    st = j.get("status")
    print(f"  -> {st} dalam {dt:.0f}s ({j.get('progress')}%)")

    rec = {"kode": kode, "status": st, "detik": round(dt), "jid": jid}
    if st == "completed" and j.get("result"):
        try:
            rr = json.loads(j["result"]) if isinstance(j["result"], str) else j["result"]
            p = Path(rr.get("file_path") or "")
            rec.update({"file": p.name, "bytes": rr.get("size_bytes"),
                        "tipe": rr.get("content_type")})
            # verifikasi magic bytes
            if p.exists():
                raw = p.read_bytes()[:12]
                rec["magic"] = raw[:4].hex()
                rec["jpeg"] = raw[:2] == b"\xff\xd8"
                print(f"  BERKAS: {p.name} | {rr.get('size_bytes')} B | "
                      f"{rr.get('content_type')} | JPEG={rec['jpeg']}")
            else:
                print(f"  !! berkas tidak ada: {p}")
        except Exception as e:
            print(f"  result tidak terbaca: {str(e)[:80]}")
    else:
        rec["error"] = str(j.get("error"))[:120]
        print(f"  error: {rec['error'][:100]}")
    hasil.append(rec)

print("\n" + "=" * 50)
print("=== RINGKASAN STABILITAS ===")
ok = [h for h in hasil if h.get("status") == "completed"]
print(f"  dicoba   : {len(hasil)}")
print(f"  BERHASIL : {len(ok)}")
print(f"  gagal    : {len(hasil) - len(ok)}")
for h in hasil:
    tanda = "OK " if h.get("status") == "completed" else "GAGAL"
    print(f"  {tanda} {h['kode']}: {h.get('status')} {h.get('detik','-')}s "
          f"{h.get('bytes','')} B {h.get('tipe','')}")
if ok:
    dt = [h["detik"] for h in ok]
    print(f"\n  waktu rata-rata: {sum(dt)/len(dt):.0f}s (min {min(dt)}, max {max(dt)})")
    semua_jpeg = all(h.get("jpeg") for h in ok)
    print(f"  semua JPEG sah : {semua_jpeg}")
print(f"\n  VERDICT: {'STABIL ✓' if len(ok) == len(hasil) else 'PERLU DIPERIKSA'}")
Path("/root/uji_stabil.json").write_text(json.dumps(hasil, indent=2), encoding="utf-8")