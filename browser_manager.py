"""
Remote Browser Manager — Playwright + Xvfb + x11vnc + noVNC.

Provides a remote browser session on the VPS that users access via noVNC.
After user logs into Google, captures cookies + ya29 token automatically.
Auto-refreshes token 1 hour before expiry.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import signal
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("browser_manager")

APP_DIR = Path(__file__).parent.resolve()
DATA_DIR = APP_DIR / "data"
PROFILES_DIR = APP_DIR / "chrome_profiles"
DB_PATH = DATA_DIR / "gateway.db"

PROFILES_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Display configuration
DISPLAY_NUM = 99
DISPLAY = f":{DISPLAY_NUM}"
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
VNC_PORT = 5999  # 5900 + DISPLAY_NUM
NOVNC_PORT = 6080
VNC_PASSWORD = "flowapi"

# Process handles
_xvfb_proc = None
_vnc_proc = None
_novnc_proc = None
# ─── Token refresh kill-switch ────────────────────────────────
# The refresh machinery closes the live browser context, which destroys every
# in-flight worker page. The Google session cookie lasts weeks and is what
# authenticates submissions, so refreshing is unnecessary during normal
# operation. Set FLOW_ENABLE_TOKEN_REFRESH=1 to re-enable.
ENABLE_TOKEN_REFRESH = os.environ.get("FLOW_ENABLE_TOKEN_REFRESH", "0") == "1"

_browser_context = None

# ── Pemilihan profil per akun (di belakang FLOW_ENABLE_ACCOUNT_SWITCH) ──────
# Satu konteks browser per akun, tiap konteks memakai profilnya sendiri. Kalau
# saklar mati, semua kembali memakai _browser_context tunggal seperti semula.
ACCOUNT_CONTEXTS = {}
# Objek Camoufox (pemegang konteks) per akun — wajib disimpan supaya bisa
# ditutup dengan benar (Camoufox bukan Playwright biasa).
ACCOUNT_CONTEXTS_CM = {}          # email -> BrowserContext
ACCOUNT_LOCK = None            # diisi asyncio.Lock saat dipakai


def account_switch_enabled():
    return os.environ.get("FLOW_ENABLE_ACCOUNT_SWITCH", "0").strip() in ("1", "true", "yes")


async def get_context_for(account_email=None):
    """Konteks browser untuk akun tertentu.

    Saklar mati ATAU belum ada profil akun itu -> konteks tunggal seperti semula,
    sehingga job tidak pernah gagal hanya karena profil belum disiapkan.
    """
    global ACCOUNT_CONTEXT, ACCOUNT_LOCK
    if not account_switch_enabled() or not account_email:
        return _browser_context
    email = str(account_email).strip()
    if not email:
        return _browser_context
    profile_dir = PROFILES_DIR / email
    if not profile_dir.exists():
        logger.warning(f"profil akun {email} belum ada — pakai konteks tunggal")
        return _browser_context
    if email in ACCOUNT_CONTEXTS:
        ctx = ACCOUNT_CONTEXTS[email]
        try:
            if ctx.pages:
                return ctx
        except Exception:
            pass
        ACCOUNT_CONTEXTS.pop(email, None)
    # luncurkan konteks akun ini
    if ACCOUNT_LOCK is None:
        import asyncio as _a
        ACCOUNT_LOCK = _a.Lock()
    async with ACCOUNT_LOCK:
        if email in ACCOUNT_CONTEXTS:
            return ACCOUNT_CONTEXTS[email]
        logger.info(f"meluncurkan konteks browser untuk akun {email} "
                    f"(mesin={_BROWSER_ENGINE})")
        # ── MESIN CAMOUFOX ──────────────────────────────────────────
        if _BROWSER_ENGINE == "camoufox":
            hasil = await _luncurkan_camoufox(email)
            if hasil:
                cm, ctx = hasil
                if not ctx.pages:
                    await ctx.new_page()
                ACCOUNT_CONTEXTS[email] = ctx
                ACCOUNT_CONTEXTS_CM[email] = cm        # simpan untuk penutupan
                logger.info(f"konteks CAMOUFOX siap: {email}")
                return ctx
            logger.warning("Camoufox gagal — turun ke Chromium untuk akun ini")
        # ── MESIN CHROMIUM (perilaku lama) ──────────────────────────
        pw = await async_playwright().start()
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                executable_path="/usr/bin/google-chrome-stable",
                headless=False,
                accept_downloads=True,
                downloads_path=str(APP_DIR / "output"),
                args=[
                    f"--display={DISPLAY}", "--no-sandbox", "--disable-gpu",
                    "--disable-dev-shm-usage",
                    f"--window-size={SCREEN_WIDTH},{SCREEN_HEIGHT}",
                    "--disable-blink-features=AutomationControlled",
                ],
                viewport={"width": SCREEN_WIDTH, "height": SCREEN_HEIGHT},
            )
            if not ctx.pages:
                await ctx.new_page()
            ACCOUNT_CONTEXTS[email] = ctx
            logger.info(f"konteks akun siap: {email}")
            return ctx
        except Exception as e:
            logger.error(f"gagal meluncurkan konteks {email}: {e}")
            return _browser_context


async def close_account_context(email):
    kunci = str(email).strip()
    cm = ACCOUNT_CONTEXTS_CM.pop(kunci, None)
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
            logger.info(f"konteks CAMOUFOX ditutup: {email}")
            ACCOUNT_CONTEXTS.pop(kunci, None)
            return True
        except Exception as e:
            logger.warning(f"gagal menutup Camoufox {email}: {str(e)[:100]}")
    ctx = ACCOUNT_CONTEXTS.pop(kunci, None)
    if ctx:
        try:
            await ctx.close()
            logger.info(f"konteks akun ditutup: {email}")
            return True
        except Exception:
            pass
    return False


# ── SAKLAR MESIN BROWSER (camoufox / chromium) ──────────────────────────
# Sesi Google TERIKAT pada mesin browser: sesi hasil login Camoufox (Firefox)
# tidak bisa dibaca Chromium (terbukti: cookie disuntik -> Google bilang
# "Telah logout"). Jadi browser yang login HARUS sama dengan yang generate.
_BROWSER_ENGINE = os.environ.get("FLOW_BROWSER", "camoufox").strip().lower()
PROFILES_FF = APP_DIR / "firefox_profiles"

# Camoufox dipasang di venv lain; tambahkan path-nya agar bisa diimpor dari
# proses gateway (Python 3.12 yang sama).
_CAMOUFOX_SITE = "/root/harbor/venv/lib/python3.12/site-packages"


def _pastikan_camoufox():
    """Impor AsyncCamoufox; tambahkan site-packages venv Camoufox bila perlu."""
    try:
        from camoufox.async_api import AsyncCamoufox   # noqa: F401
        return True
    except Exception:
        pass
    if _CAMOUFOX_SITE not in sys.path and os.path.isdir(_CAMOUFOX_SITE):
        sys.path.append(_CAMOUFOX_SITE)
    try:
        from camoufox.async_api import AsyncCamoufox   # noqa: F401
        return True
    except Exception as e:
        logger.error(f"Camoufox tidak bisa diimpor: {str(e)[:120]}")
        return False


async def _luncurkan_camoufox(email):
    """Buka konteks Camoufox persisten untuk satu akun.

    Memakai firefox_profiles/<email> — SAMA dengan camoufox_login.py — supaya
    sesi hasil login langsung terbaca. Mengembalikan objek Camoufox (pemegang
    konteks) atau None bila gagal.
    """
    if not _pastikan_camoufox():
        return None
    from camoufox.async_api import AsyncCamoufox
    from camoufox.utils import launch_options

    profil = PROFILES_FF / str(email).strip()
    profil.mkdir(parents=True, exist_ok=True)
    os.environ["DISPLAY"] = DISPLAY

    opts = launch_options(
        user_data_dir=str(profil),
        headless=False,
        humanize=True,
        locale="id-ID",
        os=["windows"],
        i_know_what_im_doing=True,
    )
    # Camoufox menerima persistent_context sebagai argumen SENDIRI.
    opts.pop("persistent_context", None)
    opts["user_data_dir"] = str(profil)
    opts["headless"] = False
    logger.info(f"meluncurkan konteks CAMOUFOX untuk {email} (profil {profil})")
    try:
        cm = AsyncCamoufox(persistent_context=True, **opts)
        ctx = await cm.__aenter__()
        return cm, ctx
    except Exception as e:
        logger.error(f"gagal meluncurkan Camoufox {email}: {str(e)[:150]}")
        return None


def list_account_contexts():
    return sorted(ACCOUNT_CONTEXTS.keys())

_browser_instance = None
_playwright_instance = None
_camoufox_stack = None   # pemegang konteks Camoufox (mode FLOW_BROWSER=camoufox)
_token_refresh_task = None
_cookie_monitor_task = None

# State
_current_session = {
    "email": None,
    "status": "idle",  # idle, browser_ready, logged_in, token_captured
    "access_token": None,
    "token_expires_at": None,
    "cookies": None,
    "last_error": None,
}


def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _start_xvfb():
    """Start Xvfb virtual framebuffer."""
    global _xvfb_proc
    if _xvfb_proc and _xvfb_proc.poll() is None:
        return True

    try:
        # Kill any existing Xvfb on this display
        subprocess.run(["pkill", "-f", f"Xvfb {DISPLAY}"], capture_output=True)
        time.sleep(0.5)

        _xvfb_proc = subprocess.Popen(
            [
                "Xvfb", DISPLAY,
                "-screen", "0", f"{SCREEN_WIDTH}x{SCREEN_HEIGHT}x24",
                "-ac",
                "-nolisten", "tcp",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        if _xvfb_proc.poll() is not None:
            logger.error("Xvfb failed to start")
            return False
        logger.info(f"Xvfb started on {DISPLAY}")
        return True
    except Exception as e:
        logger.error(f"Failed to start Xvfb: {e}")
        return False


def _start_vnc():
    """Start x11vnc to expose the Xvfb display."""
    global _vnc_proc
    if _vnc_proc and _vnc_proc.poll() is None:
        return True

    try:
        subprocess.run(["pkill", "-f", "x11vnc"], capture_output=True)
        time.sleep(0.5)

        # Create password file
        passwd_file = str(DATA_DIR / "vnc_passwd")
        subprocess.run(
            ["x11vnc", "-storepasswd", VNC_PASSWORD, passwd_file],
            capture_output=True,
        )

        _vnc_proc = subprocess.Popen(
            [
                "x11vnc",
                "-display", DISPLAY,
                "-rfbport", str(VNC_PORT),
                "-rfbauth", passwd_file,
                "-shared",
                "-forever",
                "-noxdamage",
                "-ncache", "10",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        if _vnc_proc.poll() is not None:
            logger.error("x11vnc failed to start")
            return False
        logger.info(f"x11vnc started on port {VNC_PORT}")
        return True
    except Exception as e:
        logger.error(f"Failed to start x11vnc: {e}")
        return False


def _start_novnc():
    """Start websockify/noVNC for browser-based VNC access."""
    global _novnc_proc
    if _novnc_proc and _novnc_proc.poll() is None:
        return True

    try:
        subprocess.run(["pkill", "-f", f"websockify.*{NOVNC_PORT}"], capture_output=True)
        time.sleep(0.5)

        novnc_dir = "/usr/share/novnc"
        _novnc_proc = subprocess.Popen(
            [
                "websockify",
                "--web", novnc_dir,
                str(NOVNC_PORT),
                f"localhost:{VNC_PORT}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        if _novnc_proc.poll() is not None:
            logger.error("noVNC/websockify failed to start")
            return False
        logger.info(f"noVNC started on port {NOVNC_PORT}")
        return True
    except Exception as e:
        logger.error(f"Failed to start noVNC: {e}")
        return False


async def _launch_browser(email: str = "default", force: bool = False):
    """Launch Chromium in the Xvfb display using Playwright.

    A healthy context with open pages is REUSED unless force=True. Closing it
    here destroys every in-flight worker page, which previously killed running
    jobs whenever a token refresh fired.
    """
    global _browser_context, _browser_instance, _playwright_instance

    if _browser_context and not force:
        try:
            if _browser_context.pages:
                logger.info(f"_launch_browser: reusing live context ({len(_browser_context.pages)} pages)")
                return True
        except Exception:
            pass  # context is dead; fall through and relaunch

    if _browser_context:
        try:
            await _browser_context.close()
        except Exception:
            pass
    if _browser_instance:
        try:
            await _browser_instance.close()
        except Exception:
            pass
    if _playwright_instance:
        try:
            await _playwright_instance.stop()
        except Exception:
            pass

    # ── MESIN CAMOUFOX ──────────────────────────────────────────────
    if _BROWSER_ENGINE == "camoufox":
        hasil = await _luncurkan_camoufox(email)
        if hasil:
            _camoufox_stack, _browser_context = hasil
            _browser_instance = None
            try:
                _browser_context.on("response", _on_response)
                _browser_context.on("response", _spy_response)
            except Exception:
                pass
            if len(_browser_context.pages) == 0:
                await _browser_context.new_page()
            logger.info(f"Browser CAMOUFOX siap untuk {email}")
            return True
        logger.warning("Camoufox gagal — turun ke Chromium")

    from playwright.async_api import async_playwright

    profile_dir = PROFILES_DIR / email
    profile_dir.mkdir(parents=True, exist_ok=True)

    _playwright_instance = await async_playwright().start()

    os.environ["DISPLAY"] = DISPLAY

    # Use launch_persistent_context so cookies + login survive restarts
    _browser_context = await _playwright_instance.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        executable_path="/usr/bin/google-chrome-stable",
        headless=False,
        accept_downloads=True,
        downloads_path="/root/api-google-flow/output",
        args=[
            f"--display={DISPLAY}",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            f"--window-size={SCREEN_WIDTH},{SCREEN_HEIGHT}",
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
        viewport={"width": SCREEN_WIDTH, "height": SCREEN_HEIGHT},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    )
    # _browser_instance is not used with persistent context — context IS the browser
    _browser_instance = None

    # Set up network interception for token capture
    _browser_context.on("response", _on_response)
    _browser_context.on("response", _spy_response)   # SEMENTARA

    if len(_browser_context.pages) == 0:
        page = await _browser_context.new_page()
    else:
        page = _browser_context.pages[0]

    # Only navigate to Google login if we're not already logged in
    current_url = page.url
    if "accounts.google.com" not in current_url and "flow.google.com" not in current_url and "labs.google" not in current_url:
        try:
            await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception as nav_err:
            logger.warning(f"Initial navigation failed ({nav_err}), continuing anyway — user can navigate via noVNC")

    logger.info(f"Browser launched (persistent) for {email}, navigate to noVNC to complete login")
    return True


# ── PENGINTAI JARINGAN (SEMENTARA, untuk diagnostik) ────────────────────
# Mencatat SEMUA respons: host, metode, ada authorization?, status, panjang.
# Dipakai untuk menjawab: apakah aisandbox-pa benar-benar dipanggil?
_SPY_NET = []


def spy_clear():
    _SPY_NET.clear()


async def _spy_response(response):
    """Catat SETIAP respons (bukan hanya aisandbox-pa)."""
    try:
        u = response.url or ""
        # abaikan aset statis agar tidak membanjiri
        if any(x in u for x in (".js", ".css", ".png", ".jpg", ".svg", ".woff",
                                ".gif", ".ico", "google-analytics",
                                "play.google.com/log", "gstatic.com")):
            return
        req = response.request
        auth = ""
        try:
            auth = (req.headers or {}).get("authorization", "") or ""
        except Exception:
            pass
        rec = {
            "host": (u.split("/")[2] if "//" in u else ""),
            "m": req.method,
            "status": response.status,
            "auth": auth[:60],
            "bearer": auth.startswith("Bearer ya29"),
            "url": u[:160],
        }
        _SPY_NET.append(rec)
        if len(_SPY_NET) > 400:
            del _SPY_NET[:100]
        if rec["bearer"]:
            logger.info(f"SPY: Bearer ya29 di {rec['host']} {rec['url'][:70]}")
    except Exception:
        pass


def spy_data():
    """Ringkasan isi pengintai."""
    host = {}
    for r in _SPY_NET:
        host[r["host"]] = host.get(r["host"], 0) + 1
    bearer = [r for r in _SPY_NET if r["bearer"]]
    ada_aisandbox = [r for r in _SPY_NET if "aisandbox" in (r["host"] or "")]
    return {
        "total": len(_SPY_NET),
        "per_host": host,
        "jumlah_bearer": len(bearer),
        "contoh_bearer": [{"host": r["host"], "auth": r["auth"][:40],
                           "url": r["url"][:90]} for r in bearer[:5]],
        "aisandbox_total": len(ada_aisandbox),
        "contoh_aisandbox": [{"status": r["status"], "auth": r["auth"][:30],
                              "url": r["url"][:90]} for r in ada_aisandbox[:5]],
        "contoh_semua": [{"host": r["host"], "m": r["m"], "status": r["status"],
                          "url": r["url"][:80]} for r in _SPY_NET[-8:]],
    }
# ── AKHIR PENGINTAI ─────────────────────────────────────────────────────


async def _on_response(response):
    """Intercept responses to capture ya29 tokens from aisandbox-pa requests."""
    global _current_session

    url = response.url

    # Capture ya29 from aisandbox-pa API responses
    if "aisandbox-pa.googleapis.com" in url:
        try:
            request = response.request
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer ya29"):
                token = auth_header.replace("Bearer ", "")
                _current_session["access_token"] = token
                _current_session["token_expires_at"] = (
                    datetime.utcnow() + timedelta(hours=1)
                ).isoformat() + "Z"
                _current_session["status"] = "token_captured"
                logger.info(f"Captured ya29 token: {token[:20]}...")

                # Save to DB
                await _save_session_to_db()
        except Exception as e:
            logger.debug(f"Error capturing token from response: {e}")

    # Detect successful Google login
    if "myaccount.google.com" in url or "accounts.google.com/ListAccounts" in url:
        if _current_session["status"] in ("idle", "browser_ready"):
            _current_session["status"] = "logged_in"
            logger.info("Google login detected, will attempt token capture")
            # Schedule token capture
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(_capture_token_from_browser())
            )


async def _capture_token_from_browser():
    """Navigate browser to labs.google/fx/404 and flow.google.com to trigger ya29 token capture."""
    global _current_session

    if not _browser_context:
        logger.error("No browser context available")
        return

    try:
        # First, save cookies
        cookies = await _browser_context.cookies()
        _current_session["cookies"] = cookies

        pages = _browser_context.pages
        page = pages[0] if pages else await _browser_context.new_page()

        # Step 1: Always visit labs.google/fx/404 to refresh Flow keys & trigger ya29 Bearer tokens
        logger.info("Triggering token refresh via labs.google/fx/404...")
        try:
            await page.goto("https://labs.google/fx/404", wait_until="networkidle", timeout=15000)
            await asyncio.sleep(2)
        except Exception as e:
            logger.debug(f"labs.google/fx/404 visit notice: {e}")

        # Step 1b: /fx/404 alone may not fire an aisandbox-pa call (no token).
        # The real app page does; verified 2026-09-26 that navigating to
        # labs.google/fx captures the ya29 Bearer token.
        if not _current_session.get("access_token"):
            logger.info("Token not captured from /fx/404 — visiting labs.google/fx")
            try:
                await page.goto("https://labs.google/fx", wait_until="networkidle", timeout=20000)
                await asyncio.sleep(6)
            except Exception as e:
                logger.debug(f"labs.google/fx visit notice: {e}")

        # Step 2: Navigate back to Flow
        logger.info("Navigating to flow.google.com to capture Flow session...")
        try:
            await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception as nav_err:
            logger.warning(f"Navigation to flow.google.com notice: {nav_err}")
        await page.wait_for_timeout(3000)

        # Try to extract email from page
        try:
            email = await page.evaluate("""
                () => {
                    const meta = document.querySelector('meta[name="google-signin-client_id"]');
                    const scripts = document.querySelectorAll('script');
                    for (const s of scripts) {
                        const text = s.textContent;
                        const match = text.match(/["']([a-zA-Z0-9._%+-]+@gmail\\.com)["']/);
                        if (match) return match[1];
                    }
                    return null;
                }
            """)
            if email:
                _current_session["email"] = email
        except Exception:
            pass

        # Wait for API calls that would carry the token
        await page.wait_for_timeout(3000)

        # If token still not captured, force evaluation from document cookie or fx API
        if not _current_session.get("access_token"):
            logger.info("Checking page cookies and triggering credits check...")
            try:
                captured_token = await page.evaluate("""
                    async () => {
                        const match = document.cookie.match(/ya29\\.[\\.a-zA-Z0-9_-]+/);
                        if (match) return match[0];
                        try {
                            const res = await fetch('https://aisandbox-pa.googleapis.com/v1/credits', { credentials: 'include' });
                        } catch(e) {}
                        return null;
                    }
                """)
                if captured_token:
                    _current_session["access_token"] = captured_token
                    _current_session["status"] = "token_captured"
            except Exception:
                pass

        # Save browser storage state for persistence
        if _current_session.get("email"):
            profile_dir = PROFILES_DIR / _current_session["email"]
        else:
            profile_dir = PROFILES_DIR / "default"
        profile_dir.mkdir(parents=True, exist_ok=True)
        await _browser_context.storage_state(path=str(profile_dir / "state.json"))

        # Save cookies to DB
        await _save_session_to_db()

        logger.info(f"Session capture complete. Status: {_current_session['status']}")

    except Exception as e:
        _current_session["last_error"] = str(e)
        logger.error(f"Token capture failed: {e}")


async def _save_session_to_db():
    """Save current session to SQLite."""
    email = _current_session.get("email") or "default@gmail.com"
    token = _current_session.get("access_token") or ""
    cookies = _current_session.get("cookies")
    cookies_json = json.dumps(cookies) if cookies else ""
    expires = _current_session.get("token_expires_at") or ""
    now_str = datetime.utcnow().isoformat() + "Z"

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO accounts (email, cookies_raw, access_token, at_expires, tier, credits_available, status, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'PAYGATE_TIER_ONE', 0, 'active', 1, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                cookies_raw = excluded.cookies_raw,
                access_token = excluded.access_token,
                at_expires = excluded.at_expires,
                status = 'active',
                is_active = 1,
                updated_at = excluded.updated_at
        """, (email, cookies_json, token, expires, now_str, now_str))
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Session saved to DB for {email}")


async def refresh_token_for_account(email: str):
    """Refresh ya29 token using stored browser profile (same IP, same fingerprint)."""
    global _browser_context, _browser_instance, _playwright_instance, _current_session

    profile_dir = PROFILES_DIR / email
    state_file = profile_dir / "state.json"

    if not state_file.exists():
        logger.error(f"No browser state found for {email}")
        return None

    # If the browser is already running, use a DEDICATED page. Reusing pages[0]
    # would hijack a worker's page and navigate it away mid-job.
    if _browser_context:
        refresh_page = None
        try:
            refresh_page = await _browser_context.new_page()
            await refresh_page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=30000)
            await refresh_page.wait_for_timeout(5000)

            if _current_session.get("access_token"):
                return _current_session["access_token"]
        except Exception as e:
            logger.warning(f"Token refresh via existing browser failed: {e}")
        finally:
            if refresh_page is not None:
                try:
                    await refresh_page.close()
                except Exception:
                    pass

    # Launch headless browser with same profile for refresh
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            executable_path="/usr/bin/google-chrome-stable",
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )

        ctx = await browser.new_context(
            storage_state=str(state_file),
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        )

        captured_token = None

        async def _intercept_token(response):
            nonlocal captured_token
            if "aisandbox-pa.googleapis.com" in response.url:
                try:
                    auth = response.request.headers.get("authorization", "")
                    if auth.startswith("Bearer ya29"):
                        captured_token = auth.replace("Bearer ", "")
                except Exception:
                    pass

        ctx.on("response", _intercept_token)

        page = await ctx.new_page()
        await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(8000)

        # Save updated state
        await ctx.storage_state(path=str(state_file))

        # Update cookies
        cookies = await ctx.cookies()

        await ctx.close()
        await browser.close()
        await pw.stop()

        if captured_token:
            _current_session["access_token"] = captured_token
            _current_session["token_expires_at"] = (
                datetime.utcnow() + timedelta(hours=1)
            ).isoformat() + "Z"
            _current_session["cookies"] = cookies
            await _save_session_to_db()
            logger.info(f"Token refreshed for {email}: {captured_token[:20]}...")
            return captured_token

        logger.warning(f"Could not capture token during refresh for {email}")
        return None

    except Exception as e:
        logger.error(f"Token refresh failed for {email}: {e}")
        return None


