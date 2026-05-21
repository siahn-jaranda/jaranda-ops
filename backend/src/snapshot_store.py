"""matching-ops 신청서 스냅샷 — PostgreSQL.

메모 작성/삭제 시 자란다 replica의 신청서 본문을 비정규화해서 보존.
이유: 자란다 replica `recommendation` 조회 윈도우(72h)를 벗어나거나
status=100(삭제)된 신청서도 운영팀의 "관리 신청서 목록"에 계속 노출되어야
이력이 보존됨. snapshot 자체는 application_sid PK로 UPSERT.

스키마:
    CREATE TABLE matching_ops_application_snapshot (
      application_sid     VARCHAR(36) PRIMARY KEY,
      child_name          VARCHAR(70),
      region              VARCHAR(100),
      status_key          VARCHAR(20),
      status_label        VARCHAR(20),
      subjects            JSONB,
      wage_ranges         JSONB,
      request_chips       JSONB,
      parent_request      TEXT,
      matched_teacher     VARCHAR(70),
      cancelled_reason    TEXT,
      is_urgent           BOOLEAN,
      auto_confirm        BOOLEAN,
      re_recommend        BOOLEAN,
      app_created_at      TIMESTAMPTZ,
      app_deadline_at     TIMESTAMPTZ,
      app_confirmed_at    TIMESTAMPTZ,
      app_cancelled_at    TIMESTAMPTZ,
      snapshot_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      snapshot_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
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


# snapshot UPSERT/조회에서 사용하는 필드 화이트리스트
SNAPSHOT_FIELDS = (
    "child_name",
    "region",
    "status_key",
    "status_label",
    "subjects",
    "wage_ranges",
    "request_chips",
    "parent_request",
    "matched_teacher",
    "cancelled_reason",
    "is_urgent",
    "auto_confirm",
    "re_recommend",
    "app_created_at",
    "app_deadline_at",
    "app_confirmed_at",
    "app_cancelled_at",
)


class SnapshotStore:
    def __init__(self, url: str | None = None) -> None:
        target = url or settings.matching_ops_db_url
        if not target:
            raise RuntimeError("MATCHING_OPS_DB_URL 미설정 — snapshot 영속화 비활성")
        self._engine: AsyncEngine = create_async_engine(
            target,
            pool_size=3,
            max_overflow=5,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def aclose(self) -> None:
        await self._engine.dispose()

    def _clean_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        cleaned = {k: fields.get(k) for k in SNAPSHOT_FIELDS}
        for jcol in ("request_chips", "subjects", "wage_ranges"):
            v = cleaned.get(jcol)
            if v is not None and not isinstance(v, str):
                cleaned[jcol] = json.dumps(v, ensure_ascii=False)
        return cleaned

    async def upsert(self, application_sid: str, fields: dict[str, Any]) -> None:
        """INSERT … ON CONFLICT UPDATE. fields는 SNAPSHOT_FIELDS만 사용."""
        cleaned = self._clean_fields(fields)
        col_list = ", ".join(SNAPSHOT_FIELDS)
        param_list = ", ".join(f":{c}" for c in SNAPSHOT_FIELDS)
        update_list = ", ".join(f"{c} = EXCLUDED.{c}" for c in SNAPSHOT_FIELDS)
        query = text(
            f"""
            INSERT INTO matching_ops_application_snapshot
              (application_sid, {col_list})
            VALUES
              (:application_sid, {param_list})
            ON CONFLICT (application_sid) DO UPDATE SET
              {update_list},
              snapshot_updated_at = NOW()
            """
        )
        params = {"application_sid": application_sid, **cleaned}
        async with self._session_factory() as session:
            await session.execute(query, params)
            await session.commit()

    async def insert_if_absent(self, application_sid: str, fields: dict[str, Any]) -> bool:
        """첫 메모 작성 시점의 신청서 상태를 freeze. 이미 row가 있으면 skip.

        관리 신청서 목록은 "왜 그 메모를 남겼는지" 컨텍스트가 보존되어야 하므로
        이후 메모 작성마다 신청서 현재 상태로 덮어쓰지 않는다. 첫 INSERT 시점
        ─ 즉 운영팀이 처음 그 신청서를 들여다본 시점 ─ 의 시급/지역/요청사항 등이
        고정되고, 이후 자란다 본서버에서 상태가 바뀌어도 snapshot은 유지된다.
        리턴값: 새로 INSERT 됐는지(True) / 이미 있어서 스킵(False).
        """
        cleaned = self._clean_fields(fields)
        col_list = ", ".join(SNAPSHOT_FIELDS)
        param_list = ", ".join(f":{c}" for c in SNAPSHOT_FIELDS)
        query = text(
            f"""
            INSERT INTO matching_ops_application_snapshot
              (application_sid, {col_list})
            VALUES
              (:application_sid, {param_list})
            ON CONFLICT (application_sid) DO NOTHING
            """
        )
        params = {"application_sid": application_sid, **cleaned}
        async with self._session_factory() as session:
            result = await session.execute(query, params)
            await session.commit()
            return (result.rowcount or 0) > 0

    async def delete(self, application_sid: str) -> bool:
        query = text(
            "DELETE FROM matching_ops_application_snapshot "
            "WHERE application_sid = :application_sid"
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"application_sid": application_sid})
            await session.commit()
            return (result.rowcount or 0) > 0

    async def list_managed(self, limit: int = 100) -> list[dict[str, Any]]:
        """관리 신청서 목록 — 메모 있는 sid 기준, 최근 메모 작성순.

        memo가 driving table. snapshot은 LEFT JOIN — _ensure_snapshot이 첫 메모
        시점에 fetch 실패했던 row(예: replica 윈도우 밖 신청서)도 메모만 있으면 노출.
        snapshot 누락 시 child_name 등은 NULL → _row_to_dict의 default가 채움.
        결과 row: (snapshot 필드 nullable) + memo_count + last_memo_at + handler.
        """
        query = text(
            """
            SELECT
              mm.application_sid,
              s.child_name, s.region, s.status_key, s.status_label,
              s.subjects, s.wage_ranges,
              s.request_chips, s.parent_request, s.matched_teacher,
              s.cancelled_reason, s.is_urgent, s.auto_confirm, s.re_recommend,
              s.app_created_at, s.app_deadline_at,
              s.app_confirmed_at, s.app_cancelled_at,
              s.snapshot_at, s.snapshot_updated_at,
              mm.memo_count, mm.last_memo_at,
              h.handler_email, h.handler_name, h.claimed_at AS handler_claimed_at
            FROM (
              SELECT application_sid,
                     COUNT(*) AS memo_count,
                     MAX(created_at) AS last_memo_at
              FROM matching_ops_memo
              WHERE deleted_at IS NULL
              GROUP BY application_sid
            ) mm
            LEFT JOIN matching_ops_application_snapshot s ON s.application_sid = mm.application_sid
            LEFT JOIN matching_ops_handler h ON h.application_sid = mm.application_sid
            ORDER BY mm.last_memo_at DESC
            LIMIT :limit
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"limit": limit})
            return [_row_to_dict(row) for row in result]

    async def memo_count(self, application_sid: str) -> int:
        """주어진 신청서의 활성 메모 개수 (deleted_at IS NULL)."""
        query = text(
            """
            SELECT COUNT(*) AS cnt
            FROM matching_ops_memo
            WHERE application_sid = :application_sid AND deleted_at IS NULL
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"application_sid": application_sid})
            row = result.first()
            return int(row._mapping["cnt"]) if row else 0


def _maybe_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return value


def _row_to_dict(row: Any) -> dict[str, Any]:
    m = row._mapping
    chips = _maybe_json(m.get("request_chips"), [])
    subjects = _maybe_json(m.get("subjects"), [])
    wage_ranges = _maybe_json(m.get("wage_ranges"), [])
    handler = None
    if m.get("handler_email"):
        claimed: datetime | None = m.get("handler_claimed_at")
        handler = {
            "email": m["handler_email"],
            "name": m.get("handler_name"),
            "claimed_at": claimed.isoformat() if claimed else None,
        }
    return {
        "sid": m["application_sid"],
        "child": m.get("child_name") or "자녀 미입력",
        "region": m.get("region") or "",
        "statusKey": m.get("status_key"),
        "status": m.get("status_label"),
        "subjects": subjects or [],
        "wageRanges": wage_ranges or [],
        "requestChips": chips or [],
        "parentRequest": m.get("parent_request") or "",
        "matchedTeacher": m.get("matched_teacher") or "",
        "cancelledReason": m.get("cancelled_reason") or "",
        "isUrgent": bool(m.get("is_urgent")),
        "autoConfirm": bool(m.get("auto_confirm")),
        "reRecommend": bool(m.get("re_recommend")),
        "appCreatedAt": _ts(m.get("app_created_at")),
        "appDeadlineAt": _ts(m.get("app_deadline_at")),
        "appConfirmedAt": _ts(m.get("app_confirmed_at")),
        "appCancelledAt": _ts(m.get("app_cancelled_at")),
        "snapshotAt": _ts(m.get("snapshot_at")),
        "snapshotUpdatedAt": _ts(m.get("snapshot_updated_at")),
        "memoCount": int(m.get("memo_count") or 0),
        "lastMemoAt": _ts(m.get("last_memo_at")),
        "handler": handler,
    }


def _ts(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


_store: SnapshotStore | None = None


def get_snapshot_store() -> SnapshotStore:
    """미설정 시 RuntimeError. 라우트 단에서 503으로 변환."""
    global _store
    if _store is None:
        _store = SnapshotStore()
    return _store


def snapshot_store_available() -> bool:
    return bool(settings.matching_ops_db_url)
