import os
import asyncio
import re
import logging
import sqlite3
import io
from datetime import datetime, timezone, timedelta
from contextlib import closing

from dotenv import load_dotenv
import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8979745668:AAE0r9ndCBmAoS-LOYtI8U0NjYq_RXBcDH4").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "rocks_neon.db")
ADMIN_IDS = {
    int(value.strip()) for value in os.getenv("ADMIN_IDS", "8076922044").split(",")
    if value.strip().isdigit()
}
CURRENCY = os.getenv("CURRENCY", "INR")
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("rocks-neon")

ADD_NAME, ADD_PRICE, ADD_DESCRIPTION = range(3)

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    return sqlite3.connect(DATABASE_PATH)


def init_db():
    with closing(db()) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_type TEXT NOT NULL CHECK(card_type IN ('VISA','MASTERCARD')),
            name TEXT NOT NULL,
            price REAL NOT NULL CHECK(price >= 0),
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'PENDING',
            card_status TEXT NOT NULL DEFAULT 'NOT_ISSUED',
            issuer_reference TEXT,
            utr TEXT,
            proof_file_id TEXT,
            payment_deadline TEXT,
            review_deadline TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        """)
        cols = {r[1] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
        for col in ("utr", "proof_file_id", "payment_deadline", "review_deadline"):
            if col not in cols:
                con.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
        con.execute("CREATE TABLE IF NOT EXISTS wallets (telegram_id INTEGER PRIMARY KEY, credits REAL NOT NULL DEFAULT 0)")
        con.commit()


def get_setting(key, default=""):
    with closing(db()) as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key, value):
    with closing(db()) as con:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        con.commit()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def save_user(user):
    with closing(db()) as con:
        con.execute(
            """INSERT INTO users(telegram_id, username, first_name, created_at)
               VALUES(?,?,?,?)
               ON CONFLICT(telegram_id) DO UPDATE SET
               username=excluded.username, first_name=excluded.first_name""",
            (user.id, user.username or "", user.first_name or "", now())
        )
        con.commit()


def money(amount):
    return f"{CURRENCY} {amount:,.2f}"


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Choose Your Card", callback_data="choose")],
        [InlineKeyboardButton("🛒 Card Store", callback_data="store")],
        [InlineKeyboardButton("🪪 My Cards", callback_data="mycards"),
         InlineKeyboardButton("📦 My Orders", callback_data="myorders")],
        [InlineKeyboardButton("💰 Wallet / Payments", callback_data="payments"),
         InlineKeyboardButton("👤 Profile", callback_data="profile")],
        [InlineKeyboardButton("🆘 Support", callback_data="support")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    await update.message.reply_text(
        f"✨ *Welcome to ROCKS NEON* ✨\n\n"
        f"Browse available card products and manage your orders here.\n\n"
        f"Choose an option below:",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    await update.message.reply_text("💎 *ROCKS NEON Main Menu*", reply_markup=main_keyboard(),
                                    parse_mode="Markdown")


def get_products(card_type=None):
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        if card_type:
            rows = con.execute(
                "SELECT * FROM products WHERE active=1 AND card_type=? ORDER BY id DESC",
                (card_type,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM products WHERE active=1 ORDER BY id DESC"
            ).fetchall()
        return rows


async def show_store(query, card_type=None):
    products = get_products(card_type)
    if not products:
        await query.edit_message_text(
            "🛍️ No products are available right now.\nPlease check again later.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="home")]])
        )
        return
    rows = []
    for p in products:
        label = f"{'💳' if p['card_type']=='VISA' else '🟡'} {p['card_type']} · {p['name']} · {money(p['price'])}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"product:{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="choose")])
    await query.edit_message_text("🛒 *Available Card Products*\n\nSelect a product:",
                                  reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")


async def product_detail(query, product_id):
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        p = con.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
    if not p:
        await query.edit_message_text("This product is no longer available.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Store", callback_data="store")]]))
        return
    text = (
        f"💳 *{p['card_type']} — {p['name']}*\n\n"
        f"{p['description'] or 'Premium card product'}\n\n"
        f"Price: *{money(p['price'])}*\n"
        f"Status: {'Available' if p['active'] else 'Unavailable'}\n\n"
        "Select Buy Now to create an order. Payment and card issuance require an authorized provider."
    )
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", callback_data=f"buy:{p['id']}")],
        [InlineKeyboardButton("⬅️ Back to Store", callback_data=f"store:{p['card_type']}")]
    ]))


async def countdown_message(bot, chat_id, message_id, order_id, deadline, label):
    """Edit a visible countdown periodically without flooding Telegram."""
    while True:
        remaining = max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()))
        minutes, seconds = divmod(remaining, 60)
        text = f"⏳ {label}: {minutes:02d}:{seconds:02d}\nOrder RN-{order_id:06d}"
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            # Message may have been edited/deleted or Telegram may rate-limit edits.
            log.debug("Countdown edit skipped: %s", exc)
        if remaining <= 0:
            break
        await asyncio.sleep(min(5, remaining))


async def expire_qr_if_unclaimed(bot, chat_id, order_id, deadline):
    delay = max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
    await asyncio.sleep(delay)
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        o = con.execute("SELECT payment_status,payment_deadline FROM orders WHERE id=?", (order_id,)).fetchone()
        if o and o["payment_status"] == "PENDING" and o["payment_deadline"] == deadline.isoformat(timespec="seconds"):
            con.execute("UPDATE orders SET payment_status='EXPIRED' WHERE id=?", (order_id,))
            con.commit()
            try:
                await bot.send_message(chat_id, f"⌛ QR payment window expired for RN-{order_id:06d}. Please create a new order.")
            except Exception:
                log.exception("Could not send QR expiry notice")


async def create_order(query, user_id, product_id, context):
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        p = con.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if not p:
            await query.edit_message_text("This product is unavailable.")
            return
        cur = con.execute("INSERT INTO orders(telegram_id,product_id,amount,created_at) VALUES(?,?,?,?)",
                          (user_id, product_id, p["price"], now()))
        order_id = cur.lastrowid
        con.commit()
    upi_id = get_setting("upi_id", "").strip()
    if not upi_id:
        await query.edit_message_text(
            f"Order RN-{order_id:06d} created. UPI is not configured; order remains PENDING.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Support", callback_data="support")]]))
        return
    import urllib.parse
    payee = urllib.parse.quote(upi_id, safe="")
    note = urllib.parse.quote(f"ROCKS NEON Order {order_id}", safe="")
    uri = f"upi://pay?pa={payee}&pn=ROCKS%20NEON&am={p['price']:.2f}&cu={CURRENCY}&tn={note}"
    qr = qrcode.make(uri)
    image = io.BytesIO()
    image.name = f"order_{order_id}.png"
    qr.save(image, format="PNG")
    image.seek(0)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    deadline_text = deadline.isoformat(timespec="seconds")
    with closing(db()) as con:
        con.execute("UPDATE orders SET payment_deadline=? WHERE id=?", (deadline_text, order_id))
        con.commit()
    await query.edit_message_text(
        f"🧾 Order RN-{order_id:06d}\nProduct: {p['card_type']} — {p['name']}\n"
        f"Amount: {money(p['price'])}\nPayment: PENDING\n\n"
        "⏳ QR is valid for 5 minutes. After paying, tap ‘I have paid’ and submit the UTR and payment screenshot within 9 minutes. "
        "Admin will verify the payment against actual merchant records before approving credits.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ I have paid", callback_data=f"paid:{order_id}")],
            [InlineKeyboardButton("📦 My Orders", callback_data="myorders")],
            [InlineKeyboardButton("🆘 Support", callback_data="support")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))
    photo = await context.bot.send_photo(chat_id=query.message.chat_id, photo=image,
                                 caption=f"UPI QR · RN-{order_id:06d} · {money(p['price'])} · Pay within 05:00")
    timer_msg = await context.bot.send_message(query.message.chat_id, "⏳ QR payment countdown: 05:00")
    context.application.create_task(countdown_message(context.bot, query.message.chat_id, timer_msg.message_id, order_id, deadline, "QR payment time left"))
    context.application.create_task(expire_qr_if_unclaimed(context.bot, query.message.chat_id, order_id, deadline))


async def my_orders(query, user_id):
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT o.id,o.amount,o.payment_status,o.card_status,o.created_at,p.name,p.card_type
            FROM orders o JOIN products p ON p.id=o.product_id
            WHERE o.telegram_id=? ORDER BY o.id DESC LIMIT 20
        """, (user_id,)).fetchall()
    if not rows:
        text = "📦 You have no orders yet."
    else:
        chunks = ["📦 *Your Recent Orders*\n"]
        for o in rows:
            chunks.append(
                f"\n*RN-{o['id']:06d}* · {o['card_type']} — {o['name']}\n"
                f"Amount: {money(o['amount'])}\nPayment: {o['payment_status']}\n"
                f"Card: {o['card_status']}\nDate: {o['created_at'][:10]}"
            )
        text = "\n".join(chunks)
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


