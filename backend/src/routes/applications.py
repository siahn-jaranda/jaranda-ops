"""신청서 조회 엔드포인트.

페이지 메인 테이블 + 단건 상세 + 선생님 목록을 제공.
디자인 핸드오프(design_handoff_jaranda_ops)의 statusKey/deadlineState/요청 조건 칩
등을 위한 파생 필드도 함께 노출.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.db import get_replica

router = APIRouter(prefix="/api/applications", tags=["applications"])


# 자란다 콘솔 status 코드 → 핸드오프 statusKey + 한국어 라벨.
# 핸드오프 STATUS_META: recommending(추천중) / review(부모님 확인) / searching(선생님 미정)
# / matched(매칭 완료) / cancelled
STATUS_META = {
    10: ("recommending", "진행중"),
    20: ("review", "부모님 확인"),
    40: ("matched", "매칭완료"),
    90: ("matched", "매칭완료"),
    99: ("cancelled", "취소"),
    100: ("cancelled", "취소"),
    101: ("cancelled", "취소"),
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


def _deadline_state(timer_min: int | None) -> str:
    """핸드오프 DeadlineTag state: urgent(<4h) / soon(<24h) / ok(그 외)."""
    if timer_min is None:
        return "ok"
    if timer_min < 4 * 60:
        return "urgent"
    if timer_min < 24 * 60:
        return "soon"
    return "ok"


def _deadline_label(timer_min: int | None) -> str:
    if timer_min is None:
        return "—"
    if timer_min < 60:
        return f"{timer_min}분 남음"
    h = timer_min // 60
    m = timer_min % 60
    if h < 24:
        return f"{h}시간 {m}분 남음" if m else f"{h}시간 남음"
    d = h // 24
    rh = h % 24
    return f"{d}일 {rh}시간 남음" if rh else f"{d}일 남음"


_GENDER_MAP = {1: "여성", 2: "남성"}


def _request_chips(rec: dict[str, Any]) -> list[str]:
    """핸드오프 '추가 요청' 칩 그룹용. parent_request + 정형 조건들을 칩으로."""
    chips: list[str] = []

    if rec.get("biweekly") == 0:
        chips.append("매주")
    elif rec.get("biweekly") == 1:
        chips.append("격주")

    term = rec.get("regular_visit_term")
    if term:
        chips.append(f"주 {term}회")

    g = rec.get("preferable_teacher_gender")
    if g in _GENDER_MAP:
        chips.append(f"{_GENDER_MAP[g]} 선생님 선호")

    first = rec.get("requested_first_visit_schedule")
    if first:
        chips.append(f"첫방문 {first}")

    chars = (rec.get("preferable_teacher_characteristics") or "").strip()
    if chars:
        chips.append(chars)

    return chips


def _to_row(rec: dict[str, Any]) -> dict[str, Any]:
    """DB row → 페이지 사용 스키마.

    실데이터 있음: status, statusKey, deadlineState, assignee, request, region,
                  schedule(격주/정기), preferableGender, requestedTeacherName, price
    아직 mock/null: prob (LLM 예측), appCount/confirmedCount/lessonCount/visitsAfter
                  (parent 누적 집계 후속), viewed/totalHours/rating (선생님 활동·이벤트 후속)
    """
    status_code = rec.get("status")
    status_key, status_label = STATUS_META.get(status_code, ("review", f"상태{status_code}"))
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
    price_str = f"{int(charge):,}원" if charge else "—"

    timer = _timer_min(rec.get("deadline_at"))
    deadline_state = _deadline_state(timer)
    deadline_label = _deadline_label(timer)

    # 지역: parent_address 첫 두 토큰 (예: '서울특별시 마포구 ...' → '서울 마포구')
    addr = (rec.get("parent_address") or "").strip()
    region = ""
    if addr:
        parts = addr.split()
        if len(parts) >= 2:
            simple_first = parts[0].replace("특별시", "").replace("광역시", "").replace("자치도", "").strip()
            region = f"{simple_first} {parts[1]}"
        else:
            region = parts[0]

    # 요청사항 칩 (정형 조건)
    chips = _request_chips(rec)

    # 자유 텍스트 요청
    free_request = (rec.get("parent_request_to_teacher") or "").strip()

    return {
        "key": str(rec["sid"]),
        "sid": f"SID-{rec['sid']}",
        "title": f"SID-{rec['sid']} · {rec.get('parent_name') or '학부모'} 학부모",
        "sub": f"{rec.get('child_name') or '학생'} · {rec.get('policy_name') or '—'} · 접수 {date_str}",
        "date": date_str,
        # 핸드오프 매핑
        "status": status_label,
        "statusKey": status_key,
        "deadlineState": deadline_state,
        "deadlineLabel": deadline_label,
        "region": region,
        # 기존 mock 스키마 호환 필드
        "assignee": rec.get("admin_name") or "—",
        "timerMin": timer,
        "reqCount": int(rec.get("requested_count") or 0),
        "applyCount": int(rec.get("applied_count") or 0),
        "confirmed": confirmed.strftime("%H:%M") if _is_real_ts(confirmed) else "—",
        "prob": None,  # LLM 예측 — 후속 작업
        "result": result,
        "resultType": result_type,
        "isNew": bool(rec.get("new_parent")) if rec.get("new_parent") is not None else None,
        "appCount": None,
        "confirmedCount": None,
        "lessonCount": None,
        "visitsAfter": None,
        "policy": rec.get("policy_name"),
        "price": price_str,
        "request": free_request,
        "requestChips": chips,
        "requestedTeacherName": rec.get("requested_teacher_name") or "",
        "isUrgent": bool(rec.get("is_urgent")),
        "autoConfirm": bool(rec.get("auto_confirm")),
        "matchedTeacher": rec.get("matched_teacher_name") or "",
        # 핸드오프 ViewedSummary용 — 이벤트 시스템 후속. placeholder 노출.
        "viewedSummary": None,
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
