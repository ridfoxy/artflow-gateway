#!/usr/bin/env python3
"""PATCH TAHAP 9 — modal promo menghalangi klik kotak prompt.

TEMUAN
------
  ElementHandle.click: Timeout 30000ms exceeded
    ... <button ...> from <div id="modal-..." data-testid="modal-...">
        subtree intercepts pointer events

ChatGPT kadang menampilkan modal (promo/pengumuman) yang menutupi seluruh
halaman. `#prompt-textarea` ADA di DOM tetapi tidak bisa diklik, jadi klik
kotak prompt menggantung 30 detik lalu job gagal — walau akunnya sehat.

Di skrip diagnostik masalah yang sama muncul, dan penyelesaiannya: tekan
Escape lalu buang simpul modal sebelum mengetik.

PERBAIKAN
---------
Tambah _tutup_modal(): tekan Escape, buang elemen [data-testid^='modal-'],
lalu tunggu sebentar. Dipanggil di _satu_job sebelum klik kotak prompt.
"""
import os, shutil, sys, time, py_compile

F = "/root/api-google-flow/gptimage_daemon.py"

LAMA = '''        self.prompt_aktif = prompt
        self.tangkap.clear()
        kotak = await self.pg.query_selector("#prompt-textarea")
        await kotak.click()'''

BARU = '''        self.prompt_aktif = prompt
        self.tangkap.clear()
        await self._tutup_modal()
        kotak = await self.pg.query_selector("#prompt-textarea")
        await kotak.click()'''

# metode baru, disisipkan sebelum _sisipkan_gambar
ANCHOR = "    def _sisipkan_gambar(self, body: dict) -> None:"

METODE = '''    async def _tutup_modal(self) -> None:
        """Buang modal promo yang menghalangi klik kotak prompt.

        Gejalanya: ElementHandle.click timeout 30s dengan pesan
        "from <div id=\\"modal-...\\"> subtree intercepts pointer events".
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

'''


def main():
    if not os.path.exists(F):
        print("❌ berkas tidak ada:", F); return 1
    s = open(F, encoding="utf-8").read()

    if "_tutup_modal" in s:
        print("ℹ️  patch modal sudah terpasang."); return 0
    if LAMA not in s:
        print("❌ pola klik kotak prompt tidak ketemu"); return 1
    if ANCHOR not in s:
        print("❌ titik sisip metode tidak ketemu"); return 1

    cad = f"{F}.bak-modal-{int(time.time())}"
    shutil.copy2(F, cad)
    print("✅ Cadangan:", os.path.basename(cad))

    s = s.replace(ANCHOR, METODE + ANCHOR, 1)
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
        "metode _tutup_modal ada":     "async def _tutup_modal" in v,
        "dipanggil sebelum klik":      "await self._tutup_modal()" in v,
        "Escape + buang simpul modal": "data-testid^='modal-'" in v,
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