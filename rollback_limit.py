#!/usr/bin/env python3
"""ROLLBACK perilaku akun limit — semua lewat env / backup, tanpa ubah kode.

PERILAKU SEKARANG (bawaan):
  - 429 (Resource has been exhausted) -> akun ditandai `limited` dan LANGSUNG
    disingkirkan dari rotasi selama 31 hari (kuota Vids pulih bulanan).
    Env: VIDS_LIMIT_COOLDOWN=2678400
  - Batas sesi 60 sebagai pengaman (bukan target).
    Env: VIDS_SESSION_JOBS=60

CARA MEMBALIK:
  A. Istirahat 1 jam lagi (perilaku lama):
       python rollback_limit.py jam
  B. Batas sesi kembali 12:
       python rollback_limit.py batas12
  C. Pulihkan kode dari backup:
       python rollback_limit.py kode
  D. Lihat konfigurasi + daftar akun yang disingkirkan:
       python rollback_limit.py lihat
  E. Kembalikan akun tertentu ke rotasi (mis. kuota sudah pulih):
       python rollback_limit.py pulihkan <email>
"""
import subprocess
import sys

D = "/etc/systemd/system/artflow-vids-runner-local.service.d"
sys.path.insert(0, "/root")
from ssh_baru import ssh  # noqa: E402

DB = "/root/api-google-flow/data/vids_jobs.db"
aksi = sys.argv[1] if len(sys.argv) > 1 else "lihat"


def tulis_dropin(nama, isi):
    print(ssh(f"cat > {D}/{nama} <<'EOF'\n{isi}EOF\n"
              f"systemctl daemon-reload && echo 'daemon-reload OK'"))
    print(ssh("systemctl restart artflow-vids-runner-local && sleep 16 && "
              "systemctl is-active artflow-vids-runner-local", timeout=300))


if aksi == "lihat":
    print("=== DROP-IN ===")
    print(ssh(f"for f in {D}/*.conf; do echo \"--- $(basename $f)\"; "
              f"grep -v '^#' $f | grep .; done"))
    print("\n=== NILAI EFEKTIF DI KODE ===")
    print(ssh("grep -n 'VIDS_LIMIT_COOLDOWN\\|VIDS_SESSION_JOBS\\|VIDS_RECYCLE_JOBS' "
              "/root/api-google-flow/vids_runner.py | head -6"))
    print("\n=== AKUN YANG DISINGKIRKAN (kuota habis) ===")
    print(ssh(f"""sqlite3 {DB} "SELECT email, note, CAST((until-strftime('%s','now'))/86400 AS INT) || ' hari' FROM vids_accounts WHERE state='limited' ORDER BY until DESC LIMIT 20" 2>&1"""))
    print(ssh(f"""sqlite3 {DB} "SELECT state, COUNT(*) FROM vids_accounts GROUP BY state" 2>&1"""))

elif aksi == "jam":
    print("=== BALIK: istirahat 1 jam (perilaku lama) ===")
    tulis_dropin("60-cooldown-jam.conf",
                 "# ROLLBACK: akun 429 hanya istirahat 1 jam, bukan disingkirkan 31 hari.\n"
                 "# Hapus file ini untuk kembali ke penyingkiran s/d reset bulanan.\n"
                 "[Service]\nEnvironment=VIDS_LIMIT_COOLDOWN=3600\n")

elif aksi == "batas12":
    print("=== BALIK: batas sesi 12 ===")
    tulis_dropin("61-batas12.conf",
                 "[Service]\nEnvironment=VIDS_SESSION_JOBS=12\n")

elif aksi == "kode":
    print("=== pulihkan vids_runner.py dari backup terbaru ===")
    print(ssh("cd /root/api-google-flow && B=$(ls -t vids_runner.py.bak-habis-* | head -1) "
              "&& cp $B vids_runner.py && /root/geminiomni/venv/bin/python -m py_compile "
              "vids_runner.py && echo \"dipulihkan dari $B\"", timeout=200))
    print(ssh("systemctl restart artflow-vids-runner-local && sleep 16 && "
              "systemctl is-active artflow-vids-runner-local", timeout=300))

elif aksi == "pulihkan" and len(sys.argv) > 2:
    email = sys.argv[2]
    print(f"=== kembalikan {email} ke rotasi ===")
    print(ssh(f"""sqlite3 {DB} "DELETE FROM vids_accounts WHERE email='{email}'" && echo 'dihapus dari daftar block'"""))
    print(ssh("systemctl restart artflow-vids-runner-local && sleep 16 && "
              "systemctl is-active artflow-vids-runner-local", timeout=300))

elif aksi == "reset-semua":
    print("=== kosongkan SEMUA penandaan akun (mulai bersih) ===")
    print(ssh(f"""sqlite3 {DB} "DELETE FROM vids_accounts" && sqlite3 {DB} "SELECT COUNT(*) FROM vids_accounts" """))

else:
    print("pakai: lihat | jam | batas12 | kode | pulihkan <email> | reset-semua")