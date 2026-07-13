---
name: job-radar-app
description: Expert knowledge of "Job Radar" — the personal job-posting tracker at github.com/calebcpratt-git/job-radar (Python + GitHub Actions + GitHub Pages). Use this whenever the user asks how this app works, why a job did or didn't appear on the dashboard, how to change search criteria or title rules, how the daily run/schedule/secrets/Pages deployment works, or wants to modify, debug, or extend job_check.py, config.yaml, the workflow, or the dashboard HTML. Trigger even when the user just says "my job board", "the job tracker", "job radar", "why isn't this posting showing up", mentions Adzuna or JSearch keys in the context of their job search tool, or references files like job_check.py, config.yaml, or docs/index.html — consult this skill instead of guessing from general Python or GitHub Actions knowledge. IMPORTANT: before answering, pull the live repo and treat its current code as authoritative; this skill may lag behind it.
---

# Job Radar — App Expert

Job Radar is Caleb's personal job-search automation: a single Python script that runs every morning in GitHub Actions, pulls postings from two broad job-search APIs (JSearch and Adzuna), filters them against criteria in `config.yaml`, keeps everything posted in the last 30 days, and commits a static HTML dashboard served by GitHub Pages.

**Purpose and goals.** It exists so the user (currently searching for Product Operations roles in NYC, 1–3 yrs experience) sees a fresh, filtered brief of relevant postings without visiting job boards manually. Design goals: zero paid services (free-tier Adzuna and JSearch keys), zero infrastructure (GitHub Actions + Pages do everything), one user-editable file (`config.yaml`), and resilience (one broken source never kills the run). Keep these in mind when proposing changes — solutions requiring servers, databases, or paid APIs cut against the app's philosophy.

**Architecture history (important):** the app used to track named company ATS boards and flag postings that were "new since last run" via a committed `seen_jobs.json` state file. That entire model is gone — there is no state file and no new-flagging. The app now shows a **rolling 30-day window** of everything currently matching. The Greenhouse/Lever/Ashby fetchers (`fetch_greenhouse`, `fetch_lever`, `fetch_ashby`, `fetch_companies`, `ATS_FETCHERS`) still exist in `job_check.py` but are **dead code**: `main()` never calls them and `config.yaml` has no `companies:` section. Re-wiring them (add `companies:` back to config, call `fetch_companies()` in `main()` *before* the broad sources so ATS versions win dedupe) is the sanctioned path for tracking specific companies.

## Repository map

| File | Role |
|---|---|
| `job_check.py` | The entire app: fetch → filter → dedupe → render (~575 lines) |
| `config.yaml` | The only file the user is meant to edit: label, location, `max_days_old`, Adzuna settings, JSearch settings, `digital_signals`, `title_rules` |
| `.github/workflows/job-check.yml` | The **active** daily workflow (cron `0 11 * * *` UTC ≈ 6–7am ET, plus manual `workflow_dispatch`) |
| `job-check.yml` (repo root) | A **stray duplicate** of the workflow. GitHub only runs workflows under `.github/workflows/`, so this root copy is inert. Edits go in the `.github/workflows/` copy |
| `docs/index.html` | The generated live dashboard (overwritten each run, served by Pages from `/docs`) |
| `index.html` (repo root) | Static design **preview with hardcoded sample jobs** — not generated, not served. The real template is the `TEMPLATE` string in `job_check.py` |
| `requirements.txt` | Just `requests` and `PyYAML` |
| `package.json` | Vestigial npm boilerplate; no JavaScript build. Ignore it |

There is no `seen_jobs.json` anymore; if the user mentions it, they're remembering the old architecture.

## Data flow (main() in job_check.py)

