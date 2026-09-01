# RetroLoop — Backlog

Each task is scoped to one session and is self-contained — it points back to
[plan.md](plan.md) and [stack.md](stack.md) for shared context rather than to other tasks.
Tasks are listed in a sensible build order, but each one states what it needs rather than
assuming a reader has done the others.

## 1. Project scaffold with a passing test
Goal: Stand up an empty Django project that runs and has one passing test.
Description: Initialize the Django 5.2 project (Python 3.14) per stack.md, wire up Postgres
locally via Docker Compose, and add pytest-django with a single smoke test (e.g. a
health-check or home view returning 200) that passes. No feature code — this task only proves
the toolchain works end to end.

## 2. Signup, login, logout
Goal: Let a person create an account and sign in.
Description: Add Django's built-in auth with a minimal signup form plus login and logout
views, username/password only, no email flow, per stack.md. Cover it with tests for
signup-then-login and for rejecting a bad password.

## 3. Create a project
Goal: Let a signed-in user create a Project and become its facilitator.
Description: Add the Project model (name, description, created_by, join_code) from
stack.md's data model sketch, a "create project" form, and a Membership row created
automatically with role=facilitator for the creator. Generate a short random join_code on
creation.

## 4. Join a project via join code
Goal: Let a signed-in user join an existing project as a member.
Description: Add a "join project" form that accepts a join code, looks up the matching
Project, and creates a Membership with role=member. Reject invalid codes with a clear error
and prevent a user from joining the same project twice.

## 5. Project page skeleton
Goal: Give each project a landing page its members land on.
Description: Build the Project page described in plan.md's "Main screens" section, showing
the project name and member list; leave placeholders for "current cycle," "open action
items," and "previous retrospectives" for later tasks to fill in. Restrict access to members
of that project only.

## 6. Facilitator-only permission check
Goal: Ensure facilitator-only actions actually require the facilitator role.
Description: Add a reusable permission check (decorator or mixin) that reads a user's
Membership.role for a given project and blocks non-facilitators from facilitator-only
actions. Write a test using two users in the same project with different roles to confirm
both the allow and deny paths.

## 7. Create a weekly feedback cycle
Goal: Let a facilitator open a new feedback cycle for their project.
Description: Add the FeedbackCycle model from stack.md (state, opens_at, closes_at, etc.) and
a facilitator-only action that creates one, defaulting its state to "collecting." Enforce at
most one active cycle per project at a time.

## 8. Submit a Start / Stop / Continue card
Goal: Let a member add feedback cards to the active cycle.
Description: Build the feedback form from plan.md with the three categories (start / stop /
continue), letting a member create multiple short cards, each with an "anonymous" checkbox.
An anonymous card stores no author reference at all, per the anonymity invariant in stack.md.

## 9. Edit or withdraw your own card before reveal
Goal: Let a contributor edit or delete a card they submitted, while collection is still open.
Description: For attributed cards, let the author edit or delete only their own cards while
the cycle is in "collecting" state. For anonymous cards, generate a hashed edit token stored
with the card and keep the plaintext token in the contributor's session, requiring it to
edit — this is the mechanism described in stack.md's anonymity invariant.

## 10. Enforce pre-reveal card privacy
Goal: Guarantee no one can see another member's cards before reveal.
Description: Add a model manager or query helper that returns only a member's own cards while
a cycle is "collecting," and all cards once revealed_at is set. Write a test asserting that
one member's query never returns another member's card before reveal, even via a direct
model query.

## 11. Track submission participation
Goal: Show who has and hasn't submitted feedback yet, without exposing card content.
Description: Add the CycleParticipation model (cycle, member, submitted_at) from stack.md,
populated the first time a member saves a card in a given cycle. Surface a simple "N of M
submitted" indicator on the project page.

## 12. Facilitator reveal action
Goal: Let the facilitator reveal all submitted cards at once.
Description: Add a facilitator-only action that sets FeedbackCycle.revealed_at and
transitions the cycle's state from "collecting" to "revealed," after which card creation and
editing stop. Write a test confirming cards become visible to all members only after this
action runs.

