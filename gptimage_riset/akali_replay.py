#!/usr/bin/env python3
"""AKALI tahap 2 — replay PERSIS body+header asli halaman, hanya prompt diganti.

Tahap 1 sudah menembus bot detection (403 "Unusual activity" hilang, tinggal 500).
500 berarti token diterima tapi body kurang tepat. Uji ini menangkap body asli
yang dibangun halaman, membatalkan pengiriman, lalu mengirim ULANG body yang sama
persis lewat fetch() (tanpa mengetik) → kalau 200, jalur zero-UI murni terbuka.
"""
import asyncio
import base64
import json
import re
import sys
import time
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "fverdj@gumdel.com"
PROMPT_BARU = sys.argv[2] if len(sys.argv) > 2 else "kucing hijau lucu di taman"
PROFIL = f"/root/chatgpt_profil_{EMAIL.split('@')[0]}"
KELUAR = Path("/root/api-google-flow/output/akali")
KELUAR.mkdir(parents=True, exist_ok=True)

panen = {}
mode = {"cegat": True}


async def main():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def cegat(route, request):
            if mode["cegat"] and request.url.split("?")[0].endswith(
                    "/backend-api/f/conversation"):
                panen["kepala"] = dict(request.headers or {})
                panen["body"] = request.post_data or ""
                panen["url"] = request.url
                print("  [cegat] request asli ditahan — header+body tersimpan",
                      flush=True)
                await route.abort()
            else:
                await route.continue_()

        await pg.route("**/backend-api/f/conversation", cegat)
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(12)

        sel = '#prompt-textarea'
        await pg.wait_for_selector(sel, timeout=25000)
        kotak = await pg.query_selector(sel)

        # ---------- LANGKAH 1: panen body+header asli ----------
        await kotak.click()
        await asyncio.sleep(1)
        await pg.keyboard.type("pemicu satu", delay=10)
        await asyncio.sleep(1)
        await pg.keyboard.press("Enter")
        for _ in range(20):
            await asyncio.sleep(2)
            if panen.get("body"):
                break
        if not panen.get("body"):
            print("  !! tidak terpanen", flush=True)
            return
        mode["cegat"] = False
        print(f"  panjang body asli: {len(panen['body'])} byte", flush=True)
        try:
            b0 = json.loads(panen["body"])
            print("  kunci body:", sorted(b0), flush=True)
            print("  prompt asli:", b0["messages"][0]["content"]["parts"],
                  flush=True)
        except Exception as e:
            print("  (body bukan json murni)", str(e)[:60], flush=True)

        # ---------- LANGKAH 2: replay PERSIS (tanpa ubah apa pun) ----------
        print("\n  [2a] replay PERSIS body asli...", flush=True)
        r = await pg.evaluate("""async ([h, body]) => {
            const r = await fetch("/backend-api/f/conversation", {method:"POST",
              headers:{...h, "Content-Type":"application/json",
                       "Accept":"text/event-stream"},
              body, credentials:"include"});
            const t = await r.text();
            return {status:r.status, isi:t};
        }""", [panen["kepala"], panen["body"]])
        print(f"      status {r['status']} | {len(r['isi'])} byte", flush=True)
        print(f"      awal: {r['isi'][:300]}", flush=True)

        if r["status"] == 200:
            print("\n  >>> REPLAY PERSIS BERHASIL — body-nya kunci <<<", flush=True)
            await simpan(pg, r["isi"], "replay")

        # ---------- LANGKAH 3: mint token baru + ganti prompt ----------
        print("\n  [3] mint token baru, lalu kirim dengan prompt berbeda...",
              flush=True)
        mode["cegat"] = True
        panen.clear()
        await kotak.click()
        await asyncio.sleep(1)
        await pg.keyboard.type(PROMPT_BARU, delay=10)
        await asyncio.sleep(1)
        await pg.keyboard.press("Enter")
        for _ in range(20):
            await asyncio.sleep(2)
            if panen.get("body"):
                break
        mode["cegat"] = False
        if not panen.get("body"):
            print("  !! panen kedua gagal", flush=True)
            return

        # ganti prompt di body asli
        b = json.loads(panen["body"])
        b["messages"][0]["content"]["parts"] = [PROMPT_BARU]
        body2 = json.dumps(b, ensure_ascii=False)
        print(f"      prompt diganti → {PROMPT_BARU!r}", flush=True)
        r2 = await pg.evaluate("""async ([h, body]) => {
            const r = await fetch("/backend-api/f/conversation", {method:"POST",
              headers:{...h, "Content-Type":"application/json",
                       "Accept":"text/event-stream"},
              body, credentials:"include"});
            const t = await r.text();
            return {status:r.status, isi:t};
        }""", [panen["kepala"], body2])
        print(f"      status {r2['status']} | {len(r2['isi'])} byte", flush=True)
        print(f"      awal: {r2['isi'][:300]}", flush=True)
        if r2["status"] == 200:
            print("\n  >>> KIRIM MURNI HTTP + PROMPT SENDIRI BERHASIL <<<",
                  flush=True)
            await simpan(pg, r2["isi"], "gantiprompt")
        await ctx.close()


async def simpan(pg, sse, nama):
    """Ambil file id dari SSE lalu unduh gambar. 200 bukan berarti ada gambar."""
    print(f"      SSE {nama}: {len(sse)} byte", flush=True)
    (KELUAR / f"sse_{nama}.txt").write_text(sse)
    rendah = sse.lower()
    for t in ("batas penggunaan", "coba lagi sekitar", "unusual activity"):
        if t in rendah:
            print(f"      >> catatan: {t}", flush=True)
    ids = list(dict.fromkeys(re.findall(r"file_[0-9a-f]{20,}", sse)))
    print("      file id:", ids[:4], flush=True)
    if not ids:
        return
    fid = ids[0]
    r = await pg.evaluate("""async (fid) => {
        const r = await fetch("/backend-api/files/download/" + fid,
          {credentials:"include"});
        return {status:r.status, isi:(await r.text()).slice(0,800)};
    }""", fid)
    print(f"      files/download {r['status']}: {r['isi'][:250]}", flush=True)
    m = re.search(r'(https://chatgpt\.com/backend-api/estuary/content[^"\\ ]+)',
                  r.get("isi", ""))
    if not m:
        return
    url = m.group(1).replace("\\u0026", "&")
    png = await pg.evaluate("""async (u) => {
        const r = await fetch(u, {credentials:"include"});
        const b = new Uint8Array(await r.arrayBuffer());
        let bin = ""; const step = 8192;
        for (let i=0;i<b.length;i+=step)
          bin += String.fromCharCode.apply(null, b.subarray(i,i+step));
        return {status:r.status, b64:btoa(bin), n:b.length,
                awal:Array.from(b.subarray(0,4))};
    }""", url)
    print(f"      estuary {png['status']} | {png['n']} byte | magic {png['awal']}",
          flush=True)
    if png["awal"] == [137, 80, 78, 71]:
        f = KELUAR / f"{nama}.png"
        f.write_bytes(base64.b64decode(png["b64"]))
        print(f"\n  >>> GAMBAR UTUH DISIMPAN: {f} <<<", flush=True)


asyncio.run(main())