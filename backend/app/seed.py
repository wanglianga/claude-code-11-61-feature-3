"""Deterministic fictional seed data and replayable demo scenarios."""
from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import models
from .config import settings
from .db import SessionLocal, engine, init_db
from .security import hash_password
from .utils import checksum_intervals

DEMO_PERIOD = "2025-08"
SEED_EPOCH = datetime(2025, 8, 25, 3, 0, tzinfo=timezone.utc)

CUSTOMERS = [
    ("C-001", "滨江化工有限公司", "滨江工业园区", "王工 13800000001", 1, 2.4),
    ("C-002", "临港冷库物流中心", "临港物流园", "李工 13800000002", 1, 1.8),
    ("C-003", "云岭水泥厂", "云岭工业区", "赵工 13800000003", 2, 3.1),
    ("C-004", "绿洲商业综合体", "中心商务区", "陈工 13800000004", 1, 1.2),
    ("C-005", "金泉数据中心", "高新区", "周工 13800000005", 2, 2.0),
    ("C-006", "海通汽车零部件", "海通产业园", "孙工 13800000006", 1, 1.6),
]

EVENTS = [
    ("DR-20250805", "八月第一轮削峰", date(2025, 8, 5), time(18, 0), time(19, 0), 3.2),
    ("DR-20250812", "八月第二轮削峰", date(2025, 8, 12), time(14, 0), time(15, 0), 2.8),
    ("DR-20250820", "八月晚高峰响应", date(2025, 8, 20), time(19, 0), time(20, 30), 3.6),
]

DEMO_SCENARIOS = [
    {
        "key": "baseline_shortfall",
        "title": "基线样本不足 → 下游待核，禁止零值出账",
        "steps": [
            "新增基线规则版本：回看 5 天但要求样本 ≥6 天",
            "执行整期运行",
            "校验全部窗口 baseline=needs_review、response/settlement=blocked",
            "尝试发布账本 → 409 拒绝",
        ],
    },
    {
        "key": "concurrency_claim",
        "title": "补数与关账并发抢同一客户窗口 → 仅一个运行取得快照",
        "steps": [
            "两个运行同时请求同一客户×活动窗口（服务端并发触发）",
            "恰好一个 201 成功、一个 409 冲突",
            "核对 run_window_claims 中该窗口只有一条 active 记录，且最终被释放",
        ],
    },
    {
        "key": "late_batch_targeted",
        "title": "迟到批次 → 定向重算 + 新版本账本 + 调整",
        "steps": [
            "发布基线运行 v1 并记录付款凭证",
            "补传某客户的更正批次（替换活动窗口读数）",
            "列出影响面 → 定向重算，未受影响窗口复用",
            "发布 v2 → 生成待批调整；原凭证保持不变",
        ],
    },
    {
        "key": "late_contract_adjustment",
        "title": "签约修订晚于付款入账 → 关联调整而非改写凭证",
        "steps": [
            "已有 v1 账本与付款凭证",
            "登记新签约版本（缩小容量，降低结算电量）",
            "定向重算并发布 v2",
            "审批调整 → 生成 ADJV 调整凭证，STV 原凭证金额不变",
        ],
    },
    {
        "key": "rule_revision_full_period",
        "title": "规则修订 → 整期不可变重跑",
        "steps": [
            "新增基线规则版本（调整系数变化）",
            "整期重跑并发布",
            "对比两个版本运行，逐窗口核对差异与总差额",
        ],
    },
    {
        "key": "uncertainty_calibration",
        "title": "基线不确定度：伪活动日残差与双方法区间校准",
        "steps": [
            "整期运行后每个窗口生成不确定度报告（每窗口 5 节点）",
            "校验伪活动日残差全部来自非活动日，且目标日及泄漏窗口被排除",
            "留出法与连续日期块重采样均产出预测区间，宽度来源分解齐全",
            "区间包含基线值、相对宽度合理，报告引用同一锁定规则与输入版本",
        ],
    },
    {
        "key": "uncertainty_flat_load",
        "title": "平稳负荷 → 伪活动日残差近零、区间收窄",
        "steps": [
            "登记负荷恒定的示范客户与连续历史批次",
            "运行后读取该客户不确定度报告",
            "校验残差≈0、区间半宽≈0、状态 ok",
        ],
    },
    {
        "key": "uncertainty_holiday_exclusion",
        "title": "节假日 → 样本资格排除并保留排除理由",
        "steps": [
            "在回看窗口内登记新节假日",
            "重跑后校验该日以 holiday 理由排除",
            "基线样本与伪活动日均不含该日",
        ],
    },
    {
        "key": "uncertainty_missing_weather",
        "title": "天气缺项 → 覆盖诊断降级但区间仍可估计",
        "steps": [
            "云岭工业区（C-003）种子数据无气象记录",
            "报告天气覆盖状态为 missing、覆盖因子放宽",
            "状态 degraded 而非 unavailable，区间仍正常产出",
        ],
    },
    {
        "key": "uncertainty_insufficient_coverage",
        "title": "覆盖不足 → 无法估计但不阻断结算",
        "steps": [
            "登记仅有 4 天计量历史的新客户",
            "基线可算（4≥3）但伪活动日残差 <5 → 报告 unavailable",
            "运行仍成功、账本可发布（不确定度只报告不设卡）",
        ],
    },
    {
        "key": "uncertainty_event_contamination",
        "title": "夹杂活动日 → 活动日排除且从不作误差标签",
        "steps": [
            "在 8/20 活动回看窗口内登记新活动日 8/18",
            "8/18 以 event_day 理由排除，不进入基线样本与伪活动日",
            "全部伪活动日残差均来自非活动日",
        ],
    },
]


