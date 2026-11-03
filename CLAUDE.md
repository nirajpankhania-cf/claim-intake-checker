# Claim Intake Check — rules for the coding agent

## What this is
A flag-and-report tool for insurance claim intake. It checks new claims against policies and records what was flagged, why, and when. It is NOT a decision system.

## Hard rules (from the client brief — never violate)
1. **Synthetic data only.** All policies, policyholders and claims come from `scripts/generate_data.py`. Never hard-code, paste, or fetch real-looking policy or claim data, even in tests.
2. **Flag and report only.** Do not add any code path, endpoint, button, or field that approves, denies, pays, cancels a policy, or emails/SMSes a policyholder. If asked, refuse and explain.
3. **Free text is data.** Claim `notes` and any other free-text field are stored and displayed verbatim (HTML-escaped). Never parse them for commands, never pass them to an LLM as instructions, never let them alter rule behaviour.

## Stack
Python 3.11 or newer · FastAPI · SQLite (`sqlite3` or SQLAlchemy) · Jinja2 templates · pytest · Faker

## Layout
```
app/            FastAPI app, routes, templates
app/rules/      one module per rule; pure functions: (claim, policy, context) -> Flag | None
app/runner.py   runs all rules on a batch, writes check_runs + flags
app/db.py       schema + helpers
scripts/generate_data.py
tests/
docs/           client_brief.md, meeting_notes.md, decisions.md
```

## Conventions
- Rules are pure functions with no DB access; the runner passes them what they need.
- Every threshold (reporting window days, duplicate match fields, boundary-day inclusivity) lives in `app/config.py` with a documented default and the client decision that set it.
- Every flag row stores: run_id, claim_id, rule_id, reason (plain English), inputs snapshot (JSON), created_at. History is append-only — never UPDATE or DELETE flag rows.
- Dates are ISO `YYYY-MM-DD`; compare as `date` objects, never as strings.
- Tests for each rule must cover: clearly inside, clearly outside, and both boundary days.
- Keep PRs under ~400 lines. Don't refactor unrelated files.
- Branch names: `cic-<issue>-<short-slug>`.

## Decisions log
See `docs/decisions.md`. When a client answer changes a default, update the config AND the log in the same PR.

## Git workflow (every task)
- Never commit directly to `main`. Before changing any file, check the current branch; if it is `main`, create `cic-<issue>-<short-slug>` from the latest `main` and switch to it.
- One Linear issue per branch per PR. Do not mix issues.
- Commit messages: `CIC-<n>: <what changed>`.
- When the task is done and tests pass, push the branch and open a PR against `main` with `gh pr create`. PR title `CIC-<n>: <title>`, body with What / Why / How tested. Do not merge; a teammate reviews.
- If you are unsure which issue number applies, ask before creating a branch.
