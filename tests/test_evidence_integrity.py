"""Historical evaluation evidence must never change silently; new reports must label playbook bookkeeping and
development evidence correctly (offline, $0)."""
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from autoheal import holdout, prospective, unseen
from autoheal.llm import LLMError

EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"
# SHA-256 of committed result files, line endings normalised to LF (pinned at v3.2; see docs/EVALUATION_NOTES.md).
PINNED = {
    "final-report-infrastructure-failed.md": "c433c75b615294fc32e018ad9c25e09aeef7c8f2cfc39721fe51faaf9bbb3626",
    "final-report.md": "d8f226c627aad112120843da5b13ceb03e5cdf356ded647edfef48584f92ab40",
    "final-runs3-infrastructure-failed.csv": "8219035a83da69a6d5cdb8dce245ef1a567fa0fb5673e683b81c04a459b73c43",
    "final-runs3-infrastructure-failed.json": "992da001b4d2657d6dd118b4d2fb4648539cd0bccddfbb92ec46330dc1b4e15a",
    "final-runs3.csv": "ce4aa55b1752d43e2ed7f7b84130c4a429098ef10ed667b4029653d555b45da9",
    "final-runs3.json": "1b429d3807504321ca3918609f263f7e432e555095fa12a9787501d737300501",
    "h4-results.json": "c20a9bbf7d4577a6285ff8b54d2cd1cf0e574758d6ac0d911ddb2dd4ad930eca",
    "holdout-h1-h3.json": "ee7342c7331fcb4d4b8f24fd32ea1b464606c84812df1532f4cecc77870ccda9",
    "holdout-report.md": "0ca1a99d2b50b66f23183b1efddf0322f24dd994d279a117b789204352d03680",
    "unseen-u1-u5-results.json": "0cae8bb55b57f563996af24b392c662f4bc6205205bf773eb629ed8d3f161029",
    "v1-checkpoint.csv": "50be0a6f5bdcbceec069a9c20d702aec2895d8305dfa4adcfea1a3c9a0f45c1c",
    "v1-checkpoint.json": "f0736948b2881af74fd7571ad822a28efa5f9fda421825beb0d620416dbe37b7",
    "v2-checkpoint.csv": "e00db9ce63ddb975e4286900eb6a2fd8c2adac9406d5126ee17d4214fb9b305e",
    "v2-checkpoint.json": "8023acf3c6300299c27545c68cc8c18f5b8ff19cb71d45f9003971f19beaaada",
    "v3-phase1-h1-seed202.json": "55a69f3e238b9fff8803847fd060ce8f8cee61882d140b7803105173ff3c861a",
    "v3-screening-runs1.csv": "d449673ff11e02712fad2daa1810cf544b66ea3a1444141d7f0dc7251bc02cf4",
    "v3-screening-runs1.json": "858d43a9497c8a66d4d6ab22a4a3351df4a4be11728ea27be9c5f4c8013ea318",
    "v31-h1h3-regression.json": "aa21cbd05df9606ec5d108a2b178a00cb2e1bfc3cdbdfaa819d1aa834cd94f69",
    "v31-screening-runs1.csv": "bc6398eea0653410fe77f1213d9d8a60b469dcfbedad59a42df637600ac6ce2b",
    "v31-screening-runs1.json": "3f7eadbc744533debfb2bdb9f9af5813a1b0ca87b0f4e4ab1f21d80b281ff939",
}
PREREGISTRATION_COMMITS = {"2fbde0e": "U1-U4", "2e74c88": "U5", "bc1806a": "U1-U5 execution order",
                           "4413a68": "U1-U5 results", "b8f7821": "H1/H3", "99b4b99": "H4"}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_historical_result_file_is_unchanged(name):
    if not (EVAL / name).exists():
        pytest.skip("historical result file not in the public submission snapshot; verified in the private development repository (docs/VERIFICATION.md)")
    data = (EVAL / name).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == PINNED[name], f"{name} changed - historical evidence must not be edited"


def has_development_history() -> bool:
    """True only in a checkout that contains the frozen v3.2.1 commit (the original development history)."""
    out = subprocess.run(["git", "cat-file", "-e", f"{prospective.FREEZE_COMMIT}^{{commit}}"],
                         capture_output=True, cwd=EVAL.parent.parent)
    return out.returncode == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
@pytest.mark.parametrize("commit", sorted(PREREGISTRATION_COMMITS))
def test_preregistration_and_result_commits_still_exist(commit):
    if not has_development_history():
        pytest.skip("pre-registration commits are not in the public submission snapshot; verified in the private development repository (docs/VERIFICATION.md)")
    out = subprocess.run(["git", "cat-file", "-t", commit], capture_output=True, text=True,
                         cwd=EVAL.parent.parent)
    if out.returncode != 0 and "Not a git repository" in out.stderr:
        pytest.skip("not a git checkout")
    assert out.stdout.strip() == "commit", PREREGISTRATION_COMMITS[commit]


