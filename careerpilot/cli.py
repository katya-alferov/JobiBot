"""Command line entry point: `careerpilot <command>` or `python -m careerpilot <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from careerpilot.config import PROJECT_ROOT, load_settings
from careerpilot.cost_guard import CostPolicyViolation, validate_startup


def _db(settings):
    from careerpilot.storage.db import Database

    return Database(settings)


def cmd_check(settings, args):
    for line in validate_startup(settings):
        print(f"  ok  {line}")


def cmd_init(settings, args):
    _db(settings)
    print(f"Database ready: {settings.resolved_database_url()}")


def cmd_import_cv(settings, args):
    from careerpilot.profile.service import import_master_cv

    db = _db(settings)
    with db.session() as s:
        m = import_master_cv(s, settings, Path(args.path), make_primary=not args.reference, notes=args.notes)
        print(f"Imported {m.original_filename} (sha256 {m.sha256[:12]}), primary={m.is_primary}")


def cmd_compare_cv(settings, args):
    from careerpilot.profile.cv_parser import compare_cvs, parse_docx

    diff = compare_cvs(parse_docx(Path(args.reference)), parse_docx(Path(args.candidate)))
    print(json.dumps(diff, ensure_ascii=False, indent=2))


def cmd_facts(settings, args):
    from sqlalchemy import select

    from careerpilot.storage.models import VerifiedFact

    db = _db(settings)
    with db.session() as s:
        q = select(VerifiedFact)
        if not args.all:
            q = q.where(VerifiedFact.status != "verified")
        for f in s.scalars(q):
            print(f"[{f.id}] {f.status:20} {f.category}/{f.key}: {f.value}\n      source: {f.source}")


def cmd_confirm_fact(settings, args):
    from careerpilot.profile.service import confirm_fact

    db = _db(settings)
    with db.session() as s:
        f = confirm_fact(s, args.id, value=args.value, reject=args.reject)
        print(f"Fact {f.id} is now {f.status}: {f.value}")


def cmd_check_links(settings, args):
    from careerpilot.profile.service import verify_profile_links

    db = _db(settings)
    with db.session() as s:
        print(json.dumps(verify_profile_links(s), indent=2))


def cmd_confirm_link(settings, args):
    from careerpilot.profile.service import get_profile
    from careerpilot.storage.audit import audit

    db = _db(settings)
    with db.session() as s:
        p = get_profile(s)
        attr = {"linkedin": "linkedin_profile", "website": "personal_website"}[args.which]
        if not getattr(p, f"{attr}_url"):
            sys.exit(f"No {args.which} URL in config/profile.yaml")
        setattr(p, f"{attr}_verified", True)
        audit(s, "profile_link_confirmed_by_user", actor="user", link=args.which, url=getattr(p, f"{attr}_url"))
        print(f"{args.which} marked verified: {getattr(p, f'{attr}_url')}")


def cmd_import_jobs(settings, args):
    from careerpilot.ingestion.pipeline import run_source
    from careerpilot.sources.adapters import AlertEmailSource, FixtureSource, ManualImportSource

    path = Path(args.path)
    adapter = (AlertEmailSource(path) if path.is_dir() else
               FixtureSource(path) if args.synthetic else ManualImportSource(path))
    db = _db(settings)
    with db.session() as s:
        st = run_source(s, settings, adapter)
        print(f"{st.source}: fetched {st.fetched}, new {st.new_jobs}, duplicates merged {st.merged_duplicates}"
              + (f", errors {st.errors}" if st.errors else ""))


def cmd_match(settings, args):
    from sqlalchemy import select

    from careerpilot.matching.engine import match_job
    from careerpilot.profile.service import load_profile_view
    from careerpilot.storage.models import JobPosting

    db = _db(settings)
    with db.session() as s:
        view = load_profile_view(s, settings)
        for job in s.scalars(select(JobPosting)):
            for m in match_job(s, job, view, settings):
                print(f"{m.score:5.1f} {m.recommendation:7} {m.track:14} {job.title} @ {job.employer}")


def cmd_prepare(settings, args):
    from careerpilot.applications.service import prepare_application
    from careerpilot.profile.service import load_profile_view
    from careerpilot.storage.models import JobPosting

    db = _db(settings)
    with db.session() as s:
        job = s.get(JobPosting, args.job_id)
        app = prepare_application(s, settings, load_profile_view(s, settings), job)
        print(f"{app.id}: {app.status} ({app.resume_variant.version_id}); next: {app.next_action}")


def cmd_submit(settings, args):
    from careerpilot.applications.adapters import MockApplicationAdapter
    from careerpilot.applications.service import submit_application
    from careerpilot.storage.models import Application

    db = _db(settings)
    with db.session() as s:
        app = s.get(Application, args.app_id)
        att = submit_application(s, settings, app, [MockApplicationAdapter()], retry_reason=args.retry_reason)
        print(f"{att.result} (simulated={att.simulated}); status {app.status}")
        if att.error:
            print("  " + att.error)


def cmd_answer(settings, args):
    from careerpilot.applications.service import provide_answer
    from careerpilot.storage.models import Application

    db = _db(settings)
    with db.session() as s:
        app = s.get(Application, args.app_id)
        provide_answer(s, app, args.question, args.answer)
        print(f"{app.id}: {app.status}")


def cmd_mark_submitted(settings, args):
    from careerpilot.applications.service import record_manual_submission
    from careerpilot.storage.models import Application

    db = _db(settings)
    with db.session() as s:
        app = s.get(Application, args.app_id)
        record_manual_submission(s, app, args.note)
        print(f"{app.id}: {app.status}")


def cmd_status(settings, args):
    from careerpilot.applications.service import record_outcome
    from careerpilot.storage.models import Application

    db = _db(settings)
    with db.session() as s:
        app = s.get(Application, args.app_id)
        record_outcome(s, app, args.status.upper(), args.note)
        print(f"{app.id}: {app.status}")


def cmd_run_daily(settings, args):
    from careerpilot.scheduler.runner import run_daily

    run = run_daily(_db(settings), settings)
    print(f"Run {run.id}: {run.status}")
    print(json.dumps(run.stats, indent=2, ensure_ascii=False))


def cmd_scheduler(settings, args):  # pragma: no cover
    from careerpilot.scheduler.runner import loop

    loop(_db(settings), settings, args.hours)


def cmd_report(settings, args):
    from careerpilot.notifications.reports import write_daily_report

    db = _db(settings)
    with db.session() as s:
        print(write_daily_report(s, settings))


def cmd_export(settings, args):
    from careerpilot.applications.history import export_history

    db = _db(settings)
    with db.session() as s:
        for p in export_history(s, settings.data_dir / "exports", args.format):
            print(p)


def cmd_backup(settings, args):
    from careerpilot.applications.history import backup

    print(backup(settings))


def cmd_delete_history(settings, args):
    from careerpilot.applications.history import delete_history

    confirm = input('This permanently deletes all applications and CV variants. Type "DELETE MY HISTORY": ')
    db = _db(settings)
    with db.session() as s:
        print(delete_history(s, settings, confirm=confirm))


def cmd_dashboard(settings, args):  # pragma: no cover
    subprocess.run([sys.executable, "-m", "streamlit", "run",
                    str(PROJECT_ROOT / "careerpilot" / "dashboard" / "app.py")], check=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="careerpilot", description="CareerPilot Israel (zero cost, local)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Validate zero-cost configuration").set_defaults(fn=cmd_check)
    sub.add_parser("init", help="Create or migrate the local database").set_defaults(fn=cmd_init)
    c = sub.add_parser("import-cv", help="Import a master CV (.docx); the original is never changed")
    c.add_argument("path")
    c.add_argument("--reference", action="store_true", help="Store as a reference version, not the master")
    c.add_argument("--notes")
    c.set_defaults(fn=cmd_import_cv)
    c = sub.add_parser("compare-cv", help="Show differences between two CV versions")
    c.add_argument("reference")
    c.add_argument("candidate")
    c.set_defaults(fn=cmd_compare_cv)
    c = sub.add_parser("facts", help="List facts that need confirmation (--all for every fact)")
    c.add_argument("--all", action="store_true")
    c.set_defaults(fn=cmd_facts)
    c = sub.add_parser("confirm-fact", help="Confirm (or --reject) a fact by id")
    c.add_argument("id", type=int)
    c.add_argument("--value")
    c.add_argument("--reject", action="store_true")
    c.set_defaults(fn=cmd_confirm_fact)
    sub.add_parser("check-links", help="Check website and LinkedIn URLs resolve").set_defaults(fn=cmd_check_links)
    c = sub.add_parser("confirm-link", help="Mark a link verified after opening it yourself")
    c.add_argument("which", choices=["linkedin", "website"])
    c.set_defaults(fn=cmd_confirm_link)
    c = sub.add_parser("import-jobs", help="Import jobs from a .json/.csv file or a folder of .eml alerts")
    c.add_argument("path")
    c.add_argument("--synthetic", action="store_true", help="Treat the file as synthetic demo data")
    c.set_defaults(fn=cmd_import_jobs)
    sub.add_parser("match", help="Score every job for each relevant career track").set_defaults(fn=cmd_match)
    c = sub.add_parser("prepare", help="Prepare an application for a job id")
    c.add_argument("job_id")
    c.set_defaults(fn=cmd_prepare)
    c = sub.add_parser("submit", help="Run the submission gate (simulated while DRY_RUN=true)")
    c.add_argument("app_id")
    c.add_argument("--retry-reason")
    c.set_defaults(fn=cmd_submit)
    c = sub.add_parser("answer", help="Give your own answer to an application question")
    c.add_argument("app_id")
    c.add_argument("question")
    c.add_argument("answer")
    c.set_defaults(fn=cmd_answer)
    c = sub.add_parser("mark-submitted", help="Record that you applied manually via the official link")
    c.add_argument("app_id")
    c.add_argument("note")
    c.set_defaults(fn=cmd_mark_submitted)
    c = sub.add_parser("status", help="Record an outcome: interview, offer, rejected, withdrawn")
    c.add_argument("app_id")
    c.add_argument("status")
    c.add_argument("--note")
    c.set_defaults(fn=cmd_status)
    sub.add_parser("run-daily", help="Discover, match, prepare and (gated) submit").set_defaults(fn=cmd_run_daily)
    c = sub.add_parser("scheduler", help="Keep running daily while this terminal stays open")
    c.add_argument("--hours", type=float, default=24)
    c.set_defaults(fn=cmd_scheduler)
    sub.add_parser("report", help="Write today's report to data/reports").set_defaults(fn=cmd_report)
    c = sub.add_parser("export", help="Export application history")
    c.add_argument("--format", choices=["csv", "json", "both"], default="both")
    c.set_defaults(fn=cmd_export)
    sub.add_parser("backup", help="Back up the database and documents").set_defaults(fn=cmd_backup)
    sub.add_parser("delete-history", help="Permanently delete application history").set_defaults(fn=cmd_delete_history)
    sub.add_parser("dashboard", help="Open the local Streamlit dashboard").set_defaults(fn=cmd_dashboard)
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    settings = load_settings()
    try:
        validate_startup(settings)
    except CostPolicyViolation as exc:
        sys.exit(f"Refusing to start: {exc}")
    args.fn(settings, args)


if __name__ == "__main__":
    main()
