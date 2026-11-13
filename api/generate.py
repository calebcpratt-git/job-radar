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
from anthropic import (
    Anthropic,
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

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
        "calebs-job-radar.vercel.app,"
        "calebs-job-radar-team-caleb1.vercel.app,"
        "calebs-job-radar-git-main-team-caleb1.vercel.app",
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
_META_DESCRIPTION_RE = re.compile(
    r"""<meta\s+(?:[^>]*?\s)?name=["']description["'][^>]*?\scontent=(["'])(.*?)\1[^>]*>""",
    re.IGNORECASE | re.DOTALL,
)
_OG_DESCRIPTION_RE = re.compile(
    r"""<meta\s+(?:[^>]*?\s)?property=["']og:description["'][^>]*?\scontent=(["'])(.*?)\1[^>]*>""",
    re.IGNORECASE | re.DOTALL,
)
# Below this length, treat the stripped body as an empty JS-app shell (e.g.
# Ashby, Greenhouse embeds) rather than real posting text, and fall back to
# the <meta name="description">/og:description content instead — ATS SPAs
# commonly stuff the full job description there for link previews/SEO even
# though the rendered body is empty without JavaScript.
MIN_BODY_TEXT_LEN = 200

ROLE_KEYS = [
    "playbook_csm",
    "playbook_partnerships",
    "revolution_learning",
    "interscope",
    "neuberger",
    "ltx",
]

# Longest "Label: item, item, ..." skills line that still fits on one
# printed line at 10pt Calibri/Carlito (the 2026-11 base resume's longest is
# 114 chars). _pick_skill_groups trims trailing items past this.
SKILL_LINE_MAX_CHARS = 118

RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        **{f"{key}_reasoning": {"type": "string"} for key in ROLE_KEYS},
        **{f"{key}_bullets": {"type": "array", "items": {"type": "string"}} for key in ROLE_KEYS},
        "optional_block": {"type": "string", "enum": ["projects", "tech_business_fellows", "none"]},
        # Whether the target posting is itself a recruiting/talent-acquisition
        # role. Drives two things: (1) bullet selection/ordering prioritizes
        # recruiting-relevant source facts within each role, and (2) rendering
        # swaps in each Playbook role's title_recruiting in place of its
        # normal title (see _render_resume_html).
        "recruiting_focus": {"type": "boolean"},
        # Skill lines to display under "Additional Information", one per
        # labeled group in master_resume.yaml's additional.skill_groups.
        # Normally the defaults verbatim -- only deviate (pulling from
        # computer_skills_pool) when specific items are clearly more relevant
        # to this posting. Validated again at render time (_pick_skill_groups).
        "skill_groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["label", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": [f"{key}_reasoning" for key in ROLE_KEYS]
    + [f"{key}_bullets" for key in ROLE_KEYS]
    + ["optional_block", "recruiting_focus", "skill_groups"],
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


def _meta_description(raw_html):
    m = _META_DESCRIPTION_RE.search(raw_html) or _OG_DESCRIPTION_RE.search(raw_html)
    if not m:
        return None
    return _strip_html(html.unescape(m.group(2)))


def _fetch_description(url):
    """Best-effort: scrape the posting for extra context. Never fatal."""
    try:
        resp = requests.get(
            url,
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; job-radar-resume-tailor/1.0)"},
        )
        resp.raise_for_status()
        body_text = _strip_html(resp.text)
        if len(body_text) < MIN_BODY_TEXT_LEN:
            meta_text = _meta_description(resp.text)
            if meta_text and len(meta_text) > len(body_text):
                body_text = meta_text
        return body_text[:MAX_DESCRIPTION_CHARS]
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
            target_note = f"target: {role['bullet_target']} bullets"
            if role.get("bullet_target_recruiting"):
                target_note += f", or {role['bullet_target_recruiting']} bullets if recruiting_focus is true"
            facts_lines.append(
                f"\n[{role['id']}] {exp_company['company']} — {label} ({target_note})"
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

    skill_groups = master["additional"]["skill_groups"]
    skills_pool = master["additional"]["computer_skills_pool"]
    default_skill_lines = "\n".join(f"  {g['label']}: {', '.join(g['items'])}" for g in skill_groups)

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
Also true, and as usable as the source facts above (includes the newer phrasings from Caleb's latest resume). A leading [Role title] says which role a note belongs to. Use whichever facts best fit this posting:
{chr(10).join(bank_lines) if bank_lines else "  (none yet)"}
</extra_accomplishments>

<optional_sections>
Pick exactly one, or "none":
  - "projects": {project['name']} — {project['bullets'][0]} (best for: {optional['projects']['relevance_hint']})
  - "tech_business_fellows": {tbf['org']} — {tbf['bullets'][0]} (best for: {optional['tech_business_fellows']['relevance_hint']})
</optional_sections>

<computer_skills>
Default skill lines, one per label (use these verbatim unless the posting clearly calls for a swap):
{default_skill_lines}

Full pool across every resume version Caleb has maintained (only pull from here, never invent a tool/skill not listed):
{", ".join(skills_pool)}
</computer_skills>
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

Hard line-length limit: each bullet must be 124 characters or fewer, counting every letter, space, and punctuation mark. Treat 124 as a true ceiling, not a target to approach — err toward slightly under it rather than over, since exceeding it wraps the bullet to a second line and breaks the one-page layout. Count carefully; do not estimate.

Also avoid running too short: aim for 110-124 characters where the source fact supports it, so lines use the available space rather than leaving obvious blank space at the end. Most bullets should land in the upper part of that band (115-124), not clustered near 110. Only go shorter than 110 when the underlying source fact genuinely doesn't support more detail — never pad with filler words just to reach the target.

When constraints conflict, priority order is:
(1) never invent facts
(2) every bullet stays at or under 124 characters — when in doubt, go shorter, not longer
(3) every bullet reaches at least 110 characters where the source fact supports it, ideally landing in the 115-124 range
(4) stay at or under each role's target bullet count (never exceed by more than one)
(5) vary phrasing/verbs across roles

If a fact is too rich to fit in one 124-character bullet without inventing or dropping essential meaning, cut it down to its single most relevant, truthful part rather than running long.
</constraints>

<optional_section>
Choose the one optional section (or "none") most relevant to this posting, based on the relevance hints provided.
</optional_section>

<recruiting_focus>
Set "recruiting_focus" to true if the target posting is itself a recruiting, talent acquisition, sourcing, or HR/people role — false for everything else (including roles that merely mention "cross-functional hiring involvement" as a minor duty).

When true:
  - Within playbook_csm and playbook_partnerships specifically, source facts about recruiting/hiring/sourcing/candidate screening are the most relevant facts available — put their rewritten bullets first in each role's bullet list, ahead of the other bullets for that role, still subject to the normal character-length rules.
  - Use each role's "or N bullets if recruiting_focus is true" target where one is given (see the fact list's per-role target note) instead of its normal target — this currently only applies to playbook_partnerships (5 instead of 3), so all of its facts get shown rather than trimmed.
  - Do not fabricate recruiting facts for any other role — only playbook_csm and playbook_partnerships have recruiting-related source facts to draw on.
</recruiting_focus>

<computer_skills_selection>
"skill_groups" should be the default skill lines, unchanged (same labels, same order, same items), for most postings. Only deviate when the posting names specific tools that appear in the full pool but not the defaults (e.g. a design-heavy posting mentioning Figma) — in that case swap those pool items into the best-fitting group, placing the most posting-relevant skills first within each group. Keep every label exactly as given, keep each line about as long as its default (each must fit on one printed line — under ~115 characters including the label), never add anything that isn't in the pool, and never drop the skills the posting is actually asking about in favor of ones it isn't.
</computer_skills_selection>

Respond in the required JSON format only — no commentary outside the schema fields."""


def _generate_bullets(master, bank, title, company, location, description):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the Vercel project's environment variables")

    # A single attempt at the current prompt (6 roles, each with a reasoning
    # field plus bullets) can genuinely take close to a minute; the SDK's
    # default max_retries=2 used to turn one slow-but-real generation into
    # three, stacking to ~2.5min before giving up (seen in prod: a 502 after
    # 169s against the old 55s timeout). Retrying doesn't help when the call
    # isn't flaky, it's just slow, so give it more per-attempt headroom and
    # fail fast after one retry instead of three.
    client = Anthropic(api_key=api_key, timeout=110.0, max_retries=1)
    user_content = _build_prompt(master, bank, title, company, location, description)

    response = client.messages.create(
        model=MODEL,
        # Reasoning fields for all 6 roles (added alongside bullets so the
        # model has a place to work out fact selection) push completions
        # much closer to the ceiling than the visible JSON alone suggests --
        # 6000 was tuned before those fields existed and was silently
        # truncating mid-string under `effort: medium`, which produces
        # unparseable JSON (seen in prod as "Unterminated string..." and,
        # when the cut lands before any text content exists, a bare/empty
        # exception from the `next()` below finding no text block at all).
        max_tokens=12000,
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": RESUME_SCHEMA},
        },
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude's response was cut off before finishing (hit max_tokens) -- "
            "the generated resume was incomplete, not a formatting bug. Try again, "
            "and if it keeps happening, max_tokens needs to go up further."
        )
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("Claude's response had no text content to parse.")
    result = json.loads(text)
    # Enforced here rather than left to the model: recruiting-focused resumes
    # always show the Technical Projects section (LTX Ventures is dropped
    # whenever projects show, and Parking Mandate Atlas is dropped on
    # recruiting-focused ones -- see _render_resume_html).
    if result.get("recruiting_focus"):
        result["optional_block"] = "projects"
    return result


def _sep():
    return '<span class="sep"> &middot; </span>'


def _row(left_html, right_text, cls):
    right = f'<span class="right">{html.escape(right_text)}</span>' if right_text else ""
    return f'<div class="row {cls}"><span class="left">{left_html}</span>{right}</div>'


def _bullet_list(bullets):
    return "<ul>" + "".join(f"<li>{html.escape(b)}</li>" for b in bullets) + "</ul>"


def _pick_skill_groups(add, tailored):
    """Default skill_groups from master_resume.yaml, with any model swaps
    validated: labels must match a default group, items must be in the pool
    (or that group's defaults), and each line must stay short enough to fit
    on one printed line. Anything that fails falls back to the default."""
    defaults = add["skill_groups"]
    allowed = set(add["computer_skills_pool"]) | {i for g in defaults for i in g["items"]}
    picked = {g.get("label"): g.get("items") or [] for g in (tailored.get("skill_groups") or []) if isinstance(g, dict)}
    out = []
    for g in defaults:
        items = [i for i in picked.get(g["label"], []) if i in allowed]
        # Drop trailing items until the line fits (label + items).
        while items and len(g["label"]) + 2 + len(", ".join(items)) > SKILL_LINE_MAX_CHARS:
            items.pop()
        out.append({"label": g["label"], "items": items or g["items"]})
    return out


def _render_resume_html(master, tailored, job_title, job_company):
    """Layout mirrors resume/Caleb_Pratt_Resume.pdf (the 2026-11 version):
    sizes, weights and spacing below were measured from that PDF with
    pdfplumber -- see SKILL.md "Resume tailoring" before changing them."""
    h = master["header"]
    contact_bits = [f'<a href="mailto:{html.escape(h["email"])}">{html.escape(h["email"])}</a>', html.escape(h["phone"])]
    if h.get("linkedin"):
        contact_bits.append(f'<a href="https://{html.escape(h["linkedin"])}">{html.escape(h["linkedin"])}</a>')
    parts = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(h['name'])} — Resume ({html.escape(job_company)})</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Carlito:ital,wght@0,400;0,700;1,400;1,700&display=block" rel="stylesheet">
<style>
  @page {{ size: letter; margin: 0.3in 0.5in; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: Calibri, Carlito, sans-serif; color: #000;
    font-size: 10pt; line-height: 12pt; max-width: 8.5in; margin: 0 auto; padding: 0.3in 0.5in;
  }}
  .toolbar {{
    display: flex; justify-content: flex-end; gap: 10px; margin-bottom: 14px;
    font-family: -apple-system, sans-serif;
  }}
  .toolbar button {{
    font: inherit; font-size: 13px; padding: 7px 14px; border-radius: 6px;
    border: 1px solid #1F51D6; background: #1F51D6; color: #fff; cursor: pointer;
  }}
  a {{ color: inherit; text-decoration: none; }}
  h1 {{ text-align: center; font-size: 15pt; line-height: 18pt; font-weight: bold; margin: 0; }}
  .contact {{ text-align: center; margin: 1pt 0 0; }}
  .sep {{ font-weight: bold; font-size: 12pt; }}
  h2 {{
    font-size: 10.5pt; line-height: 13pt; font-weight: bold; text-transform: uppercase;
    border-bottom: 0.75pt solid #000; padding-bottom: 0; margin: 6pt 0 4pt;
  }}
  .row {{ display: flex; justify-content: space-between; align-items: baseline; gap: 12pt; }}
  .row .right {{ white-space: nowrap; font-weight: normal; font-style: normal; font-size: 10pt; }}
  .company {{ font-size: 11pt; line-height: 13pt; font-weight: bold; }}
  .company .right {{ font-size: 11pt; font-weight: bold; }}
  .role {{ font-size: 11pt; line-height: 13pt; font-style: italic; }}
  .item {{ font-weight: bold; }}
  .item i {{ font-weight: normal; }}
  .subrole {{ font-style: italic; }}
  .entry + .entry {{ margin-top: 5pt; }}
  ul {{ list-style: none; margin: 0; padding: 0; }}
  li {{ position: relative; padding-left: 12pt; }}
  li::before {{ content: "\\25CF"; position: absolute; left: 0; font-family: Arial, sans-serif; font-size: 10pt; line-height: 12pt; }}
  .skills b {{ font-weight: bold; }}
  @media print {{
    .toolbar {{ display: none; }}
    body {{ padding: 0; }}
  }}
</style>
</head>
<body>
<div class="toolbar"><button onclick="window.print()">Print / Save as PDF</button></div>
<h1>{html.escape(h['name'])}</h1>
<p class="contact">{_sep().join(contact_bits)}</p>""")

    recruiting_focus = tailored.get("recruiting_focus")
    optional_block = tailored.get("optional_block", "none")
    projects_shown = optional_block == "projects"

    parts.append("<h2>Experience</h2>")
    for company in master["experience"]:
        if projects_shown and company.get("drop_when_projects_shown"):
            continue
        parts.append('<div class="entry">')
        parts.append(_row(html.escape(company["company"]), company["location"], "company"))
        for role in company["roles"]:
            bullets = tailored.get(f"{role['id']}_bullets") or role["bullets"]
            role_title = role["title"] or company["subtitle"]
            if recruiting_focus and role.get("title_recruiting"):
                role_title = role["title_recruiting"]
            start = role.get("start") or company["start"]
            end = role.get("end") or company["end"]
            parts.append(_row(html.escape(role_title), f"{start} – {end}", "role"))
            parts.append(_bullet_list(bullets))
        parts.append("</div>")

    if projects_shown:
        parts.append("<h2>Technical Projects</h2>")
        for entry in master["optional_blocks"]["projects"]["entries"]:
            if recruiting_focus and entry.get("drop_for_recruiting_focus"):
                continue
            left = (
                f'{html.escape(entry["name"])} &ndash; <i>{html.escape(entry["tagline"])}</i>'
                f'{_sep()}<i>{html.escape(entry["link"])}</i>'
            )
            parts.append('<div class="entry">' + _row(left, entry["date"], "item") + _bullet_list(entry["bullets"]) + "</div>")

    parts.append("<h2>Education</h2>")
    for edu in master["education"]:
        parts.append(
            '<div class="entry">'
            + _row(html.escape(edu["school"]), edu["date"], "item")
            + f'<div>{html.escape(edu["degree"])}</div>'
            + f'<div>Minor: {html.escape(edu["minor"])} &middot; {html.escape(edu["honors"])}</div>'
            + (f'<div>Coursework: {html.escape(edu["coursework"])}</div>' if edu.get("coursework") else "")
            + "</div>"
        )

    parts.append("<h2>Undergraduate Leadership Experience and Activities</h2>")
    leadership = list(master["leadership"]["entries"])
    if optional_block == "tech_business_fellows":
        leadership += master["optional_blocks"]["tech_business_fellows"]["entries"]
    for entry in leadership:
        parts.append('<div class="entry">' + _row(f'{html.escape(entry["org"])} &ndash; {html.escape(entry["role"])}', entry["dates"], "item"))
        for sub in entry.get("subroles", []):
            parts.append(f'<div class="subrole">{html.escape(sub["title"])} ({html.escape(sub["dates"])})</div>' + _bullet_list(sub["bullets"]))
        if entry.get("bullets"):
            parts.append(_bullet_list(entry["bullets"]))
        parts.append("</div>")

    parts.append('<h2>Additional Information</h2><div class="skills">')
    for g in _pick_skill_groups(master["additional"], tailored):
        parts.append(f'<div><b>{html.escape(g["label"])}:</b> {html.escape(", ".join(g["items"]))}</div>')
    parts.append("</div>")
    parts.append(f"<!-- tailored for: {html.escape(job_title)} @ {html.escape(job_company)} -->")
    parts.append("</body></html>")
    return "\n".join(parts)


def _error_page(status_text, message, detail=None):
    detail_html = (
        f'<p style="color:#888;font-size:0.85em;margin-top:24px">{html.escape(detail)}</p>' if detail else ""
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Resume generation failed</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:80px auto;color:#333">
<h1 style="color:#c0392b">{html.escape(status_text)}</h1>
<p>{html.escape(message)}</p>
{detail_html}
</body></html>"""


def _friendly_api_error(e):
    """Translate a raw Anthropic SDK exception into a plain-language message.

    The SDK's own str(e) is a debugging dump (raw JSON body, request id) --
    fine in the Vercel logs (still printed there), useless to a human reading
    the error page. Falls back to a generic message plus the raw text for
    anything not recognized, rather than hiding it.
    """
    body = getattr(e, "body", None)
    error_type = (body or {}).get("error", {}).get("type") if isinstance(body, dict) else None
    status_code = getattr(e, "status_code", None)

    if error_type == "overloaded_error" or status_code == 529:
        return (
            "Claude is temporarily overloaded",
            "Anthropic's API is at capacity right now. This isn't specific to this "
            "job or account -- wait a minute or two and click \"Tailor resume\" again.",
            None,
        )
    if isinstance(e, RateLimitError) or status_code == 429:
        return (
            "Rate limited",
            "Too many requests have hit the Claude API too quickly. Wait a bit before trying again.",
            None,
        )
    if error_type == "invalid_request_error" and "credit balance" in str(e).lower():
        return (
            "Anthropic account out of credits",
            "The Anthropic account behind this key has run out of credit balance. "
            "Add credits at console.anthropic.com/settings/billing, then try again.",
            None,
        )
    if isinstance(e, AuthenticationError) or status_code == 401:
        return (
            "Claude API key rejected",
            "ANTHROPIC_API_KEY is missing or invalid. Check the key in the Vercel "
            "project's environment variables (this isn't something retrying fixes).",
            None,
        )
    if isinstance(e, PermissionDeniedError) or status_code == 403:
        return (
            "Claude API permission denied",
            "The API key doesn't have permission for this request (e.g. no access "
            "to the configured model). Check the key's permissions in the Anthropic Console.",
            None,
        )
    if error_type == "invalid_request_error":
        return (
            "Malformed request to Claude",
            "The request sent to Claude was rejected as invalid -- this points to a bug "
            "in the resume generator itself (e.g. the schema or prompt), not something "
            "retrying will fix. See the detail below and check the Vercel logs.",
            str(e),
        )
    if isinstance(e, APITimeoutError):
        return (
            "Claude request timed out",
            "Generation took too long and the request timed out. Try again -- if this "
            "keeps happening, the timeout or the prompt may need adjusting.",
            None,
        )
    if isinstance(e, APIConnectionError):
        return (
            "Couldn't reach Claude",
            "A network error prevented the request from reaching Anthropic's API. Try again.",
            None,
        )
    if status_code is not None and status_code >= 500:
        return (
            "Claude API internal error",
            "Anthropic's API had an internal error unrelated to this request. Try again shortly.",
            None,
        )
    return (
        "Claude API error",
        "An unexpected error came back from the Claude API. Try again; if it keeps "
        "happening, check the Vercel logs for the full detail below.",
        str(e),
    )


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
            status_text, message, detail = _friendly_api_error(e)
            self._send(502, _error_page(status_text, message, detail))
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
