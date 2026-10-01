#!/usr/bin/env python3
"""UJI PRODUKSI: gambar + PORTRAIT (1 & 2 gambar) lewat gateway.

Memverifikasi:
  - jalur mana yang dipakai (RPC atau UI) dari log runner
  - dimensi hasil (harus 720x1280 untuk portrait)
  - waktu selesai
"""
import json
import re
import subprocess
import time
from pathlib import Path

import requests

APP = Path("/root/api-google-flow")
KEY = re.search(r"gflow_live_[A-Za-z0-9]+", (APP / "app.py").read_text()).group(0)
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
GW = "https://api.artflowgen.com"
G1 = "http://172.104.60.106/download/img_856ac7db3061.jpg"
G2 = "http://172.104.60.106/download/img_b950ba9f1988.jpg"


def kirim(judul, aspect, images):
    print(f"\n=== {judul} ===")
    r = requests.post(f"{GW}/v1/vids/videos/create", headers=H,
                      json={"prompt": f"uji-portrait: {judul}",
                            "aspect": aspect, "images": images}, timeout=40)
    if r.status_code not in (200, 202):
        print(f"  GAGAL HTTP {r.status_code}: {r.text[:150]}")
        return None
    jid = r.json()["jobId"]
    t0 = time.time()
    print(f"  jobId {jid[:8]} | aspect={aspect} | {len(images)} gambar")
    st = None
    for _ in range(50):
        time.sleep(8)
        pj = requests.get(f"{GW}/v1/vids/jobs/{jid}", headers=H, timeout=25).json()
        st = pj.get("status")
        if st in ("completed", "failed"):
            break
    dt = time.time() - t0
    print(f"  -> {st} dalam {dt:.0f}s | akun={pj.get('account','-')}")
    if st != "completed":
        print(f"     error: {pj.get('error')}")
        return {"jid": jid, "st": st, "detik": round(dt), "dimensi": None}
    m = requests.get(f"{GW}/v1/vids/media/{jid}", headers=H, timeout=120)
    out = Path(f"/tmp/uji_prod_{judul.replace(' ', '_')}.mp4")
    out.write_bytes(m.content)
    rr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", str(out)],
                        capture_output=True, text=True)
    dim = rr.stdout.strip()
    print(f"     media {len(m.content):,} B | dimensi {dim} | {out.name}")
    return {"jid": jid, "st": st, "detik": round(dt), "dimensi": dim,
            "bytes": len(m.content)}


hasil = {}
hasil["portrait_1gambar"] = kirim("P1 portrait 1 gambar", "portrait", [G1])
hasil["portrait_2gambar"] = kirim("P2 portrait 2 gambar", "portrait", [G1, G2])
hasil["landscape_2gambar"] = kirim("L2 landscape 2 gambar", "landscape", [G1, G2])

print("\n=== RINGKASAN ===")
for k, v in hasil.items():
    if not v:
        continue
    ok = "720x1280" in str(v.get("dimensi"))
    if "landscape" in k:
        ok = "1280x720" in str(v.get("dimensi"))
    print(f"  {k:22} {v['st']:10} {v['detik']:>4}s  {str(v.get('dimensi')):10} "
          f"{'OK' if ok else 'PERIKSA'}")

print("\n=== jalur yang dipakai (log runner) ===")
log = subprocess.run(["journalctl", "-u", "artflow-vids-runner-local.service",
                      "--since", "6 minutes ago", "--no-pager"],
                     capture_output=True, text=True).stdout
for baris in log.splitlines():
    if any(k in baris for k in ("payload RPC", "ui_fallback", "jalur UI",
                                "tersimpan", "rasio")):
        print("  ", baris.split("]: ", 1)[-1][:140])

Path("/root/uji_portrait_produksi.json").write_text(json.dumps(hasil, indent=2))