#!/usr/bin/env python3
"""Perbaiki pesan menyesatkan 'kolom sandi tidak muncul'.

Sekarang: kenali halaman akun DIHAPUS / DITOLAK Google dan laporkan apa adanya,
supaya tidak terbaca seperti bug selektor.

Backup .bak-deleted-<ts>.
"""
import shutil
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/camoufox_login.py")
src = F.read_text()
ts = int(time.time())
shutil.copy2(F, F.with_suffix(".py.bak-deleted-" + str(ts)))
print("backup:", f"{F.name}.bak-deleted-{ts}")

NL = chr(10)

# 1. tambah penanda di daftar BAD
LAMA_BAD = '''BAD = [
    "kata sandi salah", "password salah", "wrong password",'''
BARU_BAD = '''DIHAPUS = [
    "akun dihapus", "account deleted", "deletedaccount",
    "tidak dapat memproses login", "couldn't process your sign in",
    "hubungi admin domain", "contact your domain administrator",
]
BAD = [
    "kata sandi salah", "password salah", "wrong password",'''

if "DIHAPUS = [" in src:
    print("  = penanda DIHAPUS sudah ada")
elif LAMA_BAD in src:
    src = src.replace(LAMA_BAD, BARU_BAD, 1)
    print("  + penanda DIHAPUS ditambahkan")
else:
    print("  !! anchor BAD tidak ketemu")

# 2. periksa sebelum melaporkan 'kolom sandi tidak muncul'
LAMA = ('                if not visible(page, \'input[type="password"]\'):' + NL +
        '                    log("  -> error: kolom sandi tidak muncul")')
BARU = ('                if not visible(page, \'input[type="password"]\'):' + NL +
        '                    # Jangan salahkan selektor: cek dulu apakah akun' + NL +
        '                    # DIHAPUS / ditolak Google (pesan lama menyesatkan).' + NL +
        '                    if "deletedaccount" in (page.url or "").lower():' + NL +
        '                        log("  -> deleted: akun DIHAPUS Google (tidak bisa dipulihkan sendiri)")' + NL +
        '                        return 4' + NL +
        '                    if "rejected" in (page.url or "").lower():' + NL +
        '                        log("  -> deleted: pemulihan DITOLAK Google — hubungi admin domain")' + NL +
        '                        return 4' + NL +
        '                    _hap = find(DIHAPUS, text_low(page))' + NL +
        '                    if _hap:' + NL +
        '                        log(f"  -> deleted: {_hap}")' + NL +
        '                        return 4' + NL +
        '                    log("  -> error: kolom sandi tidak muncul")')

if "-> deleted:" in src:
    print("  = deteksi deleted sudah ada")
elif src.count(LAMA) >= 1:
    src = src.replace(LAMA, BARU)
    print(f"  + deteksi deleted dipasang ({src.count('-> deleted:')} titik)")
else:
    print("  !! titik 'kolom sandi' tidak ketemu")

F.write_text(src)
r = subprocess.run(["/root/harbor/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])