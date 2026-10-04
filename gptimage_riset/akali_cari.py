#!/usr/bin/env python3
"""Cari percakapan berisi gambar hasil RPC murni, lalu unduh gambarnya."""
import asyncio
import base64
import json
import re
import sys
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "dpoiku@gumdel.com"
KELUAR = Path("/root/api-google-flow/output/akali")


async def main():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=f"/root/chatgpt_profil_{EMAIL.split('@')[0]}",
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(10)
        B = "/backend-api"

        daftar = await pg.evaluate("""async () => {
            const r = await fetch("/backend-api/conversations?offset=0&limit=12&order=updated",
              {credentials:"include"});
            const j = await r.json();
            return (j.items||[]).map(x => ({id:x.id, judul:x.title,
                                            waktu:x.update_time}));
        }""")
        print(f"  percakapan: {len(daftar)}", flush=True)
        for d in daftar:
            print(f"    {d['judul'][:45]:47} {d['id']}", flush=True)

        target = None
        for d in daftar:
            if "kucing hijau" in (d["judul"] or "").lower():
                target = d
                break
        if not target and daftar:
            target = daftar[0]
        if not target:
            print("  !! tidak ada percakapan", flush=True)
            return
        print(f"\n  target: {target['judul']} / {target['id']}", flush=True)

        for label, url in [
            ("detail", f"{B}/conversation/{target['id']}"),
            ("detail+hidden", f"{B}/conversation/{target['id']}?include_visually_hidden_messages=true"),
        ]:
            r = await pg.evaluate("""async (u) => {
                const r = await fetch(u, {credentials:"include"});
                return {status:r.status, isi:(await r.text()).slice(0,200)};
            }""", url)
            print(f"  [{label}] {r['status']} {r['isi'][:120]}", flush=True)

        # ambil semua file id + pointer gambar dari detail
        det = await pg.evaluate("""async (cid) => {
            const r = await fetch("/backend-api/conversation/" + cid,
              {credentials:"include"});
            const j = await r.json();
            const ids = new Set();
            const walk = (o) => {
                if (!o || typeof o !== 'object') return;
                if (typeof o === 'string') {
                    const m = o.match(/file_[0-9a-f]{20,}/g);
                    if (m) m.forEach(x => ids.add(x));
                    return;
                }
                for (const k in o) walk(o[k]);
            };
            walk(j);
            return {status:r.status, ids:[...ids],
                    pesan: Object.keys(j.mapping||{}).length};
        }""", target["id"])
        print(f"  detail status {det['status']} | simpul {det.get('pesan')} | "
              f"file id {det['ids'][:6]}", flush=True)

        if not det["ids"]:
            return
        fid = det["ids"][0]
        r = await pg.evaluate("""async ([fid, cid]) => {
            const u = "/backend-api/files/download/" + fid +
                      "?conversation_id=" + cid + "&download_intent=true";
            const r = await fetch(u, {credentials:"include"});
            return {status:r.status, isi:(await r.text()).slice(0,900)};
        }""", [fid, target["id"]])
        print(f"  unduh {r['status']}: {r['isi'][:300]}", flush=True)
        m = re.search(r'https://chatgpt\.com/backend-api/estuary/content[^"\\ ]+',
                      r.get("isi", ""))
        if not m:
            return
        url = m.group(0).replace("\\u0026", "&")
        png = await pg.evaluate("""async (u) => {
            const r = await fetch(u, {credentials:"include"});
            const b = new Uint8Array(await r.arrayBuffer());
            let bin = ""; const step = 8192;
            for (let i=0;i<b.length;i+=step)
              bin += String.fromCharCode.apply(null, b.subarray(i,i+step));
            return {status:r.status, b64:btoa(bin), n:b.length,
                    awal:Array.from(b.subarray(0,4))};
        }""", url)
        print(f"  estuary {png['status']} | {png['n']} byte | magic {png['awal']}",
              flush=True)
        if png["awal"] == [137, 80, 78, 71]:
            f = KELUAR / "RPC-murni.png"
            f.write_bytes(base64.b64decode(png["b64"]))
            print(f"\n  >>> GAMBAR RPC-MURNI DISIMPAN: {f} <<<", flush=True)
        await ctx.close()


asyncio.run(main())