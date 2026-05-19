"""신청서별 메모 CRUD.

저장소: matching-ops 전용 Cloud SQL (PostgreSQL). vibe-cs DB와 분리.
인증: require_auth_full → author_email + author_name 기록.

메모 작성/삭제 시 application_snapshot도 함께 갱신 — "관리 신청서 목록" 탭이
자란다 prod에서 신청서가 사라진 후에도 이력을 유지하기 위함.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from src.auth import require_auth_full
from src.db import get_replica
from src.memo_store import get_memo_store, memo_store_available
from src.snapshot_store import get_snapshot_store, snapshot_store_available

router = APIRouter(prefix="/api/applications", tags=["memos"])
logger = logging.getLogger(__name__)


class MemoCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list)


def _require_store() -> Any:
    if not memo_store_available():
        raise HTTPException(status_code=503, detail="memo store 미설정")
    return get_memo_store()


def _normalize_sid(sid: str) -> str:
    """client는 'SID-{uuid}' 형태로 호출. PK/replica 조회는 raw uuid로 통일.
    handlers.py / insights.py와 동일 정책 — 모든 라우트에서 일관 적용.
    """
    return sid[4:] if sid.startswith("SID-") else sid


async def _refresh_snapshot(raw_sid: str) -> None:
    """자란다 replica에서 신청서 fetch → snapshot UPSERT. 실패는 graceful.

    신청서가 자란다 prod에서 삭제됐거나 replica 지연이면 fetch 실패 — 그 경우 기존
    snapshot row 유지 (있으면). 메모 생성 자체는 성공시킨다.
    """
    if not snapshot_store_available():
        return
    try:
        from src.routes.applications import get_subject_map, to_snapshot_fields
        replica = get_replica()
        rec = await replica.get_recommendation(raw_sid)
        if rec is None:
            logger.warning("snapshot refresh skipped — recommendation %s not in replica", raw_sid)
            return
        subject_map = await get_subject_map()
        wage_types = (await replica.list_wage_ranges([raw_sid])).get(raw_sid, [])
        fields = to_snapshot_fields(rec, subject_map, wage_types)
        await get_snapshot_store().upsert(raw_sid, fields)
    except Exception:
        logger.exception("snapshot upsert failed sid=%s (graceful)", raw_sid)


async def _maybe_drop_snapshot(raw_sid: str) -> None:
    """잔여 메모 0건이면 snapshot도 제거 — '메모 있는 동안만 이력 보존' 정책."""
    if not snapshot_store_available():
        return
    try:
        store = get_snapshot_store()
        if await store.memo_count(raw_sid) == 0:
            await store.delete(raw_sid)
    except Exception:
        logger.exception("snapshot drop failed sid=%s (graceful)", raw_sid)


@router.post("/{sid}/memos")
async def create_memo(
    body: MemoCreate,
    sid: str = Path(...),
    user: dict = Depends(require_auth_full),
) -> dict[str, Any]:
    store = _require_store()
    raw_sid = _normalize_sid(sid)
    try:
        memo = await store.create_memo(
            application_sid=raw_sid,
            author_email=user["email"],
            author_name=user.get("name") or None,
            content=body.content.strip(),
            tags=body.tags,
        )
    except Exception:
        logger.exception("create_memo failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="memo store query failed")

    # 메모 저장 성공 후 snapshot 최신화 (실패는 graceful)
    await _refresh_snapshot(raw_sid)
    return memo


@router.get("/{sid}/memos")
async def list_memos(sid: str = Path(...)) -> dict[str, Any]:
    store = _require_store()
    raw_sid = _normalize_sid(sid)
    try:
        memos = await store.list_memos_by_application(raw_sid)
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
    raw_sid = _normalize_sid(sid)
    try:
        ok = await store.delete_memo(memo_id=memo_id, author_email=user["email"])
    except Exception:
        logger.exception("delete_memo failed sid=%s memo_id=%s", sid, memo_id)
        raise HTTPException(status_code=503, detail="memo store query failed")
    if not ok:
        raise HTTPException(status_code=404, detail="memo not found or not author")

    # 잔여 메모 0건이면 snapshot도 삭제 — 관리 신청서 목록에서 빠짐
    await _maybe_drop_snapshot(raw_sid)
    return {"deleted": True, "id": memo_id}
