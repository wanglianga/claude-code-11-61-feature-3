"""Acceptance tests for the baseline-uncertainty analysis feature.

Covers: qualified-sample selection, pseudo-event-day errors (never labelled by
actual event-day load), interval calibration, missing-weather degradation,
insufficient-coverage "unavailable" state, holiday/outage exclusion and
event-day contamination.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app import models
from app.db import SessionLocal
from app.services import demo, orchestrator

PERIOD = "2025-08"


def _reset():
    demo.reset_and_seed()


def _run(**kwargs):
    with SessionLocal() as db:
        return orchestrator.create_run(db, actor="planner", period_month=PERIOD, strategy="initial", **kwargs)


def _report(db, run_id, customer_id, event_id):
    return db.scalars(
        select(models.UncertaintyReport).where(
            models.UncertaintyReport.run_id == run_id,
            models.UncertaintyReport.customer_id == customer_id,
            models.UncertaintyReport.event_id == event_id,
        )
    ).first()


def _node(db, run_id, customer_id, event_id, node_type):
    return db.scalars(
        select(models.Node).where(
            models.Node.run_id == run_id,
            models.Node.customer_id == customer_id,
            models.Node.event_id == event_id,
            models.Node.node_type == node_type,
        )
    ).first()


def test_baseline_records_sample_eligibility_with_reasons():
    _reset()
    run = _run()
    with SessionLocal() as db:
        # 2025-08-11 是种子里的虚构节假日，落在 8/12 活动回看窗口内。
        baseline = _node(db, run.id, 1, 2, "baseline")
        elig = {row["day"]: row for row in baseline.output["sample_eligibility"]}
        assert elig["2025-08-11"]["eligible"] is False
        assert "holiday" in elig["2025-08-11"]["reasons"]
        assert elig["2025-08-11"]["used"] is False
        assert baseline.output["sample_days"] == 4
        # C-006 在 2025-08-18 有限电记录，落在 8/20 活动回看窗口内。
        baseline6 = _node(db, run.id, 6, 3, "baseline")
        elig6 = {row["day"]: row for row in baseline6.output["sample_eligibility"]}
        assert "curtailment" in elig6["2025-08-18"]["reasons"]
        assert elig6["2025-08-18"]["used"] is False
        assert baseline6.output["sample_days"] == 4
        # 无排除项的窗口仍保持 5 天样本。
        baseline_clean = _node(db, run.id, 1, 1, "baseline")
        assert baseline_clean.output["sample_days"] == 5
        assert all(row["used"] for row in baseline_clean.output["sample_eligibility"])


def test_baseline_value_matches_manual_recomputation():
    """重构后的基线门控在干净窗口上必须与手工重算一致。"""
    _reset()
    run = _run()
    with SessionLocal() as db:
        baseline = _node(db, run.id, 1, 1, "baseline")
        # 手工重算：7/31..8/4 五个合格日、活动窗口（18:00-19:00）日总量均值。
        day_totals = []
        for offset in range(1, 6):
            day = date(2025, 8, 5) - timedelta(days=offset)
            start = datetime(day.year, day.month, day.day, 18, 0, tzinfo=timezone.utc)
            end = datetime(day.year, day.month, day.day, 19, 0, tzinfo=timezone.utc)
            total = db.scalar(
                select(func.sum(models.MeterInterval.kwh))
                .join(models.MeterBatch, models.MeterBatch.id == models.MeterInterval.batch_id)
                .join(models.MeterPoint, models.MeterPoint.id == models.MeterBatch.meter_point_id)
                .where(
                    models.MeterPoint.customer_id == 1,
                    models.MeterInterval.ts >= start,
                    models.MeterInterval.ts < end,
                )
            )
            day_totals.append(float(total))
        expected = sum(day_totals) / len(day_totals)
        assert abs(baseline.output["baseline_kwh"] - expected) < 0.01


def test_uncertainty_reports_for_every_window_with_lineage():
    _reset()
    run = _run()
    with SessionLocal() as db:
        reports = db.scalars(
            select(models.UncertaintyReport).where(models.UncertaintyReport.run_id == run.id)
        ).all()
        assert len(reports) == 18
        assert run.node_count == 90
        for report in reports:
            node = _node(db, run.id, report.customer_id, report.event_id, "uncertainty")
            assert node is not None and node.status == "success"
            assert node.output["report_id"] == report.id
            # 血缘：uncertainty 节点引用同一锁定规则版本与气象/节假日来源。
            sources = db.scalars(select(models.NodeSource).where(models.NodeSource.node_id == node.id)).all()
            kinds = {s.source_type for s in sources}
            assert "rule" in kinds
            rule_src = next(s for s in sources if s.source_type == "rule")
            assert rule_src.ref_id == report.input_refs["rule_id"] == 1
            edges = db.scalars(
                select(models.LineageEdge).where(
                    models.LineageEdge.run_id == run.id,
                    models.LineageEdge.to_node_id == node.id,
                )
            ).all()
            assert any(e.relation == "baseline->uncertainty" for e in edges)


def test_pseudo_residuals_never_labelled_by_event_days():
    _reset()
    run = _run()
    with SessionLocal() as db:
        event_days = {e.event_date for e in db.scalars(select(models.Event)).all()}
        reports = db.scalars(
            select(models.UncertaintyReport).where(models.UncertaintyReport.run_id == run.id)
        ).all()
        pseudo_total = 0
        for report in reports:
            rows = report.report["samples"]["eligibility"]
            for row in rows:
                pseudo = row.get("pseudo")
                if pseudo is None:
                    continue
                pseudo_total += 1
                day = date.fromisoformat(row["day"])
                # 实际响应活动日负荷不得作为误差标签。
                assert day not in event_days
                # 目标日及泄漏窗口被排除。
                assert row["day"] in pseudo["excluded_days"]
                assert len(pseudo["leakage_window"]) >= 1
                # 残差定义：实测 - 预测。
                assert abs(pseudo["actual_kwh"] - pseudo["predicted_kwh"] - pseudo["residual_kwh"]) < 0.01
        assert pseudo_total > 0


def test_interval_calibration_structure_and_methods():
    _reset()
    run = _run()
    with SessionLocal() as db:
        reports = db.scalars(
            select(models.UncertaintyReport).where(models.UncertaintyReport.run_id == run.id)
        ).all()
        for report in reports:
            detail = report.report
            assert detail["disclaimer"]
            interval = detail["interval"]
            assert interval["lower_kwh"] <= report.baseline_kwh <= interval["upper_kwh"]
            assert 0 < interval["relative_width"] < 0.5
            # 宽度来源分解：最终半宽 = 残差噪声 × 样本因子 × 覆盖因子。
            width = detail["width_components"]
            assert width["residual_noise_kwh"] > 0
            expected = width["residual_noise_kwh"] * width["sample_size_factor"] * width["coverage_factor"]
            assert abs(width["final_half_width_kwh"] - expected) < max(0.02, 0.02 * expected)
            # 双方法校准。
            holdout = detail["methods"]["holdout"]
            bootstrap = detail["methods"]["block_bootstrap"]
            assert holdout["interval"] is not None
            assert bootstrap["interval"] is not None
            assert bootstrap["replicates"] == 200
            assert bootstrap["half_width_kwh"]["p10"] <= bootstrap["half_width_kwh"]["p90"]
            coverage = holdout["holdout"].get("empirical_coverage")
            if coverage is not None:
                assert 0.0 <= coverage <= 1.0
            # 样本资格表覆盖整个校准窗口，且逐日保留排除理由。
            samples = detail["samples"]
            assert samples["qualified_days"] >= samples["pseudo_days"] >= 5
            assert len(samples["eligibility"]) >= samples["calibration_window"]["days"]
            weekday = samples["weekday_coverage"]
            assert weekday["event_weekday"] in range(7)


def test_missing_weather_degrades_but_estimates():
    _reset()
    report = demo.run_scenario("uncertainty_missing_weather")
    assert report["ok"], report


def test_flat_load_yields_narrow_interval():
    _reset()
    report = demo.run_scenario("uncertainty_flat_load")
    assert report["ok"], report


def test_holiday_excluded_from_samples_and_pseudo_days():
    _reset()
    report = demo.run_scenario("uncertainty_holiday_exclusion")
    assert report["ok"], report


def test_insufficient_coverage_unavailable_but_settles():
    _reset()
    report = demo.run_scenario("uncertainty_insufficient_coverage")
    assert report["ok"], report
    with SessionLocal() as db:
        customer = db.scalars(select(models.Customer).where(models.Customer.code == "C-902")).one()
        event = db.scalars(select(models.Event).where(models.Event.code == "DR-20250820")).one()
        run = db.scalars(select(models.Run).order_by(models.Run.id.desc())).first()
        report_row = _report(db, run.id, customer.id, event.id)
        assert report_row.status == "unavailable"
        assert report_row.lower_kwh is None and report_row.upper_kwh is None
        # 无法估计不出现在账本链路之外：运行成功且账本已发布。
        assert run.status == "success" and run.published_at is not None


def test_event_day_contamination_excluded():
    _reset()
    report = demo.run_scenario("uncertainty_event_contamination")
    assert report["ok"], report


def test_calibration_scenario_end_to_end():
    _reset()
    report = demo.run_scenario("uncertainty_calibration")
    assert report["ok"], report


def test_targeted_recompute_clones_uncertainty_report():
    _reset()
    report = demo.run_scenario("late_batch_targeted")
    assert report["ok"], report
    with SessionLocal() as db:
        targeted = db.scalars(
            select(models.Run).where(models.Run.strategy == "targeted").order_by(models.Run.id.desc())
        ).first()
        reports = db.scalars(
            select(models.UncertaintyReport).where(models.UncertaintyReport.run_id == targeted.id)
        ).all()
        assert len(reports) == 18
        reused = [r for r in reports if r.reused_from_report_id is not None]
        assert len(reused) == targeted.reused_windows
        # 复用报告与来源报告内容一致（输入未变）。
        for r in reused:
            src = db.get(models.UncertaintyReport, r.reused_from_report_id)
            assert src is not None
            assert r.status == src.status
            assert r.lower_kwh == src.lower_kwh and r.upper_kwh == src.upper_kwh


def test_uncertainty_api_list_and_detail():
    _reset()
    from fastapi.testclient import TestClient

    from app.main import app

    run = _run()
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "viewer", "password": "viewer123"})
        assert login.status_code == 200
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        listed = client.get(f"/api/runs/{run.id}/uncertainty", headers=headers)
        assert listed.status_code == 200
        payload = listed.json()
        assert len(payload["reports"]) == 18
        first = payload["reports"][0]
        assert first["baseline_kwh"] is not None
        assert first["status"] in {"ok", "degraded", "unavailable"}

        detail = client.get(f"/api/uncertainty/{first['id']}", headers=headers)
        assert detail.status_code == 200
        body = detail.json()
        assert body["report"]["samples"]["eligibility"]
        assert body["report"]["width_components"]["residual_noise_kwh"] > 0
        assert body["input_refs"]["rule_version"] == 1
        assert body["run_rule_snapshot"]

        filtered = client.get("/api/uncertainty", params={"run_id": run.id, "status": "ok"}, headers=headers)
        assert filtered.status_code == 200
        assert all(r["status"] == "ok" for r in filtered.json())
