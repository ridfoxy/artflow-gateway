#!/usr/bin/env python3
"""GSuite auto-login engine for the Flow/Vids account pool.

Reads credentials from a file YOU create (never from chat, never hard-coded):

    /root/api-google-flow/accounts.txt
    # garis komentar diawali '#'
    email@domain.com|password

Each account gets its own persistent browser profile
(`chrome_profiles/<email>`), so once logged in the session survives restarts and
later jobs can reuse it without logging in again.

Usage:
    python3 gsuite_login.py --check                 # validate the file only
    python3 gsuite_login.py --email X --dry-run     # reach the login page, submit NOTHING
    python3 gsuite_login.py --email X               # real login
    python3 gsuite_login.py                         # every account in the file
    python3 gsuite_login.py --email X --test-vids   # login, then check Vids access

Exit codes: 0 all ok, 1 something failed, 2 verifikasi manual dibutuhkan.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path("/root/api-google-flow")
ACCOUNTS_FILE = APP_DIR / "accounts.txt"
PROFILES_DIR = APP_DIR / "chrome_profiles"
DB_PATH = APP_DIR / "data" / "gateway.db"
STATUS_FILE = APP_DIR / "data" / "login_status.json"
LOG_DIR = APP_DIR / "data" / "login_logs"

VNC_URL = "http://104.64.15.167:6080/vnc.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")

# Google shows these when it wants a human (2FA, "verify it's you", captcha,
# unusual activity). We must STOP, not retry — retrying burns the account.
CHALLENGE_MARKERS = [
    "verify it's you", "verifikasi bahwa ini anda", "confirm it's you",
    "2-step verification", "verifikasi 2 langkah", "verifikasi dua langkah",
    "enter the code", "masukkan kode", "get a verification code",
    "check your phone", "periksa ponsel", "tap yes", "ketuk ya",
    "unusual activity", "aktivitas yang tidak biasa",
    "captcha", "reCAPTCHA", "try again later", "coba lagi nanti",
    # Halaman CAPTCHA Google (teks ID/EN) — kata "captcha" sendiri tidak muncul,
    # yang tampil hanya gambar + kalimat perintah ini.
    "ketik teks yang anda dengar atau lihat", "ketik teks yang anda lihat",
    "harap masukkan karakter", "masukkan karakter yang anda lihat",
    "type the text you hear or see", "enter the characters you see",
    "teks yang anda dengar atau lihat", "gambar di atas",
    "couldn't find your google account", "tidak dapat menemukan akun google anda",
    "wrong password", "kata sandi salah", "password salah",
    "this browser or app may not be secure",
]

BAD_MARKERS = [
    "wrong password", "kata sandi salah", "password salah",
    "couldn't find your google account", "tidak dapat menemukan akun google anda",
    "this browser or app may not be secure",
    "account has been disabled", "akun anda telah dinonaktifkan",
]


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_accounts(path=ACCOUNTS_FILE):
    """Parse `email|password` lines. Returns (entries, errors)."""
    entries, errors = [], []
    if not path.exists():
        return [], [f"file tidak ada: {path}"]
    seen = set()
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            errors.append(f"baris {i}: tidak ada pemisah '|'")
            continue
        email, _, password = line.partition("|")
        email, password = email.strip(), password.strip()
        if "@" not in email:
            errors.append(f"baris {i}: email tidak valid")
            continue
        if not password:
            errors.append(f"baris {i}: password kosong")
            continue
        if email.lower() in seen:
            errors.append(f"baris {i}: email duplikat ({email})")
            continue
        seen.add(email.lower())
        entries.append({"email": email, "password": password, "line": i})
    return entries, errors


def file_mode_warning(path=ACCOUNTS_FILE):
    if not path.exists():
        return None
    mode = oct(path.stat().st_mode)[-3:]
    if mode != "600":
        return f"peringatan: permission {mode}, sebaiknya 600 (chmod 600 {path})"
    return None


# --------------------------------------------------------------------------- #
# DB / status bookkeeping
# --------------------------------------------------------------------------- #

def record_status(email, state, detail="", cookies=None):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text())
        except Exception:
            data = {}
    data[email] = {"state": state, "detail": detail,
                   "at": datetime.now(timezone.utc).isoformat()}
    STATUS_FILE.write_text(json.dumps(data, indent=2))


def clear_stale_vids_state(email):
    """Hapus state basi di vids_accounts untuk akun yang sudah login sukses.

    `/v1/vids/accounts` membaca tabel vids_accounts; kalau baris lama
    (no_login/ui_retry) dibiarkan, akun yang SUDAH sehat tetap tampil mati di
    panel admin — dan tombol Hapus Akun Mati bisa membuang akun sehat.
    """
    import sqlite3 as _sq
    vids_db = APP_DIR / "data" / "vids_jobs.db"
    if not vids_db.exists():
        return False
    try:
        conn = _sq.connect(str(vids_db))
        conn.execute("DELETE FROM vids_accounts WHERE email=?", (email,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log(f"  gagal bersihkan state basi {email}: {e}")
        return False


def register_in_pool(email, cookies_json="[]"):
    """Make the account visible to the gateway pool (session lives in the profile)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        now = str(int(time.time()))
        conn.execute(
            """INSERT INTO accounts (email, cookies_raw, access_token, at_expires,
                                     status, is_active, created_at, updated_at)
               VALUES (?, ?, '', '', 'active', 1, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                   status='active', is_active=1, updated_at=excluded.updated_at""",
            (email, cookies_json, now, now))
        conn.commit()
        conn.close()
        log(f"  terdaftar di pool gateway: {email}")
        # State basi di vids_accounts harus dibuang, kalau tidak panel admin
        # menganggap akun ini masih mati.
        clear_stale_vids_state(email)
        return True
    except Exception as e:
        log(f"  gagal mendaftar ke pool: {e}")
        return False


