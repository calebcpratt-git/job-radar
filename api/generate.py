"""Vercel serverless function: GET /api/generate

Generates a one-page resume tailored to a single job posting, on demand,
when the user clicks the "Tailor Resume" link next to that posting on the
dashboard. Nothing runs in bulk and nothing runs on page load — this only
executes when this specific endpoint is hit for this specific job.

Reads the master resume facts from resume/master_resume.yaml and
resume/experience_bank.yaml (both bundled in the deployment), asks Claude to
rewrite each role's bullets to emphasize what's relevant to the posting
(never inventing facts beyond what's in those two files), and renders the
result as a print-ready HTML page.

Not strongly authenticated — see the Referer check below and SKILL.md's
"Resume tailoring" section for why, and what to do about it (set a spend
cap on the Anthropic API key).
"""

import html
import json
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests
import yaml
from anthropic import Anthropic, APIError

RESUME_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resume")

# Every host the dashboard is actually served from (see SKILL.md's
# "Deployment & operations" section) — clicking the link from any of them
# must work. Vercel assigns a project several aliases for the same
# deployment (short form, team-scoped form, git-branch form); all of them
# need to be here, not just the one that happens to be documented.
# This is abuse-deterrence, not real auth: a Referer header is trivially
# spoofable outside a browser. It stops a random crawler or a third-party
# page from silently burning API spend, nothing more; set a spend limit on
# the Anthropic API key for real protection.
ALLOWED_REFERER_HOSTS = {
    h.strip()
    for h in os.environ.get(
        "ALLOWED_REFERER_HOSTS",
        "calebcpratt-git.github.io,"
        "get-rich-radar.vercel.app,"
        "get-rich-radar-team-caleb1.vercel.app,"
        "get-rich-radar-git-main-team-caleb1.vercel.app",
    ).split(",")
    if h.strip()
}

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
EFFORT = os.environ.get("ANTHROPIC_EFFORT", "medium")

MAX_FIELD_LEN = 300
MAX_URL_LEN = 2000
MAX_DESCRIPTION_CHARS = 6000

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)

ROLE_KEYS = [
    "playbook_csm",
    "playbook_partnerships",
    "revolution_learning",
    "interscope",
    "neuberger",
    "ltx",
]

RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        **{f"{key}_reasoning": {"type": "string"} for key in ROLE_KEYS},
        **{f"{key}_bullets": {"type": "array", "items": {"type": "string"}} for key in ROLE_KEYS},
        "optional_block": {"type": "string", "enum": ["projects", "tech_business_fellows", "none"]},
    },
    "required": [f"{key}_reasoning" for key in ROLE_KEYS]
    + [f"{key}_bullets" for key in ROLE_KEYS]
    + ["optional_block"],
    "additionalProperties": False,
}


def _load_yaml(filename):
    with open(os.path.join(RESUME_DIR, filename), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _strip_html(raw_html):
    text = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_description(url):
    """Best-effort: scrape the posting for extra context. Never fatal."""
    try:
        resp = requests.get(
            url,
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; job-radar-resume-tailor/1.0)"},
        )
        resp.raise_for_status()
        return _strip_html(resp.text)[:MAX_DESCRIPTION_CHARS]
    except Exception as e:
        print(f"! description fetch failed for {url}: {e}")
        return None


def _find_role(master, role_id):
    for company in master["experience"]:
        for role in company["roles"]:
            if role["id"] == role_id:
                return company, role
    raise KeyError(role_id)


