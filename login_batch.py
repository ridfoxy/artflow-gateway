#!/usr/bin/env python3
"""Jalankan login untuk sekumpulan akun, hasil dicatat ke JSON + log.

Dipisah per gelombang (chunk) supaya kalau Google mulai minta verifikasi di
tengah jalan, sisanya tidak ikut terbakar.

    python3 login_batch.py 3 13          # akun indeks 3..12 (0-based)
    python3 login_batch.py 3 13 --test-vids
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/root/api-google-flow")
RESULTS = APP / "data" / "login_results.jsonl"
PY = "/root/geminiomni/venv/bin/python"

sys.path.insert(0, str(APP))
import gsuite_login as G  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=int)
    ap.add_argument("end", type=int)
    ap.add_argument("--test-vids", action="store_true")
    args = ap.parse_args()

    entries, _ = G.load_accounts()
    chunk = entries[args.start:args.end]
    print(f"gelombang: akun {args.start}..{args.end - 1} ({len(chunk)} akun)")
    print()

    ok = fail = human = 0
    for i, e in enumerate(chunk, args.start):
        email = e["email"]
        cmd = [PY, "gsuite_login.py", "--email", email]
        if args.test_vids:
            cmd.append("--test-vids")
        t0 = time.time()
        r = subprocess.run(cmd, cwd=str(APP), capture_output=True, text=True,
                           timeout=600, env={**__import__("os").environ,
                                             "DISPLAY": ":99"})
        out = (r.stdout or "") + (r.stderr or "")
        state = "error"
        detail = ""
        for line in out.splitlines():
            if "-> ok:" in line:
                state, detail = "ok", line.split("-> ok:", 1)[1].strip()
            elif "-> needs_human:" in line:
                state, detail = "needs_human", line.split("needs_human:", 1)[1].strip()
            elif "-> bad_credentials:" in line:
                state, detail = "bad_credentials", line.split("bad_credentials:", 1)[1].strip()
            elif "-> error:" in line:
                state, detail = "error", line.split("-> error:", 1)[1].strip()
            elif "-> dry_run:" in line:
                state, detail = "dry_run", line.split("dry_run:", 1)[1].strip()

        rec = {"i": i, "email": email, "state": state, "detail": detail[:220],
               "secs": round(time.time() - t0, 1),
               "at": datetime.now(timezone.utc).isoformat()}
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        with RESULTS.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

        mark = {"ok": "OK ", "needs_human": "VER", "bad_credentials": "BAD",
                "error": "ERR", "dry_run": "DRY"}.get(state, "???")
        print(f"[{mark}] {i:2d} {email:26s} {rec['secs']:5.1f}s  {detail[:90]}")
        if state == "ok":
            ok += 1
        elif state == "needs_human":
            human += 1
        else:
            fail += 1

        # kalau verifikasi manual muncul, JANGAN lanjut: sisanya akan ikut kena
        if state == "needs_human" and (ok + fail) and human >= 2:
            print("\n!! beberapa akun minta verifikasi — gelombang dihentikan "
                  "supaya sisanya tidak ikut terbakar")
            break
        time.sleep(4)

    print()
    print(f"gelombang selesai: ok={ok} gagal={fail} butuh_verifikasi={human}")


main()