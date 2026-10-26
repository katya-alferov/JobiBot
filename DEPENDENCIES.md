# Dependency inventory

Every dependency, what it costs, its limits, and what access it needs. All are free.

| Dependency | Purpose | License | Cost | Limits / notes | Access needed |
| --- | --- | --- | --- | --- | --- |
| Python 3.12+ | Runtime | PSF | Free | None | Local install |
| SQLAlchemy 2 | ORM, PostgreSQL-compatible models | MIT | Free | None | None |
| Alembic | Database migrations | MIT | Free | None | None |
| SQLite | Local database (bundled with Python) | Public domain | Free | Single user, local file | None |
| Pydantic 2 | Settings validation | MIT | Free | None | None |
| python-dotenv | Reads `.env` | BSD-3 | Free | None | None |
| PyYAML | Config files | MIT | Free | None | None |
| httpx | HTTP for public job boards | BSD-3 | Free | Respect each site's rate limits | Internet for discovery only |
| python-docx | Tailored CV DOCX generation and parsing | MIT | Free | DOCX only | None |
| LibreOffice (optional) | DOCX to PDF conversion | MPL-2.0 | Free | Must be installed locally; without it CVs are DOCX only | Local install |
| Streamlit | Local dashboard | Apache-2.0 | Free | Runs on `localhost`; no Streamlit Cloud needed | None |
| pandas | Dashboard tables | BSD-3 | Free | None | None |
| pytest | Tests | MIT | Free | None | None |
| Ollama (optional, not required) | Local LLM for wording suggestions only | MIT | Free | Needs enough RAM; never decides eligibility or submission | Local install |
| Playwright (next milestone, not used yet) | Read-only form inspection on approved domains | Apache-2.0 | Free | Never bypasses CAPTCHA, logins or anti-bot measures | Local install |
| FastAPI | Not used in the MVP; the CLI and dashboard cover the needs | MIT | Free | Add only if a local API becomes useful | None |

## External data

| Source | Cost | Limits |
| --- | --- | --- |
| Greenhouse public job boards (`boards-api.greenhouse.io`) | Free, no key | Per-employer board token; public data only; rate limits apply |
| Lever public postings (`api.lever.co/v0/postings`) | Free, no key | Per-employer slug; public data only |
| Job-alert emails saved as `.eml` | Free | Only alerts the candidate subscribed to; no mailbox login |
| Manual import (JSON/CSV) | Free | The candidate supplies the data |

No paid service, API key or account is required to run any feature.
