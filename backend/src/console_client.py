"""자란다 콘솔 쓰기 API 클라이언트.

vibe-cs/backend/app/services/console_api.py 의 토큰·요청 패턴을 차용.
- 로그인: POST {console_api_base}/console/v1/accounts/login (scope=counselor) → JWT
- 토큰 캐싱: 프로세스 메모리. 9일 TTL, 60s 여유로 조기 갱신, asyncio.Lock 보호
- 쓰기 base: settings.console_api_base_write (prod로 명시).

지원 액션:
  - add_teachers(recommendation_sid, teacher_sids)
      POST /console/v1/admin/recommendations/{sid}/teachers
      body {"teacherIds":[...]} → {totalCount, succeedCount}
  - write_recommendation_memo(recommendation_sids, content)
      POST /console/v1/admin/recommendations/admin-memo
      body {"content", "recommendation_sids":[...]} → 201
  - send_visit_offers(recommendation_sid, teacher_sids)
      PUT  /console/v1/admin/recommendations/{sid}/teachers/visit-offers
      body List[teacherId] → List[DeniedVisitOffer]
      ⚠️ 실제 알림톡(긴급돌봄)/FCM(일반) 발송 부수효과 있음.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class ConsoleApiError(Exception):
    """콘솔 API 호출 실패. status·operation·body 보존."""

    def __init__(self, status: int, operation: str, body: Any) -> None:
        super().__init__(f"{operation} {status} {body!r}")
        self.status = status
        self.operation = operation
        self.body = body


class ConsoleClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def _write_base(self) -> str:
        return settings.console_api_base_write.rstrip("/")

    @property
    def _login_base(self) -> str:
        return settings.console_api_base.rstrip("/")

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        async with self._lock:
            # double-check after acquiring lock
            if self._token and time.time() < self._token_expires_at - 60:
                return self._token
            if not settings.console_username or not settings.console_password:
                raise RuntimeError("CONSOLE_USERNAME / CONSOLE_PASSWORD 미설정")
            async with httpx.AsyncClient(timeout=settings.console_api_timeout) as client:
                r = await client.post(
                    f"{self._login_base}/console/v1/accounts/login",
                    json={
                        "username": settings.console_username,
                        "password": settings.console_password,
                        "scope": ["counselor"],
                    },
                )
            if r.status_code >= 400:
                raise ConsoleApiError(r.status_code, "login", _safe_body(r))
            data = r.json() or {}
            token = data.get("access_token")
            if not token:
                raise ConsoleApiError(r.status_code, "login", data)
            self._token = token
            self._token_expires_at = time.time() + settings.console_token_ttl_hours * 3600
            logger.info("console token issued ttl=%dh", settings.console_token_ttl_hours)
            return token

    async def _request(
        self, method: str, endpoint: str, *, json: Any | None = None
    ) -> tuple[int, Any]:
        token = await self._ensure_token()
        url = f"{self._write_base}{endpoint}"
        async with httpx.AsyncClient(timeout=settings.console_api_timeout) as client:
            r = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=json,
            )
        return r.status_code, _safe_body(r)

    async def add_teachers(
        self, recommendation_sid: str, teacher_sids: list[str]
    ) -> dict[str, Any]:
        if not teacher_sids:
            return {"totalCount": 0, "succeedCount": 0}
        status, body = await self._request(
            "POST",
            f"/console/v1/admin/recommendations/{recommendation_sid}/teachers",
            json={"teacherIds": teacher_sids},
        )
        if status >= 400:
            raise ConsoleApiError(status, "add_teachers", body)
        return body if isinstance(body, dict) else {}

    async def write_recommendation_memo(
        self, recommendation_sids: list[str], content: str
    ) -> None:
        if not recommendation_sids:
            return
        status, body = await self._request(
            "POST",
            "/console/v1/admin/recommendations/admin-memo",
            json={"content": content, "recommendation_sids": recommendation_sids},
        )
        if status >= 400:
            raise ConsoleApiError(status, "write_recommendation_memo", body)

    async def send_visit_offers(
        self, recommendation_sid: str, teacher_sids: list[str]
    ) -> list[dict[str, Any]]:
        """방문 제안 일괄 발송 — 실제 알림 발송 부수효과. 응답=거절된 선생님 리스트."""
        if not teacher_sids:
            return []
        status, body = await self._request(
            "PUT",
            f"/console/v1/admin/recommendations/{recommendation_sid}/teachers/visit-offers",
            json=teacher_sids,
        )
        if status >= 400:
            raise ConsoleApiError(status, "send_visit_offers", body)
        if isinstance(body, list):
            return body
        return []


def _safe_body(r: httpx.Response) -> Any:
    if not r.text:
        return None
    try:
        return r.json()
    except Exception:
        return r.text


_client: ConsoleClient | None = None


def get_console_client() -> ConsoleClient:
    global _client
    if _client is None:
        _client = ConsoleClient()
    return _client


def console_available() -> bool:
    return bool(settings.console_username and settings.console_password)