def _build_prompt(master, bank, title, company, location, description):
    facts_lines = []
    for exp_company in master["experience"]:
        for role in exp_company["roles"]:
            label = role["title"] or exp_company["subtitle"]
            facts_lines.append(
                f"\n[{role['id']}] {exp_company['company']} — {label} "
                f"(target: {role['bullet_target']} bullets)"
            )
            for b in role["bullets"]:
                facts_lines.append(f"  - {b}")

    bank_lines = []
    for company_key, notes in (bank.get("by_company") or {}).items():
        for note in notes or []:
            bank_lines.append(f"  - ({company_key}) {note}")
    for note in bank.get("general") or []:
        bank_lines.append(f"  - (general) {note}")

    optional = master["optional_blocks"]
    project = optional["projects"]["entries"][0]
    tbf = optional["tech_business_fellows"]["entries"][0]

    posting_lines = [f"Title: {title}", f"Company: {company}"]
    if location:
        posting_lines.append(f"Location: {location}")
    if description:
        posting_lines.append(f"\nPosting text (scraped, may include site chrome — use your judgment):\n{description}")
    else:
        posting_lines.append("\n(No posting description available — tailor based on title/company/location only.)")

    user_content = f"""<target_posting>
{chr(10).join(posting_lines)}
</target_posting>

<source_facts>
Each role's true accomplishments — a pool to rephrase from, not a script to copy verbatim:
{chr(10).join(facts_lines)}
</source_facts>

<extra_accomplishments>
Not on the base resume. Use only if relevant to this posting:
{chr(10).join(bank_lines) if bank_lines else "  (none yet)"}
</extra_accomplishments>

<optional_sections>
Pick exactly one, or "none":
  - "projects": {project['name']} — {project['bullets'][0]} (best for: {optional['projects']['relevance_hint']})
  - "tech_business_fellows": {tbf['org']} — {tbf['bullets'][0]} (best for: {optional['tech_business_fellows']['relevance_hint']})
</optional_sections>
"""
    return user_content


SYSTEM_PROMPT = """You tailor Caleb Pratt's resume bullets for one specific job posting at a time.

<task>
For each role, rewrite its bullets so phrasing and emphasis foreground the skills, tools, and responsibilities most relevant to the target posting. A reader should come away thinking Caleb has done a lot of what this job needs. Where truthful and natural, echo the posting's own terminology (e.g. if it says "cross-functional stakeholder management," and a source fact supports that framing, use those words) — this helps both human skimmers and ATS parsing.
</task>

<ground_truth_rule>
Every rewritten bullet must be traceable to a specific source fact provided to you. You may reorder, recombine, rephrase, and re-emphasize freely. You may NEVER invent a new accomplishment, employer, tool, metric, or outcome that isn't present in the source facts or extra accomplishments given to you. This is a hard rule, not a style preference.
</ground_truth_rule>

<example>
Source fact: "Managed relationships with 12 enterprise accounts, coordinating with product and support teams to resolve escalations"
Posting emphasizes: cross-functional coordination, technical stakeholder management

Good rewrite: "Coordinated cross-functionally with product and support teams to resolve escalations across 12 accounts"
— Reordered to lead with the coordination skill the posting cares about. Same facts, same numbers. 103 characters — fills the line without wrapping.

Bad rewrite: "Led cross-functional initiatives improving enterprise account retention by 30%"
— Invents a metric (30%) and reframes "managed relationships" as "led initiatives." Not allowed, even though it sounds more impressive.
</example>

<reasoning_step>
Before writing the final bullets for a role, briefly note in that role's "reasoning" field: which 2-3 source facts for that role are most relevant to this specific posting, and why. Keep this to one or two sentences per role — it's a working note for prompt evaluation, not part of the printed resume.
</reasoning_step>

<constraints>
The resume layout is fixed to exactly one printed page.

Hard line-length limit: each bullet must be 118 characters or fewer, counting every letter, space, and punctuation mark. Treat 118 as a true ceiling, not a target to approach — err toward slightly under it rather than over, since exceeding it wraps the bullet to a second line and breaks the one-page layout. Count carefully; do not estimate.

Also avoid running too short: aim for 100-118 characters where the source fact supports it, so lines use the available space rather than leaving obvious blank space at the end. Only go shorter than 100 when the underlying source fact genuinely doesn't support more detail — never pad with filler words just to reach the target.

When constraints conflict, priority order is:
(1) never invent facts
(2) every bullet stays at or under 118 characters — when in doubt, go shorter, not longer
(3) every bullet reaches at least 100 characters where the source fact supports it
(4) stay at or under each role's target bullet count (never exceed by more than one)
(5) vary phrasing/verbs across roles

If a fact is too rich to fit in one 118-character bullet without inventing or dropping essential meaning, cut it down to its single most relevant, truthful part rather than running long.
</constraints>

<optional_section>
Choose the one optional section (or "none") most relevant to this posting, based on the relevance hints provided.
</optional_section>

Respond in the required JSON format only — no commentary outside the schema fields."""


