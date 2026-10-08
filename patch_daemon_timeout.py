#!/usr/bin/env python3
"""PATCH TAHAP 7 — batas tunggu SSE terlalu pendek (60 detik).

TEMUAN
------
Job gagal "tidak ada respons dari server" padahal akun sehat: log menunjukkan
`referensi terunggah: file_...820 (214082 byte, image/jpeg, 853x1280)` tepat
sebelumnya. Yang salah bukan akun, melainkan batas tunggunya:

    for _ in range(150):
        await asyncio.sleep(0.4)     # 150 x 0.4s = 60 detik

Padahal satu job = unggah referensi (~15s) + generate gambar (~15-60s).
Pengukuran nyata: 62s (job berhasil) sampai 125s (job berhasil). Jadi 60s
memotong job yang sebenarnya sedang berjalan.

Bot menunggu sampai config.POLL_MAX_SECONDS = 700s, jadi daemon-lah yang
memotong lebih dulu.

PERBAIKAN
---------
Batas tunggu dibuat dari konstanta yang bisa diatur (default 240s) dan
di-reset tiap job supaya job ke-N tidak mewarisi sisa waktu job sebelumnya.
Begitu SSE tiba, tidak ada tambahan waktu tunggu.
"""
import os, shutil, sys, time, py_compile

F = "/root/api-google-flow/gptimage_daemon.py"

LAMA = '''        for _ in range(150):
            await asyncio.sleep(0.4)
            if self.tangkap.get("sse") or self.tangkap.get("error"):
                break'''

BARU = '''        # Batas tunggu SSE. Dulu di-hardcode 150 x 0.4s = 60 detik, padahal
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
                break'''


def main():
    if not os.path.exists(F):
        print("❌ berkas tidak ada:", F); return 1
    s = open(F, encoding="utf-8").read()

    if "GPTIMAGE_SSE_WAIT_SECONDS" in s:
        print("ℹ️  patch batas tunggu sudah terpasang."); return 0
    if LAMA not in s:
        print("❌ pola tidak ketemu"); return 1

    cad = f"{F}.bak-ssewait-{int(time.time())}"
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
        "tidak ada lagi range(150)":   "for _ in range(150)" not in v,
        "batas bisa diatur":           "GPTIMAGE_SSE_WAIT_SECONDS" in v,
        "default 240 detik":           '"240"' in v,
        "berhenti saat SSE tiba":      'if self.tangkap.get("sse") or self.tangkap.get("error")' in v,
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
