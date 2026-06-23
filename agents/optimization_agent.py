"""
Optimization Agent — the core scheduling intelligence.

This agent does NOT decide schedule placement itself with hardcoded
rules. That was tried first and abandoned: rule tables for break length,
fixed meal slots that pushed tasks around mechanically, and a focus-span
limit would all just be more if-else branches — not genuine reasoning,
and brittle against any input shape that wasn't anticipated.

Instead: Python's job is to gather every real constraint (wake/sleep
window, meals, recurring commitments, the user's continuous-focus limit,
and the classified task list) into one clean structured prompt, hand the
actual placement decision to Groq, and then VALIDATE the result —
checking for overlaps, blocks running past sleep time, and meals/
commitments being respected — rather than silently trusting it. If
validation fails, one corrective retry is attempted before falling back
to flagging the problem rather than returning a broken schedule.

This mirrors how Task Analysis Agent already works: the LLM makes the
judgment call (cognitive load, urgency), Python only structures the
input and handles the failure modes.
"""

import sys
import os
import datetime
import re

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "tools"))
from llm_backend import call_llm, current_backend
from deterministic_scheduler import place_schedule

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Default meal windows — never asked about, just assumed. Groq is told
# about these as fixed, non-negotiable blocks; it must work tasks around
# them (including splitting a task across a meal if that's the better
# outcome), not push everything later in the day.
DEFAULT_MEALS = [
    {"name": "Breakfast", "start": "08:00", "end": "09:00"},
    {"name": "Lunch", "start": "13:00", "end": "14:00"},
    {"name": "Dinner", "start": "21:00", "end": "22:00"},
]

WAKE_BUFFER_MINUTES = 30  # buffer after waking before the first task can start

# Strict-mode JSON schema for the Optimization Agent's response. Groq's
# strict Structured Outputs mode requires every object property to be
# listed in "required" and every object to set "additionalProperties":
# False -- in return, the model is constrained at the token level and
# CANNOT emit invalid JSON or violate this shape, which is what fixes
# the "400 Failed to validate JSON" error that plain json_object mode
# could throw on this agent's long, multi-constraint prompt.
#
# "notes" is allowed to be null (Groq sometimes has nothing to report),
# so it uses the documented strict-mode pattern for optional fields: a
# union type ["string", "null"] while still being present in "required".
SCHEDULE_RESPONSE_SCHEMA = {
    "name": "chrono_schedule",
    "schema": {
        "type": "object",
        "properties": {
            "schedule": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "string", "enum": _WEEKDAY_NAMES},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "label": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["deep_work", "light_work", "break"],
                        },
                    },
                    "required": ["day", "start", "end", "label", "type"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": ["string", "null"]},
        },
        "required": ["schedule", "notes"],
        "additionalProperties": False,
    },
}


SYSTEM_PROMPT = """You are the Optimization Agent inside an AI scheduling assistant called Chrono.

You receive a set of hard constraints and a list of tasks for ONE WEEK. Your job is to
produce an actual time-blocked daily schedule that places every task sensibly.

Note on the input format: "daily_meals_every_day" lists meal blocks that repeat identically
on EVERY day in the schedule (not just one day) -- apply each one to all days_ahead days.
"recurring_commitments" lists blocks that only apply to the specific day(s) named in each entry.

HARD CONSTRAINTS (never violate these):
- Nothing may be scheduled before wake_time or after sleep_time on any day.
- Nothing may overlap with a fixed meal block (from daily_meals_every_day, applied to every
  day) or a recurring commitment (e.g. Gym, College). These are non-negotiable fixed
  appointments, already placed.
- Do not schedule anything in the first wake_buffer_minutes after wake_time -- that time is
  reserved as a buffer before the first task of the day.

SCHEDULING JUDGMENT (use real reasoning here, not a fixed formula):
- Deep-work tasks (cognitive_load: deep) should be biased toward the morning, when focus is
  typically highest, but use judgment: if the morning is too short to fit a task meaningfully,
  it is fine to place it elsewhere rather than forcing a fragment.
- The user's focus_span_hours is the longest they can work continuously before needing a
  break. If a task's daily duration exceeds this, SPLIT it into multiple sessions that day
  (e.g. a 4-hour task with a 2-hour focus span becomes two 2-hour sessions). There must ALWAYS
  be a real recovery break between these same-task sessions -- a MINIMUM of 15 minutes, even
  if a meal already separates them. NEVER place two sessions of the same task back-to-back
  with zero gap (e.g. 9:00-11:00 immediately followed by 11:00-13:00 is WRONG even if both are
  "ML Revision") -- that defeats the entire purpose of having a focus span limit.
- If a fixed meal or commitment falls in the middle of where a task would otherwise go, you
  may split the task around it (e.g. work 7-8, breakfast 8-9, continue work 9-10) rather than
  shifting the whole task later in the day -- this is often the better outcome than leaving a
  large unused gap before lunch.
- Insert a break between two DIFFERENT consecutive tasks (not between split sessions of the
  SAME task). This inter-task break has a MINIMUM of 60 minutes -- never shorter, even on a
  packed day. When the day has more slack (fewer tasks, more open time), use a longer break --
  90 minutes to 2+ hours is appropriate when only one or two tasks are scheduled that day.
- Higher urgency tasks should get earlier, more reliable placement in the week -- don't leave a
  high-urgency task's hours unscheduled by the end of the week while a low-urgency task got
  full priority instead.
- Tasks given as hours per week should have their total hours distributed across the week in
  a way that makes sense (doesn't have to be perfectly even) -- not crammed entirely into one
  day unless that genuinely makes more sense for that specific task.
- Tasks given as hours PER DAY (period: "day") are a FIXED daily requirement, not a weekly
  total. A task with period "day" and hours 6 means EXACTLY 6 hours on EVERY SINGLE day in the
  schedule, all days_ahead days, with no exceptions and no reduction on any day -- this is not
  something to distribute or average. If a "day" task cannot fit on a particular day, that is
  a hard constraint violation, not something to silently drop or shrink.
- Any time left over after placing all tasks should simply remain OPEN. Do not invent filler
  tasks or pad the schedule -- open time is a valid and expected outcome.
- If the total task hours requested for the week genuinely exceed the hours actually
  available (after meals, recurring commitments, sleep, and required breaks are all
  accounted for), you CANNOT fit everything -- do not solve this by shortening or skipping
  required breaks, running past sleep_time, or overlapping fixed blocks. Instead, drop or
  reduce the LOWEST-urgency, LOWEST-priority tasks first, and say exactly what you cut and why
  in the "notes" field (e.g. "Could not fit all of Task X this week -- only able to schedule
  6 of the requested 10 hours given other commitments."). Never silently omit a task without
  mentioning it in notes.

Respond ONLY with valid JSON in this exact shape, no other text:

{
  "schedule": [
    {
      "day": "Monday",
      "start": "HH:MM",
      "end": "HH:MM",
      "label": "task name or Break",
      "type": "deep_work or light_work or break"
    }
  ],
  "notes": "one or two sentences on any tradeoffs you made, or null"
}

Do not include meals or recurring commitments in your response -- those are already fixed and
will be merged in separately. Only return the blocks YOU are placing: tasks and breaks.
"""


