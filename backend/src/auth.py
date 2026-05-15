"""Google OAuth — @jaranda.kr 도메인만 허용. auto-call/vibe-cs 동일 패턴."""
from __future__ import annotations

import hashlib
import hmac
import logging
import random
import time

from fastapi import APIRouter, Header, HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

ALLOWED_DOMAIN = "jaranda.kr"

# 단순 in-memory 세션 스토어. Cloud Run 인스턴스 재시작 시 만료.
_session_store: dict[str, dict] = {}


class GoogleLoginRequest(BaseModel):
    credential: str


def _create_session_token(email: str) -> str:
    payload = f"{email}:{time.time()}:{random.randint(0, 999999)}"
    return hmac.new(
        (settings.otp_secret_key or "matching-ops-default").encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def validate_session(token: str) -> str | None:
    session = _session_store.get(token)
    if not session:
        return None
    if time.time() > session["expires_at"]:
        del _session_store[token]
        return None
    return session["email"]


def validate_session_full(token: str) -> dict | None:
    """email + name 함께 반환. 메모 author 기록용."""
    session = _session_store.get(token)
    if not session:
        return None
    if time.time() > session["expires_at"]:
        del _session_store[token]
        return None
    return {"email": session["email"], "name": session.get("name") or ""}


@router.post("/google")
async def google_login(body: GoogleLoginRequest):
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="GOOGLE_CLIENT_ID 미설정")
    try:
        idinfo = id_token.verify_oauth2_token(
            body.credential,
            google_requests.Request(),
            settings.google_client_id,
        )
    except Exception as e:
        logger.warning(f"Google 토큰 검증 실패: {e}")
        raise HTTPException(status_code=401, detail="Google 인증 실패")

    email = idinfo.get("email", "")
    name = idinfo.get("name", "")
    hd = idinfo.get("hd", "")

    if hd != ALLOWED_DOMAIN and not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(status_code=403, detail="@jaranda.kr 계정만 접근 가능")

    token = _create_session_token(email)
    _session_store[token] = {
        "email": email,
        "name": name,
        "expires_at": time.time() + settings.otp_session_hours * 3600,
    }
    logger.info(f"로그인 성공: {email}")

    return {
        "success": True,
        "token": token,
        "email": email,
        "name": name,
        "expires_in_hours": settings.otp_session_hours,
    }


@router.get("/config")
async def auth_config():
    """프론트가 Google Sign-In 초기화에 쓸 공개 client ID."""
    return {"google_client_id": settings.google_client_id}


@router.get("/me")
async def get_me(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증 토큰 필요")
    token = authorization.removeprefix("Bearer ").strip()
    email = validate_session(token)
    if not email:
        raise HTTPException(status_code=401, detail="세션 만료")
    return {"email": email}


async def require_auth(authorization: str = Header(None)) -> str:
    """모든 /api/* 경로에서 의존성으로 사용 (auth.py 외)."""
    if not settings.auth_required:
        return "anonymous@dev"
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증 토큰 필요")
    token = authorization.removeprefix("Bearer ").strip()
    email = validate_session(token)
    if not email:
        raise HTTPException(status_code=401, detail="세션 만료")
    return email


async def require_auth_full(authorization: str = Header(None)) -> dict:
    """email + name 필요한 라우트(메모 작성 등)에서 사용."""
    if not settings.auth_required:
        return {"email": "anonymous@dev", "name": "dev"}
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증 토큰 필요")
    token = authorization.removeprefix("Bearer ").strip()
    info = validate_session_full(token)
    if not info:
        raise HTTPException(status_code=401, detail="세션 만료")
    return info
