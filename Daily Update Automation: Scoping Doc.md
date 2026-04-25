# Daily Briefing Automation: Scoping Document

**Owner:** Jacob
**Last updated:** April 24, 2026
**Status:** Scoped, ready to build

## 1. Purpose

A fully automated daily email briefing that runs on GitHub Actions, independent of any local machine, delivered at 6:00 AM Central time to `jacobgrosswi@outlook.com`. The briefing summarizes the previous reporting window's email activity, sports results for specific teams, market movement for specific indices, and coalesced AI news from five newsletters. It learns over time via email replies and a manually triggered weekly tune-up.

## 2. Architecture Overview

```
GitHub Actions (cron: 6 AM CT daily)
        |
        v
Python 3.12 script (uv-managed)
        |
        +--> Microsoft Graph API (read email, send email)
        +--> MLB Stats API (Brewers)
        +--> balldontlie.io (Bucks + NBA playoffs)
        +--> TheSportsDB (Packers + NFL playoffs)
        +--> yfinance (market indices)
        +--> Anthropic API (Haiku 4.5 default, Sonnet 4.6 for weekly tune-up)
        |
        v
HTML email (rendered from Markdown) -> Outlook inbox
Archive copy -> private GitHub repo at archive/YYYY-MM-DD.md
```

All state lives in the repo. No external database. Secrets live in GitHub Actions Secrets for production and a gitignored `.env` for local development.

## 3. Repo Structure

```
daily_update/                      (private GitHub repo)
├── .github/
│   └── workflows/
│       ├── daily_update.yml      (cron: 6 AM CT daily)
│       ├── weekly-tuneup.yml      (manual trigger only)
│       └── token-health.yml       (weekly refresh token check)
├── src/
│   ├── main.py                    (orchestrator)
│   ├── email_client.py            (Graph API wrapper)
│   ├── sports.py                  (3 sports API clients + playoff filter)
│   ├── markets.py                 (yfinance + material premarket check)
│   ├── newsletters.py             (filter + Claude curation)
│   ├── feedback.py                (parse replies, update preferences)
│   ├── claude_client.py           (Anthropic API wrapper)
│   ├── delivery.py                (markdown -> HTML -> Graph send)
│   ├── budget.py                  (per-run token cost guardrail)
│   └── utils.py                   (timezone, logging, state)
├── config/
│   ├── preferences.yml            (curation criteria, editable)
│   ├── tickers.yml                (4 indices)
│   ├── teams.yml                  (Brewers, Bucks, Packers)
│   └── newsletters.yml            (5 AI newsletter senders)
├── archive/                       (pruned to last 21 days)
│   └── 2026-04-25.md
├── state/
│   └── last_run.json              (timestamp of last successful run)
├── tests/
│   └── (unit tests for each module)
├── .env.example
├── .gitignore
├── pyproject.toml                 (uv-managed)
├── README.md                      (setup walkthrough)
└── SCOPING.md                     (this document)
```

## 4. Briefing Content Sections

### 4.1 Email Summary

**Window:** From last successful run timestamp to current run start. Captures weekends and any gap. Stored in `state/last_run.json`.

**Filtering logic:**

1. Fetch all messages from Outlook inbox in the window via Graph API.
2. Drop automated mail:
   - Messages with `List-Unsubscribe` header
   - Senders matching `noreply@`, `no-reply@`, `donotreply@`, `notifications@`, `mailer-daemon@`
   - Senders in `config/newsletters.yml` (handled separately)
3. Bucket remaining messages:
   - **People:** human senders (default bucket)
   - **Orders and shipping:** keyword + sender match (amazon, ups, fedex, usps, dhl, shopify, rockauto, plus keywords "shipped", "delivered", "out for delivery", "tracking")
   - **Appointments:** keyword + sender match (calendly, outlook.office.com calendar, google calendar, plus keywords "appointment", "meeting confirmation", "reschedule", "reminder")
   - **Ambiguous:** passed to Claude for final bucketing
