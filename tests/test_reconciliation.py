from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import mongomock
import pytest

from app.data import mongo, reconciliation_repository
from app.domain.models import Task, TaskStatus, TaskType
from app.services import lifecycle, orchestrator, reconciliation


@pytest.fixture
def db(monkeypatch):
    db = mongomock.MongoClient(tz_aware=True).nba
    db.tasks.create_index("task_key", unique=True)
    monkeypatch.setattr(mongo, "tasks", lambda: db.tasks)
    monkeypatch.setattr(mongo, "task_events", lambda: db.events)
    monkeypatch.setattr(mongo, "manager_prefs", lambda: db.preferences)
    return db


def task(key="old", kind=TaskType.REORDER_DUE, manager=1):
    return Task(task_key=key, client_id=10, manager_id=manager, task_type=kind, title="Call", reason="Due",
                payload={"products": [{"product_id": 7, "elapsed_days": 15}]},
                due_date=datetime.now(UTC) + timedelta(days=2))


def order(identifier=100, when="2026-10-01T12:00:00"):
    return {"order_id": identifier, "purchased_at": when}


def facts(monkeypatch, *, owner=1, debts=None, orders=None):
    value = {10: {"manager_id": owner, "debts": debts or {}, "orders": orders or {}}}
    monkeypatch.setattr(reconciliation_repository, "source_facts", lambda ids, asof: value)
    return value


def persist(candidate, asof="2026-10-16"):
    candidates, stats = reconciliation.prepare(candidate.manager_id, [candidate], asof)
    for item in candidates:
        lifecycle.upsert_generated(item)
    return candidates, stats


