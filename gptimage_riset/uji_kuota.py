#!/usr/bin/env python3
"""UJI DEFINITIF: padukan baca-kuota RPC dengan generate nyata.

Menjawab tepat: berapa gambar per akun, dan apakah penolakan terjadi karena
kuota habis (remaining=0) atau throttle jangka pendek (remaining masih >0).

Tiap putaran: baca image_gen.remaining (RPC, tanpa UI) → generate 1 gambar (UI)
→ baca remaining lagi. Selisihnya = biaya per gambar.
"""
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "gthtbs@gumdel.com"
PROFIL = f"/root/chatgpt_profil_{EMAIL.split('@')[0]}"
KELUAR = Path("/root/api-google-flow/output/kuota_uji")
KELUAR.mkdir(parents=True, exist_ok=True)
B = "https://chatgpt.com/backend-api"

PROMPT = ["kucing putih di taman", "anjing hitam di pantai",
          "burung merah di dahan", "ikan biru di akuarium",
          "kuda coklat di padang", "ayam kuning di kandang"]
LIMIT_KHAS = ["batas penggunaan", "coba lagi sekitar", "reached",
              "try again later", "upgrade to continue"]


async def remaining(pg, kepala):
    """Baca sisa kuota gambar lewat RPC (tanpa UI)."""
    r = await pg.request.post(f"{B}/conversation/init", headers=kepala,
                              data=json.dumps(
                                  {"conversation_id": None,
                                   "conversation_origin": None, "gizmo_id": None,
                                   "requested_default_model": None,
                                   "system_hints": None, "timezone": "UTC",
                                   "timezone_offset_min": 0}), timeout=60000)
    j = await r.json()
    for x in j.get("limits_progress") or []:
        if x.get("feature_name") == "image_gen":
            return x.get("remaining"), x.get("reset_after")
    return None, None


async def main():
    from camoufox.async_api import AsyncCamoufox
    hasil = {"akun": EMAIL, "putaran": []}
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(10)
        r = await pg.request.get("https://chatgpt.com/api/auth/session",
                                 timeout=60000)
        ses = await r.json()
        if not ses.get("accessToken"):
            print("  !! tanpa sesi", flush=True)
            return
        kepala = {"Authorization": f"Bearer {ses['accessToken']}",
                  "Content-Type": "application/json", "oai-language": "en-US"}

        sisa0, reset0 = await remaining(pg, kepala)
        print(f"  {EMAIL}: sisa awal={sisa0} reset={reset0}", flush=True)
        hasil["sisa_awal"] = sisa0
        hasil["reset"] = reset0

        sel = '#prompt-textarea'
        try:
            await pg.wait_for_selector(sel, timeout=25000)
        except Exception:
            print("  !! kotak prompt tidak ada", flush=True)
            return
        kotak = await pg.query_selector(sel)

        for putaran in range(1, 11):
            teks = PROMPT[(putaran - 1) % len(PROMPT)]
            t0 = time.time()
            dasar = await pg.evaluate("""() => {
                const i = Array.from(document.querySelectorAll('img'))
                  .filter(x => x.src && x.src.includes('/backend-api/estuary'));
                return i.length ? i[i.length-1].src : '';
            }""")
            await kotak.click()
            await asyncio.sleep(0.8)
            await pg.keyboard.type(teks, delay=10)
            await asyncio.sleep(0.8)
            await pg.keyboard.press("Enter")
            print(f"\n  [{putaran}] {teks}", flush=True)

            baru, kena = None, None
            for _ in range(16):
                await asyncio.sleep(5)
                st = await pg.evaluate("""() => {
                    const imgs = Array.from(document.querySelectorAll('img'))
                      .filter(x => x.src && x.src.includes('/backend-api/estuary'));
                    const l = imgs.length ? imgs[imgs.length-1] : null;
                    return {src: l ? l.src : '', w: l ? l.naturalWidth : 0,
                            akhir: (document.body.innerText||'').slice(-700)};
                }""")
                if st["src"] and st["src"] != dasar:
                    baru = st
                    break
                k = [x for x in LIMIT_KHAS if x in st["akhir"].lower()]
                if k:
                    kena = (k, st["akhir"][-200:])
                    break

            # baca kuota SETELAH tiap percobaan
            sisa, reset = await remaining(pg, kepala)

            if baru:
                d = await (await pg.request.get(baru["src"], timeout=120000)).body()
                ok = len(d) > 10000 and d[:4] == b"\x89PNG"
                if ok:
                    (KELUAR / f"{putaran:02d}.png").write_bytes(d)
                print(f"      OK {baru['w']}px {len(d)}B {time.time()-t0:.0f}s "
                      f"| sisa kuota={sisa} (delta={None if sisa0 is None or sisa is None else sisa0-sisa-putaran+1})",
                      flush=True)
                hasil["putaran"].append({"ke": putaran, "status": "ok",
                                         "w": baru["w"], "sisa": sisa,
                                         "detik": round(time.time()-t0, 1)})
            elif kena:
                print(f"      LIMIT: {kena[0]} | sisa kuota={sisa}", flush=True)
                print(f"      pesan: {kena[1].strip()[:180]}", flush=True)
                hasil["putaran"].append({"ke": putaran, "status": "limit",
                                         "tanda": kena[0], "sisa": sisa})
                break
            else:
                print(f"      tidak muncul | sisa kuota={sisa}", flush=True)
                hasil["putaran"].append({"ke": putaran, "status": "timeout",
                                         "sisa": sisa})
                break
        await ctx.close()

    Path("/root/api-google-flow/output/kuota_uji.json").write_text(
        json.dumps(hasil, indent=1, ensure_ascii=False))
    ok = sum(1 for p in hasil["putaran"] if p["status"] == "ok")
    print(f"\n  === {EMAIL}: {ok} gambar | sisa awal {hasil.get('sisa_awal')} "
          f"→ akhir {hasil['putaran'][-1].get('sisa') if hasil['putaran'] else '?'} ===",
          flush=True)


asyncio.run(main())