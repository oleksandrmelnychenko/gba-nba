"""Optional actual SQL Server acceptance using session-local #tables, never business tables.

NBA_SQL_FIXTURE_ENV_FILE points to an existing DEV read-only NBA settings file. No credentials
are printed or committed. The test is skipped without this explicit fixture opt-in.
"""
import os
from datetime import datetime
from decimal import Decimal

import pytest

from app.data.purchase_history import retained_purchase_cte


@pytest.fixture
def sql_fixture():
    env_file = os.environ.get("NBA_SQL_FIXTURE_ENV_FILE")
    if not env_file:
        pytest.skip("Set NBA_SQL_FIXTURE_ENV_FILE for disposable SQL Server #table acceptance")
    from app.core.config import Settings
    from sqlalchemy import create_engine, text
    settings = Settings(_env_file=env_file)
    assert settings.db_name == "ConcordDb_V5", "Fixture only permitted against configured DEV reader"
    engine = create_engine(settings.sqlalchemy_url, connect_args={"timeout": 30, "login_timeout": 10})
    with engine.connect() as connection:
        try:
            assert connection.execute(text("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT')")).scalar() == 0
            connection.exec_driver_sql("""
                CREATE TABLE #User(ID bigint, Deleted bit);
                CREATE TABLE #Debt(ID bigint, Total decimal(18,4), Created datetime2, Deleted bit);
                CREATE TABLE #ClientInDebt(ClientID bigint, DebtID bigint, Deleted bit);
                CREATE TABLE #Client(ID bigint, MainManagerID bigint, Deleted bit);
                CREATE TABLE #ClientAgreement(ID bigint, ClientID bigint, Deleted bit);
                CREATE TABLE #Order(ID bigint, ClientAgreementID bigint, Created datetime2, Deleted bit, IsMerged bit);
                CREATE TABLE #Sale(ID bigint, OrderID bigint, IsImported bit, Deleted bit, IsMerged bit, ChangedToInvoice datetime2, Created datetime2);
                CREATE TABLE #OrderItem(ID bigint, OrderID bigint, ProductID bigint, Qty float, ReturnedQty float,
                    PricePerItem decimal(18,4), Deleted bit, IsValidForCurrentSale bit, Updated datetime2);
                CREATE TABLE #Lineage(LocalSaleID bigint, DestinationDocumentDate datetime2, IsPresent bit, SourceSaleID varbinary(16), ImportedForAmg bit);
                INSERT #User VALUES(1,0),(2,0);
                INSERT #Debt VALUES(501,50,'2026-09-01',0);
                INSERT #ClientInDebt VALUES(10,501,0);
                INSERT #Client VALUES(10,1,0),(20,2,0);
                INSERT #ClientAgreement VALUES(10,10,0),(20,20,0);
            """)
            for identifier in range(1, 17):
                connection.execute(text("INSERT #Order VALUES(:id,:client,'2026-10-01T12:00:00',:deleted,0)"),
                                   {"id": identifier, "client": 20 if identifier == 16 else 10, "deleted": identifier == 10})
                connection.execute(text("INSERT #Sale VALUES(:id,:id,:imported,:deleted,:merged,:invoice,'2026-10-01T12:00:00')"),
                                   {"id": identifier, "imported": identifier in (2, 3), "deleted": identifier == 4,
                                    "merged": identifier == 5, "invoice": None if identifier == 9 else datetime(2026, 10, 1, 12)})
                returned = {7: 10, 8: 6, 14: -1, 15: 11}.get(identifier, 0)
                connection.execute(text("INSERT #OrderItem VALUES(:id,:id,7,10,:returned,10,:deleted,:valid,'2026-10-01T12:00:00')"),
                                   {"id": identifier, "returned": returned, "deleted": identifier == 11,
                                    "valid": identifier != 12})
            connection.exec_driver_sql("""
                INSERT #Sale VALUES(113,13,0,0,0,'2026-10-01','2026-10-01');
                UPDATE #Order SET Created='2024-01-01' WHERE ID=1;
                UPDATE #Sale SET Created='2026-10-01T13:00:00' WHERE ID=2;
                INSERT #Lineage VALUES(2,'2026-10-01T13:00:00',1,0x02,0),
                    (3,'2026-10-01T12:00:00',1,0x03,0),(3,'2026-10-01T12:00:00',1,0x04,0);
            """)
            def run(sql, params):
                replacements = {"dbo.[User]": "#User", "dbo.Debt": "#Debt", "dbo.ClientInDebt": "#ClientInDebt", "dbo.ClientAgreement": "#ClientAgreement", "dbo.OrderItem": "#OrderItem",
                    "dbo.[Order]": "#Order", "dbo.Client": "#Client", "dbo.Sale": "#Sale",
                    "dbo.DataSyncImportedSaleSourceLine": "#Lineage"}
                for original, temporary in replacements.items():
                    sql = sql.replace(original, temporary)
                return [dict(row) for row in connection.execute(text(sql), params).mappings()]
            yield connection, run
        finally:
            connection.rollback()
    engine.dispose()


def test_actual_sql_retained_purchase_scope_dates_returns_and_merged_inputs(sql_fixture):
    _, run = sql_fixture
    rows = run(retained_purchase_cte() + " SELECT * FROM retained_purchases",
               {"mid": 1, "source_start": "2025-01-01", "asof": "2026-10-02"})
    by_id = {row['order_id']: row for row in rows}
    assert set(by_id) == {1, 2, 3, 6, 8}
    assert len(rows) == len(by_id), "A sale join cannot multiply an order/product"
    assert by_id[1]['timestamp_basis'] == 'native_utc'
    assert by_id[2]['timestamp_basis'] == 'imported_lineage_utc'
    assert by_id[2]['purchased_at_utc'] == datetime(2026, 10, 1, 13)
    assert by_id[2]['purchased_at'] == datetime(2026, 10, 1, 16)
    assert by_id[1]['raw_order_created'] == datetime(2024, 1, 1)
    assert by_id[1]['purchased_at'] == datetime(2026, 10, 1, 15)
    assert by_id[3]['timestamp_basis'] is None and by_id[3]['purchased_at_utc'] is None
    assert by_id[8]['quantity'] == Decimal(4)
    assert by_id[8]['turnover_eur'] == Decimal(40)


def test_purchase_boundary_parameter_names_are_internal_identifiers():
    with pytest.raises(ValueError):
        retained_purchase_cte(start_param='bad; DROP TABLE')

