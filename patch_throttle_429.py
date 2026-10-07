#!/usr/bin/env python3
"""
PATCH: 429/throttle = ISTIRAHAT PENDEK + JOB PINDAH AKUN (pilihan A).

MASALAH YANG DIPERBAIKI
-----------------------
Di jalur paralel, 429 dengan body KOSONG tidak tertangkap:
  - cek `kuota_habis(text)` gagal (text kosong)
  - cek `st in (400,401,403)` gagal
  - jatuh ke `failed`  → job MATI, akun TIDAK ditandai
  → akun sama dipakai lagi → 429 lagi (amat: celsqc berulang)

PERBAIKAN
---------
1. Tambah `mark_throttled()` — parkir pendek (bawaan 900s), BUKAN 31 hari.
2. 3 titik produsen emisi status "throttled" (bukan "failed"/"limited").
3. Konsumen menangani "throttled": parkir akun + job dikembalikan ke antrean
   (tanpa menambah hitungan percobaan) + pindah akun.

CATATAN PENTING
---------------
`limited` (31 hari) TETAP ADA dan tidak diubah — itu untuk kuota BULANAN
yang benar-benar habis. Throttle sesaat tidak boleh memakai jalur itu,
karena akan membuang akun sehat selama 31 hari.

Semua reversibel: cadangan .bak-<tag>-<epoch> + auto-revert kalau sintaks rusak.
"""
import os, sys, time, shutil, py_compile

F = "/root/api-google-flow/vids_runner.py"
TAG = "throttle"

# ─── 1. konstanta cooldown throttle ──────────────────────────────────────
KONST_LAMA = 'ACCOUNT_COOLDOWN_LIMIT = int(os.environ.get("VIDS_LIMIT_COOLDOWN", "2678400"))'
KONST_BARU = '''ACCOUNT_COOLDOWN_LIMIT = int(os.environ.get("VIDS_LIMIT_COOLDOWN", "2678400"))
# THROTTLE (429 sesaat, kuota BELUM bulanan habis) = istirahat PENDEK saja.
# Dipilih pemilik sistem: akun sehat tidak boleh dibuang 31 hari gara-gara
# throttle sesaat. Job dipindahkan ke akun lain, akun ini kembali otomatis.
THROTTLE_COOLDOWN = int(os.environ.get("VIDS_THROTTLE_COOLDOWN", "900"))'''

# ─── 2. metode mark_throttled ────────────────────────────────────────────
ANCHOR_METHOD = '''        log(f"akun {email} UI tidak siap — istirahat {seconds}s")'''
METHOD_BARU = ANCHOR_METHOD + '''

    def mark_throttled(self, email, seconds=None):
        """Akun kena 429 SESaat (throttle) — parkir pendek, JANGAN buang.

        Beda dengan mark_limited (31 hari, kuota bulanan benar-benar habis):
        throttle hilang sendiri dalam hitungan menit, jadi akun ini kembali
        dipakai otomatis setelah cooldown. Job-nya dipindah ke akun lain.
        """
        seconds = THROTTLE_COOLDOWN if seconds is None else seconds
        until = int(time.time()) + seconds
        with db() as c:
            c.execute("""INSERT INTO vids_accounts (email, state, note, until, updated_at)
                         VALUES (?, 'throttled', 'throttle 429 — istirahat sementara', ?, ?)
                         ON CONFLICT(email) DO UPDATE SET
                           state='throttled',
                           note='throttle 429 — istirahat sementara',
                           until=excluded.until, updated_at=excluded.updated_at""",
                      (email, until, int(time.time())))
        log(f"akun {email} THROTTLE 429 — istirahat {seconds}s, job pindah akun")'''

# ─── 3. produsen: 3 titik ────────────────────────────────────────────────
P1_LAMA = '''                        if kuota_habis(text):
                            hasil[info["job_id"]] = (
                                "limited", "kuota akun habis (429) — pindah akun")
                        else:
                            hasil[info["job_id"]] = ("failed", "stream gagal/kecil")'''
P1_BARU = '''                        if kuota_habis(text):
                            # 429 = throttle sesaat → akun diparkir pendek,
                            # job JANGAN dimatikan.
                            hasil[info["job_id"]] = (
                                "throttled", "throttle 429 — pindah akun")
                        else:
                            hasil[info["job_id"]] = ("failed", "stream gagal/kecil")'''

P2_LAMA = '''                    hasil[info["job_id"]] = ("failed", f"status={st} {(text or '')[:110]}")'''
P2_BARU = '''                    if st == 429:
                        # BUG LAMA: 429 dengan body kosong jatuh ke sini dan
                        # mematikan job tanpa menandai akun. Sekarang throttle.
                        hasil[info["job_id"]] = (
                            "throttled", "throttle 429 — pindah akun")
                    else:
                        hasil[info["job_id"]] = ("failed", f"status={st} {(text or '')[:110]}")'''

