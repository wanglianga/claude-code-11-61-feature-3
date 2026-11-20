"""Source registry API: login, batches, rules, contracts and lineage tracing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, serializers
from ..db import get_db
from ..deps import audit, current_user, require_planner
from ..schemas import (
    BatchIn,
    ContractIn,
    HolidayIn,
    LoginRequest,
    OutageIn,
    RuleIn,
    RuleValidateIn,
    TokenResponse,
    WeatherDailyIn,
)
from ..security import create_token, verify_password
from ..services import registry, snapshot
from ..services.orchestrator import find_affected_windows
from ..utils import event_intervals, serialize_dt

router = APIRouter()


@router.post("/auth/login", response_model=TokenResponse, tags=["auth"])
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalars(select(models.User).where(models.User.username == body.username)).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_token({"sub": user.id, "role": user.role, "name": user.display_name})
    return {
        "access_token": token,
        "user": {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role},
    }


@router.get("/auth/me", tags=["auth"])
def me(user: models.User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role}


@router.get("/customers", tags=["registry"])
def list_customers(db: Session = Depends(get_db), _user=Depends(current_user)):
    rows = db.scalars(select(models.Customer).order_by(models.Customer.id)).all()
    out = []
    for c in rows:
        contract = db.scalars(
            select(models.ContractVersion)
            .where(models.ContractVersion.customer_id == c.id)
            .order_by(models.ContractVersion.version.desc())
            .limit(1)
        ).first()
        out.append(
            {
                **serializers.customer_brief(c),
                "contact": c.contact,
                "meters": db.scalar(
                    select(func.count()).select_from(models.MeterPoint).where(models.MeterPoint.customer_id == c.id)
                ),
                "latest_contract": (
                    {
                        "id": contract.id,
                        "version": contract.version,
                        "capacity_kw": float(contract.capacity_kw),
                        "effective_from": contract.effective_from.isoformat(),
                    }
                    if contract
                    else None
                ),
            }
        )
    return out


@router.get("/meters", tags=["registry"])
def list_meters(customer_id: int | None = None, db: Session = Depends(get_db), _user=Depends(current_user)):
    stmt = (
        select(models.MeterPoint, models.Customer)
        .join(models.Customer, models.Customer.id == models.MeterPoint.customer_id)
        .order_by(models.MeterPoint.meter_no)
    )
    if customer_id:
        stmt = stmt.where(models.MeterPoint.customer_id == customer_id)
    return [
        {
            "id": m.id,
            "meter_no": m.meter_no,
            "name": m.name,
            "customer_id": c.id,
            "customer_code": c.code,
            "customer_name": c.name,
        }
        for m, c in db.execute(stmt).all()
    ]


@router.get("/events", tags=["registry"])
def list_events(period_month: str | None = None, db: Session = Depends(get_db), _user=Depends(current_user)):
    stmt = select(models.Event).order_by(models.Event.event_date, models.Event.start_time)
    if period_month:
        stmt = stmt.where(models.Event.period_month == period_month)
    events = db.scalars(stmt).all()
    return [
        {
            "id": e.id,
            "code": e.code,
            "name": e.name,
            "event_date": e.event_date.isoformat(),
            "start_time": e.start_time.strftime("%H:%M"),
            "end_time": e.end_time.strftime("%H:%M"),
            "price_per_kwh": float(e.price_per_kwh),
            "period_month": e.period_month,
        }
        for e in events
    ]


@router.get("/periods", tags=["registry"])
def list_periods(db: Session = Depends(get_db), _user=Depends(current_user)):
    rows = db.execute(
        select(models.Event.period_month, func.count()).group_by(models.Event.period_month)
    ).all()
    out = []
    for month, count in rows:
        last_run = db.scalars(
            select(models.Run).where(models.Run.period_month == month).order_by(models.Run.version.desc()).limit(1)
        ).first()
        published = db.scalar(
            select(func.count()).select_from(models.Run).where(
                models.Run.period_month == month, models.Run.published_at.is_not(None)
            )
        )
        out.append(
            {
                "period_month": month,
                "event_count": int(count),
                "run_count": int(
                    db.scalar(select(func.count()).select_from(models.Run).where(models.Run.period_month == month)) or 0
                ),
                "published_run_count": int(published or 0),
                "latest_run": serializers.run_brief(last_run, db) if last_run else None,
            }
        )
    return sorted(out, key=lambda r: r["period_month"], reverse=True)


@router.get("/overview", tags=["dashboard"])
def overview(period_month: str | None = None, db: Session = Depends(get_db), _user=Depends(current_user)):
    run_stmt = select(models.Run).order_by(models.Run.created_at.desc())
    ledger_stmt = select(models.LedgerEntry)
    adj_stmt = select(models.Adjustment)
    if period_month:
        run_stmt = run_stmt.where(models.Run.period_month == period_month)
        ledger_stmt = ledger_stmt.where(models.LedgerEntry.period_month == period_month)
        adj_stmt = adj_stmt.join(models.Run, models.Run.id == models.Adjustment.run_id).where(
            models.Run.period_month == period_month
        )
    runs = db.scalars(run_stmt.limit(50)).all()
    ledger = db.scalars(ledger_stmt).all()
    adjustments = db.scalars(adj_stmt).all()
    published_amount = sum(float(e.amount) for e in ledger if e.status == "published")
    pending_delta = sum(float(a.delta_amount) for a in adjustments if a.status == "pending")
    return {
        "period_month": period_month,
        "run_total": db.scalar(select(func.count()).select_from(models.Run)) or 0,
        "run_needs_review": db.scalar(
            select(func.count()).select_from(models.Run).where(models.Run.status == "needs_review")
        ) or 0,
        "run_failed": db.scalar(
            select(func.count()).select_from(models.Run).where(models.Run.status == "failed")
        ) or 0,
        "ledger_published_amount": round(published_amount, 2),
        "ledger_published_count": sum(1 for e in ledger if e.status == "published"),
        "adjustment_pending_count": sum(1 for a in adjustments if a.status == "pending"),
        "adjustment_pending_delta": round(pending_delta, 2),
        "adjustment_approved_delta": round(
            sum(float(a.delta_amount) for a in adjustments if a.status == "approved"), 2
        ),
        "recent_runs": [serializers.run_brief(r, db) for r in runs[:8]],
        "ledger_by_period": _ledger_by_period(db),
        "strategy_stats": _strategy_stats(db),
    }


def _ledger_by_period(db: Session) -> list[dict]:
    rows = db.execute(
        select(
            models.LedgerEntry.period_month,
            func.count(),
            func.sum(models.LedgerEntry.amount),
        )
        .where(models.LedgerEntry.status == "published")
        .group_by(models.LedgerEntry.period_month)
        .order_by(models.LedgerEntry.period_month)
    ).all()
    return [
        {"period_month": p, "entry_count": int(c), "amount": round(float(s or 0), 2)} for p, c, s in rows
    ]


def _strategy_stats(db: Session) -> list[dict]:
    rows = db.execute(
        select(
            models.Run.strategy,
            func.count(),
            func.avg(models.Run.duration_ms),
            func.sum(models.Run.computed_windows),
            func.sum(models.Run.reused_windows),
        ).group_by(models.Run.strategy)
    ).all()
    return [
        {
            "strategy": s,
            "runs": int(c),
            "avg_duration_ms": round(float(avg or 0), 1),
            "computed_windows": int(computed or 0),
            "reused_windows": int(reused or 0),
        }
        for s, c, avg, computed, reused in rows
    ]


# ---------------------------- batches ----------------------------
@router.post("/batches", tags=["registry"], status_code=201)
def create_batch(body: BatchIn, db: Session = Depends(get_db), user=Depends(require_planner)):
    try:
        batch = registry.ingest_batch(db, body.model_dump(), user.username)
    except registry.RegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    audit(db, user.username, "batch.ingest", "meter_batch", batch.id, {"batch_key": batch.batch_key})
    db.commit()
    return _batch_detail(db, batch.id)


@router.get("/batches/{batch_id}", tags=["registry"])
def get_batch(batch_id: int, db: Session = Depends(get_db), _user=Depends(current_user)):
    return _batch_detail(db, batch_id)


@router.post("/batches/preview-impact", tags=["registry"])
def preview_batch_impact(body: BatchIn, db: Session = Depends(get_db), _user=Depends(current_user)):
    """Dry-run lineage lookup: which windows would a batch for this meter touch?"""
    meter = db.scalars(select(models.MeterPoint).where(models.MeterPoint.meter_no == body.meter_no)).first()
    if meter is None:
        raise HTTPException(status_code=404, detail="计量点不存在")
    windows: dict[int, set[int]] = {}
    rows = db.scalars(
        select(models.Node)
        .join(models.NodeSource, models.NodeSource.node_id == models.Node.id)
        .where(
            models.NodeSource.source_type == "batch",
            models.Node.customer_id == meter.customer_id,
        )
    ).all()
    for node in rows:
        windows.setdefault(node.event_id, set()).add(node.customer_id)
    candidates = {}
    for eid, cids in windows.items():
        event = db.get(models.Event, eid)
        candidates[str(eid)] = {
            "event_id": eid,
            "event_code": event.code if event else None,
            "event_date": event.event_date.isoformat() if event else None,
            "customer_ids": sorted(cids),
        }
    return {
        "meter_no": meter.meter_no,
        "customer_id": meter.customer_id,
        "candidate_windows": sorted(candidates.values(), key=lambda r: r["event_date"] or ""),
        "note": "候选窗口来自已登记血缘；正式定向重算会按批次实际覆盖区间过滤",
    }


@router.get("/batches", tags=["registry"])
def list_batches(
    meter_no: str | None = None,
    customer_id: int | None = None,
    limit: int = Query(default=50, le=500),
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    stmt = (
        select(models.MeterBatch, models.MeterPoint, models.Customer)
        .join(models.MeterPoint, models.MeterPoint.id == models.MeterBatch.meter_point_id)
        .join(models.Customer, models.Customer.id == models.MeterPoint.customer_id)
        .order_by(models.MeterBatch.submitted_at.desc())
        .limit(limit)
    )
    if meter_no:
        stmt = stmt.where(models.MeterPoint.meter_no == meter_no)
    if customer_id:
        stmt = stmt.where(models.Customer.id == customer_id)
    rows = db.execute(stmt).all()
    return [
        {
            "id": b.id,
            "batch_key": b.batch_key,
            "meter_no": m.meter_no,
            "customer_code": c.code,
            "customer_name": c.name,
            "version": b.version,
            "coverage_start": serialize_dt(b.coverage_start),
            "coverage_end": serialize_dt(b.coverage_end),
            "submitted_at": serialize_dt(b.submitted_at),
            "checksum": b.checksum[:12],
            "supersedes_batch_id": b.supersedes_batch_id,
            "status": b.status,
            "interval_count": b.interval_count,
            "total_kwh": float(b.total_kwh),
            "source_note": b.source_note,
            "created_by": b.created_by,
            "in_mpan": None,
        }
        for b, m, c in rows
    ]


def _batch_detail(db: Session, batch_id: int) -> dict:
    b = db.get(models.MeterBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    meter = db.get(models.MeterPoint, b.meter_point_id)
    used_by = db.execute(
        select(models.Node, models.Run)
        .join(models.NodeSource, models.NodeSource.node_id == models.Node.id)
        .join(models.Run, models.Run.id == models.Node.run_id)
        .where(models.NodeSource.source_type == "batch", models.NodeSource.ref_id == batch_id)
    ).all()
    return {
        "id": b.id,
        "batch_key": b.batch_key,
        "meter_no": meter.meter_no if meter else None,
        "meter_point_id": b.meter_point_id,
        "customer_id": meter.customer_id if meter else None,
        "version": b.version,
        "coverage_start": serialize_dt(b.coverage_start),
        "coverage_end": serialize_dt(b.coverage_end),
        "submitted_at": serialize_dt(b.submitted_at),
        "checksum": b.checksum,
        "supersedes_batch_id": b.supersedes_batch_id,
        "status": b.status,
        "interval_count": b.interval_count,
        "total_kwh": float(b.total_kwh),
        "source_note": b.source_note,
        "created_by": b.created_by,
        "used_by_runs": [
            {
                "run_id": r.id,
                "run_code": r.run_code,
                "run_status": r.status,
                "event_id": n.event_id,
                "node_type": n.node_type,
            }
            for n, r in used_by
        ],
    }


# ---------------------------- rules ----------------------------
@router.get("/rules", tags=["registry"])
def list_rules(db: Session = Depends(get_db), _user=Depends(current_user)):
    rows = db.scalars(
        select(models.BaselineRuleVersion).order_by(
            models.BaselineRuleVersion.rule_code, models.BaselineRuleVersion.version.desc()
        )
    ).all()
    return [
        {
            "id": r.id,
            "rule_code": r.rule_code,
            "version": r.version,
            "name": r.name,
            "method": r.method,
            "lookback_days": r.lookback_days,
            "min_sample_days": r.min_sample_days,
            "adjustment_factor": float(r.adjustment_factor),
            "effective_from": r.effective_from.isoformat(),
            "effective_to": r.effective_to.isoformat() if r.effective_to else None,
            "reason": r.reason,
            "config": r.config,
            "created_by": r.created_by,
            "created_at": serialize_dt(r.created_at),
        }
        for r in rows
    ]


@router.post("/rules", tags=["registry"], status_code=201)
def create_rule(body: RuleIn, db: Session = Depends(get_db), user=Depends(require_planner)):
    rule = registry.add_rule_version(db, body.model_dump(), user.username)
    audit(db, user.username, "rule.create", "baseline_rule", rule.id, {"version": rule.version})
    db.commit()
    return {"id": rule.id, "rule_code": rule.rule_code, "version": rule.version}


@router.post("/rules/validate", tags=["registry"])
def validate_rule(body: RuleValidateIn, db: Session = Depends(get_db), _user=Depends(current_user)):
    """Check whether a rule revision would leave any customer short of samples."""
    event = db.get(models.Event, body.event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="活动不存在")
    results = []
    meter_map: dict[int, list[int]] = {}
    for meter in db.scalars(select(models.MeterPoint)):
        meter_map.setdefault(meter.customer_id, []).append(meter.id)
    for customer in db.scalars(select(models.Customer).order_by(models.Customer.id)):
        available = 0
        for offset in range(1, body.lookback_days + 1):
            day = event.event_date - timedelta(days=offset)
            day_intervals = event_intervals(day, event.start_time, event.end_time)
            batches = snapshot.select_batches(
                db,
                meter_map.get(customer.id, []),
                day_intervals[0],
                day_intervals[-1] + timedelta(minutes=15),
                datetime.now(timezone.utc),
            )
            if len(batches) == len(meter_map.get(customer.id, [])):
                available += 1
        results.append(
            {
                "customer_id": customer.id,
                "customer_code": customer.code,
                "available_days": available,
                "meets_min_samples": available >= body.min_sample_days,
            }
        )
    return {
        "event_id": event.id,
        "lookback_days": body.lookback_days,
        "min_sample_days": body.min_sample_days,
        "at_risk": [r for r in results if not r["meets_min_samples"]],
        "customers": results,
    }


# ---------------------------- contracts ----------------------------
@router.get("/contracts", tags=["registry"])
def list_contracts(customer_id: int | None = None, db: Session = Depends(get_db), _user=Depends(current_user)):
    stmt = select(models.ContractVersion, models.Customer).join(
        models.Customer, models.Customer.id == models.ContractVersion.customer_id
    ).order_by(models.ContractVersion.customer_id, models.ContractVersion.version.desc())
    if customer_id:
        stmt = stmt.where(models.ContractVersion.customer_id == customer_id)
    return [
        {
            "id": c.id,
            "customer_id": c.customer_id,
            "customer_code": cu.code,
            "customer_name": cu.name,
            "version": c.version,
            "capacity_kw": float(c.capacity_kw),
            "price_ceiling": float(c.price_ceiling) if c.price_ceiling is not None else None,
            "effective_from": c.effective_from.isoformat(),
            "reason": c.reason,
            "created_by": c.created_by,
            "created_at": serialize_dt(c.created_at),
        }
        for c, cu in db.execute(stmt).all()
    ]


@router.post("/contracts", tags=["registry"], status_code=201)
def create_contract(body: ContractIn, db: Session = Depends(get_db), user=Depends(require_planner)):
    try:
        contract = registry.add_contract_version(db, body.model_dump(), user.username)
    except registry.RegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    audit(db, user.username, "contract.create", "contract", contract.id, {"version": contract.version})
    db.commit()
    return {"id": contract.id, "customer_id": contract.customer_id, "version": contract.version}


# ---------------------------- weather / holidays / outages ----------------------------
@router.get("/weather/daily", tags=["registry"])
def list_weather(
    region: str | None = None,
    day_from: str | None = None,
    day_to: str | None = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    stmt = select(models.WeatherDaily).order_by(models.WeatherDaily.region, models.WeatherDaily.day.desc())
    if region:
        stmt = stmt.where(models.WeatherDaily.region == region)
    if day_from:
        stmt = stmt.where(models.WeatherDaily.day >= day_from)
    if day_to:
        stmt = stmt.where(models.WeatherDaily.day <= day_to)
    rows = db.scalars(stmt.limit(limit)).all()
    return [
        {
            "id": w.id,
            "region": w.region,
            "day": w.day.isoformat(),
            "version": w.version,
            "avg_temp_c": float(w.avg_temp_c),
            "max_temp_c": float(w.max_temp_c) if w.max_temp_c is not None else None,
            "source_note": w.source_note,
            "created_by": w.created_by,
            "created_at": serialize_dt(w.created_at),
        }
        for w in rows
    ]


@router.post("/weather/daily", tags=["registry"], status_code=201)
def create_weather(body: WeatherDailyIn, db: Session = Depends(get_db), user=Depends(require_planner)):
    try:
        row = registry.add_weather_daily(db, body.model_dump(), user.username)
    except registry.RegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    audit(db, user.username, "weather.create", "weather_daily", row.id, {"region": row.region, "day": row.day.isoformat()})
    db.commit()
    return {"id": row.id, "region": row.region, "day": row.day.isoformat(), "version": row.version}


@router.get("/holidays", tags=["registry"])
def list_holidays(db: Session = Depends(get_db), _user=Depends(current_user)):
    rows = db.scalars(select(models.Holiday).order_by(models.Holiday.day)).all()
    return [
        {
            "id": h.id,
            "day": h.day.isoformat(),
            "name": h.name,
            "created_by": h.created_by,
            "created_at": serialize_dt(h.created_at),
        }
        for h in rows
    ]


@router.post("/holidays", tags=["registry"], status_code=201)
def create_holiday(body: HolidayIn, db: Session = Depends(get_db), user=Depends(require_planner)):
    try:
        row = registry.add_holiday(db, body.model_dump(), user.username)
    except registry.RegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    audit(db, user.username, "holiday.create", "holiday", row.id, {"day": row.day.isoformat()})
    db.commit()
    return {"id": row.id, "day": row.day.isoformat(), "name": row.name}


@router.get("/outages", tags=["registry"])
def list_outages(customer_id: int | None = None, db: Session = Depends(get_db), _user=Depends(current_user)):
    stmt = (
        select(models.OutageDay, models.Customer)
        .join(models.Customer, models.Customer.id == models.OutageDay.customer_id)
        .order_by(models.OutageDay.day.desc())
    )
    if customer_id:
        stmt = stmt.where(models.OutageDay.customer_id == customer_id)
    return [
        {
            "id": o.id,
            "customer_id": o.customer_id,
            "customer_code": c.code,
            "customer_name": c.name,
            "day": o.day.isoformat(),
            "kind": o.kind,
            "note": o.note,
            "created_by": o.created_by,
            "created_at": serialize_dt(o.created_at),
        }
        for o, c in db.execute(stmt).all()
    ]


@router.post("/outages", tags=["registry"], status_code=201)
def create_outage(body: OutageIn, db: Session = Depends(get_db), user=Depends(require_planner)):
    try:
        row = registry.add_outage_day(db, body.model_dump(), user.username)
    except registry.RegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    audit(db, user.username, "outage.create", "outage_day", row.id, {"customer_id": row.customer_id, "day": row.day.isoformat()})
    db.commit()
    return {"id": row.id, "customer_id": row.customer_id, "day": row.day.isoformat(), "kind": row.kind}


# ---------------------------- lineage ----------------------------
@router.get("/impact", tags=["lineage"])
def impact(
    batch_id: list[int] | None = Query(default=None),
    contract_id: list[int] | None = Query(default=None),
    rule_id: list[int] | None = Query(default=None),
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    if not any([batch_id, contract_id, rule_id]):
        raise HTTPException(status_code=400, detail="至少提供一个 batch_id / contract_id / rule_id")
    affected, evidence = find_affected_windows(
        db, batch_ids=batch_id, contract_ids=contract_id, rule_ids=rule_id
    )
    windows = []
    for eid, cids in affected.items():
        event = db.get(models.Event, eid)
        for cid in sorted(cids):
            customer = db.get(models.Customer, cid)
            latest_run = db.scalars(
                select(models.Run)
                .join(models.RunWindow, models.RunWindow.run_id == models.Run.id)
                .where(
                    models.RunWindow.customer_id == cid,
                    models.RunWindow.event_id == eid,
                )
                .order_by(models.Run.version.desc())
                .limit(1)
            ).first()
            windows.append(
                {
                    "event_id": eid,
                    "event_code": event.code if event else None,
                    "event_date": event.event_date.isoformat() if event else None,
                    "customer_id": cid,
                    "customer_code": customer.code if customer else None,
                    "latest_run_id": latest_run.id if latest_run else None,
                    "latest_run_code": latest_run.run_code if latest_run else None,
                    "latest_run_status": latest_run.status if latest_run else None,
                    "published": bool(latest_run and latest_run.published_at),
                }
            )
    return {
        "affected_window_count": len(windows),
        "windows": sorted(windows, key=lambda w: (w["event_date"] or "", w["customer_code"] or "")),
        "evidence": evidence,
    }


@router.get("/lineage/{run_id}/{customer_id}/{event_id}", tags=["lineage"])
def lineage(run_id: int, customer_id: int, event_id: int, db: Session = Depends(get_db), _user=Depends(current_user)):
    run = db.get(models.Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    nodes = db.scalars(
        select(models.Node)
        .where(
            models.Node.run_id == run_id,
            models.Node.customer_id == customer_id,
            models.Node.event_id == event_id,
        )
        .order_by(models.Node.id)
    ).all()
    if not nodes:
        raise HTTPException(status_code=404, detail="该运行下没有此客户窗口")
    node_ids = [n.id for n in nodes]
    sources = db.scalars(select(models.NodeSource).where(models.NodeSource.node_id.in_(node_ids))).all()
    edges = db.scalars(
        select(models.LineageEdge).where(
            models.LineageEdge.run_id == run_id,
            models.LineageEdge.to_node_id.in_(node_ids),
        )
    ).all()
    enriched_sources = []
    for s in sources:
        meta: dict[str, Any] = dict(s.meta or {})
        if s.source_type == "batch":
            batch = db.get(models.MeterBatch, s.ref_id)
            if batch:
                meter = db.get(models.MeterPoint, batch.meter_point_id)
                meta.update(
                    {
                        "meter_no": meter.meter_no if meter else None,
                        "version": batch.version,
                        "coverage_start": serialize_dt(batch.coverage_start),
                        "coverage_end": serialize_dt(batch.coverage_end),
                        "checksum": batch.checksum[:12],
                    }
                )
        elif s.source_type == "rule":
            rule = db.get(models.BaselineRuleVersion, s.ref_id)
            if rule:
                meta.update(
                    {
                        "rule_code": rule.rule_code,
                        "version": rule.version,
                        "lookback_days": rule.lookback_days,
                        "min_sample_days": rule.min_sample_days,
                        "adjustment_factor": float(rule.adjustment_factor),
                        "effective_from": rule.effective_from.isoformat(),
                    }
                )
        elif s.source_type == "contract":
            contract = db.get(models.ContractVersion, s.ref_id)
            if contract:
                meta.update(
                    {
                        "version": contract.version,
                        "capacity_kw": float(contract.capacity_kw),
                        "effective_from": contract.effective_from.isoformat(),
                    }
                )
        enriched_sources.append(
            {"id": s.id, "node_id": s.node_id, "source_type": s.source_type, "ref_id": s.ref_id, "meta": meta}
        )
    customer = db.get(models.Customer, customer_id)
    event = db.get(models.Event, event_id)
    return {
        "run": serializers.run_brief(run, db),
        "customer": serializers.customer_brief(customer) if customer else None,
        "event": {
            "id": event.id,
            "code": event.code,
            "name": event.name,
            "event_date": event.event_date.isoformat(),
            "start_time": event.start_time.strftime("%H:%M"),
            "end_time": event.end_time.strftime("%H:%M"),
            "price_per_kwh": float(event.price_per_kwh),
        }
        if event
        else None,
        "nodes": [
            {
                "id": n.id,
                "node_type": n.node_type,
                "status": n.status,
                "reason": n.reason,
                "output": n.output,
                "reused_from_node_id": n.reused_from_node_id,
                "sources": [s for s in enriched_sources if s["node_id"] == n.id],
            }
            for n in nodes
        ],
        "edges": [{"from_node_id": e.from_node_id, "to_node_id": e.to_node_id, "relation": e.relation} for e in edges],
    }


@router.get("/windows", tags=["lineage"])
def window_overview(
    run_id: int,
    status: str | None = None,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    stmt = (
        select(models.RunWindow, models.Customer)
        .join(models.Customer, models.Customer.id == models.RunWindow.customer_id)
        .where(models.RunWindow.run_id == run_id)
        .order_by(models.RunWindow.customer_id)
    )
    if status:
        stmt = stmt.where(models.RunWindow.status == status)
    rows = db.execute(stmt).all()
    out = []
    for window, customer in rows:
        event = db.get(models.Event, window.event_id)
        node = db.scalars(
            select(models.Node).where(
                models.Node.run_id == run_id,
                models.Node.customer_id == customer.id,
                models.Node.event_id == window.event_id,
                models.Node.node_type == "settlement",
            )
        ).first()
        blocked = db.scalars(
            select(models.Node).where(
                models.Node.run_id == run_id,
                models.Node.customer_id == customer.id,
                models.Node.event_id == window.event_id,
                models.Node.status.in_(["needs_review", "blocked"]),
            )
        ).all()
        out.append(
            {
                "customer_id": customer.id,
                "customer_code": customer.code,
                "customer_name": customer.name,
                "event_id": window.event_id,
                "event_code": event.code if event else None,
                "event_date": event.event_date.isoformat() if event else None,
                "status": window.status,
                "reused": window.reused,
                "amount": float(node.output.get("amount")) if node and node.output else None,
                "response_kwh": float(node.output.get("response_kwh")) if node and node.output else None,
                "reason": next((b.reason for b in blocked if b.reason), None),
                "review_node": next((b.node_type for b in blocked if b.status == "needs_review"), None),
            }
        )
    return out
