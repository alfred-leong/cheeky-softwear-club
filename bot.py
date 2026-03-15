import asyncio
import os
import io
import json
import base64
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from sqlalchemy import create_engine, text as sql_text
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

# Delay (seconds) to wait for all photos in a media group to arrive
MEDIA_GROUP_WAIT = 1.5
TIME_TO_CLEAR_DB = 12  # Time to clear the database (12 PM SGT)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# --- Example captions (edit this list to refine the style) ---
EXAMPLE_CAPTIONS_FILE = Path(__file__).parent / "captions.json"


def load_example_captions() -> list[str]:
    if EXAMPLE_CAPTIONS_FILE.exists():
        return json.loads(EXAMPLE_CAPTIONS_FILE.read_text())
    return []


def save_example_captions(captions: list[str]):
    EXAMPLE_CAPTIONS_FILE.write_text(json.dumps(captions, ensure_ascii=False, indent=2))


# Initialise with the examples we have so far
if not EXAMPLE_CAPTIONS_FILE.exists():
    save_example_captions(
        [
            "Literally dead….. One of my favs i've ever listed!!! 💋 Gorgeous white embroidered floral jeans… SOOO baggy & flared!! Perfect mid-waisted length🤍🤍🤍 Such an it girl pair of jeans… might disappear 😝😝\n34.5cm waist max, 44cm hips, 25cm rise, 27cm thigh, 97cm\nlength, 27cm flare\n\nSB: $3",
            "This one's from glacier ⭐️⭐️⭐️ Gorgeous gorgeous details & touch of green paisleys on this… i luvvv 🧚‍♂️🧚\nFits XS-M; 37-57cm ptp, 57cm length\n\nSB: $3",
            "Cato pink striped halter ☀️💖Such a summery top i love!!! Can be worn as a tube too 💞💞\nFits XS-M, 37-47cm ptp, 42cm from pit\n\nSB: $3",
            "Such a flattering lace cami in the prettiest purple-grey colour! 💭💭 Love the horizontal lace detail\nat the bust ⭐️⭐️ Goes sooo well with your fav pair of jeans/shorts 🫶🫶\nFits XS-M; 38-48cm ptp, 57cm length\n\nSB: $3",
            "Camel Road low-waisted flared jeans 🦋🦋 Love the pop of white on the brand patch & the subtle navy embroidery at the back pockets 🩵 This one's for my lowkey girlieesss🫶🫶\n36-39cm across, 44cm hips max, 20cm rise, 26-30cm thigh, 100cm length\n\nSB: $3",
            "Gorgeous purple flowy chiffon cami 🫧🫧 Fits sooo pretty, i love the asymmetrical flowy bottom🤍🤍 Comes with a concealed side zip, bust is double lined💋💋\nFits XS-S, 37-40cm ptp, 66cm length\n\nSB: $3",
            "literally the most gorgeous yellow floral cami evaaaa 🌼🌼⭐️ So so pretty, i'm obsessed with the yellow florals & lace straps! Bust is double-lined😋\nFits XS-M, 35-50cm ptp, 64cm length\n\nSB: $3",
        ]
    )

# --- Gemini client ---
gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
GEMINI_MODEL = "gemini-2.5-flash"

# --- Database (for delete tracker) ---
DB_URL = os.getenv("DB_URL")
engine = create_engine(DB_URL) if DB_URL else None

# --- Conversation states ---
WAITING_PHOTOS, WAITING_DESCRIPTION = range(2)


def build_prompt(description: str, example_captions: list[str]) -> str:
    examples_block = "\n".join(
        f"  {i+1}) {cap}" for i, cap in enumerate(example_captions)
    )
    return f"""You are a caption writer for an online clothing store on social media.

The seller has sent you photos of a clothing item along with this description:
"{description}"

Here are example captions written by the seller — match this exact tone, energy, and emoji style:
{examples_block}

Rules:
- Write ONE caption for this clothing item.
- Match the seller's casual, enthusiastic, emoji-heavy style shown in the examples.
- Mention the brand / item name if given in the description.
- Keep it short (1-3 sentences max).
- Use relevant emojis liberally, matching the vibe of the examples.
- Do NOT add hashtags unless the examples use them.
- Output ONLY the caption text, nothing else."""


