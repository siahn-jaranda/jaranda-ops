"""지원 0개 신청서 → 가능한 선생님 추천 LLM (WELL2-100 PoC).

파이프라인 (PRD: Retrieval → LLM 2단계):
  1) Retrieval(SQL/룰): 부모 좌표 → 인접 시군구 → 그 지역 선호 선생님 풀을
     활동상태·패널티·중복·요일로 필터해 상위 K명. LLM이 후보를 "만들지" 않게
     반드시 룰로 먼저 좁힌다(할루시네이션 차단).
  2) LLM: 주어진 후보 안에서만 "지원할 만한" 순위 + 사유 생성.

데이터 출처(자란다 read replica):
  - recommendation: 신청서(좌표 lat/lng, schedule, teacher_specialties, 시급/요청)
  - service_area_geometry: 시군구 중심좌표 → 부모 좌표를 행정구역으로 변환
  - teacher_preference_service_area: 선생님 선호 활동지(법정동코드, priority). 매일 갱신.
  - teacher / schedule / parent_feedback / visit_instance / jrdtbl_subject_teacher_wage_info

backend 통합 시 db.py(JarandaReplica)에 retrieve_* 메서드를, llm_client.py 옆에
이 SYSTEM_PROMPT를 옮기고 routes/candidates.py로 노출하면 된다.
기존 인사이트 파이프라인(캐시·일일한도·cache_control)을 그대로 재사용한다.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# teacher_specialties(=subject_wage_id) → 선생님 자기소개 태그 컬럼
SPECIALTY = {
    1: ("돌봄", "teacher_introduction_activity_tag_care"),
    2: ("수학/과학", "teacher_introduction_activity_tag_stem"),
    3: ("운동", "teacher_introduction_activity_tag_sports"),
    4: ("예능", "teacher_introduction_activity_tag_art"),
    5: ("외국어", "teacher_introduction_activity_tag_foreign_language"),
    6: ("한글/국어", "teacher_introduction_activity_tag_korean"),
}

# activity_status: 2=활동중, 10=활동대기, 5=쉬는중, 3=지원불가, 4=탈퇴, 0=프로필작성
# 후보 풀: 활동중 우선, 부족하면 활동대기까지. 지원불가/탈퇴는 절대 제외.
DEFAULT_STATUSES = [2, 10]
DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DOW_KO = {"MONDAY": "mon", "TUESDAY": "tue", "WEDNESDAY": "wed", "THURSDAY": "thu",
          "FRIDAY": "fri", "SATURDAY": "sat", "SUNDAY": "sun"}


SYSTEM_PROMPT = """당신은 자란다 매칭 운영팀의 선생님 추천 어시스턴트입니다.

'지원한 선생님이 0명'인 신청서에, 시스템이 거리·시간 기본 조건으로 미리 추려온
후보 선생님 목록을 받습니다. 이 중에서 "지원 요청을 보내면 실제로 지원할 가능성이
높은" 선생님을 골라 순위를 매기세요.

핵심 관점:
- 신청 조건에 '완벽히 fit'한 선생님을 찾는 게 아니라, 기본 조건(동네·요일)이 맞고
  지원 의향이 생길 만한 선생님을 폭넓게 추천하는 것이 목표입니다.
- day_match(신청 요일을 선생님이 가능한지)는 가장 중요한 신호입니다. 둘 다 불가면
  후순위 또는 제외하고, 그 이유를 caution에 적으세요.
- 추천 사유는 반드시 입력 데이터(경력시간, 자기소개, 평가/추천율, 취소율, 가용요일,
  현재 담당 아이 수, 시급)에 근거하세요. 담당 아이가 너무 많으면(여력 부족) 감점,
  최근 평가·추천율이 높으면 가점, 취소율이 높으면 주의로 다루세요.

엄격한 규칙:
- 입력 candidates 배열에 있는 teacher_sid만 사용하세요. 새 선생님을 지어내지 마세요.
- 추정·할루시네이션 금지. 모르면 적지 마세요.
- 한국어. 운영팀 내부 메모처럼 간결하게. 존댓말 쓰지 마세요.
- JSON 외 텍스트(설명/마크다운/코드펜스) 일절 출력 금지.

