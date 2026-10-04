#!/usr/bin/env python3
"""Cek status tiap akun — satu PROFIL TERPISAH per akun (hindari cookie silang).

Tiap akun memakai profil sendiri supaya hasilnya tidak tercemar cookie akun
lain; halaman selalu dibuat baru dan ditutup setelah selesai.
"""
import asyncio
import sys
from collections import Counter

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

AKUN = [f"asepqds{i}@rallse.com" for i in
        (8, 7, 4, 9, 3, 12, 16, 18, 24, 2, 13, 14, 17, 19, 1,
         5, 6, 10, 11, 15, 20, 21, 22, 23, 25)]

BASE = "/root/api-google-flow/firefox_profiles"


async def cek(email):
    from camoufox.async_api import AsyncCamoufox
    try:
        async with AsyncCamoufox(headless=False, persistent_context=True,
                                 user_data_dir=f"{BASE}/{email}",
                                 i_know_what_im_doing=True) as ctx:
            pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await pg.goto("https://accounts.google.com/signin/v2/identifier"
                          "?flowName=GlifWebSignIn&flowEntry=ServiceLogin",
                          wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)
            if "accountchooser" in pg.url.lower():
                for t in ("Use another account", "Gunakan akun lain"):
                    try:
                        el = await pg.query_selector(f'text="{t}"')
                        if el and await el.is_visible():
                            await el.click()
                            await asyncio.sleep(4)
                            break
                    except Exception:
                        continue
            sel = await pg.query_selector("#identifierId")
            if not sel or not await sel.is_visible():
                teks = (await pg.evaluate("() => (document.body.innerText||'').toLowerCase()"))
                if "akun dihapus" in teks or "deletedaccount" in pg.url:
                    return "DIHAPUS"
                return "TANPA-KOLOM-EMAIL"
            await pg.fill("#identifierId", email)
            await asyncio.sleep(0.8)
            try:
                await pg.click("#identifierNext", timeout=6000)
            except Exception:
                await pg.keyboard.press("Enter")
            await asyncio.sleep(9)
            url = pg.url
            teks = (await pg.evaluate("() => (document.body.innerText||'').toLowerCase()"))
            if "deletedaccount" in url or "akun dihapus" in teks or "account deleted" in teks:
                return "DIHAPUS"
            if "dinonaktifkan" in teks or "disabled" in teks:
                return "DINONAKTIFKAN"
            if "couldn't find" in teks or "tidak dapat menemukan" in teks:
                return "TIDAK-ADA"
            pw = await pg.query_selector('input[name="Passwd"]')
            if pw and await pw.is_visible():
                return "HIDUP"
            if "speedbump" in url or "captcha" in teks or "ketik teks" in teks:
                return "CAPTCHA"
            return "LAIN"
    except Exception as e:
        return f"ERROR {str(e)[:40]}"


async def main():
    hasil = {}
    for e in AKUN:
        st = await cek(e)
        hasil[e] = st
        print(f"  {e.replace('@rallse.com',''):11} {st}", flush=True)
    print("\n=== RINGKASAN ===")
    c = Counter(v.split(" ")[0] for v in hasil.values())
    for k, v in c.most_common():
        print(f"  {k:22} {v}")
    print("\n  HIDUP:", ", ".join(k.replace('@rallse.com', '')
                                  for k, v in hasil.items() if v == "HIDUP") or "-")


asyncio.run(main())