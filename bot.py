"""
bot.py — Telegram Engagement Bot (Final IST Schedule)

Session Schedule (IST):
  S1: 11:00 AM - 11:30 AM → 3:30 PM check → 3:45 PM report → 3:50/3:55 PM notify → 4:00 PM S2
  S2: 4:00 PM - 4:30 PM   → 7:30 PM check → 7:45 PM report → 7:50/7:55 PM notify → 8:00 PM S3
  S3: 8:00 PM - 8:30 PM   → 11:30 PM check → 11:45 PM report → 11:50/11:55 PM notify → 12:00 AM S4
  S4: 12:00 AM - 12:30 AM → 10:30 AM check → 10:45 AM report → 10:50/10:55 AM notify → 11:00 AM S1
"""

from telegram import Update, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import datetime, asyncio, os
import aiohttp

# ════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════
TOKEN         = os.environ.get("TOKEN", "")
CHAT_ID       = int(os.environ.get("CHAT_ID", "-1003800205030"))
POST_TOPIC_ID = int(os.environ.get("POST_TOPIC_ID", "2"))
WARN_TOPIC_ID = int(os.environ.get("WARN_TOPIC_ID", "902"))
SERVER_URL    = os.environ.get("SERVER_URL", "http://localhost:5000")

ENGAGE_THRESHOLD = 70
MAX_SESSION_NUM  = 4

