"""Career-track classification by keywords (English and Hebrew)."""

from __future__ import annotations

import re

TRACK_IDS = ["data_bi", "grc_security", "ai_automation", "marketing", "other"]


def _hits(keywords: list[str], text: str) -> int:
    return sum(1 for k in keywords if re.search(rf"(?<!\w){re.escape(k.lower())}(?!\w)", text))


def classify_tracks(title: str, description: str, preferences: dict) -> tuple[str, dict[str, float]]:
    """Return (best track, score per track). Title hits weigh three times description hits."""
    title_l, desc_l = title.lower(), description.lower()
    scores: dict[str, float] = {}
    for track, spec in (preferences.get("tracks") or {}).items():
        kws = spec.get("title_keywords") or []
        scores[track] = 3 * _hits(kws, title_l) + _hits(kws, desc_l)
    candidates = {t: s for t, s in scores.items() if t != "other"}
    best = max(candidates, key=candidates.get, default="other")
    if not candidates or candidates[best] == 0:
        best = "other"
    return best, scores
