"""Replayable integration-acceptance scenarios for the demo console.

Each scenario executes against the live database and returns a structured,
human-readable transcript so the same flow can be replayed from the UI or from
``pytest`` (see ``backend/tests/test_acceptance.py``).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..db import SessionLocal
from ..seed import DEMO_PERIOD, DEMO_SCENARIOS, reset_database, seed
from ..serializers import compare_runs
from . import orchestrator, registry
from .orchestrator import OrchestrationError, PASS, REVIEW

Step = dict


def _step(name: str, ok: bool, detail: object = None) -> Step:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _latest_run(db: Session, period: str = DEMO_PERIOD) -> models.Run | None:
    return db.scalars(
        select(models.Run).where(models.Run.period_month == period).order_by(models.Run.version.desc()).limit(1)
    ).first()


def _event(db: Session, code: str) -> models.Event:
    return db.scalars(select(models.Event).where(models.Event.code == code)).one()


def _meter(db: Session, meter_no: str) -> models.MeterPoint:
    return db.scalars(select(models.MeterPoint).where(models.MeterPoint.meter_no == meter_no)).one()


def _daily_intervals(day, low: float, high: float, event: models.Event) -> list[dict]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    rows = []
    for i in range(96):
        ts = start + timedelta(minutes=15 * i)
        in_window = event.start_time <= ts.time() < event.end_time
        rows.append({"ts": ts.isoformat(), "kwh": round(low if in_window else high, 3)})
    return rows


def _baseline_published(db: Session) -> models.Run:
    """Ensure exactly one initial published run exists for the demo period."""
    run = _latest_run(db)
    if run is None or run.published_at is None:
        if run is None:
            run = orchestrator.create_run(db, actor="planner", period_month=DEMO_PERIOD, strategy="initial")
        if run.status != PASS:
            raise OrchestrationError(f"基线运行未成功：{run.status}")
        orchestrator.publish_run(db, run.id, "approver", "基线账本")
        run = _latest_run(db)
    return run


def scenario_baseline_shortfall(db: Session) -> list[Step]:
    steps: list[Step] = []
    rule = registry.add_rule_version(
        db,
        {
            "rule_code": "BL-STRICT",
            "name": "严格样本规则",
            "lookback_days": 5,
            "min_sample_days": 6,
            "adjustment_factor": 1.0,
            "effective_from": datetime(2025, 1, 1).date(),
            "reason": "联调：样本要求超过回看窗口",
        },
        "planner",
    )
    steps.append(_step("登记样本不足规则版本", True, {"rule_id": rule.id, "version": rule.version}))
    run = orchestrator.create_run(
        db,
        actor="planner",
        period_month=DEMO_PERIOD,
        strategy="full_period",
        rule_ids=[rule.id],
        notes="验收：样本不足",
    )
    steps.append(
        _step(
            "整期运行停在待核",
            run.status == REVIEW,
            {"run_code": run.run_code, "status": run.status, "windows": run.target_windows},
        )
    )
    nodes = db.scalars(
        select(models.Node).where(models.Node.run_id == run.id, models.Node.status != "success")
    ).all()
    kinds = {(n.node_type, n.status) for n in nodes}
    steps.append(
        _step(
            "下游节点全部 blocked，无零值结算",
            ("baseline", "needs_review") in kinds
            and ("response", "blocked") in kinds
            and ("settlement", "blocked") in kinds,
            sorted(f"{k[0]}:{k[1]}" for k in kinds),
        )
    )
    rejected = False
    try:
        orchestrator.publish_run(db, run.id, "approver", "不应成功")
    except OrchestrationError as exc:
        rejected = exc.status_code == 409
        steps.append(_step("发布账本被拒绝", rejected, exc.message))
    if not rejected:
        steps.append(_step("发布账本被拒绝", False, "意外地允许发布"))
    # Clean up so other scenarios see the normal rule again.
    registry.add_rule_version(
        db,
        {
            "rule_code": "BL-DEFAULT",
            "name": "恢复默认规则",
            "lookback_days": 5,
            "min_sample_days": 3,
            "adjustment_factor": 1.0,
            "effective_from": datetime(2025, 9, 1).date(),
            "reason": "联调结束恢复默认",
        },
        "planner",
    )
    return steps


def scenario_concurrency_claim(db: Session) -> list[Step]:
    base = _baseline_published(db)
    event = _event(db, "DR-20250805")
    customer = db.scalars(select(models.Customer).where(models.Customer.code == "C-001")).one()
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker(hold: float) -> None:
        with SessionLocal() as local_db:
            barrier.wait(timeout=5)
            try:
                run = orchestrator.create_run(
                    local_db,
                    actor="planner",
                    period_month=DEMO_PERIOD,
                    strategy="initial",
                    event_id=event.id,
                    debug_hold_seconds=hold,
                    notes="并发抢占窗口",
                )
                results.append({"outcome": "started", "run_code": run.run_code, "run_id": run.id})
            except OrchestrationError as exc:
                results.append({"outcome": "conflict", "status": exc.status_code, "message": exc.message})

    threads = [threading.Thread(target=worker, args=(2.0,)), threading.Thread(target=worker, args=(0.0,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    winners = [r for r in results if r["outcome"] == "started"]
    losers = [r for r in results if r["outcome"] == "conflict"]
    steps = [
        _step(
            "仅一个运行取得窗口快照",
            len(winners) == 1 and len(losers) == 1,
            {"results": results, "baseline_run": base.run_code},
        )
    ]
    active = db.scalars(
        select(models.RunWindowClaim).where(
            models.RunWindowClaim.customer_id == customer.id,
            models.RunWindowClaim.event_id == event.id,
            models.RunWindowClaim.status == "active",
        )
    ).all()
    steps.append(_step("冲突后窗口锁已释放", len(active) == 0, {"active_claims": len(active)}))
    duplicate_versions = db.scalars(
        select(models.Run).where(models.Run.period_month == DEMO_PERIOD).order_by(models.Run.version)
    ).all()
    steps.append(
        _step(
            "冲突运行未写入任何节点",
            all(
                (db.scalar(select(models.RunWindow.id).where(models.RunWindow.run_id == r.id).limit(1)) is not None)
                == (r.status != "failed")
                for r in duplicate_versions
            ),
            [{"run": r.run_code, "status": r.status} for r in duplicate_versions],
        )
    )
    return steps


def _rebatch_event_meter(
    db: Session, meter_no: str, event_code: str, low: float, high: float, key: str
) -> models.MeterBatch:
    event = _event(db, event_code)
    meter = _meter(db, meter_no)
    intervals = _daily_intervals(event.event_date, low, high, event)
    return registry.ingest_batch(
        db,
        {
            "batch_key": key,
            "meter_no": meter.meter_no,
            "intervals": intervals,
            "source_note": "迟到更正批次（联调）",
        },
        "planner",
    )


def scenario_late_batch_targeted(db: Session) -> list[Step]:
    steps: list[Step] = []
    base = _baseline_published(db)
    first_entry = db.scalars(
        select(models.LedgerEntry)
        .where(models.LedgerEntry.run_id == base.id)
        .order_by(models.LedgerEntry.id)
        .limit(1)
    ).first()
    if first_entry is None:
        raise OrchestrationError("基线账本为空")
    voucher = orchestrator.record_statement_voucher(db, first_entry.id, f"STV-REF-{first_entry.id}")
    steps.append(
        _step(
            "基线账本已付款",
            True,
            {"ledger_id": first_entry.id, "voucher": voucher.voucher_no, "amount": float(voucher.amount)},
        )
    )
    event = db.get(models.Event, first_entry.event_id)
    batch = _rebatch_event_meter(db, "M001-01", event.code, low=20.0, high=330.0, key=f"LATE-M001-01-{event.code}")
    steps.append(
        _step(
            "迟到批次登记为新版本",
            batch.version >= 2,
            {"batch_id": batch.id, "version": batch.version, "supersedes": batch.supersedes_batch_id},
        )
    )
    affected, evidence = orchestrator.find_affected_windows(db, batch_ids=[batch.id])
    steps.append(
        _step(
            "依赖图定位受影响窗口",
            bool(affected),
            {
                "affected": [
                    {"event_id": eid, "customer_ids": sorted(cids)} for eid, cids in affected.items()
                ],
                "evidence_count": len(evidence),
            },
        )
    )
    run = orchestrator.create_run(
        db,
        actor="planner",
        period_month=DEMO_PERIOD,
        strategy="targeted",
        batch_ids=[batch.id],
        trigger_batch_id=batch.id,
        notes="验收：迟到批次定向重算",
    )
    steps.append(
        _step(
            "定向重算仅计算受影响窗口",
            run.status == PASS and run.computed_windows < run.target_windows,
            {
                "run_code": run.run_code,
                "computed": run.computed_windows,
                "reused": run.reused_windows,
                "target": run.target_windows,
            },
        )
    )
    result = orchestrator.publish_run(db, run.id, "approver", "迟到批次定向重算")
    steps.append(
        _step(
            "新版本账本发布并产生调整",
            bool(result["adjustment_ids"]) and result["adjustment_total"] != 0,
            {
                "ledger_versions": len(result["ledger_entry_ids"]),
                "adjustments": len(result["adjustment_ids"]),
                "total_delta": result["adjustment_total"],
            },
        )
    )
    original = db.get(models.LedgerEntry, first_entry.id)
    steps.append(
        _step(
            "原账本与凭证未被改写",
            original is not None and original.status == "superseded" and float(original.amount) == float(first_entry.amount),
            {
                "original_status": original.status if original else None,
                "original_amount": float(original.amount) if original else None,
                "voucher_paid": original.paid if original else None,
            },
        )
    )
    pending = db.scalars(
        select(models.Adjustment).where(models.Adjustment.run_id == run.id, models.Adjustment.status == "pending")
    ).all()
    if pending:
        adj = orchestrator.approve_adjustment(db, pending[0].id, "approver", True)
        steps.append(
            _step(
                "批准调整生成独立调整凭证",
                adj.adjustment_voucher_id is not None,
                {"adjustment": adj.code, "voucher_id": adj.adjustment_voucher_id},
            )
        )
    return steps


def scenario_late_contract_adjustment(db: Session) -> list[Step]:
    steps: list[Step] = []
    base = _baseline_published(db)
    entry = db.scalars(
        select(models.LedgerEntry)
        .where(models.LedgerEntry.run_id == base.id, models.LedgerEntry.customer_id == 1)
        .order_by(models.LedgerEntry.id)
        .limit(1)
    ).first()
    if entry is None:
        raise OrchestrationError("找不到客户 C-001 的基线账本")
    voucher = orchestrator.record_statement_voucher(db, entry.id, f"STV-CONTRACT-{entry.id}")
    steps.append(_step("原账本付款入账", True, {"voucher": voucher.voucher_no, "amount": float(voucher.amount)}))
    event = db.get(models.Event, entry.event_id)
    contract = registry.add_contract_version(
        db,
        {
            "customer_id": entry.customer_id,
            "capacity_kw": 40.0,
            "price_ceiling": 4.0,
            "effective_from": event.event_date,
            "reason": "联调：签约容量修订（晚于付款）",
        },
        "planner",
    )
    steps.append(
        _step(
            "签约修订登记为新版本",
            contract.version >= 2,
            {"contract_id": contract.id, "version": contract.version, "capacity_kw": float(contract.capacity_kw)},
        )
    )
    run = orchestrator.create_run(
        db,
        actor="planner",
        period_month=DEMO_PERIOD,
        strategy="targeted",
        contract_ids=[contract.id],
        notes="验收：签约修订定向重算",
    )
    steps.append(
        _step(
            "定向重算完成",
            run.status in {PASS, REVIEW},
            {"run_code": run.run_code, "status": run.status, "computed": run.computed_windows},
        )
    )
    result = orchestrator.publish_run(db, run.id, "approver", "签约修订重算")
    steps.append(
        _step(
            "产生关联调整记录",
            bool(result["adjustment_ids"]),
            {"adjustments": result["adjustment_ids"], "total_delta": result["adjustment_total"]},
        )
    )
    original_voucher = db.get(models.Voucher, voucher.id)
    steps.append(
        _step(
            "原付款凭证金额不变",
            original_voucher is not None and float(original_voucher.amount) == float(voucher.amount),
            {"voucher": original_voucher.voucher_no if original_voucher else None},
        )
    )
    if result["adjustment_ids"]:
        adj = orchestrator.approve_adjustment(db, result["adjustment_ids"][0], "approver", True)
        steps.append(
            _step(
                "调整凭证独立生成",
                adj.adjustment_voucher_id is not None,
                {"adjustment": adj.code, "voucher_id": adj.adjustment_voucher_id},
            )
        )
    return steps


def scenario_rule_revision_full_period(db: Session) -> list[Step]:
    steps: list[Step] = []
    before = _baseline_published(db)
    rule = registry.add_rule_version(
        db,
        {
            "rule_code": "BL-DEFAULT",
            "name": "同窗口日均值法 v2",
            "lookback_days": 5,
            "min_sample_days": 3,
            "adjustment_factor": 1.05,
            "effective_from": datetime(2025, 1, 1).date(),
            "reason": "联调：基线调整系数 1.05",
        },
        "planner",
    )
    steps.append(_step("登记规则修订版本", True, {"rule_id": rule.id, "version": rule.version}))
    run = orchestrator.create_run(
        db,
        actor="planner",
        period_month=DEMO_PERIOD,
        strategy="full_period",
        rule_ids=[rule.id],
        notes="验收：规则修订整期重跑",
    )
    steps.append(
        _step(
            "整期不可变重跑生成新版本",
            run.status == PASS and run.version > before.version,
            {"run_code": run.run_code, "version": run.version, "windows": run.target_windows},
        )
    )
    result = orchestrator.publish_run(db, run.id, "approver", "规则修订整期重跑")
    steps.append(
        _step(
            "逐窗口差异转为调整",
            bool(result["adjustment_ids"]),
            {"adjustments": len(result["adjustment_ids"]), "total_delta": result["adjustment_total"]},
        )
    )
    comparison = compare_runs(db, before.id, run.id)
    steps.append(
        _step(
            "差异看板可定位差异窗口",
            comparison["changed_count"] > 0,
            {"changed_count": comparison["changed_count"], "total_delta": comparison["total_delta"]},
        )
    )
    return steps


# --------------------------------------------------------------------------
# baseline-uncertainty scenarios
# --------------------------------------------------------------------------
def _report_for(db: Session, run_id: int, customer_id: int, event_id: int) -> models.UncertaintyReport | None:
    return db.scalars(
        select(models.UncertaintyReport).where(
            models.UncertaintyReport.run_id == run_id,
            models.UncertaintyReport.customer_id == customer_id,
            models.UncertaintyReport.event_id == event_id,
        )
    ).first()


def _baseline_node(db: Session, run_id: int, customer_id: int, event_id: int) -> models.Node | None:
    return db.scalars(
        select(models.Node).where(
            models.Node.run_id == run_id,
            models.Node.customer_id == customer_id,
            models.Node.event_id == event_id,
            models.Node.node_type == "baseline",
        )
    ).first()


def _pseudo_rows(report: models.UncertaintyReport) -> list[dict]:
    rows = (report.report or {}).get("samples", {}).get("eligibility", [])
    return [row for row in rows if row.get("pseudo")]


def _register_demo_customer(
    db: Session,
    *,
    code: str,
    name: str,
    region: str,
    meter_no: str,
    capacity_kw: float,
    day_kwh,
    first_day: date,
    last_day: date,
) -> models.Customer:
    """Register a demo customer with meter, contract and daily batches."""
    customer = models.Customer(code=code, name=name, region=region, contact="联调演示")
    db.add(customer)
    db.flush()
    db.add(
        models.ContractVersion(
            customer_id=customer.id,
            version=1,
            capacity_kw=capacity_kw,
            price_ceiling=4.0,
            effective_from=date(2025, 1, 1),
            reason="联调演示签约",
            created_by="planner",
        )
    )
    meter = models.MeterPoint(customer_id=customer.id, meter_no=meter_no, name=f"{name} 计量点")
    db.add(meter)
    db.flush()
    day = first_day
    while day <= last_day:
        intervals = []
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        for j in range(96):
            intervals.append({"ts": (start + timedelta(minutes=15 * j)).isoformat(), "kwh": day_kwh(day, j)})
        registry.ingest_batch(
            db,
            {"batch_key": f"{meter_no}-{day.isoformat()}", "meter_no": meter_no, "intervals": intervals},
            "planner",
        )
        day += timedelta(days=1)
    db.commit()
    return customer


def scenario_uncertainty_calibration(db: Session) -> list[Step]:
    steps: list[Step] = []
    run = orchestrator.create_run(db, actor="planner", period_month=DEMO_PERIOD, strategy="initial")
    steps.append(
        _step("整期运行成功", run.status == PASS, {"run_code": run.run_code, "status": run.status})
    )
    reports = db.scalars(
        select(models.UncertaintyReport).where(models.UncertaintyReport.run_id == run.id)
    ).all()
    steps.append(
        _step(
            "每个客户活动窗口均生成不确定度报告",
            len(reports) == 18,
            {"reports": len(reports), "windows": run.target_windows},
        )
    )
    node_count = db.scalar(select(func.count()).select_from(models.Node).where(models.Node.run_id == run.id))
    steps.append(
        _step("每窗口 5 个节点（含 uncertainty 旁路）", node_count == 18 * 5, {"nodes": node_count})
    )

    event_days = {e.event_date for e in db.scalars(select(models.Event)).all()}
    label_ok = True
    leakage_ok = True
    for report in reports:
        for row in _pseudo_rows(report):
            day = date.fromisoformat(row["day"])
            if day in event_days:
                label_ok = False
            pseudo = row["pseudo"]
            if row["day"] not in pseudo["excluded_days"] or not pseudo["leakage_window"]:
                leakage_ok = False
    steps.append(
        _step(
            "伪活动日残差全部来自非活动日（实际活动日负荷从不作误差标签）",
            label_ok,
            {"event_days": sorted(d.isoformat() for d in event_days)},
        )
    )
    steps.append(_step("每个伪活动日均排除目标日及泄漏窗口", leakage_ok, None))

    interval_ok = True
    method_ok = True
    refs_ok = True
    degraded_map: dict[str, int] = {}
    for report in reports:
        detail = report.report or {}
        degraded_map[report.status] = degraded_map.get(report.status, 0) + 1
        interval = detail.get("interval")
        if interval is None or report.baseline_kwh is None:
            interval_ok = False
        else:
            if not (interval["lower_kwh"] <= float(report.baseline_kwh) <= interval["upper_kwh"]):
                interval_ok = False
            if interval["relative_width"] is None or interval["relative_width"] > 0.5:
                interval_ok = False
        methods = detail.get("methods", {})
        holdout = methods.get("holdout") or {}
        bootstrap = methods.get("block_bootstrap") or {}
        if holdout.get("interval") is None or bootstrap.get("interval") is None:
            method_ok = False
        if bootstrap.get("replicates") != 200:
            method_ok = False
        width = detail.get("width_components") or {}
        if width.get("residual_noise_kwh") is None or width.get("final_half_width_kwh") is None:
            method_ok = False
        refs = report.input_refs or {}
        if refs.get("rule_version") != 1 or not refs.get("cutoff_at") or not refs.get("batch_ids"):
            refs_ok = False
    steps.append(
        _step("区间包含基线值且相对宽度 ≤50%", interval_ok, {"status分布": degraded_map})
    )
    steps.append(
        _step("留出法与块重采样双方法产出区间，宽度来源分解齐全", method_ok, None)
    )
    steps.append(_step("报告引用同一锁定规则与输入版本", refs_ok, None))
    return steps


def scenario_uncertainty_flat_load(db: Session) -> list[Step]:
    steps: list[Step] = []
    customer = _register_demo_customer(
        db,
        code="C-901",
        name="平稳负荷示范户",
        region="高新区",
        meter_no="M901-01",
        capacity_kw=2000.0,
        day_kwh=lambda _day, _slot: 40.0,
        first_day=date(2025, 7, 20),
        last_day=date(2025, 8, 22),
    )
    steps.append(_step("登记负荷恒定的示范客户（40 kWh/区间）", True, {"customer_id": customer.id}))
    event = _event(db, "DR-20250820")
    run = orchestrator.create_run(
        db, actor="planner", period_month=DEMO_PERIOD, strategy="initial", event_id=event.id
    )
    report = _report_for(db, run.id, customer.id, event.id)
    steps.append(_step("运行完成且报告生成", report is not None, {"run_code": run.run_code}))
    residuals = [row["pseudo"]["residual_kwh"] for row in _pseudo_rows(report)]
    max_abs = max((abs(r) for r in residuals), default=None)
    steps.append(
        _step(
            "伪活动日残差≈0（平稳负荷）",
            max_abs is not None and max_abs <= 0.01 and len(residuals) >= 15,
            {"n_residuals": len(residuals), "max_abs_residual": max_abs},
        )
    )
    interval = (report.report or {}).get("interval") or {}
    steps.append(
        _step(
            "区间收窄且状态 ok",
            report.status == "ok" and interval.get("half_width_kwh") is not None and interval["half_width_kwh"] <= 0.01,
            {"status": report.status, "interval": interval},
        )
    )
    return steps


def scenario_uncertainty_holiday_exclusion(db: Session) -> list[Step]:
    steps: list[Step] = []
    holiday = registry.add_holiday(db, {"day": date(2025, 8, 19), "name": "联调示范假日（虚构）"}, "planner")
    steps.append(
        _step("在回看窗口内登记节假日", True, {"day": holiday.day.isoformat(), "name": holiday.name})
    )
    event = _event(db, "DR-20250820")
    run = orchestrator.create_run(
        db, actor="planner", period_month=DEMO_PERIOD, strategy="initial", event_id=event.id
    )
    customer = db.scalars(select(models.Customer).where(models.Customer.code == "C-001")).one()
    baseline = _baseline_node(db, run.id, customer.id, event.id)
    elig = {row["day"]: row for row in (baseline.output or {}).get("sample_eligibility", [])}
    row = elig.get("2025-08-19")
    steps.append(
        _step(
            "基线样本资格表保留 holiday 排除理由",
            bool(row) and not row["eligible"] and "holiday" in row["reasons"] and not row["used"],
            row,
        )
    )
    steps.append(
        _step(
            "基线样本跳过节假日（4 天合格）",
            (baseline.output or {}).get("sample_days") == 4,
            {"sample_days": (baseline.output or {}).get("sample_days")},
        )
    )
    report = _report_for(db, run.id, customer.id, event.id)
    calib = {r["day"]: r for r in (report.report or {}).get("samples", {}).get("eligibility", [])}
    calib_row = calib.get("2025-08-19") or {}
    steps.append(
        _step(
            "伪活动日序列不含节假日",
            not calib_row.get("eligible") and "holiday" in calib_row.get("reasons", []) and "pseudo" not in calib_row,
            {"eligible": calib_row.get("eligible"), "reasons": calib_row.get("reasons")},
        )
    )
    return steps


def scenario_uncertainty_missing_weather(db: Session) -> list[Step]:
    steps: list[Step] = []
    event = _event(db, "DR-20250812")
    run = orchestrator.create_run(
        db, actor="planner", period_month=DEMO_PERIOD, strategy="initial", event_id=event.id
    )
    c003 = db.scalars(select(models.Customer).where(models.Customer.code == "C-003")).one()
    c001 = db.scalars(select(models.Customer).where(models.Customer.code == "C-001")).one()
    report = _report_for(db, run.id, c003.id, event.id)
    weather = (report.report or {}).get("samples", {}).get("weather_coverage", {})
    steps.append(
        _step(
            "云岭工业区（C-003）天气覆盖诊断为 missing",
            weather.get("status") == "missing",
            {"weather_coverage": weather},
        )
    )
    detail = report.report or {}
    steps.append(
        _step(
            "状态降级（degraded）但区间仍产出",
            report.status == "degraded"
            and "weather_missing" in detail.get("degraded_reasons", [])
            and report.lower_kwh is not None
            and report.upper_kwh is not None,
            {"status": report.status, "reasons": detail.get("degraded_reasons")},
        )
    )
    factor = (detail.get("width_components") or {}).get("weather_factor")
    steps.append(
        _step("覆盖因子按天气缺项放宽（≥1.25）", factor is not None and factor >= 1.25, {"weather_factor": factor})
    )
    other = _report_for(db, run.id, c001.id, event.id)
    other_weather = (other.report or {}).get("samples", {}).get("weather_coverage", {})
    steps.append(
        _step(
            "有气象数据的客户覆盖诊断正常",
            other_weather.get("status") == "ok",
            {"customer": "C-001", "weather_coverage": other_weather},
        )
    )
    return steps


def scenario_uncertainty_insufficient_coverage(db: Session) -> list[Step]:
    steps: list[Step] = []
    customer = _register_demo_customer(
        db,
        code="C-902",
        name="新装有限历史户",
        region="高新区",
        meter_no="M902-01",
        capacity_kw=1500.0,
        day_kwh=lambda _day, slot: 30.0 + (slot % 8),
        first_day=date(2025, 8, 16),
        last_day=date(2025, 8, 20),
    )
    steps.append(
        _step("登记仅有 5 天计量历史的新客户", True, {"customer_id": customer.id, "days": 5})
    )
    event = _event(db, "DR-20250820")
    run = orchestrator.create_run(
        db, actor="planner", period_month=DEMO_PERIOD, strategy="initial", event_id=event.id
    )
    baseline = _baseline_node(db, run.id, customer.id, event.id)
    steps.append(
        _step(
            "基线可算（4 天合格样本 ≥3）",
            baseline is not None and baseline.status == "success" and (baseline.output or {}).get("sample_days") == 4,
            {"sample_days": (baseline.output or {}).get("sample_days") if baseline else None},
        )
    )
    report = _report_for(db, run.id, customer.id, event.id)
    detail = report.report or {}
    steps.append(
        _step(
            "伪活动日残差 <5 → 报告 unavailable",
            report.status == "unavailable"
            and "qualified_samples_insufficient" in detail.get("unavailable_reasons", []),
            {"status": report.status, "pseudo_days": detail.get("samples", {}).get("pseudo_days")},
        )
    )
    steps.append(
        _step("不确定度缺失不阻断运行", run.status == PASS, {"run_status": run.status})
    )
    result = orchestrator.publish_run(db, run.id, "approver", "覆盖不足场景：区间缺失不影响出账")
    steps.append(
        _step(
            "账本仍可发布（不确定度只报告不设卡）",
            len(result["ledger_entry_ids"]) == run.target_windows,
            {"ledger_entries": len(result["ledger_entry_ids"])},
        )
    )
    return steps


def scenario_uncertainty_event_contamination(db: Session) -> list[Step]:
    steps: list[Step] = []
    db.add(
        models.Event(
            code="DR-20250818",
            name="联调追加活动（夹杂在回看窗口）",
            event_date=date(2025, 8, 18),
            start_time=time(18, 0),
            end_time=time(19, 0),
            price_per_kwh=3.0,
            period_month=DEMO_PERIOD,
        )
    )
    db.commit()
    steps.append(_step("在 8/20 回看窗口内登记新活动日 8/18", True, {"event_date": "2025-08-18"}))
    event = _event(db, "DR-20250820")
    run = orchestrator.create_run(
        db, actor="planner", period_month=DEMO_PERIOD, strategy="initial", event_id=event.id
    )
    customer = db.scalars(select(models.Customer).where(models.Customer.code == "C-001")).one()
    baseline = _baseline_node(db, run.id, customer.id, event.id)
    elig = {row["day"]: row for row in (baseline.output or {}).get("sample_eligibility", [])}
    row = elig.get("2025-08-18")
    steps.append(
        _step(
            "基线样本以 event_day 理由排除夹杂活动日",
            bool(row) and not row["used"] and "event_day" in row["reasons"],
            row,
        )
    )
    report = _report_for(db, run.id, customer.id, event.id)
    calib = {r["day"]: r for r in (report.report or {}).get("samples", {}).get("eligibility", [])}
    calib_row = calib.get("2025-08-18") or {}
    steps.append(
        _step(
            "伪活动日序列排除该活动日",
            not calib_row.get("eligible")
            and "event_day" in calib_row.get("reasons", [])
            and "pseudo" not in calib_row,
            {"eligible": calib_row.get("eligible"), "reasons": calib_row.get("reasons")},
        )
    )
    event_days = {e.event_date for e in db.scalars(select(models.Event)).all()}
    labels_ok = all(
        date.fromisoformat(r["day"]) not in event_days for r in _pseudo_rows(report)
    )
    steps.append(
        _step(
            "全部伪活动日残差均来自非活动日（实际活动日负荷不作误差标签）",
            labels_ok,
            {"pseudo_days": len(_pseudo_rows(report)), "event_days": sorted(d.isoformat() for d in event_days)},
        )
    )
    return steps


SCENARIOS: dict[str, Callable[[Session], list[Step]]] = {
    "baseline_shortfall": scenario_baseline_shortfall,
    "concurrency_claim": scenario_concurrency_claim,
    "late_batch_targeted": scenario_late_batch_targeted,
    "late_contract_adjustment": scenario_late_contract_adjustment,
    "rule_revision_full_period": scenario_rule_revision_full_period,
    "uncertainty_calibration": scenario_uncertainty_calibration,
    "uncertainty_flat_load": scenario_uncertainty_flat_load,
    "uncertainty_holiday_exclusion": scenario_uncertainty_holiday_exclusion,
    "uncertainty_missing_weather": scenario_uncertainty_missing_weather,
    "uncertainty_insufficient_coverage": scenario_uncertainty_insufficient_coverage,
    "uncertainty_event_contamination": scenario_uncertainty_event_contamination,
}


def reset_and_seed() -> None:
    reset_database()
    with SessionLocal() as db:
        seed(db)


def run_scenario(key: str) -> dict:
    if key not in SCENARIOS:
        raise KeyError(key)
    reset_and_seed()
    with SessionLocal() as db:
        steps = SCENARIOS[key](db)
    return {
        "key": key,
        "ok": all(s["ok"] for s in steps),
        "steps": steps,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def acceptance_report() -> dict:
    results = []
    for key in SCENARIOS:
        try:
            outcome = run_scenario(key)
        except Exception as exc:  # noqa: BLE001
            outcome = {"key": key, "ok": False, "steps": [{"name": "执行异常", "ok": False, "detail": str(exc)}]}
        results.append(outcome)
    return {
        "ok": all(r["ok"] for r in results),
        "period_month": DEMO_PERIOD,
        "scenarios": results,
    }


def scenarios_meta() -> list[dict]:
    return DEMO_SCENARIOS
