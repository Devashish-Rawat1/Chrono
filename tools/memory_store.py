"""
Chrono's persistent memory (local SQLite).

WHAT THIS IS
    A small, local, dependency-free fact store so Chrono remembers things
    between runs — wake/sleep times, the repo it works on, music taste,
    goals, anything. Nothing here is sent anywhere: the whole store is one
    file on this machine (config/memory.db).

WHY SQLITE AND NOT A CLOUD DB
    Chrono is a desktop app using the owner's own accounts and machine.
    There's no second device to sync with and no other user to share with,
    so a hosted database would add network latency and infrastructure for
    zero benefit. sqlite3 ships with Python — no install, one file, easy
    to inspect, back up, or delete.

DATA MODEL
    facts(key, value, category, updated_at)
        key       unique, dotted namespace e.g. "schedule.wake_time"
        value     JSON-encoded, so any type round-trips cleanly
        category  coarse grouping ("schedule", "music", "code", "general")
                  used to pull a relevant slice into a prompt
    events(id, kind, summary, created_at)
        An append-only log of things that happened ("pushed 3 files",
        "planned week"). Useful for "what did I do recently?" and for
        future semantic recall — kept deliberately simple for now.

TYPICAL USE
    from memory_store import remember, recall, recall_category, forget
    remember("schedule.wake_time", "07:00", category="schedule")
    recall("schedule.wake_time")            -> "07:00"
    recall_category("schedule")             -> {"schedule.wake_time": "07:00", ...}
    memory_prompt_block("schedule")         -> text to paste into a system prompt
"""

import os
import json
import sqlite3
import datetime as _dt

_HERE = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_HERE, "..", "config", "memory.db")


def _conn():
    """Opens the DB, creating the file and schema on first use."""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    c = sqlite3.connect(_DB_PATH)
    c.execute("""
        CREATE TABLE IF NOT EXISTS facts (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            category   TEXT DEFAULT 'general',
            updated_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL,
            summary    TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    c.commit()
    return c


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# ── facts ────────────────────────────────────────────────────────────

def remember(key: str, value, category: str = "general") -> dict:
    """
    Stores (or overwrites) a fact. `value` may be any JSON-serialisable
    type — it's encoded so ints/lists/dicts survive the round trip.
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("remember() needs a non-empty key")
    with _conn() as c:
        c.execute(
            "INSERT INTO facts(key, value, category, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "category=excluded.category, updated_at=excluded.updated_at",
            (key, json.dumps(value), category, _now()),
        )
    return {"key": key, "value": value, "category": category}


def recall(key: str, default=None):
    """Returns a single fact's value, or `default` if it isn't stored."""
    with _conn() as c:
        row = c.execute("SELECT value FROM facts WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return row[0]


def recall_category(category: str) -> dict:
    """Returns {key: value} for every fact in a category."""
    with _conn() as c:
        rows = c.execute(
            "SELECT key, value FROM facts WHERE category=? ORDER BY key", (category,)
        ).fetchall()
    out = {}
    for k, v in rows:
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def recall_all() -> dict:
    """Every fact, grouped by category — handy for a 'what do you know about me' view."""
    with _conn() as c:
        rows = c.execute(
            "SELECT category, key, value FROM facts ORDER BY category, key"
        ).fetchall()
    out = {}
    for cat, k, v in rows:
        try:
            val = json.loads(v)
        except json.JSONDecodeError:
            val = v
        out.setdefault(cat, {})[k] = val
    return out


def forget(key: str) -> bool:
    """Deletes one fact. Returns True if something was actually removed."""
    with _conn() as c:
        cur = c.execute("DELETE FROM facts WHERE key=?", (key,))
        return cur.rowcount > 0


def forget_all(category: str | None = None) -> int:
    """Wipes a category (or the whole store). Returns rows deleted."""
    with _conn() as c:
        if category:
            cur = c.execute("DELETE FROM facts WHERE category=?", (category,))
        else:
            cur = c.execute("DELETE FROM facts")
        return cur.rowcount


# ── events (append-only activity log) ────────────────────────────────

def log_event(kind: str, summary: str) -> None:
    """Records that something happened, for later 'what did I do?' recall."""
    with _conn() as c:
        c.execute(
            "INSERT INTO events(kind, summary, created_at) VALUES(?,?,?)",
            (kind, summary, _now()),
        )


def recent_events(limit: int = 10, kind: str | None = None) -> list:
    """Most recent events, newest first."""
    with _conn() as c:
        if kind:
            rows = c.execute(
                "SELECT kind, summary, created_at FROM events WHERE kind=? "
                "ORDER BY id DESC LIMIT ?", (kind, limit)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT kind, summary, created_at FROM events "
                "ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [{"kind": k, "summary": s, "at": t} for k, s, t in rows]


# ── prompt helper ────────────────────────────────────────────────────

def memory_prompt_block(*categories: str, max_facts: int = 25) -> str:
    """
    Renders stored facts as a compact text block to prepend to a system
    prompt, so the LLM "remembers" without any model-side state. Pass one
    or more categories to include only the relevant slice; pass none for
    everything.

    Returns "" when there's nothing stored, so callers can add it
    unconditionally without producing an empty heading.
    """
    data = {}
    if categories:
        for cat in categories:
            data.update(recall_category(cat))
    else:
        for cat_facts in recall_all().values():
            data.update(cat_facts)
    if not data:
        return ""
    lines = []
    for i, (k, v) in enumerate(sorted(data.items())):
        if i >= max_facts:
            break
        lines.append(f"- {k}: {v}")
    return "What you already know about the user:\n" + "\n".join(lines)


if __name__ == "__main__":
    # Smoke test: python tools/memory_store.py
    remember("schedule.wake_time", "07:00", category="schedule")
    remember("schedule.sleep_time", "23:00", category="schedule")
    remember("code.repo_path", "C:/Users/devas/Desktop/Chrono", category="code")
    log_event("push", "Pushed 3 files to Chrono")
    print("recall:", recall("schedule.wake_time"))
    print("category:", recall_category("schedule"))
    print("events:", recent_events(3))
    print("\nprompt block:\n" + memory_prompt_block("schedule"))