async def _token_refresh_loop():
    """Background task to auto-refresh tokens 1 hour before expiry.

    Disabled by default: see ENABLE_TOKEN_REFRESH. It tears down the live
    browser context and kills running jobs.
    """
    if not ENABLE_TOKEN_REFRESH:
        logger.info("_token_refresh_loop: disabled (set FLOW_ENABLE_TOKEN_REFRESH=1 to enable)")
        return
    while True:
        try:
            await asyncio.sleep(300)  # Check every 5 minutes

            conn = get_db()
            rows = conn.execute(
                "SELECT email, at_expires FROM accounts WHERE is_active = 1 AND access_token != ''"
            ).fetchall()
            conn.close()

            for row in rows:
                email = row["email"]
                expires_str = row["at_expires"]
                if not expires_str:
                    continue

                try:
                    expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00")).replace(tzinfo=None)
                    now = datetime.utcnow()
                    # Refresh if less than 10 minutes remain (token lasts ~60 min)
                    if expires_dt - now < timedelta(minutes=10):
                        logger.info(f"Token expiring soon for {email}, refreshing...")
                        await refresh_token_for_account(email)
                except Exception as e:
                    logger.error(f"Error checking token expiry for {email}: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Token refresh loop error: {e}")
            await asyncio.sleep(60)


