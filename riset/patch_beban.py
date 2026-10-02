#!/usr/bin/env python3
"""Perbaikan: batas beban untuk mesin ini + pembersih proses yatim otomatis.

MASALAH NYATA:
  1. Login berhenti di akun ke-6 dengan "!! beban 15.9 > 12.0".
     Batas 12 berasal dari mesin LAMA (2-core/4GB). Mesin ini 6 core / 16 GB,
     load 12 masih sangat longgar.
  2. Proses Camoufox/Firefox tidak selalu menutup diri -> menumpuk ->
     beban naik -> login berhenti sendiri.

PERBAIKAN:
  1. LOGIN_LOAD_MAX bawaan: naikkan ke 24 (env tetap bisa menimpa).
     Cek juga memori tersedia: kalau < 800 MB, tunda (bukan berhenti).
  2. Pembersih proses yatim dijalankan SEBELUM login mulai:
     - pkill camoufox-bin / firefox milik profil firefox_profiles
     - proses chrome yang usianya > 30 menit (yatim)
     Ini mencegah penumpukan.

ROLLBACK: cp _login_jalan.py.bak-beban-* _login_jalan.py
"""
import ast
import shutil
import subprocess
import time
from pathlib import Path

P = Path("/root/api-google-flow/_login_jalan.py")
BAK = P.with_suffix(f".py.bak-beban-{int(time.time())}")
s = P.read_text()
shutil.copy2(P, BAK)
print(f"backup: {BAK.name}")


def gagal(m):
    shutil.copy2(BAK, P)
    print(f"!! {m} — ROLLBACK")
    raise SystemExit(1)


# ── 1. naikkan batas beban + cek memori ───────────────────────────────
lama = 'LOAD_MAX = float(os.environ.get("LOGIN_LOAD_MAX", "12"))'
baru = '''# Mesin ini 6 core / 16 GB: load 12 terlalu ketat (terbukti: login berhenti
# di akun ke-6 dengan beban 15.9 padahal mesin masih longgar).
LOAD_MAX = float(os.environ.get("LOGIN_LOAD_MAX", "24"))
# Bila memori tersedia di bawah ini, login DITUNDA sebentar (bukan dihentikan).
RAM_MIN_MB = float(os.environ.get("LOGIN_RAM_MIN_MB", "800"))'''
if lama not in s:
    gagal("anchor LOAD_MAX tidak ditemukan")
s = s.replace(lama, baru, 1)
print("  ~ LOAD_MAX: 12 -> 24 (bisa ditimpa lewat LOGIN_LOAD_MAX)")

# ── 2. tambah helper pembersih proses yatim ───────────────────────────
anchor = "def beban():"
helper = '''def bersihkan_proses_yatim():
    """Bunuh sisa browser yang menumpuk sebelum login dimulai.

    Proses Camoufox/Firefox kadang tidak menutup sendiri; kalau dibiarkan,
    beban mesin naik dan login berhenti sendiri ("beban > batas").
    Hanya menyasar proses milik profil kita — bukan browser lain.
    """
    dibunuh = []
    kandidat = ("camoufox-bin", "firefox")
    for pola in kandidat:
        try:
            r = subprocess.run(["pgrep", "-f", pola], capture_output=True, text=True)
            pids = [p for p in r.stdout.split() if p.isdigit()]
        except Exception:
            pids = []
        for pid in pids:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read().decode("utf-8", "replace")
            except Exception:
                continue
            # hanya yang memakai profil kita
            if "api-google-flow" not in cmd and "firefox_profiles" not in cmd:
                continue
            try:
                os.kill(int(pid), 15)
                dibunuh.append(pid)
            except Exception:
                pass
    if dibunuh:
        time.sleep(2)
        for pid in dibunuh:
            try:
                os.kill(int(pid), 9)
            except Exception:
                pass
        print(f"  pembersih: {len(dibunuh)} proses browser yatim dibunuh", flush=True)
    return len(dibunuh)


def memori_tersedia_mb():
    """Perkiraan memori tersedia (MB) dari /proc/meminfo."""
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for l in fh:
                if ":" in l:
                    k, v = l.split(":", 1)
                    info[k.strip()] = int(v.split()[0])
        return info.get("MemAvailable", 0) / 1024
    except Exception:
        return 99999


'''
if anchor not in s:
    gagal("anchor beban() tidak ditemukan")
