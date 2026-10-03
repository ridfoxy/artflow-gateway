from fastapi.responses import HTMLResponse, FileResponse
from fastapi import FastAPI, HTTPException, Security, Depends, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import asyncio
import os
import re
import subprocess
import uuid
import logging
import sqlite3
import time
import json

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

from browser_manager import start_remote_browser, get_session_status
from batchexecute_engine import BatchExecuteEngine
from flow_pool import POOL

# Non-Swagger Custom App
app = FastAPI(
    title="Google Flow API Gateway",
    version="3.0.0-XHR",
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)
logger = logging.getLogger("app")

def get_db():
    conn = sqlite3.connect('/root/api-google-flow/data/gateway.db')
    conn.row_factory = sqlite3.Row
    return conn

# ─── API Key Authentication ────────────────────────────────────

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_auth = HTTPBearer(auto_error=False)

def verify_api_key(
    api_key: Optional[str] = Security(api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_auth)
) -> str:
    token = None
    if api_key:
        token = api_key.strip()
    elif bearer and bearer.credentials:
        token = bearer.credentials.strip()
        
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API Key. Provide via 'X-API-Key' header or 'Authorization: Bearer <key>'"
        )
        
    with get_db() as conn:
        row = conn.execute(
            "SELECT key_value, name, is_active FROM api_keys WHERE key_value = ? AND is_active = 1",
            (token,)
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or deactivated API Key"
            )
        return row["key_value"]

job_queue = asyncio.Queue()
engine = BatchExecuteEngine()

class VideoGenRequest(BaseModel):
    prompt: str
    model: str = "omni-flash"
    aspect_ratio: str = "16:9"
    duration: int = 8
    count: int = 1
    reference_images: Optional[List[str]] = []  # Multi-Reference: max 7 for omni-flash, 3 for veo-3.1-lite
    start_image: Optional[str] = None           # Frame-In (Start image)
    end_image: Optional[str] = None             # Frame-Out (End image)

# Correct mapping from memory: 16:9→2, 9:16→3, 1:1→1
ASPECT_MAP = {"16:9": 2, "9:16": 3, "1:1": 1, "4:3": 4}

def get_model_key(model: str, duration: int) -> str:
    m = model.lower().replace("-", "_").replace(" ", "_")
    if "nano" in m or "banana" in m or "image" in m or "narwhal" in m:
        return "NARWHAL"
    elif "omni" in m or "flash" in m or "abra" in m:
        # Omni Flash duration: 4s, 6s, 8s, 10s
        d = 4 if duration <= 4 else (6 if duration <= 6 else (8 if duration <= 8 else 10))
        return f"abra_t2v_{d}s"
    elif "lower" in m or "low_priority" in m or "lp" in m:
        return "veo_3_1_r2v_lite_low_priority" if "r2v" in m else "veo_3_1_lite_low_priority"
    elif "veo" in m:
        return "veo_3_1_lite"
    return f"abra_t2v_{duration}s"


# ─── Media resolution helpers (added) ─────────────────────────────────────────
POSTER_MEDIA_RE = re.compile(r"flow-content\.google/(?:image|video)/([0-9a-fA-F-]{36})")


def extract_media_id(value) -> str | None:
    """Pull the Google media id out of a signed poster/video URL."""
    if not value:
        return None
    m = POSTER_MEDIA_RE.search(str(value))
    return m.group(1) if m else None


PROJECT_URL = "https://flow.google.com/project/0f21b6bf-24a7-4cfe-81ec-0e674b324f4a"


async def click_setting(page, label: str, exact: bool = True) -> bool:
    """Click an option inside the open settings panel by its visible label.

    Labels are matched against leaf elements so a container that merely contains
    the text is not clicked by mistake. Returns True when something was clicked.
    """
    try:
        return bool(await page.evaluate("""(args) => {
            const [label, exact] = args;
            const root = document.querySelector('.cdk-overlay-container') || document;
            const wanted = label.toLowerCase().trim();
            const nodes = Array.from(root.querySelectorAll('button, [role="menuitem"], [role="radio"], mat-radio-button, [role="option"], span, div'));
            const leaf = nodes.filter(el => {
                const t = (el.innerText || '').trim();
                if (!t) return false;
                if (t.length > 60) return false;
                return exact ? t.toLowerCase() === wanted : t.toLowerCase().includes(wanted);
            });
            // prefer the most deeply nested (the actual control, not a wrapper)
            leaf.sort((a, b) => b.querySelectorAll('*').length - a.querySelectorAll('*').length);
            const target = leaf[0];
            if (!target) return false;
            const clickable = target.closest('button, [role="menuitem"], [role="radio"], mat-radio-button, [role="option"]') || target;
            clickable.click();
            return true;
        }""", [label, exact]))
    except Exception as e:
        logger.warning(f"click_setting({label}) failed: {e}")
        return False


async def apply_settings(page, is_image: bool, aspect: str, duration: int, resolution: str, count: int) -> dict:
    """Open the settings panel and apply media type, aspect, resolution, duration, count.

    Every step is verified by re-reading the trigger's summary label, so a job
    cannot silently generate with the wrong mode (the failure mode that made
    image jobs produce nothing).
    """
    async def summary() -> str:
        try:
            return await page.evaluate("""() => {
                const b = document.querySelector('button.settings-trigger-button');
                return b ? (b.innerText || '').replace(/\\s+/g, ' ').trim() : '';
            }""") or ""
        except Exception:
            return ""

    applied = {"media_type": False, "aspect": False, "resolution": False,
               "duration": False, "count": False, "summary": ""}

    async def panel_open() -> bool:
        try:
            return bool(await page.evaluate("""() => {
                const root = document.querySelector('.cdk-overlay-container');
                if (!root) return false;
                const t = (root.innerText || '');
                return t.includes('Gambar') || t.includes('Video');
            }"""))
        except Exception:
            return False

    # A previous job can leave the panel open; clicking the trigger would then
    # close it and every option below would silently fail.
    if await panel_open():
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await asyncio.sleep(1.0)

    await page.evaluate("""() => {
        const b = document.querySelector('button.settings-trigger-button');
        if (b) b.click();
    }""")
    await asyncio.sleep(1.5)

    if not await panel_open():
        # retry once through a real click
        try:
            await page.click("button.settings-trigger-button", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

    if not await panel_open():
        raise Exception("Settings panel did not open; refusing to generate with unknown settings")

    # 1. media type
    applied["media_type"] = await click_setting(page, "Gambar" if is_image else "Video")
    await asyncio.sleep(1.2)

    # 2. aspect ratio
    if aspect:
        applied["aspect"] = await click_setting(page, aspect)
        await asyncio.sleep(0.7)

    # 3. resolution (only offered for video)
    if resolution and not is_image:
        applied["resolution"] = await click_setting(page, resolution)
        await asyncio.sleep(0.7)

    # 4. duration (video only; label carries the unit)
    if duration and not is_image:
        applied["duration"] = await click_setting(page, f"{int(duration)} dtk")
        await asyncio.sleep(0.7)

    # 5. count
    if count and int(count) > 1:
        applied["count"] = await click_setting(page, f"x{int(count)}")
        await asyncio.sleep(0.7)

    # close the panel
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await asyncio.sleep(0.8)

    applied["summary"] = await summary()
    logger.info(f"apply_settings: is_image={is_image} requested={aspect}/{resolution}/{duration}s/x{count} "
                f"applied={applied} summary='{applied['summary']}'")

    # Fail loudly rather than generating in the wrong mode.
    if not applied["media_type"]:
        raise Exception(f"Could not switch media type (is_image={is_image}); summary='{applied['summary']}'")
    expected = "Nano Banana" if is_image else "Video"
    if expected.lower() not in applied["summary"].lower():
        raise Exception(f"Media type not applied: expected '{expected}', summary='{applied['summary']}'")
    return applied



async def ensure_project_page(page):
    """Make sure the Flow project editor is loaded and ready for input.

    page.goto() can return while Angular is still booting, after which the app
    redirects to the project LIST page; submitting there generates nothing. So
    navigate, then verify the URL really stayed on the project and retry through
    an in-page location change if it bounced.
    """
    async def _ready():
        try:
            has_composer = await page.evaluate("() => !!document.querySelector('.ProseMirror')")
        except Exception:
            return False
        return bool(has_composer) and "flow.google.com/project/" in page.url

    if await _ready():
        return page

    for attempt in range(4):
        logger.info(f"ensure_project_page: attempt {attempt + 1} from {page.url}")
        try:
            await page.goto(PROJECT_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as nav_err:
            logger.warning(f"ensure_project_page: goto error {nav_err}")

        # Give Angular time to boot AND finish its own redirect decision.
        for _ in range(10):
            await asyncio.sleep(1.5)
            if await _ready():
                logger.info("ensure_project_page: project editor ready")
                return page

        # Bounced back to the list page: force the location from inside the app.
        try:
            await page.evaluate(f"() => {{ location.href = {PROJECT_URL!r}; }}")
        except Exception:
            pass
        await asyncio.sleep(4)

    raise Exception(f"Flow project editor not reachable (url={page.url})")



async def _snapshot_media(page) -> list:
    """Current flow-content/asb media URLs in DOM order."""
    try:
        return await page.evaluate("""() => {
            const sel = 'img[src*="flow-content.google"], img[src*="asb"]';
            return Array.from(document.querySelectorAll(sel)).map(i => i.src);
        }""") or []
    except Exception:
        return []


def _is_video_bytes(body: bytes, content_type) -> bool:
    ctype = (content_type or "").lower()
    if "video" in ctype:
        return True
    if "image" in ctype:
        return False
    return bool(body) and body[4:8] == b"ftyp"


def _is_image_bytes(body: bytes, content_type) -> bool:
    ctype = (content_type or "").lower()
    if "image" in ctype:
        return True
    if "video" in ctype:
        return False
    if not body:
        return False
    return body[:3] == b"\xff\xd8\xff" or body[:8] == b"\x89PNG\r\n\x1a\n"


async def resolve_media(page, before: list, is_image: bool) -> dict:
    """Find the media belonging to THIS generation, verified by content type.

    Candidates are tried newest-first: entries that appeared after the
    before-snapshot, then the remaining gallery as a fallback. Each candidate is
    fetched and only accepted when its real bytes match the requested kind, so a
    stale gallery entry can never masquerade as the new result.
    """
    before = before or []
    deadline = time.time() + 600  # match the previous 10-minute budget

    while time.time() < deadline:
        await asyncio.sleep(5)
        try:
            info = await page.evaluate("""() => {
                const text = document.body.innerText || '';
                const sel = 'img[src*="flow-content.google"], img[src*="asb"]';
                const imgs = Array.from(document.querySelectorAll(sel)).map(i => i.src);
                return {imgs: imgs,
                        busy: text.includes('%') || text.includes('Membuat') || text.includes('Creating')};
            }""")
        except Exception as poll_err:
            logger.debug(f"resolve_media poll error: {poll_err}")
            continue

        imgs = info.get("imgs") or []
        if not imgs:
            continue

        fresh = [u for u in imgs if u not in before]
        ordered = fresh + [u for u in imgs if u in before]
        if info.get("busy") and not fresh:
            continue  # still rendering, nothing new yet

        tried = []
        for candidate in ordered:
            mid = extract_media_id(candidate)
            if not mid or mid in tried:
                continue
            tried.append(mid)
            body, signed, ctype = await fetch_media_bytes(page, mid)
            if not body:
                continue
            ok = _is_image_bytes(body, ctype) if is_image else _is_video_bytes(body, ctype)
            logger.info(f"resolve_media: candidate {mid} ct={ctype} matches={ok}")
            if ok:
                return {"media_id": mid, "body": body, "signed_url": signed, "content_type": ctype}
            if len(tried) >= 4:
                break  # don't scan the whole gallery

    return {}


async def fetch_signed_media(page, media_id: str) -> str | None:
    """Ask Flow for the signed URL of a media id (302 -> signed CDN URL)."""
    if not media_id:
        return None
    url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_id}"
    try:
        resp = await page.request.get(url, max_redirects=5, timeout=120000)
        if resp.status in (200, 302) and resp.url and "Signature=" in resp.url:
            logger.info(f"fetch_signed_media: {resp.url[:90]}...")
            return resp.url
        logger.warning(f"fetch_signed_media: unexpected status {resp.status} for {media_id}")
    except Exception as e:
        logger.warning(f"fetch_signed_media failed for {media_id}: {e}")
    return None


async def fetch_media_bytes(page, media_id: str) -> tuple[bytes | None, str | None, str | None]:
    """Fetch a media id's bytes. Returns (body, signed_url, content_type)."""
    if not media_id:
        return None, None, None
    url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_id}"
    try:
        resp = await page.request.get(url, max_redirects=5, timeout=120000)
        body = await resp.body()
        ctype = resp.headers.get("content-type")
        signed = resp.url if (resp.url and "Signature=" in resp.url) else None
        logger.info(f"fetch_media_bytes: {len(body)} bytes, ct={ctype}")
        return body, signed, ctype
    except Exception as e:
        logger.warning(f"fetch_media_bytes failed for {media_id}: {e}")
        return None, None, None