4. Claude summarizes each message (1-2 sentences) and presents by bucket.

**Privacy:** Anthropic API traffic is not used for training per default API terms. See https://www.anthropic.com/legal/commercial-terms.

**Output format:**

```
People
  - [Sender Name] - [subject line]
    Summary sentence.

Orders & Shipping
  - [Sender] - [subject]
    Summary sentence.

Appointments
  - [Sender] - [subject]
    Summary sentence.
```

### 4.2 Sports Scores

**Teams and scope:**

| Team | API | Scope |
|---|---|---|
| Milwaukee Brewers | MLB Stats API (statsapi.mlb.com, no key) | Any game from yesterday |
| Milwaukee Bucks | balldontlie.io (no key needed for basic) | Any game from yesterday |
| Green Bay Packers | TheSportsDB (no key) | Any game from yesterday |
| NBA playoffs (all teams) | balldontlie.io | Postseason games only |
| NFL playoffs (all teams) | TheSportsDB | Postseason games only |

**Off-season behavior:** If a team has no scheduled games for the prior day and no games in the upcoming week, omit the team from the section entirely. If a league has no playoffs running, omit that sub-section.

**Playoff detection:**
- NBA: balldontlie returns `postseason: true` on playoff games
- NFL: TheSportsDB game endpoints include a round/stage field that identifies wildcard, divisional, conference, Super Bowl

**Output format:**

```
Brewers 5, Cubs 3 (final)
  Winning pitcher: [name]. Decisive play/inning.

Bucks vs [opponent]: no game yesterday (next: [date])

NBA Playoffs
  Celtics 112, Heat 108 (Eastern Conference First Round, Game 3)
  Nuggets 98, Lakers 102 (Western Conference First Round, Game 2)

NFL Playoffs
  (none yesterday)
```

### 4.3 Market Update

**Indices tracked:** S&P 500 (^GSPC), Nasdaq Composite (^IXIC), Dow Jones Industrial (^DJI), 10-year Treasury yield (^TNX).

**Data:** Previous trading day's close via `yfinance`. On Mondays or after holidays, show the last actual trading day and label the date.

**Format:** Dollar change and percent change from previous close.

**Pre-market check:** If any of the 4 indices shows >1% movement in pre-market trading as of briefing generation (6 AM CT, so about 30 min into pre-market), include a "Pre-market note" callout. Otherwise omit.

**Output format:**

```
Market Close - Wednesday, April 23, 2026

S&P 500       5,487.21    +12.45   (+0.23%)
Nasdaq       17,234.88    -45.12   (-0.26%)
Dow          38,912.44    +89.33   (+0.23%)
10-yr Tsy        4.42%    +0.03 bps

Pre-market note: S&P 500 futures down 1.2% on [headline].
```

### 4.4 Newsletter Summary

**Newsletters tracked:** AI Breakfast, Ben's Bites, TLDR AI, Superhuman AI, The Rundown. Sender addresses captured in `config/newsletters.yml` after first run observes them.

**Listing:** Top of section lists which newsletters were received in the window, by sender and subject. No body summary per newsletter.

**Curation:** Claude ingests all newsletter bodies, deduplicates overlapping stories, and selects **top 5 items across all newsletters**. Each item is 2-7 sentences. Prioritization rules (from `preferences.yml`):

1. Items with explicit impact on finance, FP&A, or accounting are boosted.
2. Items repeated across 3+ newsletters are boosted (signal of importance).
3. Novel items with a finance/FP&A/accounting angle beat widely-covered general AI news.
4. Model releases, major funding, regulatory news, and enterprise AI adoption stories are the default "broad AI news" buckets.

**Output format:**

```
Newsletters Received
  - AI Breakfast: [subject]
  - TLDR AI: [subject]
  - (etc.)

Top 5 AI Stories

1. [Headline or topic]
   2-7 sentence summary. Finance/FP&A angle called out if relevant.
   Sources: AI Breakfast, TLDR AI

2. [...]
```

## 5. Models and Cost

