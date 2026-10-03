#!/usr/bin/env python3
"""Login massal PARALEL — 3 akun sekaligus (mesin 6 core / 16 GB).

Alasan: cara sekuensial ~87s/akun -> 65 akun ≈ 95 menit. Paralel 3 ->
≈ 32 menit. Tiap akun memakai profil Firefox SENDIRI, jadi aman.

Pengaman:
  - beban mesin dijaga (mulai tugas baru hanya bila beban < BATAS)
  - hasil per akun ditulis ke data/login_results.jsonl (dibaca panel bot)
  - bisa dimatikan total dengan menekan Ctrl-C; akun yang sudah selesai tetap
    tercatat
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/root/api-google-flow")
FF_SCRIPT = APP / "camoufox_login.py"
ACCOUNTS = APP / "accounts.txt"
RESULTS = APP / "data" / "login_results.jsonl"
PY_FF = "/root/harbor/venv/bin/python"
BATAS_BEBAN = float(os.environ.get("LOGIN_LOAD_MAX", "20"))
PARALEL = int(os.environ.get("LOGIN_PARALEL", "3"))


def beban() -> float:
    try:
        return float(open("/proc/loadavg").read().split()[0])
    except Exception:
        return 0.0


def catat(rec: dict) -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def daftar_akun(pola: str = "", limit: int = 0) -> list[str]:
    emails = []
    for line in ACCOUNTS.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "|" not in s:
            continue
        email = s.split("|")[0].strip()
        if "@" not in email:
            continue
        if pola and pola.lower() not in email.lower():
            continue
        emails.append(email)
    return emails[:limit] if limit else emails


async def login_satu(email: str, sem: asyncio.Semaphore, idx: int,
                     total: int) -> tuple[str, str]:
    async with sem:
        # tunggu beban turun sebelum mulai akun baru
        tunggu = 0
        while beban() > BATAS_BEBAN and tunggu < 300:
            await asyncio.sleep(15)
            tunggu += 15
        profil = APP / "firefox_profiles" / email
        cmd = [PY_FF, str(FF_SCRIPT), "--email", email,
               "--profile", str(profil), "--test-vids"]
        mulai = time.time()
        try:
            r = await asyncio.to_thread(
                subprocess.run, cmd, cwd=str(APP), capture_output=True,
                text=True, timeout=420)
            out = (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            out = f"-> error: {str(e)[:120]}"
        dt = time.time() - mulai

        state, alasan = "error", "tidak diketahui"
        for line in out.splitlines():
            for tag, nama in (("-> ok:", "ok"), ("-> needs_human:", "needs_human"),
                              ("-> bad_credentials:", "bad"),
                              ("-> deleted:", "deleted"),
                              ("-> error:", "error")):
                if tag in line:
                    state = nama
                    alasan = line.split(tag, 1)[1].strip()[:200]
        catat({"email": email, "state": state, "detail": alasan,
               "at": datetime.now(timezone.utc).isoformat(),
               "detik": round(dt, 1), "mesin": "camoufox"})
        tanda = {"ok": "OK ", "needs_human": "GL ", "bad": "BAD",
                 "deleted": "DEL", "error": "ERR"}.get(state, "???")
        print(f"[{tanda}] {idx:2}/{total} {email:26} {dt:6.1f}s {alasan[:70]}",
              flush=True)
        return email, state


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pola", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--paralel", type=int, default=PARALEL)
    a = ap.parse_args()

    email_list = daftar_akun(a.pola, a.limit)
    print(f"=== LOGIN PARALEL ({a.paralel} sekaligus) ===", flush=True)
    print(f"  akun  : {len(email_list)}", flush=True)
    print(f"  batas beban: {BATAS_BEBAN}", flush=True)

    sem = asyncio.Semaphore(a.paralel)
    tugas = [login_satu(e, sem, i, len(email_list))
             for i, e in enumerate(email_list, 1)]
    hasil = await asyncio.gather(*tugas)

    from collections import Counter
    c = Counter(s for _, s in hasil)
    print("\n=== RINGKASAN ===", flush=True)
    for k, v in c.most_common():
        print(f"  {k:14} {v}", flush=True)
    print(f"  TOTAL          {len(hasil)}", flush=True)


asyncio.run(main())