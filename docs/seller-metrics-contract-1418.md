# Seller metric corrections — BUG-1418

This patch is partial acceptance, not completion of the entire dashboard metric specification.

| Metric / wire field | Definition and grain | Scope / attribution | Date / unit | Coverage and missing data |
| --- | --- | --- | --- | --- |
| Known overdue debt / `value_at_risk_eur` | Sum of positive, currently unpaid Debt documents whose Kyiv obligation date + current Agreement.NumberDaysDebt precedes the requested current Kyiv day | Active clients, their current MainManagerID; independent of AI follow-up minimum amount and 365-day filtering | Current stock, EUR; document-date FX history with existing current-rate fallback | Missing FX fails the financial endpoint; it never returns a local amount as EUR. No sync-completion watermark is available. |
| `debt_aging` | Each individual unpaid document goes to exactly one bucket by days **past due**; count is documents, not clients | Same stock as known overdue debt | 1–30, 31–60, 61–90, 91+ days; legacy first bucket identifier remains `0-30` for compatibility | Sum of rounded bucket cents exactly equals the known overdue headline. Due-today/not-yet-due documents are excluded. |
| `debt_unknown_terms_count`, `debt_unknown_terms_eur` | Count and value of unpaid documents with absent/deleted agreement or invalid payment terms | Same current book | Current stock, EUR | Separate from known overdue totals. Explicit zero-day terms are valid; missing terms are never substituted with zero. |
| `paid.mtd` | Sum of persisted IncomePaymentOrder.EuroAmount for nondeleted, noncancelled client payments | Client's **current** manager, not historical manager achievement | FromDate payment month through selected day, EUR | Nonzero local payments without nonzero, same-sign EUR fail the endpoint. Source date boundary remains configured; source completeness/freshness is not inferred from the response time. |
| Legacy `shipped.mtd` | Existing SUM(OrderItem.Qty × PricePerItem), valid current-sale items, synthetic lines excluded | Current client book | Order.Created month, stored EUR calculation | Now labelled calculated order amount, **not shipped/net sales**. ShipmentDate, returns and historical manager assignment remain unresolved. |
| `target` / `expected_to_date` | Historical average of completed source-covered months; expected = average × elapsed working days / working days in month | Current client book; baseline owned by NBA configuration, no approved business plan exists | Mon–Sat calendar, current month | UI calls it historical baseline. Pace uses expected_to_date ±5%, not 80% of the full-month baseline. |

The dashboard stock query reads only existing local Debt/ClientInDebt records, including still-unpaid older obligations/opening records. It does not fetch historical records from 1C, change the 2025 sync boundary, copy data, backfill or start workers. Existing AI task-generation debt filters remain unchanged.

Financial cache values have hard maximum ages: debt ten minutes, team ninety seconds. On expiration a refresh is required; failure propagates and the UI clears old values instead of presenting an indefinitely stale snapshot as current. The source-status component still explicitly says the latest sale activity is not proof of completed synchronization.

Read-only DEV evidence on 2026-10-22:

- 1,697 active positive debts with distinct DebtIDs; 1,696 attached to sales and one resale; none missing agreements; 1,601 have positive terms.
- Current persisted EUR payment values are populated: 33,723 UAH payments total 360,767,115.58 UAH / 7,277,439.1185 EUR, none equal the nonzero local amount and none missing EUR. USD40 and EUR268 are also populated. The old comment claiming EuroAmount contains UAH is obsolete; current sync applies document conversion in ApplyIncomePaymentCurrencyValues.
- For the active assigned-client scope, 27,731 payments total €6,041,298.5024 persisted versus €6,042,053.8600 after legacy revaluation. Existing current unpaid documents have no deleted/missing sale attachments; 296 are unassigned and intentionally outside manager dashboards.
- New document snapshot query: 156 ms; legacy actionable-client aggregation: 1,054 ms. Known overdue total changes from €62,411.42 to €121,609.86 because stock is no longer truncated by task-generation thresholds/window. These are intentionally different scopes, not equivalent-query benchmark totals.

Evidence files: `/private/seller-metrics-1418-20261022/`.

Still open: real shipment/return and historical attribution contract; approved targets/business holiday calendar; authoritative sync watermark and measured completeness for every source; all other role-dashboard KPIs; rates without historical observations still use the existing current rate fallback and are not a certified ledger valuation. This patch must not close BUG-1418 in full.
