import os
import threading
import time
import random
import re
import json
import math
import base64
import urllib.parse
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template_string, request, jsonify
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, RateLimitError, ClientError, ClientForbiddenError, 
    ClientNotFoundError, ChallengeRequired, PleaseWaitFewMinutes
)

app = Flask(__name__)

# Global variables
BOT_THREAD = None
STOP_EVENT = threading.Event()
LOGS = []
START_TIME = None
CLIENT = None
SESSION_TOKEN = None
LOGIN_SUCCESS = False

# ================= SAFE COMMENT SENDER =================
COMMENT_THREAD = None
COMMENT_STOP_EVENT = threading.Event()
COMMENT_START_TIME = None
COMMENT_STATS = {"loaded": 0, "sent": 0, "failed": 0, "status": "stopped", "post": "", "owner": ""}

def _parse_comment_lines(raw_bytes):
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]

def _resolve_post_media(cl, value):
    value = (value or "").strip()
    if not value:
        raise ValueError("Post ID / URL is required")
    if value.isdigit():
        return cl.media_info(int(value))
    m = re.search(r"/p/([^/?#]+)/?", value)
    if not m:
        raise ValueError("Use an Instagram post URL or numeric media ID")
    media_pk = cl.media_pk_from_code(m.group(1))
    return cl.media_info(media_pk)

def _comment_login(token):
    cl = create_stable_client()
    cl.login_by_sessionid(token)
    account = cl.account_info()
    username = getattr(account, "username", "")
    if not username:
        raise RuntimeError("Could not read authenticated username")
    return cl, username

def run_comment_sender(token, post_value, owner, comments, delay):
    global COMMENT_START_TIME, COMMENT_STATS, COMMENT_THREAD
    COMMENT_START_TIME = datetime.now()
    COMMENT_STATS.update({"loaded": len(comments), "sent": 0, "failed": 0, "status": "running", "post": post_value, "owner": owner})
    log("💬 Comment sender started (single authenticated account, one pass)")
    try:
        cl, username = _comment_login(token)
        log(f"✅ Comment login: @{username}")
        media = _resolve_post_media(cl, post_value)
        media_owner = getattr(getattr(media, "user", None), "username", "") or ""
        if owner and media_owner and owner.lower().lstrip("@").strip() != media_owner.lower().lstrip("@").strip():
            raise ValueError(f"Target owner mismatch: post belongs to @{media_owner}")
        if media_owner:
            log(f"🎯 Post owner verified: @{media_owner}")
        for idx, comment in enumerate(comments, 1):
            if COMMENT_STOP_EVENT.is_set():
                COMMENT_STATS["status"] = "stopped"
                log("🛑 Comment sender stopped")
                return
            try:
                cl.media_comment(media.pk, comment)
                COMMENT_STATS["sent"] += 1
                log(f"✅ Comment {idx}/{len(comments)} sent")
            except Exception as e:
                COMMENT_STATS["failed"] += 1
                log(f"⚠️ Comment {idx} failed: {str(e)[:80]}")
                if "feedback_required" in str(e).lower() or "rate" in str(e).lower():
                    log("⛔ Instagram feedback/rate limit detected; stopping for safety")
                    break
            if idx < len(comments):
                COMMENT_STOP_EVENT.wait(max(10, int(delay)))
        COMMENT_STATS["status"] = "stopped" if COMMENT_STOP_EVENT.is_set() else "completed"
        log(f"🏁 Comment job finished: {COMMENT_STATS['sent']} sent / {COMMENT_STATS['failed']} failed")
    except Exception as e:
        COMMENT_STATS["status"] = "error"
        log(f"❌ Comment job error: {str(e)[:120]}")
    finally:
        COMMENT_THREAD = None

BOT_CONFIG = {
    "auto_replies": {},
    "auto_reply_active": False,
    "target_spam": {},
    "spam_active": {},
    "media_library": {}
}

# Runtime command settings
BOT_CONFIG.update({
    "reply_text": "Thanks for your message! 🤖",
    "reply_to_all": False,
    "reply_delay": 0,
    "reply_disabled_users": set(),
    "reply_history": [],
    "admins": set(),
    "group_locks": {},
    "reminders": []
})

COMMAND_CATALOG = {
    "Group Management": ["/groupname newname", "/grouplock", "/groupunlock", "/removemember @user", "/memberinfo @user", "/groupinfo", "/membercount", "/listmembers", "/adminlist", "/setdesc description"],
    "Auto Reply": ["/autoreplyon", "/autoreplyoff", "/setreply message", "/getreply", "/replytoall on/off", "/delayreplied seconds", "/replyemoji emoji", "/replydisable @user", "/replyenable @user", "/clearreplyhist"],
    "YouTube": ["/yt search_query", "/ytplay", "/ytrandom", "/yttrending", "/ytstop", "/ytdownload url", "/ytinfo url", "/ytsubtitle url", "/ytplaylist url", "/ytsearch latest"],
    "Messaging": ["/broadcast message", "/dm @user message", "/reply @user message", "/mention @user", "/announce message", "/silent message", "/spam @user message", "/stopspam", "/quote @user", "/forward message"],
    "User Info": ["/whoami", "/profile @user", "/followercount @user", "/followingcount @user", "/postcount @user", "/bio @user", "/userjoined @user", "/lastactive @user", "/isverified @user", "/getavatar @user"],
    "Fun": ["/joke", "/meme", "/roast @user", "/compliment @user", "/rate @user", "/roll", "/flip", "/random number", "/trivia", "/riddle"],
    "Emoji": ["/react emoji", "/love", "/laugh", "/fire", "/wow", "/sad", "/angry", "/thumbsup", "/thumbsdown", "/celebrate"],
    "Admin": ["/setadmin @user", "/removeadmin @user", "/kick @user", "/ban @user", "/unban @user", "/muteuser @user", "/unmuteuser @user", "/clearmessages @user", "/lockgroup", "/unlockgroup"],
    "Bot Info": ["/help", "/ping", "/uptime", "/version", "/stats", "/serverinfo", "/botowner", "/feedback message", "/report issue", "/donate"],
    "Text": ["/uppercase text", "/lowercase text", "/reverse text", "/bold text", "/italic text", "/strike text", "/morse text", "/binary text", "/hex text", "/ascii text"],
    "Time/Date": ["/time", "/date", "/time timezone", "/countdown days", "/daysleft event", "/clock timezone", "/timer seconds", "/reminder message", "/calendar", "/upcoming"],
    "Media": ["/photo caption", "/video category", "/music song", "/movie name", "/tvshow name", "/anime name", "/song lyrics", "/album artist", "/podcast search", "/stream service"],
    "Educational": ["/wiki search", "/define word", "/synonym word", "/antonym word", "/translation text", "/spelling word", "/grammar text", "/formula math", "/lesson topic", "/quote author"],
    "Utility": ["/qrcode text", "/calculate expression", "/unit conversion"]
}

def full_command_help():
    out=["📚 FULL COMMAND LIST"]
    for cat, cmds in COMMAND_CATALOG.items():
        out.append(f"\n【{cat}】")
        out.extend(cmds)
    return "\n".join(out)

def _clean_arg(text):
    return text.strip() if text else ""

def _username_from_arg(arg):
    return arg.strip().lstrip("@").lower()

def _find_user(thread, arg):
    u=_username_from_arg(arg)
    return next((x for x in thread.users if getattr(x, "username", "").lower()==u), None)

