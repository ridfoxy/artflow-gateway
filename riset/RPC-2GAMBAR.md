# RPC 2 gambar — langkah 1 & 2 (terbukti)

## Hasil

| Uji | Waktu | Hasil |
|---|---|---|
| RPC 2 gambar (langsung) | kirim 30,7s + unduh 3,1s = 33,8s | MP4 2.958.854 B, 2 burung |
| RPC 2 gambar (produksi) | completed 48s | MP4 2.442.095 B, "botol sesi ke-1" |
| UI 2 gambar (sebelum) | 300s+ | sering timeout |

Bukti log produksi:
```
[85f43b8b] 2 gambar: payload RPC siap (1427 char)
[85f43b8b] tersimpan vids_85f43b8b.mp4 (botol sesi ke-1)
```

Verifikasi visual: video memuat DUA burung sesuai dua gambar referensi.

## Struktur payload (dari rekaman nyata)

```
[0]                              = 376   (374 = teks saja, 376 = bergambar)
[2][25]                          = wadah gambar, panjang = jumlah gambar
[2][25][i][0][7][0]              = UUID unik gambar ke-i
[2][25][i][0][0][7][1][11][8][3] = token AVL gambar ke-i
[3][3][0][0][2]                  = prompt
```

Catatan: pemetaan di atas sudah diverifikasi dua cara (penelusuran rekursif
dan pengambilan langsung per jalur). Peta lengkap: `data/PETA-2GAMBAR.json`.

## Wajib: basis rekaman nyata

Template polos ditolak 400 (merujuk dokumen proyek lama). Basis yang dipakai:
`data/with_2images_payload_REAL.json`, direkam dengan cara benar:
unggah gambar 1 -> klik tombol "Add avatar or ingredients" -> unggah gambar 2.
Bukti rekaman: chip `['@Image1','@Image2']`, thumbnail 4, body 1423 char.

Cara lama gagal: mengirim 2 berkas ke satu `input[type=file]` ->
"Non-multiple file input can only accept single file".

## Kode yang dipasang

- `build_2image_body(prompt, tokens)` + `_set_path()` (pengisi jalur bersarang)
- konstanta `IMAGE2_RECORD`, `IMG2_XHR`, `IMG2_AVL_PATH`, `IMG2_PROMPT_PATH`
- titik keputusan 2 gambar menghormati `IMG2_XHR`

## Rollback

```
cepat : VIDS_IMG2_XHR=0 lalu restart runner  -> kembali ke jalur UI
kode  : cp vids_runner.py.bak-2gambar-* vids_runner.py
otomatis: bila RPC gagal, kode jatuh ke UI (ui_fallback)
```

## Masih jalur UI (langkah 3, belum dikerjakan)

Gambar + portrait: rasio portrait hidup di dokumen proyek Vids.
Log nyata: `ui_fallback: gambar + portrait: perlu dokumen proyek (UI)`.

## Catatan kredensial

Token AVL bersifat sekali pakai dan sudah kedaluwarsa; tetap diredaksi di repo
karena bentuknya menyerupai kredensial hidup.
