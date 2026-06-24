"""
Google Calendar Agent tool — handles OAuth and reads calendar events.

First run: opens a browser window, you log in and grant access.
A token.json is saved so future runs skip the login step.
"""

import os
import json
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Combined scopes — Calendar (full access, needed to create the dedicated
# "Chrono Schedule" calendar and write events to it) + Tasks (read-only).
# All scripts sharing token.json must request the exact same scopes, or
# re-auth will be triggered every time you switch between them.
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks.readonly",
]

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "credentials.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token.json")
USER_CONFIG_PATH = os.path.join(CONFIG_DIR, "user_config.json")

# Monday-first weekday names, indexed to match datetime.date.weekday()
# (Monday=0 ... Sunday=6). Used to map a block's weekday name back to a
# real date when writing events. Kept defined here so this module has no
# dependency on optimization_agent just for a constant.
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def get_calendar_service():
    """Authenticates and returns a Google Calendar API service object."""
    creds = None

    # Reuse saved token if it exists
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    # If no valid token, refresh or run the login flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"Missing {CREDENTIALS_PATH}. "
                    "Download OAuth credentials from Google Cloud Console "
                    "and place them at config/credentials.json"
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save the token for next time
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def list_calendars() -> list[dict]:
    """
    Lists every calendar the user has access to, with their IDs.
    Run this once to find the calendarId of a non-primary calendar
    (e.g. "Changing My Daily Life") instead of guessing.
    """
    service = get_calendar_service()
    result = service.calendarList().list().execute()
    calendars = result.get("items", [])
    return [
        {"summary": cal.get("summary"), "id": cal.get("id"), "primary": cal.get("primary", False)}
        for cal in calendars
    ]


def get_or_create_chrono_calendar(name: str = "Chrono Schedule") -> str:
    """
    Finds the dedicated output calendar by name, or creates it if it
    doesn't exist yet. This is the calendar Chrono writes generated
    schedules into — kept separate from your real-life calendar so it
    can be wiped and regenerated safely.

    Returns:
        The calendar ID to use for writing events.
    """
    service = get_calendar_service()
    calendars = list_calendars()

    for cal in calendars:
        if cal["summary"] == name:
            return cal["id"]

    # Not found — create it
    new_calendar = {"summary": name, "timeZone": "Asia/Kolkata"}
    created = service.calendars().insert(body=new_calendar).execute()
    return created["id"]


# Timezone every Chrono event is written in. Matches the timeZone the
# Chrono calendar is created with above, so there's never an offset
# mismatch between the calendar's own zone and the events on it.
CHRONO_TIMEZONE = "Asia/Kolkata"

# How Chrono-written events are color-coded in Google Calendar, by block
# "type". These are Google's fixed colorId values (1-11); the exact hues
# vary by client but the grouping is what matters -- deep work, light
# work, breaks, meals, and recurring commitments each read distinctly.
# Google Calendar's event colorId palette is fixed at 1-11:
#   1 Lavender  2 Sage     3 Grape    4 Flamingo  5 Banana   6 Tangerine
#   7 Peacock   8 Graphite 9 Blueberry 10 Basil   11 Tomato
#
# Fixed-meaning colors (per the user's chosen scheme):
_WAKE_COLOR_ID = "6"       # Tangerine — start of day
_SLEEP_COLOR_ID = "9"      # Blueberry — end of day
_MEAL_COLOR_ID = "5"       # Banana — breakfast / lunch / dinner
_RECURRING_COLOR_ID = "8"  # Graphite — fixed external commitments
_BREAK_COLOR_ID = "2"      # Sage — rest gaps

# Colors that scheduled TASKS cycle through, so each distinct task name
# gets its own color and two different tasks never look alike. These
# deliberately exclude the fixed-meaning colors above (Banana, Tangerine,
# Blueberry, Graphite, Sage) so a task is never confused with a meal,
# wake/sleep marker, commitment, or break.
_TASK_COLOR_CYCLE = ["7", "11", "4", "3", "1", "10"]  # Peacock, Tomato, Flamingo, Grape, Lavender, Basil


def _color_for_block(block: dict, task_color_map: dict) -> str | None:
    """
    Picks the colorId for a block. Meals/wake/sleep/recurring/break use
    their fixed-meaning color; actual task blocks (deep_work / light_work)
    get a per-TASK-NAME color from task_color_map so each task is visually
    distinct on the calendar.
    """
    btype = block.get("type")
    if btype == "wake":
        return _WAKE_COLOR_ID
    if btype == "sleep":
        return _SLEEP_COLOR_ID
    if btype == "meal":
        return _MEAL_COLOR_ID
    if btype == "recurring":
        return _RECURRING_COLOR_ID
    if btype == "break":
        return _BREAK_COLOR_ID
    if btype in ("deep_work", "light_work"):
        return task_color_map.get(block.get("label"))
    return None


