"""matching-ops 메모 영속화 — PostgreSQL.

read replica(JarandaReplica)와 책임 분리:
- 자란다 prod DB 영향 0
- 별도 Cloud SQL 인스턴스 (matching-ops-db)
- 스키마: matching_ops_memo (id, application_sid, author_email, author_name,
                            content, tags JSONB, created_at, updated_at, deleted_at)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config import settings

logger = logging.getLogger(__name__)


class MemoStore:
    def __init__(self, url: str | None = None) -> None:
        target = url or settings.matching_ops_db_url
        if not target:
            raise RuntimeError("MATCHING_OPS_DB_URL 미설정 — 메모 영속화 비활성")
        self._engine: AsyncEngine = create_async_engine(
            target,
            pool_size=3,
            max_overflow=5,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def create_memo(
        self,
        application_sid: str,
        author_email: str,
        author_name: str | None,
        content: str,
        tags: list[str],
    ) -> dict[str, Any]:
        query = text(
            """
            INSERT INTO matching_ops_memo
              (application_sid, author_email, author_name, content, tags)
            VALUES
              (:application_sid, :author_email, :author_name, :content, CAST(:tags AS JSONB))
            RETURNING id, application_sid, author_email, author_name, content, tags,
                      created_at, updated_at
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                query,
                {
                    "application_sid": application_sid,
                    "author_email": author_email,
                    "author_name": author_name,
                    "content": content,
                    "tags": json.dumps(tags or []),
                },
            )
            row = result.first()
            await session.commit()
            return _row_to_dict(row)

    async def list_memos_by_application(
        self, application_sid: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = text(
            """
            SELECT id, application_sid, author_email, author_name, content, tags,
                   created_at, updated_at
            FROM matching_ops_memo
            WHERE application_sid = :application_sid
              AND deleted_at IS NULL
            ORDER BY created_at DESC
            LIMIT :limit
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                query, {"application_sid": application_sid, "limit": limit}
            )
            return [_row_to_dict(row) for row in result]

    async def delete_memo(self, memo_id: int, author_email: str) -> bool:
        """본인 글만 soft delete. 성공 시 True, 권한 없음/없음 시 False."""
        query = text(
            """
            UPDATE matching_ops_memo
            SET deleted_at = NOW(), updated_at = NOW()
            WHERE id = :id
              AND author_email = :author_email
              AND deleted_at IS NULL
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                query, {"id": memo_id, "author_email": author_email}
            )
            await session.commit()
            return (result.rowcount or 0) > 0


def _row_to_dict(row: Any) -> dict[str, Any]:
    m = row._mapping
    created: datetime = m["created_at"]
    updated: datetime = m["updated_at"]
    tags = m["tags"]
    if isinstance(tags, str):
        tags = json.loads(tags)
    return {
        "id": int(m["id"]),
        "application_sid": m["application_sid"],
        "author_email": m["author_email"],
        "author_name": m["author_name"],
        "content": m["content"],
        "tags": tags or [],
        "created_at": created.isoformat() if created else None,
        "updated_at": updated.isoformat() if updated else None,
    }


_store: MemoStore | None = None


def get_memo_store() -> MemoStore:
    """미설정 시 RuntimeError. 라우트 단에서 503으로 변환."""
    global _store
    if _store is None:
        _store = MemoStore()
    return _store


def memo_store_available() -> bool:
    return bool(settings.matching_ops_db_url)
