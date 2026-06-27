"""
Output Stage 3 — natural-language schedule explanation.

Turns the finished schedule into a short, friendly "here's your week"
summary in plain English. This is a GOOD use of the LLM: it's a
generative writing task, not the exact arithmetic that placement needs,
so there's no correctness requirement and a transient Groq failure just
means "no narrative this run" rather than a broken schedule.

The LLM is given a compact, already-computed digest of the week (per-day
task hours, free time, fixed commitments, the wake/sleep window) rather
than the raw block list, so it explains real numbers instead of having
to derive them. A deterministic fallback produces a plain summary if the
LLM call fails for any reason.
"""

from llm_backend import call_llm


SYSTEM_PROMPT = """You are the Explanation component of an AI weekly planner called Chrono.

You are given a structured digest of a person's generated weekly schedule:
their waking window, the tasks scheduled (with hours), fixed commitments,
meals, and how much free time each day has.

Write a short, warm, encouraging summary of their week in plain English —
the kind of thing a thoughtful planning assistant would say when handing
over a finished schedule. Cover:
  - the overall shape of the week (how the tasks are distributed),
  - roughly how deep-focus work is positioned in the day,
  - how much free time they have and on which days it's tighter,
  - one or two gentle, practical observations or tips.

Rules:
  - Keep it to 2-4 short paragraphs. Be concise; do not pad.
  - Use the real numbers from the digest. Never invent tasks, times, or
    commitments that aren't in the digest.
  - Warm and human, not robotic or salesy. No emojis, no headers, no
    bullet lists — just clean prose.
  - Address the person directly as "you".
  - Do not restate the entire schedule slot by slot; summarize and
    interpret it.

Respond with only the summary text, nothing else."""


def _parse_hhmm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def _hours_label(minutes: int) -> str:
    hours = minutes / 60
    return f"{int(hours)}h" if hours == int(hours) else f"{hours:.1f}h"


_WEEKDAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _build_digest(schedule_result: dict) -> dict:
    """
    Condenses the schedule into a compact digest the LLM can explain:
    per-day task hours and free time, the set of fixed commitments, and
    the wake/sleep window. Computed here (not by the LLM) so the
    narrative is grounded in real numbers.
    """
    blocks = schedule_result.get("blocks", [])
    wake = schedule_result.get("wake_time")
    sleep = schedule_result.get("sleep_time")
    waking = (_parse_hhmm(sleep) - _parse_hhmm(wake)) if (wake and sleep) else None

    per_day = {}
    commitments = {}
    deep_work_starts = []

    for day in _WEEKDAY_ORDER:
        day_blocks = [b for b in blocks if b.get("day") == day]
        if not day_blocks:
            continue
        task_minutes = {}
        scheduled = 0
        for b in day_blocks:
            try:
                dur = _parse_hhmm(b["end"]) - _parse_hhmm(b["start"])
            except (KeyError, ValueError, AttributeError):
                continue
            scheduled += dur
            btype = b.get("type")
            if btype in ("deep_work", "light_work"):
                task_minutes[b["label"]] = task_minutes.get(b["label"], 0) + dur
                if btype == "deep_work":
                    deep_work_starts.append(_parse_hhmm(b["start"]))
            elif btype == "recurring":
                commitments.setdefault(b["label"], set()).add(day)
        free = max(0, waking - scheduled) if waking is not None else None
        per_day[day] = {
            "tasks": {n: _hours_label(m) for n, m in task_minutes.items()},
            "free": _hours_label(free) if free is not None else "unknown",
        }

    # Approximate "morning-ness" of deep work for a gentle observation.
    avg_deep_start = (sum(deep_work_starts) / len(deep_work_starts)) if deep_work_starts else None

    return {
        "wake": wake,
        "sleep": sleep,
        "per_day": per_day,
        "commitments": {name: sorted(days, key=_WEEKDAY_ORDER.index) for name, days in commitments.items()},
        "avg_deep_work_start_minutes": avg_deep_start,
    }