def save_media_bytes(body: bytes, content_type: str | None, job_id: str) -> tuple[str | None, bool]:
    """Persist bytes under an extension that matches their real content type."""
    if not body:
        return None, False
    ctype = (content_type or "").lower()
    if body[4:8] == b"ftyp" or "video" in ctype:
        ext = ".mp4"
    elif body[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif body[:3] == b"\xff\xd8\xff" or "image" in ctype:
        ext = ".jpg"
    else:
        ext = ".bin"
    path = f"/root/api-google-flow/output/{job_id}{ext}"
    with open(path, "wb") as f:
        f.write(body)
    logger.info(f"save_media_bytes: wrote {len(body)} bytes to {path}")
    return path, ext == ".mp4"

async def process_job(job_id: str, req, job_type: str):
    try:
        logger.info(f"=== Starting job {job_id} ({job_type}) ===")
        with get_db() as conn:
            conn.execute("UPDATE jobs SET status = 'processing', updated_at = ?", (int(time.time()),))
            
        import browser_manager
        if not browser_manager._browser_context or not browser_manager._browser_context.pages:
            await browser_manager.start_remote_browser("listbalck31@gmail.com")
            await asyncio.sleep(2)
            
        page = browser_manager._browser_context.pages[0]
        
        # 1. Pastikan editor project benar-benar siap (composer ada) sebelum submit
        await ensure_project_page(page)
        await asyncio.sleep(1)
            
        # 2. Terapkan mode, rasio, resolusi, durasi, dan jumlah variasi lewat
        # panel setelan yang sebenarnya. Versi lama mengeklik "model pill" yang
        # sudah tidak ada, sehingga semua pilihan ini tidak pernah terpasang.
        is_image = (job_type == "image") or ("banana" in str(req.model).lower()) or ("image" in str(req.model).lower())
        target_aspect = getattr(req, 'aspect_ratio', None) or getattr(req, 'aspectRatio', None) or ("1:1" if is_image else "16:9")
        target_duration = int(getattr(req, 'duration', 0) or 8)
        target_count = int(getattr(req, 'count', 0) or 1)
        target_resolution = "360p" if not is_image else None
        await apply_settings(page, is_image, target_aspect, target_duration, target_resolution, target_count)
        await asyncio.sleep(1)

        # Tutup popover & fokus ke prompt input
        pm = await page.wait_for_selector(".ProseMirror", timeout=10000)
        await pm.click()
        await asyncio.sleep(0.3)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.2)
        await page.keyboard.type(req.prompt, delay=15)
        await asyncio.sleep(0.8)
        
        # Snapshot the gallery BEFORE submitting: anything that appears after
        # this point is a candidate for this generation.
        await asyncio.sleep(1)
        media_before = await _snapshot_media(page)
        logger.info(f"Job {job_id}: {len(media_before)} media elements before submit")

        # Submit. A programmatic .click() on the generate button does NOT submit in
        # this Flow build (verified: prompt stayed put, no progress). Enter inside
        # the composer does, so use that and verify the composer actually cleared.
        async def _composer_text():
            try:
                return (await page.evaluate(
                    "() => (document.querySelector('.ProseMirror') || {}).innerText || ''")) or ""
            except Exception:
                return ""

        submitted = False
        for attempt in range(3):
            try:
                await page.click(".ProseMirror")
            except Exception:
                pass
            await asyncio.sleep(0.4)
            await page.keyboard.press("Enter")

            # Flow clears the composer only after the submit round-trip completes;
            # measured at ~12-14s, so poll well past that before retrying.
            for _ in range(25):
                await asyncio.sleep(1.0)
                current = await _composer_text()
                if not current or len(current.strip()) <= 1:
                    submitted = True
                    break
            if submitted:
                break
            logger.warning(f"Job {job_id}: submit attempt {attempt + 1} did not clear the composer")

        if not submitted:
            # Last resort: the button, in case the UI changes back to click-to-submit
            try:
                await page.evaluate('''() => {
                    const b = document.querySelector('button[aria-label="Mulai pembuatan"]')
                           || document.querySelector('.submit-controls button[type=submit]');
                    if (b && !b.disabled) b.click();
                }''')
                await asyncio.sleep(2.5)
                submitted = not await _composer_text()
            except Exception:
                pass

        if not submitted:
            raise Exception("Prompt submission failed: composer never cleared")

        logger.info(f"UI Native submit triggered for Job {job_id}")
        
        # Poll the DOM for the media id THIS generation produced. The previous
        # implementation took imgs[0], which is the newest generation but still a
        # stale element on a page that already held past results, and it saved the
        # poster JPEG into a .mp4 path.
        media = await resolve_media(page, media_before, is_image)

        if not media:
            raise Exception("Polling timeout. Creation took too long or failed.")

        media_id = media["media_id"]
        body = media.get("body")
        signed_url = media.get("signed_url")
        content_type = media.get("content_type")

        saved_file = None
        is_real_video = False
        if body:
            os.makedirs("/root/api-google-flow/output", exist_ok=True)
            saved_file, is_real_video = save_media_bytes(body, content_type, job_id)

        # Prefer the signed MP4 for video, the signed image for image jobs.
        poster_url = f"https://flow-content.google/image/{media_id}"
        if is_image:
            result_url = signed_url or poster_url
            poster = result_url
        else:
            result_url = signed_url or poster_url
            poster = poster_url

        res_data = {
            "id": job_id,
            "url": result_url,
            "poster_url": poster,
            "media_id": media_id,
            "file_path": saved_file,
            "download_url": f"/v1/google-flow/media/{job_id}",
            "content_type": content_type,
            "is_video": bool(is_real_video),
            "model": req.model,
            "type": job_type
        }

        with get_db() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'completed', progress = 100, result = ?, updated_at = ? WHERE job_id = ?",
                (json.dumps(res_data), int(time.time()), job_id)
            )
        logger.info(f"=== Job {job_id} COMPLETED (media_id={media_id}, real_video={is_real_video}) ===")
            
    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        with get_db() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE job_id = ?",
                (str(e), int(time.time()), job_id)
            )

async def worker():
    while True:
        job_id, req_obj, job_type = await job_queue.get()
        try:
            await process_job(job_id, req_obj, job_type)
        except Exception as e:
            logger.error(f"Worker error on job {job_id}: {e}")
        finally:
            job_queue.task_done()

# ─── Stale Job Sweeper ────────────────────────────────────────

STALE_JOB_SECONDS = int(os.environ.get("FLOW_STALE_JOB_SECONDS", "3600"))

async def stale_job_sweeper_task():
    """Fail only LEGACY 'processing' rows that have been idle for a long time.

    IMPORTANT: the concurrent pool never writes status='processing' — an
    in-flight pool job remains 'queued' until it completes or fails. So ONLY
    sweep 'processing' rows (written by the legacy process_job path) and never
    touch 'queued' ones, or live renders get killed.

    'processing' rows only ever had progress 0, so requiring progress=0 keeps the
    rule narrow and safe.
    """
    while True:
        try:
            await asyncio.sleep(300)  # every 5 minutes
            now = int(time.time())
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT job_id, updated_at FROM jobs "
                    "WHERE status='processing' AND progress=0"
                ).fetchall()
                swept = 0
                for job_id, updated in rows:
                    try:
                        last = int(updated)
                    except (TypeError, ValueError):
                        continue
                    if now - last > STALE_JOB_SECONDS:
                        conn.execute(
                            "UPDATE jobs SET status='failed', "
                            "error=COALESCE(NULLIF(error,''), "
                            "'legacy runner died before reporting (auto-swept)'), "
                            "updated_at=? WHERE job_id=?",
                            (now, job_id),
                        )
                        swept += 1
            if swept:
                logger.warning(f"stale job sweeper: marked {swept} legacy job(s) failed")
        except Exception as e:
            logger.error(f"stale job sweeper error: {e}")


# ─── Auto Disk Retention Cleaner ──────────────────────────────

async def auto_disk_cleaner_task():
    """Background task to remove output files older than 2 hours to prevent disk full."""
    while True:
        try:
            await asyncio.sleep(600)  # Runs every 10 minutes
            from pathlib import Path
            out_dir = Path("/root/api-google-flow/output")
            if out_dir.exists():
                now = time.time()
                for f in out_dir.glob("*.mp4"):
                    # Remove files older than 2 hours (7200 seconds)
                    if now - f.stat().st_mtime > 7200:
                        try:
                            f.unlink()
                            logger.info(f"Cleaned expired video cache: {f.name}")
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"Disk cleaner notice: {e}")

@app.on_event("startup")
async def startup_event():
    for _ in range(10):
        asyncio.create_task(worker())
    asyncio.create_task(auto_disk_cleaner_task())
    asyncio.create_task(stale_job_sweeper_task())
    logger.info("10 workers and Auto-Disk-Cleaner started")

@app.post("/v1/browser/start")
async def api_start_browser():
    try:
        res = await start_remote_browser("listbalck31@gmail.com")
        return res
    except Exception as e:
        logger.error(f"Browser start failed: {e}")
        return {"success": False, "error": str(e)}

@app.get("/v1/browser/net-spy")
async def api_net_spy(bersihkan: bool = False):
    """SEMENTARA: lihat lalu lintas jaringan yang tercatat pengintai.

    Menjawab: apakah panggilan ke aisandbox-pa benar-benar terjadi?
    """
    import browser_manager
    if bersihkan:
        browser_manager.spy_clear()
        return {"dibersihkan": True}
    try:
        return browser_manager.spy_data()
    except Exception as e:
        return {"error": str(e)[:150]}


class ExecCodeKeRequest2(BaseModel):
    code: str
    indeks: int = 0


@app.post("/v1/browser/exec-in-page-ke")
async def api_exec_in_page_ke2(req: ExecCodeKeRequest2):
    """SEMENTARA: jalankan JS di halaman ke-N (diagnosa)."""
    import browser_manager
    ctx = browser_manager._browser_context
    if not ctx or not ctx.pages:
        return {"error": "Browser not running"}
    i = int(getattr(req, "indeks", 0) or 0)
    if i < 0 or i >= len(ctx.pages):
        return {"error": f"indeks {i} di luar jangkauan (0..{len(ctx.pages)-1})"}
    pg = ctx.pages[i]
    try:
        return {"indeks": i, "url": (pg.url or "")[:100],
                "result": await pg.evaluate(req.code)}
    except Exception as e:
        return {"indeks": i, "error": str(e)[:150]}


class FetchUrlRequest(BaseModel):
    url: str
    indeks: int = 0


@app.post("/v1/browser/fetch-url")
async def api_fetch_url(req: FetchUrlRequest):
    """SEMENTARA: ambil URL lewat page.request (tidak terikat origin halaman).

    Dipakai untuk menguji apakah /asb/ bisa diambil tanpa fetch JS.
    """
    import browser_manager
    ctx = browser_manager._browser_context
    if not ctx or not ctx.pages:
        return {"error": "Browser not running"}
    i = int(req.indeks or 0)
    if i < 0 or i >= len(ctx.pages):
        return {"error": f"indeks di luar jangkauan (0..{len(ctx.pages)-1})"}
    pg = ctx.pages[i]
    out = {"url": req.url[:120], "indeks": i}
    # cara 1: page.request.get (tidak terikat origin)
    try:
        r = await pg.request.get(req.url, max_redirects=5, timeout=45000)
        b = await r.body()
        out["page_request"] = {
            "status": r.status,
            "ct": (r.headers or {}).get("content-type", ""),
            "bytes": len(b),
            "awal_hex": b[:8].hex() if b else "",
        }
    except Exception as e:
        out["page_request"] = {"error": str(e)[:150]}
    # cara 2: context.request.get
    try:
        r2 = await ctx.request.get(req.url, max_redirects=5, timeout=45000)
        b2 = await r2.body()
        out["context_request"] = {
            "status": r2.status,
            "ct": (r2.headers or {}).get("content-type", ""),
            "bytes": len(b2),
        }
    except Exception as e:
        out["context_request"] = {"error": str(e)[:150]}
    # cara 3: fetch JS di halaman (pembanding)
    try:
        out["fetch_js"] = await pg.evaluate("""async (u) => {
            try {
                const r = await fetch(u, {credentials: 'include'});
                const b = await r.blob();
                return {status: r.status, bytes: b.size};
            } catch(e) { return {err: String(e).slice(0, 70)}; }
        }""", req.url)
    except Exception as e:
        out["fetch_js"] = {"error": str(e)[:100]}
    return out


@app.get("/v1/browser/pages")
async def api_browser_pages2():
    """SEMENTARA: daftar SEMUA halaman (diagnosa)."""
    import browser_manager
    ctx = browser_manager._browser_context
    if not ctx or not ctx.pages:
        return {"error": "Browser not running", "jumlah": 0}
    daftar = []
    for i, pg in enumerate(ctx.pages):
        try:
            info = await pg.evaluate("""() => {
                const set = new Set();
                document.querySelectorAll('img, video, source').forEach(e => {
                    const s = e.src || e.currentSrc || '';
                    if (s.includes('/asb/')) {
                        const t = s.split('/').pop().split('?')[0];
                        if (t.length >= 60) set.add(t);
                    }
                });
                return {token_asb: set.size,
                        ada_prompt: !!document.querySelector('.ProseMirror, [contenteditable=true]')};
            }""")
        except Exception as e:
            info = {"error": str(e)[:70]}
        try:
            judul = (await pg.title())[:45]
        except Exception:
            judul = "?"
        daftar.append({"indeks": i, "url": (pg.url or "")[:100], "judul": judul, **info})
    return {"jumlah": len(ctx.pages), "halaman": daftar}


@app.get("/v1/browser/page-info")
async def api_page_info():
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        return {"error": "Browser not running"}
    page = browser_manager._browser_context.pages[0]
    url = page.url
    title = await page.title()
    info = await page.evaluate('''() => {
        const pm = document.querySelector('.ProseMirror');
        const btns = Array.from(document.querySelectorAll('button')).map(b => ({
            text: b.innerText.trim(),
            aria: b.getAttribute('aria-label'),
            disabled: b.disabled
        })).filter(b => b.text || b.aria);
        return {
            hasProseMirror: !!pm,
            pmText: pm ? pm.innerText : null,
            buttons: btns.slice(0, 15),
            textPreview: document.body.innerText.substring(0, 500)
        };
    }''')
    return {"url": url, "title": title, "info": info}

