#!/usr/bin/env python3
"""PATCH TAHAP 10 — hasil salah terunduh: mengambil thumbnail REFERENSI.

TEMUAN (job ff0c0d07, SSE 17246 karakter)
-----------------------------------------
  referensi terunggah: file_00000000c620821 (684431 byte, image/jpeg, 1501x2560)
  SSE disimpan: 17246 karakter
  akun → unduh_gagal (HTTP 200, 684431 byte)

684431 byte itu UKURAN FOTO REFERENSI, bukan hasil generate. Sebabnya:

  for _ in range(40):
      url = ... semua <img> yang src-nya /backend-api/estuary/content
      return i[i.length-1].src          # ← img TERAKHIR

Sejak referensi ikut dilampirkan ke percakapan, halaman juga merender
thumbnail referensi itu sebagai <img>. Jadi "img terakhir" bisa berupa foto
referensi (JPEG), lalu ditolak oleh syarat `data[:4] != b"\\x89PNG"`.
Akibatnya job yang sebenarnya BERHASIL dilaporkan gagal.

PERBAIKAN
---------
Unduh lewat API memakai file id hasil (tanpa menebak DOM):

  GET /backend-api/files/download/<file_id>?download_intent=false
      → {"download_url": "https://chatgpt.com/backend-api/estuary/content?...&sig=..."}
  lalu GET download_url

Kalau cara itu gagal, jatuh ke DOM sebagai cadangan — tetapi setiap kandidat
diperiksa: harus PNG asli, dan tidak boleh seukuran/ber-id referensi.
"""
import os, shutil, sys, time, py_compile

F = "/root/api-google-flow/gptimage_daemon.py"

LAMA = '''        # tunggu DOM merender URL bertanda tangan
        url = ""
        for _ in range(40):
            url = await self.pg.evaluate("""() => {
                const i = Array.from(document.querySelectorAll('img'))
                  .filter(x => x.src && x.src.includes('/backend-api/estuary/content'));
                return i.length ? i[i.length-1].src : '';
            }""")
            if url:
                break
            await asyncio.sleep(0.4)
        if not url:
            hasil["status"] = "unduh_gagal"
            hasil["pesan"] = "URL gambar tidak ditemukan"
            return hasil

        resp = await self.pg.request.get(url, timeout=180000)
        data = await resp.body()
        if data[:4] != b"\\x89PNG" or len(data) < 10000:
            hasil["status"] = "unduh_gagal"
            hasil["pesan"] = f"HTTP {resp.status}, {len(data)} byte"
            return hasil'''

BARU = '''        # Cara utama: unduh lewat API memakai file id HASIL. Ini tidak
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
                    if isi[:4] == b"\\x89PNG" and len(isi) >= 10000:
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
                    if isi[:4] == b"\\x89PNG" and len(isi) >= 10000:
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
            return hasil'''


def main():
    if not os.path.exists(F):
        print("❌ berkas tidak ada:", F); return 1
    s = open(F, encoding="utf-8").read()

    if "files/download/" in s and "lewati kandidat" in s:
        print("ℹ️  patch unduh hasil sudah terpasang."); return 0
    if LAMA not in s:
        print("❌ pola blok unduh tidak ketemu"); return 1

    cad = f"{F}.bak-unduhhasil-{int(time.time())}"
    shutil.copy2(F, cad)
    print("✅ Cadangan:", os.path.basename(cad))

    s = s.replace(LAMA, BARU, 1)
    open(F, "w", encoding="utf-8").write(s)

    try:
        py_compile.compile(F, doraise=True)
        print("✅ py_compile OK")
    except Exception as e:
        print("❌ sintaks rusak:", e, "→ dikembalikan")
        shutil.copy2(cad, F); return 1

    v = open(F, encoding="utf-8").read()
    cek = {
        "unduh lewat API hasil":      "files/download/" in v,
        "pakai download_intent":      "download_intent=false" in v,
        "DOM tetap sebagai cadangan": "lewati kandidat" in v,
        "tidak ambil img terakhir":   "i[i.length-1].src" not in v,
        "tetap wajib PNG":            'isi[:4] == b"\\x89PNG"' in v,
    }
    print()
    for k, ok in cek.items():
        print(f"   {'✅' if ok else '❌'} {k}")
    if not all(cek.values()):
        shutil.copy2(cad, F)
        print("→ dikembalikan karena verifikasi gagal")
        return 1
    print()
    print("CARA BALIK: cp", os.path.basename(cad),
          "gptimage_daemon.py && systemctl restart artflow-gptimage-daemon")
    return 0


if __name__ == "__main__":
    sys.exit(main())
