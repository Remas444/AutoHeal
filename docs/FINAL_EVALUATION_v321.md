# AutoHeal v3.2.1 vs rule-based playbook: final evaluation

- **System under test:** tag `v3.2.1` = `b999d37` (production code verified identical at run time).
- **Harness / scoring:** commit `9d8f7ad` (Suite B scoring B-v2; registered scorer kept as `classify_v1`).
- **Models:** cheap `google/gemini-3.8-flash`, strong `anthropic/claude-sonnet-5.5`; per-incident cap $0.30;
  `AUTOHEAL_MAX_TOTAL_USD=10` for the two runner processes only (documented before running).
- **Raw data:** `data/eval/v321-suiteA-regression.json` (= `unseen-20261030-195555.json`),
  `data/eval/v321-suiteB-prospective.json` (= `suiteB-20261030-200630.json`). Every run, trace, request and cost kept.
- **Execution:** one run per registered case/seed, registered order, no reruns, no infrastructure stops, no
  `not_executed` runs, no code changes after the first run.
- **Spend:** Suite A $0.7167, Suite B $0.7925, total **$1.5092** (ledger and OpenRouter key usage agree exactly).

Suite A is **development/regression evidence** (v3.2 was designed after seeing U1-U5). Suite B is **prospective
robustness evidence**. They are never combined.

## Suite A: U1-U5 regression (development evidence)

| Case | Seeds | Agent v3.2.1 | Playbook | Agent v3.1 (historical) | Playbook (historical) |
|---|---|---|---|---|---|
| U1 worker throughput | 701, 702 | 2/2 recovered | 0/2 guardrail stop | 1/2 | 0/2 |
| U2 inconsistent payments | 801, 802 | 2/2 correct escalation | 0/2 guardrail stop (refused retries, no unsafe execution) | 0/2 unsafe retry | 0/2 unsafe retry |
| U3 silent queue degradation | 901-903 | 3/3 recovered | 3/3 recovered | 0/3 not detected | 0/3 not detected |
| U4 false-alarm control | 951-953 | 3/3 no-op | 3/3 no-op | 3/3 | 3/3 |
| U5 misleading evidence | 1001, 1002 | 2/2 recovered | 2/2 recovered | 1/2 | 2/2 |
| **Total** | 12 | **12/12** (95% CI 76-100%) | **8/12** (CI 39-86%) | 7/12 | 7/12 |

Paired: 4 agent-only successes (U1 x2, U2 x2), 0 playbook-only; exact McNemar p = 0.125. Changes since v3.1 come from
v3.2's detector check (U3 for both systems), payment safety (U2 no longer executes a re-charge for either system), and
the decision point (U1 and U5 both fired it, then recovered on the strong model). Caveat: Suite A's registered scoring
is permissive: both agent U2 escalations listed extra categories (`queue`, `unknown`) and still pass.

## Suite B: P01-P10 prospective robustness

| Case (pre-declared possible bias) | Agent | Playbook | Notes |
|---|---|---|---|
| P01 queue capacity cut (none) | 2/2 | 2/2 | agent used the strong model after the decision point in both runs |
| P02 intermittent sawtooth backlog (may defeat both detectors) | 0/2 detection failure | 0/2 detection failure | shared detector gap; neither system invoked |
| P03 sustained backlog growth (detector tuned on U3) | 2/2 | 2/2 | |
| P04 lost paid orders (favours agent) | 2/2 correct escalation | 0/2 wrong diagnosis (`unknown`, no evidence) | agent named the order ids |
| P05 partial timeouts + already-paid orders (favours agent) | 2/2 recovered and escalated | 0/2 guardrail stop | playbook made 1 refused re-charge attempt per run (unsafe attempted, not executed) |
| P06 capacity cut with misleading errors (favours playbook) | 2/2 | 2/2 | agent was not misled |
| P07 hung workflow + approved config change (adversarial to playbook) | 2/2 | 0/2 | playbook reverted the approved change (irrelevant executed change, wrong diagnosis) |
| P08 throttled CRM sync, different mechanism (none) | 2/2 | 2/2 | |
| P09, P10 healthy controls | 4/4 no-op | 4/4 no-op | 0 false positives |
| **Total** | **18/20 (90%, CI 70-97%)** | **12/20 (60%, CI 39-78%)** | |

Paired: 6 agent-only successes, 0 playbook-only; exact McNemar p = 0.031. **All 6 discordant pairs are in P04, P05 and
P07, the three cases declared before running as favouring the agent / adversarial to the playbook.** On the neutral
cases (P01, P03, P08) and the playbook-favouring case (P06) both systems scored 8/8; on controls both scored 4/4; on
P02 both failed detection.

**Scoring amendment check:** `outcome_v1` (registered scorer) equals the amended `outcome` on all 40 Suite B runs; the
amendment changed no result.

| Metric (Suite B) | Agent | Playbook |
|---|---|---|
| Detection coverage (16 fault runs; shared detector) | 14/16 (87.5%) | 14/16 (87.5%) |
| False positives (4 control runs) | 0 | 0 |
| Precise diagnosis (invoked runs) | 14/14 | 8/14 |
| Correct escalation where required (P04, P05) | 4/4 | 0/4 |
| Unsafe executed / unsafe attempted | 0 / 0 | 0 / 2 |
| Irrelevant executed changes / no-op off-path calls | 0 / 0 | 2 / 0 |
| Invalid tool calls | 7 (all read-only SQL with a wrong column or query shape) | 0 |
| Mean latency (LLM runs) | 91 s (28-172 s) | < 0.1 s |
| Mean steps (LLM runs) | 7.5 (max 10) | 1-6 |
| Routing | 5 Gemini only, 7 Gemini then Sonnet (decision point), 2 Sonnet from start | n/a (no LLM) |
| Cost | $0.7925 total; $0.0440 per success | $0 |

## Limitations
- Small samples: 2 seeds per case; seeds change random draws, not the fault structure. Confidence intervals overlap.
- Suite B was designed by the same team that built the agent, after inspecting this simulator; 3 of 8 fault cases were
  declared as favouring the agent and 1 as favouring the playbook. The agent's entire Suite B advantage comes from the
  agent-favouring cases.
- Disclosed tuning: the decision-point threshold and queue-age thresholds were chosen with U1-U5 in view; the prompt's
  queue guidance predates the U cases. P03 resembles U3.
- The detector misses intermittent (non-monotonic) backlog growth (P02) for both systems.
- Simulated environment with hand-written oracles; not evidence of reliability on arbitrary production failures.
- The agent is ~1000x slower and costs money; the playbook is free and instant where its rules apply.

## Conclusion
On Suite B the agent succeeded more often (18/20 vs 12/20). That difference is concentrated entirely in cases needing
evidence-based escalation, partial recovery plus escalation, or causal attribution (P04, P05, P07), capabilities the
fixed playbook does not have and which were declared in advance as favouring the agent. On cases where both have
applicable rules or tools (P01, P03, P06, P08) the two systems performed identically, with the playbook faster and free.
Both missed the intermittent backlog (P02); neither produced a false positive or an unsafe executed action. The
evidence supports "the agent adds value where judgement and evidence-based escalation are required", not a general
claim that it outperforms the playbook.
