#!/usr/bin/env python3
"""Gali isi payload endpoint kunci → cetak untuk dokumentasi zero-UI."""
import json
from pathlib import Path

rek = json.loads(Path("/root/api-google-flow/output/rpc_gptimg.json").read_text())

target = ["sentinel/chat-requirements/prepare", "sentinel/chat-requirements/finalize",
          "conversation/init", "f/conversation/prepare", "/backend-api/f/conversation",
          "files/download", "estuary/content"]

for t in target:
    print(f"\n{'='*70}\n### {t}\n{'='*70}")
    n = 0
    for e in rek:
        if e.get("arah") != "req" or t not in e.get("url", ""):
            continue
        n += 1
        if n > 1:
            continue
        print(f"  {e['metode']} {e['url'][:150]}")
        print("  --- kepala ---")
        for k, v in (e.get("kepala") or {}).items():
            print(f"    {k}: {v[:110]}")
        if e.get("isi"):
            print("  --- isi ---")
            try:
                o = json.loads(e["isi"])
                print(json.dumps(o, indent=2, ensure_ascii=False)[:2200])
            except Exception:
                print("   ", e["isi"][:1500])
        else:
            print("  --- isi: (tidak ada) ---")
    if n == 0:
        print("  (tidak terekam)")

# cari respon estuary + files/download
print(f"\n{'='*70}\n### RESPON GAMBAR\n{'='*70}")
for e in rek:
    if e.get("arah") == "resp" and ("estuary" in e["url"] or "files/download" in e["url"]):
        print(f"  {e['status']} {e['url'][:140]} [{e.get('tipe','')[:50]}]")