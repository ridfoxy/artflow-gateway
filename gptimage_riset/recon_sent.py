#!/usr/bin/env python3
"""RECON SENTINEL — cari kenapa request ditolak "Unusual activity".

Pertanyaan yang harus dijawab sebelum bisa mengakali:
  1. Apakah token sentinel dibuat SEGAR tiap kiriman? (kalau ya, wajib minting sendiri)
  2. Apa isi respons /f/conversation/prepare? (belum pernah saya lihat)
  3. Kalau request dikirim dari DALAM halaman (fetch asli Firefox) tapi TANPA
     header sentinel, apakah errornya berbeda? → membuktikan apakah penolakan
     karena token, atau karena tumpukan jaringan (TLS/JA3 berbeda antara
     Playwright APIRequestContext dan fetch halaman).
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = "qyruoq@gumdel.com"
PROFIL = f"/root/chatgpt_profil_{EMAIL.split('@')[0]}"
KELUAR = Path("/root/api-google-flow/output")
catat = []


def ringkas_head(h):
    d = {}
    for k, v in (h or {}).items():
        kl = k.lower()
        if kl in ("openai-sentinel-chat-requirements-token",
                  "openai-sentinel-proof-token",
                  "openai-sentinel-turnstile-token", "x-conduit-token"):
            d[k] = {"len": len(v), "awal": v[:26]}
        elif kl in ("cookie", "authorization"):
            d[k] = f"<{len(v)} char>"
    return d


async def main():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()

        def on_req(req):
            u = req.url.split("?")[0]
            if u.endswith("/backend-api/f/conversation") or \
                    u.endswith("/backend-api/f/conversation/prepare"):
                catat.append({"arah": "REQ", "url": u,
                              "kepala": ringkas_head(req.headers),
                              "kepala_penuh": dict(req.headers or {}),
                              "isi": (req.post_data or "")[:1200]})

        def on_resp(resp):
            u = resp.url.split("?")[0]
            if u.endswith("/backend-api/f/conversation/prepare"):
                async def ambil():
                    try:
                        b = await resp.text()
                        catat.append({"arah": "RESP_PREPARE", "status": resp.status,
                                      "isi": b[:2500]})
                    except Exception as e:
                        catat.append({"arah": "RESP_PREPARE", "status": resp.status,
                                      "err": str(e)[:100]})
                asyncio.create_task(ambil())

        pg.on("request", on_req)
        pg.on("response", on_resp)
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(12)

        info = await pg.evaluate("""() => ({
            skrip: Array.from(document.querySelectorAll('script[src]'))
                     .map(s => s.src)
                     .filter(u => !/statsig|cdn\\.|gstatic|googleapis/.test(u)),
            global: Object.keys(window).filter(k => /sentinel|turnstile|wasm/i.test(k)),
            iframe: Array.from(document.querySelectorAll('iframe'))
                      .map(f => (f.src||'').slice(0,120))
        })""")
        print("=== SKRIP APLIKASI ===", flush=True)
        for s in info["skrip"][:14]:
            print("  ", s[:135], flush=True)
        print("=== GLOBAL ===", info["global"], flush=True)
        print("=== IFRAME ===", info["iframe"], flush=True)

        sel = '#prompt-textarea'
        await pg.wait_for_selector(sel, timeout=25000)
        kotak = await pg.query_selector(sel)
        for i, teks in enumerate(["halo apa kabar", "kucing merah di kursi"], 1):
            await kotak.click()
            await asyncio.sleep(1)
            await pg.keyboard.type(teks, delay=10)
            await asyncio.sleep(1)
            await pg.keyboard.press("Enter")
            print(f"\n  kirim #{i}: {teks}", flush=True)
            await asyncio.sleep(40)

        print("\n=== CATATAN ===", flush=True)
        for c in catat:
            if c["arah"] == "REQ":
                print(f"\n  REQ ...{c['url'].split('/backend-api')[-1]}", flush=True)
                for k, v in c["kepala"].items():
                    print(f"     {k}: {v}", flush=True)
            elif c["arah"] == "RESP_PREPARE":
                print(f"\n  RESP_PREPARE [{c['status']}]: "
                      f"{c.get('isi', c.get('err',''))[:900]}", flush=True)

        KELUAR.joinpath("recon_sent.json").write_text(
            json.dumps(catat, indent=1, ensure_ascii=False))

        # ---- uji tumpukan jaringan: fetch dari DALAM halaman ----
        print("\n=== UJI A: in-page fetch TANPA header sentinel ===", flush=True)
        ra = await pg.evaluate("""async () => {
            const body = {action:"next", model:"auto",
              parent_message_id:"client-created-root", timezone:"UTC",
              timezone_offset_min:0,
              messages:[{id:crypto.randomUUID(), author:{role:"user"},
                content:{content_type:"text", parts:["tes"]},
                recipient:"all", status:"finished_successfully", weight:1}],
              supported_encodings:["v1"], client_prepare_state:"success"};
            const r = await fetch("/backend-api/f/conversation", {method:"POST",
              headers:{"Content-Type":"application/json","Accept":"text/event-stream"},
              body: JSON.stringify(body), credentials:"include"});
            const t = await r.text();
            return {status:r.status, body:t.slice(0,500)};
        }""")
        print(f"  status {ra['status']} | {ra['body'][:320]}", flush=True)

        print("\n=== UJI B: in-page fetch DENGAN header terakhir yang terekam ===",
              flush=True)
        kepala = {}
        for c in reversed(catat):
            if c["arah"] == "REQ" and c["url"].endswith("/backend-api/f/conversation"):
                kepala = c["kepala_penuh"]
                break
        pakai = {k: v for k, v in kepala.items()
                 if k.lower().startswith("openai-sentinel")
                 or k.lower() in ("x-conduit-token", "oai-device-id",
                                  "oai-client-version", "oai-client-build-number",
                                  "oai-session-id", "x-oai-is-client-observation",
                                  "oai-language", "content-type")}
        rb = await pg.evaluate("""async (h) => {
            const body = {action:"next", model:"auto",
              parent_message_id:"client-created-root", timezone:"UTC",
              timezone_offset_min:0,
              messages:[{id:crypto.randomUUID(), author:{role:"user"},
                content:{content_type:"text", parts:["tes dua"]},
                recipient:"all", status:"finished_successfully", weight:1}],
              supported_encodings:["v1"], client_prepare_state:"success"};
            const r = await fetch("/backend-api/f/conversation", {method:"POST",
              headers:{...h, "Content-Type":"application/json",
                       "Accept":"text/event-stream"},
              body: JSON.stringify(body), credentials:"include"});
            const t = await r.text();
            return {status:r.status, body:t.slice(0,500)};
        }""", pakai)
        print(f"  header dipakai: {sorted(pakai)}", flush=True)
        print(f"  status {rb['status']} | {rb['body'][:320]}", flush=True)
        await ctx.close()


asyncio.run(main())