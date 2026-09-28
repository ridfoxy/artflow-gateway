# ArtFlow Gateway — Google Flow + Vids REST API

Gateway FastAPI yang membungkus Google Flow dan Google Vids menjadi REST API,
plus runner Vids mandiri. Dipakai oleh bot Telegram ArtFlow (`artflow-bot`).

## Isi

| Berkas | Fungsi |
|---|---|
| `app.py` | Gateway FastAPI (docs-custom v3.0). Endpoint `/v1/google-flow/*` dan `/v1/vids/*` |
| `flow_pool.py` | Kolam worker Flow: buka editor, pasang gambar acuan, submit, atribusi hasil |
| `vids_runner.py` | Runner Vids: antrian di disk, rotasi akun, jalur zero-UI + unggah gambar |
| `browser_manager.py` | Sesi browser persisten (profil per akun) |
| `gsuite_login.py` | Login otomatis GSuite (email|password -> profil Chrome) |
| `sapisidhash_resolver.py` | Resolver SAPISIDHASH untuk permintaan bertanda tangan |
| `batchexecute_engine.py` | Mesin batchexecute (jalur RPC internal Google) |
| `systemd/vids-runner.service` | Unit systemd runner Vids |

## Endpoint utama

Flow (gambar + video, mendukung foto acuan / start-end / rasio):
```
POST /v1/google-flow/images/create     # nano-banana-2-lite
POST /v1/google-flow/videos/create     # omni-flash
GET  /v1/google-flow/jobs/{job_id}
GET  /v1/google-flow/media/{job_id}
```

Vids (video; teks->video zero-UI, foto lewat UI):
```
POST /v1/vids/videos/create            # {prompt, aspect, images[]}
GET  /v1/vids/jobs/{job_id}
GET  /v1/vids/media/{job_id}
GET  /v1/vids/accounts                 # keadaan pool
POST /v1/vids/accounts/purge           # hapus akun bermasalah
```

## Catatan penting (pelajaran lapangan)

- **Atribusi hasil Flow**: semua job berbagi satu proyek, jadi galeri berisi hasil
  job lain. Atribusi memakai id media yang **baru muncul di DOM setelah submit**,
  dengan lalu lintas jaringan halaman sebagai cadangan.
- **`JOB_TIMEOUT=540s`**: bila media tidak bisa diatribusikan, job menunggu penuh
  9 menit. `FLOW_NO_CANDIDATE_GRACE` (default 150s) memotong penantian itu bila
  tidak ada kandidat sama sekali.
- **Sesi tidak sah** (`/unsupported-country`, respons `No session found`) membuat
  SEMUA kandidat ditolak dengan galat identik. Gejalanya: `resolved to non-media:
  ct=text/html size=14`.
- **Rasio Vids** disimpan di dokumen proyek, bukan di payload generate. Portrait
  dipilih lewat tombol "Generation settings" -> radio Portrait.
- **Unggah gambar Vids** memakai `input[type=file]` tersembunyi (accept png/jpeg).
  Setelah unggah, label kolom prompt **berubah** dari "Describe your video" menjadi
  "Specify how you want to use the ingredients".
- **Job berfoto wajib lewat UI**: payload berisi gambar (Image1/Image2) baru
  disusun aplikasi saat tombol Generate diklik.

## Konfigurasi (variabel lingkungan)

```
FLOW_PROJECT_ID             proyek Flow yang dipakai
FLOW_POOL_SIZE=8            jumlah worker
FLOW_LOAD_GATE=3            pemuatan editor bersamaan
FLOW_JOB_TIMEOUT=540        batas satu job (detik)
FLOW_NO_CANDIDATE_GRACE=150 gagal cepat bila tak ada kandidat
FLOW_ENABLE_VIDS_API=1      aktifkan endpoint Vids
VIDS_MAX_CONCURRENT=3       job Vids bersamaan
VIDS_ZERO_UI=1              jalur cepat tanpa UI untuk teks->video
VIDS_ASPECT_DEFAULT         landscape|portrait
```

## ⚠️ Rahasia

Repo ini **tidak** memuat kredensial. Yang sengaja tidak disertakan:

- `accounts.txt` (email|password akun GSuite)
- `chrome_profiles/` (cookie sesi per akun)
- `data/*.db` (memuat email akun dan API key)
- API key dan bot token (diredaksi menjadi `REDACTED` di kode)

Untuk menjalankan: siapkan berkas-berkas itu sendiri.
