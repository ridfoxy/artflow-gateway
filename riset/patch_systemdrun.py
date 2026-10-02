#!/usr/bin/env python3
"""PENGAMAN 3: login dijalankan sebagai layanan systemd SENDIRI.

Masalah nyata: `_login_jalan.py` dijalankan gateway lewat subprocess.Popen
(start_new_session=True). Tetapi karena ia tetap berada di dalam cgroup
artflow-gateway-local.service, `systemctl restart artflow-gateway-local`
MEMBUNUH seluruh cgroup — login yang sedang berjalan ikut mati (terbukti:
login 25 akun berhenti di akun ke-7 saat gateway direstart).

Perbaikan: gunakan systemd-run (transient unit) sehingga proses login berada
di cgroup SENDIRI dan tidak ikut mati saat gateway direstart.
"""
import ast
import shutil
import subprocess
import time
from pathlib import Path

P = Path("/root/api-google-flow/app.py")
BAK = P.with_suffix(f".py.bak-systemdrun-{int(time.time())}")
s = P.read_text()
shutil.copy2(P, BAK)
print(f"backup: {BAK.name}")


def gagal(m):
    shutil.copy2(BAK, P)
    print(f"!! {m} — ROLLBACK")
    raise SystemExit(1)


lama = '''    logf = open(LOGF, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, str(SCRIPT), ",".join(batch)],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT,
        start_new_session=True, env={**os.environ, "DISPLAY": ":99"})'''

baru = '''    logf = open(LOGF, "w", encoding="utf-8")
    # Jalankan login di cgroup SENDIRI (systemd transient unit) supaya
    # `systemctl restart artflow-gateway-local` TIDAK mematikannya.
    # Terbukti: dengan Popen biasa, restart gateway membunuh login yang
    # sedang berjalan (25 akun berhenti di akun ke-7).
    pakai_systemd_run = os.environ.get("LOGIN_VIA_SYSTEMD", "1") not in ("0", "false", "no")
    proc = None
    if pakai_systemd_run:
        try:
            proc = subprocess.Popen(
                ["systemd-run", "--collect", "--unit", f"artflow-login-job",
                 "--description", "ArtFlow auto-login (semua akun)",
                 "--setenv", "DISPLAY=:99",
                 "--working-directory", str(APP),
                 PY, str(SCRIPT), ",".join(batch)],
                stdout=logf, stderr=subprocess.STDOUT)
            logger.info("auto-login dijalankan via systemd-run (cgroup sendiri)")
        except Exception as e:
            logger.warning("systemd-run gagal (%s) — memakai Popen biasa",
                           str(e)[:90])
            proc = None
    if proc is None:
        proc = subprocess.Popen(
            [PY, str(SCRIPT), ",".join(batch)],
            cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, env={**os.environ, "DISPLAY": ":99"})
        logger.info("auto-login dijalankan via Popen biasa")'''

if lama not in s:
    gagal("anchor Popen login tidak ditemukan")
s = s.replace(lama, baru, 1)
print("  ~ login dijalankan via systemd-run (cgroup sendiri)")

P.write_text(s)
try:
    ast.parse(s)
    print("  AST: OK")
except SyntaxError as e:
    gagal(f"SyntaxError {e.lineno}: {e.msg}")

print("\n=== VERIFIKASI ===")
t = P.read_text()
print(f"  systemd-run dipakai : {'systemd-run' in t}")
print(f"  fallback Popen ada  : {'Popen biasa' in t}")
print(f"\nrollback: cp {BAK.name} app.py")