#!/usr/bin/env python3
"""
Fetchers for the named-company ATS boards listed in ats_map.csv.

Covers seven platforms with three very different shapes:
  - Ashby / Workable / Greenhouse / Lever / Rippling: plain GET, JSON response
    (Rippling returns a bare list, not {"jobs": [...]} like the others).
  - Gem (jobs.gem.com): a client-rendered GraphQL SPA — no REST endpoint, so
    fetch_gem() replicates the app's own JobBoardList GraphQL query via POST
    to jobs.gem.com/api/public/graphql. Breaks if Gem changes that query.
  - WaaS (Work at a Startup): NOT the Algolia search embedded on the site —
    that key's baked-in tagFilters return zero hits for every query. Instead
    fetch_waas() reads the server-rendered Inertia.js payload on each
    company's own /companies/{slug} page, which lists that company's jobs
    directly. Breaks if workatastartup.com changes its Inertia component/props
    shape.

get_all_ats_jobs() reads ats_map.csv, dispatches each row by its `ats`
column, and normalizes every result into the same job-dict schema job_check.py
already uses for Adzuna/JSearch (id/title/company/location/url/salary_min/
salary_max/posted/description/source).
"""

import csv
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ATS_MAP_PATH = ROOT / "ats_map.csv"

HTTP_TIMEOUT = 20
# A generic "Mozilla/5.0" UA is enough for the ATS JSON APIs, but
# workatastartup.com 406s without a full browser UA + Accept header, so use
# both everywhere for consistency.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
}


def _get(url, params=None):
    r = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _slug_from_endpoint(endpoint):
    """Company slug, used only to namespace ids. Usually the last path
    segment, but Greenhouse/Rippling URLs end in a literal '/jobs' after the
    slug, so back up one more segment in that case."""
    segments = endpoint.split("?", 1)[0].rstrip("/").split("/")
    if segments[-1] == "jobs":
        return segments[-2]
    return segments[-1]


# --------------------------------------------------------------------------- #
# Raw fetchers — one per ATS, return the platform's own job objects unchanged
# --------------------------------------------------------------------------- #
def fetch_ashby(endpoint):
    data = _get(endpoint, params={"includeCompensation": "true"}).json()
    return data.get("jobs", [])


def fetch_workable(endpoint):
    data = _get(endpoint).json()
    return data.get("jobs", [])


def fetch_greenhouse(endpoint):
    data = _get(endpoint, params={"content": "true"}).json()
    return data.get("jobs", [])


def fetch_lever(endpoint):
    # endpoint already carries ?mode=json (see ats_map.csv)
    return _get(endpoint).json()


def fetch_rippling(endpoint):
    # Confirmed against a real endpoint: Rippling returns a bare list, not
    # {"jobs": [...]} — a dict-shaped fallback would break on r.json().get().
    data = _get(endpoint).json()
    return data if isinstance(data, list) else data.get("jobs", [])


GEM_GRAPHQL_URL = "https://jobs.gem.com/api/public/graphql"
GEM_JOB_BOARD_LIST_QUERY = """
query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      id
      extId
      title
      locations { id name city isoCountry isRemote extId }
      job { id department { id name extId } locationType employmentType }
    }
  }
}
"""


def fetch_gem(slug):
    payload = {
        "operationName": "JobBoardList",
        "variables": {"boardId": slug},
        "query": GEM_JOB_BOARD_LIST_QUERY,
    }
    r = requests.post(GEM_GRAPHQL_URL, json=payload, headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"gem GraphQL error for '{slug}': {data['errors']}")
    postings = (data.get("data") or {}).get("oatsExternalJobPostings") or {}
    return postings.get("jobPostings") or []


_INERTIA_PAGE_RE = re.compile(r'data-page="([^"]+)"')


