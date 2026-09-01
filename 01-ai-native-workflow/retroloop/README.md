# RetroLoop

A weekly-feedback and retrospective tool for project teams: collect private
Start/Stop/Continue feedback, reveal and cluster it together, vote on what to
discuss, run the discussion, and turn the meeting recording into a reviewed
set of decisions and action items.

## Problem

Teams that want a real weekly retrospective loop — private feedback
collection, a facilitated discussion, and a documented outcome — end up
stitching together a survey tool, a whiteboard app, and a manual meeting
notes doc, with no link between them. RetroLoop is a single workflow that
takes a team from "submit feedback" to "published summary with confirmed
action items," described in full in [`_docs/plan.md`](_docs/plan.md).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                    # install dependencies
docker compose up -d db    # start Postgres
uv run python manage.py migrate
uv run python manage.py runserver
```

Visit `http://localhost:8000/` — it should respond `ok`.

## Testing

```bash
uv run pytest                          # the whole suite
uv run pytest tests/test_home.py       # one test file
```

Tests run against the same Postgres started by `docker compose up -d db`.

## Tech stack

Django 5.2 (Python 3.14) with PostgreSQL 18, pytest-django for tests, and
Docker Compose for local services. The full stack, including the pieces not
built yet (htmx, background jobs, AI extraction), and the reasoning behind
each choice, is in [`_docs/stack.md`](_docs/stack.md).

## Project structure

```
config/   Django project (settings, URLs, WSGI/ASGI)
tests/    pytest test suite
_docs/    product plan, tech stack, and process docs
```

## Design decisions

Product scope and the reasoning behind each MVP decision are in
[`_docs/plan.md`](_docs/plan.md). Technical choices and the invariants the
schema enforces (anonymity, draft-until-confirmed AI output, pre-reveal
visibility) are in [`_docs/stack.md`](_docs/stack.md).
