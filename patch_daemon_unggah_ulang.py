#!/usr/bin/env python3
"""PATCH TAHAP 8 — unggahan referensi gagal, job tetap lanjut (diam-diam).

TEMUAN (log daemon)
-------------------
  [WARNING] unggah gagal: APIRequestContext.post: aborted
  [INFO] referensi terunggah: 0
  ... job lanjut ...
  → "tidak ada respons dari server" / "SSE tidak memuat gambar"

Dua masalah:
 1. Unggahan bisa gagal SESAAT ("aborted"), dan tidak pernah dicoba lagi.
 2. Kalau unggahan gagal, job TETAP dilanjutkan tanpa gambar. Prompt lalu
    diperlakukan sebagai permintaan teks, ChatGPT menjawab teks, dan job
    gagal dengan pesan yang menyesatkan ("tidak ada respons dari server")
    padahal sebabnya referensi tidak pernah masuk.

Ini pola yang sama seperti bug `images` di bot: menerima data lalu membuangnya
tanpa suara. Kalau user mengirim referensi, job WAJIB memakai referensi —
atau berhenti dengan pesan yang jujur.

PERBAIKAN
---------
 a) unggah_gambar: coba ulang sampai 3x dengan jeda (1s, 3s) untuk kegagalan
    sesaat maupun HTTP non-200.
 b) _satu_job: kalau ada referensi tapi tidak satu pun terunggah, hentikan job
    dengan pesan jelas, jangan kirim prompt tanpa gambar.
"""
import os, shutil, sys, time, py_compile

F = "/root/api-google-flow/gptimage_daemon.py"

# ── a) ulangi unggahan ──────────────────────────────────────────────────
LAMA_FUNGSI = '''    async def unggah_gambar(self, jalur: str) -> str | None:
        """Unggah gambar referensi ke ChatGPT, kembalikan file id.

        Menerima path berkas, data URL, atau http(s) URL — lihat _bahan_gambar.

        Memakai protokol 3 tahap yang sama dengan UI asli (terbukti dari
        tangkapan jaringan). /backend-api/files TIDAK menerima multipart:
        dulu itu sebabnya server menjawab 422 "model_attributes_type".
        """
        try:'''

BARU_FUNGSI = '''    async def unggah_gambar(self, jalur: str) -> str | None:
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
        try:'''

# ── b) jangan lanjut tanpa referensi ────────────────────────────────────
LAMA_JOB = '''        if referensi:
            for g in referensi[:4]:
                fid = await self.unggah_gambar(g)
                if fid:
                    self.gambar_ref.append(fid)
            log.info("referensi terunggah: %d", len(self.gambar_ref))'''

BARU_JOB = '''        if referensi:
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
                return hasil'''


def main():
    if not os.path.exists(F):
        print("❌ berkas tidak ada:", F); return 1
    s = open(F, encoding="utf-8").read()

    if "_unggah_sekali" in s:
        print("ℹ️  patch pengulangan unggah sudah terpasang."); return 0
    for nama, pola in (("fungsi unggah", LAMA_FUNGSI), ("blok _satu_job", LAMA_JOB)):
        if pola not in s:
            print(f"❌ pola tidak ketemu: {nama}"); return 1

    cad = f"{F}.bak-unggahulang-{int(time.time())}"
    shutil.copy2(F, cad)
    print("✅ Cadangan:", os.path.basename(cad))

    s = s.replace(LAMA_FUNGSI, BARU_FUNGSI, 1)
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
        "pengulangan 3x ada":         "for _coba in range(3):" in v,
        "fungsi sekali-jalan ada":    "async def _unggah_sekali" in v,
        "berhenti bila 0 terunggah":  "referensi gagal diunggah" in v,
        "pesan lama tidak dipakai":   'hasil["pesan"] = "tidak ada respons dari server"' in v,
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
