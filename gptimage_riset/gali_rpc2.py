#!/usr/bin/env python3
"""Ekstrak payload POST /backend-api/f/conversation (bukan prepare) + respons gambar."""
import json
from pathlib import Path

rek = json.loads(Path("/root/api-google-flow/output/rpc_gptimg.json").read_text())

print("=== SEMUA POST /backend-api/f/conversation (tepat) ===")
for e in rek:
    if e.get("arah") != "req":
        continue
    u = e["url"].split("?")[0]
    if u.endswith("/backend-api/f/conversation"):
        print(f"\n  {e['metode']} {u}")
        print("  --- kepala ---")
        for k, v in (e.get("kepala") or {}).items():
            print(f"    {k}: {v[:110]}")
        print("  --- ISI (dipotong 3000) ---")
        try:
            o = json.loads(e["isi"])
            print(json.dumps(o, indent=1, ensure_ascii=False)[:3000])
        except Exception:
            print("   ", (e.get("isi") or "")[:3000])

print("\n=== RESPONS file/gambar ===")
for e in rek:
    if e.get("arah") == "resp" and ("files/download" in e["url"] or "estuary" in e["url"]):
        print(f"  {e['status']}  {e['url'][:150]}")
        print(f"     tipe: {e.get('tipe','')[:70]}")

print("\n=== RESPONS conversation (SSE) ===")
for e in rek:
    if e.get("arah") == "resp" and "conversation" in e["url"]:
        print(f"  {e['status']}  {e['url'].split('chatgpt.com')[-1][:110]}  [{e.get('tipe','')[:40]}]")

print("\n=== URUTAN WAKTU (f/conversation → gambar) ===")
mulai = None
for e in rek:
    if e.get("arah") == "TANDA":
        mulai = e["t"]
        print(f"  {e['t']}  --- MULAI GENERATE ---")
        continue
    if mulai and e["t"] >= mulai - 1:
        arah = e.get("arah")
        if arah == "req":
            u = e["url"].split("chatgpt.com")[-1].split("?")[0]
            print(f"  +{e['t']-mulai:6.1f}s  REQ  {e.get('metode',''):5} {u[:95]}")
        elif arah == "resp" and e.get("status"):
            u = e["url"].split("chatgpt.com")[-1].split("?")[0]
            print(f"  +{e['t']-mulai:6.1f}s  RESP {e['status']} {u[:95]}")