# --------------------------------------------------------------------------- #
# Login flow
# --------------------------------------------------------------------------- #

def page_text(page):
    try:
        return (page.inner_text("body") or "")
    except Exception:
        return ""


def find_challenge(text):
    low = text.lower()
    for m in CHALLENGE_MARKERS:
        if m.lower() in low:
            return m
    return None


def find_bad(text):
    low = text.lower()
    for m in BAD_MARKERS:
        if m.lower() in low:
            return m
    return None


def _probe_ms():
    """Timeout probe selector (ms). Kecil supaya akun yang sudah login tidak
    membuang 3 x 20 s menunggu kolom yang tidak akan muncul."""
    try:
        return max(500, int(os.environ.get("GS_LOGIN_PROBE_MS", "4000")))
    except Exception:
        return 4000


def _present_now(page, selectors):
    """Selector yang LANGSUNG ada di DOM (tanpa menunggu sama sekali)."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return sel
        except Exception:
            continue
    return None


def fill_first(page, selectors, value, timeout=None):
    """Isi kolom pertama yang muncul.

    Cek DOM dulu (instan) sebelum menunggu: satu selector yang tidak ada
    dahulu memakan satu timeout penuh, jadi urutan ini menghemat puluhan detik
    per akun ketika kolomnya memang sudah ada.
    """
    timeout = _probe_ms() if timeout is None else timeout

    sel = _present_now(page, selectors)
    if sel:
        try:
            el = page.query_selector(sel)
            el.click()
            el.fill("")
            el.type(value, delay=25)
            return sel
        except Exception:
            pass

    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                el.click()
                el.fill("")
                el.type(value, delay=25)
                return sel
        except Exception:
            continue
    return None


def click_first(page, selectors, timeout=None):
    timeout = _probe_ms() if timeout is None else timeout
    sel = _present_now(page, selectors)
    if sel:
        try:
            page.query_selector(sel).click()
            return sel
        except Exception:
            pass
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                el.click()
                return sel
        except Exception:
            continue
    return None


IDENTIFIER_SELECTORS = [
    'input[type="email"]',
    'input[name="identifier"]',
    '#identifierId',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="Passwd"]',
    '#password input',
]
NEXT_SELECTORS = [
    '#identifierNext button',
    '#identifierNext',
    'button:has-text("Next")',
    'button:has-text("Berikutnya")',
    'button:has-text("Lanjut")',
]
PWD_NEXT_SELECTORS = [
    '#passwordNext button',
    '#passwordNext',
    'button:has-text("Next")',
    'button:has-text("Berikutnya")',
    'button:has-text("Masuk")',
    'button:has-text("Sign in")',
]


def wait_for_step(page, selectors=(), markers=(), timeout=45, poll=1.5):
    """Poll until a selector appears, a marker text shows, or the page moves.

    Google's sign-in flow swaps pages with variable latency (2s-20s+), so a
    single check is not enough — that caused the password field to be declared
    missing while the page was still on the identifier step.

    Returns (kind, found) where kind is 'selector' | 'marker' | 'timeout'.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if selectors:
            for sel in selectors:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        return "selector", sel
                except Exception:
                    pass
        if markers:
            txt = page_text(page)
            low = txt.lower()
            for m in markers:
                if m.lower() in low:
                    return "marker", m
        time.sleep(poll)
    return "timeout", None


