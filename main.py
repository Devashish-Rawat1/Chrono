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

from onboarding import detect_case, collect_answers_interactively, run_onboarding, run_followups_interactively
from task_analysis_agent import analyze_tasks, run_clarifications_interactively
from optimization_agent import build_schedule
from llm_backend import current_backend


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

    info = detect_case()
    print(f"\nDetected case: {info['case']}")

    if info["case"] == 1:
        print("Your calendar is empty — running the 3-question setup.\n")
    else:
        print(f"Your calendar already has {len(info['events'])} events. Free/busy already known.\n")

    raw_answers = collect_answers_interactively(info["case"])
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

    return onboarding_result


if __name__ == "__main__":
    if not os.environ.get("GROQ_API_KEY"):
        print(
            "Warning: GROQ_API_KEY not found. Make sure config/.env (or a "
            ".env file at the project root) contains:\n"
            "  GROQ_API_KEY=your-key-here\n"
        )

    run()
