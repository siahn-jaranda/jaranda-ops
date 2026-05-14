"""신청서 조회 엔드포인트.

페이지 메인 테이블 + 단건 상세 + 선생님 목록을 제공.
디자인 핸드오프(design_handoff_jaranda_ops)의 statusKey/deadlineState/요청 조건 칩
등을 위한 파생 필드도 함께 노출.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.config import settings
from src.db import get_replica

router = APIRouter(prefix="/api/applications", tags=["applications"])
logger = logging.getLogger(__name__)

# 자란다 DB 타임스탬프는 KST naive로 저장됨 (Asia/Seoul, +09:00)
KST = timezone(timedelta(hours=9))


# 자란다 콘솔 status 코드 → 핸드오프 statusKey + 한국어 라벨.
# 핸드오프 STATUS_META: recommending(추천중) / review(부모님 확인) / searching(선생님 미정)
# / matched(매칭 완료) / cancelled
# 메모리 reference_recommendation_status: 10/20/40/90/99/100/101 외 코드는 "기타"로 노출
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
    """KST 기준 마감까지 남은 분. 자란다 DB 타임스탬프는 KST naive."""
    if not _is_real_ts(deadline):
        return None
    dl = deadline if deadline.tzinfo else deadline.replace(tzinfo=KST)
    delta = (dl - datetime.now(KST)).total_seconds() / 60
    if delta <= 0:
        return None
    return int(delta)


def _deadline_state(timer_min: int | None) -> str:
    """DeadlineTag state. 임계값은 settings에서 주입."""
    if timer_min is None:
        return "ok"
    if timer_min < settings.urgent_threshold_min:
        return "urgent"
    if timer_min < settings.soon_threshold_min:
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


def _compute_prob(
    confirmed: Any,
    cancelled: Any,
    applied_count: int,
    requested_count: int,
    is_new: bool,
    timer_min: int | None,
    is_urgent: bool,
) -> dict[str, Any]:
    """매칭 확률을 dict로 반환. {value: 0~100, source: heuristic|actual}.

    LLM 예측 미구현 — 휴리스틱: base 30 + 지원 응답률(최대 +45) + 재이용(+10)
    + 지명·요청 모수(최대 +5) + 마감 여유/임박(±15) + 긴급(-10).
    confirmed/cancelled 시점 정보가 있으면 source=actual로 마킹.
    """
    if _is_real_ts(confirmed):
        return {"value": 100, "source": "actual"}
    if _is_real_ts(cancelled):
        return {"value": 0, "source": "actual"}

    score = 30
    if applied_count >= 1:
        score += 25
    if applied_count >= 2:
        score += 12
    if applied_count >= 3:
        score += 8
    if requested_count >= 3:
        score += 5

    if not is_new:
        score += 10

    if timer_min is not None:
        if timer_min < settings.urgent_threshold_min:
            score -= 15
        elif timer_min < settings.soon_threshold_min:
            score -= 5
        elif timer_min > 2 * settings.soon_threshold_min:
            score += 5

    if is_urgent:
        score -= 10

    return {"value": max(5, min(100, score)), "source": "heuristic"}


def _to_row(
    rec: dict[str, Any],
    parent_history: dict[str, int] | None = None,
) -> dict[str, Any]:
    """DB row → 페이지 사용 스키마.

    실데이터: status, statusKey, deadlineState, assignee, request, region,
            schedule, preferableGender, requestedTeacherName, price,
            appCount/confirmedCount/lessonCount(parent_history 주입 시).
    추정: prob (휴리스틱, source=heuristic으로 마킹).
    """
    status_code = rec.get("status")
    status_key, status_label = STATUS_META.get(status_code, ("etc", f"상태{status_code}"))
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
        "sub": f"{rec.get('child_name') or '학생'} · {rec.get('policy_name') or '정책 미연결'} · 접수 {date_str}",
        "date": date_str,
        # 핸드오프 매핑
        "status": status_label,
        "statusKey": status_key,
        "deadlineState": deadline_state,
        "deadlineLabel": deadline_label,
        "region": region,
        "assignee": rec.get("admin_name") or "—",
        "timerMin": timer,
        "reqCount": int(rec.get("requested_count") or 0),
        "applyCount": int(rec.get("applied_count") or 0),
        "confirmed": confirmed.strftime("%H:%M") if _is_real_ts(confirmed) else "—",
        # prob: { value: 0~100, source: heuristic|actual }
        "prob": _compute_prob(
            confirmed, cancelled,
            int(rec.get("applied_count") or 0),
            int(rec.get("requested_count") or 0),
            bool(rec.get("new_parent")),
            timer,
            bool(rec.get("is_urgent")),
        ),
        "result": result,
        "resultType": result_type,
        "isNew": bool(rec.get("new_parent")) if rec.get("new_parent") is not None else None,
        # parent 누적 이력 — get_parent_history_counts 결과 주입
        "appCount": (parent_history or {}).get("app_count"),
        "confirmedCount": (parent_history or {}).get("confirmed_count"),
        "lessonCount": (parent_history or {}).get("lesson_count"),
        "policy": rec.get("policy_name"),
        "price": price_str,
        "request": free_request,
        "requestChips": chips,
        "requestedTeacherName": rec.get("requested_teacher_name") or "",
        "isUrgent": bool(rec.get("is_urgent")),
        "autoConfirm": bool(rec.get("auto_confirm")),
        "matchedTeacher": rec.get("matched_teacher_name") or "",
    }


@router.get("")
async def list_applications(limit: int = Query(30, le=100)) -> dict[str, Any]:
    replica = get_replica()
    try:
        rows = await replica.list_recent_recommendations(limit=limit)
        parent_sids = list({r["parent_account_sid"] for r in rows if r.get("parent_account_sid")})
        history_map = await replica.get_parent_history_counts(parent_sids)
    except Exception:
        logger.exception("list_applications failed")
        raise HTTPException(status_code=503, detail="replica query failed")

    return {
        "count": len(rows),
        "rows": [
            _to_row(r, history_map.get(r.get("parent_account_sid")))
            for r in rows
        ],
    }


@router.get("/{sid}")
async def get_application(sid: str) -> dict[str, Any]:
    replica = get_replica()
    try:
        rec = await replica.get_recommendation(sid)
    except Exception:
        logger.exception("get_application failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="replica query failed")

    if rec is None:
        raise HTTPException(status_code=404, detail="application not found")

    parent_sid = rec.get("parent_account_sid")
    history_map: dict[str, dict[str, int]] = {}
    if parent_sid:
        try:
            history_map = await replica.get_parent_history_counts([parent_sid])
        except Exception:
            logger.exception("get_parent_history_counts failed sid=%s", parent_sid)

    return _to_row(rec, history_map.get(parent_sid) if parent_sid else None)


@router.get("/{sid}/teachers")
async def list_teachers(sid: str) -> dict[str, Any]:
    replica = get_replica()
    try:
        rows = await replica.list_recommendation_teachers(sid)
    except Exception:
        logger.exception("list_teachers failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="replica query failed")

    teachers = []
    for r in rows:
        applied = bool(r.get("applied"))
        rejected = bool(r.get("rejected"))
        requested = bool(r.get("requested"))
        if applied:
            stat = "응답완료"
        elif rejected:
            stat = "거절"
        elif requested:
            stat = "요청됨"
        else:
            stat = "미응답"
        teacher_name = r.get("teacher_name")
        if not teacher_name:
            logger.warning(
                "teacher name missing — replica stale? account_sid=%s recommendation_sid=%s",
                r.get("teacher_account_sid"),
                sid,
            )
            name = "이름 없음"
        else:
            name = teacher_name
        responded_at = r.get("last_responded_at")
        teachers.append(
            {
                "teacher_account_sid": r.get("teacher_account_sid"),
                "name": name,
                "init": name[:1] if name else "?",
                "stat": stat,
                "responded_at": responded_at.isoformat() if _is_real_ts(responded_at) else None,
            }
        )
    return {"sid": sid, "teachers": teachers}
