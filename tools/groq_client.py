"""
Thin wrapper around the Groq API for structured JSON classification.

IMPORTANT: this is Groq (console.groq.com) — the fast-inference company
that hosts open-weight models like Llama and GPT-OSS — NOT Grok / xAI
(api.x.ai), which is a different company and a different API entirely.
A Groq key will not work against api.x.ai and vice versa.

Uses the OpenAI-compatible chat/completions endpoint. Requires a
GROQ_API_KEY environment variable, loaded via main.py + python-dotenv
from a .env file at the project root.

Docs: https://console.groq.com/docs/api-reference
"""

import os
import json
import requests

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# llama-3.3-70b-versatile was deprecated by Groq on 2026-06-17. Current
# recommended replacement for quality/reasoning workloads on the free tier:
# openai/gpt-oss-120b. Override with GROQ_MODEL in your environment if
# Groq's lineup changes again.
DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


def call_llm(system_prompt: str, user_prompt: str, expect_json: bool = True) -> dict | str:
    """
    Sends a single chat completion request to Groq.

    Args:
        system_prompt: sets the model's role/behavior for this call.
        user_prompt: the actual content/question.
        expect_json: if True, parses the response as JSON and returns a
                     dict. Raises ValueError if the response isn't valid
                     JSON — the caller should catch this and fall back
                     gracefully rather than crash the whole pipeline.

    Returns:
        Parsed JSON dict if expect_json=True, otherwise the raw text string.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GROQ_API_KEY is not set. Add it to your .env file at the project "
            "root and make sure main.py (or whatever entry point you're "
            "running) calls load_dotenv() before this is imported."
        )

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,  # low temperature: we want consistent classification, not creativity
    }
    if expect_json:
        # Asks the API to guarantee valid JSON output, removing the need
        # to strip markdown code fences from the response ourselves.
        payload["response_format"] = {"type": "json_object"}

    response = requests.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    content = response.json()["choices"][0]["message"]["content"]

    if expect_json:
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"Groq did not return valid JSON: {content!r}") from e

    return content


if __name__ == "__main__":
    # Load .env directly when running this file standalone (not via main.py)
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(__file__), "..", "config", ".env")
    load_dotenv(dotenv_path=env_path)

    result = call_llm(
        system_prompt="You are a helpful assistant. Respond only in JSON.",
        user_prompt='Return {"status": "ok", "message": "Groq connection working"}',
    )
    print(result)
