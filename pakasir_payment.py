#!/usr/bin/env python3
"""
MODUL PEMBAYARAN PAKASIR — auto beli kredit ArtFlow.

Dipakai oleh gateway (app.py). Dipasang sebagai router terpisah supaya:
  - app.py hanya perlu 2 baris tambahan (mudah dibalik)
  - seluruh logika pembayaran ada di satu berkas

PRINSIP DESAIN (penting):
  1. GATEWAY HANYA MENCATAT. Kredit TIDAK ditambah di sini.
     Yang menambah kredit = BOT (punya cadangkan() + catat_tx() + db_lock).
     Alasan: users.json ditulis utuh; kalau gateway ikut menulis,
     terjadi race condition lintas-proses.
  2. JANGAN PERCAYA ISI WEBHOOK. Setelah terima webhook, panggil balik
     API status Pakasir untuk memastikan.
  3. Nominal acuan = yang KITA simpan saat membuat order, bukan dari webhook.
  4. Idempoten: credited_at IS NULL sebagai kunci.

Saklar (env):
  PAKASIR_TOPUP=0     → matikan seluruh fitur (endpoint balas 503)
  PAKASIR_WEBHOOK=0   → matikan hanya webhook
  PAKASIR_SANDBOX=1   → tolak webhook non-sandbox (uji tanpa uang)
"""

import os
import asyncio
import hmac
import json
import time
import sqlite3
import logging
import threading
from typing import Optional, Dict, Any, List

import httpx
from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("pakasir")

PAKASIR_BASE = "https://app.pakasir.com"

