import os
import stat
from pathlib import Path

import pytest

from careerpilot.documents.materials import answer_question, classify_question, cover_letter
from careerpilot.documents.tailor import FactValidationError, build_plan, create_variant, validate_plan
from careerpilot.matching.engine import best_match
from careerpilot.profile.cv_parser import parse_docx
from careerpilot.profile.service import load_profile_view, sha256_file
from careerpilot.storage.models import ImmutableRecordError
from tests.conftest import job_by_source_id


def _variant(s, settings, sid, make_pdf=False):
    view = load_profile_view(s, settings)
    job = job_by_source_id(s, sid)
    return create_variant(s, settings, view, job, best_match(s, job.id), make_pdf=make_pdf), view


def test_variant_contains_only_verified_facts(ingested_db, settings):
    with ingested_db.session() as s:
        v, view = _variant(s, settings, "S-100")
        assert v.valid, v.validation
        text = parse_docx(Path(v.docx_path)).full_text
        assert "Example Swim Brand" not in text  # unverified fact never used
        master = view.parsed_cv
        cv = parse_docx(Path(v.docx_path))
        assert [r.header_text for r in cv.experience] == [r.header_text for r in master.experience]
        assert sorted(cv.all_bullets()) == sorted(master.all_bullets())  # reordered, never invented


def test_variant_is_versioned_hashed_and_read_only(ingested_db, settings):
    with ingested_db.session() as s:
        v1, _ = _variant(s, settings, "S-100")
        v2, _ = _variant(s, settings, "S-100")
        assert v1.version_id != v2.version_id
        assert v1.docx_sha256 == sha256_file(Path(v1.docx_path))
        assert not os.stat(v1.docx_path).st_mode & stat.S_IWUSR


def test_master_cv_never_overwritten(ingested_db, settings):
    with ingested_db.session() as s:
        view = load_profile_view(s, settings)
        before = sha256_file(Path(view.master.stored_path))
        _variant(s, settings, "S-104")
        assert sha256_file(Path(view.master.stored_path)) == before


def test_change_log_records_original_and_revised(ingested_db, settings):
    with ingested_db.session() as s:
        v, view = _variant(s, settings, "S-104")
        summary = next(c for c in v.changes if c.section == "summary")
        assert summary.original_text == view.parsed_cv.summary
        assert summary.revised_text.endswith(view.parsed_cv.summary)
        assert summary.material and summary.fact_source and "master_cv:" in summary.fact_source
        assert "Marketing Manager at Example Agency (2020 - 2021)" in summary.revised_text
        omit = [c for c in v.changes if c.change_type == "omit"]
        assert omit and "Development Environments" in omit[0].original_text


def test_unverified_link_is_not_used(ingested_db, settings):
    from careerpilot.profile.service import get_profile

    with ingested_db.session() as s:
        get_profile(s).linkedin_profile_verified = False
        v, _ = _variant(s, settings, "S-101")
        assert "linkedin" not in parse_docx(Path(v.docx_path)).contact_line.lower()
        assert any("LinkedIn" in w for w in v.validation["warnings"])
        get_profile(s).linkedin_profile_verified = True
        v2, _ = _variant(s, settings, "S-101")
        assert "linkedin.com/in/dana-example" in parse_docx(Path(v2.docx_path)).contact_line


def test_validator_catches_fabricated_content(ingested_db, settings):
    with ingested_db.session() as s:
        view = load_profile_view(s, settings)
        job = job_by_source_id(s, "S-100")
        plan = build_plan(view, job, best_match(s, job.id), "data_bi", settings)
        plan.experience[0].bullets.append("Led a team of 40 engineers.")
        plan.skills[0][1].append("Kubernetes")
        plan.summary += " Increased revenue by 300%."
        errors = validate_plan(plan, view)
        assert any("bullet not verified" in e for e in errors)
        assert any("Kubernetes" in e for e in errors)
        assert any("unsupported figure" in e for e in errors)


def test_validator_blocks_unverified_fact(ingested_db, settings):
    with ingested_db.session() as s:
        view = load_profile_view(s, settings)
        job = job_by_source_id(s, "S-104")
        plan = build_plan(view, job, best_match(s, job.id), "marketing", settings)
        plan.summary += " Founder & Owner, Example Swim Brand (swimwear), 2017 - 2021."
        assert any("side-brand" in e for e in validate_plan(plan, view))


def test_locked_variant_cannot_be_changed(ingested_db, settings):
    with ingested_db.session() as s:
        v, _ = _variant(s, settings, "S-100")
        v.locked = True
        s.flush()
        v.docx_sha256 = "0" * 64
        with pytest.raises(ImmutableRecordError):
            s.flush()
        s.rollback()