def cleanup_stale_profile(email):
    """Lepaskan profil yang masih dipegang proses Chrome mati.

    HANYA proses yang memakai profil akun ini yang disentuh — browser milik
    gateway (akun listbalck31) tidak boleh terganggu.
    """
    import glob as _glob
    profile = PROFILES_DIR / email
    if not profile.exists():
        return 0
    path = str(profile)
    killed = 0
    try:
        for pid_s in os.listdir("/proc"):
            if not pid_s.isdigit():
                continue
            try:
                with open(f"/proc/{pid_s}/cmdline", "rb") as fh:
                    cmd = fh.read().decode("utf-8", "replace")
            except Exception:
                continue
            if path in cmd and "chrome" in cmd.lower():
                try:
                    os.kill(int(pid_s), 15)
                    killed += 1
                except Exception:
                    pass
    except Exception:
        pass
    if killed:
        log(f"  {killed} proses Chrome lama dilepas (profil {email})")
        time.sleep(3)

    # lock & socket sisa
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        f = profile / name
        try:
            if f.is_symlink() or f.exists():
                f.unlink()
        except Exception:
            pass
    return killed


def login_one(page, email, password, dry_run=False, test_vids=False,
              screenshots_dir=None):
    """Drive one Google login. Returns (state, detail).

    state: 'ok' | 'needs_human' | 'bad_credentials' | 'error' | 'dry_run'
    """
    def shot(tag):
        if not screenshots_dir:
            return
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", email)
        try:
            page.screenshot(path=str(screenshots_dir / f"{safe}-{tag}.png"),
                            full_page=False)
        except Exception:
            pass

    log("  buka halaman login Google…")
    try:
        page.goto("https://accounts.google.com/signin/v2/identifier"
                  "?flowName=GlifWebSignIn&flowEntry=ServiceLogin",
                  wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return "error", f"gagal membuka halaman login: {e}"

    # Cek instan dulu: kalau sesi sudah login, tidak ada gunanya menunggu
    # kolom email (dulu 3 x 20 s terbuang di sini).
    settle = 0.8
    try:
        settle = float(os.environ.get("GS_LOGIN_SETTLE", "1.2"))
    except Exception:
        pass
    try:
        page.wait_for_selector('input[type="email"], input[type="password"], #identifierId',
                               timeout=int(settle * 1000))
    except Exception:
        pass
    has_email_field = _present_now(page, IDENTIFIER_SELECTORS) is not None
    if not has_email_field:
        low_now = page_text(page).lower()
        already = ("akun google" in low_now or "google account" in low_now
                   or "myaccount" in page.url or "kelola akun" in low_now)
        if already:
            log("  sesi sudah login (dicek cepat, tanpa tunggu kolom email)")
            if test_vids:
                st, dt = check_vids_access(page)
                return ("ok" if st == "ok" else st), f"sesi lama; {dt}"
            return "ok", "sesi sudah login"

    time.sleep(settle)
    txt = page_text(page)
    if find_challenge(txt):
        shot("challenge-initial")
        return "needs_human", f"Google langsung meminta verifikasi: {find_challenge(txt)}"

    # ---- email step -------------------------------------------------------
    log("  isi email…")
    sel = fill_first(page, IDENTIFIER_SELECTORS, email, timeout=20000)
    if not sel:
        # tidak ada kolom email: kemungkinan sesi lama sudah login
        low = page_text(page).lower()
        if ("akun google" not in low and "google account" not in low) or \
                "myaccount" in page.url or "kelola akun" in low:
            shot("already-logged-in")
            log("  tidak ada kolom email — sesi sudah login, uji akses")
            if test_vids:
                st, dt = check_vids_access(page)
                return ("ok" if st == "ok" else st), f"sesi lama; {dt}"
            return "ok", "sesi sudah login"
        shot("no-email-field")
        return "error", f"kolom email tidak ditemukan (url={page.url[:90]})"
    time.sleep(0.8)
    click_first(page, NEXT_SELECTORS) or page.keyboard.press("Enter")

    if dry_run:
        time.sleep(3)
        shot("dry-run-email-step")
        return "dry_run", f"berhasil sampai langkah email (kolom: {sel}), tidak submit"

    # tunggu perpindahan halaman; kalau masih di identifier, submit sekali lagi
    kind, found = wait_for_step(
        page, selectors=PASSWORD_SELECTORS,
        markers=CHALLENGE_MARKERS + BAD_MARKERS, timeout=25)
    if kind == "timeout":
        log("  halaman belum berpindah, coba submit ulang…")
        click_first(page, NEXT_SELECTORS, timeout=5000) or page.keyboard.press("Enter")
        kind, found = wait_for_step(
            page, selectors=PASSWORD_SELECTORS,
            markers=CHALLENGE_MARKERS + BAD_MARKERS, timeout=25)

    txt = page_text(page)
    bad = find_bad(txt)
    if bad:
        shot("bad-after-email")
        return "bad_credentials", f"ditolak setelah email: {bad}"
    ch = find_challenge(txt)
    if ch and kind != "selector":
        shot("needs-human-after-email")
        return "needs_human", f"verifikasi setelah email: {ch}"
    if kind != "selector":
        shot("no-password-field")
        return "error", f"kolom password tidak muncul (url={page.url[:90]})"

    # ---- password step ----------------------------------------------------
    log(f"  isi password… (kolom: {found})")
    sel = fill_first(page, PASSWORD_SELECTORS, password, timeout=25000)
    if not sel:
        shot("no-password-field")
        return "error", f"kolom password tidak bisa diisi (url={page.url[:90]})"
    time.sleep(0.8)
    click_first(page, PWD_NEXT_SELECTORS) or page.keyboard.press("Enter")

    # tunggu hasil: pindah keluar dari accounts.google.com, muncul challenge,
    # atau muncul halaman persetujuan Workspace (yang cukup diklik setuju)
    deadline = time.time() + 45
    while time.time() < deadline:
        if "accounts.google.com" not in page.url:
            break
        if "speedbump" in page.url or "workspaceterms" in page.url.lower():
            break
        txt_now = page_text(page)
        if find_challenge(txt_now) or find_bad(txt_now):
            break
        time.sleep(1.5)

    # persetujuan Workspace: satu klik, lalu lanjutkan
    for _ in range(3):
        if not ("speedbump" in page.url or "workspaceterms" in page.url.lower()):
            break
        if not handle_workspace_terms(page, log):
            break
        d2 = time.time() + 30
        while time.time() < d2 and "accounts.google.com" in page.url:
            if "speedbump" not in page.url and "workspaceterms" not in page.url.lower():
                break
            time.sleep(1.5)
    time.sleep(3)

    # ---- evaluate the outcome --------------------------------------------
    url = page.url          # HARUS didefinisikan sebelum dipakai
    txt = page_text(page)
    # sudah login (sesi lama di profil): bukan kegagalan
    if any(k in url for k in ("myaccount.google.com", "accounts.google.com/b/0/",
                              "workspace.google.com")) or (
            "accounts.google.com" not in url and "myaccount" in page.url):
        log("  sesi sudah login — lanjut uji akses")
        if test_vids:
            st, dt = check_vids_access(page)
            return ("ok" if st == "ok" else st), f"sesi lama; {dt}"
        return "ok", "sesi sudah login"
    ch = find_challenge(txt)
    bad = find_bad(txt)
    url = page.url

    if bad:
        shot("bad-credentials")
        return "bad_credentials", bad
    if ch:
        shot("needs-human")
        return "needs_human", f"verifikasi dibutuhkan: {ch}"
    if "speedbump" in url or "workspaceterms" in url.lower():
        shot("stuck-workspaceterms")
        return "needs_human", ("berhenti di persetujuan Workspace — password BENAR, "
                               "perlu klik setuju (url singkat: workspaceterms)")
    if "accounts.google.com" in url:
        shot("stuck-accounts")
        return "error", f"masih di halaman Google (url={url[:90]})"

    log("  login berhasil, uji akses…")
    shot("logged-in")

    if test_vids:
        state, detail = check_vids_access(page)
        return ("ok" if state == "ok" else state), f"login ok; {detail}"
    return "ok", "login berhasil"


# Google Workspace meminta persetujuan persyaratan pada akun baru. Ini BUKAN
# kegagalan: password sudah benar, hanya perlu satu klik setuju.
WORKSPACE_TERMS_MARKERS = [
    "workspaceterms", "persyaratan", "terms of service",
    "google workspace", "persyaratan layanan",
    "saya setuju", "i agree", "setuju",
]
TERMS_ACCEPT_SELECTORS = [
    'button:has-text("Saya setuju")',
    'button:has-text("Saya Setuju")',
    'button:has-text("Setuju dan lanjutkan")',
    'button:has-text("Terima")',
    'button:has-text("I agree")',
    'button:has-text("Accept")',
    'button:has-text("Lanjutkan")',
    'button:has-text("Continue")',
    'button:has-text("Setuju")',
    '[role="button"]:has-text("Setuju")',
    'button[jsname]',
    'input[type="submit"]',
]


def handle_workspace_terms(page, log_fn=print):
    """Klik setuju kalau halaman persyaratan Workspace muncul.

    Returns True kalau ada yang diklik.
    """
    if "speedbump" not in page.url and "workspaceterms" not in page.url.lower():
        return False
    log_fn("  halaman persyaratan Workspace terdeteksi — klik setuju…")
    for sel in TERMS_ACCEPT_SELECTORS:
        try:
            el = page.wait_for_selector(sel, timeout=4000, state="visible")
            if el:
                el.click()
                log_fn(f"  diklik: {sel}")
                time.sleep(5)
                return True
        except Exception:
            continue
    # fallback: klik elemen teks setuju mana pun
    try:
        clicked = page.evaluate("""() => {
            const want = ['setuju','agree','terima','accept','lanjutkan','continue'];
            const els = [...document.querySelectorAll('button,[role="button"],a')];
            for (const e of els) {
                const t = (e.innerText||'').toLowerCase().trim();
                if (e.offsetParent && want.some(w=>t.includes(w))) { e.click(); return t.slice(0,40); }
            }
            return null;
        }""")
        if clicked:
            log_fn(f"  diklik (fallback): {clicked}")
            time.sleep(5)
            return True
    except Exception:
        pass
    return False


VIDS_TEST_URL = "https://docs.google.com/videos/"


def check_vids_access(page, doc_url=None):
    """Confirm Google Vids opens for this account (not redirected to sign-in)."""
    try:
        page.goto(doc_url or VIDS_TEST_URL, wait_until="domcontentloaded",
                  timeout=60000)
    except Exception as e:
        return "error", f"gagal membuka Vids: {e}"
    # Tunggu sampai halaman Vids benar-benar siap, bukan 6 detik tetap.
    vids_settle = 2.0
    try:
        vids_settle = float(os.environ.get("GS_VIDS_SETTLE", "2.0"))
    except Exception:
        pass
    try:
        page.wait_for_selector('[aria-label*="Prompt"], [aria-label*="prompt"], '
                               'textarea, [contenteditable="true"], [role="textbox"]',
                               timeout=15000)
    except Exception:
        time.sleep(vids_settle)
    url = page.url
    txt = page_text(page)
    if "accounts.google.com" in url:
        return "needs_human", "Vids meminta login lagi (sesi tidak tersimpan)"
    low = txt.lower()
    if ("vids" in low) or ("buat video ai" in low) or ("video tanpa judul" in low):
        return "ok", f"Vids terbuka (title={page.title()[:60]})"
    return "error", f"Vids terbuka tapi tidak dikenali (title={page.title()[:60]})"


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #

def cleanup_orphans(email, grace=1.5):
    """Bunuh proses chrome sisa yang masih memakai profil akun ini.

    browser.close() tidak selalu mematikan seluruh keturunan; sisa proses dari
    banyak akun menumpuk lalu menjenuhkan mesin. Hanya proses dengan
    --user-data-dir=<profil akun ini> yang disentuh.
    """
    import subprocess as _sp
    import time as _t
    prof = str(PROFILES_DIR / email)
    try:
        out = _sp.run(["ps", "-eo", "pid,cmd"], capture_output=True, text=True,
                      timeout=15).stdout
    except Exception:
        return 0
    pids = []
    for line in out.splitlines():
        if "chrome" not in line or prof not in line:
            continue
        parts = line.split(None, 1)
        if parts and parts[0].isdigit():
            pids.append(parts[0])
    if not pids:
        return 0
    _t.sleep(grace)                      # beri kesempatan keluar sendiri
    hidup = []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                if prof.encode() in fh.read():
                    hidup.append(pid)
        except Exception:
            pass
    for pid in hidup:
        try:
            _sp.run(["kill", "-9", pid], timeout=5)
        except Exception:
            pass
    if hidup:
        log(f"  bersihkan {len(hidup)} proses chrome yatim")
    return len(hidup)


def run(entries, args):
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as p:
        for acct in entries:
            email, password = acct["email"], acct["password"]
            log(f"== {email} (baris {acct['line']}) ==")
            profile = PROFILES_DIR / email
            profile.mkdir(parents=True, exist_ok=True)
            # lepaskan profil yang masih dipegang proses Chrome mati
            cleanup_stale_profile(email)

            browser = None
            try:
                browser = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=False,          # headed on Xvfb: far less suspicious
                    args=[
                        "--no-sandbox", "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        # HEMAT (terukur: 8->6 proses, 348->164 MB per instance):
                        # satukan renderer per situs, batasi renderer, matikan
                        # gpu & layanan latar yang tidak dipakai saat login.
                        "--process-per-site",
                        "--renderer-process-limit=1",
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-sync",
                        "--disable-default-apps",
                        "--disable-component-update",
                        "--no-first-run",
                        "--disable-features=Translate,MediaRouter,OptimizationHints",
                    ],
                    viewport={"width": 1280, "height": 800},
                    user_agent=UA,
                    locale="id-ID",
                    timezone_id="Asia/Jakarta",
                )
                page = browser.pages[0] if browser.pages else browser.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

                state, detail = login_one(
                    page, email, password,
                    dry_run=args.dry_run,
                    test_vids=args.test_vids,
                    screenshots_dir=LOG_DIR)

                log(f"  -> {state}: {detail}")
                results[email] = {"state": state, "detail": detail}
                record_status(email, state, detail)

                if state == "ok" and not args.dry_run:
                    register_in_pool(email)
            except Exception as e:
                log(f"  -> error: {e}")
                results[email] = {"state": "error", "detail": str(e)[:200]}
                record_status(email, "error", str(e)[:200])
            finally:
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
                # Proses Chrome sering tertinggal setelah close(); sisa 65 akun
                # menumpuk dan menjenuhkan mesin (load pernah 95). Bunuh sisa
                # yang memakai profil akun ini saja — jangan sentuh browser
                # gateway (profil berbeda).
                if not args.dry_run:
                    cleanup_orphans(email)

            if len(entries) > 1:
                time.sleep(5)

    print()
    log("=== RINGKASAN ===")
    for email, r in results.items():
        log(f"  {email}: {r['state']} — {r['detail'][:110]}")

    if any(r["state"] == "needs_human" for r in results.values()):
        log(f"\nAkun yang butuh verifikasi manual: buka {VNC_URL}")
        log("Login manual di browser itu (profil akun sudah disiapkan), lalu jalankan ulang.")
        return 2
    return 1 if any(r["state"] not in ("ok", "dry_run") for r in results.values()) else 0


