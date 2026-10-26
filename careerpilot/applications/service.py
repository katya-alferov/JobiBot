"""Prepare applications, run (simulated or authorized) submissions, and keep the audit trail."""

from __future__ import annotations

import hashlib
import shutil
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerpilot.applications import state as S
from careerpilot.applications.adapters import (
    ApplicationAdapter,
    AssistedManualAdapter,
    SubmissionPackage,
    pick_adapter,
)
from careerpilot.applications.gate import _latest_real_attempt, authorize
from careerpilot.config import Settings
from careerpilot.documents.materials import answer_question, cover_letter, recruiter_message
from careerpilot.documents.tailor import create_variant
from careerpilot.matching.engine import best_match
from careerpilot.profile.service import ProfileView, make_read_only, sha256_file
from careerpilot.storage.audit import audit
from careerpilot.storage.models import (
    Application,
    ApplicationAnswer,
    ApplicationAttempt,
    ApplicationMaterial,
    FollowUpReminder,
    JobPosting,
    utcnow,
)

DEFAULT_QUESTIONS = ["First name", "Last name", "Email", "Phone"]
FOLLOW_UP_DAYS = 7


class DuplicateApplication(RuntimeError):
    pass


def idempotency_key(profile_id: int, job: JobPosting) -> str:
    return hashlib.sha256(f"{profile_id}:{job.duplicate_group_id}".encode()).hexdigest()


def find_application(session: Session, profile_id: int, job: JobPosting) -> Application | None:
    return session.scalars(select(Application).where(
        Application.idempotency_key == idempotency_key(profile_id, job))).first()


def prepare_application(session: Session, settings: Settings, view: ProfileView, job: JobPosting,
                        confirmed_answers: dict[str, str] | None = None) -> Application:
    """Create (once) the application record, tailored CV, cover letter and answers."""
    existing = find_application(session, view.profile.id, job)
    if existing:
        return existing
    match = best_match(session, job.id)
    if match is None or not match.eligible:
        raise ValueError("Job is not eligible; run matching first")
    variant = create_variant(session, settings, view, job, match, make_pdf=settings.generate_pdf)
    letter, _ = cover_letter(view, job, match)
    links = {}
    if view.profile.linkedin_profile_url:
        links["linkedin"] = {"url": view.profile.linkedin_profile_url,
                             "verified": view.profile.linkedin_profile_verified}
    if view.profile.personal_website_url:
        links["website"] = {"url": view.profile.personal_website_url,
                            "verified": view.profile.personal_website_verified}
    app = Application(
        idempotency_key=idempotency_key(view.profile.id, job), profile_id=view.profile.id, job_id=job.id,
        track=match.track, match_score=match.score, status=S.PREPARED, resume_variant_id=variant.id,
        profile_version=view.profile.version, application_url=job.official_application_url,
        platform=job.application_platform, portfolio_links=links, cover_letter_text=letter,
        recruiter_message_draft=recruiter_message(view, job),
        next_action="Review prepared materials",
    )
    session.add(app)
    session.flush()
    session.add(S_event(app, None, S.PREPARED, "system", "Application prepared"))
    questions, seen = [], set()
    for q in DEFAULT_QUESTIONS + list(job.application_questions or []):
        text = q["question"] if isinstance(q, dict) else q
        if text not in seen:
            seen.add(text)
            questions.append(q)
    for q in questions:
        q_text = q["question"] if isinstance(q, dict) else q
        required = q.get("required", True) if isinstance(q, dict) else True
        d = answer_question(q_text, view, confirmed_answers, required)
        session.add(ApplicationAnswer(application_id=app.id, question=d.question, answer=d.answer,
                                      source=d.source, confirmed_by_user=d.source == "user_confirmed",
                                      sensitive=d.sensitive, needs_user_input=d.needs_user_input,
                                      required=d.required))
    session.flush()
    session.refresh(app)
    audit(session, "application_prepared", entity_type="application", entity_id=app.id, job_id=job.id,
          cv_version=variant.version_id)
    refresh_readiness(session, app)
    return app


