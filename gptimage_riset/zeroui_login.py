#!/usr/bin/env python3
"""ZERO-UI PENUH: login akun baru (sekali, via UI) lalu generate MURNI RPC.

Bagian generate TIDAK menyentuh UI sama sekali: tidak ada click/type pada
kompositor. Semua lewat HTTP + SSE, persis seperti yang terekam.

Alur:
  A. login (hanya jika profil belum punya sesi ChatGPT)
  B. GET  /api/auth/session                 -> Bearer
  C. POST sentinel/chat-requirements/prepare
  D. POST sentinel/chat-requirements/finalize
  E. POST conversation/init
  F. POST f/conversation  (SSE)             -> file id
  G. GET  files/download/file_<id>          -> metadata/tautan
  H. GET  estuary/content?id=...            -> PNG
"""
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "wwrbgy@gumdel.com"
PROMPT = sys.argv[2] if len(sys.argv) > 2 else "kucing putih lucu di taman bunga"
PROFIL = f"/root/chatgpt_profil_{EMAIL.split('@')[0]}"
KELUAR = Path("/root/api-google-flow/output/zeroui")
KELUAR.mkdir(parents=True, exist_ok=True)


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


async def main():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=PROFIL,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(9)

        # ---- A. token; login bila belum ada ----
        r = await pg.request.get("https://chatgpt.com/api/auth/session",
                                 timeout=60000)
        ses = {}
        try:
            ses = await r.json()
        except Exception:
            pass
        if not ses.get("accessToken"):
            print("  [A] belum login — jalankan alur Google sekali", flush=True)
            await pg.goto("https://chatgpt.com/auth/login",
                          wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(8)
            await klik(pg, 'button:has-text("Continue with Google")')
            await asyncio.sleep(10)
            if "accountchooser" in pg.url.lower():
                await klik(pg, 'text="Use another account"',
                           'text="Gunakan akun lain"')
                await asyncio.sleep(5)
            print("      email:", bool(await isi(pg, ['#identifierId'], EMAIL)),
                  flush=True)
            await asyncio.sleep(1)
            await klik(pg, "#identifierNext")
            await asyncio.sleep(9)
            print("      sandi:", bool(await isi(pg,
                  ['input[name="Passwd"]'], sandi(EMAIL))), flush=True)
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
                await klik(pg, 'button[type="submit"]')
                await asyncio.sleep(18)
            r = await pg.request.get("https://chatgpt.com/api/auth/session",
                                     timeout=60000)
            ses = await r.json()

        tok = ses.get("accessToken")
        if not tok:
            print("  !! tetap tanpa token — berhenti", flush=True)
            return
        print(f"  [A] token siap ({len(tok)} char) | "
              f"{(ses.get('user') or {}).get('email')}", flush=True)

        kepala = {"Authorization": f"Bearer {tok}",
                  "Content-Type": "application/json",
                  "oai-language": "en-US",
                  "Accept": "*/*",
                  "Origin": "https://chatgpt.com",
                  "Referer": "https://chatgpt.com/"}
        B = "https://chatgpt.com/backend-api"

        # ---- C. sentinel prepare ----
        r = await pg.request.post(f"{B}/sentinel/chat-requirements/prepare",
                                  headers=kepala,
                                  data=json.dumps({"p": ""}), timeout=60000)
        sj = {}
        try:
            sj = await r.json()
        except Exception:
            pass
        prep = sj.get("prepare_token") or sj.get("token") or ""
        print(f"  [C] prepare -> {r.status} | kunci {list(sj)[:6]} | "
              f"prepare_token {len(prep)} char", flush=True)

        # ---- E. init ----
        r = await pg.request.post(f"{B}/conversation/init", headers=kepala,
                                  data=json.dumps(
                                      {"conversation_id": None,
                                       "conversation_origin": None,
                                       "gizmo_id": None,
                                       "requested_default_model": None,
                                       "system_hints": None, "timezone": "UTC",
                                       "timezone_offset_min": 0}), timeout=60000)
        ij = {}
        try:
            ij = await r.json()
        except Exception:
            pass
        print(f"  [E] init -> {r.status} | kunci {list(ij)[:8]}", flush=True)

        # ---- D. finalize ----
        if prep:
            r = await pg.request.post(f"{B}/sentinel/chat-requirements/finalize",
                                      headers=kepala,
                                      data=json.dumps({"prepare_token": prep}),
                                      timeout=60000)
            fj = {}
            try:
                fj = await r.json()
            except Exception:
                pass
            print(f"  [D] finalize -> {r.status} | kunci {list(fj)[:8]}", flush=True)

        # ---- F. kirim pesan (SSE) ----
        pesan = {
            "turn_attribution": {"turn_trigger": "composer"},
            "action": "next", "is_do_not_remember": False, "model": "auto",
            "parent_message_id": "client-created-root",
            "timezone": "UTC", "timezone_offset_min": 0,
            "client_contextual_info": {"app_name": "chatgpt.com",
                                       "app_surface": "codex_browser"},
            "local_function_names": ["local.continue_in_work"],
            "messages": [{"author": {"metadata": {}, "name": None, "role": "user"},
                          "channel": None,
                          "content": {"content_type": "text", "parts": [PROMPT]},
                          "create_time": time.time(), "end_turn": None,
                          "id": str(uuid.uuid4()), "metadata": {},
                          "recipient": "all", "status": "finished_successfully",
                          "update_time": None, "weight": 1}],
            "supported_encodings": ["v1"],
            "client_prepare_state": "success",
        }
        t0 = time.time()
        r = await pg.request.post(f"{B}/f/conversation",
                                  headers={**kepala,
                                           "Accept": "text/event-stream"},
                                  data=json.dumps(pesan), timeout=300000)
        print(f"  [F] f/conversation -> {r.status} | {time.time()-t0:.0f}s", flush=True)
        teks = await r.text()
        print("      SSE:", len(teks), "byte", flush=True)
        (KELUAR / "sse_zeroui.txt").write_text(teks)
        if r.status != 200:
            print("      badan:", teks[:400], flush=True)
            return

        rendah = teks.lower()
        for tanda in ("batas penggunaan", "coba lagi sekitar", "reached your limit"):
            if tanda in rendah:
                print(f"      >> LIMIT: {tanda}", flush=True)

        idfile = list(dict.fromkeys(re.findall(r"file_[0-9a-f]{20,}", teks)))
        print("      file id:", idfile[:4], flush=True)
        if not idfile:
            return

        # ---- G/H. unduh ----
        fid = idfile[0]
        cid = (re.findall(r'"conversation_id"\s*:\s*"([0-9a-f-]{30,})"', teks) or
               re.findall(r"conversation_id=([0-9a-f-]{30,})", teks))
        cid = cid[0] if cid else ""
        r1 = await pg.request.get(
            f"{B}/files/download/{fid}"
            + (f"?conversation_id={cid}&download_intent=true" if cid else ""),
            timeout=120000)
        print(f"  [G] files/download -> {r1.status}", flush=True)
        d = await r1.body()
        print("      magic:", d[:4], "|", len(d), "byte", flush=True)
        if d[:4] == b"\x89PNG" and len(d) > 10000:
            f = KELUAR / "zeroui_hasil.png"
            f.write_bytes(d)
            print("      disimpan:", f, flush=True)
            print("\n  >>> ZERO-UI BERHASIL (gambar langsung dari RPC) <<<", flush=True)
        else:
            try:
                mj = json.loads(d)
                print("      kunci metadata:", list(mj)[:10], flush=True)
                url = mj.get("download_url") or mj.get("url") or ""
                if url:
                    r2 = await pg.request.get(url, timeout=120000)
                    d2 = await r2.body()
                    print(f"  [H] estuary -> {r2.status} | {len(d2)} byte | "
                          f"magic {d2[:4]}", flush=True)
                    if d2[:4] == b"\x89PNG":
                        (KELUAR / "zeroui_hasil.png").write_bytes(d2)
                        print("\n  >>> ZERO-UI BERHASIL <<<", flush=True)
            except Exception as e:
                print("      (bukan JSON):", str(e)[:80], flush=True)
        await ctx.close()


asyncio.run(main())