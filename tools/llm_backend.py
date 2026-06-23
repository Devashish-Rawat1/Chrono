"""
LLM backend selector.

Every agent imports call_llm from HERE instead of importing directly
from groq_client. This keeps one indirection point so a different
backend can be swapped in later without touching agent code.

Currently Groq-only. A local Ollama backend (Qwen3 8B) was built and
tested here, but was removed after a side-by-side comparison: on this
project's hardware, local inference was roughly 10-20x slower per call
and noticeably less reliable at the Optimization Agent's multi
-constraint reasoning (repeated the same validation failure across
retries that Groq gets right). Not worth the tradeoff for this project.

A Gemini API key is also available if a second cloud backend is ever
wanted (e.g. as a Groq rate-limit fallback) — not wired in yet.

Usage in agents:
    from llm_backend import call_llm
"""

import os

_BACKEND = os.environ.get("LLM_BACKEND", "groq").lower()

if _BACKEND == "groq":
    from groq_client import call_llm as _call_llm
else:
    raise ValueError(f"Unknown LLM_BACKEND '{_BACKEND}' — only 'groq' is currently supported.")


def call_llm(
    system_prompt: str,
    user_prompt: str,
    expect_json: bool = True,
    json_schema: dict | None = None,
    max_completion_tokens: int = 7000,
    reasoning_effort: str = "low",
):
    """
    Delegates to whichever backend is currently selected via LLM_BACKEND.

    json_schema: optional {"name": str, "schema": dict} -- see
    groq_client.call_llm for the full explanation. Passed straight
    through; only groq_client currently does anything with it.
    max_completion_tokens / reasoning_effort: see groq_client.call_llm --
    raise max_completion_tokens for agents generating long output (many
    schedule blocks across many days); raise reasoning_effort only if a
    prompt's reasoning is genuinely failing at "low", since higher
    effort eats into the same token budget.
    """
    return _call_llm(
        system_prompt,
        user_prompt,
        expect_json=expect_json,
        json_schema=json_schema,
        max_completion_tokens=max_completion_tokens,
        reasoning_effort=reasoning_effort,
    )


def current_backend() -> str:
    """Returns which backend is active — useful for debug prints."""
    return _BACKEND
