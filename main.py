"""
Chrono — main entry point.

Loads environment variables from .env ONCE, here, before any agent or
tool module is imported. Every other file in this project reads secrets
via os.environ.get(...) and assumes they're already loaded — this file
is what actually loads them.

Run the full pipeline with:
    python main.py

As more agents get built, this file will grow into the real orchestrator
(calling onboarding -> task analysis -> optimization -> routine ->
replanning -> the three outputs). Right now it wires onboarding +
task analysis together as the first end-to-end slice.
"""

import sys
import os

from dotenv import load_dotenv

# Explicit path, not just load_dotenv() with no args — that version only
# looks in the current working directory, which silently fails to find
# the file if main.py is ever run from a different folder (e.g. via an
# IDE's "run" button, or from a parent directory). This always finds
# config/.env relative to this file's own location, regardless of cwd.
ENV_PATH = os.path.join(os.path.dirname(__file__), "config", ".env")
load_dotenv(dotenv_path=ENV_PATH)

sys.path.append(os.path.join(os.path.dirname(__file__), "agents"))
sys.path.append(os.path.join(os.path.dirname(__file__), "tools"))

from onboarding import collect_answers_interactively, run_onboarding, run_followups_interactively
from task_analysis_agent import analyze_tasks, run_clarifications_interactively
from optimization_agent import build_schedule
from llm_backend import current_backend
from google_calendar import write_schedule_to_calendar
from sheets_planner import fill_planner


def _print_schedule(schedule_result: dict) -> None:
    """Prints the final schedule grouped by day, in chronological order."""
    if schedule_result.get("capacity_error"):
        error = schedule_result["capacity_error"]
        print(f"\n{error['message']}\n")
        print("Suggestions:")
        for i, suggestion in enumerate(error["suggestions"], start=1):
            print(f"  {i}. {suggestion}")
        return

    if schedule_result.get("failed"):
        print("\nCouldn't build a valid schedule after multiple attempts.")
        print("Unresolved issues:")
        for p in schedule_result.get("validation_problems", []):
            print(f"  - {p}")
        print("\nThis can happen with very tight days or a temporary Groq API issue. Try running again.")
        return

    if not schedule_result.get("blocks"):
        print("\nNo schedule was generated and no specific reason was returned.")
        print(f"Raw result for debugging: {schedule_result}")
        return

    current_day = None
    for block in schedule_result["blocks"]:
        if block["day"] != current_day:
            current_day = block["day"]
            print(f"\n{current_day}")
        print(f"  {block['start']}-{block['end']}  {block['label']}  ({block['type']})")

    if schedule_result.get("notes"):
        print(f"\nNotes: {schedule_result['notes']}")
    if schedule_result.get("validation_problems"):
        print(f"\nUnresolved issues: {schedule_result['validation_problems']}")


def run() -> dict:
    """
    Runs onboarding through optimization and returns the final result.
    This is the first end-to-end slice of the full pipeline — routine and
    replanning agents, plus the three output stages, plug in after this
    once they exist.
    """
    print("=" * 60)
    print("  Chrono — Weekly Planning Setup")
    print(f"  LLM backend: {current_backend()}")
    print("=" * 60)
    print("\nLet's set up your week.\n")

    raw_answers = collect_answers_interactively()
    onboarding_result = run_onboarding(raw_answers)
    onboarding_result = run_followups_interactively(onboarding_result)

    print("\n" + "=" * 60)
    print("  Analyzing tasks...")
    print("=" * 60 + "\n")

    analysis = analyze_tasks(onboarding_result["tasks"])
    analysis = run_clarifications_interactively(analysis)

    onboarding_result["tasks"] = analysis["tasks"]

    print("\n" + "=" * 60)
    print("  Building your schedule...")
    print("=" * 60)

    schedule_result = build_schedule(onboarding_result)
    _print_schedule(schedule_result)

    onboarding_result["schedule"] = schedule_result

    # Output Stage 1: write the schedule to Google Calendar. Only attempt
    # this when there's a real, valid schedule to write -- a failed build,
    # a capacity error, or an empty result has nothing to put on the
    # calendar, and we shouldn't wipe the existing Chrono calendar for it.
    schedulable = (
        schedule_result.get("blocks")
        and not schedule_result.get("failed")
        and not schedule_result.get("capacity_error")
    )
    if schedulable:
        _maybe_write_to_calendar(schedule_result)
        _maybe_write_planner(schedule_result)

    return onboarding_result


# Where the Student Schedule template lives, and where filled planners go.
_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "config", "Student-Schedule-Template.xlsx")
_PLANNER_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")


