"""자란다 Firestore 채팅방 직접 조회.

채팅방은 자란다 서버가 Firestore에 저장 (MySQL replica에 없음).
컬렉션 구조 (kr.jaranda.app.persistence.chat.firebase.FirestoreConstant):

    ChatAccount/<account_sid>/ChatRoom/<partner_sid>
    └─ {
        chatRoomSid, partnerRole, creatorSid, createdAt,
        unreadCount, lastMessage{sentAt, content, senderSid},
        chatMatchSystem, deactivatedAt, blockedAt, partnerBlockedAt
    }

채팅방은 부모님 쪽과 선생님 쪽 양쪽에 미러링되어 저장됨. 각자 채팅방 나가기 가능:
 - 부모님 leave → ChatAccount/<parent>/ChatRoom/<teacher>.deactivatedAt 설정
 - 선생님 leave → ChatAccount/<teacher>/ChatRoom/<parent>.deactivatedAt 설정
양쪽 모두 봐야 "어느 한쪽이라도 끊긴" 상태(=종료) 정확 감지.

운영팀 요구: 채팅 전 / 채팅 중 / 채팅 종료 3-상태 구분.
 - active: 양쪽 document 모두 존재 + 둘 다 deactivatedAt is null
 - ended : 양쪽 어디든 document가 deactivatedAt 있음
 - None  : 양쪽 모두 document 미존재 (채팅 시작 전 — 자격 판단은 caller에서)

graceful 설계:
 - settings.firestore_enabled=False 또는 google-cloud-firestore 미설치 → 호출 시 빈 결과
 - IAM 권한 부족 / network 에러도 graceful (logger.exception, 빈 결과)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from src.config import settings

logger = logging.getLogger(__name__)


_client: Any = None
_client_init_failed = False


def _get_client() -> Any | None:
    """Lazy 초기화. 실패 시 None 반환 + 재시도 안 함."""
    global _client, _client_init_failed
    if not settings.firestore_enabled:
        return None
    if _client_init_failed:
        return None
    if _client is not None:
        return _client
    try:
        from google.cloud import firestore  # type: ignore
        _client = firestore.Client(project=settings.firestore_project or None)
        logger.info("firestore client initialized (project=%s)", settings.firestore_project or "ADC")
        return _client
    except Exception:
        logger.exception("firestore client init failed — chat room lookup disabled")
        _client_init_failed = True
        return None


def firestore_available() -> bool:
    return _get_client() is not None


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        return value.to_datetime().isoformat()  # type: ignore[attr-defined]
    except Exception:
        try:
            return datetime.fromtimestamp(float(value)).isoformat()
        except Exception:
            return None


def _fetch_chat_status_sync(
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """동기 Firestore 호출 — 페어당 양쪽 document 모두 조회.

    반환 dict[(parent_sid, teacher_sid)] = {
        "status": "active" | "ended",
        "last_message_at": ISO | None,
    }
    양쪽 모두 미존재(=before)는 dict에 포함하지 않음 (caller에서 chat_eligible 로 판단).
    """
    client = _get_client()
    if client is None or not pairs:
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for parent_sid, teacher_sid in pairs:
        try:
            parent_ref = (
                client.collection("ChatAccount")
                .document(parent_sid)
                .collection("ChatRoom")
                .document(teacher_sid)
            )
            teacher_ref = (
                client.collection("ChatAccount")
                .document(teacher_sid)
                .collection("ChatRoom")
                .document(parent_sid)
            )
            parent_snap = parent_ref.get()
            teacher_snap = teacher_ref.get()

            parent_exists = parent_snap.exists
            teacher_exists = teacher_snap.exists
            if not parent_exists and not teacher_exists:
                continue  # 양쪽 다 없음 = 채팅 시작 전 (dict 누락 = before)

            parent_data = parent_snap.to_dict() or {} if parent_exists else {}
            teacher_data = teacher_snap.to_dict() or {} if teacher_exists else {}

            parent_deact = parent_data.get("deactivatedAt") is not None
            teacher_deact = teacher_data.get("deactivatedAt") is not None

            # 한쪽이라도 deactivated 또는 한쪽 document만 존재(상대가 완전 삭제?) = ended
            ended = parent_deact or teacher_deact or (parent_exists ^ teacher_exists)
            status = "ended" if ended else "active"

            # 마지막 메시지 시간 — 양쪽 lastMessage 중 더 최근
            last_msgs = []
            for d in (parent_data, teacher_data):
                lm = d.get("lastMessage") or {}
                sent_at = _to_iso(lm.get("sentAt"))
                if sent_at:
                    last_msgs.append(sent_at)
            last_message_at = max(last_msgs) if last_msgs else None

            result[(parent_sid, teacher_sid)] = {
                "status": status,
                "last_message_at": last_message_at,
            }
        except Exception:
            logger.exception(
                "firestore chat room fetch failed parent=%s teacher=%s (graceful)",
                parent_sid,
                teacher_sid,
            )
    return result


async def find_chat_status(
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """(parent_sid, teacher_sid) → {status, last_message_at}. graceful.

    페어당 양쪽 fetch (총 2회 × N). 화면 30 신청서 × 5 선생님 = 300회.
    """
    if not pairs:
        return {}
    if not firestore_available():
        return {}
    try:
        return await asyncio.to_thread(_fetch_chat_status_sync, pairs)
    except Exception:
        logger.exception("firestore batch fetch failed (graceful)")
        return {}
