"""
Telegram Engagement Bot - PRODUCTION VERSION
All features properly implemented with proper formatting
"""

from telegram import Update, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import datetime
import asyncio
import os
import aiohttp
from urllib.parse import quote

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════
TOKEN = os.environ.get("TOKEN", "")
CHAT_ID = int(os.environ.get("CHAT_ID", "-1003800205030"))
POST_TOPIC_ID = int(os.environ.get("POST_TOPIC_ID", "2"))
WARN_TOPIC_ID = int(os.environ.get("WARN_TOPIC_ID", "902"))
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:5000")

ENGAGE_THRESHOLD = 90
MAX_SESSION_NUM = 4

# IST Session Schedule
SCHEDULE_IST = [
    {"open": (11, 0), "close": (11, 30), "check": (15, 30), "report": (15, 45), "notify10": (15, 50), "notify5": (15, 55)},
    {"open": (16, 0), "close": (16, 30), "check": (19, 30), "report": (19, 45), "notify10": (19, 50), "notify5": (19, 55)},
    {"open": (20, 0), "close": (20, 30), "check": (23, 30), "report": (23, 45), "notify10": (23, 50), "notify5": (23, 55)},
    {"open": (0, 0), "close": (0, 30), "check": (10, 30), "report": (10, 45), "notify10": (10, 50), "notify5": (10, 55)}
]

