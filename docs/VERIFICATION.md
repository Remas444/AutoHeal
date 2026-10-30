# Verification and provenance

This repository is a **submission snapshot** of AutoHeal: the final system, its tests, the evaluation harnesses and the
final evaluation evidence. It was exported from the `release/hackathon` branch of the private development repository
(commit `6079665`) without its Git history. The development repository keeps the full history, every tag, all
pre-registration commits and every historical result file, unedited.

## What you can verify from this snapshot alone

- **Final evidence is unchanged.** `tests/test_evidence_integrity.py` checks the SHA-256 of both final result files,
  the final report and the pre-registered plan against the values pinned at release (equal to tag `eval-final`).
- **The system behaves as tested.** Every behavioural test runs: simulator, tools, safety guard, verifier, budget caps,
  agent loop (with scripted LLMs), playbook and the Suite A/B scoring code.
- **The code matches the evaluated version.** Compare the checksums below (`sha256sum`, or `git hash-object` for the
  blob column, after normalising line endings to LF).

Expected `pytest` result here: **264 passed, 29 skipped.** In the development repository: 293 passed.

## Checks that need the private development repository (29 skipped here)

Each skip prints its reason. None of these checks was removed or weakened: in a checkout that contains the original
history and files they run and must pass.

| Test | Count | Needs |
|---|---|---|
| `test_evidence_integrity.py::test_historical_result_file_is_unchanged` | 20 | the historical result files (v1/v2 checkpoints, v2 benchmark and its 402 attempt, H1/H3, H4, U1-U5 v3.1, v3/v3.1 screenings) |
| `test_evidence_integrity.py::test_preregistration_and_result_commits_still_exist` | 6 | the pre-registration commits `2fbde0e`, `2e74c88`, `bc1806a`, `4413a68`, `b8f7821`, `99b4b99` |
| `test_evidence_integrity.py::test_final_evaluation_evidence_is_unchanged[v32-u2-801-validation.json]` | 1 | the v3.2 U2 seed 801 development run (the other four final-evidence checks run here) |
| `test_prospective.py::test_production_code_is_identical_to_the_v321_freeze` | 1 | the freeze commit `b999d37` (`git diff`); the checksums below give the same assurance by hash |
| `test_v32_hardening.py::test_playbook_results_unchanged_except_u2` | 1 | `v31-screening-runs1.json`, the v3.1 playbook results it replays against |

The only differences from the development test files are these explicit skips: a historical file is missing, or the
freeze commit `b999d37` is not in the checkout's history.

Also only provable from the development history: that each plan was committed before its results (pre-registration
order). The Suite B harness records `production_identical_to_freeze: false` when run outside that history, because it
cannot reach the freeze commit.

## Differences from the evaluated commits

- Production modules (the eleven below): **none**; byte-identical to `v3.2.1`.
- `autoheal/evaluate.py` (7-scenario benchmark harness, not used by Suite A/B): one unused import removed at release.
- `app.py`: the dashboard's Evaluation tab gained a read-only view of the two final result files.
- Documentation: README, evaluation index, notes and this file were written or updated for the submission. The final
  report and the plan are unchanged; the arithmetic erratum (historical v3.1 total 5/12, not 7/12) is recorded in
  [EVALUATION_NOTES.md](EVALUATION_NOTES.md) rather than edited into the report.

## Checksums

**Final evaluation evidence (pinned in `tests/test_evidence_integrity.py`)**

| File | SHA-256 (LF line endings) | Git blob |
|---|---|---|
| `data/eval/v321-suiteA-regression.json` | `01f59c7cc75244026bd2e54aff630d43bf38f287d4a3d06a86517bfede91a099` | `b5f2c99b37da` |
| `data/eval/v321-suiteB-prospective.json` | `cb5403e658de2d951a91dd83bfe4d133dc274018c36a4aa56f457a3f330453b1` | `a018c0d01753` |
| `docs/FINAL_EVALUATION_v321.md` | `b5478cdd79e035dc0e9e21020307d488ef02bf77b44d310f2957c4a1302cbd3e` | `aa001339a564` |
| `docs/EVALUATION_PLAN_v321.md` | `ea1e6b2c0b0aebeb63a5bbaf172c3fd9c7a587dc0babafd643674ca2fec46360` | `a7f6bd21792e` |

**Frozen production modules (`prospective.PRODUCTION_FILES`, identical to `v3.2.1` = `b999d37`)**

| File | SHA-256 (LF line endings) | Git blob |
|---|---|---|
| `autoheal/agent.py` | `ffaca75a80c3b748cfb54919308d3de2e55f6ec8eab1d360d3335e83a7f0c8c8` | `f43df5cd71ae` |
| `autoheal/tools.py` | `978702bfd6f24ffa5807cc75a020da0560e5ebabb582bb45d54b28705b324d14` | `d3def02e3526` |
| `autoheal/safety.py` | `a51c75a02af1612a821a0ade979be7ba8f1dba79fe1c42bad58a066afcd64a82` | `3fb32b5e874b` |
| `autoheal/detector.py` | `1f915f3d6ff6c3620db654f45c26eeceaac4a6a9c20de49e7b2570eee5d3a71f` | `3bc925f7d364` |
| `autoheal/verifier.py` | `3cbf6beafbcd709160e9d15199fdc7e9f5b9009663df2d04cd3d24c6d905b58c` | `c9672a729f3e` |
| `autoheal/playbook.py` | `25ff4cd445b43831a2b12fc504d089f9dea0cc7361271aac9457b05983f41a5b` | `7390429a263c` |
| `autoheal/llm.py` | `f76ffb2728c69e81386afae4ceae08eca4c75ec1fba943709526408d1154b4b7` | `5188264ecc7a` |
| `autoheal/config.py` | `9e9544a120f6711a76ac9d18f9b1a600de1fa41ad363896e38c0fdb3d12101b0` | `e931f0c63db3` |
| `autoheal/simulator.py` | `8e178cc8cf91fbfb34f93ce9642dac8b46bd492840aaf779f593f67306bafb3d` | `bff42fa95168` |
| `autoheal/runtime.py` | `2b970fd954113d1602bc4aac9c03a67c67d8889f6b3a7b3aba56e06b0a563013` | `5d0c4a9324c0` |
| `autoheal/scenarios.py` | `6233d69ae7ff280090b9c4bcae6705263d837e7dfc1d4f9b7ae9eb7ce96c3805` | `ad750b2fa674` |

**Evaluation harnesses and scoring (identical to `eval-final` = `b441b41`)**

| File | SHA-256 (LF line endings) | Git blob |
|---|---|---|
| `autoheal/unseen.py` | `9c20f395164a0b57de37c4577699a3f610e40d350b26e71d62df7f380be8a944` | `e432b451e577` |
| `autoheal/prospective.py` | `bdc1492a5a02cebe94a7646d3f69f793f2c2bb6ef90366b4b71d620d25fc7d5e` | `1e7021ca6788` |
| `autoheal/holdout.py` | `20236b7eded8beea054c7a2e4ed50a2d48757b1de6bc5bcc4393278ad181d349` | `45fb7bfe1eb8` |
