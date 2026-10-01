#!/usr/bin/env python3
"""UJI E2E: 2 gambar benar-benar lewat RPC (bukan UI).

Alur:
  1. buka editor + panen token
  2. unggah 2 gambar via upload_media_xhr -> dapat 2 token AVL
  3. rakit payload dengan build_2image_body
  4. kirim via xhr_post -> tunggu URL video
  5. unduh MP4 -> verifikasi

Tidak mengubah produksi; ini uji terpisah.
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

APP = Path("/root/api-google-flow")
sys.path.insert(0, str(APP))
os.environ.setdefault("DISPLAY", ":99")
FLAG = ["--display=:99", "--no-sandbox", "--disable-gpu", "--process-per-site",
        "--renderer-process-limit=1", "--disable-software-rasterizer",
        "--disable-extensions", "--disable-background-networking", "--disable-sync",
        "--disable-default-apps", "--disable-component-update", "--no-first-run"]


def akun_sehat():
    import requests
    KEY = re.search(r"gflow_live_[A-Za-z0-9]+", (APP / "app.py").read_text()).group(0)
    d = requests.get("https://api.artflowgen.com/v1/vids/accounts",
                     headers={"X-API-Key": KEY}, timeout=25).json()
    return [a["email"] for a in d.get("accounts", []) if a.get("state") == "ok"]


async def main():
    akun = sys.argv[1] if len(sys.argv) > 1 else (akun_sehat() or [None])[0]
    print(f"=== UJI E2E 2 GAMBAR via RPC — {akun} ===\n")
    hasil = {"akun": akun, "mulai": time.strftime("%H:%M:%S")}

    gbr = [Path("/root/eagle_google_flow_3f6d7ecb_frame.jpg"),
           Path("/root/featured_image_decoded.jpg")]
    gbr = [g for g in gbr if g.exists()]
    print(f"gambar uji: {[g.name for g in gbr]}")

    from playwright.async_api import async_playwright
    import vids_runner as V

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(APP / "chrome_profiles" / akun), headless=False,
            args=FLAG, viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()
        await page.add_init_script(V.HOOK_JS)
        try:
            await page.goto(V.UPLOAD_DIR, wait_until="domcontentloaded", timeout=90000)
            await V.wait_until(page, """() => !!document.querySelector('button') &&
                                       (document.body.innerText||'').length > 200""",
                               timeout=25)
            try:
                await page.locator('button:has-text("Create AI videos")').first.click(
                    timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(4)

            # panen token
            t0 = time.time()
            hdrs = await V.harvest_token(page, timeout=40, settle=2.0)
            print(f"[1] token: {bool(hdrs)} ({time.time()-t0:.1f}s)")
            if not hdrs:
                print("    gagal; berhenti")
                return
            hasil["token_siap"] = round(time.time() - t0, 1)

            # unggah gambar via XHR (tanpa UI)
            toks = []
            t1 = time.time()
            for i, g in enumerate(gbr, 1):
                tk = await V.upload_media_xhr(page, str(g))
                print(f"[2] unggah XHR gambar {i}: "
                      f"{'OK ' + tk[:20] + '...' if tk else 'GAGAL'}")
                if not tk:
                    break
                toks.append(tk)
            hasil["unggah_detik"] = round(time.time() - t1, 1)
            hasil["token_jumlah"] = len(toks)
            if len(toks) < 2:
                print(f"    hanya {len(toks)} token; uji 2 gambar tidak bisa lanjut")
                return

            # rakit payload 2 gambar
            url, body = V.build_2image_body(
                "dua burung berbeda warna di langit senja, sinematik", toks)
            if not body:
                print("[3] rakit payload GAGAL")
                return
            arr = json.loads(body)
            print(f"[3] payload siap: {len(body)} char, "
                  f"[2][25] len {len(arr[2][25])}")
            hasil["body_len"] = len(body)

            # kirim
            t2 = time.time()
            st, text = await V.xhr_post(page, url, body, hdrs, timeout_ms=180000)
            hasil["kirim_detik"] = round(time.time() - t2, 1)
            hasil["http"] = st
            m = re.search(r"https://contribution-rt[^\"\\]+", text or "")
            print(f"[4] kirim: HTTP {st} dalam {hasil['kirim_detik']}s | "
                  f"URL video: {'ADA' if m else 'TIDAK'}")
            if not m:
                print(f"    respons: {(text or '')[:220]}")
                hasil["respons"] = (text or "")[:300]
            else:
                t3 = time.time()
                data = await V.download_url(page, m.group(0).replace("\\u0026", "&"))
                hasil["unduh_detik"] = round(time.time() - t3, 1)
                hasil["bytes"] = len(data) if data else 0
                print(f"[5] unduh: {len(data) if data else 0} B dalam "
                      f"{hasil['unduh_detik']}s")
                if data and len(data) > 50000:
                    out = Path("/tmp/uji_2gambar_rpc.mp4")
                    out.write_bytes(data)
                    print(f"[6] tersimpan: {out}")
                    hasil["berkas"] = str(out)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    Path("/root/uji_2gambar.json").write_text(json.dumps(hasil, indent=2))
    print("\n=== HASIL ===")
    for k, v in hasil.items():
        print(f"  {k:16}: {v}")
    print("\nberkas: /root/uji_2gambar.json")


asyncio.run(main())