# ════════════════════════════════════════════════════════════════
# SERVER API HELPERS
# ════════════════════════════════════════════════════════════════
async def get_clicks_for_session(sess_num):
    """Fetch clicks from server via HTTP."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{SERVER_URL}/api/clicks/{sess_num}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("clicks", [])
    except Exception as e:
        print(f"Error fetching clicks: {e}")
    return []

# ════════════════════════════════════════════════════════════════
# IST Schedule Definition
# Format: {open, close, check, report, notify10, notify5}
# All times in IST (hour, minute)
# ════════════════════════════════════════════════════════════════
SCHEDULE_IST = [
    # Session 1
    {
        "open":     (11, 0),   # 11:00 AM
        "close":    (11, 30),  # 11:30 AM
        "check":    (15, 30),  # 3:30 PM
        "report":   (15, 45),  # 3:45 PM
        "notify10": (15, 50),  # 3:50 PM
        "notify5":  (15, 55),  # 3:55 PM
    },
    # Session 2
    {
        "open":     (16, 0),   # 4:00 PM
        "close":    (16, 30),  # 4:30 PM
        "check":    (19, 30),  # 7:30 PM
        "report":   (19, 45),  # 7:45 PM
        "notify10": (19, 50),  # 7:50 PM
        "notify5":  (19, 55),  # 7:55 PM
    },
    # Session 3
    {
        "open":     (20, 0),   # 8:00 PM
        "close":    (20, 30),  # 8:30 PM
        "check":    (23, 30),  # 11:30 PM
        "report":   (23, 45),  # 11:45 PM
        "notify10": (23, 50),  # 11:50 PM
        "notify5":  (23, 55),  # 11:55 PM
    },
    # Session 4
    {
        "open":     (0, 0),    # 12:00 AM
        "close":    (0, 30),   # 12:30 AM
        "check":    (10, 30),  # 10:30 AM (next day)
        "report":   (10, 45),  # 10:45 AM
        "notify10": (10, 50),  # 10:50 AM
        "notify5":  (10, 55),  # 10:55 AM
    },
]

def ist_to_utc(ist_hour, ist_min):
    """Convert IST to UTC (IST = UTC + 5:30)."""
    total_min = ist_hour * 60 + ist_min - 330
    if total_min < 0:
        total_min += 1440
    return (total_min // 60) % 24, total_min % 60

# ════════════════════════════════════════════════════════════════
# STATE
# ════════════════════════════════════════════════════════════════
scheduler = AsyncIOScheduler()
session_open = False
auto_sessions_enabled = True
session_number = 1
counter = 1

user_posts = {}
posted_links = set()
warnings = {}
user_cache = {}
user_streaks = {}
user_last_session = {}
topic_messages = {}
session_links = {}
session_members = set()

# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════
def timing_text_ist():
    """Human readable IST session open times."""
    times = []
    for s in SCHEDULE_IST:
        h, m = s["open"]
        period = "AM" if h < 12 else "PM"
        h12 = h if h <= 12 else h - 12
        h12 = 12 if h12 == 0 else h12
        times.append(f"{h12}:{str(m).zfill(2)} {period}")
    return " | ".join(times)

async def auto_delete(ctx, chat_id, msg_id, delay=30):
    await asyncio.sleep(delay)
    try:
        await ctx.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass

def _cache_user(user):
    if not user:
        return
    user_cache[user.id] = user
    if user.username:
        user_cache[user.username.lower()] = user

def track_msg(thread_id, msg_id):
    topic_messages.setdefault(thread_id, []).append(msg_id)

async def send_warn_msg(bot, text):
    kwargs = {"chat_id": CHAT_ID, "text": text}
    if WARN_TOPIC_ID:
        kwargs["message_thread_id"] = WARN_TOPIC_ID
    await bot.send_message(**kwargs)

def next_session_num(n):
    return (n % MAX_SESSION_NUM) + 1

def make_tracking_url(tg_id, post_num, sess_num):
    return f"{SERVER_URL}/visit?uid={tg_id}&post={post_num}&sess={sess_num}"

# ════════════════════════════════════════════════════════════════
# ADMIN / USER
# ════════════════════════════════════════════════════════════════
async def is_admin(update, context):
    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    for a in admins:
        _cache_user(a.user)
    return update.effective_user.id in {a.user.id for a in admins}

async def get_admin_ids(bot):
    try:
        admins = await bot.get_chat_administrators(CHAT_ID)
        return {a.user.id for a in admins}
    except Exception:
        return set()

async def get_target_user(update, context):
    if update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
        _cache_user(u)
        return u
    if not context.args:
        return None

    target = context.args[0].replace("@", "").lower()

    if target.isdigit():
        uid = int(target)
        if uid in user_cache:
            return user_cache[uid]
        try:
            m = await context.bot.get_chat_member(CHAT_ID, uid)
            _cache_user(m.user)
            return m.user
        except Exception:
            pass

    if target in user_cache:
        return user_cache[target]

    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        for a in admins:
            if a.user.username and a.user.username.lower() == target:
                _cache_user(a.user)
                return a.user
    except Exception:
        pass
    return None

# ════════════════════════════════════════════════════════════════
# STREAK
# ════════════════════════════════════════════════════════════════
def update_streak(user_id):
    last = user_last_session.get(user_id)
    if last is not None and last == session_number - 1:
        user_streaks[user_id] = user_streaks.get(user_id, 0) + 1
    elif last != session_number:
        user_streaks[user_id] = 1
    user_last_session[user_id] = session_number

def streak_emoji(n):
    if n >= 30: return "🔥👑"
    if n >= 14: return "🔥🔥"
    if n >= 7: return "🔥"
    if n >= 3: return "⚡"
    return ""

async def send_leaderboard(bot, chat_id, thread_id, sess_num):
    if not user_streaks:
        return
    top = sorted(user_streaks.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [f"🏆 Streak Leaderboard — Session {sess_num}\n"]
    for rank, (uid, s) in enumerate(top, 1):
        e = streak_emoji(s)
        try:
            m = await bot.get_chat_member(chat_id, uid)
            name = f"@{m.user.username}" if m.user.username else m.user.full_name
        except Exception:
            name = f"User {uid}"
        lines.append(f"{rank}. {name} — {s} sessions {e}")
    lines.append("\n🔥 Keep posting every session to grow your streak!")
    lb = await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text="\n".join(lines))
    track_msg(thread_id, lb.message_id)

# ════════════════════════════════════════════════════════════════
# ENGAGEMENT REPORT
# ════════════════════════════════════════════════════════════════
async def build_report(bot, chat_id, thread_id, sess_num, do_warn=True):
    if not session_links:
        await bot.send_message(
            chat_id=chat_id, message_thread_id=thread_id,
            text=f"📊 Session {sess_num} — No links posted."
        )
        return

    total = len(session_links)
    admin_ids = await get_admin_ids(bot)

    rows = await get_clicks_for_session(sess_num)
    user_clicked_posts = {}
    for entry in rows:
        post_num = entry.get("post_num")
        tg_id = entry.get("tg_id")
        user_clicked_posts.setdefault(tg_id, set()).add(post_num)

    engaged, non_engaged = [], []

    for uid in session_members:
        own_posts = {pn for pn, info in session_links.items() if info["poster_id"] == uid}
        eligible = total - len(own_posts)
        clicked = user_clicked_posts.get(uid, set()) - own_posts
        count = len(clicked)
        pct = round(count / eligible * 100) if eligible > 0 else 0

        cached = user_cache.get(uid)
        tg_name = f"@{cached.username}" if (cached and cached.username) else (cached.full_name if cached else f"User {uid}")

        x_name = "?"
        for pn, info in session_links.items():
            if info["poster_id"] == uid:
                x_name = f"@{info['x_username']}"
                break

        if pct >= ENGAGE_THRESHOLD:
            engaged.append((tg_name, x_name, pct, count, eligible))
        else:
            non_engaged.append((uid, tg_name, x_name, pct, count, eligible))

    lines = [f"📊 Session {sess_num} — Engagement Report\n", f"Total Posts: {total}\n"]

    if engaged:
        lines.append("✅ Engaged Members:")
        for tg_name, x_name, pct, count, elig in sorted(engaged, key=lambda x: x[2], reverse=True):
            star = " ⭐" if pct == 100 else ""
            lines.append(f"  • {tg_name} ({x_name}) — {count}/{elig} ({pct}%){star}")
    else:
        lines.append("✅ Engaged Members: None")

    lines.append("")

    if non_engaged:
        lines.append(f"❌ Non-Engagers (below {ENGAGE_THRESHOLD}%):")
        for uid, tg_name, x_name, pct, count, elig in non_engaged:
            if uid in admin_ids:
                continue
            lines.append(f"  • {tg_name} ({x_name}) — {count}/{elig} ({pct}%)")
    else:
        lines.append("❌ Non-Engagers: None — Great session! 🎉")

    report = "\n".join(lines)
    if len(report) <= 4096:
        await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=report)
    else:
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4096:
                await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=chunk)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=chunk)

    if not do_warn:
        return

    for uid, tg_name, x_name, pct, count, elig in non_engaged:
        if uid in admin_ids:
            continue
        warnings[uid] = warnings.get(uid, 0) + 1
        wc = warnings[uid]
        try:
            if wc == 2:
                until = datetime.datetime.now() + datetime.timedelta(days=1)
                await bot.restrict_chat_member(
                    chat_id, uid,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until
                )
                await send_warn_msg(bot, f"⚠️ {tg_name} — Warning {wc}/4 | 🔕 Muted 1 day (Low Engagement)")
            elif wc >= 4:
                await bot.ban_chat_member(chat_id, uid)
                await bot.unban_chat_member(chat_id, uid)
                await send_warn_msg(bot, f"🚫 {tg_name} — Removed (4 warnings reached)")
            else:
                await send_warn_msg(bot, f"⚠️ {tg_name} — Warning {wc}/4 (Low Engagement)")
        except Exception:
            pass

# ════════════════════════════════════════════════════════════════
# SESSION CLEAR
# ════════════════════════════════════════════════════════════════
def _clear_session():
    user_posts.clear()
    posted_links.clear()
    session_links.clear()
    session_members.clear()
    global counter
    counter = 1

# ════════════════════════════════════════════════════════════════
# AUTO JOBS
# ════════════════════════════════════════════════════════════════
async def auto_open(context):
    global session_open, session_number
    if not auto_sessions_enabled:
        return
    _clear_session()
    session_open = True

    thread_id = context.job.data
    sent = await context.bot.send_message(
        chat_id=CHAT_ID, message_thread_id=thread_id,
        text=f"❑ Session {session_number} Started Now ❑\n\n✅ Start Posting Your Links Now"
    )
    track_msg(thread_id, sent.message_id)

async def auto_close(context):
    global session_open
    thread_id = context.job.data
    total = len(user_posts)
    session_open = False

    sent = await context.bot.send_message(
        chat_id=CHAT_ID, message_thread_id=thread_id,
        text=f"""❑ Session {session_number} Closed Now ❑

