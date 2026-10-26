"""Ingest raw jobs: normalize, deduplicate across sources, and track source health."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerpilot.config import Settings
from careerpilot.ingestion.normalize import NormalizedJob, normalize
from careerpilot.matching.tracks import classify_tracks
from careerpilot.profile.service import load_preferences
from careerpilot.sources.base import RawJob, SourceAdapter
from careerpilot.storage.audit import audit
from careerpilot.storage.models import JobPosting, JobSource, JobSourceListing, new_id, utcnow

TITLE_SIMILARITY = 0.9
SOURCE_CONFIDENCE = {"public_api": 0.9, "fixture": 0.8, "manual": 0.7, "email_alert": 0.5}


@dataclass
class IngestStats:
    source: str
    fetched: int = 0
    new_jobs: int = 0
    merged_duplicates: int = 0
    updated_listings: int = 0
    flagged_injection: int = 0
    errors: list[str] = field(default_factory=list)


def get_or_create_source(session: Session, adapter: SourceAdapter) -> JobSource:
    meta = adapter.get_source_metadata()
    src = session.scalars(select(JobSource).where(JobSource.name == meta.name)).first()
    if src is None:
        src = JobSource(name=meta.name, kind=meta.kind)
        session.add(src)
    src.access_restrictions = meta.access_restrictions
    src.requires_user_action = meta.requires_user_action
    src.meta = {"cost": meta.cost, "automation_permitted": meta.automation_permitted,
                "freshness": meta.freshness, "terms_url": meta.terms_url}
    session.flush()
    return src


def _find_existing_job(session: Session, src: JobSource, nj: NormalizedJob) -> tuple[JobPosting | None, JobSourceListing | None]:
    raw = nj.raw
    if raw.source_job_id:
        listing = session.scalars(select(JobSourceListing).where(
            JobSourceListing.source_id == src.id, JobSourceListing.source_job_id == raw.source_job_id)).first()
        if listing:
            return listing.job, listing
    if nj.canonical_url:
        listing = session.scalars(select(JobSourceListing).where(
            JobSourceListing.canonical_url == nj.canonical_url)).first()
        if listing:
            return listing.job, (listing if listing.source_id == src.id else None)
    job = session.scalars(select(JobPosting).where(JobPosting.dedupe_key == nj.dedupe_key)).first()
    if job:
        return job, None
    # Same employer, near-identical title, compatible location: the same position.
    for candidate in session.scalars(select(JobPosting).where(
            JobPosting.employer_normalized == nj.employer_normalized)):
        ratio = difflib.SequenceMatcher(None, candidate.title_normalized, nj.title_normalized).ratio()
        loc_ok = (not candidate.location_normalized or not nj.location_normalized
                  or candidate.location_normalized == nj.location_normalized)
        if ratio >= TITLE_SIMILARITY and loc_ok:
            return candidate, None
    return None, None


def ingest_jobs(session: Session, settings: Settings, adapter: SourceAdapter, raws: list[RawJob],
                stats: IngestStats) -> list[JobPosting]:
    src = get_or_create_source(session, adapter)
    prefs = load_preferences(settings)
    meta = adapter.get_source_metadata()
    touched: list[JobPosting] = []
    now = utcnow()
    for raw in raws:
        stats.fetched += 1
        nj = normalize(raw)
        if nj.injection_flags:
            stats.flagged_injection += 1
        job, listing = _find_existing_job(session, src, nj)
        if job is None:
            track, track_scores = classify_tracks(nj.title, nj.description_normalized, prefs)
            job = JobPosting(
                id=new_id("job"), dedupe_key=nj.dedupe_key, employer=nj.employer,
                employer_normalized=nj.employer_normalized, title=nj.title,
                title_normalized=nj.title_normalized, description_original=nj.description_original,
                description_normalized=nj.description_normalized, language=nj.language,
                location=nj.location, location_normalized=nj.location_normalized,
                work_arrangement=nj.work_arrangement, office_days=nj.office_days,
                salary_min=nj.salary_min, salary_max=nj.salary_max, salary_currency=nj.salary_currency,
                salary_period=nj.salary_period, employment_type=nj.employment_type,
                seniority=nj.seniority, years_required=nj.years_required,
                injection_flags=nj.injection_flags,
                application_questions=list(raw.extra.get("application_questions") or []),
                posted_at=nj.posted_at, first_discovered_at=now, last_verified_at=now,
                career_track=track, career_track_scores=track_scores,
                source_confidence=SOURCE_CONFIDENCE.get(meta.kind, 0.5),
                official_application_url=nj.application_url, application_platform=nj.application_platform,
                employer_verified=meta.kind == "public_api",
            )
            job.duplicate_group_id = job.id
            session.add(job)
            session.flush()
            stats.new_jobs += 1
            audit(session, "job_discovered", entity_type="job", entity_id=job.id, source=src.name,
                  title=job.title, employer=job.employer)
        else:
            if listing is None:
                stats.merged_duplicates += 1
                audit(session, "job_duplicate_merged", entity_type="job", entity_id=job.id, source=src.name)
            job.last_verified_at = now
            job.expiration_status = "active"
            # Prefer the employer's own application link over a job-board link.
            if nj.application_platform not in {"linkedin", "alljobs", "drushim", "jobmaster", "indeed"} and \
                    job.application_platform in {None, "linkedin", "alljobs", "drushim", "jobmaster", "indeed"}:
                job.official_application_url = nj.application_url or job.official_application_url
                job.application_platform = nj.application_platform or job.application_platform
            if len(nj.description_original) > len(job.description_original or ""):
                job.description_original = nj.description_original
                job.description_normalized = nj.description_normalized
            job.source_confidence = max(job.source_confidence, SOURCE_CONFIDENCE.get(meta.kind, 0.5))
            if meta.kind == "public_api":
                job.employer_verified = True
        if listing is None:
            listing = JobSourceListing(
                job_id=job.id, source_id=src.id, source_job_id=raw.source_job_id, source_url=raw.source_url,
                canonical_url=nj.canonical_url if nj.canonical_url and not session.scalars(
                    select(JobSourceListing.id).where(JobSourceListing.canonical_url == nj.canonical_url)).first()
                else None,
                raw_snapshot={"title": raw.title, "employer": raw.employer, "location": raw.location,
                              "description": raw.description[:20000], "salary_text": raw.salary_text},
            )
            session.add(listing)
        else:
            stats.updated_listings += 1
            listing.last_seen_at = now
            listing.active = True
        session.flush()
        touched.append(job)
    return touched


def run_source(session: Session, settings: Settings, adapter: SourceAdapter) -> IngestStats:
    """Fetch from one source, recording health. A failing source never stops the others."""
    stats = IngestStats(source=adapter.get_source_metadata().name)
    src = get_or_create_source(session, adapter)
    src.last_run_at = utcnow()
    try:
        raws = adapter.search_jobs()
    except Exception as exc:
        src.operational = False
        src.consecutive_failures += 1
        src.last_error = str(exc)[:500]
        stats.errors.append(str(exc))
        audit(session, "source_failed", entity_type="job_source", entity_id=src.id, error=str(exc)[:500])
        return stats
    ingest_jobs(session, settings, adapter, raws, stats)
    seen_ids = {r.source_job_id for r in raws if r.source_job_id}
    # Only full-board sources can tell us a posting disappeared; manual imports are partial.
    if seen_ids and adapter.get_source_metadata().kind == "public_api":
        for listing in session.scalars(select(JobSourceListing).where(
                JobSourceListing.source_id == src.id, JobSourceListing.active.is_(True))):
            if listing.source_job_id and listing.source_job_id not in seen_ids:
                listing.active = False  # disappeared from this source; history kept
                job = listing.job
                if not any(item.active for item in job.listings):
                    job.expiration_status = "expired"
    src.operational = True
    src.consecutive_failures = 0
    src.last_error = None
    src.last_success_at = utcnow()
    return stats
