"""Startup validation that enforces the zero-cost policy (see COST_POLICY.md)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from careerpilot.config import Settings

FREE_LLM_PROVIDERS = {"none", "ollama"}


class CostPolicyViolation(RuntimeError):
    """Raised when configuration would introduce a monetary cost."""


@dataclass(frozen=True)
class Integration:
    name: str
    kind: str
    enabled: bool
    cost: str
    access: str
    automation_permitted: bool = True


def load_integrations(config_dir: Path) -> list[Integration]:
    raw = yaml.safe_load((config_dir / "integrations.yaml").read_text(encoding="utf-8")) or {}
    items = []
    for name, spec in (raw.get("integrations") or {}).items():
        items.append(
            Integration(
                name=name,
                kind=spec.get("kind", "unknown"),
                enabled=bool(spec.get("enabled", False)),
                cost=str(spec.get("cost", "unknown")).lower(),
                access=spec.get("access", ""),
                automation_permitted=bool(spec.get("automation_permitted", True)),
            )
        )
    return items


def validate_startup(settings: Settings, integrations: list[Integration] | None = None) -> list[str]:
    """Return human-readable checks passed; raise CostPolicyViolation on any paid path."""
    if not settings.zero_cost_mode:
        raise CostPolicyViolation(
            "ZERO_COST_MODE must be true. CareerPilot does not support paid operation."
        )
    if settings.llm_provider not in FREE_LLM_PROVIDERS:
        raise CostPolicyViolation(
            f"LLM_PROVIDER={settings.llm_provider!r} is not a free local provider. "
            f"Allowed: {sorted(FREE_LLM_PROVIDERS)}."
        )
    if settings.llm_provider == "ollama":
        host = settings.llm_base_url.split("//")[-1].split("/")[0].split(":")[0]
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise CostPolicyViolation("A local LLM must run on this computer (localhost only).")

    integrations = integrations if integrations is not None else load_integrations(settings.config_dir)
    problems = [
        f"{i.name} (cost: {i.cost})" for i in integrations if i.enabled and i.cost != "free"
    ]
    if problems:
        raise CostPolicyViolation("Enabled integrations are not free: " + ", ".join(problems))
    blocked = [i.name for i in integrations if i.enabled and not i.automation_permitted]
    if blocked:
        raise CostPolicyViolation(
            "Enabled sources do not permit automation: " + ", ".join(blocked)
        )
    return [
        "ZERO_COST_MODE=true",
        f"LLM provider: {settings.llm_provider}",
        f"{sum(i.enabled for i in integrations)} enabled integrations, all free",
        f"DRY_RUN={'true' if settings.dry_run else 'false'}",
        f"Kill switch {'ON (real submissions disabled)' if settings.submissions_kill_switch else 'off'}",
    ]
