#!/usr/bin/env python3
"""PATCH TAHAP 5 — bentuk lampiran gambar yang BENAR (multimodal_text).

TEMUAN (tangkapan UI asli, diag_body_kirim.py)
----------------------------------------------
Ketika UI mengirim gambar, body /backend-api/f/conversation berisi:

  "content": {"content_type": "multimodal_text",
    "parts": [
      {"content_type": "image_asset_pointer",
       "asset_pointer": "sediment://file_000000000f0082078dbd7d9000f2389c",
       "size_bytes": 70, "width": 1, "height": 1},
      "ubah lingkaran jadi biru"
    ]}

Daemon memakai prefix "file-service://" dan tanpa size/width/height.
Akibatnya dict itu TIDAK cocok dengan model ImagePart, validator jatuh ke
varian "str", lalu server menjawab:
  422 {"loc":["messages",0,"content","multimodal_text","parts",0,"str"],
       "msg":"Input should be a valid string"}

Itu sebabnya pesan errornya menunjuk ke "str" — padahal masalahnya bentuk
gambar. (Perhatikan juga: SSE hasil generate memakai "sediment://", yang
konsisten dengan prefix di sini.)
"""
import os, shutil, sys, time, py_compile

F = "/root/api-google-flow/gptimage_daemon.py"

LAMA = '''        bagian = [{"content_type": "image_asset_pointer",
                   "asset_pointer": f"file-service://{fid}"} for fid in gambar]'''

BARU = '''        bagian = [{"content_type": "image_asset_pointer",
                   "asset_pointer": f"sediment://{fid}",
                   "size_bytes": int((self.ukuran_ref or {}).get(fid, (0, 1, 1))[0]),
                   "width": int((self.ukuran_ref or {}).get(fid, (0, 1, 1))[1]),
                   "height": int((self.ukuran_ref or {}).get(fid, (0, 1, 1))[2])}
                  for fid in gambar]'''

# simpan (byte, lebar, tinggi) tiap referensi agar _sisipkan_gambar bisa mengisi
LAMA_SIMPAN = '''                fid = await self.unggah_gambar(g)
                if fid:
                    self.gambar_ref.append(fid)'''
BARU_SIMPAN = '''                fid = await self.unggah_gambar(g)
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

# tambahan: pembaca dimensi tanpa PIL (PNG & JPEG)
LAMA_ATTR = '''        self.gambar_ref = []'''
BARU_ATTR = '''        self.gambar_ref = []
        self.ukuran_ref = {}'''

TAMBAHAN = '''
    @staticmethod
    def _ukuran_gambar(data: bytes) -> tuple[int, int]:
        """Lebar/tinggi tanpa PIL: PNG (IHDR) lalu JPEG (SOF)."""
        try:
            if data[:8] == b"\\x89PNG\\r\\n\\x1a\\n":
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
'''


def main():
    if not os.path.exists(F):
        print("❌ berkas tidak ada:", F); return 1
    s = open(F, encoding="utf-8").read()

    if 'sediment://{fid}' in s:
        print("ℹ️  patch sediment sudah terpasang."); return 0

    for nama, pola in (("blok parts", LAMA), ("simpan referensi", LAMA_SIMPAN),
                       ("attr gambar_ref", LAMA_ATTR)):
        if pola not in s:
            print(f"❌ pola tidak ketemu: {nama}"); return 1

    cad = f"{F}.bak-sediment-{int(time.time())}"
    shutil.copy2(F, cad)
    print("✅ Cadangan:", os.path.basename(cad))

    s = s.replace(LAMA, BARU, 1)
    s = s.replace(LAMA_SIMPAN, BARU_SIMPAN, 1)
    s = s.replace(LAMA_ATTR, BARU_ATTR, 1)
    # sisipkan metode dimensi sebelum _sisipkan_gambar
    anchor = "    def _sisipkan_gambar(self, body: dict) -> None:"
    s = s.replace(anchor, TAMBAHAN + "\n" + anchor, 1)
    open(F, "w", encoding="utf-8").write(s)

    try:
        py_compile.compile(F, doraise=True)
        print("✅ py_compile OK")
    except Exception as e:
        print("❌ sintaks rusak:", e, "→ dikembalikan")
        shutil.copy2(cad, F); return 1

    v = open(F, encoding="utf-8").read()
    cek = {
        "pakai sediment://":          'sediment://{fid}' in v,
        "tidak lagi file-service":    "file-service://" not in v,
        "size_bytes diisi":           '"size_bytes": int(' in v,
        "width/height diisi":         '"width": int(' in v,
        "ukuran_ref disimpan":        "self.ukuran_ref[fid] = " in v,
        "pembaca dimensi ada":        "def _ukuran_gambar" in v,
        "_bahan_gambar tetap dipakai": "self._bahan_gambar(jalur)" in v,
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