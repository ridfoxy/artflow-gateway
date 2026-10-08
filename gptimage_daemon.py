#!/usr/bin/env python3
"""DAEMON GPT IMAGE — browser tetap hidup, jadi job berikutnya tidak perlu
membuka browser lagi.

Masalah: tiap job membuka Camoufox dari nol (~13 detik: 3,7s browser + 5,3s
halaman + 5,5s kotak prompt). Generate sendiri hanya ~28 detik, jadi hampir
sepertiga waktu terbuang untuk membuka browser.

Solusi: satu proses daemon memegang browser + halaman + profil akun. Job masuk
lewat UNIX socket, dikerjakan tanpa membuka browser (kecuali akun perlu ganti).

Protokol: JSON per baris (newline-delimited) di /tmp/gptimage.sock
  permintaan: {"aksi":"gambar","prompt":"...","akun":null}
  balasan:    {"status":"ok","berkas":"...","byte":N, ...}

Aksi lain:
  {"aksi":"siapkan"}  → buka browser lebih dulu (warm-up setelah reboot)
  {"aksi":"sehat"}    → ping
  {"aksi":"akun"}     → keadaan akun
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

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
SOCK = "/tmp/gptimage.sock"
LOG = "/var/log/artflow-gptimage-daemon.log"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(LOG), logging.StreamHandler()])
log = logging.getLogger("gptimage-daemon")

sys.path.append("/root/harbor/venv/lib/python3.12/site-packages")

import gptimage_runner as R
from camoufox.async_api import AsyncCamoufox

KELUAR = APP / "output" / "gptimage"
KELUAR.mkdir(parents=True, exist_ok=True)


class Mesin:
    """Pegang browser + halaman; kerjakan satu job pada satu waktu."""

    def __init__(self):
        self.ctx = None
        self.pg = None
        self.akun = None
        self.kunci = asyncio.Lock()
        self.prompt_aktif = None
        self.tangkap = {}
        self.mode = {"on": False}
        self.kuota_cache = {}          # email -> (waktu, sisa)
        self.dipakai = 0
        self.terakhir_pakai = time.time()

    # ---------------------------------------------------------------- browser
    async def _tukar(self, route, request):
        if not (self.mode["on"]
                and request.url.split("?")[0].endswith("/backend-api/f/conversation")):
            await route.continue_()
            return
        try:
            b = json.loads(request.post_data or "{}")
            if not b.get("messages"):
                await route.continue_()
                return
            b["messages"][0]["content"]["parts"] = [self.prompt_aktif]
            self._sisipkan_gambar(b)
            resp = await route.fetch(
                post_data=json.dumps(b, ensure_ascii=False), timeout=300000,
                headers={k: v for k, v in (request.headers or {}).items()
                         if k.lower() != "content-length"})
            teks = await resp.text()
            self.tangkap["status"] = resp.status
            self.tangkap["sse"] = teks
            await route.fulfill(response=resp, body=teks)
        except Exception as exc:
            self.tangkap["error"] = str(exc)[:200]
            try:
                await route.continue_()
            except Exception:
                pass


    @staticmethod
    def _ukuran_gambar(data: bytes) -> tuple[int, int]:
        """Lebar/tinggi tanpa PIL: PNG (IHDR) lalu JPEG (SOF)."""
        try:
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                w = int.from_bytes(data[16:20], "big")
                h = int.from_bytes(data[20:24], "big")
                return (w or 1, h or 1)
        except Exception:
            pass
        try:
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return (w or 1, h or 1)
                if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
        except Exception:
            pass
        return (1, 1)

    async def _tutup_modal(self) -> None:
        """Buang modal promo yang menghalangi klik kotak prompt.

        Gejalanya: ElementHandle.click timeout 30s dengan pesan
        "from <div id=\"modal-...\"> subtree intercepts pointer events".
        Kotak prompt ada, tetapi tidak bisa diklik. Escape + membuang simpul
        modal sudah cukup (cara ini terbukti pada skrip diagnostik).
        """
        try:
            await self.pg.keyboard.press("Escape")
            await asyncio.sleep(0.4)
            await self.pg.evaluate("""() => {
                document.querySelectorAll("[data-testid^='modal-']")
                        .forEach(e => e.remove());
            }""")
            await asyncio.sleep(0.3)
        except Exception as exc:
            log.warning("tutup modal gagal: %s", str(exc)[:120])

    def _sisipkan_gambar(self, body: dict) -> None:
        """Lampirkan foto referensi ke pesan (kalau ada)."""
        gambar = getattr(self, "gambar_ref", None) or []
        if not gambar:
            return
        bagian = [{"content_type": "image_asset_pointer",
                   "asset_pointer": f"sediment://{fid}",
                   "size_bytes": int((self.ukuran_ref or {}).get(fid, (0, 1, 1))[0]),
                   "width": int((self.ukuran_ref or {}).get(fid, (0, 1, 1))[1]),
                   "height": int((self.ukuran_ref or {}).get(fid, (0, 1, 1))[2])}
                  for fid in gambar]
        bagian.append(self.prompt_aktif)
        body["messages"][0]["content"] = {"content_type": "multimodal_text",
                                          "parts": bagian}

    async def buka(self, akun: str):
        """Buka browser untuk satu akun (menutup yang lama bila beda akun)."""
        if self.ctx is not None and self.akun == akun:
            return
        await self.tutup()
        t = time.time()
        profil = R.PROFIL_DIR / f"chatgpt_profil_{akun.split('@')[0]}"
        self.ctx = await AsyncCamoufox(headless=False, persistent_context=True,
                                       user_data_dir=str(profil),
                                       i_know_what_im_doing=True).__aenter__()
        self.pg = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        await self.pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                           timeout=90000)
        await self.pg.wait_for_selector("#prompt-textarea", timeout=30000)
        await self.pg.route("**/backend-api/f/conversation", self._tukar)
        self.mode["on"] = True
        self.akun = akun
        log.info("browser siap untuk %s dalam %.1fs", akun, time.time() - t)

    async def tutup(self):
        self.mode["on"] = False
        if self.ctx is not None:
            try:
                await self.ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self.ctx = None
        self.pg = None
        self.akun = None

    async def sehat(self) -> bool:
        if self.pg is None:
            return False
        try:
            await self.pg.evaluate("() => 1")
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- kuota
    async def _kepala(self):
        r = await self.pg.request.get("https://chatgpt.com/api/auth/session",
                                      timeout=60000)
        ses = await r.json()
        tok = ses.get("accessToken")
        if not tok:
            return None
        return {"Authorization": f"Bearer {tok}",
                "Content-Type": "application/json", "oai-language": "en-US"}

    async def kuota(self, paksa=False):
        """Sisa kuota gambar akun yang sedang terbuka (di-cache 60s)."""
        now = time.time()
        c = self.kuota_cache.get(self.akun)
        if c and not paksa and now - c[0] < 60:
            return c[1]
        kepala = await self._kepala()
        if not kepala:
            return None
        r = await self.pg.request.post(
            "https://chatgpt.com/backend-api/conversation/init", headers=kepala,
            data=json.dumps({"conversation_id": None, "conversation_origin": None,
                             "gizmo_id": None, "requested_default_model": None,
                             "system_hints": None, "timezone": "UTC",
                             "timezone_offset_min": 0}), timeout=60000)
        j = await r.json()
        sisa = next((x.get("remaining") for x in (j.get("limits_progress") or [])
                     if x.get("feature_name") == "image_gen"), None)
        self.kuota_cache[self.akun] = (now, sisa)
        return sisa

    # ---------------------------------------------------------------- unggah
    @staticmethod
    def _bahan_gambar(jalur: str) -> tuple[str, bytes, str] | None:
        """Ubah referensi menjadi (nama, bytes, mime). Terima 3 bentuk.

        Bentuk nyata yang dipakai sistem: DATA URL. Sebelumnya fungsi unggah
        hanya menerima PATH, sehingga referensi berbentuk data URL selalu
        dilewati diam-diam -> "referensi terunggah: 0".
        """
        s = str(jalur or "").strip()
        if not s:
            return None
        # 1) data URL: data:image/png;base64,xxxx
        if s.startswith("data:"):
            m = re.match(r"data:([^;,]+)(;base64)?,(.*)", s, re.S)
            if not m:
                return None
            mime = m.group(1) or "image/png"
            isi = m.group(3) or ""
            try:
                data = (base64.b64decode(isi) if m.group(2)
                        else __import__("urllib.parse", fromlist=["unquote"])
                        .unquote(isi).encode("utf-8", "replace"))
            except Exception as exc:
                log.warning("data URL tidak bisa didekode: %s", exc)
                return None
            ext = "png" if "png" in mime else ("jpg" if "j" in mime else "png")
            return (f"referensi.{ext}", data, mime)
        # 2) http(s) URL: unduh dulu (UA browser, banyak host menolak UA library)
        if s.startswith("http://") or s.startswith("https://"):
            try:
                # urllib dari stdlib: venv daemon TIDAK punya httpx, dan
                # `import httpx` di sini dulu membuat setiap referensi http(s)
                # gagal dengan "No module named 'httpx'" -> "terunggah: 0".
                import urllib.request as _ur
                ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
                req = _ur.Request(s, headers={"User-Agent": ua,
                                              "Accept": "image/*,*/*;q=0.8"})
                with _ur.urlopen(req, timeout=60) as resp:
                    kode = getattr(resp, "status", 200) or 200
                    if kode != 200:
                        log.warning("unduh referensi HTTP %s: %s", kode, s[:80])
                        return None
                    ctype = (resp.headers.get("Content-Type") or "image/png").split(";")[0]
                    isi = resp.read()
                if not isi:
                    log.warning("unduh referensi kosong: %s", s[:80])
                    return None
                ext = "png" if "png" in ctype else ("jpg" if "j" in ctype else "png")
                return (f"referensi.{ext}", isi, ctype)
            except Exception as exc:
                log.warning("unduh referensi gagal: %s", exc)
                return None
        # 3) path berkas lokal
        try:
            p = Path(s)
            if not p.exists():
                log.warning("referensi tidak ada: %s", s[:80])
                return None
            mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            return (p.name, p.read_bytes(), mime)
        except Exception as exc:
            log.warning("baca referensi gagal: %s", exc)
            return None

    async def unggah_gambar(self, jalur: str) -> str | None:
        """Unggah gambar referensi ke ChatGPT, kembalikan file id.

        Menerima path berkas, data URL, atau http(s) URL — lihat _bahan_gambar.

        Memakai protokol 3 tahap yang sama dengan UI asli (terbukti dari
        tangkapan jaringan). /backend-api/files TIDAK menerima multipart:
        dulu itu sebabnya server menjawab 422 "model_attributes_type".

        Kegagalan sesaat nyata terjadi ("APIRequestContext.post: aborted"),
        jadi percobaan diulang sampai 3x sebelum menyerah.
        """
        for _coba in range(3):
            _hasil = await self._unggah_sekali(jalur)
            if _hasil:
                return _hasil
            if _coba < 2:
                log.warning("unggah referensi gagal, coba lagi (%d/3)", _coba + 2)
                await asyncio.sleep(1.0 if _coba == 0 else 3.0)
        return None

    async def _unggah_sekali(self, jalur: str) -> str | None:
        """Satu percobaan unggah (lihat unggah_gambar untuk pengulangannya)."""
        try:
            bahan = self._bahan_gambar(jalur)
            if not bahan:
                return None
            nama, data, mime = bahan
            if not data:
                log.warning("referensi kosong: %s", str(jalur)[:60])
                return None

            kepala = await self._kepala()
            if not kepala:
                log.warning("sesi tidak siap untuk unggah")
                return None
            kepala = dict(kepala)

            # --- Tahap 1: minta izin unggah (JSON) -------------------------
            r1 = await self.pg.request.post(
                "https://chatgpt.com/backend-api/files",
                headers=kepala,
                data=json.dumps({
                    "file_name": nama, "file_size": len(data),
                    "use_case": "multimodal", "timezone_offset_min": 0,
                    "reset_rate_limits": False, "mime_type": mime,
                    "entry_surface": "chat_composer",
                    "selection_method": "file_picker",
                    "client_resolved_mime_type": mime,
                    "mime_resolution_source": "filename_extension",
                    "store_in_library": True,
                    "library_persistence_mode": "opportunistic",
                }), timeout=90000)
            t1 = await r1.text()
            if r1.status != 200:
                log.warning("unggah tahap1 HTTP %s: %s", r1.status, t1[:200])
                return None
            j1 = json.loads(t1)
            url = j1.get("upload_url")
            fid = j1.get("file_id")
            if not (url and fid):
                log.warning("unggah tahap1 tanpa url/id: %s", t1[:200])
                return None

            # --- Tahap 2: kirim bytes mentah ke penyimpanan ----------------
            r2 = await self.pg.request.put(
                url, headers={"Content-Type": mime,
                              "x-ms-blob-type": "BlockBlob"},
                data=data, timeout=180000)
            if r2.status not in (200, 201, 204):
                log.warning("unggah tahap2 HTTP %s: %s",
                            r2.status, str(await r2.text())[:150])
                return None

            # --- Tahap 3: suruh server memproses --------------------------
            r3 = await self.pg.request.post(
                "https://chatgpt.com/backend-api/files/process_upload_stream",
                headers=kepala,
                data=json.dumps({
                    "file_id": fid, "use_case": "multimodal",
                    "index_for_retrieval": False, "file_name": nama,
                    "library_persistence_mode": "opportunistic",
                    "entry_surface": "chat_composer",
                    "metadata": {"store_in_library": True,
                                 "is_temporary_chat": False,
                                 "library_eligibility_reason": "eligible",
                                 "is_project_thread": False},
                    "mime_type": mime,
                }), timeout=120000)
            t3 = await r3.text()
            if r3.status != 200 or "completed" not in t3:
                log.warning("unggah tahap3 HTTP %s: %s", r3.status, t3[:200])
                return None

            # Catat dimensi SEKARANG (byte sudah di tangan). Sebelumnya ini
            # dilakukan di _satu_job dengan memanggil _bahan_gambar dua kali,
            # sehingga referensi http(s) harus diunduh ulang — dan kalau
            # unduhan gagal, dekode kedua ikut gagal.
            try:
                w, h = self._ukuran_gambar(data)
            except Exception:
                w, h = 1, 1
            self.ukuran_ref[fid] = (len(data), w, h)
            log.info("referensi terunggah: %s (%d byte, %s, %dx%d)",
                     fid[:20], len(data), mime, w, h)
            return fid
        except Exception as exc:
            log.warning("unggah gagal: %s", exc)
        return None

    # ---------------------------------------------------------------- job
    def _layak(self, akun: str) -> bool:
        """Akun masih boleh dipakai? (bukan limit, sisa kuota > 0)"""
        if not akun:
            return False
        d = (R._baca_status() or {}).get(akun) or {}
        if d.get("state") == "limit" and (d.get("sampai") or 0) > time.time():
            return False
        if d.get("state") == "kuota_habis":
            return False
        sisa = d.get("sisa_kuota")
        if isinstance(sisa, int) and sisa <= 0:
            return False
        return True

    async def gambar(self, prompt: str, akun: str | None = None,
                     referensi: list[str] | None = None,
                     keluaran: str | None = None) -> dict:
        async with self.kunci:
            self.dipakai += 1
            self.terakhir_pakai = time.time()
            t0 = time.time()
            dipakai: set[str] = set()
            terakhir = {}
            # Dahulukan akun yang browsernya SUDAH terbuka: mengganti akun
            # berarti menutup + membuka browser lagi (~13s terbuang).
            if (not akun) and self.akun and self._layak(self.akun):
                target0 = self.akun
            else:
                target0 = None
            for _ in range(3):
                if akun and akun not in dipakai:
                    target = akun
                elif target0 and target0 not in dipakai and not dipakai:
                    target = target0
                else:
                    target = R._pilih_akun(dipakai)
                if not target:
                    break
                dipakai.add(target)
                try:
                    r = await self._satu_job(prompt, target, referensi, keluaran)
                except Exception as exc:
                    log.exception("job gagal")
                    r = {"akun": target, "status": "gagal", "pesan": str(exc)[:200]}
                terakhir = r
                R._catat_hasil(target, r)
                if r.get("status") == "ok":
                    r["detik"] = round(time.time() - t0, 1)
                    return r
                log.info("akun %s → %s (%s)", target, r.get("status"), r.get("pesan"))
            return terakhir or {"status": "gagal",
                                "pesan": "tidak ada akun GPT Image siap"}

    async def _satu_job(self, prompt: str, akun: str, referensi, keluaran) -> dict:
        hasil = {"akun": akun, "prompt": prompt, "status": "gagal", "pesan": ""}
        await self.buka(akun)

        sisa = await self.kuota()
        hasil["kuota"] = {"sisa": sisa}
        if isinstance(sisa, int) and sisa <= 0:
            hasil["status"] = "kuota_habis"
            hasil["pesan"] = "kuota gambar akun ini habis"
            return hasil

        # foto referensi (kalau ada) diunggah sekali
        self.gambar_ref = []
        self.ukuran_ref = {}
        if referensi:
            for g in referensi[:4]:
                fid = await self.unggah_gambar(g)
                if fid:
                    self.gambar_ref.append(fid)
            log.info("referensi terunggah: %d", len(self.gambar_ref))
            if not self.gambar_ref:
                # JANGAN lanjut: prompt bergambar yang dikirim tanpa lampiran
                # diperlakukan sebagai permintaan teks, dan gagalnya muncul
                # sebagai "tidak ada respons dari server" / "SSE tidak memuat
                # gambar" — pesan yang menyesatkan. Lebih baik berhenti jujur.
                hasil["pesan"] = ("referensi gagal diunggah "
                                  f"({len(referensi[:4])} foto) — coba lagi")
                return hasil

        # bersihkan kompositor bila ada sisa teks
        try:
            await self.pg.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass

        self.prompt_aktif = prompt
        self.tangkap.clear()
        await self._tutup_modal()
        kotak = await self.pg.query_selector("#prompt-textarea")
        await kotak.click()
        await asyncio.sleep(0.3)
        await self.pg.keyboard.type("x", delay=15)
        await asyncio.sleep(0.3)
        await self.pg.keyboard.press("Enter")

        # Batas tunggu SSE. Dulu di-hardcode 150 x 0.4s = 60 detik, padahal
        # unggah referensi (~15s) + generate gambar saja sudah sering lewat:
        # job berhasil terukur 62s dan 125s, jadi 60s memotong job yang
        # sebenarnya sedang berjalan -> "tidak ada respons dari server".
        # Bot sendiri menunggu sampai config.POLL_MAX_SECONDS (700s).
        try:
            _tunggu = float(os.environ.get("GPTIMAGE_SSE_WAIT_SECONDS", "240"))
        except Exception:
            _tunggu = 240.0
        _batas = time.time() + _tunggu
        while time.time() < _batas:
            await asyncio.sleep(0.4)
            if self.tangkap.get("sse") or self.tangkap.get("error"):
                break
        if self.tangkap.get("error"):
            hasil["pesan"] = f"route.fetch: {self.tangkap['error']}"
            return hasil
        if not self.tangkap.get("sse"):
            hasil["pesan"] = "tidak ada respons dari server"
            hasil["status"] = "timeout"
            return hasil

        sse = self.tangkap["sse"]
        rendah = sse.lower()

        # ── DIAGNOSIS: simpan SSE mentah bila diminta (GPTIMAGE_DUMP_SSE=1) ──
        if os.environ.get("GPTIMAGE_DUMP_SSE", "0") == "1":
            try:
                from pathlib import Path as _P
                _d = _P("/root/api-google-flow/data")
                _d.mkdir(parents=True, exist_ok=True)
                (_d / "sse_terakhir.txt").write_text(
                    f"# akun={getattr(self,'akun',None)} "
                    f"ref={getattr(self,'gambar_ref',None)} "
                    f"status={self.tangkap.get('status')} "
                    f"panjang={len(sse)}\n" + sse, encoding="utf-8")
                log.info("SSE disimpan: %d karakter", len(sse))
            except Exception as _e:
                log.warning("gagal simpan SSE: %s", _e)
        if "unusual activity" in rendah:
            hasil["status"] = "diblokir"
            hasil["pesan"] = "bot detection (unusual activity)"
            return hasil
        for t in R.PENANDA_LIMIT:
            if t in rendah:
                hasil["status"] = "limit"
                hasil["pesan"] = "throttle jangka pendek (~1 jam)"
                return hasil
        if self.tangkap.get("status") != 200:
            hasil["status"] = "gagal"
            hasil["pesan"] = f"HTTP {self.tangkap.get('status')}: {sse[:150]}"
            return hasil

        ptr = re.search(r'"asset_pointer":"sediment://(file_[0-9a-f]+)"', sse)
        idfile = ptr.group(1) if ptr else None
        if not idfile:
            semua = list(dict.fromkeys(re.findall(r"file_[0-9a-f]{20,}", sse)))
            idfile = semua[0] if semua else None
        # jangan tertukar dengan file id hasil unggahan referensi
        if idfile and idfile in (self.gambar_ref or []):
            semua = [x for x in dict.fromkeys(re.findall(r"file_[0-9a-f]{20,}", sse))
                     if x not in (self.gambar_ref or [])]
            idfile = semua[0] if semua else None
        if not idfile:
            hasil["status"] = "tanpa_gambar"
            hasil["pesan"] = "SSE tidak memuat gambar"
            return hasil

        dim = re.search(r'"width":(\d+),"height":(\d+)', sse)
        if dim:
            hasil["ukuran"] = f"{dim.group(2)}x{dim.group(1)}"

        # Cara utama: unduh lewat API memakai file id HASIL. Ini tidak
        # menebak-nebak: sejak referensi ikut dilampirkan, halaman juga
        # merender thumbnail referensi sebagai <img>, dan mengambil "img
        # terakhir" dulu mengambil foto referensi (JPEG) -> job yang sudah
        # berhasil dilaporkan gagal "HTTP 200, 684431 byte".
        data = None
        kepala = await self._kepala() or {}
        try:
            r = await self.pg.request.get(
                "https://chatgpt.com/backend-api/files/download/"
                f"{idfile}?download_intent=false",
                headers=kepala, timeout=60000)
            if r.status == 200:
                j = await r.json()
                durl = j.get("download_url") or ""
                if durl:
                    r2 = await self.pg.request.get(durl, timeout=180000)
                    isi = await r2.body()
                    if isi[:4] == b"\x89PNG" and len(isi) >= 10000:
                        data = isi
                    else:
                        log.warning("unduh api: HTTP %s, %d byte, bukan PNG",
                                    r2.status, len(isi))
            else:
                log.warning("unduh api: HTTP %s", r.status)
        except Exception as exc:
            log.warning("unduh api gagal: %s", str(exc)[:120])

        # Cadangan: DOM. Periksa setiap kandidat dari yang terbaru, dan
        # lewati apa pun yang bukan PNG hasil generate.
        if data is None:
            for _ in range(40):
                kandidat = await self.pg.evaluate("""() => {
                    const i = Array.from(document.querySelectorAll('img'))
                      .filter(x => x.src && x.src.includes('/backend-api/estuary/content'));
                    return i.map(x => x.src);
                }""") or []
                for url in reversed(kandidat):
                    try:
                        resp = await self.pg.request.get(url, timeout=180000)
                        isi = await resp.body()
                    except Exception as exc:
                        log.warning("unduh dom gagal: %s", str(exc)[:100])
                        continue
                    if isi[:4] == b"\x89PNG" and len(isi) >= 10000:
                        data = isi
                        break
                    log.info("lewati kandidat %d byte (bukan PNG hasil)",
                             len(isi))
                if data is not None:
                    break
                await asyncio.sleep(0.4)

        if data is None:
            hasil["status"] = "unduh_gagal"
            hasil["pesan"] = "hasil PNG tidak ditemukan"
            return hasil

        kel = Path(keluaran) if keluaran else (
            KELUAR / f"gptimage_{int(time.time()*1000)}_{random.randint(100,999)}.png")
        kel.parent.mkdir(parents=True, exist_ok=True)
        kel.write_bytes(data)
        hasil.update({"status": "ok", "berkas": str(kel), "byte": len(data)})
        return hasil


# ---------------------------------------------------------------- server
MESIN: Mesin | None = None


async def tangani(permintaan: dict) -> dict:
    global MESIN
    aksi = permintaan.get("aksi")
    if aksi == "sehat":
        hidup = MESIN is not None and await MESIN.sehat()
        return {"sehat": hidup, "akun": (MESIN.akun if MESIN else None),
                "dipakai": (MESIN.dipakai if MESIN else 0)}
    if aksi == "akun":
        R._baca_status()
        return {"status": "ok", "data": R.status_akun()}
    if aksi == "siapkan":
        if MESIN is None:
            MESIN = Mesin()
        akun = permintaan.get("akun") or R._pilih_akun()
        if not akun:
            return {"status": "gagal", "pesan": "tidak ada akun"}
        await MESIN.buka(akun)
        return {"status": "ok", "akun": akun}
    if aksi == "gambar":
        if MESIN is None:
            MESIN = Mesin()
        return await MESIN.gambar(permintaan.get("prompt") or "",
                                  permintaan.get("akun"),
                                  permintaan.get("referensi"),
                                  permintaan.get("keluaran"))
    return {"status": "gagal", "pesan": f"aksi tidak dikenal: {aksi}"}


async def layani(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        baris = await asyncio.wait_for(reader.readline(), timeout=10)
        if not baris:
            return
        permintaan = json.loads(baris.decode())
        balasan = await tangani(permintaan)
        writer.write((json.dumps(balasan, ensure_ascii=False) + "\n").encode())
        await writer.drain()
    except Exception as exc:
        try:
            writer.write((json.dumps({"status": "gagal",
                                      "pesan": str(exc)[:200]}) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


IDLE_TUTUP = float(os.environ.get("GPTIMAGE_IDLE_TUTUP", "300"))


async def penjaga_menganggur():
    """Tutup browser bila tidak ada job selama IDLE_TUTUP detik.

    Beban mesin turun saat menganggur; job berikutnya membuka lagi (~9s).
    Tanpa ini, browser menahan memori + CPU selamanya.
    """
    while True:
        await asyncio.sleep(30)
        try:
            if MESIN is None or MESIN.ctx is None:
                continue
            diam = time.time() - MESIN.terakhir_pakai
            if diam >= IDLE_TUTUP:
                log.info("menganggur %.0fs — menutup browser (hemat beban)", diam)
                async with MESIN.kunci:
                    await MESIN.tutup()
        except Exception as exc:
            log.warning("penjaga menganggur error: %s", exc)


async def utama():
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    server = await asyncio.start_unix_server(layani, path=SOCK)
    os.chmod(SOCK, 0o600)
    log.info("daemon siap di %s", SOCK)

    # hangatkan browser lebih dulu (agar job pertama tidak menanggung buka browser)
    try:
        global MESIN
        MESIN = Mesin()
        akun = R._pilih_akun()
        if akun:
            await MESIN.buka(akun)
            log.info("warm-up selesai: %s", akun)
    except Exception as exc:
        log.warning("warm-up gagal (akan dicoba saat job pertama): %s", exc)

    asyncio.create_task(penjaga_menganggur())

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(utama())
    except KeyboardInterrupt:
        pass