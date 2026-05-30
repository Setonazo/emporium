import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from database import (
    add_product,
    get_all_products,
    get_products,
    init_db,
    remove_product,
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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "15")) * 60

# Prevents overlapping scheduled check cycles
_scheduled_check_running = False


def _stock_icon(val) -> str:
    if val == 1:
        return "✅"
    if val == 0:
        return "❌"
    return "❓"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Emporium Stock Bot*\n\n"
        "Te aviso cuando un producto de Dufry Emporium vuelva a tener stock.\n\n"
        "*Comandos:*\n"
        "• `/add <url>` — Añadir producto al seguimiento\n"
        "• `/list` — Ver tus productos\n"
        "• `/remove` — Eliminar un producto\n"
        "• `/check` — Comprobar stock ahora mismo\n",
        parse_mode="Markdown",
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Uso: `/add <url>`\n\n"
            "Ejemplo:\n"
            "`/add <https://esfnf.emporium.dufry.com/es/product/whisky-johnnie-walker`>",
            parse_mode="Markdown",
        )
        return

    url = context.args[0].strip()
    if not url.startswith("http"):
        await update.message.reply_text(
            "La URL debe comenzar por `http://` o `https://`", parse_mode="Markdown"
        )
        return

    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🔍 Comprobando el producto…")

    result = await check_stock(url)
    name = result.name or url.rstrip("/").split("/")[-1]

    try:
        pid = add_product(chat_id, url, name)
    except ValueError as exc:
        await msg.edit_text(str(exc))
        return

    if result.in_stock is not None:
        update_stock(pid, result.in_stock, result.name)

    if result.in_stock is True:
        status_line = "✅ *¡Ya está en stock!* Puedes comprarlo ahora."
    elif result.in_stock is False:
        status_line = "❌ Sin stock. Te avisaré en cuanto esté disponible."
    else:
        note = f"\n_{result.error}_" if result.error else ""
        status_line = f"❓ Estado desconocido — seguiré comprobando.{note}"

    await msg.edit_text(
        f"*{name}*\n\n{status_line}",
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
    rows = get_products(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No tienes productos en seguimiento.")
        return

    msg = await update.message.reply_text(f"🔍 Comprobando {len(rows)} producto(s)…")
    lines = []
    for r in rows:
        result = await check_stock(r["url"], r["selector"])
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
            try:
                result = await check_stock(r["url"], r["selector"])
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

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
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
