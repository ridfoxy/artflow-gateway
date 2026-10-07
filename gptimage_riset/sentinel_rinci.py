#!/usr/bin/env python3
"""Rinci persyaratan sentinel untuk zero-UI: turnstile & proof-of-work.

Menjawab: kenapa POST /backend-api/f/conversation ditolak 403
"Unusual activity has been detected from your device" saat RPC murni.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "fverdj@gumdel.com"
PROFIL = f"/root/chatgpt_profil_{EMAIL.split('@')[0]}"
KELUAR = Path("/root/api-google-flow/output")


async def main():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(8)
        B = "https://chatgpt.com/backend-api"

        r = await pg.request.get("https://chatgpt.com/api/auth/session",
                                 timeout=60000)
        ses = await r.json()
        tok = ses.get("accessToken")
        kepala = {"Authorization": f"Bearer {tok}",
                  "Content-Type": "application/json", "oai-language": "en-US",
                  "Origin": "https://chatgpt.com",
                  "Referer": "https://chatgpt.com/"}

        r = await pg.request.post(f"{B}/sentinel/chat-requirements/prepare",
                                  headers=kepala,
                                  data=json.dumps({"p": ""}), timeout=60000)
        sj = await r.json()
        print("=== PREPARE (persyaratan) ===", flush=True)
        print(json.dumps(sj, indent=1, ensure_ascii=False)[:2500], flush=True)

        # struktur turnstile & proofofwork
        for k in ("turnstile", "proofofwork"):
            if k in sj:
                print(f"\n--- {k} ---", flush=True)
                print(json.dumps(sj[k], indent=1, ensure_ascii=False)[:900],
                      flush=True)

        prep = sj.get("prepare_token", "")
        r2 = await pg.request.post(f"{B}/sentinel/chat-requirements/finalize",
                                   headers=kepala,
                                   data=json.dumps({"prepare_token": prep}),
                                   timeout=60000)
        fj = await r2.json()
        print("\n=== FINALIZE ===", flush=True)
        print(json.dumps({k: (v if k != "token" else f"<{len(str(v))} char>")
                          for k, v in fj.items()}, indent=1,
                         ensure_ascii=False)[:1200], flush=True)
        print("\n  token sentinel:", len(str(fj.get("token", ""))), "char",
              flush=True)
        print("  expire_after:", fj.get("expire_after"), flush=True)

        # apakah SDK sentinel tersedia & fungsi apa saja
        print("\n=== SDK SENTINEL di halaman ===", flush=True)
        sd = await pg.evaluate("""() => {
            const w = window;
            const kunci = Object.keys(w).filter(k => /sentinel|Sentinel|turnstile/i.test(k));
            const o = {global: kunci};
            try { o.sdk = typeof w.SentinelSDK; } catch(e) {}
            try {
                o.versi = (w.SentinelSDK && w.SentinelSDK.version) || null;
                o.metode = w.SentinelSDK ? Object.keys(w.SentinelSDK) : null;
            } catch(e) { o.err = String(e); }
            return o;
        }""")
        print(json.dumps(sd, indent=1, ensure_ascii=False)[:800], flush=True)

        # token yang dipakai halaman saat benar-benar mengirim (dari cache JS)
        tok2 = await pg.evaluate("""() => {
            const o = {};
            try {
                for (const k of Object.keys(localStorage)) {
                    if (/sentinel|chat-req/i.test(k)) o[k] = (localStorage.getItem(k)||'').slice(0,120);
                }
            } catch(e) {}
            return o;
        }""")
        print("\n=== localStorage sentinel ===", flush=True)
        print(json.dumps(tok2, indent=1, ensure_ascii=False)[:600], flush=True)

        (KELUAR / "sentinel_rinci.json").write_text(
            json.dumps({"prepare": sj, "finalize": {
                k: (v if k != "token" else f"<{len(str(v))} char>")
                for k, v in fj.items()}}, indent=1, ensure_ascii=False))
        print("\n  tersimpan: output/sentinel_rinci.json", flush=True)
        await ctx.close()


asyncio.run(main())