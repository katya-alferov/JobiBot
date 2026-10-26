import csv
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select

from careerpilot.applications.adapters import MockApplicationAdapter
from careerpilot.applications.history import (
    already_applied,
    application_record,
    backup,
    delete_history,
    due_follow_ups,
    export_history,
    stats,
)
from careerpilot.applications.service import (
    mark_job_verified,
    prepare_application,
    record_outcome,
    submit_application,
)
from careerpilot.notifications.reports import daily_report
from careerpilot.profile.service import load_profile_view
from careerpilot.scheduler.runner import run_daily
from careerpilot.sources.adapters import FixtureSource
from careerpilot.storage.db import Database
from careerpilot.storage.models import Application, JobPosting, ResumeVariant, SchedulerRun, utcnow
from tests.conftest import FIXTURES, job_by_source_id

LIVE = dict(dry_run=False, submissions_kill_switch=False)


@pytest.fixture
def submitted(ingested_db, make_settings):
    settings = make_settings(**LIVE)
    with ingested_db.session() as s:
        job = job_by_source_id(s, "S-100")
        mark_job_verified(s, job, "test")
        app = prepare_application(s, settings, load_profile_view(s, settings), job)
        submit_application(s, settings, app, [MockApplicationAdapter()])
        record_outcome(s, app, "INTERVIEW", "Screening call")
        app_id = app.id
    return ingested_db, settings, app_id


def test_record_answers_the_reporting_questions(submitted):
    db, settings, app_id = submitted
    with db.session() as s:
        r = application_record(s.get(Application, app_id))
        assert r["cv_filename"].endswith(".docx") and len(r["cv_sha256"]) == 64   # which CV, exact file
        assert r["cv_changes"]                                                  # what changed
        assert r["answers"] and r["cover_letter"]                               # what it said
        assert r["submitted_at"] and r["attempts"][0]["local_time"]            # when
        assert r["source_names"] == ["synthetic_fixtures"] and r["ats_platform"]  # where
        assert r["confirmed"] is True                                          # confirmed
        assert r["follow_ups"]                                                  # follow up
        assert already_applied(s, "Example Telecom")[0]["sent"] is True


def test_stats_and_interview_rate(submitted):
    db, _, _ = submitted
    with db.session() as s:
        st = stats(s)
        assert st["submitted"] == 1 and st["confirmed"] == 1 and st["real_attempts"] == 1
        assert st["interview_rate"]["track"]["data_bi"] == {"sent": 1, "interviews": 1, "interview_rate": 1.0}


def test_export_csv_and_json(submitted):
    db, settings, app_id = submitted
    with db.session() as s:
        paths = export_history(s, settings.data_dir / "exports")
    data = json.loads(next(p for p in paths if p.suffix == ".json").read_text(encoding="utf-8"))
    assert data[0]["application_id"] == app_id
    with open(next(p for p in paths if p.suffix == ".csv"), encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["application_id"] == app_id and rows[0]["simulated_only"] == "False"


def test_follow_up_reminders_come_due(submitted):
    from datetime import timedelta

    db, _, _ = submitted
    with db.session() as s:
        assert not due_follow_ups(s)
        assert len(due_follow_ups(s, utcnow() + timedelta(days=8))) == 1


def test_posting_disappearing_keeps_application_history(submitted):
    db, _, app_id = submitted
    with db.session() as s:
        app = s.get(Application, app_id)
        app.job.expiration_status = "expired"
    with db.session() as s:
        app = s.get(Application, app_id)
        assert app.status == "INTERVIEW" and app.materials and app.attempts


def test_backup_and_delete_history(submitted):
    db, settings, _ = submitted
    dest = backup(settings)
    con = sqlite3.connect(dest / "careerpilot.db")
    assert con.execute("select count(*) from application").fetchone()[0] == 1
    con.close()
    assert any((dest / "snapshots").rglob("*.docx"))
    with db.session() as s:
        with pytest.raises(PermissionError):
            delete_history(s, settings, confirm="yes")
        counts = delete_history(s, settings, confirm="DELETE MY HISTORY")
        assert counts["application"] == 1
    with db.session() as s:
        assert s.scalar(select(func.count(Application.id))) == 0
        assert s.scalar(select(func.count(ResumeVariant.id))) == 0
        assert s.scalar(select(func.count(JobPosting.id))) == 9  # jobs are not personal history
    assert not any((settings.data_dir / "snapshots").iterdir())


def test_daily_report_works_offline(submitted):
    db, settings, _ = submitted
    with db.session() as s:
        text = daily_report(s, settings)
    assert "Top matches by career track" in text and "synthetic_fixtures" in text


def test_scheduler_run_records_stats_and_survives_source_failure(profiled_db, settings):
    from careerpilot.sources.base import SourceAdapter, SourceMetadata

    class Down(SourceAdapter):
        name = "down"

        def search_jobs(self, query=None):
            raise RuntimeError("connection refused")

        def get_source_metadata(self):
            return SourceMetadata("down", "public_api", "free", True, "", False, "live")

    run = run_daily(profiled_db, settings, sources=[Down(), FixtureSource(FIXTURES / "synthetic_jobs.json")],
                    app_adapters=[MockApplicationAdapter()])
    assert run.status == "partial"
    assert run.stats["matched"] == 9
    assert any(src["errors"] for src in run.stats["sources"])
    with profiled_db.session() as s:
        assert s.scalar(select(func.count(SchedulerRun.id))) == 1
        assert list(settings.data_dir.glob("reports/daily-*.md"))
    # A second run does not duplicate applications or attempts.
    run2 = run_daily(profiled_db, settings, sources=[FixtureSource(FIXTURES / "synthetic_jobs.json")],
                     app_adapters=[MockApplicationAdapter()])
    assert run2.stats["prepared"] == 0 and run2.stats["attempts"] == 0


def test_alembic_migration_matches_models(make_settings, tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from careerpilot.storage.models import Base

    settings = make_settings(database_url=f"sqlite:///{tmp_path / 'migrated.db'}")
    db = Database(settings, migrate=True)
    with db.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff
    with db.session() as s:
        assert s.scalar(select(func.count(Application.id))) == 0


def test_cli_check_and_parser(capsys, monkeypatch, settings):
    from careerpilot import cli

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    cli.main(["check"])
    out = capsys.readouterr().out
    assert "ZERO_COST_MODE=true" in out and "DRY_RUN=true" in out


def test_cli_refuses_paid_configuration(monkeypatch, make_settings):
    from careerpilot import cli

    monkeypatch.setattr(cli, "load_settings", lambda: make_settings(llm_provider="openai"))
    with pytest.raises(SystemExit, match="Refusing to start"):
        cli.main(["check"])


def test_dashboard_loads(submitted, monkeypatch):
    from streamlit.testing.v1 import AppTest

    db, settings, _ = submitted
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DATABASE_URL", settings.resolved_database_url())
    monkeypatch.setenv("DRY_RUN", "true")
    app_file = Path(__file__).resolve().parents[1] / "careerpilot" / "dashboard" / "app.py"
    at = AppTest.from_file(str(app_file), default_timeout=60).run()
    assert not at.exception, at.exception
    assert any("DRY RUN" in i.value for i in at.info)
    assert [t.label for t in at.tabs][:4] == ["Overview", "Job search", "Application materials", "Application tracking"]
