"""Deterministic eligibility filters and an explainable 0-100 score per career track.

Weights are configurable starting points, not proven predictors of hiring outcomes.
No model output can change eligibility here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from careerpilot.config import Settings
from careerpilot.ingestion.normalize import SENIORITY_LEVELS
from careerpilot.matching.tracks import _hits
from careerpilot.profile.service import ProfileView, _alias_pattern, find_skills, load_preferences, vocabulary
from careerpilot.storage.models import JobMatch, JobPosting

CERTIFICATIONS = {"cissp", "cism", "cisa", "soc 2", "iso 27001"}
OTHER_LANGUAGES = {
    "arabic": ["arabic", "ערבית"], "french": ["french", "צרפתית"], "german": ["german", "גרמנית"],
    "spanish": ["spanish", "ספרדית"], "italian": ["italian", "איטלקית"], "amharic": ["amharic", "אמהרית"],
    "japanese": ["japanese", "יפנית"], "chinese": ["mandarin", "chinese", "סינית"],
    "portuguese": ["portuguese", "פורטוגזית"], "dutch": ["dutch", "הולנדית"],
}
REQUIRED_WORDS = r"(required|must|mandatory|native|fluent|חובה|ברמת שפת אם)"
# Years of directly relevant experience per track, from master CV dates (assessment, not a claim).
CANDIDATE_TRACK_YEARS = {"data_bi": 4, "ai_automation": 0, "grc_security": 0, "marketing": 5, "other": 4}


@dataclass
class MatchResult:
    track: str
    score: float
    component_scores: dict
    weights: dict
    eligible: bool
    hard_filters: list[dict]
    matching_skills: list[str]
    missing: list[str]
    relevant_experience: list[dict]
    uncertainties: list[str]
    explanation: str
    confidence: str
    recommendation: str
    recommendation_reason: str
    extra: dict = field(default_factory=dict)


def _filter(name: str, passed: bool, detail: str) -> dict:
    return {"filter": name, "passed": passed, "detail": detail}


def hard_filters(job: JobPosting, prefs: dict, view: ProfileView) -> list[dict]:
    g = prefs.get("global") or {}
    text = f"{job.title}\n{job.description_normalized}".lower()
    results = []

    excluded = [e.lower() for e in g.get("excluded_employers") or []]
    hit = next((e for e in excluded if e and e in job.employer_normalized), None)
    results.append(_filter("excluded_employer", hit is None, f"employer excluded: {hit}" if hit else "not excluded"))

    kw = [k.lower() for k in g.get("excluded_keywords") or []]
    kw_hit = next((k for k in kw if k and k in text), None)
    results.append(_filter("excluded_keyword", kw_hit is None,
                           f"contains excluded term: {kw_hit}" if kw_hit else "no excluded terms"))

    results.append(_filter("posting_active", job.expiration_status != "expired",
                           "posting expired" if job.expiration_status == "expired" else "active"))

    min_salary = g.get("min_monthly_salary_ils")
    if min_salary and job.salary_max and job.salary_currency == "ILS":
        monthly_max = job.salary_max / 12 if job.salary_period == "year" else job.salary_max
        results.append(_filter("min_salary", monthly_max >= min_salary,
                               f"published max {monthly_max:,.0f} ILS/month vs minimum {min_salary:,} ILS"))

    types = g.get("employment_types")
    if types and job.employment_type != "unknown":
        results.append(_filter("employment_type", job.employment_type in types,
                               f"{job.employment_type} vs allowed {types}"))

    arrangements = g.get("work_arrangements")
    if arrangements and job.work_arrangement != "unknown":
        results.append(_filter("work_arrangement", job.work_arrangement in arrangements,
                               f"{job.work_arrangement} vs allowed {arrangements}"))

    max_days = g.get("max_office_days_per_week")
    if max_days is not None and job.office_days is not None:
        results.append(_filter("office_days", job.office_days <= max_days,
                               f"{job.office_days} office days vs maximum {max_days}"))

    locations = [loc.lower() for loc in g.get("acceptable_locations") or []]
    if locations and job.location_normalized and job.work_arrangement != "remote":
        results.append(_filter("location", job.location_normalized in locations,
                               f"{job.location_normalized} vs acceptable {locations}"))

    known = {lang.lower() for lang in g.get("required_languages_known") or []}
    for lang, aliases in OTHER_LANGUAGES.items():
        if lang in known:
            continue
        for alias in aliases:
            if re.search(rf"{re.escape(alias)}[^.\n]{{0,40}}{REQUIRED_WORDS}|{REQUIRED_WORDS}[^.\n]{{0,40}}{re.escape(alias)}", text):
                results.append(_filter("language_requirement", False, f"requires {lang}, not on the CV"))
                break
    return results


def _seniority_score(candidate: str, job: str) -> tuple[float, str | None]:
    if job == "unknown":
        return 0.6, "job seniority not stated"
    ci = SENIORITY_LEVELS.index(candidate) if candidate in SENIORITY_LEVELS else 2
    ji = SENIORITY_LEVELS.index(job)
    gap = ji - ci
    if gap <= 0:
        return (1.0 if gap >= -1 else 0.7), None
    if gap == 1:
        return 0.6, f"role is {job}; candidate level for this track assessed as {candidate}"
    return 0.2, f"role is {job}; candidate level for this track assessed as {candidate}"


def score_job(job: JobPosting, track: str, view: ProfileView, prefs: dict, settings: Settings) -> MatchResult:
    vocab = vocabulary(settings)
    spec = (prefs.get("tracks") or {}).get(track) or {}
    g = prefs.get("global") or {}
    weights = spec.get("weights") or {"skills": 35, "track": 25, "conditions": 15, "location": 15, "seniority": 10}
    uncertainties: list[str] = []
    text = f"{job.title}\n{job.description_normalized}"

    filters = hard_filters(job, prefs, view)
    eligible = all(f["passed"] for f in filters)

    # Skills and experience
    job_skills = find_skills(text, vocab)
    matched = sorted(job_skills & view.skills)
    missing = sorted(job_skills - view.skills)
    if job_skills:
        skills_score = len(matched) / len(job_skills)
        preferred = {s.lower() for s in spec.get("preferred_skills") or []}
        if preferred & set(matched):
            skills_score = min(1.0, skills_score + 0.1)
    else:
        skills_score = 0.5
        uncertainties.append("no recognizable skills in the job text")
    missing_reqs = [f"skill: {s}" for s in missing]
    for cert in sorted(CERTIFICATIONS & job_skills - view.skills):
        missing_reqs.append(f"certification or direct experience: {cert} (not on CV)")
    cand_years = CANDIDATE_TRACK_YEARS.get(track, 0)
    if job.years_required and job.years_required > cand_years:
        missing_reqs.append(f"{job.years_required}+ years requested; about {cand_years} years directly relevant on CV")

    # Track alignment
    kws = spec.get("title_keywords") or []
    if track == "other":
        track_score = 0.5
    elif _hits(kws, job.title.lower()):
        track_score = 1.0
    elif _hits(kws, job.description_normalized):
        track_score = 0.6
    else:
        track_score = 0.2

    # Conditions: salary and employment type
    cond_parts = []
    min_salary = g.get("min_monthly_salary_ils")
    if not min_salary:
        cond_parts.append(0.5)
        uncertainties.append("minimum salary preference not set")
    elif not job.salary_max:
        cond_parts.append(0.5)
        uncertainties.append("salary not published")
    else:
        cond_parts.append(1.0)
    if g.get("employment_types") and job.employment_type != "unknown":
        cond_parts.append(1.0 if job.employment_type in g["employment_types"] else 0.0)
    elif job.employment_type == "unknown":
        cond_parts.append(0.5)
        uncertainties.append("employment type unknown")
    else:
        cond_parts.append(0.6)
    conditions_score = sum(cond_parts) / len(cond_parts)

    # Location and work arrangement
    loc_parts = []
    if job.work_arrangement == "unknown":
        loc_parts.append(0.5)
        uncertainties.append("work arrangement unknown")
    elif g.get("work_arrangements"):
        loc_parts.append(1.0 if job.work_arrangement in g["work_arrangements"] else 0.0)
    else:
        loc_parts.append(0.6)
        uncertainties.append("work-arrangement preference not set")
    if g.get("acceptable_locations"):
        if job.work_arrangement == "remote":
            loc_parts.append(1.0)
        elif job.location_normalized:
            loc_parts.append(1.0 if job.location_normalized in [x.lower() for x in g["acceptable_locations"]] else 0.0)
        else:
            loc_parts.append(0.5)
            uncertainties.append("location unknown")
    else:
        loc_parts.append(0.5)
        uncertainties.append("location preference not set")
    location_score = sum(loc_parts) / len(loc_parts)

    seniority_score, sen_note = _seniority_score(spec.get("candidate_seniority", "mid"), job.seniority)
    if sen_note:
        uncertainties.append(sen_note)

    components = {"skills": skills_score, "track": track_score, "conditions": conditions_score,
                  "location": location_score, "seniority": seniority_score}
    total_w = sum(weights.values()) or 1
    score = round(sum(components[k] * weights.get(k, 0) for k in components) / total_w * 100, 1)

    # Relevant verified experience, with sources
    relevant = []
    for fact in view.facts_by_category("responsibility") + view.facts_by_category("employment"):
        fact_skills = [s for s in matched if any(_alias_pattern(a).search(fact.value) for a in vocab.get(s, []))]
        if fact_skills:
            relevant.append({"fact": fact.value, "source": fact.source, "skills": fact_skills})
    relevant = sorted(relevant, key=lambda r: -len(r["skills"]))[:6]

    if job.injection_flags:
        uncertainties.append("job text contains instruction-like content; treated as untrusted and ignored")

    data_unknowns = sum(1 for u in uncertainties if "unknown" in u or "not published" in u or "not stated" in u)
    confidence = "high" if data_unknowns <= 1 and job_skills else "medium" if data_unknowns <= 3 else "low"

    apply_t = g.get("apply_threshold", 70)
    review_t = g.get("review_threshold", 50)
    if not eligible:
        failed = [f["detail"] for f in filters if not f["passed"]]
        recommendation, reason = "reject", "Hard filter: " + "; ".join(failed)
    elif job.injection_flags:
        recommendation, reason = "review", "Suspicious instructions in the posting; review manually"
    elif score >= apply_t and confidence != "low":
        recommendation, reason = "apply", f"Score {score} meets the apply threshold {apply_t}"
    elif score >= review_t:
        recommendation, reason = "review", f"Score {score} is between {review_t} and {apply_t}, or confidence is low"
    else:
        recommendation, reason = "reject", f"Score {score} is below the review threshold {review_t}"

    explanation = (
        f"{spec.get('label', track)}: {score}/100. "
        f"Skills {skills_score:.0%} ({len(matched)} of {len(job_skills)} recognized skills on the CV), "
        f"track fit {track_score:.0%}, conditions {conditions_score:.0%}, "
        f"location {location_score:.0%}, seniority {seniority_score:.0%}."
    )
    return MatchResult(track, score, {k: round(v, 3) for k, v in components.items()}, weights, eligible,
                       filters, matched, missing_reqs, relevant, uncertainties, explanation, confidence,
                       recommendation, reason)


def tracks_for_job(job: JobPosting, prefs: dict) -> list[str]:
    enabled = [t for t, s in (prefs.get("tracks") or {}).items() if s.get("enabled", True)]
    scores = job.career_track_scores or {}
    tracks = {job.career_track} | {t for t in enabled if scores.get(t, 0) >= 2}
    return [t for t in enabled if t in tracks] or [job.career_track]


def match_job(session: Session, job: JobPosting, view: ProfileView, settings: Settings,
              prefs: dict | None = None) -> list[JobMatch]:
    prefs = prefs or load_preferences(settings)
    session.execute(update(JobMatch).where(JobMatch.job_id == job.id).values(is_latest=False))
    rows = []
    for track in tracks_for_job(job, prefs):
        r = score_job(job, track, view, prefs, settings)
        row = JobMatch(job_id=job.id, track=track, profile_version=view.profile.version, score=r.score,
                       component_scores=r.component_scores, weights=r.weights, eligible=r.eligible,
                       hard_filter_results=r.hard_filters, matching_skills=r.matching_skills,
                       missing_requirements=r.missing, relevant_experience=r.relevant_experience,
                       uncertainties=r.uncertainties, explanation=r.explanation, confidence=r.confidence,
                       recommendation=r.recommendation, recommendation_reason=r.recommendation_reason,
                       is_latest=True)
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def best_match(session: Session, job_id: str) -> JobMatch | None:
    rows = list(session.scalars(select(JobMatch).where(JobMatch.job_id == job_id, JobMatch.is_latest.is_(True))))
    if not rows:
        return None
    return sorted(rows, key=lambda m: (m.eligible, m.score), reverse=True)[0]
