"""Run orchestration, exclusive window claims, immutable publish and adjustments."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..utils import aware_utc, event_intervals
from . import engine, snapshot

PASS = engine.PASS
REVIEW = engine.REVIEW


class OrchestrationError(ValueError):
    def __init__(self, message: str, status_code: int = 400, detail: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


# --------------------------------------------------------------------------
# scope resolution
# --------------------------------------------------------------------------
def period_events(db: Session, period_month: str) -> list[models.Event]:
    return list(
        db.scalars(
            select(models.Event)
            .where(models.Event.period_month == period_month)
            .order_by(models.Event.event_date, models.Event.start_time)
        )
    )


def all_customers(db: Session) -> list[models.Customer]:
    return list(db.scalars(select(models.Customer).order_by(models.Customer.id)))


def find_affected_windows(
    db: Session,
    *,
    batch_ids: list[int] | None = None,
    contract_ids: list[int] | None = None,
    rule_ids: list[int] | None = None,
) -> tuple[dict[int, set[int]], list[dict]]:
    """Trace lineage *and* infer impact for sources with no lineage yet.

    Two complementary paths:
    * lineage  – the source was already consumed by a run's nodes (exact, from
      ``node_sources``);
    * inferred – the source is brand new (a late batch or a revised rule /
      contract), so we intersect its coverage / effective range with events and
      customer master data.  Without this a newly arrived batch would silently
      never trigger a recalculation.
    """
    affected: dict[int, set[int]] = defaultdict(set)
    evidence: list[dict] = []

    wanted = {"batch": batch_ids or [], "contract": contract_ids or [], "rule": rule_ids or []}
    for source_type, refs in wanted.items():
        if not refs:
            continue
        rows = db.execute(
            select(models.Node, models.NodeSource, models.Run)
            .join(models.NodeSource, models.NodeSource.node_id == models.Node.id)
            .join(models.Run, models.Run.id == models.Node.run_id)
            .where(
                models.NodeSource.source_type == source_type,
                models.NodeSource.ref_id.in_(refs),
                models.Run.status.in_(["succeeded", "published", "needs_review"]),
            )
        ).all()
        for node, src, run in rows:
            affected[node.event_id].add(node.customer_id)
            evidence.append(
                {
                    "origin": "lineage",
                    "source_type": source_type,
                    "ref_id": src.ref_id,
                    "customer_id": node.customer_id,
                    "event_id": node.event_id,
                    "via_run_id": run.id,
                    "via_run_code": run.run_code,
                    "via_node_id": node.id,
                    "via_node_type": node.node_type,
                }
            )

    events = list(db.scalars(select(models.Event).order_by(models.Event.event_date)))
    customers = list(db.scalars(select(models.Customer).order_by(models.Customer.id)))

    for batch_id in batch_ids or []:
        batch = db.get(models.MeterBatch, batch_id)
        if batch is None:
            continue
        meter = db.get(models.MeterPoint, batch.meter_point_id)
        if meter is None:
            continue
        for event in events:
            slots = event_intervals(event.event_date, event.start_time, event.end_time)
            win_start = slots[0]
            win_end = slots[-1] + timedelta(minutes=15)
            if aware_utc(batch.coverage_start) <= win_start and aware_utc(batch.coverage_end) >= win_end:
                affected[event.id].add(meter.customer_id)
                evidence.append(
                    {
                        "origin": "inferred",
                        "source_type": "batch",
                        "ref_id": batch.id,
                        "customer_id": meter.customer_id,
                        "event_id": event.id,
                        "reason": "批次覆盖活动窗口，可替换历史输入",
                    }
                )

    for contract_id in contract_ids or []:
        contract = db.get(models.ContractVersion, contract_id)
        if contract is None:
            continue
        for event in events:
            if event.event_date >= contract.effective_from:
                affected[event.id].add(contract.customer_id)
                evidence.append(
                    {
                        "origin": "inferred",
                        "source_type": "contract",
                        "ref_id": contract.id,
                        "customer_id": contract.customer_id,
                        "event_id": event.id,
                        "reason": "签约修订生效日不晚于活动日",
                    }
                )

    for rule_id in rule_ids or []:
        rule = db.get(models.BaselineRuleVersion, rule_id)
        if rule is None:
            continue
        for event in events:
            if event.event_date < rule.effective_from:
                continue
            if rule.effective_to is not None and rule.effective_to < event.event_date:
                continue
            for customer in customers:
                affected[event.id].add(customer.id)
                evidence.append(
                    {
                        "origin": "inferred",
                        "source_type": "rule",
                        "ref_id": rule.id,
                        "customer_id": customer.id,
                        "event_id": event.id,
                        "reason": "规则版本在活动日适用，可能改变基线",
                    }
                )

    return {eid: set(cids) for eid, cids in affected.items()}, evidence


def lineage_window_map(db: Session, run_ids: list[int]) -> dict[tuple[int, int], models.RunWindow]:
    if not run_ids:
        return {}
    rows = db.scalars(
        select(models.RunWindow).where(models.RunWindow.run_id.in_(run_ids))
    ).all()
    return {(r.customer_id, r.event_id): r for r in rows}


# --------------------------------------------------------------------------
# claims
# --------------------------------------------------------------------------
def _claim_windows(db: Session, run: models.Run, windows: list[tuple[int, int]]) -> None:
    """Insert one active claim per window in its own transaction.

    A unique partial index (customer_id, event_id) WHERE status='active' makes
    this atomic: exactly one concurrent run can win each window.
    """
    for customer_id, event_id in windows:
        claim = models.RunWindowClaim(
            customer_id=customer_id, event_id=event_id, run_id=run.id, status="active"
        )
        db.add(claim)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if _reclaim_stale(db, customer_id, event_id):
                # Previous holder's run already reached a terminal state: retry once.
                db.add(
                    models.RunWindowClaim(
                        customer_id=customer_id, event_id=event_id, run_id=run.id, status="active"
                    )
                )
                try:
                    db.commit()
                    continue
                except IntegrityError:
                    db.rollback()
            _release_claims(db, run.id)
            holders = db.scalars(
                select(models.RunWindowClaim).where(
                    models.RunWindowClaim.customer_id == customer_id,
                    models.RunWindowClaim.event_id == event_id,
                    models.RunWindowClaim.status == "active",
                )
            ).all()
            detail = [
                {
                    "claim_id": h.id,
                    "run_id": h.run_id,
                    "run_code": db.get(models.Run, h.run_id).run_code if db.get(models.Run, h.run_id) else None,
                }
                for h in holders
            ]
            raise OrchestrationError(
                "窗口输入快照已被其它运行占用：同一客户窗口同一时刻只能有一个运行取得快照。"
                f"冲突窗口 customer_id={customer_id}, event_id={event_id}",
                409,
                detail,
            )


def _reclaim_stale(db: Session, customer_id: int, event_id: int) -> bool:
    """Release claims left behind by a run that already terminated abnormally."""
    holders = db.scalars(
        select(models.RunWindowClaim).where(
            models.RunWindowClaim.customer_id == customer_id,
            models.RunWindowClaim.event_id == event_id,
            models.RunWindowClaim.status == "active",
        )
    ).all()
    released = False
    now = datetime.now(timezone.utc)
    for claim in holders:
        holder_run = db.get(models.Run, claim.run_id)
        if holder_run is None or holder_run.status != "running":
            claim.status = "released"
            claim.released_at = now
            released = True
    if released:
        db.commit()
    return released


def release_abandoned_claims(db: Session) -> int:
    """Startup housekeeping: never let a crashed process block a customer window forever."""
    rows = db.scalars(
        select(models.RunWindowClaim).where(models.RunWindowClaim.status == "active")
    ).all()
    count = 0
    now = datetime.now(timezone.utc)
    for claim in rows:
        run = db.get(models.Run, claim.run_id)
        if run is None or run.status != "running":
            claim.status = "released"
            claim.released_at = now
            count += 1
    db.commit()
    return count


def _release_claims(db: Session, run_id: int) -> None:
    claims = db.scalars(
        select(models.RunWindowClaim).where(
            models.RunWindowClaim.run_id == run_id, models.RunWindowClaim.status == "active"
        )
    ).all()
    now = datetime.now(timezone.utc)
    for claim in claims:
        claim.status = "released"
        claim.released_at = now
    db.commit()


# --------------------------------------------------------------------------
# run creation
# --------------------------------------------------------------------------
def _allocate_run(
    db: Session,
    *,
    period_month: str,
    strategy: str,
    scope: list[tuple[models.Event, models.Customer]],
    cutoff: datetime,
    base_run_id: int | None,
    trigger_type: str,
    trigger_batch_id: int | None,
    notes: str,
    actor: str,
) -> models.Run:
    """Insert the run row, retrying on a concurrent version allocation."""
    last_error: Exception | None = None
    for _attempt in range(5):
        latest_version = db.scalar(
            select(func.coalesce(func.max(models.Run.version), 0)).where(
                models.Run.period_month == period_month
            )
        )
        version = int(latest_version or 0) + 1
        run = models.Run(
            run_code=f"RUN-{period_month.replace('-', '')}-{version:03d}",
            scope_type="period" if strategy == "full_period" else "event",
            period_month=period_month,
            event_id=(scope[0][0].id if len({e.id for e, _ in scope}) == 1 else None),
            strategy=strategy,
            status="running",
            version=version,
            base_run_id=base_run_id,
            cutoff_at=cutoff,
            batch_snapshot=[],
            rule_snapshot={},
            contract_snapshot={},
            trigger_type=trigger_type,
            trigger_batch_id=trigger_batch_id,
            scope_event_ids=sorted({e.id for e, _ in scope}),
            target_windows=len(scope),
            created_by=actor,
            notes=notes,
        )
        db.add(run)
        try:
            db.commit()
            db.refresh(run)
            return run
        except IntegrityError as exc:
            db.rollback()
            last_error = exc
    raise OrchestrationError(f"并发创建运行失败：{last_error}", 409)


def create_run(
    db: Session,
    *,
    actor: str,
    period_month: str,
    strategy: str = "initial",
    event_id: int | None = None,
    batch_ids: list[int] | None = None,
    contract_ids: list[int] | None = None,
    rule_ids: list[int] | None = None,
    affected_windows: dict[int, set[int]] | None = None,
    trigger_type: str = "manual",
    trigger_batch_id: int | None = None,
    notes: str = "",
    debug_hold_seconds: float = 0.0,
) -> models.Run:
    if strategy not in {"initial", "targeted", "full_period"}:
        raise OrchestrationError("strategy 必须是 initial / targeted / full_period")
    events = period_events(db, period_month)
    if not events:
        raise OrchestrationError(f"结算期 {period_month} 没有活动", 404)
    event_by_id = {e.id: e for e in events}
    customers = all_customers(db)
    if not customers:
        raise OrchestrationError("没有客户主数据", 409)

    evidence: list[dict] = []
    scope: list[tuple[models.Event, models.Customer]] = []
    affected_keys: set[tuple[int, int]] = set()

    if strategy == "targeted":
        if affected_windows is None:
            if not any([batch_ids, contract_ids, rule_ids]):
                raise OrchestrationError("定向重算需提供 batch_ids / contract_ids / rule_ids 之一")
            affected_windows, evidence = find_affected_windows(
                db, batch_ids=batch_ids, contract_ids=contract_ids, rule_ids=rule_ids
            )
        filtered = {eid: cs for eid, cs in affected_windows.items() if eid in event_by_id and cs}
        if not filtered:
            raise OrchestrationError(
                "依赖图中没有受影响的窗口（可能尚未有成功运行，建议先整期重跑）", 409
            )
        affected_keys = {(c.id, eid) for eid, cids in filtered.items() for c in customers if c.id in cids}
        # Whole period is in scope so the new ledger version stays complete;
        # unaffected windows are reused from their own latest successful run.
        scope = [(e, c) for e in events for c in customers]
    elif strategy == "full_period":
        scope = [(e, c) for e in events for c in customers]
    else:
        scope_events = [event_by_id[event_id]] if event_id in event_by_id else events
        if event_id is not None and event_id not in event_by_id:
            raise OrchestrationError("活动不属于该结算期", 404)
        scope = [(e, c) for e in scope_events for c in customers]

    if not scope:
        raise OrchestrationError("没有可计算的客户窗口", 409)

    cutoff = datetime.now(timezone.utc)

    base_run_id = None
    if strategy == "targeted":
        base_run_id = db.scalar(
            select(models.Run.id)
            .where(models.Run.period_month == period_month, models.Run.published_at.is_not(None))
            .order_by(models.Run.version.desc())
            .limit(1)
        )

    run = _allocate_run(
        db,
        period_month=period_month,
        strategy=strategy,
        scope=scope,
        cutoff=cutoff,
        base_run_id=base_run_id,
        trigger_type=trigger_type,
        trigger_batch_id=trigger_batch_id,
        notes=notes,
        actor=actor,
    )

    # Acquire the exclusive input-snapshot claim first, then freeze the snapshot.
    window_keys = [(c.id, e.id) for e, c in scope]
    run.base_run_id = base_run_id
    db.commit()

    affected: set[tuple[int, int]] = affected_keys if strategy == "targeted" else set()

    try:
        _claim_windows(db, run, window_keys)
        if debug_hold_seconds:
            import time as _time

            _time.sleep(max(0.0, min(float(debug_hold_seconds), 15.0)))
        _freeze_snapshot(db, run, scope, cutoff)
        db.commit()
        _execute(db, run, scope, cutoff, affected)
        db.commit()
    except OrchestrationError:
        _mark_failed(db, run)
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _mark_failed(db, run)
        raise OrchestrationError(f"运行失败：{type(exc).__name__}: {exc}", 500) from exc

    run = db.get(models.Run, run.id)
    _release_claims(db, run.id)
    db.commit()
    db.refresh(run)
    return run


def _mark_failed(db: Session, run: models.Run) -> None:
    db.rollback()
    fresh = db.get(models.Run, run.id)
    if fresh is not None:
        fresh.status = "failed"
        fresh.finished_at = datetime.now(timezone.utc)
        fresh.error = fresh.error or "运行中止"
        _release_claims(db, fresh.id)
    db.commit()


def _freeze_snapshot(
    db: Session,
    run: models.Run,
    scope: list[tuple[models.Event, models.Customer]],
    cutoff: datetime,
) -> None:
    """Freeze every batch/rule/contract row visible to this run at ``cutoff``."""
    customer_ids = {c.id for _, c in scope}
    meters = db.scalars(
        select(models.MeterPoint).where(models.MeterPoint.customer_id.in_(customer_ids))
    ).all()
    batches = db.scalars(
        select(models.MeterBatch)
        .where(
            models.MeterBatch.meter_point_id.in_([m.id for m in meters] or [-1]),
            models.MeterBatch.status == "accepted",
            models.MeterBatch.submitted_at <= cutoff,
        )
        .order_by(models.MeterBatch.id)
    ).all()
    run.batch_snapshot = [b.id for b in batches]

    rule_snapshot: dict[str, Any] = {}
    for event, _customer in scope:
        rule = snapshot.applicable_rule(db, event.event_date, cutoff)
        if rule:
            rule_snapshot[str(rule.id)] = {
                "rule_code": rule.rule_code,
                "version": rule.version,
                "lookback_days": int(rule.lookback_days),
                "min_sample_days": int(rule.min_sample_days),
                "adjustment_factor": float(rule.adjustment_factor),
            }
    run.rule_snapshot = rule_snapshot

    contract_snapshot: dict[str, Any] = {}
    for event, customer in scope:
        contract = snapshot.applicable_contract(db, customer.id, event.event_date, cutoff)
        if contract:
            contract_snapshot[str(contract.id)] = {
                "customer_id": customer.id,
                "version": contract.version,
                "capacity_kw": float(contract.capacity_kw),
            }
    run.contract_snapshot = contract_snapshot


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------
def _latest_successful_window(
    db: Session, customer_id: int, event_id: int
) -> tuple[models.Run | None, models.RunWindow | None]:
    rows = db.execute(
        select(models.RunWindow, models.Run)
        .join(models.Run, models.Run.id == models.RunWindow.run_id)
        .where(
            models.RunWindow.customer_id == customer_id,
            models.RunWindow.event_id == event_id,
            models.RunWindow.status == PASS,
            models.Run.status.in_([PASS, "needs_review"]),
        )
        .order_by(models.Run.version.desc())
    ).all()
    for window, run in rows:
        nodes = db.scalars(
            select(models.Node).where(
                models.Node.run_id == run.id,
                models.Node.customer_id == customer_id,
                models.Node.event_id == event_id,
            )
        ).all()
        by_type = {n.node_type: n for n in nodes}
        required = ("ingest", "baseline", "response", "settlement")
        if all(t in by_type and by_type[t].status == PASS for t in required):
            return run, window
    return None, None


def _execute(
    db: Session,
    run: models.Run,
    scope: list[tuple[models.Event, models.Customer]],
    cutoff: datetime,
    affected: set[tuple[int, int]] | None = None,
) -> None:
    meter_map: dict[int, list[models.MeterPoint]] = defaultdict(list)
    for meter in db.scalars(select(models.MeterPoint).order_by(models.MeterPoint.id)):
        meter_map[meter.customer_id].append(meter)

    outcomes: list[engine.WindowOutcome] = []
    computed_count = 0
    reused_count = 0
    rule_cache: dict[int, models.BaselineRuleVersion | None] = {}
    contract_cache: dict[tuple[int, int], models.ContractVersion | None] = {}
    # Shared eligibility/weather/history cache for the whole run (one cut-off).
    analysis_cache: dict = {}

    for event, customer in scope:
        if event.id not in rule_cache:
            rule_cache[event.id] = snapshot.applicable_rule(db, event.event_date, cutoff)
        key = (customer.id, event.id)
        if key not in contract_cache:
            contract_cache[key] = snapshot.applicable_contract(db, customer.id, event.event_date, cutoff)
        rule = rule_cache[event.id]
        contract = contract_cache[key]

        base_run, base_window = (None, None)
        if run.strategy == "targeted" and (customer.id, event.id) not in (affected or set()):
            base_run, base_window = _latest_successful_window(db, customer.id, event.id)
        reuse = base_run is not None and base_window is not None
        if reuse:
            try:
                outcome = engine.clone_window_from(
                    db, run, base_window, customer, event, meter_map.get(customer.id, [])
                )
                reused_count += 1
            except ValueError:
                reuse = False
        if not reuse:
            outcome = engine.compute_window(
                db, run, customer, event, rule, contract, meter_map.get(customer.id, []),
                extras=analysis_cache,
            )
            computed_count += 1
        window_status = PASS if outcome.status == PASS else REVIEW
        db.add(
            models.RunWindow(
                run_id=run.id,
                customer_id=customer.id,
                event_id=event.id,
                status=window_status,
                reused=reuse,
                base_window_id=base_window.id if reuse and base_window else None,
            )
        )
        db.flush()
        outcomes.append(outcome)

    run.computed_windows = computed_count
    run.reused_windows = reused_count
    run.target_windows = len(outcomes)
    run.node_count = int(
        db.scalar(select(func.count()).select_from(models.Node).where(models.Node.run_id == run.id)) or 0
    )
    run.status = PASS if all(o.status == PASS and o.amount is not None for o in outcomes) else REVIEW
    run.finished_at = datetime.now(timezone.utc)
    run.duration_ms = int((run.finished_at - run.created_at).total_seconds() * 1000)
    db.flush()


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------
def publish_run(db: Session, run_id: int, actor: str, reason: str = "") -> dict:
    run = db.scalars(select(models.Run).where(models.Run.id == run_id).with_for_update()).first()
    if run is None:
        raise OrchestrationError("运行不存在", 404)
    if run.published_at is not None:
        raise OrchestrationError(f"运行 {run.run_code} 已发布；账本不可重复发布", 409)
    if run.status != PASS:
        raise OrchestrationError(
            f"运行 {run.run_code} 状态为 {run.status}；只有完整成功的运行才能发布账本，"
            "待核窗口不允许以零值或部分结果出账",
            409,
        )
    windows = db.scalars(select(models.RunWindow).where(models.RunWindow.run_id == run.id)).all()
    if not windows:
        raise OrchestrationError("运行没有客户窗口，无法发布", 409)
    if any(w.status != PASS for w in windows):
        raise OrchestrationError("存在待核窗口，账本发布已拒绝", 409)

    created: list[models.LedgerEntry] = []
    adjustments: list[models.Adjustment] = []

    for window in windows:
        node = db.scalars(
            select(models.Node).where(
                models.Node.run_id == run.id,
                models.Node.customer_id == window.customer_id,
                models.Node.event_id == window.event_id,
                models.Node.node_type == "settlement",
                models.Node.status == PASS,
            )
        ).first()
        if node is None:
            raise OrchestrationError("窗口标记成功但缺少结算节点，发布中止", 409)
        amount = float(node.output.get("amount") or 0.0)
        response_kwh = float(node.output.get("response_kwh") or 0.0)
        event = db.get(models.Event, window.event_id)
        previous = db.scalars(
            select(models.LedgerEntry)
            .where(
                models.LedgerEntry.customer_id == window.customer_id,
                models.LedgerEntry.event_id == window.event_id,
                models.LedgerEntry.status.in_(["published", "superseded"]),
            )
            .order_by(models.LedgerEntry.version.desc())
            .limit(1)
        ).first()
        if previous is not None:
            previous_run = db.get(models.Run, previous.run_id)
            if previous_run and previous_run.version > run.version:
                raise OrchestrationError(
                    f"窗口已有更新版本运行 {previous_run.run_code} 的账本，拒绝倒序发布", 409
                )
        version_no = (previous.version + 1) if previous else 1
        entry = models.LedgerEntry(
            customer_id=window.customer_id,
            event_id=window.event_id,
            run_id=run.id,
            node_id=node.id,
            period_month=event.period_month,
            version=version_no,
            amount=round(amount, 2),
            response_kwh=round(response_kwh, 3),
            baseline_kwh=node.output.get("baseline_kwh"),
            actual_kwh=node.output.get("actual_kwh"),
            capacity_kwh=node.output.get("capacity_kwh"),
            status="published",
            supersedes_id=previous.id if previous else None,
            published_by=actor,
        )
        db.add(entry)
        db.flush()
        created.append(entry)
        if previous is not None:
            previous.superseded_by_id = entry.id
            previous.status = "superseded"
            delta_amount = round(amount - float(previous.amount), 2)
            delta_kwh = round(response_kwh - float(previous.response_kwh), 3)
            if abs(delta_amount) < 0.005 and abs(delta_kwh) < 0.0005:
                continue
            adjustment = models.Adjustment(
                code=f"ADJ-{run.period_month.replace('-', '')}-{run.version:03d}-{entry.id:05d}",
                customer_id=window.customer_id,
                event_id=window.event_id,
                original_ledger_id=previous.id,
                new_ledger_id=entry.id,
                run_id=run.id,
                delta_amount=delta_amount,
                delta_kwh=delta_kwh,
                reason=reason or f"{run.run_code} 重算差异（{run.strategy}）",
                status="pending",
            )
            db.add(adjustment)
            db.flush()
            adjustments.append(adjustment)

    run.published_at = datetime.now(timezone.utc)
    run.published_by = actor
    db.commit()
    return {
        "run_id": run.id,
        "run_code": run.run_code,
        "ledger_entry_ids": [e.id for e in created],
        "adjustment_ids": [a.id for a in adjustments],
        "adjustment_total": round(sum(float(a.delta_amount) for a in adjustments), 2),
        "published_at": run.published_at.isoformat(),
    }


# --------------------------------------------------------------------------
# payment vouchers & adjustments
# --------------------------------------------------------------------------
def record_statement_voucher(
    db: Session, ledger_id: int, reference: str, paid_at: datetime | None = None
) -> models.Voucher:
    entry = db.scalars(select(models.LedgerEntry).where(models.LedgerEntry.id == ledger_id).with_for_update()).first()
    if entry is None:
        raise OrchestrationError("账本记录不存在", 404)
    existing = db.scalars(select(models.Voucher).where(models.Voucher.ledger_id == ledger_id)).first()
    if existing:
        return existing
    voucher = models.Voucher(
        voucher_no=f"STV-{entry.period_month.replace('-', '')}-{entry.id:05d}",
        kind="statement",
        ledger_id=entry.id,
        amount=entry.amount,
        reference=reference,
        paid_at=paid_at or datetime.now(timezone.utc),
    )
    db.add(voucher)
    db.flush()
    entry.paid = True
    entry.paid_amount = entry.amount
    db.commit()
    db.refresh(voucher)
    return voucher


def approve_adjustment(db: Session, adjustment_id: int, actor: str, approve: bool) -> models.Adjustment:
    adj = db.scalars(
        select(models.Adjustment).where(models.Adjustment.id == adjustment_id).with_for_update()
    ).first()
    if adj is None:
        raise OrchestrationError("调整记录不存在", 404)
    if adj.status != "pending":
        raise OrchestrationError(f"调整记录已处理（{adj.status}）", 409)
    adj.status = "approved" if approve else "rejected"
    adj.approved_at = datetime.now(timezone.utc)
    adj.approved_by = actor
    if approve:
        original = db.get(models.LedgerEntry, adj.original_ledger_id)
        original_voucher = db.scalars(
            select(models.Voucher).where(models.Voucher.ledger_id == adj.original_ledger_id)
        ).first()
        voucher = models.Voucher(
            voucher_no=f"ADJV-{adj.code}",
            kind="adjustment",
            ledger_id=None,
            adjustment_id=adj.id,
            amount=adj.delta_amount,
            reference=(
                f"ADJ:{adj.code}"
                + (f"|ORIG:{original_voucher.voucher_no}" if original_voucher else "")
                + (f"|LEDGER:{original.id}" if original else "")
            ),
            paid_at=datetime.now(timezone.utc),
        )
        db.add(voucher)
        db.flush()
        adj.adjustment_voucher_id = voucher.id
    db.commit()
    db.refresh(adj)
    return adj
