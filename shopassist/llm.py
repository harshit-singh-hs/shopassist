"""Gemini wrapper: quota-aware model routing, retries with exponential backoff + jitter, token accounting.

Free-tier quotas are per model and come in two kinds: per minute (recovers in seconds) and per day
(e.g. 20 requests/day/model, recovers at the daily reset). So:
- every model has its own client-side rate budget (MAX_RPM per minute);
- a 429 benches that model for the server's own `retryDelay` (seconds for RPM, hours for RPD), so an
  exhausted model is skipped instead of being hammered every minute;
- calls go to the first model in the chain that is available; a request waits at most LLM_MAX_WAIT_S
  for capacity, then fails fast so the agent can answer gracefully instead of hanging.
The rest of the code only depends on `generate(contents, config)`; swapping providers = rewrite this module.
"""
import json
import logging
import random
import re
import threading
import time
from collections import defaultdict, deque

import httpx
from google import genai
from google.genai import errors, types

from .config import settings

log = logging.getLogger("shopassist.llm")
RETRYABLE = {500, 502, 503, 504}
DEFAULT_COOLDOWN_S = 60
_client: genai.Client | None = None
_lock = threading.Lock()
_windows: dict[str, deque] = defaultdict(deque)   # model -> timestamps of recent calls
_benched_until: dict[str, float] = {}              # model -> monotonic time it becomes available again
_bench_reason: dict[str, str] = {}
_local = threading.local()                         # per-thread record of the last call, for tracing
_max_wait_s = settings.llm_max_wait_s


class LLMUnavailable(RuntimeError):
    pass


def client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise LLMUnavailable("GEMINI_API_KEY is not set (free key: https://aistudio.google.com/apikey)")
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def set_max_wait(seconds: float):
    """Interactive chat should fail fast; batch jobs (evals) can afford to wait out per-minute limits."""
    global _max_wait_s
    _max_wait_s = seconds


def chain() -> list[str]:
    return [settings.llm_model, *settings.fallback_models]


def quota_status() -> dict[str, str]:
    now = time.monotonic()
    return {m: (f"benched {int(_benched_until[m] - now)}s ({_bench_reason.get(m, '')})"
                if _benched_until.get(m, 0) > now else "available") for m in chain()}


def _parse_429(e: errors.APIError) -> tuple[float, str]:
    """Read the server's retryDelay and which quota was hit (per-minute vs per-day)."""
    raw = json.dumps(getattr(e, "details", None) or {}) + str(e)
    delay = re.search(r'retryDelay"?\W+(\d+(?:\.\d+)?)s', raw)
    quota = re.search(r'quotaId"?\W+([A-Za-z-]+)', raw)
    kind = "per-day quota" if quota and "PerDay" in quota.group(1) else "per-minute quota"
    return (float(delay.group(1)) if delay else DEFAULT_COOLDOWN_S), kind


def _acquire(candidates: list[str]) -> str:
    """Reserve a request slot on the first candidate with capacity; wait at most _max_wait_s."""
    deadline = time.monotonic() + _max_wait_s
    with _lock:
        while True:
            now = time.monotonic()
            for name in candidates:
                window = _windows[name]
                while window and now - window[0] > 60:
                    window.popleft()
                if len(window) < settings.max_rpm and _benched_until.get(name, 0) <= now:
                    window.append(now)
                    return name
            ready_at = min(max(_benched_until.get(m, 0), (_windows[m][0] + 60) if len(_windows[m]) >= settings.max_rpm else 0)
                           for m in candidates)
            if ready_at > deadline:
                raise LLMUnavailable(f"All models rate-limited; next one free in {int(ready_at - now)}s. {quota_status()}")
            time.sleep(max(0.05, ready_at - now))


def base_config(**kwargs) -> types.GenerateContentConfig:
    if settings.thinking_level:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=settings.thinking_level)
    if settings.temperature is not None:
        kwargs["temperature"] = settings.temperature
    return types.GenerateContentConfig(**kwargs)


def _for_model(name: str, config):
    """Gemini 2.x doesn't accept thinking_level, so the config is adapted per model family."""
    if config and config.thinking_config and not name.startswith("gemini-3"):
        return config.model_copy(update={"thinking_config": None})
    return config


def generate(contents, config: types.GenerateContentConfig | None = None, model: str | None = None,
             retries: int = 2) -> types.GenerateContentResponse:
    """Transient 5xx -> retry the same model with backoff. 429 -> bench the model for retryDelay, move on.
    Other 4xx (our bug, e.g. a bad request) -> fail fast; another model won't fix it."""
    remaining, last_error = ([model] if model else chain()), None
    _local.retries = 0
    while remaining:
        name = _acquire(remaining)
        for attempt in range(retries + 1):
            try:
                resp = client().models.generate_content(model=name, contents=contents, config=_for_model(name, config))
                _local.model = name
                return resp
            except errors.APIError as e:
                last_error = e
                if e.code == 429:
                    delay, kind = _parse_429(e)
                    _benched_until[name] = time.monotonic() + delay
                    _bench_reason[name] = kind
                    log.warning("%s hit its %s; benched for %ss", name, kind, int(delay))
                    break
                if e.code in RETRYABLE and attempt < retries:
                    _local.retries += 1
                    time.sleep(min(20, 2 ** (attempt + 1)) + random.random())
                    continue
                break
            except (httpx.TransportError, OSError) as e:
                # DNS failure, dropped connection, timeout: the network is down, not the model, so switching
                # models won't help. Retry briefly, then fail gracefully instead of crashing the conversation.
                if attempt < retries:
                    _local.retries += 1
                    time.sleep(min(10, 2 ** (attempt + 1)) + random.random())
                    continue
                raise LLMUnavailable(f"Network error reaching Gemini: {type(e).__name__}: {str(e)[:150]}") from e
        remaining.remove(name)
        if last_error is not None and last_error.code not in (404, 429, *RETRYABLE):
            break
    raise LLMUnavailable(f"Gemini API error {getattr(last_error, 'code', '?')}: {str(last_error)[:200]}")


def last_call_info() -> dict:
    return {"model": getattr(_local, "model", None), "retries": getattr(_local, "retries", 0)}


def usage(resp: types.GenerateContentResponse) -> dict:
    meta = resp.usage_metadata
    return {
        "input_tokens": getattr(meta, "prompt_token_count", 0) or 0,
        "output_tokens": (getattr(meta, "candidates_token_count", 0) or 0) + (getattr(meta, "thoughts_token_count", 0) or 0),
    }