def S_event(app, from_status, to_status, source, note):
    from careerpilot.storage.models import ApplicationStatusEvent

    return ApplicationStatusEvent(application_id=app.id, from_status=from_status, to_status=to_status,
                                  source=source, note=note)


def refresh_readiness(session: Session, app: Application) -> None:
    """Move between PREPARED / NEEDS_USER_INPUT / READY_TO_SUBMIT based on answers and CV."""
    if app.status not in {S.PREPARED, S.NEEDS_USER_INPUT}:
        return
    pending = [a for a in app.answers if (a.required and (a.needs_user_input or not a.answer))
               or (a.required and a.sensitive and not a.confirmed_by_user)]
    if pending:
        if app.status != S.NEEDS_USER_INPUT:
            S.transition(session, app, S.NEEDS_USER_INPUT, source="system",
                         note="Needs the candidate: " + "; ".join(a.question for a in pending))
        app.next_action = "Answer: " + "; ".join(a.question for a in pending)
        return
    if not (app.resume_variant and app.resume_variant.valid):
        S.transition(session, app, S.BLOCKED, source="system", note="CV variant failed validation")
        return
    freeze_materials(session, app)
    S.transition(session, app, S.READY_TO_SUBMIT, source="system", note="Materials complete and frozen")
    app.next_action = "Submit (automatic only when every rule passes) or apply via the official link"


def provide_answer(session: Session, app: Application, question: str, answer: str) -> None:
    """The candidate's own answer. Recorded as user-confirmed."""
    row = next((a for a in app.answers if a.question == question), None)
    if row is None:
        row = ApplicationAnswer(application_id=app.id, question=question)
        session.add(row)
    row.answer = answer
    row.source = "user_confirmed"
    row.confirmed_by_user = True
    row.needs_user_input = False
    audit(session, "answer_confirmed", actor="user", entity_type="application", entity_id=app.id,
          question=question)
    session.flush()
    session.refresh(app)
    refresh_readiness(session, app)


def freeze_materials(session: Session, app: Application) -> None:
    """Copy the exact documents into an immutable per-application snapshot and lock the CV variant."""
    if app.materials:
        return
    settings_dir = Path(app.resume_variant.docx_path).parents[3]  # data/
    snap_dir = settings_dir / "snapshots" / app.id
    snap_dir.mkdir(parents=True, exist_ok=True)
    v = app.resume_variant
    files = [("cv_docx", Path(v.docx_path))] + ([("cv_pdf", Path(v.pdf_path))] if v.pdf_path else [])
    for kind, src in files:
        dest = snap_dir / src.name
        shutil.copy2(src, dest)
        make_read_only(dest)
        digest = sha256_file(dest)
        expected = v.docx_sha256 if kind == "cv_docx" else v.pdf_sha256
        if digest != expected:
            raise RuntimeError(f"Snapshot hash mismatch for {src.name}")
        session.add(ApplicationMaterial(application_id=app.id, kind=kind, filename=src.name,
                                        snapshot_path=str(dest), sha256=digest, version_ref=v.version_id,
                                        master_sha256=v.master.sha256))
    if app.cover_letter_text:
        letter = snap_dir / "cover_letter.txt"
        letter.write_text(app.cover_letter_text, encoding="utf-8")
        make_read_only(letter)
        session.add(ApplicationMaterial(application_id=app.id, kind="cover_letter", filename=letter.name,
                                        snapshot_path=str(letter), sha256=sha256_file(letter),
                                        version_ref=f"{app.id}-letter-1"))
    if not v.locked:
        v.locked = True
        v.locked_at = utcnow()
    for a in app.answers:
        if not a.frozen:
            a.frozen = True
    session.flush()
    session.refresh(app)
    audit(session, "materials_frozen", entity_type="application", entity_id=app.id, cv_version=v.version_id,
          cv_sha256=v.docx_sha256)


