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

## Demo data

```sh
make seed
```

Runs `scripts/generate_data.py`, which builds `claim_intake.db` (SQLite) and
exports `data/policies.csv` and `data/claims.csv`. The output is synthetic and
identical on every run (fixed seed 4242 and a fixed reference date of
2026-10-31), so `data/` and `*.db` are not committed. To start over, run
`make reset`: it deletes the DB file and seeds again (the generator won't
write into a DB that already has data). Set `CIC_DB_PATH` to use a different
DB file.

What you get:

- 200 car policies and 500 claims in the DB. About 88% of the claims are clean.
- At least 6 claims for each scenario S1–S10 (see `docs/decisions.md`). Each
  is tagged at the start of its `description`, e.g.
  `SCENARIO S6: late report 41 days.`, so you can find it in the demo.
  Clean boundary cases are tagged `BOUNDARY: ...`. No rule reads these tags.
- `data/claims.csv` holds the same 500 claims plus 12 extra rows: 6 S8
  resubmissions (a repeated `claim_id` with a changed description, which
  overwrites and re-checks the stored claim and keeps the old version) and 6
  S9 malformed rows (missing fields, bad dates). It is the sample file for
  the CSV upload path.

To create an empty DB with only the schema, run `python -m scripts.seed [path]`.

See `docs/data.md` for the schema, storage formats and CSV format.

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
app/schema.sql  SQLite schema (tables, indexes, append-only triggers)
app/db.py       connection, init_db(), typed insert/get helpers, row -> dataclass converters
scripts/generate_data.py   synthetic data generator (the only data source); `make seed`
scripts/seed.py            creates an empty DB from the schema
scripts/reset_db.py        deletes the DB file so `make seed` can start over; `make reset`
data/           generated CSVs (gitignored)
tests/
docs/           client_brief.md, meeting_notes.md, decisions.md, data.md
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
