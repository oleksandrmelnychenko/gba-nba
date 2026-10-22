"""Current unpaid-document snapshot; independent of AI follow-up thresholds/window.

Debt.Created is the persisted obligation date, not response time. Imported timestamps
need positive clock provenance; IsImported alone does not imply UTC or local time.
Agreement terms are current persisted terms; historical term changes are unavailable.
"""
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.money import cents, cents_decimal, decimal_value
from app.data.db import query

_KYIV = ZoneInfo('Europe/Kyiv')
_BUCKETS = [('0-30', 1, 30), ('31-60', 31, 60), ('61-90', 61, 90), ('90+', 91, None)]

_DOCUMENTS_SQL = """
SELECT d.ID AS debt_id, c.MainManagerID AS manager_id, d.Created AS obligation_at,
       CASE WHEN sale.IsImported = 0 THEN 'native_utc'
            WHEN sale.IsImported = 1 AND source_date.first_date = source_date.last_date
              AND ABS(DATEDIFF_BIG(millisecond, source_date.first_date, d.Created)) <= 4
              THEN 'imported_lineage_utc'
       END AS obligation_date_basis,
       CASE WHEN a.Deleted = 0 AND a.NumberDaysDebt >= 0 THEN a.NumberDaysDebt END AS term_days,
       CAST(CASE
         WHEN currency.Code = 'EUR' THEN d.Total
         WHEN direct.rate > 0 THEN CASE WHEN currency.Code IN ('UAH', 'PLN') THEN d.Total / direct.rate ELSE d.Total * direct.rate END
         WHEN cross_rate.rate > 0 THEN d.Total * cross_rate.rate
         WHEN inverse.rate > 0 THEN d.Total / inverse.rate
       END AS decimal(30, 4)) AS amount_eur
FROM dbo.ClientInDebt attachment
JOIN dbo.Debt d ON d.ID = attachment.DebtID AND d.Deleted = 0
JOIN dbo.Client c ON c.ID = attachment.ClientID AND c.Deleted = 0
LEFT JOIN dbo.Sale sale ON sale.ID = attachment.SaleID AND sale.Deleted = 0
OUTER APPLY (
  SELECT MIN(line.DestinationDocumentDate) AS first_date,
         MAX(line.DestinationDocumentDate) AS last_date
  FROM dbo.DataSyncImportedSaleSourceLine line
  WHERE line.LocalSaleID = sale.ID AND line.IsPresent = 1
) source_date
LEFT JOIN dbo.Agreement a ON a.ID = attachment.AgreementID
LEFT JOIN dbo.Currency currency ON currency.ID = a.CurrencyID AND currency.Deleted = 0
OUTER APPLY (SELECT TOP (1) ID FROM dbo.Currency WHERE Code = 'EUR' AND Deleted = 0 ORDER BY ID) eur
OUTER APPLY (
  SELECT TOP (1) h.Amount AS rate
  FROM dbo.ExchangeRate r
  OUTER APPLY (SELECT TOP (1) Amount, Created, ID FROM dbo.ExchangeRateHistory
    WHERE ExchangeRateID = r.ID AND Deleted = 0 AND Created <= d.Created ORDER BY Created DESC, ID DESC) h
  WHERE r.CurrencyID = currency.ID AND r.Code = 'EUR' AND r.Deleted = 0
  ORDER BY h.Created DESC, h.ID DESC, r.ID DESC
) direct
OUTER APPLY (
  SELECT TOP (1) h.Amount AS rate
  FROM dbo.CrossExchangeRate r
  OUTER APPLY (SELECT TOP (1) Amount, Created, ID FROM dbo.CrossExchangeRateHistory
    WHERE CrossExchangeRateID = r.ID AND Deleted = 0 AND Created <= d.Created ORDER BY Created DESC, ID DESC) h
  WHERE r.CurrencyFromID = currency.ID AND r.CurrencyToID = eur.ID AND r.Deleted = 0
  ORDER BY h.Created DESC, h.ID DESC, r.ID DESC
) cross_rate
OUTER APPLY (
  SELECT TOP (1) h.Amount AS rate
  FROM dbo.CrossExchangeRate r
  OUTER APPLY (SELECT TOP (1) Amount, Created, ID FROM dbo.CrossExchangeRateHistory
    WHERE CrossExchangeRateID = r.ID AND Deleted = 0 AND Created <= d.Created ORDER BY Created DESC, ID DESC) h
  WHERE r.CurrencyFromID = eur.ID AND r.CurrencyToID = currency.ID AND r.Deleted = 0
  ORDER BY h.Created DESC, h.ID DESC, r.ID DESC
) inverse
WHERE attachment.Deleted = 0 AND d.Total > 0 AND d.Created < :snapshot_end
  AND c.MainManagerID IS NOT NULL
"""


def fold_documents(rows: list[dict], as_of: str) -> dict:
    snapshot_day = date.fromisoformat(as_of)
    amounts = {name: Decimal(0) for name, _, _ in _BUCKETS}
    counts = {name: 0 for name, _, _ in _BUCKETS}
    unknown_amount = Decimal(0)
    unknown_count = 0
    unknown_date_amount = Decimal(0)
    unknown_date_count = 0
    seen = set()
    for row in rows:
        if row['debt_id'] in seen:
            raise ValueError('debt_document_attachment_ambiguous')
        seen.add(row['debt_id'])
        if row['amount_eur'] is None:
            # A local-currency amount must never masquerade as EUR or disappear in SUM.
            raise ValueError('debt_eur_conversion_missing')
        amount = decimal_value(row['amount_eur'])
        if row.get('obligation_date_basis') not in ('native_utc', 'imported_lineage_utc'):
            unknown_date_count += 1
            unknown_date_amount += amount
            continue
        created = row['obligation_at']
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        obligation_day = created.astimezone(_KYIV).date()
        if obligation_day > snapshot_day:
            continue
        if row['term_days'] is None:
            unknown_count += 1
            unknown_amount += amount
            continue
        days_past_due = (snapshot_day - obligation_day).days - row['term_days']
        for name, low, high in _BUCKETS:
            if days_past_due >= low and (high is None or days_past_due <= high):
                amounts[name] += amount
                counts[name] += 1
                break
    amounts = {name: cents_decimal(value) for name, value in amounts.items()}
    return {
        'value_at_risk_eur': cents(sum(amounts.values(), Decimal(0))),
        'debt_aging': [{'bucket': name, 'amount_eur': cents(amounts[name]), 'count': counts[name]} for name, _, _ in _BUCKETS],
        'debt_unknown_date_count': unknown_date_count,
        'debt_unknown_date_eur': cents(unknown_date_amount),
        'debt_unknown_terms_count': unknown_count,
        'debt_unknown_terms_eur': cents(unknown_amount),
    }


def dashboards(as_of: str) -> dict[int, dict]:
    # Read through the source calendar day so an unverified local clock isn't silently
    # dropped by a guessed UTC cutoff. Verified UTC documents are filtered by Kyiv day in fold.
    end = datetime.combine(date.fromisoformat(as_of) + timedelta(days=1), time.min)
    rows = query(_DOCUMENTS_SQL, {'snapshot_end': end.replace(tzinfo=None)})
    if len({row['debt_id'] for row in rows}) != len(rows):
        raise ValueError('debt_document_attachment_ambiguous')
    by_manager = defaultdict(list)
    for row in rows:
        by_manager[int(row['manager_id'])].append(row)
    return {manager: fold_documents(documents, as_of) for manager, documents in by_manager.items()}
