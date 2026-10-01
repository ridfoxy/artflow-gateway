#!/usr/bin/env python3
"""Login semua akun sisanya, satu per satu dengan jeda panjang + uji ulang.

- Jeda lebih panjang antar akun supaya Google tidak membatasi
- Akun yang sudah 'ok' dilewati (pakai data/login_results.jsonl)
- Akun yang macet diulang sekali setelah jeda
- Menulis hasil ke data/login_results.jsonl

    python3 login_all.py            # semua yang belum ok
    python3 login_all.py --retry    # termasuk yang pernah gagal
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/root/api-google-flow")
RESULTS = APP / "data" / "login_results.jsonl"
PY = "/root/geminiomni/venv/bin/python"
DELAY = 25          # jeda antar akun (detik) — mengurangi throttle
PER_ACCOUNT_TIMEOUT = 420

sys.path.insert(0, str(APP))
import gsuite_login as G  # noqa: E402


def done_ok():
    """Email yang sudah berhasil login."""
    ok = set()
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("state") == "ok":
                    ok.add(r["email"].lower())
            except Exception:
                pass
    return ok


def record(rec):
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def run_one(email, test_vids=True):
    cmd = [PY, "gsuite_login.py", "--email", email]
    if test_vids:
        cmd.append("--test-vids")
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(APP), capture_output=True, text=True,
                           timeout=PER_ACCOUNT_TIMEOUT, env={**os.environ,
                                                             "DISPLAY": ":99"})
    except subprocess.TimeoutExpired:
        return "error", "timeout", round(time.time() - t0, 1)
    out = (r.stdout or "") + (r.stderr or "")
    state, detail = "error", ""
    for line in out.splitlines():
        for tag, name in (("-> ok:", "ok"), ("-> needs_human:", "needs_human"),
                          ("-> bad_credentials:", "bad_credentials"),
                          ("-> error:", "error"), ("-> dry_run:", "dry_run")):
            if tag in line:
                state, detail = name, line.split(tag, 1)[1].strip()
    return state, detail[:220], round(time.time() - t0, 1)


# ── jalur cadangan Camoufox ─────────────────────────────────────────────────
CAMOUFOX_PY = "/root/harbor/venv/bin/python"      # venv tempat camoufox dipasang
CAMOUFOX_SCRIPT = APP / "camoufox_login.py"


def run_camoufox(email, test_vids=True, timeout=300):
    """Jalankan login lewat Camoufox. -> (state, detail, secs).

    Dipakai saat Chromium kena CAPTCHA/verifikasi: Camoufox (Firefox anti-deteksi)
    terbukti bisa melewati tantangan yang memblokir Chromium.
    """
    if not os.path.exists(CAMOUFOX_PY) or not CAMOUFOX_SCRIPT.exists():
        return "skip", "camoufox tidak tersedia", 0.0
    # Profil Firefox terpisah; dikirim eksplisit supaya tidak ada kebingungan.
    profil_ff = APP / "firefox_profiles" / email
    cmd = [CAMOUFOX_PY, str(CAMOUFOX_SCRIPT), "--email", email,
           "--profile", str(profil_ff)]
    if test_vids:
        cmd.append("--test-vids")
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(APP), capture_output=True, text=True,
                           timeout=timeout,
                           env={**os.environ, "DISPLAY": ":99"})
    except subprocess.TimeoutExpired:
        return "error", "camoufox timeout", round(time.time() - t0, 1)
    out = (r.stdout or "") + (r.stderr or "")
    state, detail = "error", ""
    for line in out.splitlines():
        for tag, name in (("-> ok:", "ok"), ("-> needs_human:", "needs_human"),
                          ("-> bad_credentials:", "bad_credentials"),
                          ("-> error:", "error")):
            if tag in line:
                state, detail = name, line.split(tag, 1)[1].strip()
    if not detail:
        detail = (out.strip().splitlines() or [""])[-1][:180]
    return state, f"camoufox: {detail[:200]}", round(time.time() - t0, 1)


def beban_terlalu_tinggi(ambang=12.0):
    """True bila load average mesin sudah terlalu tinggi untuk lanjut login.

    Login akun membuka browser penuh; menjalankannya saat mesin sudah sesak
    membuat gateway tidak bisa melayani job (pernah terjadi: setor akun timeout).
    """
    try:
        with open("/proc/loadavg") as fh:
            load1 = float(fh.read().split()[0])
        return load1 > ambang
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry", action="store_true",
                    help="coba juga akun yang pernah gagal")
    ap.add_argument("--delay", type=int, default=DELAY)
    ap.add_argument("--limit", type=int, default=0, help="batasi jumlah akun")
    args = ap.parse_args()

    entries, _ = G.load_accounts()
    already = done_ok() if not args.retry else set()

    todo = [e for e in entries if e["email"].lower() not in already]
    if args.limit:
        todo = todo[:args.limit]

    print(f"total akun       : {len(entries)}")
    print(f"sudah berhasil   : {len(already)}")
    print(f"akan diproses    : {len(todo)}")
    print(f"jeda antar akun  : {args.delay}s")
    print(flush=True)

    tally = {"ok": 0, "error": 0, "needs_human": 0, "bad_credentials": 0}
    for i, e in enumerate(todo, 1):
        email = e["email"]

        # Jangan menggilas mesin: kalau beban sudah tinggi, hentikan dengan
        # rapi supaya gateway tetap bisa melayani job pengguna.
        if beban_terlalu_tinggi():
            print("\n!! beban mesin terlalu tinggi — login dihentikan agar "
                  "gateway tetap bisa melayani. Jalankan lagi nanti.", flush=True)
            break

        state, detail, secs = run_one(email)

        # Chromium kena CAPTCHA/verifikasi? Coba Camoufox (Firefox anti-deteksi)
        # sebelum menyerah. Camoufox memakai folder profil TERPISAH
        # (firefox_profiles/) supaya tidak merusak profil Chromium.
        if state == "needs_human":
            cf_state, cf_detail, cf_secs = run_camoufox(email)
            print(f"      [cadangan] {email}: camoufox -> {cf_state} ({cf_secs}s)",
                  flush=True)
            record({"email": email, "state": cf_state, "detail": cf_detail,
                    "secs": cf_secs, "at": datetime.now(timezone.utc).isoformat(),
                    "via": "camoufox"})
            if cf_state == "ok":
                # Chromium gagal, Camoufox berhasil: keadaan akhir = ok.
                state, detail, secs = "ok", cf_detail, secs + cf_secs
            elif cf_state not in ("skip",):
                detail = f"{detail} | {cf_detail}"

        tally[state] = tally.get(state, 0) + 1
        mark = {"ok": "OK ", "needs_human": "VER", "bad_credentials": "BAD",
                "error": "ERR"}.get(state, "???")
        print(f"[{mark}] {i:2d}/{len(todo)} {email:26s} {secs:5.1f}s  {detail[:95]}",
              flush=True)
        record({"email": email, "state": state, "detail": detail, "secs": secs,
                "at": datetime.now(timezone.utc).isoformat()})

        # terlalu banyak permintaan verifikasi = pola diblokir, hentikan
        if tally.get("needs_human", 0) >= 5 and tally["ok"] == 0:
            print("\n!! 5+ akun minta verifikasi tanpa satu pun berhasil — "
                  "pola ini diblokir. Dihentikan.", flush=True)
            break

        if i < len(todo):
            time.sleep(args.delay)

    print(f"\n=== SELESAI: ok={tally.get('ok',0)} "
          f"gagal={tally.get('error',0) + tally.get('bad_credentials',0)} "
          f"verifikasi={tally.get('needs_human',0)} ===", flush=True)


main()