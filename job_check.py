#!/usr/bin/env python3
"""
Job Radar — pulls job postings each morning, filters them to your criteria,
and writes an HTML dashboard.

Sources:
  - Specific companies via their public ATS boards (Greenhouse / Lever / Ashby)
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
def _get(url, params=None, headers=None):
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT, headers=merged_headers)
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(token):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                params={"content": "true"})
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"gh:{token}:{j.get('id')}",
            "title": j.get("title", ""),
            "company": None,  # filled by caller
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "salary_min": None,
            "salary_max": None,
            "posted": j.get("updated_at"),
            "source": "greenhouse",
        })
    return jobs


def fetch_lever(token):
    data = _get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        posted = None
        if created:
            posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
        jobs.append({
            "id": f"lv:{token}:{j.get('id')}",
            "title": j.get("text", ""),
            "company": None,
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "salary_min": None,
            "salary_max": None,
            "posted": posted,
            "source": "lever",
        })
    return jobs


def fetch_ashby(token):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                params={"includeCompensation": "true"})
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location") or j.get("locationName") or ""
        if j.get("isRemote"):
            loc = (loc + " (Remote)").strip()
        comp = j.get("compensation") or {}
        smin = smax = None
        # Ashby nests compensation tiers; grab the first numeric range if present.
        for tier in (comp.get("compensationTiers") or []):
            for comp_part in (tier.get("components") or []):
                cv = comp_part.get("compensationRange") or {}
                if cv.get("minValue"):
                    smin = cv.get("minValue")
                    smax = cv.get("maxValue")
                    break
            if smin:
                break
        jobs.append({
            "id": f"ab:{token}:{j.get('id')}",
            "title": j.get("title", ""),
            "company": None,
            "location": loc,
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "salary_min": smin,
            "salary_max": smax,
            "posted": j.get("publishedAt") or j.get("publishedDate"),
            "source": "ashby",
        })
    return jobs


ATS_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_companies(companies):
    out = []
    for c in companies:
        ats = (c.get("ats") or "").lower()
        token = c.get("token")
        name = c.get("name") or token
        fetcher = ATS_FETCHERS.get(ats)
        if not fetcher or not token:
            print(f"  ! skipping {name}: unknown ats '{ats}' or missing token", file=sys.stderr)
            continue
        try:
            jobs = fetcher(token)
            for j in jobs:
                j["company"] = name
            out.extend(jobs)
            print(f"  · {name} ({ats}): {len(jobs)} postings")
        except Exception as e:  # one bad board shouldn't kill the run
            print(f"  ! {name} ({ats}) failed: {e}", file=sys.stderr)
    return out


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
    out = []
    for page in range(1, pages + 1):
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": per_page,
            "what_or": cfg.get("what_or", ""),
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
                "source": f"JSearch ({publisher})",
            })
    print(f"  · JSearch broad search: {len(out)} postings")
    return out


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
def looks_remote(text):
    return "remote" in (text or "").lower()


def matches(job, criteria):
    title = (job.get("title") or "").lower()

    words = set(re.findall(r"[a-z]+", title))
    if not ("product" in words and ("operations" in words or "ops" in words)):
        return False

    loc_filter = (criteria.get("location") or "").lower().strip()
    if loc_filter:
        job_loc = (job.get("location") or "").lower()
        haystack = job_loc + " " + (job.get("location_area") or "").lower()
        remote_ok = criteria.get("remote_ok", True) and looks_remote(job_loc)
        if loc_filter not in haystack and not remote_ok:
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


def fmt_posted(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - dt).days
        if days <= 0:
            return "today"
        if days == 1:
            return "1 day ago"
        return f"{days} days ago"
    except Exception:
        return ""


def render(jobs, label, max_days_old):
    now = datetime.now(timezone.utc).astimezone()

    rows = []
    for j in jobs:
        salary = fmt_salary(j)
        posted = fmt_posted(j.get("posted"))
        meta = " · ".join(x for x in [
            html.escape(j.get("source", "")),
            posted,
        ] if x)
        rows.append(f"""
        <li class="job">
          <a class="title" href="{html.escape(j.get('url',''))}" target="_blank" rel="noopener">
            {html.escape(j.get('title','') or 'Untitled role')}
          </a>
          <div class="sub">
            <span class="company">{html.escape(j.get('company','') or '')}</span>
            <span class="loc">{html.escape(j.get('location','') or '')}</span>
            {f'<span class="salary">{html.escape(salary)}</span>' if salary else ''}
          </div>
          <div class="meta">{meta}</div>
        </li>""")

    empty = '<li class="empty">No matches today. The wire is quiet — check back tomorrow.</li>'
    body = "\n".join(rows) if rows else empty

    return TEMPLATE.format(
        label=html.escape(label),
        date=now.strftime("%A, %B %-d"),
        updated=now.strftime("%-I:%M %p %Z"),
        total=len(jobs),
        window_days=max_days_old,
        rows=body,
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
  .wrap {{ max-width:760px; margin:0 auto; padding:48px 24px 96px; }}
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
  .controls {{ display:flex; gap:12px; align-items:center; margin:22px 0 6px; }}
  #q {{
    flex:1; padding:9px 12px; border:1px solid var(--line); border-radius:8px;
    font:inherit; font-size:14px; background:var(--surface); color:var(--ink);
  }}
  #q:focus {{ outline:2px solid var(--accent); outline-offset:1px; border-color:transparent; }}
  ul {{ list-style:none; margin:8px 0 0; padding:0; }}
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

    <div class="controls">
      <input id="q" type="text" placeholder="Filter by title, company, or location…" aria-label="Filter jobs">
    </div>

    <ul id="list">
      {rows}
    </ul>

    <footer>Job Radar · pulls from your named company boards + a broad Adzuna search, once each morning.</footer>
  </div>

<script>
  const q = document.getElementById('q');
  const items = Array.from(document.querySelectorAll('#list .job'));
  function apply() {{
    const term = q.value.trim().toLowerCase();
    items.forEach(el => {{
      const hitText = !term || el.textContent.toLowerCase().includes(term);
      el.style.display = hitText ? '' : 'none';
    }});
  }}
  q.addEventListener('input', apply);
</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    criteria = {
        "location": cfg.get("location", ""),
        "remote_ok": cfg.get("remote_ok", True),
        "max_days_old": cfg.get("max_days_old"),
    }

    raw = []

    adz = cfg.get("adzuna", {})
    if adz.get("enabled"):
        print("Fetching broad search (Adzuna)…")
        raw += fetch_adzuna(adz, criteria)

    js = cfg.get("jsearch", {})
    if js.get("enabled"):
        print("Fetching broad search (JSearch)…")
        raw += fetch_jsearch(js, criteria)

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
