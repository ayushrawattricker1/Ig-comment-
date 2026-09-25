import os
import re
import threading
import time
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify
from instagrapi import Client
from instagrapi.exceptions import TwoFactorRequired, ChallengeRequired, BadPassword

app = Flask(__name__)

COMMENT_THREAD = None
COMMENT_STOP_EVENT = threading.Event()
COMMENT_START_TIME = 0
COMMENT_STATS = {"sent": 0, "failed": 0, "loaded": 0, "sender": ""}

LOGS = []
SESSION_TOKEN = ""

def log(msg):
    timestamp = datetime.now().strftime('%H:%M:%S')
    full_msg = f"[{timestamp}] {msg}"
    LOGS.append(full_msg)
    print(full_msg)

def login_client_token(token):
    cl = Client()
    try:
        if token:
            cl.login_by_sessionid(token)
            log("✅ Token login successful!")
            user = cl.account_info()
            log(f"👤 @{user.username} ready!")
            return cl
    except Exception as e:
        log(f"⚠️ Token error: {e}")
    return None

def _parse_comment_lines(raw_bytes):
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]

def _resolve_post_media(cl, post_id):
    value = (post_id or "").strip()
    if not value:
        raise ValueError("Post ID / URL required")

    # Numeric media PK
    if value.isdigit():
        return cl.media_info(int(value))

    # Instagram post/reel/IGTV URL or shortcode.
    # Query strings such as ?utm_source=... must not become part of the shortcode.
    match = re.search(r"/(?:p|reel|tv)/([^/?#]+)/?", value, re.IGNORECASE)
    if match:
        shortcode = match.group(1).strip()
    else:
        # Allow a bare shortcode, but reject a full URL that does not contain
        # a supported Instagram media path.
        if "://" in value:
            raise ValueError("Unsupported Instagram URL. Use /p/, /reel/, or /tv/ URL.")
        shortcode = value.strip().strip("/").split("?")[0].split("#")[0]

    if not shortcode:
        raise ValueError("Invalid post ID / URL")

    media_pk = cl.media_pk_from_code(shortcode)
    return cl.media_info(media_pk)

def run_comment_sender(session_token, post_id, owner_name, comments, delay):
    global COMMENT_STATS

    cl = login_client_token(session_token)
    if not cl:
        log("🛑 Comment login failed!")
        return

    try:
        sender = cl.account_info().username
        COMMENT_STATS["sender"] = sender
        media = _resolve_post_media(cl, post_id)
        actual_owner = getattr(getattr(media, "user", None), "username", "") or ""

        if owner_name and actual_owner.lower() != owner_name.lstrip("@").strip().lower():
            log(f"⚠️ Owner mismatch: post belongs to @{actual_owner}, not @{owner_name.lstrip('@').strip()}")
            return

        log(f"🎯 Post verified: @{actual_owner or 'unknown'}")
        log(f"📦 Loaded {len(comments)} unique comment(s)")
        log(f"👤 Sending as authenticated account: @{sender}")

        for index, comment in enumerate(comments, 1):
            if COMMENT_STOP_EVENT.is_set():
                log("🛑 Comment sender stopped by user.")
                break

            try:
                cl.media_comment(media.pk, comment)
                COMMENT_STATS["sent"] += 1
                log(f"💬 [{index}/{len(comments)}] Sent: {comment[:80]}")
            except Exception as e:
                COMMENT_STATS["failed"] += 1
                log(f"⚠️ [{index}/{len(comments)}] Failed: {e}")
                if "feedback_required" in str(e).lower() or "rate" in str(e).lower():
                    log("🛑 Instagram rate/feedback limit detected; stopping safely.")
                    break

            if index < len(comments):
                wait_for = max(10, int(delay))
                log(f"⏳ Waiting {wait_for}s before next comment...")
                for _ in range(wait_for):
                    if COMMENT_STOP_EVENT.is_set():
                        break
                    time.sleep(1)

    except Exception as e:
        log(f"⚠️ Comment worker error: {e}")
    finally:
        log(f"🏁 Comment run finished. Sent: {COMMENT_STATS['sent']}, Failed: {COMMENT_STATS['failed']}")

