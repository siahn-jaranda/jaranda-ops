"""Anthropic Claude 호출 — 매칭 신청서 인사이트 추출.

system prompt를 cache_control로 마킹 → 반복 호출 시 입력 토큰 비용 절감.
응답은 JSON 객체. 파싱 실패 시 response_json={} 반환 (raw text는 그대로 보존).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from src.config import settings

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """당신은 자란다 매칭 운영팀의 분석 어시스턴트입니다.

신청서 컨텍스트(자녀, 지역, 상태, 요청 조건, 추천된 선생님 요약, 마감 시간, 부모 누적 이력)와 운영팀 메모를 받아 다음 JSON 형식으로만 응답하세요.

{
  "summary": "한 줄 핵심 (60자 이내)",
  "key_signals": ["데이터 기반 관찰 2-4개"],
  "recommended_actions": ["구체적 다음 액션 2-4개"],
  "risk_flags": ["주의할 위험 0-3개"]
}

규칙:
- 입력에 없는 정보 추정·할루시네이션 금지
- 응답률, prob, 마감 잔여시간, 자녀 연령, 지역, 메모 내용을 우선 활용
- 한국어. 운영팀 내부 메모처럼 간결. 존댓말 사용 안 함
- JSON 외 텍스트(설명/마크다운/코드펜스) 일절 출력 금지
- summary는 한 줄, 다른 배열 항목은 각 50자 이내"""


class LlmClient:
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.anthropic_api_key
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY 미설정")
        self._client = anthropic.AsyncAnthropic(api_key=key)
        self._model = settings.llm_model_id
        self._max_tokens = settings.llm_max_tokens

    async def generate_insight(
        self, input_context: dict[str, Any]
    ) -> tuple[str, dict[str, Any], int, int]:
        """LLM 호출. (raw_text, parsed_json, input_tokens, output_tokens) 반환."""
        user_msg = json.dumps(input_context, ensure_ascii=False, default=_json_default)
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
        parsed = _try_parse_json(raw_text)
        in_tok = int(getattr(response.usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(response.usage, "output_tokens", 0) or 0)
        return raw_text, parsed, in_tok, out_tok


def _try_parse_json(text: str) -> dict[str, Any]:
    """JSON 파싱. 실패 시 빈 dict. 응답 앞뒤 잡문자 있으면 첫 '{' ~ 마지막 '}'까지만."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            pass
    logger.warning("LLM response JSON parse failed (length=%d)", len(text))
    return {}


def _json_default(obj: Any) -> Any:
    """datetime 등 직렬화 fallback."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


_client: LlmClient | None = None


def get_llm_client() -> LlmClient:
    global _client
    if _client is None:
        _client = LlmClient()
    return _client