async def _cookie_monitor_loop():
    """Monitor browser for login state changes."""
    global _current_session

    while True:
        try:
            await asyncio.sleep(10)

            if not _browser_context:
                continue

            if _current_session["status"] in ("idle", "browser_ready"):
                try:
                    cookies = await _browser_context.cookies()
                    google_cookies = [c for c in cookies if "google" in c.get("domain", "")]
                    has_sid = any(c["name"] == "SID" for c in google_cookies)
                    has_sapisid = any(c["name"] == "SAPISID" for c in google_cookies)

                    if has_sid and has_sapisid and _current_session["status"] != "logged_in":
                        logger.info("Google login detected via cookie monitor!")
                        _current_session["status"] = "logged_in"
                        await _capture_token_from_browser()
                except Exception:
                    pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"Cookie monitor error: {e}")
            await asyncio.sleep(30)


# ─── Public API ──────────────────────────────────────────────

async def start_remote_browser(email: str = "default") -> Dict[str, Any]:
    """Start the full remote browser stack: Xvfb + x11vnc + noVNC + Chromium."""
    global _current_session, _token_refresh_task, _cookie_monitor_task

    _current_session = {
        "email": email,
        "status": "idle",
        "access_token": None,
        "token_expires_at": None,
        "cookies": None,
        "last_error": None,
    }

    # Step 1: Start display stack
    if not _start_xvfb():
        return {"success": False, "error": "Failed to start Xvfb"}

    if not _start_vnc():
        return {"success": False, "error": "Failed to start x11vnc"}

    if not _start_novnc():
        return {"success": False, "error": "Failed to start noVNC"}

    # Step 2: Launch browser
    try:
        await _launch_browser(email)
        _current_session["status"] = "browser_ready"
        
        # Check if persistent profile already has cookies (login survived restart)
        if _browser_context:
            try:
                # First check if Chrome profile already has Google auth cookies
                cookies = await _browser_context.cookies("https://accounts.google.com/")
                has_sid = any(c["name"] == "__Secure-3PSID" for c in cookies)
                has_sapisid = any(c["name"] == "__Secure-3PAPISID" or c["name"] == "SAPISID" for c in cookies)
            
                if has_sid and has_sapisid:
                    _current_session["cookies"] = cookies
                    _current_session["status"] = "logged_in"
                    logger.info(f"PERSISTENT PROFILE: Login detected from existing cookies for {email}")
                else:
                    # Try loading cookies from SQLite DB (gateway.db)
                    # The DB stores cookies from previous browser sessions
                    import sqlite3 as _sqlite3
                    import json as _json
                    db_path = APP_DIR / "data" / "gateway.db"
                    if db_path.exists():
                        db_conn = _sqlite3.connect(str(db_path))
                        # Find the account with cookies
                        rows = db_conn.execute(
                            "SELECT email, cookies_raw FROM accounts WHERE cookies_raw IS NOT NULL AND cookies_raw != ''"
                        ).fetchall()
                        db_conn.close()
                    
                        for acct_email, cookies_raw in rows:
                            try:
                                parsed = _json.loads(cookies_raw)
                                # Handle different formats
                                if isinstance(parsed, dict):
                                    # {name: {value, domain}} format
                                    cookie_list = []
                                    for name, attrs in parsed.items():
                                        if isinstance(attrs, dict):
                                            cookie = {
                                                "name": name,
                                                "value": attrs.get("value", ""),
                                                "domain": attrs.get("domain", ".google.com"),
                                                "path": attrs.get("path", "/"),
                                                "secure": attrs.get("secure", True),
                                                "httpOnly": attrs.get("httpOnly", False),
                                            }
                                            if attrs.get("expires", -1) > 0:
                                                cookie["expires"] = attrs["expires"]
                                            cookie_list.append(cookie)
                                elif isinstance(parsed, list):
                                    # List of {name, value, domain, ...} format
                                    cookie_list = []
                                    for c in parsed:
                                        cookie = {
                                            "name": c.get("name", ""),
                                            "value": c.get("value", ""),
                                            "domain": c.get("domain", ".google.com"),
                                            "path": c.get("path", "/"),
                                            "secure": c.get("secure", True),
                                            "httpOnly": c.get("httpOnly", False),
                                        }
                                        if c.get("expires", -1) > 0:
                                            cookie["expires"] = c["expires"]
                                        cookie_list.append(cookie)
                                else:
                                    continue
                            
                                # Check if these cookies have Google auth
                                google_cookies = [c for c in cookie_list if "google" in c.get("domain", "")]
                                has_sid_db = any(c["name"] == "__Secure-3PSID" for c in google_cookies)
                                has_sapisid_db = any(c["name"] in ("SAPISID", "__Secure-3PAPISID") for c in google_cookies)
                            
                                if has_sid_db and has_sapisid_db:
                                    await _browser_context.add_cookies(cookie_list)
                                    _current_session["cookies"] = cookie_list
                                    _current_session["email"] = acct_email
                                    _current_session["status"] = "logged_in"
                                    logger.info(f"DB COOKIES: Injected {len(cookie_list)} cookies from gateway.db for {acct_email}")
                                    break
                            except Exception as e:
                                logger.warning(f"Failed to parse cookies for {acct_email}: {e}")
                                continue
            except Exception as e:
                logger.warning(f"Could not check existing cookies: {e}")
    except Exception as e:
        return {"success": False, "error": f"Failed to launch browser: {e}"}

    # Step 3: Start background tasks
    if _token_refresh_task is None or _token_refresh_task.done():
        _token_refresh_task = asyncio.create_task(_token_refresh_loop())

    if _cookie_monitor_task is None or _cookie_monitor_task.done():
        _cookie_monitor_task = asyncio.create_task(_cookie_monitor_loop())

    # Step 3b: auto-capture labs.google token (the login-detection path never
    # fires when cookies are restored from the persistent profile, so without
    # this every media.getMediaUrlRedirect call returns 401 after a restart).
    if _current_session.get("status") == "logged_in" and not _current_session.get("access_token"):
        logger.info("start_remote_browser: logged in via cookies — capturing labs token now")
        try:
            await _capture_token_from_browser()
            if _current_session.get("access_token"):
                logger.info("labs token captured automatically after browser start")
            else:
                logger.warning("labs token STILL not captured after start — "
                               "media downloads may return 401/Internal Error")
        except Exception as e:
            logger.warning(f"auto token capture failed: {type(e).__name__}: {e}")

    return {
        "success": True,
        "novnc_url": f"http://104.64.15.167:{NOVNC_PORT}/vnc.html?autoconnect=true&password={VNC_PASSWORD}",
        "vnc_port": VNC_PORT,
        "novnc_port": NOVNC_PORT,
        "status": "browser_ready",
        "instructions": [
            f"1. Open http://104.64.15.167:{NOVNC_PORT}/vnc.html?autoconnect=true&password={VNC_PASSWORD}",
            "2. You'll see a Chrome browser — log into your Google account",
            "3. After login, the system will automatically navigate to flow.google.com",
            "4. Token will be captured automatically from API requests",
            "5. Check status via GET /v1/browser/status",
            "6. IMPORTANT: Do NOT login to this Google account from any other browser!",
        ],
    }


