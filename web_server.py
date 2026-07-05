"""
Chrono — FastAPI web server (hostable port of the Eel desktop bridge).

This serves the SAME frontend (web/) and exposes the SAME pipeline as the
Eel app (app.py), but over HTTP so it can be deployed to a host like
Render or Railway and reached by a browser. The Eel app remains for local
"clone and run" use; this is the recruiter-facing live-demo server.

Key difference from the Eel bridge: a web server has MANY concurrent
users, so session state can't be a single global dict. Each visitor gets
a session id (cookie); their onboarding/schedule live in a per-session
store, so two people using the live demo at once never see each other's
data.

Run locally:   uvicorn web_server:app --reload --port 8000
Run on a host: uvicorn web_server:app --host 0.0.0.0 --port $PORT
"""

import os
import sys
import uuid
import traceback

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Make the agents/tools importable whether flat or under agents/ and tools/.
_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "agents"))
sys.path.insert(0, os.path.join(_HERE, "tools"))

from dotenv import load_dotenv
_ENV_PATH = os.path.join(_HERE, "config", ".env")
if os.path.exists(_ENV_PATH):
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()

from onboarding import run_onboarding, needs_followup
from task_analysis_agent import analyze_tasks
from optimization_agent import build_schedule
from explanation_agent import explain_schedule
from sheets_planner import fill_planner

_OUTPUT_DIR = os.path.join(_HERE, "output")
_WEB_DIR = os.path.join(_HERE, "web")

app = FastAPI(title="Chrono")

# ── Per-session state ────────────────────────────────────────────────
# session_id -> {"onboarding":..., "schedule":..., "last_planner_path":...}
# In-memory is fine for a single-instance demo. (For multi-instance you'd
# move this to Redis, but the demo runs one instance.)
_SESSIONS: dict[str, dict] = {}
_COOKIE = "chrono_sid"


def _is_secure_request(request: Request) -> bool:
    """
    True if this request arrived over https. On Render/Railway the app sits
    behind a TLS-terminating proxy, so the socket scheme is http but the
    ORIGINAL scheme is in X-Forwarded-Proto. We check both so the Secure
    cookie flag is set in production (https) but NOT on local http testing
    (where a Secure cookie would be silently dropped and break sessions).
    """
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def _session_for(request: Request, response: Response) -> dict:
    """Gets (or creates) the per-visitor session, tracked by a cookie."""
    sid = request.cookies.get(_COOKIE)
    if not sid or sid not in _SESSIONS:
        sid = uuid.uuid4().hex
        _SESSIONS[sid] = {}
        # httponly cookie; SameSite=Lax is fine for same-origin fetch and
        # survives the top-level redirect back from Google. Secure is set
        # only on https (prod) so local http testing still works.
        response.set_cookie(
            _COOKIE, sid,
            httponly=True,
            samesite="lax",
            secure=_is_secure_request(request),
            max_age=60 * 60 * 6,
        )
    return _SESSIONS[sid]


def _ok(data):
    return {"ok": True, "data": data}


def _err(msg):
    return {"ok": False, "error": msg}


async def _read_json(request: Request) -> dict:
    """Reads a JSON body, tolerating an empty body (returns {})."""
    try:
        body = await request.body()
        if not body:
            return {}
        import json
        return json.loads(body)
    except Exception:
        return {}


# ── API endpoints (1:1 with the Eel bridge) ─────────────────────────

@app.get("/api/backend_name")
def api_backend_name():
    try:
        from llm_backend import current_backend
        return _ok(current_backend())
    except Exception as e:
        return JSONResponse(_err(str(e)))


@app.post("/api/process_onboarding")
async def api_process_onboarding(request: Request, response: Response):
    sess = _session_for(request, response)
    try:
        body = await _read_json(request)
        answers = body.get("answers", body)  # accept {answers:{...}} or {...}
        onboarding = run_onboarding(answers)
        sess["onboarding"] = onboarding
        return JSONResponse(_ok({
            "tasks": [t["name"] for t in onboarding.get("tasks", [])],
            "window": onboarding.get("schedule_window"),
            "needs_followup": onboarding.get("needs_followup", []),
            "input_errors": onboarding.get("input_errors", []),
        }), headers=dict(response.headers))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(_err(str(e)), headers=dict(response.headers))