# WAJIB: Cloudflare memblokir User-Agent library (error code 1010) dengan
# HTTP 403 dan BODY KOSONG — sangat mudah disalahartikan sebagai rate limit.
# Diuji: "Python-urllib/3.12" -> 403 "error code: 1010"; UA browser -> 200.
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {"User-Agent": UA_BROWSER, "Accept": "application/json",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h
DB_PATH = "/root/api-google-flow/data/payments.db"

# ─── Saklar ──────────────────────────────────────────────────────────────
def _flag(nama: str, bawaan: str = "0") -> bool:
    return os.environ.get(nama, bawaan).strip().lower() in ("1", "true", "yes", "on")

def topup_aktif() -> bool:
    return _flag("PAKASIR_TOPUP", "0")          # BAWAAN MATI — harus dinyalakan sengaja

def webhook_aktif() -> bool:
    return topup_aktif() and _flag("PAKASIR_WEBHOOK", "1")

def sandbox_aktif() -> bool:
    return _flag("PAKASIR_SANDBOX", "1")        # BAWAAN SANDBOX — aman

def _slug() -> str:
    return os.environ.get("PAKASIR_SLUG", "").strip()

def _api_key() -> str:
    return os.environ.get("PAKASIR_API_KEY", "").strip()

def _x_secret() -> str:
    return os.environ.get("PAKASIR_X_SECRET", "").strip()

# ─── Basis data ──────────────────────────────────────────────────────────
_db_lock = threading.Lock()
_db_siap = False

SKEMA = """
CREATE TABLE IF NOT EXISTS payments (
    order_id       TEXT PRIMARY KEY,
    txn_id         TEXT,
    tg_user_id     TEXT NOT NULL,
    user_email     TEXT,
    amount         INTEGER NOT NULL,
    fee            INTEGER,
    total_payment  INTEGER,
    method         TEXT DEFAULT 'qris',
    status         TEXT DEFAULT 'pending',
    qr_string      TEXT,
    expires_at     TEXT,
    is_sandbox     INTEGER DEFAULT 1,
    created_at     INTEGER,
    updated_at     INTEGER,
    completed_at   TEXT,
    credited_at    INTEGER,
    credited_oleh  TEXT,
    kredit_diberi  INTEGER DEFAULT 0,
    webhook_hits   INTEGER DEFAULT 0,
    verify_count   INTEGER DEFAULT 0,
    last_error     TEXT,
    raw_webhook    TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_credit ON payments(credited_at);
CREATE INDEX IF NOT EXISTS idx_payments_user   ON payments(tg_user_id);
"""

def db() -> sqlite3.Connection:
    global _db_siap
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    with _db_lock:
        if not _db_siap:
            c.executescript(SKEMA)
            c.commit()
            _db_siap = True
    return c

# ─── Paket kredit (keputusan pemilik 5 Okt 2026) ─────────────────────────
PAKET = [
    {"rupiah": 1000,   "kredit": 3},
    {"rupiah": 5000,   "kredit": 15},
    {"rupiah": 10000,  "kredit": 30},
    {"rupiah": 20000,  "kredit": 60},
    {"rupiah": 50000,  "kredit": 200},
    {"rupiah": 100000, "kredit": 500},
]
PAKET_MAP = {p["rupiah"]: p["kredit"] for p in PAKET}

# ─── Router ──────────────────────────────────────────────────────────────
router = APIRouter(prefix="/v1/payment", tags=["payment"])


class BuatOrder(BaseModel):
    tg_user_id: str
    user_email: str
    rupiah: int


@router.post("/create")
async def buat_order(body: BuatOrder):
    """
    Bot memanggil ini saat user memilih paket.
    Nominal DISIMPAN di sini — inilah acuan verifikasi nanti.
    """
    if not topup_aktif():
        raise HTTPException(503, "Fitur pembelian kredit sedang dimatikan")

    if body.rupiah not in PAKET_MAP:
        raise HTTPException(400, f"Nominal tidak dikenal. Pilihan: {sorted(PAKET_MAP)}")
    if not _slug() or not _api_key():
        raise HTTPException(500, "Konfigurasi Pakasir belum lengkap (slug/api_key)")

    kredit = PAKET_MAP[body.rupiah]
    order_id = f"AF-{body.tg_user_id}-{int(time.time())}"

    payload = {"method": "qris", "amount": body.rupiah}
    try:
        async with httpx.AsyncClient(timeout=25) as cl:
            r = await cl.post(
                f"{PAKASIR_BASE}/api/v2/create-transaction/{_slug()}/{order_id}",
                json=payload,
                headers=_headers({"X-Api-Key": _api_key()}),
            )
        data = r.json() if r.content else {}
    except Exception as exc:
        logger.error(f"[pakasir] create gagal: {exc}")
        raise HTTPException(502, f"Gagal menghubungi Pakasir: {exc}")

    if r.status_code != 200:
        logger.error(f"[pakasir] create HTTP {r.status_code}: {data}")
        raise HTTPException(502, f"Pakasir menolak: {data}")

    txn_id = data.get("txn_id")
    now = int(time.time())
    c = db()
    with _db_lock:
        c.execute(
            """INSERT OR REPLACE INTO payments
               (order_id, txn_id, tg_user_id, user_email, amount, fee, total_payment,
                method, status, qr_string, expires_at, is_sandbox, created_at, updated_at,
                kredit_diberi)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, txn_id, body.tg_user_id, body.user_email, body.rupiah,
             data.get("fee"), data.get("total_payment"), data.get("payment_method", "qris"),
             "pending", data.get("qr_string"),
             # ⚠️ API mengirim 'expires_at'; docs salah tulis 'expired_at'
             data.get("expires_at") or data.get("expired_at"),
             1 if data.get("is_sandbox") else 0, now, now, kredit),
        )
        c.commit()
    c.close()

    logger.info(f"[pakasir] order dibuat {order_id} txn={txn_id} Rp{body.rupiah} → {kredit} kredit")
    return {
        "order_id": order_id,
        "txn_id": txn_id,
        "kredit": kredit,
        "amount": body.rupiah,
        "fee": data.get("fee"),
        "total_payment": data.get("total_payment"),
        "qr_string": data.get("qr_string"),
        "expires_at": data.get("expires_at") or data.get("expired_at"),
        "is_sandbox": bool(data.get("is_sandbox")),
    }


# ─── Rate limit API: TERBUKTI GLOBAL (bukan per-transaksi) ────────────────
# Diuji nyata:
#   3 panggilan cepat berturut → 429, 429, 429
#   jeda ~4,5 detik           → lolos
# Jatahnya milik AKUN (per IP/kunci), bukan per txn_id — jadi jeda disimpan
# GLOBAL, bukan per transaksi. (403 body kosong itu hal LAIN: blokir UA, lihat
# UA_BROWSER di atas.)
_STATUS_TERAKHIR: float = 0.0
_RL_LOCK = threading.Lock()
# Jarak aman antar panggilan API Pakasir (dari pengukuran: 12s selalu lolos)
JEDA_STATUS = float(os.environ.get("PAKASIR_STATUS_JEDA", "4.5"))


async def _tunggu_jatah(txn_id: str = "") -> None:
    """Pastikan tidak menembak API Pakasir lebih cepat dari jatah global."""
    global _STATUS_TERAKHIR
    while True:
        with _RL_LOCK:
            sejak = time.time() - _STATUS_TERAKHIR
            if sejak >= JEDA_STATUS:
                _STATUS_TERAKHIR = time.time()
                return
            perlu = JEDA_STATUS - sejak
        await asyncio.sleep(perlu)


async def _verifikasi_ke_pakasir(order_id: str, txn_id: str,
                                 maks_coba: int = 3) -> Optional[Dict[str, Any]]:
    """
    Panggil balik API status. JANGAN percaya isi webhook.

    Menghormati rate limit GLOBAL (terbukti 429/403 kalau dilanggar):
      - jaga jarak sendiri (_tunggu_jatah)
      - kalau tetap kena 429/403 → tunggu lalu ulangi (maks 3x)
    """
    for coba in range(1, maks_coba + 1):
        await _tunggu_jatah(txn_id)
        try:
            async with httpx.AsyncClient(timeout=20) as cl:
                r = await cl.get(
                    f"{PAKASIR_BASE}/api/v2/transaction-status/{_slug()}/{txn_id}",
                    headers=_headers({"X-Api-Key": _api_key()}),
                )
            if r.status_code == 429:
                tunggu = JEDA_STATUS * coba
                logger.warning(f"[pakasir] verifikasi {order_id} kena 429 "
                               f"(coba {coba}) — tunggu {tunggu:.1f}s")
                await asyncio.sleep(tunggu)
                continue
            if r.status_code != 200:
                logger.warning(f"[pakasir] verifikasi {order_id} HTTP {r.status_code}")
                return None
            return r.json()
        except Exception as exc:
            logger.warning(f"[pakasir] verifikasi {order_id} gagal (coba {coba}): {exc}")
            if coba < maks_coba:
                await asyncio.sleep(3 * coba)
    logger.error(f"[pakasir] verifikasi {order_id} menyerah setelah {maks_coba} percobaan")
    return None


@router.post("/webhook/pakasir")
async def webhook_pakasir(request: Request, x_secret: Optional[str] = Header(None)):
    """
    Pakasir memanggil ini saat pembayaran berhasil.
    URUTAN VERIFIKASI (jangan diubah):
      1. X-Secret (bandingkan waktu-konstan)
      2. order_id dikenal?
      3. sudah pernah dikredit? → 200, berhenti (idempoten)
      4. cek ulang ke API status Pakasir
      5. status == completed, is_sandbox sesuai mode, amount cocok
      6. tandai siap-kredit (credited_at tetap NULL)
    """
    if not webhook_aktif():
        raise HTTPException(503, "Webhook dimatikan")

    if not _x_secret():
        logger.error("[pakasir] X-Secret belum diset — webhook ditolak")
        raise HTTPException(500, "Konfigurasi webhook belum lengkap")

    # (1) Verifikasi rahasia — waktu konstan, fail-closed
    if not x_secret or not hmac.compare_digest(str(x_secret), _x_secret()):
        logger.warning("[pakasir] webhook dengan X-Secret SALAH — ditolak")
        raise HTTPException(401, "X-Secret tidak cocok")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body bukan JSON")

    order_id = str(body.get("order_id") or "").strip()
    if not order_id:
        raise HTTPException(400, "order_id kosong")

    c = db()
    row = c.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()

    if not row:
        c.close()
        logger.warning(f"[pakasir] webhook untuk order tidak dikenal: {order_id}")
        raise HTTPException(404, "Order tidak dikenal")

    # (3) Sudah pernah diproses? Balas 200 supaya Pakasir berhenti mengirim ulang
    if row["credited_at"] is not None:
        c.execute("UPDATE payments SET webhook_hits=webhook_hits+1, updated_at=? WHERE order_id=?",
                  (int(time.time()), order_id))
        c.commit()
        c.close()
        logger.info(f"[pakasir] webhook ulang untuk {order_id} — sudah dikredit, diabaikan")
        return {"status": "ok", "catatan": "sudah diproses"}

    # (4) Jangan percaya isi webhook — panggil balik
    txn_id = row["txn_id"] or body.get("txn_id")
    if not txn_id:
        c.execute("UPDATE payments SET last_error=?, updated_at=? WHERE order_id=?",
                  ("txn_id tidak ada", int(time.time()), order_id))
        c.commit()
        c.close()
        raise HTTPException(400, "txn_id tidak ada")

    status_api = await _verifikasi_ke_pakasir(order_id, txn_id)
    if status_api is None:
        c.execute("UPDATE payments SET last_error=?, verify_count=verify_count+1, updated_at=? WHERE order_id=?",
                  ("verifikasi gagal", int(time.time()), order_id))
        c.commit()
        c.close()
        # 5xx supaya Pakasir mengirim ulang nanti (bukan 200 — kita belum yakin)
        raise HTTPException(502, "Verifikasi ke Pakasir gagal, coba lagi")

    # (5) Semua syarat
    st = str(status_api.get("status") or "").lower()
    if st != "completed":
        c.execute("UPDATE payments SET status=?, verify_count=verify_count+1, updated_at=?, last_error=? WHERE order_id=?",
                  (st or "unknown", int(time.time()), f"webhook bilang completed tapi API bilang {st}", order_id))
        c.commit()
        c.close()
        logger.warning(f"[pakasir] {order_id}: webhook completed tapi API '{st}' — DITOLAK")
        return {"status": "ok", "catatan": f"status sebenarnya {st}"}

    # Nominal acuan = yang KITA simpan saat buat order
    if int(status_api.get("amount") or 0) != int(row["amount"]):
        c.execute("UPDATE payments SET last_error=?, updated_at=? WHERE order_id=?",
                  (f"nominal tidak cocok: API {status_api.get('amount')} vs simpan {row['amount']}",
                   int(time.time()), order_id))
        c.commit()
        c.close()
        logger.error(f"[pakasir] {order_id}: NOMINAL TIDAK COCOK — DITOLAK")
        raise HTTPException(400, "Nominal tidak cocok")

    # Mode uji: hanya terima sandbox saat sandbox aktif
    is_sb = bool(status_api.get("is_sandbox"))
    if sandbox_aktif() and not is_sb:
        logger.warning(f"[pakasir] {order_id}: transaksi NYATA masuk saat mode sandbox — ditahan")
        c.execute("UPDATE payments SET is_sandbox=0, last_error=?, updated_at=? WHERE order_id=?",
                  ("transaksi nyata saat mode sandbox", int(time.time()), order_id))
        c.commit()
        c.close()
        return {"status": "ok", "catatan": "mode sandbox — menunggu tinjauan"}
    if not sandbox_aktif() and is_sb:
        c.execute("UPDATE payments SET last_error=?, updated_at=? WHERE order_id=?",
                  ("transaksi sandbox saat mode produksi", int(time.time()), order_id))
        c.commit()
        c.close()
        raise HTTPException(400, "Transaksi sandbox ditolak di mode produksi")

    # (6) Tandai siap dikredit — credited_at TETAP NULL, bot yang mengisi
    now = int(time.time())
    c.execute(
        """UPDATE payments
           SET status='completed', completed_at=?, is_sandbox=?, updated_at=?,
               webhook_hits=webhook_hits+1, verify_count=verify_count+1,
               raw_webhook=?, last_error=NULL
           WHERE order_id=? AND credited_at IS NULL""",
        (status_api.get("completed_at"), 1 if is_sb else 0, now,
         json.dumps(body)[:2000], order_id),
    )
    c.commit()
    c.close()

    logger.info(f"[pakasir] {order_id} SIAP DIKREDIT ({row['kredit_diberi']} kredit) — menunggu bot")
    return {"status": "ok", "order_id": order_id, "siap_kredit": row["kredit_diberi"]}


@router.get("/pending")
async def daftar_pending():
    """Bot memanggil ini untuk mengambil order yang siap dikredit."""
    if not topup_aktif():
        raise HTTPException(503, "Fitur dimatikan")
    c = db()
    rows = c.execute(
        """SELECT order_id, txn_id, tg_user_id, user_email, amount, kredit_diberi
           FROM payments
           WHERE status='completed' AND credited_at IS NULL
           ORDER BY updated_at LIMIT 50"""
    ).fetchall()
    c.close()
    return {"jumlah": len(rows), "order": [dict(r) for r in rows]}


@router.post("/mark-credited/{order_id}")
async def tandai_terkredit(order_id: str, body: Dict[str, Any]):
    """
    Bot memanggil ini SETELAH kredit benar-benar masuk.
    Inilah kunci idempoten: WHERE credited_at IS NULL.
    rowcount == 0 → sudah pernah diproses → JANGAN tambah kredit lagi.
    """
    if not topup_aktif():
        raise HTTPException(503, "Fitur dimatikan")
    now = int(time.time())
    c = db()
    with _db_lock:
        cur = c.execute(
            """UPDATE payments
               SET credited_at=?, credited_oleh=?, updated_at=?
               WHERE order_id=? AND credited_at IS NULL""",
            (now, str(body.get("oleh") or "bot"), now, order_id),
        )
        c.commit()
        baris = cur.rowcount
    c.close()
    if baris == 0:
        logger.info(f"[pakasir] {order_id} sudah pernah ditandai — diabaikan (idempoten)")
        return {"status": "ok", "baru": False, "catatan": "sudah pernah diproses"}
    logger.info(f"[pakasir] {order_id} ditandai terkredit oleh {body.get('oleh')}")
    return {"status": "ok", "baru": True}


@router.get("/status/{order_id}")
async def status_order(order_id: str):
    """Bot menampilkan status pesanan ke user."""
    if not topup_aktif():
        raise HTTPException(503, "Fitur dimatikan")
    c = db()
    row = c.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
    c.close()
    if not row:
        raise HTTPException(404, "Order tidak ditemukan")
    return dict(row)


@router.get("/paket")
async def daftar_paket():
    """Daftar paket (untuk bot/layar)."""
    return {"paket": PAKET, "metode": "qris",
            "sandbox": sandbox_aktif(), "topup_aktif": topup_aktif()}


# ─── Jaring pengaman: pantau order yang webhook-nya hilang ────────────────
# Pakasir TIDAK mendokumentasikan berapa kali webhook dikirim ulang (tidak
# ditemukan di docs). Jadi jangan bergantung padanya: periksa sendiri order
# yang masih 'pending' padahal sudah lewat beberapa menit.
_PANTAU_MULAI = False
UMUR_PANTAU = int(os.environ.get("PAKASIR_PANTAU_UMUR", "120"))   # detik
JEDA_PANTAU = int(os.environ.get("PAKASIR_PANTAU_JEDA", "60"))    # detik antar putaran


async def _pantau_sekali() -> int:
    """
    Cari order 'pending' yang sudah lama, tanya Pakasir statusnya.
    Kalau ternyata completed → tandai siap-kredit (sama seperti jalur webhook).

    Mengembalikan jumlah order yang baru ditandai.
    """
    if not topup_aktif():
        return 0

    batas = int(time.time()) - UMUR_PANTAU
    c = db()
    rows = c.execute(
        """SELECT order_id, txn_id, amount, kredit_diberi
           FROM payments
           WHERE status='pending' AND txn_id IS NOT NULL AND created_at < ?
           ORDER BY created_at LIMIT 20""", (batas,)).fetchall()
    c.close()
    if not rows:
        return 0

    ditandai = 0
    for row in rows:
        order_id, txn_id = row["order_id"], row["txn_id"]
        st = await _verifikasi_ke_pakasir(order_id, txn_id)
        if st is None:
            continue
        if str(st.get("status") or "").lower() != "completed":
            continue
        if int(st.get("amount") or 0) != int(row["amount"]):
            logger.error(f"[pakasir] pantau {order_id}: nominal tidak cocok — dilewati")
            continue
        is_sb = bool(st.get("is_sandbox"))
        if sandbox_aktif() != is_sb:
            continue

        now = int(time.time())
        c = db()
        with _db_lock:
            cur = c.execute(
                """UPDATE payments
                   SET status='completed', completed_at=?, is_sandbox=?, updated_at=?,
                       verify_count=verify_count+1,
                       last_error='dipulihkan oleh pemantau (webhook tidak sampai)'
                   WHERE order_id=? AND credited_at IS NULL AND status!='completed'""",
                (st.get("completed_at"), 1 if is_sb else 0, now, order_id))
            c.commit()
            n = cur.rowcount
        c.close()
        if n:
            ditandai += 1
            logger.warning(f"[pakasir] PANTAU: {order_id} dibayar tapi webhook tak sampai "
                           f"— ditandai siap-kredit ({row['kredit_diberi']} kredit)")
    return ditandai


async def _pantau_loop():
    """Putaran pemantau. Jalan terus selama fitur hidup."""
    global _PANTAU_MULAI
    logger.info(f"[pakasir] pemantau aktif (tiap {JEDA_PANTAU}s, umur {UMUR_PANTAU}s)")
    while True:
        try:
            n = await _pantau_sekali()
            if n:
                logger.warning(f"[pakasir] pemantau memulihkan {n} order")
        except Exception as e:
            logger.error(f"[pakasir] pemantau error: {e}")
        await asyncio.sleep(JEDA_PANTAU)


def mulai_pemantau():
    """Dipanggil dari startup gateway."""
    global _PANTAU_MULAI
    if _PANTAU_MULAI or not topup_aktif():
        return False
    asyncio.create_task(_pantau_loop())
    _PANTAU_MULAI = True
    return True