# LICHESS_parser_for_club

A small, human-in-the-loop outreach tool for a Lichess club.

It parses the members of a source Lichess club, ranks them by rating, asks an LLM
(OpenRouter by default) to write a short personalized invitation, lets a human review
and approve every single message in a mobile-friendly web UI, sends the approved
messages through the Lichess API with pacing and a daily cap, and finally tracks who
actually joined the target team. The collected data can be exported as a CSV dataset.

Nothing is sent automatically: a message only leaves the server after a human approves
it (or explicitly presses "Approve & send" for that row).

## Architecture / pipeline

```
                 Lichess API                          local state
┌────────────────────────────────────┐      ┌─────────────────────────────┐
│ GET  /api/team/{club}/users        │      │ SQLite (local) or Postgres  │
│ POST /api/users  (chunks of 300)   │ ───► │ candidates, outreach        │
└────────────────────────────────────┘      └─────────────────────────────┘
             │                                       ▲        │
             │ nicks + ratings                       │        │
             ▼                                       │        ▼
   filter (min/max rating, min games,          review UI (FastAPI + static
   exclude own team) → rank by rating          frontend, X-Admin-Token)
             │                                       │
             ▼                                       │ approve / edit
   LLM draft (OpenRouter chat completions)  ─────────┘
   fallback template if the LLM fails
             │
             ▼
   sender: POST /inbox/{username}
   pacing (SEND_INTERVAL_SECONDS), daily cap (DAILY_SEND_LIMIT),
   stop the batch on HTTP 429
             │
             ▼
   analytics: GET /api/team/{team}/users
   → mark joined → CSV export
```

Step by step:

1. **Parse** — `backend/parser` downloads the member list of a club
   (`GET /api/team/{team_id}/users`, newline-delimited JSON) and enriches the nicks
   with ratings via `POST /api/users` (up to 300 nicks per request).
2. **Rank** — candidates are filtered by rating bounds, minimum number of games and
   an optional exclusion of the target team's current members. Ranking uses the
   "effective rating": rapid, else blitz, else bullet; unrated players sort last.
3. **Persist** — results are upserted into `candidates` by nick. Re-parsing refreshes
   ratings but never rewinds the outreach status of an existing candidate.
4. **Generate** — `backend/llm` asks an OpenAI-compatible chat completions endpoint for
   a short invitation (≤ 500 characters, plain text, in `MESSAGE_LANGUAGE`). If the LLM
   is unavailable or misconfigured, a deterministic template is used and the draft is
   stored with `llm_model = "fallback"`.
5. **Review** — a human edits the text, gives a 1–5 rating and optional notes, then
   approves or rejects the row. Only `approved` rows can be sent.
6. **Send** — `backend/sender` sends one message at a time to `POST /inbox/{username}`
   with a configurable interval, a daily cap and a stop-on-429 rule.
7. **Analyze** — `backend/analytics` re-reads the target team's membership, marks
   candidates as joined, computes conversion counters and exports the dataset.

## Project structure

```
app.py                        FastAPI application, /health, static frontend mount
backend/
  config.py                   pydantic-settings configuration (.env)
  api/
    deps.py                   admin-token dependency
    routes.py                 JSON API used by the review UI
    schemas.py                request models
  analytics/
    conversion.py             joined detection, counters, CSV export, CLI
  db/
    models.py                 SQLAlchemy Core tables + allowed status values
    database.py               engine, session factory, init_db, get_session
    repository.py             small CRUD helpers
  lichess_api/
    client.py                 base URL, User-Agent, token-aware session factory
    transport.py              retries and back-off for GET/POST
    teams.py                  team info and member list
    users.py                  ratings, game counts, last seen
    messaging.py              POST /inbox/{username}, response → SendResult
  llm/
    generator.py              prompt building, OpenRouter call, fallback template
  parser/
    candidates.py             fetch → filter → rank → upsert, CLI
  sender/
    queue.py                  single send and paced batches, CLI
frontend/
  index.html, app.js, style.css   mobile review UI (Russian labels)
tests/                        pytest suite (network is mocked)
data/                         local SQLite database (git-ignored)
```

## Quick start (local)

Requires Python 3.12.

