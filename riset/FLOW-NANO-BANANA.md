# Nano Banana (Flow) — BERHASIL: akar masalah & perbaikan

## Hasil

| Uji | Hasil |
|---|---|
| 1 job | completed 73s, JPEG 798.800 B, verifikasi visual sesuai prompt |
| 3 job berturut | completed 3/3, rata-rata 46s (41-49s), semua JPEG sah |

Sebelum perbaikan: 7 job berturut gagal, macet di "queued 99%".

## AKAR MASALAH

Kode lama MEMBUANG tanda tangan URL:

    m = u.rsplit("/", 1)[-1].split("?")[0]     # -> hanya UUID

Lalu URL dibangun ulang dari UUID ke `labs.google/fx/api/trpc/media.getMediaUrlRedirect`
-> SELALU 401 "No session found" (16 byte text/html) — inilah error yang dikejar.

Padahal `src` elemen <img> SUDAH berisi URL lengkap dengan tanda tangan sah:

    https://flow-content.google/image/<uuid>?Expires=...&KeyName=labs-flow-prod-cdn-key&Signature=...

## BUKTI UJI (semua terukur)

| Bentuk URL | page.request | fetch JS |
|---|---|---|
| `flow-content.google/image/<uuid>?Expires=&Signature=` (UTUH) | **200 image/jpeg 753.971 B** | TypeError (CORS) |
| `flow-content.google/<uuid>` (tanpa tanda tangan) | 403 | TypeError |
| `flow-content.google/d/<uuid>` | 403 | TypeError |
| `flow.google.com/asb/<uuid>` | 400 | 400 |
| `lh3.google.com/d/<uuid>` | 400 | 400 |
| `labs.google/...redirect?name=<uuid>` | 401 (16 B) | TypeError |

Catatan: ekstraksi lewat canvas GAGAL (SecurityError: tainted) karena
flow-content.google adalah origin berbeda. `fetch` JS juga diblokir CORS.
HANYA `page.request.get()` (sisi Playwright, tidak terikat origin) yang lolos.

## PERBAIKAN

1. `_media_ids()` dan `_collector()` menyimpan URL LENGKAP (tanda tangan ikut),
   bukan hanya UUID-nya. Flag: `FLOW_URL_UTUH` (bawaan 1).
2. `_store_media()`: bila id berupa URL -> `page.request.get(url)` langsung.
   Token `/asb/` juga didukung (`url_media_dari_id`).
3. `_store_result()` WAJIB dipanggil di jalur sukses — tanpa itu job tetap
   "queued 99%" dan hasil tidak pernah dikirim ke bot.

## TOKEN TIDAK DIPERLUKAN

Pengintai jaringan (88 respons, halaman proyek) menunjukkan **0 panggilan
ber-Bearer ya29**. `aisandbox-pa` hanya dipanggil untuk `checkAppAvailability`
dan tanpa auth. Jadi kegagalan lama BUKAN soal token.

## Jalur UI vs API

- Kirim prompt : UI (klik `.ProseMirror`, ketik, Enter)
- Ambil hasil  : API (`page.request.get` ke URL utuh)
- Jadi Flow = UI + API campuran, bukan XHR murni.

## Rollback

    cepat : FLOW_URL_UTUH=0 lalu restart gateway
    penuh : cp flow_pool.py.bak-urlutuh-* flow_pool.py

## Catatan: endpoint diagnosa di app.py

`app.py` di repo memuat endpoint diagnosa yang dipakai selama penyelidikan:
`/v1/browser/pages`, `/v1/browser/exec-in-page-ke`, `/v1/browser/fetch-url`,
`/v1/browser/net-spy` (plus pengintai `_SPY_NET` di browser_manager.py).
Semua bertanda SEMENTARA dan aman (read-only), tetapi bisa dihapus bila tidak
diperlukan lagi.

## Yang terbantah selama penyelidikan (jangan diulang)

- "Butuh token ya29" -> tidak perlu sama sekali
- "CORS / perlu idle" -> fetch JS memang diblokir, tapi page.request lolos
- "UUID tidak punya URL yang bisa diambil" -> SALAH: URL-nya ada di `src`,
  hanya saja tanda tangannya dibuang kode lama
