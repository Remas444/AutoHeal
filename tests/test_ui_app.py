"""Headless console tests with Streamlit's AppTest. Offline: an empty key guarantees no LLM call can be made."""
import json
import re
import threading
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autoheal_ui import recovery
from autoheal_ui.content import SCENARIO_COPY

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "app.py")
OPERATIONS, SAFETY = "app_pages/operations.py", "app_pages/safety_audit.py"
PAGES = [OPERATIONS, "app_pages/insights.py", SAFETY]
UI_SOURCES = [ROOT / "app.py", *sorted((ROOT / "app_pages").glob("*.py")), *sorted((ROOT / "autoheal_ui").glob("*.py"))]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHEAL_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    return at


def _text(at) -> str:
    """Rendered text, without the stylesheet (whose selectors are not user-facing)."""
    parts = []
    for kind in ("markdown", "caption", "subheader", "warning", "error", "success", "info"):  # all text elements
        parts += [str(e.value) for e in getattr(at, kind)]
    parts += [str(e.proto.body) for e in at.get("html") if "<style>" not in str(e.proto.body)]
    return "\n".join(parts)


def _wait_and_refresh(at, tmp_path):
    run = recovery.current(str(tmp_path / "console.db"))
    assert run is not None and run.finished.wait(60)
    assert run.error is None
    at.run()
    assert not at.exception
    return run


def _assert_clean_and_idle(at) -> None:
    """Healthy and idle: no incident of any kind, recorded or otherwise."""
    assert not at.exception
    text = _text(at)
    assert all(t in text for t in ("System condition", "Healthy", "Active incident", "No fault injected", "No active incident"))
    for shown in ("Recorded", "(recorded)", "Recent", "Replayed", "Fully recovered", "Record of incident", "Incident details"):
        assert shown not in text, shown
    assert "ah-banner" not in text  # no outcome banner
    assert text.count('class="ah-step pending"') == 6 and "Resolve / Escalate" in text  # the workflow has not started
    assert [e.label for e in at.get("expander")] == ["Additional controls"]  # no activity events
    assert not at.tabs
    assert at.button(key="start").disabled and not at.button(key="inject").disabled
    assert at.session_state.console.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_console_starts_on_operations_clean_and_idle(app):
    text = _text(app)
    assert "Simulation Environment" in text
    assert "AutoHeal Operations Console" in text
    _assert_clean_and_idle(app)


def test_startup_stays_clean_across_reruns_and_page_changes(app):
    app.run()
    for page in (SAFETY, "app_pages/insights.py", OPERATIONS):
        app.switch_page(page).run()
        assert not app.exception
    _assert_clean_and_idle(app)


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_when_clean(app, page):
    app.switch_page(page).run()
    assert not app.exception


def test_llm_mode_is_reported_as_not_configured_and_playbook_is_the_default(app):
    assert app.selectbox(key="engine_widget").value == "playbook"
    assert "not configured" not in _text(app)
    app.selectbox(key="engine_widget").select("openrouter").run()
    assert "LLM recovery is not configured" in _text(app)
    app.button(key="inject").click().run()
    assert app.button(key="start").disabled  # no call can be attempted


def test_inject_then_recover_with_the_playbook(app, tmp_path):
    app.selectbox(key="scenario_widget").select("payment_timeout").run()
    app.button(key="inject").click().run()
    assert not app.exception
    text = _text(app)
    assert "Awaiting recovery" in text
    assert any(str(e.label).startswith("Anomaly detected") for e in app.get("expander"))
    assert not app.button(key="start").disabled

    app.button(key="start").click().run()
    assert not app.exception
    _wait_and_refresh(app, tmp_path)
    text = _text(app)
    assert "Fully recovered" in text and "Healthy" in text
    labels = [e.label for e in app.get("expander")]
    assert "Recovery action executed: Roll back configuration" in labels
    assert "Independent verification passed" in labels
    assert "Recovery verified" in labels
    assert not any("Model selected" in str(l) for l in labels)
    # Root cause, recovery action and verification lead; everything else stays one tab away.
    assert [t.label for t in app.tabs][:5] == ["Summary", "Symptoms", "Evidence", "Action history", "Escalation"]
    assert all(h in text for h in ("Root cause", "Recovery action", "Verification result"))
    assert "Roll back configuration" in text and "probe orders fulfilled" in text


