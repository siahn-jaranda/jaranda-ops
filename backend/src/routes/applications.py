"""신청서 조회 엔드포인트.

페이지 메인 테이블 + 단건 상세 + 선생님 목록을 제공.
디자인 핸드오프(design_handoff_jaranda_ops)의 statusKey/deadlineState/요청 조건 칩
등을 위한 파생 필드도 함께 노출.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.config import settings
from src.db import get_replica
from src.handler_store import get_handler_store, handler_store_available

router = APIRouter(prefix="/api/applications", tags=["applications"])
logger = logging.getLogger(__name__)

# 자란다 DB 타임스탬프는 KST naive로 저장됨 (Asia/Seoul, +09:00)
KST = timezone(timedelta(hours=9))


# recommendation.status (DB 코멘트 기준)
#   1: 신규추천 / 10: 접수안내 / 20: 선생님추천 / 30: 선생님확정
#   40: 방문가이드 / 90: 추천완료 / 99: 추천취소 / 100: 삭제됨 / 101: 임시저장
# 운영 화면은 "매칭 전 / 매칭 완료 / 취소" 3-단계로만 노출.
STATUS_META = {
    1:   ("pending", "매칭 전"),
    10:  ("pending", "매칭 전"),
    20:  ("pending", "매칭 전"),
    30:  ("pending", "매칭 전"),
    40:  ("matched", "매칭 완료"),
    90:  ("matched", "매칭 완료"),
    99:  ("cancelled", "취소"),
    100: ("cancelled", "취소"),
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


# preferable_teacher_gender: kr.jaranda.common.model.enumeration.Gender
#   0=UNISEX(커머스용) / 1=FEMALE / 2=MALE / 3=BOTH(추천용 "성별무관")
# 0/3은 선호 없음으로 칩 노출 안 함.
_GENDER_MAP = {1: "여성", 2: "남성"}

# regularity: kr.jaranda.common.model.enumeration.Regularity
#   0=NONE / 1=ONE_TIME(1회) / 2=REGULAR(정기) / 3=MULTIPLE_TIMES(다회차)
_REGULARITY_MAP = {1: "1회 수업", 2: "정기", 3: "다회차"}

# biweekly: kr.jaranda.common.model.enumeration.Biweekly  (1=WEEKLY, 2=BIWEEKLY)


def _parse_cancelled_reason(raw: Any) -> str:
    """cancelled_info JSON에서 사람이 읽는 reason만 추출. 파싱 실패하면 원문 그대로."""
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return raw.strip()
    elif isinstance(raw, dict):
        data = raw
    else:
        return ""
    return (data.get("reason") or "").strip()


def _region_from_address(addr_raw: Any) -> str:
    """parent_address 첫 두 토큰 + '특별시/광역시/자치도' 접미사 제거."""
    addr = (addr_raw or "").strip()
    if not addr:
        return ""
    parts = addr.split()
    if len(parts) >= 2:
        first = parts[0].replace("특별시", "").replace("광역시", "").replace("자치도", "").strip()
        return f"{first} {parts[1]}"
    return parts[0]


def to_snapshot_fields(rec: dict[str, Any]) -> dict[str, Any]:
    """raw recommendation row → matching_ops_application_snapshot 컬럼 매핑.

    memos.py가 메모 작성/삭제 시 자란다 replica에서 신청서를 fetch한 후 호출.
    """
    status_code = rec.get("status")
    status_key, status_label = STATUS_META.get(status_code, ("etc", f"상태{status_code}"))
    confirmed = rec.get("confirmed_at")
    cancelled = rec.get("cancelled_at")
    return {
        "child_name": (rec.get("child_name") or "").strip() or None,
        "region": _region_from_address(rec.get("parent_address")) or None,
        "status_key": status_key,
        "status_label": status_label,
        "request_chips": _request_chips(rec),
        "parent_request": (rec.get("parent_request_to_teacher") or "").strip() or None,
        "matched_teacher": (rec.get("matched_teacher_name") or "").strip() or None,
        "cancelled_reason": _parse_cancelled_reason(rec.get("cancelled_info")) or None,
        "is_urgent": bool(rec.get("is_urgent")),
        "auto_confirm": bool(rec.get("auto_confirm")),
        "re_recommend": bool(rec.get("re_recommend")),
        "app_created_at": rec.get("created_at"),
        "app_deadline_at": rec.get("deadline_at"),
        "app_confirmed_at": confirmed if _is_real_ts(confirmed) else None,
        "app_cancelled_at": cancelled if _is_real_ts(cancelled) else None,
    }


def _request_chips(rec: dict[str, Any]) -> list[str]:
    """카드 보조 정보 칩. 첫 칩은 정기성, 그 다음 정기수업이면 매주/격주, 이후 부가 조건."""
    chips: list[str] = []

    reg = rec.get("regularity")
    if reg in _REGULARITY_MAP:
        chips.append(_REGULARITY_MAP[reg])

    # 정기/다회차일 때만 매주/격주 의미 있음
    if reg in (2, 3):
        if rec.get("biweekly") == 1:
            chips.append("매주")
        elif rec.get("biweekly") == 2:
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


def _to_frontend_teacher(
    r: dict[str, Any],
    feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """recommendation_teachers row → 프론트 mapTeacher가 기대하는 형태.

    프론트(index.html)는 stat 문자열을 substring 매칭하므로 동일 라벨 유지.
    viewed/viewed_at: 부모님이 추천 이후 선생님 프로필을 본 이력 (teacher_profile_view).
    hours/profile: teacher 테이블 활동 정보. feedback: parent_feedback 집계.
    """
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
    name = r.get("teacher_name") or "이름 없음"
    responded_at = r.get("last_responded_at")
    viewed_at = r.get("viewed_at")
    viewed = _is_real_ts(viewed_at)
    fb = feedback or {}
    return {
        "teacher_account_sid": r.get("teacher_account_sid"),
        "name": name,
        "init": name[:1] if name else "?",
        "stat": stat,
        "responded_at": responded_at.isoformat() if _is_real_ts(responded_at) else None,
        "viewed": viewed,
        "viewed_at": viewed_at.isoformat() if viewed else None,
        "viewed_count": int(r.get("viewed_count") or 0) if viewed else 0,
        "total_hours": float(r.get("experience_hour") or 0),
        "play_hours": float(r.get("experience_hour_for_play") or 0),
        "study_hours": float(r.get("experience_hour_for_study") or 0),
        "profile_url": r.get("thumbnail_profile_url") or "",
        "review_count": int(fb.get("review_count") or 0),
        "recommend_count": int(fb.get("recommend_count") or 0),
        "recommend_rate": fb.get("recommend_rate"),  # None | float (0~100)
    }


def _to_row(
    rec: dict[str, Any],
    parent_history: dict[str, int] | None = None,
    teachers: list[dict[str, Any]] | None = None,
    handler: dict[str, Any] | None = None,
    feedback_map: dict[str, dict[str, Any]] | None = None,
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

    # 자녀 표기: 메인 자녀 + 추가 자녀 수
    child_name = (rec.get("child_name") or "").strip()
    additional_num = int(rec.get("additional_children_num") or 0)
    if not child_name:
        child_display = "자녀 미입력"
    elif additional_num >= 1:
        child_display = f"{child_name} 외 {additional_num}명"
    else:
        child_display = child_name

    # 신규/재이용: recommendation.new_parent는 2024-06-17 이후 항상 0(deprecated).
    # 같은 parent_account_sid의 이전 confirmed(status 40/90) 건수로 판단.
    is_new: bool | None
    if parent_history is None:
        is_new = None
    else:
        is_new = (parent_history.get("confirmed_count") or 0) == 0

    return {
        "key": str(rec["sid"]),
        "sid": f"SID-{rec['sid']}",
        "child": child_display,
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
            bool(is_new) if is_new is not None else False,
            timer,
            bool(rec.get("is_urgent")),
        ),
        "result": result,
        "resultType": result_type,
        "isNew": is_new,
        # parent 누적 이력 — get_parent_history_counts 결과 주입
        "appCount": (parent_history or {}).get("app_count"),
        "confirmedCount": (parent_history or {}).get("confirmed_count"),
        "lessonCount": (parent_history or {}).get("lesson_count"),
        "price": price_str,
        "request": free_request,
        "requestChips": chips,
        "requestedTeacherName": rec.get("requested_teacher_name") or "",
        "isUrgent": bool(rec.get("is_urgent")),
        "autoConfirm": bool(rec.get("auto_confirm")),
        "reRecommend": bool(rec.get("re_recommend")),
        "matchedTeacher": rec.get("matched_teacher_name") or "",
        "cancelledReason": _parse_cancelled_reason(rec.get("cancelled_info")),
        "parentMobile": (rec.get("parent_mobile") or "").strip(),
        # 카드/테이블 뷰에서 t1/t2 추천 상태 표시 — batch 주입
        "teachers": [
            _to_frontend_teacher(t, (feedback_map or {}).get(t.get("teacher_account_sid")))
            for t in (teachers or [])
        ],
        # 처리 담당 — matching_ops_handler 테이블에서 batch 주입
        "handler": handler,
    }


@router.get("")
async def list_applications(limit: int = Query(30, le=100)) -> dict[str, Any]:
    replica = get_replica()
    try:
        rows = await replica.list_recent_recommendations(limit=limit)
        parent_sids = list({r["parent_account_sid"] for r in rows if r.get("parent_account_sid")})
        history_map = await replica.get_parent_history_counts(parent_sids)
        rec_sids = [str(r["sid"]) for r in rows if r.get("sid") is not None]
        teachers_map = await replica.list_recommendation_teachers_batch(rec_sids)
        teacher_sids = list({
            t.get("teacher_account_sid")
            for ts in teachers_map.values()
            for t in ts
            if t.get("teacher_account_sid")
        })
        feedback_map = await replica.get_teacher_feedback_summary(teacher_sids)
    except Exception:
        logger.exception("list_applications failed")
        raise HTTPException(status_code=503, detail="replica query failed")

    handler_map: dict[str, dict[str, Any]] = {}
    if handler_store_available():
        try:
            handler_map = await get_handler_store().list_by_sids(rec_sids)
        except Exception:
            logger.exception("handler batch fetch failed (graceful)")

    return {
        "count": len(rows),
        "rows": [
            _to_row(
                r,
                history_map.get(r.get("parent_account_sid")),
                teachers_map.get(str(r.get("sid"))),
                handler_map.get(str(r.get("sid"))),
                feedback_map,
            )
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

    teachers: list[dict[str, Any]] = []
    try:
        teachers = await replica.list_recommendation_teachers(sid)
    except Exception:
        logger.exception("list_recommendation_teachers failed sid=%s", sid)

    feedback_map: dict[str, dict[str, Any]] = {}
    if teachers:
        try:
            feedback_map = await replica.get_teacher_feedback_summary(
                [t["teacher_account_sid"] for t in teachers if t.get("teacher_account_sid")]
            )
        except Exception:
            logger.exception("feedback summary failed sid=%s", sid)

    handler: dict[str, Any] | None = None
    if handler_store_available():
        try:
            handler = await get_handler_store().get(sid)
        except Exception:
            logger.exception("handler fetch failed sid=%s (graceful)", sid)

    return _to_row(
        rec,
        history_map.get(parent_sid) if parent_sid else None,
        teachers,
        handler,
        feedback_map,
    )


@router.get("/{sid}/teachers")
async def list_teachers(sid: str) -> dict[str, Any]:
    replica = get_replica()
    try:
        rows = await replica.list_recommendation_teachers(sid)
    except Exception:
        logger.exception("list_teachers failed sid=%s", sid)
        raise HTTPException(status_code=503, detail="replica query failed")

    for r in rows:
        if not r.get("teacher_name"):
            logger.warning(
                "teacher name missing — replica stale? account_sid=%s recommendation_sid=%s",
                r.get("teacher_account_sid"),
                sid,
            )

    feedback_map: dict[str, dict[str, Any]] = {}
    if rows:
        try:
            feedback_map = await replica.get_teacher_feedback_summary(
                [r["teacher_account_sid"] for r in rows if r.get("teacher_account_sid")]
            )
        except Exception:
            logger.exception("feedback summary failed sid=%s", sid)

    return {
        "sid": sid,
        "teachers": [
            _to_frontend_teacher(r, feedback_map.get(r.get("teacher_account_sid")))
            for r in rows
        ],
    }
