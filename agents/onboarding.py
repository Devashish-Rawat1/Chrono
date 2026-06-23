"""
Onboarding flow — the entry point of Chrono.

Decides between two paths based on whether the user's real calendar
already has events:

  Case 1 (empty calendar):  ask wake/sleep window, recurring commitments,
                             and tasks for the week — 3 questions.

  Case 2 (populated calendar): busy/free is already known from the
                             Calendar Agent, so we only ask for tasks —
                             1 question, with an optional vague-task
                             follow-up.

Returns a single structured dict that every downstream agent
(Task Analysis, Optimization, Routine, Replanning) consumes as its
starting input. This module does not call any LLM — it only collects
and structures input. Classification happens in the Task Analysis Agent.
"""

import re
import sys
import os
import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "tools"))
from google_calendar import get_events, get_source_calendar_id, find_free_slots


# ── Case detection ────────────────────────────────────────────────────────

def detect_case(days_ahead: int = 7) -> dict:
    """
    Checks the user's real (non-Chrono) calendar to decide which
    onboarding path applies.

    Returns:
        {"case": 1 | 2, "calendar_id": str, "events": list, "free_slots": dict}
    """
    calendar_id = get_source_calendar_id()
    events = get_events(days_ahead=days_ahead, calendar_id=calendar_id)

    if len(events) == 0:
        return {"case": 1, "calendar_id": calendar_id, "events": [], "free_slots": {}}

    free_slots = find_free_slots(days_ahead=days_ahead, calendar_id=calendar_id)
    return {"case": 2, "calendar_id": calendar_id, "events": events, "free_slots": free_slots}


# ── Input parsing helpers ────────────────────────────────────────────────

_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\s*[-–to]+\s*(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)"
)


def parse_wake_sleep(raw: str) -> dict:
    """
    Parses an answer like "7:00 AM - 11:00 PM" into a structured window.
    Falls back to raw text if parsing fails, so nothing is silently lost.
    """
    match = _TIME_RANGE_RE.search(raw)
    if match:
        return {"wake": match.group(1).strip(), "sleep": match.group(2).strip(), "raw": raw}
    return {"wake": None, "sleep": None, "raw": raw}


def parse_recurring_commitments(raw: str) -> list[dict]:
    """
    Parses free-text recurring commitments into structured entries.
    Expects roughly one commitment per line, e.g.:
        "College: Mon-Fri 9 AM - 3 PM"
        "Gym: Mon/Wed/Fri 6 PM - 7 PM"

    Lines that don't match the "Name: days time-time" shape are kept
    as raw text so nothing the user typed gets silently dropped —
    the Task Analysis Agent can still make sense of it via LLM reasoning.
    """
    commitments = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        if ":" in line:
            name, rest = line.split(":", 1)
        else:
            name, rest = line, ""

        time_match = _TIME_RANGE_RE.search(rest)
        commitments.append(
            {
                "name": name.strip(),
                "days_raw": rest.strip(),
                "start": time_match.group(1).strip() if time_match else None,
                "end": time_match.group(2).strip() if time_match else None,
                "raw": line,
            }
        )
    return commitments


_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_DEADLINE_RE = re.compile(
    r"(?:due|deadline)\s*:?\s*"
    r"(?:in\s+(\d+)\s*days?"                              # "due in 3 days"
    r"|(\d{4}-\d{2}-\d{2})"                                 # "due 2026-06-25"
    r"|([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?"             # "due June 25"
    r"|([A-Za-z]+))",                                       # "due Friday"
    re.IGNORECASE,
)


def _resolve_deadline(match: re.Match, today: datetime.date) -> datetime.date | None:
    """
    Converts a matched deadline expression into an actual date.
    Supports: "in N days", an ISO date, "Month Day", or a weekday name
    (resolved to the next upcoming occurrence of that weekday).
    """
    days_in, iso_date, month_name, month_day, weekday_name = match.groups()

    if days_in:
        return today + datetime.timedelta(days=int(days_in))

    if iso_date:
        try:
            return datetime.date.fromisoformat(iso_date)
        except ValueError:
            return None

    if month_name and month_day:
        try:
            month_num = datetime.datetime.strptime(month_name[:3], "%b").month
        except ValueError:
            return None
        year = today.year
        try:
            candidate = datetime.date(year, month_num, int(month_day))
        except ValueError:
            return None
        if candidate < today:
            candidate = candidate.replace(year=year + 1)
        return candidate

    if weekday_name:
        key = weekday_name.lower()
        if key not in _WEEKDAYS:
            return None
        target = _WEEKDAYS[key]
        days_ahead = (target - today.weekday()) % 7
        days_ahead = days_ahead or 7  # "due Friday" said today on a Friday means next Friday
        return today + datetime.timedelta(days=days_ahead)

    return None