def _build_task_color_map(blocks: list[dict]) -> dict:
    """
    Assigns each distinct task name (across deep_work / light_work blocks)
    a color from _TASK_COLOR_CYCLE, in first-appearance order, wrapping
    around if there are more tasks than colors. Deterministic: the same
    set of tasks always maps the same way within a run.
    """
    task_color_map = {}
    next_index = 0
    for b in blocks:
        if b.get("type") in ("deep_work", "light_work"):
            name = b.get("label")
            if name not in task_color_map:
                task_color_map[name] = _TASK_COLOR_CYCLE[next_index % len(_TASK_COLOR_CYCLE)]
                next_index += 1
    return task_color_map


def _weekday_name_to_date(day_name: str, days_ahead: int = 7) -> datetime.date | None:
    """
    Maps a weekday name like "Monday" to the actual upcoming date it
    refers to, using the SAME convention the rest of the pipeline uses:
    the schedule covers the next `days_ahead` days starting today, and
    each weekday name appears exactly once in a 7-day window. So
    "Monday" means "the first Monday on or after today". Returns None if
    the name doesn't fall within the window (shouldn't happen for a
    7-day schedule, but guards against bad input).
    """
    today = datetime.date.today()
    for offset in range(days_ahead):
        date = today + datetime.timedelta(days=offset)
        if _WEEKDAY_NAMES[date.weekday()] == day_name:
            return date
    return None


def clear_chrono_calendar(calendar_id: str, days_ahead: int = 7) -> int:
    """
    Deletes all events on the Chrono calendar within the schedule window
    (today through days_ahead days ahead), so a fresh schedule can be
    written without piling new events on top of an old run's events.

    Only the dedicated Chrono calendar should ever be passed here -- it's
    safe to wipe because Chrono owns every event on it. This deliberately
    deletes events rather than deleting+recreating the whole calendar, so
    the calendar's own settings (color, sharing, subscriptions, its ID)
    survive across regenerations.

    Returns:
        The number of events deleted.
    """
    service = get_calendar_service()

    local_midnight = datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone()
    time_min = local_midnight.isoformat()
    time_max = (local_midnight + datetime.timedelta(days=days_ahead)).isoformat()

    deleted = 0
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                pageToken=page_token,
            )
            .execute()
        )
        for event in resp.get("items", []):
            service.events().delete(calendarId=calendar_id, eventId=event["id"]).execute()
            deleted += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return deleted


def write_schedule_to_calendar(
    blocks: list[dict],
    days_ahead: int = 7,
    calendar_name: str = "Chrono Schedule",
    wake_time: str | None = None,
    sleep_time: str | None = None,
) -> dict:
    """
    Wipes the dedicated Chrono calendar's events for the schedule window
    and writes the given blocks as fresh events. This is Output Stage 1
    of the pipeline -- it turns the in-memory schedule build_schedule()
    produces into real Google Calendar events.

    Each block dict is expected to have: "day" (weekday name like
    "Monday"), "start" and "end" ("HH:MM"), "label", and "type". The
    weekday name is resolved to the actual upcoming date via
    _weekday_name_to_date(), matching the pipeline's "next days_ahead
    days from today" convention.

    Args:
        blocks: the "blocks" list from build_schedule()'s result.
        days_ahead: the schedule window length, used both for resolving
                    weekday names to dates and for scoping the wipe.
        calendar_name: name of the dedicated output calendar.
        wake_time / sleep_time: optional "HH:MM" strings. When given, a
                    short "Wake up" event and a short "Sleep" event are
                    added to EVERY day in the window, so the daily start
                    and end of the schedule are visible on the calendar
                    itself. These are written as 15-minute marker events
                    at the wake and sleep times.

    Returns:
        {
          "calendar_id": str,
          "calendar_link": str,  # URL to open this calendar in the browser
          "deleted": int,        # events removed in the wipe
          "created": int,        # events written
          "skipped": [ ... ],    # any blocks that couldn't be written, with reasons
        }
    """
    service = get_calendar_service()
    calendar_id = get_or_create_chrono_calendar(calendar_name)

    deleted = clear_chrono_calendar(calendar_id, days_ahead=days_ahead)

    created = 0
    skipped = []

    # Build the full set of events to write: the schedule blocks, plus
    # (if wake/sleep were given) a wake-up and sleep marker on every day.
    events_to_write = list(blocks)

    if wake_time and sleep_time:
        today = datetime.date.today()
        for offset in range(days_ahead):
            day_name = _WEEKDAY_NAMES[(today + datetime.timedelta(days=offset)).weekday()]
            # 15-minute marker events at the wake and sleep boundaries.
            wake_end = _add_minutes(wake_time, 15)
            sleep_end = _add_minutes(sleep_time, 15)
            events_to_write.append(
                {"day": day_name, "start": wake_time, "end": wake_end, "label": "Wake up", "type": "wake"}
            )
            events_to_write.append(
                {"day": day_name, "start": sleep_time, "end": sleep_end, "label": "Sleep", "type": "sleep"}
            )

    # Assign each distinct task its own color up front, from the full set
    # of blocks, so the mapping is stable no matter the order events are
    # written in.
    task_color_map = _build_task_color_map(events_to_write)

    for block in events_to_write:
        date = _weekday_name_to_date(block.get("day", ""), days_ahead=days_ahead)
        if date is None:
            skipped.append({"block": block, "reason": f"could not resolve day '{block.get('day')}' to a date"})
            continue

        try:
            start_h, start_m = (int(x) for x in block["start"].split(":"))
            end_h, end_m = (int(x) for x in block["end"].split(":"))
        except (KeyError, ValueError, AttributeError):
            skipped.append({"block": block, "reason": "unparseable start/end time"})
            continue

        start_dt = datetime.datetime(date.year, date.month, date.day, start_h, start_m)
        end_dt = datetime.datetime(date.year, date.month, date.day, end_h, end_m)

        event_body = {
            "summary": block.get("label", "(untitled)"),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": CHRONO_TIMEZONE},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": CHRONO_TIMEZONE},
            # Mark Chrono's own events so they can be identified later
            # (e.g. by a future "only delete Chrono events" wipe) without
            # relying on the calendar being exclusively Chrono's.
            "description": f"Chrono-generated · type: {block.get('type', 'unknown')}",
        }
        color_id = _color_for_block(block, task_color_map)
        if color_id:
            event_body["colorId"] = color_id

        service.events().insert(calendarId=calendar_id, body=event_body).execute()
        created += 1

    return {
        "calendar_id": calendar_id,
        "calendar_link": _calendar_link(calendar_id),
        "deleted": deleted,
        "created": created,
        "skipped": skipped,
    }


