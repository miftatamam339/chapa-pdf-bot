from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Directory configurations (Flat Root Layout)
PROJECT_ROOT = Path(__file__).resolve().parent
PDF_ROOT = PROJECT_ROOT / "pdfs"
DATA_ROOT = PROJECT_ROOT / "storage"
DATABASE_PATH = DATA_ROOT / "orders.sqlite3"
LOCK_PATH = DATA_ROOT / "bot.lock"

# Automatically create directories on startup
PDF_ROOT.mkdir(parents=True, exist_ok=True)
DATA_ROOT.mkdir(parents=True, exist_ok=True)

SOCIAL_SCIENCE = "social"
NATURAL_SCIENCE = "natural"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("ethio-entrance-academy-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Subject:
    code: str
    name: str
    category: str


@dataclass(frozen=True)
class Product:
    product_id: str
    subject: Subject
    title: str
    question_range: str
    price_etb: int
    file_path: Path
    volume: int | None = None


@dataclass(frozen=True)
class PaymentMethod:
    key: str
    label: str
    secret_name: str


SUBJECTS = (
    Subject("geo", "Geography", SOCIAL_SCIENCE),
    Subject("hist", "History", SOCIAL_SCIENCE),
    Subject("eng", "English", SOCIAL_SCIENCE),
    Subject("civ", "Civics", SOCIAL_SCIENCE),
    Subject("bio", "Biology", NATURAL_SCIENCE),
    Subject("phys", "Physics", NATURAL_SCIENCE),
    Subject("chem", "Chemistry", NATURAL_SCIENCE),
    Subject("math", "Mathematics", NATURAL_SCIENCE),
)

PAYMENT_METHODS = (
    PaymentMethod("telebirr", "Telebirr", "TELEBIRR_RECEIVING_ACCOUNT"),
    PaymentMethod("cbe_birr", "CBE Birr", "CBE_BIRR_RECEIVING_ACCOUNT"),
    PaymentMethod("cbe_mobile", "CBE Mobile Banking", "CBE_MOBILE_BANKING_RECEIVING_ACCOUNT"),
)

SUBJECTS_BY_CODE = {s.code: s for s in SUBJECTS}
PAYMENT_METHODS_BY_KEY = {m.key: m for m in PAYMENT_METHODS}


def build_catalog() -> dict[str, Product]:
    catalog: dict[str, Product] = {}
    for subject in SUBJECTS:
        for volume in range(1, 11):
            start = ((volume - 1) * 100) + 1
            end = volume * 100
            product_id = f"{subject.code}_vol_{volume}"
            catalog[product_id] = Product(
                product_id=product_id,
                subject=subject,
                title=f"{subject.name} — Volume {volume}",
                question_range=f"Questions {start:,}–{end:,} of 1,000",
                price_etb=50,
                file_path=PDF_ROOT / f"{subject.code}_vol_{volume}.pdf",
                volume=volume,
            )
        bundle_id = f"{subject.code}_bundle"
        catalog[bundle_id] = Product(
            product_id=bundle_id,
            subject=subject,
            title=f"{subject.name} — Complete 1,000-Question Bundle",
            question_range="Questions 1–1,000",
            price_etb=300,
            file_path=PDF_ROOT / f"{subject.code}_bundle.pdf",
        )
    return catalog


CATALOG = build_catalog()


class OrderStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    product_id TEXT NOT NULL,
                    payment_method TEXT NOT NULL,
                    receipt_message_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                )
                """
            )
            conn.commit()

    def create_order(self, *, user_id: int, chat_id: int, username: str | None, product_id: str, payment_method: str, receipt_message_id: int) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                """
                INSERT INTO orders (user_id, chat_id, username, product_id, payment_method, receipt_message_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (user_id, chat_id, username, product_id, payment_method, receipt_message_id, datetime.now(UTC).isoformat()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return dict(row) if row else None

    def update_status(self, order_id: int, status: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE orders SET status = ?, reviewed_at = ? WHERE id = ?", (status, datetime.now(UTC).isoformat(), order_id))
            conn.commit()


class SingleInstanceLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._file: Any = None

    def acquire(self) -> None:
        self._file = self.lock_path.open("w")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            logger.warning("Another process detected, proceeding carefully...")

    def release(self) -> None:
        if self._file is not None:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                self._file.close()
            except Exception:
                pass
            self._file = None


app = FastAPI(title="Ethio Entrance Academy Bot", version="2.0.0")


@app.get("/")
@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(content={"status": "ok", "service": "ethio-entrance-academy-bot"}, status_code=200)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍 Buy Social Science Worksheets", callback_data=f"category:{SOCIAL_SCIENCE}")],
        [InlineKeyboardButton("🧪 Buy Natural Science Worksheets", callback_data=f"category:{NATURAL_SCIENCE}")],
    ])


def subject_menu(category: str) -> InlineKeyboardMarkup:
    rows = []
    matching = [s for s in SUBJECTS if s.category == category]
    for i in range(0, len(matching), 2):
        rows.append([InlineKeyboardButton(s.name, callback_data=f"subject:{s.code}") for s in matching[i : i + 2]])
    rows.append([InlineKeyboardButton("🏠 Back to Categories", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def product_menu(subject: Subject) -> InlineKeyboardMarkup:
    rows = []
    for start in range(1, 11, 2):
        rows.append([
            InlineKeyboardButton(f"📘 Vol {v} (50 ETB)", callback_data=f"product:{subject.code}_vol_{v}")
            for v in range(start, min(start + 2, 11))
        ])
    rows.append([InlineKeyboardButton("🌟 Complete 1,000-Question Bundle (300 ETB)", callback_data=f"product:{subject.code}_bundle")])
    rows.append([
        InlineKeyboardButton("🔙 Back", callback_data=f"category:{subject.category}"),
        InlineKeyboardButton("🏠 Home", callback_data="home"),
    ])
    return InlineKeyboardMarkup(rows)


def payment_menu(product: Product) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(m.label, callback_data=f"pay:{product.product_id}:{m.key}")] for m in PAYMENT_METHODS]
    rows.append([
        InlineKeyboardButton("🔙 Back", callback_data=f"subject:{product.subject.code}"),
        InlineKeyboardButton("❌ Cancel", callback_data="home"),
    ])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "🏛️ <b>Ethio Entrance Academy & Digital Learning Institute</b>\n\n"
            "Official Grade 12 National Entrance Examination Preparation Portal.\n"
            "Select your academic stream to purchase 1,000 practice worksheets:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""

    if data == "home":
        await query.edit_message_text("Choose a worksheet category:", reply_markup=main_menu())
    elif data.startswith("category:"):
        cat = data.split(":", 1)[1]
        label = "Social Science" if cat == SOCIAL_SCIENCE else "Natural Science"
        await query.edit_message_text(f"<b>{label}</b>\n\nChoose a subject:", parse_mode=ParseMode.HTML, reply_markup=subject_menu(cat))
    elif data.startswith("subject:"):
        code = data.split(":", 1)[1]
        subj = SUBJECTS_BY_CODE.get(code)
        if subj:
            await query.edit_message_text(f"<b>{subj.name}</b>\n\nSelect a volume or full bundle:", parse_mode=ParseMode.HTML, reply_markup=product_menu(subj))
    elif data.startswith("product:"):
        pid = data.split(":", 1)[1]
        prod = CATALOG.get(pid)
        if prod:
            context.user_data["selected_product_id"] = prod.product_id
            await query.edit_message_text(
                f"<b>{prod.title}</b>\n\nSubject: <b>{prod.subject.name}</b>\nCoverage: <b>{prod.question_range}</b>\nPrice: <b>{prod.price_etb} ETB</b>\n\nChoose payment method:",
                parse_mode=ParseMode.HTML,
                reply_markup=payment_menu(prod),
            )
    elif data.startswith("pay:"):
        _, pid, pkey = data.split(":")
        prod = CATALOG.get(pid)
        method = PAYMENT_METHODS_BY_KEY.get(pkey)
        if prod and method:
            context.user_data["pending_product_id"] = prod.product_id
            context.user_data["pending_payment_method"] = method.key
            instructions = context.application.bot_data["payment_instructions"].get(method.key, "")
            await query.edit_message_text(
                f"<b>{prod.title}</b>\nAmount: <b>{prod.price_etb} ETB</b>\nPayment Method: <b>{method.label}</b>\n\n"
                f"<b>Remittance Instructions:</b>\n{instructions}\n\n"
                "After payment, upload your transaction screenshot or receipt here.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Purchase", callback_data="home")]]),
            )


