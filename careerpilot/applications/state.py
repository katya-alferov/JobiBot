"""Application lifecycle states and the transitions allowed between them."""

from __future__ import annotations

from sqlalchemy.orm import Session

from careerpilot.storage.audit import audit
from careerpilot.storage.models import Application, ApplicationStatusEvent, utcnow

PREPARED = "PREPARED"
READY_TO_SUBMIT = "READY_TO_SUBMIT"
SUBMISSION_ATTEMPTED = "SUBMISSION_ATTEMPTED"
SUBMITTED = "SUBMITTED"
CONFIRMED = "CONFIRMED"
NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
BLOCKED = "BLOCKED"
FAILED = "FAILED"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"
WITHDRAWN = "WITHDRAWN"
INTERVIEW = "INTERVIEW"
OFFER = "OFFER"

ALL_STATUSES = [PREPARED, READY_TO_SUBMIT, SUBMISSION_ATTEMPTED, SUBMITTED, CONFIRMED, NEEDS_USER_INPUT,
                BLOCKED, FAILED, EXPIRED, REJECTED, WITHDRAWN, INTERVIEW, OFFER]
SENT_STATUSES = {SUBMITTED, CONFIRMED, INTERVIEW, OFFER, REJECTED}

TRANSITIONS: dict[str, set[str]] = {
    PREPARED: {READY_TO_SUBMIT, NEEDS_USER_INPUT, BLOCKED, EXPIRED, WITHDRAWN},
    NEEDS_USER_INPUT: {PREPARED, READY_TO_SUBMIT, BLOCKED, EXPIRED, WITHDRAWN},
    READY_TO_SUBMIT: {SUBMISSION_ATTEMPTED, NEEDS_USER_INPUT, BLOCKED, EXPIRED, WITHDRAWN},
    SUBMISSION_ATTEMPTED: {SUBMITTED, CONFIRMED, FAILED, BLOCKED, NEEDS_USER_INPUT},
    SUBMITTED: {CONFIRMED, REJECTED, INTERVIEW, WITHDRAWN, OFFER},
    CONFIRMED: {REJECTED, INTERVIEW, WITHDRAWN, OFFER},
    INTERVIEW: {INTERVIEW, OFFER, REJECTED, WITHDRAWN},
    OFFER: {WITHDRAWN, REJECTED},
    FAILED: {READY_TO_SUBMIT, BLOCKED, NEEDS_USER_INPUT, WITHDRAWN, EXPIRED},
    BLOCKED: {READY_TO_SUBMIT, NEEDS_USER_INPUT, WITHDRAWN, EXPIRED},
    EXPIRED: {WITHDRAWN},
    REJECTED: set(),
    WITHDRAWN: set(),
}
# The candidate may record a manual application (assisted flow) with their own evidence note.
USER_MANUAL_SUBMIT_FROM = {PREPARED, READY_TO_SUBMIT, NEEDS_USER_INPUT, BLOCKED, FAILED}


class InvalidTransition(ValueError):
    pass


def transition(session: Session, app: Application, to_status: str, *, source: str, note: str | None = None,
               evidence: dict | None = None) -> ApplicationStatusEvent:
    if to_status not in ALL_STATUSES:
        raise InvalidTransition(f"Unknown status {to_status}")
    current = app.status
    allowed = TRANSITIONS.get(current, set())
    manual_ok = source == "user" and to_status == SUBMITTED and current in USER_MANUAL_SUBMIT_FROM
    if to_status not in allowed and not manual_ok:
        raise InvalidTransition(f"{current} -> {to_status} is not allowed")
    if to_status in {SUBMITTED, CONFIRMED} and not (evidence or note):
        raise InvalidTransition(f"{to_status} requires evidence")
    event = ApplicationStatusEvent(application_id=app.id, from_status=current, to_status=to_status,
                                   source=source, note=note, evidence=evidence or {})
    session.add(event)
    app.status = to_status
    app.status_updated_at = utcnow()
    if to_status in {SUBMITTED, CONFIRMED} and app.submitted_at is None:
        app.submitted_at = utcnow()
    audit(session, "application_status_changed", actor=source, entity_type="application", entity_id=app.id,
          from_status=current, to_status=to_status, note=note)
    return event


def display_status(app: Application) -> str:
    """What to show the candidate. Never invent progress that has no evidence."""
    if app.status in {SUBMITTED, CONFIRMED}:
        return f"{app.status} · Awaiting update"
    return app.status