def _parse_clock(raw: str) -> datetime.time | None:
    raw = raw.strip()
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def _time_to_minutes(t: datetime.time) -> int:
    return t.hour * 60 + t.minute


_WEEKDAY_ABBR = {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}


def _parse_days(days_raw: str) -> list[int]:
    """Parses 'Mon-Fri', 'Mon/Wed/Fri', etc. into weekday indices (0=Mon)."""
    days_raw = days_raw.lower()
    range_match = re.search(r"(\w+)\s*-\s*(\w+)", days_raw)
    if range_match:
        start_key, end_key = range_match.groups()
        start = _WEEKDAY_ABBR.get(start_key[:3])
        end = _WEEKDAY_ABBR.get(end_key[:3])
        if start is not None and end is not None:
            return list(range(start, end + 1)) if start <= end else list(range(start, 7)) + list(range(0, end + 1))

    days = []
    for token in re.split(r"[\/,]", days_raw):
        key = token.strip()[:3]
        if key in _WEEKDAY_ABBR:
            days.append(_WEEKDAY_ABBR[key])
    return sorted(set(days))


def _default_schedule_window(onboarding_result: dict) -> tuple[str, str]:
    """Resolves wake/sleep as HH:MM strings, with a sensible Case 2 fallback."""
    window = onboarding_result.get("schedule_window")
    if window and window.get("wake") and window.get("sleep"):
        wake = _parse_clock(window["wake"])
        sleep = _parse_clock(window["sleep"])
        if wake and sleep:
            return wake.strftime("%H:%M"), sleep.strftime("%H:%M")
    return "07:00", "23:00"


def _build_recurring_blocks(onboarding_result: dict, day_offsets: list[int]) -> list[dict]:
    """Expands recurring_commitments into one fixed block per actual day
    in day_offsets (offsets from today, e.g. [0,1,2,3] for the first
    four days of the window, or [4,5,6] for a later chunk of the same
    week) -- NOT always assumed to start at today, since a chunk other
    than the first needs the correct later offsets."""
    today = datetime.date.today()
    blocks = []

    for commitment in onboarding_result.get("recurring_commitments") or []:
        if not commitment.get("start") or not commitment.get("end"):
            continue
        start = _parse_clock(commitment["start"])
        end = _parse_clock(commitment["end"])
        if not start or not end:
            continue
        target_days = _parse_days(commitment.get("days_raw", ""))

        for offset in day_offsets:
            date = today + datetime.timedelta(days=offset)
            if date.weekday() in target_days:
                blocks.append({
                    "day": _WEEKDAY_NAMES[date.weekday()],
                    "start": start.strftime("%H:%M"),
                    "end": end.strftime("%H:%M"),
                    "label": commitment["name"],
                    "type": "recurring",
                })

    return blocks


