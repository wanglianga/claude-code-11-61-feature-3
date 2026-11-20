"""Resolve the input snapshot for a run: batches, rules and contracts.

Every resolver honours the run's frozen ``cutoff_at`` so that a late batch or
revision arriving while a run is in flight cannot change that run's inputs.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..utils import aware_utc


def applicable_rule(db: Session, event_day: date, cutoff: datetime) -> models.BaselineRuleVersion | None:
    rows = db.scalars(
        select(models.BaselineRuleVersion)
        .where(
            models.BaselineRuleVersion.created_at <= cutoff,
            models.BaselineRuleVersion.effective_from <= event_day,
        )
        .order_by(models.BaselineRuleVersion.version.desc(), models.BaselineRuleVersion.id.desc())
    ).all()
    for row in rows:
        if row.effective_to is None or row.effective_to >= event_day:
            return row
    return None


def applicable_contract(
    db: Session, customer_id: int, event_day: date, cutoff: datetime
) -> models.ContractVersion | None:
    return db.scalars(
        select(models.ContractVersion)
        .where(
            models.ContractVersion.customer_id == customer_id,
            models.ContractVersion.created_at <= cutoff,
            models.ContractVersion.effective_from <= event_day,
        )
        .order_by(models.ContractVersion.version.desc())
        .limit(1)
    ).first()


def select_batches(
    db: Session,
    meter_ids: Iterable[int],
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> dict[int, models.MeterBatch]:
    """Latest accepted batch per meter whose coverage fully spans [start, end]."""
    meters = list(meter_ids)
    if not meters:
        return {}
    rows = db.scalars(
        select(models.MeterBatch)
        .where(
            models.MeterBatch.meter_point_id.in_(meters),
            models.MeterBatch.status == "accepted",
            models.MeterBatch.submitted_at <= cutoff,
            models.MeterBatch.coverage_start <= start,
            models.MeterBatch.coverage_end >= end,
        )
        .order_by(
            models.MeterBatch.meter_point_id,
            models.MeterBatch.version.desc(),
            models.MeterBatch.submitted_at.desc(),
        )
    ).all()
    chosen: dict[int, models.MeterBatch] = {}
    for row in rows:
        chosen.setdefault(row.meter_point_id, row)
    return chosen


def window_input_refs(
    db: Session,
    customer: models.Customer,
    event: models.Event,
    rule: models.BaselineRuleVersion | None,
    contract: models.ContractVersion | None,
    batches: dict[int, models.MeterBatch],
) -> dict:
    return {
        "batches": [
            {
                "batch_id": b.id,
                "meter_point_id": b.meter_point_id,
                "version": b.version,
                "checksum": b.checksum,
            }
            for b in sorted(batches.values(), key=lambda x: x.meter_point_id)
        ],
        "rule_id": rule.id if rule else None,
        "rule_version": rule.version if rule else None,
        "contract_id": contract.id if contract else None,
        "contract_version": contract.version if contract else None,
    }


def interval_rows(db: Session, batch_ids: list[int], start: datetime, end: datetime) -> dict[int, list]:
    if not batch_ids:
        return {}
    rows = db.scalars(
        select(models.MeterInterval)
        .where(
            models.MeterInterval.batch_id.in_(batch_ids),
            models.MeterInterval.ts >= start,
            models.MeterInterval.ts < end,
        )
        .order_by(models.MeterInterval.batch_id, models.MeterInterval.ts)
    ).all()
    grouped: dict[int, list] = defaultdict(list)
    for row in rows:
        grouped[row.batch_id].append(row)
    return grouped


def batch_map_for_interval_window(
    batch: models.MeterBatch, start: datetime, end: datetime
) -> tuple[datetime, datetime]:
    return (max(aware_utc(batch.coverage_start), start), min(aware_utc(batch.coverage_end), end))
