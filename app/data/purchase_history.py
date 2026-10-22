"""One retained confirmed purchase per order/product, shared by seller advice and segments.

purchased_at is the verified Kyiv business wall clock, or the raw sale document clock when
unknown. purchased_at_utc is populated ONLY with positive timestamp provenance. Draft-order
creation is retained separately and never substituted for the confirmed purchase event.
Callers supply internal SQL fragments/bound parameter names, never request text as scope_sql.
"""
import re


def retained_purchase_cte(scope_sql: str = "c.MainManagerID = :mid",
                          start_param: str = "source_start", end_param: str = "asof") -> str:
    for name in (start_param, end_param):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Invalid internal purchase boundary parameter")
    return f"""
    WITH purchase_candidates AS (
        SELECT c.ID AS client_id, o.ID AS order_id, o.Created AS raw_order_created,
               CASE WHEN sale.imported = 0 THEN sale.invoice_at
                    ELSE sale.sale_created END AS event_at,
               CASE WHEN sale.imported = 0 THEN 'native_utc'
                    WHEN sale.imported = 1 AND lineage.first_date = lineage.last_date
                      AND lineage.source_count = 1
                      AND ABS(DATEDIFF_BIG(millisecond, lineage.first_date, sale.sale_created)) <= 4
                      THEN 'imported_lineage_utc' END AS timestamp_basis
        FROM dbo.Client c
        JOIN dbo.ClientAgreement ca ON ca.ClientID = c.ID AND ca.Deleted = 0
        JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID AND o.Deleted = 0 AND o.IsMerged = 0
        CROSS APPLY (
            SELECT COUNT_BIG(*) AS sale_count, MIN(s.ID) AS sale_id,
                   MIN(CONVERT(int, s.IsImported)) AS imported,
                   MIN(s.Created) AS sale_created, MIN(s.ChangedToInvoice) AS invoice_at
            FROM dbo.Sale s
            WHERE s.OrderID = o.ID AND s.Deleted = 0 AND s.IsMerged = 0
                  AND s.ChangedToInvoice IS NOT NULL
        ) sale
        OUTER APPLY (
            SELECT MIN(l.DestinationDocumentDate) AS first_date,
                   MAX(l.DestinationDocumentDate) AS last_date,
                   COUNT(DISTINCT CONVERT(varchar(32), l.SourceSaleID, 2) + ':' +
                         CONVERT(varchar(1), l.ImportedForAmg)) AS source_count
            FROM dbo.DataSyncImportedSaleSourceLine l
            WHERE l.LocalSaleID = sale.sale_id AND l.IsPresent = 1
        ) lineage
        WHERE c.Deleted = 0 AND ({scope_sql}) AND sale.sale_count = 1
    ), purchase_dates AS (
        SELECT client_id, order_id, raw_order_created, timestamp_basis,
               CASE WHEN timestamp_basis IS NOT NULL THEN event_at END AS purchased_at_utc,
               CASE WHEN timestamp_basis IS NOT NULL THEN
                    CAST(event_at AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time' AS datetime2)
                    ELSE event_at END AS purchased_at
        FROM purchase_candidates
    ), purchase_orders AS (
        SELECT * FROM purchase_dates
        WHERE purchased_at >= :{start_param} AND purchased_at < :{end_param}
    ), retained_purchases AS (
        SELECT p.client_id, oi.ProductID AS product_id, p.order_id, p.purchased_at,
               p.raw_order_created, p.timestamp_basis, p.purchased_at_utc,
               SUM(CAST(oi.Qty - COALESCE(oi.ReturnedQty, 0) AS decimal(30, 8))) AS quantity,
               SUM(CAST(oi.Qty - COALESCE(oi.ReturnedQty, 0) AS decimal(30, 8)) * oi.PricePerItem) AS turnover_eur
        FROM purchase_orders p
        JOIN dbo.OrderItem oi ON oi.OrderID = p.order_id AND oi.Deleted = 0
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.Qty > 0
              AND COALESCE(oi.ReturnedQty, 0) >= 0 AND COALESCE(oi.ReturnedQty, 0) <= oi.Qty
        GROUP BY p.client_id, oi.ProductID, p.order_id, p.purchased_at,
                 p.raw_order_created, p.timestamp_basis, p.purchased_at_utc
        HAVING SUM(oi.Qty - COALESCE(oi.ReturnedQty, 0)) > 0
    )
    """
