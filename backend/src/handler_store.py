"""matching-ops 처리 담당(handler) 영속화 — PostgreSQL.

신청서의 자란다 admin_name과는 별개로, 매칭 운영팀이 "내가 보고 있다"를
표시하기 위한 ownership. 본인만 본인으로 잡고/해제할 수 있고, 이미 다른
사람이 잡았으면 변경 불가 (application_sid PRIMARY KEY로 자연 강제).

스키마:
    CREATE TABLE matching_ops_handler (
      application_sid VARCHAR(36) PRIMARY KEY,
      handler_email   VARCHAR(255) NOT NULL,
      handler_name    VARCHAR(70),
      claimed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

memo_store와 동일 PostgreSQL 인스턴스(`MATCHING_OPS_DB_URL`) 사용.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config import settings

logger = logging.getLogger(__name__)


class HandlerStore:
    def __init__(self, url: str | None = None) -> None:
        target = url or settings.matching_ops_db_url
        if not target:
            raise RuntimeError("MATCHING_OPS_DB_URL 미설정 — handler 영속화 비활성")
        self._engine: AsyncEngine = create_async_engine(
            target,
            pool_size=3,
            max_overflow=5,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def claim(
        self,
        application_sid: str,
        handler_email: str,
        handler_name: str | None,
    ) -> tuple[str, dict[str, Any]]:
        """본인 지정 시도.

        반환: ("claimed" | "exists", row)
        - "claimed": 새로 잡음
        - "exists":  이미 누군가(본인 포함) 잡고 있음 → 호출자가 본인인지 비교
        """
        insert_q = text(
            """
            INSERT INTO matching_ops_handler
              (application_sid, handler_email, handler_name)
            VALUES
              (:application_sid, :handler_email, :handler_name)
            RETURNING application_sid, handler_email, handler_name, claimed_at
            """
        )
        select_q = text(
            """
            SELECT application_sid, handler_email, handler_name, claimed_at
            FROM matching_ops_handler
            WHERE application_sid = :application_sid
            """
        )
        async with self._session_factory() as session:
            try:
                result = await session.execute(
                    insert_q,
                    {
                        "application_sid": application_sid,
                        "handler_email": handler_email,
                        "handler_name": handler_name,
                    },
                )
                row = result.first()
                await session.commit()
                return ("claimed", _row_to_dict(row))
            except IntegrityError:
                await session.rollback()
                # 이미 누가 잡았음 — 현재 상태 조회
                result = await session.execute(
                    select_q, {"application_sid": application_sid}
                )
                row = result.first()
                if row is None:
                    raise
                return ("exists", _row_to_dict(row))

    async def release(self, application_sid: str, handler_email: str) -> bool:
        """본인이 잡은 것만 해제. 성공 시 True, 본인 아니거나 없으면 False."""
        query = text(
            """
            DELETE FROM matching_ops_handler
            WHERE application_sid = :application_sid
              AND handler_email = :handler_email
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                query,
                {"application_sid": application_sid, "handler_email": handler_email},
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def get(self, application_sid: str) -> dict[str, Any] | None:
        query = text(
            """
            SELECT application_sid, handler_email, handler_name, claimed_at
            FROM matching_ops_handler
            WHERE application_sid = :application_sid
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"application_sid": application_sid})
            row = result.first()
            return _row_to_dict(row) if row else None

    async def list_by_sids(
        self, application_sids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """list_applications batch fetch용. {sid: handler_dict}."""
        if not application_sids:
            return {}
        query = text(
            """
            SELECT application_sid, handler_email, handler_name, claimed_at
            FROM matching_ops_handler
            WHERE application_sid IN :sids
            """
        ).bindparams(bindparam("sids", expanding=True))
        async with self._session_factory() as session:
            result = await session.execute(query, {"sids": application_sids})
            return {row._mapping["application_sid"]: _row_to_dict(row) for row in result}


def _row_to_dict(row: Any) -> dict[str, Any]:
    m = row._mapping
    claimed_at: datetime = m["claimed_at"]
    return {
        "application_sid": m["application_sid"],
        "email": m["handler_email"],
        "name": m["handler_name"],
        "claimed_at": claimed_at.isoformat() if claimed_at else None,
    }


_store: HandlerStore | None = None


def get_handler_store() -> HandlerStore:
    """미설정 시 RuntimeError. 라우트 단에서 503으로 변환."""
    global _store
    if _store is None:
        _store = HandlerStore()
    return _store


def handler_store_available() -> bool:
    return bool(settings.matching_ops_db_url)
