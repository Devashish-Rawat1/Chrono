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
import time
import requests

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Which Groq model to use. Override via GROQ_MODEL in your environment.
#
# IMPORTANT trade-off for THIS project on the FREE tier, where the
# binding constraint is tokens-per-minute (TPM), not quality:
#
#   openai/gpt-oss-120b  -- 8K TPM free.  A REASONING model: it spends
#       hidden "thinking" tokens before writing any JSON, so a 7-day
#       multi-task schedule can run out of room mid-output even after
#       clamping. Strongest reasoning, but the tightest budget here.
#
#   llama-3.3-70b-versatile -- 12K TPM free (50% more headroom). A
#       current Groq PRODUCTION model (not deprecated). NOT a reasoning
#       model, so every token in the budget goes to actual output --
#       no hidden thinking tokens eating the ceiling. For structured
#       schedule placement this is plenty capable, and the extra TPM
#       plus lack of reasoning overhead makes it MUCH less likely to
#       hit pacing waits or truncation on the free tier. Recommended
#       default for this project unless/until you upgrade your tier.
#
# To switch, set in config/.env:  GROQ_MODEL=llama-3.3-70b-versatile
# The TPM limit and reasoning_effort handling below auto-adjust to
# whichever model is selected -- nothing else needs to change.
DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

MAX_RATE_LIMIT_RETRIES = 3
MAX_BACKOFF_SECONDS = 30

# Per-model free-tier TPM ceilings, from Groq's published rate-limits
# table. Groq counts a SINGLE request's reserved size (input tokens +
# max_completion_tokens) against this bucket, not just actual usage --
# so a request can be rejected even on an empty bucket if its own
# reserved size alone exceeds this number. These are FREE-tier numbers;
# if you upgrade, override with GROQ_TPM_LIMIT to skip the table.
_MODEL_TPM_LIMITS = {
    "openai/gpt-oss-120b": 8000,
    "openai/gpt-oss-20b": 8000,
    "llama-3.3-70b-versatile": 12000,
    "llama-3.1-8b-instant": 6000,
    "meta-llama/llama-4-scout-17b-16e-instruct": 30000,
    "qwen/qwen3-32b": 6000,
}

# Only these models accept the "reasoning_effort" parameter. Sending it
# to a non-reasoning model (e.g. the llama models) causes a 400, so the
# payload below omits it entirely unless the active model is in this set.
_REASONING_MODELS = {"openai/gpt-oss-120b", "openai/gpt-oss-20b", "openai/gpt-oss-safeguard-20b"}

# Which structured-output mode each model supports, from Groq's
# structured-outputs docs. This matters because response_format is NOT
# uniform across models:
#
#   "strict"      -> supports response_format json_schema with
#                    strict:true (constrained decoding -- the model
#                    physically cannot emit invalid JSON or violate the
#                    schema). Strongest guarantee. ONLY the gpt-oss
#                    models support this.
#   "besteffort"  -> supports response_format json_schema with
#                    strict:false (tries to match the schema, but can
#                    still 400 or return schema-invalid JSON). A few
#                    more models support this.
#   "json_object" -> supports only response_format {"type":
#                    "json_object"} (valid JSON syntax, no schema
#                    enforcement at all). ALL models support this as a
#                    floor.
#
# Sending strict json_schema to a model that doesn't support it returns
# "This model does not support response format `json_schema`", which is
# exactly what broke when the default model was switched to a llama
# model. _resolve_structured_mode() below picks the best mode the
# ACTIVE model actually supports, so any model can be selected via
# GROQ_MODEL without the caller having to know these differences.
_STRICT_SCHEMA_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}
_BESTEFFORT_SCHEMA_MODELS = {
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-safeguard-20b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
}


def _resolve_structured_mode(model: str, has_schema: bool) -> str:
    """Returns which structured-output mode to actually use for `model`:
    'strict', 'besteffort', or 'json_object'. Falls back gracefully so
    that selecting a model without strict support (e.g. a llama model)
    silently downgrades to the best mode it DOES support, rather than
    400-ing."""
    if not has_schema:
        return "json_object"
    if model in _STRICT_SCHEMA_MODELS:
        return "strict"
    if model in _BESTEFFORT_SCHEMA_MODELS:
        return "besteffort"
    return "json_object"

