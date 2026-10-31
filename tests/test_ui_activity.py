"""Console logic: activity, lifecycle and outcome come only from observed events, and observing changes nothing.
Offline: Playbook and scripted LLMs only, no API calls."""
import json

import pytest

from autoheal.agent import AutoHealAgent
from autoheal.config import get_settings
from autoheal.db import connect, rows
from autoheal.runtime import build_llm, open_ledger, open_system, run_scenario
from autoheal.scenarios import SCENARIOS, setup_scenario
from autoheal_ui import recovery
from autoheal_ui.activity import (ACTIVE, BLOCKED, COMPLETED, ESCALATED, FAILED, PENDING, SKIPPED, ActivityEvent, ActivityLog,
                                  classify_outcome, derive_lifecycle, incident_events, next_step, replay)
from autoheal_ui.evaluation_data import load_suite, recorded_escalation


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedLLM:
    """Plays back fixed assistant messages, so agent events can be exercised without an API call."""

    def __init__(self, turns):
        self.turns = list(turns)

    def chat(self, messages, tools, model, purpose="", incident_id=0):
        return self.turns.pop(0) if self.turns else {"role": "assistant", "content": "done"}


def _msg(*calls, content=None):
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


FINISH = {"root_causes": [{"category": "payment", "cause": "deploy cut the timeout", "evidence": ["config history"],
                           "fix_applied": "rollback"}], "summary": "fixed", "confidence": 0.9}


def _settings(**kw):
    return get_settings(db_path=":memory:", openrouter_api_key="", **kw)


def _run(scenario, llm_factory, observer=None, **settings_kw):
    s = _settings(**settings_kw)
    conn = connect(":memory:")
    shop = setup_scenario(conn, scenario, seed=s.seed)
    ledger = open_ledger(s, run_tag="t")
    return AutoHealAgent(conn, shop, llm_factory(conn, s, ledger), s, ledger, on_event=observer).run(), conn


def _playbook(conn, s, ledger):
    return build_llm("playbook", conn, s, ledger)


def _stored(conn, incident_id):
    incident = rows(conn, "SELECT * FROM incidents WHERE id=?", (incident_id,))[0]
    return incident, rows(conn, "SELECT * FROM agent_steps WHERE incident_id=? ORDER BY id", (incident_id,))


def _states(stages):
    return {s.name: s.state for s in stages}


# ------------------------------------------------------- behaviour neutrality
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_observer_does_not_change_the_run(name):
    plain, c1 = _run(name, _playbook)
    log = ActivityLog(llm_used=False)
    observed, c2 = _run(name, _playbook, observer=log.on_event)
    keep = lambda r: r.model_dump(exclude={"wall_s", "report_md"})
    assert keep(plain) == keep(observed)
    query = "SELECT step, kind, name, args_json, result_json FROM agent_steps ORDER BY id"
    assert rows(c1, query) == rows(c2, query)
    assert [tuple(r) for r in c1.execute("SELECT key, value FROM config ORDER BY key")] == \
           [tuple(r) for r in c2.execute("SELECT key, value FROM config ORDER BY key")]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_background_run_matches_a_direct_run(tmp_path, name):
    settings = get_settings(db_path=str(tmp_path / "bg.db"), openrouter_api_key="")
    conn, _ = open_system(settings)
    setup_scenario(conn, name, seed=settings.seed)
    run = recovery.start(settings, "playbook", "test")
    assert run.finished.wait(30)
    assert run.error is None

    direct, _ = run_scenario(name, "playbook", get_settings(db_path=":memory:", openrouter_api_key=""), conn=connect(":memory:"))
    keep = {"status", "executed_mutations", "steps", "tool_calls", "root_causes", "escalation_cause"}
    assert run.result.model_dump(include=keep) == direct.model_dump(include=keep)
    assert [v.passed for v in run.result.verifications] == [v.passed for v in direct.verifications]


def test_second_recovery_is_refused_while_one_runs(tmp_path):
    settings = get_settings(db_path=str(tmp_path / "x.db"), openrouter_api_key="")
    open_system(settings)
    held = recovery.RecoveryRun(engine="playbook", log=ActivityLog())
    recovery._runs[str(settings.db_path)] = held
    try:
        with pytest.raises(RuntimeError):
            recovery.start(settings, "playbook", "test")
    finally:
        recovery._runs.pop(str(settings.db_path))