def _compact_prompt_payload(payload: dict) -> dict:
    """
    Builds a token-lean version of the payload for the actual prompt text.
    The full expanded fixed_blocks (one entry per meal per day, every
    day) is needed for merging and validation later, but repeating it
    verbatim in the prompt wastes tokens against Groq's free-tier TPM
    cap -- meals are identical every day, so they're described once as
    a pattern instead of 7x per meal. Recurring commitments (which vary
    by day) are kept as-is since they're not repetitive in the same way.
    """
    recurring_only = [b for b in payload["fixed_blocks"] if b["type"] == "recurring"]

    return {
        "wake_time": payload["wake_time"],
        "sleep_time": payload["sleep_time"],
        "wake_buffer_minutes": payload["wake_buffer_minutes"],
        "focus_span_hours": payload["focus_span_hours"],
        "days_ahead": payload["days_ahead"],
        "daily_meals_every_day": [{"label": m["name"], "start": m["start"], "end": m["end"]} for m in DEFAULT_MEALS],
        "recurring_commitments": recurring_only,
        "tasks": payload["tasks"],
    }


def _build_constraint_payload(onboarding_result: dict, day_offsets: list[int]) -> dict:
    """
    Assembles the full constraint set Groq needs to reason about
    placement, scoped to day_offsets (offsets from today). For the
    whole-week capacity pre-check and the final merge, this is called
    with list(range(days_ahead)) (i.e. every day). For one chunk of a
    split week, it's called with just that chunk's offsets (e.g.
    [4, 5, 6]) so the fixed_blocks and "days_ahead" field returned only
    describe that chunk's days, not the full week.

    "tasks" hours here are always the ORIGINAL full amounts from
    onboarding_result -- per-chunk proportional allocation for
    "week"-period tasks happens separately (see
    _allocate_week_tasks_across_chunks) and overwrites this payload's
    "tasks" hours before it's sent to Groq for a specific chunk.
    """
    wake, sleep = _default_schedule_window(onboarding_result)
    recurring_blocks = _build_recurring_blocks(onboarding_result, day_offsets)

    fixed_blocks = list(recurring_blocks)
    for offset in day_offsets:
        day_name = _WEEKDAY_NAMES[(datetime.date.today() + datetime.timedelta(days=offset)).weekday()]
        recurring_today = [b for b in recurring_blocks if b["day"] == day_name]
        for meal in DEFAULT_MEALS:
            meal_start = _time_to_minutes(_parse_clock(meal["start"]))
            meal_end = _time_to_minutes(_parse_clock(meal["end"]))
            # If a recurring commitment (e.g. College 9 AM-4 PM) already
            # covers this meal's slot, drop the meal rather than placing
            # an overlapping block. The user eats around the commitment;
            # forcing a fixed meal block on top of it would both be wrong
            # and make the day un-schedulable. A meal is only kept if it
            # does NOT overlap any recurring commitment that day.
            overlaps_commitment = any(
                _overlaps(
                    meal_start, meal_end,
                    _time_to_minutes(_parse_clock(rb["start"])),
                    _time_to_minutes(_parse_clock(rb["end"])),
                )
                for rb in recurring_today
            )
            if overlaps_commitment:
                continue
            fixed_blocks.append({"day": day_name, "start": meal["start"], "end": meal["end"], "label": meal["name"], "type": "meal"})

    tasks_payload = []
    for t in onboarding_result.get("tasks", []):
        if not t.get("hours"):
            continue  # frequency-only tasks need a duration to be schedulable; skip for now
        tasks_payload.append({
            "name": t["name"],
            "hours": t["hours"],
            "period": t.get("period", "week"),
            "cognitive_load": t.get("cognitive_load"),
            "urgency": t.get("urgency"),
            "deadline": t.get("deadline"),
        })

    return {
        "wake_time": wake,
        "sleep_time": sleep,
        "wake_buffer_minutes": WAKE_BUFFER_MINUTES,
        "focus_span_hours": onboarding_result.get("focus_span_hours") or 2.0,
        "days_ahead": len(day_offsets),
        "day_offsets": list(day_offsets),
        "fixed_blocks": fixed_blocks,
        "tasks": tasks_payload,
    }


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and a_end > b_start


MINIMUM_INTER_TASK_BREAK_MINUTES = 60
MINIMUM_SAME_TASK_GAP_MINUTES = 60  # user wants a full hour between sessions, even of the same task


