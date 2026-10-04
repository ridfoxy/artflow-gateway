#!/usr/bin/env python3
"""Tangani halaman PEMILIH AKUN di camoufox_login.py (dijalankan di VPS baru)."""
import shutil
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/camoufox_login.py")
src = F.read_text()
ts = int(time.time())
shutil.copy2(F, F.with_suffix(".py.bak-chooser-" + str(ts)))
print("backup:", f"{F.name}.bak-chooser-{ts}")

FUNGSI = '''

def tangani_pemilih_akun(page, log_fn=None):
    """Keluar dari halaman 'Choose an account' -> kolom email.

    Google menampilkan pemilih akun kalau profil masih membawa cookie akun
    lama/kedaluwarsa. Klik "Use another account" (atau padanannya), lalu
    tunggu kolom email muncul. -> True kalau pemilih berhasil dilewati.
    """
    log_fn = log_fn or log
    try:
        if "accountchooser" not in (page.url or "").lower():
            return False
        log_fn("    terdeteksi pemilih akun — keluar ke kolom email")
        teks = [
            "Use another account", "Gunakan akun lain", "Add another account",
            "Tambahkan akun lain", "Use a different account",
        ]
        for t in teks:
            try:
                el = page.query_selector('text="%s"' % t)
                if el and el.is_visible():
                    el.click()
                    log_fn("    diklik: " + t)
                    page.wait_for_timeout(4000)
                    return True
            except Exception:
                continue
        try:
            page.evaluate("""() => {
                const k = Array.from(document.querySelectorAll('li, div[role="link"], div[role="button"]'));
                const t = k.find(e => /another account|akun lain|akun lainnya/i.test(e.innerText||''));
                if (t) t.click();
            }""")
            log_fn("    diklik lewat DOM (akun lain)")
            page.wait_for_timeout(4000)
            return True
        except Exception:
            pass
    except Exception as e:
        log_fn("    ! gagal lewati pemilih akun: " + str(e)[:80])
    return False


'''

anchor = "\ndef load_account(email):"
if "def tangani_pemilih_akun" in src:
    print("  = fungsi sudah ada")
elif anchor in src:
    src = src.replace(anchor, FUNGSI + anchor, 1)
    print("  + fungsi tangani_pemilih_akun ditambahkan")
else:
    print("  !! anchor load_account tidak ketemu")

LAMA = ('                page.wait_for_selector(\n'
        '                    \'input[type="email"], input[type="password"], #identifierId\',\n'
        '                    timeout=8000)')
BARU = ('                # Kalau mendarat di pemilih akun, keluar dulu ke kolom email.\n'
        '                tangani_pemilih_akun(page)\n'
        '                page.wait_for_selector(\n'
        '                    \'input[type="email"], input[type="password"], #identifierId\',\n'
        '                    timeout=8000)')

if "tangani_pemilih_akun(page)" in src:
    print("  = panggilan sudah ada")
elif LAMA in src:
    src = src.replace(LAMA, BARU, 1)
    print("  + panggilan disisipkan")
else:
    print("  !! titik panggil tidak ketemu")

F.write_text(src)
r = subprocess.run(["/root/harbor/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])