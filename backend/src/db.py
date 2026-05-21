"""자란다 read replica MySQL 클라이언트.

auto-call의 src/poller/jaranda_replica.py 패턴을 단순화. 읽기 전용 조회만 수행.

PRD: vibe-cs/auto-call과 동일하게 PoC 단계는 replica 직접 polling.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config import settings


class JarandaReplica:
    def __init__(self, url: str | None = None) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url or settings.jaranda_replica_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def list_recent_recommendations(
        self,
        limit: int = 30,
        offset: int = 0,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """신청서 목록.

        - date_from/date_to (YYYY-MM-DD, KST) 주면 created_at 기준 [from 00:00, to 23:59:59] 범위.
          한쪽만 주면 그쪽만 조건으로 사용. 둘 다 없으면 최근 N시간 윈도우(settings).
        - status 101 (임시저장) 항상 제외
        - ORDER BY created_at DESC, sid (안정 정렬) — 페이지네이션용
        - offset/limit 지원 (페이지네이션)
        """
        where = ["r.status != 101"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if date_from or date_to:
            if date_from:
                where.append("r.created_at >= :date_from")
                params["date_from"] = f"{date_from} 00:00:00"
            if date_to:
                # to 일자의 끝(다음날 00:00 미만)까지 포함
                where.append("r.created_at < :date_to_excl")
                params["date_to_excl"] = f"{date_to} 23:59:59"
        else:
            where.append("r.created_at >= NOW() - INTERVAL :window_hours HOUR")
            params["window_hours"] = settings.recent_window_hours

        where_sql = " AND ".join(where)
        query = text(
            f"""
            SELECT
              r.sid,
              r.parent_account_sid,
              r.parent_name,
              r.parent_mobile,
              r.child_name,
              r.status,
              r.teacher_appliable,
              r.confirmed_at,
              r.cancelled_at,
              r.deadline_at,
              r.created_at,
              r.updated_at,
              r.new_parent,
              r.admin_account_sid,
              r.admin_name,
              r.is_urgent,
              r.auto_confirm,
              r.matched_teacher_name,
              r.estimated_charge,
              r.parent_request_to_teacher,
              r.biweekly,
              r.regular_visit_term,
              r.requested_first_visit_schedule,
              r.schedule,
              r.preferable_teacher_gender,
              r.preferable_teacher_characteristics,
              r.parent_address,
              r.requested_teacher_name,
              r.additional_children_num,
              r.regularity,
              r.cancelled_info,
              r.re_recommend,
              r.teacher_specialties,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid
                  AND (rt.applied = 1 OR rt.accepted = 1)
              ) AS applied_count,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid AND rt.requested = 1
              ) AS requested_count
            FROM recommendation r
            WHERE {where_sql}
            ORDER BY r.created_at DESC, r.sid DESC
            LIMIT :limit OFFSET :offset
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, params)
            return [dict(row._mapping) for row in result]

    async def get_recommendation(self, sid: str) -> dict[str, Any] | None:
        """단건 상세 조회."""
        query = text(
            """
            SELECT
              r.sid,
              r.parent_account_sid,
              r.parent_name,
              r.parent_mobile,
              r.child_name,
              r.status,
              r.teacher_appliable,
              r.confirmed_at,
              r.cancelled_at,
              r.deadline_at,
              r.created_at,
              r.updated_at,
              r.new_parent,
              r.admin_account_sid,
              r.admin_name,
              r.is_urgent,
              r.auto_confirm,
              r.matched_teacher_name,
              r.estimated_charge,
              r.parent_request_to_teacher,
              r.biweekly,
              r.regular_visit_term,
              r.requested_first_visit_schedule,
              r.schedule,
              r.preferable_teacher_gender,
              r.preferable_teacher_characteristics,
              r.parent_address,
              r.requested_teacher_name,
              r.additional_children_num,
              r.regularity,
              r.cancelled_info,
              r.re_recommend,
              r.teacher_specialties,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid
                  AND (rt.applied = 1 OR rt.accepted = 1)
              ) AS applied_count,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid AND rt.requested = 1
              ) AS requested_count
            FROM recommendation r
            WHERE r.sid = :sid
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"sid": sid})
            row = result.first()
            return dict(row._mapping) if row else None

    async def get_parent_history_counts(
        self, parent_account_sids: list[str]
    ) -> dict[str, dict[str, int]]:
        """학부모별 누적 이력. {sid: {app_count, confirmed_count, lesson_count}}.

        - app_count: recommendation 누적 신청 건수
        - confirmed_count: status IN (40, 90) 매칭 확정 건수
        - lesson_count: visit_instance status = 90 (방문완료) 건수
        """
        if not parent_account_sids:
            return {}

        rec_query = text(
            """
            SELECT
              parent_account_sid,
              COUNT(*) AS app_count,
              SUM(CASE WHEN status IN (40, 90) THEN 1 ELSE 0 END) AS confirmed_count
            FROM recommendation
            WHERE parent_account_sid IN :sids
            GROUP BY parent_account_sid
            """
        ).bindparams(bindparam("sids", expanding=True))
        visit_query = text(
            """
            SELECT
              parent_account_sid,
              COUNT(*) AS lesson_count
            FROM visit_instance
            WHERE parent_account_sid IN :sids
              AND status = 90
            GROUP BY parent_account_sid
            """
        ).bindparams(bindparam("sids", expanding=True))

        result: dict[str, dict[str, int]] = {
            sid: {"app_count": 0, "confirmed_count": 0, "lesson_count": 0}
            for sid in parent_account_sids
        }
        async with self._session_factory() as session:
            rec_rows = await session.execute(rec_query, {"sids": parent_account_sids})
            for row in rec_rows:
                m = row._mapping
                sid = m["parent_account_sid"]
                if sid in result:
                    result[sid]["app_count"] = int(m["app_count"] or 0)
                    result[sid]["confirmed_count"] = int(m["confirmed_count"] or 0)

            visit_rows = await session.execute(visit_query, {"sids": parent_account_sids})
            for row in visit_rows:
                m = row._mapping
                sid = m["parent_account_sid"]
                if sid in result:
                    result[sid]["lesson_count"] = int(m["lesson_count"] or 0)

        return result

    async def list_recommendation_teachers(self, sid: str) -> list[dict[str, Any]]:
        """해당 신청서에 요청된 선생님 목록 + 응답 상태 + 부모님 열람 정보.

        teacher_profile_view: viewer_id=parent_account_sid, teacher_sid=teacher.account_sid.
        viewed_at >= r.created_at 으로 "이번 신청서 이후 열람"만 인정 (누적 이력 분리).
        """
        query = text(
            """
            SELECT
              rt.teacher_account_sid,
              rt.applied,
              rt.accepted,
              rt.requested,
              rt.rejected,
              rt.last_responded_at,
              rt._created_at AS created_at,
              t.name AS teacher_name,
              t.experience_hour AS experience_hour,
              t.experience_hour_for_play AS experience_hour_for_play,
              t.experience_hour_for_study AS experience_hour_for_study,
              t.thumbnail_profile_url AS thumbnail_profile_url,
              t.university AS university,
              t.major AS major,
              tpv.viewed_at AS viewed_at,
              tpv.viewed_count AS viewed_count
            FROM recommendation_teachers rt
            LEFT JOIN teacher t ON t.account_sid = rt.teacher_account_sid
            LEFT JOIN recommendation r ON r.sid = rt.recommendation_sid
            LEFT JOIN teacher_profile_view tpv
              ON tpv.viewer_id = r.parent_account_sid
             AND tpv.teacher_sid = rt.teacher_account_sid
             AND tpv.viewed_at >= r.created_at
            WHERE rt.recommendation_sid = :sid
              AND rt.is_deleted = 0
            ORDER BY (rt.applied OR rt.accepted) DESC, rt.last_responded_at ASC
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"sid": sid})
            return [dict(row._mapping) for row in result]

    async def list_recommendation_teachers_batch(
        self, sids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """여러 신청서의 선생님 목록을 한 번에 조회. {sid: [teacher,...]}.

        - list 응답에 t1/t2 미리 채워주기 위함 (N+1 회피)
        - 정렬 순서는 단건 list_recommendation_teachers와 동일
        """
        if not sids:
            return {}

        query = text(
            """
            SELECT
              rt.recommendation_sid,
              rt.teacher_account_sid,
              rt.applied,
              rt.accepted,
              rt.requested,
              rt.rejected,
              rt.last_responded_at,
              rt._created_at AS created_at,
              t.name AS teacher_name,
              t.experience_hour AS experience_hour,
              t.experience_hour_for_play AS experience_hour_for_play,
              t.experience_hour_for_study AS experience_hour_for_study,
              t.thumbnail_profile_url AS thumbnail_profile_url,
              t.university AS university,
              t.major AS major,
              tpv.viewed_at AS viewed_at,
              tpv.viewed_count AS viewed_count
            FROM recommendation_teachers rt
            LEFT JOIN teacher t ON t.account_sid = rt.teacher_account_sid
            LEFT JOIN recommendation r ON r.sid = rt.recommendation_sid
            LEFT JOIN teacher_profile_view tpv
              ON tpv.viewer_id = r.parent_account_sid
             AND tpv.teacher_sid = rt.teacher_account_sid
             AND tpv.viewed_at >= r.created_at
            WHERE rt.recommendation_sid IN :sids
              AND rt.is_deleted = 0
            ORDER BY (rt.applied OR rt.accepted) DESC, rt.last_responded_at ASC
            """
        ).bindparams(bindparam("sids", expanding=True))

        result: dict[str, list[dict[str, Any]]] = {sid: [] for sid in sids}
        async with self._session_factory() as session:
            rows = await session.execute(query, {"sids": sids})
            for row in rows:
                m = dict(row._mapping)
                rec_sid = str(m.pop("recommendation_sid"))
                if rec_sid in result:
                    result[rec_sid].append(m)
        return result

    async def get_teacher_feedback_summary(
        self, teacher_account_sids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """선생님별 부모님 평가 집계. {sid: {review_count, recommend_count, recommend_rate}}.

        - parent_feedback.status = 2 (완료된 리뷰)만 집계
        - recommend_rate = recommend_count / review_count * 100 (0~100)
        """
        if not teacher_account_sids:
            return {}

        query = text(
            """
            SELECT
              teacher_account_sid,
              COUNT(*) AS review_count,
              SUM(CASE WHEN recommend = 1 THEN 1 ELSE 0 END) AS recommend_count
            FROM parent_feedback
            WHERE teacher_account_sid IN :sids
              AND status = 2
            GROUP BY teacher_account_sid
            """
        ).bindparams(bindparam("sids", expanding=True))

        result: dict[str, dict[str, Any]] = {
            sid: {"review_count": 0, "recommend_count": 0, "recommend_rate": None}
            for sid in teacher_account_sids
        }
        async with self._session_factory() as session:
            rows = await session.execute(query, {"sids": teacher_account_sids})
            for row in rows:
                m = row._mapping
                sid = m["teacher_account_sid"]
                if sid not in result:
                    continue
                rc = int(m["review_count"] or 0)
                rec = int(m["recommend_count"] or 0)
                result[sid]["review_count"] = rc
                result[sid]["recommend_count"] = rec
                result[sid]["recommend_rate"] = round(rec / rc * 100, 1) if rc > 0 else None
        return result


    async def list_subject_wages(self) -> dict[int, str]:
        """jrdtbl_subject_wage id → name. recommendation.teacher_specialties(1~6)와 매핑.

        (1=돌봄, 2=수학/과학, 3=운동, 4=예능, 5=외국어, 6=한글/국어)
        request_form_category(1~27)와는 다른 차원 — 시급 기준 큰 과목군.
        """
        query = text("SELECT id, name FROM jrdtbl_subject_wage")
        async with self._session_factory() as session:
            result = await session.execute(query)
            return {int(row._mapping["id"]): row._mapping["name"] for row in result}

    async def list_wage_ranges(self, sids: list[str]) -> dict[str, list[str]]:
        """신청서 sid → 부모님이 선택한 wage_range_type 코드 리스트 (DesiredCost enum).

        한 신청서가 여러 범위를 가질 수 있어 list로 반환. is_deleted=0만.
        """
        if not sids:
            return {}
        query = text(
            """
            SELECT recommendation_sid, wage_range_type
            FROM recommendation_teacher_wage_range
            WHERE recommendation_sid IN :sids
              AND is_deleted = 0
            ORDER BY recommendation_sid, recommendation_teacher_wage_range_id
            """
        ).bindparams(bindparam("sids", expanding=True))
        result: dict[str, list[str]] = {sid: [] for sid in sids}
        async with self._session_factory() as session:
            rows = await session.execute(query, {"sids": sids})
            for row in rows:
                m = row._mapping
                sid = str(m["recommendation_sid"])
                t = str(m["wage_range_type"])
                if sid in result and t not in result[sid]:
                    result[sid].append(t)
        return result

    async def list_teacher_subject_wages(
        self, teacher_sids: list[str], subject_wage_ids: list[int]
    ) -> dict[str, dict[int, dict[str, int]]]:
        """(teacher_sid → {subject_wage_id → {teacher_wage, parent_charge}}).

        jrdtbl_subject_teacher_wage_info에서 선생님별 과목별 현재 시급 조회.
        """
        if not teacher_sids or not subject_wage_ids:
            return {}
        query = text(
            """
            SELECT
              teacher_account_sid,
              subject_wage_id,
              teacher_wage_amount,
              parent_charge_amount
            FROM jrdtbl_subject_teacher_wage_info
            WHERE teacher_account_sid IN :tsids
              AND subject_wage_id IN :wids
            """
        ).bindparams(
            bindparam("tsids", expanding=True),
            bindparam("wids", expanding=True),
        )
        result: dict[str, dict[int, dict[str, int]]] = {sid: {} for sid in teacher_sids}
        async with self._session_factory() as session:
            rows = await session.execute(
                query, {"tsids": teacher_sids, "wids": subject_wage_ids}
            )
            for row in rows:
                m = row._mapping
                tsid = str(m["teacher_account_sid"])
                if tsid not in result:
                    continue
                result[tsid][int(m["subject_wage_id"])] = {
                    "teacher_wage": int(m["teacher_wage_amount"] or 0),
                    "parent_charge": int(m["parent_charge_amount"] or 0),
                }
        return result


    async def list_push_to_teachers(
        self, recommendation_sids: list[str], teacher_sids: list[str]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """(recommendation_sid, teacher_account_sid) → {count, last_sent_at, read_count, last_push_name}.

        fcm_send_history (FCM PUSH 발송 이력). 신청서 선생님 추천 PUSH만 집계:
          - app_type='TEACHER'
          - push_name LIKE '선생님_수업요청%' (일반 + 플래너)
          - deep_link LIKE 'recommend/normal?requestFormId=%' + 후처리 set 필터
        receiver_id 인덱스(MUL)로 range scan. deep_link IN(N)으로 인한 row × N
        텍스트 비교 폭주를 회피 (rec_sids 수백 개에서 응답 1~2분 폭증 → ~수초).
        """
        if not recommendation_sids or not teacher_sids:
            return {}
        rec_set = set(recommendation_sids)
        query = text(
            """
            SELECT
              deep_link,
              receiver_id AS teacher_account_sid,
              COUNT(*) AS cnt,
              MAX(sent_at) AS last_sent,
              SUM(CASE WHEN read_at IS NOT NULL THEN 1 ELSE 0 END) AS read_cnt,
              SUBSTRING_INDEX(GROUP_CONCAT(push_name ORDER BY sent_at DESC), ',', 1) AS last_push_name
            FROM fcm_send_history
            WHERE app_type = 'TEACHER'
              AND push_name LIKE '선생님_수업요청%'
              AND receiver_id IN :tsids
              AND deep_link LIKE 'recommend/normal?requestFormId=%'
              AND sent_at > NOW() - INTERVAL 30 DAY
            GROUP BY deep_link, receiver_id
            """
        ).bindparams(
            bindparam("tsids", expanding=True),
        )
        result: dict[tuple[str, str], dict[str, Any]] = {}
        async with self._session_factory() as session:
            rows = await session.execute(query, {"tsids": teacher_sids})
            for row in rows:
                m = row._mapping
                rec_sid = str(m["deep_link"]).replace("recommend/normal?requestFormId=", "")
                if rec_sid not in rec_set:
                    continue
                key = (rec_sid, str(m["teacher_account_sid"]))
                result[key] = {
                    "count": int(m["cnt"] or 0),
                    "last_sent_at": m["last_sent"],
                    "read_count": int(m["read_cnt"] or 0),
                    "last_push_name": (m["last_push_name"] or "").split(",")[0],
                }
        return result


    async def list_scheduled_child_counts(
        self, teacher_sids: list[str]
    ) -> dict[str, int]:
        """선생님별 방문예정(visit_instance.status=1) 상태인 유니크 아이 수.

        visit.status=10(진행중)은 종료 처리되지 않은 잔여 계약이 다수 섞여
        실제 활성 수업을 과대 표시함(예: 진행중 206건인데 방문예정 0건). 따라서
        '현재 선생님이 담당 중인 수업'은 앞으로 방문이 예정된(status=1) 건에서
        유니크 아이(child_account_sid) 수로 집계한다.
        """
        if not teacher_sids:
            return {}
        query = text(
            """
            SELECT matched_teacher_account_sid AS teacher_sid,
                   COUNT(DISTINCT child_account_sid) AS cnt
            FROM visit_instance
            WHERE matched_teacher_account_sid IN :tsids
              AND status = 1
            GROUP BY matched_teacher_account_sid
            """
        ).bindparams(bindparam("tsids", expanding=True))
        result: dict[str, int] = {sid: 0 for sid in teacher_sids}
        async with self._session_factory() as session:
            rows = await session.execute(query, {"tsids": teacher_sids})
            for row in rows:
                m = row._mapping
                tsid = str(m["teacher_sid"])
                if tsid in result:
                    result[tsid] = int(m["cnt"] or 0)
        return result


    async def list_teacher_weekly_availability(
        self, teacher_sids: list[str]
    ) -> dict[str, set[str]]:
        """선생님별 가능 요일 집합 (DayOfWeek 영문 대문자).

        schedule 테이블의 mon~sun 비트마스크 != 0 인 요일을 가능으로 판단.
        비트마스크 30bit 정밀 시간 해석은 v2 — 일단 요일 단위 매칭만.
        """
        if not teacher_sids:
            return {}
        query = text(
            """
            SELECT account_sid, mon, tue, wed, thu, fri, sat, sun
            FROM schedule
            WHERE account_sid IN :tsids
            """
        ).bindparams(bindparam("tsids", expanding=True))
        result: dict[str, set[str]] = {sid: set() for sid in teacher_sids}
        cols = (
            ("mon", "MONDAY"), ("tue", "TUESDAY"), ("wed", "WEDNESDAY"),
            ("thu", "THURSDAY"), ("fri", "FRIDAY"),
            ("sat", "SATURDAY"), ("sun", "SUNDAY"),
        )
        async with self._session_factory() as session:
            rows = await session.execute(query, {"tsids": teacher_sids})
            for row in rows:
                m = row._mapping
                tsid = str(m["account_sid"])
                if tsid not in result:
                    continue
                for col, name in cols:
                    if int(m[col] or 0) != 0:
                        result[tsid].add(name)
        return result


_replica: JarandaReplica | None = None


def get_replica() -> JarandaReplica:
    global _replica
    if _replica is None:
        _replica = JarandaReplica()
    return _replica
