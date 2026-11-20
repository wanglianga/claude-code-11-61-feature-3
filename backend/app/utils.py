"""Shared helpers: interval math, snapshots and serialization."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

INTERVAL_MINUTES = 15


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def combine_utc(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock).replace(tzinfo=timezone.utc)


def event_intervals(event_day: date, start: time, end: time) -> list[datetime]:
    cursor = combine_utc(event_day, start)
    stop = combine_utc(event_day, end)
    out: list[datetime] = []
    step = timedelta(minutes=INTERVAL_MINUTES)
    while cursor < stop:
        out.append(cursor)
        cursor += step
    return out


def baseline_intervals(event_day: date, start: time, end: time, lookback_days: int) -> dict[date, list[datetime]]:
    day_step = timedelta(days=1)
    result: dict[date, list[datetime]] = {}
    for offset in range(1, lookback_days + 1):
        day = event_day - day_step * offset
        result[day] = event_intervals(day, start, end)
    return result


def event_duration_hours(event: object) -> float:
    start = getattr(event, "start_time")
    end = getattr(event, "end_time")
    return (
        (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    ) / 60.0


def checksum_intervals(rows: list[dict]) -> str:
    payload = ";".join(f"{r['ts'].isoformat()}|{r['kwh']:.3f}|{r.get('quality', 'good')}" for r in rows)
    return hashlib.sha256(payload.encode()).hexdigest()


def money(value: Decimal | float | int) -> float:
    return float(round(Decimal(str(value)), 2))


def number(value: Decimal | float | int, places: int = 3) -> float:
    quant = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quant))


def as_float(value) -> float | None:
    return float(value) if value is not None else None


def serialize_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return aware_utc(value).isoformat().replace("+00:00", "Z")
