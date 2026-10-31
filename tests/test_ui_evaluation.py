"""Figures computed from the committed result files, which the evidence site publishes, must agree with the final report."""
from pathlib import Path

import pytest

from autoheal_ui.content import OUTCOME_LABELS, outcome_label
from autoheal_ui.evaluation_data import AGENT, PLAYBOOK, load_suite, wilson_interval

ROOT = Path(__file__).resolve().parent.parent
REPORT = (ROOT / "docs" / "FINAL_EVALUATION_v321.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def a():
    return load_suite("A")


@pytest.fixture(scope="module")
def b():
    return load_suite("B")


def test_headline_results(a, b):
    assert a.correct(AGENT) == (12, 12) and a.correct(PLAYBOOK) == (8, 12)
    assert b.correct(AGENT) == (18, 20) and b.correct(PLAYBOOK) == (12, 20)


def test_detection_and_false_positives(b):
    assert b.detection(AGENT) == (14, 16) and b.detection(PLAYBOOK) == (14, 16)
    assert b.false_positives(AGENT) == (0, 4) and b.false_positives(PLAYBOOK) == (0, 4)


def test_agent_lead_is_only_in_the_declared_cases(a, b):
    agent_only, playbook_only = b.discordant()
    assert sorted({r["case"] for r in agent_only}) == ["P04", "P05", "P07"]
    assert len(agent_only) == 6 and playbook_only == []
    cases = {r.case: r for r in b.cases()}
    assert all(cases[c].leaning == "favours agent" for c in ("P04", "P05", "P07"))
    assert cases["P06"].leaning == "favours playbook"
    assert sorted({r["case"] for r in a.discordant()[0]}) == ["U1", "U2"]


def test_shared_p02_failure(b):
    p02 = next(r for r in b.cases() if r.case == "P02")
    assert (p02.agent, p02.playbook) == (0, 0)
    assert p02.agent_outcome == p02.playbook_outcome == "Not detected"


def test_latency_and_cost_match_the_report(a, b):
    lat = b.latency(AGENT)
    assert (round(lat.mean_s), round(lat.min_s), round(lat.max_s)) == (91, 28, 172)
    assert b.latency(PLAYBOOK).max_s < 0.1
    assert f"{b.cost(AGENT):.4f}" == "0.7925" and f"{a.cost(AGENT):.4f}" == "0.7167"
    assert f"{b.cost_per_correct(AGENT):.4f}" == "0.0440"
    assert b.cost(PLAYBOOK) == 0 and a.cost(PLAYBOOK) == 0
    assert "$0.7925 total; $0.0440 per success" in REPORT


def test_confidence_intervals_and_paired_tests_match_the_report(a, b):
    lo, hi = wilson_interval(18, 20)
    assert (round(lo * 100), round(hi * 100)) == (70, 97)
    lo, hi = wilson_interval(12, 20)
    assert (round(lo * 100), round(hi * 100)) == (39, 78)
    assert f"{b.mcnemar_p():.3f}" == "0.031" and f"{a.mcnemar_p():.3f}" == "0.125"


def test_safety_counts_match_the_report(a, b):
    assert b.unsafe_executed(AGENT) == 0 and b.unsafe_executed(PLAYBOOK) == 0
    assert b.unsafe_attempted(AGENT) == 0 and b.unsafe_attempted(PLAYBOOK) == 2
    assert a.unsafe_executed(AGENT) == 0 and a.unsafe_executed(PLAYBOOK) == 0


def test_suites_are_never_merged(a, b):
    assert not {r["case"] for r in a.results} & {r["case"] for r in b.results}


def test_every_recorded_outcome_has_a_professional_label(a, b):
    for suite in (a, b):
        for r in suite.results:
            assert r["outcome"] in OUTCOME_LABELS
            assert "_" not in outcome_label(r["outcome"])
    assert outcome_label("correct_noop") == "No action required"
    assert outcome_label("guardrail_stop") == "Stopped by safety controls"

