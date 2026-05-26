"""지원 0개 신청서 → 가능한 선생님 추천 (WELL2-100).

흐름:
  1) 신청서 raw 조회 → 지원 0개 & 좌표 확인
  2) 부모 좌표 → 인접 시군구 → 후보 선생님 풀 (db retrieval, 룰로 먼저 좁힘)
  3) 일일 한도 가드 → LLM 추천 (generate_recommendation)
  4) LLM 순위에 후보 상세를 머지해 반환

PRD: Retrieval(룰) → LLM 2단계. 후보는 반드시 룰로 좁혀 LLM 할루시네이션을 차단한다.
인사이트와 같은 비용 가드(일일 한도)를 공유. 캐시는 후속(입력이 신청서+풀이라 변동 큼).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query

from src.config import settings
from src.db import get_replica
from src.llm_client import RECOVERY_SYSTEM_PROMPT, get_llm_client
from src.llm_insight_store import get_llm_insight_store, llm_insight_available

router = APIRouter(prefix="/api/applications", tags=["candidates"])
logger = logging.getLogger(__name__)

SPECIALTY_NAME = {1: "돌봄", 2: "수학/과학", 3: "운동", 4: "예능", 5: "외국어", 6: "한글/국어"}
DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DOW_KO = {"MONDAY": "mon", "TUESDAY": "tue", "WEDNESDAY": "wed", "THURSDAY": "thu",
          "FRIDAY": "fri", "SATURDAY": "sat", "SUNDAY": "sun"}


def _parse_schedule(raw: Any) -> dict[str, Any]:
    """recommendation.schedule(JSON) → 신청 요일/시간/시작일/주기."""
    if not raw:
        return {}
    try:
        s = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    days = [DOW_KO[d] for d in s.get("possible_day_of_weeks", []) if d in DOW_KO]
    label = None
    slots = s.get("possible_time_slots") or []
    if slots and slots[0].get("times"):
        ts = slots[0]["times"]
        label = f"{ts[0]['start_time']}~{ts[-1]['end_time']} 중 {s.get('duration_minutes')}분"
    return {"days": days, "time_label": label, "start_date": s.get("start_date"),
            "weekly_frequency": s.get("weekly_frequency")}


def _candidate_view(c: dict[str, Any], want_days: list[str]) -> dict[str, Any]:
    avail = [d for d in DOW if c.get(d)]
    day_match = [d for d in want_days if d in avail]
    rec = int(c.get("recommends") or 0)
    rev = int(c.get("reviews") or 0)
    return {
        "teacher_sid": c["teacher_sid"], "name": c["name"],
        "activity": c.get("activity_status_text"),
        "exp_hours": float(c.get("experience_hour") or 0),
        "subject_exp_hours": float(c.get("experience_hour_for_study") or 0),
        "school": c.get("university"), "major": c.get("major"),
        "reviews": rev, "recommends": rec,
        "recommend_rate": round(rec / rev * 100, 1) if rev else None,
        "lateness": int(c.get("lateness") or 0),
        "active_kids": int(c.get("active_kids") or 0),
        "subject_wage": int(c.get("subject_wage") or 0),
        "available_days": avail,
        "day_match": day_match,
        "day_match_full": bool(want_days) and len(day_match) == len(want_days),
        "intro": (c.get("intro") or "").strip() or None,
    }


def _build_input(app: dict[str, Any], cand_views: list[dict[str, Any]]) -> dict[str, Any]:
    spec = int(app.get("teacher_specialties") or 5)
    sched = _parse_schedule(app.get("schedule"))
    return {
        "application": {
            "sid": app["sid"],
            "region": (app.get("parent_address") or "").split("|")[0],
            "subject": SPECIALTY_NAME.get(spec, "?"),
            "want_days": sched.get("days", []),
            "time_slots": sched.get("time_label"),
            "start_date": sched.get("start_date"),
            "weekly_frequency": sched.get("weekly_frequency"),
            "biweekly": bool(app.get("biweekly")),
            "estimated_charge": int(app.get("estimated_charge") or 0),
            "parent_request": app.get("parent_request_to_teacher") or None,
            "preferred_gender": app.get("preferable_teacher_gender") or None,
            "preferred_traits": app.get("preferable_teacher_characteristics") or None,
            "deadline_at": app.get("deadline_at"),
        },
        "candidates": cand_views,
    }


def _recovery_view(c: dict[str, Any], want_days: list[str]) -> dict[str, Any]:
    """_candidate_view + 회수 신호(지원 미선택 횟수·최근시각, 수업 종료 수·최근시각)."""
    v = _candidate_view(c, want_days)
    v["recovery"] = {
        "unmatched_count": int(c.get("unmatched_count") or 0),
        "closed_count": int(c.get("closed_count") or 0),
        "last_unmatched_at": c.get("last_unmatched_at"),
        "last_closed_at": c.get("last_closed_at"),
    }
    return v


@router.post("/{sid}/teacher-candidates")
async def recommend_candidates(
    sid: str = Path(...),
    n_gu: int = Query(3, ge=1, le=8, description="인근 시군구 수"),
    limit: int = Query(15, ge=1, le=30, description="후보 선생님 최대 수"),
) -> dict[str, Any]:
    if not llm_insight_available():
        raise HTTPException(
            status_code=503,
            detail="LLM 미설정 — ANTHROPIC_API_KEY 또는 MATCHING_OPS_DB_URL 확인",
        )

    raw_sid = sid[4:] if sid.startswith("SID-") else sid
    replica = get_replica()
    try:
        app = await replica.get_recommendation(raw_sid)
    except Exception:
        logger.exception("candidates: get_recommendation failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="application fetch failed")
    if app is None:
        raise HTTPException(status_code=404, detail="신청서 없음")
    if int(app.get("applied_count") or 0) > 0:
        raise HTTPException(status_code=422, detail="이미 지원·수락 선생님이 있는 신청서 (지원 0개 전용)")
    if app.get("lat") is None or app.get("lng") is None:
        raise HTTPException(status_code=422, detail="신청서에 좌표(lat/lng)가 없어 거리 매칭 불가")

    spec = int(app.get("teacher_specialties") or 5)
    statuses = [2]  # 활동중만 (활동대기 제외)
    try:
        gu_codes = await replica.find_nearby_sigungu(
            float(app["lat"]), float(app["lng"]), n_gu
        )
        cands = await replica.list_candidate_teachers(
            raw_sid, gu_codes, spec, statuses, limit
        )
    except Exception:
        logger.exception("candidates: retrieval failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="후보 조회 실패")

    sched = _parse_schedule(app.get("schedule"))
    want_days = sched.get("days", [])
    cand_views = [_candidate_view(c, want_days) for c in cands]

    base = {
        "sid": sid,
        "applied_count": 0,
        "region": (app.get("parent_address") or "").split("|")[0],
        "candidate_count": len(cand_views),
        "candidates": cand_views,
        "model_id": settings.llm_recommend_model_id,
    }
    if not cand_views:
        return {**base, "summary": "후보 없음 — 지역 확장 필요", "ranked": [],
                "note": "인근 활동중 선생님 0명. n_gu(인근 시군구) 확대 필요"}

    # 일일 한도 가드 (인사이트와 공유)
    try:
        ok, current = await get_llm_insight_store().check_and_increment_daily(
            limit=settings.llm_daily_limit
        )
    except Exception:
        logger.exception("candidates: daily counter failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="counter store failed")
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"일일 LLM 호출 한도({settings.llm_daily_limit}건) 초과. 현재 {current}건",
        )

    payload = _build_input(app, cand_views)
    try:
        _, parsed, in_tok, out_tok = await get_llm_client().generate_recommendation(payload)
    except Exception:
        logger.exception("candidates: LLM call failed sid=%s", sid)
        raise HTTPException(status_code=502, detail="LLM 호출 실패")

    try:
        await get_llm_insight_store().add_token_usage(in_tok, out_tok)
    except Exception:
        logger.exception("candidates: token usage persist failed sid=%s (graceful)", sid)

    # LLM 순위에 후보 상세 머지 (프론트가 카드로 바로 그릴 수 있게)
    by_sid = {c["teacher_sid"]: c for c in cand_views}
    ranked = [
        {**r, "detail": by_sid.get(r.get("teacher_sid"))}
        for r in (parsed.get("ranked") or [])
    ]
    return {
        **base,
        "summary": parsed.get("summary") or "",
        "ranked": ranked,
        "note": parsed.get("note") or "",
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }


@router.post("/{sid}/teacher-recovery-candidates")
async def recommend_recovery_candidates(
    sid: str = Path(...),
    radius_m: int = Query(5000, ge=1000, le=20000, description="부모 좌표 반경(m)"),
    apply_days: int = Query(30, ge=1, le=90, description="지원 미선택 조회 기간(일)"),
    close_days: int = Query(3, ge=1, le=14, description="수업 종료 조회 기간(일)"),
    limit: int = Query(20, ge=1, le=40, description="후보 최대 수"),
) -> dict[str, Any]:
    """지원 0개 신청서 '지역 회수' 추천 — 부모 좌표 반경 내에서
      (A) 최근 지원했으나 미선택 / (B) 최근 수업 종료된 선생님을 LLM이 재정렬."""
    if not llm_insight_available():
        raise HTTPException(
            status_code=503,
            detail="LLM 미설정 — ANTHROPIC_API_KEY 또는 MATCHING_OPS_DB_URL 확인",
        )

    raw_sid = sid[4:] if sid.startswith("SID-") else sid
    replica = get_replica()
    try:
        app = await replica.get_recommendation(raw_sid)
    except Exception:
        logger.exception("recovery: get_recommendation failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="application fetch failed")
    if app is None:
        raise HTTPException(status_code=404, detail="신청서 없음")
    if int(app.get("applied_count") or 0) > 0:
        raise HTTPException(status_code=422, detail="이미 지원·수락 선생님이 있는 신청서 (지원 0개 전용)")
    if app.get("lat") is None or app.get("lng") is None:
        raise HTTPException(status_code=422, detail="신청서에 좌표(lat/lng)가 없어 거리 매칭 불가")

    spec = int(app.get("teacher_specialties") or 5)
    statuses = [2, 10]  # 회수는 활동중+활동대기 (폭넓게)
    try:
        cands = await replica.list_recovery_candidates(
            float(app["lat"]), float(app["lng"]), spec, statuses,
            radius_m=radius_m, apply_days=apply_days, close_days=close_days, limit=limit,
        )
    except Exception:
        logger.exception("recovery: retrieval failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="후보 조회 실패")

    sched = _parse_schedule(app.get("schedule"))
    want_days = sched.get("days", [])
    cand_views = [_recovery_view(c, want_days) for c in cands]

    base = {
        "sid": sid,
        "applied_count": 0,
        "region": (app.get("parent_address") or "").split("|")[0],
        "candidate_count": len(cand_views),
        "candidates": cand_views,
        "model_id": settings.llm_recommend_model_id,
        "params": {"radius_m": radius_m, "apply_days": apply_days, "close_days": close_days},
    }
    if not cand_views:
        return {**base, "summary": "회수 후보 없음", "ranked": [],
                "note": "반경 내 최근 지원 미선택·수업 종료 이력 없음. 반경/기간 확대 필요"}

    try:
        ok, current = await get_llm_insight_store().check_and_increment_daily(
            limit=settings.llm_daily_limit
        )
    except Exception:
        logger.exception("recovery: daily counter failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="counter store failed")
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"일일 LLM 호출 한도({settings.llm_daily_limit}건) 초과. 현재 {current}건",
        )

    payload = _build_input(app, cand_views)
    try:
        _, parsed, in_tok, out_tok = await get_llm_client().generate_recommendation(
            payload, system_prompt=RECOVERY_SYSTEM_PROMPT
        )
    except Exception:
        logger.exception("recovery: LLM call failed sid=%s", sid)
        raise HTTPException(status_code=502, detail="LLM 호출 실패")

    try:
        await get_llm_insight_store().add_token_usage(in_tok, out_tok)
    except Exception:
        logger.exception("recovery: token usage persist failed sid=%s (graceful)", sid)

    by_sid = {c["teacher_sid"]: c for c in cand_views}
    ranked = [
        {**r, "detail": by_sid.get(r.get("teacher_sid"))}
        for r in (parsed.get("ranked") or [])
    ]
    return {
        **base,
        "summary": parsed.get("summary") or "",
        "ranked": ranked,
        "note": parsed.get("note") or "",
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }
