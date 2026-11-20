"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .db import SessionLocal, init_db
from .routers import demo_api, registry_api, runs_api, uncertainty_api
from .services.orchestrator import OrchestrationError

log = logging.getLogger("drs")

OPENAPI_TAGS = [
    {"name": "auth", "description": "登录与当前用户"},
    {"name": "registry", "description": "来源登记：批次、基线规则版本、签约版本、气象、节假日、停电/限电、客户与活动"},
    {"name": "lineage", "description": "血缘与影响面：从变更来源追踪到客户/活动窗口"},
    {"name": "runs", "description": "运行编排：首次运行、定向重算、整期不可变重跑"},
    {"name": "uncertainty", "description": "基线不确定度：伪活动日残差、预测区间、覆盖与校准诊断"},
    {"name": "publish", "description": "账本发布（仅接收完整成功的运行）"},
    {"name": "ledger", "description": "已发布账本、付款凭证与调整凭证"},
    {"name": "adjustments", "description": "差额调整与从调整追溯到来源"},
    {"name": "dashboard", "description": "看板与审计"},
    {"name": "demo", "description": "可回放虚构数据与联调验收场景"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from .seed import ensure_seed, wait_for_db

    wait_for_db()
    ensure_seed()
    from .db import SessionLocal as _Session
    from .services.orchestrator import release_abandoned_claims

    with _Session() as db:
        released = release_abandoned_claims(db)
    if released:
        log.warning("released %s abandoned window claims on startup", released)
    log.info("database ready")
    yield


app = FastAPI(
    title="需求响应结算重算编排平台 API",
    version="1.0.0",
    description=(
        "省级负荷聚合商客户级需求响应结算重算平台。\n\n"
        "核心约束：\n"
        "- 来源登记保留批次与适用版本，历史版本不可改写；\n"
        "- 每次运行冻结输入截止批次、规则快照与签约快照；\n"
        "- 账本发布只接收完整成功的运行，已发布账本只追加调整不覆写；\n"
        "- 同一客户窗口同一时刻仅允许一个运行取得输入快照；\n"
        "- 基线失败或样本不足时下游停为待核，不以零值结算。"
    ),
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(registry_api.router, prefix="/api")
app.include_router(runs_api.router, prefix="/api")
app.include_router(uncertainty_api.router, prefix="/api")
app.include_router(demo_api.router, prefix="/api/demo")


@app.exception_handler(OrchestrationError)
async def orchestration_handler(_request: Request, exc: OrchestrationError):
    detail = exc.message if exc.detail is None else {"message": exc.message, "conflicts": exc.detail}
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.get("/api/health", tags=["dashboard"])
def health():
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/demo/status", tags=["demo"])
def demo_status():
    from .seed import DEMO_SCENARIOS, demo_state

    return {"scenarios": DEMO_SCENARIOS, **demo_state()}
