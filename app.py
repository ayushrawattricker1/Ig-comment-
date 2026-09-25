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
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, viewport-fit=cover">
<title>Comment Automation</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overscroll-behavior:none}
body{
  font-family:'DM Sans',sans-serif;
  background:var(--paper);
  color:var(--ink);
  overflow:hidden;
}
:root{
  --paper:#f6f3ec;
  --paper-deep:#efeae0;
  --ink:#1d1a16;
  --ink-soft:#726b5c;
  --ink-faint:#a29a89;
  --line:#dcd6c8;
  --accent:#495a41;
  --warn:#9c4632;
}

/* ============ SCREEN STACK ============ */
.stack{ position:fixed; inset:0; }
.screen{
  position:absolute; inset:0;
  display:flex; flex-direction:column;
  padding:7vh 7vw 6vh;
  opacity:0; pointer-events:none;
  transform:translateX(48px) scale(.98);
  transition:transform .55s cubic-bezier(.22,.61,.36,1), opacity .45s ease;
  overflow-y:auto;
}
.screen.active{ opacity:1; transform:translateX(0) scale(1); pointer-events:auto; }
.screen.exit-left{ opacity:0; transform:translateX(-48px) scale(.98); }
.screen.exit-right{ opacity:0; transform:translateX(48px) scale(.98); }
.screen.enter-left{ transform:translateX(-48px) scale(.98); }

/* ============ SHARED TYPE ============ */
.eyebrow{ font-size:.72rem; letter-spacing:.16em; color:var(--ink-faint); text-transform:uppercase; }
.display{ font-family:'Fraunces',serif; font-weight:600; letter-spacing:-.02em; line-height:.94; color:var(--ink); }
.idx{ font-family:'DM Sans',sans-serif; font-size:.78rem; letter-spacing:.08em; color:var(--ink-faint); font-variant-numeric:tabular-nums; }

/* ============ INTRO ============ */
.intro{ align-items:center; justify-content:center; text-align:center; gap:0; background:var(--paper-deep); }
.intro .brand{ font-size:.7rem; letter-spacing:.3em; color:var(--ink-soft); margin-bottom:34px; }
.intro .display{ font-size:clamp(2.6rem,10vw,5.2rem); max-width:820px; }
.intro .sub{ margin-top:22px; color:var(--ink-soft); font-size:1rem; max-width:340px; line-height:1.55; }
.enter{
  margin-top:52px; background:none; border:none; cursor:pointer;
  font-family:'DM Sans',sans-serif; font-weight:600; font-size:.85rem; letter-spacing:.2em;
  color:var(--ink); padding-bottom:6px; border-bottom:1px solid var(--ink);
  transition:opacity .2s;
}
.enter:hover{ opacity:.6; }

/* ============ TOP INDEX ROW ============ */
.top-row{ display:flex; justify-content:space-between; align-items:flex-start; }

/* ============ HEADING BLOCK ============ */
.headblock{ flex:1; display:flex; flex-direction:column; justify-content:center; }
.headblock .display{ font-size:clamp(3.2rem,15vw,7rem); }

/* ============ SESSION ============ */
.field{ margin-top:36px; max-width:420px; }
.field label{ display:block; font-size:.72rem; letter-spacing:.14em; color:var(--ink-faint); text-transform:uppercase; margin-bottom:10px; }
.field input{
  width:100%; border:none; border-bottom:1px solid var(--line); background:transparent;
  padding:10px 2px; font:500 1.05rem 'DM Sans',sans-serif; color:var(--ink);
}
.field input:focus{ outline:none; border-color:var(--ink); }
.field input::placeholder{ color:var(--ink-faint); }
.textbtn{
  margin-top:26px; background:none; border:none; cursor:pointer;
  font:600 .82rem 'DM Sans',sans-serif; letter-spacing:.14em; color:var(--ink);
  padding:12px 0 10px; border-bottom:1px solid var(--ink); display:inline-block;
  transition:opacity .2s, color .2s, border-color .2s;
}
.textbtn:hover{ opacity:.6; }
.textbtn.muted{ color:var(--ink-soft); border-color:var(--line); }
.status-line{ margin-top:30px; display:flex; align-items:center; gap:9px; font-size:.85rem; color:var(--ink-soft); }
.status-card{ display:inline-flex; align-items:center; gap:9px; }
.status-card::before{ content:''; width:6px; height:6px; border-radius:50%; background:var(--warn); }
.status-card.ready::before,.status-card.running::before{ background:var(--accent); }

