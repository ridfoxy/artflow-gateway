#!/usr/bin/env python3
"""UJI BANDING ADIL: RPC vs UI, keduanya end-to-end, akun sehat sama.

Jalur RPC : buka /videos/create -> klik "Create AI videos" -> panen token ->
            kirim payload template via XHR -> unduh MP4.
Jalur UI  : buka /videos/create -> klik "Create AI videos" -> ketik prompt di
            kolom (keyboard, delay 25ms seperti produksi) -> klik Generate ->
            tunggu video siap -> unduh MP4.

Diukur dari "halaman dibuka" sampai "MP4 tersimpan".
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
OUT = Path("/root/banding_rpc.json")

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


async def buka_editor(ctx, V):
    """Buka editor Vids: /videos/create + klik Create AI videos."""
    page = await ctx.new_page()
    await page.add_init_script(V.HOOK_JS)
    await page.goto(V.UPLOAD_DIR, wait_until="domcontentloaded", timeout=90000)
    await V.wait_until(page, """() => !!document.querySelector('button') &&
                               (document.body.innerText||'').length > 200""",
                       timeout=25)
    try:
        await page.locator('button:has-text("Create AI videos")').first.click(timeout=15000)
    except Exception:
        pass
    return page


async def jalur_rpc(ctx, V, akun, prompt, hasil):
    page = await buka_editor(ctx, V)
    try:
        t0 = time.time()
        hdrs = await V.harvest_token(page, timeout=40, settle=2.0)
        t_token = time.time() - t0
        if not hdrs:
            hasil["rpc"] = {"error": "token tidak dapat dipanen", "buka": round(t_token, 1)}
            return
        tpl = json.loads((APP / "data" / "portrait_template.json").read_text())
        body = V.build_body(tpl["body"], V.normalize_prompt(prompt))
        t1 = time.time()
        st, text = await V.xhr_post(page, tpl["url"], body, hdrs, timeout_ms=180000)
        t_kirim = time.time() - t1
        m = re.search(r"https://contribution-rt[^\"\\]+", text or "")
        if st != 200 or not m:
            hasil["rpc"] = {"http": st, "kirim": round(t_kirim, 1),
                            "error": (text or "")[:120]}
            return
        t2 = time.time()
        data = await V.download_url(page, m.group(0).replace("\\u0026", "&"))
        t_unduh = time.time() - t2
        hasil["rpc"] = {"http": st, "token": round(t_token, 1),
                       "kirim": round(t_kirim, 1), "unduh": round(t_unduh, 1),
                       "total": round(t_token + t_kirim + t_unduh, 1),
                       "bytes": len(data) if data else 0}
        if data and len(data) > 50000:
            (Path("/tmp") / "banding_rpc.mp4").write_bytes(data)
    finally:
        await page.close()


async def jalur_ui(ctx, V, akun, prompt, hasil):
    page = await buka_editor(ctx, V)
    try:
        # tunggu kolom prompt siap (jalur UI butuh editor matang)
        ok = False
        for _ in range(30):
            ok = await V.wait_until(
                page, """() => !!document.querySelector('[contenteditable=true],textarea')""",
                timeout=4)
            if ok:
                break
            await asyncio.sleep(2)
        t0 = time.time()
        typed = await V.type_prompt(page, V.normalize_prompt(prompt), "bandingui")
        t_ketik = time.time() - t0
        if not typed:
            hasil["ui"] = {"error": "kolom prompt tidak bisa dipakai",
                           "ketik": round(t_ketik, 1)}
            return
        t1 = time.time()
        try:
            await page.locator('button[aria-label="Generate"]').first.click(timeout=15000)
        except Exception as e:
            hasil["ui"] = {"error": f"Generate tidak bisa diklik: {str(e)[:60]}"}
            return
        t_klik = time.time() - t1
        t2 = time.time()
        src = await V.wait_video_ready(page, timeout=300, poll=2)
        t_render = time.time() - t2
        if not src:
            hasil["ui"] = {"ketik": round(t_ketik, 1), "klik": round(t_klik, 1),
                           "render": round(t_render, 1),
                           "error": "video tidak muncul sebelum batas waktu"}
            return
        t3 = time.time()
        data = await V.download_url(page, src["src"])
        t_unduh = time.time() - t3
        hasil["ui"] = {"ketik": round(t_ketik, 1), "klik": round(t_klik, 1),
                       "render": round(t_render, 1), "unduh": round(t_unduh, 1),
                       "total": round(t_ketik + t_klik + t_render + t_unduh, 1),
                       "bytes": len(data) if data else 0,
                       "durasi": src.get("dur"), "w": src.get("w"), "h": src.get("h")}
        if data and len(data) > 50000:
            (Path("/tmp") / "banding_ui.mp4").write_bytes(data)
    finally:
        await page.close()


async def main():
    akun = sys.argv[1] if len(sys.argv) > 1 else (akun_sehat() or [None])[0]
    if not akun:
        print("tidak ada akun sehat")
        return
    prompt = "banding: kucing oranye duduk di jendela, sinematik"
    hasil = {"akun": akun, "prompt": prompt, "mulai": time.strftime("%H:%M:%S")}
    print(f"=== BANDING RPC vs UI ===\nakun   : {akun}\nprompt : {prompt}\n")

    from playwright.async_api import async_playwright
    import vids_runner as V

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(APP / "chrome_profiles" / akun), headless=False,
            args=FLAG, viewport={"width": 1280, "height": 800})

        print("--- JALUR RPC ---")
        await jalur_rpc(ctx, V, akun, prompt, hasil)
        print("  ", json.dumps(hasil.get("rpc"), ensure_ascii=False))
        OUT.write_text(json.dumps(hasil, indent=2), encoding="utf-8")

        print("\n--- JALUR UI ---")
        await jalur_ui(ctx, V, akun, prompt, hasil)
        print("  ", json.dumps(hasil.get("ui"), ensure_ascii=False))
        OUT.write_text(json.dumps(hasil, indent=2), encoding="utf-8")

        await ctx.close()

    r, u = hasil.get("rpc") or {}, hasil.get("ui") or {}
    print("\n=== KESIMPULAN ===")
    if r.get("total"):
        print(f"  RPC total : {r['total']}s (token {r['token']} + kirim {r['kirim']} "
              f"+ unduh {r['unduh']}) | {r.get('bytes',0):,} B")
    else:
        print(f"  RPC       : gagal {r}")
    if u.get("total"):
        print(f"  UI  total : {u['total']}s (ketik {u['ketik']} + klik {u['klik']} "
              f"+ render {u['render']} + unduh {u['unduh']}) | {u.get('bytes',0):,} B")
    elif u:
        print(f"  UI        : tidak selesai {u}")
    if r.get("total") and u.get("total"):
        d = u["total"] - r["total"]
        print(f"\n  SELISIH: {abs(d):.1f}s — "
              f"{'RPC LEBIH CEPAT' if d > 0 else 'UI lebih cepat'}")
    print("\nhasil: /root/banding_rpc.json")


asyncio.run(main())