```bash
# with uv
uv venv .venv
# or with the standard library
python3 -m venv .venv

.venv/bin/pip install -r requirements.txt

cp .env.example .env      # fill in the values described below
.venv/bin/uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>. The UI asks for the admin token once and stores it in
`localStorage`; leave the field empty if `ADMIN_TOKEN` is unset (development mode).
Health check: <http://127.0.0.1:8000/health>.

The tables are created on startup by the FastAPI lifespan hook
(`init_db()` → `metadata.create_all`), so no migration step is needed for a fresh
database.

### Command line helpers

Each pipeline stage also has a CLI, useful for debugging without the web UI:

```bash
.venv/bin/python -m backend.parser.candidates --club <source-club-id> --min-rapid 1200
.venv/bin/python -m backend.sender.queue --batch --limit 5
.venv/bin/python -m backend.analytics.conversion --refresh-joined
.venv/bin/python -m backend.analytics.conversion --stats
.venv/bin/python -m backend.analytics.conversion --export data/dataset.csv
```

## Environment variables

Every key of `backend/config.py` is listed in `.env.example`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LICHESS_TOKEN` | empty | Personal Lichess API token. Used as a bearer token for every Lichess request. Without it only public endpoints work and sending is impossible. |
| `LICHESS_USERNAME` | empty | Lichess account that sends the invitations. Declared in the config; the current code sends as whoever owns `LICHESS_TOKEN`, so keep both consistent. |
| `SENPAI_TEAM_ID` | empty | Id of the target team: used to exclude its members when parsing and as the source of truth for "who joined". |
| `SOURCE_CLUB_ID` | empty | Id of the source club. Declared in the config; the parse form/API and the parser CLI take the club id explicitly, so this is a reference value only. |
| `LLM_API_KEY` | empty | API key for the LLM provider (OpenRouter). Without it generation falls back to the built-in template. |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible base URL; `/chat/completions` is appended. |
| `LLM_MODEL` | `openai/gpt-4o-mini` | Model used for drafts; stored per draft in `outreach.llm_model`. |
| `LLM_TEMPERATURE` | `0.7` | Sampling temperature sent to the provider. |
| `LLM_MAX_TOKENS` | `400` | `max_tokens` of one completion. |
| `LLM_TIMEOUT` | `60` | Per-request HTTP timeout in seconds. |
| `MESSAGE_LANGUAGE` | `ru` | Language of generated and fallback messages (ISO 639-1). The prompt knows `en`, `ru`, `uk`, `be`, `kk`, `de`, `fr`, `es`, `it`, `pt`, `pl`, `tr`, `nl`, `cs`, `sv`; the offline fallback has `ru` and `en` texts and uses `en` for anything else. |
| `CLUB_URL` | empty | Link inserted into messages. When empty it is derived as `https://lichess.org/team/<SENPAI_TEAM_ID>`. |
| `DATABASE_URL` | `sqlite:///./data/outreach.db` | SQLAlchemy URL. A Heroku-style `postgres://` prefix is rewritten to `postgresql://`. |
| `SEND_INTERVAL_SECONDS` | `45` | Minimum gap between two direct messages. Enforced by the batch loop and by the web UI's single-send endpoint (a too-early click is refused instead of sent). |
| `DAILY_SEND_LIMIT` | `30` | Maximum messages per UTC day; checked against `outreach.sent_at` on **every** send path (web UI, single CLI and batch). |
| `RATE_LIMIT_COOLDOWN_SECONDS` | `900` | Our own safety pause after a `429`. This is not an official Lichess number; the platform does not publish one. |
| `STALE_SENDING_SECONDS` | `600` | A `sending` row older than this is considered abandoned and is retried by the next batch. |
| `MIN_RAPID` | `0` | Declared default lower rating bound. The parse form/API currently passes its own `min_rapid`, so this value is not applied automatically. |
| `MAX_RAPID` | `9999` | Declared default upper rating bound; same note as `MIN_RAPID`. |
| `ADMIN_TOKEN` | empty | Protects `/api/*`. Send it in the `X-Admin-Token` header only (the UI stores it in `localStorage`). Query-string tokens are rejected. When empty, every request is allowed (local development only). |

`.env` is git-ignored. Never commit real tokens.

