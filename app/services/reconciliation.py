"""Reconcile AI advice against positive business facts, never generator absence.

Keys identify a purchase/debt cycle, not a calendar window or mutable manager assignment.
Existing manual commitments and contact events survive independently of an AI suggestion.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.data import mongo, reconciliation_repository
from app.domain.models import ACTIVE, TaskType

log = get_logger("reconciliation")
_ACTIVE = [status.value for status in ACTIVE]


def _positive(value) -> bool:
    return Decimal(str(value)) > 0


def _products(task) -> list[str]:
    return sorted({str(row["product_id"]) for row in task.get("payload", {}).get("products", [])
                   if row.get("product_id") is not None})


def _baseline(task, facts: dict) -> dict:
    kind = task["task_type"]
    if kind == TaskType.DEBT_FOLLOWUP:
        # Identity must be the exact actionable documents behind this advice. An unrelated
        # not-yet-due or aged-out balance must not keep a paid collection call alive.
        ids = {str(value) for value in task.get("payload", {}).get("debt", {}).get("debt_ids", [])}
        return {"debts": {key: value for key, value in facts["debts"].items()
                          if key in ids and _positive(value)}}
    ids = _products(task)
    return {"orders": {key: value for key, value in facts["orders"].items() if not ids or key in ids},
            "returns": {key: value for key, value in facts.get("returns", {}).items()
                        if not ids or key in ids}}


def _cycle(kind: str, baseline: dict) -> str | None:
    # Balances and observation dates change within one debt cycle; only source identity belongs here.
    identity = (sorted(baseline["debts"]) if "debts" in baseline else
                sorted((pid, value["order_id"]) for pid, value in baseline.get("orders", {}).items()))
    returns = sorted((pid, row["order_id"], row["quantity"])
                     for pid, row in baseline.get("returns", {}).items())
    if not identity and not returns:
        return None
    return hashlib.sha256(json.dumps([kind, identity, returns], sort_keys=True).encode()).hexdigest()[:24]


def _newer(current: dict, prior: dict | None, since) -> bool:
    if prior:
        if current["order_id"] == prior["order_id"]:
            return False  # corrected document dates are not a second purchase
        current_day = date.fromisoformat(current["purchased_at"][:10])
        prior_day = date.fromisoformat(prior["purchased_at"][:10])
        # No durable per-row timezone provenance exists for this mixed imported/native
        # source. Same-day timestamp ordering cannot safely prove a later purchase.
        return current_day > prior_day
    if since is None:
        return False
    if isinstance(since, str):
        since = datetime.fromisoformat(since)
    # Imported Order.Created is a source document wall clock, not reliably UTC.
    # A legacy task with no baseline can only be resolved by a strictly later document day.
    generated_day = since.replace(tzinfo=since.tzinfo or UTC).astimezone(ZoneInfo("Europe/Kyiv")).date()
    return date.fromisoformat(current["purchased_at"][:10]) > generated_day


def _resolution(task: dict, facts: dict) -> dict | None:
    baseline = task.get("source_baseline") or {}
    if task["task_type"] == TaskType.DEBT_FOLLOWUP:
        ids = list(baseline.get("debts", {}))
        # Every identified debt must still exist with an explicit nonpositive balance.
        # Missing/deleted debt rows, an empty response or an aged-out generator result prove nothing.
        if ids and all(key in facts["debts"] and not _positive(facts["debts"][key]) for key in ids):
            return {"kind": "debt_settled", "debts": {key: facts["debts"][key] for key in ids}}
        return None
    ids = _products(task)
    orders = facts["orders"]
    since = task.get("generated_at")
    if not ids:  # client-level winback advice requires an actual new retained purchase
        relevant = {pid: value for pid, value in orders.items()
                    if _newer(value, baseline.get("orders", {}).get(pid), since)}
        return {"kind": "purchase", "orders": relevant} if relevant else None
    purchased = {pid: orders[pid] for pid in ids if pid in orders and
                 _newer(orders[pid], baseline.get("orders", {}).get(pid), since)}
    # Bundled advice remains useful until all suggested products have been bought.
    return {"kind": "purchase", "orders": purchased} if len(purchased) == len(ids) else None


def _candidate_already_satisfied(candidate, facts: dict, as_of: str) -> bool:
    products = candidate.payload.get("products", [])
    if candidate.task_type == TaskType.CROSS_SELL:
        return bool(products) and all(str(p["product_id"]) in facts["orders"] for p in products)
    if candidate.task_type == TaskType.REORDER_DUE:
        # The generator reads an exclusive as_of boundary. A later retained purchase,
        # including today's order, is stronger evidence than that stale candidate.
        return bool(products) and all(
            str(p["product_id"]) in facts["orders"] and p.get("elapsed_days") is not None and
            date.fromisoformat(facts["orders"][str(p["product_id"])]["purchased_at"][:10]) >
            date.fromisoformat(as_of) - timedelta(days=int(p["elapsed_days"])) for p in products)
    if candidate.task_type == TaskType.CHURN_WINBACK:
        return any(order["purchased_at"][:10] >= as_of for order in facts["orders"].values())
    return False


def _preserve_commitment(task: dict, now: datetime) -> None:
    # Notes remain on the historical advice. Explicit work/snooze state also retains a live
    # follow-up independently of the AI quota. Contact-event deadlines already live separately.
    if task["status"] not in ("in_progress", "snoozed") and not task.get("in_progress_since"):
        return
    key = "commitment|" + task["task_key"]
    document = {field: value for field, value in task.items() if field != "_id"}
    document.update(task_key=key, task_type="manual", origin="seller_commitment",
                    title="Наступний контакт: " + (task.get("client_name") or str(task["client_id"])),
                    reason="Збережене домовлене продовження після оновлення AI-поради",
                    source_task_key=task["task_key"], expires_at=None, updated_at=now,
                    status="snoozed" if task["status"] == "snoozed" else "in_progress")
    mongo.tasks().update_one({"task_key": key}, {"$setOnInsert": document}, upsert=True)


def _supersede(task: dict, evidence: dict, now: datetime) -> bool:
    if evidence.get("orders"):
        evidence = {**evidence, "orders": {pid: {
            "order_id": row["order_id"], "purchased_on": row["purchased_at"][:10],
            "date_basis": "source_document_date",
        } for pid, row in evidence["orders"].items()}}
    result = mongo.tasks().update_one(
        {"_id": task["_id"], "status": task["status"], "manager_id": task["manager_id"],
         "updated_at": task.get("updated_at")},
        {"$set": {"status": "superseded", "resolved_at": now, "updated_at": now,
                  "source_evidence": {**evidence, "observed_at": now},
                  "commitment_reconciliation_pending": True},
         "$push": {"status_history": {"from": task["status"], "to": "superseded", "at": now,
                                       "by": "system", "reason": evidence["kind"]}}})
    if result.modified_count:
        _preserve_commitment(task, now)
        mongo.tasks().update_one({"_id": task["_id"]},
                                 {"$unset": {"commitment_reconciliation_pending": ""}})
    return bool(result.modified_count)


def prepare(manager_id: int, candidates: list, as_of: str) -> tuple[list, dict]:
    stats = {"superseded": 0, "reassigned": 0, "reconciliation_failed": 0}
    candidate_ids = [task.client_id for task in candidates]
    # Repair an interrupted post-resolution commitment copy before doing further work.
    for pending in mongo.tasks().find({"manager_id": manager_id, "status": "superseded",
                                       "commitment_reconciliation_pending": True}):
        previous_status = pending.get("status_history", [{}])[-1].get("from", "open")
        _preserve_commitment({**pending, "status": previous_status}, datetime.now(UTC))
        mongo.tasks().update_one({"_id": pending["_id"]},
                                 {"$unset": {"commitment_reconciliation_pending": ""}})
    scope = {"status": {"$in": _ACTIVE}, "$or": [
        {"manager_id": manager_id}, {"client_id": {"$in": candidate_ids}}]}
    tasks = list(mongo.tasks().find(scope))
    ids = sorted(set(candidate_ids + [task["client_id"] for task in tasks if task.get("client_id")]))
    if not ids:
        return candidates, stats
    try:
        facts = reconciliation_repository.source_facts(ids, as_of)
    except Exception as exc:
        log.warning("source_reconciliation_failed", manager_id=manager_id, error=type(exc).__name__)
        stats["reconciliation_failed"] = 1
        # Preserve active identity even during source failure; no calendar rollover duplicates.
        facts = {}
    stats["source_unverified_clients"] = len(set(ids) - set(facts))
    now = datetime.now(UTC)
    for task in tasks:
        source = facts.get(task.get("client_id"))
        if source is None:
            continue
        if source["manager_id"] != task["manager_id"]:
            owner = source["manager_id"]
            result = mongo.tasks().update_one(
                {"_id": task["_id"], "manager_id": task["manager_id"], "status": task["status"],
                 "updated_at": task.get("updated_at")},
                {"$set": {"manager_id": owner, "updated_at": now},
                 "$push": {"assignment_history": {"from": task["manager_id"], "to": owner,
                                                    "at": now, "source": "Client.MainManagerID"}}})
            if result.modified_count:
                task.update(manager_id=owner, updated_at=now)
                stats["reassigned"] += 1
            else:
                # A seller changed this task after our read. Never resolve/copy its stale
                # commitment; retry from a fresh snapshot on the next generation.
                continue
        if task["task_type"] == TaskType.MANUAL:
            continue
        evidence = _resolution(task, source)
        if evidence and _supersede(task, evidence, now):
            task["status"] = "superseded"
            stats["superseded"] += 1
        elif not task.get("source_baseline"):
            baseline = _baseline(task, source)
            mongo.tasks().update_one({"_id": task["_id"], "source_baseline": None},
                                     {"$set": {"source_baseline": baseline}})
            task["source_baseline"] = baseline

    # Collapse old monthly duplicates only when the actual client owner is positively known.
    seen = {}
    for task in sorted(tasks, key=lambda item: (item.get("status") not in ("in_progress", "snoozed"),
                                               item["task_key"])):
        if task["status"] not in _ACTIVE or task["task_type"] == TaskType.MANUAL:
            continue
        identity = (task.get("client_id"), task["task_type"])
        if identity in seen and task.get("client_id") in facts:
            evidence = {"kind": "duplicate_recommendation", "superseded_by": seen[identity]["task_key"]}
            if _supersede(task, evidence, now):
                task["status"] = "superseded"
                stats["superseded"] += 1
        else:
            seen[identity] = task

    retained = []
    active = {(task["client_id"], task["task_type"]): task for task in tasks
              if task["status"] in _ACTIVE and task.get("client_id")}
    for candidate in candidates:
        source = facts.get(candidate.client_id)
        previous = active.get((candidate.client_id, candidate.task_type.value))
        if source and source["manager_id"] != manager_id:
            continue  # candidate was generated before the confirmed manager transfer
        if source and _candidate_already_satisfied(candidate, source, as_of):
            continue
        if previous:
            if previous["manager_id"] != manager_id:
                continue
            candidate.task_key = previous["task_key"]
            candidate.source_baseline = previous.get("source_baseline") or {}
            if source:
                # A refreshed suggestion may add products/documents. Capture their current
                # facts now; purchases predating this addition cannot resolve new advice.
                fresh = _baseline(candidate.model_dump(), source)
                candidate.source_baseline = {
                    name: {**values, **candidate.source_baseline.get(name, {})}
                    for name, values in fresh.items()
                }
        elif source:
            candidate.source_baseline = _baseline(candidate.model_dump(), source)
            cycle = _cycle(candidate.task_type, candidate.source_baseline)
            # No positive identity means unknown, never another monthly duplicate.
            candidate.task_key = (f"client:{candidate.client_id}|type:{candidate.task_type.value}|"
                                  f"cycle:{cycle or 'unverified'}")
            terminal = mongo.tasks().find_one({"client_id": candidate.client_id,
                "task_type": candidate.task_type.value, "status": {"$in": ["done", "dismissed"]}},
                sort=[("generated_at", -1)])
            if terminal:
                previous_cycle = _cycle(candidate.task_type, terminal.get("source_baseline") or {})
                if previous_cycle == cycle or (previous_cycle is None and not _resolution(terminal, source)):
                    candidate.task_key = terminal["task_key"]
        else:
            # Keep legacy identity when source reads fail; do not guess a new business cycle.
            existing = mongo.tasks().find_one({"client_id": candidate.client_id,
                                                "task_type": candidate.task_type.value},
                                               sort=[("generated_at", -1)])
            if existing:
                candidate.task_key = existing["task_key"]
        retained.append(candidate)
    return retained, stats
