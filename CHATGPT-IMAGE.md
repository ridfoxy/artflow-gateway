# GPT Image via ChatGPT — jalur, limit, dan zero-UI RPC

Ringkasan riset 4 Okt 2026 di VPS 139.162.4.227. Semua angka hasil uji nyata.

## 1. Jalur yang dipakai

| Jalur | Status | Catatan |
|---|---|---|
| API omniroute `/v1/images/generations` | ❌ | `No credentials for image provider: vercel-ai-gateway` |
| API OpenAI langsung | ❌ | tidak ada kunci |
| **Browser ChatGPT (Camoufox)** | ✅ | **dipakai sekarang** |
| Codex Responses `POST /backend-api/codex/responses` | ⚠️ | butuh OAuth Codex (`~/.codex/auth.json`) — belum ada |

Penting: `curl https://chatgpt.com/` → **403** (8 KB) = challenge Cloudflare,
bukan blokir. Browser nyata membuka halaman aslinya. `auth.openai.com` juga
menampilkan "Just a moment..." yang selesai sendiri ~5 detik.

## 2. Login (tanpa sandi OpenAI, tanpa API key)

ChatGPT punya **"Continue with Google"** sehingga akun pool `@gumdel.com` bisa dipakai:

```
chatgpt.com/auth/login → Continue with Google
  → accounts.google.com  (#identifierId, #identifierNext, input[name=Passwd], #passwordNext)
  → "Sign in to OpenAI ... allow OpenAI to access this info" → #submit_approve_access
  → auth.openai.com/about-you  (input[name=name] + input[name=age] → Finish signing up)  [sekali saja]
  → chatgpt.com  (kotak prompt #prompt-textarea ada)
```

Sesi tersimpan di profil `/root/chatgpt_profil_<user>` → run berikutnya tak perlu login.

Periksa login dari **#prompt-textarea**, bukan dari URL: halaman belum-login
berjudul "ChatGPT: Chat, Work, Create & Code with AI" dan punya tombol "Log in".

## 3. DUA limit berbeda — jangan tertukar

Kuota asli bisa dibaca **zero-UI** (cukup Bearer token, tanpa UI, tanpa sentinel):

```
POST /backend-api/conversation/init
     → limits_progress[] feature_name="image_gen" → {remaining, reset_after}
GET  /backend-api/wham/usage              → plan_type, rate_limit
GET  /backend-api/accounts/check/v4-2023-04-27 → entitlement.subscription_plan
```

Hasil baca 3 akun free: `image_gen.remaining` = **20, 23, 24** (reset ~24 jam)
→ kuat mengarah ke **~25 gambar/hari per akun**.

| Limit | Sifat | Terukur |
|---|---|---|
| **Throttle burst** | jangka pendek | **1–2 gambar** beruntun, lalu jeda **~1 jam** |
| **Kuota harian** | per hari | **~25 gambar** (`image_gen.remaining`) |

Bukti penting: akun bersih berhenti setelah 1–2 gambar **padahal `remaining` masih 20**.

Uji 5 akun bersih beruntun: **1, 1, 1, 2, 1** → rata-rata **1,2 gambar** sebelum ditolak.

Pesan penolakan (kutipan asli):
> "Maaf, saya belum bisa membuat gambarnya sekarang karena fitur pembuatan gambar
> sedang terkena batas penggunaan sementara. Silakan coba lagi sekitar satu jam lagi."

Penolakan datang sebagai **pesan chat**, bukan error HTTP, dan **tanpa `<img>` sama sekali**
— loop yang hanya menunggu gambar akan timeout. Periksa `document.body.innerText`.

⚠️ Jangan deteksi dengan kata `batas`/`limit` saja — teks UI biasa mengandungnya.
Pakai frasa lengkap: `batas penggunaan`, `coba lagi sekitar`, `sedang terkena batas`.

**Beda dengan Google Flow:** Flow 429 = reset **bulanan** (akun dipensiunkan).
ChatGPT = reset **~1 jam** → akun boleh dipakai lagi. Rotasi akun tetap wajib
karena throughput per akun hanya ~1–2 gambar/jam.

Kapasitas: 63 akun × 1–2 gambar/jam ≈ **63–126 gambar/jam**.

## 4. Zero-UI RPC — hasil penyelidikan

### Yang BISA dilakukan zero-UI sekarang
- **Baca kuota** (`conversation/init`, `wham/usage`, `accounts/check`) ✅ terbukti
- **Ambil gambar** setelah dikirim UI (`files/download` → `estuary/content`) ✅ terbukti

### Yang BELUM bisa murni HTTP
`POST /backend-api/f/conversation` tanpa UI dijawab:
```
403 {"detail":"Unusual activity has been detected from your device. Try again later."}
```

Percobaan 1 — kirim murni RPC tanpa header sentinel → 403.
Percobaan 2 — panen SEMUA header dari kiriman UI yang berhasil, lalu kirim ulang
→ **tetap 403**. Sebabnya: token `openai-sentinel-turnstile-token` **sekali pakai**
(sudah terpakai saat kiriman UI). Token sentinel juga berumur **540 detik**
(`expire_after: 540`).

### Header wajib yang tertangkap dari kiriman yang BERHASIL
```
openai-sentinel-chat-requirements-token : 2980 char
openai-sentinel-proof-token             :  641 char
openai-sentinel-turnstile-token         : 3448 char
x-conduit-token                         :  351 char
oai-device-id, oai-client-version, oai-client-build-number,
oai-session-id, oai-echo-logs, x-oai-is-client-observation,
x-openai-target-path, x-openai-target-route, x-openai-web-frontend,
x-openai-web-sse-compression, oai-genui-client-actions, oai-telemetry
```