async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not (msg and user and chat):
        return

    pid = context.user_data.get("pending_product_id")
    pkey = context.user_data.get("pending_payment_method")
    if not (pid and pkey):
        await msg.reply_text("Please select a module first using /start.", reply_markup=main_menu())
        return

    prod = CATALOG.get(pid)
    method = PAYMENT_METHODS_BY_KEY.get(pkey)
    if not (prod and method):
        return

    store: OrderStore = context.application.bot_data["store"]
    order_id = store.create_order(
        user_id=user.id,
        chat_id=chat.id,
        username=user.username,
        product_id=prod.product_id,
        payment_method=method.key,
        receipt_message_id=msg.message_id,
    )

    admin_ids = context.application.bot_data["admin_ids"]
    file_state = "✅ PDF Ready in pdfs/" if prod.file_path.is_file() else f"⚠️ PDF Missing ({prod.file_path.name})"
    caption = (
        f"🚨 <b>New Remittance Receipt | Order #{order_id}</b>\n"
        f"Student: <b>{user.full_name}</b> (@{user.username or 'None'})\n"
        f"ID: <code>{user.id}</code>\n"
        f"Module: <b>{prod.title}</b>\n"
        f"Price: <b>{prod.price_etb} ETB</b> ({method.label})\n"
        f"File: {file_state}"
    )
    admin_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve & Send PDF", callback_data=f"approve:{order_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"decline:{order_id}")
    ]])

    for aid in admin_ids:
        try:
            await context.bot.forward_message(chat_id=aid, from_chat_id=chat.id, message_id=msg.message_id)
            await context.bot.send_message(chat_id=aid, text=caption, parse_mode=ParseMode.HTML, reply_markup=admin_kb)
        except Exception as e:
            logger.error(f"Failed to alert admin {aid}: {e}")

    await msg.reply_text(f"📥 Receipt submitted for Order #{order_id}. Verified modules will be sent automatically upon approval.", reply_markup=main_menu())


