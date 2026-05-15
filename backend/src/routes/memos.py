"""신청서별 메모 CRUD.

저장소: matching-ops 전용 Cloud SQL (PostgreSQL). vibe-cs DB와 분리.
인증: require_auth_full → author_email + author_name 기록.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from src.auth import require_auth_full
from src.memo_store import get_memo_store, memo_store_available

router = APIRouter(prefix="/api/applications", tags=["memos"])
logger = logging.getLogger(__name__)


class MemoCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list)


def _require_store() -> Any:
    if not memo_store_available():
        raise HTTPException(status_code=503, detail="memo store 미설정")
    return get_memo_store()


@router.post("/{sid}/memos")
async def create_memo(
    body: MemoCreate,
    sid: str = Path(...),
    user: dict = Depends(require_auth_full),
) -> dict[str, Any]:
    store = _require_store()
    try:
        return await store.create_memo(
            application_sid=sid,
            author_email=user["email"],
            author_name=user.get("name") or None,
            content=body.content.strip(),
            tags=body.tags,
        )
    except Exception:
        logger.exception("create_memo failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="memo store query failed")


@router.get("/{sid}/memos")
async def list_memos(sid: str = Path(...)) -> dict[str, Any]:
    store = _require_store()
    try:
        memos = await store.list_memos_by_application(sid)
    except Exception:
        logger.exception("list_memos failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="memo store query failed")
    return {"sid": sid, "count": len(memos), "memos": memos}


@router.delete("/{sid}/memos/{memo_id}")
async def delete_memo(
    sid: str = Path(...),
    memo_id: int = Path(..., ge=1),
    user: dict = Depends(require_auth_full),
) -> dict[str, Any]:
    """본인 글만 soft delete. 권한 없거나 글 없음이면 404."""
    store = _require_store()
    try:
        ok = await store.delete_memo(memo_id=memo_id, author_email=user["email"])
    except Exception:
        logger.exception("delete_memo failed sid=%s memo_id=%s", sid, memo_id)
        raise HTTPException(status_code=503, detail="memo store query failed")
    if not ok:
        raise HTTPException(status_code=404, detail="memo not found or not author")
    return {"deleted": True, "id": memo_id}
