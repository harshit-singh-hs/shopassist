"""Quota-aware routing, tested with a fake Gemini client (no network)."""
import time

import httpx
import pytest
from google.genai import errors

from shopassist import llm


def quota_error(per_day: bool, delay_s: int) -> errors.APIError:
    quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    return errors.APIError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{delay_s}s"}]}})


class FakeModels:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        outcome = self.behaviour[model]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake(monkeypatch):
    def install(behaviour):
        models = FakeModels(behaviour)
        monkeypatch.setattr(llm, "client", lambda: type("C", (), {"models": models})())
        monkeypatch.setattr(llm, "chain", lambda: list(behaviour))
        monkeypatch.setattr(llm, "_benched_until", {})
        monkeypatch.setattr(llm, "_windows", llm.defaultdict(llm.deque))
        return models
    return install


def test_daily_quota_benches_model_until_reset_and_fails_over(fake):
    models = fake({"primary": quota_error(per_day=True, delay_s=42000), "backup": "ok"})
    assert llm.generate("hi") == "ok" and llm.last_call_info()["model"] == "backup"
    assert llm._benched_until["primary"] - time.monotonic() > 41000  # benched for hours, not 60 s
    llm.generate("again")
    assert models.calls == ["primary", "backup", "backup"]          # exhausted model is not retried


def test_all_models_exhausted_fails_fast(fake):
    fake({"a": quota_error(per_day=True, delay_s=40000), "b": quota_error(per_day=True, delay_s=40000)})
    start = time.monotonic()
    with pytest.raises(llm.LLMUnavailable):
        llm.generate("hi")
    with pytest.raises(llm.LLMUnavailable):  # second call: everything benched -> immediate failure
        llm.generate("hi")
    assert time.monotonic() - start < 2


def test_bad_request_is_not_retried_on_other_models(fake):
    models = fake({"a": errors.APIError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}),
                   "b": "ok"})
    with pytest.raises(llm.LLMUnavailable):
        llm.generate("hi")
    assert models.calls == ["a"]


def test_network_failure_degrades_gracefully_instead_of_crashing(fake, monkeypatch):
    # Seen in practice: Wi-Fi dropped during an eval run and httpx.ConnectError crashed the whole agent.
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    models = fake({"a": httpx.ConnectError("getaddrinfo failed"), "b": "ok"})
    with pytest.raises(llm.LLMUnavailable, match="Network error"):
        llm.generate("hi")
    assert models.calls == ["a", "a", "a"]  # retried, and didn't pointlessly try other models


def test_parse_429_distinguishes_minute_and_day():
    assert llm._parse_429(quota_error(per_day=True, delay_s=500)) == (500.0, "per-day quota")
    assert llm._parse_429(quota_error(per_day=False, delay_s=30)) == (30.0, "per-minute quota")
