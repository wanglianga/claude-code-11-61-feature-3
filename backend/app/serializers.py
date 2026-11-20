"""JSON serializers shared by routers."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .utils import serialize_dt


def customer_brief(c: models.Customer) -> dict:
    return {"id": c.id, "code": c.code, "name": c.name, "region": c.region}


def run_brief(r: models.Run, db: Session | None = None) -> dict:
    event = getattr(r, "event", None)
    window_counts = {"success": 0, "needs_review": 0}
    if db is not None:
        rows = db.execute(
            select(models.RunWindow.status, func.count())
            .where(models.RunWindow.run_id == r.id)
            .group_by(models.RunWindow.status)
        ).all()
        window_counts = {status: int(count) for status, count in rows}
    return {
        "id": r.id,
        "run_code": r.run_code,
        "period_month": r.period_month,
        "strategy": r.strategy,
        "scope_type": r.scope_type,
        "event_id": r.event_id,
        "event_code": event.code if event else None,
        "event_name": event.name if event else None,
        "status": r.status,
        "version": r.version,
        "base_run_id": r.base_run_id,
        "trigger_type": r.trigger_type,
        "trigger_batch_id": r.trigger_batch_id,
        "target_windows": r.target_windows,
        "computed_windows": r.computed_windows,
        "reused_windows": r.reused_windows,
        "node_count": r.node_count,
        "window_counts": window_counts,
        "cutoff_at": serialize_dt(r.cutoff_at),
        "created_at": serialize_dt(r.created_at),
        "finished_at": serialize_dt(r.finished_at),
        "published_at": serialize_dt(r.published_at),
        "published_by": r.published_by,
        "duration_ms": r.duration_ms,
        "created_by": r.created_by,
        "notes": r.notes,
        "error": r.error,
        "batch_snapshot_count": len(r.batch_snapshot or []),
        "rule_snapshot_versions": [v["version"] for v in (r.rule_snapshot or {}).values()],
    }


def ledger_entry(e: models.LedgerEntry, db: Session | None = None) -> dict:
    customer = db.get(models.Customer, e.customer_id) if db else None
    event = db.get(models.Event, e.event_id) if db else None
    adjustment = None
    if db is not None:
        adj = db.scalars(
            select(models.Adjustment).where(models.Adjustment.new_ledger_id == e.id)
        ).first()
        if adj:
            adjustment = {"id": adj.id, "code": adj.code, "status": adj.status, "delta_amount": float(adj.delta_amount)}
    return {
        "id": e.id,
        "customer_id": e.customer_id,
        "customer_code": customer.code if customer else None,
        "customer_name": customer.name if customer else None,
        "event_id": e.event_id,
        "event_code": event.code if event else None,
        "event_date": event.event_date.isoformat() if event else None,
        "run_id": e.run_id,
        "period_month": e.period_month,
        "version": e.version,
        "amount": float(e.amount),
        "response_kwh": float(e.response_kwh),
        "baseline_kwh": float(e.baseline_kwh) if e.baseline_kwh is not None else None,
        "actual_kwh": float(e.actual_kwh) if e.actual_kwh is not None else None,
        "capacity_kwh": float(e.capacity_kwh) if e.capacity_kwh is not None else None,
        "status": e.status,
        "paid": e.paid,
        "paid_amount": float(e.paid_amount) if e.paid_amount is not None else None,
        "supersedes_id": e.supersedes_id,
        "superseded_by_id": e.superseded_by_id,
        "published_at": serialize_dt(e.created_at),
        "published_by": e.published_by,
        "adjustment": adjustment,
    }


def adjustment_row(a: models.Adjustment, db: Session) -> dict:
    customer = db.get(models.Customer, a.customer_id)
    event = db.get(models.Event, a.event_id)
    run = db.get(models.Run, a.run_id)
    original = db.get(models.LedgerEntry, a.original_ledger_id)
    new = db.get(models.LedgerEntry, a.new_ledger_id)
    return {
        "id": a.id,
        "code": a.code,
        "customer_id": a.customer_id,
        "customer_code": customer.code if customer else None,
        "customer_name": customer.name if customer else None,
        "event_id": a.event_id,
        "event_code": event.code if event else None,
        "event_date": event.event_date.isoformat() if event else None,
        "run_id": a.run_id,
        "run_code": run.run_code if run else None,
        "original_ledger_id": a.original_ledger_id,
        "new_ledger_id": a.new_ledger_id,
        "original_amount": float(original.amount) if original else None,
        "new_amount": float(new.amount) if new else None,
        "delta_amount": float(a.delta_amount),
        "delta_kwh": float(a.delta_kwh),
        "reason": a.reason,
        "status": a.status,
        "created_at": serialize_dt(a.created_at),
        "approved_at": serialize_dt(a.approved_at),
        "approved_by": a.approved_by,
        "adjustment_voucher_id": a.adjustment_voucher_id,
    }


def compare_runs(db: Session, left_run_id: int, right_run_id: int) -> dict:
    left = db.get(models.Run, left_run_id)
    right = db.get(models.Run, right_run_id)
    if left is None or right is None:
        raise ValueError("运行不存在")

    def collect(run: models.Run) -> dict[tuple[int, int], dict]:
        out = {}
        for w in db.scalars(select(models.RunWindow).where(models.RunWindow.run_id == run.id)):
            node = db.scalars(
                select(models.Node).where(
                    models.Node.run_id == run.id,
                    models.Node.customer_id == w.customer_id,
                    models.Node.event_id == w.event_id,
                    models.Node.node_type == "settlement",
                )
            ).first()
            review = db.scalars(
                select(models.Node).where(
                    models.Node.run_id == run.id,
                    models.Node.customer_id == w.customer_id,
                    models.Node.event_id == w.event_id,
                    models.Node.status == "needs_review",
                )
            ).first()
            out[(w.customer_id, w.event_id)] = {
                "status": w.status,
                "reused": w.reused,
                "amount": float(node.output.get("amount")) if node and node.output else None,
                "response_kwh": float(node.output.get("response_kwh")) if node and node.output else None,
                "reason": review.reason if review else None,
            }
        return out

    left_map, right_map = collect(left), collect(right)
    keys = sorted(set(left_map) | set(right_map))
    rows = []
    for cid, eid in keys:
        customer = db.get(models.Customer, cid)
        event = db.get(models.Event, eid)
        l = left_map.get((cid, eid))
        r = right_map.get((cid, eid))
        delta = None
        if l and r and l["amount"] is not None and r["amount"] is not None:
            delta = round(r["amount"] - l["amount"], 2)
        rows.append(
            {
                "customer_id": cid,
                "customer_code": customer.code if customer else None,
                "event_id": eid,
                "event_code": event.code if event else None,
                "event_date": event.event_date.isoformat() if event else None,
                "left": l,
                "right": r,
                "delta_amount": delta,
                "changed": (l is None) or (r is None) or l["amount"] != r["amount"] or l["status"] != r["status"],
            }
        )
    return {
        "left": run_brief(left, db),
        "right": run_brief(right, db),
        "changed_count": sum(1 for r in rows if r["changed"]),
        "total_delta": round(sum(r["delta_amount"] or 0 for r in rows), 2),
        "rows": rows,
    }


def uncertainty_brief(r: models.UncertaintyReport, db: Session | None = None) -> dict:
    customer = db.get(models.Customer, r.customer_id) if db else None
    event = db.get(models.Event, r.event_id) if db else None
    run = db.get(models.Run, r.run_id) if db else None
    report = r.report or {}
    interval = report.get("interval") or {}
    samples = report.get("samples") or {}
    weekday_cov = samples.get("weekday_coverage") or {}
    weather_cov = samples.get("weather_coverage") or {}
    return {
        "id": r.id,
        "run_id": r.run_id,
        "run_code": run.run_code if run else None,
        "run_version": run.version if run else None,
        "run_status": run.status if run else None,
        "customer_id": r.customer_id,
        "customer_code": customer.code if customer else None,
        "customer_name": customer.name if customer else None,
        "event_id": r.event_id,
        "event_code": event.code if event else None,
        "event_date": event.event_date.isoformat() if event else None,
        "period_month": event.period_month if event else None,
        "status": r.status,
        "primary_method": r.primary_method,
        "level": float(r.level),
        "baseline_kwh": float(r.baseline_kwh) if r.baseline_kwh is not None else None,
        "lower_kwh": float(r.lower_kwh) if r.lower_kwh is not None else None,
        "upper_kwh": float(r.upper_kwh) if r.upper_kwh is not None else None,
        "half_width_kwh": float(r.half_width_kwh) if r.half_width_kwh is not None else None,
        "relative_width": interval.get("relative_width"),
        "qualified_days": r.qualified_days,
        "pseudo_days": samples.get("pseudo_days"),
        "weekday_covered": weekday_cov.get("event_weekday_in_samples"),
        "weather_status": weather_cov.get("status"),
        "degraded_reasons": report.get("degraded_reasons", []),
        "unavailable_reasons": report.get("unavailable_reasons", []),
        "reused_from_report_id": r.reused_from_report_id,
        "created_at": serialize_dt(r.created_at),
    }
