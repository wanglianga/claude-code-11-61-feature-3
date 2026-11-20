"""Demo / acceptance endpoints: replayable scenarios and reset."""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, require_approver
from ..services import demo

router = APIRouter()
_acceptance_lock = threading.Lock()
_last_report: dict | None = None


class ScenarioRequest(BaseModel):
    key: str | None = None


def _release_request_tx(db: Session) -> None:
    """The demo helpers use their own connections; drop this request's read tx first.

    Leaving an idle-in-transaction session open here would make the helper's
    ``DROP SCHEMA`` wait on our own lock.
    """
    db.rollback()


@router.get("/scenarios", tags=["demo"])
def list_scenarios(_user=Depends(current_user)):
    return {"scenarios": demo.scenarios_meta(), "last_report": _last_report}


@router.post("/scenarios/run", tags=["demo"])
def run_scenario(body: ScenarioRequest, db: Session = Depends(get_db), user=Depends(require_approver)):
    global _last_report
    if not _acceptance_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有验收场景在执行，请稍后重试")
    try:
        _release_request_tx(db)
        if body.key:
            try:
                report = demo.run_scenario(body.key)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"未知场景：{body.key}") from exc
            report["actor"] = user.username
            _last_report = report
            return report
        report = demo.acceptance_report()
        report["actor"] = user.username
        _last_report = report
        return report
    finally:
        _acceptance_lock.release()


@router.post("/reset", tags=["demo"])
def reset(db: Session = Depends(get_db), user=Depends(require_approver)):
    global _last_report
    with _acceptance_lock:
        _release_request_tx(db)
        demo.reset_and_seed()
        _last_report = None
    return {"ok": True, "reset_by": user.username}
