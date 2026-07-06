# Chrono — Deploy Guide

Chrono runs TWO ways from ONE codebase (the frontend auto-detects which):
- **Desktop (clone & run locally):**  `python app.py`   (Eel window)
- **Web (hosted live demo):**         `uvicorn web_server:app`   (FastAPI)

---

## A. Run the web server locally (test before deploying)
```
pip install -r requirements.txt
uvicorn web_server:app --reload --port 8000
```
Open http://localhost:8000  — the whole app works except live Google
Calendar (that needs the OAuth env vars below).

For LOCAL OAuth testing over http, also set:
```
export OAUTHLIB_INSECURE_TRANSPORT=1
```
(Never set that in production — prod is https.)

---

## B. Deploy to Render (free tier)
1. Push to GitHub. **Do NOT commit** `config/.env`, `config/credentials.json`,
   or `config/token.json` (add them to .gitignore).
2. Render → New → Web Service → connect the repo.
3. **Build command:**  `pip install -r requirements.txt`
4. **Start command:**  `uvicorn web_server:app --host 0.0.0.0 --port $PORT`
5. **Environment variables** (Settings → Environment):
   ```
   GROQ_API_KEY=...
   OPENROUTER_API_KEY=...
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   GOOGLE_REDIRECT_URI=https://YOUR-APP.onrender.com/auth/google/callback
   ```
6. Deploy. You get a stable https URL.

Railway is the same idea (set the start command + env vars).

---

## C. Google Cloud setup (one-time, for the hosted demo)
1. console.cloud.google.com → new project → enable **Google Calendar API**.
2. **OAuth consent screen**: User type **External**, fill app name/emails.
   - Set **Publishing status → In Production** (unverified is fine).
     This removes the test-user list so ANY Google account can sign in.
3. **Credentials → Create Credentials → OAuth client ID → Web application.**
   - Add an **Authorized redirect URI**:
     `https://YOUR-APP.onrender.com/auth/google/callback`
   - Copy the **Client ID** and **Client secret** into the env vars above.
   (Web-app client, NOT Desktop — the hosted server uses the redirect flow.)

Recruiters will see Google's "unverified app" notice once → **Advanced →
Continue**. Add a line about this on your site/README so it's expected.

---

## D. Keep-alive (free tier sleeps after inactivity)
Add a GitHub Actions cron that pings your URL every ~10 min so recruiters
never hit a cold start. (Reuse your keep_alive.yml pattern.)

---

## E. For people who CLONE the repo (self-host their own copy)
They don't use your hosted OAuth. They:
1. Create their own `config/.env` with their `GROQ_API_KEY` / `OPENROUTER_API_KEY`.
2. For calendar (optional): create a **Desktop** OAuth client in Google
   Cloud, download `credentials.json` → `config/credentials.json`.
3. Run `python app.py`. First calendar use opens a browser to authorize.

This keeps the repo free of any secrets.

---

## What NOT to commit (.gitignore)
```
config/.env
config/credentials.json
config/token.json
output/
__pycache__/
```