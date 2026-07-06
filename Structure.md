# Chrono — Project Structure

```
chrono_app/
│
├── app.py                  # Desktop entry point (Eel) — local clone-and-run
├── web_server.py           # Web entry point (FastAPI) — hosted live demo
├── web_google_auth.py      # Web OAuth redirect flow (hosted calendar)
├── main.py                 # CLI entry point (optional)
│
├── agents/                 # The pipeline stages (the "thinking")
│   ├── onboarding.py             # parse user inputs, meals, validation
│   ├── task_analysis_agent.py    # classify cognitive load / urgency (LLM)
│   ├── optimization_agent.py     # orchestrates scheduling + meals + breaks
│   ├── deterministic_scheduler.py# pure-Python placement engine
│   └── explanation_agent.py      # natural-language week summary
│
├── tools/                  # Integrations + LLM clients (the "doing")
│   ├── google_calendar.py        # Google Calendar read/write
│   ├── sheets_planner.py         # Excel planner generation (openpyxl)
│   ├── groq_client.py            # Groq API client
│   ├── llm_backend.py            # LLM router (Groq, indirection layer)
│   └── openrouter_client.py      # OpenRouter API client
│
├── web/                    # Frontend (served by both Eel and FastAPI)
│   ├── index.html
│   ├── app.js              # dual-mode: Eel locally / HTTP when hosted
│   ├── style.css
│   └── assets/             # audio clips, logo, icons
│
├── config/                 # YOU create this — never committed
│   ├── .env                # GROQ_API_KEY, OPENROUTER_API_KEY, GOOGLE_* vars
│   ├── credentials.json    # (desktop only) your Google OAuth client
│   └── token.json          # (desktop only) auto-created after first login
│
├── output/                 # Generated planners land here (auto-created)
│
├── requirements.txt        # Python dependencies
├── Procfile                # Start command for Render/Railway
├── DEPLOY.md               # Deployment + Google setup guide
├── GOOGLE_SETUP.md         # (desktop) bring-your-own-credentials guide
├── README.md
└── .gitignore              # excludes config/, output/, __pycache__

```

## How imports work across folders
Both entry points (`app.py`, `web_server.py`) add `agents/` and `tools/`
to `sys.path` at startup, so the modules import each other by plain name
(e.g. `from llm_backend import call_llm`) regardless of the subfolder.
No package __init__.py files or relative imports are needed.