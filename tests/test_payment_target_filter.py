"""Execute the payment aggregation against fixture rows, preserving its joins and filters."""
import sqlite3
from decimal import Decimal

from app.data import signals_repository as signals


def test_monthly_paid_excludes_canceled_deleted_and_other_manager_payments(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.create_function("FORMAT", 2, lambda value, fmt: value[:7])
    db.create_function("GetExchangedToEuroValue", 3, lambda amount, currency, day: amount / 50)
    db.executescript("""
        CREATE TABLE Client(ID INTEGER, Deleted INTEGER, MainManagerID INTEGER);
        CREATE TABLE IncomePaymentOrder(ClientID INTEGER, Deleted INTEGER, IsCanceled INTEGER,
                                        FromDate TEXT, Amount NUMERIC, CurrencyID INTEGER);
        INSERT INTO Client VALUES (1,0,7),(2,0,8),(3,1,7);
        INSERT INTO IncomePaymentOrder VALUES
          (1,0,0,'2026-10-01',500,1),
          (1,0,1,'2026-10-02',1500,1),
          (1,1,0,'2026-10-03',2000,1),
          (2,0,0,'2026-10-04',3000,1),
          (3,0,0,'2026-10-05',4000,1),
          (1,0,0,'2026-10-23',5000,1),
          (1,0,0,'2026-09-30',6000,1);
    """)
    # Only schema qualification and SQL Server's formatting/FX functions are adapted;
    # the real repository WHERE/JOIN/GROUP BY execute unchanged against the fixture.
    monkeypatch.setattr(signals, "query", lambda sql, params: [
        dict(row) for row in db.execute(sql.replace("dbo.", ""), params)
    ])
    assert signals.monthly_paid(7, "2026-10-01", "2026-10-23") == {"2026-10": Decimal(10)}
    db.close()