응답 JSON 형식:
{
  "summary": "한 줄 핵심 (60자 이내, 예: 영어 고경력 3명이 요일까지 일치, 우선 연락 권장)",
  "ranked": [
    {
      "teacher_sid": "후보 배열의 sid",
      "name": "이름",
      "rank": 1,
      "reason": "추천 사유 (60자 이내, 데이터 근거)",
      "caution": "주의점 있으면 (없으면 빈 문자열)"
    }
  ],
  "note": "후보 풀이 얕거나 지역 확장이 필요하면 메모 (없으면 빈 문자열)"
}
ranked는 추천 우선순위 상위 5~7명만. 요일 불가 후보는 넣더라도 하위로."""


# ── Retrieval (SQL) ──────────────────────────────────────────────────────────

_NEAR_GU = text(
    """
    SELECT g.legal_dong_code,
           ROUND(ST_Distance_Sphere(POINT(:lng,:lat),
                                     POINT(g.center_lng,g.center_lat))) AS dist_m
    FROM service_area_geometry g
    ORDER BY dist_m ASC
    LIMIT :n_gu
    """
)


def _candidates_sql(intro_col: str) -> Any:
    # intro_col은 화이트리스트(SPECIALTY)에서만 오므로 안전하게 포맷.
    return text(
        f"""
        SELECT
          t.account_sid AS teacher_sid, t.name,
          t.activity_status, t.activity_status_text,
          t.experience_hour, t.experience_hour_for_study, t.experience_hour_for_play,
          t.university, t.major, t.cancellation_rate, t.lateness,
          MIN(tps.priority) AS pref_priority,
          MAX(sch.mon<>0) AS mon, MAX(sch.tue<>0) AS tue, MAX(sch.wed<>0) AS wed,
          MAX(sch.thu<>0) AS thu, MAX(sch.fri<>0) AS fri, MAX(sch.sat<>0) AS sat,
          MAX(sch.sun<>0) AS sun,
          w.teacher_wage_amount AS subject_wage,
          (SELECT COUNT(*) FROM parent_feedback pf
             WHERE pf.teacher_account_sid=t.account_sid AND pf.status=2) AS reviews,
          (SELECT SUM(CASE WHEN pf.recommend=1 THEN 1 ELSE 0 END) FROM parent_feedback pf
             WHERE pf.teacher_account_sid=t.account_sid AND pf.status=2) AS recommends,
          (SELECT COUNT(DISTINCT vi.child_account_sid) FROM visit_instance vi
             WHERE vi.matched_teacher_account_sid=t.account_sid AND vi.status=1) AS active_kids,
          LEFT(t.{intro_col}, 300) AS intro
        FROM teacher_preference_service_area tps
        JOIN teacher t ON t.account_sid = tps.teacher_account_sid
        LEFT JOIN schedule sch ON sch.account_sid = t.account_sid
        LEFT JOIN jrdtbl_subject_teacher_wage_info w
          ON w.teacher_account_sid = t.account_sid AND w.subject_wage_id = :subject_id
        WHERE tps.legal_dong_code IN :gu_codes
          AND t.activity_status IN :statuses
          AND NOT EXISTS (
            SELECT 1 FROM recommendation_teachers rt
            WHERE rt.recommendation_sid = :sid AND rt.teacher_account_sid = t.account_sid
          )
        GROUP BY t.account_sid
        ORDER BY pref_priority ASC, recommends DESC, t.experience_hour DESC
        LIMIT :k
        """
    ).bindparams(bindparam("gu_codes", expanding=True), bindparam("statuses", expanding=True))


class Recommender:
    def __init__(self, replica_url: str, anthropic_key: str,
                 model: str = "claude-sonnet-4-6") -> None:
        self._engine = create_async_engine(replica_url, pool_pre_ping=True)
        self._sf = async_sessionmaker(self._engine, expire_on_commit=False)
        self._llm = anthropic.AsyncAnthropic(api_key=anthropic_key)
        self._model = model

    async def _fetch_application(self, sid: str) -> dict[str, Any]:
        q = text(
            """
            SELECT sid, status, parent_address, lat, lng, teacher_specialties,
                   schedule, parent_request_to_teacher, preferable_teacher_gender,
                   preferable_teacher_characteristics, estimated_charge,
                   regularity, biweekly, deadline_at
            FROM recommendation WHERE sid = :sid
            """
        )
        async with self._sf() as s:
            row = (await s.execute(q, {"sid": sid})).first()
        if not row:
            raise ValueError(f"recommendation not found: {sid}")
        return dict(row._mapping)

    async def retrieve_candidates(
        self, app: dict[str, Any], n_gu: int = 3, k: int = 15,
        statuses: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        if app.get("lat") is None or app.get("lng") is None:
            raise ValueError("신청서에 좌표(lat/lng)가 없어 거리 매칭 불가")
        spec = int(app.get("teacher_specialties") or 5)
        _, intro_col = SPECIALTY.get(spec, SPECIALTY[5])
        async with self._sf() as s:
            gu_rows = (await s.execute(
                _NEAR_GU, {"lat": app["lat"], "lng": app["lng"], "n_gu": n_gu}
            )).all()
            gu_codes = [r._mapping["legal_dong_code"] for r in gu_rows]
            rows = (await s.execute(
                _candidates_sql(intro_col),
                {"gu_codes": gu_codes, "statuses": statuses or DEFAULT_STATUSES,
                 "sid": app["sid"], "subject_id": spec, "k": k},
            )).all()
        return [dict(r._mapping) for r in rows]

    def build_llm_input(self, app: dict[str, Any],
                        candidates: list[dict[str, Any]]) -> dict[str, Any]:
        spec = int(app.get("teacher_specialties") or 5)
        sched = _parse_schedule(app.get("schedule"))
        want_days = sched.get("days", [])  # ['wed','thu']
        cand_view = []
        for c in candidates:
            avail = [d for d in DOW if c.get(d)]
            day_match = [d for d in want_days if d in avail]
            rec = int(c.get("recommends") or 0)
            rev = int(c.get("reviews") or 0)
            cand_view.append({
                "teacher_sid": c["teacher_sid"], "name": c["name"],
                "activity": c.get("activity_status_text"),
                "exp_hours": float(c.get("experience_hour") or 0),
                "subject_exp_hours": float(c.get("experience_hour_for_study") or 0),
                "school": c.get("university"), "major": c.get("major"),
                "reviews": rev, "recommends": rec,
                "recommend_rate": round(rec / rev * 100, 1) if rev else None,
                "cancel_rate": float(c.get("cancellation_rate") or 0),
                "lateness": int(c.get("lateness") or 0),
                "active_kids": int(c.get("active_kids") or 0),
                "subject_wage": int(c.get("subject_wage") or 0),
                "available_days": avail,
                "day_match": day_match,
                "day_match_full": len(day_match) == len(want_days) and bool(want_days),
                "intro": (c.get("intro") or "").strip() or None,
            })
        return {
            "application": {
                "sid": app["sid"],
                "region": (app.get("parent_address") or "").split("|")[0],
                "subject": SPECIALTY.get(spec, ("?",))[0],
                "want_days": want_days,
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
            "candidates": cand_view,
        }

    async def recommend(self, sid: str, **kw: Any) -> dict[str, Any]:
        app = await self._fetch_application(sid)
        cands = await self.retrieve_candidates(app, **kw)
        if not cands:
            return {"summary": "후보 없음 — 지역 확장 필요", "ranked": [],
                    "note": "인근 활동 선생님 0명. n_gu 확대 또는 활동대기 포함 필요"}
        payload = self.build_llm_input(app, cands)
        resp = await self._llm.messages.create(
            model=self._model, max_tokens=1024,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user",
                       "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _parse_json(raw)


def _parse_schedule(raw: Any) -> dict[str, Any]:
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


def _parse_json(text_: str) -> dict[str, Any]:
    try:
        return json.loads(text_)
    except (ValueError, TypeError):
        a, b = text_.find("{"), text_.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(text_[a:b + 1])
            except (ValueError, TypeError):
                pass
    return {"summary": "", "ranked": [], "note": "JSON 파싱 실패", "raw": text_}


if __name__ == "__main__":
    import asyncio
    import os
    import sys

    async def _main() -> None:
        r = Recommender(os.environ["JARANDA_REPLICA_URL"], os.environ["ANTHROPIC_API_KEY"])
        out = await r.recommend(sys.argv[1])
        print(json.dumps(out, ensure_ascii=False, indent=2))

    asyncio.run(_main())
