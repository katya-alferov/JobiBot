"""Local Streamlit dashboard: `careerpilot dashboard` or `streamlit run careerpilot/dashboard/app.py`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from careerpilot.applications.history import application_record, export_history, stats  # noqa: E402
from careerpilot.applications.state import ALL_STATUSES, SENT_STATUSES, display_status  # noqa: E402
from careerpilot.config import PROJECT_ROOT, load_settings  # noqa: E402
from careerpilot.cost_guard import CostPolicyViolation, load_integrations, validate_startup  # noqa: E402
from careerpilot.storage.db import Database  # noqa: E402
from careerpilot.storage.models import (  # noqa: E402
    Application,
    ApplicationAttempt,
    JobMatch,
    JobPosting,
    JobSource,
    ResumeVariant,
    VerifiedFact,
    utcnow,
)

st.set_page_config(page_title="CareerPilot Israel", layout="wide")
settings = load_settings()
try:
    validate_startup(settings)
except CostPolicyViolation as exc:
    st.error(f"Refusing to start: {exc}")
    st.stop()


@st.cache_resource
def get_db(url: str) -> Database:
    return Database(settings)


db = get_db(settings.resolved_database_url())

mode = "DRY RUN · simulated only" if settings.dry_run else "LIVE"
kill = "kill switch ON" if settings.submissions_kill_switch else "kill switch off"
st.title("CareerPilot Israel")
(st.info if not settings.real_submissions_enabled else st.warning)(
    f"Mode: **{mode}** · {kill} · zero-cost mode on. "
    + ("No real application can be sent." if not settings.real_submissions_enabled
       else "Real submissions are possible for approved domains when every rule passes.")
)

tab_overview, tab_jobs, tab_materials, tab_apps, tab_profile, tab_settings = st.tabs(
    ["Overview", "Job search", "Application materials", "Application tracking", "Profile facts", "Settings"])

with db.session() as s:
    # ------------------------------------------------------------------ overview
    with tab_overview:
        since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        total = s.scalar(select(func.count(JobPosting.id))) or 0
        new_today = s.scalar(select(func.count(JobPosting.id)).where(JobPosting.first_discovered_at >= since)) or 0
        eligible = s.scalar(select(func.count(func.distinct(JobMatch.job_id))).where(
            JobMatch.is_latest.is_(True), JobMatch.eligible.is_(True))) or 0
        apps = list(s.scalars(select(Application)))
        attempts = list(s.scalars(select(ApplicationAttempt)))
        real = [a for a in attempts if not a.simulated]
        cols = st.columns(5)
        cols[0].metric("Jobs discovered", total)
        cols[1].metric("New today", new_today)
        cols[2].metric("Eligible jobs", eligible)
        cols[3].metric("Applications prepared", len(apps))
        cols[4].metric("Simulated attempts", sum(a.simulated for a in attempts))
        cols = st.columns(5)
        cols[0].metric("Real attempts", len(real))
        cols[1].metric("Submitted", sum(a.status in SENT_STATUSES for a in apps))
        cols[2].metric("Confirmed", sum(a.status == "CONFIRMED" for a in apps))
        cols[3].metric("Interviews", sum(any(e.to_status == "INTERVIEW" for e in a.status_events) for a in apps))
        cols[4].metric("Failed or blocked", sum(a.result in {"FAILED", "BLOCKED", "NOT_AUTHORIZED"} for a in attempts))
        st.subheader("Source health")
        src_rows = [{"source": x.name, "operational": x.operational, "last success": x.last_success_at,
                     "last error": x.last_error, "needs your action": x.requires_user_action,
                     "restrictions": x.access_restrictions} for x in s.scalars(select(JobSource))]
        st.dataframe(pd.DataFrame(src_rows), use_container_width=True) if src_rows else st.caption("No sources run yet.")
        st.subheader("Interview rate")
        rates = stats(s)["interview_rate"]
        for dim, label in (("track", "By career track"), ("cv_version", "By CV version"), ("employer", "By employer")):
            rows = [{"key": k, **v} for k, v in rates[dim].items()]
            st.caption(label)
            st.dataframe(pd.DataFrame(rows), use_container_width=True) if rows else st.caption("No submissions yet.")

    # ------------------------------------------------------------------ jobs
    with tab_jobs:
        c1, c2, c3, c4 = st.columns(4)
        query = c1.text_input("Title or company")
        track = c2.selectbox("Career track", ["all", "data_bi", "grc_security", "ai_automation", "marketing", "other"])
        min_score = c3.slider("Minimum score", 0, 100, 0)
        arrangement = c4.selectbox("Work arrangement", ["all", "remote", "hybrid", "onsite", "unknown"])
        c5, c6 = st.columns(2)
        location = c5.text_input("Location contains")
        min_salary = c6.number_input("Published salary at least (ILS/month, 0 = any)", min_value=0, step=1000)
        rows = []
        for m in s.scalars(select(JobMatch).where(JobMatch.is_latest.is_(True))):
            j = m.job
            if query and query.lower() not in f"{j.title} {j.employer}".lower():
                continue
            if track != "all" and m.track != track:
                continue
            if m.score < min_score or (arrangement != "all" and j.work_arrangement != arrangement):
                continue
            if location and location.lower() not in f"{j.location or ''} {j.location_normalized or ''}".lower():
                continue
            if min_salary and not (j.salary_max and j.salary_max >= min_salary):
                continue
            rows.append({"job_id": j.id, "score": m.score, "track": m.track, "recommendation": m.recommendation,
                         "eligible": m.eligible, "title": j.title, "employer": j.employer, "location": j.location,
                         "arrangement": j.work_arrangement, "salary": f"{j.salary_min}-{j.salary_max} {j.salary_currency}"
                         if j.salary_max else "not published", "status": j.expiration_status})
        df = pd.DataFrame(rows).sort_values("score", ascending=False) if rows else pd.DataFrame()
        st.dataframe(df, use_container_width=True)
        if rows:
            pick = st.selectbox("Job details", [r["job_id"] for r in df.to_dict("records")],
                                format_func=lambda jid: next(f"{r['title']} · {r['employer']} · {r['track']}"
                                                             for r in rows if r["job_id"] == jid))
            job = s.get(JobPosting, pick)
            st.markdown(f"**Official application URL:** {job.official_application_url or 'unknown'}")
            st.markdown("**Seen on:** " + ", ".join(f"{lst.source.name} ({lst.source_url})" for lst in job.listings))
            if job.injection_flags:
                st.warning("This posting contains instruction-like text. It is treated as untrusted and ignored.")
            for m in [m for m in job.matches if m.is_latest]:
                with st.expander(f"{m.track}: {m.score}/100 · {m.recommendation} · confidence {m.confidence}"):
                    st.write(m.explanation)
                    st.write("Reason:", m.recommendation_reason)
                    st.write("Hard filters:", m.hard_filter_results)
                    st.write("Matching skills:", m.matching_skills)
                    st.write("Missing requirements:", m.missing_requirements)
                    st.write("Relevant verified experience:", m.relevant_experience)
                    st.write("Uncertainties:", m.uncertainties)
            with st.expander("Original job description (untrusted text)"):
                st.text(job.description_original)

    # ------------------------------------------------------------------ materials
    with tab_materials:
        variants = list(s.scalars(select(ResumeVariant).order_by(ResumeVariant.created_at.desc())))
        if not variants:
            st.caption("No CV variants yet.")
        else:
            vid = st.selectbox("CV version", [v.version_id for v in variants])
            v = next(x for x in variants if x.version_id == vid)
            st.markdown(f"**File:** `{v.docx_filename}` · **SHA-256:** `{v.docx_sha256}` · "
                        f"**locked:** {v.locked} · **valid:** {v.valid}")
            st.markdown(f"**Master CV SHA-256:** `{v.master.sha256}`")
            st.write("Validation:", v.validation)
            try:
                from careerpilot.profile.cv_parser import parse_docx

                st.text_area("Preview", parse_docx(Path(v.docx_path)).full_text, height=300)
            except Exception as exc:  # file missing after deletion
                st.error(f"Cannot open the file: {exc}")
            st.subheader("Change log")
            st.dataframe(pd.DataFrame([{"section": c.section, "type": c.change_type, "material": c.material,
                                        "reason": c.reason, "original": c.original_text, "revised": c.revised_text,
                                        "source": c.fact_source} for c in v.changes]), use_container_width=True)
            app = s.scalars(select(Application).where(Application.resume_variant_id == v.id)).first()
            if app:
                st.subheader("Cover letter")
                st.text(app.cover_letter_text or "")
                st.subheader("Application answers")
                st.dataframe(pd.DataFrame([{"question": a.question, "answer": a.answer, "source": a.source,
                                            "confirmed by you": a.confirmed_by_user, "sensitive": a.sensitive,
                                            "needs your input": a.needs_user_input} for a in app.answers]),
                             use_container_width=True)

    # ------------------------------------------------------------------ tracking
    with tab_apps:
        status_filter = st.multiselect("Status", ALL_STATUSES)
        text_filter = st.text_input("Search employer or title", key="app_search")
        records = [application_record(a) for a in s.scalars(select(Application).order_by(Application.created_at.desc()))]
        records = [r for r in records if (not status_filter or r["status"] in status_filter)
                   and (not text_filter or text_filter.lower() in f"{r['employer']} {r['job_title']}".lower())]
        table = [{k: r[k] for k in ("application_id", "employer", "job_title", "career_track", "status_display",
                                     "submitted_at", "confirmed", "cv_version_id", "next_action")}
                 | {"simulated only": all(a["simulated"] for a in r["attempts"]) if r["attempts"] else None}
                 for r in records]
        st.dataframe(pd.DataFrame(table), use_container_width=True)
        if records:
            aid = st.selectbox("Application details", [r["application_id"] for r in records])
            r = next(x for x in records if x["application_id"] == aid)
            st.markdown(f"**{r['job_title']}** at **{r['employer']}** · {r['status_display']}")
            st.markdown(f"CV sent: `{r['cv_filename']}` (version `{r['cv_version_id']}`, SHA-256 `{r['cv_sha256']}`)")
            st.markdown(f"Found on: {', '.join(r['source_names'])} · ATS: {r['ats_platform']}")
            st.subheader("Attempts")
            for a in r["attempts"]:
                label = "SIMULATED" if a["simulated"] else "REAL"
                st.markdown(f"- **{label}** {a['local_time']} ({a['timezone']}) · {a['adapter']} v{a['adapter_version']}"
                            f" · {a['result']} · {a['certainty']}" + (f" · ref {a['confirmation_ref']}" if a["confirmation_ref"] else "")
                            + (f" · {a['error']}" if a["error"] else ""))
            st.subheader("Timeline")
            st.dataframe(pd.DataFrame(r["timeline"]), use_container_width=True)
            st.subheader("Follow-ups")
            st.write(r["follow_ups"] or "None scheduled")
        c1, c2 = st.columns(2)
        if c1.button("Export CSV and JSON"):
            paths = export_history(s, settings.data_dir / "exports")
            st.success("Saved: " + ", ".join(str(p) for p in paths))
        c2.download_button("Download JSON", json.dumps(records, ensure_ascii=False, indent=2),
                           file_name="applications.json", mime="application/json")

    # ------------------------------------------------------------------ profile
    with tab_profile:
        st.caption("Only verified facts can appear in CVs, letters or answers. Confirm others with "
                   "`careerpilot confirm-fact <id>`.")
        facts = list(s.scalars(select(VerifiedFact).order_by(VerifiedFact.status, VerifiedFact.category)))
        st.dataframe(pd.DataFrame([{"id": f.id, "status": f.status, "category": f.category, "value": f.value,
                                    "source": f.source} for f in facts]), use_container_width=True)

    # ------------------------------------------------------------------ settings
    with tab_settings:
        st.markdown(f"- DRY_RUN: **{settings.dry_run}**\n- Kill switch: **{settings.submissions_kill_switch}**\n"
                    f"- Max applications per day: **{settings.max_daily_applications}**\n"
                    f"- Approved submission domains: **{settings.approved_submission_domains or 'none'}**\n"
                    f"- Timezone: **{settings.timezone}**")
        st.caption("Change these in the .env file after reviewing test results. Real submission needs "
                   "DRY_RUN=false AND kill switch off AND an approved domain.")
        if st.button("Turn the kill switch ON now"):
            env = PROJECT_ROOT / ".env"
            lines = [ln for ln in (env.read_text().splitlines() if env.exists() else [])
                     if not ln.startswith("SUBMISSIONS_KILL_SWITCH=")]
            env.write_text("\n".join(lines + ["SUBMISSIONS_KILL_SWITCH=true"]) + "\n")
            st.success("Kill switch is ON. Restart the scheduler and dashboard to apply everywhere.")
        st.subheader("Preferences and career tracks")
        prefs_path = settings.config_dir / "preferences.yaml"
        edited = st.text_area("config/preferences.yaml", prefs_path.read_text(encoding="utf-8"), height=400)
        if st.button("Save preferences"):
            import yaml

            try:
                yaml.safe_load(edited)
                prefs_path.write_text(edited, encoding="utf-8")
                st.success("Saved. Re-run matching to apply.")
            except yaml.YAMLError as exc:
                st.error(f"Invalid YAML: {exc}")
        st.subheader("Free sources")
        st.dataframe(pd.DataFrame([i.__dict__ for i in load_integrations(settings.config_dir)]),
                     use_container_width=True)
