"""신청서별 LLM 인사이트 — Claude Sonnet 4.6 호출.

호출 흐름:
  1) 신청서 + 메모 fetch → LLM input 구성
  2) input_hash 계산. 캐시 hit이면 즉시 반환 (cached=true)
  3) 일일 한도 atomic check+increment. 초과 시 429
  4) LLM 호출 → 응답 캐시 upsert → 토큰 카운터 누적
  5) 응답 반환

운영 비용 가드:
- 일일 한도(settings.llm_daily_limit, 기본 200건)
- max_tokens(settings.llm_max_tokens, 기본 512)
- system prompt cache_control (반복 호출 시 입력 토큰 절감)
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Path
from pydantic import BaseModel, Field

from src.config import settings
from src.db import get_replica
from src.llm_client import get_llm_client
from src.llm_insight_store import get_llm_insight_store, llm_insight_available
from src.memo_store import get_memo_store, memo_store_available
from src.routes.applications import get_application

router = APIRouter(prefix="/api/applications", tags=["insights"])
logger = logging.getLogger(__name__)


class InsightRequest(BaseModel):
    force_refresh: bool = Field(default=False)


def _build_llm_input(app: dict[str, Any], memos: list[dict[str, Any]]) -> dict[str, Any]:
    """get_application 응답 + 메모 list → LLM에 전달할 input dict.

    선생님은 추천 응답이 의미있는 필드만 추려서 전달. 메모는 핵심 필드만.
    """
    teachers = [
        {
            "name": t.get("name"),
            "stat": t.get("stat"),
            "viewed": bool(t.get("viewed")),
            "viewed_count": int(t.get("viewed_count") or 0),
            "total_hours": float(t.get("total_hours") or 0),
            "play_hours": float(t.get("play_hours") or 0),
            "study_hours": float(t.get("study_hours") or 0),
            "review_count": int(t.get("review_count") or 0),
            "recommend_count": int(t.get("recommend_count") or 0),
            "recommend_rate": t.get("recommend_rate"),
        }
        for t in (app.get("teachers") or [])
    ]
    memo_view = [
        {
            "author_name": m.get("author_name") or m.get("author_email"),
            "content": m.get("content"),
            "tags": m.get("tags") or [],
            "created_at": m.get("created_at"),
        }
        for m in (memos or [])
    ]
    return {
        "application": {
            "sid": app.get("sid"),
            "child": app.get("child"),
            "region": app.get("region"),
            "status": app.get("status"),
            "status_key": app.get("statusKey"),
            "is_new": app.get("isNew"),
            "app_count": app.get("appCount"),
            "confirmed_count": app.get("confirmedCount"),
            "lesson_count": app.get("lessonCount"),
            "request_chips": app.get("requestChips") or [],
            "subjects": app.get("subjects") or [],
            "request": app.get("request"),
            "is_urgent": bool(app.get("isUrgent")),
            "auto_confirm": bool(app.get("autoConfirm")),
            "re_recommend": bool(app.get("reRecommend")),
            "deadline_min_left": app.get("timerMin"),
            "deadline_label": app.get("deadlineLabel"),
            "deadline_state": app.get("deadlineState"),
            "req_count": app.get("reqCount"),
            "apply_count": app.get("applyCount"),
            "prob": app.get("prob"),
            "result": app.get("result"),
            "matched_teacher": app.get("matchedTeacher"),
            "cancelled_reason": app.get("cancelledReason"),
            "requested_teacher_name": app.get("requestedTeacherName"),
        },
        "teachers": teachers,
        "memos": memo_view,
    }


def _hash_input(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _format_response(
    sid: str,
    cached: bool,
    input_hash: str,
    model_id: str,
    response_text: str,
    response_json: dict[str, Any],
    input_tokens: int,
    output_tokens: int,
    cached_at: str | None,
) -> dict[str, Any]:
    return {
        "sid": sid,
        "cached": cached,
        "input_hash": input_hash,
        "model_id": model_id,
        "summary": response_json.get("summary") or "",
        "key_signals": response_json.get("key_signals") or [],
        "recommended_actions": response_json.get("recommended_actions") or [],
        "risk_flags": response_json.get("risk_flags") or [],
        "raw_text": response_text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "generated_at": cached_at,
    }


@router.post("/{sid}/insights")
async def generate_insight(
    sid: str = Path(...),
    body: InsightRequest = Body(default_factory=InsightRequest),
) -> dict[str, Any]:
    if not llm_insight_available():
        raise HTTPException(
            status_code=503,
            detail="LLM 인사이트 미설정 — ANTHROPIC_API_KEY 또는 MATCHING_OPS_DB_URL 확인",
        )

    # 1) 신청서 + 메모 fetch
    #    client는 'SID-12345' 형태로 호출. replica 조회는 raw 숫자 PK 필요.
    raw_sid = sid[4:] if sid.startswith("SID-") else sid
    try:
        app = await get_application(raw_sid)
    except HTTPException:
        raise
    except Exception:
        logger.exception("insight: get_application failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="application fetch failed")

    memos: list[dict[str, Any]] = []
    if memo_store_available():
        try:
            memos = await get_memo_store().list_memos_by_application(sid)
        except Exception:
            logger.exception("insight: list_memos failed sid=%s (graceful)", sid)

    llm_input = _build_llm_input(app, memos)
    input_hash = _hash_input(llm_input)

    store = get_llm_insight_store()

    # 2) 캐시 hit 체크
    if not body.force_refresh:
        try:
            cached = await store.get_cached(sid)
        except Exception:
            logger.exception("insight: cache fetch failed sid=%s (graceful)", sid)
            cached = None
        if cached and cached["input_hash"] == input_hash:
            return _format_response(
                sid=sid,
                cached=True,
                input_hash=cached["input_hash"],
                model_id=cached["model_id"],
                response_text=cached["response_text"],
                response_json=cached["response_json"],
                input_tokens=cached["input_tokens"],
                output_tokens=cached["output_tokens"],
                cached_at=cached["updated_at"],
            )

    # 3) 일일 한도 atomic check + increment
    try:
        ok, current = await store.check_and_increment_daily(limit=settings.llm_daily_limit)
    except Exception:
        logger.exception("insight: daily counter failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="counter store failed")
    if not ok:
        logger.warning("insight: daily limit reached sid=%s count=%s", sid, current)
        raise HTTPException(
            status_code=429,
            detail=f"일일 LLM 호출 한도({settings.llm_daily_limit}건) 초과. 현재 {current}건",
        )

    # 4) LLM 호출
    try:
        raw_text, parsed, in_tok, out_tok = await get_llm_client().generate_insight(llm_input)
    except Exception:
        logger.exception("insight: LLM call failed sid=%s", sid)
        raise HTTPException(status_code=502, detail="LLM 호출 실패")

    # 5) 캐시 + 토큰 카운터 저장 (실패해도 응답은 반환)
    try:
        await store.upsert_cache(
            application_sid=sid,
            input_hash=input_hash,
            model_id=settings.llm_model_id,
            response_text=raw_text,
            response_json=parsed,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
        await store.add_token_usage(input_tokens=in_tok, output_tokens=out_tok)
    except Exception:
        logger.exception("insight: cache persist failed sid=%s (graceful)", sid)

    return _format_response(
        sid=sid,
        cached=False,
        input_hash=input_hash,
        model_id=settings.llm_model_id,
        response_text=raw_text,
        response_json=parsed,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cached_at=None,
    )


@router.get("/{sid}/insights")
async def get_cached_insight(sid: str = Path(...)) -> dict[str, Any]:
    """캐시된 인사이트 조회. 없으면 404. 호출 카운터 증가 없음."""
    if not llm_insight_available():
        raise HTTPException(status_code=503, detail="LLM 인사이트 미설정")
    store = get_llm_insight_store()
    try:
        cached = await store.get_cached(sid)
    except Exception:
        logger.exception("insight: cache fetch failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="cache store failed")
    if cached is None:
        raise HTTPException(status_code=404, detail="no insight cached")
    return _format_response(
        sid=sid,
        cached=True,
        input_hash=cached["input_hash"],
        model_id=cached["model_id"],
        response_text=cached["response_text"],
        response_json=cached["response_json"],
        input_tokens=cached["input_tokens"],
        output_tokens=cached["output_tokens"],
        cached_at=cached["updated_at"],
    )
