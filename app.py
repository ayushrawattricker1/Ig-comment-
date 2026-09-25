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
<title>🚀 INSTAGRAM COMMENT PANEL</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700;800&family=Cormorant+Garamond:ital,wght@0,500;0,600;1,500&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --navy:#0c1120;
  --navy-deep:#080b16;
  --paper:#f6efe0;
  --paper-shade:#ece2cc;
  --ink:#2b2013;
  --ink-soft:#5b4d38;
  --gold:#b8863a;
  --gold-bright:#d9a94f;
  --wine:#8a2d2d;
  --forest:#2f6f4f;
}
html,body{height:100%}
body{
  font-family:'Inter',sans-serif;
  background:
    radial-gradient(ellipse at 50% -10%, rgba(217,169,79,.08), transparent 55%),
    radial-gradient(ellipse at 50% 115%, rgba(138,45,45,.10), transparent 50%),
    linear-gradient(180deg, var(--navy-deep), var(--navy) 45%, #101526 100%);
  color:var(--paper);
  min-height:100vh;
  display:flex;
  align-items:center;
  justify-content:center;
  padding:26px 14px;
  overflow-x:hidden;
}

/* ============ STAGE / BOOK SHELL ============ */
.stage{ perspective:2600px; width:100%; max-width:900px; }
.book{
  position:relative;
  background:linear-gradient(160deg,#1b2036,#11142200 60%),var(--navy);
  border-radius:14px;
  border:1px solid rgba(217,169,79,.28);
  box-shadow:0 30px 70px rgba(0,0,0,.55), 0 0 0 1px rgba(217,169,79,.06) inset;
  padding:14px;
}
.book::before{
  content:'';position:absolute;inset:6px;border:1px solid rgba(217,169,79,.16);border-radius:9px;pointer-events:none;
}

/* subtle page-indicator dots */
.dots{display:flex;justify-content:center;gap:9px;margin-bottom:12px}
.dots span{width:7px;height:7px;border-radius:50%;background:rgba(217,169,79,.25);border:1px solid rgba(217,169,79,.4);cursor:pointer;transition:all .25s}
.dots span.on{background:var(--gold-bright);box-shadow:0 0 8px rgba(217,169,79,.6);transform:scale(1.25)}

/* ============ LEAF (one visible page) ============ */
.leaf-wrap{ position:relative; min-height:560px; }
.leaf{
  position:relative;
  background:
    repeating-linear-gradient(0deg, rgba(0,0,0,.015) 0 2px, transparent 2px 4px),
    var(--paper);
  color:var(--ink);
  border-radius:8px;
  padding:34px 30px 26px;
  min-height:560px;
  box-shadow:0 18px 40px rgba(0,0,0,.35) inset, 0 2px 0 rgba(255,255,255,.4) inset;
  display:none;
  transform-origin:left center;
  backface-visibility:hidden;
  transition:transform .6s cubic-bezier(.45,.05,.15,1), opacity .5s ease;
}
.leaf.active{display:block;transform:rotateY(0deg);opacity:1}
.leaf.leave-next{transform:rotateY(-115deg);opacity:0}
.leaf.leave-prev{transform:rotateY(115deg);opacity:0}
.leaf.enter-from-next{transform:rotateY(115deg);opacity:0}
.leaf.enter-from-prev{transform:rotateY(-115deg);opacity:0}

/* dog-ear corner fold, purely decorative */
.leaf::after{
  content:'';position:absolute;right:0;bottom:0;width:34px;height:34px;
  background:linear-gradient(135deg, transparent 50%, var(--paper-shade) 51%);
  box-shadow:-2px -2px 6px rgba(0,0,0,.12) inset;border-bottom-right-radius:8px;
}

/* ============ COVER ============ */
.cover{ display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; height:100%; padding:40px 20px; }
.cover .brand{ font-family:'Inter',sans-serif; font-size:.72rem; letter-spacing:5px; color:var(--gold); margin-bottom:26px; text-transform:uppercase; }
.cover h1{ font-family:'Playfair Display',serif; font-weight:800; font-size:3rem; line-height:1.08; letter-spacing:1px; }
.cover h1 span{ display:block; }
.cover .rule{ width:60px;height:2px;background:var(--gold); margin:20px auto; }
.cover .subtitle{ font-family:'Cormorant Garamond',serif; font-style:italic; font-size:1.2rem; color:var(--ink-soft); letter-spacing:.5px; max-width:420px; }
.cover .footer-brand{ margin-top:auto; padding-top:34px; font-size:.72rem; letter-spacing:4px; color:var(--ink-soft); text-transform:uppercase; }
.btn-open{
  margin-top:34px; padding:16px 46px; border:1px solid var(--gold);
  background:linear-gradient(135deg,var(--gold),var(--gold-bright)); color:#241a08;
  font-family:'Inter',sans-serif; font-weight:700; letter-spacing:2px; font-size:.86rem;
  border-radius:40px; cursor:pointer; box-shadow:0 10px 26px rgba(184,134,58,.35);
  transition:transform .2s, filter .2s;
}
.btn-open:hover{ transform:translateY(-2px); filter:brightness(1.06) }

/* ============ PAGE HEAD ============ */
.pagehead{ display:flex; align-items:baseline; gap:14px; margin-bottom:22px; padding-bottom:14px; border-bottom:1px solid rgba(43,32,19,.18); }
.pagehead .num{ font-family:'Playfair Display',serif; font-size:1.3rem; font-weight:700; color:var(--gold); }
.pagehead h2{ font-family:'Playfair Display',serif; font-size:1.5rem; font-weight:700; letter-spacing:.4px; color:var(--ink); }

/* ============ STATUS ============ */
.status-grid{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:6px; }
.status-card{ padding:18px; text-align:center; border-radius:12px; border:1.5px solid rgba(43,32,19,.18); background:rgba(255,255,255,.35); transition:all .2s; }
.status-card.ready,.status-card.running{ border-color:var(--forest); background:rgba(47,111,79,.1); }
.status-card.error{ border-color:var(--wine); background:rgba(138,45,45,.08); }
.status-icon{ font-size:1.5rem; color:var(--gold); margin-bottom:8px; display:block; }
.status-card b{ font-family:'Inter',sans-serif; font-size:.8rem; letter-spacing:.5px; text-transform:uppercase; color:var(--ink-soft); }
.status-card span{ font-weight:600; }

/* ============ FORM ============ */
.form-grid{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.input-group{ position:relative; }
.input-group.full{ grid-column:1/-1; }
label{ display:block; margin-bottom:9px; color:var(--ink-soft); font-weight:600; font-size:.78rem; text-transform:uppercase; letter-spacing:.8px; }
input[type="text"],input[type="number"]{
  width:100%; padding:13px 15px; background:#fff; border:1.5px solid rgba(43,32,19,.22);
  border-radius:9px; color:var(--ink); font:15px 'Inter',sans-serif;
}
input:focus{ outline:none; border-color:var(--gold); box-shadow:0 0 0 3px rgba(184,134,58,.15); }
.note{ padding:13px 15px; border-left:3px solid var(--gold); background:rgba(184,134,58,.08); border-radius:6px; color:var(--ink-soft); font-size:.86rem; line-height:1.6; margin-top:16px; }

/* file drop styling (visual only — same #commentsFile input underneath) */
.dropzone{
  position:relative; border:2px dashed rgba(43,32,19,.3); border-radius:12px;
  padding:22px 16px; text-align:center; background:rgba(255,255,255,.3); transition:border-color .2s, background .2s;
}
.dropzone:hover{ border-color:var(--gold); background:rgba(184,134,58,.06); }
.dropzone i{ font-size:1.5rem; color:var(--gold); margin-bottom:8px; display:block; }
.dropzone .dz-text{ font-size:.86rem; color:var(--ink-soft); }
.dropzone .dz-file{ font-size:.86rem; color:var(--forest); font-weight:600; margin-top:6px; }
.dropzone input[type="file"]{ position:absolute; inset:0; opacity:0; cursor:pointer; width:100%; height:100%; }

/* ============ BUTTONS ============ */
.btnrow{ display:flex; gap:14px; flex-wrap:wrap; }
.btn{ border:0; padding:15px 26px; border-radius:10px; font:700 .85rem 'Inter',sans-serif; letter-spacing:.6px; cursor:pointer; flex:1; min-width:150px; transition:transform .15s, filter .15s; color:#fff; }
.btn:hover{ transform:translateY(-2px); filter:brightness(1.08); }
.btn-success{ background:linear-gradient(135deg,var(--forest),#1f4d37); }
.btn-primary{ background:linear-gradient(135deg,var(--gold),#8a6220); color:#241a08; }
.btn-danger{ background:linear-gradient(135deg,var(--wine),#5c1f1f); }

/* ============ COUNTERS ============ */
.counter{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:18px; }
.counter div{ padding:14px 6px; border-radius:10px; text-align:center; background:rgba(255,255,255,.35); border:1px solid rgba(43,32,19,.16); }
.counter strong{ display:block; font-family:'Playfair Display',serif; font-size:1.4rem; color:var(--gold-bright); filter:brightness(.75); }
.counter span{ font-size:.72rem; text-transform:uppercase; letter-spacing:.6px; color:var(--ink-soft); }

/* ============ LOGS ============ */
.log-box{ height:300px; overflow:auto; background:#1c1912; border:1px solid rgba(184,134,58,.35); border-radius:10px; padding:16px; font:13px 'Courier New',monospace; line-height:1.65; color:#e8dcc0; }
.log-box::-webkit-scrollbar{width:7px}
.log-box::-webkit-scrollbar-thumb{background:var(--gold);border-radius:8px}

/* ============ FINAL PAGE ============ */
.final-grid{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:6px; }
.final-grid .cell{ padding:16px; border-radius:10px; background:rgba(255,255,255,.35); border:1px solid rgba(43,32,19,.16); text-align:center; }
.final-grid .cell span.k{ display:block; font-size:.72rem; letter-spacing:.6px; text-transform:uppercase; color:var(--ink-soft); margin-bottom:6px; }
.final-grid .cell span.v{ font-family:'Playfair Display',serif; font-weight:700; font-size:1.15rem; color:var(--ink); }
.btn-cover{
  margin-top:26px; width:100%; padding:15px; border:1px solid var(--gold); background:transparent; color:var(--gold);
  font:700 .82rem 'Inter',sans-serif; letter-spacing:1.5px; border-radius:10px; cursor:pointer; transition:all .2s;
}
.btn-cover:hover{ background:var(--gold); color:#241a08; }

/* ============ NAV ============ */
.navrow{ display:flex; justify-content:space-between; gap:12px; margin-top:26px; }
.navbtn{
  flex:1; padding:14px 16px; border-radius:10px; border:1px solid var(--gold); background:transparent; color:var(--ink);
  font:600 .8rem 'Inter',sans-serif; letter-spacing:1px; text-transform:uppercase; cursor:pointer;
  display:flex; align-items:center; justify-content:center; gap:8px; transition:all .2s;
}
.navbtn:hover{ background:rgba(184,134,58,.12); }
.navbtn:disabled{ opacity:.3; cursor:not-allowed; }

@media(max-width:650px){
  .form-grid{grid-template-columns:1fr}
  .input-group.full{grid-column:auto}
  .status-grid{grid-template-columns:1fr 1fr}
  .counter{grid-template-columns:1fr 1fr}
  .final-grid{grid-template-columns:1fr 1fr}
  .cover h1{font-size:2.1rem}
  .leaf{padding:26px 18px 22px}
  .leaf-wrap{min-height:auto}
  .leaf{min-height:auto}
  body{padding:16px 10px}
}
</style>
</head>
<body>

<div class="stage">
  <div class="dots" id="dots">
    <span class="on" data-i="0"></span><span data-i="1"></span><span data-i="2"></span>
    <span data-i="3"></span><span data-i="4"></span><span data-i="5"></span>
  </div>

  <div class="book">
    <div class="leaf-wrap" id="leafWrap">

      <!-- COVER -->
      <section class="leaf active" data-leaf="0">
        <div class="cover">
          <div class="brand">Digital Comment Panel</div>
          <h1><span>INSTAGRAM</span><span>COMMENT PANEL</span></h1>
          <div class="rule"></div>
          <div class="subtitle">Controlled single-account comment runner</div>
          <button class="btn-open" onclick="goLeaf(1)">OPEN PANEL</button>
          <div class="footer-brand">YK Tricks India</div>
        </div>
      </section>

      <!-- PAGE 01 -->
      <section class="leaf" data-leaf="1">
        <div class="pagehead"><span class="num">01</span><h2>Session Token</h2></div>

        <div class="status-grid">
          <div class="status-card error" id="tokenCard"><i class="fas fa-key status-icon"></i><b>Session</b><br><span id="tokenStatus">❌ Missing</span></div>
          <div class="status-card error" id="runCard"><i class="fas fa-play status-icon"></i><b>Comments</b><br><span id="runStatus">🔴 Stopped</span></div>
        </div>

        <div style="margin-top:24px">
          <label>Session Token</label>
          <input id="tokenInput" type="text" placeholder="Paste session token">
          <div class="btnrow" style="margin-top:16px"><button class="btn btn-success" onclick="setToken()">SET TOKEN</button></div>
          <div class="note">Comments are sent only from the authenticated account. The panel does not rotate accounts or impersonate another username.</div>
        </div>

        <div class="navrow">
          <button class="navbtn" onclick="goLeaf(0)"><i class="fas fa-chevron-left"></i> Previous</button>
          <button class="navbtn" onclick="goLeaf(2)">Next Page <i class="fas fa-chevron-right"></i></button>
        </div>
      </section>

      <!-- PAGE 02 -->
      <section class="leaf" data-leaf="2">
        <div class="pagehead"><span class="num">02</span><h2>Post Comment Tool</h2></div>

        <div class="form-grid">
          <div class="input-group full">
            <label>Post ID / Post URL</label>
            <input id="postId" placeholder="12345678901234567 or https://instagram.com/p/SHORTCODE/ or https://instagram.com/reel/SHORTCODE/">
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
            <div class="dropzone">
              <i class="fas fa-file-arrow-up"></i>
              <div class="dz-text">Click or drop your .txt file here</div>
              <div class="dz-file" id="dzFileName"></div>
              <input id="commentsFile" type="file" accept=".txt,text/plain">
            </div>
          </div>
        </div>
        <div class="note">Each non-empty TXT line is treated as one comment. Duplicate lines are removed. The current controlled run loads up to 50 unique comments and processes them once.</div>

        <div class="navrow">
          <button class="navbtn" onclick="goLeaf(1)"><i class="fas fa-chevron-left"></i> Previous</button>
          <button class="navbtn" onclick="goLeaf(3)">Next Page <i class="fas fa-chevron-right"></i></button>
        </div>
      </section>

      <!-- PAGE 03 -->
      <section class="leaf" data-leaf="3">
        <div class="pagehead"><span class="num">03</span><h2>Controls &amp; Stats</h2></div>

        <div class="btnrow">
          <button class="btn btn-primary" onclick="startComments()">START COMMENTS</button>
          <button class="btn btn-danger" onclick="stopComments()">STOP COMMENTS</button>
        </div>

        <div class="counter">
          <div><strong id="loaded">0</strong><span>Loaded</span></div>
          <div><strong id="sent">0</strong><span>Sent</span></div>
          <div><strong id="failed">0</strong><span>Failed</span></div>
          <div><strong id="uptime">00:00</strong><span>Uptime</span></div>
        </div>
        <div class="note">Sender: <b id="sender">—</b></div>

        <div class="navrow">
          <button class="navbtn" onclick="goLeaf(2)"><i class="fas fa-chevron-left"></i> Previous</button>
          <button class="navbtn" onclick="goLeaf(4)">Next Page <i class="fas fa-chevron-right"></i></button>
        </div>
      </section>

      <!-- PAGE 04 -->
      <section class="leaf" data-leaf="4">
        <div class="pagehead"><span class="num">04</span><h2>Live Logs</h2></div>
        <div class="log-box" id="logs">Panel ready. Set a session token to begin.</div>

        <div class="navrow">
          <button class="navbtn" onclick="goLeaf(3)"><i class="fas fa-chevron-left"></i> Previous</button>
          <button class="navbtn" onclick="goLeaf(5)">Next Page <i class="fas fa-chevron-right"></i></button>
        </div>
      </section>

      <!-- FINAL -->
      <section class="leaf" data-leaf="5">
        <div class="pagehead"><span class="num">✦</span><h2>Session Summary</h2></div>

        <div class="final-grid">
          <div class="cell"><span class="k">Session Status</span><span class="v" id="sessionStatusFinal">❌ Missing</span></div>
          <div class="cell"><span class="k">Comment Status</span><span class="v" id="commentStatusFinal">🔴 Stopped</span></div>
          <div class="cell"><span class="k">Loaded</span><span class="v" id="loadedFinal">0</span></div>
          <div class="cell"><span class="k">Sent</span><span class="v" id="sentFinal">0</span></div>
          <div class="cell"><span class="k">Failed</span><span class="v" id="failedFinal">0</span></div>
          <div class="cell"><span class="k">Uptime</span><span class="v" id="uptimeFinal">00:00</span></div>
          <div class="cell" style="grid-column:1/-1"><span class="k">Sender</span><span class="v" id="senderFinal">—</span></div>
        </div>

        <button class="btn-cover" onclick="goLeaf(0)">BACK TO COVER</button>
      </section>

    </div>
  </div>
</div>

<script>
/* ================= BOOK PAGE NAVIGATION (additive, does not touch existing routes/logic) ================= */
let currentLeaf = 0;
const TOTAL_LEAVES = 6;

function goLeaf(target){
  if(target === currentLeaf || target < 0 || target >= TOTAL_LEAVES) return;
  const dir = target > currentLeaf ? 'next' : 'prev';
  const curEl = document.querySelector('.leaf[data-leaf="'+currentLeaf+'"]');
  const nextEl = document.querySelector('.leaf[data-leaf="'+target+'"]');

  curEl.classList.add(dir === 'next' ? 'leave-next' : 'leave-prev');
  nextEl.style.display = 'block';
  nextEl.classList.add(dir === 'next' ? 'enter-from-next' : 'enter-from-prev');
  void nextEl.offsetWidth;

  requestAnimationFrame(() => {
    nextEl.classList.add('active');
    nextEl.classList.remove('enter-from-next','enter-from-prev');
  });

  setTimeout(() => {
    curEl.classList.remove('active','leave-next','leave-prev');
    curEl.style.display = 'none';
    currentLeaf = target;
    document.querySelectorAll('#dots span').forEach(d => {
      d.classList.toggle('on', parseInt(d.dataset.i) === currentLeaf);
    });
  }, 620);
}
document.querySelectorAll('#dots span').forEach(d => {
  d.addEventListener('click', () => goLeaf(parseInt(d.dataset.i)));
});
document.addEventListener('keydown', (e) => {
  if(e.key === 'ArrowRight') goLeaf(Math.min(currentLeaf+1, TOTAL_LEAVES-1));
  if(e.key === 'ArrowLeft') goLeaf(Math.max(currentLeaf-1, 0));
});

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

   /* mirror onto the Final Status page — additive only, no existing logic touched */
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
