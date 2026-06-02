"""메모 집계 인사이트 — 5종 필터 + LLM 호출.

기본: matching_ops_memo 활성 메모 전체를 모아 LLM 인사이트.
필터 (모두 선택):
  - tags: 메모 태그 (JSONB any-match)
  - region: snapshot.region prefix
  - subject_id: snapshot.subjects JSONB 의 id
  - wage_range_code: snapshot.wage_ranges JSONB 의 code
  - parent_type: 'new'(첫 매칭) / 'repeat'(재이용) — replica.get_parent_history_counts 배치

비용 가드: settings.llm_daily_limit 공유 (인사이트와 동일 counter).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.config import settings
from src.db import get_replica
from src.llm_client import AGGREGATED_INSIGHT_SYSTEM_PROMPT, get_llm_client
from src.llm_insight_store import get_llm_insight_store, llm_insight_available
from src.memo_store import memo_store_available
from src.routes.auto_dispatch import trigger_auth

router = APIRouter(prefix="/api/aggregated-insights", tags=["aggregated-insights"])
logger = logging.getLogger(__name__)


class DeriveRequest(BaseModel):
    tags: list[str] | None = Field(default=None, description="메모 태그 any-match")
    region: str | None = Field(default=None, description="snapshot.region prefix")
    subject_id: int | None = Field(default=None, description="snapshot.subjects.id")
    wage_range_code: str | None = Field(default=None, description="snapshot.wage_ranges.code")
    parent_type: str | None = Field(default=None, description="'new' / 'repeat'")
    memo_limit: int = Field(default=300, ge=1, le=1000)


def _get_engine() -> AsyncEngine:
    """matching-ops PG engine — memo_store 와 동일 인스턴스 재사용."""
    from src.memo_store import get_memo_store
    return get_memo_store()._engine  # type: ignore[attr-defined]


async def _query_filtered_memos(
    *,
    tags: list[str] | None,
    region: str | None,
    subject_id: int | None,
    wage_range_code: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """matching_ops_memo + snapshot LEFT JOIN, 필터 적용."""
    where = ["m.deleted_at IS NULL"]
    params: dict[str, Any] = {"lim": limit}

    if region:
        where.append("s.region LIKE :region_pat")
        params["region_pat"] = f"{region}%"
    if subject_id is not None:
        where.append("s.subjects @> CAST(:subj AS JSONB)")
        params["subj"] = json.dumps([{"id": int(subject_id)}])
    if wage_range_code:
        where.append("s.wage_ranges @> CAST(:wage AS JSONB)")
        params["wage"] = json.dumps([{"code": wage_range_code}])
    if tags:
        where.append("m.tags ?| :tags_arr")
        params["tags_arr"] = tags

    where_sql = " AND ".join(where)
    query = text(
        f"""
        SELECT
          m.id, m.application_sid, m.author_email, m.author_name,
          m.content, m.tags, m.created_at,
          s.child_name, s.region, s.subjects, s.wage_ranges,
          s.is_urgent, s.app_created_at
        FROM matching_ops_memo m
        LEFT JOIN matching_ops_application_snapshot s ON s.application_sid = m.application_sid
        WHERE {where_sql}
        ORDER BY m.created_at DESC
        LIMIT :lim
        """
    )

    engine = _get_engine()
    async with engine.begin() as conn:
        rows = await conn.execute(query, params)
        out = []
        for r in rows:
            m = r._mapping
            out.append({
                "id": int(m["id"]),
                "application_sid": m["application_sid"],
                "author_email": m["author_email"],
                "author_name": m["author_name"],
                "content": m["content"],
                "tags": m["tags"] if isinstance(m["tags"], list) else (json.loads(m["tags"]) if m["tags"] else []),
                "created_at": m["created_at"].isoformat() if m["created_at"] else None,
                "child_name": m["child_name"],
                "region": m["region"],
                "subjects": m["subjects"] if isinstance(m["subjects"], list) else (json.loads(m["subjects"]) if m["subjects"] else []),
                "wage_ranges": m["wage_ranges"] if isinstance(m["wage_ranges"], list) else (json.loads(m["wage_ranges"]) if m["wage_ranges"] else []),
                "is_urgent": bool(m["is_urgent"]) if m["is_urgent"] is not None else False,
                "app_created_at": m["app_created_at"].isoformat() if m["app_created_at"] else None,
            })
        return out


async def _filter_by_parent_type(memos: list[dict[str, Any]], parent_type: str) -> list[dict[str, Any]]:
    """parent_type 필터 — replica 에서 sid → parent_sid → confirmed_count 배치 조회."""
    if not memos:
        return memos
    sids = list({m["application_sid"] for m in memos})
    replica = get_replica()
    parent_by_sid: dict[str, str] = {}
    query = text(
        "SELECT sid, parent_account_sid FROM recommendation WHERE sid IN :sids"
    ).bindparams(bindparam("sids", expanding=True))
    async with replica._session_factory() as session:  # type: ignore[attr-defined]
        rows = await session.execute(query, {"sids": sids})
        for r in rows:
            parent_by_sid[str(r._mapping["sid"])] = str(r._mapping["parent_account_sid"])
    parent_sids = list(set(parent_by_sid.values()))
    if not parent_sids:
        return []
    history = await replica.get_parent_history_counts(parent_sids)
    want_new = parent_type == "new"
    out = []
    for m in memos:
        psid = parent_by_sid.get(m["application_sid"])
        if not psid:
            continue
        h = history.get(psid) or {}
        is_new = (h.get("confirmed_count") or 0) == 0
        if is_new == want_new:
            out.append(m)
    return out


def _build_llm_input(
    memos: list[dict[str, Any]], filters: dict[str, Any]
) -> dict[str, Any]:
    """LLM 입력 — 필터 컨텍스트 + 메모 리스트 (요약 필드만)."""
    memo_views = []
    for m in memos:
        memo_views.append({
            "child": m.get("child_name"),
            "region": m.get("region"),
            "subjects": [s.get("name") for s in (m.get("subjects") or []) if s.get("name")],
            "wage_ranges": [w.get("label") or w.get("code") for w in (m.get("wage_ranges") or [])],
            "is_urgent": m.get("is_urgent"),
            "author": m.get("author_name") or m.get("author_email"),
            "tags": m.get("tags") or [],
            "content": (m.get("content") or "").strip(),
        })
    return {
        "filters": filters,
        "memo_count": len(memo_views),
        "memos": memo_views,
    }


@router.post("/derive")
async def derive_aggregated_insights(
    body: DeriveRequest = Body(default_factory=DeriveRequest),
    _user: dict = Depends(trigger_auth),
) -> dict[str, Any]:
    if not memo_store_available():
        raise HTTPException(status_code=503, detail="memo store 미설정")
    if not llm_insight_available():
        raise HTTPException(status_code=503, detail="LLM 미설정 — ANTHROPIC_API_KEY 확인")

    try:
        memos = await _query_filtered_memos(
            tags=body.tags, region=body.region,
            subject_id=body.subject_id, wage_range_code=body.wage_range_code,
            limit=body.memo_limit,
        )
    except Exception:
        logger.exception("aggregated_insights query failed")
        raise HTTPException(status_code=503, detail="메모 조회 실패")

    if body.parent_type in ("new", "repeat"):
        try:
            memos = await _filter_by_parent_type(memos, body.parent_type)
        except Exception:
            logger.exception("aggregated_insights parent_type filter failed (graceful)")

    filters_view = {
        "tags": body.tags or [],
        "region": body.region or "",
        "subject_id": body.subject_id,
        "wage_range_code": body.wage_range_code or "",
        "parent_type": body.parent_type or "",
    }

    # 0~3건이면 LLM 호출 안 함 (비용 절약)
    if len(memos) <= 3:
        return {
            "filters": filters_view,
            "memo_count": len(memos),
            "summary": f"표본 부족 — {len(memos)}건. 필터 완화 또는 메모 누적 필요.",
            "themes": [],
            "key_insights": [],
            "recommended_actions": [],
            "model_id": settings.llm_model_id,
            "skipped_llm": True,
        }

    try:
        ok, current = await get_llm_insight_store().check_and_increment_daily(
            limit=settings.llm_daily_limit
        )
    except Exception:
        logger.exception("aggregated_insights daily counter failed")
        raise HTTPException(status_code=503, detail="counter store failed")
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"일일 LLM 호출 한도({settings.llm_daily_limit}건) 초과. 현재 {current}건",
        )

    payload = _build_llm_input(memos, filters_view)
    try:
        _, parsed, in_tok, out_tok = await get_llm_client().generate_recommendation(
            payload,
            max_tokens=2048,
            system_prompt=AGGREGATED_INSIGHT_SYSTEM_PROMPT,
        )
    except Exception:
        logger.exception("aggregated_insights LLM call failed")
        raise HTTPException(status_code=502, detail="LLM 호출 실패")

    try:
        await get_llm_insight_store().add_token_usage(in_tok, out_tok)
    except Exception:
        logger.exception("aggregated_insights token persist failed (graceful)")

    return {
        "filters": filters_view,
        "memo_count": len(memos),
        "summary": parsed.get("summary") or "",
        "themes": parsed.get("themes") or [],
        "key_insights": parsed.get("key_insights") or [],
        "recommended_actions": parsed.get("recommended_actions") or [],
        "model_id": settings.llm_model_id,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }


@router.get("/options")
async def list_filter_options(_user: dict = Depends(trigger_auth)) -> dict[str, Any]:
    """필터 dropdown 옵션 — snapshot/memo 에서 distinct."""
    engine = _get_engine()
    async with engine.begin() as conn:
        regions = await conn.execute(text(
            "SELECT DISTINCT region FROM matching_ops_application_snapshot "
            "WHERE region IS NOT NULL AND region <> '' ORDER BY region"
        ))
        region_list = [r._mapping["region"] for r in regions]

        subj_rows = await conn.execute(text(
            "SELECT DISTINCT subjects FROM matching_ops_application_snapshot "
            "WHERE subjects IS NOT NULL"
        ))
        subj_map: dict[int, str] = {}
        for sr in subj_rows:
            arr = sr._mapping["subjects"]
            if isinstance(arr, str):
                try: arr = json.loads(arr)
                except Exception: arr = []
            for s in arr or []:
                sid = s.get("id")
                if isinstance(sid, int):
                    subj_map[sid] = s.get("name") or f"과목{sid}"
        subjects = [{"id": k, "name": v} for k, v in sorted(subj_map.items())]

        wage_rows = await conn.execute(text(
            "SELECT DISTINCT wage_ranges FROM matching_ops_application_snapshot "
            "WHERE wage_ranges IS NOT NULL"
        ))
        wage_map: dict[str, str] = {}
        for wr in wage_rows:
            arr = wr._mapping["wage_ranges"]
            if isinstance(arr, str):
                try: arr = json.loads(arr)
                except Exception: arr = []
            for w in arr or []:
                code = w.get("code")
                if code:
                    wage_map[code] = w.get("label") or code
        wages = [{"code": k, "label": v} for k, v in sorted(wage_map.items())]

        tag_rows = await conn.execute(text(
            "SELECT DISTINCT jsonb_array_elements_text(tags) AS tag "
            "FROM matching_ops_memo WHERE deleted_at IS NULL AND tags IS NOT NULL "
            "ORDER BY tag"
        ))
        tags = [r._mapping["tag"] for r in tag_rows]

    return {
        "regions": region_list,
        "subjects": subjects,
        "wage_ranges": wages,
        "tags": tags,
        "parent_types": [
            {"code": "new", "label": "신규 부모님"},
            {"code": "repeat", "label": "기존 부모님(재이용)"},
        ],
    }
