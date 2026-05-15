"""신청서 처리 담당(handler) claim/release.

본인만 본인으로 잡고 해제할 수 있다. 이미 다른 사람이 잡았으면 409.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path

from src.auth import require_auth_full
from src.handler_store import get_handler_store, handler_store_available

router = APIRouter(prefix="/api/applications", tags=["handlers"])
logger = logging.getLogger(__name__)


def _require_store() -> Any:
    if not handler_store_available():
        raise HTTPException(status_code=503, detail="handler store 미설정")
    return get_handler_store()


@router.post("/{sid}/handler/claim")
async def claim_handler(
    sid: str = Path(...),
    user: dict = Depends(require_auth_full),
) -> dict[str, Any]:
    store = _require_store()
    try:
        outcome, row = await store.claim(
            application_sid=sid,
            handler_email=user["email"],
            handler_name=user.get("name") or None,
        )
    except Exception:
        logger.exception("claim_handler failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="handler store query failed")

    if outcome == "exists" and row.get("email") != user["email"]:
        # 다른 사람이 이미 잡음
        raise HTTPException(
            status_code=409,
            detail={"reason": "already_claimed", "handler": row},
        )
    return {"claimed": outcome == "claimed", "handler": row}


@router.post("/{sid}/handler/release")
async def release_handler(
    sid: str = Path(...),
    user: dict = Depends(require_auth_full),
) -> dict[str, Any]:
    store = _require_store()
    try:
        ok = await store.release(application_sid=sid, handler_email=user["email"])
    except Exception:
        logger.exception("release_handler failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="handler store query failed")
    if not ok:
        # 본인 아님 또는 row 없음 — 현 상태 조회해서 정확한 응답
        current = await store.get(sid)
        if current is None:
            return {"released": False, "reason": "not_claimed"}
        raise HTTPException(
            status_code=403,
            detail={"reason": "not_owner", "handler": current},
        )
    return {"released": True}


@router.get("/{sid}/handler")
async def get_handler(sid: str = Path(...)) -> dict[str, Any]:
    store = _require_store()
    try:
        row = await store.get(sid)
    except Exception:
        logger.exception("get_handler failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="handler store query failed")
    return {"sid": sid, "handler": row}
