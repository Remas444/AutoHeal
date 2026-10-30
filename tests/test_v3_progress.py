"""v3: progress-aware recovery control - loop guard, focus, bounded waiting (offline, no key, $0)."""
import json

import pytest

from autoheal.agent import COMPLETION_NUDGE, MAX_FOCUS_REMINDERS, AutoHealAgent
from autoheal.config import get_settings
from autoheal.db import connect, scalar
from autoheal.detector import made_progress
from autoheal.holdout import build
from autoheal.llm import UsageLedger
from autoheal.scenarios import ground_truth_healthy, setup_scenario

UNPAID = "SELECT COUNT(*) FROM orders WHERE status='created'"


@pytest.fixture
def settings():
    return get_settings(db_path=":memory:", max_total_usd=1.0, max_incident_usd=0.1)


@pytest.fixture
def ledger():
    return UsageLedger(":memory:")


def turn(*calls, reasoning=False):
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                          for i, (n, a) in enumerate(calls)]}
    if reasoning:
        msg["reasoning_details"] = [{"type": "reasoning.encrypted", "data": "opaque"}]
    return msg


LOOK = turn(("get_health_report", {}))
FINISH_PAYMENT = turn(("finish_incident", {"root_causes": [{"category": "payment", "cause": "timeout 500ms",
                                                             "evidence": ["r142"], "fix_applied": "rollback+retry"}],
                                           "summary": "fixed", "confidence": 0.9}))


class Scripted:
    """Replays assistant turns (repeating the last); records the model and whether reasoning_details were sent."""
    def __init__(self, script):
        self.script, self.i, self.models, self.sent_reasoning, self.messages = script, 0, [], [], []

    def chat(self, messages, tools, model, purpose="", incident_id=0):
        self.models.append(model)
        self.sent_reasoning.append(any("reasoning_details" in m for m in messages))
        self.messages = messages
        msg = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return json.loads(json.dumps(msg))


def steps_of(conn, kind):
    return [r["step"] for r in conn.execute("SELECT step FROM agent_steps WHERE kind=? ORDER BY id", (kind,))]


def run_h1(settings, ledger, script, seed=101):
    conn = connect(":memory:")
    shop = build("H1", seed, conn)
    llm = Scripted(script)
    agent = AutoHealAgent(conn, shop, llm, settings, ledger)
    return agent, agent.run(), conn, llm


# ------------------------------------------------------------ loop guard
def test_three_identical_successful_batches_complete_safely(settings, ledger):
    batch = (("retry_stuck_payments", {"max_orders": 100}), ("wait_and_observe", {"ticks": 1}))
    agent, res, conn, _ = run_h1(settings, ledger, [
        turn(("rollback_config", {"key": "payment.gateway_timeout_ms"}),
             ("set_config", {"key": "queue.worker_batch", "value": "100"})),
        turn(*batch), turn(*batch), turn(*batch), FINISH_PAYMENT])
    assert steps_of(conn, "blocked") == []
    assert len([s for s in steps_of(conn, "action")]) == 5  # rollback, set_config, 3 identical retries
    assert res.status == "resolved" and res.verifications[-1].passed and ground_truth_healthy(conn)["healthy"]
    assert res.mutations == 5 <= settings.max_mutations


def test_identical_action_without_progress_is_blocked(settings, ledger):
    retry = turn(("retry_stuck_payments", {"max_orders": 100}))  # timeout not fixed: every retry times out
    conn = connect(":memory:")
    shop = setup_scenario(conn, "payment_timeout")
    res = AutoHealAgent(conn, shop, Scripted([retry, retry, retry, LOOK]), settings, ledger).run()
    assert steps_of(conn, "action") == [1, 2] and steps_of(conn, "blocked") == [3]
    assert res.status == "escalated"


def test_progress_then_stall_blocks_the_next_repeat(settings, ledger):
    from autoheal.safety import SafetyGuard
    g = SafetyGuard(settings, connect(":memory:"), 1)
    a = {"max_orders": 100}
    for progressed in (True, True, False):
        assert g.check("retry_stuck_payments", a).allowed
        g.record("retry_stuck_payments", a, executed=True, progressed=progressed)
    assert not g.check("retry_stuck_payments", a).allowed
    assert g.mutations == 3  # the global mutation budget still counts every allowed mutation


