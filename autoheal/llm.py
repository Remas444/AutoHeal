"""OpenRouter chat client with spend tracking and hard budget caps.

* Every call is written to the usage ledger (data/usage.db): tokens, USD cost, latency.
* Cost comes from OpenRouter's own `usage.cost` field when present; otherwise it is
  estimated from cached model prices (see `python -m autoheal.cli models`).
* Before every call the client refuses to proceed if the global or per-incident
  cap would be exceeded (BudgetExceeded) - the agent then escalates instead.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Protocol

import requests

from .config import PROJECT_ROOT, Settings
from .db import connect, init_usage, scalar

PRICES_FILE = PROJECT_ROOT / "data" / "model_prices.json"
# Conservative fallback (USD per 1M tokens) if neither usage.cost nor cached prices exist.
FALLBACK_PRICE = {"prompt": 3.0, "completion": 15.0}


class BudgetExceeded(Exception):
    pass


class LLMError(Exception):
    pass


class ChatModel(Protocol):
    def chat(self, messages: list[dict], tools: list[dict], model: str, purpose: str,
             incident_id: int) -> dict: ...


class UsageLedger:
    def __init__(self, path: str, run_tag: str = ""):
        self.conn: sqlite3.Connection = connect(path)
        init_usage(self.conn)
        self.run_tag = run_tag

    def record(self, **kw) -> None:
        self.conn.execute(
            "INSERT INTO llm_usage (ts, run_tag, incident_id, model, purpose, prompt_tokens, completion_tokens, "
            "cost_usd, cost_source, latency_ms, ok) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), self.run_tag, kw.get("incident_id"), kw["model"], kw.get("purpose"),
             kw.get("prompt_tokens", 0), kw.get("completion_tokens", 0), kw.get("cost_usd", 0.0),
             kw.get("cost_source", ""), kw.get("latency_ms", 0), int(kw.get("ok", True))))
        self.conn.commit()

    def total(self) -> float:
        return float(scalar(self.conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage") or 0)

    def run_total(self) -> float:
        return float(scalar(self.conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE run_tag = ?",
                            (self.run_tag,)) or 0)

    def incident_total(self, incident_id: int) -> float:
        return float(scalar(self.conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage "
                                       "WHERE incident_id = ? AND run_tag = ?", (incident_id, self.run_tag)) or 0)

    def incident_rows(self, incident_id: int) -> list[dict]:
        cur = self.conn.execute("SELECT model, purpose, prompt_tokens, completion_tokens, cost_usd, latency_ms "
                                "FROM llm_usage WHERE incident_id = ? AND run_tag = ? ORDER BY id",
                                (incident_id, self.run_tag))
        return [dict(r) for r in cur.fetchall()]


def _load_prices() -> dict:
    try:
        return json.loads(Path(PRICES_FILE).read_text())
    except (OSError, ValueError):
        return {}


class OpenRouterClient:
    def __init__(self, settings: Settings, ledger: UsageLedger):
        if not settings.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key, "
                           "or use --llm playbook for the offline baseline.")
        self.s = settings
        self.ledger = ledger
        self.prices = _load_prices()

    def _check_budget(self, incident_id: int) -> None:
        spent = self.ledger.total()
        if spent >= self.s.max_total_usd:
            raise BudgetExceeded(f"Global LLM budget reached (${spent:.4f} / ${self.s.max_total_usd:.2f}).")
        inc = self.ledger.incident_total(incident_id)
        if inc >= self.s.max_incident_usd:
            raise BudgetExceeded(f"Per-incident budget reached (${inc:.4f} / ${self.s.max_incident_usd:.2f}).")

    def _cost(self, model: str, usage: dict) -> tuple[float, str]:
        if isinstance(usage.get("cost"), (int, float)):
            return float(usage["cost"]), "openrouter"
        p = self.prices.get(model, FALLBACK_PRICE)
        est = usage.get("prompt_tokens", 0) * p["prompt"] / 1e6 + usage.get("completion_tokens", 0) * p["completion"] / 1e6
        return est, "estimated"

    def chat(self, messages: list[dict], tools: list[dict], model: str, purpose: str = "",
             incident_id: int = 0) -> dict:
        self._check_budget(incident_id)
        body = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto",
                "temperature": 0.1, "max_tokens": 1500, "usage": {"include": True}}
        headers = {"Authorization": f"Bearer {self.s.openrouter_api_key}", "Content-Type": "application/json",
                   "HTTP-Referer": "https://github.com/autoheal", "X-Title": "AutoHeal"}
        last_err = ""
        for attempt in range(3):
            t0 = time.time()
            try:
                r = requests.post(f"{self.s.openrouter_base_url}/chat/completions", headers=headers,
                                  json=body, timeout=self.s.request_timeout_s)
            except requests.RequestException as e:
                last_err = f"network error: {e}"
                time.sleep(2 ** attempt)
                continue
            latency = int((time.time() - t0) * 1000)
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                self.ledger.record(model=model, purpose=purpose, incident_id=incident_id, latency_ms=latency, ok=False)
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                raise LLMError(f"OpenRouter HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            if "error" in data:
                raise LLMError(f"OpenRouter error: {data['error']}")
            usage = data.get("usage") or {}
            cost, src = self._cost(model, usage)
            self.ledger.record(model=data.get("model", model), purpose=purpose, incident_id=incident_id,
                               prompt_tokens=usage.get("prompt_tokens", 0),
                               completion_tokens=usage.get("completion_tokens", 0),
                               cost_usd=cost, cost_source=src, latency_ms=latency)
            return data["choices"][0]["message"]
        raise LLMError(f"OpenRouter failed after retries: {last_err}")


def fetch_model_catalog(settings: Settings, models: list[str]) -> dict:
    """Check model IDs exist + support tools; cache prices to data/model_prices.json."""
    r = requests.get(f"{settings.openrouter_base_url}/models", timeout=30)
    r.raise_for_status()
    catalog = {m["id"]: m for m in r.json()["data"]}
    report, prices = {}, _load_prices()
    for mid in models:
        m = catalog.get(mid)
        if not m:
            report[mid] = {"found": False}
            continue
        p = {"prompt": float(m["pricing"]["prompt"]) * 1e6, "completion": float(m["pricing"]["completion"]) * 1e6}
        prices[mid] = p
        report[mid] = {"found": True, "tools": "tools" in m.get("supported_parameters", []),
                       "usd_per_1M_in": round(p["prompt"], 3), "usd_per_1M_out": round(p["completion"], 3),
                       "context": m.get("context_length")}
    PRICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRICES_FILE.write_text(json.dumps(prices, indent=2))
    return report


def suggest_tool_models(settings: Settings, max_in_price: float = 4.0, top: int = 15) -> list[dict]:
    r = requests.get(f"{settings.openrouter_base_url}/models", timeout=30)
    r.raise_for_status()
    out = []
    for m in r.json()["data"]:
        if "tools" not in m.get("supported_parameters", []):
            continue
        pin = float(m["pricing"]["prompt"]) * 1e6
        if 0 < pin <= max_in_price and any(v in m["id"] for v in ("google/", "anthropic/", "openai/", "deepseek/",
                                                                     "qwen/", "mistralai/")):
            out.append({"id": m["id"], "in": round(pin, 3),
                        "out": round(float(m["pricing"]["completion"]) * 1e6, 3)})
    return sorted(out, key=lambda x: x["in"])[:top]
