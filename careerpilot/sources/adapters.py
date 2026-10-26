"""Concrete, free source adapters: fixtures, manual import, alert emails and public ATS boards."""

from __future__ import annotations

import csv
import email
import html
import json
import re
from datetime import UTC, datetime
from email import policy
from pathlib import Path

import httpx

from careerpilot.sources.base import RawJob, SourceAdapter, SourceError, SourceMetadata, VerificationResult

USER_AGENT = "CareerPilotIsrael/0.1 (personal job search; contact via candidate)"


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC).replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _raw_from_dict(source: str, d: dict) -> RawJob:
    return RawJob(
        source_name=source,
        source_job_id=str(d["source_job_id"]) if d.get("source_job_id") is not None else None,
        source_url=d.get("source_url") or d.get("url"),
        employer=d.get("employer") or d.get("company") or "",
        title=d.get("title") or "",
        description=d.get("description") or "",
        location=d.get("location"),
        application_url=d.get("application_url"),
        posted_at=_parse_dt(d.get("posted_at")),
        salary_text=d.get("salary") or d.get("salary_text"),
        employment_type=d.get("employment_type"),
        work_arrangement=d.get("work_arrangement"),
        extra={k: v for k, v in d.items() if k in {"application_questions", "platform"}},
    )


class FixtureSource(SourceAdapter):
    """Synthetic jobs from a local JSON file. Used for tests and demos."""

    def __init__(self, path: Path, name: str = "synthetic_fixtures"):
        self.path = Path(path)
        self.name = name

    def search_jobs(self, query=None) -> list[RawJob]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [_raw_from_dict(self.name, d) for d in data]

    def get_source_metadata(self) -> SourceMetadata:
        return SourceMetadata(self.name, "fixture", "free", True, "Local synthetic data", False, "static")


class ManualImportSource(SourceAdapter):
    """Jobs the candidate saved, as JSON or CSV. Always permitted."""

    name = "manual_import"

    def __init__(self, path: Path):
        self.path = Path(path)

    def search_jobs(self, query=None) -> list[RawJob]:
        if self.path.suffix.lower() == ".json":
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rows = data if isinstance(data, list) else [data]
        elif self.path.suffix.lower() == ".csv":
            with open(self.path, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        else:
            raise SourceError("Manual import supports .json and .csv files")
        jobs = [_raw_from_dict(self.name, r) for r in rows]
        bad = [j for j in jobs if not j.title or not j.employer]
        if bad:
            raise SourceError(f"{len(bad)} rows are missing a title or employer")
        return jobs

    def get_source_metadata(self) -> SourceMetadata:
        return SourceMetadata(self.name, "manual", "free", True,
                              "You supply the jobs; nothing is fetched", True, "as imported")


class AlertEmailSource(SourceAdapter):
    """Reads job links from .eml files of alerts the candidate subscribed to (no mailbox login).

    Only the link, subject and visible text are stored; the employer page is the authority.
    """

    name = "alert_email_import"
    LINK_RE = re.compile(r"https?://[^\s\"'<>]+")

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def search_jobs(self, query=None) -> list[RawJob]:
        jobs = []
        for path in sorted(self.folder.glob("*.eml")):
            msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
            body = msg.get_body(preferencelist=("plain", "html"))
            text = body.get_content() if body else ""
            text = strip_html(text) if body and body.get_content_type() == "text/html" else text
            sender = str(msg.get("From", ""))
            for block in re.split(r"\n-{3,}\n|\n={3,}\n", text):
                m = re.search(r"^(?P<title>[^\n]+)\n(?P<employer>[^\n]+)\n", block.strip() + "\n")
                link = self.LINK_RE.search(block)
                if not (m and link):
                    continue
                jobs.append(RawJob(
                    source_name=self.name, source_job_id=None, source_url=link.group(0).rstrip(".,)"),
                    employer=m.group("employer").strip(), title=m.group("title").strip(),
                    description=block.strip(), extra={"alert_sender": sender, "alert_file": path.name},
                ))
        return jobs

    def get_source_metadata(self) -> SourceMetadata:
        return SourceMetadata(self.name, "email_alert", "free", True,
                              "Only alert emails you saved yourself; listings must be verified on the employer page",
                              True, "as often as alerts arrive")


class GreenhouseBoardSource(SourceAdapter):
    """Public Greenhouse job board API (no key, read-only). One board token per employer."""

    BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def __init__(self, board_token: str, employer: str, client: httpx.Client | None = None):
        self.board_token = board_token
        self.employer = employer
        self.name = f"greenhouse:{board_token}"
        self.client = client

    def _client(self) -> httpx.Client:
        return self.client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})

    def search_jobs(self, query=None) -> list[RawJob]:
        client = self._client()
        try:
            resp = client.get(self.BASE.format(token=self.board_token))
        except httpx.HTTPError as exc:
            raise SourceError(f"{self.name}: {type(exc).__name__}") from exc
        if resp.status_code == 429:
            raise SourceError(f"{self.name}: rate limited; will retry on the next scheduled run")
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: HTTP {resp.status_code}")
        jobs = []
        for j in resp.json().get("jobs", []):
            jobs.append(RawJob(
                source_name=self.name, source_job_id=str(j.get("id")), source_url=j.get("absolute_url"),
                employer=self.employer, title=j.get("title", ""),
                description=strip_html(j.get("content", "")),
                location=(j.get("location") or {}).get("name"), application_url=j.get("absolute_url"),
                posted_at=_parse_dt(j.get("updated_at")), extra={"platform": "greenhouse"},
            ))
        return jobs

    def verify_job(self, raw: RawJob) -> VerificationResult:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.board_token}/jobs/{raw.source_job_id}"
        try:
            resp = self._client().get(url)
        except httpx.HTTPError as exc:
            return VerificationResult(None, url, f"unreachable: {type(exc).__name__}")
        if resp.status_code == 404:
            return VerificationResult(False, url, "posting no longer listed")
        return VerificationResult(resp.status_code == 200, url, f"HTTP {resp.status_code}")

    def get_source_metadata(self) -> SourceMetadata:
        return SourceMetadata(self.name, "public_api", "free", True,
                              "Public read-only board API; respect rate limits", False, "live",
                              "https://developers.greenhouse.io/job-board.html")


