#!/usr/bin/env python3
"""Perbaiki pemeriksa sesi agar mengikuti MESIN yang dipakai.

Bug: `has_login_cookies()` selalu membaca `chrome_profiles/<email>/Default/Cookies`,
padahal saat MESIN=camoufox sesi tersimpan di `firefox_profiles/<email>`
(Firefox: cookies.sqlite, tabel moz_cookies). Akibatnya 65 akun yang SUDAH
login lewat Camoufox ditandai "sesi login tidak ada" lalu dikecualikan
selama 7 hari -> runner tidak punya akun sama sekali.

Perbaikan: kalau MESIN == "camoufox", periksa profil Firefox; selain itu
periksa Chromium seperti semula.

Backup .bak-sesimesin-<ts>.
"""
import shutil
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/vids_runner.py")
src = F.read_text()
ts = int(time.time())
shutil.copy2(F, F.with_suffix(".py.bak-sesimesin-" + str(ts)))
print("backup:", f"{F.name}.bak-sesimesin-{ts}")

NL = chr(10)

LAMA = (
    'def has_login_cookies(email):' + NL +
    '    """True bila profil chrome akun ini punya cookie sesi Google."""' + NL +
    '    import tempfile' + NL +
    '    ck = APP / "chrome_profiles" / email / "Default" / "Cookies"' + NL
)

BARU = (
    'def has_login_cookies(email):' + NL +
    '    """True bila profil MESIN YANG DIPAKAI punya cookie sesi Google.' + NL + NL +
    '    PENTING: lokasi cookie bergantung mesin. Chromium menyimpan di' + NL +
    '    chrome_profiles/<email>/Default/Cookies; Camoufox (Firefox) di' + NL +
    '    firefox_profiles/<email>/cookies.sqlite. Versi lama selalu membaca' + NL +
    '    yang Chromium, sehingga saat MESIN=camoufox SEMUA akun dianggap' + NL +
    '    belum login dan dikecualikan 7 hari.' + NL +
    '    """' + NL +
    '    import tempfile' + NL +
    '    if MESIN == "camoufox":' + NL +
    '        ck = PROFILES_FF / email / "cookies.sqlite"' + NL +
    '        if not ck.exists():' + NL +
    '            return False' + NL +
    '        tmp = None' + NL +
    '        try:' + NL +
    '            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:' + NL +
    '                tmp = tf.name' + NL +
    '            shutil.copy2(ck, tmp)' + NL +
    '            c = sqlite3.connect(tmp)' + NL +
    '            n = c.execute("SELECT COUNT(*) FROM moz_cookies "' + NL +
    '                          "WHERE host LIKE \'%google.com\'").fetchone()[0]' + NL +
    '            c.close()' + NL +
    '            return n > 5' + NL +
    '        except Exception:' + NL +
    '            return False' + NL +
    '        finally:' + NL +
    '            if tmp:' + NL +
    '                try:' + NL +
    '                    Path(tmp).unlink()' + NL +
    '                except Exception:' + NL +
    '                    pass' + NL +
    '    ck = APP / "chrome_profiles" / email / "Default" / "Cookies"' + NL
)

if "PROFILES_FF / email / \"cookies.sqlite\"" in src:
    print("  = perbaikan sudah ada")
elif LAMA in src:
    src = src.replace(LAMA, BARU, 1)
    F.write_text(src)
    print("  + pemeriksa sesi kini mengikuti MESIN")
else:
    print("  !! pola has_login_cookies tidak ketemu")

# pastikan PROFILES_FF terdefinisi
r0 = subprocess.run(["grep", "-n", "PROFILES_FF", str(F)],
                    capture_output=True, text=True)
print("  PROFILES_FF:", r0.stdout.strip().splitlines()[:2])

r = subprocess.run(["/root/geminiomni/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])