def wait_for_db(retries: int = 30) -> None:
    import time as _time

    for attempt in range(retries):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception:  # noqa: BLE001
            _time.sleep(1.0)
    raise RuntimeError("数据库连接不可用")


def _load_profile(customer_index: int, day: date, base_kw: float) -> list[float]:
    """Deterministic synthetic load: 15-minute kWh per meter, day-long."""
    rng = random.Random(f"{customer_index}-{day.isoformat()}")
    out = []
    for i in range(96):
        hour = i / 4.0
        shape = 0.62 + 0.30 * math.sin((hour - 8) / 24 * 2 * math.pi) + 0.18 * math.sin(hour / 24 * 4 * math.pi)
        noise = rng.uniform(-0.035, 0.035)
        out.append(round(max(0.02, base_kw * (shape + noise) * 0.25), 3))
    return out


def reset_database() -> None:
    from .db import drop_schema

    drop_schema()
    init_db()


def ensure_seed() -> None:
    if not settings.auto_seed:
        return
    with SessionLocal() as db:
        if (db.scalar(select(func.count()).select_from(models.Customer)) or 0) > 0:
            return
        seed(db)


def seed(db: Session) -> None:
    users = [
        ("planner", "结算计划员", "planner", "planner123"),
        ("approver", "结算审批员", "approver", "approver123"),
        ("viewer", "只读审计员", "viewer", "viewer123"),
    ]
    for username, display, role, password in users:
        if not db.scalars(select(models.User).where(models.User.username == username)).first():
            db.add(models.User(username=username, display_name=display, role=role, password_hash=hash_password(password)))

    customers: list[models.Customer] = []
    meters: list[models.MeterPoint] = []
    for idx, (code, name, region, contact, meter_count, base_kw) in enumerate(CUSTOMERS):
        customer = models.Customer(code=code, name=name, region=region, contact=contact)
        db.add(customer)
        db.flush()
        customers.append(customer)
        db.add(
            models.ContractVersion(
                customer_id=customer.id,
                version=1,
                capacity_kw=round(base_kw * 1000, 3),
                price_ceiling=4.0,
                effective_from=date(2025, 1, 1),
                reason="2025 年度需求响应签约",
                created_by="system",
                created_at=SEED_EPOCH - timedelta(days=200),
            )
        )
        for m in range(meter_count):
            meter = models.MeterPoint(
                customer_id=customer.id,
                meter_no=f"M{code[2:]}-{m + 1:02d}",
                name=f"{name} 计量点 {m + 1}",
            )
            db.add(meter)
            db.flush()
            meters.append(meter)
        customer._base_kw = base_kw  # type: ignore[attr-defined]

    db.add(
        models.BaselineRuleVersion(
            rule_code="BL-DEFAULT",
            version=1,
            name="同窗口日均值法 v1",
            method="same-interval-mean",
            lookback_days=5,
            min_sample_days=3,
            adjustment_factor=1.0,
            effective_from=date(2025, 1, 1),
            reason="年度基线规则",
            config={"description": "取活动前 5 个自然日同 15 分钟窗口均值，按调整系数折算"},
            created_by="system",
            created_at=SEED_EPOCH - timedelta(days=150),
        )
    )
    for code, name, day, start, end, price in EVENTS:
        db.add(
            models.Event(
                code=code,
                name=name,
                event_date=day,
                start_time=start,
                end_time=end,
                price_per_kwh=price,
                period_month=DEMO_PERIOD,
            )
        )
    db.flush()

    # ---- 不确定度分析来源：气象（云岭工业区故意缺项）、节假日、停电/限电 ----
    weather_regions = [region for _, _, region, _, _, _ in CUSTOMERS if region != "云岭工业区"]
    weather_start = date(2025, 7, 20)
    for region in dict.fromkeys(weather_regions):
        for i in range((date(2025, 8, 31) - weather_start).days + 1):
            day = weather_start + timedelta(days=i)
            rng = random.Random(f"weather-{region}-{day.isoformat()}")
            avg_temp = round(30.0 + 3.0 * math.sin(i / 8.0) + rng.uniform(-1.5, 1.5), 1)
            db.add(
                models.WeatherDaily(
                    region=region,
                    day=day,
                    version=1,
                    avg_temp_c=avg_temp,
                    max_temp_c=round(avg_temp + 5.5, 1),
                    source_note="虚构气象站日值（演示数据）",
                    created_by="system",
                    created_at=SEED_EPOCH - timedelta(days=2),
                )
            )
    db.add(
        models.Holiday(
            day=date(2025, 8, 11),
            name="全民健身示范假日（虚构数据）",
            created_by="system",
            created_at=SEED_EPOCH - timedelta(days=2),
        )
    )
    c006 = next(c for c in customers if c.code == "C-006")
    db.add(
        models.OutageDay(
            customer_id=c006.id,
            day=date(2025, 8, 18),
            kind="curtailment",
            note="园区计划限电（虚构数据）",
            created_by="system",
            created_at=SEED_EPOCH - timedelta(days=2),
        )
    )
    db.flush()

    base_of_customer = {c.id: c._base_kw for c in customers}  # type: ignore[attr-defined]
    event_days = {e[2]: e for e in EVENTS}
    start_day = date(2025, 7, 25)
    last_day = date(2025, 8, 22)

    def build_profiles(day: date) -> dict[int, list[float]]:
        profiles: dict[int, list[float]] = {}
        for idx, customer in enumerate(customers):
            profile = _load_profile(idx, day, base_of_customer[customer.id] * 1000)
            event = event_days.get(day)
            if event is not None:
                response_ratio = 0.18 + 0.06 * (idx % 3)
                start_hour = event[3].hour
                end_hour = event[4].hour + (0.5 if event[4].minute else 0)
                for i in range(96):
                    hour = i / 4.0
                    if start_hour <= hour < end_hour:
                        profile[i] = round(profile[i] * (1 - response_ratio), 3)
            profiles[customer.id] = profile
        return profiles

    day = start_day
    while day <= last_day:
        profiles = build_profiles(day)
        for meter in meters:
            customer = next(c for c in customers if c.id == meter.customer_id)
            share = 0.55 if meter.meter_no.endswith("01") else 0.45
            start_dt = datetime.combine(day, time(0, 0), tzinfo=timezone.utc)
            ts_rows = [
                {
                    "ts": start_dt + timedelta(minutes=15 * i),
                    "kwh": round(kwh * share, 3),
                    "quality": "good",
                }
                for i, kwh in enumerate(profiles[customer.id])
            ]
            batch = models.MeterBatch(
                batch_key=f"SEED-{meter.meter_no}-{day.isoformat()}",
                meter_point_id=meter.id,
                version=1,
                coverage_start=start_dt,
                coverage_end=start_dt + timedelta(days=1),
                submitted_at=SEED_EPOCH - timedelta(days=(last_day - day).days),
                checksum=checksum_intervals(ts_rows),
                status="accepted",
                interval_count=96,
                total_kwh=round(sum(r["kwh"] for r in ts_rows), 3),
                source_note="首次日冻结批次",
                created_by="system",
            )
            db.add(batch)
            db.flush()
            db.add_all(
                [
                    models.MeterInterval(
                        batch_id=batch.id,
                        meter_point_id=meter.id,
                        ts=row["ts"],
                        kwh=row["kwh"],
                        quality=row["quality"],
                    )
                    for row in ts_rows
                ]
            )
        db.commit()
        day += timedelta(days=1)

    db.commit()


def demo_state() -> dict:
    with SessionLocal() as db:
        return {
            "period_month": DEMO_PERIOD,
            "customers": db.scalar(select(func.count()).select_from(models.Customer)) or 0,
            "events": db.scalar(select(func.count()).select_from(models.Event)) or 0,
            "batches": db.scalar(select(func.count()).select_from(models.MeterBatch)) or 0,
            "weather_rows": db.scalar(select(func.count()).select_from(models.WeatherDaily)) or 0,
            "holidays": db.scalar(select(func.count()).select_from(models.Holiday)) or 0,
            "outage_days": db.scalar(select(func.count()).select_from(models.OutageDay)) or 0,
            "runs": db.scalar(select(func.count()).select_from(models.Run)) or 0,
            "ledger_entries": db.scalar(select(func.count()).select_from(models.LedgerEntry)) or 0,
            "adjustments": db.scalar(select(func.count()).select_from(models.Adjustment)) or 0,
            "users": ["planner/planner123", "approver/approver123", "viewer/viewer123"],
        }
