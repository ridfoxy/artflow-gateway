#!/usr/bin/env python3
"""PASANG BATAS MEMORI per layanan ArtFlow (Tahap 1.1).

DASAR
-----
Semua layanan masih `MemoryMax=infinity`. Puncak RAM gabungan pernah 18,9 GB
padahal RAM hanya 16 GB -> risiko OOM (server mati saat ramai).

PENTING: rencana lama (RENCANA-RINGAN-CEPAT-STABIL.md) mengusulkan
gateway 7 GB + vids 7 GB + daemon 3 GB + bot 2 GB + omniroute 2 GB.
Itu BERMASALAH: kalau semuanya menyentuh batas sekaligus, jumlahnya 21 GB
alias tetap OOM. Skrip ini memakai anggaran yang muat.

Angka di bawah diambil dari pemakaian NYATA saat dipasang:
  vids 5846 MB | gateway 2329 MB | daemon 2279 MB | bot 378 MB | web 123 MB
Ditambah ruang tumbuh, tanpa melewati RAM+swap.

CARA KERJA
----------
  MemoryHigh = mulai ditekan (reclaim) -> layanan melambat, TIDAK dibunuh
  MemoryMax  = batas keras -> hanya kalau benar-benar kabur

MemoryHigh sengaja dipasang lebih dulu sebagai pengaman utama, karena kalau
MemoryMax saja yang dipasang, layanan langsung dibunuh saat menyentuhnya.
"""
import os
import subprocess
import sys

UNIT_DIR = "/etc/systemd/system"

# unit -> (MemoryHigh, MemoryMax, catatan)
#
# MemoryHigh HARUS di atas pemakaian nyata sekarang, kalau tidak layanan
# langsung ditekan begitu dipasang.
#   sekarang: vids 5846 | gateway 2329 | daemon 2279 | bot 378 | web 123 (MB)
# Jumlah High = 13,3 GB < RAM 15,6 GB  -> penekanan terjadi sebelum OOM
# Jumlah Max  = 15,6 GB ≈ RAM          -> plafon keras per layanan
BATAS = {
    "artflow-vids-runner-local": ("6656M", "7168M", "pemakan terbesar (browser)"),
    "artflow-gateway-local": ("3072M", "4096M", "termasuk browser anak"),
    "artflow-gptimage-daemon": ("2560M", "3072M", "browser ChatGPT hangat"),
    "artflow-gen-bot": ("768M", "1024M", "bot Telegram"),
    "artflow-web": ("512M", "640M", "web Next.js"),
}


def sh(cmd, t=180):
    r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                       timeout=t)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def baca(unit, prop):
    _, out = sh(f"systemctl show {unit} -p {prop} --value")
    return out


def main():
    print("=" * 68)
    print("PASANG BATAS MEMORI  (Tahap 1.1)")
    print("=" * 68)

    # ── 0. pemakaian sekarang, sebagai bukti dasar ──────────────────────
    print("\n[0] Pemakaian saat ini (dasar penetapan angka)")
    for u in BATAS:
        cur = baca(u, "MemoryCurrent")
        try:
            mb = int(cur) // 1024 // 1024
        except Exception:
            mb = -1
        print(f"   {u:30s} {mb:6d} MB")
    _, free_out = sh("free -m | awk 'NR==2{print $2\" \"$3\" \"$7} NR==3{print $2\" \"$3}'")
    print("   RAM/Swap:", free_out.replace("\n", " | "))

    # ── 1. tulis drop-in ────────────────────────────────────────────────
    print("\n[1] Tulis drop-in (10-memori.conf)")
    dibuat = []
    for u, (high, mx, note) in BATAS.items():
        if not os.path.exists(f"{UNIT_DIR}/{u}.service"):
            print(f"   ❌ {u}: unit tidak ada, dilewati")
            return 1
        d = f"{UNIT_DIR}/{u}.service.d"
        os.makedirs(d, exist_ok=True)
        f = f"{d}/10-memori.conf"
        if os.path.exists(f):
            print(f"   ℹ️  {u}: sudah ada, dilewati")
            continue
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(f"""# Batas memori — dipasang otomatis (Tahap 1.1)
# High dulu (tekan/reclaim), Max sebagai batas keras.
# Angka dipilih dari pemakaian nyata; {note}.
[Service]
MemoryHigh={high}
MemoryMax={mx}
""")
        dibuat.append(f)
        print(f"   ✅ {u}: High={high} Max={mx}  ({note})")

    if not dibuat:
        print("   (tidak ada yang baru)")

    # ── 2. terapkan ─────────────────────────────────────────────────────
    print("\n[2] Terapkan (daemon-reload, TANPA restart)")
    rc, out = sh("systemctl daemon-reload")
    print("   reload:", "OK" if rc == 0 else f"GAGAL {out[:120]}")

    # ── 3. verifikasi ───────────────────────────────────────────────────
    print("\n[3] Verifikasi nilai terpasang")
    gagal = []
    for u, (high, mx, _) in BATAS.items():
        h = baca(u, "MemoryHigh")
        m = baca(u, "MemoryMax")
        aktif = baca(u, "ActiveState")
        hb = int(h) // 1024 // 1024 if h.isdigit() else h
        mb_ = int(m) // 1024 // 1024 if m.isdigit() else m
        ok = (str(hb) != "infinity") and (str(mb_) != "infinity")
        if not ok:
            gagal.append(u)
        print(f"   {'✅' if ok else '❌'} {u:30s} High={hb} MB  Max={mb_} MB  "
              f"state={aktif}")

    print("\n[4] Jumlah anggaran maksimum")
    total = sum(int(v[1].rstrip("M")) for v in BATAS.values())
    rc, ram_swap = sh(
        "free -m | awk 'NR==2{printf \"%s \", $2} NR==3{print $2}'")
    print(f"   Total MemoryMax   = {total} MB ({total / 1024:.1f} GB)")
    print(f"   RAM + Swap (MB)   = {ram_swap.strip()}")

    if gagal:
        print(f"\n❌ GAGAL pada: {gagal}")
        return 1
    print("\n✅ Selesai. Layanan TIDAK direstart, jadi tidak mengganggu produksi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())