# =====================================================================
# Caption bot handlers (private chats)
# =====================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type

    if chat_type in ("group", "supergroup"):
        # Delete tracker: register chat in DB
        if engine is not None:
            with engine.begin() as conn:
                conn.execute(sql_text("""
                    INSERT INTO usernames (username, chat_id)
                    VALUES (:username, :chat_id)
                    ON CONFLICT (username) DO NOTHING
                """), {
                    "username": update.effective_chat.username,
                    "chat_id": update.effective_chat.id,
                })
        name = update.effective_chat.username or update.effective_chat.title
        await update.message.reply_text(
            f"Hi {name}, I will now store messages in the database. "
            f"Use /deleted in the discussion group to see deleted comments."
        )
    else:
        # Caption bot: show help
        await update.message.reply_text(
            "Hey! 👋 Send me photos of your clothing item (2-4 pics), "
            "then I'll ask for a quick description and generate a caption for you.\n\n"
            "Commands:\n"
            "/start - Start over\n"
            "/addcaption <text> - Add an example caption\n"
            "/listcaptions - List saved example captions\n"
            "/cancel - Cancel current item"
        )
        context.user_data.clear()

    return ConversationHandler.END


async def _finalize_media_group(context: ContextTypes.DEFAULT_TYPE, chat_id: int, media_group_id: str):
    """Called after a delay to send a single reply for the entire media group."""
    await asyncio.sleep(MEDIA_GROUP_WAIT)
    user_data = context.application.user_data.get(chat_id, {})
    pending = user_data.get("pending_media_groups", {})
    count = pending.pop(media_group_id, 0)
    total = len(user_data.get("photos", []))
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📸 Got {total} photo{'s' if total != 1 else ''}! "
        f"Send more photos, or type a description of the item "
        f"(e.g. 'white Nike hoodie, size M') to generate a caption.",
    )


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect photos. Batches media groups so only one reply is sent per album."""
    if "photos" not in context.user_data:
        context.user_data["photos"] = []
    if "pending_media_groups" not in context.user_data:
        context.user_data["pending_media_groups"] = {}

    # Get the highest resolution version of the photo
    photo = update.message.photo[-1]
    file = await photo.get_file()
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    photo_bytes = buf.read()
    context.user_data["photos"].append(photo_bytes)

    media_group_id = update.message.media_group_id

    if media_group_id:
        # Part of an album — only schedule one reply per group
        pending = context.user_data["pending_media_groups"]
        pending[media_group_id] = pending.get(media_group_id, 0) + 1
        if pending[media_group_id] == 1:
            # First photo in this group — schedule a delayed reply
            asyncio.create_task(
                _finalize_media_group(context, update.effective_chat.id, media_group_id)
            )
    else:
        # Single photo (not an album)
        count = len(context.user_data["photos"])
        await update.message.reply_text(
            f"📸 Got {count} photo{'s' if count != 1 else ''}! "
            f"Send more photos, or type a description of the item "
            f"(e.g. 'white Nike hoodie, size M') to generate a caption."
        )

    return WAITING_DESCRIPTION


async def description_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User sent a text description — generate the caption."""
    photos = context.user_data.get("photos", [])
    if not photos:
        await update.message.reply_text(
            "Please send at least one photo first! 📷"
        )
        return WAITING_PHOTOS

    description = update.message.text
    await update.message.reply_text("✨ Generating your caption...")

    example_captions = load_example_captions()
    prompt = build_prompt(description, example_captions)

    # Build Gemini content parts: photos + text prompt
    parts = []
    for photo_bytes in photos:
        b64 = base64.b64encode(photo_bytes).decode()
        parts.append(types.Part.from_bytes(data=photo_bytes, mime_type="image/jpeg"))
    parts.append(types.Part.from_text(text=prompt))

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Content(parts=parts, role="user")],
        )
        caption = response.text.strip()
        await update.message.reply_text(f"📝 Here's your caption:\n\n{caption}")
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        await update.message.reply_text(
            f"Sorry, something went wrong generating the caption. Error: {e}"
        )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled! Send /start to begin again.")
    return ConversationHandler.END


async def add_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace("/addcaption", "", 1).strip()
    if not text:
        await update.message.reply_text("Usage: /addcaption <your example caption>")
        return
    captions = load_example_captions()
    captions.append(text)
    save_example_captions(captions)
    await update.message.reply_text(
        f"✅ Added! You now have {len(captions)} example captions."
    )


async def list_captions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    captions = load_example_captions()
    if not captions:
        await update.message.reply_text("No example captions saved yet.")
        return
    lines = [f"{i+1}) {c}" for i, c in enumerate(captions)]
    await update.message.reply_text("📋 Example captions:\n\n" + "\n\n".join(lines))


