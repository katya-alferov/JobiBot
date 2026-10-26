"""Audit log helper. Never pass secrets, cookies or passwords in details."""

from __future__ import annotations

from sqlalchemy.orm import Session

from careerpilot.storage.models import AuditEvent

_FORBIDDEN_KEYS = {"password", "cookie", "cookies", "token", "authorization", "secret", "session"}


def _scrub(details: dict) -> dict:
    clean = {}
    for k, v in (details or {}).items():
        if any(f in k.lower() for f in _FORBIDDEN_KEYS):
            clean[k] = "[redacted]"
        elif isinstance(v, dict):
            clean[k] = _scrub(v)
        else:
            clean[k] = v
    return clean


def audit(session: Session, action: str, *, actor: str = "system", entity_type: str | None = None,
          entity_id: str | None = None, **details) -> AuditEvent:
    event = AuditEvent(actor=actor, action=action, entity_type=entity_type,
                       entity_id=str(entity_id) if entity_id is not None else None,
                       details=_scrub(details))
    session.add(event)
    return event
