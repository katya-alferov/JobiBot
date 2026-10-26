"""Cover letters, application answers and recruiter drafts from verified facts only."""

from __future__ import annotations

import re
from dataclasses import dataclass

from careerpilot.profile.service import ProfileView
from careerpilot.storage.models import JobMatch, JobPosting

# Questions the system must never answer on its own.
SENSITIVE_PATTERNS = {
    "salary": [r"salary", r"compensation", r"expected pay", r"שכר", r"ציפיות"],
    "work_authorization": [r"authori[sz]", r"visa", r"citizenship", r"work permit", r"right to work", r"אזרחות"],
    "legal_declaration": [r"declare", r"certify", r"attest", r"criminal", r"convicted", r"מצהיר", r"מצהירה"],
    "consent": [r"consent", r"agree to", r"terms", r"privacy policy", r"מסכים", r"מסכימה"],
    "demographic": [r"gender", r"race", r"ethnic", r"religion", r"age\b", r"date of birth", r"veteran",
                    r"מגדר", r"גיל", r"תאריך לידה"],
    "medical_disability": [r"disab", r"medical", r"health", r"accommodation", r"נכות", r"רפואי"],
    "availability": [r"notice period", r"start date", r"available to start", r"availability", r"זמינות"],
    "military": [r"military", r"army service", r"צבא", r"שירות צבאי"],
}


@dataclass
class DraftAnswer:
    question: str
    answer: str | None
    source: str | None
    sensitive: bool
    needs_user_input: bool
    category: str | None = None
    required: bool = True


def classify_question(question: str) -> str | None:
    q = question.lower()
    for category, pats in SENSITIVE_PATTERNS.items():
        if any(re.search(p, q) for p in pats):
            return category
    return None


def answer_question(question: str, view: ProfileView, confirmed: dict[str, str] | None = None,
                    required: bool = True) -> DraftAnswer:
    """Answer only from verified profile data or a user-confirmed answer. Otherwise ask the user."""
    confirmed = confirmed or {}
    if question in confirmed:
        return DraftAnswer(question, confirmed[question], "user_confirmed", False, False, required=required)
    category = classify_question(question)
    if category:
        return DraftAnswer(question, None, None, True, True, category, required)
    q = question.lower()
    p = view.profile
    simple = [
        (r"first name|שם פרטי", (p.full_name.split()[0], "master_cv#header")),
        (r"last name|surname|שם משפחה", (p.full_name.split()[-1], "master_cv#header")),
        (r"full name|^name$|שם מלא", (p.full_name, "master_cv#header")),
        (r"e-?mail|דוא\"?ל|מייל", (p.email, "master_cv#header")),
        (r"phone|mobile|טלפון|נייד", (p.phone, "master_cv#header")),
    ]
    for pattern, (value, source) in simple:
        if re.search(pattern, q) and value:
            return DraftAnswer(question, value, source, False, False, required=required)
    if re.search(r"linkedin", q):
        if p.linkedin_profile_url and p.linkedin_profile_verified:
            return DraftAnswer(question, p.linkedin_profile_url, "profile.yaml (verified)", False, False,
                               required=required)
        return DraftAnswer(question, None, None, False, True, "unverified_link", required)
    if re.search(r"website|portfolio|אתר", q):
        if p.personal_website_url and p.personal_website_verified:
            return DraftAnswer(question, p.personal_website_url, "profile.yaml (verified)", False, False,
                               required=required)
        return DraftAnswer(question, None, None, False, True, "unverified_link", required)
    if re.search(r"language|שפות", q):
        langs = [f.value for f in view.facts_by_category("language")]
        if langs:
            return DraftAnswer(question, "; ".join(langs), "master_cv#languages", False, False, required=required)
    if re.search(r"degree|education|השכלה|תואר", q):
        edu = [f.value for f in view.facts_by_category("education")]
        if edu:
            return DraftAnswer(question, "; ".join(edu), "master_cv#education", False, False, required=required)
    return DraftAnswer(question, None, None, False, True, "unknown", required)


def cover_letter(view: ProfileView, job: JobPosting, match: JobMatch | None) -> tuple[str, list[str]]:
    """A short letter in the job's language, built from verified role headers and matched skills."""
    parsed = view.parsed_cv
    roles = parsed.experience if parsed else []
    current = roles[0] if roles else None
    sources = []
    skills = ""
    if match and parsed:
        from careerpilot.documents.tailor import _cv_skill_names

        names = _cv_skill_names(parsed, list(match.matching_skills or []), view.vocab)
        skills = ", ".join(names[:5])
    links = []
    if view.profile.linkedin_profile_url and view.profile.linkedin_profile_verified:
        links.append(view.profile.linkedin_profile_url)
    if view.profile.personal_website_url and view.profile.personal_website_verified:
        links.append(view.profile.personal_website_url)
    name = view.profile.full_name
    if current:
        sources.append(f"master_cv#experience/{current.key}")
    org = current.organization.split("(")[0].strip() if current and current.organization else ""
    if job.language == "he":
        body = [f"שלום רב,", "",
                f"ברצוני להגיש מועמדות למשרת {job.title} ב-{job.employer}."]
        if current:
            body.append(f"תפקידי הנוכחי: {current.title} ב-{org} ({current.dates}).")
        if skills:
            body.append(f"הכישורים הרלוונטיים שלי למשרה: {skills}.")
        body.append("קורות החיים המצורפים מפרטים את הניסיון שלי.")
        if links:
            body.append("קישורים: " + " | ".join(links))
        body += ["", "בברכה,", name]
    else:
        body = ["Dear Hiring Team,", "",
                f"I am applying for the {job.title} position at {job.employer}."]
        if current:
            body.append(f"I currently work as {current.title} at {org} ({current.dates}).")
        if skills:
            body.append(f"Skills from my CV that match this role: {skills}.")
        body.append("My attached CV describes my experience in detail.")
        if links:
            body.append("Links: " + " | ".join(links))
        body += ["", "Kind regards,", name]
    return "\n".join(body), sources


def recruiter_message(view: ProfileView, job: JobPosting) -> str:
    """A draft only. Never sent automatically."""
    return (f"Hi, I saw the {job.title} role at {job.employer} and applied through the official page. "
            f"I'd be glad to talk about how my background fits. Best, {view.profile.full_name}")
