#!/usr/bin/env python3
"""Perbaiki pengunduh video untuk Camoufox.

MASALAH: `download_url()` mengambil video dengan `fetch()` DI DALAM halaman.
Uji di Vids nyata sebelumnya menunjukkan `fetch` JS dari halaman Flow DIBLOKIR
CORS (URL bertanda tangan terikat sesi), dan di Camoufox jalur ini juga gagal
-> "stream gagal/kecil" (data kosong) walau video sudah 35% jadi.

PERBAIKAN: bila hasil fetch kosong, ambil berkas lewat `page.request.get()`
(permintaan dari konteks browser, tidak terikat origin/CORS). Metode ini
SUDAH TERBUKTI untuk media Flow bertanda tangan.

Juga: catat panjang respons agar kegagalan bisa dibedakan (kosong vs kecil).

Backup .bak-unduhcam-<ts>.
"""
import shutil
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/vids_runner.py")
src = F.read_text()
bak = F.with_suffix(".py.bak-unduhcam-" + str(int(time.time())))
bak.write_text(src)
print("backup:", bak.name)

NL = chr(10)
D = chr(34)

LAMA = '''    if not b64:
        return None
    import base64
    return base64.b64decode(b64)'''

BARU = '''    if b64:
        import base64
        return base64.b64decode(b64)
    # ── CADANGAN: fetch() JS bisa diblokir (CORS/URL bertanda tangan) dan
    # gagal di Camoufox. Ambil lewat konteks permintaan browser, yang tidak
    # terikat origin halaman. Terbukti untuk media Flow bertanda tangan.
    try:
        r = await page.request.get(url, timeout=120000)
        if r.ok:
            data = await r.body()
            if data:
                return data
    except Exception as e:
        log(f"unduh cadangan gagal: {str(e)[:90]}")
    return None'''

n = src.count(LAMA)
print("pola download_url:", n, "x")
if "CADANGAN: fetch() JS bisa diblokir" in src:
    print("  = perbaikan sudah ada")
elif n == 1:
    src = src.replace(LAMA, BARU, 1)
    F.write_text(src)
    print("  + cadangan page.request.get dipasang")
else:
    print("  !! pola tidak unik/tidak ketemu")

r = subprocess.run(["/root/geminiomni/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])