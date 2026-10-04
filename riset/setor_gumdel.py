#!/usr/bin/env python3
"""Setor 65 akun @gumdel.com yang baru ke pool, dan nonaktifkan batch mati.

- mode REPLACE: batch @rallse.com sudah DIHAPUS Google, jadi tidak ada gunanya
  dipertahankan di accounts.txt. Endpoint impor membuat cadangan otomatis.
- akun mati ditandai is_active=0 di DB supaya runner tidak mencobanya.
"""
import json
import subprocess
import urllib.request

KEY = "GATEWAY_API_KEY_REDACTED"
BASE = "http://127.0.0.1:8088"
SANDI = "PASSWORD_REDACTED"

EMAILS = """fverdj dpoiku mninjl unfqyr khvxiy qyruoq gthtbs ccqetc hwiooj yknfen
lhrfla rzighe ymuluw qbdtme llchuw wwrbyy yiqvkn nmumiv mxamsl myryuo
lfwrrr szcipj ntxeqv dfgagw hmwhxu eiatee tgcprc wqgvhc vvfayv zsprnp
aczpbu yhalve cmyaeg pnvcqk xfiwxq kpsjfj kuxfek tdagwi rqjxqj dragcp
sjftpv cpakpo izhdqk flmksb celsqc jvistz zvjtny xsarri bhwsfo eaemhl
iomctx asyoxe koerow pkdlfa bvepjz isdjhq oajvzc lsddth fhwjgw wtbqrm
ezsvja cfnzrs dubeys czckgc vvkpcg""".split()

print(f"email dibaca: {len(EMAILS)}")
assert len(EMAILS) == 65, f"harus 65, dapat {len(EMAILS)}"

entri = [{"email": f"{e}@gumdel.com", "password": SANDI} for e in EMAILS]
body = json.dumps({"entries": entri, "mode": "replace"}).encode()
req = urllib.request.Request(
    f"{BASE}/v1/vids/accounts/import", data=body, method="POST",
    headers={"X-API-Key": KEY, "Content-Type": "application/json",
             "Authorization": f"Bearer {KEY}"})
with urllib.request.urlopen(req, timeout=60) as r:
    res = json.loads(r.read())
print("\n=== hasil impor ===")
for k in ("status", "mode", "written", "added", "total_in_file", "path"):
    print(f"  {k:14}: {res.get(k)}")
if res.get("skipped"):
    print("  dilewati:", res["skipped"][:5])

print("\n=== isi accounts.txt ===")
r2 = subprocess.run(["bash", "-lc",
                     "wc -l < /root/api-google-flow/accounts.txt; "
                     "grep -c gumdel /root/api-google-flow/accounts.txt; "
                     "grep -c rallse /root/api-google-flow/accounts.txt"],
                    capture_output=True, text=True)
baris = r2.stdout.split()
print(f"  total baris : {baris[0]}")
print(f"  gumdel      : {baris[1]}")
print(f"  rallse      : {baris[2]}")

print("\n=== nonaktifkan batch @rallse yang mati ===")
r3 = subprocess.run(["bash", "-lc",
                     "sqlite3 /root/api-google-flow/data/gateway.db "
                     "\"UPDATE accounts SET is_active=0, status='deleted_by_google' "
                     "WHERE email LIKE '%@rallse.com'; SELECT status, COUNT(*) "
                     "FROM accounts GROUP BY status\""],
                    capture_output=True, text=True)
print((r3.stdout or r3.stderr).strip())