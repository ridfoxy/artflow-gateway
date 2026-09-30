#!/usr/bin/env python3
"""Jalankan login untuk daftar akun tertentu (dipakai endpoint auto-login).

Mengikuti pola login_all.py yang SUDAH TERBUKTI: memanggil CLI
`gsuite_login.py --email <email>` sebagai subprocess (bukan memanggil
login_one() langsung — fungsi itu sinkron, bukan async, dan butuh page
Playwright yang disiapkan).

Pengaman:
  - jeda antar akun (anti-throttle, bawaan 25s)
  - berhenti bila beban mesin > LOGIN_LOAD_MAX (bawaan 12)
  - hasil per akun ditulis ke data/login_results.jsonl (dibaca panel Log Login)

Pakai:
    python3 _login_jalan.py "a@x.com,b@x.com"
    python3 _login_jalan.py --semua
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/root/api-google-flow")
RESULTS = APP / "data" / "login_results.jsonl"
ACCOUNTS = APP / "accounts.txt"
PY = "/root/geminiomni/venv/bin/python"
DELAY = float(os.environ.get("LOGIN_DELAY", "25"))
LOAD_MAX = float(os.environ.get("LOGIN_LOAD_MAX", "12"))
PER_AKUN_TIMEOUT = float(os.environ.get("LOGIN_TIMEOUT", "420"))

TAG_STATE = (("-> ok:", "ok"), ("-> needs_human:", "needs_human"),
             ("-> bad_credentials:", "bad_credentials"),
             ("-> error:", "error"), ("-> dry_run:", "dry_run"))


def catat(email, state, detail="", secs=None):
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "email": email, "state": state, "detail": str(detail)[:220],
            "secs": secs,
            "at": datetime.now(timezone.utc).isoformat(),
            "ts": int(time.time()),
        }) + "\n")


def beban():
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:
        return 0.0


def kredensial():
    out = {}
    if ACCOUNTS.exists():
        for line in ACCOUNTS.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            em, _, pw = line.partition("|")
            if em.strip():
                out[em.strip()] = pw.strip()
    return out


def jalankan_satu(email):
    """Panggil CLI gsuite_login untuk satu akun. Kembalikan (state, detail, detik)."""
    cmd = [PY, "gsuite_login.py", "--email", email, "--test-vids"]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(APP), capture_output=True, text=True,
                           timeout=PER_AKUN_TIMEOUT,
                           env={**os.environ, "DISPLAY": ":99"})
    except subprocess.TimeoutExpired:
        return "error", "timeout", round(time.time() - t0, 1)
    out = (r.stdout or "") + (r.stderr or "")
    state, detail = "error", ""
    for line in out.splitlines():
        for tag, nama in TAG_STATE:
            if tag in line:
                state, detail = nama, line.split(tag, 1)[1].strip()
    if not detail:
        detail = out.strip().splitlines()[-1][:200] if out.strip() else "tanpa keluaran"
    return state, detail, round(time.time() - t0, 1)


def bersihkan_kunci(email):
    """Lepaskan kunci profil Chrome yang tertinggal dari proses mati.

    Tanpa ini, login gagal dengan "Target page, context or browser has been
    closed" karena Chrome menolak membuka profil yang masih terkunci
    (SingletonLock menunjuk PID yang sudah tidak ada).
    """
    prof = APP / "chrome_profiles" / email
    if not prof.exists():
        return
    # bunuh sisa proses chrome yang masih memakai profil ini
    import glob
    for pid_s in os.listdir("/proc"):
        if not pid_s.isdigit():
            continue
        try:
            with open(f"/proc/{pid_s}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace")
        except Exception:
            continue
        if str(prof) in cmd and "chrome" in cmd.lower():
            try:
                os.kill(int(pid_s), 15)
            except Exception:
                pass
    import time as _t
    _t.sleep(0.5)
    for nama in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        f = prof / nama
        try:
            if f.is_symlink() or f.exists():
                f.unlink()
        except Exception:
            pass


def main():
    args = sys.argv[1:]
    if not args:
        print("pemakaian: _login_jalan.py <email,email> | --semua")
        return 2

    # PRASYARAT: Xvfb :99 harus hidup. Tanpa itu Chrome gagal dengan
    # "Missing X server or $DISPLAY" -> login selalu error.
    try:
        import pastikan_xvfb
        if not pastikan_xvfb.pastikan():
            print("!! Xvfb tidak bisa dijalankan — login dibatalkan")
            return 3
        os.environ["DISPLAY"] = ":99"
    except Exception as e:
        print(f"!! gagal menyiapkan Xvfb: {e!r}")
        return 3

    kred = kredensial()
    target = list(kred) if args[0] == "--semua" else [
        e.strip() for e in args[0].split(",") if e.strip()]

    print(f"login untuk {len(target)} akun (kredensial tersedia: {len(kred)})")

    berhasil = gagal = 0
    for i, email in enumerate(target, 1):
        b = beban()
        if b > LOAD_MAX:
            print(f"!! beban {b:.1f} > {LOAD_MAX} — berhenti; sisa "
                  f"{len(target) - i + 1} akun tidak diproses")
            break
        if email not in kred:
            print(f"[{i}/{len(target)}] {email}: TIDAK ADA sandi di accounts.txt")
            catat(email, "error", "sandi tidak ada")
            gagal += 1
            continue

        print(f"[{i}/{len(target)}] login {email} (beban {b:.1f})", flush=True)
        bersihkan_kunci(email)
        state, detail, secs = jalankan_satu(email)
        print(f"    -> {state}: {detail[:90]} ({secs}s)", flush=True)
        catat(email, state, detail, secs)
        if state == "ok":
            berhasil += 1
        else:
            gagal += 1

        if i < len(target):
            time.sleep(DELAY)

    print(f"\n=== hasil: {berhasil} berhasil, {gagal} gagal ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())