/* ============ POST FIELDS ============ */
.fieldset{ margin-top:8px; display:flex; flex-direction:column; gap:0; max-width:520px; }
.fieldset .field{ margin-top:30px; max-width:none; }
.fieldset .row2{ display:flex; gap:28px; flex-wrap:wrap; }
.fieldset .row2 .field{ flex:1; min-width:180px; }

.drop{
  margin-top:30px; border:1px solid var(--line); border-radius:2px; padding:34px 20px;
  text-align:center; position:relative; transition:border-color .2s, background .2s; cursor:pointer;
}
.drop:hover{ border-color:var(--ink-soft); background:rgba(0,0,0,.015); }
.drop svg{ width:20px; height:20px; margin-bottom:14px; }
.drop .dtext{ font-size:.85rem; letter-spacing:.06em; color:var(--ink-soft); }
.drop .dfile{ margin-top:8px; font-size:.85rem; color:var(--accent); font-weight:600; }
.drop input[type="file"]{ position:absolute; inset:0; opacity:0; cursor:pointer; width:100%; height:100%; }

/* ============ CONTROL ============ */
.actions{ display:flex; gap:40px; margin-top:40px; flex-wrap:wrap; }
.action{
  background:none; border:none; cursor:pointer; text-align:left; padding:0;
  font-family:'Fraunces',serif; font-size:clamp(1.6rem,5vw,2.4rem); font-weight:600; color:var(--ink);
  padding-bottom:10px; border-bottom:1px solid var(--line); transition:border-color .2s, opacity .2s;
}
.action:hover{ border-color:var(--ink); opacity:.75; }
.action.stop{ color:var(--ink-soft); }

.stats-row{ display:flex; gap:0; margin-top:56px; flex-wrap:wrap; border-top:1px solid var(--line); }
.stat{ flex:1; min-width:120px; padding:22px 22px 0 0; }
.stat .n{ font-family:'Fraunces',serif; font-size:clamp(1.6rem,5vw,2.4rem); font-weight:600; font-variant-numeric:tabular-nums; }
.stat .l{ margin-top:6px; font-size:.7rem; letter-spacing:.14em; text-transform:uppercase; color:var(--ink-faint); }
.sender-line{ margin-top:22px; font-size:.85rem; color:var(--ink-soft); }
.sender-line b{ color:var(--ink); font-weight:600; }

/* ============ LIVE ============ */
.timeline{
  margin-top:24px; padding-left:18px; border-left:1px solid var(--line);
  font-size:.88rem; line-height:2.1; color:var(--ink-soft); overflow-y:auto; flex:1;
  font-variant-numeric:tabular-nums;
}

/* ============ SUMMARY ============ */
.summary-grid{ margin-top:34px; display:flex; flex-direction:column; }
.srow{ display:flex; justify-content:space-between; align-items:baseline; padding:16px 0; border-bottom:1px solid var(--line); gap:20px; }
.srow .k{ font-size:.72rem; letter-spacing:.14em; text-transform:uppercase; color:var(--ink-faint); }
.srow .v{ font-family:'Fraunces',serif; font-size:1.3rem; font-weight:600; color:var(--ink); text-align:right; }
.final-actions{ display:flex; gap:34px; margin-top:40px; }

/* ============ FOOTER NAV (screens 1-5) ============ */
.footnav{ display:flex; align-items:center; justify-content:center; gap:16px; margin-top:auto; padding-top:26px; }
.footnav .dash{ display:flex; gap:14px; }
.footnav button{
  background:none;border:none;cursor:pointer;font:600 .78rem 'DM Sans',sans-serif;
  letter-spacing:.08em;color:var(--ink-faint);padding:4px 2px;transition:color .2s;
}
.footnav button.on{ color:var(--ink); border-bottom:1px solid var(--ink); }
.footnav .arrow{ font-size:1rem; color:var(--ink-soft); background:none;border:none;cursor:pointer;padding:4px 8px; }
.footnav .arrow:disabled{ opacity:.25; cursor:default; }
.footnav .arrow:hover:not(:disabled){ color:var(--ink); }

@media(max-width:600px){
  .screen{ padding:6vh 6vw 5vh; }
  .fieldset .row2{ flex-direction:column; gap:0; }
  .stats-row{ flex-wrap:wrap; }
  .stat{ min-width:44%; padding-bottom:22px; }
  .actions{ gap:26px; }
  .final-actions{ gap:24px; }
}
</style>
</head>
<body>

