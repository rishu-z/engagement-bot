"""
Telegram Engagement Bot - SCHEDULER FIXED
"""

from telegram import Update, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import datetime, asyncio, os, aiohttp
from urllib.parse import quote

TOKEN=os.environ.get("TOKEN","")
CHAT_ID=int(os.environ.get("CHAT_ID","-1003800205030"))
POST_TOPIC_ID=int(os.environ.get("POST_TOPIC_ID","2"))
WARN_TOPIC_ID=int(os.environ.get("WARN_TOPIC_ID","902"))
SERVER_URL=os.environ.get("SERVER_URL","http://localhost:5000")
ENGAGE_THRESHOLD=90
MAX_SESSION_NUM=4

SCHEDULE_IST=[
{"open":(11,0),"close":(11,30),"check":(15,30),"report":(15,45),"notify10":(15,50),"notify5":(15,55)},
{"open":(16,0),"close":(16,30),"check":(19,30),"report":(19,45),"notify10":(19,50),"notify5":(19,55)},
{"open":(20,0),"close":(20,30),"check":(23,30),"report":(23,45),"notify10":(23,50),"notify5":(23,55)},
{"open":(0,0),"close":(0,30),"check":(10,30),"report":(10,45),"notify10":(10,50),"notify5":(10,55)}
]

