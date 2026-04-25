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

## Setup walkthrough

OAuth and GitHub Actions setup steps will be filled in as those modules land. See SCOPING doc Section 7 for the Microsoft Graph App Registration outline.