def _format_digest_for_prompt(digest: dict) -> str:
    """Renders the digest as a compact, readable text block for the LLM."""
    lines = [f"Waking window: {digest['wake']} to {digest['sleep']}", ""]
    lines.append("Per day:")
    for day, info in digest["per_day"].items():
        tasks = ", ".join(f"{n} {h}" for n, h in info["tasks"].items()) or "no tasks"
        lines.append(f"  {day}: {tasks}; free time {info['free']}")
    if digest["commitments"]:
        lines.append("")
        lines.append("Fixed commitments:")
        for name, days in digest["commitments"].items():
            lines.append(f"  {name}: {', '.join(days)}")
    return "\n".join(lines)


def _deterministic_summary(digest: dict) -> str:
    """
    Plain, no-LLM fallback summary. Always works, used if the Groq call
    fails. Reads a little dry compared to the LLM version, but is correct
    and never leaves the user with nothing.
    """
    per_day = digest["per_day"]
    if not per_day:
        return "No tasks were scheduled this week."

    # Tasks are the same set each day in the common case; collect them.
    all_tasks = {}
    for info in per_day.values():
        for name, h in info["tasks"].items():
            all_tasks.setdefault(name, h)

    frees = [(_parse_hhmm("00:00"), d, info["free"]) for d, info in per_day.items()]
    free_values = [info["free"] for info in per_day.values()]

    parts = []
    task_list = ", ".join(f"{n} ({h}/day)" for n, h in all_tasks.items())
    parts.append(
        f"Your week runs from {digest['wake']} to {digest['sleep']} each day, "
        f"with these tasks scheduled: {task_list}."
    )
    if digest["commitments"]:
        comms = "; ".join(f"{name} on {', '.join(days)}" for name, days in digest["commitments"].items())
        parts.append(f"Fixed commitments are kept in place: {comms}.")
    # Free-time range. uniq_free is a sorted list of hour LABELS like
    # "9.5h"; sort by the numeric value so the range reads low-to-high.
    def _label_to_hours(lbl):
        return float(lbl.rstrip("h"))
    uniq_free = sorted(set(free_values), key=_label_to_hours)
    if len(uniq_free) == 1:
        parts.append(f"You have about {uniq_free[0]} of free time each day after tasks, meals, and commitments.")
    else:
        parts.append(
            f"Free time ranges from about {uniq_free[0]} to {uniq_free[-1]} per day, "
            "tighter on days with extra commitments."
        )
    parts.append("Deep-focus work is placed earlier in the day where your energy is typically highest, with breaks between sessions.")
    return " ".join(parts)


def explain_schedule(schedule_result: dict) -> dict:
    """
    Produces a natural-language summary of the schedule.

    Returns:
        {"summary": str, "source": "llm" | "fallback"}
    """
    if not schedule_result.get("blocks") or schedule_result.get("failed"):
        return {"summary": "", "source": "none"}

    digest = _build_digest(schedule_result)
    user_prompt = (
        "Here is the digest of the generated weekly schedule. Write the summary:\n\n"
        + _format_digest_for_prompt(digest)
    )

    try:
        # Prose, not JSON — expect_json=False returns the raw text.
        text = call_llm(SYSTEM_PROMPT, user_prompt, expect_json=False)
        summary = text.strip() if isinstance(text, str) else str(text).strip()
        if not summary:
            raise ValueError("empty summary from LLM")
        return {"summary": summary, "source": "llm"}
    except Exception as e:
        print(f"  [Explanation] LLM unavailable ({e}); using a plain summary.")
        return {"summary": _deterministic_summary(digest), "source": "fallback"}


if __name__ == "__main__":
    # Standalone smoke test with a hand-made schedule result.
    sample = {
        "failed": False,
        "wake_time": "06:00",
        "sleep_time": "22:30",
        "blocks": [
            {"day": "Monday", "start": "09:00", "end": "11:00", "label": "DSA", "type": "deep_work"},
            {"day": "Monday", "start": "14:00", "end": "16:00", "label": "ML Revision", "type": "deep_work"},
            {"day": "Monday", "start": "16:00", "end": "17:00", "label": "Gym", "type": "recurring"},
            {"day": "Monday", "start": "08:00", "end": "09:00", "label": "Breakfast", "type": "meal"},
        ],
    }
    result = explain_schedule(sample)
    print(f"\n[source: {result['source']}]\n")
    print(result["summary"])