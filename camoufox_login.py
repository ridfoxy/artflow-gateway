#!/usr/bin/env python3
"""Jalur CADANGAN Camoufox untuk login akun yang diblokir CAPTCHA Chromium.

Terbukti dari uji:
  - Chromium pada akun drkvpj@gumdel.com -> CAPTCHA (teks merah terdistorsi)
  - Camoufox pada akun yang sama -> halaman login bersih, sandi terkirim,
    halaman akhir "Selamat datang di akun baru Anda"

Cara pakai (dipanggil login_all.py bila Chromium kena tantangan):
    DISPLAY=:99 /root/harbor/venv/bin/python camoufox_login.py --email X
Keluaran sengaja MENIRU gsuite_login.py supaya login_all bisa membacanya:
    "  -> ok: <detail>"
    "  -> needs_human: <detail>"
    "  -> bad_credentials: <detail>"
    "  -> error: <detail>"

Flag:
    --email EMAIL        akun yang diproses (wajib)
    --test-vids          setelah login, buka Google Vids untuk verifikasi
    --profile DIR        pakai profil tertentu (default sama seperti Chromium)
    --timeout SEC        batas total (default 240)

Python: HARUS dari /root/harbor/venv (tempat camoufox 0.5.6 terpasang).
"""
import argparse
import os
import sys
import time
from pathlib import Path

APP_DIR = Path("/root/api-google-flow")

# Camoufox mengambil mode headless bila DISPLAY tidak ada. Gateway memakai
# Xvfb :99, jadi set di sini supaya tidak jalan tanpa tampilan (mudah terdeteksi).
os.environ.setdefault("DISPLAY", ":99")
ACCOUNTS = APP_DIR / "accounts.txt"
PROFILES = APP_DIR / "chrome_profiles"
VIDS_URL = "https://docs.google.com/videos/"
SIGNIN = ("https://accounts.google.com/signin/v2/identifier"
          "?flowName=GlifWebSignIn&flowEntry=ServiceLogin")

CHALLENGE = [
    "ketik teks yang anda dengar atau lihat", "harap masukkan karakter",
    "masukkan karakter yang anda lihat", "gambar di atas",
    "type the text you hear or see", "enter the characters you see",
    "aktivitas yang tidak biasa", "unusual activity",
    "verifikasi 2 langkah", "dua langkah", "masukkan kode",
    "periksa ponsel", "captcha", "recaptcha",
]
BAD = [
    "kata sandi salah", "password salah", "wrong password",
    "tidak dapat menemukan akun google", "couldn't find your google account",
    "akun anda telah dinonaktifkan", "account has been disabled",
]
TERMS = [
    'button:has-text("Saya setuju")', 'button:has-text("Setuju")',
    'button:has-text("Terima")', 'button:has-text("Lanjutkan")',
    'button:has-text("I agree")', 'button:has-text("Accept")',
    'button:has-text("Continue")',
]


def log(m):
    print(m, flush=True)


def load_account(email):
    for line in ACCOUNTS.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        e, p = line.split("|", 1)
        if e.strip().lower() == email.lower():
            return e.strip(), p.strip()
    return email, ""


def text_low(page):
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def find(needles, hay):
    for n in needles:
        if n in hay:
            return n
    return None


def visible(page, sel):
    try:
        el = page.query_selector(sel)
        return bool(el and el.is_visible())
    except Exception:
        return False


def fill_visible(page, selectors, value):
    """Isi kolom pertama yang BENAR-BENAR TERLIHAT.

    Penting: Google menyimpan input sandi tersembunyi (name="hiddenPassword",
    aria-hidden="true") di DOM. `page.fill` pada selector umum akan menyasar
    elemen itu, gagal karena tak terlihat, lalu menggantung sampai timeout.
    """
    for sel in selectors:
        try:
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    el.click()
                    el.fill("")
                    el.fill(value)
                    return sel
        except Exception:
            continue
    return None


def scroll_to_bottom(page, rounds=14):
    """Gulir semua wadah yang bisa digulir sampai dasar halaman.

    Halaman persyaratan Workspace menyembunyikan tombol setuju sampai teksnya
    habis dibaca — tanpa menggulir, klik tidak akan pernah menemukan tombolnya.
    """
    for _ in range(rounds):
        try:
            page.keyboard.press("End")
        except Exception:
            pass
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
        time.sleep(0.5)


