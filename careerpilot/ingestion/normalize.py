"""Normalization of raw jobs (Hebrew and English). Job text is untrusted and only ever parsed."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from careerpilot.sources.base import RawJob

HEBREW_RE = re.compile(r"[֐-׿]")
TRACKING_PARAMS = {"gh_src", "gh_jid_src", "fbclid", "gclid", "ref", "refid", "trk", "trackingid", "src",
                   "source", "lever-source", "lever-origin", "mc_cid", "mc_eid"}

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous |prior |earlier |the )?(instructions|rules)",
    r"disregard (the|your) (rules|instructions)",
    r"system prompt",
    r"you are (now )?an? (ai|assistant|agent)",
    r"run (this|the following) command",
    r"(api[_ -]?key|password|token)s?\b.*\b(send|reveal|share|email)",
    r"התעלם מ(כל )?ההוראות",
]

CITY_ALIASES = {
    "tel aviv": ["tel aviv", "tel-aviv", "tlv", "תל אביב", "ת\"א", "תל-אביב"],
    "ramat gan": ["ramat gan", "ramat-gan", "רמת גן", "רמת-גן", "בורסה"],
    "herzliya": ["herzliya", "herzlia", "הרצליה"],
    "petah tikva": ["petah tikva", "petach tikva", "petah tiqva", "פתח תקווה", "פ\"ת", "פתח תקוה"],
    "rosh haayin": ["rosh haayin", "rosh ha'ayin", "rosh ha-ayin", "ראש העין"],
    "haifa": ["haifa", "חיפה"],
    "jerusalem": ["jerusalem", "ירושלים"],
    "netanya": ["netanya", "נתניה"],
    "raanana": ["ra'anana", "raanana", "רעננה"],
    "holon": ["holon", "חולון"],
    "rishon lezion": ["rishon lezion", "rishon le zion", "ראשון לציון"],
    "kfar saba": ["kfar saba", "כפר סבא"],
    "hod hasharon": ["hod hasharon", "הוד השרון"],
    "airport city": ["airport city", "איירפורט סיטי"],
    "or yehuda": ["or yehuda", "אור יהודה"],
    "yokneam": ["yokneam", "יקנעם"],
    "beer sheva": ["beer sheva", "be'er sheva", "באר שבע"],
    "rehovot": ["rehovot", "רחובות"],
    "modiin": ["modiin", "modi'in", "מודיעין"],
    "bnei brak": ["bnei brak", "בני ברק"],
    "givatayim": ["givatayim", "גבעתיים"],
}

ARRANGEMENT_PATTERNS = {
    "remote": [r"\bfully remote\b", r"\bremote\b", r"work from home", r"\bwfh\b", r"עבודה מהבית",
               r"מרחוק", r"100% מהבית"],
    "hybrid": [r"\bhybrid\b", r"היברידי", r"היברידית", r"\d\s*days? (a|per) week (in|at) the office",
               r"\d\s*ימים (מהמשרד|במשרד)"],
    "onsite": [r"\bon[- ]?site\b", r"\bin[- ]office\b", r"עבודה מהמשרד", r"5 ימים במשרד"],
}

EMPLOYMENT_PATTERNS = {
    "full_time": [r"full[- ]time", r"משרה מלאה"],
    "part_time": [r"part[- ]time", r"משרה חלקית"],
    "contract": [r"\bcontract(or)?\b", r"temporary", r"זמני", r"פרויקט"],
    "freelance": [r"freelance", r"פרילנס"],
    "student": [r"student position", r"משרת סטודנט", r"סטודנט/ית"],
}

SENIORITY_LEVELS = ["entry", "junior", "mid", "senior", "lead", "manager", "head"]
SENIORITY_PATTERNS = [
    ("head", [r"\bhead of\b", r"\bdirector\b", r"\bvp\b", r"ראש תחום", r"סמנכ\"?ל"]),
    ("manager", [r"\bmanager\b", r"מנהל", r"מנהלת"]),
    ("lead", [r"\bteam lead\b", r"\btech lead\b", r"\blead\b", r"ראש צוות", r"\bprincipal\b"]),
    ("senior", [r"\bsenior\b", r"\bsr\.?\b", r"סניור", r"בכיר", r"בכירה"]),
    ("junior", [r"\bjunior\b", r"\bjr\.?\b", r"ג'וניור", r"ג׳וניור", r"\bgraduate\b"]),
    ("entry", [r"entry[- ]level", r"ללא ניסיון", r"\bintern(ship)?\b", r"סטודנט"]),
]
YEARS_RE = re.compile(r"(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?|שנות|שנים)", re.I)

SALARY_RE = re.compile(
    r"(?P<pre>₪|\$)?\s*(?P<a>\d{1,3}(?:[,.]\d{3})+|\d+(?:\.\d+)?\s*[kK]?)\s*(?:-|–|to|עד)\s*(?:₪|\$)?\s*"
    r"(?P<b>\d{1,3}(?:[,.]\d{3})+|\d+(?:\.\d+)?\s*[kK]?)\s*(?P<cur>₪|nis|ils|ש\"ח|ש״ח|שח|\$|usd)?",
    re.I,
)
CURRENCY_HINT_RE = re.compile(r"₪|\bnis\b|\bils\b|ש\"ח|ש״ח|\bשח\b|\$|\busd\b|salary|שכר", re.I)

ATS_HOSTS = {
    "greenhouse": ["greenhouse.io"], "lever": ["lever.co"], "comeet": ["comeet.com", "comeet.co"],
    "workday": ["myworkdayjobs.com", "workday.com"], "smartrecruiters": ["smartrecruiters.com"],
    "ashby": ["ashbyhq.com"], "taleo": ["taleo.net"], "successfactors": ["successfactors"],
    "linkedin": ["linkedin.com"], "alljobs": ["alljobs.co.il"], "drushim": ["drushim.co.il"],
    "jobmaster": ["jobmaster.co.il"], "indeed": ["indeed.com"],
}

COMPANY_SUFFIXES = [r"\bltd\.?\b", r"\binc\.?\b", r"\bllc\b", r"\bcorp\.?\b", r"\bco\.?\b", r"בע\"?מ", r"בע״מ",
                    r"\bgroup\b", r"\bisrael\b", r"ישראל"]


@dataclass
class NormalizedJob:
    raw: RawJob
    employer: str
    employer_normalized: str
    title: str
    title_normalized: str
    description_original: str
    description_normalized: str
    language: str
    location: str | None
    location_normalized: str | None
    work_arrangement: str
    office_days: int | None
    employment_type: str
    seniority: str
    years_required: int | None
    salary_min: int | None
    salary_max: int | None
    salary_currency: str | None
    salary_period: str | None
    posted_at: datetime | None
    canonical_url: str | None
    application_url: str | None
    application_platform: str | None
    injection_flags: list[str] = field(default_factory=list)

    @property
    def dedupe_key(self) -> str:
        return f"{self.employer_normalized}|{self.title_normalized}|{self.location_normalized or ''}"


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("‏", "").replace("‎", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def detect_language(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    hebrew = sum(1 for c in letters if HEBREW_RE.match(c))
    return "he" if hebrew / len(letters) > 0.3 else "en"


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")
             and k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower().removeprefix("www."), path, urlencode(sorted(query)), ""))


def platform_for(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url).netloc.lower()
    for platform, hosts in ATS_HOSTS.items():
        if any(h in host for h in hosts):
            return platform
    return "employer_site"


def normalize_employer(name: str) -> str:
    n = clean_text(name).lower()
    for pattern in COMPANY_SUFFIXES:
        n = re.sub(pattern, " ", n)
    n = re.sub(r"[^\w֐-׿]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def normalize_title(title: str) -> str:
    t = clean_text(title).lower()
    t = re.sub(r"\((m|f|w|d)(\s*/\s*(m|f|w|d))+\)", " ", t)
    t = re.sub(r"(?<=[֐-׿])/(ית|ת|ה|יות)\b", "", t)  # Hebrew gender suffixes: מפתח/ת
    t = re.sub(r"\bsr\.?\b", "senior", t)
    t = re.sub(r"\bjr\.?\b", "junior", t)
    t = re.sub(r"[^\w֐-׿+#/]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_location(location: str | None, description: str = "") -> str | None:
    hay = f"{location or ''}\n{description[:500]}".lower()
    for city, aliases in CITY_ALIASES.items():
        if any(a in hay for a in aliases):
            return city
    return location.strip().lower() if location else None


def _first_match(patterns: dict[str, list[str]], text: str) -> str | None:
    for label, pats in patterns.items():
        if any(re.search(p, text, re.I) for p in pats):
            return label
    return None


def detect_arrangement(hint: str | None, text: str) -> tuple[str, int | None]:
    hint_norm = (hint or "").strip().lower().replace("-", "").replace(" ", "")
    mapping = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "office": "onsite"}
    arrangement = mapping.get(hint_norm) or _first_match(ARRANGEMENT_PATTERNS, text) or "unknown"
    days = None
    m = re.search(r"(\d)\s*days? (?:a|per) week (?:in|at) the office", text, re.I) or \
        re.search(r"(\d)\s*ימים (?:מהמשרד|במשרד)", text)
    if m:
        days = int(m.group(1))
    elif arrangement == "onsite":
        days = 5
    elif arrangement == "remote":
        days = 0
    return arrangement, days


def detect_seniority(title: str, text: str) -> tuple[str, int | None]:
    years = None
    ym = YEARS_RE.search(text)
    if ym:
        years = int(ym.group(1))
    for level, pats in SENIORITY_PATTERNS:
        if any(re.search(p, title, re.I) for p in pats):
            return level, years
    if years is not None:
        if years <= 1:
            return "junior", years
        if years <= 3:
            return "mid", years
        return "senior", years
    return "unknown", years


def _to_int(value: str) -> int:
    v = value.strip().lower().replace(" ", "")
    mult = 1000 if v.endswith("k") else 1
    v = v.rstrip("k").replace(",", "")
    if v.count(".") == 1 and len(v.split(".")[1]) == 3:
        v = v.replace(".", "")
    return int(float(v) * mult)


def parse_salary(text: str | None) -> tuple[int | None, int | None, str | None, str | None]:
    if not text or not CURRENCY_HINT_RE.search(text):
        return None, None, None, None
    m = SALARY_RE.search(text)
    if not m:
        return None, None, None, None
    lo, hi = sorted((_to_int(m.group("a")), _to_int(m.group("b"))))
    cur_raw = (m.group("cur") or m.group("pre") or "").lower()
    currency = "USD" if cur_raw in {"$", "usd"} else "ILS"
    period = "year" if lo >= 150_000 or re.search(r"annual|per year|לשנה|שנתי", text, re.I) else "month"
    if lo < 1000:  # e.g. "25-30k" already expanded; anything still tiny is hourly or noise
        return None, None, None, None
    return lo, hi, currency, period


def detect_injection(text: str) -> list[str]:
    return [p for p in INJECTION_PATTERNS if re.search(p, text, re.I)]


def normalize(raw: RawJob) -> NormalizedJob:
    title = clean_text(raw.title)
    employer = clean_text(raw.employer)
    description = clean_text(raw.description)
    hay = f"{title}\n{description}\n{raw.location or ''}"
    arrangement, office_days = detect_arrangement(raw.work_arrangement, hay)
    employment = _first_match(EMPLOYMENT_PATTERNS, f"{raw.employment_type or ''}\n{hay}") or "unknown"
    seniority, years = detect_seniority(title, description)
    smin, smax, cur, period = parse_salary(raw.salary_text or description)
    app_url = raw.application_url or raw.source_url
    return NormalizedJob(
        raw=raw,
        employer=employer,
        employer_normalized=normalize_employer(employer),
        title=title,
        title_normalized=normalize_title(title),
        description_original=raw.description or "",
        description_normalized=description.lower(),
        language=detect_language(f"{title} {description}"),
        location=raw.location,
        location_normalized=normalize_location(raw.location, description),
        work_arrangement=arrangement,
        office_days=office_days,
        employment_type=employment,
        seniority=seniority,
        years_required=years,
        salary_min=smin,
        salary_max=smax,
        salary_currency=cur,
        salary_period=period,
        posted_at=raw.posted_at,
        canonical_url=canonical_url(raw.source_url),
        application_url=app_url,
        application_platform=raw.extra.get("platform") or platform_for(app_url),
        injection_flags=detect_injection(hay),
    )