1. **Load config** → `criteria` dict: `location`, `remote_ok`, `max_days_old`, `digital_signals`, `title_rules`.
2. **Fetch Adzuna** (`fetch_adzuna`) if `adzuna.enabled`: paginated (`pages` × `results_per_page`), stops early on a short page. Passes `where` (location) and `max_days_old` server-side. Reads `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` from env; if missing it **skips with a stderr warning rather than failing**. Ids look like `az:<id>`. Captures both `location` (display name, often just a neighborhood) and `location_area` (full area hierarchy) for location filtering.
3. **Fetch JSearch** (`fetch_jsearch`) if `jsearch.enabled`: Google for Jobs aggregator (surfaces LinkedIn, Indeed, Built In, etc.) via `api.openwebninja.com/jsearch/search-v2` with `x-api-key: JSEARCH_API_KEY`. One API call **per query** regardless of `pages` — `num_pages` is a page *count* bundled into a single response, not a page number. Results are nested under `data.jobs`. Maps `max_days_old` to the coarse `date_posted` buckets (today/3days/week/month). Ids look like `js:<job_id>`; `source` is `JSearch (<publisher>)`. Missing key → stderr warning + skip.
4. **Filter** everything through `matches()` (semantics below).
5. **Dedupe, two passes**: first by `id` (fallback `url`), first occurrence wins; then by normalized `(title, company)` pair to kill the same posting arriving from multiple aggregators. Fetch order = priority order.
6. **Sort** by `posted` descending (jobs with no date sort last).
7. **Render** into `TEMPLATE` via `.format()`, writing `docs/index.html`. Header shows "{n} matches · last {max_days_old} days".

## Filtering semantics (matches()) — the usual source of "why isn't X showing?"

- **title_rules** (config): a list of rules; each rule is a list of words. A title passes if **any one rule** matches, and a rule matches when **all of its words** appear in the title as whole words (title is tokenized with `re.findall(r"[a-z]+")`, so punctuation/hyphens split words but "ProductOps" stays one token and fails). Current rules: [product, operations], [product, ops], [platform, operations], [business, operations], [strategy, operations], [revenue, operations], [product, analyst]. If `title_rules` is empty, falls back to hardcoded product + (operations|ops). There is **no salary filter** anymore — salary is display-only.
- **digital_signals** (config): if the job has a non-empty description, it must contain at least one signal substring (software, saas, platform, api, roadmap, agile, ux, etc.) — filters physical-product/CPG/logistics ops out. **Jobs with no description are kept.**
- **location**: literal substring check — config `location` ("New York") must appear in `location + " " + location_area` (lowercased). A job passes anyway if `remote_ok: true` and "remote" appears in its location text. **Known gap:** boroughs with state abbreviations ("Brooklyn, NY") fail, because neither "new york" nor a spelled-out state appears. Adzuna's `location_area` mitigates this for Adzuna jobs; JSearch jobs only have `city, state` (+ "(Remote)").
- **max_days_old** (30): checked **inside matches() for every source** (this changed — it used to be Adzuna-server-side only). Parsed with `datetime.fromisoformat` after `Z`→`+00:00`; **unparseable or missing dates are kept**.

Adzuna also applies its own server-side keyword layer (`what_or` in config) before the local filters — an Adzuna posting must survive both.

## Known operational realities

- **The dashboard is ~100% JSearch-dependent.** Adzuna typically contributes 0–2 jobs/day; JSearch supplies essentially everything. A single JSearch query capped at ~10 pages (~100 results by Google relevance) means real matches can simply never enter the pipeline — when a "direct match" is missing, check source coverage *first*, filters second.
- **Sources fail soft.** A dead API key, rate limit, or API change produces a stderr warning, an empty result set, and a **successfully committed 0-match dashboard with a green Actions run**. Diagnose via the "Run job check" step log: look for `! JSearch query ... failed:` / `! Adzuna page N failed:` vs `· JSearch broad search: 0 postings`. (A patch adding a `raw == []` guard that exits 1 and injects a stale-data banner into the existing dashboard, plus `if: always()` on the commit step, may have been applied — check `main()` for `mark_dashboard_stale`.)
- **JSearch free tier** is ~200 calls/month; call cost scales with number of queries (1/day/query), so keep the `queries:` list short.