def test_paid_identified_debt_supersedes_with_evidence_and_keeps_notes_and_contact(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "12.50"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    key = rows[0].task_key
    lifecycle.add_note(key, 1, "Promise to call Friday")
    deadline = datetime.now(UTC) + timedelta(days=2)
    lifecycle.change_status(key, TaskStatus.SNOOZED, by=1, snooze_until=deadline)
    source[10]["debts"]["501"] = "0.00"
    candidates, stats = reconciliation.prepare(1, [], "2026-10-17")
    historical = db.tasks.find_one({"task_key": key})
    assert stats["superseded"] == 1 and candidates == []
    assert historical["status"] == "superseded"
    assert historical["source_evidence"]["debts"] == {"501": "0.00"}
    assert historical["notes"][0]["text"] == "Promise to call Friday"
    commitment = db.tasks.find_one({"source_task_key": key})
    assert commitment["task_type"] == "manual" and commitment["status"] == "snoozed"
    assert commitment["snooze_until"].date() == deadline.date()
    assert lifecycle.active_count(1, exclude_manual=True) == 0
    assert lifecycle.dashboard_counts(1, "2026-10-17")["manual_backlog"] == 1


@pytest.mark.parametrize("current", [{}, {"other": "0"}])
def test_missing_debt_is_not_paid_and_empty_generator_cannot_close(db, monkeypatch, current):
    source = facts(monkeypatch, debts={"501": "12.50"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    source[10]["debts"] = current
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 0
    assert db.tasks.find_one({"task_key": rows[0].task_key})["status"] == "open"


def test_purchase_supersedes_old_advice_and_second_cycle_in_same_month_is_distinct(db, monkeypatch):
    source = facts(monkeypatch, orders={"7": order()})
    first, _ = persist(task())
    key = first[0].task_key
    source[10]["orders"]["7"] = order(101, "2026-10-17T11:00:00")
    # Generator still reflects yesterday; don't create another call immediately after purchase.
    stale, stats = persist(task("monthly-old"), "2026-10-17")
    assert stale == [] and stats["superseded"] == 1
    second = task("monthly-old")
    second.payload["products"][0]["elapsed_days"] = 7
    rows, _ = persist(second, "2026-10-24")
    assert len(rows) == 1 and rows[0].task_key != key
    assert db.tasks.count_documents({"status": "open"}) == 1
    assert db.tasks.find_one({"task_key": key})["source_evidence"]["orders"]["7"]["order_id"] == 101


def test_returned_purchase_absence_does_not_close_existing_need(db, monkeypatch):
    source = facts(monkeypatch, orders={"7": order()})
    rows, _ = persist(task())
    # Fully returned orders are absent from retained purchase facts, not new purchase evidence.
    source[10]["orders"] = {}
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 0
    assert db.tasks.find_one({"task_key": rows[0].task_key})["status"] == "open"


def test_transfer_keeps_one_owner_identity_notes_and_history(db, monkeypatch):
    source = facts(monkeypatch, orders={"7": order()})
    first, _ = persist(task())
    lifecycle.add_note(first[0].task_key, 1, "Call agreed")
    source[10]["manager_id"] = 2
    rows, stats = persist(task("new-manager", manager=2))
    assert stats["reassigned"] == 1
    assert rows[0].task_key == first[0].task_key
    assert db.tasks.count_documents({"manager_id": 1}) == 0
    document = db.tasks.find_one({"manager_id": 2})
    assert document["assignment_history"][0]["from"] == 1
    assert document["notes"][0]["text"] == "Call agreed"


def test_failure_and_month_rollover_preserve_existing_identity(db, monkeypatch):
    facts(monkeypatch, orders={"7": order()})
    first, _ = persist(task())
    def unavailable(ids, asof):
        raise TimeoutError("partial source")
    monkeypatch.setattr(reconciliation_repository, "source_facts", unavailable)
    rows, stats = persist(task("next-month"), "2026-11-01")
    assert stats["reconciliation_failed"] == 1 and stats["superseded"] == 0
    assert rows[0].task_key == first[0].task_key
    assert db.tasks.count_documents({}) == 1


def test_same_terminal_cycle_stays_closed_across_month_rollover(db, monkeypatch):
    facts(monkeypatch, orders={"7": order()})
    first, _ = persist(task())
    lifecycle.change_status(first[0].task_key, TaskStatus.DONE, by=1)
    rows, _ = persist(task("November"), "2026-11-01")
    assert rows[0].task_key == first[0].task_key
    assert db.tasks.count_documents({"status": "open"}) == 0


def test_duplicate_legacy_monthly_advice_becomes_one_call(db, monkeypatch):
    facts(monkeypatch, orders={"7": order()})
    lifecycle.upsert_generated(task("month1"))
    lifecycle.upsert_generated(task("month2"))
    _, stats = reconciliation.prepare(1, [], "2026-10-16")
    assert stats["superseded"] == 1
    assert db.tasks.count_documents({"status": "open"}) == 1
    evidence = db.tasks.find_one({"status": "superseded"})["source_evidence"]
    assert evidence["kind"] == "duplicate_recommendation"


def test_manual_commitments_do_not_consume_per_client_generation_quota(db, monkeypatch):
    facts(monkeypatch, debts={"501": "12"})
    for i in range(4):
        lifecycle.create_manual(task(f"manual-{i}", kind=TaskType.MANUAL), created_by=1)
    assert lifecycle.active_counts_by_client(1) == {}
    assert lifecycle.active_counts_by_type(1) == {}
    generator = SimpleNamespace(__name__="debt", generate=lambda *args: [task(kind=TaskType.DEBT_FOLLOWUP)])
    monkeypatch.setattr(orchestrator, "_GENERATORS", [generator])
    from app.services import targets
    monkeypatch.setattr(targets, "compute_target", lambda *args: {})
    result = orchestrator.generate_for_manager(1, "2026-10-16")
    assert result["persisted"] == 1
    assert lifecycle.dashboard_counts(1, "2026-10-16")["manual_backlog"] == 4


def test_generator_failure_does_not_mean_absence(db, monkeypatch):
    facts(monkeypatch, orders={"7": order()})
    rows, _ = persist(task())
    def fail(*args):
        raise RuntimeError("model offline")
    monkeypatch.setattr(orchestrator, "_GENERATORS", [SimpleNamespace(__name__="failed", generate=fail)])
    from app.services import targets
    monkeypatch.setattr(targets, "compute_target", lambda *args: {})
    result = orchestrator.generate_for_manager(1, "2026-10-17")
    assert result["generators_failed"] == 1 and result["superseded"] == 0
    assert db.tasks.find_one({"task_key": rows[0].task_key})["status"] == "open"


def test_full_return_after_purchase_allows_renewed_need_without_resurrecting_history(db, monkeypatch):
    source = facts(monkeypatch, orders={"7": order()})
    first, _ = persist(task())
    source[10]["orders"]["7"] = order(101, "2026-10-17T11:00:00")
    reconciliation.prepare(1, [], "2026-10-17")
    assert db.tasks.find_one({"task_key": first[0].task_key})["status"] == "superseded"
    # That purchase is returned completely: the retained-purchase frontier goes back,
    # but positive return evidence gives the renewed need a new, stable identity.
    source[10]["orders"]["7"] = order()
    source[10]["returns"] = {"7": {"order_id": 101, "quantity": "20",
                                      "returned_at": "2026-10-18T12:00:00"}}
    candidate = task("returned")
    candidate.payload["products"][0]["elapsed_days"] = 17
    rows, _ = persist(candidate, "2026-10-18")
    assert rows[0].task_key != first[0].task_key
    assert db.tasks.count_documents({"status": "open"}) == 1
    assert db.tasks.count_documents({"status": "superseded"}) == 1
    again, _ = persist(task("November"), "2026-11-01")
    assert again[0].task_key == rows[0].task_key


def test_concurrent_seller_update_prevents_stale_resolution_and_commitment_copy(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "12"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    key = rows[0].task_key
    lifecycle.change_status(key, TaskStatus.IN_PROGRESS, by=1)
    stale = db.tasks.find_one({"task_key": key})
    # A seller changes the task after reconciliation's read. Millisecond precision is explicit.
    db.tasks.update_one({"task_key": key}, {"$set": {"updated_at": datetime.now(UTC) + timedelta(seconds=1),
                         "notes": [{"text": "Updated after snapshot"}]}})
    source[10]["debts"]["501"] = "0"
    assert reconciliation._supersede(stale, {"kind": "debt_settled"}, datetime.now(UTC)) is False
    assert db.tasks.count_documents({"task_type": "manual"}) == 0
    assert db.tasks.find_one({"task_key": key})["notes"][0]["text"] == "Updated after snapshot"


def test_interrupted_commitment_copy_repairs_on_next_run(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "12"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    key = rows[0].task_key
    lifecycle.change_status(key, TaskStatus.IN_PROGRESS, by=1)
    source[10]["debts"]["501"] = "0"
    original = reconciliation._preserve_commitment
    def interrupted(*args):
        raise RuntimeError("process stopped after status CAS")
    monkeypatch.setattr(reconciliation, "_preserve_commitment", interrupted)
    with pytest.raises(RuntimeError):
        reconciliation.prepare(1, [], "2026-10-17")
    assert db.tasks.find_one({"task_key": key})["status"] == "superseded"
    monkeypatch.setattr(reconciliation, "_preserve_commitment", original)
    reconciliation.prepare(1, [], "2026-10-17")
    assert db.tasks.count_documents({"source_task_key": key}) == 1
    assert "commitment_reconciliation_pending" not in db.tasks.find_one({"task_key": key})


def test_legacy_purchase_dates_are_conservative_and_public_evidence_is_date_only(db, monkeypatch):
    facts(monkeypatch, orders={"7": order(101, "2026-10-22T23:59:00")})
    lifecycle.upsert_generated(task("legacy"))
    # UTC generation on previous date can already be the same Kyiv business day.
    db.tasks.update_one({"task_key": "legacy"}, {"$set": {
        "generated_at": datetime(2026, 10, 21, 22, tzinfo=UTC)}})
    _, stats = reconciliation.prepare(1, [], "2026-10-22")
    assert stats["superseded"] == 0
    facts(monkeypatch, orders={"7": order(102, "2026-10-23T12:00:00")})
    _, stats = reconciliation.prepare(1, [], "2026-10-23")
    evidence = db.tasks.find_one({"task_key": "legacy"})["source_evidence"]["orders"]["7"]
    assert evidence == {"order_id": 102, "purchased_on": "2026-10-23", "date_basis": "source_document_date"}