### Where to get `SENPAI_TEAM_ID` and `SOURCE_CLUB_ID`

Both are the slug in a Lichess team URL:

```
https://lichess.org/team/senpai-chess
                            ^^^^^^^^^^^^  this is the id
```

Open the club page on Lichess, copy the last path segment and put it into
`SENPAI_TEAM_ID` (the team candidates are invited to) and `SOURCE_CLUB_ID`
(the club that is parsed). The parse form takes the same id as "club id".

## Lichess token scopes

Create a personal API token at <https://lichess.org/account/oauth/token> with:

- `msg:write` — required to send direct messages (`POST /api/inbox/{username}`).
- `team:read` — required to read the member list of private teams. Public teams can be
  read anonymously, but the tool always uses the token when one is configured.

Lichess does **not** publish an exact rate limit for private messages. This tool
therefore does not guess one: it uses the conservative `SEND_INTERVAL_SECONDS`
interval (default 45 s) plus `DAILY_SEND_LIMIT` (default 30 messages per UTC day).
The guards live in `backend/sender/queue.py::send_outreach`, so they apply to the web
UI button, `--outreach-id` and `--batch` alike. A `429` also stores a cooldown
(`RATE_LIMIT_COOLDOWN_SECONDS`, default 15 min) that blocks every send path until it
expires. Read responses are retried with back-off, honouring `Retry-After` when
present.

## Behavior notes for sending

`backend/lichess_api/messaging.py` maps the HTTP status of the inbox request to a
result, and `backend/sender/queue.py` persists it:

| HTTP result | `SendResult` | `outreach.send_status` | Effect |
| --- | --- | --- | --- |
| 2xx | `SENT` | `sent` | `sent_at` is stored, the candidate becomes `messaged`, the daily counter grows. |
| 400 | `REJECTED` | `skipped_400` | Message rejected by Lichess (blocked user, friends-only messages, etc.). Recorded with the response body in `send_error`; the batch continues. |
| 429 | `RATE_LIMITED` | `skipped_429` | Recorded, a cooldown is stored, and the rest of the batch stops; remaining `approved` rows stay approved for a later run. |
| other 4xx/5xx or network error | `ERROR` | `failed` | Recorded with the error text. |

A `400` is never treated as a crash: it is a normal, expected outcome for some users
and is stored so the same person is not retried automatically. Failed and skipped rows
never re-enter the queue by themselves; approve or regenerate explicitly if you want
another attempt.

Safety rules enforced before any HTTP request:

- the daily cap and the `429` cooldown are checked on every send path;
- a second message to the same player is refused (only one successful DM per nick);
- a candidate already marked `joined` is skipped (`skipped_joined`);
- empty message text is refused;
- the candidate and the outreach row are atomically claimed in the database
  (`candidate.status` and `outreach.send_status` → `sending`) before the request, so
  two approved rows for the same nick, a double submit, or the UI racing a CLI batch
  cannot send the same DM twice; `sent` rows are immutable history;
- pacing in a batch is measured from the actual last send, including a UI send made
  just before the batch started.

Only `approved` rows are selected by a batch. `approve`/`reject` refuse to move a
`sent` row, and `PATCH` refuses to change the text of a sent message (reactions and
reviewer fields are still editable).

Operational note: the daily counter is checked under an in-process lock; the DB claims
prevent duplicate DMs across processes, but two separate senders (for example the web
UI and a `heroku run ... --batch` at the same time) could still exceed the daily cap by
a message or two. Run one sender at a time.

## Human review workflow

1. **Parse** — open the "Парсинг" tab, enter the source club id and optional filters
   (min/max rapid, min games, limit, "exclude own team"), press "Парсить". The API
   stores the ranked candidates.
2. **Generate** — on the "Ревью" tab press "Сгенерировать" for a candidate. A draft is
   created, or the existing draft of that candidate is regenerated (LLM or fallback).
3. **Edit** — change the text, set a 1–5 review rating and optional notes, then
   "Сохранить оценку" (`PATCH /api/outreach/{id}`).
4. **Approve & send** — "Одобрить и отправить" saves the text, approves the row and
   sends exactly that one message. A rejected draft will not be picked up by batches.
