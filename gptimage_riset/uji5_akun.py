#!/usr/bin/env python3
"""FASE 1 — Uji limit GPT Image pada 5 akun + verifikasi rotasi akun.

Untuk tiap akun: login bila perlu, lalu minta gambar berulang sampai ditolak.
Hasil ditulis bertahap ke JSON supaya tahan gangguan.

Dipakai bareng mekanisme rotasi: begitu satu akun menolak, pindah akun berikutnya
tanpa menunggu (karena batasnya PER AKUN, bukan per IP).
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

AKUN = ["mninjl@gumdel.com", "unfqyr@gumdel.com", "khvxiy@gumdel.com",
        "qyruoq@gumdel.com", "gthtbs@gumdel.com"]
KELUAR = Path("/root/api-google-flow/output/gptimg5")
KELUAR.mkdir(parents=True, exist_ok=True)
LAPORAN = Path("/root/api-google-flow/output/limit_5akun.json")

PROMPT = [
    "kucing abu-abu di bangku taman", "anjing kecil berlari di pantai",
    "pemandangan gunung berkabut", "sepeda di depan dinding bata",
    "bunga matahari di ladang", "kopi panas di cangkir putih",
    "kapal layar di laut biru", "pasar tradisional Indonesia",
    "hutan pinus saat berkabut", "jembatan kayu di atas sungai",
]
MAKS_PER_AKUN = 12
JEDA = 5
BATAS_TUNGGU = 80
LIMIT_KHAS = ["batas penggunaan", "coba lagi sekitar", "reached",
              "try again later", "upgrade to continue", "sedang terkena batas"]


def sandi(email):
    for b in Path("/root/api-google-flow/accounts.txt").read_text(
            errors="replace").splitlines():
        s = b.strip()
        if s.startswith("#") or "|" not in s:
            continue
        e, p = s.split("|", 1)
        if e.strip().lower() == email.lower():
            return p.strip()


async def klik(pg, *sel, timeout=8000):
    for s in sel:
        try:
            await pg.click(s, timeout=timeout)
            return s
        except Exception:
            continue
    return None


async def isi(pg, sel, nilai, timeout=15000):
    for s in sel:
        try:
            await pg.fill(s, nilai, timeout=timeout)
            return s
        except Exception:
            continue
    return None


async def ada_kotak(pg, tunggu=15000):
    for sel in ('#prompt-textarea', 'div[contenteditable="true"]'):
        try:
            await pg.wait_for_selector(sel, timeout=tunggu)
            return sel
        except Exception:
            continue
    return None


async def uji_akun(email):
    """Kembalikan dict hasil untuk satu akun."""
    from camoufox.async_api import AsyncCamoufox
    pw = sandi(email)
    profil = f"/root/chatgpt_profil_{email.split('@')[0]}"
    hasil = {"akun": email, "gambar_ok": 0, "rincian": [], "catatan": ""}
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=profil,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(10)

        if not await ada_kotak(pg, 12000):
            await pg.goto("https://chatgpt.com/auth/login",
                          wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(8)
            await klik(pg, 'button:has-text("Continue with Google")')
            await asyncio.sleep(10)
            if "accountchooser" in pg.url.lower():
                await klik(pg, 'text="Use another account"',
                           'text="Gunakan akun lain"')
                await asyncio.sleep(5)
            if not await isi(pg, ['#identifierId'], email):
                hasil["catatan"] = "kolom email tidak muncul (akun mati?)"
                return hasil
            await asyncio.sleep(1)
            await klik(pg, "#identifierNext")
            await asyncio.sleep(9)
            if not await isi(pg, ['input[name="Passwd"]'], pw):
                hasil["catatan"] = "kolom sandi tidak muncul"
                return hasil
            await asyncio.sleep(1)
            await klik(pg, "#passwordNext")
            await asyncio.sleep(11)
            t = await pg.evaluate("() => (document.body.innerText||'')")
            if "sign in to openai" in t.lower():
                await klik(pg, '#submit_approve_access',
                           'button:has-text("Continue")')
                await asyncio.sleep(15)
            if "about-you" in pg.url:
                await isi(pg, ['input[name="name"]'], email.split("@")[0])
                await isi(pg, ['input[name="age"]'], "27")
                await klik(pg, 'button[type="submit"]')
                await asyncio.sleep(18)
            await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                          timeout=90000)
            await asyncio.sleep(12)

        sel = await ada_kotak(pg, 25000)
        if not sel:
            t = await pg.evaluate("() => (document.body.innerText||'').slice(0,200)")
            hasil["catatan"] = "gagal masuk: " + t.replace(chr(10), " ")[:140]
            return hasil

        for s in ('button:has-text("Continue")', 'button[aria-label*="Close"]'):
            try:
                await pg.click(s, timeout=2000)
                await asyncio.sleep(2)
            except Exception:
                pass
        kotak = await pg.query_selector(sel)

        for putaran in range(1, MAKS_PER_AKUN + 1):
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
            print(f"    [{email.split('@')[0]} #{putaran}] {teks[:40]}", flush=True)

            baru, kena = None, None
            for _ in range(BATAS_TUNGGU // JEDA):
                await asyncio.sleep(JEDA)
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
                    kena = k
                    break

            if baru:
                r = await pg.request.get(baru["src"], timeout=120000)
                d = await r.body()
                ok = len(d) > 10000 and d[:4] == b"\x89PNG"
                if ok:
                    (KELUAR / f"{email.split('@')[0]}_{putaran:02d}.png").write_bytes(d)
                    hasil["gambar_ok"] += 1
                print(f"      {baru['w']}px | {len(d)} B | {time.time()-t0:.0f}s | "
                      f"{'OK' if ok else 'BUKAN PNG'}", flush=True)
                hasil["rincian"].append({"ke": putaran, "status": "ok" if ok else "bukan_png",
                                         "w": baru["w"], "byte": len(d),
                                         "detik": round(time.time()-t0, 1)})
            elif kena:
                print(f"      >> LIMIT di gambar ke-{putaran}: {kena}", flush=True)
                hasil["rincian"].append({"ke": putaran, "status": "limit", "tanda": kena})
                break
            else:
                print("      >> tidak muncul (timeout)", flush=True)
                hasil["rincian"].append({"ke": putaran, "status": "timeout"})
                break
    return hasil


async def main():
    semua = []
    for em in AKUN:
        print(f"\n=== AKUN: {em} ===", flush=True)
        try:
            h = await uji_akun(em)
        except Exception as e:
            h = {"akun": em, "gambar_ok": 0, "rincian": [], "catatan": f"error: {e}"}
        semua.append(h)
        print(f"  HASIL {em}: {h['gambar_ok']} gambar | {h.get('catatan','')}",
              flush=True)
        LAPORAN.write_text(json.dumps(semua, indent=1, ensure_ascii=False))

    print("\n\n===== RINGKASAN 5 AKUN =====", flush=True)
    for h in semua:
        print(f"  {h['akun']:24} {h['gambar_ok']:3} gambar  {h.get('catatan','')}",
              flush=True)
    angka = [h["gambar_ok"] for h in semua if h["gambar_ok"] > 0]
    if angka:
        print(f"\n  rata-rata: {sum(angka)/len(angka):.1f} gambar/akun "
              f"| min {min(angka)} | maks {max(angka)}", flush=True)


asyncio.run(main())