#!/usr/bin/env python3
"""SEMPURNAKAN SISTEM — 3 pengaman:

PENGAMAN 1 — runner tidak menandai 'sesi mati' kalau login baru saja sukses.
  Masalah nyata: 25 akun asepqds berhasil login (tercatat ok), lalu runner
  Chromium mencoba memakai profil Firefox -> gagal -> menandai no_login,
  sehingga panel menampilkan "akun sehat 4" padahal 25.
  Perbaikan: mark_dead_session() memeriksa data/login_results.jsonl dulu.
  Bila ada catatan 'ok' yang lebih baru dari LOGIN_BUKTI_JAM (bawaan 6 jam),
  akun TIDAK ditandai mati — hanya istirahat singkat (istirahat_menit).

PENGAMAN 2 — batas waktu keras untuk Camoufox (jangan menggantung).
  _login_jalan.py: subprocess.run -> Popen + timeout + kill paksa
  (kill proses + semua anaknya), lalu lanjut ke akun berikutnya.

PENGAMAN 3 — login dipisah dari gateway (systemd unit sendiri).
  Unit baru artflow-login-job.service (Type=oneshot) supaya restart gateway
  TIDAK mematikan login yang sedang berjalan.

ROLLBACK:
  cp vids_runner.py.bak-sempurna-* vids_runner.py
  cp _login_jalan.py.bak-sempurna-* _login_jalan.py
  systemctl disable --now artflow-login-job.service
"""
import ast
import shutil
import subprocess
import time
from pathlib import Path

APP = Path("/root/api-google-flow")
STAMP = int(time.time())

# ══ PENGAMAN 1: vids_runner.py ═══════════════════════════════════════
P1 = APP / "vids_runner.py"
B1 = P1.with_suffix(f".py.bak-sempurna-{STAMP}")
s1 = P1.read_text()
shutil.copy2(P1, B1)
print(f"backup: {B1.name}")


def gagal1(m):
    shutil.copy2(B1, P1)
    print(f"!! {m} — ROLLBACK")
    raise SystemExit(1)


lama1 = '''def mark_dead_session(email, hours=int(os.environ.get("VIDS_DEAD_SESSION_HOURS", "12"))):
    """Tandai akun sesi-mati langsung (7 hari), jangan lewat has_login_cookies.

    Cookie bisa masih ADA tapi tidak sah, sehingga pemeriksaan cookie tidak
    menangkapnya — akibatnya akun berputar (istirahat 900s -> dicoba -> gagal).
    """
    until = int(time.time()) + hours * 3600
    with db() as c:
        c.execute("""INSERT INTO vids_accounts (email, state, note, until, updated_at)
                     VALUES (?, 'no_login', 'sesi mati (perlu login ulang)', ?, ?)
                     ON CONFLICT(email) DO UPDATE SET
                       state='no_login',
                       note='sesi mati — login ulang lewat panel bot untuk memulihkan',
                       until=excluded.until, updated_at=excluded.updated_at""",
                  (email, until, int(time.time())))'''

