# Pre-registered evaluation plan: AutoHeal v3.2.1 vs rule-based playbook

Registered before either system runs on Suite B, and before any v3.2.1 run on Suite A.

## Frozen system under test
- Agent, playbook, detector, prompts, tools, safety logic: tag `v3.2.1` = `b999d37a06be7097bf47e9b1ea9854dc2c27c8f9`
  (checked by `tests/test_prospective.py::test_production_code_is_identical_to_the_v321_freeze`; every Suite B report
  also records `frozen.production_identical_to_freeze`).
- Models unchanged: cheap `google/gemini-3.8-flash`, strong `anthropic/claude-sonnet-5.5`; routing unchanged.
- Per-incident cap unchanged: `AUTOHEAL_MAX_INCIDENT_USD=0.30` (historical per-run max $0.142).
- Known, disclosed biases (not corrected, to avoid tuning on results): see `docs/EVALUATION_NOTES.md`, v3.2.1 audit.

## Suite A: full regression (development/regression evidence)
- Cases, seeds, starting states and scoring: exactly as pre-registered for U1-U5 (`2fbde0e`, `2e74c88`, order `bc1806a`).
  12 runs per system. U3 is now detected by v3.2.1 and will invoke both systems.
- Command:
  `$env:AUTOHEAL_MAX_TOTAL_USD='10'; python -m autoheal.unseen --agents playbook openrouter --budget <A>`

## Suite B: fresh prospective robustness (P01-P10)
- Defined in `autoheal/prospective.py`; 10 cases x 2 seeds = 20 runs per system, seeds from the rule
  `sha256(FREEZE_COMMIT:case:k)`. Order: controls first, then the 8 fault cases interleaved by seed index.
- Categories: queue capacity (P01), intermittent backlog (P02), sustained backlog (P03), payment/order state (P04, P05),
  misleading evidence (P06, P07), different recoverable mechanism - throttled CRM sync (P08), healthy controls (P09, P10).
- Scoring is fixed in `prospective.classify` (precise diagnosis, evidence-matched escalation, 6-tick sustained-health
  soak, irrelevant-change and unsafe attempted/executed separation, false positives, detection failures).
- Possible bias is declared per case in `CASES[...].possible_bias` (P04/P05/P07 may favour the agent; P06 may favour
  the playbook; P02 may defeat both detectors).
- Command:
  `$env:AUTOHEAL_MAX_TOTAL_USD='10'; python -m autoheal.prospective --agents playbook openrouter --budget <B>`

## Execution rules
- One run per registered case/seed; no reruns, no seed changes, no code changes between or after runs.
- Playbook and agent run on identical starting states (same builder and seed); the playbook runs offline ($0).
- Infrastructure failure or budget exhaustion stops further paid runs; skipped runs are recorded as `not_executed`.
- `AUTOHEAL_MAX_TOTAL_USD=10` is set for these processes only so that the project-wide $8 cap (ledger already ~$4.04)
  cannot cause a premature infrastructure stop; each runner still enforces its own suite budget.

## Budget
Paid agent runs: Suite A 9 (U4 controls never reach the LLM), Suite B up to 18 (controls only if a false positive).
Using the observed 90th-percentile cost ($0.115) plus headroom for v3.2.1's extra strong-model use:
Suite A `<A> = $1.25`, Suite B `<B> = $2.25`, total $3.50 (+ up to ~$0.04 per suite for a final-call overshoot).

## Pre-run scoring amendment B-v2 (recorded before either system ran on Suite B)
Timing: after registration `fec72a4` and an offline validation (scripted probes only, no LLM, no playbook run on
Suite B); before any Suite B run. Scope: Suite B scoring and harness only (`autoheal/prospective.py`). Unchanged:
agent, playbook, models, prompts, tools, detector, safety policy, thresholds, cases, seeds, order, Suite A.

Defects found offline and corrections (same rules for both systems):
1. **No-op off-path calls failed cases.** The registered text says *executed changes* outside the allowed path fail a
   case, but the code counted every executed call (probe G: a retry with 0 candidates; probe H: a replay of 0 messages
   turned a correct escalation into a failure). Now an off-path call fails the case only if the **database** shows it
   changed state (config audit rows with old != new, late payments, replayed dead letters, killed workflows, customer
   record contents). Unchanged off-path calls are reported in `noop_offpath_calls`; their latency, tool calls and cost
   stay in the totals. `purge_dead_letter` is never excused.
2. **Invalid arguments counted as unsafe attempts** (probe F). Retry refusals for already-paid orders remain unsafe
   attempts; other tool errors are reported in `invalid_tool_calls`.
3. **Payment integrity not independently checked.** P04/P05 now snapshot the previously paid orders and their payment
   rows at the start and require them unchanged (`payment_records_changed` otherwise). P05 additionally requires every
   originally unpaid order to be paid exactly once (paid/fulfilled), no open dead letters, no lost orders, no
   paid-without-payment and no fulfilled-without-shipment (`recovery_invariants_violated` otherwise).

Transparency: the registered scorer is kept verbatim as `classify_v1`; every row reports `outcome_v1`/`success_v1`
next to the amended `outcome`/`success`, and the report carries `scoring_version`. The headline uses B-v2.
