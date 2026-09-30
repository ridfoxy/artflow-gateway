#!/usr/bin/env python3
"""Pastikan Xvfb :99 hidup sebelum login (kalau tidak, Chrome gagal:
"Missing X server or $DISPLAY").

Kenapa perlu: Xvfb dijalankan runner sebagai anak prosesnya. Bila runner
direstart/dimatikan, Xvfb ikut mati — dan login yang dipanggil terpisah
(dari endpoint gateway) tidak punya display.
"""
import os
import subprocess
import time
from pathlib import Path

DISPLAY = ":99"


def hidup():
    if Path("/tmp/.X11-unix/X99").exists():
        return True
    r = subprocess.run(["pgrep", "-f", f"Xvfb {DISPLAY}"], capture_output=True)
    return r.returncode == 0


def pastikan():
    if hidup():
        print(f"  Xvfb {DISPLAY} sudah jalan")
        return True
    print(f"  Xvfb {DISPLAY} mati — menjalankan...")
    try:
        subprocess.Popen(
            ["Xvfb", DISPLAY, "-screen", "0", "1280x800x24", "-ac",
             "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except FileNotFoundError:
        print("  !! perintah Xvfb tidak ada")
        return False
    for _ in range(20):
        time.sleep(0.5)
        if hidup():
            print(f"  Xvfb {DISPLAY} siap")
            os.environ["DISPLAY"] = DISPLAY
            return True
    print("  !! Xvfb gagal start")
    return False


if __name__ == "__main__":
    ok = pastikan()
    print("hasil:", ok)
    raise SystemExit(0 if ok else 1)