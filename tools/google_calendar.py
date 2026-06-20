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