#!/usr/bin/env python3
"""PERBAIKAN 2: penanganan halaman persyaratan Workspace harus SABAR & DIULANG.

BUKTI dari diagnosa (diag_terms_ff.py pada asepqds1):
  - Halaman workspacetermsofservice punya tombol "Saya mengerti" YANG TERLIHAT:
        tombol terlihat (2):
          - 'Saya mengerti' [BUTTON] disabled=False
          - 'Scroll ke bawah' [BUTTON] disabled=False
  - Setelah diklik -> keluar ke myaccount.google.com  ✅

MASALAH pada camoufox_login.py versi sekarang:
  Klik dilakukan SATU KALI terlalu cepat (halaman belum selesai dimuat),
  sehingga tombol belum ada saat diklik -> job lanjut -> masih di halaman akun.

PERBAIKAN:
  klik_setuju_kalau_ada() diulang beberapa kali: tunggu tombol MUNCUL dulu
  (wait_for_selector), gulir, klik, lalu pastikan halaman berpindah.
  Bila belum pindah, ulangi.

ROLLBACK:
  cp camoufox_login.py.bak-sabar-* camoufox_login.py
"""
import ast
import shutil
import subprocess
import time
from pathlib import Path

CF = Path("/root/api-google-flow/camoufox_login.py")
BAK = CF.with_suffix(f".py.bak-sabar-{int(time.time())}")
src = CF.read_text()
shutil.copy2(CF, BAK)
print(f"backup: {BAK.name}")


def gagal(p):
    shutil.copy2(BAK, CF)
    print(f"!! {p} — ROLLBACK")
    raise SystemExit(1)


# Ganti klik_setuju_kalau_ada() dengan versi SABAR (tunggu + ulang + verifikasi)
lama = '''def klik_setuju_kalau_ada(page, log_fn=log):
    """Klik tombol setuju di halaman persyaratan SETELAH menggulir."""
    scroll_to_bottom_soft(page)
    time.sleep(0.8)
    return click_next(page)'''

baru = '''def tawaran_setuju(page):
    """True bila tombol setuju benar-benar ADA dan terlihat di halaman ini."""
    for sel in ('button:has-text("Saya mengerti")',
                'button:has-text("Saya setuju")',
                'button:has-text("Setuju")',
                'button:has-text("Terima")',
                'button:has-text("I understand")',
                'button:has-text("I agree")',
                'button:has-text("Accept")',
                'button:has-text("Agree")'):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            continue
    return False


def klik_setuju_kalau_ada(page, log_fn=log, maks=6):
    """Klik tombol setuju dengan SABAR (tunggu muncul, gulir, klik, ulangi).

    Diuji di lapangan: tombol "Saya mengerti" pada halaman
    workspacetermsofservice TERLIHAT & aktif, tetapi bila diklik terlalu cepat
    (halaman belum selesai dimuat) kliknya tidak berpengaruh. Jadi: TUNGGU
    tombolnya muncul, gulir, klik, lalu PASTIKAN halaman berpindah; kalau belum,
    ulangi sampai `maks` kali.
    """
    for putaran in range(maks):
        url_awal = ""
        try:
            url_awal = page.url
        except Exception:
            pass

        # 1. tunggu tombolnya benar-benar ada (naikkan waktu tiap putaran)
        sabar = 3 + putaran * 3
        try:
            page.wait_for_selector(
                'button:has-text("Saya mengerti"), button:has-text("Setuju"), '
                'button:has-text("Terima"), button:has-text("I understand"), '
                'button:has-text("I agree"), button:has-text("Accept")',
                timeout=sabar * 1000)
        except Exception:
            pass

        # 2. gulir sampai dasar (tombol muncul setelah teks dibaca)
        scroll_to_bottom_soft(page, rounds=10, log_fn=log_fn)
        time.sleep(1.0)

        # 3. klik
        klik = None
        for lab in ("Saya mengerti", "Saya setuju", "Setuju", "Terima",
                    "I understand", "I agree", "Accept", "Agree", "Lanjutkan",
                    "Continue"):
            try:
                el = page.get_by_role("button", name=lab, exact=False).first
                if el and el.is_visible():
                    el.click(timeout=6000)
                    klik = lab
                    break
            except Exception:
                continue
        if not klik:
            klik = click_next(page)
        log_fn(f"    setuju putaran {putaran + 1}: klik={klik}")

        # 4. tunggu halaman berpindah
        batas = time.time() + 12
        while time.time() < batas:
            time.sleep(1.0)
            try:
                sekarang = page.url
            except Exception:
                sekarang = url_awal
            if "accounts.google.com" not in sekarang:
                log_fn(f"    keluar dari halaman akun -> {sekarang[:70]}")
                return True
            if sekarang != url_awal:
                return True

    try:
        return "accounts.google.com" not in page.url
    except Exception:
        return False'''

