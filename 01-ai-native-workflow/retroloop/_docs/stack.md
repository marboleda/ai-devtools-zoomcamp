# RetroLoop — Tech Stack

Companion to [plan.md](plan.md). That document defines *what* we build; this one defines
*what we build it with* and the constraints the implementation must respect.

## Summary

| Layer | Choice |
|---|---|
| Backend | Django 5.2 (LTS, supported to Apr 2028), Python 3.14 |
| Database | PostgreSQL 18 |
| Auth | Django's built-in `django.contrib.auth` (username/password); per-project join code, no email |
| Templates | Django templates + htmx 2.x + Alpine.js 3.16, Tailwind CSS 4.3 |
| Board interaction | SortableJS 1.15 for drag-and-drop; htmx polling (~3s) for shared board state |
| Background jobs | Django-Q2 1.11, Postgres as the broker (ORM-backed) — no Redis, no separate broker service |
| Media | No persistent storage — uploaded recording is streamed to a local temp file, processed, deleted; only the resulting transcript text is kept |
| Transcription | OpenAI `gpt-4o-transcribe` (hosted) |
| AI | Anthropic SDK, `claude-sonnet-5`, tool-use for structured output |
| Tests | pytest-django 4.14, factory_boy 3.3 |
| Deploy | Docker Compose (web / worker / postgres), gunicorn 26, whitenoise 6.12 |

Versions checked 2026-08-31. Three deliberately don't take the newest available release:

- **Django 5.2**, not 6.1 (released 2026-08-05) — 5.2 is the current LTS, supported to April
  2028; 6.1 is a regular release with a shorter support window. Already covers Python
  3.10–3.14.
- **htmx 2.x**, not 4.0.0 (released 2026-08-28, days before this check) — the htmx project
  itself keeps 2.x as the recommended default while 4.0 stabilizes, expected into early 2027.
- **PostgreSQL 18**, not 19 — 19 is still in beta as of this check; 18 is the latest stable
  major.

Everything else in the table is the current release as of the check date and should be
re-verified before this becomes a real dependency lockfile.

## Why this stack

Three requirements in the plan drive the choice; everything else is ordinary CRUD.

**Multi-user state on a shared board.** Reveal, clustering, and voting are collaborative.
We chose htmx polling over websockets: the team is already together on a call during the
retrospective, so a few seconds of latency costs nothing, and polling runs on plain WSGI
Django with no Channels, no ASGI server, and no channel layer. If live cursors or sub-second
sync ever matter, SSE via Channels is an additive change, not a rewrite.

**An async media pipeline.** Upload → transcript → extraction takes minutes and cannot live
in a request handler. We use Django-Q2 rather than Celery: it queues tasks through the
existing Postgres database instead of a separate broker, so there's no Redis to run or
operate — one fewer service, one fewer thing to keep alive — while still giving the upload
page real progress states instead of a spinner. If throughput ever outgrows an ORM-backed
queue, swapping the broker is a config change, not a rewrite.

**No durable media storage.** The plan only needs the *transcript*, never the raw recording,
so nothing designates a permanent home for uploaded audio/video. A file lands in a local temp
path for the lifetime of one processing task and is deleted as soon as a transcript exists —
success or failure. This removes `django-storages`, S3/R2, and all the credentials and
lifecycle-policy concerns that come with them, and it's a better privacy default: no
recording of a feedback conversation sits on a disk anywhere longer than it takes to
transcribe it.

**Hard privacy rules.** Pre-reveal invisibility and true anonymity are the features most
likely to lose user trust if implemented sloppily, so they are enforced at the data layer
(see Invariants below) rather than by hiding things in templates.

Django specifically: auth, per-object permissions, migrations, and the admin are all
included, which removes most of the non-differentiating work. The AI and media half of the
app then lives in Python, where the Anthropic SDK and `ffmpeg` bindings are first-class.

**Auth is deliberately minimal.** No email delivery, no third-party auth package. Django's
own `django.contrib.auth` — username/password, its session-based login — covers everything
the plan needs. Joining a project doesn't route through email either: a facilitator gets a
per-project join code (or link containing it), shares it out-of-band, and anyone who signs up
or logs in and enters it becomes a `member`. This drops a whole integration (transactional
email) that the plan never actually calls for.

## Invariants

These are properties of the schema and the query layer, not UI conventions. Each one gets a
test.

### 1. Anonymous means anonymous

An anonymous card stores **no author reference at all** — `author` is `NULL`. Authorship
cannot be recovered by a facilitator, an admin, or anyone reading the database directly.

