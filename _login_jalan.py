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
DELAY = float(os.environ.get("LOGIN_DELAY", "0"))       # langsung lanjut
# Batas per akun: login sukses ~183s (termasuk verifikasi Vids), sedangkan
# CAPTCHA terdeteksi ~7-9s. 420s hanya membuat akun yang menggantung memakan
# 7 menit sebelum lanjut — itu sumber "jeda 6 menit" yang dirasakan user.
PER_AKUN_TIMEOUT = float(os.environ.get("LOGIN_TIMEOUT", "180"))
# Jalur cadangan Camoufox: terbukti lolos CAPTCHA pada akun yang Chromium-nya
# kena tantangan (uji: drkvpj@gumdel.com).
CAMOUFOX_PY = os.environ.get("CAMOUFOX_PY", "/root/harbor/venv/bin/python")
CAMOUFOX_SCRIPT = os.environ.get("CAMOUFOX_SCRIPT", "camoufox_login.py")
PAKAI_CAMOUFOX = os.environ.get("LOGIN_CAMOUFOX", "1") not in ("0", "false", "no")
LOAD_MAX = float(os.environ.get("LOGIN_LOAD_MAX", "12"))

TAG_STATE = (("-> ok:", "ok"), ("-> needs_human:", "needs_human"),
             ("-> bad_credentials:", "bad_credentials"),
             ("-> error:", "error"), ("-> dry_run:", "dry_run"))


# Berkas status hidup: panel memakai ini untuk menampilkan
# "akun X sedang <tahap>" tanpa menunggu akun selesai.
LIVE = APP / "data" / "login_live.json"

# Frasa pada keluaran gsuite_login -> nama tahap yang mudah dibaca.
TAHAP_POLA = (
    ("membuka browser", ("membuka", "launch", "browser dibuka", "profil")),
    ("mengisi email", ("email:", "isi email", "mengisi email")),
    ("mengirim email", ("tombol berikutnya", "berikutnya", "klik email")),
    ("mengisi sandi", ("sandi", "password")),
    ("mengirim sandi", ("terkirim", "sesudah sandi", "klik sandi")),
    ("halaman persyaratan", ("persyaratan", "saya mengerti", "terms", "speedbump")),
    ("menunggu halaman akun", ("menunggu", "tunggu")),
    ("cek akses Vids", ("vids",)),
    ("selesai", ("ok:", "berhasil")),
)


def tahap_dari(teks):
    """Tebak tahap dari satu baris keluaran gsuite_login."""
    t = (teks or "").lower()
    for nama, kunci in TAHAP_POLA:
        if any(k in t for k in kunci):
            return nama
    return None