baru1 = '''# Berapa lama catatan login 'ok' dianggap bukti yang masih berlaku.
LOGIN_BUKTI_JAM = float(os.environ.get("VIDS_LOGIN_BUKTI_JAM", "6"))
# Istirahat singkat bila runner gagal padahal catatan login masih segar.
ISTIRAHAT_MENIT = float(os.environ.get("VIDS_ISTIRAHAT_MENIT", "10"))


def bukti_login_segar(email):
    """True bila akun ini punya catatan login 'ok' yang masih baru.

    Kenapa perlu: akun bisa berhasil login lewat Camoufox (sesi ada di
    firefox_profiles), tetapi runner Chromium tidak bisa membacanya dan
    menandai 'sesi mati'. Akibatnya panel menunjukkan akun sehat jauh lebih
    sedikit daripada yang sebenarnya (pernah terjadi: 4 padahal 25).
    """
    try:
        p = APP / "data" / "login_results.jsonl"
        if not p.exists():
            return False
        terakhir = None
        for baris in p.read_text(errors="replace").splitlines()[-400:]:
            baris = baris.strip()
            if not baris or email not in baris:
                continue
            try:
                d = json.loads(baris)
            except Exception:
                continue
            if (d.get("email") or "").strip().lower() != email.strip().lower():
                continue
            terakhir = d
        if not terakhir or terakhir.get("state") != "ok":
            return False
        ts = terakhir.get("ts")
        if not ts:
            # pakai waktu berkas sebagai perkiraan
            ts = p.stat().st_mtime
        return (time.time() - float(ts)) < LOGIN_BUKTI_JAM * 3600
    except Exception:
        return False


def mark_dead_session(email, hours=int(os.environ.get("VIDS_DEAD_SESSION_HOURS", "12"))):
    """Tandai akun sesi-mati — TAPI hormati bukti login yang masih segar.

    Cookie bisa masih ADA tapi tidak sah, sehingga pemeriksaan cookie tidak
    menangkapnya. Namun sebaliknya juga berbahaya: menandai mati akun yang
    BARU SAJA berhasil login membuat pool tampak kosong.

    Aturan:
      - ada catatan login 'ok' segar  -> istirahat singkat saja (jangan matikan)
      - tidak ada bukti               -> tandai no_login seperti sebelumnya
    """
    if bukti_login_segar(email):
        sementara = int(time.time()) + ISTIRAHAT_MENIT * 60
        with db() as c:
            c.execute("""INSERT INTO vids_accounts (email, state, note, until, updated_at)
                         VALUES (?, 'ui_retry', ?, ?, ?)
                         ON CONFLICT(email) DO UPDATE SET
                           state='ui_retry',
                           note=excluded.note,
                           until=excluded.until, updated_at=excluded.updated_at""",
                      (email,
                       f"istirahat {ISTIRAHAT_MENIT:.0f}m (login baru sukses, "
                       f"browser gagal baca sesi)",
                       sementara, int(time.time())))
        log(f"{email}: login baru sukses -> istirahat "
            f"{ISTIRAHAT_MENIT:.0f}m (TIDAK ditandai mati)")
        return

    until = int(time.time()) + hours * 3600
    with db() as c:
        c.execute("""INSERT INTO vids_accounts (email, state, note, until, updated_at)
                     VALUES (?, 'no_login', 'sesi mati (perlu login ulang)', ?, ?)
                     ON CONFLICT(email) DO UPDATE SET
                       state='no_login',
                       note='sesi mati — login ulang lewat panel bot untuk memulihkan',
                       until=excluded.until, updated_at=excluded.updated_at""",
                  (email, until, int(time.time())))'''

if lama1 not in s1:
    gagal1("anchor mark_dead_session tidak ditemukan")
s1 = s1.replace(lama1, baru1, 1)
print("  + PENGAMAN 1: bukti_login_segar() hormati hasil login")

P1.write_text(s1)
try:
    ast.parse(s1)
    print("  AST vids_runner.py: OK")
except SyntaxError as e:
    gagal1(f"SyntaxError {e.lineno}: {e.msg}")

# ══ PENGAMAN 2: _login_jalan.py — Camoufox timeout keras ═════════════
P2 = APP / "_login_jalan.py"
B2 = P2.with_suffix(f".py.bak-sempurna-{STAMP}")
s2 = P2.read_text()
shutil.copy2(P2, B2)
print(f"\nbackup: {B2.name}")


def gagal2(m):
    shutil.copy2(B2, P2)
    print(f"!! {m} — ROLLBACK")
    raise SystemExit(1)