# Resolved TPM ceiling for the active model. GROQ_TPM_LIMIT overrides the
# table (e.g. after a tier upgrade); otherwise look up the model, falling
# back to a conservative 8000 if it's one not listed above.
_env_tpm = os.environ.get("GROQ_TPM_LIMIT")
ACCOUNT_TPM_LIMIT = int(_env_tpm) if _env_tpm else _MODEL_TPM_LIMITS.get(DEFAULT_MODEL, 8000)

# Buffer subtracted from ACCOUNT_TPM_LIMIT before sizing a request, to
# absorb (a) imprecision in the char-based token estimate below, which
# is calibrated but not exact, and (b) any small fixed overhead Groq
# adds per request that isn't visible in the prompt text itself.
SAFETY_MARGIN_TOKENS = 700

# Calibrated against this project's own prompts by comparing prompt
# character counts to the exact input-token counts Groq's 413 error
# revealed (679 and 1691 tokens) -- this text tokenizes at roughly
# 2.9-3.4 chars/token, denser than the ~4 chars/token rule of thumb for
# plain English prose, because structured/technical text (JSON-like
# payloads, short keywords) packs more tokens per character. Using 2.8
# deliberately OVERESTIMATES token count slightly, biasing toward
# leaving more headroom rather than less.
_CHARS_PER_TOKEN_ESTIMATE = 2.8

# Below this, there usually isn't enough room left for the model to
# produce a useful JSON response anyway (especially once hidden
# reasoning tokens are accounted for) -- better to fail with a clear,
# actionable message than send a request almost certain to truncate.
MIN_VIABLE_COMPLETION_TOKENS = 600


def _estimate_tokens(text: str) -> int:
    """Rough, deliberately conservative (slightly over-) estimate of
    token count from character count. See _CHARS_PER_TOKEN_ESTIMATE for
    where the ratio comes from."""
    return int(len(text) / _CHARS_PER_TOKEN_ESTIMATE) + 1


# Tracks the most recent rate-limit headers Groq returned, so the NEXT
# call can proactively wait if the bucket looks too depleted, rather
# than firing blindly and reacting to a 429 after the fact. Module-level
# because TPM limits apply at the organization level (confirmed in
# Groq's docs) -- shared across every call this process makes, not
# scoped to a single request. Confirmed via Groq's docs that
# x-ratelimit-remaining-tokens / x-ratelimit-reset-tokens are "always
# included" on every response, not just 429s.
_last_remaining_tokens: int | None = None
_last_reset_tokens_seconds: float = 0.0
_last_response_monotonic: float = 0.0


def _parse_duration_to_seconds(duration: str) -> float:
    """Parses Groq's rate-limit reset duration format -- e.g. '7.66s' or
    '2m59.56s' -- into a float number of seconds."""
    duration = duration.strip()
    minutes = 0.0
    seconds_part = duration
    if "m" in duration:
        minutes_part, seconds_part = duration.split("m", 1)
        minutes = float(minutes_part)
    seconds_part = seconds_part.rstrip("s")
    seconds = float(seconds_part) if seconds_part else 0.0
    return minutes * 60 + seconds


def _update_rate_limit_state(response) -> None:
    """Reads Groq's rate-limit headers off any response (success or
    error -- they're always present) and updates the module-level
    tracking used to pre-emptively pace the NEXT call."""
    global _last_remaining_tokens, _last_reset_tokens_seconds, _last_response_monotonic

    remaining = response.headers.get("x-ratelimit-remaining-tokens")
    reset = response.headers.get("x-ratelimit-reset-tokens")

    if remaining is not None:
        try:
            _last_remaining_tokens = int(remaining)
        except ValueError:
            pass
    if reset is not None:
        try:
            _last_reset_tokens_seconds = _parse_duration_to_seconds(reset)
        except ValueError:
            pass

    _last_response_monotonic = time.monotonic()


def _maybe_wait_for_token_budget(this_request_size: int) -> None:
    """
    If the last response we saw indicated fewer remaining TPM tokens
    than THIS request would need, and that window hasn't reset yet,
    wait out the remainder of the window before sending -- rather than
    sending immediately and reacting to a 429 afterward. This is what
    fixes the slowdown from two back-to-back calls (e.g. Task Analysis
    immediately followed by Optimization) each individually fitting
    under the TPM cap alone, but colliding because the second call's
    reserved size pushed the SAME rolling 60-second window over budget.
    A no-op on the very first call of a run, since there's no prior
    header data yet.
    """
    if _last_remaining_tokens is None:
        return

    elapsed = time.monotonic() - _last_response_monotonic
    window_remaining = _last_reset_tokens_seconds - elapsed

    if window_remaining > 0 and _last_remaining_tokens < this_request_size:
        print(
            f"  [Groq] Pacing — waiting {window_remaining:.1f}s before the next call "
            f"(last known remaining budget {_last_remaining_tokens} tokens this minute, "
            f"this request needs ~{this_request_size})."
        )
        time.sleep(window_remaining)


