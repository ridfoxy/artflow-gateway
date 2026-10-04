#!/usr/bin/env python3
"""Jadikan CAMOUFOX jalur utama login (bukan cadangan Chromium).

Kondisi sekarang: jalur utama = Chromium (gsuite_login.py); Camoufox hanya
dipakai kalau Chromium kena CAPTCHA. Akibatnya login selalu mencoba Chromium
dulu — padahal sesi pool @gumdel tersimpan di profil Firefox, sehingga hasil
Chromium selalu "needs_human" dan baru ditolong Camoufox (buang waktu).

Perubahan: bila LOGIN_UTAMA_CAMOUFOX != 0 (bawaan 1), jalankan Camoufox dulu;
Chromium hanya dipakai bila Camoufox gagal.

Backup .bak-loginurutan-<ts>; rollback: salin balik atau set env =0.
"""
import shutil
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/_login_jalan.py")
src = F.read_text()
bak = F.with_suffix(".py.bak-loginurutan-" + str(int(time.time())))
bak.write_text(src)
print("backup:", bak.name)

NL = chr(10)

# ── 1. tambah saklar ─────────────────────────────────────────────────
LAMA_SW = 'PAKAI_CAMOUFOX = os.environ.get("LOGIN_CAMOUFOX", "1") not in ("0", "false", "no")'
BARU_SW = (LAMA_SW + NL +
           '# URUTAN JALUR: Camoufox lebih dulu (bawaan), Chromium jadi cadangan.' + NL +
           '# Sesi pool tersimpan di profil Firefox (firefox_profiles/), jadi' + NL +
           '# Chromium hampir selalu berakhir CAPTCHA. Setel =0 untuk urutan lama.' + NL +
           'UTAMA_CAMOUFOX = os.environ.get("LOGIN_UTAMA_CAMOUFOX", "1") not in ("0", "false", "no")')

if "UTAMA_CAMOUFOX" in src:
    print("  = saklar sudah ada")
elif LAMA_SW in src:
    src = src.replace(LAMA_SW, BARU_SW, 1)
    print("  + saklar UTAMA_CAMOUFOX ditambahkan")

# ── 2. jalankan sesuai urutan ────────────────────────────────────────
LAMA = '''        state, detail, secs = jalankan_satu(email)'''

BARU = '''        if UTAMA_CAMOUFOX and PAKAI_CAMOUFOX:
            # Camoufox LEBIH DULU: sesi pool ada di profil Firefox.
            st2, d2, s2 = jalankan_camoufox(email)
            if st2 == "ok":
                state, detail, secs = "ok", f"camoufox; {d2}", s2
                print(f"    -> camoufox {st2}: {str(d2)[:80]} ({s2}s)", flush=True)
            else:
                print(f"    -> camoufox {st2}: {str(d2)[:80]} — coba Chromium",
                      flush=True)
                state, detail, secs = jalankan_satu(email)
        else:
            state, detail, secs = jalankan_satu(email)'''

if "Camoufox LEBIH DULU" in src:
    print("  = urutan sudah dibalik")
elif LAMA in src:
    src = src.replace(LAMA, BARU, 1)
    print("  + urutan dibalik: Camoufox utama, Chromium cadangan")
else:
    print("  !! titik jalankan_satu tidak ketemu")

# ── 3. fallback lama: jangan jalankan Camoufox dua kali ──────────────
LAMA2 = '        if state == "needs_human" and PAKAI_CAMOUFOX:'
BARU2 = ('        if (state == "needs_human" and PAKAI_CAMOUFOX\n'
         '                and not UTAMA_CAMOUFOX):')
if "not UTAMA_CAMOUFOX" in src:
    print("  = penjaga fallback sudah ada")
elif LAMA2 in src:
    src = src.replace(LAMA2, BARU2, 1)
    print("  + fallback lama dijaga agar tidak dobel")

F.write_text(src)
r = subprocess.run(["/root/geminiomni/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])