## Deployment & operations

- **Secrets** (GitHub repo Actions secrets → env vars): `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `JSEARCH_API_KEY`. Never hardcode. Local runs can use a `.env` file (loaded by `_load_dotenv`, never overrides real env vars).
- **Workflow**: checkout → Python 3.12 → `pip install -r requirements.txt` → `python job_check.py` → commit `docs/index.html` as `job-radar-bot` (`|| echo "No changes"` prevents failures on empty commits). Requires `permissions: contents: write`.
- **Pages**: deploy-from-branch, `main` / `/docs`. Dashboard URL: `https://calebcpratt-git.github.io/job-radar/`.
- **Local run**: set the three env vars (or `.env`), `python job_check.py`, open `docs/index.html`. No state file, so local runs are harmless — just don't commit the locally generated dashboard.

## Dashboard behavior

Self-contained generated HTML (Google Fonts only external dependency): header with "{n} matches · last 30 days", one `<li class="job">` per posting (title link, company, location, salary if known, source · posted-date meta line), and an empty state ("No matches today. The wire is quiet"). Salary formatting via `fmt_salary`, relative dates via `fmt_posted`. Colors/typography are CSS variables at the top of `TEMPLATE`. Because `TEMPLATE` is filled with `str.format`, **literal braces in any CSS/JS added there must be doubled (`{{ }}`)** — forgetting this raises `KeyError`/`IndexError` at render time. Job fields are escaped with `html.escape`. There is no "new" pill or new-only toggle anymore.

## Common tasks — how to do them right

- **Change search criteria**: edit `config.yaml` only. Broadening usually means touching both a local layer (`title_rules` / `digital_signals`) and the source queries (Adzuna `what_or`, JSearch `queries:`) — a filter can only keep what the sources return.
- **Add a title pattern**: append a rule to `title_rules` (all words in a rule are ANDed; rules are ORed). Whole-word matching — add singular/abbreviated variants as separate rules if needed.
- **Track a specific company**: re-wire the dormant ATS machinery — add a `companies:` section (name, `ats` ∈ greenhouse/lever/ashby, `token` = slug from the careers URL) and call `fetch_companies()` in `main()` before the broad sources. Verify tokens by opening the ATS API URL directly. Companies on unsupported ATSes or self-hosted careers pages (e.g. Webflow sites) need a custom fetcher returning the common job-dict shape with a unique id prefix, with failures caught.
- **Change schedule**: edit the cron in `.github/workflows/job-check.yml` (UTC!), not the root duplicate.
- **Widen/narrow the window**: `max_days_old` in config — applies to all sources in `matches()` and maps to JSearch's coarse `date_posted` buckets.
- **Restyle the dashboard**: edit `TEMPLATE` in `job_check.py` (double the braces), rerun, optionally sync the root `index.html` preview.
- **Debug a missing posting, in order**: (1) is it in the raw source results at all? — JSearch coverage is the top cause of misses; (2) title_rules whole-word match; (3) location substring (borough/"NY" gap); (4) digital_signals if it has a description; (5) 30-day window / unparseable date.

## Per-source API details (dormant ATS fetchers, for re-wiring)

- **Greenhouse**: `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`. `posted` is really `updated_at` — an edited old posting looks recent and can re-enter the 30-day window.
- **Lever**: `api.lever.co/v0/postings/{token}?mode=json`; `posted` from `createdAt` epoch-ms; location from `categories.location`. No salary data.
- **Ashby**: `api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true`; appends "(Remote)" when `isRemote`; extracts salary from nested `compensationTiers`.
- Greenhouse/Lever return no descriptions → such jobs bypass `digital_signals` (kept).
- All requests share `_get()`: 20s timeout, custom User-Agent, `raise_for_status`.
