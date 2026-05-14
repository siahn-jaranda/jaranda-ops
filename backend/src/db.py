"""자란다 read replica MySQL 클라이언트.

auto-call의 src/poller/jaranda_replica.py 패턴을 단순화. 읽기 전용 조회만 수행.

PRD: vibe-cs/auto-call과 동일하게 PoC 단계는 replica 직접 polling.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
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
              p.policy_name,
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
            JOIN request_form_policy p ON p.request_form_id = r.sid
            WHERE r.created_at >= NOW() - INTERVAL :window_hours HOUR
              AND r.status != 101
            ORDER BY r.updated_at DESC
            LIMIT :limit
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                query,
                {"window_hours": 72, "limit": limit},
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
              p.policy_name,
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
            JOIN request_form_policy p ON p.request_form_id = r.sid
            WHERE r.sid = :sid
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"sid": sid})
            row = result.first()
            return dict(row._mapping) if row else None

    async def list_recommendation_teachers(self, sid: str) -> list[dict[str, Any]]:
        """해당 신청서에 요청된 선생님 목록 + 응답 상태."""
        query = text(
            """
            SELECT
              rt.teacher_account_sid,
              rt.applied,
              rt.requested,
              rt.rejected,
              rt.last_responded_at,
              rt._created_at AS created_at,
              t.name AS teacher_name
            FROM recommendation_teachers rt
            LEFT JOIN teacher t ON t.account_sid = rt.teacher_account_sid
            WHERE rt.recommendation_sid = :sid
              AND rt.is_deleted = 0
            ORDER BY rt.applied DESC, rt.last_responded_at ASC
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"sid": sid})
            return [dict(row._mapping) for row in result]


_replica: JarandaReplica | None = None


def get_replica() -> JarandaReplica:
    global _replica
    if _replica is None:
        _replica = JarandaReplica()
    return _replica
