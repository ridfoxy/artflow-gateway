#!/usr/bin/env python3
"""LANGKAH 3: uji RPC PORTRAIT bergambar.

Temuan: rasio ter-encode di payload pada [4][15][4]
  1 = landscape   2 = portrait   (dari genai_template vs portrait_template)

Artinya portrait TIDAK butuh dokumen proyek Vids — cukup isi angka 2.
Uji: 1 gambar + portrait via RPC murni.
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

RASIO_PATH = (4, 15, 4)


def akun_sehat():
    import requests
    KEY = re.search(r"gflow_live_[A-Za-z0-9]+", (APP / "app.py").read_text()).group(0)
    d = requests.get("https://api.artflowgen.com/v1/vids/accounts",
                     headers={"X-API-Key": KEY}, timeout=25).json()
    return [a["email"] for a in d.get("accounts", []) if a.get("state") == "ok"]


def set_path(arr, jalur, nilai):
    n = arr
    for k in jalur[:-1]:
        n = n[k]
    n[jalur[-1]] = nilai
    return arr


def rakit_portrait_gambar(prompt, token, n_gambar=1):
    """Payload bergambar + rasio portrait (2)."""
    import vids_runner as V
    url, body = V.build_2image_body(prompt, [token] * n_gambar
                                    if n_gambar > 1 else [token])
    if not body:
        return None, None
    arr = json.loads(body)
    set_path(arr, RASIO_PATH, 2)          # 2 = portrait
    return url, json.dumps(arr, separators=(",", ":"))


async def main():
    akun = sys.argv[1] if len(sys.argv) > 1 else (akun_sehat() or [None])[0]
    print(f"=== LANGKAH 3: RPC PORTRAIT BERGAMBAR — {akun} ===\n")
    hasil = {"akun": akun, "mulai": time.strftime("%H:%M:%S")}

    gbr = Path("/root/eagle_google_flow_3f6d7ecb_frame.jpg")
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

            hdrs = await V.harvest_token(page, timeout=40, settle=2.0)
            print(f"[1] token: {bool(hdrs)}")
            if not hdrs:
                return

            tk = await V.upload_media_xhr(page, str(gbr))
            print(f"[2] unggah gambar: {'OK' if tk else 'GAGAL'} {str(tk)[:20]}")
            if not tk:
                return

            url, body = rakit_portrait_gambar(
                "portrait: burung elang terbang di langit senja", tk)
            if not body:
                print("[3] rakit GAGAL")
                return
            arr = json.loads(body)
            print(f"[3] payload: {len(body)} char | rasio [4][15][4] = "
                  f"{arr[4][15][4]} (2=portrait) | [2][25] len {len(arr[2][25])}")
            hasil["body_len"] = len(body)

            t = time.time()
            st, text = await V.xhr_post(page, url, body, hdrs, timeout_ms=180000)
            hasil["kirim_detik"] = round(time.time() - t, 1)
            hasil["http"] = st
            m = re.search(r"https://contribution-rt[^\"\\]+", text or "")
            print(f"[4] kirim HTTP {st} dalam {hasil['kirim_detik']}s | "
                  f"URL: {'ADA' if m else 'TIDAK'}")
            if not m:
                print(f"    respons: {(text or '')[:200]}")
                hasil["respons"] = (text or "")[:250]
                return

            t = time.time()
            data = await V.download_url(page, m.group(0).replace("\\u0026", "&"))
            hasil["unduh_detik"] = round(time.time() - t, 1)
            hasil["bytes"] = len(data) if data else 0
            print(f"[5] unduh {len(data) if data else 0} B")
            if data and len(data) > 50000:
                out = Path("/tmp/uji_portrait_rpc.mp4")
                out.write_bytes(data)
                # cek dimensi via ffprobe
                import subprocess
                r = subprocess.run(["ffprobe", "-v", "error", "-select_streams",
                                    "v:0", "-show_entries", "stream=width,height",
                                    "-of", "csv=p=0", str(out)],
                                   capture_output=True, text=True)
                hasil["dimensi"] = r.stdout.strip()
                print(f"[6] dimensi: {hasil['dimensi']} "
                      f"({'PORTRAIT ✓' if '720,1280' in hasil['dimensi'] or '1080,1920' in hasil['dimensi'] else 'bukan portrait'})")
                hasil["berkas"] = str(out)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    Path("/root/uji_portrait.json").write_text(json.dumps(hasil, indent=2))
    print("\n=== HASIL ===")
    for k, v in hasil.items():
        print(f"  {k:14}: {v}")


asyncio.run(main())