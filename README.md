# GBA NBA

FastAPI service that generates and manages Next Best Action tasks for the sales cockpit.

## Source-history contract

All source data is considered complete starting at `SOURCE_HISTORY_START_DATE`
(`2025-01-01` by default).

- Rolling signals use `effective_start = max(requested_start, source_history_start)`.
- Factual purchase-history signals use the available interval
  `[source_history_start, as_of)`.
- An `as_of` before the source boundary is rejected with HTTP 422.
- API and readiness responses expose `source_history_start`, `effective_start`, and
  `history_complete`; `history_complete=false` means the requested rolling window extends
  before available source history.
- Training snapshots require a complete 365-day feature window. Cache entries, datasets, and
  model metadata are stamped with the source boundary; artifacts without a matching stamp are
  rejected rather than silently reused.

Changing the boundary requires rebuilding the NBA dataset and passing the existing retraining
quality gates before the new model can be served.

## Model artifacts

The propensity model in `app/ml/artifacts/` (`propensity_model.joblib`, `model_meta.json`,
`metrics.json`, `MODEL_CARD.md`) is produced on the host and is not tracked in Git. The weekly
`gba-nba-retrain.timer` (`deploy/systemd/`) rebuilds the dataset, retrains, gates and swaps these
files in place, keeping the previous set in `app/ml/artifacts/backup_<ts>/`.

A fresh checkout has no model: task generation falls back to the deterministic heuristic, `/health`
reports `degraded` with `model_artifact_missing`, and `/ready` returns 503. Train the first model
with the same command the timer runs (requires the `.env` SQL and Mongo access):

```bash
.venv/bin/python scripts/retrain.py --auc-floor 0.62 --auc-epsilon 0.01
```

Model-dependent scoring tests are skipped until the model exists.

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
```

The dev database reconciliation is read-only:

```bash
make integration
```

## Seller contact history

`GET/POST /cockpit/contacts` and `GET /cockpit/contacts/history` accept the existing
internal `manager_net_uid` identity. Writes and history require the client's current,
nondeleted `MainManagerID` to match that user; head roles do not bypass ownership.
The queue also filters to this current client book. Previous managers' contact events
remain stored, but are not exposed to the new manager by this version.

Contact records use a separate `seller_contact_events` Mongo collection with **no TTL**.
AI regeneration, expiry and task cleanup do not delete contacts or complete tasks.
The queue selects the latest event per client in Mongo, filters/searches that set, and
returns paged rows plus full bucket counts. Commitments come first in date order;
unscheduled contacts follow. Today uses the Kyiv calendar, including DST boundaries.

A write requires a nonzero `event_id` UUID and explicit nullable `next_contact_at` and
`next_action` keys. Both must be present together or both null; an action cannot be blank.
Timestamps require an explicit offset and are normalized to UTC milliseconds. A new
commitment must be in the future. Retrying the same manager/event UUID and payload
returns the original event even after its deadline; a different payload returns 409.
The deterministic Mongo `_id` enforces this even if secondary index setup fails.
The server records contact time and client/manager identity; callers cannot backdate it.

`tests/test_seller_contacts.py` uses an isolated mongomock fixture by default. For real
Mongo pipeline checks, set `TEST_CONTACTS_MONGO_URI` **only to a disposable test Mongo**:
the tests drop `contacts_fixture_20261022.seller_contact_events` before and after each
case. Never point this option at a shared or business database.