def main():
    ap = argparse.ArgumentParser(description="GSuite auto-login untuk pool Flow/Vids")
    ap.add_argument("--email", help="hanya proses email ini")
    ap.add_argument("--dry-run", action="store_true",
                    help="sampai langkah email saja, tidak submit apa pun")
    ap.add_argument("--test-vids", action="store_true",
                    help="setelah login, cek akses Google Vids")
    ap.add_argument("--file", default=str(ACCOUNTS_FILE),
                    help=f"file kredensial (default {ACCOUNTS_FILE})")
    args = ap.parse_args()

    path = Path(args.file)
    entries, errors = load_accounts(path)

    print("=== GSUITE AUTO LOGIN ===")
    print(f"file kredensial : {path}")
    for e in errors:
        print(f"  ! {e}")
    warn = file_mode_warning(path)
    if warn:
        print(f"  ! {warn}")

    if not entries:
        print("\nTidak ada akun yang bisa diproses.")
        print("\nBuat file-nya dulu (password TIDAK boleh lewat chat):")
        print(f"  cat > {path} <<'EOF'")
        print("  # email|password")
        print("  akun@domain.com|sandi")
        print("  EOF")
        print(f"  chmod 600 {path}")
        return 1

    print(f"akun ditemukan  : {len(entries)}")
    if args.email:
        entries = [e for e in entries if e["email"].lower() == args.email.lower()]
        if not entries:
            print(f"  ! {args.email} tidak ada di file")
            return 1
    print(f"akan diproses   : {len(entries)} akun")
    print(f"dry-run         : {args.dry_run}")
    print(f"test Vids       : {args.test_vids}")
    print()

    return run(entries, args)


if __name__ == "__main__":
    sys.exit(main())