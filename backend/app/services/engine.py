"""Calculation graph execution: ingest → baseline → response → settlement.

The engine is deliberately synchronous and transaction-scoped per window so the
concurrency guarantees are easy to reproduce with plain HTTP calls.

Failure propagation rule: a baseline that fails or is short of samples marks the
window ``needs_review``; response and settlement nodes are recorded as ``blocked``
with **no output and no ledger row**.  Nothing is ever settled at zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models
from ..utils import (
    aware_utc,
    event_duration_hours,
    event_intervals,
    money,
    number,
)
from . import eligibility, snapshot
from . import uncertainty as uncertainty_svc

PASS = "success"
REVIEW = "needs_review"
BLOCKED = "blocked"
FAILED = "failed"


@dataclass
class WindowOutcome:
    status: str
    amount: float | None
    response_kwh: float | None
    reason: str | None
    nodes: dict[str, models.Node]


def _node(
    run_id: int,
    customer_id: int,
    event_id: int,
    node_type: str,
    status: str,
    output: dict,
    reason: str | None = None,
) -> models.Node:
    now = datetime.now(timezone.utc)
    return models.Node(
        run_id=run_id,
        customer_id=customer_id,
        event_id=event_id,
        node_type=node_type,
        status=status,
        output=output,
        reason=reason,
        created_at=now,
        finished_at=now,
    )


def _source(node: models.Node, source_type: str, ref_id: int, **meta) -> models.NodeSource:
    return models.NodeSource(node_id=node.id, source_type=source_type, ref_id=ref_id, meta=meta)


def compute_window(
    db: Session,
    run: models.Run,
    customer: models.Customer,
    event: models.Event,
    rule: models.BaselineRuleVersion | None,
    contract: models.ContractVersion | None,
    meter_points: list[models.MeterPoint],
    extras: dict | None = None,
) -> WindowOutcome:
    """Compute one customer/event window and persist its nodes."""
    cutoff = aware_utc(run.cutoff_at)
    intervals = event_intervals(event.event_date, event.start_time, event.end_time)
    if not intervals:
        raise ValueError("活动窗口为空，请检查活动起止时间")
    win_start = intervals[0]
    win_end = intervals[-1] + timedelta(minutes=15)

    nodes: dict[str, models.Node] = {}

    # Shared per-run caches: reference data and per-customer meter history are
    # loaded once and reused by the baseline gate and the uncertainty analysis,
    # so both read exactly the same locked inputs at the run's cut-off.
    cache = extras if extras is not None else {}
    if ("ref",) not in cache:
        cache[("ref",)] = eligibility.RefData.load(db, cutoff)
    ref_data: eligibility.RefData = cache[("ref",)]
    hist_key = ("hist", customer.id)
    if hist_key not in cache:
        cache[hist_key] = eligibility.CustomerHistory.load(db, meter_points, cutoff)
    history: eligibility.CustomerHistory = cache[hist_key]

    # ---------- 1. 补数 (ingest) ----------
    meter_ids = [m.id for m in meter_points]
    batches = snapshot.select_batches(db, meter_ids, win_start, win_end, cutoff)
    missing = [m.meter_no for m in meter_points if m.id not in batches]
    ingest_ok = not missing
    ingest_output = {
        "meter_count": len(meter_points),
        "covered_meter_count": len(batches),
        "missing_meters": missing,
        "window_start": win_start.isoformat(),
        "window_end": win_end.isoformat(),
        "batches": [
            {"batch_id": b.id, "version": b.version, "meter_point_id": b.meter_point_id, "checksum": b.checksum}
            for b in batches.values()
        ],
    }
    ingest = _node(
        run.id,
        customer.id,
        event.id,
        "ingest",
        PASS if ingest_ok else REVIEW,
        ingest_output,
        None if ingest_ok else f"窗口 {win_start:%Y-%m-%d %H:%M} 缺少计量批次：{', '.join(missing)}",
    )
    db.add(ingest)
    db.flush()
    nodes["ingest"] = ingest
    # Freeze lineage for every batch already visible, even when the window is
    # short of data: a later correction must be able to trace back through it.
    for b in batches.values():
        db.add(_source(ingest, "batch", b.id, version=b.version, checksum=b.checksum))
    db.add(_source(ingest, "event", event.id, event_date=event.event_date.isoformat()))
    db.flush()
    if not ingest_ok:
        return _block_downstream(db, run, customer, event, nodes, ingest.reason, ingest)

    # Load actual event-window readings from the frozen batch set.
    batch_ids = list(batches.values())
    grouped_actual = snapshot.interval_rows(db, [b.id for b in batch_ids], win_start, win_end)
    actual_map: dict[datetime, float] = {}
    for batch in batches.values():
        for row in grouped_actual.get(batch.id, []):
            actual_map[aware_utc(row.ts)] = actual_map.get(aware_utc(row.ts), 0.0) + float(row.kwh)

    # ---------- 2. 基线 (baseline) ----------
    rule_meta = None
    if rule is not None:
        rule_meta = {"rule_id": rule.id, "rule_version": rule.version, "rule_code": rule.rule_code}
    baseline_ok = True
    baseline_reason: str | None = None
    baseline_total = 0.0
    sample_days = 0
    daily_baselines: list[dict] = []
    sample_eligibility: list[dict] = []
    per_interval_baseline: dict[datetime, float] = {}

    if rule is None:
        baseline_ok = False
        baseline_reason = f"活动日 {event.event_date} 无生效的基线规则版本"
    else:
        lookback = max(0, int(rule.lookback_days))
        day_step = timedelta(days=1)
        lookback_days = [event.event_date - day_step * offset for offset in range(1, lookback + 1)]
        elig_map = eligibility.eligibility_map(
            lookback_days,
            customer=customer,
            history=history,
            ref=ref_data,
            start=event.start_time,
            end=event.end_time,
        )
        samples: list[list[float]] = []
        for day in lookback_days:
            elig = elig_map[day]
            used = elig.eligible
            sample_eligibility.append(
                {
                    "day": day.isoformat(),
                    "eligible": elig.eligible,
                    "used": used,
                    "reasons": elig.reasons,
                    "reason_labels": elig.reason_labels,
                    "day_total_kwh": elig.day_total_kwh,
                    "batch_ids": list(elig.window.batch_ids) if elig.window else [],
                }
            )
            if not used:
                continue
            samples.append(elig.window.totals)
            daily_baselines.append(
                {
                    "measured_day": day.isoformat(),
                    "day_total_kwh": number(elig.window.total_kwh),
                    "batch_ids": list(elig.window.batch_ids),
                }
            )
        sample_days = len(samples)
        if sample_days < int(rule.min_sample_days):
            baseline_ok = False
            baseline_reason = (
                f"基线样本不足：合格 {sample_days} 天，规则要求 ≥{rule.min_sample_days} 天（回看 {lookback} 天）"
            )
        else:
            factor = Decimal(str(rule.adjustment_factor or 1))
            for idx, ts in enumerate(intervals):
                mean = Decimal(str(sum(day[idx] for day in samples))) / Decimal(sample_days)
                per_interval_baseline[ts] = float((mean * factor).quantize(Decimal("0.001")))
            baseline_total = sum(per_interval_baseline.values())
            # Capacity sanity: record but do not zero-fill.
            if contract is None:
                baseline_reason = "缺少适用签约版本（不阻断基线；响应电量按 0 容量校验标记）"

    baseline_output = {
        "rule": rule_meta,
        "lookback_days": int(rule.lookback_days) if rule else None,
        "min_sample_days": int(rule.min_sample_days) if rule else None,
        "adjustment_factor": float(rule.adjustment_factor) if rule else None,
        "sample_days": sample_days,
        "daily_sources": daily_baselines,
        "sample_eligibility": sample_eligibility,
        "baseline_kwh": number(baseline_total) if baseline_ok else None,
        "per_interval": [
            {"ts": ts.isoformat(), "kwh": per_interval_baseline.get(ts)} for ts in intervals
        ],
    }
    baseline = _node(
        run.id,
        customer.id,
        event.id,
        "baseline",
        PASS if baseline_ok else REVIEW,
        baseline_output,
        baseline_reason,
    )
    db.add(baseline)
    db.flush()
    nodes["baseline"] = baseline
    db.add(_source(baseline, "rule", rule.id, **rule_meta) if rule else _source(baseline, "rule", 0, missing=True))

    # ---------- 2b. 基线不确定度 (uncertainty, 从不阻断结算) ----------
    unc = uncertainty_svc.analyze_window(
        db,
        run=run,
        customer=customer,
        event=event,
        rule=rule,
        meter_points=meter_points,
        cutoff=cutoff,
        baseline_kwh=baseline_total if baseline_ok else None,
        baseline_sample_days=sample_days,
        cache=cache,
    )
    unc_node = _node(run.id, customer.id, event.id, "uncertainty", PASS, {}, None)
    db.add(unc_node)
    db.flush()
    nodes["uncertainty"] = unc_node
    for source_type, ref_id, meta in unc.sources:
        db.add(_source(unc_node, source_type, ref_id, **meta))
    report_row = models.UncertaintyReport(
        run_id=run.id,
        customer_id=customer.id,
        event_id=event.id,
        node_id=unc_node.id,
        **unc.row,
    )
    db.add(report_row)
    db.flush()
    unc_node.output = {**unc.summary, "report_id": report_row.id}

    if not baseline_ok:
        return _block_downstream(db, run, customer, event, nodes, baseline_reason, baseline)

    # ---------- 3. 响应电量 (response) ----------
    actual_total = sum(actual_map.get(ts, 0.0) for ts in intervals)
    missing_actual = [ts for ts in intervals if ts not in actual_map]
    if missing_actual:
        response_reason = f"活动窗口实际读数缺失 {len(missing_actual)} 个区间"
        response = _node(
            run.id,
            customer.id,
            event.id,
            "response",
            REVIEW,
            {
                "actual_kwh": None,
                "baseline_kwh": number(baseline_total),
                "missing_intervals": [ts.isoformat() for ts in missing_actual],
            },
            response_reason,
        )
        db.add(response)
        db.flush()
        nodes["response"] = response
        return _block_downstream(db, run, customer, event, nodes, response_reason, response)

    # 响应电量 = 基线 - 实际（正值为削峰贡献）
    response_kwh = baseline_total - actual_total
    capacity_check = None
    if contract is not None:
        duration = event_duration_hours(event)
        cap_energy = float(contract.capacity_kw) * duration
        capacity_check = {
            "contract_id": contract.id,
            "contract_version": contract.version,
            "capacity_kw": float(contract.capacity_kw),
            "capacity_kwh": number(cap_energy),
            "response_within_capacity": abs(response_kwh) <= cap_energy + 1e-9,
        }
    response = _node(
        run.id,
        customer.id,
        event.id,
        "response",
        PASS,
        {
            "actual_kwh": number(actual_total),
            "baseline_kwh": number(baseline_total),
            "response_kwh": number(response_kwh),
            "capacity_check": capacity_check,
        },
        None,
    )
    db.add(response)
    db.flush()
    nodes["response"] = response

    # ---------- 4. 结算 (settlement) ----------
    if contract is None:
        settle_reason = "缺少适用签约版本，无法结算（不按零值出账）"
        settlement = _node(
            run.id,
            customer.id,
            event.id,
            "settlement",
            REVIEW,
            {"response_kwh": number(response_kwh), "amount": None},
            settle_reason,
        )
        db.add(settlement)
        db.flush()
        nodes["settlement"] = settlement
        _wire_edges(db, run.id, nodes)
        return WindowOutcome(REVIEW, None, number(response_kwh), settle_reason, nodes)

    price = Decimal(str(event.price_per_kwh))
    cap_energy = Decimal(str(contract.capacity_kw)) * Decimal(str(event_duration_hours(event)))
    settle_kwh = min(Decimal(str(max(response_kwh, 0.0))), cap_energy)
    amount = money(settle_kwh * price)
    settlement = _node(
        run.id,
        customer.id,
        event.id,
        "settlement",
        PASS,
        {
            "response_kwh": number(response_kwh),
            "baseline_kwh": number(baseline_total),
            "actual_kwh": number(actual_total),
            "settled_kwh": number(settle_kwh),
            "price_per_kwh": float(price),
            "amount": amount,
            "contract_id": contract.id,
            "contract_version": contract.version,
            "capacity_kw": float(contract.capacity_kw),
            "capacity_kwh": number(cap_energy),
            "capacity_capped": settle_kwh < Decimal(str(max(response_kwh, 0.0))),
            "note": "响应电量为负（实际用电高于基线）时按 0 计结算电量；结算电量不超过签约容量×活动时长",
        },
        None,
    )
    db.add(settlement)
    db.flush()
    nodes["settlement"] = settlement
    db.add(
        _source(
            settlement,
            "contract",
            contract.id,
            customer_id=customer.id,
            version=contract.version,
            capacity_kw=float(contract.capacity_kw),
        )
    )
    _wire_edges(db, run.id, nodes)
    return WindowOutcome(PASS, amount, number(response_kwh), None, nodes)


def _block_downstream(
    db: Session,
    run: models.Run,
    customer: models.Customer,
    event: models.Event,
    nodes: dict[str, models.Node],
    reason: str | None,
    upstream: models.Node,
) -> WindowOutcome:
    order = ["ingest", "baseline", "uncertainty", "response", "settlement"]
    start = order.index(upstream.node_type) + 1
    for node_type in order[start:]:
        if node_type in nodes:
            continue
        blocked = _node(
            run.id,
            customer.id,
            event.id,
            node_type,
            BLOCKED,
            {},
            f"上游 {upstream.node_type} 待核：{reason or '未满足计算前置条件'}",
        )
        db.add(blocked)
        db.flush()
        nodes[node_type] = blocked
    _wire_edges(db, run.id, nodes)
    return WindowOutcome(REVIEW, None, None, reason, nodes)


def _wire_edges(db: Session, run_id: int, nodes: dict[str, models.Node]) -> None:
    chain = [n for n in (nodes.get("ingest"), nodes.get("baseline"), nodes.get("response"), nodes.get("settlement")) if n]
    for upstream, downstream in zip(chain, chain[1:]):
        db.add(
            models.LineageEdge(
                run_id=run_id,
                from_node_id=upstream.id,
                to_node_id=downstream.id,
                relation=f"{upstream.node_type}->{downstream.node_type}",
            )
        )
    # Uncertainty is a side branch of the baseline, never upstream of settlement.
    baseline = nodes.get("baseline")
    unc = nodes.get("uncertainty")
    if baseline is not None and unc is not None:
        db.add(
            models.LineageEdge(
                run_id=run_id,
                from_node_id=baseline.id,
                to_node_id=unc.id,
                relation="baseline->uncertainty",
            )
        )


def clone_window_from(
    db: Session,
    run: models.Run,
    base_window: models.RunWindow,
    customer: models.Customer,
    event: models.Event,
    meter_points: list[models.MeterPoint],
) -> WindowOutcome:
    """Copy a fully successful window from the base run, keeping source lineage.

    Targeted recomputation reuses untouched windows so repeated computation is
    avoided; the clone keeps ``reused_from_node_id`` so the new run is still
    traceable to the exact sources used.
    """
    base_run = db.get(models.Run, base_window.run_id)
    if base_run is None:
        raise ValueError("基线运行不存在")
    src_nodes = db.scalars(
        select(models.Node).where(
            models.Node.run_id == base_run.id,
            models.Node.customer_id == customer.id,
            models.Node.event_id == event.id,
        )
    ).all()
    by_type = {n.node_type: n for n in src_nodes}
    required = ("ingest", "baseline", "response", "settlement")
    if any(t not in by_type for t in required) or any(by_type[t].status != PASS for t in required):
        raise ValueError("来源窗口节点不完整或未全部成功，不可复用")

    new_nodes: dict[str, models.Node] = {}
    clone_types = [t for t in ("ingest", "baseline", "uncertainty", "response", "settlement") if t in by_type]
    for node_type in clone_types:
        src = by_type[node_type]
        clone = models.Node(
            run_id=run.id,
            customer_id=customer.id,
            event_id=event.id,
            node_type=node_type,
            status=src.status,
            reused_from_node_id=src.id,
            output=dict(src.output or {}),
            reason=src.reason,
            created_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        db.add(clone)
        db.flush()
        new_nodes[node_type] = clone
        for source in db.scalars(select(models.NodeSource).where(models.NodeSource.node_id == src.id)):
            db.add(
                models.NodeSource(
                    node_id=clone.id,
                    source_type=source.source_type,
                    ref_id=source.ref_id,
                    meta=dict(source.meta or {}),
                )
            )
    # Clone the uncertainty report so the reused window keeps its interval.
    if "uncertainty" in new_nodes:
        src_unc = by_type["uncertainty"]
        src_report = db.scalars(
            select(models.UncertaintyReport).where(models.UncertaintyReport.node_id == src_unc.id)
        ).first()
        if src_report is not None:
            new_report = models.UncertaintyReport(
                run_id=run.id,
                customer_id=customer.id,
                event_id=event.id,
                node_id=new_nodes["uncertainty"].id,
                status=src_report.status,
                primary_method=src_report.primary_method,
                level=src_report.level,
                baseline_kwh=src_report.baseline_kwh,
                lower_kwh=src_report.lower_kwh,
                upper_kwh=src_report.upper_kwh,
                half_width_kwh=src_report.half_width_kwh,
                qualified_days=src_report.qualified_days,
                report=dict(src_report.report or {}),
                input_refs=dict(src_report.input_refs or {}),
                reused_from_report_id=src_report.id,
            )
            db.add(new_report)
            db.flush()
            new_nodes["uncertainty"].output = {
                **new_nodes["uncertainty"].output,
                "report_id": new_report.id,
            }
    for edge in db.scalars(select(models.LineageEdge).where(models.LineageEdge.run_id == base_run.id)):
        from_node = next((n for n in src_nodes if n.id == edge.from_node_id), None)
        to_node = next((n for n in src_nodes if n.id == edge.to_node_id), None)
        if from_node is None or to_node is None:
            continue
        db.add(
            models.LineageEdge(
                run_id=run.id,
                from_node_id=new_nodes[from_node.node_type].id,
                to_node_id=new_nodes[to_node.node_type].id,
                relation=edge.relation,
            )
        )
    db.flush()
    settlement = new_nodes["settlement"]
    amount = float(settlement.output.get("amount") or 0.0)
    response_kwh = float(settlement.output.get("response_kwh") or 0.0)
    return WindowOutcome(
        PASS,
        amount,
        response_kwh,
        f"沿用 {base_run.run_code} 的窗口结果（来自节点 #{by_type['settlement'].id}）",
        new_nodes,
    )


def recompute_cleanup(db: Session, run_id: int) -> None:
    """Remove graph artefacts of an aborted run so a retry starts clean."""
    node_ids = [n.id for n in db.scalars(select(models.Node).where(models.Node.run_id == run_id))]
    if node_ids:
        db.execute(delete(models.LineageEdge).where(models.LineageEdge.run_id == run_id))
        db.execute(delete(models.NodeSource).where(models.NodeSource.node_id.in_(node_ids)))
        db.execute(delete(models.Node).where(models.Node.run_id == run_id))
    db.execute(delete(models.UncertaintyReport).where(models.UncertaintyReport.run_id == run_id))
    db.execute(delete(models.RunWindow).where(models.RunWindow.run_id == run_id))
    db.flush()