def _validate_schedule(llm_blocks: list[dict], fixed_blocks: list[dict], wake: str, sleep: str, tasks: list[dict], day_offsets: list[int]) -> list[str]:
    """
    Checks the LLM's proposed blocks against hard constraints. Returns a
    list of human-readable problems found (empty list = valid).

    day_offsets are offsets from today describing exactly which days
    this call is validating -- NOT always assumed to start at today,
    since a later chunk of a split week needs its own correct offsets
    (e.g. [4, 5, 6]) to build expected_days correctly.
    """
    problems = []
    wake_min = _time_to_minutes(_parse_clock(wake))
    sleep_min = _time_to_minutes(_parse_clock(sleep))

    today = datetime.date.today()
    expected_days = [_WEEKDAY_NAMES[(today + datetime.timedelta(days=i)).weekday()] for i in day_offsets]

    by_day: dict[str, list[dict]] = {}
    for b in llm_blocks:
        by_day.setdefault(b["day"], []).append(b)

    # "day"-period tasks must appear on EVERY expected day, with their
    # FULL daily hours -- this is a fixed requirement, not something
    # Groq should distribute, shrink, or skip on any day.
    for t in tasks:
        if t["period"] != "day":
            continue
        expected_minutes = int(round(t["hours"] * 60))
        for day in expected_days:
            day_blocks = [b for b in by_day.get(day, []) if b.get("label") == t["name"]]
            actual_minutes = sum(
                _time_to_minutes(_parse_clock(b["end"])) - _time_to_minutes(_parse_clock(b["start"]))
                for b in day_blocks
            )
            if actual_minutes == 0:
                problems.append(f"{day}: '{t['name']}' is missing entirely (needs {t['hours']} hrs, a fixed daily task)")
            elif actual_minutes != expected_minutes:
                problems.append(
                    f"{day}: '{t['name']}' has {round(actual_minutes/60, 2)} hrs scheduled, "
                    f"but needs exactly {t['hours']} hrs every day (fixed daily task)"
                )

    fixed_by_day: dict[str, list[dict]] = {}
    for b in fixed_blocks:
        fixed_by_day.setdefault(b["day"], []).append(b)

    # The model sometimes returns blocks for days OUTSIDE the chunk it was
    # asked about (e.g. a Saturday/Sunday/Monday chunk coming back with a
    # stray Tuesday block). Validating those against this chunk's fixed
    # blocks produces confusing cross-chunk errors like "Tuesday: 0 min
    # between X and Y" when Tuesday belongs to a different chunk entirely.
    # Flag any out-of-chunk day once, clearly, and skip validating it --
    # it isn't this chunk's job to schedule those days.
    expected_day_set = set(expected_days)
    stray_days = [d for d in by_day if d not in expected_day_set]
    for d in stray_days:
        problems.append(
            f"'{d}' is not part of this set of days -- only schedule "
            f"{', '.join(expected_days)}. Remove any blocks for other days."
        )

    for day, blocks in by_day.items():
        if day not in expected_day_set:
            continue  # already flagged above; don't add noisy downstream errors for it
        all_blocks_today = blocks + fixed_by_day.get(day, [])
        intervals = []
        for b in all_blocks_today:
            try:
                s = _time_to_minutes(_parse_clock(b["start"]))
                e = _time_to_minutes(_parse_clock(b["end"]))
            except (TypeError, ValueError):
                problems.append(f"{day}: block '{b.get('label')}' has unparseable times")
                continue

            if s < wake_min or e > sleep_min:
                problems.append(f"{day}: '{b.get('label')}' ({b['start']}-{b['end']}) falls outside wake/sleep window")

            for other_s, other_e, other_label in intervals:
                if _overlaps(s, e, other_s, other_e):
                    problems.append(f"{day}: '{b.get('label')}' overlaps with '{other_label}'")

            intervals.append((s, e, b.get("label")))

        # Check the gap between consecutive work blocks (deep_work /
        # light_work only — meals, recurring, and breaks don't count as
        # "tasks" for this check). Same-named blocks are split sessions
        # of one task and get a SMALLER minimum gap (just a short
        # recovery pause); different-named blocks need the full
        # inter-task minimum.
        work_blocks = [b for b in all_blocks_today if b.get("type") in ("deep_work", "light_work")]
        work_blocks_sorted = sorted(work_blocks, key=lambda b: _time_to_minutes(_parse_clock(b["start"])))

        for i in range(len(work_blocks_sorted) - 1):
            current = work_blocks_sorted[i]
            nxt = work_blocks_sorted[i + 1]
            gap = _time_to_minutes(_parse_clock(nxt["start"])) - _time_to_minutes(_parse_clock(current["end"]))
            same_task = current.get("label") == nxt.get("label")
            required_gap = MINIMUM_SAME_TASK_GAP_MINUTES if same_task else MINIMUM_INTER_TASK_BREAK_MINUTES

            if gap < required_gap:
                kind = "same task" if same_task else "different tasks"
                problems.append(
                    f"{day}: only {gap} minute(s) between '{current.get('label')}' and "
                    f"'{nxt.get('label')}' ({kind}) — needs at least {required_gap} minutes"
                )

    return problems


def _day_capacity_minutes(payload: dict, day_name: str) -> int:
    """
    Computes how many usable minutes exist on a given day, after
    subtracting the wake buffer and all fixed blocks (meals + recurring
    commitments) that fall on that day.
    """
    wake_min = _time_to_minutes(_parse_clock(payload["wake_time"])) + payload["wake_buffer_minutes"]
    sleep_min = _time_to_minutes(_parse_clock(payload["sleep_time"]))
    total = max(0, sleep_min - wake_min)

    for b in payload["fixed_blocks"]:
        if b["day"] == day_name:
            s = _time_to_minutes(_parse_clock(b["start"]))
            e = _time_to_minutes(_parse_clock(b["end"]))
            total -= max(0, e - s)

    return max(0, total)


