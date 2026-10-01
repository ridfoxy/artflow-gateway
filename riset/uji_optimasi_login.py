#!/usr/bin/env python3
"""Uji jalur persyaratan dengan MOCK page — versi diperbaiki.

Perbaikan dari versi pertama:
  - MockEl punya is_visible() (dibutuhkan _present_now; tanpa itu jalur
    cek-DOM selalu dianggap gagal sehingga UJI 1 tidak menguji apa pun)
  - state LOGIN_* direset tiap uji (versi lama bocor: UJI 4 menyetel TOL=0
    sehingga UJI 5 salah lapor)
"""
import sys
import time

sys.path.insert(0, "/root/api-google-flow")
import gsuite_login as G


def reset():
    G.LOGIN_CEPAT = True
    G.LOGIN_SEL_MS = 1200
    G.LOGIN_POLL = 0.5
    G.LOGIN_GULIR_TOL = 5


class MockEl:
    def __init__(self, bisa=True):
        self.bisa = bisa

    def is_visible(self):
        return self.bisa

    def click(self):
        if not self.bisa:
            raise RuntimeError("klik gagal")


class MockPage:
    def __init__(self, ada_dom=True, ada_selector=True, klik_bisa=True,
                 gulir_naik=0):
        self.ada_dom, self.ada_selector = ada_dom, ada_selector
        self.klik_bisa = klik_bisa
        self.gulir_naik = gulir_naik
        self.pos = 0
        self.diklik_dom = []      # lewat page.click (jalur cek-DOM)
        self.diklik_el = []       # lewat el.click (jalur selector)
        self.url = ("https://accounts.google.com/v3/signin/speedbump/"
                    "workspacetermsofservice")

    # --- bagian yang dipakai _present_now ---
    def query_selector(self, sel):
        return MockEl(self.klik_bisa) if self.ada_dom else None

    # --- jalur cek-DOM ---
    def click(self, sel, timeout=None):
        if not self.klik_bisa:
            raise RuntimeError("klik langsung gagal")
        self.diklik_dom.append(sel)

    # --- jalur lama ---
    def wait_for_selector(self, sel, timeout=None, state=None):
        if self.ada_selector:
            el = MockEl(self.klik_bisa)
            el.click = lambda: self.diklik_el.append(sel)
            return el
        raise TimeoutError("selector tidak ada")

    def evaluate(self, js):
        if "scrollTop" in js:
            self.pos += self.gulir_naik
            return self.pos
        return None

    def keyboard(self):
        return self

    def press(self, k):
        pass

    def mouse(self):
        return self

    def wheel(self, x, y):
        pass


def uji(nama, fn):
    reset()
    t0 = time.time()
    hasil = fn()
    dt = time.time() - t0
    print(f"  {nama:44} {str(hasil):>16}  {dt:5.2f}s")
    return hasil, dt


print("=== UJI JALUR PERSYARATAN (mock) ===")
p = MockPage(ada_dom=True)
h, dt = uji("1. tombol ada di DOM -> jalur cek-DOM", lambda: G.handle_workspace_terms(p, log_fn=lambda s: None))
print(f"       diklik via cek-DOM: {p.diklik_dom[:1]}  "
      f"-> {'BENAR' if h and p.diklik_dom else 'SALAH'}")

p = MockPage(ada_dom=False, ada_selector=True)
h, dt = uji("2. cek-DOM gagal -> jatuh ke selector", lambda: G.handle_workspace_terms(p, log_fn=lambda s: None))
print(f"       diklik via selector: {len(p.diklik_el)} -> "
      f"{'BENAR (tetap jalan)' if h else 'SALAH'}")

p = MockPage(ada_dom=False, ada_selector=False)
h, dt = uji("3. tombol tidak ada sama sekali", lambda: G.handle_workspace_terms(p, log_fn=lambda s: None))
print(f"       -> {'BENAR (lapor gagal, tidak menggantung)' if not h else 'SALAH'}")

p = MockPage(ada_dom=True, klik_bisa=False, ada_selector=True)
h, dt = uji("4. cek-DOM bisa tapi klik gagal", lambda: G.handle_workspace_terms(p, log_fn=lambda s: None))
print(f"       -> {'BENAR (mundur ke selector)' if h else 'PERIKSA'}")

print("\n=== UJI TOLERANSI GULIR (state direset) ===")
for nama, naik in (("posisi tetap (0px)", 0),
                   ("lazy-load +3px", 3),
                   ("naik +5px tepat di batas", 5),
                   ("terus naik +50px", 50)):
    reset()
    p = MockPage(gulir_naik=naik)
    t0 = time.time()
    ok = G.gulir_sampai_bawah(p, log_fn=lambda s: None)
    print(f"  {nama:28} {'berhenti' if ok else 'habis 25 langkah':>18}  "
          f"{time.time()-t0:5.1f}s")

print("\n=== UJI MODE LAMA (LOGIN_CEPAT=0) ===")
G.LOGIN_CEPAT = False
G.LOGIN_SEL_MS = 3500
G.LOGIN_GULIR_TOL = 0
p = MockPage(ada_dom=True)
h, dt = uji("5. mode lama: cek-DOM tidak dipakai", lambda: G.handle_workspace_terms(p, log_fn=lambda s: None))
print(f"       diklik via cek-DOM (harus KOSONG): {p.diklik_dom} -> "
      f"{'BENAR' if not p.diklik_dom else 'SALAH'}")
reset()

print("\n=== NILAI AKTIF SEKARANG ===")
print(f"  LOGIN_CEPAT={G.LOGIN_CEPAT} SEL_MS={G.LOGIN_SEL_MS} "
      f"POLL={G.LOGIN_POLL} GULIR_TOL={G.LOGIN_GULIR_TOL}")