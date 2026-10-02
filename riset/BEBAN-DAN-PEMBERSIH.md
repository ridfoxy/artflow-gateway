# Batas beban & pembersih proses yatim

## Masalah nyata

Login 25 akun berhenti di akun ke-6:

    !! beban 15.9 > 12.0 — berhenti; sisa 19 akun tidak diproses

Batas 12 berasal dari mesin lama (2-core / 4 GB). Mesin sekarang
**6 core / 16 GB** — load 12 masih sangat longgar.

Ditambah: proses Camoufox/Firefox tidak selalu menutup diri -> menumpuk
(terbukti: 6 proses `camoufox-bin` hidup sampai 46 menit) -> beban naik ->
login berhenti sendiri.

## Perbaikan (_login_jalan.py)

1. `LOGIN_LOAD_MAX` bawaan **24** (dulu 12), bisa ditimpa lewat env.
2. `LOGIN_RAM_MIN_MB` bawaan **800**; bila memori tersedia kurang, login
   **ditunda** (bukan dihentikan).
3. `bersihkan_proses_yatim()` dijalankan SEBELUM login mulai: membunuh
   `camoufox-bin`/`firefox` yang memakai profil kita
   (`api-google-flow` / `firefox_profiles`), SIGTERM lalu SIGKILL.
4. Saat beban/RAM tinggi, tunggu reda dulu (maks ~120s) — baru berhenti
   kalau tetap tinggi.

Terbukti: setelah pembersih + batas baru, beban **15.9 -> 2.75** dan login
berjalan melewati akun ke-9 tanpa berhenti.

## Rollback

    cp _login_jalan.py.bak-beban-* _login_jalan.py
