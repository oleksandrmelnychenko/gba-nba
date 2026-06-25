"""Train a pooled, calibrated P(outcome|task) propensity model for the NBA inbox.

Reads data/nba_dataset.parquet (pooled, 4 task_types, leak-safe label, vintage column).
Produces a calibrated probability head + a simple E[value] head, and persists everything
under app/ml/artifacts/ so app/ml/score_task.py can serve {p_outcome, expected_value, priority}.

CAVEAT (see model card): the label is NATURAL conversion in (T, T+H] -- a propensity, NOT a
manager causal lift. Correct for ranking the inbox by P(outcome) x E[value]; it does not
estimate the incremental effect of a manager touch.
"""
from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).resolve().parent
ART = HERE / "artifacts"
ART.mkdir(parents=True, exist_ok=True)
DATA = HERE.parent.parent / "data" / "nba_dataset.parquet"

TASK_TYPES = ["reorder_due", "debt_followup", "churn_winback", "cross_sell"]

# Feature columns fed to the model. Shared client features + all type signals + task one-hot.
# Non-owning type signals are 0-filled in the dataset, so the model can learn per-type structure
# via the one-hot interaction with the trees (HGB) / additively (logistic).
SHARED = ["monetary", "recency_days", "order_count"]
SIGNALS = [
    "sig_overdue_amount", "sig_days_past_terms", "sig_max_overdue_days", "sig_debt_lines",
    "sig_elapsed_days", "sig_cycle_days", "sig_overdue_ratio", "sig_n_orders",
    "sig_drop_ratio", "sig_silence_days", "sig_recent_orders", "sig_prior_orders",
    "sig_top_score", "sig_reco_candidates",
]
ONEHOT = ["is_reorder_due", "is_debt_followup", "is_churn_winback", "is_cross_sell"]
FEATURES = SHARED + SIGNALS + ONEHOT

OOT_TRAIN_MAX = "2026-01-01"   # train vintages <= this
OOT_TEST_LO = "2026-02-01"
OOT_TEST_HI = "2026-04-01"


def ks(y_true: np.ndarray, p: np.ndarray) -> float:
    """Kolmogorov-Smirnov separation between positive/negative score distributions."""
    pos = np.sort(p[y_true == 1])
    neg = np.sort(p[y_true == 0])
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    grid = np.sort(np.concatenate([pos, neg]))
    cdf_pos = np.searchsorted(pos, grid, side="right") / len(pos)
    cdf_neg = np.searchsorted(neg, grid, side="right") / len(neg)
    return float(np.max(np.abs(cdf_pos - cdf_neg)))


def reliability(y_true: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        if m.sum() == 0:
            continue
        out.append({"bin": b, "n": int(m.sum()),
                    "p_mean": float(p[m].mean()), "y_rate": float(y_true[m].mean())})
    return out


def safe_auc(y: np.ndarray, p: np.ndarray) -> float:
    if len(np.unique(y)) < 2:
        return float("nan")
    return float(roc_auc_score(y, p))


def build_hgb() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        learning_rate=0.06, max_iter=400, max_leaf_nodes=31,
        min_samples_leaf=60, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.15, random_state=42,
    )


def build_logit() -> Pipeline:
    return Pipeline([
        ("scale", StandardScaler()),
        ("clf", LogisticRegression(max_iter=2000, C=0.5, class_weight=None)),
    ])


def grouped_cv_eval(X: pd.DataFrame, y: np.ndarray, groups: np.ndarray, make_model, n_splits=5):
    """Stratified group CV (group=client). Out-of-fold predictions -> honest AUC/KS/Brier."""
    sgkf = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=42)
    oof = np.full(len(y), np.nan)
    for tr, va in sgkf.split(X, y, groups):
        m = make_model()
        m.fit(X.iloc[tr], y[tr])
        oof[va] = m.predict_proba(X.iloc[va])[:, 1]
    mask = ~np.isnan(oof)
    return oof, {
        "auc": safe_auc(y[mask], oof[mask]),
        "ks": ks(y[mask], oof[mask]),
        "brier": float(brier_score_loss(y[mask], oof[mask])),
    }