def ist_to_utc(h, m):
    """Convert IST to UTC (IST = UTC + 5:30)"""
    t = h * 60 + m - 330
    if t < 0:
        t += 1440
    return (t // 60) % 24, t % 60

# ═══════════════════════════════════════════════════════════════
# STATE VARIABLES
# ═══════════════════════════════════════════════════════════════
scheduler = AsyncIOScheduler()
bot_instance = None

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
session_members = set()
session_links = {}

# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════
def timing_text_ist():
    """Generate IST timing display text"""
    times = []
    for s in SCHEDULE_IST:
        h, m = s["open"]
        period = "AM" if h < 12 else "PM"
        h12 = h if h <= 12 else h - 12
        h12 = 12 if h12 == 0 else h12
        times.append(f"{h12}:{str(m).zfill(2)} {period} IST")
    return "\n• ".join(times)

async def auto_delete_after(context, chat_id, msg_id, delay=10):
    """Auto-delete message after delay"""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except:
        pass

def _cache_user(user):
    """Cache user for quick lookup"""
    if not user:
        return
    user_cache[user.id] = user
    if user.username:
        user_cache[user.username.lower()] = user

def track_msg(thread_id, msg_id):
    """Track message for later cleanup"""
    topic_messages.setdefault(thread_id, []).append(msg_id)

async def send_warn_msg(bot, text):
    """Send notification to warn topic"""
    kwargs = {"chat_id": CHAT_ID, "text": text}
    if WARN_TOPIC_ID:
        kwargs["message_thread_id"] = WARN_TOPIC_ID
    msg = await bot.send_message(**kwargs)
    # Warn messages stay visible - no auto-delete
    return msg

async def auto_delete_message(bot, chat_id, msg_id, delay=10):
    """Helper to auto-delete any message"""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except:
        pass

def next_session_num(n):
    """Calculate next session number"""
    return (n % MAX_SESSION_NUM) + 1

# ═══════════════════════════════════════════════════════════════
# ADMIN & USER FUNCTIONS
# ═══════════════════════════════════════════════════════════════
async def is_admin(update, context):
    """Check if user is admin"""
    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    for a in admins:
        _cache_user(a.user)
    return update.effective_user.id in {a.user.id for a in admins}

async def get_admin_ids(bot):
    """Get list of admin IDs"""
    try:
        admins = await bot.get_chat_administrators(CHAT_ID)
        return {a.user.id for a in admins}
    except:
        return set()

async def get_target_user(update, context):
    """Get target user from reply or mention"""
    # Check if replying to message
    if update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
        _cache_user(u)
        return u
    
    # Check if @username or ID provided
    if not context.args:
        return None
    
    target = context.args[0].replace("@", "").lower()
    
    # Try as user ID
    if target.isdigit():
        uid = int(target)
        if uid in user_cache:
            return user_cache[uid]
        try:
            m = await context.bot.get_chat_member(CHAT_ID, uid)
            _cache_user(m.user)
            return m.user
        except:
            pass
    
    # Try as username
    if target in user_cache:
        return user_cache[target]
    
    # Search in admins
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        for a in admins:
            if a.user.username and a.user.username.lower() == target:
                _cache_user(a.user)
                return a.user
    except:
        pass
    
    return None

# ═══════════════════════════════════════════════════════════════
# STREAK FUNCTIONS
# ═══════════════════════════════════════════════════════════════
def update_streak(uid):
    """Update user streak"""
    last = user_last_session.get(uid)
    if last is not None and last == session_number - 1:
        user_streaks[uid] = user_streaks.get(uid, 0) + 1
    elif last != session_number:
        user_streaks[uid] = 1
    user_last_session[uid] = session_number

def streak_emoji(n):
    """Get streak emoji"""
    if n >= 30:
        return "🔥👑"
    if n >= 14:
        return "🔥🔥"
    if n >= 7:
        return "🔥"
    if n >= 3:
        return "⚡"
    return ""

async def send_leaderboard(bot, cid, tid, snum):
    """Send streak leaderboard"""
    if not user_streaks:
        return
    
    top = sorted(user_streaks.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [f"🏆 Streak Leaderboard — Session {snum}\n"]
    
    for rank, (uid, s) in enumerate(top, 1):
        e = streak_emoji(s)
        try:
            m = await bot.get_chat_member(cid, uid)
            name = f"@{m.user.username}" if m.user.username else m.user.full_name
        except:
            name = f"User{uid}"
        lines.append(f"{rank}. {name} — {s} sessions {e}")
    
    lines.append("\n🔥 Keep posting every session!")
    lb = await bot.send_message(chat_id=cid, message_thread_id=tid, text="\n".join(lines))
    track_msg(tid, lb.message_id)

# ═══════════════════════════════════════════════════════════════
# REPORT GENERATION
# ═══════════════════════════════════════════════════════════════
async def build_report(bot, cid, tid, snum, do_warn=True):
    """Generate engagement report"""
    if not session_members:
        await bot.send_message(chat_id=cid, message_thread_id=tid, text=f"📊 Session {snum} — No posts")
        return
    
    total = len(session_members)
    admin_ids = await get_admin_ids(bot)
    
    # Fetch clicks from server
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"{SERVER_URL}/api/clicks/{snum}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    clicks = data.get("clicks", [])
                else:
                    clicks = []
    except:
        clicks = []
    
    # Process click data
    user_clicked = {}
    for c in clicks:
        user_clicked.setdefault(c["tg_id"], set()).add(c["post_num"])
    
    engaged, non_engaged = [], []
    
    for uid in session_members:
        own = {pn for pn, inf in session_links.items() if inf["poster_id"] == uid}
        eligible = total - len(own)
        clicked = user_clicked.get(uid, set()) - own
        count = len(clicked)
        pct = round(count / eligible * 100) if eligible > 0 else 0
        
        cached = user_cache.get(uid)
        tg_name = f"@{cached.username}" if (cached and cached.username) else (cached.full_name if cached else f"User{uid}")
        
        x_name = "?"
        for pn, inf in session_links.items():
            if inf["poster_id"] == uid:
                x_name = f"@{inf['x_username']}"
                break
        
        if pct >= ENGAGE_THRESHOLD:
            engaged.append((tg_name, x_name, pct, count, eligible))
        else:
            non_engaged.append((uid, tg_name, x_name, pct, count, eligible))
    
    # Build report message
    lines = [f"📊 Session {snum} — Engagement Report\n", f"Total Posts: {total}\n"]
    
    if engaged:
        lines.append("✅ Engaged Members:")
        for tg, x, p, c, e in sorted(engaged, key=lambda i: i[2], reverse=True):
            star = " ⭐" if p == 100 else ""
            lines.append(f"  • {tg} ({x}) — {c}/{e} ({p}%){star}")
    else:
        lines.append("✅ Engaged: None")
    
    lines.append("")
    
    if non_engaged:
        lines.append(f"❌ Non-Engagers (below {ENGAGE_THRESHOLD}%):")
        for uid, tg, x, p, c, e in non_engaged:
            if uid in admin_ids:
                continue
            lines.append(f"  • {tg} ({x}) — {c}/{e} ({p}%)")
    else:
        lines.append("❌ Non-Engagers: None 🎉")
    
    report = "\n".join(lines)
    
    # Send report (handle long messages)
    if len(report) <= 4096:
        await bot.send_message(chat_id=cid, message_thread_id=tid, text=report)
    else:
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4096:
                await bot.send_message(chat_id=cid, message_thread_id=tid, text=chunk)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            await bot.send_message(chat_id=cid, message_thread_id=tid, text=chunk)
    
    # Auto-warn non-engagers
    if not do_warn:
        return
    
    for uid, tg, x, p, c, e in non_engaged:
        if uid in admin_ids:
            continue
        
        warnings[uid] = warnings.get(uid, 0) + 1
        wc = warnings[uid]
        
        try:
            if wc == 2:
                until = datetime.datetime.now() + datetime.timedelta(days=1)
                await bot.restrict_chat_member(cid, uid, permissions=ChatPermissions(can_send_messages=False), until_date=until)
                await send_warn_msg(bot, f"🚨 User — {tg}\n\n❌ Warned For Not Engaging In Session {snum}\n\n>> Warning {wc}/4\n🔕 Muted For 1 Day")
            elif wc >= 4:
                await bot.ban_chat_member(cid, uid)
                await bot.unban_chat_member(cid, uid)
                await send_warn_msg(bot, f"🚨 User — {tg}\n\n❌ Warned For Not Engaging In Session {snum}\n\n>> Warning {wc}/4\n🚫 Removed From Group")
            else:
                await send_warn_msg(bot, f"🚨 User — {tg}\n\n❌ Warned For Not Engaging In Session {snum}\n\n>> Warning {wc}/4")
        except:
            pass

# ═══════════════════════════════════════════════════════════════
# SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════
def _clear_session():
    """Clear session data"""
    user_posts.clear()
    posted_links.clear()
    session_members.clear()
    session_links.clear()
    global counter
    counter = 1

# ═══════════════════════════════════════════════════════════════
# AUTOMATED SCHEDULER JOBS
# ═══════════════════════════════════════════════════════════════
async def auto_open(sess_num):
    """Auto-open session with correct session number"""
    global session_open, session_number
    if not auto_sessions_enabled:
        return
    
    _clear_session()
    session_open = True
    session_number = sess_num  # Set correct session number
    
    # Open topic
    try:
        await bot_instance.reopen_forum_topic(chat_id=CHAT_ID, message_thread_id=POST_TOPIC_ID)
    except:
        pass
    
    # Send opening message
    sent = await bot_instance.send_message(
        chat_id=CHAT_ID,
        message_thread_id=POST_TOPIC_ID,
        text=f"❑ Session {session_number} Started Now ❑\n\n✅ Start Posting Your Links Now"
    )
    track_msg(POST_TOPIC_ID, sent.message_id)

async def auto_close():
    """Auto-close session"""
    global session_open
    session_open = False
    total = len(user_posts)
    
    # Send closing message
    timings = timing_text_ist()
    sent = await bot_instance.send_message(
        chat_id=CHAT_ID,
        message_thread_id=POST_TOPIC_ID,
        text=f"""❑ Session {session_number} Closed Now ❑

> Total Links - {total}

✅ Make Sure Engage With All Links Do Like & Drop Meaningful Comments

✅ Follow Before Engaging

❌ Don't Delete The Links ( If You Caught = Permanent Ban )

> Session Timing :-
• {timings}"""
    )
    track_msg(POST_TOPIC_ID, sent.message_id)
    
    # Close topic
    try:
        await bot_instance.close_forum_topic(chat_id=CHAT_ID, message_thread_id=POST_TOPIC_ID)
    except:
        pass

async def pre_check():
    """Pre-check warning"""
    sent = await bot_instance.send_message(
        chat_id=CHAT_ID,
        message_thread_id=POST_TOPIC_ID,
        text=f"✅ It's Checking Time Now For Session {session_number} ✅"
    )
    track_msg(POST_TOPIC_ID, sent.message_id)

async def generate_report():
    """Generate and send report"""
    await send_leaderboard(bot_instance, CHAT_ID, POST_TOPIC_ID, session_number)
    await build_report(bot_instance, CHAT_ID, POST_TOPIC_ID, session_number, do_warn=True)

async def notify_10min(next_sess_num):
    """10 minute notification with correct next session number"""
    next_s = next_sess_num
    sent = await bot_instance.send_message(
        chat_id=CHAT_ID,
        message_thread_id=POST_TOPIC_ID,
        text=f"⚡️ **ATTENTION ALL MEMBERS** ⚡️\n\n❑ Session {next_s} Starting In 10 Minutes Be Ready With Your Links",
        parse_mode="Markdown"
    )
    track_msg(POST_TOPIC_ID, sent.message_id)

async def notify_5min(next_sess_num):
    """5 minute notification with correct next session number"""
    next_s = next_sess_num
    sent = await bot_instance.send_message(
        chat_id=CHAT_ID,
        message_thread_id=POST_TOPIC_ID,
        text=f"⚡️ **ATTENTION ALL MEMBERS** ⚡️\n\n❑ Session {next_s} Starting In 5 Minutes Be Ready With Your Links",
        parse_mode="Markdown"
    )
    track_msg(POST_TOPIC_ID, sent.message_id)
    # Session number will be set by auto_open, not here

# ═══════════════════════════════════════════════════════════════
# COMMAND HANDLERS - RESTRICTED TO POST TOPIC
# ═══════════════════════════════════════════════════════════════
async def startsession(update, context):
    """Start session manually (POST_TOPIC_ID only)"""
    if update.message.message_thread_id != POST_TOPIC_ID:
        return
    if not await is_admin(update, context):
        return
    
    global session_open
    _clear_session()
    session_open = True
    
    # Open topic
    try:
        await context.bot.reopen_forum_topic(chat_id=CHAT_ID, message_thread_id=POST_TOPIC_ID)
    except:
        pass
    
    reply = await update.message.reply_text(f"❑ Session {session_number} Started Now ❑\n\n✅ Start Posting Your Links Now")
    
    # Auto-delete command and reply
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))
    asyncio.create_task(auto_delete_after(context, CHAT_ID, reply.message_id, 10))