def test_reset_returns_to_the_same_state_as_a_fresh_start(app, tmp_path):
    app.button(key="inject").click().run()
    app.button(key="start").click().run()
    _wait_and_refresh(app, tmp_path)
    app.button(key="reset").click().run()
    _assert_clean_and_idle(app)
    app.run()
    _assert_clean_and_idle(app)
    _assert_clean_and_idle(AppTest.from_file(APP, default_timeout=60).run())  # a new browser session sees the same


def test_reset_also_clears_an_injected_fault(app):
    app.selectbox(key="scenario_widget").select("payment_timeout").run()
    app.button(key="inject").click().run()
    assert "Awaiting recovery" in _text(app)
    app.button(key="reset").click().run()
    _assert_clean_and_idle(app)


def test_reset_also_clears_the_error_of_a_failed_run(tmp_path, monkeypatch):
    def unavailable(*_):
        raise RuntimeError("model service unavailable")

    monkeypatch.setenv("AUTOHEAL_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
    monkeypatch.setattr(recovery, "build_llm", unavailable)
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.selectbox(key="engine_widget").select("openrouter").run()
    at.selectbox(key="scenario_widget").select("payment_timeout").run()
    at.button(key="inject").click().run()
    at.button(key="start").click().run()
    assert recovery.current(str(tmp_path / "console.db")).finished.wait(60)
    at.run()
    assert "model service unavailable" in _text(at)
    at.button(key="reset").click().run()
    _assert_clean_and_idle(at)


def test_injected_fault_shows_its_real_symptoms_and_advances_the_workflow(app):
    app.selectbox(key="scenario_widget").select("payment_timeout").run()
    app.button(key="inject").click().run()
    assert not app.exception
    text = _text(app)
    assert "Awaiting recovery" in text and "1 active symptom" in text and "No active incident" not in text
    assert 'class="ah-step completed"' in text and "Orders stuck before payment" in text  # Detect, from the live checks
    assert "Anomaly detected: 1 symptom" in [e.label for e in app.get("expander")]
    assert "ah-banner" not in text  # nothing has finished yet
    assert not app.button(key="start").disabled


def test_completed_incident_stays_until_reset_or_a_new_fault(app, tmp_path):
    app.selectbox(key="scenario_widget").select("payment_timeout").run()
    app.button(key="inject").click().run()
    app.button(key="start").click().run()
    _wait_and_refresh(app, tmp_path)
    for keep in (lambda: app.run(), lambda: app.switch_page(SAFETY).run(), lambda: app.switch_page(OPERATIONS).run(),
                 lambda: app.selectbox(key="scenario_widget").select("queue_capacity").run()):
        keep()  # choosing a scenario is not starting it
        assert not app.exception
    text = _text(app)
    assert "Fully recovered" in text and "Record of incident #1" in text
    assert "Recovery verified" in [e.label for e in app.get("expander")]

    app.pills(key="ops-filter").set_value("Verify").run()  # a filter on the finished run must not hide the next incident
    app.button(key="inject").click().run()
    assert not app.exception
    text = _text(app)
    assert "Fully recovered" not in text and "Record of incident" not in text and "Awaiting recovery" in text
    labels = [str(e.label) for e in app.get("expander")]
    assert any(l.startswith("Anomaly detected") for l in labels) and "Recovery verified" not in labels


def test_generate_traffic_advances_the_simulation(app):
    before = app.session_state.console.shop.tick_now
    app.button(key="traffic").click().run()
    assert not app.exception
    assert app.session_state.console.shop.tick_now == before + 3


@pytest.mark.parametrize("scenario,label", [
    ("gateway_outage", "Escalated without automatic recovery"), ("combo_payment_queue", "Verification failed"),
    ("healthy", "No action required")])
def test_each_outcome_is_named_for_what_happened(app, tmp_path, scenario, label):
    app.selectbox(key="scenario_widget").select(scenario).run()
    app.button(key="inject").click().run()
    app.button(key="start").click().run()
    _wait_and_refresh(app, tmp_path)
    assert label in _text(app)
    if scenario == "healthy":
        assert "No fault to diagnose" in _text(app)
    if scenario == "gateway_outage":
        assert "Handed to a human" in _text(app)  # the summary points to the escalation record
        assert "Still open when the incident was handed over" in _text(app)  # which is preserved in its tab


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_with_an_active_incident(app, page, tmp_path):
    app.selectbox(key="scenario_widget").select("gateway_outage").run()
    app.button(key="inject").click().run()
    app.button(key="start").click().run()
    _wait_and_refresh(app, tmp_path)
    app.switch_page(page).run()
    assert not app.exception


def test_ai_agent_recovery_updates_live_and_renders_its_outcome(tmp_path, monkeypatch):
    """The AI Agent path through the console, with a scripted model standing in for the metered one: no API call."""
    def call(name, args, cid="c1"):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    def turn(*calls):
        return {"role": "assistant", "content": None, "tool_calls": list(calls)}

    release = threading.Event()

    class ScriptedModel:
        turns = [turn(call("get_config_history", {})), turn(call("rollback_config", {"key": "payment.gateway_timeout_ms"})),
                 turn(call("retry_stuck_payments", {})),
                 turn(call("finish_incident", {"root_causes": [{"category": "payment", "cause": "scripted cause",
                                                                "evidence": ["scripted evidence"], "fix_applied": "scripted fix"}],
                                               "summary": "scripted summary", "confidence": 0.9}))]

        def __init__(self):
            self.turns = list(type(self).turns)

        def chat(self, messages, tools, model, purpose="", incident_id=0):
            if len(self.turns) < 4:  # hold the run open after its first read
                assert release.wait(60)
            return self.turns.pop(0)

    monkeypatch.setenv("AUTOHEAL_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
    monkeypatch.setattr(recovery, "build_llm", lambda kind, conn, settings, ledger: ScriptedModel())
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.selectbox(key="engine_widget").select("openrouter").run()
    at.selectbox(key="scenario_widget").select("payment_timeout").run()
    at.button(key="inject").click().run()
    at.button(key="start").click().run()
    assert not at.exception

    run = recovery.current(str(tmp_path / "console.db"))
    for _ in range(100):  # the run has reached its held turn
        if any(e.title == "Examining configuration history" for e in run.log.events):
            break
        run.finished.wait(0.1)
    at.run()
    assert not at.exception and run.active
    assert "Recovery in progress" in _text(at)  # live state, with the controls locked
    assert at.button(key="inject").disabled and at.button(key="start").disabled
    assert "Examining configuration history" in [e.label for e in at.get("expander")]

    release.set()
    _wait_and_refresh(at, tmp_path)
    text = _text(at)
    assert "Fully recovered" in text and "scripted cause" in text and "probe orders fulfilled" in text
    labels = [e.label for e in at.get("expander")]
    assert "Recovery action executed: Roll back configuration" in labels and "Independent verification passed" in labels
    assert "scripted evidence" in text  # kept under Evidence


def _set_on_safety_page(app, change) -> None:
    app.switch_page(SAFETY).run()
    change(app)
    assert not app.exception
    app.switch_page(OPERATIONS).run()
    assert not app.exception


def test_low_risk_threshold_files_an_approval_that_a_human_can_decide(app, tmp_path):
    _set_on_safety_page(app, lambda a: a.selectbox(key="risk_widget").select("low").run())
    assert "low only" in _text(app)  # a changed limit stays visible where recovery is started
    app.selectbox(key="scenario_widget").select("payment_timeout").run()
    app.button(key="inject").click().run()
    app.button(key="start").click().run()
    _wait_and_refresh(app, tmp_path)
    assert "Human approval requested" in " ".join(str(e.label) for e in app.get("expander"))
    assert "Human attention needed" in _text(app)  # a pending request cannot be missed on Operations

    app.switch_page(SAFETY).run()
    assert not app.exception
    approve = [b for b in app.button if b.key.startswith("approve-")]
    assert approve
    approve[0].click().run()
    assert not app.exception
    statuses = [r[0] for r in app.session_state.console.conn.execute("SELECT status FROM approvals")]
    assert statuses.count("approved") == 1


def test_settings_survive_leaving_the_page(app):
    _set_on_safety_page(app, lambda a: a.selectbox(key="risk_widget").select("high").run())
    assert app.session_state.console.settings.max_auto_risk == "high"
    app.switch_page(SAFETY).run()
    assert app.selectbox(key="risk_widget").value == "high"
    assert app.session_state.console.settings.max_auto_risk == "high"


def test_dry_run_setting_reaches_the_runtime(app, tmp_path):
    _set_on_safety_page(app, lambda a: a.toggle(key="dry_run_widget").set_value(True).run())
    assert "Dry run is on" in _text(app)
    app.button(key="inject").click().run()
    app.button(key="start").click().run()
    _wait_and_refresh(app, tmp_path)
    assert any(str(e.label).startswith("Proposed only") for e in app.get("expander"))


def test_console_has_exactly_three_pages_and_no_evaluation_content():
    entry = (ROOT / "app.py").read_text(encoding="utf-8")
    assert entry.count("st.Page(") == 3
    assert sorted(p.name for p in (ROOT / "app_pages").glob("*.py")) == ["insights.py", "operations.py", "safety_audit.py"]
    for path in (ROOT / "app.py", *(ROOT / "app_pages").glob("*.py"), ROOT / "autoheal_ui" / "panels.py"):
        text = path.read_text(encoding="utf-8")
        assert "evaluation.py" not in text and "evaluation_data" not in text and "Suite " not in text, path


def test_operations_keeps_low_priority_details_out_of_the_main_view(app):
    labels = _text(app)
    assert "Simulation tick" not in labels and "Simulation Environment" in labels
    secondary = ("Stuck orders", "Messages queued", "Dead letters", "Workflows waiting")
    assert not any(m in labels for m in secondary)
    assert all(t in labels for t in ("System condition", "Active incident", "Recovery engine"))
    assert "Additional controls" in [e.label for e in app.get("expander")]
    assert app.button(key="traffic").label == "Generate Test Traffic"
    for copy in SCENARIO_COPY.values():  # described by what is visible, before any investigation
        words = f"{copy.title} {copy.symptom}".lower()
        for hint in ("deploy", "revert", "roll back", "rollback", "timeout", "shrink", "capacity", "mapping", "truncat",
                     "lock", "heartbeat", "outage", "provider", "503", "recoverable", "escalat", "retry", "replay", "resync"):
            assert hint not in words, (copy.title, hint)
    app.button(key="traffic").click().run()
    app.switch_page("app_pages/insights.py").run()
    labels = _text(app)
    assert "Simulation tick" in labels  # moved, not removed
    assert all(t in labels for t in ("Stuck orders", "Queue depth", "Dead letters", "Workflows waiting"))
    assert "Health checks" in labels and "Recent logs" in labels
    expanders = [e.label for e in app.get("expander")]
    for section in ("Metric charts", "All logs", "Configuration history", "Current configuration", "Raw metric data"):
        assert section in expanders  # reachable, not removed
    app.switch_page(SAFETY).run()
    assert {"How each guardrail works", "Tool policy"} <= {e.label for e in app.get("expander")}
    assert {"Human approvals", "Guardrails", "Action record", "Session Usage", "Advanced settings"} == {t.label for t in app.tabs}


def test_session_usage_does_not_mention_historical_evaluation_costs(app):
    app.switch_page(SAFETY).run()
    text = _text(app)
    assert "Session" in " ".join(t.label for t in app.tabs)
    assert "evaluation" not in text.lower() and "historical" not in text.lower()


def test_a_fake_key_is_never_displayed(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHEAL_DB", str(tmp_path / "console.db"))
    secret = "sk-or-v1-THIS-MUST-NOT-APPEAR"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    at = AppTest.from_file(APP, default_timeout=60).run()
    for page in PAGES:
        at.switch_page(page).run()
        assert not at.exception
        assert secret not in _text(at)
        assert secret not in str([e.proto for e in at.get("arrow_data_frame")])
    assert at.session_state.console.llm_configured


# ------------------------------------------------------------------ wording
BANNED = [r"\bchaos\b", r"agent brain", r"\bbrain\b", r"Run AutoHeal", r"\bSpend\b", r"💥", r"🩺"]


@pytest.mark.parametrize("path", UI_SOURCES, ids=lambda p: p.name)
def test_no_development_terms_or_emojis_in_ui_source(path):
    text = path.read_text(encoding="utf-8")
    for pattern in BANNED:
        assert not re.search(pattern, text, re.I), pattern
    # Emoji only: the stepper's plain text marks (check, times, bullet) are not emoji.
    assert not re.search("[\U0001F300-\U0001FAFF☀-⛿✅❌✨]", text)


def test_ui_package_does_not_touch_the_frozen_runtime():
    """The runtime never imports the console, so nothing here can reach the evaluated decision path."""
    for path in (ROOT / "autoheal").glob("*.py"):
        assert "autoheal_ui" not in path.read_text(encoding="utf-8"), path
    for path in (ROOT / "autoheal_ui").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "SYSTEM_PROMPT" not in text
        assert "requests." not in text