@app.post("/api/apply_followups")
async def api_apply_followups(request: Request, response: Response):
    sess = _session_for(request, response)
    try:
        body = await _read_json(request)
        followups = body.get("followups", {})
        onboarding = sess.get("onboarding")
        if not onboarding:
            raise RuntimeError("No onboarding in progress.")
        for task in onboarding.get("tasks", []):
            if task.get("vague") and task["name"] in followups:
                task["hours"] = float(followups[task["name"]])
                task["period"] = "week"
                task["vague"] = False
        onboarding["needs_followup"] = needs_followup(onboarding["tasks"])
        return JSONResponse(_ok({"needs_followup": onboarding["needs_followup"]}), headers=dict(response.headers))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(_err(str(e)), headers=dict(response.headers))


@app.post("/api/analyze_tasks")
async def api_analyze_tasks(request: Request, response: Response):
    sess = _session_for(request, response)
    try:
        onboarding = sess.get("onboarding")
        if not onboarding:
            raise RuntimeError("No onboarding in progress.")
        analysis = analyze_tasks(onboarding["tasks"])
        by_name = {t["name"]: t for t in analysis.get("tasks", [])}
        for task in onboarding["tasks"]:
            enriched = by_name.get(task["name"])
            if enriched:
                task.update(enriched)
        return JSONResponse(_ok({
            "count": len(analysis.get("tasks", [])),
            "needs_clarification": analysis.get("needs_clarification", []),
        }), headers=dict(response.headers))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(_err(str(e)), headers=dict(response.headers))


@app.post("/api/apply_clarifications")
async def api_apply_clarifications(request: Request, response: Response):
    sess = _session_for(request, response)
    try:
        body = await _read_json(request)
        clarifications = body.get("clarifications", {})
        onboarding = sess.get("onboarding")
        if not onboarding:
            raise RuntimeError("No onboarding in progress.")

        def interpret(answer: str) -> str:
            a = (answer or "").strip().lower()
            light_words = ("light", "routine", "easy", "casual", "chill", "relax", "low")
            deep_words = ("deep", "focus", "hard", "intense", "heavy", "concentrat")
            if any(w in a for w in light_words) and not any(w in a for w in deep_words):
                return "light"
            return "deep"

        for task in onboarding["tasks"]:
            if task["name"] in clarifications:
                task["cognitive_load"] = interpret(clarifications[task["name"]])
                task["confidence"] = "high"
                task.pop("clarification", None)
        return JSONResponse(_ok({"ok": True}), headers=dict(response.headers))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(_err(str(e)), headers=dict(response.headers))


@app.post("/api/build_schedule")
async def api_build_schedule(request: Request, response: Response):
    sess = _session_for(request, response)
    try:
        onboarding = sess.get("onboarding")
        if not onboarding:
            raise RuntimeError("No onboarding in progress.")
        schedule = build_schedule(onboarding)
        sess["schedule"] = schedule
        return JSONResponse(_ok({
            "failed": schedule.get("failed", False),
            "blocks": schedule.get("blocks", []),
            "capacity_error": schedule.get("capacity_error"),
            "validation_problems": schedule.get("validation_problems", []),
            "wake_time": schedule.get("wake_time"),
            "sleep_time": schedule.get("sleep_time"),
        }), headers=dict(response.headers))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(_err(str(e)), headers=dict(response.headers))


@app.post("/api/explain_schedule")
async def api_explain_schedule(request: Request, response: Response):
    sess = _session_for(request, response)
    try:
        schedule = sess.get("schedule")
        if not schedule:
            raise RuntimeError("No schedule built yet.")
        result = explain_schedule(schedule)
        return JSONResponse(_ok({"summary": result.get("summary", ""), "source": result.get("source", "none")}), headers=dict(response.headers))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(_err(str(e)), headers=dict(response.headers))