5. **Record reaction** — on the "Отправленные" tab set the reaction
   (`none`, `replied`, `declined`, `spam_report`) and save.
6. **Refresh joined** — on the "Статистика" tab press "Обновить вступивших"
   (`POST /api/analytics/refresh`). Membership of `SENPAI_TEAM_ID` is the source of
   truth; the operation is idempotent.
7. **Export CSV** — "Скачать CSV" downloads `dataset.csv`
   (`GET /api/analytics/export.csv`).

The UI text is Russian; the API and the code are English.

## API endpoints

All `/api/*` routes require the admin token unless `ADMIN_TOKEN` is empty.
`/health` and the static frontend are always public.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness probe: `{"status": "ok"}`. |
| `GET` | `/api/stats` | Dashboard counters: candidate totals by status, outreach totals by send status, `sent_total`, `joined_total`, `conversion_rate`. Sent/joined totals count **distinct candidates** and match the CLI (`conversion_stats`). |
| `GET` | `/api/candidates` | List candidates ordered by id, each with its latest outreach row. Optional filters: `status`, `source_club_id`. |
| `POST` | `/api/parse` | Parse a club. Body: `club_id` (required), `min_rapid`, `max_rapid`, `min_games` (default 0), `limit`, `exclude_own` (default `true`, excludes `SENPAI_TEAM_ID` members). Returns `{"parsed": n, "candidates": [...]}`. |
| `POST` | `/api/outreach/generate` | Generate a draft for `candidate_id`; reuses/updates the latest `draft` row. Returns the outreach row. `404` if the candidate is unknown. |
| `GET` | `/api/outreach` | List outreach rows joined with nick and ratings. Optional filter: `send_status`. |
| `PATCH` | `/api/outreach/{outreach_id}` | Update `message_text`, `reviewer_rating` (1–5), `reviewer_notes`, `reaction`. `422` on an unknown reaction, `404` if missing, `409` when changing the text of a `sent` row (it is immutable history; reactions/notes stay editable). |
| `POST` | `/api/outreach/{outreach_id}/approve` | Move `draft`/`failed`/`rejected`/`skipped_*` to `approved`. `409` for `sent`/`sending`. |
| `POST` | `/api/outreach/{outreach_id}/reject` | Move a non-sent row to `rejected`. `409` for `sent`/`sending`. |
| `POST` | `/api/outreach/{outreach_id}/send` | Send one row through the same guards as the CLI. An untouched `draft` is approved on the fly; any other non-`approved` status returns `400`. `429` when the daily cap, pacing or the `429` cooldown refuses the send; `409` for `sent`/`sending`/already-messaged/joined; `422` for empty text. |
| `POST` | `/api/analytics/refresh` | Re-read the target team and mark joined candidates. Returns `team_id`, `checked` (members seen in the team), `newly_joined` (candidate status changed) and `total_joined` (candidates with a **sent** message that are now in the team). `400` when `SENPAI_TEAM_ID` is empty. |
| `GET` | `/api/analytics/export.csv` | Download the dataset as `text/csv` (`dataset.csv`). |

Static files (`/`, `/app.js`, `/style.css`) are served from `frontend/`.

## Data model

Three SQLAlchemy Core tables (`backend/db/models.py`), dialect-neutral so the same
schema works on SQLite and Postgres. Allowed values are enforced in the application
layer. `app_state` is a tiny key/value store used for the `429` cooldown.

**`candidates`**

| Column | Type | Notes |
| --- | --- | --- |
| `id` | integer PK | |
| `nick` | string(50), unique | Lichess username. |
| `source_club_id` | string(50) | Club the candidate was parsed from. |
| `rapid`, `blitz`, `bullet` | integer, nullable | Ratings; `null` when the player has none. |
| `games_total` | integer | Total rated games. |
| `last_seen` | bigint, nullable | Lichess "seen at" timestamp. |
| `status` | string(20) | `new`, `selected`, `sending` (transient claim), `messaged`, `joined`, `skipped` (default `new`). |
| `created_at`, `updated_at` | datetime | |

Re-parsing a club updates ratings and `last_seen` but leaves `status` untouched.

**`outreach`**

