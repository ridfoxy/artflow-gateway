#!/usr/bin/env python3
"""
TOMBOL "BELI CREDIT" — versi MANUAL (poster + kontak @konfirmasipay).

DIPASANG DI: bawah daftar kategori GPT Image (gptimage_i2i), jadi setelah
user melihat pilihan GPT Image, ada satu baris tombol Beli Credit.

ALUR:
  user tekan "💳 Beli Credit"
    → bot kirim POSTER (gambar yang user berikan)
    → caption + tombol "💬 Konfirmasi ke @konfirmasipay"
    → menghubungi admin untuk bayar manual

CATATAN: ini TERPISAH dari alur pakasir otomatis. Keduanya bisa hidup
bersamaan (pakasir untuk nanti, manual untuk sekarang).
"""
import os, re, sys, time, shutil, py_compile

BOT = "/root/bottele/bot.py"
ASET = "/root/bottele/aset/beli_kredit.jpg"

TANDA = "PKMN"      # singkatan unik untuk penanda di bot.py
ANCHOR = '''    # ── tombol beli kredit (Pakasir) ──'''

TOMBOL_BLOK = '''    # ── tombol Beli Credit MANUAL (poster + @konfirmasipay) ──
    markup.row(types.InlineKeyboardButton("💳 Beli Credit",
                                          callback_data="pkmu:buka"))
'''

HANDLER_BLOK = '''

# ===========================================================================
# Beli Credit — MANUAL (poster + kontak admin)
# ===========================================================================
@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("pkmu:"))
def handle_beli_credit_manual(call):
    """Tampilkan poster harga + arahkan ke admin untuk bayar manual."""
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    aksi = (call.data or "").split(":", 1)[1] if ":" in (call.data or "") else ""

    if aksi == "tutup":
        bot.answer_callback_query(call.id, "Dibatalkan.")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        return

    bot.answer_callback_query(call.id)

    # Siapa yang mau beli? (biar admin tahu akun mana yang dikredit)
    sesi = db.get_user_session(user_id)
    email = (sesi or {}).get("email") or "-"

    caption = (
        "💳 <b>BELI CREDIT</b>\\n"
        "━━━━━━━━━━━━━━━━━━━\\n"
        "Lebih banyak credit, lebih banyak keseruan!\\n\\n"
        "<b>Paket:</b>\\n"
        "  Rp  1.000  →   3 credit\\n"
        "  Rp  5.000  →  15 credit\\n"
        "  Rp 10.000  →  30 credit\\n"
        "  Rp 20.000  →  60 credit\\n"
        "  Rp 50.000  → 200 credit  ⭐ spesial\\n"
        "  Rp 100.000 → 500 credit  ⭐ spesial\\n\\n"
        "━━━━━━━━━━━━━━━━━━━\\n"
        "📩 <b>Cara beli:</b> hubungi admin\\n"
        "   Telegram: <b>@konfirmasipay</b>\\n\\n"
        f"💬 Sertakan email akun Anda:\\n<code>{esc(email)}</code>\\n\\n"
        "<i>Sebutkan paket yang dipilih, admin akan membantu.</i>"
    )

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.row(types.InlineKeyboardButton("💬 Chat @konfirmasipay", url="https://t.me/konfirmasipay"))
    kb.row(types.InlineKeyboardButton("❌ Tutup", callback_data="pkmu:tutup"))

    # kirim poster; kalau berkasnya hilang, tetap kirim teks (jangan gagal)
    terkirim = False
    if os.path.exists(ASET_BELI_CREDIT):
        try:
            with open(ASET_BELI_CREDIT, "rb") as f:
                bot.send_photo(chat_id, f, caption=caption[:1024],
                               parse_mode="HTML", reply_markup=kb)
            terkirim = True
        except Exception as e:
            log_pkmu(f"gagal kirim poster: {e}")
    if not terkirim:
        bot.send_message(chat_id, caption + "\\n\\n<i>(poster tidak tersedia)</i>",
                         parse_mode="HTML", reply_markup=kb)

    log_pkmu(f"user {user_id} ({email}) buka menu Beli Credit")


def log_pkmu(pesan):
    try:
        print(f"[beli-credit] {pesan}")
    except Exception:
        pass

'''

# konstanta path aset (ditaruh dekat import)
KONST_BLOK = '''
# ── Beli Credit (manual): berkas poster harga ──
ASET_BELI_CREDIT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "aset", "beli_kredit.jpg")
'''


def main():
    if not os.path.exists(BOT):
        print(f"❌ {BOT} tidak ada"); return 1
    s = open(BOT, encoding="utf-8").read()

    if TANDA in s:
        print("ℹ️  Tombol Beli Credit sudah terpasang — tidak diulang.")
        return 0

    cad = f"{BOT}.bak-pkmn-{int(time.time())}"
    shutil.copy2(BOT, cad)
    print(f"✅ Cadangan: {os.path.basename(cad)} ({os.path.getsize(cad)} byte)")

    # 1. konstanta path
    m = re.search(r'^import api_client as api\s*$', s, re.M)
    if not m:
        print("❌ anchor import tidak ditemukan"); shutil.copy2(cad, BOT); return 1
    s = s[:m.end()] + "\n" + KONST_BLOK + s[m.end():]
    print("✅ 1/3 konstanta path poster")

    # 2. tombol — DI ATAS tombol pakasir, jadi paling bawah daftar kategori
    if ANCHOR not in s:
        print("❌ anchor tombol tidak ditemukan"); shutil.copy2(cad, BOT); return 1
    s = s.replace(ANCHOR, TOMBOL_BLOK + ANCHOR, 1)
    print("✅ 2/3 tombol 'Beli Credit' (bawah daftar kategori)")

    # 3. handler — sebelum handler pakasir
    anchor_h = '@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("pk:"))'
    if anchor_h not in s:
        print("❌ anchor handler tidak ditemukan"); shutil.copy2(cad, BOT); return 1
    s = s.replace(anchor_h, HANDLER_BLOK.strip("\n") + "\n\n\n" + anchor_h, 1)
    print("✅ 3/3 handler pkmu:")

    with open(BOT, "w", encoding="utf-8") as f:
        f.write(s)

    try:
        py_compile.compile(BOT, doraise=True)
        print("✅ py_compile OK")
    except Exception as e:
        print(f"❌ Sintaks rusak: {e} → mengembalikan")
        shutil.copy2(cad, BOT); return 1

    v = open(BOT, encoding="utf-8").read()
    cek = {
        "konstanta ASET_BELI_CREDIT": "ASET_BELI_CREDIT" in v,
        "tombol pkmu:buka": '"pkmu:buka"' in v,
        "handler handle_beli_credit_manual": "def handle_beli_credit_manual" in v,
        "link @konfirmasipay": "t.me/konfirmasipay" in v,
        "pakasir TIDAK rusak": "handle_pakasir_callback" in v,
    }
    print()
    for k, ok in cek.items():
        print(f"   {'✅' if ok else '❌'} {k}")
    if not all(cek.values()):
        print("\n❌ Verifikasi gagal → mengembalikan")
        shutil.copy2(cad, BOT); return 1

    print()
    print("CARA BALIK: cp " + os.path.basename(cad) + " bot.py  (lalu restart bot)")
    return 0


if __name__ == "__main__":
    sys.exit(main())