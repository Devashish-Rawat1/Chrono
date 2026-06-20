"""
Google Tasks tool — reads raw tasks from Google Tasks.

This is the raw input feed for the Task Analysis Agent: task titles,
due dates, and notes, pulled directly from "My Tasks" instead of being
typed in manually.

Shares the same credentials.json as google_calendar.py. Uses a combined
scope so one login grants both Calendar and Tasks access.
"""

import os
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Combined scopes — Calendar (full access) + Tasks (read-only).
# Must match google_calendar.py exactly since they share token.json.
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks.readonly",
]

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "credentials.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token.json")


def get_tasks_service():
    """Authenticates and returns a Google Tasks API service object."""
    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

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

        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return build("tasks", "v1", credentials=creds)


def list_task_lists() -> list[dict]:
    """Lists every task list the user has (e.g. 'My Tasks')."""
    service = get_tasks_service()
    result = service.tasklists().list().execute()
    return [{"title": tl["title"], "id": tl["id"]} for tl in result.get("items", [])]


def get_tasks(task_list_id: str = "@default", include_completed: bool = False) -> list[dict]:
    """
    Fetches tasks from a given task list.

    Args:
        task_list_id: ID of the task list, or "@default" for "My Tasks".
        include_completed: whether to include tasks already marked done.

    Returns:
        List of dicts: [{"title": str, "due": str | None, "notes": str | None, "status": str}, ...]
    """
    service = get_tasks_service()

    result = (
        service.tasks()
        .list(
            tasklist=task_list_id,
            showCompleted=include_completed,
            showHidden=include_completed,
        )
        .execute()
    )

    tasks = result.get("items", [])

    parsed = []
    for t in tasks:
        parsed.append(
            {
                "title": t.get("title", "(untitled task)"),
                "due": t.get("due"),  # ISO date string, or None if no due date set
                "notes": t.get("notes"),
                "status": t.get("status"),  # "needsAction" or "completed"
            }
        )

    return parsed


def days_until_due(due_iso: str | None) -> int | None:
    """
    Converts a task's due date string into 'days remaining' — useful for
    the Task Analysis Agent's urgency classification.
    Returns None if the task has no due date.
    """
    if not due_iso:
        return None
    due_date = datetime.datetime.fromisoformat(due_iso.replace("Z", "+00:00")).date()
    today = datetime.date.today()
    return (due_date - today).days


if __name__ == "__main__":
    print("Your task lists:\n")
    lists = list_task_lists()
    for tl in lists:
        print(f"  {tl['title']}")
        print(f"    id: {tl['id']}")

    print("\nTasks in 'My Tasks' (@default):\n")
    tasks = get_tasks(task_list_id="@default")
    if not tasks:
        print("  (no tasks found)")
    for t in tasks:
        days_left = days_until_due(t["due"])
        due_str = f"due in {days_left}d" if days_left is not None else "no due date"
        print(f"  [{t['status']}] {t['title']}  —  {due_str}")
        if t["notes"]:
            print(f"      notes: {t['notes']}")
