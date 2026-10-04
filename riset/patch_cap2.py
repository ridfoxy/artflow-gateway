#!/usr/bin/env python3
"""Sambungkan solver CAPTCHA ke titik needs_human SETELAH EMAIL (dijalankan di VPS baru)."""
import shutil
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/camoufox_login.py")
src = F.read_text()
ts = int(time.time())
shutil.copy2(F, F.with_suffix(".py.bak-cap2-" + str(ts)))
print("backup:", f"{F.name}.bak-cap2-{ts}")

NL = chr(10)
LAMA = (
    '                hay = text_low(page)' + NL +
    '                cap = find(CHALLENGE, hay)' + NL +
    '                if cap:' + NL +
    '                    log(f"  -> needs_human: tantangan setelah email ({cap})")' + NL +
    '                    return 2'
)

BARU = (
    '                hay = text_low(page)' + NL +
    '                cap = find(CHALLENGE, hay)' + NL +
    '                if cap:' + NL +
    '                    # CAPTCHA SETELAH EMAIL: coba audio, lalu LANJUT ke sandi' + NL +
    '                    log(f"  tantangan setelah email ({cap})")' + NL +
    '                    if os.environ.get("CAPTCHA_AUTO", "1") not in ("0", "false", "no"):' + NL +
    '                        try:' + NL +
    '                            sys.path.insert(0, str(APP_DIR))' + NL +
    '                            import captcha_audio' + NL +
    '                            berhasil, ket = asyncio.run(' + NL +
    '                                captcha_audio.selesaikan(page, log=log))' + NL +
    '                            log(f"  hasil captcha: {\'LOLOS\' if berhasil else \'gagal\'} — {ket}")' + NL +
    '                            if berhasil:' + NL +
    '                                time.sleep(5)' + NL +
    '                                hay = text_low(page)' + NL +
    '                                cap = find(CHALLENGE, hay)' + NL +
    '                        except Exception as e:' + NL +
    '                            log(f"  ! captcha audio error: {str(e)[:120]}")' + NL +
    '                    if cap:' + NL +
    '                        log(f"  -> needs_human: tantangan setelah email ({cap})")' + NL +
    '                        return 2'
)

n = src.count(LAMA)
print("pola tantangan-setelah-email:", n, "x")
if n == 1:
    src = src.replace(LAMA, BARU)
    F.write_text(src)
    print("  + solver disambungkan ke titik setelah email")
else:
    print("  !! jumlah tak terduga — tidak diubah")

r = subprocess.run(["/root/harbor/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])