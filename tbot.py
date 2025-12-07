import re
import asyncio
import json
import logging
from datetime import datetime, timedelta, time, timezone
from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
    JobQueue,
    ChatMemberHandler,
)
import os

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = "group_data.json"

INACTIVITY_HOURS = 24
REMOVAL_HOURS = 72
JOB_INTERVAL_SECONDS = 3600  # hourly check

# DAILY TAG AT EXACTLY 01:00 UTC
DAILY_TAG_TIME = time(hour=1, minute=0, tzinfo=timezone.utc)

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- GLOBAL STORAGE ----------------
group_data = {}  

# ---------------- DATA PERSISTENCE ----------------
def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(group_data, f, default=str, indent=2)
    except Exception as e:
        logger.error(f"Error saving data: {e}")

def load_data():
    global group_data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                group_data = json.load(f)

            # Convert timestamps back to datetime objects
            for chat_id, data in group_data.items():
                data["user_last_message_time"] = {
                    int(uid): datetime.fromisoformat(ts)
                    for uid, ts in data.get("user_last_message_time", {}).items()
                }
                for uid, minfo in data.get("members_info", {}).items():
                    if minfo.get("muted_until"):
                        minfo["muted_until"] = datetime.fromisoformat(minfo["muted_until"])
        except Exception as e:
            logger.error(f"Error loading data: {e}")

load_data()

# ---------------- UTILITY ----------------
def extract_urls(text: str):
    return re.findall(r"(https?://\S+)", text)

async def auto_delete_message(context, chat_id, msg_id, delay):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except:
        pass

# ---------------- LINK STACKING ----------------
async def update_stack_message(context, chat_id):
    data = group_data[chat_id]
    if not data["stacked_urls"]:
        return

    text = "\n\n\n".join(f"{i+1}. {url}" for i, url in enumerate(data["stacked_urls"]))

    if data.get("last_bot_message_id"):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=data["last_bot_message_id"])
        except:
            pass

    msg = await context.bot.send_message(chat_id=chat_id, text=text)
    data["last_bot_message_id"] = msg.message_id
    save_data()

async def stack_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    # Init chat storage
    if chat_id not in group_data:
        group_data[chat_id] = {
            "stacked_urls": [],
            "last_bot_message_id": None,
            "user_last_message_time": {},
            "members_info": {},
        }

    data = group_data[chat_id]
    now = datetime.now(timezone.utc)
    data["user_last_message_time"][user.id] = now

    # Ensure member info
    if user.id not in data["members_info"]:
        data["members_info"][user.id] = {
            "username": user.username,
            "first_name": user.first_name,
            "muted_until": None
        }

    urls = extract_urls(update.message.text)
    if not urls:
        return

    data["stacked_urls"].extend(urls)
    try:
        await update.message.delete()
    except:
        pass

    await update_stack_message(context, chat_id)
    save_data()

async def reset_stack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    member = await context.bot.get_chat_member(chat_id, user.id)

    if member.status not in ["administrator", "creator"]:
        await update.message.reply_text("Only admins can reset the list.")
        return

    data = group_data.get(chat_id, {})
    data["stacked_urls"] = []

    if data.get("last_bot_message_id"):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=data["last_bot_message_id"])
        except:
            pass
        data["last_bot_message_id"] = None

    await update.message.reply_text("URL list reset.")
    save_data()

# ---------------- MEMBER TRACKING ----------------
async def track_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    member = update.chat_member.new_chat_member
    user = member.user

    if user.is_bot or member.status in ["administrator", "creator"]:
        return

    if chat_id not in group_data:
        group_data[chat_id] = {
            "stacked_urls": [],
            "last_bot_message_id": None,
            "user_last_message_time": {},
            "members_info": {},
        }

    data = group_data[chat_id]

    data["members_info"][user.id] = {
        "username": user.username,
        "first_name": user.first_name,
        "muted_until": None
    }
    data["user_last_message_time"][user.id] = datetime.now(timezone.utc)
    save_data()