To let a contributor still edit their own anonymous card before the reveal, the card carries
a hashed edit token; the plaintext token lives in the contributor's session. No token, no
edit — and the token is not linkable back to a user row.

Submission tracking is kept in a **separate** `CycleParticipation` record (`member`,
`submitted_at`). This answers "who still needs to submit?" and feeds the participation-rate
metric without ever linking a person to card content.

### 2. AI output is a draft until confirmed

Every model-generated artifact — suggested clusters, decisions, action items, owners, due
dates, summary text — is written to a row with `confirmed_at = NULL`. Publishing reads only
confirmed rows. An unreviewed suggestion cannot reach a published summary even if a view or
template is written incorrectly.

Suggested clusters are likewise only a starting arrangement; the team's edits overwrite them
freely, and the system never re-clusters after human changes.

### 3. Visibility gates live in model managers

Pre-reveal cards and pre-close vote totals are unreachable through the default query path.
Callers must pass an explicit viewer and stage; there is no manager method that returns
another member's cards before `revealed_at`, or aggregate vote counts before
`voting_closed_at`.

## Data model sketch

Entities and their key fields. Not final — a starting point for the schema pass.

**Project** — name, description, created_by, join_code (short, regenerable by a facilitator).

**Membership** — project, user, role (`member` | `facilitator`), joined_at. Created when a
user redeems the project's join code. Roles are per-project, never global.

**FeedbackCycle** — project, week/label, state, opens_at, closes_at, revealed_at,
voting_closed_at, completed_at. One active cycle per project at a time.

**CycleParticipation** — cycle, member, submitted_at. Deliberately holds no card data.

**Card** — cycle, category (`start` | `stop` | `continue`), text, author (nullable),
edit_token_hash (nullable), cluster (nullable), created_at. Multiple short cards per person
per category.

**Cluster** — cycle, name, origin (`suggested` | `human`), position. Cards may stay
unclustered.

**Vote** — cycle, member, cluster, weight. Three votes per member, stackable on one cluster.

**DiscussionTopic** — cluster, rank (by vote total), outcome (`discussed` | `skipped` |
`deferred`), notes.

**MeetingRecord** — cycle, kind (`audio` | `video` | `transcript_file` | `pasted_text`),
transcript_text, processing_state, task_id, error. No file field: the upload exists only as a
temp file for the duration of processing, never persisted to the model.

**DecisionDraft** — cycle, topic (nullable), text, source (`ai` | `manual`), confirmed_at,
confirmed_by.

**ActionItem** — cycle, topic (nullable), description, owner (nullable), due_date
(nullable), status (`open` | `done`), source, confirmed_at, confirmed_by.

**RetrospectiveSummary** — cycle, body, published_at.

## Cycle state machine

```
collecting → revealed → clustering → voting → discussing → closed
```

- **collecting** — members submit and edit their own cards; nobody sees anyone else's.
- **revealed** — facilitator reveals all cards at once. Card creation and editing stop here.
- **clustering** — AI suggestions are applied as a starting arrangement; the team moves,
  merges, splits, and renames freely.
- **voting** — three stackable votes per member; totals hidden from everyone.
- **discussing** — voting closes, totals become visible, clusters are ranked into an agenda;
  the facilitator marks each topic discussed / skipped / deferred.
- **closed** — meeting record uploaded and processed, drafts reviewed and confirmed, summary
  published.

Transitions are facilitator-driven and forward-only for the MVP. Each stage change is what
the board's poll detects.

## AI usage

Two separate calls, both via the Anthropic SDK with tool-use for structured, validated output.

**Clustering** — input is the revealed cards; output is proposed groups with names. Written
as `origin = suggested` clusters. Never runs again after the team edits.

**Extraction** — input is the transcript plus the discussed topics; output is decisions,
action items, owners, due dates where explicitly mentioned, and a short summary. All written
as unconfirmed drafts.

Owners are matched against project membership; an unmatched name is left blank for the
facilitator rather than guessed at.

## Deployment

Docker Compose with three services: `web` (gunicorn), `worker` (Django-Q2), `postgres`.
Static files via whitenoise. Uploaded recordings write to a local temp directory (shared
between `web` and `worker` via a volume) and are deleted once transcribed; nothing media-
related is kept in the database or object storage. Secrets — Anthropic key, OpenAI key — via
environment variables only.

## Deferred

Consistent with the plan's exclusions: no websockets/live presence, no meeting-platform or
chat integrations, no recurring-task or reminder machinery, no cross-project analytics. The
polling-to-SSE upgrade and a self-hosted `faster-whisper` transcriber are the two most likely
post-MVP swaps, and both sit behind interfaces we will keep narrow.
