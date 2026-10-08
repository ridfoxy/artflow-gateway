#!/usr/bin/env python3
"""PATCH TAHAP 6 — daemon gagal mengunduh referensi http(s) + dekode ulang.

TEMUAN (log daemon saat uji jalur bot)
--------------------------------------
  [WARNING] unduh referensi gagal: No module named 'httpx'
  [INFO] referensi terunggah: 0

Sebab 1: _bahan_gambar memakai `import httpx`, sedangkan venv daemon
(/root/harbor/venv) tidak punya httpx — hanya `requests`. Jadi setiap
referensi berbentuk http(s) URL gagal diunduh, dan daemon melaporkan
"referensi terunggah: 0" tanpa error yang jelas.

Sebab 2: penyimpanan dimensi memanggil ulang _bahan_gambar(g) SETELAH unggah.
Kalau unduhan gagal, dekode kedua ini juga gagal — dan lebih buruk: byte
referensi diproses dua kali (boros). Perbaikan: simpan dimensi di dalam
unggah_gambar, tepat saat byte-nya sudah ada di tangan.
"""
import os, shutil, sys, time, py_compile

F = "/root/api-google-flow/gptimage_daemon.py"

LAMA_HTTX = '''        if s.startswith("http://") or s.startswith("https://"):
            try:
                import httpx as _hx
                ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
                with _hx.Client(timeout=60, follow_redirects=True,
                                headers={"User-Agent": ua}) as cl:
                    resp = cl.get(s)
                if resp.status_code != 200:
                    log.warning("unduh referensi HTTP %s: %s", resp.status_code, s[:80])
                    return None
                ctype = (resp.headers.get("content-type") or "image/png").split(";")[0]
                ext = "png" if "png" in ctype else ("jpg" if "j" in ctype else "png")
                return (f"referensi.{ext}", resp.content, ctype)
            except Exception as exc:
                log.warning("unduh referensi gagal: %s", exc)
                return None'''

BARU_URLLIB = '''        if s.startswith("http://") or s.startswith("https://"):
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
                return None'''

# simpan dimensi di dalam unggah_gambar (byte sudah ada di tangan)
LAMA_SIMPAN = '''            log.info("referensi terunggah: %s (%d byte, %s)",
                     fid[:20], len(data), mime)
            return fid'''

BARU_SIMPAN = '''            # Catat dimensi SEKARANG (byte sudah di tangan). Sebelumnya ini
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
            return fid'''

# kembalikan _satu_job ke bentuk ringkas (tanpa dekode ulang)
LAMA_JOB = '''                fid = await self.unggah_gambar(g)
                if fid:
                    self.gambar_ref.append(fid)
                    uk = self._bahan_gambar(g)
                    if uk:
                        _, data, _ = uk
                        try:
                            w, h = self._ukuran_gambar(data)
                        except Exception:
                            w, h = 1, 1
                        self.ukuran_ref[fid] = (len(data), w, h)'''

BARU_JOB = '''                fid = await self.unggah_gambar(g)
                if fid:
                    self.gambar_ref.append(fid)'''


def main():
    if not os.path.exists(F):
        print("❌ berkas tidak ada:", F); return 1
    s = open(F, encoding="utf-8").read()

    if "import urllib.request as _ur" in s:
        print("ℹ️  patch unduh sudah terpasang."); return 0

    for nama, pola in (("blok httpx", LAMA_HTTX), ("simpan dimensi", LAMA_SIMPAN),
                       ("blok _satu_job", LAMA_JOB)):
        if pola not in s:
            print(f"❌ pola tidak ketemu: {nama}"); return 1

    cad = f"{F}.bak-unduh-{int(time.time())}"
    shutil.copy2(F, cad)
    print("✅ Cadangan:", os.path.basename(cad))

    s = s.replace(LAMA_HTTX, BARU_URLLIB, 1)
    s = s.replace(LAMA_SIMPAN, BARU_SIMPAN, 1)
    s = s.replace(LAMA_JOB, BARU_JOB, 1)
    open(F, "w", encoding="utf-8").write(s)

    try:
        py_compile.compile(F, doraise=True)
        print("✅ py_compile OK")
    except Exception as e:
        print("❌ sintaks rusak:", e, "→ dikembalikan")
        shutil.copy2(cad, F); return 1

    v = open(F, encoding="utf-8").read()
    cek = {
        "pakai urllib (stdlib)":      "import urllib.request as _ur" in v,
        "httpx sudah tidak dipakai":  "import httpx as" not in v,
        "dimensi dicatat saat unggah": "self.ukuran_ref[fid] = (len(data), w, h)" in v,
        "tidak ada dekode ulang":     "uk = self._bahan_gambar(g)" not in v,
        "log sertakan dimensi":       "%dx%d" in v,
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