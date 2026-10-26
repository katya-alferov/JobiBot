"""Application history: lookups, statistics, exports, backup and deletion."""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerpilot.applications.state import CONFIRMED, INTERVIEW, OFFER, SENT_STATUSES, display_status
from careerpilot.config import Settings
from careerpilot.storage.audit import audit
from careerpilot.storage.models import (
    Application,
    ApplicationAnswer,
    ApplicationAttempt,
    ApplicationMaterial,
    ApplicationStatusEvent,
    FollowUpReminder,
    JobSourceListing,
    ResumeChange,
    ResumeVariant,
    utcnow,
)


def application_record(app: Application) -> dict:
    """Everything needed to answer 'what exactly did I send, when, and what happened?'."""
    job = app.job
    v = app.resume_variant
    cv = next((m for m in app.materials if m.kind == "cv_docx"), None)
    real_attempts = [a for a in app.attempts if not a.simulated]
    return {
        "application_id": app.id,
        "employer": job.employer,
        "job_title": job.title,
        "career_track": app.track,
        "match_score": app.match_score,
        "status": app.status,
        "status_display": display_status(app),
        "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
        "approved_by_you_at": app.user_approved_at.isoformat() if app.user_approved_at else None,
        "confirmed": app.status == CONFIRMED or any(a.result == "CONFIRMED" for a in real_attempts),
        "submission_method": real_attempts[-1].method if real_attempts else None,
        "source_names": sorted({lst.source.name for lst in job.listings}),
        "source_urls": [lst.source_url for lst in job.listings if lst.source_url],
        "found_on_multiple_sources": len({lst.source_id for lst in job.listings}) > 1,
        "official_application_url": job.official_application_url,
        "ats_platform": job.application_platform,
        "location": job.location,
        "work_arrangement": job.work_arrangement,
        "cv_version_id": v.version_id if v else None,
        "cv_filename": cv.filename if cv else (v.docx_filename if v else None),
        "cv_snapshot_path": cv.snapshot_path if cv else None,
        "cv_sha256": cv.sha256 if cv else None,
        "master_cv_sha256": cv.master_sha256 if cv else None,
        "cv_changes": [
            {"section": c.section, "type": c.change_type, "original": c.original_text, "revised": c.revised_text,
             "reason": c.reason, "source": c.fact_source, "material": c.material}
            for c in (v.changes if v else [])
        ],
        "cover_letter": app.cover_letter_text,
        "answers": [
            {"question": a.question, "answer": a.answer, "source": a.source, "confirmed_by_user": a.confirmed_by_user,
             "sensitive": a.sensitive, "needs_user_input": a.needs_user_input}
            for a in app.answers
        ],
        "attempts": [
            {"attempt_id": a.id, "at_utc": a.attempted_at.isoformat(), "local_time": a.local_time,
             "timezone": a.timezone, "simulated": a.simulated, "method": a.method, "adapter": a.adapter_name,
             "adapter_version": a.adapter_version, "result": a.result, "certainty": a.certainty,
             "confirmation_ref": a.confirmation_ref, "evidence": a.evidence, "error": a.error,
             "restriction": a.automation_restriction, "retry_reason": a.retry_reason}
            for a in app.attempts
        ],
        "timeline": [
            {"at": e.at.isoformat(), "from": e.from_status, "to": e.to_status, "source": e.source, "note": e.note}
            for e in app.status_events
        ],
        "follow_ups": [
            {"due": r.due_at.isoformat(), "reason": r.reason, "done": r.done} for r in app.reminders
        ],
        "next_action": app.next_action,
        "next_action_due": app.next_action_due.isoformat() if app.next_action_due else None,
        "profile_version": app.profile_version,
        "user_notes": app.user_notes,
    }


def all_applications(session: Session) -> list[Application]:
    return list(session.scalars(select(Application).order_by(Application.created_at.desc())))


def already_applied(session: Session, employer: str, title: str | None = None) -> list[dict]:
    out = []
    for app in all_applications(session):
        if employer.lower() in app.job.employer.lower() and (not title or title.lower() in app.job.title.lower()):
            out.append({"application_id": app.id, "job_title": app.job.title, "status": app.status,
                        "sent": app.status in SENT_STATUSES})
    return out


