"""관리 신청서 목록 — 메모 또는 handler 잡힌 신청서를 메인과 동일 augment로 노출.

자란다 prod 신청서가 살아있으면 메인 신청서 목록과 동일한 풍부 데이터
(teachers / applyCount / prob / parent_history / handler / memoCount 등).
없거나 fetch 실패면 frozen snapshot fallback.

인증: trigger_auth — Google 세션 OR X-Trigger-Secret(진단·운영 자동화용).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from src.db import get_replica
from src.firestore_chat import find_chat_status, firestore_available
from src.handler_store import get_handler_store, handler_store_available
from src.memo_store import get_memo_store, memo_store_available
from src.routes.applications import _to_row, get_subject_map
from src.routes.auto_dispatch import trigger_auth
from src.snapshot_store import get_snapshot_store, snapshot_store_available

router = APIRouter(prefix="/api/managed-applications", tags=["managed"])
logger = logging.getLogger(__name__)


async def _handler_batch(sids: list[str]) -> dict[str, dict[str, Any]]:
    if not handler_store_available():
        return {}
    try:
        return await get_handler_store().list_by_sids(sids)
    except Exception:
        logger.exception("managed handler batch failed (graceful)")
        return {}


def _snapshot_only_row(snap: dict[str, Any]) -> dict[str, Any]:
    """자란다 prod에 신청서가 없을 때 frozen snapshot으로 최소 row 구성."""
    sid = snap.get("sid") or ""
    return {
        "key": sid,
        "sid": f"SID-{sid}" if sid else "",
        "child": snap.get("child") or "자녀 미입력",
        "date": "—",
        "createdAtIso": snap.get("appCreatedAt"),
        "region": snap.get("region") or "",
        "status": snap.get("status") or "",
        "statusKey": snap.get("statusKey") or "pending",
        "subjects": snap.get("subjects") or [],
        "wageRanges": snap.get("wageRanges") or [],
        "requestChips": snap.get("requestChips") or [],
        "isUrgent": bool(snap.get("isUrgent")),
        "autoConfirm": bool(snap.get("autoConfirm")),
        "reRecommend": bool(snap.get("reRecommend")),
        "matchedTeacher": snap.get("matchedTeacher") or "",
        "cancelledReason": snap.get("cancelledReason") or "",
        "request": snap.get("parentRequest") or "",
        "memoCount": snap.get("memoCount") or 0,
        "lastMemoAt": snap.get("lastMemoAt"),
        "handler": snap.get("handler"),
        "teachers": [],
        "applyCount": 0,
        "reqCount": 0,
        "prob": {"value": 0, "source": "heuristic"},
        "timerMin": None,
        "deadlineState": "ok",
        "deadlineLabel": "—",
        "_snapshotOnly": True,
    }


@router.get("")
async def list_managed_applications(
    _user: dict = Depends(trigger_auth),
    limit: int = Query(200, le=500),
) -> dict[str, Any]:
    """관리 신청서 목록 — 메인과 동일 응답 shape.

    1) snapshot_store.list_managed → driving sid 목록 + frozen snapshot
    2) replica에서 sids 로 raw recommendation row batch 조회
    3) applications.py 의 _to_row + 동일한 augment batch
    4) 자란다에 없는 sid 는 frozen snapshot 으로 minimal row
    """
    if not snapshot_store_available():
        raise HTTPException(status_code=503, detail="snapshot store 미설정")
    try:
        snap_rows = await get_snapshot_store().list_managed(limit=limit)
    except Exception:
        logger.exception("list_managed_applications failed")
        raise HTTPException(status_code=503, detail="snapshot store query failed")

    sids = [r["sid"] for r in snap_rows]
    if not sids:
        return {"count": 0, "rows": []}

    replica = get_replica()
    raw_rows: list[dict[str, Any]] = []
    try:
        raw_rows = await replica.list_recent_recommendations(limit=limit, sids=sids)
    except Exception:
        logger.exception("managed replica fetch failed (graceful)")

    raw_map: dict[str, dict[str, Any]] = {str(r["sid"]): r for r in raw_rows}
    rec_sids = list(raw_map.keys())
    parent_sids = list({r["parent_account_sid"] for r in raw_rows if r.get("parent_account_sid")})

    # augment batch
    history_map: dict[str, dict[str, int]] = {}
    teachers_map: dict[str, list[dict[str, Any]]] = {}
    wage_range_map: dict[str, list[str]] = {}
    subject_map: dict[int, str] = {}
    handler_map: dict[str, dict[str, Any]] = {}
    if rec_sids:
        try:
            history_map, teachers_map, wage_range_map, subject_map, handler_map = await asyncio.gather(
                replica.get_parent_history_counts(parent_sids),
                replica.list_recommendation_teachers_batch(rec_sids),
                replica.list_wage_ranges(rec_sids),
                get_subject_map(),
                _handler_batch(sids),
            )
        except Exception:
            logger.exception("managed augment batch failed (graceful)")
    else:
        try:
            subject_map = await get_subject_map()
        except Exception:
            subject_map = {}
        handler_map = await _handler_batch(sids)

    teacher_sids = list({
        str(t.get("teacher_account_sid"))
        for ts in teachers_map.values()
        for t in ts
        if t.get("teacher_account_sid")
    })
    all_subject_ids: set[int] = set()
    for r in raw_rows:
        raw = r.get("teacher_specialties")
        if not raw:
            continue
        for tok in str(raw).split("|"):
            tok = tok.strip()
            if tok.isdigit():
                all_subject_ids.add(int(tok))

    feedback_map: dict[str, dict[str, Any]] = {}
    teacher_wages_map: dict[str, list[dict[str, Any]]] = {}
    visit_counts_map: dict[str, int] = {}
    teacher_availability_map: dict[str, dict[str, bool]] = {}
    if teacher_sids:
        try:
            feedback_map, teacher_wages_map, visit_counts_map, teacher_availability_map = await asyncio.gather(
                replica.get_teacher_feedback_summary(teacher_sids),
                replica.list_teacher_subject_wages(teacher_sids, sorted(all_subject_ids)),
                replica.list_scheduled_child_counts(teacher_sids),
                replica.list_teacher_weekly_availability(teacher_sids),
            )
        except Exception:
            logger.exception("managed teacher augment batch failed (graceful)")

    chat_room_map: dict[tuple[str, str], dict[str, Any]] = {}
    if firestore_available() and teachers_map:
        pairs: list[tuple[str, str]] = []
        for r in raw_rows:
            psid = r.get("parent_account_sid")
            if not psid:
                continue
            for t in teachers_map.get(str(r.get("sid")), []) or []:
                tsid = t.get("teacher_account_sid")
                if tsid:
                    pairs.append((str(psid), str(tsid)))
        if pairs:
            try:
                chat_room_map = await find_chat_status(list(set(pairs)))
            except Exception:
                logger.exception("managed chat_room fetch failed (graceful)")

    memo_meta_map: dict[str, dict[str, Any]] = {}
    if memo_store_available():
        try:
            memo_meta_map = await get_memo_store().counts_by_sids(sids)
        except Exception:
            logger.exception("managed memo batch failed (graceful)")

    rows: list[dict[str, Any]] = []
    # snapshot driving 순서 보존 (last_activity DESC)
    for snap in snap_rows:
        sid = snap["sid"]
        raw = raw_map.get(sid)
        if raw is None:
            row = _snapshot_only_row(snap)
            if memo_meta_map.get(sid):
                row["memoCount"] = int(memo_meta_map[sid].get("count") or 0)
                row["lastMemoAt"] = memo_meta_map[sid].get("last_created_at")
            if handler_map.get(sid):
                row["handler"] = handler_map[sid]
            rows.append(row)
            continue
        try:
            row = _to_row(
                raw,
                subject_map,
                history_map.get(raw.get("parent_account_sid")),
                teachers_map.get(sid),
                handler_map.get(sid),
                feedback_map,
                wage_range_map.get(sid),
                teacher_wages_map,
                {},  # push_map — 상세 패널에서 lazy load
                visit_counts_map,
                teacher_availability_map,
                chat_room_map,
                memo_meta_map.get(sid),
            )
        except Exception:
            logger.exception("managed _to_row failed sid=%s (fallback)", sid)
            row = _snapshot_only_row(snap)
        rows.append(row)

    return {"count": len(rows), "rows": rows}