### Persyaratan sentinel (dari `sentinel/chat-requirements/prepare`)
```json
{"persona": "chatgpt-freeaccount",
 "prepare_token": "<4044 char>",
 "turnstile":   {"required": true, "dx": "<blob>"},
 "proofofwork": {"required": true, "seed": "0.5196916926941115", "difficulty": "06479a"}}
```
- **proof-of-work**: hash-wasm + sidik jari perangkat (layar, zona waktu, jumlah core),
  cari nonce sampai `sha3_512(seed + nonce + fingerprint)` diawali `difficulty`.
  Deterministik → **bisa dihitung di luar browser**.
- **turnstile**: token dari Cloudflare Turnstile yang dibuat SDK `sentinel.openai.com`
  (`/sentinel/<versi>/sdk.js`, `frame.html`, `/backend-api/sentinel/req`).
  Bergantung VM dalam iframe — **tidak bisa dihitung sendiri**; harus dijalankan di
  browser asli, dan sekali pakai.

Karena turnstile sekali pakai, "1 kali UI + N kali RPC" tidak bisa. Pilihan:
jalankan SDK sentinel sekali per kiriman (tanpa klik/ketik, hanya `page.evaluate()`
+ `fetch()` dari dalam halaman) — itu bentuk zero-UI yang realistis.

### Jalur alternatif yang lebih bersih: Codex Responses
```
POST https://chatgpt.com/backend-api/codex/responses
Authorization: Bearer <OAuth Codex>
{"model":"gpt-5.5","store":false,"stream":true,
 "instructions":"...use the image_generation tool...",
 "input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"<prompt>"}]}],
 "tools":[{"type":"image_generation","model":"gpt-image-2","size":"1024x1024",
           "quality":"medium","output_format":"png","background":"opaque",
           "partial_images":1}]}
```
SSE: `response.image_generation_call.in_progress` → `.generating` → `.partial_image`
→ `response.output_item.done` (`item.result` = **base64 PNG**) → `response.completed`.

Keunggulan: **cukup Bearer**, tanpa sentinel/turnstile, gambar langsung base64,
`size`/`quality` bisa ditentukan. Implementasi siap pakai ada di Hermes:
`/usr/local/lib/hermes-agent/plugins/image_gen/openai-codex/__init__.py`.
⚠️ Jangan kirim `tool_choice` untuk tool hosted ini — semua bentuknya dijawab
HTTP 400 `Tool choice 'image_generation' not found in 'tools' parameter`.
Syarat: login web ChatGPT **tidak otomatis** memberi token Codex (`~/.codex/auth.json`).

## 5. Urutan RPC lengkap (dari rekaman 312 request)

```
+1.1s  POST /backend-api/f/conversation/prepare        → 200 json
+3.9s  POST /backend-api/f/conversation                → 200 text/event-stream
+4.3s  POST /backend-api/sentinel/chat-requirements/prepare → 200
+5.1s  POST /backend-api/conversation/init             → 200 (model_limits, limits_progress)
+6.7s  POST /backend-api/sentinel/chat-requirements/finalize → 200
       GET  /backend-api/files/download/file_<id>?conversation_id=<cid> → 200 json
       GET  /backend-api/estuary/content?id=file_<id>&ts=..&p=fsns&cid=1&sig=.. → 200 image/png
```

Isi `POST /backend-api/f/conversation` — TIDAK ada array `tools`, prompt teks biasa:
```json
{"turn_attribution":{"turn_trigger":"composer"},"action":"next",
 "model":"auto","parent_message_id":"client-created-root",
 "messages":[{"author":{"role":"user"},"content":{"content_type":"text",
   "parts":["<PROMPT>"]},"id":"<uuid>","recipient":"all"}],
 "supported_encodings":["v1"],"client_prepare_state":"success"}
```

Dokumen rinci: `references/gpt-image-zeroui-rpc.md` di skill `chatgpt-gpt-image-browser`.
Rekaman mentah: `output/rpc_gptimg.json`, `output/kepala_fconversation.json`,
`output/sentinel_rinci.json`, `output/kuota_rpc.json`.

## 6. Skrip

| Berkas | Fungsi |
|---|---|
| `login_chatgpt3.py` | login Google lengkap (email→sandi→izin→about-you) |
| `uji_gptimage.py` | generate 1 gambar lewat UI |
| `tangkap_rpc.py <akun>` | rekam seluruh lalu lintas saat generate |
| `analisa_rpc.py` / `gali_rpc.py` / `gali_rpc2.py` | analisis rekaman |
| `tangkap_kepala.py <akun>` | tangkap header lengkap kiriman yang berhasil |
| `sentinel_rinci.py <akun>` | rinci persyaratan sentinel |
| `zeroui_login.py <akun>` | RPC murni (gagal: butuh sentinel) |
| `zeroui_hibrida.py <akun>` | panen token UI → kirim RPC (gagal: token sekali pakai) |
| `kuota_rpc.py` | **baca kuota zero-UI (BERHASIL)** |
| `uji_kuota.py <akun>` | kuota + generate nyata bersamaan |
| `uji5_akun.py` | uji limit 5 akun bersih |
| `hitung2.py` | hitung gambar per akun dari pustaka Images |