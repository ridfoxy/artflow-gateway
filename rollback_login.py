#!/usr/bin/env python3
"""Rollback optimasi login — satu perintah, tanpa mengingat nama berkas.

Pakai:
    python3 rollback_login.py                 # daftar backup + status
    python3 rollback_login.py cepat           # matikan optimasi lewat env
    python3 rollback_login.py penuh           # kembalikan berkas dari backup
    python3 rollback_login.py penuh --ke=<nama.bak>
    python3 rollback_login.py otomatis        # rollback bila login gagal teknis
                                              # berturut-turut (lihat riwayat)
"""
import ast
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/root/api-google-flow")
FILE = APP / "gsuite_login.py"
RESULTS = APP / "data" / "login_results.jsonl"
ENV_FILE = APP / "login-optimasi.env"
STATUS = APP / "data" / "optimasi_login_status.json"


def backups():
    b = sorted(APP.glob("gsuite_login.py.bak-optimasi-*"),
               key=lambda p: p.stat().st_mtime, reverse=True)
    return b


def status():
    print("=== STATUS OPTIMASI LOGIN ===")
    s = FILE.read_text()
    aktif = "LOGIN_CEPAT" in s
    print(f"  kode optimasi terpasang : {aktif}")
    if aktif:
        r = subprocess.run(
            ["/root/geminiomni/venv/bin/python", "-c",
             "import sys; sys.path.insert(0,'/root/api-google-flow');"
             "import gsuite_login as G;"
             "print(f'CEPAT={G.LOGIN_CEPAT} SEL_MS={G.LOGIN_SEL_MS} "
             "POLL={G.LOGIN_POLL} GULIR_TOL={G.LOGIN_GULIR_TOL}')"],
            capture_output=True, text=True)
        print(f"  nilai aktif sekarang    : {r.stdout.strip() or r.stderr[:100]}")
    if ENV_FILE.exists():
        print(f"  setelan env             : {ENV_FILE.read_text().strip()}")
    else:
        print("  setelan env             : (belum ada — memakai bawaan)")
    print("\n  berkas cadangan tersedia:")
    for i, b in enumerate(backups()[:6], 1):
        umur = (datetime.now().timestamp() - b.stat().st_mtime) / 60
        print(f"    {i}. {b.name}  ({umur:.0f} menit lalu, {b.stat().st_size} B)")
    if not backups():
        print("    (tidak ada)")
    print("\n  rollback:")
    print("    cepat : python3 rollback_login.py cepat   -> GS_LOGIN_CEPAT=0")
    print("    penuh : python3 rollback_login.py penuh   -> salin cadangan terbaru")
    print("    otomatis: python3 rollback_login.py otomatis")
    return 0


def set_env(nilai):
    """Tulis berkas env yang dibaca gateway (tanpa mengubah kode)."""
    ENV_FILE.write_text(f"# Rollback optimasi login\nGS_LOGIN_CEPAT={nilai}\n",
                        encoding="utf-8")
    print(f"  {ENV_FILE} ditulis: GS_LOGIN_CEPAT={nilai}")
    # terapkan ke systemd drop-in supaya benar-benar terbaca proses
    drop = Path("/etc/systemd/system/artflow-gateway-local.service.d/"
                "40-optimasi-login.conf")
    drop.parent.mkdir(parents=True, exist_ok=True)
    drop.write_text(f"[Service]\nEnvironment=GS_LOGIN_CEPAT={nilai}\n",
                    encoding="utf-8")
    print(f"  drop-in systemd: {drop}")
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "restart", "artflow-gateway-local.service"],
                       capture_output=True, text=True)
    print(f"  restart gateway: {'OK' if r.returncode == 0 else r.stderr[:120]}")
    STATUS.write_text(json.dumps({
        "mode": "env", "GS_LOGIN_CEPAT": nilai,
        "at": datetime.now(timezone.utc).isoformat()}, indent=2))
    return 0


def rollback_penuh(target=None):
    siap = backups()
    if target:
        cand = APP / target
        if not cand.exists():
            print(f"!! tidak ada: {cand}")
            return 1
    else:
        if not siap:
            print("!! tidak ada cadangan")
            return 1
        cand = siap[0]
    # simpan keadaan sekarang sebelum menimpa (supaya bisa maju lagi)
    maju = FILE.with_suffix(f".py.pra-rollback-{int(datetime.now().timestamp())}")
    shutil.copy2(FILE, maju)
    shutil.copy2(cand, FILE)
    try:
        ast.parse(FILE.read_text())
    except SyntaxError as e:
        shutil.copy2(maju, FILE)
        print(f"!! cadangan rusak ({e}) — berkas sekarang tidak diubah")
        return 1
    print(f"  dikembalikan dari : {cand.name}")
    print(f"  keadaan sebelum   : {maju.name} (untuk maju lagi)")
    # pastikan env tidak memaksa mode lama
    drop = Path("/etc/systemd/system/artflow-gateway-local.service.d/"
                "40-optimasi-login.conf")
    if drop.exists():
        drop.unlink()
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        print(f"  drop-in dihapus  : {drop.name}")
    subprocess.run(["systemctl", "restart", "artflow-gateway-local.service"],
                   capture_output=True)
    print("  gateway direstart")
    STATUS.write_text(json.dumps({
        "mode": "penuh", "dari": cand.name,
        "at": datetime.now(timezone.utc).isoformat()}, indent=2))
    return 0


def rollback_otomatis(ambang=3):
    """Rollback bila N akun terakhir gagal karena masalah TEKNIS (bukan CAPTCHA).

    CAPTCHA/sandi salah BUKAN alasan rollback: itu urusan akun, bukan kode.
    Yang dihitung: state 'error' (teknis) dan 'needs_human' yang detailnya
    menyebut persyaratan/workspaceterms — itu jalur yang baru dioptimalkan.
    """
    if not RESULTS.exists():
        print("  belum ada data hasil login")
        return 0
    rows = [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()]
    if len(rows) < ambang:
        print(f"  data belum cukup ({len(rows)} < {ambang})")
        return 0
    akhir = rows[-ambang:]
    teknis = 0
    for r in akhir:
        d = str(r.get("detail", "")).lower()
        if r["state"] == "error":
            teknis += 1
        elif r["state"] == "needs_human" and ("workspace" in d or "persyaratan" in d):
            teknis += 1
        print(f"    {r['email']:26} {r['state']:13} {str(r.get('detail'))[:60]}")
    print(f"\n  gagal teknis: {teknis}/{ambang}")
    if teknis >= ambang:
        print("  -> ROLLBACK OTOMATIS dijalankan")
        return rollback_penuh()
    print("  -> optimasi DIPERTAHANKAN (kegagalan bukan karena jalur baru)")
    return 0


def main():
    a = sys.argv[1:] or ["status"]
    perintah = a[0]
    if perintah == "status":
        return status()
    if perintah == "cepat":
        return set_env("0")
    if perintah == "aktifkan":
        return set_env("1")
    if perintah == "penuh":
        ke = next((x.split("=", 1)[1] for x in a if x.startswith("--ke=")), None)
        return rollback_penuh(ke)
    if perintah == "otomatis":
        return rollback_otomatis()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())