def _extract_deadline(line: str) -> tuple[str, datetime.date | None]:
    """
    Looks for a deadline clause anywhere in the line and returns
    (line_with_deadline_clause_removed, resolved_date_or_None).
    A task with no deadline clause simply gets (line, None) — this is
    NOT treated as vague; ongoing/recurring tasks legitimately have no
    deadline.
    """
    match = _DEADLINE_RE.search(line)
    if not match:
        return line, None

    today = datetime.date.today()
    resolved = _resolve_deadline(match, today)

    start, end = match.start(), match.end()

    # If the deadline clause sits inside a parenthetical — e.g.
    # "Amazon ML Test (due in 3 days)" or "Project-D (10 hrs/week, due 2026-06-25)"
    # — widen the removed span to also eat an immediately preceding "(" or
    # ", " and an immediately following ")", so no orphaned punctuation
    # is left behind in either position.
    if start > 0 and line[start - 1] == "(":
        start -= 1
    elif start >= 2 and line[start - 2 : start] == ", ":
        start -= 2

    if end < len(line) and line[end] == ")":
        end += 1

    cleaned = (line[:start] + line[end:]).strip()
    cleaned = re.sub(r"\(\s*\)", "", cleaned).strip()  # any now-empty "()" left over
    cleaned = re.sub(r"[,\s]+$", "", cleaned).strip()  # trailing comma/space

    return cleaned, resolved


def _strip_parenthetical(line: str, match_start: int) -> str:
    """
    Cleans the task name by removing the trailing parenthetical detail
    (hours/frequency info) the regex matched inside. Handles both:
        "Gym (4 times/week)"   -> "Gym"
        "DSA Practice 2 hrs/day" (no parens) -> "DSA Practice"
    """
    name = line[:match_start].strip()
    name = name.rstrip("(").strip()
    return name


def _derive_urgency(deadline: datetime.date | None) -> str | None:
    """
    Maps a resolved deadline date into a coarse urgency label based on
    days remaining. Returns None if there's no deadline at all — urgency
    only applies to tasks with a real due date.
    """
    if deadline is None:
        return None
    days_left = (deadline - datetime.date.today()).days
    if days_left <= 1:
        return "high"
    if days_left <= 3:
        return "medium"
    return "low"