def test_missing_key_is_reported_without_a_call(tmp_path):
    settings = get_settings(db_path=str(tmp_path / "k.db"), openrouter_api_key="")
    conn, _ = open_system(settings)
    setup_scenario(conn, "payment_timeout", seed=settings.seed)
    run = recovery.start(settings, "openrouter", "test")
    assert run.finished.wait(30)
    assert "OPENROUTER_API_KEY is not set" in run.error
    assert run.result is None


# ---------------------------------------------------------------- live feed
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_live_feed_matches_the_stored_trace(name):
    log = ActivityLog(llm_used=False)
    res, conn = _run(name, _playbook, observer=log.on_event)
    incident, steps = _stored(conn, res.incident_id)
    assert [(e.stage, e.title) for e in log.events] == [(e.stage, e.title) for e in incident_events(incident, steps)]


def test_playbook_never_claims_a_model_or_an_investigation():
    log = ActivityLog(llm_used=False)
    res, conn = _run("payment_timeout", _playbook, observer=log.on_event)
    assert not any(e.kind == "route" for e in log.events)
    assert not any(e.model for e in log.events)
    assert not any(e.stage == "Investigate" for e in log.events)
    states = _states(derive_lifecycle(log.events, status=res.status))
    assert states["Investigate"] == SKIPPED
    assert states["Execute"] == COMPLETED and states["Verify"] == COMPLETED and states["Resolved"] == COMPLETED


def test_agent_events_show_real_tools_and_results_in_order():
    def llm(conn, s, ledger):
        return ScriptedLLM([
            _msg(_call("get_health_report", {}), _call("search_logs", {"level": "WARN"}, "c2"),
                 _call("get_config_history", {}, "c3"), content="Looking at the recent deploy."),
            _msg(_call("rollback_config", {"key": "payment.gateway_timeout_ms"})),
            _msg(_call("retry_stuck_payments", {})),
            _msg(_call("finish_incident", FINISH)),
        ])

    log = ActivityLog()
    res, conn = _run("payment_timeout", llm, observer=log.on_event)
    assert res.status == "resolved"
    titles = [e.title for e in log.events]
    expected = ["Anomaly detected: 1 symptom", "Model selected for this incident", "Agent note", "Reading health report",
                "Checking logs", "Examining configuration history", "Guarded action requested: Roll back configuration",
                "Recovery action executed: Roll back configuration", "Guarded action requested: Retry stuck payments",
                "Recovery action executed: Retry stuck payments", "All health checks now pass", "Root cause reported",
                "Independent verification passed", "Recovery verified"]
    assert titles == expected
    rollback = next(e for e in log.events if e.title.startswith("Recovery action executed: Roll back"))
    assert rollback.tool == "rollback_config" and rollback.summary == "payment.gateway_timeout_ms: 500 to 3000"
    assert next(e for e in log.events if e.kind == "verify").result["passed"] is True

    incident, steps = _stored(conn, res.incident_id)
    report = json.loads(incident["report_json"])
    stored = replay(json.loads(incident["findings_json"]), steps, report["routing"], llm_used=True, status="resolved")
    assert [e.title for e in stored] == titles


def test_events_arrive_incrementally_during_a_run():
    """The feed grows while the agent is still working: each LLM turn sees the events of the previous one."""
    log = ActivityLog()
    seen = []

    class Recording(ScriptedLLM):
        def chat(self, *a, **k):
            seen.append(len(log.events))
            return super().chat(*a, **k)

    def llm(conn, s, ledger):
        return Recording([_msg(_call("get_health_report", {})), _msg(_call("get_config_history", {})),
                          _msg(_call("rollback_config", {"key": "payment.gateway_timeout_ms"})),
                          _msg(_call("retry_stuck_payments", {})), _msg(_call("finish_incident", FINISH))])

    _run("payment_timeout", llm, observer=log.on_event)
    assert seen == sorted(set(seen)) and len(seen) == 5


def test_model_upgrade_is_reported_only_when_it_happens():
    def llm(conn, s, ledger):
        return ScriptedLLM([_msg(_call("get_health_report", {}, f"c{i}")) for i in range(6)]
                           + [_msg(_call("escalate_to_human", {"reason": "no safe fix", "summary": "s"}))])

    log = ActivityLog()
    res, conn = _run("gateway_outage", llm, observer=log.on_event)
    assert res.status == "escalated"
    assert sum(e.kind == "route" for e in log.events) == 2
    assert any(e.kind == "decision" for e in log.events)
    incident, steps = _stored(conn, res.incident_id)
    stored = replay(json.loads(incident["findings_json"]), steps, res.routing, llm_used=True, status="escalated",
                    escalation=res.escalation)
    assert [e.title for e in stored] == [e.title for e in log.events]


