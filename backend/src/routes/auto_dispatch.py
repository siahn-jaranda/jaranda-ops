"""자동 디스패치 수동 트리거 라우트.

- POST /api/auto-dispatch/run — body로 dry_run·max_apps override 가능.
  인증: Google 세션 토큰(운영자) OR X-Trigger-Secret 헤더(CLI·Cloud Scheduler).
  kill switch(AUTO_DISPATCH_ENABLED=false)면 503. admin allowlist 옵션 적용.

Cloud Scheduler cron 등록은 이 라우트 호출(X-Trigger-Secret)로 진행 예정.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from src.auth import validate_session_full
from src.auto_dispatch import AutoDispatchUnavailable, run_once
from src.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auto-dispatch", tags=["auto-dispatch"])


class TriggerRunRequest(BaseModel):
    dry_run: bool | None = Field(
        default=None,
        description="None이면 settings.auto_dispatch_dry_run 사용. true=콘솔 호출 안 함.",
    )
    max_apps: int | None = Field(
        default=None, ge=1, le=200,
        description="이번 호출에서 처리할 최대 신청서 수. None이면 daily_max_apps 잔여.",
    )


async def trigger_auth(
    authorization: str = Header(None),
    x_trigger_secret: str = Header(None, alias="X-Trigger-Secret"),
) -> dict[str, str]:
    """자동 디스패치 전용 인증. 세 가지 경로 — 단 하나라도 통과하면 OK.

    1) X-Trigger-Secret 헤더 == AUTO_DISPATCH_TRIGGER_SECRET → 시스템 트리거
       (CLI 수동 검증·Cloud Scheduler용. 매시 cron 등록 시 이 헤더 사용).
    2) AUTH_REQUIRED=false → 개발 모드 (운영에서는 항상 true).
    3) Bearer 세션 토큰(Google OAuth) → 운영자 identity (email/name).
    """
    secret_env = settings.auto_dispatch_trigger_secret.strip()
    if secret_env and x_trigger_secret and x_trigger_secret == secret_env:
        return {"email": "system@auto-dispatch", "name": "auto-dispatch-scheduler"}

    if not settings.auth_required:
        return {"email": "anonymous@dev", "name": "dev"}

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="인증 토큰(Bearer) 또는 X-Trigger-Secret 헤더 필요",
        )
    token = authorization.removeprefix("Bearer ").strip()
    info = validate_session_full(token)
    if not info:
        raise HTTPException(status_code=401, detail="세션 만료")
    return info


@router.post("/run")
async def trigger_run(
    body: TriggerRunRequest,
    user: dict = Depends(trigger_auth),
) -> dict[str, Any]:
    if not settings.auto_dispatch_enabled:
        raise HTTPException(
            status_code=503,
            detail="AUTO_DISPATCH_ENABLED=false (kill switch)",
        )

    # admin allowlist 옵션
    allow = settings.auto_dispatch_admin_emails_list
    email = (user.get("email") or "").lower()
    if allow and email not in allow:
        logger.warning("auto_dispatch trigger denied for %s (not in allowlist)", email)
        raise HTTPException(status_code=403, detail="관리자 권한 필요")

    dry_run = body.dry_run if body.dry_run is not None else settings.auto_dispatch_dry_run

    try:
        return await run_once(
            dry_run=dry_run,
            max_apps=body.max_apps,
            operator_email=email,
        )
    except AutoDispatchUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception:
        logger.exception("auto_dispatch run_once failed")
        raise HTTPException(status_code=500, detail="auto_dispatch 실행 실패")


@router.get("/status")
async def get_status(user: dict = Depends(trigger_auth)) -> dict[str, Any]:
    """현재 설정 + kill switch / dry-run 상태 확인용."""
    return {
        "enabled": settings.auto_dispatch_enabled,
        "dry_run_default": settings.auto_dispatch_dry_run,
        "daily_max_apps": settings.auto_dispatch_daily_max_apps,
        "min_age_minutes": settings.auto_dispatch_min_age_minutes,
        "top_n": settings.auto_dispatch_top_n,
        "teacher_daily_cap": settings.auto_dispatch_teacher_daily_cap,
        "admin_allowlist_size": len(settings.auto_dispatch_admin_emails_list),
        "slack_webhook_set": bool(settings.auto_dispatch_slack_webhook.strip()),
        "console_configured": bool(
            settings.console_username and settings.console_password
        ),
    }