| Column | Type | Notes |
| --- | --- | --- |
| `id` | integer PK | |
| `candidate_id` | integer FK → `candidates.id` | Indexed. |
| `message_text` | text | Draft or edited message. |
| `llm_model` | string(100) | Model name, or `fallback` when the template was used. |
| `reviewer_rating` | integer | 1–5, optional. |
| `reviewer_notes` | text | Optional. |
| `send_status` | string(20) | `draft`, `approved`, `sending` (transient claim), `rejected`, `sent`, `skipped_400`, `skipped_429`, `skipped_joined`, `failed` (default `draft`, indexed). |
| `send_error` | text | Response body/error of a failed attempt. |
| `sent_at` | datetime | Set on success; used by the daily cap. |
| `reaction` | string(20) | `none`, `replied`, `declined`, `spam_report` (default `none`). |
| `joined` | boolean | Set by the analytics refresh only for rows that were actually `sent`, so drafts do not inflate conversion. |
| `joined_at` | datetime | |
| `created_at`, `updated_at` | datetime | |

### CSV export columns

`GET /api/analytics/export.csv` (or `--export`) writes one row per **outreach** record,
ordered by outreach id, UTF-8, with this header:

```
nick, source_club_id, rapid, blitz, bullet, games_total, message_text, llm_model,
reviewer_rating, reviewer_notes, send_status, send_error, sent_at, reaction, joined,
joined_at
```

Empty values are written as empty strings, booleans as `true`/`false`, timestamps in
ISO format with a space separator. String values that start with `=`, `+`, `-`, `@`,
a tab or a carriage return are prefixed with `'` to neutralize CSV formula injection
when the file is opened in a spreadsheet.

## Deployment on Heroku

Heroku's filesystem is ephemeral: a SQLite file disappears on every dyno restart. A
Postgres add-on is therefore required for persistence; Heroku injects it as
`DATABASE_URL` (the config normalizes its `postgres://` prefix to `postgresql://`,
which SQLAlchemy maps to the `psycopg2` driver from `requirements.txt`).

```bash
heroku create
heroku addons:create heroku-postgresql:essential-0
heroku config:set \
  LICHESS_TOKEN=... \
  LICHESS_USERNAME=... \
  SENPAI_TEAM_ID=... \
  LLM_API_KEY=... \
  ADMIN_TOKEN=...
git push heroku main
heroku open
```

`app.json` describes the same setup for "Deploy to Heroku" (add-on plus env vars; the
admin token is generated by Heroku). The `Procfile` contains:

```
release: python -c "from backend.db.database import init_db; init_db()"
web: uvicorn app:app --host 0.0.0.0 --port $PORT
```

The release phase creates the tables once per deploy (idempotent `create_all`), and the
web process serves the API and the static UI. Set the other optional variables
(`LLM_MODEL`, `LLM_BASE_URL`, `MESSAGE_LANGUAGE`, `SEND_INTERVAL_SECONDS`,
`DAILY_SEND_LIMIT`, ...) with `heroku config:set` when you need non-default values.

Note that background batches are not scheduled: sending happens synchronously when a
human presses the button in the UI. A paced batch can also be run manually in a
one-off dyno:

```bash
heroku run python -m backend.sender.queue --batch
```

## Safety and Lichess terms

The Lichess API has no public quota for private messages, and unsolicited bulk
messaging can be considered spam. Accounts that mass-message strangers risk being
restricted or banned by Lichess. This tool is deliberately built for slow,
personalized, human-approved outreach:

- every message is written for one specific player and approved by a person;
- only users who are already members of a related club are targeted;
- one message per player, no automatic retries after a rejection;
- the default pacing is 45 seconds between messages and 30 messages per day, enforced
  on the web UI path as well as the CLI;
- a `429` stops the batch immediately and starts a 15-minute cooldown for all paths.

Keep the defaults or make them slower, and respect anyone who does not want to be
contacted. You are responsible for how you use your Lichess account.

## Testing

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

The test suite (`tests/`) covers the Lichess transport and messaging mapping, the
parser filters/ranking, LLM prompt building and retry logic, the sender queue and the
API, and never touches the network: HTTP sessions are faked. The same two commands run
in GitHub Actions on every push and pull request (`.github/workflows/ci.yml`).
