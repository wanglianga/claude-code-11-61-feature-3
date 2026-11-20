"""Pydantic request/response bodies for the documented API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]


class IntervalIn(BaseModel):
    ts: datetime = Field(description="15 分钟区间起点（ISO8601，UTC）")
    kwh: float = Field(ge=0)
    quality: str = "good"


class BatchIn(BaseModel):
    batch_key: str = Field(description="幂等键；相同 key 重复提交返回原批次")
    meter_no: str
    intervals: list[IntervalIn]
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    source_note: str = ""


class RuleIn(BaseModel):
    rule_code: str = "BL-DEFAULT"
    name: str | None = None
    method: str = "same-interval-mean"
    lookback_days: int = Field(default=5, ge=1, le=30)
    min_sample_days: int = Field(default=3, ge=1, le=30)
    adjustment_factor: float = Field(default=1.0, ge=0.1, le=3.0)
    effective_from: date
    effective_to: date | None = None
    reason: str = "规则修订"
    config: dict[str, Any] = Field(default_factory=dict)


class ContractIn(BaseModel):
    customer_id: int
    capacity_kw: float = Field(gt=0)
    price_ceiling: float | None = None
    effective_from: date
    reason: str = "签约容量修订"


class RunCreateIn(BaseModel):
    period_month: str = Field(pattern=r"^\d{4}-\d{2}$")
    strategy: Literal["initial", "targeted", "full_period"] = "initial"
    event_id: int | None = None
    batch_ids: list[int] | None = None
    contract_ids: list[int] | None = None
    rule_ids: list[int] | None = None
    notes: str = ""
    debug_hold_seconds: float = Field(default=0.0, ge=0, le=15, description="演示用：取得窗口锁后保持 N 秒")

    model_config = {"json_schema_extra": {"examples": [{"period_month": "2025-08", "strategy": "initial"}]}}


class PublishIn(BaseModel):
    reason: str = ""


class PaymentIn(BaseModel):
    reference: str = Field(description="付款凭证号，唯一")
    paid_at: datetime | None = None


class AdjustDecisionIn(BaseModel):
    approve: bool
    comment: str = ""


class RuleValidateIn(BaseModel):
    event_id: int
    lookback_days: int = 5
    min_sample_days: int = 3


class WeatherDailyIn(BaseModel):
    region: str
    day: date
    avg_temp_c: float = Field(ge=-50, le=60)
    max_temp_c: float | None = Field(default=None, ge=-50, le=60)
    source_note: str = ""


class HolidayIn(BaseModel):
    day: date
    name: str


class OutageIn(BaseModel):
    customer_id: int
    day: date
    kind: Literal["outage", "curtailment"] = Field(description="outage 停电 / curtailment 限电")
    note: str = ""


class DemoResetIn(BaseModel):
    confirm: bool = False
