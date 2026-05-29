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


def _normalize_sid(sid: str) -> str:
    """client는 'SID-{uuid}' 형태로 호출. PK는 raw uuid로 통일.
    list_applications도 raw uuid로 handler_map 조회하므로 여기서 정규화 필수.
    """
    return sid[4:] if sid.startswith("SID-") else sid


@router.post("/{sid}/handler/claim")
async def claim_handler(
    sid: str = Path(...),
    user: dict = Depends(require_auth_full),
) -> dict[str, Any]:
    store = _require_store()
    raw_sid = _normalize_sid(sid)
    try:
        outcome, row = await store.claim(
            application_sid=raw_sid,
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
    raw_sid = _normalize_sid(sid)
    try:
        ok = await store.release(application_sid=raw_sid, handler_email=user["email"])
    except Exception:
        logger.exception("release_handler failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="handler store query failed")
    if not ok:
        # 본인 아님 또는 row 없음 — 현 상태 조회해서 정확한 응답
        current = await store.get(raw_sid)
        if current is None:
            return {"released": False, "reason": "not_claimed"}
        raise HTTPException(
            status_code=403,
            detail={"reason": "not_owner", "handler": current},
        )
    # release 성공 후 — handler 0 + memo 0이면 snapshot 정리 (관리 목록에서 자연 빠짐).
    # list_managed 가 메모 OR handler driving 이므로 둘 다 없으면 어차피 노출 안 됨.
    # graceful — 실패해도 release 결과는 OK.
    try:
        from src.routes.memos import _maybe_drop_snapshot

        await _maybe_drop_snapshot(raw_sid)
    except Exception:
        logger.exception(
            "snapshot cleanup after handler release failed sid=%s (graceful)", raw_sid
        )
    return {"released": True}


@router.get("/{sid}/handler")
async def get_handler(sid: str = Path(...)) -> dict[str, Any]:
    store = _require_store()
    raw_sid = _normalize_sid(sid)
    try:
        row = await store.get(raw_sid)
    except Exception:
        logger.exception("get_handler failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="handler store query failed")
    return {"sid": sid, "handler": row}
