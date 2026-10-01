# Perbaikan auto-login + Camoufox

## Hasil terukur

Akun baru `asepqds` yang sebelumnya **GAGAL TOTAL** (0 sehat dari 25) berhasil
dipulihkan dengan Camoufox:

| Akun | Sebelum | Sesudah |
|---|---|---|
| asepqds1 | needs_human (CAPTCHA) | **ok** — Vids terbuka |
| asepqds3 | needs_human | **ok** — Vids terbuka |
| asepqds4 | needs_human | **ok** — Vids terbuka |
| asepqds5 | needs_human | **ok** — Vids terbuka |
| asepqds10/13/14/15 | needs_human | **ok** — Vids terbuka |

Pool: `no_login 25` -> `ok 8` (dan naik terus saat login massal berjalan).

## AKAR MASALAH 1 — profil bercampur Chromium + Firefox

    Runner (browser_manager.py:98,308):
        pw.chromium.launch_persistent_context(
            user_data_dir="chrome_profiles/<email>",
            executable_path="/usr/bin/google-chrome-stable")

    Camoufox (camoufox_login.py, VERSI LAMA):
        profile = PROFILES / email          # FOLDER YANG SAMA

Camoufox = Firefox. Satu `user_data_dir` tidak boleh dipakai dua mesin browser:
isinya jadi bercampur (`Default/Cookies` + `Preferences` milik Chrome,
`cookies.sqlite` + `cert9.db` + `prefs.js` milik Firefox). Akibatnya **sesi tidak
terbaca oleh keduanya** -> runner melaporkan
`sesi mati (mendarat di halaman login Google)`.

Terukur: **16 profil** tercemar, **660 berkas** Firefox dipindahkan keluar.

### Perbaikan
- Camoufox memakai folder TERPISAH: `firefox_profiles/<email>`
  (Chromium tetap di `chrome_profiles/<email>` — tanpa regresi)
- `bersih_profil_ff.py` memindahkan (BUKAN menghapus) artefak Firefox dari
  profil Chromium ke `chrome_profiles/_firefox_sisa/<email>/`, sehingga bisa
  dikembalikan bila perlu.

## AKAR MASALAH 2 — klik "Saya mengerti" terlalu cepat

Halaman `accounts.google.com/v3/signin/speedbump/workspacetermsofservice`
menampilkan tombol yang **sudah terlihat & aktif**:

    tombol terlihat (2):
      - 'Saya mengerti' [BUTTON] disabled=False
      - 'Scroll ke bawah' [BUTTON] disabled=False

Tetapi bila diklik terlalu cepat (halaman belum selesai dimuat), klik tidak
berpengaruh dan login berhenti dengan
`error: masih di halaman akun (url=...workspacetermsofservic)`.

### Perbaikan (`klik_setuju_kalau_ada`)
1. **Tunggu** tombol benar-benar ada (`wait_for_selector`, waktu naik tiap putaran)
2. **Gulir** sampai dasar (tombol muncul setelah teks dibaca)
3. **Klik** lewat `get_by_role` + fallback JS
4. **Verifikasi** halaman benar-benar berpindah; kalau belum, **ulangi** (maks 6x)

Ditambah: loop penanganan persyaratan tidak lagi `break` hanya karena URL
mengandung `speedbump` — tombolnya ditangani di tempat.

## Cara pakai

    # login massal akun bermasalah (no_login/ui_retry) via Camoufox
    /root/harbor/venv/bin/python /root/api-google-flow/login_camoufox_massal.py

    # saring email tertentu
    ... login_camoufox_massal.py --pola asepqds --jeda 6

    # satu akun
    DISPLAY=:99 /root/harbor/venv/bin/python camoufox_login.py --email X --test-vids

`login_all.py` tetap: Chromium dulu -> bila `needs_human`, Camoufox otomatis
(sekarang dengan `--profile firefox_profiles/<email>` yang terpisah).

## Prasyarat
- Xvfb :99 hidup (`DISPLAY=:99`); Camoufox headed di Xvfb lebih aman
- Python Camoufox: `/root/harbor/venv/bin/python` (camoufox 0.5.6)
- Google meminta verifikasi ("ketik teks yang anda dengar atau lihat") pada
  banyak akun sekaligus -> Camoufox menembusnya

## Rollback

    cp camoufox_login.py.bak-ffsep-* camoufox_login.py
    cp camoufox_login.py.bak-sabar-* camoufox_login.py
    cp login_all.py.bak-ffsep-* login_all.py
    # kembalikan artefak Firefox:
    #   pindahkan balik chrome_profiles/_firefox_sisa/<email>/* ke chrome_profiles/<email>/

## Peringatan
Camoufox **menulis** ke `firefox_profiles/`, Chromium **menulis** ke
`chrome_profiles/`. Jangan pernah menyatukan keduanya lagi.