<div class="stack" id="stack">

  <!-- INTRO -->
  <section class="screen active intro" data-screen="0">
    <div class="brand">YK Tricks India</div>
    <div class="display">Comment<br>Automation</div>
    <div class="sub">A controlled, single-account comment runner. Set a token, load your file, and watch it run.</div>
    <button class="enter" onclick="goScreen(1)">ENTER</button>
  </section>

  <!-- SESSION -->
  <section class="screen" data-screen="1">
    <div class="top-row"><span class="idx">01 / 05</span></div>
    <div class="headblock">
      <div class="display">Session</div>
      <div class="field">
        <label>Session Token</label>
        <input id="tokenInput" type="text" placeholder="Paste session token">
        <button class="textbtn" onclick="setToken()">SET TOKEN</button>
      </div>
      <div class="status-line">
        <span class="status-card error" id="tokenCard"><span id="tokenStatus">Missing</span></span>
      </div>
    </div>
    <div class="footnav" id="footnav1"></div>
  </section>

  <!-- POST -->
  <section class="screen" data-screen="2">
    <div class="top-row"><span class="idx">02 / 05</span></div>
    <div class="headblock">
      <div class="display">Post</div>
      <div class="fieldset">
        <div class="field">
          <label>Post URL</label>
          <input id="postId" placeholder="instagram.com/p/SHORTCODE or media id">
        </div>
        <div class="row2">
          <div class="field">
            <label>Owner</label>
            <input id="ownerName" placeholder="@username (optional)">
          </div>
          <div class="field">
            <label>Delay</label>
            <input id="commentDelay" type="number" value="15" min="10" max="3600" placeholder="10–3600 sec">
          </div>
        </div>
        <div class="field" style="max-width:none">
          <label>Comments File</label>
          <div class="drop">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M12 16V4M12 4l-4 4M12 4l4 4"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg>
            <div class="dtext">DROP COMMENTS FILE HERE — one comment per line</div>
            <div class="dfile" id="dzFileName"></div>
            <input id="commentsFile" type="file" accept=".txt,text/plain">
          </div>
        </div>
      </div>
    </div>
    <div class="footnav" id="footnav2"></div>
  </section>

  <!-- CONTROL -->
  <section class="screen" data-screen="3">
    <div class="top-row"><span class="idx">03 / 05</span></div>
    <div class="headblock">
      <div class="display">Ready</div>
      <div class="status-line">
        <span class="status-card error" id="runCard"><span id="runStatus">Stopped</span></span>
      </div>
      <div class="actions">
        <button class="action" onclick="startComments()">Start</button>
        <button class="action stop" onclick="stopComments()">Stop</button>
      </div>
      <div class="stats-row">
        <div class="stat"><div class="n" id="loaded">0</div><div class="l">Loaded</div></div>
        <div class="stat"><div class="n" id="sent">0</div><div class="l">Sent</div></div>
        <div class="stat"><div class="n" id="failed">0</div><div class="l">Failed</div></div>
        <div class="stat"><div class="n" id="uptime">00:00</div><div class="l">Uptime</div></div>
      </div>
      <div class="sender-line">Sender — <b id="sender">—</b></div>
    </div>
    <div class="footnav" id="footnav3"></div>
  </section>

  <!-- LIVE -->
  <section class="screen" data-screen="4">
    <div class="top-row"><span class="idx">04 / 05</span></div>
    <div class="display" style="flex:0">Live</div>
    <div class="timeline" id="logs">Ready. Set a session token to begin.</div>
    <div class="footnav" id="footnav4"></div>
  </section>

  <!-- SUMMARY -->
  <section class="screen" data-screen="5">
    <div class="top-row"><span class="idx">05 / 05</span></div>
    <div class="headblock">
      <div class="display">Summary</div>
      <div class="summary-grid">
        <div class="srow"><span class="k">Session Status</span><span class="v" id="sessionStatusFinal">Missing</span></div>
        <div class="srow"><span class="k">Comment Status</span><span class="v" id="commentStatusFinal">Stopped</span></div>
        <div class="srow"><span class="k">Sender</span><span class="v" id="senderFinal">—</span></div>
        <div class="srow"><span class="k">Loaded</span><span class="v" id="loadedFinal">0</span></div>
        <div class="srow"><span class="k">Sent</span><span class="v" id="sentFinal">0</span></div>
        <div class="srow"><span class="k">Failed</span><span class="v" id="failedFinal">0</span></div>
        <div class="srow"><span class="k">Uptime</span><span class="v" id="uptimeFinal">00:00</span></div>
      </div>
      <div class="final-actions">
        <button class="textbtn" onclick="goScreen(1)">RESTART</button>
        <button class="textbtn muted" onclick="goScreen(0)">HOME</button>
      </div>
    </div>
    <div class="footnav" id="footnav5"></div>
  </section>

