# MIGRASI GATEWAY 167 → 172 (lokasi) + domain api.artflowgen.com

Status: **SELESAI & BERJALAN** — 30 Sep 2026
Rollback: lihat bagian bawah.

## Ringkas
Gateway ArtFlow (Flow + Vids) dipindah dari 104.64.15.167 (2 core / 4 GB)
ke **172.104.60.106** (6 core / 16 GB / 276 GB). Semua pemakai kini menunjuk
**https://api.artflowgen.com**, bukan IP — jadi pindah hosting berikutnya tidak
perlu ubah kode lagi.

## Arsitektur sekarang
```
Bot Telegram (172, /root/bottele)
   │  API_BASE_URL = https://api.artflowgen.com
   ▼
Cloudflare (proxied, oranye)
   │  A api.artflowgen.com -> 172.104.60.106
   ▼
nginx 172 :443  (sites-enabled/api-artflowgen)
   │  proxy_pass http://127.0.0.1:8088
   ▼
Gateway LOKAL 172.104.60.106 :8088   (artflow-gateway-local.service)
   + Vids Runner lokal 172            (artflow-vids-runner-local.service)
        │
        ▼
   Google Flow / Google Vids  -> MP4
```
Web utama (artflowgen.com, www) TETAP dilayani nginx blok lama — tidak diubah.

## Kenapa lewat nginx
Cloudflare (proxied) hanya meneruskan ke port terbatas (80, 443, 8080, 8443,
...); **8088 tidak didukung**. Karena itu `api.artflowgen.com:443` mendarat di
nginx lalu diteruskan ke `127.0.0.1:8088`. Bonus: gateway tidak dibuka langsung
ke publik.

## Unit systemd baru (di 172)
- `/etc/systemd/system/artflow-gateway-local.service` — uvicorn app:app `0.0.0.0:8088`
- `/etc/systemd/system/artflow-vids-runner-local.service` — `vids_runner.py`

Catatan: unit ini **disabled** (tidak autostart). Nyalakan manual:
`systemctl start artflow-gateway-local artflow-vids-runner-local`.
(Untuk permanen setelah masa observasi: `systemctl enable ...`.)

## Berkas pemakai yang diubah
| Berkas | Sebelum | Sesudah | Backup |
|---|---|---|---|
| `/root/bottele/config.py` | `http://104.64.15.167:8088` | `https://api.artflowgen.com` | `.bak-apidomain-1790740479` |
| `flow_server/remote_gateway.py` | `http://104.64.15.167:8088` | `https://api.artflowgen.com` | `.bak-apidomain-1790740507` |
| `/etc/nginx/sites-available/api-artflowgen` | (baru) | vhost api | `.bak-*` |

## Temuan penting
- **Sesi akun BERTAHAN setelah migrasi.** Kekhawatiran utama (profil Chrome
  terikat perangkat) tidak terjadi: pilot 3 job sukses memakai akun yang sudah
  login di mesin lama. Diuji: teks 3.461.052 B, gambar 2.408.198 B, portrait
  3.368.587 B — semua `ftypisom`.
- **urllib ditolak 403 oleh Cloudflare, `requests`/curl lolos 200.** Bot memakai
  `requests`, jadi aman. Kalau ada skrip yang memakai `urllib` ke domain ini,
  harus diganti ke `requests` atau diberi User-Agent browser.

## Rollback (urutan disarankan)
1. **Pemakai kembali ke IP lama** (paling cepat):
   ```
   cd /root/bottele && cp config.py.bak-apidomain-1790740479 config.py
   cd /root/af_project/flow-agent/flow-agent/flow_server && \
     cp remote_gateway.py.bak-apidomain-1790740507 remote_gateway.py
   systemctl restart artflow-gen-bot flow-agent
   ```
   Gateway 167 masih hidup dan utuh → langsung melayani lagi.
2. **Matikan gateway lokal** (jangan dihapus dulu):
   ```
   systemctl stop artflow-gateway-local artflow-vids-runner-local
   ```
3. **Batalkan DNS** (kalau perlu):
   ```
   python3 /root/cf_api_dns.py rollback 104.64.15.167
   # catatan id record: /root/cf_api_dns_rollback.txt
   ```
4. **Batalkan nginx vhost**:
   ```
   rm /etc/nginx/sites-enabled/api-artflowgen /etc/nginx/sites-available/api-artflowgen
   systemctl reload nginx
   ```

## Yang TIDAK dilakukan (sengaja)
- Gateway 167 tidak dihapus/dimatikan → jalan mundur.
- nginx blok web utama tidak disentuh.
- Unit lokal belum di-enable (menunggu masa observasi).