def tulis_live(email, tahap, info="", state=None, selesai=False):
    """Catat status terkini satu akun (untuk panel)."""
    try:
        data = {}
        if LIVE.exists():
            try:
                data = json.loads(LIVE.read_text())
            except Exception:
                data = {}
        akun = data.get("akun") or {}
        akun[email] = {
            "tahap": tahap, "info": str(info)[:120], "state": state,
            "selesai": bool(selesai), "at": int(time.time()),
        }
        data["akun"] = akun
        data["diperbarui"] = int(time.time())
        tmp = LIVE.with_suffix(".json.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(LIVE)
    except Exception:
        pass


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
    """Panggil CLI gsuite_login untuk satu akun — keluaran dibaca BARIS DEMI BARIS.

    Tiap baris yang bermakna dicatat sebagai tahap (membuka browser, mengisi
    email, mengirim sandi, ...) supaya operator bisa melihat akun itu sedang
    di langkah mana. Kembalikan (state, detail, detik).
    """
    cmd = [PY, "gsuite_login.py", "--email", email, "--test-vids"]
    t0 = time.time()
    state, detail = "error", ""
    tahap_terakhir = "mulai"
    try:
        p = subprocess.Popen(cmd, cwd=str(APP), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             env={**os.environ, "DISPLAY": ":99"},
                             bufsize=1)
    except Exception as e:
        return "error", f"gagal menjalankan: {str(e)[:100]}", 0.0

    batas = t0 + PER_AKUN_TIMEOUT
    for baris in iter(p.stdout.readline, ""):
        baris = baris.rstrip()
        if not baris:
            continue
        # tampilkan juga ke log berkas, dengan awalan akun
        print(f"    [{email}] {baris[:150]}", flush=True)
        # tahap mudah dibaca
        th = tahap_dari(baris)
        if th and th != tahap_terakhir:
            tahap_terakhir = th
            tulis_live(email, th, baris[:100])
        # state akhir
        for tag, nama in TAG_STATE:
            if tag in baris:
                state, detail = nama, baris.split(tag, 1)[1].strip()
        if time.time() > batas:
            print(f"    [{email}] !! melewati batas {PER_AKUN_TIMEOUT:.0f}s", flush=True)
            break
    try:
        p.wait(timeout=max(5, batas - time.time()))
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
        state, detail = "error", "timeout"
    if not detail:
        detail = "selesai tanpa keterangan"
    tulis_live(email, state, detail[:100], state=state, selesai=True)
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


def jalankan_camoufox(email):
    """Coba login akun yang kena CAPTCHA memakai Camoufox.

    Terbukti dari uji: akun yang Chromium-nya kena CAPTCHA berhasil login
    lewat Camoufox (halaman login bersih, sandi terkirim, landing "Selamat
    datang"). Keluarannya sengaja meniru gsuite_login.py supaya bisa dibaca
    fungsi yang sama.
    """
    skrip = APP / CAMOUFOX_SCRIPT
    if not skrip.exists():
        return None, "camoufox_login.py tidak ada"
    py = CAMOUFOX_PY if Path(CAMOUFOX_PY).exists() else PY
    cmd = [py, str(skrip), "--email", email, "--test-vids"]
    tulis_live(email, "camoufox (cadangan)", "login lewat Firefox")
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(APP), capture_output=True, text=True,
                           timeout=PER_AKUN_TIMEOUT + 60,
                           env={**os.environ, "DISPLAY": ":99"})
    except subprocess.TimeoutExpired:
        return "error", "camoufox timeout", round(time.time() - t0, 1)
    out = (r.stdout or "") + (r.stderr or "")
    for baris in out.splitlines():
        if baris.strip():
            print(f"    [{email}] {baris.strip()[:150]}", flush=True)
    state, detail = "error", ""
    for line in out.splitlines():
        for tag, nama in TAG_STATE:
            if tag in line:
                state, detail = nama, line.split(tag, 1)[1].strip()
    if not detail:
        detail = out.strip().splitlines()[-1][:200] if out.strip() else "tanpa keluaran"
    return state, detail, round(time.time() - t0, 1)


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

    print(f"login untuk {len(target)} akun (kredensial tersedia: {len(kred)})",
          flush=True)
    print(f"jeda antar akun: {DELAY}s | batas per akun: {PER_AKUN_TIMEOUT:.0f}s | "
          f"beban maks: {LOAD_MAX}", flush=True)
    # kosongkan status hidup supaya panel mulai dari nol
    try:
        LIVE.parent.mkdir(parents=True, exist_ok=True)
        LIVE.write_text(json.dumps({"akun": {}, "mulai": int(time.time()),
                                    "total": len(target)}))
    except Exception:
        pass

    berhasil = gagal = 0
    t_mulai = time.time()
    for i, email in enumerate(target, 1):
        b = beban()
        if b > LOAD_MAX:
            print(f"!! beban {b:.1f} > {LOAD_MAX} — berhenti; sisa "
                  f"{len(target) - i + 1} akun tidak diproses")
            break
        if email not in kred:
            print(f"[{i}/{len(target)}] {email}: TIDAK ADA sandi di accounts.txt")
            catat(email, "error", "sandi tidak ada di accounts.txt")
            tulis_live(email, "dilewati", "sandi tidak ada di accounts.txt",
                       state="error", selesai=True)
            gagal += 1
            continue

        print(f"\n[{i}/{len(target)}] {email} — mulai (beban {b:.1f})", flush=True)
        tulis_live(email, "mulai", "menyiapkan browser")
        bersihkan_kunci(email)
        state, detail, secs = jalankan_satu(email)
        print(f"    -> {state}: {detail[:110]} ({secs}s)", flush=True)
        print(f"       progres: {i}/{len(target)} | berhasil {berhasil} | "
              f"gagal {gagal} | berjalan {time.time() - t_mulai:.0f}s", flush=True)

        # FALLBACK CAMOUFOX: Chromium kena CAPTCHA -> coba Camoufox sekali.
        # Jangan menunggu timeout panjang; CAPTCHA sudah jelas masalahnya.
        if state == "needs_human" and PAKAI_CAMOUFOX:
            print("    -> CAPTCHA: mencoba jalur cadangan Camoufox...", flush=True)
            st2, d2, s2 = jalankan_camoufox(email)
            print(f"    -> camoufox {st2}: {str(d2)[:80]} ({s2}s)", flush=True)
            if st2 == "ok":
                state, detail, secs = "ok", f"camoufox; {d2}", s2
            else:
                detail = f"{detail} | camoufox: {st2} {str(d2)[:80]}"

        catat(email, state, detail, secs)
        if state == "ok":
            berhasil += 1
        else:
            gagal += 1

        if i < len(target):
            time.sleep(DELAY)

    print(f"\n=== hasil: {berhasil} berhasil, {gagal} gagal "
          f"(total {time.time() - t_mulai:.0f}s) ===", flush=True)
    try:
        data = json.loads(LIVE.read_text()) if LIVE.exists() else {}
        data["selesai"] = int(time.time())
        data["berhasil"] = berhasil
        data["gagal"] = gagal
        LIVE.write_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())