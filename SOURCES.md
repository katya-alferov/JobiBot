# Job sources: access, limits and status

Discovery targets are not claims that every site offers a free API or allows automation. A
source is automated only when access is free and permitted. Everything else uses a compliant
manual path. Terms were assessed on 2026-10-25 and must be re-checked before enabling an adapter.

| Source | Status in MVP | Method | Why / limitation | Free alternative |
| --- | --- | --- | --- | --- |
| Greenhouse boards | Supported | Public read-only JSON | Free, unauthenticated, published for job boards | — |
| Lever boards | Supported | Public read-only JSON | Free, unauthenticated postings API | — |
| Employer career pages | Manual import | Paste URL/text into JSON or CSV | Each site differs; verify on the official page | Add the employer's ATS board if it is Greenhouse or Lever |
| Comeet, Ashby, SmartRecruiters, Workday | Planned | Public boards where published | Not yet implemented; terms per employer | Manual import |
| LinkedIn Jobs | Not automated | Alert emails or manual import | Terms prohibit scraping and automation | LinkedIn job-alert emails saved as `.eml` |
| Indeed | Not automated | Alert emails or manual import | Public API discontinued; terms restrict scraping | Indeed alert emails |
| AllJobs, Drushim, JobMaster | Not automated | Alert emails or manual import | No known free public API; automation terms not verified | Their free email alerts |
| Jobnet, Civil Service Commission | Not automated yet | Manual import | Free RSS or API not yet confirmed | Manual import |

Every source run records whether it is operational, its last successful run, its last error, its
access restrictions and whether it needs your action (see the dashboard's Overview tab).

## Applications

| Method | Status |
| --- | --- |
| Mock ATS adapter (`mock-ats.careerpilot.test`) | Testing only; never contacts anyone |
| Assisted manual | Default for every real employer: documents ready, official link, you submit |
| Automated real submission | Not available in the MVP. Requires a reviewed, permitted adapter, an approved domain, `DRY_RUN=false` and the kill switch off |

## Next milestone

1. Re-check each Israeli board's terms and add employer Greenhouse/Lever boards you care about to `config/sources.yaml`.
2. Comeet and SmartRecruiters public board adapters.
3. A read-only Playwright form inspector for approved domains that maps fields and reports CAPTCHAs or account requirements, without submitting.
4. A Hebrew master CV from you, so Hebrew postings can get a Hebrew CV.
5. Optional local LLM wording suggestions, always passed through the fact validator.