| Use case | Model | Rationale |
|---|---|---|
| Daily email bucketing + summarization | Claude Haiku 4.5 | Cost efficient, fast, sufficient for classification |
| Daily newsletter curation | Claude Haiku 4.5 | Same |
| Ambiguous email edge cases | Claude Haiku 4.5 | Same |
| Weekly tune-up (when triggered) | Claude Sonnet 4.6 | Reasoning quality matters for preference refinement |
| Feedback reply parsing | Claude Haiku 4.5 | Structured extraction |

**Budget target:** $5/month cap in Anthropic Console.

**Expected spend:** ~$1.50-2.50/month for daily runs on Haiku, plus occasional weekly tune-up at ~$0.20-0.50 per invocation. Comfortable headroom.

**Per-run guardrail:** `src/budget.py` estimates token cost before calling Claude. If a single run projects over $0.25, it truncates newsletter bodies first, and aborts with a warning email if still over budget.

## 6. Scheduling

**Daily briefing:**
- 6:00 AM Central Time, 7 days a week
- Cron in UTC with timezone handling inside the script to account for daylight saving
- Workflow file: `.github/workflows/daily_update.yml`
- Concurrency lock so overlapping runs cannot occur

**Weekly tune-up:**
- Manual trigger only via GitHub Actions UI (`workflow_dispatch`)
- Not scheduled
- Workflow file: `.github/workflows/weekly-tuneup.yml`
- Pulls last 7 days of archive + replies, proposes preference updates via a pull request for review

**Token health check:**
- Weekly, Sundays at noon CT
- Workflow file: `.github/workflows/token-health.yml`
- Tests Microsoft Graph refresh token, alerts if nearing expiry or failed

**Vacation pause:**
- `paused: true` in `preferences.yml` skips briefing generation and Claude calls
- Workflow still runs (costs nothing), just no-ops with a log line

## 7. Microsoft Graph OAuth Setup (Path A)

One-time setup. Full walkthrough in README, summarized here.

**Steps:**

1. Go to Azure Portal App Registrations at https://entra.microsoft.com and create a new app registration.
2. Name it `daily_update`. Supported account types: "Personal Microsoft accounts only" (for consumer Outlook).
3. Set redirect URI to `http://localhost:8000/callback` (type: Web). Used once for initial consent.
4. Note the Application (client) ID.
5. Under "Certificates & secrets" create a new client secret. Note the value immediately, it is only shown once.
6. Under "API permissions" add delegated Microsoft Graph permissions:
   - `Mail.Read`
   - `Mail.Send`
   - `offline_access` (required for refresh tokens)
7. Run the local helper script `scripts/get_refresh_token.py` once. It opens a browser, completes consent, prints the refresh token.
8. Store in GitHub Secrets:
   - `MS_CLIENT_ID`
   - `MS_CLIENT_SECRET`
   - `MS_REFRESH_TOKEN`
9. Daily run uses refresh token to get a fresh access token, no interactive login.

**Refresh token behavior:** Personal Microsoft account refresh tokens generally last 90 days of inactivity. Daily use keeps them rolling. The token-health workflow monitors and alerts.

## 8. Feedback Loop

**Reply-to-feedback (continuous):**

1. Each briefing email has subject `Daily Briefing - YYYY-MM-DD` and a hidden header `X-Briefing-ID: YYYY-MM-DD`.
2. The next day's run scans for replies to `jacobgrosswi@outlook.com` -> `jacobgrosswi@outlook.com` threads matching that subject pattern.
3. Reply bodies are passed to Claude (Haiku) with the prompt: "The user is giving feedback on yesterday's briefing. Convert their feedback into specific changes to `preferences.yml`, or return no-op if ambiguous."
4. Proposed changes are applied to `preferences.yml` and committed by the Action with message `chore(prefs): apply feedback from YYYY-MM-DD`.
5. A small section in the next briefing confirms: "Applied your feedback: [summary of change]."

**Weekly tune-up (on demand):**

