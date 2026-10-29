# Contributing

## Setup

```bash
python3 -m venv .venv          # or: uv venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env           # fill in your own values; never commit .env
```

## One task, one commit

The history is organized as one task per commit. Each commit should:

- implement a single, reviewable step of the pipeline (for example "sender queue with
  pacing"), including its tests and docs;
- leave `main` green: run the checks below before committing;
- use a conventional-commit subject in English, for example
  `feat(sender): queued sending with backoff and skips`,
  `fix(api): reject unknown reactions`,
  `docs(deploy): Heroku and CI`.

Avoid mixing unrelated refactors into the same commit. If a change touches several
areas, split it into separate commits.

## Checks

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

Both commands also run in CI on every push and pull request.

## Tests

Tests must not touch the real Lichess or LLM APIs. Inject a fake session object
(anything with `request`/`post`) and, where code sleeps, pass a fake `sleep` function.
Use a temporary SQLite database per test instead of `data/outreach.db`.

## Safety

- Do not commit tokens, `.env` files or database dumps.
- Do not make sending faster by default; the conservative pacing is intentional.
- Do not add behavior that sends messages without human approval.
