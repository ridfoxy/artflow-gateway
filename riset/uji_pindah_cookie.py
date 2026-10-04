#!/usr/bin/env python3
"""UJI: bisakah sesi Camoufox (Firefox) dipindah ke Chromium?

Catatan penting: uji serupa dulu menghasilkan "Signed out", TAPI saat itu
akunnya sudah DIHAPUS Google — jadi kesimpulannya tidak sah. Sekarang diuji
dengan akun @gumdel yang benar-benar hidup.

Kalau berhasil, runner bisa kembali ke Chromium (jalur yang sudah terbukti
stabil + zero-UI) tanpa perlu login ulang.
"""
import asyncio
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

APP = Path("/root/api-google-flow")
EMAIL = "fverdj@gumdel.com"
FF = APP / "firefox_profiles" / EMAIL / "cookies.sqlite"
CR = APP / "chrome_profiles" / EMAIL / "Default" / "Cookies"


def baca_ff():
    """Ambil cookie google dari profil Firefox (cookies.sqlite)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    shutil.copy2(FF, tmp)
    c = sqlite3.connect(tmp)
    rows = c.execute("""SELECT name, value, host, path, expiry, isSecure, isHttpOnly
                        FROM moz_cookies WHERE host LIKE '%google.com'""").fetchall()
    c.close()
    return rows, tmp


def tulis_chrome(rows):
    """Tulis cookie google ke profil Chromium (tabel cookies)."""
    if not CR.exists():
        return 0, "profil Chromium tidak ada"
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    shutil.copy2(CR, tmp)
    c = sqlite3.connect(tmp)
    # bersihkan cookie google lama
    c.execute("DELETE FROM cookies WHERE host_key LIKE '%google.com'")
    n = 0
    for name, value, host, path, expiry, secure, http in rows:
        try:
            c.execute("""INSERT INTO cookies
                (creation_utc, host_key, name, value, path, expires_utc,
                 is_secure, is_httponly, last_access_utc, has_expires,
                 is_persistent, priority, encrypted_value, samesite,
                 source_scheme, last_update_utc, source_port,
                 has_cross_site_ancestor)
                VALUES (0,?,?,?,?,?,?,?,0,1,1,1,X'',0,1,0,443,0)""",
                (host, name, value, path, int(expiry * 1e6) if expiry else 0,
                 secure, http))
            n += 1
        except Exception as e:
            return n, f"gagal baris: {str(e)[:60]}"
    c.commit()
    c.close()
    shutil.copy2(tmp, CR)
    return n, "ok"


async def uji_chromium():
    from playwright.async_api import async_playwright
    p = await async_playwright().start()
    try:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(APP / "chrome_profiles" / EMAIL),
            executable_path="/usr/bin/google-chrome-stable", headless=False,
            args=["--display=:99", "--no-sandbox", "--disable-gpu",
                  "--disable-dev-shm-usage"],
            viewport={"width": 1200, "height": 800})
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://docs.google.com/videos",
                      wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(8)
        hidup = "accounts.google.com" not in pg.url
        print(f"  hasil  : {'HIDUP' if hidup else 'MATI (masih minta login)'}")
        print(f"  judul  : {(await pg.title())[:60]}")
        print(f"  url    : {pg.url[:100]}")
        teks = await pg.evaluate("() => (document.body.innerText||'').slice(0,200)")
        print(f"  teks   : {teks[:190]}".replace(chr(10), " | "))
        await ctx.close()
    finally:
        await p.stop()


async def main():
    rows, tmp = baca_ff()
    print(f"cookie google di Firefox : {len(rows)}")
    print("  contoh:", [r[0] for r in rows][:8])
    n, ket = tulis_chrome(rows)
    print(f"cookie ditulis ke Chromium: {n} ({ket})")
    await uji_chromium()
    Path(tmp).unlink(missing_ok=True)


asyncio.run(main())