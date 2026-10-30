"""Central settings, loaded from .env (python-dotenv) and validated with Pydantic."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


class Settings(BaseModel):
    # --- LLM provider -------------------------------------------------------
    # The key must never appear in a repr (pytest failure output, logs, tracebacks) or a serialised dump.
    openrouter_api_key: str = Field(default_factory=lambda: _env("OPENROUTER_API_KEY", ""), repr=False, exclude=True)
    openrouter_base_url: str = Field(
        default_factory=lambda: _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    # Cheap model: single-symptom incidents. Strong model: multi-fault or after a failed fix.
    cheap_model: str = Field(default_factory=lambda: _env("AUTOHEAL_CHEAP_MODEL", "google/gemini-3.8-flash"))
    strong_model: str = Field(
        default_factory=lambda: _env("AUTOHEAL_STRONG_MODEL", "anthropic/claude-sonnet-5.5")
    )
    request_timeout_s: float = Field(default_factory=lambda: float(_env("AUTOHEAL_TIMEOUT_S", "90")))

    # --- Budget (USD) -------------------------------------------------------
    max_total_usd: float = Field(default_factory=lambda: float(_env("AUTOHEAL_MAX_TOTAL_USD", "8.0")))
    max_incident_usd: float = Field(default_factory=lambda: float(_env("AUTOHEAL_MAX_INCIDENT_USD", "0.30")))

    # --- Agent guardrails ----------------------------------------------------
    max_steps: int = Field(default_factory=lambda: int(_env("AUTOHEAL_MAX_STEPS", "16")))
    max_mutations: int = Field(default_factory=lambda: int(_env("AUTOHEAL_MAX_MUTATIONS", "8")))
    max_verify_attempts: int = Field(default_factory=lambda: int(_env("AUTOHEAL_MAX_VERIFY", "2")))
    max_guard_blocks: int = Field(default_factory=lambda: int(_env("AUTOHEAL_MAX_GUARD_BLOCKS", "3")))
    # Simulated ticks the agent may let pass (wait_and_observe) per incident; verification time is separate.
    max_wait_ticks: int = Field(default_factory=lambda: int(_env("AUTOHEAL_MAX_WAIT_TICKS", "12")))
    # Highest risk level the agent may execute without a human: low | medium | high
    max_auto_risk: str = Field(default_factory=lambda: _env("AUTOHEAL_MAX_AUTO_RISK", "medium"))
    dry_run: bool = Field(default_factory=lambda: _env("AUTOHEAL_DRY_RUN", "false").lower() == "true")
    tool_result_chars: int = 1800  # truncate tool output sent to the LLM (context economy)

    # --- Storage --------------------------------------------------------------
    db_path: str = Field(default_factory=lambda: _env("AUTOHEAL_DB", str(PROJECT_ROOT / "data" / "autoheal.db")))
    seed: int = 7


def get_settings(**overrides) -> Settings:
    s = Settings()
    return s.model_copy(update=overrides) if overrides else s
