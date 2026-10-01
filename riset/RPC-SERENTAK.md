# Kirim serentak untuk job bergambar — langkah 4 (terbukti)

## Masalah sebelumnya

Job bergambar/portrait dikirim BERURUTAN (`generate_in_session`), bukan
serentak seperti job teks. Efeknya terukur: portrait 2 gambar butuh 121s,
sedangkan landscape 2 gambar hanya 40s.

## Hasil sesudah perbaikan (6 job campuran, uji produksi)

| Kode | Job | Waktu | Dimensi | Hasil |
|---|---|---|---|---|
| T1 | teks landscape | 65s | 1280x720 | OK |
| T2 | teks portrait | 66s | 720x1280 | OK |
| P1 | portrait 1 gambar | 65s | 720x1280 | OK |
| P2 | portrait 2 gambar | 74s | 720x1280 | OK |
| L1 | landscape 1 gambar | 57s | 1280x720 | OK |
| L2 | landscape 2 gambar | 57s | 1280x720 | OK |

Total 6 job dalam 74s. Portrait 2 gambar turun dari 121s -> 74s.

Bukti bergambar masuk jalur paralel:
```
b4: ambil 1 job [5eadbe64]
[5eadbe64] paralel tersimpan vids_5eadbe64.mp4 1280x720

b2: ambil 3 job [8f19f4f8] [8e0590ec] [262c3217]
[8f19f4f8] paralel tersimpan vids_8f19f4f8.mp4 720x1280
```

## Pendekatan aman

1. Logika perakitan dipindah ke FUNGSI MURNI yang bisa diuji tanpa browser:
   `rakit_body_untuk(info, tpl_lan, tpl_port, img_tokens) -> (body, galat)`
2. Diuji terpisah lebih dulu: 22 uji, 0 gagal
   (teks L/P, 1-2 gambar + rasio, jalur kegagalan, flag mati, masukan aneh).
3. Tidak ada job yang hilang: rakit gagal -> job DITURUNKAN ke jalur urut,
   bukan dibuang. Log: "paralel: <alasan> -> jalur urut".
4. Prasyarat terbukti: URL endpoint teks dan bergambar SAMA, sehingga
   `xhr_post_many` bisa memuat campuran body teks + bergambar.

## Kode yang dipasang

- `IMG_XHR_PARALEL` (env `VIDS_IMG_PARALEL`, bawaan 1)
- `rakit_body_untuk()` - fungsi murni perakit body per job
- klasifikasi: `lambat = (imgs and not IMG_XHR_PARALEL) or (port and not
  ZERO_UI_PORTRAIT and not imgs)`
- penyusunan bodies memakai fungsi murni; unggah token AVL via `upload_media_xhr`

## Rollback

```
cepat : VIDS_IMG_PARALEL=0 lalu restart runner  -> bergambar berurutan lagi
penuh : cp vids_runner.py.bak-paralel4-* vids_runner.py
```

## Catatan

Uji terpisah penting: patch pertama untuk langkah ini sempat DIBATALKAN karena
rancangannya rapuh (memakai variabel dengan `dir()` yang bisa bocor antar
iterasi). Yang dipasang sekarang memakai fungsi murni + daftar job eksplisit.
