#!/usr/bin/env python3
"""Token-hook alternatif untuk CAMOUFOX: ambil dari event Playwright.

MASALAH: hook JS (`window.fetch` + XHR) bekerja di Chromium tetapi TIDAK
menangkap apa pun di Camoufox (Firefox) — `window.__calls` tetap 0 walau
panggilan `appsgenaiserver-pa.clients6.google.com/v1/genai/quotaSummary`
benar-benar terjadi (terbukti 13x lewat event Playwright).

AKIBAT: `harvest_token()` selalu gagal -> runner menandai "UI tidak siap" ->
job tetap `queued` -> tidak ada akun yang bisa dipakai.

SOLUSI: dengarkan event `request` Playwright (bekerja di mesin apa pun) dan
simpan token ke `page.__calls_event`. `harvest_token()` membacanya sebagai
sumber cadangan bila `window.__calls` kosong.

Semua dibungkus try: kalau gagal, perilaku lama tetap berlaku.
Backup .bak-tokenff-<ts>.
"""
import shutil
import subprocess
import time
from pathlib import Path

F = Path("/root/api-google-flow/vids_runner.py")
src = F.read_text()
ts = int(time.time())
shutil.copy2(F, F.with_suffix(".py.bak-tokenff-" + str(ts)))
print("backup:", f"{F.name}.bak-tokenff-{ts}")

NL = chr(10)
D = chr(34)   # tanda kutip ganda

# ── 1. fungsi pemasang pengintai event ────────────────────────────────
FUNGSI = '''

def pasang_pengintai_token(page):
    """Simpan header panggilan appsgenaiserver dari event Playwright.

    Dipakai saat hook JS tidak jalan (Camoufox/Firefox). Hasil disimpan di
    `page.__calls_event` dalam bentuk yang SAMA dengan `window.__calls`
    (kunci: url, method, headers, body) supaya harvest_token bisa membacanya.

    -> True bila pengintai terpasang.
    """
    try:
        page.__calls_event = []
    except Exception:
        return False
    try:
        def _dengar(req):
            try:
                if "appsgenaiserver" not in req.url:
                    return
                hd = {str(k).lower(): str(v) for k, v in (req.headers or {}).items()}
                page.__calls_event.append({
                    "how": "event",
                    "url": req.url,
                    "method": req.method,
                    "headers": hd,
                    "body": req.post_data or "",
                })
            except Exception:
                pass
        page.on("request", _dengar)
        return True
    except Exception:
        return False

'''

anchor = "\nasync def harvest_token("
if "def pasang_pengintai_token" in src:
    print("  = fungsi pengintai sudah ada")
elif anchor in src:
    src = src.replace(anchor, FUNGSI + anchor, 1)
    print("  + fungsi pasang_pengintai_token ditambahkan")
else:
    print("  !! anchor harvest_token tidak ketemu")

# ── 2. harvest_token: baca cadangan event bila __calls kosong ─────────
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

if '__calls_event' in src:
    print("  = cadangan event sudah ada di harvest_token")
elif src.count(LAMA) >= 1:
    src = src.replace(LAMA, BARU)
    print(f"  + cadangan event dipasang ({src.count('__calls_event')} rujukan)")
else:
    print("  !! pembacaan window.__calls tidak ketemu")

# ── 3. pasang pengintai saat halaman dibuat ───────────────────────────
LAMA2 = '        await page.add_init_script(HOOK_JS)'
BARU2 = ('        await page.add_init_script(HOOK_JS)' + NL +
         '        pasang_pengintai_token(page)   # Camoufox: hook JS tidak jalan')
if "pasang_pengintai_token(page)" in src:
    print("  = pemanggilan pengintai sudah ada")
elif src.count(LAMA2) >= 1:
    src = src.replace(LAMA2, BARU2)
    print(f"  + pengintai dipasang di {src.count('pasang_pengintai_token(page)')} titik halaman")
else:
    print("  !! titik add_init_script tidak ketemu")

F.write_text(src)
r = subprocess.run(["/root/geminiomni/venv/bin/python", "-m", "py_compile", str(F)],
                   capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr[-400:])