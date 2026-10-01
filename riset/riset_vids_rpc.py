#!/usr/bin/env python3
"""RISET (read-only): dapatkan template RPC Vids lengkap + token segar.

Tidak mengubah kode. Hanya membaca template yang tersimpan dan memanen token
dari sesi akun sehat untuk dipakai uji tembak-langsung.
"""
import base64
import json
import re
import sys
from pathlib import Path

APP = Path("/root/api-google-flow")
sys.path.insert(0, str(APP))

print("=== 1. TEMPLATE TERSIMPAN ===")
for f in sorted((APP / "data").glob("*template*.json")):
    d = json.loads(f.read_text())
    b = d.get("body") or ""
    print(f"\n  {f.name} ({len(b)} char body)")
    print(f"    url     : {d.get('url')}")
    print(f"    verified: {d.get('verified')}")
    print(f"    headers : {list((d.get('headers') or {}).keys())}")
    # cari penanda prompt
    for tanda in ("warmup", "capture payload", "prompt", "1lKq0GzSTBzl"):
        if tanda in b:
            print(f"    penanda : ada '{tanda}'")

print("\n=== 2. struktur protobuf body (dibedah) ===")
d = json.loads((APP / "data" / "portrait_template.json").read_text())
try:
    arr = json.loads(d["body"])
    def telusuri(node, path="", kedalaman=0):
        if kedalaman > 6:
            return
        if isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str) and len(v) > 20 and "/" not in v[:8]:
                    print(f"    {path}[{i}] = str({len(v)}) {v[:60]}")
                elif isinstance(v, (list, dict)):
                    telusuri(v, f"{path}[{i}]", kedalaman + 1)
        elif isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and len(v) > 20:
                    print(f"    {path}.{k} = str({len(v)}) {v[:60]}")
                elif isinstance(v, (list, dict)):
                    telusuri(v, f"{path}.{k}", kedalaman + 1)
    telusuri(arr)
except Exception as e:
    print(f"    gagal parse: {e}")

print("\n=== 3. fungsi generate Vids di produksi (untuk ditiru) ===")
src = (APP / "vids_runner.py").read_text()
i = src.find("async def xhr_post(")
print(src[i:i + 900] if i > 0 else "  xhr_post tidak ditemukan")
