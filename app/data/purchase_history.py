"""Shared retained-purchase grain for seller advice; one row per order and product.

Only confirmed invoices count as purchases. Deleted/merged inputs and editable drafts do not.
Imported document clocks are trusted only when the retained source lineage agrees. Callers must
supply internal SQL fragments and bound parameter names; never pass request text as scope_sql.
"""
import re


def retained_purchase_cte(scope_sql: str = "c.MainManagerID = :mid",
                          start_param: str = "source_start", end_param: str = "asof") -> str:
    for name in (start_param, end_param):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Invalid internal purchase boundary parameter")
    return f"""
    WITH purchase_orders AS (
        SELECT c.ID AS client_id, o.ID AS order_id, o.Created AS purchased_at,
               CASE WHEN sale.imported = 0 THEN 'native_utc'
                    WHEN sale.imported = 1 AND lineage.first_date = lineage.last_date
                      AND ABS(DATEDIFF_BIG(millisecond, lineage.first_date, o.Created)) <= 4
                      THEN 'imported_lineage_utc' END AS timestamp_basis
        FROM dbo.Client c
        JOIN dbo.ClientAgreement ca ON ca.ClientID = c.ID AND ca.Deleted = 0
        JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID AND o.Deleted = 0 AND o.IsMerged = 0
        CROSS APPLY (
            SELECT COUNT_BIG(*) AS sale_count, MIN(s.ID) AS sale_id,
                   MIN(CONVERT(int, s.IsImported)) AS imported
            FROM dbo.Sale s
            WHERE s.OrderID = o.ID AND s.Deleted = 0 AND s.IsMerged = 0
                  AND s.ChangedToInvoice IS NOT NULL
        ) sale
        OUTER APPLY (
            SELECT MIN(l.DestinationDocumentDate) AS first_date,
                   MAX(l.DestinationDocumentDate) AS last_date
            FROM dbo.DataSyncImportedSaleSourceLine l
            WHERE l.LocalSaleID = sale.sale_id AND l.IsPresent = 1
        ) lineage
        WHERE c.Deleted = 0 AND ({scope_sql}) AND sale.sale_count = 1
              AND o.Created >= :{start_param} AND o.Created < :{end_param}
    ), retained_purchases AS (
        SELECT p.client_id, oi.ProductID AS product_id, p.order_id, p.purchased_at,
               p.timestamp_basis,
               CASE WHEN p.timestamp_basis IS NOT NULL THEN p.purchased_at END AS purchased_at_utc,
               SUM(CAST(oi.Qty - COALESCE(oi.ReturnedQty, 0) AS decimal(30, 8))) AS quantity,
               SUM(CAST(oi.Qty - COALESCE(oi.ReturnedQty, 0) AS decimal(30, 8)) * oi.PricePerItem) AS turnover_eur
        FROM purchase_orders p
        JOIN dbo.OrderItem oi ON oi.OrderID = p.order_id AND oi.Deleted = 0
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.Qty > 0
              AND COALESCE(oi.ReturnedQty, 0) >= 0 AND COALESCE(oi.ReturnedQty, 0) <= oi.Qty
        GROUP BY p.client_id, oi.ProductID, p.order_id, p.purchased_at, p.timestamp_basis
        HAVING SUM(oi.Qty - COALESCE(oi.ReturnedQty, 0)) > 0
    )
    """
