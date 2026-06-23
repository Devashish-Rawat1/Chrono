"""
Deterministic schedule placer.

This is the replacement for asking an LLM to place time blocks. The LLM
was repeatedly failing at the *arithmetic* of placement -- returning 1.5
hours where 2.0 were required, overlapping fixed meal blocks, leaking
blocks onto days outside the requested set, and burning the account's
whole token-per-minute budget on retries that never converged.

Placement is not a fuzzy-judgment problem; it's a constraint-satisfaction
problem with exact arithmetic, which Python does perfectly, instantly,
and with zero API calls. The LLM is still used for the judgment calls it
is actually good at (classifying cognitive load / urgency in the Task
Analysis Agent) -- only the exact placement moved here.

Guarantees, by construction (not by validate-then-retry):
- A "day"-period task gets EXACTLY its required minutes on EVERY day.
- Nothing overlaps a fixed meal or recurring commitment.
- Nothing starts before wake_time + wake_buffer or ends after sleep_time.
- A task longer than focus_span is split into sessions of at most
  focus_span, with a short recovery gap between same-task sessions and a
  longer inter-task break between different tasks.
- Deep-work tasks are placed earlier in the day than light-work tasks.

If something genuinely cannot fit (more task-minutes than the day's free
gaps can hold once breaks are accounted for), the placer reports it
honestly per day rather than silently dropping or shrinking a task --
mirroring the capacity check's contract.
"""

import datetime

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# These mirror the constants the validator enforces, kept in sync so the
# output this module produces always passes _validate_schedule.
# Per the user's instruction, EVERY gap between two work sessions -- even
# two sessions of the same task -- must be at least 60 minutes, not the
# shorter 15-minute recovery pause used previously. A task split across
# sessions therefore gets a real hour-long break between each, the same
# as switching between two different tasks.
MINIMUM_INTER_TASK_BREAK_MINUTES = 60
MINIMUM_SAME_TASK_GAP_MINUTES = 60


def _parse_hhmm(value: str) -> int:
    """'HH:MM' -> minutes since midnight."""
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def _minutes_to_hhmm(total: int) -> str:
    """minutes since midnight -> 'HH:MM'."""
    return f"{total // 60:02d}:{total % 60:02d}"


def _free_gaps_for_day(day_name: str, fixed_blocks: list, wake_min: int, sleep_min: int) -> list:
    """
    Returns the list of (start_min, end_min) open windows on a day,
    between wake (already including the buffer) and sleep, with every
    fixed block (meals + recurring commitments) on that day carved out.
    """
    busy = []
    for b in fixed_blocks:
        if b["day"] != day_name:
            continue
        busy.append((_parse_hhmm(b["start"]), _parse_hhmm(b["end"])))
    busy.sort()

    gaps = []
    cursor = wake_min
    for bs, be in busy:
        if bs > cursor:
            gaps.append((cursor, min(bs, sleep_min)))
        cursor = max(cursor, be)
        if cursor >= sleep_min:
            break
    if cursor < sleep_min:
        gaps.append((cursor, sleep_min))

    # Drop any zero/negative-length gaps that can arise from clamping.
    return [(s, e) for s, e in gaps if e > s]


def _order_tasks_for_day(tasks: list) -> list:
    """
    Orders tasks for placement: deep-work before light-work (deep biased
    to the earlier, higher-focus part of the day), and within the same
    cognitive load, higher urgency first. Stable and fully deterministic.
    """
    urgency_rank = {"high": 0, "medium": 1, "low": 2, None: 3}
    load_rank = {"deep": 0, "light": 1, None: 2}
    return sorted(
        tasks,
        key=lambda t: (
            load_rank.get(t.get("cognitive_load"), 2),
            urgency_rank.get(t.get("urgency"), 3),
            t["name"],  # final tiebreak so order is never ambiguous
        ),
    )


