#!/usr/bin/env python3
"""Vids job runner: ROTASI AKUN OTOMATIS + RAMAH VPS.

Aturan desain (menjawab permintaan "ribuan request" + "VPS tidak keberatan" +
"akun limit ganti akun lain"):

  1. ROTASI AKUN
     Tiap job memakai akun berikutnya dari pool (round-robin). Sebelum dipakai,
     akun diperiksa "sehat": tombol Generate hidup = kuota ada. Kalau mati
     (limit reached), akun di-cooldown dan job pindah ke akun berikutnya.

  2. RAMAH VPS
     - Browser TIDAK satu per job. Satu konteks per akun, dipakai ulang, dan
       ditutup setelah idle beberapa menit (MAX_CONTEXTS membatasi jumlah
       konteks hidup sekaligus).
     - Batas konkuren (MAX_CONCURRENT) supaya CPU/RAM tidak jebol.
     - Antrian di disk (SQLite), jadi job tidak hilang saat restart.
     - Satu job = satu halaman; halaman ditutup begitu selesai.

  3. HASIL
     Video diambil LANGSUNG dari stream (URL contribution-rt.usercontent...),
     bukan lewat ekspor — jalur ekspor balas "Failed to fetch" di sebagian akun.
"""
import asyncio
import contextlib
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/root/api-google-flow")
DB = APP / "data" / "vids_jobs.db"
PROFILES = APP / "chrome_profiles"
OUT = APP / "output"
STREAM_SAVE = APP / "data" / "vids_streamed.json"

MAX_CONCURRENT = int(os.environ.get("VIDS_MAX_CONCURRENT", "1"))
MAX_CONTEXTS = int(os.environ.get("VIDS_MAX_CONTEXTS", "1"))
IDLE_CLOSE_SECONDS = int(os.environ.get("VIDS_IDLE_CLOSE", "180"))
ACCOUNT_COOLDOWN_LIMIT = int(os.environ.get("VIDS_LIMIT_COOLDOWN", "3600"))
GEN_TIMEOUT = int(os.environ.get("VIDS_GEN_TIMEOUT", "240"))
# Batas beban: satu job tidak boleh menyandera antrian. VPS ini ~4 GB RAM, dan
# satu Chrome bisa memakai 400 MB, jadi konkurensi 1 adalah setelan aman.
JOB_HARD_TIMEOUT = int(os.environ.get("VIDS_JOB_TIMEOUT", "300"))
MIN_FREE_MB = int(os.environ.get("VIDS_MIN_FREE_MB", "300"))
# UI Vids kadang lambat/tidak siap. Tunggu adaptif jauh lebih baik daripada
# sekali tunggu 20s lalu menyerah (penyebab 6 dari 18 job gagal di produksi).
UI_WAIT_SECONDS = int(os.environ.get("VIDS_UI_WAIT", "75"))
UI_PROBE_SECONDS = int(os.environ.get("VIDS_UI_PROBE", "14"))
MAX_ACCOUNT_TRIES = int(os.environ.get("VIDS_ACCOUNT_TRIES", "4"))

UPLOAD_DIR = "https://docs.google.com/videos/create"


def log(*a):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}]", *a, flush=True)


