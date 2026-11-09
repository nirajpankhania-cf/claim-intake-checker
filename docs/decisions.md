# Decisions log

| Date | Decision | Source |
|---|---|---|
| 2026-11-04 | Domain is car insurance; team defines the data shape; synthetic data only | Karn, meeting |
| 2026-11-04 | Tool flags and reports only. No approve/deny/pay actions anywhere in code or UI | Brief; confirmed by Karn |
| 2026-11-04 | Two outcomes per claim: READY_FOR_ADJUSTER (no flags) or NEEDS_REVIEW (≥1 flag) | Karn, meeting |
| 2026-11-04 | History records every claim's outcome per run, not just flags (leadership view) | Karn, meeting |
| 2026-11-04 | Claimed amount is captured and displayed, not verified | Karn, meeting |
| 2026-11-04 | Primary intake is one claim via form; CSV upload is the batch path | Karn, meeting |
| 2026-11-04 | Attachments stored as file names only; never opened or parsed | Team |
| 2026-11-04 | Default `REPORTING_WINDOW_DAYS = 30` — pending Karn | Team default |
| 2026-11-04 | Default `EXPIRY_DAY_COVERED = True` — pending Karn | Team default |
| 2026-11-04 | Duplicate = different claim_id, same policy_number + loss_date; later claim gets the flag, reason names the earlier one | Team |
| 2026-11-04 | Re-submitted claim_id is skipped and logged, not re-checked. **Superseded 2026-11-09** (overwrite and re-check) | Team |
| 2026-11-04 | Re-running checks creates a new run; nothing is updated or deleted | Team |
| 2026-11-05 | R1 and R1b return nothing when the policy is missing; R0 is the only flag for an unknown policy | Niraj, PR #3 |
| 2026-11-05 | A cover type absent from config covers nothing (flags, doesn't crash) | Niraj, PR #3 |
| 2026-11-05 | R4 amount-over-limit is stretch only, pending Karn's answer | Team |
| 2026-11-05 | Schema lives in `app/schema.sql`; `app/db.py` uses plain `sqlite3`, no ORM | Team, PR #5 |
| 2026-11-05 | Dates stored as ISO text; money stored as text so `Decimal` round-trips exactly | Team, PR #5 |
| 2026-11-05 | `check_results` and `flags` are append-only, enforced by DB triggers as well as by having no UPDATE/DELETE helpers | Team, PR #5 |
| 2026-11-05 | `claims.policy_number` has no foreign key to `policies`: a claim on an unknown policy is valid input that R0 flags | Team, PR #5 |
| 2026-11-05 | DB helpers don't commit; the caller owns the transaction (`with conn:`) | Team, PR #5 |
| 2026-11-05 | Generator is deterministic (seed 4242, reference date 2026-10-31); generated `data/` and `*.db` are not committed | Team, PR #6 |
| 2026-11-05 | Scenario claims are tagged `SCENARIO Sn:` in `description` for demo use only; rules never read them | Team, PR #6 |
| 2026-11-05 | S8 resubmissions and S9 malformed rows exist only in `data/claims.csv`, since the DB can't store them | Team, PR #6 |
| 2026-11-05 | Clean generated claims are reported within 21 days and use `COVERED_LOSS_TYPES`, so they stay clean if the window changes | Team, PR #6 |
| 2026-11-05 | `check_results.claim_id` and `flags.claim_id` have no foreign key to `claims`: intake records rows that never became claims (S8 resubmissions, S9 malformed rows) | Team, CIC-29 |
| 2026-11-05 | S8 and S9 rows are recorded as a `NEEDS_REVIEW` result plus an `INTAKE` flag with the reason, the line number and the raw row; S8 names the earlier run (or earlier line in the same file). No schema change | Team, CIC-29 |
| 2026-11-05 | Intake rows are keyed `ROW-<run_id>-<line_no>` when the claim_id is missing, is a resubmission, or is already used by a stored claim, so they never share a key with a real claim | Team, CIC-29 |
| 2026-11-05 | Intake trims and uppercases `claim_id` and `policy_number` before validating and before the S8 check; other free text is stored verbatim | Team, CIC-29 |
| 2026-11-05 | Intake validates formats only (required fields, strict `YYYY-MM-DD`, allowed values matched case-insensitively, plain decimals); date logic stays in the rules. CSV columns must match exactly, in any order | Team, CIC-29 |
| 2026-11-05 | Default `MAX_INTAKE_BYTES = 1 MiB`, for uploaded files and pasted text alike | Team default, CIC-29 |
| 2026-11-05 | Run contract: `create_run(conn, source, claim_count)` (intake) → `run_checks(conn, run_id, claim_ids)` (runner, CIC-28, no commit inside) → `record_intake_flags(conn, run_id, rows)` (intake), all in one transaction | Team, CIC-29 |
| 2026-11-05 | Review queue lists every `NEEDS_REVIEW` result that has flags, newest run first, one row per claim per run. `INTAKE` rows (S8, S9) are included | Team, CIC-30 |
| 2026-11-05 | Queue filters: rule, flag date (UTC date of the flag, both ends inclusive) and run. Status filter waits for reviewed status in CIC-32 | Team, CIC-30 |
| 2026-11-05 | Review and claim pages are read-only GET pages with no actions; filters are a plain GET form, no JavaScript needed | Team, CIC-30 |
| 2026-11-05 | History lists every result, passed or flagged, newest run first, one row per claim per run. Filters: run, check date (UTC date of the result, both ends inclusive) and outcome | Team, CIC-31 |
| 2026-11-05 | History CSV export has the page's rows and filters: one row per claim per run, rule ids joined with `;`, reasons joined with ` \| `. UTF-8 with a BOM so Excel reads it correctly. If a run recorded a claim twice, the latest result wins | Team, CIC-31 |
| 2026-11-05 | CSV export cells whose first non-space character is `=`, `+`, `-` or `@`, or that start with a tab, carriage return or newline, get a leading `'` so spreadsheets don't run submitted text as a formula | Team, CIC-31 |
| 2026-11-05 | Single-claim form uses the same field checks as a CSV row (`intake.parse_claim_fields`). Invalid input re-shows the form with an error beside each field and stores nothing; it is not recorded as S9 | Team, CIC-38 |
| 2026-11-05 | A form claim_id already in use (a stored claim, or any key in the check history such as a `ROW-` key) is refused with a link, and nothing is stored (S8 on the form). CSV still records S8 as an `INTAKE` flag and uses the same in-use check. **Superseded 2026-11-09** (form confirms, then replaces; history-only ids don't block) | Team, CIC-38 |
| 2026-11-05 | `third_party_involved` is a required Yes/No choice on the form with no default, so an unanswered question is an error, not a silent No | Team, CIC-38 |
| 2026-11-05 | A form submission is its own run (`source = form`, `claim_count = 1`) and redirects to `/review?run_id=…`, whose banner states the claim's outcome | Team, CIC-38 |
| 2026-11-06 | Runner: "earlier" for R3 means stored earlier (SQLite insertion order), not lower claim_id, since form claim_ids are free-form. `existing_claims` is the earlier claims on the same policy | Team, CIC-28 |
| 2026-11-06 | Runner raises `LookupError` if given a claim_id that isn't stored, so intake's transaction rolls back instead of recording a partial run | Team, CIC-28 |
| 2026-11-06 | Re-runs are idempotent per run: one `check_results` row per (run, claim) and one `flags` row per (run, claim, rule), enforced by unique indexes; `run_checks` skips claims already checked in the run. Re-checking later means a new run | Team, CIC-28 (plan CIC-8) |
| 2026-11-09 | **Assumption, to confirm with Karn after the demo:** a claim submitted again with an existing `claim_id` overwrites the stored claim with the new submission and is checked again in a new run, on CSV, form and desk alike. The `INTAKE` "resubmission" flag no longer exists; earlier results are kept | Team, pending Karn |
| 2026-11-09 | Version trail: when a resubmission changes at least one field, the superseded claim is saved to `claim_versions` (append-only, enforced by triggers) before `claims` is updated. An identical resubmission writes no version, only a new result. `claims` always holds the current version; the update is a plain `UPDATE`, so the claim keeps its submission order for R3 | Team |
| 2026-11-09 | Same `claim_id` more than once in one CSV: the row with the most recent `report_date` wins; a tie goes to the last row. The other rows are dropped, not stored, and only counted (`repeats`) | Niraj |
| 2026-11-09 | Across uploads the new submission always wins, whatever its `report_date`. **Assumption**, to confirm with Karn | Team, pending Karn |
| 2026-11-09 | A `claim_id` that has check results but no stored claim (a malformed row recorded under its id, or a `ROW-` key) does not block a valid submission of that id; it is earlier history for the id. `db.claim_id_in_use` removed | Team |
| 2026-11-09 | Form: a claim_id already on file re-shows the form as a confirmation (200) naming the fields that differ (labels only, never values) with a "Replace and re-check" button. The hidden `replace` token carries the claim_id, and the overwrite only happens when it matches the id submitted, so editing the id on that page can't replace a different claim | Niraj |
| 2026-11-09 | `make reset` (`scripts/reset_db.py`) deletes the DB file and seeds again, for trying behaviour from a clean state. Development only; the web app has no delete path | Niraj |
| 2026-11-09 | Deleting a claim is deferred to a separate issue. A hard delete conflicts with append-only history; if wanted, the likely shape is a "withdrawn" marker that hides the claim and keeps every result | Niraj |

## Scenarios

| # | Scenario | Rule | Outcome |
|---|---|---|---|
| S1 | Policy number not found | R0 | Needs review |
| S2 | Loss before policy effective date | R1 | Needs review |
| S3 | Loss after expiry or on/after cancellation | R1 | Needs review |
| S4 | Loss on the expiry date itself | R1 | Covered by default — pending Karn |
| S5 | Loss type not covered by cover type | R1b | Needs review |
| S6 | Reported more than 30 days after loss | R2 | Needs review |
| S7 | Different claim ID, same policy + loss date | R3 | Needs review |
| S8 | Same claim ID submitted again | intake + all rules | Stored claim overwritten (old version kept when anything changed) and checked again; outcome from the rules. Assumption pending Karn |
| S9 | Required field missing or date unparseable | intake | Needs review (`INTAKE` flag) |
| S10 | Claimed amount above policy limit | R4 | Stretch — pending Karn |
| S11 | None of the above | — | Ready for adjuster |

## Open questions sent to Karn (4 Nov)

1. Is a loss on the expiry date itself covered?
2. Is 30 days the right reporting window?
3. Should amount above policy limit be flagged at intake?