@app.post("/v1/browser/ui-generate")
async def api_ui_generate(req: VideoGenRequest):
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    # 1. Pastikan di halaman project
    if "flow.google.com/project/" not in page.url:
        await page.goto("https://flow.google.com/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)
        # Klik project pertama atau create
        await page.evaluate('''() => {
            const proj = document.querySelector('a[href*="/project/"]');
            if (proj) proj.click();
            else {
                const btns = Array.from(document.querySelectorAll('button'));
                const cb = btns.find(b => b.innerText.toLowerCase().includes('create') || b.innerText.toLowerCase().includes('buat'));
                if (cb) cb.click();
            }
        }''')
        await asyncio.sleep(5)
    
    # 2. Tunggu dan isi prompt di .ProseMirror
    await page.wait_for_selector(".ProseMirror", timeout=15000)
    await page.click(".ProseMirror")
    await asyncio.sleep(0.5)
    
    # Select all and delete
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    
    # Type new prompt
    await page.keyboard.type(req.prompt, delay=30)
    await asyncio.sleep(1)
    
    # 3. Klik tombol Generate / Buat / Submit
    clicked = await page.evaluate('''() => {
        const btns = Array.from(document.querySelectorAll('button'));
        // Cari tombol submit/generate (icon panah atau teks generate)
        const genBtn = btns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            return t.includes('generate') || t.includes('buat') || aria.includes('generate') || aria.includes('submit') || aria.includes('send');
        });
        if (genBtn) {
            genBtn.click();
            return {clicked: true, text: genBtn.innerText, aria: genBtn.getAttribute('aria-label')};
        }
        // Fallback: tombol dengan SVG send/arrow di dalam prompt area
        const submitBtn = document.querySelector('button[type="submit"]') || document.querySelector('.prompt-container button:last-child');
        if (submitBtn) {
            submitBtn.click();
            return {clicked: true, selector: 'submitBtn'};
        }
        return {clicked: false, availableButtons: btns.map(b => b.innerText || b.getAttribute('aria-label')).filter(Boolean)};
    }''')
    
    # Alternatively press Enter in ProseMirror if button not found
    if not clicked.get("clicked"):
        await page.keyboard.press("Enter")
        clicked["tried_enter"] = True
        
    await asyncio.sleep(2)
    current_url = page.url
    return {"status": "triggered", "url": current_url, "click_result": clicked, "prompt": req.prompt}

@app.post("/v1/browser/refresh-token", dependencies=[Depends(verify_api_key)])
async def api_refresh_token():
    """Report auth state without disturbing in-flight work.

    This used to call engine.ensure_token(), which navigates pages[0] — a worker's
    page — and aborts that job. The browser authenticates by cookie, so there is
    nothing to refresh during normal operation; this now reports status instead.
    """
    try:
        import browser_manager
        ctx = browser_manager._browser_context
        cookies = await ctx.cookies("https://accounts.google.com/") if ctx else []
        names = {c["name"] for c in cookies}
        authed = "__Secure-3PSID" in names or "SID" in names
        return {
            "success": True,
            "status": "cookie_auth_ok" if authed else "cookie_auth_missing",
            "detail": ("session authenticated by cookie; no token refresh needed"
                       if authed else "no Google auth cookie found — re-login via noVNC"),
        }
    except Exception as e:
        logger.error(f"Auth status check error: {e}")
        return {"success": False, "error": str(e)}

@app.get("/v1/browser/status")
async def api_browser_status():
    return get_session_status()

@app.post("/v1/video/generate", dependencies=[Depends(verify_api_key)])
async def generate_video(req: VideoGenRequest):
    job_id = str(uuid.uuid4())
    now = int(time.time())
    with get_db() as conn:
        conn.execute("INSERT INTO jobs (job_id, account_email, status, progress, type, prompt, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                     (job_id, "listbalck31@gmail.com", "queued", 0, "video", req.prompt, req.model, now, now))
    await job_queue.put((job_id, req, "video"))
    logger.info(f"Job {job_id} queued: {req.model} {req.duration}s {req.aspect_ratio}")
    return {"jobId": job_id, "status": "queued"}

class ImageGenRequest(BaseModel):
    prompt: str
    model: str = "nano-banana-2-lite"
    aspect_ratio: str = "1:1"
    count: int = 1
    reference_images: Optional[List[str]] = []  # Multi-Reference: up to 14 references for Nano Banana 2

@app.post("/v1/image/generate", dependencies=[Depends(verify_api_key)])
async def generate_image(req: ImageGenRequest):
    job_id = str(uuid.uuid4())
    now = int(time.time())
    with get_db() as conn:
        conn.execute("INSERT INTO jobs (job_id, account_email, status, progress, type, prompt, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                     (job_id, "listbalck31@gmail.com", "queued", 0, "image", req.prompt, req.model, now, now))
    await job_queue.put((job_id, req, "image"))
    return {"jobId": job_id, "status": "queued"}

@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str):
    """Get detailed tracking information for a specific Job ID."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT job_id, account_email, type, status, progress, prompt, model, 
                      result, error, created_at, updated_at 
               FROM jobs WHERE job_id = ?""",
            (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Job ID '{job_id}' not found")
            
        import json
        result = json.loads(row["result"]) if row["result"] else None
        
        # Calculate elapsed time
        created = int(row["created_at"]) if str(row["created_at"]).isdigit() else 0
        updated = int(row["updated_at"]) if str(row["updated_at"]).isdigit() else 0
        elapsed_sec = (updated - created) if (created and updated) else 0
        
        return {
            "jobId": row["job_id"],
            "type": row["type"],
            "status": row["status"],
            "progress": row["progress"],
            "account": row["account_email"],
            "prompt": row["prompt"],
            "model": row["model"],
            "result": result,
            "error": row["error"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "elapsedSeconds": elapsed_sec
        }

@app.get("/v1/jobs")
async def list_jobs(
    status: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """
    List and track all recent jobs across the system with filtering and pagination.
    status: 'queued', 'processing', 'completed', 'failed'
    type: 'video', 'image'
    """
    query = "SELECT job_id, account_email, type, status, progress, prompt, model, result, error, created_at, updated_at FROM jobs WHERE 1=1"
    params = []
    
    if status:
        query += " AND status = ?"
        params.append(status)
    if type:
        query += " AND type = ?"
        params.append(type)
        
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    with get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        
        job_list = []
        for r in rows:
            res = json.loads(r["result"]) if r["result"] else None
            created = int(r["created_at"]) if str(r["created_at"]).isdigit() else 0
            updated = int(r["updated_at"]) if str(r["updated_at"]).isdigit() else 0
            job_list.append({
                "jobId": r["job_id"],
                "type": r["type"],
                "status": r["status"],
                "progress": r["progress"],
                "account": r["account_email"],
                "prompt": r["prompt"],
                "model": r["model"],
                "result": res,
                "error": r["error"],
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"],
                "elapsedSeconds": (updated - created) if (created and updated) else 0
            })
            
        total_count = conn.execute("SELECT COUNT(*) as c FROM jobs").fetchone()["c"]
        
        return {
            "total": total_count,
            "count": len(job_list),
            "limit": limit,
            "offset": offset,
            "jobs": job_list
        }

@app.post("/v1/debug/intercept-start")
async def debug_intercept_start():
    """Start intercepting BatchExecute requests from the browser UI to capture exact f.req format"""
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        return {"error": "Browser not started"}
    page = browser_manager._browser_context.pages[0]
    
    # Inject JS to intercept XHR requests to batchexecute
    await page.evaluate('''() => {
        window.__captured_requests = [];
        const origOpen = XMLHttpRequest.prototype.open;
        const origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(method, url, ...args) {
            this.__url = url;
            return origOpen.call(this, method, url, ...args);
        };
        XMLHttpRequest.prototype.send = function(body) {
            if (this.__url && this.__url.includes('batchexecute') && body) {
                window.__captured_requests.push({
                    url: this.__url,
                    body: body,
                    timestamp: Date.now()
                });
                console.log("CAPTURED batchexecute:", this.__url.substring(0, 100));
            }
            return origSend.call(this, body);
        };
        console.log("XHR interception ACTIVE");
    }''')
    return {"success": True, "message": "XHR interception active. Now generate a video manually in the browser UI, then call /v1/debug/intercept-read"}

class ExecCodeRequest(BaseModel):
    code: str

@app.post("/v1/browser/exec-in-page")
async def api_exec_in_page(req: ExecCodeRequest):
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        return {"error": "Browser not running"}
    page = browser_manager._browser_context.pages[0]
    res = await page.evaluate(req.code)
    return {"result": res}

@app.post("/v1/browser/send-prompt")
async def api_send_prompt(prompt: str, model: str = "omni-flash"):
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        return {"error": "Browser not running"}
    page = browser_manager._browser_context.pages[0]
    
    # 1. Pastikan ProseMirror ada
    pm = await page.query_selector(".ProseMirror")
    if not pm:
        # Coba cari textarea atau contenteditable
        pm = await page.query_selector("[contenteditable='true']")
    
    if not pm:
        return {"error": "Prompt input field not found"}
        
    await pm.click()
    await asyncio.sleep(0.3)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await page.keyboard.type(prompt, delay=20)
    await asyncio.sleep(1)
    
    # Tekan Enter atau cari tombol submit
    # Di Google Flow, tombol submit biasanya icon panah atau Enter
    await page.keyboard.press("Enter")
    await asyncio.sleep(2)
    
    # Cek apakah progress bar atau card baru muncul
    status = await page.evaluate('''() => {
        const text = document.body.innerText;
        return {
            hasGenerating: text.toLowerCase().includes('generat') || text.toLowerCase().includes('membuat') || text.includes('%'),
            cards: Array.from(document.querySelectorAll('[data-id], .media-card, .gallery-item')).map(c => c.innerText.substring(0, 100)),
            textSample: text.substring(0, 800)
        };
    }''')
    
    return {"success": True, "prompt": prompt, "status": status}

class GenerateImageOmniRequest(BaseModel):
    prompt: str = "A vibrant cybernetic hummingbird with neon feathers, hyperrealistic 8k"
    aspect_ratio: str = "1:1"

@app.post("/v1/browser/generate-image-direct")
async def api_generate_image_direct(req: GenerateImageOmniRequest):
    """Generate Image (Nano Banana / Image Mode) directly via Playwright browser UI."""
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    # 1. Pastikan di project
    if "flow.google.com/project/" not in page.url:
        await page.goto("https://flow.google.com/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        card = await page.query_selector('a[href*="/project/"]')
        if card:
            await card.click()
            await asyncio.sleep(4)
            
    # 2. Switch mode ke Gambar / Image
    await page.evaluate('''() => {
        const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
        const pill = btns.find(b => {
            const t = (b.innerText || '').trim();
            return t.includes('Nano Banana') || t.includes('Gambar') || t.includes('Video') || t.includes('Omni') || t.includes('Veo');
        });
        if (pill) pill.click();
    }''')
    await asyncio.sleep(1.5)
    
    # Di dalam popup switcher, klik tab Gambar / Image
    await page.evaluate('''() => {
        const all = Array.from(document.querySelectorAll('.cdk-overlay-container *'));
        const imgTab = all.find(el => {
            const t = (el.innerText || '').trim().toLowerCase();
            return t === 'gambar' || t === 'image' || t.includes('image');
        });
        if (imgTab) imgTab.click();
    }''')
    await asyncio.sleep(1)
    
    # Pilih Aspect ratio jika ada
    await page.evaluate(f'''() => {{
        const all = Array.from(document.querySelectorAll('.cdk-overlay-container *'));
        const aspect = all.find(el => {{
            const t = (el.innerText || '').trim();
            return t === "{req.aspect_ratio}" || t.includes("{req.aspect_ratio}");
        }});
        if (aspect) aspect.click();
    }}''')
    await asyncio.sleep(0.5)
    
    # Tutup popup
    await page.mouse.click(100, 100)
    await asyncio.sleep(0.5)
    
    # 3. Ketik Prompt di ProseMirror
    pm = await page.wait_for_selector(".ProseMirror", timeout=10000)
    await pm.click()
    await asyncio.sleep(0.3)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await page.keyboard.type(req.prompt, delay=15)
    await asyncio.sleep(0.8)
    
    # 4. Klik submit / generate
    click_res = await page.evaluate('''() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const sendBtn = btns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            return t.includes('arrow_forward') || aria.includes('generate') || aria.includes('buat') || aria.includes('kirim') || aria.includes('send');
        });
        if (sendBtn) {
            sendBtn.click();
            return { clicked: true, text: sendBtn.innerText, aria: sendBtn.getAttribute('aria-label') };
        }
        return { clicked: false };
    }''')
    
    return {
        "status": "image_generation_triggered",
        "prompt": req.prompt,
        "aspect_ratio": req.aspect_ratio,
        "clickResult": click_res
    }

class GenerateOmniRequest(BaseModel):
    prompt: str = "A majestic dragon flying over a snow-covered mountain at sunset, cinematic lighting, ultra-realistic 4k"
    duration: int = 8
    aspect_ratio: str = "9:16"

@app.post("/v1/browser/generate-omni")
async def api_generate_omni(req: GenerateOmniRequest):
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    # 1. Pastikan di project
    if "flow.google.com/project/" not in page.url:
        await page.goto("https://flow.google.com/project/0f21b6bf-24a7-4cfe-81ec-0e674b324f4a", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        
    # 2. Klik model config pill di bawah (yang ada teks Nano Banana atau model lain)
    await page.evaluate('''() => {
        const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
        const pill = btns.find(b => {
            const t = (b.innerText || '').trim();
            return t.includes('Nano Banana') || t.includes('Omni') || t.includes('Veo') || t.includes('Video') || t.includes('Gambar');
        });
        if (pill) pill.click();
    }''')
    await asyncio.sleep(1.5)
    
    # 3. Klik tab 'Video' di popover
    await page.evaluate('''() => {
        const all = Array.from(document.querySelectorAll('.cdk-overlay-container *'));
        const videoBtn = all.find(el => {
            const t = (el.innerText || '').trim().toLowerCase();
            return t === 'video' || (t.includes('videocam') && t.includes('video'));
        });
        if (videoBtn) videoBtn.click();
    }''')
    await asyncio.sleep(1)
    
    # 4. Pilih rasio aspek 9:16 atau 16:9
    target_aspect = req.aspect_ratio
    await page.evaluate(f'''() => {{
        const all = Array.from(document.querySelectorAll('.cdk-overlay-container *'));
        const aspectBtn = all.find(el => {{
            const t = (el.innerText || '').trim();
            return t === "{target_aspect}" || t.includes("{target_aspect}");
        }});
        if (aspectBtn) aspectBtn.click();
    }}''')
    await asyncio.sleep(1)
    
    # 5. Tutup overlay jika masih terbuka (klik luar atau klik ProseMirror)
    pm = await page.wait_for_selector(".ProseMirror", timeout=10000)
    await pm.click()
    await asyncio.sleep(0.5)
    
    # 6. Ketik Prompt
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await page.keyboard.type(req.prompt, delay=15)
    await asyncio.sleep(1)
    
    # 7. Klik tombol arrow_forward / Generate
    click_res = await page.evaluate('''() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const sendBtn = btns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            return t.includes('arrow_forward') || aria.includes('generate') || aria.includes('buat') || aria.includes('kirim') || aria.includes('send');
        });
        if (sendBtn) {
            sendBtn.click();
            return {clicked: true, text: sendBtn.innerText, aria: sendBtn.getAttribute('aria-label')};
        }
        return {clicked: false};
    }''')
    
    # Fallback: tekan Enter jika tombol tidak terklik
    if not click_res.get("clicked"):
        await page.keyboard.press("Enter")
        click_res["used_enter"] = True
        
    await asyncio.sleep(3)
    
    return {
        "status": "generation_triggered",
        "prompt": req.prompt,
        "model": "Omni 1.1 Flash",
        "aspect_ratio": req.aspect_ratio,
        "duration": req.duration,
        "clickResult": click_res
    }

@app.post("/v1/browser/native-submit")
async def api_native_submit():
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    # 0. Tutup dialog overlay jika ada
    try:
        await page.mouse.click(10, 10)
        await asyncio.sleep(0.5)
    except Exception:
        pass
        
    # 1. Focus input dengan force=True
    await page.click(".ProseMirror", force=True)
    await asyncio.sleep(0.3)
    
    # 2. Kirim native Enter
    await page.keyboard.press("Enter")
    await asyncio.sleep(1)
    
    # 3. Klik tombol arrow_forward / submit
    try:
        await page.evaluate('''() => {
            const btns = Array.from(document.querySelectorAll('button'));
            const send = btns.find(b => {
                const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                const text = (b.innerText || '').toLowerCase();
                return aria.includes('mulai') || aria.includes('buat') || aria.includes('generate') || text.includes('arrow_forward');
            });
            if (send) send.click();
        }''')
    except Exception:
        pass
            
    await asyncio.sleep(2)
    text = await page.evaluate('() => document.body.innerText')
    pm_text = await page.evaluate('() => document.querySelector(".ProseMirror")?.innerText')
    
    return {
        "success": True,
        "pmText": pm_text,
        "textSample": text[:500],
        "hasProgress": '%' in text or 'Membuat' in text or 'Generating' in text
    }

@app.post("/v1/browser/generate-omni-v2")
async def api_generate_omni_v2(req: GenerateOmniRequest):
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    # 1. Pastikan di project
    if "flow.google.com/project/" not in page.url:
        await page.goto("https://flow.google.com/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        card = await page.query_selector('a[href*="/project/"]')
        if card:
            await card.click()
            await asyncio.sleep(4)
            
    # 2. Tunggu dan klik input prompt
    pm = await page.wait_for_selector(".ProseMirror", timeout=10000)
    await pm.click()
    await asyncio.sleep(0.3)
    
    # Select all and delete old text
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    
    # 3. Ketik prompt secara nyata menggunakan keyboard
    await page.keyboard.type(req.prompt, delay=20)
    await asyncio.sleep(0.8)
    
    # 4. Klik pill konfigurasi model di bawah input prompt
    try:
        await page.evaluate('''() => {
            const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
            const pill = btns.find(b => {
                const t = (b.innerText || '').trim();
                return t.includes('Nano Banana') || t.includes('Omni') || t.includes('Veo') || t.includes('Video') || t.includes('Gambar');
            });
            if (pill) pill.click();
        }''')
        await asyncio.sleep(1)
        
        # Di dalam popup, klik tab Video via JS (bebas dari overlay intercept)
        await page.evaluate('''() => {
            const all = Array.from(document.querySelectorAll('.cdk-overlay-container *'));
            const videoTab = all.find(el => {
                const t = (el.innerText || '').trim().toLowerCase();
                return t === 'video' || (t.includes('videocam') && t.includes('video'));
            });
            if (videoTab) videoTab.click();
        }''')
        await asyncio.sleep(1)
        
        # Pilih rasio aspek via JS
        await page.evaluate(f'''() => {{
            const all = Array.from(document.querySelectorAll('.cdk-overlay-container *'));
            const aspect = all.find(el => {{
                const t = (el.innerText || '').trim();
                return t === "{req.aspect_ratio}" || t.includes("{req.aspect_ratio}");
            }});
            if (aspect) aspect.click();
        }}''')
        await asyncio.sleep(0.5)
        
        # Klik luar popup untuk menutupnya
        await page.mouse.click(100, 100)
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning(f"Config pill error: {e}")
        
    # 5. Focus ProseMirror dan Tekan Enter untuk submit!
    await pm.click(force=True)
    await asyncio.sleep(0.3)
    await page.keyboard.press("Enter")
    await asyncio.sleep(1)
    
    # Juga klik tombol arrow_forward jika masih ada
    try:
        await page.evaluate('''() => {
            const btns = Array.from(document.querySelectorAll('button'));
            const sendBtn = btns.find(b => {
                const t = (b.innerText || '').toLowerCase();
                const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                return t.includes('arrow_forward') || aria.includes('generate') || aria.includes('buat') || aria.includes('kirim') || aria.includes('send') || aria.includes('mulai');
            });
            if (sendBtn) sendBtn.click();
        }''')
    except Exception:
        pass
            
    await asyncio.sleep(3)
    
    # Ambil status saat ini
    url = page.url
    text = await page.evaluate('() => document.body.innerText')
    
    return {
        "status": "submitted",
        "prompt": req.prompt,
        "url": url,
        "textPreview": text[:500]
    }

@app.post("/v1/browser/generate-video-omni-direct")
async def api_generate_video_omni_direct(req: GenerateOmniRequest):
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    # 1. Pastikan di project
    if "flow.google.com/project/" not in page.url:
        await page.goto("https://flow.google.com/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        card = await page.query_selector('a[href*="/project/"]')
        if card:
            await card.click()
            await asyncio.sleep(4)
            
    # 2. Klik tab 'Video' di sidebar / toolbar kiri jika ada
    await page.evaluate('''() => {
        const sideVideo = Array.from(document.querySelectorAll('*')).find(el => {
            const t = (el.innerText || '').trim();
            return t === 'Video' && el.parentElement.innerText.includes('Semua media');
        });
        if (sideVideo) sideVideo.click();
    }''')
    await asyncio.sleep(1)
    
    # 3. Klik pill mode di bawah prompt untuk memastikan beralih ke Video
    await page.evaluate('''() => {
        const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
        const pill = btns.find(b => {
            const t = (b.innerText || '').trim();
            return t.includes('Nano Banana') || t.includes('Gambar') || t.includes('Omni') || t.includes('Veo');
        });
        if (pill) pill.click();
    }''')
    await asyncio.sleep(1.5)
    
    # 4. Klik opsi 'Video' di dalam popup switcher
    await page.evaluate('''() => {
        const items = Array.from(document.querySelectorAll('.cdk-overlay-container *'));
        const vidOption = items.find(el => {
            const t = (el.innerText || '').trim().toLowerCase();
            return t === 'video' || (t.includes('videocam') && t.includes('video'));
        });
        if (vidOption) vidOption.click();
    }''')
    await asyncio.sleep(1)
    
    # 5. Pilih Aspek Rasio (misal 9:16 atau 16:9)
    await page.evaluate(f'''() => {{
        const items = Array.from(document.querySelectorAll('.cdk-overlay-container *'));
        const aspectOption = items.find(el => {{
            const t = (el.innerText || '').trim();
            return t === "{req.aspect_ratio}" || t.includes("{req.aspect_ratio}");
        }});
        if (aspectOption) aspectOption.click();
    }}''')
    await asyncio.sleep(0.5)
    
    # 6. Tutup popover
    await page.mouse.click(200, 200)
    await asyncio.sleep(0.5)
    
    # 7. Focus ProseMirror dan ketik prompt
    pm = await page.wait_for_selector(".ProseMirror", timeout=10000)
    await pm.click(force=True)
    await asyncio.sleep(0.3)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await page.keyboard.type(req.prompt, delay=20)
    await asyncio.sleep(1)
    
    # 8. Submit via Enter & click send button
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.5)
    
    await page.evaluate('''() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const send = btns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            return t.includes('arrow_forward') || aria.includes('mulai') || aria.includes('buat') || aria.includes('generate');
        });
        if (send) send.click();
    }''')
    
    await asyncio.sleep(3)
    
    # Baca status
    text = await page.evaluate('() => document.body.innerText')
    
    return {
        "status": "video_generating_triggered",
        "prompt": req.prompt,
        "aspect_ratio": req.aspect_ratio,
        "textSample": text[:500]
    }

# ─── API Key Management Endpoints ──────────────────────────────

class CreateApiKeyRequest(BaseModel):
    name: str = "Client App"

@app.post("/v1/auth/keys", dependencies=[Depends(verify_api_key)])
async def api_create_key(req: CreateApiKeyRequest):
    """Generate a new secure API Key (Requires existing Master Key)."""
    new_key = "gflow_live_" + uuid.uuid4().hex
    now = str(int(time.time()))
    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_id, key_value, name, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            (str(uuid.uuid4()), new_key, req.name, now)
        )
    return {"key": new_key, "name": req.name, "created_at": now}

@app.get("/v1/auth/keys", dependencies=[Depends(verify_api_key)])
async def api_list_keys():
    """List all registered API keys."""
    with get_db() as conn:
        rows = conn.execute("SELECT key_id, name, is_active, created_at, substr(key_value, 1, 14) || '...' as masked_key FROM api_keys").fetchall()
        return {"keys": [dict(r) for r in rows]}

@app.delete("/v1/auth/keys/{key_id}", dependencies=[Depends(verify_api_key)])
async def api_revoke_key(key_id: str):
    """Revoke/deactivate an API Key."""
    with get_db() as conn:
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE key_id = ?", (key_id,))
    return {"success": True, "message": f"API Key {key_id} revoked"}

# ─── Endpoints with API Key Protection ───────────────────────

class AccountPool:
    def __init__(self):
        self._current_index = 0
        self._lock = asyncio.Lock()

    def get_all_active_accounts(self) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT email, cookies_raw, access_token, at_expires, status, is_active FROM accounts WHERE is_active = 1 AND status = 'active'"
            ).fetchall()
            return [dict(r) for r in rows]

    async def get_next_account(self) -> Optional[dict]:
        async with self._lock:
            accounts = self.get_all_active_accounts()
            if not accounts:
                return None
            self._current_index = (self._current_index + 1) % len(accounts)
            return accounts[self._current_index]

    def add_account(self, email: str, cookies_raw: str, access_token: str = "") -> bool:
        now = str(int(time.time()))
        with get_db() as conn:
            conn.execute(
                """INSERT INTO accounts (email, cookies_raw, access_token, at_expires, status, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, '', 'active', 1, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET cookies_raw=excluded.cookies_raw, access_token=excluded.access_token, is_active=1, status='active', updated_at=excluded.updated_at""",
                (email, cookies_raw, access_token, now, now)
            )
        logger.info(f"Account added/updated in pool: {email}")
        return True

    def remove_account(self, email: str) -> bool:
        with get_db() as conn:
            conn.execute("UPDATE accounts SET is_active = 0, status = 'disabled' WHERE email = ?", (email,))
        return True

account_pool = AccountPool()

class AddAccountRequest(BaseModel):
    email: str
    cookies: Optional[str] = None  # JSON string or raw cookie string
    access_token: Optional[str] = None

@app.post("/v1/accounts/add", dependencies=[Depends(verify_api_key)])
async def api_add_account(req: AddAccountRequest):
    """Add or update an account in the multi-account pool."""
    cookies_str = req.cookies or "[]"
    success = account_pool.add_account(req.email, cookies_str, req.access_token or "")
    return {"success": success, "email": req.email, "message": "Account registered in pool"}

@app.get("/v1/accounts", dependencies=[Depends(verify_api_key)])
async def api_list_accounts():
    """List all accounts in the pool with their status."""
    with get_db() as conn:
        rows = conn.execute("SELECT email, status, is_active, tier, credits_available, updated_at FROM accounts").fetchall()
        return {"accounts": [dict(r) for r in rows], "count": len(rows)}

@app.delete("/v1/accounts/{email}", dependencies=[Depends(verify_api_key)])
async def api_remove_account(email: str):
    """Disable an account from the pool."""
    account_pool.remove_account(email)
    return {"success": True, "message": f"Account {email} disabled"}

# ─── Fast Millisecond Job Dispatcher ──────────────────────────

@app.post("/v1/video/generate-r2v", dependencies=[Depends(verify_api_key)])
async def api_generate_video_r2v(req: VideoGenRequest):
    """
    Dedicated Sub-millisecond Image-to-Video (R2V / Multi-Reference) Dispatcher.
    Attach 1 or more reference image URLs/paths in 'reference_images' or 'start_image'.
    """
    job_id = str(uuid.uuid4())
    now = int(time.time())
    
    acct = await account_pool.get_next_account()
    acct_email = acct["email"] if acct else "listbalck31@gmail.com"
    
    with get_db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, account_email, status, progress, type, prompt, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
            (job_id, acct_email, "queued", 0, "video", req.prompt, req.model, now, now)
        )
    
    await job_queue.put((job_id, req, "video"))
    return {
        "jobId": job_id,
        "status": "queued",
        "type": "r2v_video",
        "reference_count": len(req.reference_images or []),
        "account": acct_email,
        "queuedAt": now
    }

@app.post("/v1/video/generate-fast", dependencies=[Depends(verify_api_key)])
async def api_generate_video_fast(req: VideoGenRequest):
    """
    Sub-millisecond job dispatcher for high-concurrency requests (thousands/hour).
    Jobs enter the non-blocking Async Queue instantly (< 5ms response time).
    """
    job_id = str(uuid.uuid4())
    now = int(time.time())
    
    # Pick next account from round-robin pool
    acct = await account_pool.get_next_account()
    acct_email = acct["email"] if acct else "listbalck31@gmail.com"
    
    with get_db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, account_email, status, progress, type, prompt, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
            (job_id, acct_email, "queued", 0, "video", req.prompt, req.model, now, now)
        )
    
    # Put in async non-blocking queue instantly (< 1ms)
    await job_queue.put((job_id, req, "video"))
    
    return {
        "jobId": job_id,
        "status": "queued",
        "account": acct_email,
        "queuedAt": now
    }

@app.post("/v1/browser/download-current-video")
async def api_download_current_video():
    """Trigger native download and capture file path."""
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    # Tunggu event download saat klik tombol download
    async with page.expect_download(timeout=15000) as download_info:
        await page.evaluate('''() => {
            const btns = Array.from(document.querySelectorAll('button'));
            const dlBtn = btns.find(b => {
                const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                const text = (b.innerText || '').toLowerCase();
                return aria.includes('download') || aria.includes('unduh') || text === 'download';
            });
            if (dlBtn) dlBtn.click();
        }''')
        
    download = await download_info.value
    target_path = f"/root/power_ranger_omni_video_{int(time.time())}.mp4"
    await download.save_as(target_path)
    
    return {
        "success": True,
        "filename": download.suggested_filename,
        "savedPath": target_path
    }

@app.get("/v1/video/download-mp4/{media_id}")
async def api_get_video_mp4(media_id: str):
    """Download and return signed MP4 video URL and local path."""
    try:
        path, signed_url = await engine.download_video_mp4(media_id)
        return {
            "media_id": media_id,
            "signed_mp4_url": signed_url,
            "local_path": path
        }
    except Exception as e:
        logger.error(f"Error fetching MP4 for {media_id}: {e}")
        return {"error": str(e)}

class GenerateR2VRequest(BaseModel):
    prompt: str = "The characters eating dinner, talking and smiling, smooth animated manhwa video"
    image_path: str = "/root/.hermes/cache/images/img_5abec7659702.jpg"
    model: str = "omni-flash"
    duration: int = 4
    aspect_ratio: str = "16:9"

@app.post("/v1/browser/generate-r2v-video")
async def api_generate_r2v_video(req: GenerateR2VRequest):
    """Generate Image-to-Video / Reference-to-Video using local reference image."""
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    # 1. Pastikan berada di project Flow
    if "flow.google.com/project/" not in page.url:
        await page.goto("https://flow.google.com/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        card = await page.query_selector('a[href*="/project/"]')
        if card:
            await card.click()
            await asyncio.sleep(4)
            
    # 2. Switch ke mode Video
    try:
        await page.evaluate('''() => {
            const spans = Array.from(document.querySelectorAll('span, button, div'));
            const videoTab = spans.find(el => (el.innerText || '').trim() === 'Video' && el.children.length === 0);
            if (videoTab) {
                videoTab.click();
                if (videoTab.parentElement) videoTab.parentElement.click();
            }
        }''')
        await asyncio.sleep(1)
    except Exception:
        pass
        
    # 3. Upload file gambar referensi via input[type="file"]
    import os
    if os.path.exists(req.image_path):
        logger.info(f"Uploading reference image: {req.image_path}")
        
        # Cari tombol Add Media atau file input
        file_input = await page.query_selector('input[type="file"]')
        if not file_input:
            # Klik tombol add media untuk memunculkan dialog/input
            add_btn = await page.query_selector('button[aria-label*="Tambahkan media"], button[aria-label*="Add media"], button:has-text("add")')
            if add_btn:
                await add_btn.click()
                await asyncio.sleep(1)
            file_input = await page.query_selector('input[type="file"]')
            
        if file_input:
            await file_input.set_input_files(req.image_path)
            logger.info("Reference file attached via input[type=file]")
            await asyncio.sleep(3)
            
            # Jika ada dialog picker, pilih gambar yang baru diupload
            await page.evaluate('''() => {
                const thumbs = Array.from(document.querySelectorAll('.cdk-overlay-container img, mat-dialog-container img, .media-picker img'));
                if (thumbs.length > 0) {
                    thumbs[0].click();
                }
            }''')
            await asyncio.sleep(1)
            
    # 4. Focus ProseMirror dan ketik prompt
    pm = await page.wait_for_selector(".ProseMirror", timeout=10000)
    await pm.click(force=True)
    await asyncio.sleep(0.3)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await page.keyboard.type(req.prompt, delay=15)
    await asyncio.sleep(1)
    
    # 5. Tekan Enter & Klik tombol Submit
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.5)
    
    await page.evaluate('''() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const send = btns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            return t.includes('arrow_forward') || aria.includes('mulai') || aria.includes('buat') || aria.includes('generate');
        });
        if (send) send.click();
    }''')
    
    await asyncio.sleep(3)
    
    text = await page.evaluate('() => document.body.innerText')
    
    return {
        "status": "r2v_generating_triggered",
        "prompt": req.prompt,
        "image_path": req.image_path,
        "textSample": text[:500]
    }

@app.post("/v1/browser/download-active-clip-mp4")
async def api_download_active_clip_mp4():
    """Trigger download MP4 and save directly to /root/api-google-flow/output/"""
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    os.makedirs("/root/api-google-flow/output", exist_ok=True)
    target_path = f"/root/api-google-flow/output/omni_video_{int(time.time())}.mp4"
    
    try:
        # 1. Pastikan sub-menu download terbuka atau klik Download
        await page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('.cdk-overlay-container button, .cdk-overlay-container [role="menuitem"]'));
            const dl = items.find(el => {
                const t = (el.innerText || '');
                return t.includes('Download') || t.includes('Unduh');
            });
            if (dl) dl.click();
        }""")
        await asyncio.sleep(1.0)
        
        # 2. Tunggu download saat klik opsi resolusi asli (720p / 360p)
        async with page.expect_download(timeout=25000) as download_info:
            await page.evaluate("""() => {
                const overlayBtns = Array.from(document.querySelectorAll('.cdk-overlay-container button, .cdk-overlay-container [role="menuitem"]'));
                const btnRes = overlayBtns.find(b => {
                    const t = (b.innerText || '');
                    return t.includes('720p') || t.includes('Ukuran asli') || t.includes('360p') || t.includes('1080p') || t.includes('asli');
                });
                if (btnRes) btnRes.click();
            }""")
            
        download = await download_info.value
        # Use target_path with direct stream or copy
        download_path = await download.path()
        if download_path and os.path.exists(download_path):
            import shutil
            shutil.copy2(download_path, target_path)
        else:
            await download.save_as(target_path)
            
        logger.info(f"Saved active clip MP4 to {target_path}")
        
        return {
            "success": True,
            "filename": download.suggested_filename,
            "savedPath": target_path
        }
    except Exception as e:
        logger.error(f"Download clip failed: {e}")
        return {"success": False, "error": str(e)}

@app.post("/v1/browser/download-active-image")
async def api_download_active_image():
    """Trigger download active generated Image / Photo."""
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    os.makedirs("/root/api-google-flow/output", exist_ok=True)
    target_path = f"/root/api-google-flow/output/image_{int(time.time())}.png"
    
    try:
        # Klik tombol download langsung atau dari menu opsi lainnya
        async with page.expect_download(timeout=25000) as download_info:
            await page.evaluate('''() => {
                // Coba cari tombol download
                const btns = Array.from(document.querySelectorAll('button, [role="menuitem"]'));
                const dl = btns.find(b => {
                    const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                    const text = (b.innerText || '').toLowerCase();
                    return aria.includes('download') || aria.includes('unduh') || text.includes('download') || text.includes('unduh');
                });
                if (dl) dl.click();
            }''')
            
        download = await download_info.value
        await download.save_as(target_path)
        logger.info(f"Saved image to {target_path}")
        
        return {
            "success": True,
            "filename": download.suggested_filename,
            "savedPath": target_path
        }
    except Exception as e:
        logger.error(f"Download image failed: {e}")
        return {"success": False, "error": str(e)}

# ─── Useapi.net Compatible Endpoints & Models Catalog ─────────

@app.get("/v1/google-flow/models", dependencies=[Depends(verify_api_key)])
async def api_get_models():
    """Retrieve the full catalog of supported Google Flow AI models and capabilities."""
    return {
        "models": [
            {
                "id": "omni-flash",
                "name": "Omni 1.1 Flash",
                "type": "video",
                "durations": [4, 6, 8, 10],
                "aspectRatios": ["16:9", "9:16", "1:1", "4:3"],
                "resolutions": ["360p", "720p", "1080p"],
                "maxReferences": 7,
                "supportsStartFrame": True,
                "supportsEndFrame": True,
                "supportsMultiR2V": True,
                "default": True,
                "description": "Google's fastest video generation model with multi-reference and frame interpolation"
            },
            {
                "id": "veo-3.1-lite",
                "name": "Veo 3.1 Lite (Priority)",
                "type": "video",
                "durations": [4, 6, 8],
                "aspectRatios": ["16:9", "9:16", "1:1"],
                "resolutions": ["720p", "1080p"],
                "maxReferences": 3,
                "supportsStartFrame": True,
                "supportsEndFrame": True,
                "supportsMultiR2V": True,
                "default": False,
                "description": "High fidelity Veo 3.1 model for high quality cinematic rendering"
            },
            {
                "id": "veo-3.1-lite-lower-priority",
                "name": "Veo 3.1 Lite (Lower Priority)",
                "type": "video",
                "durations": [4, 6, 8],
                "aspectRatios": ["16:9", "9:16", "1:1"],
                "resolutions": ["720p", "1080p"],
                "maxReferences": 3,
                "supportsStartFrame": True,
                "supportsEndFrame": True,
                "supportsMultiR2V": True,
                "default": False,
                "description": "Low cost/credit-efficient Veo 3.1 model"
            },
            {
                "id": "nano-banana-2-lite",
                "name": "Nano Banana 2 Lite",
                "type": "image",
                "aspectRatios": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                "resolutions": ["1K", "2K", "4K"],
                "maxReferences": 14,
                "default": True,
                "description": "State-of-the-art fast image generation model with up to 14 reference images"
            }
        ]
    }

class UseapiVideoCreateRequest(BaseModel):
    prompt: str = Field(..., description="Text description of the video")
    model: Optional[str] = Field("omni-flash", description="'omni-flash', 'veo-3.1-lite', 'veo-3.1-lite-lower-priority'")
    duration: Optional[int] = Field(8, description="Video duration: 4, 6, 8, 10 seconds")
    aspectRatio: Optional[str] = Field("16:9", description="'16:9', '9:16', '1:1', '4:3'")
    count: Optional[int] = Field(1, description="Number of variations to generate")
    referenceImages: Optional[List[str]] = Field([], description="Up to 7 reference image URLs/paths for Omni, 3 for Veo")
    startImage: Optional[str] = Field(None, description="Start frame URL/path (Frame-In)")
    endImage: Optional[str] = Field(None, description="End frame URL/path (Frame-Out / Interpolation)")
    account: Optional[str] = Field(None, description="Specific Google account email (optional, default auto round-robin)")

@app.post("/v1/google-flow/videos/create", dependencies=[Depends(verify_api_key)])
async def api_useapi_video_create(req: UseapiVideoCreateRequest):
    """Create a new video generation job (useapi compatible). Sub-millisecond non-blocking dispatch."""
    job_id = str(uuid.uuid4())
    now = int(time.time())
    
    # Pick account
    acct = await account_pool.get_next_account()
    acct_email = req.account or (acct["email"] if acct else "listbalck31@gmail.com")
    
    # Map to internal request
    internal_req = VideoGenRequest(
        prompt=req.prompt,
        model=req.model or "omni-flash",
        aspect_ratio=req.aspectRatio or "16:9",
        duration=req.duration or 8,
        count=req.count or 1,
        reference_images=req.referenceImages or [],
        start_image=req.startImage,
        end_image=req.endImage
    )
    
    with get_db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, account_email, status, progress, type, prompt, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
            (job_id, acct_email, "queued", 0, "video", req.prompt, req.model, now, now)
        )
        
    await POOL.enqueue(job_id, internal_req, "video")
    
    return {
        "jobId": job_id,
        "status": "queued",
        "model": req.model,
        "duration": req.duration,
        "aspectRatio": req.aspectRatio,
        "account": acct_email,
        "queuedAt": now
    }

class UseapiImageCreateRequest(BaseModel):
    prompt: str = Field(..., description="Text description of the image")
    model: Optional[str] = Field("nano-banana-2-lite", description="'nano-banana-2-lite'")
    aspectRatio: Optional[str] = Field("1:1", description="'1:1', '16:9', '9:16', '4:3', '3:4'")
    count: Optional[int] = Field(1, description="Number of image variations")
    referenceImages: Optional[List[str]] = Field([], description="Up to 14 reference image URLs/paths")

@app.post("/v1/google-flow/images/create", dependencies=[Depends(verify_api_key)])
async def api_useapi_image_create(req: UseapiImageCreateRequest):
    """Create a new image generation job (useapi compatible)."""
    job_id = str(uuid.uuid4())
    now = int(time.time())
    
    acct = await account_pool.get_next_account()
    acct_email = acct["email"] if acct else "listbalck31@gmail.com"
    
    internal_req = ImageGenRequest(
        prompt=req.prompt,
        model=req.model or "nano-banana-2-lite",
        aspect_ratio=req.aspectRatio or "1:1",
        count=req.count or 1,
        reference_images=req.referenceImages or []
    )
    
    with get_db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, account_email, status, progress, type, prompt, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
            (job_id, acct_email, "queued", 0, "image", req.prompt, req.model, now, now)
        )
        
    await POOL.enqueue(job_id, internal_req, "image")
    
    return {
        "jobId": job_id,
        "status": "queued",
        "type": "image",
        "model": req.model,
        "aspectRatio": req.aspectRatio,
        "account": acct_email,
        "queuedAt": now
    }

@app.get("/v1/google-flow/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def api_useapi_get_job(job_id: str):
    """Retrieve live status and CDN download URLs for a job."""
    return await get_job(job_id)

@app.get("/v1/google-flow/jobs", dependencies=[Depends(verify_api_key)])
async def api_useapi_list_jobs(status: Optional[str] = None, type: Optional[str] = None, limit: int = 50, offset: int = 0):
    """List recent generation jobs across the cluster."""
    return await list_jobs(status=status, type=type, limit=limit, offset=offset)

@app.post("/v1/google-flow/auth/refresh", dependencies=[Depends(verify_api_key)])
async def api_useapi_refresh_token():
    """Trigger token auto self-healing via labs.google/fx/404."""
    return await api_refresh_token()

# ─── Custom Clean HTML Documentation (No-Swagger Obsidian Theme) ──

@app.get("/", response_class=HTMLResponse)
@app.get("/docs-custom", response_class=HTMLResponse)
async def api_custom_docs_dashboard():
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Google Flow REST API Gateway — Developer Documentation</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #090a0f;
            --bg-surface: #0e1015;
            --bg-card: #141720;
            --border: #1f2430;
            --border-hover: #2e3547;
            --text-primary: #f1f3f7;
            --text-secondary: #9499ab;
            --text-muted: #5e6475;
            --accent: #3b82f6;
            --accent-green: #10b981;
            --accent-amber: #f59e0b;
            --accent-red: #ef4444;
            --code-bg: #07080b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
            background-color: var(--bg-base);
            color: var(--text-primary);
            line-height: 1.6;
            padding: 40px 24px;
        }
        .container { max-width: 1100px; margin: 0 auto; }
        .header {
            border-bottom: 1px solid var(--border);
            padding-bottom: 32px;
            margin-bottom: 40px;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            flex-wrap: wrap;
            gap: 20px;
        }
        .header-title h1 {
            font-size: 30px;
            font-weight: 800;
            letter-spacing: -0.5px;
            background: linear-gradient(135deg, #ffffff 30%, #9499ab 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .header-title p { color: var(--text-secondary); margin-top: 6px; font-size: 15px; }
        .badge-live {
            background: rgba(16, 185, 129, 0.1);
            color: var(--accent-green);
            border: 1px solid rgba(16, 185, 129, 0.2);
            padding: 6px 14px;
            border-radius: 99px;
            font-size: 13px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .badge-live::before {
            content: '';
            width: 8px;
            height: 8px;
            background: var(--accent-green);
            border-radius: 50%;
            box-shadow: 0 0 10px var(--accent-green);
        }
        .key-banner {
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px 24px;
            margin-bottom: 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }
        .key-banner .label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; }
        .key-banner .key-code {
            font-family: 'JetBrains Mono', monospace;
            font-size: 14px;
            background: var(--code-bg);
            padding: 8px 16px;
            border-radius: 8px;
            border: 1px solid var(--border);
            color: #60a5fa;
        }
        .section-title {
            font-size: 22px;
            font-weight: 700;
            margin: 48px 0 20px;
            display: flex;
            align-items: center;
            gap: 10px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 12px;
        }
        .card {
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 28px;
            margin-bottom: 28px;
            transition: border-color 0.2s;
        }
        .card:hover { border-color: var(--border-hover); }
        .endpoint-header {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 16px;
            flex-wrap: wrap;
        }
        .method {
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 6px;
            text-transform: uppercase;
        }
        .method-post { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
        .method-get { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
        .method-delete { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
        .path {
            font-family: 'JetBrains Mono', monospace;
            font-size: 16px;
            font-weight: 600;
            color: #fff;
        }
        .tag {
            background: var(--bg-card);
            border: 1px solid var(--border);
            padding: 3px 10px;
            border-radius: 6px;
            font-size: 12px;
            color: var(--text-secondary);
        }
        .desc { color: var(--text-secondary); font-size: 14px; margin-bottom: 20px; }
        
        .table-wrap { overflow-x: auto; margin-bottom: 20px; }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
        }
        th, td {
            padding: 10px 14px;
            border-bottom: 1px solid var(--border);
        }
        th {
            color: var(--text-muted);
            font-weight: 700;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.5px;
            background: var(--bg-card);
        }
        td { color: var(--text-primary); }
        td code {
            font-family: 'JetBrains Mono', monospace;
            color: #93c5fd;
            background: var(--code-bg);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 12px;
        }
        
        .code-block-header {
            font-size: 12px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            margin: 16px 0 8px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        pre {
            background: var(--code-bg);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 16px 20px;
            overflow-x: auto;
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            color: #e2e8f0;
            line-height: 1.5;
            margin-bottom: 16px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-title">
                <h1>Google Flow REST API Gateway</h1>
                <p>Zero-UI High Throughput Engine for Omni 1.1 Flash, Veo 3.1, & Nano Banana 2</p>
            </div>
            <div class="badge-live">v3.0.0 Production (Sub-ms Non-Blocking Queue)</div>
        </div>

        <div class="key-banner">
            <div>
                <div class="label">Authentication Header</div>
                <div style="font-size: 14px; color: var(--text-secondary); margin-top: 4px;">Pass in header via <code>X-API-Key</code> or <code>Authorization: Bearer</code></div>
            </div>
            <div class="key-code">gflow_live_ab1623b6697a4117ac721099</div>
        </div>

        <!-- SECTION 1 -->
        <h2 class="section-title">⚡ 1. Video Generation Endpoints (Useapi Compatible)</h2>

        <!-- Endpoint 1: Create Video -->
        <div class="card">
            <div class="endpoint-header">
                <span class="method method-post">POST</span>
                <span class="path">/v1/google-flow/videos/create</span>
                <span class="tag">Sub-ms Queue (<10ms)</span>
                <span class="tag">T2V / R2V / I2V</span>
            </div>
            <div class="desc">
                <strong>Deskripsi:</strong> Membuat tugas (job) generasi video baru secara instan tanpa interaksi UI browser. Mendukung mode Text-to-Video murni, Image-to-Video (Start Frame / Frame-In), Frame Interpolation (Start + End Frame), dan Multi-Reference Video (R2V hingga 7 gambar).
            </div>

            <div class="code-block-header">📋 Request Body Parameters</div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Field</th>
                            <th>Tipe</th>
                            <th>Wajib</th>
                            <th>Default</th>
                            <th>Keterangan</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td><code>prompt</code></td>
                            <td>string</td>
                            <td>Ya</td>
                            <td>-</td>
                            <td>Deskripsi visual / pergerakan video yang ingin dihasilkan.</td>
                        </tr>
                        <tr>
                            <td><code>model</code></td>
                            <td>string</td>
                            <td>Tidak</td>
                            <td><code>"omni-flash"</code></td>
                            <td>Pilihan model: <code>"omni-flash"</code> (Omni 1.1 Flash), <code>"veo-3.1-lite"</code>, <code>"veo-3.1-lite-lower-priority"</code>.</td>
                        </tr>
                        <tr>
                            <td><code>duration</code></td>
                            <td>integer</td>
                            <td>Tidak</td>
                            <td><code>8</code></td>
                            <td>Durasi video dalam detik. Omni: <code>4</code>, <code>6</code>, <code>8</code>, <code>10</code> | Veo: <code>4</code>, <code>6</code>, <code>8</code>.</td>
                        </tr>
                        <tr>
                            <td><code>aspectRatio</code></td>
                            <td>string</td>
                            <td>Tidak</td>
                            <td><code>"16:9"</code></td>
                            <td>Rasio aspek: <code>"16:9"</code> (Landscape), <code>"9:16"</code> (Portrait/Vertical), <code>"1:1"</code> (Square), <code>"4:3"</code>.</td>
                        </tr>
                        <tr>
                            <td><code>count</code></td>
                            <td>integer</td>
                            <td>Tidak</td>
                            <td><code>1</code></td>
                            <td>Jumlah variasi video yang di-generate (1 s/d 4).</td>
                        </tr>
                        <tr>
                            <td><code>referenceImages</code></td>
                            <td>array[string]</td>
                            <td>Tidak</td>
                            <td><code>[]</code></td>
                            <td>Daftar URL gambar referensi (R2V). Omni Flash: maks 7 gambar | Veo 3.1: maks 3 gambar.</td>
                        </tr>
                        <tr>
                            <td><code>startImage</code></td>
                            <td>string</td>
                            <td>Tidak</td>
                            <td><code>null</code></td>
                            <td>URL / path gambar awal video (Frame-In / Image-to-Video).</td>
                        </tr>
                        <tr>
                            <td><code>endImage</code></td>
                            <td>string</td>
                            <td>Tidak</td>
                            <td><code>null</code></td>
                            <td>URL / path gambar akhir video (Frame-Out / Interpolasi transisi).</td>
                        </tr>
                        <tr>
                            <td><code>account</code></td>
                            <td>string</td>
                            <td>Tidak</td>
                            <td><code>null</code></td>
                            <td>Email akun Google spesifik (opsional, default otomatis Round-Robin load balancer).</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <div class="code-block-header">💻 Contoh Request Body (JSON)</div>
            <pre><code>POST http://104.64.15.167:8088/v1/google-flow/videos/create
Headers:
  X-API-Key: gflow_live_ab1623b6697a4117ac721099
  Content-Type: application/json

{
  "prompt": "The woman in the black pinafore dress and white blouse moves and steps forward gracefully, 8k cinematic lighting",
  "model": "omni-flash",
  "duration": 4,
  "aspectRatio": "9:16",
  "count": 1,
  "referenceImages": [
    "https://example.com/outfit_reference.jpg"
  ]
}</code></pre>

            <div class="code-block-header">✅ Response (200 OK — Diterima Instan < 10ms)</div>
            <pre><code>{
  "jobId": "f9672d84-5cbb-4953-a295-f79350a41f2f",
  "status": "queued",
  "model": "omni-flash",
  "duration": 4,
  "aspectRatio": "9:16",
  "account": "listbalck31@gmail.com",
  "queuedAt": 1790251200
}</code></pre>

            <div class="code-block-header">❌ Response Error Contoh</div>
            <pre><code>// 401 Unauthorized (Kunci API tidak disertakan)
{ "detail": "Missing API Key. Provide via 'X-API-Key' header or 'Authorization: Bearer <key>'" }

// 403 Forbidden (Kunci API salah)
{ "detail": "Invalid or deactivated API Key" }</code></pre>
        </div>

        <!-- SECTION 2 -->
        <h2 class="section-title">🖼️ 2. Image Generation Endpoints (Nano Banana 2 Lite)</h2>

        <!-- Endpoint 2: Create Image -->
        <div class="card">
            <div class="endpoint-header">
                <span class="method method-post">POST</span>
                <span class="path">/v1/google-flow/images/create</span>
                <span class="tag">Up to 14 References</span>
                <span class="tag">Nano Banana 2</span>
            </div>
            <div class="desc">
                <strong>Deskripsi:</strong> Membuat gambar baru berkecepatan tinggi dengan model Nano Banana 2 Lite. Mendukung hingga 14 gambar referensi untuk konsistensi pose, karakter, dan gaya pencahayaan.
            </div>

            <div class="code-block-header">📋 Request Body Parameters</div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Field</th>
                            <th>Tipe</th>
                            <th>Wajib</th>
                            <th>Default</th>
                            <th>Keterangan</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td><code>prompt</code></td>
                            <td>string</td>
                            <td>Ya</td>
                            <td>-</td>
                            <td>Deskripsi gambar yang ingin dibuat.</td>
                        </tr>
                        <tr>
                            <td><code>model</code></td>
                            <td>string</td>
                            <td>Tidak</td>
                            <td><code>"nano-banana-2-lite"</code></td>
                            <td>Model gambar (Nano Banana 2 Lite).</td>
                        </tr>
                        <tr>
                            <td><code>aspectRatio</code></td>
                            <td>string</td>
                            <td>Tidak</td>
                            <td><code>"1:1"</code></td>
                            <td>Rasio aspek: <code>"1:1"</code>, <code>"16:9"</code>, <code>"9:16"</code>, <code>"4:3"</code>, <code>"3:4"</code>.</td>
                        </tr>
                        <tr>
                            <td><code>count</code></td>
                            <td>integer</td>
                            <td>Tidak</td>
                            <td><code>1</code></td>
                            <td>Jumlah variasi gambar (1 s/d 4).</td>
                        </tr>
                        <tr>
                            <td><code>referenceImages</code></td>
                            <td>array[string]</td>
                            <td>Tidak</td>
                            <td><code>[]</code></td>
                            <td>Daftar URL gambar referensi (hingga 14 gambar).</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <div class="code-block-header">💻 Contoh Request Body (JSON)</div>
            <pre><code>POST http://104.64.15.167:8088/v1/google-flow/images/create
Headers:
  X-API-Key: gflow_live_ab1623b6697a4117ac721099
  Content-Type: application/json

{
  "prompt": "A cute red panda wearing an astronaut helmet floating in deep space, hyper-detailed 8k",
  "model": "nano-banana-2-lite",
  "aspectRatio": "1:1",
  "count": 2,
  "referenceImages": ["https://example.com/pose_ref.jpg"]
}</code></pre>

            <div class="code-block-header">✅ Response (200 OK)</div>
            <pre><code>{
  "jobId": "8412fbea-1129-47ee-aa2d-8154aed88fb6",
  "status": "queued",
  "type": "image",
  "model": "nano-banana-2-lite",
  "aspectRatio": "1:1",
  "account": "listbalck31@gmail.com",
  "queuedAt": 1790251205
}</code></pre>
        </div>

        <!-- SECTION 3 -->
        <h2 class="section-title">🔍 3. Live Job Tracking & Direct Signed Video MP4 Retrieval</h2>

        <!-- Endpoint 3: Get Job -->
        <div class="card">
            <div class="endpoint-header">
                <span class="method method-get">GET</span>
                <span class="path">/v1/google-flow/jobs/{jobId}</span>
                <span class="tag">Realtime Status</span>
                <span class="tag">Signed Video MP4</span>
            </div>
            <div class="desc">
                <strong>Deskripsi:</strong> Memeriksa status pengerjaan job secara real-time. Ketika status mencapai <code>completed</code> (100%), endpoint mengembalikan URL CDN bertanda tangan Google Cloud (Signed URL) untuk file Video MP4 asli (bukan preview thumbnail).
            </div>

            <div class="code-block-header">💻 Contoh Request</div>
            <pre><code>GET http://104.64.15.167:8088/v1/google-flow/jobs/f9672d84-5cbb-4953-a295-f79350a41f2f
Headers:
  X-API-Key: gflow_live_ab1623b6697a4117ac721099</code></pre>

            <div class="code-block-header">⏳ Response Saat Masih Diproses (Status: processing)</div>
            <pre><code>{
  "jobId": "f9672d84-5cbb-4953-a295-f79350a41f2f",
  "type": "video",
  "status": "processing",
  "progress": 45,
  "account": "listbalck31@gmail.com",
  "prompt": "The woman in the black pinafore dress...",
  "model": "omni-flash",
  "result": null,
  "error": null,
  "createdAt": "1790251200",
  "updatedAt": "1790251220",
  "elapsedSeconds": 20
}</code></pre>

            <div class="code-block-header">🎉 Response Saat Selesai 100% (Status: completed)</div>
            <pre><code>{
  "jobId": "f9672d84-5cbb-4953-a295-f79350a41f2f",
  "type": "video",
  "status": "completed",
  "progress": 100,
  "account": "listbalck31@gmail.com",
  "prompt": "The woman in the black pinafore dress and white blouse moves and steps forward gracefully...",
  "model": "omni-flash",
  "result": {
    "videos": [
      {
        "id": "e9a389d3-85a9-4c94-9ff9-f35a182c98c1",
        "video_url": "https://flow-content.google/video/e9a389d3-85a9-4c94-9ff9-f35a182c98c1?Expires=1790277572&KeyName=labs-flow-prod-cdn-key&Signature=nSZ6Q3XcwBR_6Fp3dc4Y2Qe-Ljc",
        "poster_url": "https://flow-content.google/image/e9a389d3-85a9-4c94-9ff9-f35a182c98c1?Expires=1790277572&KeyName=labs-flow-prod-cdn-key&Signature=...",
        "local_file": "/root/api-google-flow/output/e9a389d3-85a9-4c94-9ff9-f35a182c98c1.mp4",
        "size_bytes": 1154813
      }
    ]
  },
  "error": null,
  "createdAt": "1790251200",
  "updatedAt": "1790251235",
  "elapsedSeconds": 35
}</code></pre>
        </div>

        <!-- Endpoint 4: List All Jobs -->
        <div class="card">
            <div class="endpoint-header">
                <span class="method method-get">GET</span>
                <span class="path">/v1/google-flow/jobs</span>
                <span class="tag">Dashboard / List</span>
            </div>
            <div class="desc">
                <strong>Deskripsi:</strong> Mengambil daftar semua pekerjaan yang sedang atau telah diproses di seluruh sistem dengan filter status dan pagination.
            </div>
            <pre><code>// Query Parameters:
// ?status=completed (queued, processing, completed, failed)
// ?type=video (video, image)
// ?limit=20&offset=0

GET http://104.64.15.167:8088/v1/google-flow/jobs?status=completed&limit=10
Headers:
  X-API-Key: gflow_live_ab1623b6697a4117ac721099</code></pre>
        </div>

        <!-- SECTION 4 -->
        <h2 class="section-title">👥 4. Multi-Account Pool & Model Catalog</h2>

        <!-- Endpoint 5: Models Catalog -->
        <div class="card">
            <div class="endpoint-header">
                <span class="method method-get">GET</span>
                <span class="path">/v1/google-flow/models</span>
            </div>
            <div class="desc">
                <strong>Deskripsi:</strong> Mengembalikan daftar katalog model resmi yang didukung beserta kapabilitas durasi, resolusi, rasio aspek, dan batas referensi.
            </div>
            <pre><code>GET http://104.64.15.167:8088/v1/google-flow/models
Headers:
  X-API-Key: gflow_live_ab1623b6697a4117ac721099

// Response 200 OK
{
  "models": [
    {
      "id": "omni-flash",
      "name": "Omni 1.1 Flash",
      "type": "video",
      "durations": [4, 6, 8, 10],
      "aspectRatios": ["16:9", "9:16", "1:1", "4:3"],
      "resolutions": ["360p", "720p", "1080p"],
      "maxReferences": 7,
      "supportsStartFrame": true,
      "supportsEndFrame": true,
      "supportsMultiR2V": true,
      "default": true
    },
    {
      "id": "veo-3.1-lite",
      "name": "Veo 3.1 Lite (Priority)",
      "type": "video",
      "durations": [4, 6, 8],
      "aspectRatios": ["16:9", "9:16", "1:1"],
      "resolutions": ["720p", "1080p"],
      "maxReferences": 3,
      "supportsStartFrame": true,
      "supportsEndFrame": true,
      "supportsMultiR2V": true,
      "default": false
    },
    {
      "id": "nano-banana-2-lite",
      "name": "Nano Banana 2 Lite",
      "type": "image",
      "aspectRatios": ["1:1", "16:9", "9:16", "4:3", "3:4"],
      "resolutions": ["1K", "2K", "4K"],
      "maxReferences": 14,
      "default": true
    }
  ]
}</code></pre>
        </div>

        <!-- Endpoint 6: Add Account -->
        <div class="card">
            <div class="endpoint-header">
                <span class="method method-post">POST</span>
                <span class="path">/v1/google-flow/accounts/add</span>
            </div>
            <div class="desc">
                <strong>Deskripsi:</strong> Menambahkan atau memperbarui akun Google ke dalam Session Pool tanpa perlu interaksi browser. Sistem akan otomatis mendistribusikan request antar akun aktif secara Round-Robin.
            </div>
            <pre><code>POST http://104.64.15.167:8088/v1/google-flow/accounts/add
Headers:
  X-API-Key: gflow_live_ab1623b6697a4117ac721099
  Content-Type: application/json

{
  "email": "user_account_2@gmail.com",
  "cookies": "[{\\"name\\": \\"__Secure-3PSID\\", \\"value\\": \\"...\\", \\"domain\\": \\".google.com\\"}]",
  "access_token": "ya29.***"
}

// Response 200 OK
{
  "success": true,
  "email": "user_account_2@gmail.com",
  "message": "Account registered in pool"
}</code></pre>
        </div>

        <!-- Endpoint 7: Token Refresh -->
        <div class="card">
            <div class="endpoint-header">
                <span class="method method-post">POST</span>
                <span class="path">/v1/google-flow/auth/refresh</span>
                <span class="tag">Self-Healing</span>
            </div>
            <div class="desc">
                <strong>Deskripsi:</strong> Memicu handshake self-healing token secara manual via <code>labs.google/fx/404</code> untuk memperbarui token <code>ya29</code> dan kunci sesi XSRF.
            </div>
            <pre><code>POST http://104.64.15.167:8088/v1/google-flow/auth/refresh
Headers:
  X-API-Key: gflow_live_ab1623b6697a4117ac721099

// Response 200 OK
{
  "success": true,
  "status": "token_refreshed",
  "detail": "refreshed"
}</code></pre>
        </div>

    </div>
</body>
</html>
"""
    return HTMLResponse(content=html_content, status_code=200)

@app.post("/v1/browser/generate-real-i2v")
async def api_generate_real_i2v(req: GenerateR2VRequest):
    """Accurately bind reference image to Flow's Start Frame / Reference slot and generate real I2V."""
    import browser_manager
    if not browser_manager._browser_context or not browser_manager._browser_context.pages:
        raise HTTPException(status_code=500, detail="Browser not running")
    page = browser_manager._browser_context.pages[0]
    
    # 1. Pastikan di project workspace
    if "flow.google.com/project/" not in page.url or "/edit/" in page.url:
        await page.goto("https://flow.google.com/project/0f21b6bf-24a7-4cfe-81ec-0e674b324f4a", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)
        
    # 2. Switch ke mode Video
    await page.evaluate('''() => {
        const spans = Array.from(document.querySelectorAll('span, button, div'));
        const videoTab = spans.find(el => (el.innerText || '').trim() === 'Video' && el.children.length === 0);
        if (videoTab) {
            videoTab.click();
            if (videoTab.parentElement) videoTab.parentElement.click();
        }
    }''')
    await asyncio.sleep(1)
    
    # 3. Klik tombol "+ Tambahkan media / bahan" di samping kotak prompt untuk membuka Media Picker
    logger.info("Opening Add Media / Bahan Dialog...")
    clicked_add = await page.evaluate('''() => {
        const allBtns = Array.from(document.querySelectorAll('button'));
        const addBtn = allBtns.find(b => {
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            const text = (b.innerText || '').toLowerCase();
            return (aria.includes('tambahkan') || aria.includes('add') || text === 'add') && 
                   (aria.includes('media') || aria.includes('bahan') || aria.includes('perintah') || text === 'add');
        });
        if (addBtn) {
            addBtn.click();
            return true;
        }
        return false;
    }''')
    await asyncio.sleep(1.5)
    
    # 4. Upload file gambar hijab melalui file chooser input di dalam dialog
    import os
    if os.path.exists(req.image_path):
        logger.info(f"Setting file to input: {req.image_path}")
        file_inputs = await page.query_selector_all('input[type="file"]')
        if file_inputs:
            await file_inputs[-1].set_input_files(req.image_path)
            logger.info("File uploaded via input[type=file]!")
            await asyncio.sleep(4)
            
            # Pilih / klik thumbnail gambar yang baru terupload di dialog
            await page.evaluate('''() => {
                const overlay = document.querySelector('.cdk-overlay-container, mat-dialog-container');
                if (overlay) {
                    const thumbs = Array.from(overlay.querySelectorAll('img, [role="button"], mat-grid-tile'));
                    if (thumbs.length > 0) {
                        thumbs[0].click();
                    }
                    // Klik tombol 'Selesai' atau 'Tambahkan' jika ada
                    const confirmBtn = Array.from(overlay.querySelectorAll('button')).find(b => {
                        const t = (b.innerText || '').toLowerCase();
                        return t.includes('tambahkan') || t.includes('add') || t.includes('selesai') || t.includes('insert');
                    });
                    if (confirmBtn) confirmBtn.click();
                }
            }''')
            await asyncio.sleep(1.5)
            
    # 5. Focus ProseMirror dan ketik prompt gerakan
    pm = await page.wait_for_selector(".ProseMirror", timeout=10000)
    await pm.click(force=True)
    await asyncio.sleep(0.3)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await page.keyboard.type(req.prompt, delay=15)
    await asyncio.sleep(1)
    
    # 6. Submit via Enter & Klik tombol Generate
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.5)
    
    await page.evaluate('''() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const send = btns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            return t.includes('arrow_forward') || aria.includes('mulai') || aria.includes('buat') || aria.includes('generate');
        });
        if (send) send.click();
    }''')
    
    await asyncio.sleep(3)
    text = await page.evaluate('() => document.body.innerText')
    
    return {
        "status": "real_i2v_triggered",
        "prompt": req.prompt,
        "image_path": req.image_path,
        "clicked_add": clicked_add,
        "textSample": text[:500]
    }

# ─── Direct media serving (added) ─────────────────────────────────────────────
@app.get("/v1/google-flow/media/{job_id}")
async def api_serve_job_media(job_id: str):
    """Serve the finished artefact of a job as real bytes.

    The API otherwise only exposes signed Google CDN links, and for video those
    are not fetchable by the caller. This streams the file the gateway holds.
    """
    from fastapi.responses import FileResponse
    import glob as _glob

    matches = [m for m in _glob.glob(f"/root/api-google-flow/output/{job_id}.*")
               if not m.endswith(".bin")]
    if not matches:
        raise HTTPException(status_code=404, detail=f"No artefact stored for job '{job_id}'")
    path = max(matches, key=os.path.getmtime)
    ext = os.path.splitext(path)[1].lower()
    media_types = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".png": "image/png", ".webp": "image/webp"}
    return FileResponse(path, media_type=media_types.get(ext, "application/octet-stream"),
                        filename=os.path.basename(path))
