# Riset RPC Vids — temuan (terukur)

## Hasil uji banding (akun denijfx8@rallse.com, prompt sama)

| Jalur | Waktu | Hasil |
|---|---|---|
| RPC (tembak payload template via XHR) | 50,4s | MP4 4.108.757 B, ftypmp42, avc1+mp4a |
| UI (ketik prompt + klik Generate) | 300,6s | video tidak muncul sebelum batas waktu |

Rincian RPC: panen token 2,0s -> kirim 45,0s -> unduh 3,3s.

## Yang dipakai produksi

Produksi memakai `xhr_post_many()` (banyak body dalam SATU panggilan RPC),
bukan satu-satu. Log nyata: 16 video berturut-turut 32-52s (rata-rata 40,6s).
Kata kunci log: "paralel tersimpan".

## Endpoint RPC Vids

```
POST https://appsgenaiserver-pa.clients6.google.com/v1/genai/generate?key=AIzaSy...
Content-Type: application/json+protobuf
Headers: X-Server-Token, Authorization (SAPISIDHASH + SAPISID1PHASH),
         X-Goog-AuthUser, Accept-Language
Body: array protobuf-JSON (~567 char), memuat rujukan berkas
      "application/vnd.google-apps.flix"
```

## Tiga syarat agar token bisa dipanen (ditemukan lewat 3 percobaan gagal)

1. Buka `https://docs.google.com/videos/create` — BUKAN `/videos/u/0/`.
2. Pasang hook `HOOK_JS` SEBELUM `goto` (hook mengintip window.fetch + XHR
   untuk URL yang memuat "appsgenaiserver").
3. Klik tombol "Create AI videos" — panggilan quotaSummary (sumber token)
   baru muncul SETELAH editor dibuka.

Tanpa ketiganya, `harvest_token()` mengembalikan None.

## Pembanding Flow

Flow memakai endpoint berbeda: `aisandbox-pa.googleapis.com` + batchexecute,
RPC ID `YhhmEf` (generate) dan `jwpduf` (poll). Fungsi RPC Flow ada di
`batchexecute_engine.py` tetapi NOL pemanggil (kode mati).

## Catatan penting

Kedua endpoint tetap memerlukan X-Server-Token + SAPISIDHASH dari cookie sesi
yang sah. "Tembak langsung ke RPC" menghapus kebutuhan membuka editor,
bukan kebutuhan autentikasi.
