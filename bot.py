import json
import asyncio
import os
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]   # used only for first-time init
ADMIN_ID   = int(os.environ["ADMIN_ID"])

# POSTING SCHEDULE (24h format, local server time)
POST_HOUR_START = 4   # 4 AM
POST_HOUR_END   = 22  # 10 PM

# INTERVAL BETWEEN POSTS (seconds) — changeable via /setinterval
POST_INTERVAL = 1200  # 20 minutes

# RUNTIME STATE
paused           = False
start_time       = datetime.now(timezone.utc)
posts_sent       = 0
last_posted      = None
last_type_posted = None  # tracks "mcq" or "news" to enforce alternating order
_resume_event: asyncio.Event | None = None  # wakes the auto-post loop instantly on /resume


# ─────────────────────────────────────────
# FILE HELPERS
# ─────────────────────────────────────────

def load_json(filename):
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────
# CHANNEL HELPERS
# ─────────────────────────────────────────

def load_channels():
    return load_json("channels.json")


def save_channels(channels):
    save_json("channels.json", channels)


def get_active_channels():
    return [ch["id"] for ch in load_channels() if ch.get("active", True)]


# ─────────────────────────────────────────
# FORMAT HELPERS
# ─────────────────────────────────────────

def format_news(item, label="📰 TODAY NEWS"):
    return f"*{label}*\n\n" + item["content"]


def format_mcq(item, label="📰 TODAY MCQ"):
    options_text = "\n".join(item["options"])
    return (
        f"*{label}*\n\n"
        f"❓ *QUESTION*\n\n"
        f"{item['question']}\n\n"
        f"{options_text}\n\n"
        f"✅ *Answer:* {item['answer']}\n\n"
        f"📝 {item['explanation']}"
    )


def detect_type(item):
    if "question" in item:
        return "mcq"
    return "news"


def format_post(item, label=None):
    is_mcq = item.get("type") == "mcq"

    # DEFAULT LABELS
    if label is None:
        label = "📰 TODAY MCQ" if is_mcq else "📰 TODAY NEWS"

    if is_mcq:
        return format_mcq(item, label)
    return format_news(item, label)


# ─────────────────────────────────────────
# ADMIN CHECK
# ─────────────────────────────────────────

def is_admin(update: Update) -> bool:
    if update.effective_user is None:
        return False
    return update.effective_user.id == ADMIN_ID


async def deny(update: Update):
    await update.message.reply_text("⛔ You are not authorized to use this command.")


# ─────────────────────────────────────────
# ALTERNATING ORDER HELPER
# ─────────────────────────────────────────

def pick_alternating(items):
    """Return the index of the next item to post, alternating MCQ → news → MCQ → ...
    Falls back to whatever type is available if the desired type is not in the list."""
    if not items:
        return None

    if last_type_posted == "mcq":
        desired = "news"
    elif last_type_posted == "news":
        desired = "mcq"
    else:
        return 0  # no preference yet, take whatever is first

    for i, item in enumerate(items):
        if item.get("type") == desired:
            return i

    # Desired type not found — only one type available, post it and log
    available_types = set(item.get("type") for item in items)
    print(f"⚠️ No '{desired}' found in list. Only {available_types} available. Posting next available item.")
    return 0


# ─────────────────────────────────────────
# SCHEDULE CHECK
# ─────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))

def within_posting_hours() -> bool:
    hour = datetime.now(IST).hour
    return POST_HOUR_START <= hour < POST_HOUR_END


# ─────────────────────────────────────────
# SHARED: PROCESS & ADD ITEMS TO QUEUE
# ─────────────────────────────────────────