async def stop_remote_browser() -> Dict[str, Any]:
    """Stop the remote browser and cleanup."""
    global _browser_context, _browser_instance, _playwright_instance
    global _xvfb_proc, _vnc_proc, _novnc_proc
    global _token_refresh_task, _cookie_monitor_task

    if _cookie_monitor_task:
        _cookie_monitor_task.cancel()
    if _token_refresh_task:
        _token_refresh_task.cancel()

    if _browser_context:
        try:
            # Save state before closing
            email = _current_session.get("email") or "default"
            profile_dir = PROFILES_DIR / email
            profile_dir.mkdir(parents=True, exist_ok=True)
            await _browser_context.storage_state(path=str(profile_dir / "state.json"))
            await _browser_context.close()
        except Exception:
            pass
        _browser_context = None

    if _browser_instance:
        try:
            await _browser_instance.close()
        except Exception:
            pass
        _browser_instance = None

    if _playwright_instance:
        try:
            await _playwright_instance.stop()
        except Exception:
            pass
        _playwright_instance = None

    for proc_name, proc in [("noVNC", _novnc_proc), ("x11vnc", _vnc_proc), ("Xvfb", _xvfb_proc)]:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    _xvfb_proc = None
    _vnc_proc = None
    _novnc_proc = None

    return {"success": True, "message": "Remote browser stopped"}