</div>

<script>
/* ============ SCREEN NAVIGATION (additive — does not touch existing routes/logic) ============ */
let current = 0;
const TOTAL = 6; /* 0 intro + 1..5 numbered */

function renderFootnav(){
  for(let s=1;s<=5;s++){
    const host = document.getElementById('footnav'+s);
    if(!host || host.dataset.built) continue;
    host.dataset.built = '1';
    const prev = document.createElement('button');
    prev.className='arrow'; prev.textContent='←'; prev.onclick=()=>goScreen(current-1);
    const dash = document.createElement('div'); dash.className='dash';
    for(let i=1;i<=5;i++){
      const b=document.createElement('button');
      b.textContent = String(i).padStart(2,'0');
      b.dataset.i = i;
      b.onclick = ()=>goScreen(i);
      dash.appendChild(b);
    }
    const next = document.createElement('button');
    next.className='arrow'; next.textContent='→'; next.onclick=()=>goScreen(current+1);
    host.appendChild(prev); host.appendChild(dash); host.appendChild(next);
  }
}
function refreshFootnav(){
  document.querySelectorAll('.footnav .dash button').forEach(b=>{
    b.classList.toggle('on', parseInt(b.dataset.i)===current);
  });
  document.querySelectorAll('.footnav .arrow').forEach((b,i)=>{
    const isPrev = b.textContent==='←';
    if(isPrev) b.disabled = (current<=1);
    else b.disabled = (current>=5);
  });
}

function goScreen(target){
  if(target<0||target>=TOTAL||target===current) return;
  const dir = target>current ? 'next':'prev';
  const curEl = document.querySelector('.screen[data-screen="'+current+'"]');
  const nextEl = document.querySelector('.screen[data-screen="'+target+'"]');

  curEl.classList.remove('active');
  curEl.classList.add(dir==='next'?'exit-left':'exit-right');

  nextEl.classList.add(dir==='next'?'':'enter-left');
  void nextEl.offsetWidth;
  requestAnimationFrame(()=>{
    nextEl.classList.add('active');
    nextEl.classList.remove('enter-left');
  });

  setTimeout(()=>{
    curEl.classList.remove('exit-left','exit-right');
    current = target;
    refreshFootnav();
  }, 560);
}

document.addEventListener('keydown', (e)=>{
  if(e.key==='ArrowRight') goScreen(Math.min(current+1, TOTAL-1));
  if(e.key==='ArrowLeft') goScreen(Math.max(current-1, 0));
});

let touchX=0;
document.addEventListener('touchstart', e=>{ touchX = e.touches[0].clientX; }, {passive:true});
document.addEventListener('touchend', e=>{
  const dx = e.changedTouches[0].clientX - touchX;
  if(Math.abs(dx) > 60){
    if(dx < 0) goScreen(Math.min(current+1, TOTAL-1));
    else goScreen(Math.max(current-1, 0));
  }
}, {passive:true});

renderFootnav();
refreshFootnav();

/* cosmetic-only: show selected filename in the drop zone (does not affect startComments()) */
const _cf = document.getElementById('commentsFile');
if(_cf){
  _cf.addEventListener('change', () => {
    const f = _cf.files[0];
    document.getElementById('dzFileName').textContent = f ? f.name : '';
  });
}

/* ================= EXISTING FUNCTIONALITY — UNCHANGED ================= */
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

   /* mirror onto the Summary screen — additive only, no existing logic touched */
   document.getElementById('loadedFinal').textContent=d.loaded||0;
   document.getElementById('sentFinal').textContent=d.sent||0;
   document.getElementById('failedFinal').textContent=d.failed||0;
   document.getElementById('senderFinal').textContent=d.sender?'@'+d.sender:'—';
   document.getElementById('uptimeFinal').textContent=String(Math.floor(t/60)).padStart(2,'0')+':'+String(t%60).padStart(2,'0');
   document.getElementById('commentStatusFinal').textContent=d.running?'🟢 Running':'🔴 Stopped';
 });
 fetch('/logs').then(r=>r.json()).then(d=>{
   const b=document.getElementById('logs');b.innerHTML=(d.logs||[]).join('<br>');b.scrollTop=b.scrollHeight;
   document.getElementById('sessionStatusFinal').textContent=d.token_set?'✅ Ready':'❌ Missing';
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
