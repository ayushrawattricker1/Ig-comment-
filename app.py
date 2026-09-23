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

    # Instagram post URL or shortcode
    match = re.search(r"/p/([^/?#]+)/?", value)
    shortcode = match.group(1) if match else value.strip("/").split("/")[-1]
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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🚀 INSTAGRAM COMMENT PANEL</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Poppins:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--violet:#8b2dff;--cyan:#22d3ee;--mint:#00ff9d;--red:#ff4757}
body{font-family:Poppins,sans-serif;background:#05030a;color:#fff;min-height:100vh}
.backdrop{position:fixed;inset:0;z-index:-1;background:
radial-gradient(ellipse at 50% 110%,rgba(139,45,255,.35),transparent 55%),
radial-gradient(ellipse at 10% 0,rgba(34,211,238,.18),transparent 45%),
linear-gradient(180deg,#07040d,#12081f 70%,#05020a)}
.container{max-width:900px;margin:auto;padding:24px 16px 45px}
h1{text-align:center;font-family:Orbitron;font-size:2.25rem;background:linear-gradient(45deg,var(--cyan),var(--violet),var(--mint));-webkit-background-clip:text;color:transparent;margin-bottom:5px}
.subtag{text-align:center;color:#c3a8e0;letter-spacing:1px;margin-bottom:18px}
.pagehead{display:flex;gap:10px;align-items:center;margin-bottom:14px}
.pagehead .num{color:var(--violet);border:1px solid var(--violet);border-radius:8px;padding:3px 10px}
.glass-card{background:rgba(20,10,35,.65);backdrop-filter:blur(14px);border:1px solid rgba(139,45,255,.4);border-radius:20px;padding:22px;margin-bottom:18px;box-shadow:0 18px 40px rgba(0,0,0,.45)}
.status-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.status-card{padding:18px;text-align:center;border:2px solid rgba(34,211,238,.3);border-radius:15px;background:rgba(255,255,255,.05)}
.status-card.ready,.status-card.running{border-color:var(--mint);background:rgba(0,255,157,.12)}
.status-card.error{border-color:var(--red);background:rgba(255,71,87,.1)}
.status-icon{font-size:1.7rem;color:var(--cyan);margin-bottom:8px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.input-group{position:relative}
.input-group.full{grid-column:1/-1}
label{display:block;margin-bottom:9px;color:var(--cyan);font-weight:600;font-size:.84rem;text-transform:uppercase;letter-spacing:.8px}
input,textarea{width:100%;padding:14px 15px;background:rgba(255,255,255,.06);border:2px solid rgba(139,45,255,.35);border-radius:13px;color:#fff;font:15px Poppins}
input:focus,textarea:focus{outline:none;border-color:var(--cyan);box-shadow:0 0 18px rgba(34,211,238,.25)}
textarea{min-height:120px;resize:vertical}
.btnrow{display:flex;gap:13px;flex-wrap:wrap}
.btn{border:0;padding:15px 25px;border-radius:13px;color:#fff;font:700 14px Orbitron;cursor:pointer;flex:1;min-width:150px}
.btn-primary{background:linear-gradient(135deg,var(--cyan),#6d28d9)}
.btn-success{background:linear-gradient(135deg,var(--mint),#00a86b)}
.btn-danger{background:linear-gradient(135deg,var(--red),#a9001e)}
.note{padding:13px;border-left:3px solid var(--cyan);background:rgba(34,211,238,.07);border-radius:8px;color:#d7cbea;font-size:.9rem;line-height:1.55;margin-top:14px}
.log-box{height:300px;overflow:auto;background:rgba(0,0,0,.65);border:1px solid rgba(139,45,255,.4);border-radius:14px;padding:16px;font:13px "Courier New";line-height:1.65;color:#c9a8ff}
.counter{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:15px}
.counter div{padding:13px;border-radius:12px;text-align:center;background:rgba(255,255,255,.06);border:1px solid rgba(139,45,255,.3)}
.counter strong{display:block;font-size:1.25rem;color:var(--mint)}
@media(max-width:650px){.form-grid{grid-template-columns:1fr}.input-group.full{grid-column:auto}.counter{grid-template-columns:1fr 1fr}h1{font-size:1.65rem}}
</style>
</head>
<body>
<div class="backdrop"></div>
<div class="container">
<h1><i class="fas fa-comment-dots"></i> INSTAGRAM COMMENT PANEL</h1>
<div class="subtag">CONTROLLED SINGLE-ACCOUNT COMMENT RUNNER</div>

<div class="glass-card">
<div class="status-grid">
<div class="status-card error" id="tokenCard"><i class="fas fa-key status-icon"></i><b>Session</b><br><span id="tokenStatus">❌ Missing</span></div>
<div class="status-card error" id="runCard"><i class="fas fa-play status-icon"></i><b>Comments</b><br><span id="runStatus">🔴 Stopped</span></div>
</div>
</div>

<div class="glass-card">
<div class="pagehead"><span class="num">01</span><h2>Session Token</h2></div>
<label>Session Token</label>
<input id="tokenInput" type="text" placeholder="Paste session token">
<div class="btnrow" style="margin-top:14px"><button class="btn btn-success" onclick="setToken()">SET TOKEN</button></div>
<div class="note">Comments are sent only from the authenticated account. The panel does not rotate accounts or impersonate another username.</div>
</div>

<div class="glass-card">
<div class="pagehead"><span class="num">02</span><h2>Post Comment Tool</h2></div>
<div class="form-grid">
<div class="input-group full">
<label>Post ID / Post URL</label>
<input id="postId" placeholder="12345678901234567 or https://instagram.com/p/SHORTCODE/">
</div>
<div class="input-group">
<label>Target Post Owner (optional)</label>
<input id="ownerName" placeholder="@username">
</div>
<div class="input-group">
<label>Delay Between Comments (10–3600 sec)</label>
<input id="commentDelay" type="number" value="15" min="10" max="3600">
</div>
<div class="input-group full">
<label>upload.txt — One comment per line</label>
<input id="commentsFile" type="file" accept=".txt,text/plain">
</div>
</div>
<div class="note">Each non-empty TXT line is treated as one comment. Duplicate lines are removed. The current controlled run loads up to 50 unique comments and processes them once.</div>
</div>

<div class="glass-card">
<div class="pagehead"><span class="num">03</span><h2>Controls & Stats</h2></div>
<div class="btnrow">
<button class="btn btn-primary" onclick="startComments()">START COMMENTS</button>
<button class="btn btn-danger" onclick="stopComments()">STOP COMMENTS</button>
</div>
<div class="counter">
<div><strong id="loaded">0</strong>Loaded</div>
<div><strong id="sent">0</strong>Sent</div>
<div><strong id="failed">0</strong>Failed</div>
<div><strong id="uptime">00:00</strong>Uptime</div>
</div>
<div class="note">Sender: <b id="sender">—</b></div>
</div>

<div class="glass-card">
<div class="pagehead"><span class="num">04</span><h2>Live Logs</h2></div>
<div class="log-box" id="logs">Panel ready. Set a session token to begin.</div>
</div>
</div>

<script>
function setToken(){
 const token=document.getElementById('tokenInput').value.trim();
 if(!token)return alert('❌ Token paste करें!');
 const fd=new FormData();fd.append('token',token);
 fetch('/set_token',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{
   alert(d.message||d.error);
   if(d.message){document.getElementById('tokenCard').className='status-card ready';document.getElementById('tokenStatus').textContent='✅ Ready';}
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
   document.getElementById('runCard').className=d.running?'status-card running':'status-card error';
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