# ---------------- MUTING / REMOVAL ----------------
async def mute_remove_inactive(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)

    for chat_id, data in group_data.items():
        for uid, minfo in list(data["members_info"].items()):
            try:
                member = await context.bot.get_chat_member(chat_id, uid)
                if member.status in ["administrator", "creator"]:
                    continue
            except:
                continue

            last_msg = data["user_last_message_time"].get(uid)
            muted_until = minfo.get("muted_until")

            # Mute after 24h
            if last_msg and (now - last_msg) > timedelta(hours=INACTIVITY_HOURS) and not muted_until:
                try:
                    await context.bot.restrict_chat_member(
                        chat_id,
                        uid,
                        permissions=ChatPermissions(can_send_messages=False)
                    )
                    minfo["muted_until"] = now
                    msg = await context.bot.send_message(chat_id, f"@{minfo['username']} has been muted for 24h inactivity.")
                    asyncio.create_task(auto_delete_message(context, chat_id, msg.message_id, 60))
                    save_data()
                except:
                    continue

            # Remove after 72h of mute
            if muted_until and (now - muted_until) > timedelta(hours=REMOVAL_HOURS):
                try:
                    await context.bot.ban_chat_member(chat_id, uid)
                    msg = await context.bot.send_message(chat_id, f"@{minfo['username']} removed for 72h inactivity.")
                    asyncio.create_task(auto_delete_message(context, chat_id, msg.message_id, 60))
                    minfo["muted_until"] = None
                    data["user_last_message_time"].pop(uid, None)
                    save_data()
                except:
                    continue

# ---------------- UNMUTE ----------------
async def unmute_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    admin = await context.bot.get_chat_member(chat_id, update.effective_user.id)

    if admin.status not in ["administrator", "creator"]:
        await update.message.reply_text("Only admins can unmute.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unmute @username")
        return

    username = context.args[0].replace("@", "").strip()
    data = group_data.get(chat_id, {})

    target_uid = None

    # Find user in saved data
    for uid, minfo in data.get("members_info", {}).items():
        if minfo.get("username") == username:
            target_uid = int(uid)
            break

    if not target_uid:
        await update.message.reply_text(f"User @{username} not found in records.")
        return

    try:
        await context.bot.restrict_chat_member(
            chat_id,
            target_uid,
            permissions=ChatPermissions(can_send_messages=True)
        )
        data["members_info"][target_uid]["muted_until"] = None
        data["user_last_message_time"][target_uid] = datetime.now(timezone.utc)

        msg = await update.message.reply_text(f"@{username} has been unmuted.")
        asyncio.create_task(auto_delete_message(context, chat_id, msg.message_id, 60))
        save_data()

    except Exception as e:
        await update.message.reply_text(f"Failed to unmute: {e}")

# ---------------- DAILY TAG ----------------
async def daily_tag_inactive(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)

    for chat_id, data in group_data.items():
        inactive = []

        for uid, minfo in data.get("members_info", {}).items():

            # Skip admins
            try:
                m = await context.bot.get_chat_member(chat_id, uid)
                if m.status in ["administrator", "creator"]:
                    continue
            except:
                continue

            last_msg = data["user_last_message_time"].get(uid)
            if not last_msg or (now - last_msg) > timedelta(hours=INACTIVITY_HOURS):
                tag = f"@{minfo['username']}" if minfo.get("username") else minfo["first_name"]
                inactive.append(tag)

        if inactive:
            msg = "Inactive in last 24 hours:\n" + " ".join(inactive)
            await context.bot.send_message(chat_id=chat_id, text=msg)

# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, stack_urls))
    app.add_handler(CommandHandler("reset", reset_stack))
    app.add_handler(CommandHandler("unmute", unmute_member))
    app.add_handler(ChatMemberHandler(track_new_member, ChatMemberHandler.CHAT_MEMBER))

    job_queue: JobQueue = app.job_queue
    job_queue.run_repeating(mute_remove_inactive, interval=JOB_INTERVAL_SECONDS, first=10)

    job_queue.run_daily(daily_tag_inactive, time=DAILY_TAG_TIME)

    logger.info("Bot is running…")
    app.run_polling()