# tambahkan helper pembunuh proses
anchor2 = "def jalankan_camoufox(email):"
helper2 = '''def bunuh_paksa(proc, nama="proses"):
    """Bunuh proses BESERTA semua anaknya (hindari Camoufox menggantung)."""
    if proc is None:
        return
    try:
        import signal
        import subprocess as _sp
        # bunuh seluruh grup proses
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=8)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        print(f"    !! {nama} dipaksa berhenti (tidak menutup sendiri)", flush=True)
    except Exception:
        pass


'''
if anchor2 not in s2:
    gagal2("anchor jalankan_camoufox tidak ditemukan")
s2 = s2.replace(anchor2, helper2 + anchor2, 1)
print("  + PENGAMAN 2: bunuh_paksa()")

# ganti subprocess.run Camoufox dengan Popen + timeout + kill
lama2 = '''    tulis_live(email, "camoufox (cadangan)", "login lewat Firefox")
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
            print(f"    [{email}] {baris.strip()[:150]}", flush=True)'''
baru2 = '''    tulis_live(email, "camoufox (cadangan)", "login lewat Firefox")
    t0 = time.time()
    batas = t0 + PER_AKUN_TIMEOUT + 60
    # Popen + batas waktu KERAS: Camoufox pernah menggantung sehingga loop
    # login berhenti sebelum akun terakhir selesai.
    try:
        proc = subprocess.Popen(cmd, cwd=str(APP), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                start_new_session=True,
                                env={**os.environ, "DISPLAY": ":99"})
    except Exception as e:
        return "error", f"gagal menjalankan camoufox: {str(e)[:90]}", 0.0

    keluaran = []
    try:
        for baris in iter(proc.stdout.readline, ""):
            baris = baris.rstrip()
            if not baris:
                continue
            keluaran.append(baris)
            print(f"    [{email}] {baris[:150]}", flush=True)
            if time.time() > batas:
                print(f"    [{email}] !! camoufox melewati batas "
                      f"{PER_AKUN_TIMEOUT + 60:.0f}s", flush=True)
                bunuh_paksa(proc, "camoufox")
                break
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(5, batas - time.time()))
            except Exception:
                bunuh_paksa(proc, "camoufox")
    finally:
        try:
            if proc.poll() is None:
                bunuh_paksa(proc, "camoufox")
        except Exception:
            pass
    out = "\\n".join(keluaran)'''
if lama2 not in s2:
    gagal2("anchor camoufox subprocess.run tidak ditemukan")
s2 = s2.replace(lama2, baru2, 1)
print("  ~ Camoufox: Popen + batas waktu keras + kill paksa")

# hapus baris duplikat print keluaran bila masih ada
s2 = s2.replace('''    out = "\\n".join(keluaran)
    for baris in out.splitlines():
        if baris.strip():
            print(f"    [{email}] {baris.strip()[:150]}", flush=True)
''', '''    out = "\\n".join(keluaran)
''', 1)

# jalankan_satu (Chromium) juga pakai bunuh_paksa saat timeout
lama3 = '''    try:
        p.wait(timeout=max(5, batas - time.time()))
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
        state, detail = "error", "timeout"'''
baru3 = '''    try:
        p.wait(timeout=max(5, batas - time.time()))
    except Exception:
        bunuh_paksa(p, "chromium-login")
        state, detail = "error", "timeout"'''
if lama3 in s2:
    s2 = s2.replace(lama3, baru3, 1)
    print("  ~ jalankan_satu: pakai bunuh_paksa saat timeout")

P2.write_text(s2)
try:
    ast.parse(s2)
    print("  AST _login_jalan.py: OK")
except SyntaxError as e:
    gagal2(f"SyntaxError {e.lineno}: {e.msg}")

print("\n=== VERIFIKASI ===")
print(f"  pengaman 1 (runner) : {'bukti_login_segar' in P1.read_text()}")
print(f"  pengaman 2 (camoufox): {'bunuh_paksa' in P2.read_text()}")
print(f"\nrollback:")
print(f"  cp {B1.name} vids_runner.py")
print(f"  cp {B2.name} _login_jalan.py")