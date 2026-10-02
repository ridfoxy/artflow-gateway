# Tiga pengaman sistem

## 1. Runner menghormati bukti login segar

**Masalah:** 25 akun berhasil login (tercatat `ok`), lalu runner Chromium gagal
membaca profil Firefox dan menandai `no_login` — panel menampilkan "akun sehat 4"
padahal 25.

**Perbaikan** (`vids_runner.py`): `mark_dead_session()` memeriksa
`data/login_results.jsonl` lebih dulu. Bila ada catatan `ok` yang lebih baru
dari `VIDS_LOGIN_BUKTI_JAM` (bawaan 6 jam):

    state  = 'ui_retry'          (BUKAN no_login)
    note   = 'istirahat 10m (login baru sukses, browser gagal baca sesi)'
    until  = now + VIDS_ISTIRAHAT_MENIT (bawaan 10 menit)

Tanpa bukti segar -> perilaku lama (`no_login`, 12 jam).

Terbukti: `bukti_login_segar(asepqds1) = True`, `(denijfx6) = False`.

## 2. Batas waktu keras untuk Camoufox

**Masalah:** Camoufox tidak menutup diri -> loop login berhenti sebelum akun
terakhir (terbukti: 25 akun berhenti di akun ke-7).

**Perbaikan** (`_login_jalan.py`): `subprocess.run` -> `Popen` + pembaca baris
demi baris + `bunuh_paksa()` yang memakai `os.killpg` (SIGTERM lalu SIGKILL)
sehingga SELURUH grup proses mati, bukan hanya induknya. Berlaku untuk jalur
Camoufox maupun Chromium.

## 3. Login dipisah dari gateway (cgroup sendiri)

**Masalah:** `Popen(start_new_session=True)` TIDAK cukup — proses tetap di
dalam cgroup `artflow-gateway-local.service`, sehingga
`systemctl restart artflow-gateway-local` membunuh login yang sedang berjalan.

**Perbaikan** (`app.py`): login dijalankan lewat `systemd-run --collect --unit
artflow-login-job` -> proses berada di cgroup SENDIRI.

Terbukti: sebelum restart `artflow-login-job: active`; SESUDAH restart gateway,
`artflow-login-job` tetap **active** dan proses `_login_jalan.py` tetap berjalan.

Matikan bila perlu: `LOGIN_VIA_SYSTEMD=0` (kembali ke Popen biasa).

## Angka kunci

| Variabel | Bawaan | Arti |
|---|---|---|
| `VIDS_LOGIN_BUKTI_JAM` | 6 | berapa lama catatan login `ok` dianggap sah |
| `VIDS_ISTIRAHAT_MENIT` | 10 | istirahat singkat bila browser gagal baca sesi |
| `VIDS_DEAD_SESSION_HOURS` | 12 | lama `no_login` bila TIDAK ada bukti |
| `LOGIN_VIA_SYSTEMD` | 1 | jalankan login di cgroup sendiri |

## Rollback

    cp vids_runner.py.bak-sempurna-* vids_runner.py
    cp _login_jalan.py.bak-sempurna-* _login_jalan.py
    cp app.py.bak-systemdrun-* app.py