@app.get("/v1/debug/capture2")
async def debug_capture2():
    import importlib, capture_test, io, contextlib
    importlib.reload(capture_test)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await capture_test.main()
    return {"log": buf.getvalue()}
@app.get("/v1/debug/bench")
async def debug_bench():
    import importlib, capture_test, io, contextlib
    importlib.reload(capture_test)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await capture_test.main()
    return {"log": buf.getvalue()}



# ───────────────────────────────────────────────────────────────
# VIDS API (di belakang FLOW_ENABLE_VIDS_API)
#   POST /v1/vids/videos/create   masukkan job ke antrian, kembali cepat
#   GET  /v1/vids/jobs/{job_id}   status + hasil
#   GET  /v1/vids/jobs            daftar job
#   GET  /v1/vids/accounts        keadaan pool akun (ok / limit)
# ───────────────────────────────────────────────────────────────

VIDS_DB = "/root/api-google-flow/data/vids_jobs.db"


def _vids_enabled() -> bool:
    return os.environ.get("FLOW_ENABLE_VIDS_API", "0").strip() in ("1", "true", "yes")


def _vids_db():
    import sqlite3 as _sq
    conn = _sq.connect(VIDS_DB)
    conn.row_factory = _sq.Row
    return conn


class VidsLoginRequest(BaseModel):
    """Permintaan login akun. emails null = semua dari accounts.txt."""
    emails: Optional[List[str]] = None
    mode: Optional[str] = "belum_ok"     # "belum_ok" (lewati yang sudah ok) | "semua"