P3_LAMA = '''                            if kuota_habis(text) or st == 429:
                                hasil[info["job_id"]] = (
                                    "limited",
                                    "kuota akun habis (429) — pindah akun")'''
P3_BARU = '''                            if kuota_habis(text) or st == 429:
                                hasil[info["job_id"]] = (
                                    "throttled",
                                    "throttle 429 — pindah akun")'''

# ─── 4. konsumen ─────────────────────────────────────────────────────────
K1_LAMA = '''                if status == "limited":
                    pool.mark_limited(email)
                    limit_hit = True
                    _requeue(jid, "akun limit", hitung=False)
                    continue'''
K1_BARU = '''                if status == "throttled":
                    # Throttle sesaat: parkir pendek + job pindah akun.
                    # TIDAK memakai mark_limited (31 hari) supaya akun sehat
                    # tidak terbuang.
                    pool.mark_throttled(email)
                    limit_hit = True          # tutup sesi → akun lain dipakai
                    _requeue(jid, "throttle 429 (pindah akun)", hitung=False)
                    continue

                if status == "limited":
                    pool.mark_limited(email)
                    limit_hit = True
                    _requeue(jid, "akun limit", hitung=False)
                    continue'''

# konsumen jalur UI (st2/st3)
K2_LAMA = '''                    elif st2 == "limited":
                        pool.mark_limited(email)'''
K2_BARU = '''                    elif st2 == "throttled":
                        pool.mark_throttled(email)
                        limit_hit = True
                        _requeue(jid, "throttle 429 (jalur UI)", hitung=False)
                    elif st2 == "limited":
                        pool.mark_limited(email)'''

K3_LAMA = '''                        elif st3 == "limited":
                            pool.mark_limited(em)'''
K3_BARU = '''                        elif st3 == "throttled":
                            pool.mark_throttled(em)
                            _requeue(jid, "throttle 429 (jalur penuh)", hitung=False)
                        elif st3 == "limited":
                            pool.mark_limited(em)'''


def main():
    if not os.path.exists(F):
        print(f"❌ {F} tidak ada"); return 1
    s = open(F, encoding="utf-8").read()

    if "mark_throttled" in s:
        print("ℹ️  Patch throttle sudah terpasang — tidak diulang.")
        return 0

    cad = f"{F}.bak-{TAG}-{int(time.time())}"
    shutil.copy2(F, cad)
    print(f"✅ Cadangan: {os.path.basename(cad)} ({os.path.getsize(cad)} byte)")

    langkah = [
        ("konstanta THROTTLE_COOLDOWN", KONST_LAMA, KONST_BARU),
        ("metode mark_throttled", ANCHOR_METHOD, METHOD_BARU),
        ("produsen 1 (stream gagal)", P1_LAMA, P1_BARU),
        ("produsen 2 (BUG UTAMA 429 kosong)", P2_LAMA, P2_BARU),
        ("produsen 3 (jalur ulang)", P3_LAMA, P3_BARU),
        ("konsumen batch", K1_LAMA, K1_BARU),
        ("konsumen UI st2", K2_LAMA, K2_BARU),
        ("konsumen UI st3", K3_LAMA, K3_BARU),
    ]

    for nama, lama, baru in langkah:
        n = s.count(lama)
        if n == 0:
            print(f"❌ [{nama}] pola TIDAK ditemukan — dibatalkan")
            shutil.copy2(cad, F)
            print("   ✅ file dikembalikan")
            return 1
        if n > 1:
            print(f"⚠️  [{nama}] pola muncul {n}× — memakai yang pertama")
        s = s.replace(lama, baru, 1)
        print(f"✅ {nama}")

    with open(F, "w", encoding="utf-8") as f:
        f.write(s)

    try:
        py_compile.compile(F, doraise=True)
        print("✅ py_compile OK")
    except Exception as e:
        print(f"❌ Sintaks rusak: {e} → mengembalikan")
        shutil.copy2(cad, F)
        return 1

    v = open(F, encoding="utf-8").read()
    cek = {
        "mark_throttled ada": "def mark_throttled" in v,
        "THROTTLE_COOLDOWN ada": "THROTTLE_COOLDOWN = int" in v,
        "produsen emit throttled": v.count('"throttled"') >= 3,
        "konsumen tangani throttled": 'if status == "throttled"' in v,
        "mark_limited MASIH ADA (31 hari utuh)": "def mark_limited" in v,
    }
    print()
    for k, ok in cek.items():
        print(f"   {'✅' if ok else '❌'} {k}")
    if not all(cek.values()):
        print("\n❌ Verifikasi gagal → mengembalikan")
        shutil.copy2(cad, F)
        return 1

    print()
    print("CARA BALIK: cp " + os.path.basename(cad) + " vids_runner.py  (lalu restart)")
    print("Atur lama parkir: VIDS_THROTTLE_COOLDOWN=900 (detik)")
    return 0


if __name__ == "__main__":
    sys.exit(main())