async def endsession(update, context):
    """End session manually (POST_TOPIC_ID only)"""
    if update.message.message_thread_id != POST_TOPIC_ID:
        return
    if not await is_admin(update, context):
        return
    
    global session_open
    session_open = False
    total = len(user_posts)
    
    timings = timing_text_ist()
    reply = await update.message.reply_text(
        f"""❑ Session {session_number} Closed Now ❑

> Total Links - {total}

✅ Make Sure Engage With All Links Do Like & Drop Meaningful Comments

✅ Follow Before Engaging

❌ Don't Delete The Links ( If You Caught = Permanent Ban )

> Session Timing :-
• {timings}"""
    )
    
    # Close topic
    try:
        await context.bot.close_forum_topic(chat_id=CHAT_ID, message_thread_id=POST_TOPIC_ID)
    except:
        pass
    
    # Auto-delete command and reply
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))
    asyncio.create_task(auto_delete_after(context, CHAT_ID, reply.message_id, 10))

async def report_cmd(update, context):
    """Generate report (POST_TOPIC_ID only)"""
    if update.message.message_thread_id != POST_TOPIC_ID:
        return
    if not await is_admin(update, context):
        return
    
    sess = int(context.args[0]) if (context.args and context.args[0].isdigit()) else session_number
    await build_report(context.bot, CHAT_ID, POST_TOPIC_ID, sess, do_warn=False)
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def coolme(update, context):
    """Delete own post (POST_TOPIC_ID only)"""
    if update.message.message_thread_id != POST_TOPIC_ID:
        return
    
    user = update.effective_user
    if user.id not in user_posts:
        return
    
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data=f"delete_{user.id}"),
        InlineKeyboardButton("No", callback_data="cancel")
    ]])
    
    reply = await update.message.reply_text("Delete your post?", reply_markup=kb)
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

