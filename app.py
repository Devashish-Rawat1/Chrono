"""
Chrono desktop app — Eel bridge.

This is the entry point for the Matrix-terminal desktop UI. It keeps the
existing Chrono pipeline (onboarding -> task analysis -> deterministic
scheduling -> explanation -> calendar -> planner) completely intact and
exposes each stage to the JavaScript frontend via Eel.

The interactive CLI helpers in the agents use input(), which can't work
inside a webview, so this bridge calls the NON-interactive core functions
directly and lets the JS terminal drive the question/answer flow instead.

Run with:  python app.py
"""

import os
import sys
import traceback

import eel

# Make sure the agents/tools import cleanly whether they live flat (as in
# the app bundle) or under agents/ and tools/ (as in the dev project).
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agents"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tools"))

from dotenv import load_dotenv
# The project keeps its secrets in config/.env (not the project root), so
# point python-dotenv there explicitly. Fall back to the default search
# (cwd / parents) if that file isn't present, so a root .env still works.
_ENV_PATH = os.path.join(os.path.dirname(__file__), "config", ".env")
if os.path.exists(_ENV_PATH):
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()

from onboarding import run_onboarding
from task_analysis_agent import analyze_tasks
from optimization_agent import build_schedule
from explanation_agent import explain_schedule
from google_calendar import write_schedule_to_calendar
from sheets_planner import fill_planner

# Where generated planners are written.
_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# Session state, kept server-side between the JS calls of a single run so
# the frontend doesn't have to shuttle the whole schedule back and forth.
_SESSION = {
    "onboarding": None,
    "schedule": None,
}


def _safe(fn):
    """
    Wraps an exposed function so any exception becomes a clean
    {"ok": False, "error": "..."} for the frontend, instead of a raw
    Python traceback vanishing into the void. Keeps the terminal UI
    able to show a tidy error line rather than freezing.
    """
    def wrapper(*args, **kwargs):
        try:
            return {"ok": True, "data": fn(*args, **kwargs)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}
    wrapper.__name__ = fn.__name__
    return wrapper


@eel.expose
@_safe
def api_backend_name():
    """Which LLM backend is active, for the intro banner."""
    from llm_backend import current_backend
    return current_backend()


@eel.expose
@_safe
def api_process_onboarding(answers: dict):
    """
    Stage 1 — Onboarding. `answers` is the raw dict the JS terminal
    collected: {wake_sleep, recurring_commitments, focus_span, tasks}.
    Returns the structured onboarding result plus which tasks (if any)
    still need clarification, so the terminal can ask follow-ups.
    """
    onboarding = run_onboarding(answers)
    _SESSION["onboarding"] = onboarding
    return {
        "tasks": [t["name"] for t in onboarding.get("tasks", [])],
        "window": onboarding.get("schedule_window"),
        "needs_followup": onboarding.get("needs_followup", []),
    }


@eel.expose
@_safe
def api_apply_followups(followups: dict):
    """
    Applies task-hours follow-up answers (task name -> hours) collected by
    the terminal for vague tasks, updating the stored onboarding result.
    """
    onboarding = _SESSION.get("onboarding")
    if not onboarding:
        raise RuntimeError("No onboarding in progress.")
    for task in onboarding.get("tasks", []):
        if task.get("vague") and task["name"] in followups:
            task["hours"] = float(followups[task["name"]])
            task["period"] = "week"
            task["vague"] = False
    # Recompute any remaining follow-ups.
    from onboarding import needs_followup
    onboarding["needs_followup"] = needs_followup(onboarding["tasks"])
    return {"needs_followup": onboarding["needs_followup"]}


@eel.expose
@_safe
def api_analyze_tasks():
    """
    Stage 2 — Task Analysis. Classifies cognitive load / urgency via the
    LLM. Returns any clarification questions the terminal should ask.
    """
    onboarding = _SESSION.get("onboarding")
    if not onboarding:
        raise RuntimeError("No onboarding in progress.")
    analysis = analyze_tasks(onboarding["tasks"])
    # Merge the classifications back onto the onboarding tasks.
    by_name = {t["name"]: t for t in analysis.get("tasks", [])}
    for task in onboarding["tasks"]:
        enriched = by_name.get(task["name"])
        if enriched:
            task.update(enriched)
    return {
        "count": len(analysis.get("tasks", [])),
        "needs_clarification": analysis.get("needs_clarification", []),
    }


