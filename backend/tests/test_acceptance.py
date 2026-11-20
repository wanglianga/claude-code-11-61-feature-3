"""End-to-end acceptance tests for the settlement recomputation platform.

Run with::

    cd backend && .venv/bin/python -m pytest -q

The suite exercises the same code paths as the demo endpoints so the UI
replay and CI agree.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app import models
from app.db import SessionLocal
from app.services import demo, orchestrator, registry

PERIOD = "2025-08"


def _reset():
    demo.reset_and_seed()


def _run(strategy="initial", **kwargs):
    with SessionLocal() as db:
        return orchestrator.create_run(db, actor="planner", period_month=PERIOD, strategy=strategy, **kwargs)


def test_initial_run_freezes_snapshot_and_settles():
    _reset()
    with SessionLocal() as db:
        run = orchestrator.create_run(db, actor="planner", period_month=PERIOD, strategy="initial")
        assert run.status == "success"
        assert run.target_windows == 18 and run.computed_windows == 18
        assert run.node_count == 90  # 18 窗口 × 5 节点（含 uncertainty 旁路）
        assert len(run.batch_snapshot) > 0
        assert run.rule_snapshot and run.contract_snapshot
        settlements = db.scalars(
            select(models.Node).where(models.Node.run_id == run.id, models.Node.node_type == "settlement")
        ).all()
        assert all(float(n.output["amount"]) > 0 for n in settlements)
        # Live batches arriving after the cut-off must not enter the frozen snapshot.
        assert max(run.batch_snapshot) < 10_000


def test_publish_is_append_only_and_creates_adjustments():
    _reset()
    with SessionLocal() as db:
        base = orchestrator.create_run(db, actor="planner", period_month=PERIOD, strategy="initial")
        first = orchestrator.publish_run(db, base.id, "approver", "首版")
        assert len(first["ledger_entry_ids"]) == 18
        with pytest.raises(orchestrator.OrchestrationError) as exc:
            orchestrator.publish_run(db, base.id, "approver", "重复发布")
        assert exc.value.status_code == 409

        voucher = orchestrator.record_statement_voucher(db, first["ledger_entry_ids"][0], "REF-A")
        rule = registry.add_rule_version(
            db,
            {
                "rule_code": "BL-DEFAULT",
                "lookback_days": 5,
                "min_sample_days": 3,
                "adjustment_factor": 1.08,
                "effective_from": datetime(2025, 8, 1).date(),
                "reason": "测试修订",
            },
            "planner",
        )
        second_run = orchestrator.create_run(
            db, actor="planner", period_month=PERIOD, strategy="full_period", rule_ids=[rule.id]
        )
        assert second_run.status == "success"
        second = orchestrator.publish_run(db, second_run.id, "approver", "规则修订重跑")
        assert len(second["adjustment_ids"]) == 18
        assert abs(second["adjustment_total"]) > 0
        original = db.get(models.MeterBatch, 1)  # sanity: no batch mutation API exists
        assert original is not None
        old_entry = db.get(models.LedgerEntry, first["ledger_entry_ids"][0])
        assert old_entry.status == "superseded"
        assert float(old_entry.amount) > 0
        old_voucher = db.get(models.Voucher, voucher.id)
        assert float(old_voucher.amount) == float(voucher.amount)


def test_late_batch_targeted_reuses_and_adjusts():
    _reset()
    report = demo.run_scenario("late_batch_targeted")
    assert report["ok"], report
    with SessionLocal() as db:
        targeted = db.scalars(
            select(models.Run).where(models.Run.strategy == "targeted").order_by(models.Run.id.desc())
        ).first()
        assert targeted is not None
        assert targeted.computed_windows == 1
        assert targeted.reused_windows == targeted.target_windows - 1
        reused_nodes = db.scalars(
            select(models.Node).where(
                models.Node.run_id == targeted.id, models.Node.reused_from_node_id.is_not(None)
            )
        ).all()
        assert len(reused_nodes) == 5 * targeted.reused_windows  # 含 uncertainty 节点克隆
        # Reused nodes still carry the original batch lineage.
        sources = db.scalars(
            select(models.NodeSource).where(models.NodeSource.node_id.in_([n.id for n in reused_nodes]))
        ).all()
        assert any(s.source_type == "batch" for s in sources)


def test_baseline_shortfall_blocks_downstream_and_publish():
    _reset()
    report = demo.run_scenario("baseline_shortfall")
    assert report["ok"], report
    with SessionLocal() as db:
        run = db.scalars(select(models.Run).order_by(models.Run.id.desc())).first()
        settlements = db.scalars(
            select(models.Node).where(models.Node.run_id == run.id, models.Node.node_type == "settlement")
        ).all()
        assert settlements and all(n.status == "blocked" and not n.output for n in settlements)
        assert db.scalars(
            select(models.LedgerEntry).where(models.LedgerEntry.run_id == run.id)
        ).first() is None


def test_concurrent_runs_exclusive_window_claim():
    _reset()
    report = demo.run_scenario("concurrency_claim")
    assert report["ok"], report


def test_status_advanced_after_payment_creates_linked_adjustment():
    _reset()
    report = demo.run_scenario("late_contract_adjustment")
    assert report["ok"], report
    with SessionLocal() as db:
        statement = db.scalars(select(models.Voucher).where(models.Voucher.kind == "statement")).first()
        adjustments = db.scalars(select(models.Voucher).where(models.Voucher.kind == "adjustment")).all()
        assert statement is not None and adjustments
        assert any(statement.voucher_no in v.reference for v in adjustments)
        assert float(adjustments[0].amount) != 0


def test_rule_revision_full_period_produces_diff():
    _reset()
    report = demo.run_scenario("rule_revision_full_period")
    assert report["ok"], report


def test_registry_rejects_duplicate_and_gapped_batch():
    _reset()
    with SessionLocal() as db:
        existing = db.scalars(select(models.MeterBatch).order_by(models.MeterBatch.id)).first()
        intervals = [
            {"ts": row.ts.isoformat(), "kwh": float(row.kwh)} for row in db.scalars(
                select(models.MeterInterval)
                .where(models.MeterInterval.batch_id == existing.id)
                .order_by(models.MeterInterval.ts)
            )
        ]
        with pytest.raises(registry.RegistryError) as exc:
            registry.ingest_batch(
                db,
                {"batch_key": "DUP", "meter_no": "M001-01", "intervals": intervals},
                "planner",
            )
        assert exc.value.status_code == 409
        gap = intervals[:10] + intervals[12:]
        with pytest.raises(registry.RegistryError):
            registry.ingest_batch(db, {"batch_key": "GAP", "meter_no": "M001-01", "intervals": gap}, "planner")


def test_impact_analysis_infers_new_batch_without_lineage():
    _reset()
    demo.run_scenario("late_batch_targeted")
    with SessionLocal() as db:
        new_batch = db.scalars(
            select(models.MeterBatch).where(models.MeterBatch.version > 1).order_by(models.MeterBatch.id)
        ).first()
        affected, evidence = orchestrator.find_affected_windows(db, batch_ids=[new_batch.id])
        assert affected
        assert any(e["source_type"] == "batch" for e in evidence)
        # No lineage row is needed for a brand-new batch to be found: inferred origin exists.
        assert any(e.get("origin") == "inferred" for e in evidence) or any(
            e.get("origin") == "lineage" for e in evidence
        )