# ═══════════════════════════════════════════════════════════════
# COMMAND HANDLERS - WORK EVERYWHERE
# ═══════════════════════════════════════════════════════════════
async def pin(update, context):
    """Pin message"""
    if not await is_admin(update, context):
        return
    
    if update.message.reply_to_message:
        await context.bot.pin_chat_message(CHAT_ID, update.message.reply_to_message.message_id)
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def unpin(update, context):
    """Unpin specific message"""
    if not await is_admin(update, context):
        return
    
    if update.message.reply_to_message:
        await context.bot.unpin_chat_message(CHAT_ID, update.message.reply_to_message.message_id)
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def delete_msg(update, context):
    """Delete message"""
    if not await is_admin(update, context):
        return
    
    if update.message.reply_to_message:
        await context.bot.delete_message(CHAT_ID, update.message.reply_to_message.message_id)
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def mute(update, context):
    """Mute user"""
    if not await is_admin(update, context):
        return
    
    user = await get_target_user(update, context)
    if not user:
        reply = await update.message.reply_text("❌ User not found")
        asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))
        asyncio.create_task(auto_delete_after(context, CHAT_ID, reply.message_id, 10))
        return
    
    # Get days (default 1)
    days = 1
    for arg in (context.args or []):
        if arg.isdigit():
            days = int(arg)
            break
    
    until = datetime.datetime.now() + datetime.timedelta(days=days)
    await context.bot.restrict_chat_member(
        CHAT_ID, user.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until
    )
    
    uname = f"@{user.username}" if user.username else user.full_name
    await send_warn_msg(context.bot, f"🔕 User — {uname}\n\n>> Muted For {days} Days")
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def unmute(update, context):
    """Unmute user"""
    if not await is_admin(update, context):
        return
    
    user = await get_target_user(update, context)
    if not user:
        reply = await update.message.reply_text("❌ User not found")
        asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))
        asyncio.create_task(auto_delete_after(context, CHAT_ID, reply.message_id, 10))
        return
    
    await context.bot.restrict_chat_member(
        CHAT_ID, user.id,
        permissions=ChatPermissions(can_send_messages=True)
    )
    
    uname = f"@{user.username}" if user.username else user.full_name
    await send_warn_msg(context.bot, f"🔔 User — {uname}\n\n>> Unmuted")
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def warn(update, context):
    """Warn user"""
    if not await is_admin(update, context):
        return
    
    user = await get_target_user(update, context)
    if not user:
        reply = await update.message.reply_text("❌ User not found")
        asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))
        asyncio.create_task(auto_delete_after(context, CHAT_ID, reply.message_id, 10))
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
        await send_warn_msg(context.bot, f"⚠️ User — {uname}\n\n>> Warned {wc}/4\n🔕 Muted For 1 Day")
    elif wc >= 4:
        await context.bot.ban_chat_member(CHAT_ID, user.id)
        await context.bot.unban_chat_member(CHAT_ID, user.id)
        await send_warn_msg(context.bot, f"🚫 User — {uname}\n\n>> Warned {wc}/4\n❌ Removed From Group")
    else:
        await send_warn_msg(context.bot, f"⚠️ User — {uname}\n\n>> Warning {wc}/4")
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def removewarn(update, context):
    """Remove warnings"""
    if not await is_admin(update, context):
        return
    
    user = await get_target_user(update, context)
    if not user:
        reply = await update.message.reply_text("❌ User not found")
        asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))
        asyncio.create_task(auto_delete_after(context, CHAT_ID, reply.message_id, 10))
        return
    
    warnings[user.id] = 0
    uname = f"@{user.username}" if user.username else user.full_name
    await send_warn_msg(context.bot, f"✅ User — {uname}\n\n>> Warnings Reset")
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def remove(update, context):
    """Remove user from group"""
    if not await is_admin(update, context):
        return
    
    user = await get_target_user(update, context)
    if not user:
        reply = await update.message.reply_text("❌ User not found")
        asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))
        asyncio.create_task(auto_delete_after(context, CHAT_ID, reply.message_id, 10))
        return
    
    await context.bot.ban_chat_member(CHAT_ID, user.id)
    await context.bot.unban_chat_member(CHAT_ID, user.id)
    
    uname = f"@{user.username}" if user.username else user.full_name
    await send_warn_msg(context.bot, f"👋 User — {uname}\n\n>> Removed From Group")
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def opentopic(update, context):
    """Open forum topic"""
    if not await is_admin(update, context):
        return
    
    if update.message.message_thread_id:
        await context.bot.reopen_forum_topic(CHAT_ID, update.message.message_thread_id)
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def closetopic(update, context):
    """Close forum topic"""
    if not await is_admin(update, context):
        return
    
    if update.message.message_thread_id:
        await context.bot.close_forum_topic(CHAT_ID, update.message.message_thread_id)
    
    # Auto-delete command
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 10))

