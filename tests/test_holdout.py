"""Held-out tests: offline feasibility of every locked seed, and the spend controls (no key, no network, $0)."""
import pytest

from autoheal import holdout
from autoheal.db import connect, scalar
from autoheal.detector import run_checks
from autoheal.llm import LLMError
from autoheal.scenarios import SCENARIOS, ground_truth_healthy
from autoheal.tools import ToolContext, execute
from autoheal.verifier import verify


def test_holdouts_are_separate_from_the_benchmark():
    assert not set(holdout.HOLDOUTS) & set(SCENARIOS)
    assert holdout.HOLDOUT_SEEDS == (101, 202, 303)


@pytest.mark.parametrize("name, seed", [(n, s) for n, h in holdout.HOLDOUTS.items() for s in h.seeds])
def test_every_seed_has_intended_faults_and_is_solvable(name, seed):
    conn = connect(":memory:")
    shop = holdout.build(name, seed, conn)
    assert {f.check for f in run_checks(conn)} == holdout.HOLDOUTS[name].expected_symptoms
    holdout.known_good_fix(name, ToolContext(conn, shop))
    assert verify(conn, shop).passed and ground_truth_healthy(conn)["healthy"]
    assert shop.cfg("queue.max_depth") == "200"


@pytest.mark.parametrize("seed", holdout.HOLDOUT_SEEDS)
def test_h1_naive_recovery_creates_a_downstream_cascade(seed):
    conn = connect(":memory:")
    shop = holdout.build("H1", seed, conn)
    ctx = ToolContext(conn, shop)
    execute(ctx, "rollback_config", {"key": "payment.gateway_timeout_ms"})
    assert execute(ctx, "retry_stuck_payments", {})["dead_lettered"] > 0
    assert "dead_letter_backlog" in {f.check for f in run_checks(conn)}


class _FakeLLM:
    """Charges a fixed amount per call to the real ledger, then escalates (or raises)."""
    def __init__(self, ledger, cost, error=False):
        self.ledger, self.cost, self.error = ledger, cost, error

    def chat(self, messages, tools, model, purpose="", incident_id=0):
        if self.error:
            raise LLMError("OpenRouter HTTP 402: test")
        self.ledger.record(model=model, incident_id=incident_id, cost_usd=self.cost)
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {
            "name": "escalate_to_human", "arguments": '{"reason": "test", "summary": "test"}'}}]}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHEAL_DB", str(tmp_path / "autoheal.db"))
    monkeypatch.setattr(holdout, "OUT_DIR", tmp_path / "eval")
    return monkeypatch


def test_budget_gate_stops_before_the_ceiling(isolated):
    real = holdout.build_llm
    isolated.setattr(holdout, "build_llm", lambda kind, conn, s, ledger:
                     real(kind, conn, s, ledger) if kind == "playbook" else _FakeLLM(ledger, 0.20))
    rep = holdout.run(["playbook", "openrouter"], 0.70, progress=lambda line: None)
    paid = [r for r in rep["results"] if r["agent"] == "openrouter"]
    assert rep["stopped"].startswith("budget gate") and rep["additional_spend_usd"] <= 0.70
    assert len(paid) == 2 and len([r for r in rep["results"] if r["agent"] == "playbook"]) == 6


def test_infrastructure_failure_stops_further_paid_runs(isolated):
    isolated.setattr(holdout, "build_llm", lambda kind, conn, s, ledger: _FakeLLM(ledger, 0, error=True))
    rep = holdout.run(["openrouter"], 0.70, progress=lambda line: None)
    assert len(rep["results"]) == 1 and rep["results"][0]["escalation_cause"] == "infrastructure"
    assert rep["stopped"].startswith("infrastructure failure")


class _StallThenRejectedSwitch:
    """Cheap model rolls back, then only investigates; the strong model's first request is rejected (HTTP 400)."""
    def __init__(self, ledger, strong):
        self.ledger, self.strong, self.calls = ledger, strong, 0

    def chat(self, messages, tools, model, purpose="", incident_id=0):
        if model == self.strong:
            raise LLMError("OpenRouter HTTP 400: invalid conversation for this model")
        self.ledger.record(model=model, incident_id=incident_id, cost_usd=0.01)
        self.calls += 1
        name, args = (("rollback_config", '{"key": "payment.gateway_timeout_ms"}') if self.calls == 1
                      else ("get_health_report", "{}"))
        return {"role": "assistant", "content": None, "reasoning_details": [{"type": "reasoning.encrypted"}],
                "tool_calls": [{"id": f"t{self.calls}", "type": "function",
                                "function": {"name": name, "arguments": args}}]}


def test_phase1_selection_records_routing_requests_and_a_rejected_switch(isolated):
    isolated.setattr(holdout, "build_llm", lambda kind, conn, s, ledger: _StallThenRejectedSwitch(ledger, s.strong_model))
    rep = holdout.run(["openrouter"], 0.30, progress=lambda line: None, holdouts=["H1"], seeds=[202],
                      prefix="phase1", label="v3 Phase 1 live validation")
    assert rep["tag"].startswith("phase1-") and rep["label"] == "v3 Phase 1 live validation" and rep["seeds"] == {"H1": [202]}
    (row,) = rep["results"]
    assert (row["holdout"], row["seed"]) == ("H1", 202)
    assert row["routing"][-1]["reason"].startswith("no measurable progress") and row["routing"][-1]["step"] == 3
    assert [r["model"] for r in row["requests"]] == [row["routing"][0]["model"]] * 3  # cheap-model requests only
    assert row["escalation_cause"] == "infrastructure" and "HTTP 400" in row["escalation_reason"]
    assert rep["stopped"].startswith("infrastructure failure") and rep["additional_spend_usd"] == pytest.approx(0.03)
    assert [t["kind"] for t in row["trace"]].count("focus") == 1


