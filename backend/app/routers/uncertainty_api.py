"""Baseline-uncertainty query API: intervals, samples, residuals, diagnostics."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, serializers
from ..db import get_db
from ..deps import current_user

router = APIRouter()


@router.get("/uncertainty", tags=["uncertainty"])
def list_uncertainty(
    run_id: int | None = None,
    period_month: str | None = None,
    customer_id: int | None = None,
    event_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    stmt = select(models.UncertaintyReport).order_by(models.UncertaintyReport.id.desc()).limit(limit)
    if run_id:
        stmt = stmt.where(models.UncertaintyReport.run_id == run_id)
    if customer_id:
        stmt = stmt.where(models.UncertaintyReport.customer_id == customer_id)
    if event_id:
        stmt = stmt.where(models.UncertaintyReport.event_id == event_id)
    if status:
        stmt = stmt.where(models.UncertaintyReport.status == status)
    if period_month:
        stmt = stmt.join(models.Event, models.Event.id == models.UncertaintyReport.event_id).where(
            models.Event.period_month == period_month
        )
    return [serializers.uncertainty_brief(r, db) for r in db.scalars(stmt).all()]


@router.get("/runs/{run_id}/uncertainty", tags=["uncertainty"])
def run_uncertainty(run_id: int, db: Session = Depends(get_db), _user=Depends(current_user)):
    run = db.get(models.Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    rows = db.scalars(
        select(models.UncertaintyReport)
        .where(models.UncertaintyReport.run_id == run_id)
        .order_by(models.UncertaintyReport.event_id, models.UncertaintyReport.customer_id)
    ).all()
    return {
        "run": serializers.run_brief(run, db),
        "reports": [serializers.uncertainty_brief(r, db) for r in rows],
    }


@router.get("/uncertainty/{report_id}", tags=["uncertainty"])
def uncertainty_detail(report_id: int, db: Session = Depends(get_db), _user=Depends(current_user)):
    report = db.get(models.UncertaintyReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="不确定度报告不存在")
    payload = serializers.uncertainty_brief(report, db)
    payload["node_id"] = report.node_id
    payload["report"] = report.report or {}
    payload["input_refs"] = report.input_refs or {}
    # Link back to the exact locked inputs of the run this report belongs to.
    run = db.get(models.Run, report.run_id)
    if run is not None:
        payload["run_cutoff_at"] = run.cutoff_at.isoformat() if run.cutoff_at else None
        payload["run_rule_snapshot"] = run.rule_snapshot or {}
    return payload
