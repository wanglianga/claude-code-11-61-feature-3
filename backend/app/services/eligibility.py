"""Shared day-eligibility gate for baseline samples and uncertainty calibration.

One rule, two consumers:

* the **baseline node** uses it to decide which look-back days enter the
  same-interval mean, and persists the per-day verdict (with exclusion
  reasons) in its node output;
* the **uncertainty analysis** uses it to pick qualified pseudo-event days —
  days with no demand response, no outage/curtailment and qualified metering
  coverage — whose actual load may act as an error label.  Actual event-day
  load is *never* used as an error label because it already contains
  curtailment.

Everything is resolved against the run's frozen cut-off, so a run always sees
the same locked inputs as the settlement graph it belongs to.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..utils import aware_utc, event_intervals

# Exclusion reason codes -> Chinese label (kept stable for the UI and tests).
REASON_LABELS = {
    "event_day": "需求响应活动日",
    "outage": "停电日",
    "curtailment": "限电日",
    "holiday": "节假日",
    "meter_coverage_gap": "计量覆盖不全",
    "meter_quality": "计量质量不合格",
    "nonpositive_load": "窗口电量非正",
}

# Interval quality values that disqualify a day (anything else, e.g.
# "estimated", is tolerated but could be surfaced later).
BAD_QUALITY = {"bad", "missing", "invalid"}


@dataclass
class DayWindow:
    """Metering view of one calendar day's event window."""

    totals: list[float]  # summed kWh across meters per 15-min slot
    batch_ids: list[int]
    qualities: set[str]

    @property
    def total_kwh(self) -> float:
        return sum(self.totals)


@dataclass
class DayEligibility:
    day: date
    weekday: int
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    window: DayWindow | None = None
    temp_c: float | None = None
    max_temp_c: float | None = None

    @property
    def reason_labels(self) -> list[str]:
        return [REASON_LABELS.get(r, r) for r in self.reasons]

    @property
    def day_total_kwh(self) -> float | None:
        return round(self.window.total_kwh, 3) if self.window else None

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "weekday": self.weekday,
            "eligible": self.eligible,
            "reasons": self.reasons,
            "reason_labels": self.reason_labels,
            "day_total_kwh": self.day_total_kwh,
            "temp_c": self.temp_c,
            "batch_ids": list(self.window.batch_ids) if self.window else [],
        }


class RefData:
    """Reference data shared by every window of a run, frozen at ``cutoff``."""

    def __init__(
        self,
        event_days: dict[date, list[int]],
        holidays: dict[date, models.Holiday],
        outages: dict[int, dict[date, list[models.OutageDay]]],
        weather: dict[str, dict[date, models.WeatherDaily]],
    ) -> None:
        self.event_days = event_days
        self.holidays = holidays
        self.outages = outages
        self.weather = weather

    @classmethod
    def load(cls, db: Session, cutoff: datetime) -> "RefData":
        event_days: dict[date, list[int]] = defaultdict(list)
        for ev in db.scalars(select(models.Event)).all():
            event_days[ev.event_date].append(ev.id)

        holidays = {
            h.day: h
            for h in db.scalars(
                select(models.Holiday).where(models.Holiday.created_at <= cutoff)
            ).all()
        }

        outages: dict[int, dict[date, list[models.OutageDay]]] = defaultdict(lambda: defaultdict(list))
        for row in db.scalars(
            select(models.OutageDay).where(models.OutageDay.created_at <= cutoff)
        ).all():
            outages[row.customer_id][row.day].append(row)

        # Latest version per (region, day) submitted at or before the cut-off.
        weather: dict[str, dict[date, models.WeatherDaily]] = defaultdict(dict)
        rows = db.scalars(
            select(models.WeatherDaily)
            .where(models.WeatherDaily.created_at <= cutoff)
            .order_by(models.WeatherDaily.region, models.WeatherDaily.day, models.WeatherDaily.version)
        ).all()
        for row in rows:
            weather[row.region][row.day] = row  # ascending version -> last write wins

        return cls(dict(event_days), holidays, {k: dict(v) for k, v in outages.items()}, {k: dict(v) for k, v in weather.items()})

    def weather_ids(self) -> list[int]:
        return sorted(w.id for days in self.weather.values() for w in days.values())

    def holiday_ids(self) -> list[int]:
        return sorted(h.id for h in self.holidays.values())

    def outage_ids(self) -> list[int]:
        return sorted(o.id for days in self.outages.values() for rows in days.values() for o in rows)


