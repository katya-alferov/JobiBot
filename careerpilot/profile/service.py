"""Candidate profile: master CV import, verified facts, links and preferences."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from careerpilot.config import Settings
from careerpilot.profile.cv_parser import ParsedCV, parse_docx
from careerpilot.storage.audit import audit
from careerpilot.storage.models import CandidateProfile, ResumeMaster, UserPreference, VerifiedFact, utcnow

CV_SOURCE_PREFIX = "master_cv"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


# --------------------------------------------------------------------------- config files


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_preferences(settings: Settings) -> dict:
    return load_yaml(settings.config_dir / "preferences.yaml")


def load_profile_config(settings: Settings) -> dict:
    path = settings.config_dir / "profile.yaml"
    return load_yaml(path if path.exists() else settings.config_dir / "profile.example.yaml")


@lru_cache(maxsize=4)
def _vocabulary(config_dir: str) -> dict[str, list[str]]:
    raw = load_yaml(Path(config_dir) / "skills_vocabulary.yaml")
    return {k.lower(): [a.lower() for a in v] for k, v in raw.items()}


def vocabulary(settings: Settings) -> dict[str, list[str]]:
    return _vocabulary(str(settings.config_dir))


def _alias_pattern(alias: str) -> re.Pattern:
    escaped = re.escape(alias)
    # Word boundaries for Latin text; Hebrew and symbols use looser boundaries.
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


def find_skills(text: str, vocab: dict[str, list[str]]) -> set[str]:
    text = text or ""
    found = set()
    for canonical, aliases in vocab.items():
        if any(_alias_pattern(a).search(text) for a in aliases):
            found.add(canonical)
    return found


# --------------------------------------------------------------------------- profile view


@dataclass
class ProfileView:
    """Read-only snapshot of what the system may claim about the candidate."""

    profile: CandidateProfile
    verified: list[VerifiedFact]
    unverified: list[VerifiedFact]
    skills: set[str]
    master: ResumeMaster | None
    parsed_cv: ParsedCV | None
    vocab: dict[str, list[str]] = field(default_factory=dict)

    def verified_text(self) -> str:
        return "\n".join(f.value for f in self.verified)

    def facts_by_category(self, category: str) -> list[VerifiedFact]:
        return [f for f in self.verified if f.category == category]


def get_profile(session: Session) -> CandidateProfile | None:
    return session.scalars(select(CandidateProfile).order_by(CandidateProfile.id)).first()


def primary_master(session: Session) -> ResumeMaster | None:
    return session.scalars(select(ResumeMaster).where(ResumeMaster.is_primary.is_(True))).first()


def load_profile_view(session: Session, settings: Settings) -> ProfileView:
    profile = get_profile(session)
    if profile is None:
        raise RuntimeError("No candidate profile yet. Run: careerpilot import-cv <master.docx>")
    facts = list(session.scalars(select(VerifiedFact).where(VerifiedFact.profile_id == profile.id)))
    verified = [f for f in facts if f.status == "verified"]
    unverified = [f for f in facts if f.status != "verified"]
    master = primary_master(session)
    parsed = parse_docx(Path(master.stored_path)) if master and Path(master.stored_path).exists() else None
    vocab = vocabulary(settings)
    skills = find_skills("\n".join(f.value for f in verified), vocab)
    return ProfileView(profile, verified, unverified, skills, master, parsed, vocab)


# --------------------------------------------------------------------------- import


def _upsert_fact(session: Session, profile: CandidateProfile, category: str, key: str, value: str,
                 status: str, source: str, detail: dict | None = None,
                 source_detail: str | None = None) -> VerifiedFact:
    fact = session.scalars(
        select(VerifiedFact).where(
            VerifiedFact.profile_id == profile.id, VerifiedFact.category == category, VerifiedFact.key == key
        )
    ).first()
    if fact is None:
        fact = VerifiedFact(profile_id=profile.id, category=category, key=key)
        session.add(fact)
    elif fact.status == "verified" and status != "verified" and fact.confirmed_at:
        return fact  # never downgrade something the candidate confirmed
    fact.value = value
    fact.status = status
    fact.source = source
    fact.detail = detail or {}
    fact.source_detail = source_detail
    if status == "verified" and fact.confirmed_at is None and not source.startswith(CV_SOURCE_PREFIX):
        fact.confirmed_at = utcnow()
    return fact


def facts_from_cv(parsed: ParsedCV, cv_sha: str) -> list[dict]:
    src = f"{CV_SOURCE_PREFIX}:{cv_sha[:12]}"
    facts: list[dict] = [
        {"category": "identity", "key": "name", "value": parsed.name.title(), "source": src + "#header"},
        {"category": "summary", "key": "master-summary", "value": parsed.summary, "source": src + "#summary"},
    ]
    for role in parsed.experience:
        facts.append({
            "category": "employment", "key": role.key, "value": role.header_text,
            "detail": {"title": role.title, "organization": role.organization, "dates": role.dates,
                       "start": role.start, "end": role.end},
            "source": f"{src}#experience/{role.key}",
        })
        for i, bullet in enumerate(role.bullets):
            facts.append({
                "category": "responsibility", "key": f"{role.key}#{i}", "value": bullet,
                "detail": {"role_key": role.key, "position": i},
                "source": f"{src}#experience/{role.key}/{i}",
            })
    for edu in parsed.education:
        facts.append({
            "category": "education", "key": edu.key, "value": edu.header_text,
            "detail": {"title": edu.title, "institution": edu.institution, "dates": edu.dates,
                       "details": edu.details},
            "source": f"{src}#education/{edu.key}",
        })
    for i, course in enumerate(parsed.courses):
        facts.append({"category": "course", "key": f"course#{i}", "value": course,
                      "source": f"{src}#courses/{i}"})
    for group in parsed.skills:
        key = re.sub(r"[^a-z0-9]+", "-", group.label.lower()).strip("-")
        facts.append({"category": "skill_group", "key": key, "value": group.line,
                      "detail": {"label": group.label, "items": group.items},
                      "source": f"{src}#skills/{key}"})
    for lang, level in parsed.languages:
        facts.append({"category": "language", "key": lang.lower(), "value": f"{lang}: {level}",
                      "detail": {"language": lang, "level": level}, "source": f"{src}#languages"})
    return facts


def import_master_cv(session: Session, settings: Settings, cv_path: Path, *, make_primary: bool = True,
                     notes: str | None = None) -> ResumeMaster:
    """Copy the CV into data/cv/master (read-only), parse it and store its facts as verified."""
    cv_path = Path(cv_path)
    if cv_path.suffix.lower() != ".docx":
        raise ValueError("The master CV must be a .docx file (PDF masters are not supported for tailoring)")
    digest = sha256_file(cv_path)
    existing = session.scalars(select(ResumeMaster).where(ResumeMaster.sha256 == digest)).first()
    parsed = parse_docx(cv_path)

    if existing is None:
        dest_dir = settings.data_dir / "cv" / "master"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{digest[:12]}_{cv_path.name.replace(' ', '_')}"
        if not dest.exists():
            shutil.copy2(cv_path, dest)
            make_read_only(dest)
        existing = ResumeMaster(original_filename=cv_path.name, stored_path=str(dest), sha256=digest,
                                notes=notes, extracted_text=parsed.full_text)
        session.add(existing)
        session.flush()

    if make_primary:
        for other in session.scalars(select(ResumeMaster).where(ResumeMaster.id != existing.id)):
            other.is_primary = False
        existing.is_primary = True

    profile = get_profile(session)
    if profile is None:
        profile = CandidateProfile(full_name=parsed.name.title(), email=parsed.email, phone=parsed.phone)
        session.add(profile)
        session.flush()
    else:
        profile.version += 1

    if make_primary:
        for f in facts_from_cv(parsed, digest):
            _upsert_fact(session, profile, f["category"], f["key"], f["value"], "verified", f["source"],
                         f.get("detail"))
    apply_profile_config(session, settings, profile)
    audit(session, "master_cv_imported", entity_type="resume_master", entity_id=existing.id,
          filename=cv_path.name, sha256=digest, primary=make_primary)
    return existing


def apply_profile_config(session: Session, settings: Settings, profile: CandidateProfile) -> None:
    cfg = load_profile_config(settings)
    website = (cfg.get("personal_website_url") or "").strip() or None
    linkedin = (cfg.get("linkedin_profile_url") or "").strip() or None
    if website != profile.personal_website_url:
        profile.personal_website_url = website
        profile.personal_website_verified = False
    if linkedin != profile.linkedin_profile_url:
        profile.linkedin_profile_url = linkedin
        profile.linkedin_profile_verified = False
    for f in cfg.get("additional_facts") or []:
        _upsert_fact(session, profile, f["category"], f["key"], f["value"], f.get("status", "needs_confirmation"),
                     f.get("source", "profile.yaml"))
    prefs = load_preferences(settings)
    for key, value in (prefs.get("global") or {}).items():
        _upsert_pref(session, profile.id, "global", key, value)
    for track, spec in (prefs.get("tracks") or {}).items():
        for key in ("enabled", "candidate_seniority", "weights"):
            _upsert_pref(session, profile.id, track, key, spec.get(key))


def _upsert_pref(session: Session, profile_id: int, track: str, key: str, value) -> None:
    pref = session.scalars(select(UserPreference).where(
        UserPreference.profile_id == profile_id, UserPreference.track == track, UserPreference.key == key)).first()
    if pref is None:
        session.add(UserPreference(profile_id=profile_id, track=track, key=key, value=value))
    else:
        pref.value = value


def confirm_fact(session: Session, fact_id: int, *, value: str | None = None, reject: bool = False) -> VerifiedFact:
    fact = session.get(VerifiedFact, fact_id)
    if fact is None:
        raise KeyError(fact_id)
    if value is not None:
        fact.value = value
    fact.status = "rejected" if reject else "verified"
    fact.confirmed_at = utcnow()
    fact.source = f"user_confirmed:{fact.source}"
    profile = session.get(CandidateProfile, fact.profile_id)
    profile.version += 1
    audit(session, "fact_rejected" if reject else "fact_confirmed", actor="user",
          entity_type="verified_fact", entity_id=fact.id, value=fact.value)
    return fact


def verify_url(url: str, client=None, timeout: float = 10.0) -> tuple[bool, str]:
    """Check that a URL resolves. A 2xx/3xx response does not prove the page content is accurate."""
    import httpx

    if not url:
        return False, "not set"
    try:
        own = client is None
        client = client or httpx.Client(follow_redirects=True, timeout=timeout)
        try:
            resp = client.get(url)
        finally:
            if own:
                client.close()
        # LinkedIn often answers automated requests with 999; that is "unknown", not "broken".
        if resp.status_code < 400:
            return True, f"HTTP {resp.status_code}"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:  # network errors are reported, never raised
        return False, f"unreachable: {type(exc).__name__}"


def verify_profile_links(session: Session, client=None) -> dict:
    profile = get_profile(session)
    results = {}
    for attr in ("personal_website_url", "linkedin_profile_url"):
        url = getattr(profile, attr)
        ok, detail = verify_url(url, client=client)
        setattr(profile, attr.replace("_url", "_verified"), ok)
        results[attr] = {"url": url, "ok": ok, "detail": detail}
    audit(session, "profile_links_checked", results=results)
    return results