def get_session_status() -> Dict[str, Any]:
    """Get current browser session status."""
    return {
        "status": _current_session["status"],
        "email": _current_session.get("email"),
        "hasToken": bool(_current_session.get("access_token")),
        "tokenPreview": _current_session["access_token"][:25] + "..." if _current_session.get("access_token") else None,
        "tokenExpiresAt": _current_session.get("token_expires_at"),
        "hasCookies": bool(_current_session.get("cookies")),
        "lastError": _current_session.get("last_error"),
        "xvfb_running": _xvfb_proc is not None and _xvfb_proc.poll() is None,
        "vnc_running": _vnc_proc is not None and _vnc_proc.poll() is None,
        "novnc_running": _novnc_proc is not None and _novnc_proc.poll() is None,
        "browser_running": _browser_context is not None,
        "novnc_url": f"http://104.64.15.167:{NOVNC_PORT}/vnc.html?autoconnect=true&password={VNC_PASSWORD}",
    }


async def trigger_token_capture() -> Dict[str, Any]:
    """Manually trigger token capture from current browser session."""
    if not _browser_context:
        return {"success": False, "error": "No browser session active"}

    await _capture_token_from_browser()
    return {
        "success": bool(_current_session.get("access_token")),
        "token_preview": _current_session["access_token"][:25] + "..." if _current_session.get("access_token") else None,
        "status": _current_session["status"],
    }