# ─────────────────────────────────────────────────────────────
# antrian di disk
# ─────────────────────────────────────────────────────────────
def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS vids_jobs (
        job_id TEXT PRIMARY KEY, account TEXT, prompt TEXT,
        status TEXT, progress INTEGER, result_path TEXT, error TEXT,
        created_at INTEGER, updated_at INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS vids_accounts (
        email TEXT PRIMARY KEY, state TEXT, note TEXT, until INTEGER, updated_at INTEGER)""")
    c.commit()
    c.close()


@contextlib.contextmanager
def db():
    """Koneksi yang DIJAMIN ditutup.

    Versi lama memakai `with sqlite3.connect(...) as c:` yang hanya commit —
    koneksinya tetap terbuka dan bisa menahan lock, membuat UPDATE status tidak
    terlihat oleh proses lain.
    """
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# pool akun
# ─────────────────────────────────────────────────────────────
class AccountPool:
    """Rotasi akun + cooldown otomatis untuk akun yang limit."""

    def __init__(self, emails):
        self.emails = list(emails)
        self._idx = 0

    def available(self):
        now = int(time.time())
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT email, state, until FROM vids_accounts").fetchall()
        finally:
            conn.close()
        # Blokir SETIAP akun yang masa istirahatnya belum habis — bukan hanya
        # 'limited'. Dulu akun 'ui_retry' tetap masuk pool sehingga dipilih lagi
        # dan membuang ~2,5 menit tiap kali gagal.
        blocked = {r["email"] for r in rows if (r["until"] or 0) > now}
        return [e for e in self.emails if e not in blocked]

    def next_account(self):
        pool = self.available()
        if not pool:
            return None
        self._idx = (self._idx + 1) % len(pool)
        return pool[self._idx]

    def mark_limited(self, email, seconds=ACCOUNT_COOLDOWN_LIMIT):
        until = int(time.time()) + seconds
        with db() as c:
            c.execute("""INSERT INTO vids_accounts (email, state, note, until, updated_at)
                         VALUES (?, 'limited', 'quota reached', ?, ?)
                         ON CONFLICT(email) DO UPDATE SET
                           state='limited', note='quota reached',
                           until=excluded.until, updated_at=excluded.updated_at""",
                      (email, until, int(time.time())))
        log(f"akun {email} limit — cooldown {seconds}s")

    def mark_ui_retry(self, email, seconds=900):
        """Akun yang UI-nya tidak siap: istirahatkan sementara, jangan dibuang."""
        until = int(time.time()) + seconds
        with db() as c:
            c.execute("""INSERT INTO vids_accounts (email, state, note, until, updated_at)
                         VALUES (?, 'ui_retry', 'UI Vids tidak siap', ?, ?)
                         ON CONFLICT(email) DO UPDATE SET
                           state='ui_retry', note='UI Vids tidak siap',
                           until=excluded.until, updated_at=excluded.updated_at""",
                      (email, until, int(time.time())))
        log(f"akun {email} UI tidak siap — istirahat {seconds}s")

    def mark_ok(self, email):
        with db() as c:
            c.execute("""INSERT INTO vids_accounts (email, state, note, until, updated_at)
                         VALUES (?, 'ok', '', 0, ?)
                         ON CONFLICT(email) DO UPDATE SET
                           state='ok', note='', until=0, updated_at=excluded.updated_at""",
                      (email, int(time.time())))


def has_login_cookies(email):
    """True bila profil chrome akun ini punya cookie sesi Google."""
    import tempfile
    ck = APP / "chrome_profiles" / email / "Default" / "Cookies"
    if not ck.exists():
        return False
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tmp = tf.name
        shutil.copy2(ck, tmp)
        c = sqlite3.connect(tmp)
        n = c.execute("SELECT COUNT(*) FROM cookies "
                      "WHERE host_key LIKE '%google.com'").fetchone()[0]
        c.close()
        return n > 5
    except Exception:
        return False
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except Exception:
                pass


def mark_no_login(pool, emails):
    """Tandai akun tanpa sesi login supaya tidak dipakai (istirahat 7 hari)."""
    import concurrent.futures as cf
    bad = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for email, ok in zip(emails, ex.map(has_login_cookies, emails)):
            if not ok:
                bad.append(email)
    if not bad:
        return []
    until = int(time.time()) + 7 * 86400
    with db() as c:
        for email in bad:
            c.execute("""INSERT INTO vids_accounts (email, state, note, until, updated_at)
                         VALUES (?, 'no_login', 'sesi login tidak ada', ?, ?)
                         ON CONFLICT(email) DO UPDATE SET
                           state='no_login', note='sesi login tidak ada',
                           until=excluded.until, updated_at=excluded.updated_at""",
                      (email, until, int(time.time())))
    log(f"akun belum login (dikecualikan): {len(bad)} — {', '.join(bad[:5])}...")
    return bad


def job_aspect(row):
    """Rasio yang diminta untuk baris job (portrait/landscape)."""
    try:
        v = row["aspect"]
        if v:
            return str(v).lower()
    except Exception:
        pass
    return os.environ.get("VIDS_ASPECT_DEFAULT", "landscape").lower()


def load_accounts():
    f = APP / "accounts.txt"
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        email = line.split("|")[0].strip()
        if "@" in email:
            out.append(email)
    return out


# ─────────────────────────────────────────────────────────────
# konteks browser per akun, dengan pembatas
# ─────────────────────────────────────────────────────────────
class ContextManager:
    """Satu konteks per akun, dibatasi jumlahnya, ditutup saat idle."""

    def __init__(self, pw):
        self.pw = pw
        self.ctx = {}          # email -> (context, last_used)
        self.lock = asyncio.Lock()

    async def get(self, email):
        async with self.lock:
            now = time.time()
            # tutup yang idle dulu supaya jumlah konteks tetap kecil
            for e, (c, t) in list(self.ctx.items()):
                if now - t > IDLE_CLOSE_SECONDS:
                    try:
                        await c.close()
                    except Exception:
                        pass
                    self.ctx.pop(e, None)
                    log(f"konteks idle ditutup: {e}")
            if email in self.ctx:
                c, _ = self.ctx[email]
                self.ctx[email] = (c, now)
                return c
            # batas konteks hidup
            while len(self.ctx) >= MAX_CONTEXTS:
                oldest = min(self.ctx, key=lambda k: self.ctx[k][1])
                c, _ = self.ctx.pop(oldest)
                try:
                    await c.close()
                except Exception:
                    pass
                log(f"konteks dibuang (batas {MAX_CONTEXTS}): {oldest}")
            # beban VPS: jangan buka konteks baru kalau memori sudah tipis
            try:
                import subprocess as _sp
                out = _sp.run("free -m | awk 'NR==2{print $7}'", shell=True,
                              capture_output=True, text=True).stdout.strip()
                free_mb = int(out or "0")
                if free_mb < MIN_FREE_MB:
                    log(f"memori tipis ({free_mb} MB < {MIN_FREE_MB} MB) — "
                        f"menunggu sebelum buka konteks")
                    for _ in range(30):
                        await asyncio.sleep(5)
                        out = _sp.run("free -m | awk 'NR==2{print $7}'", shell=True,
                                      capture_output=True, text=True).stdout.strip()
                        if int(out or "0") >= MIN_FREE_MB:
                            break
            except Exception:
                pass

            prof = PROFILES / email
            prof.mkdir(parents=True, exist_ok=True)
            # lepaskan profil yang masih dipegang proses chrome lain (kelas bug
            # "Target page, context or browser has been closed")
            import glob as _glob
            for pid_s in os.listdir("/proc"):
                if not pid_s.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid_s}/cmdline", "rb") as fh:
                        cmd = fh.read().decode("utf-8", "replace")
                except Exception:
                    continue
                if str(prof) in cmd and "chrome" in cmd.lower():
                    try:
                        os.kill(int(pid_s), 15)
                    except Exception:
                        pass
            for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                f = prof / name
                try:
                    if f.is_symlink() or f.exists():
                        f.unlink()
                except Exception:
                    pass
            await asyncio.sleep(1)
            ctx = await self.pw.chromium.launch_persistent_context(
                user_data_dir=str(prof),
                executable_path="/usr/bin/google-chrome-stable",
                headless=False,
                accept_downloads=True,
                downloads_path=str(OUT),
                args=["--display=:99", "--no-sandbox", "--disable-gpu",
                      "--disable-dev-shm-usage", "--window-size=1400,900",
                      "--disable-blink-features=AutomationControlled"],
                viewport={"width": 1400, "height": 900},
            )
            self.ctx[email] = (ctx, now)
            log(f"konteks dibuka: {email} (hidup: {len(self.ctx)})")
            return ctx

    async def close_all(self):
        for e, (c, _) in list(self.ctx.items()):
            try:
                await c.close()
            except Exception:
                pass
        self.ctx.clear()


# ─────────────────────────────────────────────────────────────
# satu job
# ─────────────────────────────────────────────────────────────
async def wait_until(page, js_condition, timeout=30, poll=0.4):
    """Poll sampai kondisi JS bernilai true. Ganti sleep tetap.

    Mengembalikan True bila kondisi tercapai, False bila waktu habis.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await page.evaluate(js_condition):
                return True
        except Exception:
            pass
        await asyncio.sleep(poll)
    return False


async def wait_video_ready(page, timeout=GEN_TIMEOUT, poll=2):
    """Poll sampai video hasil benar-benar ada dan siap.

    Menggantikan sleep(8) tiap putaran: begitu video muncul, langsung lanjut.
    Mengembalikan dict {src,dur,w,h} atau None.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = await page.evaluate("""() => {
                const vs=[...document.querySelectorAll('video')].map(v=>({
                    src:(v.currentSrc||v.src||''), dur:v.duration||0,
                    w:v.videoWidth, h:v.videoHeight, ready:v.readyState}));
                const real = vs.filter(v=>v.dur>1 && v.src.indexOf('gstatic')<0);
                const busy=/membuat|generating|menunggu|progress|rendering/i
                    .test(document.body.innerText||'');
                return {real, busy};
            }""")
            if info.get("real") and not info.get("busy"):
                return info["real"][0]
        except Exception:
            pass
        await asyncio.sleep(poll)
    return None


async def wait_for_editor(page, timeout=UI_WAIT_SECONDS):
    """Tunggu editor Vids siap (kolom prompt muncul).

    Mengembalikan True bila siap. Polling dengan jeda supaya halaman yang lambat
    tetap punya kesempatan, bukan sekali cek lalu menyerah.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            el = await page.query_selector('div[aria-label^="Describe your video"]')
            if el and await el.is_visible():
                return True
        except Exception:
            pass
        await asyncio.sleep(2)
    return False


def looks_like_ui_not_ready(detail):
    """True bila kegagalan berasal dari UI yang tidak siap (layak coba akun lain)."""
    d = (detail or "").lower()
    return any(k in d for k in (
        "timeout", "wait_for", "not visible", "tidak muncul",
        "target page, context or browser", "no such element",
        "element is not attached", "describe your video",
    ))


ZERO_UI = os.environ.get("VIDS_ZERO_UI", "1") not in ("0", "false", "no")
TEMPLATE_FILE = Path(__file__).parent / "data" / "genai_template.json"
WARMUP_MARK = "warmup"

HOOK_JS = """
(() => {
  if (window.__zHook) return; window.__zHook = true;
  window.__calls = [];
  function keep(r) {
    try { if (String(r.url).indexOf('appsgenaiserver') >= 0) window.__calls.push(r); }
    catch (e) {}
  }
  const of = window.fetch;
  window.fetch = function(...a) {
    try { const u = String(a[0] && a[0].url ? a[0].url : a[0]); const i = a[1] || {};
      let h = {};
      if (i.headers) h = (i.headers instanceof Headers)
          ? Object.fromEntries(i.headers.entries()) : Object.assign({}, i.headers);
      keep({how:'fetch', url:u, method:i.method||'GET', headers:h,
            body: i.body ? String(i.body) : null});
    } catch (e) {}
    return of.apply(this, a);
  };
  const oO=XMLHttpRequest.prototype.open, oS=XMLHttpRequest.prototype.send,
        oH=XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open=function(m,u,...r){this.__m=m;this.__u=String(u);this.__h={};
    return oO.call(this,m,u,...r);};
  XMLHttpRequest.prototype.setRequestHeader=function(k,v){try{this.__h[k]=v;}catch(e){}
    return oH.call(this,k,v);};
  XMLHttpRequest.prototype.send=function(b){
    try{keep({how:'xhr',url:this.__u,method:this.__m,headers:this.__h,
              body:b?String(b):null});}catch(e){}
    return oS.call(this,b);};
})();
"""


def load_template():
    """Body payload genai/generate hasil rekaman nyata (penanda: warmup)."""
    try:
        return json.loads(TEMPLATE_FILE.read_text())
    except Exception:
        return None


def normalize_prompt(prompt):
    """Ambil teks prompt yang benar dari input user.

    User kadang mengirim prompt bergaya JSON:
      {"prompt": "...", "negative_prompt": "...", "settings": {...}}
    Vids hanya menerima teks prompt biasa, jadi ambil isi "prompt" saja dan
    tempelkan negative_prompt sebagai kalimat penghindaran.
    """
    p = (prompt or "").strip()
    if not p or not p.startswith("{"):
        return p
    try:
        obj = json.loads(p)
    except Exception:
        return p
    if not isinstance(obj, dict) or "prompt" not in obj:
        return p
    main = str(obj.get("prompt") or "").strip()
    neg = str(obj.get("negative_prompt") or "").strip()
    if neg:
        main = f"{main} Avoid: {neg}"
    return main or p


def build_body(template_body, prompt):
    """Isi token berpetik '"warmup"' di template dengan prompt ter-escape JSON.

    WAJIB pakai json.dumps: prompt yang mengandung kurawal, kutip, atau baris
    baru akan merusak payload kalau ditempel mentah (Google membalas 400).
    """
    return template_body.replace('"' + WARMUP_MARK + '"', json.dumps(prompt))


# Kolom prompt Vids berubah label setelah gambar diunggah:
#   tanpa gambar : "Describe your video. ..."
#   dengan gambar: "Specify how you want to use the ingredients. ..."
PROMPT_SEL = ('div[aria-label^="Describe your video"], '
              'div[aria-label^="Specify how you want to use the ingredients"], '
              'div[aria-label*="how you want to use"], '
              'div[aria-label*="Describe your"]')


async def find_prompt(page, timeout=20):
    """Cari kolom prompt (label berubah bila ada gambar). Kembalikan locator/None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            loc = page.locator(PROMPT_SEL).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            pass
        await asyncio.sleep(0.7)
    return None


async def type_prompt(page, text, job_id=""):
    """Isi prompt ke kolom yang benar. True bila berhasil."""
    loc = await find_prompt(page, timeout=20)
    if loc is None:
        log(f"[{job_id[:8]}] kolom prompt tidak ditemukan")
        return False
    try:
        await loc.click(timeout=8000)
    except Exception:
        pass
    try:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    except Exception:
        pass
    await page.keyboard.type(text, delay=25)
    # pastikan teks benar-benar masuk
    for _ in range(10):
        await asyncio.sleep(0.5)
        try:
            ok = await page.evaluate("""(sel) => {
                const el = document.querySelector(sel);
                return !!(el && (el.innerText || '').trim().length > 3);
            }""", PROMPT_SEL)
            if ok:
                return True
        except Exception:
            pass
    return False


async def settings_bar(page):
    """Baca bilah pengaturan (contoh: 'Generation settings: Omni, 720p, Portrait, 10 seconds')."""
    try:
        return await page.evaluate("""() => {
            const b = [...document.querySelectorAll('button')]
                .find(e => /Generation settings/i.test(e.getAttribute('aria-label') || ''));
            return b ? (b.getAttribute('aria-label') || '') : null;
        }""")
    except Exception:
        return None


async def select_portrait(page):
    """Pilih rasio Portrait lewat panel pengaturan.

    Membuka tombol "Generation settings", lalu mengklik radio 'Portrait'.
    Mengembalikan True bila bilah pengaturan benar-benar menyebut Portrait.
    """
    bar0 = await settings_bar(page)
    if bar0 and "Portrait" in bar0:
        return True
    # buka panel pengaturan
    try:
        await page.evaluate("""() => {
            const b = [...document.querySelectorAll('button')]
                .find(e => /Generation settings/i.test(e.getAttribute('aria-label') || ''));
            if (b) b.click();
        }""")
    except Exception:
        return False
    await asyncio.sleep(2.5)
    # klik radio Portrait
    try:
        await page.evaluate("""() => {
            const els = [...document.querySelectorAll('[role="radio"],button')]
                .filter(el => el.offsetParent !== null)
                .filter(el => ((el.innerText || '').trim()) === 'Portrait');
            if (els.length) els[0].click();
        }""")
    except Exception:
        return False
    await asyncio.sleep(2.5)
    # tutup panel
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await asyncio.sleep(1.5)
    bar1 = await settings_bar(page)
    ok = bool(bar1 and "Portrait" in bar1)
    log(f"rasio portrait: {'OK' if ok else 'GAGAL'} ({bar1})")
    return ok


def load_aspect_template(aspect):
    """Template payload untuk rasio tertentu (portrait/landscape)."""
    name = "portrait_template.json" if aspect == "portrait" else "genai_template.json"
    f = Path(__file__).parent / "data" / name
    try:
        return json.loads(f.read_text())
    except Exception:
        # portrait belum ada -> pakai template biasa
        try:
            return json.loads(
                (Path(__file__).parent / "data" / "genai_template.json").read_text())
        except Exception:
            return None


async def download_media(url, job_id, idx):
    """Unduh gambar/video dari URL ke berkas sementara. Kembalikan Path atau None."""
    import urllib.request
    tmpdir = APP / "data" / "uploads"
    tmpdir.mkdir(parents=True, exist_ok=True)
    ext = ".png"
    low = (url or "").lower()
    for cand in (".png", ".jpg", ".jpeg", ".webp", ".mp4"):
        if low.split("?")[0].endswith(cand):
            ext = cand
            break
    dst = tmpdir / f"{job_id[:8]}_{idx}{ext}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if len(data) < 200:
            log(f"[{job_id[:8]}] gambar terlalu kecil ({len(data)} byte)")
            return None
        dst.write_bytes(data)
        return dst
    except Exception as e:
        log(f"[{job_id[:8]}] unduh gambar gagal: {str(e)[:100]}")
        return None


async def upload_media(page, path, job_id, idx):
    """Unggah satu berkas lewat input[type=file] tersembunyi.

    Mengembalikan True bila thumbnail muncul (gambar benar-benar terpasang).
    """
    before = await count_thumbnails(page)
    try:
        fi = page.locator('input[type="file"]').first
        await fi.set_input_files(str(path), timeout=45000)
    except Exception as e:
        log(f"[{job_id[:8]}] unggah gambar {idx} gagal: {str(e)[:110]}")
        return False
    # tunggu sampai thumbnail bertambah (unggahan benar-benar selesai)
    for _ in range(30):
        await asyncio.sleep(1)
        if await count_thumbnails(page) > before:
            log(f"[{job_id[:8]}] gambar {idx} terpasang")
            return True
    log(f"[{job_id[:8]}] gambar {idx} tidak muncul di UI")
    return False


async def count_thumbnails(page):
    """Jumlah thumbnail gambar yang terpasang di editor."""
    try:
        return await page.evaluate("""() => [...document.querySelectorAll('img')]
            .filter(i => /blob:|data:image/.test(i.src || '')).length""")
    except Exception:
        return 0


async def attach_images(page, job_id, urls):
    """Pasang gambar berurutan (slot 1, 2, ...). Kembalikan jumlah yang berhasil."""
    ok = 0
    for i, url in enumerate(urls, 1):
        path = await download_media(url, job_id, i)
        if not path:
            continue
        if await upload_media(page, path, job_id, i):
            ok += 1
        else:
            break
    return ok


async def harvest_token(page, timeout=20, settle=2.0):
    """Ambil Authorization + X-Server-Token dari panggilan quotaSummary.

    GRATIS: quotaSummary dipanggil aplikasi begitu editor terbuka, jadi tidak
    perlu mengklik Generate (yang akan membakar satu render + kuota).

    Penting: token dari panggilan PALING AWAL bisa ditolak 400 (editor belum
    matang). Karena itu kita tunggu `settle` detik dan utamakan panggilan yang
    membawa body sungguhan (body_len >= 12).
    """
    await asyncio.sleep(settle)
    best = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            calls = await page.evaluate("() => JSON.stringify(window.__calls || [])")
            for c in json.loads(calls):
                hl = {k.lower(): v for k, v in (c.get("headers") or {}).items()}
                if not (hl.get("authorization") and hl.get("x-server-token")):
                    continue
                cand = {
                    "Authorization": hl["authorization"],
                    "X-Server-Token": hl["x-server-token"],
                    "X-Goog-AuthUser": hl.get("x-goog-authuser") or "0",
                    "Content-Type": hl.get("content-type") or "application/json",
                    "Accept-Language": hl.get("accept-language") or "id-ID,id;q=0.9",
                }
                # panggilan dengan body sungguhan lebih matang -> pakai ini
                if len(c.get("body") or "") >= 12:
                    return cand
                best = best or cand
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return best


async def xhr_post(page, url, body, hdrs, timeout_ms=90000):
    """POST payload dari dalam halaman -> (status, teks respons)."""
    raw = await page.evaluate("""async ([url, body, hdrs, tmo]) => {
        return await new Promise((res) => {
            const x = new XMLHttpRequest();
            x.open('POST', url, true);
            x.withCredentials = true;
            x.timeout = tmo;
            Object.keys(hdrs||{}).forEach(k => { try { x.setRequestHeader(k, hdrs[k]); } catch(e){} });
            x.onload = () => res(JSON.stringify({status: x.status, text: String(x.responseText)}));
            x.onerror = () => res(JSON.stringify({error: 'xhr error', status: x.status}));
            x.ontimeout = () => res(JSON.stringify({error: 'timeout', status: 0}));
            x.send(body);
        });
    }""", [url, body, hdrs, timeout_ms])
    j = json.loads(raw)
    return j.get("status"), (j.get("text") or j.get("error") or "")


async def download_url(page, url):
    """Unduh dari dalam halaman (cookie lintas-domain) -> bytes atau None."""
    b64 = await page.evaluate("""async (u) => {
        try {
            const r = await fetch(u, {credentials:'include'});
            if (!r.ok) return '';
            const buf = await r.arrayBuffer();
            let bin = ''; const b = new Uint8Array(buf); const C = 32768;
            for (let i = 0; i < b.length; i += C) {
                bin += String.fromCharCode.apply(null, b.subarray(i, i + C));
            }
            return btoa(bin);
        } catch (e) { return ''; }
    }""", url)
    if not b64:
        return None
    import base64
    return base64.b64decode(b64)


async def run_job(cm, pool, job_id, email, prompt, aspect="landscape", images=None):
    """Generate satu video di akun tertentu; simpan MP4; kembalikan (status, path/err)."""
    ctx = await cm.get(email)
    page = await ctx.new_page()
    try:
        await page.add_init_script(HOOK_JS)
        log(f"[{job_id[:8]}] buka Vids ({email})")
        await page.goto(UPLOAD_DIR, wait_until="domcontentloaded", timeout=90000)
        await wait_until(page,
                         """() => !!document.querySelector('button') &&
                                  (document.body.innerText||'').length > 200""",
                         timeout=25)

        # buka editor. Pada mode zero-UI, yang menentukan hanyalah TOKEN
        # (quotaSummary) — kolom prompt adalah urusan jalur UI, dan menunggunya
        # dulu memakan ~2,5 menit per akun rusak. Jadi: klik, lalu tunggu token;
        # hanya kalau token tak kunjung datang kita menunggu kolom prompt.
        opened = False
        if ZERO_UI:
            for attempt in range(2):
                try:
                    await page.locator('button:has-text("Create AI videos")').first.click(
                        timeout=12000)
                except Exception:
                    pass
                hdrs_probe = await harvest_token(page, timeout=UI_PROBE_SECONDS,
                                                 settle=1.5)
                if hdrs_probe:
                    opened = True
                    break
                await asyncio.sleep(2)
        if not opened:
            for attempt in range(2):
                try:
                    await page.locator('button:has-text("Create AI videos")').first.click(
                        timeout=12000)
                except Exception:
                    pass
                if await wait_for_editor(page, timeout=UI_WAIT_SECONDS // 2 if attempt == 0
                                         else UI_WAIT_SECONDS):
                    opened = True
                    break
                await asyncio.sleep(3)
        if not opened:
            return "retry_account", "editor Vids tidak siap di akun ini"

        # ── jalur cepat ZERO-UI ──────────────────────────────────────────
        video_url = None
        t_fast = time.time()
        # Gambar: referensi / start frame / end frame. Wajib lewat UI
        # (input[type=file] tersembunyi) — bukti: 1 foto jadi Image1,
        # 2 foto jadi Image1+Image2 dan video mengikuti keduanya.
        images = [u for u in (images or []) if u]
        images_attached = 0
        if images:
            images_attached = await attach_images(page, job_id, images)
            log(f"[{job_id[:8]}] gambar terpasang: {images_attached}/{len(images)}")
            if images_attached == 0:
                log(f"[{job_id[:8]}] tidak ada gambar yang terpasang")

        zero_ui_note = ""
        # Rasio: portrait perlu dokumen proyek yang dibuat di UI. Kita pilih
        # Portrait SETELAH editor siap, lalu pakai template portrait.
        want_portrait = str(aspect or "").lower().startswith("port")
        if want_portrait:
            got = await select_portrait(page)
            if not got:
                log(f"[{job_id[:8]}] portrait tidak bisa dipilih -> landscape")
        # Job berfoto WAJIB lewat UI: aplikasi Vids baru menyusun payload berisi
        # gambar (Image1/Image2) saat Generate diklik; template tersimpan tidak
        # memuatnya, sehingga video tadi tidak mengikuti foto.
        use_zero_ui = ZERO_UI and images_attached == 0
        if images_attached and ZERO_UI:
            log(f"[{job_id[:8]}] ada gambar -> pakai jalur UI (bukan zero-UI)")
        if use_zero_ui:
            tpl = load_aspect_template("portrait" if want_portrait else "landscape")
            for attempt in range(2):
                hdrs = await harvest_token(page, timeout=20, settle=2.0 if attempt == 0 else 1.0)
                if not hdrs or not tpl:
                    zero_ui_note = "token/template tidak tersedia"
                    break
                body = build_body(tpl.get("body") or "", normalize_prompt(prompt))
                st, text = await xhr_post(page, tpl["url"], body, hdrs)
                dt = time.time() - t_fast
                if st == 200:
                    m = re.search(r"https://contribution-rt[^\"\\]+", text)
                    if m:
                        video_url = m.group(0).replace("\\u0026", "&")
                        log(f"[{job_id[:8]}] zero-UI OK {dt:.1f}s"
                            + ("" if attempt == 0 else " (percobaan ke-2)"))
                        break
                    zero_ui_note = "200 tanpa URL video"
                else:
                    zero_ui_note = f"status={st} {text[:120]}"
                    log(f"[{job_id[:8]}] zero-UI gagal: {zero_ui_note}")
                if attempt == 0:
                    await asyncio.sleep(2)
            if not video_url and zero_ui_note:
                log(f"[{job_id[:8]}] beralih ke jalur UI ({zero_ui_note[:80]})")

        # ── cadangan: jalur UI lama ──────────────────────────────────────
        if not video_url:
            # Label kolom berubah bila ada gambar -> pakai helper find_prompt
            if not await type_prompt(page, normalize_prompt(prompt), job_id):
                return "retry_account", "kolom prompt tidak bisa dipakai (label berubah)"

            st = await page.evaluate("""() => {
                const b=document.querySelector('button[aria-label="Generate"]');
                return {disabled: b?!!b.disabled:null};
            }""")
            if st.get("disabled"):
                return "limited", "tombol Generate mati (kuota akun habis)"

            try:
                await page.locator('button[aria-label="Generate"]').first.click(timeout=12000)
            except Exception as e:
                return "retry_account", f"tombol Generate tidak bisa diklik: {str(e)[:80]}"
            log(f"[{job_id[:8]}] generate dikirim (UI)")

            src = await wait_video_ready(page, timeout=GEN_TIMEOUT, poll=2)
            if not src:
                return "failed", "video hasil tidak muncul sebelum batas waktu"
            log(f"[{job_id[:8]}] video jadi {src['dur']:.1f}s {src['w']}x{src['h']}")
            video_url = src["src"]

        # unduh
        data = await download_url(page, video_url)
        if not data:
            return "failed", "stream tidak bisa diunduh"
        if len(data) < 50000:
            return "failed", f"file terlalu kecil ({len(data)} byte)"
        path = OUT / f"vids_{job_id[:8]}.mp4"
        path.write_bytes(data)
        log(f"[{job_id[:8]}] tersimpan {path.name} {len(data)} byte")
        return "completed", str(path)
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# worker
# ─────────────────────────────────────────────────────────────
async def worker(name, cm, pool, sem):
    async with sem:
        # ambil satu job dari antrian secara ATOMIK.
        # rowid dipakai sebagai urutan karena created_at hanya berpresisi detik
        # dan bisa bertabrakan (itulah yang membuat job tertentu tidak pernah
        # terambil meski runner hidup).
        job_id = prompt = None
        job_aspect_value = "landscape"
        with db() as c:
            # kolom `aspect` mungkin belum ada di DB lama -> ambil dengan aman
            try:
                row = c.execute("""SELECT job_id, prompt, aspect FROM vids_jobs
                                   WHERE status='queued' ORDER BY rowid LIMIT 1""").fetchone()
            except Exception:
                row = c.execute("""SELECT job_id, prompt FROM vids_jobs
                                   WHERE status='queued' ORDER BY rowid LIMIT 1""").fetchone()
            if not row:
                return None
            cur = c.execute("""UPDATE vids_jobs SET status='running', updated_at=?
                               WHERE job_id=? AND status='queued'""",
                            (int(time.time()), row["job_id"]))
            if cur.rowcount != 1:
                # worker lain sudah mengambilnya
                return None
            job_id, prompt = row["job_id"], row["prompt"]
            try:
                job_aspect_value = (row["aspect"] or "landscape")
            except Exception:
                job_aspect_value = "landscape"
        job_images = []
        try:
            with db() as c2:
                r2 = c2.execute("SELECT images FROM vids_jobs WHERE job_id=?",
                                (job_id,)).fetchone()
                if r2 and r2["images"]:
                    job_images = json.loads(r2["images"])
        except Exception:
            job_images = []
        log(f"[{job_id[:8]}] diambil dari antrian")

        # rotasi akun: coba beberapa akun sampai ada yang sanggup
        tried = set()
        for attempt in range(MAX_ACCOUNT_TRIES):
            email = pool.next_account()
            if not email:
                with db() as c:
                    c.execute("""UPDATE vids_jobs SET status='failed',
                                 error='semua akun sedang limit', updated_at=?
                                 WHERE job_id=?""", (int(time.time()), job_id))
                log(f"[{job_id[:8]}] semua akun limit")
                return job_id
            if email in tried:
                continue
            tried.add(email)
            try:
                status, detail = await asyncio.wait_for(
                    run_job(cm, pool, job_id, email, prompt, aspect=job_aspect_value,
                            images=job_images),
                    timeout=JOB_HARD_TIMEOUT)
            except asyncio.TimeoutError:
                status, detail = "failed", f"batas waktu job {JOB_HARD_TIMEOUT}s terlampaui"
            except Exception as e:
                status, detail = "failed", str(e)[:200]
            log(f"[{job_id[:8]}] akun={email} -> {status}: {detail[:90]}")

            if status == "limited":
                pool.mark_limited(email)
                continue                      # GANTI AKUN lain, job tetap jalan
            if status == "retry_account":
                # UI tidak siap / tombol mati: akun ini dilewati sementara,
                # job pindah ke akun berikutnya (bukan langsung gagal)
                pool.mark_ui_retry(email)
                continue
            if status == "completed":
                pool.mark_ok(email)
                with db() as c:
                    c.execute("""UPDATE vids_jobs SET status='completed', account=?,
                                 progress=100, result_path=?, updated_at=? WHERE job_id=?""",
                              (email, detail, int(time.time()), job_id))
                return job_id
            # failed karena hal lain: catat, tidak mengganti akun
            with db() as c:
                c.execute("""UPDATE vids_jobs SET status='failed', account=?,
                             error=?, updated_at=? WHERE job_id=?""",
                          (email, detail[:300], int(time.time()), job_id))
            return job_id
        with db() as c:
            c.execute("""UPDATE vids_jobs SET status='failed', error=?,
                         updated_at=? WHERE job_id=?""",
                      ("semua akun yang dicoba tidak sanggup (limit / UI tidak siap)",
                       int(time.time()), job_id))
        return job_id


def ensure_xvfb():
    """Xvfb wajib ada: tanpa display, Chrome langsung menutup konteks.

    Gejala kalau tidak ada: "BrowserType.launch_persistent_context: Target page,
    context or browser has been closed" — bukan masalah profil terkunci.
    """
    import subprocess
    display = os.environ.get("DISPLAY", ":99")
    r = subprocess.run(["pgrep", "-f", f"Xvfb {display}"], capture_output=True)
    if r.returncode == 0:
        log(f"Xvfb sudah jalan di {display}")
        return True
    log(f"Xvfb tidak ada — menjalankan di {display}")
    subprocess.Popen(["Xvfb", display, "-screen", "0", "1400x900x24", "-ac",
                      "-nolisten", "tcp"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    time.sleep(4)
    r2 = subprocess.run(["pgrep", "-f", f"Xvfb {display}"], capture_output=True)
    ok = r2.returncode == 0
    log(f"Xvfb {'siap' if ok else 'GAGAL dijalankan'}")
    return ok


async def main():
    from playwright.async_api import async_playwright
    init_db()
    ensure_xvfb()
    OUT.mkdir(parents=True, exist_ok=True)

    # job 'running' yang menggantung dari proses sebelumnya -> kembalikan ke antrian
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM vids_jobs WHERE status='running'").fetchone()[0]
        if n:
            c.execute("""UPDATE vids_jobs SET status='queued',
                         error='dikembalikan ke antrian (runner restart)', updated_at=?
                         WHERE status='running'""", (int(time.time()),))
            log(f"{n} job menggantung dikembalikan ke antrian")

    emails = load_accounts()
    if os.environ.get("VIDS_SKIP_LOGIN_CHECK", "0") not in ("1", "true"):
        mark_no_login(None, emails)
    pool = AccountPool(emails)
    log(f"pool akun: {len(emails)} | tersedia: {len(pool.available())}")
    log(f"konkuren maks: {MAX_CONCURRENT} | konteks maks: {MAX_CONTEXTS}")

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with async_playwright() as pw:
        cm = ContextManager(pw)
        try:
            while True:
                with db() as c:
                    n = c.execute("SELECT COUNT(*) FROM vids_jobs WHERE status='queued'"
                                  ).fetchone()[0]
                if n == 0:
                    await asyncio.sleep(5)
                    continue
                await asyncio.gather(*[worker(f"w{i}", cm, pool, sem)
                                       for i in range(MAX_CONCURRENT)])
        finally:
            await cm.close_all()


if __name__ == "__main__":
    asyncio.run(main())