if lama not in src:
    gagal("anchor klik_setuju_kalau_ada tidak ditemukan")
src = src.replace(lama, baru, 1)
print("  ~ klik_setuju_kalau_ada(): sabar + ulang + verifikasi")
print("  + tawaran_setuju()")

# Perbaikan: jangan BREAK terlalu cepat pada halaman speedbump di loop pertama
lama2 = '''            while time.time() < deadline:
                url = page.url
                if "accounts.google.com" not in url:
                    break
                hay = text_low(page)
                if find(CHALLENGE, hay) or find(BAD, hay):
                    break
                if "speedbump" in url or "workspaceterms" in url.lower():
                    break
                time.sleep(1.5)'''
baru2 = '''            while time.time() < deadline:
                url = page.url
                if "accounts.google.com" not in url:
                    break
                hay = text_low(page)
                if find(CHALLENGE, hay) or find(BAD, hay):
                    break
                # Halaman persyaratan: tangani DI SINI, jangan keluar dari loop
                # hanya karena URL-nya speedbump (tombolnya belum tentu dimuat).
                if "speedbump" in url or "terms" in url.lower():
                    if tawaran_setuju(page):
                        klik_setuju_kalau_ada(page, log_fn=log)
                        if "accounts.google.com" not in page.url:
                            break
                    else:
                        time.sleep(2)
                        continue
                time.sleep(1.5)'''
if lama2 not in src:
    gagal("anchor loop pertama tidak ditemukan")
src = src.replace(lama2, baru2, 1)
print("  ~ loop pertama: tangani persyaratan di tempat")

# Loop kedua: pakai versi sabar
lama3 = '''            for _ in range(6):
                u = page.url
                if "accounts.google.com" not in u:
                    break
                hay = text_low(page)
                if find(CHALLENGE, hay) or find(BAD, hay):
                    break
                if "speedbump" in u or "workspaceterms" in u.lower() or "terms" in u.lower():
                    # GULIR dulu: tombol setuju tersembunyi di bawah teks panjang.
                    klik_setuju_kalau_ada(page)
                    time.sleep(1.2)
                if not click_next(page):
                    break
                time.sleep(5)'''
baru3 = '''            for _ in range(8):
                u = page.url
                if "accounts.google.com" not in u:
                    break
                hay = text_low(page)
                if find(CHALLENGE, hay) or find(BAD, hay):
                    break
                if "speedbump" in u or "terms" in u.lower():
                    # GULIR dulu + ULANG sampai benar-benar berpindah.
                    if klik_setuju_kalau_ada(page, log_fn=log):
                        continue
                    time.sleep(2)
                    continue
                if not click_next(page):
                    time.sleep(1.5)
                    continue
                time.sleep(3)'''
if lama3 not in src:
    gagal("anchor loop kedua tidak ditemukan")
src = src.replace(lama3, baru3, 1)
print("  ~ loop kedua: sabar + ulang (8 putaran)")

# Setelah tanda tangan: tambah waktu muat halaman
lama4 = "            time.sleep(3)\n            url = page.url"
baru4 = "            # Beri waktu halaman akun selesai dimuat sebelum diperiksa.\n            time.sleep(6)\n            url = page.url"
if lama4 in src:
    src = src.replace(lama4, baru4, 1)
    print("  ~ jeda 3s -> 6s sebelum periksa hasil")

CF.write_text(src)
try:
    ast.parse(src)
    print("  AST: OK")
except SyntaxError as e:
    gagal(f"SyntaxError baris {e.lineno}: {e.msg}")

r = subprocess.run(["/root/harbor/venv/bin/python", "-c",
                    "import ast,sys; ast.parse(open(sys.argv[1]).read())", str(CF)],
                   capture_output=True, text=True)
print("  parse (venv harbor):", "OK" if r.returncode == 0 else r.stderr[:200])
print(f"\nrollback: cp {BAK.name} camoufox_login.py")