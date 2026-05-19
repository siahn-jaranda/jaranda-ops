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


# kr.jaranda.domain.requestform.desiredcost.DesiredCost — recommendation_teacher_wage_range.wage_range_type
# (label, teacher_min, teacher_max, parent_min, parent_max). MAX=100000은 사실상 상한 없음.
# DB에는 enum 외 deprecated 코드(STUDY_VETERAN, STUDY_PREMIUM, CARE_LEVEL_n)도 존재 — _DESIRED_COST_EXTRA 로 보강.
_DESIRED_COST_MAP: dict[str, dict[str, Any]] = {
    "NONE":                     {"label": "선호 없음",         "teacher_min": 0,     "teacher_max": None, "parent_min": 0,     "parent_max": None},
    "ALL_WAGE":                 {"label": "전체",              "teacher_min": 0,     "teacher_max": None, "parent_min": 0,     "parent_max": None},
    "CARE_FRIENDLY":            {"label": "돌봄 · 친근형",     "teacher_min": 0,     "teacher_max": 16000, "parent_min": 0,     "parent_max": 20000},
    "CARE_VETERAN":             {"label": "돌봄 · 베테랑",     "teacher_min": 16000, "teacher_max": None, "parent_min": 20000, "parent_max": None},
    "STUDY_FRIENDLY":           {"label": "학습 · 친근형",     "teacher_min": 0,     "teacher_max": 19000, "parent_min": 0,     "parent_max": 25000},
    "STUDY_HIGHLY_EXPERIENCED": {"label": "학습 · 경력 다수",  "teacher_min": 19000, "teacher_max": 29000, "parent_min": 25000, "parent_max": 35000},
    "STUDY_VETERAN":            {"label": "학습 · 베테랑",     "teacher_min": 29000, "teacher_max": None, "parent_min": 35000, "parent_max": None},
    # 아래는 운영 데이터에 남아있는 과거 코드 — 정확한 범위는 enum에 없어 라벨만 노출.
    "STUDY_PREMIUM":            {"label": "학습 · 프리미엄(과거)", "teacher_min": None, "teacher_max": None, "parent_min": None, "parent_max": None},
    "STUDY_MODERATE":           {"label": "학습 · 일반(과거)",   "teacher_min": None, "teacher_max": None, "parent_min": None, "parent_max": None},
    "STUDY_EXPERIENCED":        {"label": "학습 · 경험(과거)",   "teacher_min": None, "teacher_max": None, "parent_min": None, "parent_max": None},
    "CARE_LEVEL_1":             {"label": "돌봄 · 레벨 1(과거)", "teacher_min": None, "teacher_max": None, "parent_min": None, "parent_max": None},
    "CARE_LEVEL_2":             {"label": "돌봄 · 레벨 2(과거)", "teacher_min": None, "teacher_max": None, "parent_min": None, "parent_max": None},
    "CARE_LEVEL_3":             {"label": "돌봄 · 레벨 3(과거)", "teacher_min": None, "teacher_max": None, "parent_min": None, "parent_max": None},
    "CARE_LEVEL_4":             {"label": "돌봄 · 레벨 4(과거)", "teacher_min": None, "teacher_max": None, "parent_min": None, "parent_max": None},
}


# request_form_category와는 다른 차원의 시급 기준 과목군 (jrdtbl_subject_wage, 1~6).
# 프로세스 생존 동안 1회 fetch 후 캐시 — 매번 join 안 함.
_subject_cache: dict[int, str] | None = None


async def get_subject_map() -> dict[int, str]:
    global _subject_cache
    if _subject_cache is None:
        try:
            _subject_cache = await get_replica().list_subject_wages()
        except Exception:
            logger.exception("subject wages fetch failed (graceful)")
            _subject_cache = {}
    return _subject_cache


def _parse_subjects(rec: dict[str, Any], subject_map: dict[int, str]) -> list[dict[str, Any]]:
    """teacher_specialties (pipe-delimited subject_wage id) → [{id, name}] 리스트."""
    raw = rec.get("teacher_specialties")
    if not raw:
        return []
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for token in str(raw).split("|"):
        token = token.strip()
        if not token:
            continue
        try:
            sid = int(token)
        except ValueError:
            continue
        if sid in seen:
            continue
        name = subject_map.get(sid)
        if not name:
            continue
        seen.add(sid)
        out.append({"id": sid, "name": name})
    return out