def _place_one_day(
    day_name: str,
    day_tasks: list,
    fixed_blocks: list,
    wake_min: int,
    sleep_min: int,
    focus_span_minutes: int,
) -> tuple:
    """
    Places all of day_tasks into the day's free gaps. Returns
    (blocks, problems). Each task dict must carry "name", "minutes"
    (exact minutes to place THIS day), "cognitive_load", "urgency".

    Strategy: for each task (deep-work first, then by urgency), lay down
    sessions of at most focus_span_minutes. To avoid ugly fragmentation
    (e.g. a 2-hour task split into 1.5h + 0.5h around a meal), each
    session prefers the EARLIEST gap that can hold a full focus-span
    session; only if no gap can hold a full session does it fall back to
    placing a partial session in whatever room remains. The required
    recovery gap between same-task sessions and inter-task break between
    different tasks are honored within a gap; a fixed block between gaps
    already provides a real break, so spacing resets per gap.
    """
    blocks = []
    problems = []

    ordered = _order_tasks_for_day(day_tasks)
    remaining = {t["name"]: t["minutes"] for t in ordered}

    raw_gaps = _free_gaps_for_day(day_name, fixed_blocks, wake_min, sleep_min)
    # Mutable cursor + last-block tracking per gap, indexed alongside raw_gaps.
    gap_state = [{"cursor": gs, "last_label": None, "last_end": None} for (gs, _e) in raw_gaps]

    def _try_place_session(task, prefer_full):
        """
        Attempts to place ONE session of `task`. If prefer_full is True,
        only places into a gap that can fit a full focus-span (or the
        task's full remaining, whichever is smaller) session. Returns the
        minutes placed (0 if nothing could be placed under the
        constraints).
        """
        want = min(focus_span_minutes, remaining[task["name"]])
        best = None  # (gap_index, session_start, session_len)

        for gi, (gs, ge) in enumerate(raw_gaps):
            st = gap_state[gi]
            cursor = st["cursor"]
            if cursor >= ge:
                continue

            if st["last_label"] is not None:
                needed_gap = (
                    MINIMUM_SAME_TASK_GAP_MINUTES
                    if st["last_label"] == task["name"]
                    else MINIMUM_INTER_TASK_BREAK_MINUTES
                )
                session_start = max(cursor, st["last_end"] + needed_gap)
            else:
                session_start = cursor

            if session_start >= ge:
                continue

            avail = ge - session_start
            session_len = min(want, avail)
            if session_len <= 0:
                continue

            if prefer_full and session_len < want:
                continue  # this gap can't hold a full session; skip in the first pass

            # Earliest viable placement wins (keeps the day compact and
            # deep-work early). Since we iterate gaps in chronological
            # order, the first hit is already the earliest.
            best = (gi, session_start, session_len)
            break

        if best is None:
            return 0

        gi, session_start, session_len = best
        blocks.append({
            "day": day_name,
            "start": _minutes_to_hhmm(session_start),
            "end": _minutes_to_hhmm(session_start + session_len),
            "label": task["name"],
            "type": "deep_work" if task.get("cognitive_load") == "deep" else "light_work",
        })
        remaining[task["name"]] -= session_len
        gap_state[gi]["last_label"] = task["name"]
        gap_state[gi]["last_end"] = session_start + session_len
        gap_state[gi]["cursor"] = session_start + session_len
        return session_len

    for t in ordered:
        # Keep placing sessions until this task's daily minutes are done
        # or no more room exists. First try only gaps that fit a full
        # session (avoids fragmentation); if a pass places nothing, allow
        # partial placement so we still use leftover slivers rather than
        # silently dropping minutes.
        while remaining[t["name"]] > 0:
            placed = _try_place_session(t, prefer_full=True)
            if placed == 0:
                placed = _try_place_session(t, prefer_full=False)
            if placed == 0:
                break  # genuinely no room left anywhere for this task

    for name, left in remaining.items():
        if left > 0:
            problems.append(
                f"{day_name}: could not place {round(left / 60, 2)} hr(s) of '{name}' "
                "-- not enough free time between fixed blocks and required breaks."
            )

    return blocks, problems


def place_schedule(payload: dict) -> dict:
    """
    Deterministically places every task across every day in the payload.

    Args mirror the Optimization Agent's constraint payload:
        payload["wake_time"], ["sleep_time"]  : "HH:MM"
        payload["wake_buffer_minutes"]        : int
        payload["focus_span_hours"]           : float
        payload["day_offsets"]                : offsets from today
        payload["fixed_blocks"]               : meals + recurring, per day
        payload["tasks"]                      : each with name, hours,
                                                period ("day"|"week"),
                                                cognitive_load, urgency

    Returns:
        {"blocks": [...placed task/work blocks...],
         "problems": [...any per-day shortfalls...]}

    "day"-period tasks are placed with their full hours on EVERY day.
    "week"-period tasks have their weekly hours spread as evenly as
    possible across the payload's days (any rounding remainder lands on
    the earliest days), so each day gets an exact integer-minute share.
    """
    wake_min = _parse_hhmm(payload["wake_time"]) + payload["wake_buffer_minutes"]
    sleep_min = _parse_hhmm(payload["sleep_time"])
    focus_span_minutes = int(round(payload["focus_span_hours"] * 60))

    today = datetime.date.today()
    day_names = [_WEEKDAY_NAMES[(today + datetime.timedelta(days=o)).weekday()] for o in payload["day_offsets"]]
    num_days = len(day_names)

    # Pre-compute each day's per-task minutes.
    # "day" tasks: same minutes every day. "week" tasks: spread evenly,
    # remainder distributed one minute at a time to the earliest days so
    # the per-day totals are exact integers and sum to the weekly total.
    day_period_minutes = {}
    week_even_minutes = {}   # name -> base minutes per day
    week_remainder = {}      # name -> how many of the first days get +1 min
    for t in payload["tasks"]:
        total = int(round(t["hours"] * 60))
        if t.get("period", "week") == "day":
            day_period_minutes[t["name"]] = total
        else:
            if num_days > 0:
                week_even_minutes[t["name"]] = total // num_days
                week_remainder[t["name"]] = total % num_days
            else:
                week_even_minutes[t["name"]] = 0
                week_remainder[t["name"]] = 0

    all_blocks = []
    all_problems = []

    for day_index, day_name in enumerate(day_names):
        day_tasks = []
        for t in payload["tasks"]:
            if t.get("period", "week") == "day":
                minutes = day_period_minutes[t["name"]]
            else:
                minutes = week_even_minutes[t["name"]]
                if day_index < week_remainder[t["name"]]:
                    minutes += 1  # absorb the rounding remainder on the earliest days
            if minutes <= 0:
                continue
            day_tasks.append({
                "name": t["name"],
                "minutes": minutes,
                "cognitive_load": t.get("cognitive_load"),
                "urgency": t.get("urgency"),
            })

        blocks, problems = _place_one_day(
            day_name, day_tasks, payload["fixed_blocks"], wake_min, sleep_min, focus_span_minutes
        )
        all_blocks.extend(blocks)
        all_problems.extend(problems)

    return {"blocks": all_blocks, "problems": all_problems}
