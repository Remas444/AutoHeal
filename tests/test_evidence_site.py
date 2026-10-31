"""The Evaluation & Evidence site: its data must match the committed results exactly, and its wording must stay honest."""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from autoheal import prospective, unseen
from autoheal_ui.evaluation_data import AGENT, PLAYBOOK, SUITE_FILES, load_suite
from autoheal_ui.evidence_export import CASES, OUTPUT, PREFIX, build, main, render

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "evidence-site"
DOC = (ROOT / "docs" / "VERIFICATION.md").read_text(encoding="utf-8")
SOURCES = [SITE / "index.html", SITE / "app.js", SITE / "styles.css"]
INTERNAL_ID = re.compile(r"\b[PU]\d{1,2}\b|\bSuite [AB]\b|\bv\d\.\d|\bseed\b|\w_\w", re.I)


@pytest.fixture(scope="module")
def data():
    return build()


@pytest.fixture(scope="module")
def groups(data):
    return {g["key"]: g for g in data["groups"]}


@pytest.fixture(scope="module")
def published():
    text = OUTPUT.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    assert text.startswith(PREFIX)
    return text


def _all_runs(data):
    return [(g, r) for g in data["groups"] for r in g["runs"]]


# ---------------------------------------------------------------- data integrity
def test_published_file_is_exactly_what_the_export_produces(data, published):
    assert published == render(data)
    assert main(["--check"]) == 0


def test_export_is_deterministic(data):
    assert render(build()) == render(data)


def test_export_uses_the_pinned_result_files(groups):
    for group in groups.values():
        assert f"`{group['source']['file']}` | `{group['source']['sha256']}`" in DOC


@pytest.mark.parametrize("key,suite_key", [("prospective", "B"), ("development", "A")])
def test_every_record_is_embedded_unchanged_and_in_order(groups, key, suite_key):
    source = json.loads((ROOT / "data" / "eval" / SUITE_FILES[suite_key]).read_text(encoding="utf-8"))["results"]
    runs = groups[key]["runs"]
    assert len(runs) == len(source)
    for i, (run, record) in enumerate(zip(runs, source)):
        assert run["raw"] == record and run["source_index"] == i
        assert run["case"] == record["case"] and run["seed"] == record["seed"]
        assert run["outcome"] == record["outcome"] and run["correct"] == bool(record["success"])
        assert run["cost_usd"] == record["cost_usd"] and run["latency_s"] == record["wall_s"]


def test_recorded_run_numbers_follow_the_registered_order(groups):
    source = json.loads((ROOT / "data" / "eval" / SUITE_FILES["B"]).read_text(encoding="utf-8"))
    seeds = {}
    for case, seed in source["runs"]:
        seeds.setdefault(case, []).append(seed)
    for run in groups["prospective"]["runs"]:
        assert seeds[run["case"]][run["number"] - 1] == run["seed"]


@pytest.mark.parametrize("key,suite_key", [("prospective", "B"), ("development", "A")])
def test_summaries_match_the_evaluation_helpers(groups, key, suite_key):
    suite = load_suite(suite_key)
    for engine, system in (("agent", AGENT), ("playbook", PLAYBOOK)):
        summary = groups[key][engine]
        assert (summary["correct"], summary["total"]) == suite.correct(system)
        assert summary["cost_usd"] == round(suite.cost(system), 6)
        assert sum(summary["outcomes"].values()) == summary["total"]
    agent_only, playbook_only = suite.discordant()
    assert groups[key]["paired"]["agent_only"] == len(agent_only) and groups[key]["paired"]["playbook_only"] == len(playbook_only)


