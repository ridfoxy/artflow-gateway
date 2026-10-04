#!/usr/bin/env python3
"""ZERO-UI HIBRIDA: panen token sentinel dari kiriman UI, lalu kirim MURNI RPC.

Temuan: POST /backend-api/f/conversation tanpa token sentinel dijawab
  403 {"detail":"Unusual activity has been detected from your device..."}
Header wajib (tertangkap dari kiriman asli):
  openai-sentinel-chat-requirements-token
  openai-sentinel-proof-token
  openai-sentinel-turnstile-token
  x-conduit-token, oai-device-id, oai-client-version/build-number, dll.
Token sentinel berumur 540 detik (expire_after).

Uji ini: kirim 1x lewat UI untuk memanen token, lalu kirim prompt BERIKUTNYA
lewat HTTP murni (tanpa click/type) memakai token hasil panen.
"""
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "fverdj@gumdel.com"
PROFIL = f"/root/chatgpt_profil_{EMAIL.split('@')[0]}"
KELUAR = Path("/root/api-google-flow/output/zeroui")
KELUAR.mkdir(parents=True, exist_ok=True)

panen = {}
hasil_ui = None


async def main():
    from camoufox.async_api import AsyncCamoufox
    global hasil_ui

    def catat(req):
        if req.url.split("?")[0].endswith("/backend-api/f/conversation"):
            panen.clear()
            panen.update(dict(req.headers or {}))

    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        pg.on("request", catat)
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(12)

        sel = '#prompt-textarea'
        await pg.wait_for_selector(sel, timeout=25000)
        kotak = await pg.query_selector(sel)

        # ---------- LANGKAH 1: kirim sekali lewat UI untuk memanen token ----------
        await kotak.click()
        await asyncio.sleep(1)
        await pg.keyboard.type("kucing oranye di taman", delay=12)
        await asyncio.sleep(1)
        await pg.keyboard.press("Enter")
        print("  [1] kirim UI (pemanenan token)...", flush=True)
        for _ in range(14):
            await asyncio.sleep(5)
            if panen.get("openai-sentinel-chat-requirements-token"):
                break
        kunci_wajib = ["openai-sentinel-chat-requirements-token",
                       "openai-sentinel-proof-token",
                       "openai-sentinel-turnstile-token", "x-conduit-token",
                       "oai-device-id", "oai-client-version",
                       "oai-client-build-number", "oai-session-id",
                       "oai-echo-logs", "x-oai-is-client-observation"]
        print("  [1] panen:", flush=True)
        for k in kunci_wajib:
            v = panen.get(k, "")
            print(f"      {k}: {len(v)} char" if v else f"      {k}: (kosong)",
                  flush=True)
        if not panen.get("openai-sentinel-turnstile-token"):
            print("  !! token tidak terpanen — berhenti", flush=True)
            return

        # ---------- LANGKAH 2: kirim MURNI RPC dengan token hasil panen ----------
        B = "https://chatgpt.com/backend-api"
        r = await pg.request.get("https://chatgpt.com/api/auth/session",
                                 timeout=60000)
        ses = await r.json()
        tok = ses.get("accessToken")

        kepala = {"Authorization": f"Bearer {tok}",
                  "Content-Type": "application/json",
                  "Accept": "text/event-stream",
                  "oai-language": "en-US",
                  "origin": "https://chatgpt.com",
                  "referer": "https://chatgpt.com/",
                  "x-openai-target-path": "/backend-api/f/conversation",
                  "x-openai-target-route": "/backend-api/f/conversation",
                  "x-openai-web-frontend": "core_web",
                  "x-openai-web-sse-compression": "gzip"}
        for k in kunci_wajib:
            if panen.get(k):
                kepala[k] = panen[k]

        pesan = {
            "turn_attribution": {"turn_trigger": "composer"},
            "action": "next", "is_do_not_remember": False, "model": "auto",
            "parent_message_id": "client-created-root",
            "timezone": "UTC", "timezone_offset_min": 0,
            "client_contextual_info": {"app_name": "chatgpt.com",
                                       "app_surface": "codex_browser"},
            "local_function_names": ["local.continue_in_work"],
            "messages": [{"author": {"metadata": {}, "name": None, "role": "user"},
                          "channel": None,
                          "content": {"content_type": "text",
                                      "parts": ["kucing biru di taman"]},
                          "create_time": time.time(), "end_turn": None,
                          "id": str(uuid.uuid4()), "metadata": {},
                          "recipient": "all", "status": "finished_successfully",
                          "update_time": None, "weight": 1}],
            "supported_encodings": ["v1"],
            "client_prepare_state": "success",
        }
        print("\n  [2] kirim MURNI RPC (tanpa UI)...", flush=True)
        t0 = time.time()
        try:
            r2 = await pg.request.post(f"{B}/f/conversation", headers=kepala,
                                       data=json.dumps(pesan), timeout=300000)
            print(f"  [2] status {r2.status} | {time.time()-t0:.0f}s", flush=True)
            teks = await r2.text()
            (KELUAR / "sse_hibrida.txt").write_text(teks)
            print("      SSE:", len(teks), "byte", flush=True)
            if r2.status != 200:
                print("      badan:", teks[:400], flush=True)
            else:
                rendah = teks.lower()
                for t in ("batas penggunaan", "coba lagi sekitar"):
                    if t in rendah:
                        print(f"      >> LIMIT: {t}", flush=True)
                ids = list(dict.fromkeys(re.findall(r"file_[0-9a-f]{20,}", teks)))
                print("      file id:", ids[:3], flush=True)
                if ids:
                    fid = ids[0]
                    cid = (re.findall(r"conversation_id=([0-9a-f-]{30,})", teks) or
                           re.findall(r'"conversation_id"\s*:\s*"([0-9a-f-]{30,})"', teks))
                    url = f"{B}/files/download/{fid}"
                    if cid:
                        url += f"?conversation_id={cid[0]}&download_intent=true"
                    r3 = await pg.request.get(url, timeout=120000)
                    d = await r3.body()
                    print(f"  [3] unduh -> {r3.status} | magic {d[:4]} | "
                          f"{len(d)} byte", flush=True)
                    if d[:4] == b"\x89PNG" and len(d) > 10000:
                        (KELUAR / "zeroui_murni.png").write_bytes(d)
                        print("\n  >>> ZERO-UI MURNI BERHASIL <<<", flush=True)
                    else:
                        try:
                            mj = json.loads(d)
                            u = mj.get("download_url") or ""
                            if u:
                                r4 = await pg.request.get(u, timeout=120000)
                                d4 = await r4.body()
                                print(f"      estuary {r4.status} | {len(d4)} B | "
                                      f"magic {d4[:4]}", flush=True)
                                if d4[:4] == b"\x89PNG":
                                    (KELUAR / "zeroui_murni.png").write_bytes(d4)
                                    print("\n  >>> ZERO-UI MURNI BERHASIL <<<",
                                          flush=True)
                        except Exception as ex:
                            print("      (bukan json):", str(ex)[:80], flush=True)
        except Exception as e:
            print("  [2] gagal:", str(e)[:150], flush=True)
        await ctx.close()


asyncio.run(main())