async def admin_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    admin_ids = context.application.bot_data["admin_ids"]
    if query.from_user.id not in admin_ids:
        await query.answer("Unauthorized.", show_alert=True)
        return
    await query.answer()

    action, order_text = (query.data or "").split(":", 1)
    order_id = int(order_text)
    store: OrderStore = context.application.bot_data["store"]
    order = store.get_order(order_id)
    if not order or order["status"] != "pending":
        await query.edit_message_text(f"Order #{order_id} already settled.")
        return

    prod = CATALOG.get(order["product_id"])
    if not prod:
        return

    if action == "approve":
        if not prod.file_path.is_file():
            await query.answer(f"Missing PDF: {prod.file_path.name}. Upload to pdfs/ first!", show_alert=True)
            return
        try:
            with prod.file_path.open("rb") as f:
                await context.bot.send_document(
                    chat_id=order["chat_id"],
                    document=InputFile(f, filename=prod.file_path.name),
                    caption=f"🎉 Payment Verified!\n\n📘 {prod.title}\nEthio Entrance Academy & Digital Learning Institute."
                )
            store.update_status(order_id, "approved")
            await query.edit_message_text(f"✅ Order #{order_id} approved. Document delivered.")
        except Exception as e:
            logger.error(f"Delivery failed: {e}")
            await query.answer("Delivery failed. Check logs.", show_alert=True)
    elif action == "decline":
        store.update_status(order_id, "declined")
        await context.bot.send_message(chat_id=order["chat_id"], text="❌ Your transaction voucher could not be verified. Please contact support: @MiftaSupport.")
        await query.edit_message_text(f"❌ Order #{order_id} declined.")


async def run_server() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing.")

    admin_raw = os.getenv("ADMIN_TELEGRAM_IDS", "6612835179")
    admin_ids = frozenset([int(x.strip()) for x in admin_raw.split(",") if x.strip().isdigit()])

    port = int(os.getenv("PORT", "8000"))

    payment_instructions = {
        "telebirr": os.getenv("TELEBIRR_RECEIVING_ACCOUNT", "Telebirr: 0979304102 (Mifta Temam Aliy)"),
        "cbe_birr": os.getenv("CBE_BIRR_RECEIVING_ACCOUNT", "CBE Birr: 0979304102 (Miftahw Tamam Aliyi)"),
        "cbe_mobile": os.getenv("CBE_MOBILE_BANKING_RECEIVING_ACCOUNT", "CBE Bank: 1000222146511 (Mifta Temam Aliy)"),
    }

    store = OrderStore(DATABASE_PATH)
    lock = SingleInstanceLock(LOCK_PATH)
    lock.acquire()

    tg_app = ApplicationBuilder().token(token).build()
    tg_app.bot_data["store"] = store
    tg_app.bot_data["admin_ids"] = admin_ids
    tg_app.bot_data["payment_instructions"] = payment_instructions

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(admin_review, pattern=r"^(approve|decline):"))
    tg_app.add_handler(CallbackQueryHandler(button_click))
    tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_receipt))

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)

    logger.info("Starting Telegram Bot and Uptime Health Server on Port %s...", port)
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    try:
        await server.serve()
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        lock.release()


if __name__ == "__main__":
    asyncio.run(run_server())