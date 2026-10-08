#!/usr/bin/env python3
"""SWAP + MATIKAN VNC + MATIKAN BROWSER FLOW (sementara)

Urutan dan alasannya
--------------------
1. SWAP 495 MB -> ~4,5 GB. Sekarang swap terpakai penuh (492/495 MB), ini
   pemicu paling mendesak. Tidak butuh restart apa pun.

2. MATIKAN VNC (x11vnc + websockify). Xvfb :99 TIDAK boleh dimatikan — semua
   browser (Flow/Vids/GPT Image) memakai layar itu; mematikannya = semua
   browser mati.

3. MATIKAN BROWSER FLOW SAJA (profil listbalck31@gmail.com + anak-anaknya,
   hidup 22 jam, ~2,3 GB). Browser Vids & GPT Image TIDAK disentuh.

4. NONAKTIFKAN xvfb.service — unit terpisah yang GAGAL BERULANG karena
   Xvfb :99 sudah dipegang gateway. Tidak pernah bisa berhasil, hanya
   memutar restart + memenuhi jurnal.

CATATAN JUJUR
-------------
VNC dan browser Flow AKAN HIDUP LAGI kalau ada job Flow masuk, karena
`process_job` di app.py memanggil `start_remote_browser()` (yang juga
menjalankan VNC) saat browser kosong. Mati tidak berarti tetap mati.

  Balik VNC/Flow sekarang : curl -X POST http://127.0.0.1:8088/v1/browser/start
  Balik swap              : swapoff /swapfile2 && rm /swapfile2 (+ hapus fstab)
  Balik xvfb.service      : systemctl enable --now xvfb.service
"""
import os
import re
import subprocess
import sys
import time

BARIS = "=" * 68
SWAPFILE = "/swapfile2"
UKURAN_GB = 4
# swappiness 60 -> 20: kurangi thrash, tapi masih memakai swap sebelum OOM
SWAPPINESS = 20


def sh(cmd, t=600):
    r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                       timeout=t)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def ram():
    _, o = sh("free -m | awk 'NR==2{print $7} NR==3{print $2\" \"$3}'")
    p = o.split("\n")
    return f"tersedia {p[0]} MB | swap {p[1] if len(p) > 1 else '?'} MB"


def bagian(n, judul):
    print(f"\n{BARIS}\n{n}. {judul}\n{BARIS}")


