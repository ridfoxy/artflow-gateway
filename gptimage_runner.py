#!/usr/bin/env python3
"""GPT IMAGE RUNNER — mesin gambar ketiga (setelah Omni & Nano Banana).

Menghasilkan gambar dari ChatGPT (GPT Image) memakai akun pool, TANPA API key
OpenAI. Teknik terbukti:

  1. Buka profil Camoufox akun (sesi ChatGPT tersimpan di chatgpt_profil_<user>).
  2. Baca kuota lewat RPC murni: conversation/init → limits_progress.image_gen.
     Kalau remaining <= 0 → akun dilewati (tidak dipakai).
  3. Pasang route interception pada /backend-api/f/conversation.
  4. Ketik 1 karakter ke #prompt-textarea + Enter → halaman membuat token
     sentinel yang SAH.
  5. Di tengah jalan, body request DITUKAR dengan prompt kita
     (route.fetch(post_data=body_baru) → route.fulfill).
  6. Baca SSE → dapat file id gambar → unduh lewat estuary/content bertanda tangan.

Penting (jangan diubah tanpa uji):
  - Jangan pakai route.abort(): 200 tapi percakapan tak tersimpan → unduh 403/404.
  - Jangan bikin body sendiri: 500 Internal Server Error (field kurang).
  - route.fetch wajib timeout besar (generate 30-60s), default hanya 30s.
  - Token sentinel sekali pakai, jadi harus dibuat di kiriman yang sama.

API publik modul ini:
    jalankan(prompt, akun=None, keluaran=None, timeout=300) -> dict
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

logger = logging.getLogger("gptimage")

APP = Path(__file__).resolve().parent
PROFIL_DIR = Path("/root")
KELUARAN_DIR = APP / "output" / "gptimage"
AKUN_PATH = APP / "accounts.txt"
STATUS_PATH = APP / "data" / "gptimage_status.json"

# venv tempat Camoufox/Playwright terpasang
VENV_SITE = "/root/harbor/venv/lib/python3.12/site-packages"
if VENV_SITE not in sys.path:
    sys.path.append(VENV_SITE)

PENANDA_LIMIT = ("batas penggunaan", "coba lagi sekitar", "sedang terkena batas",
                 "reached your limit", "try again later")
PENANDA_BLOKIR = ("unusual activity",)
PENANDA_TIDAK_DIDUKUNG = ("tidak dapat membuat gambar", "can't generate images",
                          "unable to generate images")


# --------------------------------------------------------------------------
# Akun
# --------------------------------------------------------------------------

def _baca_akun() -> list[str]:
    """Email pool yang punya profil ChatGPT."""
    hasil = []
    for b in AKUN_PATH.read_text(errors="replace").splitlines():
        s = b.strip()
        if s.startswith("#") or "|" not in s:
            continue
        email = s.split("|", 1)[0].strip().lower()
        if not email:
            continue
        profil = PROFIL_DIR / f"chatgpt_profil_{email.split('@')[0]}"
        if profil.exists():
            hasil.append(email)
    return hasil


def _baca_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text())
    except Exception:
        return {}


def _tulis_status(data: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def status_akun() -> dict:
    """Ringkasan status tiap akun GPT Image (untuk panel bot)."""
    st = _baca_status()
    akun = _baca_akun()
    keluar = []
    for em in akun:
        d = st.get(em) or {}
        sampai = d.get("sampai") or 0
        pulih = max(0, int((sampai - time.time()) / 60)) if sampai else 0
        keluar.append({
            "email": em,
            "state": d.get("state", "siap"),
            # None aman untuk panel: pakai -1 sebagai "belum diketahui"
            "sisa_kuota": d.get("sisa_kuota") if isinstance(
                d.get("sisa_kuota"), int) else -1,
            "reset": d.get("reset") or "",
            "dipakai": d.get("dipakai", 0),
            "pesan": d.get("pesan", ""),
            "diperiksa": d.get("diperiksa") or "",
            "pulih_dalam_menit": pulih,
        })
    return {"total": len(akun), "akun": keluar}


def _pilih_akun(lewati: set[str] | None = None) -> str | None:
    """Pilih akun: yang sisa kuotanya paling besar & belum diblokir jangka pendek."""
    lewati = lewati or set()
    st = _baca_status()
    layak = []
    now = time.time()
    for em in _baca_akun():
        if em in lewati:
            continue
        d = st.get(em) or {}
        if d.get("state") == "limit":
            # throttle jangka pendek ~1 jam
            sampai = d.get("sampai") or 0
            if sampai > now:
                continue
        sisa = d.get("sisa_kuota")
        if isinstance(sisa, int) and sisa <= 0:
            continue
        layak.append((sisa if isinstance(sisa, int) else 999, em))
    if not layak:
        return None
    layak.sort(reverse=True)
    return layak[0][1]


# --------------------------------------------------------------------------
# Inti: satu job GPT Image
# --------------------------------------------------------------------------

async def _jalan_async(prompt: str, akun: str, keluaran: Path,
                       timeout: int = 300) -> dict:
    nama = akun.split("@")[0]
    profil = PROFIL_DIR / f"chatgpt_profil_{nama}"
    hasil = {"akun": akun, "prompt": prompt, "status": "gagal",
             "pesan": "", "berkas": None, "kuota": None}
    t0 = time.time()

    from camoufox.async_api import AsyncCamoufox

    tangkap: dict = {}
    mode = {"tukar": False}

    async def tukar(route, request):
        if not (mode["tukar"]
                and request.url.split("?")[0].endswith("/backend-api/f/conversation")):
            await route.continue_()
            return
        try:
            b = json.loads(request.post_data or "{}")
            if not b.get("messages"):
                await route.continue_()
                return
            b["messages"][0]["content"]["parts"] = [prompt]
            body = json.dumps(b, ensure_ascii=False)
            kepala = {k: v for k, v in (request.headers or {}).items()
                      if k.lower() != "content-length"}
            resp = await route.fetch(post_data=body, headers=kepala,
                                     timeout=timeout * 1000)
            teks = await resp.text()
            tangkap["status"] = resp.status
            tangkap["sse"] = teks
            await route.fulfill(response=resp, body=teks)
        except Exception as exc:  # jangan pernah menggantung job
            tangkap["error"] = str(exc)[:200]
            try:
                await route.continue_()
            except Exception:
                pass

    async with AsyncCamoufox(headless=False, persistent_context=True,
                             user_data_dir=str(profil),
                             i_know_what_im_doing=True) as ctx:
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                      timeout=90000)
        await asyncio.sleep(9)

        # -------- kuota dulu (RPC murni, tanpa UI) --------
        try:
            r = await pg.request.get("https://chatgpt.com/api/auth/session",
                                     timeout=60000)
            ses = await r.json()
            tok = ses.get("accessToken")
            if not tok:
                hasil["status"] = "no_login"
                hasil["pesan"] = "sesi ChatGPT tidak ada di profil ini"
                return hasil
            kepala = {"Authorization": f"Bearer {tok}",
                      "Content-Type": "application/json", "oai-language": "en-US"}
            r = await pg.request.post(
                "https://chatgpt.com/backend-api/conversation/init", headers=kepala,
                data=json.dumps({"conversation_id": None, "conversation_origin": None,
                                 "gizmo_id": None, "requested_default_model": None,
                                 "system_hints": None, "timezone": "UTC",
                                 "timezone_offset_min": 0}), timeout=60000)
            ij = await r.json()
            for x in ij.get("limits_progress") or []:
                if x.get("feature_name") == "image_gen":
                    hasil["kuota"] = {"sisa": x.get("remaining"),
                                      "reset": x.get("reset_after")}
            sisa = (hasil["kuota"] or {}).get("sisa")
            if isinstance(sisa, int) and sisa <= 0:
                hasil["status"] = "kuota_habis"
                hasil["pesan"] = f"kuota gambar habis (reset {hasil['kuota'].get('reset')})"
                return hasil
        except Exception as exc:
            logger.warning("baca kuota gagal (lanjut saja): %s", exc)

        # -------- pasang route, aktifkan mode tukar --------
        await pg.route("**/backend-api/f/conversation", tukar)
        mode["tukar"] = True

        # Kotak prompt: ChatGPT mengganti markup (dulu #prompt-textarea,
        # sekarang ProseMirror contenteditable). Selector lama tetap
        # didahulukan; SATU panggilan dengan daftar dipisah koma supaya semua
        # kandidat ditunggu BERBARENGAN (kalau dicoba berurutan, kandidat
        # pertama menghabiskan seluruh anggaran waktu).
        SELECTOR_KOTAK = (
            "#prompt-textarea, "
            "div.ProseMirror[contenteditable='true'], "
            "div[contenteditable='true'], "
            "form div[contenteditable='true']"
        )

        async def _ambil_kotak(ms):
            try:
                h = await pg.wait_for_selector(SELECTOR_KOTAK, timeout=ms,
                                               state="visible")
                return h
            except Exception:
                return None

        kotak = await _ambil_kotak(30000)
        if kotak is None:
            # kesempatan kedua: composer kadang belum selesai merender
            logger.warning("kotak prompt belum ada — muat ulang sekali")
            try:
                await pg.reload(wait_until="domcontentloaded", timeout=90000)
            except Exception as exc:
                logger.warning("muat ulang gagal: %s", str(exc)[:90])
            kotak = await _ambil_kotak(30000)
        if kotak is None:
            # catat kandidat DOM yang ada, supaya penyebabnya terlihat
            try:
                dom = await pg.evaluate("""() => ({
                    promptTextarea: document.querySelectorAll('#prompt-textarea').length,
                    proseMirror: document.querySelectorAll('.ProseMirror').length,
                    editables: document.querySelectorAll('[contenteditable="true"]').length,
                    textareas: document.querySelectorAll('textarea').length,
                    url: location.href.slice(0, 80),
                    judul: (document.title || '').slice(0, 50),
                })""")
                logger.warning("kotak prompt TIDAK muncul; keadaan DOM: %s", dom)
            except Exception:
                pass
            hasil["status"] = "ui_tidak_siap"
            hasil["pesan"] = "kotak prompt tidak muncul"
            return hasil

        # pemicu seminim mungkin — isinya akan ditukar
        await kotak.click()
        await asyncio.sleep(0.6)
        await pg.keyboard.type("x", delay=20)
        await asyncio.sleep(0.6)
        await pg.keyboard.press("Enter")

        for _ in range(max(10, timeout // 5)):
            await asyncio.sleep(3)
            if tangkap.get("sse") or tangkap.get("error"):
                break
        if tangkap.get("error"):
            hasil["status"] = "gagal"
            hasil["pesan"] = f"route.fetch: {tangkap['error']}"
            return hasil
        if not tangkap.get("sse"):
            hasil["status"] = "timeout"
            hasil["pesan"] = "tidak ada respons dari server"
            return hasil

        sse = tangkap["sse"]
        kode = tangkap.get("status")
        rendah = sse.lower()
        for t in PENANDA_BLOKIR:
            if t in rendah:
                hasil["status"] = "diblokir"
                hasil["pesan"] = "bot detection (unusual activity)"
                return hasil
        for t in PENANDA_LIMIT:
            if t in rendah:
                hasil["status"] = "limit"
                hasil["pesan"] = "throttle jangka pendek (~1 jam)"
                return hasil
        if kode != 200:
            hasil["status"] = "gagal"
            hasil["pesan"] = f"HTTP {kode}: {sse[:180]}"
            return hasil

        # -------- ambil gambar --------
        pointer = re.search(r'"asset_pointer":"sediment://(file_[0-9a-f]+)"', sse)
        idfile = pointer.group(1) if pointer else None
        if not idfile:
            semua = list(dict.fromkeys(re.findall(r"file_[0-9a-f]{20,}", sse)))
            idfile = semua[0] if semua else None
        if not idfile:
            hasil["status"] = "tanpa_gambar"
            hasil["pesan"] = "SSE tidak memuat gambar (mungkin dijawab teks)"
            return hasil

        dim = re.search(r'"width":(\d+),"height":(\d+)', sse)
        if dim:
            hasil["ukuran"] = f"{dim.group(2)}x{dim.group(1)}"

        # halaman merender <img> dengan URL bertanda tangan
        url_gbr = ""
        for _ in range(20):
            await asyncio.sleep(3)
            url_gbr = await pg.evaluate("""() => {
                const i = Array.from(document.querySelectorAll('img'))
                  .filter(x => x.src && x.src.includes('/backend-api/estuary/content'));
                return i.length ? i[i.length-1].src : '';
            }""")
            if url_gbr:
                break
        if not url_gbr:
            cid = re.findall(r'"conversation_id":"([0-9a-f-]{30,})"', sse)
            cid = cid[0] if cid else ""
            r = await pg.evaluate("""async ([fid, cid]) => {
                const u = "/backend-api/files/download/" + fid +
                          "?conversation_id=" + cid + "&download_intent=true";
                const r = await fetch(u, {credentials:"include"});
                return {status:r.status, isi:(await r.text()).slice(0,900)};
            }""", [idfile, cid])
            m = re.search(r'https://chatgpt\.com/backend-api/estuary/content[^"\\ ]+',
                          r.get("isi", ""))
            if m:
                url_gbr = m.group(0).replace("\\u0026", "&")
        if not url_gbr:
            hasil["status"] = "unduh_gagal"
            hasil["pesan"] = "URL gambar tidak ditemukan"
            return hasil

        resp = await pg.request.get(url_gbr, timeout=180000)
        data = await resp.body()
        if data[:4] != b"\x89PNG" or len(data) < 10000:
            hasil["status"] = "unduh_gagal"
            hasil["pesan"] = f"HTTP {resp.status}, {len(data)} byte, magic {data[:4]}"
            return hasil

        keluaran.parent.mkdir(parents=True, exist_ok=True)
        keluaran.write_bytes(data)
        hasil.update({"status": "ok", "berkas": str(keluaran),
                      "byte": len(data), "detik": round(time.time() - t0, 1)})
        return hasil


def _catat_hasil(target: str, r: dict) -> None:
    """Simpan keadaan akun setelah satu percobaan (dipakai runner + daemon)."""
    st = _baca_status()
    d = st.get(target) or {}
    d["diperiksa"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if r.get("kuota"):
        d["sisa_kuota"] = r["kuota"].get("sisa")
        d["reset"] = r["kuota"].get("reset")
    if r.get("status") == "ok":
        d["state"] = "siap"
        d["pesan"] = ""
        d["dipakai"] = d.get("dipakai", 0) + 1
        if isinstance(d.get("sisa_kuota"), int):
            d["sisa_kuota"] = max(0, d["sisa_kuota"] - 1)
    elif r.get("status") == "limit":
        d["state"] = "limit"
        d["sampai"] = time.time() + 3600      # throttle ~1 jam, bukan bulanan
        d["pesan"] = r.get("pesan", "")
    elif r.get("status") == "kuota_habis":
        d["state"] = "kuota_habis"
        d["sisa_kuota"] = 0
        d["pesan"] = r.get("pesan", "")
    else:
        d["pesan"] = r.get("pesan", "")
    st[target] = d
    _tulis_status(st)



# ───────────────────────────────────────────────────────────────
# Jalur CEPAT: daemon memegang browser yang sudah hidup
# ───────────────────────────────────────────────────────────────

SOCK_DAEMON = "/tmp/gptimage.sock"


def _lewat_daemon(prompt: str, akun, keluaran, timeout: int = 300,
                  referensi: list[str] | None = None) -> dict | None:
    """Kirim job ke daemon lewat UNIX socket. None bila daemon tidak ada/gagal.

    Daemon menghilangkan ~13 detik buka browser: halaman sudah terbuka dan
    profil akun sudah dimuat, jadi job berikutnya langsung mengirim prompt.
    """
    import socket as _s
    if not os.path.exists(SOCK_DAEMON):
        return None
    try:
        s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_DAEMON)
        permintaan = {"aksi": "gambar", "prompt": prompt}
        if akun:
            permintaan["akun"] = akun
        if keluaran:
            permintaan["keluaran"] = str(keluaran)
        # Tanpa ini, foto referensi tidak pernah sampai ke ChatGPT
        # (gejala: "SSE tidak memuat gambar" untuk SEMUA job bergambar).
        if referensi:
            permintaan["referensi"] = [str(x) for x in referensi if x][:4]
        s.sendall((json.dumps(permintaan, ensure_ascii=False) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            potong = s.recv(65536)
            if not potong:
                break
            buf += potong
        s.close()
        if not buf:
            return None
        return json.loads(buf.decode().splitlines()[0])
    except Exception as exc:
        logger.warning("daemon tidak terpakai (%s) — jatuh ke jalur biasa", exc)
        return None


def jalankan(prompt: str, akun: str | None = None, keluaran: str | Path | None = None,
             timeout: int = 300, referensi: list[str] | None = None) -> dict:
    """Jalankan satu job GPT Image. Blokir sampai selesai (dipakai runner).

    Mengembalikan dict: {status: ok|limit|kuota_habis|no_login|timeout|gagal|...,
                         berkas, byte, kuota, akun, pesan}
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"status": "gagal", "pesan": "prompt kosong"}

    # jalur cepat dulu: daemon dengan browser yang sudah hidup
    lewat = _lewat_daemon(prompt, akun, keluaran, timeout, referensi)
    if lewat is not None and lewat.get("status"):
        return lewat

    dipakai: set[str] = set()
    terakhir: dict = {}
    for _ in range(4):
        target = akun if (akun and not dipakai) else _pilih_akun(dipakai)
        if not target:
            break
        dipakai.add(target)
        kel = Path(keluaran) if keluaran else (
            KELUARAN_DIR / f"gptimage_{int(time.time()*1000)}_{random.randint(100,999)}.png")
        try:
            r = asyncio.run(_jalan_async(prompt, target, kel, timeout))
        except Exception as exc:
            r = {"akun": target, "status": "gagal", "pesan": str(exc)[:200]}
        terakhir = r

        _catat_hasil(target, r)
        if r["status"] == "ok":
            return r
        logger.info("akun %s → %s (%s)", target, r["status"], r.get("pesan", ""))
        # status yang tidak layak dicoba ulang di akun berikutnya? justru itu
        # tujuannya: lempar ke akun sehat, sama seperti Vids.
    return terakhir or {"status": "gagal", "pesan": "tidak ada akun GPT Image siap"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = sys.argv[1] if len(sys.argv) > 1 else "kucing lucu di taman"
    a = sys.argv[2] if len(sys.argv) > 2 else None
    out = jalankan(p, a)
    print(json.dumps(out, indent=1, ensure_ascii=False))