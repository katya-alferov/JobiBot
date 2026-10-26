# Cost policy: zero monetary cost, always

CareerPilot Israel must run at **zero additional monetary cost** at every stage: development,
operation, data, AI, hosting and notifications. This is a hard constraint, not a preference.

## Rules

The system never:

- buys or subscribes to job boards, premium search features, recruiter databases or services;
- calls paid APIs, paid LLMs, pay-as-you-go AI or paid embeddings;
- deploys paid cloud servers, databases, hosting, browser-automation or notification services;
- buys domains, licenses, datasets, certificates or integrations;
- starts a free trial that can convert into a paid plan, or treats "free signup" as "free forever";
- asks for, stores or submits payment details;
- registers, upgrades or pays for any service on the candidate's behalf.

It uses only free and open-source software, the candidate's own computer, local SQLite storage,
local document generation, public and free job data where automated access is permitted, free
job alerts the candidate subscribes to, rule-based matching, and optionally a local model that
runs on existing hardware (Ollama on `localhost`).

## How it is enforced in code

| Control | Where |
| --- | --- |
| `ZERO_COST_MODE=true` is required; the app refuses to start otherwise | `careerpilot/cost_guard.py` |
| Every integration is listed with its cost; an enabled item that is not `free` blocks startup | `config/integrations.yaml`, `validate_startup()` |
| Sources whose terms forbid automation cannot be enabled | `automation_permitted: false` in the inventory |
| Only `LLM_PROVIDER=none` or a local `ollama` on localhost is accepted | `cost_guard.py`, `matching/model.py` |
| Tests prove each of the above | `tests/test_cost_policy.py` |

## If something useful is not free

Mark it unavailable, document a free alternative in `SOURCES.md`, and keep the system working
without it. Nothing in the MVP depends on a paid service.

## Building this project

The project was built with Claude Code in the candidate's existing development environment.
Running CareerPilot needs no Claude, Anthropic or other paid subscription: all decisions are
deterministic Python code and templates.
