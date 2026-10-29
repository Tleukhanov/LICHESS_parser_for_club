# LICHESS_parser_for_club

Tooling for outreach to a Lichess club: collect candidate players from a public club,
rank them by rating and activity, generate personalized invitation messages with an LLM
(OpenRouter), let a human approve them in a small mobile web UI, then send the DMs
through the Lichess API while tracking results and reactions.

This repository currently contains only the project skeleton: configuration, database
schema and a health endpoint. The Lichess client, parser, LLM layer, review UI,
rate-limited sender and analytics are added in later steps.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env    # fill in the secrets
.venv/bin/uvicorn app:app --reload
```

Then open http://127.0.0.1:8000/health.

Run the checks:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `LICHESS_TOKEN` | empty | Personal Lichess API token used for club reads and DMs |
| `LICHESS_USERNAME` | empty | Lichess account that sends the invitations |
| `SENPAI_TEAM_ID` | empty | Target team id the candidates are invited to |
| `SOURCE_CLUB_ID` | empty | Id of the Lichess club being parsed |
| `LLM_API_KEY` | empty | OpenRouter API key |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter-compatible base URL |
| `LLM_MODEL` | `openai/gpt-4o-mini` | Model used to write invitation messages |
| `DATABASE_URL` | `sqlite:///./data/outreach.db` | SQLAlchemy database URL (`postgres://` is normalized to `postgresql://`) |
| `SEND_INTERVAL_SECONDS` | `45` | Delay between two outgoing DMs |
| `DAILY_SEND_LIMIT` | `30` | Maximum DMs per day |
| `MIN_RAPID` | `0` | Lower bound of the rapid rating filter |
| `MAX_RAPID` | `9999` | Upper bound of the rapid rating filter |
| `ADMIN_TOKEN` | empty | Token protecting the admin actions of the review UI |

Copy `.env.example` to `.env` and fill in the values; `.env` is git-ignored.

## Project structure

```
app.py                     FastAPI app, health endpoint, startup hook
backend/
  config.py                pydantic-settings configuration
  db/
    models.py              SQLAlchemy Core tables, status constants
    database.py            engine, session factory, init_db, get_session
    repository.py          session-based CRUD helpers
tests/
  test_scaffold.py         schema, repository and health endpoint tests
data/                      local sqlite database (git-ignored)
```
