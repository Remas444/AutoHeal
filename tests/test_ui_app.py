"""Headless console tests with Streamlit's AppTest. Offline: an empty key guarantees no LLM call can be made."""
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autoheal_ui import recovery

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


def test_console_starts_on_operations_with_a_guided_empty_state(app):
    text = _text(app)
    assert "Simulation Environment" in text
    assert "AutoHeal Operations Console" in text
    assert "No active incident" in text
    assert "Start here" in text
    assert app.button(key="start").disabled
    assert not app.button(key="inject").disabled


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
    assert "Awaiting recovery" in text and "Next: click Start Recovery" in text
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


def test_reset_returns_to_a_clean_state(app, tmp_path):
    app.button(key="inject").click().run()
    app.button(key="start").click().run()
    _wait_and_refresh(app, tmp_path)
    app.button(key="reset").click().run()
    assert not app.exception
    text = _text(app)
    assert "No active incident" in text and "Start here" in text
    assert app.button(key="start").disabled


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


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_with_an_active_incident(app, page, tmp_path):
    app.selectbox(key="scenario_widget").select("gateway_outage").run()
    app.button(key="inject").click().run()
    app.button(key="start").click().run()
    _wait_and_refresh(app, tmp_path)
    app.switch_page(page).run()
    assert not app.exception


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
    app.switch_page("app_pages/insights.py").run()
    assert "Simulation tick" in _text(app)  # moved, not removed


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