def main():
    print(BARIS)
    print("SWAP + MATIKAN VNC + MATIKAN FLOW")
    print(BARIS)
    print(f"\nSEBELUM: {ram()}")

    # ── 1. SWAP ─────────────────────────────────────────────────────────
    bagian(1, f"SWAP ({UKURAN_GB} GB)")
    if os.path.exists(SWAPFILE):
        print(f"   ℹ️  {SWAPFILE} sudah ada, dilewati")
    else:
        rc, o = sh(f"fallocate -l {UKURAN_GB}G {SWAPFILE} || "
                   f"dd if=/dev/zero of={SWAPFILE} bs=1M count={UKURAN_GB*1024} "
                   f"status=none")
        if rc != 0:
            print(f"   ❌ gagal membuat berkas: {o[:150]}"); return 1
        sh(f"chmod 600 {SWAPFILE}")
        rc, o = sh(f"mkswap {SWAPFILE} 2>&1 | tail -2")
        print(f"   mkswap: {o[:100]}")
        # prioritas lebih tinggi dari partisi lama (-2) supaya dipakai lebih dulu
        rc, o = sh(f"swapon -p 100 {SWAPFILE}")
        if rc != 0:
            print(f"   ❌ swapon gagal: {o[:150]}"); return 1
        print(f"   ✅ aktif: {SWAPFILE}")

    # swappiness
    lama = open("/proc/sys/vm/swappiness").read().strip()
    if lama != str(SWAPPINESS):
        sh(f"sysctl -w vm.swappiness={SWAPPINESS} >/dev/null")
        f = "/etc/sysctl.d/99-swappiness.conf"
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(f"vm.swappiness={SWAPPINESS}\n")
        print(f"   ✅ swappiness {lama} → {SWAPPINESS} (permanen di {f})")

    # fstab supaya bertahan setelah reboot
    try:
        fstab = open("/etc/fstab", encoding="utf-8").read()
        if SWAPFILE not in fstab:
            with open("/etc/fstab", "a", encoding="utf-8") as fh:
                fh.write(f"{SWAPFILE} none swap sw 0 0\n")
            print("   ✅ ditambahkan ke /etc/fstab (bertahan setelah reboot)")
    except Exception as e:
        print(f"   ⚠️  fstab tidak bisa diubah: {e}")

    _, o = sh("swapon --show")
    print("   swap sekarang:\n" + "\n".join("     " + l for l in o.split("\n")))

    # ── 2. MATIKAN VNC ──────────────────────────────────────────────────
    bagian(2, "MATIKAN VNC (Xvfb tetap hidup)")
    _, o = sh("ps -eo pid,comm,args | grep -E 'x11vnc|websockify' "
              "| grep -v grep | awk '{print $1\" \"$2}'")
    if o:
        print("   ditemukan:\n" + "\n".join("     " + l for l in o.split("\n")))
        sh("pkill -f 'x11vnc -display'; pkill -f 'websockify --web'")
        time.sleep(2)
        _, sisa = sh("pgrep -fc 'x11vnc|websockify' || echo 0")
        print(f"   setelah dimatikan, sisa proses: {sisa.strip()}")
    else:
        print("   (tidak ada yang jalan)")

    # pastikan Xvfb masih hidup — kalau ini mati, semua browser mati
    _, xv = sh("pgrep -f 'Xvfb :99' | head -1")
    print(f"   ✅ Xvfb :99 tetap hidup (pid {xv.strip() or 'TIDAK ADA!'})")
    if not xv.strip():
        print("   ❌ BAHAYA: Xvfb tidak ada; browser akan gagal. Perlu dicek.")

    # ── 3. MATIKAN BROWSER FLOW SAJA ────────────────────────────────────
    bagian(3, "MATIKAN BROWSER FLOW SAJA (Vids & GPT Image tidak disentuh)")
    # cari pid kerangka dengan profile listbalck31 (bukan anak-anaknya)
    _, out = sh("ps -eo pid,args | grep camoufox-bin | grep -v grep")
    induk = []
    for ln in out.split("\n"):
        if "listbalck31@gmail.com" in ln and "-no-remote" in ln:
            induk.append(ln.split()[0])
    if not induk:
        print("   (browser Flow tidak jalan)")
    else:
        total = 0
        for p in induk:
            # total RSS satu keluarga: proses ini + semua keturunannya
            _, o2 = sh(f"ps -o rss= --ppid {p} --pid {p} 2>/dev/null "
                       f"| awk '{{s+=$1}} END{{print s+0}}'")
            _, o3 = sh(f"pstree -p {p} 2>/dev/null | grep -oE '\\([0-9]+\\)' "
                       f"| tr -d '()' | tr '\\n' ',' ")
            pids = [x for x in o3.strip().strip(",").split(",") if x.isdigit()]
            rss = 0
            for q in pids or [p]:
                _, r = sh(f"ps -o rss= -p {q} 2>/dev/null | tr -d ' '")
                if r.isdigit():
                    rss += int(r)
            total += rss
            print(f"   membunuh keluarga pid={p} ({rss/1024:.0f} MB, "
                  f"{len(pids) or 1} proses)")
            for q in (pids or [p]):
                sh(f"kill -TERM {q} 2>/dev/null")
        time.sleep(3)
        # sisa yang membandel
        for q in pids or []:
            _, masih = sh(f"ps -p {q} >/dev/null 2>&1 && echo ada || echo tidak")
            if "ada" in masih:
                sh(f"kill -KILL {q} 2>/dev/null")
        print(f"   ✅ dibebaskan ≈ {total/1024:.0f} MB")

    # ── 4. xvfb.service ─────────────────────────────────────────────────
    bagian(4, "NONAKTIFKAN xvfb.service (gagal berulang, Xvfb dipegang gateway)")
    _, st = sh("systemctl is-active xvfb.service")
    print(f"   keadaan sebelum: {st}")
    if st != "inactive":
        rc, o = sh("systemctl disable --now xvfb.service 2>&1 | tail -3")
        print(f"   {o[:200]}")
    _, st2 = sh("systemctl is-active xvfb.service")
    print(f"   keadaan sesudah: {st2}")

    # ── 5. HASIL ────────────────────────────────────────────────────────
    bagian(5, "HASIL")
    print(f"   SESUDAH: {ram()}")
    print()
    _, o = sh("for u in artflow-vids-runner-local artflow-gateway-local "
              "artflow-gptimage-daemon artflow-gen-bot artflow-web; do "
              "printf '  %-30s %s\\n' $u $(systemctl is-active $u); done")
    print(o)
    print()
    _, o = sh("ps -eo rss,comm --sort=-rss | head -8 "
              "| awk '{printf \"  %7.0f MB  %s\\n\", $1/1024, $2}'")
    print("   pemakai RAM teratas sekarang:")
    print(o)

    print(f"""
{BARIS}
CARA BALIK
{BARIS}
  VNC + Flow hidup lagi : curl -X POST http://127.0.0.1:8088/v1/browser/start
  xvfb.service          : systemctl enable --now xvfb.service
  swap                  : swapoff {SWAPFILE} && rm {SWAPFILE}
                          (lalu hapus baris {SWAPFILE} di /etc/fstab)
  swappiness            : sysctl -w vm.swappiness=60

  PENTING: VNC & browser Flow akan hidup SENDIRI kalau ada job Flow masuk,
  karena process_job memanggil start_remote_browser().""")
    return 0


if __name__ == "__main__":
    sys.exit(main())