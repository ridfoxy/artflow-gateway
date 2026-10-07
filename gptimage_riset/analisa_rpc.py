#!/usr/bin/env python3
"""Analisis rekaman lalu lintas → rangkum jalur RPC GPT Image (zero-UI)."""
import json
from collections import Counter
from pathlib import Path

rek = json.loads(Path("/root/api-google-flow/output/rpc_gptimg.json").read_text())

print("=== 1. ENDPOINT UNIK (req) ===")
c = Counter()
for e in rek:
    if e.get("arah") != "req":
        continue
    u = e["url"]
    jalur = u.split("chatgpt.com")[-1] if "chatgpt.com" in u else u
    jalur = jalur.split("?")[0]
    c[f"{e['metode']} {jalur}"] += 1
for k, v in c.most_common(40):
    print(f"  {v:4}x  {k}")

print("\n=== 2. ENDPOINT YANG MENARIK (kata kunci) ===")
kunci = ["conversation", "conduit", "sentinel", "backend-api/codex", "responses",
         "prepare", "requirements", "estuary", "image", "file"]
for e in rek:
    if e.get("arah") != "req":
        continue
    u = e["url"]
    if not any(k in u for k in kunci):
        continue
    jalur = u.split("chatgpt.com")[-1].split("?")[0] if "chatgpt.com" in u else u
    print(f"  {e['metode']:5} {jalur[:105]}")

print("\n=== 3. KEPALA TERPENTING (tanda tangan) ===")
for e in rek:
    if e.get("arah") != "req":
        continue
    if "conversation" in e["url"] or "conduit" in e["url"]:
        print(f"\n  >> {e['metode']} {e['url'][:130]}")
        for k, v in (e.get("kepala") or {}).items():
            print(f"     {k}: {v[:100]}")
        if e.get("isi"):
            print(f"     ISI: {e['isi'][:1200]}")

print("\n=== 4. RESPONSE PENTING ===")
for e in rek:
    if e.get("arah") != "resp":
        continue
    u = e["url"]
    if any(k in u for k in ("conversation", "conduit", "sentinel", "requirements")):
        print(f"  {e['status']} {u.split('chatgpt.com')[-1].split('?')[0][:100]} [{e.get('tipe','')[:40]}]")