def expected_value_row(r: pd.Series) -> float:
    """Simple, documented E[value] per task (EUR). Used by the priority formula, not learned.

    debt_followup : overdue_amount at T (cash directly at stake).
    reorder_due / cross_sell : expected line revenue ~ client avg order value (trailing-365
                   turnover / order_count), the grounded proxy for one re-bought / cross-sold line.
    churn_winback : client monetary (trailing-365 EUR turnover) -- the relationship at risk.
    """
    tt = r["task_type"]
    monetary = float(r["monetary"])
    oc = float(r["order_count"])
    aov = monetary / oc if oc > 0 else 0.0
    if tt == "debt_followup":
        return float(r["sig_overdue_amount"])
    if tt in ("reorder_due", "cross_sell"):
        return aov
    if tt == "churn_winback":
        return monetary
    return monetary


def main() -> None:
    df = pd.read_parquet(DATA)
    df["vd"] = pd.to_datetime(df["vintage"])
    X = df[FEATURES].astype(float)
    y = df["label"].to_numpy().astype(int)
    groups = df["client_id"].to_numpy()

    report: dict = {"n_rows": int(len(df)), "n_clients": int(df["client_id"].nunique()),
                    "features": FEATURES, "base_rate": float(y.mean())}

    # ----------------------------------------------------------------- grouped CV (model select)
    print("=== Stratified Group CV (group=client) ===")
    cv = {}
    oof_hgb, m_hgb = grouped_cv_eval(X, y, groups, build_hgb)
    oof_lr, m_lr = grouped_cv_eval(X, y, groups, build_logit)
    cv["hgb"] = m_hgb
    cv["logit"] = m_lr
    print("  HGB  ", m_hgb)
    print("  LOGIT", m_lr)

    # per-type CV AUC for both, from OOF
    df["_oof_hgb"] = oof_hgb
    df["_oof_lr"] = oof_lr
    cv_per_type = {}
    for tt in TASK_TYPES:
        g = df[df["task_type"] == tt]
        cv_per_type[tt] = {
            "n": int(len(g)), "pos_rate": float(g["label"].mean()),
            "auc_hgb": safe_auc(g["label"].to_numpy(), g["_oof_hgb"].to_numpy()),
            "auc_logit": safe_auc(g["label"].to_numpy(), g["_oof_lr"].to_numpy()),
        }
    report["cv"] = cv
    report["cv_per_type"] = cv_per_type

    # ----------------------------------------------------------------- temporal OOT split
    tr_mask = df["vd"] <= OOT_TRAIN_MAX
    te_mask = (df["vd"] >= OOT_TEST_LO) & (df["vd"] <= OOT_TEST_HI)
    Xtr, ytr = X[tr_mask], y[tr_mask]
    Xte, yte = X[te_mask], y[te_mask]
    print(f"\n=== Temporal OOT split: train n={tr_mask.sum()} test n={te_mask.sum()} ===")

    def fit_calibrated(make_model):
        # Calibrate with isotonic via internal CV on the TRAIN fold only (no OOT leakage).
        base = make_model()
        cal = CalibratedClassifierCV(base, method="isotonic", cv=5)
        cal.fit(Xtr, ytr)
        return cal

    oot = {}
    cal_models = {}
    for name, maker in (("hgb", build_hgb), ("logit", build_logit)):
        cal = fit_calibrated(maker)
        cal_models[name] = cal
        p = cal.predict_proba(Xte)[:, 1]
        oot[name] = {
            "auc": safe_auc(yte, p), "ks": ks(yte, p),
            "brier": float(brier_score_loss(yte, p)),
        }
        print(f"  {name:5s} OOT", oot[name])

    # per-type OOT metrics + reliability for each model
    oot_per_type = {"hgb": {}, "logit": {}}
    df_te = df[te_mask].copy()
    for name in ("hgb", "logit"):
        p_all = cal_models[name].predict_proba(Xte)[:, 1]
        df_te[f"_p_{name}"] = p_all
        for tt in TASK_TYPES:
            g = df_te[df_te["task_type"] == tt]
            yv = g["label"].to_numpy()
            pv = g[f"_p_{name}"].to_numpy()
            oot_per_type[name][tt] = {
                "n": int(len(g)), "pos": int(yv.sum()),
                "auc": safe_auc(yv, pv), "ks": ks(yv, pv),
                "brier": float(brier_score_loss(yv, pv)) if len(yv) else float("nan"),
                "reliability": reliability(yv, pv, bins=8),
            }
    report["oot"] = oot
    report["oot_per_type"] = oot_per_type

    # ----------------------------------------------------------------- vs OLD priority benchmark
    # old_priority is a 0..100 score; AUC against the same label measures ranking power.
    old = df["old_priority"].to_numpy()
    bench = {
        "overall": {
            "auc_old": safe_auc(y, old),
            "auc_model_cv_hgb": safe_auc(y, oof_hgb),
            "auc_model_cv_logit": safe_auc(y, oof_lr),
        },
        "per_type": {},
        "oot": {},
    }
    for tt in TASK_TYPES:
        g = df[df["task_type"] == tt]
        bench["per_type"][tt] = {
            "auc_old": safe_auc(g["label"].to_numpy(), g["old_priority"].to_numpy()),
            "auc_model_cv_hgb": safe_auc(g["label"].to_numpy(), g["_oof_hgb"].to_numpy()),
            "auc_model_cv_logit": safe_auc(g["label"].to_numpy(), g["_oof_lr"].to_numpy()),
        }
    # OOT benchmark: old priority vs production model on the held-out future
    bench["oot"]["auc_old"] = safe_auc(yte, old[te_mask.to_numpy()])
    report["benchmark"] = bench

    # ----------------------------------------------------------------- pick production model
    # Prefer better OOT calibration (Brier) since priority math needs true probabilities;
    # require it not to lose meaningful OOT AUC.
    auc_hgb, auc_lr = oot["hgb"]["auc"], oot["logit"]["auc"]
    br_hgb, br_lr = oot["hgb"]["brier"], oot["logit"]["brier"]
    if (br_hgb <= br_lr + 0.002) and (auc_hgb >= auc_lr - 0.01):
        prod_name = "hgb"
    elif (br_lr < br_hgb) and (auc_lr >= auc_hgb - 0.01):
        prod_name = "logit"
    else:
        prod_name = "hgb" if auc_hgb >= auc_lr else "logit"
    report["production_model"] = prod_name
    bench["oot"]["auc_model"] = oot[prod_name]["auc"]
    print(f"\n=== Production model: {prod_name} ===")

    # ----------------------------------------------------------------- final fit on ALL data
    # Refit the chosen calibrated model on the full dataset for serving.
    maker = build_hgb if prod_name == "hgb" else build_logit
    final = CalibratedClassifierCV(maker(), method="isotonic", cv=5)
    final.fit(X, y)
    joblib.dump(final, ART / "propensity_model.joblib")

    meta = {
        "production_model": prod_name,
        "features": FEATURES,
        "task_types": TASK_TYPES,
        "calibration": "isotonic",
        "oot_split": {"train_max": OOT_TRAIN_MAX, "test": [OOT_TEST_LO, OOT_TEST_HI]},
        "value_head": "see expected_value_row in train.py / score_task.py",
        "priority_formula": "priority = 100 * p_outcome_normalized; rank by p_outcome * expected_value",
    }
    (ART / "model_meta.json").write_text(json.dumps(meta, indent=2))
    (ART / "metrics.json").write_text(json.dumps(report, indent=2, default=float))

    print("\nArtifacts written to", ART)
    print(json.dumps({"cv": cv, "oot": oot, "production_model": prod_name,
                      "bench_overall": bench["overall"], "bench_oot": bench["oot"]},
                     indent=2, default=float))


if __name__ == "__main__":
    main()