@eel.expose
@_safe
def api_apply_clarifications(clarifications: dict):
    """
    Applies the user's clarification answers DIRECTLY (name -> answer).

    The earlier version re-ran the LLM with the answer as a hint, but the
    LLM would sometimes re-classify from scratch and ignore what the user
    explicitly said. When a user answers "deep focus" or "light", that's
    an authoritative instruction -- we set cognitive_load from it and do
    NOT ask the model again. Free-text answers that aren't clearly deep or
    light are interpreted with a simple keyword check; anything still
    ambiguous defaults to "deep" (the safer choice for scheduling, since
    deep work gets the protected morning slots).
    """
    onboarding = _SESSION.get("onboarding")
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
            # Once the user has told us, it's settled -- clear any flag
            # that would make it look unclassified downstream.
            task["confidence"] = "high"
            task.pop("clarification", None)
    return {"ok": True}


@eel.expose
@_safe
def _api_apply_clarifications_via_llm(clarifications: dict):
    """(Kept for reference — the old LLM-re-run path, no longer used.)"""
    onboarding = _SESSION.get("onboarding")
    if not onboarding:
        raise RuntimeError("No onboarding in progress.")
    for task in onboarding["tasks"]:
        if task["name"] in clarifications:
            task["clarification"] = clarifications[task["name"]]
    analysis = analyze_tasks(onboarding["tasks"])
    by_name = {t["name"]: t for t in analysis.get("tasks", [])}
    for task in onboarding["tasks"]:
        enriched = by_name.get(task["name"])
        if enriched:
            task.update(enriched)
    return {"ok": True}


@eel.expose
@_safe
def api_build_schedule():
    """
    Stage 3 — Optimization. Runs the deterministic scheduler. Returns the
    placed blocks (or a capacity error with suggestions if it doesn't fit).
    """
    onboarding = _SESSION.get("onboarding")
    if not onboarding:
        raise RuntimeError("No onboarding in progress.")
    schedule = build_schedule(onboarding)
    _SESSION["schedule"] = schedule
    return {
        "failed": schedule.get("failed", False),
        "blocks": schedule.get("blocks", []),
        "capacity_error": schedule.get("capacity_error"),
        "validation_problems": schedule.get("validation_problems", []),
        "wake_time": schedule.get("wake_time"),
        "sleep_time": schedule.get("sleep_time"),
    }


@eel.expose
@_safe
def api_explain_schedule():
    """Stage 4 — Explanation. Natural-language 'here's your week' summary."""
    schedule = _SESSION.get("schedule")
    if not schedule:
        raise RuntimeError("No schedule built yet.")
    result = explain_schedule(schedule)
    return {"summary": result.get("summary", ""), "source": result.get("source", "none")}


@eel.expose
@_safe
def api_write_calendar():
    """Stage 5a — publish to Google Calendar (wipe & regenerate)."""
    schedule = _SESSION.get("schedule")
    if not schedule:
        raise RuntimeError("No schedule built yet.")
    result = write_schedule_to_calendar(
        schedule["blocks"],
        wake_time=schedule.get("wake_time"),
        sleep_time=schedule.get("sleep_time"),
    )
    return {
        "deleted": result.get("deleted", 0),
        "created": result.get("created", 0),
        "calendar_link": result.get("calendar_link", ""),
    }


@eel.expose
@_safe
def api_write_planner():
    """
    Stage 5b — generate the downloadable Excel planner. Falls back to a
    timestamped filename if the target file is open/locked. Returns the
    path so the frontend can offer a download.
    """
    schedule = _SESSION.get("schedule")
    if not schedule:
        raise RuntimeError("No schedule built yet.")
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(_OUTPUT_DIR, "Chrono-Weekly-Planner.xlsx")
    try:
        result = fill_planner(
            schedule["blocks"], template_path="", output_path=output_path,
            wake_time=schedule.get("wake_time"), sleep_time=schedule.get("sleep_time"),
        )
    except PermissionError:
        import datetime as _dt
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = os.path.join(_OUTPUT_DIR, f"Chrono-Weekly-Planner-{stamp}.xlsx")
        result = fill_planner(
            schedule["blocks"], template_path="", output_path=output_path,
            wake_time=schedule.get("wake_time"), sleep_time=schedule.get("sleep_time"),
        )
    _SESSION["last_planner_path"] = output_path
    return {"written": result.get("written", 0), "path": output_path}


@eel.expose
@_safe
def api_get_planner_download():
    """
    Reads the most recently generated planner file and returns it as
    base64 plus its filename, so the frontend can offer a real download
    (rather than just printing the on-disk path). Called when the user
    clicks the download button in the terminal.
    """
    import base64
    path = _SESSION.get("last_planner_path")
    if not path or not os.path.exists(path):
        raise RuntimeError("No planner file available to download.")
    with open(path, "rb") as f:
        data = f.read()
    return {
        "filename": os.path.basename(path),
        "b64": base64.b64encode(data).decode("ascii"),
    }


def main():
    eel.init(os.path.join(os.path.dirname(__file__), "web"))
    # A reasonable terminal-ish window size; the UI is responsive anyway.
    eel.start("index.html", size=(1000, 800), position=(120, 60))


if __name__ == "__main__":
    main()