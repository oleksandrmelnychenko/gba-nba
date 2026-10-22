"""Confirmed invoice gross: an order draft date and later returns cannot shift its month."""

from decimal import Decimal

import pytest

from app.data import signals_repository as sig
from tests.test_purchase_history_sql import sql_fixture as _sql_fixture

sql_fixture = _sql_fixture


def test_monthly_invoice_reference_uses_confirmed_events_and_does_not_subtract_later_returns(
    sql_fixture, monkeypatch
):
    db, query = sql_fixture
    monkeypatch.setattr(sig, "query", query)
    monkeypatch.setattr(sig, "_excluded", lambda day: [])
    with pytest.raises(ValueError, match="invoice_event_date_unverified"):
        sig.monthly_shipped(1, "2025-01-01", "2026-10-02")
    db.exec_driver_sql("DELETE #Lineage WHERE LocalSaleID=3 AND SourceSaleID=0x04")
    result = sig.monthly_shipped(1, "2025-01-01", "2026-10-02")
    assert result == {"2026-10": Decimal(800)}
    db.exec_driver_sql("UPDATE #OrderItem SET ReturnedQty=Qty WHERE ID IN (1,2,3,6,8)")
    assert sig.monthly_shipped(1, "2025-01-01", "2026-10-02") == result
