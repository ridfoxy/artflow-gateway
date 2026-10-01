# RPC portrait bergambar — langkah 3 (terbukti)

## Temuan kunci (membalikkan dugaan lama)

Rasio video ter-encode DI DALAM payload:

```
[4][15][4]  =  1 -> landscape
               2 -> portrait
```

Diperoleh dengan membandingkan `genai_template.json` (landscape) dan
`portrait_template.json` (portrait): satu-satunya beda struktural selain
id dokumen adalah elemen ini.

DUGAAN LAMA SALAH: komentar kode menyebut "rasio portrait hidup di dokumen
proyek Vids yang dibuat lewat UI". Tidak perlu dokumen proyek sama sekali —
cukup mengisi angka rasio di payload.

## Hasil uji produksi (log runner, 3 job)

| Job | Waktu | Dimensi | Hasil |
|---|---|---|---|
| portrait 1 gambar | 49s | 720x1280 | OK |
| portrait 2 gambar | 121s | 720x1280 | OK |
| landscape 2 gambar | 40s | 1280x720 | OK |

Bukti log:
```
[a8974f5b] 2 gambar: payload RPC siap (1005 char)
[a8974f5b] tersimpan vids_a8974f5b.mp4 (botol sesi ke-1)
[a8974f5b] rasio hasil terverifikasi 720x1280
```

Uji terpisah (RPC murni): HTTP 200, kirim 39,6s, unduh 3,4s, 720x1280 (ffprobe).

## Kode yang dipasang

```python
RASIO_PATH  = (4, 15, 4)
RASIO_NILAI = {"landscape": 1, "portrait": 2}
IMG_XHR     = os.environ.get("VIDS_IMG_XHR", "1")   # flag rollback
```

`build_2image_body(prompt, tokens, aspect="landscape")` kini mengisi rasio.
Baris `ui_fallback "gambar + portrait: perlu dokumen proyek (UI)"` DIHAPUS,
diganti jalur RPC dengan flag.

## Pengaman yang sudah ada (bukan buatan patch ini)

Kode produksi memverifikasi dimensi hasil; bila bukan vertikal padahal diminta
portrait, job otomatis diulang lewat jalur UI. Jaring pengaman ini dipertahankan.

## Rollback

```
cepat : VIDS_IMG_XHR=0 lalu restart runner  -> gambar kembali ke jalur UI
penuh : cp vids_runner.py.bak-portraitgambar-* vids_runner.py
```

## Belum dikerjakan

Job bergambar/portrait masih dikirim BERURUTAN (generate_in_session), bukan
serentak seperti job teks. Itu sebabnya portrait 2 gambar (121s) lebih lama
daripada landscape 2 gambar (40s): ia menunggu giliran. Patch agar bergambar
ikut jalur paralel sempat disiapkan lalu DIBATALKAN karena rancangannya rapuh
dan menyentuh jalur tercepat produksi; lebih baik dikerjakan dengan uji
terpisah.
