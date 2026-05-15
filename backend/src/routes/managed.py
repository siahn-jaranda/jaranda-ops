"""관리 신청서 목록 — 메모가 있는 신청서의 영속 이력.

자란다 prod의 신청서가 사라지거나 윈도우(72h)를 벗어나도, 메모가 남아 있는
한 snapshot 테이블에서 정보를 보존한다. 정렬은 최근 메모 작성순.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.snapshot_store import get_snapshot_store, snapshot_store_available

router = APIRouter(prefix="/api/managed-applications", tags=["managed"])
logger = logging.getLogger(__name__)


@router.get("")
async def list_managed_applications(limit: int = Query(100, le=200)) -> dict[str, Any]:
    if not snapshot_store_available():
        raise HTTPException(status_code=503, detail="snapshot store 미설정")
    try:
        rows = await get_snapshot_store().list_managed(limit=limit)
    except Exception:
        logger.exception("list_managed_applications failed")
        raise HTTPException(status_code=503, detail="snapshot store query failed")
    return {"count": len(rows), "rows": rows}