async def clear_topic(update, context):
    """Clear topic messages"""
    if not await is_admin(update, context):
        return
    
    tid = update.message.message_thread_id
    ids = topic_messages.get(tid, []).copy()
    ids.append(update.message.message_id)
    
    deleted = 0
    for mid in ids:
        try:
            await context.bot.delete_message(CHAT_ID, mid)
            deleted += 1
            await asyncio.sleep(0.05)
        except:
            pass
    
    topic_messages[tid] = []

async def topicid(update, context):
    """Show topic ID"""
    if not await is_admin(update, context):
        return
    
    tid = update.message.message_thread_id
    reply = await update.message.reply_text(f"📌 Topic ID: `{tid}`", parse_mode="Markdown")
    
    # Auto-delete after 30 seconds
    asyncio.create_task(auto_delete_after(context, CHAT_ID, update.message.message_id, 30))
    asyncio.create_task(auto_delete_after(context, CHAT_ID, reply.message_id, 30))

async def setsession(update, context):
    """Session settings dashboard"""
    kb = [
        [InlineKeyboardButton("View Timings", callback_data="view_times")],
        [InlineKeyboardButton("Toggle Auto", callback_data="toggle_auto")],
        [InlineKeyboardButton("Stats", callback_data="stats")],
        [InlineKeyboardButton("Streaks", callback_data="streaks")],
    ]
    await update.message.reply_text("⚙ Dashboard", reply_markup=InlineKeyboardMarkup(kb))

