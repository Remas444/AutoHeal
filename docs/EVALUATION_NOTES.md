# Evaluation notes and errata

> **Submission snapshot:** only the two final result files (`v321-suiteA-regression.json`,
> `v321-suiteB-prospective.json`) are included here. The historical files named below are kept, unedited, in the
> private development repository; see [VERIFICATION.md](VERIFICATION.md) and the summary in
> [EVALUATION_INDEX.md](EVALUATION_INDEX.md).

Historical result files in `data/eval/` are **never edited**. Their SHA-256 hashes are pinned in
`tests/test_evidence_integrity.py`. This page records how to read them correctly; fixes apply only to runs made
after v3.2.

## Evidence types

| Evidence | Files | Type |
|---|---|---|
| v2 final benchmark (7 scenarios x 3) | `final-runs3.*`, `final-report.md` | benchmark (v2 agent) |
| First 3-run attempt, blocked by HTTP 402 | `*-infrastructure-failed.*` | infrastructure failure, kept as recorded |
| H1/H3 holdout (v2) | `holdout-h1-h3.json`, `holdout-report.md` | prospective, pre-registered at `b8f7821` |
| v3 / v3.1 screenings, v3 Phase 1 | `v3-*.json`, `v31-screening-runs1.*` | development checks |
| v3.1 H1/H3 rerun | `v31-h1h3-regression.json` | development/regression (H1 informed v3) |
| H4 | `h4-results.json` | prospective stress variant, pre-registered at `99b4b99` |
| U1-U5 (v3.1 agent) | `unseen-u1-u5-results.json` | prospective, pre-registered at `2fbde0e`, `2e74c88`, order amended at `bc1806a`, results `4413a68` |

**Any rerun of U1-U5, H1, H3 or H4 after these commits is development/regression evidence**, not an untouched
holdout: v3.2 was designed after inspecting their results. New runner reports carry an `evidence_type` field.

## 7-scenario benchmark metrics (`autoheal/evaluate.py`, `python -m autoheal.cli eval`)

- Fixable scenarios succeed only if the agent resolves the incident and the hidden ground truth is healthy (config
  restored, order lifecycle consistent). The unfixable `gateway_outage` succeeds only if the agent itself escalates
  and names `external_dependency`; a guardrail stop is safe but not a success.
- Every escalation records `escalation_cause`: `agent` (the agent chose to hand off), `guardrail` (step, mutation,
  loop or verify limit) or `infrastructure` (LLM unavailable or budget cap).
- `valid_run_success_rate` excludes infrastructure failures; `end_to_end_success_rate` counts every attempted run.
- `cost_per_success_usd` divides the spend of all runs, failed ones included, by the number of successes.

## Known reporting quirks in historical files (not corrected in place)

1. **Playbook "models", "routing" and "model_upgrades".** The playbook is rule-based and never calls an LLM. Model
   names in its rows are routing bookkeeping (for example after a failed verification). Ignore them.
2. **`llm_called: true` for playbook rows in `unseen-u1-u5-results.json`.** It was derived from routing entries,
   not API requests. The playbook made no LLM calls. Since v3.2 `llm_called` comes from actual requests.
3. **`diagnosis_correct` / `config_restored` on U3/U4 rows.** Neither system was invoked (the detector was
   silent), so these fields are not applicable; since v3.2 they are `null` in that case.
4. **H1 title "Payment timeout under sustained heavy traffic".** Heavy traffic (60 orders/tick) applies only while
   the fault manifests; recovery (`wait_and_observe`) runs at normal traffic (12 orders/tick). The same holds for H4,
   which is labelled correctly.
5. **Verifier-drained queue backlog.** The independent verifier advances time and can drain in-flight queue
   messages before judging. It never clears dead letters. Runs since H4 record `verifier_drained_queue`.
6. **`mutations`** counts allowed mutation attempts (the guard's budget accounting), including attempts the tool
   rejected or dry-run skipped. Since v3.2 reports add `executed_mutations` and `unexecuted_mutation_attempts`.

## v3.2 behaviour changes that affect comparisons with earlier results

- **Payment safety:** `retry_stuck_payments` never re-charges an order that already has a payment record and
  refuses (fails closed) when every candidate is already paid. In U2 this changes both systems' behaviour; the
  pre-registered U2 rule (an executed retry is unsafe) is unchanged.
- **Detection:** `queue_backlog_age` makes U3 detectable, so both systems are now invoked on U3.
- **Decision point:** a never-acting investigation gets one prompt and the strong model after 6 turns.

## v3.2.1 contamination audit (before freezing the evaluated version)

Production modules (agent, tools, guard, detector, verifier, playbook, LLM client, config, simulator, runtime) contain
no scenario IDs, seeds, oracles or evaluation labels. Agent-visible text and thresholds changed in v3.2 were classified:

| Change | Class | v3.2.1 action |
|---|---|---|
| Retry never re-charges an order with a payment record; fail-closed refusal | Safety policy | kept unchanged |
| Refusal/hint text "escalate_to_human with this evidence" | **Direct answer leakage** (named the U2 success action) | **removed**; factual text only |
| Detector text "(order status never advanced)" | Mild diagnostic leakage (named the U2 mechanism) | **removed**; counts kept |
| Tool description "orders that already have one are never re-charged" | Generalisable (documents real behaviour) | kept |
| `queue_backlog_age` thresholds (age >= 5, growth window 5) | Test-informed tuning (validated on U3) | kept and disclosed; not retuned |
| Decision point at 6 investigation turns | Test-informed tuning (chosen with U1-702/U5-1001 traces in view) | kept and disclosed; not retuned |
| Prompt and replay-hint guidance to raise `queue.worker_batch` and drain (since v2, tuned on the combo scenario) | Test-informed tuning predating U-cases; favours queue-throughput cases | kept and disclosed |

Consequences: the v3.2 U2 seed 801 validation (`v32-u2-801-validation.json`) ran with the leaked wording in the tool
description era and is treated as possibly influenced. Suite A (U1-U5 regression) remains development/regression
evidence. Suite B is designed only after the v3.2.1 freeze.

## Erratum to `FINAL_EVALUATION_v321.md` (found during release cleanup; the report itself is not edited)

In the Suite A table, the **Total** row for the historical v3.1 columns reads `7/12 | 7/12`. Recomputed from
`unseen-u1-u5-results.json`, it is **agent 5/12 and playbook 5/12**, which matches the per-case rows above it
(agent 1+0+0+3+1, playbook 0+0+0+3+2). This was an arithmetic error in the report. The v3.2.1 results (agent 12/12,
playbook 8/12), all Suite B results and every conclusion are unaffected. The report stays byte-identical (hash-pinned);
`docs/EVALUATION_INDEX.md` and the README use the corrected figures.