> Total Links - {total}

✅ Make Sure Engage With All Links
✅ Follow Before Engaging
❌ Don't Delete The Links ( If You Caught = Permanent Ban )

> Session Timing :- {timing_text_ist()}
"""
    )
    track_msg(thread_id, sent.message_id)

async def pre_check_warning(context):
    thread_id = context.job.data
    sent = await context.bot.send_message(
        chat_id=CHAT_ID, message_thread_id=thread_id,
        text=f"✅ It's Checking Time Now For Session {session_number} ✅"
    )
    track_msg(thread_id, sent.message_id)

async def generate_report(context):
    thread_id = context.job.data
    await send_leaderboard(context.bot, CHAT_ID, thread_id, session_number)
    await build_report(context.bot, CHAT_ID, thread_id, session_number, do_warn=True)

async def notify_10min(context):
    thread_id = context.job.data
    next_sess = next_session_num(session_number)
    sent = await context.bot.send_message(
        chat_id=CHAT_ID, message_thread_id=thread_id,
        text=f"⚡️ **ATTENTION ALL MEMBERS** ⚡️\n\n❑ Session {next_sess} Starting In 10 Minutes Be Ready With Your Links",
        parse_mode="Markdown"
    )
    track_msg(thread_id, sent.message_id)

async def notify_5min(context):
    global session_number
    thread_id = context.job.data
    next_sess = next_session_num(session_number)
    sent = await context.bot.send_message(
        chat_id=CHAT_ID, message_thread_id=thread_id,
        text=f"⚡️ **ATTENTION ALL MEMBERS** ⚡️\n\n❑ Session {next_sess} Starting In 5 Minutes Be Ready With Your Links",
        parse_mode="Markdown"
    )
    track_msg(thread_id, sent.message_id)
    session_number = next_sess

# ════════════════════════════════════════════════════════════════
# MANUAL SESSION
# ════════════════════════════════════════════════════════════════
async def startsession(update, context):
    if not await is_admin(update, context):
        return
    global session_open
    _clear_session()
    session_open = True
    await update.message.reply_text(f"✅ Manual Session {session_number} Started\n\n❑ Start Posting Your Links ❑")
    await update.message.delete()

async def endsession(update, context):
    if not await is_admin(update, context):
        return
    global session_open
    total = len(user_posts)
    session_open = False

    await update.message.reply_text(
        f"""❑ Session {session_number} Closed Now ❑