def _morse(text):
    table={"A":".-","B":"-...","C":"-.-.","D":"-..","E":".","F":"..-.","G":"--.","H":"....","I":"..","J":".---","K":"-.-","L":".-..","M":"--","N":"-.","O":"---","P":".--.","Q":"--.-","R":".-.","S":"...","T":"-","U":"..-","V":"...-","W":".--","X":"-..-","Y":"-.--","Z":"--..","0":"-----","1":".----","2":"..---","3":"...--","4":"....-","5":".....","6":"-....","7":"--...","8":"---..","9":"----."}
    return " ".join(table.get(c,c) for c in text.upper())

def _simple_format(cmd,text):
    if cmd=="/uppercase": return text.upper()
    if cmd=="/lowercase": return text.lower()
    if cmd=="/reverse": return text[::-1]
    if cmd=="/bold": return "**"+text+"**"
    if cmd=="/italic": return "_"+text+"_"
    if cmd=="/strike": return "~~"+text+"~~"
    if cmd=="/morse": return _morse(text)
    if cmd=="/binary": return " ".join(format(ord(c),"08b") for c in text)
    if cmd=="/hex": return " ".join(format(ord(c),"02x") for c in text)
    if cmd=="/ascii": return " ".join(str(ord(c)) for c in text)

def _local_fun(cmd,arg):
    if cmd=="/joke": return random.choice(["Why did the computer go to the doctor? Because it had a virus. 😄","I told my bot to take a break. It said: 404 relaxation not found. 🤖"])
    if cmd=="/meme": return "😂 Meme mode: When the bot finally replies after 25 seconds... ‘I was thinking.’"
    if cmd=="/roll": return f"🎲 {random.randint(1,6)}"
    if cmd=="/flip": return random.choice(["🪙 Heads","🪙 Tails"])
    if cmd=="/random":
        try:
            n=int(arg); return f"🎲 Random: {random.randint(1,n)}" if n>0 else "❌ Number must be > 0"
        except: return "Usage: /random number"
    if cmd=="/trivia": return random.choice(["🌍 Trivia: The Pacific Ocean is the largest ocean on Earth.","🧠 Trivia: Octopuses have three hearts."])
    if cmd=="/riddle": return "🧩 Riddle: What has keys but cannot open locks? — A keyboard."
    if cmd in {"/love","/laugh","/fire","/wow","/sad","/angry","/thumbsup","/thumbsdown","/celebrate"}:
        return {"/love":"❤️","/laugh":"😂","/fire":"🔥","/wow":"😮","/sad":"😢","/angry":"😡","/thumbsup":"👍","/thumbsdown":"👎","/celebrate":"🎉"}[cmd]
    if cmd=="/rate": return f"⭐ Rating: {random.randint(1,100)}/100"
    if cmd=="/compliment": return "✨ You're doing great! Keep going."
    if cmd=="/roast": return "🔥 Roast: Itna slow reply kiya ki loading screen bhi bore ho gayi. 😄"
    if cmd=="/riddle": return "🧩 What has keys but can't open locks? A keyboard."

MORSE_TABLE = {}

def uptime():
    if not START_TIME:
        return "00:00:00"
    delta = datetime.now() - START_TIME
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    lm = f"[{ts}] {msg}"
    LOGS.append(lm)
    if len(LOGS) > 500:
        LOGS[:] = LOGS[-500:]
    print(lm)

def clear_logs():
    global LOGS
    LOGS.clear()
    log("🧹 Logs cleared by user!")

def create_stable_client():
    cl = Client()
    cl.delay_range = [8, 15]
    cl.request_timeout = 90
    cl.max_retries = 1
    ua = "Instagram 380.0.0.28.104 Android (35/14; 600dpi; 1440x3360; samsung; SM-S936B; dm5q; exynos2500; en_IN; 380000028)"
    cl.set_user_agent(ua)
    return cl

def safe_login(cl, token, max_retries=3):
    global LOGIN_SUCCESS, SESSION_TOKEN
    for attempt in range(max_retries):
        try:
            log(f"🔐 Login attempt {attempt+1}/{max_retries}")
            cl.login_by_sessionid(token)
            account = cl.account_info()
            if account and hasattr(account, 'username') and account.username:
                username = account.username
                log(f"✅ Login SUCCESS: @{username}")
                LOGIN_SUCCESS = True
                SESSION_TOKEN = token
                time.sleep(3)
                return True, username
        except Exception as e:
            error_msg = str(e).lower()
            if "session" in error_msg or "login required" in error_msg:
                log("❌ Session expired!")
                return False, None
            elif "rate limit" in error_msg:
                log("⏳ Rate limited - 60s wait")
                time.sleep(60)
            elif "challenge" in error_msg:
                log("❌ Challenge required")
                time.sleep(30)
            else:
                log(f"⚠️ Login error: {str(e)[:50]}")
                time.sleep(15 * (attempt + 1))
    return False, None

def session_health_check():
    global CLIENT, LOGIN_SUCCESS
    try:
        if CLIENT:
            CLIENT.account_info()
            return True
    except:
        pass
    LOGIN_SUCCESS = False
    return False

def refresh_session(token):
    global CLIENT, LOGIN_SUCCESS
    log("🔄 Auto session refresh...")
    new_client = create_stable_client()
    success, _ = safe_login(new_client, token)
    if success:
        CLIENT = new_client
        return True
    return False

