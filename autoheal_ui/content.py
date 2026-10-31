"""User-facing wording: every internal identifier shown in the console is translated here."""
from __future__ import annotations

from dataclasses import dataclass

ENGINE_LABELS = {"openrouter": "AI Agent", "playbook": "Rule-based Playbook"}

RISK_LABELS = {"low": "Low only", "medium": "Up to medium (default)", "high": "Up to high"}

CHECK_LABELS = {
    "orders_without_payment": "Orders stuck before payment",
    "payment_failure_rate": "High payment failure rate",
    "dead_letter_backlog": "Dead-letter backlog",
    "queue_backlog_age": "Queue falling behind",
    "orders_lost": "Paid orders lost",
    "progress_without_payment": "Orders progressing without payment",
    "payment_without_progress": "Payments without order progress",
    "fulfilled_without_shipment": "Fulfilled orders without a shipment",
    "data_reconciliation": "Customer data out of sync",
    "workflow_stall": "Workflows stalled",
}

READ_TOOL_TITLES = {
    "get_health_report": "Reading health report",
    "search_logs": "Checking logs",
    "get_metrics": "Checking metrics",
    "get_config": "Reading current configuration",
    "get_config_history": "Examining configuration history",
    "run_readonly_sql": "Querying system data (read-only)",
    "list_workflows": "Inspecting workflows",
    "wait_and_observe": "Letting simulated time pass and observing",
}

ACTION_TITLES = {
    "rollback_config": "Roll back configuration",
    "set_config": "Change configuration",
    "retry_stuck_payments": "Retry stuck payments",
    "replay_dead_letter": "Replay dead letters",
    "resync_customers": "Resynchronise customer data",
    "terminate_workflow": "Terminate stalled workflow",
    "purge_dead_letter": "Purge dead letters",
}

# Evaluation outcome identifiers, as recorded in the committed result files.
OUTCOME_LABELS = {
    "recovered": "Recovered",
    "correct_noop": "No action required",
    "correct_escalation": "Correct escalation",
    "recovered_and_escalated": "Partially recovered and escalated",
    "guardrail_stop": "Stopped by safety controls",
    "detection_failure": "Not detected",
    "wrong_diagnosis": "Wrong diagnosis",
    "false_positive": "False positive",
    "infrastructure_failure": "Infrastructure failure",
    "not_executed": "Not executed",
}


def outcome_label(outcome: str) -> str:
    return OUTCOME_LABELS.get(outcome, outcome.replace("_", " ").capitalize())


def check_label(check: str) -> str:
    return CHECK_LABELS.get(check, check.replace("_", " ").capitalize())


@dataclass(frozen=True)
class ScenarioCopy:
    title: str
    what_happens: str
    what_to_expect: str


SCENARIO_COPY = {
    "payment_timeout": ScenarioCopy(
        "Payments silently dropping orders",
        "A deploy cuts the payment gateway timeout below real latency. Timeouts are swallowed, so orders stay "
        "unpaid with no error raised.",
        "Recoverable: the cause is a configuration change, and stuck orders can be retried once it is reverted.",
    ),
    "queue_capacity": ScenarioCopy(
        "Queue losing messages under load",
        "A deploy shrinks the fulfilment queue during a traffic spike, so messages overflow into the dead-letter "
        "store.",
        "Recoverable: revert the change, then replay the dead letters.",
    ),
    "sync_corruption": ScenarioCopy(
        "Sync job corrupting customer data",
        "A new CRM field mapping truncates phone numbers, damaging a few customer records every tick.",
        "Recoverable: revert the mapping, then run a full resynchronisation.",
    ),
    "workflow_hang": ScenarioCopy(
        "Workflow hanging forever",
        "A workflow holds the billing lock and stops sending heartbeats, so every other workflow queues behind it.",
        "Recoverable: the stalled lock holder may be terminated because its heartbeat is stale.",
    ),
    "gateway_outage": ScenarioCopy(
        "Upstream payment provider outage",
        "The external payment provider returns HTTP 503. Nothing inside this system caused it.",
        "Not recoverable from inside the system: the correct result is an escalation to a human, with no changes made.",
    ),
    "combo_payment_queue": ScenarioCopy(
        "Two overlapping failures",
        "A payment timeout and a queue shrink overlap during a traffic spike, and repairing one can trigger "
        "knock-on damage in the other.",
        "Hard case: the Playbook's fixed rules do not fully clear it here, and independent verification reports "
        "that. The AI Agent can keep working on what remains.",
    ),
    "healthy": ScenarioCopy(
        "Healthy control",
        "No fault is injected. This confirms that AutoHeal does nothing when nothing is wrong.",
        "No action required: no recovery action and no LLM call.",
    ),
}
