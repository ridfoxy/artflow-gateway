#!/usr/bin/env python3
"""LANGKAH 1: REKAM payload 2 gambar dengan cara yang BENAR.

Cara lama gagal ("Non-multiple file input can only accept single file") karena
mengirim 2 berkas ke satu input. Cara benar (yang dipakai produksi sekarang):
  unggah gambar 1 -> klik "Add avatar or ingredients" -> unggah gambar 2.

Yang terekam:
  - payload generate yang dikirim aplikasi saat klik Generate
  - struktur [2][25] (penampung gambar) dan slot rujukan flix
  - token AVL masing-masing gambar

Read-only terhadap produksi: hanya menulis berkas baru di data/.
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
DATA = APP / "data"

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


def buat_gambar():
    """Dua gambar uji (pakai gambar nyata yang sudah ada, tanpa PIL)."""
    kandidat = ["/root/eagle_google_flow_3f6d7ecb_frame.jpg",
                "/root/featured_image_decoded.jpg",
                "/root/artflow_omni_live.png",
                "/root/dashboard_logged_in_verified.png"]
    ada = [Path(k) for k in kandidat if Path(k).exists()]
    if len(ada) < 2:
        # fallback: salin berkas apa pun yang berupa gambar
        ekstra = [p for p in Path("/root").glob("*.png") if p.stat().st_size > 20000]
        for e in ekstra:
            if e not in ada:
                ada.append(e)
            if len(ada) >= 2:
                break
    return ada[:2]


async def main():
    akun = sys.argv[1] if len(sys.argv) > 1 else (akun_sehat() or [None])[0]
    if not akun:
        print("tidak ada akun sehat")
        return
    print(f"=== REKAM PAYLOAD 2 GAMBAR — akun {akun} ===\n")

    gbr = buat_gambar()
    for g in gbr:
        print(f"  gambar uji: {g} ({g.stat().st_size} B)")

    from playwright.async_api import async_playwright
    import vids_runner as V

    rekaman = {"akun": akun, "waktu": time.strftime("%Y-%m-%d %H:%M:%S"),
               "catatan": []}
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
            print("[1] halaman siap")
            try:
                await page.locator('button:has-text("Create AI videos")').first.click(
                    timeout=15000)
                print("[2] editor dibuka (Create AI videos)")
            except Exception as e:
                print(f"[2] klik editor gagal: {str(e)[:70]}")
            await asyncio.sleep(5)

            # ── UNGGAH GAMBAR 1 ──────────────────────────────────────────
            print("\n[3] unggah gambar 1 (via helper produksi upload_media)...")
            ok1 = await V.upload_media(page, str(gbr[0]), "rekam2", 1)
            print(f"    hasil: {ok1} | thumbnail: {await V.count_thumbnails(page)}")
            rekaman["catatan"].append(f"gambar1_terpasang={ok1}")

            # ── UNGGAH GAMBAR 2 (via tombol Add avatar) ──────────────────
            print("\n[4] unggah gambar 2 (input dimunculkan tombol Add avatar)...")
            ok2 = await V.upload_media(page, str(gbr[1]), "rekam2", 2)
            th = await V.count_thumbnails(page)
            print(f"    hasil: {ok2} | thumbnail total: {th}")
            rekaman["catatan"].append(f"gambar2_terpasang={ok2} thumbnail={th}")

            # label chip yang muncul (bukti gambar benar terpasang)
            chips = await page.evaluate("""() => [...document.querySelectorAll('*')]
                .map(e=>(e.innerText||'').trim())
                .filter(t=>/^@Image\\d$/.test(t))""")
            print(f"    chip gambar: {sorted(set(chips))}")
            rekaman["chip"] = sorted(set(chips))

            # ── KETIK PROMPT + GENERATE ──────────────────────────────────
            print("\n[5] ketik prompt + klik Generate...")
            typed = await V.type_prompt(page, V.normalize_prompt(
                "rekam-2gambar: dua kucing berbeda warna"), "rekam2")
            print(f"    prompt terketik: {typed}")
            await page.evaluate("() => { window.__calls = []; }")
            try:
                await page.locator('button[aria-label="Generate"]').first.click(timeout=15000)
                print("    tombol Generate diklik")
            except Exception as e:
                print(f"    Generate gagal diklik: {str(e)[:70]}")

            # ── TANGKAP PAYLOAD ──────────────────────────────────────────
            print("\n[6] menangkap payload (paling panjang = payload sungguhan)...")
            tplm = None
            for _ in range(40):
                await asyncio.sleep(1.5)
                calls = await page.evaluate(
                    "() => JSON.stringify(window.__calls || [])")
                for c in json.loads(calls or "[]"):
                    if "genai/generate" not in str(c.get("url", "")):
                        continue
                    b = c.get("body") or ""
                    if len(b) > 500 and (tplm is None or len(b) > len(tplm["body"])):
                        tplm = c
                if tplm and len(tplm.get("body") or "") > 500:
                    break

            if not tplm:
                print("    !! payload tidak tertangkap")
                rekaman["payload"] = None
            else:
                d = DATA / "with_2images_payload_REAL.json"
                d.write_text(json.dumps(tplm, indent=2), encoding="utf-8")
                print(f"    payload tertangkap: {len(tplm['body'])} char -> {d.name}")
                # uraikan
                try:
                    arr = json.loads(tplm["body"])
                    def ambil(n, jalur):
                        for k in jalur:
                            if n is None:
                                return None
                            try:
                                n = n[k]
                            except Exception:
                                return None
                        return n
                    print(f"    [0]      = {ambil(arr, [0])}   (<- 374 teks / 376 1 gambar)")
                    slot = ambil(arr, [2, 8, 3, 0, 0, 7, 2])
                    print(f"    slot gambar [2][8][3][0][0][7][2]: {len(slot or [])} rujukan")
                    for s in (slot or [])[:4]:
                        print(f"       {json.dumps(s)[:110]}")
                    wadah = ambil(arr, [2, 25])
                    print(f"    [2][25] penampung: "
                          f"{'ADA' if wadah else 'NULL'} len="
                          f"{len(wadah) if isinstance(wadah, list) else '-'}")
                    avl = re.findall(r"AVL_0[A-Za-z0-9_\-]{30,}", tplm["body"])
                    print(f"    token AVL: {len(avl)}")
                    for a in avl[:4]:
                        print(f"       {a[:56]}")
                    rekaman.update({"body_len": len(tplm["body"]), "avl": len(avl),
                                    "slot_gambar": len(slot or []),
                                    "penampung_25": bool(wadah)})
                except Exception as e:
                    print(f"    urai gagal: {str(e)[:80]}")

            (Path("/root") / "rekam_2gambar.json").write_text(
                json.dumps(rekaman, indent=2), encoding="utf-8")
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    print("\n=== RINGKASAN REKAMAN ===")
    for c in rekaman.get("catatan", []):
        print(f"  {c}")
    print(f"  chip       : {rekaman.get('chip')}")
    print(f"  body_len   : {rekaman.get('body_len')}")
    print(f"  slot gambar: {rekaman.get('slot_gambar')}")
    print(f"  penampung  : {rekaman.get('penampung_25')}")
    print("\nberkas: /root/rekam_2gambar.json")


asyncio.run(main())