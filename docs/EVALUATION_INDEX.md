# Evaluation index

Start here to find AutoHeal's evaluation evidence. This submission contains the **final evaluation only**. Earlier
baselines and development runs are summarised in section 2 so nothing is hidden, but their raw files are kept in the
private development repository (see [VERIFICATION.md](VERIFICATION.md)).

## 1. Final evaluation: AutoHeal v3.2.1 vs rule-based playbook

| What | File |
|---|---|
| **Report** (methodology, per-case results, limitations, conclusion) | [FINAL_EVALUATION_v321.md](FINAL_EVALUATION_v321.md) |
| Pre-registered plan, budgets, scoring amendment B-v2 | [EVALUATION_PLAN_v321.md](EVALUATION_PLAN_v321.md) |
| **Suite A** raw results (U1-U5 regression) | [../data/eval/v321-suiteA-regression.json](../data/eval/v321-suiteA-regression.json) |
| **Suite B** raw results (P01-P10 prospective) | [../data/eval/v321-suiteB-prospective.json](../data/eval/v321-suiteB-prospective.json) |
| Case definitions and scoring code | `autoheal/unseen.py` (Suite A), `autoheal/prospective.py` (Suite B) |
| Audit, evidence types, errata | [EVALUATION_NOTES.md](EVALUATION_NOTES.md) |
| Provenance and checksums | [VERIFICATION.md](VERIFICATION.md) |

Both result files are complete: every run, success and failure, trace, request and cost is kept.

| Suite | Evidence type | Agent | Playbook |
|---|---|---|---|
| **A**: U1-U5, 12 runs | **Development/regression.** v3.2 was designed after these cases were observed. | 12/12 | 8/12 |
| **B**: P01-P10, 20 runs | **Prospective.** Designed after the v3.2.1 freeze, by the same team (designer bias disclosed). | 18/20 correct outcomes | 12/20 |

Suite B's 90% counts **correct outcomes**: recoveries, correct escalations and correct no-ops. It is not a full-recovery
rate. All six cases where only the agent succeeded are P04, P05 and P07, which were declared before running as
favouring the agent. On the neutral and playbook-favouring cases both systems tied 8/8. Both failed P02 (an
intermittent backlog the detector misses). This does not establish general superiority.

Frozen system: commit `b999d37` (tag `v3.2.1` in the development repository). Results and report: commit `b441b41`
(tag `eval-final`).

**Erratum:** the report's Suite A table gives the historical v3.1 total as 7/12 for both systems. The correct value is
**5/12** for both, which matches the per-case rows in the same table (see [EVALUATION_NOTES.md](EVALUATION_NOTES.md)).
The report itself is unchanged and hash-pinned.

## 2. Earlier evidence (historical; raw files not included in this submission)

Listed for transparency, including the weak and failed results. These runs used earlier agent versions or informed
later changes, so they are not evidence about v3.2.1.

| Evidence | Agent version | Type | Agent | Playbook |
|---|---|---|---|---|
| 7-scenario benchmark, 3 runs each | v2 | benchmark | 20/21 | 15/21 |
| First 3-run attempt (blocked by an HTTP 402 credit limit) | v2 | infrastructure failure, kept as recorded | 3/21 (18 infrastructure failures) | 15/21 |
| H1/H3 held-out stress tests | v2 | prospective (pre-registered `b8f7821`) | 3/6 | 3/6 |
| U1-U5 unseen-fault evaluation | v3.1 | prospective (pre-registered `2fbde0e`, `2e74c88`, `bc1806a`) | 5/12 | 5/12 |
| H4 queue backlog stress test | v3.1 | prospective stress variant (pre-registered `99b4b99`) | 3/3 | 1/3 |
| Single-run checkpoints (before and after the completion nudge) | v1, v2 | development | 6/7, 7/7 | 5/7, 5/7 |
| 7-scenario screenings, 1 run each | v3, v3.1 | development | 7/7, 7/7 | 5/7, 5/7 |
| H1/H3 regression | v3.1 | development/regression | 6/6 | 3/6 |
| U2 seed 801 targeted validation (possibly influenced by tool text later removed) | v3.2 | development | 1/1 | - |

The 7-scenario benchmark harness (`python -m autoheal.cli eval`) and the H1/H3/H4 harness (`python -m autoheal.holdout`)
are included so these experiments can be rerun; a rerun produces new evidence, not the recorded files.
