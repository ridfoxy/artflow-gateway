#!/usr/bin/env python3
"""UJI: pindahkan sesi Google dari profil Firefox (Camoufox) ke profil Chromium.

MASALAH:
  - Camoufox (Firefox) login BERHASIL -> sesi tersimpan di firefox_profiles/<email>
  - Runner memakai Chromium  -> membaca chrome_profiles/<email>
  - Dua folder BERBEDA, jadi sesi tidak terbaca:
        "sesi mati (mendarat di halaman login Google)"

YANG DIUJI:
  Ambil cookie Google dari Firefox (profil Camoufox), lalu masukkan ke konteks
  Chromium (via Playwright add_cookies / CDP), kemudian buka Google Vids.
  Kalau Vids terbuka -> sesi BERHASIL dipindah, runner bisa dipakai seperti biasa.

Pakai: python3 uji_pindah_sesi.py --email asepqds1@rallse.com
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

APP = Path("/root/api-google-flow")
FF_PROF = APP / "firefox_profiles"
CR_PROF = APP / "chrome_profiles"
VIDS = "https://docs.google.com/videos/"


def cookies_firefox(profile_dir, domain_kata="google"):
    """Baca cookie dari cookies.sqlite milik Firefox."""
    db = profile_dir / "cookies.sqlite"
    if not db.exists():
        return [], f"tidak ada {db.name}"
    out = []
    try:
        # salin dulu supaya tidak mengunci DB yang sedang dipakai
        tmp = Path("/tmp/_ck.sqlite")
        tmp.write_bytes(db.read_bytes())
        con = sqlite3.connect(str(tmp))
        q = ("SELECT name, value, host, path, expiry, isSecure, isHttpOnly "
             "FROM moz_cookies")
        for name, value, host, path, expiry, sec, ho in con.execute(q):
            if domain_kata and domain_kata not in (host or ""):
                continue
            out.append({
                "name": name, "value": value,
                "domain": host, "path": path or "/",
                "expires": int(expiry) if expiry else -1,
                "secure": bool(sec), "httpOnly": bool(ho),
                "sameSite": "Lax",
            })
        con.close()
        tmp.unlink(missing_ok=True)
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:90]}"
    return out, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--kering", action="store_true",
                    help="hanya baca cookie, jangan buka browser")
    args = ap.parse_args()
    email = args.email

    print(f"=== PINDAH SESI: {email} ===")
    prof_ff = FF_PROF / email
    prof_cr = CR_PROF / email
    print(f"  profil FF (sumber) : {prof_ff.exists()}")
    print(f"  profil CR (tujuan) : {prof_cr.exists()}")

    ck, err = cookies_firefox(prof_ff)
    print(f"\n  1. cookie Google dari Firefox: {len(ck)}")
    if err:
        print(f"     ! {err}")
    if ck:
        inti = [c["name"] for c in ck if c["name"] in
                ("SID", "HSID", "SSID", "APISID", "SAPISID",
                 "__Secure-1PSID", "__Secure-3PSID", "SIDCC",
                 "__Secure-1PAPISID", "__Secure-3PAPISID")]
        print(f"     cookie inti: {sorted(set(inti))}")
        host = {}
        for c in ck:
            host[c["domain"]] = host.get(c["domain"], 0) + 1
        print(f"     domain: {json.dumps(host, ensure_ascii=False)[:200]}")

    if not ck:
        print("\n  -> TIDAK ADA cookie. Login Camoufox dulu untuk akun ini.")
        return 1

    if args.kering:
        print("\n  (mode kering: berhenti di sini)")
        return 0

    Path("/tmp/_cookies_ff.json").write_text(json.dumps(ck, ensure_ascii=False))
    print(f"\n  2. cookie disimpan sementara: /tmp/_cookies_ff.json ({len(ck)})")

    # ── buka Chromium dengan profil CHROME, suntikkan cookie, cek Vids ──
    print("\n  3. buka Chromium (profil Chrome) + suntik cookie ...")
    from playwright.sync_api import sync_playwright

    hasil = {"vids": None, "url_akhir": None, "judul": None}
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(prof_cr),
            executable_path="/usr/bin/google-chrome-stable",
            headless=False,
            args=["--no-first-run", "--no-default-browser-check",
                  "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
            locale="id-ID",
        )
        try:
            # suntikkan cookie (host google)
            siap = []
            sekarang = int(time.time())
            for c in ck:
                d = c["domain"]
                if not d:
                    continue
                # Firefox menyimpan expiry dalam MILIDETIK (mis. 1825452506835),
                # Playwright menerima DETIK. Nilai > 10^11 pasti milidetik.
                exp = c.get("expires")
                try:
                    exp = int(exp)
                except Exception:
                    exp = -1
                if exp > 10 ** 11:
                    exp = exp // 1000
                if exp <= 0:
                    exp = -1
                elif exp < sekarang + 60:
                    exp = -1
                siap.append({
                    "name": c["name"], "value": c["value"],
                    "domain": d, "path": c["path"],
                    "expires": exp, "secure": c["secure"],
                    "httpOnly": c["httpOnly"], "sameSite": c["sameSite"],
                })
            ctx.add_cookies(siap)
            print(f"     cookie disuntik: {len(siap)}")

            page = ctx.new_page()
            page.goto(VIDS, wait_until="domcontentloaded", timeout=90000)
            print(f"     sesudah buka Vids: {page.url[:90]}")
            # tunggu sampai jelas (masuk atau tetap di login)
            batas = time.time() + 30
            while time.time() < batas:
                if "accounts.google.com" not in page.url:
                    break
                time.sleep(2)
            try:
                page.wait_for_selector('[contenteditable="true"], textarea, '
                                       '[role="textbox"]', timeout=20000)
            except Exception:
                pass
            hasil["url_akhir"] = page.url
            try:
                hasil["judul"] = page.title()
            except Exception:
                pass
            if "accounts.google.com" not in page.url:
                hasil["vids"] = "TERBUKA"
            else:
                hasil["vids"] = "MASIH LOGIN"

            # simpan cookie hasil (profil Chrome akan menulisnya sendiri)
            time.sleep(3)
            try:
                page.screenshot(path="/tmp/uji_pindah_sesi.png")
            except Exception:
                pass
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    print("\n  4. HASIL:")
    print(f"     Vids      : {hasil['vids']}")
    print(f"     URL akhir : {str(hasil['url_akhir'])[:100]}")
    print(f"     judul     : {hasil['judul']}")
    print(f"     screenshot: /tmp/uji_pindah_sesi.png")

    if hasil["vids"] == "TERBUKA":
        print("\n  >>> BERHASIL: sesi Firefox BISA dipakai Chromium lewat "
              "pemindahan cookie.")
        print("      Solusi: setelah login Camoufox, pindahkan cookie ke profil Chrome.")
    else:
        print("\n  >>> GAGAL: cookie saja tidak cukup. Perlu cara lain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())