def test_reference_figures(groups):
    p, d = groups["prospective"], groups["development"]
    assert (p["agent"]["correct"], p["agent"]["total"], p["playbook"]["correct"], p["playbook"]["total"]) == (18, 20, 12, 20)
    assert (d["agent"]["correct"], d["agent"]["total"], d["playbook"]["correct"], d["playbook"]["total"]) == (12, 12, 8, 12)
    assert p["agent"]["detection"] == p["playbook"]["detection"] == (14, 16)
    assert p["agent"]["false_positives"] == p["playbook"]["false_positives"] == (0, 4)
    assert p["agent"]["invalid_tool_calls"] == 7 and p["playbook"]["invalid_tool_calls"] == 0
    assert (p["agent"]["diagnosis"], p["playbook"]["diagnosis"]) == ([14, 14], [8, 14])
    assert (p["agent"]["escalation"], p["playbook"]["escalation"]) == ([4, 4], [0, 4])
    assert (p["agent"]["unsafe_executed"], p["playbook"]["unsafe_executed"], p["playbook"]["unsafe_attempted"]) == (0, 0, 2)
    assert d["agent"]["unsafe_executed"] == d["playbook"]["unsafe_executed"] == 0
    assert p["agent"]["cost_usd"] == pytest.approx(0.7925, abs=5e-5) and p["playbook"]["cost_usd"] == 0
    assert round(p["agent"]["latency"]["mean_s"]) == 91 and p["playbook"]["latency"]["max_s"] < 0.1
    assert p["agent"]["steps"]["mean"] == 7.5
    assert p["agent"]["model_paths"] == {"Cheap model only": 5, "Cheap model, then strong model": 7, "Strong model from the start": 2}
    assert p["paired"]["agent_only_cases"] == ["P04", "P05", "P07"] and p["paired"]["playbook_only"] == 0
    assert p["scoring_amendment_changed"] == 0
    assert all(c["leaning"] == "favours agent" for c in p["cases"] if c["category"] == "difference")
    assert [c["id"] for c in p["cases"] if c["category"] == "shared_failure"] == ["P02"]


def test_development_and_prospective_results_are_never_combined(data, groups):
    assert not {r["case"] for r in groups["prospective"]["runs"]} & {r["case"] for r in groups["development"]["runs"]}
    assert set(data) == {"site", "outcomes", "trace_result_limit", "groups"}  # no cross-group totals anywhere
    script = (SITE / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"GROUPS\.reduce|\.correct\s*\+\s*\w*\.\w*\.?correct", script)


def test_correct_outcomes_are_never_called_full_recovery(data):
    text = " ".join(s.read_text(encoding="utf-8") for s in SOURCES).lower()
    assert "full recoveries" in text  # only ever to say they are not the same
    assert "fully recovered" not in text and "% recovered" not in text
    recovered = {g["key"]: g["agent"]["outcomes"]["recovered"] for g in data["groups"]}
    assert all(recovered[g["key"]] < g["agent"]["correct"] for g in data["groups"])


def test_an_escalation_can_be_correct_and_is_never_shown_in_the_failure_tone(data):
    tones = data["outcomes"]
    assert tones["correct_escalation"]["tone"] == "escalation" and tones["recovered_and_escalated"]["tone"] == "partial"
    for _, run in _all_runs(data):
        if run["outcome"] in ("correct_escalation", "recovered_and_escalated"):
            assert run["correct"] and run["tone"] != "failure"


# ----------------------------------------------------------------- traces
def test_a_missed_fault_is_not_described_as_a_healthy_system(data):
    missed = [r for _, r in _all_runs(data) if r["outcome"] == "detection_failure"]
    assert len(missed) == 4
    for run in missed:
        assert run["fault_present"] and run["lifecycle"][0]["state"] == "failed" and run["lifecycle"][-1]["name"] == "Not detected"
        titles = " ".join(e["title"] for e in run["events"])
        assert "All health checks pass" not in titles and "No action required" not in titles


def test_the_playbook_is_never_shown_as_an_llm(data):
    for _, run in _all_runs(data):
        if run["engine"] == "playbook":
            assert not run["llm"] and run["cost_usd"] == 0 and run["llm_requests"] == 0
            assert all("model" not in e for e in run["events"])
            assert not any(e["title"] in ("Agent note", "Model selected for this incident") for e in run["events"])
            assert not any(e["stage"] == "Investigate" for e in run["events"])


def test_events_only_come_from_the_record(data):
    for _, run in _all_runs(data):
        trace = run["raw"]["trace"]
        reads = sum(t["kind"] == "observe" for t in trace)
        assert sum(e.get("kind") == "observe" for e in run["events"]) == reads
        assert sum(e.get("effect") == "executed" for e in run["events"]) == sum(t["kind"] == "action" for t in trace)
        assert sum(e.get("kind") == "verify" for e in run["events"]) == sum(t["kind"] == "verify" for t in trace)
        if not trace and run["outcome"] != "detection_failure":
            assert [e.get("kind") for e in run["events"]] in (["detect", "done"], ["detect"])


def test_truncated_results_are_marked_and_left_as_stored(data):
    marked = 0
    for _, run in _all_runs(data):
        stored = {t["result"] for t in run["raw"]["trace"] if len(t["result"]) >= data["trace_result_limit"]}
        for event in run["events"]:
            if event.get("truncated"):
                marked += 1
                assert event["result"] in stored
    assert marked > 0


def test_no_event_invents_a_timestamp(data):
    assert all("elapsed_s" not in e for _, r in _all_runs(data) for e in r["events"])