class _Fake:
    def __init__(self, ledger):
        self.ledger = ledger

    def chat(self, messages, tools, model, purpose="", incident_id=0):
        raise LLMError("OpenRouter HTTP 402: test")  # stops paid runs immediately; no spend


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHEAL_DB", str(tmp_path / "autoheal.db"))
    monkeypatch.setattr(unseen, "OUT_DIR", tmp_path / "eval")
    monkeypatch.setattr(holdout, "OUT_DIR", tmp_path / "eval")
    return monkeypatch


def test_unseen_reports_label_development_evidence_and_playbook_bookkeeping(isolated):
    rep = unseen.run(["playbook"], 0.40, progress=lambda line: None)
    assert rep["evidence_type"].startswith("development/regression") and "U1-U5" in rep["label"]
    for r in rep["results"]:
        assert r["llm_called"] is False and r["routing_is_bookkeeping"] is True
    u4 = [r for r in rep["results"] if r["case"] == "U4"]
    assert all(r["diagnosis_correct"] is None and r["config_restored"] is None for r in u4)  # not invoked


def test_holdout_reports_label_development_evidence_and_no_playbook_model_upgrades(isolated):
    rep = holdout.run(["playbook"], 0.10, progress=lambda line: None, holdouts=["H4-60"])
    assert rep["evidence_type"].startswith("development/regression")
    (row,) = rep["results"]
    assert row["llm_called"] is False and row["model_upgrades"] == 0 and "executed_mutations" in row


def test_llm_called_reflects_actual_api_requests(isolated):
    isolated.setattr(unseen, "build_llm", lambda kind, conn, s, led: _Fake(led))
    rep = unseen.run(["openrouter"], 0.40, progress=lambda line: None, runs=[("U4", 951), ("U5", 1001)])
    u4, u5 = rep["results"]
    assert u4["llm_called"] is False  # detector silent: no request
    assert u5["outcome"] == "infrastructure_failure" and u5["llm_called"] is False  # the request never succeeded


def test_new_unseen_reports_describe_the_current_agent_not_v31(isolated):
    rep = unseen.run(["playbook"], 0.40, progress=lambda line: None, runs=[("U2", 801)])
    assert rep["agent_version"] == "v3.2" and "Agent v3.2" in rep["note"] and "v3.1" not in rep["note"]
    assert "development/regression" in rep["note"] and [(r["case"], r["seed"]) for r in rep["results"]] == [("U2", 801)]


def test_only_accepts_pre_registered_runs_only():
    assert unseen.parse_only(["U2:801"]) == [("U2", 801)]
    for bad in (["U2:999"], ["U9:801"], ["U2"]):
        with pytest.raises(SystemExit):
            unseen.parse_only(bad)


# Final v3.2.1 evaluation evidence and its pre-registration (pinned at release cleanup; values equal tag eval-final).
ROOT = EVAL.parent.parent
PINNED_FINAL = {
    "data/eval/v321-suiteA-regression.json": "01f59c7cc75244026bd2e54aff630d43bf38f287d4a3d06a86517bfede91a099",
    "data/eval/v321-suiteB-prospective.json": "cb5403e658de2d951a91dd83bfe4d133dc274018c36a4aa56f457a3f330453b1",
    "data/eval/v32-u2-801-validation.json": "531f2a069fb1c0e9139d2af3fa49f7292ad35b097c2d3e984b3067d97324d164",
    "docs/FINAL_EVALUATION_v321.md": "b5478cdd79e035dc0e9e21020307d488ef02bf77b44d310f2957c4a1302cbd3e",
    "docs/EVALUATION_PLAN_v321.md": "ea1e6b2c0b0aebeb63a5bbaf172c3fd9c7a587dc0babafd643674ca2fec46360",
}

DEVELOPMENT_ONLY = {"data/eval/v32-u2-801-validation.json"}  # the final results, report and plan are never skipped


@pytest.mark.parametrize("path", sorted(PINNED_FINAL))
def test_final_evaluation_evidence_is_unchanged(path):
    if path in DEVELOPMENT_ONLY and not (ROOT / path).exists():
        pytest.skip("development evidence not in the public submission snapshot; verified in the private development repository (docs/VERIFICATION.md)")
    data = (ROOT / path).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == PINNED_FINAL[path], f"{path} changed - final evidence must not be edited"