async def my_cards(query, user_id):
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT o.id,o.card_status,o.issuer_reference,p.name,p.card_type
            FROM orders o JOIN products p ON p.id=o.product_id
            WHERE o.telegram_id=? AND o.payment_status='PAID'
            ORDER BY o.id DESC LIMIT 20
        """, (user_id,)).fetchall()
    if not rows:
        text = "🪪 *My Cards*\n\nNo issuer-confirmed cards are available yet."
    else:
        chunks = ["🪪 *My Cards*\n"]
        for c in rows:
            chunks.append(
                f"\n💳 *{c['card_type']} — {c['name']}*\n"
                f"Order: RN-{c['id']:06d}\nStatus: {c['card_status']}\n"
                f"Issuer reference: `{c['issuer_reference'] or 'Not assigned'}`\n"
                "Use the issuer's official secure flow. Never share this reference publicly."
            )
        text = "\n".join(chunks)
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


async def receive_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.get("proof_order_id")
    if not order_id or not update.message.text:
        return
    utr = update.message.text.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{6,40}", utr):
        await update.message.reply_text("Send a valid UTR/reference (6–40 letters or numbers).")
        return
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        o = con.execute("SELECT * FROM orders WHERE id=? AND telegram_id=?", (order_id, update.effective_user.id)).fetchone()
        if not o or o["payment_status"] != "PENDING":
            context.user_data.pop("proof_order_id", None)
            await update.message.reply_text("This order is no longer awaiting proof.")
            return
        proof_deadline = o["review_deadline"] or o["payment_deadline"]
        if proof_deadline and datetime.now(timezone.utc) > datetime.fromisoformat(proof_deadline):
            context.user_data.pop("proof_order_id", None)
            await update.message.reply_text("The 9-minute proof submission window expired. Please create a new order.")
            return
        con.execute("UPDATE orders SET utr=? WHERE id=?", (utr, order_id)); con.commit()
    context.user_data["awaiting_proof_photo"] = order_id
    await update.message.reply_text("UTR received. Now send the payment screenshot as a photo. Hide unrelated personal/bank information first.")


async def receive_proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.get("awaiting_proof_photo")
    if not order_id or not update.message.photo:
        return
    file_id = update.message.photo[-1].file_id
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        o = con.execute("SELECT * FROM orders WHERE id=? AND telegram_id=?", (order_id, update.effective_user.id)).fetchone()
        if not o or o["payment_status"] != "PENDING" or not o["utr"]:
            await update.message.reply_text("Order or UTR missing. Start the payment flow again.")
            return
        proof_deadline = o["review_deadline"] or o["payment_deadline"]
        if proof_deadline and datetime.now(timezone.utc) > datetime.fromisoformat(proof_deadline):
            context.user_data.pop("proof_order_id", None)
            context.user_data.pop("awaiting_proof_photo", None)
            await update.message.reply_text("The 9-minute proof submission window expired. Please create a new order.")
            return
        con.execute("UPDATE orders SET proof_file_id=? WHERE id=?", (file_id, order_id)); con.commit()
    context.user_data.pop("proof_order_id", None); context.user_data.pop("awaiting_proof_photo", None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Verify & Approve", callback_data=f"review:yes:{order_id}"),
                                InlineKeyboardButton("❌ Reject", callback_data=f"review:no:{order_id}")]])
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_photo(aid, photo=file_id,
                caption=f"PAYMENT PROOF · RN-{order_id:06d}\nUser: {update.effective_user.id}\nAmount: {money(o['amount'])}\nUTR: {o['utr']}\nReview target: 9 minutes. Verify in merchant records before approving.",
                reply_markup=kb)
        except Exception:
            log.exception("Admin proof notification failed")
    await update.message.reply_text(f"📨 Proof submitted for RN-{order_id:06d}. Admin review target is 9 minutes. Credits are added only after manual verification.")


async def review_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_caption(caption="Unauthorized."); return
    _, decision, raw_id = q.data.split(":"); order_id = int(raw_id)
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        o = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o or o["payment_status"] != "PENDING" or not o["proof_file_id"] or not o["utr"]:
            await q.edit_message_caption(caption="Order is not awaiting review."); return
        if decision == "yes":
            con.execute("UPDATE orders SET payment_status='PAID',card_status='PAYMENT_APPROVED' WHERE id=?", (order_id,))
            con.execute("INSERT INTO wallets(telegram_id,credits) VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET credits=credits+excluded.credits", (o["telegram_id"], o["amount"]))
            status = "APPROVED"
        else:
            con.execute("UPDATE orders SET payment_status='REJECTED',card_status='NOT_ISSUED' WHERE id=?", (order_id,)); status = "REJECTED"
        con.commit()
    await q.edit_message_caption(caption=f"RN-{order_id:06d} · {status} by admin {q.from_user.id}.")
    msg = f"✅ Payment approved for RN-{order_id:06d}. Credits added: {money(o['amount'])}" if decision == "yes" else f"❌ Payment rejected for RN-{order_id:06d}. No credits were added."
    await context.bot.send_message(o["telegram_id"], msg)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    save_user(q.from_user)
    data = q.data
    if data.startswith("paid:"):
        try:
            order_id = int(data.split(":", 1)[1])
        except ValueError:
            await q.edit_message_text("Invalid order.")
            return
        with closing(db()) as con:
            con.row_factory = sqlite3.Row
            o = con.execute("SELECT * FROM orders WHERE id=? AND telegram_id=?", (order_id, user_id)).fetchone()
            if not o or o["payment_status"] != "PENDING":
                await q.edit_message_text("This order is not awaiting payment.")
                return
            if not o["payment_deadline"] or datetime.now(timezone.utc) > datetime.fromisoformat(o["payment_deadline"]):
                con.execute("UPDATE orders SET payment_status='EXPIRED' WHERE id=? AND payment_status='PENDING'", (order_id,))
                con.commit()
                await q.edit_message_text("The 5-minute QR window has expired. Please create a new order.")
                return
            proof_deadline = datetime.now(timezone.utc) + timedelta(minutes=9)
            con.execute("UPDATE orders SET review_deadline=? WHERE id=?", (proof_deadline.isoformat(timespec="seconds"), order_id))
            con.commit()
        context.user_data["proof_order_id"] = order_id
        await q.message.reply_text(f"🧾 Send the UTR/reference for RN-{order_id:06d}. You have 09:00 minutes to submit UTR and screenshot.")
        timer_msg = await q.message.reply_text("⏳ UTR and screenshot submission time: 09:00")
        context.application.create_task(countdown_message(context.bot, q.message.chat_id, timer_msg.message_id, order_id, proof_deadline, "Proof submission time left"))
        return
    if data == "home":
        await q.edit_message_text("✨ *ROCKS NEON Main Menu*", parse_mode="Markdown", reply_markup=main_keyboard())
    elif data == "choose":
        await q.edit_message_text("💳 *Choose Your Card Type*",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Visa Card", callback_data="store:VISA")],
                [InlineKeyboardButton("🟡 Mastercard", callback_data="store:MASTERCARD")],
                [InlineKeyboardButton("🛍️ All Products", callback_data="store")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")]
            ]))
    elif data == "store":
        await show_store(q)
    elif data.startswith("store:"):
        await show_store(q, data.split(":",1)[1])
    elif data.startswith("product:"):
        await product_detail(q, int(data.split(":",1)[1]))
    elif data.startswith("buy:"):
        await create_order(q, user_id, int(data.split(":",1)[1]), context)
    elif data == "myorders":
        await my_orders(q, user_id)
    elif data == "mycards":
        await my_cards(q, user_id)
    elif data == "payments":
        await q.edit_message_text("💰 Payment history will appear here after a real payment provider is integrated.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))
    elif data == "profile":
        await q.edit_message_text(f"👤 *Your Profile*\n\nTelegram ID: `{user_id}`\nNo separate website login is required.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))
    elif data == "support":
        support = get_setting("support", "Please contact the bot administrator for support.")
        await q.edit_message_text(f"🆘 Support\\n\\n{support}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Visa Product", callback_data="admin:add:VISA")],
        [InlineKeyboardButton("➕ Add Mastercard Product", callback_data="admin:add:MASTERCARD")],
        [InlineKeyboardButton("📋 List Products", callback_data="admin:products")],
        [InlineKeyboardButton("📦 Recent Orders", callback_data="admin:orders")],
        [InlineKeyboardButton("🔗 Set UPI ID", callback_data="admin:setupi")],
        [InlineKeyboardButton("🆘 Set Support", callback_data="admin:setsupport")]
    ])


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    await update.message.reply_text("👑 *ROCKS NEON Admin Panel*", parse_mode="Markdown", reply_markup=admin_keyboard())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_text("⛔ Unauthorized.")
        return
    if q.data.startswith("admin:add:"):
        kind = q.data.split(":")[-1]
        if kind not in ("VISA", "MASTERCARD"):
            await q.edit_message_text("Invalid card type.")
            return
        context.user_data["admin_stage"] = "product_name"
        context.user_data["admin_kind"] = kind
        await q.edit_message_text(f"Send {kind} product name. Do not send real card numbers, expiry dates or CVV.")
        return
    if q.data == "admin:setupi":
        context.user_data["admin_stage"] = "upi_id"
        await q.edit_message_text("Send merchant UPI ID only. Never send UPI PIN or bank password.")
        return
    if q.data == "admin:setsupport":
        context.user_data["admin_stage"] = "support"
        await q.edit_message_text("Send the support text or official support link.")
        return
    if q.data == "admin:products":
        products = get_products()
        if not products:
            await q.edit_message_text("No products found.", reply_markup=admin_keyboard())
            return
        lines = ["📋 *Active Products*"]
        for p in products:
            lines.append(f"\nID `{p['id']}` · {p['card_type']} · {p['name']} · {money(p['price'])}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_keyboard())
    elif q.data == "admin:orders":
        with closing(db()) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("""
                SELECT o.id,o.telegram_id,o.amount,o.payment_status,o.card_status,p.name,p.card_type
                FROM orders o JOIN products p ON p.id=o.product_id ORDER BY o.id DESC LIMIT 15
            """).fetchall()
        if not rows:
            await q.edit_message_text("No orders found.", reply_markup=admin_keyboard())
            return
        lines = ["📦 *Recent Orders*"]
        for o in rows:
            lines.append(f"\nRN-{o['id']:06d} · User `{o['telegram_id']}`\n{o['card_type']} {o['name']} · {money(o['amount'])}\n{o['payment_status']} / {o['card_status']}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_keyboard())


async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return ConversationHandler.END
    card_type = context.args[0].upper() if context.args else ""
    if card_type not in ("VISA", "MASTERCARD"):
        await update.message.reply_text("Usage: /addproduct VISA or /addproduct MASTERCARD")
        return ConversationHandler.END
    context.user_data["new_product_type"] = card_type
    await update.message.reply_text(f"Enter the {card_type} product name:")
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or len(name) > 80:
        await update.message.reply_text("Please enter a name between 1 and 80 characters.")
        return ADD_NAME
    context.user_data["new_product_name"] = name
    await update.message.reply_text("Enter product price as a number (example: 500):")
    return ADD_PRICE


async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price < 0 or price > 10000000:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Invalid price. Enter a valid non-negative number:")
        return ADD_PRICE
    context.user_data["new_product_price"] = price
    await update.message.reply_text("Enter a short product description:")
    return ADD_DESCRIPTION


async def add_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = update.message.text.strip()[:1000]
    card_type = context.user_data["new_product_type"]
    name = context.user_data["new_product_name"]
    price = context.user_data["new_product_price"]
    with closing(db()) as con:
        cur = con.execute(
            "INSERT INTO products(card_type,name,price,description,active,created_at) VALUES(?,?,?,?,1,?)",
            (card_type, name, price, description, now())
        )
        product_id = cur.lastrowid
        con.commit()
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ Product added.\nID: {product_id}\nType: {card_type}\nName: {name}\nPrice: {money(price)}\n\n"
        "This adds a catalog product only. Genuine card issuance requires an authorized issuer integration."
    )
    return ConversationHandler.END


async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Product creation cancelled.")
    return ConversationHandler.END


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    stage = context.user_data.get("admin_stage")
    if not stage:
        return
    value = update.message.text.strip()
    if stage == "upi_id":
        if len(value) > 120 or " " in value or "@" not in value:
            await update.message.reply_text("Invalid-looking UPI ID. Send a valid UPI ID or /cancel.")
            return
        set_setting("upi_id", value)
        context.user_data.clear()
        await update.message.reply_text("UPI ID saved. QR generation is enabled; payments still require independent verification.")
    elif stage == "support":
        if len(value) > 500:
            await update.message.reply_text("Keep support text under 500 characters.")
            return
        set_setting("support", value)
        context.user_data.clear()
        await update.message.reply_text("Support option updated.")
    elif stage == "product_name":
        if not value or len(value) > 80:
            await update.message.reply_text("Name must be 1–80 characters.")
            return
        context.user_data["admin_name"] = value
        context.user_data["admin_stage"] = "product_price"
        await update.message.reply_text("Send product price as a number:")
    elif stage == "product_price":
        try:
            price = float(value)
            if not 0 <= price <= 10000000: raise ValueError()
        except ValueError:
            await update.message.reply_text("Invalid price. Send a valid non-negative number:")
            return
        context.user_data["admin_price"] = price
        context.user_data["admin_stage"] = "product_description"
        await update.message.reply_text("Send product description:")
    elif stage == "product_description":
        kind = context.user_data["admin_kind"]
        name = context.user_data["admin_name"]
        price = context.user_data["admin_price"]
        with closing(db()) as con:
            cur = con.execute("INSERT INTO products(card_type,name,price,description,active,created_at) VALUES(?,?,?,?,1,?)",
                              (kind, name, price, value[:1000], now()))
            pid = cur.lastrowid
            con.commit()
        context.user_data.clear()
        await update.message.reply_text(f"Product added to {kind} catalog. ID: {pid}. Genuine card issuance requires an authorized issuer integration.")


async def cancel_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Admin action cancelled.")


async def fulfill_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin fulfillment using an issuer-generated non-sensitive reference only."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: /fulfill ORDER_ID ISSUER_REFERENCE\n"
            "Use only an issuer-generated token/reference, never PAN, expiry, CVV, PIN, or OTP."
        )
        return
    try:
        order_id = int(context.args[0])
        reference = context.args[1].strip()
        if order_id < 1 or not re.fullmatch(r"[A-Za-z0-9._:-]{3,100}", reference):
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Invalid order ID or issuer reference.")
        return
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        order = con.execute("SELECT telegram_id,payment_status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            await update.message.reply_text("Order not found.")
            return
        if order["payment_status"] != "PENDING":
            await update.message.reply_text("Order is not pending; no changes made.")
            return
        # Run only after independently verifying payment and issuer-side issuance.
        con.execute("UPDATE orders SET payment_status='PAID',card_status='ISSUER_REFERENCE_READY',issuer_reference=? WHERE id=?", (reference, order_id))
        con.commit()
    try:
        await context.bot.send_message(chat_id=order["telegram_id"], text=(f"✅ Order RN-{order_id:06d} has been verified and fulfilled.\nYour issuer reference is available in 🪪 My Cards. Use the issuer's official secure flow for any card access."))
    except Exception:
        pass
    await update.message.reply_text(f"Order RN-{order_id:06d} updated. Stored only the issuer reference; no card credentials were collected.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    with closing(db()) as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        products = con.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
        orders = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM orders WHERE payment_status='PENDING'").fetchone()[0]
    await update.message.reply_text(
        f"📊 *ROCKS NEON Statistics*\n\nUsers: {users}\nActive products: {products}\nOrders: {orders}\nPending orders: {pending}",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — Open bot\n/menu — Main menu\n/store — Browse products\n/mycards — My cards\n/myorders — My orders\n/admin — Admin panel\n/addproduct VISA or MASTERCARD — Add catalog product (admin)\n/stats — Bot statistics (admin)"
    )


async def store_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    products = get_products()
    if not products:
        await update.message.reply_text("No products are available right now.")
        return
    buttons = [[InlineKeyboardButton(
        f"{p['card_type']} · {p['name']} · {money(p['price'])}"[:60],
        callback_data=f"product:{p['id']}"
    )] for p in products]
    await update.message.reply_text("🛒 Available products:", reply_markup=InlineKeyboardMarkup(buttons))


async def myorders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    q = update.message
    # Reuse a plain-message summary for command invocation.
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT o.id,o.amount,o.payment_status,o.card_status,o.created_at,p.name,p.card_type
            FROM orders o JOIN products p ON p.id=o.product_id
            WHERE o.telegram_id=? ORDER BY o.id DESC LIMIT 20
        """, (update.effective_user.id,)).fetchall()
    if not rows:
        await q.reply_text("📦 You have no orders yet.")
        return
    lines = ["📦 Your Recent Orders"]
    for o in rows:
        lines.append(f"\nRN-{o['id']:06d} · {o['card_type']} — {o['name']}\nAmount: {money(o['amount'])}\nPayment: {o['payment_status']}\nCard: {o['card_status']}")
    await q.reply_text("\n".join(lines))