async def process_new_items(update: Update, context: ContextTypes.DEFAULT_TYPE, new_items: list):

    queue   = load_json("queue.json")
    archive = load_json("archive.json")
    added   = []

    for item in new_items:

        # SKIP DUPLICATES (check content or question)
        is_duplicate = any(
            existing.get("content") == item.get("content") and
            existing.get("question") == item.get("question")
            for existing in archive
        )

        if is_duplicate:
            print("⚠️ Duplicate skipped")
            continue

        item["type"]           = detect_type(item)
        item["id"]             = f"POST_{len(archive) + len(added) + 1}"
        item["revision_count"] = 0
        added.append(item)

    queue_was_empty = len(queue) == 0

    queue.extend(added)
    archive.extend(added)

    save_json("queue.json", queue)
    save_json("archive.json", archive)

    # PREVIEW FIRST ITEM BEFORE ADDING
    if added:
        preview_text = (
            f"👁 *Preview of first item ({added[0]['type'].upper()}):*\n\n"
            + format_post(added[0])
        )
        await update.message.reply_text(preview_text, parse_mode="Markdown")

    # INSTANT FIRST POST IF QUEUE WAS EMPTY AND IN HOURS
    if queue_was_empty and added and within_posting_hours() and not paused:
        idx = pick_alternating(queue)
        first = queue.pop(idx)
        await send_item(context.bot, first)
        save_json("queue.json", queue)
        global posts_sent, last_posted, last_type_posted
        posts_sent       += 1
        last_posted       = first["id"]
        last_type_posted  = first.get("type", "news")
        print(f"✅ Instantly posted: {first['id']}")

    await update.message.reply_text(
        f"✅ *{len(added)} new posts added to queue!*\n"
        f"⏭ Duplicates skipped: {len(new_items) - len(added)}",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────
# FILE UPLOAD HANDLER (.json file)
# ─────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    document = update.message.document
    if not document:
        return

    # DOWNLOAD AND READ FILE
    file = await context.bot.get_file(document.file_id)
    file_name = document.file_name
    await file.download_to_drive(file_name)

    try:
        new_items = load_json(file_name)
    except Exception:
        await update.message.reply_text("❌ Could not read file. Make sure it is valid JSON.")
        return

    if not isinstance(new_items, list):
        await update.message.reply_text("❌ JSON must be a list `[...]` of items.")
        return

    await process_new_items(update, context, new_items)


# ─────────────────────────────────────────
# TEXT MESSAGE HANDLER (pasted JSON text)
# ─────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        return  # silently ignore non-admin text

    text = update.message.text.strip()

    # ONLY HANDLE IF IT LOOKS LIKE JSON
    if not (text.startswith("[") or text.startswith("{")):
        return

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        await update.message.reply_text("❌ Invalid JSON. Please check the format and try again.")
        return

    # WRAP SINGLE OBJECT IN A LIST
    if isinstance(data, dict):
        data = [data]

    if not isinstance(data, list):
        await update.message.reply_text("❌ JSON must be a list `[...]` of items.")
        return

    await process_new_items(update, context, data)


# ─────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = (
        "👋 *Welcome to AffairsMCQ Bot!*\n\n"
        "I auto-post news and MCQs to your Telegram channel every 20 minutes.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "📥 *HOW TO ADD CONTENT*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Send a `.json` file *or* paste JSON text directly here.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🎮 *POSTING CONTROLS*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📊 /status — Queue size, uptime, last post\n"
        "⏸ /pause — Stop auto-posting\n"
        "▶️ /resume — Resume auto-posting\n"
        "⚡ /next — Force post next item now\n"
        "⏭ /skip — Skip next item in queue\n"
        "📋 /queue — List all pending posts\n"
        "🗑 /clear — Clear entire queue (asks confirm)\n"
        "🕒 /setinterval 30 — Change posting interval\n"
        "📈 /revision — Top revised posts stats\n"
        "📊 /poll — Post next MCQ as quiz poll\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "📡 *CHANNEL MANAGEMENT*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 /channels — List all channels & status\n"
        "➕ /addchannel @name — Add a new channel\n"
        "🔁 /togglechannel @name — Pause/resume a channel\n"
        "🗑 /removechannel @name — Remove a channel\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "⏰ *POSTING HOURS*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Posts go out between *4:00 AM – 10:00 PM* only.\n"
        "Outside these hours the bot waits automatically.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "♻️ *REVISION MODE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "When queue is empty, old posts are reposted automatically.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🗃 *ARCHIVE MANAGEMENT*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📂 /archive [page] — Browse all archived posts\n"
        "👁 /viewpost POST\\_ID — View full content of a post\n"
        "🗑 /deletepost POST\\_ID — Delete a post from archive\n"
        "🔼 /requeue POST\\_ID — Move a post to front of queue"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = (
        "🆘 *COMMANDS*\n\n"
        "📊 /status — Queue, archive, uptime, interval\n"
        "⏸ /pause — Stop auto-posting\n"
        "▶️ /resume — Resume auto-posting\n"
        "⚡ /next — Force post next item now\n"
        "⏭ /skip — Skip next item in queue\n"
        "📋 /queue — List all pending posts\n"
        "🗑 /clear — Clear entire queue\n"
        "🕒 /setinterval 30 — Change interval\n"
        "📈 /revision — Top revised posts\n"
        "📊 /poll — Post next MCQ as quiz poll\n\n"
        "📡 /channels — List channels & status\n"
        "➕ /addchannel @name — Add channel\n"
        "🔁 /togglechannel @name — Pause/resume channel\n"
        "🗑 /removechannel @name — Remove channel\n\n"
        "📂 /archive [page] — Browse archived posts\n"
        "👁 /viewpost POST\\_ID — View a post\n"
        "🗑 /deletepost POST\\_ID — Delete a post\n"
        "🔼 /requeue POST\\_ID — Re-queue a post\n\n"
        "🆘 /help — Show this list"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    queue   = load_json("queue.json")
    archive = load_json("archive.json")

    uptime_seconds = (datetime.now(timezone.utc) - start_time).seconds
    hours, rem     = divmod(uptime_seconds, 3600)
    minutes, _     = divmod(rem, 60)

    status_text = (
        f"📊 *BOT STATUS*\n\n"
        f"▶️ State: {'⏸ Paused' if paused else '✅ Running'}\n"
        f"📰 Queue: {len(queue)} posts\n"
        f"📦 Archive: {len(archive)} posts\n"
        f"✅ Posts sent this session: {posts_sent}\n"
        f"🕐 Last posted: {last_posted or 'None'}\n"
        f"⏱ Uptime: {hours}h {minutes}m\n"
        f"⏰ Posting hours: {POST_HOUR_START}:00 AM – {POST_HOUR_END}:00 PM\n"
        f"🕒 Interval: {POST_INTERVAL // 60} minutes\n"
        f"🟢 Within hours: {'Yes' if within_posting_hours() else 'No'}"
    )

    await update.message.reply_text(status_text, parse_mode="Markdown")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    global paused
    paused = True
    await update.message.reply_text("⏸ Bot paused. Posts will not be sent until resumed.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    global paused, _resume_event
    paused = False
    if _resume_event:
        _resume_event.set()  # wake the auto-post loop immediately, no waiting
    await update.message.reply_text("▶️ Bot resumed. Posting will continue.")


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    queue = load_json("queue.json")

    if not queue:
        await update.message.reply_text("❌ Queue is empty, nothing to post.")
        return

    idx = pick_alternating(queue)
    if idx is None:
        await update.message.reply_text("❌ Queue is empty, nothing to post.")
        return
    post = queue.pop(idx)
    await send_item(context.bot, post)
    save_json("queue.json", queue)

    global posts_sent, last_posted, last_type_posted
    posts_sent       += 1
    last_posted       = post["id"]
    last_type_posted  = post.get("type", "news")

    await update.message.reply_text(f"✅ Force posted: *{post['id']}* ({post.get('type', 'news').upper()})", parse_mode="Markdown")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    queue = load_json("queue.json")

    if not queue:
        await update.message.reply_text("❌ Queue is empty, nothing to skip.")
        return

    skipped = queue.pop(0)
    save_json("queue.json", queue)
    await update.message.reply_text(f"⏭ Skipped: *{skipped['id']}*", parse_mode="Markdown")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    queue = load_json("queue.json")

    if not queue:
        await update.message.reply_text("📭 Queue is empty.")
        return

    lines = [f"📋 *Queue ({len(queue)} items):*\n"]
    for i, item in enumerate(queue[:20], 1):
        label = item.get("question", item.get("content", ""))[:60]
        lines.append(f"{i}. [{item['type'].upper()}] {label}...")

    if len(queue) > 20:
        lines.append(f"\n...and {len(queue) - 20} more.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    # STORE PENDING CONFIRMATION
    context.user_data["awaiting_clear_confirm"] = True
    await update.message.reply_text(
        "⚠️ Are you sure you want to clear the entire queue?\n\nReply /confirmclear to confirm or /cancel to abort."
    )


async def cmd_confirmclear(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    if not context.user_data.get("awaiting_clear_confirm"):
        await update.message.reply_text("Nothing to confirm.")
        return

    queue = load_json("queue.json")
    count = len(queue)
    save_json("queue.json", [])
    context.user_data["awaiting_clear_confirm"] = False
    await update.message.reply_text(f"🗑 Queue cleared! {count} posts removed.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_clear_confirm"] = False
    await update.message.reply_text("✅ Cancelled.")


async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    if not context.args:
        await update.message.reply_text("Usage: /setinterval <minutes>\nExample: /setinterval 30")
        return

    try:
        minutes = int(context.args[0])
        if minutes < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number of minutes (minimum 1).")
        return

    global POST_INTERVAL
    POST_INTERVAL = minutes * 60
    await update.message.reply_text(f"✅ Posting interval set to *{minutes} minutes*.", parse_mode="Markdown")


async def cmd_revision(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    archive = load_json("archive.json")

    if not archive:
        await update.message.reply_text("📭 Archive is empty.")
        return

    sorted_archive = sorted(archive, key=lambda x: x.get("revision_count", 0), reverse=True)
    top = sorted_archive[:10]

    lines = ["📈 *Top Revised Posts:*\n"]
    for i, item in enumerate(top, 1):
        label = item.get("question", item.get("content", ""))[:50]
        lines.append(f"{i}. [{item['id']}] revised {item['revision_count']}x — {label}...")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────
# CHANNEL MANAGEMENT COMMANDS
# ─────────────────────────────────────────

async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    channels = load_channels()

    if not channels:
        await update.message.reply_text("📭 No channels configured.")
        return

    lines = ["📡 *Channels:*\n"]
    for ch in channels:
        status = "✅ Active" if ch.get("active", True) else "⏸ Paused"
        lines.append(f"{status} — `{ch['id']}`")

    lines.append("\nUse /addchannel @username to add")
    lines.append("Use /togglechannel @username to pause/resume")
    lines.append("Use /removechannel @username to remove")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    if not context.args:
        await update.message.reply_text("Usage: /addchannel @channelname")
        return

    channel_id = context.args[0]
    if not channel_id.startswith("@") and not channel_id.startswith("-"):
        await update.message.reply_text("❌ Channel must start with @ or be a numeric ID.")
        return

    channels = load_channels()

    if any(ch["id"] == channel_id for ch in channels):
        await update.message.reply_text(f"⚠️ `{channel_id}` is already in the list.", parse_mode="Markdown")
        return

    channels.append({"id": channel_id, "active": True})
    save_channels(channels)
    await update.message.reply_text(f"✅ Added `{channel_id}` — posting is *active*.", parse_mode="Markdown")


async def cmd_togglechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    if not context.args:
        await update.message.reply_text("Usage: /togglechannel @channelname")
        return

    channel_id = context.args[0]
    channels = load_channels()

    for ch in channels:
        if ch["id"] == channel_id:
            ch["active"] = not ch.get("active", True)
            save_channels(channels)
            status = "✅ Active" if ch["active"] else "⏸ Paused"
            await update.message.reply_text(
                f"{status} — `{channel_id}` posting is now *{'on' if ch['active'] else 'off'}*.",
                parse_mode="Markdown"
            )
            return

    await update.message.reply_text(f"❌ Channel `{channel_id}` not found. Use /channels to see the list.", parse_mode="Markdown")


async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    if not context.args:
        await update.message.reply_text("Usage: /removechannel @channelname")
        return

    channel_id = context.args[0]
    channels = load_channels()

    new_channels = [ch for ch in channels if ch["id"] != channel_id]

    if len(new_channels) == len(channels):
        await update.message.reply_text(f"❌ Channel `{channel_id}` not found.", parse_mode="Markdown")
        return

    save_channels(new_channels)
    await update.message.reply_text(f"🗑 Removed `{channel_id}` from the list.", parse_mode="Markdown")


# ─────────────────────────────────────────
# DAILY REPORT
# ─────────────────────────────────────────

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):

    queue   = load_json("queue.json")
    archive = load_json("archive.json")

    report = (
        f"📅 *DAILY REPORT*\n\n"
        f"✅ Posts sent today: {posts_sent}\n"
        f"📰 Queue remaining: {len(queue)}\n"
        f"📦 Archive total: {len(archive)}\n"
        f"⏰ Posting hours: {POST_HOUR_START}:00 AM – {POST_HOUR_END}:00 PM\n"
        f"🕒 Interval: {POST_INTERVAL // 60} minutes"
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=report,
        parse_mode="Markdown"
    )
    print("📅 Daily report sent to admin")


# ─────────────────────────────────────────
# AUTO POST LOOP
# ─────────────────────────────────────────

async def interruptible_sleep(seconds: float):
    """Sleep for `seconds` in 2-second steps, returning early if _resume_event is set.
    Uses simple polling — no asyncio.wait_for, so it never raises CancelledError."""
    elapsed = 0.0
    while elapsed < seconds:
        if _resume_event and _resume_event.is_set():
            _resume_event.clear()
            return
        await asyncio.sleep(min(2.0, seconds - elapsed))
        elapsed += 2.0


async def auto_post(app):

    global posts_sent, last_posted, last_type_posted, POST_INTERVAL, _resume_event
    _resume_event = asyncio.Event()

    while True:

        try:

            # RESPECT POSTING HOURS
            if not within_posting_hours():
                print(f"🌙 Outside posting hours ({POST_HOUR_START}:00–{POST_HOUR_END}:00), sleeping 60s")
                await asyncio.sleep(60)
                continue

            # RESPECT PAUSE
            if paused:
                print("⏸ Bot is paused, sleeping 60s")
                await asyncio.sleep(60)
                continue

            queue = load_json("queue.json")
            print(f"📰 Queue size: {len(queue)}")

            if queue:

                # POST NEXT ITEM FROM QUEUE (alternating MCQ/news)
                idx  = pick_alternating(queue)
                if idx is None:
                    await interruptible_sleep(POST_INTERVAL)
                    continue
                post = queue.pop(idx)
                await send_item(app.bot, post)
                print(f"✅ Posted: {post['id']} ({post.get('type', 'news')})")
                save_json("queue.json", queue)
                posts_sent       += 1
                last_posted       = post["id"]
                last_type_posted  = post.get("type", "news")

            else:

                # REVISION MODE (alternating MCQ/news from archive)
                print("♻️ Queue empty, starting revision mode")
                archive = load_json("archive.json")
                print(f"📦 Archive size: {len(archive)}")

                if archive:

                    idx = pick_alternating(archive)
                    if idx is None:
                        await interruptible_sleep(POST_INTERVAL)
                        continue
                    old_post = archive.pop(idx)
                    old_post["revision_count"] += 1
                    archive.append(old_post)
                    save_json("archive.json", archive)

                    revision_label = "♻️ REVISION MCQ" if old_post.get("type") == "mcq" else "♻️ REVISION NEWS"

                    await send_item(app.bot, old_post, label=revision_label)
                    print(f"♻️ Posted revision: {old_post['id']} ({old_post.get('type', 'news')})")
                    posts_sent       += 1
                    last_posted       = old_post["id"]
                    last_type_posted  = old_post.get("type", "news")

                else:
                    print("⚠️ Archive is empty")

            # WAIT FOR NEXT INTERVAL (interruptible — wakes instantly on /resume)
            await interruptible_sleep(POST_INTERVAL)

        except Exception as e:
            print(f"❌ AUTO POST ERROR: {e}")
            try:
                await app.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"❌ *Bot Error:*\n`{e}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            await asyncio.sleep(30)


# ─────────────────────────────────────────
# SEND HELPER — auto picks poll or message
# ─────────────────────────────────────────

async def send_item(bot, item, label=None):
    from telegram.error import Forbidden

    active_channels = get_active_channels()

    if not active_channels:
        print("⚠️ No active channels to post to.")
        return

    for channel in active_channels:
        try:
            if item.get("type") == "mcq":
                # MCQ → always send as Telegram quiz poll
                options = item["options"]
                correct_index = next(
                    (i for i, o in enumerate(options) if o.startswith(item["answer"][0])),
                    0
                )
                clean_options = [o[3:].strip() if len(o) > 3 else o for o in options]
                await bot.send_poll(
                    chat_id=channel,
                    question=item["question"][:300],
                    options=clean_options,
                    type="quiz",
                    correct_option_id=correct_index,
                    explanation=item.get("explanation", "")[:200],
                    is_anonymous=True
                )
            else:
                # NEWS → send as text message
                await bot.send_message(
                    chat_id=channel,
                    text=format_post(item, label=label),
                    parse_mode="Markdown"
                )

        except Forbidden:
            # Bot was kicked — auto-disable this channel so it stops retrying
            print(f"🚫 Bot was kicked from {channel}. Auto-disabling it.")
            channels = load_channels()
            for ch in channels:
                if ch["id"] == channel:
                    ch["active"] = False
                    break
            save_channels(channels)
            try:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🚫 *Bot was kicked from* `{channel}`\nIt has been auto-disabled. Use /addchannel or /togglechannel to re-enable it once the bot is re-added.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        except Exception as e:
            print(f"❌ Failed to post to {channel}: {e}")


async def cmd_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    queue = load_json("queue.json")

    # FIND THE NEXT MCQ IN QUEUE
    mcq_index = next(
        (i for i, item in enumerate(queue) if item.get("type") == "mcq"),
        None
    )

    if mcq_index is None:
        await update.message.reply_text("❌ No MCQ found in the queue.")
        return

    # REMOVE FROM QUEUE AND POST AS POLL
    item = queue.pop(mcq_index)
    await send_item(context.bot, item)
    save_json("queue.json", queue)

    global posts_sent, last_posted
    posts_sent  += 1
    last_posted  = item["id"]

    await update.message.reply_text(
        f"✅ Posted MCQ *{item['id']}* as a quiz poll!",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────
# ARCHIVE MANAGEMENT COMMANDS
# ─────────────────────────────────────────

ARCHIVE_PAGE_SIZE = 10

async def cmd_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    archive = load_json("archive.json")

    if not archive:
        await update.message.reply_text("📭 Archive is empty.")
        return

    # PARSE PAGE NUMBER
    try:
        page = int(context.args[0]) if context.args else 1
        if page < 1:
            page = 1
    except ValueError:
        page = 1

    total_pages = (len(archive) + ARCHIVE_PAGE_SIZE - 1) // ARCHIVE_PAGE_SIZE
    page = min(page, total_pages)
    start = (page - 1) * ARCHIVE_PAGE_SIZE
    items = archive[start: start + ARCHIVE_PAGE_SIZE]

    lines = [f"🗃 Archive — Page {page}/{total_pages} ({len(archive)} total)\n"]
    for item in items:
        tag   = "📊 MCQ" if item.get("type") == "mcq" else "📰 News"
        # use `or` chain so None values don't cause TypeError
        label = (item.get("question") or item.get("content") or "")[:55]
        lines.append(f"{item['id']} | {tag}\n   {label}...")

    if page < total_pages:
        lines.append(f"\n➡️ Next: /archive {page + 1}")
    lines.append("👁 Use /viewpost POST_ID to see full content")

    # NO parse_mode — user content can break Markdown
    await update.message.reply_text("\n".join(lines))


async def cmd_viewpost(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    if not context.args:
        await update.message.reply_text("Usage: /viewpost POST_ID\nExample: /viewpost POST_5")
        return

    post_id = context.args[0].upper()
    archive = load_json("archive.json")

    item = next((i for i in archive if i.get("id", "").upper() == post_id), None)

    if not item:
        await update.message.reply_text(f"❌ Post `{post_id}` not found in archive.", parse_mode="Markdown")
        return

    header = (
        f"👁 Post: {item['id']}\n"
        f"📌 Type: {item.get('type', 'unknown').upper()}\n"
        f"♻️ Revised: {item.get('revision_count', 0)}x\n"
        f"{'─' * 30}\n"
    )

    if item.get("type") == "mcq":
        options_text = "\n".join(item.get("options", []))
        body = (
            f"❓ {item.get('question', '')}\n\n"
            f"{options_text}\n\n"
            f"✅ Answer: {item.get('answer', '')}\n\n"
            f"📝 {item.get('explanation', '')}"
        )
    else:
        body = item.get("content", "")

    # NO parse_mode — raw user content, Markdown would break on special chars
    await update.message.reply_text(header + body)


async def cmd_deletepost(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    if not context.args:
        await update.message.reply_text("Usage: /deletepost POST_ID\nExample: /deletepost POST_5")
        return

    post_id = context.args[0].upper()
    archive = load_json("archive.json")
    queue   = load_json("queue.json")

    new_archive = [i for i in archive if i.get("id", "").upper() != post_id]
    new_queue   = [i for i in queue   if i.get("id", "").upper() != post_id]

    if len(new_archive) == len(archive):
        await update.message.reply_text(f"❌ Post `{post_id}` not found in archive.", parse_mode="Markdown")
        return

    removed_from_queue = len(new_queue) < len(queue)
    save_json("archive.json", new_archive)
    save_json("queue.json",   new_queue)

    msg = f"🗑 Deleted `{post_id}` from archive."
    if removed_from_queue:
        msg += " Also removed from queue."
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_requeue(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    if not context.args:
        await update.message.reply_text("Usage: /requeue POST_ID\nExample: /requeue POST_5")
        return

    post_id = context.args[0].upper()
    archive = load_json("archive.json")
    queue   = load_json("queue.json")

    item = next((i for i in archive if i.get("id", "").upper() == post_id), None)

    if not item:
        await update.message.reply_text(f"❌ Post `{post_id}` not found in archive.", parse_mode="Markdown")
        return

    # CHECK IF ALREADY IN QUEUE
    if any(i.get("id", "").upper() == post_id for i in queue):
        await update.message.reply_text(f"⚠️ `{post_id}` is already in the queue.", parse_mode="Markdown")
        return

    queue.insert(0, item)
    save_json("queue.json", queue)

    await update.message.reply_text(
        f"🔼 `{post_id}` moved to the *front of the queue*!\nIt will be posted on the next cycle.",
        parse_mode="Markdown"
    )


async def cmd_clearqueue(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    queue = load_json("queue.json")
    count = len(queue)

    if count == 0:
        await update.message.reply_text("📭 Queue is already empty.")
        return

    save_json("queue.json", [])
    await update.message.reply_text(f"🗑 Queue cleared. {count} post(s) removed.")


async def cmd_cleararchive(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await deny(update)
        return

    archive = load_json("archive.json")
    count = len(archive)

    if count == 0:
        await update.message.reply_text("📭 Archive is already empty.")
        return

    save_json("archive.json", [])
    await update.message.reply_text(f"🗑 Archive cleared. {count} post(s) permanently deleted.")


# ─────────────────────────────────────────
# START BOT
# ─────────────────────────────────────────

app = ApplicationBuilder().token(BOT_TOKEN).build()

# FILE HANDLER (json file attachment)
app.add_handler(MessageHandler(filters.ATTACHMENT, handle_document))

# TEXT HANDLER (pasted json text)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# COMMANDS
app.add_handler(CommandHandler("start",        cmd_start))
app.add_handler(CommandHandler("help",         cmd_help))
app.add_handler(CommandHandler("status",       cmd_status))
app.add_handler(CommandHandler("pause",        cmd_pause))
app.add_handler(CommandHandler("resume",       cmd_resume))
app.add_handler(CommandHandler("next",         cmd_next))
app.add_handler(CommandHandler("skip",         cmd_skip))
app.add_handler(CommandHandler("queue",        cmd_queue))
app.add_handler(CommandHandler("clear",        cmd_clear))
app.add_handler(CommandHandler("confirmclear", cmd_confirmclear))
app.add_handler(CommandHandler("cancel",       cmd_cancel))
app.add_handler(CommandHandler("setinterval",  cmd_setinterval))
app.add_handler(CommandHandler("revision",      cmd_revision))
app.add_handler(CommandHandler("poll",          cmd_poll))
app.add_handler(CommandHandler("channels",      cmd_channels))
app.add_handler(CommandHandler("addchannel",    cmd_addchannel))
app.add_handler(CommandHandler("togglechannel", cmd_togglechannel))
app.add_handler(CommandHandler("removechannel", cmd_removechannel))
app.add_handler(CommandHandler("archive",       cmd_archive))
app.add_handler(CommandHandler("viewpost",      cmd_viewpost))
app.add_handler(CommandHandler("deletepost",    cmd_deletepost))
app.add_handler(CommandHandler("requeue",       cmd_requeue))
app.add_handler(CommandHandler("clearqueue",    cmd_clearqueue))
app.add_handler(CommandHandler("cleararchive",  cmd_cleararchive))

# DAILY REPORT — every day at 11 PM
app.job_queue.run_daily(
    send_daily_report,
    time=datetime.now().replace(hour=23, minute=0, second=0).time()
)

# AUTO POST WATCHDOG — restarts auto_post if it ever crashes
async def auto_post_watchdog(context):
    while True:
        try:
            print("🔁 Starting auto_post task...")
            await auto_post(app)
        except Exception as e:
            print(f"💀 auto_post crashed: {e}. Restarting in 10s...")
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ *Auto-post loop crashed and restarted:*\n`{e}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            await asyncio.sleep(10)

app.job_queue.run_once(
    lambda context: asyncio.create_task(auto_post_watchdog(context)),
    when=1
)

print("🚀 Bot is running...")
app.run_polling()
