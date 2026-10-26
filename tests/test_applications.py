import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from careerpilot.applications import state as S
from careerpilot.applications.adapters import MockApplicationAdapter
from careerpilot.applications.gate import authorize
from careerpilot.applications.service import (
    mark_job_verified,
    prepare_application,
    provide_answer,
    record_manual_submission,
    record_outcome,
    submit_application,
)
from careerpilot.profile.service import load_profile_view, sha256_file
from careerpilot.storage.models import (
    Application,
    ApplicationAttempt,
    AuditEvent,
    ImmutableRecordError,
    utcnow,
)
from tests.conftest import job_by_source_id


def _ready_app(s, settings, sid="S-100"):
    job = job_by_source_id(s, sid)
    mark_job_verified(s, job, "checked on the employer site")
    return prepare_application(s, settings, load_profile_view(s, settings), job)


LIVE = dict(dry_run=False, submissions_kill_switch=False)


def test_prepared_application_is_ready_with_frozen_materials(ingested_db, settings):
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        assert app.status == S.READY_TO_SUBMIT
        kinds = {m.kind for m in app.materials}
        assert {"cv_docx", "cover_letter"} <= kinds
        cv = next(m for m in app.materials if m.kind == "cv_docx")
        assert cv.sha256 == app.resume_variant.docx_sha256 == sha256_file(Path(cv.snapshot_path))
        assert not os.stat(cv.snapshot_path).st_mode & stat.S_IWUSR
        assert app.resume_variant.locked
        assert [e.to_status for e in app.status_events] == [S.PREPARED, S.READY_TO_SUBMIT]


def test_dry_run_prevents_every_real_submission(ingested_db, settings):
    """Even when matching recommends 'apply' and every other check passes, DRY_RUN sends nothing."""
    adapter = MockApplicationAdapter()
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        assert app.job.matches and any(m.recommendation == "apply" for m in app.job.matches)
        decision = authorize(s, settings, app, adapter)
        assert decision.application_ready and not decision.real_submission_allowed
        att = submit_application(s, settings, app, [adapter])
        assert att.result == "SIMULATED" and att.simulated
        assert adapter.submit_calls == 0 and not adapter.received
        assert app.status == S.READY_TO_SUBMIT and app.submitted_at is None


def test_kill_switch_alone_blocks_real_submission(ingested_db, make_settings):
    settings = make_settings(dry_run=False, submissions_kill_switch=True)
    adapter = MockApplicationAdapter()
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        att = submit_application(s, settings, app, [adapter])
        assert att.simulated and adapter.submit_calls == 0


def test_dry_run_holds_for_every_job_in_a_daily_run(ingested_db, settings):
    from careerpilot.scheduler.runner import run_daily
    from careerpilot.storage.models import JobPosting

    adapter = MockApplicationAdapter()
    with ingested_db.session() as s:
        for job in s.scalars(select(JobPosting)):
            mark_job_verified(s, job, "test")
    run = run_daily(ingested_db, settings, sources=[], app_adapters=[adapter])
    assert run.stats["prepared"] >= 1
    assert adapter.submit_calls == 0
    with ingested_db.session() as s:
        assert s.scalar(select(func.count(ApplicationAttempt.id)).where(ApplicationAttempt.simulated.is_(False))) == 0