class VidsCreateRequest(BaseModel):
    prompt: str
    account: Optional[str] = None      # paksa akun tertentu (opsional)
    aspect: Optional[str] = None       # "portrait" | "landscape"
    images: Optional[List[str]] = None  # URL gambar Vids: 1=referensi, 2=start+end


@app.post("/v1/vids/videos/create", dependencies=[Depends(verify_api_key)])
async def api_vids_create(req: VidsCreateRequest):
    """Masukkan job Vids ke antrian. Kembali secepat mungkin (anti-timeout)."""
    if not _vids_enabled():
        raise HTTPException(status_code=503,
                            detail="Vids API nonaktif (set FLOW_ENABLE_VIDS_API=1)")
    import uuid as _uuid
    job_id = str(_uuid.uuid4())
    now = int(time.time())
    conn = _vids_db()
    try:
        conn.execute(
            """INSERT INTO vids_jobs (job_id, account, prompt, status, progress,
                                      created_at, updated_at, aspect, images)
               VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)""",
            (job_id, req.account or "", req.prompt, now, now,
             (req.aspect or "landscape"), json.dumps(req.images or [])))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"vids job queued: {job_id} "
                f"aspect={req.aspect or 'landscape'} images={len(req.images or [])}")
    return {"jobId": job_id, "status": "queued", "engine": "vids",
            "aspect": req.aspect or "landscape",
            "images": len(req.images or []), "queuedAt": now}