async def navigate_browser(url: str) -> Dict[str, Any]:
    """Navigate the remote browser to a URL."""
    if not _browser_context:
        return {"success": False, "error": "No browser session active"}

    try:
        pages = _browser_context.pages
        page = pages[0] if pages else await _browser_context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"success": True, "url": url}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def execute_js_and_get_recaptcha(action: str = "VIDEO_GENERATION") -> Dict[str, Any]:
    """Execute grecaptcha.enterprise.execute in the browser and return the token."""
    if not _browser_context:
        return {"success": False, "error": "No browser session active"}

    try:
        pages = _browser_context.pages
        page = pages[0] if pages else await _browser_context.new_page()
        
        # Make sure we're on flow.google.com
        if "flow.google.com" not in page.url and "labs.google" not in page.url:
            await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
        
        # Execute grecaptcha
        recaptcha_token = await page.evaluate("""
            async (action) => {
                try {
                    if (typeof grecaptcha === 'undefined') {
                        // Try to load grecaptcha first
                        await new Promise((resolve, reject) => {
                            const script = document.createElement('script');
                            script.src = 'https://www.google.com/recaptcha/enterprise.js?render=6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';
                            script.onload = resolve;
                            script.onerror = reject;
                            document.head.appendChild(script);
                        });
                        await new Promise(r => setTimeout(r, 2000));
                    }
                    if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {
                        return await grecaptcha.enterprise.execute('6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV', {action: action});
                    }
                    return null;
                } catch(e) {
                    return 'ERROR: ' + e.message;
                }
            }
        """, action)
        
        if recaptcha_token and not str(recaptcha_token).startswith("ERROR"):
            logger.info(f"reCAPTCHA token captured for action={action}")
            return {"success": True, "token": recaptcha_token}
        else:
            return {"success": False, "error": recaptcha_token or "grecaptcha not available"}
    except Exception as e:
        logger.error(f"reCAPTCHA execution failed: {e}")
        return {"success": False, "error": str(e)}


