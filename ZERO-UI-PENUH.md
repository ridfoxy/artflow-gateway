# ZERO-UI PENUH — cara mengaktifkan, mengukur, dan membalikkan

Status: **AKTIF & TERUJI di produksi** (30 Sep 2026), gateway lokal 172.

## Hasil uji produksi (lewat https://api.artflowgen.com)

```
[05:48:12] ambil 3 job [8e1d0fd9] [40dd41c2] [70b78355]
[05:48:50] paralel x3 dalam 31.2s
[05:48:53] 8e1d0fd9 tersimpan 4168696 byte 720x1280   <- VERTIKAL BENAR
[05:48:56] 40dd41c2 tersimpan 3109388 byte 720x1280   <- VERTIKAL BENAR
[05:48:58] 70b78355 tersimpan 3028352 byte 720x1280   <- VERTIKAL BENAR
3 job selesai 52 detik (kirim->selesai), rasio 3/3 benar
```

Sebelumnya: job vertikal mengantri di jalur UI, ~3,5 menit untuk 1 job.

## Yang berubah

| Aspek | Sebelum | Sekarang |
|---|---|---|
| Tampilan browser | headful + Xvfb | **headless** (editor siap 2s vs 24s) |
| Job teks vertikal | jalur UI berurutan | **paralel XHR** (31s untuk 3 job) |
| Template jalur paralel | hardcode landscape | **sesuai rasio job** |
| Pengaman rasio | tidak ada | ada: hasil salah -> ulang lewat UI |
| Konkurensi | 1 | **8** |
| Daur ulang konteks | tidak ada | tiap 40 job (atasi bocor memori) |

## Tombol (env) — semua bisa dibalik tanpa ubah kode

| Env | Default | Arti |
|---|---|---|
| `VIDS_HEADLESS` | `1` | `0` = kembali headful + Xvfb |
| `VIDS_PORTRAIT_ZEROUI` | `1` | `0` = portrait kembali ke jalur UI |
| `VIDS_RECYCLE_JOBS` | `40` | buka ulang konteks tiap N job; `0` = tidak pernah |
| `VIDS_MAX_CONCURRENT` | `8` | job serentak |
| `VIDS_MAX_CONTEXTS` | `8` | sesi/akun serentak |
| `VIDS_PARALLEL` | `3` | prompt serentak per sesi (tanpa cooldown) |

Ubah: `systemctl edit artflow-vids-runner-local` atau sunting
`/etc/systemd/system/artflow-vids-runner-local.service.d/20-kapasitas.conf`,
lalu `systemctl daemon-reload && systemctl restart artflow-vids-runner-local`.

## Kapasitas & RAM (terukur)

- ~1,5 GB per konteks Chrome (headful). Headless lebih hemat (editor 2s).
- 8 konteks x 3 prompt paralel = 24 generate bersamaan (dibatasi 8 job).
- Render Google ~31-34s SERVER-SIDE: 1 prompt dan 3 prompt serentak
  memakan waktu hampir sama -> menambah prompt serentak nyaris gratis.
- Tidak ada cooldown di sisi Google Vids; yang tampak seperti antrean adalah
  batas batch (`VIDS_PARALLEL`) — naikkan bila perlu.

## Rollback

Backup berurutan (terbaru terakhir):
```
vids_runner.py.bak-zeroui-semua-1790745965
vids_runner.py.bak-rasio-recycle-1790746009
vids_runner.py.bak-recycle-1790746087
vids_runner.py.bak-zeroui-penuh-1790747236     <- terbaru sebelum patch penuh
```

Balikkan:
```
cd /root/api-google-flow
cp vids_runner.py.bak-zeroui-penuh-1790747236 vids_runner.py   # atau .bak lain
systemctl restart artflow-vids-runner-local
```

Balikkan hanya fitur tertentu (tanpa sentuh kode):
```
# headful lagi
systemctl edit artflow-vids-runner-local   # tambah: Environment=VIDS_HEADLESS=0
# portrait ke jalur UI lagi
#   Environment=VIDS_PORTRAIT_ZEROUI=0
# konkurensi lama
#   Environment=VIDS_MAX_CONCURRENT=1 / VIDS_MAX_CONTEXTS=1
```

## Catatan penting

- **Xvfb tidak lagi diperlukan** untuk runner headless (masih jalan di :99,
  hanya tak terpakai). Aman dibiarkan; bisa dimatikan untuk hemat ~50 MB.
- Pengaman rasio hanya berlaku pada jalur yang bisa menghasilkan rasio salah
  (paralel). Jalur tunggal sudah memverifikasi rasio sejak sebelumnya.
- Job BERGAMBAR tetap lewat jalur UI: payload gambar butuh dokumen proyek yang
  dibuat aplikasi Vids. Ini batas nyata, bukan pilihan.