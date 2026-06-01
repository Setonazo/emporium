import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import (
    add_product,
    delete_credentials,
    get_all_products,
    get_credentials,
    get_products,
    init_db,
    remove_product,
    set_credentials,
    update_stock,
)
from scraper import check_stock

load_dotenv()

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "60")) * 60

# Conversation states for /login
WAITING_EMAIL, WAITING_PASSWORD = range(2)

# Prevents overlapping scheduled check cycles
_scheduled_check_running = False


def _stock_icon(val) -> str:
    if val == 1:
        return "✅"
    if val == 0:
        return "❌"
    return "❓"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    creds = get_credentials(chat_id)
    login_hint = "" if creds else "\n⚠️ Primero configura tus credenciales con /login"
    await update.message.reply_text(
        "👋 *Emporium Stock Bot*\n\n"
        "Te aviso cuando un producto de Dufry Emporium vuelva a tener stock.\n\n"
        "*Comandos:*\n"
        "• `/login` — Configurar tus credenciales de Emporium\n"
        "• `/logout` — Eliminar tus credenciales\n"
        "• `/add <url>` — Añadir producto al seguimiento\n"
        "• `/list` — Ver tus productos\n"
        "• `/remove` — Eliminar un producto\n"
        "• `/check` — Comprobar stock ahora mismo\n"
        + login_hint,
        parse_mode="Markdown",
    )


# ── /login conversation ───────────────────────────────────────────────────────

async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 *Configurar credenciales de Emporium*\n\n"
        "Introduce tu email de Club Avolta / Emporium:\n"
        "_(Escribe /cancel para cancelar)_",
        parse_mode="Markdown",
    )
    return WAITING_EMAIL


async def got_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip()
    if "@" not in email or "." not in email:
        await update.message.reply_text(
            "No parece un email válido. Inténtalo de nuevo:"
        )
        return WAITING_EMAIL
    context.user_data["pending_email"] = email
    await update.message.reply_text(
        "Ahora introduce tu contraseña:"
    )
    return WAITING_PASSWORD


