#!/usr/bin/env python3
"""Pindah akun + tandai 'limited' saat Google bilang kuota habis (429).

Perilaku yang diminta user:
  "gunakan akun hingga Resource has been exhausted, lalu lempar ke akun yang
   sehat, dan tandai akun tersebut sebagai limit."

Kondisi sekarang:
  - 429 dari Google hanya membuat job gagal; akun TIDAK ditandai.
  - Akun terus dipakai sampai VIDS_SESSION_JOBS (12) walau kuotanya sudah habis
    -> sisa percobaan membuang waktu.
  - Batas 12/sesi memotong akun yang sebenarnya masih punya kuota.

Perubahan (semua bisa dibalik lewat env, tanpa mengubah kode lagi):
  1. Kenali 429/"Resource has been exhausted" -> akun ditandai `limited`
     selama VIDS_LIMIT_COOLDOWN (default 1 jam) + job langsung dipindah ke
     akun berikutnya.
  2. Batas sesi dinaikkan ke VIDS_SESSION_JOBS (default baru 60) supaya akun
     dipakai SAMPAI kuotanya benar-benar habis, bukan sampai hitungan buatan.
     Batas keras itu penting sebagai pengaman: kalau kuota ternyata tak pernah
     habis, sesi berhenti di 60 (dan proses didaur ulang tiap
     VIDS_RECYCLE_JOBS untuk menjaga memori).

ROLLBACK:
  - Ubah drop-in: VIDS_SESSION_JOBS=12 VIDS_LIMIT_COOLDOWN=3600
  - atau salin balik vids_runner.py.bak-limitdet-<ts>
"""
import shutil
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/vids_runner.py")
src = F.read_text()
bak = F.with_suffix(".py.bak-limitdet-" + str(int(time.time())))
bak.write_text(src)
print("backup:", bak.name)

NL = chr(10)

# ═══ 1. helper pengenal kuota habis ═══
FUNGSI = '''

def kuota_habis(teks):
    """True bila respons Google menandakan kuota akun habis (429).

    Bentuk nyata: status 429 + body memuat "Resource has been exhausted
    (e.g. check quota)". Dipakai untuk MEMINDAHKAN job ke akun lain dan
    menandai akun itu `limited`, bukan menggagalkan job.
    """
    t = str(teks or "").lower()
    return ("resource has been exhausted" in t
            or "resource_exhausted" in t
            or "check quota" in t
            or (t.strip().startswith("429")))

'''

anchor = "\ndef looks_like_ui_not_ready(detail):"
if "def kuota_habis(" in src:
    print("  = helper kuota_habis sudah ada")
elif anchor in src:
    src = src.replace(anchor, FUNGSI + anchor, 1)
    print("  + helper kuota_habis ditambahkan")
else:
    print("  !! anchor looks_like_ui_not_ready tidak ketemu")

# ═══ 2. jalur paralel: 429 -> limited + ulang akun lain ═══
LAMA_P = '''                            hasil[info["job_id"]] = ("ui_fallback",
                                                     f"ulang gagal status={st}")'''
BARU_P = '''                            if kuota_habis(text) or st == 429:
                                hasil[info["job_id"]] = (
                                    "limited",
                                    "kuota akun habis (429) — pindah akun")
                            else:
                                hasil[info["job_id"]] = ("ui_fallback",
                                                         f"ulang gagal status={st}")'''

if 'kuota akun habis (429) — pindah akun' in src:
    print("  = cabang paralel sudah ada")
elif LAMA_P in src:
    src = src.replace(LAMA_P, BARU_P, 1)
    print("  + cabang paralel: 429 -> limited")
else:
    print("  !! cabang paralel tidak ketemu")

# ═══ 3. jalur paralel utama (percobaan pertama) ═══
LAMA_P2 = '''                        hasil[info["job_id"]] = ("failed", "stream gagal/kecil")'''
BARU_P2 = '''                        if kuota_habis(text):
                            hasil[info["job_id"]] = (
                                "limited", "kuota akun habis (429) — pindah akun")
                        else:
                            hasil[info["job_id"]] = ("failed", "stream gagal/kecil")'''

if '"limited", "kuota akun habis (429) — pindah akun")' in src:
    print("  = cabang utama sudah ada")
elif LAMA_P2 in src:
    src = src.replace(LAMA_P2, BARU_P2, 1)
    print("  + cabang utama: 429 -> limited")
else:
    print("  !! cabang utama tidak ketemu")

# ═══ 4. batas sesi dinaikkan (akun dipakai sampai kuota habis) ═══
LAMA_S = 'SESSION_MAX_JOBS = int(os.environ.get("VIDS_SESSION_JOBS", "12"))'
BARU_S = ('# BATAS SESI = pengaman, bukan target. Akun sebaiknya dipakai sampai\n'
          '# Google bilang kuota habis (429) -> ditandai `limited` + pindah akun.\n'
          '# Nilai kecil (dulu 12) memotong akun yang masih punya kuota.\n'
          'SESSION_MAX_JOBS = int(os.environ.get("VIDS_SESSION_JOBS", "60"))')

if 'os.environ.get("VIDS_SESSION_JOBS", "60")' in src:
    print("  = batas sesi sudah 60")
elif LAMA_S in src:
    src = src.replace(LAMA_S, BARU_S, 1)
    print("  + batas sesi: 12 -> 60 (pengaman, bukan target)")
else:
    print("  !! baris SESSION_MAX_JOBS tidak ketemu")

F.write_text(src)
r = subprocess.run(["/root/geminiomni/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])

print("\nverifikasi:")
for p in subprocess.run(["grep", "-n", "kuota_habis\\|SESSION_MAX_JOBS", str(F)],
                        capture_output=True, text=True).stdout.splitlines():
    print("  ", p[:110])