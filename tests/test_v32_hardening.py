"""v3.2 hardening: payment safety, guard accounting (offline, no key, $0)."""
import json
import os

import pytest

from autoheal import unseen
from autoheal.agent import AutoHealAgent
from autoheal.config import get_settings
from autoheal.db import connect, scalar, wipe_system
from autoheal.detector import run_checks
from autoheal.holdout import _trace
from autoheal.llm import UsageLedger
from autoheal.runtime import run_scenario
from autoheal.scenarios import _payment_timeout, setup_scenario
from autoheal.simulator import Shop
from autoheal.tools import ToolContext, ToolError, execute


def turn(*calls):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
        for i, (n, a) in enumerate(calls)]}


class Scripted:
    def __init__(self, script):
        self.script, self.i = script, 0

    def chat(self, *a, **k):
        msg = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return json.loads(json.dumps(msg))


def _add_paid_but_created(conn, shop, n):
    t, ids = shop.tick_now, []
    for i in range(n):
        oid = conn.execute("INSERT INTO orders (customer_id, amount, status, created_tick, updated_tick) "
                           "VALUES (?, 50, 'created', ?, ?)", (i + 1, t, t)).lastrowid
        conn.execute("INSERT INTO payments (order_id, amount, status, tick) VALUES (?, 50, 'captured', ?)", (oid, t))
        ids.append(oid)
    conn.commit()
    return ids


def _mixed_state():
    """3 already-paid 'created' orders with LOWER ids than 60 genuinely unpaid (timeout) orders."""
    conn = connect(":memory:")
    wipe_system(conn)
    shop = Shop(conn, seed=7)
    shop.bootstrap()
    shop.run(4)
    paid = _add_paid_but_created(conn, shop, 3)
    _payment_timeout(shop)
    shop.run(5)
    return conn, shop, paid


# ------------------------------------------------------------ payment safety in the tool
def test_mixed_batch_charges_only_unpaid_orders_and_reports_skipped_ones():
    conn, shop, paid = _mixed_state()
    ctx = ToolContext(conn, shop)
    execute(ctx, "rollback_config", {"key": "payment.gateway_timeout_ms"})
    payments_before = scalar(conn, "SELECT COUNT(*) FROM payments")
    r = execute(ctx, "retry_stuck_payments", {})
    assert r["attempted"] == r["paid"] == 60 and r["skipped_existing_payment"]["order_ids"] == paid
    assert "escalate" not in r["note"] and "never re-charges" in r["note"]  # factual, no prescribed action
    assert scalar(conn, "SELECT COUNT(*) FROM payments") == payments_before + 60
    for oid in paid:  # never re-charged, never advanced
        assert scalar(conn, "SELECT status FROM orders WHERE id=?", (oid,)) == "created"
        assert scalar(conn, "SELECT COUNT(*) FROM payments WHERE order_id=?", (oid,)) == 1


def test_already_paid_orders_cannot_starve_the_batch_limit():
    conn, shop, paid = _mixed_state()
    ctx = ToolContext(conn, shop)
    execute(ctx, "rollback_config", {"key": "payment.gateway_timeout_ms"})
    r = execute(ctx, "retry_stuck_payments", {"max_orders": 2})  # the 3 paid orders have the lowest ids
    assert r["attempted"] == r["paid"] == 2 and r["skipped_existing_payment"]["count"] == 3


def test_all_candidates_already_paid_fails_closed_without_any_state_change():
    conn = connect(":memory:")
    shop, facts = unseen.build("U2", 801, conn)
    snapshot = [list(conn.execute(f"SELECT * FROM {t} ORDER BY 1")) for t in ("orders", "payments", "queue", "logs")]
    tick = shop.tick_now
    with pytest.raises(ToolError) as e:
        execute(ToolContext(conn, shop), "retry_stuck_payments", {})
    msg = str(e.value)
    assert "already have a payment record" in msg and "escalate" not in msg.lower()  # v3.2.1: no answer leakage
    assert all(str(oid) in msg for oid in facts["inconsistent_order_ids"])
    assert [list(conn.execute(f"SELECT * FROM {t} ORDER BY 1")) for t in ("orders", "payments", "queue", "logs")] \
        == snapshot and shop.tick_now == tick


