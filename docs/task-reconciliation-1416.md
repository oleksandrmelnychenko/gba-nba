# BUG1416: source-backed recommendation reconciliation

Generation now reconciles existing advice before applying quotas. It does not infer success
from an empty generator response. No new worker, scheduler, sync, or write to the source SQL
business database is introduced.

* A debt task is superseded only when every previously identified debt row still exists with
  an explicit nonpositive balance. Missing/deleted debts, missing clients, SQL failures and
  partial data remain unknown. Legacy tasks without a debt baseline need a positive baseline
  before a later settlement can be confirmed; they cannot be safely closed retroactively.
* A bundled purchase suggestion is superseded only after every suggested product has a newer
  retained purchase. Fully returned orders do not count. Positive return evidence distinguishes
  a renewed need from the earlier, superseded cycle. Current-day purchases suppress stale
  candidates calculated at the generator's exclusive date boundary.
* Source-cycle keys replace the monthly deduplication window. Existing task identities survive
  month rollover and manager reassignment. Current nondeleted Client.MainManagerID plus an active
  User row is required for reassignment; assignment history is retained. Old monthly duplicates
  are superseded with an explicit link to the retained advice.
* Notes, deadlines, source evidence and status history are preserved. Explicit snoozed or
  in-progress work becomes a separately identified manual follow-up when its AI suggestion is
  superseded. Interrupted follow-up copies are retried on the next reconciliation. Seller contact
  events and their next-contact dates remain untouched and follow the current client book.
* Manual tasks are excluded from both global and per-client AI quotas. Dashboard count payloads
  expose ai_active and manual_backlog separately. Superseded advice is available in workday history.
* Reconciliation occurs on an already-authorized generation run, including runs with zero
  candidates; deploying this code does not run generation. Source errors are reported separately
  from unverified client IDs. No claim is made that source absence proves a completed full sync.

Validation (2026-10-22): 231 tests passed, 34 skipped. New fixtures cover settlement, missing debt,
placed order, full/partial returns, a second reorder cycle in one month, month rollover, transfer,
generator/source failures, manual quota, legacy duplicates, concurrent seller changes and recovery
after interrupted follow-up copying. A disposable SQL Server database exercised the actual selectors;
it was removed afterward. No DEV/QA business rows were modified.

Evidence: /private/dashboard-reconcile-1416-20261022/{tests.log,sql-acceptance.log,sql_acceptance.py}.