def click_next(page):
    """Klik tombol lanjut yang NYATA.

    Kunci keberhasilan (terbukti): Google tidak memindahkan halaman bila hanya
    menekan Enter — tombol "Berikutnya"/"Next" harus benar-benar diklik.
    """
    for label in ("Berikutnya", "Next", "Lanjutkan", "Continue", "Setuju",
                  "Saya setuju", "Terima", "I agree", "Accept"):
        try:
            el = page.get_by_role("button", name=label, exact=False).first
            if el and el.is_visible():
                el.click(timeout=5000)
                return True
        except Exception:
            continue
    try:
        got = page.evaluate("""() => {
            const want = ['berikutnya','next','lanjutkan','continue','setuju','agree','terima','accept'];
            const els = [...document.querySelectorAll('button,[role="button"],input[type="submit"]')];
            for (const e of els) {
                const t = (e.innerText || e.value || '').toLowerCase().trim();
                if (e.offsetParent && !e.disabled && t && want.some(w => t.includes(w))) {
                    e.click(); return t.slice(0,30);
                }
            }
            return null;
        }""")
        return bool(got)
    except Exception:
        return False


def accept_terms(page):
    """Klik tombol persetujuan / lanjut di halaman antara."""
    return click_next(page)


def check_vids(page):
    """Buka Vids dan pastikan sesinya benar-benar tersimpan.

    Sesi Google ditulis ke profil saat halaman sungguhan dimuat. Membuka Vids
    terlalu cepat membuat cookie sesi belum tersimpan sehingga Vids meminta
    login lagi — jadi beri jeda, buka antarmuka akun dulu, lalu Vids.
    """
    try:
        page.goto("https://myaccount.google.com/",
                  wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
    except Exception:
        pass
    try:
        page.goto(VIDS_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return "error", f"gagal buka Vids: {str(e)[:80]}"

    # Tunggu sampai benar-benar masuk (url keluar dari accounts) atau editor siap.
    deadline = time.time() + 25
    while time.time() < deadline:
        if "accounts.google.com" not in page.url:
            break
        time.sleep(2)

    try:
        page.wait_for_selector('[contenteditable="true"], textarea, '
                               '[role="textbox"], [aria-label*="rompt"]',
                               timeout=15000)
    except Exception:
        time.sleep(2)

    if "accounts.google.com" in page.url:
        return "needs_human", "Vids minta login lagi (sesi tidak tersimpan)"
    try:
        title = page.title()
    except Exception:
        title = ""
    return "ok", f"Vids terbuka (title={title[:40]})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--test-vids", action="store_true")
    ap.add_argument("--profile", default="")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    email, password = load_account(args.email)
    profile = Path(args.profile) if args.profile else (PROFILES / email)
    profile.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + args.timeout

    from camoufox.sync_api import Camoufox
    from camoufox.utils import launch_options

    log(f"=== CAMOUFOX LOGIN: {email} ===")
    # Opsi dirakit lewat launch_options() supaya user_data_dir benar-benar
    # sampai ke launch_persistent_context. Tanpa ini Camoufox memakai profil
    # SEMENTARA (/tmp/playwright_firefoxdev_profile-*) dan sesi tidak tersimpan.
    opts = launch_options(
        user_data_dir=str(profile),
        headless=False,
        humanize=True,
        locale="id-ID",
        os=["windows"],
        i_know_what_im_doing=True,
    )
    # Camoufox menerima persistent_context sebagai argumen SENDIRI; kalau kunci
    # ini ikut di opts, Camoufox melempar "Unknown option". Jadi dibuang.
    opts.pop("persistent_context", None)   # dibuang: Camoufox menolaknya
    opts["user_data_dir"] = str(profile)      # dipastikan, jangan hilang
    opts["headless"] = False                  # headed di Xvfb: lebih aman
    log(f"  profil: {profile}")
    try:
        # persistent_context=True WAJIB di sini: tanpa itu NewBrowser memakai
        # playwright.firefox.launch() dengan profil SEMENTARA
        # (/tmp/playwright_firefoxdev_profile-*) sehingga sesi tidak tersimpan.
        with Camoufox(persistent_context=True, **opts) as browser:
            page = browser.pages[0] if browser.pages else browser.new_page()

            page.goto(SIGNIN, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector(
                    'input[type="email"], input[type="password"], #identifierId',
                    timeout=8000)
            except Exception:
                pass

            hay = text_low(page)
            cap = find(CHALLENGE, hay)
            if cap:
                log(f"  -> needs_human: Camoufox juga kena tantangan ({cap})")
                return 2

            v_email = visible(page, 'input[type="email"]') or visible(page, "#identifierId")
            v_pwd = visible(page, 'input[type="password"]')

            if not v_email and not v_pwd:
                if "myaccount" in page.url or "kelola akun" in hay or "akun google" in hay:
                    log("  sesi sudah login")
                    if args.test_vids:
                        st, dt = check_vids(page)
                        log(f"  -> {st}: sesi lama; {dt}")
                        return 0 if st == "ok" else 2
                    log("  -> ok: sesi sudah login")
                    return 0
                log(f"  -> error: tidak ada kolom login (url={page.url[:70]})")
                return 1

            if v_email:
                fill_visible(page, ['input[type="email"]', "#identifierId"], email)
                time.sleep(0.6)
                # KLIK tombol, jangan Enter: terbukti Enter tidak memindahkan halaman.
                click_next(page)
                try:
                    page.wait_for_selector('input[type="password"]', timeout=25000)
                except Exception:
                    pass
                hay = text_low(page)
                cap = find(CHALLENGE, hay)
                if cap:
                    log(f"  -> needs_human: tantangan setelah email ({cap})")
                    return 2
                bad = find(BAD, hay)
                if bad:
                    log(f"  -> bad_credentials: {bad}")
                    return 3
                if not visible(page, 'input[type="password"]'):
                    log("  -> error: kolom sandi tidak muncul")
                    return 1

            if not password:
                log("  -> error: sandi tidak ada di accounts.txt")
                return 1

            got = fill_visible(page, ['input[type="password"]'], password)
            if not got:
                log("  -> error: kolom sandi terlihat tidak ditemukan")
                return 1
            time.sleep(0.6)
            # KLIK tombol lanjut — inti keberhasilan jalur Camoufox.
            click_next(page)
            time.sleep(5)

            # Tunggu keluar dari accounts.google.com, atau muncul persetujuan.
            while time.time() < deadline:
                url = page.url
                if "accounts.google.com" not in url:
                    break
                hay = text_low(page)
                if find(CHALLENGE, hay) or find(BAD, hay):
                    break
                if "speedbump" in url or "workspaceterms" in url.lower():
                    break
                time.sleep(1.5)

            # Selesaikan halaman antara (persetujuan/konfirmasi).
            # Halaman syarat Workspace perlu DIGULIR sampai dasar dulu: tombol
            # setujunya baru muncul setelah teksnya habis dibaca.
            for _ in range(6):
                u = page.url
                if "accounts.google.com" not in u:
                    break
                hay = text_low(page)
                if find(CHALLENGE, hay) or find(BAD, hay):
                    break
                if "speedbump" in u or "workspaceterms" in u.lower():
                    scroll_to_bottom(page)
                if not click_next(page):
                    break
                time.sleep(5)

            time.sleep(3)
            url = page.url
            hay = text_low(page)

            cap = find(CHALLENGE, hay)
            if cap:
                log(f"  -> needs_human: tantangan setelah sandi ({cap})")
                return 2
            bad = find(BAD, hay)
            if bad:
                log(f"  -> bad_credentials: {bad}")
                return 3

            if "accounts.google.com" in url:
                log(f"  -> error: masih di halaman akun (url={url[:70]})")
                return 1

            # Login berhasil -> daftarkan ke pool supaya tidak dianggap mati.
            try:
                sys.path.insert(0, str(APP_DIR))
                import gsuite_login as G
                G.register_in_pool(email)
            except Exception as e:
                log(f"  ! gagal daftar pool: {str(e)[:80]}")

            if args.test_vids:
                st, dt = check_vids(page)
                log(f"  -> {st}: camoufox; {dt}")
                return 0 if st == "ok" else 2

            log("  -> ok: login berhasil via Camoufox")
            return 0
    except Exception as e:
        log(f"  -> error: {str(e)[:150]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())