# =====================================================================
# Delete tracker handlers (group chats)
# =====================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store reply messages in group chats for delete tracking."""
    if engine is None:
        return
    if not (update.message and update.message.reply_to_message):
        return

    message_id = update.message.message_id
    username = update.message.from_user.username or update.message.from_user.full_name
    text_msg = update.message.text or ""
    now = datetime.now()
    message_thread_id = update.message.message_thread_id
    message_chat_id = update.message.chat.id

    with engine.begin() as conn:
        result = conn.execute(sql_text("""
            INSERT INTO messages (id, message_id, username, text, timestamp, message_thread_id, chat_id)
            VALUES (gen_random_uuid(), :msg_id, :username, :text, :ts, :message_thread_id, :chat_id)
        """), {
            "msg_id": message_id,
            "username": username,
            "text": text_msg,
            "ts": now,
            "message_thread_id": message_thread_id,
            "chat_id": message_chat_id,
        })

        if result.rowcount == 1:
            logger.info("Insert into messages successful.")
        else:
            logger.warning("Insert into messages may have failed.")


async def handle_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store photo captions in group chats for delete tracking."""
    if engine is None:
        return
    if not update.message:
        return

    caption = update.message.caption
    if not caption:
        return

    message_id = update.message.message_id
    message_chat_id = update.message.chat.id

    with engine.begin() as conn:
        result = conn.execute(sql_text("""
            INSERT INTO items (message_id, caption, chat_id)
            VALUES (:msg_id, :caption, :chat_id)
        """), {
            "msg_id": message_id,
            "caption": caption,
            "chat_id": message_chat_id,
        })

        if result.rowcount == 1:
            logger.info("Insert into items successful.")
        else:
            logger.warning("Insert into items may have failed.")


async def show_deleted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check which stored messages have been deleted and report them."""
    if engine is None:
        await update.message.reply_text("Database not configured.")
        return
    if not (update.message and update.message.from_user):
        return

    chat_id = update.effective_chat.id
    deleted = []

    with engine.connect() as conn:
        messages = conn.execute(sql_text("""
            SELECT message_id, username, text, message_thread_id
            FROM messages
            WHERE chat_id = :chat_id
        """), {"chat_id": chat_id}).fetchall()

        for msg in messages:
            try:
                forwarded_msg = await context.bot.forward_message(
                    chat_id=chat_id, from_chat_id=chat_id, message_id=msg.message_id
                )
                await context.bot.delete_message(
                    chat_id=chat_id, message_id=forwarded_msg.message_id
                )
            except Exception:
                with engine.connect() as inner_conn:
                    item_row = inner_conn.execute(sql_text("""
                        SELECT caption FROM items
                        WHERE message_id = :message_thread_id
                        AND chat_id = :chat_id
                    """), {
                        "message_thread_id": msg.message_thread_id,
                        "chat_id": chat_id,
                    }).fetchone()

                item_caption = item_row.caption if item_row else "(unknown item)"
                deleted.append(f"@{msg.username}: {msg.text}\nItem: {item_caption}\n\n")

    if not deleted:
        message = f"No deleted messages detected in '{update.effective_chat.title or chat_id}'."
    else:
        chat_label = update.effective_chat.title or f"Chat ID: {chat_id}"
        message = f"Deleted messages from {chat_label}:\n\n" + "\n".join(deleted)

    await context.bot.send_message(chat_id=chat_id, text=message)


async def clear_db():
    """Daily cleanup of messages and items tables."""
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM messages"))
        conn.execute(sql_text("DELETE FROM items"))
    now = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"Database cleared successfully at {TIME_TO_CLEAR_DB}PM on {now}")


# =====================================================================
# Application setup
# =====================================================================

async def post_init(application: Application):
    """Start the scheduler after the application initialises."""
    if engine is not None:
        scheduler = AsyncIOScheduler(timezone=timezone('Asia/Singapore'))
        scheduler.add_job(clear_db, trigger='cron', hour=TIME_TO_CLEAR_DB, minute=0)
        scheduler.start()
        logger.info("Scheduler started - DB cleanup at 12 PM SGT daily")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Health server listening on port {port}")


def main():
    start_health_server()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addcaption", add_caption))
    app.add_handler(CommandHandler("listcaptions", list_captions))
    app.add_handler(CommandHandler("deleted", show_deleted))

    # Caption generation conversation (private chats only)
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_received),
        ],
        states={
            WAITING_PHOTOS: [
                MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_received),
            ],
            WAITING_DESCRIPTION: [
                MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, description_received),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )
    app.add_handler(conv_handler)

    # Delete tracker (group chats only)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.GROUPS, handle_item))

    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