def fetch_waas(slug):
    """workatastartup.com/companies/{slug} server-renders an Inertia.js page
    whose data-page attribute embeds props.company.jobs[] — the company's
    own listing, not a global search. No API key needed."""
    r = requests.get(
        f"https://www.workatastartup.com/companies/{slug}",
        headers=DEFAULT_HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    m = _INERTIA_PAGE_RE.search(r.text)
    if not m:
        raise RuntimeError(
            f"waas '{slug}': no Inertia data-page payload found — "
            "workatastartup.com page structure may have changed"
        )
    page = json.loads(html.unescape(m.group(1)))
    company = (page.get("props") or {}).get("company")
    return (company or {}).get("jobs") or []


# --------------------------------------------------------------------------- #
# Normalization — map each platform's raw job dict to the common schema
# --------------------------------------------------------------------------- #
def _normalize_ashby(j, company, slug):
    loc = j.get("location") or j.get("locationName") or ""
    if j.get("isRemote"):
        loc = (loc + " (Remote)").strip()
    comp = j.get("compensation") or {}
    smin = smax = None
    for tier in comp.get("compensationTiers") or []:
        for part in tier.get("components") or []:
            cv = part.get("compensationRange") or {}
            if cv.get("minValue"):
                smin, smax = cv.get("minValue"), cv.get("maxValue")
                break
        if smin:
            break
    return {
        "id": f"ab:{slug}:{j.get('id')}",
        "title": j.get("title", ""),
        "company": company,
        "location": loc,
        "url": j.get("jobUrl") or j.get("applyUrl", ""),
        "salary_min": smin,
        "salary_max": smax,
        "posted": j.get("publishedAt") or j.get("publishedDate"),
        "description": j.get("descriptionPlain") or "",
        "source": "ashby",
    }


def _normalize_workable(j, company, slug):
    city, state, country = j.get("city") or "", j.get("state") or "", j.get("country") or ""
    location = ", ".join(p for p in (city, state) if p) or country
    if j.get("telecommuting"):
        location = (location + " (Remote)").strip()
    posted = j.get("published_on") or j.get("created_at")
    if posted and len(posted) == 10:  # date-only "YYYY-MM-DD" -> make tz-aware
        posted = f"{posted}T00:00:00+00:00"
    return {
        "id": f"wk:{slug}:{j.get('shortcode') or j.get('code') or j.get('title')}",
        "title": j.get("title", ""),
        "company": company,
        "location": location,
        "url": j.get("url") or j.get("shortlink") or "",
        "salary_min": None,
        "salary_max": None,
        "posted": posted,
        "description": "",  # widget response has no job description field
        "source": "workable",
    }


def _normalize_greenhouse(j, company, slug):
    return {
        "id": f"gh:{slug}:{j.get('id')}",
        "title": j.get("title", ""),
        "company": company,
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "salary_min": None,
        "salary_max": None,
        # Really updated_at — an edited old posting can look recent. Same
        # quirk as the rest of the pipeline's Greenhouse handling.
        "posted": j.get("updated_at"),
        "description": _strip_html(j.get("content") or ""),
        "source": "greenhouse",
    }


def _normalize_lever(j, company, slug):
    cats = j.get("categories") or {}
    created = j.get("createdAt")
    posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat() if created else None
    salary = j.get("salaryRange") or {}
    return {
        "id": f"lv:{slug}:{j.get('id')}",
        "title": j.get("text", ""),
        "company": company,
        "location": cats.get("location", ""),
        "url": j.get("hostedUrl", ""),
        "salary_min": salary.get("min"),
        "salary_max": salary.get("max"),
        "posted": posted,
        "description": j.get("descriptionPlain") or j.get("description") or "",
        "source": "lever",
    }


def _normalize_rippling(j, company, slug):
    return {
        "id": f"rp:{slug}:{j.get('uuid')}",
        "title": j.get("name", ""),
        "company": company,
        "location": (j.get("workLocation") or {}).get("label", ""),
        "url": j.get("url", ""),
        "salary_min": None,
        "salary_max": None,
        "posted": None,  # not exposed by this endpoint
        "description": "",
        "source": "rippling",
    }


def _normalize_gem(j, company, slug):
    locs = j.get("locations") or []
    location = ", ".join(filter(None, (l.get("name") or l.get("city") or "" for l in locs)))
    if any(l.get("isRemote") for l in locs):
        location = (location + " (Remote)").strip()
    ext_id = j.get("extId") or j.get("id")
    return {
        "id": f"gm:{slug}:{ext_id}",
        "title": j.get("title", ""),
        "company": company,
        "location": location,
        "url": f"https://jobs.gem.com/{slug}/{ext_id}",
        "salary_min": None,
        "salary_max": None,
        "posted": None,  # not exposed by the list query (only per-job query has it)
        "description": "",
        "source": "gem",
    }


_K_RANGE_RE = re.compile(r"\$([\d,.]+)K\s*-\s*\$([\d,.]+)K", re.IGNORECASE)
_K_SINGLE_RE = re.compile(r"\$([\d,.]+)K", re.IGNORECASE)


def _parse_k_salary(s):
    """WaaS salaries are display strings like '$20K - $40K', not numbers."""
    if not s:
        return None, None
    m = _K_RANGE_RE.search(s)
    if m:
        return int(float(m.group(1).replace(",", "")) * 1000), int(float(m.group(2).replace(",", "")) * 1000)
    m = _K_SINGLE_RE.search(s)
    if m:
        v = int(float(m.group(1).replace(",", "")) * 1000)
        return v, v
    return None, None


def _normalize_waas(j, company, slug):
    smin, smax = _parse_k_salary(j.get("salaryRange"))
    return {
        "id": f"wa:{slug}:{j.get('id')}",
        "title": j.get("title", ""),
        "company": company,
        "location": j.get("location", ""),
        "url": f"https://www.workatastartup.com/jobs/{j.get('id')}",
        "salary_min": smin,
        "salary_max": smax,
        "posted": None,  # not exposed on the company page
        "description": "",
        "source": "waas",
    }


FETCHERS = {
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "rippling": fetch_rippling,
}
NORMALIZERS = {
    "ashby": _normalize_ashby,
    "workable": _normalize_workable,
    "greenhouse": _normalize_greenhouse,
    "lever": _normalize_lever,
    "rippling": _normalize_rippling,
}


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def get_all_ats_jobs(csv_path=None):
    """Read ats_map.csv, fetch every row through the right platform fetcher,
    and return one flat list of normalized job dicts. One bad row (dead
    board, API change, network error) is logged to stderr and skipped —
    never kills the run."""
    path = Path(csv_path) if csv_path else ATS_MAP_PATH
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            company = (row.get("company") or "").strip()
            ats = (row.get("ats") or "").strip().lower()
            endpoint = (row.get("endpoint") or "").strip()
            waas_slug = (row.get("waas_slug") or "").strip()
            if not company:
                continue
            try:
                if ats == "not found":
                    if not waas_slug:
                        print(f"  ! skipping {company}: no ats/endpoint and no waas_slug", file=sys.stderr)
                        continue
                    jobs = [_normalize_waas(j, company, waas_slug) for j in fetch_waas(waas_slug)]
                    label = "waas"
                elif ats == "gem":
                    if not endpoint:
                        print(f"  ! skipping {company}: ats 'gem' but empty endpoint", file=sys.stderr)
                        continue
                    slug = _slug_from_endpoint(endpoint)
                    jobs = [_normalize_gem(j, company, slug) for j in fetch_gem(slug)]
                    label = "gem"
                elif ats in FETCHERS:
                    if not endpoint:
                        print(f"  ! skipping {company}: ats '{ats}' but empty endpoint", file=sys.stderr)
                        continue
                    slug = _slug_from_endpoint(endpoint)
                    raw = FETCHERS[ats](endpoint)
                    jobs = [NORMALIZERS[ats](j, company, slug) for j in raw]
                    label = ats
                else:
                    print(f"  ! skipping {company}: unrecognized ats '{ats}'", file=sys.stderr)
                    continue
            except Exception as e:  # one bad board shouldn't kill the run
                print(f"  ! {company} ({ats or 'waas'}) failed: {e}", file=sys.stderr)
                continue
            out.extend(jobs)
            print(f"  · {company} ({label}): {len(jobs)} postings")
    return out