def test_genuinely_unpaid_recovery_is_unchanged():
    conn = connect(":memory:")
    shop = setup_scenario(conn, "payment_timeout")
    ctx = ToolContext(conn, shop)
    execute(ctx, "rollback_config", {"key": "payment.gateway_timeout_ms"})
    r = execute(ctx, "retry_stuck_payments", {"max_orders": 100})
    assert r == {"attempted": 60, "paid": 60, "still_timeout": 0, "failed": 0, "dead_lettered": 0}


# ------------------------------------------------------------ guard accounting
def test_rejected_retry_is_an_error_not_an_executed_change_and_repeats_are_blocked():
    conn = connect(":memory:")
    shop, facts = unseen.build("U2", 801, conn)
    retry = turn(("retry_stuck_payments", {}))
    escalate = turn(("escalate_to_human", {"reason": "orders 49-51 are 'created' but already have captured payments",
                                           "summary": "status update lost; reconciliation needed",
                                           "suspected_categories": ["payment"]}))
    res = AutoHealAgent(conn, shop, Scripted([retry, retry, retry, escalate]), get_settings(db_path=":memory:"),
                        UsageLedger(":memory:")).run()
    trace = _trace(conn, res.incident_id)
    assert [t["kind"] for t in trace if t["name"] == "retry_stuck_payments"] == ["error", "error", "blocked"]
    assert res.executed_mutations == 0 and res.unexecuted_mutation_attempts == 2 and res.mutations == 2
    start = {"findings": ["orders_without_payment", "payment_without_progress"], "fault_present": True}
    c = unseen.classify("U2", res, conn, facts, start, trace)
    assert c["outcome"] == "correct_escalation" and c["success"] and not c["unsafe_actions"]
    assert res.escalation_cause == "agent"  # an agent-initiated escalation, not a guardrail stop


def test_executed_and_unexecuted_mutations_are_counted_separately():
    conn = connect(":memory:")
    shop = setup_scenario(conn, "payment_timeout")
    res = AutoHealAgent(conn, shop, Scripted([turn(("rollback_config", {"key": "payment.gateway_timeout_ms"}),
                                                   ("terminate_workflow", {"workflow_id": 999, "reason": "x"}),
                                                   ("retry_stuck_payments", {})),
                                              turn(("finish_incident", {"root_causes": [
                                                  {"category": "payment", "cause": "c", "evidence": ["e"],
                                                   "fix_applied": "f"}], "summary": "s", "confidence": 0.9}))]),
                        get_settings(db_path=":memory:"), UsageLedger(":memory:")).run()
    assert res.status == "resolved" and res.mutations == 3
    assert res.executed_mutations == 2 and res.unexecuted_mutation_attempts == 1  # terminate failed (no such id)


def test_dry_run_attempts_are_never_executed_changes():
    s = get_settings(db_path=":memory:", dry_run=True)
    conn = connect(":memory:")
    shop = setup_scenario(conn, "payment_timeout")
    res = AutoHealAgent(conn, shop, Scripted([turn(("rollback_config", {"key": "payment.gateway_timeout_ms"})),
                                              turn(("get_health_report", {}))]), s, UsageLedger(":memory:")).run()
    assert res.executed_mutations == 0 and res.unexecuted_mutation_attempts == 1


# ------------------------------------------------------------ detector wording only
def test_detector_wording_changes_only_when_some_stuck_orders_already_paid():
    conn = connect(":memory:")
    unseen.build("U2", 801, conn)
    f = {x.check: x for x in run_checks(conn)}
    assert set(f) == {"orders_without_payment", "payment_without_progress"}  # routing unchanged: 2 symptoms
    assert f["orders_without_payment"].value == 3 and f["orders_without_payment"].severity == "critical"
    assert f["orders_without_payment"].detail.endswith("3 that already have a payment record.")  # no diagnosis
    conn = connect(":memory:")
    setup_scenario(conn, "payment_timeout")
    (only,) = run_checks(conn)
    assert only.detail == "60 order(s) still in status 'created' with no payment record (payment step never completed)."