@pytest.mark.parametrize("before, after, expected", [
    ({"unpaid_orders": 300, "dead_letters": 0, "queue_pending": 0}, {"unpaid_orders": 200, "dead_letters": 0, "queue_pending": 100}, True),
    ({"unpaid_orders": 300, "dead_letters": 0, "queue_pending": 200}, {"unpaid_orders": 200, "dead_letters": 100, "queue_pending": 200}, False),
    ({"unpaid_orders": 0, "dead_letters": 60, "queue_pending": 200}, {"unpaid_orders": 0, "dead_letters": 60, "queue_pending": 100}, True),
    ({"unpaid_orders": 0, "dead_letters": 0, "queue_pending": 200}, {"unpaid_orders": 0, "dead_letters": 0, "queue_pending": 100}, False),
    ({"unpaid_orders": 10, "dead_letters": 0, "queue_pending": 50}, {"unpaid_orders": 10, "dead_letters": 1, "queue_pending": 10}, False),
])
def test_made_progress_rules(before, after, expected):
    # reduction; cascade moves work between backlogs; drain while blocked work remains; drain alone; new damage
    assert made_progress(before, after) is expected


# ------------------------------------------------------------ progress classification
@pytest.mark.parametrize("scenario, call, dry_run", [
    ("payment_timeout", ("purge_dead_letter", {}), False),                              # blocked (approval)
    ("workflow_hang", ("terminate_workflow", {"workflow_id": 999, "reason": "x"}), False),  # failed
    ("queue_capacity", ("replay_dead_letter", {}), True),                               # dry-run: not executed
])
def test_failed_blocked_and_dry_run_calls_are_never_progress(ledger, scenario, call, dry_run):
    s = get_settings(db_path=":memory:", max_total_usd=1.0, max_incident_usd=0.1, dry_run=dry_run)
    conn = connect(":memory:")
    shop = setup_scenario(conn, scenario)
    agent = AutoHealAgent(conn, shop, Scripted([turn(call), turn(call), turn(call), LOOK]), s, ledger)
    agent.run()
    assert agent._last_progress_action == ""
    assert steps_of(conn, "nudge") == []
    if call[0] != "purge_dead_letter":  # repeated non-progress attempts are blocked on the third try
        assert steps_of(conn, "blocked")[:1] == [3]


# ------------------------------------------------------------ focus after recovery has started
def test_off_task_investigation_after_recovery_started_is_checked(settings, ledger):
    agent, res, conn, llm = run_h1(settings, ledger, [turn(("rollback_config", {"key": "payment.gateway_timeout_ms"}),
                                                           reasoning=True), turn(("get_health_report", {}), reasoning=True)])
    focus = steps_of(conn, "focus")
    assert focus == [3, 6] and len(focus) == MAX_FOCUS_REMINDERS  # bounded, once per stall episode
    note = conn.execute("SELECT result_json FROM agent_steps WHERE kind='focus' ORDER BY id").fetchone()[0]
    assert "unpaid_orders" in note and "13 of 16 steps remain" in note
    assert res.routing[-1]["reason"].startswith("no measurable progress") and llm.models[3] == settings.strong_model
    assert not llm.sent_reasoning[3]  # Gemini-specific reasoning_details are not sent to the new model
    assert res.status == "escalated" and res.escalation_cause == "guardrail"  # never auto-closed


def test_no_focus_reminder_before_any_recovery_action(settings, ledger):
    conn = connect(":memory:")
    shop = setup_scenario(conn, "payment_timeout")
    llm = Scripted([LOOK])
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert steps_of(conn, "focus") == []  # the post-change focus reminder needs a first change
    # v3.2: a never-acting investigation now gets the bounded pre-change decision point instead (was: cheap model
    # for the whole incident, which let U1-702 / U5-1001 run out of steps).
    assert steps_of(conn, "decision") == [6] and set(llm.models[:6]) == {settings.cheap_model}
    assert res.escalation_cause == "guardrail"


def test_measurable_progress_resets_the_stall_count(settings, ledger):
    batch = turn(("retry_stuck_payments", {"max_orders": 100}), ("wait_and_observe", {"ticks": 1}))
    _, _, conn, _ = run_h1(settings, ledger, [
        turn(("rollback_config", {"key": "payment.gateway_timeout_ms"}),
             ("set_config", {"key": "queue.worker_batch", "value": "100"}), ("retry_stuck_payments", {"max_orders": 100})),
        LOOK, LOOK, batch, LOOK, LOOK, LOOK])
    assert steps_of(conn, "focus")[0] == 7  # steps 2-3 and 5-6 stalled, but progress at step 4 reset the count


