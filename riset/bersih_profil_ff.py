#!/usr/bin/env python3
"""Bersihkan artefak FIREFOX dari profil Chromium (agar Chrome bisa dipakai lagi).

Konteks: Camoufox (Firefox) pernah menulis ke folder profil yang sama dengan
Chromium, sehingga folder berisi DUA format. Chromium membaca folder itu sebagai
profil rusak -> sesi tidak terbaca -> "sesi mati (mendarat di halaman login)".

Yang dilakukan:
  - pindahkan (BUKAN hapus) semua berkas/folder khas Firefox ke
    chrome_profiles/_firefox_sisa/<email>/  supaya bisa diperiksa/dikembalikan
  - sisakan berkas khas Chromium (Default/, Local State, dsb.)

Aman: tidak ada yang dihapus; semua dipindah.
"""
import json
import os
import shutil
import time
from pathlib import Path

BASE = Path("/root/api-google-flow/chrome_profiles")
SISA = BASE / "_firefox_sisa"

# Nama khas Firefox yang TIDAK boleh ada di profil Chromium.
KHAS_FF = {
    "cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm",
    "cert9.db", "key4.db", "pkcs11.txt", "prefs.js", "times.json",
    "compatibility.ini", "addonStartup.json.lz4", "containers.json",
    "content-prefs.sqlite", "permissions.sqlite", "formhistory.sqlite",
    "places.sqlite", "favicons.sqlite", "webappsstore.sqlite",
    "sessionstore.jsonlz4", "sessionstore-backups", "chrome_debug.log",
    "AlternateServices.bin", "cert_override.txt", "SiteSecurityServiceState.bin",
    "bounce-tracking-protection.sqlite", "domain_to_categories.sqlite",
    "domain_to_categories.sqlite-journal", "datareporting",
    "broadcast-listeners.json", "bookmarkbackups", "cache2", "startupCache",
    "storage", "extensions.json", "extension-preferences.json",
    "xulstore.json", "search.json.mozlz4", "shield-preference-experiments.json",
    "protections.sqlite", "safebrowsing", "security_state",
    "signedInUser.json", "handlers.json", "localstore.rdf",
}
# Cocokkan juga pola umum
POLA_FF = ("sqlite", "sqlite-wal", "sqlite-shm")

print("=== 1. CARI PROFIL TERCEMAR ===")
rusak = []
for d in sorted(BASE.iterdir()):
    if not d.is_dir() or d.name.startswith("_"):
        continue
    ada_ff = (d / "cookies.sqlite").exists() or (d / "cert9.db").exists() \
        or (d / "prefs.js").exists()
    if ada_ff:
        rusak.append(d)
print(f"  profil tercemar: {len(rusak)}")

rincian = []
total_dipindah = 0
for d in rusak:
    tujuan = SISA / d.name
    tujuan.mkdir(parents=True, exist_ok=True)
    pindah_dari = []
    for f in sorted(d.iterdir()):
        nama = f.name
        # JANGAN sentuh berkas khas Chromium
        if nama in ("Default", "Local State", "First Run", "Last Version",
                    "Variations", "VariationsSeedV2", "GPUPersistentCache",
                    "segmentation_platform", "Safe Browsing",
                    "BrowserMetrics-spare.pma", "OptimizationGuideModels",
                    "component_crx_cache", "extensions_crx_cache",
                    "NativeMessagingHosts", "first_party_sets.db",
                    "first_party_sets.db-journal", "Dictionaries",
                    "ShaderCache", "GrShaderCache", "GraphiteDawnCache",
                    "Crashpad", "SingletonCookie", "SingletonLock",
                    "SingletonSocket"):
            continue
        if (nama in KHAS_FF
                or nama.endswith(".sqlite")
                or nama.endswith(".sqlite-wal")
                or nama.endswith(".sqlite-shm")
                or nama.endswith(".mozlz4")
                or nama.endswith(".jsonlz4")
                or nama in ("cache2",)):
            try:
                shutil.move(str(f), str(tujuan / nama))
                pindah_dari.append(nama)
            except Exception as e:
                print(f"    ! gagal pindah {nama}: {str(e)[:60]}")
    if pindah_dari:
        total_dipindah += len(pindah_dari)
        rincian.append({"email": d.name, "dipindah": pindah_dari})
        print(f"  {d.name:28} -> {len(pindah_dari)} berkas Firefox dipindah")

print(f"\n  total berkas dipindah: {total_dipindah}")
print(f"  disimpan di: {SISA}")

print("\n=== 2. VERIFIKASI profil sekarang ===")
bersih = 0
for d in rusak:
    sisa_ff = [f.name for f in d.iterdir()
               if f.name.endswith(".sqlite") or f.name == "cert9.db"
               or f.name == "prefs.js"]
    ada_default = (d / "Default").exists()
    if not sisa_ff and ada_default:
        bersih += 1
    else:
        print(f"  {d.name:28} sisa FF: {sisa_ff[:4]} | Default={ada_default}")
print(f"  profil bersih & punya struktur Chromium: {bersih}/{len(rusak)}")

SISA.mkdir(parents=True, exist_ok=True)
(SISA / "rincian.json").write_text(json.dumps(rincian, indent=2, ensure_ascii=False))
print(f"\n  catatan: {SISA/'rincian.json'}")
print("\n  KEMBALIKAN bila perlu: pindahkan balik isi _firefox_sisa/<email> ke <email>/")