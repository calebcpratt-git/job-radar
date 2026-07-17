#!/usr/bin/env python3
"""
Job Radar — pulls job postings each morning, filters them to your criteria,
and writes an HTML dashboard.

Sources:
  - Named companies via their public ATS boards, driven by ats_map.csv
    (Ashby / Workable / Greenhouse / Lever / Rippling / Gem / Work at a
    Startup) — see ats_fetch.py
  - A broad search across job boards via the Adzuna API
  - A broad search via JSearch (Google for Jobs: LinkedIn, Indeed, Glassdoor, etc.)

Nothing here needs a paid service. Credentials are free-tier API keys, read
from the environment (ADZUNA_APP_ID / ADZUNA_APP_KEY, JSEARCH_API_KEY).
"""

import html
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from ats_fetch import get_all_ats_jobs

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
OUT_PATH = ROOT / "docs" / "index.html"
ENV_PATH = ROOT / ".env"


def _load_dotenv():
    """Local-dev convenience: fill os.environ from .env without overriding
    real env vars (e.g. those set by the GitHub Actions workflow)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

HTTP_TIMEOUT = 20
USER_AGENT = "job-radar/1.0 (personal job search)"


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def title_rule_words(title_rules):
    """Union of every word across all title_rules, in first-seen order.

    title_rules is the single source of truth for "what titles count" — the
    same word list drives the local title_rules filter (matches()) AND the
    Adzuna/JSearch source queries below, so a rule added there automatically
    widens what those APIs are asked for. Without this, a source query stuck
    on a hardcoded phrase like "product operations" can never return a
    posting titled e.g. "Forward Deployed Strategist" for title_rules to even
    have a chance to keep."""
    seen = set()
    words = []
    for rule in title_rules or []:
        for w in rule:
            w = str(w).lower()
            if w not in seen:
                seen.add(w)
                words.append(w)
    return words


def _get(url, params=None, headers=None):
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT, headers=merged_headers)
    r.raise_for_status()
    return r.json()


def fetch_adzuna(cfg, criteria):
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        print("  ! Adzuna keys not set (ADZUNA_APP_ID / ADZUNA_APP_KEY) — skipping broad search",
              file=sys.stderr)
        return []

    country = cfg.get("country", "us")
    pages = int(cfg.get("pages", 1))
    per_page = int(cfg.get("results_per_page", 50))
    # Derived from title_rules (see title_rule_words) rather than a hardcoded
    # phrase, so this stays in sync with the same criteria used to filter
    # named-company ATS postings — otherwise Adzuna's own server-side search
    # would exclude titles (e.g. "Chief of Staff") before title_rules ever
    # gets a chance to keep them.
    what_or = " ".join(title_rule_words(criteria.get("title_rules")))
    out = []
    for page in range(1, pages + 1):
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": per_page,
            "what_or": what_or,
            "sort_by": "date",
            "content-type": "application/json",
        }
        if criteria.get("location"):
            params["where"] = criteria["location"]
        if criteria.get("max_days_old"):
            params["max_days_old"] = criteria["max_days_old"]
        try:
            data = _get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}", params=params)
        except Exception as e:
            print(f"  ! Adzuna page {page} failed: {e}", file=sys.stderr)
            break
        results = data.get("results", [])
        for j in results:
            loc = j.get("location") or {}
            out.append({
                "id": f"az:{j.get('id')}",
                "title": j.get("title", ""),
                "company": (j.get("company") or {}).get("display_name", ""),
                "location": loc.get("display_name", ""),
                # Adzuna's display_name is often just a neighborhood (e.g.
                # "Grand Central, Manhattan"); "area" carries the full
                # hierarchy up to city/state, which is what location
                # filtering should match against.
                "location_area": " ".join(loc.get("area") or []),
                "url": j.get("redirect_url", ""),
                "salary_min": j.get("salary_min"),
                "salary_max": j.get("salary_max"),
                "posted": j.get("created"),
                "description": j.get("description") or "",
                "source": "adzuna",
            })
        if len(results) < per_page:
            break  # no more pages
    print(f"  · Adzuna broad search: {len(out)} postings")
    return out


JSEARCH_URL = "https://api.openwebninja.com/jsearch/search-v2"


def fetch_jsearch(cfg, criteria):
    """Broad search via JSearch (Google for Jobs: LinkedIn, Indeed, etc.)."""
    api_key = os.environ.get("JSEARCH_API_KEY")
    if not api_key:
        print("  ! JSearch key not set (JSEARCH_API_KEY) — skipping broad search",
              file=sys.stderr)
        return []

    days = criteria.get("max_days_old")
    if days is None:
        date_posted = "all"
    elif days <= 1:
        date_posted = "today"
    elif days <= 3:
        date_posted = "3days"
    elif days <= 7:
        date_posted = "week"
    else:
        date_posted = "month"

    # JSearch's `query` is natural-language-only (confirmed against Google
    # for Jobs behavior) — it does not parse OR/parentheses as boolean logic,
    # so a single query combining every title_rules term actually returns
    # *fewer*, less relevant results than a plain short phrase. Real coverage
    # comes from multiple separate natural-language queries in config.yaml
    # instead (one API call each, billed per query regardless of `pages`).
    out = []
    for query in cfg.get("queries", []):
        # search-v2 takes a page *count* (num_pages) and returns that many
        # pages of results in a single call — it does not take a page
        # *number*, so one call per query covers cfg["pages"] pages.
        params = {
            "query": query,
            "num_pages": cfg.get("pages", 1),
            "country": cfg.get("country", "us"),
            "date_posted": date_posted,
        }
        try:
            data = _get(JSEARCH_URL, params=params, headers={"x-api-key": api_key})
        except Exception as e:
            print(f"  ! JSearch query '{query}' failed: {e}", file=sys.stderr)
            continue
        # search-v2 nests results under data.jobs (plus a data.cursor for
        # paging beyond num_pages), unlike the flat list some JSearch docs
        # describe.
        results = (data.get("data") or {}).get("jobs") or []
        for j in results:
            city = j.get("job_city") or ""
            state = j.get("job_state") or ""
            location = ", ".join(p for p in (city, state) if p)
            if j.get("job_is_remote"):
                location = (location + " (Remote)").strip()
            publisher = j.get("job_publisher") or "JSearch"
            out.append({
                "id": f"js:{j.get('job_id')}",
                "title": j.get("job_title") or "",
                "company": j.get("employer_name") or "",
                "location": location,
                "url": j.get("job_apply_link") or j.get("job_google_link") or "",
                "salary_min": j.get("job_min_salary"),
                "salary_max": j.get("job_max_salary"),
                "posted": j.get("job_posted_at_datetime_utc"),
                "description": j.get("job_description") or "",
                "source": f"JSearch ({publisher})",
            })
    print(f"  · JSearch broad search: {len(out)} postings")
    return out


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
def looks_remote(text):
    return "remote" in (text or "").lower()


def _words(text):
    return set(re.findall(r"[a-z]+", (text or "").lower()))


# Generic words that show up in a "Remote" location string without tying the
# job to any specific place ("Remote (United States)", "Remote - US",
# "US Remote") — used to tell a location-agnostic remote posting apart from
# one anchored to a specific other city/country ("Remote - India").
_REMOTE_FILLER_WORDS = {
    "remote", "us", "usa", "u", "s", "a", "united", "states", "the", "in", "only",
}


def _mentions_city(text, aliases):
    """Whole-word/phrase match against a list of alias strings (e.g. NYC
    borough names) — not a plain substring check, so a short alias like
    "queens" doesn't false-match inside an unrelated word like
    "Queensland"."""
    if not aliases or not text:
        return False
    pattern = r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b"
    return re.search(pattern, text) is not None


def location_ok(job, criteria):
    loc_filter = (criteria.get("location") or "").lower().strip()
    if not loc_filter:
        return True
    job_loc = (job.get("location") or "").lower()
    haystack = job_loc + " " + (job.get("location_area") or "").lower()
    aliases = [str(a).lower() for a in (criteria.get("location_aliases") or [loc_filter])]
    is_target_city = _mentions_city(haystack, aliases)
    is_remote = looks_remote(job_loc)

    if job.get("group") == "target":
        if is_target_city:
            # A posting naming New York (or a borough/alias) is kept
            # regardless of a remote flag — "New York (Remote)" means the
            # NY office is an option, not that NY is irrelevant.
            return True
        if is_remote:
            # Fully remote with no other city/country named (e.g. "Remote",
            # "Remote (United States)") might still be workable from the NY
            # office, so keep it. Remote tied to a different specific place
            # ("Remote - India", "Madrid (Remote)") is excluded.
            return not (_words(job_loc) - _REMOTE_FILLER_WORDS)
        return False
    else:
        remote_ok = criteria.get("remote_ok", True) and is_remote
        return is_target_city or remote_ok


def matches(job, criteria):
    title = (job.get("title") or "").lower()

    words = set(re.findall(r"[a-z]+", title))

    rules = criteria.get("title_rules") or []
    if rules:
        title_ok = False
        for rule in rules:
            rule_words = []
            for w in rule:
                rule_words.extend(str(w).lower().split())
            if rule_words and all(w in words for w in rule_words):
                title_ok = True
                break
        if not title_ok:
            return False
    elif not ("product" in words and ("operations" in words or "ops" in words)):
        return False

    signals = criteria.get("digital_signals")
    if signals:
        desc = (job.get("description") or "").lower()
        if desc and not any(s.lower() in desc for s in signals):
            return False

    if not location_ok(job, criteria):
        return False

    max_days = criteria.get("max_days_old")
    posted = job.get("posted")
    if max_days is not None and posted:
        try:
            dt = datetime.fromisoformat(posted.replace("Z", "+00:00"))
            days_old = (datetime.now(timezone.utc) - dt).days
            if days_old > max_days:
                return False
        except Exception:
            pass  # unparseable date: keep, same as unknown salary

    return True


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def fmt_salary(job):
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if not lo and not hi:
        return ""
    if lo and hi and lo != hi:
        return f"${int(lo):,}–${int(hi):,}"
    v = lo or hi
    return f"${int(v):,}"


def days_old(iso):
    """Whole days between an ISO posted timestamp and now, or None if the
    timestamp is missing/unparseable. Used by fmt_posted() for the
    "N days ago" display text."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def fmt_posted(iso):
    days = days_old(iso)
    if days is None:
        return ""
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def posted_date_str(iso):
    """UTC calendar date (YYYY-MM-DD) of a posted ISO timestamp, or None if
    missing/unparseable. This is what the "Posted since" date-picker filter
    compares against — a plain calendar date, not a relative day count, so
    it lines up with what an <input type="date"> picker returns."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).date().isoformat()
    except Exception:
        return None


def render_rows(jobs, empty_message):
    rows = []
    for j in jobs:
        salary = fmt_salary(j)
        posted_iso = j.get("posted")
        posted = fmt_posted(posted_iso)
        posted_date = posted_date_str(posted_iso)
        company = j.get("company") or ""
        meta = " · ".join(x for x in [
            html.escape(j.get("source", "")),
            posted,
        ] if x)
        # data-company: normalized (trimmed/lowercased) matching key for the
        # Companies filter. data-posted-date: UTC calendar date, or "" when
        # unknown — the date filter treats "" as "keep", mirroring
        # matches()'s server-side handling of unparseable/missing dates.
        rows.append(f"""
        <li class="job" data-company="{html.escape(company.strip().lower())}" data-posted-date="{posted_date or ''}">
          <a class="title" href="{html.escape(j.get('url',''))}" target="_blank" rel="noopener">
            {html.escape(j.get('title','') or 'Untitled role')}
          </a>
          <div class="sub">
            <span class="company">{html.escape(company)}</span>
            <span class="loc">{html.escape(j.get('location','') or '')}</span>
            {f'<span class="salary">{html.escape(salary)}</span>' if salary else ''}
          </div>
          <div class="meta">{meta}</div>
        </li>""")
    if rows:
        return "\n".join(rows)
    return f'<li class="empty">{empty_message}</li>'


def render(jobs, label, max_days_old):
    now = datetime.now(timezone.utc).astimezone()

    target_jobs = [j for j in jobs if j.get("group") == "target"]
    other_jobs = [j for j in jobs if j.get("group") != "target"]

    target_body = render_rows(
        target_jobs, "No target-company matches today. The wire is quiet — check back tomorrow.")
    other_body = render_rows(
        other_jobs, "No other matches today. The wire is quiet — check back tomorrow.")

    return TEMPLATE.format(
        label=html.escape(label),
        date=now.strftime("%A, %B %-d"),
        updated=now.strftime("%-I:%M %p %Z"),
        today_utc=datetime.now(timezone.utc).date().isoformat(),
        total=len(target_jobs),
        target_count=len(target_jobs),
        other_count=len(other_jobs),
        window_days=max_days_old,
        target_rows=target_body,
        other_rows=other_body,
    )


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{label}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#FAFBFC; --surface:#FFFFFF; --ink:#14181F; --muted:#66727E;
    --line:#E6EAEE; --accent:#1F51D6; --signal:#C77B0A; --signal-bg:#FBF1DE;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:"Inter",system-ui,sans-serif; line-height:1.5;
    -webkit-font-smoothing:antialiased;
  }}
  .wrap {{ max-width:980px; margin:0 auto; padding:48px 24px 96px; }}
  header {{ border-bottom:1px solid var(--line); padding-bottom:24px; margin-bottom:8px; }}
  .eyebrow {{
    font-family:"IBM Plex Mono",monospace; font-size:12px; letter-spacing:0.12em;
    text-transform:uppercase; color:var(--muted); margin:0 0 10px;
  }}
  h1 {{
    font-family:"Space Grotesk",sans-serif; font-weight:700; font-size:34px;
    letter-spacing:-0.02em; margin:0 0 14px;
  }}
  .status {{
    font-family:"IBM Plex Mono",monospace; font-size:13px; color:var(--muted);
    display:flex; flex-wrap:wrap; gap:6px 16px; align-items:center;
  }}
  .status .hot {{ color:var(--signal); font-weight:500; }}
  .tabs {{ display:flex; gap:0; margin:24px 0 0; border-bottom:1px solid var(--line); }}
  .tab {{
    font:inherit; font-family:"Space Grotesk",sans-serif; font-weight:500; font-size:14.5px;
    background:none; border:none; border-bottom:2px solid transparent; color:var(--muted);
    padding:10px 4px 12px; margin-right:24px; cursor:pointer;
    display:flex; align-items:center; gap:8px;
  }}
  .tab:hover {{ color:var(--ink); }}
  .tab.active {{ color:var(--ink); border-bottom-color:var(--accent); }}
  .tab .count {{
    font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
    background:var(--bg); border:1px solid var(--line); border-radius:999px; padding:1px 7px;
  }}
  .tab.active .count {{ color:var(--accent); border-color:var(--accent); }}
  .controls {{ display:flex; gap:12px; align-items:center; margin:16px 0 6px; }}
  #q {{
    flex:1; padding:9px 12px; border:1px solid var(--line); border-radius:8px;
    font:inherit; font-size:14px; background:var(--surface); color:var(--ink);
  }}
  #q:focus {{ outline:2px solid var(--accent); outline-offset:1px; border-color:transparent; }}
  ul {{ list-style:none; margin:8px 0 0; padding:0; }}
  .job-list[hidden] {{ display:none; }}
  .job {{ padding:20px 0 18px; border-bottom:1px solid var(--line); }}
  .title {{
    font-family:"Space Grotesk",sans-serif; font-weight:500; font-size:19px;
    color:var(--ink); text-decoration:none; letter-spacing:-0.01em;
    display:inline-flex; align-items:center; gap:10px;
  }}
  .title:hover {{ color:var(--accent); }}
  .sub {{ margin:6px 0 4px; font-size:14.5px; color:#3A424C; display:flex; flex-wrap:wrap; gap:4px 14px; }}
  .sub .company {{ font-weight:600; }}
  .sub .salary {{ color:var(--accent); font-weight:500; }}
  .meta {{
    font-family:"IBM Plex Mono",monospace; font-size:11.5px; letter-spacing:0.04em;
    color:var(--muted); text-transform:uppercase;
  }}
  .empty {{ padding:56px 0; text-align:center; color:var(--muted); font-size:15px; }}
  footer {{
    margin-top:40px; font-family:"IBM Plex Mono",monospace; font-size:11px;
    color:var(--muted); letter-spacing:0.04em;
  }}
  @media (prefers-reduced-motion:no-preference) {{
    .job {{ animation:rise .4s ease both; }}
    @keyframes rise {{ from {{ opacity:0; transform:translateY(6px); }} }}
  }}
  .layout {{ display:flex; gap:32px; align-items:flex-start; margin-top:20px; }}
  .sidebar {{ flex:0 0 220px; width:220px; }}
  .main {{ flex:1; min-width:0; }}
  @media (max-width:760px) {{
    .layout {{ flex-direction:column; }}
    .sidebar {{ width:100%; flex:0 0 auto; }}
  }}
  .date-filter-group {{ display:flex; align-items:center; gap:8px; }}
  .date-filter-label {{
    font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--muted);
    white-space:nowrap;
  }}
  #date-filter {{
    padding:8px 10px; border:1px solid var(--line); border-radius:8px;
    font:inherit; font-family:"Inter",system-ui,sans-serif; font-size:14px;
    background:var(--surface); color:var(--ink); cursor:pointer;
  }}
  #date-filter:focus {{ outline:2px solid var(--accent); outline-offset:1px; border-color:transparent; }}
  .company-filter {{
    border:1px solid var(--line); border-radius:8px; background:var(--surface);
    padding:14px;
  }}
  .company-filter h2 {{
    font-family:"Space Grotesk",sans-serif; font-weight:500; font-size:13px;
    letter-spacing:0.04em; text-transform:uppercase; color:var(--muted);
    margin:0 0 10px;
  }}
  .company-filter-list {{
    max-height:310px; overflow-y:auto; border:1px solid var(--line);
    border-radius:6px; background:var(--bg); padding:6px 8px;
  }}
  .company-filter-row {{
    display:flex; align-items:center; gap:8px; padding:6px 4px;
    font-size:13.5px; line-height:1.3; color:var(--ink);
  }}
  .company-filter-row input {{ margin:0; accent-color:var(--accent); cursor:pointer; }}
  .company-filter-row label {{
    cursor:pointer; flex:1; min-width:0; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap;
  }}
  .company-filter-empty {{
    font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--muted);
    padding:4px 2px;
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <p class="eyebrow">Morning brief · {date}</p>
      <h1>{label}</h1>
      <div class="status">
        <span class="hot">{total} matches · last {window_days} days</span>
        <span>updated {updated}</span>
      </div>
    </header>

    <div class="layout">
      <aside class="sidebar">
        <div class="company-filter">
          <h2>Companies</h2>
          <div id="company-filter-list" class="company-filter-list">
            <p class="company-filter-empty">Loading…</p>
          </div>
        </div>
      </aside>

      <div class="main">
        <div class="tabs" role="tablist">
          <button class="tab active" data-tab="target" role="tab" aria-selected="true">
            Target Companies <span class="count">{target_count}</span>
          </button>
        </div>

        <div class="controls">
          <input id="q" type="text" placeholder="Filter by title, company, or location…" aria-label="Filter jobs">
          <div class="date-filter-group">
            <label class="date-filter-label" for="date-filter">Posted since</label>
            <input type="date" id="date-filter" max="{today_utc}" aria-label="Show postings since this date">
          </div>
        </div>

        <ul id="list-target" class="job-list">
          {target_rows}
        </ul>

        <footer>Job Radar · Target Companies from your named ATS boards, once each morning.</footer>
      </div>
    </div>
  </div>

<script>
  const tabs = Array.from(document.querySelectorAll('.tab'));
  const panels = {{
    target: document.getElementById('list-target'),
  }};

  const q = document.getElementById('q');
  const items = Array.from(document.querySelectorAll('.job-list .job'));

  // Shared filter state: every control (search box, and the ones added
  // below) writes into this object and then calls applyFilters(). Keeps
  // the independently-built filters from stepping on each other's DOM
  // queries or display-toggling logic.
  const filterState = {{
    search: '',       // lowercased search term, '' = no filter
    companies: null,  // Set of selected data-company keys, null/empty = no filter
    sinceDate: null,  // 'YYYY-MM-DD' string, null = no filter
  }};

  function computeVisible(el) {{
    if (filterState.search) {{
      const text = el.textContent.toLowerCase();
      if (!text.includes(filterState.search)) return false;
    }}
    if (filterState.companies && filterState.companies.size > 0 && !filterState.companies.has(el.dataset.company)) {{
      return false;
    }}
    if (filterState.sinceDate) {{
      const postedDate = el.dataset.postedDate;
      // Missing/unparseable posted date: keep, same as everywhere else in
      // the app that treats an unknown date as "don't filter it out".
      // ISO 'YYYY-MM-DD' strings compare correctly with plain < / >=.
      if (postedDate && postedDate < filterState.sinceDate) return false;
    }}
    return true;
  }}

  function applyFilters() {{
    items.forEach(el => {{ el.style.display = computeVisible(el) ? '' : 'none'; }});
  }}

  q.addEventListener('input', () => {{
    filterState.search = q.value.trim().toLowerCase();
    applyFilters();
  }});

  const dateFilter = document.getElementById('date-filter');
  dateFilter.addEventListener('change', () => {{
    filterState.sinceDate = dateFilter.value || null;
    applyFilters();
  }});
  const companyFilterListEl = document.getElementById('company-filter-list');

  // Rebuilds the sidebar checkbox list from only the jobs in the given tab's
  // panel, so it never offers a company that isn't on the open tab. Called
  // on load and on every tab switch; resets any active company selection
  // since the previous tab's checked companies may not exist on this one.
  function buildCompanyFilter(tabKey) {{
    const tabItems = Array.from(panels[tabKey].querySelectorAll('.job'));
    const seen = new Map(); // data-company key -> human-readable label
    tabItems.forEach(el => {{
      const key = el.dataset.company;
      if (!key || seen.has(key)) return;
      const companyEl = el.querySelector('.company');
      const label = companyEl ? companyEl.textContent.trim() : key;
      seen.set(key, label);
    }});
    const companies = Array.from(seen.entries()).sort((a, b) => a[1].localeCompare(b[1]));

    filterState.companies = new Set();
    companyFilterListEl.innerHTML = '';

    if (companies.length === 0) {{
      companyFilterListEl.innerHTML = '<p class="company-filter-empty">No companies</p>';
      applyFilters();
      return;
    }}

    companies.forEach(([key, label], i) => {{
      const row = document.createElement('div');
      row.className = 'company-filter-row';

      const id = 'company-filter-' + i;
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.id = id;
      checkbox.value = key;

      const labelNode = document.createElement('label');
      labelNode.setAttribute('for', id);
      labelNode.textContent = label;

      checkbox.addEventListener('change', () => {{
        if (checkbox.checked) {{
          filterState.companies.add(key);
        }} else {{
          filterState.companies.delete(key);
        }}
        applyFilters();
      }});

      row.appendChild(checkbox);
      row.appendChild(labelNode);
      companyFilterListEl.appendChild(row);
    }});

    applyFilters();
  }}

  tabs.forEach(btn => btn.addEventListener('click', () => {{
    tabs.forEach(b => {{ b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); }});
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    const tabKey = btn.dataset.tab;
    Object.entries(panels).forEach(([key, el]) => {{ el.hidden = key !== tabKey; }});
    buildCompanyFilter(tabKey);
  }}));

  buildCompanyFilter('target');
</script>
</body>
</html>"""


