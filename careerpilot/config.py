"""Runtime settings loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def _as_bool(value: str | bool | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    zero_cost_mode: bool = True
    dry_run: bool = True
    submissions_kill_switch: bool = True
    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str = ""
    timezone: str = "Asia/Jerusalem"
    max_daily_applications: int = Field(default=5, ge=0)
    llm_provider: str = "none"
    llm_base_url: str = "http://localhost:11434"
    approved_submission_domains: list[str] = Field(default_factory=list)
    generate_pdf: bool = True
    config_dir: Path = CONFIG_DIR

    @field_validator("approved_submission_domains", mode="before")
    @classmethod
    def _split_domains(cls, v):
        if isinstance(v, str):
            return [d.strip().lower() for d in v.split(",") if d.strip()]
        return [d.lower() for d in (v or [])]

    @property
    def real_submissions_enabled(self) -> bool:
        """True only when every global switch allows a real submission."""
        return self.zero_cost_mode and not self.dry_run and not self.submissions_kill_switch

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'careerpilot.db').as_posix()}"

    def ensure_dirs(self) -> None:
        for sub in ("", "cv/master", "cv/variants", "snapshots", "reports", "exports", "backups", "imports"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


def load_settings(env_file: Path | None = None, **overrides) -> Settings:
    """Environment variables win over .env; keyword overrides win over both."""
    file_values = dotenv_values(env_file or PROJECT_ROOT / ".env")
    merged = {**{k: v for k, v in file_values.items() if v is not None}, **os.environ}

    def get(name: str):
        return merged.get(name)

    values = {
        "zero_cost_mode": _as_bool(get("ZERO_COST_MODE"), True),
        "dry_run": _as_bool(get("DRY_RUN"), True),
        "submissions_kill_switch": _as_bool(get("SUBMISSIONS_KILL_SWITCH"), True),
        "timezone": get("TIMEZONE") or "Asia/Jerusalem",
        "max_daily_applications": int(get("MAX_DAILY_APPLICATIONS") or 5),
        "llm_provider": (get("LLM_PROVIDER") or "none").strip().lower(),
        "llm_base_url": get("LLM_BASE_URL") or "http://localhost:11434",
        "approved_submission_domains": get("APPROVED_SUBMISSION_DOMAINS") or "",
        "database_url": get("DATABASE_URL") or "",
        "generate_pdf": _as_bool(get("GENERATE_PDF"), True),
    }
    if get("DATA_DIR"):
        data_dir = Path(get("DATA_DIR"))
        values["data_dir"] = data_dir if data_dir.is_absolute() else (PROJECT_ROOT / data_dir).resolve()
    values.update(overrides)
    return Settings(**values)
