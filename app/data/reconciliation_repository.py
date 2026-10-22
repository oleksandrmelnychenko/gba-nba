"""Positive, document-level facts for task reconciliation; absence is never settlement.

Small client batches only. Zero balances are deliberately included, deleted/missing rows are
not evidence. Orders with no remaining purchased quantity cannot resolve a sales suggestion.
"""
from datetime import date, timedelta

from app.core import history
from app.data.db import in_clause, query


def source_facts(client_ids: list[int], as_of: str) -> dict[int, dict]:
    result = {}
    client_ids = sorted(set(client_ids))
    end = (date.fromisoformat(as_of) + timedelta(days=1)).isoformat()
    for start in range(0, len(client_ids), 300):
        clause, params = in_clause("client", client_ids[start:start + 300])
        params |= {"start": history.source_history_start().isoformat(), "end": end}
        owners = query(f"""
            SELECT c.ID AS client_id, c.MainManagerID AS manager_id
            FROM dbo.Client c
            JOIN dbo.[User] u ON u.ID = c.MainManagerID AND u.Deleted = 0
            WHERE c.ID IN {clause} AND c.Deleted = 0
        """, params)
        debts = query(f"""
            SELECT cid.ClientID AS client_id, d.ID AS debt_id, d.Total AS balance
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID AND d.Deleted = 0
            WHERE cid.ClientID IN {clause} AND cid.Deleted = 0
                  AND d.Created < :end
        """, params)
        orders = query(f"""
            WITH purchases AS (
                SELECT ca.ClientID AS client_id, oi.ProductID AS product_id,
                       o.ID AS order_id, o.Created AS purchased_at,
                       SUM(oi.Qty - ISNULL(oi.ReturnedQty, 0)) AS quantity
                FROM dbo.ClientAgreement ca
                JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID AND o.Deleted = 0
                JOIN dbo.OrderItem oi ON oi.OrderID = o.ID AND oi.Deleted = 0
                WHERE ca.ClientID IN {clause} AND ca.Deleted = 0
                      AND oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
                      AND o.Created >= :start AND o.Created < :end
                GROUP BY ca.ClientID, oi.ProductID, o.ID, o.Created
                HAVING SUM(oi.Qty - ISNULL(oi.ReturnedQty, 0)) > 0
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY client_id, product_id ORDER BY purchased_at DESC, order_id DESC
                ) AS rn FROM purchases
            )
            SELECT client_id, product_id, order_id, purchased_at, quantity
            FROM ranked WHERE rn = 1
        """, params)
        returns = query(f"""
            WITH returned AS (
                SELECT ca.ClientID AS client_id, oi.ProductID AS product_id,
                       o.ID AS order_id, SUM(oi.ReturnedQty) AS returned_quantity,
                       MAX(oi.Updated) AS returned_at,
                       ROW_NUMBER() OVER (PARTITION BY ca.ClientID, oi.ProductID
                           ORDER BY MAX(oi.Updated) DESC, o.ID DESC) AS rn
                FROM dbo.ClientAgreement ca
                JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID AND o.Deleted = 0
                JOIN dbo.OrderItem oi ON oi.OrderID = o.ID AND oi.Deleted = 0
                WHERE ca.ClientID IN {clause} AND ca.Deleted = 0
                      AND oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
                      AND oi.ReturnedQty > 0 AND oi.Updated < :end
                      AND o.Created >= :start AND o.Created < :end
                GROUP BY ca.ClientID, oi.ProductID, o.ID
            )
            SELECT client_id, product_id, order_id, returned_quantity, returned_at
            FROM returned WHERE rn = 1
        """, params)
        for row in owners:
            result[int(row["client_id"])] = {"manager_id": int(row["manager_id"]),
                                             "debts": {}, "orders": {}, "returns": {}}
        for row in debts:
            if int(row["client_id"]) in result:
                result[int(row["client_id"])]["debts"][str(row["debt_id"])] = str(row["balance"])
        for row in orders:
            if int(row["client_id"]) in result:
                result[int(row["client_id"])]["orders"][str(row["product_id"])] = {
                    "order_id": int(row["order_id"]),
                    "purchased_at": row["purchased_at"].isoformat(),
                }
        for row in returns:
            if int(row["client_id"]) in result:
                result[int(row["client_id"])]["returns"][str(row["product_id"])] = {
                    "order_id": int(row["order_id"]), "quantity": str(row["returned_quantity"]),
                    "returned_at": row["returned_at"].isoformat(),
                }
    return result
