#!/usr/bin/env python3
"""Lanjutkan: sisipkan pembacaan cadangan di harvest_token + pasang pengintai."""
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/vids_runner.py")
src = F.read_text()
ts = int(time.time())
bak = F.with_suffix(".py.bak-tokenff2-" + str(ts))
bak.write_text(src)
print("backup:", bak.name)

NL = chr(10)
D = chr(34)

# ── A. harvest_token: baca page.__calls_event bila window.__calls kosong ──
LAMA = ('            calls = await page.evaluate(' + D
        + '() => JSON.stringify(window.__calls || [])' + D + ')')
BARU = ('            calls = await page.evaluate(' + D
        + '() => JSON.stringify(window.__calls || [])' + D + ')' + NL +
        '            if calls in ("[]", "", "null", None):' + NL +
        '                # Camoufox/Firefox: hook JS tidak jalan. Pakai hasil' + NL +
        '                # pengintai event Playwright (page.__calls_event).' + NL +
        '                try:' + NL +
        '                    _ev = getattr(page, "__calls_event", None) or []' + NL +
        '                    if _ev:' + NL +
        '                        calls = json.dumps(_ev)' + NL +
        '                except Exception:' + NL +
        '                    pass')

n_a = src.count(LAMA)
print("pola window.__calls:", n_a, "x")
if "__calls_event, None" in src or "_ev = getattr(page" in src:
    print("  = cadangan sudah ada")
elif n_a >= 1:
    src = src.replace(LAMA, BARU)
    print("  + cadangan event disisipkan di harvest_token")
else:
    print("  !! pola tidak ketemu")

# ── B. pasang pengintai saat halaman dibuat ──────────────────────────────
LAMA2 = '        await page.add_init_script(HOOK_JS)'
BARU2 = ('        await page.add_init_script(HOOK_JS)' + NL +
         '        pasang_pengintai_token(page)   # Camoufox: hook JS tidak jalan')
n_b = src.count(LAMA2)
print("titik add_init_script:", n_b, "x")
if "pasang_pengintai_token(page)" in src:
    print("  = pemanggilan sudah ada")
elif n_b >= 1:
    src = src.replace(LAMA2, BARU2)
    print(f"  + pengintai dipasang di {n_b} titik")
else:
    print("  !! titik tidak ketemu")

F.write_text(src)
r = subprocess.run(["/root/geminiomni/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])
print("\nverifikasi:")
r2 = subprocess.run(["grep", "-n", "__calls_event\\|pasang_pengintai_token(page)", str(F)],
                    capture_output=True, text=True)
print(r2.stdout)