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
(Created<=:asof), per-manager filter dropped. 35,943 rows, 725 clients, 9 monthly vintages
2025-08 → 2026-04, H=60d. Base rate 26.6%.

**cross_sell coverage fix (2026-06):** the original backfill thinned cross_sell on the later
vintages because the live reco service (:8000) was contended (8s-timeout graceful skips) — Nov–Apr
had 51–407 rows/vintage with hundreds of active clients silently dropped (Jan was the worst, 51 of
~423). The per-(client, as-of) reco responses are persisted to `data/reco_cache/`, so the cache was
completed by replaying the missing pairs serially against an uncontended reco (1,333 new non-empty
responses) and the dataset rebuilt purely from cache hits. cross_sell is now uniformly covered
(391–460 rows/vintage, n 2,453 → 3,787). The pooled cross_sell H60 base rate is **9.0%** (was 9.7%):
the thinned-out clients converted at a *lower* rate, so completing coverage slightly *lowered* the
rate rather than raising it toward the ~20% that had been assumed — natural cross-sell conversion in
this book is genuinely ~9%, the earlier figure was upward-biased by the well-covered early vintages.

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
| AUC | 0.724 | 0.723 |
| KS | 0.332 | 0.329 |
| Brier | 0.164 | 0.167 |

Temporal Out-of-Time split — train vintages ≤ 2026-01, test 2026-02..04 (the leakage defense):

| metric | HGB | Logit |
|---|---|---|
| AUC | 0.727 | 0.726 |
| KS | 0.348 | 0.341 |
| Brier | 0.159 | 0.162 |

OOT per-type (production HGB):

| task_type | n | pos | AUC | KS | Brier |
|---|---|---|---|---|---|
| reorder_due | 12389 | 3119 | 0.682 | 0.289 | 0.169 |
| debt_followup | 625 | 377 | 0.909 | 0.660 | 0.126 |
| churn_winback | 634 | 219 | 0.820 | 0.538 | 0.156 |
| cross_sell | 1318 | 114 | 0.711 | 0.370 | 0.076 |

Reliability (OOT, per type) is in `metrics.json` (`oot_per_type.hgb[*].reliability`). reorder_due
(the dense mass) is well-calibrated (gaps within ±0.035). debt/churn show a mild upward gap in
sparse mid bins because paydown/reactivation rates trend up across the test vintages — an expected
cost of temporal extrapolation; recalibrate on a rolling window in production. After the coverage
fix cross_sell has 1,318 OOT rows / 114 positives (was 701 / 65); its CV AUC improved 0.678 → 0.689
on the fuller, more representative pool, while its OOT AUC reads 0.711 (was 0.740) because the test
slice is now nearly double the size and includes the previously-skipped harder clients — the larger
CV estimate is the more reliable read. Pooled OOT AUC is unchanged (0.727) and pooled OOT Brier
improved (0.162 → 0.159).

## Benchmark vs OLD guessed priority (AUC on the same label)

| scope | old | model | Δ |
|---|---|---|---|
| overall (CV) | 0.571 | 0.724 | +0.153 |
| OOT future | 0.566 | 0.727 | +0.161 |
| reorder_due | 0.539 | 0.681 | +0.142 |
| debt_followup | 0.320 | 0.907 | +0.587 |
| churn_winback | 0.601 | 0.753 | +0.152 |
| cross_sell | 0.672 | 0.689 | +0.017 |

The model beats the old priority overall and on all 4 types; the cross_sell edge (+0.017, was a tie
before the coverage fix) is now positive on the fuller pool — its old urgency already tracked the reco
score, which is most of the signal. Old debt_followup priority was **worse than random** (0.320) — it
ranked debts roughly inversely to actual paydown; the model fixes this outright.

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
