#!/usr/bin/env python3
"""PERBAIKAN SISTEM AUTO LOGIN + CAMOUFOX.

AKAR MASALAH (terbukti dari isi folder):
  Runner memakai  : chromium.launch_persistent_context(chrome_profiles/<email>)
  Camoufox memakai: PROFILES / <email>          ← FOLDER YANG SAMA
  Camoufox = FIREFOX. Satu folder tidak boleh dipakai dua mesin browser:
  isinya jadi bercampur (cookies.sqlite+cert9.db [Firefox] DAN
  Default/Cookies+Preferences [Chromium]) -> sesi tidak terbaca keduanya.

PERBAIKAN:
  1. Camoufox pakai folder TERPISAH: chrome_profiles/<email>__ff
     (Chromium tetap di chrome_profiles/<email> — TIDAK diubah, tidak ada regresi)
  2. camoufox_login.py: tambah penanganan halaman persyaratan Workspace
     (gulir sampai bawah dulu — pelajaran dari gsuite_login.py)
  3. login_all.py: urutan jadi Chromium -> kalau gagal, Camoufox
     (sudah begitu) + teruskan folder terpisah + catat lebih jelas

ROLLBACK:
  cp camoufox_login.py.bak-ffsep-* camoufox_login.py
  cp login_all.py.bak-ffsep-* login_all.py
"""
import ast
import re
import shutil
import subprocess
import time
from pathlib import Path

APP = Path("/root/api-google-flow")
STAMP = int(time.time())

# ── 1. camoufox_login.py: folder terpisah + tangani persyaratan ────────
CF = APP / "camoufox_login.py"
BAK1 = CF.with_suffix(f".py.bak-ffsep-{STAMP}")
src = CF.read_text()
shutil.copy2(CF, BAK1)
print(f"backup: {BAK1.name}")


def gagal(p):
    shutil.copy2(BAK1, CF)
    print(f"!! {p} — ROLLBACK")
    raise SystemExit(1)


# 1a. folder profil terpisah (Firefox tidak boleh berbagi dengan Chromium)
lama = 'profile = Path(args.profile) if args.profile else (PROFILES / email)'
baru = '''# FOLDER TERPISAH dari Chromium. Chrome dan Firefox tidak boleh berbagi
    # user_data_dir: isinya bercampur (Default/Cookies milik Chrome, cookies.sqlite
    # + cert9.db milik Firefox) sehingga SESI TIDAK TERBACA oleh keduanya.
    # Runner tetap memakai PROFILES / email (Chrome); Camoufox memakai <email>__ff.
    profil_ff = PROFILES.parent / "firefox_profiles" / email
    profile = Path(args.profile) if args.profile else profil_ff'''
if lama not in src:
    gagal("anchor profile tidak ditemukan")
src = src.replace(lama, baru, 1)
print("  ~ folder profil Camoufox dipisah -> firefox_profiles/<email>")

# 1b. konstanta folder firefox
lama = 'PROFILES = APP_DIR / "chrome_profiles"'
baru = '''PROFILES = APP_DIR / "chrome_profiles"
# Profil Firefox (Camoufox) — WAJIB terpisah dari profil Chrome.
PROFILES_FF = APP_DIR / "firefox_profiles"'''
if lama not in src:
    gagal("anchor PROFILES tidak ditemukan")
src = src.replace(lama, baru, 1)
src = src.replace('PROFILES.parent / "firefox_profiles" / email',
                  'PROFILES_FF / email', 1)
print("  ~ konstanta PROFILES_FF ditambahkan")

# 1c. gulir sampai bawah sebelum klik setuju (pelajaran gsuite_login)
helper = '''
def scroll_to_bottom_soft(page, rounds=12, log_fn=print):
    """Gulir sampai dasar (halaman persyaratan Workspace menyembunyikan tombol).

    Terbukti di gsuite_login.py: tombol "Saya mengerti"/"I understand" baru ada
    setelah teksnya habis dibaca. Tanpa ini klik tidak pernah menemukannya.
    """
    posisi_lama = -1
    for i in range(rounds):
        try:
            page.evaluate("""() => {
                const all = [...document.querySelectorAll('div,section,main,article')];
                for (const e of all) {
                    if (e.scrollHeight > e.clientHeight + 20) e.scrollTop = e.scrollHeight;
                }
                window.scrollTo(0, document.body.scrollHeight);
                const d = document.scrollingElement;
                if (d) d.scrollTop = d.scrollHeight;
            }""")
        except Exception:
            pass
        try:
            posisi = page.evaluate("() => window.scrollY || document.scrollingElement.scrollTop || 0")
            tinggi = page.evaluate("() => Math.max(document.body.scrollHeight, document.scrollingElement.scrollHeight)")
            if abs(float(tinggi) - (float(posisi) + float(page.evaluate("() => window.innerHeight || 800")))) <= 5:
                break
            posisi_lama = posisi
        except Exception:
            posisi_lama = -1
        time.sleep(0.4)
    try:
        page.keyboard.press("End")
    except Exception:
        pass


def klik_setuju_kalau_ada(page, log_fn=log):
    """Klik tombol setuju di halaman persyaratan SETELAH menggulir."""
    scroll_to_bottom_soft(page)
    time.sleep(0.8)
    return click_next(page)


'''
if "def klik_setuju_kalau_ada" not in src:
    anchor = "def check_vids(page):"
    if anchor not in src:
        gagal("anchor check_vids tidak ditemukan")
    src = src.replace(anchor, helper + anchor, 1)
    print("  + scroll_to_bottom_soft() & klik_setuju_kalau_ada()")

