#!/usr/bin/env python3
"""Baca KUOTA lewat RPC (tanpa UI): conversation/init + wham/usage.

Uji zero-UI yang MUDAH dicapai: membaca limit tidak butuh token sentinel.
Ini juga menjawab "berapa limit satu akun" dari sisi server, bukan dari pesan.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

AKUN = ["fverdj@gumdel.com", "dpoiku@gumdel.com", "qyruoq@gumdel.com"]


async def baca(email):
    from camoufox.async_api import AsyncCamoufox
    profil = f"/root/chatgpt_profil_{email.split('@')[0]}"
    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=profil,
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(9)
        B = "https://chatgpt.com/backend-api"
        r = await pg.request.get("https://chatgpt.com/api/auth/session",
                                 timeout=60000)
        ses = await r.json()
        if not ses.get("accessToken"):
            print(f"  [{email}] tanpa sesi", flush=True)
            return None
        kepala = {"Authorization": f"Bearer {ses['accessToken']}",
                  "Content-Type": "application/json", "oai-language": "en-US"}
        out = {"akun": email, "plan": (ses.get("account") or {}).get("plan_type")}

        # 1. conversation/init -> model_limits / limits_progress
        r = await pg.request.post(f"{B}/conversation/init", headers=kepala,
                                  data=json.dumps(
                                      {"conversation_id": None,
                                       "conversation_origin": None,
                                       "gizmo_id": None,
                                       "requested_default_model": None,
                                       "system_hints": None, "timezone": "UTC",
                                       "timezone_offset_min": 0}), timeout=60000)
        ij = await r.json()
        for k in ("model_limits", "limits_progress", "blocked_features",
                  "default_model_slug", "atlas_mode_enabled"):
            if k in ij:
                out[k] = ij[k]
        print(f"\n  === {email} (plan={out['plan']}) ===", flush=True)
        print("  conversation/init →", flush=True)
        for k in ("model_limits", "limits_progress"):
            if k in out:
                print(f"    {k}: {json.dumps(out[k], ensure_ascii=False)[:700]}",
                      flush=True)

        # 2. wham/usage
        r = await pg.request.get(f"{B}/wham/usage", headers=kepala, timeout=60000)
        print(f"  wham/usage -> {r.status}", flush=True)
        try:
            wj = await r.json()
            print("    kunci:", list(wj)[:12], flush=True)
            out["wham"] = wj
            print("    isi:", json.dumps(wj, ensure_ascii=False)[:900], flush=True)
        except Exception as e:
            print("    (bukan json)", str(e)[:60], flush=True)

        # 3. accounts/check
        r = await pg.request.get(f"{B}/accounts/check/v4-2023-04-27",
                                 headers=kepala, timeout=60000)
        try:
            aj = await r.json()
            ak = ((aj.get("accounts") or {}).get("default") or {})
            out["entitlement"] = ak.get("entitlement")
            print("  entitlement:", json.dumps(ak.get("entitlement") or {},
                                              ensure_ascii=False)[:400], flush=True)
        except Exception:
            pass
        await ctx.close()
        return out


async def main():
    semua = []
    for em in AKUN:
        try:
            h = await baca(em)
            if h:
                semua.append(h)
        except Exception as e:
            print(f"  [{em}] error: {e}", flush=True)
    Path("/root/api-google-flow/output/kuota_rpc.json").write_text(
        json.dumps(semua, indent=1, ensure_ascii=False))
    print("\n  tersimpan: output/kuota_rpc.json", flush=True)


asyncio.run(main())