def _package(app: Application) -> SubmissionPackage:
    v = app.resume_variant
    cv = next((m for m in app.materials if m.kind == "cv_docx"), None)
    return SubmissionPackage(
        application_id=app.id, idempotency_key=app.idempotency_key,
        target_url=app.application_url or app.job.official_application_url or "",
        employer=app.job.employer, job_title=app.job.title,
        cv_path=cv.snapshot_path if cv else v.docx_path, cv_sha256=cv.sha256 if cv else v.docx_sha256,
        cover_letter=app.cover_letter_text,
        answers={a.question: a.answer for a in app.answers if a.answer},
    )


def _record_attempt(session: Session, settings: Settings, app: Application, adapter: ApplicationAdapter, *,
                    simulated: bool, result: str, certainty: str, decision, confirmation_ref=None,
                    evidence=None, error=None, restriction=None, retry_reason=None) -> ApplicationAttempt:
    now = utcnow()
    tz = ZoneInfo(settings.timezone)
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).isoformat(timespec="seconds")
    attempt = ApplicationAttempt(
        application_id=app.id, attempted_at=now, timezone=settings.timezone, local_time=local,
        method="simulated" if simulated else adapter.method, target_url=_package(app).target_url,
        employer=app.job.employer, adapter_name=adapter.name, adapter_version=adapter.version,
        simulated=simulated, result=result, certainty=certainty, confirmation_ref=confirmation_ref,
        evidence=evidence or {}, error=error, automation_restriction=restriction, retry_reason=retry_reason,
        authorization=decision.as_dict() if decision else {},
    )
    session.add(attempt)
    session.flush()
    session.refresh(app)
    audit(session, "application_attempt", entity_type="application", entity_id=app.id, attempt=attempt.id,
          simulated=simulated, result=result)
    return attempt


def submit_application(session: Session, settings: Settings, app: Application,
                       adapters: list[ApplicationAdapter], *, retry_reason: str | None = None) -> ApplicationAttempt:
    """Run the deterministic gate. In DRY_RUN, or when any check fails, nothing is sent."""
    adapter = pick_adapter(app.application_url or app.job.official_application_url, adapters)

    # Safe retries: an uncertain earlier attempt must be resolved before anything is resent.
    last = _latest_real_attempt(app)
    if last is not None and last.result == "UNCERTAIN":
        receipt = adapter.check_receipt(_package(app))
        if receipt.found:
            attempt = _record_attempt(session, settings, app, adapter, simulated=False, result="CONFIRMED",
                                      certainty="confirmed", decision=None, evidence=receipt.evidence,
                                      confirmation_ref=receipt.confirmation_ref,
                                      retry_reason="receipt check after uncertain attempt; not resubmitted")
            _apply_outcome(session, app, "CONFIRMED", adapter, receipt.evidence, "receipt found after uncertain attempt")
            return attempt
        if receipt.found is None:
            attempt = _record_attempt(session, settings, app, adapter, simulated=False, result="BLOCKED",
                                      certainty="uncertain", decision=None,
                                      error="cannot verify whether the earlier attempt was received",
                                      retry_reason="retry refused until receipt is checked")
            if app.status == S.SUBMISSION_ATTEMPTED:
                S.transition(session, app, S.NEEDS_USER_INPUT, source="system",
                             note="Check your email or the employer portal for a receipt before retrying")
            return attempt
        # Receipt definitely not found: the earlier attempt failed, so a retry is safe.
        if app.status == S.SUBMISSION_ATTEMPTED:
            S.transition(session, app, S.FAILED, source="system", note="No receipt found for uncertain attempt")
            S.transition(session, app, S.READY_TO_SUBMIT, source="system", note="Safe to retry")
        retry_reason = retry_reason or "earlier attempt uncertain; receipt check found none"

    decision = authorize(session, settings, app, adapter)

    if not decision.application_ready:
        failed = "; ".join(f"{c.name}: {c.detail}" for c in decision.failed if not c.name.startswith("global_"))
        restriction = "manual_submission_only" if isinstance(adapter, AssistedManualAdapter) else None
        return _record_attempt(session, settings, app, adapter, simulated=settings.dry_run,
                               result="NOT_AUTHORIZED", certainty="failed", decision=decision,
                               error=failed, restriction=restriction, retry_reason=retry_reason)

    if not decision.real_submission_allowed:
        # Global switches (DRY_RUN, kill switch) block it: simulate only. The adapter is never called.
        return _record_attempt(session, settings, app, adapter, simulated=True, result="SIMULATED",
                               certainty="simulated", decision=decision,
                               evidence={"note": "Dry run: every application check passed; nothing was sent"},
                               retry_reason=retry_reason)

    S.transition(session, app, S.SUBMISSION_ATTEMPTED, source=f"adapter:{adapter.name}")
    try:
        outcome = adapter.submit(_package(app))
    except Exception as exc:  # unknown state: treat as uncertain, never as failed-and-retry
        from careerpilot.applications.adapters import SubmissionOutcome

        outcome = SubmissionOutcome("UNCERTAIN", error=f"{type(exc).__name__}: {exc}"[:500])
    certainty = {"CONFIRMED": "confirmed", "SUBMITTED": "uncertain", "UNCERTAIN": "uncertain"}.get(
        outcome.result, "failed")
    attempt = _record_attempt(session, settings, app, adapter, simulated=False, result=outcome.result,
                              certainty=certainty, decision=decision, confirmation_ref=outcome.confirmation_ref,
                              evidence=outcome.evidence, error=outcome.error,
                              restriction=outcome.automation_restriction, retry_reason=retry_reason)
    _apply_outcome(session, app, outcome.result, adapter, outcome.evidence, outcome.error)
    return attempt