# 1d. pakai di loop penyelesaian halaman antara
lama = '''                if "speedbump" in u or "workspaceterms" in u.lower():
                    scroll_to_bottom(page)
                if not click_next(page):
                    break'''
baru = '''                if "speedbump" in u or "workspaceterms" in u.lower() or "terms" in u.lower():
                    # GULIR dulu: tombol setuju tersembunyi di bawah teks panjang.
                    klik_setuju_kalau_ada(page)
                    time.sleep(1.2)
                if not click_next(page):
                    break'''
if lama not in src:
    gagal("anchor loop halaman antara tidak ditemukan")
src = src.replace(lama, baru, 1)
print("  ~ halaman persyaratan: gulir + klik setuju")

# 1e. setelah sandi: coba juga setuju kalau masih di halaman terms
lama = '''            # Selesaikan halaman antara (persetujuan/konfirmasi).'''
baru = '''            # Kalau langsung mendarat di halaman persyaratan, klik setuju.
            try:
                if "accounts.google.com" in page.url and (
                        "terms" in page.url.lower() or "speedbump" in page.url):
                    klik_setuju_kalau_ada(page)
                    time.sleep(2)
            except Exception:
                pass

            # Selesaikan halaman antara (persetujuan/konfirmasi).'''
if lama in src:
    src = src.replace(lama, baru, 1)
    print("  ~ cek persyaratan tepat setelah sandi")

CF.write_text(src)
try:
    ast.parse(src)
    print("  AST camoufox_login.py: OK")
except SyntaxError as e:
    gagal(f"SyntaxError camoufox: {e.lineno} {e.msg}")

# ── 2. login_all.py: teruskan folder ff + urutan lebih jelas ───────────
LA = APP / "login_all.py"
BAK2 = LA.with_suffix(f".py.bak-ffsep-{STAMP}")
src2 = LA.read_text()
shutil.copy2(LA, BAK2)
print(f"\nbackup: {BAK2.name}")

# 2a. Camoufox didahulukan untuk akun baru yang Chromium-nya kena verifikasi
lama = '''        state, detail, secs = run_one(email)

        # Chromium kena CAPTCHA? Coba Camoufox sebelum menyerah.'''
baru = '''        state, detail, secs = run_one(email)

        # Chromium kena CAPTCHA/verifikasi? Coba Camoufox (Firefox anti-deteksi)
        # sebelum menyerah. Camoufox memakai folder profil TERPISAH
        # (firefox_profiles/) supaya tidak merusak profil Chromium.'''
if lama not in src2:
    print("  ! anchor komentar camoufox tidak persis — dilewati (aman)")
else:
    src2 = src2.replace(lama, baru, 1)
    print("  ~ komentar urutan diperjelas")

# 2b. catat folder profil yang dipakai supaya mudah diaudit
lama = '''    cmd = [CAMOUFOX_PY, str(CAMOUFOX_SCRIPT), "--email", email]'''
baru = '''    # Profil Firefox terpisah; dikirim eksplisit supaya tidak ada kebingungan.
    profil_ff = APP / "firefox_profiles" / email
    cmd = [CAMOUFOX_PY, str(CAMOUFOX_SCRIPT), "--email", email,
           "--profile", str(profil_ff)]'''
if lama in src2:
    src2 = src2.replace(lama, baru, 1)
    print("  ~ run_camoufox: pakai --profile firefox_profiles/<email>")
else:
    print("  ! anchor cmd camoufox tidak ditemukan — dilewati")

LA.write_text(src2)
try:
    ast.parse(src2)
    print("  AST login_all.py: OK")
except SyntaxError as e:
    shutil.copy2(BAK2, LA)
    print(f"!! SyntaxError login_all: {e.lineno} {e.msg} — ROLLBACK")
    raise SystemExit(1)

print("\n=== VERIFIKASI ===")
s1, s2 = CF.read_text(), LA.read_text()
for nama, s, tanda in (("camoufox_login.py", s1, ["PROFILES_FF", "firefox_profiles",
                                                  "scroll_to_bottom_soft",
                                                  "klik_setuju_kalau_ada"]),
                       ("login_all.py", s2, ["firefox_profiles", "--profile"])):
    print(f"  {nama}:")
    for t in tanda:
        print(f"    {t:26} {'ADA' if t in s else 'TIDAK ADA'}")

print("\nrollback:")
print(f"  cp {BAK1.name} camoufox_login.py")
print(f"  cp {BAK2.name} login_all.py")