def test_authorized_mock_submission_end_to_end(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    adapter = MockApplicationAdapter("confirm")
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        att = submit_application(s, settings, app, [adapter])
        assert att.result == "CONFIRMED" and not att.simulated and att.confirmation_ref.startswith("MOCK-")
        assert att.timezone == "Asia/Jerusalem" and att.local_time.endswith(("+02:00", "+03:00"))
        assert app.status == S.CONFIRMED and app.submitted_at
        assert adapter.submit_calls == 1
        assert app.reminders and app.next_action == "Awaiting update"
        assert S.display_status(app) == "CONFIRMED · Awaiting update"


def test_retry_cannot_duplicate_an_application(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    adapter = MockApplicationAdapter("confirm")
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        submit_application(s, settings, app, [adapter])
        again = submit_application(s, settings, app, [adapter], retry_reason="user clicked twice")
        assert again.result == "NOT_AUTHORIZED" and "already applied" in again.error
        assert adapter.submit_calls == 1 and len(adapter.received) == 1
        # Preparing the same job again returns the same application (idempotency key).
        same = prepare_application(s, settings, load_profile_view(s, settings), app.job)
        assert same.id == app.id
        assert s.scalar(select(func.count(Application.id))) == 1


def test_same_position_on_another_source_is_not_applied_twice(ingested_db, make_settings):
    from careerpilot.ingestion.pipeline import run_source
    from careerpilot.sources.adapters import FixtureSource
    from tests.conftest import FIXTURES

    settings = make_settings(**LIVE)
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        submit_application(s, settings, app, [MockApplicationAdapter()])
        run_source(s, settings, FixtureSource(FIXTURES / "synthetic_jobs_other_board.json", name="other_board"))
        dup = job_by_source_id(s, "B-9")
        assert dup.id == app.job_id  # merged into the same underlying job
        assert prepare_application(s, settings, load_profile_view(s, settings), dup).id == app.id


def test_uncertain_result_is_not_retried_blindly(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    adapter = MockApplicationAdapter("uncertain")
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        first = submit_application(s, settings, app, [adapter])
        assert first.result == "UNCERTAIN" and app.status == S.SUBMISSION_ATTEMPTED
        # The employer actually received it: the retry checks for receipt and does NOT resubmit.
        second = submit_application(s, settings, app, [adapter], retry_reason="timeout")
        assert second.result == "CONFIRMED" and "not resubmitted" in second.retry_reason
        assert adapter.submit_calls == 1 and app.status == S.CONFIRMED


def test_uncertain_without_receipt_lookup_needs_the_user(ingested_db, make_settings):
    settings = make_settings(**LIVE)

    class NoLookup(MockApplicationAdapter):
        def check_receipt(self, package):
            from careerpilot.applications.adapters import ReceiptCheck

            return ReceiptCheck(found=None)

    adapter = NoLookup("uncertain")
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        submit_application(s, settings, app, [adapter])
        retry = submit_application(s, settings, app, [adapter])
        assert retry.result == "BLOCKED" and adapter.submit_calls == 1
        assert app.status == S.NEEDS_USER_INPUT


def test_captcha_stops_automation_and_gives_link(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    adapter = MockApplicationAdapter("captcha")
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        att = submit_application(s, settings, app, [adapter])
        assert att.result == "NOT_AUTHORIZED" and "captcha" in att.error
        assert adapter.submit_calls == 0


def test_account_creation_is_never_automated(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    adapter = MockApplicationAdapter("account_required")
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        att = submit_application(s, settings, app, [adapter])
        assert att.result == "NOT_AUTHORIZED" and "create_account" in att.error


def test_unapproved_domain_uses_assisted_manual_flow(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    with ingested_db.session() as s:
        app = _ready_app(s, settings, "S-101")  # URL on the mock domain but job board differs
        app.application_url = "https://careers.unknown-employer.test/apply"
        att = submit_application(s, settings, app, [MockApplicationAdapter()])
        assert att.adapter_name == "assisted_manual" and att.result == "NOT_AUTHORIZED"
        assert "domain_approved" in att.error
        record_manual_submission(s, app, "Applied on the employer site; got an email receipt")
        assert app.status == S.SUBMITTED and app.status_events[-1].source == "user"


def test_failed_submission_can_be_retried_safely(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    adapter = MockApplicationAdapter("error")
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        att = submit_application(s, settings, app, [adapter])
        assert att.result == "FAILED" and app.status == S.FAILED
        S.transition(s, app, S.READY_TO_SUBMIT, source="user", note="retry after error")
        adapter.behavior = "confirm"
        att2 = submit_application(s, settings, app, [adapter], retry_reason="server error earlier")
        assert att2.result == "CONFIRMED" and att2.retry_reason == "server error earlier"


def test_missing_and_sensitive_answers_require_the_candidate(ingested_db, make_settings, settings):
    with ingested_db.session() as s:
        job = job_by_source_id(s, "S-101")
        job.application_questions = ["What are your salary expectations?", "Why this company?",
                                     {"question": "Gender (optional)", "required": False}]
        mark_job_verified(s, job, "test")
        app = prepare_application(s, settings, load_profile_view(s, settings), job)
        assert app.status == S.NEEDS_USER_INPUT
        assert "salary" in app.next_action.lower()
        gender = next(a for a in app.answers if a.question.startswith("Gender"))
        assert gender.answer is None  # optional sensitive question is left blank, never guessed
        provide_answer(s, app, "What are your salary expectations?", "To be discussed")
        assert app.status == S.NEEDS_USER_INPUT
        provide_answer(s, app, "Why this company?", "I like its data platform.")
        assert app.status == S.READY_TO_SUBMIT
        confirmed = {a.question: a.confirmed_by_user for a in app.answers}
        assert confirmed["Why this company?"] is True


def test_volume_limit(ingested_db, make_settings):
    settings = make_settings(**LIVE, max_daily_applications=1)
    with ingested_db.session() as s:
        a1 = _ready_app(s, settings, "S-100")
        a2 = _ready_app(s, settings, "S-101")
        assert submit_application(s, settings, a1, [MockApplicationAdapter()]).result == "CONFIRMED"
        att = submit_application(s, settings, a2, [MockApplicationAdapter()])
        assert att.result == "NOT_AUTHORIZED" and "volume_limit" in att.error


def test_state_machine_rejects_invalid_or_evidence_free_transitions(ingested_db, settings):
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        with pytest.raises(S.InvalidTransition):
            S.transition(s, app, S.OFFER, source="system")
        with pytest.raises(S.InvalidTransition):
            S.transition(s, app, S.SUBMITTED, source="system")  # no evidence, not allowed
        S.transition(s, app, S.WITHDRAWN, source="user", note="not interested")
        with pytest.raises(S.InvalidTransition):
            S.transition(s, app, S.READY_TO_SUBMIT, source="user")


def test_outcomes_and_timeline(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        submit_application(s, settings, app, [MockApplicationAdapter()])
        record_outcome(s, app, S.INTERVIEW, "Phone screen on Sunday")
        record_outcome(s, app, S.OFFER, "Offer received")
        assert [e.to_status for e in app.status_events][-3:] == [S.CONFIRMED, S.INTERVIEW, S.OFFER]
        assert all(r.done for r in app.reminders)


def test_attempts_and_audit_are_append_only(ingested_db, settings):
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        att = submit_application(s, settings, app, [MockApplicationAdapter()])
        s.flush()
        att.result = "CONFIRMED"
        with pytest.raises(ImmutableRecordError):
            s.flush()
        s.rollback()
    with ingested_db.session() as s:
        ev = s.scalars(select(AuditEvent)).first()
        s.delete(ev)
        with pytest.raises(ImmutableRecordError):
            s.flush()
        s.rollback()


def test_submitted_material_cannot_be_overwritten(ingested_db, settings):
    with ingested_db.session() as s:
        _ready_app(s, settings)
    with ingested_db.session() as s:
        app = s.scalars(select(Application)).one()
        app.materials[0].sha256 = "f" * 64
        with pytest.raises(ImmutableRecordError):
            s.flush()
        s.rollback()
    with ingested_db.session() as s:
        app = s.scalars(select(Application)).one()
        app.answers[0].answer = "changed"
        with pytest.raises(ImmutableRecordError):
            s.flush()
        s.rollback()
    with ingested_db.session() as s:
        app = s.scalars(select(Application)).one()
        app.resume_variant.docx_path = "/tmp/other.docx"
        with pytest.raises(ImmutableRecordError):
            s.flush()
        s.rollback()


def test_audit_log_covers_the_lifecycle(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    with ingested_db.session() as s:
        app = _ready_app(s, settings)
        submit_application(s, settings, app, [MockApplicationAdapter()])
        actions = {e.action for e in s.scalars(select(AuditEvent).where(AuditEvent.entity_id == app.id))}
        assert {"application_prepared", "materials_frozen", "application_attempt",
                "application_status_changed"} <= actions


def test_audit_scrubs_secrets(db):
    from careerpilot.storage.audit import audit

    with db.session() as s:
        ev = audit(s, "login_attempt", password="hunter2", details={"session_cookie": "abc"}, ok=True)
        assert ev.details["password"] == "[redacted]"
        assert ev.details["details"]["session_cookie"] == "[redacted]"
