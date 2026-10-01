#!/usr/bin/env python3
"""Login massal akun baru lewat Camoufox (jalur yang terbukti berhasil).

Terbukti hari ini:
  - Akun asepqds3/4/5 (yang GAGAL total sebelumnya) berhasil dipulihkan:
    "Saya mengerti" diklik -> myaccount.google.com -> Vids terbuka ✅
  - Kunci: folder profil Firefox TERPISAH + klik setuju SABAR & DIULANG

Cara pakai:
  /root/harbor/venv/bin/python login_camoufox_massal.py            # semua yang no_login
  /root/harbor/venv/bin/python login_camoufox_massal.py --limit 5
  /root/harbor/venv/bin/python login_camoufox_massal.py --pola asepqds

Hasil dicatat ke data/login_results.jsonl (sama seperti jalur lain).
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/root/api-google-flow")
FF_SCRIPT = APP / "camoufox_login.py"
ACCOUNTS = APP / "accounts.txt"
RESULTS = APP / "data" / "login_results.jsonl"
VIDS_DB = APP / "data" / "vids_jobs.db"
PY_FF = "/root/harbor/venv/bin/python"


def catat(rec):
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def akun_bermasalah(pola=None):
    """Email yang state-nya no_login / ui_retry di vids_jobs.db."""
    out = []
    try:
        con = sqlite3.connect(f"file:{VIDS_DB}?mode=ro", uri=True)
        for r in con.execute("SELECT email, state FROM vids_accounts "
                             "WHERE state IN ('no_login','ui_retry')"):
            if pola and pola.lower() not in str(r[0]).lower():
                continue
            out.append(r[0])
        con.close()
    except Exception as e:
        print(f"  ! gagal baca vids_jobs.db: {e}")
    return out


def semua_akun(pola=None):
    emails = []
    for l in ACCOUNTS.read_text(errors="replace").splitlines():
        l = l.strip()
        if not l or l.startswith("#") or "|" not in l:
            continue
        e = l.split("|", 1)[0].strip()
        if pola and pola.lower() not in e.lower():
            continue
        emails.append(e)
    return emails


def beban_tinggi(ambang=12.0):
    try:
        with open("/proc/loadavg") as fh:
            return float(fh.read().split()[0]) > ambang
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pola", default="", help="saring email (mis. asepqds)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jeda", type=int, default=8, help="jeda antar akun (detik)")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--semua", action="store_true",
                    help="coba SEMUA akun, bukan hanya yang bermasalah")
    args = ap.parse_args()

    daftar = semua_akun(args.pola) if args.semua else akun_bermasalah(args.pola)
    if args.limit:
        daftar = daftar[:args.limit]

    print(f"=== LOGIN CAMOUFOX MASSAL ===")
    print(f"  akun akan diproses : {len(daftar)}")
    print(f"  jeda antar akun    : {args.jeda}s")
    print(f"  ambang beban       : 12.0")
    print(flush=True)

    tally = {"ok": 0, "gagal": 0, "lain": 0}
    for i, email in enumerate(daftar, 1):
        if beban_tinggi():
            print("\n!! beban mesin tinggi — dihentikan agar gateway tetap melayani.")
            break

        profil = APP / "firefox_profiles" / email
        profil.mkdir(parents=True, exist_ok=True)
        cmd = [PY_FF, str(FF_SCRIPT), "--email", email,
               "--profile", str(profil), "--test-vids"]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=str(APP), capture_output=True, text=True,
                               timeout=args.timeout,
                               env={**os.environ, "DISPLAY": ":99"})
            out = (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            out, r = "timeout", None

        state, detail = "error", ""
        for line in out.splitlines():
            for tag, nama in (("-> ok:", "ok"), ("-> needs_human:", "needs_human"),
                              ("-> bad_credentials:", "bad_credentials"),
                              ("-> error:", "error")):
                if tag in line:
                    state, detail = nama, line.split(tag, 1)[1].strip()
        secs = round(time.time() - t0, 1)

        tanda = {"ok": "OK ", "needs_human": "VER", "bad_credentials": "BAD",
                 "error": "ERR"}.get(state, "???")
        print(f"[{tanda}] {i:2d}/{len(daftar)} {email:26s} {secs:5.1f}s {detail[:80]}",
              flush=True)
        catat({"email": email, "state": state, "detail": detail, "secs": secs,
               "at": datetime.now(timezone.utc).isoformat(), "via": "camoufox-massal"})

        if state == "ok":
            tally["ok"] += 1
        elif state == "needs_human":
            tally["lain"] += 1
        else:
            tally["gagal"] += 1

        if i < len(daftar):
            time.sleep(args.jeda)

    print(f"\n=== SELESAI: ok={tally['ok']} gagal={tally['gagal']} "
          f"lain={tally['lain']} ===", flush=True)


if __name__ == "__main__":
    main()