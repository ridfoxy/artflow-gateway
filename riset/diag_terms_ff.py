#!/usr/bin/env python3
"""DIAGNOSA: apa isi halaman persyaratan Workspace & tombol apa yang ada.

Tujuan: mengetahui PERSIS kenapa klik setuju gagal (tombol tidak ada? label
berbeda? perlu gulir lebih? perlu waktu?).
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DISPLAY", ":99")
APP = Path("/root/api-google-flow")
EMAIL = sys.argv[1] if len(sys.argv) > 1 else "asepqds1@rallse.com"

ACCOUNTS = APP / "accounts.txt"
pw = ""
for line in ACCOUNTS.read_text(errors="replace").splitlines():
    if "|" in line and line.split("|", 1)[0].strip().lower() == EMAIL.lower():
        pw = line.split("|", 1)[1].strip()
        break
print(f"akun: {EMAIL} | sandi ada: {bool(pw)}", flush=True)

from camoufox.sync_api import Camoufox
from camoufox.utils import launch_options

profil = APP / "firefox_profiles" / EMAIL
profil.mkdir(parents=True, exist_ok=True)
opts = launch_options(user_data_dir=str(profil), headless=False,
                      humanize=True, locale="id-ID", os=["windows"],
                      i_know_what_im_doing=True)
opts.pop("persistent_context", None)
opts["user_data_dir"] = str(profil)
opts["headless"] = False

SIGNIN = ("https://accounts.google.com/signin/v2/identifier"
          "?flowName=GlifWebSignIn&flowEntry=ServiceLogin")

with Camoufox(persistent_context=True, **opts) as browser:
    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto(SIGNIN, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    def isi(sel, val):
        for el in page.query_selector_all(sel):
            try:
                if el.is_visible():
                    el.click(); el.fill(""); el.fill(val); return True
            except Exception:
                continue
        return False

    def klik(label_list):
        for lab in label_list:
            try:
                el = page.get_by_role("button", name=lab, exact=False).first
                if el and el.is_visible():
                    el.click(timeout=5000); return lab
            except Exception:
                continue
        return None

    if isi('input[type="email"]', EMAIL) or isi("#identifierId", EMAIL):
        time.sleep(0.8)
        print("  klik email:", klik(["Berikutnya", "Next", "Lanjutkan", "Continue"]), flush=True)
    time.sleep(4)
    if isi('input[type="password"]', pw):
        time.sleep(0.8)
        print("  klik sandi:", klik(["Berikutnya", "Next", "Lanjutkan", "Continue"]), flush=True)

    print(f"\n  URL setelah sandi: {page.url}", flush=True)
    time.sleep(6)

    # ── DIAGNOSA HALAMAN PERSYARATAN ──
    for putaran in range(6):
        url = page.url
        print(f"\n=== PUTARAN {putaran} | url={url[:110]}", flush=True)
        try:
            txt = (page.inner_text("body") or "")
        except Exception:
            txt = ""
        print(f"  teks halaman ({len(txt)} char):", flush=True)
        print("   ", txt[:600].replace("\n", " | "), flush=True)

        # daftar SEMUA tombol yang terlihat
        try:
            tombol = page.evaluate("""() => {
                const out = [];
                const els = [...document.querySelectorAll('button,[role="button"],input[type="submit"],a[role="button"]')];
                for (const e of els) {
                    const vis = !!(e.offsetParent || e.getClientRects().length);
                    if (!vis) continue;
                    const t = (e.innerText || e.value || e.getAttribute('aria-label') || '').trim();
                    out.push({teks: t.slice(0,60), tag: e.tagName,
                              id: e.id || '', cls: (e.className||'').toString().slice(0,40),
                              disabled: !!e.disabled});
                }
                return out;
            }""")
            print(f"  tombol terlihat ({len(tombol)}):", flush=True)
            for t in tombol[:15]:
                print(f"    - '{t['teks']}' [{t['tag']}] id={t['id']} disabled={t['disabled']}", flush=True)
        except Exception as e:
            print(f"  gagal baca tombol: {str(e)[:80]}", flush=True)

        # cek apakah perlu gulir
        try:
            sk = page.evaluate("""() => {
                const out = [];
                const all = [...document.querySelectorAll('div,section,main,article')];
                for (const e of all) {
                    if (e.scrollHeight > e.clientHeight + 30)
                        out.push({tag:e.tagName, cls:(e.className||'').toString().slice(0,40),
                                  sh: e.scrollHeight, ch: e.clientHeight});
                }
                return {bisa_digulir: out.slice(0,5),
                        doc: {sh: document.body.scrollHeight, ch: window.innerHeight,
                              y: window.scrollY}};
            }""")
            print(f"  status gulir: {sk}", flush=True)
        except Exception as e:
            print(f"  gagal cek gulir: {str(e)[:70]}", flush=True)

        # coba gulir + klik
        try:
            page.evaluate("""() => {
                const all = [...document.querySelectorAll('div,section,main,article')];
                for (const e of all) {
                    if (e.scrollHeight > e.clientHeight + 20) e.scrollTop = e.scrollHeight;
                }
                window.scrollTo(0, document.body.scrollHeight);
                const d = document.scrollingElement; if (d) d.scrollTop = d.scrollHeight;
            }""")
        except Exception:
            pass
        time.sleep(1.5)
        hasil = klik(["Saya mengerti", "Saya setuju", "Setuju", "Terima", "Lanjutkan",
                      "Berikutnya", "I understand", "I agree", "Accept", "Continue", "Next"])
        print(f"  -> klik: {hasil}", flush=True)
        if not hasil:
            try:
                got = page.evaluate("""() => {
                    const want = ['mengerti','setuju','terima','lanjutkan','berikutnya',
                                  'understand','agree','accept','continue','next'];
                    for (const e of document.querySelectorAll('button,[role="button"],input[type="submit"]')) {
                        const vis = !!(e.offsetParent || e.getClientRects().length);
                        if (!vis || e.disabled) continue;
                        const t = (e.innerText||e.value||'').toLowerCase();
                        if (t && want.some(w => t.includes(w))) { e.click(); return t.slice(0,40); }
                    }
                    return null;
                }""")
                print(f"  -> klik JS: {got}", flush=True)
            except Exception as e:
                print(f"  -> klik JS gagal: {str(e)[:70]}", flush=True)
        time.sleep(4)
        if "accounts.google.com" not in page.url:
            print(f"  ✅ KELUAR dari halaman akun -> {page.url[:90]}", flush=True)
            break

    print(f"\n=== AKHIR: url={page.url[:120]}", flush=True)
    try:
        page.screenshot(path="/tmp/diag_terms.png")
        print("  screenshot: /tmp/diag_terms.png", flush=True)
    except Exception:
        pass