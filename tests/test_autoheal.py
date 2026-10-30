"""Offline test suite - no API key, no network, $0."""
import json

import pytest

from autoheal.agent import AutoHealAgent
from autoheal.config import get_settings
from autoheal.db import connect
from autoheal.detector import run_checks
from autoheal.llm import BudgetExceeded, UsageLedger
from autoheal.runtime import run_scenario
from autoheal.scenarios import SCENARIOS, ground_truth_healthy, setup_scenario
from autoheal.tools import ToolContext, ToolError, execute, openai_tool_schemas
from autoheal.verifier import verify


@pytest.fixture
def settings():
    return get_settings(db_path=":memory:", max_total_usd=1.0, max_incident_usd=0.1)


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def ledger():
    return UsageLedger(":memory:")


def call(name, args=None, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


class ScriptedLLM:
    """Replays a fixed list of assistant messages, then repeats the last one forever."""
    def __init__(self, script):
        self.script, self.i, self.calls = script, 0, 0

    def chat(self, messages, tools, model, purpose="", incident_id=0):
        self.calls += 1
        msg = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return msg


# ---------------------------------------------------------------- simulator
@pytest.mark.parametrize("name", [n for n in SCENARIOS if n != "healthy"])
def test_every_fault_is_detected(conn, name):
    setup_scenario(conn, name)
    assert run_checks(conn), f"{name} produced no symptoms"
    assert not ground_truth_healthy(conn)["healthy"]


def test_healthy_system_has_no_findings(conn):
    setup_scenario(conn, "healthy")
    assert run_checks(conn) == []


# -------------------------------------------------------------------- tools
def test_readonly_sql_blocks_writes_and_hidden_tables(conn):
    shop = setup_scenario(conn, "healthy")
    ctx = ToolContext(conn, shop)
    for q in ["DELETE FROM orders", "SELECT * FROM sim_faults", "SELECT 1; DROP TABLE orders",
              "WITH x AS (SELECT 1) SELECT * FROM incidents"]:
        with pytest.raises(ToolError):
            execute(ctx, "run_readonly_sql", {"query": q})
    out = execute(ctx, "run_readonly_sql", {"query": "SELECT COUNT(*) AS n FROM orders"})
    assert out["rows"][0]["n"] > 0


def test_set_config_validates_ranges(conn):
    shop = setup_scenario(conn, "healthy")
    with pytest.raises(ToolError):
        execute(ToolContext(conn, shop), "set_config", {"key": "queue.max_depth", "value": "5"})
    with pytest.raises(ToolError):
        execute(ToolContext(conn, shop), "set_config", {"key": "workflow.lock_name", "value": "x"})


def test_terminate_refuses_healthy_workflow(conn):
    shop = setup_scenario(conn, "workflow_hang")
    conn.execute("INSERT INTO workflows (name, status, started_tick, heartbeat_tick, created_tick) "
                 "VALUES ('fresh', 'running', ?, ?, ?)", (shop.tick_now,) * 3)
    fresh = conn.execute("SELECT id FROM workflows WHERE name='fresh'").fetchone()[0]
    with pytest.raises(ToolError):
        execute(ToolContext(conn, shop), "terminate_workflow", {"workflow_id": fresh, "reason": "test"})


def test_symptom_only_fix_fails_verification(conn):
    """Retrying payments without fixing the timeout must NOT pass verification."""
    shop = setup_scenario(conn, "payment_timeout")
    execute(ToolContext(conn, shop), "retry_stuck_payments", {})
    assert not verify(conn, shop).passed


def test_root_cause_fix_passes_verification(conn):
    shop = setup_scenario(conn, "payment_timeout")
    ctx = ToolContext(conn, shop)
    execute(ctx, "rollback_config", {"key": "payment.gateway_timeout_ms"})
    execute(ctx, "retry_stuck_payments", {})
    assert verify(conn, shop).passed
    assert ground_truth_healthy(conn)["healthy"]


def test_combo_cascade_drains_without_raising_queue_limit(conn):
    """Retrying 200 payments fills the restored queue; draining (not a bigger queue) lets replay succeed."""
    shop = setup_scenario(conn, "combo_payment_queue")
    ctx = ToolContext(conn, shop)
    for key in ("payment.gateway_timeout_ms", "queue.max_depth"):
        execute(ctx, "rollback_config", {"key": key})
    execute(ctx, "retry_stuck_payments", {})
    blocked = execute(ctx, "replay_dead_letter", {})
    assert blocked["replayed"] == 0 and "hint" in blocked
    execute(ctx, "set_config", {"key": "queue.worker_batch", "value": "100"})
    waited = execute(ctx, "wait_and_observe", {"ticks": 1})
    assert waited["after"]["queue_pending"] < waited["before"]["queue_pending"]
    assert execute(ctx, "replay_dead_letter", {})["remaining"] == 0
    assert verify(conn, shop).passed and ground_truth_healthy(conn)["healthy"]
    assert shop.cfg("queue.max_depth") == "200"


def test_tool_schemas_have_no_refs():
    s = json.dumps(openai_tool_schemas())
    assert "$ref" not in s and "$defs" not in s


# -------------------------------------------------------------------- agent
@pytest.mark.parametrize("name", ["payment_timeout", "queue_capacity", "sync_corruption", "workflow_hang"])
def test_playbook_resolves_single_faults(settings, conn, ledger, name):
    res, conn = run_scenario(name, "playbook", settings, conn, ledger)
    assert res.status == "resolved" and res.verifications[-1].passed
    assert ground_truth_healthy(conn)["healthy"]


def test_healthy_costs_nothing(settings, conn, ledger):
    res, _ = run_scenario("healthy", "playbook", settings, conn, ledger)
    assert res.status == "healthy" and res.steps == 0 and res.cost_usd == 0


def test_outage_is_escalated_not_looped(settings, conn, ledger):
    res, _ = run_scenario("gateway_outage", "playbook", settings, conn, ledger)
    assert res.status == "escalated" and res.mutations == 0


def test_escalation_event_works_with_cli_printer(settings, conn, ledger):
    """escalate_to_human must not emit an 'escalate' event without a reason (crashed the CLI and dashboard)."""
    from autoheal.cli import _printer
    from autoheal.runtime import build_llm
    shop = setup_scenario(conn, "gateway_outage")
    events = []
    on_event = lambda kind, d: (events.append((kind, d)), _printer(kind, d))
    res = AutoHealAgent(conn, shop, build_llm("playbook", conn, settings, ledger), settings, ledger, on_event).run()
    assert res.status == "escalated"
    assert [d for k, d in events if k == "escalate"] and all("reason" in d for k, d in events if k == "escalate")


def test_infinite_retry_llm_is_stopped(settings, conn, ledger):
    """Challenge item 5: an AI that retries forever must be stopped and escalated."""
    shop = setup_scenario(conn, "gateway_outage")
    llm = ScriptedLLM([{"role": "assistant", "content": None, "tool_calls": [call("retry_stuck_payments")]}])
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert res.status == "escalated" and res.escalation_cause == "guardrail"
    assert llm.calls < settings.max_steps


def test_false_finish_is_caught_by_verification(settings, conn, ledger):
    shop = setup_scenario(conn, "payment_timeout")
    lie = {"role": "assistant", "content": None, "tool_calls": [call("finish_incident", {
        "root_causes": [{"category": "payment", "cause": "x", "evidence": ["x"], "fix_applied": "none"}],
        "summary": "all good", "confidence": 0.99})]}
    res = AutoHealAgent(conn, shop, ScriptedLLM([lie]), settings, ledger).run()
    assert res.status == "escalated"
    assert all(not v.passed for v in res.verifications)


def test_high_risk_action_needs_approval(settings, conn, ledger):
    shop = setup_scenario(conn, "queue_capacity")
    llm = ScriptedLLM([
        {"role": "assistant", "content": None, "tool_calls": [call("purge_dead_letter")]},
        {"role": "assistant", "content": None, "tool_calls": [call("escalate_to_human", {
            "reason": "purge needs approval", "summary": "s"})]},
    ])
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert res.status == "escalated"
    assert conn.execute("SELECT COUNT(*) FROM dead_letter WHERE replayed=0").fetchone()[0] > 0  # nothing deleted
    assert conn.execute("SELECT status FROM approvals").fetchone()[0] == "pending"


def test_budget_exceeded_escalates(settings, conn, ledger):
    class Broke:
        def chat(self, *a, **k):
            raise BudgetExceeded("cap")
    shop = setup_scenario(conn, "payment_timeout")
    res = AutoHealAgent(conn, shop, Broke(), settings, ledger).run()
    assert res.status == "escalated" and "Budget" in res.escalation["reason"]


def test_text_only_llm_escalates(settings, conn, ledger):
    shop = setup_scenario(conn, "payment_timeout")
    res = AutoHealAgent(conn, shop, ScriptedLLM([{"role": "assistant", "content": "I think it's fine"}]),
                        settings, ledger).run()
    assert res.status == "escalated"


def test_llm_style_investigation_resolves_combo(settings, conn, ledger):
    """A realistic LLM trajectory on the multi-fault scenario, including the cascade."""
    shop = setup_scenario(conn, "combo_payment_queue")
    finish = {"role": "assistant", "content": None, "tool_calls": [call("finish_incident", {
        "root_causes": [
            {"category": "payment", "cause": "timeout 500ms < gateway latency", "evidence": ["r142"], "fix_applied": "rollback"},
            {"category": "queue", "cause": "max_depth 10", "evidence": ["r143"], "fix_applied": "rollback+replay"}],
        "summary": "two deploys", "confidence": 0.9})]}
    llm = ScriptedLLM([
        {"role": "assistant", "content": None, "tool_calls": [call("get_config_history", {}, "a"),
                                                               call("search_logs", {"level": "WARN"}, "b")]},
        {"role": "assistant", "content": None, "tool_calls": [
            call("rollback_config", {"key": "payment.gateway_timeout_ms"}, "c"),
            call("rollback_config", {"key": "queue.max_depth"}, "d")]},
        {"role": "assistant", "content": None, "tool_calls": [call("retry_stuck_payments", {}, "e")]},
        {"role": "assistant", "content": None, "tool_calls": [call("set_config", {"key": "queue.max_depth", "value": "500"}, "f"),
                                                               call("replay_dead_letter", {}, "g")]},
        finish,
    ])
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert res.status == "resolved", res.report_md
    assert {rc["category"] for rc in res.root_causes} == {"payment", "queue"}
    assert res.routing[0]["model"] == settings.strong_model  # multi-symptom -> strong model


# ---------------------------------------------------- order lifecycle checks
def _order(conn, shop, status, payment=True, queue=None, dead_letter=False, shipment=False):
    """Insert one order in an arbitrary lifecycle state (to build inconsistencies on purpose)."""
    t = shop.tick_now
    oid = conn.execute("INSERT INTO orders (customer_id, amount, status, created_tick, updated_tick) "
                       "VALUES (1, 50, ?, ?, ?)", (status, t, t)).lastrowid
    if payment:
        conn.execute("INSERT INTO payments (order_id, amount, status, tick) VALUES (?, 50, 'captured', ?)", (oid, t))
    if queue:
        conn.execute("INSERT INTO queue (order_id, status, enqueued_tick) VALUES (?, ?, ?)", (oid, queue, t))
    if dead_letter:
        conn.execute("INSERT INTO dead_letter (order_id, reason, tick) VALUES (?, 'queue_full', ?)", (oid, t))
    if shipment:
        conn.execute("INSERT INTO shipments (order_id, tick) VALUES (?, ?)", (oid, t))
    conn.commit()
    return oid


def _checks(conn):
    return {f.check for f in run_checks(conn)}


def test_orphaned_paid_order_fails_verification(conn):
    shop = setup_scenario(conn, "healthy")
    _order(conn, shop, "paid")  # paid, but no pending queue row and no dead letter: lost
    v = verify(conn, shop)
    assert not v.passed and "orders_lost" in {f.check for f in v.findings}
    assert not ground_truth_healthy(conn)["order_lifecycle_consistent"]


@pytest.mark.parametrize("state, check", [
    (dict(status="paid", payment=False, queue="pending"), "progress_without_payment"),
    (dict(status="fulfilled", payment=False, shipment=True), "progress_without_payment"),
    (dict(status="created"), "payment_without_progress"),
    (dict(status="payment_failed"), "payment_without_progress"),
    (dict(status="fulfilled"), "fulfilled_without_shipment"),
])
def test_lifecycle_inconsistencies_are_detected(conn, state, check):
    shop = setup_scenario(conn, "healthy")
    _order(conn, shop, **state)
    assert check in _checks(conn)
    assert not ground_truth_healthy(conn)["order_lifecycle_consistent"]


def test_queued_and_dead_lettered_orders_are_not_lost(conn):
    shop = setup_scenario(conn, "healthy")
    _order(conn, shop, "paid", queue="pending")  # in flight
    assert _checks(conn) == set()
    _order(conn, shop, "paid", dead_letter=True)  # reported by dead_letter_backlog only, not double-counted
    assert _checks(conn) == {"dead_letter_backlog"}


def test_purging_dead_letters_loses_orders(conn):
    shop = setup_scenario(conn, "queue_capacity")
    execute(ToolContext(conn, shop), "purge_dead_letter", {})
    assert "orders_lost" in _checks(conn)  # the destructive "fix" cannot pass as healthy


def test_no_lifecycle_false_positives_after_recovery(settings, ledger):
    """Healthy, every playbook recovery, the scripted combo recovery and the outage escalation stay consistent."""
    from autoheal.detector import lifecycle_violations
    for name in ["healthy", "payment_timeout", "queue_capacity", "sync_corruption", "workflow_hang", "gateway_outage"]:
        res, c = run_scenario(name, "playbook", settings, connect(":memory:"), ledger)
        assert not any(lifecycle_violations(c).values()), name
        if res.status != "escalated":
            assert ground_truth_healthy(c)["healthy"], name
    c = connect(":memory:")
    shop = setup_scenario(c, "combo_payment_queue")
    ctx = ToolContext(c, shop)
    for key in ("payment.gateway_timeout_ms", "queue.max_depth"):
        execute(ctx, "rollback_config", {"key": key})
    execute(ctx, "retry_stuck_payments", {})
    execute(ctx, "set_config", {"key": "queue.worker_batch", "value": "100"})
    execute(ctx, "wait_and_observe", {"ticks": 1})
    execute(ctx, "replay_dead_letter", {})
    assert verify(c, shop).passed and not any(lifecycle_violations(c).values())


# ------------------------------------------------------- evaluation scoring
def _escalate_msg(categories):
    return {"role": "assistant", "content": None, "tool_calls": [call("escalate_to_human", {
        "reason": "PayFast returns 503", "summary": "upstream outage", "suspected_categories": categories})]}


def test_eval_llm_error_is_infra_not_success_and_keeps_cost(settings, conn, ledger):
    from autoheal.evaluate import score, summarize
    from autoheal.llm import LLMError

    class SpendsThenFails:
        calls = 0

        def chat(self, messages, tools, model, purpose="", incident_id=0):
            self.calls += 1
            if self.calls > 1:
                raise LLMError("HTTP 502")
            ledger.record(model=model, incident_id=incident_id, cost_usd=0.04)
            return {"role": "assistant", "content": None, "tool_calls": [call("get_health_report")]}

    shop = setup_scenario(conn, "gateway_outage")
    res = AutoHealAgent(conn, shop, SpendsThenFails(), settings, ledger).run()
    infra = {"agent": "a", **score("gateway_outage", res, conn)}
    assert res.escalation_cause == "infrastructure" and not infra["success"]
    assert infra["cost_usd"] == pytest.approx(0.04)
    ok = {"agent": "a", "scenario": "payment_timeout", "success": True, "diagnosis_correct": True,
          "escalation_cause": "", "cost_usd": 0.02, "steps": 3, "wall_s": 1.0}
    s = summarize([infra, ok])["a"]
    assert s["infra_failure_count"] == 1 and s["valid_runs"] == 1
    assert s["valid_run_success_rate"] == 1.0  # infra run excluded here...
    assert s["end_to_end_success_rate"] == 0.5  # ...but not here
    assert s["total_cost_usd"] == pytest.approx(0.06)  # the infra run's spend is kept


def test_eval_agent_escalation_with_external_dependency_is_success(settings, conn, ledger):
    from autoheal.evaluate import score
    shop = setup_scenario(conn, "gateway_outage")
    res = AutoHealAgent(conn, shop, ScriptedLLM([_escalate_msg(["external_dependency", "payment"])]),
                        settings, ledger).run()
    row = score("gateway_outage", res, conn)
    assert res.escalation_cause == "agent" and row["success"]
    assert "**agent**" in res.report_md


def test_eval_escalation_without_external_dependency_is_not_success(settings, conn, ledger):
    from autoheal.evaluate import score
    res, c = run_scenario("gateway_outage", "playbook", settings, conn, ledger)  # playbook says "unknown"
    assert res.escalation_cause == "agent" and not score("gateway_outage", res, c)["success"]


def test_eval_guardrail_stop_is_not_success(settings, conn, ledger):
    from autoheal.evaluate import score, summarize
    shop = setup_scenario(conn, "gateway_outage")
    llm = ScriptedLLM([{"role": "assistant", "content": None, "tool_calls": [call("retry_stuck_payments")]}])
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    row = {"agent": "a", **score("gateway_outage", res, conn)}
    assert res.escalation_cause == "guardrail" and not row["success"]
    s = summarize([row])["a"]
    assert s["guardrail_stop_count"] == 1 and s["infra_failure_count"] == 0 and s["valid_run_success_rate"] == 0


def test_cost_per_success_includes_failed_runs():
    from autoheal.evaluate import summarize
    base = {"agent": "a", "scenario": "payment_timeout", "diagnosis_correct": True, "steps": 1, "wall_s": 1.0}
    runs = [{**base, "success": True, "escalation_cause": "", "cost_usd": 0.10},
            {**base, "success": False, "escalation_cause": "guardrail", "cost_usd": 0.30},
            {**base, "success": False, "escalation_cause": "infrastructure", "cost_usd": 0.20}]
    assert summarize(runs)["a"]["cost_per_success_usd"] == pytest.approx(0.60)


def test_crashed_eval_run_keeps_its_spend(tmp_path, monkeypatch):
    from autoheal import evaluate

    def crash(name, agent, settings, conn=None, ledger=None, on_event=None):
        ledger.record(model="m", incident_id=1, cost_usd=0.07)
        raise RuntimeError("boom")

    monkeypatch.setattr(evaluate, "run_scenario", crash)
    monkeypatch.setattr(evaluate, "EVAL_DIR", tmp_path / "eval")
    s = get_settings(db_path=str(tmp_path / "autoheal.db"))
    rep = evaluate.run_eval(s, ["openrouter"], 1, ["payment_timeout"], progress=lambda line: None)
    assert rep["results"][0]["status"] == "crashed"
    assert rep["summary"]["openrouter"]["total_cost_usd"] == pytest.approx(0.07)


# ------------------------------------------------------- completion nudge
def _turn(*calls):
    return {"role": "assistant", "content": None,
            "tool_calls": [call(name, args, f"t{i}") for i, (name, args) in enumerate(calls)]}


LOOK = _turn(("get_health_report", {}))


class HookedLLM(ScriptedLLM):
    """ScriptedLLM that keeps the conversation and can change the system out of band before a given call."""
    def __init__(self, script, hooks=None):
        super().__init__(script)
        self.hooks, self.messages = hooks or {}, []

    def chat(self, messages, tools, model, purpose="", incident_id=0):
        self.messages = messages
        if self.calls in self.hooks:
            self.hooks[self.calls]()
        return super().chat(messages, tools, model, purpose, incident_id)


def _nudges(llm, conn):
    from autoheal.agent import COMPLETION_NUDGE
    sent = [m for m in llm.messages if m["role"] == "user" and m["content"] == COMPLETION_NUDGE]
    logged = [r["step"] for r in conn.execute("SELECT step FROM agent_steps WHERE kind='nudge' ORDER BY id")]
    assert len(sent) == len(logged)
    return logged


def _repair_queue(conn, shop):
    ctx = ToolContext(conn, shop)
    execute(ctx, "rollback_config", {"key": "queue.max_depth"})
    execute(ctx, "replay_dead_letter", {})


def test_repaired_but_still_investigating_gets_exactly_one_nudge(settings, conn, ledger):
    shop = setup_scenario(conn, "queue_capacity")
    llm = HookedLLM([_turn(("rollback_config", {"key": "queue.max_depth"}), ("replay_dead_letter", {})),
                     _turn(("set_config", {"key": "queue.worker_batch", "value": "30"})),  # still healthy: no 2nd
                     LOOK])
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert _nudges(llm, conn) == [1]
    nudge_at = next(i for i, m in enumerate(llm.messages) if m["role"] == "user" and i > 1)
    assert [m["role"] for m in llm.messages[nudge_at - 3:nudge_at]] == ["assistant", "tool", "tool"]  # after the turn
    # A healthy detector never closes the incident: no finish_incident -> no verification -> step limit.
    assert res.status == "escalated" and res.escalation_cause == "guardrail" and not res.verifications


def test_nudge_then_finish_still_goes_through_verification(settings, conn, ledger):
    shop = setup_scenario(conn, "queue_capacity")
    finish = _turn(("finish_incident", {"root_causes": [{"category": "queue", "cause": "max_depth 10",
                                                          "evidence": ["r143"], "fix_applied": "rollback+replay"}],
                                         "summary": "queue shrink reverted", "confidence": 0.9}))
    llm = HookedLLM([_turn(("rollback_config", {"key": "queue.max_depth"}), ("replay_dead_letter", {})), finish])
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert _nudges(llm, conn) == [1]
    assert res.status == "resolved" and len(res.verifications) == 1 and res.verifications[0].passed


def test_wait_and_observe_can_trigger_the_nudge(settings, conn, ledger):
    shop = setup_scenario(conn, "workflow_hang")
    wf = conn.execute("SELECT id FROM workflows WHERE hung=1").fetchone()["id"]
    llm = HookedLLM([_turn(("terminate_workflow", {"workflow_id": wf, "reason": "stale heartbeat"})),
                     _turn(("wait_and_observe", {"ticks": 1})), LOOK])
    AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert _nudges(llm, conn) == [2]  # not after terminate (waiting backlog remains), only after the wait


def test_no_nudge_while_violations_remain(settings, conn, ledger):
    shop = setup_scenario(conn, "payment_timeout")
    llm = HookedLLM([_turn(("rollback_config", {"key": "payment.gateway_timeout_ms"})), LOOK])
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert _nudges(llm, conn) == [] and res.status == "escalated"


@pytest.mark.parametrize("action, dry_run, expected", [
    (("purge_dead_letter", {}), False, []),                                     # blocked (needs approval)
    (("terminate_workflow", {"workflow_id": 999, "reason": "x"}), False, []),   # failed (ToolError)
    (("set_config", {"key": "queue.worker_batch", "value": "30"}), True, []),  # dry-run, not executed
    (("set_config", {"key": "queue.worker_batch", "value": "30"}), False, [1]),  # control: executed
])
def test_no_nudge_for_blocked_failed_or_dry_run_actions(conn, ledger, action, dry_run, expected):
    s = get_settings(db_path=":memory:", max_total_usd=1.0, max_incident_usd=0.1, dry_run=dry_run)
    shop = setup_scenario(conn, "queue_capacity")
    llm = HookedLLM([_turn(action), LOOK], hooks={0: lambda: _repair_queue(conn, shop)})  # healthy before the call
    AutoHealAgent(conn, shop, llm, s, ledger).run()
    assert _nudges(llm, conn) == expected


def test_nudge_rearms_after_new_violations(settings, conn, ledger):
    shop = setup_scenario(conn, "queue_capacity")

    def break_again():
        conn.execute("INSERT INTO dead_letter (order_id, reason, tick) SELECT id, 'queue_full', 0 FROM orders "
                     "WHERE status='paid' LIMIT 1")
        conn.execute("UPDATE queue SET status='done' WHERE order_id IN (SELECT order_id FROM dead_letter "
                     "WHERE replayed=0)")
        conn.commit()

    llm = HookedLLM([_turn(("rollback_config", {"key": "queue.max_depth"}), ("replay_dead_letter", {})),
                     _turn(("set_config", {"key": "queue.worker_batch", "value": "30"})),  # sees new violation
                     _turn(("replay_dead_letter", {"max_messages": 50})),                 # healthy again
                     LOOK], hooks={1: break_again})
    AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert _nudges(llm, conn) == [1, 3]


def test_premature_finish_still_fails_verification(settings, conn, ledger):
    shop = setup_scenario(conn, "payment_timeout")
    finish = _turn(("finish_incident", {"root_causes": [{"category": "payment", "cause": "stuck payments",
                                                          "evidence": ["created orders"], "fix_applied": "retry"}],
                                         "summary": "retried", "confidence": 0.5}))
    llm = HookedLLM([_turn(("retry_stuck_payments", {})), finish])  # symptom-only fix: timeout still 500ms
    res = AutoHealAgent(conn, shop, llm, settings, ledger).run()
    assert _nudges(llm, conn) == []
    assert res.status == "escalated" and res.verifications and not any(v.passed for v in res.verifications)


# ------------------------------------------------------ escalation reason
def test_eval_records_escalation_reason(settings, conn, ledger, tmp_path, monkeypatch):
    import csv
    from autoheal import evaluate
    shop = setup_scenario(conn, "gateway_outage")
    res = AutoHealAgent(conn, shop, ScriptedLLM([_escalate_msg(["external_dependency"])]), settings, ledger).run()
    assert evaluate.score("gateway_outage", res, conn)["escalation_reason"] == "PayFast returns 503"
    shop = setup_scenario(conn, "gateway_outage")
    res = AutoHealAgent(conn, shop, ScriptedLLM([_turn(("retry_stuck_payments", {}))]), settings, ledger).run()
    row = evaluate.score("gateway_outage", res, conn)
    assert row["escalation_cause"] == "guardrail" and row["escalation_reason"] == res.escalation["reason"] != ""
    # End to end into the per-run JSON and CSV (free playbook run).
    monkeypatch.setattr(evaluate, "EVAL_DIR", tmp_path)
    rep = evaluate.run_eval(get_settings(db_path=str(tmp_path / "a.db")), ["playbook"], 1, ["gateway_outage", "healthy"],
                            progress=lambda line: None)
    reasons = {r["scenario"]: r["escalation_reason"] for r in rep["results"]}
    assert reasons == {"gateway_outage": "No runbook rule matches the remaining symptoms.", "healthy": ""}
    with open(tmp_path / f"{rep['tag']}.csv", encoding="utf-8") as f:
        assert {r["scenario"]: r["escalation_reason"] for r in csv.DictReader(f)} == reasons


def test_eval_reports_nudge_observations(settings, ledger):
    from autoheal.evaluate import score
    finish = _turn(("finish_incident", {"root_causes": [{"category": "queue", "cause": "max_depth 10",
                                                          "evidence": ["r143"], "fix_applied": "rollback+replay"}],
                                         "summary": "reverted", "confidence": 0.9}))
    repair = _turn(("rollback_config", {"key": "queue.max_depth"}), ("replay_dead_letter", {}))
    cases = [([repair, finish], {"nudge_count": 1, "nudge_before_finish": True}),
             ([repair, LOOK], {"nudge_count": 1, "nudge_before_finish": False}),  # nudged, never finished
             ([LOOK], {"nudge_count": 0, "nudge_before_finish": ""})]
    for script, expected in cases:
        c = connect(":memory:")
        shop = setup_scenario(c, "queue_capacity")
        res = AutoHealAgent(c, shop, ScriptedLLM(script), settings, ledger).run()
        row = score("queue_capacity", res, c)
        assert {k: row[k] for k in expected} == expected
