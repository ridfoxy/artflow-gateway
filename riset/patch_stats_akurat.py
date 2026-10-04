#!/usr/bin/env python3
"""Panel statistik akurat: pakai hitungan DATABASE, bukan 200 job terakhir.

MASALAH: panel menghitung hanya 200 job terakhir dari endpoint daftar, lalu
menampilkannya sebagai "Job selesai/gagal". Dengan 1666 job di DB, angkanya
jauh dari kenyataan.

PERBAIKAN DUA SISI:
  1. gateway: /v1/vids/jobs mengembalikan `summary` (COUNT per status di
     SELURUH tabel) + `total`, tanpa membebani daftar.
  2. bot: panel membaca `summary` itu; kalau tidak ada (versi lama), panel
     menandai angkanya sebagai "200 terakhir" supaya tidak menyesatkan.

Backup .bak-<ts> di kedua sisi.
"""
import shutil
import subprocess
import time

GATE = "/root/api-google-flow/app.py"
BOT = "/root/bottele/bot.py"
ts = int(time.time())
NL = chr(10)

# ═══════════════ 1. GATEWAY ═══════════════
g = open(GATE).read()
shutil.copy2(GATE, f"{GATE}.bak-statsum-{ts}")
print(f"backup: app.py.bak-statsum-{ts}")

LAMA_G = '''    finally:
        conn.close()
    return {"jobs": [dict(r) for r in rows], "count": len(rows)}'''

BARU_G = '''        # RINGKASAN AKURAT: hitung SELURUH tabel, bukan hanya halaman ini.
        # Panel bot butuh angka total; menghitung dari `rows` (yang dibatasi
        # `limit`) membuat angka jauh lebih kecil dari kenyataan.
        summary = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM vids_jobs GROUP BY status")}
        total = sum(summary.values())
    finally:
        conn.close()
    return {"jobs": [dict(r) for r in rows], "count": len(rows),
            "total": total, "summary": summary,
            "window": len(rows)}'''

if '"summary": summary' in g:
    print("  = gateway sudah punya summary")
elif LAMA_G in g:
    g = g.replace(LAMA_G, BARU_G, 1)
    open(GATE, "w").write(g)
    print("  + gateway: summary + total ditambahkan")
else:
    print("  !! pola gateway tidak ketemu")

r = subprocess.run(["/root/geminiomni/venv/bin/python", "-m", "py_compile", GATE],
                   capture_output=True, text=True)
print("  py_compile app.py:", "OK" if r.returncode == 0 else r.stderr[-300:])

# ═══════════════ 2. BOT ═══════════════
b = open(BOT).read()
shutil.copy2(BOT, f"{BOT}.bak-statakurat-{ts}")
print(f"backup: bot.py.bak-statakurat-{ts}")

# _gw_vids_jobs: kembalikan seluruh respons (bukan hanya daftar) via fungsi baru
LAMA_B = '''def _gw_vids_accounts():'''
BARU_B = '''def _gw_vids_jobs_penuh(limit=200):
    """Respons LENGKAP /v1/vids/jobs: daftar + summary + total (akurat)."""
    try:
        import requests as _rq
        url = f"{config.API_BASE_URL}/v1/vids/jobs?limit={limit}"
        r = _rq.get(url, headers=api.get_headers(), timeout=config.API_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json() or {}
    except Exception as e:
        print(f"[admin] gagal ambil job: {e}")
        return {}


def _gw_vids_accounts():'''

if "_gw_vids_jobs_penuh" in b:
    print("  = fungsi penuh sudah ada")
elif LAMA_B in b:
    b = b.replace(LAMA_B, BARU_B, 1)
    print("  + fungsi _gw_vids_jobs_penuh ditambahkan")

