#!/usr/bin/env python3
"""Uji API GPT Image di gateway end-to-end (create → poll → unduh PNG)."""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/root/bottele")
import config

BASE = "http://127.0.0.1:8088"
KUNCI = config.API_KEY
PROMPT = "kucing oranye lucu bermain di taman bunga matahari"


def panggil(jalur, data=None, metode=None):
    req = urllib.request.Request(
        BASE + jalur,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"X-API-Key": KUNCI, "Content-Type": "application/json"},
        method=metode or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status, json.loads(r.read())


print("=== 1. status akun ===")
st, ak = panggil("/v1/gptimage/accounts")
print(f"  HTTP {st} | total akun: {ak.get('total')}")
for a in (ak.get("akun") or [])[:5]:
    print(f"    {a['email']:26} {a['state']:12} sisa={a['sisa_kuota']} "
          f"dipakai={a['dipakai']}")

print("\n=== 2. buat job ===")
st, jb = panggil("/v1/gptimage/images/create", {"prompt": PROMPT})
print(f"  HTTP {st} | {jb}")
jid = jb.get("jobId")

print("\n=== 3. tunggu hasil ===")
akhir = None
for i in range(50):
    time.sleep(6)
    st, j = panggil(f"/v1/gptimage/jobs/{jid}")
    print(f"    t+{(i+1)*6:3}s status={j.get('status'):10} progress={j.get('progress')} "
          f"{j.get('error') or ''}")
    if j.get("status") in ("completed", "failed", "limited"):
        akhir = j
        break

print("\n=== 4. hasil ===")
print(json.dumps(akhir, indent=1, ensure_ascii=False))

if akhir and akhir.get("status") == "completed":
    url = BASE + akhir["download_url"]
    req = urllib.request.Request(url, headers={"X-API-Key": KUNCI})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    f = "/root/api-google-flow/output/gptimage/uji_api.png"
    open(f, "wb").write(data)
    print(f"  unduh: {len(data)} byte | magic {data[:4]} | {f}")
    print("  ✅ API GPT IMAGE BERFUNGSI")
else:
    print("  ❌ job tidak selesai")

print("\n=== 5. ringkasan job ===")
st, r = panggil("/v1/gptimage/jobs")
print(f"  total={r.get('total')} ringkasan={r.get('summary')}")