async def generate_video_from_browser(
    token: str, prompt: str, model_key: str, aspect_ratio: str, duration: int
) -> Dict[str, Any]:
    """Generate video by executing fetch() inside the browser — reCAPTCHA validates because same browser context."""
    if not _browser_context:
        return {"success": False, "error": "No browser session active"}

    try:
        pages = _browser_context.pages
        page = pages[0] if pages else await _browser_context.new_page()
        
        # Make sure we're on flow.google.com or labs.google
        if "flow.google.com" not in page.url and "labs.google" not in page.url:
            await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
        
        # Execute the entire generate request inside the browser
        result = await page.evaluate("""
            async (params) => {
                try {
                    const batchId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
                    const sessionId = ';' + String(Date.now());
                    
                    // Get reCAPTCHA token
                    let recaptchaToken = null;
                    if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {
                        recaptchaToken = await grecaptcha.enterprise.execute(
                            '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                            {action: 'VIDEO_GENERATION'}
                        );
                    }
                    
                    const clientContext = {
                        tool: 'PINHOLE',
                        sessionId: sessionId,
                    };
                    if (recaptchaToken) {
                        clientContext.recaptchaContext = {
                            token: recaptchaToken,
                            applicationType: 'RECAPTCHA_APPLICATION_TYPE_WEB',
                        };
                    }
                    
                    const payload = {
                        clientContext: clientContext,
                        mediaGenerationContext: {batchId: batchId},
                        useV2ModelConfig: true,
                        requests: [{
                            aspectRatio: params.aspectRatio,
                            textInput: {structuredPrompt: {parts: [{text: params.prompt}]}},
                            videoModelKey: params.modelKey,
                        }],
                    };
                    
                    const resp = await fetch(
                        'https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText',
                        {
                            method: 'POST',
                            headers: {
                                'Authorization': 'Bearer ' + params.token,
                                'Content-Type': 'text/plain;charset=UTF-8',
                            },
                            body: JSON.stringify(payload),
                        }
                    );
                    
                    const data = await resp.text();
                    return {status: resp.status, body: data};
                } catch(e) {
                    return {status: 0, body: 'ERROR: ' + e.message};
                }
            }
        """, {"token": token, "prompt": prompt, "modelKey": model_key, "aspectRatio": aspect_ratio})
        
        logger.info(f"Browser video generate: status={result.get('status')}")
        return {"success": result.get("status") == 200, "result": result}
    except Exception as e:
        logger.error(f"Browser video generation failed: {e}")
        return {"success": False, "error": str(e)}


