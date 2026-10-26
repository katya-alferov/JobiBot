"""Deterministic submission authorization. Only this code can allow a real submission.

A model or matching recommendation is an input to nothing here except the eligibility result,
which itself comes from deterministic hard filters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from careerpilot.applications.adapters import ApplicationAdapter, AssistedManualAdapter
from careerpilot.applications.state import READY_TO_SUBMIT, SENT_STATUSES
from careerpilot.config import Settings
from careerpilot.matching.engine import best_match
from careerpilot.profile.service import sha256_file
from careerpilot.storage.models import Application, ApplicationAttempt, utcnow


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class AuthorizationDecision:
    checks: list[Check] = field(default_factory=list)

    @property
    def application_ready(self) -> bool:
        """Everything about the application itself is in order (ignores global switches)."""
        return all(c.passed for c in self.checks if not c.name.startswith("global_"))

    @property
    def real_submission_allowed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def as_dict(self) -> dict:
        return {"real_submission_allowed": self.real_submission_allowed,
                "checks": [c.__dict__ for c in self.checks]}


def authorize(session: Session, settings: Settings, app: Application,
              adapter: ApplicationAdapter) -> AuthorizationDecision:
    d = AuthorizationDecision()
    job = app.job
    add = lambda name, ok, detail: d.checks.append(Check(name, bool(ok), detail))  # noqa: E731

    m = best_match(session, job.id)
    add("hard_filters", m is not None and m.eligible,
        "all hard filters passed" if m and m.eligible else "job failed a hard filter or is unscored")

    url = app.application_url or job.official_application_url
    verified = job.employer_verified and bool(url) and job.expiration_status == "active"
    add("job_verified", verified,
        "employer, role and URL verified" if verified else "employer/URL unverified or posting not active")

    add("method_free_and_permitted", adapter.cost == "free" and adapter.automation_permitted
        and not isinstance(adapter, AssistedManualAdapter),
        f"adapter {adapter.name}: cost={adapter.cost}, automated={not isinstance(adapter, AssistedManualAdapter)}")

    host = urlsplit(url).netloc.lower() if url else ""
    add("domain_approved", host in settings.approved_submission_domains,
        f"{host or 'no URL'} {'is' if host in settings.approved_submission_domains else 'is not'} on the allowlist")

    duplicate = session.scalars(select(Application).where(
        Application.profile_id == app.profile_id, Application.id != app.id,
        Application.job_id.in_(select_job_group(job)), Application.status.in_(SENT_STATUSES))).first()
    prior_sent = any(a.result in {"SUBMITTED", "CONFIRMED"} for a in app.attempts if not a.simulated)
    pending_uncertain = _latest_real_attempt(app) is not None and _latest_real_attempt(app).result == "UNCERTAIN"
    add("not_already_applied", not duplicate and not prior_sent and app.status not in SENT_STATUSES,
        "no earlier application for this position" if not (duplicate or prior_sent) else "already applied")
    add("no_uncertain_attempt", not pending_uncertain,
        "no unresolved attempt" if not pending_uncertain else "an earlier attempt is uncertain; check for receipt first")

    v = app.resume_variant
    cv_ok = bool(v and v.valid and Path(v.docx_path).exists() and sha256_file(Path(v.docx_path)) == v.docx_sha256)
    add("cv_validated", cv_ok, "CV variant valid, saved and hash matches" if cv_ok else "CV missing, invalid or changed")

    missing = [a.question for a in app.answers if a.required and (a.needs_user_input or not a.answer)]
    add("required_fields_verified", not missing,
        "all required answers come from verified data" if not missing else f"missing: {missing}")

    sensitive = [a.question for a in app.answers if a.required and a.sensitive and not a.confirmed_by_user]
    add("no_critical_user_questions", not sensitive,
        "no sensitive questions pending" if not sensitive else f"needs the candidate: {sensitive}")

    inspection = adapter.inspect(url or "")
    restricted = inspection.restricted_actions + (["captcha"] if inspection.captcha_present else []) + \
        (["account_required"] if inspection.requires_account else [])
    add("no_restricted_actions", not restricted, "none" if not restricted else f"restricted: {restricted}")

    add("status_ready", app.status == READY_TO_SUBMIT, f"status is {app.status}")

    since = utcnow() - timedelta(hours=24)
    today = session.scalar(select(func.count(ApplicationAttempt.id)).where(
        ApplicationAttempt.simulated.is_(False), ApplicationAttempt.attempted_at >= since,
        ApplicationAttempt.result.in_(["SUBMITTED", "CONFIRMED", "UNCERTAIN"]))) or 0
    add("volume_limit", today < settings.max_daily_applications,
        f"{today} real submissions in the last 24h; limit {settings.max_daily_applications}")

    add("global_zero_cost", settings.zero_cost_mode, "ZERO_COST_MODE=true" if settings.zero_cost_mode else "off")
    add("global_dry_run_off", not settings.dry_run, "DRY_RUN=true blocks real submissions" if settings.dry_run else "DRY_RUN=false")
    add("global_kill_switch_off", not settings.submissions_kill_switch,
        "kill switch ON" if settings.submissions_kill_switch else "kill switch off")
    return d


def select_job_group(job):
    from careerpilot.storage.models import JobPosting

    return select(JobPosting.id).where(JobPosting.duplicate_group_id == job.duplicate_group_id)


def _latest_real_attempt(app: Application) -> ApplicationAttempt | None:
    real = [a for a in app.attempts if not a.simulated]
    return real[-1] if real else None
