"""SQLAlchemy 2 models. SQLite locally; types and constraints stay PostgreSQL-compatible.

All timestamps are stored as naive UTC. The configured TIMEZONE is used for display and is
recorded on each application attempt.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ImmutableRecordError(RuntimeError):
    """Raised when code tries to change a record that must never change."""


# --------------------------------------------------------------------------- profile


class CandidateProfile(TimestampMixin, Base):
    __tablename__ = "candidate_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    personal_website_url: Mapped[str | None] = mapped_column(String(500))
    personal_website_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    linkedin_profile_url: Mapped[str | None] = mapped_column(String(500))
    linkedin_profile_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)

    facts: Mapped[list[VerifiedFact]] = relationship(back_populates="profile")


class VerifiedFact(TimestampMixin, Base):
    """One material fact about the candidate, with its source and verification status."""

    __tablename__ = "verified_fact"
    __table_args__ = (UniqueConstraint("profile_id", "category", "key", name="uq_fact_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profile.id"), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    # verified | needs_confirmation | rejected
    status: Mapped[str] = mapped_column(String(30), default="needs_confirmation", index=True)
    source: Mapped[str] = mapped_column(String(300))
    source_detail: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)

    profile: Mapped[CandidateProfile] = relationship(back_populates="facts")


class UserPreference(TimestampMixin, Base):
    __tablename__ = "user_preference"
    __table_args__ = (UniqueConstraint("profile_id", "track", "key", name="uq_pref_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profile.id"), index=True)
    track: Mapped[str] = mapped_column(String(40))  # "global" or a career track id
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[dict | list | str | int | float | None] = mapped_column(JSON, nullable=True)


# --------------------------------------------------------------------------- jobs


class JobSource(TimestampMixin, Base):
    __tablename__ = "job_source"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    operational: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    access_restrictions: Mapped[str | None] = mapped_column(Text)
    requires_user_action: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class JobPosting(TimestampMixin, Base):
    """One underlying position, possibly seen on several sources."""

    __tablename__ = "job_posting"
    __table_args__ = (
        Index("ix_job_employer_title", "employer_normalized", "title_normalized"),
        Index("ix_job_track_discovered", "career_track", "first_discovered_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("job"))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    duplicate_group_id: Mapped[str] = mapped_column(String(40), index=True)
    employer: Mapped[str] = mapped_column(String(300))
    employer_normalized: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300))
    title_normalized: Mapped[str] = mapped_column(String(300))
    description_original: Mapped[str] = mapped_column(Text, default="")
    description_normalized: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(10), default="en")
    location: Mapped[str | None] = mapped_column(String(300))
    location_normalized: Mapped[str | None] = mapped_column(String(100))
    work_arrangement: Mapped[str] = mapped_column(String(20), default="unknown")
    office_days: Mapped[int | None] = mapped_column(Integer)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(10))
    salary_period: Mapped[str | None] = mapped_column(String(20))
    employment_type: Mapped[str] = mapped_column(String(30), default="unknown")
    seniority: Mapped[str] = mapped_column(String(30), default="unknown")
    years_required: Mapped[int | None] = mapped_column(Integer)
    injection_flags: Mapped[list] = mapped_column(JSON, default=list)
    application_questions: Mapped[list] = mapped_column(JSON, default=list)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    expiration_status: Mapped[str] = mapped_column(String(20), default="active")
    career_track: Mapped[str] = mapped_column(String(40), default="other")
    career_track_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    source_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    official_application_url: Mapped[str | None] = mapped_column(String(1000))
    application_platform: Mapped[str | None] = mapped_column(String(50))
    employer_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    listings: Mapped[list[JobSourceListing]] = relationship(back_populates="job")
    matches: Mapped[list[JobMatch]] = relationship(back_populates="job")


class JobSourceListing(TimestampMixin, Base):
    """Where a job was seen. Never deleted when the posting disappears."""

    __tablename__ = "job_source_listing"
    __table_args__ = (
        UniqueConstraint("source_id", "source_job_id", name="uq_listing_source_job"),
        UniqueConstraint("canonical_url", name="uq_listing_canonical_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job_posting.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("job_source.id"), index=True)
    source_job_id: Mapped[str | None] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    canonical_url: Mapped[str | None] = mapped_column(String(1000))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)

    job: Mapped[JobPosting] = relationship(back_populates="listings")
    source: Mapped[JobSource] = relationship()


class JobMatch(TimestampMixin, Base):
    __tablename__ = "job_match"
    __table_args__ = (Index("ix_match_job_track", "job_id", "track"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job_posting.id"))
    track: Mapped[str] = mapped_column(String(40))
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    score: Mapped[float] = mapped_column(Float)
    component_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    weights: Mapped[dict] = mapped_column(JSON, default=dict)
    eligible: Mapped[bool] = mapped_column(Boolean)
    hard_filter_results: Mapped[list] = mapped_column(JSON, default=list)
    matching_skills: Mapped[list] = mapped_column(JSON, default=list)
    missing_requirements: Mapped[list] = mapped_column(JSON, default=list)
    relevant_experience: Mapped[list] = mapped_column(JSON, default=list)
    uncertainties: Mapped[list] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str] = mapped_column(String(10), default="low")
    recommendation: Mapped[str] = mapped_column(String(20))  # apply | review | reject
    recommendation_reason: Mapped[str] = mapped_column(Text, default="")
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    job: Mapped[JobPosting] = relationship(back_populates="matches")


# --------------------------------------------------------------------------- resumes


class ResumeMaster(TimestampMixin, Base):
    __tablename__ = "resume_master"

    id: Mapped[int] = mapped_column(primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str] = mapped_column(Text, default="")


class ResumeVariant(TimestampMixin, Base):
    __tablename__ = "resume_variant"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[str] = mapped_column(String(60), unique=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("resume_master.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("job_posting.id"), index=True)
    track: Mapped[str] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(10), default="en")
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    docx_filename: Mapped[str] = mapped_column(String(300))
    docx_path: Mapped[str] = mapped_column(String(1000))
    docx_sha256: Mapped[str] = mapped_column(String(64))
    pdf_path: Mapped[str | None] = mapped_column(String(1000))
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)

    master: Mapped[ResumeMaster] = relationship()
    changes: Mapped[list[ResumeChange]] = relationship(back_populates="variant")


class ResumeChange(Base):
    __tablename__ = "resume_change"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("resume_variant.id"), index=True)
    section: Mapped[str] = mapped_column(String(100))
    change_type: Mapped[str] = mapped_column(String(40))  # reorder | reword | omit | add_link
    original_text: Mapped[str] = mapped_column(Text, default="")
    revised_text: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text)
    fact_source: Mapped[str | None] = mapped_column(String(300))
    material: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    variant: Mapped[ResumeVariant] = relationship(back_populates="changes")


# --------------------------------------------------------------------------- applications


class Application(TimestampMixin, Base):
    __tablename__ = "application"
    __table_args__ = (
        UniqueConstraint("profile_id", "job_id", name="uq_application_profile_job"),
        Index("ix_application_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("app"))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profile.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("job_posting.id"))
    track: Mapped[str] = mapped_column(String(40))
    match_score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="PREPARED")
    status_updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resume_variant_id: Mapped[int | None] = mapped_column(ForeignKey("resume_variant.id"))
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    submission_method: Mapped[str | None] = mapped_column(String(40))
    application_url: Mapped[str | None] = mapped_column(String(1000))
    platform: Mapped[str | None] = mapped_column(String(50))
    portfolio_links: Mapped[dict] = mapped_column(JSON, default=dict)
    cover_letter_text: Mapped[str | None] = mapped_column(Text)
    recruiter_message_draft: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_action: Mapped[str | None] = mapped_column(Text)
    next_action_due: Mapped[datetime | None] = mapped_column(DateTime)
    recruiter_contact: Mapped[str | None] = mapped_column(Text)
    user_notes: Mapped[str | None] = mapped_column(Text)

    job: Mapped[JobPosting] = relationship()
    resume_variant: Mapped[ResumeVariant | None] = relationship()
    materials: Mapped[list[ApplicationMaterial]] = relationship(back_populates="application")
    answers: Mapped[list[ApplicationAnswer]] = relationship(back_populates="application")
    attempts: Mapped[list[ApplicationAttempt]] = relationship(
        back_populates="application", order_by="ApplicationAttempt.attempted_at"
    )
    status_events: Mapped[list[ApplicationStatusEvent]] = relationship(
        back_populates="application", order_by="ApplicationStatusEvent.id"
    )
    reminders: Mapped[list[FollowUpReminder]] = relationship(back_populates="application")


class ApplicationMaterial(Base):
    """An immutable snapshot of a document attached to an application."""

    __tablename__ = "application_material"
    __table_args__ = (UniqueConstraint("application_id", "kind", "sha256", name="uq_material"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("application.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # cv_docx | cv_pdf | cover_letter | attachment
    filename: Mapped[str] = mapped_column(String(300))
    snapshot_path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    version_ref: Mapped[str | None] = mapped_column(String(60))
    master_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="materials")


class ApplicationAnswer(TimestampMixin, Base):
    __tablename__ = "application_answer"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("application.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(300))
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_user_input: Mapped[bool] = mapped_column(Boolean, default=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)

    application: Mapped[Application] = relationship(back_populates="answers")


class ApplicationAttempt(Base):
    """Append-only record of one submission attempt (real or simulated)."""

    __tablename__ = "application_attempt"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("att"))
    application_id: Mapped[str] = mapped_column(ForeignKey("application.id"), index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    timezone: Mapped[str] = mapped_column(String(60))
    local_time: Mapped[str] = mapped_column(String(40))
    method: Mapped[str] = mapped_column(String(40))
    target_url: Mapped[str | None] = mapped_column(String(1000))
    employer: Mapped[str] = mapped_column(String(300))
    adapter_name: Mapped[str] = mapped_column(String(100))
    adapter_version: Mapped[str] = mapped_column(String(20))
    simulated: Mapped[bool] = mapped_column(Boolean)
    # SIMULATED | SUBMITTED | CONFIRMED | UNCERTAIN | FAILED | BLOCKED | NOT_AUTHORIZED
    result: Mapped[str] = mapped_column(String(30))
    certainty: Mapped[str] = mapped_column(String(20))  # confirmed | uncertain | failed | simulated
    confirmation_ref: Mapped[str | None] = mapped_column(String(200))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    automation_restriction: Mapped[str | None] = mapped_column(String(200))
    retry_reason: Mapped[str | None] = mapped_column(Text)
    authorization: Mapped[dict] = mapped_column(JSON, default=dict)

    application: Mapped[Application] = relationship(back_populates="attempts")


class ApplicationStatusEvent(Base):
    __tablename__ = "application_status_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("application.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source: Mapped[str] = mapped_column(String(100))  # system | user | adapter:<name> | email
    note: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)

    application: Mapped[Application] = relationship(back_populates="status_events")


class FollowUpReminder(TimestampMixin, Base):
    __tablename__ = "follow_up_reminder"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("application.id"), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    reason: Mapped[str] = mapped_column(Text)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)

    application: Mapped[Application] = relationship(back_populates="reminders")


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(100))  # system | user | scheduler
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(60), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class SchedulerRun(Base):
    __tablename__ = "scheduler_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(String(60))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|ok|partial|failed
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- immutability guards

_APPEND_ONLY = (ApplicationAttempt, ApplicationMaterial, AuditEvent, ApplicationStatusEvent)


@event.listens_for(Session, "before_flush")
def _enforce_immutability(session: Session, flush_context, instances) -> None:
    for obj in session.dirty:
        if not session.is_modified(obj, include_collections=False):
            continue
        if isinstance(obj, _APPEND_ONLY):
            raise ImmutableRecordError(f"{type(obj).__name__} records are append-only")
        state = inspect(obj)
        if isinstance(obj, ResumeVariant):
            was_locked = state.attrs.locked.history.deleted
            if (was_locked and was_locked[0]) or (obj.locked and not state.attrs.locked.history.has_changes()):
                raise ImmutableRecordError(
                    f"Resume variant {obj.version_id} is locked: it was used in an application"
                )
        if isinstance(obj, ApplicationAnswer):
            was_frozen = state.attrs.frozen.history.deleted
            if (was_frozen and was_frozen[0]) or (obj.frozen and not state.attrs.frozen.history.has_changes()):
                raise ImmutableRecordError("Submitted application answers cannot be changed")
    for obj in session.deleted:
        if isinstance(obj, _APPEND_ONLY + (ResumeVariant,)) and not session.info.get("allow_history_deletion"):
            raise ImmutableRecordError(
                f"{type(obj).__name__} history can only be removed by the explicit delete-history command"
            )