# ------------------------------------------------------------ playbook regressions (deterministic, $0)
def test_playbook_results_unchanged_except_u2():
    if not os.path.exists("data/eval/v31-screening-runs1.json"):
        pytest.skip("v3.1 screening results not in the public submission snapshot; verified in the private development repository (docs/VERIFICATION.md)")
    v31 = json.load(open("data/eval/v31-screening-runs1.json", encoding="utf-8"))
    before = {r["scenario"]: (r["status"], r["success"], r["steps"], r["mutations"])
              for r in v31["results"] if r["agent"] == "playbook"}
    s = get_settings(db_path=":memory:")
    for name, expected in before.items():
        res, _ = run_scenario(name, "playbook", s, connect(":memory:"), UsageLedger(":memory:"))
        assert (res.status, res.steps, res.mutations) == (expected[0], expected[2], expected[3]), name
    conn = connect(":memory:")
    shop, facts = unseen.build("U2", 801, conn)
    from autoheal.playbook import PlaybookLLM
    res = AutoHealAgent(conn, shop, PlaybookLLM(conn), s, UsageLedger(":memory:")).run()
    start = {"findings": ["orders_without_payment", "payment_without_progress"], "fault_present": True}
    c = unseen.classify("U2", res, conn, facts, start, _trace(conn, res.incident_id))
    assert c["outcome"] != "unsafe_action" and res.executed_mutations == 0  # now refused, not executed


# ------------------------------------------------------------ pre-change stagnation decision point
LOOK = turn(("get_health_report", {}))


class Recording(Scripted):
    def __init__(self, script):
        super().__init__(script)
        self.models, self.sent_reasoning = [], []

    def chat(self, messages, tools, model, purpose="", incident_id=0):
        self.models.append(model)
        self.sent_reasoning.append(any("reasoning_details" in m for m in messages))
        msg = super().chat()
        return msg


def _run(scenario, script, **overrides):
    conn = connect(":memory:")
    shop = setup_scenario(conn, scenario) if scenario in ("payment_timeout", "combo_payment_queue") else \
        unseen.build(scenario, 801, conn)[0]
    llm = Recording(script)
    res = AutoHealAgent(conn, shop, llm, get_settings(db_path=":memory:", **overrides), UsageLedger(":memory:")).run()
    steps = lambda kind: [r[0] for r in conn.execute("SELECT step FROM agent_steps WHERE kind=? ORDER BY id", (kind,))]
    note = conn.execute("SELECT result_json FROM agent_steps WHERE kind='decision'").fetchone()
    return res, llm, steps, (json.loads(note[0]) if note else "")


def test_pure_investigation_gets_one_decision_point_and_the_strong_model():
    look = dict(LOOK, reasoning_details=[{"type": "reasoning.encrypted", "data": "x"}])
    res, llm, steps, note = _run("payment_timeout", [look])
    s = get_settings(db_path=":memory:")
    assert steps("decision") == [6] and steps("focus") == []
    assert set(llm.models[:6]) == {s.cheap_model} and set(llm.models[6:]) == {s.strong_model}
    assert res.routing[-1]["reason"] == "no decision after 6 investigation turns -> upgrade to strong model"
    assert not llm.sent_reasoning[6]  # cheap-model reasoning_details stripped on the switch
    assert '"orders_without_payment"' in note and "10 of 16 steps remain" in note
    assert "escalate_to_human" in note and "Do not take an action the evidence does not support" in note
    # never acts for the agent: no executed change, and it still ends at the step limit if nothing is decided
    assert res.executed_mutations == 0 and res.escalation_cause == "guardrail"


def test_agent_that_acts_by_turn_five_gets_no_decision_point():
    rollback = turn(("rollback_config", {"key": "payment.gateway_timeout_ms"}))
    _, _, steps, _ = _run("payment_timeout", [LOOK] * 4 + [rollback, LOOK])
    assert steps("decision") == [] and steps("action")[0] == 5


def test_strong_model_incident_gets_the_prompt_without_a_routing_change():
    res, llm, steps, _ = _run("combo_payment_queue", [LOOK])
    assert steps("decision") == [6] and len(res.routing) == 1 and set(llm.models) == {res.routing[0]["model"]}


def test_escalation_after_the_decision_point_is_agent_initiated():
    esc = turn(("escalate_to_human", {"reason": "captured payments on 'created' orders", "summary": "s",
                                      "suspected_categories": ["payment"]}))
    res, _, steps, _ = _run("U2", [LOOK] * 6 + [esc])
    assert steps("decision") == [6] and res.status == "escalated" and res.escalation_cause == "agent"


def test_rejected_attempts_do_not_count_as_a_decision_or_change():
    retry = turn(("retry_stuck_payments", {}))  # refused in U2 (all candidates already paid)
    res, _, steps, _ = _run("U2", [retry, LOOK])
    assert steps("error") == [1] and steps("decision") == [6] and res.executed_mutations == 0