## 13. Retrospective board — Reveal view
Goal: Show all revealed cards on a shared board.
Description: Build the "Reveal" mode of the retrospective board described in plan.md, listing
all cards grouped by category with no clustering yet. Use htmx polling every few seconds so
all participants see the same state, per stack.md.

## 14. AI clustering suggestion
Goal: Auto-generate a first-pass grouping of revealed cards.
Description: Call the Anthropic API, per stack.md's "AI usage" section, with the revealed
cards and use tool-use to get back proposed clusters with names; write them as Cluster rows
with origin="suggested". This call runs once right after reveal and never re-runs
automatically.

## 15. Manual clustering — move, merge, split, rename
Goal: Let the team freely edit the AI-suggested clusters.
Description: Add drag-and-drop (SortableJS) to move cards between clusters, plus actions to
merge two clusters, split one apart, rename one, or leave a card unclustered. Any cluster,
regardless of how it originated, must stay editable by the team.

## 16. Cast votes on clusters
Goal: Let each member spend three stackable votes across clusters.
Description: Add a voting UI where each member has exactly three votes to distribute across
clusters, including stacking multiple votes on one cluster. Enforce the three-vote cap
server-side, and hide all vote totals from everyone while voting is open, per the invariant
in stack.md.

## 17. Close voting and rank the agenda
Goal: Reveal vote totals and produce a prioritized discussion order.
Description: Add a facilitator-only "close voting" action that sets voting_closed_at, after
which totals become visible to everyone and clusters are turned into a DiscussionTopic list
ordered by vote count.

## 18. Discussion mode
Goal: Let the facilitator run through topics in priority order during the meeting.
Description: Build the "Discuss" mode of the retrospective board, letting the facilitator
mark each DiscussionTopic as discussed, skipped, or deferred, and letting any member attach
manual notes to the topic currently being discussed. This is manual note-taking only — no AI
involved at this stage.

## 19. Meeting upload page
Goal: Let the facilitator submit a meeting recording or transcript for processing.
Description: Build the upload page from plan.md, accepting audio, video, a transcript file,
or pasted transcript text; creating a MeetingRecord row; and enqueueing a background job. Show
a processing-status indicator that updates via polling.

## 20. Background transcription pipeline
Goal: Turn an uploaded audio or video file into transcript text.
Description: Add a Django-Q2 task that, for audio/video uploads, extracts audio with ffmpeg
if needed and calls the OpenAI transcription API, then writes the result to
MeetingRecord.transcript_text. Delete the temp file whether the job succeeds or fails, per the
no-persistent-media rule in stack.md; pasted-text and transcript-file uploads skip
transcription and populate transcript_text directly.

## 21. AI extraction of decisions and action items
Goal: Turn a transcript into draft decisions and action items.
Description: Call the Anthropic API with the transcript and the cycle's discussed topics,
using tool-use to extract decisions, action items (with owner and due date when explicitly
mentioned), and a short summary. Write every result as an unconfirmed draft
(confirmed_at = NULL), matching owner names against project membership and leaving an
unmatched name blank rather than guessing.

## 22. Facilitator review and confirmation of drafts
Goal: Let the facilitator approve, edit, or discard AI-suggested outcomes.
Description: Build the review screen where the facilitator sees each DecisionDraft and
ActionItem, can edit its text, owner, or due date, and confirms or discards it. Only rows with
confirmed_at set are eligible to appear in the published summary.

## 23. Publish the retrospective summary
Goal: Produce the final, shareable record of the retrospective.
Description: Build the summary screen from plan.md showing top discussion topics, key notes,
confirmed decisions, confirmed action items, attendance/participation, and the original
feedback cards, then let the facilitator publish it (setting RetrospectiveSummary.
published_at). This is the "closed" state of the cycle state machine described in stack.md.

## 24. Action item tracking on the project page
Goal: Let assigned owners mark their action items done, and surface open items project-wide.
Description: Add a status toggle (open/done) that an action item's owner can update from their
own view, and list a project's open action items across all its retrospectives on the project
page. This closes the loop the plan's success metrics describe around completed actions.

## 25. Previous retrospectives list
Goal: Let a project show its history of completed retrospectives.
Description: Add a list of past, closed retrospective cycles to the project page, each linking
to its published RetrospectiveSummary. This fulfills the "Previous retrospectives" item
specified for the project page in plan.md.