def test_hard_ceiling_is_the_tightened_global_cap(isolated):
    seen = {}
    isolated.setattr(holdout, "build_llm", lambda kind, conn, s, ledger: seen.setdefault("s", s) and _FakeLLM(ledger, 0))
    holdout.run(["openrouter"], 0.70, progress=lambda line: None)
    assert seen["s"].max_total_usd == pytest.approx(0.70 - holdout.MARGIN)  # fresh ledger starts at $0


# ------------------------------------------------------------ H4 (pre-registered for v3.1)
H4 = {"H4-45": (45, 404), "H4-60": (60, 505), "H4-75": (75, 606)}


def test_h4_matrix_is_exactly_three_cases_and_h1_h3_are_unchanged():
    assert {n: (h.manifest_orders, h.seeds) for n, h in holdout.HOLDOUTS.items() if n.startswith("H4")} ==         {n: (load, (seed,)) for n, (load, seed) in H4.items()}
    assert holdout.HOLDOUTS["H1"].seeds == holdout.HOLDOUTS["H3"].seeds == (101, 202, 303)
    assert holdout.HOLDOUTS["H1"].manifest_orders == 60 and holdout.HOLDOUTS["H3"].manifest_orders is None
    assert holdout.DEFAULT_HOLDOUTS == ("H1", "H3")  # H4 never runs unless selected explicitly
    assert all(holdout.HOLDOUTS[n].expected_categories == {"queue"} for n in H4)


@pytest.mark.parametrize("name", list(H4))
def test_h4_is_recoverable_by_a_scripted_repair_through_the_real_agent_and_guard(name, monkeypatch):
    """Feasibility only: the scripted turns live in this test, never in the agent's prompt or logic."""
    import json
    from autoheal.agent import AutoHealAgent
    from autoheal.config import get_settings
    from autoheal.llm import UsageLedger

    def turn(*calls):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)]}

    finish = turn(("finish_incident", {"root_causes": [{"category": "queue", "cause": "max_depth cut to 10",
                                                         "evidence": ["r143"], "fix_applied": "rollback+replay"}],
                                       "summary": "s", "confidence": 0.9}))
    script = [turn(("rollback_config", {"key": "queue.max_depth"}),
                   ("set_config", {"key": "queue.worker_batch", "value": "100"})),
              turn(("replay_dead_letter", {}), ("wait_and_observe", {"ticks": 2})),
              turn(("replay_dead_letter", {}), ("wait_and_observe", {"ticks": 2})), finish]

    class Scripted:
        i = 0

        def chat(self, *a, **k):
            msg = script[min(self.i, len(script) - 1)]
            self.i += 1
            return json.loads(json.dumps(msg))

    s = get_settings(db_path=":memory:")  # real limits: 16 steps, 8 mutations, 12 wait ticks, medium risk
    conn = connect(":memory:")
    shop = holdout.build(name, H4[name][1], conn)
    records = []
    with holdout._observe_verification(records):
        res = AutoHealAgent(conn, shop, Scripted(), s, UsageLedger(":memory:")).run()
    trace = holdout._trace(conn, res.incident_id)
    assert res.status == "resolved" and res.steps <= s.max_steps and res.mutations <= s.max_mutations
    assert not [t for t in trace if t["kind"] == "blocked"]
    assert holdout.criteria(name, res, conn)["success"]
    (v,) = records
    assert v["passed"] and v["before_finish"]["dead_letters"] == 0 and v["verifier_ticks"] >= 2
    m = holdout._queue_metrics(conn, {"dead_letters": 1, "dl_max_id": 0}, trace)
    assert m["agent_wait_ticks"] == 4 <= s.max_wait_ticks and m["outstanding_dead_letters_at_end"] == 0


@pytest.mark.parametrize("name", list(H4))
def test_h4_single_replay_cannot_clear_the_backlog_at_higher_loads(name):
    conn = connect(":memory:")
    shop = holdout.build(name, H4[name][1], conn)
    ctx = ToolContext(conn, shop)
    initial = scalar(conn, "SELECT COUNT(*) FROM dead_letter WHERE replayed=0")
    execute(ctx, "rollback_config", {"key": "queue.max_depth"})
    r = execute(ctx, "replay_dead_letter", {})
    assert initial == {"H4-45": 175, "H4-60": 250, "H4-75": 325}[name]
    assert (r["rejected_queue_full"] > 0) == (name != "H4-45")  # 45 fits in one replay; 60/75 need draining


def test_selecting_h4_runs_exactly_three_evaluations_and_records_h4_metrics(isolated):
    isolated.setattr(holdout, "build_llm", lambda kind, conn, s, ledger: _FakeLLM(ledger, 0.01))
    rep = holdout.run(["openrouter"], 0.60, progress=lambda line: None, holdouts=list(H4), prefix="h4")
    assert [(r["holdout"], r["seed"]) for r in rep["results"]] == [(n, seed) for n, (_, seed) in H4.items()]
    assert rep["seeds"] == {n: [seed] for n, (_, seed) in H4.items()}
    row = rep["results"][0]
    for key in ("initial_dead_letters", "dead_letters_created_during_recovery", "replayed", "replay_rejections",
                "outstanding_dead_letters_at_end", "agent_wait_ticks", "blocked_actions", "focus_reminders",
                "model_upgrades", "verification_detail", "criteria", "requests", "load_orders_per_tick"):
        assert key in row
    assert row["initial_dead_letters"] == 175 and row["load_orders_per_tick"] == 45
