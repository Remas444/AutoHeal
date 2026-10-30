"""OpenRouter client tests with a mocked HTTP layer (no network, $0)."""
import pytest

from autoheal import llm as llm_mod
from autoheal.config import get_settings
from autoheal.llm import BudgetExceeded, LLMError, OpenRouterClient, UsageLedger


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, str(payload)

    def json(self):
        return self._p


OK = {"model": "m", "choices": [{"message": {"role": "assistant", "content": "hi"}}],
      "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.0123}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    s = get_settings(openrouter_api_key="sk-test", max_total_usd=0.03, max_incident_usd=0.02, db_path=":memory:")
    return OpenRouterClient(s, UsageLedger(":memory:"))


def test_records_real_cost(client, monkeypatch):
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: FakeResp(200, OK))
    msg = client.chat([{"role": "user", "content": "x"}], [], model="m", incident_id=1)
    assert msg["content"] == "hi"
    assert client.ledger.total() == pytest.approx(0.0123)


def test_per_incident_cap(client, monkeypatch):
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: FakeResp(200, OK))
    client.chat([], [], model="m", incident_id=1)
    client.chat([], [], model="m", incident_id=1)  # 0.0246 >= 0.02 after this
    with pytest.raises(BudgetExceeded):
        client.chat([], [], model="m", incident_id=1)


def test_global_cap(client, monkeypatch):
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: FakeResp(200, OK))
    for i in range(3):
        client.chat([], [], model="m", incident_id=10 + i)
    with pytest.raises(BudgetExceeded):
        client.chat([], [], model="m", incident_id=99)


def test_retries_on_429_then_succeeds(client, monkeypatch):
    seq = iter([FakeResp(429, {"error": "rate"}), FakeResp(200, OK)])
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: next(seq))
    assert client.chat([], [], model="m", incident_id=1)["content"] == "hi"


def test_estimates_cost_when_missing(client, monkeypatch):
    payload = {**OK, "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: FakeResp(200, payload))
    client.prices = {"m": {"prompt": 0.5, "completion": 2.0}}
    client.s.max_total_usd = client.s.max_incident_usd = 10
    client.chat([], [], model="m", incident_id=1)
    assert client.ledger.total() == pytest.approx(0.5)


def test_bad_request_raises(client, monkeypatch):
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: FakeResp(400, {"error": "bad model"}))
    with pytest.raises(LLMError):
        client.chat([], [], model="m", incident_id=1)


def test_missing_key_is_clear():
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        OpenRouterClient(get_settings(openrouter_api_key=""), UsageLedger(":memory:"))


def test_incident_cost_isolated_across_runs(tmp_path):
    # Every demo resets the system DB, so each run's incident is #1; costs must not leak between runs.
    from autoheal.runtime import new_run_tag
    path = str(tmp_path / "usage.db")
    first = UsageLedger(path, run_tag=new_run_tag("cli"))
    first.record(model="m", incident_id=1, cost_usd=0.25)
    second = UsageLedger(path, run_tag=new_run_tag("cli"))
    assert second.run_tag != first.run_tag
    assert second.incident_total(1) == 0
    assert second.total() == pytest.approx(0.25)


def test_settings_never_expose_the_api_key():
    s = get_settings(openrouter_api_key="sk-or-v1-secretvalue")
    for out in (repr(s), str(s), str(s.model_dump()), s.model_dump_json()):
        assert "secretvalue" not in out and "openrouter_api_key" not in out
    assert s.openrouter_api_key == "sk-or-v1-secretvalue"  # still usable by the client
    assert s.model_copy(update={"dry_run": True}).openrouter_api_key == "sk-or-v1-secretvalue"  # survives overrides


def test_client_still_sends_the_key(monkeypatch):
    sent = {}

    def post(url, headers, json, timeout):
        sent.update(headers)
        return FakeResp(200, OK)

    monkeypatch.setattr(llm_mod.requests, "post", post)
    s = get_settings(openrouter_api_key="sk-or-v1-secretvalue", db_path=":memory:")
    OpenRouterClient(s, UsageLedger(":memory:")).chat([], [], model="m", incident_id=1)
    assert sent["Authorization"] == "Bearer sk-or-v1-secretvalue"
