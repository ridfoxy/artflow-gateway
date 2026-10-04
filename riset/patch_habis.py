#!/usr/bin/env python3
"""429 = langsung singkirkan akun (kuota reset BULANAN, bukan per jam).

Permintaan user: "jika sudah 429 jangan istirahatkan satu jam, tetapi langsung
tandai limited dan langsung singkirkan jangan dipakai kembali, karena resetnya
bulanan".

Kondisi lama: cooldown 3600s (1 jam) -> akun kuota-habis dicoba lagi sejam
kemudian, gagal 429 lagi, buang waktu + slot sesi. Padahal kuota Vids Google
baru pulih bulan depan.

Perubahan:
  1. `VIDS_LIMIT_COOLDOWN` bawaan 3600 -> 2678400 (31 hari). Akun yang kena 429
     langsung keluar dari rotasi (`available()` memblokir `until > now`) dan
     baru kembali setelah kuota bulanan pulih.
  2. Catatan di DB diperjelas: "kuota habis (429) — disingkirkan s/d reset bulanan",
     supaya panel admin bisa menjelaskan sebabnya.
  3. Tetap bisa dibalik: VIDS_LIMIT_COOLDOWN=3600 (drop-in) atau salin balik
     vids_runner.py.bak-habis-<ts>.
"""
import shutil
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/vids_runner.py")
src = F.read_text()
bak = F.with_suffix(".py.bak-habis-" + str(int(time.time())))
bak.write_text(src)
print("backup:", bak.name)

NL = chr(10)

# ── 1. cooldown bawaan: 1 jam -> 31 hari ────────────────────────────
LAMA1 = 'ACCOUNT_COOLDOWN_LIMIT = int(os.environ.get("VIDS_LIMIT_COOLDOWN", "3600"))'
BARU1 = ('# KUOTA HABIS (429) = SINGKIRKAN, bukan istirahat sebentar.\n'
         '# Kuota Vids Google pulih BULANAN, jadi akun yang sudah 429 tidak\n'
         '# berguna dicoba lagi sejam kemudian (gagal 429 lagi, buang slot sesi).\n'
         '# Bawaan 31 hari = keluar dari rotasi sampai kuota bulanan pulih.\n'
         '#   VIDS_LIMIT_COOLDOWN=3600 -> kembali ke perilaku "istirahat 1 jam"\n'
         'ACCOUNT_COOLDOWN_LIMIT = int(os.environ.get("VIDS_LIMIT_COOLDOWN", "2678400"))')

if 'os.environ.get("VIDS_LIMIT_COOLDOWN", "2678400")' in src:
    print("  = cooldown sudah 31 hari")
elif LAMA1 in src:
    src = src.replace(LAMA1, BARU1, 1)
    print("  + cooldown: 3600s (1 jam) -> 2678400s (31 hari)")
else:
    print("  !! baris ACCOUNT_COOLDOWN_LIMIT tidak ketemu")

# ── 2. catatan DB lebih jelas ───────────────────────────────────────
LAMA2 = """                         VALUES (?, 'limited', 'quota reached', ?, ?)"""
BARU2 = """                         VALUES (?, 'limited', 'kuota habis (429) — disingkirkan sampai kuota bulanan pulih', ?, ?)"""
LAMA3 = """                           state='limited', note='quota reached',"""
BARU3 = """                           state='limited',
                           note='kuota habis (429) — disingkirkan sampai kuota bulanan pulih',"""

if "disingkirkan sampai kuota bulanan pulih" in src:
    print("  = catatan sudah diperjelas")
elif LAMA2 in src and LAMA3 in src:
    src = src.replace(LAMA2, BARU2, 1).replace(LAMA3, BARU3, 1)
    print("  + catatan DB diperjelas")
else:
    print("  !! pola note mark_limited tidak ketemu")

# ── 3. log yang jelas saat akun disingkirkan ─────────────────────────
LAMA4 = '        log(f"akun {email} limit — cooldown {seconds}s")'
BARU4 = ('        _hari = seconds / 86400.0\n'
         '        if _hari >= 1:\n'
         '            log(f"akun {email} KUOTA HABIS — disingkirkan {_hari:.0f} hari "\n'
         '                f"(sampai kuota bulanan pulih)")\n'
         '        else:\n'
         '            log(f"akun {email} limit — cooldown {seconds}s")')

if "KUOTA HABIS — disingkirkan" in src:
    print("  = log sudah ada")
elif LAMA4 in src:
    src = src.replace(LAMA4, BARU4, 1)
    print("  + log penyingkiran ditambahkan")
else:
    print("  !! baris log mark_limited tidak ketemu")

F.write_text(src)
r = subprocess.run(["/root/geminiomni/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])

print("\nverifikasi:")
for p in subprocess.run(["grep", "-n", "2678400\\|disingkirkan", str(F)],
                        capture_output=True, text=True).stdout.splitlines():
    print("  ", p[:105])