def ist_to_utc(h,m):
 t=h*60+m-330
 if t<0:t+=1440
 return(t//60)%24,t%60

scheduler=AsyncIOScheduler()
session_open=False
auto_sessions_enabled=True
session_number=1
counter=1
user_posts,posted_links,warnings,user_cache,user_streaks,user_last_session,topic_messages,session_members,session_links={},set(),{},{},{},{},{},set(),{}

def timing_text_ist():
 times=[]
 for s in SCHEDULE_IST:
  h,m=s["open"];period="AM"if h<12 else"PM";h12=h if h<=12 else h-12;h12=12 if h12==0 else h12
  times.append(f"{h12}:{str(m).zfill(2)} {period}")
 return" | ".join(times)

async def auto_delete(ctx,cid,mid,delay=30):
 await asyncio.sleep(delay)
 try:await ctx.bot.delete_message(chat_id=cid,message_id=mid)
 except:pass

def _cache_user(user):
 if not user:return
 user_cache[user.id]=user
 if user.username:user_cache[user.username.lower()]=user

def track_msg(tid,mid):topic_messages.setdefault(tid,[]).append(mid)

async def send_warn_msg(bot,text):
 kwargs={"chat_id":CHAT_ID,"text":text}
 if WARN_TOPIC_ID:kwargs["message_thread_id"]=WARN_TOPIC_ID
 await bot.send_message(**kwargs)

def next_session_num(n):return(n%MAX_SESSION_NUM)+1

async def is_admin(update,context):
 admins=await context.bot.get_chat_administrators(update.effective_chat.id)
 for a in admins:_cache_user(a.user)
 return update.effective_user.id in{a.user.id for a in admins}

async def get_admin_ids(bot):
 try:admins=await bot.get_chat_administrators(CHAT_ID);return{a.user.id for a in admins}
 except:return set()

async def get_target_user(update,context):
 if update.message.reply_to_message:u=update.message.reply_to_message.from_user;_cache_user(u);return u
 if not context.args:return None
 target=context.args[0].replace("@","").lower()
 if target.isdigit():
  uid=int(target)
  if uid in user_cache:return user_cache[uid]
  try:m=await context.bot.get_chat_member(CHAT_ID,uid);_cache_user(m.user);return m.user
  except:pass
 if target in user_cache:return user_cache[target]
 try:
  admins=await context.bot.get_chat_administrators(update.effective_chat.id)
  for a in admins:
   if a.user.username and a.user.username.lower()==target:_cache_user(a.user);return a.user
 except:pass
 return None

def update_streak(uid):
 last=user_last_session.get(uid)
 if last is not None and last==session_number-1:user_streaks[uid]=user_streaks.get(uid,0)+1
 elif last!=session_number:user_streaks[uid]=1
 user_last_session[uid]=session_number

def streak_emoji(n):
 if n>=30:return"🔥👑"
 if n>=14:return"🔥🔥"
 if n>=7:return"🔥"
 if n>=3:return"⚡"
 return""

async def send_leaderboard(bot,cid,tid,snum):
 if not user_streaks:return
 top=sorted(user_streaks.items(),key=lambda x:x[1],reverse=True)[:10]
 lines=[f"🏆 Streak Leaderboard — Session {snum}\n"]
 for rank,(uid,s)in enumerate(top,1):
  e=streak_emoji(s)
  try:m=await bot.get_chat_member(cid,uid);name=f"@{m.user.username}"if m.user.username else m.user.full_name
  except:name=f"User{uid}"
  lines.append(f"{rank}. {name} — {s} sessions {e}")
 lines.append("\n🔥 Keep posting!")
 lb=await bot.send_message(chat_id=cid,message_thread_id=tid,text="\n".join(lines))
 track_msg(tid,lb.message_id)

async def build_report(bot,cid,tid,snum,do_warn=True):
 if not session_members:await bot.send_message(chat_id=cid,message_thread_id=tid,text=f"📊 Session {snum} — No posts");return
 total=len(session_members);admin_ids=await get_admin_ids(bot)
 try:
  async with aiohttp.ClientSession()as sess:
   async with sess.get(f"{SERVER_URL}/api/clicks/{snum}")as resp:
    if resp.status==200:data=await resp.json();clicks=data.get("clicks",[])
    else:clicks=[]
 except:clicks=[]
 user_clicked={}
 for c in clicks:user_clicked.setdefault(c["tg_id"],set()).add(c["post_num"])
 engaged,non_engaged=[],[]
 for uid in session_members:
  own={pn for pn,inf in session_links.items()if inf["poster_id"]==uid}
  eligible=total-len(own);clicked=user_clicked.get(uid,set())-own;count=len(clicked)
  pct=round(count/eligible*100)if eligible>0 else 0
  cached=user_cache.get(uid)
  tg_name=f"@{cached.username}"if(cached and cached.username)else(cached.full_name if cached else f"User{uid}")
  x_name="?"
  for pn,inf in session_links.items():
   if inf["poster_id"]==uid:x_name=f"@{inf['x_username']}";break
  if pct>=ENGAGE_THRESHOLD:engaged.append((tg_name,x_name,pct,count,eligible))
  else:non_engaged.append((uid,tg_name,x_name,pct,count,eligible))
 lines=[f"📊 Session {snum} — Engagement Report\n",f"Total Posts: {total}\n"]
 if engaged:
  lines.append("✅ Engaged Members:")
  for tg,x,p,c,e in sorted(engaged,key=lambda i:i[2],reverse=True):
   star=" ⭐"if p==100 else""
   lines.append(f"  • {tg} ({x}) — {c}/{e} ({p}%){star}")
 else:lines.append("✅ Engaged: None")
 lines.append("")
 if non_engaged:
  lines.append(f"❌ Non-Engagers (below {ENGAGE_THRESHOLD}%):")
  for uid,tg,x,p,c,e in non_engaged:
   if uid in admin_ids:continue
   lines.append(f"  • {tg} ({x}) — {c}/{e} ({p}%)")
 else:lines.append("❌ Non-Engagers: None 🎉")
 report="\n".join(lines)
 if len(report)<=4096:await bot.send_message(chat_id=cid,message_thread_id=tid,text=report)
 else:
  chunk=""
  for line in lines:
   if len(chunk)+len(line)+1>4096:await bot.send_message(chat_id=cid,message_thread_id=tid,text=chunk);chunk=line+"\n"
   else:chunk+=line+"\n"
  if chunk:await bot.send_message(chat_id=cid,message_thread_id=tid,text=chunk)
 if not do_warn:return
 for uid,tg,x,p,c,e in non_engaged:
  if uid in admin_ids:continue
  warnings[uid]=warnings.get(uid,0)+1;wc=warnings[uid]
  try:
   if wc==2:
    until=datetime.datetime.now()+datetime.timedelta(days=1)
    await bot.restrict_chat_member(cid,uid,permissions=ChatPermissions(can_send_messages=False),until_date=until)
    await send_warn_msg(bot,f"⚠️ {tg} — Warn {wc}/4 | 🔕 Muted 1 day")
   elif wc>=4:await bot.ban_chat_member(cid,uid);await bot.unban_chat_member(cid,uid);await send_warn_msg(bot,f"🚫 {tg} — Removed (4 warns)")
   else:await send_warn_msg(bot,f"⚠️ {tg} — Warning {wc}/4")
  except:pass

def _clear_session():user_posts.clear();posted_links.clear();session_members.clear();session_links.clear();global counter;counter=1

# SCHEDULER JOBS - FIXED
bot_instance=None

async def auto_open():
 global session_open
 if not auto_sessions_enabled:return
 _clear_session();session_open=True
 try:await bot_instance.reopen_forum_topic(chat_id=CHAT_ID,message_thread_id=POST_TOPIC_ID)
 except:pass
 sent=await bot_instance.send_message(chat_id=CHAT_ID,message_thread_id=POST_TOPIC_ID,text=f"❑ Session {session_number} Started ❑\n\n✅ Start Posting Links")
 track_msg(POST_TOPIC_ID,sent.message_id)

async def auto_close():
 global session_open;session_open=False;total=len(user_posts)
 sent=await bot_instance.send_message(chat_id=CHAT_ID,message_thread_id=POST_TOPIC_ID,text=f"❑ Session {session_number} Closed ❑\n\n> Total - {total}\n\n✅ Engage All\n❌ Don't Delete\n\n> {timing_text_ist()}")
 track_msg(POST_TOPIC_ID,sent.message_id)
 try:await bot_instance.close_forum_topic(chat_id=CHAT_ID,message_thread_id=POST_TOPIC_ID)
 except:pass

async def pre_check():
 sent=await bot_instance.send_message(chat_id=CHAT_ID,message_thread_id=POST_TOPIC_ID,text=f"✅ Checking Time Session {session_number} ✅")
 track_msg(POST_TOPIC_ID,sent.message_id)

async def generate_report():
 await send_leaderboard(bot_instance,CHAT_ID,POST_TOPIC_ID,session_number)
 await build_report(bot_instance,CHAT_ID,POST_TOPIC_ID,session_number,do_warn=True)

async def notify_10min():
 next_s=next_session_num(session_number)
 sent=await bot_instance.send_message(chat_id=CHAT_ID,message_thread_id=POST_TOPIC_ID,text=f"⚡️ **ATTENTION** ⚡️\n\n❑ Session {next_s} In 10 Mins",parse_mode="Markdown")
 track_msg(POST_TOPIC_ID,sent.message_id)

async def notify_5min():
 global session_number;next_s=next_session_num(session_number)
 sent=await bot_instance.send_message(chat_id=CHAT_ID,message_thread_id=POST_TOPIC_ID,text=f"⚡️ **ATTENTION** ⚡️\n\n❑ Session {next_s} In 5 Mins",parse_mode="Markdown")
 track_msg(POST_TOPIC_ID,sent.message_id);session_number=next_s

async def startsession(update,context):
 if not await is_admin(update,context):return
 global session_open;_clear_session();session_open=True
 try:await context.bot.reopen_forum_topic(chat_id=CHAT_ID,message_thread_id=POST_TOPIC_ID)
 except:pass
 await update.message.reply_text(f"✅ Session {session_number} Started\n\n❑ Post Links")
 await update.message.delete()

async def endsession(update,context):
 if not await is_admin(update,context):return
 global session_open;session_open=False;total=len(user_posts)
 await update.message.reply_text(f"❑ Session {session_number} Closed ❑\n\n> Total - {total}\n\n✅ Engage All\n> {timing_text_ist()}")
 try:await context.bot.close_forum_topic(chat_id=CHAT_ID,message_thread_id=POST_TOPIC_ID)
 except:pass
 await update.message.delete()

async def handle_message(update,context):
 global counter
 if update.message.message_thread_id!=POST_TOPIC_ID:return
 if not session_open:await update.message.delete();return
 text=update.message.text or"";user=update.message.from_user;_cache_user(user);track_msg(update.message.message_thread_id,update.message.message_id)
 if"http"not in text:return
 if user.id in user_posts:await update.message.delete();return
 if text in posted_links:await update.message.delete();return
 if"x.com/i/"in text or"/i/"in text:
  await update.message.delete()
  sent=await context.bot.send_message(chat_id=update.effective_chat.id,message_thread_id=update.message.message_thread_id,text=f"⟡ Hey {user.full_name}\n\nReplace @i With Real X Username\n\nThank You 😊")
  asyncio.create_task(auto_delete(context,update.effective_chat.id,sent.message_id,30));return
 posted_links.add(text);update_streak(user.id);session_members.add(user.id)
 streak=user_streaks.get(user.id,1);s_emoji=f" {streak_emoji(streak)}"if streak>=3 else""
 x_username="Unknown"
 try:
  if"x.com"in text:x_username=text.split("/")[3]
 except:pass
 post_num=counter;session_links[post_num]={"url":text,"poster_id":user.id,"x_username":x_username}
 formatted=f"Post - {post_num}\n𖣯 Name - {user.full_name}{s_emoji}\n𖣯 X - @{x_username}\n‣ {text}"
 await update.message.delete()
 track_url=f"{SERVER_URL}/track?uid={user.id}&post={post_num}&sess={session_number}&x={quote(x_username)}&link={quote(text)}"
 keyboard=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Visit & Engage",url=track_url)]])
 sent=await context.bot.send_message(chat_id=update.effective_chat.id,message_thread_id=update.message.message_thread_id,text=formatted,reply_markup=keyboard)
 user_posts[user.id]=sent.message_id;track_msg(update.message.message_thread_id,sent.message_id);counter+=1

async def report_cmd(update,context):
 if not await is_admin(update,context):return
 sess=int(context.args[0])if(context.args and context.args[0].isdigit())else session_number
 await build_report(context.bot,update.effective_chat.id,update.message.message_thread_id,sess,do_warn=False)
 await update.message.delete()

async def topicid(update,context):
 if not await is_admin(update,context):return
 tid=update.message.message_thread_id;msg=await update.message.reply_text(f"📌 Topic ID: `{tid}`",parse_mode="Markdown")
 asyncio.create_task(auto_delete(context,update.effective_chat.id,msg.message_id,30));await update.message.delete()

async def coolme(update,context):
 if update.message.message_thread_id!=POST_TOPIC_ID:return
 user=update.effective_user
 if user.id not in user_posts:return
 kb=InlineKeyboardMarkup([[InlineKeyboardButton("Yes",callback_data=f"delete_{user.id}"),InlineKeyboardButton("No",callback_data="cancel")]])
 await update.message.reply_text("Delete post?",reply_markup=kb);await update.message.delete()

async def button_handler(update,context):
 query=update.callback_query;await query.answer()
 if query.data.startswith("delete_"):uid=int(query.data.split("_")[1]);
 if uid in user_posts:await context.bot.delete_message(CHAT_ID,user_posts[uid]);del user_posts[uid];await query.edit_message_text("Deleted.")
 elif query.data=="cancel":await query.edit_message_text("Cancelled.")

async def warn(update,context):
 if not await is_admin(update,context):return
 user=await get_target_user(update,context)
 if not user:return
 warnings[user.id]=warnings.get(user.id,0)+1;wc=warnings[user.id];uname=f"@{user.username}"if user.username else user.full_name
 if wc==2:
  until=datetime.datetime.now()+datetime.timedelta(days=1)
  await context.bot.restrict_chat_member(CHAT_ID,user.id,permissions=ChatPermissions(can_send_messages=False),until_date=until)
  await send_warn_msg(context.bot,f"❑ {uname}\nWarned {wc}/4\n🔕 Muted 1d")
 elif wc>=4:await context.bot.ban_chat_member(CHAT_ID,user.id);await context.bot.unban_chat_member(CHAT_ID,user.id);await send_warn_msg(context.bot,f"❑ {uname}\nWarned {wc}/4\n🚫 Removed")
 else:await send_warn_msg(context.bot,f"❑ {uname}\nWarned {wc}/4")
 await update.message.delete()

async def removewarn(update,context):
 if not await is_admin(update,context):return
 user=await get_target_user(update,context)
 if not user:return
 warnings[user.id]=0;uname=f"@{user.username}"if user.username else user.full_name
 await send_warn_msg(context.bot,f"❑ {uname}\nWarnings Reset ✅");await update.message.delete()

async def mute(update,context):
 if not await is_admin(update,context):return
 user=await get_target_user(update,context)
 if not user:return
 days=next((int(a)for a in(context.args or[])if a.isdigit()),1);until=datetime.datetime.now()+datetime.timedelta(days=days)
 await context.bot.restrict_chat_member(CHAT_ID,user.id,permissions=ChatPermissions(can_send_messages=False),until_date=until)
 await send_warn_msg(context.bot,f"🔕 @{user.username or user.full_name} muted {days}d");await update.message.delete()

async def unmute(update,context):
 if not await is_admin(update,context):return
 user=await get_target_user(update,context)
 if not user:return
 await context.bot.restrict_chat_member(CHAT_ID,user.id,permissions=ChatPermissions(can_send_messages=True))
 await send_warn_msg(context.bot,f"🔔 @{user.username or user.full_name} unmuted");await update.message.delete()

async def ban(update,context):
 if not await is_admin(update,context):return
 user=await get_target_user(update,context)
 if not user:return
 await context.bot.ban_chat_member(CHAT_ID,user.id);await send_warn_msg(context.bot,f"🚫 @{user.username or user.full_name} banned");await update.message.delete()

async def unban(update,context):
 if not await is_admin(update,context):return
 user=await get_target_user(update,context)
 if not user:return
 await context.bot.unban_chat_member(CHAT_ID,user.id);await send_warn_msg(context.bot,f"✅ @{user.username or user.full_name} unbanned");await update.message.delete()

async def remove(update,context):
 if not await is_admin(update,context):return
 user=await get_target_user(update,context)
 if not user:return
 await context.bot.ban_chat_member(CHAT_ID,user.id);await context.bot.unban_chat_member(CHAT_ID,user.id)
 await send_warn_msg(context.bot,f"👋 @{user.username or user.full_name} removed");await update.message.delete()

async def pin(update,context):
 if not await is_admin(update,context):return
 if update.message.reply_to_message:await context.bot.pin_chat_message(CHAT_ID,update.message.reply_to_message.message_id)
 await update.message.delete()

async def unpin(update,context):
 if not await is_admin(update,context):return
 if update.message.reply_to_message:await context.bot.unpin_chat_message(CHAT_ID,update.message.reply_to_message.message_id)
 await update.message.delete()

async def delete_msg(update,context):
 if not await is_admin(update,context):return
 if update.message.reply_to_message:await context.bot.delete_message(CHAT_ID,update.message.reply_to_message.message_id)
 await update.message.delete()

async def clear_topic(update,context):
 if not await is_admin(update,context):return
 tid=update.message.message_thread_id;ids=topic_messages.get(tid,[]).copy();ids.extend([update.message.message_id]);deleted=0
 for mid in ids:
  try:await context.bot.delete_message(CHAT_ID,mid);deleted+=1;await asyncio.sleep(0.05)
  except:pass
 topic_messages[tid]=[]

async def opentopic(update,context):
 if not await is_admin(update,context):return
 if update.message.message_thread_id:await context.bot.reopen_forum_topic(CHAT_ID,update.message.message_thread_id)
 await update.message.delete()

async def closetopic(update,context):
 if not await is_admin(update,context):return
 if update.message.message_thread_id:await context.bot.close_forum_topic(CHAT_ID,update.message.message_thread_id)
 await update.message.delete()

async def setsession(update,context):
 kb=[[InlineKeyboardButton("Timings",callback_data="view_times")],[InlineKeyboardButton("Toggle Auto",callback_data="toggle_auto")],[InlineKeyboardButton("Stats",callback_data="stats")],[InlineKeyboardButton("Streaks",callback_data="streaks")]]
 await update.message.reply_text("⚙ Dashboard",reply_markup=InlineKeyboardMarkup(kb))

async def dashboard_buttons(update,context):
 global auto_sessions_enabled;query=update.callback_query;await query.answer()
 if query.data=="view_times":await query.edit_message_text(f"Session Times:\n{timing_text_ist()}")
 elif query.data=="toggle_auto":auto_sessions_enabled=not auto_sessions_enabled;await query.edit_message_text(f"Auto: {'ON'if auto_sessions_enabled else'OFF'}")
 elif query.data=="stats":await query.edit_message_text(f"Session: {session_number}\nPosts: {len(user_posts)}\nThreshold: {ENGAGE_THRESHOLD}%")
 elif query.data=="streaks":
  if not user_streaks:await query.edit_message_text("No streaks yet.");return
  top=sorted(user_streaks.items(),key=lambda x:x[1],reverse=True)[:10];lines=["🔥 Top Streaks\n"]
  for rank,(uid,s)in enumerate(top,1):
   e=streak_emoji(s)
   try:m=await context.bot.get_chat_member(CHAT_ID,uid);name=f"@{m.user.username}"if m.user.username else m.user.full_name
   except:name=f"User{uid}"
   lines.append(f"{rank}. {name} — {s} {e}")
  await query.edit_message_text("\n".join(lines))

async def cache_new_member(update,context):
 if update.message.new_chat_members:
  for m in update.message.new_chat_members:_cache_user(m)

app=ApplicationBuilder().token(TOKEN).build()
for cmd,fn in[("startsession",startsession),("endsession",endsession),("report",report_cmd),("warn",warn),("removewarn",removewarn),("mute",mute),("unmute",unmute),("ban",ban),("unban",unban),("remove",remove),("pin",pin),("unpin",unpin),("del",delete_msg),("coolme",coolme),("setsession",setsession),("opentopic",opentopic),("closetopic",closetopic),("clear",clear_topic),("topicid",topicid)]:
 app.add_handler(CommandHandler(cmd,fn))
app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,handle_message))
app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS,cache_new_member))
app.add_handler(CallbackQueryHandler(button_handler,pattern="^(delete_|cancel)"))
app.add_handler(CallbackQueryHandler(dashboard_buttons,pattern="^(view_times|toggle_auto|stats|streaks)$"))

