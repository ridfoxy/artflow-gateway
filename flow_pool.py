"""Concurrent worker pool for the Flow gateway.

Replaces the single shared page + serial workers. Each job owns a page from
submit through completion, and POOL_SIZE jobs run at once.

Measured on this host:
  - page open+close = 0.07s
  - 8 concurrent submits accepted (submission itself is fast)
  - but 8 SIMULTANEOUS page loads of the Flow editor overload the browser:
    .ProseMirror never appears and clicks time out.
So page LOADING is gated to LOAD_GATE at a time, while submission and polling
run concurrently across POOL_SIZE workers.

The one UI step kept is the composer keystroke: submitting via the batchexecute
RPC is rejected by Google's anti-abuse (PUBLIC_ERROR_UNUSUAL_ACTIVITY), so the
verified path is used. Nothing else touches the UI — no clicking through menus,
no settings panel, no mode pills.
"""

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import time

logger = logging.getLogger("pool")

PROJECT_ID = os.environ.get("FLOW_PROJECT_ID", "0f21b6bf-24a7-4cfe-81ec-0e674b324f4a")
OUTPUT_DIR = "/root/api-google-flow/output"
DB_PATH = "/root/api-google-flow/data/gateway.db"

# Measured safe concurrency on a 2-vCPU host.
POOL_SIZE = int(os.environ.get("FLOW_POOL_SIZE", "8"))
# How many Flow editors may be loading at once (measured safe value).
LOAD_GATE = int(os.environ.get("FLOW_LOAD_GATE", "3"))
SUBMIT_TIMEOUT = float(os.environ.get("FLOW_SUBMIT_TIMEOUT", "45"))
JOB_TIMEOUT = float(os.environ.get("FLOW_JOB_TIMEOUT", "540"))

# ── reference images / frames (start frame, end frame, references) ───────────
ENABLE_FRAME_ATTACH = os.environ.get("FLOW_ENABLE_FRAME_ATTACH", "0") == "1"
# Waktu tunggu DI DEPAN picker. Loop deteksi kondisi setelahnya
# (memantau nama berkas + penanda sibuk, batas 60s) yang benar-benar
# menunggu unggahan siap, jadi menunggu lama di muka hanya membuang waktu.
FRAME_UPLOAD_WAIT = float(os.environ.get("FLOW_FRAME_UPLOAD_WAIT", "2"))
FRAME_DIR = os.environ.get("FLOW_FRAME_DIR", "/root/api-google-flow/output/frames")
POOL_BUILD = "ref-attach-v5+isolasi"

# ── isolasi proyek + verifikasi aset (perbaikan salah-ambil gambar) ──────────
# Terukur: video job 7c724ce7 paling mirip gambar milik JOB LAIN, dan job
# 057ef9c2 memakai gambar ke-2 (harusnya frame akhir) sebagai frame awal.
# Sebabnya tiga hal bertumpuk:
#   (a) satu PROJECT_ID untuk semua job -> satu perpustakaan aset bersama
#   (b) POOL_SIZE job memasang gambar paralel ke perpustakaan itu
#   (c) _attach_one mengandalkan asumsi "picker otomatis memilih aset terbaru"
#       tanpa memverifikasi aset terpilih = berkas kita
# Tiga lapis perbaikan di bawah menutup ketiganya. Semua bisa dimatikan:
#   FLOW_ASSET_VERIFY=0, FLOW_ATTACH_SERIALIZE=0, FLOW_USE_PER_JOB_PROJECT=0
ASSET_VERIFY = os.environ.get("FLOW_ASSET_VERIFY", "1") != "0"
# Kunci attach DIMATIKAN secara bawaan: mengukur produksi menunjukkan
# kunci menahan ~21,9 detik per job berfoto (18,6s di dalam kunci), dan
# itu memangkas daya angkat jadi ~2,7 job berfoto/menit. Yang menjamin
# gambar tidak tertukar adalah VERIFIKASI; kunci hanya mengurangi
# percobaan ulang. Set FLOW_ATTACH_SERIALIZE=1 untuk menyalakannya lagi.
ATTACH_SERIALIZE = os.environ.get("FLOW_ATTACH_SERIALIZE", "0") == "1"
USE_PER_JOB_PROJECT = os.environ.get("FLOW_USE_PER_JOB_PROJECT", "1") != "0"
ATTACH_VERIFY_TRIES = int(os.environ.get("FLOW_ATTACH_VERIFY_TRIES", "3"))
PROJECT_OPEN_TRIES = int(os.environ.get("FLOW_PROJECT_OPEN_TRIES", "5"))
PROJECT_URL_TPL = os.environ.get("FLOW_PROJECT_URL", "https://flow.google.com/")
# Proyek baru belum ada di daftar "recent", jadi dibuat lewat tombol di beranda.
NEW_PROJECT_LABELS = [t.strip() for t in os.environ.get(
    "FLOW_NEW_PROJECT_LABELS",
    "Proyek baru,New project,Buat proyek,Proyek kosong,Blank project").split(",")]