def _check_capacity(payload: dict, days_ahead: int) -> dict | None:
    """
    Pre-flight arithmetic check, run BEFORE any Groq call. Determines
    whether the total demanded task time can possibly fit in the
    available window — no LLM reasoning needed to know that 14 hours of
    tasks cannot fit in a 10-hour day.

    Returns None if everything fits. Returns a structured "not possible"
    result with concrete, computed suggestions if it doesn't.
    """
    today = datetime.date.today()
    day_names = [_WEEKDAY_NAMES[(today + datetime.timedelta(days=i)).weekday()] for i in range(days_ahead)]

    capacities = {day: _day_capacity_minutes(payload, day) for day in day_names}

    # "day" tasks demand the same minutes every single day — check each
    # day's demand against that day's capacity directly.
    daily_demand_per_day = 0
    for t in payload["tasks"]:
        if t["period"] == "day":
            daily_demand_per_day += int(round(t["hours"] * 60))

    # "week" tasks distribute their total across days_ahead, adding a
    # roughly even amount on top of every day's "day" task demand.
    weekly_total_minutes = sum(int(round(t["hours"] * 60)) for t in payload["tasks"] if t["period"] == "week")
    weekly_per_day = int(round(weekly_total_minutes / days_ahead)) if days_ahead else 0

    overflowing_days = []
    worst_overflow = 0
    for day in day_names:
        demand = daily_demand_per_day + weekly_per_day
        capacity = capacities[day]
        if demand > capacity:
            overflow = demand - capacity
            overflowing_days.append((day, demand, capacity, overflow))
            worst_overflow = max(worst_overflow, overflow)

    if not overflowing_days:
        return None  # everything fits, no problem

    worst_day, worst_demand, worst_capacity, _ = max(overflowing_days, key=lambda x: x[3])

    suggestions = []

    # Suggestion 1: how much would the wake/sleep window need to extend
    # to fit the worst day's demand?
    extend_minutes = worst_overflow
    extend_hours = round(extend_minutes / 60, 1)
    suggestions.append(
        f"Extend your wake/sleep window by about {extend_hours} hour(s) "
        f"(e.g. wake earlier or sleep later) to fit everything on {worst_day}."
    )

    # Suggestion 2: alternate days — only feasible if there are at least
    # 2 "day"-period tasks, since alternating requires more than one
    # such task to spread across different days.
    day_period_tasks = [t["name"] for t in payload["tasks"] if t["period"] == "day"]
    if len(day_period_tasks) >= 2:
        suggestions.append(
            "Alternate tasks across days instead of doing all of them daily — "
            f"e.g. {day_period_tasks[0]} on odd days, {', '.join(day_period_tasks[1:])} on even days."
        )

    # Suggestion 3: reduce scope — always offered as a fallback, with the
    # specific amount of reduction needed.
    reduce_hours = round(worst_overflow / 60, 1)
    suggestions.append(
        f"Reduce total daily task time by about {reduce_hours} hour(s) "
        "(shorten a task's daily duration, or move it to a 'per week' total instead of 'per day')."
    )

    return {
        "possible": False,
        "message": (
            f"Not possible to schedule all tasks on {worst_day}: they need "
            f"{round(worst_demand / 60, 1)} hours, but only "
            f"{round(worst_capacity / 60, 1)} hours are available after meals "
            "and commitments."
        ),
        "suggestions": suggestions,
        "overflowing_days": [
            {"day": d, "needed_hours": round(demand / 60, 1), "available_hours": round(cap / 60, 1)}
            for d, demand, cap, _ in overflowing_days
        ],
    }


DEFAULT_MAX_DAYS_PER_CHUNK = 4  # see _split_days_into_chunks