> Total Links - {total}

✅ Make Sure Engage With All Links
✅ Follow Before Engaging
❌ Don't Delete The Links ( If You Caught = Permanent Ban )

> Session Timing :- {timing_text_ist()}

⏳ Manual session — use /report when ready
"""
    )
    await update.message.delete()

# ════════════════════════════════════════════════════════════════
# COMMANDS
# ════════════════════════════════════════════════════════════════
async def report_cmd(update, context):
    if not await is_admin(update, context):
        return
    if context.args and context.args[0].isdigit():
        sess = int(context.args[0])
    else:
        sess = session_number

    await build_report(
        context.bot, update.effective_chat.id,
        update.message.message_thread_id, sess, do_warn=False
    )
    await update.message.delete()

async def topicid(update, context):
    if not await is_admin(update, context):
        return
    tid = update.message.message_thread_id
    msg = await update.message.reply_text(f"📌 Topic ID: `{tid}`", parse_mode="Markdown")
    asyncio.create_task(auto_delete(context, update.effective_chat.id, msg.message_id, 30))
    await update.message.delete()

# ════════════════════════════════════════════════════════════════
# POST HANDLER
# ════════════════════════════════════════════════════════════════
async def handle_message(update, context):
    global counter

    if update.message.message_thread_id != POST_TOPIC_ID:
        return
    if not session_open:
        await update.message.delete()
        return

    text = update.message.text or ""
    user = update.message.from_user
    _cache_user(user)
    track_msg(update.message.message_thread_id, update.message.message_id)

    if "http" not in text:
        return
    if user.id in user_posts:
        await update.message.delete()
        return
    if text in posted_links:
        await update.message.delete()
        return

    if "x.com/i/" in text or "/i/" in text:
        tg_uname = f"@{user.username}" if user.username else user.full_name
        await update.message.delete()
        sent = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            message_thread_id=update.message.message_thread_id,
            text=f"⟡ Hey {tg_uname}\n\nPlease Replace The @i With Your Real X Username\n\nThank You 😊"
        )
        asyncio.create_task(auto_delete(context, update.effective_chat.id, sent.message_id, 30))
        return

    posted_links.add(text)
    update_streak(user.id)
    session_members.add(user.id)

    streak = user_streaks.get(user.id, 1)
    s_text = f"\n‣‣ Streak - {streak} Sessions {streak_emoji(streak)}" if streak >= 2 else ""

    x_username = "Unknown"
    try:
        if "x.com" in text:
            parts = text.split("/")
            x_username = parts[3] if len(parts) > 3 else "Unknown"
    except Exception:
        pass

    post_num = counter
    session_links[post_num] = {
        "url": text,
        "poster_id": user.id,
        "poster_name": user.full_name,
        "x_username": x_username,
    }

    formatted = (
        f"Post -- {post_num}\n"
        f"‣‣ Name - {user.full_name}\n"
        f"‣‣ X Username - @{x_username}{s_text}\n"
        f"⚡︎ - {text}\n"
    )

    await update.message.delete()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Visit & Engage", callback_data=f"visit_{post_num}")]
    ])

    sent = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text=formatted,
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    user_posts[user.id] = sent.message_id
    track_msg(update.message.message_thread_id, sent.message_id)
    counter += 1

# ════════════════════════════════════════════════════════════════
# BUTTON HANDLER
# ════════════════════════════════════════════════════════════════
async def button_handler(update, context):
    query = update.callback_query
    clicker = query.from_user
    _cache_user(clicker)

    if query.data.startswith("visit_"):
        post_num = int(query.data.split("_")[1])
        if post_num not in session_links:
            await query.answer("Session ended.", show_alert=True)
            return
        if clicker.id == session_links[post_num]["poster_id"]:
            await query.answer("❌ Apna link khud visit nahi kar sakte!", show_alert=True)
            return
        url = make_tracking_url(clicker.id, post_num, session_number)
        await query.answer(url=url)

    elif query.data.startswith("delete_"):
        await query.answer()
        uid = int(query.data.split("_")[1])
        if uid in user_posts:
            await context.bot.delete_message(CHAT_ID, user_posts[uid])
            del user_posts[uid]
            await query.edit_message_text("Post deleted.")

    elif query.data == "cancel":
        await query.answer()
        await query.edit_message_text("Cancelled.")

# ════════════════════════════════════════════════════════════════
# COOLME
# ════════════════════════════════════════════════════════════════
async def coolme(update, context):
    user = update.effective_user
    if user.id not in user_posts:
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes", callback_data=f"delete_{user.id}"),
         InlineKeyboardButton("No", callback_data="cancel")]
    ])
    await update.message.reply_text("Delete your post?", reply_markup=kb)
    await update.message.delete()

# ════════════════════════════════════════════════════════════════
# WARN & MODERATION
# ════════════════════════════════════════════════════════════════
async def warn(update, context):
    if not await is_admin(update, context):
        return
    user = await get_target_user(update, context)
    if not user:
        msg = await update.message.reply_text("User not found.")
        asyncio.create_task(auto_delete(context, update.effective_chat.id, msg.message_id, 10))
        return

    warnings[user.id] = warnings.get(user.id, 0) + 1
    wc = warnings[user.id]
    uname = f"@{user.username}" if user.username else user.full_name

    if wc == 2:
        until = datetime.datetime.now() + datetime.timedelta(days=1)
        await context.bot.restrict_chat_member(
            CHAT_ID, user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await send_warn_msg(context.bot, f"❑ User :- {uname}\nWarned {wc}/4\n🔕 Muted 1 day")
    elif wc >= 4:
        await context.bot.ban_chat_member(CHAT_ID, user.id)
        await context.bot.unban_chat_member(CHAT_ID, user.id)
        await send_warn_msg(context.bot, f"❑ User :- {uname}\nWarned {wc}/4\n🚫 Removed")
    else:
        await send_warn_msg(context.bot, f"❑ User :- {uname}\nWarned {wc}/4")
    await update.message.delete()

async def removewarn(update, context):
    if not await is_admin(update, context):
        return
    user = await get_target_user(update, context)
    if not user:
        msg = await update.message.reply_text("User not found.")
        asyncio.create_task(auto_delete(context, update.effective_chat.id, msg.message_id, 10))
        return
    warnings[user.id] = 0
    uname = f"@{user.username}" if user.username else user.full_name
    await send_warn_msg(context.bot, f"❑ User :- {uname}\nWarnings Reset ✅")
    await update.message.delete()

async def mute(update, context):
    if not await is_admin(update, context):
        return
    user = await get_target_user(update, context)
    if not user:
        msg = await update.message.reply_text("User not found.")
        asyncio.create_task(auto_delete(context, update.effective_chat.id, msg.message_id, 10))
        return
    days = next((int(a) for a in (context.args or []) if a.isdigit()), 1)
    until = datetime.datetime.now() + datetime.timedelta(days=days)
    await context.bot.restrict_chat_member(
        CHAT_ID, user.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until
    )
    uname = f"@{user.username}" if user.username else user.full_name
    await send_warn_msg(context.bot, f"🔕 {uname} muted for {days} day(s)")
    await update.message.delete()

async def unmute(update, context):
    if not await is_admin(update, context):
        return
    user = await get_target_user(update, context)
    if not user:
        msg = await update.message.reply_text("User not found.")
        asyncio.create_task(auto_delete(context, update.effective_chat.id, msg.message_id, 10))
        return
    await context.bot.restrict_chat_member(
        CHAT_ID, user.id, permissions=ChatPermissions(can_send_messages=True)
    )
    uname = f"@{user.username}" if user.username else user.full_name
    await send_warn_msg(context.bot, f"🔔 {uname} unmuted")
    await update.message.delete()

async def ban(update, context):
    if not await is_admin(update, context):
        return
    user = await get_target_user(update, context)
    if not user:
        msg = await update.message.reply_text("User not found.")
        asyncio.create_task(auto_delete(context, update.effective_chat.id, msg.message_id, 10))
        return
    uname = f"@{user.username}" if user.username else user.full_name
    await context.bot.ban_chat_member(CHAT_ID, user.id)
    await send_warn_msg(context.bot, f"🚫 {uname} banned")
    await update.message.delete()

async def unban(update, context):
    if not await is_admin(update, context):
        return
    user = await get_target_user(update, context)
    if not user:
        msg = await update.message.reply_text("User not found.")
        asyncio.create_task(auto_delete(context, update.effective_chat.id, msg.message_id, 10))
        return
    uname = f"@{user.username}" if user.username else user.full_name
    await context.bot.unban_chat_member(CHAT_ID, user.id)
    await send_warn_msg(context.bot, f"✅ {uname} unbanned")
    await update.message.delete()

async def remove(update, context):
    if not await is_admin(update, context):
        return
    user = await get_target_user(update, context)
    if not user:
        msg = await update.message.reply_text("User not found.")
        asyncio.create_task(auto_delete(context, update.effective_chat.id, msg.message_id, 10))
        return
    uname = f"@{user.username}" if user.username else user.full_name
    await context.bot.ban_chat_member(CHAT_ID, user.id)
    await context.bot.unban_chat_member(CHAT_ID, user.id)
    await send_warn_msg(context.bot, f"👋 {uname} removed")
    await update.message.delete()

# ════════════════════════════════════════════════════════════════
# PIN / DELETE / CLEAR / TOPIC
# ════════════════════════════════════════════════════════════════
async def pin(update, context):
    if not await is_admin(update, context):
        return
    if update.message.reply_to_message:
        await context.bot.pin_chat_message(CHAT_ID, update.message.reply_to_message.message_id)
    await update.message.delete()

async def unpin(update, context):
    if not await is_admin(update, context):
        return
    if update.message.reply_to_message:
        await context.bot.unpin_chat_message(CHAT_ID, update.message.reply_to_message.message_id)
    await update.message.delete()

async def delete_msg(update, context):
    if not await is_admin(update, context):
        return
    if update.message.reply_to_message:
        await context.bot.delete_message(CHAT_ID, update.message.reply_to_message.message_id)
    await update.message.delete()

async def clear_topic(update, context):
    if not await is_admin(update, context):
        return
    thread_id = update.message.message_thread_id
    chat_id = update.effective_chat.id
    status = await update.message.reply_text("🗑 Clearing...")

    ids = topic_messages.get(thread_id, []).copy()
    ids.extend([update.message.message_id, status.message_id])

    deleted = 0
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    topic_messages[thread_id] = []
    try:
        done = await context.bot.send_message(
            chat_id=chat_id, message_thread_id=thread_id,
            text=f"✅ Cleared {deleted} messages."
        )
        asyncio.create_task(auto_delete(context, chat_id, done.message_id, 10))
    except Exception:
        pass

async def opentopic(update, context):
    if not await is_admin(update, context):
        return
    if update.message.message_thread_id:
        await context.bot.reopen_forum_topic(
            chat_id=update.effective_chat.id,
            message_thread_id=update.message.message_thread_id
        )
    await update.message.delete()

async def closetopic(update, context):
    if not await is_admin(update, context):
        return
    if update.message.message_thread_id:
        await context.bot.close_forum_topic(
            chat_id=update.effective_chat.id,
            message_thread_id=update.message.message_thread_id
        )
    await update.message.delete()

# ════════════════════════════════════════════════════════════════
# DASHBOARD
# ════════════════════════════════════════════════════════════════
async def setsession(update, context):
    kb = [
        [InlineKeyboardButton("View Timings", callback_data="view_times")],
        [InlineKeyboardButton("Toggle Auto", callback_data="toggle_auto")],
        [InlineKeyboardButton("Stats", callback_data="stats")],
        [InlineKeyboardButton("Streaks", callback_data="streaks")],
    ]
    await update.message.reply_text("⚙ Dashboard", reply_markup=InlineKeyboardMarkup(kb))

async def dashboard_buttons(update, context):
    global auto_sessions_enabled
    query = update.callback_query
    await query.answer()

    if query.data == "view_times":
        await query.edit_message_text(f"Session Times (IST):\n{timing_text_ist()}")
    elif query.data == "toggle_auto":
        auto_sessions_enabled = not auto_sessions_enabled
        await query.edit_message_text(f"Auto Sessions: {'ON' if auto_sessions_enabled else 'OFF'}")
    elif query.data == "stats":
        rows = await get_clicks_for_session(session_number)
        await query.edit_message_text(
            f"Session: {session_number}\nPosts: {len(user_posts)}\nClicks: {len(rows)}"
        )
    elif query.data == "streaks":
        if not user_streaks:
            await query.edit_message_text("No streak data yet.")
            return
        top = sorted(user_streaks.items(), key=lambda x: x[1], reverse=True)[:10]
        lines = ["🔥 Top Streaks\n"]
        for rank, (uid, s) in enumerate(top, 1):
            e = streak_emoji(s)
            try:
                m = await context.bot.get_chat_member(CHAT_ID, uid)
                name = f"@{m.user.username}" if m.user.username else m.user.full_name
            except Exception:
                name = f"User {uid}"
            lines.append(f"{rank}. {name} — {s} sessions {e}")
        await query.edit_message_text("\n".join(lines))

async def cache_new_member(update, context):
    if update.message.new_chat_members:
        for m in update.message.new_chat_members:
            _cache_user(m)

# ════════════════════════════════════════════════════════════════
# HANDLERS
# ════════════════════════════════════════════════════════════════
app = ApplicationBuilder().token(TOKEN).build()

for cmd, fn in [
    ("startsession", startsession), ("endsession", endsession),
    ("report", report_cmd), ("warn", warn), ("removewarn", removewarn),
    ("mute", mute), ("unmute", unmute), ("ban", ban), ("unban", unban),
    ("remove", remove), ("pin", pin), ("unpin", unpin), ("del", delete_msg),
    ("coolme", coolme), ("setsession", setsession),
    ("opentopic", opentopic), ("closetopic", closetopic),
    ("clear", clear_topic), ("topicid", topicid),
]:
    app.add_handler(CommandHandler(cmd, fn))

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, cache_new_member))
app.add_handler(CallbackQueryHandler(button_handler, pattern="^(visit_|delete_|cancel)"))
app.add_handler(CallbackQueryHandler(dashboard_buttons, pattern="^(view_times|toggle_auto|stats|streaks)$"))

# ════════════════════════════════════════════════════════════════
# SCHEDULER (IST → UTC)
# ════════════════════════════════════════════════════════════════
for idx, sched in enumerate(SCHEDULE_IST):
    # Convert all IST times to UTC
    oh, om = ist_to_utc(*sched["open"])
    ch, cm = ist_to_utc(*sched["close"])
    ckh, ckm = ist_to_utc(*sched["check"])
    rh, rm = ist_to_utc(*sched["report"])
    n10h, n10m = ist_to_utc(*sched["notify10"])
    n5h, n5m = ist_to_utc(*sched["notify5"])

    scheduler.add_job(auto_open, "cron", hour=oh, minute=om, args=[POST_TOPIC_ID], id=f"open_{idx}")
    scheduler.add_job(auto_close, "cron", hour=ch, minute=cm, args=[POST_TOPIC_ID], id=f"close_{idx}")
    scheduler.add_job(pre_check_warning, "cron", hour=ckh, minute=ckm, args=[POST_TOPIC_ID], id=f"check_{idx}")
    scheduler.add_job(generate_report, "cron", hour=rh, minute=rm, args=[POST_TOPIC_ID], id=f"rep_{idx}")
    scheduler.add_job(notify_10min, "cron", hour=n10h, minute=n10m, args=[POST_TOPIC_ID], id=f"n10_{idx}")
    scheduler.add_job(notify_5min, "cron", hour=n5h, minute=n5m, args=[POST_TOPIC_ID], id=f"n5_{idx}")

async def start_scheduler(application):
    scheduler.start()

app.post_init = start_scheduler
app.run_polling()