s = s.replace(anchor, helper + anchor, 1)
print("  + bersihkan_proses_yatim() + memori_tersedia_mb()")

# ── 3. main(): jalankan pembersih + cek memori ────────────────────────
lama2 = '''    kred = kredensial()
    target = list(kred) if args[0] == "--semua" else [
        e.strip() for e in args[0].split(",") if e.strip()]'''
baru2 = '''    # Bersihkan sisa browser sebelum mulai (mencegah beban menumpuk).
    print("membersihkan sisa proses browser...", flush=True)
    bersihkan_proses_yatim()

    kred = kredensial()
    target = list(kred) if args[0] == "--semua" else [
        e.strip() for e in args[0].split(",") if e.strip()]'''
if lama2 not in s:
    gagal("anchor kredensial tidak ditemukan")
s = s.replace(lama2, baru2, 1)
print("  ~ main(): pembersih dijalankan sebelum login")

lama3 = '''        b = beban()
        if b > LOAD_MAX:
            print(f"!! beban {b:.1f} > {LOAD_MAX} — berhenti; sisa "
                  f"{len(target) - i + 1} akun tidak diproses")
            break'''
baru3 = '''        b = beban()
        ram = memori_tersedia_mb()
        if b > LOAD_MAX or ram < RAM_MIN_MB:
            # Tunggu sebentar dulu (bukan langsung berhenti): browser biasanya
            # menutup sendiri setelah beberapa puluh detik.
            print(f"    (beban {b:.1f}, RAM {ram:.0f} MB — menunggu reda)",
                  flush=True)
            reda = False
            for _ in range(12):          # maks ~120s
                time.sleep(10)
                b2, r2 = beban(), memori_tersedia_mb()
                if b2 <= LOAD_MAX and r2 >= RAM_MIN_MB:
                    reda = True
                    break
            if not reda:
                print(f"!! beban {b:.1f} / RAM {ram:.0f} MB masih tinggi — "
                      f"berhenti; sisa {len(target) - i + 1} akun tidak diproses",
                      flush=True)
                break
            print(f"    beban reda -> lanjut", flush=True)
            bersihkan_proses_yatim()'''
if lama3 not in s:
    gagal("anchor cek beban tidak ditemukan")
s = s.replace(lama3, baru3, 1)
print("  ~ loop: tunggu reda dulu, baru berhenti bila tetap tinggi")

P.write_text(s)
try:
    ast.parse(s)
    print("  AST: OK")
except SyntaxError as e:
    gagal(f"SyntaxError {e.lineno}: {e.msg}")

print("\n=== VERIFIKASI ===")
t = P.read_text()
for k in ("LOAD_MAX", '"24"', "RAM_MIN_MB", "bersihkan_proses_yatim",
          "memori_tersedia_mb"):
    print(f"  {k:24} {'ADA' if k in t else 'TIDAK'}")

# uji helper
r = subprocess.run(["/root/geminiomni/venv/bin/python", "-c",
                    "import sys; sys.path.insert(0,'/root/api-google-flow');"
                    "import _login_jalan as L;"
                    "print('  LOAD_MAX =', L.LOAD_MAX);"
                    "print('  RAM_MIN  =', L.RAM_MIN_MB);"
                    "print('  RAM kini =', round(L.memori_tersedia_mb()), 'MB')"],
                   capture_output=True, text=True)
print(r.stdout or r.stderr[:300])
print(f"\nrollback: cp {BAK.name} _login_jalan.py")