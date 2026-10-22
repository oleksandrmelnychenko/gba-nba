# Task-only delegation concurrency review

Seller status and note mutations now carry the manager/updated_at snapshot authorized by the API into Mongo CAS. A head assignment between authorization and lifecycle read, or between lifecycle read and update, causes409 rather than writing under the former owner. Existing authenticated head overrides are retained; internal lifecycle callers may intentionally omit caller scope. Scoped writes advance the millisecond revision monotonically.

Explicit sale linking validates its SQL/contact facts first, then linearizes task ownership by atomically advancing task updated_at against the authorized manager/status/revision. If an assignment already won, no sale link is inserted. If linking wins first, a later assignment leaves the prior owner's immutable receipt intact. A process failure between CAS and link insertion can leave only a newer task revision; an ordinary retry revalidates and completes the link. No financial journal entry or source ownership is mutated. Existing sale-ID uniqueness and exact owned retries remain.

Assignment clarification is appended atomically to existing task notes with head author/name/time as well as the immutable assignment history. This makes new instructions visible in the assignee's existing workday task details without rewriting earlier notes or requiring another DTO.

Regression coverage uses disposable Mongo namespaces (dropped in fixture teardown), no production/business fixtures: reassignment between route check and status/note write; reassignment between lifecycle read and write; source-read/link race in both orders; preserved immutable attribution/retry; head override; new assignee sees original notes plus clarification; same-millisecond write revisions.