def test_repaired_but_unfinished_gets_completion_nudge_then_bounded_focus(settings, ledger):
    conn = connect(":memory:")
    shop = setup_scenario(conn, "queue_capacity")
    llm = Scripted([turn(("rollback_config", {"key": "queue.max_depth"}), ("replay_dead_letter", {})), LOOK])
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert steps_of(conn, "nudge") == [1] and steps_of(conn, "focus") == [4, 7]
    assert sum(m["role"] == "user" and m["content"] == COMPLETION_NUDGE for m in llm.messages) == 1
    note = conn.execute("SELECT result_json FROM agent_steps WHERE kind='focus' ORDER BY id").fetchone()[0]
    assert "All invariants pass but finish_incident has not been called" in note
    assert res.status == "escalated" and not res.verifications  # a healthy detector never closes an incident


# ------------------------------------------------------------ bounded waiting
def test_wait_is_bounded_but_usable(settings, ledger):
    conn = connect(":memory:")
    shop = setup_scenario(conn, "payment_timeout")
    waits = [turn(("wait_and_observe", {"ticks": t})) for t in (5, 4, 3, 1)]
    res = AutoHealAgent(conn, shop, Scripted(waits + [LOOK]), settings, ledger).run()
    assert steps_of(conn, "observe")[:3] == [1, 2, 3] and steps_of(conn, "blocked")[0] == 4  # 12 ticks max
    blocked = conn.execute("SELECT result_json FROM agent_steps WHERE kind='blocked'").fetchone()[0]
    assert "Time budget exhausted" in blocked and res.mutations == 0  # waiting never uses the mutation budget


def test_identical_wait_without_progress_is_blocked(settings, ledger):
    conn = connect(":memory:")
    shop = setup_scenario(conn, "payment_timeout")
    wait = turn(("wait_and_observe", {"ticks": 1}))
    AutoHealAgent(conn, shop, Scripted([wait, wait, wait, LOOK]), settings, ledger).run()
    assert steps_of(conn, "blocked")[:1] == [3]


# ------------------------------------------------------------ unchanged guarantees
def test_premature_finish_on_h1_still_fails_verification(settings, ledger):
    _, res, conn, _ = run_h1(settings, ledger, [
        turn(("rollback_config", {"key": "payment.gateway_timeout_ms"}), ("retry_stuck_payments", {"max_orders": 100})),
        FINISH_PAYMENT])
    assert scalar(conn, UNPAID) > 0  # only one of three batches was retried
    assert res.status == "escalated" and res.verifications and not any(v.passed for v in res.verifications)


def test_healthy_system_makes_zero_llm_calls(settings, ledger):
    class NoCalls:
        def chat(self, *a, **k):
            raise AssertionError("LLM must not be called for a healthy system")
    conn = connect(":memory:")
    shop = setup_scenario(conn, "healthy")
    res = AutoHealAgent(conn, shop, NoCalls(), settings, ledger).run()
    assert res.status == "healthy" and res.cost_usd == 0 and res.steps == 0


