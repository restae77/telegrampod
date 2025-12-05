import re
import asyncio
from datetime import datetime, timedelta, time, timezone
from telegram import Update, ChatMember
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

# BOT TOKEN LOADED FROM RAILWAY ENVIRONMENT VARIABLE
BOT_TOKEN = os.getenv("BOT_TOKEN")

INACTIVITY_HOURS = 24
REMOVAL_HOURS = 72
DAILY_TAG_HOUR_UTC1 = 22  # 11 PM UTC+1
JOB_INTERVAL_SECONDS = 3600  # Check every hour

# Global storage
group_data = {}  # {chat_id: {"stacked_urls": [], "last_bot_message_id": None, "user_last_message_time": {}, "all_members": []}}

def extract_urls(text: str):
    return re.findall(r"(https?://\S+)", text)

# ---------------- LINK STACKING ----------------
async def update_stack_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    data = group_data[chat_id]
    if not data["stacked_urls"]:
        return

    text = "\n\n\n".join(f"{i+1}. {url}" for i, url in enumerate(data["stacked_urls"]))

    if data["last_bot_message_id"]:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=data["last_bot_message_id"])
        except:
            pass

    msg = await context.bot.send_message(chat_id=chat_id, text=text)
    data["last_bot_message_id"] = msg.message_id

async def stack_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id not in group_data:
        group_data[chat_id] = {
            "stacked_urls": [],
            "last_bot_message_id": None,
            "user_last_message_time": {},
            "all_members": [],
        }

    data = group_data[chat_id]
    now = datetime.now(timezone.utc)
    data["user_last_message_time"][user.id] = now

    urls = extract_urls(update.message.text)
    if not urls:
        return

    data["stacked_urls"].extend(urls)
    try:
        await update.message.delete()
    except:
        pass

    await update_stack_message(context, chat_id)

async def reset_stack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    member = await context.bot.get_chat_member(chat_id, user.id)
    if member.status not in ["administrator", "creator"]:
        await update.message.reply_text("Only admins can reset the list.")
        return

    data = group_data.get(chat_id, None)
    if data:
        data["stacked_urls"] = []
        if data["last_bot_message_id"]:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=data["last_bot_message_id"])
            except:
                pass
            data["last_bot_message_id"] = None

    await update.message.reply_text("URL list has been reset.")

# ---------------- MEMBER TRACKING ----------------
async def track_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    member: ChatMember = update.chat_member
    user = member.new_chat_member.user

    if chat_id not in group_data:
        group_data[chat_id] = {
            "stacked_urls": [],
            "last_bot_message_id": None,
            "user_last_message_time": {},
            "all_members": [],
        }
    data = group_data[chat_id]

    if user.is_bot or member.new_chat_member.status in ["administrator", "creator"]:
        return

    if user.id not in [m.user.id for m in data["all_members"]]:
        data["all_members"].append(member.new_chat_member)
        data["user_last_message_time"][user.id] = datetime.now(timezone.utc)

# ---------------- MUTING / REMOVAL ----------------
async def mute_remove_inactive(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)
    for chat_id, data in group_data.items():
        for member in data.get("all_members", []):
            if member.user.is_bot or member.status in ["administrator", "creator"]:
                continue
            last_msg = data["user_last_message_time"].get(member.user.id)
            muted_until = getattr(member.user, "muted_until", None)

            if not last_msg or (now - last_msg) > timedelta(hours=INACTIVITY_HOURS):
                if not muted_until:
                    try:
                        await context.bot.restrict_chat_member(
                            chat_id=chat_id,
                            user_id=member.user.id,
                            permissions=None,
                        )
                        member.user.muted_until = now
                        msg = await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"@{member.user.username} has been muted for inactivity.",
                        )
                        asyncio.create_task(auto_delete_message(context, chat_id, msg.message_id, 60))
                    except:
                        continue

            if muted_until and (now - muted_until) > timedelta(hours=REMOVAL_HOURS):
                try:
                    await context.bot.ban_chat_member(chat_id=chat_id, user_id=member.user.id)
                    msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"@{member.user.username} has been removed for inactivity.",
                    )
                    asyncio.create_task(auto_delete_message(context, chat_id, msg.message_id, 60))
                    del data["user_last_message_time"][member.user.id]
                    member.user.muted_until = None
                except:
                    continue

# ---------------- UNMUTE ----------------
async def unmute_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    admin = await context.bot.get_chat_member(chat_id, update.effective_user.id)

    if admin.status not in ["administrator", "creator"]:
        await update.message.reply_text("Only admins can unmute members.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unmute @username")
        return

    username = context.args[0].lstrip("@")
    data = group_data.get(chat_id, None)

    if not data:
        return

    target = next((m for m in data["all_members"] if m.user.username == username), None)

    if not target:
        await update.message.reply_text(f"User @{username} not found.")
        return

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id, 
            user_id=target.user.id,
            permissions={"can_send_messages": True}
        )
        data["user_last_message_time"][target.user.id] = datetime.now(timezone.utc)
        target.user.muted_until = None
        msg = await update.message.reply_text(f"@{username} has been unmuted.")
        asyncio.create_task(auto_delete_message(context, chat_id, msg.message_id, 60))
    except:
        pass

# ---------------- DAILY TAG ----------------
async def daily_tag_inactive(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)
    for chat_id, data in group_data.items():
        inactive_users = []
        for member in data.get("all_members", []):
            if member.user.is_bot or member.status in ["administrator", "creator"]:
                continue

            last_msg = data["user_last_message_time"].get(member.user.id)

            if not last_msg or (now - last_msg) > timedelta(hours=INACTIVITY_HOURS):
                inactive_users.append(f"@{member.user.username}")

        if inactive_users:
            text = "Inactive members in last 24 hours:\n" + " ".join(inactive_users)
            await context.bot.send_message(chat_id=chat_id, text=text)

# ---------------- HELPER ----------------
async def auto_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except:
        pass

# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), stack_urls))
    app.add_handler(CommandHandler("reset", reset_stack))
    app.add_handler(CommandHandler("unmute", unmute_member))
    app.add_handler(ChatMemberHandler(track_new_member, ChatMemberHandler.CHAT_MEMBER))

    job_queue: JobQueue = app.job_queue
    job_queue.run_repeating(mute_remove_inactive, interval=JOB_INTERVAL_SECONDS, first=30)

    job_queue.run_daily(
        daily_tag_inactive,
        time=time(hour=DAILY_TAG_HOUR_UTC1, minute=0, tzinfo=timezone(timedelta(hours=1)))
    )

    print("Bot is running...")
    app.run_polling()