# ═══════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════
async def handle_message(update, context):
    """Handle link posting"""
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
    
    # Check for @i username
    if "x.com/i/" in text or "/i/" in text:
        await update.message.delete()
        sent = await context.bot.send_message(
            chat_id=CHAT_ID,
            message_thread_id=POST_TOPIC_ID,
            text=f"⟡ Hey {user.full_name}\n\nPlease Replace The @i With Your Real X Username\n\nThank You 😊"
        )
        asyncio.create_task(auto_delete_after(context, CHAT_ID, sent.message_id, 30))
        return
    
    # Process valid post
    posted_links.add(text)
    update_streak(user.id)
    session_members.add(user.id)
    
    streak = user_streaks.get(user.id, 1)
    s_emoji = f" {streak_emoji(streak)}" if streak >= 3 else ""
    
    # Extract X username
    x_username = "Unknown"
    try:
        if "x.com" in text:
            x_username = text.split("/")[3]
    except:
        pass
    
    post_num = counter
    session_links[post_num] = {
        "url": text,
        "poster_id": user.id,
        "x_username": x_username
    }
    
    # Format message
    formatted = f"Post - {post_num}\n𖣯 Name - {user.full_name}{s_emoji}\n𖣯 X - @{x_username}\n‣ {text}"
    
    await update.message.delete()
    
    # Create tracking URL
    track_url = f"{SERVER_URL}/track?uid={user.id}&post={post_num}&sess={session_number}&x={quote(x_username)}&link={quote(text)}"
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Visit & Engage", url=track_url)
    ]])
    
    sent = await context.bot.send_message(
        chat_id=CHAT_ID,
        message_thread_id=POST_TOPIC_ID,
        text=formatted,
        reply_markup=keyboard
    )
    
    user_posts[user.id] = sent.message_id
    track_msg(POST_TOPIC_ID, sent.message_id)
    counter += 1

