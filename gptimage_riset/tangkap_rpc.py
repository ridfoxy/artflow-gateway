#!/usr/bin/env python3
"""TANGKAP JALUR RPC GPT IMAGE (untuk zero-UI nanti).

Login 1 akun baru + rekam SEMUA lalu lintas jaringan saat generate gambar.
Yang dicari:
  - endpoint pembuatan percakapan & pengiriman pesan
  - endpoint streaming jawaban (conduit/SSE)
  - token otorisasi (Bearer) yang dipakai
  - parameter yang menentukan model gambar

Hasil: /root/api-google-flow/output/rpc_gptimg.json  (tanpa token mentah,
tokoh disamarkan panjangnya saja).
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "sjftpv@gumdel.com"
PROFIL = f"/root/chatgpt_profil_{EMAIL.split('@')[0]}"
KELUAR = Path("/root/api-google-flow/output")
PROMPT = "kucing abu-abu lucu duduk di bangku taman"

rekaman = []


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


async def ada_kotak(pg, tunggu=20000):
    for sel in ('#prompt-textarea', 'div[contenteditable="true"]'):
        try:
            await pg.wait_for_selector(sel, timeout=tunggu)
            return sel
        except Exception:
            continue
    return None


def pasang_perekam(pg):
    """Rekam request & response yang relevan."""
    def catat_req(req):
        u = req.url
        if not any(k in u for k in ("backend-api", "openai.com", "conduit",
                                    "chatgpt.com/api", "estuary")):
            return
        kepala = {}
        for k, v in (req.headers or {}).items():
            kl = k.lower()
            if kl == "authorization":
                kepala[k] = f"<{len(v)} char>"
            elif kl in ("cookie",):
                kepala[k] = f"<{len(v)} char>"
            elif kl in ("content-type", "oai-device-id", "oai-language",
                        "openai-beta", "x-openai-target-route",
                        "x-openai-target-path", "user-agent"):
                kepala[k] = v[:120]
        isi_req = None
        try:
            if req.post_data:
                isi_req = req.post_data[:3000]
        except Exception:
            pass
        rekaman.append({"t": round(time.time(), 2), "arah": "req",
                        "metode": req.method, "url": u[:200],
                        "kepala": kepala, "isi": isi_req})

    def catat_resp(resp):
        u = resp.url
        if not any(k in u for k in ("backend-api", "openai.com", "conduit",
                                    "chatgpt.com/api", "estuary")):
            return
        ct = (resp.headers or {}).get("content-type", "")
        rekaman.append({"t": round(time.time(), 2), "arah": "resp",
                        "status": resp.status, "url": u[:200],
                        "tipe": ct[:80]})

    pg.on("request", catat_req)
    pg.on("response", catat_resp)


async def main():
    pw = sandi(EMAIL)
    print("  akun:", EMAIL, "| sandi:", bool(pw), flush=True)
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        pasang_perekam(pg)

        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(10)

        if not await ada_kotak(pg, 12000):
            print("  login dulu...", flush=True)
            await pg.goto("https://chatgpt.com/auth/login",
                          wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(8)
            await klik(pg, 'button:has-text("Continue with Google")')
            await asyncio.sleep(10)
            if "accountchooser" in pg.url.lower():
                await klik(pg, 'text="Use another account"',
                           'text="Gunakan akun lain"')
                await asyncio.sleep(5)
            r1 = await isi(pg, ['#identifierId'], EMAIL)
            print("  email:", bool(r1), flush=True)
            await asyncio.sleep(1)
            await klik(pg, "#identifierNext")
            await asyncio.sleep(9)
            r2 = await isi(pg, ['input[name="Passwd"]'], pw)
            print("  sandi:", bool(r2), flush=True)
            await asyncio.sleep(1)
            await klik(pg, "#passwordNext")
            await asyncio.sleep(11)
            t = await pg.evaluate("() => (document.body.innerText||'')")
            if "sign in to openai" in t.lower():
                await klik(pg, '#submit_approve_access',
                           'button:has-text("Continue")')
                await asyncio.sleep(15)
            if "about-you" in pg.url:
                await isi(pg, ['input[name="name"]'], EMAIL.split("@")[0])
                await isi(pg, ['input[name="age"]'], "27")
                await klik(pg, 'button[type="submit"]',
                           'button:has-text("Continue")')
                await asyncio.sleep(18)
            await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                          timeout=90000)
            await asyncio.sleep(12)

        sel = await ada_kotak(pg, 25000)
        print("  kotak prompt:", sel, flush=True)
        if not sel:
            t = await pg.evaluate("() => (document.body.innerText||'').slice(0,300)")
            print("  !! gagal login:", t.replace(chr(10), " | ")[:250], flush=True)
            KELUAR.joinpath("rpc_gptimg.json").write_text(
                json.dumps(rekaman, indent=1, ensure_ascii=False))
            return

        for s in ('button:has-text("Continue")', 'button[aria-label*="Close"]'):
            try:
                await pg.click(s, timeout=2000)
                await asyncio.sleep(2)
            except Exception:
                pass

        # ---- rekam dari sini: generate gambar ----
        rekaman.append({"t": round(time.time(), 2), "arah": "TANDA",
                        "metode": "MULAI", "url": "--- mulai generate ---"})
        kotak = await pg.query_selector(sel)
        await kotak.click()
        await asyncio.sleep(1)
        await pg.keyboard.type(PROMPT, delay=12)
        await asyncio.sleep(1)
        await pg.keyboard.press("Enter")
        print("  prompt dikirim, menunggu 70s...", flush=True)
        await asyncio.sleep(70)

        st = await pg.evaluate("""() => {
            const i = Array.from(document.querySelectorAll('img'))
              .filter(x => x.src && x.src.includes('/backend-api/estuary'));
            return {n: i.length, src: i.length ? i[i.length-1].src : '',
                    w: i.length ? i[i.length-1].naturalWidth : 0,
                    akhir: (document.body.innerText||'').slice(-300)};
        }""")
        print("  gambar:", st["n"], "| lebar:", st["w"], flush=True)
        if st["src"]:
            r = await pg.request.get(st["src"], timeout=120000)
            d = await r.body()
            if len(d) > 10000 and d[:4] == b"\x89PNG":
                KELUAR.joinpath("rpc_test.png").write_bytes(d)
                print("  gambar disimpan:", len(d), "byte", flush=True)

        # ---- token dari localStorage / cookie ----
        sesi = await pg.evaluate("""() => {
            const o = {};
            try {
                o.kunci_local = Object.keys(localStorage);
                for (const k of Object.keys(localStorage)) {
                    if (/token|sess|auth/i.test(k)) {
                        const v = localStorage.getItem(k) || '';
                        o['L_' + k] = v.slice(0, 200) + ' <' + v.length + ' char>';
                    }
                }
            } catch(e) { o.err_local = String(e); }
            try { o.cookie_nama = document.cookie.split(';').map(c => c.split('=')[0].trim()); }
            catch(e) {}
            return o;
        }""")
        print("\n  === SESI ===", flush=True)
        print(json.dumps(sesi, indent=1, ensure_ascii=False)[:1500], flush=True)

        KELUAR.joinpath("rpc_gptimg.json").write_text(
            json.dumps(rekaman, indent=1, ensure_ascii=False))
        print("\n  rekaman:", len(rekaman), "entri →", KELUAR / "rpc_gptimg.json",
              flush=True)
        await ctx.close()


asyncio.run(main())