async def mycards_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    with closing(db()) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT o.id,o.card_status,p.name,p.card_type FROM orders o
            JOIN products p ON p.id=o.product_id
            WHERE o.telegram_id=? AND o.payment_status='PAID' ORDER BY o.id DESC
        """, (update.effective_user.id,)).fetchall()
    if not rows:
        await update.message.reply_text("🪪 No issuer-confirmed cards are available yet.")
        return
    lines = ["🪪 My Cards"]
    for c in rows:
        lines.append(f"\n{c['card_type']} — {c['name']}\nOrder: RN-{c['id']:06d}\nStatus: {c['card_status']}")
    await update.message.reply_text("\n".join(lines))


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in your .env file.")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS is missing. Set at least one Telegram numeric user ID in .env.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("Unhandled update error", exc_info=context.error)
    app.add_error_handler(on_error)
    add_product_conv = ConversationHandler(
        entry_points=[CommandHandler("addproduct", add_product_start)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
            ADD_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_description)],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
        conversation_timeout=300,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("store", store_command))
    app.add_handler(CommandHandler("myorders", myorders_command))
    app.add_handler(CommandHandler("mycards", mycards_command))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("fulfill", fulfill_order))
    app.add_handler(CommandHandler("cancel", cancel_admin_input))
    app.add_handler(add_product_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_input))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(review_payment, pattern=r"^review:"))
    app.add_handler(MessageHandler(filters.PHOTO, receive_proof_photo), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_utr), group=1)
    app.add_handler(CallbackQueryHandler(callback_router))
    log.info("ROCKS NEON bot is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
