#!/usr/bin/env python3
"""B. SOLVER CAPTCHA audio untuk Camoufox — TANPA model lokal.

Cara kerja (sesuai permintaan "tidak memberatkan mesin"):
  1. di halaman reCAPTCHA, klik tombol audio -> dapat URL mp3
  2. unduh mp3 (ffmpeg ambil dari situ juga, tanpa beban)
  3. transkripsi pakai omniRoute (http://127.0.0.1:20128) — layanan yang
     sudah hidup; kalau tidak ada model audio, JANGAN gagal: hasilkan
     berkas audio + beri tahu perlu transkripsi manual
  4. ketik jawaban, submit, tunggu

Dipakai oleh camoufox_login.py sebagai langkah CADANGAN. Kalau gagal dalam
bentuk apa pun, login mengembalikan needs_human (perilaku lama) — jadi tidak
ada risiko regresi.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

KELUAR = Path("/root/api-google-flow/data/captcha_audio")
KELUAR.mkdir(parents=True, exist_ok=True)
OMNI = os.environ.get("OMNI_URL", "http://127.0.0.1:20128")
# nama model STT; kosong = coba beberapa nama umum via omniRoute
STT_MODEL = os.environ.get("STT_MODEL", "")
STT_KANDIDAT = ["whisper-1", "whisper-large-v3", "gpt-4o-mini-transcribe",
                "whisper-large-v3-turbo"]

# Selektor tombol audio reCAPTCHA (beberapa versi)
TOMbol_AUDIO = [
    '#recaptcha-audio-button',
    'button[aria-label*="audio" i]',
    'button[id*="audio"]',
    '#audio-button',
]
INPUT_JAWABAN = ['#audio-response', 'input[id*="audio-response"]',
                 'textarea[id*="audio-response"]']
TOMbol_VERIFIKASI = ['#recaptcha-verify-button', 'button[id*="verify"]']


async def _ada(page, selektor, timeout=3000):
    for s in selektor:
        try:
            el = await page.wait_for_selector(s, state="visible", timeout=timeout)
            if el:
                return s
        except Exception:
            continue
    return None


def _unduh(url, tujuan: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r, open(tujuan, "wb") as f:
            f.write(r.read())
        return tujuan.stat().st_size > 1000
    except Exception:
        return False


def _transkrip(path: Path) -> str:
    """Coba transkripsi lewat omniRoute. Kosong = tidak tersedia."""
    model = STT_MODEL or ""
    kandidat = [model] if model else STT_KANDIDAT
    data = path.read_bytes()
    for m in kandidat:
        try:
            batas = "----b"
            body = (
                f"--{batas}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="a.mp3"\r\n'
                f"Content-Type: audio/mpeg\r\n\r\n"
            ).encode() + data + (
                f"\r\n--{batas}\r\n"
                f'Content-Disposition: form-data; name="model"\r\n\r\n{m}\r\n'
                f"--{batas}--\r\n"
            ).encode()
            req = urllib.request.Request(
                f"{OMNI}/v1/audio/transcriptions", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={batas}",
                         "Authorization": "Bearer local"})
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.loads(r.read())
            t = (j.get("text") or "").strip()
            if t:
                return t
        except Exception:
            continue
    return ""


async def selesaikan(page, log=print, maks_detik=150) -> tuple[bool, str]:
    """Coba selesaikan CAPTCHA audio. -> (berhasil, keterangan)."""
    mulai = time.time()
    # 1. masuk ke mode audio
    sel = await _ada(page, TOMbol_AUDIO, 5000)
    if not sel:
        return False, "tombol audio tidak ada"
    try:
        await page.click(sel)
        log("    [captcha] mode audio dibuka")
    except Exception as e:
        return False, f"klik audio gagal: {str(e)[:60]}"
    await asyncio.sleep(3)

    # 2. cari URL audio
    url = None
    for _ in range(20):
        try:
            url = await page.evaluate(
                "() => {const a=document.querySelector('audio source, audio');"
                " return a ? (a.src || a.getAttribute('src')) : null;}")
        except Exception:
            url = None
        if url and url.startswith("http"):
            break
        await asyncio.sleep(1.5)
    if not url:
        return False, "URL audio tidak ditemukan"

    # 3. unduh
    berkas = KELUAR / f"cap_{int(time.time())}.mp3"
    if not _unduh(url, berkas):
        return False, "unduh audio gagal"
    log(f"    [captcha] audio tersimpan {berkas.name} ({berkas.stat().st_size} B)")

    # 4. transkripsi (tanpa model lokal)
    teks = _transkrip(berkas)
    if not teks:
        return False, f"audio siap untuk transkripsi manual: {berkas}"
    log(f"    [captcha] transkripsi: {teks[:60]}")

    # 5. isi jawaban
    sel_in = await _ada(page, INPUT_JAWABAN, 5000)
    if not sel_in:
        return False, "kolom jawaban tidak ada"
    try:
        await page.fill(sel_in, teks)
        await asyncio.sleep(0.5)
    except Exception as e:
        return False, f"isi jawaban gagal: {str(e)[:60]}"

    # 6. submit
    sel_v = await _ada(page, TOMbol_VERIFIKASI, 4000)
    if sel_v:
        try:
            await page.click(sel_v)
        except Exception:
            pass
    await asyncio.sleep(6)

    # 7. periksa hasil
    try:
        sudah = "accounts.google.com" not in page.url
        if sudah:
            return True, "captcha lolos, keluar dari halaman akun"
        isi = (await page.evaluate(
            "() => (document.body.innerText||'').toLowerCase()"))
        if "coba lagi" in isi or "try again" in isi or "salah" in isi:
            return False, "jawaban captcha ditolak"
        return False, "masih di halaman captcha"
    except Exception as e:
        return False, f"verifikasi gagal: {str(e)[:60]}"