def _generate_bullets(master, bank, title, company, location, description):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the Vercel project's environment variables")

    client = Anthropic(api_key=api_key, timeout=55.0)
    user_content = _build_prompt(master, bank, title, company, location, description)

    response = client.messages.create(
        model=MODEL,
        max_tokens=6000,
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": RESUME_SCHEMA},
        },
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _render_resume_html(master, tailored, job_title, job_company):
    h = master["header"]
    parts = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(h['name'])} — Resume ({html.escape(job_company)})</title>
<style>
  @page {{ size: letter; margin: 0.4in 0.5in; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: Calibri, Carlito, Candara, Segoe, "Segoe UI", Optima, Arial, sans-serif; color: #111;
    font-size: 9.1pt; line-height: 1.25; max-width: 8.5in; margin: 0 auto; padding: 0.35in 0.5in;
  }}
  .toolbar {{
    display: flex; justify-content: flex-end; gap: 10px; margin-bottom: 14px;
    font-family: -apple-system, sans-serif;
  }}
  .toolbar button {{
    font: inherit; font-size: 13px; padding: 7px 14px; border-radius: 6px;
    border: 1px solid #1F51D6; background: #1F51D6; color: #fff; cursor: pointer;
  }}
  h1 {{ text-align: center; font-size: 15pt; letter-spacing: 0.03em; margin: 0 0 2px; }}
  .contact {{ text-align: center; font-size: 9.2pt; margin: 0 0 7px; }}
  .contact a {{ color: #111; }}
  h2 {{
    font-size: 9.4pt; letter-spacing: 0.05em; border-bottom: 1px solid #111;
    margin: 8px 0 3px; padding-bottom: 1px; text-transform: uppercase;
  }}
  .entry {{ margin-bottom: 2px; }}
  .row {{ display: flex; justify-content: space-between; gap: 12px; font-weight: bold; }}
  .row .dates {{ white-space: nowrap; font-weight: normal; }}
  .subtitle {{ font-style: italic; display: flex; justify-content: space-between; gap: 12px; }}
  ul {{ margin: 1px 0 4px; padding-left: 16px; }}
  li {{ margin: 0 0 0.5px; }}
  .role-title {{ font-style: italic; margin: 1px 0 0; }}
  .edu-line {{ font-size: 9.8pt; }}
  .add-info p {{ margin: 1px 0; font-size: 9.8pt; }}
  .add-info b {{ font-weight: bold; }}
  @media print {{
    .toolbar {{ display: none; }}
    body {{ padding: 0; }}
  }}
</style>
</head>
<body>
<div class="toolbar"><button onclick="window.print()">Print / Save as PDF</button></div>
<h1>{html.escape(h['name'])}</h1>
<p class="contact"><a href="mailto:{html.escape(h['email'])}">{html.escape(h['email'])}</a> &middot; {html.escape(h['phone'])}</p>
<h2>Experience</h2>""")

    for company in master["experience"]:
        parts.append('<div class="entry">')
        parts.append(
            f'<div class="row"><span>{html.escape(company["company"])} '
            f'&ndash; <span style="font-weight:normal;font-style:italic">{html.escape(company["subtitle"])}</span>; '
            f'{html.escape(company["location"])}</span>'
            f'<span class="dates">{html.escape(company["start"])} &ndash; {html.escape(company["end"])}</span></div>'
        )
        for role in company["roles"]:
            bullets = tailored.get(f"{role['id']}_bullets") or role["bullets"]
            if not bullets:
                bullets = role["bullets"]
            if role["title"]:
                parts.append(
                    f'<div class="role-title">{html.escape(role["title"])} '
                    f'({html.escape(role["start"])} &ndash; {html.escape(role["end"])})</div>'
                )
            parts.append("<ul>")
            for b in bullets:
                parts.append(f"<li>{html.escape(b)}</li>")
            parts.append("</ul>")
        parts.append("</div>")

    parts.append("<h2>Education</h2>")
    for edu in master["education"]:
        parts.append(
            f'<div class="entry edu-line"><div class="row"><span>{html.escape(edu["school"])}</span>'
            f'<span class="dates">{html.escape(edu["date"])}</span></div>'
            f'<div>{html.escape(edu["degree"])}</div>'
            f'<div>Minor: {html.escape(edu["minor"])} &middot; {html.escape(edu["honors"])}</div></div>'
        )

    optional_block = tailored.get("optional_block", "none")
    if optional_block == "projects":
        parts.append("<h2>Projects</h2>")
        for entry in master["optional_blocks"]["projects"]["entries"]:
            parts.append(
                f'<div class="entry"><div class="row"><span>{html.escape(entry["name"])}</span>'
                f'<span class="dates">{html.escape(entry["date"])}</span></div>'
                f'<div style="font-size:9.6pt">{html.escape(entry["link"])}</div><ul>'
                + "".join(f"<li>{html.escape(b)}</li>" for b in entry["bullets"])
                + "</ul></div>"
            )

    parts.append("<h2>Undergraduate Leadership Experience and Activities</h2>")
    for entry in master["leadership"]["entries"]:
        parts.append(
            f'<div class="entry"><div class="row"><span>{html.escape(entry["org"])} &ndash; {html.escape(entry["role"])}</span>'
            f'<span class="dates">{html.escape(entry["dates"])}</span></div>'
        )
        for sub in entry.get("subroles", []):
            parts.append(
                f'<div class="role-title">{html.escape(sub["title"])} ({html.escape(sub["dates"])})</div><ul>'
                + "".join(f"<li>{html.escape(b)}</li>" for b in sub["bullets"])
                + "</ul>"
            )
        parts.append("</div>")
    if optional_block == "tech_business_fellows":
        for entry in master["optional_blocks"]["tech_business_fellows"]["entries"]:
            parts.append(
                f'<div class="entry"><div class="row"><span>{html.escape(entry["org"])} &ndash; {html.escape(entry["role"])}</span>'
                f'<span class="dates">{html.escape(entry["dates"])}</span></div><ul>'
                + "".join(f"<li>{html.escape(b)}</li>" for b in entry["bullets"])
                + "</ul></div>"
            )

    add = master["additional"]
    parts.append(
        '<h2>Additional Information</h2><div class="add-info">'
        f'<p><b>Computer Skills:</b> {html.escape(", ".join(add["computer_skills"]))}</p>'
        f'<p><b>Interests:</b> {html.escape(", ".join(add["interests"]))}</p>'
        f'<p><b>Work Eligibility:</b> {html.escape(add["work_eligibility"])}</p>'
        "</div>"
    )
    parts.append(f"<!-- tailored for: {html.escape(job_title)} @ {html.escape(job_company)} -->")
    parts.append("</body></html>")
    return "\n".join(parts)


def _error_page(status_text, message):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Resume generation failed</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:80px auto;color:#333">
<h1 style="color:#c0392b">{html.escape(status_text)}</h1>
<p>{html.escape(message)}</p>
</body></html>"""


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        referer = self.headers.get("Referer", "")
        referer_host = urlparse(referer).hostname or ""
        if referer_host not in ALLOWED_REFERER_HOSTS:
            self._send(403, _error_page(
                "Blocked",
                "This endpoint only serves requests coming from the job-radar dashboard.",
            ))
            return

        title = (params.get("title", [""])[0])[:MAX_FIELD_LEN]
        company = (params.get("company", [""])[0])[:MAX_FIELD_LEN]
        location = (params.get("location", [""])[0])[:MAX_FIELD_LEN]
        job_url = (params.get("url", [""])[0])[:MAX_URL_LEN]

        if not title or not company:
            self._send(400, _error_page("Missing job info", "title and company query params are required."))
            return

        try:
            master = _load_yaml("master_resume.yaml")
            bank = _load_yaml("experience_bank.yaml")
            description = _fetch_description(job_url) if job_url else None
            tailored = _generate_bullets(master, bank, title, company, location, description)
            resume_html = _render_resume_html(master, tailored, title, company)
        except APIError as e:
            print(f"! Claude API error: {e}")
            self._send(502, _error_page("Claude API error", str(e)))
            return
        except Exception as e:
            print(f"! resume generation failed: {e}")
            self._send(500, _error_page("Generation failed", str(e)))
            return

        self._send(200, resume_html)

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))
