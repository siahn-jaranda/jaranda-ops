"""관리 신청서 목록 — 메모가 있는 신청서의 영속 이력.

자란다 prod의 신청서가 사라지거나 윈도우(72h)를 벗어나도, 메모가 남아 있는
한 snapshot 테이블에서 정보를 보존한다. 정렬은 최근 메모 작성순.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.db import get_replica
from src.snapshot_store import get_snapshot_store, snapshot_store_available

router = APIRouter(prefix="/api/managed-applications", tags=["managed"])
logger = logging.getLogger(__name__)


async def _overlay_live_status(rows: list[dict[str, Any]]) -> None:
    """frozen snapshot의 동적 필드(상태·매칭·취소·마감)를 replica 현재값으로 덮어쓴다.

    in-place 수정. replica 조회 실패나 sid 누락(삭제된 신청서)은 graceful —
    해당 row는 frozen 값을 유지한다. 컨텍스트 필드(시급·지역·요청)는 손대지 않음.
    """
    from src.routes.applications import live_status_overlay

    sids = [r["sid"] for r in rows]
    if not sids:
        return
    try:
        overlay = await get_replica().get_status_overlay(sids)
    except Exception:
        logger.exception("managed live status overlay failed (graceful — frozen 유지)")
        return
    for r in rows:
        rec = overlay.get(r["sid"])
        if rec:
            r.update(live_status_overlay(rec))


@router.get("")
async def list_managed_applications(limit: int = Query(100, le=200)) -> dict[str, Any]:
    if not snapshot_store_available():
        raise HTTPException(status_code=503, detail="snapshot store 미설정")
    try:
        rows = await get_snapshot_store().list_managed(limit=limit)
    except Exception:
        logger.exception("list_managed_applications failed")
        raise HTTPException(status_code=503, detail="snapshot store query failed")
    await _overlay_live_status(rows)
    return {"count": len(rows), "rows": rows}