def call_llm(
    system_prompt: str,
    user_prompt: str,
    expect_json: bool = True,
    json_schema: dict | None = None,
    max_completion_tokens: int = 7000,
    reasoning_effort: str = "low",
) -> dict | str:
    """
    Sends a single chat completion request to Groq.

    Automatically retries on 429 (rate limited) — Groq sends a
    'retry-after' header telling us exactly how many seconds to wait,
    which is respected directly rather than guessing a backoff. Falls
    back to exponential backoff only if that header is missing. Up to
    MAX_RATE_LIMIT_RETRIES attempts before giving up and raising.

    Before sending, this ALWAYS clamps max_completion_tokens down to
    whatever actually fits under ACCOUNT_TPM_LIMIT given the real size
    of THIS prompt (see _estimate_tokens) -- a single Groq request's
    reserved size is input tokens + max_completion_tokens, checked
    against the same per-minute bucket, and a request whose reserved
    size alone exceeds the bucket gets rejected outright with a 413
    REGARDLESS of recent usage. Passing a large max_completion_tokens
    is therefore not "safe but wasteful" the way it would be on most
    APIs -- on Groq's free tier it can make an otherwise-fine request
    impossible. Callers should pass their DESIRED ceiling; this
    function will never send more than what's actually available.

    Args:
        system_prompt: sets the model's role/behavior for this call.
        user_prompt: the actual content/question.
        expect_json: if True, parses the response as JSON and returns a
                     dict. Raises ValueError if the response isn't valid
                     JSON — the caller should catch this and fall back
                     gracefully rather than crash the whole pipeline.
        json_schema: optional dict like {"name": "...", "schema": {...}}.
                     When provided (and expect_json=True), switches from
                     the older "json_object" mode to Groq's Structured
                     Outputs in STRICT mode. Strict mode uses constrained
                     decoding on supported models (openai/gpt-oss-20b and
                     openai/gpt-oss-120b -- the two models this project
                     uses) so the model is physically unable to emit a
                     token sequence that violates the schema. This is
                     what eliminates the "400 Failed to validate JSON" /
                     json_validate_failed error that plain json_object
                     mode can throw on long, multi-constraint prompts.
                     If None, falls back to plain "json_object" mode
                     (valid JSON guaranteed, shape not guaranteed) --
                     fine for simpler payloads. The schema passed in
                     must follow Groq's strict-mode rules: every
                     property listed in "required", every object has
                     "additionalProperties": False.
        max_completion_tokens: the DESIRED ceiling on how many tokens the
                     model can generate, INCLUDING hidden reasoning
                     tokens (gpt-oss models spend real budget thinking
                     before they write any JSON). This is automatically
                     clamped down (never up) to whatever fits under
                     ACCOUNT_TPM_LIMIT for this specific prompt's actual
                     size -- see the clamping logic below. Pass the
                     largest value that would genuinely help if there
                     were no TPM ceiling; the clamp handles the rest.
        reasoning_effort: "low" | "medium" | "high", only supported by
                     openai/gpt-oss-20b and openai/gpt-oss-120b. Lower
                     effort means fewer hidden chain-of-thought tokens
                     spent before the actual JSON output begins, which
                     directly reduces truncation risk for a fixed token
                     budget. Defaults to "low" since both of this
                     project's agents are doing structured placement/
                     classification, not open-ended problem solving --
                     Groq's default ("medium") spends more budget on
                     reasoning than either task needs.

    Returns:
        Parsed JSON dict if expect_json=True, otherwise the raw text string.

    Raises:
        ValueError if this prompt's own estimated input size leaves less
        than MIN_VIABLE_COMPLETION_TOKENS of room under ACCOUNT_TPM_LIMIT
        -- in that case no amount of clamping can make the request
        viable, so it fails fast with an explanation rather than sending
        a request almost certain to truncate or be rejected.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GROQ_API_KEY is not set. Add it to your .env file at the project "
            "root and make sure main.py (or whatever entry point you're "
            "running) calls load_dotenv() before this is imported."
        )

    # Estimate this specific prompt's input size and clamp
    # max_completion_tokens to whatever room is actually left under the
    # account's TPM ceiling. The +20 covers small fixed overhead from
    # the chat message structure (role wrappers etc.) that isn't visible
    # in the raw prompt text. This is recalculated on every call rather
    # than hardcoded, so it stays correct as prompts grow (more tasks,
    # more recurring commitments) instead of silently breaking again.
    estimated_input_tokens = (
        _estimate_tokens(system_prompt) + _estimate_tokens(user_prompt) + 20
    )
    available_for_completion = (
        ACCOUNT_TPM_LIMIT - SAFETY_MARGIN_TOKENS - estimated_input_tokens
    )

    if available_for_completion < MIN_VIABLE_COMPLETION_TOKENS:
        raise ValueError(
            f"This prompt's estimated input size (~{estimated_input_tokens} tokens) "
            f"leaves only ~{available_for_completion} tokens of completion room under "
            f"this account's {ACCOUNT_TPM_LIMIT}-token-per-minute limit for {DEFAULT_MODEL} "
            f"-- too little to get a useful response. Shrink the prompt (fewer tasks/"
            "commitments per call) or upgrade your Groq tier at "
            "https://console.groq.com/settings/billing."
        )

    actual_max_completion_tokens = min(max_completion_tokens, available_for_completion)
    if actual_max_completion_tokens < max_completion_tokens:
        print(
            f"  [Groq] Capping max_completion_tokens to {actual_max_completion_tokens} "
            f"(requested {max_completion_tokens}, but only that much fits under the "
            f"{ACCOUNT_TPM_LIMIT}-token-per-minute limit alongside ~{estimated_input_tokens} "
            "estimated input tokens)."
        )

    # Proactively wait if the last call we made left this rolling 60s
    # window too depleted for THIS request -- see _maybe_wait_for_token_budget.
    # This is what prevents two back-to-back calls (e.g. Task Analysis
    # immediately followed by Optimization) from colliding on the same
    # TPM bucket even though each individually fits under the cap alone.
    _maybe_wait_for_token_budget(estimated_input_tokens + actual_max_completion_tokens)

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,  # low temperature: we want consistent classification, not creativity
        "max_completion_tokens": actual_max_completion_tokens,
    }
    # reasoning_effort is only valid for the gpt-oss reasoning models;
    # sending it to a llama/qwen model returns a 400. Include it only
    # when the active model actually supports it, so switching to
    # llama-3.3-70b-versatile (the recommended free-tier default) just
    # works with no other changes.
    if DEFAULT_MODEL in _REASONING_MODELS:
        payload["reasoning_effort"] = reasoning_effort
    structured_mode = "json_object"
    if expect_json:
        structured_mode = _resolve_structured_mode(DEFAULT_MODEL, has_schema=json_schema is not None)

        if structured_mode == "strict":
            # Strict Structured Outputs -- never produces invalid JSON or
            # a 400 schema-validation error, because the model is
            # constrained at the token level. Only the gpt-oss models
            # support this.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("name", "chrono_response"),
                    "strict": True,
                    "schema": json_schema["schema"],
                },
            }
        elif structured_mode == "besteffort":
            # json_schema with strict:false -- the model tries to match
            # the schema but isn't token-constrained, so it can still
            # occasionally 400 or return schema-invalid JSON (the retry
            # loop in the agents handles that). Used for models that
            # support json_schema but not strict mode (e.g. llama-4-scout).
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("name", "chrono_response"),
                    "strict": False,
                    "schema": json_schema["schema"],
                },
            }
        else:
            # Plain JSON Object mode -- valid JSON syntax but NO schema
            # enforcement. The floor that EVERY model supports, used when
            # no schema was passed OR the active model supports neither
            # json_schema variant (e.g. llama-3.3-70b-versatile). The
            # SYSTEM_PROMPTs in this project already describe the exact
            # JSON shape in words, so the model still has the shape to
            # follow; it just isn't enforced by the API, which is why the
            # agents validate the result and retry on mismatch.
            payload["response_format"] = {"type": "json_object"}

    last_error = None

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        response = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )

        # Always read rate-limit headers, success or error -- Groq's docs
        # confirm these are present on every response -- so the NEXT call
        # (possibly from a different agent moments later) has fresh data
        # to pace itself against.
        _update_rate_limit_state(response)

        if response.status_code == 429:
            last_error = requests.HTTPError(
                f"429 Too Many Requests for url: {GROQ_API_URL}", response=response
            )
            if attempt == MAX_RATE_LIMIT_RETRIES:
                break  # out of retries, raise below

            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    # Trust the server's own number completely -- Groq tells
                    # us exactly how long to wait (sometimes several minutes
                    # for a TPM/TPD exhaustion), and clamping that to our
                    # own short MAX_BACKOFF_SECONDS was the actual bug:
                    # it caused us to retry too early, hit a second 429,
                    # and burn through all retries before the real wait
                    # time had even elapsed.
                    wait_seconds = float(retry_after)
                except ValueError:
                    wait_seconds = min(2 ** attempt, MAX_BACKOFF_SECONDS)
            else:
                # No header at all -- this is OUR guess, so the short cap
                # makes sense here.
                wait_seconds = min(2 ** attempt, MAX_BACKOFF_SECONDS)

            print(f"  [Groq] Rate limited — waiting {wait_seconds:.1f}s before retry {attempt + 1}/{MAX_RATE_LIMIT_RETRIES}...")
            time.sleep(wait_seconds)
            continue

        # Transient server-side errors (502/503/504/520/522) come from
        # Groq's servers or Cloudflare in front of them, not from anything
        # wrong with our request -- the body is usually a Cloudflare HTML
        # error page, not JSON. These are typically momentary, so retry a
        # few times with short backoff, and if they persist, raise a clean
        # one-line message instead of dumping the whole HTML page into the
        # caller's logs (which is what happened on the 522 outage).
        if response.status_code in (500, 502, 503, 504, 520, 521, 522, 524):
            last_error = requests.HTTPError(
                f"{response.status_code} server error from Groq (transient — Groq/Cloudflare "
                "side, not your request). Groq's API may be briefly unavailable; "
                "try again in a few minutes.",
                response=response,
            )
            if attempt == MAX_RATE_LIMIT_RETRIES:
                break
            wait_seconds = min(2 ** attempt, MAX_BACKOFF_SECONDS)
            print(
                f"  [Groq] Transient {response.status_code} server error — retrying in "
                f"{wait_seconds:.1f}s ({attempt + 1}/{MAX_RATE_LIMIT_RETRIES})..."
            )
            time.sleep(wait_seconds)
            continue

        if response.status_code >= 400:
            try:
                error_body = response.json()
                error_detail = error_body.get("error", {}).get("message", response.text)
                # Surface failed_generation when Groq includes it -- this
                # is the model's raw (invalid) output, and is the single
                # most useful piece of information for debugging a JSON
                # validation failure. The community has reported this
                # field sometimes comes back empty on Groq's end, in
                # which case we say so explicitly rather than just
                # omitting it silently.
                failed_generation = error_body.get("error", {}).get("failed_generation")
                if failed_generation:
                    error_detail += f"\n  failed_generation: {failed_generation!r}"
                elif "failed_generation" in error_body.get("error", {}):
                    error_detail += "\n  failed_generation: (empty — Groq did not return the raw output)"
            except (ValueError, AttributeError):
                error_detail = response.text
            raise requests.HTTPError(
                f"{response.status_code} error from Groq: {error_detail}", response=response
            )

        response_json = response.json()
        choice = response_json["choices"][0]
        content = choice["message"]["content"]

        # A 200 response can still be a truncated generation if the model
        # hit max_completion_tokens before finishing -- Groq reports this
        # via finish_reason="length" rather than a 4xx status. This is
        # distinguished explicitly because it has a different fix (raise
        # max_completion_tokens or lower reasoning_effort) than a genuine
        # malformed-output error, and a clear message here saves a lot of
        # guessing if it happens again.
        if choice.get("finish_reason") == "length":
            raise ValueError(
                "Groq cut off the response because it hit max_completion_tokens "
                f"({actual_max_completion_tokens}, after TPM-based clamping) before "
                "finishing -- this is a truncation, not a malformed-JSON issue. The "
                f"prompt's estimated input size (~{estimated_input_tokens} tokens) is "
                f"leaving little room under the {ACCOUNT_TPM_LIMIT}-token-per-minute "
                "limit; shrink the prompt (fewer tasks/commitments per call), lower "
                "reasoning_effort further, or upgrade your Groq tier. "
                f"Partial content received: {content!r}"
            )

        if expect_json:
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                raise ValueError(f"Groq did not return valid JSON: {content!r}") from e

        return content

    raise last_error


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