# NBA Inbox Propensity Model

Pooled, calibrated `P(outcome | task)` model that replaces the expert-guessed
`priority = 100·(0.5·urgency + 0.3·value + 0.2·confidence)` in `app/services/scoring.py`.

## What it predicts
Natural conversion propensity: P(the task's defined outcome happens in (T, T+H], H=60 days | task as-of T).
This is **propensity, NOT manager causal lift** — it ranks who is most likely to convert, not who is
most *moved* by a manager touch. Correct use: rank the inbox by `p_outcome × E[value]`.

Outcome labels (leak-safe, outcome strictly in (T, T+H]):
- reorder_due: client re-buys that product.
- debt_followup: IncomePaymentOrder EUR ≥ 50% of overdue@T.
- churn_winback: client places any valid order.
- cross_sell: client buys a reco-discovered (as-of T) product.

(`new_client_activation` dropped — Client.Created is a 1С sync stamp, not a real activation signal.)

## Data
`data/nba_dataset.parquet` — historical backfill, signal SQL replayed verbatim at each historical T
(Created<=:asof), per-manager filter dropped. 34,622 rows, 719 clients, 9 monthly vintages
2025-08 → 2026-04, H=60d. Base rate 26.5%.

## Features (all as-of T)
Shared client features (`monetary` = trailing-365d EUR turnover, `recency_days`, `order_count`)
+ 14 type-specific signals (0-filled outside owning type) + task_type one-hot.

## Model
HistGradientBoosting (production) vs LogisticRegression, both isotonic-calibrated.
HGB chosen: better OOT AUC and Brier. Calibration matters because the priority math needs true probabilities.

## Validation

Stratified Group CV (group = client, prevents same client across folds):

| metric | HGB | Logit |
|---|---|---|
| AUC | 0.720 | 0.716 |
| KS | 0.332 | 0.324 |
| Brier | 0.167 | 0.171 |

Temporal Out-of-Time split — train vintages ≤ 2026-01, test 2026-02..04 (the leakage defense):

| metric | HGB | Logit |
|---|---|---|
| AUC | 0.727 | 0.720 |
| KS | 0.348 | 0.334 |
| Brier | 0.162 | 0.167 |

OOT per-type (production HGB):

| task_type | n | pos | AUC | KS | Brier |
|---|---|---|---|---|---|
| reorder_due | 12389 | 3119 | 0.690 | 0.295 | 0.168 |
| debt_followup | 632 | 384 | 0.906 | 0.666 | 0.128 |
| churn_winback | 634 | 219 | 0.819 | 0.533 | 0.156 |
| cross_sell | 701 | 65 | 0.740 | 0.402 | 0.080 |

Reliability (OOT, per type) is in `metrics.json` (`oot_per_type.hgb[*].reliability`). reorder_due
(the dense mass) is well-calibrated (gaps within ±0.035). debt/churn show a mild upward gap in
sparse mid bins because paydown/reactivation rates trend up across the test vintages — an expected
cost of temporal extrapolation; recalibrate on a rolling window in production. cross_sell upper bins
are nearly empty (only 65 OOT positives) so AUC there is the more reliable signal than its curve.

## Benchmark vs OLD guessed priority (AUC on the same label)

| scope | old | model | Δ |
|---|---|---|---|
| overall (CV) | 0.557 | 0.720 | +0.163 |
| OOT future | 0.550 | 0.727 | +0.177 |
| reorder_due | 0.539 | 0.682 | +0.144 |
| debt_followup | 0.323 | 0.906 | +0.584 |
| churn_winback | 0.601 | 0.762 | +0.161 |
| cross_sell | 0.679 | 0.678 | −0.002 |

The model beats the old priority overall and on 3 of 4 types; cross_sell is a statistical tie
(its old urgency already tracked the reco score, which is most of the signal). Old debt_followup
priority was **worse than random** (0.323) — it ranked debts roughly inversely to actual paydown;
the model fixes this outright.

## E[value] head (EUR, not learned — simple + documented)
- debt_followup → overdue_amount @ T (cash directly at stake).
- reorder_due / cross_sell → expected line revenue ≈ avg order value = monetary / order_count.
- churn_winback → client monetary (trailing-365 turnover, relationship at risk).

`score_task()` returns `ev_score = p_outcome × expected_value` as the ranking key, plus a
0..100 `priority = 100·p_outcome` convenience score.

## Ship-worthiness
**Ship-worthy as a ranking model.** It cleanly beats the incumbent overall, OOT, and per-type
(except a cross_sell tie), holds AUC out-of-time, and is well-calibrated on the dense reorder mass.
Watch-items before/at integration: (1) recalibrate on a rolling window — debt/churn rates drift
upward over time; (2) cross_sell has thin positives (65 OOT) so treat its calibration as provisional;
(3) it is propensity, not causal lift — pair with a holdout/experiment later if causal attribution
is needed. Integration into `scoring.py` is the next phase (not done here).

## Artifacts
- `propensity_model.joblib` — final isotonic-calibrated HGB, refit on all data.
- `model_meta.json` — features, task types, OOT split, formula.
- `metrics.json` — full CV / OOT / per-type / reliability / benchmark numbers.
- `../score_task.py` — pure `{p_outcome, expected_value, ev_score, priority}` head.
- `../train.py` — reproduces everything.
