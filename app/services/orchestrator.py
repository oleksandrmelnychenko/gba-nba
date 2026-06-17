"""Generation orchestrator: run generators → apply anti-spam/throttle → persist.

Window tag = year-month (dedup horizon: one task of a type per client per month, via task_key).
Anti-spam rules (from manager_prefs / config):
  - skip (client, task_type) muted after a recent DISMISS;
  - cap tasks per client per generation run (max_tasks_per_client_per_day);
  - cap total active tasks per manager (max_active_tasks_per_manager).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.models import TaskType
from app.services import lifecycle
from app.services.generators import (
    churn_winback,
    cross_sell,
    debt_followup,
    new_client_activation,
    reorder_due,
)

log = get_logger("orchestrator")

# generators ordered by default business value (debt first)
_GENERATORS = [debt_followup, reorder_due, churn_winback, new_client_activation, cross_sell]

# Per-type share of the manager cap. On real data each of debt/reorder/churn alone produces
# more candidates than the cap, so without a quota the highest-confidence type (debt) fills the
# whole inbox. The quota keeps it a balanced, actionable mix; leftover capacity (e.g. when
# cross_sell is empty because gba-reco is offline) is redistributed in a second pass.
_TYPE_SHARE = {
    TaskType.DEBT_FOLLOWUP: 0.35,
    TaskType.REORDER_DUE: 0.25,
    TaskType.CHURN_WINBACK: 0.15,
    TaskType.NEW_CLIENT_ACTIVATION: 0.10,
    TaskType.CROSS_SELL: 0.15,
}

# Pace coupling: a manager behind their monthly target gets a proportional priority lift —
# revenue tasks (reorder/cross_sell) when SHIPPED is behind, debt-collection when PAID is behind —
# up to max_pace_boost (config) at 100% behind pace. This is how the target gap "drives" the queue.
_SHIPPED_BOOST_TYPES = {TaskType.REORDER_DUE, TaskType.CROSS_SELL, TaskType.NEW_CLIENT_ACTIVATION}
_PAID_BOOST_TYPES = {TaskType.DEBT_FOLLOWUP}


def _pace_deficit(metric: dict) -> float:
    """Fraction behind pace (0 if on/ahead): max(0, gap) / expected_to_date."""
    expected = metric.get("expected_to_date", 0.0)
    return max(0.0, metric.get("gap", 0.0)) / expected if expected > 0 else 0.0


def _apply_pace_boost(candidates: list, pace: dict) -> None:
    span = get_settings().max_pace_boost - 1.0
    shipped = 1.0 + min(_pace_deficit(pace.get("shipped", {})), 1.0) * span
    paid = 1.0 + min(_pace_deficit(pace.get("paid", {})), 1.0) * span
    for t in candidates:
        if t.task_type in _SHIPPED_BOOST_TYPES:
            factor = shipped
        elif t.task_type in _PAID_BOOST_TYPES:
            factor = paid
        else:
            factor = 1.0
        if factor > 1.0:
            t.priority = min(100.0, round(t.priority * factor, 2))


def _apply_feedback_penalty(candidates: list, rejections: dict) -> None:
    """Lower the priority of (client, task_type) pairs the manager keeps rejecting (dismissed /
    done-not-sold) — the queue learns from behaviour. Penalty caps at the configured floor."""
    if not rejections:
        return
    s = get_settings()
    for t in candidates:
        n = rejections.get((t.client_id, t.task_type.value), 0)
        if n:
            factor = max(s.feedback_penalty_floor, 1.0 - s.feedback_penalty_per_rejection * n)
            t.priority = round(t.priority * factor, 2)


def _window_tag(as_of: str) -> str:
    return as_of[:7]  # YYYY-MM


def generate_for_manager(manager_id: int, as_of: str | None = None) -> dict:
    s = get_settings()
    as_of = as_of or datetime.now(UTC).strftime("%Y-%m-%d")
    window = _window_tag(as_of)

    # collect candidates from all generators
    candidates = []
    for gen in _GENERATORS:
        try:
            candidates.extend(gen.generate(manager_id, as_of, window))
        except Exception as exc:  # noqa: BLE001
            log.warning("generator_failed", generator=gen.__name__, manager_id=manager_id, error=str(exc))

    # pace coupling: behind monthly target -> lift revenue (shipped) / debt (paid) task priority.
    # Graceful: if targets/DB are unavailable, generation proceeds without the boost.
    try:
        from app.services import targets
        _apply_pace_boost(candidates, targets.compute_target(manager_id, as_of))
    except Exception as exc:  # noqa: BLE001
        log.warning("pace_boost_skipped", manager_id=manager_id, error=str(exc))

    # feedback learning: sink (client,type) pairs the manager keeps rejecting
    try:
        _apply_feedback_penalty(candidates, lifecycle.feedback_rejections(manager_id, s.feedback_window_days))
    except Exception as exc:  # noqa: BLE001
        log.warning("feedback_penalty_skipped", manager_id=manager_id, error=str(exc))

    # highest priority first so caps keep the BEST tasks
    candidates.sort(key=lambda t: t.priority, reverse=True)

    cap = s.max_active_tasks_per_manager
    type_cap = {tt: max(1, round(share * cap)) for tt, share in _TYPE_SHARE.items()}

    # seed caps from tasks ALREADY active this window so re-runs (on-demand /generate) can't exceed
    # the per-client/per-type daily caps; existing tasks are refreshed, not re-counted.
    per_client: dict[int, int] = defaultdict(int, lifecycle.active_counts_by_client(manager_id))
    per_type: dict[TaskType, int] = defaultdict(int)
    _valid_types = {tt.value: tt for tt in TaskType}
    for raw_type, n in lifecycle.active_counts_by_type(manager_id).items():
        if raw_type in _valid_types:
            per_type[_valid_types[raw_type]] = n
    active = lifecycle.active_count(manager_id)
    counters = {"persisted": 0, "skipped_muted": 0, "skipped_capped": 0, "refreshed": 0}

    def attempt(task) -> None:
        nonlocal active
        if lifecycle.get_task(task.task_key) is not None:
            lifecycle.upsert_generated(task)   # refresh computed fields; no-op if terminal; no re-count
            counters["refreshed"] += 1
            return
        if active >= cap:
            counters["skipped_capped"] += 1
            return
        if lifecycle.is_muted(manager_id, task.client_id, task.task_type.value):
            counters["skipped_muted"] += 1
            return
        if per_client[task.client_id] >= s.max_tasks_per_client_per_day:
            counters["skipped_capped"] += 1
            return
        lifecycle.upsert_generated(task)
        per_client[task.client_id] += 1
        per_type[task.task_type] += 1
        active += 1
        counters["persisted"] += 1

    # pass 1: respect per-type diversity quota; defer overflow
    deferred = []
    for task in candidates:
        if per_type[task.task_type] >= type_cap.get(task.task_type, cap):
            deferred.append(task)
            continue
        attempt(task)
    # pass 2: fill any leftover global capacity, quota lifted (best remaining first)
    for task in deferred:
        attempt(task)

    stats = {"manager_id": manager_id, "as_of": as_of, "candidates": len(candidates),
             "by_type": {tt.value: n for tt, n in per_type.items()}, **counters}
    log.info("generation_done", **stats)
    return stats
