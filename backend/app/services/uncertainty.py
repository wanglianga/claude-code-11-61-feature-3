"""Baseline uncertainty analysis: prediction intervals from pseudo-event days.

Methodology
-----------
The error label for the baseline model must never come from an actual demand
-response event day: event-day load already contains curtailment, so using it
would mislabel the "no-response baseline" error.  Instead, every *qualified
non-event day* (no demand response, no outage/curtailment, qualified metering
coverage — the shared gate in :mod:`eligibility`) is treated as a
**pseudo-event day**: the locked baseline rule is re-run for that day with the
target day and its leakage window excluded, and the prediction is compared
with the day's measured load.  The resulting cross-fitted residuals calibrate
the prediction interval of the real event day's baseline.

Two calibration flavours are computed for every window:

* ``holdout`` — leave out a contiguous trailing block of pseudo-event days,
  fit residual quantiles on the remaining cross-fitted residuals, then check
  empirical coverage on the held-out block.  Cheap, but depends on the
  holdout period's weather/season representativeness.
* ``block_bootstrap`` — resample similar non-event days in contiguous date
  blocks (preserving adjacent-day volatility) to probe the stability of the
  interval width.  Unstable when the sample is short or the season drifts.

The analysis reports intervals only: it never rewrites the baseline value and
never claims actual curtailment.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..utils import number
from .eligibility import (
    CustomerHistory,
    DayEligibility,
    RefData,
    eligibility_map,
)

CALIBRATION_LOOKBACK_DAYS = 28
MIN_RESIDUALS = 5
LOW_SAMPLE_RESIDUALS = 8
LEVEL = 0.90
ALPHA = round(1.0 - LEVEL, 10)
BLOCK_LENGTH = 3
BOOTSTRAP_REPLICATES = 200
SIMILAR_TEMP_TOLERANCE_C = 2.5
SEASON_DRIFT_RATIO = 0.15

DISCLAIMER = (
    "区间为基线不确定度报告：不修改基线数值、不宣称实际削减量；"
    "误差校准仅使用无需求响应、无停电/限电且计量覆盖合格的非活动日（伪活动日），"
    "实际活动日负荷从未用作误差标签。"
)

REASON_TEXT = {
    "no_applicable_rule": "活动日无生效基线规则版本",
    "baseline_unavailable": "基线节点未产出数值，无法放置区间",
    "qualified_samples_insufficient": "合格伪活动日样本不足（<5 天）",
    "qualified_samples_low": "合格伪活动日样本偏少（5–7 天）",
    "weather_missing": "天气缺项：活动日或全部样本日无气象数据",
    "weather_partial": "部分样本日缺少气象数据",
    "holdout_not_independent": "样本过少，留出段与拟合段不独立",
    "similarity_pool_fallback": "相似非活动日不足，块重采样回退到全部合格日",
    "blocks_insufficient": "连续日期块过少，块重采样不稳定",
    "season_drift": "校准窗口内负荷存在季节迁移趋势",
}


# ---------------------------------------------------------------------------
# small statistics helpers (no third-party deps)
# ---------------------------------------------------------------------------
def _quantile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    vals = sorted(sorted_vals)
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _pstd(vals: list[float]) -> float | None:
    if not vals:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals))


def _lag1_autocorr(vals: list[float]) -> float | None:
    if len(vals) < 3:
        return None
    mu = sum(vals) / len(vals)
    denom = sum((v - mu) ** 2 for v in vals)
    if denom <= 0:
        return 0.0
    numer = sum((vals[i] - mu) * (vals[i - 1] - mu) for i in range(1, len(vals)))
    return numer / denom


# ---------------------------------------------------------------------------
# pseudo-event day recomputation
# ---------------------------------------------------------------------------
@dataclass
class PseudoDay:
    elig: DayEligibility
    predicted_kwh: float
    actual_kwh: float
    residual_kwh: float
    sample_days: int
    excluded_days: list[str]
    leakage_window: list[str]

    def as_dict(self) -> dict:
        return {
            "day": self.elig.day.isoformat(),
            "weekday": self.elig.weekday,
            "temp_c": self.elig.temp_c,
            "actual_kwh": number(self.actual_kwh),
            "predicted_kwh": number(self.predicted_kwh),
            "residual_kwh": number(self.residual_kwh),
            "sample_days": self.sample_days,
            "excluded_days": self.excluded_days,
            "leakage_window": self.leakage_window,
        }


def _predict_window_total(
    elig_map: dict[date, DayEligibility],
    target: date,
    *,
    lookback: int,
    min_samples: int,
    factor: Decimal,
) -> tuple[float | None, int, list[date]]:
    """Re-run the locked baseline rule for ``target`` with leakage excluded.

    The target day itself and its leakage window (the days whose own
    predictions would consume the target as a sample, i.e. [target, target +
    lookback]) are structurally excluded from the sample pool; with the
    look-back-only rule the target can never enter its own prediction.
    """
    excluded = {target}
    excluded |= {target + timedelta(days=i) for i in range(1, lookback + 1)}
    samples: list[list[float]] = []
    used: list[date] = []
    for offset in range(1, lookback + 1):
        day = target - timedelta(days=offset)
        if day in excluded:
            continue
        elig = elig_map.get(day)
        if elig is None or not elig.eligible or elig.window is None:
            continue
        samples.append(elig.window.totals)
        used.append(day)
    if len(samples) < min_samples:
        return None, len(samples), used
    n_intervals = len(samples[0])
    total = 0.0
    for idx in range(n_intervals):
        mean = Decimal(str(sum(s[idx] for s in samples))) / Decimal(len(samples))
        total += float((mean * factor).quantize(Decimal("0.001")))
    return total, len(samples), used


# ---------------------------------------------------------------------------
# result container
# ---------------------------------------------------------------------------
@dataclass
class UncertaintyResult:
    status: str
    summary: dict[str, Any]
    detail: dict[str, Any]
    row: dict[str, Any]
    sources: list[tuple[str, int, dict]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
def analyze_window(
    db: Session,
    *,
    run: models.Run,
    customer: models.Customer,
    event: models.Event,
    rule: models.BaselineRuleVersion | None,
    meter_points: list[models.MeterPoint],
    cutoff: datetime,
    baseline_kwh: float | None,
    baseline_sample_days: int,
    cache: dict | None = None,
) -> UncertaintyResult:
    """Estimate the prediction interval for one customer/event window."""
    cache = cache if cache is not None else {}
    if ("ref",) not in cache:
        cache[("ref",)] = RefData.load(db, cutoff)
    ref: RefData = cache[("ref",)]
    hkey = ("hist", customer.id)
    if hkey not in cache:
        cache[hkey] = CustomerHistory.load(db, meter_points, cutoff)
    history: CustomerHistory = cache[hkey]

    lookback = int(rule.lookback_days) if rule else 5
    min_samples = int(rule.min_sample_days) if rule else 3
    factor = Decimal(str(rule.adjustment_factor)) if rule else Decimal("1")

    calib_days = max(CALIBRATION_LOOKBACK_DAYS, lookback)
    first_calib = event.event_date - timedelta(days=calib_days)
    first_sample = event.event_date - timedelta(days=calib_days + lookback)
    all_days = [
        first_sample + timedelta(days=i)
        for i in range((event.event_date - first_sample).days)
    ]
    elig_map = eligibility_map(
        all_days,
        customer=customer,
        history=history,
        ref=ref,
        start=event.start_time,
        end=event.end_time,
    )

    # ---- pseudo-event day residuals (cross-fitted, leakage excluded) -------
    pseudo: list[PseudoDay] = []
    unpredictable = 0
    day = first_calib
    while day < event.event_date:
        elig = elig_map[day]
        if elig.eligible:
            predicted, n_samples, _used = _predict_window_total(
                elig_map, day, lookback=lookback, min_samples=min_samples, factor=factor
            )
            if predicted is None:
                unpredictable += 1
            else:
                actual = elig.window.total_kwh
                pseudo.append(
                    PseudoDay(
                        elig=elig,
                        predicted_kwh=predicted,
                        actual_kwh=actual,
                        residual_kwh=actual - predicted,
                        sample_days=n_samples,
                        excluded_days=[day.isoformat()],
                        leakage_window=[
                            (day + timedelta(days=i)).isoformat() for i in range(0, lookback + 1)
                        ],
                    )
                )
        day += timedelta(days=1)

    residuals = [p.residual_kwh for p in pseudo]
    n_res = len(residuals)
    qualified_days = sum(
        1 for d, e in elig_map.items() if first_calib <= d < event.event_date and e.eligible
    )

    # ---- coverage diagnostics (baseline's own sample window) ---------------
    baseline_window_days = [
        event.event_date - timedelta(days=i) for i in range(1, lookback + 1)
    ]
    baseline_samples = [elig_map[d] for d in baseline_window_days if elig_map[d].eligible]
    sample_weekdays = sorted({e.weekday for e in baseline_samples})
    event_weekday = event.event_date.weekday()
    weekday_covered = event_weekday in sample_weekdays

    sample_temps = [e.temp_c for e in baseline_samples if e.temp_c is not None]
    event_weather = ref.weather.get(customer.region, {}).get(event.event_date)
    event_temp = float(event_weather.avg_temp_c) if event_weather else None
    any_missing_weather = any(e.temp_c is None for e in baseline_samples) or event_temp is None
    if event_temp is None or not sample_temps:
        weather_status = "missing"
        weather_factor = 1.25
        event_within_range = None
    else:
        t_min, t_max = min(sample_temps), max(sample_temps)
        if t_min <= event_temp <= t_max:
            weather_factor = 1.0
            event_within_range = True
        elif min(abs(event_temp - t_min), abs(event_temp - t_max)) <= 2.0:
            weather_factor = 1.1
            event_within_range = False
        else:
            weather_factor = 1.2
            event_within_range = False
        weather_status = "partial" if any_missing_weather else "ok"
        if weather_status == "partial":
            weather_factor += 0.05

    weekday_factor = 1.0 if weekday_covered else 1.15
    coverage_factor = round(weekday_factor * weather_factor, 4)
    sample_factor = round(math.sqrt(1 + 1 / max(1, baseline_sample_days)), 4)
    width_factor = round(sample_factor * coverage_factor, 6)

    weather_coverage = {
        "status": weather_status,
        "event_temp_c": event_temp,
        "sample_temp_min_c": min(sample_temps) if sample_temps else None,
        "sample_temp_max_c": max(sample_temps) if sample_temps else None,
        "event_within_sample_range": event_within_range,
        "missing_weather_days": [
            d.isoformat() for d in baseline_window_days if elig_map[d].temp_c is None
        ],
    }
    weekday_coverage = {
        "event_weekday": event_weekday,
        "sample_weekdays": sample_weekdays,
        "event_weekday_in_samples": weekday_covered,
    }

    # ---- method 1: holdout cross-fit ---------------------------------------
    holdout = _calibrate_holdout(pseudo, residuals, width_factor, baseline_kwh, event_temp, ref, customer)
    # ---- method 2: contiguous-block bootstrap ------------------------------
    bootstrap = _calibrate_block_bootstrap(
        pseudo, residuals, width_factor, baseline_kwh, event, event_weekday, event_temp, run, customer
    )

    # ---- status -------------------------------------------------------------
    unavailable_reasons: list[str] = []
    degraded_reasons: list[str] = []
    if rule is None:
        unavailable_reasons.append("no_applicable_rule")
    if n_res < MIN_RESIDUALS:
        unavailable_reasons.append("qualified_samples_insufficient")
    if baseline_kwh is None:
        unavailable_reasons.append("baseline_unavailable")
    if not unavailable_reasons:
        if n_res < LOW_SAMPLE_RESIDUALS:
            degraded_reasons.append("qualified_samples_low")
        if weather_status == "missing":
            degraded_reasons.append("weather_missing")
        elif weather_status == "partial":
            degraded_reasons.append("weather_partial")
        degraded_reasons.extend(holdout.get("warnings", []))
        degraded_reasons.extend(bootstrap.get("warnings", []))
    status = "unavailable" if unavailable_reasons else ("degraded" if degraded_reasons else "ok")

    primary = holdout
    interval = primary["interval"] if status != "unavailable" else None

    # ---- width decomposition -----------------------------------------------
    width_components = {
        "residual_noise_kwh": primary.get("base_half_width_kwh"),
        "sample_size_factor": sample_factor,
        "coverage_factor": coverage_factor,
        "weekday_factor": weekday_factor,
        "weather_factor": weather_factor,
        "final_half_width_kwh": interval["half_width_kwh"] if interval else None,
        "notes": [
            "残差噪声：伪活动日交叉拟合残差的分位数宽度（主要来源）",
            "样本量因子：sqrt(1 + 1/基线样本天数)，样本越少区间越宽",
            "覆盖因子：活动日星期/天气未被样本覆盖时按比例放宽",
        ],
    }

    eligibility_rows = []
    pseudo_by_day = {p.elig.day: p for p in pseudo}
    for d in sorted(elig_map):
        e = elig_map[d]
        row = e.as_dict()
        row["role"] = "calibration" if first_calib <= d < event.event_date else "lookback_only"
        p = pseudo_by_day.get(d)
        if p is not None:
            row["pseudo"] = p.as_dict()
        elif row["role"] == "calibration" and e.eligible:
            row["prediction_unavailable"] = True
        eligibility_rows.append(row)

    detail = {
        "level": LEVEL,
        "status": status,
        "degraded_reasons": degraded_reasons,
        "unavailable_reasons": unavailable_reasons,
        "reason_text": {code: REASON_TEXT.get(code, code) for code in (degraded_reasons + unavailable_reasons)},
        "baseline_kwh": number(baseline_kwh) if baseline_kwh is not None else None,
        "interval": interval,
        "primary_method": "holdout",
        "methods": {"holdout": holdout, "block_bootstrap": bootstrap},
        "samples": {
            "calibration_window": {
                "start": first_calib.isoformat(),
                "end": (event.event_date - timedelta(days=1)).isoformat(),
                "days": calib_days,
            },
            "qualified_days": qualified_days,
            "pseudo_days": n_res,
            "eligible_but_unpredictable_days": unpredictable,
            "min_residuals_required": MIN_RESIDUALS,
            "baseline_sample_days": baseline_sample_days,
            "eligibility": eligibility_rows,
            "weekday_coverage": weekday_coverage,
            "weather_coverage": weather_coverage,
        },
        "width_components": width_components,
        "disclaimer": DISCLAIMER,
    }

    input_refs = {
        "rule_id": rule.id if rule else None,
        "rule_version": rule.version if rule else None,
        "cutoff_at": cutoff.isoformat(),
        "weather_ids": sorted(w.id for w in ref.weather.get(customer.region, {}).values()),
        "holiday_ids": ref.holiday_ids(),
        "outage_ids": sorted(
            o.id for days in ref.outages.get(customer.id, {}).values() for o in days
        ),
        "batch_ids": sorted(
            {bid for e in elig_map.values() if e.window for bid in e.window.batch_ids}
        ),
        "params": {
            "calibration_lookback_days": calib_days,
            "min_residuals": MIN_RESIDUALS,
            "level": LEVEL,
            "block_length": BLOCK_LENGTH,
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "similar_temp_tolerance_c": SIMILAR_TEMP_TOLERANCE_C,
        },
    }

    summary = {
        "status": status,
        "level": LEVEL,
        "baseline_kwh": number(baseline_kwh) if baseline_kwh is not None else None,
        "interval": interval,
        "qualified_days": qualified_days,
        "pseudo_days": n_res,
        "primary_method": "holdout",
        "method_status": {
            "holdout": holdout["status"],
            "block_bootstrap": bootstrap["status"],
        },
        "degraded_reasons": degraded_reasons,
        "unavailable_reasons": unavailable_reasons,
        "disclaimer": "区间仅为基线不确定度报告，不修改基线、不宣称实际削减",
    }

    row = {
        "status": status,
        "primary_method": "holdout",
        "level": LEVEL,
        "baseline_kwh": number(baseline_kwh) if baseline_kwh is not None else None,
        "lower_kwh": interval["lower_kwh"] if interval else None,
        "upper_kwh": interval["upper_kwh"] if interval else None,
        "half_width_kwh": interval["half_width_kwh"] if interval else None,
        "qualified_days": qualified_days,
        "report": detail,
        "input_refs": input_refs,
    }

    sources: list[tuple[str, int, dict]] = []
    if rule is not None:
        sources.append(("rule", rule.id, {"rule_code": rule.rule_code, "version": rule.version}))
    for wid in input_refs["weather_ids"]:
        sources.append(("weather", wid, {"region": customer.region}))
    for hid in input_refs["holiday_ids"]:
        sources.append(("holiday", hid, {}))
    for oid in input_refs["outage_ids"]:
        sources.append(("outage", oid, {"customer_id": customer.id}))

    return UncertaintyResult(status=status, summary=summary, detail=detail, row=row, sources=sources)


# ---------------------------------------------------------------------------
# calibration flavours
# ---------------------------------------------------------------------------
def _interval_dict(
    baseline_kwh: float, q_lo: float, q_hi: float, width_factor: float
) -> dict:
    lower = max(0.0, baseline_kwh + q_lo * width_factor)
    upper = max(lower, baseline_kwh + q_hi * width_factor)
    half = (upper - lower) / 2
    return {
        "lower_kwh": number(lower),
        "upper_kwh": number(upper),
        "half_width_kwh": number(half),
        "relative_width": number(half / baseline_kwh, 4) if baseline_kwh > 0 else None,
    }


def _calibrate_holdout(
    pseudo: list[PseudoDay],
    residuals: list[float],
    width_factor: float,
    baseline_kwh: float | None,
    event_temp: float | None,
    ref: RefData,
    customer: models.Customer,
) -> dict:
    """Leave out a contiguous trailing block; fit quantiles on the rest."""
    out: dict[str, Any] = {"method": "holdout", "status": "unavailable", "warnings": []}
    n = len(residuals)
    if n < MIN_RESIDUALS:
        out["reason"] = "qualified_samples_insufficient"
        return out

    k = min(7, max(3, math.ceil(n / 4)))
    fit, holdout = pseudo[:-k], pseudo[-k:]
    warnings: list[str] = []
    if len(fit) < MIN_RESIDUALS:
        fit, holdout = pseudo, []
        warnings.append("holdout_not_independent")
    fit_residuals = sorted(p.residual_kwh for p in fit)
    q_lo = _quantile(fit_residuals, ALPHA / 2)
    q_hi = _quantile(fit_residuals, 1 - ALPHA / 2)

    holdout_diag: dict[str, Any] = {"days": [], "n": 0}
    if holdout:
        covered = 0
        temps = []
        for p in holdout:
            lo = p.predicted_kwh + q_lo * width_factor
            hi = p.predicted_kwh + q_hi * width_factor
            hit = lo <= p.actual_kwh <= hi
            covered += 1 if hit else 0
            if p.elig.temp_c is not None:
                temps.append(p.elig.temp_c)
            holdout_diag["days"].append(
                {
                    "day": p.elig.day.isoformat(),
                    "actual_kwh": number(p.actual_kwh),
                    "interval": [number(lo), number(hi)],
                    "covered": hit,
                }
            )
        holdout_diag["n"] = len(holdout)
        holdout_diag["empirical_coverage"] = number(covered / len(holdout), 3)
        holdout_diag["target_coverage"] = LEVEL
        holdout_diag["temp_mean_c"] = number(_mean(temps), 2) if temps else None
        holdout_diag["event_temp_c"] = event_temp
        if temps and event_temp is not None:
            holdout_diag["representative"] = abs((_mean(temps) or 0) - event_temp) <= 3.0
        else:
            holdout_diag["representative"] = None

    interval = None
    base_half = None
    if baseline_kwh is not None and q_lo is not None and q_hi is not None:
        interval = _interval_dict(baseline_kwh, q_lo, q_hi, width_factor)
        base_half = number((q_hi - q_lo) / 2)

    out.update(
        {
            "status": "degraded" if warnings else "ok",
            "warnings": warnings,
            "n_residuals": n,
            "fit": {
                "n": len(fit),
                "q_lo_kwh": number(q_lo) if q_lo is not None else None,
                "q_hi_kwh": number(q_hi) if q_hi is not None else None,
                "bias_kwh": number(_mean(fit_residuals)) if fit_residuals else None,
                "std_kwh": number(_pstd(fit_residuals)) if fit_residuals else None,
                "lag1_autocorr": number(_lag1_autocorr([p.residual_kwh for p in fit]), 3)
                if len(fit) >= 3
                else None,
            },
            "holdout": holdout_diag,
            "interval": interval,
            "base_half_width_kwh": base_half,
        }
    )
    return out


def _calibrate_block_bootstrap(
    pseudo: list[PseudoDay],
    residuals: list[float],
    width_factor: float,
    baseline_kwh: float | None,
    event: models.Event,
    event_weekday: int,
    event_temp: float | None,
    run: models.Run,
    customer: models.Customer,
) -> dict:
    """Resample similar non-event days in contiguous date blocks."""
    out: dict[str, Any] = {"method": "block_bootstrap", "status": "unavailable", "warnings": []}
    if len(residuals) < MIN_RESIDUALS:
        out["reason"] = "qualified_samples_insufficient"
        return out

    def similar(p: PseudoDay) -> bool:
        if p.elig.weekday == event_weekday:
            return True
        if p.elig.temp_c is not None and event_temp is not None:
            return abs(p.elig.temp_c - event_temp) <= SIMILAR_TEMP_TOLERANCE_C
        return False

    pool = [p for p in pseudo if similar(p)]
    warnings: list[str] = []
    similarity = "weekday+weather" if event_temp is not None else "weekday_only"
    if len(pool) < 6:
        pool = list(pseudo)
        warnings.append("similarity_pool_fallback")
    if len(pool) < MIN_RESIDUALS:
        out["reason"] = "similar_pool_insufficient"
        out["warnings"] = warnings
        return out

    # Maximal calendar-contiguous runs -> sliding blocks of BLOCK_LENGTH days.
    pool.sort(key=lambda p: p.elig.day)
    runs: list[list[PseudoDay]] = [[pool[0]]]
    for p in pool[1:]:
        if (p.elig.day - runs[-1][-1].elig.day).days == 1:
            runs[-1].append(p)
        else:
            runs.append([p])
    blocks: list[list[PseudoDay]] = []
    for run_days in runs:
        if len(run_days) <= BLOCK_LENGTH:
            blocks.append(run_days)
        else:
            for i in range(0, len(run_days) - BLOCK_LENGTH + 1):
                blocks.append(run_days[i : i + BLOCK_LENGTH])
    if len(blocks) < 3:
        warnings.append("blocks_insufficient")

    # Season drift over the full pseudo-day series.
    drift = _season_drift(pseudo)
    if drift["drift_ratio"] is not None and drift["drift_ratio"] > SEASON_DRIFT_RATIO:
        warnings.append("season_drift")

    rng = random.Random(f"uncertainty:{run.id}:{customer.id}:{event.id}")
    n_target = len(pool)
    half_widths: list[float] = []
    q_los: list[float] = []
    q_his: list[float] = []
    for _ in range(BOOTSTRAP_REPLICATES):
        seq: list[PseudoDay] = []
        while len(seq) < n_target:
            seq.extend(rng.choice(blocks))
        sample_residuals = sorted(p.residual_kwh for p in seq[:n_target])
        q_lo = _quantile(sample_residuals, ALPHA / 2)
        q_hi = _quantile(sample_residuals, 1 - ALPHA / 2)
        q_los.append(q_lo)
        q_his.append(q_hi)
        half_widths.append((q_hi - q_lo) / 2)

    med_lo = _quantile(q_los, 0.5)
    med_hi = _quantile(q_his, 0.5)
    interval = (
        _interval_dict(baseline_kwh, med_lo, med_hi, width_factor)
        if baseline_kwh is not None
        else None
    )
    out.update(
        {
            "status": "degraded" if warnings else "ok",
            "warnings": warnings,
            "pool": {
                "n_days": len(pool),
                "n_blocks": len(blocks),
                "block_length": BLOCK_LENGTH,
                "similarity": similarity,
                "run_lengths": [len(r) for r in runs],
            },
            "replicates": BOOTSTRAP_REPLICATES,
            "half_width_kwh": {
                "p10": number(_quantile(half_widths, 0.1)),
                "p50": number(_quantile(half_widths, 0.5)),
                "p90": number(_quantile(half_widths, 0.9)),
            },
            "season_drift": drift,
            "interval": interval,
        }
    )
    return out


def _season_drift(pseudo: list[PseudoDay]) -> dict:
    """Least-squares slope of daily window totals; large drift destabilises blocks."""
    n = len(pseudo)
    if n < 4:
        return {"slope_kwh_per_day": None, "drift_ratio": None}
    ordered = sorted(pseudo, key=lambda p: p.elig.day)
    ys = [p.actual_kwh for p in ordered]
    mean_y = sum(ys) / n
    if mean_y <= 0:
        return {"slope_kwh_per_day": None, "drift_ratio": None}
    xs = list(range(n))
    mean_x = (n - 1) / 2
    var_x = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)) / var_x if var_x else 0.0
    span_days = (ordered[-1].elig.day - ordered[0].elig.day).days or 1
    drift_ratio = abs(slope) * span_days / mean_y
    return {
        "slope_kwh_per_day": number(slope, 4),
        "drift_ratio": number(drift_ratio, 4),
        "threshold": SEASON_DRIFT_RATIO,
    }
