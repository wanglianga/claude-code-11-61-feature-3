"""Request dependencies: bearer auth and role guards."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from . import models
from .db import get_db
from .security import decode_token

ROLE_RANK = {"viewer": 1, "planner": 2, "approver": 3}


def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> models.User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少访问令牌，请先登录")
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    if payload is None:
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期")
    user = db.get(models.User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


def require_planner(user: models.User = Depends(current_user)) -> models.User:
    if ROLE_RANK.get(user.role, 0) < ROLE_RANK["planner"]:
        raise HTTPException(status_code=403, detail="需要计划员或审批员角色")
    return user


def require_approver(user: models.User = Depends(current_user)) -> models.User:
    if user.role != "approver":
        raise HTTPException(status_code=403, detail="需要审批员角色")
    return user


def audit(db: Session, actor: str, action: str, entity_type: str, entity_id, detail: dict | None = None) -> None:
    db.add(
        models.AuditLog(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            detail=detail or {},
        )
    )