def _apply_outcome(session: Session, app: Application, result: str, adapter, evidence, note) -> None:
    src = f"adapter:{adapter.name}"
    if result == "CONFIRMED":
        S.transition(session, app, S.CONFIRMED, source=src, evidence=evidence or {"result": result}, note=note)
        _schedule_follow_up(session, app)
    elif result == "SUBMITTED":
        S.transition(session, app, S.SUBMITTED, source=src, evidence=evidence or {"result": result}, note=note)
        _schedule_follow_up(session, app)
    elif result == "BLOCKED":
        S.transition(session, app, S.BLOCKED, source=src, note=note or "automation blocked")
        app.next_action = f"Apply manually: {app.application_url or app.job.official_application_url}"
    elif result == "FAILED":
        S.transition(session, app, S.FAILED, source=src, note=note)
    # UNCERTAIN stays SUBMISSION_ATTEMPTED until a receipt check resolves it.


def _schedule_follow_up(session: Session, app: Application) -> None:
    due = utcnow() + timedelta(days=FOLLOW_UP_DAYS)
    session.add(FollowUpReminder(application_id=app.id, due_at=due,
                                 reason="No response yet: follow up or check the portal"))
    app.next_action = "Awaiting update"
    app.next_action_due = due


def record_manual_submission(session: Session, app: Application, note: str) -> None:
    """The candidate applied through the official link and says so."""
    if app.status in S.SENT_STATUSES:
        raise S.InvalidTransition("Already recorded as submitted")
    if not app.materials and app.resume_variant and app.resume_variant.valid:
        freeze_materials(session, app)
    S.transition(session, app, S.SUBMITTED, source="user", note=note,
                 evidence={"reported_by": "candidate", "note": note})
    _schedule_follow_up(session, app)


def record_outcome(session: Session, app: Application, status: str, note: str | None = None,
                   source: str = "user") -> None:
    S.transition(session, app, status, source=source, note=note)
    if status in {S.REJECTED, S.WITHDRAWN, S.OFFER}:
        for r in app.reminders:
            if not r.done:
                r.done = True
                r.done_at = utcnow()
    if status == S.INTERVIEW:
        app.next_action = "Prepare for interview"


def mark_job_verified(session: Session, job: JobPosting, note: str) -> None:
    """The candidate checked the employer, role and official link themselves."""
    job.employer_verified = True
    job.last_verified_at = utcnow()
    audit(session, "job_verified_by_user", actor="user", entity_type="job", entity_id=job.id, note=note)
