# Data: database and demo files

Reference for the storage layer (`app/schema.sql`, `app/db.py`) and the
synthetic data generator (`scripts/generate_data.py`). For why each choice was
made, see `docs/decisions.md`.

## Database

SQLite, plain `sqlite3`. `app/schema.sql` defines every table, index and
trigger. `db.init_db(conn)` runs it and is safe to call more than once.

| Table | One row per | Key columns |
|---|---|---|
| `policies` | policy (mirrors `Policy`) | `policy_number` (PK) |
| `claims` | claim (mirrors `Claim`) | `claim_id` (PK), indexed on `policy_number` |
| `check_runs` | run of the checks over a form submission or CSV batch | `run_id`, `started_at`, `source` (`form` / `csv`), `claim_count` |
| `check_results` | claim per run, with its outcome | `run_id`, `claim_id`, `outcome` (`READY_FOR_ADJUSTER` / `NEEDS_REVIEW`), `checked_at` |
| `flags` | flag raised on a claim in a run | `run_id`, `claim_id`, `rule_id`, `reason`, `inputs_json`, `created_at` |

### Storage formats

| Python type | Stored as | Example |
|---|---|---|
| `date` | ISO text | `2026-06-15` |
| `Decimal` (amounts, limits) | text, so it round-trips exactly | `1234.50` |
| `bool` | `INTEGER` 0 / 1 | `1` |
| `list[str]` (`attachments`) | JSON array | `["photo_front.jpg"]` |
| `Flag.inputs` | JSON object, sorted keys | `{"days": 31}` |
| timestamps | ISO 8601 UTC text | `2026-11-05T14:02:11+00:00` |

The schema has CHECK constraints for enum columns (`cover_type`, `loss_type`,
`submitted_via`, `source`, `outcome`). Free text is stored verbatim.

### Rules the schema enforces

- **History is append-only.** Triggers abort any `UPDATE` or `DELETE` on
  `check_results` and `flags`, and `app/db.py` has no helper that does either.
  Re-running checks creates a new run.
- **Claims can reference unknown policies.** `claims.policy_number` has no
  foreign key, because rule R0 flags that case.

### Helpers (`app/db.py`)

| Function | Does |
|---|---|
| `get_connection(path=None)` | opens `path`, else `CIC_DB_PATH`, else `claim_intake.db`; rows are `sqlite3.Row`; foreign keys on |
| `init_db(conn)` | creates the schema |
| `insert_policy`, `insert_claim` | store a `Policy` / `Claim` |
| `get_policy(conn, policy_number)` | `Policy` or `None` |
| `get_claims_on_policy(conn, policy_number)` | `list[Claim]`, ordered by `claim_id` |
| `claim_exists(conn, claim_id)` | `bool` |
| `insert_run(conn, source, claim_count)` | returns a new `run_id` |
| `insert_result(conn, run_id, claim_id, outcome)` | one `check_results` row |
| `insert_flag(conn, run_id, claim_id, flag)` | one `flags` row |
| `row_to_policy`, `row_to_claim`, `row_to_flag` | DB row back to the dataclass |

Helpers don't commit. Wrap them in a transaction:

```python
conn = db.get_connection()
with conn:
    db.insert_policy(conn, policy)
```

## Synthetic data

`make seed` (or `python -m scripts.generate_data [--db PATH] [--out DIR] [--seed N]`)
writes the DB and `data/policies.csv` and `data/claims.csv`. Output is the
same on every run. It is the only allowed source of policy and claim data.

- **Policies (200):** UK-style regs (`AB12 CDE`), about 60/25/15%
  comprehensive / tpft / tpo, 12-month terms starting between 2025-01-01 and
  2026-06-30, and about 10% cancelled mid-term.
- **Claims (500 in the DB):** 440 clean, plus tagged scenario and boundary
  claims. No claim is reported after the reference date of 2026-10-31.

### Scenario tags

Tags sit at the start of `description`. Search for them in the demo.

| Tag | Example | Where | Expected flag |
|---|---|---|---|
| `SCENARIO S1` | policy number not found (`POL-9xxxxx`) | DB + CSV | R0 |
| `SCENARIO S2` | loss 1 day before effective date | DB + CSV | R1 |
| `SCENARIO S3` | loss 1 day after expiry / on cancellation date | DB + CSV | R1 |
| `SCENARIO S4` | loss on expiry date | DB + CSV | none (expiry day covered by default) |
| `SCENARIO S5` | theft not covered by tpo | DB + CSV | R1b |
| `SCENARIO S6` | late report 31 days | DB + CSV | R2 |
| `SCENARIO S7` | duplicate of CLM-000123 | DB + CSV | R3 |
| `SCENARIO S8` | original submission / resubmission of CLM-… | original in DB, resubmission CSV only | intake: skip and log |
| `SCENARIO S9` | loss_date not a real date | CSV only | intake: needs review |
| `SCENARIO S10` | claimed amount above policy limit | DB + CSV | none yet (R4 is stretch) |
| `BOUNDARY` | loss on effective date; report at 29 / 30 days | DB + CSV | none |

`tests/test_generate_data.py` runs every rule over every DB claim and checks
that each tagged claim raises exactly the flag in this table. Untagged claims
must raise nothing.

### CSV format

Columns use the dataclass field names, in the same order. Dates are ISO,
booleans are `true` / `false`, `attachments` is `;`-separated, and an empty
cell means null. `data/claims.csv` has 512 rows: the 500 DB claims, 6 S8
resubmissions and 6 S9 malformed rows.
