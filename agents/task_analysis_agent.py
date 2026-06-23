"""
Task Analysis Agent.

Takes the task list produced by onboarding.py and enriches each task with:

  cognitive_load:  "deep" | "light"   — how much focus the task needs.
                   Deep work (DSA, coding, research) gets scheduled in
                   peak-focus morning slots by the Optimization Agent;
                   light work (admin, calls, chores) fills lower-energy
                   periods.

  urgency:         "high" | "medium" | "low"  — onboarding already
                   computes this FROM A DEADLINE DATE when one exists.
                   This agent fills the gap for tasks with NO deadline
                   (e.g. ongoing weekly habits), since "no deadline"
                   does not mean "no urgency" — recurring commitments
                   like Gym still deserve a real priority signal.

  confidence:      "high" | "low"  — how sure the LLM is about its own
                   classification. A "low" confidence triggers a
                   clarifying question back to the user instead of
                   silently guessing, per the project's design choice
                   that ambiguous tasks should be asked about, not
                   assumed.

This agent does NOT call the LLM per task one at a time — all tasks are
sent in a single batched prompt, since classifying 3-10 short task
names is well within one context window and avoids unnecessary API calls.
"""

import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "tools"))
from llm_backend import call_llm


SYSTEM_PROMPT = """You are the Task Analysis component of an AI scheduling assistant called Chrono.

For each task you are given, classify it along two dimensions:

1. cognitive_load: either "deep" or "light"
   - "deep": requires sustained focus, learning, problem-solving, or creative effort
     (e.g. studying, coding, writing, research, exam prep)
   - "light": lower-focus, routine, physical, or administrative
     (e.g. exercise, chores, replying to emails, simple errands)

2. urgency: either "high", "medium", or "low"
   - Base this on how time-pressured the task inherently is, independent of any
     deadline already given. A task explicitly described as urgent, an exam, or
     a test should be "high". A recurring health/fitness habit is usually "medium"
     (important but not time-critical day to day). Open-ended personal projects
     are usually "low" unless described otherwise.

For each task, also report a confidence level: "high" or "low".
Use "low" confidence ONLY when the task name is genuinely ambiguous and you
cannot reasonably guess its cognitive load or urgency without more context
(e.g. a vague project name like "Project-D" with no description of what it
involves). Do not use "low" confidence for clearly understandable tasks just
to be cautious — most tasks should get "high" confidence.

If confidence is "low", include a short, specific clarifying question that
would resolve the ambiguity (e.g. "What kind of work does Project-D involve —
coding, writing, or something else?"). Otherwise set clarifying_question to null.

Respond ONLY with valid JSON in this exact shape, no other text:

{
  "classifications": [
    {
      "name": "<task name, copied exactly as given>",
      "cognitive_load": "deep" | "light",
      "urgency": "high" | "medium" | "low",
      "confidence": "high" | "low",
      "clarifying_question": "<string or null>"
    }
  ]
}
"""

# Strict-mode JSON schema for this agent's response. Mirrors the prompt's
# shape exactly. Using Groq's strict Structured Outputs here too (not just
# on the Optimization Agent) is cheap insurance: this agent hasn't hit the
# "400 Failed to validate JSON" error yet, but it uses the same
# json_object mode that caused it elsewhere, and there's no reason to
# wait for it to fail in the field when strict mode is free and available
# on this model.
CLASSIFICATION_RESPONSE_SCHEMA = {
    "name": "chrono_task_classification",
    "schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "cognitive_load": {"type": "string", "enum": ["deep", "light"]},
                        "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
                        "confidence": {"type": "string", "enum": ["high", "low"]},
                        "clarifying_question": {"type": ["string", "null"]},
                    },
                    "required": [
                        "name",
                        "cognitive_load",
                        "urgency",
                        "confidence",
                        "clarifying_question",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["classifications"],
        "additionalProperties": False,
    },
}


def _build_user_prompt(tasks: list[dict]) -> str:
    """Builds a compact, readable task list for the prompt."""
    lines = ["Classify the following tasks:\n"]
    for t in tasks:
        detail_bits = []
        if t.get("hours"):
            detail_bits.append(f"{t['hours']} hrs/{t.get('period', 'week')}")
        if t.get("frequency_per_week"):
            detail_bits.append(f"{t['frequency_per_week']}x/week")
        if t.get("deadline"):
            detail_bits.append(f"deadline {t['deadline']}")
        detail = f" ({', '.join(detail_bits)})" if detail_bits else ""
        lines.append(f"- {t['name']}{detail}")
    return "\n".join(lines)


def _normalize_name(name: str) -> str:
    """Lowercases and strips whitespace so minor rephrasing doesn't break name matching."""
    return name.strip().lower()