def _maybe_write_planner(schedule_result: dict) -> None:
    """
    Output Stage 2: asks whether to generate the styled weekly planner
    spreadsheet (a filled copy of the Student Schedule template) the user
    can download, then writes it to the output/ folder.
    """
    print("\n" + "=" * 60)
    print("  Download weekly planner (Excel)")
    print("=" * 60)

    if not os.path.exists(_TEMPLATE_PATH):
        print(
            f"\nPlanner template not found at {_TEMPLATE_PATH}.\n"
            "Place the Student Schedule template there (named "
            "'Student-Schedule-Template.xlsx') to enable the downloadable planner."
        )
        return

    answer = input("\nGenerate a downloadable Excel planner of this schedule? (y/n): ").strip().lower()
    if answer not in ("y", "yes"):
        print("Skipped — no planner file was created.")
        return

    output_path = os.path.join(_PLANNER_OUTPUT_DIR, "Chrono-Weekly-Planner.xlsx")
    try:
        result = fill_planner(
            schedule_result["blocks"],
            template_path=_TEMPLATE_PATH,
            output_path=output_path,
            wake_time=schedule_result.get("wake_time"),
            sleep_time=schedule_result.get("sleep_time"),
        )
    except Exception as e:
        print(f"\nCouldn't generate the planner: {e}")
        return

    print(f"\nDone. Wrote {result['written']} entries to your planner.")
    print(f"Saved to: {result['output_path']}")
    if result["skipped"]:
        print(f"Note: {len(result['skipped'])} block(s) couldn't be placed:")
        for s in result["skipped"]:
            print(f"  - {s['reason']}")


def _maybe_write_to_calendar(schedule_result: dict) -> None:
    """
    Asks the user whether to publish the schedule to Google Calendar,
    then (on yes) wipes the dedicated Chrono calendar's events for the
    week and writes the fresh schedule. Kept as a confirmation step
    because writing involves DELETING the previous run's events -- a
    destructive action the user should green-light, not have happen
    silently on every run.
    """
    print("\n" + "=" * 60)
    print("  Publish to Google Calendar")
    print("=" * 60)
    answer = input(
        "\nWrite this schedule to your 'Chrono Schedule' calendar?\n"
        "This wipes that calendar's events for the week and replaces them "
        "with the schedule above. (y/n): "
    ).strip().lower()

    if answer not in ("y", "yes"):
        print("Skipped — nothing was written to your calendar.")
        return

    try:
        result = write_schedule_to_calendar(
            schedule_result["blocks"],
            wake_time=schedule_result.get("wake_time"),
            sleep_time=schedule_result.get("sleep_time"),
        )
    except Exception as e:
        print(f"\nCouldn't write to Google Calendar: {e}")
        print(
            "Your schedule above is still valid — this only affects publishing it. "
            "Check that config/credentials.json exists and you've authorized access."
        )
        return

    print(f"\nDone. Cleared {result['deleted']} old event(s), wrote {result['created']} new one(s).")
    _print_clickable_link("Open your Chrono calendar", result["calendar_link"])
    if result["skipped"]:
        print(f"Note: {len(result['skipped'])} block(s) couldn't be written:")
        for s in result["skipped"]:
            print(f"  - {s['reason']}")


def _print_clickable_link(label: str, url: str) -> None:
    """
    Prints a terminal hyperlink. Terminals that support the OSC 8 escape
    sequence (most modern ones — Windows Terminal, iTerm2, GNOME Terminal,
    VS Code's terminal) render `label` as clickable text linking to `url`.
    For terminals that DON'T support OSC 8 (e.g. older Git Bash / mintty),
    the escape codes are harmless but invisible-looking, so the raw URL is
    ALSO printed on its own line underneath -- a bare URL alone on a line,
    with no surrounding punctuation, is what most terminals auto-detect
    and make Ctrl/Cmd-clickable. Belt and suspenders so the link is
    reachable no matter the terminal.
    """
    # OSC 8 hyperlink: ESC ] 8 ; ; <url> ESC \  <label>  ESC ] 8 ; ; ESC \
    esc = "\033"
    hyperlink = f"{esc}]8;;{url}{esc}\\{label}{esc}]8;;{esc}\\"
    print(f"\n{hyperlink}")
    print(url)


if __name__ == "__main__":
    if not os.environ.get("GROQ_API_KEY"):
        print(
            "Warning: GROQ_API_KEY not found. Make sure config/.env (or a "
            ".env file at the project root) contains:\n"
            "  GROQ_API_KEY=your-key-here\n"
        )

    run()