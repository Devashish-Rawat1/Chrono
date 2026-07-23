# Chrono — Developer Notes & Decisions

This file captures the *why* behind Chrono's design, plus gotchas and
hard-won lessons. Read this before making changes — several "obvious"
improvements would reintroduce bugs we already fixed.

---

## What Chrono is

A Python multi-agent **weekly planning assistant** with a Matrix-themed
terminal UI. The user answers a few questions; Chrono builds a full weekly
schedule and produces three outputs: Google Calendar events, a styled
Excel planner, and a natural-language explanation.

Runs **two ways from ONE codebase**:
- **Desktop (local, clone-and-run):** `python app.py` — Eel launches a
  local Chrome window talking to Python on the same machine.
- **Web (hosted, recruiter-facing demo):** `uvicorn web_server:app` —
  FastAPI serves the same frontend over HTTP.

The frontend (`web/app.js`) **auto-detects** which backend it's on and
routes calls accordingly. DO NOT break either path when editing app.js.

---

## Architecture / pipeline

```
Onboarding → Task Analysis → Optimization → 3 Outputs
                (Groq LLM)    (deterministic)   ├─ Google Calendar
                                                 ├─ Excel planner
                                                 └─ NL explanation
```

- **agents/** = the pipeline stages (the "thinking"):
  onboarding, task_analysis_agent, optimization_agent,
  deterministic_scheduler, explanation_agent.
- **tools/** = integrations + LLM clients (the "doing"):
  google_calendar, sheets_planner, groq_client, llm_backend,
  openrouter_client.
- **root** = entry points: app.py (Eel), web_server.py (FastAPI),
  web_google_auth.py (web OAuth), main.py (CLI).

**Imports across folders:** app.py and web_server.py add `agents/` and
`tools/` to `sys.path` at startup, so modules import by plain name
(`from llm_backend import call_llm`). No `__init__.py`, no relative
imports. If you add a new module, put it in the right folder and it just
works.

---

## KEY DECISIONS (don't undo these)

### 1. Scheduling is DETERMINISTIC Python, not LLM
`deterministic_scheduler.py` (`place_schedule`) does all task placement in
pure Python. The LLM is used ONLY for (a) task classification
(deep/light) in task_analysis_agent, and (b) the prose explanation.
**Why:** LLMs are bad at arithmetic/constraint-satisfaction — they'd
double-book, ignore breaks, or overflow the day. Deterministic placement
is reliable and instant. There's a legacy LLM-placement path behind
`LLM_PLACEMENT=1` but it's not the default. Don't "upgrade" the scheduler
to an LLM.

### 2. LLM providers & order
- **Groq** is primary (fast, ~394 tok/s). Model: `llama-3.3-70b-versatile`
  (set via `GROQ_MODEL` in config/.env).
- **OpenRouter** is the fallback. Model is PINNED to
  `meta-llama/llama-3.3-70b-instruct:free` — NOT the `openrouter/free`
  auto-router. **Why:** the auto-router picked reasoning models that
  leaked their scratchpad ("We need to produce a summary… \ \ \ …") as
  garbage output. Pinned instruct model = clean prose.
- **Explanation provider order is Groq → OpenRouter → deterministic
  fallback.** There's a `_looks_like_garbage()` guard that rejects
  reasoning-leak/backslash-spam and falls through. Keep it.
- Explanation is capped at ~300 tokens and asks for 1–2 short paragraphs
  (speed + no runaway).

### 3. Meals are USER-INPUT, not hardcoded
2nd onboarding question asks for meal times, any number (2/3/4+).
`parse_meals()` in onboarding.py handles `Breakfast (8:00 AM - 9:00 AM)`
(parens optional, custom names OK). Blank → `_DEFAULT_MEALS_FALLBACK`
(8AM/1PM/9PM). **Do not** re-hardcode meals in optimization_agent — it
reads `onboarding_result["meals"]`. Meals overlapping a commitment are
dropped (you eat around class, not on top of it).

### 4. Scheduler rules (in deterministic_scheduler.py / optimization_agent.py)
- `WAKE_BUFFER_MINUTES = 30` (no task the instant you wake)
- `MINIMUM_INTER_TASK_BREAK_MINUTES = 60` (between different tasks)
- `MINIMUM_SAME_TASK_GAP_MINUTES = 60`
- `_POST_BLOCK_BREAK_MINUTES`: 30 min after meals, 60 after gym/recurring
- Focus-span-based session splitting; deep work biased to mornings
- Pre-flight capacity check → returns a `capacity_error` with suggestions
  if the day can't fit (rather than silently overflowing)

### 5. Google Calendar quirks (learned the hard way)
- `calendarId="primary"` only reads the DEFAULT calendar, not named
  secondary ones — must list via `calendarList().list()`.
- `timeMin` must be LOCAL midnight, not UTC now (UTC midnight = 5:30 AM
  IST, silently drops same-day events).
- Unverified OAuth apps get 403 for the dev's own account unless added as
  a test user (desktop) / app is in Production (web).
- Scope changes require deleting and regenerating token.json.

---

## WEB / DEPLOYMENT

- **web_server.py** (FastAPI) mirrors the Eel bridge 1:1 but with
  **per-user sessions** (cookie `chrono_sid` → `_SESSIONS` dict). This is
  CRITICAL: a web server has concurrent users; a single global session
  (like the Eel app) would mix people's schedules. Don't revert to a
  global session on the web path.
- `_SESSIONS` is in-memory — fine for ONE free-tier instance (the demo).
  Needs Redis only if scaled to multiple instances.
- **web_google_auth.py** does the web OAuth REDIRECT flow (uses `Flow`,
  not `InstalledAppFlow`). Desktop uses the loopback flow. Different flows
  for different modes — don't merge them.
- `google_calendar.py` has a `set_service_override()` so the web server
  can inject a per-session Calendar service without rewriting every func.
- **Deploy:** Render/Railway, `uvicorn web_server:app --host 0.0.0.0
  --port $PORT`. Secrets as ENV VARS, never committed. See DEPLOY.md.
- OAuth env vars: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `GOOGLE_REDIRECT_URI`. If unset, the app says "calendar not configured"
  and everything else still works (calendar is optional).
- OAuth model: consent screen in **Production, unverified** (recruiters
  click through the "unverified app" warning). Max ~100 users is fine.
- Sign-in is an UPFRONT screen (after boot gate, before "Chrono is
  Online"), with a "Continue without calendar" skip. Not mid-flow.

---

## FRONTEND (web/app.js) — structure & gotchas

- **Single merged file:** faces + matrix rain + terminal engine are all in
  app.js (faces.js was merged in — a separate faces.js caused a blank
  screen when it failed to load). Don't split them back out.
- **Dual-mode `call()`:** detects Eel vs HTTP via `isEelMode()`, routes to
  `window.eel[fn]()` or `fetch('/api/...')` using the `_HTTP_ROUTES` map.
- **Boot sequence:** boot gate (unlocks audio) → sign-in screen → 2s wait
  → "Chrono is Online" voice → questions. Audio can't autoplay before a
  user gesture — that's why the boot gate exists.
- **Session token (`_sessionId`):** the Reload button restarts the flow
  WITHOUT reloading the page (so it skips the boot gate). A stale run
  detects a newer `_sessionId` and bails; `ask()` rejects with
  `__session_aborted__`. Don't remove this or reload will double-run.
- **Audio:** ONE reused typing-sound element (non-overlapping). Voice
  clips are separate and gated by mute. `say()` waits for typing to be
  idle so voice never overlaps typing. `sayOnLoop()` repeats a clip with a
  1s gap for long waits (calendar write). Mute stops ALL voice + the
  amplitude visualizer via `stopAllVoice()` + the `_activeLoops` registry.
- **Amplitude visualizers** flank the face; they animate on VOICE (not
  typing) — bound to audio play/ended.
- **Hover easter eggs:** Drazan badge → "He is my master"; GitHub/LinkedIn
  → "That's my master's profile". Respect mute; have a re-trigger guard.

---

## INPUT VALIDATION (onboarding.py `_validate_onboarding`)

Catches and reports (instead of silently proceeding with null):
- Unparseable wake/sleep → clear message, stops with reload prompt
- Sleep not after wake
- Meals that don't parse, don't end after start, or are >3h (catches the
  12:00 AM vs 12:00 PM typo)
- Time regex handles mixed-case AM/PM (`Pm`, `aM`) and no-space (`2:00PM`)

If you add questions, add matching validation — the UI shows `input_errors`
and stops rather than scheduling against garbage.

---

## BUGS WE ALREADY FIXED (don't reintroduce)

- **Dropped `wait()` function** when editing app.js → fatal "wait is not
  defined". Always re-check helper functions survive an edit.
- **Missing files in the deploy package** (deterministic_scheduler,
  llm_backend, groq_client, main, sheets_planner were left out twice).
  After any repackage, run `python -c "import web_server"` to confirm all
  imports resolve.
- **config/.env not loading:** it's at `config/.env`, so entry points call
  `load_dotenv(config/.env)` explicitly — plain `load_dotenv()` misses it.
- **Windows CRLF:** Windows editors save `\r\n`; normalize with
  `sed 's/\r$//'` if a file behaves oddly.
- **eel.js 404 in web mode** is EXPECTED (no Eel on the server). The
  `onerror` handler sets `window.__noEel=true`; `waitForEel()` then uses
  the HTTP check. If you see "eel not connected" in web mode, it's usually
  a BROWSER CACHE of an old app.js — hard-refresh (Ctrl+Shift+R).

---

## DEV PATTERNS / how to verify changes

- **Test the pipeline headless** by mocking the Google libs (types.
  ModuleType stubs) and a fake `analyze_tasks`, then run
  onboarding → build_schedule → fill_planner.
- **Verify xlsx** has zero formula errors via the xlsx skill's recalc.
- **Render the UI** with Playwright screenshots (mock `window.eel` /
  `window.Audio`) to check layout without a real display.
- **The developer:** GitHub Devashish-Rawat1, LinkedIn devashish-rawat01,
  app credit badge "Drazan". Windows / Git Bash / VS Code. Machine:
  i5-12450HX, 16GB RAM, RTX 3050 6GB.

---

## FUTURE / open items

- Full Google OAuth verification (Production-verified) if going past ~100
  users — currently Production-unverified.
- Real OAuth round-trip (button → Google → redirect → signed in) can only
  be tested on the live deployment with real creds — most likely spot to
  need a small fix on first live run.
- keep-alive cron (GitHub Actions) to avoid free-tier cold starts.
- Gemini is a candidate backend (key available; llm_backend indirection
  is ready) but not wired in.
- PyInstaller `.exe` for desktop distribution (`--icon
  web/assets/chrono-icon.ico`).