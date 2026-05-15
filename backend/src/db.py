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

    async def list_recent_recommendations(self, limit: int = 30) -> list[dict[str, Any]]:
        """최근 신청서 목록.

        - 최근 72시간 내 신청서 (status 101 제외)
        - 페이지 메인 테이블 데이터 소스
        """
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
              r.preferable_teacher_gender,
              r.preferable_teacher_characteristics,
              r.parent_address,
              r.requested_teacher_name,
              r.additional_children_num,
              r.regularity,
              r.cancelled_info,
              r.re_recommend,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid AND rt.applied = 1
              ) AS applied_count,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid AND rt.requested = 1
              ) AS requested_count
            FROM recommendation r
            WHERE r.created_at >= NOW() - INTERVAL :window_hours HOUR
              AND r.status != 101
            ORDER BY r.updated_at DESC
            LIMIT :limit
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                query,
                {"window_hours": settings.recent_window_hours, "limit": limit},
            )
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
              r.preferable_teacher_gender,
              r.preferable_teacher_characteristics,
              r.parent_address,
              r.requested_teacher_name,
              r.additional_children_num,
              r.regularity,
              r.cancelled_info,
              r.re_recommend,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid AND rt.applied = 1
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
              rt.requested,
              rt.rejected,
              rt.last_responded_at,
              rt._created_at AS created_at,
              t.name AS teacher_name,
              t.experience_hour AS experience_hour,
              t.experience_hour_for_play AS experience_hour_for_play,
              t.experience_hour_for_study AS experience_hour_for_study,
              t.thumbnail_profile_url AS thumbnail_profile_url,
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
            ORDER BY rt.applied DESC, rt.last_responded_at ASC
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
              rt.requested,
              rt.rejected,
              rt.last_responded_at,
              rt._created_at AS created_at,
              t.name AS teacher_name,
              t.experience_hour AS experience_hour,
              t.experience_hour_for_play AS experience_hour_for_play,
              t.experience_hour_for_study AS experience_hour_for_study,
              t.thumbnail_profile_url AS thumbnail_profile_url,
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
            ORDER BY rt.applied DESC, rt.last_responded_at ASC
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


_replica: JarandaReplica | None = None


def get_replica() -> JarandaReplica:
    global _replica
    if _replica is None:
        _replica = JarandaReplica()
    return _replica
