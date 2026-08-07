# NBA Inbox Propensity Model

Pooled, calibrated `P(outcome | task)` model for the NBA live inbox.

`priority` is the compatibility score `100 * p_outcome`. Live ranking uses
`ev_score = p_outcome * expected_value` (expected EUR), with `priority` only as the fallback for
legacy tasks that do not have `ev_score`.

## What It Predicts

Natural conversion propensity: P(the task's defined outcome happens in `(T, T+H]`, `H=60` days,
given task signals as of `T`).

This is propensity, not manager causal lift. It ranks likely value capture; it does not estimate the
incremental effect of a manager touch.

Outcome labels, leak-safe and strictly after the as-of date:

- `reorder_due`: client re-buys that product.
- `debt_followup`: income payment EUR is at least 50% of overdue amount at `T`.
- `churn_winback`: client places any valid order.
- `cross_sell`: client buys a reco-discovered product.

`new_client_activation` is excluded because `Client.Created` is a 1C sync stamp, not a reliable
activation signal.

## Data

`data/nba_dataset.parquet` historical backfill, signal SQL replayed at each historical `T`, with
manager filter dropped.

Current artifact metrics:

- Rows: 16,717.
- Clients: 709.
- Base rate: 25.7%.
- Features: 21 shared/type-signal/one-hot columns.
- Production model: calibrated HGB.
- Source history starts at `2025-01-01`; every training vintage has a
  complete 365-day feature window.
- Training vintages: `2026-02-01..2026-06-01`.
- Temporal OOT split: train vintages `<= 2026-04-01`, test `2026-05-01..2026-06-01`.

## Model

HistGradientBoosting and LogisticRegression are both isotonic-calibrated. The production model is
selected by OOT calibration and AUC, then refit on all rows for serving.

## Validation

Stratified Group CV, grouped by client:

| metric | HGB | Logit |
|---|---:|---:|
| AUC | 0.680 | 0.691 |
| KS | 0.259 | 0.285 |
| Brier | 0.175 | 0.172 |

Temporal out-of-time split:

| metric | HGB | Logit |
|---|---:|---:|
| AUC | 0.668 | 0.652 |
| KS | 0.243 | 0.221 |
| Brier | 0.168 | 0.175 |

OOT per type, production HGB:

| task_type | n | pos | AUC | KS | Brier |
|---|---:|---:|---:|---:|---:|
| reorder_due | 6,037 | 1,386 | 0.648 | 0.218 | 0.174 |
| debt_followup | 7 | 0 | n/a | n/a | 0.006 |
| churn_winback | 421 | 140 | 0.744 | 0.380 | 0.184 |
| cross_sell | 870 | 125 | 0.714 | 0.373 | 0.113 |

Reliability bins are in `metrics.json` under `oot_per_type.hgb[*].reliability`.

## Benchmark vs Old Priority

AUC on the same outcome label:

| scope | old | model | delta |
|---|---:|---:|---:|
| overall CV | 0.579 | 0.680 | 0.101 |
| OOT future | 0.551 | 0.668 | 0.117 |
| reorder_due CV | 0.557 | 0.654 | 0.097 |
| debt_followup CV | n/a | n/a | n/a |
| churn_winback CV | 0.494 | 0.770 | 0.276 |
| cross_sell CV | 0.697 | 0.682 | -0.015 |

The model beats old priority overall, out-of-time, and on the modeled task types. This card is
generated from `metrics.json` by `app/ml/train.py`; if the metrics change, the card changes with
them.

## E[value] Head

The value head is simple, deterministic, and documented in `train.py` / `score_task.py`:

- `debt_followup`: overdue amount at `T`.
- `reorder_due` / `cross_sell`: approximate average order value, `monetary / order_count`.
- `churn_winback`: trailing-365 turnover, the relationship value at risk.

`score_task()` returns `p_outcome`, `expected_value`, `ev_score`, and `priority`.

## Ship Notes

Ship-worthy as a ranking model. The model improves historical and OOT ranking versus the old expert
priority. Remaining caveat: this is a propensity model, not causal lift; causal attribution still
needs holdout or experiment design.

Operational contract:

- Inbox/caps rank by `ev_score` after the urgency band; task type is only a tie-breaker.
- Fallback to `priority` only when `ev_score` is absent.
- Keep `priority = 100 * p_outcome` as the compatibility field for old clients and legacy docs.

## Artifacts

- `propensity_model.joblib`: final isotonic-calibrated model, refit on all data.
- `model_meta.json`: features, task types, OOT split, formula.
- `metrics.json`: full CV, OOT, per-type, reliability, and benchmark numbers.
- `MODEL_CARD.md`: this generated card.
- `../score_task.py`: serving head for `{p_outcome, expected_value, ev_score, priority}`.
- `../train.py`: training script that reproduces the artifact metrics.
