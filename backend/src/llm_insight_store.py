"""LLM 인사이트 캐시 + 일일 호출 카운터.

캐시: application_sid PK. input_hash가 현재 입력 해시와 다르면 miss → 재호출.
카운터: KST 일자 단위. check_and_increment는 한도 초과 시 False (atomic UPSERT).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config import settings

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


class LlmInsightStore:
    def __init__(self, url: str | None = None) -> None:
        target = url or settings.matching_ops_db_url
        if not target:
            raise RuntimeError("MATCHING_OPS_DB_URL 미설정 — LLM 인사이트 비활성")
        self._engine: AsyncEngine = create_async_engine(
            target,
            pool_size=2,
            max_overflow=3,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def get_cached(self, application_sid: str) -> dict[str, Any] | None:
        query = text(
            """
            SELECT application_sid, input_hash, model_id, response_text, response_json,
                   input_tokens, output_tokens, created_at, updated_at
            FROM matching_ops_llm_insight_cache
            WHERE application_sid = :sid
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"sid": application_sid})
            row = result.first()
            if row is None:
                return None
            return _row_to_dict(row)

    async def upsert_cache(
        self,
        application_sid: str,
        input_hash: str,
        model_id: str,
        response_text: str,
        response_json: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        query = text(
            """
            INSERT INTO matching_ops_llm_insight_cache
                (application_sid, input_hash, model_id, response_text, response_json,
                 input_tokens, output_tokens, created_at, updated_at)
            VALUES
                (:sid, :hash, :model, :text, CAST(:json AS JSONB), :in_tok, :out_tok,
                 NOW(), NOW())
            ON CONFLICT (application_sid) DO UPDATE SET
                input_hash    = EXCLUDED.input_hash,
                model_id      = EXCLUDED.model_id,
                response_text = EXCLUDED.response_text,
                response_json = EXCLUDED.response_json,
                input_tokens  = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens,
                updated_at    = NOW()
            """
        )
        async with self._session_factory() as session:
            await session.execute(
                query,
                {
                    "sid": application_sid,
                    "hash": input_hash,
                    "model": model_id,
                    "text": response_text,
                    "json": json.dumps(response_json or {}),
                    "in_tok": input_tokens,
                    "out_tok": output_tokens,
                },
            )
            await session.commit()

    async def check_and_increment_daily(
        self,
        limit: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> tuple[bool, int]:
        """KST 일자 카운터 atomic 증가. 한도 초과 시 (False, current_count) 반환.

        호출 *전*에 체크 + 증가를 한 트랜잭션에 묶는다. 토큰은 호출 *후* 추가 update로
        합산 (호출 시점엔 모르므로). 한도 판정은 call_count 기준만.
        """
        today = datetime.now(KST).date()
        upsert = text(
            """
            INSERT INTO matching_ops_llm_daily_counter (date_kst, call_count, last_called_at)
            VALUES (:date, 1, NOW())
            ON CONFLICT (date_kst) DO UPDATE SET
                call_count = matching_ops_llm_daily_counter.call_count + 1,
                last_called_at = NOW()
            WHERE matching_ops_llm_daily_counter.call_count < :limit
            RETURNING call_count
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(upsert, {"date": today, "limit": limit})
            row = result.first()
            if row is None:
                # 한도 초과 — 현재 count 조회 후 반환
                cur = await session.execute(
                    text(
                        "SELECT call_count FROM matching_ops_llm_daily_counter "
                        "WHERE date_kst = :date"
                    ),
                    {"date": today},
                )
                cur_row = cur.first()
                await session.commit()
                return False, int(cur_row[0]) if cur_row else 0
            await session.commit()
            return True, int(row[0])

    async def add_token_usage(
        self, input_tokens: int, output_tokens: int
    ) -> None:
        """호출 성공 후 토큰 사용량 누적 (KST 일자)."""
        today = datetime.now(KST).date()
        update = text(
            """
            UPDATE matching_ops_llm_daily_counter
            SET input_tokens = input_tokens + :in_tok,
                output_tokens = output_tokens + :out_tok
            WHERE date_kst = :date
            """
        )
        async with self._session_factory() as session:
            await session.execute(
                update,
                {"date": today, "in_tok": input_tokens, "out_tok": output_tokens},
            )
            await session.commit()


def _row_to_dict(row: Any) -> dict[str, Any]:
    m = row._mapping
    created: datetime = m["created_at"]
    updated: datetime = m["updated_at"]
    response_json = m["response_json"]
    if isinstance(response_json, str):
        try:
            response_json = json.loads(response_json)
        except (ValueError, TypeError):
            response_json = {}
    return {
        "application_sid": m["application_sid"],
        "input_hash": m["input_hash"],
        "model_id": m["model_id"],
        "response_text": m["response_text"],
        "response_json": response_json or {},
        "input_tokens": int(m["input_tokens"] or 0),
        "output_tokens": int(m["output_tokens"] or 0),
        "created_at": created.isoformat() if created else None,
        "updated_at": updated.isoformat() if updated else None,
    }


_store: LlmInsightStore | None = None


def get_llm_insight_store() -> LlmInsightStore:
    global _store
    if _store is None:
        _store = LlmInsightStore()
    return _store


def llm_insight_available() -> bool:
    return bool(settings.matching_ops_db_url and settings.anthropic_api_key)