def _wage_range_objects(types: list[str] | None) -> list[dict[str, Any]]:
    """DesiredCost 코드 리스트 → 프론트 노출용 객체 리스트."""
    if not types:
        return []
    out: list[dict[str, Any]] = []
    for t in types:
        meta = _DESIRED_COST_MAP.get(t)
        if not meta:
            out.append({"code": t, "label": t, "teacherMin": None, "teacherMax": None, "parentMin": None, "parentMax": None})
            continue
        out.append({
            "code": t,
            "label": meta["label"],
            "teacherMin": meta["teacher_min"],
            "teacherMax": meta["teacher_max"],
            "parentMin": meta["parent_min"],
            "parentMax": meta["parent_max"],
        })
    return out


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


def to_snapshot_fields(
    rec: dict[str, Any],
    subject_map: dict[int, str],
    wage_range_types: list[str] | None = None,
) -> dict[str, Any]:
    """raw recommendation row → matching_ops_application_snapshot 컬럼 매핑.

    memos.py가 메모 작성/삭제 시 자란다 replica에서 신청서를 fetch한 후 호출.
    """
    status_code = rec.get("status")
    status_key, status_label = STATUS_META.get(status_code, ("etc", f"상태{status_code}"))
    confirmed = rec.get("confirmed_at")
    cancelled = rec.get("cancelled_at")
    subjects = _parse_subjects(rec, subject_map)
    wage_ranges = _wage_range_objects(wage_range_types)
    return {
        "child_name": (rec.get("child_name") or "").strip() or None,
        "region": _region_from_address(rec.get("parent_address")) or None,
        "status_key": status_key,
        "status_label": status_label,
        "subjects": json.dumps(subjects, ensure_ascii=False) if subjects else None,
        "wage_ranges": json.dumps(wage_ranges, ensure_ascii=False) if wage_ranges else None,
        "request_chips": _request_chips(rec, subjects),
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


def _request_chips(rec: dict[str, Any], subjects: list[dict[str, Any]] | None = None) -> list[str]:
    """카드 보조 정보 칩. 맨 앞에 수업 과목(있으면) → 정기성 → 매주/격주 → 기타."""
    chips: list[str] = []

    if subjects:
        chips.append(", ".join(s["name"] for s in subjects))

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
    teacher_wages: dict[int, dict[str, int]] | None = None,
    subjects: list[dict[str, Any]] | None = None,
    alimtalk: dict[str, Any] | None = None,
    active_visit_count: int | None = None,
) -> dict[str, Any]:
    """recommendation_teachers row → 프론트 mapTeacher가 기대하는 형태.

    프론트(index.html)는 stat 문자열을 substring 매칭하므로 동일 라벨 유지.
    viewed/viewed_at: 부모님이 추천 이후 선생님 프로필을 본 이력 (teacher_profile_view).
    hours/profile: teacher 테이블 활동 정보. feedback: parent_feedback 집계.
    wage_by_subject: 신청서 과목(subject_wage_id) 기준 선생님의 현재 시급.
    """
    applied = bool(r.get("applied"))
    accepted = bool(r.get("accepted"))
    rejected = bool(r.get("rejected"))
    requested = bool(r.get("requested"))
    # 자란다 도메인: accepted=추천요청 수락, applied=후속 지원 단계.
    # applied=1이면 이미 accepted 이상의 진전. 화면에선 진전된 상태를 우선 표기.
    if applied:
        stat = "지원"
    elif accepted:
        stat = "수락"
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

    wage_by_subject: list[dict[str, Any]] = []
    if subjects and teacher_wages:
        for s in subjects:
            w = teacher_wages.get(s["id"])
            if not w:
                continue
            wage_by_subject.append({
                "subjectId": s["id"],
                "subjectName": s["name"],
                "teacherWage": w["teacher_wage"],
                "parentCharge": w["parent_charge"],
            })

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
        "university": (r.get("university") or "").strip(),
        "major": (r.get("major") or "").strip(),
        "review_count": int(fb.get("review_count") or 0),
        "recommend_count": int(fb.get("recommend_count") or 0),
        "recommend_rate": fb.get("recommend_rate"),  # None | float (0~100)
        "wage_by_subject": wage_by_subject,
        "alimtalk_count": int((alimtalk or {}).get("count") or 0),
        "alimtalk_last_sent_at": (alimtalk or {}).get("last_sent_at").isoformat() if alimtalk and alimtalk.get("last_sent_at") else None,
        "alimtalk_last_template": (alimtalk or {}).get("last_template") or "",
        "active_visit_count": int(active_visit_count or 0),
    }


