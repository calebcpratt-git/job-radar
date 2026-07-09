#!/usr/bin/env python3
"""
Job Radar — pulls job postings each morning, filters them to your criteria,
flags anything new since the last run, and writes an HTML dashboard.

Sources:
  - Specific companies via their public ATS boards (Greenhouse / Lever / Ashby)
  - A broad search across job boards via the Adzuna API

Nothing here needs a paid service. The only credentials are a free Adzuna
app id + key, read from the environment (ADZUNA_APP_ID / ADZUNA_APP_KEY).
"""

import html
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
SEEN_PATH = ROOT / "seen_jobs.json"
OUT_PATH = ROOT / "docs" / "index.html"

HTTP_TIMEOUT = 20
USER_AGENT = "job-radar/1.0 (personal job search)"


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def _get(url, params=None):
    r = requests.get(
        url, params=params, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
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
        if criteria.get("salary_min"):
            params["salary_min"] = criteria["salary_min"]
        if criteria.get("max_days_old"):
            params["max_days_old"] = criteria["max_days_old"]
        if cfg.get("what_exclude"):
            params["what_exclude"] = cfg["what_exclude"]
        try:
            data = _get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}", params=params)
        except Exception as e:
            print(f"  ! Adzuna page {page} failed: {e}", file=sys.stderr)
            break
        results = data.get("results", [])
        for j in results:
            out.append({
                "id": f"az:{j.get('id')}",
                "title": j.get("title", ""),
                "company": (j.get("company") or {}).get("display_name", ""),
                "location": (j.get("location") or {}).get("display_name", ""),
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


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
def looks_remote(text):
    return "remote" in (text or "").lower()


def matches(job, criteria):
    title = (job.get("title") or "").lower()

    any_kw = [k.lower() for k in criteria.get("keywords_any", []) if k.strip()]
    if any_kw and not any(k in title for k in any_kw):
        return False

    for bad in criteria.get("keywords_exclude", []):
        if bad.strip() and bad.lower() in title:
            return False

    loc_filter = (criteria.get("location") or "").lower().strip()
    if loc_filter:
        job_loc = (job.get("location") or "").lower()
        remote_ok = criteria.get("remote_ok", True) and looks_remote(job_loc)
        if loc_filter not in job_loc and not remote_ok:
            return False

    floor = criteria.get("salary_min")
    if floor:
        smax = job.get("salary_max") or job.get("salary_min")
        # Only exclude when a salary is known AND clearly below the floor.
        if smax is not None and smax < floor:
            return False

    return True


# --------------------------------------------------------------------------- #
# "New since last run" tracking
# --------------------------------------------------------------------------- #
def load_seen():
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_seen(seen):
    # Prune anything first seen more than 60 days ago to keep the file small.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.write_text(json.dumps(pruned, indent=0, sort_keys=True))


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


def render(jobs, label):
    now = datetime.now(timezone.utc).astimezone()
    new_count = sum(1 for j in jobs if j["is_new"])

    rows = []
    for j in jobs:
        salary = fmt_salary(j)
        posted = fmt_posted(j.get("posted"))
        new_pill = '<span class="pill">new</span>' if j["is_new"] else ""
        meta = " · ".join(x for x in [
            html.escape(j.get("source", "")),
            posted,
        ] if x)
        rows.append(f"""
        <li class="job{' is-new' if j['is_new'] else ''}" data-new="{str(j['is_new']).lower()}">
          <a class="title" href="{html.escape(j.get('url',''))}" target="_blank" rel="noopener">
            {html.escape(j.get('title','') or 'Untitled role')}{new_pill}
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
        new_count=new_count,
        new_word="match" if new_count == 1 else "matches",
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
  .toggle {{
    font-family:"IBM Plex Mono",monospace; font-size:12px; letter-spacing:0.04em;
    text-transform:uppercase; color:var(--muted); cursor:pointer;
    display:flex; align-items:center; gap:7px; user-select:none; white-space:nowrap;
  }}
  .toggle input {{ accent-color:var(--signal); width:15px; height:15px; }}
  ul {{ list-style:none; margin:8px 0 0; padding:0; }}
  .job {{ padding:20px 0 18px; border-bottom:1px solid var(--line); }}
  .job.is-new {{
    margin:0 -16px; padding:18px 16px 16px; border-bottom:1px solid var(--line);
    background:linear-gradient(90deg,var(--signal-bg),transparent 62%);
    border-left:3px solid var(--signal);
  }}
  .title {{
    font-family:"Space Grotesk",sans-serif; font-weight:500; font-size:19px;
    color:var(--ink); text-decoration:none; letter-spacing:-0.01em;
    display:inline-flex; align-items:center; gap:10px;
  }}
  .title:hover {{ color:var(--accent); }}
  .pill {{
    font-family:"IBM Plex Mono",monospace; font-size:10px; font-weight:500;
    letter-spacing:0.1em; text-transform:uppercase; color:var(--signal);
    background:var(--signal-bg); border:1px solid #EAD6AE;
    padding:2px 7px; border-radius:999px;
  }}
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
        <span class="hot">{new_count} new {new_word}</span>
        <span>{total} total on the board</span>
        <span>updated {updated}</span>
      </div>
    </header>

    <div class="controls">
      <input id="q" type="text" placeholder="Filter by title, company, or location…" aria-label="Filter jobs">
      <label class="toggle"><input type="checkbox" id="newonly"> new only</label>
    </div>

    <ul id="list">
      {rows}
    </ul>

    <footer>Job Radar · pulls from your named company boards + a broad Adzuna search, once each morning.</footer>
  </div>

<script>
  const q = document.getElementById('q');
  const newonly = document.getElementById('newonly');
  const items = Array.from(document.querySelectorAll('#list .job'));
  function apply() {{
    const term = q.value.trim().toLowerCase();
    const onlyNew = newonly.checked;
    items.forEach(el => {{
      const hitText = !term || el.textContent.toLowerCase().includes(term);
      const hitNew = !onlyNew || el.dataset.new === 'true';
      el.style.display = (hitText && hitNew) ? '' : 'none';
    }});
  }}
  q.addEventListener('input', apply);
  newonly.addEventListener('change', apply);
</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    criteria = {
        "keywords_any": cfg.get("keywords_any", []),
        "keywords_exclude": cfg.get("keywords_exclude", []),
        "location": cfg.get("location", ""),
        "remote_ok": cfg.get("remote_ok", True),
        "salary_min": cfg.get("salary_min"),
        "max_days_old": cfg.get("max_days_old"),
    }

    print("Fetching company boards…")
    raw = fetch_companies(cfg.get("companies", []))

    adz = cfg.get("adzuna", {})
    if adz.get("enabled"):
        print("Fetching broad search…")
        raw += fetch_adzuna(adz, criteria)

    # Filter
    kept = [j for j in raw if matches(j, criteria)]

    # Dedupe (prefer first occurrence; ATS boards win over Adzuna duplicates)
    seen_ids, deduped = set(), []
    for j in kept:
        key = j.get("id") or j.get("url")
        if key in seen_ids:
            continue
        seen_ids.add(key)
        deduped.append(j)

    # Flag new vs. previously seen
    seen = load_seen()
    now_iso = datetime.now(timezone.utc).isoformat()
    for j in deduped:
        key = j["id"]
        j["is_new"] = key not in seen
        seen.setdefault(key, now_iso)
    save_seen(seen)

    # Sort: new first, then most recently posted
    deduped.sort(key=lambda j: (not j["is_new"], j.get("posted") or ""), reverse=False)
    deduped.sort(key=lambda j: j.get("posted") or "", reverse=True)
    deduped.sort(key=lambda j: not j["is_new"])

    label = cfg.get("label", "Job Radar")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(deduped, label))

    new_n = sum(1 for j in deduped if j["is_new"])
    print(f"\nDone: {len(deduped)} matches ({new_n} new). Dashboard → {OUT_PATH}")


if __name__ == "__main__":
    main()