def test_high_risk_action_shows_an_approval_request_and_blocks_execute():
    def llm(conn, s, ledger):
        return ScriptedLLM([_msg(_call("purge_dead_letter", {})),
                            _msg(_call("escalate_to_human", {"reason": "needs a human", "summary": "s"}))])

    log = ActivityLog()
    res, conn = _run("queue_capacity", llm, observer=log.on_event)
    assert any(e.title == "Human approval requested: Purge dead letters" for e in log.events)
    assert rows(conn, "SELECT status FROM approvals") == [{"status": "pending"}]
    assert _states(derive_lifecycle(log.events, status=res.status))["Execute"] == BLOCKED
    assert classify_outcome(res.model_dump(), log.events).key == "blocked"


def test_dry_run_is_never_shown_as_executed():
    def llm(conn, s, ledger):
        return ScriptedLLM([_msg(_call("rollback_config", {"key": "payment.gateway_timeout_ms"})),
                            _msg(_call("escalate_to_human", {"reason": "dry run", "summary": "s"}))])

    log = ActivityLog()
    res, _ = _run("payment_timeout", llm, observer=log.on_event, dry_run=True)
    assert not any(e.title.startswith("Recovery action executed") for e in log.events)
    assert any(e.title.startswith("Proposed only") for e in log.events)
    assert _states(derive_lifecycle(log.events, status=res.status))["Execute"] == BLOCKED


def test_refused_payment_retry_reads_as_a_safety_refusal():
    log = ActivityLog()
    log.step("error", "retry_stuck_payments", {}, {"error": "Refused: all 2 order(s) stuck already have a payment record"})
    assert log.events[-1].title == "Refused by safety controls: Retry stuck payments"
    assert _states(derive_lifecycle(log.events, status="escalated"))["Execute"] == BLOCKED


def test_rejected_read_query_stays_in_investigation():
    log = ActivityLog()
    log.step("error", "run_readonly_sql", {"query": "x"}, {"error": "SQL rejected"})
    assert log.events[-1].stage == "Investigate" and log.events[-1].effect is None


def test_truncated_recorded_verification_is_not_guessed():
    log = ActivityLog()
    log.step("verify", "finish_incident", "{}", '{"passed": false, "probe_orders": 16, "probe_fulfilled": 3, "findings": [{"che')
    assert log.events[-1].title == "Independent verification failed"
    assert log.events[-1].summary == "3/16 probe orders fulfilled"
    log.step("verify", "finish_incident", "{}", "not a json object")
    assert log.events[-1].title == "Independent verification recorded"


# ---------------------------------------------------------------- lifecycle
def test_lifecycle_is_pending_with_no_events():
    stages = derive_lifecycle([])
    assert all(s.state == PENDING for s in stages)
    assert stages[-1].name == "Resolve / Escalate"
    assert derive_lifecycle([], symptoms_present=True)[0].state == COMPLETED


def test_active_stage_follows_the_latest_event():
    log = ActivityLog()
    log.detect([{"check": "orders_lost", "severity": "critical", "value": 1, "detail": "x"}])
    log.step("observe", "get_config", {}, {"config": []})
    assert _states(derive_lifecycle(log.events, running=True))["Investigate"] == ACTIVE
    log.step("action", "resync_customers", {}, {"records_resynced": 1, "mismatched_after": 0})
    states = _states(derive_lifecycle(log.events, running=True))
    assert states["Investigate"] == COMPLETED and states["Execute"] == ACTIVE and states["Verify"] == PENDING


def test_failed_verification_and_interruption_are_visible():
    log = ActivityLog()
    log.step("verify", "finish_incident", {}, {"passed": False, "probe_orders": 4, "probe_fulfilled": 1})
    assert _states(derive_lifecycle(log.events, status="escalated"))["Verify"] == FAILED
    assert _states(derive_lifecycle(log.events, status="escalated"))["Escalated"] == ESCALATED
    assert derive_lifecycle([], status="open")[-1].name == "Interrupted"
    assert derive_lifecycle([], status="open", running=True)[-1].state == PENDING


# ---------------------------------------------------------------- outcomes
@pytest.mark.parametrize("scenario,key", [
    ("payment_timeout", "recovered"), ("queue_capacity", "recovered"), ("sync_corruption", "recovered"),
    ("workflow_hang", "recovered"), ("gateway_outage", "escalated"), ("combo_payment_queue", "verification_failed"),
    ("healthy", "no_action")])
def test_real_playbook_outcomes(scenario, key):
    log = ActivityLog(llm_used=False)
    res, _ = _run(scenario, _playbook, observer=log.on_event)
    assert classify_outcome(res.model_dump(), log.events).key == key


