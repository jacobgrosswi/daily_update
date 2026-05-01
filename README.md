# Daily Update

Automated daily email briefing delivered at 6:00 AM Central time. Runs entirely on GitHub Actions — no local machine required.

Sections:
- Email summary (people / orders & shipping / appointments)
- Sports scores (Brewers, Bucks, Packers + NBA/NFL playoffs)
- Market update (S&P 500, Nasdaq, Dow, 10-yr Treasury)
- Curated top 5 AI news items from 5 newsletters

See `Daily Update Automation: Scoping Doc.md` for full design.

## Local development

```bash
uv sync
cp .env.example .env
# fill in .env with your dev secrets
uv run python -m src.main --dry-run
```

## Modes

- `--dry-run` — compose briefing and print to stdout; no email send, no archive write
- `--test` — use fixture data only; no external API calls

## Production deployment

The daily briefing runs from `.github/workflows/daily_update.yml` on a 6 AM Central cron. Two cron entries cover both daylight-saving offsets; a gate step exits early when the local Central hour isn't 06, so only one of the two firings does work each day.

### Required repo secrets

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API auth |
| `MS_CLIENT_ID` | Microsoft Graph app ID |
| `MS_CLIENT_SECRET` | Microsoft Graph app secret |
| `MS_REFRESH_TOKEN` | Long-lived Graph auth (rotates — see below) |
| `BALLDONTLIE_API_KEY` | NBA scores (free tier) |
| `BRIEFING_RECIPIENT` | Where the briefing is sent |
| `GH_PAT` | Fine-grained PAT with `secrets:write` on this repo. Used to update `MS_REFRESH_TOKEN` after Microsoft rotates it. |

### Refresh token rotation

Personal Microsoft account refresh tokens rotate on every use. The workflow detects rotation via `REFRESH_TOKEN_OUT_PATH` (the orchestrator writes the new token to this file only when MSAL hands back a different value), then runs `gh secret set MS_REFRESH_TOKEN` using `GH_PAT`. Without `GH_PAT`, day 2 will fail with `invalid_grant`.

To create `GH_PAT`: GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens. Resource access: this repo only. Permissions: `Secrets: Read and write` (also read on Metadata, which is required by default).

### Manual run

The workflow has a `workflow_dispatch` trigger with a `dry_run` input — useful for verifying setup without sending email or committing archive/state.

## Feedback loop & weekly tune-up

The briefing has two preference-editing paths:

- **Continuous feedback** runs every morning before composing. It reads new
  replies to prior briefings (subject `Re: Daily Briefing - ...`), asks Claude
  Haiku to translate each reply into a narrow ops vocabulary
  (`set_top_n`, `add_drop_sender`, `add_bucket_keyword`, `add_curation_rule`,
  `set_paused`), and applies validated ops to `config/preferences.yml`. The
  next briefing renders an "Applied your feedback" block at the top showing
  what changed. Anything outside the vocabulary is collected in a
  "Needs tune-up" sub-section. Replies are tracked in
  `state/processed_replies.json` to prevent re-processing.

- **Weekly tune-up** is a `workflow_dispatch`-only workflow
  (`.github/workflows/weekly_tuneup.yml`). It feeds Claude Sonnet 4.6 (with
  adaptive thinking) the last 7 days of archived briefings plus the current
  preferences, and Sonnet proposes an updated `preferences.yml`. The workflow
  opens a PR for manual review — it does NOT auto-merge.

## Microsoft Graph OAuth setup

See SCOPING doc Section 7 for the App Registration walkthrough. After registering the app and getting client ID/secret, run `scripts/get_refresh_token.py` once locally to mint the initial `MS_REFRESH_TOKEN`.
