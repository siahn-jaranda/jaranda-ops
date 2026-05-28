-- matching-ops 자동 디스패치 처리 이력
-- 2026-05-28 신진섭. POSTGRES_15. DB=matching_ops, app user=matching_ops_app.
--
-- recommendation_sid PK — 신청서 1회만 자동 처리 (idempotency).
-- dry_run=true 인 row는 자동화 제외 판정에서 카운트하지 않음 (live 전환 후 재대상).
-- 성공·실패 무관 무조건 INSERT (부분 실패 신청서도 자동 재시도 안 함; 운영자 수동 해제 시에만).

CREATE TABLE IF NOT EXISTS matching_ops_auto_run (
    recommendation_sid VARCHAR(64)  PRIMARY KEY,
    run_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    pool_size          INTEGER      NOT NULL DEFAULT 0,   -- LLM 입력 후보 풀 크기
    added_count        INTEGER      NOT NULL DEFAULT 0,   -- 콘솔 add_teachers 요청 수
    succeed_count      INTEGER      NOT NULL DEFAULT 0,   -- 콘솔 응답 succeedCount
    denied_count       INTEGER      NOT NULL DEFAULT 0,   -- visit-offers denied 길이
    llm_model_id       VARCHAR(64)  NOT NULL DEFAULT '',
    dry_run            BOOLEAN      NOT NULL DEFAULT FALSE,
    operator_email     VARCHAR(128) NOT NULL DEFAULT '',  -- 수동 트리거 운영자 (cron이면 scheduler-sa)
    error_message      TEXT
);

CREATE INDEX IF NOT EXISTS idx_auto_run_run_at
    ON matching_ops_auto_run (run_at DESC);

CREATE INDEX IF NOT EXISTS idx_auto_run_dry_run_run_at
    ON matching_ops_auto_run (dry_run, run_at DESC);

GRANT ALL ON matching_ops_auto_run TO matching_ops_app;
