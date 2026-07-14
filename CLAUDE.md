# CLAUDE.md — job-radar

## Documentation rule (mandatory)

This repo contains a SKILL.md at the root that serves as the living "product
expert" documentation for this app. It is consumed by other Claude sessions
as their source of truth for how the app currently works.

**Any commit that changes app behavior MUST update SKILL.md in the same
commit.** This includes changes to:

- job_check.py — fetching, filtering (title_rules, digital_signals, location,
  max_days_old), dedupe, sorting, rendering, error handling, or the TEMPLATE
- config.yaml — new/removed/renamed keys, changed semantics of existing keys
  (a simple value tweak like editing a title_rules entry does NOT require a
  doc update; adding a new kind of rule or key DOES)
- .github/workflows/job-check.yml — schedule, secrets, steps, commit behavior
- Anything that changes what appears on the dashboard or why

When updating SKILL.md, edit the specific sections affected — do not rewrite
the whole file. Keep its structure intact: Repository map, Data flow,
Filtering semantics, Known operational realities, Deployment & operations,
Dashboard behavior, Common tasks, Per-source API details.

Pure dashboard-data commits (docs/index.html updates by job-radar-bot) and
README-only changes are exempt.

## Working conventions

- config.yaml is the only file the user edits by hand; keep it that way —
  prefer making behavior configurable there over hardcoding in job_check.py.
- Sources must fail soft: a broken API or missing key produces a stderr
  warning, never a crash that kills the whole run.
- No paid services, servers, or databases — GitHub Actions + Pages only.
- The workflow file that runs is .github/workflows/job-check.yml; the
  job-check.yml at the repo root is an inert stray duplicate.
