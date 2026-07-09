# Job Radar

A small tool that checks for job postings matching your criteria every morning
and publishes them to a dashboard. It pulls from:

- **Specific companies** you name, via their public career boards (Greenhouse, Lever, Ashby) — free, no key.
- **A broad search across job boards**, via the Adzuna API — free key required.

New postings since the previous run are highlighted at the top of the dashboard.

---

## What you edit

Only `config.yaml`. Set your keywords, location, salary floor, and the companies
you want to track. Comments in that file explain each field.

---

## One-time setup (runs free, in the cloud)

**1. Get a free Adzuna API key** (for the broad search).
Register at https://developer.adzuna.com/ — you'll get an **App ID** and an **App Key**.
(You can skip this and set `adzuna: enabled: false` if you only want to track named companies.)

**2. Put this folder in a new GitHub repository.**
Create a repo, then upload these files (or `git push` them).

**3. Add your Adzuna keys as repository secrets.**
In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add two:
- `ADZUNA_APP_ID`
- `ADZUNA_APP_KEY`

They stay encrypted and never appear in the code or dashboard.

**4. Turn on the dashboard (GitHub Pages).**
In the repo: **Settings → Pages → Build and deployment → Source: Deploy from a branch**,
branch `main`, folder `/docs`. Your dashboard will live at
`https://YOUR-USERNAME.github.io/YOUR-REPO/`.

**5. Run it once to confirm it works.**
Go to the **Actions** tab → **Job Radar** → **Run workflow**. It fetches jobs,
filters them, and commits an updated `docs/index.html`. Open your Pages URL to see it.

After that it runs automatically each morning on the schedule in
`.github/workflows/job-check.yml`.

---

## Adjusting the time

The schedule uses cron in **UTC**. The default `0 11 * * *` is roughly 6–7am US Eastern.
Change the hour in the workflow file to suit your timezone. (Scheduled runs can be
delayed a few minutes during busy periods — that's a GitHub quirk, not a bug.)

---

## Running it locally instead (optional)

If you'd rather run it on your own machine:

```bash
pip install -r requirements.txt
export ADZUNA_APP_ID=your_id
export ADZUNA_APP_KEY=your_key
python job_check.py
open docs/index.html    # the dashboard
```

On a Mac/Linux machine you can schedule it with cron; on Windows, Task Scheduler.
The catch is it only runs when your computer is awake — which is why the cloud
setup above is usually easier.

---

## Finding a company's board token

The token is the last part of the company's careers URL:

| ATS | URL looks like | token |
|-----|----------------|-------|
| Greenhouse | `boards.greenhouse.io/stripe` | `stripe` |
| Lever | `jobs.lever.co/netflix` | `netflix` |
| Ashby | `jobs.ashbyhq.com/ramp` | `ramp` |

Not every company uses one of these three, but a large share of tech and
venture-backed companies do. If a company isn't on one of them, the broad
Adzuna search will usually still surface its roles.
