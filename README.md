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

## Panel admin: setor akun pool (endpoint)

Dua endpoint supaya bot bisa mengelola daftar akun dari panel admin:

```
GET  /v1/vids/accounts/template   -> contoh format + jumlah akun sekarang
POST /v1/vids/accounts/import     -> {"entries":[{"email","password"}],
                                      "mode":"replace"|"append"}
```

Aturan yang dipakai endpoint import:

- Setiap email **wajib** punya password. Akun tanpa password ditolak dan
  dilaporkan di `skipped`, karena pool tidak bisa login tanpanya — menyimpannya
  hanya membuat akun terlihat ada padahal tidak bisa dipakai.
- Email tidak valid / duplikat juga masuk `skipped`, bukan gagal total.
- Berkas ditulis lewat berkas sementara lalu `os.replace` (atomik) dan
  `chmod 600`.
- **Cadangan dibuat setiap penyetoran** (`accounts.txt.bak-import-<epoch>`).
  PENTING: `app.py` mengimpor `shutil` di dalam fungsi lain, jadi endpoint ini
  WAJIB memakai `import shutil` lokal — memakai nama global menghasilkan
  `NameError` yang tertelan `except` sehingga cadangan tidak pernah terbentuk
  dan berkas tertimpa tanpa jejak pemulihan.

Memulihkan setelah penyetoran yang salah: jangan menyamakan berkas utuh. Ambil
keanggotaan yang benar dari DB (`SELECT email FROM vids_accounts`) lalu ambil
baris `email|password` yang cocok dari cadangan terbaru.

## Memilih mesin: Flow atau Vids

Keduanya hidup berdampingan dan dipilih pemanggil, bukan oleh gateway:

- **Flow** (`/v1/google-flow/*`) — mendukung `aspectRatio`, `referenceImages`,
  `startImage`, `endImage` sebagai parameter API.
- **Vids** (`/v1/vids/*`) — hanya `prompt`, `aspect`, `images` (1 = referensi,
  2 = awal+akhir). Rasio tidak ada di payload generate; rasio hidup di dokumen
  proyek Vids, jadi portrait dibuat lewat dokumen proyek hasil rekaman.

Bot memilih mesin lewat `VIDEO_BACKEND`, bisa diubah admin dari panel admin.

## Beban: PARALLEL dikali MAX_CONCURRENT

`VIDS_PARALLEL` mengalikan jumlah generate serentak: total yang terbang =
`VIDS_MAX_CONCURRENT x VIDS_PARALLEL`. Di host 2 core / ~4 GB, `3 x 3 = 9`
membuat semua sesi gagal dibuka dan proses Chrome menumpuk (55 proses sisa).
Setelan aman: `3 x 2 = 6`. Buka editor ~27s dibayar SEKALI per sesi, lalu tiap
video ~31s; satu token hasil panen sah untuk banyak generate.
