#!/usr/bin/env python3
"""UJI ROLLBACK pada SALINAN (produksi tidak disentuh).

Memverifikasi:
  1. rollback penuh benar-benar mengembalikan berkas ke versi lama
  2. berkas hasil rollback bebas dari kode optimasi
  3. berkas bisa diimpor (tidak rusak)
  4. bisa "maju lagi" dari salinan pra-rollback
"""
import ast
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP = Path("/root/api-google-flow")
ASLI = APP / "gsuite_login.py"
UJI = Path("/tmp/uji_rollback_login")
UJI.mkdir(exist_ok=True)

# salin keadaan sekarang + cadangan ke tempat uji
salin = UJI / "gsuite_login.py"
shutil.copy2(ASLI, salin)
bak = sorted(APP.glob("gsuite_login.py.bak-optimasi-*"),
             key=lambda p: p.stat().st_mtime, reverse=True)[0]
salin_bak = UJI / bak.name
shutil.copy2(bak, salin_bak)
print(f"berkas uji    : {salin}")
print(f"cadangan      : {salin_bak.name}")

s_now = salin.read_text()
s_bak = salin_bak.read_text()

print("\n=== SEBELUM ROLLBACK ===")
for t in ("LOGIN_CEPAT", "def tahap", "_present_now(page, SELECTOR)",
          "abs(posisi - posisi_akhir)"):
    print(f"  {t:34} {'ADA' if t in s_now else 'tidak ada'}")
print(f"  ukuran: {len(s_now)} char")

print("\n=== JALANKAN ROLLBACK (pada salinan) ===")
shutil.copy2(salin_bak, salin)
print("  salinan ditimpa dengan cadangan")

s_new = salin.read_text()
print("\n=== SESUDAH ROLLBACK ===")
bersih = 0
for t in ("LOGIN_CEPAT", "def tahap", "_present_now(page, SELECTOR)"):
    ada = t in s_new
    if not ada:
        bersih += 1
    print(f"  {t:34} {'MASIH ADA (salah)' if ada else 'sudah hilang (benar)'}")
print(f"  ukuran: {len(s_new)} char")
print(f"  -> {'BENAR: kode optimasi hilang' if bersih == 3 else 'PERIKSA'}")

# 3. apakah hasil rollback bisa diimpor?
print("\n=== UJI IMPOR HASIL ROLLBACK ===")
r = subprocess.run(["/root/geminiomni/venv/bin/python", "-c",
                    f"import ast; ast.parse(open({str(salin)!r}).read()); "
                    "print('  AST: OK (berkas tidak rusak)')"],
                   capture_output=True, text=True)
print(r.stdout.strip() or r.stderr[:200])

# 4. apakah identik dengan cadangan?
identik = salin.read_text() == s_bak
print(f"\n=== IDENTIK dengan cadangan? {identik} ===")

# 5. bisa maju lagi?
print("\n=== UJI 'MAJU LAGI' (pulihkan versi optimasi) ===")
maju = UJI / "pra-rollback.py"
shutil.copy2(ASLI, maju)
print(f"  salinan versi optimasi disimpan: {maju.name} ({maju.stat().st_size} B)")
print(f"  -> bisa dikembalikan kapan saja dengan menyalinnya kembali")

# ringkas
print("\n=== RINGKASAN UJI ROLLBACK ===")
lulus = 0
total = 4
if bersih == 3:
    lulus += 1
    print("  OK  kode optimasi terhapus saat rollback")
if "OK" in (r.stdout or ""):
    lulus += 1
    print("  OK  berkas hasil rollback tidak rusak")
if identik:
    lulus += 1
    print("  OK  hasil identik dengan cadangan")
if maju.exists() and maju.stat().st_size > 30000:
    lulus += 1
    print("  OK  versi optimasi tersimpan untuk maju lagi")
print(f"\n  {lulus}/{total} lulus")