# ---------------------------------------------------------------- terminology
def test_every_scenario_has_a_readable_name_and_keeps_its_original_identifier(groups):
    assert set(CASES) == {c["id"] for g in groups.values() for c in g["cases"]}
    for group in groups.values():
        for case in group["cases"]:
            for field in ("title", "what", "correct", "why", "bias"):
                assert not INTERNAL_ID.search(case[field]), (case["id"], field, case[field])
            assert case["original_title"] and case["id"] not in case["title"]
    assert CASES["P02"]["title"] == "Intermittent Queue Backlog" and CASES["P04"]["title"] == "Missing Paid Orders"
    assert CASES["P05"]["title"] == "Partial Payment Recovery & Escalation"
    assert CASES["P07"]["title"] == "Workflow Failure with Misleading Configuration Evidence"
    assert CASES["U1"]["title"] == "Worker Throughput Degradation"


def test_presentation_matches_the_fault_definitions():
    for cid, case in prospective.CASES.items():
        assert CASES[cid]["requires_escalation"] == (case.kind in ("escalate", "recover_and_escalate")), cid
        assert CASES[cid]["what"].startswith("No fault") == (case.kind == "control"), cid
    for cid, case in unseen.CASES.items():
        assert CASES[cid]["requires_escalation"] == (case.success_outcomes == frozenset({"correct_escalation"})), cid
        assert CASES[cid]["what"].startswith("No fault") == (case.success_outcomes == frozenset({"correct_noop"})), cid
    assert prospective.CASES["P07"].required_config == {"sync.batch_size": "10"}  # the approved change must stay


def test_group_and_outcome_names_are_readable(data):
    assert [g["name"] for g in data["groups"]] == ["Prospective Scenario Evaluation", "Development & Regression Tests"]
    for group in data["groups"]:
        assert not INTERNAL_ID.search(group["name"] + group["summary"] + group["role"])
    for outcome in data["outcomes"].values():
        assert "_" not in outcome["label"] and not INTERNAL_ID.search(outcome["meaning"])
    assert data["outcomes"]["correct_noop"]["label"] == "No action required"
    assert data["outcomes"]["guardrail_stop"]["label"] == "Stopped by safety controls"
    for _, run in _all_runs(data):
        assert "_" not in run["outcome_label"]
        for row in run["scoring"]:
            assert not INTERNAL_ID.search(row["label"])


def test_static_page_shows_no_internal_names_or_result_figures():
    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts, self.skip = [], 0

        def handle_starttag(self, tag, attrs):
            self.skip += tag in ("script", "style", "code")

        def handle_endtag(self, tag):
            self.skip -= tag in ("script", "style", "code")

        def handle_data(self, text):
            if not self.skip:
                self.parts.append(text)

    parser = Text()
    html = (SITE / "index.html").read_text(encoding="utf-8")
    parser.feed(html)
    text = " ".join(parser.parts)
    assert not re.search(r"\b[PU]\d{1,2}\b|\bSuite [AB]\b|\bv\d\.\d", text)
    for source in (SITE / "index.html", SITE / "app.js"):
        body = source.read_text(encoding="utf-8")
        for figure in ("12/12", "8/12", "18/20", "12/20", "14/16", "0.7925", "0.7167"):
            assert figure not in body, (source.name, figure)
        assert not re.search(r"\b90%|\b60%", body)


def test_required_disclosures_are_on_the_page():
    html = re.sub(r"<[^>]+>", "", (SITE / "index.html").read_text(encoding="utf-8"))
    text = re.sub(r"\s+", " ", html + (SITE / "app.js").read_text(encoding="utf-8")).lower()
    for phrase in ("does not show general superiority", "not independently designed real-world unseen failures",
                   "synthetic e-commerce environment", "not trained or fine-tuned", "No claim of statistical significance",
                   "A replay of recorded evidence, not a new execution", "correlated", "stored cut to"):
        assert phrase.lower() in text, phrase


# ------------------------------------------------------------------ navigation
def test_the_site_has_three_views_in_order():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    nav = re.search(r"<nav.*?</nav>", html, re.S).group(0)
    assert re.findall(r'href="#(\w+)"', nav) == ["overview", "comparison", "runs"]
    assert re.findall(r'<section id="(\w+)"', html) == ["overview", "comparison", "runs"]
    assert 'var SECTIONS = ["overview", "comparison", "runs"];' in (SITE / "app.js").read_text(encoding="utf-8")


def test_there_are_no_dropdown_panels_or_secondary_views():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    script = (SITE / "app.js").read_text(encoding="utf-8")
    assert "<details" not in html and '"details"' not in script
    assert "data-tip" not in script and "latency-chart" not in html and "runs-body" not in html


