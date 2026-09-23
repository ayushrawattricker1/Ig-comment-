Fix for Render deploy failures (igspam)

What changed:
- requirements.txt updated to pinned versions that exist on PyPI:
  Flask==2.3.3
  instagrapi==2.2.1
  gunicorn==21.2.0

How to use:
1) Replace the repository's requirements.txt with this file (or commit and push).
2) Ensure app.py is at repo root (the Flask app).
3) Optional: add render.yaml to repo root to auto-configure Render.
4) On Render dashboard, set Build Command to: pip install -r requirements.txt
   and Start Command to: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8
5) Manual deploy on Render -> Deploy latest commit.

Notes:
- Do NOT include any shell commands in requirements.txt (only package names).
- If you still get a build error, open the build logs and paste the error here and I'll help.
