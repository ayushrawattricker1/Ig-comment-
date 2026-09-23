
Today 1:14 PM

app(3).py
Python
Sorry jo file me tumne comment wala kia esme karna hai auto welcome wala feature hata dena jo bataya hu sara kuch bo dalana hai theek 

Haan bhai. Ab sirf wahi Comment Panel rakha hai — extra bot/auto-welcome/group/command features nahi.

Is app.py mein exactly ye hain:

Session Token

Post ID / Post URL

Target Post Owner verification

upload.txt

1 line = 1 comment

Start / Stop

Delay

Loaded / Sent / Failed / Status

Live Logs

Authenticated account ka username

Single-account, one-pass run

Maine syntax bhi check kar diya hai; py_compile pass hai.

Download corrected app.py


app.py
Code


Screenshot_2026-09-23-14-05-41-30.jpg
Ye kya aa rha hai 

Bhai, screenshot se clear hai ki Session Token sahi login ho raha hai:

Session token activated ✅

Token login successful ✅

@nfyter ready ✅

Loaded: 6 ✅

Problem post ko resolve/verify karte waqt aa rahi hai:

⚠️ Comment worker error: substring not found

Isliye Sent = 0 hai. Matlab TXT ya token ki problem nahi hai; error Post ID / Post URL ko Instagram post mein convert karne wale part mein aa raha hai.

Tumhare current app.py mein ye line use ho rahi hai:

media_pk = cl.media_pk_from_code(shortcode)
Aur uske baad media_info() hota hai. 


Ek aur important problem
Screenshot mein owner verification bhi abhi successfully log nahi hua:

🎯 Post verified: @...
ye line aani chahiye thi. Iska matlab error post resolve hone se pehle aa raha hai.

Tum mujhe jo Post ID/URL field mein daal rahe ho uska exact format bata do — jaise:

https://www.instagram.com/p/ABC123xyz/
ya

12345678901234567
Password/session token mat bhejna. Sirf Post ID/URL ka format batao. Phir main isi app.py ke post-resolve wale part ko sahi karne ke hisaab se bataunga.


Ye hai post url https://www.instagram.com/reel/C2UnIhfBTyZ/?utm_source=ig_web_copy_link


Haan bhai, ab exact problem mil gayi. Tumhara URL /reel/ wala hai:

https://www.instagram.com/reel/C2UnIhfBTyZ/?utm_source=ig_web_copy_link

Purane code mein sirf /p/SHORTCODE/ recognize ho raha tha. Is wajah se पूरा URL query string ke saath shortcode mein chala gaya aur:

substring not found

aa raha tha.

Maine app.py mein fix kar diya hai:

/p/.../ ✅

/reel/.../ ✅

/tv/.../ ✅

?utm_source=... jaise query parameters ignore honge ✅

bare shortcode bhi accept hoga ✅

baaki panel wahi rakha hai

Python syntax check pass ✅

Download fixed app.py

Render par isi app.py ko replace karke redeploy karo. Tumhara diya hua /reel/C2UnIhfBTyZ/ URL ab correctly shortcode C2UnIhfBTyZ ke roop mein parse hoga.


app.py
Code



No file chosen
Library
/
app.py



332
333
334
335
336
337
338
339
340
341
342
343
344
345
346
347
348
349
350
351
352
353
354
355
356
357
358
359
360
361
362
363
364
365
366
367
368
369
370
371
372
373
374
375
376
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

