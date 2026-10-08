#!/usr/bin/env python3
"""PATCH TAHAP 4 — ganti cara unggah referensi ke protokol yang BENAR.

TEMUAN (dari tangkapan UI asli, diag_unggah_body.py)
----------------------------------------------------
Daemon selama ini memakai SATU panggilan multipart ke /backend-api/files.
Server menjawab:
    HTTP 422 {"type":"model_attributes_type","loc":["body"],
              "msg":"Input should be a valid dictionary or object ..."}
Endpoint itu TIDAK menerima multipart sama sekali — ia menerima JSON.

UI asli ChatGPT memakai 3 tahap:
  1. POST /backend-api/files                        (application/json)
       body: {"file_name","file_size","use_case":"multimodal","mime_type",
              "entry_surface":"chat_composer","store_in_library":true, ...}
       → 200 {"upload_url": "...oaiusercontent.com/files/<id>/raw?...",
              "file_id": "file_00000000cb0481fa8140b7f310caf57f"}
  2. PUT  <upload_url>                              (bytes mentah, image/png)
       → 201
  3. POST /backend-api/files/process_upload_stream  (application/json)
       body: {"file_id","use_case":"multimodal","file_name","mime_type", ...}
       → 200 (stream: file.processing.completed)

PERBAIKAN
---------
unggah_gambar() diubah mengikuti 3 tahap itu. _bahan_gambar() tetap dipakai
(masih menerima data URL / http URL / path berkas).
"""
import os, shutil, sys, time, py_compile

F = "/root/api-google-flow/gptimage_daemon.py"

LAMA_MULAI = '''    async def unggah_gambar(self, jalur: str) -> str | None:'''
LAMA_AKHIR = '''            log.warning("unggah tidak dapat id: %s", isi[:200])'''

BARU = '''    async def unggah_gambar(self, jalur: str) -> str | None:
        """Unggah gambar referensi ke ChatGPT, kembalikan file id.

        Menerima path berkas, data URL, atau http(s) URL — lihat _bahan_gambar.

        Memakai protokol 3 tahap yang sama dengan UI asli (terbukti dari
        tangkapan jaringan). /backend-api/files TIDAK menerima multipart:
        dulu itu sebabnya server menjawab 422 "model_attributes_type".
        """
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
                url, headers={"Content-Type": mime},
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

            log.info("referensi terunggah: %s (%d byte, %s)",
                     fid[:20], len(data), mime)
            return fid
        except Exception as exc:
            log.warning("unggah gagal: %s", exc)
        return None
'''


def main():
    if not os.path.exists(F):
        print("❌ berkas tidak ada:", F); return 1
    s = open(F, encoding="utf-8").read()

    if "process_upload_stream" in s:
        print("ℹ️  patch unggah JSON sudah terpasang."); return 0

    i = s.find(LAMA_MULAI)
    if i < 0:
        print("❌ tidak menemukan awal unggah_gambar"); return 1
    j = s.find("    # ---------------------------------------------------------------- job", i)
    if j < 0:
        j = s.find(LAMA_AKHIR, i)
        if j < 0:
            print("❌ tidak menemukan akhir unggah_gambar"); return 1
        j = s.find("\n", s.find("\n", j) + 1) + 1
    lama = s[i:j]
    if "multipart=" not in lama:
        print("❌ blok lama tidak seperti yang diharapkan"); return 1

    cad = f"{F}.bak-unggahjson-{int(time.time())}"
    shutil.copy2(F, cad)
    print("✅ Cadangan:", os.path.basename(cad))

    baru = BARU + "\n"
    s = s[:i] + baru + s[j:]
    open(F, "w", encoding="utf-8").write(s)

    try:
        py_compile.compile(F, doraise=True)
        print("✅ py_compile OK")
    except Exception as e:
        print("❌ sintaks rusak:", e, "→ dikembalikan")
        shutil.copy2(cad, F); return 1

    v = open(F, encoding="utf-8").read()
    cek = {
        "tidak ada multipart lagi":      "multipart=" not in v,
        "tahap1 /backend-api/files":     'backend-api/files"' in v,
        "tahap2 PUT upload_url":         "await self.pg.request.put(" in v,
        "tahap3 process_upload_stream":  "process_upload_stream" in v,
        "_bahan_gambar tetap dipakai":   "self._bahan_gambar(jalur)" in v,
        "log sukses ada":                "referensi terunggah:" in v,
    }
    print()
    for k, ok in cek.items():
        print(f"   {'✅' if ok else '❌'} {k}")
    if not all(cek.values()):
        shutil.copy2(cad, F)
        print("→ dikembalikan karena verifikasi gagal")
        return 1
    print()
    print("CARA BALIK: cp", os.path.basename(cad), "gptimage_daemon.py && systemctl restart artflow-gptimage-daemon")
    return 0


if __name__ == "__main__":
    sys.exit(main())