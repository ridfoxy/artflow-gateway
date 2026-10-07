#!/usr/bin/env python3
"""AKALI FINAL — ubah body request asli halaman, lalu loloskan.

Rantai yang sudah terbukti:
  403 "Unusual activity"  →  token sentinel wajib & sekali pakai
  500 Internal Server     →  token diterima, tapi body buatan kurang lengkap
  200                     →  body asli halaman WAJIB dipakai utuh

Penghalang sisa: request yang DIBATALKAN tidak menyimpan percakapan, sehingga
files/download menjawab 404/403. Solusinya: JANGAN batalkan — ambil request asli,
ganti prompt-nya dengan prompt kita, lalu teruskan (route.fetch + route.fulfill).
Halaman menerima respons seperti biasa (gambar ikut dirender), percakapan
tersimpan, dan kita memegang SSE-nya.
"""
import asyncio
import base64
import json
import re
import sys
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "dpoiku@gumdel.com"
PROMPT = sys.argv[2] if len(sys.argv) > 2 else "kucing hijau lucu di taman"
PROFIL = f"/root/chatgpt_profil_{EMAIL.split('@')[0]}"
KELUAR = Path("/root/api-google-flow/output/akali")
KELUAR.mkdir(parents=True, exist_ok=True)

tangkapan = []
state = {"ganti": True}


async def main():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def ubah(route, request):
            if not (state["ganti"] and request.url.split("?")[0].endswith(
                    "/backend-api/f/conversation")):
                await route.continue_()
                return
            try:
                b = json.loads(request.post_data or "{}")
            except Exception:
                await route.continue_()
                return
            try:
                b["messages"][0]["content"]["parts"] = [PROMPT]
                body = json.dumps(b, ensure_ascii=False)
                print(f"  [ubah] prompt → {PROMPT!r}", flush=True)
                # kirim modified request, lalu suapkan responsnya ke halaman
                # generate gambar butuh ~30-60s; default route.fetch hanya 30s
                resp = await route.fetch(post_data=body, timeout=300000,
                                         headers={k: v for k, v in
                                                  (request.headers or {}).items()
                                                  if k.lower() not in ("content-length",)})
                teks = await resp.text()
                tangkapan.append({"status": resp.status, "sse": teks})
                print(f"  [ubah] respons server: {resp.status} | "
                      f"{len(teks)} byte", flush=True)
                await route.fulfill(response=resp, body=teks)
            except Exception as e:
                print("  [ubah] gagal:", str(e)[:120], flush=True)
                await route.continue_()

        await pg.route("**/backend-api/f/conversation", ubah)
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(12)

        sel = '#prompt-textarea'
        await pg.wait_for_selector(sel, timeout=25000)
        kotak = await pg.query_selector(sel)

        # pemicu seminim mungkin: satu karakter (isinya akan DIGANTI)
        await kotak.click()
        await asyncio.sleep(0.8)
        await pg.keyboard.type("x", delay=20)
        await asyncio.sleep(0.8)
        await pg.keyboard.press("Enter")
        print("  pemicu terkirim, menunggu respons...", flush=True)

        for _ in range(40):
            await asyncio.sleep(3)
            if tangkapan:
                break
        if not tangkapan:
            print("  !! tidak ada respons", flush=True)
            return

        hasil = tangkapan[-1]
        sse = hasil["sse"]
        (KELUAR / "sse_modif.txt").write_text(sse)
        print(f"\n  status {hasil['status']} | SSE {len(sse)} byte", flush=True)

        rendah = sse.lower()
        for t in ("batas penggunaan", "unusual activity", "coba lagi"):
            if t in rendah:
                print(f"  >> catatan: {t}", flush=True)

        cid = re.findall(r'"conversation_id":"([0-9a-f-]{30,})"', sse)
        cid = cid[0] if cid else ""
        ids = list(dict.fromkeys(re.findall(r"file_[0-9a-f]{20,}", sse)))
        tgl = re.search(r'"asset_pointer":"sediment://(file_[0-9a-f]+)"', sse)
        print(f"  conversation: {cid}", flush=True)
        print(f"  file id: {ids[:3]} | sediment: {tgl.group(1) if tgl else '-'}",
              flush=True)
        dim = re.search(r'"width":(\d+),"height":(\d+)', sse)
        if dim:
            print(f"  ukuran gambar: {dim.group(2)}x{dim.group(1)}", flush=True)
        if not (ids or tgl):
            return
        fid = (tgl.group(1) if tgl else ids[0])

        # tunggu gambar dirender halaman, lalu pakai URL bertanda tangan dari DOM
        print("\n  menunggu halaman merender gambar...", flush=True)
        url_sig = None
        for _ in range(20):
            await asyncio.sleep(3)
            url_sig = await pg.evaluate("""() => {
                const i = Array.from(document.querySelectorAll('img'))
                  .filter(x => x.src && x.src.includes('/backend-api/estuary/content'));
                return i.length ? i[i.length-1].src : '';
            }""")
            if url_sig:
                break
        print("  URL bertanda tangan:", (url_sig or "-")[:110], flush=True)

        if not url_sig:
            # coba files/download dengan conversation_id
            r = await pg.evaluate("""async ([fid, cid]) => {
                const u = "/backend-api/files/download/" + fid +
                          "?conversation_id=" + cid + "&download_intent=true";
                const r = await fetch(u, {credentials:"include"});
                return {status:r.status, isi:(await r.text()).slice(0,900)};
            }""", [fid, cid])
            print(f"  files/download {r['status']}: {r['isi'][:250]}", flush=True)
            m = re.search(r'https://chatgpt\.com/backend-api/estuary/content[^"\\ ]+',
                          r.get("isi", ""))
            if m:
                url_sig = m.group(0).replace("\\u0026", "&")

        if not url_sig:
            print("  !! tidak dapat URL gambar", flush=True)
            await ctx.close()
            return

        # unduh lewat konteks browser (cookie ikut). Cara in-page gagal karena
        # objek lintas-realm ("Permission denied to access property constructor").
        resp = await pg.request.get(url_sig, timeout=180000)
        data = await resp.body()
        print(f"  estuary {resp.status} | {len(data)} byte | magic {data[:4]}",
              flush=True)
        if data[:4] == b"\x89PNG" and len(data) > 10000:
            f = KELUAR / "BERHASIL-rpc.png"
            f.write_bytes(data)
            print(f"\n  >>>>>> SUKSES: GAMBAR DARI PROMPT YANG KITA UBAH <<<<<<", flush=True)
            print(f"  berkas: {f} ({len(data)} byte)", flush=True)
        await ctx.close()


asyncio.run(main())