"""Domain model for the demand-response settlement re-computation platform.

Immutability rules encoded here:
* Contract versions, baseline-rule versions, meter batches and ledger entries are
  append-only.  Corrections create new versions instead of overwriting rows.
* A run freezes the batch cut-off, rule snapshot and contract snapshot it used.
* Published ledger rows are never mutated; differences flow through adjustments.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))  # planner | approver | viewer
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(64))
    contact: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    meter_points: Mapped[list["MeterPoint"]] = relationship(back_populates="customer")


class ContractVersion(Base):
    __tablename__ = "contracts"
    __table_args__ = (UniqueConstraint("customer_id", "version", name="uq_contract_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    capacity_kw: Mapped[float] = mapped_column(Numeric(12, 3))
    price_ceiling: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(255), default="初始签约")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    event_date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    price_per_kwh: Mapped[float] = mapped_column(Numeric(10, 4))
    period_month: Mapped[str] = mapped_column(String(7), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BaselineRuleVersion(Base):
    __tablename__ = "baseline_rules"
    __table_args__ = (UniqueConstraint("rule_code", "version", name="uq_rule_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_code: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(128))
    method: Mapped[str] = mapped_column(String(64), default="same-interval-mean")
    lookback_days: Mapped[int] = mapped_column(Integer, default=5)
    min_sample_days: Mapped[int] = mapped_column(Integer, default=3)
    adjustment_factor: Mapped[float] = mapped_column(Numeric(8, 4), default=1.0)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str] = mapped_column(String(255), default="初始基线规则")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MeterPoint(Base):
    __tablename__ = "meter_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    meter_no: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    customer: Mapped[Customer] = relationship(back_populates="meter_points")


class MeterBatch(Base):
    __tablename__ = "meter_batches"
    __table_args__ = (
        UniqueConstraint("batch_key", name="uq_batch_key"),
        Index("ix_batch_meter_submitted", "meter_point_id", "submitted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_key: Mapped[str] = mapped_column(String(64))
    meter_point_id: Mapped[int] = mapped_column(ForeignKey("meter_points.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    coverage_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    supersedes_batch_id: Mapped[int | None] = mapped_column(ForeignKey("meter_batches.id"), nullable=True)
    source_note: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(24), default="accepted")
    interval_count: Mapped[int] = mapped_column(Integer, default=0)
    total_kwh: Mapped[float] = mapped_column(Numeric(16, 3), default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MeterInterval(Base):
    __tablename__ = "meter_intervals"
    __table_args__ = (Index("ix_interval_batch_ts", "batch_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("meter_batches.id"), index=True)
    meter_point_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kwh: Mapped[float] = mapped_column(Numeric(12, 3))
    quality: Mapped[str] = mapped_column(String(16), default="good")


class WeatherDaily(Base):
    """Daily weather observation per region, append-only like every other source.

    A later version for the same (region, day) supersedes earlier ones; every
    consumer resolves "latest version submitted before the run cut-off".
    """

    __tablename__ = "weather_daily"
    __table_args__ = (
        UniqueConstraint("region", "day", "version", name="uq_weather_region_day_version"),
        Index("ix_weather_region_day", "region", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region: Mapped[str] = mapped_column(String(64))
    day: Mapped[date] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer, default=1)
    avg_temp_c: Mapped[float] = mapped_column(Numeric(5, 2))
    max_temp_c: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    source_note: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Holiday(Base):
    """Holiday calendar entry.  Append-only; consumers filter by created_at <= cutoff."""

    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("day", name="uq_holiday_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutageDay(Base):
    """Planned outage / curtailment (停电/限电) day for one customer.  Append-only."""

    __tablename__ = "outage_days"
    __table_args__ = (
        UniqueConstraint("customer_id", "day", "kind", name="uq_outage_customer_day_kind"),
        Index("ix_outage_customer_day", "customer_id", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))  # outage 停电 | curtailment 限电
    note: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("period_month", "version", name="uq_run_period_version"),
        Index("ix_run_event", "event_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_code: Mapped[str] = mapped_column(String(40), unique=True)
    scope_type: Mapped[str] = mapped_column(String(16))  # event | period
    period_month: Mapped[str] = mapped_column(String(7), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    strategy: Mapped[str] = mapped_column(String(24))  # initial | targeted | full_period
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    version: Mapped[int] = mapped_column(Integer)
    base_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    batch_snapshot: Mapped[list[int]] = mapped_column(JSONB, default=list)
    rule_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    contract_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    trigger_type: Mapped[str] = mapped_column(String(32), default="manual")
    trigger_batch_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_windows: Mapped[int] = mapped_column(Integer, default=0)
    computed_windows: Mapped[int] = mapped_column(Integer, default=0)
    reused_windows: Mapped[int] = mapped_column(Integer, default=0)
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    scope_event_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    event: Mapped[Event | None] = relationship()


class RunWindow(Base):
    __tablename__ = "run_windows"
    __table_args__ = (UniqueConstraint("run_id", "customer_id", "event_id", name="uq_run_window"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    status: Mapped[str] = mapped_column(String(24))
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    base_window_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RunWindowClaim(Base):
    """Only one active run may own the input snapshot of a customer/event window."""

    __tablename__ = "run_window_claims"
    __table_args__ = (
        Index(
            "uq_claim_active_window",
            "customer_id",
            "event_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_claim_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    status: Mapped[str] = mapped_column(String(16), default="active")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Node(Base):
    __tablename__ = "run_nodes"
    __table_args__ = (
        UniqueConstraint("run_id", "customer_id", "event_id", "node_type", name="uq_run_node"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    node_type: Mapped[str] = mapped_column(String(24))  # ingest | baseline | response | settlement
    status: Mapped[str] = mapped_column(String(24))  # success | needs_review | blocked | failed
    reused_from_node_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeSource(Base):
    __tablename__ = "node_sources"
    __table_args__ = (Index("ix_node_source_node", "node_id", "source_type"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("run_nodes.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(24))  # batch | rule | contract | event | node
    ref_id: Mapped[int] = mapped_column(BigInteger)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class LineageEdge(Base):
    __tablename__ = "lineage_edges"
    __table_args__ = (UniqueConstraint("run_id", "from_node_id", "to_node_id", name="uq_edge"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    from_node_id: Mapped[int] = mapped_column(ForeignKey("run_nodes.id"))
    to_node_id: Mapped[int] = mapped_column(ForeignKey("run_nodes.id"))
    relation: Mapped[str] = mapped_column(String(32))


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("customer_id", "event_id", "version", name="uq_ledger_version"),
        Index("ix_ledger_period", "period_month", "customer_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("run_nodes.id"))
    period_month: Mapped[str] = mapped_column(String(7), index=True)
    version: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Numeric(16, 2))
    response_kwh: Mapped[float] = mapped_column(Numeric(16, 3))
    baseline_kwh: Mapped[float | None] = mapped_column(Numeric(16, 3), nullable=True)
    actual_kwh: Mapped[float | None] = mapped_column(Numeric(16, 3), nullable=True)
    capacity_kwh: Mapped[float | None] = mapped_column(Numeric(16, 3), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="published")  # published | superseded
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_amount: Mapped[float | None] = mapped_column(Numeric(16, 2), nullable=True)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_entries.id"), nullable=True)
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_entries.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_by: Mapped[str] = mapped_column(String(64))


class Voucher(Base):
    """Payment voucher.  Never rewritten; adjustments get their own voucher."""

    __tablename__ = "vouchers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    voucher_no: Mapped[str] = mapped_column(String(48), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # statement | adjustment
    ledger_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_entries.id"), nullable=True, index=True)
    adjustment_id: Mapped[int | None] = mapped_column(ForeignKey("adjustments.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(16, 2))
    reference: Mapped[str] = mapped_column(String(96), unique=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Adjustment(Base):
    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(48), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    original_ledger_id: Mapped[int] = mapped_column(ForeignKey("ledger_entries.id"), index=True)
    new_ledger_id: Mapped[int] = mapped_column(ForeignKey("ledger_entries.id"))
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    delta_amount: Mapped[float] = mapped_column(Numeric(16, 2))
    delta_kwh: Mapped[float] = mapped_column(Numeric(16, 3))
    reason: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adjustment_voucher_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class UncertaintyReport(Base):
    """Baseline-uncertainty report for one customer/event window of a run.

    The report is an *analysis artefact*: it never rewrites the baseline and
    never claims actual curtailment.  It references the same locked rule and
    input versions as the run it belongs to (see ``input_refs``).
    """

    __tablename__ = "uncertainty_reports"
    __table_args__ = (
        UniqueConstraint("run_id", "customer_id", "event_id", name="uq_uncertainty_window"),
        Index("ix_uncertainty_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    node_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("run_nodes.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # ok | degraded | unavailable
    primary_method: Mapped[str] = mapped_column(String(24), default="holdout")
    level: Mapped[float] = mapped_column(Numeric(4, 2), default=0.9)
    baseline_kwh: Mapped[float | None] = mapped_column(Numeric(16, 3), nullable=True)
    lower_kwh: Mapped[float | None] = mapped_column(Numeric(16, 3), nullable=True)
    upper_kwh: Mapped[float | None] = mapped_column(Numeric(16, 3), nullable=True)
    half_width_kwh: Mapped[float | None] = mapped_column(Numeric(16, 3), nullable=True)
    qualified_days: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    input_refs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reused_from_report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
