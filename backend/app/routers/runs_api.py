"""Runs API: create/execute runs, publish, adjustments, ledger and payments."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, serializers
from ..db import get_db
from ..deps import audit, current_user, require_approver, require_planner
from ..schemas import AdjustDecisionIn, PaymentIn, PublishIn, RunCreateIn
from ..services import orchestrator
from ..services.orchestrator import OrchestrationError
from ..utils import serialize_dt

router = APIRouter()


def _error(exc: OrchestrationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message if exc.detail is None else {"message": exc.message, "conflicts": exc.detail})


@router.get("/runs", tags=["runs"])
def list_runs(
    period_month: str | None = None,
    status: str | None = None,
    strategy: str | None = None,
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    stmt = select(models.Run).order_by(models.Run.created_at.desc()).limit(limit)
    if period_month:
        stmt = stmt.where(models.Run.period_month == period_month)
    if status:
        stmt = stmt.where(models.Run.status == status)
    if strategy:
        stmt = stmt.where(models.Run.strategy == strategy)
    return [serializers.run_brief(r, db) for r in db.scalars(stmt).all()]


@router.get("/runs/{run_id}", tags=["runs"])
def get_run(run_id: int, db: Session = Depends(get_db), _user=Depends(current_user)):
    run = db.get(models.Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    windows = db.scalars(
        select(models.RunWindow).where(models.RunWindow.run_id == run_id).order_by(models.RunWindow.customer_id)
    ).all()
    payload = serializers.run_brief(run, db)
    payload["batch_snapshot"] = run.batch_snapshot or []
    payload["rule_snapshot"] = run.rule_snapshot or {}
    payload["contract_snapshot"] = run.contract_snapshot or {}
    payload["scope_event_ids"] = run.scope_event_ids or []
    payload["windows"] = []
    unc_reports = db.scalars(
        select(models.UncertaintyReport).where(models.UncertaintyReport.run_id == run_id)
    ).all()
    unc_map = {(r.customer_id, r.event_id): r for r in unc_reports}
    for w in windows:
        customer = db.get(models.Customer, w.customer_id)
        event = db.get(models.Event, w.event_id)
        settlement = db.scalars(
            select(models.Node).where(
                models.Node.run_id == run_id,
                models.Node.customer_id == w.customer_id,
                models.Node.event_id == w.event_id,
                models.Node.node_type == "settlement",
            )
        ).first()
        review = db.scalars(
            select(models.Node).where(
                models.Node.run_id == run_id,
                models.Node.customer_id == w.customer_id,
                models.Node.event_id == w.event_id,
                models.Node.status == "needs_review",
            )
        ).first()
        unc = unc_map.get((w.customer_id, w.event_id))
        payload["windows"].append(
            {
                "customer_id": w.customer_id,
                "customer_code": customer.code if customer else None,
                "customer_name": customer.name if customer else None,
                "event_id": w.event_id,
                "event_code": event.code if event else None,
                "event_date": event.event_date.isoformat() if event else None,
                "status": w.status,
                "amount": float(settlement.output.get("amount")) if settlement and settlement.output else None,
                "response_kwh": float(settlement.output.get("response_kwh"))
                if settlement and settlement.output
                else None,
                "review_node": review.node_type if review else None,
                "reason": review.reason if review else None,
                "uncertainty": (
                    {
                        "report_id": unc.id,
                        "status": unc.status,
                        "half_width_kwh": float(unc.half_width_kwh) if unc.half_width_kwh is not None else None,
                        "relative_width": (unc.report or {}).get("interval", {}).get("relative_width"),
                    }
                    if unc
                    else None
                ),
            }
        )
    ledger_rows = db.scalars(select(models.LedgerEntry).where(models.LedgerEntry.run_id == run_id)).all()
    payload["ledger_entries"] = [serializers.ledger_entry(e, db) for e in ledger_rows]
    return payload


@router.post("/runs", tags=["runs"], status_code=201)
def create_run(body: RunCreateIn, db: Session = Depends(get_db), user=Depends(require_planner)):
    try:
        run = orchestrator.create_run(
            db,
            actor=user.username,
            period_month=body.period_month,
            strategy=body.strategy,
            event_id=body.event_id,
            batch_ids=body.batch_ids,
            contract_ids=body.contract_ids,
            rule_ids=body.rule_ids,
            notes=body.notes,
            debug_hold_seconds=body.debug_hold_seconds,
        )
    except OrchestrationError as exc:
        raise _error(exc) from exc
    audit(
        db,
        user.username,
        "run.create",
        "run",
        run.id,
        {"strategy": body.strategy, "period_month": body.period_month, "status": run.status},
    )
    db.commit()
    return serializers.run_brief(run, db)


@router.post("/runs/{run_id}/publish", tags=["publish"])
def publish(run_id: int, body: PublishIn, db: Session = Depends(get_db), user=Depends(require_approver)):
    try:
        result = orchestrator.publish_run(db, run_id, user.username, body.reason)
    except OrchestrationError as exc:
        raise _error(exc) from exc
    audit(db, user.username, "run.publish", "run", run_id, {"reason": body.reason})
    db.commit()
    return result


# ----------------------------- ledger -----------------------------
@router.get("/ledger", tags=["ledger"])
def list_ledger(
    period_month: str | None = None,
    customer_id: int | None = None,
    status: str | None = None,
    include_superseded: bool = True,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    stmt = select(models.LedgerEntry).order_by(
        models.LedgerEntry.period_month, models.LedgerEntry.customer_id, models.LedgerEntry.version
    )
    if period_month:
        stmt = stmt.where(models.LedgerEntry.period_month == period_month)
    if customer_id:
        stmt = stmt.where(models.LedgerEntry.customer_id == customer_id)
    if status:
        stmt = stmt.where(models.LedgerEntry.status == status)
    elif not include_superseded:
        stmt = stmt.where(models.LedgerEntry.status == "published")
    return [serializers.ledger_entry(e, db) for e in db.scalars(stmt).all()]


@router.post("/ledger/{ledger_id}/payment", tags=["ledger"])
def record_payment(ledger_id: int, body: PaymentIn, db: Session = Depends(get_db), user=Depends(require_planner)):
    # Late contract revisions after payment must go to adjustments, not rewrite vouchers.
    try:
        voucher = orchestrator.record_statement_voucher(db, ledger_id, body.reference, body.paid_at)
    except OrchestrationError as exc:
        raise _error(exc) from exc
    audit(db, user.username, "ledger.payment", "ledger_entry", ledger_id, {"reference": body.reference})
    db.commit()
    return {
        "voucher_id": voucher.id,
        "voucher_no": voucher.voucher_no,
        "amount": float(voucher.amount),
        "reference": voucher.reference,
        "paid_at": serialize_dt(voucher.paid_at),
    }


@router.get("/vouchers", tags=["ledger"])
def list_vouchers(
    kind: str | None = None, ledger_id: int | None = None, db: Session = Depends(get_db), _user=Depends(current_user)
):
    stmt = select(models.Voucher).order_by(models.Voucher.paid_at.desc())
    if kind:
        stmt = stmt.where(models.Voucher.kind == kind)
    if ledger_id:
        stmt = stmt.where(models.Voucher.ledger_id == ledger_id)
    return [
        {
            "id": v.id,
            "voucher_no": v.voucher_no,
            "kind": v.kind,
            "ledger_id": v.ledger_id,
            "adjustment_id": v.adjustment_id,
            "amount": float(v.amount),
            "reference": v.reference,
            "paid_at": serialize_dt(v.paid_at),
        }
        for v in db.scalars(stmt).all()
    ]


# --------------------------- adjustments ---------------------------
@router.get("/adjustments", tags=["adjustments"])
def list_adjustments(
    period_month: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    stmt = select(models.Adjustment).order_by(models.Adjustment.created_at.desc())
    if status:
        stmt = stmt.where(models.Adjustment.status == status)
    if period_month:
        stmt = stmt.join(models.Run, models.Run.id == models.Adjustment.run_id).where(
            models.Run.period_month == period_month
        )
    return [serializers.adjustment_row(a, db) for a in db.scalars(stmt).all()]


@router.post("/adjustments/{adjustment_id}/decision", tags=["adjustments"])
def decide_adjustment(
    adjustment_id: int, body: AdjustDecisionIn, db: Session = Depends(get_db), user=Depends(require_approver)
):
    try:
        adj = orchestrator.approve_adjustment(db, adjustment_id, user.username, body.approve)
    except OrchestrationError as exc:
        raise _error(exc) from exc
    audit(
        db,
        user.username,
        "adjustment.decide",
        "adjustment",
        adjustment_id,
        {"approve": body.approve, "comment": body.comment},
    )
    db.commit()
    return serializers.adjustment_row(adj, db)


@router.get("/adjustments/{adjustment_id}/trace", tags=["adjustments"])
def trace_adjustment(adjustment_id: int, db: Session = Depends(get_db), _user=Depends(current_user)):
    """From one adjustment down to every source used by the recomputation."""
    adj = db.get(models.Adjustment, adjustment_id)
    if adj is None:
        raise HTTPException(status_code=404, detail="调整记录不存在")
    run = db.get(models.Run, adj.run_id)
    window = db.scalars(
        select(models.RunWindow).where(
            models.RunWindow.run_id == adj.run_id,
            models.RunWindow.customer_id == adj.customer_id,
            models.RunWindow.event_id == adj.event_id,
        )
    ).first()
    nodes = db.scalars(
        select(models.Node)
        .where(
            models.Node.run_id == adj.run_id,
            models.Node.customer_id == adj.customer_id,
            models.Node.event_id == adj.event_id,
        )
        .order_by(models.Node.id)
    ).all()
    sources = []
    for node in nodes:
        for src in db.scalars(select(models.NodeSource).where(models.NodeSource.node_id == node.id)):
            entry = {
                "node_type": node.node_type,
                "source_type": src.source_type,
                "ref_id": src.ref_id,
                "meta": src.meta,
            }
            if src.source_type == "batch":
                batch = db.get(models.MeterBatch, src.ref_id)
                if batch:
                    meter = db.get(models.MeterPoint, batch.meter_point_id)
                    entry["meta"] = {
                        **(src.meta or {}),
                        "meter_no": meter.meter_no if meter else None,
                        "version": batch.version,
                        "checksum": batch.checksum[:12],
                        "coverage_start": serialize_dt(batch.coverage_start),
                        "coverage_end": serialize_dt(batch.coverage_end),
                    }
            sources.append(entry)
    original = db.get(models.LedgerEntry, adj.original_ledger_id)
    new = db.get(models.LedgerEntry, adj.new_ledger_id)
    vouchers = db.scalars(
        select(models.Voucher).where(
            (models.Voucher.ledger_id == adj.original_ledger_id)
            | (models.Voucher.adjustment_id == adj.id)
        )
    ).all()
    return {
        "adjustment": serializers.adjustment_row(adj, db),
        "run": serializers.run_brief(run, db) if run else None,
        "window_status": window.status if window else None,
        "cutoff_at": serialize_dt(run.cutoff_at) if run else None,
        "snapshot": {
            "batch_snapshot": run.batch_snapshot if run else [],
            "rule_snapshot": run.rule_snapshot if run else {},
            "contract_snapshot": run.contract_snapshot if run else {},
        },
        "nodes": [
            {"id": n.id, "node_type": n.node_type, "status": n.status, "reason": n.reason, "output": n.output}
            for n in nodes
        ],
        "sources": sources,
        "ledger_chain": [
            serializers.ledger_entry(e, db) for e in (original, new) if e is not None
        ],
        "vouchers": [
            {
                "id": v.id,
                "voucher_no": v.voucher_no,
                "kind": v.kind,
                "amount": float(v.amount),
                "reference": v.reference,
                "paid_at": serialize_dt(v.paid_at),
            }
            for v in vouchers
        ],
    }


@router.get("/compare", tags=["lineage"])
def compare(left_run_id: int, right_run_id: int, db: Session = Depends(get_db), _user=Depends(current_user)):
    """Window-by-window difference between two runs."""
    try:
        return serializers.compare_runs(db, left_run_id, right_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/audit", tags=["dashboard"])
def audit_log(limit: int = Query(default=50, le=300), db: Session = Depends(get_db), _user=Depends(current_user)):
    rows = db.scalars(select(models.AuditLog).order_by(models.AuditLog.created_at.desc()).limit(limit)).all()
    return [
        {
            "id": r.id,
            "actor": r.actor,
            "action": r.action,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "detail": r.detail,
            "created_at": serialize_dt(r.created_at),
        }
        for r in rows
    ]