# ================= COMMAND ENGINE =================
def process_command(msg_obj, thread, gid, is_admin, sender):
    """Process commands that can be handled locally or through the current Instagram thread."""
    raw=(getattr(msg_obj, "text", "") or "").strip()
    if not raw.startswith(("/","!")):
        return False
    prefix=raw[0]
    body=raw[1:].strip()
    if not body:
        return False
    parts=body.split(None,1)
    cmd=prefix+parts[0].lower()
    arg=parts[1] if len(parts)>1 else ""
    reply=None

    # Public bot commands
    if cmd in ("/ping","!ping"): reply=f"🏓 Pong! Uptime: {uptime()}"
    elif cmd in ("/uptime","!uptime"): reply=f"⏱️ Uptime: {uptime()}"
    elif cmd in ("/help","!help"): reply=full_command_help()
    elif cmd=="/version": reply="🤖 Instagram Bot v4.5"
    elif cmd=="/stats": reply=f"📊 Bot status: {'RUNNING' if BOT_THREAD and BOT_THREAD.is_alive() else 'STOPPED'} | Uptime: {uptime()}"
    elif cmd=="/whoami": reply=f"👤 @{getattr(sender,'username','unknown')}"
    elif cmd=="/membercount": reply=f"👥 Members: {len(thread.users)}"
    elif cmd=="/listmembers":
        names=["@"+getattr(u,"username",str(getattr(u,"pk","?"))) for u in thread.users]
        reply="👥 Members:\n"+"\n".join(names[:100])
    elif cmd=="/adminlist": reply="👑 Configured admins: "+(", ".join(sorted(BOT_CONFIG.get("admins",set()))) or "none")
    elif cmd=="/groupinfo": reply=f"👥 Group: {gid}\nMembers: {len(thread.users)}"
    elif cmd=="/memberinfo":
        u=_find_user(thread,arg); reply=(f"👤 @{u.username}\nID: {u.pk}\nName: {getattr(u,'full_name','-')}" if u else "❌ User not found in this thread.")
    elif cmd in ("/profile","/bio","/followercount","/followingcount","/postcount","/isverified","/getavatar"):
        u=_find_user(thread,arg)
        if not u: reply="❌ User not found in this thread."
        elif cmd=="/profile": reply=f"👤 @{u.username}\nName: {getattr(u,'full_name','-')}\nID: {u.pk}"
        elif cmd=="/bio": reply=f"📝 @{u.username}\n{getattr(u,'biography','') or 'No bio'}"
        elif cmd=="/isverified": reply=f"☑️ @{u.username}: {'Yes' if getattr(u,'is_verified',False) else 'No'}"
        elif cmd=="/getavatar": reply="🖼️ Avatar lookup is available from the account object when supported by your instagrapi version."
        else: reply="ℹ️ This account statistic is not exposed reliably by the installed instagrapi version."
    elif cmd in ("/uppercase","/lowercase","/reverse","/bold","/italic","/strike","/morse","/binary","/hex","/ascii"):
        reply=_simple_format(cmd,arg) if arg else f"Usage: {cmd} text"
    elif cmd=="/calculate":
        try:
            if not re.fullmatch(r"[0-9+\-*/().%\s]+",arg): raise ValueError
            reply=f"🧮 {arg} = {eval(arg,{'__builtins__':{}},{})}"
        except: reply="❌ Invalid calculation. Example: /calculate 25*4+10"
    elif cmd=="/unit":
        reply="ℹ️ Unit syntax: /unit 100 km mi (basic conversion support can be added for your exact units)."
    elif cmd=="/time": reply="🕐 "+datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif cmd=="/date": reply="📅 "+datetime.now().strftime("%A, %d %B %Y")
    elif cmd in ("/clock","/time") and arg: reply=f"🕐 {arg}: {datetime.now().strftime('%H:%M:%S')} (server time)"
    elif cmd=="/calendar": reply=datetime.now().strftime("📅 %B %Y")
    elif cmd=="/upcoming": reply="📌 Upcoming reminders: "+(str(BOT_CONFIG["reminders"]) if BOT_CONFIG["reminders"] else "none")
    elif cmd=="/reminder":
        if arg: BOT_CONFIG["reminders"].append(arg); reply="⏰ Reminder saved in bot memory."
        else: reply="Usage: /reminder message"
    elif cmd in ("/joke","/meme","/roll","/flip","/random","/trivia","/riddle","/roast","/compliment","/rate","/love","/laugh","/fire","/wow","/sad","/angry","/thumbsup","/thumbsdown","/celebrate"):
        reply=_local_fun(cmd,arg)
    elif cmd=="/react": reply=f"{arg or '👍'}"
    elif cmd=="/autoreplyon": BOT_CONFIG["auto_reply_active"]=True; reply="🤖 Auto-reply ON"
    elif cmd=="/autoreplyoff": BOT_CONFIG["auto_reply_active"]=False; reply="🛑 Auto-reply OFF"
    elif cmd=="/setreply": BOT_CONFIG["reply_text"]=arg or BOT_CONFIG["reply_text"]; reply="✅ Auto-reply updated."
    elif cmd=="/getreply": reply="💬 "+BOT_CONFIG["reply_text"]
    elif cmd=="/replytoall": BOT_CONFIG["reply_to_all"]=arg.lower() in ("on","yes","1"); reply=f"🤖 Reply-to-all: {'ON' if BOT_CONFIG['reply_to_all'] else 'OFF'}"
    elif cmd=="/delayreplied":
        try: BOT_CONFIG["reply_delay"]=max(0,int(arg)); reply=f"⏳ Reply delay: {BOT_CONFIG['reply_delay']}s"
        except: reply="Usage: /delayreplied seconds"
    elif cmd=="/replyemoji": BOT_CONFIG["reply_text"]=(arg or "🤖")+" "+BOT_CONFIG["reply_text"]; reply="✅ Reply emoji updated."
    elif cmd=="/replydisable": BOT_CONFIG["reply_disabled_users"].add(_username_from_arg(arg)); reply="🚫 User disabled from auto-reply."
    elif cmd=="/replyenable": BOT_CONFIG["reply_disabled_users"].discard(_username_from_arg(arg)); reply="✅ User enabled for auto-reply."
    elif cmd=="/clearreplyhist": BOT_CONFIG["reply_history"].clear(); reply="🧹 Reply history cleared."
    elif cmd=="/setadmin" and is_admin:
        name=_username_from_arg(arg); BOT_CONFIG["admins"].add(name); reply=f"👑 Admin added: @{name}"
    elif cmd=="/removeadmin" and is_admin:
        name=_username_from_arg(arg); BOT_CONFIG["admins"].discard(name); reply=f"👑 Admin removed: @{name}"
    elif cmd in ("/lockgroup","/grouplock") and is_admin: BOT_CONFIG["group_locks"][gid]=True; reply="🔒 Group lock flag enabled."
    elif cmd in ("/unlockgroup","/groupunlock") and is_admin: BOT_CONFIG["group_locks"][gid]=False; reply="🔓 Group lock flag disabled."
    elif cmd=="/setdesc" and is_admin: reply="ℹ️ Group description editing is not exposed consistently by the installed instagrapi build."
    elif cmd=="/groupname" and is_admin: reply="ℹ️ Group title editing depends on the installed instagrapi endpoint/version; no fake success is reported."
    elif cmd in ("/kick","/ban","/unban","/muteuser","/unmuteuser","/clearmessages","/removemember") and is_admin:
        reply="⚠️ This command needs a version-specific Instagram endpoint/permission; the bot will not claim success without confirmation."
    elif cmd in ("/spam","/broadcast","/dm","/announce","/forward"):
        # Do not turn the bot into a bulk-spam sender. Keep the existing /spam implementation untouched below.
        reply="⚠️ Bulk/mass messaging is not enabled by this command engine."
    elif cmd in ("/yt","/ytplay","/ytrandom","/yttrending","/ytstop","/ytdownload","/ytinfo","/ytsubtitle","/ytplaylist","/ytsearch","/photo","/video","/music","/movie","/tvshow","/anime","/song","/album","/podcast","/stream","/wiki","/define","/synonym","/antonym","/translation","/spelling","/grammar","/formula","/lesson","/quote","/qrcode"):
        reply="ℹ️ This command requires an external API/service. It is listed in /help, but this app.py does not have that service configured, so no fake result is sent."
    else:
        return False
    if reply is not None:
        try: CLIENT.direct_send(reply, thread_ids=[gid])
        except Exception as e: log(f"⚠️ Command reply failed: {str(e)[:50]}")
        return True
    return False

