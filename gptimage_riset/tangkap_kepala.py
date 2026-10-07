#!/usr/bin/env python3
"""Tangkap SEMUA kepala request POST /backend-api/f/conversation yang BERHASIL.

Ini kunci zero-UI: header sentinel apa saja yang wajib ikut. Tanpa itu
server menjawab 403 "Unusual activity has been detected from your device".
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "fverdj@gumdel.com"
PROFIL = f"/root/chatgpt_profil_{EMAIL.split('@')[0]}"
KELUAR = Path("/root/api-google-flow/output")

tertangkap = []


async def main():
    from camoufox.async_api import AsyncCamoufox

    def catat(req):
        if req.url.split("?")[0].endswith("/backend-api/f/conversation"):
            tertangkap.append({"url": req.url, "metode": req.method,
                               "kepala": dict(req.headers or {})})

    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        pg.on("request", catat)
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(12)

        sel = '#prompt-textarea'
        try:
            await pg.wait_for_selector(sel, timeout=20000)
        except Exception:
            print("  kotak tidak ada", flush=True)
            return
        kotak = await pg.query_selector(sel)
        await kotak.click()
        await asyncio.sleep(1)
        await pg.keyboard.type("kucing oranye lucu di taman", delay=12)
        await asyncio.sleep(1)
        await pg.keyboard.press("Enter")
        print("  prompt dikirim, tunggu 60s...", flush=True)
        await asyncio.sleep(60)

        if not tertangkap:
            print("  !! tidak tertangkap", flush=True)
            return
        e = tertangkap[-1]
        print(f"\n=== KEPALA LENGKAP {e['metode']} {e['url'][:80]} ===", flush=True)
        penting = []
        for k, v in sorted(e["kepala"].items()):
            kl = k.lower()
            if kl in ("cookie", "authorization"):
                print(f"  {k}: <{len(v)} char>", flush=True)
            elif "sentinel" in kl or "conduit" in kl or "device" in kl or \
                    "beta" in kl or "oai-" in kl or "openai" in kl:
                print(f"  {k}: {v[:150]}", flush=True)
                penting.append(k)
            else:
                print(f"  {k}: {v[:90]}", flush=True)

        # simpan versi aman (tanpa nilai rahasia)
        aman = {}
        for k, v in e["kepala"].items():
            kl = k.lower()
            if kl in ("cookie", "authorization"):
                aman[k] = f"<{len(v)} char>"
            else:
                aman[k] = v[:300]
        (KELUAR / "kepala_fconversation.json").write_text(
            json.dumps(aman, indent=1, ensure_ascii=False))
        print("\n  header sentinel yang ada:", penting, flush=True)
        print("  tersimpan: output/kepala_fconversation.json", flush=True)
        await ctx.close()


asyncio.run(main())