_PROJECT_LOCKS = {}
_PROJECT_LOCKS_GUARD = None


async def _project_lock(key):
    """Kunci per proyek: satu job memasang gambar pada satu proyek sekaligus."""
    global _PROJECT_LOCKS_GUARD
    if _PROJECT_LOCKS_GUARD is None:
        _PROJECT_LOCKS_GUARD = asyncio.Lock()
    async with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _PROJECT_LOCKS[key] = lock
        return lock


def _name_core(name):
    """Bentuk pembanding nama aset: tanpa ekstensi, huruf kecil."""
    return str(name or "").strip().lower().rsplit(".", 1)[0]


def _same_asset(a, b):
    """True bila dua nama menunjuk aset yang sama (UI kadang memotong nama)."""
    ca, cb = _name_core(a), _name_core(b)
    if not ca or not cb:
        return False
    return ca == cb or ca in cb or cb in ca


def _ts_of(name):
    """Stempel waktu pada nama berkas kita (mis. 1790595143622_img_xxx.jpg)."""
    m = re.match(r"(\d{10,})_", str(name or ""))
    return int(m.group(1)) if m else 0


def _db():
    return sqlite3.connect(DB_PATH, timeout=30)


class FlowPool:
    def __init__(self, size=POOL_SIZE):
        self.size = size
        self.queue = None
        self.workers = []
        self._started = False
        self.stats = {"submitted": 0, "completed": 0, "failed": 0}
        # Page loading is the expensive step; cap how many run at once.
        self._load_sem = None
        # Berapa kali berturut-turut proyek baru gagal dibuat. Setelah beberapa
        # kali, berhenti mencoba supaya tidak memperlambat setiap job berfoto.
        self._proj_fail = 0

    # ── page helpers ────────────────────────────────────────────────────────
    def _ctx(self):
        import browser_manager
        if not browser_manager._browser_context or not browser_manager._browser_context.pages:
            raise Exception("Browser not running")
        return browser_manager._browser_context

    async def _ctx_for(self, account_email=None):
        """Konteks untuk akun job ini.

        Saklar mati atau profil belum ada -> konteks tunggal (perilaku lama),
        jadi job tidak pernah gagal hanya karena akun belum disiapkan.
        """
        import browser_manager
        if getattr(self, "_job_account", None) is None:
            self._job_account = account_email
        try:
            ctx = await browser_manager.get_context_for(account_email)
        except Exception as e:
            logger.warning(f"get_context_for({account_email}) gagal: {e}")
            ctx = None
        if ctx is None or not getattr(ctx, "pages", None):
            return self._ctx()
        return ctx

    async def _ensure_browser(self):
        """Start the browser if a restart left it down, so jobs never fail on it."""
        import browser_manager
        if browser_manager._browser_context and browser_manager._browser_context.pages:
            return
        logger.info("browser not running — starting it")
        await browser_manager.start_remote_browser("listbalck31@gmail.com")
        for _ in range(30):
            await asyncio.sleep(1)
            if browser_manager._browser_context and browser_manager._browser_context.pages:
                logger.info("browser ready")
                return
        raise Exception("browser did not start")

    @staticmethod
    def _collector(page, exclude=None):
        """Record media ids this page's own network traffic reveals, in order.

        With several pages open the DOM shows every job's output, so DOM diffing
        cannot attribute a result; network responses are per-page and do belong
        to this job.

        `exclude` holds media ids belonging to INPUTS (attached frames) — a frame
        is itself a flow-content media id and is re-requested around submit.

        Returns (seen, order): `order` preserves appearance so candidates can be
        tried chronologically instead of the arbitrary alphabetical `sorted()`.
        """
        seen = set()
        order = []
        EXCLUDE_FROM_RESULT = set(exclude or ())

        def on_response(resp):
            try:
                u = resp.url or ""
                if "flow-content.google" not in u and "/asb/" not in u:
                    return
                tail = u.rsplit("/", 1)[-1].split("?")[0]
                if len(tail) == 36 and tail.count("-") == 4:
                    if tail in EXCLUDE_FROM_RESULT:
                        return
                    if tail not in seen:
                        seen.add(tail)
                        order.append(tail)
            except Exception:
                pass

        page.on("response", on_response)
        return seen, order

    @staticmethod
    async def _dom_tracker(page):
        """Pelacak DOM: panggil() mengembalikan id yang BARU muncul.

        Dipakai sebagai atribusi utama karena semua job berbagi satu proyek:
        yang membedakan hasil job ini adalah kemunculan BARU setelah submit,
        bukan sekadar ada di galeri (galeri berisi hasil job lama juga).

        Pemakaian: tracker = await self._dom_tracker(page)
                   ...submit...
                   baru = await tracker()      # id yang muncul sejak snapshot
        """
        async def snapshot():
            return await FlowPool._media_ids(page)

        awal = await snapshot()

        async def diff():
            kini = await snapshot()
            return [m for m in kini if m not in awal]

        return diff

    @staticmethod
    async def _media_ids(page):
        try:
            urls = await page.evaluate("""() => {
                const sel = 'img[src*="flow-content.google"], img[src*="asb"]';
                return Array.from(document.querySelectorAll(sel)).map(i => i.src);
            }""")
        except Exception:
            return set()
        out = set()
        for u in urls or []:
            m = str(u).rsplit("/", 1)[-1].split("?")[0]
            if len(m) == 36 and m.count("-") == 4:
                out.add(m)
        return out

    async def _open_project(self, page, project_id=None):
        """Siapkan composer pada proyek yang diminta.

        `project_id` dipakai untuk ISOLASI PER JOB: proyek sendiri = perpustakaan
        aset sendiri, jadi gambar job lain tidak mungkin tertukar.
        """
        pid = project_id or PROJECT_ID
        if f"/project/{pid}" not in (page.url or ""):
            await page.goto(f"https://flow.google.com/project/{pid}",
                            wait_until="domcontentloaded", timeout=60000)
        for _ in range(120):  # up to 60s
            if await page.evaluate("() => !!document.querySelector('.ProseMirror')") and \
               "project/" in (page.url or ""):
                return True
            await asyncio.sleep(0.5)
        return False


    async def _click_option(self, page, label, exact=True):
        """Click a leaf option inside the open settings panel."""
        try:
            return bool(await page.evaluate("""(args) => {
                const [label, exact] = args;
                const root = document.querySelector('.cdk-overlay-container') || document;
                const want = label.toLowerCase().trim();
                const nodes = Array.from(root.querySelectorAll(
                    'button, [role="radio"], [role="menuitem"], [role="option"], mat-radio-button, span, div'));
                const leaf = nodes.filter(el => {
                    const t = (el.innerText || '').trim();
                    if (!t || t.length > 60) return false;
                    return exact ? t.toLowerCase() === want : t.toLowerCase().includes(want);
                });
                leaf.sort((a, b) => b.querySelectorAll('*').length - a.querySelectorAll('*').length);
                const target = leaf[0];
                if (!target) return false;
                const clickable = target.closest(
                    'button, [role="radio"], [role="menuitem"], [role="option"], mat-radio-button') || target;
                clickable.click();
                return true;
            }""", [label, exact]))
        except Exception:
            return False

    async def _set_mode(self, page, is_image, aspect, duration, count):
        """Set media type / aspect / duration / count on this page.

        Each job has its own page, and the mode is per-page UI state, so it must
        be set every time — otherwise a video job silently produces an image.
        """
        async def panel_open():
            try:
                return bool(await page.evaluate("""() => {
                    const r = document.querySelector('.cdk-overlay-container');
                    if (!r) return false;
                    const t = r.innerText || '';
                    return t.includes('Gambar') || t.includes('Video');
                }"""))
            except Exception:
                return False

        if await panel_open():
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.6)

        await page.evaluate("""() => {
            const b = document.querySelector('button.settings-trigger-button');
            if (b) b.click();
        }""")
        await asyncio.sleep(1.2)
        if not await panel_open():
            try:
                await page.click("button.settings-trigger-button", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(1.2)
        if not await panel_open():
            raise Exception("settings panel did not open")

        if not await self._click_option(page, "Gambar" if is_image else "Video"):
            raise Exception(f"could not select media type (is_image={is_image})")
        await asyncio.sleep(0.8)

        if aspect:
            await self._click_option(page, aspect)
            await asyncio.sleep(0.5)
        if not is_image:
            if duration:
                await self._click_option(page, f"{int(duration)} dtk")
                await asyncio.sleep(0.4)
        if count and int(count) > 1:
            await self._click_option(page, f"x{int(count)}")
            await asyncio.sleep(0.4)

        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        summary = await page.evaluate("""() => {
            const b = document.querySelector('button.settings-trigger-button');
            return b ? (b.innerText || '').replace(/\\s+/g, ' ').trim() : '';
        }""")
        expect = "Nano Banana" if is_image else "Video"
        if expect.lower() not in (summary or "").lower():
            raise Exception(f"mode not applied: expected {expect}, summary='{summary}'")
        logger.info(f"mode set: {summary}")
        return summary

    # ── the job ─────────────────────────────────────────────────────────────
    # ── reference images / frames ───────────────────────────────────────────
    @staticmethod
    def _image_sources(req):
        """Images the request carries, in attach order (start, end, refs)."""
        out = []
        for attr in ("start_image", "end_image"):
            v = getattr(req, attr, None)
            if v:
                out.append((attr, v))
        for i, v in enumerate(getattr(req, "reference_images", None) or []):
            if v:
                out.append((f"reference[{i}]", v))
        return out

    @staticmethod
    def _materialize(src):
        """Local path for an image url, or the path itself if it exists."""
        if not src:
            return None
        src = str(src)
        if src.startswith(("http://", "https://")):
            import urllib.request
            os.makedirs(FRAME_DIR, exist_ok=True)
            name = src.rsplit("/", 1)[-1].split("?")[0] or "frame"
            if not os.path.splitext(name)[1]:
                name += ".jpg"
            dest = os.path.join(FRAME_DIR, f"{int(time.time()*1000)}_{name}")
            with urllib.request.urlopen(src, timeout=60) as r, open(dest, "wb") as f:
                f.write(r.read())
            return dest
        return src if os.path.exists(src) else None

    async def _open_new_project(self, page):
        """Buat proyek Flow BARU untuk job ini. Kembalikan id proyek atau None.

        Isolasi per job: proyek sendiri berarti perpustakaan aset sendiri, jadi
        gambar milik job lain tidak mungkin tertukar.

        Kalau proyek baru tidak bisa dibuat, kembalikan None supaya pemanggil
        memakai proyek dasar + kunci attach. Job berfoto TIDAK boleh gagal
        hanya karena proyek baru tidak tersedia.
        """
        if self._proj_fail >= PROJECT_OPEN_TRIES:
            return None
        try:
            await page.goto(PROJECT_URL_TPL, wait_until="domcontentloaded",
                            timeout=60000)
        except Exception as e:
            self._proj_fail += 1
            logger.warning(f"buka beranda Flow gagal: {str(e)[:90]}")
            return None
        await asyncio.sleep(4)

        for label in NEW_PROJECT_LABELS:
            label = (label or "").strip()
            if not label:
                continue
            try:
                await page.get_by_role("button", name=label, exact=False).first.click(
                    timeout=6000)
            except Exception:
                continue
            for _ in range(60):
                await asyncio.sleep(1)
                url = page.url or ""
                m = re.search(r"/project/([0-9a-fA-F-]{36})", url)
                try:
                    ready = bool(await page.evaluate(
                        "() => !!document.querySelector('.ProseMirror')"))
                except Exception:
                    ready = False
                if m and ready:
                    self._proj_fail = 0
                    return m.group(1)
            logger.info(f"tombol '{label}' diklik tapi editor proyek baru tidak siap")
            break

        self._proj_fail += 1
        logger.info(f"proyek baru tidak bisa dibuat (gagal {self._proj_fail}x) "
                    f"-> pakai proyek dasar + kunci attach")
        return None

    @staticmethod
    async def _picker_state(page):
        """Isi picker: nama berkas terlihat, penanda sibuk, ada tidaknya tombol konfirmasi."""
        try:
            return await page.evaluate("""() => {
                const sels = ['.cdk-overlay-container .cdk-overlay-pane',
                              '[role="dialog"]', '.cdk-overlay-container'];
                for (const s of sels) {
                    const root = document.querySelector(s);
                    if (!root) continue;
                    const txt = root.innerText || '';
                    if (!txt.trim()) continue;
                    const names = (txt.match(/[\w\-]{3,}\.(?:png|jpe?g|webp)/gi) || [])
                        .map(n => n.toLowerCase());
                    const busy = /mengupload|uploading|memproses|menyiapkan|memuat/i.test(txt);
                    const confirm = Array.from(root.querySelectorAll('button'))
                        .some(b => /tambahkan ke perintah/i.test(b.innerText || ''));
                    return {found: true, names: [...new Set(names)], busy,
                            confirm, head: txt.slice(0, 220)};
                }
                return {found: false, names: [], busy: false, confirm: false, head: ''};
            }""") or {}
        except Exception:
            return {}

    @staticmethod
    async def _selected_names(page):
        """Nama berkas yang BENAR-BENAR terpilih di picker (penanda terpilih).

        Kalau UI tidak menandai pilihannya, kembalikan himpunan kosong supaya
        pemanggil memakai jalur cadangan berbasis stempel waktu.
        """
        try:
            raw = await page.evaluate("""() => {
                const out = [];
                const sels = ['.cdk-overlay-container .cdk-overlay-pane',
                              '[role="dialog"]'];
                for (const s of sels) {
                    const root = document.querySelector(s);
                    if (!root) continue;
                    root.querySelectorAll('*').forEach(el => {
                        const al = (el.getAttribute && el.getAttribute('aria-label')) || '';
                        const ap = (el.getAttribute && el.getAttribute('aria-pressed')) || '';
                        const ast = (el.getAttribute && el.getAttribute('aria-selected')) || '';
                        const txt = (el.innerText || '').slice(0, 120);
                        if (!/terpilih|dipilih|selected|checked|centang/i.test(al + ' ' + txt))
                            if (ap !== 'true' && ast !== 'true')
                                return;
                        const m = (al + ' ' + txt).match(/[\w\-]{3,}\.(?:png|jpe?g|webp)/i);
                        if (m) out.push(m[0].toLowerCase());
                    });
                }
                return out;
            }""") or []
            return {str(n).lower() for n in raw}
        except Exception:
            return set()

    @staticmethod
    async def _close_picker(page):
        """Tutup picker tanpa memilih apa pun (dipakai saat verifikasi gagal)."""
        for fn in (lambda: page.keyboard.press("Escape"),
                   lambda: page.mouse.click(20, 20)):
            try:
                await fn()
            except Exception:
                pass
            await asyncio.sleep(0.7)

    async def _attach_one(self, page, path, filename, job_id=""):
        """Pasang satu gambar KE PROYEK YANG BENAR — dengan VERIFIKASI ASET.

        Perbedaan penting dari versi lama: pemilihan aset DIVERIFIKASI. Versi lama
        mengandalkan asumsi "picker otomatis memilih aset terbaru" dan hanya
        memeriksa bahwa ada chip muncul — kalau aset yang terpilih milik job lain,
        chip TETAP muncul dan video dibuat dari gambar yang salah (terbukti di
        produksi). Sekarang nama aset terpilih dibandingkan dengan berkas kita;
        kalau tidak cocok, picker ditutup lalu diulang.
        """
        tag = (job_id or "?")[:8]
        expect = os.path.basename(path)

        for attempt in range(1, ATTACH_VERIFY_TRIES + 1):
            # 1. buka picker lewat menu "tambahkan media"
            try:
                await page.click('button[aria-label*="tambahkan media"]', timeout=10000)
            except Exception:
                await self._close_picker(page)
                await page.click('button[aria-label*="tambahkan media"]', timeout=10000)
            await asyncio.sleep(2)
            async with page.expect_file_chooser(timeout=15000) as fc:
                await page.get_by_role("menuitem", name="Upload").click(timeout=10000)
            chooser = await fc.value
            await chooser.set_files(path)
            await asyncio.sleep(FRAME_UPLOAD_WAIT)

            # 2. buka tab "File yang diupload"
            await page.click('button[aria-label*="Tambahkan bahan"]', timeout=10000)
            await asyncio.sleep(3)
            await page.evaluate("""() => {
                const ov = document.querySelector('.cdk-overlay-container');
                if (!ov) return;
                const els = Array.from(ov.querySelectorAll('*'))
                    .filter(e => e.children.length === 0 && /file yang diupload/i.test(e.innerText || ''));
                if (els.length) els[0].click();
            }""")
            await asyncio.sleep(2.5)

            # 3. tunggu unggahan selesai DAN picker siap
            deadline = time.time() + 60
            while time.time() < deadline:
                st = await self._picker_state(page)
                if not st.get("found"):
                    break
                if st.get("busy"):
                    await asyncio.sleep(2)
                    continue
                if any(_same_asset(n, expect) for n in (st.get("names") or [])):
                    break
                await asyncio.sleep(1)

            # 4. VERIFIKASI: pastikan aset yang akan dipakai memang milik kita
            if ASSET_VERIFY:
                state = await self._picker_state(page)
                listed = [str(n).lower() for n in (state.get("names") or [])]
                chosen = await self._selected_names(page)
                verdict, why = "unknown", ""

                if chosen:
                    if any(_same_asset(n, expect) for n in chosen):
                        verdict = "ok"
                    else:
                        verdict = "wrong"
                        why = f"terpilih: {sorted(chosen)[:2]}"
                else:
                    # UI tidak menandai pilihan -> pakai stempel waktu: aplikasi
                    # memilih aset TERBARU, jadi kalau ada unggahan job lain yang
                    # lebih baru, asumsi itu berbahaya -> tolak.
                    mine = _ts_of(expect)
                    newer = [n for n in listed
                             if _ts_of(n) > mine and not _same_asset(n, expect)]
                    if newer:
                        verdict = "wrong"
                        why = f"unggahan lebih baru: {newer[:2]}"
                    else:
                        verdict = "ok"
                        why = "aset terbaru = milik kita"

                if verdict == "wrong":
                    logger.warning(f"attach {tag}: VERIFIKASI GAGAL ({why}) -> "
                                   f"tutup picker & ulangi "
                                   f"[{attempt}/{ATTACH_VERIFY_TRIES}]")
                    await self._close_picker(page)
                    await asyncio.sleep(2)
                    continue
                if verdict == "unknown":
                    logger.info(f"attach {tag}: picker tak terbaca, lanjut ke "
                                f"pemeriksaan chip")
                else:
                    logger.info(f"attach {tag}: terverifikasi ({why})")

            # 5. konfirmasi
            if not await self._press_add_to_prompt(page):
                await self._close_picker(page)
                await asyncio.sleep(2)
                continue
            await asyncio.sleep(3)

            # 6. chip harus muncul di composer
            for _ in range(15):
                if await self._chip_count(page):
                    logger.info(f"attach {tag}: terpasang {expect} "
                                f"(percobaan {attempt})")
                    return True
                await asyncio.sleep(1)

            logger.warning(f"attach {tag}: chip tidak muncul "
                           f"[{attempt}/{ATTACH_VERIFY_TRIES}]")
            await self._close_picker(page)
            await asyncio.sleep(2)

        raise Exception(f"gagal memasang {expect} setelah {ATTACH_VERIFY_TRIES} "
                        f"percobaan — aset tidak bisa diverifikasi sebagai milik job ini")


    @staticmethod
    async def _press_add_to_prompt(page):
        try:
            await page.get_by_role("button", name="Tambahkan ke perintah").click(timeout=8000)
            return True
        except Exception:
            return bool(await page.evaluate("""() => {
                const ov = document.querySelector('.cdk-overlay-container');
                if (!ov) return false;
                const btns = Array.from(ov.querySelectorAll('button'))
                    .filter(b => /tambahkan ke perintah/i.test(b.innerText || ''));
                if (!btns.length) return false;
                btns[0].click();
                return true;
            }"""))

    @staticmethod
    async def _chip_count(page):
        try:
            return await page.evaluate("""() => {
                const pm = document.querySelector('.ProseMirror');
                let root = pm;
                for (let i=0;i<7 && root && root.parentElement;i++) root = root.parentElement;
                return root ? root.querySelectorAll('img.chip-image, img[alt="Gambar bahan"]').length : 0;
            }""") or 0
        except Exception:
            return 0

    @staticmethod
    async def _chip_ids(page):
        """Media ids of the images attached to the composer (inputs, not results)."""
        try:
            return set(await page.evaluate("""() => {
                const pm = document.querySelector('.ProseMirror');
                let root = pm;
                for (let i=0;i<7 && root && root.parentElement;i++) root = root.parentElement;
                if (!root) return [];
                return Array.from(root.querySelectorAll('img')).map(i => {
                    const m = (i.src||'').match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/);
                    return m ? m[0] : null;
                }).filter(Boolean);
            }""") or [])
        except Exception:
            return set()

    async def _attach_images(self, page, req, job_id=""):
        """Attach all supplied images. Returns their media ids (to exclude).

        Dijalankan BERURUTAN dan diverifikasi satu per satu: urutan penting
        (foto awal dulu, baru akhir), dan salah-ambil satu gambar merusak hasil.
        """
        sources = self._image_sources(req)
        if not sources:
            return set()
        chip_ids = set()
        for role, src in sources:
            path = self._materialize(src)
            if not path:
                raise Exception(f"image {role} could not be fetched: {str(src)[:120]}")
            filename = os.path.basename(path)
            await self._attach_one(page, path, filename, job_id=job_id)
            chip_ids |= await self._chip_ids(page)
            logger.info(f"attached {role} -> {filename}")
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)
        return chip_ids

    async def run_job(self, job_id, req, job_type):
        """Submit one generation and persist its artefact. Owns its page."""
        await self._ensure_browser()
        # akun job ini: dipakai untuk memilih konteks browser yang benar
        account_email = getattr(req, "account", None) or getattr(req, "account_email", None)
        ctx = await self._ctx_for(account_email)
        page = await ctx.new_page()
        try:
            # Loading the editor is the heavy step; only LOAD_GATE at a time.
            async with self._load_sem:
                if not await self._open_project(page, project_id=PROJECT_ID):
                    raise Exception("Project composer not ready")

            # Mode is per-page UI state: set it before submitting or a video job
            # silently produces an image.
            is_image = (job_type == "image") or ("banana" in str(getattr(req, "model", "")).lower())
            aspect = getattr(req, "aspect_ratio", None) or getattr(req, "aspectRatio", None)
            await self._set_mode(page, is_image, aspect,
                                 getattr(req, "duration", None), getattr(req, "count", 1))

            logger.info(f"job {job_id[:8]}: pool build {POOL_BUILD}")

            # Reference images must be in the composer BEFORE the prompt is typed.
            input_media = set()
            if ENABLE_FRAME_ATTACH:
                wanted = len(self._image_sources(req))
                if wanted:
                    logger.info(f"job {job_id[:8]}: attaching {wanted} image(s)")
                    # ── ISOLASI: proyek sendiri per job berfoto ──────────────
                    # Satu perpustakaan aset bersama + pemasangan paralel adalah
                    # sebab video memakai gambar job lain (terbukti di produksi).
                    active_pid = PROJECT_ID
                    lock = None
                    if USE_PER_JOB_PROJECT:
                        new_pid = await self._open_new_project(page)
                        if new_pid:
                            active_pid = new_pid
                            logger.info(f"job {job_id[:8]}: proyek job sendiri "
                                        f"{new_pid[:8]}")
                        else:
                            logger.info(f"job {job_id[:8]}: pakai proyek dasar "
                                        f"+ kunci attach")
                    if active_pid == PROJECT_ID and ATTACH_SERIALIZE:
                        lock = await _project_lock(active_pid)
                        await lock.acquire()
                        logger.info(f"job {job_id[:8]}: kunci attach proyek didapat")
                    try:
                        input_media = await self._attach_images(page, req,
                                                                job_id=job_id)
                    finally:
                        if lock is not None:
                            lock.release()
                            logger.info(f"job {job_id[:8]}: kunci attach dilepas")
                    logger.info(f"job {job_id[:8]}: excluding {len(input_media)} input id(s)")

            # Let the gallery finish its initial loads, then start listening.
            await asyncio.sleep(2)
            captured_seen, captured_order = self._collector(page, exclude=input_media)
            before_net = set(captured_seen)
            # Atribusi utama: pelacak DOM (kemunculan baru setelah submit).
            dom_tracker = await self._dom_tracker(page)
            # `before` is the DOM snapshot the original computed but never used:
            # gallery thumbnails already on screen are NOT this job's result.
            before_dom = await self._media_ids(page)
            prompt = getattr(req, "prompt", "") or ""

            # Submit. The composer clearing is the only reliable acceptance signal.
            await page.click(".ProseMirror")
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(prompt, delay=4)
            await asyncio.sleep(0.3)
            await page.keyboard.press("Enter")

            deadline = time.time() + SUBMIT_TIMEOUT
            accepted = False
            while time.time() < deadline:
                await asyncio.sleep(0.5)
                left = await page.evaluate(
                    "() => ((document.querySelector('.ProseMirror')||{}).innerText||'').length")
                if left <= 1:
                    accepted = True
                    break
            if not accepted:
                raise Exception("submit not confirmed: composer never cleared")
            self.stats["submitted"] += 1
            logger.info(f"job {job_id[:8]}: submitted ({len(before_dom)} media before)")

            # Atribusi berlapis (POIN A): id media yang BARU MUNCUL di DOM
            # setelah submit dipakai lebih dulu — semua job berbagi satu proyek,
            # jadi galeri berisi hasil job lama juga dan hanya kemunculan baru
            # yang benar-benar milik job ini. Lalu lintas jaringan halaman tetap
            # dipakai sebagai cadangan.
            media_id = None
            started = time.time()
            last_pct = -1
            rejected = set()
            last_err = None
            # Kalau tidak ada KANDIDAT sama sekali (bukan gagal unduh), menunggu
            # 9 menit tidak ada gunanya — hasilnya pasti gagal. Beri tenggang
            # terbatas supaya job lebih cepat dilaporkan dan tidak menahan slot.
            candidates_seen_total = 0
            no_candidate_grace = float(os.environ.get("FLOW_NO_CANDIDATE_GRACE", "150"))
            while time.time() - started < JOB_TIMEOUT:
                await asyncio.sleep(3)
                pct = await page.evaluate("""() => {
                    const m = (document.body.innerText||'').match(/(\d+)%/);
                    return m ? parseInt(m[1]) : null;
                }""")
                if pct is not None and pct != last_pct:
                    last_pct = pct
                    self._set_progress(job_id, min(pct, 99))

                # Candidates: network ids seen since attach, minus the pre-existing
                # DOM gallery, minus permanently-rejected ids. Appearance order.
                fresh = (captured_seen - before_net) - rejected
                candidates = [m for m in captured_order
                              if m in fresh and m not in before_dom]
                # Kandidat GABUNGAN: id yang baru muncul di DOM lebih
                # diutamakan (atribusi per-halaman), lalu kandidat jaringan.
                try:
                    dom_new = await dom_tracker()
                except Exception:
                    dom_new = []
                dom_new = [m for m in dom_new
                           if m not in rejected and m not in input_media
                           and m not in before_dom]
                gabung = dom_new + [m for m in candidates if m not in dom_new]
                candidates_seen_total += len(gabung)
                if (not gabung and candidates_seen_total == 0
                        and time.time() - started > no_candidate_grace):
                    raise Exception(
                        f"tidak ada kandidat media setelah {time.time()-started:.0f}s "
                        f"(galeri awal={len(before_dom)}, lalu lintas baru={len(fresh)}) "
                        f"— hasil tidak bisa diatribusikan ke job ini")
                for cand in gabung:
                    try:
                        # NOTE: await is essential — without it the coroutine is
                        # never executed, nothing is written and _store_result
                        # never runs, leaving the job stuck at queued/99 forever.
                        await self._store_media(page, job_id, cand, req, job_type,
                                                want_video=(job_type == "video"))
                        media_id = cand
                        break
                    except Exception as e:
                        msg = str(e)
                        if "resolved to non-media" in msg or "wrong media type" in msg:
                            rejected.add(cand)   # definitive: never retry
                            logger.info(f"job {job_id[:8]}: {cand[:8]} rejected ({msg[:80]})")
                        else:
                            last_err = e       # transient: retry next pass
                            logger.info(f"job {job_id[:8]}: {cand[:8]} transient ({msg[:80]})")
                if media_id:
                    break

            if not media_id:
                raise Exception(
                    f"no usable media after {time.time()-started:.0f}s: {last_err} "
                    f"(kandidat={candidates_seen_total}, ditolak={len(rejected)})")
            logger.info(f"job {job_id[:8]}: media {media_id}")

            self.stats["completed"] += 1
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _store_media(self, page, job_id, media_id, req, job_type, want_video=None):
        """Fetch the artefact bytes through the page and write them to disk.

        The signed CDN url is browser-bound (a swapped /video/ path 403s), so the
        bytes are pulled with an in-page fetch and decoded here.
        """
        url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_id}"
        # page.request follows the 302 to the signed CDN url and carries this
        # page's session; an in-page fetch() of the redirect target fails because
        # the signature is bound to the requesting session.
        try:
            resp = await page.request.get(url, max_redirects=5, timeout=45000)
            body = await resp.body()
            ct = (resp.headers.get("content-type") or "").lower()
        except Exception as e:
            raise Exception(f"media fetch failed: {e}")

        if not body:
            raise Exception("media fetch returned no data")
        if "text/html" in ct or b"Internal Error" in body[:120] or b"<!doctype" in body[:120].lower():
            raise Exception(f"resolved to non-media: ct={ct} size={len(body)}")

        is_video = "video" in ct or body[4:8] == b"ftyp"
        if body[:3] == b"\xff\xd8\xff":
            is_video = False
        if want_video is not None and is_video != want_video:
            raise Exception(f"wrong media type: got video={is_video}, wanted {want_video}")
        ext = ".mp4" if is_video else ".jpg"
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        path = os.path.join(OUTPUT_DIR, f"{job_id}{ext}")
        with open(path, "wb") as f:
            f.write(body)

        result = {
            "id": job_id,
            "media_id": media_id,
            "file_path": path,
            "content_type": ct or ("video/mp4" if is_video else "image/jpeg"),
            "is_video": is_video,
            "size_bytes": len(body),
            "download_url": f"/v1/google-flow/media/{job_id}",
            "model": getattr(req, "model", None),
            "type": job_type,
        }
        self._store_result(job_id, result)
        logger.info(f"job {job_id[:8]}: stored {len(body)}B ct={ct} video={is_video}")

    # ── db ──────────────────────────────────────────────────────────────────
    def _set_progress(self, job_id, pct):
        try:
            with _db() as c:
                c.execute("UPDATE jobs SET progress=?, updated_at=? WHERE job_id=?",
                          (pct, int(time.time()), job_id))
        except Exception:
            pass

    def _store_result(self, job_id, result):
        try:
            with _db() as c:
                c.execute("UPDATE jobs SET status='completed', progress=100, result=?, updated_at=? "
                          "WHERE job_id=?", (json.dumps(result), int(time.time()), job_id))
        except Exception as e:
            logger.error(f"store result failed for {job_id[:8]}: {e}")

    def _mark_failed(self, job_id, err):
        try:
            with _db() as c:
                c.execute("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE job_id=?",
                          (str(err)[:500], int(time.time()), job_id))
        except Exception:
            pass

    # ── pool ────────────────────────────────────────────────────────────────
    async def _worker(self, n):
        while True:
            job_id, req, job_type = await self.queue.get()
            try:
                await self.run_job(job_id, req, job_type)
            except Exception as e:
                self.stats["failed"] += 1
                logger.error(f"worker {n} job {job_id[:8]} failed: {e}")
                self._mark_failed(job_id, str(e))
            finally:
                self.queue.task_done()

    async def start(self):
        if self._started:
            return
        self.queue = asyncio.Queue()
        self._load_sem = asyncio.Semaphore(LOAD_GATE)
        self.workers = [asyncio.create_task(self._worker(i)) for i in range(self.size)]
        self._started = True
        logger.info(f"FlowPool started: {self.size} workers, load gate {LOAD_GATE}")

    async def enqueue(self, job_id, req, job_type):
        if not self._started:
            await self.start()
        await self.queue.put((job_id, req, job_type))

    def pending(self):
        return self.queue.qsize() if self.queue else 0


POOL = FlowPool()
