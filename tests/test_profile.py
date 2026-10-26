import os
import stat

from sqlalchemy import select

from careerpilot.profile.cv_parser import compare_cvs, parse_docx
from careerpilot.profile.service import (
    confirm_fact,
    get_profile,
    import_master_cv,
    load_profile_view,
    sha256_file,
    verify_url,
)
from careerpilot.storage.models import ResumeMaster, VerifiedFact
from tests.conftest import make_synthetic_cv


def test_parser_extracts_structure(cv_path):
    cv = parse_docx(cv_path)
    assert cv.name == "DANA EXAMPLE"
    assert cv.email == "dana@example.test"
    assert [r.organization for r in cv.experience][:2] == ["Example Telecom (Billing Team)", "Example Card"]
    assert cv.experience[0].start == "11/2024" and cv.experience[0].end == "Present"
    assert cv.experience[3].organization is None  # freelance role without an employer
    assert ("English", "Fluent") in cv.languages
    groups = {g.label: g.items for g in cv.skills}
    assert "Microsoft SQL Server (MSS)" in groups["Databases"]


def test_import_preserves_original_and_stores_read_only_copy(db, settings, cv_path):
    before = sha256_file(cv_path)
    with db.session() as s:
        master = import_master_cv(s, settings, cv_path)
        assert master.sha256 == before
        stored = master.stored_path
    assert sha256_file(cv_path) == before  # original unchanged
    mode = os.stat(stored).st_mode
    assert not mode & stat.S_IWUSR


def test_cv_facts_are_verified_and_sourced(profiled_db, settings):
    with profiled_db.session() as s:
        view = load_profile_view(s, settings)
        assert all(f.source for f in view.verified)
        assert any(f.category == "employment" and "Example Telecom" in f.value for f in view.verified)
        assert {"informatica", "oracle", "seo", "google analytics"} <= view.skills


def test_facts_not_on_cv_stay_unverified(profiled_db, settings):
    with profiled_db.session() as s:
        view = load_profile_view(s, settings)
        side = next(f for f in view.unverified if f.key == "side-brand")
        assert side.status == "needs_confirmation"
        assert "Example Swim Brand" not in view.verified_text()


def test_confirming_a_fact_bumps_profile_version(profiled_db, settings):
    with profiled_db.session() as s:
        fact = s.scalars(select(VerifiedFact).where(VerifiedFact.key == "side-brand")).one()
        v1 = get_profile(s).version
        confirm_fact(s, fact.id)
        assert fact.status == "verified" and fact.source.startswith("user_confirmed")
        assert get_profile(s).version == v1 + 1


def test_reimport_is_idempotent_and_reference_versions_compare(db, settings, cv_path, tmp_path):
    with db.session() as s:
        import_master_cv(s, settings, cv_path)
        import_master_cv(s, settings, cv_path)
        assert len(list(s.scalars(select(ResumeMaster)))) == 1
    newer = make_synthetic_cv(tmp_path / "v2.docx", extra_skill="UML")
    with db.session() as s:
        ref = import_master_cv(s, settings, newer, make_primary=False)
        assert not ref.is_primary
        primary = s.scalars(select(ResumeMaster).where(ResumeMaster.is_primary.is_(True))).one()
        assert primary.sha256 == sha256_file(cv_path)
    diff = compare_cvs(parse_docx(cv_path), parse_docx(newer))
    assert diff["skills_added"] == ["UML"]


def test_links_are_unset_until_supplied(profiled_db):
    with profiled_db.session() as s:
        p = get_profile(s)
        assert p.personal_website_url is None
        assert p.linkedin_profile_url == "https://www.linkedin.com/in/dana-example"


def test_url_verification_uses_injected_client():
    import httpx

    ok_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    bad_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert verify_url("https://site.example", client=ok_client)[0] is True
    assert verify_url("https://site.example", client=bad_client) == (False, "HTTP 404")
    assert verify_url("", client=ok_client) == (False, "not set")
