"""
Thin wrapper around the OpenRouter API for natural-language generation.

OpenRouter (openrouter.ai) is an OpenAI-compatible gateway to many model
providers behind one endpoint and one key. Chrono uses it for ONE thing:
the Stage-3 explanation agent's prose summary. Keeping that off Groq means
the explanation never competes with scheduling/classification for Groq's
rate limit -- if Groq is busy, the "here's your week" summary still works.

Only prose generation is needed here (no JSON schema), so this client is
deliberately minimal: build messages, POST, return choices[0].message
.content, with a short retry on transient/rate-limit errors.

Requires OPENROUTER_API_KEY in the environment (loaded via main.py +
python-dotenv from the project-root .env). Get a free key at
openrouter.ai -- the free tier (50 requests/day) is plenty for one
explanation per planning run.

Docs: https://openrouter.ai/docs
"""

import os
import time
import requests

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Default model for the explanation. Pinned to a fast, non-reasoning
# general-chat model rather than the "openrouter/free" auto-router.
#
# The auto-router picks ANY available free model, including reasoning
# models that stream their scratchpad ("We need to produce a summary...")
# and long garbage instead of clean prose -- which is exactly what broke
# the explanation. A pinned instruct model avoids that. Llama 3.3 70B is
# a solid, widely-available free instruct model; if it's rate-limited or
# pulled, override via OPENROUTER_MODEL. Other decent free picks:
#   meta-llama/llama-4-maverick:free, deepseek/deepseek-chat-v3:free
# Verify current availability at openrouter.ai/models (the free roster
# rotates). NOTE: avoid ":free" reasoning models (r1, qwq, etc.) here --
# they leak reasoning text into the prose.
DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

MAX_RETRIES = 3
MAX_BACKOFF_SECONDS = 20


def call_llm(
    system_prompt: str,
    user_prompt: str,
    expect_json: bool = False,
    json_schema: dict | None = None,
    max_completion_tokens: int = 500,
    reasoning_effort: str = "low",
) -> str:
    """
    Sends a chat completion to OpenRouter and returns the text content.

    The signature matches llm_backend.call_llm / groq_client.call_llm so
    this can be a drop-in for prose calls. expect_json / json_schema /
    reasoning_effort are accepted for signature compatibility but this
    client is used for plain prose; if expect_json is True, basic JSON
    object mode is requested, but no schema enforcement is done.

    Returns the response text (str). Raises on unrecoverable errors so the
    caller (the explanation agent) can fall back to its deterministic
    summary.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to your .env file at the "
            "project root (get a free key at openrouter.ai) so the explanation "
            "agent can use OpenRouter instead of Groq."
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Optional attribution headers; harmless if unset.
        "HTTP-Referer": "https://github.com/Devashish-Rawat1",
        "X-Title": "Chrono Weekly Planner",
    }

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,  # a bit of warmth for a friendly summary
        "max_tokens": max_completion_tokens,
    }
    if expect_json:
        payload["response_format"] = {"type": "json_object"}

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=25)
        except requests.RequestException as e:
            last_error = e
            if attempt == MAX_RETRIES:
                break
            time.sleep(min(2 ** attempt, MAX_BACKOFF_SECONDS))
            continue

        # Rate limited or transient server-side error -> short retry.
        if response.status_code == 429 or response.status_code in (500, 502, 503, 504, 520, 522, 524):
            last_error = requests.HTTPError(
                f"{response.status_code} from OpenRouter (transient/rate-limit); retrying",
                response=response,
            )
            if attempt == MAX_RETRIES:
                break
            wait = min(2 ** attempt, MAX_BACKOFF_SECONDS)
            print(f"  [OpenRouter] {response.status_code} — retrying in {wait:.0f}s ({attempt + 1}/{MAX_RETRIES})...")
            time.sleep(wait)
            continue

        if response.status_code >= 400:
            # Non-retryable client error: surface a clean one-line message.
            snippet = response.text[:200].replace("\n", " ")
            raise requests.HTTPError(f"{response.status_code} error from OpenRouter: {snippet}")

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"Unexpected OpenRouter response shape: {e}; body={str(data)[:200]}")

    raise last_error if last_error else RuntimeError("OpenRouter call failed for an unknown reason")


def current_model() -> str:
    return DEFAULT_MODEL