@app.route("/")
def index():
    return render_template_string(PAGE_HTML)

@app.route("/set_token", methods=["POST"])
def set_token():
    global SESSION_TOKEN
    token = request.form.get("token", "").strip()
    if token:
        SESSION_TOKEN = token
        log("🔑 Session token activated.")
        return jsonify({"message": "✅ Token ready!"})
    return jsonify({"error": "Empty token!"})

@app.route("/comment_start", methods=["POST"])
def comment_start():
    global COMMENT_THREAD, COMMENT_STOP_EVENT, COMMENT_START_TIME, COMMENT_STATS

    if COMMENT_THREAD and COMMENT_THREAD.is_alive():
        return jsonify({"message": "⚙️ Comment sender already running."})

    token = SESSION_TOKEN
    post_id = request.form.get("post_id", "").strip()
    owner_name = request.form.get("owner_name", "").strip()
    try:
        delay = max(10, min(3600, int(request.form.get("comment_delay", 15))))
    except ValueError:
        delay = 15

    uploaded = request.files.get("comments_file")
    if not token:
        return jsonify({"message": "❌ First set a session token."})
    if not post_id:
        return jsonify({"message": "❌ Post ID / URL required."})
    if not uploaded or not uploaded.filename:
        return jsonify({"message": "❌ Upload a TXT file."})

    try:
        comments = _parse_comment_lines(uploaded.read())
    except Exception as e:
        return jsonify({"message": f"❌ TXT read error: {e}"})

    # One-pass controlled run; duplicate lines are removed.
    unique_comments = []
    seen = set()
    for item in comments:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            unique_comments.append(item)

    if not unique_comments:
        return jsonify({"message": "❌ No comments found in TXT."})

    # Keep the safe one-pass limit from the previous version.
    unique_comments = unique_comments[:50]

    COMMENT_STATS = {"sent": 0, "failed": 0, "loaded": len(unique_comments), "sender": ""}
    COMMENT_STOP_EVENT.clear()
    COMMENT_START_TIME = time.time()

    COMMENT_THREAD = threading.Thread(
        target=run_comment_sender,
        args=(token, post_id, owner_name, unique_comments, delay),
        daemon=True
    )
    COMMENT_THREAD.start()

    log(f"🚀 Comment run started with {len(unique_comments)} comment(s).")
    return jsonify({"message": f"✅ Started. {len(unique_comments)} comment(s) loaded."})

@app.route("/comment_stop", methods=["POST"])
def comment_stop():
    COMMENT_STOP_EVENT.set()
    log("🛑 Comment stop requested.")
    return jsonify({"message": "✅ Comment sender stopped."})

@app.route("/comment_stats")
def comment_stats():
    uptime = int(time.time() - COMMENT_START_TIME) if COMMENT_START_TIME else 0
    return jsonify({
        "running": bool(COMMENT_THREAD and COMMENT_THREAD.is_alive()),
        "uptime": uptime,
        "sent": COMMENT_STATS["sent"],
        "failed": COMMENT_STATS["failed"],
        "loaded": COMMENT_STATS["loaded"],
        "sender": COMMENT_STATS["sender"]
    })

@app.route("/logs")
def get_logs():
    return jsonify({"logs": LOGS[-100:], "token_set": bool(SESSION_TOKEN)})

PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>INSTAGRAM COMMENT PANEL</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --ink:#111111;
  --paper:#f2f0e9;
  --red:#d1361f;
  --blue:#1c3f94;
  --line:#111111;
}
html,body{height:100%}
body{
  font-family:'Inter',sans-serif;
  background:var(--paper);
  color:var(--ink);
  min-height:100vh;
}
.wrap{ max-width:920px; margin:0 auto; padding:0 18px 60px; }

