#!/usr/bin/env python3
"""UJI PRODUKSI (langkah 4): kirim CAMPURAN job serentak lalu ukur.

6 job sekaligus:
  T1 teks landscape, T2 teks portrait
  P1 portrait 1 gambar, P2 portrait 2 gambar
  L1 landscape 1 gambar, L2 landscape 2 gambar

Yang diperiksa:
  - apakah job bergambar benar-benar masuk jalur paralel (log "paralel xN")
  - dimensi tiap hasil sesuai yang diminta
  - waktu selesai masing-masing
"""
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

APP = Path("/root/api-google-flow")
KEY = re.search(r"gflow_live_[A-Za-z0-9]+", (APP / "app.py").read_text()).group(0)
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
GW = "https://api.artflowgen.com"
G1 = "http://172.104.60.106/download/img_856ac7db3061.jpg"
G2 = "http://172.104.60.106/download/img_b950ba9f1988.jpg"

JOBS = [
    ("T1", "teks landscape", "landscape", []),
    ("T2", "teks portrait", "portrait", []),
    ("P1", "portrait 1 gambar", "portrait", [G1]),
    ("P2", "portrait 2 gambar", "portrait", [G1, G2]),
    ("L1", "landscape 1 gambar", "landscape", [G1]),
    ("L2", "landscape 2 gambar", "landscape", [G1, G2]),
]


def kirim(j):
    kode, nama, asp, img = j
    t0 = time.time()
    r = requests.post(f"{GW}/v1/vids/videos/create", headers=H,
                      json={"prompt": f"serentak-{kode}: {nama}",
                            "aspect": asp, "images": img}, timeout=40)
    if r.status_code not in (200, 202):
        return {"kode": kode, "gagal": f"HTTP {r.status_code}"}
    jid = r.json()["jobId"]
    # tunggu selesai
    pj = {}
    for _ in range(60):
        time.sleep(8)
        pj = requests.get(f"{GW}/v1/vids/jobs/{jid}", headers=H, timeout=25).json()
        if pj.get("status") in ("completed", "failed"):
            break
    dt = time.time() - t0
    out = {"kode": kode, "nama": nama, "aspect": asp, "gambar": len(img),
           "jid": jid, "status": pj.get("status"), "detik": round(dt),
           "akun": pj.get("account")}
    if pj.get("status") == "completed":
        m = requests.get(f"{GW}/v1/vids/media/{jid}", headers=H, timeout=120)
        p = Path(f"/tmp/serentak_{kode}.mp4")
        p.write_bytes(m.content)
        rr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=width,height", "-of",
                             "csv=p=0", str(p)], capture_output=True, text=True)
        out["dimensi"] = rr.stdout.strip()
        out["bytes"] = len(m.content)
    else:
        out["error"] = pj.get("error")
    return out


print("=== KIRIM 6 JOB SERENTAK ===")
t0 = time.time()
with ThreadPoolExecutor(max_workers=6) as ex:
    hasil = list(ex.map(kirim, JOBS))
total = time.time() - t0

print(f"\n=== HASIL (total {total:.0f}s untuk 6 job) ===")
for h in hasil:
    dim = h.get("dimensi") or ""
    w, _, hh = dim.partition(",")
    benar = ""
    if w and hh:
        w, hh = int(w), int(hh)
        harap_port = h["aspect"] == "portrait"
        benar = "OK" if ((hh > w) == harap_port) else "SALAH ARAH"
    print(f"  {h['kode']:3} {h['nama']:20} {h.get('status','?'):10} "
          f"{h.get('detik','?'):>4}s {dim:>10} {benar}")

print("\n=== jalur yang dipakai (log) ===")
log = subprocess.run(["journalctl", "-u", "artflow-vids-runner-local.service",
                      "--since", f"{int(total)+90} seconds ago", "--no-pager"],
                     capture_output=True, text=True).stdout
for b in log.splitlines():
    if any(k in b for k in ("paralel x", "paralel:", "-> jalur urut",
                            "paralel tersimpan", "paralel hasil")):
        print("  ", b.split("]: ", 1)[-1][:150])

Path("/root/uji_serentak.json").write_text(json.dumps(hasil, indent=2))
print("\nberkas: /root/uji_serentak.json")