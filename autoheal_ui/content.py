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
    """What an operator can see before any investigation: never the root cause or the expected repair."""
    title: str
    symptom: str


SCENARIO_COPY = {
    "payment_timeout": ScenarioCopy("Orders stuck before payment", "New orders are stuck with no payment recorded."),
    "queue_capacity": ScenarioCopy("Paid orders not shipping",
                                   "During a traffic spike, paid orders are landing in the dead-letter store."),
    "sync_corruption": ScenarioCopy("Customer records out of sync", "Customer records no longer match the CRM source."),
    "workflow_hang": ScenarioCopy("Workflows piling up", "Workflows are waiting in line and not moving forward."),
    "gateway_outage": ScenarioCopy("Payments failing", "Recent orders are ending in payment failure."),
    "combo_payment_queue": ScenarioCopy("Orders stuck and not shipping",
                                        "During a traffic spike, orders are stuck before payment and paid orders are "
                                        "not shipping."),
    "healthy": ScenarioCopy("Healthy control", "Normal traffic with no fault injected."),
}
