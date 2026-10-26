import yaml

from careerpilot.matching.engine import best_match, match_job, score_job
from careerpilot.profile.service import load_preferences, load_profile_view
from tests.conftest import job_by_source_id


def _latest(s, sid):
    return best_match(s, job_by_source_id(s, sid).id)


def test_scores_are_explainable(ingested_db):
    with ingested_db.session() as s:
        m = _latest(s, "S-100")
        assert 0 <= m.score <= 100
        assert m.eligible
        assert {"informatica", "oracle", "control-m"} <= set(m.matching_skills)
        assert set(m.component_scores) == {"skills", "track", "conditions", "location", "seniority"}
        assert m.explanation.startswith("Data, DWH & BI")
        assert m.relevant_experience and all(r["source"].startswith("master_cv:") for r in m.relevant_experience)
        assert "minimum salary preference not set" in m.uncertainties  # unknown preference, not assumed


def test_separate_scores_per_track(ingested_db):
    with ingested_db.session() as s:
        ai = job_by_source_id(s, "S-103")
        tracks = {m.track for m in ai.matches if m.is_latest}
        assert "ai_automation" in tracks
        mkt = _latest(s, "S-104")
        assert mkt.track == "marketing"
        assert mkt.weights == {"skills": 30, "track": 20, "conditions": 15, "location": 15, "seniority": 20}


def test_language_requirement_is_a_hard_filter(ingested_db):
    with ingested_db.session() as s:
        m = _latest(s, "S-107")
        assert not m.eligible and m.recommendation == "reject"
        assert any(f["filter"] == "language_requirement" and not f["passed"] for f in m.hard_filter_results)


def test_missing_certifications_are_listed_not_claimed(ingested_db):
    with ingested_db.session() as s:
        m = _latest(s, "S-102")
        assert any("cissp" in x for x in m.missing_requirements)
        assert "cissp" not in m.matching_skills


def test_seniority_is_assessed_honestly(ingested_db):
    with ingested_db.session() as s:
        m = _latest(s, "S-106")  # Head of Growth, 10+ years, team of 12
        assert m.component_scores["seniority"] <= 0.2
        assert any("10+ years" in x for x in m.missing_requirements)
        assert m.recommendation != "apply"


def test_injected_instructions_force_manual_review(ingested_db):
    with ingested_db.session() as s:
        m = _latest(s, "S-108")
        assert m.recommendation == "review"
        assert any("untrusted" in u for u in m.uncertainties)


def test_hard_exclusions_override_scores(ingested_db, settings):
    prefs_path = settings.config_dir / "preferences.yaml"
    prefs = yaml.safe_load(prefs_path.read_text())
    prefs["global"]["excluded_employers"] = ["example telecom"]
    prefs["global"]["min_monthly_salary_ils"] = 40000
    prefs_path.write_text(yaml.safe_dump(prefs, allow_unicode=True))
    with ingested_db.session() as s:
        view = load_profile_view(s, settings)
        job = job_by_source_id(s, "S-100")
        r = score_job(job, "data_bi", view, load_preferences(settings), settings)
        failed = {f["filter"] for f in r.hard_filters if not f["passed"]}
        assert failed == {"excluded_employer", "min_salary"}
        assert r.recommendation == "reject" and r.score > 0  # score kept for transparency


def test_wording_differences_do_not_reject(ingested_db):
    with ingested_db.session() as s:
        m = _latest(s, "S-105")  # Hebrew posting
        assert m.eligible and "informatica" in m.matching_skills


def test_weights_are_configurable(ingested_db, settings):
    prefs_path = settings.config_dir / "preferences.yaml"
    prefs = yaml.safe_load(prefs_path.read_text())
    prefs["tracks"]["data_bi"]["weights"] = {"skills": 100, "track": 0, "conditions": 0, "location": 0, "seniority": 0}
    prefs_path.write_text(yaml.safe_dump(prefs, allow_unicode=True))
    with ingested_db.session() as s:
        view = load_profile_view(s, settings)
        job = job_by_source_id(s, "S-100")
        r = score_job(job, "data_bi", view, load_preferences(settings), settings)
        assert r.score == round(r.component_scores["skills"] * 100, 1)


def test_rematch_keeps_history(ingested_db, settings):
    with ingested_db.session() as s:
        view = load_profile_view(s, settings)
        job = job_by_source_id(s, "S-101")
        before = len(job.matches)
        match_job(s, job, view, settings)
        s.refresh(job)
        assert len(job.matches) > before
        assert sum(m.is_latest for m in job.matches) == len({m.track for m in job.matches if m.is_latest})