# ------------------------------------------------------------ evaluation reporting (no scoring change)
def test_eval_reports_routing_focus_and_traces_without_changing_scoring(settings, ledger, tmp_path, monkeypatch):
    import csv
    from autoheal import evaluate
    conn = connect(":memory:")
    shop = setup_scenario(conn, "queue_capacity")
    res = AutoHealAgent(conn, shop, Scripted([turn(("rollback_config", {"key": "queue.max_depth"}),
                                                   ("replay_dead_letter", {})), LOOK]), settings, ledger).run()
    row = evaluate.score("queue_capacity", res, conn)
    assert row["focus_count"] == 2 and row["nudge_count"] == 1 and not row["success"]  # never finished
    assert row["routing"][-1]["reason"].startswith("finish_incident repeatedly not called")  # v3.1 fallback
    monkeypatch.setenv("AUTOHEAL_DB", str(tmp_path / "a.db"))
    monkeypatch.setattr(evaluate, "EVAL_DIR", tmp_path)
    rep = evaluate.run_eval(get_settings(), ["playbook"], 1, ["payment_timeout", "healthy"], progress=lambda line: None)
    assert set(rep["traces"]) == {f"{rep['tag']}:playbook:payment_timeout:0", f"{rep['tag']}:playbook:healthy:0"}
    assert [s["kind"] for s in rep["traces"][f"{rep['tag']}:playbook:payment_timeout:0"]["steps"]][-1] == "verify"
    assert [r["success"] for r in rep["results"]] == [True, True] and rep["results"][0]["focus_count"] == 0
    with open(tmp_path / f"{rep['tag']}.csv", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert "focus_count" in header and "routing" in header and "traces" not in header and "steps" in header


# ------------------------------------------------------------ v3.1: stalled recovery vs. verification pending
FIX_PAYMENT = turn(("rollback_config", {"key": "payment.gateway_timeout_ms"}), ("retry_stuck_payments", {}))


def _run_payment(settings, ledger, script):
    conn = connect(":memory:")
    shop = setup_scenario(conn, "payment_timeout")
    llm = Scripted(script)
    return AutoHealAgent(conn, shop, llm, settings, ledger).run(), conn, llm


def _notes(conn):
    return [r[0] for r in conn.execute("SELECT result_json FROM agent_steps WHERE kind='focus' ORDER BY id")]


def test_healthy_but_unfinished_is_reminded_on_the_current_model_first(settings, ledger):
    res, conn, llm = _run_payment(settings, ledger, [FIX_PAYMENT, LOOK])
    assert steps_of(conn, "nudge") == [1] and steps_of(conn, "focus") == [4, 7]
    assert set(llm.models[:7]) == {settings.cheap_model}  # no upgrade at the first completion reminder (step 4)
    assert llm.models[7] == settings.strong_model  # bounded fallback at the second reminder (step 7)
    assert [r["reason"] for r in res.routing[1:]] == ["finish_incident repeatedly not called -> upgrade to strong model"]
    assert all("pending independent verification" in n and "Call finish_incident now" in n for n in _notes(conn))
    # never closed on its behalf: no finish_incident -> no verification -> step-limit escalation
    assert res.status == "escalated" and not res.verifications and res.escalation_cause == "guardrail"


def test_fallback_model_can_complete_through_verification(settings, ledger):
    res, conn, llm = _run_payment(settings, ledger, [FIX_PAYMENT] + [LOOK] * 6 + [FINISH_PAYMENT])
    assert llm.models[7] == settings.strong_model and steps_of(conn, "verify") == [8]
    assert res.status == "resolved" and res.verifications[-1].passed and ground_truth_healthy(conn)["healthy"]


def test_cheap_model_that_finishes_after_the_reminder_never_upgrades(settings, ledger):
    res, conn, llm = _run_payment(settings, ledger, [FIX_PAYMENT, LOOK, LOOK, LOOK, FINISH_PAYMENT])
    assert steps_of(conn, "focus") == [4] and set(llm.models) == {settings.cheap_model}
    assert len(res.routing) == 1 and res.status == "resolved" and res.verifications[-1].passed


def test_genuinely_stalled_recovery_still_upgrades_at_the_first_reminder(settings, ledger):
    rollback_only = turn(("rollback_config", {"key": "payment.gateway_timeout_ms"}))  # unpaid orders remain
    res, conn, llm = _run_payment(settings, ledger, [rollback_only, LOOK])
    assert steps_of(conn, "focus")[0] == 3 and llm.models[3] == settings.strong_model
    assert res.routing[1]["reason"].startswith("no measurable progress")
    note = json.loads(_notes(conn)[0])
    assert '"unpaid_orders": 60' in note and "pending independent verification" not in note


def test_stalled_reminder_names_failing_checks_when_no_work_count_applies(settings, ledger):
    # gateway outage: payment_failure_rate has no outstanding-work count; it must not be called "healthy"
    conn = connect(":memory:")
    shop = setup_scenario(conn, "gateway_outage")
    probe = turn(("set_config", {"key": "queue.worker_batch", "value": "30"}))  # a change that cannot help
    res = AutoHealAgent(conn, shop, Scripted([probe, LOOK]), settings, ledger).run()
    note = json.loads(_notes(conn)[0])
    assert "failing checks" in note and "payment_failure_rate" in note and "All invariants pass" not in note
    assert res.routing[1]["reason"].startswith("no measurable progress")