def parse_tasks(raw: str) -> list[dict]:
    """
    Parses free-text tasks/goals into structured entries.
    Expects roughly one task per line, e.g.:
        "- DSA Practice (2 hrs/day)"
        "- Build ChronoKai (10 hrs/week)"
        "- Gym (4 times/week)"
        "- Amazon ML Test (due in 3 days)"
        "- Submit report (due Friday)"

    Recognized duration patterns:
      - "<N> hrs/day" or "<N> hrs/week"  -> hours-based task
      - "<N> times/week"                 -> frequency-based task (no
        duration given; the Optimization Agent decides session length)

    Recognized deadline patterns (checked independently of duration,
    since a task can have both, either, or neither):
      - "due in N days"
      - "due 2026-06-25" (ISO date)
      - "due June 25" / "deadline June 25"
      - "due Friday" / "deadline Friday" (resolves to the next
        upcoming occurrence of that weekday)

    A task with no recognized duration pattern (hours or frequency) is
    flagged as vague so the caller can trigger a follow-up question.
    A task with no deadline clause is NOT vague — ongoing/recurring
    tasks legitimately have no deadline.
    """
    hours_re = re.compile(r"(\d+(?:\.\d+)?)\s*hrs?\s*/\s*(day|week)", re.IGNORECASE)
    freq_re = re.compile(r"(\d+(?:\.\d+)?)\s*(?:times|x)\s*/\s*week", re.IGNORECASE)

    tasks = []
    for raw_line in raw.strip().splitlines():
        line = raw_line.strip().lstrip("-•").strip()
        if not line:
            continue

        # Deadline is extracted first and stripped out, so it doesn't
        # interfere with the hours/frequency matching below.
        line, deadline = _extract_deadline(line)
        urgency = _derive_urgency(deadline)

        hours_match = hours_re.search(line)
        freq_match = freq_re.search(line) if not hours_match else None

        if hours_match:
            amount, period = hours_match.groups()
            name = _strip_parenthetical(line, hours_match.start())
            tasks.append(
                {
                    "name": name,
                    "hours": float(amount),
                    "period": period.lower(),
                    "deadline": deadline.isoformat() if deadline else None,
                    "urgency": urgency,
                    "vague": False,
                    "raw": raw_line.strip(),
                }
            )
        elif freq_match:
            count = freq_match.group(1)
            name = _strip_parenthetical(line, freq_match.start())
            tasks.append(
                {
                    "name": name,
                    "hours": None,
                    "period": "week",
                    "frequency_per_week": float(count),
                    "deadline": deadline.isoformat() if deadline else None,
                    "urgency": urgency,
                    "vague": False,
                    "raw": raw_line.strip(),
                }
            )
        else:
            tasks.append(
                {
                    "name": line,
                    "hours": None,
                    "period": None,
                    "deadline": deadline.isoformat() if deadline else None,
                    "urgency": urgency,
                    "vague": True,
                    "raw": raw_line.strip(),
                }
            )

    return tasks


def needs_followup(tasks: list[dict]) -> list[dict]:
    """Returns the subset of tasks that are vague and need a follow-up question."""
    return [t for t in tasks if t["vague"]]


# ── Orchestration ─────────────────────────────────────────────────────────

def run_onboarding(answers: dict) -> dict:
    """
    Takes raw answers collected from the user (via chat, CLI, or any UI)
    and returns one structured onboarding result for downstream agents.

    Expected keys in `answers`, depending on case:
      Case 1: "wake_sleep", "recurring_commitments", "tasks"
      Case 2: "tasks"  (and optionally "task_followups": {task_name: hours})

    Returns:
        {
          "case": 1 or 2,
          "schedule_window": {"wake": ..., "sleep": ...} | None,
          "recurring_commitments": [...] | None,
          "free_slots": {...} | None,        # only populated for case 2
          "tasks": [...],
          "needs_followup": [...]            # tasks still missing hours
        }
    """
    case_info = detect_case()
    result = {
        "case": case_info["case"],
        "schedule_window": None,
        "recurring_commitments": None,
        "focus_span_hours": None,
        "free_slots": case_info.get("free_slots"),
        "tasks": [],
        "needs_followup": [],
    }

    if case_info["case"] == 1:
        result["schedule_window"] = parse_wake_sleep(answers.get("wake_sleep", ""))
        result["recurring_commitments"] = parse_recurring_commitments(
            answers.get("recurring_commitments", "")
        )

    # Focus span is asked in both cases — it's a personal work-style
    # preference, not something that depends on calendar state. Defaults
    # to 2 hours if unparseable, a reasonable general assumption, rather
    # than leaving it None and silently skipping the splitting logic.
    focus_raw = answers.get("focus_span", "").strip()
    try:
        result["focus_span_hours"] = float(focus_raw)
    except ValueError:
        result["focus_span_hours"] = 2.0

    tasks = parse_tasks(answers.get("tasks", ""))

    # Apply any follow-up answers (task name -> hours) to resolve vague tasks
    followups = answers.get("task_followups", {})
    for task in tasks:
        if task["vague"] and task["name"] in followups:
            task["hours"] = float(followups[task["name"]])
            task["period"] = "week"
            task["vague"] = False

    result["tasks"] = tasks
    result["needs_followup"] = needs_followup(tasks)

    return result


# ── Interactive CLI collection ──────────────────────────────────────────

def _collect_singleline(prompt: str, example: str) -> str:
    """
    Collects a single-line answer from the terminal — used for questions
    that have exactly one expected answer (e.g. wake/sleep window), where
    "one item per line, blank line to finish" instructions would be
    confusing since there's nothing to list.
    """
    print(f"  Q: {prompt}")
    print(f"     e.g. {example}\n")
    return input("  > ").strip()