@app.post("/api/write_calendar")
async def api_write_calendar(request: Request, response: Response):
    sess = _session_for(request, response)
    try:
        schedule = sess.get("schedule")
        if not schedule:
            raise RuntimeError("No schedule built yet.")

        # In hosted web mode, use THIS visitor's Google token (from the
        # OAuth flow) to build a per-session Calendar service and inject it
        # into the calendar module. If they haven't connected Google, tell
        # the frontend so it can prompt them.
        import web_google_auth as gauth
        token = sess.get("google_token")
        if not gauth.has_valid_session(token):
            return JSONResponse(
                _ok({"needs_auth": True, "auth_url": "/auth/google/login"}),
                headers=dict(response.headers),
            )

        from google_calendar import write_schedule_to_calendar, set_service_override, clear_service_override
        try:
            set_service_override(gauth.service_from_session(token))
            result = write_schedule_to_calendar(
                schedule["blocks"],
                wake_time=schedule.get("wake_time"),
                sleep_time=schedule.get("sleep_time"),
            )
        finally:
            clear_service_override()

        return JSONResponse(_ok({
            "deleted": result.get("deleted", 0),
            "created": result.get("created", 0),
            "calendar_link": result.get("calendar_link", ""),
        }), headers=dict(response.headers))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(_err(str(e)), headers=dict(response.headers))


# ── Google OAuth (web redirect flow) ────────────────────────────────

@app.get("/api/google_status")
def api_google_status(request: Request):
    """Tells the frontend whether Google is configured on the server and
    whether THIS visitor has connected their account."""
    import web_google_auth as gauth
    sid = request.cookies.get(_COOKIE)
    sess = _SESSIONS.get(sid or "", {})
    return _ok({
        "configured": gauth.is_configured(),
        "connected": gauth.has_valid_session(sess.get("google_token")),
    })


@app.get("/auth/google/login")
def auth_google_login(request: Request, response: Response):
    """Starts the OAuth flow: redirect the visitor to Google's consent page."""
    from fastapi.responses import RedirectResponse
    import web_google_auth as gauth
    sess = _session_for(request, response)
    try:
        auth_url, state, verifier = gauth.build_auth_url()
        sess["oauth_state"] = state
        sess["oauth_code_verifier"] = verifier  # needed at exchange time
        r = RedirectResponse(auth_url)
        for k, v in response.headers.items():
            if k.lower() == "set-cookie":
                r.headers.append("set-cookie", v)
        return r
    except Exception as e:
        return JSONResponse(_err(str(e)), status_code=500)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, response: Response):
    """Google redirects here with ?code=...; exchange it and store the token."""
    from fastapi.responses import RedirectResponse, HTMLResponse
    import web_google_auth as gauth
    sess = _session_for(request, response)
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(f"<p>Google sign-in was cancelled ({error}). You can close this tab.</p>")
    if not code:
        return HTMLResponse("<p>No authorization code returned. You can close this tab.</p>")
    try:
        verifier = sess.get("oauth_code_verifier")
        sess["google_token"] = gauth.exchange_code(code, code_verifier=verifier)
    except Exception as e:
        return HTMLResponse(f"<p>Sign-in failed: {e}</p>")
    # Back to the app; the frontend re-checks status and continues.
    r = RedirectResponse("/?google=connected")
    for k, v in response.headers.items():
        if k.lower() == "set-cookie":
            r.headers.append("set-cookie", v)
    return r


@app.post("/api/write_planner")
async def api_write_planner(request: Request, response: Response):
    sess = _session_for(request, response)
    try:
        schedule = sess.get("schedule")
        if not schedule:
            raise RuntimeError("No schedule built yet.")
        os.makedirs(_OUTPUT_DIR, exist_ok=True)
        # Per-session filename so concurrent users don't clobber each other.
        sid = request.cookies.get(_COOKIE, "anon")
        output_path = os.path.join(_OUTPUT_DIR, f"Chrono-Weekly-Planner-{sid[:8]}.xlsx")
        result = fill_planner(
            schedule["blocks"], template_path="", output_path=output_path,
            wake_time=schedule.get("wake_time"), sleep_time=schedule.get("sleep_time"),
        )
        sess["last_planner_path"] = output_path
        return JSONResponse(_ok({"written": result.get("written", 0), "path": output_path}), headers=dict(response.headers))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(_err(str(e)), headers=dict(response.headers))


@app.get("/api/download_planner")
def api_download_planner(request: Request):
    sid = request.cookies.get(_COOKIE)
    sess = _SESSIONS.get(sid or "", {})
    path = sess.get("last_planner_path")
    if not path or not os.path.exists(path):
        return JSONResponse(_err("No planner file available to download."), status_code=404)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="Chrono-Weekly-Planner.xlsx",
    )


# ── Static frontend (served last so /api/* wins) ─────────────────────
# index.html at "/", plus style.css, app.js, assets/...
app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")