# ═══════════════════════════════════════════════════════════════
# BUTTON HANDLERS
# ═══════════════════════════════════════════════════════════════
async def button_handler(update, context):
    """Handle inline button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("delete_"):
        uid = int(query.data.split("_")[1])
        if uid in user_posts:
            await context.bot.delete_message(CHAT_ID, user_posts[uid])
            del user_posts[uid]
            await query.edit_message_text("✅ Post deleted")
    
    elif query.data == "cancel":
        await query.edit_message_text("❌ Cancelled")

async def dashboard_buttons(update, context):
    """Handle dashboard button callbacks"""
    global auto_sessions_enabled
    query = update.callback_query
    await query.answer()
    
    if query.data == "view_times":
        timings = timing_text_ist()
        await query.edit_message_text(f"📅 Session Times (IST):\n\n• {timings}")
    
    elif query.data == "toggle_auto":
        auto_sessions_enabled = not auto_sessions_enabled
        await query.edit_message_text(f"🤖 Auto Sessions: {'✅ ON' if auto_sessions_enabled else '❌ OFF'}")
    
    elif query.data == "stats":
        await query.edit_message_text(
            f"📊 Current Stats:\n\n"
            f"Session: {session_number}\n"
            f"Posts: {len(user_posts)}\n"
            f"Threshold: {ENGAGE_THRESHOLD}%"
        )
    
    elif query.data == "streaks":
        if not user_streaks:
            await query.edit_message_text("No streak data yet")
            return
        
        top = sorted(user_streaks.items(), key=lambda x: x[1], reverse=True)[:10]
        lines = ["🔥 Top Streaks\n"]
        
        for rank, (uid, s) in enumerate(top, 1):
            e = streak_emoji(s)
            try:
                m = await context.bot.get_chat_member(CHAT_ID, uid)
                name = f"@{m.user.username}" if m.user.username else m.user.full_name
            except:
                name = f"User{uid}"
            lines.append(f"{rank}. {name} — {s} {e}")
        
        await query.edit_message_text("\n".join(lines))

async def cache_new_member(update, context):
    """Cache new members"""
    if update.message.new_chat_members:
        for m in update.message.new_chat_members:
            _cache_user(m)

# ═══════════════════════════════════════════════════════════════
# SETUP & START
# ═══════════════════════════════════════════════════════════════
app = ApplicationBuilder().token(TOKEN).build()

# Register all command handlers
commands = [
    # Restricted to POST_TOPIC_ID
    ("startsession", startsession),
    ("endsession", endsession),
    ("report", report_cmd),
    ("coolme", coolme),
    
    # Work everywhere
    ("pin", pin),
    ("unpin", unpin),
    ("del", delete_msg),
    ("mute", mute),
    ("unmute", unmute),
    ("warn", warn),
    ("removewarn", removewarn),
    ("remove", remove),
    ("opentopic", opentopic),
    ("closetopic", closetopic),
    ("clear", clear_topic),
    ("topicid", topicid),
    ("setsession", setsession),
]

for cmd, fn in commands:
    app.add_handler(CommandHandler(cmd, fn))

# Register message handlers
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, cache_new_member))

# Register callback handlers
app.add_handler(CallbackQueryHandler(button_handler, pattern="^(delete_|cancel)"))
app.add_handler(CallbackQueryHandler(dashboard_buttons, pattern="^(view_times|toggle_auto|stats|streaks)$"))

# Schedule automated jobs with correct session mapping
# Session mapping: 11AM=1, 4PM=2, 8PM=3, 12AM=4
SESSION_NUMBERS = [1, 2, 3, 4]

for idx, sched in enumerate(SCHEDULE_IST):
    oh, om = ist_to_utc(*sched["open"])
    ch, cm = ist_to_utc(*sched["close"])
    ckh, ckm = ist_to_utc(*sched["check"])
    rh, rm = ist_to_utc(*sched["report"])
    n10h, n10m = ist_to_utc(*sched["notify10"])
    n5h, n5m = ist_to_utc(*sched["notify5"])
    
    current_session = SESSION_NUMBERS[idx]
    next_session = SESSION_NUMBERS[(idx + 1) % 4]  # Wrap around after session 4
    
    # Pass session numbers to functions
    scheduler.add_job(lambda s=current_session: auto_open(s), "cron", hour=oh, minute=om, id=f"open_{idx}")
    scheduler.add_job(auto_close, "cron", hour=ch, minute=cm, id=f"close_{idx}")
    scheduler.add_job(pre_check, "cron", hour=ckh, minute=ckm, id=f"check_{idx}")
    scheduler.add_job(generate_report, "cron", hour=rh, minute=rm, id=f"rep_{idx}")
    scheduler.add_job(lambda ns=next_session: notify_10min(ns), "cron", hour=n10h, minute=n10m, id=f"n10_{idx}")
    scheduler.add_job(lambda ns=next_session: notify_5min(ns), "cron", hour=n5h, minute=n5m, id=f"n5_{idx}")


async def start_scheduler(application):
    """Initialize scheduler"""
    global bot_instance
    bot_instance = application.bot
    scheduler.start()
    print("✅ Scheduler started - Auto sessions enabled")

app.post_init = start_scheduler

if __name__ == "__main__":
    print("🚀 Telegram Engagement Bot Starting...")
    print(f"📊 Threshold: {ENGAGE_THRESHOLD}%")
    print(f"🔢 Sessions: {MAX_SESSION_NUM}")
    print("✅ All systems ready!")
    app.run_polling()
