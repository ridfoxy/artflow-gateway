#!/usr/bin/env python3
"""Selesaikan pendaftaran ChatGPT (form umur) setelah login Google.

Alur terbukti: Continue with Google -> Google -> izin -> halaman "How old are
you?" (auth.openai.com/about-you). Halaman ini hanya muncul SEKALI per akun.
"""
import asyncio
import sys
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = "fverdj@gumdel.com"
PROFIL = "/root/chatgpt_profil_fverdj"


def sandi(email):
    for b in Path("/root/api-google-flow/accounts.txt").read_text(
            errors="replace").splitlines():
        s = b.strip()
        if s.startswith("#") or "|" not in s:
            continue
        e, p = s.split("|", 1)
        if e.strip().lower() == email.lower():
            return p.strip()


async def klik(pg, *sel, timeout=9000):
    for s in sel:
        try:
            await pg.click(s, timeout=timeout)
            return s
        except Exception:
            continue
    return None


async def isi(pg, sel, nilai, timeout=15000):
    for s in sel:
        try:
            await pg.fill(s, nilai, timeout=timeout)
            return s
        except Exception:
            continue
    return None


async def main():
    pw = sandi(EMAIL)
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://chatgpt.com/auth/login",
                      wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(8)

        # 1. kalau sudah login, langsung cek
        if "auth" not in pg.url:
            print("  sepertinya sudah login:", pg.url[:80])

        # 2. alur login
        if await klik(pg, 'button:has-text("Continue with Google")',
                      timeout=8000):
            await asyncio.sleep(10)
            if "accountchooser" in pg.url.lower():
                await klik(pg, 'text="Use another account"',
                           'text="Gunakan akun lain"')
                await asyncio.sleep(5)
            if await isi(pg, ['#identifierId'], EMAIL):
                await asyncio.sleep(1)
                await klik(pg, "#identifierNext")
                await asyncio.sleep(8)
            if await isi(pg, ['input[name="Passwd"]'], pw):
                await asyncio.sleep(1)
                await klik(pg, "#passwordNext")
                await asyncio.sleep(10)
            teks = await pg.evaluate("() => (document.body.innerText||'')")
            if "sign in to openai" in teks.lower():
                await klik(pg, '#submit_approve_access',
                           'button:has-text("Continue")')
                await asyncio.sleep(14)

        # 3. form umur
        print("  url-1:", pg.url[:110])
        if "about-you" in pg.url:
            print("  form umur terdeteksi — mengisi")
            await isi(pg, ['input[name="name"]', 'input[autocomplete="name"]'],
                      "fverdj")
            await asyncio.sleep(1)
            # kolom umur (bisa select / input)
            r = await isi(pg, ['input[name="age"]', 'input[inputmode="numeric"]',
                               'input[type="number"]'], "27")
            print("    isi umur:", bool(r))
            await asyncio.sleep(1)
            tombol = await klik(pg, 'button:has-text("Finish signing up")',
                                'button[type="submit"]',
                                'button:has-text("Continue")')
            print("    klik daftar:", tombol)
            await asyncio.sleep(18)

        print("\n  url akhir :", pg.url[:120])
        print("  judul     :", (await pg.title())[:70])
        t2 = await pg.evaluate("() => (document.body.innerText||'').slice(0,350)")
        print("  teks      :", t2.replace(chr(10), " | ")[:330])
        if "chatgpt.com" in pg.url and "auth" not in pg.url:
            print("\n  >> BERHASIL MASUK CHATGPT ✅")
        await ctx.close()


asyncio.run(main())