def _collect_multiline(prompt: str, example: str) -> str:
    """
    Collects a multi-line answer from the terminal. The user types one
    item per line, matching the example shown, and finishes with an
    empty line (just pressing Enter on a blank line).
    """
    print(f"  Q: {prompt}")
    print(f"     e.g. {example}")
    print("     (follow the example format above, one item per line — press Enter on a blank line when done)\n")

    lines = []
    while True:
        line = input("  > ")
        if line.strip() == "":
            break
        lines.append(line)

    return "\n".join(lines)


def collect_answers_interactively(case: int) -> dict:
    """
    Runs the real interactive prompt sequence for the given case and
    returns the raw answers dict, ready to pass into run_onboarding().
    Each question declares whether it expects a single line or a list
    of lines, so the on-screen instructions always match what's
    actually being asked.
    """
    answers = {}
    questions = ONBOARDING_QUESTIONS_CASE_1 if case == 1 else ONBOARDING_QUESTIONS_CASE_2

    for q in questions:
        print()
        if q.get("multiline", True):
            answers[q["key"]] = _collect_multiline(q["question"], q["example"])
        else:
            answers[q["key"]] = _collect_singleline(q["question"], q["example"])

    return answers


def run_followups_interactively(parsed: dict) -> dict:
    """
    If any tasks came back vague, asks one follow-up question per vague
    task and merges the hours back into the parsed result. Returns the
    updated parsed dict.
    """
    if not parsed["needs_followup"]:
        return parsed

    print("\nA couple of your tasks need more detail:\n")
    followups = {}
    for task in parsed["needs_followup"]:
        raw = input(f"  How many hours would you like to spend on '{task['name']}' this week? ").strip()
        try:
            followups[task["name"]] = float(raw)
        except ValueError:
            print(f"  Couldn't read that as a number — leaving '{task['name']}' as-is.")

    for task in parsed["tasks"]:
        if task["name"] in followups:
            task["hours"] = followups[task["name"]]
            task["period"] = "week"
            task["vague"] = False

    parsed["needs_followup"] = needs_followup(parsed["tasks"])
    return parsed


# ── Question text (for whichever UI calls this — CLI, chat, etc.) ────────

ONBOARDING_QUESTIONS_CASE_1 = [
    {
        "key": "wake_sleep",
        "question": "What time do you usually wake up and go to sleep?",
        "example": "7:00 AM - 11:00 PM",
        "multiline": False,
    },
    {
        "key": "recurring_commitments",
        "question": "Do you have any fixed commitments that repeat weekly?",
        "example": "College: Mon-Fri 9 AM - 3 PM\nGym: Mon/Wed/Fri 6 PM - 7 PM",
    },
    {
        "key": "focus_span",
        "question": "How many hours can you focus continuously before needing a break?",
        "example": "2",
        "multiline": False,
    },
    {
        "key": "tasks",
        "question": "What tasks or goals would you like to schedule this week?",
        "example": "- DSA Practice (2 hrs/day)\n- Build ChronoKai (10 hrs/week)\n- Read ML papers (3 hrs/week)",
    },
]

ONBOARDING_QUESTIONS_CASE_2 = [
    {
        "key": "focus_span",
        "question": "How many hours can you focus continuously before needing a break?",
        "example": "2",
        "multiline": False,
    },
    {
        "key": "tasks",
        "question": "What tasks or goals would you like me to schedule?",
        "example": "- DSA Practice (10 hrs/week)\n- Gym (4 times/week)\n- AI Project (15 hrs/week)",
    },
]


if __name__ == "__main__":
    info = detect_case()
    print(f"Detected case: {info['case']}\n")

    if info["case"] == 1:
        print("Calendar is empty. Running the 3-question setup.")
    else:
        print(f"Calendar already has {len(info['events'])} events. Free/busy already known.")
        print("Just need your tasks for the week.")

    raw_answers = collect_answers_interactively(info["case"])
    parsed = run_onboarding(raw_answers)
    parsed = run_followups_interactively(parsed)

    print("\n--- Final onboarding result ---")
    print(parsed)