def stats(session: Session) -> dict:
    apps = all_applications(session)
    attempts = list(session.scalars(select(ApplicationAttempt)))
    real = [a for a in attempts if not a.simulated]
    by = {"track": defaultdict(lambda: [0, 0]), "employer": defaultdict(lambda: [0, 0]),
          "cv_version": defaultdict(lambda: [0, 0])}
    for app in apps:
        if app.status not in SENT_STATUSES:
            continue
        reached = any(e.to_status in {INTERVIEW, OFFER} for e in app.status_events)
        for dim, key in (("track", app.track), ("employer", app.job.employer),
                         ("cv_version", app.resume_variant.version_id if app.resume_variant else "none")):
            by[dim][key][0] += 1
            by[dim][key][1] += int(reached)
    rate = {dim: {k: {"sent": s, "interviews": i, "interview_rate": round(i / s, 3) if s else None}
                  for k, (s, i) in d.items()} for dim, d in by.items()}
    return {
        "applications": len(apps),
        "submitted": sum(1 for a in apps if a.status in SENT_STATUSES),
        "confirmed": sum(1 for a in apps if a.status == CONFIRMED
                         or any(t.result == "CONFIRMED" for t in a.attempts if not t.simulated)),
        "failed_attempts": sum(1 for a in real if a.result == "FAILED"),
        "blocked_attempts": sum(1 for a in attempts if a.result in {"BLOCKED", "NOT_AUTHORIZED"}),
        "simulated_attempts": sum(1 for a in attempts if a.simulated),
        "real_attempts": len(real),
        "interview_rate": rate,
    }


def due_follow_ups(session: Session, now: datetime | None = None) -> list[FollowUpReminder]:
    now = now or utcnow()
    return list(session.scalars(select(FollowUpReminder).where(
        FollowUpReminder.done.is_(False), FollowUpReminder.due_at <= now)))


def export_history(session: Session, out_dir: Path, fmt: str = "both") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    records = [application_record(a) for a in all_applications(session)]
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    paths = []
    if fmt in {"json", "both"}:
        p = out_dir / f"applications-{stamp}.json"
        p.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.append(p)
    if fmt in {"csv", "both"}:
        p = out_dir / f"applications-{stamp}.csv"
        flat_keys = ["application_id", "employer", "job_title", "career_track", "match_score", "status",
                     "submitted_at", "confirmed", "submission_method", "ats_platform", "official_application_url",
                     "cv_version_id", "cv_filename", "cv_sha256", "found_on_multiple_sources", "next_action"]
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=flat_keys + ["source_names", "attempt_count", "simulated_only"])
            w.writeheader()
            for r in records:
                row = {k: r[k] for k in flat_keys}
                row["source_names"] = "; ".join(r["source_names"])
                row["attempt_count"] = len(r["attempts"])
                row["simulated_only"] = all(a["simulated"] for a in r["attempts"]) if r["attempts"] else None
                w.writerow(row)
        paths.append(p)
    audit(session, "history_exported", paths=[str(p) for p in paths])
    return paths


def backup(settings: Settings) -> Path:
    """Consistent copy of the SQLite database plus document snapshots."""
    url = settings.resolved_database_url()
    if not url.startswith("sqlite:///"):
        raise RuntimeError("Backup command supports SQLite only; use pg_dump for PostgreSQL")
    db_path = Path(url.removeprefix("sqlite:///"))
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    dest = settings.data_dir / "backups" / stamp
    dest.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest / "careerpilot.db")
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for sub in ("snapshots", "cv"):
        if (settings.data_dir / sub).exists():
            shutil.copytree(settings.data_dir / sub, dest / sub, dirs_exist_ok=True)
    return dest


def delete_history(session: Session, settings: Settings, *, confirm: str) -> dict:
    """Permanently delete personal application history. Requires confirm='DELETE MY HISTORY'."""
    if confirm != "DELETE MY HISTORY":
        raise PermissionError("Type DELETE MY HISTORY to confirm")
    session.info["allow_history_deletion"] = True
    counts = {}
    try:
        for model in (ApplicationAnswer, ApplicationMaterial, ApplicationAttempt, ApplicationStatusEvent,
                      FollowUpReminder):
            rows = list(session.scalars(select(model)))
            counts[model.__tablename__] = len(rows)
            for r in rows:
                session.delete(r)
        session.flush()
        apps = list(session.scalars(select(Application)))
        counts["application"] = len(apps)
        for a in apps:
            session.delete(a)
        session.flush()
        for model in (ResumeChange, ResumeVariant):
            rows = list(session.scalars(select(model)))
            counts[model.__tablename__] = len(rows)
            for r in rows:
                session.delete(r)
        session.flush()
    finally:
        session.info.pop("allow_history_deletion", None)
    for sub in ("snapshots", "cv/variants"):
        path = settings.data_dir / sub
        if path.exists():
            for p in path.rglob("*"):
                if p.is_file():
                    p.chmod(0o600)
            shutil.rmtree(path)
            path.mkdir(parents=True)
    audit(session, "history_deleted", actor="user", counts=counts)
    return counts


def listings_for(session: Session, job_id: str) -> list[JobSourceListing]:
    return list(session.scalars(select(JobSourceListing).where(JobSourceListing.job_id == job_id)))
