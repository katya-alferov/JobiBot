"""Local scheduling. Runs only while the computer is on and awake.

Use cron (Linux/macOS) or Task Scheduler (Windows) to call `careerpilot run-daily`, or keep
`careerpilot scheduler` running in a terminal.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select

from careerpilot.applications.adapters import ApplicationAdapter, MockApplicationAdapter
from careerpilot.applications.service import prepare_application, submit_application
from careerpilot.applications.state import READY_TO_SUBMIT
from careerpilot.config import Settings
from careerpilot.ingestion.pipeline import run_source
from careerpilot.matching.engine import best_match, match_job
from careerpilot.notifications.reports import write_daily_report
from careerpilot.profile.service import load_preferences, load_profile_view
from careerpilot.sources.adapters import build_sources
from careerpilot.sources.base import SourceAdapter
from careerpilot.storage.audit import audit
from careerpilot.storage.db import Database
from careerpilot.storage.models import Application, JobPosting, SchedulerRun, utcnow

log = logging.getLogger("careerpilot.scheduler")


def run_daily(db: Database, settings: Settings, sources: list[SourceAdapter] | None = None,
              app_adapters: list[ApplicationAdapter] | None = None) -> SchedulerRun:
    """Discover -> deduplicate -> match -> prepare -> (gated) submit -> report."""
    with db.session() as s:
        run = SchedulerRun(task="daily")
        s.add(run)
        s.flush()
        run_id = run.id
    stats: dict = {"sources": [], "matched": 0, "prepared": 0, "attempts": 0, "errors": []}
    sources = sources if sources is not None else build_sources(settings)
    app_adapters = app_adapters if app_adapters is not None else [MockApplicationAdapter()]

    for adapter in sources:
        with db.session() as s:
            st = run_source(s, settings, adapter)
            stats["sources"].append({"source": st.source, "fetched": st.fetched, "new": st.new_jobs,
                                     "duplicates": st.merged_duplicates, "errors": st.errors})
            stats["errors"] += st.errors

    try:
        with db.session() as s:
            view = load_profile_view(s, settings)
            prefs = load_preferences(settings)
            jobs = list(s.scalars(select(JobPosting).where(JobPosting.expiration_status == "active")))
            for job in jobs:
                match_job(s, job, view, settings, prefs)
                stats["matched"] += 1
            limit = (prefs.get("global") or {}).get("max_daily_applications", settings.max_daily_applications)
            prepared_today = 0
            for job in sorted(jobs, key=lambda j: -(best_match(s, j.id).score if best_match(s, j.id) else 0)):
                m = best_match(s, job.id)
                if not m or m.recommendation != "apply" or prepared_today >= limit:
                    continue
                exists = s.scalars(select(Application).where(Application.job_id == job.id)).first()
                if exists:
                    continue
                try:
                    prepare_application(s, settings, view, job)
                    stats["prepared"] += 1
                    prepared_today += 1
                except Exception as exc:  # one bad job never stops the run
                    stats["errors"].append(f"prepare {job.id}: {exc}")
            for app in s.scalars(select(Application).where(Application.status == READY_TO_SUBMIT)):
                if app.attempts:  # already tried or simulated; retries are a deliberate user action
                    continue
                submit_application(s, settings, app, app_adapters)
                stats["attempts"] += 1
            write_daily_report(s, settings)
    except Exception as exc:
        stats["errors"].append(str(exc))
        log.exception("daily run failed")

    with db.session() as s:
        run = s.get(SchedulerRun, run_id)
        run.finished_at = utcnow()
        run.stats = stats
        failing = [x for x in stats["sources"] if x["errors"]]
        run.status = "failed" if stats["errors"] and not stats["matched"] else "partial" if stats["errors"] or failing else "ok"
        run.error = "; ".join(stats["errors"])[:2000] or None
        audit(s, "scheduler_run", actor="scheduler", entity_type="scheduler_run", entity_id=run.id,
              status=run.status)
    return run


def loop(db: Database, settings: Settings, interval_hours: float = 24) -> None:  # pragma: no cover
    while True:
        run_daily(db, settings)
        time.sleep(interval_hours * 3600)
