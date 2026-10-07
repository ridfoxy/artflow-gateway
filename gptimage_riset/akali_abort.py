#!/usr/bin/env python3
"""AKALI: tahan request asli halaman sebelum terkirim, lalu pakai tokennya.

Logika:
  - Token sentinel (turnstile/PoW/requirements) dimintakan halaman SETIAP kali
    akan mengirim pesan, dan hangus hanya kalau request benar-benar SAMPAI.
  - Jadi: cegat request di lapisan jaringan (page.route) dan BATALKAN.
    Header + token ikut tertangkap, dan belum terpakai.
  - Lalu kirim prompt kita sendiri lewat fetch() dari dalam halaman (zero-UI,
    tanpa mengetik) memakai token segar itu.

Kalau ini 200 → pengiriman gambar bisa 100% HTTP, UI hanya dipakai "mint" token.
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
KELUAR = Path("/root/api-google-flow/output/akali")
KELUAR.mkdir(parents=True, exist_ok=True)

tertangkap = {}
dihitung = {"cegat": 0, "aktif": True}


async def main():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def cegat(route, request):
            # Hanya mencegat SELAMA mode panen. Setelah dimatikan, request lewat
            # bebas (kalau tidak, fetch kita sendiri juga ikut dibatalkan).
            if (dihitung["aktif"]
                    and request.url.split("?")[0].endswith("/backend-api/f/conversation")):
                tertangkap.clear()
                tertangkap.update(dict(request.headers or {}))
                tertangkap["_waktu"] = time.time()
                dihitung["cegat"] += 1
                print(f"  [cegat #{dihitung['cegat']}] request asli DITAHAN — "
                      f"token tersimpan", flush=True)
                await route.abort()
            else:
                await route.continue_()

        await pg.route("**/backend-api/f/conversation", cegat)
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(12)

        # intip isi halaman: skrip, global sentinel, iframe
        info = await pg.evaluate("""() => ({
            global: Object.keys(window).filter(k => /sentinel|turnstile|wasm/i.test(k)),
            iframe: Array.from(document.querySelectorAll('iframe')).map(f => (f.src||'(kosong)').slice(0,110)),
            wasm: performance.getEntriesByType('resource')
                    .map(r => r.name).filter(n => /sentinel|wasm/i.test(n)).slice(0,6)
        })""")
        print("  global sentinel:", info["global"], flush=True)
        print("  iframe:", info["iframe"], flush=True)
        print("  sumber sentinel:", info["wasm"], flush=True)

        sel = '#prompt-textarea'
        await pg.wait_for_selector(sel, timeout=25000)
        kotak = await pg.query_selector(sel)

        # ---- Langkah 1: picu halaman membangun token, lalu tahan ----
        print("\n  [1] mengetik untuk memicu mint token (request akan ditahan)...",
              flush=True)
        await kotak.click()
        await asyncio.sleep(1)
        await pg.keyboard.type("pemicu token", delay=10)
        await asyncio.sleep(1)
        await pg.keyboard.press("Enter")

        for _ in range(20):
            await asyncio.sleep(2)
            if tertangkap.get("openai-sentinel-turnstile-token"):
                break
        if not tertangkap.get("openai-sentinel-turnstile-token"):
            print("  !! token tidak tertangkap", flush=True)
            return
        print("  [1] token tertangkap (belum terpakai):", flush=True)
        for k in sorted(tertangkap):
            if k.startswith("openai-sentinel") or k in ("x-conduit-token",
                                                        "oai-device-id"):
                print(f"      {k}: {len(tertangkap[k])} char", flush=True)

        # ---- Langkah 2: kirim prompt SENDIRI lewat fetch dalam halaman ----
        pakai = {k: v for k, v in tertangkap.items()
                 if k.lower().startswith("openai-sentinel")
                 or k.lower() in ("x-conduit-token", "oai-device-id",
                                  "oai-client-version", "oai-client-build-number",
                                  "oai-session-id", "oai-language",
                                  "x-oai-is-client-observation")}
        dihitung["aktif"] = False   # lepas route agar fetch kita lolos
        print(f"\n  [2] mengirim sendiri via fetch (token: {len(pakai)})", flush=True)
        for i, prompt in enumerate(["kucing hijau di taman", "burung kuning di pohon"], 1):
            t0 = time.time()
            hasil = await pg.evaluate("""async ([h, teks]) => {
                const body = {
                  turn_attribution:{turn_trigger:"composer"},
                  action:"next", is_do_not_remember:false, model:"auto",
                  parent_message_id:"client-created-root",
                  timezone:"UTC", timezone_offset_min:0,
                  local_function_names:["local.continue_in_work"],
                  messages:[{id:crypto.randomUUID(), author:{role:"user"},
                    content:{content_type:"text", parts:[teks]},
                    recipient:"all", status:"finished_successfully", weight:1}],
                  supported_encodings:["v1"], client_prepare_state:"success"};
                const r = await fetch("/backend-api/f/conversation", {method:"POST",
                  headers:{...h, "Content-Type":"application/json",
                           "Accept":"text/event-stream"},
                  body: JSON.stringify(body), credentials:"include"});
                const t = await r.text();
                return {status:r.status, isi:t};
            }""", [pakai, prompt])
            print(f"  [{i}] status {hasil['status']} | {time.time()-t0:.0f}s | "
                  f"{len(hasil['isi'])} byte", flush=True)
            if hasil["status"] != 200:
                print("      badan:", hasil["isi"][:300], flush=True)
                if i == 1:
                    # token sudah terpakai → mint ulang untuk percobaan berikut
                    print("      (mint ulang untuk percobaan 2...)", flush=True)
                    dihitung["aktif"] = True
                    tertangkap.clear()
                    await kotak.click()
                    await asyncio.sleep(1)
                    await pg.keyboard.type("pemicu kedua", delay=10)
                    await asyncio.sleep(1)
                    await pg.keyboard.press("Enter")
                    for _ in range(20):
                        await asyncio.sleep(2)
                        if tertangkap.get("openai-sentinel-turnstile-token"):
                            break
                    pakai = {k: v for k, v in tertangkap.items()
                             if k.lower().startswith("openai-sentinel")
                             or k.lower() in ("x-conduit-token", "oai-device-id",
                                              "oai-client-version",
                                              "oai-client-build-number",
                                              "oai-session-id", "oai-language",
                                              "x-oai-is-client-observation")}
                    dihitung["aktif"] = False
                    print(f"      token baru: {len(pakai)}", flush=True)
                continue

            # 200 → ambil file id + unduh
            teks = hasil["isi"]
            (KELUAR / f"sse_{i}.txt").write_text(teks)
            rendah = teks.lower()
            for tanda in ("batas penggunaan", "coba lagi sekitar"):
                if tanda in rendah:
                    print(f"      >> LIMIT: {tanda}", flush=True)
            ids = list(dict.fromkeys(re.findall(r"file_[0-9a-f]{20,}", teks)))
            print("      file id:", ids[:3], flush=True)
            if ids:
                fid = ids[0]
                d = await pg.evaluate("""async (fid) => {
                    const r = await fetch("/backend-api/files/download/" + fid,
                      {credentials:"include"});
                    const t = await r.text();
                    return {status:r.status, isi:t.slice(0,600)};
                }""", fid)
                print(f"      unduh: {d['status']} | {d['isi'][:200]}", flush=True)
                m = re.search(r'(https://chatgpt\.com/backend-api/estuary/content[^"\\ ]+)',
                              d["isi"])
                if m:
                    url = m.group(1).replace("\\u0026", "&")
                    png = await pg.evaluate("""async (u) => {
                        const r = await fetch(u, {credentials:"include"});
                        const b = new Uint8Array(await r.arrayBuffer());
                        let bin = ""; const step = 8192;
                        for (let i=0;i<b.length;i+=step)
                          bin += String.fromCharCode.apply(null, b.subarray(i,i+step));
                        return {status:r.status, b64: btoa(bin), n: b.length,
                                awal: Array.from(b.subarray(0,4))};
                    }""", url)
                    print(f"      estuary {png['status']} | {png['n']} byte | "
                          f"magic {png['awal']}", flush=True)
                    if png["awal"] == [137, 80, 78, 71]:
                        import base64
                        (KELUAR / f"akali_{i}.png").write_bytes(
                            base64.b64decode(png["b64"]))
                        print("\n  >>>>>> BERHASIL: KIRIM MURNI HTTP, GAMBAR UTUH <<<<<<",
                              flush=True)
                        break

        await ctx.close()


asyncio.run(main())