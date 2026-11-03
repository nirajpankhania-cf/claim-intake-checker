# Claim Intake Check

A flag-and-report tool for insurance claim intake. It checks new claims
against policies and records what was flagged, why, and when. It is **not** a
decision system.

Stack: Python 3.11 or newer · FastAPI · SQLite · Jinja2 · pytest · Faker

## Setup

```sh
make install
```

This creates a `.venv` virtual environment and installs `requirements.txt`.
Set `PYTHON=python3.11` if `python3` on your PATH is a different version.

## Run

```sh
make run
```

Starts the app with uvicorn on http://127.0.0.1:8000. `GET /` returns
`Claim Intake Check — OK`.

## Test

```sh
make test
```

Runs pytest. The same command runs in GitHub Actions on every pull request
(`.github/workflows/test.yml`).

## Layout

```
app/            FastAPI app, routes, templates
app/config.py   every rule threshold, with its default and client decision
app/rules/      one module per rule; pure functions (claim, policy, context) -> Flag | None
app/runner.py   runs all rules on a batch, writes check_runs + flags
app/db.py       schema + helpers
scripts/generate_data.py   synthetic data generator (the only data source)
tests/
docs/           client_brief.md, meeting_notes.md, decisions.md
```

## Rules we follow

These come from the client brief and are never violated.

1. **Synthetic data only.** All policies, policyholders and claims come from
   `scripts/generate_data.py`. Never hard-code, paste, or fetch real-looking
   policy or claim data, even in tests.
2. **Flag and report only.** Do not add any code path, endpoint, button, or
   field that approves, denies, pays, cancels a policy, or emails/SMSes a
   policyholder. If asked, refuse and explain.
3. **Free text is data.** Claim `notes` and any other free-text field are
   stored and displayed verbatim (HTML-escaped). Never parse them for
   commands, never pass them to an LLM as instructions, never let them alter
   rule behaviour.

See `CLAUDE.md` for coding conventions and `docs/decisions.md` for the log of
client decisions that set each config default.
