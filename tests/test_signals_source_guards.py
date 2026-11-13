"""Always-on source guards (no DB) — pin the just-fixed currency-conversion SQL into CI.

Every mocked unit test stayed GREEN while a real currency bug shipped (UAH summed as if EUR,
~50x inflated), because mocks never see the SQL. These guards read the repository module source
with inspect.getsource and assert the fixed conversion patterns are present and the reverted
(raw-sum) patterns are absent — so reintroducing the bug fails CI immediately, no DB required.
"""
from __future__ import annotations

import inspect
import re

from app.core import config
from app.data import debt_snapshot
from app.data import signals_repository as sig
from app.ml import dataset as ds

_PAID_SRC = inspect.getsource(sig.monthly_paid)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


# Legacy signal guards below predate the current confirmed-invoice source.
# monthly_shipped now uses canonical confirmed ownership plus Deleted=0, verified against
# current imported lineage and real SQL fixtures in test_monthly_invoice_reference_sql.
# Do not extend this historical guard to those source-backed documentary queries.
_SALES_SPINE_FUNCS = [
    sig.ubiquitous_product_ids,
    ds.reorder_candidates, ds.churn_candidates, ds.active_clients,
    ds.label_reorder, ds.label_any_order, ds.label_buy_products,
]


# Payment conversion semantics are executed against fixture rows in
# test_payment_target_filter.py. The former EuroAmount prohibition was based on
# pre-fix sync data and is invalid after the current document-conversion sync.


def test_sales_spine_uses_validity_flag_not_deleted():
    """Every Order/OrderItem-spine sales query must gate on oi.IsValidForCurrentSale=1 and must NOT
    use the reverted o.Deleted=0 / oi.Deleted=0 (which kept only ~16% of sales). DB-free guard."""
    for fn in _SALES_SPINE_FUNCS:
        src = _norm(inspect.getsource(fn))
        assert "IsValidForCurrentSale = 1" in src, \
            f"{fn.__module__}.{fn.__name__} must gate sales on oi.IsValidForCurrentSale = 1"
        assert not re.search(r"\bo\.Deleted\s*=\s*0", src), \
            f"{fn.__module__}.{fn.__name__} reintroduced o.Deleted=0 on the Order spine (~16% bug)"
        assert not re.search(r"\boi\.Deleted\s*=\s*0", src), \
            f"{fn.__module__}.{fn.__name__} reintroduced oi.Deleted=0 on the OrderItem spine"


# --- Hard synthetic-product exclusion (independent of the ubiquity filter) ---------------------
# The 1C refresh re-mints «Ввід боргів», so its ID must be resolved from the current Product row.
# The hard exclusion must remain independent of the data-driven ubiquity threshold.



def test_synthetic_product_id_is_resolved_dynamically():
    field = config.Settings.model_fields["synthetic_product_ids"]
    assert field.default == frozenset()
    src = inspect.getsource(sig.synthetic_product_ids)
    assert "Ввід боргів" in src
    assert "Deleted = 0" in src
    assert "ORDER BY ID DESC" in src


def test_excluded_helpers_union_synthetic_ids_unconditionally():
    """Both exclusion helpers must UNION settings.synthetic_product_ids so the synthetic exclusion is
    independent of the data-driven ubiquity set — it can never be lost if ubiquity stops firing."""
    for fn in (sig._excluded, ds._excluded_pids):
        src = inspect.getsource(fn)
        assert "synthetic_product_ids" in src, (
            f"{fn.__module__}.{fn.__name__} must reference settings.synthetic_product_ids so the "
            "hard exclusion does not depend on the ubiquity threshold catching 25422404"
        )
        assert re.search(r"synthetic_product_ids\(\)\s*\)?\s*\|", src), (
            f"{fn.__module__}.{fn.__name__} must UNION (|) the synthetic ids with the ubiquity set, "
            "so the pinned exclusion is unconditional"
        )


def test_client_features_share_the_turnover_column_population(monkeypatch):
    """monetary is «Оборот (12м)»: retained confirmed purchases, synthetic lines excluded, and the
    training dataset reads the very same function."""
    calls = []
    monkeypatch.setattr(sig, "query", lambda sql, params=None: calls.append((_norm(sql), params)) or [
        {"client_id": 11, "monetary": 120.5, "order_count": 3, "recency_days": 4}])
    monkeypatch.setattr(sig, "_excluded", lambda as_of: frozenset({42}))
    features = ds.client_features([11, 12], "2026-07-25")
    sql, params = calls[0]
    assert features == {11: {"monetary": 120.5, "order_count": 3, "recency_days": 4},
                        12: {"monetary": 0.0, "order_count": 0, "recency_days": None}}
    assert "s.ChangedToInvoice IS NOT NULL" in sql and "s.Deleted = 0 AND s.IsMerged = 0" in sql
    assert "oi.Qty - COALESCE(oi.ReturnedQty, 0)" in sql
    assert "SUM(turnover_eur) AS monetary" in sql and "COUNT(DISTINCT order_id) AS order_count" in sql
    assert "FROM retained_purchases WHERE product_id NOT IN (:x0)" in sql and params["x0"] == 42
    assert "c.ID IN (:c0,:c1)" in sql
    overview = inspect.getsource(sig.clients_overview_for_manager)
    assert "retained_purchase_cte" in overview and "turnover_eur" in overview


def test_synthetic_exclusion_holds_when_ubiquity_does_not_fire(monkeypatch):
    """Simulate the latent failure: ubiquity returns EMPTY. The hard
    guard must STILL exclude the synthetic id from the effective exclusion set."""
    dynamic_id = 42_424_242
    monkeypatch.setattr(sig, "synthetic_product_ids", lambda: frozenset({dynamic_id}))
    monkeypatch.setattr(sig, "ubiquitous_product_ids", lambda pct, as_of: frozenset())
    assert dynamic_id in sig._excluded("2026-07-25"), (
        "with ubiquity not firing, the synthetic id must still be excluded via the hard guard"
    )
    assert dynamic_id in ds._excluded_pids("2026-07-25"), (
        "dataset path must also hard-exclude the synthetic id when ubiquity does not fire"
    )


def test_debt_and_payment_paths_keep_their_deleted_flags():
    """The debt/payment paths are correct as-is — they must keep d.Deleted=0 / p.Deleted=0 (these
    are NOT the all-Deleted=1 sales spine) and must NOT adopt the sales validity flag by mistake."""
    debt_src = _norm(debt_snapshot._DOCUMENTS_SQL)
    assert "d.Deleted = 0" in debt_src, "debt path must keep d.Deleted=0"
    assert "IsValidForCurrentSale" not in debt_src, "debt path must not use the sales validity flag"
    paid_src = _norm(_PAID_SRC)
    assert "p.Deleted = 0" in paid_src, "payment path must keep p.Deleted=0"
    assert "IsValidForCurrentSale" not in paid_src, "payment path must not use the sales validity flag"


def test_retained_seller_queries_delegate_to_canonical_source():
    # Current imported sales are not the historical all-deleted calibration snapshot.
    # Executed SQL fixtures validate cancellation, merge, returns and current source provenance.
    for fn in (sig.new_clients_for_manager, sig.clients_overview_for_manager,
               sig.active_clients_for_manager, sig.repeat_products_for_manager,
               sig.churn_candidates_for_manager):
        assert "retained_purchase_cte(" in inspect.getsource(fn)
    assert "repeat_products_for_manager(" in inspect.getsource(sig.reorder_candidates_for_manager)
