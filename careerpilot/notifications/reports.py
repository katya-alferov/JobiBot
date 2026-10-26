"""Local daily reports (Markdown files). No email or paid notification service is required."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from careerpilot.applications.history import due_follow_ups, stats
from careerpilot.config import Settings
from careerpilot.storage.models import JobMatch, JobPosting, JobSource, utcnow


def daily_report(session: Session, settings: Settings) -> str:
    since = utcnow() - timedelta(days=1)
    new_jobs = list(session.scalars(select(JobPosting).where(JobPosting.first_discovered_at >= since)))
    total_jobs = session.scalar(select(func.count(JobPosting.id))) or 0
    latest = list(session.scalars(select(JobMatch).where(JobMatch.is_latest.is_(True))))
    by_track: dict[str, list[JobMatch]] = {}
    for m in latest:
        by_track.setdefault(m.track, []).append(m)
    s = stats(session)
    lines = [
        f"# CareerPilot daily report, {utcnow():%Y-%m-%d} (UTC)",
        "",
        f"Mode: {'DRY RUN (simulated only)' if settings.dry_run else 'live'}; "
        f"kill switch {'ON' if settings.submissions_kill_switch else 'off'}.",
        "",
        f"- Jobs known: {total_jobs}; new in the last 24 hours: {len(new_jobs)}",
        f"- Applications: {s['applications']}; submitted: {s['submitted']}; confirmed: {s['confirmed']}",
        f"- Real attempts: {s['real_attempts']}; simulated: {s['simulated_attempts']}; "
        f"failed: {s['failed_attempts']}; blocked or not authorized: {s['blocked_attempts']}",
        "",
        "## Top matches by career track",
    ]
    for track, matches in sorted(by_track.items()):
        top = sorted([m for m in matches if m.eligible], key=lambda m: -m.score)[:5]
        lines.append(f"### {track} ({len(matches)} scored)")
        for m in top:
            lines.append(f"- {m.score:.0f} · {m.job.title} · {m.job.employer} · {m.recommendation}")
        if not top:
            lines.append("- none eligible")
    lines += ["", "## Follow-ups due"]
    due = due_follow_ups(session)
    lines += [f"- {r.application.job.employer}: {r.application.job.title} ({r.reason})" for r in due] or ["- none"]
    lines += ["", "## Source health"]
    for src in session.scalars(select(JobSource)):
        state = "OK" if src.operational else f"FAILING ({src.last_error})"
        last = src.last_success_at.strftime("%Y-%m-%d %H:%M") if src.last_success_at else "never"
        lines.append(f"- {src.name}: {state}; last success {last}"
                     + ("; needs your action" if src.requires_user_action else ""))
    return "\n".join(lines) + "\n"


def write_daily_report(session: Session, settings: Settings) -> Path:
    path = settings.data_dir / "reports" / f"daily-{utcnow():%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(daily_report(session, settings), encoding="utf-8")
    return path
