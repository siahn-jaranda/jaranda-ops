-- matching-ops LLM 인사이트 캐시 + 일일 호출 카운터
-- 2026-05-18 신진섭. POSTGRES_15. DB=matching_ops, app user=matching_ops_app.
-- input_hash가 현재 입력 해시와 다르면 cache miss → 재호출. 한 신청서당 1 row.

CREATE TABLE IF NOT EXISTS matching_ops_llm_insight_cache (
    application_sid VARCHAR(64)  PRIMARY KEY,
    input_hash      CHAR(64)     NOT NULL,
    model_id        VARCHAR(64)  NOT NULL,
    response_text   TEXT         NOT NULL,
    response_json   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    input_tokens    INTEGER      NOT NULL DEFAULT 0,
    output_tokens   INTEGER      NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_insight_created
    ON matching_ops_llm_insight_cache (created_at DESC);

GRANT ALL ON matching_ops_llm_insight_cache TO matching_ops_app;

-- KST 기준 일일 호출 카운터. 한도 초과 가드용.
CREATE TABLE IF NOT EXISTS matching_ops_llm_daily_counter (
    date_kst        DATE         PRIMARY KEY,
    call_count      INTEGER      NOT NULL DEFAULT 0,
    input_tokens    BIGINT       NOT NULL DEFAULT 0,
    output_tokens   BIGINT       NOT NULL DEFAULT 0,
    last_called_at  TIMESTAMPTZ
);

GRANT ALL ON matching_ops_llm_daily_counter TO matching_ops_app;