def test_full_evidence_stays_one_link_away_on_github():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for doc in ("docs/FINAL_EVALUATION_v321.md", "docs/EVALUATION_PLAN_v321.md", "docs/EVALUATION_NOTES.md", "docs/VERIFICATION.md",
                *(f"data/eval/{name}" for name in SUITE_FILES.values())):
        assert f"https://github.com/Remas444/AutoHeal/blob/main/{doc}" in html, doc
    script = (SITE / "app.js").read_text(encoding="utf-8")
    assert 'DATA.site.repository + "/blob/main/" + g.source.file' in script
    assert '"Showing " + steps.length + " of "' in script  # the reader is told when events are left out


def _key_steps(run):
    escalated = any(e["kind"] == "escalate" for e in run["events"])
    return [e for e in run["events"] if e["kind"] not in {"observe", "route", "thought", "nudge", "decision", "focus"}
            and not (e["kind"] == "action" and e["stage"] == "Decide")
            and not (e["kind"] == "error" and e["stage"] == "Investigate")
            and not (e["kind"] == "escalate_decision" and escalated)]


def test_key_steps_keep_every_action_refusal_and_outcome(data):
    script = (SITE / "app.js").read_text(encoding="utf-8")
    hidden = set(re.findall(r"(\w+): 1", re.search(r"SUPPORTING_EVENTS = \{(.*?)\}", script).group(1)))
    assert hidden == {"observe", "route", "thought", "nudge", "decision", "focus"}
    for _, run in _all_runs(data):
        steps = _key_steps(run)
        assert steps
        for kept in ("detect", "root_cause", "verify", "escalate", "blocked", "done"):
            assert sum(e["kind"] == kept for e in steps) == sum(e["kind"] == kept for e in run["events"]), kept
        assert sum(e.get("effect") == "executed" for e in steps) == sum(e.get("effect") == "executed" for e in run["events"])
        for i, event in enumerate(run["events"]):
            if event["kind"] == "action" and event["stage"] == "Decide":  # a request is only left out next to its execution
                assert run["events"][i + 1]["kind"] == "action" and run["events"][i + 1]["stage"] == "Execute"


# ------------------------------------------------------------------ deployment and safety
def _links():
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []

        def handle_starttag(self, tag, attrs):
            self.urls += [v for k, v in attrs if k in ("src", "href")]

    parser = Links()
    parser.feed((SITE / "index.html").read_text(encoding="utf-8"))
    return parser.urls


def test_site_paths_are_relative_and_exist():
    urls = _links()
    assert urls
    for url in urls:
        if url.startswith("#"):
            continue
        if url.startswith("https://"):
            assert url.startswith("https://github.com/Remas444/AutoHeal/blob/main/"), url
            assert (ROOT / url.split("/blob/main/")[1]).exists(), url
            continue
        assert not url.startswith("/") and ":" not in url, url
        assert (SITE / url).exists(), url


def test_site_makes_no_requests_and_runs_nothing():
    script = (SITE / "app.js").read_text(encoding="utf-8")
    for banned in ("fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "eval(", "openrouter.ai", "localStorage", "setInterval"):
        assert banned not in script, banned
    assert "http" not in (SITE / "styles.css").read_text(encoding="utf-8")


def test_published_data_holds_nothing_private(published):
    for pattern in (r"sk-or-v1-[A-Za-z0-9]", r"sk-[A-Za-z0-9]{20,}", r"Bearer\s+\S", r"OPENROUTER_API_KEY", r"[A-Z]:\\\\Users", r"/home/\w", r"/Users/\w"):
        assert not re.search(pattern, published), pattern
    assert set(re.findall(r"[\w.+-]+@([\w-]+\.[\w.-]+)", published)) <= {"example.com"}  # the simulator's synthetic customers


def test_only_site_files_are_published():
    workflow = (ROOT / ".github" / "workflows" / "deploy-evidence-site.yml").read_text(encoding="utf-8")
    assert "path: evidence-site\n" in workflow
    assert "secrets." not in workflow and "OPENROUTER" not in workflow
    assert "evidence_export --check" in workflow
    files = sorted(p.relative_to(SITE).as_posix() for p in SITE.rglob("*") if p.is_file())
    assert files == ["app.js", "assets/icon.svg", "assets/logo.svg", "data/evidence.js", "index.html", "styles.css"]


def test_site_has_no_emojis():
    for source in [*SOURCES, OUTPUT.parent.parent / "assets" / "logo.svg"]:
        assert not re.search("[\U0001F300-\U0001FAFF☀-⛿✅❌✨]", source.read_text(encoding="utf-8")), source