1. Triggered manually from GitHub Actions UI.
2. Pulls last 7 days from `archive/` + any feedback applied in that window.
3. Claude (Sonnet 4.6) analyzes patterns: which sections are you engaging with, which feedback themes have repeated, what curation rules could be added.
4. Opens a pull request against `preferences.yml` with proposed refinements and a summary of rationale.
5. You review and merge (or close) the PR.

## 9. Failure Handling

**Partial failure policy:** Send the briefing anyway. Each section that fails is replaced with `Section unavailable: [reason]`. A compact "Issues" footer lists failures with enough detail to debug.

**Full failure policy:** If the briefing itself cannot be composed or sent (e.g., Graph send fails), write a GitHub Actions job failure log and the token-health workflow will catch persistent auth issues. No secondary alert channel for now.

**Retry policy:** Each API call retries up to 3 times with exponential backoff before marking the section failed.

## 10. Archive and Retention

**Archive:** Every successful briefing is committed to `archive/YYYY-MM-DD.md` in the repo as the raw Markdown that was emailed.

**Retention:** A cleanup step in the daily workflow deletes archive files older than 21 days. Committed as `chore(archive): prune old briefings`.

**Rationale for 3 weeks:** Enough history for weekly tune-ups to see patterns, not so much that personal email summaries accumulate indefinitely.

## 11. Secrets Inventory

Stored in GitHub Actions Secrets (production) and `.env` locally (dev, gitignored).

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API auth |
| `MS_CLIENT_ID` | Microsoft Graph app ID |
| `MS_CLIENT_SECRET` | Microsoft Graph app secret |
| `MS_REFRESH_TOKEN` | Long-lived Graph auth |
| `GITHUB_TOKEN` | Auto-provided by Actions for repo commits |

No market data or sports API keys needed. All chosen providers offer free tiers without auth for this volume.

## 12. Local Development

**Setup:**

```bash
git clone git@github.com:jacobgrosswi/daily_update.git
cd daily_update
uv sync
cp .env.example .env
# fill in .env with personal dev secrets
uv run python -m src.main --dry-run
```

**Dry-run mode:** `--dry-run` flag composes the briefing and prints to stdout instead of sending email or committing to archive. Useful for iterating on prompts.

**Test mode:** `--test` runs with fixture data only (no external APIs). Useful for CI and prompt regression testing.

## 13. Build Sequence

Recommended order when implementing:

1. Repo scaffolding, `pyproject.toml`, `.env.example`, `.gitignore`
2. `utils.py` (logging, timezone, state file I/O)
3. `claude_client.py` (minimal wrapper, dry-run first)
4. `email_client.py` + OAuth helper script
5. `markets.py` (simplest section, quick win)
6. `sports.py` (3 APIs, more surface area)
7. `newsletters.py` (depends on Claude client)
8. `delivery.py` (Markdown -> HTML -> Graph send)
9. `main.py` orchestrator wiring all sections
10. `.github/workflows/daily_update.yml` and production deployment
11. `feedback.py` + `.github/workflows/weekly-tuneup.yml`
12. `budget.py` and cost guardrails
13. `.github/workflows/token-health.yml`
14. Archive pruning logic

Each step tested locally with `--dry-run` before the next.

## 14. Open Items / Future Enhancements

Not in scope for v1, worth noting:

- Push to Slack/Discord in addition to email
- Weather snippet for Milwaukee area
- Calendar digest (today's appointments) via Graph Calendar API
- FP&A job market scan (Indeed/LinkedIn via RSS or scraper)
- Personal finance dashboard pull (hledger balances) since you're already tracking
- Automatic screenshot capture of the briefing for archival/searchability

## 15. Success Criteria for v1

- Briefing arrives in Outlook inbox daily at 6:00 AM CT without manual intervention
- All 4 sections populate correctly on a weekday with active news, sports, and newsletters
- Reply-to-feedback loop successfully updates preferences within one cycle
- Weekly tune-up opens a reviewable PR when triggered
- Monthly Anthropic spend stays under $5
- Laptop is fully off during all of the above