class CustomerHistory:
    """A customer's accepted batches (submitted <= cutoff) with intervals.

    Replicates :func:`snapshot.select_batches` semantics in memory so the
    baseline gate and the pseudo-event recomputation read exactly the same
    locked input versions without re-querying per day.
    """

    def __init__(self, meter_points: list[models.MeterPoint], cutoff: datetime) -> None:
        self.cutoff = cutoff
        self.meter_ids = [m.id for m in meter_points]
        self.meter_count = len(meter_points)
        self._batches: dict[int, list[models.MeterBatch]] = {}
        self._intervals: dict[int, dict[datetime, tuple[float, str]]] = {}
        self._window_cache: dict[tuple[date, time, time], DayWindow | None] = {}

    @classmethod
    def load(cls, db: Session, meter_points: list[models.MeterPoint], cutoff: datetime) -> "CustomerHistory":
        hist = cls(meter_points, cutoff)
        if not hist.meter_ids:
            return hist
        batches = db.scalars(
            select(models.MeterBatch)
            .where(
                models.MeterBatch.meter_point_id.in_(hist.meter_ids),
                models.MeterBatch.status == "accepted",
                models.MeterBatch.submitted_at <= cutoff,
            )
            .order_by(models.MeterBatch.meter_point_id, models.MeterBatch.version, models.MeterBatch.submitted_at)
        ).all()
        for batch in batches:
            hist._batches.setdefault(batch.meter_point_id, []).append(batch)
        batch_ids = [b.id for b in batches]
        if batch_ids:
            rows = db.scalars(
                select(models.MeterInterval)
                .where(models.MeterInterval.batch_id.in_(batch_ids))
                .order_by(models.MeterInterval.batch_id, models.MeterInterval.ts)
            ).all()
            for row in rows:
                hist._intervals.setdefault(row.batch_id, {})[aware_utc(row.ts)] = (
                    float(row.kwh),
                    row.quality or "good",
                )
        return hist

    def _batch_for(self, meter_id: int, start: datetime, end: datetime) -> models.MeterBatch | None:
        """Latest accepted version whose coverage fully spans [start, end]."""
        chosen = None
        for batch in self._batches.get(meter_id, []):
            if aware_utc(batch.coverage_start) <= start and aware_utc(batch.coverage_end) >= end:
                chosen = batch  # ordered by (version, submitted_at) ascending
        return chosen

    def day_window(self, day: date, start: time, end: time) -> DayWindow | None:
        """Per-slot totals across all meters, or None when coverage is incomplete."""
        key = (day, start, end)
        if key in self._window_cache:
            return self._window_cache[key]
        slots = event_intervals(day, start, end)
        result: DayWindow | None = None
        if slots and self.meter_ids:
            win_start, win_end = slots[0], slots[-1] + timedelta(minutes=15)
            per_meter: list[dict[datetime, tuple[float, str]]] = []
            batch_ids: list[int] = []
            for meter_id in self.meter_ids:
                batch = self._batch_for(meter_id, win_start, win_end)
                if batch is None:
                    per_meter = []
                    batch_ids = []
                    break
                series = self._intervals.get(batch.id, {})
                if any(ts not in series for ts in slots):
                    per_meter = []
                    batch_ids = []
                    break
                per_meter.append(series)
                batch_ids.append(batch.id)
            if per_meter:
                totals = [round(sum(series[ts][0] for series in per_meter), 3) for ts in slots]
                qualities = {series[ts][1] for series in per_meter for ts in slots}
                result = DayWindow(totals=totals, batch_ids=batch_ids, qualities=qualities)
        self._window_cache[key] = result
        return result


def evaluate_day(
    day: date,
    *,
    customer: models.Customer,
    history: CustomerHistory,
    ref: RefData,
    start: time,
    end: time,
) -> DayEligibility:
    """Apply the shared eligibility gate to one calendar day."""
    reasons: list[str] = []
    if day in ref.event_days:
        reasons.append("event_day")
    for outage in ref.outages.get(customer.id, {}).get(day, []):
        kind = outage.kind if outage.kind in REASON_LABELS else "outage"
        if kind not in reasons:
            reasons.append(kind)
    if day in ref.holidays:
        reasons.append("holiday")

    window = history.day_window(day, start, end)
    if window is None:
        reasons.append("meter_coverage_gap")
    else:
        if window.qualities & BAD_QUALITY:
            reasons.append("meter_quality")
        if window.total_kwh <= 0:
            reasons.append("nonpositive_load")

    weather_row = ref.weather.get(customer.region, {}).get(day)
    return DayEligibility(
        day=day,
        weekday=day.weekday(),
        eligible=not reasons,
        reasons=reasons,
        window=window,
        temp_c=float(weather_row.avg_temp_c) if weather_row else None,
        max_temp_c=float(weather_row.max_temp_c) if weather_row and weather_row.max_temp_c is not None else None,
    )


def eligibility_map(
    days: Iterable[date],
    *,
    customer: models.Customer,
    history: CustomerHistory,
    ref: RefData,
    start: time,
    end: time,
) -> dict[date, DayEligibility]:
    return {
        day: evaluate_day(day, customer=customer, history=history, ref=ref, start=start, end=end)
        for day in days
    }