def _add_minutes(hhmm: str, minutes: int) -> str:
    """Adds `minutes` to an 'HH:MM' string, returning 'HH:MM' (clamped so
    it never rolls past 23:59, since these are same-day marker events)."""
    h, m = (int(x) for x in hhmm.split(":"))
    total = min(h * 60 + m + minutes, 23 * 60 + 59)
    return f"{total // 60:02d}:{total % 60:02d}"


def _calendar_link(calendar_id: str) -> str:
    """
    Returns a browser URL that opens this calendar in Google Calendar's
    web UI. The calendarId is base64-encoded with padding stripped, which
    is the format Google Calendar's web URLs expect for the `cid`
    parameter.
    """
    import base64
    encoded = base64.b64encode(calendar_id.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"https://calendar.google.com/calendar/u/0/r?cid={encoded}"



def _load_user_config() -> dict:
    """Reads the small persisted config file, or returns {} if it doesn't exist yet."""
    if os.path.exists(USER_CONFIG_PATH):
        with open(USER_CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}


def _save_user_config(config: dict) -> None:
    """Writes the persisted config file."""
    with open(USER_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def get_source_calendar_id(exclude_name: str = "Chrono Schedule") -> str:
    """
    Returns the calendar ID for the user's real-life schedule — the one
    onboarding checks for Case 1 / Case 2 detection.

    The user is asked to name this calendar ONCE. That choice is saved to
    config/user_config.json and reused on every future run, so this never
    has to guess again even if more calendars get added later.

    Set FORCE_REASK = True below (or delete the "source_calendar_id" key
    from user_config.json) if you ever switch which calendar you use.
    """
    config = _load_user_config()

    if "source_calendar_id" in config:
        return config["source_calendar_id"]

    # Not configured yet — ask, once.
    calendars = [c for c in list_calendars() if c["summary"] != exclude_name]

    print("\nWhich calendar holds your real schedule? Chrono needs to know this once.\n")
    for i, cal in enumerate(calendars, start=1):
        marker = " (primary)" if cal["primary"] else ""
        print(f"  {i}. {cal['summary']}{marker}")

    choice = None
    while choice is None:
        raw = input(f"\nEnter a number (1-{len(calendars)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(calendars):
            choice = calendars[int(raw) - 1]
        else:
            print("Invalid choice, try again.")

    config["source_calendar_id"] = choice["id"]
    config["source_calendar_name"] = choice["summary"]
    _save_user_config(config)

    print(f"Saved. Using '{choice['summary']}' as your source calendar from now on.\n")
    return choice["id"]


def reset_source_calendar() -> None:
    """
    Clears the saved source calendar choice so get_source_calendar_id()
    asks again on the next call. Use this if you switch which calendar
    holds your real schedule.
    """
    config = _load_user_config()
    config.pop("source_calendar_id", None)
    config.pop("source_calendar_name", None)
    _save_user_config(config)
    print("Source calendar choice cleared. You'll be asked again next run.")


def get_events(days_ahead: int = 7, calendar_id: str = "primary") -> list[dict]:
    """
    Fetches events from the given calendar, starting at the beginning of
    today (local midnight) through `days_ahead` days forward.

    Important: timeMin is set to local midnight today, not the current
    moment and not UTC midnight. Using "now" as timeMin would silently
    exclude any event on today's date that already started or ended
    before the script happened to run — e.g. running at 9 AM would drop
    a 6 AM wake-up block entirely, making free-slot detection think the
    morning was open when it wasn't. UTC midnight is also wrong for IST
    (UTC+5:30) — it lands at 5:30 AM local time and would still chop off
    early-morning events.

    Args:
        days_ahead: how many days into the future to look.
        calendar_id: which calendar to read. Defaults to "primary".
                     Run list_calendars() to find the ID of a secondary
                     calendar like "Changing My Daily Life".

    Returns:
        List of dicts: [{"summary": str, "start": str, "end": str}, ...]
    """
    service = get_calendar_service()

    local_midnight = datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone()
    now = local_midnight.isoformat()
    later = (local_midnight + datetime.timedelta(days=days_ahead)).isoformat()

    events_result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=now,
            timeMax=later,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    events = events_result.get("items", [])

    parsed = []
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        end = event["end"].get("dateTime", event["end"].get("date"))
        parsed.append(
            {
                "summary": event.get("summary", "(no title)"),
                "start": start,
                "end": end,
            }
        )

    return parsed


def find_free_slots(
    days_ahead: int = 7,
    day_start: str = "07:00",
    day_end: str = "23:00",
    calendar_id: str = "primary",
) -> dict:
    """
    Detects free time slots between existing events for each day.

    Returns:
        dict mapping date string -> list of {"start": str, "end": str} free windows
    """
    events = get_events(days_ahead=days_ahead, calendar_id=calendar_id)

    # Group events by date
    by_date: dict[str, list[tuple[datetime.time, datetime.time]]] = {}
    for ev in events:
        start_dt = _parse_dt(ev["start"])
        end_dt = _parse_dt(ev["end"])
        if start_dt is None or end_dt is None:
            continue  # skip all-day events for slot math
        date_key = start_dt.date().isoformat()
        by_date.setdefault(date_key, []).append((start_dt.time(), end_dt.time()))

    free_slots = {}
    day_start_t = datetime.time.fromisoformat(day_start)
    day_end_t = datetime.time.fromisoformat(day_end)

    today = datetime.date.today()
    for offset in range(days_ahead):
        date = today + datetime.timedelta(days=offset)
        date_key = date.isoformat()
        busy = sorted(by_date.get(date_key, []))

        slots = []
        cursor = day_start_t
        for busy_start, busy_end in busy:
            if busy_start > cursor:
                slots.append({"start": cursor.strftime("%H:%M"), "end": busy_start.strftime("%H:%M")})
            if busy_end > cursor:
                cursor = busy_end
        if cursor < day_end_t:
            slots.append({"start": cursor.strftime("%H:%M"), "end": day_end_t.strftime("%H:%M")})

        free_slots[date_key] = slots

    return free_slots


def _parse_dt(value: str) -> datetime.datetime | None:
    """Parses an ISO datetime string. Returns None for all-day (date-only) events."""
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None  # likely a date-only all-day event like "2025-06-20"


if __name__ == "__main__":
    print("Your calendars:\n")
    calendars = list_calendars()
    for cal in calendars:
        marker = " (primary)" if cal["primary"] else ""
        print(f"  {cal['summary']}{marker}")
        print(f"    id: {cal['id']}")

    # ── Set this to the calendar you actually want to read from ──
    # Find its name in the printed list above. "primary" is your
    # default Google account calendar, which is often empty if you
    # keep a separate named calendar for your real schedule.
    TARGET_CALENDAR = "560fdcb69e7f74c6b5a61b70a9b223e197c0e56745825d94c6024944d6a7e345@group.calendar.google.com"

    print(f"\nFetching events for the next 7 days from: {TARGET_CALENDAR}\n")
    events = get_events(days_ahead=7, calendar_id=TARGET_CALENDAR)
    if not events:
        print("  (no events found — check TARGET_CALENDAR is set correctly)")
    for e in events:
        print(f"  {e['start']}  →  {e['end']}   {e['summary']}")

    print("\nDetected free slots:\n")
    # day_start matches actual wake-up time (06:00) so free slots reflect
    # the real day boundary instead of an arbitrary default
    free = find_free_slots(days_ahead=7, day_start="06:00", calendar_id=TARGET_CALENDAR)
    for date, slots in free.items():
        print(f"  {date}:")
        for s in slots:
            print(f"    {s['start']} – {s['end']}")