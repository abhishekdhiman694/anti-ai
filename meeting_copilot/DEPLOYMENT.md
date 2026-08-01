# Deploying the Meeting Copilot backend + admin dashboard

Two pieces to deploy, both free to start:
- **`server/`** → Render.com (the backend - holds your OpenAI key, prompts, and the login/expiry system)
- **`dashboard/`** → Vercel (a web page to issue/revoke access credentials)

The client exe never contains your OpenAI key or prompts - it only talks to the URL you set up here.

## 1. Push the code to GitHub

Render and Vercel both deploy from a Git repo.

```bash
cd meeting_copilot
git init
git add server dashboard
git commit -m "Add backend server and admin dashboard"
```

Create a new repo on GitHub (can be private) and push:

```bash
git remote add origin https://github.com/<you>/<repo-name>.git
git push -u origin main
```

## 2. Deploy the backend on Render

1. Go to [render.com](https://render.com), sign up (no card needed for the free tier).
2. **New +** → **Web Service** → connect your GitHub repo.
3. Settings:
   - **Root Directory**: `server`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
4. **Environment** tab → add two variables:
   - `OPENAI_API_KEY` = your real OpenAI key
   - `ADMIN_SECRET` = a long random string only you know (this guards token creation - treat it like a password; e.g. generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
5. Click **Create Web Service**. First deploy takes a couple minutes.
6. Once live, note your URL - something like `https://meeting-copilot-abc1.onrender.com`.
7. Test it: open `https://your-url.onrender.com/health` in a browser - should show `{"ok":true}`.

**Free tier caveat**: the service sleeps after ~15 min of no traffic, and the first request after that takes ~30-50s to wake up (the client's timeouts already account for this). Also, the credential store (`tokens.json`) resets whenever you redeploy (push new code) - it survives normal sleep/wake, just not a fresh deploy. Fine for casually sharing with a few people; if that becomes a problem later, swap the JSON file for a proper database.

## 3. Deploy the dashboard on Vercel

1. Go to [vercel.com](https://vercel.com), sign up, **Add New** → **Project** → import the same GitHub repo.
2. **Root Directory**: `dashboard`
3. Framework preset: **Other** (it's a static HTML file, no build step needed).
4. Deploy. You'll get a URL like `https://your-dashboard.vercel.app`.
5. Open it, and on first load enter:
   - **Server URL**: your Render URL from step 2
   - **Admin Secret**: the `ADMIN_SECRET` you set on Render
6. Click **Connect** - you should see an empty credentials table. Use the form to issue your first token.

## 4. Point the client at your server

In `meeting_copilot/config.py`, update:

```python
SERVER_URL = os.getenv("MEETING_COPILOT_SERVER_URL", "http://127.0.0.1:8000")
```

Change the fallback to your real Render URL, then rebuild the exe (`pyinstaller MeetingCopilot.spec --noconfirm`). That's the URL baked into every exe you hand out from then on.

## 5. Issue access to someone

Either via the dashboard (easiest) or the CLI:

```bash
cd server
SERVER_URL=https://your-url.onrender.com ADMIN_SECRET=your-secret \
  python manage_tokens.py create alice hunter2 3 "Alice - interview 8/2"
```

That gives `alice` / `hunter2`, valid for 3 hours. Send them the exe + those two values (not a file - just tell them the username/password). They enter it once when the app first asks; it stops working automatically when the 3 hours are up, and you can revoke it early anytime from the dashboard.
