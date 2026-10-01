# Optimasi login — hasil terukur + rollback

## Hasil pengukuran nyata

Dua akun, keduanya melewati jalur persyaratan Workspace (jalur yang paling
lambat), dijalankan dengan jalur produksi yang sama:

| Jalur | Akun | Waktu | Status |
|---|---|---|---|
| OPTIMASI | denijfx23 | 128s | ok |
| MODE LAMA (GS_LOGIN_CEPAT=0) | denijfx24 | 167s | ok |

Hemat 39s per akun (23%) pada jalur persyaratan.

Rincian tahap akun optimasi (dari log [waktu]):
```
email terisi       : 3,1s
sandi terisi       : 6,4s
sandi terkirim     : 7,3s
persyaratan diklik : 12,5s
```

## Akar masalah yang ditemukan (sebelum optimasi)

Analisis read-only menemukan bahwa jalur persyaratan Workspace bisa memakan
sampai ~167s karena EMPAT hal:

1. Selector dicoba BERURUTAN tanpa cek DOM: 17 selector x timeout 3,5s = 59,5s
   bila tombol ada di urutan belakang.
2. Caller menunggu 3 putaran x 30s = 90s walau halaman sudah berpindah.
3. gulir_sampai_bawah berhenti hanya bila posisi sama PERSIS, sehingga halaman
   lazy-load berjalan sampai 25 langkah penuh = 17,5s.
4. Sleep tetap 3-4s di beberapa titik.

## Empat optimasi yang dipasang

Semua lewat env, sehingga bisa dimatikan tanpa mengubah berkas:

| # | Perubahan | Variabel |
|---|---|---|
| 1 | Selector setuju: cek DOM dulu (pakai `_present_now` yang SUDAH ada di kode, dipakai `fill_first`) | `GS_LOGIN_SEL_MS` |
| 2 | Poll perpindahan halaman 1,5s -> 0,5s | `GS_LOGIN_POLL` |
| 3 | Gulir pakai toleransi 5px | `GS_LOGIN_GULIR_TOL` |
| 4 | Log waktu per tahap (untuk MEMBUKTIKAN, bukan klaim) | - |

Saklar utama: `GS_LOGIN_CEPAT` (bawaan 1). Bila 0, semua nilai kembali ke
angka lama (3500ms, 1,5s, toleransi 0) — sudah diuji.

## Bug yang ditemukan lewat uji mock (sudah diperbaiki)

`abs(posisi - posisi_akhir)` meledak TypeError bila `page.evaluate`
mengembalikan None (halaman transisi). Ini NYATA di produksi bila halaman
sedang berpindah. Diperbaiki dengan koersi ke angka.

Pelajaran: uji dengan mock LEBIH DULU menemukan bug yang tidak terlihat dari
membaca kode saja.

## Rollback — 4 cara, teruji 4/4

```
python3 rollback_login.py status      # lihat keadaan + daftar cadangan
python3 rollback_login.py cepat       # GS_LOGIN_CEPAT=0 + restart gateway
python3 rollback_login.py penuh       # salin cadangan terbaru
python3 rollback_login.py otomatis    # rollback bila 3 akun terakhir gagal teknis
python3 rollback_login.py aktifkan    # hidupkan lagi
```

Rollback otomatis CERDAS: CAPTCHA / sandi salah TIDAK memicu rollback (itu
urusan akun, bukan kode). Hanya state `error` teknis dan `needs_human` yang
detailnya menyebut persyaratan/workspaceterms.

Uji rollback pada salinan: 4/4 lulus (kode optimasi hilang, berkas tidak
rusak, hasil identik dengan cadangan, versi optimasi tersimpan untuk maju lagi).

Cadangan: `gsuite_login.py.bak-optimasi-*`

## Catatan operasional

- Xvfb :99 adalah prasyarat; tanpa itu Chrome gagal dengan "Missing X server
  or $DISPLAY" dan login selalu error. Jalankan `pastikan_xvfb.py`.
- Membersihkan Chrome harus memakai filter KETAT (cocokkan biner
  /usr/bin/google-chrome). Filter berbasis substring "chrome" akan membunuh
  skrip pembersih itu sendiri dan shell pemanggilnya.