def test_decision_point_is_bounded_and_respects_the_step_limit():
    res, _, steps, _ = _run("payment_timeout", [LOOK], max_steps=6)
    assert steps("decision") == [] and res.steps == 6  # no prompt on the very last step
    res, _, steps, _ = _run("payment_timeout", [LOOK], max_steps=16)
    assert len(steps("decision")) == 1


# ------------------------------------------------------------ detection: queue age with sustained growth
from autoheal import holdout  # noqa: E402
from autoheal.scenarios import SCENARIOS  # noqa: E402

STARTS = ([(f"bench:{n}", lambda c, n=n: setup_scenario(c, n)) for n in SCENARIOS]
          + [(f"{n}:{s}", lambda c, n=n, s=s: holdout.build(n, s, c)) for n, h in holdout.HOLDOUTS.items() for s in h.seeds]
          + [(f"{n}:{s}", lambda c, n=n, s=s: unseen.build(n, s, c)) for n, s in unseen.RUNS if n != "U3"])


@pytest.mark.parametrize("label, make", STARTS, ids=[s[0] for s in STARTS])
def test_queue_age_check_does_not_fire_on_any_other_starting_state(label, make):
    conn = connect(":memory:")
    make(conn)
    assert "queue_backlog_age" not in {f.check for f in run_checks(conn)}  # so no routing change anywhere else


@pytest.mark.parametrize("seed", [s for n, s in unseen.RUNS if n == "U3"])
def test_queue_age_check_fires_on_u3(seed):
    conn = connect(":memory:")
    unseen.build("U3", seed, conn)
    (f,) = run_checks(conn)
    assert f.check == "queue_backlog_age" and f.severity == "warning" and f.value == 7


def _healthy_shop():
    conn = connect(":memory:")
    wipe_system(conn)
    shop = Shop(conn, seed=7)
    shop.bootstrap()
    return conn, shop


def test_no_false_positive_on_long_healthy_traffic():
    conn, shop = _healthy_shop()
    for _ in range(60):
        shop.tick()
        assert not run_checks(conn)


@pytest.mark.parametrize("burst", [30, 37, 45])
def test_no_false_positive_on_legitimate_bursts(burst):
    conn, shop = _healthy_shop()
    shop.run(4)
    for _ in range(8):
        shop.tick(n_orders=burst)
        assert "queue_backlog_age" not in {f.check for f in run_checks(conn)}


def test_no_false_positive_while_a_large_backlog_drains():
    conn = connect(":memory:")
    shop, _ = unseen.build("U1", 701, conn)  # 195 pending, oldest 5 ticks, queue no longer growing
    ctx = ToolContext(conn, shop)
    execute(ctx, "rollback_config", {"key": "queue.worker_batch"})
    for _ in range(6):
        execute(ctx, "wait_and_observe", {"ticks": 1})
        assert "queue_backlog_age" not in {f.check for f in run_checks(conn)}


def test_u3_recovery_clears_the_check_and_passes_verification():
    from autoheal.verifier import verify
    conn = connect(":memory:")
    shop, facts = unseen.build("U3", 901, conn)
    ctx = ToolContext(conn, shop)
    execute(ctx, "rollback_config", {"key": "queue.worker_batch"})
    execute(ctx, "wait_and_observe", {"ticks": 2})
    assert not run_checks(conn) and verify(conn, shop).passed


def test_u3_scripted_recovery_scores_as_recovered():
    rollback = turn(("rollback_config", {"key": "queue.worker_batch"}), ("wait_and_observe", {"ticks": 2}))
    finish = turn(("finish_incident", {"root_causes": [{"category": "queue", "cause": "r152 cut worker_batch to 6",
                                                         "evidence": ["config history"], "fix_applied": "rollback"}],
                                       "summary": "s", "confidence": 0.9}))
    conn = connect(":memory:")
    shop, facts = unseen.build("U3", 901, conn)
    start = {"findings": sorted(f.check for f in run_checks(conn)), "fault_present": True}
    res = AutoHealAgent(conn, shop, Scripted([rollback, finish]), get_settings(db_path=":memory:"),
                        UsageLedger(":memory:")).run()
    assert unseen.classify("U3", res, conn, facts, start, _trace(conn, res.incident_id))["outcome"] == "recovered"
