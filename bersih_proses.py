#!/usr/bin/env python3
"""Bersihkan proses browser yang tidak dipakai runner (pencegah beban naik).

Aman: hanya membunuh chrome/firefox/camoufox yang PPID-nya 1 (yatim, artinya
induknya sudah mati) ATAU yang lebih tua dari BATAS_JAM dan bukan milik
layanan systemd aktif. Tidak pernah menyentuh proses di dalam cgroup runner.
"""
import os
import subprocess
import time

BATAS_JAM = float(os.environ.get("BERSIH_BATAS_JAM", "6"))
CERMIN = ("chrome", "chromium", "camoufox", "firefox", "playwright")


def cgroup(pid):
    try:
        return open(f"/proc/{pid}/cgroup").read()
    except Exception:
        return ""


def ppid(pid):
    try:
        for l in open(f"/proc/{pid}/status"):
            if l.startswith("PPid:"):
                return int(l.split()[1])
    except Exception:
        pass
    return -1


def umur(pid):
    try:
        return time.time() - os.stat(f"/proc/{pid}").st_mtime
    except Exception:
        return 0


def main():
    mati = []
    for pid in [d for d in os.listdir("/proc") if d.isdigit()]:
        pid = int(pid)
        try:
            nama = open(f"/proc/{pid}/comm").read().strip().lower()
        except Exception:
            continue
        if not any(c in nama for c in CERMIN):
            continue
        # JANGAN sentuh proses di dalam layanan (cgroup .service)
        cg = cgroup(pid)
        if ".service" in cg:
            continue
        if ppid(pid) == 1 or umur(pid) > BATAS_JAM * 3600:
            mati.append((pid, nama))
    for pid, nama in mati:
        try:
            os.kill(pid, 15)
        except Exception:
            pass
    if mati:
        print(f"dibersihkan {len(mati)} proses yatim: "
              + ", ".join(f"{n}({p})" for p, n in mati[:10]))
    else:
        print("tidak ada proses yatim")


if __name__ == "__main__":
    main()