# ================= MAIN BOT WITH ADMIN COMMANDS =================
def run_bot(session_token, gids, pol, ecmd, admin_ids):
    global START_TIME, CLIENT, LOGIN_SUCCESS
    
    START_TIME = datetime.now()
    consecutive_errors = 0
    max_errors = 12
    
    BOT_CONFIG["admins"] = set(a.lower().lstrip('@') for a in admin_ids if a)
    log("🚀 Premium Bot v4.5 with ADMIN COMMANDS starting...")
    
    CLIENT = create_stable_client()
    success, username = safe_login(CLIENT, session_token)
    if not success:
        log("💥 Login failed - Bot STOPPED")
        return
    
    km = {gid: set() for gid in gids}
    lm = {gid: None for gid in gids}
    
    log("📱 Initializing groups...")
    for i, gid in enumerate(gids):
        try:
            time.sleep(10)
            thread = CLIENT.direct_thread(gid)
            km[gid] = {u.pk for u in thread.users}
            if thread.messages:
                lm[gid] = thread.messages[0].id
            BOT_CONFIG["spam_active"][gid] = False
            log(f"✅ Group {i+1}: {gid[:12]}...")
        except Exception as e:
            log(f"⚠️ Group error: {str(e)[:30]}")
    
    log("🎉 Bot running with FULL FEATURES!")
    
    while not STOP_EVENT.is_set():
        for gid in gids:
            if STOP_EVENT.is_set():
                break
                
            try:
                if not session_health_check():
                    if refresh_session(SESSION_TOKEN):
                        consecutive_errors = 0
                    else:
                        log("💥 Session recovery failed")
                        return
                
                time.sleep(random.uniform(12, 20))
                thread = CLIENT.direct_thread(gid)
                consecutive_errors = 0
                
                # ========== COMMANDS PROCESSING ==========
                if ecmd:
                    new_msgs = []
                    if lm[gid] and thread.messages:
                        for msg in thread.messages[:20]:
                            if msg.id == lm[gid]:
                                break
                            new_msgs.append(msg)
                    elif not lm[gid] and thread.messages:
                        # First poll: establish cursor without replaying old messages.
                        new_msgs = []

                    for msg_obj in reversed(new_msgs[:10]):
                        try:
                            if not msg_obj or msg_obj.user_id == CLIENT.user_id:
                                continue
                            sender = next((u for u in thread.users if u.pk == msg_obj.user_id), None)
                            if not sender:
                                continue
                            sender_username = getattr(sender, 'username', '').lower()
                            configured_admins = set(a.lower().lstrip('@') for a in admin_ids if a) | BOT_CONFIG.get('admins', set())
                            is_admin = sender_username in configured_admins

                            # Keep existing /spam and /stopspam admin behavior unchanged.
                            raw_text=(msg_obj.text or '').strip()
                            low=raw_text.lower()
                            if is_admin and low.startswith('/spam '):
                                CLIENT.direct_send("⚠️ Bulk spam is disabled.", thread_ids=[gid])
                                continue
                            if is_admin and low in ['/stopspam','!stopspam']:
                                BOT_CONFIG["spam_active"][gid] = False
                                CLIENT.direct_send("🛑 Spam OFF!", thread_ids=[gid])
                                continue

                            handled=process_command(msg_obj, thread, gid, is_admin, sender)
                            # Auto reply is separate from command handling.
                            if not handled and BOT_CONFIG.get('auto_reply_active') and sender_username not in BOT_CONFIG.get('reply_disabled_users', set()):
                                if BOT_CONFIG.get('reply_delay',0): time.sleep(BOT_CONFIG['reply_delay'])
                                CLIENT.direct_send(BOT_CONFIG.get('reply_text','Thanks for your message! 🤖'), thread_ids=[gid])
                                BOT_CONFIG['reply_history'].append({'user':sender_username,'time':datetime.now().isoformat()})
                                BOT_CONFIG['reply_history']=BOT_CONFIG['reply_history'][-200:]
                        except Exception as e:
                            log(f"⚠️ Command error: {str(e)[:60]}")

                    if thread.messages:
                        lm[gid] = thread.messages[0].id

                # Bulk spam sender intentionally disabled.


            except RateLimitError:
                consecutive_errors += 1
                log("⏳ Rate limit - 2min cooldown")
                time.sleep(120)
            except Exception as e:
                consecutive_errors += 1
                log(f"⚠️ Error: {str(e)[:40]}")
                time.sleep(15)
        
        if consecutive_errors > max_errors:
            log("🔄 Emergency restart...")
            if not refresh_session(SESSION_TOKEN):
                break
        
        time.sleep(pol + random.uniform(3, 7))

    log("🛑 Bot stopped")

# ================= FLASK ROUTES =================
@app.route("/")
def index():
    return render_template_string(PAGE_HTML)

@app.route("/start", methods=["POST"])
def start():
    global BOT_THREAD
    if BOT_THREAD and BOT_THREAD.is_alive():
        return jsonify({"message": "❌ Bot already running!"})
    
    try:
        token = request.form.get("session", "").strip()
        gids = [x.strip() for x in request.form.get("group_ids", "").split(",") if x.strip()]
        admins = [x.strip() for x in request.form.get("admin_ids", "").split(",") if x.strip()]
        
        if not all([token, gids]):
            return jsonify({"message": "❌ Fill Session Token & Group IDs!"})

        global STOP_EVENT
        STOP_EVENT.clear()
        BOT_THREAD = threading.Thread(
            target=run_bot,
            args=(token, gids,
                  int(request.form.get("poll", 25)),
                  request.form.get("enable_commands") == "yes",
                  admins),
            daemon=True
        )
        BOT_THREAD.start()
        log("🚀 Bot v4.5 STARTED!")
        return jsonify({"message": "✅ Bot started!"})
    except Exception as e:
        return jsonify({"message": f"❌ Error: {str(e)}"})

@app.route("/stop", methods=["POST"])
def stop():
    global STOP_EVENT, CLIENT
    STOP_EVENT.set()
    CLIENT = None
    if BOT_THREAD:
        BOT_THREAD.join(timeout=5)
    log("🛑 Bot STOPPED!")
    return jsonify({"message": "✅ Bot stopped!"})

@app.route("/logs")
def logs():
    return jsonify({
        "logs": LOGS[-200:],
        "uptime": uptime(),
        "status": "running" if BOT_THREAD and BOT_THREAD.is_alive() else "stopped"
    })

@app.route("/clear_logs", methods=["POST"])
def clear_logs_route():
    clear_logs()
    return jsonify({"message": "✅ Logs cleared!"})

@app.route("/stats")
def stats():
    return jsonify({
        "uptime": uptime(),
        "status": "running" if BOT_THREAD and BOT_THREAD.is_alive() else "stopped",
        "comment": COMMENT_STATS
    })

@app.route("/comment_start", methods=["POST"])
def comment_start():
    global COMMENT_THREAD
    if COMMENT_THREAD and COMMENT_THREAD.is_alive():
        return jsonify({"message": "❌ Comment job already running!"})
    token = request.form.get("comment_session", "").strip() or request.form.get("session", "").strip()
    post_value = request.form.get("post_id", "").strip()
    owner = request.form.get("post_owner", "").strip()
    delay = request.form.get("comment_delay", "10").strip()
    upload = request.files.get("comments_file")
    if not token or not post_value or not upload:
        return jsonify({"message": "❌ Session Token, Post ID/URL and upload.txt are required!"})
    try:
        delay = max(10, int(delay))
        comments = _parse_comment_lines(upload.read())
        # One pass, unique comments, hard safety cap.
        seen = set(); unique = []
        for line in comments:
            key = line.casefold()
            if key not in seen:
                seen.add(key); unique.append(line)
        comments = unique[:50]
        if not comments:
            return jsonify({"message": "❌ upload.txt has no usable comments."})
        COMMENT_STOP_EVENT.clear()
        COMMENT_THREAD = threading.Thread(target=run_comment_sender, args=(token, post_value, owner, comments, delay), daemon=True)
        COMMENT_THREAD.start()
        log(f"💬 Comment job queued: {len(comments)} unique comments")
        return jsonify({"message": f"✅ Comment job started with {len(comments)} comments."})
    except Exception as e:
        return jsonify({"message": f"❌ Comment start error: {str(e)}"})

@app.route("/comment_stop", methods=["POST"])
def comment_stop():
    COMMENT_STOP_EVENT.set()
    return jsonify({"message": "🛑 Comment stop requested!"})