class LeverPostingsSource(SourceAdapter):
    """Public Lever postings API (no key, read-only). One company slug per employer."""

    BASE = "https://api.lever.co/v0/postings/{slug}?mode=json"

    def __init__(self, slug: str, employer: str, client: httpx.Client | None = None):
        self.slug = slug
        self.employer = employer
        self.name = f"lever:{slug}"
        self.client = client

    def search_jobs(self, query=None) -> list[RawJob]:
        client = self.client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
        try:
            resp = client.get(self.BASE.format(slug=self.slug))
        except httpx.HTTPError as exc:
            raise SourceError(f"{self.name}: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: HTTP {resp.status_code}")
        jobs = []
        for j in resp.json():
            cats = j.get("categories") or {}
            jobs.append(RawJob(
                source_name=self.name, source_job_id=j.get("id"), source_url=j.get("hostedUrl"),
                employer=self.employer, title=j.get("text", ""),
                description=j.get("descriptionPlain") or strip_html(j.get("description", "")),
                location=cats.get("location"), application_url=j.get("applyUrl") or j.get("hostedUrl"),
                posted_at=_parse_dt(j.get("createdAt")), employment_type=cats.get("commitment"),
                work_arrangement=j.get("workplaceType"), extra={"platform": "lever"},
            ))
        return jobs

    def get_source_metadata(self) -> SourceMetadata:
        return SourceMetadata(self.name, "public_api", "free", True,
                              "Public read-only postings API", False, "live",
                              "https://github.com/lever/postings-api")


def build_sources(settings, sources_config: dict | None = None) -> list[SourceAdapter]:
    """Sources configured in config/sources.yaml."""
    from careerpilot.profile.service import load_yaml

    cfg = sources_config if sources_config is not None else load_yaml(settings.config_dir / "sources.yaml")
    adapters: list[SourceAdapter] = []
    for entry in cfg.get("greenhouse") or []:
        adapters.append(GreenhouseBoardSource(entry["board_token"], entry["employer"]))
    for entry in cfg.get("lever") or []:
        adapters.append(LeverPostingsSource(entry["slug"], entry["employer"]))
    imports = settings.data_dir / "imports"
    for path in sorted(imports.glob("*.json")) + sorted(imports.glob("*.csv")):
        adapters.append(ManualImportSource(path))
    if any(imports.glob("*.eml")):
        adapters.append(AlertEmailSource(imports))
    if cfg.get("use_synthetic_fixtures"):
        from careerpilot.config import PROJECT_ROOT

        adapters.append(FixtureSource(PROJECT_ROOT / "tests" / "fixtures" / "synthetic_jobs.json"))
    return adapters