def analyze_tasks(tasks: list[dict]) -> dict:
    """
    Sends all tasks to Groq in one batched call and merges the
    classification results back onto each task dict.

    Args:
        tasks: the "tasks" list from onboarding.run_onboarding()'s output.
               Each task must have at least a "name" key.

    Returns:
        {
          "tasks": [...same tasks, enriched with cognitive_load/urgency/confidence...],
          "needs_clarification": [ {"name": str, "question": str}, ... ]
        }
    """
    if not tasks:
        return {"tasks": [], "needs_clarification": []}

    user_prompt = _build_user_prompt(tasks)

    try:
        result = call_llm(SYSTEM_PROMPT, user_prompt, expect_json=True, json_schema=CLASSIFICATION_RESPONSE_SCHEMA)
        raw_classifications = result.get("classifications", [])
        print(f"  [Task Analysis] Groq returned {len(raw_classifications)} classification(s) for {len(tasks)} task(s).")
    except Exception as e:
        # If the LLM call fails outright (network, auth, bad JSON), fall back
        # to a safe default rather than crashing the whole pipeline. Every
        # task gets flagged low-confidence so the user is asked rather than
        # silently given a wrong classification.
        print(f"  [Task Analysis] Groq call failed ({e}); falling back to manual review.")
        raw_classifications = []

    # Primary lookup: normalized name (case/whitespace-insensitive), since
    # the model is asked to copy names exactly but isn't 100% reliable
    # about it (e.g. might add a trailing period or change capitalization).
    by_normalized_name = {
        _normalize_name(c["name"]): c for c in raw_classifications if "name" in c
    }

    needs_clarification = []

    for i, task in enumerate(tasks):
        match = by_normalized_name.get(_normalize_name(task["name"]))

        # Fallback: if exact/normalized name matching failed but the model
        # returned the same NUMBER of classifications in the same order we
        # sent the tasks, assume positional correspondence rather than
        # treating every task as unclassified. This recovers gracefully
        # from cases where the model rephrased a name slightly.
        if match is None and len(raw_classifications) == len(tasks):
            match = raw_classifications[i]

        if match is None:
            # Genuinely no classification available for this task.
            task["cognitive_load"] = None
            task.setdefault("urgency", None)
            task["analysis_confidence"] = "low"
            needs_clarification.append(
                {"name": task["name"], "question": f"What kind of task is '{task['name']}' — deep focus work or lighter/routine work?"}
            )
            continue

        task["cognitive_load"] = match.get("cognitive_load")

        # Only overwrite urgency if onboarding left it as None (i.e. no
        # deadline was given). A deadline-derived urgency from onboarding
        # is more reliable than the LLM's guess and should not be overridden.
        if task.get("urgency") is None:
            task["urgency"] = match.get("urgency")

        task["analysis_confidence"] = match.get("confidence", "high")

        if match.get("confidence") == "low" and match.get("clarifying_question"):
            needs_clarification.append(
                {"name": task["name"], "question": match["clarifying_question"]}
            )

    return {"tasks": tasks, "needs_clarification": needs_clarification}


def run_clarifications_interactively(analysis: dict) -> dict:
    """
    Asks the user each clarifying question from the terminal, then re-runs
    that single task back through Groq with the user's clarification added
    as context, to get an updated classification.
    """
    if not analysis["needs_clarification"]:
        return analysis

    print("\nA couple of tasks need clarification before scheduling:\n")

    for item in analysis["needs_clarification"]:
        answer = input(f"  {item['question']} ").strip()

        # Find the matching task and re-classify it with the extra context
        task = next((t for t in analysis["tasks"] if t["name"] == item["name"]), None)
        if task is None or not answer:
            continue

        clarified_prompt = (
            f"Task: {item['name']}\n"
            f"Additional context from the user: {answer}\n\n"
            "Classify this single task as you would normally."
        )
        try:
            result = call_llm(SYSTEM_PROMPT, clarified_prompt, expect_json=True, json_schema=CLASSIFICATION_RESPONSE_SCHEMA)
            classification = result.get("classifications", [{}])[0]
            task["cognitive_load"] = classification.get("cognitive_load", task["cognitive_load"])
            if task.get("urgency") is None:
                task["urgency"] = classification.get("urgency")
            task["analysis_confidence"] = "high"
        except Exception as e:
            print(f"  Couldn't re-classify '{item['name']}' ({e}); leaving as-is.")

    analysis["needs_clarification"] = []
    return analysis


if __name__ == "__main__":
    # Quick standalone test with a few example tasks, independent of the
    # full onboarding flow.
    sample_tasks = [
        {"name": "ML Revision", "hours": 12.0, "period": "week", "deadline": None, "urgency": None},
        {"name": "Gym", "hours": None, "frequency_per_week": 4.0, "deadline": None, "urgency": None},
        {"name": "Amazon ML Test", "hours": 10.0, "period": "week", "deadline": "2026-06-23", "urgency": "medium"},
        {"name": "Project-D", "hours": 10.0, "period": "week", "deadline": None, "urgency": None},
    ]

    analysis = analyze_tasks(sample_tasks)

    print("\n--- Classification result ---")
    for t in analysis["tasks"]:
        print(t)

    analysis = run_clarifications_interactively(analysis)

    print("\n--- After clarifications ---")
    for t in analysis["tasks"]:
        print(t)