def _to_row(
    rec: dict[str, Any],
    subject_map: dict[int, str],
    parent_history: dict[str, int] | None = None,
    teachers: list[dict[str, Any]] | None = None,
    handler: dict[str, Any] | None = None,
    feedback_map: dict[str, dict[str, Any]] | None = None,
    wage_range_types: list[str] | None = None,
    teacher_wages_map: dict[str, dict[int, dict[str, int]]] | None = None,
    alimtalk_map: dict[tuple[str, str], dict[str, Any]] | None = None,
    visit_counts_map: dict[str, int] | None = None,
) -> dict[str, Any]:
    """DB row → 페이지 사용 스키마.

    실데이터: status, statusKey, deadlineState, assignee, request, region,
            schedule, preferableGender, requestedTeacherName, price,
            subjects, wageRanges, teachers[].wageBySubject,
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
    # KST 명시 ISO + 전체 표기 (상세 패널용)
    if isinstance(created_at, datetime):
        ca = created_at if created_at.tzinfo else created_at.replace(tzinfo=KST)
        created_at_iso = ca.isoformat()
        created_at_full = ca.strftime("%Y-%m-%d %H:%M")
    else:
        created_at_iso = None
        created_at_full = "—"

    charge = rec.get("estimated_charge")
    price_str = f"{int(charge):,}원" if charge else "—"

    timer = _timer_min(rec.get("deadline_at"))
    deadline_state = _deadline_state(timer)
    deadline_label = _deadline_label(timer)

    # 지역: parent_address 첫 두 토큰 (예: '서울특별시 마포구 ...' → '서울 마포구')
    region = _region_from_address(rec.get("parent_address"))

    # 과목 + 시급 범위
    subjects = _parse_subjects(rec, subject_map)
    wage_ranges = _wage_range_objects(wage_range_types)

    # 요청사항 칩 (정형 조건) — 첫 칩은 수업 과목
    chips = _request_chips(rec, subjects)

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
        "createdAtIso": created_at_iso,
        "createdAtFull": created_at_full,
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
        "subjects": subjects,
        "wageRanges": wage_ranges,
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
            _to_frontend_teacher(
                t,
                (feedback_map or {}).get(t.get("teacher_account_sid")),
                (teacher_wages_map or {}).get(str(t.get("teacher_account_sid") or "")),
                subjects,
                (alimtalk_map or {}).get((str(rec["sid"]), str(t.get("teacher_account_sid") or ""))),
                (visit_counts_map or {}).get(str(t.get("teacher_account_sid") or "")),
            )
            for t in (teachers or [])
        ],
        # 처리 담당 — matching_ops_handler 테이블에서 batch 주입
        "handler": handler,
    }


_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(name: str, value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not _DATE_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD")
    return value


@router.get("")
async def list_applications(
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0, le=100000),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> dict[str, Any]:
    date_from = _validate_date("from", date_from)
    date_to = _validate_date("to", date_to)
    replica = get_replica()
    try:
        rows = await replica.list_recent_recommendations(
            limit=limit, offset=offset, date_from=date_from, date_to=date_to
        )
        parent_sids = list({r["parent_account_sid"] for r in rows if r.get("parent_account_sid")})
        history_map = await replica.get_parent_history_counts(parent_sids)
        rec_sids = [str(r["sid"]) for r in rows if r.get("sid") is not None]
        teachers_map = await replica.list_recommendation_teachers_batch(rec_sids)
        teacher_sids = list({
            str(t.get("teacher_account_sid"))
            for ts in teachers_map.values()
            for t in ts
            if t.get("teacher_account_sid")
        })
        feedback_map = await replica.get_teacher_feedback_summary(teacher_sids)
        wage_range_map = await replica.list_wage_ranges(rec_sids)

        # 화면에 노출되는 모든 신청서의 teacher_specialties를 합쳐 batch 시급 조회
        all_subject_ids: set[int] = set()
        for r in rows:
            raw = r.get("teacher_specialties")
            if not raw:
                continue
            for tok in str(raw).split("|"):
                tok = tok.strip()
                if tok.isdigit():
                    all_subject_ids.add(int(tok))
        teacher_wages_map = await replica.list_teacher_subject_wages(
            teacher_sids, sorted(all_subject_ids)
        )
        alimtalk_map = await replica.list_alimtalk_to_teachers(rec_sids, teacher_sids)
        visit_counts_map = await replica.list_active_visit_counts(teacher_sids)
    except Exception:
        logger.exception("list_applications failed")
        raise HTTPException(status_code=503, detail="replica query failed")

    subject_map = await get_subject_map()

    handler_map: dict[str, dict[str, Any]] = {}
    if handler_store_available():
        try:
            handler_map = await get_handler_store().list_by_sids(rec_sids)
        except Exception:
            logger.exception("handler batch fetch failed (graceful)")

    return {
        "count": len(rows),
        "filter": {"from": date_from, "to": date_to, "limit": limit, "offset": offset},
        "hasMore": len(rows) >= limit,
        "rows": [
            _to_row(
                r,
                subject_map,
                history_map.get(r.get("parent_account_sid")),
                teachers_map.get(str(r.get("sid"))),
                handler_map.get(str(r.get("sid"))),
                feedback_map,
                wage_range_map.get(str(r.get("sid"))),
                teacher_wages_map,
                alimtalk_map,
                visit_counts_map,
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

    wage_range_types: list[str] = []
    try:
        wage_map = await replica.list_wage_ranges([sid])
        wage_range_types = wage_map.get(sid, [])
    except Exception:
        logger.exception("wage range fetch failed sid=%s (graceful)", sid)

    subject_map = await get_subject_map()
    subject_ids = [s["id"] for s in _parse_subjects(rec, subject_map)]
    teacher_sids = [t["teacher_account_sid"] for t in teachers if t.get("teacher_account_sid")]
    teacher_wages_map: dict[str, dict[int, dict[str, int]]] = {}
    if subject_ids and teacher_sids:
        try:
            teacher_wages_map = await replica.list_teacher_subject_wages(
                teacher_sids, subject_ids
            )
        except Exception:
            logger.exception("teacher wages fetch failed sid=%s (graceful)", sid)

    alimtalk_map: dict[tuple[str, str], dict[str, Any]] = {}
    visit_counts_map: dict[str, int] = {}
    if teacher_sids:
        try:
            alimtalk_map = await replica.list_alimtalk_to_teachers([sid], teacher_sids)
        except Exception:
            logger.exception("alimtalk fetch failed sid=%s (graceful)", sid)
        try:
            visit_counts_map = await replica.list_active_visit_counts(teacher_sids)
        except Exception:
            logger.exception("visit count fetch failed sid=%s (graceful)", sid)

    handler: dict[str, Any] | None = None
    if handler_store_available():
        try:
            handler = await get_handler_store().get(sid)
        except Exception:
            logger.exception("handler fetch failed sid=%s (graceful)", sid)

    return _to_row(
        rec,
        subject_map,
        history_map.get(parent_sid) if parent_sid else None,
        teachers,
        handler,
        feedback_map,
        wage_range_types,
        teacher_wages_map,
        alimtalk_map,
        visit_counts_map,
    )


@router.get("/{sid}/teachers")
async def list_teachers(sid: str) -> dict[str, Any]:
    replica = get_replica()
    try:
        rec = await replica.get_recommendation(sid)
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

    subject_map = await get_subject_map()
    subjects = _parse_subjects(rec, subject_map) if rec else []
    teacher_sids = [r["teacher_account_sid"] for r in rows if r.get("teacher_account_sid")]
    teacher_wages_map: dict[str, dict[int, dict[str, int]]] = {}
    if subjects and teacher_sids:
        try:
            teacher_wages_map = await replica.list_teacher_subject_wages(
                teacher_sids, [s["id"] for s in subjects]
            )
        except Exception:
            logger.exception("teacher wages fetch failed sid=%s (graceful)", sid)

    alimtalk_map: dict[tuple[str, str], dict[str, Any]] = {}
    visit_counts_map: dict[str, int] = {}
    if teacher_sids:
        try:
            alimtalk_map = await replica.list_alimtalk_to_teachers([sid], teacher_sids)
        except Exception:
            logger.exception("alimtalk fetch failed sid=%s (graceful)", sid)
        try:
            visit_counts_map = await replica.list_active_visit_counts(teacher_sids)
        except Exception:
            logger.exception("visit count fetch failed sid=%s (graceful)", sid)

    return {
        "sid": sid,
        "teachers": [
            _to_frontend_teacher(
                r,
                feedback_map.get(r.get("teacher_account_sid")),
                teacher_wages_map.get(str(r.get("teacher_account_sid") or "")),
                subjects,
                alimtalk_map.get((sid, str(r.get("teacher_account_sid") or ""))),
                visit_counts_map.get(str(r.get("teacher_account_sid") or "")),
            )
            for r in rows
        ],
    }