/* ============ MASTHEAD ============ */
.masthead{ padding:38px 0 22px; border-bottom:4px solid var(--ink); }
.masthead .kicker{ font-family:'IBM Plex Mono',monospace; font-size:.72rem; letter-spacing:.18em; color:var(--red); text-transform:uppercase; margin-bottom:14px; }
.masthead h1{ font-family:'Archivo Black',sans-serif; font-size:clamp(1.8rem,6vw,3rem); line-height:1.02; letter-spacing:-.01em; text-transform:uppercase; }
.masthead .sub{ margin-top:10px; font-family:'IBM Plex Mono',monospace; font-size:.78rem; letter-spacing:.08em; color:var(--ink); opacity:.7; text-transform:uppercase; }

/* ============ TOP STATUS STRIP ============ */
.statusstrip{ display:grid; grid-template-columns:1fr 1fr; border-bottom:4px solid var(--ink); }
.statuscell{ padding:16px 18px; display:flex; align-items:center; justify-content:space-between; gap:10px; }
.statuscell:first-child{ border-right:2px solid var(--ink); }
.statuscell .label{ font-family:'IBM Plex Mono',monospace; font-size:.72rem; letter-spacing:.1em; text-transform:uppercase; opacity:.65; }
.tag{
  font-family:'IBM Plex Mono',monospace; font-size:.72rem; font-weight:700; letter-spacing:.06em;
  padding:5px 10px; border:2px solid var(--ink); text-transform:uppercase; background:var(--paper);
}
.tag.error{ background:var(--red); color:#fff; border-color:var(--ink); }
.tag.ready,.tag.running{ background:var(--blue); color:#fff; border-color:var(--ink); }

/* ============ MODULE ============ */
.module{ display:grid; grid-template-columns:76px 1fr; border-bottom:4px solid var(--ink); }
.module .idx{
  font-family:'Archivo Black',sans-serif; font-size:2.4rem; color:var(--red);
  border-right:2px solid var(--ink); padding:26px 10px; text-align:center; align-self:stretch;
}
.module .body{ padding:26px 20px 30px; }
.module h2{ font-family:'Archivo Black',sans-serif; font-size:1.3rem; text-transform:uppercase; letter-spacing:.01em; margin-bottom:20px; }

/* ============ FORM ============ */
.field{ margin-bottom:20px; }
.field:last-child{ margin-bottom:0; }
.field label{ display:block; font-family:'IBM Plex Mono',monospace; font-size:.7rem; letter-spacing:.1em; text-transform:uppercase; margin-bottom:8px; opacity:.75; }
input[type="text"],input[type="number"]{
  width:100%; padding:13px 14px; border:2px solid var(--ink); background:#fff; color:var(--ink);
  font:600 .95rem 'Inter',sans-serif; border-radius:0;
}
input:focus{ outline:none; box-shadow:4px 4px 0 var(--red); position:relative; }
.grid2{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }

.filebox{ position:relative; border:2px dashed var(--ink); padding:22px 16px; text-align:center; background:#fff; }
.filebox .ft{ font-family:'IBM Plex Mono',monospace; font-size:.76rem; letter-spacing:.06em; text-transform:uppercase; }
.filebox .fn{ margin-top:8px; font-family:'IBM Plex Mono',monospace; font-size:.78rem; color:var(--blue); font-weight:600; }
.filebox input[type="file"]{ position:absolute; inset:0; opacity:0; cursor:pointer; width:100%; height:100%; }

.note{ margin-top:18px; padding:12px 14px; border-left:4px solid var(--blue); background:#fff; font-size:.84rem; line-height:1.55; opacity:.85; }

/* ============ BUTTONS ============ */
.btnrow{ display:flex; gap:16px; flex-wrap:wrap; margin-top:4px; }
.btn{
  font-family:'IBM Plex Mono',monospace; font-weight:700; font-size:.82rem; letter-spacing:.08em; text-transform:uppercase;
  padding:15px 24px; border:2px solid var(--ink); background:#fff; color:var(--ink); cursor:pointer;
  box-shadow:5px 5px 0 var(--ink); transition:transform .12s, box-shadow .12s; flex:1; min-width:150px;
}
.btn:hover{ transform:translate(-2px,-2px); box-shadow:7px 7px 0 var(--ink); }
.btn:active{ transform:translate(0,0); box-shadow:2px 2px 0 var(--ink); }
.btn-primary{ background:var(--ink); color:#fff; }
.btn-danger{ background:var(--red); color:#fff; border-color:var(--ink); }

/* ============ STATS ============ */
.statgrid{ display:grid; grid-template-columns:repeat(4,1fr); border:2px solid var(--ink); margin-top:26px; }
.statgrid div{ padding:16px 8px; text-align:center; border-right:2px solid var(--ink); }
.statgrid div:last-child{ border-right:none; }
.statgrid strong{ display:block; font-family:'Archivo Black',sans-serif; font-size:1.6rem; }
.statgrid span{ font-family:'IBM Plex Mono',monospace; font-size:.66rem; letter-spacing:.08em; text-transform:uppercase; opacity:.65; }
.senderline{ margin-top:16px; font-family:'IBM Plex Mono',monospace; font-size:.8rem; }
.senderline b{ color:var(--blue); }

/* ============ LOGS ============ */
.logtab{ display:inline-block; font-family:'IBM Plex Mono',monospace; font-size:.7rem; letter-spacing:.1em; text-transform:uppercase; background:var(--ink); color:#fff; padding:5px 12px; margin-bottom:-2px; position:relative; z-index:1; }
.log-box{ height:280px; overflow:auto; background:#fff; border:2px solid var(--ink); padding:16px; font:13px/1.7 'IBM Plex Mono',monospace; }

/* ============ FOOTER ============ */
.footer{ padding:22px 0 0; font-family:'IBM Plex Mono',monospace; font-size:.7rem; letter-spacing:.08em; text-transform:uppercase; opacity:.55; text-align:center; }

@media(max-width:640px){
  .grid2{ grid-template-columns:1fr; }
  .module{ grid-template-columns:52px 1fr; }
  .module .idx{ font-size:1.5rem; padding:20px 6px; }
  .module .body{ padding:20px 14px 24px; }
  .statusstrip{ grid-template-columns:1fr; }
  .statuscell:first-child{ border-right:none; border-bottom:2px solid var(--ink); }
  .statgrid{ grid-template-columns:1fr 1fr; }
  .statgrid div:nth-child(2){ border-right:none; }
  .statgrid div:nth-child(3){ border-top:2px solid var(--ink); }
  .statgrid div:nth-child(4){ border-top:2px solid var(--ink); border-right:none; }
  .btn{ min-width:100%; }
}
</style>
</head>
<body>
<div class="wrap">

  <div class="masthead">
    <div class="kicker">Controlled Single-Account Runner</div>
    <h1>Instagram Comment Panel</h1>
    <div class="sub">Manual token · Manual post · One-pass execution</div>
  </div>

  <div class="statusstrip">
    <div class="statuscell"><span class="label">Session</span><span class="tag error" id="tokenCard"><span id="tokenStatus">❌ Missing</span></span></div>
    <div class="statuscell"><span class="label">Comments</span><span class="tag error" id="runCard"><span id="runStatus">🔴 Stopped</span></span></div>
  </div>

  <div class="module">
    <div class="idx">01</div>
    <div class="body">
      <h2>Session Token</h2>
      <div class="field">
        <label>Session Token</label>
        <input id="tokenInput" type="text" placeholder="Paste session token">
      </div>
      <div class="btnrow"><button class="btn btn-primary" onclick="setToken()">Set Token</button></div>
      <div class="note">Comments are sent only from the authenticated account. The panel does not rotate accounts or impersonate another username.</div>
    </div>
  </div>

  <div class="module">
    <div class="idx">02</div>
    <div class="body">
      <h2>Post Comment Tool</h2>
      <div class="field">
        <label>Post ID / Post URL</label>
        <input id="postId" placeholder="12345678901234567 or https://instagram.com/p/SHORTCODE/ or https://instagram.com/reel/SHORTCODE/">
      </div>
      <div class="grid2">
        <div class="field">
          <label>Target Post Owner (optional)</label>
          <input id="ownerName" placeholder="@username">
        </div>
        <div class="field">
          <label>Delay Between Comments (10–3600 sec)</label>
          <input id="commentDelay" type="number" value="15" min="10" max="3600">
        </div>
      </div>
      <div class="field">
        <label>upload.txt — One comment per line</label>
        <div class="filebox">
          <div class="ft">Select .txt file</div>
          <div class="fn" id="dzFileName"></div>
          <input id="commentsFile" type="file" accept=".txt,text/plain">
        </div>
      </div>
      <div class="note">Each non-empty TXT line is treated as one comment. Duplicate lines are removed. The current controlled run loads up to 50 unique comments and processes them once.</div>
    </div>
  </div>

  <div class="module">
    <div class="idx">03</div>
    <div class="body">
      <h2>Controls &amp; Stats</h2>
      <div class="btnrow">
        <button class="btn btn-primary" onclick="startComments()">Start Comments</button>
        <button class="btn btn-danger" onclick="stopComments()">Stop Comments</button>
      </div>
      <div class="statgrid">
        <div><strong id="loaded">0</strong><span>Loaded</span></div>
        <div><strong id="sent">0</strong><span>Sent</span></div>
        <div><strong id="failed">0</strong><span>Failed</span></div>
        <div><strong id="uptime">00:00</strong><span>Uptime</span></div>
      </div>
      <div class="senderline">Sender: <b id="sender">—</b></div>
    </div>
  </div>

  <div class="module" style="border-bottom:4px solid var(--ink)">
    <div class="idx">04</div>
    <div class="body">
      <h2>Live Logs</h2>
      <div class="logtab">Live</div>
      <div class="log-box" id="logs">Panel ready. Set a session token to begin.</div>
    </div>
  </div>

  <div class="footer">Instagram Comment Panel — one-pass controlled execution</div>
</div>

<script>
/* cosmetic-only: show selected filename (does not affect startComments()) */
const _cf = document.getElementById('commentsFile');
if(_cf){
  _cf.addEventListener('change', () => {
    const f = _cf.files[0];
    document.getElementById('dzFileName').textContent = f ? f.name : '';
  });
}

function setToken(){
 const token=document.getElementById('tokenInput').value.trim();
 if(!token)return alert('❌ Token paste करें!');
 const fd=new FormData();fd.append('token',token);
 fetch('/set_token',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{
   alert(d.message||d.error);
   if(d.message){document.getElementById('tokenCard').className='tag ready';document.getElementById('tokenStatus').textContent='✅ Ready';}
 }).catch(e=>alert('❌ '+e.message));
}
function startComments(){
 const file=document.getElementById('commentsFile').files[0];
 if(!file)return alert('❌ upload.txt select करें!');
 const fd=new FormData();
 fd.append('post_id',document.getElementById('postId').value.trim());
 fd.append('owner_name',document.getElementById('ownerName').value.trim());
 fd.append('comment_delay',document.getElementById('commentDelay').value);
 fd.append('comments_file',file);
 fetch('/comment_start',{method:'POST',body:fd}).then(r=>r.json()).then(d=>alert(d.message)).catch(e=>alert('❌ '+e.message));
}
function stopComments(){
 fetch('/comment_stop',{method:'POST'}).then(r=>r.json()).then(d=>alert(d.message));
}
function update(){
 fetch('/comment_stats').then(r=>r.json()).then(d=>{
   document.getElementById('loaded').textContent=d.loaded||0;
   document.getElementById('sent').textContent=d.sent||0;
   document.getElementById('failed').textContent=d.failed||0;
   document.getElementById('sender').textContent=d.sender?'@'+d.sender:'—';
   const t=d.uptime||0;
   document.getElementById('uptime').textContent=String(Math.floor(t/60)).padStart(2,'0')+':'+String(t%60).padStart(2,'0');
   document.getElementById('runStatus').textContent=d.running?'🟢 Running':'🔴 Stopped';
   document.getElementById('runCard').className=d.running?'tag running':'tag error';
 });
 fetch('/logs').then(r=>r.json()).then(d=>{
   const b=document.getElementById('logs');b.innerHTML=(d.logs||[]).join('<br>');b.scrollTop=b.scrollHeight;
 });
}
setInterval(update,2000);update();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("🚀 INSTAGRAM COMMENT PANEL - ready")
    app.run(host="0.0.0.0", port=port, debug=False)