async def import_cookies(cookies_text: str, email: str = None) -> Dict[str, Any]:
    """Import cookies from text (Chrome DevTools table format) into the browser session."""
    global _current_session

    # Parse cookies
    parsed = _parse_cookies_text(cookies_text)
    if not parsed:
        return {"success": False, "error": "Could not parse cookies"}

    # Validate essential cookies
    cookie_names = {c["name"] for c in parsed}
    essential = {"SID", "SAPISID", "__Secure-1PSID"}
    missing = essential - cookie_names
    if missing:
        return {"success": False, "error": f"Missing essential cookies: {', '.join(missing)}"}

    # If browser is running, inject cookies
    if _browser_context:
        try:
            await _browser_context.add_cookies(parsed)
            _current_session["status"] = "logged_in"
            _current_session["cookies"] = parsed
            if email:
                _current_session["email"] = email

            # Navigate to flow.google.com to capture token
            await _capture_token_from_browser()

            return {
                "success": True,
                "cookies_imported": len(parsed),
                "token_captured": bool(_current_session.get("access_token")),
                "status": _current_session["status"],
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to inject cookies: {e}"}

    # No browser running — just save to DB
    _current_session["cookies"] = parsed
    _current_session["status"] = "logged_in"
    if email:
        _current_session["email"] = email
    await _save_session_to_db()

    return {
        "success": True,
        "cookies_imported": len(parsed),
        "note": "Cookies saved but browser not running. Start browser to capture token.",
    }


def _parse_cookies_text(text: str) -> List[Dict]:
    """Parse cookies from various formats: table, JSON, Netscape."""
    text = text.strip()

    # Try JSON array
    if text.startswith("["):
        try:
            data = json.loads(text)
            result = []
            for c in data:
                result.append({
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ".google.com"),
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", True),
                    "httpOnly": c.get("httpOnly", False),
                })
            return result
        except json.JSONDecodeError:
            pass

    # Try tab-separated table format (Chrome DevTools)
    lines = text.strip().split("\n")
    result = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) >= 2:
            # Chrome DevTools format: name\tvalue
            if len(parts) == 2:
                result.append({
                    "name": parts[0].strip(),
                    "value": parts[1].strip(),
                    "domain": ".google.com",
                    "path": "/",
                    "secure": parts[0].strip().startswith("__Secure"),
                    "httpOnly": False,
                })
            # Netscape format: domain\tflag\tpath\tsecure\texpiry\tname\tvalue
            elif len(parts) >= 7:
                result.append({
                    "name": parts[5].strip(),
                    "value": parts[6].strip(),
                    "domain": parts[0].strip(),
                    "path": parts[2].strip(),
                    "secure": parts[3].strip().upper() == "TRUE",
                    "httpOnly": False,
                })
            # Other tab-separated: try first and last columns
            else:
                result.append({
                    "name": parts[0].strip(),
                    "value": parts[1].strip(),
                    "domain": ".google.com",
                    "path": "/",
                    "secure": parts[0].strip().startswith("__Secure"),
                    "httpOnly": False,
                })

    return result if result else []