async def got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = context.user_data.pop("pending_email", None)
    password = update.message.text.strip()
    chat_id = update.effective_chat.id

    if not email:
        await update.message.reply_text("Algo fue mal. Usa /login de nuevo.")
        return ConversationHandler.END

    set_credentials(chat_id, email, password)
    await update.message.reply_text(
        "✅ *Credenciales guardadas.*\n\n"
        "Ya puedes añadir productos con /add\n"
        "Para eliminar tus credenciales usa /logout",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_email", None)
    await update.message.reply_text("Cancelado.")
    return ConversationHandler.END


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    delete_credentials(chat_id)
    await update.message.reply_text(
        "🔓 Credenciales eliminadas.\n"
        "Tus productos siguen en la lista pero no se comprobarán hasta que hagas /login de nuevo."
    )


# ── Product commands ──────────────────────────────────────────────────────────

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    creds = get_credentials(chat_id)
    if not creds:
        await update.message.reply_text(
            "Primero configura tus credenciales con /login"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Uso: `/add <url> [url2] [url3] ...`\n\n"
            "Ejemplo:\n"
            "`/add https://esfnf.emporium.dufry.com/producto-1 https://esfnf.emporium.dufry.com/producto-2`",
            parse_mode="Markdown",
        )
        return

    urls = [a.strip() for a in context.args if a.strip().startswith("http")]
    invalid = [a for a in context.args if not a.strip().startswith("http")]

    if invalid:
        await update.message.reply_text(
            f"URLs ignoradas (no empiezan por http): {', '.join(invalid)}"
        )

    if not urls:
        await update.message.reply_text(
            "No se encontró ninguna URL válida."
        )
        return

    email, password = creds
    msg = await update.message.reply_text(
        f"🔍 Procesando {len(urls)} producto(s)…"
    )

    lines = []
    for url in urls:
        result = await check_stock(url, email=email, password=password, chat_id=chat_id)
        name = result.name or url.rstrip("/").split("/")[-1]

        try:
            pid = add_product(chat_id, url, name)
            if result.in_stock is not None:
                update_stock(pid, result.in_stock, result.name)

            if result.in_stock is True:
                lines.append(f"✅ *{name}* — ya en stock")
            elif result.in_stock is False:
                lines.append(f"❌ *{name}* — sin stock, te avisaré")
            else:
                note = f" _{result.error}_" if result.error else ""
                lines.append(f"❓ *{name}* — estado desconocido{note}")

        except ValueError:
            lines.append(f"⚠️ *{name}* — ya estaba en seguimiento")

        await asyncio.sleep(2)

    await msg.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_products(update.effective_chat.id)
    if not rows:
        await update.message.reply_text(
            "No tienes productos en seguimiento. Usa /add para añadir uno."
        )
        return

    lines = ["*Tus productos:*\n"]
    for r in rows:
        ts = r["last_checked"][:16].replace("T", " ") if r["last_checked"] else "nunca"
        lines.append(
            f"{_stock_icon(r['in_stock'])} *{r['name'] or 'Sin nombre'}*\n"
            f"  `ID {r['id']}` · última comprobación: {ts}\n"
            f"  {r['url']}\n"
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_products(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No tienes productos en seguimiento.")
        return

    keyboard = [
        [InlineKeyboardButton(
            f"❌  {(r['name'] or r['url'])[:45]}",
            callback_data=f"del:{r['id']}",
        )]
        for r in rows
    ]
    keyboard.append([InlineKeyboardButton("Cancelar", callback_data="del:cancel")])

    await update.message.reply_text(
        "¿Qué producto quieres eliminar?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cb_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "del:cancel":
        await query.edit_message_text("Cancelado.")
        return

    pid = int(query.data.split(":")[1])
    if remove_product(update.effective_chat.id, pid):
        await query.edit_message_text("Producto eliminado del seguimiento. ✓")
    else:
        await query.edit_message_text("Producto no encontrado (puede que ya estuviera eliminado).")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    creds = get_credentials(chat_id)
    if not creds:
        await update.message.reply_text(
            "No tienes credenciales guardadas. Usa /login primero."
        )
        return

    rows = get_products(chat_id)
    if not rows:
        await update.message.reply_text("No tienes productos en seguimiento.")
        return

    email, password = creds
    msg = await update.message.reply_text(f"🔍 Comprobando {len(rows)} producto(s)…")
    lines = []
    for r in rows:
        result = await check_stock(r["url"], r["selector"], email=email, password=password, chat_id=chat_id)
        if result.in_stock is not None:
            update_stock(r["id"], result.in_stock, result.name)
        name = result.name or r["name"] or "Producto"
        note = f" — _{result.error}_" if result.error else ""
        lines.append(f"{_stock_icon(result.in_stock)} *{name}*{note}")
        await asyncio.sleep(2)

    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    global _scheduled_check_running
    if _scheduled_check_running:
        logger.info("Skipping scheduled check — previous cycle still running")
        return

    _scheduled_check_running = True
    try:
        rows = get_all_products()
        logger.info("Scheduled check: %d product(s)", len(rows))

        for r in rows:
            creds = get_credentials(r["chat_id"])
            if not creds:
                logger.info("Skipping product %d — no credentials for chat %d", r["id"], r["chat_id"])
                continue

            email, password = creds
            try:
                result = await check_stock(
                    r["url"], r["selector"],
                    email=email, password=password, chat_id=r["chat_id"],
                )
            except Exception as exc:
                logger.error("Error checking product %d: %s", r["id"], exc)
                await asyncio.sleep(3)
                continue

            if result.in_stock is None:
                await asyncio.sleep(2)
                continue

            prev = r["in_stock"]
            update_stock(r["id"], result.in_stock, result.name)

            if result.in_stock and prev != 1:
                name = result.name or r["name"] or "Producto"
                try:
                    await context.bot.send_message(
                        chat_id=r["chat_id"],
                        text=(
                            f"🔔 *¡Disponible!*\n\n"
                            f"*{name}*\n\n"
                            f"🛒 [Ver producto]({r['url']})"
                        ),
                        parse_mode="Markdown",
                    )
                except Exception as exc:
                    logger.error("Failed to notify chat %d: %s", r["chat_id"], exc)

            await asyncio.sleep(2)
    finally:
        _scheduled_check_running = False


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", cmd_login)],
        states={
            WAITING_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_email)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(login_conv)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CallbackQueryHandler(cb_remove, pattern=r"^del:"))

    app.job_queue.run_repeating(scheduled_check, interval=CHECK_INTERVAL, first=60)
    logger.info("Bot iniciado. Comprobando stock cada %d minutos.", CHECK_INTERVAL // 60)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
