"""Application adapters. The MVP ships a mock adapter and an assisted-manual adapter only.

A real adapter must be free, permitted by the target site, and reviewed before its domain is
added to APPROVED_SUBMISSION_DOMAINS. None may bypass CAPTCHAs, logins or anti-automation.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class FormInspection:
    requires_account: bool = False
    captcha_present: bool = False
    restricted_actions: list[str] = field(default_factory=list)
    unmapped_required_fields: list[str] = field(default_factory=list)


@dataclass
class SubmissionPackage:
    application_id: str
    idempotency_key: str
    target_url: str
    employer: str
    job_title: str
    cv_path: str
    cv_sha256: str
    cover_letter: str | None
    answers: dict[str, str]


@dataclass
class SubmissionOutcome:
    result: str  # SUBMITTED | CONFIRMED | UNCERTAIN | FAILED | BLOCKED
    confirmation_ref: str | None = None
    evidence: dict = field(default_factory=dict)
    error: str | None = None
    automation_restriction: str | None = None


@dataclass
class ReceiptCheck:
    found: bool | None  # None = cannot tell
    confirmation_ref: str | None = None
    evidence: dict = field(default_factory=dict)


class ApplicationAdapter(abc.ABC):
    name = "base"
    version = "0.1"
    cost = "free"
    automation_permitted = True
    method = "automated"

    def handles(self, url: str) -> bool:
        return False

    @abc.abstractmethod
    def inspect(self, url: str) -> FormInspection: ...

    @abc.abstractmethod
    def submit(self, package: SubmissionPackage) -> SubmissionOutcome: ...

    def check_receipt(self, package: SubmissionPackage) -> ReceiptCheck:
        return ReceiptCheck(found=None)


class MockApplicationAdapter(ApplicationAdapter):
    """Simulates an employer ATS on the reserved .test domain. Never makes a network request.

    behavior: confirm | submitted | uncertain | captcha | error | account_required
    """

    name = "mock_ats"
    version = "1.0"
    DOMAIN = "mock-ats.careerpilot.test"

    def __init__(self, behavior: str = "confirm"):
        self.behavior = behavior
        self.received: dict[str, SubmissionPackage] = {}  # what the fake employer received
        self.submit_calls = 0

    def handles(self, url: str) -> bool:
        return urlsplit(url).netloc.lower() == self.DOMAIN

    def inspect(self, url: str) -> FormInspection:
        if self.behavior == "captcha":
            return FormInspection(captcha_present=True)
        if self.behavior == "account_required":
            return FormInspection(requires_account=True, restricted_actions=["create_account"])
        return FormInspection()

    def submit(self, package: SubmissionPackage) -> SubmissionOutcome:
        self.submit_calls += 1
        if self.behavior == "captcha":
            return SubmissionOutcome("BLOCKED", automation_restriction="captcha")
        if self.behavior == "error":
            return SubmissionOutcome("FAILED", error="HTTP 500 from mock ATS")
        self.received[package.idempotency_key] = package
        if self.behavior == "uncertain":
            return SubmissionOutcome("UNCERTAIN", error="timeout after clicking submit")
        if self.behavior == "submitted":
            return SubmissionOutcome("SUBMITTED", evidence={"http_status": 200, "page": "Thank you"})
        ref = f"MOCK-{uuid.uuid4().hex[:8].upper()}"
        return SubmissionOutcome("CONFIRMED", confirmation_ref=ref,
                                 evidence={"http_status": 200, "receipt": ref})

    def check_receipt(self, package: SubmissionPackage) -> ReceiptCheck:
        if package.idempotency_key in self.received:
            return ReceiptCheck(True, evidence={"mock_receipt_lookup": "found"})
        return ReceiptCheck(False, evidence={"mock_receipt_lookup": "not found"})


class AssistedManualAdapter(ApplicationAdapter):
    """Prepares everything and hands the official link to the candidate. Never submits."""

    name = "assisted_manual"
    version = "1.0"
    method = "assisted_manual"

    def handles(self, url: str) -> bool:
        return True

    def inspect(self, url: str) -> FormInspection:
        return FormInspection(restricted_actions=["manual_submission_only"])

    def submit(self, package: SubmissionPackage) -> SubmissionOutcome:
        return SubmissionOutcome("BLOCKED", automation_restriction="manual_submission_only",
                                 evidence={"official_link": package.target_url})


def pick_adapter(url: str | None, adapters: list[ApplicationAdapter]) -> ApplicationAdapter:
    for adapter in adapters:
        if url and not isinstance(adapter, AssistedManualAdapter) and adapter.handles(url):
            return adapter
    return AssistedManualAdapter()