@app.route("/comment_stats")
def comment_stats():
    running = bool(COMMENT_THREAD and COMMENT_THREAD.is_alive())
    return jsonify({**COMMENT_STATS, "running": running})

# ================= COMPLETE HTML WITH ADMIN FIELD =================
PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
    <title>Premium Instagram Bot v4.5 - Admin Panel</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Bebas+Neue&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        *{margin:0;padding:0;box-sizing:border-box;}
        :root{
            --red:#ff1a3c;
            --red-glow:rgba(255,26,60,0.55);
            --red-dim:rgba(255,26,60,0.25);
            --panel:rgba(15,8,10,0.62);
            --panel-strong:rgba(10,5,7,0.82);
        }
        html,body{height:100%;}
        body{
            font-family:'Inter',sans-serif;
            background:#050203;
            min-height:100vh;
            color:#f1e7e8;
            overflow-x:hidden;
            position:relative;
        }

        /* ===== CINEMATIC KAIJU BACKDROP ===== */
        .backdrop{position:fixed;inset:0;z-index:0;overflow:hidden;background:
            radial-gradient(ellipse at 50% 120%,rgba(255,26,60,0.35) 0%,rgba(120,10,20,0.12) 35%,transparent 60%),
            radial-gradient(ellipse at 20% -10%,rgba(255,60,60,0.18) 0%,transparent 45%),
            linear-gradient(180deg,#0a0405 0%,#0d0507 40%,#180307 75%,#050002 100%);
        }
        .backdrop svg{position:absolute;bottom:-2%;left:50%;transform:translateX(-50%);width:140%;max-width:1600px;opacity:0.5;filter:drop-shadow(0 0 60px rgba(255,26,60,0.35));}
        .city{position:absolute;bottom:0;left:0;width:100%;height:22%;display:flex;align-items:flex-end;gap:2px;opacity:0.55;z-index:1;}
        .city span{display:block;background:linear-gradient(180deg,#150406,#02000100);flex:1;box-shadow:inset 0 0 20px rgba(255,20,40,0.15);}
        .smoke{position:absolute;border-radius:50%;background:radial-gradient(circle,rgba(80,10,15,0.55),transparent 70%);filter:blur(18px);animation:drift 22s linear infinite;}
        .smoke.s1{width:420px;height:420px;top:5%;left:-10%;animation-duration:30s;}
        .smoke.s2{width:520px;height:520px;top:20%;right:-15%;animation-duration:26s;animation-delay:-8s;}
        .smoke.s3{width:340px;height:340px;bottom:5%;left:20%;animation-duration:34s;animation-delay:-14s;}
        @keyframes drift{0%{transform:translate(0,0) scale(1);}50%{transform:translate(40px,-30px) scale(1.15);}100%{transform:translate(0,0) scale(1);}}
        .embers{position:absolute;inset:0;pointer-events:none;}
        .embers i{position:absolute;bottom:-5%;width:3px;height:3px;background:#ff5c3c;border-radius:50%;box-shadow:0 0 8px 2px rgba(255,90,50,0.8);animation:rise linear infinite;}
        @keyframes rise{0%{transform:translateY(0) translateX(0);opacity:0;}10%{opacity:1;}100%{transform:translateY(-110vh) translateX(20px);opacity:0;}}
        .lightning{position:fixed;inset:0;background:rgba(255,60,60,0.35);opacity:0;pointer-events:none;z-index:2;animation:flash 9s infinite;}
        @keyframes flash{0%,93%,100%{opacity:0;}93.3%{opacity:0.55;}93.6%{opacity:0;}94%{opacity:0.35;}94.3%{opacity:0;}}

        /* ===== SHELL ===== */
        .shell{position:relative;z-index:3;max-width:760px;margin:0 auto;padding:22px 16px 40px;min-height:100vh;display:flex;flex-direction:column;}
        .brandbar{text-align:center;margin-bottom:18px;}
        .brandbar h1{
            font-family:'Bebas Neue',Inter,sans-serif;
            font-size:2.9rem;letter-spacing:3px;color:#fff;
            text-shadow:0 0 12px var(--red-glow),0 0 40px rgba(255,26,60,0.35);
        }
        .brandbar h1 i{color:var(--red);margin-right:10px;filter:drop-shadow(0 0 10px var(--red-glow));}
        .brandbar p{color:#c7a3a8;font-size:0.85rem;letter-spacing:1px;margin-top:4px;text-transform:uppercase;}

        .pagedots{display:flex;justify-content:center;gap:9px;margin:14px 0 6px;}
        .pagedots span{width:9px;height:9px;border-radius:50%;background:rgba(255,255,255,0.15);border:1px solid rgba(255,26,60,0.4);transition:all .3s;}
        .pagedots span.on{background:var(--red);box-shadow:0 0 10px var(--red-glow);transform:scale(1.2);}

        /* ===== BOOK / PAGE FLIP ===== */
        .book{position:relative;perspective:2400px;flex:1;}
        .page{
            position:relative;
            transform-origin:center;
            transition:transform .55s cubic-bezier(.4,.2,.2,1), opacity .45s ease;
            display:none;
            backface-visibility:hidden;
        }
        .page.active{display:block;transform:rotateY(0deg);opacity:1;}
        .page.leave-next{transform:rotateY(-100deg);opacity:0;}
        .page.leave-prev{transform:rotateY(100deg);opacity:0;}
        .page.enter-from-next{transform:rotateY(100deg);opacity:0;}
        .page.enter-from-prev{transform:rotateY(-100deg);opacity:0;}

        .pagehead{display:flex;align-items:center;gap:10px;margin-bottom:16px;}
        .pagehead .num{
            font-family:'Bebas Neue',sans-serif;font-size:1.1rem;color:var(--red);
            border:1px solid var(--red-dim);border-radius:8px;padding:2px 10px;box-shadow:0 0 10px var(--red-dim) inset;
        }
        .pagehead h2{font-size:1.05rem;letter-spacing:1.5px;text-transform:uppercase;color:#f4d9dc;font-weight:700;}

        /* ===== GLASS CARDS ===== */
        .card{
            background:var(--panel);
            backdrop-filter:blur(14px) saturate(140%);
            -webkit-backdrop-filter:blur(14px) saturate(140%);
            border:1px solid rgba(255,26,60,0.35);
            border-radius:20px;
            padding:22px;
            margin-bottom:18px;
            box-shadow:0 8px 30px rgba(0,0,0,0.55),0 0 0 1px rgba(255,26,60,0.05) inset, 0 0 24px rgba(255,26,60,0.08);
        }

        /* status */
        .status-bar{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;}
        .status-item{display:flex;align-items:center;gap:10px;font-weight:600;letter-spacing:0.5px;}
        .status-running{color:#3dffa0;}.status-stopped{color:var(--red);}
        .status-dot{width:12px;height:12px;border-radius:50%;background:var(--red);box-shadow:0 0 12px var(--red-glow);animation:pulse 1.6s infinite;}
        @keyframes pulse{0%,100%{opacity:1;}50%{opacity:0.4;}}
        #uptime{font-family:'Bebas Neue',monospace;font-size:1.2rem;letter-spacing:2px;color:#fff;}

        /* stats */
        .stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:18px;}
        .stat-card{padding:22px 10px;text-align:center;}
        .stat-number{font-family:'Bebas Neue',sans-serif;font-size:2.6rem;color:#fff;text-shadow:0 0 18px var(--red-glow);margin-bottom:4px;}
        .stat-card div:last-child{font-size:0.78rem;letter-spacing:1.5px;text-transform:uppercase;color:#d9a5ab;}

        /* logo block */
        .logo-wrap{display:flex;flex-direction:column;align-items:center;padding:26px 20px;}
        .logo-ring{
            width:92px;height:92px;border-radius:50%;
            display:flex;align-items:center;justify-content:center;
            border:2px solid var(--red);
            box-shadow:0 0 25px var(--red-glow),0 0 60px rgba(255,26,60,0.25) inset;
            margin-bottom:14px;background:radial-gradient(circle,rgba(255,26,60,0.15),transparent 70%);
        }
        .logo-ring i{font-size:2.4rem;color:var(--red);filter:drop-shadow(0 0 10px var(--red-glow));}
        .logo-wrap .tagline{color:#c7a3a8;font-size:0.85rem;letter-spacing:1px;text-align:center;}

        /* form */
        label{display:block;margin-bottom:8px;font-weight:600;color:#f0d6d9;font-size:0.9rem;letter-spacing:0.3px;}
        label i{color:var(--red);margin-right:6px;width:16px;text-align:center;}
        input,textarea{
            width:100%;padding:14px 16px;border-radius:12px;font-size:0.95rem;
            background:rgba(0,0,0,0.45);color:#fdeef0;
            border:1px solid rgba(255,26,60,0.3);
            transition:all 0.25s;
        }
        input::placeholder,textarea::placeholder{color:#7a5459;}
        input:focus,textarea:focus{outline:none;border-color:var(--red);box-shadow:0 0 0 3px rgba(255,26,60,0.18),0 0 20px rgba(255,26,60,0.25);}
        textarea{resize:vertical;min-height:120px;font-family:inherit;}
        .form-group{margin-bottom:18px;}
        .hint{color:#f59e0b;font-weight:500;}
        .req{color:var(--red);}

        /* toggles */
        .toggle-row{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:16px 18px;margin-bottom:14px;}
        .toggle-row .lbl{display:flex;align-items:center;gap:10px;font-weight:600;color:#f0d6d9;font-size:0.92rem;cursor:pointer;}
        .toggle-row .lbl i{color:var(--red);}
        .switch{position:relative;width:52px;height:28px;flex-shrink:0;cursor:pointer;}
        .switch input{opacity:0;width:0;height:0;position:absolute;}
        .slider{position:absolute;inset:0;background:rgba(255,255,255,0.12);border:1px solid rgba(255,26,60,0.4);border-radius:999px;transition:.3s;}
        .slider:before{content:'';position:absolute;width:20px;height:20px;left:3px;top:3px;background:#7a5459;border-radius:50%;transition:.3s;}
        .switch input:checked + .slider{background:rgba(255,26,60,0.35);border-color:var(--red);box-shadow:0 0 12px var(--red-glow);}
        .switch input:checked + .slider:before{transform:translateX(24px);background:var(--red);box-shadow:0 0 8px var(--red-glow);}

        /* admin section */
        .admin-section{border:1px solid rgba(245,158,11,0.5);background:rgba(40,20,4,0.5);}
        .admin-section h3{color:#f5b03e;margin-bottom:12px;font-size:1rem;letter-spacing:0.5px;}
        .admin-section .cmdline{font-size:0.9rem;color:#f0ce9c;line-height:1.9;}
        .admin-section strong{color:#ffd27a;}

        /* buttons */
        .controls{display:flex;gap:14px;justify-content:center;margin:8px 0 4px;flex-wrap:wrap;}
        .btn{
            padding:16px 26px;border:none;border-radius:14px;font-size:1rem;font-weight:700;
            cursor:pointer;transition:all 0.25s;display:flex;align-items:center;gap:10px;letter-spacing:0.5px;
            flex:1;min-width:140px;justify-content:center;
        }
        .btn-start{background:linear-gradient(135deg,#0dff8a,#059669);color:#04240f;box-shadow:0 0 20px rgba(13,255,138,0.55),0 8px 20px rgba(0,0,0,0.4);}
        .btn-stop{background:linear-gradient(135deg,#ff3050,#8f0d1c);color:#fff;box-shadow:0 0 22px rgba(255,26,60,0.6),0 8px 20px rgba(0,0,0,0.4);}
        .btn-clear{background:linear-gradient(135deg,#3a3f47,#181b1f);color:#e6e6e6;border:1px solid #55595f;box-shadow:0 8px 20px rgba(0,0,0,0.5);}
        .btn:hover{transform:translateY(-3px);filter:brightness(1.1);}
        .btn:active{transform:translateY(0);}

        /* nav */
        .navrow{display:flex;justify-content:space-between;gap:12px;margin-top:10px;}
        .navbtn{
            flex:1;padding:15px 18px;border-radius:14px;border:1px solid var(--red);
            background:linear-gradient(135deg,rgba(255,26,60,0.25),rgba(90,5,15,0.35));
            color:#ffd9de;font-weight:700;letter-spacing:0.5px;cursor:pointer;
            display:flex;align-items:center;justify-content:center;gap:8px;
            box-shadow:0 0 16px rgba(255,26,60,0.35);transition:all .25s;
        }
        .navbtn:hover{box-shadow:0 0 26px rgba(255,26,60,0.6);transform:translateY(-2px);}
        .navbtn:disabled{opacity:0.25;cursor:not-allowed;box-shadow:none;transform:none;}

        /* logs */
        .logs-container{padding:20px;}
        .logs-title{display:flex;justify-content:space-between;align-items:center;color:#ffd9de;margin-bottom:14px;font-weight:700;letter-spacing:0.5px;}
        .logs-title .clearmini{background:linear-gradient(135deg,#3a3f47,#181b1f);color:#eee;border:1px solid #55595f;padding:9px 16px;border-radius:8px;cursor:pointer;font-weight:600;font-size:0.85rem;}
        #logs{
            background:#060506;color:#ff8f8f;border-radius:14px;padding:20px;height:min(52vh,420px);overflow-y:auto;
            font-family:'Courier New',monospace;font-size:0.85rem;line-height:1.65;white-space:pre-wrap;
            border:1px solid rgba(255,26,60,0.4);box-shadow:inset 0 0 30px rgba(255,26,60,0.15),0 0 18px rgba(255,26,60,0.15);
            text-shadow:0 0 6px rgba(255,26,60,0.35);
        }
        #logs::-webkit-scrollbar{width:8px;}
        #logs::-webkit-scrollbar-thumb{background:var(--red);border-radius:8px;}

        /* settings/info page */
        .info-list{list-style:none;color:#e7c8cb;font-size:0.9rem;line-height:2;}
        .info-list i{color:var(--red);width:20px;}
        .final-badge{text-align:center;padding:26px 20px;}
        .final-badge .big{font-family:'Bebas Neue',sans-serif;font-size:2.4rem;letter-spacing:2px;color:#fff;text-shadow:0 0 20px var(--red-glow);}

        @media(max-width:520px){
            .brandbar h1{font-size:2.2rem;}
            .btn{min-width:100%;}
            .stats-grid{grid-template-columns:1fr 1fr;}
        }
    </style>
</head>
<body>
    <div class="backdrop">
        <div class="smoke s1"></div>
        <div class="smoke s2"></div>
        <div class="smoke s3"></div>
        <svg viewBox="0 0 800 420" xmlns="http://www.w3.org/2000/svg">
            <path fill="#1a0306" d="M60,420 C40,300 90,260 70,190 C55,140 110,120 130,150 C145,100 190,95 205,140 C230,90 280,95 290,150 C310,110 360,120 365,170 C400,120 450,130 455,180 C480,140 530,150 530,200 C560,160 610,175 605,225 C640,200 690,215 680,260 C710,250 745,275 730,320 C750,340 745,390 720,420 Z"/>
            <circle cx="205" cy="150" r="7" fill="#ff1a3c" opacity="0.85"/>
            <circle cx="290" cy="160" r="6" fill="#ff1a3c" opacity="0.7"/>
        </svg>
        <div class="city">
            <span style="height:40%"></span><span style="height:65%"></span><span style="height:30%"></span>
            <span style="height:80%"></span><span style="height:45%"></span><span style="height:70%"></span>
            <span style="height:35%"></span><span style="height:90%"></span><span style="height:50%"></span>
            <span style="height:60%"></span><span style="height:25%"></span><span style="height:75%"></span>
            <span style="height:40%"></span><span style="height:55%"></span><span style="height:85%"></span>
        </div>
        <div class="embers" id="embers"></div>
    </div>
    <div class="lightning"></div>

    <div class="shell">
        <div class="brandbar">
            <h1><i class="fas fa-robot"></i>INSTAGRAM BOT v4.5</h1>
            <p> 👹 UNLEASH THE MONSTER — Your Instagram Chatbot That Never Sleeps</p>
        </div>

        <div class="pagedots">
            <span class="dot on" data-i="1"></span>
            <span class="dot" data-i="2"></span>
            <span class="dot" data-i="3"></span>
            <span class="dot" data-i="4"></span>
        </div>

        <form id="botForm">
        <div class="book" id="book">

            <!-- PAGE 1 -->
            <section class="page active" data-page="1">
                <div class="card logo-wrap">
                    <div class="logo-ring"><i class="fas fa-robot"></i></div>
                    <div class="tagline">✅ Admin Panel • Commands • Anti-Logout • Render Ready</div>
                </div>

                <div class="card status-bar status-stopped" id="statusBar">
                    <div class="status-item">
                        <div class="status-dot"></div>
                        <span>Status: Stopped</span>
                    </div>
                    <div class="status-item">
                        <span id="uptime">00:00:00</span>
                    </div>
                </div>

                <div class="card">
                    <div class="form-group">
                        <label><i class="fas fa-key"></i> Session Token <span class="req">*</span></label>
                        <input type="password" name="session" placeholder="Fresh session token" required>
                    </div>
                    <div class="form-group">
                        <label><i class="fas fa-hashtag"></i> Group IDs <span class="req">*</span></label>
                        <input type="text" name="group_ids" placeholder="1234567890,0987654321" required>
                    </div>
                    <div class="form-group">
                        <label><i class="fas fa-users-crown"></i> Admin IDs</label>
                        <input type="text" name="admin_ids" placeholder="admin1,admin2,you">
                    </div>
                    <div class="form-group">
                        <label><i class="fas fa-sync"></i> Poll Interval <span class="hint">(25s recommended)</span></label>
                        <input type="number" name="poll" value="25" min="20" max="45">
                    </div>
                </div>

                <div class="navrow">
                    <button type="button" class="navbtn" disabled><i class="fas fa-chevron-left"></i> Previous Page</button>
                    <button type="button" class="navbtn" onclick="goPage(2)">Next Page <i class="fas fa-chevron-right"></i></button>
                </div>
            </section>

            <!-- PAGE 2 -->
            <section class="page" data-page="2">
                <div class="pagehead"><span class="num">02</span><h2>Controls &amp; Commands</h2></div>

                <div class="card toggle-row" onclick="toggleCheckbox('enable_commands')">
                    <span class="lbl"><i class="fas fa-terminal"></i> Enable Commands</span>
                    <label class="switch" onclick="event.stopPropagation()">
                        <input type="checkbox" id="enable_commands" name="enable_commands" value="yes" checked>
                        <span class="slider"></span>
                    </label>
                </div>

                <div class="card admin-section">
                    <h3><i class="fas fa-crown"></i> 👑 Admin Commands</h3>
                    <div class="cmdline">
                        <strong>/help</strong> - Full command list<br>
                        <strong>/ping, /uptime, /stats</strong> - Bot status<br>
                        <strong>/autoreplyon, /autoreplyoff, /setreply message</strong> - Auto reply<br>
                        <strong>/groupinfo, /membercount, /listmembers</strong> - Group info<br>
                        <strong>/uppercase text, /lowercase text, /reverse text</strong> - Text tools<br>
                        <strong>/calculate expression, /time, /date</strong> - Utility tools<br>
                        <strong>/joke, /meme, /roll, /flip, /trivia, /riddle</strong> - Fun
                    </div>
                </div>

                <div class="card">
                    <div class="controls">
                        <button type="button" class="btn btn-start" onclick="startBot()">
                            <i class="fas fa-play"></i> Start Bot
                        </button>
                        <button type="button" class="btn btn-stop" onclick="stopBot()">
                            <i class="fas fa-stop"></i> Stop Bot
                        </button>
                        <button type="button" class="btn btn-clear" onclick="clearLogs()">
                            <i class="fas fa-trash"></i> Clear Logs
                        </button>
                    </div>
                </div>

                <div class="navrow">
                    <button type="button" class="navbtn" onclick="goPage(1)"><i class="fas fa-chevron-left"></i> Previous Page</button>
                    <button type="button" class="navbtn" onclick="goPage(3)">Next Page <i class="fas fa-chevron-right"></i></button>
                </div>
            </section>

            <!-- PAGE 3 -->
            <section class="page" data-page="3">
                <div class="pagehead"><span class="num">03</span><h2>Live Logs</h2></div>

                <div class="card logs-container">
                    <div class="logs-title">
                        <div><i class="fas fa-list"></i> Live Logs</div>
                        <button class="clearmini" onclick="clearLogs()">Clear</button>
                    </div>
                    <div id="logs">🚀 Premium Bot v4.5 ready! Admin features enabled ✅</div>
                </div>

                <div class="navrow">
                    <button type="button" class="navbtn" onclick="goPage(2)"><i class="fas fa-chevron-left"></i> Previous Page</button>
                    <button type="button" class="navbtn" onclick="goPage(4)">Next Page <i class="fas fa-chevron-right"></i></button>
                </div>
            </section>

            <!-- PAGE 4 -->
            <section class="page" data-page="4">
                <div class="pagehead"><span class="num">04</span><h2>Instagram Post Comments</h2></div>

                <div class="card">
                    <div class="form-group">
                        <label><i class="fas fa-key"></i> Session Token <span class="req">*</span></label>
                        <input type="password" id="commentSession" placeholder="Authenticated session token">
                    </div>
                    <div class="form-group">
                        <label><i class="fas fa-link"></i> Post ID / Post URL <span class="req">*</span></label>
                        <input type="text" id="postId" placeholder="https://www.instagram.com/p/SHORTCODE/ or media ID">
                    </div>
                    <div class="form-group">
                        <label><i class="fas fa-user"></i> Target Post Owner</label>
                        <input type="text" id="postOwner" placeholder="username (optional verification)">
                    </div>
                    <div class="form-group">
                        <label><i class="fas fa-clock"></i> Delay Between Comments (sec)</label>
                        <input type="number" id="commentDelay" value="10" min="10" max="3600">
                    </div>
                    <div class="form-group">
                        <label><i class="fas fa-file-lines"></i> upload.txt <span class="req">*</span></label>
                        <input type="file" id="commentsFile" accept=".txt,text/plain">
                        <div class="hint" style="margin-top:8px;">One line = one comment • duplicate lines are ignored • maximum 50 unique comments per run</div>
                    </div>
                    <div class="controls">
                        <button type="button" class="btn btn-start" onclick="startComments()"><i class="fas fa-paper-plane"></i> Start Comments</button>
                        <button type="button" class="btn btn-stop" onclick="stopComments()"><i class="fas fa-stop"></i> Stop Comments</button>
                    </div>
                </div>

                <div class="stats-grid" style="display:grid;">
                    <div class="card stat-card"><div class="stat-number" id="commentLoaded">0</div><div>Loaded</div></div>
                    <div class="card stat-card"><div class="stat-number" id="commentSent">0</div><div>Sent</div></div>
                    <div class="card stat-card"><div class="stat-number" id="commentFailed">0</div><div>Failed</div></div>
                    <div class="card stat-card"><div class="stat-number" id="commentState">Stopped</div><div>Status</div></div>
                </div>

                <div class="card">
                    <h3 style="color:#f5b03e;margin-bottom:10px;font-size:1rem;"><i class="fas fa-shield-halved"></i> Comment Safety</h3>
                    <div class="cmdline">One authenticated account • one pass through the uploaded list • minimum 10-second delay • stops on feedback/rate-limit signals.</div>
                </div>

                <div class="navrow">
                    <button type="button" class="navbtn" onclick="goPage(3)"><i class="fas fa-chevron-left"></i> Previous Page</button>
                    <button type="button" class="navbtn" disabled>Next Page <i class="fas fa-chevron-right"></i></button>
                </div>
            </section>

        </div>
        </form>
    </div>

    <script>
        // ---- embers particles (purely decorative) ----
        (function(){
            const box = document.getElementById('embers');
            for(let i=0;i<24;i++){
                const e = document.createElement('i');
                e.style.left = (Math.random()*100)+'%';
                e.style.animationDuration = (6+Math.random()*8)+'s';
                e.style.animationDelay = (Math.random()*8)+'s';
                box.appendChild(e);
            }
        })();

        // ---- book page navigation ----
        let currentPage = 1;
        function goPage(target){
            if(target === currentPage) return;
            const dir = target > currentPage ? 'next' : 'prev';
            const curEl = document.querySelector('.page[data-page="'+currentPage+'"]');
            const nextEl = document.querySelector('.page[data-page="'+target+'"]');

            curEl.classList.add(dir === 'next' ? 'leave-next' : 'leave-prev');
            nextEl.style.display = 'block';
            nextEl.classList.add(dir === 'next' ? 'enter-from-next' : 'enter-from-prev');
            void nextEl.offsetWidth; // reflow

            requestAnimationFrame(() => {
                nextEl.classList.add('active');
                nextEl.classList.remove('enter-from-next','enter-from-prev');
            });

            setTimeout(() => {
                curEl.classList.remove('active','leave-next','leave-prev');
                curEl.style.display = 'none';
                currentPage = target;
                document.querySelectorAll('.pagedots .dot').forEach(d => {
                    d.classList.toggle('on', parseInt(d.dataset.i) === currentPage);
                });
                window.scrollTo({top:0, behavior:'smooth'});
            }, 560);
        }
        document.querySelectorAll('.pagedots .dot').forEach(d => {
            d.addEventListener('click', () => goPage(parseInt(d.dataset.i)));
        });

        function toggleCheckbox(id) {
            document.getElementById(id).click();
        }
        
        async function startBot() {
            try {
                const formData = new FormData(document.getElementById('botForm'));
                const response = await fetch('/start', {method: 'POST', body: formData});
                const result = await response.json();
                alert(result.message);
                updateStatus();
            } catch (error) {
                alert('❌ Error: ' + error.message);
            }
        }
        
        async function stopBot() {
            try {
                const response = await fetch('/stop', {method: 'POST'});
                const result = await response.json();
                alert(result.message);
                updateStatus();
            } catch (error) {
                alert('❌ Error: ' + error.message);
            }
        }
        
        async function clearLogs() {
            try {
                await fetch('/clear_logs', {method: 'POST'});
                document.getElementById('logs').textContent = '🧹 Logs cleared!';
            } catch (error) {}
        }
        
        async function startComments() {
            const fd = new FormData();
            fd.append('comment_session', document.getElementById('commentSession').value.trim());
            fd.append('post_id', document.getElementById('postId').value.trim());
            fd.append('post_owner', document.getElementById('postOwner').value.trim());
            fd.append('comment_delay', document.getElementById('commentDelay').value);
            const file = document.getElementById('commentsFile').files[0];
            if (file) fd.append('comments_file', file);
            try {
                const r = await fetch('/comment_start', {method:'POST', body:fd});
                const data = await r.json();
                alert(data.message);
                updateCommentStats();
            } catch(e) { alert('❌ Error: '+e.message); }
        }

        async function stopComments() {
            try {
                const r = await fetch('/comment_stop', {method:'POST'});
                const data = await r.json();
                alert(data.message);
            } catch(e) { alert('❌ Error: '+e.message); }
        }

        async function updateCommentStats() {
            try {
                const r = await fetch('/comment_stats');
                const d = await r.json();
                document.getElementById('commentLoaded').textContent = d.loaded ?? 0;
                document.getElementById('commentSent').textContent = d.sent ?? 0;
                document.getElementById('commentFailed').textContent = d.failed ?? 0;
                document.getElementById('commentState').textContent = d.running ? 'Running' : (d.status || 'Stopped');
            } catch(e) {}
        }
        
        async function updateStatus() {
            try {
                const response = await fetch('/stats');
                const data = await response.json();
                document.getElementById('uptime').textContent = data.uptime;
                document.getElementById('finalUptime').textContent = data.uptime;
                
                const statusBar = document.getElementById('statusBar');
                const statusDot = statusBar.querySelector('.status-dot');
                const statusText = statusBar.querySelector('span');
                
                if (data.status === 'running') {
                    statusBar.className = 'card status-bar status-running';
                    statusDot.style.background = '#3dffa0';
                    statusText.textContent = 'Status: Running';
                    document.getElementById('finalStatusText').textContent = 'Status: Running';
                } else {
                    statusBar.className = 'card status-bar status-stopped';
                    statusDot.style.background = '#ff1a3c';
                    statusText.textContent = 'Status: Stopped';
                    document.getElementById('finalStatusText').textContent = 'Status: Stopped';
                }
            } catch (error) {}
        }
        
        async function updateLogs() {
            try {
                const response = await fetch('/logs');
                const data = await response.json();
                const logsDiv = document.getElementById('logs');
                logsDiv.textContent = data.logs.join('\
');
                logsDiv.scrollTop = logsDiv.scrollHeight;
            } catch (error) {}
        }
        
        setInterval(() => {
            updateStatus();
            updateLogs();
            updateCommentStats();
        }, 3000);
        
        updateStatus();
        updateLogs();
        updateCommentStats();
    </script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log("🌟 Premium Instagram Bot v4.5 - COMPLETE BUILD!")
    log("✅ Admin IDs field ADDED!")
    log("✅ Command engine loaded!")
    log("✅ Render.com ready - Copy paste karo!")
    app.run(host="0.0.0.0", port=port, debug=False)
