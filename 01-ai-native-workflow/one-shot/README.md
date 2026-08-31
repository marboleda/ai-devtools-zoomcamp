# weekly-feedback

## Disclaimer

NOTE: This project was created with a single sentence `Implement tool for weekly feedback for projects` being fed into Claude Code.  
It's not what we're looking for, but is an example of what AI will generate if we just give it vague specifications.

***All the files in this folder and the README contents under this are AI-generated***

A command-line tool that reads a project's git activity for one ISO week and
writes feedback on it: how much landed, how it was paced, and what looks worth
fixing. Built for a weekly check-in on one or many project repositories, run by
hand or from cron/CI.

No third-party dependencies — Python 3.10+ and `git` on `PATH` are enough.

## Usage

```console
$ weekly-feedback --project ../ingestion --week 2026-W35

Week 2026-W35 (2026-08-24 to 2026-08-30)

ingestion  [C:\projects\ingestion]
  commits 5   files 2   +1,030/-0   active days 4/7
  authors: Marco (5)
  feedback:
    ! 1 commit changed more than 800 lines; smaller commits are far easier to review.
        3f7677b1 +950/-0 across 1 file: Rewrite the storage layer end to end
    ! 2 source files changed but no test file was touched.
        src/loader.py
        src/storage.py
    ~ 2 commit messages say little about the change; write what changed and why.
        c85b232f wip
        c8539156 fix
    ~ 1 file was revisited repeatedly; repeated edits often point at a design that wants splitting.
        src/loader.py (3 commits)
    + Steady cadence: work landed on 4 separate days of the week.
```

Markers: `!` warning, `~` suggestion, `+` something done well.

Without installing:

```console
python -m weekly_feedback --week last
```

### Common invocations

```console
weekly-feedback                                    # this week, repo in the current directory
weekly-feedback --week last                        # the previous week
weekly-feedback --week 2026-W35                     # a specific ISO week
weekly-feedback --week 2026-08-27                   # the week containing that date
weekly-feedback --weeks-ago 3                       # three weeks before this one

weekly-feedback -p ../api -p ../web -p ../docs      # several projects in one report
weekly-feedback --author marco@example.com          # only one person's commits
weekly-feedback --all-branches                      # not just the checked-out branch

weekly-feedback --format markdown --out reports/2026-W35.md
weekly-feedback --format json | jq '.projects[].stats'
```

### Options

| Option | Meaning |
| --- | --- |
| `-p`, `--project PATH` | A git repository to review. Repeat for several; defaults to `.` |
| `-w`, `--week SPEC` | `2026-W35`, a date inside the week, or `current` / `last` |
| `--weeks-ago N` | Shift the selected week N weeks into the past |
| `-f`, `--format` | `text` (default), `markdown`, or `json` |
| `-o`, `--out FILE` | Write to a file instead of stdout (parent directories are created) |
| `--author PATTERN` | Restrict to matching authors (passed to `git --author`) |
| `--all-branches` | Consider every branch, not just `HEAD` |
| `--large-commit-lines N` | Threshold for the large-commit warning (default 800) |
| `--min-commits N` | Below this many commits the week is flagged as quiet (default 3) |
| `--strict` | Exit 1 if any project has a warning |

Exit codes: `0` clean, `1` warnings under `--strict`, `2` a project could not be
read (a bad path or a git failure) — the other projects are still reported.

## What it looks at

| Check | Fires when |
| --- | --- |
| `no-activity` | No commits authored in the week |
| `low-activity` | Fewer commits than `--min-commits` |
| `steady-cadence` | Commits on 4 or more separate days (praise) |
| `bursty-cadence` | Everything landed on a single day |
| `large-commits` | A non-merge commit exceeds `--large-commit-lines` |
| `no-tests-touched` | Source files changed, no test file did |
| `tests-alongside-code` | Tests moved with the code (praise) |
| `no-docs-touched` | 8+ source files changed with no docs or README update |
| `vague-commit-messages` | Subjects like `wip`, `fix`, or anything under 12 characters |
| `churn-hotspot` | One file rewritten across 3 or more commits |

Files are bucketed as `source`, `test`, `docs`, `config` or `other` by path and
extension (see `weekly_feedback/paths.py`).

## Notes on the numbers

- A commit belongs to the week containing its **author date as the author saw
  it** — the date part of `git log %aI`, in that author's own UTC offset. The
  timezone of the machine running the report never changes the answer.
- **Merge commits** are counted (and reported as "PRs merged" when the subject
  carries a `Merge pull request #N`), but contribute no churn: their content is
  already counted on the commits being merged in.
- Renames are recorded as an add plus a delete (`git log --no-renames`).
- Binary files count as changed files with zero churn.

## Install

```console
uv tool install .          # or: pip install .
```

## Development

```console
python -m unittest discover        # 106 tests, no dependencies
```

Layout:

| Module | Role |
| --- | --- |
| `weeks.py` | Parse and resolve week specifications |
| `gitlog.py` | Run `git log`, parse commits and per-file churn |
| `paths.py` | Classify changed files |
| `analyze.py` | Aggregate stats, run the feedback rules |
| `report.py` | Render text / markdown / JSON |
| `cli.py` | Argument parsing and orchestration |