def _report(status="escalated", cause="agent", executed=0, initial=(("a", 5),), remaining=(("a", 5),), verifs=()):
    finding = lambda c, v: {"check": c, "value": v, "severity": "critical", "detail": ""}
    return {"status": status, "executed_mutations": executed, "findings": [finding(*f) for f in initial],
            "verifications": [{"passed": v} for v in verifs],
            "escalation": {"reason": "r", "cause": cause, "open_symptoms": [finding(*f) for f in remaining]}}


@pytest.mark.parametrize("report,key", [
    (_report(executed=2, remaining=(("a", 1),)), "partial"),
    (_report(executed=1, initial=(("a", 5), ("b", 1)), remaining=(("a", 5),)), "partial"),
    (_report(executed=1, remaining=(("a", 5),)), "attempted"),
    (_report(executed=1, remaining=(("a", 9),)), "attempted"),
    (_report(executed=1, remaining=(("a", 1), ("new", 1))), "attempted"),
    (_report(executed=0), "escalated"),
    (_report(executed=0, cause="infrastructure"), "escalated"),
    (_report(executed=0, cause="guardrail"), "blocked"),
    (_report(executed=2, cause="guardrail", remaining=(("a", 1),), verifs=(False, False)), "verification_failed"),
    (_report(executed=2, cause="agent", remaining=(("a", 1),), verifs=(False,)), "partial"),
    (_report(executed=2, cause="agent", verifs=(False,)), "verification_failed"),
    ({"status": "open"}, "interrupted"),
    ({"status": "resolved"}, "recovered"),
    ({"status": "healthy"}, "no_action"),
])
def test_outcome_categories(report, key):
    assert classify_outcome(report).key == key


def test_safety_stop_after_some_actions_reads_as_blocked():
    log = ActivityLog()
    log.step("blocked", "retry_stuck_payments", {}, {"blocked": "requires human approval (approval #1 filed)"})
    out = classify_outcome(_report(executed=1, cause="guardrail"), log.events)
    assert out.key == "blocked" and "Some actions ran" in out.explanation


def test_escalation_is_not_labelled_as_failure():
    out = classify_outcome(_report(executed=0))
    assert out.tone == "warning" and "fail" not in out.label.lower()
    assert classify_outcome({"status": "healthy"}).tone == "info"


# ---------------------------------------------------------------- recorded runs
@pytest.mark.parametrize("key", ["A", "B"])
def test_every_committed_trace_replays(key):
    suite = load_suite(key)
    for run in suite.results:
        esc = recorded_escalation(run) if run["status"] == "escalated" else None
        events = replay(run["start"]["findings"], run.get("trace") or [], run.get("routing") or [],
                        llm_used=bool(run.get("llm_called")), status=run["status"], escalation=esc)
        assert all(isinstance(e, ActivityEvent) for e in events)
        rows_seen = [t for t in run.get("trace") or [] if t["kind"] in ("observe", "action", "blocked", "error", "verify")]
        tools = [e.tool for e in events if e.kind in ("observe", "action", "blocked", "error", "verify") and e.stage != "Decide"]
        assert tools == [t["name"] for t in rows_seen], (run["case"], run["agent"])
        derive_lifecycle(events, status=run["status"])
        if run.get("routing_is_bookkeeping") or not run.get("llm_called"):
            assert not any(e.kind == "route" for e in events)


def test_recorded_p05_tells_the_partial_recovery_story():
    run = load_suite("B").run_for("P05", next(r["seed"] for r in load_suite("B").results if r["case"] == "P05"), "openrouter")
    events = replay(run["start"]["findings"], run["trace"], run["routing"], status=run["status"],
                    escalation=recorded_escalation(run))
    titles = [e.title for e in events]
    assert "Recovery action executed: Roll back configuration" in titles
    assert any(t.startswith("Reading health report") for t in titles)
    assert titles[-1] == "Escalation issued to a human operator"


# ---------------------------------------------------------------- guidance
def test_next_step_guides_each_state():
    assert "Inject Fault" in next_step(False, 0, None, None).title
    assert "Start Recovery" in next_step(False, 2, "payment_timeout", None).title
    assert "Start Recovery" in next_step(False, 0, "healthy", None).title
    assert "Generate Test Traffic" in next_step(False, 0, "sync_corruption", None).title
    assert "progress" in next_step(True, 2, "payment_timeout", None).title
    assert "Review" in next_step(False, 0, "payment_timeout", "resolved").title
    assert "escalation" in next_step(False, 1, "gateway_outage", "escalated").title
