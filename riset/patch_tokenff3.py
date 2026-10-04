#!/usr/bin/env python3
"""Sisipkan pasang_pengintai_token(page) SETELAH setiap add_init_script(HOOK_JS).

Bug cek sebelumnya: 'pasang_pengintai_token(page)' cocok juga dengan baris
`def pasang_pengintai_token(page):`, jadi pengecekan mengira sudah terpasang.
Di sini penyisipan berbasis baris sehingga pasti tepat.
"""
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/vids_runner.py")
baris = F.read_text().splitlines(keepends=True)
bak = F.with_suffix(".py.bak-tokenff3-" + str(int(time.time())))
bak.write_text("".join(baris))
print("backup:", bak.name)

KUNCI = "await page.add_init_script(HOOK_JS)"
SISIP = ("        pasang_pengintai_token(page)   "
         "# Camoufox: hook JS tidak jalan, pakai event Playwright\n")

keluar = []
n = 0
for b in baris:
    keluar.append(b)
    if KUNCI in b and "init_script" in b:
        # hindari sisip ganda
        keluar.append(SISIP)
        n += 1
print("titik disisipkan:", n)

F.write_text("".join(keluar))
r = subprocess.run(["/root/geminiomni/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])

r2 = subprocess.run(["grep", "-n", "pasang_pengintai_token", str(F)],
                    capture_output=True, text=True)
print("rujukan sekarang:")
print(r2.stdout)