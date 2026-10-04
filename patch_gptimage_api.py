#!/usr/bin/env python3
"""Tanam API GPT Image ke gateway (app.py) — reversibel.

Menambah:
  POST /v1/gptimage/images/create   → antrian job, kembali cepat
  GET  /v1/gptimage/jobs/{job_id}   → status + tautan hasil
  GET  /v1/gptimage/jobs            → daftar + ringkasan
  GET  /v1/gptimage/media/{job_id}  → berkas PNG
  GET  /v1/gptimage/accounts        → keadaan akun + sisa kuota

Ada worker latar yang mengambil job `queued` lalu menjalankannya lewat
gptimage_runner.jalankan() (yang sudah terbukti: rotasi akun, baca kuota RPC,
tukar prompt via route.fulfill).

Di belakang FLOW_ENABLE_GPTIMAGE_API (bawaan 1 = aktif).
Cadangan: app.py.bak-gptimage-<epoch>
"""
from pathlib import Path

APP = Path("/root/api-google-flow/app.py")
import time

BAK = APP.with_suffix(f".py.bak-gptimage-{int(time.time())}")

BLOK = '''


# ───────────────────────────────────────────────────────────────
# GPT IMAGE API (ChatGPT / GPT Image lewat akun pool, tanpa API key)
#   POST /v1/gptimage/images/create   antrian job, kembali cepat
#   GET  /v1/gptimage/jobs/{job_id}   status + tautan hasil
#   GET  /v1/gptimage/jobs            daftar + ringkasan
#   GET  /v1/gptimage/media/{job_id}  berkas PNG
#   GET  /v1/gptimage/accounts        keadaan akun + sisa kuota
#
# Mesin ketiga setelah Omni (video) dan Nano Banana (gambar).
# Teknik: buka profil Camoufox akun, baca kuota lewat RPC murni, lalu kirim
# prompt dengan menukar body request halaman di tengah jalan
# (route.fetch + route.fulfill). Lihat gptimage_runner.py.
# ───────────────────────────────────────────────────────────────

GPTIMAGE_DB = "/root/api-google-flow/data/gptimage_jobs.db"


def _gptimage_enabled() -> bool:
    return os.environ.get("FLOW_ENABLE_GPTIMAGE_API", "1").strip() in ("1", "true", "yes")


def _gptimage_db():
    import sqlite3 as _sq
    conn = _sq.connect(GPTIMAGE_DB)
    conn.row_factory = _sq.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS gptimage_jobs (
        job_id TEXT PRIMARY KEY, account TEXT, prompt TEXT, status TEXT,
        progress INTEGER DEFAULT 0, created_at INTEGER, updated_at INTEGER,
        result_path TEXT, size TEXT, error TEXT, attempts INTEGER DEFAULT 0)""")
    conn.commit()
    return conn


class GptImageCreateRequest(BaseModel):
    prompt: str
    account: Optional[str] = None


@app.post("/v1/gptimage/images/create", dependencies=[Depends(verify_api_key)])
async def api_gptimage_create(req: GptImageCreateRequest):
    """Masukkan job GPT Image ke antrian. Kembali cepat (anti-timeout)."""
    if not _gptimage_enabled():
        raise HTTPException(status_code=503,
                            detail="GPT Image API nonaktif (set FLOW_ENABLE_GPTIMAGE_API=1)")
    import uuid as _uuid
    job_id = str(_uuid.uuid4())
    now = int(time.time())
    conn = _gptimage_db()
    try:
        conn.execute(
            """INSERT INTO gptimage_jobs (job_id, account, prompt, status,
                                          progress, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', 0, ?, ?)""",
            (job_id, req.account or "", req.prompt, now, now))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"gptimage job queued: {job_id} prompt={req.prompt[:60]!r}")
    return {"jobId": job_id, "status": "queued", "engine": "gptimage",
            "queuedAt": now}


@app.get("/v1/gptimage/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def api_gptimage_job(job_id: str):
    if not _gptimage_enabled():
        raise HTTPException(status_code=503, detail="GPT Image API nonaktif")
    conn = _gptimage_db()
    try:
        row = conn.execute("SELECT * FROM gptimage_jobs WHERE job_id=?",
                           (job_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="job tidak ditemukan")
    out = dict(row)
    if out.get("result_path") and os.path.exists(out["result_path"]):
        out["download_url"] = f"/v1/gptimage/media/{job_id}"
        out["file_name"] = os.path.basename(out["result_path"])
    return out


@app.get("/v1/gptimage/jobs", dependencies=[Depends(verify_api_key)])
async def api_gptimage_jobs(limit: int = 50, status: Optional[str] = None):
    if not _gptimage_enabled():
        raise HTTPException(status_code=503, detail="GPT Image API nonaktif")
    conn = _gptimage_db()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM gptimage_jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM gptimage_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        # hitung SELURUH tabel, bukan hanya halaman ini (pelajaran dari panel Vids)
        summary = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM gptimage_jobs GROUP BY status")}
        total = sum(summary.values())
    finally:
        conn.close()
    return {"jobs": [dict(r) for r in rows], "count": len(rows),
            "total": total, "summary": summary, "window": len(rows)}


@app.get("/v1/gptimage/media/{job_id}", dependencies=[Depends(verify_api_key)])
async def api_gptimage_media(job_id: str):
    """Stream hasil PNG."""
    if not _gptimage_enabled():
        raise HTTPException(status_code=503, detail="GPT Image API nonaktif")
    conn = _gptimage_db()
    try:
        row = conn.execute("SELECT result_path FROM gptimage_jobs WHERE job_id=?",
                           (job_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["result_path"] or not os.path.exists(row["result_path"]):
        raise HTTPException(status_code=404, detail="hasil belum ada")
    return FileResponse(row["result_path"], media_type="image/png",
                        filename=os.path.basename(row["result_path"]))


@app.get("/v1/gptimage/accounts", dependencies=[Depends(verify_api_key)])
async def api_gptimage_accounts():
    """Keadaan akun GPT Image + sisa kuota."""
    if not _gptimage_enabled():
        raise HTTPException(status_code=503, detail="GPT Image API nonaktif")
    try:
        import gptimage_runner as _g
        return _g.status_akun()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"status akun gagal: {exc}")


class GptImagePurgeRequest(BaseModel):
    emails: List[str] = []


@app.post("/v1/gptimage/accounts/purge", dependencies=[Depends(verify_api_key)])
async def api_gptimage_purge(req: GptImagePurgeRequest):
    """Bersihkan catatan status akun (bukan menghapus akun dari pool)."""
    if not _gptimage_enabled():
        raise HTTPException(status_code=503, detail="GPT Image API nonaktif")
    try:
        import gptimage_runner as _g
        from pathlib import Path as _P
        p = _P(_g.STATUS_PATH)
        if p.exists():
            p.unlink()
        return {"dibersihkan": True, "jumlah": len(req.emails or [])}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:200])


async def _gptimage_worker():
    """Ambil job queued satu per satu, jalankan lewat gptimage_runner."""
    import gptimage_runner as _g
    logger.info("gptimage worker mulai")
    while True:
        try:
            await asyncio.sleep(5)
            conn = _gptimage_db()
            try:
                row = conn.execute(
                    "SELECT * FROM gptimage_jobs WHERE status='queued' "
                    "ORDER BY created_at ASC LIMIT 1").fetchone()
            finally:
                conn.close()
            if not row:
                continue
            job_id = row["job_id"]
            conn = _gptimage_db()
            try:
                conn.execute(
                    "UPDATE gptimage_jobs SET status='running', progress=10, "
                    "updated_at=? WHERE job_id=?", (int(time.time()), job_id))
                conn.commit()
            finally:
                conn.close()

            kel = f"/root/api-google-flow/output/gptimage/{job_id}.png"
            r = await asyncio.to_thread(_g.jalankan, row["prompt"], row["account"] or None,
                                        kel, 300)
            st = r.get("status") or "gagal"
            status_job = {"ok": "completed", "limit": "limited",
                          "kuota_habis": "limited",
                          "no_login": "failed"}.get(st, "failed")
            conn = _gptimage_db()
            try:
                conn.execute(
                    """UPDATE gptimage_jobs SET status=?, progress=?, updated_at=?,
                       result_path=?, size=?, error=?,
                       attempts=COALESCE(attempts,0)+1 WHERE job_id=?""",
                    (status_job, 100 if status_job == "completed" else 0,
                     int(time.time()), r.get("berkas"),
                     r.get("ukuran") or "", r.get("pesan") or "", job_id))
                conn.commit()
            finally:
                conn.close()
            logger.info(f"gptimage job {job_id}: {st} → {status_job} "
                        f"({r.get('pesan','')})")
        except Exception as exc:
            logger.warning(f"gptimage worker error: {exc}")
            await asyncio.sleep(10)


@app.on_event("startup")
async def _gptimage_startup():
    if _gptimage_enabled():
        _gptimage_db().close()
        asyncio.create_task(_gptimage_worker())
        logger.info("GPT Image API aktif (FLOW_ENABLE_GPTIMAGE_API=1)")

'''

TEKS = APP.read_text()
if "GPTIMAGE_DB" in TEKS:
    print("sudah terpasang — tidak diubah")
    raise SystemExit(0)

# sisipkan sebelum endpoint /health (paling akhir berkas)
penanda = '@app.get("/health")'
if penanda not in TEKS:
    raise SystemExit("penanda /health tidak ditemukan")

BAK.write_text(TEKS)
baru = TEKS.replace(penanda, BLOK.strip("\n") + "\n\n" + penanda, 1)
APP.write_text(baru)
print(f"cadangan: {BAK.name}")
print(f"app.py: {len(TEKS)} → {len(baru)} byte")