for idx,sched in enumerate(SCHEDULE_IST):
 oh,om=ist_to_utc(*sched["open"]);ch,cm=ist_to_utc(*sched["close"]);ckh,ckm=ist_to_utc(*sched["check"]);rh,rm=ist_to_utc(*sched["report"]);n10h,n10m=ist_to_utc(*sched["notify10"]);n5h,n5m=ist_to_utc(*sched["notify5"])
 scheduler.add_job(auto_open,"cron",hour=oh,minute=om,id=f"open_{idx}")
 scheduler.add_job(auto_close,"cron",hour=ch,minute=cm,id=f"close_{idx}")
 scheduler.add_job(pre_check,"cron",hour=ckh,minute=ckm,id=f"check_{idx}")
 scheduler.add_job(generate_report,"cron",hour=rh,minute=rm,id=f"rep_{idx}")
 scheduler.add_job(notify_10min,"cron",hour=n10h,minute=n10m,id=f"n10_{idx}")
 scheduler.add_job(notify_5min,"cron",hour=n5h,minute=n5m,id=f"n5_{idx}")

async def start_scheduler(application):
 global bot_instance;bot_instance=application.bot;scheduler.start()

app.post_init=start_scheduler

if __name__=="__main__":print("🚀 Bot starting...");app.run_polling()