def _split_days_into_chunks(days_ahead: int, max_days_per_chunk: int = DEFAULT_MAX_DAYS_PER_CHUNK) -> list[list[int]]:
    """
    Splits day offsets [0, 1, ..., days_ahead-1] into balanced chunks of
    at most max_days_per_chunk days each, so each Groq call only has to
    reason about and emit blocks for ONE chunk's days rather than the
    whole week at once. This directly fixes the truncation bug: a full
    7-day, multi-task week needs enough hidden reasoning + output tokens
    that it can exceed the account's TPM ceiling even after clamping
    max_completion_tokens -- clamping can only prevent SENDING a request
    too large to ever fit, it can't make a genuinely hard 7-day problem
    finish inside whatever budget happens to be left. Splitting the
    PROBLEM itself (not just the token budget) is what actually fixes
    this, and it scales automatically as days_ahead or task count grows.

    Chunk sizes are balanced as evenly as possible rather than always
    maxing out early chunks and leaving a small remainder (e.g. 7 days
    at max 4 becomes [4, 3], not [4, 4] with a day silently dropped, or
    an uneven [4, 2, 1]) -- this keeps every chunk a similarly-sized
    sub-problem and avoids especially tiny final chunks where week-task
    hour rounding has the least room to land cleanly.
    """
    if days_ahead <= max_days_per_chunk:
        return [list(range(days_ahead))]

    num_chunks = -(-days_ahead // max_days_per_chunk)  # ceil division
    base_size = days_ahead // num_chunks
    remainder = days_ahead - base_size * num_chunks

    chunks = []
    offset = 0
    for i in range(num_chunks):
        size = base_size + (1 if i < remainder else 0)
        chunks.append(list(range(offset, offset + size)))
        offset += size
    return chunks


def _allocate_week_tasks_across_chunks(tasks_payload: list[dict], chunk_sizes: list[int]) -> list[list[dict]]:
    """
    Returns one tasks list per chunk. For "week"-period tasks, each
    chunk gets a proportional share of the total weekly hours
    (chunk_size / total_days), rounded to the nearest 5 minutes so the
    prompt stays clean -- the model is never asked to reason about days
    it can't see, since it only receives the portion already allocated
    to ITS chunk. The LAST chunk receives whatever's left after earlier
    chunks' rounded allocations are subtracted, which guarantees the sum
    across all chunks always exactly equals the original weekly total --
    no hours silently gained or lost to rounding, regardless of how
    unevenly days_ahead splits.

    "day"-period tasks are copied through unchanged into every chunk,
    since their hours are a fixed daily amount independent of chunking
    (this matches how they already behaved before chunking existed).
    """
    total_days = sum(chunk_sizes)
    per_chunk_tasks: list[list[dict]] = [[] for _ in chunk_sizes]

    remaining_minutes = {
        t["name"]: int(round(t["hours"] * 60))
        for t in tasks_payload
        if t["period"] == "week"
    }

    for chunk_index, chunk_size in enumerate(chunk_sizes):
        is_last_chunk = chunk_index == len(chunk_sizes) - 1
        for t in tasks_payload:
            if t["period"] != "week":
                per_chunk_tasks[chunk_index].append(dict(t))
                continue

            if is_last_chunk:
                chunk_minutes = remaining_minutes[t["name"]]
            else:
                total_minutes = int(round(t["hours"] * 60))
                exact_share = total_minutes * (chunk_size / total_days)
                chunk_minutes = int(round(exact_share / 5) * 5)  # nearest 5 min, clean for the prompt
                chunk_minutes = min(chunk_minutes, remaining_minutes[t["name"]])
                remaining_minutes[t["name"]] -= chunk_minutes

            chunk_task = dict(t)
            chunk_task["hours"] = round(chunk_minutes / 60, 2)
            per_chunk_tasks[chunk_index].append(chunk_task)

    return per_chunk_tasks


def _solve_chunk(chunk_payload: dict, day_offsets: list[int]) -> tuple[list[dict], str | None, list[str]]:
    """
    Runs the call-Groq-then-validate-then-retry loop for ONE chunk of
    days. This is the same logic build_schedule used to run once for
    the whole week, now scoped to a day_offsets subset -- keeping each
    individual Groq call's reasoning depth and output size bounded
    regardless of how large days_ahead or the task list grows overall.

    Returns:
        (llm_blocks, notes, problems) -- problems is empty on success.
    """
    base_prompt = "Build the schedule for these days given these constraints:\n\n" + str(_compact_prompt_payload(chunk_payload))
    user_prompt = base_prompt

    llm_blocks: list[dict] = []
    notes = None
    problems: list[str] = []

    for attempt in range(3):  # one initial attempt + two corrective retries
        print(f"  [Optimization]   attempt {attempt + 1}: calling {current_backend()}...")
        try:
            result = call_llm(
                SYSTEM_PROMPT,
                user_prompt,
                expect_json=True,
                json_schema=SCHEDULE_RESPONSE_SCHEMA,
                # A desired ceiling, not a guarantee -- call_llm dynamically
                # clamps this to whatever fits under the account's TPM
                # limit for this specific chunk's actual prompt size.
                max_completion_tokens=8000,
            )
            llm_blocks = result.get("schedule", [])
            notes = result.get("notes")
            print(f"  [Optimization]   {current_backend()} returned {len(llm_blocks)} block(s).")
        except Exception as e:
            print(f"  [Optimization]   {current_backend()} call raised an exception: {e!r}")
            problems = [f"LLM call failed ({current_backend()}): {e}"]
            llm_blocks = []
            break

        problems = _validate_schedule(
            llm_blocks,
            chunk_payload["fixed_blocks"],
            chunk_payload["wake_time"],
            chunk_payload["sleep_time"],
            chunk_payload["tasks"],
            day_offsets,
        )

        if not problems:
            print("  [Optimization]   Validation passed.")
            break

        print(f"  [Optimization]   Validation found {len(problems)} problem(s): {problems}")

        if attempt < 2:
            user_prompt = base_prompt + (
                "\n\nYour previous response had these problems -- fix them and "
                "return a corrected schedule:\n" + "\n".join(problems)
            )

    return llm_blocks, notes, problems


def build_schedule(onboarding_result: dict, days_ahead: int = 7, max_days_per_chunk: int = DEFAULT_MAX_DAYS_PER_CHUNK) -> dict:
    """
    Main entry point. Splits the week into one or more day-chunks (see
    _split_days_into_chunks), sends each chunk to Groq as its own
    self-contained request, validates each chunk independently, and
    merges everything into one final sorted schedule.

    Chunking exists because a full 7-day, multi-task week in a single
    Groq call can need more hidden reasoning + output tokens than the
    account's token-per-minute ceiling allows, even after
    max_completion_tokens is dynamically clamped to fit -- clamping only
    prevents SENDING an over-budget request, it can't make a genuinely
    hard 7-day problem finish inside whatever room happens to be left.
    Splitting the problem itself into smaller, independent sub-problems
    is what actually fixes that, and it scales automatically as
    days_ahead or the task list grows, unlike a fixed token number.

    Returns:
        {
          "blocks": [...],            # all blocks, fixed + LLM-placed, sorted
          "notes": str | None,        # Groq's own explanation(s) of tradeoffs,
                                       # joined across chunks if more than one
          "validation_problems": [...], # non-empty only if a chunk failed
                                         # after all retries, prefixed with
                                         # which days that chunk covered
          "capacity_error": dict | None # set instead of blocks if tasks can't
                                         # possibly fit; see _check_capacity()
        }
    """
    full_payload = _build_constraint_payload(onboarding_result, list(range(days_ahead)))

    print(f"  [Optimization] {len(full_payload['tasks'])} schedulable task(s), {len(full_payload['fixed_blocks'])} fixed block(s).")

    if not full_payload["tasks"]:
        print("  [Optimization] No schedulable tasks (missing hours?) — returning fixed blocks only.")
        return {"blocks": sorted(full_payload["fixed_blocks"], key=lambda b: (b["day"], b["start"])), "notes": None, "validation_problems": [], "capacity_error": None, "failed": False}

    # Pre-flight arithmetic check BEFORE spending any API call, on the
    # FULL week regardless of chunking -- if the tasks simply cannot fit
    # no matter how cleverly arranged, there's no point asking Groq, and
    # chunking the request wouldn't change that answer.
    capacity_error = _check_capacity(full_payload, days_ahead)
    if capacity_error is not None:
        print("  [Optimization] Capacity check failed — tasks don't fit. Skipping Groq call.")
        return {"blocks": [], "notes": None, "validation_problems": [], "capacity_error": capacity_error, "failed": False}

    print("  [Optimization] Capacity check passed.")

    # Default engine: deterministic Python placement. No API calls, no
    # token limits, no pacing waits, and correct by construction every
    # time -- it cannot return the wrong number of hours, overlap a meal,
    # or leak blocks onto the wrong day, which is what the LLM placement
    # kept doing. The LLM is still used for the judgment it's good at
    # (cognitive load / urgency, in the Task Analysis Agent); only the
    # exact time-block placement is done here in code.
    #
    # Set LLM_PLACEMENT=1 in the environment to use the older LLM-driven
    # chunked path instead (kept for comparison / experimentation).
    if os.environ.get("LLM_PLACEMENT", "0") != "1":
        print("  [Optimization] Placing schedule deterministically (no API call needed)...")
        placement = place_schedule(full_payload)

        if placement["problems"]:
            # The placer couldn't fit everything. This is the REAL
            # feasibility signal -- finer-grained than _check_capacity,
            # because the placer accounts for how the mandatory 60-minute
            # inter-task breaks fragment the day's free gaps, which the
            # capacity check's raw-minutes total does not. Rather than
            # return a bare failure, surface it as the same structured
            # capacity_error (with actionable suggestions) the capacity
            # check produces, so the user gets help, not just "couldn't
            # do it". Build the suggestions from the worst shortfall.
            print(f"  [Optimization] Couldn't fit everything once required breaks are accounted for.")
            worst_hours = 0.0
            for p in placement["problems"]:
                # problems read like "...could not place 1.0 hr(s) of 'DSA'..."
                import re as _re
                m = _re.search(r"could not place ([\d.]+) hr", p)
                if m:
                    worst_hours = max(worst_hours, float(m.group(1)))

            day_period_tasks = [t["name"] for t in full_payload["tasks"] if t["period"] == "day"]
            suggestions = [
                f"Extend your wake/sleep window by about {round(worst_hours + 1, 1)} hour(s) "
                "to leave room for tasks plus the required breaks between them.",
            ]
            if len(day_period_tasks) >= 2:
                suggestions.append(
                    "Alternate tasks across days instead of doing every task every day — "
                    f"e.g. {day_period_tasks[0]} on some days, {', '.join(day_period_tasks[1:])} on others. "
                    "Fewer task switches per day means fewer mandatory 60-minute breaks eating into your time."
                )
            suggestions.append(
                "Increase your continuous-focus span (longer sessions need fewer breaks), "
                "or reduce a task's daily hours / move it to a weekly total."
            )

            capacity_error = {
                "possible": False,
                "message": (
                    "Everything fits on paper, but once the required 60-minute breaks between "
                    "different tasks are placed, there isn't enough room to fit every task on every day."
                ),
                "suggestions": suggestions,
                "overflowing_days": [],
            }
            return {
                "blocks": [],
                "notes": None,
                "validation_problems": [],
                "capacity_error": capacity_error,
                "failed": False,
            }

        # Validate our own output with the same checker used for the LLM,
        # as a safety net -- if the placer ever produced something
        # invalid, better to know than to trust it blindly.
        problems = _validate_schedule(
            placement["blocks"],
            full_payload["fixed_blocks"],
            full_payload["wake_time"],
            full_payload["sleep_time"],
            full_payload["tasks"],
            list(range(days_ahead)),
        )
        if problems:
            print(f"  [Optimization] Deterministic placement failed self-check: {problems}")
            return {
                "blocks": [],
                "notes": None,
                "validation_problems": problems,
                "capacity_error": None,
                "failed": True,
            }

        all_blocks = list(full_payload["fixed_blocks"]) + placement["blocks"]
        # Sort in fixed weekday order Monday -> Sunday (not starting from
        # today), per the user's preference. _WEEKDAY_NAMES is already in
        # Mon..Sun order, so a name's index in it IS its sort position.
        day_order = {name: i for i, name in enumerate(_WEEKDAY_NAMES)}
        all_blocks.sort(key=lambda b: (day_order.get(b["day"], 999), b["start"]))

        print("  [Optimization] Schedule placed and validated.")
        return {
            "blocks": all_blocks,
            "notes": None,
            "validation_problems": [],
            "capacity_error": None,
            "failed": False,
        }

    # ---- LLM-driven chunked path (only when LLM_PLACEMENT=1) ----
    print("  [Optimization] LLM_PLACEMENT=1 — using the LLM-driven chunked path.")

    chunks = _split_days_into_chunks(days_ahead, max_days_per_chunk)
    chunk_sizes = [len(c) for c in chunks]

    if len(chunks) > 1:
        sizes_desc = " + ".join(f"{s}d" for s in chunk_sizes)
        print(f"  [Optimization] Splitting the week into {len(chunks)} call(s) ({sizes_desc}) to stay within the account's token budget.")

    per_chunk_tasks = _allocate_week_tasks_across_chunks(full_payload["tasks"], chunk_sizes)

    all_llm_blocks: list[dict] = []
    all_notes: list[str] = []
    all_problems: list[str] = []
    any_chunk_failed = False

    today = datetime.date.today()

    for chunk_index, day_offsets in enumerate(chunks):
        chunk_payload = _build_constraint_payload(onboarding_result, day_offsets)
        chunk_payload["tasks"] = per_chunk_tasks[chunk_index]

        chunk_day_names = [_WEEKDAY_NAMES[(today + datetime.timedelta(days=o)).weekday()] for o in day_offsets]
        chunk_label = ", ".join(chunk_day_names)
        print(f"  [Optimization] Chunk {chunk_index + 1}/{len(chunks)} ({chunk_label}): {len(chunk_payload['tasks'])} task(s), {len(chunk_payload['fixed_blocks'])} fixed block(s).")

        chunk_blocks, chunk_notes, chunk_problems = _solve_chunk(chunk_payload, day_offsets)

        if chunk_problems:
            any_chunk_failed = True
            all_problems.extend(f"[{chunk_label}] {p}" for p in chunk_problems)
            # Keep going through remaining chunks even after one fails --
            # surfacing every chunk's problems in one pass is more useful
            # than stopping at the first failure, and a later chunk may
            # still succeed independently.
            continue

        all_llm_blocks.extend(chunk_blocks)
        if chunk_notes:
            all_notes.append(f"[{chunk_label}] {chunk_notes}" if len(chunks) > 1 else chunk_notes)

    if any_chunk_failed:
        print("  [Optimization] One or more chunks could not produce a valid schedule.")
        return {
            "blocks": [],
            "notes": " ".join(all_notes) if all_notes else None,
            "validation_problems": all_problems,
            "capacity_error": None,
            "failed": True,
        }

    all_blocks = list(full_payload["fixed_blocks"]) + all_llm_blocks

    # Sort in fixed weekday order Monday -> Sunday (per user preference),
    # not starting from today. _WEEKDAY_NAMES is already Mon..Sun.
    day_order = {name: i for i, name in enumerate(_WEEKDAY_NAMES)}
    all_blocks.sort(key=lambda b: (day_order.get(b["day"], 999), b["start"]))

    return {
        "blocks": all_blocks,
        "notes": " ".join(all_notes) if all_notes else None,
        "validation_problems": [],
        "capacity_error": None,
        "failed": False,
    }


if __name__ == "__main__":
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(__file__), "..", "config", ".env")
    load_dotenv(dotenv_path=env_path)

    sample_onboarding_result = {
        "case": 1,
        "schedule_window": {"wake": "6:00 AM", "sleep": "10:30 PM"},
        "recurring_commitments": [
            {"name": "Gym", "days_raw": "Mon/Wed/Fri", "start": "4 PM", "end": "5 PM"},
        ],
        "focus_span_hours": 2.0,
        "tasks": [
            {"name": "ML Revision", "hours": 2.0, "period": "day", "deadline": None, "urgency": "medium", "cognitive_load": "deep"},
            {"name": "DSA", "hours": 2.0, "period": "day", "deadline": None, "urgency": "medium", "cognitive_load": "deep"},
        ],
    }

    result = build_schedule(sample_onboarding_result, days_ahead=3)

    current_day = None
    for block in result["blocks"]:
        if block["day"] != current_day:
            current_day = block["day"]
            print(f"\n{current_day}")
        print(f"  {block['start']}-{block['end']}  {block['label']}  ({block['type']})")

    if result.get("notes"):
        print(f"\nNotes: {result['notes']}")
    if result["validation_problems"]:
        print(f"\nUnresolved validation problems: {result['validation_problems']}")