def test_pdf_generated_when_libreoffice_available(ingested_db, settings):
    import shutil

    if not shutil.which("soffice"):
        pytest.skip("LibreOffice not installed")
    with ingested_db.session() as s:
        v, _ = _variant(s, settings, "S-101", make_pdf=True)
        assert v.pdf_path and Path(v.pdf_path).read_bytes()[:4] == b"%PDF"
        assert v.pdf_sha256 == sha256_file(Path(v.pdf_path))


def test_answers_come_only_from_verified_data(profiled_db, settings):
    with profiled_db.session() as s:
        view = load_profile_view(s, settings)
        assert answer_question("Email", view).answer == "dana@example.test"
        for q in ["What are your salary expectations?", "Are you authorized to work in Israel?",
                  "Do you have a disability?", "Gender", "I consent to the privacy policy", "Notice period"]:
            d = answer_question(q, view)
            assert d.answer is None and d.needs_user_input and d.sensitive, q
        unknown = answer_question("Why do you want to work here?", view)
        assert unknown.answer is None and unknown.needs_user_input and not unknown.sensitive
        assert answer_question("Personal website", view).answer is None  # not supplied yet
        assert classify_question("מה ציפיות השכר שלך?") == "salary"


def test_cover_letter_uses_job_language(ingested_db, settings):
    with ingested_db.session() as s:
        view = load_profile_view(s, settings)
        he_job = job_by_source_id(s, "S-105")
        text, sources = cover_letter(view, he_job, best_match(s, he_job.id))
        assert "שלום רב" in text and "ETL & BI Developer" in text and sources
        en_job = job_by_source_id(s, "S-101")
        text, _ = cover_letter(view, en_job, best_match(s, en_job.id))
        assert text.startswith("Dear Hiring Team") and "Example Bank" in text


def test_degree_with_unconfirmed_completion_is_left_out(ingested_db, settings):
    from careerpilot.profile.service import _upsert_fact, confirm_fact, get_profile

    with ingested_db.session() as s:
        fact = _upsert_fact(s, get_profile(s), "education", "computer-science-completion",
                            "Completion status of the CS degree", "needs_confirmation", "test")
        s.flush()
        v, _ = _variant(s, settings, "S-101")
        text = parse_docx(Path(v.docx_path)).full_text
        assert "Computer Science & Business Administration" not in text
        assert any(c.change_type == "omit" and c.section == "education" and c.material for c in v.changes)
        confirm_fact(s, fact.id)
        v2, _ = _variant(s, settings, "S-101")
        assert "Computer Science & Business Administration" in parse_docx(Path(v2.docx_path)).full_text


def test_track_scoped_role_appears_only_on_allowed_tracks(ingested_db, settings):
    from careerpilot.profile.service import _upsert_fact, get_profile

    header = "Founder & Owner | Example Swim Brand (swimwear)  (2017 - 2021)"
    with ingested_db.session() as s:
        _upsert_fact(s, get_profile(s), "employment", "side-brand", header, "verified",
                     "confirmed by the candidate", {"tracks": ["marketing"]})
        s.flush()
        mkt, _ = _variant(s, settings, "S-104")
        roles = [r.header_text for r in parse_docx(Path(mkt.docx_path)).experience]
        assert header in roles
        # Placed chronologically: after the 2020 - 2021 role, before the 2017 - 2021 freelance role.
        assert roles.index(header) == roles.index("Marketing Manager | Example Agency  (2020 - 2021)") + 1
        added = [c for c in mkt.changes if c.change_type == "add_verified_role"]
        assert added and added[0].material and added[0].fact_source == "confirmed by the candidate"
        assert mkt.valid
        data, _ = _variant(s, settings, "S-100")
        assert "Example Swim Brand" not in parse_docx(Path(data.docx_path)).full_text


def test_validator_rejects_scoped_role_on_wrong_track(ingested_db, settings):
    from careerpilot.profile.cv_parser import Role
    from careerpilot.profile.service import _upsert_fact, get_profile

    header = "Founder & Owner | Example Swim Brand (swimwear)  (2017 - 2021)"
    with ingested_db.session() as s:
        _upsert_fact(s, get_profile(s), "employment", "side-brand", header, "verified",
                     "confirmed by the candidate", {"tracks": ["marketing"]})
        s.flush()
        view = load_profile_view(s, settings)
        job = job_by_source_id(s, "S-100")
        plan = build_plan(view, job, best_match(s, job.id), "data_bi", settings)
        plan.experience.append(Role("Founder & Owner", "Example Swim Brand (swimwear)", "2017 - 2021",
                                    "2017", "2021", [], header))
        assert any("limited to" in e for e in validate_plan(plan, view))
