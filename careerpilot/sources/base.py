"""Source adapter interface. Each adapter must use only free, permitted access methods."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawJob:
    """A job as a source reports it, before normalization. All text is untrusted input."""

    source_name: str
    source_job_id: str | None
    source_url: str | None
    employer: str
    title: str
    description: str = ""
    location: str | None = None
    application_url: str | None = None
    posted_at: datetime | None = None
    salary_text: str | None = None
    employment_type: str | None = None
    work_arrangement: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SourceMetadata:
    name: str
    kind: str  # public_api | manual | email_alert | fixture
    cost: str  # must be "free"
    automation_permitted: bool
    access_restrictions: str
    requires_user_action: bool
    freshness: str
    terms_url: str | None = None


@dataclass
class VerificationResult:
    still_open: bool | None  # None = could not tell
    checked_url: str | None
    detail: str


class SourceError(RuntimeError):
    pass


class SourceAdapter(abc.ABC):
    """Contract every job source implements."""

    name: str = "base"

    @abc.abstractmethod
    def search_jobs(self, query: dict | None = None) -> list[RawJob]:
        """Return raw jobs. Must not bypass logins, CAPTCHAs, paywalls or rate limits."""

    def normalize_job(self, raw: RawJob):
        from careerpilot.ingestion.normalize import normalize

        return normalize(raw)

    def verify_job(self, raw: RawJob) -> VerificationResult:
        return VerificationResult(None, raw.source_url, "verification not supported by this source")

    def get_application_url(self, raw: RawJob) -> str | None:
        return raw.application_url or raw.source_url

    @abc.abstractmethod
    def get_source_metadata(self) -> SourceMetadata:
        ...