# panel stats: pakai summary akurat
LAMA_P = '''        jobs = _gw_vids_jobs(200)
        acc = _gw_vids_accounts()
        states = [a for a in (acc.get("accounts") or []) if (a.get("email") or "").strip()]
        healthy = [a for a in states if a.get("state") == "ok"]
        limited = [a for a in states if a.get("state") in ("limited", "no_login", "ui_retry")]
        done = [j for j in jobs if (j.get("status") or "").lower() == "completed"]
        fail = [j for j in jobs if (j.get("status") or "").lower() == "failed"]
        run = [j for j in jobs if (j.get("status") or "").lower() in ("running", "queued")]
        txt = (
            "📊 <b>STATISTIK</b>\\n"
            "━━━━━━━━━━━━━━━━━━━\\n"
            f"🎬 Job selesai : <b>{len(done)}</b>\\n"
            f"❌ Job gagal   : <b>{len(fail)}</b>\\n"
            f"⏳ Jalan/antre : <b>{len(run)}</b>\\n"
            "\\n"'''

BARU_P = '''        penuh = _gw_vids_jobs_penuh(200)
        jobs = penuh.get("jobs") or []
        ringkas = penuh.get("summary") or {}
        total_db = int(penuh.get("total") or 0)
        acc = _gw_vids_accounts()
        states = [a for a in (acc.get("accounts") or []) if (a.get("email") or "").strip()]
        healthy = [a for a in states if a.get("state") == "ok"]
        limited = [a for a in states if a.get("state") != "ok"]

        def _n(*kunci):
            return sum(int(ringkas.get(k, 0)) for k in kunci)

        # Angka dari DATABASE (seluruh riwayat). Kalau gateway lama (tanpa
        # summary), jatuh ke hitungan jendela + beri tanda jujur.
        if ringkas:
            done = _n("completed")
            fail = _n("failed")
            run = _n("running", "queued")
            catatan = f"seluruh riwayat ({total_db} job)"
        else:
            done = len([j for j in jobs if (j.get("status") or "").lower() == "completed"])
            fail = len([j for j in jobs if (j.get("status") or "").lower() == "failed"])
            run = len([j for j in jobs if (j.get("status") or "").lower() in ("running", "queued")])
            catatan = f"hanya {len(jobs)} job terakhir (gateway lama)"

        txt = (
            "📊 <b>STATISTIK</b>\\n"
            "━━━━━━━━━━━━━━━━━━━\\n"
            f"🎬 Job selesai : <b>{done}</b>\\n"
            f"❌ Job gagal   : <b>{fail}</b>\\n"
            f"⏳ Jalan/antre : <b>{run}</b>\\n"
            "\\n"'''

if "summary\" or {}" in b or "_gw_vids_jobs_penuh(200)" in b:
    print("  = panel sudah pakai summary")
elif LAMA_P in b:
    b = b.replace(LAMA_P, BARU_P, 1)
    print("  + panel: pakai summary akurat")
else:
    print("  !! pola panel tidak ketemu")

# tambahkan baris catatan sumber angka pada teks panel
LAMA_C = '''            f"✅ Akun sehat  : <b>{len(healthy)}</b>\\n"
            f"🚫 Akun limit  : <b>{len(limited)}</b>\\n"
            f"📦 Total akun  : <b>{len(states)}</b>\\n"
            "━━━━━━━━━━━━━━━━━━━\\n"
            f"🕐 <code>{esc(_now_wib())}</code>"'''
BARU_C = '''            f"✅ Akun sehat  : <b>{len(healthy)}</b>\\n"
            f"🚫 Akun limit  : <b>{len(limited)}</b>\\n"
            f"📦 Total akun  : <b>{len(states)}</b>\\n"
            "━━━━━━━━━━━━━━━━━━━\\n"
            f"📚 Angka job dari: <i>{esc(catatan)}</i>\\n"
            f"🕐 <code>{esc(_now_wib())}</code>"'''

if "Angka job dari" in b:
    print("  = catatan sumber sudah ada")
elif LAMA_C in b:
    b = b.replace(LAMA_C, BARU_C, 1)
    open(BOT, "w").write(b)
    print("  + catatan sumber angka ditambahkan")
else:
    print("  !! pola penutup panel tidak ketemu")
    open(BOT, "w").write(b)

r = subprocess.run(["/root/af_project/flow-agent/flow-agent/.venv/bin/python",
                    "-m", "py_compile", BOT], capture_output=True, text=True)
print("  py_compile bot.py:", "OK" if r.returncode == 0 else r.stderr[-300:])