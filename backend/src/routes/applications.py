"""신청서 조회 엔드포인트.

페이지 메인 테이블 + 단건 상세 + 선생님 목록을 제공.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.db import get_replica

router = APIRouter(prefix="/api/applications", tags=["applications"])


STATUS_LABEL = {
    10: "진행중",
    20: "진행중",
    40: "매칭완료",
    90: "매칭완료",
    99: "취소",
    100: "취소",
    101: "취소",
}


def _is_real_ts(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, datetime):
        return value.year > 2000
    return False


def _timer_min(deadline: Any) -> int | None:
    if not _is_real_ts(deadline):
        return None
    dl = deadline if deadline.tzinfo else deadline.replace(tzinfo=timezone.utc)
    delta = (dl - datetime.now(timezone.utc)).total_seconds() / 60
    if delta <= 0:
        return None
    return int(delta)


def _to_row(rec: dict[str, Any]) -> dict[str, Any]:
    """DB row를 페이지가 사용하는 mock 스키마와 호환되는 형태로 변환."""
    status_code = rec.get("status")
    status_label = STATUS_LABEL.get(status_code, f"상태{status_code}")
    confirmed = rec.get("confirmed_at")
    cancelled = rec.get("cancelled_at")

    if _is_real_ts(confirmed):
        result = "성공"
        result_type = "ok"
    elif _is_real_ts(cancelled):
        result = "실패"
        result_type = "fail"
    else:
        result = "—"
        result_type = ""

    created_at = rec.get("created_at")
    date_str = created_at.strftime("%m/%d %H:%M") if isinstance(created_at, datetime) else "—"

    charge = rec.get("estimated_charge")
    price_str = f"예상 단가 {int(charge):,}원" if charge else "—"

    return {
        "key": str(rec["sid"]),
        "sid": f"SID-{rec['sid']}",
        "title": f"SID-{rec['sid']} · {rec.get('parent_name') or '학부모'} 학부모",
        "sub": f"{rec.get('child_name') or '학생'} · {rec.get('policy_name') or '—'} · 접수 {date_str}",
        "date": date_str,
        "status": status_label,
        "assignee": rec.get("admin_name") or "—",
        "timerMin": _timer_min(rec.get("deadline_at")),
        "reqCount": int(rec.get("requested_count") or 0),
        "applyCount": int(rec.get("applied_count") or 0),
        "confirmed": confirmed.strftime("%H:%M") if _is_real_ts(confirmed) else "—",
        "prob": None,  # LLM 예측 — 후속 작업
        "result": result,
        "resultType": result_type,
        "isNew": bool(rec.get("new_parent")) if rec.get("new_parent") is not None else None,
        "appCount": None,  # 누적 작성수 — parent_account_sid 별도 집계
        "confirmedCount": None,  # 누적 확정 — 별도 집계
        "lessonCount": None,  # 누적 수업 — 별도 집계 (visit_instance 등)
        "visitsAfter": None,  # 앱 진입 횟수 — 이벤트 분석
        "policy": rec.get("policy_name"),
        "price": price_str,
        "request": rec.get("parent_request_to_teacher") or "",
        "isUrgent": bool(rec.get("is_urgent")),
        "autoConfirm": bool(rec.get("auto_confirm")),
        "matchedTeacher": rec.get("matched_teacher_name") or "",
    }


@router.get("")
async def list_applications(limit: int = Query(30, le=100)) -> dict[str, Any]:
    replica = get_replica()
    rows = await replica.list_recent_recommendations(limit=limit)
    return {
        "count": len(rows),
        "rows": [_to_row(r) for r in rows],
    }


@router.get("/{sid}")
async def get_application(sid: str) -> dict[str, Any]:
    replica = get_replica()
    rec = await replica.get_recommendation(sid)
    if rec is None:
        raise HTTPException(status_code=404, detail="application not found")
    return _to_row(rec)


@router.get("/{sid}/teachers")
async def list_teachers(sid: str) -> dict[str, Any]:
    replica = get_replica()
    rows = await replica.list_recommendation_teachers(sid)
    teachers = []
    for r in rows:
        applied = bool(r.get("applied"))
        rejected = bool(r.get("rejected"))
        if applied:
            stat = "응답완료"
        elif rejected:
            stat = "거절"
        else:
            stat = "미응답"
        name = r.get("teacher_name") or f"선생님-{r.get('teacher_account_sid')}"
        responded_at = r.get("last_responded_at")
        teachers.append(
            {
                "teacher_account_sid": r.get("teacher_account_sid"),
                "name": name,
                "init": name[:1] if name else "?",
                "stat": stat,
                "responded_at": responded_at.isoformat() if _is_real_ts(responded_at) else None,
                # 활동정보(totalHours/subject/rating/reviewCount)와 viewed(프로필 열람) 는
                # 별도 테이블/이벤트 시스템 조회 필요 — 후속 작업
                "totalHours": None,
                "subject": None,
                "subjectHours": None,
                "rating": None,
                "reviewCount": None,
                "viewed": None,
            }
        )
    return {"sid": sid, "teachers": teachers}