STALE_BANNER_RE = re.compile(r'\s*<div id="stale-banner".*?</div>', re.DOTALL)

def mark_dashboard_stale():
    """Inject a warning banner into the existing dashboard (if any) so the
    page itself says today's search failed, while keeping the last good
    results visible. Idempotent: replaces any previous banner. On the next
    successful run render() overwrites the whole file, removing the banner."""
    if not OUT_PATH.exists():
        return
    page = STALE_BANNER_RE.sub("", OUT_PATH.read_text())
    now = datetime.now(timezone.utc)
    today = f"{now.strftime('%B')} {now.day}, {now.year}"
    banner = (
        '\n<div id="stale-banner" style="margin:16px auto 0;max-width:760px;'
        'padding:12px 16px;border:1px solid #b45309;border-radius:8px;'
        'background:#451a03;color:#fbbf24;font-family:sans-serif;'
        'font-size:14px;line-height:1.5;">'
        f"&#9888;&#65039; <strong>Today&rsquo;s search ({today}) failed</strong> "
        "&mdash; no postings were returned from any source, so the jobs below "
        "are from the last successful run. Re-run the <em>Job Radar</em> "
        "workflow from the repo&rsquo;s Actions tab (Run workflow) to refresh."
        "</div>"
    )
    page = re.sub(r"(<body[^>]*>)", r"\1" + banner.replace("\\", "\\\\"), page, count=1)
    OUT_PATH.write_text(page)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    criteria = {
        "location": cfg.get("location", ""),
        "remote_ok": cfg.get("remote_ok", True),
        "max_days_old": cfg.get("max_days_old"),
        "digital_signals": cfg.get("digital_signals") or [],
        "title_rules": cfg.get("title_rules") or [],
        "location_aliases": cfg.get("location_aliases") or [],
    }

    raw = []

    # "group" marks which dashboard tab a job belongs to: postings from the
    # named-company ATS boards are "target" (Target Companies tab); postings
    # from the broad aggregators (Adzuna, JSearch) are "other" (Other
    # Postings tab). Cross-source dedupe below runs ATS boards first, so a
    # job appearing in both an ATS board and an aggregator keeps its
    # "target" group — the specific company match wins over the generic one.
    ats = cfg.get("ats_map", {})
    if ats.get("enabled", True):
        print("Fetching named-company ATS boards…")
        csv_path = ROOT / ats["csv"] if ats.get("csv") else None
        ats_jobs = get_all_ats_jobs(csv_path)
        for j in ats_jobs:
            j["group"] = "target"
        raw += ats_jobs

    adz = cfg.get("adzuna", {})
    if adz.get("enabled"):
        print("Fetching broad search (Adzuna)…")
        adzuna_jobs = fetch_adzuna(adz, criteria)
        for j in adzuna_jobs:
            j["group"] = "other"
        raw += adzuna_jobs

    js = cfg.get("jsearch", {})
    if js.get("enabled"):
        print("Fetching broad search (JSearch)…")
        jsearch_jobs = fetch_jsearch(js, criteria)
        for j in jsearch_jobs:
            j["group"] = "other"
        raw += jsearch_jobs

    # Guard: if every source came back empty, something upstream failed
    # (rate limit, expired key, API change). Don't publish a blank
    # dashboard — flag the existing one as stale and fail the run so the
    # workflow shows red and GitHub sends a failure notification.
    if not raw:
        print(
            "\nERROR: no postings returned from ANY source — today's search "
            "failed.\nThe dashboard was NOT overwritten (a stale-data banner "
            "was added instead).\nCheck the fetch warnings above, then re-run "
            "this workflow from the Actions tab.",
            file=sys.stderr,
        )
        mark_dashboard_stale()
        sys.exit(1)

    # Filter
    kept = [j for j in raw if matches(j, criteria)]

    # Dedupe (prefer first occurrence; ATS boards win over aggregator duplicates)
    seen_ids, deduped = set(), []
    for j in kept:
        key = j.get("id") or j.get("url")
        if key in seen_ids:
            continue
        seen_ids.add(key)
        deduped.append(j)

    # Second-pass dedupe: the same posting can come back from multiple
    # aggregators (or from both a company's ATS board and an aggregator)
    # under different ids/urls. Drop later duplicates by normalized
    # (title, company) so the earlier — i.e. higher-priority — source wins.
    seen_pairs, cross_deduped = set(), []
    for j in deduped:
        pair = ((j.get("title") or "").lower().strip(), (j.get("company") or "").lower().strip())
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        cross_deduped.append(j)
    deduped = cross_deduped

    # Sort: most recently posted first (jobs with no date sort last).
    deduped.sort(key=lambda j: j.get("posted") or "", reverse=True)

    label = cfg.get("label", "Job Radar")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(deduped, label, criteria.get("max_days_old")))

    print(f"\nDone: {len(deduped)} matches. Dashboard → {OUT_PATH}")


if __name__ == "__main__":
    main()