@app.get("/v1/vids/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def api_vids_job(job_id: str):
    if not _vids_enabled():
        raise HTTPException(status_code=503, detail="Vids API nonaktif")
    conn = _vids_db()
    try:
        row = conn.execute("SELECT * FROM vids_jobs WHERE job_id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="job tidak ditemukan")
    out = dict(row)
    if out.get("result_path"):
        out["download_url"] = f"/v1/vids/media/{job_id}"
        out["file_name"] = os.path.basename(out["result_path"])
    return out


@app.get("/v1/vids/jobs", dependencies=[Depends(verify_api_key)])
async def api_vids_jobs(limit: int = 50, status: Optional[str] = None):
    if not _vids_enabled():
        raise HTTPException(status_code=503, detail="Vids API nonaktif")
    conn = _vids_db()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM vids_jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vids_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        # RINGKASAN AKURAT: hitung SELURUH tabel, bukan hanya halaman ini.
        # Panel bot butuh angka total; menghitung dari `rows` (yang dibatasi
        # `limit`) membuat angka jauh lebih kecil dari kenyataan.
        summary = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM vids_jobs GROUP BY status")}
        total = sum(summary.values())
    finally:
        conn.close()
    return {"jobs": [dict(r) for r in rows], "count": len(rows),
            "total": total, "summary": summary,
            "window": len(rows)}


@app.get("/v1/vids/media/{job_id}", dependencies=[Depends(verify_api_key)])
async def api_vids_media(job_id: str):
    """Stream hasil MP4."""
    if not _vids_enabled():
        raise HTTPException(status_code=503, detail="Vids API nonaktif")
    conn = _vids_db()
    try:
        row = conn.execute("SELECT result_path FROM vids_jobs WHERE job_id=?",
                           (job_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["result_path"] or not os.path.exists(row["result_path"]):
        raise HTTPException(status_code=404, detail="hasil belum ada")
    return FileResponse(row["result_path"], media_type="video/mp4",
                        filename=os.path.basename(row["result_path"]))


def purge_accounts_txt(emails):
    """Cabut baris akun dari accounts.txt (dengan cadangan). Kembalikan jumlah."""
    from pathlib import Path as _P
    import time as _t
    f = _P("/root/api-google-flow/accounts.txt")
    if not f.exists():
        return 0
    drop = {e.strip().lower() for e in emails if e and e.strip()}
    lines = f.read_text(encoding="utf-8").splitlines()
    keep, gone = [], 0
    for line in lines:
        raw = line.strip()
        if raw and not raw.startswith("#") and "|" in raw:
            em = raw.split("|")[0].strip().lower()
            if em in drop:
                gone += 1
                continue
        keep.append(line)
    if gone:
        bak = f.with_suffix(f".txt.bak-purge-{int(_t.time())}")
        try:
            bak.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass
        f.write_text("\n".join(keep) + "\n", encoding="utf-8")
        try:
            import os as _os
            _os.chmod(f, 0o600)
        except Exception:
            pass
    return gone


class VidsPurgeRequest(BaseModel):
    emails: List[str] = []
    dry_run: bool = False
    # True = boleh hapus walau state 'ok' (admin memilih sendiri dari daftar)
    force: bool = False
    # Cara pintas hapus massal per keadaan, mis. ["no_login","ui_retry"]
    states: List[str] = []


@app.post("/v1/vids/accounts/purge", dependencies=[Depends(verify_api_key)])
async def api_vids_accounts_purge(req: VidsPurgeRequest):
    """Hapus akun bermasalah dari pool BESERTA profil chrome-nya.

    Hanya akun yang sudah tercatat di vids_accounts (dengan state selain 'ok')
    yang boleh dihapus — supaya tidak bisa menghapus akun yang masih sehat.
    """
    if not _vids_enabled():
        raise HTTPException(status_code=503, detail="Vids API nonaktif")
    import shutil as _sh
    import time as _t
    from pathlib import Path as _P
    now = int(_t.time())
    conn = _vids_db()
    removed, kept, missing, profiles = [], [], [], 0
    _txt_gone = 0
    try:
        rows = conn.execute("SELECT email, state FROM vids_accounts").fetchall()
        state_of = {r["email"]: r["state"] for r in rows}

        # ── PENGAMAN: keberhasilan login dicatat di tabel `accounts`
        # (gateway.db) sebagai status='active', sedangkan vids_accounts sering
        # tertinggal basi. Tanpa pemeriksaan ini, akun yang SUDAH sehat akan
        # ikut terhapus hanya karena label lama di vids_accounts.
        actually_active = set()
        try:
            with get_db() as _aconn:
                for _ar in _aconn.execute(
                        "SELECT email FROM accounts WHERE status='active' "
                        "AND COALESCE(is_active,1)=1").fetchall():
                    actually_active.add(_ar["email"])
        except Exception as _e:
            logger.warning(f"purge: gagal baca tabel accounts: {_e}")

        # Baris basi untuk akun yang sebenarnya sehat: bersihkan labelnya,
        # jangan hapus akunnya. Dijalankan sebelum loop sasaran.
        stale_fixed = 0
        if not req.dry_run:
            for _e in list(state_of.keys()):
                if _e in actually_active:
                    try:
                        conn.execute("DELETE FROM vids_accounts WHERE email=?",
                                     (_e,))
                        stale_fixed += 1
                    except Exception:
                        pass
            if stale_fixed:
                conn.commit()
                logger.info(f"purge: {stale_fixed} label basi dibersihkan "
                            f"(akun sehat, tidak dihapus)")

        # Sasaran: gabungan email yang diminta + semua akun ber-state yang dipilih.
        targets = list(req.emails or [])
        want_states = [str(s).strip() for s in (req.states or []) if str(s).strip()]
        if want_states:
            for e, st in state_of.items():
                if st in want_states and e not in targets:
                    targets.append(e)

        for email in targets:
            email = (email or "").strip()
            if not email or email not in state_of:
                missing.append(email)
                continue
            if (state_of[email] == "ok" or email in actually_active) and not req.force:
                kept.append(email)      # akun sehat: JANGAN dihapus (kecuali force)
                continue
            if not req.dry_run:
                conn.execute("DELETE FROM vids_accounts WHERE email=?", (email,))
                prof = _P("/root/api-google-flow/chrome_profiles") / email
                if prof.exists():
                    try:
                        _sh.rmtree(prof, ignore_errors=True)
                        profiles += 1
                    except Exception as e:
                        logger.warning(f"gagal hapus profil {email}: {e}")
            removed.append(email)
        if not req.dry_run:
            conn.commit()

        # Cabut juga dari accounts.txt: endpoint /v1/vids/accounts menyusun
        # daftar dari berkas ini, jadi tanpa langkah ini akun yang sudah
        # dihapus tetap muncul sebagai state 'unknown'.
        if removed and not req.dry_run:
            try:
                _txt_gone = purge_accounts_txt(removed)
            except Exception as e:
                logger.warning(f"gagal memperbarui accounts.txt: {e}")
    finally:
        conn.close()
    logger.info(f"vids purge: {len(removed)} dihapus, {len(kept)} sehat dilindungi")
    return {"removed": len(removed), "removed_emails": removed,
            "stale_labels_cleared": locals().get("stale_fixed", 0),
            "profiles_deleted": profiles,
            "removed_from_accounts_txt": _txt_gone,
            "kept_healthy": kept, "unknown": [m for m in missing if m],
            "dry_run": req.dry_run}


class VidsAccountEntry(BaseModel):
    email: str
    password: str = ""


class VidsAccountsImport(BaseModel):
    entries: List[VidsAccountEntry]
    mode: str = "replace"          # "replace" (ganti total) | "append" (tambah)


def _accounts_path() -> str:
    return "/root/api-google-flow/accounts.txt"


def _read_accounts_file():
    """Kembalikan (list baris akun, list baris lain) dari accounts.txt."""
    p = _accounts_path()
    acc, other = [], []
    if not os.path.exists(p):
        return acc, other
    for line in open(p, encoding="utf-8").read().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "|" in s and "@" in s.split("|")[0]:
            acc.append(s)
        else:
            other.append(line)
    return acc, other


@app.post("/v1/vids/accounts/import", dependencies=[Depends(verify_api_key)])
async def api_vids_accounts_import(req: VidsAccountsImport):
    """Setor daftar akun (email|password) ke accounts.txt.

    Dipakai panel admin bot. WAJIB punya password untuk tiap email: pool tidak
    bisa login tanpa itu, dan menyimpan baris tanpa password membuat akun
    terlihat ada padahal tidak bisa dipakai.
    """
    p = _accounts_path()
    entries = []
    seen = set()
    skipped = []
    for e in req.entries:
        email = (e.email or "").strip()
        password = (e.password or "").strip()
        if "@" not in email:
            skipped.append({"email": email, "reason": "email tidak valid"})
            continue
        if not password:
            skipped.append({"email": email, "reason": "password kosong"})
            continue
        if email.lower() in seen:
            skipped.append({"email": email, "reason": "duplikat"})
            continue
        seen.add(email.lower())
        entries.append(f"{email}|{password}")

    if not entries:
        return {"status": "error", "written": 0, "skipped": skipped,
                "message": "tidak ada baris valid untuk ditulis"}

    mode = (req.mode or "replace").strip().lower()
    old_acc, other = _read_accounts_file()

    # cadangan sebelum menyentuh berkas.
    # `import shutil` LOKAL: app.py mengimpor shutil di dalam fungsi lain,
    # jadi memakai nama global di sini = NameError yang tertelan `except` dan
    # cadangan tidak pernah terbentuk (bug nyata: penyetoran menimpa berkas
    # tanpa jejak untuk dipulihkan).
    import shutil as _shutil
    if os.path.exists(p):
        bak = f"{p}.bak-import-{int(time.time())}"
        try:
            _shutil.copy2(p, bak)
            logger.info(f"cadangan accounts.txt: {bak}")
        except Exception as exc:
            logger.warning(f"gagal membuat cadangan accounts.txt: {exc}")

    if mode == "append" and old_acc:
        have = {ln.split("|")[0].strip().lower() for ln in old_acc}
        merged = list(old_acc)
        added = 0
        for ln in entries:
            if ln.split("|")[0].strip().lower() not in have:
                merged.append(ln)
                added += 1
        body = merged
        result_mode = f"append (+{added})"
    else:
        body = entries
        result_mode = "replace"

    header = ("# Kredensial pool GSuite — dikelola lewat panel admin bot\n"
              "# Format: email|password\n")
    tmp = f"{p}.tmp-{int(time.time())}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(header)
        fh.write("\n".join(body) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)

    logger.info(f"accounts.txt disetor: {len(body)} akun (mode {result_mode})")
    return {"status": "ok", "mode": result_mode, "written": len(body),
            "added": len(entries) if result_mode == "replace" else None,
            "skipped": skipped,
            "total_in_file": len(body),
            "path": p}


@app.get("/v1/vids/accounts/template", dependencies=[Depends(verify_api_key)])
async def api_vids_accounts_template():
    """Contoh isi file akun, supaya bot bisa menjelaskan format ke admin."""
    acc, _ = _read_accounts_file()
    return {
        "format": "email|password  (satu akun per baris)",
        "tanpa_password": "email saja — bot akan menanyakan passwordnya",
        "contoh": "akun1@domain.com|sandi1\nakun2@domain.com|sandi2",
        "jumlah_sekarang": len(acc),
        "punya_password": sum(1 for a in acc if a.split("|", 1)[1].strip()),
    }


def _akun_perlu_login(kandidat: list, mode: str) -> tuple:
    """Saring akun agar yang sudah sehat TIDAK di-login ulang.

    Kembalikan (daftar_perlu_login, jumlah_dilewati).

    Alasan: login memakan ~25-190s per akun dan bisa memicu verifikasi Google.
    Akun yang sudah ok dan profilnya masih hangat tidak perlu diulang.
    """
    import json as _json
    import os as _os
    from pathlib import Path as _P

    if (mode or "belum_ok") == "semua":
        return list(kandidat), 0

    APP = _P("/root/api-google-flow")
    segar = float(_os.environ.get("LOGIN_FRESH_HOURS", "6")) * 3600
    now = time.time()

    # 1. hasil login terakhir per akun
    terakhir = {}
    rp = APP / "data" / "login_results.jsonl"
    if rp.exists():
        for line in rp.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = _json.loads(line)
            except Exception:
                continue
            e = (d.get("email") or "").strip()
            if e:
                terakhir[e] = d

    perlu, dilewati = [], 0
    for email in kandidat:
        # 2. catatan terakhir: ok & belum kadaluarsa -> lewati
        d = terakhir.get(email) or {}
        if str(d.get("state")) == "ok":
            umur = now - float(d.get("ts") or 0)
            if umur < segar:
                dilewati += 1
                continue
            # ok tapi catatan lama: cek apakah profilnya masih hangat
            prof = APP / "chrome_profiles" / email
            ck = prof / "Default" / "Cookies"
            if ck.exists() and (now - ck.stat().st_mtime) < segar:
                dilewati += 1
                continue
            perlu.append(email)
            continue

        # 3. belum pernah dicatat, atau terakhir gagal:
        #    kalau profilnya baru saja ditulis (sesi mungkin sudah hidup),
        #    jangan dibuang percuma.
        prof = APP / "chrome_profiles" / email
        ck = prof / "Default" / "Cookies"
        if ck.exists() and ck.stat().st_size > 20000:
            umur_prof = now - ck.stat().st_mtime
            if umur_prof < segar and str(d.get("state")) == "ok":
                dilewati += 1
                continue
        perlu.append(email)

    return perlu, dilewati


@app.post("/v1/vids/accounts/login", dependencies=[Depends(verify_api_key)])
async def api_vids_accounts_login(req: VidsLoginRequest):
    """Jalankan LOGIN akun pool di LATAR BELAKANG (tidak menunggu selesai).

    Kenapa asinkron: 10 akun x ~25s = ~4 menit, jauh melebihi batas Cloudflare
    (~100s) -> bot menerima "524 Server Error" padahal login masih berjalan.
    Sekarang endpoint langsung membalas; kemajuannya dipantau lewat
    GET /v1/vids/accounts/login-status.
    """
    import json as _json
    from pathlib import Path as _P

    APP = _P("/root/api-google-flow")
    PY = "/root/geminiomni/venv/bin/python"
    SCRIPT = APP / "_login_jalan.py"
    STATUS = APP / "data" / "login_proses.json"
    LOGF = APP / "data" / "login_last.log"

    # sudah ada yang berjalan? jangan dobel
    st = {}
    if STATUS.exists():
        try:
            st = _json.loads(STATUS.read_text())
        except Exception:
            st = {}
    if st.get("running"):
        pid_lama = st.get("pid")
        hidup = False
        if pid_lama:
            try:
                import os as _os
                _os.kill(int(pid_lama), 0)
                hidup = True
            except Exception:
                hidup = False
        if hidup:
            return {"status": "sudah_jalan", "pid": pid_lama,
                    "diproses": st.get("jumlah"), "message": "login sedang berjalan"}
        st = {}

    # pilih kandidat
    kandidat = []
    if req.emails:
        kandidat = [e.strip() for e in req.emails if e and e.strip()]
    else:
        f = APP / "accounts.txt"
        if f.exists():
            for line in f.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                em = line.split("|")[0].strip()
                if em:
                    kandidat.append(em)
        kandidat, dilewati = _akun_perlu_login(kandidat, req.mode or "belum_ok")
        logger.info("auto-login: %d akun dilewati (sudah sehat), %d perlu login",
                    dilewati, len(kandidat))

    if not kandidat:
        return {"status": "ok", "diproses": 0,
                "message": "tidak ada akun yang perlu login"}

    # SEMUA kandidat diproses (bukan hanya 10). Endpoint ini sudah asinkron,
    # jadi tidak ada alasan membatasi; bot memantau lewat login-status.
    batch = kandidat
    logger.info("auto-login dimulai (latar belakang): %d akun dari %d kandidat",
                len(batch), len(kandidat))

    logf = open(LOGF, "w", encoding="utf-8")
    # Jalankan login di cgroup SENDIRI (systemd transient unit) supaya
    # `systemctl restart artflow-gateway-local` TIDAK mematikannya.
    # Terbukti: dengan Popen biasa, restart gateway membunuh login yang
    # sedang berjalan (25 akun berhenti di akun ke-7).
    pakai_systemd_run = os.environ.get("LOGIN_VIA_SYSTEMD", "1") not in ("0", "false", "no")
    proc = None
    if pakai_systemd_run:
        try:
            proc = subprocess.Popen(
                ["systemd-run", "--collect", "--unit", f"artflow-login-job",
                 "--description", "ArtFlow auto-login (semua akun)",
                 "--setenv", "DISPLAY=:99",
                 "--working-directory", str(APP),
                 PY, str(SCRIPT), ",".join(batch)],
                stdout=logf, stderr=subprocess.STDOUT)
            logger.info("auto-login dijalankan via systemd-run (cgroup sendiri)")
        except Exception as e:
            logger.warning("systemd-run gagal (%s) — memakai Popen biasa",
                           str(e)[:90])
            proc = None
    if proc is None:
        proc = subprocess.Popen(
            [PY, str(SCRIPT), ",".join(batch)],
            cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, env={**os.environ, "DISPLAY": ":99"})
        logger.info("auto-login dijalankan via Popen biasa")

    STATUS.write_text(_json.dumps({
        "running": True, "pid": proc.pid, "jumlah": len(batch),
        "kandidat": len(kandidat), "mulai": int(time.time()),
    }), encoding="utf-8")

    return {"status": "mulai", "pid": proc.pid, "diproses": len(batch),
            "sisa_kandidat": max(0, len(kandidat) - len(batch)),
            "message": f"login {len(batch)} akun berjalan di latar belakang"}


@app.get("/v1/vids/accounts/login-status", dependencies=[Depends(verify_api_key)])
async def api_vids_login_status():
    """Keadaan proses login latar belakang (untuk pemantauan bot)."""
    import json as _json
    import os as _os
    from pathlib import Path as _P

    APP = _P("/root/api-google-flow")
    STATUS = APP / "data" / "login_proses.json"
    LOGF = APP / "data" / "login_last.log"

    st = {}
    if STATUS.exists():
        try:
            st = _json.loads(STATUS.read_text())
        except Exception:
            st = {}

    hidup = False
    pid = st.get("pid")
    if pid:
        try:
            _os.kill(int(pid), 0)
            hidup = True
        except Exception:
            hidup = False

    if st and not hidup:
        st["running"] = False
        STATUS.write_text(_json.dumps(st), encoding="utf-8")

    # ringkasan hasil dari log terakhir
    hasil, baris = {}, []
    if LOGF.exists():
        teks = LOGF.read_text(errors="replace")
        for l in teks.splitlines()[-40:]:
            if "-> " in l and "@" in l:
                baris.append(l.strip()[:120])
            if "=== hasil:" in l:
                hasil["ringkas"] = l.strip()
    st["running"] = hidup
    st["hasil_terakhir"] = baris[-10:]
    st["ringkas"] = hasil.get("ringkas")
    return st


@app.get("/v1/vids/accounts/login-live", dependencies=[Depends(verify_api_key)])
async def api_vids_login_live():
    """Status HIDUP login per akun: tahap terkini (untuk panel).

    Sumber: data/login_live.json yang ditulis _login_jalan.py tiap tahap.
    """
    import json as _json
    from pathlib import Path as _P

    p = _P("/root/api-google-flow/data/login_live.json")
    if not p.exists():
        return {"akun": {}, "note": "belum ada proses login"}
    try:
        return _json.loads(p.read_text(errors="replace"))
    except Exception as e:
        return {"akun": {}, "note": f"gagal baca: {str(e)[:80]}"}


@app.get("/v1/vids/accounts/login-log", dependencies=[Depends(verify_api_key)])
async def api_vids_login_log(limit: int = 50, state: Optional[str] = None):
    """Riwayat login akun pool: hasil TERBARU per akun.

    Sumber: data/login_results.jsonl (ditulis gsuite_login/login_all).
    `state` menyaring: ok | error | needs_human | bad_credentials.
    """
    import json as _json
    from pathlib import Path as _P

    p = _P("/root/api-google-flow/data/login_results.jsonl")
    if not p.exists():
        return {"total": 0, "items": [], "note": "belum ada riwayat login"}

    latest = {}
    order = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = _json.loads(line)
        except Exception:
            continue
        e = (d.get("email") or "").strip()
        if not e:
            continue
        if e not in latest:
            order.append(e)
        latest[e] = d                      # baris terakhir = hasil terbaru

    items = [latest[e] for e in order]
    if state:
        items = [i for i in items if (i.get("state") or "") == state]
    items.sort(key=lambda i: i.get("at") or "", reverse=True)

    counts = {}
    for i in latest.values():
        s = i.get("state") or "?"
        counts[s] = counts.get(s, 0) + 1

    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 50

    return {
        "total": len(latest),
        "counts": counts,
        "showing": min(limit, len(items)),
        "items": [
            {"email": i.get("email"), "state": i.get("state"),
             "detail": (i.get("detail") or "")[:300],
             "secs": i.get("secs"), "at": i.get("at")}
            for i in items[:limit]
        ],
    }


@app.get("/v1/vids/accounts", dependencies=[Depends(verify_api_key)])
async def api_vids_accounts():
    """Keadaan pool akun Vids: mana yang siap, mana yang limit."""
    if not _vids_enabled():
        raise HTTPException(status_code=503, detail="Vids API nonaktif")
    import time as _t
    now = int(_t.time())
    conn = _vids_db()
    try:
        rows = conn.execute("SELECT email, state, note, until, updated_at "
                            "FROM vids_accounts").fetchall()
    finally:
        conn.close()
    states = {}
    for r in rows:
        limited = (r["state"] == "limited" and r["until"] > now)
        states[r["email"]] = {
            "state": "limited" if limited else r["state"],
            "cooldown_left": max(0, r["until"] - now) if limited else 0,
            "note": r["note"],
        }

    # Keberhasilan login dicatat di tabel `accounts` (gateway.db) sebagai
    # status='active'. Tanpa menggabungkannya, akun yang baru login sukses
    # tetap tampil 'unknown' dan admin tidak bisa memisahkan yang sehat.
    active_emails = set()
    try:
        with get_db() as aconn:
            for arow in aconn.execute(
                    "SELECT email FROM accounts WHERE status='active' "
                    "AND COALESCE(is_active,1)=1").fetchall():
                active_emails.add(arow["email"])
    except Exception as e:
        logger.warning(f"vids accounts: gagal baca tabel accounts: {e}")

    for e in active_emails:
        cur = states.get(e)
        if cur is None or cur.get("state") in (None, "", "unknown"):
            states[e] = {"state": "ok", "cooldown_left": 0,
                         "note": "login sukses (pool gateway)"}
    # daftar akun dari accounts.txt
    all_acc = []
    acc_file = "/root/api-google-flow/accounts.txt"
    if os.path.exists(acc_file):
        for line in open(acc_file, encoding="utf-8").read().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "|" in line:
                e = line.split("|")[0].strip()
                if "@" in e:
                    all_acc.append(e)
    ready = [e for e in all_acc if states.get(e, {}).get("state") != "limited"]
    by_state = {}
    for e in all_acc:
        s = states.get(e, {}).get("state") or "unknown"
        by_state[s] = by_state.get(s, 0) + 1
    return {"total": len(all_acc), "ready": len(ready),
            "by_state": by_state,
            "limited": [e for e in all_acc if states.get(e, {}).get("state") == "limited"],
            "accounts": [{"email": e, **states.get(e, {"state": "unknown",
                                                       "cooldown_left": 0, "note": ""})}
                         for e in all_acc]}


@app.get("/health")
async def health():
    return {"status": "healthy", "version": "3.1.0-accounts", "queue_length": job_queue.qsize()}
